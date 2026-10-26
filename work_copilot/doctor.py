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


def _storage_check(settings):
    if settings['storage']=='protected-file':
        return {'ok':os.name=='posix','mode':'protected-file','note':'Explicit unencrypted POSIX 0600 storage; no token file opened or created. Use an OS keyring on Windows.'}
    try:
        import keyring
        backend=keyring.get_keyring()
        module=type(backend).__module__
        secure=any(module.startswith(prefix) for prefix in ('keyring.backends.SecretService','keyring.backends.macOS','keyring.backends.Windows','keyring.backends.kwallet','keyring.backends.libsecret'))
        return {'ok':secure,'mode':'keyring','backend':module,'note':'Secure OS backend detected; unlocked state and credential validity were not checked.' if secure else 'Configure a supported secure OS keyring backend; plaintext, chained and fail backends are rejected.'}
    except ImportError:
        return {'ok':False,'mode':'keyring','note':'Install the oauth-keyring extra and configure a secure OS keyring backend; no automatic plaintext fallback.'}
    except Exception:
        return {'ok':False,'mode':'keyring','note':'OS keyring backend is unavailable. Configure a secure OS keyring backend before explicit OAuth authorization.'}


def _database_details(db):
    integrity=db.execute('PRAGMA quick_check').fetchone()[0]
    connectors=[json.loads(row[0]) for row in db.execute('SELECT payload FROM connectors')]
    projects=db.execute('SELECT count(*) FROM projects').fetchone()[0]
    if not all(isinstance(item,dict) for item in connectors):
        raise ValueError('Invalid stored connector state')
    return integrity,connectors,projects


def _result(directory, database, connectors, projects, timezone_name, vault, config):
    checks={**_runtime_checks(),
        'database':database,
        'vault':{'ok':False,'status':'not_selected','note':'Select an existing work-only Markdown folder with --vault when ready.'},
        'connector':{'ok':False,'status':'not_configured','note':'Pass --config to inspect a private OAuth configuration offline.'},
        'projects':{'ok':projects>0,'count':projects,'next_step':'Create your first project in the dashboard or create_project MCP tool.' if not projects else 'Project identities configured.'},
    }
    if vault:
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
            ready=bool(selected and selected in settings.get('allowed_read_tools',[]))
            checks['connector']={'ok':bool(state and state.get('status')=='healthy'),'status':state.get('status') if state else 'auth_or_discovery_required','name':settings['name'],'allowed_read_tools':settings.get('allowed_read_tools',[]),'collection_configured':ready,'last_success':state.get('last_success') if state else None,'gap_since':state.get('gap_since') if state else None,'note':'Configuration inspected offline; remote schemas, capabilities and credential validity were not probed. '+('Read tool selected; verify its arguments and mapping with your administrator.' if ready else 'Run explicit connect --authorize when approved, then select a verified tool in allowed_read_tools and configure arguments/mapping before collection.')}
            checks['oauth_storage']=_storage_check(settings)
        except ValueError as exc:
            # load_config emits field-only validation errors, not input values.
            checks['connector']={'ok':False,'status':'invalid_config','note':str(exc)+'; see docs/SYMPHONY_MCP.md. No network request made.'}
        except OSError:
            checks['connector']={'ok':False,'status':'unreadable_config','note':'Configuration file is missing or unreadable; provide a readable private JSON file.'}
        except ImportError:
            checks['connector']={'ok':False,'status':'dependencies_missing','note':'Install project dependencies before validating OAuth configuration.'}
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
                    finally:
                        db.close()
                    database.update(ok=integrity=='ok',status='ready' if integrity=='ok' else 'corrupt',integrity=integrity,note='Read-only check of a private temporary DB/WAL snapshot; original files unchanged.')
    except (OSError,sqlite3.Error,ValueError,TypeError):
        database.update(ok=False,status='unreadable_or_invalid',note='Database is unreadable, incompatible or corrupt. Check permissions and restore a verified backup; doctor made no repairs.')
        connectors=[];projects=0
    return _result(directory,database,connectors,projects,timezone_name,vault,config)
