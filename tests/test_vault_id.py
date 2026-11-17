"""KAN-391: a vault keeps its note ids and evidence links when its folder moves."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from work_copilot import vault as vault_module
from work_copilot.store import Store
from work_copilot.vault import VAULT_ID_FILE, capture_vault, write_inbox_note

TRACKED = '---\nproject_ids: ["atlas"]\nobligations: [{"intent_key": "confirm-release", "title": "Confirm the release date", "owner": "me"}]\n---\n# Release\nShip Thursday?\n'
UNMAPPED = '# Loose note\nNothing mapped here.\n'


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'vault'
    (root / 'Meetings').mkdir(parents=True)
    (root / 'Meetings' / 'release.md').write_text(TRACKED, encoding='utf-8')
    (root / 'loose.md').write_text(UNMAPPED, encoding='utf-8')
    store = Store(tmp_path / 'copilot.db')
    store.create_project('atlas', 'Atlas')
    return tmp_path, root, store


def _state(store):
    sources = sorted((s['id'], s['deleted']) for s in store.list_sources())
    obligations = sorted((o['id'], o['intent_key'], tuple(sorted(e['id'] for e in o['evidence']))) for o in store.snapshot()['obligations'])
    return sources, obligations


def _move(root, tmp_path):
    moved = tmp_path / 'synced' / 'Work Vault'
    moved.parent.mkdir()
    shutil.move(str(root), str(moved))
    return moved.resolve()


def test_first_capture_writes_an_id_and_namespaces_by_it(setup):
    _, root, store = setup
    report = capture_vault(store, root)
    vault_id = json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']
    assert report['vault_id'] == vault_id and not report['errors']
    assert {s['id'] for s in store.list_sources()} == {f'vault:{vault_id}:Meetings/release.md', f'vault:{vault_id}:loose.md'}
    assert capture_vault(store, root)['vault_id'] == vault_id


def test_moving_the_vault_keeps_notes_and_evidence(setup):
    tmp_path, root, store = setup
    capture_vault(store, root)
    before = _state(store)
    assert any(o[1] == 'confirm-release' and o[2] for o in before[1])
    moved = _move(root, tmp_path)
    report = capture_vault(store, moved)
    assert not report['errors']
    assert _state(store) == before
    assert all(not s['deleted'] and s['url'].startswith(moved.as_uri()) for s in store.list_sources())
    assert store.search('Thursday')
    capture_vault(store, moved)
    assert _state(store) == before


def _legacy_capture(monkeypatch, store, root):
    """Capture as before KAN-391: no id file, ids namespaced by the path hash."""
    with monkeypatch.context() as patch:
        patch.setattr(vault_module, '_write_vault_id', lambda root: (_ for _ in ()).throw(PermissionError(13, 'Permission denied')))
        report = capture_vault(store, root)
    assert report['vault_id'] is None and 'note' in report
    assert not (root / VAULT_ID_FILE).exists()
    return vault_module._legacy_namespace(root)


def test_old_path_hash_ids_are_migrated_once(setup, monkeypatch):
    tmp_path, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    old_sources, old_obligations = _state(store)
    assert all(sid.startswith(f'vault:{legacy}:') for sid, _ in old_sources)
    report = capture_vault(store, root)
    vault_id = report['vault_id']
    assert report['migrated_note_ids'] == 2
    sources, obligations = _state(store)
    assert sources == sorted((sid.replace(legacy, vault_id), deleted) for sid, deleted in old_sources)
    assert [o[0] for o in obligations] == [o[0] for o in old_obligations]
    assert [o[2] for o in obligations] == [tuple(e.replace(legacy, vault_id) for e in o[2]) for o in old_obligations]
    store.close()
    db = sqlite3.connect(tmp_path / 'copilot.db')
    dump = '\n'.join(db.iterdump())
    assert legacy not in dump
    db.close()
    store = Store(tmp_path / 'copilot.db')
    assert 'migrated_note_ids' not in capture_vault(store, root)
    assert _state(store) == (sources, obligations)
    moved = _move(root, tmp_path)
    capture_vault(store, moved)
    assert _state(store) == (sources, obligations)


def test_a_copied_vault_is_refused_until_its_id_file_is_removed(setup):
    tmp_path, root, store = setup
    capture_vault(store, root)
    copy = tmp_path / 'copy'
    shutil.copytree(root, copy)
    with pytest.raises(ValueError, match='same id'):
        capture_vault(store, copy)
    (copy / VAULT_ID_FILE).unlink()
    report = capture_vault(store, copy)
    assert report['vault_id'] != json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']
    assert len(store.list_sources()) == 4


def test_a_damaged_id_file_is_refused(setup):
    _, root, store = setup
    (root / VAULT_ID_FILE).write_text('{"vault_id": "not-an-id"}', encoding='utf-8')
    with pytest.raises(ValueError, match='no valid vault id'):
        capture_vault(store, root)
    (root / VAULT_ID_FILE).write_text('garbage', encoding='utf-8')
    with pytest.raises(ValueError, match='damaged'):
        capture_vault(store, root)


def test_inbox_note_uses_the_stable_namespace(setup):
    _, root, store = setup
    result = write_inbox_note(store, root, 'Standup', 'Atlas is blocked on review.', project_id='atlas')
    vault_id = json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']
    assert result['source_ids'][0].startswith(f'vault:{vault_id}:Inbox/')
    assert store.source(result['source_ids'][0])
