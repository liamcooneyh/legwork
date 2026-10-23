from __future__ import annotations

from datetime import datetime, timezone

from .base import Job, client, html_to_text

API = "https://api.lever.co/v0/postings/{board}?mode=json"


def _salary(raw: dict) -> str:
    rng = raw.get("salaryRange") or {}
    if rng.get("min") and rng.get("max"):
        return f"{rng.get('currency', '')} {rng['min']:,}–{rng['max']:,} ({rng.get('interval', '')})".strip()
    return raw.get("salaryDescriptionPlain", "") or ""


def fetch(board: str) -> list[Job]:
    with client() as http:
        resp = http.get(API.format(board=board))
        resp.raise_for_status()
        data = resp.json()
    jobs = []
    for raw in data:
        cats = raw.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        parts = [raw.get("openingPlain") or "", raw.get("descriptionPlain") or ""]
        for section in raw.get("lists") or []:
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content', ''))}")
        parts.append(raw.get("additionalPlain") or "")
        workplace = (raw.get("workplaceType") or "").lower()
        posted = ""
        if raw.get("createdAt"):
            posted = datetime.fromtimestamp(raw["createdAt"] / 1000, tz=timezone.utc).isoformat()
        hosted = raw.get("hostedUrl") or f"https://jobs.lever.co/{board}/{raw['id']}"
        jobs.append(Job(
            ats="lever",
            board=board,
            external_id=raw["id"],
            company=board.replace("-", " ").title(),
            title=(raw.get("text") or "").strip(),
            location=location,
            remote=(workplace == "remote") or ("remote" in location.lower()) or None,
            url=hosted,
            apply_url=raw.get("applyUrl") or f"{hosted}/apply",
            description="\n\n".join(p.strip() for p in parts if p and p.strip()),
            compensation=_salary(raw),
            posted_at=posted,
            department=" / ".join(x for x in (cats.get("department"), cats.get("team")) if x),
        ))
    return jobs


def describe(board: str, external_id: str) -> str:
    with client() as http:
        resp = http.get(f"https://api.lever.co/v0/postings/{board}/{external_id}", params={"mode": "json"})
        resp.raise_for_status()
        raw = resp.json()
    parts = [raw.get("openingPlain") or "", raw.get("descriptionPlain") or ""]
    for section in raw.get("lists") or []:
        parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content', ''))}")
    parts.append(raw.get("additionalPlain") or "")
    return "\n\n".join(x.strip() for x in parts if x and x.strip())
