from fastapi.testclient import TestClient
from work_copilot.app import create_app


def test_local_security(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8765') as c:
        assert c.get('/api/state').status_code == 200
        assert c.get('/api/state', headers={'host':'attacker.example'}).status_code == 400
        assert c.get('/api/state', headers={'origin':'https://evil.example'}).status_code == 403
        assert c.post('/api/capture').status_code == 403
        token = c.get('/api/session').json()['token']
        assert c.post('/api/capture',headers={'X-Copilot-Token':token,'origin':'https://evil.example'}).status_code == 403
        assert c.post('/api/capture',headers={'X-Copilot-Token':token}).status_code == 400
        assert c.post('/api/send',headers={'X-Copilot-Token':token}).status_code == 404
        assert "default-src 'self'" in c.get('/').headers['content-security-policy']


def test_revision_review_and_draft(tmp_path):
    app=create_app(tmp_path, demo=True)
    with TestClient(app,base_url='http://localhost:8765') as c:
        token=c.get('/api/session').json()['token']; h={'X-Copilot-Token':token}
        state=c.get('/api/state').json(); ob=state['obligations'][0]
        r=c.post(f"/api/obligations/{ob['id']}/review",json={'revision':ob['revision']+1,'changes':{'status':'open'}},headers=h)
        assert r.status_code==409
        r=c.post(f"/api/obligations/{ob['id']}/draft",headers=h)
        assert r.status_code==200
        assert r.json()['mode']=='offline_template'
        assert c.get('/api/state').json()['runtime']['external_sends'] is False
