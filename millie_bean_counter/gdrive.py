"""Optional: save the daycare's photos into Google Drive.

Google Drive only takes uploads from a real Google account (service accounts
have no storage of their own), so this signs in as you, once, through an OAuth
client of type "Desktop app" that you create in Google Cloud (README.md has the
steps). It asks only for the drive.file permission: this app can see and change
the files and folders it made itself, and nothing else in your Drive.

The sign-in leaves a refresh token in the Docker data volume, never in the
project folder. Everything else is plain HTTPS calls to Google with requests.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_TYPE = "application/vnd.google-apps.folder"
CLIENT_FILE = "google-oauth-client.json"
TOKEN_FILE = "google-drive-token.json"
LOGIN_PORT = 8737
TAG_KEY = "mbc_photo"  # private label on every uploaded photo, so a retried upload can find it
SIGN_IN_AGAIN = "Run Setup.command again and sign in to Google Drive (step 5)."

DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>MillieBeanCounter</title>
<body style="font-family:-apple-system,system-ui,sans-serif;margin:3em auto;max-width:32em;line-height:1.5">
<h2>Signed in to Google Drive</h2><p>You can close this tab and go back to the Terminal window.</p></body>"""
FAILED_PAGE = """<!doctype html><meta charset="utf-8"><title>MillieBeanCounter</title>
<body style="font-family:-apple-system,system-ui,sans-serif;margin:3em auto;max-width:32em;line-height:1.5">
<h2>Not signed in</h2><p>{}</p><p>Go back to the Terminal window.</p></body>"""
WAITING_PAGE = """<!doctype html><meta charset="utf-8"><title>MillieBeanCounter</title>
<body style="font-family:-apple-system,system-ui,sans-serif;margin:3em auto;max-width:32em;line-height:1.5">
<p>Waiting for the Google sign-in. Use the link shown in the Terminal window.</p></body>"""


