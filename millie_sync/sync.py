"""One pass: read new daily sheets from Gmail and log them in Huckleberry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .huckleberry import Existing, Huckleberry, differences, event_key, pair_up
from .journal import Journal, exclusive
from .mailbox import Mailbox, MailError, MailHeader, send_email
from .sheet import Event, Sheet, amount_text, clock_text, format_minutes, parse_email


@dataclass
class SheetOutcome:
    sheet: Sheet
    added: list[Event] = field(default_factory=list)
    would_add: list[Event] = field(default_factory=list)
    already: list[tuple[Event, str]] = field(default_factory=list)
    keys: dict[int, str] = field(default_factory=dict)  # id(event) -> audit key, for added events

    def key_of(self, event: Event) -> str:
        return self.keys.get(id(event), "")


@dataclass
class MessageOutcome:
    header: MailHeader
    status: str = "pending"  # done | ignored | error | pending
    reason: str = ""
    sheets: list[SheetOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    email_error: str = ""


@dataclass
class RunReport:
    dry_run: bool
    lookback_days: int = 7
    seen: int = 0  # daily-sheet emails in the lookback window
    handled_before: int = 0
    messages: list[MessageOutcome] = field(default_factory=list)
    child_name: str = ""
    error: str = ""

    @property
    def added(self) -> int:
        return sum(len(s.added) for m in self.messages for s in m.sheets)


def run_once(
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    mailbox_factory: Callable | None = None,
    huckleberry_factory: Callable | None = None,
    notifier: Callable | None = None,
) -> RunReport:
    mailbox_factory = mailbox_factory or Mailbox
    huckleberry_factory = huckleberry_factory or Huckleberry
    notifier = notifier or send_email
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(settings.data_dir / "journal.sqlite3")
    try:
        if dry_run:
            return _run(settings, journal, True, now, mailbox_factory, huckleberry_factory, notifier)
        with exclusive(settings.data_dir / "sync.lock"):
            return _run(settings, journal, False, now, mailbox_factory, huckleberry_factory, notifier)
    finally:
        journal.close()


def _run(settings, journal, dry_run, now, mailbox_factory, huckleberry_factory, notifier) -> RunReport:
    tz = settings.timezone
    now = (now or datetime.now(tz)).astimezone(tz)
    today = now.date()
    report = RunReport(dry_run=dry_run, lookback_days=settings.lookback_days)

    with mailbox_factory(settings) as mailbox:
        headers = mailbox.recent(today)
        report.seen = len(headers)
        todo = []
        for header in headers:
            if journal.message_status(header.key) in ("done", "ignored"):
                report.handled_before += 1
            else:
                todo.append(header)
        to_push: list[MessageOutcome] = []
        for header in todo:
            outcome = MessageOutcome(header)
            report.messages.append(outcome)
            if not settings.sender_allowed(header.sender):
                outcome.status, outcome.reason = "ignored", f"sender {header.sender} isn't in ALLOWED_SENDERS"
                continue
            raw = mailbox.fetch(header.uid)
            try:
                parsed = parse_email(raw, tz)
            except Exception as exc:  # a parser bug must not stop other emails
                outcome.status = "error"
                outcome.reason = f"couldn't read this email ({exc.__class__.__name__}: {exc})"
                continue
            outcome.errors = parsed.errors
            for sheet in parsed.sheets:
                if not settings.daycare_child_matches(sheet.child):
                    outcome.errors.append(f"Skipped {sheet.child}'s sheet (DAYCARE_CHILD is {settings.daycare_child}).")
                elif settings.import_since and sheet.day < settings.import_since:
                    outcome.errors.append(f"Skipped {sheet.title}: it's before IMPORT_SINCE ({settings.import_since}).")
                elif sheet.day > today:
                    outcome.errors.append(f"Skipped {sheet.title}: that date is in the future.")
                else:
                    outcome.sheets.append(SheetOutcome(sheet))
            if outcome.sheets:
                to_push.append(outcome)
            else:
                outcome.status = "ignored"
                outcome.reason = "; ".join(outcome.errors) or "no daily sheet found in this email"

    if to_push:
        try:
            asyncio.run(_push(huckleberry_factory(settings), journal, to_push, report, dry_run, now))
        except Exception as exc:  # recorded per message below; the service retries later
            report.error = str(exc) or exc.__class__.__name__

    for outcome in report.messages:
        if outcome.status == "pending":
            outcome.status = "error"
            outcome.reason = report.error or "interrupted"
        if dry_run:
            continue
        journal.mark_message(
            outcome.header.key,
            subject=outcome.header.subject,
            sender=outcome.header.sender,
            received=outcome.header.received.isoformat() if outcome.header.received else None,
            status=outcome.status,
            detail=outcome.reason,
            sheet_days=[s.sheet.day.isoformat() for s in outcome.sheets],
        )
        if outcome.status == "done" and settings.notify and _worth_emailing(outcome):
            subject, body = summary_email(outcome, report.child_name)
            try:
                notifier(settings, subject, body)
            except MailError as exc:
                outcome.email_error = str(exc)
    return report


async def _push(huckleberry: Huckleberry, journal, outcomes, report, dry_run, now) -> None:
    async with huckleberry as hb:
        report.child_name = hb.child_name
        for outcome in sorted(outcomes, key=lambda o: min(s.sheet.day for s in o.sheets)):
            for item in outcome.sheets:
                await _push_sheet(hb, journal, outcome, item, dry_run, now)
            outcome.status = "done"


async def _push_sheet(hb: Huckleberry, journal: Journal, outcome: MessageOutcome, item: SheetOutcome, dry_run, now):
    sheet = item.sheet
    existing: dict[str, list[Existing]] = await hb.existing(sheet.day)
    events = list(sheet.events)
    # Match every event (even ones handled before, so they keep "their" entries)
    # against what Huckleberry already has, one to one.
    matches = pair_up(events, [row for rows in existing.values() for row in rows])
    for index, event in enumerate(events):
        key = event_key(hb.child_uid, event)
        if event.start > now:
            sheet.problems.append(f"{_summary(event)} is in the future, so it was skipped.")
            continue
        earlier = journal.event_summary(key)
        if earlier is not None:
            item.already.append((event, "handled on an earlier run"))
            if earlier != _summary(event):
                sheet.problems.append(
                    f'Daycare changed "{earlier}" to "{_summary(event)}". Huckleberry was left as it was.'
                )
            continue
        match = matches.get(index)
        if match:
            when = _clock(match.start, hb)
            item.already.append((event, f"Huckleberry already has a {event.kind} at {when} ({match.detail})"))
            different = differences(event, match, hb.settings.timezone)
            if different:
                sheet.problems.append(f"{clock_text(event.start)} {event.kind}: {different}. Left as it was.")
            if not dry_run:
                _record(journal, hb, key, event, "already_in_app", sheet, outcome)
            continue
        if dry_run:
            item.would_add.append(event)
            continue
        await hb.add(event)
        item.keys[id(event)] = _record(journal, hb, key, event, "added", sheet, outcome)
        item.added.append(event)


def _summary(event: Event) -> str:
    return " ".join(f"{clock_text(event.start)} {event.describe()}".split())


def event_details(event: Event) -> dict:
    """The structured facts of a sheet event, kept for the agent's audit log."""
    details: dict = {"comment": event.comment or ""}
    if event.kind == "diaper":
        details["diaper"] = event.diaper
    elif event.kind == "bottle":
        details.update(amount=event.amount, units=event.units, offered=event.offered, milk=event.milk)
    elif event.kind == "nap":
        details.update(end=event.end.isoformat() if event.end else None, minutes=event.minutes)
    return details


