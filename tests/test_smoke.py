from pathlib import Path

from work_copilot.smoke import run_smoke


def test_synthetic_smoke_checks_local_workflow_and_mcp_without_user_data(tmp_path, monkeypatch):
    sentinel = tmp_path/'private-vault'; sentinel.mkdir()
    note = sentinel/'Never read me.md'; note.write_text('PRIVATE SENTINEL')
    monkeypatch.setenv('WORK_COPILOT_VAULT', str(sentinel))
    monkeypatch.setenv('WORK_COPILOT_TIMEZONE', 'Pacific/Honolulu')
    monkeypatch.setenv('WORK_COPILOT_HOOK_ACTIVE', '1')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path/'private-data'))
    before = {p.name:p.read_bytes() for p in sentinel.iterdir()}
    result = run_smoke()
    assert result['success'], result
    assert result['synthetic_only'] is True
    assert {c['name'] for c in result['checks']} == {
        'fresh_store', 'projects_and_rooms', 'capture_and_preservation',
        'waiting_followup_search_draft', 'evidence_edit_invalidates_draft',
        'activity_queue', 'wiki_export', 'restart_and_dedupe', 'mcp_stdio'}
    assert all(c['success'] for c in result['checks'])
    assert {p.name:p.read_bytes() for p in sentinel.iterdir()} == before
    assert 'PRIVATE SENTINEL' not in str(result)


def test_smoke_failure_returns_failed_phase_and_cleans_temporary_files(monkeypatch):
    import work_copilot.smoke as smoke
    from tempfile import TemporaryDirectory
    created = []
    def directory(*args, **kwargs):
        obj = TemporaryDirectory(*args, **kwargs)
        created.append(Path(obj.name))
        return obj
    class BrokenStore(smoke.Store):
        def create_project(self, *args, **kwargs):
            raise RuntimeError('Synthetic injected failure')
    monkeypatch.setattr(smoke, 'TemporaryDirectory', directory)
    monkeypatch.setattr(smoke, 'Store', BrokenStore)
    result = run_smoke()
    assert not result['success']
    assert result['checks'][-1]['name'] == 'projects_and_rooms'
    assert result['checks'][-1]['success'] is False
    assert 'Synthetic injected failure' in result['checks'][-1]['detail']
    assert created and all(not path.exists() for path in created)
