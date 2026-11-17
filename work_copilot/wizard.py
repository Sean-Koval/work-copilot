"""`init --wizard`: set up a vault, the first project and the Claude Code registration line.

Explicit and local: it creates only missing folders and one project, never overwrites a file,
and prints the `claude mcp add` command for you to run; it never runs `claude` itself.
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys

VAULT_FOLDERS = ('Notes', 'Meeting notes', 'Research', 'Decision write-ups')
# PowerShell treats the typographic single quotes as quote characters too; each is escaped by doubling.
_PS_QUOTES = re.compile("(['‘’‚‛])")


def _quote(value, nt):
    """One shell word: POSIX sh, or a PowerShell single-quoted literal on Windows."""
    if nt:
        return "'"+_PS_QUOTES.sub(r'\1\1', value)+"'"
    return shlex.quote(value)


def mcp_add_command(data_dir, timezone_name=None, vault=None, nt=None):
    """The user-scope stdio registration that KAN-380 also uses; the data dir stays per machine."""
    nt = os.name == 'nt' if nt is None else nt
    # An absolute path: Claude started from another shell may not have this venv on PATH.
    executable = shutil.which('work-copilot')
    if not executable:
        # Installed in the checkout's .venv but not on PATH: name the script itself.
        local = Path(sys.executable).parent/('work-copilot.exe' if nt else 'work-copilot')
        executable = str(local) if local.is_file() else None
    env = [('WORK_COPILOT_DATA_DIR', str(data_dir))]
    if timezone_name:
        env.append(('WORK_COPILOT_TIMEZONE', timezone_name))
    if vault:
        env.append(('WORK_COPILOT_VAULT', str(vault)))
    words = ['claude', 'mcp', 'add', '--scope', 'user', '--transport', 'stdio', 'work-copilot']
    for key, value in env:
        words += ['--env', _quote(f'{key}={value}', nt)]
    # PowerShell consumes a bare `--` before a script shim (claude.ps1); quoted, it is passed on.
    words += ["'--'" if nt else '--', _quote(executable, nt) if executable else 'work-copilot', 'mcp']
    return ' '.join(words)


def _ask(label, value, default=None, interactive=False):
    if isinstance(value, str):
        value = value.strip()
    if value:
        return value
    if not interactive:
        return default
    answer = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def _case_insensitive():
    """macOS and Windows file systems ignore case by default."""
    return sys.platform in ('darwin', 'win32') or os.name == 'nt'


def _inside(child, parent):
    child, parent = Path(child), Path(parent)
    if _case_insensitive():
        child, parent = (Path(os.path.normcase(str(p)).casefold()) for p in (child, parent))
    return child == parent or child.is_relative_to(parent)


def _vault_plan(path):
    """Apply the vault rule (no symlinked components) and name what is missing; create nothing."""
    from .doctor import is_link
    path = Path(path).expanduser().absolute()
    if any(is_link(p) for p in (path, *path.parents)):
        raise ValueError('Selected path must not contain symlinks')
    if not path.is_dir() and not path.parent.is_dir():
        raise ValueError('Select an existing vault directory, or a new one inside an existing folder')
    if path.exists() and not path.is_dir():
        raise ValueError('Select an existing vault directory')
    missing = []
    for name in VAULT_FOLDERS:
        folder = path/name
        if is_link(folder):
            raise ValueError(f'Vault folder {name!r} must not be a symlink')
        if folder.exists() and not folder.is_dir():
            raise ValueError(f'{folder} is a file where the {name!r} folder belongs; it was not overwritten')
        if not folder.exists():
            missing.append(name)
    return path.resolve(), missing


def _check_project(data, timezone_name, project_id, project_name):
    """Refuse a conflicting existing project before anything is created."""
    database = data/'copilot.db'
    if not database.is_file():
        return
    from .store import Store
    store = Store(database, timezone_name=timezone_name)
    try:
        try:
            existing = store.project(project_id)
        except KeyError:
            return
    finally:
        store.close()
    if existing['name'] != project_name or existing['aliases'] or existing['configured_rooms']:
        raise ValueError(f'Project {project_id!r} already exists as {existing["name"]!r}; existing setup was not overwritten')


def run_wizard(args, data_dir, timezone_given, data_dir_given=False, interactive=None):
    """Return the summary lines to print. Prompts only for missing values on a terminal.

    Everything is validated before the first folder, data directory or database is created.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from .doctor import is_link, sync_provider
    interactive = sys.stdin.isatty() if interactive is None else interactive
    vault = _ask('Work-only Markdown vault folder', args.vault, interactive=interactive)
    project_id = _ask('First project id (stable, e.g. atlas)', args.project_id, interactive=interactive)
    project_name = _ask('First project name', args.project_name, interactive=interactive)
    missing = [flag for flag, value in (('--vault', vault), ('--project-id', project_id), ('--project-name', project_name)) if not value]
    if missing:
        raise ValueError('init --wizard needs '+', '.join(missing)+('' if interactive else ' when no terminal can prompt for them'))
    if len(project_id) > 200 or len(project_name) > 300:
        raise ValueError('Project id must be at most 200 characters and its name at most 300')
    chosen = (getattr(args, 'data_dir', None) or data_dir) if data_dir_given else _ask('Private data directory', None, str(data_dir), interactive)
    chosen = Path(chosen).expanduser().absolute()
    if is_link(chosen):
        raise ValueError('The private data directory must not be a symlink; select the real folder')
    data = chosen.resolve()
    timezone_name = args.timezone if timezone_given else _ask('IANA timezone', None, args.timezone, interactive)
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f'Unknown IANA timezone: {timezone_name}') from None
    selected = Path(vault).expanduser().absolute().resolve()
    if _inside(data, selected):
        raise ValueError('Keep the private data directory outside the vault')
    if data.exists() and not data.is_dir():
        raise ValueError(f'{data} exists and is not a directory')
    root, absent = _vault_plan(vault)
    _check_project(data, timezone_name, project_id, project_name)
    # Validated: now create.
    root.mkdir(exist_ok=True)
    for name in absent:
        (root/name).mkdir()
    lines = [f'Vault: {root}' + (f" (created {', '.join(absent)})" if absent else ' (folders already there)')]
    data_created = not data.exists()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    warnings = []
    if os.name == 'posix':
        if data_created:
            os.chmod(data, 0o700)
        elif stat.S_IMODE(data.stat().st_mode) & 0o077:
            warnings.append(f'Warning: the existing data directory is readable by other users (mode {stat.S_IMODE(data.stat().st_mode):o}); it was left as is. Consider chmod 700 {shlex.quote(str(data))}.')
    from .store import Store
    store = Store(data/'copilot.db', timezone_name=timezone_name)
    try:
        project = store.create_project(project_id, project_name)
    finally:
        store.close()
    lines += [f"Project: {project['id']} ({project['name']})",
              f'Private data: {data}',
              f'Timezone: {timezone_name}', *warnings]
    provider = sync_provider(root)
    if provider:
        lines.append(f'Warning: the vault is inside {provider}; company notes would be copied into that sync service, which may be personal.')
    provider = sync_provider(data)
    if provider:
        lines.append(f'Warning: the data directory is inside {provider}; never sync a live SQLite database.')
    where = 'in PowerShell (not cmd.exe)' if os.name == 'nt' else 'in your shell'
    lines += ['', f'Register the MCP server with Claude Code (run this yourself, {where}):',
              mcp_add_command(data, timezone_name, root)]
    return lines
