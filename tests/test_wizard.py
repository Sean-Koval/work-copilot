import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from work_copilot.store import Store
from work_copilot import wizard
from work_copilot.wizard import VAULT_FOLDERS, mcp_add_command, run_wizard

REPO = Path(__file__).resolve().parents[1]


def run_cli(*args, env=None):
    return subprocess.run([sys.executable, '-m', 'work_copilot', *map(str, args)], capture_output=True, text=True,
                          env=env, timeout=60, stdin=subprocess.DEVNULL)


def isolated_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('WORK_COPILOT_', 'XDG_', 'CLAUDE_CONFIG'))}
    home = tmp_path/'home'; home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO), **extra)
    return env


def wizard_args(vault=None, project_id=None, project_name=None, timezone='UTC'):
    return SimpleNamespace(vault=vault, project_id=project_id, project_name=project_name, timezone=timezone)


def test_wizard_creates_folders_project_and_prints_registration(tmp_path):
    vault, data = tmp_path/'Work Vault', tmp_path/'private data'
    result = run_cli('--timezone', 'America/New_York', '--data-dir', data, 'init', '--wizard', '--vault', vault,
                     '--project-id', 'atlas', '--project-name', 'Fictional Atlas', env=isolated_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in vault.iterdir()) == sorted(VAULT_FOLDERS)
    store = Store(data/'copilot.db')
    try:
        assert store.project('atlas')['name'] == 'Fictional Atlas'
    finally:
        store.close()
    line = result.stdout.strip().splitlines()[-1]
    assert line.startswith('claude mcp add --scope user --transport stdio work-copilot --env ')
    assert (" '--' " if os.name == 'nt' else ' -- ') in line and line.endswith(' mcp')
    assert 'WORK_COPILOT_TIMEZONE=America/New_York' in line
    assert not (tmp_path/'home'/'.claude.json').exists()


def test_wizard_is_idempotent_and_never_overwrites(tmp_path):
    vault, data = tmp_path/'vault', tmp_path/'data'
    vault.mkdir(); (vault/'Notes').mkdir(); (vault/'Notes'/'keep.md').write_text('mine')
    args = wizard_args(vault, 'atlas', 'Atlas')
    first = run_wizard(args, data, True, True, interactive=False)
    assert 'created Meeting notes, Research, Decision write-ups' in first[0]
    second = run_wizard(args, data, True, True, interactive=False)
    assert 'folders already there' in second[0]
    assert (vault/'Notes'/'keep.md').read_text() == 'mine'
    with pytest.raises(ValueError, match='not overwritten'):
        run_wizard(wizard_args(vault, 'atlas', 'Different name'), data, True, True, interactive=False)


def test_wizard_refuses_a_file_where_a_folder_belongs(tmp_path):
    vault = tmp_path/'vault'; vault.mkdir(); (vault/'Research').write_text('a file')
    with pytest.raises(ValueError, match='not overwritten'):
        run_wizard(wizard_args(vault, 'atlas', 'Atlas'), tmp_path/'data', True, True, interactive=False)
    assert (vault/'Research').read_text() == 'a file'
    assert sorted(p.name for p in vault.iterdir()) == ['Research'] and not (tmp_path/'data').exists()


def test_wizard_conflicting_project_creates_nothing(tmp_path):
    data = tmp_path/'data'
    run_wizard(wizard_args(tmp_path/'first', 'atlas', 'Atlas'), data, True, True, interactive=False)
    with pytest.raises(ValueError, match='not overwritten'):
        run_wizard(wizard_args(tmp_path/'second', 'atlas', 'Other'), data, True, True, interactive=False)
    assert not (tmp_path/'second').exists()


def test_wizard_blank_project_id_creates_nothing(tmp_path):
    result = run_cli('--data-dir', tmp_path/'data', 'init', '--wizard', '--vault', tmp_path/'vault',
                     '--project-id', ' ', '--project-name', 'Atlas', env=isolated_env(tmp_path))
    assert result.returncode == 1 and 'Traceback' not in result.stderr and '--project-id' in result.stderr
    assert not (tmp_path/'data').exists() and not (tmp_path/'vault').exists()


