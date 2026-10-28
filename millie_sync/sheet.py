"""Read Goddard/Kaymbu "Daily Sheet" emails and turn them into events.

The parser is strict about the three things it imports (bottles, diapers and
naps) and never guesses. A line it doesn't fully understand is reported back
as a problem instead of being logged, so nothing wrong lands in Huckleberry.

Times on the sheet are local to the daycare. The date comes from the sheet
itself ("Monday, October 12th, 2026"), never from when the email was sent or
forwarded.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

NOTE_TAG = "From daycare"

# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

_SPACES = re.compile(r"[\s\u00a0\u2000-\u200b\u202f\u205f\u3000\ufeff]+")


def _clean(line: str) -> str:
    return _SPACES.sub(" ", line).strip()


_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "center",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_SKIP_TAGS = {"head", "noscript", "script", "style", "template", "title"}


class _TextExtractor(HTMLParser):
    """HTML to lines: block elements end a line, all other whitespace collapses."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(_SPACES.sub(" ", data))


def html_to_lines(html: str) -> list[str]:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return [line for line in (_clean(x) for x in "".join(extractor.parts).split("\n")) if line]


def text_to_lines(text: str) -> list[str]:
    return [line for line in (_clean(x) for x in text.splitlines()) if line]


def email_bodies(raw: bytes) -> list[tuple[str, list[str]]]:
    """Every readable body in the email, HTML first, as (kind, lines)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace")
        (html_parts if content_type == "text/html" else text_parts).append(content)
    return [("html", html_to_lines(h)) for h in html_parts] + [("text", text_to_lines(t)) for t in text_parts]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

TIME = r"(?:1[0-2]|0?[1-9]):[0-5]\d\s*[ap]\.?\s*m\.?"
_NUM = r"\d+(?:\.\d+)?|\.\d+"
_UNIT = r"ounces?|fl\.?\s*oz\.?|oz\.?|milliliters?|millilitres?|mls?\.?"
_MILK = (
    r"expressed\s+breast\s*milk|breast\s*milk|formula|whole\s+milk|cow['’]?s?\s+milk"
    r"|goat['’]?s?\s+milk|soy\s+milk"
)

TITLE_RE = re.compile(r"^(?P<name>.{1,60}?)\s*['’`]s\s+daily\s+sheet$", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}
DATE_RE = re.compile(
    r"^(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+"
    r"(?P<month>[a-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})$",
    re.IGNORECASE,
)
BOTTLE_RE = re.compile(
    rf"^(?:offered\s+(?P<offered>{_NUM})\s*(?P<offered_unit>{_UNIT})\s+(?:of\s+)?(?P<milk>{_MILK})\s+and\s+)?"
    rf"(?:took|drank|consumed|had)\s+(?P<taken>{_NUM})\s*(?P<taken_unit>{_UNIT})"
    rf"(?:\s+(?:of\s+)?(?P<milk2>{_MILK}))?\s+at\s+(?P<time>{TIME})\.?$",
    re.IGNORECASE,
)
_DIAPER_TYPES = (
    r"bm\s*/\s*wet|wet\s*/\s*bm|wet\s*(?:and|&|\+)\s*bm|bm\s*(?:and|&|\+)\s*wet"
    r"|wet|bm|dry|dirty|soiled"
)
DIAPER_RE = re.compile(rf"^(?P<type>{_DIAPER_TYPES})\s+diaper(?:\s+change)?\s+at\s+(?P<time>{TIME})\.?$", re.IGNORECASE)
NAP_RE = re.compile(
    rf"^(?:napped|slept)\s+from\s+(?P<start>{TIME})\s+(?:to|until|till|-|–|—)\s+(?P<end>{TIME})"
    rf"(?:\s*\((?P<duration>[^)]*)\))?\.?$",
    re.IGNORECASE,
)
ATTENDANCE_RE = re.compile(rf"^checked\s+(?:in|out)\s+at\s+{TIME}(?:\s+by\s+.*)?$", re.IGNORECASE)
_HAS_TIME = re.compile(TIME, re.IGNORECASE)
_EVENT_WORDS = re.compile(
    r"\b(?:bottles?|ounces?|oz|ml|breast\s*milk|breastmilk|formula|offered|took|drank|diapers?|wet|bm"
    r"|napp?ed|naps?|slept|sleep|asleep|woke)\b",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r"^<?(?:https?://|mailto:)", re.IGNORECASE)
_END_RE = re.compile(
    r"^(?:--|-- .*|-{5,}\s*forwarded message.*|begin forwarded message:?|sent from my .*"
    r"|.*\bunsubscribe\b.*|.*\byou are receiving this\b.*|.*\bpowered by kaymbu\b.*)$",
    re.IGNORECASE,
)
_HEADER_LIKE = re.compile(r"^(?:[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*){0,2}|[A-Z][A-Z &/-]{1,30})$")

SECTIONS = {
    "bottle": "bottle",
    "bottles": "bottle",
    "bottle feeding": "bottle",
    "bottle feedings": "bottle",
    "diaper": "diaper",
    "diapers": "diaper",
    "diapering": "diaper",
    "diaper change": "diaper",
    "diaper changes": "diaper",
    "nap": "nap",
    "naps": "nap",
    "napping": "nap",
}
# Other sections a daily sheet can have. Their lines are not imported.
SECTIONS.update(
    {
        name: "other"
        for name in (
            "activities",
            "activity",
            "check in",
            "check-in",
            "check out",
            "check-out",
            "meal",
            "meals",
            "food",
            "snack",
            "snacks",
            "breakfast",
            "lunch",
            "dinner",
            "solids",
            "toilet",
            "toileting",
            "potty",
            "bathroom",
            "mood",
            "moods",
            "note",
            "notes",
            "teacher note",
            "teacher notes",
            "comments",
            "daily notes",
            "medication",
            "medications",
            "medicine",
            "incident",
            "incidents",
            "accident",
            "accidents",
            "health",
            "temperature",
            "supplies",
            "supplies needed",
            "needs",
            "learning",
            "milestones",
            "highlights",
            "outdoor play",
            "outdoor",
            "indoor play",
            "play",
            "photos",
            "photo",
            "videos",
            "sunscreen",
            "tummy time",
            "reading",
            "circle time",
        )
    }
)
IMPORTED_SECTIONS = {"bottle", "diaper", "nap"}


def parse_clock(text: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", text.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"Not a clock time: {text!r}")
    hour, minute, half = int(match[1]), int(match[2]), match[3].lower()
    if not 1 <= hour <= 12 or minute > 59:
        raise ValueError(f"Not a clock time: {text!r}")
    return time(hour % 12 + (12 if half == "p" else 0), minute)


def parse_duration_minutes(text: str) -> int | None:
    lowered = text.lower()
    hours = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", lowered)
    minutes = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", lowered)
    if not hours and not minutes:
        return None
    return (int(hours[1]) if hours else 0) * 60 + (int(minutes[1]) if minutes else 0)


def _unit(text: str) -> str:
    return "ml" if text.lower().startswith("m") else "oz"


def _milk_type(text: str | None) -> str | None:
    if not text:
        return None
    letters = re.sub(r"[^a-z]", "", text.lower())
    if "breast" in letters:
        return "Breast Milk"
    if "formula" in letters:
        return "Formula"
    if "goat" in letters:
        return "Goat Milk"
    if "soy" in letters:
        return "Soy Milk"
    if letters in ("wholemilk", "cowmilk", "cowsmilk"):
        return "Cow Milk"
    return None


def _diaper_mode(text: str) -> str:
    letters = text.lower()
    wet = "wet" in letters
    dirty = any(word in letters for word in ("bm", "dirty", "soiled"))
    if wet and dirty:
        return "both"
    if wet:
        return "pee"
    if dirty:
        return "poo"
    return "dry"


def amount_text(value: float) -> str:
    return f"{value:g}"


def clock_text(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

DIAPER_WORDS = {"pee": "wet", "poo": "BM", "both": "wet + BM", "dry": "dry"}


@dataclass(frozen=True)
class Event:
    kind: str  # "diaper" | "bottle" | "nap"
    start: datetime  # timezone-aware, daycare local time
    end: datetime | None = None  # naps
    diaper: str | None = None  # Huckleberry mode: "pee" | "poo" | "both" | "dry"
    amount: float | None = None  # bottle: amount the baby took
    offered: float | None = None  # bottle: amount offered, if the sheet says
    units: str | None = None  # "oz" | "ml"
    milk: str | None = None  # Huckleberry bottle type, e.g. "Breast Milk"
    comment: str | None = None  # teacher comment written under this entry
    source: str = field(default="", compare=False)

    @property
    def minutes(self) -> int | None:
        if self.end is None:
            return None
        return round((self.end - self.start).total_seconds() / 60)

    @property
    def identity(self) -> tuple:
        return (self.kind, self.start, self.end, self.diaper, self.amount, self.units, self.milk)

    def describe(self) -> str:
        if self.kind == "diaper":
            return f"Diaper  {DIAPER_WORDS[self.diaper or 'dry']}"
        if self.kind == "bottle":
            text = f"Bottle  {amount_text(self.amount or 0)} {self.units} {(self.milk or '').lower()}"
            if self.offered is not None:
                text += f" (offered {amount_text(self.offered)} {self.units})"
            return text
        return f"Nap     until {clock_text(self.end)} ({format_minutes(self.minutes or 0)})"

    def huckleberry_note(self) -> str:
        parts = [NOTE_TAG]
        if self.kind == "bottle" and self.offered is not None:
            parts.append(
                f"offered {amount_text(self.offered)} {self.units}, took {amount_text(self.amount or 0)} {self.units}"
            )
        if self.comment:
            parts.append(f"teacher: {self.comment}")
        return " · ".join(parts)


def format_minutes(total: int) -> str:
    hours, minutes = divmod(int(total), 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes} min"


@dataclass
class Sheet:
    child: str
    day: date
    events: list[Event] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # need a human: not imported
    comments: list[str] = field(default_factory=list)  # teacher comments with nowhere to go
    notes: list[str] = field(default_factory=list)  # informational

    @property
    def title(self) -> str:
        return f"{self.child}, {self.day.strftime('%A, %B')} {self.day.day}, {self.day.year}"


@dataclass
class ParseResult:
    sheets: list[Sheet] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # a sheet was found but could not be read at all


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_lines(lines: list[str], tz: ZoneInfo) -> ParseResult:
    result = ParseResult()
    starts = [i for i, line in enumerate(lines) if TITLE_RE.match(line)]
    for n, index in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(lines)
        child = TITLE_RE.match(lines[index])["name"].strip()
        sheet, error = _parse_block(child, lines[index + 1 : stop], tz)
        if error:
            result.errors.append(f"{child}'s Daily Sheet: {error}")
        elif sheet:
            result.sheets.append(sheet)
    return result


def _parse_block(child: str, lines: list[str], tz: ZoneInfo) -> tuple[Sheet | None, str | None]:
    day: date | None = None
    section = "other"
    sheet: Sheet | None = None
    events: list[Event] = []
    previous_event: int | None = None  # index into events of the line just before, if any

    for raw in lines:
        line = _clean(raw)
        if _END_RE.match(line):
            break
        if _LINK_RE.match(line) or line.lower().startswith("[image"):
            continue

        if day is None:
            match = DATE_RE.match(line)
            if not match:
                continue  # anything before the date line is page chrome
            month = _MONTHS.get(match["month"].lower())
            if month is None:
                return None, f"couldn't read the date {line!r}"
            try:
                day = date(int(match["year"]), month, int(match["day"]))
            except ValueError:
                return None, f"couldn't read the date {line!r}"
            if day.strftime("%A").lower() != match["weekday"].lower():
                return None, f"the date {line!r} has the wrong weekday, so nothing was imported"
            sheet = Sheet(child=child, day=day)
            continue

        assert sheet is not None
        header = line.lower().rstrip(":").strip()
        if header in SECTIONS:
            section = SECTIONS[header]
            previous_event = None
            continue

        event, problem, note = _parse_entry(line, day, tz)
        if event:
            events.append(event)
            previous_event = len(events) - 1
            continue
        if problem or note:
            if problem:
                sheet.problems.append(problem)
            if note:
                sheet.notes.append(note)
            previous_event = None
            continue
        if ATTENDANCE_RE.match(line):
            previous_event = None
            continue

        has_time = bool(_HAS_TIME.search(line))
        looks_like_event = bool(_EVENT_WORDS.search(line))
        if has_time:
            # A timed line we couldn't read. Only worth flagging if it's about
            # something we import; meals and activities are not our business.
            if section in IMPORTED_SECTIONS or looks_like_event:
                sheet.problems.append(f'Couldn\'t read "{line}"; add it by hand if it matters.')
            previous_event = None
            continue
        if section in IMPORTED_SECTIONS and re.search(rf"\b(?:{_NUM})\s*(?:{_UNIT})\b", line, re.IGNORECASE):
            sheet.problems.append(f'"{line}" has no time, so it wasn\'t logged.')
            previous_event = None
            continue
        if previous_event is not None and section in IMPORTED_SECTIONS and len(line) <= 200:
            event = events[previous_event]
            comment = f"{event.comment} {line}" if event.comment else line
            events[previous_event] = replace(event, comment=comment)
            continue
        if _HEADER_LIKE.match(line):
            section = "other"  # a section this tool doesn't import (meals, mood, ...)
            previous_event = None
            continue
        if section in IMPORTED_SECTIONS:
            sheet.comments.append(line)

    if sheet is None:
        return None, "no date line was found, so nothing was imported"

    seen: set[tuple] = set()
    for event in sorted(events, key=lambda e: (e.start, e.kind)):
        if event.identity in seen:
            sheet.notes.append(f"Listed twice on the sheet, logged once: {clock_text(event.start)} {event.describe()}")
            continue
        seen.add(event.identity)
        sheet.events.append(event)
    return sheet, None


def _at(day: date, clock: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, parse_clock(clock), tzinfo=tz)


def _parse_entry(line: str, day: date, tz: ZoneInfo) -> tuple[Event | None, str | None, str | None]:
    """(event, problem, note). All three are None when the line isn't an entry.

    A problem means a human needs to look; a note is just information.
    """
    match = BOTTLE_RE.match(line)
    if match:
        start = _at(day, match["time"], tz)
        taken = float(match["taken"])
        units = _unit(match["taken_unit"])
        offered = float(match["offered"]) if match["offered"] else None
        milk = _milk_type(match["milk"] or match["milk2"])
        if match["offered_unit"] and _unit(match["offered_unit"]) != units:
            return None, f'"{line}" mixes units; add it by hand.', None
        if milk is None:
            return None, f'"{line}" doesn\'t say breast milk or formula; add it by hand.', None
        if offered is not None and taken > offered + 1e-9:
            return None, f'"{line}" says she took more than was offered; check it and add it by hand.', None
        if taken > (16 if units == "oz" else 480):
            return None, f'"{line}" is a much bigger bottle than expected; check it and add it by hand.', None
        if taken <= 0:
            return None, None, f'"{line}": she didn\'t drink any, so no bottle was logged.'
        event = Event("bottle", start, amount=taken, offered=offered, units=units, milk=milk, source=line)
        return event, None, None

    match = DIAPER_RE.match(line)
    if match:
        start = _at(day, match["time"], tz)
        return Event("diaper", start, diaper=_diaper_mode(match["type"]), source=line), None, None

    match = NAP_RE.match(line)
    if match:
        start = _at(day, match["start"], tz)
        end = _at(day, match["end"], tz)
        minutes = (end - start).total_seconds() / 60
        if minutes <= 0:
            return None, f'"{line}" ends before it starts; add it by hand.', None
        if minutes > 6 * 60:
            return None, f'"{line}" is longer than 6 hours; check it and add it by hand.', None
        if match["duration"]:
            stated = parse_duration_minutes(match["duration"])
            if stated is not None and abs(stated - minutes) > 1:
                return None, f'"{line}": the times and the stated length disagree; add it by hand.', None
        return Event("nap", start, end=end, source=line), None, None

    return None, None, None


def parse_email(raw: bytes, tz: ZoneInfo) -> ParseResult:
    """Parse the best body of an email (HTML first) and cross-check the others."""
    chosen: ParseResult | None = None
    others: list[ParseResult] = []
    for _kind, lines in email_bodies(raw):
        parsed = parse_lines(lines, tz)
        if chosen is None and parsed.sheets:
            chosen = parsed
        elif parsed.sheets:
            others.append(parsed)
    if chosen is None:
        # No readable sheet in any body. Surface errors from whichever body had a title.
        errors: list[str] = []
        for _kind, lines in email_bodies(raw):
            errors.extend(e for e in parse_lines(lines, tz).errors if e not in errors)
        return ParseResult(errors=errors)

    def key(result: ParseResult) -> set:
        return {(s.child, s.day, e.identity) for s in result.sheets for e in s.events}

    for other in others:
        if key(other) != key(chosen):
            for sheet in chosen.sheets:
                sheet.notes.append(
                    "The email's plain-text and HTML versions didn't match exactly; the HTML version was used."
                )
            break
    return chosen
