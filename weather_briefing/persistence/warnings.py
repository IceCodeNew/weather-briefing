"""Weather-warning persistence operations."""

from __future__ import annotations

import json
import sqlite3
from typing import TypeGuard

import pendulum

from ..models import Warning
from ..time_utils import require_aware_datetime
from .serialization import _parse_time as parse_time
from .serialization import _storage_time as storage_time


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _required_text_field(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"Stored warning payload {field} must be a string")
    return value


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _stored_warning_payload(value: object) -> tuple[str, str, str, str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise ValueError("Stored warning payload must be JSON text")
    decoded: object = json.loads(value)
    if not _is_string_object_dict(decoded):
        raise ValueError("Stored warning payload must be an object with string keys")
    source_ids = decoded.get("source_ids")
    if not _is_string_list(source_ids):
        raise ValueError("Stored warning payload source_ids must be a list of strings")
    return (
        _required_text_field(decoded, "id"),
        _required_text_field(decoded, "title"),
        _required_text_field(decoded, "status"),
        _required_text_field(decoded, "detail"),
        tuple(source_ids),
    )


class WarningStateOperations:
    """Persist active weather warnings and explicit resolutions."""

    _connection: sqlite3.Connection

    def active_warnings(self, now: pendulum.DateTime, retention_hours: int) -> tuple[Warning, ...]:
        """Return warnings confirmed inside the retention window."""
        now = require_aware_datetime(now, context="Warning retention time")
        threshold = storage_time(now.subtract(hours=retention_hours))
        rows = self._connection.execute(
            "SELECT payload, last_confirmed_at FROM warnings WHERE last_confirmed_at >= ?",
            (threshold,),
        )
        warnings: list[Warning] = []
        for row in rows:
            warning_id, title, status, detail, source_ids = _stored_warning_payload(row["payload"])
            warnings.append(
                Warning(
                    id=warning_id,
                    title=title,
                    status=status,
                    detail=detail,
                    source_ids=source_ids,
                    last_confirmed_at=parse_time(row["last_confirmed_at"]),
                )
            )
        return tuple(warnings)

    def update_warnings(
        self,
        warnings: tuple[Warning, ...],
        resolved_warning_ids: tuple[str, ...],
        now: pendulum.DateTime,
        confirmed_source_ids: set[str] | None = None,
    ) -> None:
        """Apply active and resolved warning updates atomically."""
        now = require_aware_datetime(now, context="Warning update time")
        with self._connection:
            self._update_warnings(warnings, resolved_warning_ids, now, confirmed_source_ids)

    def _update_warnings(
        self,
        warnings: tuple[Warning, ...],
        resolved_warning_ids: tuple[str, ...],
        now: pendulum.DateTime,
        confirmed_source_ids: set[str] | None = None,
    ) -> None:
        confirmed_source_ids = confirmed_source_ids or set()
        if resolved_warning_ids:
            placeholders = ",".join("?" for _ in resolved_warning_ids)
            self._connection.execute(
                f"DELETE FROM warnings WHERE id IN ({placeholders})",  # noqa: S608
                resolved_warning_ids,
            )
        for warning in warnings:
            existing = self._connection.execute(
                "SELECT last_confirmed_at FROM warnings WHERE id = ?", (warning.id,)
            ).fetchone()
            has_new_evidence = bool(set(warning.source_ids) & confirmed_source_ids)
            confirmed_at = now if existing is None or has_new_evidence else parse_time(existing["last_confirmed_at"])
            payload = json.dumps(
                {
                    "id": warning.id,
                    "title": warning.title,
                    "status": warning.status,
                    "detail": warning.detail,
                    "source_ids": warning.source_ids,
                },
                ensure_ascii=False,
            )
            self._connection.execute(
                """INSERT INTO warnings(id, payload, last_confirmed_at) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload,
                last_confirmed_at = excluded.last_confirmed_at""",
                (warning.id, payload, storage_time(confirmed_at)),
            )
