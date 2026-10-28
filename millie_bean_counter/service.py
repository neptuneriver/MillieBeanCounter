"""The always-on loop that runs inside Docker: check Gmail every few minutes."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .journal import Busy, Journal
from .mailbox import MailError, send_email
from .sync import describe_report, error_email, run_once

SETUP_WAIT_MINUTES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _log_zone():
    """The daycare's time zone from the settings, so log times match the sheets."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    saved = config.read_raw().get("TIMEZONE", "") if config.settings_path().exists() else ""
    try:
        return ZoneInfo(saved or os.environ.get("TZ") or config.DEFAULTS["TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def log(message: str) -> None:
    stamp = datetime.now(_log_zone()).strftime("%Y-%m-%d %H:%M:%S")
    for line in message.splitlines() or [""]:
        print(f"{stamp}  {line}", flush=True)


def status_path(data_dir: Path) -> Path:
    return data_dir / "status.json"


def read_status(data_dir: Path) -> dict:
    try:
        return json.loads(status_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def write_status(data_dir: Path, **values) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    current = read_status(data_dir)
    current.update(values, heartbeat=_stamp(_now()))
    fd, temp = tempfile.mkstemp(dir=data_dir, prefix=".status-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(current, handle, indent=2)
        os.replace(temp, status_path(data_dir))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def notify_error_once(settings: config.Settings, error: str, *, sender=None) -> None:
    """At most one email per distinct problem per day."""
    sender = sender or send_email
    if not settings.notify:
        return
    journal = Journal(settings.data_dir / "journal.sqlite3")
    try:
        key = f"{datetime.now(settings.timezone).date().isoformat()}|{error}"
        if journal.get("last_error_email") == key:
            return
        subject, body = error_email(error)
        try:
            sender(settings, subject, body)
        except MailError as exc:
            log(f"Couldn't email the problem report: {exc}")
            return
        journal.set("last_error_email", key)
    finally:
        journal.close()


def check_once(data_dir: Path) -> int:
    """One pass of the service loop. Returns how many minutes to wait before the next."""
    try:
        settings = config.load(data_dir)
    except config.ConfigError as exc:
        log(f"Waiting for Setup.command: {exc}")
        write_status(data_dir, state="needs setup", last_error=str(exc))
        return SETUP_WAIT_MINUTES
    checked = _stamp(_now())
    try:
        report = run_once(settings)
    except Busy as exc:
        log(str(exc))
        write_status(data_dir, state="running", poll_minutes=settings.poll_minutes, last_check=checked)
    except Exception as exc:  # keep the service alive no matter what
        message = str(exc) or exc.__class__.__name__
        log(f"Problem: {message}")
        if not isinstance(exc, MailError):
            log(traceback.format_exc().rstrip())
        write_status(
            data_dir,
            state="problem",
            poll_minutes=settings.poll_minutes,
            last_check=checked,
            last_error=message,
        )
        if not isinstance(exc, MailError):  # a Gmail problem can't be reported through Gmail
            notify_error_once(settings, message)
    else:
        text = describe_report(report)
        photos = report.photos
        if report.messages or report.error or photos.error or any(e.found for e in photos.emails):
            log(text)
        failed = [m for m in report.messages if m.status == "error"]
        problem = report.error or "; ".join(m.reason for m in failed)
        if photos.error:
            problem = "; ".join(p for p in (problem, f"Photos: {photos.error}") if p)
        values = {
            "state": "problem" if problem else "running",
            "poll_minutes": settings.poll_minutes,
            "last_check": checked,
            "last_result": text,
            "last_error": problem,
        }
        if report.added:
            values["last_import"] = checked
        if photos.saved:
            values["last_photos"] = checked
        write_status(data_dir, **values)
        if problem:
            notify_error_once(settings, problem)
        if not report.error:  # Huckleberry itself is fine, so the data export can run
            maybe_export(settings, report.added)
    return settings.poll_minutes


def maybe_export(settings: config.Settings, added: int) -> None:
    """Refresh the data export after new entries, and at least every EXPORT_EVERY_HOURS."""
    from .data import export_due, export_now, mark_export_pending

    try:
        if not export_due(settings, added):
            return
        result = export_now(settings)
    except Busy:
        log("Data export postponed: an export was already running. It will run at the next check.")
        if added:
            mark_export_pending(settings.data_dir)
        return
    except Exception as exc:  # the export must never stop the sync itself
        message = f"Data export failed: {exc}"
        log(message)
        if added:
            mark_export_pending(settings.data_dir)
        write_status(settings.data_dir, export_error=message)
        notify_error_once(settings, message)
        return
    log(result.summary())
    write_status(settings.data_dir, last_export=_stamp(_now()), export_error=result.sheet_error)
    if result.sheet_error:
        notify_error_once(settings, f"Google Sheet not updated: {result.sheet_error}")


def serve(data_dir: Path | None = None) -> int:
    data_dir = data_dir or config.DATA_DIR
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log("MillieBeanCounter started.")
    write_status(data_dir, state="starting", started=_stamp(_now()))
    while not stop.is_set():
        minutes = check_once(data_dir)
        write_status(data_dir, next_check=_stamp(_now() + timedelta(minutes=minutes)))
        stop.wait(minutes * 60)
    write_status(data_dir, state="stopped", next_check=None)
    log("MillieBeanCounter stopped.")
    return 0


def health(data_dir: Path | None = None) -> int:
    status = read_status(data_dir or config.DATA_DIR)
    try:
        beat = datetime.fromisoformat(status["heartbeat"])
    except (KeyError, TypeError, ValueError):
        return 1
    allowed = timedelta(minutes=int(status.get("poll_minutes") or SETUP_WAIT_MINUTES) + 15)
    return 0 if _now() - beat < allowed else 1