def _record(journal, hb, key, event, status, sheet, outcome) -> str | None:
    return journal.record_event(
        key,
        child_uid=hb.child_uid,
        kind=event.kind,
        start=event.start,
        summary=_summary(event),
        status=status,
        sheet_day=sheet.day.isoformat(),
        message_key=outcome.header.key,
        details=event_details(event),
    )


def _clock(timestamp: float, hb: Huckleberry) -> str:
    return clock_text(datetime.fromtimestamp(timestamp, hb.settings.timezone))


def _worth_emailing(outcome: MessageOutcome) -> bool:
    return any(s.added or s.sheet.problems for s in outcome.sheets)


# ---------------------------------------------------------------------------
# Words for people
# ---------------------------------------------------------------------------


def totals(sheet: Sheet) -> list[str]:
    bottles = [e for e in sheet.events if e.kind == "bottle"]
    naps = [e for e in sheet.events if e.kind == "nap"]
    diapers = [e for e in sheet.events if e.kind == "diaper"]
    lines = []
    if bottles:
        by_unit: dict[str, float] = {}
        for e in bottles:
            by_unit[e.units] = by_unit.get(e.units, 0) + (e.amount or 0)
        amounts = " + ".join(f"{amount_text(round(v, 2))} {u}" for u, v in by_unit.items())
        lines.append(f"Bottles: {len(bottles)}, {amounts} total")
    if naps:
        lines.append(f"Naps: {len(naps)}, {format_minutes(sum(e.minutes or 0 for e in naps))} total")
    if diapers:
        counts: dict[str, int] = {}
        for e in diapers:
            word = {"pee": "wet", "poo": "BM", "both": "wet + BM", "dry": "dry"}[e.diaper]
            counts[word] = counts.get(word, 0) + 1
        lines.append(f"Diapers: {len(diapers)} ({', '.join(f'{n} {w}' for w, n in counts.items())})")
    return lines


