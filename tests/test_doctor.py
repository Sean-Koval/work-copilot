from work_copilot.doctor import diagnose
from work_copilot.store import Store


def test_doctor_offline_does_not_authorize(tmp_path):
    store=Store(tmp_path/'copilot.db')
    result=diagnose(store,tmp_path,vault=tmp_path/'missing')
    assert result['network_used'] is False
    assert result['external_sends'] is False
    assert result['checks']['vault']['ok'] is False
    assert result['checks']['database']['ok'] is True
    assert result['checks']['connector']['status']=='not_configured'


def test_doctor_reports_store_timezone_not_changed_environment(tmp_path, monkeypatch):
    store=Store(tmp_path/'copilot.db',timezone_name='America/New_York')
    monkeypatch.setenv('WORK_COPILOT_TIMEZONE','UTC')
    assert diagnose(store,tmp_path)['timezone']=='America/New_York'


def test_doctor_config_error_actionable_and_nonmutating(tmp_path):
    import json
    store=Store(tmp_path/'copilot.db')
    config=tmp_path/'remote.json'
    config.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'protected-file','max_pages':'secret-invalid-number'}))
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    result=diagnose(store,tmp_path,config=config)
    assert result['checks']['connector']['status']=='invalid_config'
    assert 'max_pages' in result['checks']['connector']['note']
    assert 'secret-invalid-number' not in str(result)
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert result['checks']['python']['ok'] is True
    assert result['checks']['dependencies']['ok'] is True


def test_doctor_keyring_inspection_never_reads_credentials(tmp_path, monkeypatch):
    import json,sys,types
    store=Store(tmp_path/'copilot.db');config=tmp_path/'remote.json'
    config.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'keyring'}))
    backend=type('Keyring',(),{'__module__':'keyring.backends.fail'})()
    fake=types.SimpleNamespace(get_keyring=lambda:backend,get_password=lambda *a:(_ for _ in ()).throw(AssertionError('secret access')))
    monkeypatch.setitem(sys.modules,'keyring',fake)
    result=diagnose(store,tmp_path,config=config)
    assert result['checks']['oauth_storage']['ok'] is False
    assert 'secure OS' in result['checks']['oauth_storage']['note']
    assert not (tmp_path/'oauth').exists()


def test_doctor_path_missing_creates_nothing(tmp_path):
    from work_copilot.doctor import diagnose_path
    missing=tmp_path/'not-created'
    result=diagnose_path(missing,timezone_name='Asia/Tokyo')
    assert result['checks']['database']['status']=='not_initialized'
    assert result['timezone']=='Asia/Tokyo'
    assert not missing.exists()


def test_doctor_path_reads_live_wal_without_source_mutation(tmp_path):
    from work_copilot.doctor import diagnose_path
    store=Store(tmp_path/'copilot.db')
    store.db.execute('INSERT INTO projects VALUES (?,?)',('project-1','Fictional project'))
    store.db.commit()
    def snapshot():
        return {p.name:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.iterdir()}
    before=snapshot()
    result=diagnose_path(tmp_path)
    assert result['checks']['database']['ok'] is True
    assert result['checks']['projects']['count']==1
    assert before==snapshot()
    store.close()


def test_doctor_path_corrupt_database_reports_without_repair(tmp_path):
    from work_copilot.doctor import diagnose_path
    path=tmp_path/'copilot.db';path.write_bytes(b'fictional-invalid-database')
    result=diagnose_path(tmp_path)
    assert result['checks']['database']['status']=='unreadable_or_invalid'
    assert path.read_bytes()==b'fictional-invalid-database'
    assert sorted(p.name for p in tmp_path.iterdir())==['copilot.db']


def test_compatibility_doctor_snapshots_custom_database_name(tmp_path):
    store=Store(tmp_path/'custom.db',timezone_name='Asia/Tokyo')
    store.db.execute('INSERT INTO projects VALUES (?,?)',('custom-project','Fictional custom project'))
    store.db.commit()
    before={p.name:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.iterdir()}
    result=diagnose(store,tmp_path)
    assert result['checks']['database']['ok'] is True
    assert result['checks']['database']['path']==str(tmp_path/'custom.db')
    assert result['checks']['projects']['count']==1
    assert result['timezone']=='Asia/Tokyo'
    assert before=={p.name:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.iterdir()}
    store.close()
