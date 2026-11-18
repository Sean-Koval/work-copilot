"""Offline, non-mutating diagnostics; no credential reads, OAuth grants or network."""
from importlib import metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


def _runtime_checks():
    versions={}
    for name in ('fastapi','uvicorn','mcp','filelock','tzdata'):
        try:
            versions[name]={'ok':True,'version':metadata.version(name)}
        except metadata.PackageNotFoundError:
            versions[name]={'ok':False,'note':'Install the project dependencies using the documented launcher.'}
    if versions['mcp'].get('version') not in (None,'1.30.0'):
        versions['mcp']['ok']=False
        versions['mcp']['note']='Reinstall project dependencies: this OAuth adapter is verified against MCP 1.30.0.'
    return {'python':{'ok':sys.version_info >= (3,11),'version':'.'.join(map(str,sys.version_info[:3])),'note':'Python 3.11 or newer is required.'},
            'dependencies':{'ok':all(v['ok'] for v in versions.values()),'packages':versions}}


def _storage_check(directory):
    """Inspect the keychain backend without reading credentials; flag leftover plaintext token files."""
    from .remote_mcp import KeychainUnavailable, secure_keyring
    try:
        backend=secure_keyring().get_keyring()
        result={'ok':True,'mode':'keyring','backend':type(backend).__module__,'note':'Secure OS keychain detected; unlocked state and credential validity were not checked.'}
    except KeychainUnavailable as exc:
        result={'ok':False,'mode':'keyring','note':str(exc)+'.'}
    oauth=directory/'oauth'
    try:
        leftovers=len(list(oauth.glob('*.json'))) if oauth.is_dir() and not oauth.is_symlink() else 0
    except OSError:
        leftovers=0
    if leftovers:
        # Presence only: the files are never opened.
        result.update(ok=False,legacy_token_files=leftovers,note=result['note']+' Delete the '+str(leftovers)+' unencrypted token file(s) left in oauth/ by the removed protected-file mode, then run connect --authorize.')
    return result


def is_link(path):
    """A symlink, or a Windows junction where Python can tell (3.12+)."""
    path = Path(path)
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


_SYNC_NAMES = (('onedrive', 'OneDrive'), ('dropbox', 'Dropbox'), ('google drive', 'Google Drive'),
               ('googledrive', 'Google Drive'), ('icloud drive', 'iCloud Drive'), ('com~apple~clouddocs', 'iCloud Drive'))


def sync_provider(path):
    """Name the sync service whose folder holds this path, from path names only (nothing is read)."""
    path = Path(path).expanduser().absolute()
    parts = [part.casefold() for part in path.parts]
    for part in parts:
        for prefix, name in _SYNC_NAMES:
            if part.startswith(prefix):
                return name
    for parent, part in zip(parts, parts[1:]):
        if parent == 'library' and part == 'mobile documents':
            return 'iCloud Drive'
        if parent == 'library' and part == 'cloudstorage':
            return 'a synced cloud folder (Library/CloudStorage)'
    return None


def _vault_sync_check(vault):
    path = Path(vault).expanduser().absolute()
    try:
        linked = any(is_link(p) for p in (path, *path.parents))
    except OSError:
        linked = False
    if linked:
        try:
            fix = f'Select the real folder: --vault {path.resolve()}'
        except (OSError, RuntimeError):
            fix = 'Select the real folder, without symlinked components.'
        return {'ok':False,'status':'error','path':str(path),'note':'Selected path must not contain symlinks: the vault rule refuses it.','fix':fix}
    provider = sync_provider(path)
    if provider:
        return {'ok':False,'status':'warning','path':str(path),'sync':provider,'note':f'The vault sits inside {provider}: company notes would be copied into that sync service, which may be personal.','fix':'Move the vault to a local work-only folder outside synced folders, unless your company approves this location.'}
    return {'ok':True,'status':'ok','path':str(path),'note':'Not inside a known sync folder (OneDrive, iCloud, Dropbox, Google Drive) and no symlinks.'}


def _same_path(a, b):
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _registered(servers):
    return isinstance(servers, dict) and isinstance(servers.get('work-copilot'), dict)


def _registered_data_dir(entry):
    """The data dir a registration selects: --data-dir in args, else WORK_COPILOT_DATA_DIR in its env."""
    env = entry.get('env') if isinstance(entry.get('env'), dict) else {}
    data = env.get('WORK_COPILOT_DATA_DIR')
    args = entry.get('args') if isinstance(entry.get('args'), list) else []
    for index, arg in enumerate(args):
        if arg == '--data-dir' and index+1 < len(args):
            data = args[index+1]
        elif isinstance(arg, str) and arg.startswith('--data-dir='):
            data = arg.split('=', 1)[1]
    return data if isinstance(data, str) and data else None


