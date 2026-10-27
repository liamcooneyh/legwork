"""Deterministic field answers, sensitive-question classification, and option matching."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..profile import Profile

DECLINE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not to (say|answer|disclose|self)|rather not|choose not", re.I)


def _words(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", (s or "").lower()).split())


def best_option(value: str, options: list[str], threshold: float = 0.72) -> str | None:
    """Pick the form option that means `value`."""
    if not options or not value:
        return None
    v = _words(value)
    if v in ("decline", "declined", "prefer not to say"):
        return next((o for o in options if DECLINE.search(o)), None)
    exact = [o for o in options if _words(o) == v]
    if exact:
        return exact[0]
    if v in ("yes", "no"):
        starts = [o for o in options if _words(o).split()[:1] == [v]]
        return starts[0] if len(starts) == 1 else None
    prefix = [o for o in options if _words(o).startswith(v) or v.startswith(_words(o))]
    if len(prefix) == 1:
        return prefix[0]
    scored = sorted(((SequenceMatcher(None, v, _words(o)).ratio(), o) for o in options), reverse=True)
    if scored and scored[0][0] >= threshold:
        return scored[0][1]
    contains = [o for o in options if v and v in _words(o)]
    return contains[0] if len(contains) == 1 else None


# ------------------------------------------------------------------ sensitive questions

SENSITIVE = [
    ("sponsorship", r"sponsor|visa|h-?1b|immigration (status|support)"),
    ("authorized", r"authori[sz]ed to work|work authori[sz]ation|eligible to work|legally (able|permitted|authori)|right to work|permanent work"),
    ("salary", r"salary|compensation|pay (expectation|requirement|range)|expected (pay|comp)|desired pay|rate expectation"),
    ("relocate", r"relocat"),
    ("criminal", r"convicted|criminal|felony|background check|misdemeanor"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("transgender", r"transgender"),
    ("orientation", r"sexual orientation"),
    ("pronouns", r"pronoun"),
    ("hispanic", r"hispanic|latin[oax]"),
    ("race", r"\brace\b|ethnicit"),
    ("veteran", r"veteran|military (service|status)"),
    ("disability", r"disabilit"),
    ("consent", r"privacy (policy|notice|statement)|consent|i acknowledge|i agree|terms (of|and)|certify that|data (processing|retention)"),
]


def classify(label: str) -> str | None:
    text = label.lower()
    for category, pattern in SENSITIVE:
        if re.search(pattern, text):
            return category
    return None


def _yn(value: bool | None) -> str | None:
    return None if value is None else ("Yes" if value else "No")


def sensitive_value(category: str, profile: Profile, accept_consents: bool) -> str | None:
    """The profile's answer for a sensitive question, or None if the profile doesn't say."""
    wa, pref, eeo = profile.work_authorization, profile.preferences, profile.eeo
    return {
        "authorized": _yn(wa.authorized_us),
        "sponsorship": _yn(wa.requires_sponsorship),
        "salary": pref.desired_salary or None,
        "relocate": _yn(pref.willing_to_relocate) or pref.relocation_notes or None,
        "criminal": None,
        "gender": eeo.gender,
        "transgender": eeo.transgender,
        "orientation": eeo.sexual_orientation,
        "pronouns": eeo.pronouns or "Decline",
        "hispanic": eeo.hispanic_latino,
        "race": eeo.race_ethnicity,
        "veteran": eeo.veteran_status,
        "disability": eeo.disability_status,
        "consent": "I agree / acknowledge" if accept_consents else None,
    }.get(category)


# ------------------------------------------------------------------ deterministic fields

def _current_role(profile: Profile):
    return profile.experience[0] if profile.experience else None


def deterministic(field: dict, profile: Profile) -> str | None:
    """Answer standard contact fields without the model. Returns None when not a standard field."""
    label = _words(field.get("label", ""))
    hint = _words(field.get("hint", ""))
    both = f"{label} {hint}"
    p = profile.personal
    role = _current_role(profile)

    def has(*words: str) -> bool:
        return any(re.search(rf"\b{w}\b", both) for w in words)

    # Broad keywords only count on short labels, so "Do you plan to move out of the
    # state/country..." isn't mistaken for a Country field.
    short = len(label.split()) <= 6

    if field["type"] == "file":
        return None
    if has("first name", "given name", "first_name", "fname") and not has("preferred"):
        return p.first_name
    if has("last name", "family name", "surname", "last_name", "lname"):
        return p.last_name
    if has("preferred first name", "preferred name", "nickname"):
        return p.preferred_name or p.first_name
    if label in ("name", "full name", "your name", "legal name", "full legal name") or has("full name"):
        return p.full_name
    if has("email", "e mail") and not has("confirm"):
        return p.email
    if has("confirm email"):
        return p.email
    if has("phone", "mobile", "telephone", "tel") and field["type"] != "combobox":
        return p.phone or None
    if has("linkedin"):
        return p.linkedin or None
    if has("github"):
        return p.github or None
    if short and has("portfolio") and not has("linkedin"):
        return p.website or p.github or None
    if short and has("personal website", "website", "other website", "blog") and not has("linkedin", "github"):
        return p.website or None
    if has("twitter", "x com"):
        return None
    if short and has("country") and not has("phone"):
        return p.location.country or None
    if (has("current location", "where are you located", "where do you currently live", "what city")
            or (short and has("location city", "city", "location"))) and not has("relocat", "prefer", "willing", "office"):
        return p.location.display() or None
    if has("most recent employer") or (short and has("current company", "current employer", "company", "organization", "org")):
        return role.company if role else None
    if has("most recent job title") or (short and has("current title", "job title", "current role", "position title")):
        return role.title if role else None
    if short and has("school", "university", "college") and profile.education:
        return profile.education[0].school.split("—")[0].strip()
    for sa in profile.standard_answers:
        if sa.question.lower() in field.get("label", "").lower():
            return sa.answer
    return None


def file_role(field: dict) -> str | None:
    text = f"{field.get('label', '')} {field.get('hint', '')}".lower()
    if re.search(r"resume|cv\b|curriculum", text):
        return "resume"
    if re.search(r"cover", text):
        return "cover_letter"
    return None


HEARD_ABOUT = re.compile(r"how did you (hear|find|learn|come across)|where did you (hear|find|see)|referral source|source of (your )?application", re.I)


def heard_about_options(field: dict, job: dict) -> list[str]:
    """Truthful answers to "How did you hear about us?", most specific first."""
    if not HEARD_ABOUT.search(field.get("label", "")):
        return []
    via_linkedin = job.get("source") == "linkedin_alert"
    picks = ["LinkedIn"] if via_linkedin else []
    picks += ["Company website", "Careers page", "Company careers page", "Website", "Job board", "Online job board",
              "Internet search", "Other"]
    return picks


# ------------------------------------------------------------------ cross-checking Claude's answers
#
# Claude answers every question (it reads the wording in context, which keyword rules can't).
# For the questions where a wrong answer is costly, the rules above work out what the profile
# says independently; if the two disagree, nobody wins silently: the question goes to you.

ACKNOWLEDGE = re.compile(r"\b(i understand|i acknowledge|i agree|i consent|i certify|i confirm|i have read|i accept)\b", re.I)


def ticks(value: str) -> bool:
    """Whether an answer for a lone checkbox means "tick it". Claude often answers with the box's
    own text ("I understand", "White") rather than "Yes"."""
    v = _words(value)
    if not v or v in ("no", "false", "0", "off", "unchecked", "not checked", "leave unchecked"):
        return False
    return polarity(value) != "No" or bool(ACKNOWLEDGE.search(value))


CHECKED = {"authorized", "sponsorship", "salary", "gender", "transgender", "orientation", "hispanic", "race",
           "veteran", "disability"}
CONTACT = ("first name", "last name", "full name", "email", "phone")


def question_text(label: str) -> str:
    """The question actually being asked: long labels often open with a preamble ("This role
    requires... sponsorship...") before the question itself."""
    text = " ".join((label or "").split())
    parts = re.split(r"(?<=[.?!])\s+(?=[A-Z(])", text)
    asked = [p for p in parts if p.rstrip(" *").endswith("?")]
    if asked:
        return asked[-1]
    return parts[-1] if len(text) > 160 else text


def classify_question(label: str) -> tuple[str | None, bool]:
    """Category of the question itself (not its preamble), and whether it is ambiguous: one
    question about both work authorization and sponsorship can't be checked by a rule."""
    q = question_text(label).lower()
    hits = sorted((m.start(), cat) for cat, pattern in SENSITIVE if (m := re.search(pattern, q)))
    if not hits:
        return None, False
    cats = {c for _, c in hits}
    return hits[0][1], {"authorized", "sponsorship"} <= cats


def polarity(text: str) -> str | None:
    t = _words(text)
    if re.match(r"(yes|true|i am|i do|i have|i will)\b", t) and not re.search(r"\bnot\b|n t\b", t):
        return "Yes"
    if re.match(r"(no|false)\b", t) or re.search(r"\b(not|never)\b|don t|won t|do not|will not", t):
        return "No"
    return None


def salary_numbers(text: str) -> list[float]:
    out = []
    for num, k in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", (text or "").lower()):
        n = float(num.replace(",", "")) * (1000 if k else 1)
        if n >= 1000:
            out.append(n)
    return out


SALARY_FLOOR = 125_000   # never ask below this when the posting's range allows more
SALARY_BAND = 30_000
HOURS_PER_YEAR = 2080


def _pay_numbers(text: str, hourly: bool = False) -> list[float]:
    """Pay figures in a salary string: yearly amounts, or hourly rates when it says per hour."""
    if hourly or re.search(r"hour|\bhr\b|/\s*h\b", text or "", re.I):
        return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text) if 5 <= float(n.replace(",", "")) < 1000]
    return salary_numbers(text)


