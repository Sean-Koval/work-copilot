"""KAN-390: capture only picked rooms and spaces; keep bodies for the retention limit, then a pointer."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from work_copilot import schema
from work_copilot.doctor import diagnose
from work_copilot.remote_mcp import CAPTURE_REQUIRED, _run_unlocked, load_config, select_captured
from work_copilot.store import Store

START = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SECRET = 'Quarterly numbers are confidential'


class Clock:
    def __init__(self):
        self.value = START

    def __call__(self):
        return self.value


def message(number, revision='1', body=SECRET, observed=START, **extra):
    return {'id': f'symphony:room-1:{number}', 'revision': revision, 'provider': 'symphony', 'title': f'Message {number}',
            'body': body, 'url': f'https://chat.example.invalid/m/{number}', 'room_id': 'room-1', 'observed_at': observed.isoformat(),
            'projects': [{'id': 'atlas', 'name': 'Atlas'}],
            'obligations': [{'intent_key': f'reply-{number}', 'title': 'Reply about the numbers', 'project_id': 'atlas', 'owner': 'me'}], **extra}


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    value = Store(tmp_path / 'copilot.db', now=clock)
    value.clock = clock
    yield value
    value.close()


def _dump(path):
    db = sqlite3.connect(path)
    try:
        return '\n'.join(db.iterdump())
    finally:
        db.close()


def test_an_expired_body_becomes_a_pointer(store, tmp_path):
    store.ingest([message(1)], connector='symphony')
    obligation = store.snapshot()['obligations'][0]
    store.clock.value = START + timedelta(days=29)
    assert store.apply_retention('symphony', 30)['expired_bodies'] == 0
    assert store.search('confidential')
    store.clock.value = START + timedelta(days=31)
    report = store.apply_retention('symphony', 30)
    assert report['expired_bodies'] == 1 and report['redacted_revisions'] == 1
    source = store.source('symphony:room-1:1')
    assert source['body'] == '' and source['title'] == 'Message 1' and source['url'].endswith('/m/1')
    assert source['retained']['retention_days'] == 30 and len(source['retained']['body_sha256']) == 64
    assert not store.search('confidential')
    kept = store.obligation(obligation['id'])
    assert [e['id'] for e in kept['evidence']] == ['symphony:room-1:1']
    store.close()
    assert SECRET not in _dump(tmp_path / 'copilot.db')
    reopened = Store(tmp_path / 'copilot.db', now=store.clock)
    assert reopened.ingest([message(1)], connector='symphony')['unchanged'] == 1
    assert reopened.source('symphony:room-1:1')['body'] == ''
    with pytest.raises(ValueError, match='collision'):
        reopened.ingest([message(1, body='Edited without a new revision')], connector='symphony')
    reopened.close()


def test_old_revisions_and_deleted_sources_keep_only_a_hash(store, tmp_path):
    store.ingest([message(1, body='First draft of the numbers')], connector='symphony')
    store.ingest([message(1, revision='2', observed=START + timedelta(hours=1))], connector='symphony')
    store.ingest([message(2), ], connector='symphony')
    store.ingest([message(2, revision='2', deleted=True, observed=START + timedelta(hours=1))], connector='symphony')
    report = store.apply_retention('symphony', 30)
    assert report == {'expired_bodies': 1, 'redacted_revisions': 3, 'backups_removed': 0}
    assert store.source('symphony:room-1:1')['body'] == SECRET
    store.close()
    dump = _dump(tmp_path / 'copilot.db')
    assert 'First draft' not in dump
    # Only the live message keeps its text: its row, its current revision and its search entry.
    assert all('symphony:room-1:1' in line for line in dump.splitlines() if SECRET in line)


def test_retention_touches_only_its_connector(store):
    store.ingest([message(1)], connector='vault:abc')
    store.clock.value = START + timedelta(days=90)
    assert store.apply_retention('symphony', 30)['expired_bodies'] == 0
    assert store.source('symphony:room-1:1')['body'] == SECRET


def test_retention_removes_backups_past_the_limit(store, tmp_path):
    folder = tmp_path / 'backups'
    folder.mkdir()
    old = folder / f'copilot-schema-v0-{(START - timedelta(days=8)).strftime("%Y%m%dT%H%M%S%fZ")}.db'
    new = folder / f'copilot-schema-v0-{(START - timedelta(days=2)).strftime("%Y%m%dT%H%M%S%fZ")}.db'
    old.write_bytes(b'x'); new.write_bytes(b'x')
    assert store.apply_retention('symphony', 7)['backups_removed'] == 1
    assert schema.backups(tmp_path / 'copilot.db') == [new]


def test_forget_removes_a_source_its_text_and_backups(store, tmp_path):
    store.ingest([message(1), message(2, body='Other text')], connector='symphony')
    (tmp_path / 'backups').mkdir()
    (tmp_path / 'backups' / 'copilot-schema-v0-20261101T120000000000Z.db').write_bytes(b'copy')
    obligation = next(o for o in store.snapshot()['obligations'] if o['intent_key'] == 'reply-1')
    report = store.forget('symphony:room-1:1')
    assert report == {'forgotten': 'symphony:room-1:1', 'stale_drafts': 0, 'backups_removed': 1}
    assert store.source('symphony:room-1:1') is None and not store.search('confidential')
    assert store.obligation(obligation['id'])['evidence'] == []
    assert store.ingest([message(1)], connector='symphony')['forgotten'] == 1
    assert store.source('symphony:room-1:1') is None
    with pytest.raises(KeyError):
        store.forget('symphony:room-1:unknown')
    store.close()
    assert SECRET not in _dump(tmp_path / 'copilot.db')


def test_only_picked_rooms_and_spaces_are_kept():
    items = [{'id': 'a', 'room_id': 'room-1'}, {'id': 'b', 'room_id': 'room-2'}, {'id': 'c', 'space_key': 'ENG'}, {'id': 'd'}]
    kept, skipped = select_captured(items, {'rooms': ['room-1'], 'spaces': ['ENG'], 'retention_days': 30})
    assert [item['id'] for item in kept] == ['a', 'c'] and skipped == 2


@pytest.mark.parametrize('capture,field', [
    ({'rooms': 'room-1'}, 'capture.rooms'),
    ({'spaces': ['']}, 'capture.spaces'),
    ({'rooms': ['r'], 'retention_days': 0}, 'retention_days'),
    ({'rooms': ['r'], 'retention_days': True}, 'retention_days'),
    ({'rooms': ['r'], 'keep_forever': True}, 'capture must'),
])
def test_capture_config_is_validated(tmp_path, capture, field):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'capture': capture}))
    with pytest.raises(ValueError, match=field):
        load_config(path)


def test_capture_defaults_to_thirty_days(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'capture': {'spaces': ['ENG']}}))
    assert load_config(path)['capture'] == {'rooms': [], 'spaces': ['ENG'], 'retention_days': 30}


async def test_collection_without_picked_rooms_is_refused_before_any_request(store, tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'tool': 't', 'allowed_read_tools': ['t'], 'capture': {'rooms': []}}))
    with pytest.raises(ValueError, match='capture.rooms'):
        await _run_unlocked(store, path, tmp_path, False, True)
    assert store.connectors() == []
    assert 'capture.spaces' in CAPTURE_REQUIRED


def test_doctor_shows_picked_rooms_and_retention(store, tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'tool': 't', 'allowed_read_tools': ['t'],
                                'capture': {'rooms': ['room-1'], 'spaces': ['ENG'], 'retention_days': 14}}))
    connector = diagnose(store, tmp_path, config=path)['checks']['connector']
    assert connector['capture'] == {'rooms': ['room-1'], 'spaces': ['ENG'], 'retention_days': 14}
    assert connector['collection_configured'] is True
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'tool': 't', 'allowed_read_tools': ['t']}))
    connector = diagnose(store, tmp_path, config=path)['checks']['connector']
    assert connector['collection_configured'] is False and connector['capture']['rooms'] == []
