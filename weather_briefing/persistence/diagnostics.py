"""SQLite-backed runtime diagnostic switches."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pendulum

from ..time_utils import require_aware_datetime
from .serialization import _parse_time, _storage_time

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
