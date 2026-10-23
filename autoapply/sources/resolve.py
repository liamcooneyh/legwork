"""Map a company name to its public ATS board, and find a posting on it by title."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx

from .. import db
from ..settings import LinkedInAlertsConfig
from . import FETCHERS
from .base import Job

LEGAL = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh", "sa", "the"}
SUFFIXES = LEGAL | {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh", "sa",
            "technologies", "technology", "tech", "labs", "lab", "hq", "group", "holdings", "the", "software",
            "systems", "solutions", "global", "usa", "us", "io", "app", "ai"}


def _ascii_words(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return re.findall(r"[a-z0-9]+", text)


def company_key(name: str) -> str:
    return "".join(_ascii_words(name))


def slug_variants(name: str) -> list[str]:
    words = _ascii_words(name)
    legal = [w for w in words if w not in LEGAL] or words
    core = [w for w in words if w not in SUFFIXES] or words
    candidates = ["".join(legal), "-".join(legal), "".join(core), "-".join(core), "".join(words)]
    if len(core) > 1 and len(core[0]) > 3:
        candidates.append(core[0])
    return [c for c in dict.fromkeys(candidates) if len(c) >= 2]


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\bsr\.?\b", "senior", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    t = re.sub(r"\beng\.?\b", "engineer", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if set(na.split()) == set(nb.split()):
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _location_bonus(alert_location: str, job: Job) -> float:
    a, b = alert_location.lower(), (job.location or "").lower()
    if not a or not b:
        return 0.0
    if "remote" in a and ("remote" in b or job.remote):
        return 0.05
    city = a.split(",")[0].strip()
    return 0.05 if city and city in b else 0.0


@dataclass
class Found:
    job: Job | None
    reason: str = ""


class BoardResolver:
    def __init__(self, cfg: LinkedInAlertsConfig):
        self.cfg = cfg
        self._boards: dict[tuple[str, str], list[Job] | None] = {}
        self._overrides = {company_key(k): v for k, v in cfg.company_overrides.items()}

    def _postings(self, ats: str, board: str) -> list[Job] | None:
        """Jobs on a board, or None if the board doesn't exist. Memoised per run."""
        key = (ats, board)
        if key not in self._boards:
            try:
                jobs = FETCHERS[ats](board)
                self._boards[key] = jobs or None
            except (httpx.HTTPError, ValueError, KeyError):
                self._boards[key] = None
        return self._boards[key]

    def _best_match(self, jobs: list[Job], title: str, location: str) -> tuple[Job | None, float]:
        best, best_score = None, 0.0
        for job in jobs:
            score = title_similarity(title, job.title)
            if score >= self.cfg.title_match_threshold:
                score += _location_bonus(location, job)
            if score > best_score:
                best, best_score = job, score
        return best, best_score

    def _name_matches(self, company: str, jobs: list[Job]) -> bool:
        # Greenhouse reports the employer's name; Lever/Ashby only give the slug.
        names = {j.company for j in jobs if j.ats == "greenhouse"}
        if not names:
            return True
        target = company_key(company)
        return any(SequenceMatcher(None, target, company_key(n)).ratio() >= 0.7 or
                   company_key(n) in target or target in company_key(n) for n in names)

    def _candidates(self, company: str) -> list[tuple[str, str]]:
        """Every existing board that could belong to this company (several companies can share a name)."""
        found = []
        for slug in slug_variants(company):
            for ats in FETCHERS:
                jobs = self._postings(ats, slug)
                if jobs and self._name_matches(company, jobs) and (ats, slug) not in found:
                    found.append((ats, slug))
        return found

    def resolve_board(self, company: str) -> tuple[str, str] | None:
        """Find (ats, board) for a company, using the cache and config overrides."""
        key = company_key(company)
        override = self._overrides.get(key)
        if override:
            if override.lower() == "none":
                return None
            ats, _, board = override.partition(":")
            return (ats, board) if ats in FETCHERS and board else None

        cached = db.get_company_board(key)
        if cached is not None:
            if cached["ats"]:
                return cached["ats"], cached["board"]
            checked = datetime.fromisoformat(cached["checked_at"])
            if datetime.now(timezone.utc) - checked < timedelta(days=self.cfg.reprobe_misses_after_days):
                return None
        candidates = self._candidates(company)
        if candidates:
            db.set_company_board(key, company, *candidates[0])
            return candidates[0]
        db.set_company_board(key, company, None, None)
        return None

    def find_posting(self, company: str, title: str, location: str) -> Found:
        """Look for this job on the company's boards. Checks the remembered board first, then every
        other candidate board, and remembers whichever board actually has the job."""
        key = company_key(company)
        override = self._overrides.get(key)
        if override and override.lower() == "none":
            return Found(None, "Company set to 'not on a supported board' in config")
        first = self.resolve_board(company)
        boards = [first] if first else []
        if not override:
            boards += [b for b in self._candidates(company) if b not in boards]
        if not boards:
            return Found(None, "Company isn't on Greenhouse, Lever, or Ashby (or its board name couldn't be guessed)")
        best, best_score, best_board = None, 0.0, None
        for board in boards:
            match, score = self._best_match(self._postings(*board) or [], title, location)
            if match is not None and score > best_score:
                best, best_score, best_board = match, score, board
        if best is not None and best_score >= self.cfg.title_match_threshold:
            if best_board != first and not override:
                db.set_company_board(key, company, *best_board)  # remember the board that really has it
            return Found(best)
        names = ", ".join(f"{a}:{b}" for a, b in boards)
        return Found(None, f"No posting titled like this on {names} (it may be closed or listed elsewhere)")


