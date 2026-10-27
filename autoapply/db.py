"""SQLite storage. One short-lived connection per operation keeps it safe across the
scheduler thread and the web server."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .settings import env

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,              -- "{ats}:{board}:{external_id}"
    ats TEXT NOT NULL,
    board TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    url TEXT,
    apply_url TEXT NOT NULL,
    description TEXT,
    compensation TEXT,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT,
    score INTEGER,
    score_json TEXT,
    source TEXT NOT NULL DEFAULT 'board',   -- board | linkedin_alert
    linkedin_id TEXT,
    linkedin_url TEXT,
    department TEXT,
    lookup_at TEXT,                   -- when we last searched the web for a LinkedIn job's original posting
    applied_at TEXT,                  -- when the application was submitted
    track_stage TEXT,                 -- your tracking after applying (see TRACK_STAGES)
    track_notes TEXT,
    track_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS jobs_linkedin ON jobs(linkedin_id);

-- Which ATS board (if any) a company's jobs live on, learned from LinkedIn alerts.
CREATE TABLE IF NOT EXISTS company_boards (
    company_key TEXT PRIMARY KEY,     -- normalised company name
    company TEXT NOT NULL,
    ats TEXT,                         -- NULL on a miss
    board TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL,
    outcome TEXT,                     -- applied | dry_run | needs_manual | failed
    detail TEXT,
    artifacts_json TEXT,              -- resume, cover letter, screenshots (paths relative to artifacts dir)
    answers_json TEXT,
    log TEXT
);
CREATE INDEX IF NOT EXISTS applications_job ON applications(job_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    error TEXT
);

-- Timeline of everything that happened to a job (shown on its page).
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,               -- status | score | stage | note | email
    detail TEXT,
    link TEXT
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id);

-- Emails already looked at by reply tracking, so each is processed once.
CREATE TABLE IF NOT EXISTS mail_seen (
    message_key TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL,
    job_id TEXT,
    category TEXT
);

-- Every version of your profile; the newest is the current one.
CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note TEXT,
    yaml TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Claude spend per day (every call, from runs, drafts, lookups, and scoring).
CREATE TABLE IF NOT EXISTS spend (
    day TEXT PRIMARY KEY,             -- YYYY-MM-DD (UTC)
    usd REAL NOT NULL DEFAULT 0
);
"""

# Job statuses, in pipeline order.
NEW = "new"                    # discovered, not yet filtered
SCREEN = "screen"              # passed location/age filters; waiting for the AI title screen
SCREENED_OUT = "screened_out"  # the AI title screen judged the title not plausibly relevant
FILTERED_OUT = "filtered_out"  # failed the keyword/location prefilter
PREFILTERED = "prefiltered"    # passed the prefilter, waiting for scoring
REJECTED = "rejected"          # Claude scored it below the threshold
MATCHED = "matched"            # scored high enough, waiting to apply
HELD = "held"                  # matched, but you already have a (better) role at this company
APPLIED = "applied"
READY = "ready"                # prepared (resume, answers, filled form); waiting for your approval
SUBMITTING = "submitting"      # approved; being submitted now
DRY_RUN = "dry_run"            # filled for a test (CLI), not submitted
NEEDS_MANUAL = "needs_manual"  # stopped for a human (CAPTCHA, unanswerable required question)
FAILED = "failed"
SKIPPED = "skipped"            # skipped by hand or by caps
CLOSED = "closed"              # disappeared from the board before we applied
MANUAL_LINKEDIN = "manual_linkedin"  # found in a LinkedIn alert, but not on a supported ATS

# What happened after applying; you set these on the Applications page.
TRACK_STAGES = [
    ("applied", "Applied"),
    ("heard_back", "Heard back"),
    ("interviewing", "Interviewing"),
    ("offer", "Offer"),
    ("rejected", "Rejected"),
    ("no_response", "No response"),
    ("withdrawn", "Withdrawn"),
]

