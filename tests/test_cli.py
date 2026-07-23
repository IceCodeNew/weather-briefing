import base64
import io
import logging
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import pendulum
import pytest

import weather_briefing.cli as cli_module
from weather_briefing.capabilities import CapabilityName
from weather_briefing.cli import (
    _LOGGER,
    _SENSITIVE_SDK_LOGGERS,
    _briefing_delivery_policy,
    _briefing_sent_today,
    _configure_logging,
    _delivery_provider,
    _hour_in_cron,
    _in_schedule,
    _llm_provider,
    _location_state_path,
    _manage_rendered_text_diagnostics,
    _parse_forecast_date,
    _parse_run_time,
    _precision_reduction_notice,
    _save_resolved_location_fields,
    _weather_context_provider,
    build_parser,
    daemon,
    main,
    run,
)
from weather_briefing.composition.providers import (
    PUBLISHER_BUILDERS,
    _build_jma,
    _build_nea,
    _build_open_meteo,
    _build_qweather,
)
from weather_briefing.composition.providers import aqicn_provider as _aqicn_provider
from weather_briefing.composition.providers import build_weather_provider as _build_weather_provider
from weather_briefing.composition.providers import qweather_is_configured as _qweather_is_configured
from weather_briefing.composition.providers import weather_provider_metadata as _weather_provider_metadata
from weather_briefing.config import ConfigurationError, Settings
from weather_briefing.models import LocationSpec, ResolvedLocation
from weather_briefing.persistence import daemon_state_owner
from weather_briefing.registries import PublisherName, WeatherProviderName
from weather_briefing.state import SQLiteRuntimeDiagnostics, SQLiteStateStore
from weather_briefing.weather import QWeatherProvider

_REQUIRED_SENSITIVE_SDK_LOGGERS = frozenset({"any_llm", "openai", "httpx", "httpcore"})


class _ClosableLLMProviderStub:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_location_backfill_failure_logs_warning_and_continues(monkeypatch, caplog) -> None:
    settings = _make_fake_settings(locations=(LocationSpec(id="test", name="Test City"),))
    location = ResolvedLocation("test", "Test City", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)

    def fail_backfill(*_args: object) -> bool:
        raise ConfigurationError("locations.json is locked")

    monkeypatch.setattr("weather_briefing.cli.backfill_location_fields", fail_backfill)

    with caplog.at_level(logging.WARNING, logger="weather_briefing"):
        _save_resolved_location_fields(settings, (location,))

    assert "Could not save resolved location fields; continuing without updating" in caplog.text
    assert "locations.json is locked" in caplog.text


def test_configure_logging_is_idempotent_and_updates_level() -> None:
    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    assert set(_SENSITIVE_SDK_LOGGERS) >= _REQUIRED_SENSITIVE_SDK_LOGGERS
    sdk_loggers = [logging.getLogger(name) for name in _SENSITIVE_SDK_LOGGERS]
    original_sdk_levels = [logger.level for logger in sdk_loggers]
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        _configure_logging(debug=False)
        own_handler = _LOGGER.handlers[0]
        root_handler = logging.root.handlers[0]
        _configure_logging(debug=True)

        assert _LOGGER.handlers == [own_handler]
        assert _LOGGER.level == logging.DEBUG
        assert not _LOGGER.propagate
        assert logging.root.handlers == [root_handler]
        assert logging.root.level == logging.WARNING
        assert root_handler.level == logging.WARNING
        assert all(logger.level == logging.WARNING for logger in sdk_loggers)
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)
        for logger, original_level in zip(sdk_loggers, original_sdk_levels, strict=True):
            logger.setLevel(original_level)


def test_debug_logging_keeps_application_metadata_and_suppresses_sdk_payloads() -> None:
    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    sdk_loggers = [logging.getLogger(name) for name in (*_SENSITIVE_SDK_LOGGERS, "provider_sdk")]
    original_sdk_levels = [logger.level for logger in sdk_loggers]
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()
        output = io.StringIO()
        _LOGGER.addHandler(logging.StreamHandler(output))
        logging.root.addHandler(logging.StreamHandler(output))
        _configure_logging(debug=True)

        logging.getLogger("weather_briefing.service").debug("LLM attempt=1 input_characters=42")
        logging.getLogger("any_llm").debug("private prompt from any-llm")
        logging.getLogger("openai._base_client").debug("private request body from OpenAI SDK")
        logging.getLogger("provider_sdk").setLevel(logging.DEBUG)
        logging.getLogger("provider_sdk").debug("private payload from a provider SDK")

        logged_text = output.getvalue()
        assert "LLM attempt=1 input_characters=42" in logged_text
        assert "private prompt" not in logged_text
        assert "private request body" not in logged_text
        assert "private payload" not in logged_text
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)
        for logger, original_level in zip(sdk_loggers, original_sdk_levels, strict=True):
            logger.setLevel(original_level)


@pytest.mark.parametrize(
    "value",
    ("2026-03-29T02:30:00", "2026-10-25T02:30:00"),
)
def test_parse_run_time_rejects_timestamp_without_timezone(value: str) -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        _parse_run_time(value, pendulum.timezone("Europe/Paris"))


def test_parse_run_time_converts_explicit_offset_to_configured_timezone() -> None:
    parsed = _parse_run_time("2026-07-11T00:00:00Z", pendulum.timezone("Asia/Shanghai"))

    assert parsed.to_iso8601_string() == "2026-07-11T08:00:00+08:00"


def test_parse_run_time_preserves_existing_minute_precision_support() -> None:
    parsed = _parse_run_time("2026-07-11T08:30+08:00", pendulum.timezone("Asia/Shanghai"))

    assert parsed.to_iso8601_string() == "2026-07-11T08:30:00+08:00"


def test_parse_forecast_date_uses_configured_local_schedule_time() -> None:
    parsed = _parse_forecast_date("2026-07-11")

    assert str(parsed) == "2026-07-11"


@pytest.mark.parametrize("value", ("20260711", "2026-02-30"))
def test_parse_forecast_date_rejects_invalid_date(value: str) -> None:
    with pytest.raises(ValueError, match="Forecast date"):
        _parse_forecast_date(value)


