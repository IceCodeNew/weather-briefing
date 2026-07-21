"""Validated access to packaged domain reference data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import PurePath
from types import MappingProxyType
from typing import Any, TypeGuard

from . import data
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
_CLASSIFICATION_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


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


class ReferenceDataError(RuntimeError):
    """Raised when packaged domain reference data is missing or malformed."""


@dataclass(frozen=True, slots=True)
class TelegramErrorClassification:
    """Validated Telegram API error-description and status mappings."""

    description_markers: tuple[tuple[str, str], ...]
    status_reasons: Mapping[int, str]


@cache
def load_reference_data(filename: str) -> dict[str, object]:
    """Load and validate one packaged JSON reference-data object."""
    if PurePath(filename).name != filename or not filename.endswith(".json"):
        raise ReferenceDataError("Reference data filename must identify one JSON file")
    try:
        text = resources.files(data).joinpath(filename).read_text(encoding="utf-8")
        value = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ReferenceDataError(f"Unable to load reference data: {filename}") from exc
    if not isinstance(value, dict):
        raise ReferenceDataError(f"Reference data root must be an object: {filename}")
    return value


def reference_value(filename: str, *path: str) -> Any:
    """Read a nested value from a packaged reference-data file."""
    value: Any = load_reference_data(filename)
    try:
        for key in path:
            value = value[key]
    except (KeyError, TypeError) as exc:
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Missing reference data field: {filename}:{joined_path}") from exc
    return value


def reference_string(filename: str, *path: str) -> str:
    """Read a non-empty string from packaged reference data."""
    value = reference_value(filename, *path)
    if not isinstance(value, str) or not value.strip():
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Reference data field must be a non-empty string: {filename}:{joined_path}")
    return value


def reference_string_tuple(filename: str, *path: str) -> tuple[str, ...]:
    """Read a non-empty string sequence from packaged reference data."""
    value = reference_value(filename, *path)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Reference data field must be a non-empty string list: {filename}:{joined_path}")
    return tuple(value)


@cache
def open_meteo_weather_code_descriptions() -> Mapping[int, str]:
    """Return validated Chinese descriptions for Open-Meteo WMO weather codes."""
    value = load_reference_data("open_meteo_weather_codes.json")
    descriptions = value.get("descriptions_zh_CN")
    if set(value) != {"descriptions_zh_CN"} or not isinstance(descriptions, dict) or not descriptions:
        raise ReferenceDataError("Open-Meteo weather codes must contain Chinese descriptions")

    validated: dict[int, str] = {}
    for code, description in descriptions.items():
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not code.isdigit()
            or str(int(code)) != code
            or not 0 <= int(code) <= 99
            or not isinstance(description, str)
            or not description.strip()
        ):
            raise ReferenceDataError("Open-Meteo weather codes must map numeric codes to descriptions")
        validated[int(code)] = description
    return MappingProxyType(validated)


@cache
def telegram_error_classification() -> TelegramErrorClassification:
    """Return validated Telegram API error classification data."""
    value = load_reference_data("telegram_error_classification.json")
    markers = value.get("description_markers")
    statuses = value.get("status_reasons")
    if set(value) != {"description_markers", "status_reasons"}:
        raise ReferenceDataError("Telegram error classification must contain the supported fields")
    if not isinstance(markers, dict) or not markers:
        raise ReferenceDataError("Telegram description markers must map normalized strings to reasons")
    validated_markers: list[tuple[str, str]] = []
    for marker, reason in markers.items():
        if (
            not isinstance(marker, str)
            or not marker.strip()
            or marker != marker.casefold()
            or not isinstance(reason, str)
            or _CLASSIFICATION_REASON.fullmatch(reason) is None
        ):
            raise ReferenceDataError("Telegram description markers must map normalized strings to reasons")
        validated_markers.append((marker, reason))

    if not isinstance(statuses, dict) or not statuses:
        raise ReferenceDataError("Telegram statuses must map HTTP error codes to reasons")
    validated_statuses: dict[int, str] = {}
    for status, reason in statuses.items():
        if (
            not isinstance(status, str)
            or not status.isascii()
            or not status.isdigit()
            or not 400 <= int(status) <= 599
            or not isinstance(reason, str)
            or _CLASSIFICATION_REASON.fullmatch(reason) is None
        ):
            raise ReferenceDataError("Telegram statuses must map HTTP error codes to reasons")
        validated_statuses[int(status)] = reason

    return TelegramErrorClassification(
        description_markers=tuple(validated_markers),
        status_reasons=MappingProxyType(validated_statuses),
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
