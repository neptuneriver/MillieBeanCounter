"""Optional: mirror the workbook into a Google Sheet.

Uses a Google Cloud service account (a robot login with its own email address).
You share the Google Sheet with that address as an Editor, and the sync rewrites
its own tabs after every export. Tabs you add yourself are never touched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .export import OLD_TAB_NAMES, Workbook

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
CELLS_PER_REQUEST = 40_000
KEY_FILE = "google-service-account.json"


class SheetsError(Exception):
    """Google Sheets problem. The message is written for a person."""


def spreadsheet_id(value: str) -> str:
    """Accepts the sheet's full link or just its ID."""
    value = value.strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", value)
    if match:
        return match[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise SheetsError("That doesn't look like a Google Sheets link (it should contain /spreadsheets/d/...).")


def key_path(data_dir: Path) -> Path:
    return data_dir / KEY_FILE


def save_key(data_dir: Path, text: str) -> str:
    """Store the service account key (JSON) in the data volume. Returns its email address."""
    try:
        info = json.loads(text)
    except ValueError as exc:
        raise SheetsError("That file isn't a Google service account key (it should be a .json file).") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SheetsError("That JSON file isn't a service account key. Download a key of type JSON for the account.")
    for needed in ("client_email", "private_key", "token_uri"):
        if not info.get(needed):
            raise SheetsError(f"The key file is missing {needed!r}. Download a fresh JSON key.")
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=data_dir, prefix=".key-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(info, handle)
        os.chmod(temp, 0o600)
        os.replace(temp, key_path(data_dir))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return info["client_email"]


def load_key(data_dir: Path) -> dict | None:
    path = key_path(data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SheetsError("The saved Google key couldn't be read. Run Setup.command and add the key again.") from exc


def quoted(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _column_letter(number: int) -> str:
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


class GoogleSheet:
    def __init__(self, sheet_id: str, session, robot_email: str = ""):
        self.sheet_id = sheet_id
        self.session = session
        self.robot_email = robot_email

    @classmethod
    def connect(cls, sheet_link: str, key: dict) -> GoogleSheet:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        try:
            credentials = service_account.Credentials.from_service_account_info(key, scopes=SCOPES)
        except (ValueError, KeyError) as exc:
            raise SheetsError(f"The Google key file couldn't be used ({exc}). Download a new JSON key.") from exc
        return cls(spreadsheet_id(sheet_link), AuthorizedSession(credentials), key.get("client_email", ""))

    # -- HTTP --------------------------------------------------------------
    def _call(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = getattr(self.session, method)(url, timeout=120, **kwargs)
        except Exception as exc:  # network trouble, bad key, token refresh failure
            raise SheetsError(f"Couldn't reach Google Sheets ({exc.__class__.__name__}: {exc}).") from exc
        if response.status_code >= 400:
            raise SheetsError(self._explain(response))
        return response.json() if response.content else {}

    def _explain(self, response) -> str:
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = response.text[:300]
        if response.status_code == 404:
            return "The Google Sheet wasn't found. Check the link in Setup.command."
        if response.status_code == 403 and ("has not been used" in message or "disabled" in message):
            return "The Google Sheets API is turned off for the Google Cloud project. Turn it on (see README.md)."
        if response.status_code == 403:
            who = self.robot_email or "the service account"
            return f"The Google Sheet isn't shared with {who}. In the sheet, click Share and add it as an Editor."
        return f"Google Sheets said HTTP {response.status_code}: {message}"

    # -- operations ----------------------------------------------------------
    def title(self) -> str:
        meta = self._call("get", f"{API}/{self.sheet_id}", params={"fields": "properties.title"})
        return meta.get("properties", {}).get("title", "")

    def push(self, workbook: Workbook) -> int:
        """Rewrite the sync's tabs with the workbook. Returns the number of cells written."""
        meta = self._call(
            "get", f"{API}/{self.sheet_id}", params={"fields": "sheets.properties(sheetId,title,gridProperties)"}
        )
        tabs = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
        missing = [sheet for sheet in workbook.sheets if sheet.name not in tabs]
        if missing:
            reply = self._call(
                "post",
                f"{API}/{self.sheet_id}:batchUpdate",
                json={"requests": [{"addSheet": {"properties": {"title": s.name}}} for s in missing]},
            )
            for answer in reply.get("replies", []):
                properties = answer.get("addSheet", {}).get("properties", {})
                if properties:
                    tabs[properties["title"]] = properties
        # Tabs this sync wrote under older names are replaced by the new ones above.
        requests: list[dict] = [
            {"deleteSheet": {"sheetId": tabs.pop(name)["sheetId"]}} for name in OLD_TAB_NAMES if name in tabs
        ]
        for sheet in workbook.sheets:
            tab_id = tabs[sheet.name]["sheetId"]
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": tab_id,
                            "gridProperties": {
                                "rowCount": max(len(sheet.rows) + 1, 2),
                                "columnCount": max(len(sheet.columns), 1),
                                "frozenRowCount": 1,
                            },
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount",
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
            )
        self._call("post", f"{API}/{self.sheet_id}:batchUpdate", json={"requests": requests})
        self._call(
            "post",
            f"{API}/{self.sheet_id}/values:batchClear",
            json={"ranges": [quoted(sheet.name) for sheet in workbook.sheets]},
        )
        written = 0
        batch: list[dict] = []
        batch_cells = 0

        def send() -> None:
            nonlocal batch, batch_cells
            if batch:
                self._call(
                    "post",
                    f"{API}/{self.sheet_id}/values:batchUpdate",
                    json={"valueInputOption": "RAW", "data": batch},
                )
            batch, batch_cells = [], 0

        for sheet in workbook.sheets:
            values = [sheet.columns] + [["" if v is None else v for v in row] for row in sheet.rows]
            width = max(len(sheet.columns), 1)
            rows_per_chunk = max(1, CELLS_PER_REQUEST // width)
            for first in range(0, len(values), rows_per_chunk):
                chunk = values[first : first + rows_per_chunk]
                last_column = _column_letter(width)
                cell_range = f"{quoted(sheet.name)}!A{first + 1}:{last_column}{first + len(chunk)}"
                if batch_cells + len(chunk) * width > CELLS_PER_REQUEST:
                    send()
                batch.append({"range": cell_range, "values": chunk})
                batch_cells += len(chunk) * width
                written += len(chunk) * width
        send()
        return written
