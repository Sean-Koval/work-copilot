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
# ai-dlc contracts/knowledge-record.schema.json at main c10ba3b3 (adds boundary `project`, KAN-457/475).
# Refresh the copy and this hash together when the contract changes.
CONTRACT_SHA256 = 'eda789937b670f3bc527ebffc366464af650bf3464defab33e084b0409444f59'
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
    (root / 'knowledge.toml').write_text('[format]\nversion = 1\n', encoding='utf-8')
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
        front = parse_frontmatter(path.read_text(encoding='utf-8'))
        assert path.name.startswith(front['id'] + '--') and front['revision']
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
        text = path.read_text(encoding='utf-8')
        assert 'file://' not in text and 'permanent' not in text


def test_second_export_writes_nothing(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox_files(repo)}
    again = export_sources(store, repo, owner='sean', now=NOW + timedelta(days=40))
    assert again['written'] == [] and again['raw_written'] == []
    assert len(again['unchanged']) == 3
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox_files(repo)} == before


def test_ids_are_stable_and_existing_files_are_never_touched(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    before = {p: p.read_bytes() for p in inbox_files(repo)}
    store.ingest([dict(SOURCES[0], revision='4', body='Revised thread.', observed_at='2026-11-11T10:00:00+00:00')])
    store.ingest([dict(SOURCES[1], revision='2', deleted=True, body='', obligations=[], observed_at='2026-11-11T10:00:00+00:00')])
    result = export_sources(store, repo, owner='sean', now=NOW + timedelta(days=40))
    assert len(result['written']) == 1
    new = parse_frontmatter((repo / 'inbox' / result['written'][0]).read_text(encoding='utf-8'))
    old = next(parse_frontmatter(p.read_text(encoding='utf-8')) for p in before
               if p.suffix == '.md' and p.name.startswith('symphony-'))
    assert new['provider'] == 'symphony' and new['id'] == old['id']
    assert (new['revision'], old['revision']) == ('4', '3')
    assert all(p.read_bytes() == data for p, data in before.items())


def test_dedupe_hashes_the_envelope_without_revision(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    # Same envelope under a new revision: not a new record.
    store.ingest([dict(SOURCES[0], revision='5')])
    assert export_sources(store, repo, owner='sean', now=NOW)['written'] == []
    # A changed obligation needs a new revision, and gives a new record.
    changed = [{'intent_key': 'rename-tickets', 'title': 'Rename open tickets', 'status': 'open'}]
    with pytest.raises(ValueError, match='revision'):
        store.ingest([dict(SOURCES[0], revision='5', obligations=changed)])
    store.ingest([dict(SOURCES[0], revision='6', obligations=changed)])
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert len(result['written']) == 1 and result['raw_written'] == []
    # Same body under another id: a second record that shares one raw file.
    store.ingest([dict(SOURCES[0], id='symphony:tenant:thread:43', url=None)])
    assert len(export_sources(store, repo, owner='sean', now=NOW)['written']) == 1
    assert len(list((repo / 'inbox').glob('*.md'))) == 5
    assert len(list((repo / 'inbox' / 'raw').glob('*.txt'))) == 3


def test_a_tampered_raw_payload_is_not_overwritten(store, repo):
    export_sources(store, repo, owner='sean', now=NOW)
    raw = sorted((repo / 'inbox' / 'raw').glob('*.txt'))[0]
    raw.write_bytes(b'edited')
    for path in (repo / 'inbox').glob('*.md'):
        path.rename(path.with_suffix('.moved'))
    with pytest.raises(ValueError, match='not overwriting'):
        export_sources(store, repo, owner='sean', now=NOW)
    assert raw.read_bytes() == b'edited'


def test_since_limits_the_export(store, repo):
    result = export_sources(store, repo, owner='sean', since='2026-11-10T00:00:00+00:00', now=NOW)
    providers = sorted(parse_frontmatter((repo / 'inbox' / n).read_text(encoding='utf-8'))['provider'] for n in result['written'])
    assert providers == ['notes', 'zoom']
    with pytest.raises(ValueError):
        export_sources(store, repo, owner='sean', since='2026-11-10', now=NOW)


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


# --- Customer boundary (KAN-489) ----------------------------------------------

CUSTOMER_SOURCES = [
    {'id': 'symphony:tenant:thread:alpha-1', 'revision': '1', 'provider': 'symphony', 'title': 'ALPHA-TITLE outage room',
     'body': 'ALPHA-BODY: the cutover moves to Saturday.', 'observed_at': '2026-11-11T10:00:00+00:00',
     'projects': [{'id': 'alpha', 'name': 'Alpha'}], 'obligations': []},
    {'id': 'symphony:tenant:thread:mixed-2', 'revision': '1', 'provider': 'symphony', 'title': 'MIXED-TITLE',
     'body': 'MIXED-BODY: shared room for alpha and atlas.', 'observed_at': '2026-11-11T11:00:00+00:00',
     'projects': [{'id': 'alpha', 'name': 'Alpha'}, {'id': 'atlas', 'name': 'Atlas'}], 'obligations': []},
    {'id': 'symphony:tenant:thread:unbound-3', 'revision': '1', 'provider': 'symphony', 'title': 'UNBOUND-TITLE',
     'body': 'UNBOUND-BODY: a room bound to no project.', 'observed_at': '2026-11-11T12:00:00+00:00',
     'projects': [], 'obligations': []},
    {'id': 'symphony:tenant:thread:beta-4', 'revision': '1', 'provider': 'symphony', 'title': 'BETA-TITLE',
     'body': 'BETA-BODY: another customer.', 'observed_at': '2026-11-11T13:00:00+00:00',
     'projects': [{'id': 'beta', 'name': 'Beta'}], 'obligations': []},
]
TEAM_TOML = '''[transcripts]
owner = "sean"

[[transcripts.customers]]
project = "alpha"
titles = ["Alpha"]

[[transcripts.customers]]
project = "beta"
titles = ["Beta"]
'''


def _customer_repo(tmp_path, project='alpha'):
    root = tmp_path / f'{project}-knowledge'
    (root / 'inbox').mkdir(parents=True)
    (root / 'knowledge.toml').write_text(
        f'[format]\nversion = 1\n\n[repo]\nboundary = "customer"\nproject = "{project}"\n', encoding='utf-8')
    return root.resolve()


def _text(root):
    return '\n'.join(p.read_text(encoding='utf-8') for p in inbox_files(root))


@pytest.fixture
def customer_store(tmp_path):
    value = Store(tmp_path / 'c.db', now=lambda: NOW, timezone_name='UTC')
    value.ingest([dict(s) for s in SOURCES + CUSTOMER_SOURCES])
    yield value
    value.close()


def test_team_repository_never_receives_customer_or_unbound_sources(customer_store, repo):
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    result = export_sources(customer_store, repo, owner='sean', now=NOW)
    text = _text(repo)
    for marker in ('ALPHA', 'MIXED', 'UNBOUND', 'BETA'):
        assert marker not in text
    # Atlas is not a customer, so its thread goes; the transcript is bound to no project, so it does not.
    assert 'Ticket naming thread' in text and 'Weekly planning call' not in text
    assert 'Pin the harness by digest' in text  # marked notes are the user's own words
    reasons = {item['reason'] for item in result['skipped']}
    assert any('customer project' in r for r in reasons) and any('no project' in r for r in reasons)
    assert len(result['skipped']) == 5
    # The report names a skipped Source by record id only: no title, no source id.
    report = json.dumps(result)
    for marker in ('ALPHA', 'MIXED', 'UNBOUND', 'BETA', 'thread:alpha', 'Weekly planning'):
        assert marker not in report


def test_customer_repository_receives_only_its_own_sources(customer_store, tmp_path):
    alpha = _customer_repo(tmp_path)
    result = export_sources(customer_store, alpha, owner='sean', scope='team', now=NOW)
    text = _text(alpha)
    assert 'ALPHA-BODY' in text
    for marker in ('MIXED', 'UNBOUND', 'BETA', 'Ticket naming', 'Weekly planning', 'Pin the harness'):
        assert marker not in text
    assert result['customer'] == 'alpha' and len(result['written']) == 1
    front = parse_frontmatter((alpha / 'inbox' / result['written'][0]).read_text(encoding='utf-8'))
    assert (front['scope'], front['boundary'], front['project']) == ('project', 'customer', 'alpha')
    assert record_errors(alpha / 'inbox' / result['written'][0]) == []
    assert 'ALPHA' not in json.dumps(result['skipped'])


def test_customer_marked_notes_stay_with_the_customer(tmp_path, repo):
    vault = tmp_path / 'vault'
    (vault / 'Daily').mkdir(parents=True)
    (vault / 'Daily' / '2026-11-12.md').write_text('>> Team note: pin the harness\n', encoding='utf-8')
    store = Store(tmp_path / 'v.db', timezone_name='UTC')
    capture_vault(store, vault.resolve())
    [note] = [s for s in store.list_sources() if s['provider'] == 'vault']
    store.ingest([dict(note, id=note['id'] + ':alpha', revision='r2', title='Daily/2026-11-12 alpha.md',
                       body='L1: >> ALPHA-NOTE: the cutover moves', projects=[{'id': 'alpha', 'name': 'Alpha'}])])
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    export_sources(store, repo, owner='sean', now=NOW)
    assert 'Team note' in _text(repo) and 'ALPHA-NOTE' not in _text(repo)
    alpha = _customer_repo(tmp_path)
    export_sources(store, alpha, owner='sean', now=NOW)
    assert 'ALPHA-NOTE' in _text(alpha) and 'Team note' not in _text(alpha)
    store.close()


@pytest.mark.parametrize('toml, message', [
    ('[repo]\nboundary = "team"\nproject = "alpha"\n', 'boundary'),
    ('[repo]\nboundary = "customer"\n', 'project'),
    ('[repo]\nboundary = "customer"\nproject = "Alpha Corp"\n', 'project'),
    ('[repo]\nbondary = "customer"\nproject = "alpha"\n', 'boundary'),
    ('[[transcripts.customers]]\ntitles = ["Alpha"]\n', 'project'),
    ('[transcripts]\ncustomers = "alpha"\n', 'list'),
    ('[repo\n', 'TOML'),
    ('[[transcripts.projects]]\nproject = "alpha"\n', 'retired'),
])
def test_a_malformed_declaration_exports_nothing(customer_store, repo, toml, message):
    (repo / 'knowledge.toml').write_text(toml, encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        export_sources(customer_store, repo, owner='sean', now=NOW)
    assert not (repo / 'inbox').exists()


def test_without_customer_rules_the_team_export_is_unchanged(customer_store, repo):
    result = export_sources(customer_store, repo, owner='sean', now=NOW)
    assert result['skipped'] == [] and result['customer'] is None
    assert 'UNBOUND-BODY' in _text(repo)


@pytest.mark.parametrize('spelling', ['Alpha', ' alpha ', 'ALPHA', '\uff41lpha', 'Alpha!'])
def test_a_differently_written_customer_id_is_still_the_customer(tmp_path, repo, spelling):
    store = Store(tmp_path / 'k.db', now=lambda: NOW, timezone_name='UTC')
    store.ingest([dict(CUSTOMER_SOURCES[0], projects=[{'id': spelling, 'name': 'Alpha'}])])
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert 'ALPHA' not in _text(repo) and len(result['skipped']) == 1
    alpha = _customer_repo(tmp_path)
    assert len(export_sources(store, alpha, owner='sean', now=NOW)['written']) == 1
    store.close()


def test_a_customer_room_stays_the_customers_whatever_the_payload_says(tmp_path, repo):
    store = Store(tmp_path / 'r.db', now=lambda: NOW, timezone_name='UTC')
    store.create_project('alpha', 'Alpha', rooms=[{'id': 'room-alpha', 'name': 'Alpha room'}])
    store.ingest([dict(CUSTOMER_SOURCES[0], room_id='room-alpha', projects=[{'id': 'atlas', 'name': 'Atlas'}])])
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert 'ALPHA' not in _text(repo)
    assert result['written'] == [] and 'customer project' in result['skipped'][0]['reason']
    store.close()


@pytest.mark.parametrize('spelling', ['Acme Corp', 'acme_corp', ' ACME-corp ', 'acme--corp'])
def test_spaces_and_underscores_in_an_id_still_match_the_customer_slug(tmp_path, repo, spelling):
    store = Store(tmp_path / 'u.db', now=lambda: NOW, timezone_name='UTC')
    store.ingest([dict(CUSTOMER_SOURCES[0], projects=[{'id': spelling, 'name': 'Acme'}])])
    (repo / 'knowledge.toml').write_text('[[transcripts.customers]]\nproject = "acme-corp"\n', encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert 'ALPHA' not in _text(repo) and result['written'] == [] and len(result['skipped']) == 1
    store.close()


def test_a_room_bound_to_a_differently_written_customer_is_the_customers(tmp_path, repo):
    store = Store(tmp_path / 'a.db', now=lambda: NOW, timezone_name='UTC')
    store.create_project('Acme Corp', 'Acme', rooms=[{'id': 'room-acme', 'name': 'Acme room'}])
    store.ingest([dict(CUSTOMER_SOURCES[0], room_id='room-acme', projects=[])])
    (repo / 'knowledge.toml').write_text('[[transcripts.customers]]\nproject = "acme-corp"\n', encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert 'ALPHA' not in _text(repo) and 'customer project' in result['skipped'][0]['reason']
    acme = _customer_repo(tmp_path, 'acme-corp')
    assert len(export_sources(store, acme, owner='sean', now=NOW)['written']) == 1
    store.close()


def test_a_folder_without_a_regular_knowledge_toml_gets_nothing(customer_store, tmp_path):
    missing = tmp_path / 'plain'
    missing.mkdir()
    with pytest.raises(ValueError, match='knowledge.toml'):
        export_sources(customer_store, missing, owner='sean', now=NOW)
    (missing / 'knowledge.toml').mkdir()
    with pytest.raises(ValueError, match='knowledge.toml'):
        export_sources(customer_store, missing, owner='sean', now=NOW)
    assert not (missing / 'inbox').exists()


def _note_store(tmp_path, body, **extra):
    vault = tmp_path / 'vault'
    (vault / 'Daily').mkdir(parents=True)
    (vault / 'Daily' / '2026-11-12.md').write_text('>> Team note: pin the harness\n', encoding='utf-8')
    store = Store(tmp_path / 'n.db', timezone_name='UTC')
    capture_vault(store, vault.resolve())
    [note] = [s for s in store.list_sources() if s['provider'] == 'vault']
    store.ingest([dict(note, id=note['id'] + ':x', revision='r2', title='Daily/2026-11-12 x.md', body=body, **extra)])
    return store


def test_a_left_out_note_is_reported_by_record_id(tmp_path, repo):
    store = _note_store(tmp_path, 'L1: >> ALPHA-NOTE: the cutover', projects=[{'id': 'alpha', 'name': 'Alpha'}])
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    assert len(result['skipped']) == 1 and 'ALPHA' not in json.dumps(result)
    store.close()


def test_an_unbound_note_that_names_a_customer_stays_out_of_the_team(tmp_path, repo):
    store = _note_store(tmp_path, 'L1: >> ALPHA-NOTE: the cutover', suggested_projects=['alpha'])
    (repo / 'knowledge.toml').write_text(TEAM_TOML, encoding='utf-8')
    result = export_sources(store, repo, owner='sean', now=NOW)
    text = _text(repo)
    assert 'Team note' in text and 'ALPHA-NOTE' not in text
    assert 'names a customer project' in result['skipped'][0]['reason']
    store.close()
