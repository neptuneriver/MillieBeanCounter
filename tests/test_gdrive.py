"""Google Drive: the saved client and sign-in, the sign-in flow, and the REST calls (against fakes)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from millie_bean_counter import gdrive
from millie_bean_counter.gdrive import Drive, DriveError, Login

CLIENT = {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-not-real"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Reply:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    """Answers calls in order from a queue, and records them."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, **kwargs):
        return self._answer("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._answer("put", url, **kwargs)


def token(value="access-1", expires=3600):
    return Reply(200, {"access_token": value, "expires_in": expires, "token_type": "Bearer"})


class SavedFilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_desktop_client_is_kept_and_a_new_one_drops_the_old_sign_in(self):
        gdrive.save_sign_in(self.data, "refresh-1", "inbox@example.com")
        desktop = {"installed": {**CLIENT, "project_id": "p", "redirect_uris": ["http://localhost"]}}
        self.assertEqual(gdrive.save_client(self.data, json.dumps(desktop)), CLIENT["client_id"])
        self.assertEqual(gdrive.load_client(self.data), CLIENT)
        self.assertIsNone(gdrive.load_sign_in(self.data))
        self.assertEqual((self.data / gdrive.CLIENT_FILE).stat().st_mode & 0o777, 0o600)

    def test_wrong_files_are_explained(self):
        cases = {
            "not json": "isn't a Google OAuth client",
            json.dumps({"web": CLIENT}): "Web application",
            json.dumps({"type": "service_account", "client_email": "x"}): "service account key",
            json.dumps({"installed": {"client_id": "x"}}): "Desktop app",
            json.dumps([1, 2]): "isn't a Google OAuth client",
        }
        for text, words in cases.items():
            with self.assertRaises(DriveError) as caught:
                gdrive.save_client(self.data, text)
            self.assertIn(words, str(caught.exception))

    def test_sign_in_round_trip(self):
        gdrive.save_sign_in(self.data, "refresh-1", "inbox@example.com")
        saved = gdrive.load_sign_in(self.data)
        self.assertEqual((saved["refresh_token"], saved["account"]), ("refresh-1", "inbox@example.com"))

    def test_missing_pieces(self):
        with self.assertRaises(DriveError) as caught:
            Drive.from_saved(self.data, Session())
        self.assertIn("No Google OAuth client", str(caught.exception))
        gdrive.save_client(self.data, json.dumps({"installed": CLIENT}))
        with self.assertRaises(DriveError) as caught:
            Drive.from_saved(self.data, Session())
        self.assertIn("isn't signed in", str(caught.exception))
        (self.data / gdrive.TOKEN_FILE).write_text("{broken")
        with self.assertRaises(DriveError):
            Drive.from_saved(self.data, Session())


class LoginTest(unittest.TestCase):
    def test_the_link(self):
        login = Login(CLIENT, login_hint="inbox@example.com")
        query = {k: v[0] for k, v in parse_qs(urlsplit(login.url()).query).items()}
        self.assertTrue(login.url().startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual(query["scope"], "https://www.googleapis.com/auth/drive.file")  # nothing broader
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:8737/")
        self.assertEqual((query["access_type"], query["prompt"]), ("offline", "consent"))
        self.assertEqual(query["login_hint"], "inbox@example.com")
        self.assertEqual(query["code_challenge_method"], "S256")
        expected = base64.urlsafe_b64encode(hashlib.sha256(login.verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], expected)
        self.assertEqual(query["state"], login.state)

    def test_reading_the_address_google_sends_back(self):
        login = Login(CLIENT)
        self.assertEqual(login.code_from(f"/?state={login.state}&code=4/abc&scope=x"), "4/abc")
        self.assertIsNone(login.code_from("/"))
        with self.assertRaises(DriveError):
            login.code_from("/?state=someone-else&code=4/abc")
        with self.assertRaises(DriveError) as caught:
            login.code_from(f"/?state={login.state}&error=access_denied")
        self.assertIn("wasn't allowed", str(caught.exception))

    def _visit(self, port: int, path: str) -> str:
        for _ in range(50):  # the server starts in a moment
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", path)
                body = connection.getresponse().read().decode()
                connection.close()
                return body
            except ConnectionRefusedError:
                time.sleep(0.05)
        raise AssertionError("the sign-in page never came up")

    def test_the_browser_comes_back_with_the_code(self):
        port = free_port()
        login = Login(CLIENT, port=port)
        pages: list[str] = []

        def browser():
            pages.append(self._visit(port, "/"))  # an early visit just waits
            pages.append(self._visit(port, f"/?state={login.state}&code=4/xyz"))

        thread = threading.Thread(target=browser)
        thread.start()
        code = login.wait(timeout=10, bind="127.0.0.1", typed=[])
        thread.join()
        self.assertEqual(code, "4/xyz")
        self.assertIn("Waiting for the Google sign-in", pages[0])
        self.assertIn("Signed in to Google Drive", pages[1])

    def test_the_address_can_be_pasted_instead(self):
        login = Login(CLIENT, port=free_port())
        typed = ["nonsense\n", f"http://127.0.0.1:{login.port}/?state={login.state}&code=4/typed&scope=x\n"]
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(login.wait(timeout=10, bind="127.0.0.1", typed=typed), "4/typed")
        self.assertIn("doesn't look like the address", said.getvalue())

    def test_refused_or_too_slow(self):
        port = free_port()
        login = Login(CLIENT, port=port)
        thread = threading.Thread(target=self._visit, args=(port, f"/?state={login.state}&error=access_denied"))
        thread.start()
        with self.assertRaises(DriveError):
            login.wait(timeout=10, bind="127.0.0.1", typed=[])
        thread.join()
        with self.assertRaises(DriveError) as caught:
            Login(CLIENT, port=free_port()).wait(timeout=0.3, bind="127.0.0.1", typed=[])
        self.assertIn("in time", str(caught.exception))

    def test_trading_the_code(self):
        login = Login(CLIENT)
        session = Session(Reply(200, {"access_token": "a", "refresh_token": "r-1", "expires_in": 3599}))
        self.assertEqual(login.finish("4/xyz", session), "r-1")
        _, url, sent = session.calls[0]
        self.assertEqual(url, gdrive.TOKEN_URL)
        self.assertEqual(sent["data"]["code_verifier"], login.verifier)
        self.assertEqual(sent["data"]["redirect_uri"], "http://127.0.0.1:8737/")
        with self.assertRaises(DriveError) as caught:
            login.finish("4/xyz", Session(Reply(200, {"access_token": "a"})))
        self.assertIn("lasting sign-in", str(caught.exception))
        with self.assertRaises(DriveError) as caught:
            login.finish("4/xyz", Session(Reply(400, {"error": "invalid_grant"})))
        self.assertIn("expired", str(caught.exception))


class DriveTest(unittest.TestCase):
    def drive(self, *replies, clock=None):
        session = Session(*replies)
        drive = Drive(CLIENT, "refresh-1", session, clock=clock or (lambda: 1000.0))
        return drive, session

    def test_access_token_is_reused_until_it_expires(self):
        now = [1000.0]
        drive, session = self.drive(
            token("a1"),
            Reply(200, {"user": {"emailAddress": "inbox@example.com"}}),
            Reply(200, {"user": {"emailAddress": "inbox@example.com"}}),
            token("a2"),
            Reply(200, {"user": {"emailAddress": "inbox@example.com"}}),
            clock=lambda: now[0],
        )
        self.assertEqual(drive.account(), "inbox@example.com")
        self.assertEqual(drive.account(), "inbox@example.com")
        now[0] += 3600
        drive.account()
        tokens = [c for c in session.calls if c[1] == gdrive.TOKEN_URL]
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0][2]["data"]["grant_type"], "refresh_token")
        self.assertEqual(session.calls[-1][2]["headers"]["Authorization"], "Bearer a2")

    def test_a_rejected_token_is_refreshed_once(self):
        drive, session = self.drive(
            token("old"),
            Reply(401, {"error": {"message": "Invalid Credentials"}}),
            token("new"),
            Reply(200, {"user": {"emailAddress": "x@example.com"}}),
        )
        self.assertEqual(drive.account(), "x@example.com")
        self.assertEqual(session.calls[-1][2]["headers"]["Authorization"], "Bearer new")

    def test_problems_in_plain_words(self):
        cases = [
            (Reply(400, {"error": "invalid_grant"}), "expired or was removed"),
            (Reply(401, {"error": "invalid_client"}), "OAuth client"),
        ]
        for reply, words in cases:
            drive, _ = self.drive(reply)
            with self.assertRaises(DriveError) as caught:
                drive.account()
            self.assertIn(words, str(caught.exception))
        api_off = {
            "error": {
                "message": "Google Drive API has not been used in project 1 before or it is disabled.",
                "errors": [{"reason": "accessNotConfigured"}],
            }
        }
        full = {
            "error": {
                "message": "The user's Drive storage quota has been exceeded.",
                "errors": [{"reason": "storageQuotaExceeded"}],
            }
        }
        for body, words in ((api_off, "turned off"), (full, "Google Drive is full")):
            drive, _ = self.drive(token(), Reply(403, body))
            with self.assertRaises(DriveError) as caught:
                drive.account()
            self.assertIn(words, str(caught.exception))
        drive, _ = self.drive(ConnectionError("no network"))
        with self.assertRaises(DriveError) as caught:
            drive.account()
        self.assertIn("Couldn't reach Google", str(caught.exception))

    def test_folders(self):
        drive, session = self.drive(
            token(),
            Reply(200, {"files": []}),
            Reply(200, {"id": "folder-1"}),
            Reply(200, {"files": [{"id": "folder-2"}]}),
        )
        self.assertIsNone(drive.find_folder("Josie's Daycare Photos", "root"))
        query = session.calls[1][2]["params"]["q"]
        self.assertIn("name = 'Josie\\'s Daycare Photos'", query)
        self.assertIn("'root' in parents and trashed = false", query)
        self.assertIn("mimeType = 'application/vnd.google-apps.folder'", query)
        self.assertEqual(drive.create_folder("October 2026", "parent-1"), "folder-1")
        self.assertEqual(
            session.calls[2][2]["json"],
            {"name": "October 2026", "mimeType": "application/vnd.google-apps.folder", "parents": ["parent-1"]},
        )
        self.assertEqual(drive.find_folder("October 2026", "parent-1"), "folder-2")

    def test_is_it_still_there(self):
        drive, _ = self.drive(
            token(),
            Reply(200, {"id": "f", "trashed": False}),
            Reply(200, {"id": "f", "trashed": True}),
            Reply(404, {"error": {"message": "File not found"}}),
        )
        self.assertEqual([drive.usable("f"), drive.usable("f"), drive.usable("f")], [True, False, False])

    def test_upload(self):
        drive, session = self.drive(
            token(),
            Reply(200, {}, headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=u1"}),
            Reply(200, {"id": "file-9"}),
        )
        when = datetime(2026, 10, 14, 19, 22, 8, tzinfo=timezone.utc)
        file_id = drive.upload(
            name="10/14 Josie 4m 11d Photo 1.jpg",
            data=b"\xff\xd8 picture",
            mime="image/jpeg",
            parent="month-1",
            description="From the daycare",
            modified=when,
            tag="t-1",
        )
        self.assertEqual(file_id, "file-9")
        method, url, start = session.calls[1]
        self.assertEqual((method, url), ("post", gdrive.UPLOAD_URL))
        self.assertEqual(start["params"], {"uploadType": "resumable", "fields": "id"})
        self.assertEqual(
            start["json"],
            {
                "name": "10/14 Josie 4m 11d Photo 1.jpg",
                "parents": ["month-1"],
                "description": "From the daycare",
                "modifiedTime": "2026-10-14T19:22:08Z",
                "appProperties": {"mbc_photo": "t-1"},
            },
        )
        self.assertEqual(start["headers"]["X-Upload-Content-Type"], "image/jpeg")
        self.assertEqual(start["headers"]["X-Upload-Content-Length"], str(len(b"\xff\xd8 picture")))
        method, url, bytes_sent = session.calls[2]
        self.assertEqual((method, url), ("put", "https://www.googleapis.com/upload/drive/v3/files?upload_id=u1"))
        self.assertEqual(bytes_sent["data"], b"\xff\xd8 picture")

    def test_find_by_private_label(self):
        drive, session = self.drive(token(), Reply(200, {"files": [{"id": "file-3"}]}))
        self.assertEqual(drive.find_tagged("t-1"), "file-3")
        self.assertIn("appProperties has { key='mbc_photo' and value='t-1' }", session.calls[1][2]["params"]["q"])


if __name__ == "__main__":
    unittest.main()
