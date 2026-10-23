"""Profile editing, history, the "I've done this" flow, and rescues, through the web app."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autoapply import db, profile_store
from autoapply.llm import ProfileAddition


@pytest.fixture()
def client(monkeypatch):
    db.init()
    from autoapply.web import app as webapp
    return TestClient(webapp.app)


def test_profile_starts_from_yaml_and_keeps_history(client):
    first = profile_store.load()
    assert profile_store.latest() is not None
    r = client.post("/profile/section/facts", data={"facts": "Fact one\nFact two"}, follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    assert profile_store.load().extra_facts == ["Fact one", "Fact two"]
    versions = profile_store.versions()
    assert len(versions) >= 2 and versions[0]["note"] == "Edited facts"
    # Restore the original: saved as a new version, nothing lost.
    oldest = versions[-1]["id"]
    client.post(f"/profile/restore/{oldest}")
    assert profile_store.load().extra_facts == first.extra_facts
    assert profile_store.versions()[0]["note"] == f"Restored version {oldest}"
    assert client.get("/profile").status_code == 200
    assert client.get("/profile/history").status_code == 200


def test_role_bullets_edit_remove_add(client):
    role = profile_store.load().experience[0]
    form = {"title": role.title, "company": role.company, "location": role.location, "start": role.start, "end": role.end,
            "new_bullet": "Added a brand new bullet."}
    for i, b in enumerate(role.bullets):
        form[f"bullet_{i}"] = b
    form["remove_0"] = "1"
    client.post(f"/profile/section/role:{role.id}", data=form)
    updated = profile_store.load().experience[0]
    assert updated.bullets[0] == role.bullets[1] and updated.bullets[-1] == "Added a brand new bullet."
    assert len(updated.bullets) == len(role.bullets)


def test_raw_editor_rejects_invalid(client):
    r = client.post("/profile/raw", data={"text": "personal: {first_name: 1"})
    assert r.status_code == 200 and "Not saved" in r.text


def test_gap_fix_adds_bullet_and_rescores(client, monkeypatch):
    from autoapply import pipeline
    db.insert_jobs([{"id": "greenhouse:t:1", "ats": "greenhouse", "board": "t", "external_id": "1", "company": "T",
                     "title": "Analyst", "apply_url": "u", "status": db.REJECTED, "description": "needs A/B testing"}])
    role_id = profile_store.load().experience[0].id
    fake_llm = SimpleNamespace(
        suggest_addition=lambda gap, what, job: ProfileAddition(kind="bullet", role_id=role_id, skill_group="",
                                                                text="Run A/B tests on marketing campaigns."),
        score=lambda job, prefs, **kw: {"score": 82, "dealbreakers": [], "parts": {}, "summary": ""},
        model="claude-sonnet-5-5",
    )
    monkeypatch.setattr(pipeline.Pipeline, "llm", property(lambda self: fake_llm))
    r = client.post("/job/greenhouse:t:1/gap", data={"gap": "No A/B testing", "what": "i run ab tests at acme"})
    assert r.status_code == 200 and "Run A/B tests on marketing campaigns." in r.text
    r = client.post("/profile/add", data={"kind": "bullet", "role_id": role_id, "text": "Run A/B tests on marketing campaigns.",
                                          "job_id": "greenhouse:t:1", "gap": "No A/B testing"}, follow_redirects=False)
    assert "Re-scored" in r.headers["location"]
    assert "Run A/B tests on marketing campaigns." in profile_store.load().experience[0].bullets
    assert db.get_job("greenhouse:t:1")["score"] == 82 and db.get_job("greenhouse:t:1")["status"] == db.MATCHED


def test_rescue_teaches_title_screen(client, monkeypatch):
    from autoapply.web import app as webapp
    monkeypatch.setattr(webapp, "_score_in_background", lambda job_id: None)
    db.insert_jobs([{"id": "greenhouse:t:2", "ats": "greenhouse", "board": "t", "external_id": "2", "company": "T",
                     "title": "Revenue Programs Lead", "apply_url": "u", "status": db.SCREENED_OUT}])
    assert client.get("/filtered?tab=screen").status_code == 200
    client.post("/job/greenhouse:t:2/rescue", data={"back": "/filtered?tab=screen"})
    assert db.get_job("greenhouse:t:2")["status"] == db.PREFILTERED
    assert "Revenue Programs Lead" in db.kv_json("screen_examples", [])
