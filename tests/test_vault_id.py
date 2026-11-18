"""KAN-391: a vault keeps its note ids and evidence links when its folder moves."""
import errno
import json
import os
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
        patch.setattr(vault_module, '_write_vault_id', lambda root: (_ for _ in ()).throw(vault_module._ReadOnlyVault(13, 'Permission denied')))
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
    # Only the alias that lets backups taken before the rename be cleared still names it.
    dump = '\n'.join(line for line in db.iterdump() if f"'vault-alias:{legacy}'" not in line)
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
    spelled = os.path.join(str(root.parent), '.', root.name)
    assert spelled != str(root)
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


def test_notes_under_both_namespaces_merge_keeping_the_reviewed_obligation(setup, monkeypatch):
    tmp_path, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    old_note = f'vault:{legacy}:loose.md'
    reviewed = store.track_obligation({'intent_key': f'map:{old_note}', 'title': 'Map the loose note', 'project_id': 'atlas', 'evidence_ids': [old_note]})
    reviewed = store.review(reviewed['id'], reviewed['revision'], {'status': 'waiting', 'waiting_on': 'Priya'})
    with monkeypatch.context() as patch:
        patch.setattr(Store, '_rename_vault_namespace', lambda self, old, new: 0)
        vault_id = capture_vault(store, root)['vault_id']
    new_note = f'vault:{vault_id}:loose.md'
    fresh = store.track_obligation({'intent_key': f'map:{new_note}', 'title': 'Map the loose note', 'project_id': 'atlas', 'evidence_ids': [new_note]})
    store.draft(fresh['id'])
    assert len(store.list_sources(provider='vault')) == 4
    capture_vault(store, root, moved_from=root)
    sources = [(s['id'], s['deleted']) for s in store.list_sources(provider='vault')]
    assert all(sid.startswith(f'vault:{vault_id}:') and not deleted for sid, deleted in sources) and len(sources) == 2
    # capture's own review candidate (no project) merged the same way; one of each remains.
    assert sorted(o['project_id'] or '' for o in store.snapshot()['obligations'] if o['intent_key'] == f'map:{new_note}') == ['', 'atlas']
    mapped = [o for o in store.snapshot()['obligations'] if o['intent_key'] == f'map:{new_note}' and o['project_id'] == 'atlas']
    assert len(mapped) == 1 and mapped[0]['id'] == reviewed['id'] and mapped[0]['waiting_on'] == 'Priya'
    assert [e['id'] for e in mapped[0]['evidence'] if e['id'].startswith('vault:')] == [new_note]
    assert store.draft(reviewed['id'])  # the fresh obligation's draft moved; snapshot and drafts still work
    assert all(e.startswith(f'vault:{vault_id}:') for o in _state(store)[1] for e in o[2] if e.startswith('vault:'))


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


def test_a_filesystem_without_hard_links_still_gets_an_id_file(setup, monkeypatch):
    _, root, store = setup
    monkeypatch.setattr(os, 'link', lambda *args: (_ for _ in ()).throw(OSError(errno.EPERM, 'Operation not permitted')))
    report = capture_vault(store, root)
    assert report['vault_id'] == json.loads((root / VAULT_ID_FILE).read_text(encoding='utf-8'))['vault_id']
    assert not [p for p in root.iterdir() if p.name.endswith('.tmp')]


def test_a_racing_first_capture_uses_the_id_written_first(setup, monkeypatch):
    _, root, store = setup
    winner = '0' * 32
    real = vault_module._write_vault_id

    def racing(folder):
        (folder / VAULT_ID_FILE).write_text(json.dumps({'vault_id': winner}), encoding='utf-8')
        return real(folder)
    monkeypatch.setattr(vault_module, '_write_vault_id', racing)
    assert capture_vault(store, root)['vault_id'] == winner


def test_an_unwritable_folder_keeps_the_path_hash(setup, monkeypatch):
    _, root, store = setup
    monkeypatch.setattr(vault_module, '_write_vault_id', lambda root: (_ for _ in ()).throw(vault_module._ReadOnlyVault(errno.EROFS, 'Read-only file system')))
    report = capture_vault(store, root)
    assert report['vault_id'] is None and report['namespace'] == vault_module._legacy_namespace(root) and 'Read-only' in report['note']


def test_a_new_vault_in_an_old_vaults_folder_does_not_take_its_notes(setup, tmp_path):
    _, root, store = setup
    first = capture_vault(store, root)['vault_id']
    moved = _move(root, tmp_path)
    (root / 'other').mkdir(parents=True)
    (root / 'other' / 'new.md').write_text('# New vault\n', encoding='utf-8')
    report = capture_vault(store, root)
    assert report['vault_id'] != first and 'migrated_note_ids' not in report and first in report['note']
    assert capture_vault(store, moved)['vault_id'] == first
    assert not [s for s in store.list_sources() if s['id'].startswith(f'vault:{first}:') and s['deleted']]


