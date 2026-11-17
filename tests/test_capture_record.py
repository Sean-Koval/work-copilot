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
