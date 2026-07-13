import logging
from pathlib import Path

import pendulum
import pytest

from weather_briefing.cli import (
    _LOGGER,
    _configure_logging,
    _hour_in_cron,
    _in_schedule,
    _location_state_path,
    _parse_run_time,
    _precision_reduction_notice,
    build_parser,
    main,
    run,
)
from weather_briefing.config import Settings
from weather_briefing.models import ResolvedLocation


def test_configure_logging_is_idempotent_and_updates_level() -> None:
    original_handlers = _LOGGER.handlers[:]
    original_level = _LOGGER.level
    original_propagate = _LOGGER.propagate
    original_root_handlers = logging.root.handlers[:]
    original_root_level = logging.root.level
    original_httpx_level = logging.getLogger("httpx").level
    original_httpcore_level = logging.getLogger("httpcore").level
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
        assert logging.root.level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)
        logging.getLogger("httpx").setLevel(original_httpx_level)
        logging.getLogger("httpcore").setLevel(original_httpcore_level)


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
    assert "确认" in notice


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
    def test_daily_matches_greeting_hour(self, monkeypatch) -> None:
        monkeypatch.setenv("GREETING_HOUR", "7")
        monkeypatch.setenv("GREETING_MINUTE", "30")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_match = pendulum.datetime(2026, 7, 14, 7, 0, tz=settings.timezone)
        now_no_match = pendulum.datetime(2026, 7, 14, 8, 0, tz=settings.timezone)

        assert _in_schedule("daily", now_match, settings)
        assert not _in_schedule("daily", now_no_match, settings)

    def test_hourly_matches_cron_range(self, monkeypatch) -> None:
        monkeypatch.setenv("BRIEFING_CRON", "10-18")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(Path(__file__).parents[1] / "locations.example.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(Path(__file__).parents[1] / "rss-sources.example.json"))

        settings = Settings.from_env()
        now_in = pendulum.datetime(2026, 7, 14, 10, 0, tz=settings.timezone)
        now_out = pendulum.datetime(2026, 7, 14, 9, 0, tz=settings.timezone)

        assert _in_schedule("hourly", now_in, settings)
        assert not _in_schedule("hourly", now_out, settings)


def test_version_flag() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])


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

    async def fail_daemon(run_now: bool = False) -> None:
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

    async def fail_run(kind: str, enforce_window: bool, at: str | None) -> None:
        assert kind == "hourly"
        assert not enforce_window
        assert at is None
        assert len(_LOGGER.handlers) == 1
        assert _LOGGER.level == logging.INFO
        raise RuntimeError("boom")

    monkeypatch.setattr("weather_briefing.cli.load_dotenv", lambda *, override: True)
    monkeypatch.setattr("weather_briefing.cli.run", fail_run)
    monkeypatch.setattr("sys.argv", ["weather-briefing", "run", "hourly"])
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


def _make_fake_settings(**overrides: object) -> object:
    from types import SimpleNamespace

    tz = pendulum.timezone("Asia/Shanghai")
    defaults: dict[str, object] = {
        "debug": False,
        "timezone": tz,
        "api_key": "k",
        "llm_provider": "deepseek",
        "llm_model": "m",
        "llm_base_url": None,
        "llm_max_output_tokens": 8192,
        "llm_max_attempts": 3,
        "http_timeout_seconds": 30,
        "locations": (),
        "locations_path": Path("locations.json"),
        "geocoding_base_url": "https://geo.example.com",
        "geocoding_api_key": None,
        "nominatim_base_url": "https://nominatim.example.com",
        "geocoding_user_agent": "test",
        "geocoding_cache_path": Path("state/geocoding.json"),
        "feeds": (),
        "context_sources": (),
        "weather_providers": None,
        "qweather_project_id": None,
        "qweather_credential_id": None,
        "qweather_private_key": None,
        "qweather_jwt_lifetime_seconds": 900,
        "qweather_base_url": None,
        "qweather_index_types": (),
        "open_meteo_weather_base_url": "https://weather.example.com",
        "open_meteo_air_quality_base_url": "https://air.example.com",
        "open_meteo_api_key": None,
        "aqicn_api_token": None,
        "aqicn_base_url": "https://aqi.example.com",
        "state_path": Path("state/weather.sqlite3"),
        "publisher": "stdout",
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "rss_max_attempts": 3,
        "rss_retry_min_seconds": 3,
        "rss_retry_max_seconds": 5,
        "rss_stale_hours": 24,
        "task_failure_threshold": 3,
        "warning_retention_hours": 12,
        "history_hours": 48,
        "briefing_max_characters": 3500,
        "greeting_hour": 8,
        "greeting_minute": 0,
        "hourly_cron": "9-23",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeAsyncClient:
    def __init__(self) -> None:
        self.timeout = None

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


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
            await run("hourly", enforce_window=True)

        stderr = capsys.readouterr().err
        assert "Skipping delayed hourly run outside configured local-time window" in stderr
        assert "[INFO] weather_briefing:" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)


async def test_run_logs_start_resolve_and_publish(monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from weather_briefing.models import ResolvedLocation

    tz = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 8, tz=tz)
    location = ResolvedLocation("test", "Test City", 39.9, 116.3, "CN", "Beijing", tz.name, True)
    settings = _make_fake_settings(debug=False, publisher="stdout", locations=(location,))

    monkeypatch.setattr("weather_briefing.cli._parse_run_time", lambda v, t: now)
    monkeypatch.setattr("weather_briefing.cli._in_schedule", lambda k, n, s: True)
    monkeypatch.setattr("weather_briefing.cli._delivery_provider", lambda s, c: None)
    monkeypatch.setattr("weather_briefing.cli._llm_provider", lambda s, c: None)
    monkeypatch.setattr("weather_briefing.cli._weather_context_provider", lambda s, c, loc: None)
    monkeypatch.setattr("weather_briefing.cli.httpx.AsyncClient", lambda **kw: _FakeAsyncClient())

    class FakeResolver:
        async def resolve_with_metadata(self, loc: object) -> object:
            return SimpleNamespace(location=loc, from_cache=True)

    monkeypatch.setattr("weather_briefing.cli.CachedLocationResolver", lambda *a, **kw: FakeResolver())

    class FakeState:
        def __enter__(self) -> "FakeState":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("weather_briefing.cli.SQLiteStateStore", lambda p: FakeState())

    async def fake_service_run(kind: str, n: object) -> str:
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
            await run("hourly", enforce_window=False)

        stderr = capsys.readouterr().err
        assert "Starting hourly briefing run" in stderr
        assert "Resolving 1 location(s)" in stderr
        assert "Processing location test (Test City)" in stderr
        assert "briefing published (14 characters)" in stderr
    finally:
        _LOGGER.handlers.clear()
        _LOGGER.handlers.extend(original_handlers)
        _LOGGER.setLevel(original_level)
        _LOGGER.propagate = original_propagate
        logging.root.handlers.clear()
        logging.root.handlers.extend(original_root_handlers)
        logging.root.setLevel(original_root_level)
