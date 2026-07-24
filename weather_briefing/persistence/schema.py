"""Application-state SQLite schema and forward migrations."""

from __future__ import annotations

import sqlite3


def initialize_state(connection: sqlite3.Connection) -> None:
    """Create the current schema and apply supported forward migrations."""
    connection.executescript(
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
                CREATE TABLE IF NOT EXISTS verbatim_delivery_queue (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id TEXT NOT NULL UNIQUE REFERENCES articles(id),
                    silent INTEGER NOT NULL CHECK (silent IN (0, 1)),
                    queued_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, last_confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                    name TEXT NOT NULL, url TEXT NOT NULL, content TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'zh-CN',
                    history_summary TEXT, history_value TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_budget_alert (
                    source_id TEXT PRIMARY KEY, content_fingerprint TEXT NOT NULL, alerted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_status_message_state (
                    source_id TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    observed_revision_id TEXT NOT NULL,
                    observed_title TEXT NOT NULL,
                    observed_status TEXT NOT NULL,
                    observed_body TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    decided_revision_id TEXT,
                    should_notify INTEGER CHECK (should_notify IN (0, 1)),
                    handled_revision_id TEXT,
                    handled_title TEXT,
                    handled_status TEXT,
                    handled_body TEXT,
                    handled_at TEXT,
                    PRIMARY KEY(source_id, incident_id)
                );
                CREATE TABLE IF NOT EXISTS service_status_message_delivery (
                    source_id TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    publisher_id TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, incident_id, revision_id, publisher_id)
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
    context_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(context_snapshots)")}
    if "history_summary" not in context_columns:
        connection.execute("ALTER TABLE context_snapshots ADD COLUMN history_summary TEXT")
    if "history_value" not in context_columns:
        connection.execute("ALTER TABLE context_snapshots ADD COLUMN history_value TEXT")
    if "language" not in context_columns:
        connection.execute("ALTER TABLE context_snapshots ADD COLUMN language TEXT NOT NULL DEFAULT 'zh-CN'")
    connection.commit()
