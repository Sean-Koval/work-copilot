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
    config.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'keyring','max_pages':'secret-invalid-number'}))
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
    assert 'Secret Service' in result['checks']['oauth_storage']['note']
    assert not (tmp_path/'oauth').exists()


def test_doctor_flags_leftover_protected_file_tokens_without_reading(tmp_path, fake_keyring):
    import json
    store=Store(tmp_path/'copilot.db');config=tmp_path/'remote.json'
    config.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'keyring'}))
    (tmp_path/'oauth').mkdir();(tmp_path/'oauth'/'old.json').write_text('{"tokens":{"access_token":"fictional-old"}}')
    result=diagnose(store,tmp_path,config=config)['checks']['oauth_storage']
    assert result['ok'] is False and result['legacy_token_files']==1
    assert 'connect --authorize' in result['note'] and 'fictional-old' not in str(result)


def test_doctor_reports_protected_file_migration(tmp_path):
    import json
    store=Store(tmp_path/'copilot.db');config=tmp_path/'remote.json'
    config.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'protected-file'}))
    connector=diagnose(store,tmp_path,config=config)['checks']['connector']
    assert connector['status']=='invalid_config' and 'connect --authorize' in connector['note']


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


def _registration(tmp_path, monkeypatch, user=None, project=None, local=None):
    import json
    from work_copilot.doctor import _mcp_registration_check
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    home=tmp_path/'home';home.mkdir(exist_ok=True);cwd=tmp_path/'cwd';cwd.mkdir(exist_ok=True)
    config={}
    if user is not None: config['mcpServers']=user
    if local is not None: config['projects']={str(cwd):{'mcpServers':local}}
    if config: (home/'.claude.json').write_text(json.dumps(config))
    if project is not None: (cwd/'.mcp.json').write_text(json.dumps({'mcpServers':project}))
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=_mcp_registration_check(tmp_path/'data','UTC',home=home,cwd=cwd)
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    return result


def test_doctor_mcp_registration_missing_gives_fix(tmp_path, monkeypatch):
    result=_registration(tmp_path,monkeypatch)
    assert result['status']=='warning' and result['ok'] is False
    assert result['fix'].startswith('claude mcp add --scope user --transport stdio work-copilot --env ')
    assert 'WORK_COPILOT_DATA_DIR=' in result['fix']


def test_doctor_mcp_registration_user_project_and_local_scopes(tmp_path, monkeypatch):
    entry={'type':'stdio','command':'work-copilot','args':['mcp'],'env':{'WORK_COPILOT_DATA_DIR':str(tmp_path/'data')}}
    assert _registration(tmp_path,monkeypatch,user={'work-copilot':entry})['scope']=='user'
    assert _registration(tmp_path,monkeypatch,user={'other':entry},local={'work-copilot':entry})['scope']=='local'
    (tmp_path/'p').mkdir()
    project=_registration(tmp_path/'p',monkeypatch,project={'work-copilot':{'command':'work-copilot','args':['--data-dir',str(tmp_path/'p'/'data'),'mcp']}})
    assert project['status']=='ok' and project['scope']=='project' and 'approval' in project['note']


def test_doctor_mcp_registration_flags_another_data_dir(tmp_path, monkeypatch):
    entry={'command':'work-copilot','args':['mcp'],'env':{'WORK_COPILOT_DATA_DIR':str(tmp_path/'elsewhere')}}
    result=_registration(tmp_path,monkeypatch,user={'work-copilot':entry})
    assert result['status']=='warning' and result['registered_data_dir']==str(tmp_path/'elsewhere')
    assert result['fix'].startswith('claude mcp remove --scope user work-copilot')


def test_doctor_mcp_registration_invalid_settings_is_a_warning(tmp_path, monkeypatch):
    from work_copilot.doctor import _mcp_registration_check
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    home=tmp_path/'home';home.mkdir();(home/'.claude.json').write_text('{not json')
    result=_mcp_registration_check(tmp_path/'data',home=home,cwd=tmp_path)
    assert result['status']=='warning' and 'Unreadable' in result['note'] and 'fix' in result


