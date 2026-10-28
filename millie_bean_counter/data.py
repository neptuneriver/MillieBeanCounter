"""Export everything: read Huckleberry, give every entry its key, write the
files, and mirror them to the Google Sheet if one is set up."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import export, gsheets, keys, verify
from .config import Settings
from .huckleberry import Huckleberry
from .journal import Journal, exclusive


@dataclass
class ExportResult:
    workbook: export.Workbook
    directory: Path
    skipped: dict[str, int] = field(default_factory=dict)
    sheet_cells: int | None = None
    sheet_error: str = ""
    comparison: verify.Comparison | None = None

    def summary(self) -> str:
        counts = {sheet.name: len(sheet.rows) for sheet in self.workbook.sheets}
        lines = [
            (
                f"Exported {counts['All entries']} Huckleberry entries "
                f"({counts['Daycare diapers']} diapers, {counts['Daycare naps']} naps and "
                f"{counts['Daycare bottles']} bottles of them added by the sync)."
            )
        ]
        if self.skipped:
            lines.append(
                "Skipped entries Huckleberry stores in a form this export doesn't know: "
                + ", ".join(f"{name} {count}" for name, count in sorted(self.skipped.items()))
            )
        if self.sheet_cells is not None:
            lines.append(f"Google Sheet updated ({self.sheet_cells:,} cells).")
        if self.sheet_error:
            lines.append(f"Google Sheet not updated: {self.sheet_error}")
        return "\n".join(lines)


def last_export(data_dir: Path) -> datetime | None:
    journal = Journal(data_dir / "journal.sqlite3")
    try:
        value = journal.get("last_export")
    finally:
        journal.close()
    return datetime.fromisoformat(value) if value else None


def export_due(settings: Settings, added: int, now: datetime | None = None) -> bool:
    if added:
        return True
    journal = Journal(settings.data_dir / "journal.sqlite3")
    try:
        if journal.get("export_pending"):  # an export after new entries didn't finish
            return True
    finally:
        journal.close()
    last = last_export(settings.data_dir)
    now = now or datetime.now(timezone.utc)
    return last is None or now - last >= timedelta(hours=settings.export_every_hours)


def mark_export_pending(data_dir: Path) -> None:
    """New entries went in but the export didn't run: try again at the next check."""
    journal = Journal(data_dir / "journal.sqlite3")
    try:
        journal.set("export_pending", "1")
    finally:
        journal.close()


def export_now(
    settings: Settings,
    *,
    huckleberry_factory: Callable | None = None,
    push_sheet: bool = True,
    compare_with: str | None = None,
    sheet_factory: Callable | None = None,
) -> ExportResult:
    """Read everything from Huckleberry and write a fresh export. Raises
    HuckleberryError if Huckleberry can't be read; Google Sheets problems are
    reported in the result instead, because the files are still good."""
    factory = huckleberry_factory or Huckleberry
    theirs = verify.read_their_csv(compare_with) if compare_with is not None else None

    async def read():
        async with factory(settings) as hb:
            return await hb.read_history()

    history = asyncio.run(read())
    with exclusive(settings.data_dir / "sync.lock"):
        journal = Journal(settings.data_dir / "journal.sqlite3")
        try:
            keys.assign(journal, history.entries)
            workbook = export.build(history, journal, settings.timezone)
            folder = export.fresh_directory(settings.data_dir / "exports")
            export.write_all(workbook, folder)
            comparison = None
            if theirs is not None:
                comparison = verify.compare(theirs, workbook.huckleberry_rows)
                (folder / "verify_report.txt").write_text(verify.report(comparison, limit=10**9) + "\n")
            final = settings.data_dir / "exports" / "latest"
            export.replace_directory(folder, final)
            journal.set("last_export", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            journal.set("export_pending", "")
        finally:
            journal.close()
    result = ExportResult(workbook, final, history.skipped, comparison=comparison)
    if push_sheet and settings.google_sheet:
        try:
            key = gsheets.load_key(settings.data_dir)
            if key is None:
                result.sheet_error = "no Google service account key is saved yet (run Setup.command)."
            else:
                sheet = (sheet_factory or gsheets.GoogleSheet.connect)(settings.google_sheet, key)
                result.sheet_cells = sheet.push(workbook)
        except gsheets.SheetsError as exc:
            result.sheet_error = str(exc)
    return result
