"""One cycle: discover -> prefilter -> score -> tailor -> apply."""

from __future__ import annotations

import json
import os
import queue
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from playwright.sync_api import Browser, sync_playwright

from . import db, notify
from .apply import FormFiller, Outcome
from .filters import fast_track, prefilter
from .llm import LLM, LLMError, TailoredResume
from .profile import Profile, load_profile, resume_file
from .resume_render import render_cover_letter, render_resume
from . import runtime
from .settings import Config, env
from .sources import FETCHERS, board_list, describe_job, hydrate, linkedin_alerts

_cycle_lock = threading.Lock()
_browser_lock = threading.Lock()   # one Chromium at a time (each one uses ~300-400 MB)

RUN_STEPS = [("discover", "Checking company boards"), ("linkedin", "Reading LinkedIn alerts"),
             ("filter", "Filtering"), ("screen", "AI title screen"), ("linkedin_free", "LinkedIn jobs: free checks"),
             ("score", "Scoring fit"),
             ("apply", "Preparing applications")]



REPOST_OPEN = ("new", "screen", "prefiltered", "matched", "held", "ready", "needs_manual", "dry_run", "failed",
               "manual_linkedin")


def set_aside_reposts(reposters: set[str]) -> int:
    """Jobs from job boards and recruiters that repost other companies' jobs (Jobgether and the
    like) are skipped wherever they came from, LinkedIn alerts or the reposter's own board."""
    from .sources.resolve import company_key

    if not reposters:
        return 0
    n = 0
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id, company, board FROM jobs WHERE status IN ({','.join('?' * len(REPOST_OPEN))})",
                            REPOST_OPEN).fetchall()
    for r in rows:
        if company_key(r["company"] or "") in reposters or (r["board"] or "").lower().replace("-", "") in reposters:
            db.set_status(r["id"], db.SKIPPED, "Job board or recruiter repost: the real employer isn't named")
            n += 1
    return n

class RunProgress:
    """What the current cycle is doing, for the dashboard's live status (kept in memory only)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {}
        self._cost: Callable[[], float] | None = None

    def begin(self, cost: Callable[[], float] | None) -> None:
        with self._lock:
            self._state, self._cost = {"started_at": db.now()}, cost

    def step(self, key: str, total: int | None = None, current: str | None = None) -> None:
        with self._lock:
            if self._state:
                self._state.update(step=key, done=0, total=total, current=current, step_started_at=db.now())

    def tick(self, n: int = 1, current: str | None = None) -> None:
        with self._lock:
            if self._state:
                self._state["done"] = self._state.get("done", 0) + n
                if current is not None:
                    self._state["current"] = current

    def note(self, current: str) -> None:
        with self._lock:
            if self._state:
                self._state["current"] = current

    def end(self) -> None:
        with self._lock:
            self._state, self._cost = {}, None

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            cost = self._cost
        if state and cost is not None:
            try:
                state["cost_usd"] = round(cost(), 2)
            except Exception:
                pass
        return state


progress = RunProgress()

# Steps of preparing one draft, for the progress bar on a job page.
PREP_STEPS = [("score", "Re-scoring against your profile"), ("tailor", "Tailoring your resume"),
              ("form", "Reading the application form"), ("answers", "Answering the questions"),
              ("cover", "Writing the cover letter"), ("check", "Checking answers against the form"),
              ("save", "Saving the draft")]
_prep_progress: dict[str, dict] = {}
_prep_lock = threading.Lock()


def prep_step(job_id: str, step: str | None) -> None:
    """Record which step a job's draft is on (None when finished)."""
    with _prep_lock:
        if step is None:
            _prep_progress.pop(job_id, None)
            return
        entry = _prep_progress.setdefault(job_id, {"started_at": db.now(), "done": []})
        if entry.get("step") and entry["step"] != step and entry["step"] not in entry["done"]:
            entry["done"].append(entry["step"])
        entry["step"] = step


def prep_progress(job_id: str) -> dict | None:
    with _prep_lock:
        entry = _prep_progress.get(job_id)
        return dict(entry, done=list(entry["done"])) if entry else None

NO_DESCRIPTION = "Couldn't find the full description outside LinkedIn: open it there, or use Send to Legwork"
LOOKUP_COST_ESTIMATE = 0.015  # dollars per paid LinkedIn lookup (Haiku + one web search), shown before you start