def salary_band(compensation: str | None) -> str | None:
    """What to ask for when the posting lists pay: a $30k band ending at the top of their range,
    starting no higher than their midpoint (wide ranges span levels) and no lower than $125k or
    their own minimum.
    If their top is under $125k, their top: an answer above a posted range can get filtered out."""
    if not compensation:
        return None
    hourly = bool(re.search(r"hour|\bhr\b|/\s*h\b", compensation, re.I))
    nums = _pay_numbers(compensation)
    scale = HOURS_PER_YEAR if hourly else 1
    if not nums or not all(30_000 <= n * scale <= 1_000_000 for n in nums):
        return None
    low_posted, top = min(nums), max(nums)
    floor, width = SALARY_FLOOR / scale, SALARY_BAND / scale
    low = max(floor, low_posted, min(top - width, (low_posted + top) / 2))
    # Round figures: yearly to the nearest $5k (hourly to the dollar), never below their minimum
    # or above their top.
    step = 1 if hourly else 5000

    def nearest(n: float) -> float:
        return round(n / step) * step

    hi = nearest(min(low + width, top))
    if hi > top:
        hi -= step
    lo = nearest(low)
    if lo < max(floor, low_posted) and lo + step <= hi:
        lo += step
    suffix = " per hour" if hourly else ""
    if low >= top or lo >= hi:
        return f"${hi:,.0f}{suffix}"
    return f"${lo:,.0f}–${hi:,.0f}{suffix}"


