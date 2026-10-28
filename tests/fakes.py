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

from millie_sync.config import from_values
from millie_sync.huckleberry import EventTimeAPI
from millie_sync.mailbox import MailHeader, _header_from_bytes

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


def family(children=(("child-1", "Millie", "Millie"),)) -> FakeFirestore:
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
    """Stands in for millie_sync.mailbox.Mailbox."""

    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.fetched: list[str] = []

    def __call__(self, settings):  # used as mailbox_factory
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def recent(self, today) -> list[MailHeader]:
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
        "HUCKLEBERRY_CHILD": "Millie",
        "DAYCARE_CHILD": "Emilia",
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
