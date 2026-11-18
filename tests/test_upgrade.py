"""A database written before KAN-392 opens and keeps its records (ids, identity, review state).

The legacy rows here are written the way the base branch wrote them: obligations keyed
``[project_id, intent_key]`` with the key as given (a bare kind, ``map:<sid>``,
``review:<key>``), review sources ``review/<key>`` and tracked sources ``local/obligation/...``.
"""
import hashlib
import json

import pytest

from work_copilot.remote_mcp import load_config, normalize_result
from work_copilot.store import Store, _id, _json
from work_copilot.vault import capture_vault


class _Ledger:
    """Collects what capture_vault would ingest, without a store."""
    def __init__(self):
        self.items = []
    def snapshot(self):
        return {'sources': [], 'projects': [], 'obligations': []}
    def ingest(self, items, connector='import', checkpoint=None):
        self.items = items
        return {}
    def set_connector(self, *args, **kwargs):
        pass


def _legacy_obligation(oid, candidate, now='2026-11-01T00:00:00+00:00', **extra):
    return {'id': oid, 'intent_key': candidate['intent_key'], 'title': candidate['title'], 'project_id': candidate.get('project_id'),
            'owner': candidate.get('owner'), 'status': candidate.get('status', 'candidate'), 'question_state': 'pending',
            'due_at': None, 'follow_up_at': None, 'waiting_on': candidate.get('waiting_on'), 'commitment': False,
            'revision': 1, 'created_at': now, 'reviewed': False, **extra}


def legacy_ingest(store, source, connector):
    """Write ``source`` and its obligations as the pre-KAN-392 ingest did."""
    source = {'schema_version': 1, 'observed_at': '2026-11-01T00:00:00+00:00', 'projects': [], 'obligations': [],
              'deleted': False, 'url': None, **source}
    db = store.db
    db.execute('INSERT INTO sources VALUES(?,?,?,?)', (source['id'], source['revision'], _json(source), connector))
    db.execute('INSERT INTO source_revisions VALUES(?,?,?)', (source['id'], source['revision'], _json(source)))
    for project in source['projects']:
        db.execute('INSERT OR IGNORE INTO projects VALUES(?,?)', (project['id'], project.get('name', project['id'])))
    ids = []
    for candidate in source['obligations']:
        canonical = _json([candidate.get('project_id'), candidate['intent_key']])
        oid = _id(canonical)
        db.execute('INSERT INTO obligations VALUES(?,?,?)', (oid, canonical, _json(_legacy_obligation(oid, candidate))))
        db.execute('INSERT INTO source_intents VALUES(?,?,?)', (source['id'], canonical, oid))
        db.execute('INSERT INTO evidence VALUES(?,?)', (oid, source['id']))
        ids.append(oid)
    db.commit()
    return ids


def legacy_vault(store, root):
    """Capture ``root`` as the base branch did: bare kinds and map:<sid> as given, the old extraction hash."""
    ledger = _Ledger()
    capture_vault(ledger, root)
    oids = {}
    for item in ledger.items:
        sid = item['id']
        legacy = []
        for ob in item['obligations']:
            kind = ob['intent_key'][len(sid) + 1:]
            legacy.append({**ob, 'intent_key': f'map:{sid}' if kind == 'map' else kind})
        extraction_hash = hashlib.sha256(json.dumps([]).encode()).hexdigest()
        revision = hashlib.sha256((item['content_hash'] + extraction_hash).encode()).hexdigest()
        item = {**item, 'obligations': legacy, 'extraction_hash': extraction_hash, 'revision': revision}
        oids[item['title']] = legacy_ingest(store, item, item['id'].rsplit(':', 1)[0])
    return oids


def reopen(store):
    path, now = store.path, store._clock
    store.close()
    return Store(path, now=now)


def obligation_ids(store):
    return sorted(row[0] for row in store.db.execute('SELECT id FROM obligations'))


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'db.sqlite3')
    yield value
    value.close()


