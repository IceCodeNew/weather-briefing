"""CLI logging and temporary sensitive-text diagnostics."""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pendulum

from .config import state_path_from_env
from .persistence import diagnostics as diagnostics_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from .delivery import RenderedTextDiagnostics

_LOGGER = logging.getLogger("weather_briefing")
SENSITIVE_SDK_LOGGERS = ("any_llm", "openai", "httpx", "httpcore")


class _UTCISOFormatter(logging.Formatter):
    """Render log record timestamps as explicit UTC ISO-8601 values."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """Format the record creation time without relying on the host timezone."""
        del datefmt
        return pendulum.from_timestamp(record.created, tz="UTC").to_iso8601_string()


@contextmanager
def runtime_diagnostics(path: Path) -> Iterator[RenderedTextDiagnostics | None]:
    """Open diagnostics without making their unavailability fatal."""
    try:
        diagnostics = diagnostics_store.SQLiteRuntimeDiagnostics(path)
    except (OSError, sqlite3.Error):
        _LOGGER.warning(
            "Runtime diagnostics unavailable; continuing without sensitive rendered text logging",
            exc_info=True,
        )
        yield None
        return
    with diagnostics:
        yield diagnostics


def configure_logging(*, debug: bool) -> None:
    """Configure application logging while keeping third-party SDKs quiet."""
    level = logging.DEBUG if debug else logging.INFO
    formatter = _UTCISOFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not _LOGGER.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        _LOGGER.addHandler(handler)
    _LOGGER.setLevel(level)
    _LOGGER.propagate = False
    if not logging.root.handlers:
        root_handler = logging.StreamHandler(sys.stderr)
        root_handler.setFormatter(formatter)
        logging.root.addHandler(root_handler)
    logging.root.setLevel(logging.WARNING)
    for handler in logging.root.handlers:
        handler.setLevel(logging.WARNING)
    for logger_name in SENSITIVE_SDK_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def manage_rendered_text_diagnostics(action: object, duration_seconds: object = None) -> None:
    """Enable, disable, or inspect the temporary rendered-text diagnostic switch."""
    if not isinstance(action, str) or action not in {"enable", "disable", "status"}:
        msg = f"Unsupported rendered text diagnostics action: {action}"
        raise ValueError(msg)
    validated_duration: int | None = None
    if action == "enable":
        if duration_seconds is None:
            msg = "Rendered text diagnostics require a duration"
            raise ValueError(msg)
        if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool) or duration_seconds <= 0:
            msg = "Rendered text diagnostics duration must be a positive integer"
            raise ValueError(msg)
        validated_duration = duration_seconds
    elif duration_seconds is not None:
        msg = "Rendered text diagnostics duration is only valid for enable"
        raise ValueError(msg)

    with diagnostics_store.SQLiteRuntimeDiagnostics(state_path_from_env()) as diagnostics:
        if validated_duration is not None:
            expires_at = pendulum.now("UTC").add(seconds=validated_duration)
            diagnostics.enable_rendered_text_logging(expires_at)
            print(  # noqa: T201
                "Rendered text diagnostic logging enabled until "
                f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
            )
            return
        if action == "disable":
            diagnostics.disable_rendered_text_logging()
            print("Rendered text diagnostic logging disabled")  # noqa: T201
            return
        expires_at = diagnostics.rendered_text_logging_until()
        if expires_at is None:
            print("Rendered text diagnostic logging is disabled")  # noqa: T201
        else:
            print(  # noqa: T201
                "Rendered text diagnostic logging is enabled until "
                f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
            )
