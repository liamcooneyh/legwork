from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

from autoapply import db, replies
from autoapply.llm import ReplyVerdict
from autoapply.settings import ReplyTrackingConfig
from autoapply.sources import mailbox


def test_stages_only_move_forward():
    ns = replies.next_stage
    assert ns(None, "recruiter_reply") == "heard_back"
    assert ns("heard_back", "interview_request") == "interviewing"
    assert ns("interviewing", "recruiter_reply") is None      # never backwards
    assert ns("interviewing", "rejection") == "rejected"      # rejection after interviews still counts
    assert ns("no_response", "interview_request") == "interviewing"
    assert ns("offer", "rejection") is None                   # offers are yours to change
    assert ns("withdrawn", "interview_request") is None
    assert ns("rejected", "interview_request") is None
    assert ns("applied", "confirmation") is None


def test_query_includes_companies_and_ats_senders():
    q = replies.build_query([{"company": "Stripe"}, {"company": 'Acme "Labs"'}, {"company": "Stripe"}], 21)
    assert q.startswith("newer_than:21d -from:linkedin.com")
    assert '"Stripe"' in q and '"Acme  Labs"' in q.replace("  ", "  ") and q.count('"Stripe"') == 1
    assert "from:greenhouse-mail.io" in q


def _job(**kw):
    base = dict(id="greenhouse:stripe:1", company="Stripe", board="stripe", title="Solutions Engineer",
                applied_at="2026-10-01T12:00:00+00:00", updated_at="2026-10-01T12:00:00+00:00", track_stage="applied")
    return {**base, **kw}


def test_candidates_match_company_and_skip_older_mail():
    job = _job()
    msg = mailbox.Message("1", "Your application to Stripe", "", "Thanks for applying", "Stripe <no-reply@stripe.com>",
                          "Mon, 06 Oct 2026 10:00:00 +0000", "<a@b>")
    assert replies.candidates_for(msg, msg.text, [job]) == [job]
    old = mailbox.Message("2", "Stripe newsletter", "", "", "Stripe <news@stripe.com>", "Mon, 01 Sep 2026 10:00:00 +0000")
    assert replies.candidates_for(old, "", [job]) == []
    other = mailbox.Message("3", "Lunch?", "", "see you", "Pat <pat@example.com>", "Mon, 06 Oct 2026 10:00:00 +0000")
    assert replies.candidates_for(other, other.text, [job]) == []


def test_check_updates_stage_and_records_email(monkeypatch):
    db.init()
    job = _job(id="greenhouse:stripe:99")
    db.insert_jobs([{**job, "ats": "greenhouse", "external_id": "99", "apply_url": "x", "status": db.APPLIED}])
    db.set_status(job["id"], db.APPLIED)
    msg = mailbox.Message("m1", "Next steps with Stripe", "", "We'd love to set up a call.", "Recruiter <r@stripe.com>",
                          format_datetime(datetime.now(timezone.utc)), "<abc@stripe.com>")
    monkeypatch.setattr(mailbox, "configured", lambda: "imap")
    monkeypatch.setattr(mailbox, "fetch", lambda q, n, skip=None: [msg])
    sent = []
    monkeypatch.setattr(replies.notify, "send", lambda text, path="": sent.append(text))
    fake = SimpleNamespace(classify_reply=lambda e, c: ReplyVerdict(job_key=job["id"], category="interview_request",
                                                                     summary="Recruiter asks to schedule a call."))
    stats = replies.check(ReplyTrackingConfig(no_response_days=0), fake)
    assert stats["stage_changes"] == 1
    assert db.get_job(job["id"])["track_stage"] == "interviewing"
    events = [e for e in db.events_for(job["id"]) if e["kind"] == "email"]
    assert events and "Interview request" in events[0]["detail"] and "rfc822msgid" in events[0]["link"]
    assert sent and "Stripe: Interview request" in sent[0]
    # Second pass: the same email is not processed again.
    assert replies.check(ReplyTrackingConfig(no_response_days=0), fake)["emails"] == 0


def test_reposts_of_handled_jobs_are_skipped():
    from autoapply.pipeline import Pipeline

    db.init()
    rows = [
        {"id": "greenhouse:acme:1", "ats": "greenhouse", "board": "acme", "external_id": "1", "company": "Acme",
         "title": "Senior Data Engineer", "apply_url": "x", "status": db.SKIPPED},
        {"id": "greenhouse:acme:2", "ats": "greenhouse", "board": "acme", "external_id": "2", "company": "Acme",
         "title": "Senior Data Engineer ", "apply_url": "x", "status": db.PREFILTERED},
        {"id": "greenhouse:acme:3", "ats": "greenhouse", "board": "acme", "external_id": "3", "company": "Acme",
         "title": "Analytics Engineer", "apply_url": "x", "status": db.PREFILTERED},
    ]
    db.insert_jobs(rows)
    p = Pipeline.__new__(Pipeline)  # no profile/config needed for this step
    assert p.mark_reposts() == 1
    assert db.get_job("greenhouse:acme:2")["status"] == db.SKIPPED
    assert "re-post" in db.get_job("greenhouse:acme:2")["status_reason"]
    assert db.get_job("greenhouse:acme:3")["status"] == db.PREFILTERED
