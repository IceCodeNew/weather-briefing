"""Compatibility exports for reference data awaiting feature migration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType

from .data.resources import (
    ReferenceDataError,
    load_reference_data,
    reference_string,
    reference_string_tuple,
    reference_value,
)
from .localization import localization_table

_CLASSIFICATION_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class TelegramErrorClassification:
    """Validated Telegram API error mappings and delivery metadata."""

    description_markers: tuple[tuple[str, str], ...]
    parameter_reasons: Mapping[str, str]
    status_reasons: Mapping[int, str]
    channel_unavailable_reasons: frozenset[str]


@cache
def open_meteo_weather_code_descriptions() -> Mapping[int, str]:
    """Return validated English descriptions for Open-Meteo WMO weather codes."""
    value = load_reference_data("open_meteo_weather_codes.json")
    descriptions = value.get("descriptions_en")
    if set(value) != {"descriptions_en"} or not isinstance(descriptions, dict) or not descriptions:
        raise ReferenceDataError("Open-Meteo weather codes must contain English descriptions")

    validated: dict[int, str] = {}
    for code, description in descriptions.items():
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not code.isdigit()
            or len(code) > 2
            or not isinstance(description, str)
            or not description.strip()
        ):
            raise ReferenceDataError("Open-Meteo weather codes must map numeric codes to descriptions")
        numeric_code = int(code)
        if str(numeric_code) != code:
            raise ReferenceDataError("Open-Meteo weather codes must map numeric codes to descriptions")
        validated[numeric_code] = description
    return MappingProxyType(validated)


@cache
def telegram_error_classification() -> TelegramErrorClassification:
    """Return validated Telegram API error classification data."""
    value = load_reference_data("telegram_error_classification.json")
    markers = value.get("description_markers")
    parameters = value.get("parameter_reasons")
    statuses = value.get("status_reasons")
    unavailable_reasons = value.get("channel_unavailable_reasons")
    if set(value) != {
        "channel_unavailable_reasons",
        "description_markers",
        "parameter_reasons",
        "status_reasons",
    }:
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

    if not isinstance(parameters, dict) or set(parameters) != {"migrate_to_chat_id"}:
        raise ReferenceDataError("Telegram parameters must map supported API fields to reasons")
    migration_reason = parameters.get("migrate_to_chat_id")
    if not isinstance(migration_reason, str) or _CLASSIFICATION_REASON.fullmatch(migration_reason) is None:
        raise ReferenceDataError("Telegram parameters must map supported API fields to reasons")

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

    known_reasons = {
        *(reason for _, reason in validated_markers),
        migration_reason,
        *validated_statuses.values(),
    }
    if not isinstance(unavailable_reasons, list) or not unavailable_reasons:
        raise ReferenceDataError("Telegram channel availability must reference known reasons")
    validated_unavailable_reasons: list[str] = []
    for reason in unavailable_reasons:
        if not isinstance(reason, str) or _CLASSIFICATION_REASON.fullmatch(reason) is None:
            raise ReferenceDataError("Telegram channel availability must reference known reasons")
        validated_unavailable_reasons.append(reason)
    if len(set(validated_unavailable_reasons)) != len(validated_unavailable_reasons) or not set(
        validated_unavailable_reasons
    ).issubset(known_reasons):
        raise ReferenceDataError("Telegram channel availability must reference known reasons")

    return TelegramErrorClassification(
        description_markers=tuple(validated_markers),
        parameter_reasons=MappingProxyType({"migrate_to_chat_id": migration_reason}),
        status_reasons=MappingProxyType(validated_statuses),
        channel_unavailable_reasons=frozenset(validated_unavailable_reasons),
    )


__all__ = [
    "ReferenceDataError",
    "TelegramErrorClassification",
    "load_reference_data",
    "localization_table",
    "open_meteo_weather_code_descriptions",
    "reference_string",
    "reference_string_tuple",
    "reference_value",
    "telegram_error_classification",
]
