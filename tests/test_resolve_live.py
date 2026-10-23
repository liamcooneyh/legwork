"""Hits the real public ATS APIs. Run with: pytest -m network"""

import pytest

from autoapply import db
from autoapply.settings import LinkedInAlertsConfig
from autoapply.sources import FETCHERS
from autoapply.sources.resolve import BoardResolver

pytestmark = pytest.mark.network


@pytest.fixture(autouse=True)
def _db():
    db.init()


@pytest.mark.parametrize("company,expected", [
    ("Figma", ("greenhouse", "figma")),
    ("Flatiron Health", ("greenhouse", "flatironhealth")),
    ("Palantir Technologies", ("lever", "palantir")),
    ("Ramp", ("ashby", "ramp")),
    ("Hex Technologies Inc.", ("ashby", "hex")),
    ("Zzyzx Imaginary Widgets LLC", None),
])
def test_resolve_board(company, expected):
    assert BoardResolver(LinkedInAlertsConfig()).resolve_board(company) == expected


def test_find_posting_by_title():
    real = FETCHERS["greenhouse"]("figma")[0]
    found = BoardResolver(LinkedInAlertsConfig()).find_posting("Figma, Inc.", real.title, real.location)
    assert found.job is not None and found.job.id == real.id

    missing = BoardResolver(LinkedInAlertsConfig()).find_posting("Figma", "Underwater Basket Weaving Lead", "")
    assert missing.job is None and "No posting" in missing.reason


def test_same_name_companies_checks_every_board():
    """Two companies named Lovable: Greenhouse (Italy) and Ashby (the AI company). The match must
    come from whichever board actually has the posting."""
    real = next(j for j in FETCHERS["ashby"]("lovable") if "engineer" in j.title.lower())
    found = BoardResolver(LinkedInAlertsConfig()).find_posting("Lovable", real.title, real.location)
    assert found.job is not None and found.job.ats == "ashby" and found.job.board == "lovable"