class LinkedInLookups:
    """Background queue for LinkedIn lookups: separate from the regular run and only started by you."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._worker: threading.Thread | None = None
        self._cancel = False
        self.progress = RunProgress()
        self.results: dict[str, int] = {}

    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def queued(self) -> set[str]:
        with self._lock:
            return set(self._queue)

    def add(self, job_ids: list[str], budget: float | None = None) -> int:
        with self._lock:
            new = [j for j in job_ids if j not in self._queue]
            self._queue += new
            start = not self.running()
            if start:
                self._cancel = False
                self.results = {}
                self.budget = budget
                self._worker = threading.Thread(target=self._run, name="linkedin-lookups", daemon=True)
        if start:
            self._worker.start()
        else:
            with self.progress._lock:
                if self.progress._state:
                    self.progress._state["total"] = (self.progress._state.get("total") or 0) + len(new)
        return len(new)

    def cancel(self) -> None:
        with self._lock:
            self._queue.clear()
            self._cancel = True

    def _run(self) -> None:
        from .sources.resolve import BoardResolver

        pipeline = Pipeline()
        if pipeline.llm is None:
            with self._lock:
                self._queue.clear()
            return
        resolver = BoardResolver(pipeline.cfg.search.linkedin_alerts)
        with self._lock:
            total = len(self._queue)
        self.progress.begin(pipeline.llm.cost_usd)
        self.progress.step("lookup", total=total)
        try:
            while True:
                with self._lock:
                    if self._cancel or not self._queue:
                        break
                    if getattr(self, "budget", None) and pipeline.llm.cost_usd() >= self.budget:
                        self.results["stopped_at_budget"] = 1
                        self._queue.clear()
                        break
                    job_id = self._queue.pop(0)
                job = db.get_job(job_id)
                if job is None or job["status"] != db.MANUAL_LINKEDIN or job["ats"] != "linkedin":
                    self.progress.tick()
                    continue  # applied, skipped, or already converted since you queued it
                self.progress.note(f"{job['company']} — {job['title']}")
                try:
                    outcome = pipeline.lookup_linkedin(dict(job), resolver)
                except Exception as e:
                    pipeline.log(f"lookup failed for {job_id}: {e}")
                    outcome = "error"
                self.results[outcome] = self.results.get(outcome, 0) + 1
                self.progress.tick()
        finally:
            spent = pipeline.llm.cost_usd()
            db.kv_set_json("linkedin_lookup_last", {"at": db.now(), "results": self.results, "cost_usd": round(spent, 2)})
            self.progress.end()


linkedin_lookups = LinkedInLookups()


def release_memory() -> None:
    """Hand freed memory back to the OS before starting Chromium (Python keeps it otherwise)."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not Linux/glibc (e.g. running locally on a Mac)


@contextmanager
def browser_session(headless: bool = True):
    release_memory()
    with _browser_lock, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            yield browser
        finally:
            browser.close()


# Approvals clicked in the dashboard are submitted one at a time by a background worker.
_approvals: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None


def approve(job_id: str) -> None:
    global _worker
    db.set_status(job_id, db.SUBMITTING, "Approved; submitting")
    _approvals.put(job_id)
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_approval_worker, name="approvals", daemon=True)
        _worker.start()


def _approval_worker() -> None:
    pipeline = Pipeline()
    while True:
        try:
            job_id = _approvals.get(timeout=300)
        except queue.Empty:
            return
        try:
            pipeline.submit_approved(job_id)
        except Exception as e:
            db.set_status(job_id, db.FAILED, f"Submit error: {e}")

