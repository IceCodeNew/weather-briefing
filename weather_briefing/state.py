from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pendulum

from .models import Article, SourceDocument, Warning
from .time_utils import require_aware_datetime

_STORAGE_TIME_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")


class SQLiteStateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL,
                content TEXT NOT NULL, is_verbatim INTEGER NOT NULL, processed_at TEXT NOT NULL
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
            INSERT OR IGNORE INTO task_health(singleton, consecutive_failures) VALUES (1, 0);
            """
        )
        self._connection.commit()

    def known_article_ids(self, ids: tuple[str, ...]) -> set[str]:
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"SELECT id FROM articles WHERE id IN ({placeholders})",
            ids,  # noqa: S608
        )
        return {str(row["id"]) for row in rows}

    def save_articles(self, articles: tuple[Article, ...], processed_at: pendulum.DateTime) -> None:
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
        self._connection.commit()

    def record_source_check(
        self,
        source_id: str,
        checked_at: pendulum.DateTime,
        latest_at: pendulum.DateTime | None,
    ) -> None:
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
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        self._connection.execute(
            f"UPDATE source_health SET stale_alerted_at = ? "  # noqa: S608
            f"WHERE source_id IN ({placeholders})",
            (_storage_time(alerted_at), *source_ids),
        )
        self._connection.commit()

    def recent_briefings(self, now: pendulum.DateTime, history_hours: int) -> tuple[str, ...]:
        threshold = _storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT body FROM briefings WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(str(row["body"]) for row in rows)

    def recent_articles(self, now: pendulum.DateTime, history_hours: int) -> tuple[Article, ...]:
        threshold = _storage_time(now.subtract(hours=history_hours))
        rows = self._connection.execute(
            "SELECT * FROM articles WHERE published_at >= ? ORDER BY published_at",
            (threshold,),
        )
        return tuple(
            Article(
                id=str(row["id"]),
                source_id=str(row["source_id"]),
                source_name=str(row["source_name"]),
                title=str(row["title"]),
                url=str(row["url"]),
                published_at=_parse_time(row["published_at"]),
                content=str(row["content"]),
                is_verbatim=bool(row["is_verbatim"]),
            )
            for row in rows
        )

    def save_briefing(self, kind: str, body: str, published_at: pendulum.DateTime) -> None:
        self._connection.execute(
            "INSERT INTO briefings(kind, body, published_at) VALUES (?, ?, ?)",
            (kind, body, _storage_time(published_at)),
        )
        self._connection.commit()

    def save_context_documents(self, documents: tuple[SourceDocument, ...], observed_at: pendulum.DateTime) -> None:
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

    def record_success(self) -> None:
        self._connection.execute("UPDATE task_health SET consecutive_failures = 0 WHERE singleton = 1")
        self._connection.commit()

    def record_failure(self) -> int:
        self._connection.execute(
            "UPDATE task_health SET consecutive_failures = consecutive_failures + 1 WHERE singleton = 1"
        )
        self._connection.commit()
        row = self._connection.execute("SELECT consecutive_failures FROM task_health WHERE singleton = 1").fetchone()
        return int(row["consecutive_failures"])


def _parse_time(value: str) -> pendulum.DateTime:
    if not _STORAGE_TIME_PATTERN.fullmatch(value):
        raise ValueError("State timestamp must use fixed-width UTC format")
    return pendulum.from_format(value, "YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]", tz="UTC")


def _storage_time(value: pendulum.DateTime) -> str:
    aware = require_aware_datetime(value, context="State timestamp")
    return aware.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss.SSSSSS[Z]")
