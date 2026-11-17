"""Explicit Markdown intake and separately owned wiki projections.

Capture never rewrites a hand-written note. It writes one id file at the vault root
(KAN-391) and, when renaming old ids, the id lines in headers of its own Inbox notes.

The small metadata grammar is deliberately not a semantic/AI note extractor.
"""
from __future__ import annotations

import errno
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
# Names the vault, so its note ids survive moving or re-syncing the folder (KAN-391).
VAULT_ID_FILE = '.work-copilot-vault.json'
_VAULT_ID = re.compile(r'[0-9a-f]{32}')
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
    lines = text.splitlines()
    result = {}
    if not lines or lines[0].strip() != '---':
        return result
    for line in lines[1:]:
        if line.strip() == '---':
            break
        key, sep, value = line.partition(':')
        if sep and key.strip() in {'project_ids', 'workstream', 'obligations'}:
            value = value.strip()
            try:
                result[key.strip()] = json.loads(value)
            except json.JSONDecodeError:
                result[key.strip()] = value.strip('"\'')
    return result


def _legacy_namespace(root: Path) -> str:
    """Before KAN-391, note ids were namespaced by a hash of the vault's absolute path."""
    return hashlib.sha256(str(root).encode()).hexdigest()[:20]


def _read_vault_id(root: Path):
    path = root / VAULT_ID_FILE
    if path.is_symlink():
        raise ValueError(f'{VAULT_ID_FILE} in the vault cannot be a symlink')
    if not path.exists():
        return None
    try:
        value = json.loads(_read(root, Path(VAULT_ID_FILE)).decode('utf-8')).get('vault_id')
    except (ValueError, AttributeError, UnicodeError) as exc:
        raise ValueError(f'{VAULT_ID_FILE} in the vault is damaged') from exc
    if not isinstance(value, str) or not _VAULT_ID.fullmatch(value):
        raise ValueError(f'{VAULT_ID_FILE} in the vault holds no valid vault id')
    return value


def _write_vault_id(root: Path) -> str:
    """Create the id file whole or not at all: write a temporary file, then link it into place.

    Linking fails if another process created the file first (FileExistsError), so two first
    captures can never leave a half-written or second id.
    """
    import uuid
    value = uuid.uuid4().hex
    content = (json.dumps({'vault_id': value, 'note': 'Work Copilot names this folder by this id. Keep it when moving or syncing the folder.'}, indent=2) + '\n').encode('utf-8')
    temporary = root / f'.{VAULT_ID_FILE}.{uuid.uuid4().hex}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == 'posix':
            os.link(temporary, root / VAULT_ID_FILE)
        else:
            os.rename(temporary, root / VAULT_ID_FILE)  # Windows: fails if the file exists
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return value


# A folder Work Copilot may not write to: keep capturing it under the old path-hash namespace.
_READ_ONLY = {errno.EACCES, errno.EPERM, errno.EROFS}


def _vault_identity(root: Path):
    """(namespace, vault_id, note) for a resolved vault root, writing the id file on first use."""
    vault_id = _read_vault_id(root)
    if vault_id is None:
        try:
            vault_id = _write_vault_id(root)
        except FileExistsError:
            vault_id = _read_vault_id(root)
        except OSError as exc:
            if exc.errno not in _READ_ONLY:
                raise
            return _legacy_namespace(root), None, f'Could not write {VAULT_ID_FILE} in the vault ({exc.strerror or exc}); note ids still depend on the folder path.'
    return vault_id, vault_id, None


def _vault_namespace(root: Path) -> str:
    """The vault's id in source ids (``vault:<namespace>:<path>``): the one place it is made,
    so a scan and a captured note's record header always agree. ``root`` must be resolved."""
    return _vault_identity(root)[0]


def _old_root(path) -> Path:
    """Where a vault was before it moved, as the path-hash namespace saw it (it may be gone now)."""
    return Path(os.path.abspath(Path(path).expanduser())).resolve()


