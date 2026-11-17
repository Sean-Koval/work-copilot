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
    with pytest.raises(ValueError, match='copy of the other'):
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


def test_migration_renames_ids_but_not_note_text(setup, monkeypatch):
    tmp_path, root, store = setup
    legacy = vault_module._legacy_namespace(root)
    (root / 'quoted.md').write_text(f'# Quoted\nThe old id was vault:{legacy}:loose.md, kept as written.\n', encoding='utf-8')
    _legacy_capture(monkeypatch, store, root)
    vault_id = capture_vault(store, root)['vault_id']
    body = store.source(f'vault:{vault_id}:quoted.md')['body']
    assert f'vault:{legacy}:loose.md' in body
    assert not [s for s in store.list_sources() if legacy in s['id']]


def test_a_rewritten_id_file_keeps_the_notes(setup):
    _, root, store = setup
    capture_vault(store, root)
    before = _state(store)
    first = json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']
    (root / VAULT_ID_FILE).unlink()
    report = capture_vault(store, root)
    assert report['vault_id'] != first and report['migrated_note_ids'] == 2
    sources, obligations = _state(store)
    assert sources == sorted((sid.replace(first, report['vault_id']), deleted) for sid, deleted in before[0])
    assert [o[0] for o in obligations] == [o[0] for o in before[1]]


def test_the_same_folder_under_another_spelling_is_not_a_copy(setup):
    _, root, store = setup
    vault_id = capture_vault(store, root)['vault_id']
    spelled = str(root.parent / '.' / root.name)
    store.db.execute("UPDATE local_metadata SET payload=? WHERE key=?", (json.dumps({'root': spelled}), 'vault-root:' + vault_id))
    store.db.commit()
    assert capture_vault(store, root)['vault_id'] == vault_id
    assert store.vault_root(vault_id) == str(root)


def test_a_vault_moved_before_it_had_an_id_is_renamed_with_moved_from(setup, monkeypatch):
    tmp_path, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    old_sources, _ = _state(store)
    moved = _move(root, tmp_path)
    report = capture_vault(store, moved, moved_from=root)
    assert report['migrated_note_ids'] == 2
    sources, _ = _state(store)
    assert sources == sorted((sid.replace(legacy, report['vault_id']), deleted) for sid, deleted in old_sources)


def test_notes_under_both_namespaces_merge_keeping_the_new_rows(setup, monkeypatch):
    tmp_path, root, store = setup
    _legacy_capture(monkeypatch, store, root)
    with monkeypatch.context() as patch:
        patch.setattr(Store, '_rename_vault_namespace', lambda self, old, new: 0)
        vault_id = capture_vault(store, root)['vault_id']
    assert len(store.list_sources()) == 4
    capture_vault(store, root, moved_from=root)
    sources, obligations = _state(store)
    assert all(sid.startswith(f'vault:{vault_id}:') and not deleted for sid, deleted in sources) and len(sources) == 2
    assert len([o for o in obligations if o[1] == 'confirm-release']) == 1
    assert all(e.startswith(f'vault:{vault_id}:') for o in obligations for e in o[2])


def test_only_a_read_only_folder_falls_back_to_the_path_hash(setup, monkeypatch):
    import errno
    _, root, store = setup
    monkeypatch.setattr(vault_module, '_write_vault_id', lambda root: (_ for _ in ()).throw(OSError(errno.ENOSPC, 'No space left on device')))
    with pytest.raises(OSError):
        capture_vault(store, root)


def test_the_id_file_is_written_whole(setup):
    _, root, store = setup
    capture_vault(store, root)
    assert not [p.name for p in root.iterdir() if p.name.endswith('.tmp')]
    assert json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']


def test_a_damaged_id_file_names_the_recorded_id(setup):
    _, root, store = setup
    vault_id = capture_vault(store, root)['vault_id']
    (root / VAULT_ID_FILE).write_text('garbage', encoding='utf-8')
    with pytest.raises(ValueError, match=vault_id):
        capture_vault(store, root)


def test_migration_rewrites_ids_in_inbox_note_headers(setup, monkeypatch):
    tmp_path, root, store = setup
    legacy = vault_module._legacy_namespace(root)
    old_id = f'vault:{legacy}:Inbox/standup.md'
    record = vault_module._record_id(old_id)
    (root / 'Inbox').mkdir()
    header = f'---\nid: {record}\nsources:\n  - id: "{old_id}"\n---\n'
    body = f'# Standup\nMentions {old_id} in the text.\n'
    (root / 'Inbox' / 'standup.md').write_bytes((header + body).encode('utf-8'))
    (root / 'Meetings' / 'hand.md').write_text(f'---\nsee: {old_id}\n---\nHand-written.\n', encoding='utf-8')
    _legacy_capture(monkeypatch, store, root)
    report = capture_vault(store, root)
    vault_id = report['vault_id']
    assert report['rewritten_note_headers'] == 1
    new_id = f'vault:{vault_id}:Inbox/standup.md'
    text = (root / 'Inbox' / 'standup.md').read_text(encoding='utf-8')
    assert text == f'---\nid: {vault_module._record_id(new_id)}\nsources:\n  - id: "{new_id}"\n---\n' + body
    assert old_id in (root / 'Meetings' / 'hand.md').read_text(encoding='utf-8')
    assert not [p for p in (root / 'Inbox').iterdir() if p.name.endswith('.tmp')]
