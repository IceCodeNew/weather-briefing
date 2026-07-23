"""Process coordination for a local weather-briefing state directory."""

from __future__ import annotations

import asyncio
import fcntl
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TextIO


class StateDirectoryInUseError(RuntimeError):
    """Report that another daemon owns the configured state directory."""


@contextmanager
def daemon_state_owner(state_path: Path) -> Iterator[None]:
    """Hold exclusive ownership of a state directory for one daemon lifetime."""
    lock_file = _open_lock_file(state_path, "daemon")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateDirectoryInUseError(
                "Another weather-briefing daemon is already using the configured state directory"
            ) from None
        yield
    finally:
        lock_file.close()


@asynccontextmanager
async def serialized_state_run(state_path: Path) -> AsyncIterator[None]:
    """Serialize complete scheduled and manual business runs."""
    lock_file = await asyncio.to_thread(_acquire_run_lock, state_path)
    try:
        yield
    finally:
        lock_file.close()


def _acquire_run_lock(state_path: Path) -> TextIO:
    lock_file = _open_lock_file(state_path, "run")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except BaseException:
        lock_file.close()
        raise
    return lock_file


def _open_lock_file(state_path: Path, purpose: str) -> TextIO:
    state_directory = state_path.parent
    state_directory.mkdir(parents=True, exist_ok=True)
    return (state_directory / f".weather-briefing.{purpose}.lock").open("a+", encoding="utf-8")
