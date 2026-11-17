"""Find named people to reach at a company, from public sources only: the job posting, YC's company page
(founders, with the LinkedIn links YC lists), and the company's own team or about page. Never LinkedIn itself;
the job page links to a LinkedIn search for each name instead."""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from urllib.parse import urlparse

import httpx

from .settings import env
from .sources.base import html_to_text
from .sources.resolve import company_key

YC_INDEX = "https://yc-oss.github.io/api/companies/all.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Legwork personal job search)"}
NOT_COMPANY_SITES = ("greenhouse", "lever.co", "ashbyhq", "workday", "linkedin", "smartrecruiters", "icims",
                     "jobvite", "workable", "bamboohr", "rippling", "gem.com", "indeed", "glassdoor", "google.",
                     "youtube", "twitter.com", "x.com", "facebook", "instagram", "github.com", "medium.com",
                     "ycombinator.com", "crunchbase", "eeoc.gov", "dol.gov", "w3.org", "apple.com", "notion.so")
TEAM_PATHS = ("/about", "/team", "/company", "/about-us", "/our-team")
PROFILE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%-]+/?")


def _get(url: str, timeout: float = 10.0) -> httpx.Response | None:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        return r if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def yc_index() -> list[dict]:
    """YC's public company list (names, slugs, websites), cached for a week."""
    path = env.data_dir / "yc_companies.json"
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        return json.loads(path.read_text())
    r = _get(YC_INDEX, timeout=60)
    if r is None:
        return json.loads(path.read_text()) if path.exists() else []
    slim = [{k: c.get(k) for k in ("name", "slug", "website", "isHiring", "status")} for c in r.json()]
    path.write_text(json.dumps(slim))
    return slim


def _domain(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _base(domain: str) -> str:
    labels = domain.split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 and len(labels[-2]) > 3 else domain


def posting_domains(job: dict) -> set[str]:
    """Company websites the posting links to (job boards and social sites left out)."""
    found = set()
    for url in re.findall(r"https?://[^\s\"'<>)]+", job.get("description") or ""):
        d = _domain(url)
        if d and not any(x in d for x in NOT_COMPANY_SITES):
            found.add(_base(d))
    return found


def yc_match(job: dict, index: list[dict]) -> dict | None:
    """The YC company this job is at, only when the evidence agrees. Many companies share a name (two YC
    "Nash"es; a YC "Coast" and a non-YC fintech "Coast"), and the wrong match means the wrong founders."""
    key = company_key(job.get("company") or "")
    if not key:
        return None
    same = [c for c in index if company_key(c.get("name") or "") == key and c.get("website")]
    if not same:
        return None
    sites = posting_domains(job)
    if sites:   # the posting names its website: YC's must be the same one
        picked = [c for c in same if _base(_domain(c["website"])) in sites]
    else:       # no website to check against: the job board's name has to match YC's slug or site
        board = re.sub(r"[^a-z0-9]", "", (job.get("board") or "").lower())
        picked = [c for c in same if board and (board == (c.get("slug") or "").replace("-", "")
                                                 or board == _base(_domain(c["website"])).split(".")[0])]
    return picked[0] if len(picked) == 1 else None


def yc_page(slug: str) -> dict:
    """The company data YC's page carries: founders (with the LinkedIn links YC lists) and open job titles."""
    r = _get(f"https://www.ycombinator.com/companies/{slug}")
    m = r and re.search(r'data-page="([^"]+)"', r.text)
    if not m:
        return {}
    try:
        props = json.loads(htmllib.unescape(m.group(1)))["props"]
        return {"founders": props["company"].get("founders") or [],
                "job_titles": [(j.get("title") or "") for j in props.get("jobPostings") or []]}
    except (ValueError, KeyError, TypeError):
        return {}


def yc_confirmed(job: dict, yc: dict, page: dict) -> bool:
    """A name match alone isn't enough: the posting has to mention YC, link to the YC company's website, or
    the same job has to be listed on its YC page."""
    text = job.get("description") or ""
    title = (job.get("title") or "").strip().lower()
    return bool(re.search(r"Y ?Combinator|\bYC\b", text)
                or _base(_domain(yc.get("website"))) in posting_domains(job)
                or any(t.strip().lower() == title for t in page.get("job_titles", [])))


def yc_founders(slug: str, page: dict | None = None) -> list[dict]:
    page = page if page is not None else yc_page(slug)
    out = []
    for f in page.get("founders") or []:
        name = (f.get("full_name") or "").strip()
        if not name:
            continue
        link = f.get("linkedin_url") or ""
        out.append({"name": name, "title": (f.get("title") or "Founder").strip(), "where": "a founder, per YC",
                    "source": "YC company page", "source_url": f"https://www.ycombinator.com/companies/{slug}",
                    "linkedin_url": link if PROFILE.fullmatch(link) else ""})
    return out


def company_site(job: dict, yc: dict | None) -> str:
    """The company's own website: from YC, or the first non-job-board link in the posting."""
    if yc and yc.get("website"):
        return _domain(yc["website"])
    for url in re.findall(r"https?://[^\s\"'<>)]+", job.get("description") or ""):
        d = _domain(url)
        if d and not any(x in d for x in NOT_COMPANY_SITES):
            return d
    return ""


def team_page(domain: str) -> tuple[str, list[str], str]:
    """Text and LinkedIn profile links from the company's about or team page, and that page's URL."""
    labels = domain.split(".")
    hosts = [domain] + ([".".join(labels[-2:])] if len(labels) > 2 and len(labels[-2]) > 3 else [])  # docs.x.ai -> x.ai
    for host in hosts:
        for path in TEAM_PATHS:
            r = _get(f"https://{host}{path}", timeout=8)
            if r is None or "html" not in r.headers.get("content-type", ""):
                continue
            text = html_to_text(r.text)
            if len(text) > 300:
                links = sorted(set(PROFILE.findall(r.text)))
                return text, links, str(r.url)
    return "", [], ""


def find_people(job: dict, llm=None) -> list[dict]:
    """Up to six named people worth contacting about this job, best first. Each has name, title, where (why
    them), source, source_url, and linkedin_url (only a link a source actually gave)."""
    yc = yc_match(job, yc_index())
    page = yc_page(yc["slug"]) if yc else {}
    if yc and not yc_confirmed(job, yc, page):
        yc, page = None, {}
    founders = yc_founders(yc["slug"], page) if yc else []
    domain = company_site(job, yc)
    team_text, team_links, team_url = team_page(domain) if domain else ("", [], "")
    named: list[dict] = []
    if llm is not None and (job.get("description") or team_text):
        posting_url = job.get("url") or job.get("apply_url") or ""
        for p in llm.people_from_text(job, job.get("description") or "", team_text, team_links):
            from_team = p.source == "team page"
            named.append({"name": p.name.strip(), "title": p.title.strip(), "where": p.where.strip(),
                          "source": "company website" if from_team else "job posting",
                          "source_url": team_url if from_team else posting_url, "linkedin_url": p.linkedin_url})
    people: list[dict] = []
    by_name: dict[str, dict] = {}
    # Who the posting names (usually the hiring manager or recruiter) first, then founders, then the team page.
    ordered = ([p for p in named if p["source"] == "job posting"] + founders
               + [p for p in named if p["source"] != "job posting"])
    for p in ordered:
        k = p["name"].lower()
        if not p["name"]:
            continue
        if k in by_name:   # the same person from two sources: keep the first, but take a profile link if it lacks one
            by_name[k]["linkedin_url"] = by_name[k]["linkedin_url"] or p["linkedin_url"]
            continue
        by_name[k] = p
        people.append(p)
    return people[:6]
