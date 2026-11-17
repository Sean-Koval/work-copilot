"""Transactional local evidence ledger. Input records are data, never instructions."""
from __future__ import annotations
import hashlib
import json
import re
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import sqlite3
import threading
import time
from contextlib import contextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path
from filelock import FileLock
from .knowledge import ENVELOPE_VERSION, validate_envelope
from . import schema


class ConflictError(ValueError):
    """The caller edited an obsolete revision or stale evidence."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# Keys only this store writes; never taken from a connector's item (KAN-390).
_RESERVED_SOURCE_KEYS=('retained','redacted','captured_at')
# Keys that differ between replays of one revision without changing its content.
_OBSERVATION_KEYS=('observed_at','schema_version','captured_at')


def _meaningful(source):
    return {k:v for k,v in source.items() if k not in _OBSERVATION_KEYS}


def _content_hash(source):
    """sha256 of a source envelope without its observation times: what a redacted revision keeps (KAN-390)."""
    return hashlib.sha256(_json(_meaningful(source)).encode('utf-8')).hexdigest()


def _excerpt(body):
    """The passage of a source body that an offline draft quotes."""
    return (body or '').strip().replace('\n',' ')[:220]


def _scrub_draft(draft, source_id, quotes, revisions=None):
    """Remove ``source_id``'s body and quoted passages from one draft; returns (relevant, changed).

    With ``revisions``, only evidence captured at those revisions loses its body: an older
    revision was redacted while the current text is still kept.
    """
    if source_id not in draft.get('source_revisions',{}) and not any(e.get('id')==source_id for e in draft.get('evidence',[])):
        return False, False
    changed=False
    for entry in draft.get('evidence',[]):
        if entry.get('id')==source_id and entry.get('body') and (revisions is None or entry.get('revision') in revisions):
            entry['body']=''; entry['text_removed']=True; changed=True
    text=draft.get('text') or ''
    for quote in quotes:
        if quote and quote in text:
            text=text.replace(quote,'[text removed: retention limit or forget]'); changed=True
    draft.update(text=text,stale=True)
    return True, changed


def _clear_backup(path, retained, redacted, forgotten):
    """Remove expired and forgotten source text from one backup file, in place (KAN-390)."""
    db=sqlite3.connect(str(path))
    try:
        db.execute('PRAGMA secure_delete=ON')
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        gone=set(retained)|set(forgotten)
        old_revisions={}
        for sid,revision in redacted:
            old_revisions.setdefault(sid,set()).add(revision)
        quotes={}
        # A backup taken before an edit holds, as its current row, a revision live has since
        # replaced and redacted: that row's text goes too.
        superseded=set()
        if 'sources' in tables:
            for sid,revision,payload in db.execute('SELECT id,revision,payload FROM sources'):
                if sid in gone or (sid,revision) in redacted:
                    quotes.setdefault(sid,[]).append(_excerpt(json.loads(payload).get('body')))
                    if sid not in gone:
                        superseded.add((sid,revision))
        if 'source_revisions' in tables:
            for sid,revision,payload in db.execute('SELECT id,revision,payload FROM source_revisions'):
                if sid in gone or revision in old_revisions.get(sid,()):
                    quotes.setdefault(sid,[]).append(_excerpt(json.loads(payload).get('body')))
        with db:
            if 'drafts' in tables:
                for did,payload in db.execute('SELECT id,payload FROM drafts').fetchall():
                    draft=json.loads(payload); changed=False
                    for sid,found in quotes.items():
                        changed|=_scrub_draft(draft,sid,found,None if sid in gone else old_revisions[sid])[0]
                    if changed:
                        db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),did))
            if 'sources' in tables:
                for sid,mark in retained.items():
                    db.execute("UPDATE sources SET payload=json_set(payload,'$.body','','$.retained',json(?)) WHERE id=?",(_json(mark),sid))
            for sid,revision in superseded:
                mark=json.loads(redacted[(sid,revision)])['redacted']
                db.execute("UPDATE sources SET payload=json_set(payload,'$.body','','$.redacted',json(?)) WHERE id=? AND revision=?",(_json(mark),sid,revision))
            if 'source_revisions' in tables:
                for (sid,revision),pointer in redacted.items():
                    db.execute('UPDATE source_revisions SET payload=? WHERE id=? AND revision=?',(pointer,sid,revision))
            for sid in forgotten:
                for table,column in (('sources','id'),('source_revisions','id'),('source_mappings','source_id'),('source_intents','source_id'),
                                     ('evidence','source_id'),('manual_evidence','source_id'),('source_projects','source_id')):
                    if table in tables:
                        db.execute(f'DELETE FROM {table} WHERE {column}=?',(sid,))
            if 'source_search' in tables:
                for sid in set(retained)|set(forgotten)|{sid for sid,_ in superseded}:
                    db.execute('DELETE FROM source_search WHERE source_id=?',(sid,))
            if 'local_metadata' in tables:
                # A restored copy rebuilds search and project links from what is left.
                db.execute("DELETE FROM local_metadata WHERE key='projections'")
        if 'source_search' in tables:
            db.execute("INSERT INTO source_search(source_search) VALUES('optimize')")
            db.commit()
        db.execute('VACUUM')
    finally:
        db.close()


# Schema backups (pre-upgrade, pre-rename) are rollback copies: expiry clears the expired
# text inside them but never deletes one younger than this (KAN-390).
BACKUP_MIN_KEEP_DAYS=7


# Retention also runs when the store opens, at most this often (KAN-390).
RETENTION_INTERVAL=timedelta(hours=1)


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not result.tzinfo:
            raise ValueError("Timestamp requires timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("observed_at must be an ISO timestamp with timezone") from exc


def _id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


# Bump when what _index_source writes or how source_projects is derived changes: the next
# open then rebuilds both once, without a schema migration.
PROJECTION_VERSION=1


class Store:
    def __init__(self, path, now=None, timezone_name=None):
        self.timezone_name=timezone_name or os.environ.get("WORK_COPILOT_TIMEZONE", "UTC")
        try:
            self.timezone=ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone: "+self.timezone_name) from exc
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        # Include connection PRAGMAs: journal_mode can race before SQLite's
        # migration transaction exists. The same canonical path shares this lock
        # across processes as well as independently constructed thread instances.
        with FileLock(str(self.path)+'.init.lock', timeout=30, mode=0o600):
            try:
                self._initialize_database()
            except BaseException:
                if hasattr(self,'db'):
                    self.db.close()
                raise
        # Text past a connector's retention limit expires even when collection has stopped
        # or keeps failing (KAN-390). A failure is reported by apply_due_retention, never raised.
        try:
            self.retention_on_open=self.apply_due_retention()
        except sqlite3.Error:
            self.retention_on_open={'error':'database busy'}

    def _initialize_database(self):
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        # Deleted rows (expired bodies, forgotten sources) are overwritten, not left in free pages (KAN-390).
        self.db.execute('PRAGMA secure_delete=ON')
        # Ordered migrations, with a backup first when the database already holds data.
        self.migration=schema.migrate(self.db,self.path)
        # Backups hold captured text; keep none past the retention limit.
        schema.prune_backups(self.path)
        # Search and project links are kept current on write. Rebuild them only when the
        # marker is missing (a migration or an import cleared it) or the projection code
        # changed, never on every open (KAN-388).
        self.db.execute('BEGIN IMMEDIATE')
        built=self.db.execute("SELECT payload FROM local_metadata WHERE key='projections'").fetchone()
        if not built or json.loads(built[0]).get('version')!=PROJECTION_VERSION:
            self._rebuild_all_projections()
        self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _transaction(self):
        # Reserve the write lock before optimistic reads, including across processes.
        with self._lock:
            nested=self.db.in_transaction
            savepoint='nested_'+uuid4().hex
            self.db.execute('SAVEPOINT '+savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                if nested:
                    self.db.execute('ROLLBACK TO '+savepoint)
                    self.db.execute('RELEASE '+savepoint)
                else:
                    self.db.rollback()
                raise
            else:
                if nested:
                    self.db.execute('RELEASE '+savepoint)
                else:
                    self.db.commit()

    def now(self):
        value = self._clock() if callable(self._clock) else self._clock
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(self.timezone)

    def _calendar_date(self, value):
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        return (date.astimezone(self.timezone) if date.tzinfo else date).date().isoformat()

    def close(self):
        with self._lock:
            self.db.close()

    def export_jsonl(self, destination):
        """Write all recorded state as JSONL (one consistent snapshot); restore with import_jsonl.

        A read transaction: in WAL mode other processes keep writing meanwhile.
        """
        with self._lock:
            self.db.execute('BEGIN')
            try:
                return schema.export_jsonl(self.db,destination,self.now().isoformat())
            finally:
                self.db.rollback()

    @classmethod
    def import_jsonl(cls, source, path, **options):
        """Restore an export into a new database at ``path`` and open it.

        Built beside the target, opened and checked there, and only then moved into place, so
        a failed import leaves nothing behind and never touches an existing database.
        """
        path=Path(path).resolve()
        path.parent.mkdir(parents=True,exist_ok=True)
        staging=path.with_name(path.name+'.importing')
        with FileLock(str(path)+'.init.lock',timeout=30,mode=0o600):
            for existing in (path,*schema._sidecars(path)):
                if existing.exists():
                    raise ValueError(f'{existing} already exists; import restores into an empty data folder only')
            for leftover in (staging,*schema._sidecars(staging)):
                leftover.unlink(missing_ok=True)
            try:
                result=schema.import_jsonl(source,staging)
                check=cls(staging,**options)
                try:
                    # Read every record back through the store, then discard any plan it anchors.
                    check.db.execute('BEGIN')
                    try:
                        check.snapshot()
                    finally:
                        check.db.rollback()
                    check.db.execute('PRAGMA journal_mode=DELETE')
                finally:
                    check.close()
                try:
                    staging.chmod(0o600)
                except OSError:
                    pass
                if any(sidecar.exists() for sidecar in schema._sidecars(path)):
                    raise ValueError('A database journal appeared beside the target during import; nothing was restored')
                os.replace(staging,path)
            except Exception as exc:
                for leftover in (staging,*schema._sidecars(staging)):
                    leftover.unlink(missing_ok=True)
                if isinstance(exc,ValueError):
                    raise
                raise ValueError(f'Export holds records this Work Copilot cannot open ({type(exc).__name__}: {exc})') from exc
            finally:
                Path(str(staging)+'.init.lock').unlink(missing_ok=True)
        return cls(path,**options),result

    def _rows(self, table):
        return [json.loads(row['payload']) for row in self.db.execute(f'SELECT payload FROM {table}')]

    def list_sources(self, provider=None):
        """Full latest ledger for incremental adapters; includes deletion tombstones."""
        with self._lock:
            return [self._source(s["id"]) for s in self._rows("sources") if provider is None or s["provider"] == provider]

    def _source(self, sid):
        row = self.db.execute('SELECT payload FROM sources WHERE id=?', (sid,)).fetchone()
        if not row:
            return None
        source=json.loads(row[0])
        mapping=self.db.execute('SELECT revision,projects FROM source_mappings WHERE source_id=?',(sid,)).fetchone()
        if mapping:
            source['projects']=json.loads(mapping['projects'])
            source['mapping_revision']=source['revision']+'@mapping:'+str(mapping['revision'])
        elif not source.get('projects') and source.get('room_id'):
            bindings=[]
            for row in self.db.execute('SELECT p.id,p.name,c.rooms,c.revision FROM projects p JOIN project_config c ON c.project_id=p.id ORDER BY p.id'):
                if any(room['id']==source['room_id'] for room in json.loads(row['rooms'])):
                    bindings.append(row)
            source['projects']=[{'id':row['id'],'name':row['name']} for row in bindings]
            if bindings:
                source['mapping_revision']=source['revision']+'@rooms:'+_id(_json([(row['id'],row['revision']) for row in bindings]))
                source['mapping_origin']='configured_room'
        return source

    def _map_source(self, source_id, revision, projects):
        source=self._source(source_id)
        if not source:
            raise KeyError(source_id)
        if revision != source.get('mapping_revision',source['revision']):
            raise ConflictError('Source or mapping changed; reload before mapping')
        if not isinstance(projects,list) or any(not isinstance(p,dict) or not isinstance(p.get('id'),str) or not p['id'] or not isinstance(p.get('name',p['id']),str) or not isinstance(p.get('workstream',''),str) for p in projects):
            raise ValueError('Mappings require project id, optional name and workstream')
        previous=self.db.execute('SELECT revision FROM source_mappings WHERE source_id=?',(source_id,)).fetchone()
        self.db.execute('INSERT OR REPLACE INTO source_mappings VALUES(?,?,?)',(source_id,(previous[0]+1 if previous else 1),_json(projects)))
        self.db.execute('DELETE FROM source_projects WHERE source_id=?',(source_id,))
        if not source.get('deleted'):
            for project in projects:
                self.db.execute('INSERT OR IGNORE INTO projects VALUES(?,?)',(project['id'],project.get('name',project['id'])))
                self.db.execute('INSERT OR IGNORE INTO source_projects VALUES(?,?,?)',(source_id,project['id'],project.get('workstream','')))
        for draft in self._rows('drafts'):
            if source_id in draft['source_revisions'] or self._obligation(draft['obligation_id']).get('project_id') in {p['id'] for p in projects}:
                draft['stale']=True
                self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))
        self._index_source(self._source(source_id))
        return self._source(source_id)

    def map_source(self, source_id, revision, projects):
        with self._transaction():
            return self._map_source(source_id, revision, projects)

    def _obligation(self, oid):
        row = self.db.execute('SELECT payload FROM obligations WHERE id=?', (oid,)).fetchone()
        if not row:
            raise KeyError(oid)
        ob = json.loads(row[0])
        refs = [self._source(row[0]) for row in self.db.execute('SELECT source_id FROM evidence WHERE obligation_id=? UNION SELECT source_id FROM manual_evidence WHERE obligation_id=?', (oid,oid))]
        ob['evidence'] = [s for s in refs if s and not s.get('deleted')]
        ob['freshness'] = self._freshness(ob['evidence'])
        if ob.get('opened_at'):
            ob['age_hours'] = round(max(0.0,(self.now()-_instant(ob['opened_at'])).total_seconds())/3600,1)
        return ob

    def _freshness(self, evidence):
        if not evidence:
            return 'missing'
        for source in evidence:
            row=self.db.execute('SELECT connector FROM sources WHERE id=?',(source['id'],)).fetchone()
            if row and self._connector(row[0])['status'] != 'healthy':
                return 'coverage_gap'
        if any(self.now()-_instant(source['observed_at']) > timedelta(days=7) for source in evidence):
            return 'stale'
        return 'observed'

    def _put_obligation(self, ob):
        clean = {k:v for k,v in ob.items() if k not in ('evidence','freshness','age_hours')}
        self.db.execute('UPDATE obligations SET payload=? WHERE id=?', (_json(clean), ob['id']))

    def _connector(self, name):
        row = self.db.execute('SELECT payload FROM connectors WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else {'name':name,'status':'not_configured','checkpoint':None,'last_success':None,'error':None,'gap_since':None,'capabilities':['normalized_import'], 'limitations':'No live API integration or remote ACL verification.'}

    def _put_connector(self, value):
        self.db.execute('INSERT OR REPLACE INTO connectors VALUES (?,?)', (value['name'], _json(value)))

    def configure_connector(self, name, capabilities, limitations):
        if not isinstance(capabilities,list) or not all(isinstance(x,str) for x in capabilities) or not isinstance(limitations,str):
            raise ValueError('Connector capabilities must be strings and limitations text')
        with self._transaction():
            value=self._connector(name)
            value.update(capabilities=capabilities,limitations=limitations)
            self._put_connector(value)
            return value

    def set_connector(self, name, status, error=None):
        status={'connected':'healthy','syncing':'catching_up'}.get(status,status)
        if status not in ('healthy','auth_expired','auth_required','error','catching_up','not_configured','unsupported'):
            raise ValueError('Unsupported connector status')
        with self._transaction():
            value = self._connector(name)
            value.update(status=status,error=error)
            if status in ('auth_expired','auth_required','error'):
                value['gap_since'] = value.get('gap_since') or self.now().isoformat()
                for draft in self._rows('drafts'):
                    if any(self._source(sid) and self.db.execute('SELECT connector FROM sources WHERE id=?',(sid,)).fetchone()[0] == name for sid in draft['source_revisions']):
                        draft['stale']=True
                        self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))
            self._put_connector(value)
            return value

    def ingest(self, records, connector='import', checkpoint=None):
        if not isinstance(records,list):
            raise ValueError('Records must be a list')
        result={'inserted':0,'updated':0,'unchanged':0}
        with self._transaction():
            for item in records:
                if not isinstance(item,dict):
                    raise ValueError('Source envelope must be an object')
                source={k:v for k,v in item.items() if k not in _RESERVED_SOURCE_KEYS}
                source.setdefault('schema_version',ENVELOPE_VERSION)
                source.setdefault('observed_at',self.now().isoformat())
                # The shared Source contract (schema_version 1) covers id, revision,
                # provider, title, body, observed_at, obligations and url.
                validate_envelope(source)
                source.setdefault('projects',[])
                source.setdefault('obligations',[])
                source.setdefault('deleted',False)
                source.setdefault('url',None)
                if not isinstance(source['projects'],list) or not isinstance(source['obligations'],list):
                    raise ValueError('Projects and obligations must be lists')
                if self.db.execute('SELECT 1 FROM local_metadata WHERE key=?',('forgotten:'+source['id'],)).fetchone():
                    # The user asked to forget this source; a later collection does not copy it back.
                    result['forgotten']=result.get('forgotten',0)+1
                    continue
                old=self._source(source['id'])
                prior=self.db.execute('SELECT payload FROM source_revisions WHERE id=? AND revision=?',(source['id'],source['revision'])).fetchone()
                if prior:
                    # Observation time may differ on replay; content cannot.
                    # Payloads stored before schema_version existed are version 1.
                    # A redacted revision keeps only the hash of its content (KAN-390).
                    stored=json.loads(prior[0])
                    if (stored['redacted']['content_sha256']!=_content_hash(source)) if 'redacted' in stored else _meaningful(stored) != _meaningful(source):
                        raise ValueError('Source revision collision: payload changed without a new revision')
                    # Seen historical revisions are replay, not newer evidence.
                    result['unchanged']+=1
                    continue
                # When this copy arrived, so retention can't be put off by a far-future observed_at.
                source['captured_at']=self.now().isoformat()
                if old and _instant(source['observed_at']) < _instant(old['observed_at']):
                    self.db.execute('INSERT INTO source_revisions VALUES(?,?,?)',(source['id'],source['revision'],_json(source)))
                    result['unchanged']+=1
                    continue
                self.db.execute('INSERT INTO source_revisions VALUES(?,?,?)',(source['id'],source['revision'],_json(source)))
                self.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?)',(source['id'],source['revision'],_json(source),connector))
                self.db.execute('DELETE FROM source_projects WHERE source_id=?',(source['id'],))
                self.db.execute('DELETE FROM evidence WHERE source_id=?',(source['id'],))
                result['updated' if old else 'inserted']+=1
                if not source['deleted']:
                    for project in self._source(source['id'])['projects']:
                        if not isinstance(project,dict) or not isinstance(project.get('id'),str) or not project['id']:
                            raise ValueError('Projects require explicit stable IDs, never guessed aliases')
                        self.db.execute('INSERT OR IGNORE INTO projects VALUES(?,?)',(project['id'],project.get('name',project['id'])))
                        self.db.execute('INSERT OR IGNORE INTO source_projects VALUES(?,?,?)',(source['id'],project['id'],project.get('workstream','')))
                    for candidate in source['obligations']:
                        canonical=_json([candidate.get('project_id'),candidate['intent_key']])
                        existing_row=self.db.execute('SELECT obligation_id FROM source_intents WHERE source_id=? AND candidate_key=?',(source['id'],canonical)).fetchone()
                        if not existing_row:
                            existing_row=self.db.execute('SELECT id FROM obligations WHERE canonical=?',(canonical,)).fetchone()
                        oid=existing_row[0] if existing_row else _id(canonical)
                        if not existing_row and self.db.execute('SELECT 1 FROM obligations WHERE id=?',(oid,)).fetchone():
                            oid=_id(canonical+str(uuid4()))
                        exists=existing_row
                        if not exists:
                            owner=candidate.get('owner')
                            ob={'id':oid,'intent_key':candidate['intent_key'],'title':candidate['title'],'project_id':candidate.get('project_id'),'owner':owner,'status':candidate.get('status','open' if owner=='me' else 'candidate'),'question_state':candidate.get('question_state','pending'),'due_at':candidate.get('due_at'),'follow_up_at':candidate.get('follow_up_at'),'waiting_on':candidate.get('waiting_on'),'commitment':bool(candidate.get('commitment',False)),'revision':1,'created_at':self.now().isoformat(),'reviewed':False}
                            self._validate_changes(ob)
                            self.db.execute('INSERT INTO obligations VALUES(?,?,?)',(oid,canonical,_json(ob)))
                        if exists:
                            current=self._obligation(oid)
                            changed=[key for key in ('title','owner','status','question_state','due_at','follow_up_at','waiting_on','commitment') if key in candidate and candidate[key]!=current.get(key)]
                            if changed:
                                current['review_reason']='New source differs on '+', '.join(changed)+'. Existing reviewed state is preserved.'
                                current['revision']+=1
                                self._put_obligation(current)
                        self.db.execute('INSERT OR IGNORE INTO source_intents VALUES(?,?,?)',(source['id'],canonical,oid))
                        self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?)',(oid,source['id']))
                self._index_source(self._source(source['id']))
                for draft in self._rows('drafts'):
                    ob=self._obligation(draft['obligation_id'])
                    # Any project evidence update can change a grounded response.
                    relevant=source['id'] in draft['source_revisions'] or (ob.get('project_id') and ob['project_id'] in [p.get('id') for p in self._source(source['id'])['projects']])
                    if relevant:
                        draft['stale']=True
                        self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))
            value=self._connector(connector)
            if value.get('gap_since'):
                value['last_gap']={'since':value['gap_since'],'recovered_at':self.now().isoformat()}
            value.update(status='healthy',error=None,last_success=self.now().isoformat(),gap_since=None)
            if checkpoint is not None:
                value['checkpoint']=checkpoint
            self._put_connector(value)
        return result

    @staticmethod
    def _validate_changes(changes):
        if 'status' in changes and changes['status'] not in ('candidate','open','waiting','resolved','dismissed'):
            raise ValueError('Invalid obligation status')
        if 'question_state' in changes and changes['question_state'] not in ('pending','sent','answered'):
            raise ValueError('Invalid question state')
        if 'owner' in changes and changes['owner'] is not None and not isinstance(changes['owner'],str):
            raise ValueError('Owner must be a stable string or null')
        for field in ('due_at','follow_up_at'):
            if field in changes and changes[field] is not None:
                try: datetime.fromisoformat(changes[field].replace('Z','+00:00'))
                except (ValueError,TypeError,AttributeError): raise ValueError(field+' must be ISO date or datetime')
        for field in ('title','waiting_on','project_id'):
            if field in changes and changes[field] is not None and not isinstance(changes[field],str):
                raise ValueError(field+' must be text')
        if 'commitment' in changes and not isinstance(changes['commitment'],bool):
            raise ValueError('commitment must be boolean')

    def review(self, id, revision, changes):
        allowed={'owner','status','question_state','due_at','commitment','project_id','title','follow_up_at','waiting_on'}
        if not isinstance(changes,dict) or not changes or set(changes)-allowed:
            raise ValueError('Unsupported review fields')
        self._validate_changes(changes)
        with self._transaction():
            ob=self._obligation(id)
            if ob['revision'] != revision:
                raise ConflictError('Obligation changed; reload before reviewing')
            if changes.get('project_id') and not self.db.execute('SELECT 1 FROM projects WHERE id=?',(changes['project_id'],)).fetchone():
                raise ValueError('Unknown project; import an explicit project first')
            if 'project_id' in changes:
                canonical=_json([changes['project_id'],ob['intent_key']])
                duplicate=self.db.execute('SELECT id FROM obligations WHERE canonical=? AND id!=?',(canonical,id)).fetchone()
                if duplicate:
                    raise ConflictError('That project already has this intent; resolve duplicate explicitly')
                self.db.execute('UPDATE obligations SET canonical=? WHERE id=?',(canonical,id))
            if changes.get('project_id'):
                project=self.db.execute('SELECT id,name FROM projects WHERE id=?',(changes['project_id'],)).fetchone()
                for source in ob['evidence']:
                    if not source['projects']:
                        self._map_source(source['id'],source.get('mapping_revision',source['revision']),[dict(project)])
            ob.update(changes)
            ob.update(revision=ob['revision']+1,reviewed=True,review_reason=None)
            self._put_obligation(ob)
            for draft in self._rows('drafts'):
                if draft['obligation_id']==id:
                    draft['stale']=True
                    self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))
            return self._obligation(id)

    def project(self, id):
        with self._lock:
            row=self.db.execute('SELECT * FROM projects WHERE id=?',(id,)).fetchone()
            if not row: raise KeyError(id)
            links=self.db.execute('SELECT source_id,workstream FROM source_projects WHERE project_id=?',(id,)).fetchall()
            sources=list({r['source_id']:self._source(r['source_id']) for r in links}.values())
            obligations=[self._obligation(o['id']) for o in self._rows('obligations') if o.get('project_id')==id]
            rooms={s['room_id']:{'id':s['room_id'],'name':s.get('room_name') or s['room_id']} for s in sources if s.get('room_id')}
            config=self._project_config(id)
            rooms.update({room['id']:room for room in config['rooms']})
            return {'id':id,'name':row['name'],'aliases':config['aliases'],'config_revision':config['config_revision'],'configured_rooms':config['rooms'],'workstreams':sorted({r['workstream'] for r in links if r['workstream']}),'rooms':list(rooms.values()),'sources':sources,'obligations':obligations,'drafts':[d for d in self._drafts() if d['obligation_id'] in {o['id'] for o in obligations}]}

    def _today(self, active, now):
        """Anchor today's plan from active obligation payloads; may persist the plan."""
        day=now.date().isoformat()
        row=self.db.execute('SELECT payload FROM plans WHERE day=?',(day,)).fetchone()
        existing=json.loads(row[0]) if row else None
        eligible=[o for o in active if o.get('owner')=='me' and o['status']=='open']
        eligible_ids={o['id'] for o in eligible}
        if existing and (existing.get('anchored') or existing['priorities']):
            today=existing
            today.setdefault('revision',1)
            survivors=[oid for oid in today['priorities'] if oid in eligible_ids]
            if survivors!=today['priorities']:
                today.update(priorities=survivors,revision=today['revision']+1,anchored=True)
                self.db.execute('INSERT OR REPLACE INTO plans VALUES(?,?)',(day,_json(today)))
        else:
            ranked=sorted(eligible,key=lambda o:(o.get('follow_up_at') or o['due_at'] or '9999',o['created_at'],o['id']))
            today={'date':day,'priorities':[o['id'] for o in ranked[:3]],'kind':'suggested','anchored_at':now.isoformat(),'revision':(existing.get('revision',1)+1 if existing and ranked else existing.get('revision',1) if existing else 1),'anchored':bool(ranked)}
            if today['priorities'] or not existing:
                self.db.execute('INSERT OR REPLACE INTO plans VALUES(?,?)',(day,_json(today)))
        today['new_urgency']=[o['id'] for o in active if o['id'] not in today['priorities'] and ((o.get('due_at') and self._calendar_date(o['due_at'])<=day) or (o.get('follow_up_at') and self._calendar_date(o['follow_up_at'])<=day))]
        return today

    def _week(self, active, now):
        day=now.date().isoformat()
        monday=now.date()-timedelta(days=now.weekday())
        week_end=(monday+timedelta(days=6)).isoformat()
        this_week=[]; undated=[]; backlog=[]; overdue=[]
        for obligation in active:
            dates=[self._calendar_date(obligation[field]) for field in ('due_at','follow_up_at') if obligation.get(field)]
            if not dates:
                undated.append(obligation)
            elif any(date<day for date in dates):
                overdue.append(obligation)
            elif any(monday.isoformat()<=date<=week_end for date in dates):
                this_week.append(obligation)
            else:
                backlog.append(obligation)
        return {'start':monday.isoformat(),'end':week_end,'commitments':[o for o in this_week if o['commitment']],'suggestions':[o for o in this_week if not o['commitment']],'overdue':overdue,'overdue_count':len(overdue),'undated':undated,'backlog':backlog,'undated_count':len(undated),'backlog_count':len(backlog)}

    def _refresh_needed(self):
        row=self.db.execute("SELECT payload FROM local_metadata WHERE key='refresh_needed'").fetchone()
        return json.loads(row[0]) if row else {'needed':False}

    COVERAGE_NOTE='Only explicitly configured and observed sources are covered. Vendor semantics and upstream ACL enforcement are not guaranteed. Laptop sleep pauses capture.'

    def snapshot(self):
        with self._transaction():
            now=self.now()
            obligations=[self._obligation(o['id']) for o in self._rows('obligations')]
            active=[o for o in obligations if o['status'] not in ('resolved','dismissed')]
            today=self._today(active,now)
            week=self._week(active,now)
            recent=[self._source(row[0]) for row in self.db.execute("SELECT id FROM sources ORDER BY json_extract(payload,'$.observed_at') DESC,rowid LIMIT 30")]
            return {'refresh_needed':self._refresh_needed(),'timezone':self.timezone_name,'projects':[{**dict(p),**self._project_config(p['id'])} for p in self.db.execute('SELECT * FROM projects ORDER BY name')],'obligations':obligations,'drafts':self._drafts(),'connectors':self._rows('connectors'),'today':today,'week':week,'recent':recent,'review':[o for o in active if o.get('kind')!='review' and (o['status']=='candidate' or not o.get('owner') or not o.get('project_id') or o.get('review_reason'))],'review_queue':sorted([o for o in active if o.get('kind')=='review'],key=lambda o:(-o.get('age_hours',0),o['id'])),'coverage_note':self.COVERAGE_NOTE}

    def attention(self, cursor=0, limit=10, status=None, project_id=None, project_limit=30):
        """What get_attention needs, from narrow queries (KAN-388).

        Plans and week buckets read obligation payloads only; evidence and freshness are
        loaded for the requested page alone, and no source body is read.
        """
        if not isinstance(cursor,int) or cursor<0 or not isinstance(limit,int) or not 1<=limit<=30:
            raise ValueError('cursor must be nonnegative; limit must be 1..30')
        with self._transaction():
            now=self.now()
            active=[json.loads(row[0]) for row in self.db.execute("SELECT payload FROM obligations WHERE json_extract(payload,'$.status') NOT IN ('resolved','dismissed') ORDER BY rowid")]
            today=self._today(active,now)
            week=self._week(active,now)
            clauses=[]; args=[]
            if status is not None:
                clauses.append("json_extract(payload,'$.status')=?"); args.append(status)
            if project_id is not None:
                clauses.append("json_extract(payload,'$.project_id')=?"); args.append(project_id)
            where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
            total=self.db.execute('SELECT count(*) FROM obligations'+where,args).fetchone()[0]
            page=[self._obligation(row[0]) for row in self.db.execute('SELECT id FROM obligations'+where+' ORDER BY rowid LIMIT ? OFFSET ?',[*args,limit,cursor])]
            projects=[dict(row) for row in self.db.execute('SELECT id,name FROM projects ORDER BY name LIMIT ?',(project_limit,))]
            return {'obligations':page,'page':{'total':total,'next_cursor':cursor+limit if cursor+limit<total else None,'truncated':cursor+limit<total},
                    'projects':projects,'project_total':self.db.execute('SELECT count(*) FROM projects').fetchone()[0],
                    'refresh_needed':self._refresh_needed(),'today':today,'week':week,'connectors':self._rows('connectors'),'coverage_note':self.COVERAGE_NOTE}

    def source(self, source_id):
        """One source by id, or None; reads that row only."""
        with self._lock:
            return self._source(source_id)

    def obligation(self, obligation_id):
        """One obligation with its evidence and freshness; KeyError if unknown."""
        with self._lock:
            return self._obligation(obligation_id)

    def drafts_for(self, obligation_id):
        with self._lock:
            return [self._current_draft(json.loads(row[0])) for row in self.db.execute("SELECT payload FROM drafts WHERE json_extract(payload,'$.obligation_id')=? ORDER BY rowid",(obligation_id,))]

    def connectors(self):
        with self._lock:
            return self._rows('connectors')

    def draft(self, id):
        with self._transaction():
            ob=self._obligation(id)
            project_evidence=self.project(ob['project_id'])['sources'] if ob.get('project_id') else []
            evidence=list({source['id']:source for source in ob['evidence']+project_evidence}.values())
            evidence=[s for s in evidence if not s.get('deleted')]
            gaps=[]
            if not ob.get('owner'): gaps.append('Ownership is not confirmed.')
            if not evidence: gaps.append('No current source evidence is available.')
            if not ob.get('due_at'): gaps.append('No deadline is established.')
            gaps.append('Deployment is not verified unless explicitly evidenced; a merged PR alone is not deployment.')
            direct={s['id'] for s in ob['evidence']}
            evidence.sort(key=lambda source:(source['id'] not in direct,source['id']))
            if any(word in ob['intent_key'].lower() for word in ('release','deployment')) and len(evidence)>1:
                engineering=next((source for source in evidence[1:] if 'github' in source['provider'].lower()),None)
                if engineering:
                    evidence.remove(engineering)
                    evidence.insert(1,engineering)
            if ob['intent_key']=='request-missing-transcript' and ob['question_state']=='sent':
                lines=['The transcript request is already recorded as sent. Check for a response before sending another request.']
            elif ob['intent_key']=='request-missing-transcript' and ob['question_state']=='answered':
                lines=['The transcript request is recorded as answered. Review the received evidence; do not request it again automatically.']
            elif ob['intent_key']=='request-missing-transcript':
                lines=['Could you share the meeting transcript or notes when available? I do not have the transcript, so I cannot yet summarize the discussion or confirm its decisions.']
            else:
                lines=[]
                for index,source in enumerate(evidence[:2],1):
                    if source.get('retained'):
                        lines.append(f'Observed source [{index}]: its text expired under the retention limit; open the link to read it.')
                        continue
                    lines.append(f'Observed source [{index}] says: “{_excerpt(source["body"])}”')
                if not evidence:
                    lines.append('I do not have current evidence to give a reliable status yet.')
                if any(word in ob['intent_key'].lower() for word in ('release','deployment')):
                    lines.append('Deployment is not verified by this evidence packet; merged code is not a deployment receipt. No delivery date is established here.')
                if ob['question_state']=='sent':
                    lines.append('A clarification is already recorded as sent; check for a response rather than ask again.')
                elif ob['question_state']=='answered':
                    lines.append('This intent is already recorded as answered; check whether another reply is needed.')
            freshness=self._freshness(evidence)
            if freshness!='observed':
                gaps.append('Evidence is '+freshness+'; refresh or obtain missing evidence before relying on this draft.')
            did='draft-'+id
            prior=self.db.execute('SELECT payload FROM drafts WHERE id=?',(did,)).fetchone()
            revision=json.loads(prior[0])['revision']+1 if prior else 1
            draft={'id':did,'obligation_id':id,'text':'\n\n'.join(lines),'revision':revision,'mode':'offline_template','stale':freshness!='observed','evidence':evidence,'gaps':gaps,'reviewed':False,'source_revisions':{s['id']:s.get('mapping_revision',s['revision']) for s in evidence},'obligation_revision':ob['revision'],'created_at':self.now().isoformat()}
            self.db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?)',(did,_json(draft)))
            return draft

    def _current_draft(self, draft):
        current=[self._source(sid) for sid in draft['source_revisions']]
        valid=[source for source in current if source and not source.get('deleted')]
        changed=any(not source or source.get('deleted') or source.get('mapping_revision',source['revision']) != draft['source_revisions'][sid] for sid,source in zip(draft['source_revisions'],current))
        draft['stale']=draft['stale'] or changed or self._freshness(valid)!='observed' or self._obligation(draft['obligation_id'])['revision'] != draft['obligation_revision']
        return draft

    def _drafts(self):
        return [self._current_draft(draft) for draft in self._rows('drafts')]

    def save_draft(self, id, revision, text):
        if not isinstance(text,str) or len(text)>100000: raise ValueError('Draft text must be a string of at most 100000 characters')
        with self._transaction():
            row=self.db.execute('SELECT payload FROM drafts WHERE id=?',(id,)).fetchone()
            if not row: raise KeyError(id)
            draft=self._current_draft(json.loads(row[0]))
            if draft['revision']!=revision or draft['stale']: raise ConflictError('Draft or evidence changed; regenerate and review')
            draft.update(text=text,revision=revision+1,reviewed=True)
            self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),id))
            return draft

    def _index_source(self, source):
        self.db.execute('DELETE FROM source_search WHERE source_id=?',(source['id'],))
        if not source.get('deleted'):
            parts=[]
            for project in source['projects']:
                canonical=self.db.execute('SELECT name FROM projects WHERE id=?',(project['id'],)).fetchone()
                parts.extend([project['id'],project.get('name',project['id']),canonical['name'] if canonical else '',*self._project_config(project['id'])['aliases']])
            terms=' '.join(parts)
            self.db.execute('INSERT INTO source_search VALUES(?,?,?,?)',(source['id'],source['title'],source['body'],terms))

    def search(self, query, project_id=None, workstream=None, provider=None, since=None, until=None, limit=30):
        """Ranked lexical retrieval; explicit project IDs/names are indexed aliases.

        Query operators are treated as plain words, not executable FTS syntax.
        Time bounds are inclusive ISO dates/timestamps against observed_at.
        """
        if not isinstance(query,str) or len(query)>500 or not isinstance(limit,int) or not 1<=limit<=100:
            raise ValueError('Query max 500 characters; limit must be 1..100')
        words=re.findall(r'\w+',query,flags=re.UNICODE)
        if not words:
            return []
        match=' AND '.join('"'+word+'"' for word in words)
        clauses=['source_search MATCH ?']; args=[match]
        if project_id or workstream:
            link=['sp.source_id=s.id']
            if project_id:
                link.append('sp.project_id=?'); args.append(project_id)
            if workstream:
                link.append('sp.workstream=?'); args.append(workstream)
            clauses.append('EXISTS(SELECT 1 FROM source_projects sp WHERE '+' AND '.join(link)+')')
        if provider:
            clauses.append("json_extract(s.payload,'$.provider')=?"); args.append(provider)
        for value,operator in ((since,'>='),(until,'<=')):
            if value is not None:
                try: parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                except (ValueError,AttributeError,TypeError): raise ValueError('Time filters must be ISO dates or timestamps')
                if len(value)==10 and operator=='<=':
                    parsed+=timedelta(days=1)
                    operator='<'
                if not parsed.tzinfo:
                    parsed=parsed.replace(tzinfo=self.timezone)
                value=parsed.isoformat()
                clauses.append("julianday(json_extract(s.payload,'$.observed_at'))"+operator+'julianday(?)'); args.append(value)
        sql="SELECT source_search.source_id,bm25(source_search,0,5,1,2) AS rank,snippet(source_search,2,'[',']',' … ',28) AS snippet FROM source_search JOIN sources s ON s.id=source_search.source_id WHERE "+' AND '.join(clauses)+' ORDER BY rank,source_search.source_id LIMIT ?'
        args.append(limit)
        with self._lock:
            results=[]
            for row in self.db.execute(sql,args):
                source=self._source(row['source_id'])
                source.update(source_id=source['id'],rank=row['rank'],snippet=row['snippet'])
                results.append(source)
            return results

    def track_obligation(self, payload):
        """Create a local user-entered waiting/action record, never an external send."""
        allowed={'intent_key','title','project_id','owner','status','waiting_on','follow_up_at','due_at','question_state','commitment','evidence_ids','body'}
        if not isinstance(payload,dict) or set(payload)-allowed or not all(isinstance(payload.get(k),str) and payload[k].strip() for k in ('intent_key','title')):
            raise ValueError('Structured obligation requires intent_key and title; unknown fields rejected')
        body=payload.get('body','')
        if not isinstance(body,str) or len(body)>100000:
            raise ValueError('Body must be text of at most 100000 characters')
        candidate={k:v for k,v in payload.items() if k not in ('evidence_ids','body')}
        candidate.setdefault('owner','me'); candidate.setdefault('status','waiting')
        self._validate_changes(candidate)
        ids=payload.get('evidence_ids',[])
        if not isinstance(ids,list) or not all(isinstance(sid,str) for sid in ids):
            raise ValueError('evidence_ids must be a list of stable source IDs')
        with self._transaction():
            for sid in ids:
                evidence=self._source(sid)
                if not evidence or evidence.get('deleted'):
                    raise ValueError('Evidence source is missing or deleted: '+sid)
            project=None
            if candidate.get('project_id'):
                row=self.db.execute('SELECT id,name FROM projects WHERE id=?',(candidate['project_id'],)).fetchone()
                if not row:
                    self.db.execute('INSERT INTO projects VALUES(?,?)',(candidate['project_id'],candidate['project_id']))
                    project={'id':candidate['project_id'],'name':candidate['project_id']}
                else:
                    project=dict(row)
            canonical=_json([candidate.get('project_id'),candidate['intent_key']])
            existing=self.db.execute('SELECT id FROM obligations WHERE canonical=?',(canonical,)).fetchone()
            if existing:
                return self._obligation(existing[0])
            source={'id':'local/obligation/'+_id(canonical),'revision':'1','provider':'local-user','title':'User-entered obligation: '+candidate['title'],'body':'Explicitly entered by the local user. '+candidate['title']+('\n\n'+body if body else ''),'projects':[project] if project else [],'obligations':[candidate]}
            self.ingest([source],connector='local-user')
            oid=self.db.execute('SELECT id FROM obligations WHERE canonical=?',(canonical,)).fetchone()[0]
            for sid in ids:
                self.db.execute('INSERT OR IGNORE INTO manual_evidence VALUES(?,?)',(oid,sid))
            return self._obligation(oid)

    def sync_review_queue(self, items, connector='knowledge-review', observed_at=None):
        """Replace the read-only review queue with ``items``; absent items resolve themselves.

        Each item becomes a source (its evidence) and an open ``review`` obligation owned by
        the user. An item missing from a later collect is resolved, and reopens if it returns.
        Nothing here writes outside the local store.
        """
        if not isinstance(items,list):
            raise ValueError('Review queue items must be a list')
        observed=observed_at or self.now().isoformat()
        present=set(); opened=[]; reopened=[]; resolved=[]
        fields=('title','review_kind','url','path','opened_at','due_at','draft')
        with self._transaction():
            for item in items:
                if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('key','title','body','revision','opened_at')):
                    raise ValueError('Review items need key, title, body, revision and opened_at')
                _instant(item['opened_at'])
                self._validate_changes({k:item.get(k) for k in ('title','due_at')})
                sid='review/'+item['key']
                url=item.get('url') if isinstance(item.get('url'),str) and item['url'].startswith(('https://','http://')) else None
                self.ingest([{'id':sid,'revision':item['revision'],'provider':connector,'title':item['title'],'body':item['body'],'url':url,'observed_at':observed}],connector=connector)
                canonical=_json([None,'review:'+item['key']])
                row=self.db.execute('SELECT id FROM obligations WHERE canonical=?',(canonical,)).fetchone()
                values={k:item.get(k) for k in fields} | {'url':url}
                if row:
                    ob=self._obligation(row[0])
                    changed={k:v for k,v in values.items() if ob.get(k)!=v}
                    if ob['status']=='resolved' and ob.get('auto_resolved'):
                        changed.update(status='open',auto_resolved=False,resolved_at=None)
                        reopened.append(ob['id'])
                    if changed:
                        ob.update(changed); ob['revision']+=1
                        self._put_obligation(ob)
                    oid=ob['id']
                else:
                    oid=_id(canonical)
                    ob={'id':oid,'intent_key':'review:'+item['key'],'kind':'review',**values,'project_id':None,'owner':'me','status':'open','question_state':'pending','follow_up_at':None,'waiting_on':None,'commitment':False,'revision':1,'created_at':self.now().isoformat(),'reviewed':True}
                    self.db.execute('INSERT INTO obligations VALUES(?,?,?)',(oid,canonical,_json(ob)))
                    opened.append(oid)
                self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?)',(oid,sid))
                present.add(oid)
            for ob in self._rows('obligations'):
                if ob.get('kind')=='review' and ob['id'] not in present and ob['status'] not in ('resolved','dismissed'):
                    ob.update(status='resolved',auto_resolved=True,resolved_at=self.now().isoformat(),revision=ob['revision']+1)
                    self._put_obligation(ob)
                    resolved.append(ob['id'])
        return {'open':len(present),'opened':opened,'reopened':reopened,'resolved':resolved}

    def _project_config(self, id):
        row=self.db.execute('SELECT aliases,rooms,revision FROM project_config WHERE project_id=?',(id,)).fetchone()
        return {'aliases':json.loads(row['aliases']),'rooms':json.loads(row['rooms']),'configured_rooms':json.loads(row['rooms']),'config_revision':row['revision']} if row else {'aliases':[],'rooms':[],'configured_rooms':[],'config_revision':1}

    def create_project(self, id, name, aliases=None, rooms=None):
        """Explicit local configuration, not fabricated source evidence.

        IDs and aliases may overlap across projects; they never auto-assign notes.
        Repeated identical creation is idempotent; conflicting setup needs review.
        """
        aliases=[] if aliases is None else aliases
        rooms=[] if rooms is None else rooms
        if not isinstance(id,str) or not id.strip() or len(id)>200 or not isinstance(name,str) or not name.strip() or len(name)>300:
            raise ValueError('Project requires a stable id and display name')
        if not isinstance(aliases,list) or not all(isinstance(alias,str) and alias.strip() and len(alias)<=200 for alias in aliases):
            raise ValueError('Aliases must be nonempty strings')
        if not isinstance(rooms,list) or any(not isinstance(room,dict) or set(room)-{'id','name'} or not isinstance(room.get('id'),str) or not room['id'] or not isinstance(room.get('name'),str) or not room['name'] for room in rooms):
            raise ValueError('Rooms require explicit id and name')
        if len({room['id'] for room in rooms})!=len(rooms):
            raise ValueError('Room IDs must be unique within a project')
        aliases=list(dict.fromkeys(aliases))
        with self._transaction():
            configured=self.db.execute('SELECT 1 FROM project_config WHERE project_id=?',(id,)).fetchone()
            if configured:
                project=self.project(id)
                config=self._project_config(id)
                if project['name']!=name or config['aliases']!=aliases or config['rooms']!=rooms:
                    raise ConflictError('Project already configured; existing setup was not overwritten')
                return project
            self.db.execute('INSERT OR REPLACE INTO projects VALUES(?,?)',(id,name))
            self.db.execute('INSERT INTO project_config(project_id,aliases,rooms) VALUES(?,?,?)',(id,_json(aliases),_json(rooms)))
            self._rebuild_project_projections(id,{room['id'] for room in rooms})
            return self.project(id)

    def request_refresh(self, reason, source_id=None):
        """Persist a local reminder only; never initiate network calls or sending."""
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError('Refresh reason must be nonempty text, at most 2000 characters')
        if source_id is not None and not isinstance(source_id,str):
            raise ValueError('source_id must be a string or null')
        with self._transaction():
            if source_id is not None and not self._source(source_id):
                raise ValueError('Refresh source does not exist')
            value={'needed':True,'reason':reason,'source_id':source_id,'requested_at':self.now().isoformat(),'network_triggered':False}
            self.db.execute('INSERT OR REPLACE INTO local_metadata VALUES(?,?)',('refresh_needed',_json(value)))
            return value

    def set_priorities(self, ids, revision):
        """Pin an exact ordered local focus list; this creates no commitment."""
        if not isinstance(ids,list) or len(ids)>3 or not all(isinstance(oid,str) for oid in ids) or len(set(ids))!=len(ids):
            raise ValueError('Priorities must contain up to three unique obligation IDs')
        if type(revision) is not int or revision<1:
            raise ValueError('A positive integer plan revision is required')
        with self._transaction():
            today=self.snapshot()['today']
            if today['revision']!=revision:
                raise ConflictError('Today changed; reload before pinning priorities')
            for oid in ids:
                ob=self._obligation(oid)
                if ob.get('owner')!='me' or ob['status']!='open':
                    raise ValueError('Only personally owned open obligations can be pinned')
            today.update(priorities=list(ids),kind='pinned',revision=revision+1,anchored=True)
            today.pop('new_urgency',None)
            self.db.execute('INSERT OR REPLACE INTO plans VALUES(?,?)',(today['date'],_json(today)))
            return self.snapshot()['today']

    def _project_source(self, source):
        """Derive one source's project links and search row from its recorded state."""
        self.db.execute('DELETE FROM source_projects WHERE source_id=?',(source['id'],))
        if not source.get('deleted'):
            for project in source['projects']:
                self.db.execute('INSERT OR IGNORE INTO source_projects VALUES(?,?,?)',(source['id'],project['id'],project.get('workstream','')))
        self._index_source(source)

    def _rebuild_all_projections(self):
        """Recreate search and project links from recorded state (migration, import, repair)."""
        self.db.execute('DELETE FROM source_search')
        self.db.execute('DELETE FROM source_projects')
        for source in self.list_sources():
            self._project_source(source)
        self.db.execute('INSERT INTO local_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',('projections',_json({'version':PROJECTION_VERSION})))

    def set_retention(self, connector, days):
        """Record how long ``connector``'s text is kept, so retention also runs on open (KAN-390)."""
        if type(days) is not int or days<1:
            raise ValueError('retention_days must be a positive integer')
        with self._transaction():
            value=self._connector(connector)
            if value.get('retention_days')!=days:
                value['retention_days']=days
                self._put_connector(value)

    def has_source(self, source_id):
        with self._lock:
            return self.db.execute('SELECT 1 FROM sources WHERE id=?',(source_id,)).fetchone() is not None

    def apply_due_retention(self, force=False):
        """Run retention for each connector with a limit, at most once per RETENTION_INTERVAL.

        Called when the store opens, so text expires even when collection stops or fails.
        A failure here never stops the store from opening; it is reported instead.
        """
        report={}
        for connector in self._rows('connectors'):
            days=connector.get('retention_days')
            if type(days) is not int or days<1:
                continue
            last=self.db.execute('SELECT payload FROM local_metadata WHERE key=?',('retention-run:'+connector['name'],)).fetchone()
            if not force and last and self.now()-_instant(json.loads(last[0])['at'])<RETENTION_INTERVAL:
                continue
            try:
                report[connector['name']]=self.apply_retention(connector['name'],days)
            except (sqlite3.Error,OSError,ValueError) as exc:
                report[connector['name']]={'error':type(exc).__name__}
        return report

    def apply_retention(self, connector, days):
        """Keep captured text from ``connector`` for ``days`` only (KAN-390).

        A source captured or observed more than ``days`` ago, or deleted upstream, keeps a
        pointer: its id, link, title, projects and the sha256 of its body, which is removed.
        Every older revision keeps only the hash of its content, so a replay is still
        recognized. Drafts lose the expired text they quoted. Obligations (their titles) and
        evidence links stay. The same text is cleared inside schema backups, which are kept as
        rollback copies under the usual backup cleanup (see _scrub_backups).
        """
        if type(days) is not int or days<1:
            raise ValueError('retention_days must be a positive integer')
        now=self.now(); cutoff=now-timedelta(days=days)
        expired=redacted=scrubbed=0
        with self._transaction():
            for sid,payload in self.db.execute('SELECT id,payload FROM sources WHERE connector=?',(connector,)).fetchall():
                source=json.loads(payload)
                age=min(_instant(source['observed_at']),_instant(source.get('captured_at') or source['observed_at']))
                gone=bool(source.get('deleted')) or age<cutoff
                for revision,stored in self.db.execute('SELECT revision,payload FROM source_revisions WHERE id=?',(sid,)).fetchall():
                    stored=json.loads(stored)
                    if 'redacted' in stored or (revision==source['revision'] and not gone):
                        continue
                    pointer={'id':sid,'revision':revision,'redacted':{'content_sha256':_content_hash(stored),'at':now.isoformat()}}
                    self.db.execute('UPDATE source_revisions SET payload=? WHERE id=? AND revision=?',(_json(pointer),sid,revision))
                    scrubbed+=self._scrub_drafts(sid,[stored.get('body')],None if gone else {revision})
                    redacted+=1
                if gone and not source.get('retained'):
                    body=source.get('body') or ''
                    source.update(body='',retained={'expired_at':now.isoformat(),'retention_days':days,'body_sha256':hashlib.sha256(body.encode('utf-8')).hexdigest()})
                    self.db.execute('UPDATE sources SET payload=? WHERE id=?',(_json(source),sid))
                    self._project_source(self._source(sid))
                    scrubbed+=self._scrub_drafts(sid,[body])
                    expired+=1
            self.db.execute('INSERT INTO local_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',('retention-run:'+connector,_json({'at':now.isoformat(),'days':days})))
        wal_cleared=self._compact(optimize=expired>0) if (expired or redacted or scrubbed) else True
        pending=self.db.execute("SELECT 1 FROM local_metadata WHERE key='backups-pending'").fetchone()
        backups=self._scrub_backups() if (expired or redacted or pending) else {'backups_cleared':0,'backups_removed':0,'backups_left':0}
        return {'expired_bodies':expired,'redacted_revisions':redacted,'drafts_scrubbed':scrubbed,'wal_cleared':wal_cleared,**backups}

    def _scrub_drafts(self, source_id, bodies, revisions=None):
        """Remove a source's text from every draft that quotes it; the draft is marked stale."""
        quotes=[_excerpt(body) for body in bodies if body]
        count=0
        for draft in self._rows('drafts'):
            was_stale=draft.get('stale')
            relevant,changed=_scrub_draft(draft,source_id,quotes,revisions)
            if relevant and (changed or not was_stale):
                self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))
                count+=changed
        return count

    def _scrub_backups(self):
        """Clear expired and forgotten text inside every schema backup, keeping the backup.

        A backup is a rollback copy taken before an upgrade or a vault-id rename, so it is not
        deleted here: the expired bodies, older revisions and forgotten sources this database
        no longer holds are removed from it, then it is vacuumed so no free page keeps them.
        Works by table name only, so an older schema's backup is never migrated. A backup that
        can't be cleared (held open, damaged) is deleted only when it is at least
        BACKUP_MIN_KEEP_DAYS old; otherwise it is reported in ``backups_left``.
        """
        with self._lock:
            retained={row[0]:json.loads(row[1]) for row in self.db.execute("SELECT id,json_extract(payload,'$.retained') FROM sources WHERE json_extract(payload,'$.retained') IS NOT NULL")}
            redacted={(row[0],row[1]):row[2] for row in self.db.execute("SELECT id,revision,payload FROM source_revisions WHERE json_extract(payload,'$.redacted') IS NOT NULL")}
            forgotten={row[0][len('forgotten:'):] for row in self.db.execute("SELECT key FROM local_metadata WHERE key LIKE 'forgotten:%'")}
        report={'backups_cleared':0,'backups_removed':0,'backups_left':0}
        if not (retained or redacted or forgotten):
            return report
        now=self.now()
        for path in schema.backups(self.path):
            try:
                _clear_backup(path,retained,redacted,forgotten)
                report['backups_cleared']+=1
            except (sqlite3.Error,OSError):
                try:
                    taken=datetime.strptime(path.name.rsplit('-',1)[-1],'%Y%m%dT%H%M%S%fZ.db').replace(tzinfo=timezone.utc)
                    if (now-taken).days>=BACKUP_MIN_KEEP_DAYS:
                        path.unlink(missing_ok=True); report['backups_removed']+=1
                        continue
                except (ValueError,OSError):
                    pass
                report['backups_left']+=1
        # A backup left holding text is tried again on the next retention run, even one that
        # expires nothing new (retention also runs on open, hourly).
        with self._transaction():
            if report['backups_left']:
                self.db.execute("INSERT INTO local_metadata VALUES('backups-pending',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",(_json({'left':report['backups_left']}),))
            else:
                self.db.execute("DELETE FROM local_metadata WHERE key='backups-pending'")
        return report

    def forget(self, source_id):
        """Remove one source's text from this database: revisions, search, mappings, evidence
        links and the passages drafts quote, and the same from every schema backup.
        Obligations created from it keep their titles. Copies outside the database (JSONL
        exports, the generated wiki, knowledge records) are not touched. A later collection
        skips the source (KAN-390)."""
        if not isinstance(source_id,str) or not source_id:
            raise ValueError('Give the id of the source to forget')
        with self._transaction():
            if not (self.db.execute('SELECT 1 FROM sources WHERE id=?',(source_id,)).fetchone() or self.db.execute('SELECT 1 FROM source_revisions WHERE id=?',(source_id,)).fetchone()):
                raise KeyError(source_id)
            bodies=[json.loads(row[0]).get('body') for row in self.db.execute('SELECT payload FROM source_revisions WHERE id=?',(source_id,))]
            current=self.db.execute('SELECT payload FROM sources WHERE id=?',(source_id,)).fetchone()
            if current:
                bodies.append(json.loads(current[0]).get('body'))
            stale=sum(1 for draft in self._rows('drafts') if source_id in draft.get('source_revisions',{}) or any(e.get('id')==source_id for e in draft.get('evidence',[])))
            self._scrub_drafts(source_id,bodies)
            for table,column in (('sources','id'),('source_revisions','id'),('source_mappings','source_id'),('source_intents','source_id'),
                                 ('evidence','source_id'),('manual_evidence','source_id'),('source_projects','source_id'),('source_search','source_id')):
                self.db.execute(f'DELETE FROM {table} WHERE {column}=?',(source_id,))
            self.db.execute('INSERT INTO local_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',('forgotten:'+source_id,_json({'at':self.now().isoformat()})))
        wal_cleared=self._compact(optimize=True)
        return {'forgotten':source_id,'stale_drafts':stale,'wal_cleared':wal_cleared,**self._scrub_backups()}

    def _compact(self, optimize=False):
        """Drop removed text from the write-ahead log, and from the search index's segments
        when ``optimize``. Returns whether the log was emptied: another reader can hold it."""
        with self._lock:
            if optimize:
                self.db.execute("INSERT INTO source_search(source_search) VALUES('optimize')")
            self.db.commit()
            for attempt in range(5):
                busy=self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]
                if not busy:
                    return True
                time.sleep(0.05*(attempt+1))
            return False

    def rebuild_projections(self):
        """Explicit repair: rebuild the search index and project links. Recorded state is untouched."""
        with self._transaction():
            self._rebuild_all_projections()
            return {'sources':self.db.execute('SELECT count(*) FROM sources').fetchone()[0],'projection_version':PROJECTION_VERSION}

    def _rebuild_project_projections(self, project_id, rooms):
        """Re-derive only the sources a project's name, aliases or rooms can affect."""
        affected={row[0] for row in self.db.execute('SELECT source_id FROM source_projects WHERE project_id=?',(project_id,))}
        if rooms:
            marks=','.join('?'*len(rooms))
            affected|={row[0] for row in self.db.execute(f"SELECT id FROM sources WHERE json_extract(payload,'$.room_id') IN ({marks})",sorted(rooms))}
        for sid in sorted(affected):
            self._project_source(self._source(sid))
        # Mapping or name changes can affect context packets already reviewed.
        for draft in self._rows('drafts'):
            draft['stale']=True
            self.db.execute('UPDATE drafts SET payload=? WHERE id=?',(_json(draft),draft['id']))

    def update_project(self, id, revision, name=None, aliases=None, rooms=None):
        """Revision-checked local configuration only; never expand remote scopes."""
        if type(revision) is not int or revision<1:
            raise ValueError('A positive integer project revision is required')
        with self._transaction():
            current=self.project(id)
            config=self._project_config(id)
            if config['config_revision']!=revision:
                raise ConflictError('Project configuration changed; reload before editing')
            name=current['name'] if name is None else name
            aliases=config['aliases'] if aliases is None else aliases
            rooms=config['rooms'] if rooms is None else rooms
            if not isinstance(name,str) or not name.strip() or len(name)>300:
                raise ValueError('Project name must be nonempty text, at most 300 characters')
            if not isinstance(aliases,list) or not all(isinstance(alias,str) and alias.strip() and len(alias)<=200 for alias in aliases):
                raise ValueError('Aliases must be nonempty strings')
            if not isinstance(rooms,list) or any(not isinstance(room,dict) or set(room)-{'id','name'} or not isinstance(room.get('id'),str) or not room['id'] or not isinstance(room.get('name'),str) or not room['name'] for room in rooms):
                raise ValueError('Rooms require explicit id and name')
            if len({room['id'] for room in rooms})!=len(rooms):
                raise ValueError('Room IDs must be unique within a project')
            self.db.execute('UPDATE projects SET name=? WHERE id=?',(name,id))
            self.db.execute('INSERT OR REPLACE INTO project_config(project_id,aliases,rooms,revision) VALUES(?,?,?,?)',(id,_json(list(dict.fromkeys(aliases))),_json(rooms),revision+1))
            self._rebuild_project_projections(id,{room['id'] for room in rooms})  # sources of removed rooms are already linked
            return self.project(id)
