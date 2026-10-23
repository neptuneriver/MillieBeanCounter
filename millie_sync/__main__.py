"""Command line: python -m millie_sync <command>.

The .command files on the Mac run these inside Docker for you.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__, config


def _print_problem(prefix: str, message: str) -> None:
    lines = str(message).splitlines() or [""]
    print(f"{prefix}{lines[0]}")
    for line in lines[1:]:
        print(f"    {line}")


def cmd_check(data_dir: Path) -> int:
    from .huckleberry import Huckleberry, HuckleberryError
    from .mailbox import Mailbox, MailError

    try:
        settings = config.load(data_dir)
    except config.ConfigError as exc:
        _print_problem("✗ Settings: ", exc)
        return 1
    print("✓ Settings are complete.")
    ok = True
    today = datetime.now(settings.timezone).date()
    try:
        with Mailbox(settings) as mailbox:
            found = mailbox.recent(today)
        print(
            f"✓ Gmail: signed in as {settings.gmail_address}. "
            f"{len(found)} daily-sheet email(s) in the last {settings.lookback_days} days."
        )
    except MailError as exc:
        ok = False
        _print_problem("✗ Gmail: ", exc)

    async def probe():
        async with Huckleberry(settings) as hb:
            return hb.child_name, [names[0] for _, names in hb.children]

    try:
        child, everyone = asyncio.run(probe())
        extra = f" (children on the account: {', '.join(everyone)})" if len(everyone) > 1 else ""
        print(f"✓ Huckleberry: signed in. Entries will go to {child}{extra}.")
    except HuckleberryError as exc:
        ok = False
        _print_problem("✗ Huckleberry: ", exc)
    except Exception as exc:  # unexpected library or network trouble
        ok = False
        _print_problem("✗ Huckleberry: ", f"unexpected problem ({exc.__class__.__name__}: {exc})")
    if settings.google_sheet:
        from .gsheets import GoogleSheet, SheetsError, load_key

        try:
            key = load_key(data_dir)
            if key is None:
                ok = False
                print("✗ Google Sheet: no service account key is saved yet. Run Setup.command again.")
            else:
                title = GoogleSheet.connect(settings.google_sheet, key).title()
                print(f'✓ Google Sheet: "{title}" is reachable as {key.get("client_email")}.')
        except SheetsError as exc:
            ok = False
            _print_problem("✗ Google Sheet: ", exc)
    else:
        print("• Google Sheet copy is off (the files from Get Data.command still work).")
    if settings.notify:
        print(f"✓ Summary emails go to {', '.join(settings.notify)}.")
    else:
        print("• Summary emails are off.")
    if settings.allowed_senders:
        print(f"• Sheets are accepted from: {', '.join(settings.allowed_senders)}.")
    return 0 if ok else 1


def cmd_run(data_dir: Path, dry_run: bool) -> int:
    from .journal import Busy
    from .sync import describe_report, run_once

    try:
        settings = config.load(data_dir)
        report = run_once(settings, dry_run=dry_run)
    except config.ConfigError as exc:
        _print_problem("Settings problem: ", exc)
        return 1
    except Busy as exc:
        print(exc)
        return 1
    except Exception as exc:
        _print_problem("Problem: ", f"{exc}" or exc.__class__.__name__)
        return 1
    print(describe_report(report))
    return 1 if report.error or any(m.status == "error" for m in report.messages) else 0


def cmd_status(data_dir: Path) -> int:
    from .journal import Journal
    from .service import read_status

    status = read_status(data_dir)
    try:
        tz = config.load(data_dir).timezone
    except config.ConfigError:
        tz = ZoneInfo(config.DEFAULTS["TIMEZONE"])

    rows, exported = [], None
    journal_path = data_dir / "journal.sqlite3"
    if journal_path.exists():
        journal = Journal(journal_path)
        try:
            rows = journal.recent_messages(10)
            exported = journal.get("last_export")  # automatic and Get Data.command exports alike
        finally:
            journal.close()

    def when(value: str | None) -> str:
        if not value:
            return "never"
        moment = datetime.fromisoformat(value).astimezone(tz)
        return moment.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")

    if status:
        state = status.get("state", "unknown")
        print(f"State: {state}")
        if state == "stopped":
            print(f"Last check: {when(status.get('last_check'))}   (stopped; Start.command turns it back on)")
        else:
            print(f"Last check: {when(status.get('last_check'))}   Next check: {when(status.get('next_check'))}")
        print(f"Last time something was added: {when(status.get('last_import'))}")
    else:
        print("The sync hasn't run yet.")
    print(f"Last data export: {when(exported)}")
    if status.get("last_error") and status.get("state") == "problem":
        _print_problem("Problem: ", status["last_error"])
    if status.get("export_error"):
        _print_problem("Export problem: ", status["export_error"])
    if rows:
        print("\nRecent daily-sheet emails:")
        for row in rows:
            days = row["sheet_days"].strip("[]").replace('"', "") or "-"
            detail = f"  ({row['detail']})" if row["detail"] and row["status"] != "done" else ""
            print(f"  {row['status']:<8} sheet {days:<12} {row['subject'][:70]}{detail}")
    return 0


def cmd_parse(path: Path, tz_name: str) -> int:
    from .sheet import clock_text, html_to_lines, parse_email, parse_lines, text_to_lines

    tz = ZoneInfo(tz_name)
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        result = parse_lines(html_to_lines(data.decode("utf-8", "replace")), tz)
    elif suffix == ".txt":
        result = parse_lines(text_to_lines(data.decode("utf-8", "replace")), tz)
    else:
        result = parse_email(data, tz)
    for error in result.errors:
        print(f"Couldn't read a sheet: {error}")
    for sheet in result.sheets:
        print(sheet.title)
        for event in sheet.events:
            print(f"  {clock_text(event.start):>8}  {event.describe()}")
        for problem in sheet.problems:
            print(f"  needs a look: {problem}")
        for note in sheet.notes:
            print(f"  note: {note}")
    if not result.sheets and not result.errors:
        print("No daily sheet found in that file.")
    return 0 if result.sheets else 1


def cmd_save_settings(data_dir: Path) -> int:
    text = sys.stdin.read(65536)
    updates = config.parse_env(text)
    try:
        saved = config.save(updates, data_dir)
    except config.ConfigError as exc:
        _print_problem("Not saved: ", exc)
        return 1
    print("Settings saved inside Docker.")
    try:
        config.from_values(saved, data_dir)
    except config.ConfigError as exc:
        _print_problem("Still missing or wrong: ", exc)
        return 1
    return 0


def cmd_skip_new(data_dir: Path) -> int:
    """Mark every daily-sheet email currently in Gmail as handled, without adding anything."""
    from .journal import Journal, exclusive
    from .mailbox import Mailbox, MailError

    try:
        settings = config.load(data_dir)
        with exclusive(data_dir / "sync.lock"), Mailbox(settings) as mailbox:
            headers = mailbox.recent(datetime.now(settings.timezone).date())
            journal = Journal(data_dir / "journal.sqlite3")
            skipped = 0
            try:
                for header in headers:
                    if journal.message_status(header.key) not in ("done", "ignored"):
                        journal.mark_message(
                            header.key,
                            subject=header.subject,
                            sender=header.sender,
                            received=header.received.isoformat() if header.received else None,
                            status="ignored",
                            detail="skipped during setup",
                        )
                        skipped += 1
            finally:
                journal.close()
    except (config.ConfigError, MailError) as exc:
        _print_problem("Problem: ", exc)
        return 1
    print(f"Skipped {skipped} daily-sheet email(s). Sheets that arrive from now on will be added.")
    return 0


def cmd_test_email(data_dir: Path) -> int:
    from .mailbox import MailError, send_email

    try:
        settings = config.load(data_dir)
    except config.ConfigError as exc:
        _print_problem("Settings problem: ", exc)
        return 1
    if not settings.notify:
        print("NOTIFY_EMAIL is empty, so there's nowhere to send a test email.")
        return 1
    try:
        send_email(
            settings,
            "Millie sync is set up",
            "This is a test from the Millie daycare sync on your Mac. "
            "After each daily sheet it logs, you'll get a summary like the one in README.md.",
        )
    except MailError as exc:
        _print_problem("✗ Test email: ", exc)
        return 1
    print(f"✓ Test email sent to {', '.join(settings.notify)}.")
    return 0


def cmd_export(data_dir: Path, *, as_tar: bool, compare: bool) -> int:
    """Fresh export of everything. With --tar the files go to stdout (messages to stderr)."""
    from .data import export_now
    from .export import tar_bytes
    from .huckleberry import HuckleberryError
    from .journal import Busy
    from .verify import NotAHuckleberryFile, report

    out = sys.stderr if as_tar else sys.stdout
    try:
        settings = config.load(data_dir)
        theirs = sys.stdin.buffer.read().decode("utf-8", "replace") if compare else None
        if compare and not (theirs or "").strip():
            print("No Huckleberry CSV was given to compare with.", file=out)
            return 1
        print("Reading everything from Huckleberry...", file=out, flush=True)
        result = export_now(settings, compare_with=theirs)
    except (config.ConfigError, HuckleberryError, NotAHuckleberryFile, Busy) as exc:
        lines = str(exc).splitlines() or [""]
        print("Problem: " + "\n    ".join(lines), file=out)
        return 1
    print(result.summary(), file=out)
    if result.comparison is not None:
        print(file=out)
        print(report(result.comparison), file=out)
    if as_tar:
        sys.stdout.buffer.write(tar_bytes(result.directory))
        sys.stdout.buffer.flush()
    return 0 if not result.comparison or result.comparison.perfect else 1


def cmd_save_google_key(data_dir: Path) -> int:
    from .gsheets import SheetsError, save_key

    try:
        email = save_key(data_dir, sys.stdin.read(200_000))
    except SheetsError as exc:
        _print_problem("✗ ", exc)
        return 1
    print(f"✓ Google key saved. Share your Google Sheet with {email} as an Editor.")
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="millie_sync", description="Daycare daily sheets -> Huckleberry")
    parser.add_argument("--data", type=Path, default=config.DATA_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("serve", help="Check Gmail every few minutes (what Docker runs)")
    sub.add_parser("run", help="Check Gmail once now and log anything new")
    sub.add_parser("preview", help="Show what would be logged, without writing anything")
    sub.add_parser("check", help="Test the Gmail, Huckleberry and Google Sheet connections")
    sub.add_parser("status", help="Show what the sync has been doing")
    export = sub.add_parser("export", help="Export all Huckleberry data (workbook, CSVs, SQLite, Google Sheet)")
    export.add_argument("--tar", action="store_true", help="write the export folder to stdout as a tar file")
    export.add_argument("--compare", action="store_true", help="compare with a Huckleberry CSV given on stdin")
    parse = sub.add_parser("parse", help="Read a saved .eml/.html/.txt daily sheet (no logins needed)")
    parse.add_argument("file", type=Path)
    parse.add_argument("--timezone", default=config.DEFAULTS["TIMEZONE"])
    sub.add_parser("test-email", help="Send a test summary email")
    sub.add_parser("skip-new", help="Mark the daily sheets now in Gmail as handled without adding them")
    sub.add_parser("version", help="Show the version")
    for hidden in ("save-settings", "show-settings", "save-google-key", "health"):  # used by the .command files
        sub.add_parser(hidden)
    args = parser.parse_args(argv)
    data_dir: Path = args.data
    command = args.command or "serve"

    if command == "serve":
        from .service import serve

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(name)s: %(message)s")
        return serve(data_dir)
    # Interactive commands explain problems themselves; keep library chatter out.
    logging.basicConfig(level=logging.CRITICAL)
    if command == "run":
        return cmd_run(data_dir, dry_run=False)
    if command == "preview":
        return cmd_run(data_dir, dry_run=True)
    if command == "check":
        return cmd_check(data_dir)
    if command == "status":
        return cmd_status(data_dir)
    if command == "parse":
        return cmd_parse(args.file, args.timezone)
    if command == "save-settings":
        return cmd_save_settings(data_dir)
    if command == "show-settings":
        from .gsheets import key_path

        print("\n".join(config.public_view(config.read_raw(data_dir))))
        print(f"GOOGLE_KEY_SAVED={'yes' if key_path(data_dir).exists() else 'no'}")
        return 0
    if command == "test-email":
        return cmd_test_email(data_dir)
    if command == "skip-new":
        return cmd_skip_new(data_dir)
    if command == "export":
        return cmd_export(data_dir, as_tar=args.tar, compare=args.compare)
    if command == "save-google-key":
        return cmd_save_google_key(data_dir)
    if command == "health":
        from .service import health

        return health(data_dir)
    if command == "version":
        print(__version__)
        return 0
    parser.error(f"unknown command {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