def project_root(cwd):
    """Claude Code keys local scope by the git root and reads .mcp.json there; else the cwd."""
    cwd = Path(cwd)
    for directory in (cwd, *cwd.parents):
        try:
            if (directory/'.git').exists():
                return directory
        except OSError:
            break
    return cwd


def _mcp_registration_check(directory, timezone_name=None, vault=None, home=None, cwd=None):
    """Read the Claude Code user settings and the project's .mcp.json only; never runs `claude` or writes its settings."""
    from .wizard import mcp_add_command
    fix = mcp_add_command(Path(directory).expanduser().absolute(), timezone_name, vault)
    try:
        root = project_root(Path.cwd() if cwd is None else Path(cwd))
        config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
        user_file = Path(config_dir)/'.claude.json' if config_dir else (Path.home() if home is None else Path(home))/'.claude.json'
    except (OSError, RuntimeError):
        return {'ok':False,'status':'warning','note':'Cannot locate the home directory to read Claude Code settings.','fix':fix}
    keys = [str(root)] + ([root.as_posix()] if os.name == 'nt' else [])

    def local(config):
        projects = config.get('projects') or {}
        return next((projects[k].get('mcpServers') for k in keys if isinstance(projects.get(k), dict)), None)

    found = []; unreadable = []
    # Claude Code's precedence: local, then project, then user scope.
    for scope, path, servers in (('local', user_file, local),
                                 ('project', root/'.mcp.json', lambda c: c.get('mcpServers')),
                                 ('user', user_file, lambda c: c.get('mcpServers'))):
        try:
            if not path.is_file():
                continue
            config = json.loads(path.read_text(encoding='utf-8'))
            selected = servers(config) if isinstance(config, dict) else None
        except (OSError, ValueError, AttributeError):
            unreadable.append(str(path))
            continue
        if _registered(selected):
            found.append((scope, path, selected['work-copilot']))
    checked = f'Checked project directory: {root}.'
    if not found:
        note = f'No work-copilot MCP server registered for Claude Code (user scope, or local or project scope for this project). {checked}'
        if unreadable:
            note += ' Unreadable or invalid: '+', '.join(dict.fromkeys(unreadable))+'.'
        return {'ok':False,'status':'warning','project_dir':str(root),'note':note,'fix':fix}
    scope, path, entry = found[0]
    result = {'ok':True,'status':'ok','scope':scope,'path':str(path),'project_dir':str(root),'note':f'Registered in Claude Code ({scope} scope); not started or contacted. {checked}'}
    registered = _registered_data_dir(entry)
    if registered is None:
        # Unset in the registration: the server inherits Claude Code's environment, else the default.
        registered = os.environ.get('WORK_COPILOT_DATA_DIR')
    if not registered:
        from .cli import default_data_dir
        registered = str(default_data_dir())
    if _same_path(registered, directory) is False:
        result.update(ok=False,status='warning',registered_data_dir=registered,note=f'Registered ({scope} scope), but with another data directory: the agent and this checkout would see different state. {checked}',fix='claude mcp remove --scope '+scope+' work-copilot, then '+fix)
    elif scope == 'project':
        result['note'] += ' Project-scoped servers need your approval when Claude Code asks.'
    return result


def overlay_check(directory, overlay=None):
    """Report the company overlay marker. KAN-381 adds `overlay apply`, which writes it."""
    path = Path(overlay) if overlay else Path(directory)/'overlay.json'
    fix = 'An overlay is applied by `work-copilot overlay apply <org.yaml>` (KAN-381, not yet available).'
    try:
        if not path.exists():
            return {'ok':False,'status':'warning','state':'not_applied','note':'No company overlay applied; defaults are generic.','fix':fix}
        if path.is_symlink():
            raise ValueError('symlink')
        marker = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(marker, dict):
            raise ValueError('not an object')
    except (OSError, ValueError):
        return {'ok':False,'status':'warning','state':'invalid','path':str(path),'note':'Overlay marker is unreadable, invalid or a symlink; it was not changed.','fix':fix}
    details = {k: marker.get(k) for k in ('name','applied_at') if isinstance(marker.get(k), str)}
    if marker.get('placeholder'):
        return {'ok':False,'status':'warning','state':'placeholder','path':str(path),**details,'note':'A placeholder overlay is applied; replace it with your company overlay.','fix':fix}
    if details.get('name') and details.get('applied_at'):
        return {'ok':True,'status':'ok','state':'applied','path':str(path),**details,'note':'Company overlay applied.'}
    return {'ok':False,'status':'warning','state':'invalid','path':str(path),'note':'Overlay marker lacks name or applied_at.','fix':fix}


