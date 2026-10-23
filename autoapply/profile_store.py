"""Your profile, stored on the server with a full history of changes.

profile.yaml is only the starting point: the first time the server needs a profile it imports
the file as version 1. After that, edits made in the dashboard create new versions, and any
version can be viewed or restored. Nothing is ever overwritten.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from . import db
from .profile import Profile
from .settings import env


def _file_profile(path: Path | None = None) -> Profile:
    path = path or env.profile_path
    if not path.exists():
        raise FileNotFoundError(
            f"No profile at {path}. Copy profile/profile.example.yaml to profile/profile.yaml "
            "or run `autoapply import-resume <resume.pdf>`."
        )
    with path.open() as f:
        return Profile.model_validate(yaml.safe_load(f))


def to_yaml(profile: Profile) -> str:
    return yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True, width=110)


def latest() -> sqlite3.Row | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM profile_versions ORDER BY id DESC LIMIT 1").fetchone()


def get(version_id: int) -> sqlite3.Row | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone()


def versions(limit: int = 200) -> list[sqlite3.Row]:
    with db.connect() as conn:
        return conn.execute("SELECT id, created_at, note, length(yaml) AS size FROM profile_versions "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def save(profile: Profile | dict | str, note: str) -> int:
    """Validate and store a new version; returns its id."""
    if isinstance(profile, str):
        profile = yaml.safe_load(profile)
    if isinstance(profile, dict):
        profile = Profile.model_validate(profile)
    text = to_yaml(profile)
    with db.connect() as conn:
        return int(conn.execute("INSERT INTO profile_versions (created_at, note, yaml) VALUES (?,?,?)",
                                (db.now(), note[:300], text)).lastrowid)


def import_file(note: str = "Imported from profile.yaml") -> int:
    return save(_file_profile(), note)


def load(version_id: int | None = None) -> Profile:
    """The current profile (or a specific version). Imports profile.yaml on first use."""
    try:
        row = get(version_id) if version_id else latest()
        if row is None and version_id is None:
            import_file()
            row = latest()
    except sqlite3.OperationalError:  # database not initialised (e.g. a bare CLI run)
        return _file_profile()
    if row is None:
        return _file_profile()
    return Profile.model_validate(yaml.safe_load(row["yaml"]))


def current_version_id() -> int | None:
    try:
        row = latest()
    except sqlite3.OperationalError:
        return None
    return int(row["id"]) if row else None
