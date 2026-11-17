"""Schema version, ordered migrations, pre-migration backup and JSONL export/import (KAN-389)."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from work_copilot import schema
from work_copilot.demo import seed_demo
from work_copilot.store import Store

NOW = lambda: datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

# The unversioned schema shipped before KAN-389: no user_version, no project_config.revision.
LEGACY = '''
CREATE VIRTUAL TABLE source_search USING fts5(source_id UNINDEXED,title,body,project_terms);
CREATE TABLE sources(id TEXT PRIMARY KEY, revision TEXT NOT NULL, payload TEXT NOT NULL, connector TEXT NOT NULL);
CREATE TABLE source_revisions(id TEXT NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE source_mappings(source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,projects TEXT NOT NULL);
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE project_config(project_id TEXT PRIMARY KEY,aliases TEXT NOT NULL,rooms TEXT NOT NULL);
CREATE TABLE source_projects(source_id TEXT NOT NULL,project_id TEXT NOT NULL,workstream TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_id,project_id,workstream));
CREATE TABLE obligations(id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
CREATE TABLE source_intents(source_id TEXT NOT NULL,candidate_key TEXT NOT NULL,obligation_id TEXT NOT NULL,PRIMARY KEY(source_id,candidate_key));
CREATE TABLE manual_evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id));
CREATE TABLE evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id));
CREATE TABLE drafts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE connectors(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE local_metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE plans(day TEXT PRIMARY KEY,payload TEXT NOT NULL);
'''


def _legacy_database(path):
    db = sqlite3.connect(path)
    db.executescript(LEGACY)
    source = {'id': 'sym/1', 'revision': '1', 'provider': 'symphony', 'title': 'Old question', 'body': 'Is Atlas live?',
              'observed_at': '2026-10-20T09:00:00+00:00', 'projects': [{'id': 'atlas', 'name': 'Atlas'}], 'obligations': [], 'deleted': False, 'url': None}
    db.execute('INSERT INTO sources VALUES(?,?,?,?)', ('sym/1', '1', json.dumps(source), 'import'))
    db.execute('INSERT INTO source_revisions VALUES(?,?,?)', ('sym/1', '1', json.dumps(source)))
    db.execute('INSERT INTO projects VALUES(?,?)', ('atlas', 'Atlas'))
    db.execute('INSERT INTO project_config VALUES(?,?,?)', ('atlas', '["ATL"]', '[]'))
    db.commit()
    db.close()


def test_new_database_starts_at_current_version_without_backup(tmp_path):
    store = Store(tmp_path / 'copilot.db')
    assert schema.version(store.db) == schema.SCHEMA_VERSION
    assert store.migration == {'from': 0, 'to': schema.SCHEMA_VERSION, 'backup': None}
    assert not (tmp_path / 'backups').exists()
    store.close()
    reopened = Store(tmp_path / 'copilot.db')
    assert reopened.migration == {'from': schema.SCHEMA_VERSION, 'to': schema.SCHEMA_VERSION, 'backup': None}


def test_unversioned_database_upgrades_after_a_backup(tmp_path):
    path = tmp_path / 'copilot.db'
    _legacy_database(path)
    store = Store(path, now=NOW)
    assert schema.version(store.db) == schema.SCHEMA_VERSION
    assert store.migration['from'] == 0
    backup = store.migration['backup']
    assert backup and backup.startswith(str(tmp_path / 'backups'))
    # The backup is the database as it was, still unversioned and still openable.
    old = sqlite3.connect(backup)
    assert old.execute('PRAGMA user_version').fetchone()[0] == 0
    assert 'revision' not in {row[1] for row in old.execute('PRAGMA table_info(project_config)')}
    assert old.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    old.close()
    # The upgraded store keeps every record and rebuilds its projections.
    assert store.project('atlas')['config_revision'] == 1 and store.project('atlas')['aliases'] == ['ATL']
    assert [s['id'] for s in store.search('Atlas')] == ['sym/1']
    assert store.update_project('atlas', 1, name='Atlas 2')['config_revision'] == 2


def test_failed_migration_rolls_back_and_keeps_the_old_version(tmp_path, monkeypatch):
    path = tmp_path / 'copilot.db'
    Store(path).close()

    def broken(db):
        db.execute('CREATE TABLE half_done(x)')
        raise RuntimeError('step failed')
    monkeypatch.setattr(schema, 'MIGRATIONS', schema.MIGRATIONS + (broken,))
    monkeypatch.setattr(schema, 'SCHEMA_VERSION', len(schema.MIGRATIONS))
    with pytest.raises(RuntimeError):
        Store(path)
    db = sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0] == len(schema.MIGRATIONS) - 1
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone()


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / 'copilot.db'
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute(f'PRAGMA user_version={schema.SCHEMA_VERSION + 1}')
    db.commit(); db.close()
    with pytest.raises(ValueError, match='newer than this Work Copilot'):
        Store(path)


def _populated(path):
    store = Store(path, now=NOW)
    seed_demo(store)
    store.create_project('beacon', 'Beacon', aliases=['BCN'], rooms=[{'id': 'r9', 'name': 'Beacon room'}])
    ob = next(o for o in store.snapshot()['obligations'] if o['status'] == 'candidate')
    store.review(ob['id'], ob['revision'], {'owner': 'me', 'status': 'open'})
    store.draft(ob['id'])
    store.track_obligation({'intent_key': 'ping-dana', 'title': 'Ping Dana', 'project_id': 'beacon', 'waiting_on': 'Dana', 'body': 'Ünïcode “quotes” kept'})
    store.request_refresh('check again')
    return store


def _rows(path):
    db = sqlite3.connect(path)
    try:
        return {table: sorted(map(tuple, db.execute(f'SELECT * FROM {table}'))) for table in schema.STATE_TABLES}
    finally:
        db.close()


def test_export_then_import_round_trips_with_no_loss(tmp_path):
    original = _populated(tmp_path / 'a' / 'copilot.db')
    exported = original.export_jsonl(tmp_path / 'state.jsonl')
    assert exported['schema_version'] == schema.SCHEMA_VERSION and sum(exported['rows'].values()) > 10
    lines = (tmp_path / 'state.jsonl').read_text(encoding='utf-8').splitlines()
    assert json.loads(lines[0])['kind'] == schema.EXPORT_KIND and len(lines) == 1 + sum(exported['rows'].values())
    restored, result = Store.import_jsonl(tmp_path / 'state.jsonl', tmp_path / 'b' / 'copilot.db', now=NOW)
    assert result['rows'] == exported['rows']
    assert _rows(tmp_path / 'a' / 'copilot.db') == _rows(tmp_path / 'b' / 'copilot.db')
    before, after = original.snapshot(), restored.snapshot()
    assert after == before
    assert restored.search('quotes')[0]['id'] == original.search('quotes')[0]['id']
    # Exporting the restore gives the same rows again.
    restored.export_jsonl(tmp_path / 'again.jsonl')
    assert (tmp_path / 'again.jsonl').read_text(encoding='utf-8').splitlines()[1:] == lines[1:]


def test_import_refuses_existing_data_and_bad_files(tmp_path):
    original = _populated(tmp_path / 'a' / 'copilot.db')
    original.export_jsonl(tmp_path / 'state.jsonl')
    with pytest.raises(ValueError, match='already exists'):
        Store.import_jsonl(tmp_path / 'state.jsonl', tmp_path / 'a' / 'copilot.db')
    lines = (tmp_path / 'state.jsonl').read_text(encoding='utf-8').splitlines()
    (tmp_path / 'short.jsonl').write_text('\n'.join(lines[:-1]) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='incomplete'):
        Store.import_jsonl(tmp_path / 'short.jsonl', tmp_path / 'c' / 'copilot.db')
    assert not (tmp_path / 'c' / 'copilot.db').exists() and not (tmp_path / 'c' / 'copilot.db.importing').exists()
    bad = json.loads(lines[1]); bad['table'] = 'sqlite_master'
    (tmp_path / 'bad.jsonl').write_text(lines[0] + '\n' + json.dumps(bad) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='recorded table'):
        Store.import_jsonl(tmp_path / 'bad.jsonl', tmp_path / 'd' / 'copilot.db')
    header = json.loads(lines[0]); header['schema_version'] = schema.SCHEMA_VERSION + 1
    (tmp_path / 'future.jsonl').write_text(json.dumps(header) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='not supported'):
        Store.import_jsonl(tmp_path / 'future.jsonl', tmp_path / 'e' / 'copilot.db')


def test_cli_export_and_import_jsonl(tmp_path):
    _populated(tmp_path / 'a' / 'copilot.db').close()
    run = lambda *args: subprocess.run([sys.executable, '-m', 'work_copilot', *args], capture_output=True, text=True)
    out = run('--data-dir', str(tmp_path / 'a'), 'export', '--jsonl', str(tmp_path / 'state.jsonl'))
    assert out.returncode == 0, out.stderr
    out = run('--data-dir', str(tmp_path / 'b'), 'import', '--jsonl', str(tmp_path / 'state.jsonl'))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)['schema_version'] == schema.SCHEMA_VERSION
    assert _rows(tmp_path / 'a' / 'copilot.db') == _rows(tmp_path / 'b' / 'copilot.db')
    again = run('--data-dir', str(tmp_path / 'b'), 'import', '--jsonl', str(tmp_path / 'state.jsonl'))
    assert again.returncode == 1 and 'already exists' in again.stderr
