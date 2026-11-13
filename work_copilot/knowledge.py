"""Source envelope contract and export of captured sources to a knowledge repo inbox.

The envelope and the Source record are versioned local copies of the knowledge-record
contract that ai-dlc publishes as ``contracts/knowledge-record.schema.json``. Work
Copilot does not import ai-dlc; ``tests/test_knowledge.py`` checks exports against a
vendored copy of that schema.

Export writes only below ``<knowledge repo>/inbox/`` on the local disk. It never
pushes, sends or calls an external system, and it never edits or deletes a file it
did not just create. Per source it writes two new files:

* the raw payload (transcript, thread or marked note lines) to
  ``inbox/raw/sha256-<hex>.txt``, named by its sha256 so identical payloads are
  stored once.
* a Source record ``inbox/<id>--<envelope hash>.md``: metadata, the ``revision``,
  ``sources[{id, resource, hash}]`` pointing at the raw payload, and a short summary.

Inbox items are candidates for 30 days (``stale_after``). Retention, numbering and
promotion belong to the knowledge repository's own workflow.

Customer boundary (KAN-489, ai-dlc ADR 0007). The destination's ``knowledge.toml``
decides what it may receive, and anything unclear is left out:

* The destination must have a ``knowledge.toml``; a folder without one gets nothing.
* A source's projects are its own and those its room is bound to, compared in the
  shape of ai-dlc's slug, so ``Acme Corp`` and ``acme-corp`` are one project.
* A customer repository declares ``[repo] boundary = "customer"`` and ``project``. It
  receives only Sources whose projects are exactly that project, written with
  ``scope: project``, ``boundary: customer`` and ``project``.
* A team repository that lists ``[[transcripts.customers]]`` never receives a Source
  bound to a customer project, nor, once any customer is listed, one bound to no
  project: an unbound room may be a customer's. Marked note lines are the user's own
  words and go unless their note is bound to, or names, a customer project.

A Source left out is reported by its record id and a reason, never by its title.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
)

ENVELOPE_VERSION = 1
"""``schema_version`` of the source envelope (ai-dlc ``ENVELOPE_VERSION``)."""
RECORD_FORMAT_VERSION = 1
"""Knowledge record format this export writes (ai-dlc ``FORMAT_VERSION``)."""
CANDIDATE_WINDOW = timedelta(days=30)
"""How long an inbox item stays a candidate; written as ``stale_after``."""
OBLIGATION_STATUSES = ('candidate', 'open', 'waiting', 'resolved', 'dismissed')
SCOPES = ('personal', 'project', 'team')
# Synthesized from user input, not captured content.
_NOT_CAPTURED = {'local-user'}
_MARK = '>>'
_SUMMARY_LINES = 3
_SUMMARY_CHARS = 400
_DAY = re.compile(r'^(\d{4}-\d{2}-\d{2})')
_LINE_LABEL = re.compile(r'^L\d+: ')
_PROJECT = re.compile(r'^[a-z0-9][a-z0-9-]*$')  # ai-dlc's SLUG


def _instant(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError('must be an ISO timestamp with timezone')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('must be an ISO timestamp with timezone') from None
    if result.tzinfo is None:
        raise ValueError('must be an ISO timestamp with timezone')
    return result


class EnvelopeObligation(BaseModel):
    """The contract's obligation fields; Work Copilot's own extra fields are allowed."""

    model_config = ConfigDict(extra='allow')
    intent_key: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)


class SourceEnvelope(BaseModel):
    """Source envelope ``schema_version: 1``.

    The fields named here follow ai-dlc's ``SourceEnvelope``. Work Copilot's local
    extensions (``projects``, ``room_id``, ``deleted`` and so on) are allowed on
    ingest and never exported.
    """

    model_config = ConfigDict(extra='allow')
    schema_version: Literal[1] = ENVELOPE_VERSION
    id: StrictStr = Field(min_length=1)
    revision: StrictStr = Field(min_length=1)
    provider: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)
    body: StrictStr
    observed_at: StrictStr
    obligations: list[EnvelopeObligation] = Field(default_factory=list)
    url: StrictStr | None = None

    @field_validator('observed_at')
    @classmethod
    def _aware(cls, value: str) -> str:
        _instant(value)
        return value


def validate_envelope(item) -> None:
    """Raise ``ValueError`` naming every field of ``item`` that breaks the envelope."""
    if not isinstance(item, dict):
        raise ValueError('Source envelope must be an object')
    try:
        SourceEnvelope.model_validate(item)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            location = '.'.join(str(part) for part in error['loc']) or 'source'
            problems.append(f"{location}: {error['msg'].removeprefix('Value error, ')}")
        raise ValueError('Invalid source envelope: ' + '; '.join(problems)) from None


# --- Source records ---------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:32] or 'source'


def record_id(source_id: str, provider: str) -> str:
    """A stable draft slug for one source; the revision is a separate field."""
    return f'{_slug(provider)}-{_sha256(source_id.encode())[:16]}'


def contract_envelope(source: dict) -> dict:
    """The envelope fields a Source record carries, without ``revision``."""
    obligations = []
    for item in source.get('obligations') or []:
        entry = {'intent_key': item['intent_key'], 'title': item['title']}
        if item.get('status') in OBLIGATION_STATUSES:
            entry['status'] = item['status']
        obligations.append(entry)
    envelope = {'schema_version': ENVELOPE_VERSION, 'id': source['id'], 'provider': source['provider'],
                'title': source['title'], 'body': source['body'],
                'observed_at': _instant(source['observed_at']).isoformat(), 'obligations': obligations}
    url = source.get('url')
    if isinstance(url, str) and url.startswith(('https://', 'http://')):
        envelope['url'] = url
    return envelope


def envelope_hash(source: dict) -> str:
    """sha256 of the whole exported envelope except ``revision``; the de-duplication key."""
    canonical = json.dumps(contract_envelope(source), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return _sha256(canonical.encode('utf-8'))


def record_name(source: dict) -> str:
    """File name: stable id plus envelope hash, safe on Windows and macOS."""
    return f"{record_id(source['id'], source['provider'])}--{envelope_hash(source)[:16]}.md"


def summary(body: str) -> str:
    """A short extract of the first non-empty lines; no model is involved."""
    lines = [' '.join(line.split()) for line in body.splitlines()]
    text = '\n'.join([line for line in lines if line][:_SUMMARY_LINES])
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS - 1].rstrip() + '…'


def render_frontmatter(fields: dict) -> str:
    """YAML frontmatter with each value as a JSON scalar or flow collection.

    JSON is a subset of YAML 1.2 flow style, so any YAML reader parses it, and dates
    stay strings as the JSON Schema expects.
    """
    lines = [f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in fields.items()]
    return '---\n' + '\n'.join(lines) + '\n---\n'


def parse_frontmatter(text: str) -> dict | None:
    """Read frontmatter written by :func:`render_frontmatter`; ``None`` for anything else."""
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        return None
    data = {}
    for line in lines[1:]:
        if line == '---':
            return data
        key, sep, value = line.partition(': ')
        if not sep:
            return None
        try:
            data[key] = json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def project_ids(source: dict) -> list[str]:
    """The Work Copilot project ids a source is bound to, in order, without repeats."""
    projects = []
    for project in source.get('projects') or []:
        target = project.get('id') if isinstance(project, dict) else None
        if isinstance(target, str) and target and target not in projects:
            projects.append(target)
    return projects


def _key(project: str) -> str:
    """A project id in the shape of ai-dlc's slug: NFKC, casefolded, every run of
    other characters one ``-``. ``Acme Corp`` and ``acme-corp`` are one project, so a
    near match counts as the customer and fails closed."""
    folded = unicodedata.normalize('NFKC', project).casefold()
    return re.sub(r'[^a-z0-9]+', '-', folded).strip('-')


def room_projects(store) -> dict[str, list[str]]:
    """Project ids each configured room is bound to.

    The store reads a room binding only when a source carries no projects of its own,
    so export looks the binding up itself: a customer's room stays a customer's room
    whatever the payload or a manual mapping says.
    """
    rooms: dict[str, list[str]] = {}
    with store._lock:
        rows = store.db.execute('SELECT project_id, rooms FROM project_config').fetchall()
    for project, value in rows:
        for room in json.loads(value or '[]'):
            if isinstance(room, dict) and isinstance(room.get('id'), str):
                rooms.setdefault(room['id'], []).append(project)
    return rooms


def build_record(source: dict, *, owner: str, scope: str, day: str, raw_resource: str,
                 raw_hash: str, exported_at: datetime, extra_sources=(), customer: str | None = None) -> str:
    """Render one Source record (frontmatter and summary body) for a source."""
    envelope = contract_envelope(source)
    projects = project_ids(source)
    stale = (exported_at + CANDIDATE_WINDOW).astimezone(timezone.utc)
    excerpt = summary(envelope['body'])
    fields = {
        'id': record_id(envelope['id'], envelope['provider']),
        'type': 'source',
        'scope': 'project' if customer else scope,
        'status': 'raw',
        'date': day,
        'owner': owner,
        'sources': [{'id': envelope['id'], 'resource': raw_resource, 'hash': 'sha256:' + raw_hash},
                    *extra_sources],
        'links': [{'type': 'belongs_to', 'target': target} for target in projects],
        'title': envelope['title'],
        'stale_after': stale.isoformat().replace('+00:00', 'Z'),
        'schema_version': ENVELOPE_VERSION,
        'revision': source['revision'],
        'provider': envelope['provider'],
        'observed_at': envelope['observed_at'],
        'obligations': envelope['obligations'],
    }
    if customer:
        fields['boundary'] = 'customer'
        fields['project'] = customer
    if excerpt:
        fields['description'] = excerpt.splitlines()[0][:200]
    if 'url' in envelope:
        fields['url'] = envelope['url']
    body = [f"# {' '.join(envelope['title'].split())}", '']
    if excerpt:
        body += [excerpt, '']
    body += [f'Raw payload: `{raw_resource}`. Inbox candidate until {stale.date().isoformat()}.', '']
    return render_frontmatter(fields) + '\n'.join(body)


# --- Marked notes -----------------------------------------------------------


def marked_lines(body: str) -> list[str]:
    """Lines of a captured vault note that start with ``>>``, without the marker."""
    found = []
    for line in body.splitlines():
        text = _LINE_LABEL.sub('', line, count=1).lstrip(' \t')
        if text.startswith(_MARK):
            content = text[len(_MARK):].strip()
            if content:
                found.append(content)
    return found


def _note_day(source: dict, store) -> str:
    match = _DAY.match(Path(source.get('title', '')).name)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y-%m-%d').date().isoformat()
        except ValueError:
            pass
    return store._calendar_date(source['observed_at'])


def marked_note_envelopes(store, vault_sources) -> list[tuple[dict, list[dict]]]:
    """One envelope per day of marked note lines, with the notes it came from."""
    days: dict[str, list[tuple[dict, list[str]]]] = {}
    for source in sorted(vault_sources, key=lambda s: s.get('title', '')):
        lines = marked_lines(source.get('body', ''))
        if lines:
            days.setdefault(_note_day(source, store), []).append((source, lines))
    envelopes = []
    for day, notes in sorted(days.items()):
        body = '\n'.join(line for _source, lines in notes for line in lines) + '\n'
        observed = max((_instant(s['observed_at']) for s, _lines in notes)).isoformat()
        envelope = {'schema_version': ENVELOPE_VERSION, 'id': f'notes:{day}', 'provider': 'notes',
                    'title': f'Marked notes {day}', 'body': body, 'observed_at': observed,
                    'obligations': [], 'day': day}
        envelope['revision'] = 'sha256:' + envelope_hash(envelope)
        # No hash: an unchanged note may feed several revisions of one day's record.
        origins = [{'id': s['id'], 'resource': str(s['title']).replace('\\', '/')} for s, _lines in notes]
        envelopes.append((envelope, origins))
    return envelopes


# --- Files ------------------------------------------------------------------


def _directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f'{path.name} cannot be a symlink')
    path.mkdir(exist_ok=True)
    return path


def _write(path: Path, data: bytes) -> None:
    """Create a new file atomically with exact bytes; never replace an existing one."""
    if path.exists() or path.is_symlink():
        raise ValueError(f'Refusing to overwrite {path.name}')
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tmp-', suffix=path.suffix)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def knowledge_root(path) -> Path:
    from .vault import _root
    try:
        return _root(path)
    except ValueError as exc:
        raise ValueError(f'Knowledge repository path: {exc}') from None


def destination_rules(root: Path) -> dict:
    """What the knowledge repository at ``root`` may receive, from its ``knowledge.toml``.

    ``{'customer': <project>}`` for a declared customer repository, otherwise
    ``{'customer': None, 'customers': {<customer project>, ...}}``. A malformed file or
    declaration raises, so nothing is exported on a guess.
    """
    path = root / 'knowledge.toml'
    if path.is_symlink():
        raise ValueError('knowledge.toml cannot be a symlink')
    if not path.is_file():
        raise ValueError('--to must be the root of a knowledge repository, with a knowledge.toml file')
    try:
        parsed = tomllib.loads(path.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f'knowledge.toml is not valid TOML: {exc}') from None
    except OSError as exc:
        raise ValueError(f'knowledge.toml cannot be read: {exc.strerror or exc}') from None
    if 'repo' in parsed:
        repo = parsed['repo']
        project = repo.get('project') if isinstance(repo, dict) else None
        if not isinstance(repo, dict) or repo.get('boundary') != 'customer' or not (
                isinstance(project, str) and _PROJECT.fullmatch(project)):
            raise ValueError('knowledge.toml [repo] must declare boundary = "customer" and a project slug')
        return {'customer': project}
    transcripts = parsed.get('transcripts', {})
    if isinstance(transcripts, dict) and 'projects' in transcripts:
        raise ValueError('knowledge.toml [[transcripts.projects]] is retired; list customers in [[transcripts.customers]]')
    rules = transcripts.get('customers', []) if isinstance(transcripts, dict) else None
    if not isinstance(rules, list):
        raise ValueError('knowledge.toml [[transcripts.customers]] must be a list of tables')
    customers = set()
    for rule in rules:
        project = rule.get('project') if isinstance(rule, dict) else None
        if not isinstance(project, str) or not project.strip():
            raise ValueError('every [[transcripts.customers]] rule needs a project')
        customers.add(_key(project))
    return {'customer': None, 'customers': customers}


def left_out(projects: list[str], rules: dict, *, note: bool = False, named=()) -> str | None:
    """Why a source bound to ``projects`` may not go to this destination, or ``None``.

    ``named`` are projects a note mentions without being bound to them."""
    keys = {_key(project) for project in projects}
    customer = rules['customer']
    if customer:
        if keys == {customer}:
            return None
        return 'not bound to this customer project alone'
    customers = rules['customers']
    if customers & keys:
        return "bound to a customer project; export it to that customer's repository"
    if customers and not keys and not note:
        return 'bound to no project; bind its room to a project in Work Copilot'
    if customers & {_key(project) for project in named}:
        return 'names a customer project; bind the note to a project in Work Copilot'
    return None


def export_sources(store, destination, *, owner: str, since: str | None = None,
                   scope: str = 'personal', now: datetime | None = None) -> dict:
    """Write Source records for captured sources into ``<destination>/inbox/``.

    Idempotent and de-duplicated by :func:`envelope_hash`: an envelope already in the
    inbox, under any revision, is not written again. Existing files are never edited or
    deleted. Deleted (tombstoned) sources and unmarked note lines are never exported.
    """
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError('An owner is required (--owner or WORK_COPILOT_KNOWLEDGE_OWNER)')
    if scope not in SCOPES:
        raise ValueError('scope must be one of ' + ', '.join(SCOPES))
    cutoff = _instant(since) if since else None
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = knowledge_root(destination)
    rules = destination_rules(root)
    inbox = _directory(root / 'inbox')
    raw = _directory(inbox / 'raw')

    def recent(source):
        return cutoff is None or _instant(source['observed_at']) >= cutoff

    current = [s for s in store.list_sources() if not s.get('deleted') and recent(s)]
    rooms = room_projects(store)
    pending, skipped, notes = [], [], []
    for source in current:
        if source['provider'] in _NOT_CAPTURED:
            continue
        projects = project_ids(source) + rooms.get(source.get('room_id') or '', [])
        note = source['provider'] == 'vault'
        named = (source.get('suggested_projects') or []) if note else []
        reason = left_out(projects, rules, note=note, named=named)
        if reason:
            if not note or marked_lines(source.get('body', '')):
                skipped.append({'record': record_id(source['id'], source['provider']), 'reason': reason})
            continue
        if note:
            notes.append(source)
        else:
            pending.append((source, store._calendar_date(source['observed_at']), []))
    for envelope, origins in marked_note_envelopes(store, notes):
        pending.append((envelope, envelope['day'], origins))

    written, unchanged, raw_written = [], [], []
    for source, day, origins in pending:
        name = record_name(source)
        target = inbox / name
        if target.exists() or name in written:
            unchanged.append(name)
            continue
        payload = source['body'].encode('utf-8')
        digest = _sha256(payload)
        raw_name = f'sha256-{digest}.txt'
        raw_path = raw / raw_name
        if not raw_path.exists():
            _write(raw_path, payload)
            raw_written.append(raw_name)
        elif _sha256(raw_path.read_bytes()) != digest:
            raise ValueError(f'inbox/raw/{raw_name} does not match its sha256; not overwriting it')
        record = build_record(source, owner=owner.strip(), scope=scope, day=day,
                              raw_resource=f'inbox/raw/{raw_name}', raw_hash=digest,
                              exported_at=moment, extra_sources=origins, customer=rules['customer'])
        _write(target, record.encode('utf-8'))
        written.append(name)
    return {'inbox': str(inbox), 'customer': rules['customer'], 'written': written, 'unchanged': unchanged,
            'raw_written': raw_written, 'skipped': skipped, 'external_writes': False}
