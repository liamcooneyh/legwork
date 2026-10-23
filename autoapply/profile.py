"""The master profile: the single source of truth every tailored document is built from.

Claude may select, reorder and rephrase what is here, but never add to it. Answers to
sensitive questions (work authorization, sponsorship, salary, EEO) come only from this
file, never from the model.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .settings import env


class Location(BaseModel):
    city: str = ""
    state: str = ""
    country: str = "United States"

    def display(self) -> str:
        return ", ".join(p for p in (self.city, self.state) if p)


class Personal(BaseModel):
    first_name: str
    last_name: str
    preferred_name: str = ""
    email: str
    phone: str = ""
    location: Location = Field(default_factory=Location)
    linkedin: str = ""
    github: str = ""
    website: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class WorkAuthorization(BaseModel):
    # None means "not filled in": a required question on this topic sends the job to manual review.
    authorized_us: bool | None = None
    requires_sponsorship: bool | None = None
    notes: str = ""


class Preferences(BaseModel):
    desired_salary: str = ""
    willing_to_relocate: bool | None = None
    relocation_notes: str = ""
    start_date: str = ""
    notice_period: str = ""
    remote_preference: str = ""


class EEO(BaseModel):
    # Free text matched against the form's options. "Decline" picks the decline/prefer-not option.
    gender: str = "Decline"
    pronouns: str = ""
    race_ethnicity: str = "Decline"
    hispanic_latino: str = "Decline"
    veteran_status: str = "Decline"
    disability_status: str = "Decline"
    sexual_orientation: str = "Decline"
    transgender: str = "Decline"


class Experience(BaseModel):
    id: str
    company: str
    title: str
    location: str = ""
    start: str
    end: str = "Present"
    bullets: list[str] = Field(default_factory=list)

    def bullet_ids(self) -> list[tuple[str, str]]:
        return [(f"{self.id}.{i}", text) for i, text in enumerate(self.bullets)]


class Education(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    details: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""


class SkillGroup(BaseModel):
    name: str
    items: list[str]


class Project(BaseModel):
    id: str
    name: str
    url: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)

    def bullet_ids(self) -> list[tuple[str, str]]:
        return [(f"{self.id}.{i}", text) for i, text in enumerate(self.bullets)]


class StandardAnswer(BaseModel):
    question: str
    answer: str


class WritingSample(BaseModel):
    """Something the candidate wrote themselves (a cover letter, a short answer). Used only as a
    guide to their voice; facts always come from the rest of the profile."""
    title: str
    text: str


class Profile(BaseModel):
    personal: Personal
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferences: Preferences = Field(default_factory=Preferences)
    eeo: EEO = Field(default_factory=EEO)
    summary: str = ""
    alternate_summaries: list[str] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    extra_facts: list[str] = Field(default_factory=list)
    standard_answers: list[StandardAnswer] = Field(default_factory=list)
    writing_style: str = ""   # how the candidate writes; edited on the Profile page
    writing_samples: list[WritingSample] = Field(default_factory=list)

    def all_bullets(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for exp in self.experience:
            out.update(exp.bullet_ids())
        for proj in self.projects:
            out.update(proj.bullet_ids())
        return out

    def all_skills(self) -> list[str]:
        return [item for group in self.skills for item in group.items]

    def to_prompt_yaml(self) -> str:
        """The profile as Claude sees it, with bullet ids made explicit."""
        data = self.model_dump()
        # Voice material goes only to writing tasks (see LLM._voice), not into every call's facts.
        data.pop("writing_style", None)
        data.pop("writing_samples", None)
        for exp, raw in zip(self.experience, data["experience"]):
            raw["bullets"] = [{"id": bid, "text": text} for bid, text in exp.bullet_ids()]
        for proj, raw in zip(self.projects, data["projects"]):
            raw["bullets"] = [{"id": bid, "text": text} for bid, text in proj.bullet_ids()]
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_profile(path: Path | None = None) -> Profile:
    """The current profile: the latest version stored on the server (see profile_store), or the
    given YAML file."""
    if path is not None:
        with path.open() as f:
            return Profile.model_validate(yaml.safe_load(f))
    from . import profile_store

    return profile_store.load()


def resume_file(profile: Profile) -> str:
    return f"{profile.personal.first_name}_{profile.personal.last_name}_Resume.pdf"
