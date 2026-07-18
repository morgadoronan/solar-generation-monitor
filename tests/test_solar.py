from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.solar import is_production_window, solar_elevation

ZONE = ZoneInfo("America/Sao_Paulo")


def test_trindade_midday_is_in_production_window() -> None:
    instant = datetime(2026, 6, 7, 12, 15, tzinfo=ZONE)
    elevation = solar_elevation(instant, latitude=-16.65, longitude=-49.49)
    assert elevation == pytest.approx(50.8, abs=1)
    assert is_production_window(instant, latitude=-16.65, longitude=-49.49)


def test_trindade_night_is_suppressed() -> None:
    instant = datetime(2026, 6, 7, 2, tzinfo=ZONE)
    assert solar_elevation(instant, latitude=-16.65, longitude=-49.49) < 0
    assert not is_production_window(instant, latitude=-16.65, longitude=-49.49)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        solar_elevation(datetime(2026, 6, 7, 12), latitude=-16.65, longitude=-49.49)
