from pathlib import Path
import tempfile
import unittest

from work_copilot.vault import capture_vault, export_projects, GENERATED_DIR


class Ledger:
    def __init__(self):
        self.sources = {}
    def snapshot(self):
        return {'sources': list(self.sources.values()), 'projects': [], 'obligations': []}
    def ingest(self, items, connector='import', checkpoint=None):
        for item in items:
            self.sources[item['id']] = item
        return {'ingested': len(items)}
    def set_connector(self, *args, **kwargs):
        pass


class VaultFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'vault'
        self.root.mkdir()
        self.store = Ledger()
    def tearDown(self):
        self.temp.cleanup()


class VaultTests(VaultFixture):
    def test_originals_preserved_and_edit_has_stable_id(self):
        note = self.root / 'Daily.md'
        original = '# Meeting\n\nNeed to discuss Atlas.\n'
        note.write_text(original)
        capture_vault(self.store, self.root)
        first = next(iter(self.store.sources.values())).copy()
        self.assertEqual(note.read_text(), original)
        note.write_text(original + 'More context.\n')
        capture_vault(self.store, self.root)
        second = next(iter(self.store.sources.values()))
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(len(self.store.sources), 1)
    def test_deletion_tombstone_and_idempotent_reingest(self):
        note = self.root / 'Daily.md'
        note.write_text('Original')
        capture_vault(self.store, self.root)
        capture_vault(self.store, self.root)
        self.assertEqual(len(self.store.sources), 1)
        note.unlink()
        capture_vault(self.store, self.root)
        self.assertTrue(next(iter(self.store.sources.values()))['deleted'])
    def test_symlink_and_generated_files_not_ingested(self):
        secret = Path(self.temp.name) / 'outside.md'
        secret.write_text('NOT ALLOWED')
        (self.root / 'link.md').symlink_to(secret)
        generated = self.root / GENERATED_DIR
        generated.mkdir()
        (generated / 'projection.md').write_text('GENERATED')
        capture_vault(self.store, self.root)
        self.assertEqual(self.store.sources, {})
    def test_symlink_root_rejected(self):
        link = Path(self.temp.name) / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            capture_vault(self.store, link)
    def test_structured_metadata_and_line_citations(self):
        (self.root / 'meeting.md').write_text('---\nproject_ids: ["atlas"]\nworkstream: engineering\n---\n# Notes\n- [ ] Validate release\n')
        capture_vault(self.store, self.root)
        source = next(iter(self.store.sources.values()))
        self.assertEqual(source['projects'][0]['id'], 'atlas')
        self.assertEqual(source['workstream'], 'engineering')
        self.assertIn('L6', source['body'])
        self.assertEqual(source['obligations'], [])
    def test_export_refuses_preexisting_handwritten_output(self):
        generated = self.root / GENERATED_DIR
        generated.mkdir()
        (generated / 'README.md').write_text('My own file')
        with self.assertRaises(ValueError):
            export_projects(self.store, self.root)
        self.assertEqual((generated / 'README.md').read_text(), 'My own file')

if __name__ == '__main__':
    unittest.main()

class InboxTests(VaultFixture):
    def test_inbox_capture_preserves_full_context_and_does_not_overwrite(self):
        from work_copilot.vault import write_inbox_note
        first = write_inbox_note(self.store, self.root, 'Meeting', 'Decision: use staging.\nRisk: release uncertain.')
        second = write_inbox_note(self.store, self.root, 'Meeting', 'Awareness: no action assigned.')
        self.assertNotEqual(first['path'], second['path'])
        self.assertIn('Risk: release uncertain.', Path(first['path']).read_text())
        self.assertEqual(len(self.store.sources), 2)
    def test_inbox_symlink_rejected(self):
        from work_copilot.vault import write_inbox_note
        (self.root / 'Inbox').symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            write_inbox_note(self.store, self.root, 'Meeting', 'No')

