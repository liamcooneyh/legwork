"""Recruiter panel: does a resume get picked over realistic competing applicants?

A dev tool, not part of the live app (`autoapply panel`). For each posting it builds a stack of ten
resumes: the candidate's plus nine realistic competing applicants, written once by Claude and saved
so every comparison runs against the same field. Reviewers with different lenses and models see the
stack anonymized and shuffled, rank it, and may advance only two. Run it on applications that were
actually rejected first: if the panel advances those often, it's too easy to trust."""
from __future__ import annotations

import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import anthropic
from pydantic import BaseModel

from . import db
from .llm import LLM, Strict, TailoredResume
from .profile import Profile
from .settings import env

STACK = 10          # resumes per stack (the candidate plus STACK - 1 competitors)
ADVANCE = 2         # how many each reviewer may advance
LETTERS = "ABCDEFGHIJKLMNOP"

# Lenses after the Recruiter Panel plugin's six (skim, tech, hiring manager, agency, skeptic) plus two
# startup readers; models are mixed so the reviewers don't all think alike.
REVIEWERS = {
    "skim": ("claude-haiku-5-5", "You are an in-house recruiter at a 3,000-person public SaaS company. You give each resume "
             "six to ten seconds: titles, companies, dates, and whether the story is clear at a glance."),
    "tech": ("claude-sonnet-5-5", "You are a technical recruiter. You list the posting's must-have skills and look for evidence "
             "of each in recent roles, with scope and results, not a bare tool list."),
    "hiring_manager": ("claude-opus-5-5", "You are the hiring manager this role reports to. You want people who have done this "
                       "exact work, at this level, with results, and who will stay long enough to matter."),
    "agency": ("claude-sonnet-5-5", "You are an agency recruiter paid only on placement. You submit candidates a client will "
               "not object to: clear level, marketable titles, an easy-to-explain history."),
    "skeptic": ("claude-opus-5-5", "You are a bar raiser who assumes every claim needs proof. Vague bullets, unverifiable "
                "metrics, inflated titles, and inconsistencies count against a resume."),
    "startup_recruiter": ("claude-sonnet-5-5", "You are the only recruiter at a 60-person Series B startup. You care about "
                          "slope and ownership more than pedigree, and you are wary of people who won't stay."),
    "seed_founder": ("claude-opus-5-5", "You are the technical co-founder of a 12-person seed-stage startup. You hire for "
                     "things people have actually built and how fast they learn; titles matter little."),
    "skim_fast": ("claude-haiku-5-5", "You are a recruiter with 400 applications for this role and an afternoon to cut them. "
                  "Anything that raises a question gets passed over; there are plenty of other resumes."),
}


class Applicant(Strict):
    sketch: str     # one line: who this applicant is (for the report, never shown to reviewers)
    resume: str     # plain-text resume, no name or contact details


class Field(Strict):
    applicants: list[Applicant]


class Note(Strict):
    id: str
    note: str       # one line: why it's ranked where it is


class StackReview(Strict):
    ranking: list[str]      # every id, best first
    advance: list[str]      # the ids you'd move to a screen (at most ADVANCE)
    notes: list[Note]       # one per id


def resume_text(resume: TailoredResume, profile: Profile) -> str:
    """A resume as an ATS would extract it, with the name and contact details left out."""
    p = profile.personal
    lines = ["CANDIDATE", p.location.display(), "", "SUMMARY", resume.summary, "", "EXPERIENCE"]
    for r in resume.experience:
        head = " · ".join(x for x in (r.title, r.company, r.location) if x)
        lines.append(f"{head} | {r.start} – {r.end}")
        for t in r.earlier_titles:
            lines.append(f"  {t.title}, {t.start} – {t.end}")
        if r.note:
            lines.append(f"  {r.note}")
        lines += [f"  - {b.text}" for b in r.bullets]
    if resume.projects:
        lines += ["", "PROJECTS"]
        for pr in resume.projects:
            lines.append(f"{pr.name}{' (public GitHub repo)' if 'github' in pr.url else ''} | {pr.dates}")
            lines += [f"  - {b.text}" for b in pr.bullets]
    if profile.education:
        lines += ["", "EDUCATION"]
        for e in profile.education:
            lines.append(f"{e.school} | {e.degree}{' · ' + e.details if e.details else ''} | {e.end}")
    if resume.skills:
        lines += ["", "SKILLS"] + [f"{g.name}: {', '.join(g.items)}" for g in resume.skills if g.items]
    text = "\n".join(lines)
    for secret in (p.full_name, p.email, p.phone, p.linkedin, p.github, p.website, p.first_name, p.last_name, p.preferred_name):
        if secret and len(secret) > 2:
            text = re.sub(re.escape(secret), "[redacted]", text, flags=re.I)
    return text


