"""Health tracking operations sharing the state-store connection."""

from __future__ import annotations

import sqlite3

import pendulum

from ..time_utils import require_aware_datetime
from .serialization import _parse_time as parse_time
from .serialization import _storage_time as storage_time


class HealthStateOperations:
    """Track source freshness and consecutive task or RSS failures."""

    _connection: sqlite3.Connection

    def record_source_check(
        self,
        source_id: str,
        checked_at: pendulum.DateTime,
        latest_at: pendulum.DateTime | None,
    ) -> None:
        """Record an RSS source check and its newest observed article time."""
        self._connection.execute(
            """INSERT INTO source_health(
                source_id, first_checked_at, last_article_at, stale_alerted_at
            ) VALUES (?, ?, ?, NULL)
            ON CONFLICT(source_id) DO UPDATE SET
                stale_alerted_at = CASE
                    WHEN excluded.last_article_at IS NOT NULL
                        AND (source_health.last_article_at IS NULL
                            OR excluded.last_article_at > source_health.last_article_at)
                    THEN NULL
                    ELSE source_health.stale_alerted_at
                END,
                last_article_at = CASE
                    WHEN source_health.last_article_at IS NULL
                        OR excluded.last_article_at > source_health.last_article_at
                    THEN excluded.last_article_at
                    ELSE source_health.last_article_at
                END""",
            (
                source_id,
                storage_time(checked_at),
                storage_time(latest_at) if latest_at else None,
            ),
        )
        self._connection.commit()

    def stale_sources(
        self,
        source_ids: tuple[str, ...],
        now: pendulum.DateTime,
        stale_hours: int,
    ) -> list[str]:
        """Return sources without recent articles inside the threshold."""
        now = require_aware_datetime(now, context="Stale source check time")
        threshold = now.subtract(hours=stale_hours)
        stale: list[str] = []
        for source_id in source_ids:
            row = self._connection.execute(
                "SELECT first_checked_at, last_article_at FROM source_health WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                continue
            reference_time = row["last_article_at"] or row["first_checked_at"]
            if parse_time(reference_time) < threshold:
                stale.append(source_id)
        return stale

    def stale_sources_requiring_alert(
        self,
        source_ids: tuple[str, ...],
        now: pendulum.DateTime,
        stale_hours: int,
    ) -> list[str]:
        """Return stale sources not yet alerted for the current stale period."""
        stale = set(self.stale_sources(source_ids, now, stale_hours))
        return [
            source_id
            for source_id in source_ids
            if source_id in stale
            and self._connection.execute(
                "SELECT stale_alerted_at FROM source_health WHERE source_id = ?",
                (source_id,),
            ).fetchone()["stale_alerted_at"]
            is None
        ]

    def mark_stale_sources_alerted(
        self,
        source_ids: tuple[str, ...],
        alerted_at: pendulum.DateTime,
    ) -> None:
        """Record successful stale-source alert delivery."""
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        self._connection.execute(
            f"UPDATE source_health SET stale_alerted_at = ? "  # noqa: S608
            f"WHERE source_id IN ({placeholders})",
            (storage_time(alerted_at), *source_ids),
        )
        self._connection.commit()

    def record_failure(self) -> int:
        """Increment and return the consecutive task failure count."""
        self._connection.execute(
            "UPDATE task_health SET consecutive_failures = consecutive_failures + 1 WHERE singleton = 1"
        )
        self._connection.commit()
        row = self._connection.execute("SELECT consecutive_failures FROM task_health WHERE singleton = 1").fetchone()
        return int(row["consecutive_failures"])

    def task_failure_requires_alert(self) -> bool:
        """Return whether the current task failure period lacks an alert."""
        row = self._connection.execute("SELECT 1 FROM task_failure_alert WHERE singleton = 1").fetchone()
        return row is None

    def mark_task_failure_alerted(self, alerted_at: pendulum.DateTime) -> None:
        """Record successful task-failure alert delivery."""
        self._connection.execute(
            "INSERT OR REPLACE INTO task_failure_alert(singleton, alerted_at) VALUES (1, ?)",
            (storage_time(alerted_at),),
        )
        self._connection.commit()

    def record_rss_fetch_failure(self, source_id: str) -> int:
        """Increment and return one RSS source's consecutive failure count."""
        self._connection.execute(
            """INSERT INTO rss_failure_tracker(source_id, consecutive_failures, failure_alerted_at)
            VALUES (?, 1, NULL)
            ON CONFLICT(source_id) DO UPDATE SET
                consecutive_failures = consecutive_failures + 1""",
            (source_id,),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT consecutive_failures FROM rss_failure_tracker WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return int(row["consecutive_failures"])

    def record_rss_fetch_success(self, source_id: str) -> None:
        """Reset one RSS source's failure period."""
        self._connection.execute(
            "DELETE FROM rss_failure_tracker WHERE source_id = ?",
            (source_id,),
        )
        self._connection.commit()

    def rss_sources_requiring_failure_alert(
        self,
        source_ids: tuple[str, ...],
        threshold: int,
    ) -> list[str]:
        """Return RSS sources whose unalerted failures reached the threshold."""
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        rows = self._connection.execute(
            f"""SELECT source_id
            FROM rss_failure_tracker
            WHERE source_id IN ({placeholders})
            AND consecutive_failures >= ?
            AND failure_alerted_at IS NULL
            ORDER BY source_id""",  # noqa: S608
            (*source_ids, threshold),
        )
        return [str(row["source_id"]) for row in rows]

    def mark_rss_failure_alerted(
        self,
        source_ids: tuple[str, ...],
        alerted_at: pendulum.DateTime,
    ) -> None:
        """Record successful RSS failure alert delivery for sources."""
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        self._connection.execute(
            f"UPDATE rss_failure_tracker SET failure_alerted_at = ? "  # noqa: S608
            f"WHERE source_id IN ({placeholders})",
            (storage_time(alerted_at), *source_ids),
        )
        self._connection.commit()
