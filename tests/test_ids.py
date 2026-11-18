"""Source ids are provider:site:object and intent keys are <source id>:<ask kind> (KAN-392)."""
import json

import pytest

from work_copilot.ids import ask_kind, intent_key, parse_intent_key, parse_source_id, tracked_intent_key
from work_copilot.remote_mcp import load_config, normalize_result
from work_copilot.store import Store
from work_copilot.vault import capture_vault

SITE_A = 'jira:a.atlassian.net:ATLAS-12'
SITE_B = 'jira:b.atlassian.net:ATLAS-12'


def jira(source_id, revision='1', **kw):
    return {'id': source_id, 'revision': revision, 'provider': 'jira', 'title': 'ATLAS-12 · pilot scope',
            'body': 'Can you confirm the pilot scope?', 'projects': [{'id': 'atlas', 'name': 'Atlas'}],
            'obligations': [{'intent_key': source_id + ':reply', 'title': 'Reply on ATLAS-12', 'project_id': 'atlas', 'owner': 'me'}], **kw}


def counts(store):
    return {table: store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('sources', 'source_revisions', 'obligations', 'source_intents', 'evidence', 'projects')}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 's.db')
    yield value
    value.close()


def test_the_same_issue_key_on_two_jira_sites_is_two_sources_and_two_obligations(store):
    store.ingest([jira(SITE_A), jira(SITE_B)])
    assert sorted(s['id'] for s in store.list_sources('jira')) == [SITE_A, SITE_B]
    obligations = store.snapshot()['obligations']
    assert sorted(o['intent_key'] for o in obligations) == [SITE_A + ':reply', SITE_B + ':reply']
    assert len({o['id'] for o in obligations}) == 2
    for ob in obligations:
        assert [s['id'] for s in ob['evidence']] == [parse_intent_key(ob['intent_key'])[0]]


def test_a_second_sweep_a_new_revision_and_a_second_mapping_create_no_new_obligations(store):
    store.create_project('beacon', 'Beacon')
    first = store.ingest([jira(SITE_A), jira(SITE_B)])
    assert first['inserted'] == 2
    ids = sorted(o['id'] for o in store.snapshot()['obligations'])
    # The same sweep again.
    assert store.ingest([jira(SITE_A), jira(SITE_B)])['unchanged'] == 2
    # A new revision of the same issue, now mapped to another project as well.
    revised = jira(SITE_A, revision='2', body='Still waiting on scope.',
                   projects=[{'id': 'atlas', 'name': 'Atlas'}, {'id': 'beacon', 'name': 'Beacon'}])
    assert store.ingest([revised])['updated'] == 1
    # The same ask arriving from a second source mapped to another project.
    room = {'id': 'symphony:acme-tenant:room/delivery/7', 'revision': '1', 'provider': 'symphony', 'title': 'Delivery room',
            'body': 'Same ATLAS-12 question.', 'projects': [{'id': 'beacon', 'name': 'Beacon'}],
            'obligations': [{'intent_key': SITE_A + ':reply', 'title': 'Reply on ATLAS-12', 'project_id': 'beacon', 'owner': 'me'}]}
    store.ingest([room])
    store.ingest([room])
    obligations = store.snapshot()['obligations']
    assert sorted(o['id'] for o in obligations) == ids
    merged = next(o for o in obligations if o['intent_key'] == SITE_A + ':reply')
    assert {s['id'] for s in merged['evidence']} == {SITE_A, room['id']}
    assert merged['project_id'] == 'atlas'


@pytest.mark.parametrize('bad_id', [
    'jira:ATLAS-12',                       # no site
    'Jira:a.atlassian.net:ATLAS-12',       # upper-case provider
    'jira:a.atlassian.net:ATLAS\n12',      # control character
    'jira:a.atlassian.net:ATLAS\x0012',    # NUL
    'jira:a.atlassian.net: ATLAS-12',      # leading space in the object
    'jira:a.atlassian.net:ATLAS-12 ',      # trailing space
    ' jira:a.atlassian.net:ATLAS-12',      # leading space before the provider
    'jira:a.atlassian.net:',               # empty object
    'jira::ATLAS-12',                      # empty site
    'ATLAS-12',
])
def test_invalid_source_ids_are_refused_and_nothing_is_written(store, bad_id):
    store.ingest([jira(SITE_A)])
    before = counts(store)
    item = jira(SITE_B)
    item['id'] = bad_id
    with pytest.raises(ValueError, match='Source id'):
        store.ingest([jira(SITE_B), item])
    assert counts(store) == before
    with pytest.raises(ValueError):
        parse_source_id(bad_id)


