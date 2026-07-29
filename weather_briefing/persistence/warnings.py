"""Weather-warning persistence operations."""

from __future__ import annotations

import json
import sqlite3

import pendulum

from ..models import Warning
from .serialization import _parse_time as parse_time
from .serialization import _storage_time as storage_time


class WarningStateOperations:
    """Persist active weather warnings and explicit resolutions."""

    _connection: sqlite3.Connection

    def active_warnings(self, now: pendulum.DateTime, retention_hours: int) -> tuple[Warning, ...]:
        """Return warnings confirmed inside the retention window."""
        threshold = storage_time(now.subtract(hours=retention_hours))
        rows = self._connection.execute(
            "SELECT payload, last_confirmed_at FROM warnings WHERE last_confirmed_at >= ?",
            (threshold,),
        )
        warnings: list[Warning] = []
        for row in rows:
            payload = json.loads(row["payload"])
            warnings.append(
                Warning(
                    id=payload["id"],
                    title=payload["title"],
                    status=payload["status"],
                    detail=payload["detail"],
                    source_ids=tuple(payload["source_ids"]),
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
