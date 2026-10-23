"""Render tailored resumes and cover letters to PDF with the same Chromium used for applying."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup
from playwright.sync_api import Browser

from .llm import TailoredResume
from .profile import Profile

_env = Environment(loader=PackageLoader("autoapply", "templates"), autoescape=select_autoescape(["j2", "html"]))


def _display_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


_env.filters["display_url"] = _display_url


def _html_to_pdf(browser: Browser, html: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    page = browser.new_page()
    try:
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out), prefer_css_page_size=True, print_background=True)
    finally:
        page.close()
    return out


def render_resume(browser: Browser, profile: Profile, resume: TailoredResume, out: Path, paper: str = "Letter") -> Path:
    pers = profile.personal
    contact: list[Markup] = [Markup.escape(x) for x in (pers.location.display(), pers.phone) if x]
    contact.append(Markup('<a href="mailto:{0}">{0}</a>').format(pers.email))
    contact += [Markup('<a href="{}">{}</a>').format(u, _display_url(u))
                for u in (pers.linkedin, pers.github, pers.website) if u]
    template = _env.get_template("resume.html.j2")
    # Shrink the type a little at a time until it fits on one page.
    for font_pt, line_height in ((9.6, 1.24), (9.3, 1.2), (9.0, 1.17), (8.7, 1.15)):
        html = template.render(p=profile, r=resume, paper=paper, contact=contact, Markup=Markup,
                               font_pt=font_pt, line_height=line_height)
        _html_to_pdf(browser, html, out)
        if page_count(out) <= 1:
            break
    return out


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


def render_cover_letter(browser: Browser, profile: Profile, text: str, out: Path, paper: str = "Letter") -> Path:
    paragraphs = [Markup.escape(p.strip()).replace("\n", Markup("<br>")) for p in text.split("\n\n") if p.strip()]
    html = _env.get_template("cover_letter.html.j2").render(
        p=profile, paragraphs=paragraphs, paper=paper, date=f"{date.today():%B} {date.today().day}, {date.today().year}"
    )
    return _html_to_pdf(browser, html, out)
