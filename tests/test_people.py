import html
import json
from types import SimpleNamespace

from autoapply import people


INDEX = [
    {"name": "Nash", "slug": "nash", "website": "https://www.usenash.com/"},
    {"name": "Nash", "slug": "nash-2", "website": "https://getnashglobal.com/"},
    {"name": "Sola", "slug": "sola", "website": "https://www.sola-solutions.com/"},
]


def test_yc_match_only_when_unambiguous():
    assert people.yc_match({"company": "Sola", "board": "sola"}, INDEX)["slug"] == "sola"
    # Two YC companies named Nash and nothing in the job to tell them apart: no match, so no wrong founders.
    assert people.yc_match({"company": "Nash", "board": "nash-ai", "description": "nash.ai"}, INDEX) is None
    # The posting names one of the two websites: that one.
    assert people.yc_match({"company": "Nash", "board": "x", "description": "see https://usenash.com/careers"}, INDEX)["slug"] == "nash"
    assert people.yc_match({"company": "Unknown Co"}, INDEX) is None


def test_yc_needs_confirmation():
    sola = INDEX[2]
    page = {"job_titles": ["Forward Deployed Engineer"]}
    assert people.yc_confirmed({"title": "Forward Deployed Engineer", "description": ""}, sola, page)
    assert people.yc_confirmed({"title": "Other", "description": "We're backed by Y Combinator"}, sola, {})
    assert people.yc_confirmed({"title": "Other", "description": "https://www.sola-solutions.com/careers"}, sola, {})
    # Same name, nothing tying the job to the YC company: not confirmed (a different "Coast", say).
    assert not people.yc_confirmed({"title": "AI Operations Engineer", "description": "Fleet cards"}, sola, page)


def test_yc_founders_parse_page(monkeypatch):
    data = {"props": {"company": {"founders": [
        {"full_name": "Jessica Wu", "title": "Founder", "linkedin_url": "https://linkedin.com/in/jessica-wu-1"},
        {"full_name": "No Link", "title": "", "linkedin_url": "https://example.com/not-a-profile"},
    ]}}}
    page = f'<div data-page="{html.escape(json.dumps(data))}"></div>'
    monkeypatch.setattr(people, "_get", lambda url, timeout=10.0: SimpleNamespace(text=page))
    out = people.yc_founders("sola")
    assert [p["name"] for p in out] == ["Jessica Wu", "No Link"]
    assert out[0]["linkedin_url"] == "https://linkedin.com/in/jessica-wu-1"
    assert out[1]["linkedin_url"] == "" and out[1]["title"] == "Founder"


def test_company_site_skips_job_boards():
    job = {"description": "Apply at https://jobs.ashbyhq.com/acme or read https://www.acme.io/blog"}
    assert people.company_site(job, None) == "acme.io"
    assert people.company_site(job, {"website": "https://www.acme.com"}) == "acme.com"


def test_find_people_orders_and_dedupes(monkeypatch):
    monkeypatch.setattr(people, "yc_index", lambda: INDEX)
    monkeypatch.setattr(people, "yc_page", lambda slug: {"job_titles": ["Sola job"]})
    monkeypatch.setattr(people, "yc_founders", lambda slug, page=None: [
        {"name": "Jessica Wu", "title": "Founder", "where": "a founder, per YC", "source": "YC company page",
         "source_url": "u", "linkedin_url": "https://linkedin.com/in/jw"}])
    monkeypatch.setattr(people, "team_page", lambda d: ("", [], ""))
    llm = SimpleNamespace(people_from_text=lambda job, posting, text, links: [
        SimpleNamespace(name="Pat Lee", title="Head of Eng", where="role reports to them", source="posting", linkedin_url=""),
        SimpleNamespace(name="jessica wu", title="CEO", where="founder", source="posting", linkedin_url=""),
    ])
    out = people.find_people({"company": "Sola", "board": "sola", "title": "Sola job", "description": "text", "url": "p"}, llm)
    # Posting-named people first, then founders; the same person isn't listed twice, and keeps YC's profile link.
    assert [p["name"] for p in out] == ["Pat Lee", "jessica wu"]
    assert out[1]["linkedin_url"] == "https://linkedin.com/in/jw"
