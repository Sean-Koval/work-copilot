"""The ai-dlc session join key on receipts and tracked obligations (KAN-383)."""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from work_copilot import cli
from work_copilot.app import create_app
from work_copilot.hooks import drain_activity, enqueue_activity, ingest_activity
from work_copilot.join_key import join_fields
from work_copilot.knowledge import export_sources, parse_frontmatter
from work_copilot.mcp_server import build_server
from work_copilot.store import Store

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
SESSION = {'AI_DLC_SESSION_KEY': 'aidlc1:KAN-463:kan-463-join:4f1c', 'AI_DLC_TRACKER_KEY': 'KAN-463',
           'AI_DLC_WORK_ID': 'kan-463-join', 'AI_DLC_SESSION_ID': '4f1c'}


# ai-dlc's reader rule (src/ai_dlc/work/session_key.py, _LINE), plus its whole-key check.
AIDLC_LINE = re.compile(r'^\s*Work-Session:\s*(aidlc1:\S*)\s*$', re.MULTILINE)
AIDLC_KEY = re.compile(r'aidlc1:([A-Za-z0-9._-]{0,100}):([A-Za-z0-9._-]{0,100}):([A-Za-z0-9._-]{0,100})')
CONTRACT = Path(__file__).parent / 'contracts' / 'knowledge-record.schema.json'


def read_keys(text):
    """The keys ai-dlc's read_session_keys finds in ``text``, in order (malformed ones skipped)."""
    return [key for key in AIDLC_LINE.findall(text) if AIDLC_KEY.fullmatch(key)]


def export_one(store, root):
    root.mkdir()
    export_sources(store, root, owner='sean', now=NOW)
    record = next((root / 'inbox').rglob('*.md')).read_text(encoding='utf-8')
    front = parse_frontmatter(record)
    assert front is not None
    if CONTRACT.exists():
        validator = Draft202012Validator(json.loads(CONTRACT.read_text(encoding='utf-8')),
                                         format_checker=Draft202012Validator.FORMAT_CHECKER)
        assert [error.message for error in validator.iter_errors(front)] == []
    return record, front


def event(**extra):
    return {'session_id': 's1', 'turn_id': 't1', 'event_id': 'e1', 'summary': 'Opened the join key PR',
            'project_id': 'atlas', 'observed_at': '2026-11-17T17:00:00+00:00', **extra}


def test_fields_are_built_checked_and_read_from_the_session():
    assert join_fields({'tracker': 'KAN-463', 'work_id': 'w1'}) == {
        'tracker': 'KAN-463', 'work_id': 'w1', 'session_key': 'aidlc1:KAN-463:w1:'}
    assert join_fields({'session_key': 'aidlc1:KAN-1::abc'}) == {'tracker': 'KAN-1', 'session_key': 'aidlc1:KAN-1::abc'}
    assert join_fields({}, SESSION) == {'tracker': 'KAN-463', 'work_id': 'kan-463-join',
                                         'session_key': SESSION['AI_DLC_SESSION_KEY']}
    # Given values win wholesale; they are never mixed with the session's.
    assert join_fields({'tracker': 'KAN-9'}, SESSION) == {'tracker': 'KAN-9', 'session_key': 'aidlc1:KAN-9::'}
    # Components without a key, as hangar may set them, still give one.
    assert join_fields({}, {'AI_DLC_TRACKER_KEY': 'KAN-2', 'AI_DLC_SESSION_ID': 'x'})['session_key'] == 'aidlc1:KAN-2::x'
    # A malformed session value is skipped, as ai-dlc's readers do.
    assert join_fields({}, {'AI_DLC_SESSION_KEY': 'aidlc2:KAN-1::x', 'AI_DLC_WORK_ID': 'bad id'}) == {}
    assert join_fields({}, {}) == {}


