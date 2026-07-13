from pathlib import Path
from zoneinfo import ZoneInfo

from weather_briefing.cli import _location_state_path, _parse_run_time
from weather_briefing.models import ResolvedLocation


def test_parse_run_time_treats_naive_override_as_configured_timezone() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    parsed = _parse_run_time("2026-07-11T08:00:00", timezone)

    assert parsed.isoformat() == "2026-07-11T08:00:00+08:00"


def test_multiple_locations_receive_isolated_state_paths() -> None:
    location = ResolvedLocation(
        "example",
        "北京市西城区中南海",
        39.911389,
        116.380556,
        "CN",
        "北京市",
        "Asia/Shanghai",
        True,
    )

    assert _location_state_path(Path("state/weather.sqlite3"), location, 1) == Path(
        "state/weather.sqlite3"
    )
    assert _location_state_path(Path("state/weather.sqlite3"), location, 2) == Path(
        "state/weather-example.sqlite3"
    )