def _event_line(event: Event) -> str:
    return f"  {clock_text(event.start):>8}  {event.describe()}"


def describe_sheet(item: SheetOutcome, dry_run: bool) -> list[str]:
    sheet = item.sheet
    lines = [sheet.title]
    if dry_run:
        if item.would_add:
            lines.append(" Would add:")
            lines += [_event_line(e) for e in item.would_add]
    elif item.added:
        lines.append(" Added to Huckleberry (audit key in brackets):")
        lines += [f"{_event_line(e):<58} [{item.key_of(e)}]" if item.key_of(e) else _event_line(e) for e in item.added]
    if item.already:
        lines.append(" Left alone:")
        lines += [f"{_event_line(e)}  ({why})" for e, why in item.already]
    if sheet.problems:
        lines.append(" Needs a look (not added):")
        lines += [f"  - {p}" for p in sheet.problems]
    comments = [f"{clock_text(e.start)} {e.kind}: {e.comment}" for e in sheet.events if e.comment] + sheet.comments
    if comments:
        lines.append(" Teacher comments:")
        lines += [f"  - {c}" for c in comments]
    if sheet.notes:
        lines += [f"  Note: {n}" for n in sheet.notes]
    day_totals = totals(sheet)
    if day_totals:
        lines.append(" Day totals from the sheet:")
        lines += [f"  {line}" for line in day_totals]
    return lines


def describe_report(report: RunReport) -> str:
    lines: list[str] = []
    if report.error:
        lines.append(f"Problem: {report.error}")
    if not report.messages:
        if report.seen:
            lines.append(
                f"No new daily sheets. The {report.seen} from the last {report.lookback_days} days were handled before."
            )
        else:
            lines.append(f"No daily-sheet emails in the last {report.lookback_days} days.")
        return "\n".join(lines)
    if report.child_name:
        lines.append(f"Huckleberry child: {report.child_name}")
    for outcome in report.messages:
        lines.append("")
        lines.append(f'Email: "{outcome.header.subject}" from {outcome.header.sender}')
        if outcome.status == "ignored":
            lines.append(f" Skipped: {outcome.reason}")
            continue
        if outcome.status == "error":
            lines.append(f" Not finished, will retry: {outcome.reason}")
        for error in outcome.errors:
            lines.append(f" {error}")
        for item in outcome.sheets:
            lines += describe_sheet(item, report.dry_run)
        if outcome.email_error:
            lines.append(f" Summary email not sent: {outcome.email_error}")
    if report.dry_run:
        lines.append("")
        lines.append("Preview only: nothing was written to Huckleberry.")
    return "\n".join(lines)


def summary_email(outcome: MessageOutcome, child_name: str) -> tuple[str, str]:
    added = sum(len(s.added) for s in outcome.sheets)
    problems = sum(len(s.sheet.problems) for s in outcome.sheets)
    first = min(outcome.sheets, key=lambda s: s.sheet.day).sheet
    day = f"{first.day.strftime('%a %b')} {first.day.day}"
    who = child_name or first.child
    subject = f"{who} {day}: {added} added to Huckleberry"
    if problems:
        subject += f", {problems} need{'s' if problems == 1 else ''} a look"
    body = [f'From "{outcome.header.subject}".', ""]
    for item in outcome.sheets:
        body += describe_sheet(item, dry_run=False)
        body.append("")
    body.append("Sent by the Millie daycare sync running in Docker on your Mac.")
    return subject, "\n".join(body)


def error_email(error: str) -> tuple[str, str]:
    body = (
        "The Millie daycare sync hit a problem and will keep retrying every few minutes:\n\n"
        f"  {error}\n\n"
        "Double-click Status.command on your Mac to see more. You'll get at most one of these a day per problem."
    )
    return "Millie sync needs attention", body
