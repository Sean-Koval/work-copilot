"""Source envelope contract and export of captured sources to a knowledge repo inbox.

The envelope and the Source record are versioned local copies of the knowledge-record
contract that ai-dlc publishes as ``contracts/knowledge-record.schema.json``. Work
Copilot does not import ai-dlc; ``tests/test_knowledge.py`` checks exports against a
vendored copy of that schema.

Export writes only below ``<knowledge repo>/inbox/`` on the local disk. It never
pushes, sends or calls an external system. Per source it writes two files:

* the raw payload (transcript, thread or marked note lines) to
  ``inbox/raw/sha256-<hex>.txt``, named by its sha256 so identical payloads are
  stored once. Raw payloads expire after :data:`RAW_RETENTION`.
* a permanent Source record ``inbox/<id>--<revision>.md``: metadata,
  ``sources[{id, resource, hash}]`` pointing at the raw payload, and a short summary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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
RAW_RETENTION = timedelta(days=30)
"""Raw payloads expire this long after export; the Source record stays."""
OBLIGATION_STATUSES = ('candidate', 'open', 'waiting', 'resolved', 'dismissed')
SCOPES = ('personal', 'project', 'team')
# Synthesized from user input, not captured content.
_NOT_CAPTURED = {'local-user'}
_MARK = '>>'
_SUMMARY_LINES = 3
_SUMMARY_CHARS = 400
_RAW_NAME = re.compile(r'^sha256-[0-9a-f]{64}\.txt$')
_DAY = re.compile(r'^(\d{4}-\d{2}-\d{2})')
_LINE_LABEL = re.compile(r'^L\d+: ')


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


def record_id(source_id: str, provider: str, revision: str) -> str:
    """Draft slug from source id and revision; record ids are unique per revision."""
    return f'{_slug(provider)}-{_sha256(source_id.encode())[:16]}--{_sha256(revision.encode())[:16]}'


def record_name(source_id: str, provider: str, revision: str) -> str:
    """File name: the record id, safe on Windows and macOS."""
    return record_id(source_id, provider, revision) + '.md'


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


def build_record(envelope: dict, *, owner: str, scope: str, day: str, raw_resource: str,
                 raw_hash: str, exported_at: datetime, extra_sources=()) -> str:
    """Render one Source record (frontmatter and summary body) for an envelope."""
    observed = _instant(envelope['observed_at']).isoformat()
    obligations = []
    for item in envelope.get('obligations') or []:
        entry = {'intent_key': item['intent_key'], 'title': item['title']}
        if item.get('status') in OBLIGATION_STATUSES:
            entry['status'] = item['status']
        obligations.append(entry)
    projects = []
    for project in envelope.get('projects') or []:
        target = project.get('id') if isinstance(project, dict) else None
        if isinstance(target, str) and target and target not in projects:
            projects.append(target)
    expires = (exported_at + RAW_RETENTION).astimezone(timezone.utc)
    excerpt = summary(envelope['body'])
    fields = {
        'id': record_id(envelope['id'], envelope['provider'], envelope['revision']),
        'type': 'source',
        'scope': scope,
        'status': 'raw',
        'date': day,
        'owner': owner,
        'sources': [{'id': envelope['id'], 'resource': raw_resource, 'hash': 'sha256:' + raw_hash},
                    *extra_sources],
        'links': [{'type': 'belongs_to', 'target': target} for target in projects],
        'title': envelope['title'],
        'stale_after': expires.isoformat().replace('+00:00', 'Z'),
        'schema_version': ENVELOPE_VERSION,
        'revision': envelope['revision'],
        'provider': envelope['provider'],
        'observed_at': observed,
        'obligations': obligations,
    }
    if excerpt:
        fields['description'] = excerpt.splitlines()[0][:200]
    url = envelope.get('url')
    if isinstance(url, str) and url.startswith(('https://', 'http://')):
        fields['url'] = url
    body = [f"# {' '.join(envelope['title'].split())}", '']
    if excerpt:
        body += [excerpt, '']
    body += [f'Raw payload: `{raw_resource}` until {expires.date().isoformat()}; this record stays.', '']
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
        envelope = {'schema_version': ENVELOPE_VERSION, 'id': f'notes:{day}',
                    'revision': 'sha256:' + _sha256(body.encode()), 'provider': 'notes',
                    'title': f'Marked notes {day}', 'body': body, 'observed_at': observed,
                    'obligations': [], 'day': day}
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
    """Atomic write of exact bytes (no newline translation on Windows)."""
    if path.is_symlink():
        raise ValueError('Refusing to write through a symlink')
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


def _source_records(inbox: Path):
    for path in sorted(inbox.glob('*.md')):
        if path.is_symlink():
            continue
        text = path.read_bytes().decode('utf-8')
        front = parse_frontmatter(text)
        if front is not None and front.get('type') == 'source':
            yield path, text, front


def _exported(inbox: Path) -> dict:
    """``(title, payload hash)`` of the Source records already in the inbox."""
    seen = {}
    for path, _text, front in _source_records(inbox):
        entries = front.get('sources') or []
        first = entries[0] if entries and isinstance(entries[0], dict) else {}
        if isinstance(front.get('title'), str) and isinstance(first.get('hash'), str):
            seen.setdefault((' '.join(front['title'].split()), first['hash']), path.name)
    return seen


def expire_inbox(root, now: datetime) -> dict:
    """Mark Source records past ``stale_after`` expired and delete raw payloads only they use."""
    inbox = Path(root) / 'inbox'
    raw = inbox / 'raw'
    if not inbox.is_dir():
        return {'expired': [], 'removed_raw': []}
    records = list(_source_records(inbox))
    expired, keep, release = [], set(), set()
    for path, text, front in records:
        resources = {s.get('resource') for s in front.get('sources', []) if isinstance(s, dict)}
        due = False
        if front.get('status') == 'raw' and isinstance(front.get('stale_after'), str):
            try:
                due = _instant(front['stale_after']) <= now
            except ValueError:
                due = False
        if due:
            old = 'status: "raw"\n'
            _write(path, text.replace(old, 'status: "expired"\n', 1).encode('utf-8'))
            expired.append(path.name)
            release |= resources
        elif front.get('status') != 'expired':
            keep |= resources
    removed = []
    for resource in sorted(release - keep):
        name = Path(str(resource)).name
        if str(resource) == f'inbox/raw/{name}' and _RAW_NAME.match(name):
            target = raw / name
            if target.is_file() and not target.is_symlink():
                target.unlink()
                removed.append(name)
    return {'expired': expired, 'removed_raw': removed}


def export_sources(store, destination, *, owner: str, since: str | None = None,
                   scope: str = 'personal', now: datetime | None = None) -> dict:
    """Write Source records for captured sources into ``<destination>/inbox/``.

    Idempotent: a record whose id and revision already exist is not rewritten, and a
    raw payload already present under its sha256 name is not written again. Deleted
    (tombstoned) sources and unmarked note lines are never exported.
    """
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError('An owner is required (--owner or WORK_COPILOT_KNOWLEDGE_OWNER)')
    if scope not in SCOPES:
        raise ValueError('scope must be one of ' + ', '.join(SCOPES))
    cutoff = _instant(since) if since else None
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = knowledge_root(destination)
    inbox = _directory(root / 'inbox')
    raw = _directory(inbox / 'raw')
    expiry = expire_inbox(root, moment)

    def recent(source):
        return cutoff is None or _instant(source['observed_at']) >= cutoff

    current = [s for s in store.list_sources() if not s.get('deleted') and recent(s)]
    pending = []
    for source in current:
        if source['provider'] in _NOT_CAPTURED or source['provider'] == 'vault':
            continue
        pending.append((source, store._calendar_date(source['observed_at']), []))
    for envelope, origins in marked_note_envelopes(store, [s for s in current if s['provider'] == 'vault']):
        pending.append((envelope, envelope['day'], origins))

    seen = _exported(inbox)
    written, unchanged, duplicates, raw_written = [], [], [], []
    for source, day, origins in pending:
        name = record_name(source['id'], source['provider'], source['revision'])
        target = inbox / name
        if target.exists():
            unchanged.append(name)
            continue
        payload = source['body'].encode('utf-8')
        digest = _sha256(payload)
        # sha256 de-duplication: same title and payload is the same Source.
        key = (' '.join(source['title'].split()), 'sha256:' + digest)
        if key in seen:
            duplicates.append({'record': name, 'duplicate_of': seen[key]})
            continue
        seen[key] = name
        raw_name = f'sha256-{digest}.txt'
        raw_path = raw / raw_name
        if not raw_path.exists() or _sha256(raw_path.read_bytes()) != digest:
            _write(raw_path, payload)
            raw_written.append(raw_name)
        record = build_record(source, owner=owner.strip(), scope=scope, day=day,
                              raw_resource=f'inbox/raw/{raw_name}', raw_hash=digest,
                              exported_at=moment, extra_sources=origins)
        _write(target, record.encode('utf-8'))
        written.append(name)
    return {'inbox': str(inbox), 'written': written, 'unchanged': unchanged, 'duplicates': duplicates,
            'raw_written': raw_written,
            **expiry, 'external_writes': False}
