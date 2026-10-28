"""Read everything Huckleberry has for one child, as plain records.

Covers every tracker the app has: diapers (and potty), sleep, bottles,
breastfeeding, solids, pumping, medicine, growth, temperature and activities.
Each raw Firestore entry is read leniently (only the fields we use), so an odd
old entry can't stop an export; anything unrecognisable is counted, not lost
silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Where each tracker keeps its history: (collection, subcollection).
SOURCES = [
    ("sleep", "intervals"),
    ("feed", "intervals"),
    ("diaper", "intervals"),
    ("pump", "intervals"),
    ("health", "data"),
    ("activities", "intervals"),
]
ENTRY_TYPES = (
    "diaper",
    "potty",
    "sleep",
    "bottle",
    "breastfeeding",
    "solids",
    "pumping",
    "medicine",
    "growth",
    "temperature",
    "activity",
)
SIZES = {0: "small", 50: "medium", 100: "large"}
LATEST_TIMESTAMP = 4_102_444_800  # 2100-01-01
POTTY_RESULTS = {"satButDry": "sat but dry", "wentPotty": "went potty", "accident": "accident"}


@dataclass
class Entry:
    hb_id: str  # "<collection>/<entry id>", stable across exports
    type: str  # one of ENTRY_TYPES
    start_ts: float  # unix seconds
    tz: timezone  # the time zone the entry was recorded in
    seconds: float | None = None  # duration, for things with an end
    fields: dict = field(default_factory=dict)
    notes: str = ""
    key: str = ""  # audit key, filled in by keys.assign
    source: str = "app"  # "agent" (from a daycare sheet) or "app" (entered in Huckleberry)

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts, self.tz)

    @property
    def end(self) -> datetime | None:
        if not self.seconds:
            return None
        return datetime.fromtimestamp(self.start_ts + self.seconds, self.tz)


@dataclass
class History:
    entries: list[Entry]
    skipped: dict[str, int]  # unrecognisable entries per collection


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def size_word(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return SIZES.get(int(number), "small" if number < 25 else "medium" if number < 75 else "large")


def words(flag: str) -> str:
    """'longTimeToFallAsleep' -> 'long time to fall asleep' (snake keys are kept as they are)."""
    return re.sub(r"(?<=[a-z])([A-Z])", lambda m: " " + m.group(1).lower(), flag)


def true_flags(value) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(k for k, v in value.items() if v is True)


def _zone(raw: dict, default_tz) -> timezone:
    offset = _num(raw.get("offset"))
    if offset is None or abs(offset) >= 24 * 60:  # missing or nonsense: use the configured zone
        return default_tz
    return timezone(timedelta(minutes=-offset))


def normalize(collection: str, entry_id: str, raw: dict, default_tz) -> Entry | None:
    """One raw history entry -> Entry, or None if it isn't something we recognise."""
    start = _num(raw.get("start"))
    if start is None:
        return None
    if start > 1e11:  # milliseconds
        start /= 1000
    if not 0 < start < LATEST_TIMESTAMP:  # a corrupt time would break every date below
        return None
    base = {
        "hb_id": f"{collection}/{entry_id}",
        "start_ts": start,
        "tz": _zone(raw, default_tz),
        "notes": raw.get("notes") if isinstance(raw.get("notes"), str) else "",
    }
    mode = raw.get("mode")
    duration = _num(raw.get("duration"))

    if collection == "sleep":
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        notes = base.pop("notes") or (details.get("notes") if isinstance(details.get("notes"), str) else "")
        return Entry(
            type="sleep",
            seconds=duration,
            notes=notes,
            fields={
                "fell_asleep": true_flags(details.get("startSleepCondition")),
                "location": true_flags(details.get("sleepLocations")),
                "woke_up": true_flags(details.get("endSleepCondition")),
            },
            **base,
        )
    if collection == "feed":
        if mode == "breast":
            left, right = _num(raw.get("leftDuration")) or 0.0, _num(raw.get("rightDuration")) or 0.0
            return Entry(
                type="breastfeeding",
                seconds=left + right,
                fields={"left_seconds": left, "right_seconds": right, "last_side": _text(raw.get("lastSide"))},
                **base,
            )
        if mode == "bottle":
            return Entry(
                type="bottle",
                fields={
                    "amount": _num(raw.get("amount")),
                    "units": _text(raw.get("units")),
                    "bottle_type": _text(raw.get("bottleType")),
                },
                **base,
            )
        if mode == "solids":
            foods = raw.get("foods") if isinstance(raw.get("foods"), dict) else {}
            names = [_text(food.get("created_name")) for food in foods.values() if isinstance(food, dict)]
            return Entry(
                type="solids",
                fields={
                    "foods": [n for n in names if n],
                    "reactions": [r.lower() for r in true_flags(raw.get("reactions"))],
                },
                **base,
            )
        return None
    if collection == "diaper":
        quantity = raw.get("quantity") if isinstance(raw.get("quantity"), dict) else {}
        common = {
            "kind": _text(mode),
            "pee_size": size_word(quantity.get("pee")),
            "poo_size": size_word(quantity.get("poo")),
            "quantity_order": [k for k in quantity if k in ("pee", "poo")],
            "color": _text(raw.get("color")),
            "consistency": _text(raw.get("consistency")),
        }
        if raw.get("isPotty"):
            result = _text(raw.get("howItHappened"))
            return Entry(type="potty", fields={**common, "result": POTTY_RESULTS.get(result, result)}, **base)
        if mode not in ("pee", "poo", "both", "dry"):
            return None
        return Entry(type="diaper", fields={**common, "diaper_rash": bool(raw.get("diaperRash"))}, **base)
    if collection == "pump":
        left, right = _num(raw.get("leftAmount")), _num(raw.get("rightAmount"))
        total = (left or 0.0) + (right or 0.0) if left is not None or right is not None else None
        return Entry(
            type="pumping",
            seconds=duration,
            fields={
                "left_amount": left,
                "right_amount": right,
                "total_amount": total,
                "units": _text(raw.get("units")),
                "entry_mode": _text(raw.get("entryMode")),
            },
            **base,
        )
    if collection == "health":
        if mode == "medication":
            return Entry(
                type="medicine",
                fields={
                    "name": _text(raw.get("medication_name")),
                    "amount": _num(raw.get("amount")),
                    "units": _text(raw.get("units")),
                },
                **base,
            )
        if mode == "temperature":
            return Entry(
                type="temperature",
                fields={"amount": _num(raw.get("amount")), "units": _text(raw.get("units"))},
                **base,
            )
        if mode == "growth":
            return Entry(
                type="growth",
                fields={
                    "weight": _num(raw.get("weight")),
                    "weight_units": _text(raw.get("weightUnits")),
                    "height": _num(raw.get("height")),
                    "height_units": _text(raw.get("heightUnits")),
                    "head": _num(raw.get("head")),
                    "head_units": _text(raw.get("headUnits")),
                },
                **base,
            )
        return None
    if collection == "activities":
        if not isinstance(mode, str) or not mode:
            return None
        return Entry(type="activity", seconds=duration, fields={"activity": mode}, **base)
    return None


async def read_all(db, child_uid: str, default_tz) -> History:
    """Every history entry for the child, oldest first."""
    entries: dict[str, Entry] = {}
    skipped: dict[str, int] = {}
    for collection, subcollection in SOURCES:
        ref = db.collection(collection).document(child_uid).collection(subcollection)
        async for doc in ref.stream(timeout=180):
            data = doc.to_dict() or {}
            if data.get("multi"):
                # Batched documents hold many entries, keyed by each entry's own id.
                batch = data.get("data") if isinstance(data.get("data"), dict) else {}
                items = [(str(entry_id), raw) for entry_id, raw in batch.items()]
            else:
                items = [(str(doc.id), data)]
            for entry_id, raw in items:
                entry = normalize(collection, entry_id, raw, default_tz) if isinstance(raw, dict) else None
                if entry is None:
                    skipped[collection] = skipped.get(collection, 0) + 1
                else:
                    entries[entry.hb_id] = entry
    return History(sorted(entries.values(), key=lambda e: (e.start_ts, e.type, e.hb_id)), skipped)