OUTCOME_STATUS = {
    "applied": db.APPLIED,
    "dry_run": db.DRY_RUN,
    "needs_manual": db.NEEDS_MANUAL,
    "failed": db.FAILED,
}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class Pipeline:
    def __init__(self, cfg: Config | None = None, profile: Profile | None = None, log: Callable[[str], None] = print):
        self.cfg = cfg or runtime.config()
        self.profile = profile or load_profile()
        self.log = log
        self._llm: LLM | None = None

    @property
    def llm(self) -> LLM | None:
        if self._llm is None and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            self._llm = LLM(self.profile, self.cfg.llm.model, scoring_model=self.cfg.llm.scoring_model)
        return self._llm

    # ------------------------------------------------------------------ discovery

    def _sync_board(self, ats: str, board: str) -> tuple[list[dict], set[str]]:
        """Fetch one board. New jobs are prefiltered here, so descriptions are only kept
        (and, for Greenhouse, only downloaded) for jobs worth scoring."""
        jobs = FETCHERS[ats](board)
        known = db.existing_ids(ats, board)
        rows = []
        for job in jobs:
            if job.id in known:
                continue
            row = job.to_row()
            reason = prefilter(row, self.cfg.search)
            if reason:
                row.update(status=db.FILTERED_OUT, status_reason=reason, description=None)
            elif fast_track(job.title, self.cfg.search):
                try:
                    row = hydrate(job).to_row()
                except httpx.HTTPError:
                    pass  # scored on the title alone rather than dropped
                row.update(status=db.PREFILTERED, status_reason="Matches an 'always consider' title")
            else:
                row["status"] = db.SCREEN  # descriptions for Greenhouse jobs are fetched if they pass
            rows.append(row)
        return rows, {j.id for j in jobs}

    def discover(self) -> dict:
        boards = board_list(self.cfg.search)
        stats = {"boards": 0, "board_errors": [], "new_jobs": 0, "new_matches_filter": 0, "closed": 0}
        progress.step("discover", total=len(boards))
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._sync_board, ats, board): (ats, board) for ats, board in boards}
            for future in as_completed(futures):
                # Drop each board's results once saved; holding all ~470 at once ran the VM out of memory.
                ats, board = futures.pop(future)
                progress.tick(current=board)
                try:
                    rows, live_ids = future.result()
                except (httpx.HTTPError, ValueError) as e:
                    stats["board_errors"].append(f"{ats}:{board} ({e.__class__.__name__})")
                    continue
                stats["boards"] += 1
                db.insert_jobs(rows)
                stats["new_jobs"] += len(rows)
                stats["new_matches_filter"] += sum(r["status"] in (db.PREFILTERED, db.SCREEN) for r in rows)
                stats["closed"] += db.mark_closed(ats, board, live_ids)
                del rows, live_ids, future
        progress.step("linkedin")
        try:
            stats["linkedin"] = linkedin_alerts.ingest(self.cfg.search, log=self.log)
        except Exception as e:  # mail problems must not stop the cycle
            stats["linkedin"] = {"error": str(e)[:300]}
        return stats

    def mark_reposts(self, days: int = 90) -> int:
        """Companies often take a job down and re-post it under a new ID. Treat a new posting with
        the same company and (nearly) the same title as one you've already handled as a duplicate."""
        from .sources.resolve import company_key, title_similarity

        handled: dict[str, list] = {}
        for r in db.handled_roles(days):
            handled.setdefault(company_key(r["company"] or ""), []).append(r)
        if not handled:
            return 0
        count = 0
        for job in db.jobs_with_status(db.PREFILTERED) + db.jobs_with_status(db.NEW):
            for prev in handled.get(company_key(job["company"] or ""), []):
                if prev["id"] != job["id"] and title_similarity(prev["title"], job["title"]) >= 0.92:
                    what = {"applied": "applied to", "skipped": "skipped"}.get(prev["status"], "already have")
                    db.set_status(job["id"], db.SKIPPED,
                                  f"Looks like a re-post of a job you {what} on {prev['at'][:10]}")
                    count += 1
                    break
        return count

    def run_prefilter(self) -> dict:
        progress.step("filter")
        passed = dropped = 0
        for job in db.jobs_with_status(db.NEW):
            reason = prefilter(dict(job), self.cfg.search)
            if reason:
                db.set_status(job["id"], db.FILTERED_OUT, reason)
                dropped += 1
            else:
                db.set_status(job["id"], db.PREFILTERED if fast_track(job["title"], self.cfg.search) else db.SCREEN)
                passed += 1
        return {"passed": passed, "dropped": dropped}

    # ------------------------------------------------------------------ scoring

    def score(self) -> dict:
        if self.llm is None:
            return {"skipped": "ANTHROPIC_API_KEY not set"}
        matched = rejected = errors = 0
        jobs = db.jobs_with_status(db.PREFILTERED, limit=self.cfg.matching.max_scored_per_cycle)
        progress.step("score", total=len(jobs))

        def work(job_id: str):
            try:
                return self.score_one(job_id)
            except (LLMError, httpx.HTTPError) as e:
                self.log(f"score failed for {job_id}: {e}")
                return None
            finally:
                progress.tick()

        # A few at a time: the first pass over a big backlog is thousands of jobs.
        with ThreadPoolExecutor(max_workers=6) as pool:
            for ok in pool.map(work, [j["id"] for j in jobs]):
                if ok is None:
                    errors += 1
                elif ok:
                    matched += 1
                else:
                    rejected += 1
        return {"matched": matched, "rejected": rejected, "errors": errors}

    def score_one(self, job_id: str, change: str = "") -> bool:
        """Fetch the description if needed, score the job, and record the result. Returns matched.
        `change`: what you just added to your profile, for an anchored re-score."""
        job = db.get_job(job_id)
        if not job["description"] and job["ats"] in FETCHERS:
            try:  # Greenhouse lists, and jobs let back in by a filter change, arrive without one
                text = describe_job(job["ats"], job["board"], job["external_id"])
                if text:
                    db.set_description(job["id"], text)
                    job = db.get_job(job["id"])
            except httpx.HTTPError:
                pass
        previous = json.loads(job["score_json"]) if job["score_json"] else None
        prefs = self.cfg.search.preferences
        min_score = db.setting_int("min_score", self.cfg.matching.min_score)
        anchored = bool(change and previous and previous.get("parts"))
        result = self.llm.score(dict(job), prefs, previous=previous if anchored else None, change=change)
        how = f"after you added: {change[:80]}" if anchored else "fresh"
        if not anchored:
            # A second opinion from the stronger model when it's close to your cutoff, or when it moved a
            # lot since the last time this job was scored.
            moved = previous is not None and job["score"] is not None and abs(result["score"] - job["score"]) > 10
            if abs(result["score"] - min_score) <= 6 or moved:
                result = self.llm.score(dict(job), prefs, model=self.llm.model)
                how = "second opinion (close to your cutoff or a big change)"
        history = (previous or {}).get("history", [])
        if previous and job["score"] is not None:
            history = history + [{"at": previous.get("scored_at") or job["updated_at"], "score": job["score"],
                                  "parts": {k: v["points"] for k, v in (previous.get("parts") or {}).items()},
                                  "how": previous.get("how", "")}]
        result.update(history=history[-8:], scored_at=db.now(), how=how)
        ok = result["score"] >= min_score and not result["dealbreakers"]
        # Prepared, applied, held or hand-handled jobs keep their status; only the score is refreshed.
        keep = job["status"] in (db.READY, db.SUBMITTING, db.APPLIED, db.NEEDS_MANUAL, db.SKIPPED, db.DRY_RUN,
                                 db.MANUAL_LINKEDIN, db.HELD)
        db.set_score(job["id"], result["score"], result,
                     job["status"] if keep else (db.MATCHED if ok else db.REJECTED))
        return ok

    # ------------------------------------------------------------------ LinkedIn-only jobs

    def _convert_linkedin(self, manual: dict, board_job) -> str:
        """A LinkedIn-only job turned out to be on a supported board: make it a normal job. Returns its id."""
        try:
            hydrate(board_job)
        except httpx.HTTPError:
            pass
        row = board_job.to_row()
        row["status"] = db.PREFILTERED
        db.delete_job(manual["id"])
        if not db.upsert_job(row, source="linkedin_alert"):
            existing = db.get_job(row["id"])
            if existing["status"] in (db.SCREENED_OUT, db.FILTERED_OUT, db.SCREEN, db.NEW):
                db.set_status(row["id"], db.PREFILTERED, "Found through your LinkedIn alert")
        db.link_linkedin(row["id"], manual["linkedin_id"] or manual["external_id"], manual["linkedin_url"] or manual["url"])
        return row["id"]

    def _linkedin_board_job(self, job: dict, board_job) -> str:
        new_id = self._convert_linkedin(job, board_job)
        try:  # scored now, so a good fit is prepared on the next regular run
            self.score_one(new_id)
        except (LLMError, httpx.HTTPError) as e:
            self.log(f"score failed for {new_id}: {e}")
        return "applyable"

    def _linkedin_scored(self, job: dict, text: str, url: str, where: str) -> str:
        """Score a LinkedIn-only job from the employer's own posting text."""
        db.set_description(job["id"], text)
        db.set_lookup(job["id"], url)
        try:
            ok = self.score_one(job["id"])
        except LLMError as e:
            self.log(f"score failed for {job['id']}: {e}")
            return "error"
        if ok:
            db.set_status(job["id"], db.MANUAL_LINKEDIN, f"Scored from the employer's posting{where}; apply there")
            return "good_fit"
        db.set_status(job["id"], db.REJECTED, f"Poor fit (scored from the employer's posting{where})")
        return "poor_fit"

    def free_linkedin(self, job: dict, resolver) -> str | None:
        """The free part of a LinkedIn lookup: the company's boards on Greenhouse/Lever/Ashby, then on
        SmartRecruiters, Workable, Recruitee, BambooHR, Breezy and Rippling. None if not found."""
        from .sources.resolve import find_on_other_systems

        found = resolver.find_posting(job["company"], job["title"], job["location"] or "")
        if found.job is not None:
            return self._linkedin_board_job(job, found.job)
        other = find_on_other_systems(job["company"], job["title"], job["location"] or "")
        if other is not None:
            posting, text = other
            if posting.posted_at:  # the employer's own date beats when the LinkedIn alert came
                db.set_posted(job["id"], posting.posted_at)
            return self._linkedin_scored(job, text, posting.url, f" on {posting.ats.title()}")
        return None

    def lookup_linkedin(self, job: dict, resolver) -> str:
        """For one "apply yourself" LinkedIn job: the free checks (unless already done), then one
        Claude web search for the employer's posting, read it, and score it. Returns what happened:
        applyable | good_fit | poor_fit | no_description | error."""
        from .sources import detect_from_url
        from .sources.resolve import company_key
        from .sources.webpage import looks_like, read_posting

        if not job.get("free_check_at"):
            outcome = self.free_linkedin(job, resolver)
            if outcome is not None:
                return outcome
        try:
            hit = self.llm.find_original_posting(job["title"], job["company"], job["location"] or "")
        except LLMError as e:
            self.log(f"posting search failed for {job['id']}: {e}")
            return "error"  # not marked as looked up, so it can be tried again
        url = hit.url.strip() if hit.confident else ""
        board_hit = detect_from_url(url) if url else None
        if board_hit and board_hit[0] in FETCHERS:
            ats, board, ext = board_hit
            try:
                match = next((j for j in FETCHERS[ats](board) if j.external_id == ext), None)
            except (httpx.HTTPError, ValueError):
                match = None
            if match is not None:
                # Remember (and from now on watch) the board the search uncovered, e.g. gong → gongio.
                db.set_company_board(company_key(job["company"]), job["company"], ats, board)
                return self._linkedin_board_job(job, match)
        text = ""
        if url:
            with browser_session(self.cfg.apply.headless) as browser:  # held only while reading this page
                text = read_posting(browser, url)
        if not text or not looks_like(text, job["title"], job["company"]):
            db.set_lookup(job["id"])
            db.set_status(job["id"], db.MANUAL_LINKEDIN, NO_DESCRIPTION)
            return "no_description"
        return self._linkedin_scored(job, text, url, "")

    def triage_and_free_check_linkedin(self, limit: int = 400) -> dict:
        """Part of every run, and free apart from one cheap title pass: flag recruiter/job-board
        reposts, rate each LinkedIn-only title (for best-first lookups), then try the free board
        checks. Paid web searches only happen from the LinkedIn page."""
        from .sources.resolve import BoardResolver, company_key

        stats = {"reposts": 0, "found_free": 0, "checked": 0}
        stats["reposts"] += set_aside_reposts(set(db.kv_json("reposter_companies", [])))
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM jobs WHERE status=? AND ats='linkedin' AND lookup_at IS NULL AND score IS NULL "
                "AND (free_check_at IS NULL OR title_fit IS NULL) ORDER BY discovered_at DESC LIMIT ?",
                (db.MANUAL_LINKEDIN, limit))]
        if not rows or self.llm is None:
            return stats
        reposters = set(db.kv_json("reposter_companies", []))
        untriaged = [r for r in rows if r["title_fit"] is None and company_key(r["company"]) not in reposters]
        snapshot = self.llm.candidate_snapshot(self.cfg.search.preferences)
        for i in range(0, len(untriaged), 100):
            batch = untriaged[i:i + 100]
            try:
                verdicts = self.llm.triage_linkedin(batch, snapshot)
            except LLMError as e:
                self.log(f"LinkedIn triage failed: {e}")
                continue
            with db.connect() as conn:
                for n, r in enumerate(batch):
                    v = verdicts.get(n)
                    if v is None:
                        continue
                    conn.execute("UPDATE jobs SET title_fit=? WHERE id=?", (max(0, min(10, v.fit)), r["id"]))
                    r["title_fit"] = v.fit
                    if v.reposter:
                        reposters.add(company_key(r["company"]))
        db.kv_set_json("reposter_companies", sorted(reposters))
        stats["reposts"] += set_aside_reposts(reposters)
        resolver = BoardResolver(self.cfg.search.linkedin_alerts)
        todo = []
        for r in rows:
            if company_key(r["company"]) in reposters:
                db.set_status(r["id"], db.SKIPPED, "Recruiter or job-board repost: the real employer isn't named")
                stats["reposts"] += 1
            elif not r["free_check_at"]:
                todo.append(r)
        progress.step("linkedin_free", total=len(todo))

        def check(r):
            try:
                outcome = self.free_linkedin(r, resolver)
            except Exception as e:
                self.log(f"free check failed for {r['id']}: {e}")
                outcome = None
            if outcome is None:
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET free_check_at=? WHERE id=?", (db.now(), r["id"]))
            progress.tick(current=f"{r['company']} — {r['title']}")
            return outcome

        with ThreadPoolExecutor(max_workers=6) as pool:  # a few public requests per company; a handful at once
            for outcome in pool.map(check, todo):
                stats["checked"] += 1
                stats["found_free"] += outcome is not None
        return stats

    # ------------------------------------------------------------------ title screen

    def screen(self, batch_size: int = 120) -> dict:
        """AI first pass over titles that passed location/age filters. Errs toward keeping."""
        if self.llm is None:
            return {"skipped": "ANTHROPIC_API_KEY not set"}
        pending = db.jobs_with_status(db.SCREEN)
        progress.step("screen", total=len(pending))
        if not pending:
            return {"screened": 0}
        snapshot = self.llm.candidate_snapshot(self.cfg.search.preferences)
        examples = db.kv_json("screen_examples", [])
        kept = dropped = errors = 0
        batches = [[dict(r) for r in pending[i:i + batch_size]] for i in range(0, len(pending), batch_size)]

        def work(batch):
            try:
                return batch, self.llm.screen_titles(batch, snapshot, examples)
            except LLMError as e:
                self.log(f"title screen batch failed: {e}")
                return batch, None  # stays in SCREEN; retried next run
            finally:
                progress.tick(len(batch))

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(work, batches))
        for batch, keep in results:
            if keep is None:
                errors += 1
                continue
            updates = []
            for i, job in enumerate(batch):
                if i in keep and job["ats"] == "linkedin":
                    updates.append((db.MANUAL_LINKEDIN, "Kept by the AI title screen (LinkedIn only)", job["id"]))
                    kept += 1
                elif i in keep:
                    updates.append((db.PREFILTERED, "Kept by the AI title screen", job["id"]))
                    kept += 1
                else:
                    updates.append((db.SCREENED_OUT, "AI title screen: not a plausible fit for your target roles", job["id"]))
                    dropped += 1
            db.bulk_status(updates)
        return {"screened": kept + dropped, "kept": kept, "dropped": dropped, "batch_errors": errors}

    # ------------------------------------------------------------------ applying

    def apply_queue(self, mode: str) -> dict:
        """approve mode: prepare matches and mark them ready for your approval.
        auto mode: submit matches (and anything already prepared) automatically."""
        if self.llm is None:
            return {"skipped": "ANTHROPIC_API_KEY not set"}
        cfg = self.cfg.apply
        daily_cap = db.setting_int("max_per_day", cfg.max_per_day)
        queue = db.jobs_with_status(db.MATCHED)
        if mode == "auto":
            queue += db.jobs_with_status(db.READY)
            queue.sort(key=lambda j: -(j["score"] or 0))
        results: dict[str, int] = {}
        done_before = db.submitted_since(24) if mode == "auto" else db.prepared_since(24)
        progress.step("apply", total=max(0, min(len(queue), daily_cap - done_before)))
        for i, job in enumerate(queue):
            if db.is_paused() or db.apply_mode(cfg.mode) != mode:
                break  # paused or switched mode from the dashboard mid-run
            done_today = db.submitted_since(24) if mode == "auto" else db.prepared_since(24)
            if done_today >= daily_cap:
                results["daily_cap_reached"] = 1
                break
            # One role per company (in any 30 days): the best-scoring one. The others are held so you
            # can compare them on the job page and swap if you'd rather.
            overrides = set(db.kv_json("company_overrides", []))
            slot = db.company_slot(job["company"], job["id"], cfg.company_window_days)
            if slot is not None and job["id"] not in overrides:
                if slot["status"] in (db.READY, db.NEEDS_MANUAL) and slot["id"] not in overrides \
                        and (job["score"] or 0) > (slot["score"] or 0):
                    db.set_status(slot["id"], db.HELD, f"Held: {job['company']} has a higher-scoring role "
                                                       f"({job['title']}, {job['score']})")
                else:
                    db.set_status(job["id"], db.HELD, f"Held: one role per company. You already have "
                                                      f"{slot['title']} ({slot['score']}) at {job['company']}")
                    results["held_same_company"] = results.get("held_same_company", 0) + 1
                    continue
            progress.note(f"{job['company']} — {job['title']}")
            if job["status"] == db.READY:
                outcome = self.submit_approved(job["id"])
            else:
                with browser_session(cfg.headless) as browser:
                    outcome = self.apply_one(browser, dict(job), dry_run=(mode == "approve"))
            key = "ready" if outcome.status == "dry_run" and mode == "approve" else outcome.status
            progress.tick()
            results[key] = results.get(key, 0) + 1
            if outcome.status == "applied" and i < len(queue) - 1:
                time.sleep(random.uniform(*cfg.delay_seconds))
        return results

    def submit_approved(self, job_id: str) -> Outcome:
        """Submit exactly what was prepared and reviewed: same resume, cover letter, and answers."""
        job = db.get_job(job_id)
        prepared = db.latest_prepared(job_id)
        if job is None or prepared is None:
            outcome = Outcome("failed", "Nothing prepared to submit; queue it again to re-prepare")
            if job is not None:
                db.set_status(job_id, db.FAILED, outcome.detail)
            return outcome
        art = json.loads(prepared["artifacts_json"] or "{}")
        src = env.artifacts_dir / art.get("dir", "")
        approved = {a["label"]: a.get("values") or ([a["value"]] if a.get("value") else [])
                    for a in json.loads(prepared["answers_json"] or "[]")
                    if a.get("source") not in ("unanswered", "you_checked") and a.get("type") != "file"}
        cover: tuple[str, Path] | None = None
        if art.get("cover_letter") and (src / "cover_letter.txt").exists():
            cover = ((src / "cover_letter.txt").read_text(), src / art["cover_letter"])
        db.set_status(job_id, db.SUBMITTING)
        with browser_session(self.cfg.apply.headless) as browser:
            return self.apply_one(browser, dict(job), dry_run=False, reuse=(src / art.get("resume", ""), cover, approved))

    def reanswer(self, job_id: str) -> Outcome:
        """Redo only the form answers for a prepared draft, keeping the resume and cover letter
        you've already seen. The result is a fresh draft for your approval."""
        job = db.get_job(job_id)
        prepared = db.latest_prepared(job_id)
        if job is None or prepared is None:
            return Outcome("failed", "Nothing prepared to re-answer")
        art = json.loads(prepared["artifacts_json"] or "{}")
        src = env.artifacts_dir / art.get("dir", "")
        cover: tuple[str, Path] | None = None
        if art.get("cover_letter") and (src / "cover_letter.txt").exists():
            cover = ((src / "cover_letter.txt").read_text(), src / art["cover_letter"])
        with browser_session(self.cfg.apply.headless) as browser:
            return self.apply_one(browser, dict(job), dry_run=True, reuse=(src / art.get("resume", ""), cover, None))

    def apply_one(self, browser: Browser, job: dict, dry_run: bool,
                  reuse: tuple[Path, tuple[str, Path] | None, dict[str, list[str]] | None] | None = None) -> Outcome:
        cfg = self.cfg.apply
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rel_dir = Path(_safe(job["id"])) / stamp
        out_dir = env.artifacts_dir / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        app_id = db.start_application(job["id"], dry_run)
        from . import profile_store
        artifacts: dict = {"dir": str(rel_dir), "profile_version": profile_store.current_version_id()}
        cost_before = self.llm.cost_usd() if self.llm is not None else 0.0
        verb = ("re-answering" if reuse else "preparing") if dry_run else ("submitting approved" if reuse else "submitting")
        self.log(f"{verb}: {job['company']} — {job['title']}")

        try:
            if reuse is not None:
                # Copy the approved materials so this attempt's folder is self-contained.
                old_resume, cover, approved = reuse
                resume_pdf = out_dir / old_resume.name
                resume_pdf.write_bytes(old_resume.read_bytes())
                artifacts["resume"] = resume_pdf.name
                if (old_resume.parent / "resume.json").exists():
                    (out_dir / "resume.json").write_text((old_resume.parent / "resume.json").read_text())
                if cover:
                    (out_dir / "cover_letter.txt").write_text(cover[0])
                    (out_dir / cover[1].name).write_bytes(cover[1].read_bytes())
                    artifacts["cover_letter"] = cover[1].name
                    cover = (cover[0], out_dir / cover[1].name)
                filler = FormFiller(browser, job, self.profile, self.llm, cfg, out_dir, resume_pdf,
                                    lambda: cover, approved_answers=approved)
                filler.fill_form = not dry_run
                outcome = filler.run(dry_run=dry_run)
            else:
                outcome = self._prepare_and_fill(browser, job, dry_run, out_dir, artifacts)
        except Exception as e:
            outcome = Outcome("failed", f"Internal error: {e}", log=[traceback.format_exc()])

        prep_step(job["id"], "save")
        artifacts["screenshots"] = outcome.screenshots
        if self.llm is not None:
            artifacts["claude_cost_usd"] = round(self.llm.cost_usd() - cost_before, 4)
        if outcome.status == "applied":
            copy_to_drive(job, out_dir, artifacts, outcome.answers, self.log)
        db.finish_application(app_id, outcome.status, outcome.detail, artifacts, outcome.answers, outcome.log)
        # A prepared (filled, not submitted) application waits for your approval.
        status = db.READY if outcome.status == "dry_run" else OUTCOME_STATUS[outcome.status]
        db.set_status(job["id"], status, "Ready to fill in Chrome" if status == db.READY else outcome.detail)
        self.log(f"  → {outcome.status}: {outcome.detail[:200]}")
        prep_step(job["id"], None)
        return outcome

    def prepare_materials(self, job_id: str) -> None:
        """Tailored resume + cover letter for a job you'll apply to yourself (no form to fill)."""
        from . import profile_store

        job = dict(db.get_job(job_id))
        cfg = self.cfg.apply
        rel_dir = Path(_safe(job["id"])) / datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = env.artifacts_dir / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        app_id = db.start_application(job["id"], True)
        artifacts: dict = {"dir": str(rel_dir), "profile_version": profile_store.current_version_id()}
        cost_before = self.llm.cost_usd() if self.llm is not None else 0.0
        detail = "Resume and cover letter ready to download"
        try:
            with browser_session(cfg.headless) as browser:
                resume = TailoredResume.from_profile(self.profile)
                if self.llm is not None:
                    try:
                        resume = self.llm.tailor_resume(job)
                        artifacts["tailoring_notes"] = resume.notes
                    except LLMError as e:
                        artifacts["tailoring_notes"] = [f"tailoring failed, used master resume: {e}"]
                pdf = render_resume(browser, self.profile, resume, out_dir / resume_file(self.profile), cfg.paper)
                artifacts["resume"] = pdf.name
                (out_dir / "resume.json").write_text(resume.model_dump_json(indent=1))
                if self.llm is not None:
                    text = self.llm.cover_letter(job)
                    if text:
                        (out_dir / "cover_letter.txt").write_text(text)
                        artifacts["cover_letter"] = render_cover_letter(
                            browser, self.profile, text, out_dir / "Cover_Letter.pdf", cfg.paper).name
                    else:
                        artifacts["cover_letter_error"] = "failed fact-check twice; left out"
        except Exception as e:
            detail = f"Couldn't prepare materials: {e}"
        if self.llm is not None:
            artifacts["claude_cost_usd"] = round(self.llm.cost_usd() - cost_before, 4)
        db.finish_application(app_id, "materials", detail, artifacts, [], [])
        db.add_event(job["id"], "status", "materials: " + detail)

    def _prepare_and_fill(self, browser: Browser, job: dict, dry_run: bool, out_dir: Path, artifacts: dict) -> Outcome:
        cfg = self.cfg.apply
        prep_step(job["id"], "tailor")
        resume = TailoredResume.from_profile(self.profile)
        if cfg.tailor_resume and self.llm is not None:
            try:
                resume = self.llm.tailor_resume(job)
                artifacts["tailoring_notes"] = resume.notes
            except LLMError as e:
                artifacts["tailoring_notes"] = [f"tailoring failed, used master resume: {e}"]
        resume_pdf = render_resume(browser, self.profile, resume, out_dir / resume_file(self.profile), cfg.paper)
        artifacts["resume"] = resume_pdf.name
        (out_dir / "resume.json").write_text(resume.model_dump_json(indent=1))

        def cover_provider() -> tuple[str, Path] | None:
            if self.llm is None:
                return None
            prep_step(job["id"], "cover")
            try:
                text = self.llm.cover_letter(job)
            except LLMError as e:
                artifacts["cover_letter_error"] = str(e)
                return None
            if not text:
                artifacts["cover_letter_error"] = "failed fact-check twice; left out"
                return None
            (out_dir / "cover_letter.txt").write_text(text)
            pdf = render_cover_letter(browser, self.profile, text, out_dir / "Cover_Letter.pdf", cfg.paper)
            artifacts["cover_letter"] = pdf.name
            return text, pdf

        if cfg.cover_letter == "always":
            generated = cover_provider()
            provider = lambda: generated  # noqa: E731
        else:
            provider = cover_provider
        filler = FormFiller(browser, job, self.profile, self.llm, cfg, out_dir, resume_pdf, provider)
        filler.on_step = lambda step: prep_step(job["id"], step)
        filler.fill_form = not dry_run  # drafts are filled by the extension in your Chrome
        return filler.run(dry_run)

    # ------------------------------------------------------------------ cycle

    def run_cycle(self, apply: bool = True) -> dict | None:
        if not _cycle_lock.acquire(blocking=False):
            self.log("a cycle is already running; skipping")
            return None
        if db.is_paused():
            _cycle_lock.release()
            self.log("paused; skipping this cycle")
            return {"paused": True}
        run_id = db.start_run()
        stats: dict = {}
        error = None
        progress.begin(self.llm.cost_usd if self.llm is not None else None)
        try:
            # If the filters changed (config.yaml or the Filters page), re-check jobs already stored.
            import hashlib
            fingerprint = hashlib.sha256((runtime.FILTER_LOGIC_VERSION + self.cfg.search.model_dump_json()).encode()).hexdigest()
            if db.kv_get("filters_fingerprint") != fingerprint:
                progress.step("filter", current="Re-checking stored jobs against your updated filters")
                stats["refilter"] = runtime.refilter_all(self.cfg.search)
                db.kv_set("filters_fingerprint", fingerprint)
            stats["discover"] = self.discover()
            stats["prefilter"] = self.run_prefilter()
            stats["reposts_skipped"] = self.mark_reposts()
            stats["screen"] = self.screen()
            stats["linkedin_free"] = self.triage_and_free_check_linkedin()
            stats["score"] = self.score()
            if apply:
                mode = db.apply_mode(self.cfg.apply.mode)
                stats["mode"] = mode
                stats["apply"] = self.apply_queue(mode)
            # LinkedIn-only jobs aren't looked up here: that costs a web search each, so it runs only
            # when you ask, from the LinkedIn page (see LinkedInLookups).
            return stats
        except Exception as e:
            error = f"{e.__class__.__name__}: {e}"
            self.log(traceback.format_exc())
            return stats
        finally:
            db.finish_run(run_id, stats, error)
            progress.end()
            _cycle_lock.release()
            try:
                notify.cycle_summary(stats, error)
            except Exception as e:
                self.log(f"notification failed: {e}")


def copy_to_drive(job: dict, files_dir: Path, artifacts: dict, answers: list[dict], log=print) -> None:
    """Copy a submitted application's resume, cover letter, and answers to Google Drive.
    Failures are recorded but never affect the application itself."""
    from . import gdrive

    if not gdrive.configured():
        return
    try:
        link = gdrive.copy_application(job, files_dir, artifacts, answers, datetime.now().strftime("%Y-%m-%d"))
        artifacts["drive_folder"] = link
        artifacts.pop("drive_error", None)
        db.add_event(job["id"], "drive", "Copied resume and answers to Google Drive", link)
    except Exception as e:
        artifacts["drive_error"] = str(e)[:300]
        log(f"Drive copy failed for {job['id']}: {e}")


def cycle_running() -> bool:
    return _cycle_lock.locked()


def describe(stats: dict) -> str:
    return json.dumps(stats, indent=1, default=str)
