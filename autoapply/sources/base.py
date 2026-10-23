from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "autoapply/0.1 (personal job search tool)"


@dataclass
class Job:
    ats: str
    board: str
    external_id: str
    company: str
    title: str
    location: str
    remote: bool | None
    url: str
    apply_url: str
    description: str
    compensation: str = ""
    posted_at: str = ""
    department: str = ""   # team/department when the board reports one (helps the title screen)

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.board}:{self.external_id}"

    def to_row(self) -> dict:
        row = asdict(self)
        row["id"] = self.id
        row["remote"] = None if self.remote is None else int(self.remote)
        return row


def client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def html_to_text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def detect_from_url(url: str) -> tuple[str, str, str] | None:
    """Map a job URL to (ats, board, external_id)."""
    patterns = [
        ("greenhouse", r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)"),
        ("greenhouse", r"greenhouse\.io/embed/job_app\?.*for=([\w-]+).*token=(\d+)"),
        ("greenhouse", r"boards\.greenhouse\.io/embed/job_app\?.*for=([\w-]+).*token=(\d+)"),
        ("lever", r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)/([0-9a-f-]{36})"),
        ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})"),
    ]
    for ats, pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return ats, m.group(1), m.group(2)
    return None
