"""Timezone-aware parsing and SQLite timestamp conversion."""

from __future__ import annotations

import re

import pendulum

_OFFSET_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$")


def require_aware_datetime(value: pendulum.DateTime, *, context: str) -> pendulum.DateTime:
    """Return a datetime after rejecting missing timezone information."""
    if value.tzinfo is None:
        raise ValueError(f"{context} must include explicit timezone information")
    return value


def parse_aware_datetime(value: str, *, context: str) -> pendulum.DateTime:
    """Parse a timestamp that carries an explicit UTC offset or Z suffix."""
    if not _OFFSET_SUFFIX.search(value):
        raise ValueError(f"{context} must include an explicit UTC offset or Z suffix")
    parsed = pendulum.parse(value, strict=True)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"{context} must include a date and time")
    return require_aware_datetime(parsed, context=context)


def parse_datetime_with_default_timezone(
    value: str,
    default_timezone: str,
    *,
    context: str,
) -> pendulum.DateTime:
    """Parse provider time, applying an explicit provider fallback when needed."""
    if _OFFSET_SUFFIX.search(value):
        return parse_aware_datetime(value, context=context)
    if not default_timezone:
        raise ValueError(f"{context} requires a provider default timezone")
    if _OFFSET_SUFFIX.fullmatch(default_timezone):
        return parse_aware_datetime(
            f"{value}{default_timezone}",
            context=context,
        )
    parsed = pendulum.parse(value, strict=True, tz=default_timezone)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"{context} must include a date and time")
    return require_aware_datetime(parsed, context=context)


def parse_datetime_with_utc_offset(
    value: str,
    offset_seconds: int,
    *,
    context: str,
) -> pendulum.DateTime:
    """Parse a provider-local timestamp with its explicit response offset."""
    if abs(offset_seconds) > 14 * 60 * 60 or offset_seconds % 60:
        raise ValueError(f"{context} has an invalid UTC offset")
    sign = "+" if offset_seconds >= 0 else "-"
    hours, minutes = divmod(abs(offset_seconds) // 60, 60)
    return parse_aware_datetime(
        f"{value}{sign}{hours:02}:{minutes:02}",
        context=context,
    )


def datetime_timezone_specifier(value: pendulum.DateTime, *, context: str) -> str:
    """Return an IANA timezone name or an explicit offset for an aware time."""
    aware = require_aware_datetime(value, context=context)
    return aware.timezone_name or aware.format("Z")
