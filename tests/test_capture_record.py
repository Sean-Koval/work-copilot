"""capture_note writes ai-dlc knowledge-record frontmatter (KAN-394)."""
import hashlib
import shutil
from pathlib import Path

import pytest

from test_knowledge import record_errors
from work_copilot.knowledge import parse_frontmatter
from work_copilot.store import Store
from work_copilot.vault import write_inbox_note


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    store = Store(tmp_path / 'state.db')
    store.create_project('atlas', 'Atlas')
    return store, root


def _split(path):
    text = Path(path).read_text(encoding='utf-8')
    front = parse_frontmatter(text)
    assert front is not None
    payload = text.split('\n---\n', 1)[1]
    return front, payload


def test_plain_capture_is_a_valid_source_record(vault, monkeypatch):
    store, root = vault
    monkeypatch.setenv('WORK_COPILOT_KNOWLEDGE_OWNER', 'sean')
    receipt = write_inbox_note(store, root, 'Standup', 'Decision: staging first.')
    front, payload = _split(receipt['path'])
    assert record_errors(Path(receipt['path'])) == []
    assert (front['type'], front['status'], front['scope'], front['owner']) == ('source', 'raw', 'personal', 'sean')
    digest = 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()
    assert front['revision'] == digest
    assert front['sources'] == [{'id': receipt['source_ids'][0], 'resource': 'Inbox/' + Path(receipt['path']).name, 'hash': digest}]
    assert front['stale_after'].endswith('Z') and front['author'] == 'agent:work-copilot'
    assert payload.startswith('\n# Standup\n\nDecision: staging first.\n')
    # No project: Work Copilot still asks for a project mapping, as before.
    source = store.list_sources()[0]
    assert any(o['intent_key'].startswith('map:') for o in source['obligations'])


def test_projects_and_workstream_round_trip_through_links_and_tags(vault, monkeypatch):
    store, root = vault
    monkeypatch.delenv('WORK_COPILOT_KNOWLEDGE_OWNER', raising=False)
    receipt = write_inbox_note(store, root, 'Plan', 'Ship Thursday.', project_ids=['atlas', 'beacon'], workstream='engineering')
    front, _payload = _split(receipt['path'])
    assert record_errors(Path(receipt['path'])) == []
    assert front['owner'] == 'local-user'
    assert front['links'] == [{'type': 'belongs_to', 'target': 'atlas'}, {'type': 'belongs_to', 'target': 'beacon'}]
    assert front['tags'] == ['capture', 'workstream:engineering']
    assert 'project_ids' not in front and 'workstream' not in front
    source = next(s for s in store.list_sources() if s['id'] == receipt['source_ids'][0])
    assert {p['id'] for p in source['projects']} == {'atlas', 'beacon'}
    assert source['workstream'] == 'engineering'


def test_pasted_frontmatter_is_kept_and_unheld_fields_are_named(vault):
    store, root = vault
    body = ('---\nproject_ids: ["atlas", "beacon"]\nworkstream: "ops"\nreviewer: "dana"\n'
            'obligations: [{"intent_key":"r1","title":"Confirm release","owner":"me","project_id":"atlas","status":"open"}]\n'
            '---\nDecision: staging first.')
    receipt = write_inbox_note(store, root, 'Pasted', body)
    front, payload = _split(receipt['path'])
    assert record_errors(Path(receipt['path'])) == []
    assert front['obligations'] == [{'intent_key': 'r1', 'title': 'Confirm release', 'status': 'open'}]
    assert front['tags'] == ['capture', 'workstream:ops']
    assert receipt['not_in_header'] == ['obligations[0].owner', 'obligations[0].project_id']
    assert 'reviewer: "dana"' in payload and '```yaml' in payload


def test_a_malformed_pasted_obligation_is_refused_before_writing(vault):
    store, root = vault
    body = '---\nobligations: [{"title":"No key"}]\n---\nText'
    with pytest.raises(ValueError, match='intent_key'):
        write_inbox_note(store, root, 'Bad', body)
    assert not any((root / 'Inbox').glob('*.md')) if (root / 'Inbox').exists() else True


def test_ai_dlc_knowledge_check_passes_on_captured_notes(vault):
    """The same check ai-dlc runs, when ai-dlc is installed alongside (it is optional)."""
    notes = pytest.importorskip('ai_dlc.documentation.knowledge_notes')
    store, root = vault
    write_inbox_note(store, root, 'Plain', 'Text only.')
    write_inbox_note(store, root, 'Mapped', 'Ship it.', project_id='atlas', workstream='engineering')
    write_inbox_note(store, root, 'Pasted', '---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"k","title":"T"}]\n---\nBody')
    # ai-dlc's vault check reads <vault>/learnings, so check copies of the notes there.
    learnings = root / 'learnings'
    learnings.mkdir()
    for note in (root / 'Inbox').glob('*.md'):
        shutil.copy(note, learnings / note.name)
    result = notes.check_vault(root)
    assert result['status'] == 'valid', result['invalid']
    assert result['valid'] == 3 and result['deprecated'] == [] and result['legacy'] == []