class IntegrationTests(VaultFixture):
    def test_real_store_restart_delete_restore_and_wiki(self):
        from work_copilot.store import Store
        db = Path(self.temp.name) / 'state.db'
        self.store = Store(db)
        note = self.root / 'Daily.md'
        original = '---\nproject_ids: ["atlas"]\n---\nDecision: staging first.\n<script>alert(1)</script>\n'
        note.write_text(original)
        capture_vault(self.store, self.root)
        self.store.close()
        self.store = Store(db)
        note.unlink()
        capture_vault(self.store, self.root)
        self.assertTrue(self.store.list_sources()[0]['deleted'])
        note.write_text(original)
        capture_vault(self.store, self.root)
        self.assertFalse(self.store.list_sources()[0]['deleted'])
        result = export_projects(self.store, self.root)
        project = next(p for p in result['files'] if p.startswith('Project-'))
        projected = (self.root / GENERATED_DIR / project).read_text()
        self.assertIn('Decision: staging first.', projected)
        self.assertIn('L4:', projected)
        self.assertNotIn('<script>', projected)
        self.assertEqual(note.read_text(), original)
        capture_vault(self.store, self.root)
        self.assertEqual(len(self.store.list_sources()), 1)
        self.store.close()


class ReviewExportTests(VaultFixture):
    def test_export_includes_conflicts_even_after_owner_confirmation(self):
        class ConflictLedger(Ledger):
            def snapshot(self):
                return {'projects': [], 'review': [{'id':'o1', 'title':'Release answer', 'status':'open', 'review_reason':'Source owner changed'}]}
        export_projects(ConflictLedger(), self.root)
        review = (self.root / GENERATED_DIR / 'Review.md').read_text()
        self.assertIn('Release answer', review)
        self.assertIn('Source owner changed', review)

class ScanSafetyTests(VaultFixture):
    def test_scan_directory_error_does_not_mark_missing_sources_deleted(self):
        from unittest.mock import patch
        note = self.root / 'Daily.md'
        note.write_text('Keep evidence')
        capture_vault(self.store, self.root)
        def failed_walk(root, **kwargs):
            callback = kwargs.get('onerror')
            if callback:
                callback(PermissionError('directory unreadable'))
            return iter([])
        with patch('work_copilot.vault.os.walk', failed_walk):
            result = capture_vault(self.store, self.root)
        self.assertTrue(result['errors'])
        self.assertFalse(next(iter(self.store.sources.values()))['deleted'])
    def test_generated_links_reject_control_characters(self):
        from work_copilot.vault import _safe_link
        self.assertEqual(_safe_link({'url': 'https://example.invalid/a\n<script>bad</script>'}, self.root), '')

class InboxMetadataTests(VaultFixture):
    def test_pasted_frontmatter_matches_dropped_note_metadata(self):
        from work_copilot.vault import write_inbox_note
        body = '---\nproject_ids: ["atlas"]\nobligations: [{"intent_key":"release","title":"Confirm release","project_id":"atlas","owner":null}]\n---\nDecision: staging first.'
        (self.root / 'Dropped.md').write_text(body)
        receipt = write_inbox_note(self.store, self.root, 'Pasted', body)
        pasted = self.store.sources[receipt['source_ids'][0]]
        dropped = next(s for s in self.store.sources.values() if s['title'] == 'Dropped.md')
        self.assertEqual(pasted['projects'], dropped['projects'])
        self.assertEqual(pasted['obligations'], dropped['obligations'])
        self.assertTrue(Path(receipt['path']).read_text().startswith('---\n'))

class ExplicitCaptureTests(VaultFixture):
    def test_optional_project_selection_needs_no_frontmatter(self):
        from work_copilot.vault import write_inbox_note
        result = write_inbox_note(self.store, self.root, 'Transcript', 'Decision: staging first.', project_ids=['atlas', 'beacon'], workstream='engineering')
        source = self.store.sources[result['source_ids'][0]]
        self.assertEqual({p['id'] for p in source['projects']}, {'atlas', 'beacon'})
        self.assertEqual(source['workstream'], 'engineering')
        self.assertEqual(source['obligations'], [])
        self.assertIn('Decision: staging first.', Path(result['path']).read_text())
