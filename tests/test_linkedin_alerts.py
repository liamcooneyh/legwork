from pathlib import Path

import pytest

from autoapply.sources.linkedin_alerts import parse_alert_html, parse_alert_text
from autoapply.sources.resolve import slug_variants, title_similarity

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED = [
    ("4100000001", "Senior Data Engineer", "Northwind Analytics", "New York, NY (Hybrid)"),
    ("4100000002", "Analytics Engineer", "Contoso Health", "United States"),
    ("4100000003", "Business Intelligence Analyst", "Fabrikam", "Remote"),
    ("4100000004", "Sales Engineer", "Tailspin Toys", "United States"),
]


@pytest.mark.parametrize("parser,fixture", [(parse_alert_html, "linkedin_alert.html"),
                                            (parse_alert_text, "linkedin_alert.txt")])
def test_parse_alert(parser, fixture):
    jobs = parser((FIXTURES / fixture).read_text())
    assert [(j.linkedin_id, j.title, j.company, j.location) for j in jobs] == EXPECTED
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4100000001/"


def test_slug_variants():
    assert slug_variants("Acme Labs, Inc.") == ["acmelabs", "acme-labs", "acme", "acmelabsinc"]
    assert slug_variants("Hex Technologies")[:2] == ["hextechnologies", "hex-technologies"]
    assert "hex" in slug_variants("Hex Technologies")
    assert slug_variants("Flatiron Health")[:2] == ["flatironhealth", "flatiron-health"]


def test_title_similarity():
    assert title_similarity("Sr. Data Engineer", "Senior Data Engineer") == 1.0
    assert title_similarity("Data Engineer, Analytics", "Analytics Data Engineer") == 1.0
    assert title_similarity("Data Engineer", "Account Executive") < 0.5


def test_parse_alert_created_confirmation():
    jobs = parse_alert_text((FIXTURES / "linkedin_alert_created.txt").read_text())
    assert [(j.linkedin_id, j.title, j.company, j.location) for j in jobs] == [
        ("4200000001", "Data Analyst", "Woodgrove Bank", "United States"),
        ("4200000002", "Analytics Consultant", "Litware", "California, United States"),
    ]