class DriveError(Exception):
    """Google Drive problem. The message is written for a person."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Saved files: the OAuth client and the sign-in
# ---------------------------------------------------------------------------


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".drive-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path: Path, what: str) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DriveError(f"The saved {what} couldn't be read. {SIGN_IN_AGAIN}") from exc
    return data if isinstance(data, dict) else None


def save_client(data_dir: Path, text: str) -> str:
    """Keep the OAuth client (the .json file from Google Cloud). Returns its client ID.
    A new client means a new sign-in, so any saved sign-in is dropped."""
    try:
        info = json.loads(text)
    except ValueError as exc:
        raise DriveError("That file isn't a Google OAuth client (it should be a .json file).") from exc
    if not isinstance(info, dict):
        raise DriveError("That file isn't a Google OAuth client.")
    if info.get("type") == "service_account":
        raise DriveError(
            "That's a service account key (the one for the Google Sheet copy). For photos, use the OAuth "
            'client of type "Desktop app" (see README.md).'
        )
    if "web" in info:
        raise DriveError('That OAuth client is a "Web application". Create one of type "Desktop app" (see README.md).')
    client = info.get("installed")
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise DriveError('That .json file isn\'t an OAuth client of type "Desktop app" (see README.md).')
    _write_private(data_dir / CLIENT_FILE, {"client_id": client["client_id"], "client_secret": client["client_secret"]})
    (data_dir / TOKEN_FILE).unlink(missing_ok=True)
    return client["client_id"]


def load_client(data_dir: Path) -> dict | None:
    client = _read(data_dir / CLIENT_FILE, "Google OAuth client")
    if client is not None and not (client.get("client_id") and client.get("client_secret")):
        raise DriveError(f"The saved Google OAuth client is incomplete. {SIGN_IN_AGAIN}")
    return client


def save_sign_in(data_dir: Path, refresh_token: str, account: str) -> None:
    _write_private(
        data_dir / TOKEN_FILE,
        {
            "refresh_token": refresh_token,
            "account": account,
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def load_sign_in(data_dir: Path) -> dict | None:
    token = _read(data_dir / TOKEN_FILE, "Google Drive sign-in")
    if token is not None and not token.get("refresh_token"):
        return None
    return token


# ---------------------------------------------------------------------------
# Signing in (once, from setup)
# ---------------------------------------------------------------------------


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _token_problem(payload: dict) -> str:
    error = payload.get("error", "")
    if error == "invalid_grant":
        return (
            "Google Drive's sign-in has expired or was removed. " + SIGN_IN_AGAIN + " If this keeps happening "
            "about a week after each sign-in, the app in Google Cloud is still in Testing: publish it "
            "(README.md, Google Drive step 4)."
        )
    if error in ("invalid_client", "unauthorized_client", "deleted_client"):
        return "Google doesn't recognize the saved OAuth client (was it deleted?). " + SIGN_IN_AGAIN
    detail = payload.get("error_description") or error or "no details"
    return f"Google sign-in failed ({detail})."


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class Login:
    """One sign-in: make the link, wait for Google to send the browser back,
    then trade the code it brings for a long-lived refresh token."""

    def __init__(self, client: dict, *, port: int = LOGIN_PORT, login_hint: str = ""):
        self.client = client
        self.port = port
        self.redirect_uri = f"http://127.0.0.1:{port}/"
        self.verifier = secrets.token_urlsafe(48)
        self.state = secrets.token_urlsafe(16)
        self.login_hint = login_hint

    def url(self) -> str:
        params = {
            "client_id": self.client["client_id"],
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "code_challenge": _challenge(self.verifier),
            "code_challenge_method": "S256",
            "state": self.state,
        }
        if self.login_hint:
            params["login_hint"] = self.login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def code_from(self, address: str) -> str | None:
        """The sign-in code in the address Google sent the browser back to.
        None if the address has nothing to do with this sign-in yet."""
        query = parse_qs(urlsplit(address.strip()).query)
        if not query:
            return None
        if query.get("state", [""])[0] != self.state:
            raise DriveError("That address is from a different sign-in attempt. Start the Google Drive sign-in again.")
        if "error" in query:
            if query["error"][0] == "access_denied":
                raise DriveError("Google Drive access wasn't allowed, so photos can't be saved there.")
            raise DriveError(f"Google said: {query['error'][0]}.")
        code = query.get("code", [""])[0]
        if not code:
            raise DriveError("Google didn't send a sign-in code back.")
        return code

    def wait(self, *, timeout: float = 600, bind: str = "0.0.0.0", typed=None) -> str:
        """Serve one tiny page on the login port until Google sends the browser back.

        If the browser can't reach this computer (say, setup runs over SSH), the
        address it was sent to can be pasted into the terminal instead; `typed`
        is where those lines come from (standard input by default)."""
        login = self
        outcome: dict = {}
        done = threading.Event()

        def settle(address: str) -> bool:
            try:
                code = login.code_from(address)
            except DriveError as exc:
                outcome.setdefault("error", exc)
                done.set()
                return True
            if code:
                outcome.setdefault("code", code)
                done.set()
                return True
            return False

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # the name http.server expects
                if urlsplit(self.path).path != "/":
                    self.send_error(404)
                    return
                finished = settle(self.path)
                if not finished:
                    page = WAITING_PAGE
                elif "error" in outcome:
                    page = FAILED_PAGE.format(html.escape(str(outcome["error"])))
                else:
                    page = DONE_PAGE
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep the terminal quiet
                pass

        try:
            server = http.server.ThreadingHTTPServer((bind, self.port), Handler)
        except OSError as exc:
            raise DriveError(f"Couldn't wait for the sign-in on port {self.port} ({exc}).") from exc
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

        def read_typed():
            for line in typed if typed is not None else sys.stdin:
                if done.is_set():
                    return
                if line.strip() and not settle(line):
                    print("   That doesn't look like the address Google sent the browser to. Try again.", flush=True)

        threading.Thread(target=read_typed, daemon=True).start()
        try:
            if not done.wait(timeout):
                raise DriveError("The Google sign-in wasn't finished in time.")
        finally:
            server.shutdown()
            server.server_close()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["code"]

    def finish(self, code: str, session) -> str:
        """Trade the code for a refresh token."""
        try:
            response = session.post(
                TOKEN_URL,
                data={
                    "client_id": self.client["client_id"],
                    "client_secret": self.client["client_secret"],
                    "code": code,
                    "code_verifier": self.verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
                timeout=60,
            )
        except Exception as exc:  # network trouble
            raise DriveError(f"Couldn't reach Google ({exc.__class__.__name__}: {exc}).") from exc
        payload = _json(response)
        if response.status_code >= 400:
            raise DriveError(_token_problem(payload), response.status_code)
        if not payload.get("refresh_token"):
            raise DriveError(
                "Google signed in but didn't allow a lasting sign-in. Remove MillieBeanCounter at "
                "myaccount.google.com/permissions and try again."
            )
        return payload["refresh_token"]


# ---------------------------------------------------------------------------
# Using Drive
# ---------------------------------------------------------------------------


def _quote(value: str) -> str:
    """A string inside a Drive search query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def drive_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Drive:
    def __init__(self, client: dict, refresh_token: str, session=None, *, clock=time.monotonic):
        if session is None:
            import requests

            session = requests.Session()
        self.client = client
        self.refresh_token = refresh_token
        self.session = session
        self.clock = clock
        self._token = ""
        self._expires = 0.0

    @classmethod
    def from_saved(cls, data_dir: Path, session=None) -> Drive:
        client = load_client(data_dir)
        if client is None:
            raise DriveError(f"No Google OAuth client is saved yet. {SIGN_IN_AGAIN}")
        sign_in = load_sign_in(data_dir)
        if sign_in is None:
            raise DriveError(f"Google Drive isn't signed in yet. {SIGN_IN_AGAIN}")
        return cls(client, sign_in["refresh_token"], session)

    # -- HTTP --------------------------------------------------------------
    def _access_token(self, *, fresh: bool = False) -> str:
        if self._token and not fresh and self.clock() < self._expires:
            return self._token
        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "client_id": self.client["client_id"],
                    "client_secret": self.client["client_secret"],
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=60,
            )
        except Exception as exc:  # network trouble
            raise DriveError(f"Couldn't reach Google ({exc.__class__.__name__}: {exc}).") from exc
        payload = _json(response)
        if response.status_code >= 400 or not payload.get("access_token"):
            raise DriveError(_token_problem(payload), response.status_code)
        self._token = payload["access_token"]
        self._expires = self.clock() + max(60, int(payload.get("expires_in", 3600)) - 120)
        return self._token

    def _call(self, method: str, url: str, *, headers: dict | None = None, **kwargs):
        for attempt in (1, 2):
            sent = {**(headers or {}), "Authorization": f"Bearer {self._access_token(fresh=attempt == 2)}"}
            try:
                response = getattr(self.session, method)(url, headers=sent, timeout=300, **kwargs)
            except Exception as exc:  # network trouble
                raise DriveError(f"Couldn't reach Google Drive ({exc.__class__.__name__}: {exc}).") from exc
            if response.status_code != 401:
                break
        if response.status_code >= 400:
            raise DriveError(self._explain(response), response.status_code)
        return response

    @staticmethod
    def _explain(response) -> str:
        error = _json(response).get("error", {})
        error = error if isinstance(error, dict) else {}
        message = error.get("message", "") or getattr(response, "text", "")[:300]
        reasons = {e.get("reason", "") for e in error.get("errors", []) if isinstance(e, dict)}
        status = response.status_code
        if status == 403 and (reasons & {"accessNotConfigured", "SERVICE_DISABLED"} or "has not been used" in message):
            return "The Google Drive API is turned off in your Google Cloud project. Turn it on (README.md, Google Drive step 2)."
        if reasons & {"storageQuotaExceeded"}:
            return "Google Drive is full, so photos can't be saved there until some space is freed."
        if status == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
            return "Google Drive asked to slow down. The photos will be saved at the next check."
        if status == 401:
            return "Google Drive didn't accept the sign-in. " + SIGN_IN_AGAIN
        return f"Google Drive said HTTP {status}: {message}"

    # -- operations ----------------------------------------------------------
    def account(self) -> str:
        """The email address of the signed-in Google account."""
        reply = self._call("get", f"{API}/about", params={"fields": "user(emailAddress)"})
        return _json(reply).get("user", {}).get("emailAddress", "")

    def find_folder(self, name: str, parent: str) -> str | None:
        query = (
            f"name = '{_quote(name)}' and mimeType = '{FOLDER_TYPE}' and '{_quote(parent)}' in parents "
            "and trashed = false"
        )
        reply = self._call(
            "get", f"{API}/files", params={"q": query, "fields": "files(id)", "spaces": "drive", "pageSize": 10}
        )
        files = _json(reply).get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent: str) -> str:
        reply = self._call(
            "post",
            f"{API}/files",
            params={"fields": "id"},
            json={"name": name, "mimeType": FOLDER_TYPE, "parents": [parent]},
        )
        return _json(reply)["id"]

    def usable(self, file_id: str) -> bool:
        """Is this file or folder still there (not deleted, not in the trash)?"""
        try:
            reply = self._call("get", f"{API}/files/{file_id}", params={"fields": "id,trashed"})
        except DriveError as exc:
            if exc.status == 404:
                return False
            raise
        return not _json(reply).get("trashed", False)

    def find_tagged(self, tag: str) -> str | None:
        """A photo uploaded earlier under this private label (from an upload that
        finished on Google's side after this computer stopped waiting)."""
        query = f"appProperties has {{ key='{TAG_KEY}' and value='{_quote(tag)}' }} and trashed = false"
        reply = self._call("get", f"{API}/files", params={"q": query, "fields": "files(id)", "spaces": "drive"})
        files = _json(reply).get("files", [])
        return files[0]["id"] if files else None

    def upload(
        self,
        *,
        name: str,
        data: bytes,
        mime: str,
        parent: str,
        description: str = "",
        modified: datetime | None = None,
        tag: str = "",
    ) -> str:
        """Upload one file (resumable upload: one call to start, one with the bytes). Returns its ID."""
        meta: dict = {"name": name, "parents": [parent]}
        if description:
            meta["description"] = description
        if modified is not None:
            meta["modifiedTime"] = drive_time(modified)
        if tag:
            meta["appProperties"] = {TAG_KEY: tag}
        start = self._call(
            "post",
            UPLOAD_URL,
            params={"uploadType": "resumable", "fields": "id"},
            json=meta,
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(data))},
        )
        location = start.headers.get("Location") or start.headers.get("location")
        if not location:
            raise DriveError("Google Drive didn't accept the upload (it gave no upload address).")
        done = self._call("put", location, data=data, headers={"Content-Type": mime})
        file_id = _json(done).get("id")
        if not file_id:
            raise DriveError("Google Drive didn't confirm the upload.")
        return file_id
