"""Weather context and input-budget alert persistence operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.models import SourceDocument
from weather_briefing.time_utils import require_aware_datetime

from .serialization import _storage_time as storage_time

if TYPE_CHECKING:
    import sqlite3

    import pendulum


class ContextStateOperations:
    """Persist weather context snapshots and input-budget alert state."""

    _connection: sqlite3.Connection

    def save_context_documents(self, documents: tuple[SourceDocument, ...], observed_at: pendulum.DateTime) -> None:
        """Persist context documents observed during a successful run."""
        observed_at = require_aware_datetime(observed_at, context="Context observation time")
        with self._connection:
            self._insert_context_documents(documents, observed_at)

    def _insert_context_documents(
        self,
        documents: tuple[SourceDocument, ...],
        observed_at: pendulum.DateTime,
    ) -> None:
        self._connection.executemany(
            """INSERT INTO context_snapshots(
                source_id, name, url, content, language, history_summary, history_value, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    document.id,
                    document.name,
                    document.url,
                    document.content,
                    document.language,
                    document.history_summary,
                    document.history_value,
                    storage_time(observed_at),
                )
                for document in documents
            ],
        )

    def recent_context_documents(self, now: pendulum.DateTime, history_hours: int) -> tuple[SourceDocument, ...]:
        """Return context documents inside the configured history window."""
        now = require_aware_datetime(now, context="Context history time")
        threshold = storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            """SELECT source_id, name, url, content, language, history_summary, history_value FROM context_snapshots
            WHERE observed_at >= ? ORDER BY observed_at""",
            (threshold,),
        )
        return tuple(
            SourceDocument(
                id=str(row["source_id"]),
                name=str(row["name"]),
                url=str(row["url"]),
                content=str(row["content"]),
                language=str(row["language"]),
                history_summary=str(row["history_summary"]) if row["history_summary"] is not None else None,
                history_value=str(row["history_value"]) if row["history_value"] is not None else None,
            )
            for row in rows
        )

    def context_budget_sources_requiring_alert(self, fingerprints: dict[str, str]) -> tuple[str, ...]:
        """Return changed overflow sources and clear alerts for recovered sources."""
        with self._connection:
            if not fingerprints:
                self._connection.execute("DELETE FROM context_budget_alert")
                return ()
            placeholders = ",".join("?" for _ in fingerprints)
            self._connection.execute(
                f"DELETE FROM context_budget_alert WHERE source_id NOT IN ({placeholders})",  # noqa: S608
                tuple(fingerprints),
            )
            rows = self._connection.execute(
                f"SELECT source_id, content_fingerprint FROM context_budget_alert "  # noqa: S608
                f"WHERE source_id IN ({placeholders})",
                tuple(fingerprints),
            )
            alerted = {str(row["source_id"]): str(row["content_fingerprint"]) for row in rows}
        return tuple(
            source_id for source_id, fingerprint in fingerprints.items() if alerted.get(source_id) != fingerprint
        )

    def mark_context_budget_alerted(
        self,
        fingerprints: dict[str, str],
        alerted_at: pendulum.DateTime,
    ) -> None:
        """Record delivered context-budget alerts by source and content fingerprint."""
        alerted_at = require_aware_datetime(alerted_at, context="Context budget alert time")
        with self._connection:
            self._connection.executemany(
                """INSERT INTO context_budget_alert(source_id, content_fingerprint, alerted_at) VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    content_fingerprint = excluded.content_fingerprint,
                    alerted_at = excluded.alerted_at""",
                [(source_id, fingerprint, storage_time(alerted_at)) for source_id, fingerprint in fingerprints.items()],
            )