def test_recapturing_an_unchanged_vault_note_after_upgrade_keeps_its_obligations(tmp_path, store):
    root = (tmp_path / 'vault').resolve()
    root.mkdir()
    (root / 'Mapped.md').write_text('---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"release-confirmation","title":"Confirm release","project_id":"atlas","owner":null,"status":"candidate"}]\n---\n# Mapped\n')
    (root / 'Unmapped.md').write_text('# Note\nSomeone asks about the release.\n')
    oids = legacy_vault(store, root)
    store = reopen(store)
    before = obligation_ids(store)
    dismissed = oids['Mapped.md'][0]
    store.review(dismissed, 1, {'status': 'dismissed'})

    result = capture_vault(store, root)
    assert result['errors'] == []
    assert result['result'] == {'inserted': 0, 'updated': 2, 'unchanged': 0}
    # The notes' obligations are the ones stored before, with their review state.
    assert obligation_ids(store) == before
    assert store._obligation(dismissed)['status'] == 'dismissed'
    for title, (oid,) in oids.items():
        assert [s['title'] for s in store._obligation(oid)['evidence']] == [title]
    # Capturing again is replay.
    assert capture_vault(store, root)['result'] == {'inserted': 0, 'updated': 0, 'unchanged': 2}
    assert obligation_ids(store) == before
    store.close()


def test_review_queue_adopts_items_stored_before_the_upgrade(store):
    item = {'key': 'pr:o/r#1', 'title': 'PR 1', 'body': 'b', 'revision': '1', 'opened_at': '2026-11-01T00:00:00+00:00'}
    other = {**item, 'key': 'pr:o/r#2', 'title': 'PR 2'}
    oids = {}
    for entry in (item, other):
        sid = 'review/' + entry['key']
        candidate = {'intent_key': 'review:' + entry['key'], 'title': entry['title'], 'owner': 'me', 'status': 'open'}
        canonical = _json([None, candidate['intent_key']])
        oid = _id(canonical)
        legacy_ingest(store, {'id': sid, 'revision': '1', 'provider': 'knowledge-review', 'title': entry['title'], 'body': 'b'}, 'knowledge-review')
        store.db.execute('INSERT INTO obligations VALUES(?,?,?)', (oid, canonical, _json(_legacy_obligation(oid, candidate, kind='review', opened_at=entry['opened_at']))))
        store.db.execute('INSERT INTO evidence VALUES(?,?)', (oid, sid))
        store.db.commit()
        oids[entry['key']] = oid
    store = reopen(store)
    store.review(oids['pr:o/r#1'], 1, {'status': 'dismissed'})

    result = store.sync_review_queue([item, other])
    assert result['opened'] == [] and result['resolved'] == []
    assert obligation_ids(store) == sorted(oids.values())
    # A dismissed item stays dismissed; an open one is not duplicated.
    assert store._obligation(oids['pr:o/r#1'])['status'] == 'dismissed'
    assert [o['id'] for o in store.snapshot()['review_queue']] == [oids['pr:o/r#2']]
    # Its evidence is the current source, not the old copy as well.
    assert [s['id'] for s in store._obligation(oids['pr:o/r#2'])['evidence']] == ['knowledge-review:local:pr:o/r#2']
    # An item that is gone resolves, and comes back as the same record.
    assert store.sync_review_queue([item])['resolved'] == [oids['pr:o/r#2']]
    assert store.sync_review_queue([item, other])['reopened'] == [oids['pr:o/r#2']]
    assert obligation_ids(store) == sorted(oids.values())
    store.close()


@pytest.mark.parametrize('project', ['atlas', None])
def test_tracking_a_key_tracked_before_the_upgrade_returns_that_record(store, project):
    candidate = {'intent_key': 'atlas-transcript', 'title': 'Get transcript', 'project_id': project, 'owner': 'me', 'status': 'waiting'}
    canonical = _json([project, 'atlas-transcript'])
    (oid,) = legacy_ingest(store, {'id': 'local/obligation/' + _id(canonical), 'revision': '1', 'provider': 'local-user', 'title': 'User-entered obligation: Get transcript',
                                   'body': 'Explicitly entered by the local user. Get transcript', 'obligations': [candidate],
                                   'projects': [{'id': project, 'name': project}] if project else []}, 'local-user')
    store = reopen(store)
    again = store.track_obligation({'intent_key': 'atlas-transcript', 'title': 'Get transcript', 'project_id': project, 'status': 'waiting'})
    assert again['id'] == oid
    assert again.get('review_reason') is None
    assert obligation_ids(store) == [oid]
    store.close()


