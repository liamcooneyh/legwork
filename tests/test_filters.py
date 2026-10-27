import pytest

from autoapply.filters import location_ok
from autoapply.settings import load_config

CFG = load_config().search


@pytest.mark.parametrize("location,remote,ok", [
    ("Remote - US", True, True),
    ("Remote U.S.", True, True),
    ("United States (Remote)", False, True),
    ("USA", False, True),
    ("Remote", True, True),
    ("Remote, Global", True, True),
    ("New York, NY", False, True),
    ("Boston, MA", False, True),
    ("United States - MA Remote", True, True),
    ("Remote (US) / San Francisco, CA / New York, NY (HQ)", True, True),
    ("New York / Stockholm / San Francisco", False, True),
    ("Paris", True, False),
    ("Bucharest / Cluj / Iasi / Sibiu / Remote-Timisoara", True, False),
    ("Remote-Texas / Remote-Florida / Remote-Arizona", True, False),
    ("Remote - Seattle", True, False),
    ("Hybrid - San Francisco", True, False),
    ("San Francisco HQ", False, False),
    ("Remote - Canada", True, False),
    ("Remoto", True, False),
    ("Sydney", True, False),
    ("Chicago, IL", False, False),
])
def test_location_ok(location, remote, ok):
    assert location_ok(location, remote, CFG) is ok


def test_company_names_come_from_the_posting():
    from autoapply.db import posted_name

    assert posted_name("Imentor", "At iMentor we pair mentors. iMentor serves students. Email careers@imentor.org") == "iMentor"
    assert posted_name("Openai", "OpenAI is hiring. Join OpenAI. openai.com/careers") == "OpenAI"
    assert posted_name("Fivetran ", "Fivetran moves data.") == "Fivetran"
    assert posted_name("Stripe", "Stripe builds. Stripe ships. STRIPE.") == "Stripe"       # already right
    assert posted_name("Acme", "Visit acme.com or write jobs@acme.io") == "Acme"           # addresses don't count
    assert posted_name("Notion", "We use notion and Notion daily") == "Notion"             # a tie keeps the board's
