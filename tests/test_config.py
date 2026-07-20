import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from weather_briefing.config import ConfigurationError, Settings, weather_providers_for
from weather_briefing.models import ResolvedLocation
from weather_briefing.reference_data import reference_string_tuple


def _required_environment(monkeypatch) -> None:
    values = {
        "DEEPSEEK_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
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
    assert settings.llm_max_attempts == 3
    assert settings.qweather_index_types == ("1", "3", "6", "7", "8", "15")
    assert settings.qweather_jwt_lifetime_seconds == 900
    assert settings.llm_history_max_documents == 8
    assert settings.llm_history_max_characters == 16_000


def test_llm_history_limits_can_be_configured(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_HISTORY_MAX_DOCUMENTS", "6")
    monkeypatch.setenv("LLM_HISTORY_MAX_CHARACTERS", "8000")

    settings = Settings.from_env()

    assert settings.llm_history_max_documents == 6
    assert settings.llm_history_max_characters == 8_000


@pytest.mark.parametrize("value", ("0", "257"))
def test_llm_history_document_limit_rejects_unsafe_values(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_HISTORY_MAX_DOCUMENTS", value)

    with pytest.raises(ConfigurationError, match="LLM_HISTORY_MAX_DOCUMENTS"):
        Settings.from_env()


@pytest.mark.parametrize("value", ("0", "1", "1000001"))
def test_llm_history_character_limit_rejects_unsafe_values(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_HISTORY_MAX_CHARACTERS", value)

    with pytest.raises(ConfigurationError, match="LLM_HISTORY_MAX_CHARACTERS"):
        Settings.from_env()


def test_environment_example_preserves_default_qweather_indices() -> None:
    configured = dotenv_values(Path(__file__).parents[1] / "env.example")["QWEATHER_INDEX_TYPES"]

    assert configured is not None
    assert tuple(item.strip() for item in configured.split(",")) == reference_string_tuple(
        "provider_defaults.json",
        "qweather_lifestyle_index_types",
    )


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


def test_location_summary_language_is_loaded_and_normalized(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text(
        '[{"id":"tokyo","name":"Tokyo","language":"ja-jp"}]',
        encoding="utf-8",
    )
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    settings = Settings.from_env()

    assert settings.locations[0].summary_language == "ja-JP"


def test_location_summary_language_defaults_to_english(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text('[{"id":"singapore","name":"Singapore"}]', encoding="utf-8")
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    settings = Settings.from_env()

    assert settings.locations[0].summary_language == "en"


def test_location_summary_language_rejects_invalid_tag(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text(
        '[{"id":"tokyo","name":"Tokyo","language":"not a language"}]',
        encoding="utf-8",
    )
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    with pytest.raises(ConfigurationError, match="language must be a basic BCP 47-like"):
        Settings.from_env()


def test_location_summary_language_rejects_non_string(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text('[{"id":"tokyo","name":"Tokyo","language":7}]', encoding="utf-8")
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    with pytest.raises(ConfigurationError, match="language must be a basic BCP 47-like"):
        Settings.from_env()


def test_rss_source_requires_public_display_name(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        '[{"id":"test","url":"https://example.invalid/feed"}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(ConfigurationError, match=r"rss-sources\.json\[0\]\.name must be a non-empty string"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("source", "message"),
    (
        ('{"name":"Test","url":"https://example.invalid/feed"}', "id"),
        ('{"id":null,"name":"Test","url":"https://example.invalid/feed"}', "id"),
        ('{"id":"test","name":null,"url":"https://example.invalid/feed"}', "name"),
        ('{"id":"test","name":"Test"}', "url"),
        ('{"id":"test","name":"Test","url":null}', "url"),
    ),
)
def test_rss_source_rejects_missing_or_null_required_fields(
    monkeypatch,
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(f"[{source}]", encoding="utf-8")
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(
        ConfigurationError,
        match=rf"rss-sources\.json\[0\]\.{message} must be a non-empty string",
    ):
        Settings.from_env()


@pytest.mark.parametrize("field", ("id", "name", "url"))
@pytest.mark.parametrize("value", (1, ["value"], {"value": "nested"}))
def test_rss_source_required_fields_reject_non_strings(
    monkeypatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _required_environment(monkeypatch)
    source = {"id": "test", "name": "Test", "url": "https://example.invalid/feed"}
    source[field] = value
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(json.dumps([source]), encoding="utf-8")
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(
        ConfigurationError,
        match=rf"rss-sources\.json\[0\]\.{field} must be a non-empty string",
    ):
        Settings.from_env()


def test_rss_source_treats_null_optional_arrays_as_empty(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        '[{"id":"test","name":"Test","url":"https://example.invalid/feed",'
        '"verbatim_title_patterns":null,"forecast_title_patterns":null,'
        '"content_remove_selectors":null,"content_remove_patterns":null,"location_ids":null}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    feed = Settings.from_env().feeds[0]

    assert feed.verbatim_title_patterns == ()
    assert feed.forecast_title_patterns == ()
    assert feed.content_remove_selectors == ()
    assert feed.content_remove_patterns == ()
    assert feed.location_ids == ()


@pytest.mark.parametrize(
    "field",
    (
        "verbatim_title_patterns",
        "forecast_title_patterns",
        "content_remove_selectors",
        "content_remove_patterns",
        "location_ids",
    ),
)
def test_rss_source_optional_arrays_reject_non_arrays(monkeypatch, tmp_path: Path, field: str) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        json.dumps(
            [
                {
                    "id": "test",
                    "name": "Test",
                    "url": "https://example.invalid/feed",
                    field: "not-an-array",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(ConfigurationError, match=rf"RSS source test field {field} must be a JSON array"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("field", "entry"),
    (
        ("verbatim_title_patterns", ""),
        ("forecast_title_patterns", " "),
        ("content_remove_selectors", 1),
        ("content_remove_patterns", {}),
        ("location_ids", None),
    ),
)
def test_rss_source_optional_arrays_require_non_empty_strings(
    monkeypatch,
    tmp_path: Path,
    field: str,
    entry: object,
) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        json.dumps(
            [
                {
                    "id": "test",
                    "name": "Test",
                    "url": "https://example.invalid/feed",
                    field: [entry],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(
        ConfigurationError,
        match=rf"RSS source test field {field}\[0\] must be a non-empty string",
    ):
        Settings.from_env()


@pytest.mark.parametrize(
    ("field", "entry"),
    (
        ("content_remove_selectors", "["),
        ("content_remove_patterns", "["),
    ),
)
def test_rss_source_cleaning_rules_reject_invalid_syntax(
    monkeypatch,
    tmp_path: Path,
    field: str,
    entry: str,
) -> None:
    _required_environment(monkeypatch)
    source_file = tmp_path / "rss-sources.json"
    source_file.write_text(
        json.dumps(
            [
                {
                    "id": "test",
                    "name": "Test",
                    "url": "https://example.invalid/feed",
                    field: [entry],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

    with pytest.raises(ConfigurationError, match=rf"RSS source test field {field}\[0\] is invalid"):
        Settings.from_env()


def test_location_file_supports_name_coordinates_or_both(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    location_file = tmp_path / "locations.json"
    location_file.write_text(
        '[{"id":"beijing","name":"北京市西城区中南海"},'
        '{"id":"beijing-fixed","name":"北京市西城区中南海",'
        '"latitude":39.911389,"longitude":116.380556},'
        '{"id":"coordinates-only","latitude":39.911389,"longitude":116.380556}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

    settings = Settings.from_env()

    assert [location.id for location in settings.locations] == [
        "beijing",
        "beijing-fixed",
        "coordinates-only",
    ]
    assert settings.locations[0].latitude is None
    assert settings.locations[1].longitude == 116.380556
    assert settings.locations[2].name is None


@pytest.mark.parametrize("field", ("id", "name"))
@pytest.mark.parametrize("value", (1, ["value"], {"value": "nested"}))
def test_location_string_fields_reject_non_strings(
    monkeypatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _required_environment(monkeypatch)
    location = {"id": "test", "name": "Test"}
    location[field] = value
    location_file = tmp_path / "locations.json"
    location_file.write_text(json.dumps([location]), encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

    with pytest.raises(
        ConfigurationError,
        match=rf"locations\.json\[0\]\.{field} must be a non-empty string",
    ):
        Settings.from_env()


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


@pytest.mark.parametrize("value", ("1", "true", "yes", "'true'", '"yes"', "' true '", '" yes "'))
def test_debug_accepts_truthy_values_with_optional_outer_quotes(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEBUG", value)

    assert Settings.from_env().debug


@pytest.mark.parametrize("value", ("", "0", "false", "no", "'false'", '"no"', "' false '", '" no "'))
def test_debug_accepts_false_values_with_optional_outer_quotes(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEBUG", value)

    assert not Settings.from_env().debug


@pytest.mark.parametrize("value", ("tru", "enabled", "2"))
def test_debug_rejects_unknown_values(monkeypatch, value: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEBUG", value)

    with pytest.raises(ConfigurationError, match="DEBUG must be one of"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name",
    (
        "HTTP_TIMEOUT_SECONDS",
        "RSS_FAILURE_THRESHOLD",
        "RSS_STALE_HOURS",
        "WARNING_RETENTION_HOURS",
        "HISTORY_HOURS",
    ),
)
def test_positive_operational_settings_reject_zero(monkeypatch, name: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv(name, "0")

    with pytest.raises(ConfigurationError, match="greater than zero"):
        Settings.from_env()


def test_any_llm_provider_uses_sdk_managed_configuration(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "generic-key")
    monkeypatch.setenv("LLM_MODEL", "generic-model")
    monkeypatch.setenv("MISTRAL_API_BASE", "https://compatible.example.invalid/v1")

    settings = Settings.from_env()

    assert settings.api_key is None
    assert settings.llm_provider == "mistral"
    assert settings.llm_model == "generic-model"
    assert settings.llm_base_url is None


def test_deepseek_model_name_remains_compatible(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.setenv("DEEPSEEK_MODEL", "existing-model")

    settings = Settings.from_env()

    assert settings.llm_model == "existing-model"


def test_deepseek_prefers_any_llm_api_base_name(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://existing.example.invalid")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://sdk.example.invalid")

    settings = Settings.from_env()

    assert settings.llm_base_url == "https://sdk.example.invalid"


def test_deepseek_accepts_any_llm_api_base_name(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://sdk.example.invalid")

    settings = Settings.from_env()

    assert settings.llm_base_url == "https://sdk.example.invalid"


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


class TestConfigErrorPaths:
    def test_missing_required_llm_model_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.delenv("LLM_MODEL")
        monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

        with pytest.raises(ConfigurationError, match="LLM_MODEL"):
            Settings.from_env()

    def test_unsupported_llm_provider_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "unsupported")

        with pytest.raises(ConfigurationError, match="Unsupported LLM_PROVIDER"):
            Settings.from_env()

    def test_llm_provider_without_completion_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "voyage")

        with pytest.raises(ConfigurationError, match="does not support completion"):
            Settings.from_env()

    def test_invalid_float_env_value_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "not-a-number")

        with pytest.raises(ConfigurationError, match="must be a number"):
            Settings.from_env()

    def test_empty_locations_file_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="Configure at least one location"):
            Settings.from_env()

    def test_invalid_location_id_characters_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text('[{"id":"invalid id","name":"Name"}]', encoding="utf-8")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="Location id must use"):
            Settings.from_env()

    def test_duplicate_location_id_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text(
            '[{"id":"beijing","name":"Beijing"},{"id":"beijing","name":"Also Beijing"}]',
            encoding="utf-8",
        )
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="Duplicate location id"):
            Settings.from_env()

    def test_location_without_name_or_coordinates_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text('[{"id":"beijing"}]', encoding="utf-8")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="must provide a name or coordinates"):
            Settings.from_env()

    def test_mismatched_lat_lon_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text(
            '[{"id":"beijing","name":"Beijing","latitude":39.9}]',
            encoding="utf-8",
        )
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="provide both latitude and longitude"):
            Settings.from_env()

    def test_latitude_out_of_range_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text(
            '[{"id":"beijing","name":"Beijing","latitude":95,"longitude":116}]',
            encoding="utf-8",
        )
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="latitude is out of range"):
            Settings.from_env()

    def test_longitude_out_of_range_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text(
            '[{"id":"beijing","name":"Beijing","latitude":39,"longitude":200}]',
            encoding="utf-8",
        )
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="longitude is out of range"):
            Settings.from_env()

    def test_invalid_context_sources_json_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("CONTEXT_SOURCES_JSON", "not-json")

        with pytest.raises(ConfigurationError, match="CONTEXT_SOURCES_JSON must contain valid JSON"):
            Settings.from_env()

    def test_context_sources_not_array_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("CONTEXT_SOURCES_JSON", '{"key":"value"}')

        with pytest.raises(ConfigurationError, match="CONTEXT_SOURCES_JSON must be a JSON array"):
            Settings.from_env()

    def test_context_source_not_object_includes_index_in_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("CONTEXT_SOURCES_JSON", "[1]")

        with pytest.raises(ConfigurationError, match=r"CONTEXT_SOURCES_JSON\[0\] must be a JSON object"):
            Settings.from_env()

    @pytest.mark.parametrize("field", ("id", "name", "url"))
    def test_context_source_requires_named_fields(self, monkeypatch, field: str) -> None:
        _required_environment(monkeypatch)
        source = {"id": "context", "name": "Context", "url": "https://example.invalid/context"}
        del source[field]
        monkeypatch.setenv("CONTEXT_SOURCES_JSON", json.dumps([source]))

        with pytest.raises(ConfigurationError, match=rf"CONTEXT_SOURCES_JSON\[0\]\.{field}"):
            Settings.from_env()

    def test_context_source_accepts_and_strips_required_strings(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv(
            "CONTEXT_SOURCES_JSON",
            '[{"id":" context ","name":" Context ","url":" https://example.invalid/context "}]',
        )

        source = Settings.from_env().context_sources[0]

        assert source.id == "context"
        assert source.name == "Context"
        assert source.url == "https://example.invalid/context"
        assert source.language == "und"

    def test_context_source_normalizes_declared_language(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv(
            "CONTEXT_SOURCES_JSON",
            '[{"id":"context","name":"Context","url":"https://example.invalid/context","language":"EN-us"}]',
        )

        assert Settings.from_env().context_sources[0].language == "en-US"

    @pytest.mark.parametrize("language", ("english", 1, None))
    def test_context_source_rejects_invalid_language(self, monkeypatch, language: object) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv(
            "CONTEXT_SOURCES_JSON",
            json.dumps(
                [
                    {
                        "id": "context",
                        "name": "Context",
                        "url": "https://example.invalid/context",
                        "language": language,
                    }
                ]
            ),
        )

        with pytest.raises(ConfigurationError, match=r"CONTEXT_SOURCES_JSON\[0\]\.language"):
            Settings.from_env()

    def test_invalid_timezone_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("BRIEFING_TIMEZONE", "Invalid/Timezone")

        with pytest.raises(ConfigurationError, match="Invalid timezone"):
            Settings.from_env()

    def test_rss_retry_delay_range_invalid_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("RSS_RETRY_MIN_SECONDS", "10")
        monkeypatch.setenv("RSS_RETRY_MAX_SECONDS", "5")

        with pytest.raises(ConfigurationError, match="RSS retry delay range is invalid"):
            Settings.from_env()

    def test_empty_weather_providers_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("WEATHER_PROVIDERS", ",")

        with pytest.raises(ConfigurationError, match="WEATHER_PROVIDERS cannot be empty"):
            Settings.from_env()

    def test_unsupported_weather_providers_raise_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("WEATHER_PROVIDERS", "qweather,typo,unknown")

        with pytest.raises(
            ConfigurationError,
            match="WEATHER_PROVIDERS contains unsupported providers: typo, unknown",
        ):
            Settings.from_env()

    def test_unsupported_publisher_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "telegrm")

        with pytest.raises(ConfigurationError, match="PUBLISHER must be one of: stdout, telegram"):
            Settings.from_env()

    def test_invalid_json_in_rss_sources_file_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        source_file = tmp_path / "rss-sources.json"
        source_file.write_text("not-json", encoding="utf-8")
        monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

        with pytest.raises(ConfigurationError, match="must contain readable JSON"):
            Settings.from_env()

    def test_rss_sources_file_not_array_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        source_file = tmp_path / "rss-sources.json"
        source_file.write_text('{"key":"value"}', encoding="utf-8")
        monkeypatch.setenv("RSS_SOURCES_FILE", str(source_file))

        with pytest.raises(ConfigurationError, match="must be a JSON array"):
            Settings.from_env()

    def test_non_numeric_coordinates_raise_error(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        location_file = tmp_path / "locations.json"
        location_file.write_text(
            '[{"id":"beijing","name":"Beijing","latitude":"abc","longitude":"def"}]',
            encoding="utf-8",
        )
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(location_file))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "rss-sources.json"))

        with pytest.raises(ConfigurationError, match="coordinates must be numbers"):
            Settings.from_env()
