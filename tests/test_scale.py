"""KAN-388: opening a store and get_attention stay narrow as the ledger grows."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from work_copilot.mcp_server import build_server
from work_copilot.store import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scale_check import synthetic  # noqa: E402


@pytest.fixture
def populated(tmp_path):
    store = Store(tmp_path / 'copilot.db')
    store.ingest(list(synthetic(200)), connector='scale')
    store.close()
    return tmp_path


def _forbid(monkeypatch, *names):
    def refuse(*args, **kwargs):
        raise AssertionError('full rebuild or full source read on a hot path')
    for name in names:
        monkeypatch.setattr(Store, name, refuse)


def test_reopen_does_not_rebuild_projections(populated, monkeypatch):
    _forbid(monkeypatch, '_rebuild_all_projections', 'list_sources')
    store = Store(populated / 'copilot.db')
    assert store.search('Thread 17')


def test_get_attention_reads_no_source_ledger(populated, monkeypatch):
    server = build_server(populated)
    _forbid(monkeypatch, '_rebuild_all_projections', 'list_sources', 'snapshot')
    original = Store._rows
    monkeypatch.setattr(Store, '_rows', lambda self, table: (_ for _ in ()).throw(AssertionError(table)) if table in ('sources', 'obligations') else original(self, table))
    data = json.loads(server._tool_manager.get_tool('get_attention').fn(limit=5))
    assert data['page']['total'] == 20 and len(data['obligations']) == 5
    assert data['page']['next_cursor'] == 5


def test_projection_version_change_rebuilds_once(populated, monkeypatch):
    import work_copilot.store as store_module
    monkeypatch.setattr(store_module, 'PROJECTION_VERSION', store_module.PROJECTION_VERSION + 1)
    Store(populated / 'copilot.db').close()
    _forbid(monkeypatch, '_rebuild_all_projections')
    assert Store(populated / 'copilot.db').search('Thread 17')


def test_doctor_repair_rebuilds_a_damaged_index(populated):
    import sqlite3
    db = sqlite3.connect(populated / 'copilot.db')
    db.execute('DELETE FROM source_search'); db.commit(); db.close()
    assert not Store(populated / 'copilot.db').search('Thread 17')
    run = subprocess.run([sys.executable, '-m', 'work_copilot', '--data-dir', str(populated), 'doctor', '--repair'],
                         capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['repaired']['sources'] == 200
    assert Store(populated / 'copilot.db').search('Thread 17')


def test_scale_check_runs():
    run = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'scale_check.py'), '--sources', '300'],
                         capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['obligations'] == 30


def test_a_crash_after_a_migration_still_rebuilds_on_next_open(populated, monkeypatch):
    import sqlite3
    from work_copilot import schema
    db = sqlite3.connect(populated / 'copilot.db')
    db.execute('DELETE FROM source_search'); db.commit(); db.close()

    def second(db):
        pass
    monkeypatch.setattr(schema, 'MIGRATIONS', schema.MIGRATIONS + (second,))
    monkeypatch.setattr(schema, 'SCHEMA_VERSION', len(schema.MIGRATIONS))
    with monkeypatch.context() as patch:
        patch.setattr(Store, '_rebuild_all_projections', lambda self: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError):
            Store(populated / 'copilot.db')
    assert Store(populated / 'copilot.db').search('Thread 17')
