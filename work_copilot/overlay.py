"""The company overlay: which work site, knowledge space, Jira project and review room this install serves (KAN-381).

team-harness renders it from the company's org overlay (`scripts/work_copilot_overlay.py --harness <name>`), so the
harness and Work Copilot read one source. `overlay apply <file>`, run through the launcher (`./run.sh` or
`.\\run.ps1`, with `--data-dir` before it), checks the file and records it as `overlay.json` in the private data
directory, with the time it was applied; `doctor` reports it. The file names places, never credentials, and applying
it contacts nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 'work-copilot.overlay/v1'
MAX_BYTES = 16 * 1024
# These formats must stay identical to team-harness scripts/work_copilot_overlay.py's FORMATS, which normalises to
# them; both repositories test the same cases, and tests/fixtures/overlay/ holds that script's output as goldens.
_TEXT = re.compile(r'[^\x00-\x1f\x7f-\x9f\u2028\u2029]{1,200}')  # no C0 or C1 controls, DEL or line separators
_FIELDS = {
    'name': _TEXT,
    'source': _TEXT,
    'work_site': re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63}){1,10}'),
    'kb_space': re.compile(r'~?[A-Za-z0-9][A-Za-z0-9_-]{0,63}'),  # ~ first: a personal space
    'jira_project': re.compile(r'[A-Z][A-Z0-9_]{0,19}'),
    'room': _TEXT,
}
_ALLOWED = {'schema', 'placeholder', *_FIELDS}


def validate(data):
    """The overlay's fields if ``data`` is a valid overlay; ``ValueError`` naming every problem otherwise."""
    if not isinstance(data, dict):
        raise ValueError('Overlay must be a JSON object')
    problems = []
    if data.get('schema') != SCHEMA:
        problems.append(f'schema must be {SCHEMA}')
    unknown = sorted(set(data) - _ALLOWED)
    if unknown:
        problems.append('unknown keys: ' + ', '.join(unknown))
    if not isinstance(data.get('placeholder', False), bool):
        problems.append('placeholder must be true or false')
    if not isinstance(data.get('name'), str):
        problems.append('name is required')
    for key, pattern in _FIELDS.items():
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not pattern.fullmatch(value) or value != value.strip():
            problems.append(f'{key} is not valid')
    if problems:
        raise ValueError('Invalid overlay: ' + '; '.join(problems))
    return {key: data[key] for key in ('schema', 'placeholder', *_FIELDS) if key in data}


def read(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'{path} is a symlink; give the overlay file itself')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f'{path} is larger than 16 KiB; an overlay is a few lines')
    raw = path.read_bytes()
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raise ValueError(f'{path} is UTF-16 (as PowerShell\'s > writes it); render it again with '
                         '`work_copilot_overlay.py --out <file>`, which writes UTF-8')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except UnicodeDecodeError:
        raise ValueError(f'{path} is not UTF-8; render it again with `work_copilot_overlay.py --out <file>`') from None
    except json.JSONDecodeError as exc:
        raise ValueError(f'{path} is not JSON: {exc}') from None
    return validate(data)


def apply(data_dir, path, now=None):
    """Check the overlay file and record it as ``<data dir>/overlay.json``; returns what was recorded."""
    marker = dict(read(path))
    marker['placeholder'] = marker.get('placeholder', False)
    marker['applied_at'] = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    target = Path(data_dir) / 'overlay.json'
    if target.is_symlink():
        raise ValueError(f'{target} is a symlink; remove it first')
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix='.overlay-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(marker, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if os.name == 'posix':
        os.chmod(target, 0o600)
    return {'applied': True, 'path': str(target), **marker,
            'note': 'A placeholder overlay is recorded; replace it with your company overlay.' if marker['placeholder']
            else 'Company overlay recorded; doctor reports it.'}
