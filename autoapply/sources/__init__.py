"""Job sources: public, unauthenticated job-board APIs for each ATS."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from ..settings import SearchConfig, env
from . import ashby, greenhouse, lever
from .base import Job, detect_from_url

FETCHERS: dict[str, Callable[[str], list[Job]]] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}


def load_boards_file(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {ats: [str(b) for b in (data.get(ats) or [])] for ats in FETCHERS}


def board_list(search: SearchConfig) -> list[tuple[str, str]]:
    """Boards to poll: config.yaml + boards.yaml + boards learned from LinkedIn alerts."""
    from .. import db

    path = Path(search.boards_file)
    # Relative to config.yaml (on Fly that's /app, not the installed package's directory).
    from_file = load_boards_file(path if path.is_absolute() else env.config_path.parent / path)
    boards: list[tuple[str, str]] = []
    for ats in FETCHERS:
        boards += [(ats, b) for b in getattr(search.boards, ats)]
        boards += [(ats, b) for b in from_file.get(ats, [])]
    if search.linkedin_alerts.watch_matched_companies:
        boards += db.learned_boards()
    boards += [tuple(b.split(":", 1)) for b in db.kv_json("boards_added", []) if ":" in b]
    removed = set(db.kv_json("boards_removed", []))
    excluded = {c.lower() for c in search.exclude_companies}
    # Job boards and recruiters that repost other companies' jobs (found by the LinkedIn triage)
    reposters = set(db.kv_json("reposter_companies", []))
    return [b for b in dict.fromkeys(boards) if b[1].lower() not in excluded and f"{b[0]}:{b[1]}" not in removed
            and b[1].lower().replace("-", "") not in reposters]


_board_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


def describe_job(ats: str, board: str, external_id: str) -> str:
    """Fetch one job's description (for jobs stored without one)."""
    import time

    if ats == "greenhouse":
        return greenhouse.describe(board, external_id)
    if ats == "lever":
        return lever.describe(board, external_id)
    if ats not in FETCHERS:
        return ""
    cached = _board_cache.get((ats, board))
    if cached is None or time.time() - cached[0] > 3600:
        cached = (time.time(), {j.external_id: j.description for j in FETCHERS[ats](board)})
        if len(_board_cache) >= 20:  # whole boards with descriptions are big; keep only recent ones
            _board_cache.pop(min(_board_cache, key=lambda k: _board_cache[k][0]))
        _board_cache[(ats, board)] = cached
    return cached[1].get(external_id, "")


def hydrate(job: Job) -> Job:
    """Fill in the description for sources that list jobs without one."""
    if not job.description and job.ats == "greenhouse":
        job.description = greenhouse.describe(job.board, job.external_id)
    return job


__all__ = ["FETCHERS", "Job", "board_list", "describe_job", "detect_from_url", "hydrate"]
