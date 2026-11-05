from types import SimpleNamespace

from autoapply import panel
from autoapply.llm import ResumeBullet, ResumeProject, ResumeRole, TailoredResume
from autoapply.profile import Profile

PROFILE = Profile.model_validate({
    "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com", "phone": "555-0100",
                 "github": "github.com/janedoe", "location": {"city": "New York", "state": "NY"}},
    "summary": "Builds pipelines.",
    "education": [{"school": "State U", "degree": "B.S.", "end": "2022"}],
})
RESUME = TailoredResume(
    summary="Jane Doe builds data pipelines.",
    experience=[ResumeRole(id="a", company="Acme", title="Data Engineer", location="Remote", start="2024", end="Present",
                           bullets=[ResumeBullet(text="Built SQL pipelines.", source_ids=["a.0"])])],
    projects=[ResumeProject(id="p", name="App", url="https://github.com/janedoe/app", dates="2026",
                            bullets=[ResumeBullet(text="Shipped an app.", source_ids=["p.0"])])],
    skills=[])
JOB = {"id": "greenhouse:acme:1", "company": "Acme", "title": "Data Engineer", "description": "Build pipelines."}


def test_resume_text_is_anonymous():
    text = panel.resume_text(RESUME, PROFILE)
    assert "Jane" not in text and "Doe" not in text and "janedoe" not in text and "example.com" not in text
    assert "Built SQL pipelines." in text and "public GitHub repo" in text and "New York, NY" in text


class FakeClient:
    """Writes a field of nine, then ranks every stack with the candidate placed by `place`."""
    def __init__(self, place):
        self.place, self.field_calls, self.stacks = place, 0, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, *, model, output_format, messages, **_):
        if output_format is panel.Field:
            self.field_calls += 1
            out = panel.Field(applicants=[panel.Applicant(tier=["strong", "adjacent", "partial", "senior", "weak"][i % 5], sketch=f"s{i}", resume=f"OTHER {i}") for i in range(9)])
        else:
            content = messages[0]["content"]
            ids = panel.LETTERS[:10]
            me = next(i for i in ids if f'<resume id="{i}">\nCANDIDATE' in content)
            self.stacks.append((me, content))
            others = [i for i in ids if i != me]
            ranking = others[: self.place] + [me] + others[self.place:]
            out = panel.StackReview(ranking=ranking, advance=ranking[:2],
                                    notes=[panel.Note(id=i, note=f"note {i}") for i in ids])
        return SimpleNamespace(parsed_output=out, usage=None)


def test_panel_ranks_variants_against_the_same_field(tmp_path):
    client = FakeClient(place=0)
    p = panel.Panel(client=client, out_dir=tmp_path, reviewers={"a": ("m", "persona a"), "b": ("m", "persona b")}, log=lambda m: None)
    text = panel.resume_text(RESUME, PROFILE)
    rows = p.run([JOB], {"old": {JOB["id"]: text}, "new": {JOB["id"]: text + "\nmore"}}, workers=1)
    assert client.field_calls == 1 and (tmp_path / "competitors").exists()     # one field, saved
    assert all(r["rank"] == 1 and r["advanced"] for r in rows) and len(rows) == 4
    by = {(r["variant"], r["reviewer"]): r["slot"] for r in rows}
    assert by[("old", "a")] == by[("new", "a")] and by[("old", "b")] == by[("new", "b")]   # same order for both versions
    summary = panel.summarize(rows)
    assert "4/4 advanced" not in summary and "**old:** 2/2 advanced (100%), average rank 1.0" in summary

    client2 = FakeClient(place=5)
    rows = panel.Panel(client=client2, out_dir=tmp_path, reviewers={"a": ("m", "x")}, log=lambda m: None).run(
        [JOB], {"old": {JOB["id"]: text}}, workers=1)
    assert client2.field_calls == 0                                           # reused the saved field
    assert rows[0]["rank"] == 6 and not rows[0]["advanced"] and len(rows[0]["picked_instead"]) == 2
    assert len(rows[0]["beaten_by"]) == 5 and "Is the panel sane?" in panel.summarize(rows)