def _safe(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", job_id)


class Panel:
    def __init__(self, client: anthropic.Anthropic | None = None, out_dir: Path | None = None,
                 reviewers: dict | None = None, log: Callable[[str], None] = print):
        self.client = client or anthropic.Anthropic()
        self.out = out_dir or env.data_dir / "panel"
        self.reviewers = reviewers or REVIEWERS
        self.log = log
        self.cost = 0.0

    def _parse(self, model: str, system: str, content: str, output: type[BaseModel], max_tokens: int):
        resp = self.client.beta.messages.parse(model=model, max_tokens=max_tokens, system=system,
                                               output_format=output, messages=[{"role": "user", "content": content}])
        usage = getattr(resp, "usage", None)
        if usage is not None:
            inp, outp = LLM.PRICES.get(model, (3.0, 15.0))
            self.cost += (getattr(usage, "input_tokens", 0) * inp + getattr(usage, "output_tokens", 0) * outp) / 1e6
        if resp.parsed_output is None:
            raise RuntimeError(f"{model} returned no structured output")
        return resp.parsed_output

    def competitors(self, job: dict) -> list[Applicant]:
        """Nine realistic competing applicants for this posting, written once and reused."""
        path = self.out / "competitors" / f"{_safe(job['id'])}.json"
        if path.exists():
            return [Applicant(**a) for a in json.loads(path.read_text())]
        task = (
            f"Write {STACK - 1} realistic resumes of people who would apply to the job posting below through the company's "
            "careers page. Make the field look like a real applicant pool for this company and level, not caricatures: "
            "two strong, exact-fit candidates (the title, years, and stack the posting asks for, at credible companies); "
            "three solid but adjacent ones; two partial fits (fewer years, a career change, or a different stack); one more "
            "senior than the role; and one weak, spray-and-pray applicant. Real people have flaws, so give some of them a "
            "short tenure, a gap, or vague bullets. Each resume is plain text in this layout: the word CANDIDATE, a city, "
            "SUMMARY, EXPERIENCE (title · company · location | dates, then bullets), optional PROJECTS, EDUCATION, SKILLS; "
            "about 250-400 words; no names, emails, phone numbers, or links. For each, also give a one-line sketch of who it is.")
        content = f"<job_posting>\n{job['company']} — {job['title']}\n\n{(job.get('description') or '')[:12000]}\n</job_posting>"
        field = self._parse("claude-sonnet-5-5", task, content, Field, 16000).applicants[: STACK - 1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([a.model_dump() for a in field], indent=1))
        return field

    def review(self, job: dict, candidate: str, field: list[Applicant], reviewer: str) -> dict:
        """One reviewer ranks one stack. The candidate's slot depends only on the job and reviewer, so
        two versions of the candidate's resume face the same order."""
        model, persona = self.reviewers[reviewer]
        rng = random.Random(int(hashlib.sha256(f"{job['id']}|{reviewer}".encode()).hexdigest(), 16))
        others = [a.resume for a in field]
        rng.shuffle(others)
        slot = rng.randrange(len(others) + 1)
        stack = others[:slot] + [candidate] + others[slot:]
        ids = LETTERS[: len(stack)]
        me = ids[slot]
        content = (f"<job_posting>\n{job['company']} — {job['title']}\n\n{(job.get('description') or '')[:12000]}\n</job_posting>\n\n"
                   + "\n\n".join(f"<resume id=\"{i}\">\n{text}\n</resume>" for i, text in zip(ids, stack)))
        task = (f"{persona}\n\nThese are {len(stack)} applications to the job below; you can move at most {ADVANCE} to a "
                "recruiter screen, as you really would with a pile like this. Rank all of them best first, list the ids you "
                f"would advance (at most {ADVANCE}, fewer if none deserve it), and give each id a one-line note on why it "
                "landed where it did. Judge only job-related content.")
        out = self._parse(model, task, content, StackReview, 4000)
        ranking = [i for i in out.ranking if i in ids]
        rank = ranking.index(me) + 1 if me in ranking else len(stack)
        note = next((n.note for n in out.notes if n.id == me), "")
        ahead = [next((n.note for n in out.notes if n.id == i), "") for i in out.advance[:ADVANCE] if i != me]
        return {"job": job["id"], "company": job["company"], "title": job["title"], "fit": job.get("panel_fit"),
                "reviewer": reviewer, "model": model,
                "slot": me, "rank": rank, "advanced": me in out.advance[:ADVANCE], "note": note, "picked_instead": ahead}

    def run(self, jobs: list[dict], variants: dict[str, dict[str, str]], workers: int = 8) -> list[dict]:
        """variants: name -> {job_id: candidate resume text}. Every variant faces the same field and order."""
        fields = {}
        for j in jobs:
            fields[j["id"]] = self.competitors(j)
            self.log(f"field ready: {j['company']} — {j['title']}")
        tasks = [(v, j, r) for v, texts in variants.items() for j in jobs if j["id"] in texts for r in self.reviewers]

        def one(t):
            v, j, r = t
            try:
                return {"variant": v, **self.review(j, variants[v][j["id"]], fields[j["id"]], r)}
            except Exception as e:  # one failed call shouldn't sink a whole run
                return {"variant": v, "job": j["id"], "company": j["company"], "title": j["title"], "reviewer": r,
                        "error": str(e)[:200]}
        with ThreadPoolExecutor(workers) as ex:
            return list(ex.map(one, tasks))


def summarize(rows: list[dict]) -> str:
    """Markdown: advance rate and average rank per variant, per job, and per reviewer."""
    ok = [r for r in rows if "error" not in r]

    def stats(group: list[dict]) -> str:
        if not group:
            return "–"
        adv = sum(r["advanced"] for r in group)
        return f"{adv}/{len(group)} advanced ({adv / len(group):.0%}), average rank {sum(r['rank'] for r in group) / len(group):.1f}"

    variants = list(dict.fromkeys(r["variant"] for r in ok))
    out = ["# Panel results", "", f"Stacks of {STACK}, each reviewer advances at most {ADVANCE}. Rank 1 is best.", ""]
    out += ["## Overall", ""] + [f"- **{v}:** {stats([r for r in ok if r['variant'] == v])}" for v in variants]
    out += ["", "## By job", "", "| Job | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)]
    for job in dict.fromkeys(r["job"] for r in ok):
        g = [r for r in ok if r["job"] == job]
        fit = f" (fit {g[0]['fit']})" if g[0].get("fit") is not None else ""
        out.append(f"| {g[0]['company']} — {g[0]['title'][:50]}{fit} | " +
                   " | ".join(stats([r for r in g if r["variant"] == v]) for v in variants) + " |")
    out += ["", "## By reviewer", "", "| Reviewer | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)]
    for rev in dict.fromkeys(r["reviewer"] for r in ok):
        out.append(f"| {rev} | " + " | ".join(stats([r for r in ok if r["reviewer"] == rev and r["variant"] == v])
                                             for v in variants) + " |")
    out += ["", "## What reviewers said about the candidate", ""]
    for r in ok:
        out.append(f"- [{r['variant']}] {r['company']} / {r['reviewer']}: rank {r['rank']}"
                   f"{' (advanced)' if r['advanced'] else ''}. {r['note']}")
    errors = [r for r in rows if "error" in r]
    if errors:
        out += ["", f"{len(errors)} reviews failed: " + "; ".join(f"{e['company']}/{e['reviewer']}: {e['error'][:80]}" for e in errors[:5])]
    return "\n".join(out) + "\n"


def sent_resume(job_id: str) -> TailoredResume | None:
    """The tailored resume that went out with an application, if it was saved."""
    for a in sorted(db.applications_for(job_id), key=lambda a: (a["outcome"] == "applied", a["id"]), reverse=True):
        art = json.loads(a["artifacts_json"] or "{}")
        path = env.artifacts_dir / art.get("dir", "-") / "resume.json"
        if path.exists():
            return TailoredResume.model_validate_json(path.read_text())
    return None
