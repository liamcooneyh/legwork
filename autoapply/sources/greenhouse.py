from __future__ import annotations

import html

from .base import Job, client, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
JOB_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}"


def fetch(board: str, content: bool = False) -> list[Job]:
    """List a board's jobs. Without `content` the description is left empty (the list is ~12x
    smaller); `describe()` fills it in for the jobs that pass the prefilter."""
    with client() as http:
        resp = http.get(API.format(board=board), params={"content": "true"} if content else None)
        resp.raise_for_status()
        data = resp.json()
    jobs = []
    for raw in data.get("jobs", []):
        location = (raw.get("location") or {}).get("name", "")
        description = html_to_text(html.unescape(raw.get("content") or ""))
        # The hosted board URL always carries the application form, even when the company
        # links to its own careers site.
        apply_url = f"https://job-boards.greenhouse.io/{board}/jobs/{raw['id']}"
        jobs.append(Job(
            ats="greenhouse",
            board=board,
            external_id=str(raw["id"]),
            company=raw.get("company_name") or board,
            title=raw.get("title", "").strip(),
            location=location,
            remote="remote" in location.lower() or None,
            url=raw.get("absolute_url") or apply_url,
            apply_url=apply_url,
            description=description,
            posted_at=raw.get("first_published") or raw.get("updated_at") or "",
        ))
    return jobs


def describe(board: str, external_id: str) -> str:
    with client() as http:
        resp = http.get(JOB_API.format(board=board, id=external_id))
        resp.raise_for_status()
        return html_to_text(html.unescape(resp.json().get("content") or ""))
