"""Daycare photos: find the moments in Kaymbu emails and save them into Google
Drive, one folder per month, named with the date, the child's name and age:

    Josie Daycare Photos/October 2026/10/14 Josie 4m 11d Photo 1.jpg

Kaymbu's "Shared Moments" emails show each photo as an <img class="moment-image">
on Kaymbu's picture server, followed by a "Download this moment" link. When that
link leads straight to a picture, that copy (usually bigger) is saved; otherwise
the email-sized one is.

Photo numbers count up through each day, across all of that day's emails. Every
photo is remembered in the journal, so nothing is saved twice, and a photo that
was already saved from another email (the same moment shared again) is skipped.

Old moments can be sent in bulk: in Gmail, select them, then More > "Forward as
attachment" to the sync's inbox. Each attached email counts on its own date.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .config import Settings
from .gdrive import Drive, DriveError
from .journal import Journal
from .mailbox import Mailbox, MailError, MailHeader, is_daily_sheet
from .sheet import html_to_lines, text_to_lines

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_BYTES = 50_000_000
MIN_BYTES = 1_000  # anything smaller is an icon or a tracking pixel, not a photo
MAX_PHOTOS_PER_RUN = 100  # a big first catch-up is spread over a few checks
GIVE_UP_AFTER = 20  # tries for an email whose photos can't be downloaded
BACKFILL_DONE = "photos_backfill_done"
USER_AGENT = "Mozilla/5.0 (compatible; MillieBeanCounter)"

_MOMENT_PATH = re.compile(r"/moments?/", re.IGNORECASE)
_SHOWN_DATE = re.compile(r"^(" + "|".join(MONTHS) + r") (\d{1,2}), (\d{4})$")
_FORWARDED_DATE = re.compile(
    r"\bDate:\s*(?:[A-Za-z]{3,9},?\s+)?([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4}),? at (\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def _add_months(start: date, months: int) -> date:
    year, month = divmod(start.month - 1 + months, 12)
    year, month = start.year + year, month + 1
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def age_text(birthday: date | None, day: date) -> str:
    """'4m 11d' (months and days), or '2y 3m 5d' from the second birthday on."""
    if birthday is None or day < birthday:
        return ""
    months = (day.year - birthday.year) * 12 + day.month - birthday.month
    if _add_months(birthday, months) > day:
        months -= 1
    days = (day - _add_months(birthday, months)).days
    if months >= 24:
        return f"{months // 12}y {months % 12}m {days}d"
    return f"{months}m {days}d"


def month_folder(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.year}"


def photo_name(day: date, child: str, age: str, number: int, extension: str) -> str:
    """'10/14 Josie 4m 11d Photo 1.jpg'. Google Drive allows the slash in a name."""
    words = [f"{day:%m/%d}", child, age, f"Photo {number}"]
    return " ".join(word for word in words if word) + extension


# ---------------------------------------------------------------------------
# Reading the emails
# ---------------------------------------------------------------------------


@dataclass
class Moment:
    url: str  # the picture as it appears in the email
    download: str = ""  # the moment's own "Download" link, when it could be matched up


@dataclass
class PhotoEmail:
    key: str
    subject: str
    sender: str
    sent: datetime | None  # when the daycare sent it
    shown_day: date | None  # the date printed in the email ("October 14, 2026")
    moments: list[Moment] = field(default_factory=list)


def _real_src(src: str) -> str:
    """Gmail's image proxy keeps the original address after a '#'."""
    src = src.strip()
    if "#http" in src:
        src = src[src.index("#http") + 1 :]
    return src


class _MomentFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.downloads: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {name: value or "" for name, value in attrs}
        if tag == "a":
            self.links.append(values.get("href", ""))
        elif tag == "img":
            src = _real_src(values.get("src", ""))
            # Forwarded copies may rename classes (Gmail adds m_123... in front), so look inside them.
            if src.startswith("https://") and (
                "moment-image" in values.get("class", "") or _MOMENT_PATH.search(urlsplit(src).path)
            ):
                self.images.append(src)
            if values.get("alt", "").lower().startswith("download") and self.links and self.links[-1]:
                self.downloads.append(self.links[-1])

    def handle_endtag(self, tag):
        if tag == "a" and self.links:
            self.links.pop()


