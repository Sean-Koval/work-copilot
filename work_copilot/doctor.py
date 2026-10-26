"""Offline diagnostics; never opens a browser, grants access or calls a source."""
import json
import os
from pathlib import Path


def diagnose(store, data_dir, vault=None, config=None):
    directory=Path(data_dir)
    with store._lock:
        integrity=store.db.execute('PRAGMA quick_check').fetchone()[0]
        connectors=[json.loads(row[0]) for row in store.db.execute('SELECT payload FROM connectors')]
        projects=store.db.execute('SELECT count(*) FROM projects').fetchone()[0]
    checks={
        'database':{'ok':integrity=='ok','integrity':integrity,'path':str(store.path),'directory_writable':os.access(directory,os.W_OK),'note':'Permission check only; no test file written.'},
        'vault':{'ok':False,'status':'not_selected'},
        'connector':{'ok':False,'status':'not_configured'},
        'projects':{'ok':projects>0,'count':projects,'next_step':'Create your first project in the dashboard or create_project MCP tool.' if not projects else 'Project identities configured.'},
    }
    if vault:
        from .vault import _root
        try:
            root=_root(vault)
            checks['vault']={'ok':os.access(root,os.R_OK),'path':str(root),'writable':os.access(root,os.W_OK),'status':'selected'}
        except (ValueError,OSError):
            checks['vault']={'ok':False,'status':'unavailable','note':'Select an existing work-only folder without symlink components.'}
    if config:
        from .remote_mcp import load_config
        try:
            settings=load_config(config)
            state=next((c for c in connectors if c['name']==settings['name']),None)
            checks['connector']={'ok':bool(state and state['status']=='healthy'),'status':state['status'] if state else 'auth_or_discovery_required','name':settings['name'],'allowed_read_tools':settings.get('allowed_read_tools',[]),'last_success':state.get('last_success') if state else None,'gap_since':state.get('gap_since') if state else None,'note':'Configuration inspected offline; remote capabilities and credential validity were not probed.'}
        except (ValueError,OSError,TypeError):
            checks['connector']={'ok':False,'status':'invalid_config','note':'Check configuration against docs/SYMPHONY_MCP.md; no network request made.'}
    queue=directory/'activity-queue'
    checks['activity_queue']={'pending':len(list(queue.glob('*.event.json'))) if queue.is_dir() and not queue.is_symlink() else 0,'errors':len(list(queue.glob('*.error.json'))) if queue.is_dir() and not queue.is_symlink() else 0,'note':'Use drain-activity to process explicit queued receipts; nothing runs automatically.'}
    return {'checks':checks,'connectors':connectors,'timezone':os.environ.get('WORK_COPILOT_TIMEZONE','UTC'),'network_used':False,'external_sends':False,'next_steps':['Start serve with --vault and optional --connector-config.','Run connect --authorize only when ready to grant approved OAuth access.','Use structured session handoffs or approved JSON imports for agent work; no private trace directories are scanned.']}
