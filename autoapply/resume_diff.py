"""Explain how a tailored resume differs from the master profile, for the dashboard."""

from __future__ import annotations

from .profile import Profile


def resume_changes(profile: Profile, resume: dict) -> dict:
    bullets = profile.all_bullets()
    sections = []
    for kind in ("experience", "projects"):
        for sec in resume.get(kind, []):
            used: set[str] = set()
            items = []
            for b in sec.get("bullets", []):
                ids = b.get("source_ids", [])
                used.update(ids)
                original = " ".join(bullets.get(i, "") for i in ids).strip()
                text = b.get("text", "").strip()
                items.append({
                    "text": text,
                    "original": original if text != original else None,
                    "merged": len(ids) > 1,
                    "reverted": bool(b.get("reverted")),
                })
            own = [i for i in bullets if i.split(".")[0] == sec["id"]]
            sections.append({
                "kind": kind,
                "name": sec.get("company") or sec.get("name", ""),
                "title": sec.get("title", ""),
                "items": items,
                "left_out": [bullets[i] for i in own if i not in used],
            })
    kept_projects = {p["id"] for p in resume.get("projects", [])}
    original_skills = {g.name: g.items for g in profile.skills}
    skills = []
    for g in resume.get("skills", []):
        orig = original_skills.get(g["name"], [])
        skills.append({
            "name": g["name"],
            "items": g["items"],
            "reordered": g["items"] != orig[: len(g["items"])],
            "dropped": [i for i in orig if i not in g["items"]],
        })
    rewritten = sum(1 for s in sections for i in s["items"] if i["original"])
    return {
        "summary": resume.get("summary", ""),
        "summary_original": profile.summary if resume.get("summary", "").strip() != profile.summary.strip() else None,
        "sections": sections,
        "projects_left_out": [p.name for p in profile.projects if p.id not in kept_projects],
        "skills": skills,
        "rewritten_count": rewritten,
        "left_out_count": sum(len(s["left_out"]) for s in sections),
    }
