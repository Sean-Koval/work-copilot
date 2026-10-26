import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(*args,env=None):
    return subprocess.run([sys.executable,'-m','work_copilot',*map(str,args)],capture_output=True,text=True,env=env,timeout=60)


def test_doctor_missing_state_does_not_create_directory(tmp_path):
    selected=tmp_path/'missing'
    result=run_cli('--data-dir',selected,'doctor')
    assert not selected.exists()
    assert 'Traceback' not in result.stderr
    assert json.loads(result.stdout)['checks']['database']['ok'] is False


def test_startup_failure_is_actionable_without_traceback(tmp_path):
    file=tmp_path/'not-a-directory';file.write_text('keep')
    result=run_cli('--data-dir',file,'init')
    assert result.returncode!=0
    assert 'Traceback' not in result.stderr
    assert 'Error:' in result.stderr
    assert file.read_text()=='keep'


def test_cli_smoke_isolated_from_selected_user_data(tmp_path):
    user=tmp_path/'user-state';user.mkdir();sentinel=user/'keep.txt';sentinel.write_text('untouched')
    env={**os.environ,'WORK_COPILOT_VAULT':str(tmp_path/'not-a-vault'),'WORK_COPILOT_HOOK_ACTIVE':'1','WORK_COPILOT_TIMEZONE':'Invalid/Timezone'}
    result=run_cli('--data-dir',user,'smoke',env=env)
    assert result.returncode==0,result.stderr+result.stdout
    report=json.loads(result.stdout)
    assert report['success'] and report['synthetic_only']
    assert list(user.iterdir())==[sentinel]
    assert sentinel.read_text()=='untouched'


def test_invalid_selected_vault_fails_before_service_start(tmp_path):
    result=run_cli('--data-dir',tmp_path/'data','serve','--vault',tmp_path/'missing')
    assert result.returncode!=0
    assert 'Traceback' not in result.stderr
    assert 'existing vault' in result.stderr
