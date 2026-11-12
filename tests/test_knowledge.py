"""Source envelope contract and export of Source records to a knowledge inbox (KAN-442)."""
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from work_copilot.knowledge import (
    ENVELOPE_VERSION,
    RECORD_FORMAT_VERSION,
    export_sources,
    parse_frontmatter,
    validate_envelope,
)
from work_copilot.store import Store
from work_copilot.vault import capture_vault

CONTRACT = Path(__file__).parent / 'contracts' / 'knowledge-record.schema.json'
# ai-dlc contracts/knowledge-record.schema.json at commit e5d535622dbb (PR #256, KAN-421).
# Refresh the copy and this hash together when the contract changes.
CONTRACT_SHA256 = '0a7633d88024a668140a6d658f09f5d11bf7e26b8ff09368abd82d17ddcd9bd7'
FIXTURES = Path(__file__).parent / 'fixtures' / 'knowledge_export'
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
SOURCES = [
    {'id': 'symphony:tenant:thread:42', 'revision': '3', 'provider': 'symphony', 'title': 'Ticket naming thread',
     'body': 'Agreed: ticket titles start with the repository name and a colon.\nThanks all.',
     'url': 'https://chat.example.invalid/rooms/team/threads/42', 'observed_at': '2026-11-06T10:12:00+00:00',
     'projects': [{'id': 'atlas', 'name': 'Atlas'}],
     'obligations': [{'intent_key': 'rename-tickets', 'title': 'Rename open tickets', 'project_id': 'atlas', 'owner': None, 'status': 'candidate'}]},
    {'id': 'zoom:tenant:meeting:7', 'revision': '1', 'provider': 'zoom', 'title': 'Weekly planning call',
     'body': "Speaker 1: Let's keep releases on Thursdays.\nSpeaker 2: I'll share the release notes by Friday.\n",
     'observed_at': '2026-11-10T15:00:00-05:00', 'url': 'file:///private/path/transcript.vtt'},
    {'id': 'vault:fixture:Daily/2026-11-10.md', 'revision': 'r1', 'provider': 'vault', 'title': 'Daily/2026-11-10.md',
     'content_hash': 'a' * 64, 'observed_at': '2026-11-10T21:00:00+00:00',
     'body': 'L1: # Tuesday\nL2: private scratch line\nL3: >> Pin the harness by digest.\nL4: >>Ask about release notes'},
]


def schema():
    return json.loads(CONTRACT.read_text(encoding='utf-8'))


