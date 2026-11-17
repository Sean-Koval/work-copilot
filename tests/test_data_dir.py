"""Default data folder per OS (KAN-397). Hermetic: home, platform and environment are all patched."""
import json
from pathlib import Path
import sys

import pytest

from work_copilot import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    root=tmp_path/'home';root.mkdir()
    monkeypatch.setattr(Path,'home',classmethod(lambda cls: root))
    for name in ('XDG_DATA_HOME','LOCALAPPDATA'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('WORK_COPILOT_TIMEZONE','UTC')  # _run() writes it; setenv restores it afterwards
    return root


def on(monkeypatch, platform):
    monkeypatch.setattr(sys,'platform',platform)


def seed(folder):
    folder.mkdir(parents=True,exist_ok=True);(folder/'copilot.db').write_bytes(b'existing')


def test_linux_uses_local_share(home, monkeypatch):
    on(monkeypatch,'linux')
    assert cli.default_data_dir()==home/'.local'/'share'/'work-copilot'
    assert cli.default_data_dir(demo=True)==home/'.local'/'share'/'work-copilot-demo'


def test_mac_uses_application_support(home, monkeypatch):
    on(monkeypatch,'darwin')
    assert cli.default_data_dir()==home/'Library'/'Application Support'/'work-copilot'
    assert cli.default_data_dir(demo=True)==home/'Library'/'Application Support'/'work-copilot-demo'


def test_windows_uses_localappdata(home, tmp_path, monkeypatch):
    on(monkeypatch,'win32');monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    assert cli.default_data_dir()==tmp_path/'Local'/'work-copilot'
    assert cli.default_data_dir(demo=True)==tmp_path/'Local'/'work-copilot-demo'


def test_windows_without_localappdata_falls_back_to_appdata_local(home, monkeypatch):
    on(monkeypatch,'win32')
    assert cli.default_data_dir()==home/'AppData'/'Local'/'work-copilot'


@pytest.mark.parametrize('platform',['linux','darwin','win32'])
def test_xdg_data_home_wins_on_every_os(home, tmp_path, monkeypatch, platform):
    on(monkeypatch,platform);monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'xdg'));monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    seed(home/'.local'/'share'/'work-copilot')
    assert cli.default_data_dir()==tmp_path/'xdg'/'work-copilot'
    assert cli.default_data_dir(demo=True)==tmp_path/'xdg'/'work-copilot-demo'


@pytest.mark.parametrize('platform',['darwin','win32'])
@pytest.mark.parametrize('demo',[False,True])
def test_existing_legacy_folder_stays_in_use(home, tmp_path, monkeypatch, platform, demo):
    on(monkeypatch,platform);monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    legacy=home/'.local'/'share'/('work-copilot-demo' if demo else 'work-copilot');seed(legacy)
    assert cli.default_data_dir(demo=demo)==legacy
    assert (legacy/'copilot.db').read_bytes()==b'existing'
    other=cli.default_data_dir(demo=not demo)
    assert other!=legacy and '.local' not in other.parts


@pytest.mark.parametrize('platform',['darwin','win32'])
def test_empty_legacy_folder_is_ignored_and_new_data_wins(home, tmp_path, monkeypatch, platform):
    on(monkeypatch,platform);monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    legacy=home/'.local'/'share'/'work-copilot';legacy.mkdir(parents=True)
    assert '.local' not in cli.default_data_dir().parts
    seed(legacy)
    new=(tmp_path/'Local' if platform=='win32' else home/'Library'/'Application Support')/'work-copilot';seed(new)
    assert cli.default_data_dir()==new


def test_doctor_says_when_the_legacy_folder_is_used(home, monkeypatch, capsys):
    on(monkeypatch,'darwin')
    legacy=home/'.local'/'share'/'work-copilot';legacy.mkdir(parents=True);(legacy/'keep.txt').write_text('keep')
    monkeypatch.setattr(sys,'argv',['work-copilot','doctor'])
    cli._run()
    report=json.loads(capsys.readouterr().out)
    assert str(legacy.resolve()) in report['data_dir_note']
    assert sorted(p.name for p in legacy.iterdir())==['keep.txt']


def test_doctor_has_no_note_for_the_new_folder_or_an_explicit_one(home, tmp_path, monkeypatch, capsys):
    on(monkeypatch,'darwin')
    seed(home/'.local'/'share'/'work-copilot')
    monkeypatch.setattr(sys,'argv',['work-copilot','--data-dir',str(home/'.local'/'share'/'work-copilot'),'doctor'])
    cli._run()
    assert 'data_dir_note' not in json.loads(capsys.readouterr().out)
    on(monkeypatch,'linux')
    monkeypatch.setattr(sys,'argv',['work-copilot','doctor'])
    cli._run()
    assert 'data_dir_note' not in json.loads(capsys.readouterr().out)
