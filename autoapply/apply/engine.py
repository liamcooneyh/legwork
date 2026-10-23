"""Fill and (optionally) submit one application form.

Order of operations:
  1. Open the form and upload the resume first (Ashby and Lever re-parse resumes and may
     prefill fields, which we then overwrite).
  2. Extract every field with its label and options.
  3. Answer: files and standard contact fields by code, sensitive questions from explicit
     profile values, everything else with one Claude call.
  4. Any required field we cannot answer truthfully stops the run as needs_manual.
  5. Fill, screenshot, and in live mode submit and wait for a confirmation.

It never tries to solve or evade a CAPTCHA: a visible challenge means needs_manual.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..llm import LLM, LLMError
from ..profile import Profile
from ..settings import ApplyConfig
from . import mapping
from .mapping import US_STATES as US_STATE_NAMES
from .ats import BY_NAME, form_url

EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text()

OPTION_TYPES = {"select", "radio", "checkbox_group"}
SEARCH_LABELS = re.compile(r"\b(school|university|college|institution|alma mater|degree granting|employer|company name)\b", re.I)
TEXT_TYPES = {"text", "email", "tel", "url", "number", "textarea", "date"}

# Reads back, for each field key, whether the page now holds a value (true), is empty (false),
# or the field is gone (null). Used to verify a fill instead of trusting it.
STATE_JS = """(keys) => {
  const out = {};
  for (const key of keys) {
    const els = [...document.querySelectorAll(`[data-aa-key="${key}"]`)];
    if (!els.length) { out[key] = null; continue; }
    const inputs = els.filter((e) => e.tagName === "INPUT" && (e.type === "checkbox" || e.type === "radio"));
    if (inputs.length) { out[key] = inputs.some((e) => e.checked); continue; }
    const buttons = els.filter((e) => e.tagName === "BUTTON");
    if (buttons.length) { out[key] = buttons.some((b) => b.getAttribute("aria-pressed") === "true"); continue; }
    const el = els[0];
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      out[key] = !!(el.value && opt && !/^(select|choose|please select|--)/i.test(opt.text.trim()));
      continue;
    }
    if (el.type === "file") { out[key] = !!(el.files && el.files.length); continue; }
    if (el.getAttribute("role") === "combobox") {
      if (el.value && el.value.trim()) { out[key] = true; continue; }
      // react-select keeps the choice in a sibling element, not in the input.
      let n = el.parentElement, found = false;
      for (let d = 0; n && d < 4 && !found; d++, n = n.parentElement) {
        const v = n.querySelector("[class*='single-value' i], [class*='singleValue' i], [class*='multi-value' i]");
        if (v && v.innerText.trim()) found = true;
      }
      out[key] = found;
      continue;
    }
    out[key] = !!(el.value && el.value.trim());
  }
  return out;
}"""

CAPTCHA_JS = """() => {
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 100 && r.height > 60 && r.bottom > 0 && r.top < innerHeight * 3 &&
      (!el.checkVisibility || el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
  };
  return [...document.querySelectorAll('iframe')].some((f) => {
    const src = f.src || '';
    const challenge = /recaptcha\\/.*bframe|hcaptcha.*frame=challenge|challenges\\.cloudflare\\.com/.test(src);
    const checkbox = /recaptcha\\/.*anchor/.test(src) && !/size=invisible/.test(src) || /hcaptcha.*frame=checkbox/.test(src);
    return (challenge || checkbox) && shown(f);
  });
}"""

ERRORS_JS = """() => {
  const out = new Set();
  const vis = (el) => el.offsetParent !== null || getComputedStyle(el).position === 'fixed';
  document.querySelectorAll('[aria-invalid=true]').forEach((el) => {
    if (vis(el)) out.add('invalid: ' + (el.getAttribute('aria-label') || el.name || el.id || el.tagName));
  });
  document.querySelectorAll("[role=alert], .error, .error-message, .field-error, [class*='error-text' i], [class*='_error' i]").forEach((el) => {
    const t = (el.innerText || '').trim();
    if (t && t.length < 300 && vis(el)) out.add(t);
  });
  return [...out].slice(0, 12);
}"""


@dataclass
class Outcome:
    status: str                     # applied | dry_run | needs_manual | failed
    detail: str
    answers: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


@dataclass
class Planned:
    field: dict
    values: list[str]
    source: str


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


class FormFiller:
    def __init__(self, browser: Browser, job: dict, profile: Profile, llm: LLM | None, cfg: ApplyConfig,
                 out_dir: Path, resume_pdf: Path, cover_provider: Callable[[], tuple[str, Path] | None],
                 approved_answers: dict[str, list[str]] | None = None):
        self.browser = browser
        self.job = job
        self.profile = profile
        self.llm = llm
        self.cfg = cfg
        self.out_dir = out_dir
        self.resume_pdf = resume_pdf
        self.cover_provider = cover_provider
        # When submitting an application you approved: use exactly these answers (by question
        # label). A required question that wasn't in the approved set stops the submit.
        self.approved = approved_answers
        self._cover: tuple[str, Path] | None | bool = False  # False = not generated yet
        self.ats = BY_NAME[job["ats"]]
        self.log: list[str] = []
        self.screenshots: list[str] = []
        # Questions that need you, with what the dashboard needs to ask them (type, options, why).
        self.needs: dict[str, dict] = {}
        self.on_step: Callable[[str], None] = lambda step: None  # progress bar on the job page
        # False for drafts: read the form and answer every question, but leave the filling to the
        # Chrome extension in your own browser (no server-side fill, read-back, or screenshot review).
        self.fill_form = True

    # ------------------------------------------------------------------ helpers

    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _shot(self, page: Page, name: str) -> None:
        path = self.out_dir / f"{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            self.screenshots.append(path.name)
        except PlaywrightError as e:
            self._log(f"screenshot {name} failed: {e}")

    def _cover_letter(self) -> tuple[str, Path] | None:
        if self._cover is False:
            self._cover = self.cover_provider() if self.cfg.cover_letter != "never" else None
        return self._cover or None

    @staticmethod
    def _loc(page: Page, key: str, opt: str | None = None) -> Locator:
        sel = f"[data-aa-key={json.dumps(key)}]"
        if opt is not None:
            sel += f"[data-aa-opt={json.dumps(opt)}]"
        return page.locator(sel).first

    def _extract(self, page: Page) -> list[dict]:
        page.evaluate("() => document.querySelectorAll('[data-aa-key]').forEach(e => { e.removeAttribute('data-aa-key'); e.removeAttribute('data-aa-opt'); })")
        fields = page.evaluate(EXTRACT_JS, None)
        for f in fields:
            if f["type"] == "combobox":
                # Typing letters into a location box returns arbitrary places, not its option list.
                place = bool(re.search(r"location|city|country|where|based|reside|live|address|state|province", f["label"], re.I))
                # Schools and the like search a huge list as you type: what shows first is a page of it,
                # not the choices, and picking from that page gives "Albion College" for Boston University.
                search_box = bool(SEARCH_LABELS.search(f["label"]))
                f["options"] = [] if search_box else self._combobox_options(page, f["key"], probe=not place)
                f["searchable"] = search_box or not f["options"] or len(f["options"]) > 150
                if len(f["options"]) > 150:
                    f["options"] = []
        return fields

    def _combobox_options(self, page: Page, key: str, probe: bool = True) -> list[str]:
        el = self._loc(page, key)
        try:
            el.click(timeout=3000)
            page.wait_for_timeout(500)
            ctrl = el.get_attribute("aria-controls")
            opts = page.evaluate(
                """(ctrl) => { const scope = (ctrl && document.getElementById(ctrl)) || document;
                   return [...scope.querySelectorAll('[role=option]')].filter(o => o.offsetParent)
                     .map(o => o.innerText.trim()).filter(Boolean); }""", ctrl)
            if not opts and probe:
                # Some (Ashby) only list options once you type. A space shows them all on some forms;
                # others filter by substring, so a few vowels together cover nearly every option.
                seen: dict[str, None] = {}
                for probe in (" ", "a", "e", "i", "o"):
                    el.fill("")
                    el.press_sequentially(probe)
                    page.wait_for_timeout(700)
                    found = self._visible_options(page)
                    seen.update(dict.fromkeys(found))
                    if probe == " " and found:
                        break
                    if len(seen) > 150:
                        break  # a search box (cities, schools), not a short list
                opts = list(seen)
                el.fill("")
        except PlaywrightError:
            opts = []
        page.keyboard.press("Escape")
        return opts

    # ------------------------------------------------------------------ main

    def run(self, dry_run: bool) -> Outcome:
        context = self.browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US")
        page = context.new_page()
        page.set_default_timeout(self.cfg.page_timeout_seconds * 1000)
        try:
            return self._run(page, dry_run)
        except PlaywrightTimeout as e:
            self._shot(page, "error")
            return self._outcome("failed", f"Timed out: {str(e).splitlines()[0]}", [])
        except PlaywrightError as e:
            self._shot(page, "error")
            return self._outcome("failed", f"Browser error: {str(e).splitlines()[0]}", [])
        finally:
            context.close()

    def prefill(self) -> tuple:
        """Fill the form in a visible window and stop before Submit, for you to review and submit.
        Returns (context, page, outcome); the window stays open."""
        context = self.browser.new_context(viewport=None, locale="en-US")
        page = context.new_page()
        page.set_default_timeout(self.cfg.page_timeout_seconds * 1000)
        self.handoff = True
        try:
            outcome = self._run(page, dry_run=True)
        except PlaywrightError as e:
            outcome = Outcome("failed", f"Browser error: {str(e).splitlines()[0]}", [], self.screenshots, self.log)
        return context, page, outcome

    def _outcome(self, status: str, detail: str, answers: list[dict]) -> Outcome:
        self._log(f"outcome: {status} — {detail}")
        return Outcome(status, detail, answers, self.screenshots, self.log)

    def _run(self, page: Page, dry_run: bool) -> Outcome:
        url = form_url(self.job["ats"], self.job["board"], self.job["external_id"], self.job["apply_url"])
        self.on_step("form")
        self._log(f"open {url}")
        self._watch_saves(page)
        self._opened_at = time.time()
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(self.ats.ready_selector, state="attached", timeout=30000)
        except PlaywrightTimeout:
            self._shot(page, "no_form")
            return self._outcome("failed", "No application form found (posting may be closed)", [])
        page.wait_for_timeout(2000)

        fields = self._extract(page)
        self._log(f"found {len(fields)} fields")

        # 1. Resume first.
        resume_field = self._pick_resume_field(fields)
        if resume_field is None:
            return self._outcome("needs_manual", "Could not find a resume upload field", [])
        if self.fill_form:
            self.on_step("resume")
            self._loc(page, resume_field["key"]).set_input_files(str(self.resume_pdf))
            self._log(f"uploaded resume to '{resume_field['label']}'")
            page.wait_for_timeout(4000)  # let resume parsing/autofill settle
            fields = self._extract(page)

        self.on_step("answers")
        # 2. Plan answers.
        plan, blocking = self._plan(fields)
        self._planned = plan
        answers_log = self._answers_log(plan)

        if not self.fill_form:
            # 3. Pick the answers that can open follow-up questions (Greenhouse asks for race only
            # after "Hispanic/Latino: No"), answer anything new, then check every answer against
            # the form itself: the extension types exactly these.
            self.on_step("check")
            fields, revealed = self._reveal_followups(page, fields, plan)
            if revealed:
                self._log(f"{len(revealed)} follow-up question(s) appeared: answering them too")
                more, more_blocking = self._plan(revealed)
                plan += more
                blocking += more_blocking
                answers_log += self._answers_log(more)
            blocking += self._check_planned(fields, plan)
        else:
            self.on_step("fill")
            # 3. Fill.
            for p in plan:
                try:
                    self._fill(page, p)
                except (PlaywrightError, ValueError) as e:
                    self._log(f"could not fill '{p.field['label'][:60]}': {str(e).splitlines()[0]}")
                # One field at a time: some forms save or validate each answer as you go (Ashby does), and
                # overlapping saves can overwrite each other. Costs a few seconds per form.
                self._wait_saves(page, quiet_ms=400, max_s=8)
            page.wait_for_timeout(800)
            self.on_step("check")
            blocking += self._verify(page, plan, blocking)
            blocking += self._confirm_saved(page, plan, blocking)
            self._shot(page, "filled")
            if not blocking and not getattr(self, "handoff", False):
                self.on_step("review")
                blocking += self._visual_review(page, answers_log)

        if blocking:
            unique = list(dict.fromkeys(blocking))
            # What it tried stays visible; the questions for you are listed as unanswered.
            field_needs = {k for k, v in self.needs.items() if v["type"] != "check"}
            answers_log = [a for a in answers_log if a["label"] not in field_needs] + list(self.needs.values())
            for a in answers_log:
                tried = next((p for p in plan if p.field["label"] == a["label"]), None) if a["source"] == "unanswered" else None
                if tried and tried.field["type"] != "file":
                    a["tried"] = tried.values
            return self._outcome("needs_manual", "Questions need you: " + " | ".join(l[:90] for l in unique), answers_log)
        if not self.fill_form:
            # A CAPTCHA, if the site shows one, is yours to do in Chrome anyway.
            return self._outcome("dry_run", "Prepared: every question answered, ready for the extension", answers_log)
        if page.evaluate(CAPTCHA_JS):
            return self._outcome("needs_manual", "A CAPTCHA must be completed by hand", answers_log)
        if dry_run:
            return self._outcome("dry_run", "Prepared and filled; not submitted", answers_log)

        # 4. Submit.
        return self._submit(page, answers_log)

    @staticmethod
    def _rekey(plan: list["Planned"], fresh: list[dict]) -> None:
        """A fresh read of the form hands out new keys; point each planned field at its new key
        (matched by label and type) so later retries touch the right element."""
        pool: dict[tuple[str, str], list[dict]] = {}
        for f in fresh:
            pool.setdefault((f["label"], f["type"]), []).append(f)
        for p in plan:
            same = pool.get((p.field["label"], p.field["type"]))
            if same:
                p.field = {**p.field, "key": same.pop(0)["key"]}

    def _need(self, f: dict | None, note: str = "", question: str = "") -> str:
        """Record a question for you to answer on the dashboard; returns its one-line summary."""
        label = f["label"] if f else question
        key = label if f else f"check: {label}"  # a visual flag can share a field's label; keep both
        if key not in self.needs:
            self.needs[key] = {"label": label, "type": f["type"] if f else "check", "required": True,
                               "options": (f.get("options") or []) if f else [], "note": note,
                               "value": "", "source": "unanswered"}
        elif note and not self.needs[key]["note"]:
            self.needs[key]["note"] = note
        short = (mapping.question_text(label) if f else label)[:90]
        return f"{short} ({note})" if note else short

    @staticmethod
    def _answers_log(plan: list["Planned"]) -> list[dict]:
        return [
            {"label": p.field["label"], "type": p.field["type"], "required": p.field["required"],
             "value": "; ".join(Path(v).name if p.field["type"] == "file" else v for v in p.values),
             "values": [] if p.field["type"] == "file" else p.values, "source": p.source}
            for p in plan
        ]

    def _reveal_followups(self, page: Page, fields: list[dict], plan: list["Planned"]) -> tuple[list[dict], list[dict]]:
        """Pick the planned dropdown and radio answers on the server's copy of the form (nothing is
        saved or sent), then read it again. Returns the fresh fields and the ones that weren't there."""
        picks = [p for p in plan if p.field["type"] in ("select", "radio") or (p.field["type"] == "combobox" and p.field["options"])]
        if not picks:
            return fields, []
        page.set_default_timeout(5000)
        try:
            for p in picks:
                try:
                    self._fill(page, p)
                except (PlaywrightError, ValueError) as e:
                    self._log(f"couldn't pick '{p.field['label'][:60]}' to look for follow-ups: {str(e).splitlines()[0]}")
            page.wait_for_timeout(800)
            fresh = self._extract(page)
        finally:
            page.set_default_timeout(self.cfg.page_timeout_seconds * 1000)
        self._rekey(plan, fresh)
        known = {(f["label"], f["type"]) for f in fields}
        return fresh, [f for f in fresh if (f["label"], f["type"]) not in known]

    def _check_planned(self, fields: list[dict], plan: list["Planned"]) -> list[str]:
        """Without filling the form: every pick-from-a-list answer must be one of the form's own
        options, word for word, and every required question needs an answer."""
        out: list[str] = []
        for p in plan:
            f = p.field
            options = f.get("options") or []
            if options and not f.get("searchable") and (f["type"] in OPTION_TYPES or f["type"] == "combobox"):
                bad = [v for v in p.values if v not in options]
                if bad:
                    out.append(self._need(f, f"“{bad[0][:40]}” isn't one of the form's options"))
        planned = {p.field["key"] for p in plan}
        for f in fields:
            if f["required"] and f["type"] != "file" and f["key"] not in planned and f["label"] not in self.needs:
                out.append(self._need(f, "no answer prepared"))
        return out

    def _verify(self, page: Page, plan: list["Planned"], blocking: list[str]) -> list[str]:
        """Read the form back. Anything we meant to fill that is still empty gets one more try (a
        different way of clicking); then every field the form marks required must hold a value.
        This is what keeps silent misses (a box that didn't tick, a dropdown that didn't take) out
        of Ready: they become named questions in Needs you instead."""
        problems: list[str] = []
        meant = [p for p in plan if p.field["type"] != "file" and not (p.field["type"] == "checkbox" and not mapping.ticks(p.values[0]))]
        state = page.evaluate(STATE_JS, [p.field["key"] for p in meant])
        missed = [p for p in meant if state.get(p.field["key"]) is False]
        for p in missed:
            try:
                self._fill_again(page, p)
            except (PlaywrightError, ValueError) as e:
                self._log(f"retry failed for '{p.field['label'][:60]}': {str(e).splitlines()[0]}")
        if missed:
            page.wait_for_timeout(500)
            state = page.evaluate(STATE_JS, [p.field["key"] for p in missed])
            for p in missed:
                if state.get(p.field["key"]) is False:
                    self._log(f"still empty after retry: '{p.field['label'][:60]}'")
                    if p.field["required"]:
                        problems.append(self._need(p.field, "couldn't fill it in"))
                else:
                    self._log(f"filled on retry: '{p.field['label'][:60]}'")
        # Fresh read of the whole form: conditional questions may have appeared, and some fields
        # may never have been recognized. Required and empty means it needs you.
        page.evaluate("() => document.querySelectorAll('[data-aa-key]').forEach(e => { e.removeAttribute('data-aa-key'); e.removeAttribute('data-aa-opt'); })")
        fresh = page.evaluate(EXTRACT_JS, None)
        self._rekey(plan, fresh)
        state = page.evaluate(STATE_JS, [f["key"] for f in fresh])
        known = " ".join(blocking + problems).lower()
        labels = [f["label"] for f in fresh if f["label"]]
        for f in fresh:
            # A "label" that strings together several other questions is a wrapper, not a field.
            if sum(1 for other in labels if other != f["label"] and len(other) > 3 and other in f["label"]) >= 3:
                continue
            if f["required"] and f["type"] != "file" and state.get(f["key"]) is False:
                short = f["label"][:90]
                if short.lower()[:60] not in known:
                    self._log(f"required and empty after filling: '{short}'")
                    problems.append(self._need(f, "required, still empty"))
        return problems

    def _visual_review(self, page: Page, answers: list[dict]) -> list[str]:
        """Final check: Claude looks at screenshots of the filled form. Anything it flags goes to you."""
        if self.llm is None:
            return []
        try:
            height = page.evaluate("() => document.documentElement.scrollHeight")
            width = page.viewport_size["width"] if page.viewport_size else 1280
            chunk = 1400
            images = [page.screenshot(type="jpeg", quality=70, full_page=True,
                                      clip={"x": 0, "y": y, "width": width, "height": min(chunk, height - y)})
                      for y in range(0, min(height, chunk * 8), chunk) if height - y > 40]
            issues = self.llm.review_filled_form(self.job, images, answers)
        except (PlaywrightError, LLMError) as e:
            self._log(f"visual check skipped: {e}")
            return []
        for i in issues:
            self._log(f"visual check: {i.question[:80]}: {i.problem[:160]}")
        return [self._need(None, f"visual check: {i.problem[:160]}", question=i.question[:200]) for i in issues]

    def _fill_again(self, page: Page, p: "Planned") -> None:
        """Second attempt for a field that didn't take: click labels/options instead of inputs."""
        f, key = p.field, p.field["key"]
        if f["type"] in ("checkbox", "radio", "checkbox_group") and f.get("widget") != "buttons":
            opts = [None] if f["type"] == "checkbox" else p.values
            for opt in opts:
                loc = self._loc(page, key, opt)
                loc.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement; l.click(); }")
        else:
            self._fill(page, p)

    def _pick_resume_field(self, fields: list[dict]) -> dict | None:
        files = [f for f in fields if f["type"] == "file"
                 and f["label"].strip().lower() not in self.ats.skip_file_labels]
        for f in files:
            if mapping.file_role(f) == "resume":
                return f
        return files[0] if len(files) == 1 else None

    # ------------------------------------------------------------------ planning

    def _plan(self, fields: list[dict]) -> tuple[list[Planned], list[str]]:
        plan: list[Planned] = []
        blocking: list[str] = []
        ask: list[dict] = []
        checks: dict[str, tuple[str, str]] = {}

        for f in fields:
            label = f["label"]
            if f["type"] == "file":
                role = mapping.file_role(f)
                if role == "resume" or label.strip().lower() in self.ats.skip_file_labels:
                    continue
                if role == "cover_letter":
                    cover = self._cover_letter()
                    if cover:
                        plan.append(Planned(f, [str(cover[1])], "generated"))
                        continue
                if f["required"]:
                    blocking.append(self._need(f, "needs a file"))
                continue

            if self.approved is not None:
                if label in self.approved and self.approved[label]:
                    values = self.approved[label]
                    if f["options"]:
                        values = [m for m in (mapping.best_option(v, f["options"], 0.6) for v in values) if m]
                    if values:
                        plan.append(Planned(f, values, "approved"))
                        continue
                if f["required"]:
                    blocking.append(self._need(f, "new or changed since you approved"))
                continue

            if f["type"] == "textarea" and re.search(r"cover letter", label, re.I):
                cover = self._cover_letter()
                if cover:
                    plan.append(Planned(f, [cover[0]], "generated"))
                elif f["required"]:
                    blocking.append(self._need(f, "cover letter"))
                continue

            category, _ = mapping.classify_question(label)
            if f["type"] == "checkbox" and mapping.ACKNOWLEDGE.search(label):
                category = "consent"  # "I understand / acknowledge / agree" boxes, e.g. background-check notices
            if category == "consent" and f["type"] == "checkbox":
                if f["required"] and self.cfg.auto_accept_required_consents:
                    plan.append(Planned(f, ["Yes"], "consent"))
                continue
            # Everything else is answered by Claude with the whole form in view; high-stakes answers
            # are then checked against what the profile says (see mapping.expected).
            check = mapping.expected(f, self.profile, self.cfg.auto_accept_required_consents,
                                     self.job.get("compensation") or "")
            if check:
                checks[f["key"]] = check
            ask.append(f)

        if ask:
            if self.llm is None:
                blocking += [self._need(f) for f in ask if f["required"]]
            else:
                try:
                    answers = self.llm.answer_fields(self.job, ask)
                except LLMError as e:
                    self._log(f"answering failed: {e}")
                    answers = []
                by_key = {a.key: a for a in answers}
                # Required open questions sometimes come back blank when the form has several
                # similar ones; ask again for just those.
                missed = [f for f in ask if f["required"] and f["key"] not in checks
                          and not [v for v in (by_key[f["key"]].values if f["key"] in by_key else []) if v.strip()]]
                if missed:
                    try:
                        for a in self.llm.answer_fields(self.job, missed):
                            if [v for v in a.values if v.strip()]:
                                by_key[a.key] = a
                        self._log(f"re-asked {len(missed)} unanswered question(s)")
                    except LLMError as e:
                        self._log(f"re-ask failed: {e}")
                for f in ask:
                    a = by_key.get(f["key"])
                    values = [v for v in (a.values if a else []) if v and v.strip()]
                    if values and f["type"] == "combobox" and f["options"]:
                        # A typeahead's list may be only what it showed first. Snap to an option only when
                        # it's clearly the same answer; otherwise type the answer and pick from the search.
                        snapped = [mapping.best_option(v, f["options"], 0.9) for v in values[:1]]
                        if snapped[0]:
                            values = snapped
                        else:
                            f["searchable"] = True
                            values = values[:1]
                    elif values and f["type"] in OPTION_TYPES:
                        values = [m for m in (mapping.best_option(v, f["options"], 0.6) for v in values) if m]
                        if f["type"] != "checkbox_group":
                            values = values[:1]
                    if f["type"] == "checkbox" and values:
                        values = values[:1]
                    source = a.source if a else "unknown"
                    check = checks.get(f["key"])
                    if check and values:
                        conflict = mapping.check_answer(check, values)
                        if conflict:
                            self._log(f"conflict on '{f['label'][:60]}': {conflict}")
                            blocking.append(self._need(f, conflict))
                            continue
                        source = "checked"
                    if not values:
                        if f["required"]:
                            blocking.append(self._need(f, "your profile doesn't cover this"))
                        continue
                    plan.append(Planned(f, values, source))
        return plan, blocking

    # ------------------------------------------------------------------ filling

    def _fill(self, page: Page, p: Planned) -> None:
        f, values, key = p.field, p.values, p.field["key"]
        kind = f["type"]
        if kind == "file":
            self._loc(page, key).set_input_files(values[0])
        elif kind in TEXT_TYPES:
            text = values[0]
            if f.get("max_length"):
                text = text[: f["max_length"]]
            loc = self._loc(page, key)
            loc.fill(text)
            if kind == "text" and re.search(r"location|city", f["label"], re.I):
                self._pick_suggestion(page, loc, text)
        elif kind == "select":
            self._loc(page, key).select_option(label=values[0])
        elif f.get("widget") == "buttons":
            self._loc(page, key, values[0]).click()
        elif kind in ("radio", "checkbox_group"):
            for v in values:
                self._check(page, key, v)
        elif kind == "checkbox":
            if mapping.ticks(values[0]):
                self._check(page, key, None)
        elif kind == "combobox":
            self._fill_combobox(page, f, values[0])
        else:
            raise ValueError(f"unsupported field type {kind}")
        self._log(f"filled '{f['label'][:60]}' ({p.source})")

    def _check(self, page: Page, key: str, opt: str | None) -> None:
        loc = self._loc(page, key, opt)
        try:
            loc.check(force=True, timeout=5000)
        except PlaywrightError:
            loc.evaluate("e => ((e.labels && e.labels[0]) || e.closest('label') || e).click()")

    def _visible_options(self, page: Page) -> list[str]:
        return page.evaluate(
            "() => [...document.querySelectorAll('[role=option]')].filter(o => o.offsetParent)"
            ".map(o => o.innerText.trim()).filter(Boolean)")

    def _fill_combobox(self, page: Page, f: dict, value: str) -> None:
        alternatives = mapping.heard_about_options(f, self.job)
        if alternatives:
            for alt in [value, *alternatives]:
                try:
                    return self._fill_combobox_once(page, f, alt)
                except ValueError:
                    continue
            raise ValueError("no 'how did you hear' option matched")
        self._fill_combobox_once(page, f, value)

    def _fill_combobox_once(self, page: Page, f: dict, value: str) -> None:
        el = self._loc(page, f["key"])
        el.click()
        options: list[str] = []
        # Search boxes often only match the first part ("New York", not "New York, NY"), and some
        # "Location" boxes only know countries, so fall back from city to state to country.
        attempts = [value[:60], value.split(",")[0].strip()[:40]] if f.get("searchable") else [value[:25]]
        if f.get("searchable") and re.search(r"location|city|country|where|based|reside|live", f["label"], re.I):
            home = self.profile.personal.location
            attempts += [x for x in (US_STATE_NAMES.get(home.state.upper(), home.state), home.country) if x]
        loc = self.profile.personal.location
        place_like = bool(re.search(r"location|city|country|where|based|reside|live|address", f["label"], re.I))
        choice = None
        for query in dict.fromkeys(attempts):
            el.fill("")
            el.press_sequentially(query, delay=25)
            page.wait_for_timeout(1500 if f.get("searchable") else 500)
            options = self._visible_options(page)
            if not options:
                continue
            if place_like:
                # Only a confident match; otherwise try the next search ("New York" on a countries-only
                # list offers "New Caledonia", which must not be picked).
                choice = (value if value in options else None) \
                    or mapping.best_place(value, options, loc.city, loc.state, loc.country) \
                    or next((o for o in options if _norm(o) == _norm(query)), None) \
                    or mapping.best_option(value, options, 0.85)
            else:
                choice = mapping.best_option(value, options, 0.5)
                if choice is None and f.get("searchable"):
                    first_word = value.split(",")[0].strip().lower()
                    choice = next((o for o in options if o.lower().startswith(first_word)), None)
            if choice is not None or not place_like:
                break
        if choice is None:
            page.keyboard.press("Escape")
            raise ValueError(f"no option matched '{value}'")
        # Exact text among visible options only: has_text="No" would also match a hidden "Norfolk"
        # in another dropdown on the page and wait on it until timing out.
        exact = re.compile(rf"^\s*{re.escape(choice)}\s*$")
        page.locator("[role=option]:visible").filter(has_text=exact).first.click(timeout=10000)

    def _pick_suggestion(self, page: Page, loc: Locator, text: str) -> None:
        """Location inputs often only keep a value chosen from their autocomplete list, and
        only open that list on real keystrokes."""
        selectors = "[role=option], .dropdown-location, .pac-item, [class*='suggestion' i] li"
        loc.fill("")
        loc.press_sequentially(text.split(",")[0].strip(), delay=40)
        page.wait_for_timeout(2000)
        options = page.locator(selectors)
        texts = [t.strip() for t in options.all_inner_texts()]
        if not any(texts):
            loc.fill(text)  # plain text field after all
            return
        loc = self.profile.personal.location
        choice = mapping.best_place(text, texts, loc.city, loc.state, loc.country) or mapping.best_option(text, texts, 0.5)
        if choice is None:
            city = text.split(",")[0].strip().lower()
            choice = next((t for t in texts if t.lower().startswith(city)), None)
        if choice is None:
            raise ValueError(f"no location suggestion matched '{text}'")
        options.nth(texts.index(choice)).click()

    # ------------------------------------------------------------------ submit

    def _watch_saves(self, page: Page) -> None:
        """Some ATSs (Ashby) save each answer to their server in the background as you fill, and
        Submit only submits what was saved. Count those requests so we can wait for them."""
        self._inflight: set = set()
        self._failed_saves: list[str] = []

        def started(r):
            # Only the form's own saves: same site, script-issued. Bot-check and analytics scripts
            # (hCaptcha, Cloudflare, Google Analytics) keep requests open forever and must not count.
            if r.method != "POST" or r.resource_type not in ("xhr", "fetch"):
                return
            try:
                from urllib.parse import urlparse
                host, page_host = urlparse(r.url).hostname or "", urlparse(page.url).hostname or ""
            except Exception:
                return
            if "/cdn-cgi/" in r.url or not page_host or host.split(".")[-2:] != page_host.split(".")[-2:]:
                return
            self._inflight.add(r)

        def done(r):
            self._inflight.discard(r)

        def failed(r):
            self._inflight.discard(r)
            if "SetFormValue" in r.url:
                self._failed_saves.append(r.url[-60:])

        self._saved_state: dict[str, dict] | None = None

        def response(r):
            if "SetFormValue" not in r.url:
                return
            try:
                data = r.json()
            except Exception:
                return
            state = {}
            for payload in (data.get("data") or {}).values():
                for section in (payload or {}).get("sections") or []:
                    for e in section.get("fieldEntries") or []:
                        title = ((e.get("field") or {}).get("title") or "").strip()
                        if title:
                            state[title] = {"required": bool(e.get("isRequired")) and not e.get("isHidden"),
                                            "has_value": e.get("fieldValue") not in (None, "", [])}
            if state:
                self._saved_state = state

        page.on("request", started)
        page.on("requestfinished", done)
        page.on("requestfailed", failed)
        page.on("response", response)

    def _wait_saves(self, page: Page, quiet_ms: int = 1500, max_s: float = 20) -> None:
        """Wait until no background save has been in flight for `quiet_ms`."""
        deadline, quiet_since = time.time() + max_s, None
        while time.time() < deadline:
            if getattr(self, "_inflight", None):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.time()
            elif (time.time() - quiet_since) * 1000 >= quiet_ms:
                return
            page.wait_for_timeout(250)
        self._log(f"saves still in flight after {max_s:.0f}s ({len(self._inflight)})")

    def _server_missing(self, page: Page, plan: list["Planned"]) -> list[str] | None:
        """Required fields Ashby's server has no value for (None if we can't tell). A harmless
        re-save of a text answer first, so the state we read is the latest."""
        if self.job["ats"] != "ashby" or self._saved_state is None:
            return None
        self._wait_saves(page)
        touch = next((p for p in plan if p.field["type"] in ("text", "email") and p.values), None)
        if touch is not None:
            try:
                self._fill(page, touch)
            except (PlaywrightError, ValueError):
                pass
            self._wait_saves(page)
        return [t for t, v in self._saved_state.items() if v["required"] and not v["has_value"]
                and t.lower() not in ("resume", "cover letter")]

    def _confirm_saved(self, page: Page, plan: list["Planned"], blocking: list[str]) -> list[str]:
        """Ashby only submits what reached its server. Re-send anything required that didn't (twice
        at most); what still hasn't arrived goes to you instead of being submitted."""
        for attempt in range(2):
            missing = self._server_missing(page, plan)
            if not missing:
                return []
            self._log(f"Ashby's server is missing: {', '.join(m[:50] for m in missing)}")
            self._refill_missing(page, [f"Missing entry for required field: {m}" for m in missing])
        missing = self._server_missing(page, plan) or []
        known = " ".join(blocking).lower()
        out = []
        for m in missing:
            f = next((p.field for p in plan if _norm(p.field["label"]).startswith(_norm(m)[:40])), None)
            if _norm(m)[:40] not in _norm(known):
                out.append(self._need(f, "didn't save on Ashby's side") if f else self._need(None, "didn't save on Ashby's side", question=m))
        return out

    def _rejected_fields(self, page: Page, errors: list[str]) -> list["Planned"]:
        """After a rejected submit, on any platform: the planned fields to fill again. Those that are
        empty on the page, marked invalid next to their input, or named in the error text."""
        plan = [p for p in getattr(self, "_planned", []) if p.field["type"] != "file"]
        if not plan:
            return []
        keys = [p.field["key"] for p in plan]
        state = page.evaluate(STATE_JS, keys)
        invalid = page.evaluate("""(keys) => keys.filter((k) => {
            const el = document.querySelector(`[data-aa-key="${k}"]`);
            if (!el) return false;
            if (el.getAttribute("aria-invalid") === "true") return true;
            const box = el.closest("[data-field-path], fieldset, .field, .application-question, li, div");
            return !!(box && box.querySelector("[aria-invalid=true], .error, .field-error, [class*='error-text' i]"));
        })""", keys)
        named = [_norm(m.group(1))[:60] for e in errors
                 for m in re.finditer(r"(?:missing entry for required field|required field|please (?:fill|complete|answer))[:\s]+(.+)", e, re.I)]
        out = []
        for p in plan:
            label = _norm(p.field["label"])
            if state.get(p.field["key"]) is False or p.field["key"] in invalid or \
                    any(n and (label.startswith(n[:40]) or n.startswith(label[:40])) for n in named):
                out.append(p)
        return out

    def _refill_missing(self, page: Page, errors: list[str]) -> bool:
        """Fill the fields a rejected submit (or Ashby's saved state) says are missing, once more.
        True if any were found to retry."""
        again = self._rejected_fields(page, errors)
        for p in again:
            self._log(f"server didn't get '{p.field['label'][:60]}'; filling it again")
            try:
                if p.field.get("widget") == "buttons":
                    # Toggle away and back so the page registers a real change and saves it.
                    other = next((o for o in p.field.get("options", []) if o != p.values[0]), None)
                    if other:
                        self._loc(page, p.field["key"], other).click()
                        page.wait_for_timeout(400)
                    self._loc(page, p.field["key"], p.values[0]).click()
                elif p.field["type"] in ("radio", "checkbox", "checkbox_group"):
                    self._fill_again(page, p)  # a click on an already-selected option can toggle it off
                    page.wait_for_timeout(400)
                    if page.evaluate(STATE_JS, [p.field["key"]]).get(p.field["key"]) is False:
                        self._fill(page, p)
                else:
                    self._fill(page, p)
            except (PlaywrightError, ValueError) as e:
                self._log(f"refill failed: {str(e).splitlines()[0]}")
        return bool(again)

    def _submit(self, page: Page, answers: list[dict]) -> Outcome:
        button = None
        for sel in self.ats.submit_selectors:
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    button = loc.nth(i)
                    break
            if button:
                break
        if button is None:
            return self._outcome("needs_manual", "Could not find the submit button", answers)

        self._wait_saves(page)
        # Never submit sooner than a minute after opening the form: gives background saves time
        # to land everywhere, and is closer to how a person fills one in.
        wait = 60 - (time.time() - getattr(self, "_opened_at", time.time()))
        if wait > 0:
            self._log(f"waiting {wait:.0f}s before submitting")
            page.wait_for_timeout(int(wait * 1000))
            self._wait_saves(page)
        if self._failed_saves:
            self._log(f"{len(self._failed_saves)} background save(s) failed while filling")
        self._log("submitting")
        button.click()
        deadline = time.time() + 40
        errors: list[str] = []
        retried = False
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            try:
                body = page.inner_text("body")[:30000]
            except PlaywrightError:
                continue  # navigating
            if self.ats.success_text.search(body) or (self.ats.success_url and self.ats.success_url.search(page.url)):
                self._shot(page, "submitted")
                return self._outcome("applied", "Submitted; confirmation seen", answers)
            if page.evaluate(CAPTCHA_JS):
                self._shot(page, "captcha")
                return self._outcome("needs_manual", "A CAPTCHA challenge appeared on submit", answers)
            if re.search(r"verification code (was|has been) sent|enter the \d+-character code|security code", body, re.I):
                # A check that a human is submitting (Greenhouse emails a code). Not ours to get past:
                # nothing was submitted; you finish this one on the site.
                self._shot(page, "verification_code")
                return self._outcome("needs_manual", "The site emailed you a security code to confirm a person is "
                                     "applying. Nothing was submitted: finish this one on the site yourself (your "
                                     "answers and materials are on this page)", answers)
            errors = page.evaluate(ERRORS_JS)
            if errors and not retried:
                # Rejected before anything was submitted: re-send the missing answers once, then retry.
                retried = True
                if self._refill_missing(page, errors):
                    self._wait_saves(page)
                    self._shot(page, "validation_first")
                    self._log("submitting again")
                    button.click()
                    deadline = time.time() + 40
                    continue
            if errors and time.time() > deadline - 30:
                self._shot(page, "validation")
                return self._outcome("failed", "Form rejected: " + " | ".join(errors), answers)
        self._shot(page, "after_submit")
        errors = page.evaluate(ERRORS_JS)
        if errors:
            return self._outcome("failed", "Form rejected: " + " | ".join(errors), answers)
        # Unknown result: never retry automatically (it could double-apply). A human checks.
        return self._outcome("needs_manual", "Submitted but no confirmation seen — check your email", answers)