def test_pasted_fields_the_header_cannot_hold_reach_the_store(vault):
    store, root = vault
    body = ('---\nproject_ids: ["atlas", "beacon"]\n'
            'obligations: [{"intent_key":"r1","title":"Ship","owner":"me","due_at":"2026-12-01T00:00:00+00:00",'
            '"project_id":"atlas","status":"open"}]\n---\nx')
    receipt = write_inbox_note(store, root, 'Pasted', body)
    source = next(s for s in store.list_sources() if s['id'] == receipt['source_ids'][0])
    held = next(o for o in source['obligations'] if o['intent_key'] == 'r1')
    assert (held['owner'], held['due_at'], held['project_id']) == ('me', '2026-12-01T00:00:00+00:00', 'atlas')


def test_a_bad_record_note_is_one_scan_error_not_a_crash(vault):
    from work_copilot.vault import capture_vault
    store, root = vault
    (root / 'Hand.md').write_text('---\ntype: meeting\nproject_ids: null\nobligations: [{"intent_key":"k","title":"T"}]\n---\nhi\n')
    (root / 'Other.md').write_text('---\nproject_ids: ["atlas"]\n---\nok\n')
    capture_vault(store, root)
    assert any(s['title'] == 'Other.md' for s in store.list_sources())


def test_crlf_text_and_control_characters_still_check(vault):
    store, root = vault
    receipt = write_inbox_note(store, root, 'Hi\x7fthere\x9b', 'text\r\nmore\r\n')
    front, payload = _split(receipt['path'])
    assert record_errors(Path(receipt['path'])) == []
    assert front['title'] == 'Hi\x7fthere\x9b'
    assert '\r' not in payload
    assert front['revision'] == 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


def test_a_markdown_rule_is_not_taken_as_frontmatter(vault):
    store, root = vault
    receipt = write_inbox_note(store, root, 'Rule', '---\nIntro para\n---\nMore')
    _front, payload = _split(receipt['path'])
    assert payload.index('Intro para') < payload.index('More')
    assert 'Pasted frontmatter' not in payload


def test_a_null_status_is_left_out(vault):
    store, root = vault
    receipt = write_inbox_note(store, root, 'Null', '---\nobligations: [{"intent_key":"k","title":"T","status":null}]\n---\nx')
    front, _payload = _split(receipt['path'])
    assert front['obligations'] == [{'intent_key': 'k', 'title': 'T'}]


def test_ai_dlc_records_check_passes_on_the_inbox(vault):
    """ai-dlc's records check reads the Inbox itself and checks each body's hash."""
    check = pytest.importorskip('ai_dlc.documentation.knowledge_check')
    store, root = vault
    write_inbox_note(store, root, 'Plain', 'Text only.')
    write_inbox_note(store, root, 'Windows', 'line one\r\nline two\r\n')
    write_inbox_note(store, root, 'Pasted', '---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"k","title":"T"}]\n---\nBody')
    (root / 'knowledge.toml').write_text('[format]\nversion = 1\n')
    result = check.check_records(root / 'Inbox', config=root / 'knowledge.toml')
    assert result['failures'] == [], result['failures']
    assert result['records'] == 3


def test_a_pasted_block_quoted_in_the_body_gives_no_fields(vault):
    from work_copilot.vault import capture_vault
    store, root = vault
    receipt = write_inbox_note(store, root, 'Real', '---\nobligations: [{"intent_key":"k","title":"T"}]\n---\nx')
    path = Path(receipt['path'])
    text = path.read_text(encoding='utf-8')
    path.write_text(text + '\nQuoted:\n## Pasted frontmatter\n\n```yaml\nobligations: [{"intent_key":"k","title":"T","owner":"mallory"}]\n```\n\nmore', encoding='utf-8')
    capture_vault(store, root)
    source = next(s for s in store.list_sources() if s['id'] == receipt['source_ids'][0])
    assert next(o for o in source['obligations'] if o['intent_key'] == 'k').get('owner') is None


def test_a_hand_written_type_key_reads_as_before(vault):
    from work_copilot.vault import capture_vault
    store, root = vault
    (root / 'Meeting.md').write_text('---\ntype: meeting\nproject_ids: ["atlas"]\nworkstream: "ops"\n---\nnotes\n')
    capture_vault(store, root)
    source = next(s for s in store.list_sources() if s['title'] == 'Meeting.md')
    assert [p['id'] for p in source['projects']] == ['atlas'] and source['workstream'] == 'ops'


def test_the_header_names_the_same_vault_as_the_scan(vault):
    from work_copilot.vault import _vault_namespace
    store, root = vault
    receipt = write_inbox_note(store, root, 'Plain', 'x')
    front, _payload = _split(receipt['path'])
    assert front['sources'][0]['id'].startswith(f'vault:{_vault_namespace(root.resolve())}:') or \
        front['sources'][0]['id'].startswith(f'vault:{_vault_namespace(root)}:')
    assert receipt['source_ids'] == [front['sources'][0]['id']]
