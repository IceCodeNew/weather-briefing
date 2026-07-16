"""SQLite-backed briefing state and runtime diagnostics."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import pendulum

from .models import Article, BriefingRecord, SourceDocument, Warning
from .time_utils import require_aware_datetime

_STORAGE_TIME_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
_RENDERED_TEXT_DIAGNOSTIC = "rendered_text"
_RUNTIME_DIAGNOSTIC_BUSY_TIMEOUT_SECONDS = 0.1
_LOGGER = logging.getLogger("weather_briefing.state")


class SQLiteRuntimeDiagnostics:
    """Persist the expiring switch for sensitive rendered-text diagnostics."""

    def __init__(self, path: Path) -> None:
        """Open the diagnostics database and ensure its runtime-switch schema."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=_RUNTIME_DIAGNOSTIC_BUSY_TIMEOUT_SECONDS)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_diagnostics (name TEXT PRIMARY KEY, expires_at TEXT NOT NULL)"
        )
        self._connection.commit()

    def close(self) -> None:
        """Close the diagnostics database connection."""
        self._connection.close()

    def __enter__(self) -> SQLiteRuntimeDiagnostics:
        """Return this diagnostics store for context-managed use."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection without suppressing context exceptions."""
        self.close()

    def enable_rendered_text_logging(self, expires_at: pendulum.DateTime) -> None:
        """Enable sensitive rendered-text logging until an aware timestamp."""
        expires_at = require_aware_datetime(expires_at, context="Rendered text diagnostic expiration")
        self._connection.execute(
            """INSERT INTO runtime_diagnostics(name, expires_at) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET expires_at = excluded.expires_at""",
            (_RENDERED_TEXT_DIAGNOSTIC, _storage_time(expires_at)),
        )
        self._connection.commit()
        _LOGGER.warning(
            "Sensitive rendered text diagnostic logging enabled until %s",
            expires_at.to_iso8601_string(),
        )

    def disable_rendered_text_logging(self) -> None:
        """Disable sensitive rendered-text logging immediately."""
        self._connection.execute(
            "DELETE FROM runtime_diagnostics WHERE name = ?",
            (_RENDERED_TEXT_DIAGNOSTIC,),
        )
        self._connection.commit()
        _LOGGER.warning("Sensitive rendered text diagnostic logging disabled")

    def rendered_text_logging_until(
        self,
        now: pendulum.DateTime | None = None,
    ) -> pendulum.DateTime | None:
        """Return the active expiration time and remove expired state."""
        current_time = require_aware_datetime(
            now or pendulum.now("UTC"),
            context="Rendered text diagnostic check time",
        )
        row = self._connection.execute(
            "SELECT expires_at FROM runtime_diagnostics WHERE name = ?",
            (_RENDERED_TEXT_DIAGNOSTIC,),
        ).fetchone()
        if row is None:
            return None
        expires_at = _parse_time(str(row["expires_at"]))
        if expires_at > current_time:
            return expires_at
        self._connection.execute(
            "DELETE FROM runtime_diagnostics WHERE name = ?",
            (_RENDERED_TEXT_DIAGNOSTIC,),
        )
        self._connection.commit()
        _LOGGER.warning(
            "Sensitive rendered text diagnostic logging expired at %s",
            expires_at.to_iso8601_string(),
        )
        return None

    def rendered_text_logging_enabled(self) -> bool:
        """Return whether rendered-text diagnostics are currently enabled."""
        return self.rendered_text_logging_until() is not None


