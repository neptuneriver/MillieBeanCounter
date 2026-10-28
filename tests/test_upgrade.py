"""Moving up from the version called millie-sync: keys and saved data."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from millie_bean_counter.__main__ import main
from millie_bean_counter.journal import Journal


def old_journal(path: Path) -> None:
    """A journal as millie-sync 1.1 left it: the sync's keys use A for 'agent'."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE messages (key TEXT PRIMARY KEY, subject TEXT NOT NULL, sender TEXT NOT NULL, received TEXT,
            status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', sheet_days TEXT NOT NULL DEFAULT '[]',
            attempts INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE events (key TEXT PRIMARY KEY, child_uid TEXT NOT NULL, kind TEXT NOT NULL, start TEXT NOT NULL,
            summary TEXT NOT NULL, status TEXT NOT NULL, sheet_day TEXT NOT NULL, message_key TEXT NOT NULL,
            updated TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', entry_key TEXT);
        CREATE TABLE keys (key TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, start_ts REAL NOT NULL, event_key TEXT UNIQUE, hb_id TEXT UNIQUE,
            created TEXT NOT NULL, UNIQUE (day, source, type, seq));
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO keys VALUES ('101226AD1', '2026-10-12', 'A', 'D', 1, 1.0, 'event-1', 'hb-1', 'x');
        INSERT INTO keys VALUES ('101226UA1', '2026-10-12', 'U', 'A', 1, 2.0, NULL, 'hb-2', 'x');
        INSERT INTO keys VALUES ('101226UD1', '2026-10-12', 'U', 'D', 1, 3.0, NULL, 'hb-3', 'x');
        INSERT INTO events VALUES ('event-1', 'c', 'diaper', '2026-10-12T09:10:00-04:00', 's', 'added',
            '2026-10-12', 'm', 'x', '{}', '101226AD1');
        """
    )
    db.commit()
    db.close()


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_agent_keys_become_sync_keys(self):
        path = self.root / "journal.sqlite3"
        old_journal(path)
        for _ in range(2):  # opening it again changes nothing more
            journal = Journal(path)
            keys = {row["key"]: row["source"] for row in journal.all_keys()}
            entry = journal.synced_entries()[0]["entry_key"]
            journal.close()
            # An app-entered activity (U + A for activity) is not the sync's, so it keeps its key.
            self.assertEqual(keys, {"101226SD1": "S", "101226UA1": "U", "101226UD1": "U"})
            self.assertEqual(entry, "101226SD1")

    def test_settings_and_history_move_over_once(self):
        old, new = self.root / "old", self.root / "new"
        old.mkdir()
        (old / "settings.env").write_text("GMAIL_ADDRESS=inbox@example.com\n")
        (old / "google-service-account.json").write_text(json.dumps({"type": "service_account"}))
        old_journal(old / "journal.sqlite3")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--data", str(new), "adopt-old-data", str(old)]), 0)
        self.assertIn("Copied your settings and history", out.getvalue())
        self.assertEqual((new / "settings.env").read_text(), "GMAIL_ADDRESS=inbox@example.com\n")
        self.assertTrue((new / "google-service-account.json").exists())
        self.assertEqual((new / "settings.env").stat().st_mode & 0o777, 0o600)
        journal = Journal(new / "journal.sqlite3")
        self.assertIn("101226SD1", [row["key"] for row in journal.all_keys()])
        journal.close()

        (old / "settings.env").write_text("GMAIL_ADDRESS=changed@example.com\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data", str(new), "adopt-old-data", str(old)]), 0)
        self.assertEqual((new / "settings.env").read_text(), "GMAIL_ADDRESS=inbox@example.com\n")  # left alone

    def test_nothing_to_move(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--data", str(self.root / "new"), "adopt-old-data", str(self.root / "none")]), 0)
        self.assertIn("nothing to copy", out.getvalue())
        self.assertFalse((self.root / "new" / "settings.env").exists())


if __name__ == "__main__":
    unittest.main()
