"""Optional per-cycle summary to a phone or chat: ntfy.sh topic, Slack webhook, or Discord webhook."""

from __future__ import annotations

import os

import httpx

from .settings import env


def dashboard_url() -> str | None:
    app = os.environ.get("FLY_APP_NAME")
    return os.environ.get("PUBLIC_URL") or (f"https://{app}.fly.dev" if app else None)


def send(text: str, path: str = "") -> None:
    """path: dashboard page the notification opens when tapped (e.g. a job page)."""
    url = env.notify_url
    if not url:
        return
    if "hooks.slack.com" in url:
        httpx.post(url, json={"text": text}, timeout=15)
    elif "discord.com/api/webhooks" in url:
        httpx.post(url, json={"content": text[:1900]}, timeout=15)
    else:  # ntfy.sh and anything that accepts a plain-text POST
        headers = {"Title": "Job auto-apply"}
        if dashboard_url():
            headers["Click"] = dashboard_url() + path  # tapping the notification opens the dashboard
        httpx.post(url, content=text.encode(), headers=headers, timeout=15)


def cycle_summary(stats: dict, error: str | None) -> None:
    apply = stats.get("apply") or {}
    linkedin = (stats.get("discover") or {}).get("linkedin") or {}
    applied, manual, failed = apply.get("applied", 0), apply.get("needs_manual", 0), apply.get("failed", 0)
    ready = apply.get("ready", 0)
    manual_li = linkedin.get("manual", 0)
    if not (applied or ready or manual or failed or manual_li or error):
        return  # nothing worth a ping
    lines = []
    if applied:
        lines.append(f"Applied to {applied} job(s).")
    if apply.get("ready"):
        lines.append(f"{apply['ready']} application(s) ready for your approval.")
    if manual:
        lines.append(f"{manual} application(s) need you (CAPTCHA or unanswerable question).")
    if manual_li:
        lines.append(f"{manual_li} LinkedIn job(s) to apply to yourself.")
    if failed:
        lines.append(f"{failed} failed.")
    if error:
        lines.append(f"Cycle error: {error[:200]}")
    send("\n".join(lines))
