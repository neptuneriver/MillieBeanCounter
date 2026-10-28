"""Settings storage and the Gmail/SMTP plumbing (with fake servers)."""

from __future__ import annotations

import imaplib
import os
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fakes import fixture, settings

from millie_sync import config
from millie_sync.mailbox import Mailbox, MailError, imap_date, send_email


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_values_are_taken_literally(self):
        values = config.parse_env("A=pa=ss#word\n# comment\nB = \"quoted value\" \nexport C='x'\nD=\n")
        self.assertEqual(values, {"A": "pa=ss#word", "B": "quoted value", "C": "x", "D": ""})

    def test_save_keeps_saved_passwords_when_left_blank(self):
        config.save(
            {
                "GMAIL_ADDRESS": "inbox@example.com",
                "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
                "HUCKLEBERRY_PASSWORD": "p@ss word=1",
            },
            self.data,
        )
        saved = config.save({"GMAIL_APP_PASSWORD": "", "HUCKLEBERRY_PASSWORD": "", "NOTIFY_EMAIL": ""}, self.data)
        self.assertEqual(saved["GMAIL_APP_PASSWORD"], "abcdefghijklmnop")
        self.assertEqual(saved["HUCKLEBERRY_PASSWORD"], "p@ss word=1")
        mode = stat.S_IMODE(os.stat(config.settings_path(self.data)).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertIn("GMAIL_APP_PASSWORD_SAVED=yes", config.public_view(config.read_raw(self.data)))
        self.assertNotIn("abcdefghijklmnop", "\n".join(config.public_view(config.read_raw(self.data))))

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(config.ConfigError):
            config.save({"GMAIL_PASWORD": "x"}, self.data)

    def test_missing_values_are_explained(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.from_values({"GMAIL_ADDRESS": "not-an-email"}, self.data)
        text = str(caught.exception)
        self.assertIn("doesn't look like an email", text)
        self.assertIn("HUCKLEBERRY_PASSWORD", text)

    def test_passwords_never_show_up_in_printouts(self):
        text = repr(settings(self.data))
        self.assertNotIn("not-a-real-password", text)
        self.assertNotIn("abcd", text)

    def test_senders_and_child_names(self):
        s = settings(self.data, ALLOWED_SENDERS="parent@example.com, kaymbu.com")
        self.assertTrue(s.sender_allowed("parent@example.com"))
        self.assertTrue(s.sender_allowed("classroom@inbox.kaymbu.com"))
        self.assertFalse(s.sender_allowed("stranger@example.com"))
        self.assertFalse(s.sender_allowed("x@notkaymbu.com"))
        self.assertTrue(settings(self.data, ALLOWED_SENDERS="").sender_allowed("anyone@example.com"))
        self.assertTrue(s.daycare_child_matches("Emilia"))
        self.assertTrue(s.daycare_child_matches("emilia Example"))
        self.assertFalse(s.daycare_child_matches("Emily"))


class FakeIMAP:
    """Answers like imaplib does when talking to Gmail."""

    def __init__(self, messages, *, password="abcdefghijklmnop", uid_after_literal=False):
        self.messages = messages
        self.password = password
        self.uid_after_literal = uid_after_literal
        self.commands = []

    def __call__(self, host, port, ssl_context=None, timeout=None):
        self.address = (host, port)
        return self

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"Success"]

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def logout(self):
        return "BYE", [b""]

    def _item(self, uid, payload, section):
        if self.uid_after_literal:
            return [(f"{uid} ({section} {{{len(payload)}}}".encode(), payload), f" UID {uid})".encode()]
        return [(f"{uid} (UID {uid} {section} {{{len(payload)}}}".encode(), payload), b")"]

    def uid(self, command, *args):
        self.commands.append((command,) + args)
        if command == "SEARCH":
            return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]
        if command == "FETCH":
            uids, what = args[0].split(","), args[1]
            data = []
            for uid in uids:
                raw = self.messages[int(uid) - 1]
                if "HEADER.FIELDS" in what:
                    data += self._item(uid, raw.split(b"\n\n", 1)[0] + b"\r\n\r\n", "BODY[HEADER.FIELDS]")
                else:
                    data += self._item(uid, raw, "BODY[]")
            return "OK", data
        raise AssertionError(command)


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = settings(Path(self._tmp.name))
        not_a_sheet = b"From: someone@example.com\nSubject: Picture day\nMessage-ID: <x@y>\n\nhello\n"
        self.messages = [fixture("forwarded-oct-12.eml"), not_a_sheet]

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_headers_then_bodies_without_changing_anything(self):
        for uid_after in (False, True):
            imap = FakeIMAP(self.messages, uid_after_literal=uid_after)
            with Mailbox(self.settings, imap_factory=imap) as box:
                found = box.recent(date(2026, 10, 23))
                self.assertEqual(
                    [(h.uid, h.key, h.sender) for h in found],
                    [("1", "<forwarded-oct-12@example.com>", "parent@example.com")],
                )
                self.assertEqual(box.fetch("1"), self.messages[0])
            self.assertEqual(imap.commands[0], ("select", '"INBOX"', True))
            self.assertEqual(imap.commands[1], ("SEARCH", "SINCE", "16-Oct-2026", "SUBJECT", '"Daily Sheet"'))
            self.assertTrue(all("PEEK" in c[2] for c in imap.commands if c[0] == "FETCH"))

    def test_wrong_password_explains_app_passwords(self):
        imap = FakeIMAP(self.messages, password="something-else")
        with self.assertRaises(MailError) as caught, Mailbox(self.settings, imap_factory=imap):
            pass
        self.assertIn("App Password", str(caught.exception))

    def test_imap_dates_are_english(self):
        self.assertEqual(imap_date(date(2026, 3, 5)), "05-Mar-2026")

    def test_summary_email(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, context=None, timeout=None):
                sent["server"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def login(self, user, password):
                sent["login"] = (user, password)

            def send_message(self, message):
                sent["message"] = message

        send_email(self.settings, "Subject here", "Body here", smtp_factory=FakeSMTP)
        self.assertEqual(sent["server"], ("smtp.gmail.com", 465))
        self.assertEqual(sent["login"], ("daycare-inbox@example.com", "abcdefghijklmnop"))
        self.assertEqual(sent["message"]["To"], "parent@example.com")
        self.assertEqual(sent["message"]["Subject"], "Subject here")


if __name__ == "__main__":
    unittest.main()