def validator():
    return Draft202012Validator(schema(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def inbox_files(root):
    return sorted(p for p in (root / 'inbox').rglob('*') if p.is_file())


def record_errors(path):
    front = parse_frontmatter(path.read_text(encoding='utf-8'))
    assert front is not None, path
    return [error.message for error in validator().iter_errors(front)]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'knowledge'
    root.mkdir()
    return root.resolve()


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 's.db', now=lambda: NOW, timezone_name='America/New_York')
    value.ingest([dict(s) for s in SOURCES])
    yield value
    value.close()


def test_vendored_contract_is_the_pinned_copy():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == CONTRACT_SHA256
    published = schema()
    assert published['x-format-version'] == RECORD_FORMAT_VERSION
    for field in ('schema_version', 'revision', 'provider', 'title', 'observed_at', 'obligations', 'url'):
        assert field in published['properties']
    assert published['properties']['schema_version']['anyOf'][0]['const'] == ENVELOPE_VERSION


def test_exported_records_match_the_contract_and_fixtures(store, repo):
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert result['external_writes'] is False
    records = sorted((repo / 'inbox').glob('*.md'))
    assert len(records) == 3 and len(result['written']) == 3
    for path in records:
        assert record_errors(path) == []
        assert re.fullmatch(r'[a-z0-9-]+--[0-9a-f]{16}\.md', path.name)
    exported = {p.relative_to(repo).as_posix(): p.read_bytes() for p in inbox_files(repo)}
    committed = {p.relative_to(FIXTURES).as_posix(): p.read_bytes() for p in FIXTURES.rglob('*') if p.is_file()}
    assert exported == committed


def test_committed_fixtures_pass_the_contract():
    records = sorted((FIXTURES / 'inbox').glob('*.md'))
    assert records
    for path in records:
        assert record_errors(path) == []


def test_record_points_at_raw_payload_by_sha256(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    for path in (repo / 'inbox').glob('*.md'):
        front = parse_frontmatter(path.read_text(encoding='utf-8'))
        raw = front['sources'][0]
        payload = (repo / raw['resource']).read_bytes()
        assert raw['hash'] == 'sha256:' + hashlib.sha256(payload).hexdigest()
        assert front['status'] == 'raw' and front['schema_version'] == 1
        assert front['stale_after'] == '2026-12-12T18:00:00Z'
        assert 'file://' not in path.read_text(encoding='utf-8')


def test_second_export_writes_nothing(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox_files(repo)}
    again = export_sources(store, repo, owner='sean', now=NOW + timedelta(hours=1))
    assert again['written'] == [] and again['raw_written'] == [] and again['expired'] == []
    assert len(again['unchanged']) == 3
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox_files(repo)} == before


def test_new_revision_is_a_new_record_and_tombstones_are_not_exported(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    store.ingest([dict(SOURCES[0], revision='4', body='Revised thread.', observed_at='2026-11-11T10:00:00+00:00')])
    store.ingest([dict(SOURCES[1], revision='2', deleted=True, body='', obligations=[], observed_at='2026-11-11T10:00:00+00:00')])
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert len(result['written']) == 1
    providers = [parse_frontmatter((repo / 'inbox' / name).read_text(encoding='utf-8'))['provider'] for name in result['written']]
    assert providers == ['symphony']
    ids = [parse_frontmatter(p.read_text(encoding='utf-8'))['id'] for p in (repo / 'inbox').glob('*.md')]
    assert len(ids) == len(set(ids)) == 4


def test_sha256_deduplicates_payloads_and_records(store, repo):
    # Same payload under another title: a second record, one raw file.
    store.ingest([dict(SOURCES[0], id='symphony:tenant:thread:43', title='Forwarded thread', url=None)])
    export_sources(store, repo, owner='sean', now=NOW)
    assert len(list((repo / 'inbox').glob('*.md'))) == 4
    assert len(list((repo / 'inbox' / 'raw').glob('*.txt'))) == 3
    # Same title and payload, even under a new revision or id: not a new record.
    store.ingest([dict(SOURCES[0], revision='5', observed_at='2026-11-11T00:00:00+00:00'),
                  dict(SOURCES[0], id='symphony:tenant:thread:45', url=None)])
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert result['written'] == [] and len(result['duplicates']) == 2
    assert len(list((repo / 'inbox').glob('*.md'))) == 4


def test_since_limits_the_export(store, repo):
    result = export_sources(store, repo, owner='sean', since='2026-11-10T00:00:00+00:00', now=NOW)
    providers = sorted(parse_frontmatter((repo / 'inbox' / n).read_text(encoding='utf-8'))['provider'] for n in result['written'])
    assert providers == ['notes', 'zoom']
    with pytest.raises(ValueError):
        export_sources(store, repo, owner='sean', since='2026-11-10', now=NOW)


def test_raw_payloads_expire_and_records_stay(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    later = NOW + timedelta(days=31)
    store.ingest([dict(SOURCES[0], id='symphony:tenant:thread:44', title='Thread follow-up', url=None, observed_at='2026-12-13T09:00:00+00:00')])
    result = export_sources(store, repo, owner='sean', now=later)
    assert len(result['expired']) == 3
    # Expiry runs first; the new record then writes the payload it shares again.
    assert len(result['removed_raw']) == 3
    records = sorted((repo / 'inbox').glob('*.md'))
    assert len(records) == 4
    statuses = sorted(parse_frontmatter(p.read_text(encoding='utf-8'))['status'] for p in records)
    assert statuses == ['expired', 'expired', 'expired', 'raw']
    for path in records:
        assert record_errors(path) == []
    assert len(list((repo / 'inbox' / 'raw').glob('*.txt'))) == 1
    assert export_sources(store, repo, owner='sean', now=later)['written'] == []


def test_unmarked_note_lines_never_leave_the_vault(tmp_path, repo):
    vault = tmp_path / 'vault'
    (vault / 'Daily').mkdir(parents=True)
    vault = vault.resolve()
    (vault / 'Daily' / '2026-11-12.md').write_text(
        '# Thursday\nUNMARKED-SECRET salary talk\n>> Decision: ship on Thursdays\n  >> indented marked line\n> quoted UNMARKED-QUOTE\nend\n',
        encoding='utf-8')
    (vault / 'Daily' / '2026-11-11.md').write_text('only UNMARKED-OTHER lines here\n', encoding='utf-8')
    (vault / 'meeting.md').write_text('UNMARKED-MEETING\n>> From a note without a date\n', encoding='utf-8')
    store = Store(tmp_path / 'v.db', timezone_name='UTC')
    capture_vault(store, vault)
    result = export_sources(store, repo, owner='sean', now=NOW)
    text = '\n'.join(p.read_text(encoding='utf-8') for p in inbox_files(repo))
    assert 'UNMARKED' not in text
    assert 'Decision: ship on Thursdays' in text and 'indented marked line' in text
    assert 'From a note without a date' in text
    days = sorted(parse_frontmatter((repo / 'inbox' / n).read_text(encoding='utf-8'))['date'] for n in result['written'])
    assert '2026-11-12' in days and '2026-11-11' not in days
    for path in (repo / 'inbox').glob('*.md'):
        assert record_errors(path) == []
    store.close()


def test_export_refuses_missing_owner_or_repo(store, tmp_path):
    with pytest.raises(ValueError):
        export_sources(store, tmp_path, owner=' ', now=NOW)
    with pytest.raises(ValueError):
        export_sources(store, tmp_path / 'missing', owner='sean', now=NOW)


def test_ingest_validates_the_envelope(tmp_path):
    store = Store(tmp_path / 's.db')
    base = dict(SOURCES[0])
    with pytest.raises(ValueError, match='schema_version'):
        store.ingest([dict(base, schema_version=2)])
    with pytest.raises(ValueError, match='observed_at'):
        store.ingest([dict(base, observed_at='2026-11-06T10:12:00')])
    with pytest.raises(ValueError, match='obligations'):
        store.ingest([dict(base, obligations=[{'intent_key': 'x'}])])
    with pytest.raises(ValueError, match='title'):
        store.ingest([dict(base, title=7)])
    assert store.ingest([base])['inserted'] == 1
    assert store.list_sources()[0]['schema_version'] == ENVELOPE_VERSION
    assert store.ingest([dict(base, schema_version=1)])['unchanged'] == 1
    validate_envelope(dict(base, schema_version=1))
    store.close()


def test_payloads_stored_before_schema_version_replay_unchanged(tmp_path):
    store = Store(tmp_path / 's.db')
    legacy = dict(SOURCES[1], projects=[], obligations=[], deleted=False)
    store.db.execute('INSERT INTO source_revisions VALUES(?,?,?)', (legacy['id'], legacy['revision'], json.dumps(legacy)))
    store.db.commit()
    assert store.ingest([dict(SOURCES[1])])['unchanged'] == 1
    store.close()


def test_cli_export_to_knowledge_repo(tmp_path, repo, monkeypatch, capsys):
    from work_copilot import cli
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'copilot.db')
    store.ingest([dict(SOURCES[0])])
    store.close()
    monkeypatch.setattr(sys, 'argv', ['work-copilot', '--data-dir', str(data), 'export', '--since', '2026-11-01T00:00:00Z', '--to', str(repo), '--owner', 'sean'])
    cli.main()
    assert len(json.loads(capsys.readouterr().out)['written']) == 1
    monkeypatch.setattr(sys, 'argv', ['work-copilot', '--data-dir', str(data), 'export', '--to', str(repo), '--owner', 'sean'])
    cli.main()
    assert json.loads(capsys.readouterr().out)['written'] == []


def test_mcp_write_tools_have_typed_inputs(tmp_path):
    from work_copilot.mcp_server import build_server
    tools = {tool.name: tool for tool in asyncio.run(build_server(tmp_path).list_tools())}
    assert len(tools) == 10

    def fields(name, argument):
        spec = tools[name].inputSchema
        ref = spec['properties'][argument]['$ref'].rsplit('/', 1)[-1]
        return spec['$defs'][ref]

    track = fields('track_obligation', 'payload')
    assert set(track['required']) == {'intent_key', 'title'} and track['additionalProperties'] is False
    assert {'project_id', 'owner', 'status', 'evidence_ids', 'follow_up_at'} <= set(track['properties'])
    review = fields('review_obligation', 'changes')
    assert {'owner', 'status', 'question_state', 'due_at'} <= set(review['properties'])
    project = fields('update_project', 'changes')
    assert set(project['properties']) == {'name', 'aliases', 'rooms'}
