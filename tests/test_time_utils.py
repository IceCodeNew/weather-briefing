from __future__ import annotations

import pendulum
import pytest

from weather_briefing.time_utils import (
    datetime_timezone_specifier,
    parse_aware_datetime,
    parse_datetime_with_default_timezone,
    parse_datetime_with_utc_offset,
    require_aware_datetime,
)


def test_require_aware_datetime_rejects_naive_value() -> None:
    naive = pendulum.naive(2026, 7, 13, 8)
    with pytest.raises(ValueError, match="explicit timezone"):
        require_aware_datetime(naive, context="test")


def test_require_aware_datetime_returns_aware_value() -> None:
    aware = pendulum.datetime(2026, 7, 13, 8, tz="UTC")
    assert require_aware_datetime(aware, context="test") == aware


def test_parse_aware_datetime_rejects_value_without_offset() -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        parse_aware_datetime("2026-07-13T08:00:00", context="test")


def test_parse_aware_datetime_accepts_z_suffix() -> None:
    result = parse_aware_datetime("2026-07-13T08:00:00Z", context="test")
    assert result.to_iso8601_string() == "2026-07-13T08:00:00Z"


def test_parse_datetime_with_default_timezone_applies_timezone_to_value_without_offset() -> None:
    result = parse_datetime_with_default_timezone(
        "2026-07-13T08:00:00",
        "Asia/Shanghai",
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00+08:00"


def test_parse_datetime_with_default_timezone_rejects_empty_default() -> None:
    with pytest.raises(ValueError, match="provider default timezone"):
        parse_datetime_with_default_timezone(
            "2026-07-13T08:00:00",
            "",
            context="test",
        )


def test_parse_datetime_with_default_timezone_uses_explicit_offset_timezone_fallback() -> None:
    result = parse_datetime_with_default_timezone(
        "2026-07-13T08:00:00",
        "+05:30",
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00+05:30"


def test_parse_datetime_with_default_timezone_uses_aware_parser_for_z_suffix() -> None:
    result = parse_datetime_with_default_timezone(
        "2026-07-13T08:00:00Z",
        "Asia/Shanghai",
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00Z"


def test_parse_datetime_with_utc_offset_rejects_invalid_offset_magnitude() -> None:
    with pytest.raises(ValueError, match="invalid UTC offset"):
        parse_datetime_with_utc_offset(
            "2026-07-13T08:00:00",
            15 * 60 * 60 + 1,
            context="test",
        )


def test_parse_datetime_with_utc_offset_rejects_non_whole_minute_offset() -> None:
    with pytest.raises(ValueError, match="invalid UTC offset"):
        parse_datetime_with_utc_offset(
            "2026-07-13T08:00:00",
            30,
            context="test",
        )


def test_parse_datetime_with_utc_offset_handles_positive_offset() -> None:
    result = parse_datetime_with_utc_offset(
        "2026-07-13T08:00:00",
        8 * 60 * 60,
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00+08:00"


def test_parse_datetime_with_utc_offset_handles_negative_offset() -> None:
    result = parse_datetime_with_utc_offset(
        "2026-07-13T08:00:00",
        -5 * 60 * 60,
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00-05:00"


def test_parse_datetime_with_utc_offset_handles_non_hour_offset() -> None:
    result = parse_datetime_with_utc_offset(
        "2026-07-13T08:00:00",
        5 * 60 * 60 + 30 * 60,
        context="test",
    )
    assert result.to_iso8601_string() == "2026-07-13T08:00:00+05:30"


def test_datetime_timezone_specifier_returns_timezone_name() -> None:
    aware = pendulum.datetime(2026, 7, 13, 8, tz="Asia/Shanghai")
    assert datetime_timezone_specifier(aware, context="test") == "Asia/Shanghai"


def test_datetime_timezone_specifier_returns_timezone_name_for_utc() -> None:
    aware = pendulum.datetime(2026, 7, 13, 8, tz="UTC")
    assert datetime_timezone_specifier(aware, context="test") == "UTC"


def test_datetime_timezone_specifier_rejects_naive_value() -> None:
    naive = pendulum.naive(2026, 7, 13, 8)
    with pytest.raises(ValueError, match="explicit timezone"):
        datetime_timezone_specifier(naive, context="test")


def test_parse_aware_datetime_rejects_non_datetime_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.time_utils.pendulum.parse",
        lambda value, **_: pendulum.date(2026, 7, 13),
    )
    with pytest.raises(ValueError, match="must include a date and time"):
        parse_aware_datetime("2026-07-13T08:00:00Z", context="test")


def test_parse_datetime_with_default_timezone_rejects_non_datetime_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.time_utils.pendulum.parse",
        lambda value, **_: pendulum.date(2026, 7, 13),
    )
    with pytest.raises(ValueError, match="must include a date and time"):
        parse_datetime_with_default_timezone("2026-07-13T08:00:00", "Asia/Shanghai", context="test")
