"""Bounded local activity receipts, not client integration or source verification.

Nothing here reads client traces, scans repositories, or performs network calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

MAX_EVENT_BYTES = 64 * 1024
MAX_QUEUE_EVENTS = 1000


def _validated(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError('Activity event must be an object')
    if len(json.dumps(event, ensure_ascii=False).encode()) > MAX_EVENT_BYTES:
        raise ValueError('Activity event exceeds 64 KiB')
    for key in ('session_id', 'turn_id', 'event_id', 'summary'):
        if not isinstance(event.get(key), str) or not event[key].strip():
            raise ValueError(f'{key} is required')
        if len(event[key]) > (16000 if key == 'summary' else 200):
            raise ValueError(f'{key} is too long')
    for key in ('repo', 'project_id', 'procedure_id', 'procedure_version', 'step_id'):
        if event.get(key) is not None and (not isinstance(event[key], str) or not event[key].strip() or len(event[key]) > 1000):
            raise ValueError(f'{key} must be a nonempty literal string')
    outcomes = event.get('outcomes', [])
    if not isinstance(outcomes, list) or len(outcomes) > 30 or any(not isinstance(x, str) or len(x) > 2000 for x in outcomes):
        raise ValueError('outcomes must contain at most 30 claim strings')
    urls = event.get('evidence_urls', [])
    if not isinstance(urls, list) or len(urls) > 30:
        raise ValueError('evidence_urls must contain at most 30 URLs')
    for url in urls:
        if not isinstance(url, str) or len(url) > 4000 or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError('Evidence URL contains invalid characters')
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Evidence URLs must be credential-free HTTP(S) links')
    learning = event.get('learning')
    if learning is not None:
        if not isinstance(learning, dict) or learning.get('scope', 'session') not in {'session','project','repository'}:
            raise ValueError('learning scope must be session, project or repository')
        for key in ('correction', 'preferred_approach'):
            if not isinstance(learning.get(key), str) or not learning[key].strip() or len(learning[key]) > 4000:
                raise ValueError(f'learning.{key} is required (up to 4000 characters)')
        if any(not isinstance(v, str) or len(v) > 4000 for v in learning.values()):
            raise ValueError('Learning fields must be strings up to 4000 characters')
        learning = {key: learning[key] for key in ('scope','project_id','repo','correction','preferred_approach','conditions','session_ref','supersedes') if key in learning}
        learning['status'] = 'proposed'
    observed = event.get('observed_at') or datetime.now(timezone.utc).isoformat()
    try:
        timestamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('timezone required')
    except (ValueError, AttributeError):
        raise ValueError('observed_at must be an ISO timestamp with timezone') from None
    return {key: event.get(key) for key in ('session_id','turn_id','event_id','repo','project_id','summary','procedure_id','procedure_version','step_id')} | {
        'outcomes': outcomes, 'evidence_urls': urls, 'observed_at': timestamp.isoformat(), 'learning': learning}


def _guarded() -> bool:
    return os.environ.get('WORK_COPILOT_HOOK_ACTIVE', '').lower() in {'1','true','yes'}


def ingest_activity(store, event: dict, config: dict | None = None) -> dict:
    if _guarded():
        return {'skipped': 'recursion_guard'}
    event = _validated(event)
    config = config or {}
    if not isinstance(config, dict) or not isinstance(config.get('repo_projects', {}), dict):
        raise ValueError('Hook config and repo_projects must be objects')
    identity = json.dumps([event['session_id'], event['turn_id'], event['event_id']], separators=(',', ':'))
    source_id = 'local-activity:' + hashlib.sha256(identity.encode()).hexdigest()
    mapping = config.get('repo_projects', {}).get(event.get('repo'))
    if event.get('project_id'):
        mapping = {'id': event['project_id'], 'name': event['project_id']}
    elif isinstance(mapping, str):
        mapping = {'id': mapping, 'name': mapping}
    projects = []
    if mapping is not None:
        if not isinstance(mapping, dict) or not isinstance(mapping.get('id'), str) or not mapping['id'].strip():
            raise ValueError('Exact repo mapping needs a stable project id')
        projects = [{'id': mapping['id'], 'name': mapping.get('name', mapping['id']), 'workstream': mapping.get('workstream', 'engineering')}]
    authority = config.get('authority', {})
    if not isinstance(authority, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in authority.items()):
        raise ValueError('authority must map provider names to descriptive strings')
    lines = ['Local client activity receipt — unverified claims, not upstream state.', '', event['summary']]
    lines += ['', *(f'Claimed (not independently verified): {claim}' for claim in event['outcomes'])]
    if event['repo']:
        lines += ['', 'Repository label (not scanned or accessed): ' + event['repo']]
    lines += ['', *(f'Provided evidence link (not fetched): {url}' for url in event['evidence_urls'])]
    if authority:
        lines += ['', 'Configured authority roles (not validation): ' + json.dumps(authority, sort_keys=True)]
    procedure = {key:event[key] for key in ('procedure_id','procedure_version','step_id') if event.get(key)}
    if procedure:
        lines += ['', 'Procedure reference (not executed or advanced): ' + json.dumps(procedure, sort_keys=True)]
    obligations = [] if projects else [{'intent_key': f'map:{source_id}', 'title':'Review project mapping for local activity', 'status':'candidate', 'owner':None}]
    if event.get('learning'):
        lines += ['', 'Proposed correction learning — not an accepted rule:', json.dumps(event['learning'], ensure_ascii=False, sort_keys=True)]
        obligations.append({'intent_key':f'learning:{source_id}', 'title':'Review proposed learning: ' + event['learning']['correction'][:120], 'status':'candidate', 'owner':None, 'project_id':projects[0]['id'] if len(projects) == 1 else None})
    source = {'id':source_id, 'provider':'local-activity', 'title':event['summary'].strip().splitlines()[0][:160],
              'body':'\n'.join(lines), 'projects':projects, 'obligations':obligations,
              'url':event['evidence_urls'][0] if event['evidence_urls'] else None,
              'observed_at':event['observed_at'], 'verification':'claimed',
              'procedure':procedure, 'learning':event.get('learning'), 'activity': {k:event[k] for k in ('session_id','turn_id','event_id','repo')}, 'deleted':False}
    payload = {k:v for k,v in source.items() if k != 'observed_at'}
    source['revision'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result = store.ingest([source], connector='local-activity')
    # This is only a visible local suggestion to refresh; never executes a connector.
    # Repeat the local flag on replay too: a prior process may have committed
    # evidence and stopped before recording this separate non-network suggestion.
    store.request_refresh('Local activity claims need source verification', source_id=source_id)
    return {'source_id':source_id, 'revision':source['revision'], 'verification':'claimed',
            'project_ids':[p['id'] for p in projects], 'refresh_requested':True, 'result':result}


def _queue(data_dir) -> Path:
    root = Path(data_dir).expanduser().absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Activity data directory cannot contain symlinks')
    root.mkdir(parents=True, exist_ok=True)
    queue = root / 'activity-queue'
    if queue.is_symlink():
        raise ValueError('Activity queue cannot be a symlink')
    queue.mkdir(mode=0o700, exist_ok=True)
    return queue


def _atomic_json(path: Path, value: dict):
    temporary = path.parent / ('.pending-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def enqueue_activity(data_dir, event) -> dict:
    if _guarded():
        return {'skipped':'recursion_guard'}
    event = _validated(event)
    queue = _queue(data_dir)
    if len(list(queue.glob('*.event.json'))) >= MAX_QUEUE_EVENTS:
        raise ValueError('Activity queue is full (1000 events); drain or review errors first')
    path = queue / f'{time.time_ns():020d}-{uuid.uuid4().hex}.event.json'
    _atomic_json(path, {'version':1, 'event':event})
    return {'queued':True, 'queue_id':path.name, 'path':str(path)}


@contextmanager
def _queue_lock(queue: Path):
    """One drain process at a time; OS releases lock after process failure."""
    path = queue / '.drain.lock'
    if path.is_symlink():
        raise ValueError('Queue lock cannot be a symlink')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError('Another activity drain is active') from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == 'posix':
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def drain_activity(store, data_dir, config=None) -> dict:
    if _guarded():
        return {'skipped':'recursion_guard', 'processed':0, 'errors':[]}
    config = config or {}
    maximum = config.get('max_events', 100)
    if not isinstance(maximum, int) or not 1 <= maximum <= 1000:
        raise ValueError('max_events must be between 1 and 1000')
    queue = _queue(data_dir)
    try:
        with _queue_lock(queue):
            return _drain_locked(store, queue, config, maximum)
    except BlockingIOError:
        return {'processed':0, 'receipts':[], 'errors':[], 'busy':True, 'remaining':len(list(queue.glob('*.event.json')))}


def _drain_locked(store, queue, config, maximum):
    result = {'processed':0, 'receipts':[], 'errors':[]}
    for path in sorted(queue.glob('*.event.json'))[:maximum]:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_EVENT_BYTES + 2000:
                raise ValueError('Unsafe or oversized queued event')
            with path.open(encoding='utf-8') as handle:
                envelope = json.load(handle)
            if envelope.get('version') != 1:
                raise ValueError('Unsupported queued event version')
            receipt = ingest_activity(store, envelope['event'], config)
            result['receipts'].append(receipt)
            result['processed'] += 1
            path.unlink(missing_ok=True)
            path.with_suffix('.error.json').unlink(missing_ok=True)
        except Exception as exc:
            error = {'queue_id':path.name, 'error':str(exc), 'at':datetime.now(timezone.utc).isoformat()}
            result['errors'].append(error)
            _atomic_json(path.with_suffix('.error.json'), error)
            break  # FIFO: an invalid head remains visible; do not silently skip it.
    result['remaining'] = len(list(queue.glob('*.event.json')))
    return result
