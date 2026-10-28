"""Daycare photos: reading Kaymbu's moment emails, naming, and saving into (fake) Google Drive."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fakes import (
    DOWNLOAD_LINK,
    MOMENT_URL,
    FakeDrive,
    FakeMailbox,
    FakePictures,
    at,
    download_link,
    fixture,
    forward_as_attachments,
    gmail_forward,
    jpeg,
    moment_url,
    moments_email,
    settings,
)

from millie_bean_counter import photos
from millie_bean_counter.journal import Journal
from millie_bean_counter.mailbox import _header_from_bytes
from millie_bean_counter.photos import (
    PhotoGone,
    PhotoTrouble,
    age_text,
    find_moments,
    month_folder,
    photo_name,
    read_email,
    save_photos,
)
from millie_bean_counter.sync import describe_photos

TZ = ZoneInfo("America/New_York")
NOW = at("2026-10-27T18:00:00")
PHOTO_SETTINGS = {"DRIVE_PHOTOS": "yes", "CHILD_NAME": "Josie", "CHILD_BIRTHDAY": "2026-06-03"}


def header(raw: bytes, uid: str = "1"):
    return _header_from_bytes(uid, raw, "INBOX")


class NamesTest(unittest.TestCase):
    def test_age_in_months_and_days(self):
        born = date(2026, 6, 3)
        self.assertEqual(age_text(born, date(2026, 10, 14)), "4m 11d")
        self.assertEqual(age_text(born, date(2026, 6, 3)), "0m 0d")
        self.assertEqual(age_text(born, date(2026, 7, 2)), "0m 29d")
        self.assertEqual(age_text(born, date(2026, 7, 3)), "1m 0d")
        self.assertEqual(age_text(born, date(2027, 6, 2)), "11m 30d")
        self.assertEqual(age_text(born, date(2027, 6, 3)), "12m 0d")
        self.assertEqual(age_text(born, date(2028, 6, 4)), "2y 0m 1d")
        self.assertEqual(age_text(born, date(2026, 5, 1)), "")  # before the birthday
        self.assertEqual(age_text(None, date(2026, 10, 14)), "")

    def test_age_at_the_end_of_short_months(self):
        born = date(2027, 1, 31)
        self.assertEqual(age_text(born, date(2027, 2, 28)), "1m 0d")  # no Feb 31: the last day counts
        self.assertEqual(age_text(born, date(2027, 3, 30)), "1m 30d")
        self.assertEqual(age_text(born, date(2027, 3, 31)), "2m 0d")
        self.assertEqual(age_text(date(2028, 2, 29), date(2029, 2, 28)), "12m 0d")

    def test_file_and_folder_names(self):
        day = date(2026, 10, 14)
        self.assertEqual(photo_name(day, "Josie", "4m 11d", 1, ".jpg"), "10/14 Josie 4m 11d Photo 1.jpg")
        self.assertEqual(photo_name(date(2026, 9, 5), "Josie", "", 12, ".png"), "09/05 Josie Photo 12.png")
        self.assertEqual(month_folder(day), "October 2026")
        self.assertEqual(month_folder(date(2027, 1, 2)), "January 2027")


class ReadingTest(unittest.TestCase):
    def test_kaymbu_moment_email(self):
        raw = fixture("moments-oct-14.eml")
        (item,) = read_email(raw, header(raw), TZ)
        self.assertEqual(item.key, "<moments-oct-14@example.com>")
        self.assertEqual(item.sender, "classroom@inbox.kaymbu.com")
        self.assertEqual(item.shown_day, date(2026, 10, 14))
        self.assertEqual(item.sent, at("2026-10-14T15:22:08"))
        # Only the moment itself: not the school banner, icons, app badges, logo or tracking pixel.
        self.assertEqual([(m.url, m.download) for m in item.moments], [(MOMENT_URL, DOWNLOAD_LINK)])

    def test_several_moments_pair_with_their_own_download_links(self):
        raw = moments_email(moments=3)
        (item,) = read_email(raw, header(raw), TZ)
        self.assertEqual(
            [(m.url, m.download) for m in item.moments], [(moment_url(n), download_link(n)) for n in (1, 2, 3)]
        )

    def test_download_links_are_left_out_when_they_dont_line_up(self):
        html = (
            '<img class="moment-image" src="https://cdn.example.invalid/media/s/2026/moments/a_display.jpg">'
            '<img class="moment-image" src="https://cdn.example.invalid/media/s/2026/moments/b_display.jpg">'
            '<a href="https://example.invalid/all"><img alt="Download all" src="https://cdn.example.invalid/i.png"></a>'
        )
        self.assertEqual([m.download for m in find_moments(html)], ["", ""])

    def test_gmail_proxy_addresses_and_moment_paths_without_the_class(self):
        html = (
            '<img src="https://ci3.googleusercontent.com/meips/ABC=s0-d-e1-ft#'
            'https://cdn.example.invalid/media/s/2026/moments/a_display.jpg">'
            '<img src="http://cdn.example.invalid/media/s/2026/moments/plain-http.jpg">'
            '<img src="https://cdn.example.invalid/email/footer_logo.png" width="71">'
        )
        self.assertEqual(
            [m.url for m in find_moments(html)], ["https://cdn.example.invalid/media/s/2026/moments/a_display.jpg"]
        )

    def test_forwarded_by_hand_uses_the_original_date(self):
        raw = gmail_forward(moments_email("2026-10-20"))
        (item,) = read_email(raw, header(raw), TZ)
        self.assertEqual(item.shown_day, date(2026, 10, 20))
        self.assertEqual(item.sent, at("2026-10-20T15:22:00"))  # not the day it was forwarded
        self.assertEqual([(m.url, m.download) for m in item.moments], [(MOMENT_URL, download_link(1))])

    def test_forwarded_as_attachments_counts_each_email_on_its_own(self):
        raw = forward_as_attachments(
            moments_email("2026-10-05", message_id="<a@example.com>"),
            fixture("original-oct-12.eml"),  # a daily sheet in the bundle is left to the sheet sync
            moments_email("2026-10-06", 2, message_id="<b@example.com>", first=2),
        )
        items = read_email(raw, header(raw), TZ)
        self.assertEqual([i.key for i in items], ["<a@example.com>", "<b@example.com>"])
        self.assertEqual([i.shown_day for i in items], [date(2026, 10, 5), date(2026, 10, 6)])
        self.assertEqual([len(i.moments) for i in items], [1, 2])
        self.assertEqual(items[0].sender, "classroom@inbox.kaymbu.com")

    def test_daily_sheets_have_no_moments(self):
        raw = fixture("original-oct-12.eml")
        (item,) = read_email(raw, header(raw), TZ)
        self.assertEqual(item.moments, [])


class FakeResponse:
    def __init__(self, status=200, mime="image/jpeg", body=b"", error=None):
        self.status_code = status
        self.headers = {"Content-Type": mime}
        self.body = body
        self.error = error
        self.closed = False

    def iter_content(self, size):
        if self.error:
            raise self.error
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, reply):
        self.reply = reply
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class DownloaderTest(unittest.TestCase):
    def get(self, reply, url="https://cdn.example.invalid/p.jpg"):
        return photos.Downloader(FakeSession(reply))(url)

    def test_a_picture(self):
        self.assertEqual(self.get(FakeResponse(body=jpeg("a"))), (jpeg("a"), "image/jpeg"))
        self.assertEqual(self.get(FakeResponse(mime="image/png; charset=binary", body=jpeg("b")))[1], "image/png")

    def test_links_that_will_never_work(self):
        for reply in (
            FakeResponse(404),
            FakeResponse(403),
            FakeResponse(mime="text/html", body=b"<html>sign in</html>" * 100),
            FakeResponse(mime="image/svg+xml", body=b"<svg/>" * 500),
            FakeResponse(body=b"tiny"),
        ):
            with self.assertRaises(PhotoGone):
                self.get(reply)
        invalid_schema = type("InvalidSchema", (Exception,), {})  # what requests raises for app:// redirects
        with self.assertRaises(PhotoGone):
            self.get(invalid_schema("No connection adapters were found for 'goddardhub://moment'"))
        for url in ("http://cdn.example.invalid/p.jpg", "https://127.0.0.1/p.jpg", "https://localhost/p.jpg"):
            with self.assertRaises(PhotoGone):
                self.get(FakeResponse(body=jpeg("a")), url)

    def test_trouble_worth_retrying(self):
        for reply in (
            FakeResponse(503),
            FakeResponse(429),
            ConnectionError("reset"),
            FakeResponse(body=jpeg("a"), error=ConnectionError("dropped")),
        ):
            with self.assertRaises(PhotoTrouble):
                self.get(reply)

    def test_too_big(self):
        with mock.patch.object(photos, "MAX_BYTES", 10_000), self.assertRaises(PhotoGone):
            self.get(FakeResponse(body=jpeg("a", 20_000)))


class SaveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.drive = FakeDrive()
        self.pictures = FakePictures({moment_url(n): (jpeg(f"photo{n}"), "image/jpeg") for n in range(1, 9)})

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, messages, now=NOW, dry_run=False, **overrides):
        values = {**PHOTO_SETTINGS, **overrides}
        journal = Journal(self.data / "journal.sqlite3")
        self.mailbox = FakeMailbox(messages)
        try:
            return save_photos(
                settings(self.data, **values),
                journal,
                dry_run=dry_run,
                now=now,
                mailbox_factory=self.mailbox,
                drive_factory=self.drive,
                get=self.pictures,
            )
        finally:
            journal.close()

    def journal(self) -> Journal:
        journal = Journal(self.data / "journal.sqlite3")
        self.addCleanup(journal.close)
        return journal

    def test_photos_land_in_month_folders_with_names_and_ages(self):
        messages = [
            moments_email("2026-10-27", 2, first=1),
            moments_email("2026-10-27", 1, first=3),  # a second email the same day keeps counting
            moments_email("2026-10-28", 1, first=4),  # a new day starts again at 1
            moments_email("2026-11-02", 1, first=5),
        ]
        report = self.save(messages, now=at("2026-11-02T18:00"))
        self.assertEqual(report.error, "")
        self.assertEqual(report.saved, 5)
        self.assertEqual(
            self.drive.files(),
            [
                "Josie Daycare Photos > October 2026 > 10/27 Josie 4m 24d Photo 1.jpg",
                "Josie Daycare Photos > October 2026 > 10/27 Josie 4m 24d Photo 2.jpg",
                "Josie Daycare Photos > October 2026 > 10/27 Josie 4m 24d Photo 3.jpg",
                "Josie Daycare Photos > October 2026 > 10/28 Josie 4m 25d Photo 1.jpg",
                "Josie Daycare Photos > November 2026 > 11/02 Josie 4m 30d Photo 1.jpg",
            ],
        )
        first = self.drive.uploads[0]
        self.assertEqual(first["data"], jpeg("photo1"))
        self.assertEqual(first["mime"], "image/jpeg")
        self.assertEqual(first["modified"], at("2026-10-27T15:22:08"))
        self.assertIn('"Shared Moments from The Goddard School"', first["description"])
        self.assertEqual(self.mailbox.since_days, [date(2026, 6, 3)])  # first time: back to the birthday
        # Only the moments were downloaded (the Download links lead nowhere in this test).
        self.assertTrue(all("moments" in url or "download" in url for url in self.pictures.asked))

        # Nothing is saved twice, and Drive isn't even asked.
        uploads, calls = len(self.drive.uploads), len(self.drive.calls)
        again = self.save(messages, now=at("2026-11-02T18:15"))
        self.assertEqual((again.saved, again.emails), (0, []))
        self.assertEqual((len(self.drive.uploads), len(self.drive.calls)), (uploads, calls))
        self.assertEqual(self.mailbox.since_days, [date(2026, 10, 26)])  # caught up: the usual 7 days

    def test_the_download_links_bigger_picture_is_preferred(self):
        self.pictures.pictures[download_link(1)] = (jpeg("original", 9000), "image/jpeg")
        self.pictures.pictures[download_link(2)] = PhotoGone("not a picture (text/html)")
        self.save([moments_email("2026-10-27", 2)])
        self.assertEqual([u["data"] for u in self.drive.uploads], [jpeg("original", 9000), jpeg("photo2")])
        variants = [row["variant"] for row in self.journal().all_photos()]
        self.assertEqual(variants, ["download", "email"])

    def test_download_links_can_be_turned_off(self):
        self.pictures.pictures[download_link(1)] = (jpeg("original", 9000), "image/jpeg")
        self.save([moments_email("2026-10-27", 1)], PHOTO_DOWNLOAD_LINKS="no")
        self.assertEqual([u["data"] for u in self.drive.uploads], [jpeg("photo1")])
        self.assertNotIn(download_link(1), self.pictures.asked)

    def test_png_keeps_its_extension(self):
        self.pictures.pictures[moment_url(1)] = (jpeg("p"), "image/png")
        self.save([moments_email("2026-10-27", 1)])
        self.assertEqual(self.drive.files(), ["Josie Daycare Photos > October 2026 > 10/27 Josie 4m 24d Photo 1.png"])

    def test_a_moment_shared_twice_is_saved_once(self):
        report = self.save(
            [
                moments_email("2026-10-27", 1, message_id="<first@example.com>"),
                moments_email("2026-10-27", 1, message_id="<again@example.com>"),
                gmail_forward(moments_email("2026-10-27", 1, message_id="<third@example.com>")),
            ]
        )
        self.assertEqual(len(self.drive.uploads), 1)
        self.assertEqual([e.duplicates for e in report.emails], [0, 1, 1])
        rows = self.journal().all_photos()
        self.assertEqual([(r["status"], r["seq"]) for r in rows], [("saved", 1), ("duplicate", 0), ("duplicate", 0)])

    def test_the_same_picture_under_a_new_link_is_saved_once(self):
        self.pictures.pictures[moment_url(2)] = (jpeg("photo1"), "image/jpeg")  # identical bytes
        report = self.save([moments_email("2026-10-27", 1), moments_email("2026-10-27", 1, first=2)])
        self.assertEqual(len(self.drive.uploads), 1)
        self.assertEqual(report.emails[1].duplicates, 1)

    def test_only_daycare_emails_with_moments(self):
        stranger = moments_email("2026-10-27", message_id="<stranger@example.com>").replace(
            b"From: 01 Infants <classroom@inbox.kaymbu.com>", b"From: Someone <someone@example.org>"
        )
        newsletter = moments_email("2026-10-27", 0, message_id="<newsletter@example.com>")
        report = self.save([stranger, fixture("original-oct-12.eml"), newsletter], now=at("2026-10-27T18:00"))
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual([(e.key, e.status) for e in report.emails], [("<newsletter@example.com>", "ignored")])
        self.assertEqual(self.journal().photo_message_status("<newsletter@example.com>"), "ignored")
        self.assertEqual(self.mailbox.fetched, ["3"])  # the stranger and the daily sheet weren't even opened

    def test_bulk_forward_of_older_moments(self):
        bundle = forward_as_attachments(
            moments_email("2026-09-30", 1, message_id="<sep30@example.com>", first=1),
            moments_email("2026-10-01", 2, message_id="<oct1@example.com>", first=2),
        )
        report = self.save([bundle])
        self.assertEqual(
            self.drive.files(),
            [
                "Josie Daycare Photos > September 2026 > 09/30 Josie 3m 27d Photo 1.jpg",
                "Josie Daycare Photos > October 2026 > 10/01 Josie 3m 28d Photo 1.jpg",
                "Josie Daycare Photos > October 2026 > 10/01 Josie 3m 28d Photo 2.jpg",
            ],
        )
        journal = self.journal()
        for key in ("<sep30@example.com>", "<oct1@example.com>", "<bulk-forward@example.com>"):
            self.assertEqual(journal.photo_message_status(key), "done")
        self.assertEqual(report.saved, 3)

    def test_drive_trouble_stops_and_the_next_check_carries_on_in_order(self):
        self.drive.fail_upload_at = 2
        messages = [moments_email("2026-10-27", 3)]
        report = self.save(messages)
        self.assertIn("HTTP 503", report.error)
        self.assertEqual(report.emails[0].status, "error")
        self.assertEqual(len(self.drive.files()), 1)
        report = self.save(messages, now=at("2026-10-27T18:15"))
        self.assertEqual(report.error, "")
        self.assertEqual(
            [name.rsplit(" > ", 1)[1] for name in self.drive.files()],
            ["10/27 Josie 4m 24d Photo 1.jpg", "10/27 Josie 4m 24d Photo 2.jpg", "10/27 Josie 4m 24d Photo 3.jpg"],
        )
        self.assertEqual(self.journal().photo_message_status("<moments-2026-10-27-1@example.com>"), "done")

    def test_an_upload_that_finished_at_google_isnt_repeated(self):
        self.drive.lose_reply_at = 1  # uploaded, but the answer never arrived
        messages = [moments_email("2026-10-27", 1)]
        self.assertIn("ReadTimeout", self.save(messages).error)
        self.assertEqual(self.journal().all_photos()[0]["status"], "uploading")
        self.save(messages, now=at("2026-10-27T18:15"))
        self.assertEqual(len(self.drive.uploads), 1)  # found by its private label instead of uploaded again
        self.assertEqual(self.journal().all_photos()[0]["status"], "saved")

    def test_a_deleted_folder_is_made_again(self):
        self.save([moments_email("2026-10-27", 1)])
        root = next(i for i, item in self.drive.items.items() if item["name"] == "Josie Daycare Photos")
        self.drive.items[root]["trashed"] = True
        self.save([moments_email("2026-10-28", 1, first=2)], now=at("2026-10-28T18:00"))
        roots = [i for i, item in self.drive.items.items() if item["name"] == "Josie Daycare Photos"]
        self.assertEqual(len(roots), 2)
        month = self.drive.items[self.drive.uploads[-1]["parent"]]
        self.assertEqual((month["name"], month["parent"]), ("October 2026", roots[1]))
        self.assertFalse(self.drive.items[roots[1]]["trashed"])

    def test_dead_links_are_retried_for_a_while_then_left(self):
        self.pictures.pictures.clear()  # every picture link says 404
        messages = [moments_email("2026-10-27", 1)]
        report = self.save(messages)
        self.assertEqual(report.emails[0].status, "error")
        self.assertIn("none of the photos could be downloaded", report.emails[0].reason)
        with mock.patch.object(photos, "GIVE_UP_AFTER", 3):
            self.save(messages, now=at("2026-10-27T18:15"))
            report = self.save(messages, now=at("2026-10-27T18:30"))
        self.assertEqual(report.emails[0].status, "ignored")
        self.assertIn("gave up after 3 tries", report.emails[0].reason)
        self.assertEqual(self.save(messages, now=at("2026-10-27T18:45")).emails, [])

    def test_temporary_trouble_retries_the_email(self):
        self.pictures.pictures[moment_url(2)] = PhotoTrouble("HTTP 503")
        messages = [moments_email("2026-10-27", 2)]
        report = self.save(messages)
        self.assertEqual(report.emails[0].status, "error")
        self.assertEqual(report.error, "")  # not a Drive problem; other emails carry on
        del self.pictures.pictures[moment_url(2)]
        self.pictures.pictures[moment_url(2)] = (jpeg("photo2"), "image/jpeg")
        report = self.save(messages, now=at("2026-10-27T18:15"))
        self.assertEqual(
            (report.emails[0].status, report.emails[0].already, len(report.emails[0].saved)), ("done", 1, 1)
        )

    def test_a_big_catch_up_is_spread_over_several_checks(self):
        messages = [moments_email("2026-10-27", 3), moments_email("2026-10-28", 2, first=4)]
        with mock.patch.object(photos, "MAX_PHOTOS_PER_RUN", 2):
            first = self.save(messages)
            self.assertTrue(first.more)
            self.assertIsNone(self.journal().get(photos.BACKFILL_DONE))
            second = self.save(messages, now=at("2026-10-28T18:00"))
            third = self.save(messages, now=at("2026-10-28T18:15"))
        self.assertEqual([first.saved, second.saved, third.saved], [2, 2, 1])
        self.assertEqual(len(self.drive.files()), 5)
        self.assertIsNotNone(self.journal().get(photos.BACKFILL_DONE))

    def test_preview_writes_nothing(self):
        report = self.save([moments_email("2026-10-27", 2)], dry_run=True)
        self.assertEqual((report.waiting, self.drive.uploads, self.pictures.asked), (2, [], []))
        self.assertEqual(self.journal().recent_photo_messages(), [])
        self.assertIn('Preview only: 2 photo(s) would go to "Josie Daycare Photos"', "\n".join(describe_photos(report)))

    def test_turned_off_does_nothing(self):
        report = self.save([moments_email("2026-10-27")], DRIVE_PHOTOS="no")
        self.assertFalse(report.enabled)
        self.assertEqual((self.mailbox.since_days, self.drive.uploads), ([], []))
        self.assertEqual(describe_photos(report), [])

    def test_without_a_birthday_the_age_is_left_out(self):
        self.save([moments_email("2026-10-27")], CHILD_BIRTHDAY="", CHILD_NAME="")
        # No CHILD_NAME: the Huckleberry name is used.
        self.assertEqual(self.drive.files(), ["Josie Daycare Photos > October 2026 > 10/27 Josie Photo 1.jpg"])

    def test_the_summary_lines(self):
        report = self.save([moments_email("2026-10-27", 2)])
        text = "\n".join(describe_photos(report))
        self.assertIn('Photos: "Shared Moments from The Goddard School" (Oct 27)', text)
        self.assertIn("Saved Josie Daycare Photos/October 2026/10/27 Josie 4m 24d Photo 2.jpg", text)


if __name__ == "__main__":
    unittest.main()
