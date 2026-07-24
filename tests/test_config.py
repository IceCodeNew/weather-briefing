import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from weather_briefing.config import (
    ConfigurationError,
    Settings,
    backfill_location_fields,
    weather_providers_for,
)
from weather_briefing.config.locations import load_locations as _locations
from weather_briefing.models import LocationSpec, ResolvedLocation, normalize_jma_office_code


def _required_environment(monkeypatch) -> None:
    values = {
        "DEEPSEEK_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "test-chat",
        "BRIEFING_LOCATIONS_FILE": str(Path(__file__).parents[1] / "locations.example.json"),
        "RSS_SOURCES_FILE": str(Path(__file__).parents[1] / "rss-sources.example.json"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _select_bark(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISHER", "bark")
    monkeypatch.setenv("BARK_DEVICE_KEY", "test-device")


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
    assert settings.qweather_jwt_lifetime_seconds == 900
    assert settings.llm_history_max_documents == 8
    assert settings.llm_history_max_characters == 16_000


@pytest.mark.parametrize(
    ("selected_publisher", "briefing_max_characters", "llm_max_output_tokens"),
    (("bark", 650, 4096), ("stdout", 3500, 8192), ("telegram", 3500, 8192)),
)
def test_publisher_selects_generation_defaults(
    monkeypatch,
    selected_publisher: str,
    briefing_max_characters: int,
    llm_max_output_tokens: int,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("PUBLISHER", selected_publisher)
    monkeypatch.delenv("BRIEFING_MAX_CHARACTERS", raising=False)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    if selected_publisher == "bark":
        monkeypatch.setenv("BARK_DEVICE_KEY", "test-device")

    settings = Settings.from_env()

    assert settings.briefing_max_characters == briefing_max_characters
    assert settings.llm_max_output_tokens == llm_max_output_tokens


def test_explicit_generation_limits_override_bark_defaults(monkeypatch) -> None:
    _required_environment(monkeypatch)
    _select_bark(monkeypatch)
    monkeypatch.setenv("BRIEFING_MAX_CHARACTERS", "500")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "1536")

    settings = Settings.from_env()

    assert settings.briefing_max_characters == 500
    assert settings.llm_max_output_tokens == 1536


def test_bark_llm_token_default_is_independent_of_briefing_limit(monkeypatch) -> None:
    _required_environment(monkeypatch)
    _select_bark(monkeypatch)
    monkeypatch.setenv("BRIEFING_MAX_CHARACTERS", "500")
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)

    settings = Settings.from_env()

    assert settings.briefing_max_characters == 500
    assert settings.llm_max_output_tokens == 4096


@pytest.mark.parametrize("selected_publisher", ("stdout", "telegram"))
def test_non_bark_llm_token_default_is_independent_of_briefing_limit(monkeypatch, selected_publisher: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("PUBLISHER", selected_publisher)
    monkeypatch.setenv("BRIEFING_MAX_CHARACTERS", "500")
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)

    settings = Settings.from_env()

    assert settings.briefing_max_characters == 500
    assert settings.llm_max_output_tokens == 8192


def test_bark_briefing_limit_rejects_values_above_platform_limit(monkeypatch) -> None:
    _required_environment(monkeypatch)
    _select_bark(monkeypatch)
    monkeypatch.setenv("BRIEFING_MAX_CHARACTERS", "651")

    with pytest.raises(ConfigurationError, match="BRIEFING_MAX_CHARACTERS cannot exceed 650"):
        Settings.from_env()


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


def test_weather_provider_order_can_be_configured(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("WEATHER_PROVIDERS", " open-meteo, qweather ")

    settings = Settings.from_env()

    assert weather_providers_for(_resolved_location(mainland=True), settings.weather_providers) == (
        "open-meteo",
        "qweather",
    )


def test_local_supplement_must_follow_explicit_primary_providers(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("WEATHER_PROVIDERS", "nea-sg,open-meteo")

    with pytest.raises(ConfigurationError, match="local capability providers after all primary providers"):
        Settings.from_env()


def test_nea_supplement_can_be_last_or_the_only_explicit_provider(monkeypatch) -> None:
    _required_environment(monkeypatch)

    monkeypatch.setenv("WEATHER_PROVIDERS", "open-meteo,nea-sg")
    assert Settings.from_env().weather_providers == ("open-meteo", "nea-sg")

    monkeypatch.setenv("WEATHER_PROVIDERS", "nea-sg")
    assert Settings.from_env().weather_providers == ("nea-sg",)


@pytest.mark.parametrize("local_provider", ("nea-sg", "jma-jp"))
def test_programmatic_weather_order_cannot_bypass_local_supplement_constraint(local_provider: str) -> None:
    with pytest.raises(ConfigurationError, match="local capability providers after all primary providers"):
        weather_providers_for(_resolved_location(mainland=False), (local_provider, "open-meteo"))


def test_multiple_local_supplements_can_follow_primary_providers() -> None:
    providers = ("open-meteo", "nea-sg", "jma-jp")
    singapore = replace(_resolved_location(mainland=False), country_code="SG")

    assert weather_providers_for(singapore, providers) == providers


@pytest.mark.parametrize("country_code", (None, "JP", "US"))
def test_explicit_nea_provider_is_removed_outside_singapore(country_code: str | None) -> None:
    location = replace(_resolved_location(mainland=False), country_code=country_code)

    assert weather_providers_for(location, ("open-meteo", "nea-sg")) == ("open-meteo",)

    with pytest.raises(ConfigurationError, match="only available for locations identified as Singapore"):
        weather_providers_for(location, ("nea-sg",))


def test_non_mainland_weather_providers_default_to_open_meteo_only(monkeypatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.delenv("WEATHER_PROVIDERS", raising=False)

    settings = Settings.from_env()

    assert weather_providers_for(_resolved_location(mainland=False), settings.weather_providers) == ("open-meteo",)


def test_singapore_and_japan_weather_provider_defaults(monkeypatch) -> None:
    _required_environment(monkeypatch)
    singapore = ResolvedLocation("sg", "Singapore", 1.3, 103.8, "SG", None, "Asia/Singapore", False)
    assert weather_providers_for(singapore, None) == ("open-meteo", "nea-sg")
    japan_without_office = ResolvedLocation("jp", "Osaka", 1.0, 1.0, "JP", None, "Asia/Tokyo", False)
    assert weather_providers_for(japan_without_office, None) == ("open-meteo",)
    japan = ResolvedLocation("jp", "Osaka", 1.0, 1.0, "JP", None, "Asia/Tokyo", False, jma_office_code="270000")
    assert weather_providers_for(japan, None) == ("open-meteo", "jma-jp")
    japan_without_country_code = ResolvedLocation(
        "jp-coordinates",
        "Osaka",
        1.0,
        1.0,
        None,
        None,
        None,
        False,
        jma_office_code="270000",
    )
    assert weather_providers_for(japan_without_country_code, None) == ("open-meteo", "jma-jp")
    known_non_japan = replace(japan_without_country_code, country_code="US")
    assert weather_providers_for(known_non_japan, None) == ("open-meteo",)


def test_location_jma_office_code_is_loaded(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text('[{"id":"osaka","name":"Osaka","jma_office_code":" 270000 "}]', encoding="utf-8")
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    assert Settings.from_env().locations[0].jma_office_code == "270000"


def test_location_models_enforce_jma_office_code_invariant() -> None:
    spec = LocationSpec("jp", "Tokyo", jma_office_code=" 130000 ")
    resolved = ResolvedLocation("jp", "Tokyo", 1.0, 1.0, "JP", None, "Asia/Tokyo", False, jma_office_code=" 130000 ")

    assert spec.jma_office_code == "130000"
    assert resolved.jma_office_code == "130000"
    with pytest.raises(ValueError, match="six digits"):
        LocationSpec("jp", "Tokyo", jma_office_code="１２３４５６")
    with pytest.raises(ValueError, match="six digits"):
        ResolvedLocation("jp", "Tokyo", 1.0, 1.0, "JP", None, "Asia/Tokyo", False, jma_office_code="１２３４５６")
    with pytest.raises(ValueError, match="six digits"):
        normalize_jma_office_code(130000)


@pytest.mark.parametrize("value", ("13000", "tokyo", "１２３４５６", 130000))
def test_location_jma_office_code_rejects_invalid_values(monkeypatch, tmp_path: Path, value: object) -> None:
    _required_environment(monkeypatch)
    locations_file = tmp_path / "locations.json"
    locations_file.write_text(
        json.dumps([{"id": "tokyo", "name": "Tokyo", "jma_office_code": value}]),
        encoding="utf-8",
    )
    rss_file = tmp_path / "rss-sources.json"
    rss_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(locations_file))
    monkeypatch.setenv("RSS_SOURCES_FILE", str(rss_file))

    with pytest.raises(ConfigurationError, match="six-digit JMA office code"):
        Settings.from_env()


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


def test_backfill_location_fields_writes_only_missing_user_fields(tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    location_file.write_text(
        json.dumps(
            [
                {"id": "name-only", "name": "Named Place", "custom": "preserved"},
                {"id": "coordinates-only", "latitude": 1.0, "longitude": 2.0},
                {"id": "complete", "name": "Configured", "latitude": 3.0, "longitude": 4.0},
            ]
        ),
        encoding="utf-8",
    )
    configured = (
        LocationSpec("name-only", "Named Place"),
        LocationSpec("coordinates-only", latitude=1.0, longitude=2.0),
        LocationSpec("complete", "Configured", 3.0, 4.0),
    )
    resolved = (
        ResolvedLocation("name-only", "Provider Name", 10.0, 20.0, "US", None, None, False),
        ResolvedLocation("coordinates-only", "Resolved Name", 1.0, 2.0, "US", None, None, False),
        ResolvedLocation("complete", "Provider Override", 30.0, 40.0, "US", None, None, False),
    )

    changed = backfill_location_fields(location_file, configured, resolved)

    assert changed
    assert json.loads(location_file.read_text(encoding="utf-8")) == [
        {
            "id": "name-only",
            "name": "Named Place",
            "custom": "preserved",
            "latitude": 10.0,
            "longitude": 20.0,
        },
        {"id": "coordinates-only", "latitude": 1.0, "longitude": 2.0, "name": "Resolved Name"},
        {"id": "complete", "name": "Configured", "latitude": 3.0, "longitude": 4.0},
    ]


def test_backfill_location_fields_preserves_file_permissions_and_identity(tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    location_file.write_text('[{"id":"place","name":"Place"}]', encoding="utf-8")
    location_file.chmod(0o600)
    original_inode = location_file.stat().st_ino
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", 10.0, 20.0, "US", None, None, False),)

    assert backfill_location_fields(location_file, configured, resolved)
    assert stat.S_IMODE(location_file.stat().st_mode) == 0o600
    assert location_file.stat().st_ino == original_inode


def test_backfill_location_fields_skips_reduced_precision_matches(tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","name":"Specific Place"}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", "Specific Place"),)
    resolved = (
        ResolvedLocation(
            "place",
            "Specific Place",
            10.0,
            20.0,
            "CN",
            None,
            None,
            True,
            precision_reduced=True,
        ),
    )

    changed = backfill_location_fields(location_file, configured, resolved)

    assert not changed
    assert location_file.read_text(encoding="utf-8") == original


def test_backfill_location_fields_preserves_fields_added_after_configuration_load(tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","name":"Place","latitude":30.0,"longitude":40.0}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", 10.0, 20.0, "US", None, None, False),)

    changed = backfill_location_fields(location_file, configured, resolved)

    assert not changed
    assert location_file.read_text(encoding="utf-8") == original


def test_backfill_location_fields_does_not_mix_partial_on_disk_coordinates(tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","name":"Place","latitude":30.0,"longitude":null}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", 10.0, 20.0, "US", None, None, False),)

    changed = backfill_location_fields(location_file, configured, resolved)

    assert not changed
    assert location_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("name", ("", "   ", None, 123))
def test_backfill_location_fields_rejects_invalid_resolved_names(tmp_path: Path, name: object) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","latitude":10.0,"longitude":20.0}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", latitude=10.0, longitude=20.0),)
    resolved_record = {
        "id": "place",
        "name": name,
        "latitude": 10.0,
        "longitude": 20.0,
        "country_code": "US",
        "administrative_area": None,
        "timezone": None,
        "is_mainland_china": False,
    }
    resolved = (ResolvedLocation(**json.loads(json.dumps(resolved_record))),)

    with pytest.raises(ConfigurationError, match="Resolved name for location place is invalid"):
        backfill_location_fields(location_file, configured, resolved)

    assert location_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("latitude", "10"),
        ("latitude", None),
        ("latitude", True),
        ("longitude", "20"),
        ("longitude", None),
        ("longitude", False),
    ),
)
def test_backfill_location_fields_rejects_non_numeric_resolved_coordinates(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","name":"Place"}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved_record = {
        "id": "place",
        "name": "Place",
        "latitude": 10.0,
        "longitude": 20.0,
        "country_code": "US",
        "administrative_area": None,
        "timezone": None,
        "is_mainland_china": False,
    }
    resolved_record[field] = value
    resolved = (ResolvedLocation(**json.loads(json.dumps(resolved_record))),)

    with pytest.raises(ConfigurationError, match=rf"Resolved {field} for location place is invalid"):
        backfill_location_fields(location_file, configured, resolved)

    assert location_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("latitude", "longitude", "field"),
    (
        (float("nan"), 20.0, "latitude"),
        (float("inf"), 20.0, "latitude"),
        (91.0, 20.0, "latitude"),
        (10.0, float("nan"), "longitude"),
        (10.0, float("inf"), "longitude"),
        (10.0, 181.0, "longitude"),
    ),
)
def test_backfill_location_fields_rejects_invalid_resolved_coordinates(
    tmp_path: Path,
    latitude: float,
    longitude: float,
    field: str,
) -> None:
    location_file = tmp_path / "locations.json"
    original = '[{"id":"place","name":"Place"}]'
    location_file.write_text(original, encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", latitude, longitude, "US", None, None, False),)

    with pytest.raises(ConfigurationError, match=rf"Resolved {field} for location place is invalid"):
        backfill_location_fields(location_file, configured, resolved)

    assert location_file.read_text(encoding="utf-8") == original


def test_locations_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Configure at least one location"):
        _locations(tmp_path / "missing.json")


def test_locations_reports_file_read_errors(monkeypatch, tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    location_file.write_text('[{"id":"place","name":"Place"}]', encoding="utf-8")

    def fail_open(*_args: object, **_kwargs: object) -> None:
        raise OSError("read failure")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(ConfigurationError, match="must contain readable JSON"):
        _locations(location_file)


def test_json_file_reports_read_errors(monkeypatch, tmp_path: Path) -> None:
    from weather_briefing.config.files import json_file

    source_file = tmp_path / "rss-sources.json"
    source_file.write_text("[]", encoding="utf-8")

    def fail_read_text(*_args: object, **_kwargs: object) -> None:
        raise OSError("read failure")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    with pytest.raises(ConfigurationError, match="must contain readable JSON"):
        json_file(source_file)


@pytest.mark.parametrize(
    "name",
    [
        "BRIEFING_LOCATIONS_FILE",
        "RSS_SOURCES_FILE",
        "GEOCODING_CACHE_PATH",
        "BRIEFING_STATE_PATH",
    ],
)
def test_settings_rejects_empty_path_environment_values(monkeypatch, name: str) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv(name, "   ")

    with pytest.raises(ConfigurationError, match=rf"{name} must not be empty"):
        Settings.from_env()


def test_backfill_location_fields_requires_writable_file(monkeypatch, tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    location_file.write_text('[{"id":"place","name":"Place"}]', encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", 10.0, 20.0, "US", None, None, False),)

    def deny_location_write(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only mount")

    monkeypatch.setattr(Path, "open", deny_location_write)

    with pytest.raises(ConfigurationError, match="must be writable to save resolved location fields"):
        backfill_location_fields(location_file, configured, resolved)


def test_backfill_location_fields_reports_non_permission_io_errors(monkeypatch, tmp_path: Path) -> None:
    location_file = tmp_path / "locations.json"
    location_file.write_text('[{"id":"place","name":"Place"}]', encoding="utf-8")
    configured = (LocationSpec("place", "Place"),)
    resolved = (ResolvedLocation("place", "Place", 10.0, 20.0, "US", None, None, False),)

    def fail_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(
        ConfigurationError,
        match=rf"Failed to save resolved location fields to {location_file}: disk full",
    ):
        backfill_location_fields(location_file, configured, resolved)

    assert json.loads(location_file.read_text(encoding="utf-8")) == [
        {"id": "place", "name": "Place", "latitude": 10.0, "longitude": 20.0}
    ]


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

    settings = Settings.from_env()

    assert settings.geocoding_api_key == "geocoding-key"


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
        assert settings.service_status_cron == "*/5 * * * *"
        assert settings.service_status_language == "en"
        assert settings.service_status_publishers == ("telegram",)

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

    def test_custom_service_status_cron(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_CRON", "*/2 8-20 * * 1-5")

        assert Settings.from_env().service_status_cron == "*/2 8-20 * * 1-5"

    @pytest.mark.parametrize("value", ("en", "zh-CN", "ja"))
    def test_service_status_notification_language(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_LANGUAGE", value)

        assert Settings.from_env().service_status_language == value

    @pytest.mark.parametrize("value", ("", "fr", "zh-Hant", "not_a_language"))
    def test_invalid_service_status_notification_language_rejected(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_LANGUAGE", value)

        with pytest.raises(ConfigurationError, match="SERVICE_STATUS_LANGUAGE"):
            Settings.from_env()

    @pytest.mark.parametrize("value", ("", "*/5", "60 * * * *", "* * * * * *"))
    def test_invalid_service_status_cron_rejected(self, monkeypatch, value: str) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_CRON", value)

        with pytest.raises(ConfigurationError, match="SERVICE_STATUS_CRON"):
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

    def test_service_status_providers_default_to_disabled(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.delenv("SERVICE_STATUS_PROVIDERS", raising=False)

        assert Settings.from_env().service_status_providers == ()

    def test_service_status_providers_accept_official_sources(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "deepseek,openai,anthropic,kimi")

        assert Settings.from_env().service_status_providers == (
            "deepseek",
            "openai",
            "anthropic",
            "kimi",
        )

    def test_service_status_providers_can_be_disabled(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "")

        assert Settings.from_env().service_status_providers == ()

    def test_service_status_only_mode_does_not_require_locations_or_rss(self, monkeypatch, tmp_path: Path) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "openai")
        monkeypatch.setenv("BRIEFING_LOCATIONS_FILE", str(tmp_path / "missing-locations.json"))
        monkeypatch.setenv("RSS_SOURCES_FILE", str(tmp_path / "missing-rss.json"))

        settings = Settings.from_env()

        assert not settings.weather_briefings_enabled
        assert settings.locations == ()
        assert settings.feeds == ()

    def test_missing_locations_remains_an_error_without_explicit_status_sources(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        _required_environment(monkeypatch)
        monkeypatch.delenv("SERVICE_STATUS_PROVIDERS", raising=False)
        monkeypatch.setenv(
            "BRIEFING_LOCATIONS_FILE",
            str(tmp_path / "missing-locations.json"),
        )

        with pytest.raises(ConfigurationError, match="Configure at least one location"):
            Settings.from_env()

    def test_service_status_publishers_accept_multiple_platforms(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "stdout")
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "openai")
        monkeypatch.setenv("SERVICE_STATUS_PUBLISHERS", "telegram,bark")
        monkeypatch.setenv("BARK_DEVICE_KEY", "test-device")

        settings = Settings.from_env()

        assert settings.service_status_publishers == ("telegram", "bark")
        assert settings.briefing_max_characters == 3500
        assert settings.llm_max_output_tokens == 8192

    def test_disabled_service_status_does_not_require_publisher_credentials(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "stdout")
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "")
        monkeypatch.setenv("SERVICE_STATUS_PUBLISHERS", "telegram,bark")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        monkeypatch.delenv("TELEGRAM_CHAT_ID")
        monkeypatch.delenv("BARK_DEVICE_KEY", raising=False)

        settings = Settings.from_env()

        assert settings.service_status_providers == ()
        assert settings.service_status_publishers == ("telegram", "bark")

    @pytest.mark.parametrize(
        ("missing_name", "message"),
        (
            ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
            ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
        ),
    )
    def test_service_status_telegram_requires_credentials(
        self,
        monkeypatch,
        missing_name: str,
        message: str,
    ) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "stdout")
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "openai")
        monkeypatch.setenv("SERVICE_STATUS_PUBLISHERS", "telegram")
        monkeypatch.delenv(missing_name)

        with pytest.raises(ConfigurationError, match=message):
            Settings.from_env()

    def test_weather_telegram_requires_credentials(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "telegram")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")

        with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
            Settings.from_env()

    @pytest.mark.parametrize(
        ("value", "message"),
        (
            ("", "cannot be empty"),
            (", , ", "cannot be empty"),
            ("telegram,telegram", "cannot contain duplicates"),
            ("telegram,email", "unsupported publishers: email"),
        ),
    )
    def test_invalid_service_status_publishers_are_rejected(
        self,
        monkeypatch,
        value: str,
        message: str,
    ) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PUBLISHERS", value)

        with pytest.raises(ConfigurationError, match=message):
            Settings.from_env()

    def test_unsupported_service_status_provider_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "openai,zhipu")

        with pytest.raises(
            ConfigurationError,
            match="SERVICE_STATUS_PROVIDERS contains unsupported providers: zhipu",
        ):
            Settings.from_env()

    def test_duplicate_service_status_provider_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("SERVICE_STATUS_PROVIDERS", "openai,openai")

        with pytest.raises(ConfigurationError, match="SERVICE_STATUS_PROVIDERS cannot contain duplicates"):
            Settings.from_env()

    def test_unsupported_publisher_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "telegrm")

        with pytest.raises(ConfigurationError, match="PUBLISHER must be one of: bark, stdout, telegram"):
            Settings.from_env()

    def test_missing_bark_device_key_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "bark")
        monkeypatch.delenv("BARK_DEVICE_KEY", raising=False)

        with pytest.raises(ConfigurationError, match="Missing required environment variable: BARK_DEVICE_KEY"):
            Settings.from_env()

    @pytest.mark.parametrize("key", ("short", "a" * 17, "a" * 33))
    def test_invalid_bark_encryption_key_length_raises_error(self, monkeypatch, key: str) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_KEY", key)

        with pytest.raises(ConfigurationError, match="16, 24, or 32 ASCII characters"):
            Settings.from_env()

    def test_non_ascii_bark_encryption_key_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_KEY", "密" * 16)

        with pytest.raises(ConfigurationError, match="only ASCII"):
            Settings.from_env()

    @pytest.mark.parametrize("iv", ("short", "longer-than-twelve"))
    def test_invalid_bark_encryption_iv_length_raises_error(self, monkeypatch, iv: str) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_IV", iv)

        with pytest.raises(ConfigurationError, match="exactly 12 ASCII characters"):
            Settings.from_env()

    def test_non_ascii_bark_encryption_iv_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_IV", "密" * 12)

        with pytest.raises(ConfigurationError, match="only ASCII"):
            Settings.from_env()

    def test_bark_encryption_key_without_iv_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_KEY", "k" * 32)

        with pytest.raises(ConfigurationError, match="must be configured together"):
            Settings.from_env()

    def test_bark_encryption_iv_without_key_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_IV", "fixed-iv-123")

        with pytest.raises(ConfigurationError, match="must be configured together"):
            Settings.from_env()

    @pytest.mark.parametrize(
        "base_url",
        (
            "api.example.invalid",
            "ftp://api.example.invalid",
            "https://user:password@api.example.invalid",
            "https://api.example.invalid?token=private",
            "https://api.example.invalid#fragment",
            "https://api.example.invalid:invalid",
        ),
    )
    def test_invalid_bark_base_url_raises_error(self, monkeypatch, base_url: str) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_BASE_URL", base_url)

        with pytest.raises(ConfigurationError, match="BARK_BASE_URL must be"):
            Settings.from_env()

    def test_bark_base_url_is_loaded_and_normalized(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_BASE_URL", "https://bark.example.invalid/prefix/")

        settings = Settings.from_env()

        assert settings.bark_base_url == "https://bark.example.invalid/prefix"

    def test_bark_group_is_loaded(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_GROUP", "forecast-team")

        settings = Settings.from_env()

        assert settings.bark_group == "forecast-team"

    def test_empty_bark_group_raises_error(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_GROUP", "   ")

        with pytest.raises(ConfigurationError, match="BARK_GROUP must not be empty"):
            Settings.from_env()

    @pytest.mark.parametrize("key_length", (16, 24, 32))
    def test_bark_encryption_key_lengths_are_loaded(self, monkeypatch, key_length: int) -> None:
        _required_environment(monkeypatch)
        _select_bark(monkeypatch)
        monkeypatch.setenv("BARK_ENCRYPTION_KEY", "k" * key_length)
        monkeypatch.setenv("BARK_ENCRYPTION_IV", "fixed-iv-123")

        settings = Settings.from_env()

        assert settings.publisher == "bark"
        assert settings.bark_device_key == "test-device"
        assert settings.bark_base_url == "https://api.day.app"
        assert settings.bark_group == "weather-briefing"
        assert settings.bark_encryption_key == "k" * key_length
        assert settings.bark_encryption_iv == "fixed-iv-123"

    def test_bark_settings_do_not_block_another_publisher(self, monkeypatch) -> None:
        _required_environment(monkeypatch)
        monkeypatch.setenv("PUBLISHER", "telegram")
        monkeypatch.setenv("BARK_BASE_URL", "not-a-url")
        monkeypatch.setenv("BARK_GROUP", "   ")
        monkeypatch.setenv("BARK_ENCRYPTION_KEY", "short")
        monkeypatch.setenv("BARK_ENCRYPTION_IV", "short")

        settings = Settings.from_env()

        assert settings.publisher == "telegram"
        assert settings.bark_device_key is None
        assert settings.bark_base_url == "https://api.day.app"
        assert settings.bark_group == "weather-briefing"
        assert settings.bark_encryption_key is None
        assert settings.bark_encryption_iv is None

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
