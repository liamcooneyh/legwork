"""Dashboard + scheduler in one process (what runs on Fly)."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .. import db
from .. import impact
from ..pipeline import LOOKUP_COST_ESTIMATE, RUN_STEPS, Pipeline, cycle_running, linkedin_lookups, prep_progress, progress
from ..profile import load_profile
from ..resume_diff import resume_changes
from .. import runtime
from ..settings import env, load_config
from ..sources import board_list, load_boards_file
from ..sources.resolve import BoardResolver

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
TEMPLATES.env.filters["q"] = lambda s: quote(str(s), safe="")
STATUS_LABELS = {
    "new": "new", "prefiltered": "waiting to score", "matched": "matched, queued", "ready": "ready to fill",
    "submitting": "submitting", "applied": "applied", "dry_run": "prepared", "needs_manual": "needs you",
    "manual_linkedin": "LinkedIn: apply yourself", "failed": "failed", "rejected": "poor fit",
    "screen": "waiting for title screen", "screened_out": "dropped by title screen",
    "filtered_out": "filtered out", "skipped": "skipped", "closed": "posting closed", "held": "held: same company",
}
TEMPLATES.env.filters["label"] = lambda s: STATUS_LABELS.get(s or "", (s or "").replace("_", " "))


def _fill_url(job) -> str | None:
    """The application form with the extension's cue: opening it fills the form in your Chrome."""
    from ..apply.ats import form_url

    if job["ats"] not in ("greenhouse", "lever", "ashby"):
        return None
    return form_url(job["ats"], job["board"], job["external_id"], job["apply_url"]) + "#aa-fill"


TEMPLATES.env.filters["fill_url"] = _fill_url