def _namespace(store, root: Path, moved_from=None) -> dict:
    """The vault's stable id, with notes from its earlier namespaces renamed to it once.

    A vault the store already knows at another folder that still exists with the same id is
    a copy, and is refused rather than letting two folders overwrite each other's notes.
    """
    try:
        namespace, vault_id, note = _vault_identity(root)
    except ValueError as exc:
        known = store.vault_at(str(root)) if hasattr(store, 'vault_at') else None
        hint = f' Its recorded id is {known}: restore the file from a backup or sync copy, or write {{"vault_id": "{known}"}} into it.' if known else ' Restore it from a backup or sync copy.'
        raise ValueError(str(exc) + '.' + hint) from exc
    result = {'namespace': namespace, 'vault_id': vault_id, 'migrated': 0, 'note': note}
    if vault_id is None or not hasattr(store, 'adopt_vault'):
        return result
    known = store.vault_root(vault_id)
    if known and known != str(root):
        other = Path(known)
        try:
            same = os.path.samefile(other, root)
        except OSError:
            same = False
        if not same:
            try:
                copied = other.is_dir() and not other.is_symlink() and _read_vault_id(other.resolve()) == vault_id
            except (OSError, ValueError):
                copied = False
            if copied:
                raise ValueError(f'This folder and {known} have the same {VAULT_ID_FILE}, so one is a copy of the other. '
                                 f'Work Copilot keeps capturing {known}. To capture this folder as a separate vault, '
                                 f'delete {VAULT_ID_FILE} in it and capture again.')
    olds = [_legacy_namespace(root)] + ([_legacy_namespace(_old_root(moved_from))] if moved_from else [])
    adopted = store.adopt_vault(vault_id, str(root), olds, expected_root=known)
    result['migrated'] = adopted['migrated']
    if adopted['renamed']:
        try:
            result['rewritten_headers'] = _rewrite_note_headers(root, adopted['renamed'], vault_id)
        except OSError as exc:
            result['note'] = f'Could not update the ids in Inbox note headers ({exc.strerror or exc}).'
    return result


def _record_id(source_id: str) -> str:
    """knowledge.record_id(source_id, 'vault'): the record id a captured note's header carries."""
    return 'vault-' + hashlib.sha256(source_id.encode()).hexdigest()[:16]


_FRONTMATTER = re.compile(r'\A---\r?\n.*?\r?\n---\r?\n', re.S)


