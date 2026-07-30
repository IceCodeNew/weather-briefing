"""Durable state for official service-status message handling."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import pendulum

from ..models import ServiceSurface
from .serialization import _storage_time as storage_time


@dataclass(frozen=True, slots=True)
class ServiceStatusMessageState:
    """Track the last observed and successfully handled official message."""

    observed_revision_id: str
    decided_revision_id: str | None
    should_notify: bool | None
    handled_revision_id: str | None
    handled_title: str | None
    handled_status: str | None
    handled_body: str | None
    handled_surfaces: tuple[ServiceSurface, ...] | None


def _stored_surfaces(value: object) -> tuple[ServiceSurface, ...] | None:
    """Decode one optional list of application-owned service surfaces."""
    if value is None:
        return None
    decoded: object = json.loads(str(value))
    if not isinstance(decoded, list):
        raise ValueError("Stored service-status surfaces must be a list")
    surfaces: list[ServiceSurface] = []
    for surface in decoded:
        if not isinstance(surface, str):
            raise ValueError("Stored service-status surfaces must contain strings")
        try:
            surfaces.append(ServiceSurface(surface))
        except ValueError as exc:
            raise ValueError(f"Stored service-status surface is unsupported: {surface}") from exc
    return tuple(surfaces)


class SQLiteServiceStatusStore:
    """Persist service-status decisions and per-publisher delivery progress."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Share the owning state store's initialized connection."""
        self._connection = connection

    def service_status_message_state(
        self,
        source_id: str,
        incident_id: str,
    ) -> ServiceStatusMessageState | None:
        """Return durable handling state for one official incident."""
        row = self._connection.execute(
            """SELECT observed_revision_id, decided_revision_id, should_notify,
            handled_revision_id, handled_title, handled_status, handled_body, handled_surfaces
            FROM service_status_message_state WHERE source_id = ? AND incident_id = ?""",
            (source_id, incident_id),
        ).fetchone()
        if row is None:
            return None
        return ServiceStatusMessageState(
            observed_revision_id=str(row["observed_revision_id"]),
            decided_revision_id=(str(row["decided_revision_id"]) if row["decided_revision_id"] is not None else None),
            should_notify=bool(row["should_notify"]) if row["should_notify"] is not None else None,
            handled_revision_id=(str(row["handled_revision_id"]) if row["handled_revision_id"] is not None else None),
            handled_title=str(row["handled_title"]) if row["handled_title"] is not None else None,
            handled_status=str(row["handled_status"]) if row["handled_status"] is not None else None,
            handled_body=str(row["handled_body"]) if row["handled_body"] is not None else None,
            handled_surfaces=_stored_surfaces(row["handled_surfaces"]),
        )

    def observe_service_status_message(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        title: str,
        status: str,
        body: str,
        observed_at: pendulum.DateTime,
    ) -> None:
        """Persist an official message without claiming handling succeeded."""
        self._connection.execute(
            """INSERT INTO service_status_message_state(
                source_id, incident_id, observed_revision_id, observed_title,
                observed_status, observed_body, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, incident_id) DO UPDATE SET
                observed_revision_id = excluded.observed_revision_id,
                observed_title = excluded.observed_title,
                observed_status = excluded.observed_status,
                observed_body = excluded.observed_body,
                observed_at = excluded.observed_at""",
            (source_id, incident_id, revision_id, title, status, body, storage_time(observed_at)),
        )
        self._connection.commit()

    def mark_service_status_message_decided(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        should_notify: bool,
    ) -> None:
        """Persist notification value so partial delivery retries remain deterministic."""
        cursor = self._connection.execute(
            """UPDATE service_status_message_state SET
                decided_revision_id = ?,
                should_notify = ?
            WHERE source_id = ? AND incident_id = ? AND observed_revision_id = ?""",
            (revision_id, int(should_notify), source_id, incident_id, revision_id),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise RuntimeError("Service-status message changed before its decision was recorded")
        self._connection.commit()

    def service_status_delivered_publishers(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
    ) -> frozenset[str]:
        """Return publishers that already accepted this exact message revision."""
        rows = self._connection.execute(
            """SELECT publisher_id FROM service_status_message_delivery
            WHERE source_id = ? AND incident_id = ? AND revision_id = ?""",
            (source_id, incident_id, revision_id),
        )
        return frozenset(str(row["publisher_id"]) for row in rows)

    def mark_service_status_message_delivered(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        publisher_id: str,
        delivered_at: pendulum.DateTime,
    ) -> None:
        """Record successful delivery to one configured publisher."""
        self._connection.execute(
            """INSERT OR IGNORE INTO service_status_message_delivery(
                source_id, incident_id, revision_id, publisher_id, delivered_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (source_id, incident_id, revision_id, publisher_id, storage_time(delivered_at)),
        )
        self._connection.commit()

    def mark_service_status_message_handled(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        title: str,
        status: str,
        body: str,
        surfaces: tuple[ServiceSurface, ...],
        handled_at: pendulum.DateTime,
    ) -> None:
        """Mark one observed message as delivered or intentionally skipped."""
        cursor = self._connection.execute(
            """UPDATE service_status_message_state SET
                handled_revision_id = ?,
                handled_title = ?,
                handled_status = ?,
                handled_body = ?,
                handled_surfaces = ?,
                handled_at = ?
            WHERE source_id = ? AND incident_id = ? AND observed_revision_id = ?""",
            (
                revision_id,
                title,
                status,
                body,
                json.dumps([surface.value for surface in surfaces], ensure_ascii=False, separators=(",", ":")),
                storage_time(handled_at),
                source_id,
                incident_id,
                revision_id,
            ),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise RuntimeError("Service-status message changed before handling was recorded")
        self._connection.commit()
