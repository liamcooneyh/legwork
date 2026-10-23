"""Dashboard pages for viewing and editing your profile (everything the system knows about you)."""

from __future__ import annotations

from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from .. import db, profile_store
from ..llm import LLMError
from ..pipeline import Pipeline
from .app import TEMPLATES, _ctx, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

TRI = {"yes": True, "no": False, "": None}


def _redirect(msg: str, to: str = "/profile") -> RedirectResponse:
    return RedirectResponse(f"{to}{'&' if '?' in to else '?'}msg={quote(msg)}", status_code=303)


def _bullets(form, prefix: str = "bullet_") -> list[str]:
    out = []
    i = 0
    while f"{prefix}{i}" in form:
        text = str(form[f"{prefix}{i}"]).strip()
        if text and not form.get(f"remove_{i}"):
            out.append(text)
        i += 1
    new = str(form.get("new_bullet", "")).strip()
    if new:
        out.append(new)
    return out


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, msg: str = ""):
    profile = profile_store.load()
    row = profile_store.latest()
    return TEMPLATES.TemplateResponse(request, "profile.html", _ctx(
        request, p=profile, version=row, version_count=len(profile_store.versions()), msg=msg,
    ))


@router.post("/profile/section/{section}")
async def save_section(section: str, request: Request):
    form = await request.form()
    data = profile_store.load().model_dump()
    kind, _, key = section.partition(":")
    note = ""
    if kind == "summary":
        data["summary"] = str(form.get("summary", "")).strip()
        data["alternate_summaries"] = [x.strip() for x in str(form.get("alternates", "")).split("\n\n") if x.strip()]
        note = "Edited summary"
    elif kind in ("role", "project"):
        items = data["experience" if kind == "role" else "projects"]
        item = next((x for x in items if x["id"] == key), None)
        if item is None:
            raise HTTPException(404)
        fields = ("title", "company", "location", "start", "end") if kind == "role" else ("name", "url", "dates")
        for f in fields:
            if f in form:
                item[f] = str(form[f]).strip()
        item["bullets"] = _bullets(form)
        note = f"Edited {item.get('company') or item.get('name')}"
    elif kind == "skills":
        groups = []
        i = 0
        while f"group_name_{i}" in form:
            name = str(form[f"group_name_{i}"]).strip()
            items = [x.strip() for x in str(form.get(f"group_items_{i}", "")).replace("\n", ",").split(",") if x.strip()]
            if name and items:
                groups.append({"name": name, "items": items})
            i += 1
        data["skills"] = groups
        note = "Edited skills"
    elif kind == "facts":
        data["extra_facts"] = [x.strip(" -•") for x in str(form.get("facts", "")).splitlines() if x.strip(" -•")]
        note = "Edited facts"
    elif kind == "writing":
        data["writing_style"] = str(form.get("writing_style", "")).strip()
        samples = []
        i = 0
        while f"sample_title_{i}" in form:
            title, text = str(form[f"sample_title_{i}"]).strip(), str(form.get(f"sample_text_{i}", "")).strip()
            if text and not form.get(f"sample_remove_{i}"):
                samples.append({"title": title or f"Sample {i + 1}", "text": text})
            i += 1
        new = str(form.get("new_sample_text", "")).strip()
        if new:
            samples.append({"title": str(form.get("new_sample_title", "")).strip() or "New sample", "text": new})
        data["writing_samples"] = samples
        note = "Edited writing style"
    elif kind == "answers":
        answers = []
        i = 0
        while f"q_{i}" in form:
            q, a = str(form[f"q_{i}"]).strip(), str(form.get(f"a_{i}", "")).strip()
            if q and a:
                answers.append({"question": q, "answer": a})
            i += 1
        data["standard_answers"] = answers
        note = "Edited standard answers"
    elif kind == "prefs":
        wa, pr = data["work_authorization"], data["preferences"]
        wa["authorized_us"] = TRI.get(str(form.get("authorized_us", "")))
        wa["requires_sponsorship"] = TRI.get(str(form.get("requires_sponsorship", "")))
        pr["willing_to_relocate"] = TRI.get(str(form.get("willing_to_relocate", "")))
        for f in ("desired_salary", "start_date", "notice_period", "remote_preference", "relocation_notes"):
            pr[f] = str(form.get(f, "")).strip()
        note = "Edited work authorization & preferences"
    elif kind == "eeo":
        for f in data["eeo"]:
            if f in form:
                data["eeo"][f] = str(form[f]).strip()
        note = "Edited demographic answers"
    elif kind == "contact":
        for f in ("first_name", "last_name", "preferred_name", "email", "phone", "linkedin", "github", "website"):
            if f in form:
                data["personal"][f] = str(form[f]).strip()
        for f in ("city", "state", "country"):
            if f in form:
                data["personal"]["location"][f] = str(form[f]).strip()
        note = "Edited contact info"
    else:
        raise HTTPException(404)
    try:
        version = profile_store.save(data, note)
    except ValidationError as e:
        return _redirect(f"Not saved: {e.errors()[0].get('msg', 'invalid value')}")
    return _redirect(f"Saved ({note.lower()}) as version {version}.", f"/profile#{section}")


