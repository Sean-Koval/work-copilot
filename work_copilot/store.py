"""Transactional local evidence ledger. Input records are data, never instructions."""
from __future__ import annotations
import hashlib
import json
import re
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import sqlite3
import threading
from contextlib import contextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path
from filelock import FileLock


class ConflictError(ValueError):
    """The caller edited an obsolete revision or stale evidence."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


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

    def _initialize_database(self):
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE VIRTUAL TABLE IF NOT EXISTS source_search USING fts5(source_id UNINDEXED,title,body,project_terms);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision TEXT NOT NULL, payload TEXT NOT NULL, connector TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_revisions(id TEXT NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision));
        CREATE TABLE IF NOT EXISTS source_mappings(source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,projects TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS project_config(project_id TEXT PRIMARY KEY,aliases TEXT NOT NULL,rooms TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_projects(source_id TEXT NOT NULL,project_id TEXT NOT NULL,workstream TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_id,project_id,workstream));
        CREATE TABLE IF NOT EXISTS obligations(id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_intents(source_id TEXT NOT NULL,candidate_key TEXT NOT NULL,obligation_id TEXT NOT NULL,PRIMARY KEY(source_id,candidate_key));
        CREATE TABLE IF NOT EXISTS manual_evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id));
        CREATE TABLE IF NOT EXISTS evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id));
        CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS connectors(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS local_metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plans(day TEXT PRIMARY KEY,payload TEXT NOT NULL);
        ''')
        # Serialize schema inspection and projection rebuild across UI/MCP startup.
        self.db.execute('BEGIN IMMEDIATE')
        if 'revision' not in {row['name'] for row in self.db.execute('PRAGMA table_info(project_config)')}:
            self.db.execute('ALTER TABLE project_config ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
        # Rebuildable projection; migration also indexes pre-search databases.
        self.db.execute('DELETE FROM source_search')
        self._rebuild_project_projections(invalidate_drafts=False)
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
        clean = {k:v for k,v in ob.items() if k not in ('evidence','freshness')}
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
                if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('id','revision','provider','title')) or not isinstance(item.get('body'),str):
                    raise ValueError('Source requires string id, revision, provider, title and body')
                source=dict(item)
                source.setdefault('observed_at',self.now().isoformat())
                _instant(source['observed_at'])
                source.setdefault('projects',[])
                source.setdefault('obligations',[])
                source.setdefault('deleted',False)
                source.setdefault('url',None)
                if not isinstance(source['projects'],list) or not isinstance(source['obligations'],list):
                    raise ValueError('Projects and obligations must be lists')
                old=self._source(source['id'])
                prior=self.db.execute('SELECT payload FROM source_revisions WHERE id=? AND revision=?',(source['id'],source['revision'])).fetchone()
                if prior:
                    # Observation time may differ on replay; content cannot.
                    meaningful=lambda value: {k:v for k,v in value.items() if k!='observed_at'}
                    if meaningful(json.loads(prior[0])) != meaningful(source):
                        raise ValueError('Source revision collision: payload changed without a new revision')
                    # Seen historical revisions are replay, not newer evidence.
                    result['unchanged']+=1
                    continue
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
                        if not isinstance(candidate,dict) or not candidate.get('intent_key') or not candidate.get('title'):
                            raise ValueError('Obligations require intent_key and title')
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

    def snapshot(self):
        with self._transaction():
            now=self.now(); day=now.date().isoformat()
            obligations=[self._obligation(o['id']) for o in self._rows('obligations')]
            active=[o for o in obligations if o['status'] not in ('resolved','dismissed')]
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
            week={'start':monday.isoformat(),'end':week_end,'commitments':[o for o in this_week if o['commitment']],'suggestions':[o for o in this_week if not o['commitment']],'overdue':overdue,'overdue_count':len(overdue),'undated':undated,'backlog':backlog,'undated_count':len(undated),'backlog_count':len(backlog)}
            refresh_row=self.db.execute("SELECT payload FROM local_metadata WHERE key='refresh_needed'").fetchone()
            refresh_needed=json.loads(refresh_row[0]) if refresh_row else {'needed':False}
            return {'refresh_needed':refresh_needed,'timezone':self.timezone_name,'projects':[{**dict(p),**self._project_config(p['id'])} for p in self.db.execute('SELECT * FROM projects ORDER BY name')],'obligations':obligations,'drafts':self._drafts(),'connectors':self._rows('connectors'),'today':today,'week':week,'recent':sorted(self.list_sources(),key=lambda s:s['observed_at'],reverse=True)[:30],'review':[o for o in active if o['status']=='candidate' or not o.get('owner') or not o.get('project_id') or o.get('review_reason')],'coverage_note':'Only explicitly configured and observed sources are covered. Vendor semantics and upstream ACL enforcement are not guaranteed. Laptop sleep pauses capture.'}

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
                    excerpt=source['body'].strip().replace('\n',' ')[:220]
                    lines.append(f'Observed source [{index}] says: “{excerpt}”')
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
            self._rebuild_project_projections()
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

    def _rebuild_project_projections(self, invalidate_drafts=True):
        """Reconcile explicit room configuration without rewriting upstream evidence."""
        self.db.execute('DELETE FROM source_projects')
        for source in self.list_sources():
            if not source.get('deleted'):
                for project in source['projects']:
                    self.db.execute('INSERT OR IGNORE INTO source_projects VALUES(?,?,?)',(source['id'],project['id'],project.get('workstream','')))
            self._index_source(source)
        # Mapping or name changes can affect context packets already reviewed.
        if not invalidate_drafts:
            return
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
            self._rebuild_project_projections()
            return self.project(id)
