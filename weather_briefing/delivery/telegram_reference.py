"""Validated Telegram delivery classification metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType

from ..data import resources
from ..data.resources import ReferenceDataError

_CLASSIFICATION_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class TelegramErrorClassification:
    """Validated Telegram API error mappings and delivery metadata."""

    description_markers: tuple[tuple[str, str], ...]
    parameter_reasons: Mapping[str, str]
    status_reasons: Mapping[int, str]
    channel_unavailable_reasons: frozenset[str]


@cache
def telegram_error_classification() -> TelegramErrorClassification:
    """Return validated Telegram API error classification data."""
    value = resources.load_reference_data("telegram_error_classification.json")
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
