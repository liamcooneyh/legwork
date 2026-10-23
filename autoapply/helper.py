"""The Mac helper: opens a prepared application pre-filled in a real browser window on your Mac,
so you review it and press Submit yourself (security codes, CAPTCHAs and odd forms included).

It polls the dashboard for "Open on my Mac" requests, fills the form with the answers you'd see
on the job page, and watches the window: when the employer's confirmation page appears it records
the job as applied. It never presses Submit.

    autoapply helper              # run in a terminal
    autoapply helper --install    # start it automatically at login (macOS LaunchAgent)
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import httpx
import yaml

from .settings import ROOT, env, load_config

POLL_SECONDS = 2.5
WATCH_HOURS = 3
LABEL = "com.autoapply.helper"

BANNER_JS = """([title, lines, tone]) => {
  const old = document.getElementById("aa-banner");
  if (old) old.remove();
  const d = document.createElement("div");
  d.id = "aa-banner";
  d.setAttribute("data-aa-skip", "1");
  d.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;max-width:400px;" +
    "background:" + (tone === "good" ? "#1f6b3a" : "#23202a") + ";color:#fff;font:14px/1.45 -apple-system,Segoe UI,sans-serif;" +
    "padding:14px 16px 12px;border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.35)";
  const h = document.createElement("div");
  h.style.cssText = "font-weight:700;margin-bottom:4px";
  h.textContent = title;
  d.appendChild(h);
  for (const line of lines) {
    const p = document.createElement("div");
    p.style.cssText = "margin-top:4px;opacity:.92";
    p.textContent = line;
    d.appendChild(p);
  }
  const x = document.createElement("button");
  x.textContent = "Hide";
  x.style.cssText = "margin-top:10px;background:#fff;color:#23202a;border:0;border-radius:8px;padding:4px 10px;font-weight:600;cursor:pointer";
  x.onclick = () => d.remove();
  d.appendChild(x);
  document.body.appendChild(d);
}"""


def base_url() -> str:
    return (os.environ.get("AUTOAPPLY_URL") or os.environ.get("PUBLIC_URL") or f"http://localhost:{env.port}").rstrip("/")


def client() -> httpx.Client:
    password = env.dashboard_password
    if not password:
        raise SystemExit("DASHBOARD_PASSWORD isn't in .env, so the helper can't sign in to the dashboard.")
    return httpx.Client(base_url=base_url(), auth=(env.dashboard_user, password), timeout=60, follow_redirects=True)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


class Session(threading.Thread):
    """One application window: fill, hand over, watch for the confirmation page."""

    def __init__(self, payload: dict, log) -> None:
        super().__init__(daemon=True, name=f"helper-{payload['job']['id']}")
        self.payload, self.log = payload, log
        self.job = payload["job"]

    def _event(self, event: str, detail: str = "") -> None:
        try:
            with client() as c:
                c.post(f"/api/job/{self.job['id']}/helper", json={"event": event, "detail": detail})
        except httpx.HTTPError as e:
            self.log(f"couldn't report '{event}' for {self.job['company']}: {e}")

    def _download(self, folder: Path) -> dict[str, Path]:
        folder.mkdir(parents=True, exist_ok=True)
        out = {}
        with client() as c:
            for kind, path in self.payload["files"].items():
                r = c.get(path)
                r.raise_for_status()
                target = folder / Path(path).name
                target.write_bytes(r.content)
                out[kind] = target
        return out

    def run(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        from .apply.ats import BY_NAME
        from .apply.engine import FormFiller
        from .profile import Profile

        job = self.job
        name = f"{job['company']} — {job['title']}"
        self.log(f"opening {name}")
        try:
            files = self._download(env.data_dir / "helper" / _safe(job["id"]))
        except (httpx.HTTPError, KeyError) as e:
            self.log(f"couldn't download the resume for {name}: {e}")
            self._event("error", f"couldn't download the draft's files: {e}")
            return
        if "resume" not in files:
            self._event("error", "the draft has no resume file")
            return
        profile = Profile.model_validate(yaml.safe_load(self.payload["profile_yaml"]))
        approved = {a["label"]: a.get("values") or ([a["value"]] if a.get("value") else [])
                    for a in self.payload["answers"] if a.get("source") not in ("unanswered", "you_checked")}
        cover = None
        if files.get("cover_letter") and files.get("cover_letter_text"):
            cover = (files["cover_letter_text"].read_text(), files["cover_letter"])
        ats = BY_NAME[job["ats"]]
        out_dir = env.data_dir / "helper" / _safe(job["id"]) / time.strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
            filler = FormFiller(browser, job, profile, None, load_config().apply, out_dir, files["resume"],
                                lambda: cover, approved_answers=approved)
            context, page, outcome = filler.prefill()
            todo = [a["label"] for a in outcome.answers if a.get("source") == "unanswered"]
            self._event("opened", outcome.detail)
            lines = ["Review everything, change whatever you like, then press Submit yourself.",
                     "If the site emails you a security code, enter it here."]
            if todo:
                lines.append("Still needs you: " + "; ".join(t[:70] for t in todo[:5]))
            if outcome.status == "failed":
                lines = [f"The form didn't load as expected ({outcome.detail[:120]}). Fill it in by hand here."]
            try:
                page.evaluate(BANNER_JS, ["Filled in by Legwork", lines, ""])
            except PlaywrightError:
                pass
            self.log(f"ready for you: {name}" + (f" (still needs: {len(todo)})" if todo else ""))

            deadline = time.time() + WATCH_HOURS * 3600
            result = "closed"
            while time.time() < deadline:
                if page.is_closed() or not browser.is_connected():
                    break
                try:
                    page.wait_for_timeout(2000)
                    body = page.inner_text("body")[:30000]
                    url = page.url
                except PlaywrightError:
                    if page.is_closed() or not browser.is_connected():
                        break
                    continue  # navigating
                if ats.success_text.search(body) or (ats.success_url and ats.success_url.search(url)):
                    result = "applied"
                    try:
                        page.evaluate(BANNER_JS, ["Recorded as applied ✓", ["You can close this window."], "good"])
                    except PlaywrightError:
                        pass
                    break
            if result == "applied":
                self.log(f"applied: {name}")
                self._event("applied", "You submitted it; the confirmation page appeared")
                # Leave the confirmation on screen until you close the window.
                while not page.is_closed() and browser.is_connected():
                    try:
                        page.wait_for_timeout(1500)
                    except PlaywrightError:
                        break
            else:
                self.log(f"window closed without a confirmation: {name}")
                self._event("closed")
            try:
                browser.close()
            except PlaywrightError:
                pass


def run(log=print) -> None:
    log(f"Legwork helper: watching {base_url()} for 'Open on my Mac' requests (Ctrl+C to stop)")
    warned = False
    while True:
        try:
            with client() as c:
                r = c.get("/api/helper/next")
            if r.status_code == 401:
                if not warned:
                    log("The dashboard rejected the password in .env (DASHBOARD_USER / DASHBOARD_PASSWORD). "
                        "It must match the one set on Fly.")
                    warned = True
                time.sleep(30)
                continue
            r.raise_for_status()
            warned = False
            payload = r.json()
            if payload.get("job"):
                Session(payload, log).start()
        except httpx.HTTPError as e:
            log(f"can't reach the dashboard ({e.__class__.__name__}); retrying")
            time.sleep(10)
        time.sleep(POLL_SECONDS)


def install() -> str:
    """Start the helper at login and keep it running (a per-user macOS LaunchAgent)."""
    import subprocess
    import sys

    exe = Path(sys.executable).parent / "autoapply"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log_path = env.data_dir / "helper.log"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>helper</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict></plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=True)
    return f"Installed: the helper now starts at login and keeps running. Log: {log_path}"


def uninstall() -> str:
    import subprocess

    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    plist.unlink(missing_ok=True)
    return "Removed: the helper no longer starts at login."
