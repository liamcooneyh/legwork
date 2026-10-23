"""Drafts are answered on the server and filled by the extension in your Chrome."""

from datetime import datetime, timedelta, timezone

from autoapply import db
from autoapply.apply.engine import FormFiller, Planned


def _filler() -> FormFiller:
    f = FormFiller.__new__(FormFiller)
    f.needs = {}
    return f


def _field(key, label, type_="select", options=(), required=True):
    return {"key": key, "label": label, "type": type_, "options": list(options), "required": required}


def test_option_answers_must_match_the_form_word_for_word():
    auth = _field("a", "Are you authorized to work in the US?", "radio", ["Yes", "No"])
    state = _field("s", "State", "combobox", ["New York", "Massachusetts"])
    f = _filler()
    assert f._check_planned([auth, state], [Planned(auth, ["Yes"], "checked"), Planned(state, ["New York"], "written")]) == []
    f = _filler()
    out = f._check_planned([auth], [Planned(auth, ["Yes, I am"], "written")])
    assert out and "isn't one of the form's options" in out[0]
    assert f.needs[auth["label"]]["options"] == ["Yes", "No"]


def test_every_required_question_needs_an_answer():
    why = _field("w", "Why do you want to work here?", "textarea")
    city = _field("c", "City", "combobox")  # a search box: no option list to check against
    optional = _field("o", "Anything else?", "textarea", required=False)
    f = _filler()
    out = f._check_planned([why, city, optional], [Planned(city, ["Brooklyn, NY"], "written")])
    assert len(out) == 1 and "no answer prepared" in out[0]


def test_required_question_already_flagged_is_not_flagged_twice():
    why = _field("w", "Why do you want to work here?", "textarea")
    f = _filler()
    f._need(why, "your profile doesn't cover this")
    assert f._check_planned([why], []) == []


def _job(n: int) -> str:
    jid = f"greenhouse:test:{n}"
    db.upsert_job({"id": jid, "ats": "greenhouse", "board": "test", "external_id": str(n), "company": "Test",
                   "title": f"Analyst {n}", "apply_url": "https://example.com"})
    return jid


def _draft(job_id: str, hours_ago: float) -> None:
    app_id = db.start_application(job_id, True)
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    db.finish_application(app_id, "dry_run", "Prepared", {}, [], [])
    with db.connect() as conn:
        conn.execute("UPDATE applications SET started_at=? WHERE id=?", (at, app_id))


def test_daily_limit_counts_new_drafts_not_re_prepares():
    db.init()
    with db.connect() as conn:
        conn.execute("DELETE FROM applications")
    before = db.prepared_since(24)
    fresh, redone = _job(1), _job(2)
    _draft(fresh, 2)
    _draft(fresh, 1)        # re-prepared today: still one
    _draft(redone, 72)      # first drafted three days ago...
    _draft(redone, 1)       # ...re-prepared today: doesn't count
    assert db.prepared_since(24) - before == 1


def test_school_boxes_are_searched_not_picked_from_a_partial_list():
    from autoapply.apply import engine, mapping
    assert engine.SEARCH_LABELS.search("School") and engine.SEARCH_LABELS.search("University attended")
    assert not engine.SEARCH_LABELS.search("Are you authorized to work in the US?")
    # The first page of a school search: a near miss must not stand in for the real answer.
    first_page = ["Abilene Christian University", "Adelphi University", "Albion College", "Alfred University"]
    assert mapping.best_option("Boston University", first_page, 0.9) is None
    assert mapping.best_option("Albion College", first_page, 0.9) == "Albion College"