ALL_STATUSES = [READY, SUBMITTING, APPLIED, NEEDS_MANUAL, MANUAL_LINKEDIN, MATCHED, HELD, PREFILTERED, SCREEN, NEW,
                DRY_RUN, FAILED, REJECTED, SCREENED_OUT, FILTERED_OUT, SKIPPED, CLOSED]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(env.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


MIGRATIONS = [
    ("jobs", "department", "TEXT"),
    ("jobs", "lookup_at", "TEXT"),
    ("job_events", "link", "TEXT"),
    ("jobs", "applied_at", "TEXT"),
    ("jobs", "track_stage", "TEXT"),
    ("jobs", "track_notes", "TEXT"),
    ("jobs", "track_updated_at", "TEXT"),
    ("jobs", "source", "TEXT NOT NULL DEFAULT 'board'"),
    ("jobs", "linkedin_id", "TEXT"),
    ("jobs", "linkedin_url", "TEXT"),
    ("jobs", "free_check_at", "TEXT"),   # LinkedIn-only job: free board checks done (no match)
    ("jobs", "title_fit", "INTEGER"),    # LinkedIn-only job: 0-10 title fit, for best-first lookups
]


def init() -> None:
    with connect() as conn:
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- jobs

def upsert_job(job: dict[str, Any], source: str = "board") -> bool:
    """Insert a discovered job. Returns True if it was new. Existing jobs only get
    their description and timestamps refreshed, never their status."""
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        ts = now()
        if exists:
            conn.execute(
                "UPDATE jobs SET title=?, location=?, remote=?, description=?, compensation=?, "
                "url=?, apply_url=?, updated_at=? WHERE id=?",
                (job["title"], job.get("location"), job.get("remote"), job.get("description"),
                 job.get("compensation"), job.get("url"), job["apply_url"], ts, job["id"]),
            )
            return False
        conn.execute(
            "INSERT INTO jobs (id, ats, board, external_id, company, title, location, remote, url, "
            "apply_url, description, compensation, posted_at, discovered_at, updated_at, status, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["ats"], job["board"], job["external_id"], job["company"], job["title"],
             job.get("location"), job.get("remote"), job.get("url"), job["apply_url"],
             job.get("description"), job.get("compensation"), job.get("posted_at"), ts, ts,
             job.get("status", NEW), source),
        )
        return True


def existing_ids(ats: str, board: str) -> set[str]:
    with connect() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE ats=? AND board=?", (ats, board))}


def insert_jobs(rows: list[dict[str, Any]], source: str = "board") -> None:
    ts = now()
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO jobs (id, ats, board, external_id, company, title, location, remote, url, "
            "apply_url, description, compensation, posted_at, discovered_at, updated_at, status, status_reason, source, "
            "department) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["id"], r["ats"], r["board"], r["external_id"], r["company"], r["title"], r.get("location"),
              r.get("remote"), r.get("url"), r["apply_url"], r.get("description"), r.get("compensation"),
              r.get("posted_at"), ts, ts, r.get("status", NEW), r.get("status_reason"), source,
              r.get("department")) for r in rows],
        )


def learned_boards() -> list[tuple[str, str]]:
    with connect() as conn:
        return [(r["ats"], r["board"]) for r in
                conn.execute("SELECT DISTINCT ats, board FROM company_boards WHERE ats IS NOT NULL")]


def link_linkedin(job_id: str, linkedin_id: str, linkedin_url: str, reason: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET linkedin_id=?, linkedin_url=?, status_reason=COALESCE(?, status_reason) WHERE id=?",
            (linkedin_id, linkedin_url, reason, job_id),
        )