def rules_check(directory):
    """Read the team rules.json beside the database without changing it; the store refuses to open on a bad one."""
    path = Path(directory)/'rules.json'
    if not path.exists():
        return {'ok':True,'status':'ok','state':'defaults','note':'No rules.json; the built-in draft rules apply.'}
    try:
        if path.is_symlink():
            raise ValueError('rules.json is a symlink')
        try:
            from .rules import Rules
        except ImportError:
            Rules = None
        if Rules is not None and hasattr(Rules, 'load'):
            Rules.load(Path(directory))
        else:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('rules.json must be a JSON object')
    except (OSError, ValueError, RecursionError) as exc:
        return {'ok':False,'status':'error','state':'invalid','path':str(path),'note':f'Team rules are invalid, so the dashboard and MCP server will not start: {exc}','fix':'Fix or remove rules.json, then run doctor again.'}
    return {'ok':True,'status':'ok','state':'custom','path':str(path),'note':'Team rules.json read and valid.'}


def _database_details(db):
    integrity=db.execute('PRAGMA quick_check').fetchone()[0]
    connectors=[json.loads(row[0]) for row in db.execute('SELECT payload FROM connectors')]
    projects=db.execute('SELECT count(*) FROM projects').fetchone()[0]
    if not all(isinstance(item,dict) for item in connectors):
        raise ValueError('Invalid stored connector state')
    return integrity,connectors,projects


def _backup_status(database_path):
    from .schema import backups, BACKUP_MAX_AGE_DAYS
    found=backups(database_path)
    oldest=found[0].name.rsplit('-',1)[-1].removesuffix('.db') if found else None
    return {'count':len(found),'oldest':oldest,'folder':str(Path(database_path).parent/'backups'),'note':f'Pre-migration copies of all captured text; removed after {BACKUP_MAX_AGE_DAYS} days.'}


def _schema_status(user_version):
    from .schema import SCHEMA_VERSION
    if user_version>SCHEMA_VERSION:
        return {'ok':False,'status':'newer_schema','schema_version':user_version,'supported_schema_version':SCHEMA_VERSION,'note':'This database was written by a newer Work Copilot; upgrade before opening it.'}
    status={'schema_version':user_version,'supported_schema_version':SCHEMA_VERSION}
    if user_version<SCHEMA_VERSION:
        status['schema_note']='The next start migrates this database, after copying it to backups/.'
    return status


def _result(directory, database, connectors, projects, timezone_name, vault, config):
    checks={**_runtime_checks(),
        'database':database,
        'vault':{'ok':False,'status':'not_selected','note':'Select an existing work-only Markdown folder with --vault when ready.'},
        'connector':{'ok':False,'status':'not_configured','note':'Pass --config to inspect a private OAuth configuration offline.'},
        'projects':{'ok':projects>0,'count':projects,'next_step':'Create your first project with init --wizard, the dashboard or the create_project MCP tool.' if not projects else 'Project identities configured.'},
        'overlay':overlay_check(directory),
        'rules':rules_check(directory),
    }
    if vault:
        checks['vault_sync']=_vault_sync_check(vault)
        from .vault import _root
        try:
            root=_root(vault)
            checks['vault']={'ok':os.access(root,os.R_OK),'path':str(root),'writable':os.access(root,os.W_OK),'status':'selected','note':'Read permission checked; no notes read or written.'}
        except (ValueError,OSError):
            checks['vault']={'ok':False,'status':'unavailable','note':'Select an existing work-only folder without symlink components.'}
    if config:
        try:
            from .remote_mcp import load_config
            settings=load_config(config)
            state=next((c for c in connectors if c.get('name')==settings['name']),None)
            selected=settings.get('tool')
            from .remote_mcp import capture_settings
            capture=capture_settings(settings)
            ready=bool(selected and selected in settings.get('allowed_read_tools',[]) and capture)
            checks['connector']={'ok':bool(state and state.get('status')=='healthy'),'status':state.get('status') if state else 'auth_or_discovery_required','name':settings['name'],'allowed_read_tools':settings.get('allowed_read_tools',[]),'collection_configured':ready,'capture':{'rooms':capture['rooms'],'spaces':capture['spaces'],'retention_days':capture['retention_days']} if capture else {'rooms':[],'spaces':[],'retention_days':None,'note':'No rooms or spaces picked; collection is refused until capture.rooms or capture.spaces is set.'},'last_success':state.get('last_success') if state else None,'gap_since':state.get('gap_since') if state else None,'note':'Configuration inspected offline; remote schemas, capabilities and credential validity were not probed. '+('Read tool selected; verify its arguments and mapping with your administrator.' if ready else 'Run explicit connect --authorize when approved, then select a verified tool in allowed_read_tools and configure arguments/mapping before collection.')}
            checks['oauth_storage']=_storage_check(directory)
        except ValueError as exc:
            # load_config emits field-only validation errors, not input values.
            checks['connector']={'ok':False,'status':'invalid_config','note':str(exc)+'; see docs/SYMPHONY_MCP.md. No network request made.'}
        except OSError:
            checks['connector']={'ok':False,'status':'unreadable_config','note':'Configuration file is missing or unreadable; provide a readable private JSON file.'}
        except ImportError:
            checks['connector']={'ok':False,'status':'dependencies_missing','note':'Install project dependencies before validating OAuth configuration.'}
    checks['mcp_registration']=_mcp_registration_check(directory,timezone_name,checks['vault'].get('path') if checks['vault']['ok'] else None)
    queue=directory/'activity-queue'
    try:
        checks['activity_queue']={'pending':len(list(queue.glob('*.event.json'))) if queue.is_dir() and not queue.is_symlink() else 0,'errors':len(list(queue.glob('*.error.json'))) if queue.is_dir() and not queue.is_symlink() else 0,'note':'Use drain-activity to process explicit queued receipts; nothing runs automatically.'}
    except OSError:
        checks['activity_queue']={'pending':None,'errors':None,'note':'Activity queue is unreadable; check directory permissions.'}
    return {'checks':checks,'connectors':connectors,'timezone':timezone_name,'network_used':False,'external_sends':False,'next_steps':['Start serve with --vault and optional --connector-config.','Run connect --authorize only when ready to grant approved OAuth access.','Use structured session handoffs or approved JSON imports for agent work; no private trace directories are scanned.']}


