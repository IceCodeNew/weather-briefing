from pathlib import Path

import pytest

from weather_briefing.config import ConfigurationError, Settings, weather_providers_for
from weather_briefing.models import ResolvedLocation


def _required_environment(monkeypatch) -> None:
    values = {
        "DEEPSEEK_API_KEY": "test-key",
        "DEEPSEEK_MODEL": "test-model",
        "BRIEFING_LOCATIONS_FILE": str(Path(__file__).parents[1] / "locations.example.json"),
        "RSS_SOURCES_FILE": str(Path(__file__).parents[1] / "rss-sources.example.json"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _resolved_location(*, mainland: bool) -> ResolvedLocation:
    return ResolvedLocation(
        "test",
        "test-region",
        39.911389,
        116.380556,
        "CN" if mainland else "US",
        "Beijing" if mainland else "California",
        "Asia/Shanghai" if mainland else "America/Los_Angeles",
        mainland,
    )


def test_mainland_weather_providers_default_to_qweather_then_open_meteo(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.delenv("WEATHER_PROVIDERS", raising=False)

    settings = Settings.from_env()

    assert settings.weather_providers is None
    assert weather_providers_for(_resolved_location(mainland=True), None) == (
        "qweather",
        "open-meteo",
    )
    assert [feed.id for feed in settings.feeds] == ["authority-weather"]
    assert settings.llm_provider == "deepseek"
    assert settings.llm_base_url is None
    assert settings.qweather_index_types == ("1", "3", "6", "8", "15")
    assert settings.qweather_jwt_lifetime_seconds == 900


def test_weather_provider_order_can_be_configured(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("WEATHER_PROVIDERS", " open-meteo, qweather ")

    settings = Settings.from_env()

    assert weather_providers_for(_resolved_location(mainland=True), settings.weather_providers) == (
        "open-meteo",
        "qweather",
    )


def test_non_mainland_weather_providers_default_to_open_meteo_only(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.delenv("WEATHER_PROVIDERS", raising=False)

    settings = Settings.from_env()

    assert weather_providers_for(_resolved_location(mainland=False), settings.weather_providers) == ("open-meteo",)


def test_optional_rss_sources_are_loaded_from_named_file(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        '[{"id":"test","name":"Test","url":"https://example.invalid/feed"}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    settings = Settings.from_env()

    assert settings.rss_sources_path == source_file
    assert [feed.id for feed in settings.feeds] == ["test"]


def test_location_file_supports_multiple_places_and_optional_coordinates(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    location_file = tmp_path / "locations.json"
    location_file.write_text(
        '[{"id":"beijing","name":"北京市西城区中南海"},'
        '{"id":"beijing-fixed","name":"北京市西城区中南海",'
        '"latitude":39.911389,"longitude":116.380556}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

    settings = Settings.from_env()

    assert [location.id for location in settings.locations] == ["beijing", "beijing-fixed"]
    assert settings.locations[0].latitude is None
    assert settings.locations[1].longitude == 116.380556


def test_rss_source_location_ids_must_reference_configured_locations(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    location_file = tmp_path / "locations.json"
    location_file.write_text('[{"id":"beijing","name":"Beijing"}]', encoding="utf-8")
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        '[{"id":"feed","name":"Feed","url":"https://example.invalid/feed","location_ids":["shanghai"]}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(ConfigurationError, match="unknown location ids: shanghai"):
        Settings.from_env()


def test_qweather_jwt_lifetime_cannot_exceed_official_limit(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("QWEATHER_JWT_LIFETIME_SECONDS", "86401")

    with pytest.raises(ConfigurationError, match="cannot exceed 86400"):
        Settings.from_env()


def test_qweather_private_key_strips_surrounding_quotes(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("QWEATHER_PRIVATE_KEY", "'LS0tLS0='")

    settings = Settings.from_env()

    assert settings.qweather_private_key == "LS0tLS0="


def test_qweather_private_key_double_quotes_are_also_stripped(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("QWEATHER_PRIVATE_KEY", '"LS0tLS0="')

    settings = Settings.from_env()

    assert settings.qweather_private_key == "LS0tLS0="


def test_qweather_private_key_without_quotes_is_unchanged(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("QWEATHER_PRIVATE_KEY", "LS0tLS0=")

    settings = Settings.from_env()

    assert settings.qweather_private_key == "LS0tLS0="


def test_env_api_key_with_quotes_is_cleaned(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "'sk-quoted-key'")

    settings = Settings.from_env()

    assert settings.api_key == "sk-quoted-key"


def test_env_url_with_quotes_is_cleaned(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("GEOCODING_API_KEY", "'geocoding-key'")
    monkeypatch.setenv("GEOCODING_BASE_URL", "'https://geocoding.example.invalid/'")

    settings = Settings.from_env()

    assert settings.geocoding_api_key == "geocoding-key"
    assert settings.geocoding_base_url == "https://geocoding.example.invalid"


def test_env_numeric_with_quotes_is_parsed(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("RSS_MAX_ATTEMPTS", "'5'")

    settings = Settings.from_env()

    assert settings.rss_max_attempts == 5


def test_env_provider_with_quotes_is_cleaned(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "'deepseek'")

    settings = Settings.from_env()

    assert settings.llm_provider == "deepseek"


def test_env_optional_with_quoted_empty_yields_none(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("QWEATHER_PROJECT_ID", "''")

    settings = Settings.from_env()

    assert settings.qweather_project_id is None


@pytest.mark.parametrize("value", ("sk-key'", 'sk-key"', "'sk-key\""))
def test_env_value_with_unmatched_quotes_is_unchanged(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", value)

    settings = Settings.from_env()

    assert settings.api_key == value


@pytest.mark.parametrize(
    "name",
    (
        "HTTP_TIMEOUT_SECONDS",
        "RSS_STALE_HOURS",
        "TASK_FAILURE_THRESHOLD",
        "WARNING_RETENTION_HOURS",
        "HISTORY_HOURS",
    ),
)
def test_positive_operational_settings_reject_zero(monkeypatch, name: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv(name, "0")

    with pytest.raises(ConfigurationError, match="greater than zero"):
        Settings.from_env()


def test_generic_llm_provider_uses_generic_configuration(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_API_KEY", "generic-key")
    monkeypatch.setenv("LLM_MODEL", "generic-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://compatible.example.invalid/v1")

    settings = Settings.from_env()

    assert settings.api_key == "generic-key"
    assert settings.llm_provider == "openai-compatible"
    assert settings.llm_model == "generic-model"
    assert settings.llm_base_url == "https://compatible.example.invalid/v1"


class TestScheduleSettings:
    def test_defaults(self, monkeypatch) -> None:
        _required_environment(monkeypatch)

        settings = Settings.from_env()

        assert settings.greeting_hour == 8
        assert settings.greeting_minute == 0
        assert settings.hourly_cron == "9-23"

    def test_custom_greeting(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("GREETING_HOUR", "7")
        monkeypatch.setenv("GREETING_MINUTE", "30")

        settings = Settings.from_env()

        assert settings.greeting_hour == 7
        assert settings.greeting_minute == 30

    def test_custom_hourly_cron(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("BRIEFING_CRON", "8-20")

        settings = Settings.from_env()

        assert settings.hourly_cron == "8-20"

    def test_empty_briefing_cron_rejected(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("BRIEFING_CRON", "")

        with pytest.raises(ConfigurationError, match="BRIEFING_CRON must not be empty"):
            Settings.from_env()

    @pytest.mark.parametrize("value", ("foo", "24", "9-", "9 - 18"))
    def test_invalid_briefing_cron_rejected(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("BRIEFING_CRON", value)

        with pytest.raises(ConfigurationError, match="valid APScheduler hour expression"):
            Settings.from_env()

    @pytest.mark.parametrize("value", ("25", "-1", "abc"))
    def test_greeting_hour_out_of_bounds_rejected(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("GREETING_HOUR", value)

        with pytest.raises(ConfigurationError):
            Settings.from_env()

    @pytest.mark.parametrize("value", ("60", "-1", "abc"))
    def test_greeting_minute_out_of_bounds_rejected(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("GREETING_MINUTE", value)

        with pytest.raises(ConfigurationError):
            Settings.from_env()
