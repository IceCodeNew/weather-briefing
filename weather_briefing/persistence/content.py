"""Article, briefing, and verbatim-delivery persistence operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeGuard

from weather_briefing.models import Article, BriefingRecord
from weather_briefing.time_utils import require_aware_datetime

from .serialization import _article_from_row as article_from_row
from .serialization import _parse_time as parse_time
from .serialization import _storage_time as storage_time

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    import pendulum


@dataclass(frozen=True, slots=True)
class VerbatimDelivery:
    """A durable verbatim delivery awaiting platform acceptance."""

    article: Article
    silent: bool


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _stored_notification_payload(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = "Stored notification payload must be JSON text"
        raise ValueError(msg)  # noqa: TRY004
    decoded: object = json.loads(value)
    if not _is_string_object_dict(decoded):
        msg = "Stored notification payload must be an object with string keys"
        raise ValueError(msg)
    return decoded


class ContentStateOperations:
    """Persist articles, briefing history, and verbatim delivery state."""

    _connection: sqlite3.Connection

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
        processed_at = require_aware_datetime(processed_at, context="Article processing time")
        with self._connection:
            self._insert_articles(articles, processed_at)

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
        first_seen_at = require_aware_datetime(first_seen_at, context="Pending article observation time")
        with self._connection:
            self._insert_pending_articles(articles, first_seen_at)

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
        processed_at = require_aware_datetime(processed_at, context="Article processing time")
        with self._connection:
            self._insert_articles(articles, processed_at)
            self._delete_pending_articles(articles)

    def _delete_pending_articles(self, articles: tuple[Article, ...]) -> None:
        if not articles:
            return
        placeholders = ",".join("?" for _ in articles)
        self._connection.execute(
            f"DELETE FROM pending_articles WHERE id IN ({placeholders})",  # noqa: S608
            tuple(article.id for article in articles),
        )

    def recent_articles(self, now: pendulum.DateTime, history_hours: int) -> tuple[Article, ...]:
        """Return processed articles inside the configured history window."""
        now = require_aware_datetime(now, context="Article history time")
        threshold = storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT * FROM articles WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(article_from_row(row) for row in rows)

    def recent_briefings(self, now: pendulum.DateTime, history_hours: int) -> tuple[BriefingRecord, ...]:
        """Return briefings inside the configured history window."""
        now = require_aware_datetime(now, context="Briefing history time")
        threshold = storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            """SELECT kind, body, published_at, notification_payload
            FROM briefings WHERE published_at >= ? ORDER BY published_at""",
            (threshold,),
        )
        return tuple(
            BriefingRecord(
                kind=str(row["kind"]),
                body=str(row["body"]),
                published_at=parse_time(str(row["published_at"])),
                notification_payload=_stored_notification_payload(row["notification_payload"]),
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

    def save_briefing(
        self,
        kind: str,
        body: str,
        published_at: pendulum.DateTime,
        *,
        notification_payload: Mapping[str, object] | None = None,
    ) -> None:
        """Persist a successfully published briefing."""
        self._insert_briefing(kind, body, published_at, notification_payload)
        self._connection.commit()

    def _insert_briefing(
        self,
        kind: str,
        body: str,
        published_at: pendulum.DateTime,
        notification_payload: Mapping[str, object] | None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO briefings(kind, body, published_at, notification_payload)
            VALUES (?, ?, ?, ?)""",
            (
                kind,
                body,
                storage_time(published_at),
                (
                    json.dumps(dict(notification_payload), ensure_ascii=False, separators=(",", ":"))
                    if notification_payload is not None
                    else None
                ),
            ),
        )

    def _enqueue_verbatim_deliveries(
        self,
        articles: tuple[Article, ...],
        *,
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
