"""Stable SQLite serialization for state values."""

from __future__ import annotations

import re
import sqlite3

import pendulum

from ..models import Article
from ..time_utils import require_aware_datetime

_STORAGE_TIME_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")


def _article_from_row(row: sqlite3.Row) -> Article:
    return Article(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        source_name=str(row["source_name"]),
        title=str(row["title"]),
        url=str(row["url"]),
        published_at=_parse_time(row["published_at"]),
        content=str(row["content"]),
        is_verbatim=bool(row["is_verbatim"]),
    )


def _parse_time(value: str) -> pendulum.DateTime:
    if not _STORAGE_TIME_PATTERN.fullmatch(value):
        raise ValueError("State timestamp must use fixed-width UTC format")
    return pendulum.from_format(value, "YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]", tz="UTC")


def _storage_time(value: pendulum.DateTime) -> str:
    aware = require_aware_datetime(value, context="State timestamp")
    return aware.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]")
