"""Command-line argument and version parsing."""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

_LOGGER = logging.getLogger("weather_briefing.command_parser")


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for one-shot runs, the daemon, and diagnostics."""
    parser = argparse.ArgumentParser(description="Generate a stateful weather briefing")
    parser.add_argument("-V", "--version", action=_VersionAction, nargs=0)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("kind", choices=("forecast", "briefing"))
    timing_group = run_parser.add_mutually_exclusive_group()
    timing_group.add_argument("--enforce-window", action="store_true")
    timing_group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the selected one-shot task immediately; briefings also publish deferred information",
    )
    run_time_group = run_parser.add_mutually_exclusive_group()
    run_time_group.add_argument("--at", help="Override run time with an ISO-8601 timestamp including UTC offset")
    run_time_group.add_argument("--date", help="Generate a forecast for a local date in YYYY-MM-DD format")
    subparsers.add_parser("daemon")
    subparsers.add_parser("service-status")
    diagnostics_parser = subparsers.add_parser("diagnostics")
    diagnostics_topics = diagnostics_parser.add_subparsers(dest="diagnostics_topic", required=True)
    rendered_text_parser = diagnostics_topics.add_parser("rendered-text")
    rendered_text_actions = rendered_text_parser.add_subparsers(dest="diagnostics_action", required=True)
    enable_parser = rendered_text_actions.add_parser("enable")
    enable_parser.add_argument(
        "--for",
        dest="duration_seconds",
        required=True,
        type=_diagnostic_duration_seconds,
        metavar="DURATION",
        help="Enable sensitive rendered-text logging temporarily, for example 15m or 1h (maximum 24h)",
    )
    rendered_text_actions.add_parser("status")
    rendered_text_actions.add_parser("disable")
    return parser


class _VersionAction(argparse.Action):
    """Resolve development Git metadata only when version output is requested."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[object] | None,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        print(f"{parser.prog} {_display_version()}")  # noqa: T201
        parser.exit()


def _display_version() -> str:
    """Add Git revision details to development versions when available."""
    if not __version__.endswith("-dev"):
        return __version__

    repository_root = Path(__file__).resolve().parents[1]
    try:
        git_metadata = subprocess.run(  # noqa: S603
            (  # noqa: S607
                "git",
                "-C",
                str(repository_root),
                "rev-parse",
                "--show-toplevel",
                "--short=7",
                "HEAD",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if len(git_metadata) != 2 or Path(git_metadata[0]).resolve() != repository_root:  # noqa: PLR2004
            return __version__
        revision = git_metadata[1]
        status = subprocess.run(  # noqa: S603
            ("git", "-C", str(repository_root), "status", "--porcelain"),  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except OSError as exc:
        _LOGGER.debug(
            "Git metadata probe unavailable; using package version: error_type=%s",
            type(exc).__name__,
        )
        return __version__
    except subprocess.CalledProcessError as exc:
        _LOGGER.debug(
            "Git metadata probe failed; using package version: returncode=%d",
            exc.returncode,
        )
        return __version__

    version = __version__.removesuffix("-dev")
    dirty = "-dirty" if status else ""
    return f"{version}{dirty}-g{revision}"


_DIAGNOSTIC_DURATION_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smh])$")


def _diagnostic_duration_seconds(value: str) -> int:
    match = _DIAGNOSTIC_DURATION_PATTERN.fullmatch(value)
    if match is None:
        msg = "duration must use a positive value followed by s, m, or h"
        raise argparse.ArgumentTypeError(msg)
    multipliers = {"s": 1, "m": 60, "h": 3600}
    seconds = int(match.group("value")) * multipliers[match.group("unit")]
    if seconds > 24 * 60 * 60:
        msg = "duration cannot exceed 24h"
        raise argparse.ArgumentTypeError(msg)
    return seconds
