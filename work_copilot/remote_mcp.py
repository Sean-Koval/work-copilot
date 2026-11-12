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


KEYRING_SERVICE = 'work-copilot'
_SECURE_BACKENDS = ('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService',
                    'keyring.backends.libsecret', 'keyring.backends.kwallet')
# Windows Credential Manager caps one secret at 2560 bytes (1280 UTF-16 characters); OAuth tokens and
# client registrations can exceed that, so long values are split across entries on every OS.
_CHUNK_CHARS = 1000
_CHUNK_MANIFEST = 'work-copilot-chunks:v1:'
_KINDS = ('metadata', 'tokens', 'client')
PROTECTED_FILE_REMOVED = (
    'storage "protected-file" was removed: OAuth tokens are kept only in the OS keychain. Set "storage": "keyring", '
    'install the oauth-keyring extra, delete the old unencrypted token files (oauth/*.json in the data directory), '
    'then run connect --authorize to sign in again; existing tokens are not migrated')


class KeychainUnavailable(ValueError):
    """No secure OS keychain backend; there is deliberately no file fallback."""


def _backend_is_secure(backend):
    module = type(backend).__module__
    if module.startswith('keyring.backends.chainer'):
        children = list(getattr(backend, 'backends', ()))
        return bool(children) and all(_backend_is_secure(child) for child in children)
    return module.startswith(_SECURE_BACKENDS)


def secure_keyring():
    """Return the keyring module only when its active backend is an OS keychain.

    macOS Keychain and Windows Credential Manager are the targets; Linux needs a running
    Secret Service (GNOME Keyring, KWallet). Plaintext, null and fail backends are refused.
    """
    try:
        import keyring
    except ImportError:
        raise KeychainUnavailable('OAuth storage needs the OS keychain: install work-copilot[oauth-keyring]') from None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any backend load failure means no keychain
        raise KeychainUnavailable('OS keychain backend could not be loaded; tokens are never written to a file instead') from None
    if not _backend_is_secure(backend):
        raise KeychainUnavailable(
            'No secure OS keychain is available (active keyring backend: ' + type(backend).__module__ + '). '
            'Use macOS Keychain or Windows Credential Manager; on Linux start an unlocked Secret Service '
            '(GNOME Keyring or KWallet). Tokens are never written to a file instead')
    return keyring


class KeyringStorage:
    """OAuth metadata, tokens and client registration in the OS keychain only."""
    def __init__(self, identity, keyring=None):
        self.keyring = keyring or secure_keyring()
        self.identity = identity

    def _key(self, kind):
        return self.identity + ':' + kind

    def _read(self, kind):
        key = self._key(kind)
        value = self.keyring.get_password(KEYRING_SERVICE, key)
        if not value or not value.startswith(_CHUNK_MANIFEST):
            return value or None
        parts = [self.keyring.get_password(KEYRING_SERVICE, key + '#' + str(i)) for i in range(int(value[len(_CHUNK_MANIFEST):]))]
        # An interrupted write leaves an incomplete set; treat it as absent so the user re-authorizes.
        return None if any(part is None for part in parts) else ''.join(parts)

    def _chunk_count(self, key):
        value = self.keyring.get_password(KEYRING_SERVICE, key)
        return int(value[len(_CHUNK_MANIFEST):]) if value and value.startswith(_CHUNK_MANIFEST) else 0

    def _delete(self, key):
        if self.keyring.get_password(KEYRING_SERVICE, key) is not None:
            self.keyring.delete_password(KEYRING_SERVICE, key)

    def _write(self, kind, text):
        key = self._key(kind)
        old = self._chunk_count(key)
        if len(text) <= _CHUNK_CHARS and not text.startswith(_CHUNK_MANIFEST):
            self.keyring.set_password(KEYRING_SERVICE, key, text)
            new = 0
        else:
            parts = [text[i:i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]
            for i, part in enumerate(parts):
                self.keyring.set_password(KEYRING_SERVICE, key + '#' + str(i), part)
            self.keyring.set_password(KEYRING_SERVICE, key, _CHUNK_MANIFEST + str(len(parts)))
            new = len(parts)
        for i in range(new, old):
            self._delete(key + '#' + str(i))

    def clear(self):
        """Remove every entry for this connector identity."""
        for kind in _KINDS:
            key = self._key(kind)
            for i in range(self._chunk_count(key)):
                self._delete(key + '#' + str(i))
            self._delete(key)

    def _get(self, kind, model):
        value = self._read(kind)
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
        self._write('metadata', value.model_dump_json())

    async def get_tokens(self):
        return self._get('tokens', OAuthToken)

    async def set_tokens(self, tokens):
        self._write('tokens', json.dumps({'token':tokens.model_dump(mode='json'),'expires_at':time.time()+tokens.expires_in if tokens.expires_in is not None else None}))

    async def get_client_info(self):
        return self._get('client', OAuthClientInformationFull)

    async def set_client_info(self, client_info):
        self._write('client', client_info.model_dump_json())


def load_config(path):
    """Validate local configuration before credentials, status writes or network I/O.

    Errors name the field, never echo untrusted values (which may contain secrets).
    Discovery may omit tool selection; collection still requires an allowlist.
    """
    try:
        config = json.loads(Path(path).read_text(encoding='utf-8'))
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
    if config.get('storage') == 'protected-file':
        raise ValueError(PROTECTED_FILE_REMOVED)
    if config.get('storage') != 'keyring':
        raise ValueError('storage must be set explicitly to "keyring" (OS keychain)')
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
    storage = KeyringStorage(identity)
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