def test_tracking_a_bare_kind_for_another_project_flags_it_for_review(store):
    store.create_project('beta', 'Beta')
    first = store.track_obligation({'intent_key': 'atlas-transcript', 'title': 'Get transcript', 'project_id': 'atlas'})
    same = store.track_obligation({'intent_key': 'atlas-transcript', 'title': 'Get transcript', 'project_id': 'atlas'})
    assert same['id'] == first['id'] and same['revision'] == first['revision'] and not same.get('review_reason')
    # No project given means no claim about the project.
    assert not store.track_obligation({'intent_key': 'atlas-transcript', 'title': 'Get transcript'}).get('review_reason')
    other = store.track_obligation({'intent_key': 'atlas-transcript', 'title': 'Get transcript', 'project_id': 'beta'})
    assert other['id'] == first['id']
    assert other['project_id'] == 'atlas'
    assert 'beta' in other['review_reason']
    assert other['revision'] == first['revision'] + 1
    assert other['id'] in {o['id'] for o in store.snapshot()['review']}


def test_ingest_flags_an_ask_met_again_under_another_project(store):
    key = 'jira:a.atlassian.net:ATLAS-12:reply'
    def source(sid, project):
        return {'id': sid, 'revision': '1', 'provider': 'jira', 'title': 't', 'body': 'b',
                'obligations': [{'intent_key': key, 'title': 'Reply', 'project_id': project, 'owner': 'me'}]}
    store.ingest([source('jira:a.atlassian.net:ATLAS-12', 'atlas')])
    store.ingest([source('jira:a.atlassian.net:ATLAS-13', 'beta')])
    (ob,) = store.snapshot()['obligations']
    assert ob['project_id'] == 'atlas'
    assert 'project_id' in ob['review_reason']


def test_id_prefix_completes_bare_ask_kinds_and_is_not_applied_twice(store):
    mapping = {'constants': {'provider': 'symphony'}, 'id_prefix': 'symphony:acme-tenant:'}
    full = 'jira:a.atlassian.net:ATLAS-12:reply'
    payload = {'items': [
        {'id': 'room-7/msg-42', 'provider': 'symphony', 'revision': '1', 'title': 'Q', 'body': 'Ready?',
         'obligations': [{'intent_key': 'reply', 'title': 'Reply'}, {'intent_key': full, 'title': 'Reply on Jira'}]},
        {'id': 'symphony:acme-tenant:room-7/msg-43', 'provider': 'symphony', 'revision': '1', 'title': 'Q2', 'body': 'Ready too?'},
    ]}
    items, _cursor, _checkpoint = normalize_result(payload, mapping)
    assert [item['id'] for item in items] == ['symphony:acme-tenant:room-7/msg-42', 'symphony:acme-tenant:room-7/msg-43']
    assert [o['intent_key'] for o in items[0]['obligations']] == ['symphony:acme-tenant:room-7/msg-42:reply', full]
    assert store.ingest(items)['inserted'] == 2
    assert sorted(o['intent_key'] for o in store.snapshot()['obligations']) == [full, 'symphony:acme-tenant:room-7/msg-42:reply']


def test_load_config_refuses_an_id_prefix_for_another_provider(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring',
                                'mapping': {'constants': {'provider': 'symphony'}, 'id_prefix': 'jira:a.atlassian.net:'}}))
    with pytest.raises(ValueError, match='constants.provider'):
        load_config(path)
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring',
                                'mapping': {'constants': {'provider': 'symphony'}, 'id_prefix': 'symphony:acme:'}}))
    assert load_config(path)['mapping']['id_prefix'] == 'symphony:acme:'