def test_wizard_strips_project_id_and_name(tmp_path):
    lines = run_wizard(wizard_args(tmp_path/'vault', ' atlas ', ' Atlas '), tmp_path/'data', True, True, interactive=False)
    assert 'Project: atlas (Atlas)' in lines


@pytest.mark.parametrize('error', [EOFError, KeyboardInterrupt])
def test_wizard_interrupted_prompt_creates_nothing(tmp_path, monkeypatch, capsys, error):
    def interrupted(prompt):
        raise error
    monkeypatch.setattr('builtins.input', interrupted)
    monkeypatch.setattr('work_copilot.wizard.stdin_is_terminal', lambda stream=None: True)
    for name in ('HOME', 'USERPROFILE', 'XDG_DATA_HOME', 'CLAUDE_CONFIG_DIR'):
        monkeypatch.setenv(name, str(tmp_path/'home'))
    monkeypatch.setenv('WORK_COPILOT_TIMEZONE', 'UTC')  # _run sets it; restored afterwards
    monkeypatch.delenv('WORK_COPILOT_DATA_DIR', raising=False)
    monkeypatch.setattr('sys.argv', ['work-copilot', '--data-dir', str(tmp_path/'data'), 'init', '--wizard', '--vault', str(tmp_path/'vault')])
    from work_copilot.cli import _run
    with pytest.raises(SystemExit) as exit_info:
        _run()
    assert exit_info.value.code == 1
    assert 'nothing was created' in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_wizard_new_data_dir_is_private(tmp_path):
    if os.name != 'posix':
        pytest.skip('POSIX modes')
    run_wizard(wizard_args(tmp_path/'vault', 'atlas', 'Atlas'), tmp_path/'data', True, True, interactive=False)
    assert stat.S_IMODE((tmp_path/'data').stat().st_mode) == 0o700


def test_wizard_leaves_existing_data_dir_mode_and_warns(tmp_path):
    if os.name != 'posix':
        pytest.skip('POSIX modes')
    data = tmp_path/'data'; data.mkdir(); os.chmod(data, 0o755)
    lines = run_wizard(wizard_args(tmp_path/'vault', 'atlas', 'Atlas'), data, True, True, interactive=False)
    assert stat.S_IMODE(data.stat().st_mode) == 0o755
    assert any('readable by other users' in line and '755' in line for line in lines)


def test_wizard_refuses_symlinked_data_dir(tmp_path):
    real = tmp_path/'real'; real.mkdir(); link = tmp_path/'link'
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    args = wizard_args(tmp_path/'vault', 'atlas', 'Atlas'); args.data_dir = link
    with pytest.raises(ValueError, match='must not be a symlink'):
        run_wizard(args, real, True, True, interactive=False)
    assert list(real.iterdir()) == [] and not (tmp_path/'vault').exists()


def test_wizard_warns_when_vault_is_synced(tmp_path):
    (tmp_path/'Dropbox').mkdir()
    lines = run_wizard(wizard_args(tmp_path/'Dropbox'/'vault', 'atlas', 'Atlas'), tmp_path/'data', True, True, interactive=False)
    assert any('vault is inside Dropbox' in line for line in lines)


def test_wizard_refuses_symlinked_vault(tmp_path):
    real = tmp_path/'real'; real.mkdir()
    link = tmp_path/'link'
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError, match='must not contain symlinks'):
        run_wizard(wizard_args(link, 'atlas', 'Atlas'), tmp_path/'data', True, True, interactive=False)
    assert list(real.iterdir()) == [] and not (tmp_path/'data').exists()


def test_wizard_without_terminal_names_missing_flags_and_creates_nothing(tmp_path):
    result = run_cli('--data-dir', tmp_path/'data', 'init', '--wizard', '--vault', tmp_path/'vault', env=isolated_env(tmp_path))
    assert result.returncode == 1 and 'Traceback' not in result.stderr
    assert '--project-id' in result.stderr and '--project-name' in result.stderr
    assert not (tmp_path/'data').exists() and not (tmp_path/'vault').exists()