# ------------------------------------------------------------------ other hiring systems (free)

OTHER_RECHECK_DAYS = 7


def find_on_other_systems(company: str, title: str, location: str = "", threshold: float = 0.85):
    """Look for this job on SmartRecruiters, Workable, Recruitee, BambooHR, Breezy and Rippling,
    by guessing the company's board name. Free: a few public requests, no AI. Returns
    (Posting, description) or None. Which boards a company has is remembered for a week."""
    from datetime import datetime, timedelta, timezone

    from . import extra_ats

    key = company_key(company)
    cached = db.kv_json(f"other_boards:{key}", None)
    fresh = cached and datetime.now(timezone.utc) - datetime.fromisoformat(cached["checked_at"]) < timedelta(days=OTHER_RECHECK_DAYS)
    client = extra_ats.client()
    postings: list = []
    boards = cached["boards"] if fresh else []
    try:
        if fresh:
            for ats, slug in boards:
                postings += extra_ats.SYSTEMS[ats](client, slug)
        else:
            for slug in slug_variants(company)[:4]:
                for ats, fetch in extra_ats.SYSTEMS.items():
                    try:
                        found = fetch(client, slug)
                    except extra_ats.RateLimited:
                        continue  # that system asked us to slow down; skip it this time
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        continue
                    if found:
                        boards.append([ats, slug])
                        postings += found
            db.kv_set_json(f"other_boards:{key}", {"boards": boards, "checked_at": db.now()})
        target = company_key(company)
        best, best_score = None, 0.0
        for p in postings:
            if p.company_name and SequenceMatcher(None, target, company_key(p.company_name)).ratio() < 0.6 \
                    and company_key(p.company_name) not in target and target not in company_key(p.company_name):
                continue  # a different company that happens to use the same board name
            score = title_similarity(title, p.title)
            if score >= threshold and location and p.location and norm_title(location.split(",")[0]) in norm_title(p.location):
                score += 0.05
            if score > best_score:
                best, best_score = p, score
        if best is None or best_score < threshold:
            return None
        try:
            text = best.describe()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        return (best, text) if text and len(text) > 200 else None
    finally:
        client.close()
