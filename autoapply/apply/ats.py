"""Per-ATS details: where the form lives, how to submit, how to tell it worked."""

from __future__ import annotations

import re

# Seen on real confirmation pages, e.g. Ashby: "Success … We have received your application".
RECEIVED = r"(we.ve|we have|we.ve successfully|we have successfully) received your application|your application (has been|was) (received|submitted)"
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ATS:
    name: str
    submit_selectors: list[str]
    success_text: re.Pattern
    success_url: re.Pattern | None = None
    ready_selector: str = "input[type=file]"
    skip_file_labels: list[str] = field(default_factory=list)


GREENHOUSE = ATS(
    name="greenhouse",
    submit_selectors=["button[type=submit]:has-text('Submit')", "button:has-text('Submit application')",
                      "input[type=submit]"],
    success_text=re.compile(r"thank you for applying|application (has been )?(submitted|received)|" + RECEIVED, re.I),
    success_url=re.compile(r"/confirmation|/thanks|submitted=true", re.I),
)

LEVER = ATS(
    name="lever",
    submit_selectors=["button#btn-submit", "button[data-qa=btn-submit]", "button:has-text('Submit application')"],
    success_text=re.compile(r"application submitted|thank you for applying|thanks for applying|" + RECEIVED, re.I),
    success_url=re.compile(r"/thanks", re.I),
)

ASHBY = ATS(
    name="ashby",
    submit_selectors=["button.ashby-application-form-submit-button", "button:has-text('Submit Application')"],
    success_text=re.compile(r"thanks for applying|thank you for applying|application (was )?(successfully )?submitted|" + RECEIVED, re.I),
    # Ashby's top "autofill from resume" drop zone: uploading there re-parses the resume over our answers.
    skip_file_labels=["", "autofill", "upload file"],
)

BY_NAME = {a.name: a for a in (GREENHOUSE, LEVER, ASHBY)}


def form_url(ats: str, board: str, external_id: str, apply_url: str) -> str:
    if ats == "greenhouse":
        # The embed form never redirects to the company's own careers site.
        return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={external_id}"
    return apply_url
