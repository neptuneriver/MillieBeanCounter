"""Permanent audit keys for every Huckleberry entry.

Format: MMDDYY + who + what + number, e.g. 102226AD1 = the first diaper the
agent (A) logged for October 22, 2026. Entries made in the Huckleberry app get
U instead of A. Type letters: D diaper, N nap/sleep, B bottle, F breastfeeding,
P pumping, M medicine, S solids, T temperature, G growth, A activity, O potty.

A key never changes once given, even if the entry is edited later, so you can
always trace a row back. Numbers are handed out in time order the first time
an entry is seen; something logged later for an earlier day gets the next
free number rather than renumbering the others.
"""

from __future__ import annotations

from .history import Entry
from .journal import SOURCE_LETTERS, TYPE_LETTERS, Journal

RELINK_SECONDS = 60
LETTER_TO_SOURCE = {letter: source for source, letter in SOURCE_LETTERS.items()}


def assign(journal: Journal, entries: list[Entry]) -> None:
    """Give every entry its key and source (agent or app), creating keys as needed."""
    known = journal.all_keys()
    by_hb = {row["hb_id"]: row for row in known if row["hb_id"]}
    current = {entry.hb_id for entry in entries}
    # Keys not tied to anything in Huckleberry right now: agent entries not linked
    # yet, or entries whose id changed (the app sometimes batches old entries).
    loose = [row for row in known if not row["hb_id"] or row["hb_id"] not in current]
    with journal.db:  # one transaction for the whole pass
        for entry in sorted(entries, key=lambda e: (e.start_ts, e.hb_id)):
            row = by_hb.get(entry.hb_id)
            if row is None:
                row = _closest(loose, entry)
                if row is not None:
                    loose.remove(row)
                    journal._link(row["key"], entry.hb_id, entry.start_ts)
            if row is not None:
                entry.key = row["key"]
                entry.source = LETTER_TO_SOURCE.get(row["source"], "app")
                continue
            entry.key = journal._new_key(entry.start.date(), "app", entry.type, entry.start_ts, hb_id=entry.hb_id)
            entry.source = "app"


def _closest(rows: list[dict], entry: Entry) -> dict | None:
    letter = TYPE_LETTERS[entry.type]
    best, best_gap = None, None
    for row in rows:
        if row["type"] != letter:
            continue
        gap = abs(row["start_ts"] - entry.start_ts)
        if gap <= RELINK_SECONDS and (best_gap is None or gap < best_gap):
            best, best_gap = row, gap
    return best