def test_doctor_vault_sync_names_sync_roots(tmp_path):
    from work_copilot.doctor import _vault_sync_check, sync_provider
    assert sync_provider(tmp_path/'OneDrive - Fictional Co'/'vault')=='OneDrive'
    assert sync_provider(tmp_path/'Dropbox (Personal)'/'vault')=='Dropbox'
    assert sync_provider(tmp_path/'Library'/'Mobile Documents'/'com~apple~CloudDocs'/'vault')=='iCloud Drive'
    assert sync_provider(tmp_path/'Library'/'CloudStorage'/'GoogleDrive-me'/'vault')=='Google Drive'
    assert 'CloudStorage' in sync_provider(tmp_path/'Library'/'CloudStorage'/'Box-Box'/'vault')
    assert sync_provider(tmp_path/'work-vault') is None
    synced=_vault_sync_check(tmp_path/'Dropbox'/'vault')
    assert synced['status']=='warning' and synced['ok'] is False and 'company' in synced['note'] and synced['fix']
    assert _vault_sync_check(tmp_path)['status']=='ok'


def test_doctor_vault_sync_refuses_symlinks_like_the_vault_rule(tmp_path):
    import pytest
    real=tmp_path/'real';real.mkdir();link=tmp_path/'link'
    try:
        link.symlink_to(real,target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    store=Store(tmp_path/'copilot.db')
    result=diagnose(store,tmp_path,vault=link)['checks']['vault_sync']
    store.close()
    assert result['status']=='error' and 'must not contain symlinks' in result['note']
    assert str(real) in result['fix']


def test_doctor_overlay_states(tmp_path):
    import json
    from work_copilot.doctor import overlay_check
    missing=overlay_check(tmp_path)
    assert missing['state']=='not_applied' and missing['status']=='warning' and '<your Work Copilot launcher> --data-dir ' in missing['fix'] and ' overlay apply <file>' in missing['fix'] and 'work-copilot overlay' not in missing['fix'] and 'work_copilot_overlay.py' in missing['fix']
    marker=tmp_path/'overlay.json'
    marker.write_text(json.dumps({'name':'fictional-co','placeholder':True,'applied_at':'2026-11-17T00:00:00Z'}))
    assert overlay_check(tmp_path)['state']=='placeholder'
    marker.write_text(json.dumps({'name':'fictional-co','applied_at':'2026-11-17T00:00:00Z'}))
    applied=overlay_check(tmp_path)
    assert applied['status']=='ok' and applied['ok'] is True and applied['name']=='fictional-co' and 'fix' not in applied
    marker.write_text('[]')
    assert overlay_check(tmp_path)['state']=='invalid'
    other=tmp_path/'org.json';other.write_text(json.dumps({'name':'x','applied_at':'t'}))
    assert overlay_check(tmp_path,overlay=other)['state']=='applied'


def test_doctor_new_checks_in_cli_output_and_read_only(tmp_path):
    import json,os,subprocess,sys
    home=tmp_path/'home';home.mkdir();vault=tmp_path/'vault';vault.mkdir()
    env={k:v for k,v in os.environ.items() if not k.startswith(('WORK_COPILOT_','CLAUDE_CONFIG'))}
    from pathlib import Path
    env.update(HOME=str(home),USERPROFILE=str(home),WORK_COPILOT_DATA_DIR=str(tmp_path/'data'),PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    out=subprocess.run([sys.executable,'-m','work_copilot','doctor','--vault',str(vault)],capture_output=True,text=True,env=env,cwd=tmp_path,timeout=60)
    checks=json.loads(out.stdout)['checks']
    for name in ('mcp_registration','vault_sync','overlay'):
        assert checks[name]['status'] in {'ok','warning','error'}
        assert checks[name]['ok'] or checks[name]['fix']
    assert checks['database']['path']==str((tmp_path/'data'/'copilot.db').resolve())
    assert sorted(p.name for p in tmp_path.iterdir())==['home','vault'] and list(home.iterdir())==[]


def test_doctor_mcp_registration_follows_scope_precedence_and_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'xdg'))
    right={'command':'work-copilot','args':['mcp'],'env':{'WORK_COPILOT_DATA_DIR':str(tmp_path/'data')}}
    wrong={'command':'work-copilot','args':['--data-dir',str(tmp_path/'other'),'mcp']}
    result=_registration(tmp_path,monkeypatch,user={'work-copilot':wrong},project={'work-copilot':right})
    assert result['status']=='ok' and result['scope']=='project'
    (tmp_path/'x').mkdir()
    unset=_registration(tmp_path/'x',monkeypatch,user={'work-copilot':{'command':'work-copilot','args':['mcp']}})
    assert unset['status']=='warning' and unset['registered_data_dir']==str(tmp_path/'xdg'/'work-copilot')
    monkeypatch.setenv('WORK_COPILOT_DATA_DIR',str(tmp_path/'inherited'))
    inherited=_registration(tmp_path/'x',monkeypatch,user={'work-copilot':{'command':'work-copilot','args':['mcp']}})
    assert inherited['registered_data_dir']==str(tmp_path/'inherited')


