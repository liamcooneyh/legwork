from __future__ import annotations

from .base import Job, client

API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"


def fetch(board: str) -> list[Job]:
    with client() as http:
        resp = http.get(API.format(board=board))
        resp.raise_for_status()
        data = resp.json()
    jobs = []
    for raw in data.get("jobs", []):
        if raw.get("isListed") is False:
            continue
        locations = [raw.get("location") or ""] + [
            s.get("location", "") for s in raw.get("secondaryLocations") or []
        ]
        comp = (raw.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or (
            (raw.get("compensation") or {}).get("compensationTierSummary") or ""
        )
        job_url = raw.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{raw['id']}"
        jobs.append(Job(
            ats="ashby",
            board=board,
            external_id=raw["id"],
            company=board.replace("-", " ").title(),
            title=(raw.get("title") or "").strip(),
            location=" / ".join(l for l in locations if l),
            remote=bool(raw.get("isRemote")) or (raw.get("workplaceType") == "Remote"),
            url=job_url,
            apply_url=raw.get("applyUrl") or f"{job_url}/application",
            description=raw.get("descriptionPlain") or "",
            compensation=comp,
            posted_at=raw.get("publishedAt") or "",
            department=" / ".join(x for x in (raw.get("department"), raw.get("team")) if x),
        ))
    return jobs
