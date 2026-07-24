"""Validated localized scaffold tables."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import TypeGuard

from .data.resources import ReferenceDataError, reference_value
from .languages import normalize_language_tag

_LOCALIZATION_FIELDS = {
    "air_quality": frozenset(
        {
            "separator",
            "unavailable",
            "forecast_time",
            "observation_time",
            "observation",
            "forecast",
            "time_kind",
            "aqi",
            "aqi_summary",
            "pm25_aqi",
            "pm25_summary",
            "pm25",
            "health",
        }
    ),
    "allergen": frozenset(
        {"separator", "unavailable", "observed_at", "allergens", "overall", "health", "count", "level"}
    ),
    "briefing": frozenset(
        {
            "weather",
            "warnings",
            "disasters",
            "advice",
            "attribution",
            "html_source_separator",
            "plain_source_separator",
            "sources",
            "status_open",
            "status_close",
            "detail_separator",
        }
    ),
    "qweather": frozenset({"day", "lifestyle", "unknown", "no_details"}),
    "weather_document": frozenset(
        {
            "separator",
            "section_separator",
            "unavailable",
            "updated_at",
            "forecast",
            "lifestyle",
            "summary",
            "lifestyle_count",
        }
    ),
}
_LOCALIZATION_LANGUAGES = frozenset({"zh-CN", "zh-TW", "en", "ja"})


def _is_normalized_language(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return normalize_language_tag(value) == value
    except ValueError:
        return False


def _is_localization_labels(value: object, expected_fields: frozenset[str]) -> TypeGuard[dict[str, str]]:
    return (
        isinstance(value, dict)
        and set(value) == expected_fields
        and all(isinstance(item, str) and item.strip() for item in value.values())
    )


@cache
def localization_table(name: str) -> Mapping[str, Mapping[str, str]]:
    """Return one fully validated localized scaffold table."""
    tables = reference_value("localization.json", "tables")
    aliases = reference_value("localization.json", "aliases")
    if (
        not isinstance(tables, dict)
        or set(tables) != set(_LOCALIZATION_FIELDS)
        or not isinstance(aliases, dict)
        or not set(aliases).issubset(_LOCALIZATION_FIELDS)
    ):
        raise ReferenceDataError("Localization data must contain every supported table")
    table = tables.get(name)
    if name not in _LOCALIZATION_FIELDS or not isinstance(table, dict):
        raise ReferenceDataError(f"Unknown localization table: {name}")
    if set(table) != _LOCALIZATION_LANGUAGES:
        raise ReferenceDataError(f"Localization table must contain every supported language: {name}")
    expected_fields = _LOCALIZATION_FIELDS[name]
    validated: dict[str, Mapping[str, str]] = {}
    for language, labels in table.items():
        if not _is_localization_labels(labels, expected_fields):
            raise ReferenceDataError(f"Invalid localization fields: {name}:{language}")
        validated[language] = MappingProxyType(dict(labels))
    table_aliases = aliases.get(name, {})
    if not isinstance(table_aliases, dict):
        raise ReferenceDataError(f"Localization aliases must be an object: {name}")
    for alias, target in table_aliases.items():
        if (
            not _is_normalized_language(alias)
            or not isinstance(target, str)
            or target not in validated
            or alias in validated
        ):
            raise ReferenceDataError(f"Invalid localization alias: {name}:{alias}")
        validated[alias] = validated[target]
    return MappingProxyType(validated)
