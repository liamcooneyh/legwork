"""Read a job posting page on an employer's site (never LinkedIn) with the server's browser."""

from __future__ import annotations

import re

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

BLOCKED_HOSTS = ("linkedin.com",)


def read_posting(browser: Browser, url: str, timeout_ms: int = 30000) -> str:
    """Visible text of the page, or "" if it can't be loaded."""
    if not url.startswith(("http://", "https://")) or any(h in url.split("/")[2] for h in BLOCKED_HOSTS):
        return ""
    context = browser.new_context(locale="en-US")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)  # let JavaScript-built pages (Workday etc.) render
        text = page.inner_text("body", timeout=10000)
    except PlaywrightError:
        return ""
    finally:
        context.close()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:40000]


def looks_like(text: str, title: str, company: str) -> bool:
    """Sanity check that the page really is this job: most title words and the company appear."""
    low = text.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2]
    hits = sum(1 for w in words if w in low)
    company_hit = any(w in low for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 2)
    return bool(words) and hits / len(words) >= 0.6 and company_hit and len(text) > 500
