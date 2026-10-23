"""A small SQLite journal: what has been handled (so nothing is logged twice)
and the permanent audit keys given to every entry (like 102226AD1)."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

# Key = MMDDYY + who entered it + what it is + a running number for that day.
SOURCE_LETTERS = {"agent": "A", "app": "U"}
TYPE_LETTERS = {
    "diaper": "D",
    "sleep": "N",
    "bottle": "B",
    "breastfeeding": "F",
    "pumping": "P",
    "medicine": "M",
    "solids": "S",
    "temperature": "T",
    "growth": "G",
    "activity": "A",
    "potty": "O",
}
AGENT_TYPES = {"diaper": "diaper", "nap": "sleep", "bottle": "bottle"}  # sheet kind -> entry type


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_key(day: date, source: str, entry_type: str, seq: int) -> str:
    return f"{day:%m%d%y}{SOURCE_LETTERS[source]}{TYPE_LETTERS[entry_type]}{seq}"


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                key TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                received TEXT,
                status TEXT NOT NULL CHECK (status IN ('done', 'ignored', 'error')),
                detail TEXT NOT NULL DEFAULT '',
                sheet_days TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                key TEXT PRIMARY KEY,
                child_uid TEXT NOT NULL,
                kind TEXT NOT NULL,
                start TEXT NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('added', 'already_in_app')),
                sheet_day TEXT NOT NULL,
                message_key TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY,
                day TEXT NOT NULL,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                start_ts REAL NOT NULL,
                event_key TEXT UNIQUE,
                hb_id TEXT UNIQUE,
                created TEXT NOT NULL,
                UNIQUE (day, source, type, seq)
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        with self.db:
            if "details" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
            if "entry_key" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN entry_key TEXT")

    def close(self) -> None:
        self.db.close()

    # -- messages ----------------------------------------------------------
    def message_status(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM messages WHERE key = ?", (key,)).fetchone()
        return row["status"] if row else None

    def mark_message(
        self,
        key: str,
        *,
        subject: str,
        sender: str,
        received: str | None,
        status: str,
        detail: str = "",
        sheet_days: list[str] | None = None,
    ) -> None:
        now = _now()
        with self.db:
            self.db.execute(
                """
                INSERT INTO messages (key, subject, sender, received, status, detail, sheet_days, attempts, first_seen, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    status = excluded.status, detail = excluded.detail, sheet_days = excluded.sheet_days,
                    attempts = messages.attempts + 1, updated = excluded.updated
                """,
                (key, subject, sender, received, status, detail, json.dumps(sheet_days or []), now, now),
            )

    def recent_messages(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT * FROM messages ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- events from daily sheets ------------------------------------------
    def event_status(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM events WHERE key = ?", (key,)).fetchone()
        return row["status"] if row else None

    def event_summary(self, key: str) -> str | None:
        """How the entry read when it was handled, e.g. '1:00 PM Bottle 5 oz breast milk'."""
        row = self.db.execute("SELECT summary FROM events WHERE key = ?", (key,)).fetchone()
        return row["summary"] if row else None

    def record_event(
        self,
        key: str,
        *,
        child_uid: str,
        kind: str,
        start: datetime,
        summary: str,
        status: str,
        sheet_day: str,
        message_key: str,
        details: dict | None = None,
    ) -> str | None:
        """Remember a sheet event. Events the agent added get their audit key here,
        in the same transaction. Returns that key (None for 'already_in_app')."""
        with self.db:
            entry_key = None
            if status == "added":
                entry_key = self._new_key(start.date(), "agent", AGENT_TYPES[kind], start.timestamp(), event_key=key)
            self.db.execute(
                """
                INSERT INTO events (key, child_uid, kind, start, summary, status, sheet_day, message_key, updated,
                                    details, entry_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET status = excluded.status, updated = excluded.updated,
                    details = excluded.details, entry_key = COALESCE(events.entry_key, excluded.entry_key)
                """,
                (
                    key,
                    child_uid,
                    kind,
                    start.isoformat(),
                    summary,
                    status,
                    sheet_day,
                    message_key,
                    _now(),
                    json.dumps(details or {}, sort_keys=True),
                    entry_key,
                ),
            )
        return entry_key

    def events_for_day(self, sheet_day: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE sheet_day = ? ORDER BY start", (sheet_day,)).fetchall()
        return [dict(row) for row in rows]

    def agent_entries(self) -> list[dict]:
        """Everything the agent added, with the email it came from."""
        rows = self.db.execute(
            """
            SELECT e.*, m.subject AS email_subject, m.received AS email_received, k.hb_id AS hb_id
            FROM events e
            LEFT JOIN messages m ON m.key = e.message_key
            LEFT JOIN keys k ON k.key = e.entry_key
            WHERE e.status = 'added'
            ORDER BY e.start
            """
        ).fetchall()
        return [dict(row) for row in rows]

    # -- audit keys ----------------------------------------------------------
    def _new_key(self, day: date, source: str, entry_type: str, start_ts: float, *, event_key=None, hb_id=None) -> str:
        letter_source, letter_type = SOURCE_LETTERS[source], TYPE_LETTERS[entry_type]
        row = self.db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM keys WHERE day = ? AND source = ? AND type = ?",
            (day.isoformat(), letter_source, letter_type),
        ).fetchone()
        seq = row["seq"] + 1
        key = make_key(day, source, entry_type, seq)
        self.db.execute(
            "INSERT INTO keys (key, day, source, type, seq, start_ts, event_key, hb_id, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, day.isoformat(), letter_source, letter_type, seq, start_ts, event_key, hb_id, _now()),
        )
        return key

    def new_key(self, day: date, source: str, entry_type: str, start_ts: float, *, hb_id: str | None = None) -> str:
        with self.db:
            return self._new_key(day, source, entry_type, start_ts, hb_id=hb_id)

    def all_keys(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM keys ORDER BY day, source, type, seq")]

    def _link(self, key: str, hb_id: str, start_ts: float) -> None:
        self.db.execute("UPDATE keys SET hb_id = NULL WHERE hb_id = ? AND key != ?", (hb_id, key))
        self.db.execute("UPDATE keys SET hb_id = ?, start_ts = ? WHERE key = ?", (hb_id, start_ts, key))

    def link(self, key: str, hb_id: str, start_ts: float) -> None:
        """Tie a key to the Huckleberry entry it belongs to (or moved to)."""
        with self.db:
            self._link(key, hb_id, start_ts)

    # -- misc --------------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


class Busy(Exception):
    """Another sync is still running."""


@contextmanager
def exclusive(path: Path, wait_seconds: float = 180):
    """One sync at a time, across containers sharing the data volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("Another sync is still running; try again in a few minutes.") from None
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
