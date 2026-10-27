"""Resume tailoring guardrails, using a fake Claude client (no API calls)."""

from types import SimpleNamespace

from autoapply import llm
from autoapply.apply import mapping
from autoapply.profile import Profile

PROFILE = Profile.model_validate({
    "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
    "summary": "Data analyst with 3 years of SQL.",
    "experience": [
        {"id": "acme", "company": "Acme", "title": "Analyst", "start": "2022", "end": "Present",
         "bullets": ["Built SQL pipelines that cut reporting time by 40%.", "Maintained Tableau dashboards."]},
        {"id": "beta", "company": "Beta", "title": "Intern", "start": "2021", "end": "2021",
         "bullets": ["Cleaned data in Excel."]},
    ],
    "skills": [{"name": "Tools", "items": ["SQL", "Tableau", "Excel"]}],
})


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=self.outputs.pop(0))


def B(ids, text):
    return llm.TailoredBullet(source_ids=ids, text=text)


def test_tailoring_keeps_facts_from_profile():
    draft = llm.TailoredResumeOut(
        summary="Data analyst with 3 years of SQL and dashboarding.",
        experience=[
            llm.TailoredRole(id="acme", bullets=[
                B(["acme.0"], "Engineered SQL data pipelines, reducing reporting time by 40%."),  # ok rephrase
                B(["acme.1"], "Maintained 25 Tableau dashboards."),        # invented number -> reverted
                B(["beta.0"], "Stole a bullet from another role."),        # wrong role -> dropped
            ]),
            # role "beta" omitted entirely -> restored from profile
        ],
        projects=[],
        skills=[llm.TailoredSkillGroup(name="Tools", items=["Tableau", "Kubernetes", "SQL"])],  # Kubernetes not in profile
    )
    fact_check = llm.FactCheck(issues=[])
    model = llm.LLM(PROFILE, client=FakeClient([draft, fact_check]))
    resume = model.tailor_resume({"company": "X", "title": "Analyst", "description": "SQL"})

    acme, beta = resume.experience
    assert (acme.company, acme.title, acme.start) == ("Acme", "Analyst", "2022")
    assert acme.bullets[0].text.startswith("Engineered SQL")
    assert acme.bullets[1].text == "Maintained Tableau dashboards." and acme.bullets[1].reverted
    assert len(acme.bullets) == 2
    assert [b.text for b in beta.bullets] == ["Cleaned data in Excel."]
    assert resume.skills[0].items == ["Tableau", "SQL"]


def test_fact_check_reverts_flagged_bullets():
    draft = llm.TailoredResumeOut(
        summary="Analyst who led a team.",
        experience=[llm.TailoredRole(id="acme", bullets=[B(["acme.1"], "Owned the company's BI strategy via Tableau.")]),
                    llm.TailoredRole(id="beta", bullets=[B(["beta.0"], "Cleaned data in Excel.")])],
        projects=[], skills=[],
    )
    check = llm.FactCheck(issues=[llm.FactIssue(item_id="acme#0", problem="adds ownership of strategy", fix=""),
                                  llm.FactIssue(item_id="summary", problem="no team leadership in profile",
                                                fix="Analyst with 3 years of SQL.")])
    resume = llm.LLM(PROFILE, client=FakeClient([draft, check])).tailor_resume({"company": "X", "title": "Y"})
    assert resume.experience[0].bullets[0].text == "Maintained Tableau dashboards."  # no fix → original
    assert resume.summary == "Analyst with 3 years of SQL."                         # fix applied

    bad_fix = llm.FactCheck(issues=[llm.FactIssue(item_id="summary", problem="x", fix="Analyst with 9 years.")])
    resume = llm.LLM(PROFILE, client=FakeClient([draft, bad_fix])).tailor_resume({"company": "X", "title": "Y"})
    assert resume.summary == PROFILE.summary  # a "fix" that adds a number is rejected


def test_requests_use_fallbacks_and_cached_profile():
    part = lambda n: llm.PartScore(points=n, reason="")
    client = FakeClient([llm.FitBreakdown(role_family=part(150), experience_level=part(10), location=part(10), pay=part(5),
                                          requirements=[], summary="", dealbreakers=[])])
    model = llm.LLM(PROFILE, client=client, scoring_model="claude-haiku-5-5")
    result = model.score({"company": "X", "title": "Y"}, "")
    call = client.calls[0]
    # Parts are capped and the total is added up in code: 30 + 24 (no required items listed) + 15 + 10 + 10 + 5.
    assert result["parts"]["role_family"]["points"] == 30 and result["score"] == 94
    assert call["model"] == "claude-haiku-5-5" and "fallbacks" not in call  # Haiku has no server-side fallback

    client.outputs.append(llm.CoverLetterOut(body="x"))
    model._call("t", "c", llm.CoverLetterOut, effort="medium")
    call = client.calls[1]
    assert call["model"] == "claude-sonnet-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"} and "Acme" in call["system"][0]["text"]
    assert "thinking" not in call


def test_option_matching():
    assert mapping.best_option("Decline", ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"
    assert mapping.best_option("Yes", ["Yes, I am authorized", "No"]) == "Yes, I am authorized"
    assert mapping.best_option("New York, NY", ["New York, NY, USA", "New York, USA"]) == "New York, NY, USA"
    assert mapping.best_option("Kubernetes", ["SQL", "Python"]) is None


def test_sensitive_questions_come_only_from_profile():
    assert mapping.classify("Will you now or in the future require visa sponsorship?") == "sponsorship"
    assert mapping.classify("Are you legally authorized to work in the US?") == "authorized"
    assert mapping.sensitive_value("authorized", PROFILE, True) is None  # unknown -> needs you, never guessed
    assert mapping.deterministic({"label": "How are you using AI today in your current role?", "type": "textarea"}, PROFILE) is None
    assert mapping.deterministic({"label": "Do you plan to move out of the state/country you reside in?", "type": "text"}, PROFILE) is None


def test_removal_only_edits_add_few_words():
    from autoapply.llm import _added_words

    before = "I built the CPQ tool. I use Claude daily and check everything. I like data."
    assert _added_words(before, "I built the CPQ tool. I like data.") == 0
    assert _added_words(before, "I built the CPQ tool, and I like data.") <= 2
    assert _added_words(before, before + " I also led a team of ten engineers at Google.") >= 10
