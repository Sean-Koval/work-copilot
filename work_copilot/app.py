"""Loopback-only HTTP companion. No external action endpoints exist."""
from contextlib import asynccontextmanager
import asyncio
import hmac
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import Store, ConflictError


class Review(BaseModel):
    revision: int = Field(ge=1)
    changes: dict


class ProjectSetup(BaseModel):
    id: str = Field(min_length=1,max_length=100)
    name: str = Field(min_length=1,max_length=200)
    aliases: list[str] = []
    rooms: list[dict] = []


class TrackObligation(BaseModel):
    payload: dict


class SourceMap(BaseModel):
    source_id: str
    revision: str
    projects: list[dict]


class InboxNote(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=500000)
    project_id: str | None = None
    workstream: str | None = None


class DraftEdit(BaseModel):
    revision: int = Field(ge=1)
    text: str = Field(max_length=30000)


def create_app(data_dir: Path, demo: bool = False, vault: Path | None = None, poll_seconds: float = 30, connector_config: Path | None = None) -> FastAPI:
    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(data_dir / 'copilot.db')
    if demo:
        from .demo import seed_demo
        seed_demo(store)
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / 'static'
    selected = Path(vault).expanduser().absolute() if vault else None
    capture_lock = threading.Lock()
    collect_lock = asyncio.Lock()

    def capture():
        if not capture_lock.acquire(blocking=False):
            raise ConflictError('A vault capture is already running')
        try:
            return capture_unlocked()
        finally:
            capture_lock.release()

    def capture_unlocked():
        if selected is None:
            raise ValueError('No vault selected. Restart with --vault /path/to/approved/work-vault.')
        from .vault import capture_vault, export_projects
        result = capture_vault(store, selected)
        export_projects(store, selected)
        return result

    async def worker():
        while True:
            if selected:
                try:
                    await asyncio.to_thread(capture)
                except Exception:
                    store.set_connector('vault', 'error', 'Capture failed; check selected folder permissions and configuration.')
            if connector_config:
                try:
                    from .remote_mcp import remote_collect
                    async with collect_lock:
                        await remote_collect(store, connector_config, data_dir, authorize=False)
                except Exception:
                    pass  # Connector records sanitized status; never log work payloads or token errors.
            await asyncio.sleep(max(5, poll_seconds))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        close = getattr(store, 'close', None)
        if close:
            close()

    app = FastAPI(title='Work Copilot', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        host = request.headers.get('host', '')
        try:
            parsed = urlsplit('http://' + host)
            valid_host = parsed.hostname in {'localhost', '127.0.0.1', '::1'} and not parsed.username and not parsed.password and not parsed.path
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({'detail':'Loopback host required'}, status_code=400)
        origin = request.headers.get('origin')
        if origin and origin != f'{request.url.scheme}://{host}':
            return JSONResponse({'detail':'Cross-origin access denied'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail':'Cross-site access denied'}, status_code=403)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if not hmac.compare_digest(request.headers.get('x-copilot-token',''),token):
                return JSONResponse({'detail':'Session mutation token required'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ConflictError)
    async def conflict(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=503)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail':'Record not found'}, status_code=404)

    @app.get('/api/session')
    def session():
        return {'token':token}

    @app.get('/api/state')
    def state():
        result = store.snapshot()
        result['runtime'] = {'external_sends':False, 'draft_mode':'offline_template', 'vault_selected':selected is not None, 'poll_seconds':max(5,poll_seconds), 'demo':demo, 'connector_configured':connector_config is not None, 'live_connectors':'Explicit OAuth MCP configuration required; no account connected automatically.'}
        return result

    @app.get('/api/search')
    def search(q: str = Query(min_length=1,max_length=500), project_id: str | None = None, workstream: str | None = None, provider: str | None = None, since: str | None = None, until: str | None = None):
        return store.search(q,project_id=project_id,workstream=workstream,provider=provider,since=since,until=until)

    @app.post('/api/obligations')
    def track(payload: TrackObligation):
        return store.track_obligation(payload.payload)

    @app.post('/api/projects')
    def create_project(payload: ProjectSetup):
        return store.create_project(payload.id,payload.name,aliases=payload.aliases,rooms=payload.rooms)

    @app.get('/api/projects/{project_id}')
    def project(project_id: str):
        return store.project(project_id)

    @app.post('/api/obligations/{obligation_id}/review')
    def review(obligation_id: str, payload: Review):
        return store.review(obligation_id,payload.revision,payload.changes)

    @app.post('/api/obligations/{obligation_id}/draft')
    def draft(obligation_id: str):
        return store.draft(obligation_id)

    @app.patch('/api/drafts/{draft_id}')
    def draft_edit(draft_id: str,payload: DraftEdit):
        return store.save_draft(draft_id,payload.revision,payload.text)

    @app.post('/api/capture')
    def run_capture():
        return capture()

    @app.post('/api/sources/map')
    def map_source(payload: SourceMap):
        result = store.map_source(payload.source_id, payload.revision, payload.projects)
        if selected:
            from .vault import export_projects
            try:
                export_projects(store, selected)
            except (OSError,ValueError,TimeoutError):
                result['projection_warning']='Mapping saved; Markdown projection could not be refreshed. Retry capture after checking vault access.'
        return result

    @app.post('/api/inbox')
    def inbox(payload: InboxNote):
        if selected is None:
            raise ValueError('Select a work vault with --vault before capturing notes.')
        from .vault import write_inbox_note
        return write_inbox_note(store, selected, payload.title, payload.body, project_id=payload.project_id, workstream=payload.workstream)

    @app.post('/api/collect')
    async def collect():
        if connector_config is None:
            raise ValueError('No MCP connector selected. Start with --connector-config after OAuth setup.')
        from .remote_mcp import remote_collect
        if collect_lock.locked():
            raise ConflictError('A connector sync is already running')
        async with collect_lock:
            return await remote_collect(store, connector_config, data_dir, authorize=False)

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    app.mount('/static',StaticFiles(directory=static),name='static')
    return app