def test_multiple_locations_receive_isolated_state_paths() -> None:
    location = ResolvedLocation(
        "example",
        "北京市西城区中南海",
        39.911389,
        116.380556,
        "CN",
        "北京市",
        "Asia/Shanghai",
        True,
    )

    assert _location_state_path(Path("state/weather.sqlite3"), location, 1) == Path("state/weather.sqlite3")
    assert _location_state_path(Path("state/weather.sqlite3"), location, 2) == Path("state/weather-example.sqlite3")


def test_precision_reduction_notice_contains_match_coordinates_and_action() -> None:
    location = ResolvedLocation(
        "example",
        "北京市西城区中南海1号",
        39.911389,
        116.380556,
        "CN",
        "北京市",
        "Asia/Shanghai",
        True,
        matched_name="中南海, 西城区, 北京市, 中国",
        precision_reduced=True,
    )

    notice = _precision_reduction_notice(location, Path("locations.json"))

    assert "中南海, 西城区, 北京市, 中国" in notice
    assert "39.9113890" in notice
    assert "116.3805560" in notice
    assert "locations.json" in notice
    assert "Confirm that this location is correct" in notice


def test_precision_reduction_notice_uses_english_fallback_for_missing_match() -> None:
    location = ResolvedLocation(
        "example",
        "Test City",
        1.0,
        1.0,
        "CN",
        "Beijing",
        "Asia/Shanghai",
        True,
        precision_reduced=True,
    )

    notice = _precision_reduction_notice(location, Path("locations.json"))

    assert 'matched at reduced precision as "no matched name provided"' in notice


class TestHourInCron:
    @pytest.mark.parametrize("hour", (9, 15, 23))
    def test_range_includes_bounds(self, hour: int) -> None:
        assert _hour_in_cron(hour, "9-23")

    @pytest.mark.parametrize("hour", (0, 8, 24))
    def test_range_excludes_outside(self, hour: int) -> None:
        assert not _hour_in_cron(hour, "9-23")

    def test_single_value(self) -> None:
        assert _hour_in_cron(8, "8")
        assert not _hour_in_cron(9, "8")

    def test_comma_separated_list(self) -> None:
        assert _hour_in_cron(8, "8,12,16")
        assert _hour_in_cron(12, "8,12,16")
        assert _hour_in_cron(16, "8,12,16")
        assert not _hour_in_cron(9, "8,12,16")

    def test_wildcard_falls_back_to_true(self) -> None:
        assert _hour_in_cron(5, "*")

    def test_stepped_wildcard_matches_only_scheduled_hours(self) -> None:
        assert _hour_in_cron(4, "*/2")
        assert not _hour_in_cron(5, "*/2")

    def test_stepped_range(self) -> None:
        assert _hour_in_cron(3, "1-23/2")
        assert not _hour_in_cron(2, "1-23/2")

    def test_comma_whitespace_tolerance(self) -> None:
        assert _hour_in_cron(12, " 9 , 12 , 16 ")


class TestInSchedule:
    def test_forecast_matches_greeting_hour(self, monkeypatch) -> None:
        monkeypatch.setenv("GREETING_HOUR", "7")
        monkeypatch.setenv("GREETING_MINUTE", "30")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_match = pendulum.datetime(2026, 7, 14, 7, 0, tz=settings.timezone)
        now_no_match = pendulum.datetime(2026, 7, 14, 8, 0, tz=settings.timezone)

        assert _in_schedule("forecast", now_match, settings)
        assert not _in_schedule("forecast", now_no_match, settings)

    def test_briefing_matches_cron_range(self, monkeypatch) -> None:
        monkeypatch.setenv("BRIEFING_CRON", "10-18")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_in = pendulum.datetime(2026, 7, 14, 10, 0, tz=settings.timezone)
        now_out = pendulum.datetime(2026, 7, 14, 9, 0, tz=settings.timezone)

        assert _in_schedule("briefing", now_in, settings)
        assert not _in_schedule("briefing", now_out, settings)


def test_version_flag_uses_embedded_release_version(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module, "__version__", "1.1.0")
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])

    assert capsys.readouterr().out == "pytest 1.1.0\n"


def test_development_version_does_not_probe_git_for_other_commands(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "__version__", "1.1.1-dev")
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        Mock(side_effect=AssertionError("Git must only be inspected for --version")),
    )

    args = build_parser().parse_args(["run", "forecast"])

    assert args.command == "run"


@pytest.mark.parametrize(("status", "expected"), (("", "1.1.1-g1234567"), (" M file.py\n", "1.1.1-dirty-g1234567")))
def test_development_version_includes_git_revision(monkeypatch, capsys, status: str, expected: str) -> None:
    repository_root = Path(cli_module.__file__).resolve().parents[1]
    results = iter(
        (
            subprocess.CompletedProcess((), 0, f"{repository_root}\n1234567\n", ""),
            subprocess.CompletedProcess((), 0, status, ""),
        )
    )
    monkeypatch.setattr(cli_module, "__version__", "1.1.1-dev")
    git = Mock(side_effect=lambda *args, **kwargs: next(results))
    monkeypatch.setattr(cli_module.subprocess, "run", git)

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == f"pytest {expected}\n"
    assert git.call_args_list == [
        call(
            (
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
        ),
        call(
            ("git", "-C", str(repository_root), "status", "--porcelain"),
            check=True,
            capture_output=True,
            text=True,
        ),
    ]


@pytest.mark.parametrize("metadata", ("/unrelated/repository\n1234567\n", "unexpected\n"))
def test_development_version_rejects_unrelated_git_metadata(monkeypatch, capsys, metadata: str) -> None:
    monkeypatch.setattr(cli_module, "__version__", "1.1.1-dev")
    git = Mock(return_value=subprocess.CompletedProcess((), 0, metadata, ""))
    monkeypatch.setattr(cli_module.subprocess, "run", git)

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "pytest 1.1.1-dev\n"
    git.assert_called_once()


@pytest.mark.parametrize(
    "error",
    (FileNotFoundError(), subprocess.CalledProcessError(128, ("git", "rev-parse"))),
)
def test_development_version_falls_back_outside_git(monkeypatch, capsys, error: Exception) -> None:
    monkeypatch.setattr(cli_module, "__version__", "1.1.1-dev")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli_module.subprocess, "run", fail)

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "pytest 1.1.1-dev\n"


