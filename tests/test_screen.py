"""AI title screen routing, with a fake Claude client (no API calls)."""

from types import SimpleNamespace

from autoapply import db, runtime
from autoapply.pipeline import Pipeline
from autoapply.settings import SearchConfig


def _row(i, title, status=db.SCREEN, location="New York, NY"):
    return {"id": f"greenhouse:x:{i}", "ats": "greenhouse", "board": "x", "external_id": str(i), "company": "X",
            "title": title, "location": location, "apply_url": "u", "status": status}


def test_screen_keeps_and_drops(monkeypatch):
    db.init()
    db.insert_jobs([_row(101, "GTM Operations Lead"), _row(102, "Senior iOS Engineer"), _row(103, "Revenue Systems Associate")])
    calls = []

    def fake_screen(postings, snapshot, examples):
        calls.append([p["title"] for p in postings])
        return {i for i, p in enumerate(postings) if "iOS" not in p["title"]}

    p = Pipeline.__new__(Pipeline)
    p.cfg = SimpleNamespace(search=SearchConfig(preferences="data, GTM, solutions roles"))
    p._llm = SimpleNamespace(candidate_snapshot=lambda prefs: "snapshot", screen_titles=fake_screen)
    p.log = lambda m: None
    stats = p.screen()
    assert stats["kept"] >= 2 and stats["dropped"] >= 1
    assert db.get_job("greenhouse:x:101")["status"] == db.PREFILTERED
    assert db.get_job("greenhouse:x:103")["status"] == db.PREFILTERED
    assert db.get_job("greenhouse:x:102")["status"] == db.SCREENED_OUT


def test_refilter_routes_to_screen_or_fast_track():
    db.init()
    db.insert_jobs([
        _row(201, "Deal Desk Analyst", status=db.FILTERED_OUT),                    # passes → title screen
        _row(202, "Senior Data Engineer", status=db.FILTERED_OUT),                 # "always consider" → scoring
        _row(203, "Data Engineer", status=db.FILTERED_OUT, location="Paris"),      # still wrong location
        _row(204, "Software Engineer", status=db.FILTERED_OUT),                    # never-consider title
    ])
    search = SearchConfig(title_include=["data engineer"], title_exclude=["software engineer"],
                          locations=["New York"], allow_remote=True)
    runtime.refilter_all(search)
    assert db.get_job("greenhouse:x:201")["status"] == db.SCREEN
    assert db.get_job("greenhouse:x:202")["status"] == db.PREFILTERED
    assert db.get_job("greenhouse:x:203")["status"] == db.FILTERED_OUT
    assert db.get_job("greenhouse:x:204")["status"] == db.FILTERED_OUT
