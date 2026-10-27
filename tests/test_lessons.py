from datetime import datetime, timedelta, timezone

from autoapply import db, lessons
from autoapply.llm import LLM


def _job(**kw):
    base = dict(track_stage="applied", applied_at=datetime.now(timezone.utc).isoformat(), track_updated_at=None)
    return {**base, **kw}


def _ev(detail):
    return {"kind": "stage", "detail": detail}


def test_outcomes():
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    assert lessons.outcome(_job(), []) == "waiting"
    assert lessons.outcome(_job(applied_at=old), []) == "no_response"
    assert lessons.outcome(_job(track_stage="rejected"), []) == "rejected"
    assert lessons.outcome(_job(track_stage="interviewing"), []) == "screen"
    # a rejection after a recruiter screen still counts as the draft working
    assert lessons.outcome(_job(track_stage="rejected"), [_ev("Heard back (email)"), _ev("Rejected")]) == "screen"
    assert lessons.outcome(_job(track_stage="withdrawn"), []) is None


def _applied(n, stage):
    with db.connect() as conn:
        start = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        for i in range(start, start + n):
            jid = f"greenhouse:t:{i}"
            conn.execute(
                "INSERT INTO jobs (id, ats, board, external_id, company, title, apply_url, discovered_at, updated_at, status, "
                "track_stage, applied_at) VALUES (?, 'greenhouse', 't', ?, 'Acme', 'Analyst', 'x', ?, ?, ?, ?, ?)",
                (jid, jid, db.now(), db.now(), db.APPLIED, stage, db.now()))


def test_review_waits_for_enough_new_outcomes(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))     # a database of its own
    db.init()
    db.kv_set("lessons_reviewed_decided", "0")
    db.kv_set("lessons_reviewed_at", db.now())
    _applied(3, "rejected")
    _applied(4, "applied")
    st = lessons.status()
    assert (st["decided"], st["waiting"], st["new"], st["due"]) == (3, 4, 3, False)
    _applied(2, "rejected")
    assert lessons.status()["due"]

    calls = []

    class Fake:
        def review_lessons(self, cases, existing):
            calls.append(cases)
            return [lessons_finding()]

    ids = lessons.review(Fake())
    assert len(ids) == 1 and "outcome: rejected" in calls[0] and "4 more applications are still waiting" in calls[0]
    assert not lessons.status()["due"]                 # counted as reviewed
    assert lessons.review(Fake()) == []                # nothing new: no call
    assert len(calls) == 1
    assert lessons.guidance() == {}                    # proposed lessons don't steer anything
    db.set_lesson_status(ids[0], "applied")
    assert lessons.guidance() == {"writing": ["Lead with the posting's core function."]}
    assert lessons.applied_ids() == ids


def lessons_finding():
    from autoapply.llm import LessonFinding

    return LessonFinding(area="writing", finding="Rejections led with AI.", evidence="5 rejections",
                         guidance="Lead with the posting's core function.")


def test_applied_lessons_reach_only_their_steps(monkeypatch):
    from autoapply.profile import load_profile

    llm = LLM(load_profile(), client=object())
    assert llm._lessons("writing") == ""
    llm.guidance = {"writing": ["W1"], "scoring": ["S1"], "targeting": ["T1"]}
    assert "- W1" in llm._lessons("writing") and "S1" not in llm._lessons("writing")
    both = llm._lessons("scoring", "targeting")
    assert "- S1" in both and "- T1" in both and "never the facts" in both