def test_rendered_text_diagnostics_parser_accepts_bounded_duration() -> None:
    args = build_parser().parse_args(["diagnostics", "rendered-text", "enable", "--for", "15m"])

    assert args.diagnostics_action == "enable"
    assert args.duration_seconds == 900


@pytest.mark.parametrize("kind", ("forecast", "briefing"))
def test_run_now_is_a_one_shot_option_for_each_task(kind: str) -> None:
    args = build_parser().parse_args(["run", kind, "--run-now"])

    assert args.kind == kind
    assert args.run_now
    assert not args.enforce_window


@pytest.mark.parametrize("kind", ("daily", "hourly"))
def test_removed_run_mode_names_are_rejected(kind: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", kind])


def test_run_now_and_enforce_window_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "briefing", "--run-now", "--enforce-window"])


def test_forecast_date_and_at_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "forecast", "--date", "2026-07-11", "--at", "2026-07-11T08:00:00+08:00"])


async def test_run_holds_state_lock_while_loading_settings(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import patch

    state_path = tmp_path / "weather.sqlite3"
    settings = replace(_make_fake_settings(), state_path=state_path)
    lock_is_held = False

    @asynccontextmanager
    async def record_state_lock(path: Path) -> AsyncIterator[None]:
        nonlocal lock_is_held
        assert path == state_path
        lock_is_held = True
        try:
            yield
        finally:
            lock_is_held = False

    def load_settings(_cls: type[Settings]) -> Settings:
        assert lock_is_held
        return settings

    monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("weather_briefing.persistence.locking.serialized_state_run", record_state_lock)

    with (
        patch.object(Settings, "from_env", classmethod(load_settings)),
        pytest.raises(ValueError, match="only supported for run forecast"),
    ):
        await run("briefing", enforce_window=False, forecast_date="2026-07-11")

    assert not lock_is_held


async def test_forecast_date_rejects_past_date_and_points_to_at(monkeypatch) -> None:
    from unittest.mock import patch

    settings = _make_fake_settings()
    now = pendulum.datetime(2026, 7, 13, 8, tz=settings.timezone)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda value, timezone: now)

    with (
        patch.object(Settings, "from_env", classmethod(lambda cls: settings)),
        pytest.raises(ValueError, match="use --at for historical tests"),
    ):
        await run("forecast", enforce_window=False, forecast_date="2026-07-12")


def test_briefing_delivery_policy_forces_final_window_silently() -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, 16, tz=settings.timezone)

    assert _briefing_delivery_policy(
        "briefing",
        now,
        settings,
        run_now=False,
        briefing_sent_today=False,
    ) == (True, True)


def test_briefing_delivery_policy_does_not_flush_after_earlier_delivery() -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, 16, tz=settings.timezone)

    assert _briefing_delivery_policy(
        "briefing",
        now,
        settings,
        run_now=False,
        briefing_sent_today=True,
    ) == (False, False)


def test_briefing_delivery_policy_keeps_manual_run_now_audible() -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, 10, tz=settings.timezone)

    assert _briefing_delivery_policy(
        "briefing",
        now,
        settings,
        run_now=True,
        briefing_sent_today=True,
    ) == (True, False)


def test_briefing_delivery_policy_does_not_force_earlier_window() -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, 12, tz=settings.timezone)

    assert _briefing_delivery_policy(
        "briefing",
        now,
        settings,
        run_now=False,
        briefing_sent_today=False,
    ) == (False, False)


def test_forecast_delivery_policy_never_forces_briefing_delivery() -> None:
    settings = _make_fake_settings()
    now = pendulum.datetime(2026, 7, 14, 8, tz=settings.timezone)

    assert _briefing_delivery_policy(
        "forecast",
        now,
        settings,
        run_now=False,
        briefing_sent_today=False,
    ) == (False, False)


def test_briefing_sent_today_reads_final_window_state(tmp_path: Path) -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, 16, tz=settings.timezone)
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_briefing("briefing", "Earlier briefing", now.subtract(hours=4))

        assert _briefing_sent_today("briefing", now, settings, state, run_now=False)


@pytest.mark.parametrize(
    ("kind", "hour", "run_now"),
    (
        ("forecast", 16, False),
        ("briefing", 16, True),
        ("briefing", 12, False),
    ),
)
def test_briefing_sent_today_skips_irrelevant_state_queries(
    tmp_path: Path,
    kind: str,
    hour: int,
    run_now: bool,
) -> None:
    settings = replace(_make_fake_settings(), hourly_cron="9,12,16")
    now = pendulum.datetime(2026, 7, 14, hour, tz=settings.timezone)
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_briefing("briefing", "Earlier briefing", now.subtract(hours=1))

        assert not _briefing_sent_today(kind, now, settings, state, run_now=run_now)


@pytest.mark.parametrize("duration", ("0m", "15", "25h"))
def test_rendered_text_diagnostics_parser_rejects_invalid_duration(duration: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["diagnostics", "rendered-text", "enable", "--for", duration])


def test_main_manages_rendered_text_diagnostics_without_loading_service_settings(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))
    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)

    monkeypatch.setattr(
        "sys.argv",
        ["weather-briefing", "diagnostics", "rendered-text", "enable", "--for", "15m"],
    )
    main()
    assert "enabled until" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["weather-briefing", "diagnostics", "rendered-text", "status"])
    main()
    assert "is enabled until" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["weather-briefing", "diagnostics", "rendered-text", "disable"])
    main()
    assert capsys.readouterr().out.strip() == "Rendered text diagnostic logging disabled"

    monkeypatch.setattr("sys.argv", ["weather-briefing", "diagnostics", "rendered-text", "status"])
    main()
    assert capsys.readouterr().out.strip() == "Rendered text diagnostic logging is disabled"