def _stamp(raw) -> dict | None:
    """Any timestamp the way every list shows it: "Oct 15, 10:43 AM" plus "7 days ago"
    (just the day when there's no time)."""
    from datetime import datetime, timezone

    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    n = int(days)
    plural = lambda k, unit: f"{k} {unit}{'' if k == 1 else 's'} ago"  # noqa: E731
    ago = ("today" if n == 0 else "yesterday" if n == 1 else plural(n, "day") if n < 14
           else plural(n // 7, "week") if n < 60 else plural(n // 30, "month"))
    # Some systems give only the day; show a time only when there is one.
    if re.search(r"\d{2}:\d{2}", str(raw)):
        from zoneinfo import ZoneInfo

        et = dt.astimezone(ZoneInfo("America/New_York"))
        when = et.strftime("%b %-d") + ("" if et.year == datetime.now().year else et.strftime(", %Y")) + et.strftime(", %-I:%M %p")
    else:  # a calendar day, not a moment: don't shift it across time zones
        when = dt.strftime("%b %-d") + ("" if dt.year == datetime.now().year else dt.strftime(", %Y"))
    return {"when": when, "ago": ago, "days": round(days, 2)}


def _posted(job) -> dict | None:
    """When the job was posted: the employer's own date when it's known, otherwise (LinkedIn-only
    jobs) the first time it showed up here, which LinkedIn alerts make an "at least this old"."""
    known = bool(job["posted_at"])
    st = _stamp(job["posted_at"] or job["discovered_at"])
    if st is None:
        return None
    label = "Posted" if known else "First seen"
    return {**st, "known": known, "label": label,
            "title": f"{label} {st['when']} ({st['ago']})" + ("" if known else ": the posting date isn't known, so it's at least this old")}


TEMPLATES.env.filters["stamp"] = _stamp
TEMPLATES.env.filters["posted"] = _posted


def _eastern(value, with_time: bool = True) -> str:
    """Timestamps on the dashboard in New York time, 12-hour: "Oct 20, 2:31 PM"."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(ZoneInfo("America/New_York"))
    return dt.strftime("%b %-d, %-I:%M %p") if with_time else dt.strftime("%b %-d, %Y")


TEMPLATES.env.filters["et"] = _eastern
_REMOTE = re.compile(r"\bremote\b|\banywhere\b|work from home|\bwfh\b|\bdistributed\b", re.I)
TEMPLATES.env.filters["remote"] = lambda j: bool(_REMOTE.search(f"{j['location'] or ''} {j['title'] or ''}"))
from ..llm import SCORE_PARTS  # noqa: E402  (score breakdown on the job page)
TEMPLATES.env.globals["SCORE_PARTS"] = SCORE_PARTS
TEMPLATES.env.filters["et_date"] = lambda v: _eastern(v, with_time=False)
TEMPLATES.env.filters["et_time"] = lambda v: _eastern(v).split(", ")[-1] if v else ""

security = HTTPBasic(auto_error=False)
scheduler = BackgroundScheduler(timezone="UTC")


def _cycle() -> None:
    Pipeline().run_cycle()


def _replies() -> None:
    from .. import replies

    pipeline = Pipeline(log=lambda m: None)
    try:
        replies.check(pipeline.cfg.replies, pipeline.llm)
    except Exception as e:  # mail hiccups must never take down the scheduler
        print(f"reply check failed: {e}")


def _backup() -> None:
    from .. import gdrive, profile_store

    if not gdrive.configured():
        return
    try:
        link = gdrive.backup(env.db_path, profile_store.to_yaml(profile_store.load()))
        db.kv_set("last_backup", f"{db.now()}|{link}")
    except Exception as e:
        db.kv_set("last_backup_error", f"{db.now()}|{str(e)[:200]}")


def start_cycle_in_background() -> bool:
    if cycle_running():
        return False
    threading.Thread(target=_cycle, name="cycle", daemon=True).start()
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    recovered = db.recover_interrupted()
    if any(recovered.values()):
        print(f"recovered after restart: {recovered}")
    cfg = load_config()
    if cfg.schedule.enabled:
        scheduler.add_job(_cycle, "interval", hours=cfg.schedule.interval_hours, id="cycle",
                          max_instances=1, coalesce=True, jitter=300)
    if cfg.replies.enabled:
        scheduler.add_job(_replies, "interval", minutes=cfg.replies.interval_minutes, id="replies",
                          max_instances=1, coalesce=True, jitter=60)
    # Nightly copy of the database and profile to Google Drive (about 3am New York time).
    scheduler.add_job(_backup, "cron", hour=7, minute=15, id="backup", max_instances=1, coalesce=True)
    if scheduler.get_jobs():
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def require_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    password = env.dashboard_password
    if password is None:
        if os.environ.get("FLY_APP_NAME"):
            raise HTTPException(503, "Set DASHBOARD_PASSWORD (fly secrets set DASHBOARD_PASSWORD=...)")
        return  # local development
    ok = creds is not None and secrets.compare_digest(creds.username.encode(), env.dashboard_user.encode()) \
        and secrets.compare_digest(creds.password.encode(), password.encode())
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="autoapply"'})
    if request.method == "POST":
        # Browsers resend basic-auth credentials cross-site; only accept same-origin form posts.
        # The Mac helper isn't a browser (it sends no Origin), and may only use /api/.
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if request.url.path.startswith("/api/") and (not origin or origin.startswith("chrome-extension://")):
            return
        if urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-site request blocked")


PRIVACY = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legwork · Privacy</title>
<style>body{font:16px/1.55 -apple-system,Helvetica,Arial,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;color:#1b1f24;background:#fff}
@media (prefers-color-scheme: dark){body{color:#e6e9ed;background:#101317}a{color:#6fa8dc}}</style></head><body>
<h1>Legwork</h1>
<p>Legwork is a personal tool run by {owner} for their own job search. It is not offered to anyone else.</p>
<h2>Privacy</h2>
<ul>
<li><b>Gmail</b> (read-only): reads LinkedIn job alert emails and replies from companies about the owner's job applications, to track application status. Emails are not modified, forwarded, or shared.</li>
<li><b>Google Drive</b> (<code>drive.file</code>): creates folders holding copies of the owner's submitted resumes, cover letters, and application answers. It can only access files it created.</li>
<li>Email excerpts and job postings are sent to Anthropic's Claude API to classify replies and prepare applications. Nothing is sold or shared with anyone else.</li>
<li>Data is stored on a private server and deleted when the owner deletes the app.</li>
</ul>
{contact}
</body></html>"""


@app.get("/favicon.svg")
def favicon() -> Response:
    """Pip's face, for the browser tab (no sign-in needed: browsers fetch it on their own)."""
    return Response((Path(__file__).parent / "static" / "favicon.svg").read_text(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> str:
    # Who runs this copy, for Google's app review: OWNER_NAME and CONTACT_EMAIL (in fly.toml's [env]).
    from html import escape

    owner = escape(os.environ.get("OWNER_NAME", "").strip() or "its owner")
    email = escape(os.environ.get("CONTACT_EMAIL", "").strip())
    return PRIVACY.replace("{owner}", owner).replace("{contact}", f"<p>Contact: {email}</p>" if email else "")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


def _ctx(request: Request, **extra) -> dict:
    cfg = runtime.config()
    return {
        "request": request,
        "paused": db.is_paused(),
        "mode": db.apply_mode(cfg.apply.mode),
        "running": cycle_running(),
        "cfg": cfg,
        "bg_motion": db.kv_get("bg_motion") != "0",  # animate the background even with reduced motion on
        **extra,
    }


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def index(request: Request):
    cfg = runtime.config()
    next_run = scheduler.get_job("cycle").next_run_time if scheduler.running and scheduler.get_job("cycle") else None
    return TEMPLATES.TemplateResponse(request, "index.html", _ctx(
        request,
        counts=db.status_counts(),
        submitted_24h=db.submitted_since(24),
        drafts_24h=db.prepared_since(24),
        ready=db.jobs_with_status(db.READY, limit=100),
        submitting=db.jobs_with_status(db.SUBMITTING, limit=20),
        needs_manual=db.jobs_with_status(db.NEEDS_MANUAL, limit=50),
        manual_linkedin=db.jobs_with_status(db.MANUAL_LINKEDIN, limit=10),
        recent=db.recent_applications(20),
        replies=db.email_events(8),
        runs=db.last_runs(5),
        next_run=next_run,
        max_per_day=db.setting_int("max_per_day", cfg.apply.max_per_day),
        board_count=len(board_list(cfg.search)),
        impact=impact.summary(min_score=db.setting_int("min_score", cfg.matching.min_score)),
        run_steps=RUN_STEPS,
        live=progress.snapshot(),
    ))


@app.post("/job/{job_id:path}/answer", dependencies=[Depends(require_auth)])
async def answer_needs(request: Request, job_id: str):
    """Your answers to a Needs-you draft's questions. Once none are left it's ready to fill,
    and approving fills the form with the saved answers plus yours."""
    from .. import profile_store
    from ..apply.mapping import question_text

    job, draft = db.get_job(job_id), db.latest_prepared(job_id)
    if job is None or draft is None:
        raise HTTPException(404)
    form = await request.form()
    answers = json.loads(draft["answers_json"] or "[]")
    pending = [a for a in answers if a.get("source") == "unanswered"]
    remembered, answered = [], 0
    i = 0
    while f"q_{i}" in form:
        label = str(form[f"q_{i}"])
        entry = pending[i] if i < len(pending) and pending[i]["label"] == label else None
        values = [str(v).strip() for v in form.getlist(f"a_{i}") if str(v).strip()]
        if entry is not None:
            if entry.get("type") == "check" and values:
                # A correction for a field the screenshot check flagged: it replaces that field's answer.
                entry.update(source="you", type="text", value="; ".join(values), values=values)
                answered += 1
            elif entry.get("type") == "check" and form.get(f"ok_{i}"):
                entry.update(source="you_checked", value="You checked this: fine as is")
                answered += 1
            elif values:
                entry.update(source="you", value="; ".join(values), values=values)
                answered += 1
                if form.get(f"remember_{i}"):
                    remembered.append({"question": question_text(label)[:300], "answer": "; ".join(values)})
        i += 1
    db.update_answers(draft["id"], answers)
    if remembered:
        data = profile_store.load().model_dump()
        known = {a["question"].lower() for a in data["standard_answers"]}
        data["standard_answers"] += [r for r in remembered if r["question"].lower() not in known]
        profile_store.save(data, f"Remembered {len(remembered)} answer(s) from {job['company']}")
    left = sum(1 for a in answers if a.get("source") == "unanswered")
    if left == 0:
        db.set_status(job_id, db.READY, f"Ready to fill in Chrome (you answered {answered})")
        msg = "All answered: it's ready. Open it in Chrome and the extension fills in everything, these answers included."
    else:
        msg = f"Saved {answered} answer(s). {left} still to go."
    if remembered:
        msg += f" Remembered {len(remembered)} for future applications."
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(msg)}", status_code=303)


# ---------------------------------------------------------------- Mac helper: opens a draft pre-filled, you submit

HELPER_ONLINE_SECONDS = 20


def helper_seen_seconds() -> float | None:
    seen = db.kv_get("helper_seen")
    if not seen:
        return None
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(seen)).total_seconds()


def helper_online() -> bool:
    s = helper_seen_seconds()
    return s is not None and s < HELPER_ONLINE_SECONDS


@app.post("/job/{job_id:path}/open", dependencies=[Depends(require_auth)])
def open_on_mac(job_id: str):
    """Ask the Mac helper to open this draft pre-filled in a browser window for you to submit."""
    if db.get_job(job_id) is None or db.latest_prepared(job_id) is None:
        raise HTTPException(404)
    queue = [q for q in db.kv_json("open_queue", []) if q["job_id"] != job_id]
    queue.append({"job_id": job_id, "at": db.now()})
    db.kv_set_json("open_queue", queue)
    db.add_event(job_id, "helper", "Asked the Mac helper to open it pre-filled")
    msg = ("Opening on your Mac in a few seconds: review it there and press Submit yourself." if helper_online() else
           "Your Mac helper isn't running, so this will open as soon as it starts. On your Mac: "
           "cd ~/Developer/jobs && .venv/bin/autoapply helper")
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(msg)}", status_code=303)


@app.get("/api/helper/next", dependencies=[Depends(require_auth)])
def helper_next() -> dict:
    """Polled by the Mac helper every few seconds: a heartbeat, and the next draft to open."""
    from datetime import datetime, timedelta, timezone

    db.kv_set("helper_seen", db.now())
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    queue = [q for q in db.kv_json("open_queue", []) if datetime.fromisoformat(q["at"]) > cutoff]
    if not queue:
        db.kv_set_json("open_queue", [])
        return {}
    request, rest = queue[0], queue[1:]
    db.kv_set_json("open_queue", rest)
    return draft_payload(request["job_id"])


def draft_payload(job_id: str) -> dict:
    """Everything needed to fill a job's application: answers, file links, where you live."""
    from .. import profile_store
    from ..apply.mapping import US_STATES

    job, draft = db.get_job(job_id), db.latest_prepared(job_id)
    if job is None or draft is None:
        return {}
    art = json.loads(draft["artifacts_json"] or "{}")
    files = {k: f"/artifacts/{art['dir']}/{art[k]}" for k in ("resume", "cover_letter") if art.get(k)}
    if art.get("cover_letter"):
        files["cover_letter_text"] = f"/artifacts/{art['dir']}/cover_letter.txt"
    answers = [a for a in json.loads(draft["answers_json"] or "[]") if a.get("type") != "file"]
    profile = profile_store.load()
    loc = profile.personal.location
    return {"job": {k: job[k] for k in ("id", "ats", "board", "external_id", "apply_url", "url", "company", "title", "source")},
            "draft_id": draft["id"], "files": files, "answers": answers, "status": job["status"],
            "location": {"city": loc.city, "state": loc.state, "state_name": US_STATES.get(loc.state.upper(), loc.state),
                         "country": loc.country},
            "profile_yaml": profile_store.to_yaml(profile)}


@app.get("/api/draft", dependencies=[Depends(require_auth)])
def api_draft(url: str = "", job_id: str = "") -> dict:
    """For the Chrome extension: the prepared draft for the application page you're on."""
    from ..sources import detect_from_url

    if not job_id:
        hit = detect_from_url(url)
        if hit is None:
            return {}
        job_id = f"{hit[0]}:{hit[1]}:{hit[2]}"
    return draft_payload(job_id)


def record_applied(job_id: str, via: str, detail: str) -> None:
    """You submitted it yourself: keep what was sent (the draft's resume, letter and answers),
    copy it to Drive, and start reply tracking."""
    from ..pipeline import copy_to_drive

    job, draft = db.get_job(job_id), db.latest_prepared(job_id)
    if job["status"] == db.APPLIED:  # already recorded (the extension can see the same submit twice)
        db.add_event(job_id, "helper", f"Submit seen again: {detail}")
        return
    art = json.loads(draft["artifacts_json"] or "{}") if draft else {}
    answers = json.loads(draft["answers_json"] or "[]") if draft else []
    art = {**art, "submitted_by": f"you, {via}"}
    app_id = db.start_application(job_id, False)
    if draft:
        copy_to_drive(dict(job), env.artifacts_dir / art.get("dir", ""), art, answers)
    db.finish_application(app_id, "applied", detail, art, answers, [f"Submitted by you, {via}"])
    db.set_status(job_id, db.APPLIED, f"You submitted it ({via})")


@app.post("/api/job/{job_id:path}/helper", dependencies=[Depends(require_auth)])
async def helper_event(request: Request, job_id: str) -> dict:
    """What happened in the window on your Mac: opened, applied (you submitted), closed, or error."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    body = await request.json()
    event, detail = body.get("event"), str(body.get("detail", ""))[:500]
    if event == "applied":
        via = "in your Chrome (filled by the extension)" if body.get("via") == "extension" else "in the Mac helper's window"
        record_applied(job_id, via, detail or "You submitted it; confirmation page seen")
    else:
        db.add_event(job_id, "helper", {"opened": "Opened pre-filled on your Mac", "filled": "Filled in your Chrome by the extension",
                                        "closed": "Window closed without submitting",
                                        "error": f"Mac helper error: {detail}"}.get(event, f"{event}: {detail}"))
    return {"ok": True}


@app.get("/impact", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def impact_page(request: Request):
    cfg = runtime.config()
    return TEMPLATES.TemplateResponse(request, "impact.html", _ctx(
        request, s=impact.summary(min_score=db.setting_int("min_score", cfg.matching.min_score))))


@app.get("/progress", dependencies=[Depends(require_auth)])
def run_progress() -> dict:
    """Live status of the current run, polled by the Overview."""
    return {"running": cycle_running(), "steps": RUN_STEPS, **progress.snapshot()}


# ---------------------------------------------------------------- LinkedIn-only jobs (looked up only when you ask)

@app.get("/linkedin", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def linkedin_page(request: Request, msg: str = ""):
    rows = db.linkedin_jobs()
    todo = [r for r in rows if r["lookup_at"] is None and r["score"] is None]
    with db.connect() as conn:
        reposts = conn.execute("SELECT COUNT(*) FROM jobs WHERE status=? AND status_reason LIKE 'Recruiter or job-board repost%'",
                               (db.SKIPPED,)).fetchone()[0]
    return TEMPLATES.TemplateResponse(request, "linkedin.html", _ctx(
        request, rows=rows, todo=len(todo), est=len(todo) * LOOKUP_COST_ESTIMATE, per=LOOKUP_COST_ESTIMATE, reposts=reposts,
        scored=sum(r["score"] is not None for r in rows),
        not_found=sum(r["lookup_at"] is not None and r["score"] is None for r in rows),
        queued=linkedin_lookups.queued(), lookup_running=linkedin_lookups.running(),
        lookup_live=linkedin_lookups.progress.snapshot(), last=db.kv_json("linkedin_lookup_last", None), msg=msg,
    ))


@app.post("/linkedin/lookup", dependencies=[Depends(require_auth)])
def linkedin_lookup(job_id: str = Form(""), back: str = Form(""), budget: float = Form(0)):
    if job_id:
        ids, cap = [job_id], None
    else:  # best title fit first, as many as the budget covers
        todo = [r["id"] for r in db.linkedin_jobs() if r["lookup_at"] is None and r["score"] is None]
        cap = max(0.0, min(budget, 20.0)) or None
        ids = todo[:int(cap / LOOKUP_COST_ESTIMATE) + 1] if cap else todo
    n = linkedin_lookups.add(ids, budget=cap)
    msg = (f"Looking up {'the best ' if cap else ''}{n} LinkedIn job{'s' if n != 1 else ''} in the background"
           + (f", stopping at ${cap:.2f}." if cap else ".") if n else "Nothing new to look up.")
    to = f"/job/{quote(job_id, safe='')}" if back == "job" and job_id else "/linkedin"
    return RedirectResponse(f"{to}?msg={quote(msg)}", status_code=303)


@app.post("/linkedin/cancel", dependencies=[Depends(require_auth)])
def linkedin_cancel():
    linkedin_lookups.cancel()
    return RedirectResponse("/linkedin?msg=" + quote("Stopping after the current job."), status_code=303)


@app.get("/linkedin/progress", dependencies=[Depends(require_auth)])
def linkedin_progress() -> dict:
    return {"running": linkedin_lookups.running(), "results": linkedin_lookups.results, **linkedin_lookups.progress.snapshot()}


@app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def jobs(request: Request, status: str | None = None):
    if status and status not in db.ALL_STATUSES:
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, "jobs.html", _ctx(
        request, jobs=db.list_jobs(status, limit=500), status=status, statuses=db.ALL_STATUSES,
        counts=db.status_counts(),
    ))


@app.get("/job/{job_id:path}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def job_detail(request: Request, job_id: str, msg: str = ""):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    applications = []
    from .. import profile_store

    profiles: dict = {}

    def profile_for(version):
        # Compare against the profile version the application was prepared with.
        if version not in profiles:
            try:
                profiles[version] = profile_store.load(version) if version else load_profile()
            except (FileNotFoundError, ValueError):
                profiles[version] = None
        return profiles[version]

    for a in db.applications_for(job_id):
        art = json.loads(a["artifacts_json"] or "{}")
        changes = None
        profile = profile_for(art.get("profile_version"))
        resume_json = env.artifacts_dir / art.get("dir", "_none_") / "resume.json"
        if profile and resume_json.is_file():
            try:
                changes = resume_changes(profile, json.loads(resume_json.read_text()))
            except (ValueError, KeyError):
                changes = None
        cover_txt = env.artifacts_dir / art.get("dir", "_none_") / "cover_letter.txt"
        applications.append({"row": a, "art": art, "answers": json.loads(a["answers_json"] or "[]"),
                             "changes": changes, "cover_text": cover_txt.read_text() if cover_txt.is_file() else None})
    from ..apply.ats import form_url

    needs = []
    has_draft = db.latest_prepared(job_id) is not None
    fill_url = _fill_url(job)
    if job["status"] == db.NEEDS_MANUAL:
        draft = db.latest_prepared(job_id)
        if draft is not None:
            needs = [a for a in json.loads(draft["answers_json"] or "[]") if a.get("source") == "unanswered"]
    return TEMPLATES.TemplateResponse(request, "job.html", _ctx(
        request, job=job, applications=applications, events=db.events_for(job_id), stages=db.TRACK_STAGES, msg=msg,
        needs=needs, has_draft=has_draft, fill_url=fill_url,
        preparing_now=job_id in _preparing_now or prep_progress(job_id) is not None,
        other_roles=[r for r in db.company_roles(job["company"]) if r["id"] != job_id],
    ))


ACTIONS = {
    "retry": (db.PREFILTERED, "Queued again by hand: re-scored, then re-prepared if it still fits"),
    "skip": (db.SKIPPED, "Skipped by hand"),
    "applied_myself": (db.APPLIED, "Applied by hand"),
    "rescore": (db.PREFILTERED, None),
}


@app.post("/job/{job_id:path}/action", dependencies=[Depends(require_auth)])
def job_action(job_id: str, action: str = Form(...), back: str = Form("")):
    job = db.get_job(job_id)
    if job is None or action not in ACTIONS:
        raise HTTPException(404)
    if action == "applied_myself" and db.latest_prepared(job_id) is not None:
        record_applied(job_id, "marked applied on the dashboard", "You marked it applied")
    else:
        status, reason = ACTIONS[action]
        db.set_status(job_id, status, reason)
    target = "/" if back == "home" else f"/job/{quote(job_id, safe='')}"
    return RedirectResponse(target, status_code=303)


_preparing_now: set[str] = set()


@app.post("/job/{job_id:path}/prepare-now", dependencies=[Depends(require_auth)])
def prepare_now(job_id: str):
    """Prepare (or re-prepare) just this job, right away, in the background: a fresh tailored resume,
    cover letter and answers. Doesn't touch the rest of the queue or wait for a run."""
    from ..pipeline import browser_session, prep_step

    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    if job_id not in _preparing_now:
        _preparing_now.add(job_id)
        overrides = set(db.kv_json("company_overrides", []))
        overrides.add(job_id)  # your explicit choice, even if another role at this company is in play
        db.kv_set_json("company_overrides", sorted(overrides))
        db.add_event(job_id, "status", "You asked for a fresh draft now")

        def work():
            try:
                pipeline = Pipeline()
                # Score it against your current profile first, so the draft and the score agree.
                prep_step(job_id, "score")
                try:
                    if not pipeline.score_one(job_id):
                        db.add_event(job_id, "status", "Re-scored below your cutoff; preparing anyway since you asked")
                except Exception as e:
                    db.add_event(job_id, "status", f"Re-scoring failed, preparing with the old score: {str(e)[:120]}")
                with browser_session(pipeline.cfg.apply.headless) as browser:
                    pipeline.apply_one(browser, dict(db.get_job(job_id)), dry_run=True)
            except Exception as e:
                db.add_event(job_id, "status", f"Preparing failed: {str(e)[:200]}")
            finally:
                _preparing_now.discard(job_id)

        threading.Thread(target=work, name=f"prepare-{job_id}", daemon=True).start()
    msg = "Preparing a fresh draft now: about 1–2 minutes. Refresh this page to see it."
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(msg)}", status_code=303)