def diagnose(store, data_dir, vault=None, config=None):
    """Compatibility API; never query the live connection or modify WAL read marks."""
    return _diagnose_path(data_dir, vault, config, store.timezone_name, Path(store.path))


def diagnose_path(data_dir, vault=None, config=None, timezone_name='UTC'):
    return _diagnose_path(data_dir, vault, config, timezone_name, Path(data_dir).expanduser()/'copilot.db')


def _diagnose_path(data_dir, vault, config, timezone_name, database_path):
    """Inspect without constructing Store or touching original database/sidecars.

    A private temporary DB+WAL snapshot allows SQLite read-only diagnostics without
    creating -shm in the user's directory. Concurrent snapshot changes fail closed
    with a retry message. Temporary copies are removed before returning.
    """
    directory=Path(data_dir).expanduser()
    path=database_path.expanduser()
    database={'ok':False,'path':str(path),'directory_writable':directory.is_dir() and os.access(directory,os.W_OK),'note':'No original files, permissions or database schema changed.'}
    connectors=[];projects=0
    try:
        if not path.exists():
            database.update(status='not_initialized',note='No database found. Run init or the documented launcher when ready; doctor created nothing.')
        elif path.is_symlink():
            database.update(status='unsafe_path',note='Database is a symlink; select a direct app-data directory.')
        else:
            files=[path]
            wal=path.with_name(path.name+'-wal')
            if wal.exists():
                if wal.is_symlink():
                    raise ValueError('Unsafe WAL')
                files.append(wal)
            signatures={p:(p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ino) for p in files}
            with tempfile.TemporaryDirectory(prefix='work-copilot-doctor-') as temporary:
                copied=Path(temporary)/path.name
                for source in files:
                    target=Path(temporary)/source.name
                    with source.open('rb') as incoming, target.open('xb') as outgoing:
                        os.chmod(target,0o600)
                        import shutil
                        shutil.copyfileobj(incoming,outgoing)
                if any((p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ino)!=signature for p,signature in signatures.items()) or wal.exists()!=(wal in files):
                    database.update(status='busy',note='Database changed during offline snapshot; retry when collection is idle.')
                else:
                    db=sqlite3.connect(copied.as_uri()+'?mode=ro',uri=True)
                    try:
                        integrity,connectors,projects=_database_details(db)
                        user_version=db.execute('PRAGMA user_version').fetchone()[0]
                    finally:
                        db.close()
                    database.update(ok=integrity=='ok',status='ready' if integrity=='ok' else 'corrupt',integrity=integrity,note='Read-only check of a private temporary DB/WAL snapshot; original files unchanged.')
                    database.update(_schema_status(user_version))
                    database['backups']=_backup_status(path)
    except (OSError,sqlite3.Error,ValueError,TypeError):
        database.update(ok=False,status='unreadable_or_invalid',note='Database is unreadable, incompatible or corrupt. Check permissions and restore a verified backup; doctor made no repairs.')
        connectors=[];projects=0
    return _result(directory,database,connectors,projects,timezone_name,vault,config)
