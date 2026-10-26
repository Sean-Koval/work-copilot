"""Explicit local setup, capture, import and MCP entrypoints."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


def default_data_dir(demo=False):
    base = Path(os.environ.get('XDG_DATA_HOME', Path.home()/'.local'/'share'))
    return base / ('work-copilot-demo' if demo else 'work-copilot')


def _run():
    parser=argparse.ArgumentParser(description='Local-first Work Copilot. No external sending.')
    parser.add_argument('--timezone',default=os.environ.get('WORK_COPILOT_TIMEZONE','UTC'),help='IANA timezone for Today/follow-ups; default UTC')
    parser.add_argument('--data-dir',type=Path,help='Private state directory (never a public repository)')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['init','demo','serve','capture','export','import','mcp','connect','collect','doctor','activity','drain-activity','smoke']:
        p=sub.add_parser(name)
        if name=='activity':
            p.add_argument('--file',type=Path,required=True,help='Structured local session event JSON; no trace scraping')
            p.add_argument('--config',type=Path)
            p.add_argument('--queue-only',action='store_true')
        elif name=='drain-activity':
            p.add_argument('--config',type=Path)
        elif name=='doctor':
            p.add_argument('--vault',type=Path)
            p.add_argument('--config',type=Path)
        elif name in {'serve','demo'}:
            p.add_argument('--connector-config',type=Path,help='Explicit configured OAuth MCP collector; never opens OAuth automatically')
            p.add_argument('--port',type=int,default=8765)
            p.add_argument('--vault',type=Path,help='Explicitly selected approved Markdown folder')
        elif name in {'capture','export'}:
            p.add_argument('vault',type=Path)
        elif name=='import':
            p.add_argument('file',type=Path,help='Normalized JSON envelope or source array')
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
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        parser.error('Unknown IANA timezone')
    os.environ['WORK_COPILOT_TIMEZONE']=args.timezone
    data=(args.data_dir or default_data_dir(args.command=='demo')).expanduser().resolve()
    if args.command=='doctor':
        from .doctor import diagnose_path
        print(json.dumps(diagnose_path(data,vault=args.vault,config=args.config,timezone_name=args.timezone),indent=2,default=str))
        return
    data.mkdir(parents=True,exist_ok=True,mode=0o700)
    if os.name=='posix':
        os.chmod(data,0o700)
    if args.command=='activity' and args.queue_only:
        from .hooks import enqueue_activity, MAX_EVENT_BYTES
        if args.file.stat().st_size>MAX_EVENT_BYTES:
            parser.error('Activity file exceeds 64 KiB')
        try:
            print(json.dumps(enqueue_activity(data,json.loads(args.file.read_text(encoding='utf-8'))),indent=2))
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
        uvicorn.run(create_app(data,demo=args.command=='demo',vault=args.vault,connector_config=args.connector_config),host='127.0.0.1',port=args.port)
        return
    from .store import Store
    store=Store(data/'copilot.db')
    try:
        if args.command=='activity':
            from .hooks import enqueue_activity, ingest_activity
            event=json.loads(args.file.read_text(encoding='utf-8'))
            if args.queue_only:
                result=enqueue_activity(data,event)
            else:
                config=json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
                result=ingest_activity(store,event,config)
        elif args.command=='drain-activity':
            from .hooks import drain_activity
            config=json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
            result=drain_activity(store,data,config)
        elif args.command=='doctor':
            from .doctor import diagnose
            result=diagnose(store,data,vault=args.vault,config=args.config)
        elif args.command=='init':
            result={'data_dir':str(data),'external_sends':False}
        elif args.command=='capture':
            from .vault import capture_vault, export_projects
            result=capture_vault(store,args.vault)
            result['export']=export_projects(store,args.vault)
        elif args.command=='export':
            from .vault import export_projects
            result=export_projects(store,args.vault)
        elif args.command=='import':
            payload=json.loads(args.file.read_text(encoding='utf-8'))
            items=payload if isinstance(payload,list) else payload['items']
            result=store.ingest(items,connector='import',checkpoint=payload.get('checkpoint') if isinstance(payload,dict) else None)
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