@pytest.mark.parametrize(
    ("action", "duration", "message"),
    (
        ("enable", None, "require a duration"),
        ("unsupported", None, "Unsupported rendered text diagnostics action"),
    ),
)
def test_rendered_text_diagnostics_reject_invalid_internal_requests(
    action: str,
    duration: int | None,
    message: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(tmp_path / "state.sqlite3"))

    with pytest.raises(ValueError, match=message):
        _manage_rendered_text_diagnostics(action, duration)


def test_main_loads_dotenv_with_supported_arguments(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_load_dotenv(*, override: bool) -> bool:
        calls.append(override)
        return True

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", fake_load_dotenv)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert calls == [False]


def test_main_configures_info_logging_before_daemon_and_logs_failure_once(monkeypatch, capsys) -> None:
    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level

    async def fail_daemon() -> None:
        assert len(_LOGGER.handlers) == 1
        assert _LOGGER.level == logging.INFO
        raise RuntimeError("daemon-boom")

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli.daemon", fail_daemon)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "daemon"])
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        with pytest.raises(SystemExit) as exc_info:
            main()

        stderr = capsys.readouterr().err
        assert exc_info.value.code == 1
        assert stderr.count("weather-briefing terminated with an error") == 1
        assert stderr.count("RuntimeError: daemon-boom") == 1
        assert "[ERROR] weather_briefing:" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


def test_main_configures_info_logging_before_run_and_logs_failure_once(monkeypatch, capsys) -> None:
    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level

    async def fail_run(
        kind: str,
        enforce_window: bool,
        at: str | None,
        *,
        forecast_date: str | None = None,
        run_now: bool = False,
    ) -> None:
        assert kind == "briefing"
        assert not enforce_window
        assert at is None
        assert forecast_date is None
        assert not run_now
        assert len(_LOGGER.handlers) == 1
        assert _LOGGER.level == logging.INFO
        raise RuntimeError("boom")

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli.run", fail_run)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "run", "briefing"])
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        with pytest.raises(SystemExit) as exc_info:
            main()

        stderr = capsys.readouterr().err
        assert exc_info.value.code == 1
        assert stderr.count("weather-briefing terminated with an error") == 1
        assert stderr.count("RuntimeError: boom") == 1
        assert "[ERROR] weather_briefing:" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


_DEFAULT_TZ = pendulum.timezone("Asia/Shanghai")

_DEFAULT_SETTINGS = Settings(
    debug=False,
    timezone=_DEFAULT_TZ,
    api_key="k",
    llm_provider="deepseek",
    llm_model="m",
    llm_base_url=None,
    llm_max_output_tokens=8192,
    llm_max_attempts=3,
    http_timeout_seconds=30.0,
    locations=(),
    locations_path=Path("locations.json"),
    geocoding_api_key=None,
    geocoding_cache_path=Path("state/geocoding.json"),
    rss_sources_path=Path("rss-sources.json"),
    feeds=(),
    weather_providers=None,
    qweather_project_id=None,
    qweather_credential_id=None,
    qweather_private_key=None,
    qweather_jwt_lifetime_seconds=900,
    qweather_base_url=None,
    nea_api_key=None,
    open_meteo_api_key=None,
    aqicn_api_token=None,
    state_path=Path("state/weather.sqlite3"),
    publisher="stdout",
    telegram_bot_token=None,
    telegram_chat_id=None,
    rss_max_attempts=3,
    rss_retry_min_seconds=3.0,
    rss_retry_max_seconds=5.0,
    rss_stale_hours=24,
    rss_failure_threshold=3,
    warning_retention_hours=12,
    history_hours=48,
    llm_history_max_documents=16,
    llm_history_max_characters=16_000,
    briefing_max_characters=3500,
    greeting_hour=8,
    greeting_minute=0,
    hourly_cron="9-23",
)


def _make_fake_settings(
    *,
    debug: bool = False,
    llm_provider: str = "deepseek",
    llm_base_url: str | None = None,
    publisher: str = "stdout",
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    weather_providers: tuple[str, ...] | None = None,
    qweather_project_id: str | None = None,
    qweather_credential_id: str | None = None,
    qweather_private_key: str | None = None,
    qweather_base_url: str | None = None,
    aqicn_api_token: str | None = None,
    locations: tuple[LocationSpec, ...] = (),
) -> Settings:
    return replace(
        _DEFAULT_SETTINGS,
        debug=debug,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        publisher=publisher,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        weather_providers=weather_providers,
        qweather_project_id=qweather_project_id,
        qweather_credential_id=qweather_credential_id,
        qweather_private_key=qweather_private_key,
        qweather_base_url=qweather_base_url,
        aqicn_api_token=aqicn_api_token,
        locations=locations,
    )


@pytest.fixture
async def async_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


async def test_run_skips_and_logs_when_enforce_window_outside_schedule(monkeypatch, capsys) -> None:
    from unittest.mock import patch

    settings = _make_fake_settings(debug=False)
    tz = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 3, tz=tz)

    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda v, t: now)
        monkeypatch.setattr("weather_briefing.cli._in_schedule", lambda k, n, s: False)
        with patch.object(Settings, "from_env", classmethod(lambda cls: settings)):
            await run("briefing", enforce_window=True)

        stderr = capsys.readouterr().err
        assert "Skipping delayed briefing run outside configured local-time window" in stderr
        assert "[INFO] weather_briefing:" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


