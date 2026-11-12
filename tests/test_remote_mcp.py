import json

import pytest
from work_copilot.remote_mcp import KeychainUnavailable, KeyringStorage, load_config, normalize_result
from mcp.shared.auth import OAuthToken

@pytest.mark.asyncio
async def test_tokens_round_trip_through_keychain_in_chunks(tmp_path, fake_keyring):
    store=KeyringStorage('identity-a')
    big='fictional-'+'x'*4000
    await store.set_tokens(OAuthToken(access_token=big, token_type='Bearer', refresh_token='fictional-refresh'))
    restored=await store.get_tokens()
    assert restored is not None and restored.access_token==big
    assert all(len(v)<=1000 for v in fake_keyring.entries.values())
    await store.set_tokens(OAuthToken(access_token='short', token_type='Bearer'))
    restored=await store.get_tokens()
    assert restored is not None and restored.access_token=='short'
    assert {k for _,k in fake_keyring.entries}=={'identity-a:tokens'}
    store.clear()
    assert fake_keyring.entries=={} and await store.get_tokens() is None
    assert list(tmp_path.iterdir())==[]

@pytest.mark.asyncio
async def test_incomplete_chunked_entry_reads_as_signed_out(fake_keyring):
    store=KeyringStorage('identity-b')
    await store.set_tokens(OAuthToken(access_token='y'*3000, token_type='Bearer'))
    del fake_keyring.entries[('work-copilot','identity-b:tokens#1')]
    assert await store.get_tokens() is None

def test_keychain_unavailable_fails_closed(monkeypatch):
    import sys
    import types
    def install(backend):
        monkeypatch.setitem(sys.modules,'keyring',types.SimpleNamespace(get_keyring=lambda:backend))
    install(type('Keyring',(),{'__module__':'keyring.backends.fail'})())
    with pytest.raises(KeychainUnavailable,match='Secret Service') as failure:
        KeyringStorage('identity')
    assert 'never written to a file' in str(failure.value)
    plaintext=type('PlaintextKeyring',(),{'__module__':'keyrings.alt.file'})()
    secure=type('Keyring',(),{'__module__':'keyring.backends.SecretService'})()
    chained=type('ChainerBackend',(),{'__module__':'keyring.backends.chainer'})
    install(type('Chain',(chained,),{'__module__':'keyring.backends.chainer','backends':[secure,plaintext]})())
    with pytest.raises(KeychainUnavailable):
        KeyringStorage('identity')
    install(type('Chain',(chained,),{'__module__':'keyring.backends.chainer','backends':[secure]})())
    assert KeyringStorage('identity').identity=='identity'

def test_keychain_requires_keyring_package(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'keyring',None)
    with pytest.raises(KeychainUnavailable,match='oauth-keyring'):
        KeyringStorage('identity')

def test_protected_file_config_is_rejected_with_migration_step(tmp_path):
    p=tmp_path/'config.json';p.write_text(json.dumps({'endpoint':'https://example.invalid/mcp','storage':'protected-file'}))
    with pytest.raises(ValueError,match='protected-file') as failure:
        load_config(p)
    for step in ('"storage": "keyring"','oauth/*.json','connect --authorize','not migrated'):
        assert step in str(failure.value)

def test_config_and_normalization(tmp_path):
    p=tmp_path/'config.json';p.write_text(json.dumps({'endpoint':'https://example.invalid/mcp'}))
    with pytest.raises(ValueError,match='storage'):
        load_config(p)
    assert normalize_result({'items':[{'id':'a'}]}, {}) == ([{'id':'a'}],None,None)
    assert normalize_result({'messages':[{'messageId':'a','text':'hello'}], 'next':'2'}, {'items_path':'messages','fields':{'id':'messageId','body':'text'},'constants':{'provider':'symphony'},'next_cursor_path':'next'}) == ([{'id':'a','body':'hello','provider':'symphony'}],'2',None)

