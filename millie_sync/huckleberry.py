"""Everything that talks to Huckleberry, through py-huckleberry-api.

Writes use the library's own log_diaper / log_bottle / log_sleep, so the
entries look exactly like ones the library's authors verified against the app.
Reads (which child, what's already logged) go straight to the documents and
only look at a few plain fields, so an unusual old entry in your history can't
stop the sync.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import aiohttp
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from huckleberry_api import HuckleberryAPI
from pydantic import ValidationError

from .config import Settings
from .history import History, read_all
from .sheet import DIAPER_WORDS, Event, amount_text, clock_text

COLLECTIONS = {"diaper": "diaper", "bottle": "feed", "nap": "sleep"}
# Hand-entered copies of daycare events are often a few minutes off (your
# October 12 entries were 0 to 10 minutes away from the sheet's times).
MATCH_WINDOW_SECONDS = 15 * 60
NAP_SAME_SECONDS = 5 * 60  # nap start/end this close counts as the same nap
OZ_TO_ML = 29.5735
BOTTLE_NOTES = "notes" in inspect.signature(HuckleberryAPI.log_bottle).parameters

GOOGLE_SIGN_IN_HELP = (
    "Huckleberry didn't accept that email and password. Use the email and password you "
    "sign in to the Huckleberry app with. If you sign in with Google or Apple instead, "
    "this tool can't use that login (see 'Huckleberry login' in README.md)."
)


class HuckleberryError(Exception):
    """Huckleberry problem. The message is written for a person."""


class EventTimeAPI(HuckleberryAPI):
    """Stores the UTC offset in effect at each entry's own time.

    The library uses the offset at the moment of writing. That only differs when
    catching up across a daylight-saving change, but then it would be an hour off.
    """

    event_time: datetime | None = None

    async def _get_timezone_offset_minutes(self) -> float:
        moment = self.event_time or datetime.now(self._timezone)
        offset = moment.astimezone(self._timezone).utcoffset()
        return -offset.total_seconds() / 60 if offset is not None else 0.0


@dataclass(frozen=True)
class Existing:
    """An entry already in Huckleberry, reduced to the fields used for comparing."""

    kind: str
    start: float  # unix seconds
    duration: float = 0.0  # seconds (naps)
    mode: str = ""  # diapers: pee / poo / both / dry
    amount: float | None = None  # bottles
    units: str = ""
    bottle_type: str = ""

    @property
    def detail(self) -> str:
        if self.kind == "bottle":
            return f"{self.amount:g} {self.units} {self.bottle_type}".strip() if self.amount else "no amount"
        if self.kind == "nap":
            return f"{round(self.duration / 60)} min"
        return DIAPER_WORDS.get(self.mode, self.mode or "diaper")


def to_ml(amount: float | None, units: str | None) -> float | None:
    if amount is None:
        return None
    return amount * OZ_TO_ML if (units or "").lower() == "oz" else amount


def differences(event: Event, row: Existing, tz) -> str | None:
    """How the sheet and a matching Huckleberry entry disagree, or None if they agree.

    A few minutes' difference in time isn't a disagreement; different details are.
    """
    if event.kind == "diaper":
        if row.mode and row.mode != event.diaper:
            return f"Huckleberry has {row.detail}, the sheet says {DIAPER_WORDS[event.diaper]}"
        return None
    if event.kind == "bottle":
        theirs, ours = to_ml(row.amount, row.units), to_ml(event.amount, event.units)
        same = (
            theirs is not None
            and ours is not None
            and abs(theirs - ours) <= max(5.0, 0.03 * ours)
            and (not row.bottle_type or row.bottle_type == event.milk)
        )
        if same:
            return None
        return (
            f"Huckleberry has {row.detail}, the sheet says {amount_text(event.amount or 0)} {event.units} {event.milk}"
        )
    end = event.end.timestamp() if event.end else event.start.timestamp()
    if (
        abs(row.start - event.start.timestamp()) <= NAP_SAME_SECONDS
        and abs(row.start + row.duration - end) <= NAP_SAME_SECONDS
    ):
        return None

    def clock(ts: float) -> str:
        return clock_text(datetime.fromtimestamp(ts, tz))

    return (
        f"Huckleberry has a nap {clock(row.start)} to {clock(row.start + row.duration)}, "
        f"the sheet says {clock_text(event.start)} to {clock_text(event.end)}"
    )


def pair_up(events: list[Event], rows: list[Existing]) -> dict[int, Existing]:
    """Match sheet events to Huckleberry entries, one to one.

    A diaper or bottle within 15 minutes of the sheet's time, or a nap that
    overlaps (or starts within 15 minutes), counts as the same thing. Matches
    with the same details win, then the closest in time. Each Huckleberry entry
    stands in for at most one sheet event, so one hand-entered diaper can't hide
    two daycare diapers. Returns {index into events: matching entry}.
    """
    candidates = []
    for i, event in enumerate(events):
        start = event.start.timestamp()
        end = event.end.timestamp() if event.end else start
        for j, row in enumerate(rows):
            if row.kind != event.kind:
                continue
            gap = abs(row.start - start)
            if event.kind == "nap":
                overlaps = row.start < end and row.start + row.duration > start
                if not overlaps and gap > MATCH_WINDOW_SECONDS:
                    continue
                alike = 0 if overlaps else 1
            else:
                if gap > MATCH_WINDOW_SECONDS:
                    continue
                alike = 1 if differences(event, row, None) else 0
            candidates.append((alike, gap, i, j))
    matched: dict[int, Existing] = {}
    taken: set[int] = set()
    for _alike, _gap, i, j in sorted(candidates):
        if i not in matched and j not in taken:
            matched[i] = rows[j]
            taken.add(j)
    return matched


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _login_message(exc: aiohttp.ClientResponseError) -> str:
    text = f"{exc.message}"
    for code in ("INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_EMAIL"):
        if code in text:
            return GOOGLE_SIGN_IN_HELP
    if "TOO_MANY_ATTEMPTS" in text:
        return "Huckleberry paused logins after too many attempts. Wait an hour and try again."
    if "USER_DISABLED" in text:
        return "Huckleberry says this account is disabled."
    return f"Huckleberry login failed (HTTP {exc.status})."


def event_key(child_uid: str, event: Event) -> str:
    return f"{child_uid}|{event.kind}|{int(event.start.timestamp())}"


class Huckleberry:
    def __init__(self, settings: Settings, *, api: HuckleberryAPI | None = None):
        self.settings = settings
        self.api = api
        self._session: aiohttp.ClientSession | None = None
        self.child_uid = ""
        self.child_name = ""
        self.children: list[tuple[str, list[str]]] = []

    async def __aenter__(self) -> Huckleberry:
        if self.api is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
            self.api = EventTimeAPI(
                self.settings.huckleberry_email,
                self.settings.huckleberry_password,
                self.settings.timezone_name,
                self._session,
            )
            try:
                await self.api.authenticate()
            except aiohttp.ClientResponseError as exc:
                await self.close()
                raise HuckleberryError(_login_message(exc)) from exc
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                await self.close()
                raise HuckleberryError(f"Couldn't reach Huckleberry ({exc.__class__.__name__}).") from exc
        try:
            self.child_uid, self.child_name = await self._pick_child()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is None:
            return  # nothing of ours to close (tests pass in their own client)
        # Shut the Firestore gRPC channel before the event loop ends, so the
        # long-running service doesn't leave a channel behind after each sync.
        client = getattr(self.api, "_firestore_client", None)
        transport = getattr(client, "_transport", None)
        if transport is not None and hasattr(transport, "close"):
            try:
                result = transport.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # closing is best-effort
                pass
        await self._session.close()
        self._session = None

    async def _db(self):
        try:
            return await self.api._get_firestore_client()
        except aiohttp.ClientResponseError as exc:
            raise HuckleberryError(_login_message(exc)) from exc

    async def _pick_child(self) -> tuple[str, str]:
        db = await self._db()
        user = await db.collection("users").document(self.api.user_uid).get()
        data = (user.to_dict() or {}) if user.exists else {}
        children: list[tuple[str, list[str]]] = []
        for ref in data.get("childList") or []:
            if not isinstance(ref, dict) or not ref.get("cid"):
                continue
            cid = str(ref["cid"])
            names: list[str] = []
            child = await db.collection("childs").document(cid).get()
            if child.exists:
                name = (child.to_dict() or {}).get("childsName")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            nickname = ref.get("nickname")
            if isinstance(nickname, str) and nickname.strip() and nickname.strip() not in names:
                names.append(nickname.strip())
            children.append((cid, names or [cid]))
        self.children = children
        if not children:
            raise HuckleberryError("This Huckleberry account has no children set up.")
        wanted = self.settings.huckleberry_child.casefold()
        everyone = ", ".join(names[0] for _, names in children)
        if wanted:
            matches = [(cid, names) for cid, names in children if wanted in {n.casefold() for n in names}]
            if len(matches) == 1:
                return matches[0][0], matches[0][1][0]
            if not matches:
                raise HuckleberryError(
                    f"No child named {self.settings.huckleberry_child!r} in Huckleberry. "
                    f"Children on this account: {everyone}. Fix HUCKLEBERRY_CHILD with Setup.command."
                )
            raise HuckleberryError(f"More than one child is named {self.settings.huckleberry_child!r} in Huckleberry.")
        if len(children) == 1:
            return children[0][0], children[0][1][0]
        raise HuckleberryError(
            f"This Huckleberry account has several children ({everyone}). Set HUCKLEBERRY_CHILD with Setup.command."
        )

    async def read_history(self) -> History:
        """Everything Huckleberry has for the child, for the data export."""
        db = await self._db()
        try:
            return await read_all(db, self.child_uid, self.settings.timezone)
        except Exception as exc:  # gRPC, permissions, network
            raise HuckleberryError(f"Couldn't read Huckleberry's history ({exc.__class__.__name__}: {exc}).") from exc

    async def existing(self, day: date) -> dict[str, list[Existing]]:
        """What Huckleberry already has around this day, for duplicate checks."""
        tz = self.settings.timezone
        window_start = (datetime.combine(day, time.min, tzinfo=tz) - timedelta(hours=12)).timestamp()
        window_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).timestamp()
        db = await self._db()
        found: dict[str, list[Existing]] = {}
        for kind, collection in COLLECTIONS.items():
            try:
                rows = await self._read_intervals(db, collection, window_start, window_end)
            except HuckleberryError:
                raise
            except Exception as exc:  # gRPC, permissions, network
                raise HuckleberryError(
                    f"Couldn't read Huckleberry's {kind} history ({exc.__class__.__name__}: {exc})."
                ) from exc
            found[kind] = [row for row in (self._existing(kind, r) for r in rows) if row is not None]
        return found

    async def _read_intervals(self, db, collection: str, start: float, end: float) -> list[dict]:
        ref = db.collection(collection).document(self.child_uid).collection("intervals")
        rows: list[dict] = []
        # Same two queries py-huckleberry-api's list_*_intervals use, minus its strict parsing.
        query = (
            ref.where(filter=firestore.FieldFilter("start", ">=", start))
            .where(filter=firestore.FieldFilter("start", "<", end))
            .order_by("start")
        )
        async for doc in query.stream(timeout=60):
            data = doc.to_dict() or {}
            if not data.get("multi"):
                rows.append(data)
        # The app also batches older entries into "multi" documents.
        async for doc in ref.where(filter=firestore.FieldFilter("multi", "==", True)).stream(timeout=60):
            entries = (doc.to_dict() or {}).get("data") or {}
            if isinstance(entries, dict):
                for entry in entries.values():
                    value = _number(entry.get("start")) if isinstance(entry, dict) else None
                    if value is not None and start <= value < end:
                        rows.append(entry)
        return rows

    @staticmethod
    def _existing(kind: str, row: dict) -> Existing | None:
        start = _number(row.get("start"))
        if start is None:
            return None

        def text(key: str) -> str:
            value = row.get(key)
            return value if isinstance(value, str) else ""

        if kind == "bottle":
            if row.get("mode") != "bottle":
                return None
            return Existing(
                kind, start, amount=_number(row.get("amount")), units=text("units"), bottle_type=text("bottleType")
            )
        if kind == "nap":
            return Existing(kind, start, duration=_number(row.get("duration")) or 0.0)
        if row.get("isPotty"):
            return None  # potty-training entries share the diaper list; they aren't diapers
        return Existing(kind, start, mode=text("mode"))

    async def add(self, event: Event) -> None:
        api = self.api
        if isinstance(api, EventTimeAPI):
            api.event_time = event.start
        try:
            if event.kind == "diaper":
                await api.log_diaper(
                    self.child_uid, start_time=event.start, mode=event.diaper, notes=event.huckleberry_note()
                )
            elif event.kind == "bottle":
                extra = {"notes": event.huckleberry_note()} if BOTTLE_NOTES else {}
                await api.log_bottle(
                    self.child_uid,
                    start_time=event.start,
                    amount=float(event.amount),
                    bottle_type=event.milk,
                    units=event.units,
                    **extra,
                )
            elif event.kind == "nap":
                await api.log_sleep(self.child_uid, start_time=event.start, end_time=event.end)
            else:
                raise ValueError(f"Unknown event kind {event.kind!r}")
        except ValidationError as exc:
            detail = " ".join(str(exc).splitlines()[:2])
            raise HuckleberryError(
                "py-huckleberry-api couldn't make sense of part of your Huckleberry data, so it stopped "
                f"before writing the {clock_text(event.start)} {event.kind}. A newer py-huckleberry-api "
                f"usually fixes this (see README.md). Details: {detail}"
            ) from exc
        except (GoogleAPICallError, RuntimeError) as exc:
            raise HuckleberryError(
                f"Huckleberry didn't take the {clock_text(event.start)} {event.kind} ({exc}). It will be retried."
            ) from exc
        finally:
            if isinstance(api, EventTimeAPI):
                api.event_time = None
