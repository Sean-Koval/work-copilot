"""The ai-dlc session join key on receipts and tracked obligations (KAN-383)."""
import json
import sys
from datetime import datetime, timezone

import pytest

from work_copilot import cli
from work_copilot.hooks import drain_activity, enqueue_activity, ingest_activity
from work_copilot.join_key import KEY, join_fields
from work_copilot.knowledge import export_sources, parse_frontmatter
from work_copilot.store import Store

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
SESSION = {'AI_DLC_SESSION_KEY': 'aidlc1:KAN-463:kan-463-join:4f1c', 'AI_DLC_TRACKER_KEY': 'KAN-463',
           'AI_DLC_WORK_ID': 'kan-463-join', 'AI_DLC_SESSION_ID': '4f1c'}


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
    {'tracker': 'KAN 463'}, {'work_id': ''}, {'tracker': 'x' * 101}, {'session_key': 'aidlc2:a:b:c'},
    {'session_key': 'aidlc1:KAN-1:w:s', 'tracker': 'KAN-2'}, {'tracker': 463},
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

    (tmp_path / 'kb').mkdir()
    export_sources(store, tmp_path / 'kb', owner='sean', now=NOW)
    record = next((tmp_path / 'kb' / 'inbox').rglob('*.md')).read_text(encoding='utf-8')
    front = parse_frontmatter(record)
    assert {'type': 'belongs_to', 'target': 'KAN-463'} in front['links']
    lines = [line for line in record.splitlines() if line.startswith('Work-Session: ')]
    assert lines == ['Work-Session: ' + SESSION['AI_DLC_SESSION_KEY']]
    assert KEY.fullmatch(lines[0].split(': ', 1)[1])


def test_activity_without_a_session_carries_no_key(tmp_path):
    store = Store(tmp_path / 's.db')
    receipt = ingest_activity(store, event(), env={})
    assert 'session_key' not in receipt
    assert store.list_sources(provider='local-activity')[0]['join'] == {}


def test_activity_names_its_own_key(tmp_path):
    store = Store(tmp_path / 's.db')
    receipt = ingest_activity(store, event(tracker='KAN-7', work_id='w7'), env=SESSION)
    assert receipt['session_key'] == 'aidlc1:KAN-7:w7:'
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