@pytest.mark.parametrize("debug", (False, True))
async def test_run_continues_when_runtime_diagnostics_are_unavailable(monkeypatch, capsys, debug: bool) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from weather_briefing.models import ResolvedLocation

    tz = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 8, tz=tz)
    location = ResolvedLocation("test", "Test City", 39.9, 116.3, "CN", "Beijing", tz.name, True)
    settings = _make_fake_settings(
        debug=debug,
        publisher="stdout",
        locations=(LocationSpec(id="test", name="Test City"),),
    )

    monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda v, t: now)
    monkeypatch.setattr("weather_briefing.cli._in_schedule", lambda k, n, s: True)

    def delivery_without_diagnostics(s: object, c: object, diagnostics: object) -> None:
        assert diagnostics is None

    monkeypatch.setattr("weather_briefing.cli._delivery_provider", delivery_without_diagnostics)
    llm_provider = _ClosableLLMProviderStub()
    monkeypatch.setattr("weather_briefing.cli._llm_provider", lambda s, d: llm_provider)
    monkeypatch.setattr("weather_briefing.cli._weather_context_provider", lambda s, c, loc: None)
    backfills: list[tuple[Path, tuple[LocationSpec, ...], tuple[ResolvedLocation, ...]]] = []

    def record_backfill(
        path: Path,
        configured: tuple[LocationSpec, ...],
        resolved: tuple[ResolvedLocation, ...],
    ) -> bool:
        backfills.append((path, configured, resolved))
        return True

    monkeypatch.setattr("weather_briefing.cli.backfill_location_fields", record_backfill)

    class FakeResolver:
        async def resolve_with_metadata(self, loc: object) -> object:
            return SimpleNamespace(location=location, from_cache=True)

    monkeypatch.setattr("weather_briefing.cli.CachedLocationResolver", lambda *a, **kw: FakeResolver())

    class FakeState:
        def __enter__(self) -> "FakeState":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("weather_briefing.cli.SQLiteStateStore", lambda p: FakeState())

    def unavailable_diagnostics(path: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("weather_briefing.cli.SQLiteRuntimeDiagnostics", unavailable_diagnostics)

    async def fake_service_run(kind: str, n: object, **kwargs: object) -> str:
        return "published body"

    monkeypatch.setattr("weather_briefing.cli.BriefingService", lambda *a, **kw: SimpleNamespace(run=fake_service_run))

    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        with patch.object(Settings, "from_env", classmethod(lambda cls: settings)):
            await run("briefing", enforce_window=False)

        stderr = capsys.readouterr().err
        assert "Starting briefing run" in stderr
        assert "Runtime diagnostics unavailable; continuing without sensitive rendered text logging" in stderr
        assert "Resolving 1 location(s)" in stderr
        assert "Saved missing resolved fields to the location configuration" in stderr
        assert "Processing location test" in stderr
        assert ("Location test display name: Test City" in stderr) is debug
        assert "briefing published (14 characters)" in stderr
        assert llm_provider.closed
        assert backfills == [(settings.locations_path, settings.locations, (location,))]
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


async def test_run_sends_alert_for_precision_reduced_location(monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    tz = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 8, tz=tz)
    location = ResolvedLocation(
        "test",
        "Test City",
        39.9,
        116.3,
        "CN",
        "Beijing",
        tz.name,
        True,
        precision_reduced=True,
        matched_name="Matched City",
    )
    settings = _make_fake_settings(
        debug=False,
        publisher="stdout",
        locations=(LocationSpec(id="test", name="Test City"),),
    )

    alerts: list[tuple[str, str]] = []
    events: list[str] = []

    class AlertDelivery:
        async def publish_alert(self, title: str, body: str) -> None:
            events.append("alert")
            alerts.append((title, body))

    def record_backfill(*args: object) -> bool:
        events.append("backfill")
        return False

    monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda v, t: now)
    monkeypatch.setattr("weather_briefing.cli._in_schedule", lambda k, n, s: True)
    monkeypatch.setattr("weather_briefing.cli._delivery_provider", lambda s, c, d: AlertDelivery())
    monkeypatch.setattr("weather_briefing.cli._llm_provider", lambda s, d: _ClosableLLMProviderStub())
    monkeypatch.setattr("weather_briefing.cli._weather_context_provider", lambda s, c, loc: None)
    monkeypatch.setattr("weather_briefing.cli.backfill_location_fields", record_backfill)

    class FakeResolver:
        async def resolve_with_metadata(self, loc: object) -> object:
            return SimpleNamespace(location=location, from_cache=False)

    monkeypatch.setattr("weather_briefing.cli.CachedLocationResolver", lambda *a, **kw: FakeResolver())

    class FakeState:
        def __enter__(self) -> "FakeState":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("weather_briefing.cli.SQLiteStateStore", lambda p: FakeState())
    monkeypatch.setattr("weather_briefing.cli.SQLiteRuntimeDiagnostics", lambda p: FakeState())

    async def fake_service_run(kind: str, n: object, **kwargs: object) -> str:
        return "published body"

    monkeypatch.setattr("weather_briefing.cli.BriefingService", lambda *a, **kw: SimpleNamespace(run=fake_service_run))

    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        with patch.object(Settings, "from_env", classmethod(lambda cls: settings)):
            await run("briefing", enforce_window=False)

    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)

    assert len(alerts) == 1
    assert alerts[0][0] == "Location match requires confirmation"
    assert "Confirm that this location is correct" in alerts[0][1]
    assert events == ["backfill", "alert"]


async def test_run_logs_skipped_when_no_content(monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    tz = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 8, tz=tz)
    location = ResolvedLocation("test", "Test City", 39.9, 116.3, "CN", "Beijing", tz.name, True)
    settings = _make_fake_settings(
        debug=False,
        publisher="stdout",
        locations=(LocationSpec(id="test", name="Test City"),),
    )

    monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda v, t: now)
    monkeypatch.setattr("weather_briefing.cli._in_schedule", lambda k, n, s: True)
    monkeypatch.setattr("weather_briefing.cli._delivery_provider", lambda s, c, d: None)
    monkeypatch.setattr("weather_briefing.cli._llm_provider", lambda s, d: _ClosableLLMProviderStub())
    monkeypatch.setattr("weather_briefing.cli._weather_context_provider", lambda s, c, loc: None)
    monkeypatch.setattr("weather_briefing.cli.backfill_location_fields", lambda *args: False)

    class FakeResolver:
        async def resolve_with_metadata(self, loc: object) -> object:
            return SimpleNamespace(location=location, from_cache=True)

    monkeypatch.setattr("weather_briefing.cli.CachedLocationResolver", lambda *a, **kw: FakeResolver())

    class FakeState:
        def __enter__(self) -> "FakeState":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("weather_briefing.cli.SQLiteStateStore", lambda p: FakeState())
    monkeypatch.setattr("weather_briefing.cli.SQLiteRuntimeDiagnostics", lambda p: FakeState())

    async def fake_service_run(kind: str, n: object, **kwargs: object) -> str | None:
        return None

    monkeypatch.setattr("weather_briefing.cli.BriefingService", lambda *a, **kw: SimpleNamespace(run=fake_service_run))

    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    try:
        _LOGGER.handlers.clear()
        logging.root.handlers.clear()

        with patch.object(Settings, "from_env", classmethod(lambda cls: settings)):
            await run("briefing", enforce_window=False)

        stderr = capsys.readouterr().err
        assert "briefing skipped (no content)" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


