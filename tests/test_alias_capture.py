from work_copilot.store import Store
from work_copilot.vault import capture_vault


def test_exact_alias_is_suggestion_not_assignment(tmp_path):
    vault=tmp_path/'vault';vault.mkdir();(vault/'daily.md').write_text('Dart: staging discussed. [[Dart]] needs context.')
    store=Store(tmp_path/'db');store.create_project('delta','Delta',aliases=['Dart']);store.create_project('echo','Echo',aliases=['Dart'])
    capture_vault(store,vault)
    source=store.list_sources()[0]
    assert set(source['suggested_projects'])=={'delta','echo'}
    assert source['projects']==[]
    ob=store.snapshot()['review'][0]
    assert ob['owner'] is None and ob['project_id'] is None
    assert 'Delta' in ob['title'] and 'Echo' in ob['title']


def test_alias_no_substring_and_explicit_project_wins(tmp_path):
    vault=tmp_path/'vault';vault.mkdir();(vault/'daily.md').write_text('cart discussion')
    store=Store(tmp_path/'db');store.create_project('art','Art')
    capture_vault(store,vault)
    assert store.list_sources()[0]['suggested_projects']==[]
    (vault/'daily.md').write_text('---\nproject_ids: ["explicit"]\n---\nArt: discussion')
    capture_vault(store,vault)
    source=store.list_sources()[0]
    assert source['suggested_projects']==[]
    assert source['projects'][0]['id']=='explicit'
