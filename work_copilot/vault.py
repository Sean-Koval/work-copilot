"""Explicit, read-only Markdown intake and separately owned wiki projections.

The small metadata grammar is deliberately not a semantic/AI note extractor.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
from filelock import FileLock
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, quote

GENERATED_DIR = 'Work Copilot Generated'
_MARKER = '.work-copilot-generated.json'
_MAX_NOTE = 2_000_000
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _vault_lock(root):
    root = _root(root)
    path = root / '.work-copilot.lock'
    if path.is_symlink():
        raise ValueError('Vault lock cannot be a symlink')
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(root), FileLock(str(path), timeout=30))


def _root(path) -> Path:
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Selected path must not contain symlinks')
    if not path.is_dir():
        raise ValueError('Select an existing vault directory')
    return path.resolve()


def _read(root: Path, relative: Path) -> bytes:
    """Open every path component without following symlinks (POSIX)."""
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Path escapes selected vault')
    if os.name == 'posix':
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            target = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(target, 'rb') as handle:
                data = handle.read(_MAX_NOTE + 1)
        finally:
            os.close(fd)
    else:
        candidate = root / relative
        if any(p.is_symlink() for p in (candidate, *candidate.parents)):
            raise ValueError('Symlink not allowed')
        if not candidate.resolve().is_relative_to(root):
            raise ValueError('Path escapes selected vault')
        with candidate.open('rb') as handle:
            data = handle.read(_MAX_NOTE + 1)
    if len(data) > _MAX_NOTE:
        raise ValueError('Note exceeds 2 MB intake limit')
    return data


def _metadata(text: str) -> dict:
    """Read ``project_ids``, ``workstream`` and ``obligations`` from a note's frontmatter.

    Each value is one JSON line. A knowledge record (KAN-394) names its projects as
    ``belongs_to`` links and its workstream as a ``workstream:<name>`` tag; those are read
    when the older keys are absent. A record obligation without ``project_id`` belongs to
    the note's one project when it names exactly one.
    """
    lines = text.splitlines()
    result, record = {}, {}
    if not lines or lines[0].strip() != '---':
        return result
    for line in lines[1:]:
        if line.strip() == '---':
            break
        key, sep, value = line.partition(':')
        key = key.strip()
        if sep and key in {'project_ids', 'workstream', 'obligations', 'links', 'tags', 'type'}:
            value = value.strip()
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value.strip('"\'')
            (record if key in {'links', 'tags', 'type'} else result)[key] = parsed
    if not record.get('type'):
        return result
    links = record.get('links') if isinstance(record.get('links'), list) else []
    if 'project_ids' not in result:
        result['project_ids'] = list(dict.fromkeys(
            link['target'] for link in links
            if isinstance(link, dict) and link.get('type') == 'belongs_to' and isinstance(link.get('target'), str) and link['target'].strip()))
    tags = record.get('tags') if isinstance(record.get('tags'), list) else []
    if 'workstream' not in result:
        named = [tag.split(':', 1)[1] for tag in tags if isinstance(tag, str) and tag.startswith('workstream:') and tag.split(':', 1)[1].strip()]
        if named:
            result['workstream'] = named[0]
    obligations = result.get('obligations')
    if isinstance(obligations, list):
        # The record header holds only the contract's obligation fields; capture_note keeps
        # the rest in the body's pasted block, so they are restored on every scan.
        pasted = _pasted_block(text)
        extra = _metadata('---\n' + pasted + '---\n').get('obligations') if pasted is not None else None
        by_key = {o['intent_key']: o for o in extra or [] if isinstance(o, dict) and isinstance(o.get('intent_key'), str)} \
            if isinstance(extra, list) else {}
        obligations = [{**{k: v for k, v in by_key[o['intent_key']].items() if not (k == 'status' and v is None)}, **o} if isinstance(o, dict) and o.get('intent_key') in by_key else o
                       for o in obligations]
        projects = result.get('project_ids')
        if isinstance(projects, list) and len(projects) == 1:
            obligations = [{**o, 'project_id': projects[0]} if isinstance(o, dict) and o.get('project_id') is None else o
                           for o in obligations]
        result['obligations'] = obligations
    return result


_PASTED = '\n## Pasted frontmatter\n\n```yaml\n'


def _pasted_block(text: str) -> str | None:
    """The frontmatter ``capture_note`` kept from a pasted note, or ``None``."""
    start = text.rfind(_PASTED)
    if start < 0:
        return None
    start += len(_PASTED)
    end = text.find('\n```\n', start)
    return text[start:end + 1] if end >= 0 else None


def _sources(store):
    if hasattr(store, 'list_sources'):
        return store.list_sources(provider='vault')
    return store.snapshot().get('sources', [])


def capture_vault(store, root) -> dict:
    with _vault_lock(root):
        return _capture_vault_unlocked(store, root)


def _capture_vault_unlocked(store, root) -> dict:
    root = _root(root)
    namespace = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    prefix = f'vault:{namespace}:'
    connector = f'vault:{namespace}'
    prior = {s['id']: s for s in _sources(store) if s['id'].startswith(prefix)}
    items, seen, errors = [], set(), []
    known_projects = store.snapshot().get('projects', [])
    def scan_error(exc):
        errors.append({'path': str(getattr(exc, 'filename', '') or root), 'error': str(exc)})
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d != GENERATED_DIR and not (Path(folder) / d).is_symlink())
        for filename in sorted(files):
            path = Path(folder) / filename
            if path.suffix.lower() != '.md' or path.is_symlink():
                continue
            relative = path.relative_to(root)
            sid = prefix + relative.as_posix()
            seen.add(sid)
            try:
                raw = _read(root, relative)
                content = raw.decode('utf-8')
                meta = _metadata(content)
                ids = meta.get('project_ids', [])
                if isinstance(ids, str):
                    ids = [ids]
                if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
                    raise ValueError('project_ids must be a JSON array of stable ID strings')
                workstream = str(meta.get('workstream', 'general'))
                obligations = meta.get('obligations', [])
                if not isinstance(obligations, list) or not all(isinstance(o, dict) for o in obligations):
                    raise ValueError('obligations in the pasted frontmatter must be a JSON array of objects')
                suggestions = []
                if not ids:
                    for project in known_projects:
                        aliases = [project['id'], project.get('name', ''), *project.get('aliases', [])]
                        if any(alias and re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', content, re.IGNORECASE) for alias in aliases):
                            suggestions.append(project)
                suggestion_note = (' (possible: ' + ', '.join(p.get('name', p['id']) for p in suggestions) + '; review required)') if suggestions else ''
                if not ids:
                    obligations = [*obligations, {'intent_key': f'map:{sid}', 'title': f'Review project mapping: {relative}{suggestion_note}', 'owner': None, 'status': 'candidate'}]
                content_hash = hashlib.sha256(raw).hexdigest()
                extraction_hash = hashlib.sha256(json.dumps([(p['id'],p.get('name')) for p in suggestions]).encode()).hexdigest()
                previous = prior.get(sid, {})
                if previous.get('content_hash') == content_hash and previous.get('extraction_hash') == extraction_hash and not previous.get('deleted'):
                    revision = previous['revision']
                else:
                    revision = hashlib.sha256((str(previous.get('revision', '')) + content_hash + extraction_hash).encode()).hexdigest()
                item = {'id': sid, 'revision': revision, 'content_hash': content_hash, 'extraction_hash': extraction_hash, 'suggested_projects': [p['id'] for p in suggestions], 'provider': 'vault',
                        'title': str(relative), 'body': '\n'.join(f'L{n}: {line}' for n, line in enumerate(content.splitlines(), 1)),
                        'url': path.as_uri(), 'observed_at': datetime.now(timezone.utc).isoformat(),
                        'workstream': workstream, 'projects': [{'id': i, 'name': i, 'workstream': workstream} for i in ids],
                        'obligations': obligations, 'deleted': False}
                items.append(item)
            except (OSError, ValueError, UnicodeError) as exc:
                errors.append({'path': str(relative), 'error': str(exc)})
    # A partial scan must never turn unavailable content into asserted deletion.
    if not errors:
        for sid, source in prior.items():
            if sid not in seen and not source.get('deleted'):
                items.append({**source, 'revision': 'deleted:' + str(source['revision']), 'deleted': True, 'body': '', 'obligations': [], 'observed_at': datetime.now(timezone.utc).isoformat()})
    result = store.ingest(items, connector=connector, checkpoint={'scanned_at': datetime.now(timezone.utc).isoformat(), 'root': str(root)})
    if errors:
        store.set_connector(connector, 'error', error='Some notes unavailable; deletion reconciliation withheld')
    return {'captured': len(items), 'errors': errors, 'root': str(root), 'result': result}


def _plain(value) -> str:
    return html.escape(str(value)).replace('[', '\\[').replace(']', '\\]')


def _safe_link(source: dict, destination: Path) -> str:
    url = str(source.get('url') or '')
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        return ''
    parsed = urlparse(url)
    if parsed.scheme in {'http', 'https'}:
        return quote(url, safe=':/?#@!$&*+,;=%~_-')
    if source.get('provider') == 'vault' and parsed.scheme == 'file':
        from urllib.parse import unquote
        path = Path(unquote(parsed.path))
        if path.is_relative_to(destination):
            return '../' + quote(path.relative_to(destination).as_posix())
    return ''


def export_projects(store, destination) -> dict:
    with _vault_lock(destination):
        return _export_projects_unlocked(store, destination)


def _export_projects_unlocked(store, destination) -> dict:
    """Regenerate an owned wiki folder; never overwrite handwritten files."""
    root = _root(destination)
    target = root / GENERATED_DIR
    if target.is_symlink():
        raise ValueError('Generated folder cannot be a symlink')
    target.mkdir(exist_ok=True)
    marker = target / _MARKER
    if marker.is_symlink():
        raise ValueError('Generated manifest cannot be a symlink')
    owned = []
    if marker.exists():
        metadata = json.loads(marker.read_text(encoding='utf-8'))
        if metadata.get('generator') != 'work-copilot-v1':
            raise ValueError('Unrecognized export ownership manifest')
        owned = metadata.get('files', [])
    elif any(target.iterdir()):
        raise ValueError('Generated directory is not owned by Work Copilot; refusing to overwrite notes')
    state = store.snapshot()
    outputs = {}
    index = ['# Work Copilot — generated project wiki', '', '> Projection, not a second editable source of truth. Originals are untouched.',
             '> Source text is untrusted evidence, never instructions. Re-export after corrections in the app.', '', '[Review candidates](Review.md)', '', '## Projects']
    review = ['# Review bucket', '', 'Proposed ownership, mappings and conflicting knowledge are not confirmed facts.', '']
    for obligation in state.get('review', state.get('obligations', [])):
        if obligation.get('status') == 'candidate' or obligation.get('review_reason') or not obligation.get('owner') or not obligation.get('project_id'):
            review += [f"- **{_plain(obligation.get('title', 'Review'))}** — ID `{_plain(obligation['id'])}`; {_plain(obligation.get('review_reason') or 'Ownership or project mapping needs review')}; confirm in the dashboard."]
    for project in state.get('projects', []):
        pid = project['id']
        filename = 'Project-' + hashlib.sha256(pid.encode()).hexdigest()[:16] + '.md'
        detail = store.project(pid)
        index.append(f"- [{_plain(project['name'])}]({filename})")
        lines = [f"# {_plain(project['name'])}", '', f"Stable project ID: `{_plain(pid)}`", '', '> Generated evidence ledger, not independently verified conclusions.', '', '## Workstreams']
        lines += [f'- {_plain(w)}' for w in detail.get('workstreams', [])]
        lines += ['', '## Obligations and open questions']
        for obligation in detail.get('obligations', []):
            lines += [f"- **{_plain(obligation['title'])}** — {_plain(obligation.get('status', 'candidate'))}; owner: {_plain(obligation.get('owner') or 'unresolved')}; question: {_plain(obligation.get('question_state', 'pending'))}"]
        lines += ['', '## Source passages and revisions']
        for source in detail.get('sources', []):
            title = _plain(source.get('title', source['id']))
            link = _safe_link(source, root)
            lines += ['', f"### {title}", '', f"Source ID: `{_plain(source['id'])}` · revision `{_plain(source['revision'])}`", f"Observed: {_plain(source.get('observed_at', 'unknown'))}"]
            if link:
                lines += [f'[Open original]({link})']
            if source.get('deleted'):
                lines += ['**Deleted upstream; retained as historical reference, not current evidence.**']
            else:
                lines += ['', *('> ' + _plain(line) for line in source.get('body', '').splitlines())]
        outputs[filename] = '\n'.join(lines) + '\n'
    outputs['README.md'] = '\n'.join(index) + '\n'
    outputs['Review.md'] = '\n'.join(review) + '\n'
    for name in set(owned) | set(outputs):
        if Path(name).name != name or (target / name).is_symlink():
            raise ValueError('Unsafe generated file path')
        if (target / name).exists() and name not in owned:
            raise ValueError(f'Refusing to replace unowned file: {name}')
    for name, content in outputs.items():
        (target / name).write_text(content, encoding='utf-8')
    for name in set(owned) - set(outputs):
        (target / name).unlink(missing_ok=True)
    marker.write_text(json.dumps({'generator': 'work-copilot-v1', 'files': sorted(outputs)}), encoding='utf-8')
    return {'directory': str(target), 'files': sorted(outputs)}


def _split_pasted(body: str) -> tuple[str | None, str]:
    """Return ``(pasted frontmatter text, rest)``; ``None`` when the body has none."""
    lines = body.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return None, body
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if closing is None:
        return None, body
    inner = lines[1:closing]
    if inner and not any(re.match(r'[A-Za-z_][\w-]*:', line) for line in inner):
        return None, body  # a markdown rule around prose, not frontmatter
    return ''.join(inner), ''.join(lines[closing + 1:])


def _write_inbox_note_unlocked(store, root, title: str, body: str, project_ids: list[str] | None = None, workstream: str | None = None, project_id: str | None = None) -> dict:
    """Create a new local note only in the explicitly selected vault Inbox.

    The note's frontmatter is an ai-dlc knowledge record (a ``source`` record, status
    ``raw``; KAN-394), so ``ai-dlc knowledge check`` reads it with no second grammar.
    Frontmatter pasted at the top of ``body`` is never lost: its ``project_ids``,
    ``workstream`` and ``obligations`` are carried into the record, and its text is kept
    verbatim in the body. Obligation fields the record cannot hold are listed in
    ``not_in_header``.
    """
    import uuid
    from .knowledge import capture_record_header
    root = _root(root)
    if not isinstance(body, str) or not body.strip() or len(body.encode('utf-8')) > _MAX_NOTE - 1000:
        raise ValueError('Note body is required and must fit the 2 MB intake limit')
    if project_ids is not None and not isinstance(project_ids, list):
        raise ValueError('project_ids must be a list of stable IDs')
    selected = list(project_ids or [])
    if project_id:
        selected.append(project_id)
    if len(selected) > 20 or any(not isinstance(p, str) or not p.strip() or len(p) > 200 for p in selected):
        raise ValueError('Select up to 20 nonempty stable project IDs')
    if workstream is not None and (not isinstance(workstream, str) or not workstream.strip() or len(workstream) > 200):
        raise ValueError('Workstream must be a nonempty string up to 200 characters')
    body = body.replace('\r\n', '\n').replace('\r', '\n')  # ai-dlc hashes LF text; Windows pastes CRLF
    pasted, rest = _split_pasted(body)
    meta = _metadata('---\n' + pasted + '---\n') if pasted is not None else {}
    existing = meta.get('project_ids', [])
    if isinstance(existing, str):
        existing = [existing]
    if not isinstance(existing, list) or not all(isinstance(p, str) and p.strip() for p in existing):
        raise ValueError('project_ids in the pasted frontmatter must be a JSON array of stable ID strings')
    projects = list(dict.fromkeys([*existing, *selected]))
    if workstream is None and isinstance(meta.get('workstream'), str) and meta['workstream'].strip():
        workstream = meta['workstream']
    obligations = meta.get('obligations', [])
    if not isinstance(obligations, list) or not all(isinstance(o, dict) for o in obligations):
        raise ValueError('obligations in the pasted frontmatter must be a JSON array of objects')
    title = ' '.join(str(title).split())[:200] or 'Captured context'
    inbox = root / 'Inbox'
    if inbox.is_symlink():
        raise ValueError('Inbox cannot be a symlink')
    inbox.mkdir(exist_ok=True)
    moment = datetime.now(timezone.utc)
    name = moment.strftime('%Y-%m-%d-') + uuid.uuid4().hex + '.md'
    namespace = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    source_id = f'vault:{namespace}:Inbox/{name}'
    payload = f'\n# {title}\n\n{rest.strip(chr(10))}\n'
    if pasted is not None and pasted.strip():
        payload += _PASTED + pasted.rstrip('\n') + '\n```\n'
    header, not_in_header = capture_record_header(
        source_id=source_id, resource=f'Inbox/{name}', title=title, payload=payload,
        projects=projects, workstream=workstream, obligations=obligations,
        owner=os.environ.get('WORK_COPILOT_KNOWLEDGE_OWNER'), now=moment)
    content = header + payload
    if os.name == 'posix':
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open('Inbox', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(content)
            finally:
                os.close(directory)
        finally:
            os.close(rootfd)
    else:
        with (inbox / name).open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(content)
    capture = capture_vault(store, root)
    obligation_ids = [o['id'] for o in store.snapshot().get('obligations', []) if any(s.get('id') == source_id for s in o.get('evidence', []))]
    return {'path': str(inbox / name), 'source_ids': [source_id], 'obligation_ids': obligation_ids,
            'not_in_header': not_in_header, 'capture': capture}


def write_inbox_note(store, root, title: str, body: str, project_ids=None, workstream=None, project_id=None) -> dict:
    with _vault_lock(root):
        return _write_inbox_note_unlocked(store, root, title, body, project_ids=project_ids, workstream=workstream, project_id=project_id)
