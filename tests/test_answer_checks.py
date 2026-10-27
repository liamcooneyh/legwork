"""Claude answers every form question; these rules decide which answers get double-checked and how."""

from autoapply.apply import mapping as m

ONE_PASSWORD = ("This role requires you to already be legally authorized to work in the countries listed in the job "
                "posting and 1Password will not be offering work authorization, relocation assistance, or "
                "sponsorship/transferring of a visa. Do you live in, and are you legally authorized to work in the "
                "countries listed in the job posting? *")
EXTEND = ("Will you now or in the future require immigration sponsorship by Extend to attain or maintain your "
          "employment authorization status (e.g., H-1B, E-3, TN, O-1, STEM OPT, or any immigration work "
          "authorization requiring a written submission from Extend to a government agency)?")


def test_question_not_preamble_decides_category():
    assert m.classify_question(ONE_PASSWORD) == ("authorized", False)
    assert m.classify_question("Will you now or in the future require visa sponsorship?") == ("sponsorship", False)
    assert m.classify_question("Are you legally authorized to work in the United States?") == ("authorized", False)


def test_mixed_authorization_and_sponsorship_is_ambiguous():
    assert m.classify_question(EXTEND)[1] is True
    assert m.classify_question("Are you authorized to work in the US, and will you require sponsorship?")[1] is True


def test_yes_no_conflicts():
    assert m.check_answer(("authorized", "Yes"), ["Yes"]) is None
    assert m.check_answer(("authorized", "Yes"), ["No"])
    assert m.check_answer(("sponsorship", "No"), ["No, I will not require sponsorship"]) is None
    assert m.check_answer(("sponsorship", "No"), ["Maybe"])  # unreadable counts as a conflict


def test_salary_must_fall_in_profile_range():
    want = "$120,000–$150,000"
    assert m.check_answer(("salary", want), ["$120000"]) is None
    assert m.check_answer(("salary", want), ["150k"]) is None
    assert m.check_answer(("salary", want), ["$90000"])
    assert m.check_answer(("salary", want), ["Negotiable"])


def test_demographics_follow_profile():
    assert m.check_answer(("gender", "Decline"), ["I prefer not to answer"]) is None
    assert m.check_answer(("gender", "Decline"), ["Male"])
    assert m.check_answer(("gender", "Male"), ["Man"]) is None
    assert m.check_answer(("gender", "Male"), ["Decline to self-identify"])


def test_demographic_checks_only_on_pick_one_questions():
    profile = _profile()
    free_text = {"label": "Other than your ethnicity, gender, and disability status, is there anything else?",
                 "type": "textarea", "options": []}
    assert m.expected(free_text, profile, True) is None
    pick = {"label": "Gender", "type": "select", "options": ["Male", "Female", "Decline to self-identify"]}
    assert m.expected(pick, profile, True) == ("gender", "Male")


def test_contact_check_ignores_formatting():
    assert m.check_answer(("contact", "(212) 555-0100"), ["+1 212-555-0100"]) is None
    assert m.check_answer(("contact", "jane@example.com"), ["Jane@Example.com"]) is None
    assert m.check_answer(("contact", "jane@example.com"), ["someone@else.com"])


def _profile():
    from autoapply.profile import Profile

    return Profile.model_validate({
        "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
        "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
        "preferences": {"desired_salary": "$120,000–$150,000"},
        "eeo": {"gender": "Male"},
    })


def test_citizenship_options_answer_authorization():
    assert m.check_answer(("authorized", "Yes"), ["U.S. Citizen"]) is None
    assert m.check_answer(("authorized", "Yes"), ["United States Citizen or Permanent Resident"]) is None
    assert m.check_answer(("authorized", "Yes"), ["Require sponsorship (H-1B)"])


def test_contact_check_skips_option_fields():
    profile = _profile()
    consent = {"label": "Phone Number", "type": "radio", "options": ["Yes - I consent to receiving text messages", "No"]}
    assert m.expected(consent, profile, True) is None