def _rewrite_note_headers(root: Path, old_namespaces, namespace: str) -> int:
    """Rename source and record ids in the headers of notes capture_note wrote to Inbox/.

    Only the frontmatter changes, and only ids under an old namespace, which no
    hand-written note carries. The note body and every other note stay as written.
    """
    inbox = root / 'Inbox'
    if inbox.is_symlink() or not inbox.is_dir():
        return 0
    rewritten = 0
    for path in sorted(inbox.glob('*.md')):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = _read(root, path.relative_to(root)).decode('utf-8')
        except (OSError, ValueError, UnicodeError):
            continue
        match = _FRONTMATTER.match(text)
        if not match:
            continue
        head = new_head = match.group(0)
        for old in old_namespaces:
            for source_id in set(re.findall(rf'vault:{old}:[^\s"\',\]}}]+', head)):
                renamed = f'vault:{namespace}:' + source_id[len(f'vault:{old}:'):]
                new_head = new_head.replace(source_id, renamed).replace(_record_id(source_id), _record_id(renamed))
        if new_head == head:
            continue
        temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        with temporary.open('xb') as handle:
            handle.write((new_head + text[len(head):]).encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        rewritten += 1
    return rewritten


def _sources(store):
    if hasattr(store, 'list_sources'):
        return store.list_sources(provider='vault')
    return store.snapshot().get('sources', [])


def capture_vault(store, root, moved_from=None) -> dict:
    """Capture the vault. ``moved_from`` names the folder it was in before it moved, when
    that move happened before the vault had an id file (its notes are then renamed)."""
    with _vault_lock(root):
        return _capture_vault_unlocked(store, root, moved_from)


def _capture_vault_unlocked(store, root, moved_from=None) -> dict:
    root = _root(root)
    vault = _namespace(store, root, moved_from)
    namespace = vault['namespace']
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
                    raise ValueError('obligations must be a JSON array of objects')
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
                # A moved vault keeps its ids but changes its file URLs: that needs a new revision.
                if previous.get('content_hash') == content_hash and previous.get('extraction_hash') == extraction_hash and previous.get('url') == path.as_uri() and not previous.get('deleted'):
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
    report = {'captured': len(items), 'errors': errors, 'root': str(root), 'vault_id': vault['vault_id'], 'namespace': namespace, 'result': result}
    if vault.get('migrated'):
        report['migrated_note_ids'] = vault['migrated']
    if vault.get('rewritten_headers'):
        report['rewritten_note_headers'] = vault['rewritten_headers']
    if vault.get('note'):
        report['note'] = vault['note']
    return report


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


def _write_inbox_note_unlocked(store, root, title: str, body: str, project_ids: list[str] | None = None, workstream: str | None = None, project_id: str | None = None) -> dict:
    """Create a new local note only in the explicitly selected vault Inbox."""
    import uuid
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
    if selected or workstream:
        lines = body.splitlines(keepends=True)
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None) if lines and lines[0].strip() == '---' else None
        frontmatter = ''.join(lines[1:closing]) if closing is not None else ''
        rest = ''.join(lines[closing + 1:]) if closing is not None else body
        additions = []
        if selected:
            existing = _metadata(body).get('project_ids', [])
            if isinstance(existing, str):
                existing = [existing]
            if not isinstance(existing, list) or not all(isinstance(p, str) for p in existing):
                raise ValueError('Existing project_ids metadata must contain stable ID strings')
            additions.append('project_ids: ' + json.dumps(list(dict.fromkeys([*existing, *selected]))))
        if workstream:
            additions.append('workstream: ' + json.dumps(workstream))
        body = '---\n' + frontmatter + '\n'.join(additions) + '\n---\n' + rest
    title = ' '.join(str(title).split())[:200] or 'Captured context'
    inbox = root / 'Inbox'
    if inbox.is_symlink():
        raise ValueError('Inbox cannot be a symlink')
    inbox.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime('%Y-%m-%d-') + uuid.uuid4().hex + '.md'
    content = f'# {title}\n\n{body}\n'
    lines = body.splitlines(keepends=True)
    if lines and lines[0].strip() == '---':
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if closing is not None:
            frontmatter = ''.join(lines[:closing + 1]).rstrip('\r\n')
            rest = ''.join(lines[closing + 1:])
            content = f'{frontmatter}\n\n# {title}\n\n{rest}\n'
    if os.name == 'posix':
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open('Inbox', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    handle.write(content)
            finally:
                os.close(directory)
        finally:
            os.close(rootfd)
    else:
        with (inbox / name).open('x', encoding='utf-8') as handle:
            handle.write(content)
    capture = _capture_vault_unlocked(store, root)
    source_id = f'vault:{capture["namespace"]}:Inbox/{name}'
    obligations = [o['id'] for o in store.snapshot().get('obligations', []) if any(s.get('id') == source_id for s in o.get('evidence', []))]
    return {'path': str(inbox / name), 'source_ids': [source_id], 'obligation_ids': obligations, 'capture': capture}


def write_inbox_note(store, root, title: str, body: str, project_ids=None, workstream=None, project_id=None) -> dict:
    with _vault_lock(root):
        return _write_inbox_note_unlocked(store, root, title, body, project_ids=project_ids, workstream=workstream, project_id=project_id)