class SQLiteStateStore:
    """Persist briefing history, warnings, articles, and health state."""

    def __init__(self, path: Path) -> None:
        """Open the state database and initialize its application schema."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
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
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL,
                content TEXT NOT NULL, is_verbatim INTEGER NOT NULL, processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_articles (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL,
                content TEXT NOT NULL, is_verbatim INTEGER NOT NULL, first_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                body TEXT NOT NULL, published_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, last_confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                name TEXT NOT NULL, url TEXT NOT NULL, content TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY, first_checked_at TEXT NOT NULL,
                last_article_at TEXT, stale_alerted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_health (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1), consecutive_failures INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_failure_alert (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1), alerted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rss_failure_tracker (
                source_id TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                failure_alerted_at TEXT
            );
            INSERT OR IGNORE INTO task_health(singleton, consecutive_failures) VALUES (1, 0);
            """
        )
        self._connection.commit()

    def known_article_ids(self, ids: tuple[str, ...]) -> set[str]:
        """Return the subset of article IDs already processed."""
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"SELECT id FROM articles WHERE id IN ({placeholders})",
            ids,  # noqa: S608
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
                    _storage_time(article.published_at),
                    article.content,
                    article.is_verbatim,
                    _storage_time(processed_at),
                )
                for article in articles
            ],
        )

    def save_pending_articles(self, articles: tuple[Article, ...], first_seen_at: pendulum.DateTime) -> None:
        """Persist articles awaiting successful briefing delivery."""
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
                    _storage_time(article.published_at),
                    article.content,
                    article.is_verbatim,
                    _storage_time(first_seen_at),
                )
                for article in articles
            ],
        )
        self._connection.commit()

    def pending_articles(self) -> tuple[Article, ...]:
        """Return pending articles in stable processing order."""
        rows = self._connection.execute("SELECT * FROM pending_articles ORDER BY first_seen_at, published_at")
        return tuple(_article_from_row(row) for row in rows)

    def mark_articles_processed(
        self,
        articles: tuple[Article, ...],
        processed_at: pendulum.DateTime,
    ) -> None:
        """Move delivered articles from pending to processed state."""
        self._insert_articles(articles, processed_at)
        if not articles:
            self._connection.commit()
            return
        placeholders = ",".join("?" for _ in articles)
        self._connection.execute(
            f"DELETE FROM pending_articles WHERE id IN ({placeholders})",  # noqa: S608
            tuple(article.id for article in articles),
        )
        self._connection.commit()

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
                _storage_time(checked_at),
                _storage_time(latest_at) if latest_at else None,
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
            if _parse_time(reference_time) < threshold:
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
            (_storage_time(alerted_at), *source_ids),
        )
        self._connection.commit()

    def recent_briefings(self, now: pendulum.DateTime, history_hours: int) -> tuple[BriefingRecord, ...]:
        """Return briefings inside the configured history window."""
        threshold = _storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT kind, body, published_at FROM briefings WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(
            BriefingRecord(
                kind=str(row["kind"]),
                body=str(row["body"]),
                published_at=_parse_time(str(row["published_at"])),
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
            (kind, _storage_time(start), _storage_time(end)),
        ).fetchone()
        return row is not None

    def recent_articles(self, now: pendulum.DateTime, history_hours: int) -> tuple[Article, ...]:
        """Return processed articles inside the configured history window."""
        threshold = _storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT * FROM articles WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(_article_from_row(row) for row in rows)

    def save_briefing(self, kind: str, body: str, published_at: pendulum.DateTime) -> None:
        """Persist a successfully published briefing."""
        self._connection.execute(
            "INSERT INTO briefings(kind, body, published_at) VALUES (?, ?, ?)",
            (kind, body, _storage_time(published_at)),
        )
        self._connection.commit()

    def save_context_documents(self, documents: tuple[SourceDocument, ...], observed_at: pendulum.DateTime) -> None:
        """Persist context documents observed during a successful run."""
        self._connection.executemany(
            """INSERT INTO context_snapshots(source_id, name, url, content, observed_at)
            VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    document.id,
                    document.name,
                    document.url,
                    document.content,
                    _storage_time(observed_at),
                )
                for document in documents
            ],
        )
        self._connection.commit()

    def recent_context_documents(self, now: pendulum.DateTime, history_hours: int) -> tuple[SourceDocument, ...]:
        """Return context documents inside the configured history window."""
        threshold = _storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            """SELECT source_id, name, url, content FROM context_snapshots
            WHERE observed_at >= ? ORDER BY observed_at""",
            (threshold,),
        )
        return tuple(
            SourceDocument(
                id=str(row["source_id"]),
                name=str(row["name"]),
                url=str(row["url"]),
                content=str(row["content"]),
            )
            for row in rows
        )

    def active_warnings(self, now: pendulum.DateTime, retention_hours: int) -> tuple[Warning, ...]:
        """Return warnings confirmed inside the retention window."""
        threshold = _storage_time(now.subtract(hours=retention_hours))
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
                    last_confirmed_at=_parse_time(row["last_confirmed_at"]),
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
            confirmed_at = now if existing is None or has_new_evidence else _parse_time(existing["last_confirmed_at"])
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
                (warning.id, payload, _storage_time(confirmed_at)),
            )
        self._connection.commit()

    def record_success(
        self,
        now: pendulum.DateTime,
        *,
        history_hours: int,
        warning_retention_hours: int,
    ) -> None:
        """Record task success and prune expired history in one transaction."""
        history_threshold = _storage_time(now.subtract(hours=history_hours))
        warning_threshold = _storage_time(now.subtract(hours=warning_retention_hours))
        self._connection.execute("DELETE FROM articles WHERE processed_at < ?", (history_threshold,))
        self._connection.execute("DELETE FROM briefings WHERE published_at < ?", (history_threshold,))
        self._connection.execute("DELETE FROM context_snapshots WHERE observed_at < ?", (history_threshold,))
        self._connection.execute("DELETE FROM warnings WHERE last_confirmed_at < ?", (warning_threshold,))
        self._connection.execute("UPDATE task_health SET consecutive_failures = 0 WHERE singleton = 1")
        self._connection.execute("DELETE FROM task_failure_alert WHERE singleton = 1")
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
            (_storage_time(alerted_at),),
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
            (_storage_time(alerted_at), *source_ids),
        )
        self._connection.commit()


def _article_from_row(row: sqlite3.Row) -> Article:
    return Article(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        source_name=str(row["source_name"]),
        title=str(row["title"]),
        url=str(row["url"]),
        published_at=_parse_time(row["published_at"]),
        content=str(row["content"]),
        is_verbatim=bool(row["is_verbatim"]),
    )


def _parse_time(value: str) -> pendulum.DateTime:
    if not _STORAGE_TIME_PATTERN.fullmatch(value):
        raise ValueError("State timestamp must use fixed-width UTC format")
    return pendulum.from_format(value, "YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]", tz="UTC")


def _storage_time(value: pendulum.DateTime) -> str:
    aware = require_aware_datetime(value, context="State timestamp")
    return aware.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]")
