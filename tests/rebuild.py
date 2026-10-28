"""Rebuild Firestore documents from a Huckleberry CSV export (test helper).

The export only shows whole minutes, so seconds are chosen to reproduce every
minute value in the file (durations, side times and end times are all rounded
down by Huckleberry). Exporting the rebuilt documents must then give back the
original file exactly, which checks the exporter's reading of every field.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

SIZE_VALUES = {"small": 0.0, "medium": 50.0, "large": 100.0}
NEW_YORK = ZoneInfo("America/New_York")


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _camel(label: str) -> str:
    """'long time to fall asleep' -> 'longTimeToFallAsleep'; snake keys stay as they are."""
    parts = label.split(" ")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _flags(text: str) -> dict | None:
    if not text:
        return None
    text = text[:1].lower() + text[1:]
    return {_camel(part.strip()): True for part in text.split(", ")}


def _amount(text: str) -> tuple[float, str]:
    match = re.fullmatch(r"([\d.]+)(ml|oz)", text)
    assert match, text
    return float(match[1]), match[2]


def _length(row: dict) -> tuple[int, int]:
    """(start seconds past the minute, duration in seconds) matching Duration and End."""
    total_min = _minutes(row["Duration"])
    gap = int((datetime.fromisoformat(row["End"]) - datetime.fromisoformat(row["Start"])).total_seconds() // 60)
    assert gap - total_min in (0, 1), row
    return (30, total_min * 60 + 30) if gap > total_min else (0, total_min * 60)


def documents(text: str, tz=NEW_YORK) -> list[tuple[str, str, dict]]:
    """(collection, entry id, raw document) for every row of a Huckleberry CSV."""
    docs = []
    for number, row in enumerate(csv.DictReader(io.StringIO(text))):
        local = datetime.strptime(row["Start"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        base = {"offset": -local.utcoffset().total_seconds() / 60}
        start = local.timestamp()
        notes = row["Notes"]
        kind, where = row["Type"], row["Start Location"]
        entry_id = f"{int(start * 1000)}-{number:020d}"
        if kind == "Feed" and where == "Bottle":
            amount, units = _amount(row["End Condition"])
            doc = {
                "mode": "bottle",
                "start": start,
                "bottleType": row["Start Condition"],
                "amount": amount,
                "units": units,
                **base,
            }
            if notes:
                doc["notes"] = notes
            docs.append(("feed", entry_id, doc))
        elif kind == "Feed" and where == "Breast":
            right = _minutes(row["Start Condition"][:-1]) if row["Start Condition"] else None
            left = _minutes(row["End Condition"][:-1]) if row["End Condition"] else None
            if row["Duration"]:
                total = _minutes(row["Duration"])
                sides = (left or 0) + (right or 0)
                both = left is not None and right is not None
                # Seconds per side: keep each side's minutes, make the sum land on Duration,
                # and leave 20-30 spare seconds so an End one minute later is reachable.
                extra = 40 if both and total > sides else 10 if both else 30
                left_s = left * 60 + extra if left is not None else 0
                right_s = right * 60 + extra if right is not None else 0
                length = left_s + right_s
                assert length // 60 == total, row
                gap = int(
                    (datetime.fromisoformat(row["End"]) - datetime.fromisoformat(row["Start"])).total_seconds() // 60
                )
                lead = max(0, gap * 60 - length)
                assert (lead + length) // 60 == gap and lead < 60, row
            else:
                left_s = right_s = 0
                lead = 0
            doc = {
                "mode": "breast",
                "start": start + lead,
                "lastSide": "left",
                "leftDuration": float(left_s),
                "rightDuration": float(right_s),
                **base,
            }
            if notes:
                doc["notes"] = notes
            docs.append(("feed", entry_id, doc))
        elif kind == "Diaper":
            condition = row["End Condition"]
            mode = condition.split(",")[0].split(":")[0].lower()
            quantity = {k: SIZE_VALUES[v] for k, v in re.findall(r"(pee|poo):(small|medium|large)", condition.lower())}
            doc = {"mode": mode, "start": start, **base}
            if quantity:
                doc["quantity"] = quantity
            if row["Duration"]:
                doc["color"] = row["Duration"]
            if row["Start Condition"]:
                doc["consistency"] = row["Start Condition"].lower()
            if where == "Diaper rash":
                doc["diaperRash"] = True
            if notes:
                doc["notes"] = notes
            docs.append(("diaper", entry_id, doc))
        elif kind == "Sleep":
            lead, length = _length(row)
            details = {}
            for key, column in (
                ("startSleepCondition", "Start Condition"),
                ("sleepLocations", "Start Location"),
                ("endSleepCondition", "End Condition"),
            ):
                flags = _flags(row[column])
                if flags:
                    details[key] = flags
            doc = {"start": int(start + lead), "duration": length, **base}
            if details:
                doc["details"] = details
            docs.append(("sleep", entry_id[:16], doc))
        elif kind == "Pump":
            total, units = _amount(row["Start Condition"])
            doc = {
                "start": start,
                "entryMode": "total",
                "leftAmount": total / 2,
                "rightAmount": total / 2,
                "units": units,
                **base,
            }
            if row["Duration"]:
                lead, length = _length(row)
                doc.update(start=start + lead, duration=float(length))
            if notes:
                doc["notes"] = notes
            docs.append(("pump", entry_id, doc))
        elif kind == "Meds":
            doc = {
                "mode": "medication",
                "start": start,
                "medication_id": "med-1",
                "medication_name": where,
                "amount": 0.0,
                "notes": notes,
                **base,
            }
            docs.append(("health", entry_id, doc))
        else:  # activities, e.g. "Tummy time"
            mode = _camel(kind[:1].lower() + kind[1:])
            doc = {"mode": mode, "start": start, **base}
            if row["Duration"]:
                lead, length = _length(row)
                doc.update(start=start + lead, duration=float(length))
            if notes:
                doc["notes"] = notes
            docs.append(("activities", entry_id, doc))
    return docs


def load_into(store, text: str, child: str = "child-1", tz=NEW_YORK) -> int:
    """Put a Huckleberry CSV's entries into a FakeFirestore. Returns how many."""
    sub = {"health": "data"}
    docs = documents(text, tz)
    for collection, entry_id, doc in docs:
        store.docs[(collection, child, sub.get(collection, "intervals"), entry_id)] = doc
    return len(docs)
