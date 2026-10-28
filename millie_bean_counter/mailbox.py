"""Gmail access over IMAP (reading) and SMTP (optional summary emails).

Both use the Gmail address plus a Google App Password. The inbox is opened
read-only and messages are fetched with BODY.PEEK, so nothing in Gmail is
changed, moved or marked as read.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from .config import Settings

IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
SUBJECT_WORDS = "Daily Sheet"
HEADER_BATCH = 400  # UIDs per header FETCH, so the command line stays short
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

APP_PASSWORD_HELP = (
    "Gmail didn't accept the login for {address}. It needs a Google App Password "
    "(16 letters from myaccount.google.com/apppasswords), not the normal Gmail password. "
    "App passwords stop working if the account's password is changed; then make a new one "
    "and run Setup.command again."
)


class MailError(Exception):
    """Gmail problem. The message is written for a person."""


@dataclass(frozen=True)
class MailHeader:
    uid: str
    key: str  # Message-ID, used to remember what was handled
    subject: str
    sender: str  # bare address, lowercase
    received: datetime | None


def is_daily_sheet(subject: str) -> bool:
    return SUBJECT_WORDS.lower() in subject.lower()


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _header_from_bytes(uid: str, data: bytes, folder: str) -> MailHeader:
    message = email.message_from_bytes(data, policy=email.policy.default)
    message_id = str(message.get("Message-ID", "") or "").strip()
    subject = " ".join(str(message.get("Subject", "") or "").split())
    addresses = getaddresses([str(message.get("From", "") or "")])
    sender = addresses[0][1].strip().lower() if addresses else ""
    try:
        received = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        received = None
    return MailHeader(
        uid=uid,
        key=message_id or f"uid:{folder}:{uid}",
        subject=subject,
        sender=sender,
        received=received,
    )


def _text(value) -> str:
    return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)


def _fetch_parts(data: list) -> list[tuple[str, bytes]]:
    """(uid, payload) pairs from an imaplib FETCH response.

    The UID can come before the literal (inside the tuple) or after it (in the
    trailing b' UID 12)' chunk), so look in both places.
    """
    parts = []
    items = list(data or [])
    for index, item in enumerate(items):
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        match = re.search(r"\bUID (\d+)", _text(item[0]))
        if not match and index + 1 < len(items) and isinstance(items[index + 1], bytes):
            match = re.search(r"\bUID (\d+)", _text(items[index + 1]))
        if match:
            parts.append((match[1], item[1]))
    return parts


class Mailbox:
    def __init__(self, settings: Settings, *, imap_factory=imaplib.IMAP4_SSL):
        self.settings = settings
        self.imap_factory = imap_factory
        self.imap = None

    def __enter__(self) -> Mailbox:
        try:
            self.imap = self.imap_factory(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=60)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(
                f"Couldn't reach Gmail ({exc.__class__.__name__}: {exc}). Check the internet connection."
            ) from exc
        try:
            self.imap.login(self.settings.gmail_address, self.settings.gmail_app_password)
        except imaplib.IMAP4.error as exc:
            self._logout()
            said = exc.args[0].decode("utf-8", "replace") if exc.args and isinstance(exc.args[0], bytes) else exc
            raise MailError(
                APP_PASSWORD_HELP.format(address=self.settings.gmail_address) + f" (Gmail said: {said})"
            ) from exc
        except OSError as exc:
            self._logout()
            raise MailError(f"Couldn't reach Gmail ({exc.__class__.__name__}: {exc}).") from exc
        folder = self.settings.imap_folder
        try:
            status, _ = self.imap.select(f'"{folder}"', readonly=True)
        except (OSError, imaplib.IMAP4.error) as exc:
            self._logout()
            raise MailError(f"Couldn't open the Gmail folder {folder!r} ({exc}).") from exc
        if status != "OK":
            self._logout()
            raise MailError(f"Gmail folder {folder!r} wasn't found (IMAP_FOLDER).")
        return self

    def __exit__(self, *exc_info) -> None:
        self._logout()

    def _logout(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            self.imap = None

    def recent(self, today: date) -> list[MailHeader]:
        """Daily-sheet emails that arrived in the last LOOKBACK_DAYS days, oldest first."""
        since = imap_date(today - timedelta(days=self.settings.lookback_days))
        headers = self._headers(self._search("SINCE", since, "SUBJECT", f'"{SUBJECT_WORDS}"'))
        # Gmail matches subject words loosely; keep only real daily sheets.
        return [h for h in headers if is_daily_sheet(h.subject)]

    def since(self, day: date) -> list[MailHeader]:
        """Every email that arrived on or after day, whatever its subject, oldest first."""
        return self._headers(self._search("SINCE", imap_date(day)))

    def _search(self, *criteria: str) -> list[str]:
        try:
            status, data = self.imap.uid("SEARCH", *criteria)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(f"Gmail search failed ({exc}).") from exc
        if status != "OK":
            raise MailError(f"Gmail search failed ({status}).")
        return [uid for chunk in data or [] if chunk for uid in chunk.decode().split()]

    def _headers(self, uids: list[str]) -> list[MailHeader]:
        headers: list[MailHeader] = []
        for first in range(0, len(uids), HEADER_BATCH):
            batch = uids[first : first + HEADER_BATCH]
            try:
                status, data = self.imap.uid(
                    "FETCH", ",".join(batch), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])"
                )
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailError(f"Reading Gmail headers failed ({exc}).") from exc
            if status != "OK":
                raise MailError(f"Reading Gmail headers failed ({status}).")
            folder = self.settings.imap_folder
            headers += [_header_from_bytes(uid, payload, folder) for uid, payload in _fetch_parts(data)]
        return sorted(headers, key=lambda h: int(h.uid))

    def fetch(self, uid: str) -> bytes:
        try:
            status, data = self.imap.uid("FETCH", uid, "(UID BODY.PEEK[])")
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(f"Downloading an email from Gmail failed ({exc}).") from exc
        parts = _fetch_parts(data) if status == "OK" else []
        if not parts:
            raise MailError(f"Gmail didn't return email {uid}.")
        return parts[0][1]


def send_email(settings: Settings, subject: str, body: str, *, smtp_factory=smtplib.SMTP_SSL) -> None:
    if not settings.notify:
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"MillieBeanCounter <{settings.gmail_address}>"
    message["To"] = ", ".join(settings.notify)
    message.set_content(body)
    try:
        with smtp_factory(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=60) as smtp:
            smtp.login(settings.gmail_address, settings.gmail_app_password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(APP_PASSWORD_HELP.format(address=settings.gmail_address)) from exc
    except (TimeoutError, OSError, smtplib.SMTPException) as exc:
        raise MailError(f"Sending the summary email failed ({exc.__class__.__name__}: {exc}).") from exc
