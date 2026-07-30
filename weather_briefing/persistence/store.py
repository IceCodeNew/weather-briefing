"""Transactional SQLite state-store composition."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pendulum

from ..models import Article, SourceDocument, Warning
from ..time_utils import require_aware_datetime
from .content import ContentStateOperations
from .context import ContextStateOperations
from .health import HealthStateOperations
from .schema import initialize_state
from .serialization import _storage_time as storage_time
from .service_status import SQLiteServiceStatusStore
from .warnings import WarningStateOperations


class SQLiteStateStore(
    ContentStateOperations,
    ContextStateOperations,
    WarningStateOperations,
    HealthStateOperations,
):
    """Compose domain operations around one transactional SQLite connection."""

    def __init__(self, path: Path) -> None:
        """Open the state database and initialize its application schema."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        initialize_state(self._connection)
        # Service-status methods own transactions and must not run inside commit_result().
        self.service_status = SQLiteServiceStatusStore(self._connection)

    def close(self) -> None:
        """Close the state database connection."""
        self._connection.close()

    def __enter__(self) -> SQLiteStateStore:
        """Return this state store for context-managed use."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection without suppressing context exceptions."""
        self.close()

    def commit_result(
        self,
        *,
        kind: str,
        body: str | None,
        articles: tuple[Article, ...],
        context_documents: tuple[SourceDocument, ...],
        active_warnings: tuple[Warning, ...],
        resolved_warning_ids: tuple[str, ...],
        recorded_at: pendulum.DateTime,
        verbatim_silent: bool,
    ) -> None:
        """Atomically persist one summarized result and its delivery queue."""
        recorded_at = require_aware_datetime(recorded_at, context="Result recording time")
        confirmed_source_ids = {article.id for article in articles} | {document.id for document in context_documents}
        with self._connection:
            if body is None:
                self._insert_pending_articles(articles, recorded_at)
            else:
                self._insert_articles(articles, recorded_at)
                self._delete_pending_articles(articles)
            self._insert_context_documents(context_documents, recorded_at)
            if body is not None:
                self._insert_briefing(kind, body, recorded_at)
                self._enqueue_verbatim_deliveries(articles, verbatim_silent, recorded_at)
            self._update_warnings(
                active_warnings,
                resolved_warning_ids,
                recorded_at,
                confirmed_source_ids,
            )

    def record_success(
        self,
        now: pendulum.DateTime,
        *,
        history_hours: int,
        warning_retention_hours: int,
    ) -> None:
        """Record task success and prune expired history in one transaction."""
        now = require_aware_datetime(now, context="State pruning time")
        history_threshold = storage_time(now.subtract(hours=history_hours))
        warning_threshold = storage_time(now.subtract(hours=warning_retention_hours))
        with self._connection:
            self._connection.execute(
                """DELETE FROM articles
                WHERE processed_at < ?
                    AND id NOT IN (SELECT article_id FROM verbatim_delivery_queue)""",
                (history_threshold,),
            )
            self._connection.execute("DELETE FROM briefings WHERE published_at < ?", (history_threshold,))
            self._connection.execute("DELETE FROM context_snapshots WHERE observed_at < ?", (history_threshold,))
            self._connection.execute("DELETE FROM warnings WHERE last_confirmed_at < ?", (warning_threshold,))
            self._connection.execute("UPDATE task_health SET consecutive_failures = 0 WHERE singleton = 1")
            self._connection.execute("DELETE FROM task_failure_alert WHERE singleton = 1")