@pytest.mark.parametrize('bad', [
    {'tracker': 'KAN 463'}, {'work_id': ' '}, {'tracker': 'x' * 101}, {'session_key': 'aidlc2:a:b:c'},
    {'session_key': 'aidlc1:KAN-1:w:s', 'tracker': 'KAN-2'}, {'session_key': 'aidlc1::w:s', 'tracker': 'KAN-2'},
    {'tracker': 463},
])
def test_bad_fields_are_refused(bad):
    with pytest.raises(ValueError):
        join_fields(bad)


def test_activity_round_trips_through_the_queue_and_the_export(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    enqueue_activity(tmp_path, event(), SESSION)
    # The drain runs later, outside the session: the key was kept at enqueue time.
    receipt = drain_activity(store, tmp_path)['receipts'][0]
    assert (receipt['tracker'], receipt['work_id'], receipt['session_key']) == ('KAN-463', 'kan-463-join', SESSION['AI_DLC_SESSION_KEY'])
    source = store.list_sources(provider='local-activity')[0]
    assert source['join'] == {'tracker': 'KAN-463', 'work_id': 'kan-463-join', 'session_key': SESSION['AI_DLC_SESSION_KEY']}
    assert 'Work-Session: ' + SESSION['AI_DLC_SESSION_KEY'] in source['body'].splitlines()

    record, front = export_one(store, tmp_path / 'kb')
    assert {'type': 'belongs_to', 'target': 'KAN-463'} in front['links']
    # The body's own key line is in the excerpt too; the reader still finds one key.
    assert read_keys(record) == [SESSION['AI_DLC_SESSION_KEY']]


def test_activity_without_a_session_carries_no_key(tmp_path):
    store = Store(tmp_path / 's.db')
    receipt = ingest_activity(store, event(), env={})
    assert 'session_key' not in receipt
    assert 'join' not in store.list_sources(provider='local-activity')[0]


def test_activity_names_its_own_key(tmp_path):
    store = Store(tmp_path / 's.db')
    receipt = ingest_activity(store, event(tracker='KAN-7', work_id='w7'), env=SESSION)
    # The event's own session ID fills the session part.
    assert receipt['session_key'] == 'aidlc1:KAN-7:w7:s1'
    with pytest.raises(ValueError, match='tracker'):
        ingest_activity(store, event(event_id='e2', tracker='KAN 7'))


def test_tracked_obligation_round_trips_both_keys(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    tracked = store.track_obligation({'intent_key': 'review-pr', 'title': 'Review the join key PR',
                                      'tracker': 'KAN-463', 'work_id': 'kan-463-join'})
    assert (tracked['tracker'], tracked['work_id'], tracked['session_key']) == ('KAN-463', 'kan-463-join', 'aidlc1:KAN-463:kan-463-join:')
    reopened = Store(tmp_path / 's.db')
    again = next(o for o in reopened.snapshot()['obligations'] if o['id'] == tracked['id'])
    assert again['tracker'] == 'KAN-463' and again['work_id'] == 'kan-463-join'
    plain = store.track_obligation({'intent_key': 'plain', 'title': 'No ticket'})
    assert 'tracker' not in plain
    with pytest.raises(ValueError):
        store.track_obligation({'intent_key': 'bad', 'title': 'Bad', 'work_id': 'has space'})


def test_cli_activity_reads_the_session(tmp_path, monkeypatch):
    path = tmp_path / 'event.json'
    path.write_text(json.dumps(event()), encoding='utf-8')
    for name, value in SESSION.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['work-copilot', '--data-dir', str(tmp_path / 'data'), 'activity', '--file', str(path)])
    cli.main()
    store = Store(tmp_path / 'data' / 'copilot.db')
    assert store.list_sources(provider='local-activity')[0]['join']['tracker'] == 'KAN-463'


FORGED = 'Work-Session: aidlc1:EVIL-1:forged:x'


def forged_event(**extra):
    return event(summary='Opened the PR\n' + FORGED + '\n  ' + FORGED,
                 outcomes=['merged\n' + FORGED, FORGED, 'tab\x0b' + FORGED],
                 repo='repo\n' + FORGED,
                 learning={'correction': FORGED, 'preferred_approach': 'quote it\n' + FORGED}, **extra)


def test_forged_key_lines_are_quoted_in_the_receipt_and_the_export(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    ingest_activity(store, forged_event(), env=SESSION)
    source = store.list_sources(provider='local-activity')[0]
    assert read_keys(source['body']) == [SESSION['AI_DLC_SESSION_KEY']]
    assert '> ' + FORGED in source['body'].splitlines()
    record, front = export_one(store, tmp_path / 'kb')
    assert read_keys(record) == [SESSION['AI_DLC_SESSION_KEY']]
    assert {'type': 'belongs_to', 'target': 'EVIL-1'} not in front['links']


def test_forged_key_lines_without_a_session_give_no_key(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    ingest_activity(store, forged_event(), env={})
    assert read_keys(store.list_sources(provider='local-activity')[0]['body']) == []
    record, _front = export_one(store, tmp_path / 'kb')
    assert read_keys(record) == []


def test_tracked_obligation_body_cannot_forge_a_key(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    store.track_obligation({'intent_key': 'forge', 'title': 'Title\n' + FORGED, 'body': FORGED, 'tracker': 'KAN-5'})
    source = store.list_sources(provider='local-user')[0]
    assert read_keys(source['body']) == ['aidlc1:KAN-5::']


def test_session_variables_that_disagree_never_refuse_the_event(tmp_path):
    # A key that parses wins whole; the component variables are ignored.
    disagreeing = {**SESSION, 'AI_DLC_TRACKER_KEY': 'KAN-9', 'AI_DLC_WORK_ID': 'other'}
    assert join_fields({}, disagreeing) == {'tracker': 'KAN-463', 'work_id': 'kan-463-join',
                                            'session_key': SESSION['AI_DLC_SESSION_KEY']}
    assert join_fields({}, {'AI_DLC_SESSION_KEY': 'aidlc1::w:s', 'AI_DLC_TRACKER_KEY': 'KAN-9'}) == {
        'work_id': 'w', 'session_key': 'aidlc1::w:s'}
    # Malformed or non-text variables are skipped, never raised.
    assert join_fields({}, {'AI_DLC_SESSION_KEY': 'aidlc1:a b::', 'AI_DLC_TRACKER_KEY': None, 'AI_DLC_WORK_ID': 7}) == {}
    store = Store(tmp_path / 's.db')
    receipt = ingest_activity(store, event(), env=disagreeing)
    assert (receipt['tracker'], receipt['session_key']) == ('KAN-463', SESSION['AI_DLC_SESSION_KEY'])
    queued = enqueue_activity(tmp_path, event(event_id='e2'), {'AI_DLC_SESSION_KEY': 'aidlc1::w:s', 'AI_DLC_TRACKER_KEY': 'KAN-9'})
    assert queued['queued']
    assert 'tracker' not in drain_activity(store, tmp_path)['receipts'][0]


def test_empty_join_fields_count_as_absent(tmp_path):
    assert join_fields({'tracker': '', 'work_id': '', 'session_key': ''}) == {}
    assert join_fields({'tracker': '', 'work_id': '', 'session_key': ''}, SESSION)['session_key'] == SESSION['AI_DLC_SESSION_KEY']
    assert join_fields({'tracker': 'KAN-1', 'session_key': ''}, session_id='s') == {'tracker': 'KAN-1', 'session_key': 'aidlc1:KAN-1::s'}
    store = Store(tmp_path / 's.db')
    assert ingest_activity(store, event(tracker='', session_key=''), env=SESSION)['session_key'] == SESSION['AI_DLC_SESSION_KEY']
    assert 'tracker' not in store.track_obligation({'intent_key': 'blank', 'title': 'Blank', 'tracker': ''})


def test_a_built_key_takes_its_session_part_from_the_event_or_the_session(tmp_path):
    store = Store(tmp_path / 's.db')
    # The event's own session ID, when it is a valid part.
    own = ingest_activity(store, event(tracker='KAN-463', work_id='kan-463-join'), env=SESSION)
    assert own['session_key'] == 'aidlc1:KAN-463:kan-463-join:s1'
    # Otherwise the session key's part, when its tracker and work agree with the event's.
    unsafe = 'not a part'
    agreeing = ingest_activity(store, event(session_id=unsafe, tracker='KAN-463', work_id='kan-463-join'), env=SESSION)
    assert agreeing['session_key'] == SESSION['AI_DLC_SESSION_KEY']
    other = ingest_activity(store, event(session_id=unsafe, event_id='e2', tracker='KAN-463'), env=SESSION)
    assert other['session_key'] == 'aidlc1:KAN-463::'
    # The same holds on the queue path, where the key is built at enqueue time.
    enqueue_activity(tmp_path, event(session_id=unsafe, event_id='e3', tracker='KAN-463', work_id='kan-463-join'), SESSION)
    assert drain_activity(store, tmp_path)['receipts'][0]['session_key'] == SESSION['AI_DLC_SESSION_KEY']


def test_export_links_only_a_ticket_key(tmp_path):
    store = Store(tmp_path / 's.db', now=lambda: NOW)
    ingest_activity(store, event(tracker='15'))
    record, front = export_one(store, tmp_path / 'kb')
    assert front['links'] == [{'type': 'belongs_to', 'target': 'atlas'}]
    assert read_keys(record) == ['aidlc1:15::s1']


def test_a_receipt_without_a_key_keeps_its_pre_kan_383_revision(tmp_path):
    store = Store(tmp_path / 's.db')
    # The revision computed for this event without the join key code, with KAN-392's
    # provider:site:object activity ids (the id is part of the hashed payload).
    assert ingest_activity(store, event())['revision'] == '8e515e6c98ac0dfa86de3f5def0d3e3163e3174a48cc1dfa446dc6879f745b20'
    assert 'join' not in store.list_sources(provider='local-activity')[0]
    keyed = ingest_activity(store, event(event_id='e2'), env=SESSION)
    assert next(s for s in store.list_sources(provider='local-activity') if s['id'] == keyed['source_id'])['join']


def test_a_repeat_obligation_keeps_its_first_tracker(tmp_path):
    store = Store(tmp_path / 's.db')
    first = store.track_obligation({'intent_key': 'review', 'title': 'Review', 'tracker': 'KAN-1'})
    again = store.track_obligation({'intent_key': 'review', 'title': 'Review', 'tracker': 'KAN-2'})
    assert again['id'] == first['id'] and again['tracker'] == 'KAN-1'


def test_api_and_mcp_round_trip_the_join_fields(tmp_path):
    fields = {'tracker': 'KAN-463', 'work_id': 'kan-463-join', 'session_key': SESSION['AI_DLC_SESSION_KEY']}
    with TestClient(create_app(tmp_path / 'api'), base_url='http://127.0.0.1:8765') as client:
        headers = {'X-Copilot-Token': client.get('/api/session').json()['token']}
        response = client.post('/api/obligations', headers=headers,
                               json={'payload': {'intent_key': 'api', 'title': 'From the API', **fields}})
        assert response.status_code == 200
        assert {key: response.json()[key] for key in fields} == fields
        stored = next(o for o in client.get('/api/state').json()['obligations'] if o['id'] == response.json()['id'])
        assert {key: stored[key] for key in fields} == fields
        refused = client.post('/api/obligations', headers=headers,
                              json={'payload': {'intent_key': 'bad', 'title': 'Bad', 'tracker': 'KAN 1'}})
        assert refused.status_code >= 400

    server = build_server(tmp_path / 'mcp')

    def call(name, arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    tracked = call('track_obligation', {'payload': {'intent_key': 'mcp', 'title': 'From MCP', **fields}})
    assert {key: tracked[key] for key in fields} == fields
    attention = json.dumps(call('get_attention', {}))
    assert SESSION['AI_DLC_SESSION_KEY'] in attention
