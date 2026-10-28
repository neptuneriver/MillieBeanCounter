"""Settings.

They live in the Docker data volume (/data/settings.env), not in the project
folder, so passwords never end up in a zip you share or upload somewhere.
Setup.command writes them through `save-settings`.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATA_DIR = Path(os.environ.get("MILLIE_DATA", "/data"))

KEYS = (
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "HUCKLEBERRY_EMAIL",
    "HUCKLEBERRY_PASSWORD",
    "HUCKLEBERRY_CHILD",
    "DAYCARE_CHILD",
    "ALLOWED_SENDERS",
    "NOTIFY_EMAIL",
    "TIMEZONE",
    "POLL_MINUTES",
    "LOOKBACK_DAYS",
    "IMPORT_SINCE",
    "IMAP_FOLDER",
    "GOOGLE_SHEET",
    "EXPORT_EVERY_HOURS",
)
SECRET_KEYS = frozenset({"GMAIL_APP_PASSWORD", "HUCKLEBERRY_PASSWORD"})
DEFAULTS = {
    "TIMEZONE": "America/New_York",
    "POLL_MINUTES": "15",
    "LOOKBACK_DAYS": "7",
    "IMAP_FOLDER": "INBOX",
    "EXPORT_EVERY_HOURS": "24",
}

_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


class ConfigError(Exception):
    """Settings are missing or wrong. The message is written for a person."""


def settings_path(data_dir: Path | None = None) -> Path:
    return (data_dir or DATA_DIR) / "settings.env"


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines. Everything after the first '=' is the value, taken literally."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        if key.startswith("EXPORT "):
            key = key[len("EXPORT ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def read_raw(data_dir: Path | None = None) -> dict[str, str]:
    path = settings_path(data_dir)
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8-sig"))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(updates: dict[str, str], data_dir: Path | None = None) -> dict[str, str]:
    """Merge updates into the saved settings.

    A blank password means "keep the saved one". A blank for anything else
    clears it (for example NOTIFY_EMAIL= turns summary emails off).
    """
    current = read_raw(data_dir)
    unknown = sorted(set(updates) - set(KEYS))
    if unknown:
        raise ConfigError("Unknown setting(s): " + ", ".join(unknown))
    for key, value in updates.items():
        if key in SECRET_KEYS:
            secret = re.sub(r"\s+", "", value) if key == "GMAIL_APP_PASSWORD" else value
            if secret:
                current[key] = secret
        else:
            current[key] = value.strip()
    lines = [
        "# Millie daycare -> Huckleberry settings. Written by Setup.command.",
        "# Re-run Setup.command to change them.",
    ]
    lines += [f"{key}={current[key]}" for key in KEYS if key in current]
    _atomic_write(settings_path(data_dir), "\n".join(lines) + "\n")
    return current


def public_view(values: dict[str, str]) -> list[str]:
    """Settings safe to print: secrets become KEY_SAVED=yes/no."""
    out = []
    for key in KEYS:
        if key in SECRET_KEYS:
            out.append(f"{key}_SAVED={'yes' if values.get(key) else 'no'}")
        else:
            out.append(f"{key}={values.get(key, '')}")
    return out


@dataclass(frozen=True)
class Settings:
    gmail_address: str
    gmail_app_password: str = field(repr=False)  # never printed, even in an error trace
    huckleberry_email: str
    huckleberry_password: str = field(repr=False)
    huckleberry_child: str
    daycare_child: str
    allowed_senders: tuple[str, ...]
    notify: tuple[str, ...]
    timezone_name: str
    poll_minutes: int
    lookback_days: int
    import_since: date | None
    imap_folder: str
    google_sheet: str  # link or ID of the Google Sheet to mirror into ("" = off)
    export_every_hours: int
    data_dir: Path

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def sender_allowed(self, address: str) -> bool:
        if not self.allowed_senders:
            return True
        address = address.strip().lower()
        domain = address.rpartition("@")[2]
        for entry in self.allowed_senders:
            if "@" in entry:
                if address == entry:
                    return True
            elif domain == entry or domain.endswith("." + entry):
                return True
        return False

    def daycare_child_matches(self, name: str) -> bool:
        if not self.daycare_child:
            return True
        wanted = self.daycare_child.casefold()
        name = name.strip().casefold()
        return name == wanted or name.split(" ")[0] == wanted


def _split(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,;]+", value.strip()) if part]


def from_values(values: dict[str, str], data_dir: Path | None = None) -> Settings:
    merged = {**DEFAULTS, **{k: v for k, v in values.items() if v != ""}}
    problems: list[str] = []

    def need(key: str, label: str) -> str:
        value = merged.get(key, "").strip()
        if not value:
            problems.append(f"{label} ({key}) is not set.")
        return value

    gmail = need("GMAIL_ADDRESS", "The Gmail address that receives the daily sheets").lower()
    if gmail and not _EMAIL.match(gmail):
        problems.append(f"GMAIL_ADDRESS {gmail!r} doesn't look like an email address.")
    app_password = re.sub(r"\s+", "", merged.get("GMAIL_APP_PASSWORD", ""))
    if not app_password:
        problems.append("The Gmail app password (GMAIL_APP_PASSWORD) is not set.")
    hb_email = need("HUCKLEBERRY_EMAIL", "Your Huckleberry login email")
    if hb_email and not _EMAIL.match(hb_email):
        problems.append(f"HUCKLEBERRY_EMAIL {hb_email!r} doesn't look like an email address.")
    hb_password = merged.get("HUCKLEBERRY_PASSWORD", "")
    if not hb_password:
        problems.append("Your Huckleberry password (HUCKLEBERRY_PASSWORD) is not set.")

    senders: list[str] = []
    for raw_entry in _split(merged.get("ALLOWED_SENDERS", "").lower()):
        entry = raw_entry.lstrip("@").removeprefix("*.")
        if _EMAIL.match(entry) or _DOMAIN.match(entry):
            senders.append(entry)
        else:
            problems.append(f"ALLOWED_SENDERS entry {raw_entry!r} isn't an email address or a domain.")
    notify = _split(merged.get("NOTIFY_EMAIL", ""))
    for address in notify:
        if not _EMAIL.match(address):
            problems.append(f"NOTIFY_EMAIL entry {address!r} doesn't look like an email address.")

    tz_name = merged["TIMEZONE"].strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TIMEZONE {tz_name!r} isn't a known time zone (example: America/New_York).")

    def number(key: str, low: int, high: int) -> int:
        try:
            value = int(merged[key])
        except ValueError:
            problems.append(f"{key} must be a whole number.")
            return int(DEFAULTS[key])
        if not low <= value <= high:
            problems.append(f"{key} must be between {low} and {high}.")
        return value

    poll = number("POLL_MINUTES", 5, 1440)
    lookback = number("LOOKBACK_DAYS", 1, 60)
    export_hours = number("EXPORT_EVERY_HOURS", 1, 168)
    sheet = merged.get("GOOGLE_SHEET", "").strip()
    if sheet and not re.search(r"(/spreadsheets/d/[A-Za-z0-9_-]{20,}|^[A-Za-z0-9_-]{20,}$)", sheet):
        problems.append("GOOGLE_SHEET should be the Google Sheet's link (it contains /spreadsheets/d/...).")
    since: date | None = None
    if merged.get("IMPORT_SINCE", "").strip():
        try:
            since = date.fromisoformat(merged["IMPORT_SINCE"].strip())
        except ValueError:
            problems.append("IMPORT_SINCE must look like 2026-10-12 (or be blank).")
    folder = merged["IMAP_FOLDER"].strip() or "INBOX"
    if '"' in folder or "\\" in folder:
        problems.append("IMAP_FOLDER can't contain quotes or backslashes.")

    if problems:
        raise ConfigError("\n".join(problems))
    return Settings(
        gmail_address=gmail,
        gmail_app_password=app_password,
        huckleberry_email=hb_email,
        huckleberry_password=hb_password,
        huckleberry_child=merged.get("HUCKLEBERRY_CHILD", "").strip(),
        daycare_child=merged.get("DAYCARE_CHILD", "").strip(),
        allowed_senders=tuple(senders),
        notify=tuple(notify),
        timezone_name=tz_name,
        poll_minutes=poll,
        lookback_days=lookback,
        import_since=since,
        imap_folder=folder,
        google_sheet=sheet,
        export_every_hours=export_hours,
        data_dir=data_dir or DATA_DIR,
    )


def load(data_dir: Path | None = None) -> Settings:
    values = read_raw(data_dir)
    if not values:
        raise ConfigError("No settings saved yet. Run Setup.command first.")
    return from_values(values, data_dir)
