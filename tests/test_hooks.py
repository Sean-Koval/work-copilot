from pathlib import Path
import json
import pytest

from work_copilot.hooks import ingest_activity, enqueue_activity, drain_activity
from work_copilot.store import Store


def event(number='1', **changes):
    return {'session_id':'fictional-session', 'turn_id':number, 'event_id':number,
            'repo':'example-repo', 'summary':'Prepared a patch',
            'outcomes':['Code merged'], 'evidence_urls':['https://example.invalid/pr/1'], **changes}


def test_activity_claims_are_not_verified_and_unmapped_is_candidate(tmp_path):
    store = Store(tmp_path/'copilot.db')
    result = ingest_activity(store, event())
    source = store.list_sources()[0]
    assert result['source_id'] == source['id']
    assert 'Claimed (not independently verified): Code merged' in source['body']
    assert source['verification'] == 'claimed'
    assert store.snapshot()['review'][0]['status'] == 'candidate'
    assert not source['projects']


def test_exact_mapping_dedupe_restart_and_no_network(tmp_path):
    store = Store(tmp_path/'copilot.db')
    config = {'repo_projects': {'example-repo': {'id':'atlas','name':'Atlas','workstream':'engineering'}},
              'authority': {'jira':'high-level outcomes', 'github':'implementation evidence'}}
    first = ingest_activity(store, event(), config)
    store.close()
    store = Store(tmp_path/'copilot.db')
    second = ingest_activity(store, event(), config)
    assert first['source_id'] == second['source_id']
    assert len(store.list_sources()) == 1
    assert store.project('atlas')['sources'][0]['verification'] == 'claimed'
    assert store.snapshot()['refresh_needed']['needed']


def test_queue_fifo_survives_restart_and_failure_keeps_pending(tmp_path):
    enqueue_activity(tmp_path, event('1'))
    enqueue_activity(tmp_path, event('2'))
    paths = sorted((tmp_path/'activity-queue').glob('*.event.json'))
    first = json.loads(paths[0].read_text())
    assert first['event']['event_id'] == '1'
    store = Store(tmp_path/'copilot.db')
    outcome = drain_activity(store, tmp_path, {'max_events':1})
    assert outcome['processed'] == 1
    assert len(list((tmp_path/'activity-queue').glob('*.event.json'))) == 1
    store.close()
    store = Store(tmp_path/'copilot.db')
    assert drain_activity(store, tmp_path)['processed'] == 1
    assert len(store.list_sources()) == 2
    bad = tmp_path/'activity-queue'/'000.event.json'
    bad.write_text('{bad json')
    enqueue_activity(tmp_path, event('3'))
    failed = drain_activity(store, tmp_path)
    assert failed['errors'] and failed['processed'] == 0
    assert bad.exists()
    assert len(list((tmp_path/'activity-queue').glob('*.event.json'))) == 2
    assert list((tmp_path/'activity-queue').glob('*.error.json'))


def test_limits_safe_urls_and_recursion_guard(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        enqueue_activity(tmp_path, event(summary='x'*70000))
    with pytest.raises(ValueError):
        enqueue_activity(tmp_path, event(evidence_urls=['javascript:alert(1)']))
    monkeypatch.setenv('WORK_COPILOT_HOOK_ACTIVE', '1')
    assert enqueue_activity(tmp_path, event())['skipped'] == 'recursion_guard'
    assert not (tmp_path/'activity-queue').exists()


def test_queue_rejects_symlink(tmp_path):
    outside = tmp_path/'outside'; outside.mkdir()
    (tmp_path/'activity-queue').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        enqueue_activity(tmp_path, event())


def test_correction_learning_is_proposed_not_a_rule(tmp_path):
    store = Store(tmp_path/'copilot.db')
    activity = event(project_id='atlas', learning={'scope':'project', 'correction':'Do not infer deployment from merge', 'preferred_approach':'Request deployment evidence', 'conditions':'Release questions', 'session_ref':'fictional-session'})
    ingest_activity(store, activity)
    source = store.list_sources()[0]
    assert source['learning']['status'] == 'proposed'
    candidate = store.snapshot()['review'][0]
    assert candidate['title'].startswith('Review proposed learning')
    store.review(candidate['id'], candidate['revision'], {'status':'resolved'})
    assert store.list_sources()[0]['learning']['status'] == 'proposed'
    assert 'not an accepted rule' in source['body']


def test_retry_after_ingest_before_refresh_failure_keeps_flag(tmp_path, monkeypatch):
    store = Store(tmp_path/'copilot.db')
    enqueue_activity(tmp_path, event())
    real = store.request_refresh
    def fail(*args, **kwargs):
        raise RuntimeError('Simulated interruption after ingest')
    monkeypatch.setattr(store, 'request_refresh', fail)
    assert drain_activity(store, tmp_path)['errors']
    assert len(store.list_sources()) == 1
    monkeypatch.setattr(store, 'request_refresh', real)
    assert drain_activity(store, tmp_path)['processed'] == 1
    assert store.snapshot()['refresh_needed']['needed']


def test_concurrent_drain_returns_busy_without_processing(tmp_path):
    from work_copilot.hooks import _queue_lock
    store = Store(tmp_path/'copilot.db')
    enqueue_activity(tmp_path, event())
    with _queue_lock(tmp_path/'activity-queue'):
        result = drain_activity(store, tmp_path)
    assert result['processed'] == 0 and result['busy']
    assert not store.list_sources()
    assert drain_activity(store, tmp_path)['processed'] == 1


def test_procedure_references_are_context_not_stage_execution(tmp_path):
    store = Store(tmp_path/'copilot.db')
    ingest_activity(store, event(procedure_id='release-check', procedure_version='v2', step_id='verify-staging'))
    source = store.list_sources()[0]
    assert source['procedure']['step_id'] == 'verify-staging'
    assert 'not executed or advanced' in source['body']
