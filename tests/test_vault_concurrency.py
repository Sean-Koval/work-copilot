from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from work_copilot.store import Store
from work_copilot.vault import capture_vault, export_projects, write_inbox_note


def test_simultaneous_capture_export_inbox_preserves_manifest(tmp_path):
    vault=tmp_path/'vault';vault.mkdir()
    store=Store(tmp_path/'db');store.create_project('atlas','Atlas')
    (vault/'daily.md').write_text('---\nproject_ids: ["atlas"]\n---\nHandwritten original')
    def task(i):
        if i%3==0: return export_projects(store,vault)
        if i%3==1: return capture_vault(store,vault)
        return write_inbox_note(store,vault,'Inbox','Context',project_id='atlas')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(task,range(24))))==24
    export_projects(store,vault)
    assert 'Handwritten original' in (vault/'daily.md').read_text()
    assert (vault/'Work Copilot Generated'/'Review.md').exists()


def test_independent_processes_capture_and_export_same_vault(tmp_path):
    import subprocess,sys
    vault=tmp_path/'vault';vault.mkdir();data=tmp_path/'data'
    store=Store(data/'copilot.db');store.create_project('atlas','Atlas');store.close()
    (vault/'daily.md').write_text('---\nproject_ids: ["atlas"]\n---\nOriginal unchanged')
    processes=[subprocess.Popen([sys.executable,'-m','work_copilot','--data-dir',str(data),command,str(vault)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for command in ['capture','export','capture','export']]
    for process in processes:
        stdout,stderr=process.communicate(timeout=30)
        assert process.returncode==0,(stdout,stderr)
    assert 'Original unchanged' in (vault/'daily.md').read_text()
    assert (vault/'Work Copilot Generated'/'Review.md').is_file()
