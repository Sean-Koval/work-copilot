"""Explicitly configured, read-only remote MCP collection using SDK OAuth/PKCE.

Tool descriptions and returned text are data, never authorization. No tools are
called during discovery; collection requires both local allowlisting and the
server's readOnlyHint. A lying server remains a trust boundary, not a sandbox.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

import httpx
from filelock import FileLock, Timeout
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken, OAuthMetadata


class _RedactOAuthLog(logging.Filter):
    def filter(self, record):
        record.msg = 'OAuth operation diagnostic (details redacted)'
        record.args = (); record.exc_info = None; record.exc_text = None
        return True


logging.getLogger('mcp.client.auth.oauth2').addFilter(_RedactOAuthLog())


class AuthorizationRequired(RuntimeError):
    pass


class ProtectedFileStorage:
    """Explicit POSIX protected-file fallback; unencrypted, never silently selected."""
    def __init__(self, path):
        self.path = Path(path)
        if os.name != 'posix':
            raise ValueError('Protected-file storage requires POSIX permissions; use keyring on this OS')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise ValueError('Credential directory cannot be a symlink')
        os.chmod(self.path.parent, 0o700)

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ValueError('Credential file must be private (0600) and not a symlink')
        return json.loads(self.path.read_text())

    def _write(self, key, value):
        payload = self._read()
        payload[key] = value.model_dump(mode='json')
        if key == 'tokens':
            payload['expires_at'] = time.time() + value.expires_in if value.expires_in is not None else None
        temp = self.path.with_name(self.path.name + '.' + secrets.token_hex(8))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(payload, out)
                out.flush(); os.fsync(out.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    async def get_metadata(self):
        value = self._read().get('metadata')
        return OAuthMetadata.model_validate(value) if value else None

    async def set_metadata(self, value):
        self._write('metadata', value)

    async def get_tokens(self):
        payload = self._read()
        value = payload.get('tokens')
        if value and payload.get('expires_at') is not None:
            value['expires_in'] = max(0, int(payload['expires_at'] - time.time()))
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        self._write('tokens', tokens)

    async def get_client_info(self):
        value = self._read().get('client')
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info):
        self._write('client', client_info)


class KeyringStorage:
    def __init__(self, identity):
        try:
            import keyring
        except ImportError as exc:
            raise ValueError('Install work-copilot[oauth-keyring] or explicitly choose protected-file storage') from exc
        backend = keyring.get_keyring()
        module = type(backend).__module__
        if not any(module.startswith(prefix) for prefix in ('keyring.backends.SecretService', 'keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.kwallet', 'keyring.backends.libsecret')):
            raise ValueError('Configure a secure OS keyring backend explicitly; plaintext/chained/fail backends are not accepted')
        self.keyring = keyring
        self.identity = identity

    def _get(self, kind, model):
        value = self.keyring.get_password('work-copilot', self.identity + ':' + kind)
        if not value: return None
        payload = json.loads(value)
        if kind == 'tokens' and 'token' in payload:
            token = payload['token']
            if payload.get('expires_at') is not None:
                token['expires_in'] = max(0, int(payload['expires_at'] - time.time()))
            return model.model_validate(token)
        return model.model_validate(payload)

    async def get_metadata(self):
        return self._get('metadata', OAuthMetadata)

    async def set_metadata(self, value):
        self.keyring.set_password('work-copilot', self.identity + ':metadata', value.model_dump_json())

    async def get_tokens(self):
        return self._get('tokens', OAuthToken)

    async def set_tokens(self, tokens):
        self.keyring.set_password('work-copilot', self.identity + ':tokens', json.dumps({'token':tokens.model_dump(mode='json'),'expires_at':time.time()+tokens.expires_in if tokens.expires_in is not None else None}))

    async def get_client_info(self):
        return self._get('client', OAuthClientInformationFull)

    async def set_client_info(self, client_info):
        self.keyring.set_password('work-copilot', self.identity + ':client', client_info.model_dump_json())


def load_config(path):
    """Validate local configuration before credentials, status writes or network I/O.

    Errors name the field, never echo untrusted values (which may contain secrets).
    Discovery may omit tool selection; collection still requires an allowlist.
    """
    try:
        config = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('Configuration must contain valid UTF-8 JSON') from None
    if not isinstance(config, dict):
        raise ValueError('Configuration must be a JSON object')
    endpoint_value = config.get('endpoint')
    if not isinstance(endpoint_value, str):
        raise ValueError('endpoint must be an HTTPS URL string')
    try:
        endpoint = urlparse(endpoint_value)
        port = endpoint.port
    except ValueError:
        raise ValueError('endpoint must be a valid HTTPS URL with a valid port') from None
    local = endpoint.hostname in {'127.0.0.1', 'localhost'}
    if not endpoint.hostname or any(c.isspace() for c in endpoint_value) or (endpoint.scheme != 'https' and not (local and endpoint.scheme == 'http')):
        raise ValueError('endpoint requires HTTPS and a host (HTTP allowed only for loopback testing)')
    if endpoint.username or endpoint.password or endpoint.fragment or endpoint.query:
        raise ValueError('endpoint must not contain credentials, query or fragment')
    if config.get('storage') not in ('keyring', 'protected-file'):
        raise ValueError('Choose storage explicitly: keyring or protected-file (unencrypted POSIX 0600)')
    allowed = config.get('allowed_read_tools', [])
    if not isinstance(allowed, list) or any(not isinstance(name, str) or not name.strip() for name in allowed):
        raise ValueError('allowed_read_tools must be a list of nonempty tool-name strings')
    for field in ('tool', 'scope', 'client_id', 'cursor_argument', 'checkpoint_argument'):
        if field in config and (not isinstance(config[field], str) or not config[field].strip()):
            raise ValueError(field + ' must be a nonempty string when specified')
    if 'trust_env' in config and not isinstance(config['trust_env'], bool):
        raise ValueError('trust_env must be a JSON boolean')
    if not isinstance(config.get('arguments', {}), dict):
        raise ValueError('arguments must be a JSON object using the verified tool schema')
    if 'client_metadata_url' in config:
        value = config['client_metadata_url']
        if not isinstance(value, str):
            raise ValueError('client_metadata_url must be an administrator-approved HTTPS URL')
        try:
            metadata = urlparse(value)
            _ = metadata.port
            valid = metadata.scheme == 'https' and metadata.hostname and not (metadata.username or metadata.password or metadata.fragment or metadata.query) and not any(c.isspace() for c in value)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('client_metadata_url must be an administrator-approved HTTPS URL without credentials, query or fragment')
    callback_port = config.get('callback_port', 8766)
    if type(callback_port) is not int or not 1024 <= callback_port <= 65535:
        raise ValueError('callback_port must be an integer from 1024 to 65535')
    config.setdefault('name', 'remote-mcp')
    if not isinstance(config['name'], str) or not config['name'].strip() or len(config['name']) > 100:
        raise ValueError('Connector name must be a nonempty string up to 100 characters')
    config.setdefault('max_pages', 20)
    if type(config['max_pages']) is not int or not 1 <= config['max_pages'] <= 100:
        raise ValueError('max_pages must be an integer from 1 to 100')
    mapping = config.get('mapping', {})
    if not isinstance(mapping, dict):
        raise ValueError('mapping must be a JSON object')
    for field in ('items_path', 'next_cursor_path', 'checkpoint_path'):
        if field in mapping and (not isinstance(mapping[field], str) or not mapping[field] or any(not part for part in mapping[field].split('.'))):
            raise ValueError('mapping.' + field + ' must be a nonempty dot-separated object path')
    fields = mapping.get('fields', {})
    if not isinstance(fields, dict) or any(not key or not isinstance(value, str) or not value or any(not part for part in value.split('.')) for key, value in fields.items()):
        raise ValueError('mapping.fields must map output names to nonempty dot-separated object paths')
    if not isinstance(mapping.get('constants', {}), dict):
        raise ValueError('mapping.constants must be a JSON object')
    return config


def _identity(config, data_dir):
    return hashlib.sha256(json.dumps([str(Path(data_dir).resolve()),config['name'],config['endpoint']],separators=(',',':')).encode()).hexdigest()


class LoopbackCallback:
    def __init__(self, port, authorize):
        self.port, self.authorize = port, authorize
        self.server = None
        self.result = None
        self.ready = threading.Event()
        self.expected_state = None
        self.provider = None

    async def redirect(self, url):
        if not self.authorize:
            raise AuthorizationRequired('Run connect --authorize to grant or reconnect OAuth locally')
        parsed = urlparse(url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}):
            raise ValueError('Authorization endpoint requires HTTPS')
        self.expected_state = parse_qs(parsed.query).get('state', [''])[0]
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                host = self.headers.get('Host', '')
                state = query.get('state', [''])[0]
                if host != f'127.0.0.1:{owner.port}' or parsed.path != '/callback' or len(query.get('state',[])) != 1 or len(query.get('code',[])) != 1 or not query.get('code',[''])[0] or not secrets.compare_digest(state, owner.expected_state):
                    self.send_error(400); return
                issuer = query.get('iss')
                metadata = owner.provider.context.oauth_metadata if owner.provider else None
                if issuer and (len(issuer) != 1 or not metadata or issuer[0] != str(metadata.issuer)):
                    self.send_error(400); return
                owner.result = (query.get('code', [''])[0], state)
                self.send_response(200); self.send_header('Content-Type', 'text/plain'); self.end_headers()
                self.wfile.write(b'Authorization received. You may close this tab.')
                owner.ready.set()
            def log_message(self, *args):
                pass  # Never log authorization codes or callback query strings.
        self.server = HTTPServer(('127.0.0.1', self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if not await asyncio.to_thread(webbrowser.open, url):
            self.close()
            raise AuthorizationRequired('No browser opened; run this command in an interactive laptop session')

    async def callback(self):
        try:
            if not await asyncio.to_thread(self.ready.wait, 180):
                raise AuthorizationRequired('OAuth callback timed out; reconnect when ready')
            return self.result
        finally:
            self.close()

    def close(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.server = None


class _ExplicitGrantProvider(OAuthClientProvider):
    """Do not dynamically register a changed client during unattended refresh."""
    def __init__(self, *args, allow_registration=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_registration = allow_registration

    async def async_auth_flow(self, request):
        flow = super().async_auth_flow(request)
        try:
            outgoing = await flow.__anext__()
            while True:
                target = urlparse(str(outgoing.url))
                if target.scheme != 'https' and not (target.scheme == 'http' and target.hostname in {'127.0.0.1', 'localhost'}):
                    raise ValueError('OAuth discovery and token endpoints require HTTPS')
                if not self.allow_registration and outgoing.method == 'POST' and outgoing is not request:
                    metadata = self.context.oauth_metadata
                    if not metadata or str(outgoing.url) != str(metadata.token_endpoint):
                        raise AuthorizationRequired('Explicit authorization required for client registration')
                response = yield outgoing
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()


@asynccontextmanager
async def remote_session(config, data_dir, authorize=False):
    identity = _identity(config, data_dir)
    storage = (KeyringStorage(identity) if config['storage'] == 'keyring'
               else ProtectedFileStorage(Path(data_dir) / 'oauth' / (identity + '.json')))
    if not authorize and not await storage.get_tokens():
        raise AuthorizationRequired('Run connect --authorize to grant OAuth locally')
    # Pre-registered public-client deployments can provide client_id without secrets.
    if config.get('client_id') and not await storage.get_client_info():
        await storage.set_client_info(OAuthClientInformationFull(
            client_id=config['client_id'], redirect_uris=[f"http://127.0.0.1:{config.get('callback_port',8766)}/callback"],
            token_endpoint_auth_method='none', grant_types=['authorization_code','refresh_token']))
    callback = LoopbackCallback(config.get('callback_port',8766), authorize)
    provider = _ExplicitGrantProvider(
        allow_registration=authorize,
        server_url=config['endpoint'],
        client_metadata=OAuthClientMetadata(
            redirect_uris=[f"http://127.0.0.1:{callback.port}/callback"],
            client_name='Work Copilot local read collector', scope=config.get('scope'),
            token_endpoint_auth_method='none'),
        storage=storage, redirect_handler=callback.redirect, callback_handler=callback.callback,
        client_metadata_url=config.get('client_metadata_url'))
    callback.provider = provider
    # SDK v1 restores tokens but not their expiry or discovered refresh endpoint.
    await provider._initialize()
    provider.context.oauth_metadata = await storage.get_metadata()
    if provider.context.oauth_metadata:
        provider.context.auth_server_url = str(provider.context.oauth_metadata.issuer)
    tokens = provider.context.current_tokens
    if tokens and tokens.expires_in is not None:
        provider.context.token_expiry_time = time.time() + tokens.expires_in
    try:
        async with httpx.AsyncClient(auth=provider, timeout=httpx.Timeout(30, read=60), follow_redirects=False, trust_env=config.get('trust_env', False)) as client:
            async with streamable_http_client(config['endpoint'], http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        callback.close()
        if provider.context.oauth_metadata:
            await storage.set_metadata(provider.context.oauth_metadata)


def _path(obj, path, default=None):
    for part in path.split('.') if path else []:
        if not isinstance(obj, dict) or part not in obj:
            return default
        obj = obj[part]
    return obj


def normalize_result(payload, mapping):
    items = _path(payload, mapping.get('items_path','items'))
    if not isinstance(items, list) or len(items) > 10000:
        raise ValueError('Read tool must return a bounded items list; configure mapping.items_path')
    fields = mapping.get('fields')
    if fields:
        items = [{**mapping.get('constants',{}), **{key:_path(item,path) for key,path in fields.items()}} for item in items]
    if not all(isinstance(item,dict) for item in items):
        raise ValueError('Source items must be objects')
    return items, _path(payload,mapping.get('next_cursor_path','next_cursor')), _path(payload,mapping.get('checkpoint_path','checkpoint'))


def _error_status(exc):
    if isinstance(exc, BaseExceptionGroup):
        return 'auth_required' if any(_error_status(e)=='auth_required' for e in exc.exceptions) else 'error'
    if isinstance(exc, AuthorizationRequired) or 'oauth' in type(exc).__name__.lower():
        return 'auth_required'
    if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in {401,403}:
        return 'auth_required'
    return 'error'


async def _run_unlocked(store, config_path, data_dir, authorize, collect):
    config = load_config(config_path)
    name = config['name']
    store.configure_connector(name, ['remote_mcp_http', 'oauth_pkce', 'explicit_read_allowlist', 'bounded_pagination'], 'Configured remote MCP; company-specific schema and coverage require verification. No complete cached ACL revocation or retention enforcement. Discovery alone does not imply source coverage.')
    store.set_connector(name, 'catching_up')
    try:
        async with remote_session(config,data_dir,authorize) as session:
            tools = []; cursor = None
            for _ in range(20):
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools); cursor = page.nextCursor
                if not cursor: break
            else:
                raise ValueError('Tool discovery pagination exceeded limit')
            if not collect:
                # A successful OAuth handshake/tool listing is not source catchup.
                store.set_connector(name,'catching_up')
                return {'tools':[t.model_dump(mode='json') for t in tools], 'note':'Discovery only; no source read tool called.'}
            tool = config.get('tool')
            if tool not in config.get('allowed_read_tools',[]):
                raise ValueError('Configured tool is not in local allowed_read_tools')
            match = next((t for t in tools if t.name==tool), None)
            if not match or not match.annotations or match.annotations.readOnlyHint is not True:
                raise ValueError('Configured tool must exist and advertise readOnlyHint=true; verify server semantics with administrator')
            arguments = dict(config.get('arguments',{}))
            connector = next((c for c in store.snapshot()['connectors'] if c['name']==name),{})
            checkpoint = connector.get('checkpoint')
            if config.get('checkpoint_argument') and checkpoint is not None:
                arguments[config['checkpoint_argument']] = checkpoint
            all_items=[]; seen=set(); final_checkpoint=checkpoint
            for _ in range(config['max_pages']):
                result=await session.call_tool(tool, arguments)
                if result.isError: raise ValueError('Configured read tool returned an error')
                payload=result.structuredContent
                if payload is None:
                    texts=[c.text for c in result.content if c.type=='text']
                    payload=json.loads('\n'.join(texts))
                items,cursor,page_checkpoint=normalize_result(payload,config.get('mapping',{}))
                all_items.extend(items)
                if len(all_items)>50000: raise ValueError('Collection exceeded item limit; narrow configured scope')
                if page_checkpoint is not None: final_checkpoint=page_checkpoint
                if not cursor: break
                if not config.get('cursor_argument') or str(cursor) in seen:
                    raise ValueError('Pagination requires cursor_argument and nonrepeating cursor')
                seen.add(str(cursor)); arguments[config['cursor_argument']]=cursor
            else:
                raise ValueError('Page limit reached; nothing committed, narrow scope or increase max_pages')
            store.ingest(all_items, connector=name, checkpoint=final_checkpoint)
            store.set_connector(name,'healthy')
            return {'items':len(all_items),'checkpoint':final_checkpoint,'status':'connected'}
    except Exception as exc:
        status=_error_status(exc)
        # Never persist exception text: upstream errors may contain credentials or payloads.
        store.set_connector(name,status,error='OAuth reconnect required' if status=='auth_required' else 'Collection failed; verify endpoint, read tool, scope and mapping')
        raise RuntimeError('OAuth reconnect required' if status=='auth_required' else 'Remote MCP collection failed; inspect configuration and administrator-approved tool schema') from None


async def _run(store, config_path, data_dir, authorize, collect):
    config = load_config(config_path)
    lock_dir = Path(data_dir) / 'oauth'
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock_dir.is_symlink():
        raise ValueError('OAuth directory must not be a symlink')
    identity = _identity(config, data_dir)
    lock = FileLock(str(lock_dir / (identity + '.lock')))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise RuntimeError('Connector busy; another OAuth or collection operation is in progress') from None
    try:
        return await _run_unlocked(store, config_path, data_dir, authorize, collect)
    finally:
        lock.release()


async def remote_discover(store, config_path, data_dir, authorize=False):
    return await _run(store, config_path, data_dir, authorize, False)


async def remote_collect(store, config_path, data_dir, authorize=False):
    return await _run(store, config_path, data_dir, authorize, True)