@pytest.mark.parametrize('bad_key', [
    'reply',                                   # bare ask kind on ingest
    SITE_B + ':Reply',                         # upper-case ask kind
    SITE_B + ':',                              # empty ask kind
    'jira:ATLAS-12:reply',                     # the source part has no site
    'Jira:b.atlassian.net:ATLAS-12:reply',     # the source part has an upper-case provider
    SITE_B + ':reply ',
])
def test_invalid_intent_keys_are_refused_and_nothing_is_written(store, bad_key):
    store.ingest([jira(SITE_A)])
    before = counts(store)
    item = jira(SITE_B)
    item['obligations'][0]['intent_key'] = bad_key
    with pytest.raises(ValueError, match='Intent key'):
        store.ingest([jira('jira:b.atlassian.net:ATLAS-13'), item])
    assert counts(store) == before
    with pytest.raises(ValueError):
        parse_intent_key(bad_key)


def test_a_source_id_from_another_provider_is_refused(store):
    store.ingest([jira(SITE_A)])
    before = counts(store)
    with pytest.raises(ValueError, match='does not match'):
        store.ingest([jira('confluence:a.atlassian.net:123456')])
    assert counts(store) == before


def test_intent_key_helpers():
    assert parse_intent_key(SITE_A + ':reply') == (SITE_A, 'reply')
    # An object may contain colons; the ask kind is the last part.
    assert parse_intent_key('symphony:acme:thread:42:release-readiness') == ('symphony:acme:thread:42', 'release-readiness')
    assert intent_key(SITE_A, 'reply') == SITE_A + ':reply'
    with pytest.raises(ValueError):
        intent_key(SITE_A, 'Reply')
    with pytest.raises(ValueError):
        intent_key('ATLAS-12', 'reply')
    assert ask_kind(SITE_A + ':reply') == 'reply'
    assert ask_kind('legacy free-form key') == 'legacy free-form key'
    assert tracked_intent_key('atlas-pilot-scope') == 'local-user:local:tracked:atlas-pilot-scope'
    assert tracked_intent_key(SITE_A + ':reply') == SITE_A + ':reply'


def test_track_obligation_takes_a_bare_kind_and_dedupes_it(store):
    first = store.track_obligation({'intent_key': 'atlas-pilot-scope', 'title': 'Pilot scope'})
    assert first['intent_key'] == 'local-user:local:tracked:atlas-pilot-scope'
    assert parse_source_id(first['evidence'][0]['id'])[0] == 'local-user'
    again = store.track_obligation({'intent_key': 'atlas-pilot-scope', 'title': 'Pilot scope, again'})
    assert again['id'] == first['id'] and len(store.snapshot()['obligations']) == 1
    # A full intent key is kept, and it joins the ask a source already carries.
    store.ingest([jira(SITE_A)])
    joined = store.track_obligation({'intent_key': SITE_A + ':reply', 'title': 'Reply on ATLAS-12'})
    assert joined['intent_key'] == SITE_A + ':reply' and len(store.snapshot()['obligations']) == 2
    before = counts(store)
    for bad in ('Pilot Scope', '1-pilot', 'jira:ATLAS-12:reply', ''):
        with pytest.raises(ValueError):
            store.track_obligation({'intent_key': bad, 'title': 'Bad'})
    assert counts(store) == before


def test_records_stored_before_the_change_keep_their_identity(store):
    store.create_project('atlas', 'Atlas')
    store.ingest([jira(SITE_A, projects=[], obligations=[{'intent_key': SITE_A + ':reply', 'title': 'Reply', 'owner': None}])])
    ob = store.snapshot()['obligations'][0]
    # Rewrite it as a row stored before KAN-392: a free-form key and a (project, key) canonical.
    payload = dict(store._obligation(ob['id']), intent_key='pilot-scope')
    payload.pop('evidence', None)
    payload.pop('freshness', None)
    store.db.execute('UPDATE obligations SET canonical=?, payload=? WHERE id=?', (json.dumps([None, 'pilot-scope']), json.dumps(payload), ob['id']))
    store.db.commit()
    store.review(ob['id'], ob['revision'], {'project_id': 'atlas'})
    assert store.db.execute('SELECT canonical FROM obligations WHERE id=?', (ob['id'],)).fetchone()[0] == json.dumps(['atlas', 'pilot-scope'], sort_keys=True)
    # A current key is its own identity whatever the project.
    store.ingest([jira(SITE_B, projects=[], obligations=[{'intent_key': SITE_B + ':reply', 'title': 'Reply', 'owner': None}])])
    current = next(o for o in store.snapshot()['obligations'] if o['intent_key'] == SITE_B + ':reply')
    store.review(current['id'], current['revision'], {'project_id': 'atlas'})
    assert store.db.execute('SELECT canonical FROM obligations WHERE id=?', (current['id'],)).fetchone()[0] == SITE_B + ':reply'


