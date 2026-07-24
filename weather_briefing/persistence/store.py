"""Transactional SQLite state store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pendulum

from ..models import Article, BriefingRecord, SourceDocument, Warning
from ..time_utils import require_aware_datetime
from .health import HealthStateOperations
from .schema import initialize_state
from .serialization import (
    _article_from_row as article_from_row,
)
from .serialization import _parse_time as parse_time
from .serialization import _storage_time as storage_time


@dataclass(frozen=True, slots=True)
class VerbatimDelivery:
    """A durable verbatim delivery awaiting platform acceptance."""

    article: Article
    silent: bool


@dataclass(frozen=True, slots=True)
class ServiceStatusState:
    """Track the last observed and successfully notified provider states."""

    observed_fingerprint: str
    observed_unhealthy: bool
    notified_fingerprint: str | None
    notified_unhealthy: bool | None


class SQLiteStateStore(HealthStateOperations):
    """Persist briefing history, warnings, articles, and health state."""

    def __init__(self, path: Path) -> None:
        """Open the state database and initialize its application schema."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        """Close the state database connection."""
        self._connection.close()

    def __enter__(self) -> SQLiteStateStore:
        """Return this state store for context-managed use."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection without suppressing context exceptions."""
        self.close()

    def _initialize(self) -> None:
        initialize_state(self._connection)

    def known_article_ids(self, ids: tuple[str, ...]) -> set[str]:
        """Return the subset of article IDs already processed."""
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"SELECT id FROM articles WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
        return {str(row["id"]) for row in rows}

    def save_articles(self, articles: tuple[Article, ...], processed_at: pendulum.DateTime) -> None:
        """Persist processed articles at an aware timestamp."""
        self._insert_articles(articles, processed_at)
        self._connection.commit()

    def _insert_articles(self, articles: tuple[Article, ...], processed_at: pendulum.DateTime) -> None:
        self._connection.executemany(
            """INSERT OR IGNORE INTO articles
            (id, source_id, source_name, title, url, published_at, content, is_verbatim, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    article.id,
                    article.source_id,
                    article.source_name,
                    article.title,
                    article.url,
                    storage_time(article.published_at),
                    article.content,
                    article.is_verbatim,
                    storage_time(processed_at),
                )
                for article in articles
            ],
        )

    def save_pending_articles(self, articles: tuple[Article, ...], first_seen_at: pendulum.DateTime) -> None:
        """Persist articles awaiting successful briefing delivery."""
        self._insert_pending_articles(articles, first_seen_at)
        self._connection.commit()

    def _insert_pending_articles(self, articles: tuple[Article, ...], first_seen_at: pendulum.DateTime) -> None:
        self._connection.executemany(
            """INSERT OR IGNORE INTO pending_articles
            (id, source_id, source_name, title, url, published_at, content, is_verbatim, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    article.id,
                    article.source_id,
                    article.source_name,
                    article.title,
                    article.url,
                    storage_time(article.published_at),
                    article.content,
                    article.is_verbatim,
                    storage_time(first_seen_at),
                )
                for article in articles
            ],
        )

    def pending_articles(self) -> tuple[Article, ...]:
        """Return pending articles in stable processing order."""
        rows = self._connection.execute("SELECT * FROM pending_articles ORDER BY first_seen_at, published_at")
        return tuple(article_from_row(row) for row in rows)

    def mark_articles_processed(
        self,
        articles: tuple[Article, ...],
        processed_at: pendulum.DateTime,
    ) -> None:
        """Move delivered articles from pending to processed state."""
        self._insert_articles(articles, processed_at)
        self._delete_pending_articles(articles)
        self._connection.commit()

    def _delete_pending_articles(self, articles: tuple[Article, ...]) -> None:
        if not articles:
            return
        placeholders = ",".join("?" for _ in articles)
        self._connection.execute(
            f"DELETE FROM pending_articles WHERE id IN ({placeholders})",  # noqa: S608
            tuple(article.id for article in articles),
        )

    def recent_briefings(self, now: pendulum.DateTime, history_hours: int) -> tuple[BriefingRecord, ...]:
        """Return briefings inside the configured history window."""
        threshold = storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT kind, body, published_at FROM briefings WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(
            BriefingRecord(
                kind=str(row["kind"]),
                body=str(row["body"]),
                published_at=parse_time(str(row["published_at"])),
            )
            for row in rows
        )

    def has_briefing_between(
        self,
        kind: str,
        start: pendulum.DateTime,
        end: pendulum.DateTime,
    ) -> bool:
        """Return whether a briefing kind was published in a time interval."""
        row = self._connection.execute(
            "SELECT 1 FROM briefings WHERE kind = ? AND published_at >= ? AND published_at <= ? LIMIT 1",
            (kind, storage_time(start), storage_time(end)),
        ).fetchone()
        return row is not None

    def recent_articles(self, now: pendulum.DateTime, history_hours: int) -> tuple[Article, ...]:
        """Return processed articles inside the configured history window."""
        threshold = storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT * FROM articles WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(article_from_row(row) for row in rows)

    def save_briefing(self, kind: str, body: str, published_at: pendulum.DateTime) -> None:
        """Persist a successfully published briefing."""
        self._insert_briefing(kind, body, published_at)
        self._connection.commit()

    def _insert_briefing(self, kind: str, body: str, published_at: pendulum.DateTime) -> None:
        self._connection.execute(
            "INSERT INTO briefings(kind, body, published_at) VALUES (?, ?, ?)",
            (kind, body, storage_time(published_at)),
        )

    def save_context_documents(self, documents: tuple[SourceDocument, ...], observed_at: pendulum.DateTime) -> None:
        """Persist context documents observed during a successful run."""
        self._insert_context_documents(documents, observed_at)
        self._connection.commit()

    def service_status_state(self, source_id: str) -> ServiceStatusState | None:
        """Return durable delivery state for one official status source."""
        row = self._connection.execute(
            """SELECT observed_fingerprint, observed_unhealthy, notified_fingerprint, notified_unhealthy
            FROM service_status_state WHERE source_id = ?""",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        notified_unhealthy = row["notified_unhealthy"]
        return ServiceStatusState(
            observed_fingerprint=str(row["observed_fingerprint"]),
            observed_unhealthy=bool(row["observed_unhealthy"]),
            notified_fingerprint=(
                str(row["notified_fingerprint"]) if row["notified_fingerprint"] is not None else None
            ),
            notified_unhealthy=bool(notified_unhealthy) if notified_unhealthy is not None else None,
        )

    def observe_service_status(
        self,
        source_id: str,
        fingerprint: str,
        unhealthy: bool,
        observed_at: pendulum.DateTime,
    ) -> None:
        """Persist a provider observation without claiming delivery succeeded."""
        self._connection.execute(
            """INSERT INTO service_status_state(
                source_id, observed_fingerprint, observed_unhealthy, observed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                observed_fingerprint = excluded.observed_fingerprint,
                observed_unhealthy = excluded.observed_unhealthy,
                observed_at = excluded.observed_at""",
            (source_id, fingerprint, int(unhealthy), storage_time(observed_at)),
        )
        self._connection.commit()

    def mark_service_status_notified(
        self,
        source_id: str,
        fingerprint: str,
        unhealthy: bool,
        notified_at: pendulum.DateTime,
    ) -> None:
        """Mark one observed state as delivered or intentionally silent."""
        cursor = self._connection.execute(
            """UPDATE service_status_state SET
                notified_fingerprint = ?,
                notified_unhealthy = ?,
                notified_at = ?
            WHERE source_id = ? AND observed_fingerprint = ?""",
            (fingerprint, int(unhealthy), storage_time(notified_at), source_id, fingerprint),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise RuntimeError("Service-status observation changed before notification was recorded")
        self._connection.commit()

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
        if not fingerprints:
            self._connection.execute("DELETE FROM context_budget_alert")
            self._connection.commit()
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
        self._connection.commit()
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
        self._connection.executemany(
            """INSERT INTO context_budget_alert(source_id, content_fingerprint, alerted_at) VALUES (?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                content_fingerprint = excluded.content_fingerprint,
                alerted_at = excluded.alerted_at""",
            [(source_id, fingerprint, storage_time(alerted_at)) for source_id, fingerprint in fingerprints.items()],
        )
        self._connection.commit()

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
        self._update_warnings(warnings, resolved_warning_ids, now, confirmed_source_ids)
        self._connection.commit()

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

    def _enqueue_verbatim_deliveries(
        self,
        articles: tuple[Article, ...],
        silent: bool,
        queued_at: pendulum.DateTime,
    ) -> None:
        self._connection.executemany(
            """INSERT OR IGNORE INTO verbatim_delivery_queue(article_id, silent, queued_at)
            VALUES (?, ?, ?)""",
            [(article.id, silent, storage_time(queued_at)) for article in articles if article.is_verbatim],
        )

    def pending_verbatim_deliveries(self) -> tuple[VerbatimDelivery, ...]:
        """Return queued verbatim deliveries in stable insertion order."""
        rows = self._connection.execute(
            """SELECT articles.*, verbatim_delivery_queue.silent
            FROM verbatim_delivery_queue
            JOIN articles ON articles.id = verbatim_delivery_queue.article_id
            ORDER BY verbatim_delivery_queue.sequence"""
        )
        return tuple(VerbatimDelivery(article=article_from_row(row), silent=bool(row["silent"])) for row in rows)

    def acknowledge_verbatim_delivery(self, article_id: str) -> None:
        """Remove one verbatim item after successful platform delivery."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM verbatim_delivery_queue WHERE article_id = ?",
                (article_id,),
            )

    def record_success(
        self,
        now: pendulum.DateTime,
        *,
        history_hours: int,
        warning_retention_hours: int,
    ) -> None:
        """Record task success and prune expired history in one transaction."""
        history_threshold = storage_time(now.subtract(hours=history_hours))
        warning_threshold = storage_time(now.subtract(hours=warning_retention_hours))
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
        self._connection.commit()
