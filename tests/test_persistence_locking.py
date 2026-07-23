import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import weather_briefing.persistence.locking as locking_module
from weather_briefing.persistence import (
    StateDirectoryInUseError,
    daemon_state_owner,
    serialized_state_run,
)


def test_daemon_state_owner_rejects_a_second_daemon(tmp_path: Path) -> None:
    state_path = tmp_path / "weather.sqlite3"

    with (
        daemon_state_owner(state_path),
        pytest.raises(StateDirectoryInUseError, match="Another weather-briefing daemon"),
        daemon_state_owner(state_path),
    ):
        pytest.fail("A second daemon acquired the state directory")  # pragma: no cover - failure guard


def test_daemon_state_owner_excludes_another_process(tmp_path: Path) -> None:
    state_path = tmp_path / "weather.sqlite3"
    probe = """
import sys
from pathlib import Path
from weather_briefing.persistence import StateDirectoryInUseError, daemon_state_owner

try:
    with daemon_state_owner(Path(sys.argv[1])):
        raise SystemExit(2)
except StateDirectoryInUseError:
    raise SystemExit(0)
"""

    with daemon_state_owner(state_path):
        result = subprocess.run(
            (sys.executable, "-c", probe, str(state_path)),
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stderr


def test_daemon_state_owner_releases_lock_on_exit(tmp_path: Path) -> None:
    state_path = tmp_path / "weather.sqlite3"

    with daemon_state_owner(state_path):
        pass
    with daemon_state_owner(state_path):
        pass


def test_daemon_state_owner_allows_sibling_state_directories(tmp_path: Path) -> None:
    production = tmp_path / "state" / "weather.sqlite3"
    production_test = tmp_path / "abc-state" / "weather.sqlite3"

    with daemon_state_owner(production), daemon_state_owner(production_test):
        assert (production.parent / ".weather-briefing.daemon.lock").exists()
        assert (production_test.parent / ".weather-briefing.daemon.lock").exists()


async def test_serialized_state_run_waits_for_the_active_run(tmp_path: Path) -> None:
    state_path = tmp_path / "weather.sqlite3"
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_run() -> None:
        async with serialized_state_run(state_path):
            first_entered.set()
            await release_first.wait()

    async def second_run() -> None:
        async with serialized_state_run(state_path):
            second_entered.set()

    first_task = asyncio.create_task(first_run())
    await first_entered.wait()
    second_task = asyncio.create_task(second_run())
    await asyncio.sleep(0.05)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


async def test_serialized_state_run_releases_lock_acquired_after_cancellation(tmp_path: Path) -> None:
    state_path = tmp_path / "weather.sqlite3"
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first_lock() -> None:
        async with serialized_state_run(state_path):
            first_entered.set()
            await release_first.wait()

    async def wait_for_lock() -> None:
        async with serialized_state_run(state_path):
            pytest.fail("Canceled run entered the critical section")  # pragma: no cover - failure guard

    first_task = asyncio.create_task(hold_first_lock())
    await first_entered.wait()
    canceled_task = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0.05)

    canceled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled_task

    release_first.set()
    await first_task
    async with asyncio.timeout(1):
        async with serialized_state_run(state_path):
            pass


async def test_cancelled_lock_cleanup_ignores_acquisition_failure() -> None:
    acquisition = asyncio.get_running_loop().create_future()
    acquisition.set_exception(OSError("locking unavailable"))

    locking_module._close_cancelled_lock_acquisition(acquisition)


async def test_serialized_state_run_closes_file_when_locking_fails(monkeypatch, tmp_path: Path) -> None:
    lock_file = (tmp_path / "run.lock").open("a+", encoding="utf-8")

    def fail_lock(_file: object, _operation: int) -> None:
        raise OSError("locking unavailable")

    monkeypatch.setattr(locking_module, "_open_lock_file", lambda _path, _purpose: lock_file)
    monkeypatch.setattr(locking_module.fcntl, "flock", fail_lock)

    with pytest.raises(OSError, match="locking unavailable"):
        async with serialized_state_run(tmp_path / "weather.sqlite3"):
            pytest.fail("Run started without acquiring its lock")  # pragma: no cover - failure guard

    assert lock_file.closed
