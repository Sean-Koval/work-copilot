import json
import re
from pathlib import Path

import pytest

from work_copilot.demo import seed_demo
from work_copilot.rules import DEFAULT_RULES, RULES_FILE, Rules
from work_copilot.store import Store


def _obligation(store, intent_key):
    return next(o for o in store.snapshot()['obligations'] if o['intent_key'] == intent_key)


def test_store_holds_no_domain_words():
    text = (Path(__file__).parents[1] / 'work_copilot' / 'store.py').read_text(encoding='utf-8')
    code = re.sub(r"'RELEASE '", '', text)  # SQLite's RELEASE SAVEPOINT is not a domain word
    for word in ('release', 'deployment', 'transcript', 'github', "'me'", 'merged'):
        assert word not in code.lower(), word


def test_default_rules_keep_the_demo_drafts(tmp_path):
    store = Store(tmp_path / 's.db')
    seed_demo(store)
    transcript = _obligation(store, 'request-missing-transcript')
    assert store.draft(transcript['id'])['text'].startswith('Could you share the meeting transcript')
    store.review(transcript['id'], transcript['revision'], {'question_state': 'sent'})
    assert 'already recorded as sent' in store.draft(transcript['id'])['text']

    release = _obligation(store, 'release-readiness')
    draft = store.draft(release['id'])
    assert 'github' in draft['evidence'][1]['provider']
    assert 'merged code is not a deployment receipt' in draft['text']
    assert any('a merged PR alone is not deployment' in gap for gap in draft['gaps'])

    scope = _obligation(store, 'pilot-scope')
    assert 'clarification is already recorded as sent' in store.draft(scope['id'])['text']


def test_rules_file_changes_owner_and_wording(tmp_path):
    (tmp_path / RULES_FILE).write_text(json.dumps({
        'self_owner': 'sean',
        'standing_gaps': [],
        'intents': [{'name': 'handover', 'kinds': ['handover'], 'replies': {'pending': 'Handover notes are not shared yet.'}}],
    }), encoding='utf-8')
    store = Store(tmp_path / 's.db')
    store.ingest([{'id': 'a', 'revision': '1', 'provider': 'notes', 'title': 'Handover', 'body': 'Need the handover.',
                   'obligations': [{'intent_key': 'handover', 'title': 'Get handover', 'owner': 'sean'},
                                   {'intent_key': 'release-x', 'title': 'Release x', 'owner': 'me'}]}])
    handover = _obligation(store, 'handover')
    assert handover['status'] == 'open'
    assert _obligation(store, 'release-x')['status'] == 'candidate'
    assert store.snapshot()['today']['priorities'] == [handover['id']]
    draft = store.draft(handover['id'])
    assert draft['text'] == 'Handover notes are not shared yet.'
    assert not any('deployment' in gap for gap in draft['gaps'])
    assert 'deployment receipt' not in store.draft(_obligation(store, 'release-x')['id'])['text']
    tracked = store.track_obligation({'intent_key': 'mine', 'title': 'Mine'})
    assert tracked['owner'] == 'sean'


@pytest.mark.parametrize('bad', [
    {'unknown': 1},
    {'self_owner': ''},
    {'intents': [{'name': 'x'}]},
    {'intents': [{'name': 'x', 'kinds': ['x'], 'replies': {'later': 'text'}}]},
    {'intents': [{'name': 'x', 'words': ['x'], 'extra': True}]},
    {'standing_gaps': 'not a list'},
])
def test_invalid_rules_are_refused(tmp_path, bad):
    (tmp_path / RULES_FILE).write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(ValueError):
        Store(tmp_path / 's.db')


def test_unreadable_rules_file_is_refused(tmp_path):
    (tmp_path / RULES_FILE).write_text('{not json', encoding='utf-8')
    with pytest.raises(ValueError, match='rules.json'):
        Rules.load(tmp_path)


def test_defaults_are_data():
    assert json.loads(json.dumps(DEFAULT_RULES)) == DEFAULT_RULES
    assert Rules.from_dict().self_owner == 'me'
