"""Cheap deterministic prefilter, run before spending any tokens.

Only certain dealbreakers live here: excluded companies, definitely-not titles, location,
and posting age. Whether a title is *relevant* is decided by the AI title screen
(see Pipeline.screen); titles matching an "always consider" keyword skip it."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .settings import SearchConfig


def _contains(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(needle.lower())}(?![a-z])", haystack.lower()) is not None


def _age_days(posted_at: str | None) -> float | None:
    if not posted_at:
        return None
    try:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted) / timedelta(days=1)


REMOTE_WORDS = r"remote|anywhere|distributed|work from home|wfh|virtual|telecommute"
US_WORDS = (r"united states of america|united states|u\.s\.a\.?|u\.s\.|usa|us|america|americas|north america|"
            r"nationwide|east coast|eastern time|est|et|global|worldwide")
FILLER = r"hybrid|on-?site|in-?office|office|hq|headquarters|based|only|preferred|time zones?|timezone|or|and|within|the|any"


def _strip(seg: str, pattern: str) -> str:
    return re.sub(rf"(?<![a-z])(?:{pattern})(?![a-z])", " ", seg)


def location_ok(location: str, remote_flag: bool, cfg: SearchConfig) -> bool:
    """True if any listed location is a wanted city, or remote without being tied to some
    other place. "Remote - US" passes; "Remote - Texas", "Paris" (even if flagged remote), and
    "Hybrid - San Francisco" don't; "Remote (US) / San Francisco" passes on its first part."""
    segments = [s.strip() for s in re.split(r"\s*(?:/|•|;|\||\n)\s*", location or "") if s.strip()]
    if not segments:
        return bool(remote_flag and cfg.allow_remote)
    for seg in segments:
        low = seg.lower()
        if any(_contains(low, loc) for loc in cfg.locations):
            return True
        if not cfg.allow_remote:
            continue
        has_remote = re.search(rf"(?<![a-z])(?:{REMOTE_WORDS})(?![a-z])", low) is not None
        has_us = re.search(rf"(?<![a-z])(?:{US_WORDS})(?![a-z])", low) is not None
        if not (has_remote or has_us or (remote_flag and len(segments) == 1)):
            continue
        if any(_contains(low, ex) for ex in cfg.location_exclude):
            continue
        leftover = _strip(_strip(_strip(low, REMOTE_WORDS), US_WORDS), FILLER)
        if not re.sub(r"[^a-z]+", "", leftover):
            return True  # nothing left but "remote"/"US"/filler
    return False


def prefilter(job: Mapping[str, Any], cfg: SearchConfig) -> str | None:
    """Return a reason the job should be dropped, or None if it passes."""
    title = job["title"] or ""
    location = job.get("location") or ""

    if any(c.lower() == (job["company"] or "").lower() or c.lower() == job["board"].lower()
           for c in cfg.exclude_companies):
        return "company excluded"

    for term in cfg.title_exclude:
        if _contains(title, term):
            return f"title contains '{term}'"

    if not location_ok(location, bool(job.get("remote")), cfg):
        return f"location '{location[:80]}' not wanted"

    age = _age_days(job.get("posted_at"))
    if cfg.max_age_days and age is not None and age > cfg.max_age_days:
        return f"posted {int(age)} days ago"

    return None


def fast_track(title: str, cfg: SearchConfig) -> bool:
    """Titles with an "always consider" keyword skip the AI title screen and go straight to scoring."""
    return any(_contains(title or "", term) for term in cfg.title_include)
