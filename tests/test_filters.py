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
