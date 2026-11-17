"""work-copilot overlay apply: the company overlay rendered by team-harness (KAN-381)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from work_copilot.doctor import overlay_check
from work_copilot.overlay import SCHEMA, apply, validate

# Fictional company, the shape team-harness's scripts/work_copilot_overlay.py writes.
FICTIONAL = {'schema': SCHEMA, 'name': 'example-co', 'placeholder': False, 'source': 'orgs/example-co/org.yaml',
             'work_site': 'example-co.atlassian.net', 'kb_space': 'ENG', 'jira_project': 'EXMP', 'room': '#eng-review'}


def _file(tmp_path, data, name='overlay-in.json'):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_apply_records_the_overlay_and_doctor_reports_it(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    now = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)
    result = apply(data, _file(tmp_path, FICTIONAL), now=now)
    assert result['applied'] and result['applied_at'] == '2026-11-17T20:00:00+00:00'
    stored = json.loads((data / 'overlay.json').read_text(encoding='utf-8'))
    assert stored == {**FICTIONAL, 'applied_at': '2026-11-17T20:00:00+00:00'}
    report = overlay_check(data)
    assert report['state'] == 'applied' and report['ok']
    assert (report['work_site'], report['jira_project'], report['room']) == ('example-co.atlassian.net', 'EXMP', '#eng-review')
    if os.name == 'posix':
        assert (data / 'overlay.json').stat().st_mode & 0o077 == 0


def test_placeholder_overlay_is_recorded_as_a_warning(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    apply(data, _file(tmp_path, {**FICTIONAL, 'placeholder': True, 'jira_project': 'WORKPROJ',
                                 'work_site': 'COMPANY-SITE.atlassian.net'}))
    assert overlay_check(data)['state'] == 'placeholder'


def test_only_the_named_fields_are_needed(tmp_path):
    assert validate({'schema': SCHEMA, 'name': 'example-co'}) == {'schema': SCHEMA, 'name': 'example-co'}


@pytest.mark.parametrize('bad', [
    [], 'x', {},
    {**FICTIONAL, 'schema': 'work-copilot.overlay/v2'},
    {**FICTIONAL, 'token': 'abc'},
    {**FICTIONAL, 'placeholder': 'no'},
    {k: v for k, v in FICTIONAL.items() if k != 'name'},
    {**FICTIONAL, 'work_site': 'https://example-co.atlassian.net/'},
    {**FICTIONAL, 'jira_project': 'exmp'},
    {**FICTIONAL, 'room': 'eng\nreview'},
    {**FICTIONAL, 'kb_space': ' ENG'},
], ids=['list', 'string', 'empty', 'schema', 'unknown-key', 'placeholder-type', 'no-name', 'site-url',
        'project-case', 'room-newline', 'space-edge'])
def test_invalid_overlay_is_refused_and_nothing_written(tmp_path, bad):
    data = tmp_path / 'data'
    data.mkdir()
    with pytest.raises(ValueError):
        apply(data, _file(tmp_path, bad))
    assert list(data.iterdir()) == []


def test_symlinked_or_oversized_files_are_refused(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    big = _file(tmp_path, {**FICTIONAL, 'room': 'x' * 100}, 'big.json')
    big.write_text(' ' * 20000 + json.dumps(FICTIONAL), encoding='utf-8')
    with pytest.raises(ValueError, match='16 KiB'):
        apply(data, big)
    if os.name == 'posix':
        link = tmp_path / 'link.json'
        link.symlink_to(_file(tmp_path, FICTIONAL))
        with pytest.raises(ValueError, match='symlink'):
            apply(data, link)


def test_cli_apply_and_show(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
           'WORK_COPILOT_DATA_DIR': str(tmp_path / 'data')}
    env.pop('XDG_DATA_HOME', None)
    run = lambda *a: subprocess.run([sys.executable, '-m', 'work_copilot', '--data-dir', str(tmp_path / 'data'), *a],
                                    capture_output=True, text=True, env=env, timeout=60)
    out = run('overlay', 'apply', str(_file(tmp_path, FICTIONAL)))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)['jira_project'] == 'EXMP'
    shown = run('overlay', 'show')
    assert shown.returncode == 0 and json.loads(shown.stdout)['state'] == 'applied'
    bad = run('overlay', 'apply', str(_file(tmp_path, {'schema': 'x'}, 'bad.json')))
    assert bad.returncode == 1 and 'schema must be' in bad.stderr
    assert run('overlay', 'apply').returncode == 2
