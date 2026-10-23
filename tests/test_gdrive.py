import json

import httpx

from autoapply import gdrive


def test_copy_application_creates_folders_and_uploads(monkeypatch, tmp_path):
    monkeypatch.setenv("GDRIVE_CLIENT_ID", "id")
    monkeypatch.setenv("GDRIVE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GDRIVE_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(gdrive.httpx, "post", lambda *a, **k: httpx.Response(200, json={"access_token": "t"}))
    calls, folders = [], {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":  # folder search: nothing exists yet
            return httpx.Response(200, json={"files": []})
        if "upload" in request.url.path:
            assert b"multipart" not in request.content[:2]  # body starts with the boundary
            return httpx.Response(200, json={"id": f"file{len(calls)}"})
        body = json.loads(request.content)
        folders[body["name"]] = body.get("parents")
        return httpx.Response(200, json={"id": f"folder{len(folders)}", "webViewLink": f"https://drive/{len(folders)}"})

    real_client = httpx.Client
    monkeypatch.setattr(gdrive.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    (tmp_path / "Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Cover_Letter.pdf").write_bytes(b"%PDF-1.4 letter")
    job = {"company": "Stripe", "title": "Solutions Engineer", "url": "https://x", "apply_url": "https://y"}
    answers = [{"label": "Why Stripe?", "value": "Because.", "source": "written"},
               {"label": "Salary", "value": "", "source": "unanswered"}]
    link = gdrive.copy_application(job, tmp_path, {"resume": "Resume.pdf", "cover_letter": "Cover_Letter.pdf"},
                                   answers, "2026-10-21")
    assert link == "https://drive/2"
    assert list(folders) == ["Job Applications", "Stripe – Solutions Engineer (2026-10-21)"]
    assert folders["Stripe – Solutions Engineer (2026-10-21)"] == ["folder1"]
    assert sum(1 for m, p in calls if "upload" in p) == 3  # resume, cover letter, summary
    summary = (tmp_path / "Application summary.txt").read_text()
    assert "Why Stripe?" in summary and "Salary" not in summary


def test_not_configured_without_secrets(monkeypatch):
    for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert not gdrive.configured()


def test_backup_uploads_and_prunes(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setenv("GDRIVE_CLIENT_ID", "id")
    monkeypatch.setenv("GDRIVE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GDRIVE_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(gdrive.httpx, "post", lambda *a, **k: httpx.Response(200, json={"access_token": "t"}))
    deleted, uploads = [], []
    old = [{"id": f"old{i}", "name": f"autoapply-2026-09-{i:02d}.db"} for i in range(1, 20)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if request.method == "GET":
            q = request.url.params.get("q", "")
            return httpx.Response(200, json={"files": old if "in parents and trashed" in q and "name" not in q else []})
        if "upload" in request.url.path:
            uploads.append(request.content[:200])
            return httpx.Response(200, json={"id": "f"})
        return httpx.Response(200, json={"id": "folder", "webViewLink": "https://drive/backups"})

    real_client = httpx.Client
    monkeypatch.setattr(gdrive.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    dbfile = tmp_path / "x.db"
    sqlite3.connect(dbfile).execute("create table t(a)").connection.commit()
    assert gdrive.backup(dbfile, "personal: {}", keep=14) == "https://drive/backups"
    assert len(uploads) == 2 and len(deleted) == 5  # 19 old backups, keep newest 14
