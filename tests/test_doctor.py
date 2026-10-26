from work_copilot.doctor import diagnose
from work_copilot.store import Store


def test_doctor_offline_does_not_authorize(tmp_path):
    store=Store(tmp_path/'copilot.db')
    result=diagnose(store,tmp_path,vault=tmp_path/'missing')
    assert result['network_used'] is False
    assert result['external_sends'] is False
    assert result['checks']['vault']['ok'] is False
    assert result['checks']['database']['ok'] is True
    assert result['checks']['connector']['status']=='not_configured'
