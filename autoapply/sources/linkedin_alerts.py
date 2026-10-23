"""Discover jobs from LinkedIn job alert emails.

LinkedIn itself is never scraped or logged into. Each job in an alert email is traced
to the company's own Greenhouse / Lever / Ashby board and applied to there; jobs that
can't be found on a supported board are listed in the dashboard for a manual apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .. import db
from ..filters import fast_track, prefilter
from ..settings import SearchConfig
from . import hydrate, mailbox
from .resolve import BoardResolver

JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d{6,})|currentJobId=(\d{6,})")
# Tags LinkedIn prints under a job card ("Fast growing", "$125K-$150K / year", "3 connections"...).
SIGNAL = re.compile(
    r"^(this company is actively hiring|actively (hiring|recruiting)|fast growing|easy apply|be an early applicant|"
    r"promoted|top applicant|reposted.*|new|view|in your network|apply with .*|.*\bconnections?\b.*|.*\balum(ni|nus|na)?\b.*|"
    r".*\bapplicants?\b.*|\$[\d,.]+.*|.*/ ?(year|yr|hour|hr)\b.*|.*\b(ago|today)\b|.*\bresponds?\b.*|"
    r"·)$",
    re.I,
)
# Lines that end a job card when reading upwards: separators, links, and section headers.
BOUNDARY = re.compile(
    r"<[^>]+>|https?://|^(your job alert|you.?ll receive|new jobs? match|new jobs from your other alerts|see all jobs|"
    r"manage (your )?(job )?alerts?|edit alert|this email was intended|you are receiving)",
    re.I,
)
VIEW_JOB = re.compile(r"^view job:?\s*(\S+)", re.I)


@dataclass
class AlertJob:
    linkedin_id: str
    title: str
    company: str
    location: str

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/jobs/view/{self.linkedin_id}/"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ").replace("͏", "")).strip()


def _job_id(href: str) -> str | None:
    m = JOB_ID.search(href or "")
    return (m.group(1) or m.group(2)) if m else None


def _company_location(line: str, rest: list[str]) -> tuple[str, str]:
    """"Company · Location" on one line (HTML cards) or on two lines (text emails)."""
    parts = [p.strip() for p in re.split(r"\s+[·•]\s+", line) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return line, (rest[0] if rest else "")


def parse_alert_text(text: str) -> list[AlertJob]:
    """The plain-text part of LinkedIn alert emails: each card is title / company / location /
    optional tags, then "View job: <link>", with dashed separators and section headers between."""
    lines = [_clean(l) for l in (text or "").splitlines()]
    jobs, seen = [], set()
    for i, line in enumerate(lines):
        m = VIEW_JOB.match(line)
        jid = _job_id(m.group(1)) if m else None
        if not jid or jid in seen:
            continue
        card: list[str] = []
        for prev in reversed(lines[:i]):
            if not prev:
                continue
            if set(prev) <= set("-=_ ") or BOUNDARY.search(prev) or _job_id(prev):
                break
            card.insert(0, prev)
        card = [l for l in card if not SIGNAL.match(l)]
        if len(card) < 2:
            continue
        seen.add(jid)
        company, location = _company_location(card[1], card[2:])
        jobs.append(AlertJob(jid, card[0], company, location))
    return jobs


def parse_alert_html(html: str) -> list[AlertJob]:
    """Fallback for emails without a text part. A card's link wraps its text pieces: title,
    "Company · Location", then tags."""
    soup = BeautifulSoup(html or "", "html.parser")
    jobs, seen = [], set()
    for a in soup.find_all("a", href=True):
        jid = _job_id(a["href"])
        if not jid or jid in seen:
            continue
        pieces = [_clean(t) for t in a.stripped_strings]
        pieces = [t for t in pieces if t and not SIGNAL.match(t)]
        if len(pieces) < 2:
            continue  # a logo or "Apply" link; the card link comes separately
        seen.add(jid)
        company, location = _company_location(pieces[1], pieces[2:])
        jobs.append(AlertJob(jid, pieces[0], company, location))
    return jobs


def parse_alert(html: str, text: str) -> list[AlertJob]:
    found = parse_alert_text(text) if text else []
    return found or parse_alert_html(html)


def ingest(cfg: SearchConfig, log=print) -> dict:
    """Read recent alert emails and route each new job to its ATS board or the manual list."""
    alerts = cfg.linkedin_alerts
    if not alerts.enabled:
        return {"skipped": "disabled in config"}
    if mailbox.configured() is None:
        return {"skipped": "no Gmail credentials set"}

    messages = mailbox.fetch(alerts.query, alerts.max_emails)
    stats = {"emails": len(messages), "alert_jobs": 0, "already_seen": 0, "matched": 0, "manual": 0, "filtered_out": 0}
    unique: dict[str, AlertJob] = {}
    for msg in messages:
        for job in parse_alert(msg.html, msg.text):
            unique.setdefault(job.linkedin_id, job)
    stats["alert_jobs"] = len(unique)

    resolver = BoardResolver(alerts)
    for alert in unique.values():
        if db.linkedin_seen(alert.linkedin_id):
            stats["already_seen"] += 1
            continue
        found = resolver.find_posting(alert.company, alert.title, alert.location)
        if found.job is not None:
            try:
                hydrate(found.job)
            except Exception:
                pass
            row = found.job.to_row()
            db.upsert_job(row, source="linkedin_alert")
            db.link_linkedin(row["id"], alert.linkedin_id, alert.url)
            stats["matched"] += 1
            log(f"linkedin: {alert.company} — {alert.title} → {found.job.ats}:{found.job.board}")
            continue
        row = {
            "id": f"linkedin:{alert.linkedin_id}", "ats": "linkedin", "board": "", "external_id": alert.linkedin_id,
            "company": alert.company, "title": alert.title, "location": alert.location, "remote": None,
            "url": alert.url, "apply_url": alert.url, "description": "", "status": db.MANUAL_LINKEDIN,
        }
        reason = prefilter({**row, "posted_at": None}, cfg)
        if reason:
            row["status"] = db.FILTERED_OUT
            stats["filtered_out"] += 1
        else:
            row["status"] = db.MANUAL_LINKEDIN if fast_track(alert.title, cfg) else db.SCREEN
            stats["manual"] += 1
        db.upsert_job(row, source="linkedin_alert")
        db.link_linkedin(row["id"], alert.linkedin_id, alert.url, reason or found.reason)
    return stats