def find_moments(html: str) -> list[Moment]:
    finder = _MomentFinder()
    finder.feed(html)
    finder.close()
    # Each moment has its own Download link right below it. Pair them only when
    # the counts agree; otherwise the email-sized pictures are used.
    downloads = finder.downloads if len(finder.downloads) == len(finder.images) else [""] * len(finder.images)
    moments: dict[str, Moment] = {}
    for url, link in zip(finder.images, downloads, strict=True):
        moments.setdefault(url, Moment(url, link))
    return list(moments.values())


def _bodies(message: EmailMessage) -> tuple[str, str]:
    """(HTML, plain text) of a message, without descending into attached emails."""
    html_body = text_body = ""
    for part in message.walk():
        if part is not message and part.get_content_type() == "message/rfc822":
            break
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if kind == "text/html" and not html_body:
            html_body = content
        elif kind == "text/plain" and not text_body:
            text_body = content
    return html_body, text_body


def shown_date(lines: list[str]) -> date | None:
    for line in lines:
        match = _SHOWN_DATE.match(line)
        if match:
            try:
                return date(int(match[3]), MONTHS.index(match[1]) + 1, int(match[2]))
            except ValueError:
                return None
    return None


def forwarded_sent(lines: list[str], tz) -> datetime | None:
    """When the original was sent, from a forwarded email's 'Date: Wed, Oct 14, 2026 at 3:22 PM' line."""
    for line in lines:
        match = _FORWARDED_DATE.search(line)
        if not match:
            continue
        try:
            month = [name[:3] for name in MONTHS].index(match[1][:3].title()) + 1
            hour = int(match[4]) % 12 + (12 if match[6].upper() == "PM" else 0)
            return datetime(int(match[3]), month, int(match[2]), hour, int(match[5]), tzinfo=tz)
        except ValueError:
            continue
    return None


def _header_time(value) -> datetime | None:
    try:
        moment = parsedate_to_datetime(str(value or ""))
    except (TypeError, ValueError, IndexError):
        return None
    return moment if moment.tzinfo else None


def _photo_email(message: EmailMessage, key: str, subject: str, sender: str, sent: datetime | None, tz) -> PhotoEmail:
    html_body, text_body = _bodies(message)
    lines = (html_to_lines(html_body) if html_body else []) + (text_to_lines(text_body) if text_body else [])
    return PhotoEmail(
        key=key,
        subject=subject,
        sender=sender,
        sent=forwarded_sent(lines, tz) or sent,
        shown_day=shown_date(lines),
        moments=find_moments(html_body) if html_body else [],
    )


def read_email(raw: bytes, header: MailHeader, tz) -> list[PhotoEmail]:
    """The photo emails in one Gmail message: the message itself, or, when emails
    were forwarded as attachments, each attached email on its own."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    attached = (
        [part for part in message.iter_attachments() if part.get_content_type() == "message/rfc822"]
        if message.is_multipart()
        else []
    )
    if not attached:
        return [_photo_email(message, header.key, header.subject, header.sender, header.received, tz)]
    found = []
    for index, part in enumerate(attached, start=1):
        inner = part.get_content()
        subject = " ".join(str(inner.get("Subject", "") or "").split())
        if is_daily_sheet(subject):
            continue  # daily sheets are read by the sheet sync, not here
        addresses = getaddresses([str(inner.get("From", "") or "")])
        sender = addresses[0][1].strip().lower() if addresses else ""
        key = str(inner.get("Message-ID", "") or "").strip() or f"{header.key}#{index}"
        found.append(_photo_email(inner, key, subject, sender, _header_time(inner.get("Date")), tz))
    return found


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


class PhotoGone(Exception):
    """The picture can't be had from that link (it's gone, or it isn't a picture)."""


class PhotoTrouble(Exception):
    """A temporary problem downloading a picture; try again later."""