def linkedin_seen(linkedin_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM jobs WHERE linkedin_id=?", (linkedin_id,)).fetchone() is not None


def get_company_board(company_key: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM company_boards WHERE company_key=?", (company_key,)).fetchone()


def set_company_board(company_key: str, company: str, ats: str | None, board: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO company_boards (company_key, company, ats, board, checked_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(company_key) DO UPDATE SET ats=excluded.ats, board=excluded.board, checked_at=excluded.checked_at",
            (company_key, company, ats, board, now()),
        )


def get_job(job_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def jobs_with_status(status: str, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM jobs WHERE status = ? ORDER BY COALESCE(score, 0) DESC, discovered_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, (status,)).fetchall()


def list_jobs(status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            # Lists you pick from read best-fit first; the rest newest first.
            order = "COALESCE(score, -1) DESC, updated_at DESC" if status in (MANUAL_LINKEDIN, MATCHED, READY, REJECTED,
                                                                              NEEDS_MANUAL, HELD) \
                else "updated_at DESC"
            return conn.execute(
                f"SELECT * FROM jobs WHERE status = ? ORDER BY {order} LIMIT ?", (status, limit)
            ).fetchall()
        return conn.execute(
            "SELECT * FROM jobs WHERE status NOT IN (?, ?) ORDER BY updated_at DESC LIMIT ?",
            (FILTERED_OUT, CLOSED, limit),
        ).fetchall()


def _event(conn: sqlite3.Connection, job_id: str, kind: str, detail: str | None, link: str | None = None) -> None:
    conn.execute("INSERT INTO job_events (job_id, at, kind, detail, link) VALUES (?,?,?,?,?)",
                 (job_id, now(), kind, detail, link))


def add_event(job_id: str, kind: str, detail: str, link: str | None = None) -> None:
    with connect() as conn:
        _event(conn, job_id, kind, detail, link)


def mail_seen(key: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM mail_seen WHERE message_key=?", (key,)).fetchone() is not None


def mark_mail(key: str, job_id: str | None, category: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO mail_seen (message_key, seen_at, job_id, category) VALUES (?,?,?,?)",
                     (key, now(), job_id, category))


def email_events(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT e.*, j.company, j.title FROM job_events e JOIN jobs j ON j.id = e.job_id "
            "WHERE e.kind='email' ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()


def last_email_by_job() -> dict[str, sqlite3.Row]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM job_events WHERE id IN (SELECT MAX(id) FROM job_events WHERE kind='email' GROUP BY job_id)"
        ).fetchall()
    return {r["job_id"]: r for r in rows}


def set_status(job_id: str, status: str, reason: str | None = None) -> None:
    ts = now()
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE id=?",
            (status, reason, ts, job_id),
        )
        if status == APPLIED:
            conn.execute(
                "UPDATE jobs SET applied_at=COALESCE(applied_at, ?), track_stage=COALESCE(track_stage, 'applied'), "
                "track_updated_at=COALESCE(track_updated_at, ?) WHERE id=?", (ts, ts, job_id))
        _event(conn, job_id, "status", f"{status}" + (f": {reason}" if reason else ""))


def add_spend(usd: float) -> None:
    if usd <= 0:
        return
    with connect() as conn:
        conn.execute("INSERT INTO spend (day, usd) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd",
                     (now()[:10], usd))


def set_posted(job_id: str, posted_at: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE jobs SET posted_at=? WHERE id=?", (posted_at, job_id))


def set_score(job_id: str, score: int, detail: dict[str, Any], status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET score=?, score_json=?, status=?, status_reason=NULL, updated_at=? WHERE id=?",
            (score, json.dumps(detail), status, now(), job_id),
        )
        _event(conn, job_id, "score", f"scored {score}/100 → {status}")


def set_tracking(job_id: str, stage: str | None, notes: str | None, via: str = "") -> None:
    valid = {k for k, _ in TRACK_STAGES}
    with connect() as conn:
        row = conn.execute("SELECT track_stage, track_notes FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        if stage and stage in valid and stage != row["track_stage"]:
            conn.execute("UPDATE jobs SET track_stage=?, track_updated_at=? WHERE id=?", (stage, now(), job_id))
            _event(conn, job_id, "stage", dict(TRACK_STAGES)[stage] + (f" ({via})" if via else ""))
        if notes is not None and notes != (row["track_notes"] or ""):
            conn.execute("UPDATE jobs SET track_notes=?, track_updated_at=? WHERE id=?", (notes, now(), job_id))
            _event(conn, job_id, "note", "notes updated")


def events_for(job_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


def tracked_jobs(stage: str | None = None) -> list[sqlite3.Row]:
    """Everything you've applied to (by this tool or by hand), newest first."""
    sql = "SELECT * FROM jobs WHERE status=? "
    args: list[Any] = [APPLIED]
    if stage:
        sql += "AND track_stage=? "
        args.append(stage)
    with connect() as conn:
        return conn.execute(sql + "ORDER BY COALESCE(applied_at, updated_at) DESC", args).fetchall()


def stage_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT COALESCE(track_stage, 'applied') AS s, COUNT(*) AS n FROM jobs WHERE status=? GROUP BY s",
                            (APPLIED,)).fetchall()
    return {r["s"]: r["n"] for r in rows}


def all_jobs_for_refilter() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, ats, board, company, title, location, remote, posted_at, status, status_reason, score FROM jobs "
            "WHERE status IN (?, ?, ?, ?, ?, ?, ?)",
            (NEW, PREFILTERED, MATCHED, FILTERED_OUT, SCREEN, SCREENED_OUT, MANUAL_LINKEDIN)).fetchall()


def bulk_status(updates: list[tuple[str, str | None, str]]) -> None:
    """[(status, reason, job_id)] without timeline events (used for mass re-filtering)."""
    ts = now()
    with connect() as conn:
        conn.executemany("UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE id=?",
                         [(st, reason, ts, jid) for st, reason, jid in updates])


def set_description(job_id: str, description: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE jobs SET description=? WHERE id=?", (description, job_id))


def set_lookup(job_id: str, url: str | None = None) -> None:
    """Record a web lookup for a LinkedIn job; `url` is the original posting if one was found."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET lookup_at=?, url=COALESCE(?, url) WHERE id=?", (now(), url, job_id))


def delete_job(job_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=? AND id NOT IN (SELECT job_id FROM applications)", (job_id,))


def board_stats() -> dict[tuple[str, str], dict[str, int]]:
    """Per board: total jobs seen, jobs passing the filters, applications submitted."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT ats, board, COUNT(*) AS total, "
            "SUM(status NOT IN ('filtered_out', 'closed')) AS passing, SUM(status='applied') AS applied "
            "FROM jobs GROUP BY ats, board").fetchall()
    return {(r["ats"], r["board"]): {"total": r["total"], "passing": r["passing"] or 0, "applied": r["applied"] or 0}
            for r in rows}


def handled_roles(days: int) -> list[sqlite3.Row]:
    """Jobs you've applied to, skipped, or have waiting, updated within `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT id, company, title, status, COALESCE(applied_at, updated_at) AS at FROM jobs "
            "WHERE status IN (?, ?, ?, ?, ?) AND COALESCE(applied_at, updated_at) >= ?",
            (APPLIED, SKIPPED, READY, SUBMITTING, NEEDS_MANUAL, since)).fetchall()


def search_jobs(statuses: list[str], q: str = "", limit: int = 100, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    where = f"status IN ({','.join('?' * len(statuses))})"
    args: list[Any] = list(statuses)
    if q:
        where += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR status_reason LIKE ?)"
        args += [f"%{q}%"] * 4
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY COALESCE(score, -1) DESC, updated_at DESC "
                            f"LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return rows, int(total)


def linkedin_jobs() -> list[sqlite3.Row]:
    """LinkedIn-only jobs for the LinkedIn page: scored best-first, then not looked up yet, then not found."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status=? AND ats='linkedin' ORDER BY score IS NULL, score DESC, "
            "lookup_at IS NOT NULL, COALESCE(title_fit, -1) DESC, discovered_at DESC", (MANUAL_LINKEDIN,)).fetchall()


def linkedin_to_enrich(limit: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE status=? AND ats='linkedin' AND lookup_at IS NULL "
                            "ORDER BY discovered_at DESC LIMIT ?", (MANUAL_LINKEDIN, limit)).fetchall()


def kv_json(key: str, default: Any) -> Any:
    raw = kv_get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def kv_set_json(key: str, value: Any) -> None:
    kv_set(key, json.dumps(value))


def mark_closed(ats: str, board: str, live_ids: set[str]) -> int:
    """Jobs that vanished from a board before we got to them are closed."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE ats=? AND board=? AND status IN (?, ?, ?)",
            (ats, board, NEW, PREFILTERED, MATCHED),
        ).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in live_ids]
        conn.executemany(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?", [(CLOSED, now(), g) for g in gone]
        )
        return len(gone)


def status_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------- applications

def start_application(job_id: str, dry_run: bool) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO applications (job_id, started_at, dry_run) VALUES (?,?,?)",
            (job_id, now(), int(dry_run)),
        )
        return int(cur.lastrowid)


def finish_application(app_id: int, outcome: str, detail: str | None, artifacts: dict[str, Any],
                       answers: list[dict[str, Any]], log: list[str]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE applications SET finished_at=?, outcome=?, detail=?, artifacts_json=?, "
            "answers_json=?, log=? WHERE id=?",
            (now(), outcome, detail, json.dumps(artifacts), json.dumps(answers), "\n".join(log), app_id),
        )


def update_answers(app_id: int, answers: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute("UPDATE applications SET answers_json=? WHERE id=?", (json.dumps(answers), app_id))


def update_artifacts(app_id: int, artifacts: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("UPDATE applications SET artifacts_json=? WHERE id=?", (json.dumps(artifacts), app_id))


def drive_links() -> dict[str, str]:
    """job id -> Drive folder of its latest submitted application."""
    with connect() as conn:
        rows = conn.execute("SELECT job_id, artifacts_json FROM applications WHERE outcome='applied' ORDER BY id").fetchall()
    out = {}
    for r in rows:
        link = json.loads(r["artifacts_json"] or "{}").get("drive_folder")
        if link:
            out[r["job_id"]] = link
    return out


def applications_for(job_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE job_id=? ORDER BY id DESC", (job_id,)
        ).fetchall()


def recent_applications(limit: int = 25) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT a.*, j.company, j.title, j.score FROM applications a JOIN jobs j ON j.id = a.job_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,)
        ).fetchall()


def submitted_since(hours: float) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE outcome='applied' AND finished_at >= ?", (since,)
        ).fetchone()
    return int(row["n"])


COMPANY_LIVE = ("ready", "needs_manual", "submitting")


def company_roles(company: str, days: int = 30) -> list[sqlite3.Row]:
    """Every role at this company that's in play: applied recently, prepared, queued or held."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE lower(company)=lower(?) AND (status IN (?,?,?,?,?) OR (status=? AND applied_at >= ?)) "
            "ORDER BY status='applied' DESC, COALESCE(score, 0) DESC",
            (company, READY, NEEDS_MANUAL, SUBMITTING, MATCHED, HELD, APPLIED, since)).fetchall()


def company_slot(company: str, exclude_id: str, days: int = 30) -> sqlite3.Row | None:
    """The role that already uses this company's one slot: one applied to in the last `days`,
    else one prepared or being submitted."""
    for r in company_roles(company, days):
        if r["id"] != exclude_id and (r["status"] == APPLIED or r["status"] in COMPANY_LIVE):
            return r
    return None


def company_submissions(company: str, days: int) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications a JOIN jobs j ON j.id = a.job_id "
            "WHERE a.outcome='applied' AND lower(j.company)=lower(?) AND a.finished_at >= ?",
            (company, since),
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- runs

def start_run() -> int:
    with connect() as conn:
        return int(conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid)


def finish_run(run_id: int, stats: dict[str, Any], error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, stats_json=?, error=? WHERE id=?",
            (now(), json.dumps(stats), error, run_id),
        )


def last_runs(limit: int = 5) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------- kv (dashboard overrides)

def kv_get(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def is_paused() -> bool:
    return kv_get("paused", "0") == "1"


def set_paused(on: bool) -> None:
    """Pause or resume, remembering when a pause started (the Overview says so)."""
    if on and not is_paused():
        kv_set("paused_at", now())
    kv_set("paused", "1" if on else "0")


def paused_since() -> str | None:
    return kv_get("paused_at") if is_paused() else None


# Settings the dashboard can change at runtime (override config.yaml until changed again).
MODES = ("approve", "auto")


def apply_mode(config_default: str) -> str:
    # Submitting from the server is retired (bot checks, emailed codes): every draft is filled in
    # your own Chrome by the extension and you press Submit.
    return "approve"


def setting_int(key: str, config_default: int) -> int:
    value = kv_get(key)
    try:
        return int(value) if value is not None else config_default
    except ValueError:
        return config_default


def prepared_since(hours: float) -> int:
    """New drafts in the last `hours`: jobs whose first draft falls in that window. Re-preparing a
    job that already had a draft doesn't use up the daily limit."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT MIN(started_at) AS first FROM applications WHERE dry_run=1 "
            "AND COALESCE(outcome, '') NOT IN ('materials', 'interrupted') GROUP BY job_id) WHERE first >= ?", (since,)
        ).fetchone()
    return int(row["n"])


def latest_prepared(job_id: str) -> sqlite3.Row | None:
    """The most recent prepared-but-not-submitted attempt for a job (what you approve)."""
    with connect() as conn:
        return conn.execute(
            # A Needs-you draft is prepared too: once you answer its questions it can be approved.
            "SELECT * FROM applications WHERE job_id=? AND dry_run=1 AND outcome IN ('dry_run', 'needs_manual') "
            "ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()


def recover_interrupted() -> dict[str, int]:
    """Run at startup. If the server died mid-cycle (out of memory, a deploy), close out the run
    and any half-done application so nothing stays stuck. A job that was being prepared goes back
    in the queue; one that was being submitted needs you to check whether it went through."""
    ts = now()
    with connect() as conn:
        runs = conn.execute("UPDATE runs SET finished_at=?, error=COALESCE(error, ?) WHERE finished_at IS NULL",
                            (ts, "Interrupted: the server restarted before this run finished")).rowcount
        stuck = conn.execute("SELECT id, job_id, dry_run FROM applications WHERE outcome IS NULL").fetchall()
        conn.execute("UPDATE applications SET finished_at=?, outcome='interrupted', "
                     "detail='Interrupted: the server restarted partway through' WHERE outcome IS NULL", (ts,))
    for app in stuck:
        job = get_job(app["job_id"])
        if job is None:
            continue
        if job["status"] == SUBMITTING or not app["dry_run"]:
            set_status(job["id"], NEEDS_MANUAL, "Interrupted while submitting; check whether it went through")
        elif job["status"] == MATCHED:
            _requeued_event(job["id"])
    # Approved jobs whose submit never started (the approval queue lives in memory): nothing
    # was sent, so they go back to Ready.
    for job in jobs_with_status(SUBMITTING):
        if not any(a["job_id"] == job["id"] for a in stuck):
            set_status(job["id"], READY, "Your approval was interrupted by a server restart; approve again")
    return {"runs": runs, "applications": len(stuck)}


def _requeued_event(job_id: str) -> None:
    with connect() as conn:
        _event(conn, job_id, "status", "Preparing was interrupted by a server restart; queued again")
