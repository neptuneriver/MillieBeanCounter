"""Test doubles: an in-memory Firestore and Gmail.

The real py-huckleberry-api code runs against FakeFirestore, so the tests check
the exact documents the library would write to Huckleberry.
"""

from __future__ import annotations

import copy
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from millie_bean_counter.config import from_values
from millie_bean_counter.huckleberry import EventTimeAPI
from millie_bean_counter.mailbox import MailHeader, _header_from_bytes

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------


def _merge(target: dict, data: dict) -> None:
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocument:
    def __init__(self, store: FakeFirestore, path: tuple):
        self.store = store
        self.path = path
        self.id = path[-1]

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.store, self.path + (name,))

    async def get(self, **kwargs) -> FakeSnapshot:
        return FakeSnapshot(self.id, self.store.docs.get(self.path))

    async def set(self, data: dict, merge: bool = False) -> None:
        self.store.before_write(self.path, data)
        self.store.writes.append((self.path, copy.deepcopy(data), merge))
        if merge and self.path in self.store.docs:
            _merge(self.store.docs[self.path], data)
        else:
            self.store.docs[self.path] = copy.deepcopy(data)

    async def update(self, data: dict) -> None:
        self.store.before_write(self.path, data)
        self.store.writes.append((self.path, copy.deepcopy(data), "update"))
        doc = self.store.docs.setdefault(self.path, {})
        for dotted, value in data.items():
            target = doc
            *parents, last = dotted.split(".")
            for part in parents:
                target = target.setdefault(part, {})
            target[last] = copy.deepcopy(value)


def _matches(data: dict, field_path: str, op: str, value) -> bool:
    if field_path not in data:
        return False
    actual = data[field_path]
    if op == "==":
        return actual == value
    try:
        return {">=": actual >= value, "<": actual < value, ">": actual > value, "<=": actual <= value}[op]
    except TypeError:
        return False


class FakeCollection:
    def __init__(self, store: FakeFirestore, path: tuple, filters: tuple = ()):
        self.store = store
        self.path = path
        self.filters = filters

    def document(self, doc_id: str | None = None) -> FakeDocument:
        return FakeDocument(self.store, self.path + (doc_id or uuid.uuid4().hex[:20],))

    def where(self, field_path=None, op_string=None, value=None, *, filter=None) -> FakeCollection:
        f = filter if filter is not None else SimpleNamespace(field_path=field_path, op_string=op_string, value=value)
        return FakeCollection(self.store, self.path, self.filters + ((f.field_path, f.op_string, f.value),))

    def order_by(self, *args, **kwargs) -> FakeCollection:
        return self

    async def stream(self, **kwargs):
        self.store.reads += 1
        for path, data in sorted(self.store.docs.items()):
            if len(path) == len(self.path) + 1 and path[:-1] == self.path:
                if all(_matches(data, *f) for f in self.filters):
                    yield FakeSnapshot(path[-1], data)


class FakeFirestore:
    def __init__(self):
        self.docs: dict[tuple, dict] = {}
        self.writes: list = []
        self.reads = 0
        self.fail_on_write: int | None = None  # raise on the Nth write (1-based)

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, (name,))

    def before_write(self, path, data) -> None:
        if self.fail_on_write is not None and len(self.writes) + 1 >= self.fail_on_write:
            self.fail_on_write = None
            raise RuntimeError("simulated network failure while writing")

    def intervals(self, collection: str, child: str = "child-1") -> list[dict]:
        rows = [data for path, data in self.docs.items() if path[:3] == (collection, child, "intervals")]
        return sorted(rows, key=lambda r: r["start"])

    def add_interval(self, collection: str, data: dict, child: str = "child-1") -> None:
        self.docs[(collection, child, "intervals", uuid.uuid4().hex[:16])] = data


def family(children=(("child-1", "Josie", "Josie"),)) -> FakeFirestore:
    store = FakeFirestore()
    store.docs[("users", "user-1")] = {
        "email": "parent@example.com",
        "childList": [{"cid": cid, "nickname": nickname} for cid, _, nickname in children],
    }
    for cid, name, _ in children:
        store.docs[("childs", cid)] = {"childsName": name}
    return store


def make_api(store: FakeFirestore, tz: str = "America/New_York") -> EventTimeAPI:
    api = EventTimeAPI("parent@example.com", "not-a-real-password", tz, None)
    api.id_token = "test-token"
    api.refresh_token = "test-refresh"
    api.user_uid = "user-1"
    api.token_expires_at = time.time() + 10**9

    async def client():
        return store

    api._get_firestore_client = client  # the library awaits this for every read and write
    return api


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class FakeMailbox:
    """Stands in for millie_bean_counter.mailbox.Mailbox."""

    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.fetched: list[str] = []
        self.since_days: list = []

    def __call__(self, settings):  # used as mailbox_factory
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def recent(self, today) -> list[MailHeader]:
        headers = [_header_from_bytes(str(i + 1), raw, "INBOX") for i, raw in enumerate(self.messages)]
        return [h for h in headers if "daily sheet" in h.subject.lower()]

    def since(self, day) -> list[MailHeader]:
        self.since_days.append(day)
        return [_header_from_bytes(str(i + 1), raw, "INBOX") for i, raw in enumerate(self.messages)]

    def fetch(self, uid: str) -> bytes:
        self.fetched.append(uid)
        return self.messages[int(uid) - 1]