def expected(field: dict, profile: Profile, accept_consents: bool, compensation: str = "") -> tuple[str, str] | None:
    """(category, what the profile says) for a question worth cross-checking, else None.
    `compensation`: the posting's pay range, which sets the salary answer when it's listed."""
    category, ambiguous = classify_question(field.get("label", ""))
    if category in CHECKED and not ambiguous:
        if category not in ("authorized", "sponsorship", "salary") and not field.get("options"):
            return None  # demographic checks only make sense on pick-one questions, not free text
        value = (salary_band(compensation) if category == "salary" else None) or \
            sensitive_value(category, profile, accept_consents)
        return (category, value) if value else None
    if category is None:
        label = _words(field.get("label", ""))
        if any(c in label for c in CONTACT) and len(label.split()) <= 6 and not field.get("options") \
                and field.get("type") in ("text", "email", "tel", None):
            value = deterministic(field, profile)
            return ("contact", value) if value else None
    return None


def check_answer(check: tuple[str, str], values: list[str]) -> str | None:
    """None if Claude's answer agrees with the profile, else a short description of the conflict."""
    category, want = check
    got = "; ".join(values)
    if not values:
        return None  # blank is handled as "unanswered", not a conflict
    if category in ("authorized", "sponsorship"):
        said = polarity(got)
        if said is None and category == "authorized":
            # Status options ("U.S. Citizen", "Permanent Resident") answer "are you authorized?" too.
            t = got.lower()
            if re.search(r"citizen|permanent resident|green card|authori[sz]ed", t) and \
                    not re.search(r"\bnot\b|require|need|visa|sponsor|temporary|opt\b|h-?1b", t):
                said = "Yes"
        return None if said == want else f"Claude said “{got}”, your profile says {want}"
    if category == "salary":
        hourly = bool(re.search(r"hour", want, re.I))
        lo_hi = _pay_numbers(want)
        nums = _pay_numbers(got, hourly)
        if lo_hi and nums and all(min(lo_hi) * 0.98 <= n <= max(lo_hi) * 1.02 for n in nums):
            return None
        return f"Claude said “{got}”, your profile's range is {want}"
    if category == "contact":
        norm = (lambda s: re.sub(r"\D", "", s)[-10:]) if "@" not in want and re.search(r"\d{7}", re.sub(r"\D", "", want)) \
            else (lambda s: _words(s))
        return None if norm(got) == norm(want) else f"Claude said “{got}”, your profile says “{want}”"
    # Demographic questions: decline when the profile declines, answer when it doesn't.
    declines = bool(DECLINE.search(got))
    if _words(want) == "decline":
        return None if declines else f"Claude said “{got}”, your profile says Decline"
    return f"Claude chose “{got}”, your profile says “{want}”" if declines else None


