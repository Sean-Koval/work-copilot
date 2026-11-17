"""`init --wizard`: set up a vault, the first project and the Claude Code registration line.

Explicit and local: it creates only missing folders and one project, never overwrites a file,
and prints the `claude mcp add` command for you to run; it never runs `claude` itself.
"""
import os
from pathlib import Path
import shlex
import shutil
import sys

VAULT_FOLDERS = ('Notes', 'Meeting notes', 'Research', 'Decision write-ups')


def _quote(value, nt):
    """One shell word: POSIX sh, or a PowerShell single-quoted literal on Windows."""
    if nt:
        return "'"+value.replace("'", "''")+"'"
    return shlex.quote(value)


def mcp_add_command(data_dir, timezone_name=None, vault=None, nt=None):
    """The user-scope stdio registration that KAN-380 also uses; the data dir stays per machine."""
    nt = os.name == 'nt' if nt is None else nt
    executable = 'work-copilot'
    if not shutil.which(executable):
        # Installed in the checkout's .venv but not on PATH: name the script itself.
        local = Path(sys.executable).parent/('work-copilot.exe' if nt else 'work-copilot')
        if local.is_file():
            executable = str(local)
    env = [('WORK_COPILOT_DATA_DIR', str(data_dir))]
    if timezone_name:
        env.append(('WORK_COPILOT_TIMEZONE', timezone_name))
    if vault:
        env.append(('WORK_COPILOT_VAULT', str(vault)))
    words = ['claude', 'mcp', 'add', '--scope', 'user', '--transport', 'stdio', 'work-copilot']
    for key, value in env:
        words += ['--env', _quote(f'{key}={value}', nt)]
    # PowerShell consumes a bare `--` before a script shim (claude.ps1); quoted, it is passed on.
    words += ["'--'" if nt else '--', _quote(executable, nt) if executable != 'work-copilot' else executable, 'mcp']
    return ' '.join(words)


def _ask(label, value, default=None, interactive=False):
    if value:
        return value
    if not interactive:
        return default
    answer = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def _vault(path):
    """Apply the vault rule (no symlinked components), create the folder if only it is missing."""
    from .vault import _root
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Selected path must not contain symlinks')
    if not path.exists() and path.parent.is_dir():
        path.mkdir()
    root = _root(path)
    created = []
    for name in VAULT_FOLDERS:
        folder = root/name
        if folder.is_symlink():
            raise ValueError(f'Vault folder {name!r} must not be a symlink')
        if not folder.is_dir():
            folder.mkdir()  # FileExistsError if a file has that name: never overwritten
            created.append(name)
    return root, created


def run_wizard(args, data_dir, timezone_given, data_dir_given=False, interactive=None):
    """Return the summary lines to print. Prompts only for missing values on a terminal."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    interactive = sys.stdin.isatty() if interactive is None else interactive
    vault = _ask('Work-only Markdown vault folder', args.vault, interactive=interactive)
    project_id = _ask('First project id (stable, e.g. atlas)', args.project_id, interactive=interactive)
    project_name = _ask('First project name', args.project_name, interactive=interactive)
    missing = [flag for flag, value in (('--vault', vault), ('--project-id', project_id), ('--project-name', project_name)) if not value]
    if missing:
        raise ValueError('init --wizard needs '+', '.join(missing)+('' if interactive else ' when no terminal can prompt for them'))
    data = data_dir if data_dir_given else Path(_ask('Private data directory', None, str(data_dir), interactive)).expanduser().resolve()
    timezone_name = args.timezone if timezone_given else _ask('IANA timezone', None, args.timezone, interactive)
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f'Unknown IANA timezone: {timezone_name}') from None
    selected = Path(vault).expanduser().absolute().resolve()
    if data == selected or data.is_relative_to(selected):
        raise ValueError('Keep the private data directory outside the vault')
    root, created = _vault(vault)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        os.chmod(data, 0o700)
    from .store import Store
    store = Store(data/'copilot.db', timezone_name=timezone_name)
    try:
        project = store.create_project(project_id.strip(), project_name.strip())
    finally:
        store.close()
    from .doctor import sync_provider
    lines = [f'Vault: {root}' + (f" (created {', '.join(created)})" if created else ' (folders already there)'),
             f"Project: {project['id']} ({project['name']})",
             f'Private data: {data}',
             f'Timezone: {timezone_name}']
    provider = sync_provider(data)
    if provider:
        lines.append(f'Warning: the data directory is inside {provider}; never sync a live SQLite database.')
    lines += ['', 'Register the MCP server with Claude Code (run this yourself):',
              mcp_add_command(data, timezone_name, root)]
    return lines
