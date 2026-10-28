"""The background loop: status, health and problem emails."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeMailbox, Outbox, family, fixture, make_api

from millie_bean_counter import config, data, service, sync
from millie_bean_counter.huckleberry import Huckleberry, HuckleberryError
from millie_bean_counter.mailbox import MailError

SETTINGS = {
    "GMAIL_ADDRESS": "daycare-inbox@example.com",
    "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
    "HUCKLEBERRY_EMAIL": "parent@example.com",
    "HUCKLEBERRY_PASSWORD": "x",
    "DAYCARE_CHILD": "Josephine",
    "NOTIFY_EMAIL": "parent@example.com",
}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.store = family()
        self.outbox = Outbox()
        patches = [
            mock.patch.object(sync, "send_email", self.outbox),
            mock.patch.object(service, "send_email", self.outbox),
            mock.patch.object(sync, "Mailbox", FakeMailbox([fixture("forwarded-oct-12.eml")])),
            mock.patch.object(sync, "Huckleberry", lambda s: Huckleberry(s, api=make_api(self.store))),
            mock.patch.object(data, "Huckleberry", lambda s: Huckleberry(s, api=make_api(self.store))),
            mock.patch.object(service, "log", lambda message: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_waits_for_setup(self):
        self.assertEqual(service.check_once(self.data), service.SETUP_WAIT_MINUTES)
        self.assertEqual(service.read_status(self.data)["state"], "needs setup")

    def test_normal_pass(self):
        config.save(SETTINGS, self.data)
        self.assertEqual(service.check_once(self.data), 15)
        status = service.read_status(self.data)
        self.assertEqual(status["state"], "running")
        self.assertIn("last_import", status)
        self.assertEqual(service.health(self.data), 0)
        self.assertEqual(len(self.outbox.sent), 1)  # the summary
        self.assertEqual(len(self.store.intervals("diaper")), 4)
        # The data export ran right after the import.
        self.assertIn("last_export", service.read_status(self.data))
        synced = (self.data / "exports" / "latest" / "csv" / "daycare_diapers.csv").read_text().splitlines()
        self.assertEqual(
            [line.split(",")[0] for line in synced[1:]], ["101226SD1", "101226SD2", "101226SD3", "101226SD4"]
        )
        # ...and isn't repeated on the next quiet check.
        stamp = service.read_status(self.data)["last_export"]
        service.check_once(self.data)
        self.assertEqual(service.read_status(self.data)["last_export"], stamp)

    def test_failed_export_after_new_entries_is_retried(self):
        config.save(SETTINGS, self.data)

        class BrokenReader:
            def __init__(self, settings):
                pass

            async def __aenter__(self):
                raise HuckleberryError("Huckleberry is having a moment.")

            async def __aexit__(self, *exc):
                return None

        with mock.patch.object(data, "Huckleberry", BrokenReader):
            service.check_once(self.data)
        status = service.read_status(self.data)
        self.assertIn("having a moment", status["export_error"])
        self.assertNotIn("last_export", status)
        self.assertEqual([subject for subject, _ in self.outbox.sent][-1], "MillieBeanCounter needs attention")
        self.assertTrue(data.export_due(config.load(self.data), 0))
        service.check_once(self.data)  # nothing new in Gmail, but the export catches up
        status = service.read_status(self.data)
        self.assertIn("last_export", status)
        self.assertEqual(status["export_error"], "")
        self.assertFalse(data.export_due(config.load(self.data), 0))

    def test_problem_is_emailed_once_a_day(self):
        config.save(SETTINGS, self.data)

        class BrokenHuckleberry:
            def __init__(self, settings):
                pass

            async def __aenter__(self):
                raise HuckleberryError("Huckleberry didn't accept that email and password.")

            async def __aexit__(self, *exc):
                return None

        with mock.patch.object(sync, "Huckleberry", BrokenHuckleberry):
            service.check_once(self.data)
            service.check_once(self.data)
        status = service.read_status(self.data)
        self.assertEqual(status["state"], "problem")
        self.assertIn("didn't accept", status["last_error"])
        self.assertEqual([subject for subject, _ in self.outbox.sent], ["MillieBeanCounter needs attention"])

    def test_gmail_problem_is_not_emailed(self):
        config.save(SETTINGS, self.data)

        class BrokenMailbox:
            def __init__(self, settings):
                pass

            def __enter__(self):
                raise MailError("Gmail didn't accept the login.")

            def __exit__(self, *exc):
                return None

        with mock.patch.object(sync, "Mailbox", BrokenMailbox):
            service.check_once(self.data)
        self.assertEqual(service.read_status(self.data)["state"], "problem")
        self.assertEqual(self.outbox.sent, [])

    def photo_patches(self, drive):
        from fakes import FakePictures, jpeg, moment_url, moments_email

        from millie_bean_counter import photos

        mailbox = FakeMailbox([fixture("forwarded-oct-12.eml"), moments_email("2026-10-27", 2)])
        pictures = FakePictures({moment_url(n): (jpeg(f"p{n}"), "image/jpeg") for n in (1, 2)})
        for patch in (
            mock.patch.object(sync, "Mailbox", mailbox),
            mock.patch.object(photos.Drive, "from_saved", lambda data_dir: drive()),
            mock.patch.object(photos, "Downloader", lambda: pictures),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.save({**SETTINGS, "DRIVE_PHOTOS": "yes", "CHILD_BIRTHDAY": "2026-06-03"}, self.data)

    def test_photos_are_saved_in_the_same_pass(self):
        from fakes import FakeDrive

        drive = FakeDrive()
        self.photo_patches(lambda: drive)
        service.check_once(self.data)
        status = service.read_status(self.data)
        self.assertEqual(status["state"], "running")
        self.assertIn("last_photos", status)
        self.assertEqual(len(drive.files()), 2)
        # No CHILD_NAME or HUCKLEBERRY_CHILD saved here, so the daily-sheet name is used.
        self.assertIn(
            "Saved Josephine Daycare Photos/October 2026/10/27 Josephine 4m 24d Photo 1.jpg", status["last_result"]
        )
        self.assertEqual(len(self.store.intervals("diaper")), 4)  # the daily sheet went in too

    def test_a_drive_problem_is_reported_but_the_sheets_still_go_in(self):
        from millie_bean_counter.gdrive import DriveError

        def no_drive():
            raise DriveError("Google Drive isn't signed in yet. Run Setup.command again.")

        self.photo_patches(no_drive)
        service.check_once(self.data)
        status = service.read_status(self.data)
        self.assertEqual(status["state"], "problem")
        self.assertIn("Photos: Google Drive isn't signed in yet", status["last_error"])
        self.assertEqual(len(self.store.intervals("diaper")), 4)
        self.assertIn("MillieBeanCounter needs attention", [subject for subject, _ in self.outbox.sent])

    def test_health_fails_without_a_heartbeat(self):
        self.assertEqual(service.health(self.data), 1)


if __name__ == "__main__":
    unittest.main()
