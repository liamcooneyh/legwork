"""Read-only lookups on more hiring systems' public job boards: used to find the employer's own
posting for a LinkedIn-only job for free (no AI, no search fees). The app can't fill these
forms, so a job found here keeps its LinkedIn status: it gets a real description and score,
and you apply on the employer's page.

Each system publishes a company's open jobs at a predictable address (the company's "slug"),
so a lookup is a few ordinary requests: list the jobs, find the title, read the description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import httpx

UA = {"User-Agent": "Mozilla/5.0 (personal job search tool; reads public job boards)"}
TIMEOUT = 8


@dataclass
class Posting:
    ats: str
    slug: str
    id: str
    title: str
    location: str
    url: str
    describe: Callable[[], str]   # fetches the full description when needed
    company_name: str = ""        # when the system reports the employer's name (to catch slug collisions)
    posted_at: str = ""           # when the employer published it, if the system says


def _text(html: str) -> str:
    html = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", html or "")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text).replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _when(d: dict, *keys: str) -> str:
    """The first of these fields that holds a date, as ISO "2026-10-03T14:05:00" (or just the day)."""
    for k in keys:
        m = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", str(d.get(k) or ""))
        if m:
            return m.group(0).replace(" ", "T")
    return ""


def _get(client: httpx.Client, url: str):
    r = client.get(url)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return None
    return r.json()


def smartrecruiters(client: httpx.Client, slug: str) -> list[Posting]:
    data = _get(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100") or {}
    out = []
    for p in data.get("content", []):
        loc = p.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        if loc.get("remote"):
            where = (where + " (Remote)").strip()

        def describe(pid=p["id"]):
            d = _get(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}") or {}
            secs = (d.get("jobAd") or {}).get("sections") or {}
            return _text("\n".join(f"{s.get('title', '')}\n{s.get('text', '')}" for s in secs.values()))
        out.append(Posting("smartrecruiters", slug, p["id"], p.get("name", ""), where,
                           f"https://jobs.smartrecruiters.com/{slug}/{p['id']}", describe,
                           (p.get("company") or {}).get("name", ""), _when(p, "releasedDate", "createdOn")))
    return out


def recruitee(client: httpx.Client, slug: str) -> list[Posting]:
    data = _get(client, f"https://{slug}.recruitee.com/api/offers/") or {}
    return [Posting("recruitee", slug, str(o["id"]), o.get("title", ""), o.get("location", "") or "",
                    o.get("careers_url") or f"https://{slug}.recruitee.com/o/{o.get('slug', '')}",
                    lambda o=o: _text(f"{o.get('description', '')}\n{o.get('requirements', '')}"),
                    posted_at=_when(o, "published_at", "created_at"))
            for o in data.get("offers", [])]


def bamboohr(client: httpx.Client, slug: str) -> list[Posting]:
    data = _get(client, f"https://{slug}.bamboohr.com/careers/list") or {}
    out = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x) if isinstance(loc, dict) else str(loc)

        def describe(jid=j["id"]):
            d = _get(client, f"https://{slug}.bamboohr.com/careers/{jid}/detail") or {}
            return _text(((d.get("result") or {}).get("jobOpening") or {}).get("description", ""))
        out.append(Posting("bamboohr", slug, str(j["id"]), j.get("jobOpeningName", "").strip(), where,
                           f"https://{slug}.bamboohr.com/careers/{j['id']}", describe,
                           posted_at=_when(j, "datePosted", "createdDate")))
    return out


def breezy(client: httpx.Client, slug: str) -> list[Posting]:
    r = client.get(f"https://{slug}.breezy.hr/json")
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return []
    out = []
    for p in r.json():
        loc = p.get("location")
        where = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")

        def describe(url=p["url"]):
            html = client.get(url).text
            m = re.search(r'class="description"[^>]*>(.*?)</div>\s*</div>', html, re.S)
            return _text(m.group(1)) if m else ""
        out.append(Posting("breezy", slug, p["id"], p.get("name", ""), where, p["url"], describe,
                           (p.get("company") or {}).get("name", "") if isinstance(p.get("company"), dict) else "",
                           _when(p, "published_date", "creation_date")))
    return out


def rippling(client: httpx.Client, slug: str) -> list[Posting]:
    data = _get(client, f"https://ats.rippling.com/api/v2/board/{slug}/jobs") or {}
    out = []
    for j in data.get("items", []):
        locs = j.get("locations") or []
        where = "; ".join((l.get("name") or "") if isinstance(l, dict) else str(l) for l in locs)

        posting = Posting("rippling", slug, j["id"], j.get("name", ""), where, j.get("url", ""), lambda: "")

        def describe(jid=j["id"], posting=posting):
            d = _get(client, f"https://ats.rippling.com/api/v2/board/{slug}/jobs/{jid}") or {}
            posting.posted_at = posting.posted_at or _when(d, "createdOn")  # only the detail page has it
            desc = d.get("description") or {}
            return _text("\n".join(v for v in desc.values() if isinstance(v, str))) if isinstance(desc, dict) else _text(str(desc))
        posting.describe = describe
        out.append(posting)
    return out


def workable(client: httpx.Client, slug: str) -> list[Posting]:
    r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if r.status_code == 429:
        raise RateLimited("workable")
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return []
    out = []
    for j in r.json().get("jobs", []):
        where = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if j.get("telecommuting"):
            where = (where + " (Remote)").strip()
        out.append(Posting("workable", slug, j.get("shortcode", ""), j.get("title", ""), where,
                           j.get("url") or j.get("shortlink", ""),
                           lambda j=j: _text(f"{j.get('description', '')}\n{j.get('requirements', '')}"),
                           posted_at=_when(j, "published_on", "created_at")))
    return out


class RateLimited(Exception):
    pass


SYSTEMS: dict[str, Callable[[httpx.Client, str], list[Posting]]] = {
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "bamboohr": bamboohr,
    "breezy": breezy, "rippling": rippling, "workable": workable,
}


def client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers=UA)
