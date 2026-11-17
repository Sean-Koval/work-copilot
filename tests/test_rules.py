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
    for word in ('release', 'deployment', 'transcript', 'github', "'me'", '"me"', 'merged'):
        assert word not in code.lower(), word


def test_default_rules_keep_the_demo_drafts(tmp_path):
    store = Store(tmp_path / 's.db')
    seed_demo(store)
    transcript = _obligation(store, 'request-missing-transcript')
    canned = store.draft(transcript['id'])
    assert canned['text'].startswith('Could you share the meeting transcript')
    assert canned['rule'] == 'missing-transcript'
    assert any('team rule missing-transcript' in gap for gap in canned['gaps'])
    store.review(transcript['id'], transcript['revision'], {'question_state': 'sent'})
    assert 'already recorded as sent' in store.draft(transcript['id'])['text']

    release = _obligation(store, 'release-readiness')
    draft = store.draft(release['id'])
    assert draft['rule'] is None
    assert 'github' in draft['evidence'][1]['provider']
    assert 'merged code is not a deployment receipt' in draft['text']
    assert any('a merged PR alone is not deployment' in gap for gap in draft['gaps'])

    scope = _obligation(store, 'pilot-scope')
    assert 'clarification is already recorded as sent' in store.draft(scope['id'])['text']


def _write(tmp_path, rules, encoding='utf-8'):
    (tmp_path / RULES_FILE).write_text(json.dumps(rules), encoding=encoding)


def test_rules_file_changes_aliases_and_wording(tmp_path):
    _write(tmp_path, {
        'self_aliases': ['Sean'],
        'standing_gaps': [],
        'intents': [{'name': 'handover', 'kinds': ['Handover'], 'replies': {'pending': 'Handover notes are not shared yet.'}}],
    })
    store = Store(tmp_path / 's.db')
    store.ingest([{'id': 'a', 'revision': '1', 'provider': 'notes', 'title': 'Handover', 'body': 'Need the handover.',
                   'obligations': [{'intent_key': 'handover', 'title': 'Get handover', 'owner': 'sean '},
                                   {'intent_key': 'release-x', 'title': 'Release x', 'owner': 'alex'}]}])
    handover = _obligation(store, 'handover')
    assert handover['owner'] == 'me' and handover['status'] == 'open'
    assert _obligation(store, 'release-x')['status'] == 'candidate'
    assert store.snapshot()['today']['priorities'] == [handover['id']]
    draft = store.draft(handover['id'])
    assert draft['text'] == 'Handover notes are not shared yet.'
    assert draft['rule'] == 'handover'
    assert not any('a merged PR alone' in gap for gap in draft['gaps'])
    release = _obligation(store, 'release-x')
    reviewed = store.review(release['id'], release['revision'], {'owner': 'SEAN'})
    assert reviewed['owner'] == 'me'
    tracked = store.track_obligation({'intent_key': 'mine', 'title': 'Mine', 'owner': 'Sean'})
    assert tracked['owner'] == 'me'


def test_adding_an_intent_keeps_the_defaults(tmp_path):
    _write(tmp_path, {'intents': [{'name': 'handover', 'kinds': ['handover'], 'replies': {'pending': 'Later.'}}]})
    store = Store(tmp_path / 's.db')
    seed_demo(store)
    release = store.draft(_obligation(store, 'release-readiness')['id'])
    assert 'merged code is not a deployment receipt' in release['text']
    assert [rule.name for rule in store.rules.intents] == ['missing-transcript', 'release', 'handover']


def test_an_intent_is_replaced_or_disabled_by_name(tmp_path):
    rules = Rules.from_dict({'intents': [{'name': 'missing-transcript', 'disabled': True},
                                         {'name': 'release', 'words': ['ship'], 'lead_provider': 'CI', 'notes': ['Check CI.']}]})
    assert [rule.name for rule in rules.intents] == ['release']
    assert rules.intent_rule('request-missing-transcript') is None
    ob = {'intent_key': 'Ship-it', 'question_state': 'answered'}
    assert rules.notes(ob) == ['Check CI.', DEFAULT_RULES['question_state_notes']['answered']]
    evidence = [{'provider': 'room'}, {'provider': 'notes'}, {'provider': 'gitlab-ci'}]
    assert [s['provider'] for s in rules.order_evidence(ob, evidence)] == ['room', 'gitlab-ci', 'notes']


def test_first_matching_intent_applies_and_replies_fall_back_to_pending():
    rules = Rules.from_dict({'intents': [{'name': 'release', 'disabled': True},
                                         {'name': 'deploy-ask', 'words': ['deploy'], 'replies': {'pending': 'Ask ops.'}},
                                         {'name': 'deploy-note', 'words': ['deploy'], 'notes': ['Never reached.']}]})
    ob = {'intent_key': 'deploy-status', 'question_state': 'sent'}
    assert rules.reply(ob) == ('deploy-ask', ['Ask ops.'])
    only_sent = Rules.from_dict({'intents': [{'name': 'x', 'kinds': ['x'], 'replies': {'sent': 'Sent.'}}]})
    assert only_sent.reply({'intent_key': 'x'}) is None


def test_question_state_notes_and_store_rules_can_be_given(tmp_path):
    rules = Rules.from_dict({'question_state_notes': {'sent': 'Wait for it.'}})
    store = Store(tmp_path / 's.db', rules=rules)
    assert store.rules is rules
    assert rules.notes({'intent_key': 'other', 'question_state': 'sent'}) == ['Wait for it.']


def test_rules_file_saved_with_a_bom_loads(tmp_path):
    _write(tmp_path, {'self_aliases': ['sean']}, encoding='utf-8-sig')
    assert Rules.load(tmp_path).owner('Sean') == 'me'


@pytest.mark.parametrize('bad', [
    {'unknown': 1},
    {'self_aliases': ['']},
    {'self_aliases': 'sean'},
    [], None, 0, '',
    {'intents': [{'name': 'x'}]},
    {'intents': [{'name': 'x', 'disabled': False}]},
    {'intents': [{'name': 'release', 'disabled': True, 'words': ['x']}]},
    {'intents': 'not a list'},
    {'intents': [{'name': 'x', 'kinds': ['x'], 'replies': {'later': 'text'}}]},
    {'intents': [{'name': 'x', 'words': ['x'], 'extra': True}]},
    {'standing_gaps': 'not a list'},
])
def test_invalid_rules_are_refused(tmp_path, bad):
    _write(tmp_path, bad)
    with pytest.raises(ValueError, match='rules.json'):
        Store(tmp_path / 's.db')


@pytest.mark.parametrize('text', ['{not json', '[' * 100000])
def test_unreadable_rules_file_is_refused(tmp_path, text):
    (tmp_path / RULES_FILE).write_text(text, encoding='utf-8')
    with pytest.raises(ValueError, match='rules.json'):
        Rules.load(tmp_path)


def test_defaults_are_data():
    assert json.loads(json.dumps(DEFAULT_RULES)) == DEFAULT_RULES
    assert Rules.from_dict().self_owner == 'me'
    assert Rules.from_dict().owner('alex') == 'alex'
