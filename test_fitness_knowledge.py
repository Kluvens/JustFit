import tempfile
import unittest
from pathlib import Path
from fitness_knowledge import NoteError, invalidate_owned_note, note_path, publish_new_note


class KnowledgeFilesTests(unittest.TestCase):
    def test_publish_invalidate_and_preserve_external_edit(self):
        with tempfile.TemporaryDirectory() as root:
            digest = publish_new_note(root, '2026-11', 'synthetic first summary')
            path = note_path(root, '2026-11')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                publish_new_note(root, '2026-11', 'replacement')
            path.write_text('user edited synthetic content', encoding='utf-8')
            with self.assertRaises(NoteError):
                invalidate_owned_note(root, '2026-11', digest)
            self.assertEqual(path.read_text(), 'user edited synthetic content')
            path.write_text('synthetic first summary', encoding='utf-8')
            self.assertTrue(invalidate_owned_note(root, '2026-11', digest))
            self.assertFalse(invalidate_owned_note(root, '2026-11', digest))

    def test_rejects_invalid_month_and_symlink_target(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as external:
            with self.assertRaises(NoteError):
                note_path(root, '../outside')
            health = Path(root) / '11_Health'
            health.mkdir()
            (health / 'Fitness_Auto').symlink_to(external, target_is_directory=True)
            with self.assertRaises(NoteError):
                publish_new_note(root, '2026-11', 'test')
            self.assertEqual(list(Path(external).iterdir()), [])
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as external:
            (Path(root) / '11_Health').symlink_to(external, target_is_directory=True)
            with self.assertRaises(NoteError):
                publish_new_note(root, '2026-11', 'test')
            self.assertEqual(list(Path(external).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
