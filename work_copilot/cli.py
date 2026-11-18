"""Explicit local setup, capture, import and MCP entrypoints."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


def default_data_dir(demo=False):
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local'/'share')
    return base / ('work-copilot-demo' if demo else 'work-copilot')


def _review_options(p):
    p.add_argument('--github',default=os.environ.get('WORK_COPILOT_KNOWLEDGE_GITHUB'),help='owner/repo whose open records PRs to read with gh (GET only). Default: WORK_COPILOT_KNOWLEDGE_GITHUB')
    p.add_argument('--me',default=os.environ.get('WORK_COPILOT_GITHUB_LOGIN'),help='Your GitHub login. Default: WORK_COPILOT_GITHUB_LOGIN')
    p.add_argument('--owner',default=os.environ.get('WORK_COPILOT_KNOWLEDGE_OWNER'),help='Your record owner value, if it differs from --me. Default: WORK_COPILOT_KNOWLEDGE_OWNER')
    p.add_argument('--review',choices=['all','assigned'],default=os.environ.get('WORK_COPILOT_KNOWLEDGE_REVIEW','all'),help='all: every open records PR (personal repo, default). assigned: PRs that request you, you code-own, you or the batch bot opened (team repo)')
    p.add_argument('--batch-author',action='append',default=[a for a in os.environ.get('WORK_COPILOT_KNOWLEDGE_BATCH_AUTHORS','').split(',') if a.strip()],help='GitHub login of the bot that opens records PRs; repeat for several. Default: WORK_COPILOT_KNOWLEDGE_BATCH_AUTHORS')


def _review_settings(args, repo):
    return {'repo':repo,'me':args.me or '','owner':args.owner,'github':args.github,'review':args.review,'batch_authors':tuple(a.strip() for a in args.batch_author)}


def _run():
    parser=argparse.ArgumentParser(description='Local-first Work Copilot. No external sending.')
    parser.add_argument('--timezone',help='IANA timezone for Today/follow-ups. Default: WORK_COPILOT_TIMEZONE, else UTC')
    parser.add_argument('--data-dir',type=Path,help='Private state directory (never a public repository). Default: WORK_COPILOT_DATA_DIR (not for demo), else the per-user data folder; init and doctor print the one in use')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['init','demo','serve','capture','export','import','mcp','connect','collect','doctor','activity','drain-activity','smoke','review-queue','forget','overlay']:
        p=sub.add_parser(name)
        if name=='activity':
            p.add_argument('--file',type=Path,required=True,help='Structured local session event JSON; no trace scraping')
            p.add_argument('--config',type=Path)
            p.add_argument('--queue-only',action='store_true')
        elif name=='drain-activity':
            p.add_argument('--config',type=Path)
        elif name=='init':
            p.add_argument('--wizard',action='store_true',help='Create the vault folders and first project, choose data dir and timezone, print the claude mcp add line')
            p.add_argument('--vault',type=Path,help='Work-only Markdown folder (wizard)')
            p.add_argument('--project-id',help='Stable id of the first project (wizard)')
            p.add_argument('--project-name',help='Display name of the first project (wizard)')
        elif name=='doctor':
            p.add_argument('--vault',type=Path)
            p.add_argument('--config',type=Path)
            p.add_argument('--repair',action='store_true',help='Rebuild search and project links from recorded state, then diagnose')
        elif name in {'serve','demo'}:
            p.add_argument('--connector-config',type=Path,help='Explicit configured OAuth MCP collector; never opens OAuth automatically')
            p.add_argument('--port',type=int,default=8765)
            p.add_argument('--vault',type=Path,help='Explicitly selected approved Markdown folder')
            p.add_argument('--review-repo',type=Path,help='Refresh the knowledge review queue from this local clone on the collector cadence')
            _review_options(p)
        elif name=='capture':
            p.add_argument('vault',type=Path)
            p.add_argument('--moved-from',type=Path,help='The folder this vault was in before it moved, if it moved before it had a .work-copilot-vault.json (renames its notes once)')
        elif name=='export':
            p.add_argument('vault',type=Path,nargs='?',help='Regenerate the project wiki in this vault (omit with --to or --jsonl)')
            p.add_argument('--jsonl',type=Path,help='Write all local state to this JSONL file (a portable backup; restore with import --jsonl)')
            p.add_argument('--to',type=Path,default=os.environ.get('WORK_COPILOT_KNOWLEDGE_REPO'),help='Local knowledge repository; Source records go to its inbox/. Default: WORK_COPILOT_KNOWLEDGE_REPO')
            p.add_argument('--since',help='Export sources observed at or after this ISO timestamp with timezone')
            p.add_argument('--owner',default=os.environ.get('WORK_COPILOT_KNOWLEDGE_OWNER'),help='Record owner. Default: WORK_COPILOT_KNOWLEDGE_OWNER')
            p.add_argument('--scope',default='personal',choices=['personal','project','team'])
        elif name=='review-queue':
            p.add_argument('--repo',type=Path,default=os.environ.get('WORK_COPILOT_KNOWLEDGE_REPO'),help='Local clone of the knowledge repository (read only). Default: WORK_COPILOT_KNOWLEDGE_REPO')
            _review_options(p)
        elif name=='overlay':
            p.add_argument('action',choices=['apply','show'],help='apply: check an overlay file and record it in the data directory. show: print the recorded overlay')
            p.add_argument('file',type=Path,nargs='?',help='apply only: the overlay JSON rendered by team-harness scripts/work_copilot_overlay.py --out <file>')
        elif name=='import':
            p.add_argument('file',type=Path,nargs='?',help='Normalized JSON envelope or source array')
            p.add_argument('--jsonl',type=Path,help='Restore a JSONL export into an empty --data-dir')
        elif name=='forget':
            p.add_argument('--source',required=True,help='Id of the source whose text to remove from the database and backups/ (not from exports or the wiki)')
        elif name in {'connect','collect'}:
            p.add_argument('--config',type=Path,required=True)
            p.add_argument('--authorize',action='store_true',help='Explicitly initiate OAuth in browser; only approved company server')
    args=parser.parse_args()
    if args.command=='smoke':
        from .smoke import run_smoke
        result=run_smoke()
        print(json.dumps(result,indent=2,default=str))
        if not result.get('success'):
            raise SystemExit(1)
        return
    timezone_given=bool(args.timezone or os.environ.get('WORK_COPILOT_TIMEZONE'))
    args.timezone=args.timezone or os.environ.get('WORK_COPILOT_TIMEZONE') or 'UTC'
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        parser.error('Unknown IANA timezone')
    os.environ['WORK_COPILOT_TIMEZONE']=args.timezone
    if args.data_dir is None and args.command!='demo' and os.environ.get('WORK_COPILOT_DATA_DIR'):
        args.data_dir=Path(os.environ['WORK_COPILOT_DATA_DIR'])
    data=(args.data_dir or default_data_dir(args.command=='demo')).expanduser().resolve()
    if args.command=='init' and args.wizard:
        from .wizard import run_wizard
        try:
            print('\n'.join(run_wizard(args,data,timezone_given,data_dir_given=args.data_dir is not None)))
        except (ValueError,OSError) as exc:
            print(f'Error: {exc}',file=sys.stderr)
            raise SystemExit(1)
        except (EOFError,KeyboardInterrupt):
            print('Error: input ended before the wizard finished; nothing was created.',file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command=='doctor' and not args.repair:
        from .doctor import diagnose_path
        print(json.dumps(diagnose_path(data,vault=args.vault,config=args.config,timezone_name=args.timezone),indent=2,default=str))
        return
    if args.command=='overlay' and args.action=='show' and args.file is not None:
        parser.error('overlay show takes no file; it prints the overlay recorded in the data directory (use overlay apply <file> to record one)')
    if args.command=='overlay' and args.action=='show':
        from .doctor import overlay_check
        print(json.dumps(overlay_check(data),indent=2))
        return
    if args.command=='overlay' and args.file is None:
        parser.error('overlay apply needs the overlay file')
    data.mkdir(parents=True,exist_ok=True,mode=0o700)
    if os.name=='posix':
        os.chmod(data,0o700)
    if args.command=='overlay':
        from .overlay import apply
        try:
            print(json.dumps(apply(data,args.file),indent=2,ensure_ascii=False))
        except (ValueError,OSError) as exc:
            print(f'Error: {exc}',file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command=='activity' and args.queue_only:
        from .hooks import enqueue_activity, MAX_EVENT_BYTES
        if args.file.stat().st_size>MAX_EVENT_BYTES:
            parser.error('Activity file exceeds 64 KiB')
        try:
            print(json.dumps(enqueue_activity(data,json.loads(args.file.read_text(encoding='utf-8')),os.environ),indent=2))
        except (ValueError,OSError) as exc:
            parser.error(str(exc))
        return
    if args.command=='mcp':
        from .mcp_server import serve_mcp
        serve_mcp(data)
        return
    if args.command in {'serve','demo'}:
        if args.vault:
            from .vault import _root
            _root(args.vault)
        if args.connector_config:
            from .remote_mcp import load_config
            load_config(args.connector_config)
        from .app import create_app
        import uvicorn
        print(f'Work Copilot: http://127.0.0.1:{args.port} | private data: {data}',flush=True)
        review_settings=None
        if args.review_repo:
            from .knowledge import knowledge_root
            knowledge_root(args.review_repo)
            if not args.me:
                parser.error('--review-repo needs --me or WORK_COPILOT_GITHUB_LOGIN')
            review_settings=_review_settings(args,args.review_repo)
        uvicorn.run(create_app(data,demo=args.command=='demo',vault=args.vault,connector_config=args.connector_config,review_queue=review_settings),host='127.0.0.1',port=args.port)
        return
    from .store import Store
    if args.command=='import' and (args.jsonl is None)==(args.file is None):
        parser.error('import takes exactly one of: a source file, or --jsonl <export>')
    if args.command=='export' and args.jsonl and args.vault:
        parser.error('export --jsonl writes local state only; omit the vault')
    if args.command=='import' and args.jsonl:
        try:
            store,result=Store.import_jsonl(args.jsonl,data/'copilot.db')
            store.close()
        except (ValueError,OSError) as exc:
            print(f'Error: {exc}',file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(result,indent=2,default=str))
        return
    store=Store(data/'copilot.db')
    try:
        if args.command=='activity':
            from .hooks import enqueue_activity, ingest_activity
            event=json.loads(args.file.read_text(encoding='utf-8'))
            if args.queue_only:
                result=enqueue_activity(data,event,os.environ)
            else:
                config=json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
                result=ingest_activity(store,event,config,os.environ)
        elif args.command=='drain-activity':
            from .hooks import drain_activity
            config=json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
            result=drain_activity(store,data,config)
        elif args.command=='doctor':
            from .doctor import diagnose
            repaired=store.rebuild_projections() if args.repair else None
            result=diagnose(store,data,vault=args.vault,config=args.config)
            if repaired is not None:
                result['repaired']=repaired
        elif args.command=='init':
            result={'data_dir':str(data),'external_sends':False}
        elif args.command=='capture':
            from .vault import capture_vault, export_projects
            result=capture_vault(store,args.vault,moved_from=args.moved_from)
            result['export']=export_projects(store,args.vault)
        elif args.command=='export' and args.jsonl:
            result=store.export_jsonl(args.jsonl)
        elif args.command=='export' and args.vault is None:
            if args.to is None:
                raise ValueError('Give a vault for the project wiki, or --to <knowledge repo> for Source records')
            from .knowledge import export_sources
            result=export_sources(store,args.to,owner=args.owner,since=args.since,scope=args.scope)
        elif args.command=='export':
            from .vault import export_projects
            result=export_projects(store,args.vault)
        elif args.command=='review-queue':
            if args.repo is None:
                raise ValueError('Give the local knowledge repository with --repo or WORK_COPILOT_KNOWLEDGE_REPO')
            from .review_queue import collect_review_queue
            result=collect_review_queue(store,**_review_settings(args,args.repo))
        elif args.command=='import':
            payload=json.loads(args.file.read_text(encoding='utf-8'))
            items=payload if isinstance(payload,list) else payload['items']
            result=store.ingest(items,connector='import',checkpoint=payload.get('checkpoint') if isinstance(payload,dict) else None)
        elif args.command=='forget':
            result=store.forget(args.source)
        elif args.command in {'connect','collect'}:
            from .remote_mcp import remote_discover, remote_collect
            fn=remote_discover if args.command=='connect' else remote_collect
            result=asyncio.run(fn(store,args.config,data,authorize=args.authorize))
        print(json.dumps(result,indent=2,default=str))
    except (ValueError,KeyError,OSError,RuntimeError) as exc:
        print(f'Error: {exc}',file=sys.stderr)
        raise SystemExit(1)
    finally:
        close=getattr(store,'close',None)
        if close: close()


def main():
    try:
        _run()
    except ModuleNotFoundError as exc:
        print(f'Error: Missing dependency {exc.name}. Run the documented launcher to install this checkout’s requirements.',file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'Error: {exc}. Check the selected paths and setup guide (docs/GETTING_STARTED.md).',file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        import sqlite3
        if isinstance(exc,sqlite3.Error):
            print('Error: Cannot open or initialize the local SQLite store. Check directory permissions, available disk space, and that the database is compatible; run doctor for read-only diagnostics.',file=sys.stderr)
            raise SystemExit(1) from None
        raise
