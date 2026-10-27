"""Watch Gmail for replies about your applications and move their tracking stage.

Only emails that mention a company you've applied to (or come from an ATS's mail domain) are
read past the headers, and each email is processed once. Stages only move forward:
Applied → Heard back → Interviewing → Offer, or to Rejected. Offer and Withdrawn are never
changed automatically, and a "No response" application can still move up if a reply arrives.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote

from . import db, notify
from .llm import LLM, LLMError
from .settings import ReplyTrackingConfig
from .sources import mailbox
from .sources.base import html_to_text
from .sources.resolve import company_key

ATS_MAIL_DOMAINS = ["greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "lever.co", "ashbyhq.com",
                    "myworkday.com", "icims.com", "smartrecruiters.com", "jobvite.com", "workablemail.com",
                    "bamboohr.com", "rippling.com", "gem.com"]

CATEGORY_STAGE = {
    "rejection": "rejected",
    "interview_request": "interviewing",
    "assessment": "heard_back",
    "recruiter_reply": "heard_back",
    "offer": "offer",
}
CATEGORY_LABEL = {
    "confirmation": "Application received",
    "rejection": "Rejection",
    "interview_request": "Interview request",
    "assessment": "Assessment / take-home",
    "recruiter_reply": "Recruiter reply",
    "offer": "Offer",
}
FORWARD = {"applied": 0, "no_response": 0, "heard_back": 1, "interviewing": 2, "offer": 3}

_lock = threading.Lock()


def next_stage(current: str | None, category: str) -> str | None:
    """The stage an email moves an application to, or None to leave it."""
    target = CATEGORY_STAGE.get(category)
    current = current or "applied"
    if target is None or current in ("offer", "withdrawn", "rejected"):
        return None
    if target == "rejected":
        return "rejected"
    # "No response" ranks with "Applied", so any real reply moves it up.
    return target if FORWARD[target] > FORWARD.get(current, 0) else None


def build_query(jobs: list, days: int) -> str:
    names = []
    for j in jobs:
        name = re.sub(r'["{}()]', " ", j["company"] or "").strip()
        if name and name not in names:
            names.append(name)
    terms = [f"from:{d}" for d in ATS_MAIL_DOMAINS]
    for n in names:   # every company you've applied to, as long as the search stays a sensible length
        if sum(len(t) + 1 for t in terms) + len(n) + 3 > 1800:
            break
        terms.append(f'"{n}"')
    return f"newer_than:{days}d -from:linkedin.com -from:me {{{' '.join(terms)}}}"


def _parse_date(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def candidates_for(msg: mailbox.Message, body: str, jobs: list) -> list:
    """Applications an email could plausibly be about, by company name or sender domain."""
    _, address = parseaddr(msg.sender)
    domain = address.split("@")[-1].lower() if "@" in address else ""
    blob = f"{msg.subject}\n{msg.sender}\n{body[:6000]}".lower()
    compact = re.sub(r"[^a-z0-9]", "", blob)
    sent = _parse_date(msg.date)
    out = []
    for j in jobs:
        key = company_key(j["company"] or "")
        board = re.sub(r"[^a-z0-9]", "", (j["board"] or "").lower())
        hit = (len(key) >= 4 and key in compact) or (len(board) >= 4 and board in compact) or \
              (key and key in re.sub(r"[^a-z0-9]", "", domain))
        if not hit:
            continue
        applied = _parse_date_iso(j["applied_at"] or j["updated_at"])
        if sent and applied and sent < applied - timedelta(days=1):
            continue  # older than the application, so not a reply to it
        out.append(j)
    return out[:6]


def _parse_date_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def check(cfg: ReplyTrackingConfig, llm: LLM | None, log=print) -> dict:
    if not cfg.enabled:
        return {"skipped": "disabled"}
    if mailbox.configured() is None:
        return {"skipped": "no Gmail credentials set"}
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return _check(cfg, llm, log)
    finally:
        _lock.release()


def _check(cfg: ReplyTrackingConfig, llm: LLM | None, log) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=cfg.track_days)
    jobs = [j for j in db.tracked_jobs()
            if (_parse_date_iso(j["applied_at"] or j["updated_at"]) or since) >= since]
    stats = {"emails": 0, "matched": 0, "stage_changes": 0, "no_response": 0}
    if not jobs:
        return {**stats, "skipped": "no applications to track yet"}

    for msg in mailbox.fetch(build_query(jobs, cfg.lookback_days), cfg.max_emails, skip=db.mail_seen):
        key = msg.message_id or msg.id
        if db.mail_seen(key):
            continue
        stats["emails"] += 1
        body = msg.text or html_to_text(msg.html)
        cands = candidates_for(msg, body, jobs)
        if not cands:
            db.mark_mail(key, None, "no_match")
            continue
        if llm is None:
            break  # leave it unseen so it's classified once a key is set
        try:
            verdict = llm.classify_reply(
                {"sender": msg.sender, "date": msg.date, "subject": msg.subject, "body": body},
                [{"job_key": j["id"], "company": j["company"], "title": j["title"],
                  "applied": (j["applied_at"] or "")[:10]} for j in cands],
            )
        except LLMError as e:
            log(f"reply classification failed: {e}")
            continue
        job = next((j for j in cands if j["id"] == verdict.job_key), None)
        db.mark_mail(key, job["id"] if job else None, verdict.category)
        if job is None or verdict.category == "other":
            continue
        stats["matched"] += 1
        label = CATEGORY_LABEL.get(verdict.category, verdict.category)
        db.add_event(job["id"], "email", f"{label}: {verdict.summary}", msg.gmail_link or None)
        new = next_stage(job["track_stage"], verdict.category)
        if new:
            db.set_tracking(job["id"], new, None, via="from email")
            stats["stage_changes"] += 1
            log(f"reply: {job['company']} — {label}")
            if verdict.category != "confirmation":
                notify.send(f"{job['company']}: {label}\n{verdict.summary}", f"/job/{quote(job['id'], safe='')}")

    if cfg.no_response_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.no_response_days)
        replied = set(db.last_email_by_job())
        for j in jobs:
            applied = _parse_date_iso(j["applied_at"])
            if (j["track_stage"] or "applied") == "applied" and applied and applied < cutoff and j["id"] not in replied:
                db.set_tracking(j["id"], "no_response", None, via=f"no reply in {cfg.no_response_days} days")
                stats["no_response"] += 1
    return stats
