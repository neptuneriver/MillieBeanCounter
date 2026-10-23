"""Check this export against the CSV Huckleberry emails you.

Both describe the same entries, so after lining them up by type and start time
every field should agree. Entries newer than the last one in Huckleberry's file
are left out (they were logged after you downloaded it).
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .export import HUCKLEBERRY_COLUMNS

FIELD_NAMES = HUCKLEBERRY_COLUMNS


@dataclass
class Comparison:
    theirs: int = 0
    ours_compared: int = 0
    ours_newer: int = 0
    same: int = 0
    only_theirs: list[list[str]] = field(default_factory=list)
    only_ours: list[list[str]] = field(default_factory=list)
    different: list[tuple[list[str], list[str]]] = field(default_factory=list)
    first: str = ""
    last: str = ""

    @property
    def perfect(self) -> bool:
        return not (self.only_theirs or self.only_ours or self.different)


class NotAHuckleberryFile(ValueError):
    pass


def read_their_csv(text: str) -> list[list[str]]:
    text = text.lstrip("\ufeff")  # a byte-order mark from Excel or Numbers
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or [h.strip() for h in header[: len(FIELD_NAMES)]] != FIELD_NAMES:
        raise NotAHuckleberryFile(
            "That doesn't look like Huckleberry's CSV export (its first line should start with "
            '"Type","Start","End","Duration").'
        )
    rows = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        rows.append((row + [""] * len(FIELD_NAMES))[: len(FIELD_NAMES)])
    return rows


def _normal(row: list[str]) -> tuple:
    """A row in a form where harmless differences disappear."""
    cells = [" ".join(str(c).split()).lower() for c in row]
    if cells[0] == "diaper" and "," in cells[6]:
        head, _, parts = cells[6].partition(",")
        cells[6] = head + ", " + " ".join(sorted(parts.split()))
    return tuple(cells)


def compare(theirs: list[list[str]], ours: list[list[str]]) -> Comparison:
    result = Comparison(theirs=len(theirs))
    if not theirs:
        result.only_ours = list(ours)
        result.ours_compared = len(ours)
        return result
    result.first = min(r[1] for r in theirs)
    result.last = max(r[1] for r in theirs)
    compared = [r for r in ours if r[1] <= result.last]
    result.ours_newer = len(ours) - len(compared)
    result.ours_compared = len(compared)

    def slot(row: list[str]) -> tuple:
        return (row[0].lower(), row[1])

    groups_theirs: dict[tuple, list] = defaultdict(list)
    groups_ours: dict[tuple, list] = defaultdict(list)
    for row in theirs:
        groups_theirs[slot(row)].append(row)
    for row in compared:
        groups_ours[slot(row)].append(row)
    for key in sorted(set(groups_theirs) | set(groups_ours), key=lambda k: (k[1], k[0])):
        a, b = list(groups_theirs.get(key, [])), list(groups_ours.get(key, []))
        # Exact (normalised) matches first...
        for row in list(a):
            twin = next((other for other in b if _normal(other) == _normal(row)), None)
            if twin is not None:
                a.remove(row)
                b.remove(twin)
                result.same += 1
        # ...then whatever is left at the same type and time differs.
        while a and b:
            result.different.append((a.pop(0), b.pop(0)))
        result.only_theirs += a
        result.only_ours += b
    return result


def _entries(count: int) -> str:
    return "1 entry" if count == 1 else f"{count} entries"


def _verb(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _show(row: list[str]) -> str:
    return " | ".join(f"{name}: {value}" for name, value in zip(FIELD_NAMES, row, strict=False) if value)


def report(result: Comparison, limit: int = 25) -> str:
    lines = []
    if not result.theirs:
        return "Huckleberry's file has no entries to compare."
    lines.append(
        f"Huckleberry's file: {result.theirs} entries from {result.first} to {result.last}. "
        f"This export has {result.ours_compared} entries in that span"
        + (f" (and {result.ours_newer} newer ones, not compared)." if result.ours_newer else ".")
    )
    if result.perfect:
        lines.append(f"✓ All {_entries(result.same)} match." if result.same != 1 else "✓ The 1 entry matches.")
        return "\n".join(lines)
    lines.append(f"✓ {_entries(result.same)} {_verb(result.same, 'matches', 'match')} exactly.")
    if result.different:
        fields = Counter()
        for theirs, ours in result.different:
            for name, x, y in zip(FIELD_NAMES, _normal(theirs), _normal(ours), strict=True):
                if x != y:
                    fields[name] += 1
        summary = ", ".join(f"{name} ({count})" for name, count in fields.most_common())
        count = len(result.different)
        lines.append(f"✗ {_entries(count)} {_verb(count, 'differs', 'differ')}. Fields that differ: {summary}.")
        for theirs, ours in result.different[:limit]:
            lines.append(f"    Huckleberry: {_show(theirs)}")
            lines.append(f"    This export: {_show(ours)}")
    if result.only_theirs:
        count = len(result.only_theirs)
        lines.append(f"✗ {_entries(count)} {_verb(count, 'is', 'are')} only in Huckleberry's file:")
        lines += [f"    {_show(row)}" for row in result.only_theirs[:limit]]
    if result.only_ours:
        count = len(result.only_ours)
        lines.append(f"✗ {_entries(count)} {_verb(count, 'is', 'are')} only in this export:")
        lines += [f"    {_show(row)}" for row in result.only_ours[:limit]]
    hidden = max(0, len(result.different) - limit) + max(0, len(result.only_theirs) - limit)
    hidden += max(0, len(result.only_ours) - limit)
    if hidden:
        lines.append(f"    ...and {hidden} more (all of them are in verify_report.txt in the export folder).")
    return "\n".join(lines)
