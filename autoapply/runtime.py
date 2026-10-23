"""config.yaml plus the changes you make in the dashboard (stored in the database).

The dashboard's edits win until you reset them, so filters and companies can change without
a redeploy.
"""

from __future__ import annotations

from . import db
from .filters import fast_track, prefilter
from .settings import Config, SearchConfig, load_config

EDITABLE_FILTERS = ["title_include", "title_exclude", "locations", "location_exclude", "allow_remote",
                    "exclude_companies", "max_age_days", "preferences"]


def search_config() -> SearchConfig:
    base = load_config().search
    overrides = {k: v for k, v in db.kv_json("search_overrides", {}).items() if k in EDITABLE_FILTERS}
    return base.model_copy(update=overrides)


def config() -> Config:
    return load_config().model_copy(update={"search": search_config()})


def save_filters(values: dict) -> None:
    current = db.kv_json("search_overrides", {})
    current.update({k: v for k, v in values.items() if k in EDITABLE_FILTERS})
    db.kv_set_json("search_overrides", current)


def reset_filters() -> None:
    db.kv_set_json("search_overrides", {})


FILTER_LOGIC_VERSION = "3"  # bump when the filtering logic itself changes, to re-check stored jobs


def refilter_all(search: SearchConfig) -> dict:
    """Re-run the free filters over stored jobs after the filters (or filter logic) change.
    Newly passing jobs go to the AI title screen, or straight to scoring if they match an
    "always consider" title; jobs that now fail are set aside. Jobs Claude already scored are
    left alone unless they now fail the free filters."""
    moved_in = moved_out = fast = 0
    updates = []
    for row in db.all_jobs_for_refilter():
        reason = prefilter(dict(row), search)
        status = row["status"]
        if reason:
            if status != db.FILTERED_OUT or reason != row["status_reason"]:
                updates.append((db.FILTERED_OUT, reason, row["id"]))
                moved_out += status != db.FILTERED_OUT
            continue
        if status == db.MANUAL_LINKEDIN:
            # LinkedIn "apply yourself" jobs get the title screen too, unless already scored or rescued.
            if row["score"] is None and row["status_reason"] != "You marked this relevant" \
                    and not fast_track(row["title"], search):
                updates.append((db.SCREEN, row["status_reason"], row["id"]))
                moved_in += 1
            continue
        if fast_track(row["title"], search) and status in (db.FILTERED_OUT, db.SCREEN, db.SCREENED_OUT):
            updates.append((db.PREFILTERED, "Matches an 'always consider' title", row["id"]))
            fast += 1
        elif status == db.FILTERED_OUT:
            updates.append((db.SCREEN, None, row["id"]))
            moved_in += 1
    db.bulk_status(updates)
    return {"to_title_screen": moved_in, "fast_tracked": fast, "now_filtered_out": moved_out}
