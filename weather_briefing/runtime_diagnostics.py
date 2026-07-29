"""CLI logging and temporary sensitive-text diagnostics."""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pendulum

from .config import state_path_from_env
from .delivery import RenderedTextDiagnostics
from .persistence import diagnostics as diagnostics_store

_LOGGER = logging.getLogger("weather_briefing")
SENSITIVE_SDK_LOGGERS = ("any_llm", "openai", "httpx", "httpcore")


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
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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


def manage_rendered_text_diagnostics(action: str, duration_seconds: int | None = None) -> None:
    """Enable, disable, or inspect the temporary rendered-text diagnostic switch."""
    with diagnostics_store.SQLiteRuntimeDiagnostics(state_path_from_env()) as diagnostics:
        if action == "enable":
            if duration_seconds is None:
                raise ValueError("Rendered text diagnostics require a duration")
            expires_at = pendulum.now("UTC").add(seconds=duration_seconds)
            diagnostics.enable_rendered_text_logging(expires_at)
            print(
                "Rendered text diagnostic logging enabled until "
                f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
            )
            return
        if action == "disable":
            diagnostics.disable_rendered_text_logging()
            print("Rendered text diagnostic logging disabled")
            return
        if action == "status":
            expires_at = diagnostics.rendered_text_logging_until()
            if expires_at is None:
                print("Rendered text diagnostic logging is disabled")
            else:
                print(
                    "Rendered text diagnostic logging is enabled until "
                    f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
                )
            return
        raise ValueError(f"Unsupported rendered text diagnostics action: {action}")
