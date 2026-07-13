from pathlib import Path

import pendulum
import pytest

from weather_briefing.cli import (
    _hour_in_cron,
    _in_schedule,
    _location_state_path,
    _parse_run_time,
    _precision_reduction_notice,
    build_parser,
)
from weather_briefing.config import Settings
from weather_briefing.models import ResolvedLocation


@pytest.mark.parametrize(
    "value",
    ("2026-03-29T02:30:00", "2026-10-25T02:30:00"),
)
def test_parse_run_time_rejects_timestamp_without_timezone(value: str) -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        _parse_run_time(value, pendulum.timezone("Europe/Paris"))


def test_parse_run_time_converts_explicit_offset_to_configured_timezone() -> None:
    parsed = _parse_run_time("2026-07-11T00:00:00Z", pendulum.timezone("Asia/Shanghai"))

    assert parsed.to_iso8601_string() == "2026-07-11T08:00:00+08:00"


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

    assert _location_state_path(Path("state/weather.sqlite3"), location, 1) == Path("state/weather.sqlite3")
    assert _location_state_path(Path("state/weather.sqlite3"), location, 2) == Path("state/weather-example.sqlite3")


def test_precision_reduction_notice_contains_match_coordinates_and_action() -> None:
    location = ResolvedLocation(
        "example",
        "北京市西城区中南海1号",
        39.911389,
        116.380556,
        "CN",
        "北京市",
        "Asia/Shanghai",
        True,
        matched_name="中南海, 西城区, 北京市, 中国",
        precision_reduced=True,
    )

    notice = _precision_reduction_notice(location, Path("locations.json"))

    assert "中南海, 西城区, 北京市, 中国" in notice
    assert "39.9113890" in notice
    assert "116.3805560" in notice
    assert "locations.json" in notice
    assert "确认" in notice


class TestHourInCron:
    @pytest.mark.parametrize("hour", (9, 15, 23))
    def test_range_includes_bounds(self, hour: int) -> None:
        assert _hour_in_cron(hour, "9-23")

    @pytest.mark.parametrize("hour", (0, 8, 24))
    def test_range_excludes_outside(self, hour: int) -> None:
        assert not _hour_in_cron(hour, "9-23")

    def test_single_value(self) -> None:
        assert _hour_in_cron(8, "8")
        assert not _hour_in_cron(9, "8")

    def test_comma_separated_list(self) -> None:
        assert _hour_in_cron(8, "8,12,16")
        assert _hour_in_cron(12, "8,12,16")
        assert _hour_in_cron(16, "8,12,16")
        assert not _hour_in_cron(9, "8,12,16")

    def test_wildcard_falls_back_to_true(self) -> None:
        assert _hour_in_cron(5, "*")

    def test_stepped_wildcard_matches_only_scheduled_hours(self) -> None:
        assert _hour_in_cron(4, "*/2")
        assert not _hour_in_cron(5, "*/2")

    def test_stepped_range(self) -> None:
        assert _hour_in_cron(3, "1-23/2")
        assert not _hour_in_cron(2, "1-23/2")

    def test_comma_whitespace_tolerance(self) -> None:
        assert _hour_in_cron(12, " 9 , 12 , 16 ")


class TestInSchedule:
    def test_daily_matches_greeting_hour(self, monkeypatch) -> None:
        monkeypatch.setenv("GREETING_HOUR", "7")
        monkeypatch.setenv("GREETING_MINUTE", "30")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_match = pendulum.datetime(2026, 7, 14, 7, 0, tz=settings.timezone)
        now_no_match = pendulum.datetime(2026, 7, 14, 8, 0, tz=settings.timezone)

        assert _in_schedule("daily", now_match, settings)
        assert not _in_schedule("daily", now_no_match, settings)

    def test_hourly_matches_cron_range(self, monkeypatch) -> None:
        monkeypatch.setenv("BRIEFING_CRON", "10-18")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_in = pendulum.datetime(2026, 7, 14, 10, 0, tz=settings.timezone)
        now_out = pendulum.datetime(2026, 7, 14, 9, 0, tz=settings.timezone)

        assert _in_schedule("hourly", now_in, settings)
        assert not _in_schedule("hourly", now_out, settings)


def test_version_flag() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
