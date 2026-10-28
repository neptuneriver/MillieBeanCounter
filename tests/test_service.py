"""The background loop: status, health and problem emails."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeMailbox, Outbox, family, fixture, make_api

from millie_sync import config, data, service, sync
from millie_sync.huckleberry import Huckleberry, HuckleberryError
from millie_sync.mailbox import MailError

SETTINGS = {
    "GMAIL_ADDRESS": "daycare-inbox@example.com",
    "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
    "HUCKLEBERRY_EMAIL": "parent@example.com",
    "HUCKLEBERRY_PASSWORD": "x",
    "DAYCARE_CHILD": "Emilia",
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
        agent = (self.data / "exports" / "latest" / "csv" / "agent_diapers.csv").read_text().splitlines()
        self.assertEqual(
            [line.split(",")[0] for line in agent[1:]], ["101226AD1", "101226AD2", "101226AD3", "101226AD4"]
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
        self.assertEqual([subject for subject, _ in self.outbox.sent][-1], "Millie sync needs attention")
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
        self.assertEqual([subject for subject, _ in self.outbox.sent], ["Millie sync needs attention"])

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

    def test_health_fails_without_a_heartbeat(self):
        self.assertEqual(service.health(self.data), 1)


if __name__ == "__main__":
    unittest.main()
