"""KAN-390: capture only picked rooms and spaces; keep bodies for the retention limit, then a pointer."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from work_copilot import schema
from work_copilot.doctor import diagnose
from work_copilot.remote_mcp import CAPTURE_REQUIRED, load_config, remote_collect, select_captured
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
    assert {k: report[k] for k in ('expired_bodies', 'redacted_revisions', 'backups_removed', 'backups_left', 'wal_cleared')} == \
        {'expired_bodies': 1, 'redacted_revisions': 3, 'backups_removed': 0, 'backups_left': 0, 'wal_cleared': True}
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


def test_forget_removes_a_source_its_text_and_backups(store, tmp_path):
    store.ingest([message(1), message(2, body='Other text')], connector='symphony')
    backup = schema.backup(store.db, store.path, 'schema-v1')
    obligation = next(o for o in store.snapshot()['obligations'] if o['intent_key'] == 'reply-1')
    report = store.forget('symphony:room-1:1')
    assert report == {'forgotten': 'symphony:room-1:1', 'stale_drafts': 0, 'wal_cleared': True,
                      'backups_cleared': 1, 'backups_removed': 0, 'backups_left': 0}
    # The backup is kept as a rollback copy, without the forgotten source.
    assert backup.exists() and SECRET.encode() not in backup.read_bytes()
    assert 'Other text' in _dump(backup) and 'symphony:room-1:1' not in _dump(backup)
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
        await remote_collect(store, path, tmp_path)
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


def _raw(tmp_path):
    """The database file and its write-ahead log as bytes, read while the store is open."""
    return b''.join(path.read_bytes() for path in (tmp_path / 'copilot.db', tmp_path / 'copilot.db-wal') if path.exists())


def _real_backup(tmp_path, taken):
    folder = tmp_path / 'backups'
    folder.mkdir(exist_ok=True)
    path = folder / f'copilot-schema-v0-{taken.strftime("%Y%m%dT%H%M%S%fZ")}.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE t(body)')
    db.execute('INSERT INTO t VALUES(?)', (SECRET,))
    db.commit()
    db.close()
    return path


def test_expired_text_is_gone_from_the_file_and_its_log_while_open(store, tmp_path):
    store.ingest([message(1)], connector='symphony')
    assert SECRET.encode() in _raw(tmp_path)
    store.clock.value = START + timedelta(days=31)
    assert store.apply_retention('symphony', 30)['wal_cleared'] is True
    assert SECRET.encode() not in _raw(tmp_path)


def test_drafts_lose_quoted_text_on_expiry(store, tmp_path):
    store.ingest([message(1)], connector='symphony')
    obligation = store.snapshot()['obligations'][0]
    assert SECRET in store.draft(obligation['id'])['text']
    store.clock.value = START + timedelta(days=31)
    assert store.apply_retention('symphony', 30)['drafts_scrubbed'] == 1
    draft = store.drafts_for(obligation['id'])[0]
    assert SECRET not in draft['text'] and draft['stale'] is True
    assert all(not entry.get('body') for entry in draft['evidence'])
    assert 'expired' in store.draft(obligation['id'])['text']
    assert SECRET.encode() not in _raw(tmp_path)


def test_forget_removes_quoted_text_from_drafts(store, tmp_path):
    store.ingest([message(1)], connector='symphony')
    obligation = store.snapshot()['obligations'][0]
    store.draft(obligation['id'])
    assert store.forget('symphony:room-1:1')['stale_drafts'] == 1
    draft = store.drafts_for(obligation['id'])[0]
    assert SECRET not in json.dumps(draft) and draft['stale'] is True
    assert SECRET.encode() not in _raw(tmp_path)


def test_retention_runs_on_open_at_most_hourly(tmp_path):
    clock = Clock()
    store = Store(tmp_path / 'copilot.db', now=clock)
    store.ingest([message(1)], connector='symphony')
    store.set_retention('symphony', 30)
    store.close()
    clock.value = START + timedelta(days=31)
    reopened = Store(tmp_path / 'copilot.db', now=clock)
    assert reopened.retention_on_open['symphony']['expired_bodies'] == 1
    assert reopened.source('symphony:room-1:1')['body'] == ''
    reopened.close()
    again = Store(tmp_path / 'copilot.db', now=clock)
    assert again.retention_on_open == {}
    again.close()


def test_a_far_future_observed_at_still_expires(store):
    store.ingest([message(1, observed=START + timedelta(days=3650))], connector='symphony')
    store.clock.value = START + timedelta(days=31)
    assert store.apply_retention('symphony', 30)['expired_bodies'] == 1


def test_store_owned_keys_are_never_taken_from_an_item(store):
    store.ingest([message(1, retained={'expired_at': START.isoformat()}, captured_at=(START + timedelta(days=3650)).isoformat(),
                         redacted={'content_sha256': 'x'})], connector='symphony')
    source = store.source('symphony:room-1:1')
    assert 'retained' not in source and 'redacted' not in source and source['captured_at'] == START.isoformat()
    store.clock.value = START + timedelta(days=31)
    assert store.apply_retention('symphony', 30)['expired_bodies'] == 1


def test_known_sources_pass_the_room_filter():
    items = [{'id': 'old', 'deleted': True}, {'id': 'new'}]
    kept, skipped = select_captured(items, {'rooms': ['room-1'], 'spaces': [], 'retention_days': 30}, known=lambda sid: sid == 'old')
    assert [item['id'] for item in kept] == ['old'] and skipped == 1


def test_expiry_clears_text_inside_backups_and_keeps_them(store, tmp_path):
    store.ingest([message(1, body='First draft of the numbers'), message(2, body='Still current')], connector='symphony')
    store.ingest([message(1, revision='2', observed=START + timedelta(hours=1))], connector='symphony')
    store.clock.value = START + timedelta(hours=2)
    store.ingest([message(2, revision='2', body='Still current, edited', observed=START + timedelta(hours=2))], connector='symphony')
    obligation = next(o for o in store.snapshot()['obligations'] if o['intent_key'] == 'reply-1')
    store.draft(obligation['id'])
    # A pre-upgrade backup, taken while all of that text was live.
    backup = schema.backup(store.db, store.path, 'schema-v1')
    assert SECRET.encode() in backup.read_bytes() and b'First draft' in backup.read_bytes()
    store.ingest([message(1, revision='3', deleted=True, observed=START + timedelta(hours=3))], connector='symphony')
    report = store.apply_retention('symphony', 30)
    assert report['backups_cleared'] == 1 and report['backups_removed'] == 0 and report['backups_left'] == 0
    raw = backup.read_bytes()
    assert backup.exists() and SECRET.encode() not in raw and b'First draft' not in raw
    # The expired source and every replaced revision lose their text; the current text stays.
    assert b'Still current, edited' in raw and b'Still current' not in raw.replace(b'Still current, edited', b'')
    copy = sqlite3.connect(backup)
    try:
        body, retained = copy.execute("SELECT json_extract(payload,'$.body'),json_extract(payload,'$.retained') FROM sources WHERE id='symphony:room-1:1'").fetchone()
        assert body == '' and json.loads(retained)['retention_days'] == 30
        assert copy.execute("SELECT count(*) FROM local_metadata WHERE key='projections'").fetchone()[0] == 0
    finally:
        copy.close()


def test_a_young_backup_that_cannot_be_cleared_is_kept_and_reported(store, tmp_path):
    store.ingest([message(1)], connector='symphony')
    folder = tmp_path / 'backups'
    folder.mkdir()
    young = folder / f'copilot-schema-v0-{START.strftime("%Y%m%dT%H%M%S%fZ")}.db'
    old = folder / f'copilot-schema-v0-{(START - timedelta(days=8)).strftime("%Y%m%dT%H%M%S%fZ")}.db'
    young.write_bytes(b'not a database'); old.write_bytes(b'not a database')
    report = store.forget('symphony:room-1:1')
    # Under seven days old, a rollback copy is never deleted, even when it can't be cleared.
    assert report['backups_left'] == 1 and young.exists()
    assert report['backups_removed'] == 1 and not old.exists()