@router.get("/profile/history", response_class=HTMLResponse)
def history(request: Request, msg: str = ""):
    return TEMPLATES.TemplateResponse(request, "profile_history.html", _ctx(
        request, versions=profile_store.versions(), msg=msg))


@router.get("/profile/version/{version_id}", response_class=HTMLResponse)
def view_version(request: Request, version_id: int):
    row = profile_store.get(version_id)
    if row is None:
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, "profile_version.html", _ctx(request, row=row))


@router.post("/profile/restore/{version_id}")
def restore(version_id: int):
    row = profile_store.get(version_id)
    if row is None:
        raise HTTPException(404)
    new = profile_store.save(row["yaml"], f"Restored version {version_id}")
    return _redirect(f"Restored version {version_id} (saved as version {new}).", "/profile/history")


@router.get("/profile/raw", response_class=HTMLResponse)
def raw_editor(request: Request, error: str = ""):
    row = profile_store.latest()
    return TEMPLATES.TemplateResponse(request, "profile_raw.html", _ctx(
        request, text=row["yaml"] if row else profile_store.to_yaml(profile_store.load()), error=error))


@router.post("/profile/raw")
def save_raw(request: Request, text: str = Form(...)):
    try:
        version = profile_store.save(text, "Edited as text")
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        detail = e.errors()[0] if isinstance(e, ValidationError) else None
        where = " → ".join(str(x) for x in detail["loc"]) + ": " + detail["msg"] if detail else str(e)[:300]
        return TEMPLATES.TemplateResponse(request, "profile_raw.html", _ctx(request, text=text, error=where))
    return _redirect(f"Saved as version {version}.")


@router.post("/profile/import")
def reimport():
    try:
        version = profile_store.import_file("Re-imported profile.yaml from the deployed app")
    except (FileNotFoundError, ValidationError) as e:
        return _redirect(f"Couldn't import: {e}")
    return _redirect(f"Imported profile.yaml as version {version}. Earlier versions are still in History.")


@router.post("/profile/rescore")
def rescore_all():
    with db.connect() as conn:
        n = conn.execute("UPDATE jobs SET status=?, status_reason=? WHERE status IN (?, ?)",
                         (db.PREFILTERED, "Re-score after profile update", db.REJECTED, db.MATCHED)).rowcount
    return _redirect(f"{n} scored jobs will be re-scored with your updated profile on the next run "
                     "(press Run now on the Overview to do it now). Prepared and applied jobs aren't touched.")


# ---------------------------------------------------------------- "I've done this" on a fit gap

@router.post("/job/{job_id:path}/gap", response_class=HTMLResponse)
def gap_suggest(request: Request, job_id: str, gap: str = Form(...), what: str = Form(...)):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    pipeline = Pipeline(log=lambda m: None)
    suggestion, error = None, ""
    if pipeline.llm is None:
        error = "Claude isn't configured, so write the wording yourself below."
    else:
        try:
            suggestion = pipeline.llm.suggest_addition(gap, what, f"{job['title']} at {job['company']}")
        except LLMError as e:
            error = f"Couldn't draft wording ({e}); write it yourself below."
    profile = pipeline.profile
    return TEMPLATES.TemplateResponse(request, "profile_suggest.html", _ctx(
        request, job=job, gap=gap, what=what, s=suggestion, error=error, p=profile))


@router.post("/profile/add")
def add_from_gap(kind: str = Form(...), text: str = Form(...), role_id: str = Form(""), skill_group: str = Form(""),
                 job_id: str = Form(""), gap: str = Form("")):
    text = text.strip()
    if not text:
        return _redirect("Nothing to add.")
    data = profile_store.load().model_dump()
    if kind == "bullet":
        target = next((x for x in data["experience"] + data["projects"] if x["id"] == role_id), None)
        if target is None:
            return _redirect("Pick which job or project this belongs to.")
        target["bullets"].append(text)
        where = target.get("company") or target.get("name")
    elif kind == "skill":
        group = next((g for g in data["skills"] if g["name"].lower() == skill_group.strip().lower()), None)
        if group is None:
            group = {"name": skill_group.strip() or "Other", "items": []}
            data["skills"].append(group)
        if text not in group["items"]:
            group["items"].append(text)
        where = f"skills ({group['name']})"
    else:
        data["extra_facts"].append(text)
        where = "facts"
    job = db.get_job(job_id) if job_id else None
    note = f"Added to {where}" + (f" from a fit gap on {job['company']} – {job['title']}" if job else "")
    profile_store.save(data, note)
    if job is None:
        return _redirect(f"Added to your {where}.")
    old = job["score"]
    message = f"Added to your {where}."
    try:
        Pipeline(log=lambda m: None).score_one(job_id, change=f"Added to {where}: {text}")
        new = db.get_job(job_id)["score"]
        message += f" Re-scored this job: {old if old is not None else '–'} → {new}."
    except LLMError as e:
        message += f" Re-scoring failed ({e}); it will be re-scored on the next run."
        db.set_status(job_id, db.PREFILTERED, "Re-score after profile update")
    return RedirectResponse(f"/job/{quote(job_id, safe='')}?msg={quote(message)}", status_code=303)
