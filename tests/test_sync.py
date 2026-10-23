"""End to end: Gmail -> parser -> the real py-huckleberry-api -> (fake) Firestore."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fakes import FakeMailbox, Outbox, at, edit_email, family, fixture, make_api, settings, with_message_id

from millie_sync.huckleberry import BOTTLE_NOTES, Huckleberry
from millie_sync.journal import Journal
from millie_sync.sync import describe_report, run_once

NOW = at("2026-10-22T22:00:00")  # the evening the (made-up) October 12 sheet was forwarded


def ts(text: str) -> float:
    return at(text).timestamp()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.store = family()
        self.outbox = Outbox()

    def tearDown(self):
        self._tmp.cleanup()

    def run_sync(self, messages, dry_run=False, now=NOW, **overrides):
        return run_once(
            settings(self.data, **overrides),
            dry_run=dry_run,
            now=now,
            mailbox_factory=FakeMailbox(messages),
            huckleberry_factory=lambda s: Huckleberry(s, api=make_api(self.store)),
            notifier=self.outbox,
        )

    def test_daily_sheet_lands_in_huckleberry_exactly(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(report.error, "")
        self.assertEqual(report.added, 9)
        self.assertEqual(report.child_name, "Josie")

        diapers = self.store.intervals("diaper")
        self.assertEqual([d["mode"] for d in diapers], ["pee", "both", "pee", "poo"])
        self.assertEqual(
            [d["start"] for d in diapers],
            [ts("2026-10-12T09:10"), ts("2026-10-12T11:50"), ts("2026-10-12T13:35"), ts("2026-10-12T15:40")],
        )
        self.assertTrue(all(d["offset"] == 240 for d in diapers))  # EDT, minutes behind UTC
        self.assertEqual(diapers[0]["notes"], "From daycare")

        bottles = self.store.intervals("feed")
        self.assertEqual(
            [(b["mode"], b["amount"], b["units"], b["bottleType"]) for b in bottles],
            [("bottle", 4.0, "oz", "Breast Milk"), ("bottle", 3.5, "oz", "Breast Milk")],
        )
        self.assertEqual([b["start"] for b in bottles], [ts("2026-10-12T10:30"), ts("2026-10-12T14:15")])
        if BOTTLE_NOTES:
            self.assertEqual(bottles[0]["notes"], "From daycare · offered 4.5 oz, took 4 oz")
            self.assertEqual(
                bottles[1]["notes"],
                "From daycare · offered 4.5 oz, took 3.5 oz · teacher: Took a little break halfway through.",
            )

        naps = self.store.intervals("sleep")
        self.assertEqual(
            [(n["start"], n["duration"]) for n in naps],
            [(ts("2026-10-12T09:40"), 35 * 60), (ts("2026-10-12T12:10"), 48 * 60), (ts("2026-10-12T14:45"), 40 * 60)],
        )
        self.assertTrue(all(n["offset"] == 240 for n in naps))

        # "Last diaper/bottle/sleep" summaries point at the latest entries.
        self.assertEqual(self.store.docs[("diaper", "child-1")]["prefs"]["lastDiaper"]["start"], ts("2026-10-12T15:40"))
        self.assertEqual(self.store.docs[("feed", "child-1")]["prefs"]["lastBottle"]["bottleAmount"], 3.5)
        self.assertEqual(self.store.docs[("sleep", "child-1")]["prefs"]["lastSleep"]["duration"], 40 * 60)

        self.assertEqual(len(self.outbox.sent), 1)
        subject, body = self.outbox.sent[0]
        self.assertEqual(subject, "Josie Mon Oct 12: 9 added to Huckleberry")
        self.assertIn("Bottles: 2, 7.5 oz total", body)
        self.assertIn("Naps: 3, 2h 3m total", body)
        self.assertIn("Diapers: 4 (2 wet, 1 wet + BM, 1 BM)", body)
        self.assertIn("2:15 PM bottle: Took a little break halfway through.", body)

        journal = Journal(self.data / "journal.sqlite3")
        try:
            self.assertEqual(journal.message_status("<forwarded-oct-12@example.com>"), "done")
            self.assertEqual(len(journal.events_for_day("2026-10-12")), 9)
        finally:
            journal.close()

    def test_running_again_changes_nothing(self):
        self.run_sync([fixture("forwarded-oct-12.eml")])
        writes = len(self.store.writes)
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(len(self.store.writes), writes)
        self.assertEqual(report.messages, [])
        self.assertIn("No new daily sheets", describe_report(report))
        self.assertEqual(len(self.outbox.sent), 1)

    def test_same_sheet_forwarded_twice_is_not_doubled(self):
        self.run_sync([fixture("forwarded-oct-12.eml")])
        writes = len(self.store.writes)
        again = with_message_id(fixture("forwarded-oct-12.eml"), "<forwarded-again@example.com>")
        report = self.run_sync([fixture("forwarded-oct-12.eml"), again])
        self.assertEqual(len(self.store.writes), writes)
        self.assertEqual(report.added, 0)
        self.assertEqual(len(report.messages[0].sheets[0].already), 9)
        self.assertEqual(len(self.outbox.sent), 1)  # nothing new, so no second email

    def test_auto_forwarded_original_email_works_too(self):
        report = self.run_sync([fixture("original-oct-12.eml")])
        self.assertEqual(report.added, 9)

    def test_entries_already_in_huckleberry_are_left_alone(self):
        self.store.add_interval("diaper", {"mode": "pee", "start": ts("2026-10-12T11:50"), "offset": 240})
        self.store.add_interval("sleep", {"start": ts("2026-10-12T12:07"), "duration": 50 * 60, "offset": 240})
        # Older entries can live inside a batched "multi" document.
        self.store.add_interval(
            "feed",
            {
                "multi": True,
                "data": {
                    "a": {
                        "mode": "bottle",
                        "start": ts("2026-10-12T10:30"),
                        "amount": 4,
                        "units": "oz",
                        "bottleType": "Breast Milk",
                        "offset": 240,
                    },
                    "b": {"mode": "breast", "start": ts("2026-10-11T13:00"), "offset": 240},
                },
            },
        )
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(report.added, 6)
        sheet = report.messages[0].sheets[0]
        reasons = [why for _, why in sheet.already]
        self.assertEqual(len(reasons), 3)
        self.assertIn("Huckleberry already has a diaper at 11:50 AM (wet)", reasons)
        self.assertIn("Huckleberry already has a nap at 12:07 PM (50 min)", reasons)
        self.assertIn("Huckleberry already has a bottle at 10:30 AM (4 oz Breast Milk)", reasons)
        self.assertEqual(len(self.store.intervals("diaper")), 4)  # 3 new + the one that was there
        # Where Huckleberry's entry disagrees with the sheet, you're told; nothing is changed.
        self.assertEqual(
            sheet.sheet.problems,
            [
                "11:50 AM diaper: Huckleberry has wet, the sheet says wet + BM. Left as it was.",
            ],
        )
        self.assertEqual(self.outbox.sent[0][0], "Josie Mon Oct 12: 6 added to Huckleberry, 1 needs a look")

    def test_hand_entries_a_few_minutes_off_are_recognised(self):
        # Diapers and bottles copied into the app by hand from the sheet, 0 to 10
        # minutes off its times (as happens in real life). Only the naps are missing.
        for clock, mode in (("09:05", "pee"), ("12:00", "both"), ("13:36", "pee"), ("15:37", "poo")):
            self.store.add_interval("diaper", {"mode": mode, "start": ts(f"2026-10-12T{clock}"), "offset": 240})
        for clock, amount in (("10:30", 4), ("14:09", 3.5)):
            self.store.add_interval(
                "feed",
                {
                    "mode": "bottle",
                    "start": ts(f"2026-10-12T{clock}"),
                    "amount": amount,
                    "units": "oz",
                    "bottleType": "Breast Milk",
                    "offset": 240,
                },
            )
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        item = report.messages[0].sheets[0]
        self.assertEqual([e.kind for e in item.added], ["nap", "nap", "nap"])
        self.assertEqual(len(item.already), 6)
        self.assertEqual(item.sheet.problems, [])
        self.assertEqual(len(self.store.intervals("diaper")), 4)
        self.assertEqual(len(self.store.intervals("feed")), 2)
        self.assertEqual([item.key_of(e) for e in item.added], ["101226AN1", "101226AN2", "101226AN3"])

    def test_one_hand_entry_cannot_hide_two_daycare_diapers(self):
        # Daycare changed two wet diapers 10 minutes apart; one was entered by hand in
        # between. It can stand in for only one of them.
        sheet = edit_email(fixture("forwarded-oct-12.eml"), "BM diaper at 3:40pm", "Wet diaper at 1:45pm")
        self.store.add_interval("diaper", {"mode": "pee", "start": ts("2026-10-12T13:40"), "offset": 240})
        report = self.run_sync([sheet])
        item = report.messages[0].sheets[0]
        self.assertEqual(len(item.already), 1)
        self.assertEqual(len(item.added), 8)

    def test_corrected_sheet_is_reported_not_applied(self):
        self.run_sync([fixture("forwarded-oct-12.eml")])
        writes = len(self.store.writes)
        corrected = edit_email(
            fixture("forwarded-oct-12.eml"), "took 4 ounces at 10:30am", "took 3.5 ounces at 10:30am"
        )
        corrected = with_message_id(corrected, "<corrected@example.com>")
        report = self.run_sync([corrected])
        self.assertEqual(len(self.store.writes), writes)  # Huckleberry untouched
        problems = report.messages[0].sheets[0].sheet.problems
        self.assertEqual(
            problems,
            [
                (
                    'Daycare changed "10:30 AM Bottle 4 oz breast milk (offered 4.5 oz)" to '
                    '"10:30 AM Bottle 3.5 oz breast milk (offered 4.5 oz)". Huckleberry was left as it was.'
                )
            ],
        )
        self.assertEqual(self.outbox.sent[-1][0], "Josie Mon Oct 12: 0 added to Huckleberry, 1 needs a look")

    def test_preview_writes_nothing(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")], dry_run=True)
        self.assertEqual(self.store.writes, [])
        self.assertEqual(len(report.messages[0].sheets[0].would_add), 9)
        self.assertIn("Preview only", describe_report(report))
        self.assertEqual(self.outbox.sent, [])
        # ...and the real run afterwards still does everything.
        self.assertEqual(self.run_sync([fixture("forwarded-oct-12.eml")]).added, 9)

    def test_unknown_sender_is_ignored(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")], ALLOWED_SENDERS="someone-else@example.com")
        self.assertEqual(report.messages[0].status, "ignored")
        self.assertEqual(self.store.writes, [])

    def test_other_child_is_ignored(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")], DAYCARE_CHILD="Oliver")
        self.assertEqual(report.messages[0].status, "ignored")
        self.assertIn("Skipped Josephine's sheet", report.messages[0].reason)
        self.assertEqual(self.store.writes, [])

    def test_import_since_skips_older_sheets(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")], IMPORT_SINCE="2026-10-13")
        self.assertEqual(report.messages[0].status, "ignored")
        self.assertEqual(self.store.writes, [])

    def test_failed_write_is_retried_without_duplicates(self):
        self.store.fail_on_write = 7  # dies part-way through the sheet
        first = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertIn("simulated network failure", first.error)
        self.assertEqual(first.messages[0].status, "error")
        self.assertEqual(self.outbox.sent, [])
        partial = sum(len(self.store.intervals(c)) for c in ("diaper", "feed", "sleep"))
        self.assertGreater(partial, 0)
        self.assertLess(partial, 9)

        second = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(second.error, "")
        self.assertEqual(second.messages[0].status, "done")
        self.assertEqual(sum(len(self.store.intervals(c)) for c in ("diaper", "feed", "sleep")), 9)

    def test_library_rejecting_odd_data_is_explained(self):
        # A value the library's strict models don't know makes it refuse to write.
        self.store.docs[("feed", "child-1")] = {"prefs": {"lastBottle": {"mode": "bottle", "bottleType": "Juice"}}}
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(report.messages[0].status, "error")
        self.assertIn("A newer py-huckleberry-api usually fixes this", report.error)
        self.assertIn("10:30 AM bottle", report.error)
        self.assertEqual(self.store.intervals("feed"), [])

    def test_crash_after_write_before_journal_is_caught_by_app_check(self):
        self.run_sync([fixture("forwarded-oct-12.eml")])
        (self.data / "journal.sqlite3").unlink()  # pretend the journal was lost
        for extra in ("-wal", "-shm"):
            (self.data / f"journal.sqlite3{extra}").unlink(missing_ok=True)
        report = self.run_sync([fixture("forwarded-oct-12.eml")])
        self.assertEqual(report.added, 0)
        self.assertEqual(sum(len(self.store.intervals(c)) for c in ("diaper", "feed", "sleep")), 9)

    def test_daylight_saving_offset_uses_the_event_date(self):
        # A late-October sheet logged after clocks change on Nov 1 keeps its EDT offset.
        raw = fixture("forwarded-oct-12.eml")
        later = edit_email(raw, "Monday, October 12th, 2026", "Friday, October 30th, 2026")
        report = self.run_sync([later], now=at("2026-11-03T20:00:00"))
        self.assertEqual(report.added, 9)
        self.assertTrue(all(d["offset"] == 240 for d in self.store.intervals("diaper")))
        november = edit_email(raw, "Monday, October 12th, 2026", "Monday, November 2nd, 2026")
        november = with_message_id(november, "<november@example.com>")
        self.run_sync([november], now=at("2026-11-03T20:00:00"))
        offsets = sorted({d["offset"] for d in self.store.intervals("diaper")})
        self.assertEqual(offsets, [240, 300])

    def test_future_sheet_is_skipped(self):
        report = self.run_sync([fixture("forwarded-oct-12.eml")], now=at("2026-10-11T12:00:00"))
        self.assertEqual(report.messages[0].status, "ignored")
        self.assertEqual(self.store.writes, [])


class ChildPickingTest(unittest.TestCase):
    def pick(self, store, child=""):
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            s = settings(Path(tmp), HUCKLEBERRY_CHILD=child)

            async def go():
                async with Huckleberry(s, api=make_api(store)) as hb:
                    return hb.child_uid, hb.child_name

            return asyncio.run(go())

    def test_single_child_needs_no_name(self):
        self.assertEqual(self.pick(family()), ("child-1", "Josie"))

    def test_picks_by_name_or_nickname(self):
        store = family((("c1", "Josephine", "Josie"), ("c2", "Oliver", "Ollie")))
        self.assertEqual(self.pick(store, "josie"), ("c1", "Josephine"))
        self.assertEqual(self.pick(store, "Oliver"), ("c2", "Oliver"))

    def test_close_shuts_the_grpc_channel(self):
        import asyncio
        from types import SimpleNamespace

        closed = []

        class Transport:
            async def close(self):
                closed.append("channel")

        class Session:
            async def close(self):
                closed.append("session")

        with tempfile.TemporaryDirectory() as tmp:
            hb = Huckleberry(settings(Path(tmp)))
        hb.api = SimpleNamespace(_firestore_client=SimpleNamespace(_transport=Transport()))
        hb._session = Session()
        asyncio.run(hb.close())
        self.assertEqual(closed, ["channel", "session"])

    def test_unknown_name_lists_the_children(self):
        from millie_sync.huckleberry import HuckleberryError

        store = family((("c1", "Josephine", "Josie"), ("c2", "Oliver", "Ollie")))
        with self.assertRaises(HuckleberryError) as caught:
            self.pick(store, "Mila")
        self.assertIn("Josephine, Oliver", str(caught.exception))
        with self.assertRaises(HuckleberryError):
            self.pick(store, "")


if __name__ == "__main__":
    unittest.main()