def _public_https(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


class Downloader:
    """Fetches pictures over HTTPS, following redirects (the Download links go
    through Kaymbu's click tracker first)."""

    def __init__(self, session=None):
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    def __call__(self, url: str) -> tuple[bytes, str]:
        if not _public_https(url):
            raise PhotoGone("not a public https link")
        try:
            response = self.session.get(
                url, timeout=60, stream=True, allow_redirects=True, headers={"User-Agent": USER_AGENT}
            )
        except Exception as exc:  # network trouble, or a redirect to an app link
            if exc.__class__.__name__ in ("InvalidSchema", "InvalidURL", "MissingSchema", "TooManyRedirects"):
                raise PhotoGone(f"the link doesn't lead to a picture ({exc.__class__.__name__})") from exc
            raise PhotoTrouble(f"{exc.__class__.__name__}: {exc}") from exc
        try:
            status = response.status_code
            if status == 429 or status >= 500:
                raise PhotoTrouble(f"HTTP {status}")
            if status >= 400:
                raise PhotoGone(f"HTTP {status}")
            mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if not mime.startswith("image/") or mime == "image/svg+xml":
                raise PhotoGone(f"not a picture ({mime or 'unknown type'})")
            chunks, total = [], 0
            for chunk in response.iter_content(256 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise PhotoGone("bigger than 50 MB")
                chunks.append(chunk)
        except (PhotoGone, PhotoTrouble):
            raise
        except Exception as exc:  # the connection dropped mid-download
            raise PhotoTrouble(f"{exc.__class__.__name__}: {exc}") from exc
        finally:
            response.close()
        data = b"".join(chunks)
        if len(data) < MIN_BYTES:
            raise PhotoGone("too small to be a photo")
        return data, mime


def load_moment(moment: Moment, get: Callable[[str], tuple[bytes, str]]) -> tuple[bytes, str, str]:
    """(bytes, type, which copy): the Download link's picture if it gives one, else the email's."""
    if moment.download:
        try:
            data, mime = get(moment.download)
            return data, mime, "download"
        except (PhotoGone, PhotoTrouble):
            pass  # the copy shown in the email is still fine
    data, mime = get(moment.url)
    return data, mime, "email"


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


@dataclass
class PhotoOutcome:
    key: str
    subject: str
    sender: str
    sent: datetime | None
    day: date | None = None
    status: str = "pending"  # done | ignored | error | preview
    reason: str = ""
    found: int = 0
    saved: list[str] = field(default_factory=list)  # "October 2026/10/14 Josie 4m 11d Photo 1.jpg"
    copies: list[str] = field(default_factory=list)  # for each saved photo: "download" or "email"
    already: int = 0  # saved on an earlier run
    duplicates: int = 0  # the same picture was already saved from another email
    skipped: list[str] = field(default_factory=list)  # pictures that couldn't be downloaded, and why


@dataclass
class PhotoReport:
    enabled: bool = False
    dry_run: bool = False
    folder: str = ""
    checked: int = 0  # emails looked at for photos this time
    emails: list[PhotoOutcome] = field(default_factory=list)
    more: bool = False  # stopped at MAX_PHOTOS_PER_RUN; the rest go at the next check
    error: str = ""

    @property
    def saved(self) -> int:
        return sum(len(e.saved) for e in self.emails)

    @property
    def waiting(self) -> int:
        return sum(e.found for e in self.emails if e.status == "preview")


class _Saver:
    """Uploads photos and keeps track of the Drive folders they go in."""

    def __init__(self, drive: Drive, journal: Journal, settings: Settings):
        self.drive = drive
        self.journal = journal
        self.settings = settings
        self.folders: dict[str, str] = {}

    def folder(self, name: str, parent: str) -> str:
        key = f"drive_folder:{parent}/{name}"
        if key not in self.folders:
            saved = self.journal.get(key)
            if saved and self.drive.usable(saved):
                folder_id = saved
            else:  # first time, or the folder was deleted: find it by name or make it
                folder_id = self.drive.find_folder(name, parent) or self.drive.create_folder(name, parent)
                self.journal.set(key, folder_id)
            self.folders[key] = folder_id
        return self.folders[key]

    def save(
        self,
        item: PhotoEmail,
        moment: Moment,
        known: dict | None,
        data: bytes,
        mime: str,
        variant: str,
        day: date,
        sha: str,
    ) -> str:
        month = month_folder(day)
        parent = self.folder(month, self.folder(self.settings.drive_folder, "root"))
        tag = hashlib.sha1(f"{item.key}|{moment.url}".encode()).hexdigest()
        file_id = None
        if known is None:
            child = self.settings.child_name
            age = age_text(self.settings.child_birthday, day)
            extension = EXTENSIONS.get(mime, ".jpg")
            known = self.journal.reserve_photo(
                message_key=item.key,
                source=moment.url,
                day=day,
                sha256=sha,
                nbytes=len(data),
                variant=variant,
                folder=month,
                name=lambda number: photo_name(day, child, age, number, extension),
            )
        else:  # an earlier upload was cut off; Google may have finished it anyway
            file_id = self.drive.find_tagged(tag)
        if not file_id:
            file_id = self.drive.upload(
                name=known["name"],
                data=data,
                mime=mime,
                parent=parent,
                description=_description(item, self.settings.timezone),
                modified=item.sent,
                tag=tag,
            )
        self.journal.finish_photo(known["id"], file_id)
        return f"{known['folder']}/{known['name']}"


def _description(item: PhotoEmail, tz) -> str:
    text = f'From the daycare email "{item.subject}"'
    if item.sent:
        sent = item.sent.astimezone(tz)
        clock = sent.strftime("%I:%M %p").lstrip("0")
        text += f", sent {sent:%b} {sent.day}, {sent.year} at {clock}"
    return text + ". Saved by MillieBeanCounter."


def save_photos(
    settings: Settings,
    journal: Journal,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    mailbox_factory: Callable | None = None,
    drive_factory: Callable | None = None,
    get: Callable[[str], tuple[bytes, str]] | None = None,
) -> PhotoReport:
    """Save the photos from new daycare emails into Google Drive."""
    report = PhotoReport(enabled=settings.drive_photos, dry_run=dry_run, folder=settings.drive_folder)
    if not settings.drive_photos:
        return report
    tz = settings.timezone
    now = (now or datetime.now(tz)).astimezone(tz)
    mailbox_factory = mailbox_factory or Mailbox
    drive_factory = drive_factory or (lambda s: Drive.from_saved(s.data_dir))
    get = get or Downloader()
    caught_up = bool(journal.get(BACKFILL_DONE))
    if caught_up:
        since = now.date() - timedelta(days=settings.lookback_days)
    else:  # the first time, go back to the birthday (or a year) for every photo so far
        since = settings.child_birthday or now.date() - timedelta(days=365)
    state = {"saver": None, "count": 0}

    def saver() -> _Saver:
        if state["saver"] is None:
            state["saver"] = _Saver(drive_factory(settings), journal, settings)
        return state["saver"]

    try:
        with mailbox_factory(settings) as mailbox:
            for header in mailbox.since(since):
                if report.error or report.more:
                    break
                if is_daily_sheet(header.subject) or not settings.sender_allowed(header.sender):
                    continue
                if journal.photo_message_status(header.key) in ("done", "ignored"):
                    continue
                report.checked += 1
                try:
                    items = read_email(mailbox.fetch(header.uid), header, tz)
                except MailError:
                    raise
                except Exception as exc:  # a reading bug must not stop the other emails
                    outcome = PhotoOutcome(header.key, header.subject, header.sender, header.received)
                    outcome.status, outcome.reason = "error", f"couldn't read this email ({exc.__class__.__name__})"
                    report.emails.append(outcome)
                    _remember(journal, outcome, dry_run)
                    continue
                inner = [item for item in items if item.key != header.key]
                for item in items:
                    if item.key != header.key and journal.photo_message_status(item.key) in ("done", "ignored"):
                        continue
                    outcome = PhotoOutcome(item.key, item.subject, item.sender or header.sender, item.sent)
                    report.emails.append(outcome)
                    _handle(item, outcome, report, journal, settings, saver, get, now, dry_run, state)
                    _remember(journal, outcome, dry_run)
                    if report.error or report.more:
                        break
                if inner and not dry_run and not (report.error or report.more):
                    # Emails forwarded as attachments: the wrapper is done once every attached one is.
                    statuses = {journal.photo_message_status(item.key) for item in inner}
                    finished = statuses <= {"done", "ignored"}
                    wrapper = PhotoOutcome(header.key, header.subject, header.sender, header.received)
                    wrapper.status = "done" if finished else "error"
                    wrapper.reason = "" if finished else "some of the attached emails aren't finished yet"
                    _remember(journal, wrapper, dry_run)
    except MailError as exc:
        report.error = f"Gmail: {exc}"
    except DriveError as exc:
        report.error = str(exc)
    if (
        not dry_run
        and not caught_up
        and not report.error
        and not report.more
        and not any(e.status == "error" for e in report.emails)
    ):
        journal.set(BACKFILL_DONE, now.isoformat(timespec="seconds"))
    return report


def _remember(journal: Journal, outcome: PhotoOutcome, dry_run: bool) -> None:
    if dry_run or outcome.status not in ("done", "ignored", "error"):
        return
    detail = outcome.reason
    if outcome.skipped and not detail:
        detail = "; ".join(outcome.skipped)
    journal.mark_photo_message(
        outcome.key,
        subject=outcome.subject,
        sender=outcome.sender,
        received=outcome.sent.isoformat() if outcome.sent else None,
        status=outcome.status,
        detail=detail,
        photos=len(outcome.saved) + outcome.already,
    )


def _handle(
    item: PhotoEmail,
    outcome: PhotoOutcome,
    report: PhotoReport,
    journal: Journal,
    settings: Settings,
    saver: Callable[[], _Saver],
    get,
    now: datetime,
    dry_run: bool,
    state: dict,
) -> None:
    outcome.found = len(item.moments)
    if not item.moments:
        outcome.status, outcome.reason = "ignored", "no daycare photos in this email"
        return
    day = item.shown_day or (item.sent.astimezone(settings.timezone).date() if item.sent else now.date())
    outcome.day = day
    if not settings.photo_download_links:  # PHOTO_DOWNLOAD_LINKS=no: only the copies shown in the email
        item.moments = [Moment(moment.url) for moment in item.moments]
    if dry_run:
        outcome.status = "preview"
        return
    try:
        for moment in item.moments:
            known = journal.photo(item.key, moment.url)
            if known and known["status"] in ("saved", "duplicate"):
                outcome.already += 1
                continue
            if known is None:
                same = journal.saved_photo_like(source=moment.url)
                if same:  # the same moment, shared again in another email
                    journal.record_duplicate_photo(
                        message_key=item.key,
                        source=moment.url,
                        day=day,
                        sha256=same["sha256"],
                        nbytes=same["bytes"],
                        variant=same["variant"],
                        same_as=same,
                    )
                    outcome.duplicates += 1
                    continue
            if state["count"] >= MAX_PHOTOS_PER_RUN:
                report.more = True  # left unfinished on purpose, so the next check carries on here
                outcome.reason = f"the rest follow at the next check ({MAX_PHOTOS_PER_RUN} photos per check)"
                return
            try:
                data, mime, variant = load_moment(moment, get)
            except PhotoGone as exc:
                outcome.skipped.append(f"a photo couldn't be downloaded ({exc})")
                continue
            sha = hashlib.sha256(data).hexdigest()
            if known is None:
                same = journal.saved_photo_like(sha256=sha)
                if same:  # the same picture under another link
                    journal.record_duplicate_photo(
                        message_key=item.key,
                        source=moment.url,
                        day=day,
                        sha256=sha,
                        nbytes=len(data),
                        variant=variant,
                        same_as=same,
                    )
                    outcome.duplicates += 1
                    continue
            outcome.saved.append(saver().save(item, moment, known, data, mime, variant, day, sha))
            outcome.copies.append(variant)
            state["count"] += 1
    except PhotoTrouble as exc:
        outcome.status = "error"
        outcome.reason = f"couldn't download a photo ({exc}); will try again"
    except DriveError as exc:
        outcome.status = "error"
        outcome.reason = str(exc)
        report.error = str(exc)
    else:
        nothing = not outcome.saved and not outcome.already and not outcome.duplicates
        if nothing and outcome.skipped:
            # Every picture failed. Maybe the links are dead, maybe the picture server is
            # refusing for now: keep trying for a while before giving up on this email.
            outcome.status = "error"
            outcome.reason = "none of the photos could be downloaded (" + "; ".join(outcome.skipped) + ")"
        else:
            outcome.status = "done"
    tries = journal.photo_message_attempts(item.key) + 1
    if outcome.status == "error" and not report.error and not report.more and tries >= GIVE_UP_AFTER:
        outcome.status = "ignored"
        outcome.reason = f"gave up after {GIVE_UP_AFTER} tries: {outcome.reason}"