def test_remote_mapping_id_prefix_is_applied_and_ingests(store):
    mapping = {'items_path': 'messages', 'fields': {'id': 'messageId', 'title': 'subject', 'body': 'text', 'revision': 'version'},
               'constants': {'provider': 'symphony'}, 'id_prefix': 'symphony:acme-tenant:'}
    payload = {'messages': [{'messageId': 'room/7/msg/1', 'subject': 'Question', 'text': 'Ready?', 'version': '1'},
                            {'messageId': 42, 'subject': 'Numbered', 'text': 'Ready too?', 'version': '1'}]}
    items, _cursor, _checkpoint = normalize_result(payload, mapping)
    assert [item['id'] for item in items] == ['symphony:acme-tenant:room/7/msg/1', 'symphony:acme-tenant:42']
    assert store.ingest(items)['inserted'] == 2
    # Without a prefix the item id is used as given, and ingest refuses it.
    unprefixed, _cursor, _checkpoint = normalize_result(payload, {k: v for k, v in mapping.items() if k != 'id_prefix'})
    assert unprefixed[0]['id'] == 'room/7/msg/1'
    with pytest.raises(ValueError, match='Source id'):
        store.ingest(unprefixed)
    # An item with no usable id is left alone for ingest to refuse.
    odd, _cursor, _checkpoint = normalize_result({'items': [{'id': True}, {'title': 'no id'}]}, {'id_prefix': 'symphony:acme-tenant:'})
    assert odd == [{'id': True}, {'title': 'no id'}]


@pytest.mark.parametrize('prefix', ['symphony:acme-tenant:', 'jira:a.atlassian.net:'])
def test_load_config_accepts_a_provider_site_prefix(tmp_path, prefix):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'mapping': {'id_prefix': prefix}}))
    assert load_config(path)['mapping']['id_prefix'] == prefix


@pytest.mark.parametrize('prefix', ['symphony:acme-tenant', 'symphony:', 'symphony::', 'Symphony:acme:', ':acme:', 'symphony:ac me:', '', 42, None])
def test_load_config_refuses_a_bad_id_prefix(tmp_path, prefix):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'endpoint': 'https://example.invalid/mcp', 'storage': 'keyring', 'mapping': {'id_prefix': prefix}}))
    with pytest.raises(ValueError, match='mapping.id_prefix'):
        load_config(path)


def test_a_vault_note_bare_ask_kind_is_an_ask_from_that_note(tmp_path, store):
    vault = (tmp_path / 'vault')
    vault.mkdir()
    vault = vault.resolve()
    (vault / 'Atlas.md').write_text('---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"reply","title":"Reply on scope","project_id":"atlas","owner":null},'
                                    '{"intent_key":"jira:a.atlassian.net:ATLAS-12:reply","title":"Reply on ATLAS-12","project_id":"atlas","owner":null}]\n---\nScope question.\n',
                                    encoding='utf-8')
    capture_vault(store, vault)
    note = store.list_sources('vault')[0]
    assert parse_source_id(note['id'])[0] == 'vault' and note['id'].endswith(':Atlas.md')
    assert sorted(o['intent_key'] for o in store.snapshot()['obligations']) == sorted([note['id'] + ':reply', SITE_A + ':reply'])
    capture_vault(store, vault)
    assert len(store.snapshot()['obligations']) == 2


def test_a_vault_note_with_a_bad_intent_key_is_reported_alone(tmp_path, store):
    vault = (tmp_path / 'vault')
    vault.mkdir()
    vault = vault.resolve()
    (vault / 'Good.md').write_text('---\nproject_ids: ["atlas"]\n---\nFine.\n', encoding='utf-8')
    (vault / 'Bad.md').write_text('---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"Release Status","title":"Free-form key"}]\n---\nOld style.\n', encoding='utf-8')
    result = capture_vault(store, vault)
    assert [e['path'] for e in result['errors']] == ['Bad.md'] and 'Intent key' in result['errors'][0]['error']
    assert [s['title'] for s in store.list_sources('vault')] == ['Good.md']
    assert store.snapshot()['obligations'] == []
