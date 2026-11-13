"""Read-only review queue over a knowledge repository (KAN-468). GitHub is always a fake."""
import hashlib
import json
import socket
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_copilot import review_queue
from work_copilot.mcp_server import _brief_obligation
from work_copilot.review_queue import (
    AccessDenied,
    codeowner_rules,
    collect_review_queue,
    gh_reader,
    owners_of,
)
from work_copilot.store import Store

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
ME = 'reviewer-me'
REPO = 'example-org/knowledge'


def _iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def _record(path: Path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = '\n'.join(f'{key}: {json.dumps(value)}' for key, value in fields.items())
    path.write_text(f'---\n{header}\n---\n# {fields.get("title", path.stem)}\n', encoding='utf-8')


@pytest.fixture
def knowledge(tmp_path):
    root = tmp_path.resolve() / 'knowledge'
    (root / '.github').mkdir(parents=True)
    (root / '.github' / 'CODEOWNERS').write_text('# conventions need the owner\n/team/conventions/ @Reviewer-Me\n', encoding='utf-8')
    (root / 'knowledge.toml').write_text('[check]\nroots = ["team", "projects"]\n', encoding='utf-8')
    past, future = _iso(NOW - timedelta(days=2)), _iso(NOW + timedelta(days=20))
    base = {'scope': 'team', 'date': '2026-09-01', 'sources': [], 'links': []}
    _record(root / 'team/decisions/DEC-0001.md', id='DEC-0001', type='decision', status='accepted', owner=ME,
            title='Pin harness by digest', stale_after=past, **base)
    _record(root / 'team/decisions/DEC-0002.md', id='DEC-0002', type='decision', status='accepted', owner=ME,
            stale_after=future, **base)
    _record(root / 'team/decisions/DEC-0003.md', id='DEC-0003', type='decision', status='accepted', owner='someone-else',
            stale_after=past, **base)
    _record(root / 'team/decisions/DEC-0004.md', id='DEC-0004', type='decision', status='superseded', owner=ME,
            stale_after=past, **base)
    _record(root / 'inbox/zoom-old--aa.md', id='zoom-old', type='source', status='raw', owner=ME, title='Planning call',
            stale_after=_iso(NOW + timedelta(days=3)), **base)
    _record(root / 'inbox/zoom-new--bb.md', id='zoom-new', type='source', status='raw', owner=ME,
            stale_after=_iso(NOW + timedelta(days=20)), **base)
    (root / 'inbox' / 'raw').mkdir()
    (root / 'inbox' / 'raw' / 'sha256-x.txt').write_text('transcript', encoding='utf-8')
    return root


def _pull(number, *, labels=('records',), reviewers=(), author='batch-bot', hours=10):
    return {'number': number, 'title': f'Records from meeting {number}', 'draft': False,
            'html_url': f'https://github.com/{REPO}/pull/{number}', 'user': {'login': author},
            'labels': [{'name': name} for name in labels], 'requested_reviewers': [{'login': r} for r in reviewers],
            'created_at': _iso(NOW - timedelta(hours=hours))}


class FakeGitHub:
    """Answers the REST GETs the collector makes; records every path."""

    def __init__(self):
        self.pulls = [
            _pull(1, reviewers=[ME], hours=50),                       # requested reviewer, over 48h
            _pull(2, hours=5),                                        # code owner of a changed convention
            _pull(3, reviewers=['someone-else']),                     # neither
            _pull(4, labels=('docs',), reviewers=[ME]),               # not a records PR: no record files
        ]
        self.files = {2: ['team/conventions/branch-names.md'], 3: ['team/decisions/x.md'], 4: ['README.md'],
                      8: ['projects/hangar/decisions/a.md', 'team/sources/kan-1.md']}
        self.calls = []
        self.teams = {('example-org', 'knowledge-reviewers'): {'state': 'active'}}
        self.forbidden = set()

    def __call__(self, path):
        self.calls.append(path)
        route = path.split('?')[0]
        if route.startswith('orgs/'):
            _, org, _, slug, _, user = route.split('/')
            assert user == ME
            if slug in self.forbidden:
                raise AccessDenied(route)  # what gh_reader raises on HTTP 403
            if (org, slug) not in self.teams:
                raise LookupError(route)  # what gh_reader raises on HTTP 404
            return self.teams[(org, slug)]
        if route == f'repos/{REPO}/pulls':
            return list(self.pulls)
        number = int(route.split('/')[4])
        return [{'filename': name} for name in self.files.get(number, [])]


def _tree(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.fixture
def store(tmp_path):
    clock = {'now': NOW}
    value = Store(tmp_path.resolve() / 'copilot.db', now=lambda: clock['now'])
    value.clock = clock
    yield value
    value.close()


def _collect(store, knowledge, github, review='assigned', batch_authors=()):
    return collect_review_queue(store, knowledge, me=ME, github=REPO, fetch=github, now=store.clock['now'],
                                review=review, batch_authors=batch_authors)


def _queue(store):
    return {o['intent_key']: o for o in store.snapshot()['review_queue']}


def test_two_prs_an_overdue_record_and_an_old_source_make_four_obligations(store, knowledge):
    result = _collect(store, knowledge, FakeGitHub())
    assert result['open'] == 4 and result['external_writes'] is False
    queue = _queue(store)
    assert sorted(queue) == sorted([
        f'review:pr:{REPO}#1', f'review:pr:{REPO}#2',
        'review:record:team/decisions/DEC-0001.md', 'review:source:inbox/zoom-old--aa.md'])
    for item in queue.values():
        assert item['kind'] == 'review' and item['owner'] == 'me' and item['status'] == 'open'
    pr = queue[f'review:pr:{REPO}#1']
    assert pr['age_hours'] == 50.0
    assert pr['due_at'] == (NOW - timedelta(hours=2)).isoformat()
    assert pr['evidence'][0]['url'] == f'https://github.com/{REPO}/pull/1'
    assert queue['review:source:inbox/zoom-old--aa.md']['age_hours'] == 27 * 24
    assert queue['review:record:team/decisions/DEC-0001.md']['path'] == 'team/decisions/DEC-0001.md'
    state = store.snapshot()
    # Shown in "Needs you" (owned and open), not in the candidate review page.
    assert all(o['kind'] != 'review' for o in state['review'])
    assert state['review_queue'][0]['intent_key'] == 'review:source:inbox/zoom-old--aa.md'  # oldest first


def test_closing_a_pr_resolves_it_on_the_next_collect_and_reopening_restores_it(store, knowledge):
    github = FakeGitHub()
    _collect(store, knowledge, github)
    github.pulls = [p for p in github.pulls if p['number'] != 1]
    store.clock['now'] = NOW + timedelta(hours=1)
    result = _collect(store, knowledge, github)
    assert result['open'] == 3 and len(result['resolved']) == 1
    resolved = [o for o in store.snapshot()['obligations'] if o['status'] == 'resolved']
    assert [o['intent_key'] for o in resolved] == [f'review:pr:{REPO}#1']
    assert len(_queue(store)) == 3
    github.pulls.append(_pull(1, reviewers=[ME], hours=50))
    result = _collect(store, knowledge, github)
    assert result['reopened'] and len(_queue(store)) == 4


def test_moving_stale_after_or_processing_the_source_resolves_the_item(store, knowledge):
    _collect(store, knowledge, FakeGitHub())
    record = knowledge / 'team/decisions/DEC-0001.md'
    record.write_text(record.read_text(encoding='utf-8').replace(
        _iso(NOW - timedelta(days=2)), _iso(NOW + timedelta(days=90))), encoding='utf-8')
    source = knowledge / 'inbox/zoom-old--aa.md'
    (knowledge / 'processed').mkdir()
    source.rename(knowledge / 'processed' / source.name)
    result = _collect(store, knowledge, FakeGitHub())
    assert result['open'] == 2 and len(result['resolved']) == 2


def test_collect_never_writes_to_any_external_system(store, knowledge, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('network or process call during collect')

    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(subprocess, 'run', refuse)
    monkeypatch.setattr(subprocess, 'Popen', refuse)
    github = FakeGitHub()
    before = _tree(knowledge)
    _collect(store, knowledge, github)
    assert _tree(knowledge) == before
    # Only list reads: open pulls and a pull's files.
    assert github.calls and all(c.startswith(f'repos/{REPO}/pulls') and 'state=' in c or c.split('?')[0].endswith('/files')
                                or c.startswith('orgs/') and '/memberships/' in c for c in github.calls)


def test_gh_reader_only_issues_get_requests():
    seen = []

    class Result:
        returncode = 0
        stdout = '[]'

    def runner(argv, **kwargs):
        seen.append(argv)
        return Result()

    fetch = gh_reader(runner=runner, executable='gh')
    assert fetch(f'repos/{REPO}/pulls?state=open&per_page=100&page=1') == []
    argv = seen[0]
    assert argv[:4] == ['gh', 'api', '--method', 'GET']
    assert not {'-f', '-F', '--field', '--raw-field', '--input', 'POST', 'PATCH', 'PUT', 'DELETE'} & set(argv)
    for bad in ('/repos/x/y/pulls', 'https://api.github.com/x', 'repos/../user'):
        with pytest.raises(ValueError):
            fetch(bad)


def test_codeowners_last_match_wins(knowledge):
    rules = codeowner_rules(knowledge) + [('*.md', ['other'])]
    assert owners_of(codeowner_rules(knowledge), 'team/conventions/a.md') == ['reviewer-me']
    assert owners_of(codeowner_rules(knowledge), 'team/decisions/a.md') == []
    assert owners_of(rules, 'team/conventions/a.md') == ['other']
    assert owners_of([('/docs/', ['d'])], 'src/docs/x.md') == []
    assert owners_of([('docs/', ['d'])], 'src/docs/x.md') == ['d']
    assert owners_of([('conventions/', ['c'])], 'team/conventions/x.md') == ['c']


def test_without_github_only_local_items_are_collected(store, knowledge):
    result = collect_review_queue(store, knowledge, me=ME, now=NOW)
    assert result['open'] == 2 and result['pull_requests_read'] is False
    with pytest.raises(ValueError):
        collect_review_queue(store, knowledge, me='', now=NOW)
    with pytest.raises(ValueError):
        collect_review_queue(store, knowledge, me=ME, github='not a repo', fetch=FakeGitHub(), now=NOW)


def test_mcp_attention_exposes_kind_link_and_age(store, knowledge):
    _collect(store, knowledge, FakeGitHub())
    pr = _queue(store)[f'review:pr:{REPO}#1']
    brief = _brief_obligation(pr)
    assert brief['kind'] == 'review' and brief['review_kind'] == 'pull_request'
    assert brief['url'].endswith('/pull/1') and brief['age_hours'] == 50.0


def test_module_has_no_write_verbs():
    text = Path(review_queue.__file__).read_text(encoding='utf-8')
    for verb in ("'POST'", "'PATCH'", "'PUT'", "'DELETE'", 'write_text', 'write_bytes', 'unlink('):
        assert verb not in text


def test_personal_repo_review_all_lists_every_open_records_pr(store, knowledge):
    github = FakeGitHub()
    github.pulls.append(_pull(5, author=ME))                      # the batch, under the owner's own account
    github.pulls.append({**_pull(6), 'draft': True})               # a labelled draft that changes no records
    result = _collect(store, knowledge, github, review='all')
    prs = sorted(k for k in _queue(store) if k.startswith('review:pr:'))
    assert prs == [f'review:pr:{REPO}#{n}' for n in (1, 2, 3, 5)]
    assert result['open'] == 6
    # Labelled non-drafts need no file read when every records PR counts.
    assert [c.split('?')[0] for c in github.calls if '/files' in c] == [f'repos/{REPO}/pulls/{n}/files' for n in (4, 6)]


def test_draft_prs_that_change_records_are_listed_with_the_clock_from_created_at(store, knowledge):
    # ai-dlc `propose` always opens records PRs as drafts.
    github = FakeGitHub()
    github.pulls = [{**_pull(10, labels=(), author=ME, hours=60), 'draft': True}]
    github.files[10] = ['team/decisions/new-decision.md']
    _collect(store, knowledge, github, review='all')
    pr = _queue(store)[f'review:pr:{REPO}#10']
    assert pr['draft'] is True and pr['title'].startswith('Review draft records PR #10')
    assert pr['age_hours'] == 60.0
    assert pr['due_at'] == (NOW - timedelta(hours=12)).isoformat()
    assert _brief_obligation(pr)['draft'] is True
    # Marked ready later: same obligation, no longer a draft, same clock.
    github.pulls = [_pull(10, labels=(), author=ME, hours=60)]  # same created_at
    store.clock['now'] = NOW + timedelta(hours=1)
    _collect(store, knowledge, github, review='all')
    ready = _queue(store)[f'review:pr:{REPO}#10']
    assert ready['id'] == pr['id'] and ready['draft'] is False and ready['due_at'] == pr['due_at']


def test_assigned_mode_counts_requested_teams_i_belong_to(store, knowledge):
    github = FakeGitHub()
    team = lambda slug: {'slug': slug, 'organization': {'login': 'example-org'}}  # noqa: E731
    github.pulls = [{**_pull(11, author='teammate'), 'requested_teams': [team('knowledge-reviewers')]},
                    {**_pull(12, author='teammate'), 'requested_teams': [team('other-team')]},
                    {**_pull(13, author='teammate'), 'requested_teams': [team('knowledge-reviewers')]}]
    _collect(store, knowledge, github, review='assigned')
    assert sorted(k for k in _queue(store) if k.startswith('review:pr:')) == [f'review:pr:{REPO}#{n}' for n in (11, 13)]
    membership = [c for c in github.calls if c.startswith('orgs/')]
    assert membership == ['orgs/example-org/teams/knowledge-reviewers/memberships/reviewer-me',
                          'orgs/example-org/teams/other-team/memberships/reviewer-me']  # cached per team


def test_team_repo_adds_my_own_and_batch_bot_prs_to_assigned_ones(store, knowledge):
    github = FakeGitHub()
    github.pulls += [_pull(5, author='Reviewer-Me'), _pull(6, author='records-bot'), _pull(7, author='teammate')]
    _collect(store, knowledge, github, review='assigned', batch_authors=('@records-bot',))
    prs = sorted(k for k in _queue(store) if k.startswith('review:pr:'))
    assert prs == [f'review:pr:{REPO}#{n}' for n in (1, 2, 5, 6)]
    with pytest.raises(ValueError):
        _collect(store, knowledge, github, review='everything')


def test_serving_app_refreshes_the_queue_on_the_collector_cadence(tmp_path, knowledge, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from work_copilot.app import create_app
    calls = []
    real = review_queue.collect_review_queue

    def counting(store, **settings):
        calls.append(settings)
        return real(store, **settings, fetch=FakeGitHub(), now=NOW)

    monkeypatch.setattr(review_queue, 'collect_review_queue', counting)
    settings = {'repo': knowledge, 'me': ME, 'owner': None, 'github': REPO, 'review': 'all', 'batch_authors': ()}
    app = create_app(tmp_path / 'data', review_queue=settings)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        # The worker records the call before the collect commits; wait for the result itself.
        for _ in range(200):
            state = client.get('/api/state').json()
            if len(state['review_queue']) == 5:
                break
            asyncio.run(asyncio.sleep(0.05))
    assert calls and calls[0]['review'] == 'all'
    assert state['runtime']['review_queue_configured'] is True
    assert len(state['review_queue']) == 5  # PRs 1-3, the overdue record, the old Source


def test_unlabelled_pr_that_changes_records_counts_as_a_records_pr(store, knowledge):
    # Live run on 2026-11-12: the repository had no `records` label and the back-fill PR carried none.
    github = FakeGitHub()
    github.pulls = [_pull(8, labels=(), author=ME, hours=3), _pull(9, labels=(), author=ME)]
    github.files[9] = ['README.md', '.github/workflows/check.yml']
    _collect(store, knowledge, github, review='all')
    assert sorted(k for k in _queue(store) if k.startswith('review:pr:')) == [f'review:pr:{REPO}#8']


def test_gh_reader_maps_404_to_lookup_error():
    class Result:
        returncode = 1
        stdout = ''
        stderr = 'gh: Not Found (HTTP 404)'

    fetch = gh_reader(runner=lambda argv, **kwargs: Result(), executable='gh')
    with pytest.raises(LookupError):
        fetch('orgs/example-org/teams/x/memberships/reviewer-me')


def test_team_membership_403_skips_that_team_with_one_warning(store, knowledge, caplog):
    github = FakeGitHub()
    github.forbidden = {'secret-team', 'other-secret'}
    team = lambda slug: {'slug': slug, 'organization': {'login': 'example-org'}}  # noqa: E731
    github.pulls = [{**_pull(11, author='teammate'), 'requested_teams': [team('secret-team'), team('knowledge-reviewers')]},
                    {**_pull(12, author='teammate'), 'requested_teams': [team('other-secret')]},
                    {**_pull(13, author='teammate'), 'requested_teams': [team('secret-team')]}]
    with caplog.at_level('WARNING', logger='work_copilot.review_queue'):
        result = _collect(store, knowledge, github, review='assigned')
    assert result['open'] == 3  # PR 11 via the team I am in, plus the overdue record and the old Source
    assert [k for k in _queue(store) if k.startswith('review:pr:')] == [f'review:pr:{REPO}#11']
    assert [r.levelname for r in caplog.records] == ['WARNING']


def test_gh_reader_maps_403_to_access_denied():
    class Result:
        returncode = 1
        stdout = ''
        stderr = 'gh: Must have admin rights (HTTP 403)'

    fetch = gh_reader(runner=lambda argv, **kwargs: Result(), executable='gh')
    with pytest.raises(AccessDenied):
        fetch('orgs/example-org/teams/x/memberships/reviewer-me')