class Outbox:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, settings, subject, body):
        self.sent.append((subject, body))


def settings(tmp: Path, **overrides):
    values = {
        "GMAIL_ADDRESS": "daycare-inbox@example.com",
        "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
        "HUCKLEBERRY_EMAIL": "parent@example.com",
        "HUCKLEBERRY_PASSWORD": "not-a-real-password",
        "HUCKLEBERRY_CHILD": "Josie",
        "DAYCARE_CHILD": "Josephine",
        "ALLOWED_SENDERS": "parent@example.com, kaymbu.com",
        "NOTIFY_EMAIL": "parent@example.com",
        "TIMEZONE": "America/New_York",
    }
    values.update(overrides)
    return from_values(values, data_dir=tmp)


def edit_email(raw: bytes, old: str, new: str) -> bytes:
    """Replace text in every text part of an email (decoding and re-encoding properly)."""
    import email
    import email.policy

    message = email.message_from_bytes(raw, policy=email.policy.default)
    for part in message.walk():
        if part.get_content_maintype() == "text":
            content = part.get_content()
            assert old in content, f"{old!r} not found in {part.get_content_type()}"
            part.set_content(content.replace(old, new), subtype=part.get_content_subtype(), cte="quoted-printable")
    return message.as_bytes()


def with_message_id(raw: bytes, message_id: str) -> bytes:
    import email
    import email.policy

    message = email.message_from_bytes(raw, policy=email.policy.default)
    del message["Message-ID"]
    message["Message-ID"] = message_id
    return message.as_bytes()


def at(text: str, tz: str = "America/New_York") -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz))


# ---------------------------------------------------------------------------
# Google Drive and the picture server
# ---------------------------------------------------------------------------


class FakeDrive:
    """Stands in for millie_bean_counter.gdrive.Drive: folders and files in memory."""

    def __init__(self, account: str = "daycare-inbox@example.com"):
        self.account_email = account
        self.items: dict[str, dict] = {}  # id -> {name, parent, folder, trashed, ...}
        self.uploads: list[dict] = []
        self.calls: list[str] = []
        self.fail_upload_at: int | None = None  # the Nth upload raises DriveError
        self.lose_reply_at: int | None = None  # the Nth upload succeeds at Google, but the reply is lost
        self.created = 0

    def __call__(self, settings):  # used as drive_factory
        return self

    def _new_id(self) -> str:
        self.created += 1
        return f"id-{self.created}"

    def account(self) -> str:
        return self.account_email

    def find_folder(self, name, parent):
        self.calls.append(f"find_folder {name}")
        for file_id, item in self.items.items():
            if item["folder"] and item["name"] == name and item["parent"] == parent and not item["trashed"]:
                return file_id
        return None

    def create_folder(self, name, parent):
        self.calls.append(f"create_folder {name}")
        file_id = self._new_id()
        self.items[file_id] = {"name": name, "parent": parent, "folder": True, "trashed": False}
        return file_id

    def usable(self, file_id):
        self.calls.append(f"usable {file_id}")
        item = self.items.get(file_id)
        return bool(item) and not item["trashed"]

    def find_tagged(self, tag):
        self.calls.append("find_tagged")
        for file_id, item in self.items.items():
            if item.get("tag") == tag and not item["trashed"]:
                return file_id
        return None

    def upload(self, *, name, data, mime, parent, description="", modified=None, tag=""):
        from millie_bean_counter.gdrive import DriveError

        number = len(self.uploads) + 1
        if self.fail_upload_at == number:
            self.fail_upload_at = None
            raise DriveError("Google Drive said HTTP 503: try later", 503)
        file_id = self._new_id()
        item = {
            "name": name,
            "parent": parent,
            "folder": False,
            "trashed": False,
            "data": data,
            "mime": mime,
            "description": description,
            "modified": modified,
            "tag": tag,
        }
        self.items[file_id] = item
        self.uploads.append(item)
        if self.lose_reply_at == number:
            self.lose_reply_at = None
            raise DriveError("Couldn't reach Google Drive (ReadTimeout: timed out).")
        return file_id

    def path_of(self, file_id: str) -> str:
        parts = []
        while file_id in self.items:
            parts.append(self.items[file_id]["name"])
            file_id = self.items[file_id]["parent"]
        return " > ".join(reversed(parts))

    def files(self) -> list[str]:
        return [self.path_of(i) for i, item in self.items.items() if not item["folder"] and not item["trashed"]]


