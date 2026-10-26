"""UI and agent entrypoints share the durable local context service."""
from fastapi.testclient import TestClient
from work_copilot.app import create_app


def test_inbox_map_search_track_and_restart(tmp_path):
    vault=tmp_path/'vault';vault.mkdir()
    app=create_app(tmp_path/'data',demo=True,vault=vault)
    with TestClient(app,base_url='http://localhost:8765') as c:
        h={'X-Copilot-Token':c.get('/api/session').json()['token']}
        r=c.post('/api/inbox',json={'title':'Risk discussion','body':'Decision: staging required. Risk: unique-migration-risk remains.'},headers=h)
        assert r.status_code==200,r.text
        candidates=c.get('/api/state').json()['review']
        candidate=next(o for o in candidates if 'Review project mapping' in o['title'])
        source=candidate['evidence'][0]
        r=c.post('/api/sources/map',json={'source_id':source['id'],'revision':source.get('mapping_revision',source['revision']),'projects':[{'id':'atlas','name':'Atlas','workstream':'engineering'}]},headers=h)
        assert r.status_code==200,r.text
        found=c.get('/api/search',params={'q':'unique-migration-risk','project_id':'atlas'}).json()
        assert found and found[0]['id']==source['id']
        r=c.post('/api/obligations',json={'payload':{'title':'Request transcript from Alex','intent_key':'alex-transcript','project_id':'atlas','owner':'me','status':'waiting','follow_up_at':'2026-10-27T09:00:00+00:00'}},headers=h)
        assert r.status_code==200,r.text
        assert r.json()['follow_up_at']=='2026-10-27T09:00:00+00:00'
        assert r.json().get('due_at') is None
    from work_copilot.store import Store
    store=Store(tmp_path/'data'/'copilot.db')
    assert any(o['intent_key']=='alex-transcript' for o in store.snapshot()['obligations'])
    store.close()


def test_empty_project_onboarding_and_explicit_capture(tmp_path):
    vault=tmp_path/'vault';vault.mkdir()
    with TestClient(create_app(tmp_path/'data',vault=vault),base_url='http://127.0.0.1:8765') as c:
        h={'X-Copilot-Token':c.get('/api/session').json()['token']}
        assert not c.get('/api/state').json()['projects']
        r=c.post('/api/projects',json={'id':'delta','name':'Delta Project','aliases':['Dart'],'rooms':[{'id':'room-1','name':'Engineering'},{'id':'room-shared','name':'Shared product'}]},headers=h)
        assert r.status_code==200,r.text
        assert len(r.json()['rooms'])==2
        r=c.post('/api/inbox',json={'title':'Daily notes','body':'Decision: staging comes first.','project_id':'delta','workstream':'engineering'},headers=h)
        assert r.status_code==200,r.text
        assert c.get('/api/projects/delta').json()['sources']
        r=c.post('/api/obligations',json={'payload':{'title':'Waiting on Alex','intent_key':'alex-transcript','project_id':'delta','owner':'me','status':'waiting','body':'Session handoff: request not sent; awaiting transcript.','follow_up_at':'2026-10-27T09:00:00-04:00'}},headers=h)
        assert r.status_code==200,r.text
        assert r.json()['status']=='waiting'


def test_edit_room_bindings_priority_and_overdue_ui_contract(tmp_path):
    app=create_app(tmp_path/'data')
    with TestClient(app,base_url='http://127.0.0.1:8765') as c:
        h={'X-Copilot-Token':c.get('/api/session').json()['token']}
        project=c.post('/api/projects',json={'id':'atlas','name':'Atlas'},headers=h).json()
        updated=c.patch('/api/projects/atlas',json={'revision':project['config_revision'],'changes':{'rooms':[{'id':'tenant:shared','name':'Shared'}]}},headers=h)
        assert updated.status_code==200,updated.text
        assert c.patch('/api/projects/atlas',json={'revision':project['config_revision'],'changes':{'name':'stale'}},headers=h).status_code==409
        assert c.patch('/api/projects/atlas',json={'revision':updated.json()['config_revision'],'changes':{'unknown':True}},headers=h).status_code==400
        app.state.store.ingest([{'id':'fixture:room','revision':'1','provider':'fixture','title':'Engineering room','body':'Fictional staging evidence','room_id':'tenant:shared','projects':[],'obligations':[]}])
        assert c.get('/api/projects/atlas').json()['sources'][0]['id']=='fixture:room'
        action=c.post('/api/obligations',json={'payload':{'intent_key':'dev-work','title':'Do implementation','project_id':'atlas','status':'open','owner':'me','waiting_on':'Fictional reviewer','commitment':True,'due_at':'2020-01-01','follow_up_at':'2020-01-01'}},headers=h).json()
        state=c.get('/api/state').json()
        assert action['id'] in [o['id'] for o in state['week']['overdue']]
        selected=c.post('/api/priorities',json={'ids':[action['id']],'revision':state['today']['revision']},headers=h)
        assert selected.status_code==200,selected.text
        assert selected.json()['kind']=='pinned'
        assert c.post('/api/priorities',json={'ids':[],'revision':state['today']['revision']},headers=h).status_code==409
        c.post('/api/obligations/'+action['id']+'/review',json={'revision':action['revision'],'changes':{'status':'resolved'}},headers=h)
        assert action['id'] not in c.get('/api/state').json()['today']['priorities']