def test_moved_from_renames_a_vault_whose_id_file_was_lost(setup, tmp_path):
    _, root, store = setup
    first = capture_vault(store, root)['vault_id']
    before = _state(store)
    moved = _move(root, tmp_path)
    (moved / VAULT_ID_FILE).unlink()
    report = capture_vault(store, moved, moved_from=root)
    assert report['migrated_note_ids'] == 2
    assert _state(store)[0] == sorted((sid.replace(first, report['vault_id']), deleted) for sid, deleted in before[0])


def test_a_failed_header_rewrite_is_retried_and_spares_hand_written_inbox_notes(setup, monkeypatch):
    _, root, store = setup
    legacy = vault_module._legacy_namespace(root)
    (root / 'Inbox').mkdir()
    own = f'vault:{legacy}:Inbox/standup.md'
    (root / 'Inbox' / 'standup.md').write_text(f'---\nid: {vault_module._record_id(own)}\nsource: "{own}"\n---\nBody\n', encoding='utf-8')
    quoting = f'---\nsee: "{own}"\n---\nHand-written.\n'
    (root / 'Inbox' / 'mine.md').write_text(quoting, encoding='utf-8')
    _legacy_capture(monkeypatch, store, root)
    with monkeypatch.context() as patch:
        patch.setattr(vault_module.os, 'replace', lambda *a: (_ for _ in ()).throw(PermissionError(13, 'locked')))
        report = capture_vault(store, root)
    assert 'next capture tries again' in report['note']
    assert not [p for p in (root / 'Inbox').iterdir() if p.name.endswith('.tmp')]
    report = capture_vault(store, root)
    assert report['rewritten_note_headers'] == 1
    assert f'vault:{report["vault_id"]}:Inbox/standup.md' in (root / 'Inbox' / 'standup.md').read_text(encoding='utf-8')
    assert (root / 'Inbox' / 'mine.md').read_text(encoding='utf-8') == quoting
    assert 'rewritten_note_headers' not in capture_vault(store, root)


def test_the_first_rename_backs_up_the_database_first(setup, monkeypatch):
    from work_copilot import schema
    tmp_path, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    before = _state(store)
    report = capture_vault(store, root)
    assert report['migrated_note_ids'] == 2
    backup = Path(report['backup_before_rename'])
    # Named so prune_backups and doctor count it with the schema backups.
    assert backup in schema.backups(tmp_path / 'copilot.db') and 'vault-id' in backup.name
    copy = Store(backup)
    try:
        assert _state(copy) == before and all(sid.startswith(f'vault:{legacy}:') for sid, _ in _state(copy)[0])
    finally:
        copy.close()
    # Nothing left to rename: no second backup.
    assert 'backup_before_rename' not in capture_vault(store, root)
    assert schema.backups(tmp_path / 'copilot.db') == [backup]


def test_a_forgotten_note_stays_forgotten_after_the_rename(setup, monkeypatch):
    _, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    store.forget(f'vault:{legacy}:loose.md')
    report = capture_vault(store, root)
    vault_id = report['vault_id']
    assert report['migrated_note_ids'] == 1
    # The marker moved with the namespace, so the note is not captured again (KAN-390).
    assert {s['id'] for s in store.list_sources()} == {f'vault:{vault_id}:Meetings/release.md'}
    capture_vault(store, root)
    assert {s['id'] for s in store.list_sources()} == {f'vault:{vault_id}:Meetings/release.md'}


def test_text_forgotten_after_the_rename_is_cleared_from_the_backup_taken_before_it(setup, monkeypatch):
    _, root, store = setup
    _legacy_capture(monkeypatch, store, root)
    report = capture_vault(store, root)
    backup = Path(report['backup_before_rename'])
    assert b'Ship Thursday' in backup.read_bytes()
    result = store.forget(f"vault:{report['vault_id']}:Meetings/release.md")
    assert result['backups_cleared'] == 1 and result['backups_left'] == 0
    # The backup still names the note by its old id; the alias recorded at the rename finds it.
    assert b'Ship Thursday' not in backup.read_bytes()
    assert backup.exists()


def test_backups_from_before_two_renames_are_both_cleared(setup, monkeypatch):
    _, root, store = setup
    _legacy_capture(monkeypatch, store, root)
    first = capture_vault(store, root)
    (root / VAULT_ID_FILE).unlink()
    second = capture_vault(store, root)
    assert second['migrated_note_ids'] == 2
    backups = [Path(first['backup_before_rename']), Path(second['backup_before_rename'])]
    assert all(b'Ship Thursday' in backup.read_bytes() for backup in backups)
    store.forget(f"vault:{second['vault_id']}:Meetings/release.md")
    # The first backup names the path-hash ids; the alias chain leads back to them.
    assert all(b'Ship Thursday' not in backup.read_bytes() for backup in backups)


def test_a_vault_whose_notes_were_all_forgotten_still_records_its_alias(setup, monkeypatch):
    _, root, store = setup
    legacy = _legacy_capture(monkeypatch, store, root)
    store.forget(f'vault:{legacy}:loose.md')
    store.forget(f'vault:{legacy}:Meetings/release.md')
    report = capture_vault(store, root)
    assert store.list_sources() == []
    keys = {row[0] for row in store.db.execute("SELECT key FROM local_metadata")}
    assert f'vault-alias:{legacy}' in keys
    assert f"forgotten:vault:{report['vault_id']}:loose.md" in keys