def test_score_is_added_up_from_parts():
    from autoapply import llm
    part = lambda n, r="": llm.PartScore(points=n, reason=r)
    b = llm.FitBreakdown(
        role_family=part(28), experience_level=part(10), location=part(10), pay=part(5), summary="s", dealbreakers=[],
        requirements=[llm.Requirement(text="SQL", kind="required", status="met", evidence="Acme", hard=False),
                      llm.Requirement(text="Jira", kind="required", status="partly", evidence="Globex", hard=False),
                      llm.Requirement(text="Looker", kind="preferred", status="not_met", evidence="not shown", hard=False)])
    out = llm.score_from_breakdown(b)
    assert out["parts"]["required"]["points"] == 22   # (1 + 0.5) / 2 of 30
    assert out["parts"]["preferred"]["points"] == 0
    assert out["score"] == 28 + 22 + 0 + 10 + 10 + 5
    assert any("Looker" in g for g in out["gaps"]) and any("SQL" in st for st in out["strengths"])


def test_salary_band_from_posted_range():
    band = m.salary_band
    assert band("$132K - $165K") == "$135,000–$165,000"       # a $30k band, not below their minimum
    assert band("$170K - $210K") == "$180,000–$210,000"       # $30k band ending at their top
    assert band("USD 147,000–165,000 (per-year-salary)") == "$150,000–$165,000"   # never below their minimum
    assert band("$141.5K - $192K") == "$160,000–$190,000"     # round figures, to the nearest $5k
    assert band("$103K - $129K") == "$125,000"                # rounded down: never above their top
    assert band("$190K - $270K") == "$230,000–$260,000"       # no higher than their midpoint
    assert band("$115K - $130K") == "$125,000–$130,000"       # the $125k floor, inside their range
    assert band("$90K - $110K") == "$110,000"                 # their top is under the floor: their top
    assert band("$55 – $65 per hour") == "$61–$65 per hour"
    assert band("Competitive") is None and band("") is None


def test_salary_check_uses_the_posting_band():
    profile = _profile()
    field = {"label": "What are your salary expectations?", "type": "text", "options": []}
    check = m.expected(field, profile, True, "$170K - $210K")
    assert check == ("salary", "$180,000–$210,000")
    assert m.check_answer(check, ["$185,000"]) is None
    assert m.check_answer(check, ["$120,000–$150,000"])
    assert m.expected(field, profile, True, "")[1] == "$120,000–$150,000"   # no posted range: the profile's
    hourly = m.expected(field, profile, True, "$55 – $65 per hour")
    assert m.check_answer(hourly, ["65"]) is None


def test_answers_that_explain_their_own_limits_are_caught():
    caught = [
        "I can't claim a personal story with 15Five that isn't mine, so I'll stay with what's true.",
        "I use AI daily. I don't have a measured figure for time saved to quote.",
        "The candidate has built several pipelines.",
        "My profile doesn't show direct Workday experience, but I learn fast.",
    ]
    for text in caught:
        assert m.meta_phrase(text), text
    fine = [
        "I can't wait to dig into usage-based billing.",
        "I haven't used Kafka in production, but I've built streaming jobs with Pub/Sub.",
        "At Zippin I built a self-service CPQ tool that cut quote turnaround by 90%.",
    ]
    for text in fine:
        assert m.meta_phrase(text) is None, text


def test_strip_meta_keeps_the_real_answer():
    text = ("Claude Code is a core part of how I build. I check the SQL and the question. "
            "I don't have a measured figure for time saved to quote.")
    assert m.strip_meta(text) == "Claude Code is a core part of how I build. I check the SQL and the question."



def test_unmet_hard_requirements_cost_real_points():
    from autoapply import llm
    part = lambda n: llm.PartScore(points=n, reason="")
    req = lambda t, st, hard: llm.Requirement(text=t, kind="required", status=st, evidence="", hard=hard)
    base = dict(role_family=part(28), experience_level=part(10), location=part(10), pay=part(5), summary="s", dealbreakers=[])
    soft = llm.score_from_breakdown(llm.FitBreakdown(**base, requirements=[req("SQL", "met", False), req("Stakeholders", "not_met", False)]))
    hard = llm.score_from_breakdown(llm.FitBreakdown(**base, requirements=[req("SQL", "met", False), req("Xactly", "not_met", True)]))
    assert soft["parts"]["required"]["points"] == 15 and hard["parts"]["required"]["points"] == 11
    many = llm.score_from_breakdown(llm.FitBreakdown(**base, requirements=[req(t, "not_met", True) for t in "abcde"] + [req("SQL", "met", False)]))
    assert many["parts"]["required"]["points"] == 0          # never below zero
    assert "unmet hard requirement" in hard["parts"]["required"]["reason"]