@app.get("/prepare-progress/{job_id:path}", dependencies=[Depends(require_auth)])
def prepare_progress(job_id: str) -> dict:
    from ..pipeline import PREP_STEPS, prep_progress

    p = prep_progress(job_id)
    return {"running": job_id in _preparing_now or p is not None, "steps": PREP_STEPS, **(p or {})}


@app.post("/job/{job_id:path}/company-swap", dependencies=[Depends(require_auth)])
def company_swap(job_id: str, keep_other: str = Form("")):
    """One role per company: prepare this one instead of (or as well as) the one already chosen."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    overrides = set(db.kv_json("company_overrides", []))
    overrides.add(job_id)
    moved = []
    if not keep_other:  # instead: the company's other prepared or queued roles go on hold
        for r in db.company_roles(job["company"]):
            if r["id"] != job_id and r["status"] in (db.READY, db.NEEDS_MANUAL, db.MATCHED):
                db.set_status(r["id"], db.HELD, f"Held: you chose {job['title']} ({job['score']}) at {job['company']} instead")
                overrides.discard(r["id"])
                moved.append(r["title"])
    db.kv_set_json("company_overrides", sorted(overrides))
    if job["status"] in (db.HELD, db.SKIPPED, db.REJECTED):
        db.set_status(job_id, db.MATCHED, "You chose this role at this company" + (" (as well)" if keep_other else ""))
    msg = ("Queued: it's prepared on the next run (or press Run now)."
           + (f" Put on hold instead: {', '.join(t[:50] for t in moved)}." if moved else ""))
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(msg)}", status_code=303)


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def settings_page(request: Request, saved: int = 0):
    cfg = load_config()
    from .. import gdrive
    from ..sources import mailbox

    last_backup = (db.kv_get("last_backup") or "").split("|", 1)
    return TEMPLATES.TemplateResponse(request, "settings.html", _ctx(
        request, saved=saved, drive_on=gdrive.configured(), mail_on=mailbox.configured() is not None,
        last_backup=last_backup if len(last_backup) == 2 else None,
        max_per_day=db.setting_int("max_per_day", cfg.apply.max_per_day),
        min_score=db.setting_int("min_score", cfg.matching.min_score),
        impact_cfg=impact.settings(),
    ))


@app.post("/settings", dependencies=[Depends(require_auth)])
def save_settings(max_per_day: int = Form(...), min_score: int = Form(...), paused: str = Form(""),
                  manual_minutes: float = Form(impact.MANUAL_MINUTES), screen_minutes: float = Form(impact.SCREEN_MINUTES),
                  bg_motion: str = Form("")):
    db.kv_set("max_per_day", str(max(0, min(max_per_day, 200))))
    db.kv_set("impact_manual_minutes", str(max(0.0, min(manual_minutes, 240.0))))
    db.kv_set("impact_screen_minutes", str(max(0.0, min(screen_minutes, 30.0))))
    db.kv_set("bg_motion", "1" if bg_motion else "0")
    db.kv_set("min_score", str(max(0, min(min_score, 100))))
    db.kv_set("paused", "1" if paused else "0")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/toggle", dependencies=[Depends(require_auth)])
def toggle(name: str = Form(...)):
    cfg = load_config()
    if name == "paused":
        db.kv_set("paused", "0" if db.is_paused() else "1")
    else:
        raise HTTPException(404)
    return RedirectResponse("/", status_code=303)


@app.post("/notify-test", dependencies=[Depends(require_auth)])
def notify_test():
    from .. import notify

    notify.send("Test from Legwork: notifications are working.")
    return RedirectResponse("/", status_code=303)


@app.post("/backup", dependencies=[Depends(require_auth)])
def backup_now():
    threading.Thread(target=_backup, name="backup", daemon=True).start()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/run", dependencies=[Depends(require_auth)])
def run_now():
    start_cycle_in_background()
    return RedirectResponse("/", status_code=303)


@app.get("/artifacts/{path:path}", dependencies=[Depends(require_auth)])
def artifact(path: str):
    root = env.artifacts_dir.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


# ---------------------------------------------------------------- application tracking

@app.get("/applications", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def applications_page(request: Request, stage: str | None = None, checking: int = 0):
    from datetime import datetime, timezone

    rows = db.tracked_jobs(stage)
    now = datetime.now(timezone.utc)
    items = []
    for j in rows:
        applied = j["applied_at"] or j["updated_at"]
        try:
            days = (now - datetime.fromisoformat(applied)).days
        except ValueError:
            days = None
        items.append({"job": j, "days": days})
    return TEMPLATES.TemplateResponse(request, "applications.html", _ctx(
        request, items=items, stage=stage, stages=db.TRACK_STAGES, stage_counts=db.stage_counts(),
        last_email=db.last_email_by_job(), replies=db.email_events(15), checking=checking, drive=db.drive_links(),
    ))


@app.post("/job/{job_id:path}/drive", dependencies=[Depends(require_auth)])
def copy_job_to_drive(job_id: str):
    from ..pipeline import copy_to_drive

    job = db.get_job(job_id)
    sent = next((a for a in db.applications_for(job_id) if a["outcome"] == "applied"), None)
    if job is None or sent is None:
        raise HTTPException(404, "No submitted application to copy")
    art = json.loads(sent["artifacts_json"] or "{}")
    copy_to_drive(dict(job), env.artifacts_dir / art.get("dir", ""), art, json.loads(sent["answers_json"] or "[]"))
    db.update_artifacts(sent["id"], art)
    return RedirectResponse(f"/job/{quote(job_id, safe='')}", status_code=303)


# ---------------------------------------------------------------- filtered out / rescue

FILTERED_TABS = {
    "screen": ([db.SCREENED_OUT], "Dropped by the AI title screen"),
    "filters": ([db.FILTERED_OUT], "Dropped by the free filters"),
    "poor_fit": ([db.REJECTED], "Scored a poor fit"),
}


@app.get("/filtered", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def filtered_page(request: Request, tab: str = "screen", q: str = "", offset: int = 0, msg: str = ""):
    if tab not in FILTERED_TABS:
        tab = "screen"
    statuses, heading = FILTERED_TABS[tab]
    rows, total = db.search_jobs(statuses, q.strip(), limit=100, offset=max(0, offset))
    counts = db.status_counts()
    return TEMPLATES.TemplateResponse(request, "filtered.html", _ctx(
        request, rows=rows, total=total, tab=tab, heading=heading, q=q, offset=max(0, offset), msg=msg,
        tab_counts={k: sum(counts.get(s, 0) for s in v[0]) for k, v in FILTERED_TABS.items()},
        examples=db.kv_json("screen_examples", []),
    ))


def _score_in_background(job_id: str) -> None:
    def work():
        try:
            Pipeline(log=lambda m: None).score_one(job_id)
        except Exception as e:
            db.set_status(job_id, db.PREFILTERED, f"Scoring will retry on the next run ({e})")
    threading.Thread(target=work, name=f"score-{job_id}", daemon=True).start()


@app.post("/job/{job_id:path}/rescue", dependencies=[Depends(require_auth)])
def rescue(job_id: str, back: str = Form("")):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    if job["status"] in (db.SCREENED_OUT, db.FILTERED_OUT):
        # Teach the title screen: similar titles are kept from now on.
        examples = [t for t in db.kv_json("screen_examples", []) if t != job["title"]] + [job["title"]]
        db.kv_set_json("screen_examples", examples[-60:])
    db.set_status(job_id, db.PREFILTERED, "You marked this relevant")
    _score_in_background(job_id)
    note = quote(f"Rescued “{job['title']}” at {job['company']}. It's being scored now; refresh in a few seconds.")
    if back.startswith("/filtered"):
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={note}", status_code=303)
    return RedirectResponse(f"/job/{quote(job_id, safe='')}", status_code=303)


@app.post("/job/{job_id:path}/materials", dependencies=[Depends(require_auth)])
def make_materials(job_id: str):
    if db.get_job(job_id) is None:
        raise HTTPException(404)

    def work():
        Pipeline(log=lambda m: None).prepare_materials(job_id)

    threading.Thread(target=work, name=f"materials-{job_id}", daemon=True).start()
    msg = quote("Preparing a tailored resume and cover letter; refresh in about a minute.")
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={msg}", status_code=303)


BOOKMARKLET = "(function(){var q=function(s){var e=document.querySelector(s);return e?e.innerText.trim():''};var t=q('.job-details-jobs-unified-top-card__job-title')||q('.top-card-layout__title')||q('h1');var c=q('.job-details-jobs-unified-top-card__company-name')||q('.topcard__org-name-link')||(document.title.split(' | ')[1]||'');var l=(q('.job-details-jobs-unified-top-card__primary-description-container')||q('.job-details-jobs-unified-top-card__tertiary-description-container')||q('.topcard__flavor--bullet')).split('\\n')[0].split(' · ')[0];var d=q('#job-details')||q('.jobs-description__content')||q('.description__text')||document.body.innerText;var m=location.href.match(/currentJobId=(\\d+)/)||location.href.match(/jobs\\/view\\/(\\d+)/);var u=m?'https://www.linkedin.com/jobs/view/'+m[1]+'/':location.href;window.open('__BASE__/import#data='+encodeURIComponent(JSON.stringify({url:u,title:t,company:c,location:l,description:d.slice(0,25000)})),'_blank');})();"


@app.get("/import", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def import_page(request: Request):
    from ..notify import dashboard_url

    base = dashboard_url() or str(request.base_url).rstrip("/")
    return TEMPLATES.TemplateResponse(request, "import.html", _ctx(
        request, bookmarklet="javascript:" + quote(BOOKMARKLET.replace("__BASE__", base), safe="()/:;,'=+!*~"),
    ))


@app.post("/import", dependencies=[Depends(require_auth)])
def import_job(url: str = Form(""), title: str = Form(""), company: str = Form(""), location: str = Form(""),
               description: str = Form("")):
    """A job you sent yourself (browser button or paste): store it and score it from its description."""
    import hashlib

    from ..sources import FETCHERS, detect_from_url

    url, title, company, description = url.strip(), title.strip(), company.strip(), description.strip()
    pipeline = Pipeline(log=lambda m: None)
    board_hit = detect_from_url(url) if url else None
    if board_hit and board_hit[0] in FETCHERS:  # a supported board: treat it as a normal, applyable job
        ats, board, ext = board_hit
        match = next((j for j in FETCHERS[ats](board) if j.external_id == ext), None)
        if match is not None:
            try:
                from ..sources import hydrate
                hydrate(match)
            except Exception:
                pass
            row = match.to_row()
            row["status"] = db.PREFILTERED
            if not db.upsert_job(row, source="manual_import"):
                db.set_status(row["id"], db.PREFILTERED, "You sent this job")
            job_id = row["id"]
        else:
            return RedirectResponse("/import?error=" + quote("That posting wasn't found on its board (closed?)."), status_code=303)
    else:
        if not description or len(description) < 200:
            return RedirectResponse("/import?error=" + quote("Paste the full job description (it looked empty)."), status_code=303)
        m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d{6,})|currentJobId=(\d{6,})", url)
        if m:
            ext = m.group(1) or m.group(2)
            job_id = f"linkedin:{ext}"
        else:
            ext = hashlib.sha1((url or f"{title}|{company}").encode()).hexdigest()[:12]
            job_id = f"manual:{ext}"
        existing = db.get_job(job_id)
        if existing is None:
            db.insert_jobs([{"id": job_id, "ats": "linkedin", "board": "", "external_id": ext, "company": company or "Unknown",
                             "title": title or "Untitled job", "location": location, "url": url or None,
                             "apply_url": url or "", "description": description, "status": db.MANUAL_LINKEDIN,
                             "status_reason": "You sent this job"}], source="manual_import")
            if m:
                db.link_linkedin(job_id, ext, f"https://www.linkedin.com/jobs/view/{ext}/")
        else:
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET description=?, title=COALESCE(NULLIF(?, ''), title), "
                             "company=COALESCE(NULLIF(?, ''), company), location=COALESCE(NULLIF(?, ''), location) "
                             "WHERE id=?", (description, title, company, location, job_id))
        db.set_lookup(job_id)  # no web search needed: you supplied the description
    if pipeline.llm is None:
        return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg=" + quote("Saved. (Claude isn't configured, so it wasn't scored.)"), status_code=303)
    try:
        ok = pipeline.score_one(job_id)
    except Exception as e:
        return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg=" + quote(f"Saved, but scoring failed: {e}"), status_code=303)
    job = db.get_job(job_id)
    if job["ats"] == "linkedin":
        db.set_status(job_id, db.MANUAL_LINKEDIN if ok else db.REJECTED,
                      "Scored from the description you sent" if ok else "Poor fit (scored from the description you sent)")
    msg = f"Scored {job['score']}/100" + (": a good fit." if ok else ".") + (
        " It's on a supported board, so it can be prepared and submitted from here." if job["ats"] != "linkedin" else "")
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(msg)}", status_code=303)


@app.post("/replies/check", dependencies=[Depends(require_auth)])
def check_replies_now():
    threading.Thread(target=_replies, name="replies", daemon=True).start()
    return RedirectResponse("/applications?checking=1", status_code=303)


@app.post("/job/{job_id:path}/track", dependencies=[Depends(require_auth)])
def track(job_id: str, stage: str = Form(""), notes: str | None = Form(None), back: str = Form("")):
    if db.get_job(job_id) is None:
        raise HTTPException(404)
    db.set_tracking(job_id, stage or None, notes)
    target = "/applications" if back == "applications" else f"/job/{quote(job_id, safe='')}"
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------- filters

def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.replace(",", "\n").splitlines() if l.strip()]


@app.get("/filters", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def filters_page(request: Request, now_passing: int | None = None, now_filtered_out: int | None = None):
    search = runtime.search_config()
    overridden = set(db.kv_json("search_overrides", {}))
    return TEMPLATES.TemplateResponse(request, "filters.html", _ctx(
        request, s=search, overridden=overridden, now_passing=now_passing, now_filtered_out=now_filtered_out,
        counts=db.status_counts(),
    ))


@app.post("/filters", dependencies=[Depends(require_auth)])
def save_filters(title_include: str = Form(""), title_exclude: str = Form(""), locations: str = Form(""),
                 location_exclude: str = Form(""), exclude_companies: str = Form(""), max_age_days: int = Form(30),
                 preferences: str = Form(""), allow_remote: str = Form(""), reset: str = Form("")):
    if reset:
        runtime.reset_filters()
    else:
        runtime.save_filters({
            "title_include": _lines(title_include), "title_exclude": _lines(title_exclude),
            "locations": _lines(locations), "location_exclude": _lines(location_exclude),
            "exclude_companies": _lines(exclude_companies), "max_age_days": max(0, max_age_days),
            "preferences": preferences.strip(), "allow_remote": bool(allow_remote),
        })
    result = runtime.refilter_all(runtime.search_config())
    passing = result["to_title_screen"] + result["fast_tracked"]
    return RedirectResponse(f"/filters?now_passing={passing}&now_filtered_out={result['now_filtered_out']}",
                            status_code=303)


# ---------------------------------------------------------------- companies

@app.get("/companies", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def companies_page(request: Request, msg: str = ""):
    search = runtime.search_config()
    from_config = {(a, b) for a in ("greenhouse", "lever", "ashby") for b in getattr(search.boards, a)}
    path = env.config_path.parent / search.boards_file
    from_file = {(a, b) for a, bs in load_boards_file(path).items() for b in bs}
    learned = set(db.learned_boards())
    added = {tuple(b.split(":", 1)) for b in db.kv_json("boards_added", [])}
    stats = db.board_stats()
    rows = []
    for ats, board in board_list(search):
        source = ("added here" if (ats, board) in added else "LinkedIn alert" if (ats, board) in learned - from_file - from_config
                  else "built-in list")
        rows.append({"ats": ats, "board": board, "source": source, **stats.get((ats, board), {"total": 0, "passing": 0, "applied": 0})})
    rows.sort(key=lambda r: (-r["passing"], r["board"]))
    removed = db.kv_json("boards_removed", [])
    return TEMPLATES.TemplateResponse(request, "companies.html", _ctx(request, rows=rows, removed=removed, msg=msg))


BOARD_URLS = [
    ("greenhouse", r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
]


def _board_from_url(text: str) -> tuple[str, str] | None:
    for ats, pattern in BOARD_URLS:
        m = re.search(pattern, text)
        if m:
            return ats, m.group(1)
    return None


@app.post("/companies/add", dependencies=[Depends(require_auth)])
def add_company(name: str = Form(...)):
    name = name.strip()
    found = _board_from_url(name) or (BoardResolver(runtime.search_config().linkedin_alerts).resolve_board(name)
                                      if name else None)
    if not found:
        return RedirectResponse("/companies?msg=" + quote(f"Couldn't find '{name}' on Greenhouse, Lever, or Ashby. Try pasting a link to one of its job postings."), status_code=303)
    key = f"{found[0]}:{found[1]}"
    added = [b for b in db.kv_json("boards_added", []) if b != key] + [key]
    db.kv_set_json("boards_added", added)
    db.kv_set_json("boards_removed", [b for b in db.kv_json("boards_removed", []) if b != key])
    return RedirectResponse("/companies?msg=" + quote(f"Added {found[1]} ({found[0]}). Its jobs are fetched on the next run."), status_code=303)


@app.post("/companies/remove", dependencies=[Depends(require_auth)])
def remove_company(board: str = Form(...)):
    db.kv_set_json("boards_added", [b for b in db.kv_json("boards_added", []) if b != board])
    removed = db.kv_json("boards_removed", [])
    if board not in removed:
        db.kv_set_json("boards_removed", removed + [board])
    return RedirectResponse("/companies?msg=" + quote(f"Stopped watching {board.split(':', 1)[-1]}."), status_code=303)


@app.post("/companies/restore", dependencies=[Depends(require_auth)])
def restore_company(board: str = Form(...)):
    db.kv_set_json("boards_removed", [b for b in db.kv_json("boards_removed", []) if b != board])
    return RedirectResponse("/companies?msg=" + quote(f"Watching {board.split(':', 1)[-1]} again."), status_code=303)


# Profile pages live in their own module (imported last: it uses the helpers above).
from .profile_pages import router as profile_router  # noqa: E402

app.include_router(profile_router)
