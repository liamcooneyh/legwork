"""Lessons: what the outcomes of your applications say about the drafts, scoring, and targeting.

After every few new outcomes (or weekly), Claude reviews the applications next to how they turned out
and proposes findings with their evidence. Nothing changes until you press Apply on the Impact page;
an applied lesson becomes standing guidance in the prompts for its area (llm.LLM._lessons). Lessons
change emphasis, wording, and judgment only: they can never add facts about you. Each draft records
the lessons it was made under, so the Impact page can compare reply rates before and after."""
import json
from datetime import datetime, timedelta, timezone

from . import db, notify
from .settings import env

REVIEW_EVERY = 5          # new outcomes
REVIEW_DAYS = 7           # or a week, if anything new came in
NO_RESPONSE_DAYS = 30
SCREEN_STAGES = {"heard_back", "interviewing", "offer"}
OUTCOME_LABELS = {"screen": "Screen or interview", "rejected": "Rejected, no screen",
                  "no_response": "No response", "waiting": "Waiting"}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def outcome(job, events) -> str | None:
    """screen | rejected | no_response | waiting (None for a withdrawn application). A rejection after
    a screen counts as a screen: the draft did its job."""
    stage = job["track_stage"] or "applied"
    if stage == "withdrawn":
        return None
    screened = stage in SCREEN_STAGES or any(
        e["kind"] == "stage" and e["detail"].split(" (")[0] in ("Heard back", "Interviewing", "Offer") for e in events)
    if screened:
        return "screen"
    if stage == "rejected":
        return "rejected"
    if stage == "no_response":
        return "no_response"
    applied = _dt(job["applied_at"]) or _dt(job["track_updated_at"])
    if applied and datetime.now(timezone.utc) - applied > timedelta(days=NO_RESPONSE_DAYS):
        return "no_response"
    return "waiting"


def _submitted(job_id: str) -> dict:
    """Artifacts of the draft that went out (the submitted one, else the latest with a resume)."""
    apps = db.applications_for(job_id)
    for a in sorted(apps, key=lambda a: (a["outcome"] == "applied", a["id"]), reverse=True):
        art = json.loads(a["artifacts_json"] or "{}")
        if art.get("dir"):
            return art
    return {}


def _resume(art: dict) -> dict:
    try:
        return json.loads((env.artifacts_dir / art["dir"] / "resume.json").read_text())
    except (KeyError, OSError, ValueError):
        return {}


def cases() -> list[dict]:
    """Every application you've made, with its outcome and what went into it."""
    out = []
    for job in db.tracked_jobs():
        result = outcome(job, db.events_for(job["id"]))
        if result is None:
            continue
        detail = json.loads(job["score_json"] or "{}")
        art = _submitted(job["id"])
        out.append({"job": job, "outcome": result, "detail": detail, "artifacts": art,
                    "lessons": art.get("lessons", []), "applied_at": job["applied_at"] or job["track_updated_at"]})
    return out


def _case_text(c: dict) -> str:
    job, d = c["job"], c["detail"]
    parts = d.get("parts") or {}
    misses = [r for r in d.get("requirements", []) if r.get("status") == "not_met"]
    lines = [f"## {job['company']} | {job['title']} | outcome: {c['outcome']} | score {job['score']}",
             "parts: " + ", ".join(f"{k} {v.get('points')}" for k, v in parts.items())]
    if misses:
        lines.append("unmet: " + "; ".join(f"{r['kind']}{' (hard)' if r.get('hard') else ''}: {r['text']}" for r in misses[:6]))
    resume = _resume(c["artifacts"])
    if resume.get("summary"):
        lines.append("resume summary: " + resume["summary"])
        for role in resume.get("experience", [])[:2]:
            for b in role.get("bullets", [])[:2]:
                lines.append(f"  {role.get('company')}: {b.get('text')}")
    if c["artifacts"].get("cover_letter"):
        lines.append("cover letter: yes")
    if c["lessons"]:
        lines.append("made under lessons: " + ", ".join(f"#{i}" for i in c["lessons"]))
    if (job["track_notes"] or "").strip():
        lines.append("candidate's notes: " + job["track_notes"].strip()[:600])
    return "\n".join(lines)


def _decided(all_cases: list[dict]) -> list[dict]:
    return [c for c in all_cases if c["outcome"] != "waiting"]


def status(all_cases: list[dict] | None = None) -> dict:
    all_cases = cases() if all_cases is None else all_cases
    decided = len(_decided(all_cases))
    seen = int(db.kv_get("lessons_reviewed_decided", "0") or 0)
    last = _dt(db.kv_get("lessons_reviewed_at"))
    new = max(0, decided - seen)
    due = new >= REVIEW_EVERY or (new > 0 and (last is None or datetime.now(timezone.utc) - last >= timedelta(days=REVIEW_DAYS)))
    return {"decided": decided, "new": new, "last": last, "due": due, "waiting": len(all_cases) - decided}


def review(llm, force: bool = False) -> list[int]:
    """Ask Claude for findings if enough new outcomes came in (or `force`). Returns new lesson ids."""
    all_cases = cases()
    st = status(all_cases)
    decided = _decided(all_cases)
    if llm is None or not decided or not (st["due"] or force):
        return []
    existing = "\n".join(f"#{l['id']} [{l['status']}, {l['area']}] {l['finding']} → {l['guidance']}" for l in db.lessons())
    text = "\n\n".join(_case_text(c) for c in decided)
    text += f"\n\n({st['waiting']} more applications are still waiting for a reply and aren't listed.)"
    findings = llm.review_lessons(text, existing)
    ids = [db.add_lesson(f.area, f.finding, f.evidence, f.guidance) for f in findings]
    db.kv_set("lessons_reviewed_decided", str(st["decided"]))
    db.kv_set("lessons_reviewed_at", db.now())
    if ids:
        notify.send(f"{len(ids)} new lesson{'s' if len(ids) != 1 else ''} from your application outcomes, ready to "
                    "review", "/impact#lessons")
    return ids


def guidance() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for l in reversed(db.lessons("applied")):
        out.setdefault(l["area"], []).append(l["guidance"])
    return out


def applied_ids() -> list[int]:
    return sorted(l["id"] for l in db.lessons("applied"))


def _rate(group: list[dict]) -> dict:
    decided = _decided(group)
    screens = sum(c["outcome"] == "screen" for c in decided)
    return {"applied": len(group), "decided": len(decided), "screens": screens,
            "rate": screens / len(decided) if decided else None}


def board() -> dict:
    """Everything the Impact page's Lessons section shows."""
    all_cases = cases()
    applied = []
    for l in db.lessons("applied", "retired"):
        since = _dt(l["decided_at"])
        under = [c for c in all_cases if l["id"] in c["lessons"]]
        before = [c for c in all_cases if l["id"] not in c["lessons"] and since and (_dt(c["applied_at"]) or since) < since]
        applied.append({"lesson": l, "under": _rate(under), "before": _rate(before)})
    return {"proposed": db.lessons("proposed"), "applied": applied,
            "dismissed": len(db.lessons("dismissed")), "status": status(all_cases),
            "outcomes": {k: sum(c["outcome"] == k for c in all_cases) for k in OUTCOME_LABELS},
            "labels": OUTCOME_LABELS, "every": REVIEW_EVERY, "days": REVIEW_DAYS}