def test_doctor_mcp_registration_reads_data_dir_equals_form(tmp_path, monkeypatch):
    from work_copilot.doctor import _registered_data_dir
    assert _registered_data_dir({'args':['--data-dir='+str(tmp_path/'d'),'mcp']})==str(tmp_path/'d')
    assert _registered_data_dir({'args':['--data-dir',str(tmp_path/'a'),'mcp'],'env':{'WORK_COPILOT_DATA_DIR':'b'}})==str(tmp_path/'a')
    assert _registered_data_dir({'args':['mcp'],'env':{'WORK_COPILOT_DATA_DIR':'b'}})=='b'
    entry={'command':'work-copilot','args':['--data-dir='+str(tmp_path/'data'),'mcp']}
    assert _registration(tmp_path,monkeypatch,user={'work-copilot':entry})['status']=='ok'


def test_doctor_mcp_registration_local_scope_keyed_at_git_root(tmp_path, monkeypatch):
    import json
    from work_copilot.doctor import _mcp_registration_check
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    home=tmp_path/'home';home.mkdir();repo=tmp_path/'repo';(repo/'.git').mkdir(parents=True);sub=repo/'src'/'deep';sub.mkdir(parents=True)
    entry={'command':'work-copilot','args':['mcp'],'env':{'WORK_COPILOT_DATA_DIR':str(tmp_path/'data')}}
    (home/'.claude.json').write_text(json.dumps({'projects':{str(repo):{'mcpServers':{'work-copilot':entry}}}}))
    result=_mcp_registration_check(tmp_path/'data','UTC',home=home,cwd=sub)
    assert result['status']=='ok' and result['scope']=='local' and result['project_dir']==str(repo)
    assert str(repo) in result['note']
    (home/'.claude.json').unlink();(repo/'.mcp.json').write_text(json.dumps({'mcpServers':{'work-copilot':entry}}))
    project=_mcp_registration_check(tmp_path/'data','UTC',home=home,cwd=sub)
    assert project['scope']=='project' and project['path']==str(repo/'.mcp.json')


def test_doctor_rules_check(tmp_path):
    from work_copilot.doctor import rules_check
    assert rules_check(tmp_path)['state']=='defaults'
    (tmp_path/'rules.json').write_text('{}',encoding='utf-8')
    assert rules_check(tmp_path)['state']=='custom'
    (tmp_path/'rules.json').write_text('{not json',encoding='utf-8')
    bad=rules_check(tmp_path)
    assert bad['state']=='invalid' and not bad['ok'] and 'rules.json' in bad['path']
    (tmp_path/'rules.json').write_text('[]',encoding='utf-8')
    assert rules_check(tmp_path)['state']=='invalid'
