import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts")
)

from transaccion import FileTransaction


class TestFileTransaction(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="fpv-transaction-test-"
        )
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_restore_existing_file(self):
        target = self.root / "original.service"
        target.write_text("ORIGINAL")

        tx = FileTransaction(self.root / "backup")
        tx.register(target)

        target.write_text("MODIFICADO")
        tx.restore()

        self.assertEqual(target.read_text(), "ORIGINAL")

    def test_remove_new_file(self):
        target = self.root / "nuevo.service"

        tx = FileTransaction(self.root / "backup")
        tx.register(target)

        target.write_text("CREADO")
        tx.restore()

        self.assertFalse(target.exists())

    def test_reject_symlink(self):
        original = self.root / "original"
        original.write_text("NO MODIFICAR")

        link = self.root / "enlace"
        link.symlink_to(original)

        tx = FileTransaction(self.root / "backup")

        with self.assertRaises(RuntimeError):
            tx.register(link)

        self.assertEqual(
            original.read_text(), "NO MODIFICAR"
        )

    def test_detect_corrupted_backup(self):
        target = self.root / "detector.service"
        target.write_text("ORIGINAL")

        tx = FileTransaction(self.root / "backup")
        tx.register(target)

        Path(tx.entries[0]["backup"]).write_text("CORRUPTO")
        target.write_text("MODIFICADO")

        with self.assertRaises(RuntimeError):
            tx.restore()

        self.assertEqual(
            target.read_text(), "MODIFICADO"
        )

    def test_preflight_prevents_partial_restore(self):
        first = self.root / "first.service"
        second = self.root / "second.service"

        first.write_text("ORIGINAL1")
        second.write_text("ORIGINAL2")

        tx = FileTransaction(self.root / "backup")
        tx.register(first)
        tx.register(second)

        first.write_text("MODIFICADO1")
        second.write_text("MODIFICADO2")

        Path(tx.entries[0]["backup"]).write_text("CORRUPTO")

        with self.assertRaises(RuntimeError):
            tx.restore()

        self.assertEqual(first.read_text(), "MODIFICADO1")
        self.assertEqual(second.read_text(), "MODIFICADO2")

    def test_reload_transaction(self):
        existing = self.root / "original.service"
        created = self.root / "nuevo.service"

        existing.write_text("ORIGINAL")

        tx = FileTransaction(self.root / "backup")
        tx.register(existing)
        tx.register(created)

        existing.write_text("MODIFICADO")
        created.write_text("CREADO")

        del tx

        recovered = FileTransaction.load(
            self.root / "backup",
            allowed_destinations=[existing, created]
        )

        recovered.restore()

        self.assertEqual(existing.read_text(), "ORIGINAL")
        self.assertFalse(created.exists())


    def test_reject_unauthorized_journal_destination(self):
        import json

        authorized = self.root / "authorized.service"
        unauthorized = self.root / "unauthorized.service"

        authorized.write_text("ORIGINAL")
        unauthorized.write_text("NO MODIFICAR")

        tx = FileTransaction(self.root / "backup")
        tx.register(authorized)

        journal = self.root / "backup/journal.json"
        entries = json.loads(journal.read_text())

        entries[0]["destination"] = str(unauthorized)
        journal.write_text(json.dumps(entries))

        with self.assertRaises(RuntimeError):
            FileTransaction.load(
                self.root / "backup",
                allowed_destinations=[authorized]
            )

        self.assertEqual(
            unauthorized.read_text(),
            "NO MODIFICAR"
        )


if __name__ == "__main__":
    unittest.main()