class TestLLMProvider:
    async def test_deepseek_with_custom_base_url(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        sdk_client = SimpleNamespace()
        monkeypatch.setattr(
            "weather_briefing.llm.any_llm.create_any_llm_provider",
            lambda *args, **kwargs: calls.append((args, kwargs)) or sdk_client,
        )
        settings = _make_fake_settings(
            llm_provider="deepseek",
            llm_base_url="https://custom.example.invalid",
        )
        with SQLiteRuntimeDiagnostics(tmp_path / "diagnostics.db") as diagnostics:
            provider = _llm_provider(settings, diagnostics)
            assert provider is sdk_client
            assert calls == [
                (
                    ("deepseek", "m", 8192),
                    {
                        "api_key": "k",
                        "api_base": "https://custom.example.invalid",
                        "diagnostics": diagnostics,
                    },
                )
            ]

    async def test_deepseek_without_base_url(self, monkeypatch) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            "weather_briefing.llm.any_llm.create_any_llm_provider",
            lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(),
        )
        settings = _make_fake_settings(llm_provider="deepseek", llm_base_url=None)
        provider = _llm_provider(settings)
        assert provider is not None
        assert calls[0][0][:3] == ("deepseek", "m", 8192)
        assert calls[0][1]["api_base"] is None

    async def test_arbitrary_any_llm_provider_is_forwarded(self, monkeypatch) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            "weather_briefing.llm.any_llm.create_any_llm_provider",
            lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(),
        )
        settings = replace(_make_fake_settings(llm_provider="mistral"), api_key=None, llm_base_url=None)
        provider = _llm_provider(settings)
        assert provider is not None
        assert calls[0][0][:3] == ("mistral", "m", 8192)
        assert calls[0][1] == {
            "api_key": None,
            "api_base": None,
            "diagnostics": None,
        }


