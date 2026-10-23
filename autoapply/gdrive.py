"""Copy what was sent for each application into a Google Drive folder.

Uses the drive.file scope: the app can only see and change files and folders it created
itself, never the rest of your Drive.

    Job Applications/
      Stripe – Solutions Engineer (2026-10-21)/
        Jane_Doe_Resume.pdf
        Cover_Letter.pdf
        Application summary.txt
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink"
FOLDER = "application/vnd.google-apps.folder"
SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveError(RuntimeError):
    pass


def credentials() -> tuple[str, str, str] | None:
    vals = tuple(os.environ.get(k, "") for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"))
    return vals if all(vals) else None  # type: ignore[return-value]


def configured() -> bool:
    return credentials() is not None


class Drive:
    def __init__(self) -> None:
        creds = credentials()
        if creds is None:
            raise DriveError("Google Drive isn't connected (GDRIVE_* secrets not set)")
        client_id, client_secret, refresh = creds
        resp = httpx.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                           "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30)
        if resp.status_code != 200:
            raise DriveError(f"Google token refresh failed ({resp.status_code}): {resp.text[:200]}")
        self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {resp.json()['access_token']}"})

    def _find_folder(self, name: str, parent: str | None) -> dict | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name = '{safe}' and mimeType = '{FOLDER}' and trashed = false"
        q += f" and '{parent}' in parents" if parent else " and 'root' in parents"
        resp = self.http.get(FILES, params={"q": q, "fields": "files(id,webViewLink)", "spaces": "drive"})
        if resp.status_code != 200:
            raise DriveError(f"Drive search failed ({resp.status_code}): {resp.text[:200]}")
        files = resp.json().get("files", [])
        return files[0] if files else None

    def folder(self, name: str, parent: str | None = None) -> dict:
        """Find or create a folder; returns {id, webViewLink}."""
        existing = self._find_folder(name, parent)
        if existing:
            return existing
        body = {"name": name, "mimeType": FOLDER, **({"parents": [parent]} if parent else {})}
        resp = self.http.post(FILES, params={"fields": "id,webViewLink"}, json=body)
        if resp.status_code != 200:
            raise DriveError(f"Couldn't create Drive folder ({resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def children(self, parent: str) -> list[dict]:
        resp = self.http.get(FILES, params={"q": f"'{parent}' in parents and trashed = false",
                                            "fields": "files(id,name,createdTime)", "orderBy": "createdTime desc",
                                            "pageSize": 200})
        if resp.status_code != 200:
            raise DriveError(f"Drive listing failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json().get("files", [])

    def delete(self, file_id: str) -> None:
        resp = self.http.delete(f"{FILES}/{file_id}")
        if resp.status_code not in (200, 204, 404):
            raise DriveError(f"Drive delete failed ({resp.status_code}): {resp.text[:200]}")

    def upload(self, path: Path, parent: str, mime: str, name: str | None = None) -> dict:
        boundary = "autoapply-boundary"
        meta = json.dumps({"name": name or path.name, "parents": [parent]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--".encode()
        resp = self.http.post(UPLOAD, content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        if resp.status_code != 200:
            raise DriveError(f"Upload of {path.name} failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json()


def _summary(job: dict, answers: list[dict], applied_on: str) -> str:
    lines = [f"{job['title']} at {job['company']}", f"Applied: {applied_on}", f"Posting: {job.get('url') or job['apply_url']}",
             f"Application form: {job['apply_url']}", "", "Answers submitted:", ""]
    for a in answers:
        if a.get("source") == "unanswered":
            continue
        lines += [f"Q: {a['label']}", f"A: {a['value']}", ""]
    return "\n".join(lines)


def copy_application(job: dict, files_dir: Path, artifacts: dict, answers: list[dict], applied_on: str,
                     root_name: str = "Job Applications") -> str:
    """Upload one application's materials; returns the Drive folder link."""
    drive = Drive()
    root = drive.folder(root_name)
    title = f"{job['company']} – {job['title']} ({applied_on})".replace("/", "-")[:180]
    folder = drive.folder(title, root["id"])
    if artifacts.get("resume") and (files_dir / artifacts["resume"]).exists():
        drive.upload(files_dir / artifacts["resume"], folder["id"], "application/pdf")
    if artifacts.get("cover_letter") and (files_dir / artifacts["cover_letter"]).exists():
        drive.upload(files_dir / artifacts["cover_letter"], folder["id"], "application/pdf")
    summary = files_dir / "Application summary.txt"
    summary.write_text(_summary(job, answers, applied_on))
    drive.upload(summary, folder["id"], "text/plain")
    return folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder['id']}"


def backup(db_path: Path, profile_yaml: str, keep: int = 14, root_name: str = "Job Applications") -> str:
    """Upload a consistent copy of the database plus the current profile; keep the newest `keep` of each."""
    import sqlite3
    import tempfile
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    drive = Drive()
    folder = drive.folder("_backups", drive.folder(root_name)["id"])
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / f"autoapply-{stamp}.db"
        src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
        with dst:
            src.backup(dst)  # safe while the app is writing
        src.close()
        dst.close()
        drive.upload(copy, folder["id"], "application/x-sqlite3")
        prof = Path(tmp) / f"profile-{stamp}.yaml"
        prof.write_text(profile_yaml)
        drive.upload(prof, folder["id"], "text/yaml")
    for prefix in ("autoapply-", "profile-"):
        files = [f for f in drive.children(folder["id"]) if f["name"].startswith(prefix)]
        for old in files[keep:]:
            drive.delete(old["id"])
    return folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder['id']}"