def test_wizard_prompts_only_for_missing_values(tmp_path, monkeypatch):
    answers = iter(['Atlas', '', 'Europe/Berlin'])
    asked = []
    monkeypatch.setattr('builtins.input', lambda prompt: asked.append(prompt) or next(answers))
    lines = run_wizard(wizard_args(tmp_path/'vault', 'atlas'), tmp_path/'data', False, interactive=True)
    assert [p.split(' [')[0].rstrip(': ') for p in asked] == ['First project name', 'Private data directory', 'IANA timezone']
    assert 'Timezone: Europe/Berlin' in lines


def test_wizard_keeps_data_dir_out_of_the_vault(tmp_path):
    vault = tmp_path/'vault'
    with pytest.raises(ValueError, match='outside the vault'):
        run_wizard(wizard_args(vault, 'atlas', 'Atlas'), vault/'state', True, True, interactive=False)
    assert not vault.exists()


def test_wizard_data_dir_check_ignores_case_where_the_file_system_does(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, '_case_insensitive', lambda: True)
    vault = tmp_path/'Vault'
    with pytest.raises(ValueError, match='outside the vault'):
        run_wizard(wizard_args(vault, 'atlas', 'Atlas'), tmp_path/'VAULT'/'state', True, True, interactive=False)
    assert not vault.exists() and not (tmp_path/'VAULT').exists()


def test_plain_init_unchanged_and_data_dir_env_default(tmp_path):
    data = tmp_path/'from-env'
    result = run_cli('init', env=isolated_env(tmp_path, WORK_COPILOT_DATA_DIR=str(data)))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'data_dir': str(data.resolve()), 'external_sends': False}
    assert sorted(p.name for p in (tmp_path/'home').iterdir()) == []


def test_empty_data_dir_env_means_default(tmp_path):
    result = run_cli('init', env=isolated_env(tmp_path, WORK_COPILOT_DATA_DIR='', XDG_DATA_HOME=str(tmp_path/'xdg')))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['data_dir'] == str((tmp_path/'xdg'/'work-copilot').resolve())


def test_mcp_add_command_quotes_for_posix_and_powershell(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: '/opt/it is/'+name)
    posix = mcp_add_command("/tmp/it's data", nt=False)
    assert posix == ("claude mcp add --scope user --transport stdio work-copilot "
                     "--env 'WORK_COPILOT_DATA_DIR=/tmp/it'\"'\"'s data' -- '/opt/it is/work-copilot' mcp")
    ps = mcp_add_command("C:\\Users\\me\\it's data", nt=True)
    assert ps == ("claude mcp add --scope user --transport stdio work-copilot "
                  "--env 'WORK_COPILOT_DATA_DIR=C:\\Users\\me\\it''s data' '--' '/opt/it is/work-copilot' mcp")


def test_powershell_quoting_doubles_typographic_single_quotes(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: None)
    monkeypatch.setattr('sys.executable', '/nonexistent/python')
    line = mcp_add_command('C:\\data \u2018a\u2019 \u201ab\u201b', nt=True)
    assert "'WORK_COPILOT_DATA_DIR=C:\\data \u2018\u2018a\u2019\u2019 \u201a\u201ab\u201b\u201b'" in line
    assert line.endswith(" '--' work-copilot mcp")


def test_wizard_names_powershell_on_windows(tmp_path, monkeypatch):
    lines = run_wizard(wizard_args(tmp_path/'vault', 'atlas', 'Atlas'), tmp_path/'data', True, True, interactive=False)
    assert ('in PowerShell (not cmd.exe)' if os.name == 'nt' else 'in your shell') in lines[-2]


def test_stdin_is_terminal_needs_a_real_terminal():
    from work_copilot.wizard import stdin_is_terminal
    class NotTty:
        def isatty(self): return False
    class Closed:
        def isatty(self): raise ValueError('closed')
    assert stdin_is_terminal(NotTty()) is False and stdin_is_terminal(Closed()) is False
    with open(os.devnull) as null:  # the NUL device reports isatty() on Windows but has no console
        assert stdin_is_terminal(null) is False
