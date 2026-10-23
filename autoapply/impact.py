"""Time saved by the app compared with applying by hand, and the numbers behind it (the Impact page).

Each application you submit is credited the minutes it would have taken by hand (finding and reading
the posting, tailoring the resume, writing the cover letter and answers, filling the form), minus the
minutes you actually spent: measured from the extension's fill to your submit when it can be. Each
posting Claude scored for you, and that you didn't go on to apply to, is credited a minute or so of
reading it yourself. Both assumptions are editable in Settings.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

ET = ZoneInfo("America/New_York")
MANUAL_MINUTES = 18      # your estimate: 3+ hours for 10 applications, even with Claude chat
SCREEN_MINUTES = 1.0     # opening and skimming one posting to decide it's not for you
FALLBACK_MINUTES = 4     # your time on an application when it can't be measured
FILL_MINUTES = 1         # the extension filling the form, before the measured part starts
MAX_MEASURED = 60        # longer than this, you probably left the tab open: not a real measure
REPLIED = ("heard_back", "interviewing", "offer", "rejected")
INTERVIEWED = ("interviewing", "offer")


def settings() -> dict[str, float]:
    def num(key: str, default: float) -> float:
        try:
            return float(db.kv_get(key) or default)
        except ValueError:
            return default
    return {"manual": num("impact_manual_minutes", MANUAL_MINUTES), "screen": num("impact_screen_minutes", SCREEN_MINUTES)}


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(dt: datetime) -> str:
    """The calendar day in New York time."""
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def _first_scored(row) -> datetime | None:
    try:
        detail = json.loads(row["score_json"] or "{}")
    except ValueError:
        detail = {}
    history = detail.get("history") or []
    return _dt(history[0].get("at") if history else None) or _dt(detail.get("scored_at")) or _dt(row["discovered_at"])


def summary(days: int = 14, min_score: int = 70) -> dict:
    cfg = settings()
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        applied = [dict(r) for r in conn.execute(
            "SELECT id, company, title, score, applied_at, track_stage, posted_at FROM jobs WHERE applied_at IS NOT NULL")]
        fills: dict[str, list[datetime]] = {}
        for r in conn.execute("SELECT job_id, at FROM job_events WHERE kind='helper' AND detail LIKE 'Filled in your Chrome%'"):
            if (at := _dt(r["at"])) is not None:
                fills.setdefault(r["job_id"], []).append(at)
        scored = conn.execute("SELECT score_json, discovered_at FROM jobs WHERE score IS NOT NULL AND applied_at IS NULL").fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        found = sum(counts.values())
        drafted = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM applications WHERE dry_run=1 "
            "AND COALESCE(outcome, '') NOT IN ('materials', 'interrupted')").fetchone()[0]
        drafts_cost = 0.0
        for r in conn.execute("SELECT artifacts_json FROM applications WHERE dry_run=1 AND artifacts_json IS NOT NULL"):
            try:
                drafts_cost += float(json.loads(r[0]).get("claude_cost_usd") or 0)
            except (ValueError, TypeError, AttributeError):
                pass
        spend_rows = conn.execute("SELECT day, usd FROM spend ORDER BY day").fetchall()

    # Applications: credited minutes minus what you spent.
    measured: list[float] = []
    apps_minutes = 0.0
    by_day: dict[str, dict[str, float]] = {}
    for a in applied:
        at = _dt(a["applied_at"])
        before = [f for f in fills.get(a["id"], []) if at and f <= at]
        spent = None
        if before and at:
            m = (at - max(before)).total_seconds() / 60 + FILL_MINUTES
            if 0 < m <= MAX_MEASURED:
                spent = m
                measured.append(m)
        saved = max(0.0, cfg["manual"] - (spent if spent is not None else FALLBACK_MINUTES))
        a["minutes_saved"] = saved
        apps_minutes += saved
        if at:
            w = by_day.setdefault(_day(at), {"apps": 0.0, "screen": 0.0, "applied": 0})
            w["apps"] += saved
            w["applied"] += 1

    # Screening: postings read for you.
    screen_minutes = len(scored) * cfg["screen"]
    for r in scored:
        if (at := _first_scored(r)) is not None:
            by_day.setdefault(_day(at), {"apps": 0.0, "screen": 0.0, "applied": 0})["screen"] += cfg["screen"]

    today = datetime.strptime(_day(now), "%Y-%m-%d")
    series = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        w = by_day.get(day, {"apps": 0.0, "screen": 0.0, "applied": 0})
        series.append({"day": day, "label": datetime.strptime(day, "%Y-%m-%d").strftime("%b %-d"),
                       "apps_hours": w["apps"] / 60, "screen_hours": w["screen"] / 60, "applied": int(w["applied"])})
    week = series[-7:]

    # How old each posting was when you applied (only where the employer's date is known).
    ages = []
    for a in applied:
        at, posted = _dt(a["applied_at"]), _dt(a["posted_at"])
        if at and posted and at >= posted:
            ages.append((at - posted).total_seconds() / 86400)
    fresh_buckets = [{"label": label, "count": sum(1 for d in ages if lo <= d < hi)}
                     for label, lo, hi in (("Under 3 days", 0, 3), ("3–7 days", 3, 7), ("1–2 weeks", 7, 14),
                                           ("2–4 weeks", 14, 30), ("Over 30 days", 30, 10_000))]

    # Replies by score band.
    bands = []
    for name, lo, hi in (("90+", 90, 101), ("80–89", 80, 90), ("70–79", 70, 80), ("Under 70 / unscored", -1, 70)):
        rows = [a for a in applied if (lo <= (a["score"] if a["score"] is not None else -1) < hi)]
        bands.append({"band": name, "applied": len(rows),
                      "replied": sum(1 for a in rows if a["track_stage"] in REPLIED),
                      "interviewing": sum(1 for a in rows if a["track_stage"] in INTERVIEWED)})

    gone = lambda *ss: sum(counts.get(s, 0) for s in ss)  # noqa: E731
    past_filters = found - gone("filtered_out")
    funnel = [
        ("Found", found),
        ("Passed your filters", past_filters),
        ("Passed the title screen", past_filters - gone("screened_out", "screen", "new")),
        ("Scored by Claude", len(scored) + len(applied)),
        (f"Good fit ({min_score}+)", None),
        ("Drafted", drafted),
        ("Applied", len(applied)),
        ("Heard back", sum(1 for a in applied if a["track_stage"] in REPLIED)),
        ("Interviewing", sum(1 for a in applied if a["track_stage"] in INTERVIEWED)),
    ]
    with db.connect() as conn:
        good = conn.execute("SELECT COUNT(*) FROM jobs WHERE score >= ?", (min_score,)).fetchone()[0]
    funnel[4] = (funnel[4][0], good)

    spend_total = sum(r["usd"] for r in spend_rows)
    hours = (apps_minutes + screen_minutes) / 60
    return {
        "settings": cfg, "fallback_minutes": FALLBACK_MINUTES,
        "hours": hours, "apps_hours": apps_minutes / 60, "screen_hours": screen_minutes / 60,
        "applied": len(applied), "screened": len(scored),
        "week_hours": sum(d["apps_hours"] + d["screen_hours"] for d in week),
        "week_applied": sum(d["applied"] for d in week),
        "series": series,
        "median_minutes": statistics.median(measured) if measured else None, "measured": len(measured),
        "funnel": funnel, "bands": bands,
        "fresh_median_days": statistics.median(ages) if ages else None, "fresh_known": len(ages),
        "fresh_buckets": fresh_buckets,
        "drafted": drafted, "drafts_cost": drafts_cost,
        "spend_total": spend_total, "spend_since": spend_rows[0]["day"] if spend_rows else None,
        "cost_per_draft": drafts_cost / drafted if drafted else None,
        "cost_per_hour": (spend_total or drafts_cost) / hours if hours else None,
    }