@pytest.mark.asyncio
async def test_real_http_mcp_oauth_discovery_collect_restart(tmp_path, monkeypatch, fake_keyring):
    import asyncio, socket, threading, time
    import httpx, uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from mcp.server.fastmcp import FastMCP
    from work_copilot.remote_mcp import remote_discover, remote_collect
    def port():
        with socket.socket() as s: s.bind(('127.0.0.1',0)); return s.getsockname()[1]
    server_port, callback_port=port(),port(); base=f'http://127.0.0.1:{server_port}'
    mcp=FastMCP('fictional-read-source',stateless_http=True,streamable_http_path='/')
    @mcp.tool(annotations={'readOnlyHint':True})
    def read_messages(cursor: str | None=None):
        return {'items':[{'id':'fictional:room:1','revision':'1','provider':'fixture','title':'Request','body':'Confirm staging only','projects':[], 'obligations':[{'intent_key':'confirm-staging','title':'Confirm staging','owner':'me','status':'open'}]}], 'checkpoint':'1'}
    @mcp.tool(annotations={'readOnlyHint':False})
    def send_message():
        raise AssertionError('Write tool must never execute')
    transport=mcp.streamable_http_app()
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run(): yield
    app=FastAPI(lifespan=lifespan)
    @app.middleware('http')
    async def guard(request,call_next):
        if request.url.path.startswith('/mcp') and request.headers.get('authorization')!='Bearer test-access':
            return JSONResponse({'error':'unauthorized'},status_code=401,headers={'WWW-Authenticate':f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
        return await call_next(request)
    @app.get('/.well-known/oauth-protected-resource')
    async def resource(): return {'resource':base+'/mcp','authorization_servers':[base]}
    @app.get('/.well-known/oauth-authorization-server')
    async def metadata(): return {'issuer':base,'authorization_endpoint':base+'/authorize','token_endpoint':base+'/custom-token','registration_endpoint':base+'/register','response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],'code_challenge_methods_supported':['S256']}
    @app.post('/register')
    async def register(request:Request): return {**await request.json(),'client_id':'local-public-client'}
    @app.post('/custom-token')
    async def token(request:Request):
        form=await request.form()
        if form['grant_type']=='authorization_code': assert form['code_verifier']
        else: assert form['refresh_token']=='test-refresh'
        return {'access_token':'test-access','token_type':'Bearer','expires_in':3600,'refresh_token':'test-refresh'}
    app.mount('/mcp',transport)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=server_port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started: break
        await asyncio.sleep(.02)
    def fake_browser(url):
        from urllib.parse import urlparse,parse_qs,urlencode
        q=parse_qs(urlparse(url).query)
        assert q['code_challenge_method']==['S256']
        response=httpx.get(q['redirect_uri'][0]+'?'+urlencode({'code':'fictional-code','state':q['state'][0]}), trust_env=False)
        assert response.status_code==200
        return True
    monkeypatch.setattr('work_copilot.remote_mcp.webbrowser.open',fake_browser)
    from work_copilot.store import Store
    store=Store(tmp_path/'copilot.db');config=tmp_path/'config.json'
    config.write_text(json.dumps({'endpoint':base+'/mcp/','storage':'keyring','callback_port':callback_port,'tool':'read_messages','allowed_read_tools':['read_messages']}))
    try:
        with pytest.raises(RuntimeError,match='reconnect'):
            await remote_discover(store,config,tmp_path,False)
        assert store.snapshot()['connectors'][0]['status']=='auth_required'
        result=await remote_discover(store,config,tmp_path,True)
        assert any(t['name']=='read_messages' for t in result['tools'])
        assert sorted(p.suffix for p in (tmp_path/'oauth').iterdir())==['.lock'], 'no credential file may be written'
        assert 'test-access' not in ''.join(p.read_bytes().decode('utf-8','replace') for p in tmp_path.rglob('*') if p.is_file())
        token_key=next(k for (_,k) in fake_keyring.entries if k.endswith(':tokens'))
        saved=json.loads(fake_keyring.entries[('work-copilot',token_key)]);saved['expires_at']=0
        fake_keyring.entries[('work-copilot',token_key)]=json.dumps(saved)
        store.close(); store=Store(tmp_path/'copilot.db')
        result=await remote_collect(store,config,tmp_path,False)
        assert result['items']==1 and store.list_sources()[0]['id']=='fictional:room:1'
        assert store.snapshot()['connectors'][0]['checkpoint']=='1'
        obligation=store.snapshot()['obligations'][0]
        store.set_connector('remote-mcp','auth_expired')
        assert store.draft(obligation['id'])['stale'] is True
        await remote_discover(store,config,tmp_path,False)
        assert store.snapshot()['connectors'][0]['status']=='catching_up'
        assert store.draft(obligation['id'])['stale'] is True
        await remote_collect(store,config,tmp_path,False)
        assert store.draft(obligation['id'])['stale'] is False
        settings=json.loads(config.read_text(encoding='utf-8'));settings.update(tool='send_message',allowed_read_tools=['send_message']);config.write_text(json.dumps(settings))
        with pytest.raises(RuntimeError,match='Remote MCP collection failed') as failure:
            await remote_collect(store,config,tmp_path,False)
        assert 'test-access' not in str(failure.value)
        assert len(store.list_sources())==1
    finally:
        server.should_exit=True
        await asyncio.to_thread(thread.join,5)

@pytest.mark.asyncio
async def test_persist_absolute_expiry(monkeypatch, fake_keyring):
    storage=KeyringStorage('identity-expiry')
    monkeypatch.setattr('work_copilot.remote_mcp.time.time',lambda:100)
    await storage.set_tokens(OAuthToken(access_token='fictional', token_type='Bearer',expires_in=20))
    monkeypatch.setattr('work_copilot.remote_mcp.time.time',lambda:115)
    assert (await storage.get_tokens()).expires_in==5
    monkeypatch.setattr('work_copilot.remote_mcp.time.time',lambda:130)
    assert (await storage.get_tokens()).expires_in==0


@pytest.mark.asyncio
async def test_connector_lock_preserves_status(tmp_path):
    from filelock import FileLock
    import hashlib
    from work_copilot.remote_mcp import remote_discover, _identity
    endpoint='https://example.invalid/mcp'
    config=tmp_path/'config.json'; config.write_text(json.dumps({'endpoint':endpoint,'storage':'keyring'}))
    lock_dir=tmp_path/'oauth';lock_dir.mkdir()
    lock=FileLock(str(lock_dir/(_identity({'endpoint':endpoint,'name':'remote-mcp'},tmp_path)+'.lock')))
    with lock:
        with pytest.raises(RuntimeError,match='busy'):
            await remote_discover(object(),config,tmp_path)

def test_sdk_oauth_logs_are_redacted(caplog):
    import logging
    logger=logging.getLogger('mcp.client.auth.oauth2')
    try:
        raise RuntimeError('fictional-sensitive-token-and-code')
    except RuntimeError:
        logger.exception('token error fictional-sensitive-token-and-code')
    assert 'fictional-sensitive-token-and-code' not in caplog.text
    assert 'redacted' in caplog.text

@pytest.mark.parametrize('change,field',[
    ({'endpoint':'https:///mcp'},'endpoint'),
    ({'allowed_read_tools':['read',42]},'allowed_read_tools'),
    ({'arguments':[]},'arguments'),
    ({'max_pages':True},'max_pages'),
    ({'max_pages':'20'},'max_pages'),
    ({'trust_env':'false'},'trust_env'),
    ({'mapping':[]},'mapping'),
    ({'mapping':{'fields':{'id':42}}},'mapping.fields'),
    ({'mapping':{'items_path':''}},'mapping.items_path'),
    ({'client_metadata_url':'http://evil.invalid/client'},'client_metadata_url'),
    ({'scope':['read']},'scope'),
])
def test_config_rejects_unsafe_field_types(tmp_path,change,field):
    config={'endpoint':'https://example.invalid/mcp','storage':'keyring',**change}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match=field):load_config(path)


def test_config_root_must_be_object(tmp_path):
    path=tmp_path/'config.json';path.write_text('[]')
    with pytest.raises(ValueError,match='JSON object'):load_config(path)