class FakePictures:
    """Stands in for photos.Downloader: url -> (bytes, type), or an exception to raise."""

    def __init__(self, pictures: dict | None = None):
        self.pictures = dict(pictures or {})
        self.asked: list[str] = []

    def __call__(self, url: str):
        from millie_bean_counter.photos import PhotoGone

        self.asked.append(url)
        found = self.pictures.get(url)
        if found is None:
            raise PhotoGone("HTTP 404")
        if isinstance(found, Exception):
            raise found
        return found


def jpeg(seed: str, size: int = 4000) -> bytes:
    """Bytes that are different for every seed (the code never decodes them)."""
    body = (seed.encode() * (size // max(1, len(seed)) + 1))[:size]
    return b"\xff\xd8\xff\xe0" + body


MOMENT_URL = "https://media.example.invalid/media/school/2026/moments/0/0/0/1/moment-0001_display.jpg"
DOWNLOAD_LINK = "https://example.invalid/kaymbu-link/2"


def moments_email(
    day: str = "2026-10-14", moments: int = 1, *, message_id: str = "", sent: str = "", first: int = 1
) -> bytes:
    """The made-up Shared Moments email, on another day and/or with more moments.
    Moment n has the picture moment-000n_display.jpg and the Download link kaymbu-link/download-n."""
    import email
    import email.policy
    import re
    from datetime import date as _date

    raw = fixture("moments-oct-14.eml")
    message = email.message_from_bytes(raw, policy=email.policy.default)
    html = message.get_body(("html",)).get_content()
    when = _date.fromisoformat(day)
    months = [
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
    ]
    html = html.replace("October 14, 2026", f"{months[when.month - 1]} {when.day}, {when.year}")
    rows = re.search(r'<tbody class="moments">.*?</tbody><tbody class="download-panel">.*?</tbody>', html, re.DOTALL)[0]
    blocks = []
    for n in range(first, first + moments):
        block = rows.replace("moment-0001_display.jpg", f"moment-{n:04d}_display.jpg")
        block = block.replace(
            'href="https://example.invalid/kaymbu-link/2"', f'href="https://example.invalid/kaymbu-link/download-{n}"'
        )
        blocks.append(block)
    html = html.replace(rows, "".join(blocks))
    message.set_content(html, subtype="html", cte="quoted-printable")
    del message["Message-ID"]
    message["Message-ID"] = message_id or f"<moments-{day}-{first}@example.com>"
    del message["Date"]
    message["Date"] = sent or f"{when:%a}, {when.day} {when:%b %Y} 19:22:08 +0000"
    return message.as_bytes()


def moment_url(n: int) -> str:
    return MOMENT_URL.replace("moment-0001", f"moment-{n:04d}")


def download_link(n: int) -> str:
    return f"https://example.invalid/kaymbu-link/download-{n}"


def gmail_forward(raw: bytes, *, forwarded_on: str = "Thu, 29 Oct 2026 21:05:00 -0400") -> bytes:
    """What Gmail sends when the parent forwards an email by hand: classes renamed,
    the original wrapped under a 'Forwarded message' block."""
    import email
    import email.policy
    from email.message import EmailMessage

    original = email.message_from_bytes(raw, policy=email.policy.default)
    html = original.get_body(("html",)).get_content().replace('class="', 'class="m_-4433221100')
    sent = original["Date"].datetime.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    clock = sent.strftime("%I:%M %p").lstrip("0")
    header_lines = [
        "---------- Forwarded message ---------",
        f"From: {original['From']}",
        f"Date: {sent:%a}, {sent:%b} {sent.day}, {sent.year} at {clock}",
        f"Subject: {original['Subject']}",
        "To: <parent@example.com>",
    ]
    forward = EmailMessage()
    forward["From"] = "Parent Name <parent@example.com>"
    forward["To"] = "daycare-inbox@example.com"
    forward["Subject"] = "Fwd: " + str(original["Subject"])
    forward["Date"] = forwarded_on
    forward["Message-ID"] = "<forward-of-" + str(original["Message-ID"]).strip("<>") + ">"
    forward.set_content("\n".join(header_lines) + "\n\n(see the HTML version)\n")
    forward.add_alternative(
        '<div dir="ltr"><div class="gmail_quote">' + "<br>".join(header_lines) + "<br><br>" + html + "</div></div>",
        subtype="html",
    )
    return forward.as_bytes()


def forward_as_attachments(*raws: bytes, message_id: str = "<bulk-forward@example.com>") -> bytes:
    """Gmail's 'Forward as attachment' with several emails selected."""
    import email
    import email.policy
    from email.message import EmailMessage

    wrapper = EmailMessage()
    wrapper["From"] = "Parent Name <parent@example.com>"
    wrapper["To"] = "daycare-inbox@example.com"
    wrapper["Subject"] = "Fwd: older daycare photos"
    wrapper["Date"] = "Fri, 30 Oct 2026 08:00:00 -0400"
    wrapper["Message-ID"] = message_id
    wrapper.set_content("Older ones.")
    for raw in raws:
        wrapper.add_attachment(email.message_from_bytes(raw, policy=email.policy.default))
    return wrapper.as_bytes()