# ------------------------------------------------------------------ place suggestions

US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
             "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
             "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
             "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
             "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
             "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
             "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
             "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
             "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def best_place(value: str, options: list[str], city: str = "", state: str = "", country: str = "") -> str | None:
    """Pick the right place from location suggestions: "New York City, New York, United States",
    not the state "New York, United States" or the town "York, New York"."""
    parts_v = [p.strip() for p in value.split(",") if p.strip()]
    city = _words(city or (parts_v[0] if parts_v else ""))
    st = state or (parts_v[1] if len(parts_v) > 1 else "")
    states = {_words(st), _words(US_STATES.get(st.upper(), st))} - {""}
    country = _words(country)
    best, best_score = None, 0.0
    for o in options:
        parts = [_words(p) for p in o.split(",") if p.strip()]
        if not parts:
            continue
        first, rest = parts[0], parts[1:]
        score = 0.0
        if city and first in (city, f"{city} city"):
            score += 3
        elif city and first.startswith(city):
            score += 1
        if states & set(rest):
            score += 1
        if country and country in " ".join(rest):
            score += 0.5
        if len(parts) >= 3:
            score += 0.5  # a city entry, not a state or country of the same name
        if score > best_score:
            best, best_score = o, score
    return best if best_score >= 3 else None


# ------------------------------------------------------------------ written answers

# Sentences where the writing explains its own limits to the employer ("I can't claim...", "I don't have
# a measured figure to quote") instead of answering as the candidate.
_A = r"(?:'|’)?"
META = re.compile(
    r"\b(?:can" + _A + r"t|cannot|won" + _A + r"t|don" + _A + r"t|do not|will not)\s+(?:honestly\s+|truthfully\s+|really\s+)?"
    r"(?:claim|invent|make up|fabricate|speak to|point to|quote|cite)\b"
    r"|\bisn" + _A + r"t mine\b|\bstay with what" + _A + r"s true\b"
    r"|\bI don" + _A + r"t have (?:a|any) (?:measured|specific|exact|concrete|hard|precise) (?:figure|number|metric|data|stat)"
    r"|\b(?:my|the) profile (?:doesn" + _A + r"t|does not|lacks|has no|shows no)\b"
    r"|\bthe candidate\b"
    r"|\bwithout (?:inventing|making (?:anything|something) up|overstating)\b",
    re.I)


def meta_phrase(text: str) -> str | None:
    """The first phrase that explains a gap instead of answering, or None."""
    m = META.search(text or "")
    return m.group(0) if m else None


def strip_meta(text: str) -> str:
    """Drop the sentences that contain such a phrase, keeping everything else as written."""
    paragraphs = []
    for para in (text or "").split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = [s for s in sentences if not META.search(s)]
        paragraphs.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paragraphs)).strip()