class TestDeliveryProvider:
    async def test_stdout(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(publisher="stdout")
        provider = _delivery_provider(settings, async_client)
        assert provider.renderer is not None
        assert provider.publisher is not None

    async def test_telegram_missing_config(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(publisher="telegram", telegram_bot_token=None)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            _delivery_provider(settings, async_client)

    async def test_telegram_with_config(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(
            publisher="telegram",
            telegram_bot_token="test-token",
            telegram_chat_id="test-chat",
        )
        provider = _delivery_provider(settings, async_client)
        assert provider.single_message_limit == 4096

    async def test_unsupported_publisher(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(publisher="unsupported")
        with pytest.raises(ValueError, match="Unsupported publisher"):
            _delivery_provider(settings, async_client)


class TestWeatherContextProvider:
    async def test_qweather_not_configured_skips_when_auto(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(
            weather_providers=None,
            qweather_project_id=None,
            qweather_credential_id=None,
            qweather_private_key=None,
            qweather_base_url=None,
        )
        location = ResolvedLocation("test", "Test", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)
        provider = _weather_context_provider(settings, async_client, location)
        assert provider.weather_metadata.provider_id == "open-meteo"
        assert provider.weather_metadata.supports(CapabilityName.ALLERGEN)
        assert not provider.weather_metadata.supports(CapabilityName.LIFESTYLE)

    async def test_qweather_explicit_not_configured_raises(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(
            weather_providers=("qweather",),
            qweather_project_id=None,
            qweather_credential_id=None,
            qweather_private_key=None,
            qweather_base_url=None,
        )
        location = ResolvedLocation("test", "Test", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)
        with pytest.raises(ValueError, match="JWT configuration"):
            _weather_context_provider(settings, async_client, location)

    async def test_single_provider_bypasses_fallback(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(
            weather_providers=None,
        )
        location = ResolvedLocation("test", "Test", 40.7, -74.0, "US", "NY", "America/New_York", False)
        provider = _weather_context_provider(settings, async_client, location)
        assert provider is not None

    @pytest.mark.parametrize(
        ("country_code", "office_code", "reason"),
        (("JP", None, "missing-jma-office-code"), ("US", "130000", "known-non-japan-country")),
    )
    async def test_unavailable_jma_skips_explicit_supplement(
        self,
        async_client: httpx.AsyncClient,
        caplog,
        country_code: str,
        office_code: str | None,
        reason: str,
    ) -> None:
        settings = _make_fake_settings(weather_providers=("open-meteo", "jma-jp"))
        location = ResolvedLocation(
            "test",
            "Test",
            1.0,
            1.0,
            country_code,
            None,
            "Asia/Tokyo",
            False,
            jma_office_code=office_code,
        )

        with caplog.at_level(logging.WARNING, logger="weather_briefing"):
            provider = _weather_context_provider(settings, async_client, location)

        assert provider.weather_metadata.provider_id == "open-meteo"
        assert provider.supplements == ()
        assert provider.supplement_metadata == ()
        assert f"Skipping explicit JMA provider reason={reason}" in caplog.text

    async def test_missing_jma_office_rejects_explicit_jma_only(self, async_client: httpx.AsyncClient) -> None:
        settings = _make_fake_settings(weather_providers=("jma-jp",))
        location = ResolvedLocation("test", "Test", 1.0, 1.0, "JP", None, "Asia/Tokyo", False)

        with pytest.raises(ValueError, match="No configured weather provider"):
            _weather_context_provider(settings, async_client, location)

    @pytest.mark.parametrize(
        ("country_code", "reason"),
        ((None, "missing-country-code"), ("JP", "known-non-singapore-country")),
    )
    async def test_unavailable_nea_skips_explicit_supplement(
        self,
        async_client: httpx.AsyncClient,
        caplog,
        country_code: str | None,
        reason: str,
    ) -> None:
        settings = _make_fake_settings(weather_providers=("open-meteo", "nea-sg"))
        location = ResolvedLocation("test", "Test", 1.0, 1.0, country_code, None, None, False)

        with caplog.at_level(logging.WARNING, logger="weather_briefing"):
            provider = _weather_context_provider(settings, async_client, location)

        assert provider.weather_metadata.provider_id == "open-meteo"
        assert provider.supplements == ()
        assert provider.supplement_metadata == ()
        assert f"Skipping explicit NEA provider reason={reason}" in caplog.text

    async def test_qweather_configured(self, async_client: httpx.AsyncClient, caplog) -> None:
        key = b"fake-private-key-content"
        settings = _make_fake_settings(
            weather_providers=("qweather",),
            qweather_project_id="project",
            qweather_credential_id="credential",
            qweather_private_key=base64.b64encode(key).decode(),
            qweather_base_url="https://qweather.example.invalid",
        )
        location = ResolvedLocation("test", "Test", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)
        with caplog.at_level("INFO", logger="weather_briefing"):
            provider = _weather_context_provider(settings, async_client, location)
        assert provider.weather_metadata.provider_id == "qweather"
        assert provider.weather_metadata.supports(CapabilityName.LIFESTYLE)
        assert not provider.weather_metadata.supports(CapabilityName.ALLERGEN)
        assert "Weather provider order providers=qweather" in caplog.text
        assert "location=test" not in caplog.text

    async def test_fallback_metadata_only_claims_common_capabilities(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        key = b"fake-private-key-content"
        settings = _make_fake_settings(
            weather_providers=("qweather", "open-meteo"),
            qweather_project_id="project",
            qweather_credential_id="credential",
            qweather_private_key=base64.b64encode(key).decode(),
            qweather_base_url="https://qweather.example.invalid",
        )
        location = ResolvedLocation("test", "Test", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)

        metadata = _weather_context_provider(settings, async_client, location).weather_metadata

        assert metadata.provider_id == "weather-composite"
        assert metadata.capabilities == frozenset(
            {
                CapabilityName.WEATHER,
                CapabilityName.AIR_QUALITY,
            }
        )


def test_weather_provider_metadata_rejects_unregistered_provider() -> None:
    with pytest.raises(ValueError, match="has no capability metadata"):
        _weather_provider_metadata(("unregistered",))


async def test_no_weather_provider_available(monkeypatch, async_client: httpx.AsyncClient) -> None:
    monkeypatch.setattr(
        "weather_briefing.config.environment.weather_providers_for",
        lambda *_: ("qweather",),
    )
    settings = _make_fake_settings(
        weather_providers=None,
        qweather_project_id=None,
        qweather_credential_id=None,
        qweather_private_key=None,
        qweather_base_url=None,
    )
    location = ResolvedLocation("test", "Test", 39.9, 116.3, "CN", "Beijing", "Asia/Shanghai", True)
    with pytest.raises(ValueError, match="No configured weather provider"):
        _weather_context_provider(settings, async_client, location)


def test_qweather_is_configured_all_fields() -> None:
    settings = _make_fake_settings(
        qweather_project_id="p",
        qweather_credential_id="c",
        qweather_private_key="k",
        qweather_base_url="https://example.invalid",
    )
    assert _qweather_is_configured(settings)


def test_qweather_is_configured_missing_field() -> None:
    settings = _make_fake_settings(
        qweather_project_id=None,
        qweather_credential_id="c",
        qweather_private_key="k",
        qweather_base_url="https://example.invalid",
    )
    assert not _qweather_is_configured(settings)


async def test_build_weather_provider_unsupported(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings()
    with pytest.raises(ValueError, match="Unsupported weather provider"):
        _build_weather_provider("unknown", settings, async_client)


async def test_build_open_meteo_returns_provider(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings()
    provider = _build_open_meteo(settings, async_client)
    assert provider is not None


async def test_build_qweather_returns_provider(async_client: httpx.AsyncClient) -> None:
    import base64 as b64

    settings = _make_fake_settings(
        qweather_project_id="project",
        qweather_credential_id="credential",
        qweather_private_key=b64.b64encode(b"fake-private-key-content").decode(),
        qweather_base_url="https://qweather.example.invalid",
    )
    provider = _build_qweather(settings, async_client)
    assert isinstance(provider, QWeatherProvider)
    assert provider.output_language == "en"


async def test_build_weather_provider_passes_location_language_to_qweather(async_client: httpx.AsyncClient) -> None:
    import base64 as b64

    settings = _make_fake_settings(
        qweather_project_id="project",
        qweather_credential_id="credential",
        qweather_private_key=b64.b64encode(b"fake-private-key-content").decode(),
        qweather_base_url="https://qweather.example.invalid",
    )

    provider = _build_weather_provider("qweather", settings, async_client, "ja")

    assert isinstance(provider, QWeatherProvider)
    assert provider.output_language == "ja"


async def test_local_provider_registry_builders(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings()

    assert _build_nea(settings, async_client) is not None
    with pytest.raises(ValueError, match="jma_office_code"):
        _build_jma(settings, async_client)


async def test_explicit_local_provider_can_be_used_as_primary(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings(weather_providers=("nea-sg",))
    location = ResolvedLocation("sg", "Singapore", 1.3, 103.8, "SG", None, "Asia/Singapore", False)

    provider = _weather_context_provider(settings, async_client, location)

    assert provider.weather_metadata.provider_id == "nea-sg"
    assert provider.weather_metadata.language_support.default == "en"


async def test_explicit_jma_provider_can_be_used_as_primary() -> None:
    settings = _make_fake_settings(weather_providers=("jma-jp",))
    location = ResolvedLocation(
        "jp",
        "Tokyo",
        1.0,
        1.0,
        "JP",
        None,
        "Asia/Tokyo",
        False,
        jma_office_code="130000",
    )

    payload = [
        {
            "reportDatetime": "2026-07-20T05:00:00+09:00",
            "timeSeries": [
                {
                    "timeDefines": ["2026-07-20T00:00:00+09:00", "2026-07-21T00:00:00+09:00"],
                    "areas": [
                        {"area": {"name": "東京都"}, "weathers": ["晴れ", "曇り"], "winds": ["南の風", "東の風"]}
                    ],
                }
            ],
        }
    ]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = _weather_context_provider(settings, client, location)
        snapshot = await provider.fetch_for_date(1.0, 1.0, pendulum.date(2026, 7, 21))

    assert provider.weather_metadata.provider_id == "jma-jp"
    assert provider.weather_metadata.language_support.default == "ja"
    assert snapshot.weather_forecast == ("東京都: 曇り", "東京都の風: 東の風")


async def test_build_jma_returns_provider(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings()

    provider = _build_weather_provider("jma-jp", settings, async_client, jma_office_code="130000")

    assert provider is not None


async def test_build_qweather_missing_config_raises(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings(qweather_project_id=None)
    with pytest.raises(ValueError, match="QWeather provider requires"):
        _build_qweather(settings, async_client)


async def test_aqicn_provider_returns_none_when_no_token(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings(aqicn_api_token=None)
    assert _aqicn_provider(settings, async_client) is None


async def test_aqicn_provider_returns_instance_when_token_set(async_client: httpx.AsyncClient) -> None:
    settings = _make_fake_settings(aqicn_api_token="test-token")
    provider = _aqicn_provider(settings, async_client)
    assert provider is not None


def test_parse_run_time_returns_now_when_value_is_none(monkeypatch) -> None:
    tz = pendulum.timezone("Asia/Shanghai")
    result = _parse_run_time(None, tz)
    assert result.timezone_name == "Asia/Shanghai"


@pytest.mark.parametrize("name", tuple(WeatherProviderName))
async def test_build_weather_provider_dispatch_is_exhaustive(
    async_client: httpx.AsyncClient,
    name: WeatherProviderName,
) -> None:
    settings = _make_fake_settings(
        qweather_project_id="project",
        qweather_credential_id="credential",
        qweather_private_key=base64.b64encode(b"fake-private-key-content").decode(),
        qweather_base_url="https://qweather.example.invalid",
    )

    provider = _build_weather_provider(name, settings, async_client, jma_office_code="130000")

    assert provider is not None


def test_publisher_builders_cover_declared_configuration_names() -> None:
    assert set(PUBLISHER_BUILDERS) == set(PublisherName)


async def test_daemon_schedules_forecast_and_briefing_without_running_either_immediately(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    jobs: list[tuple[object, tuple[object, ...]]] = []
    state_path = tmp_path / "weather.sqlite3"
    lock_is_held = False

    @asynccontextmanager
    async def record_state_lock(path: Path) -> AsyncIterator[None]:
        nonlocal lock_is_held
        assert path == state_path
        lock_is_held = True
        try:
            yield
        finally:
            lock_is_held = False

    class FakeEvent:
        async def wait(self) -> None:
            pass

    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))
    monkeypatch.setattr("weather_briefing.persistence.locking.serialized_state_run", record_state_lock)
    monkeypatch.setattr(
        "weather_briefing.cli.AsyncIOScheduler",
        lambda **kw: SimpleNamespace(
            add_job=lambda function, trigger, *, args, max_instances: jobs.append((function, tuple(args))),
            start=lambda: None,
        ),
    )
    monkeypatch.setattr("weather_briefing.cli.asyncio.Event", FakeEvent)

    settings = replace(_make_fake_settings(), state_path=state_path)

    def load_settings(_cls: type[Settings]) -> Settings:
        assert lock_is_held
        return settings

    with patch.object(Settings, "from_env", classmethod(load_settings)):
        await daemon()

    assert jobs == [(run, ("forecast", True)), (run, ("briefing", True))]
    assert not lock_is_held


def test_main_calls_daemon_correctly(monkeypatch) -> None:
    calls = 0

    async def fake_daemon() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("weather_briefing.cli.daemon", fake_daemon)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "daemon"])

    main()
    assert calls == 1


def test_main_rejects_a_second_daemon_with_a_clear_error(monkeypatch, tmp_path: Path, caplog) -> None:
    state_path = tmp_path / "weather.sqlite3"
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))
    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "daemon"])

    with daemon_state_owner(state_path), pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    assert "Another weather-briefing daemon is already using the configured state directory" in caplog.text


def test_daemon_parser_rejects_run_now() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["daemon", "--run-now"])


def test_main_allows_manual_run_while_daemon_owns_state(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, bool, str | None]] = []
    state_path = tmp_path / "weather.sqlite3"

    async def fake_run(
        kind: str,
        enforce_window: bool,
        at: str | None = None,
        *,
        forecast_date: str | None = None,
        run_now: bool = False,
    ) -> None:
        calls.append((kind, enforce_window, forecast_date or at))
        assert not run_now

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("weather_briefing.cli.run", fake_run)
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))
    monkeypatch.setattr("sys.argv", ["weather-briefing", "run", "forecast", "--at", "2026-07-14T08:00:00+08:00"])

    with daemon_state_owner(state_path):
        main()
    assert calls == [("forecast", False, "2026-07-14T08:00:00+08:00")]


def test_main_passes_forecast_date(monkeypatch) -> None:
    calls: list[tuple[str, bool, str | None, bool]] = []

    async def fake_run(
        kind: str,
        enforce_window: bool,
        at: str | None = None,
        *,
        forecast_date: str | None = None,
        run_now: bool = False,
    ) -> None:
        assert at is None
        calls.append((kind, enforce_window, forecast_date, run_now))

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli._configure_logging", lambda *, debug: None)
    monkeypatch.setattr("weather_briefing.cli.run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["weather-briefing", "run", "forecast", "--date", "2026-07-11", "--run-now"],
    )

    main()

    assert calls == [("forecast", False, "2026-07-11", True)]
