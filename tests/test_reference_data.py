from copy import deepcopy
from types import MappingProxyType
from typing import TypeGuard

import pytest

from weather_briefing import __version__
from weather_briefing.air_quality import health_guidance
from weather_briefing.data import resources as data_resources
from weather_briefing.data.resources import (
    ReferenceDataError,
    load_reference_data,
    reference_string,
    reference_string_tuple,
    reference_value,
)
from weather_briefing.data.service_endpoints import NOMINATIM_USER_AGENT
from weather_briefing.delivery.telegram_reference import telegram_error_classification
from weather_briefing.localization import localization_table
from weather_briefing.weather.open_meteo_reference import open_meteo_weather_code_descriptions


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _mutable_localization_data() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    value = deepcopy(load_reference_data("localization.json"))
    tables = value["tables"]
    aliases = value["aliases"]
    assert _is_string_object_dict(tables)
    assert _is_string_object_dict(aliases)
    return value, tables, aliases


def _localization_language(tables: dict[str, object], table_name: str, language: str) -> dict[str, object]:
    table = tables[table_name]
    assert _is_string_object_dict(table)
    labels = table[language]
    assert _is_string_object_dict(labels)
    return labels


def _telegram_classification_data() -> dict[str, object]:
    return {
        "description_markers": {"chat not found": "chat-not-found"},
        "parameter_reasons": {"migrate_to_chat_id": "chat-migrated"},
        "status_reasons": {"401": "bot-token-rejected"},
        "channel_unavailable_reasons": ["chat-not-found", "chat-migrated", "bot-token-rejected"],
    }


def test_packaged_reference_data_is_available() -> None:
    assert reference_value("geography.json", "mainland_china_service_bounds", "latitude")
    assert reference_string_tuple(
        "geography.json",
        "mainland_china_geocoding_precision_reduction_patterns",
    )
    assert reference_string_tuple("content_cleaning.json", "default_remove_selectors")
    assert reference_string_tuple("provider_defaults.json", "qweather_lifestyle_index_types")
    assert reference_string("provider_defaults.json", "qweather_allergen_index_type") == "7"
    weather_codes = open_meteo_weather_code_descriptions()
    assert set(weather_codes) == {
        0,
        1,
        2,
        3,
        45,
        48,
        51,
        53,
        55,
        56,
        57,
        61,
        63,
        65,
        66,
        67,
        71,
        73,
        75,
        77,
        80,
        81,
        82,
        85,
        86,
        95,
        96,
        99,
    }
    assert weather_codes[53] == "Moderate drizzle"
    assert localization_table("weather_document")["ja"]["forecast"] == "天気予報"
    assert localization_table("briefing")["zh-Hans"]["weather"] == "天气信息"
    classification = telegram_error_classification()
    assert ("chat not found", "chat-not-found") in classification.description_markers
    assert classification.parameter_reasons["migrate_to_chat_id"] == "chat-migrated"
    assert classification.status_reasons[401] == "bot-token-rejected"
    assert "chat-not-found" in classification.channel_unavailable_reasons


def test_nominatim_user_agent_identifies_current_version() -> None:
    assert (f"weather-briefing/{__version__} (+https://github.com/IceCodeNew/weather-briefing)") == NOMINATIM_USER_AGENT


@pytest.mark.parametrize(
    "value",
    (
        {},
        {"descriptions_en": {}},
        {"descriptions_en": {"unknown": "Clear sky"}},
        {"descriptions_en": {"00": "Clear sky"}},
        {"descriptions_en": {"9" * 5_000: "Unknown"}},
        {"descriptions_en": {"0": ""}},
        {"descriptions_en": {"0": "Clear sky"}, "unknown": {}},
    ),
)
def test_open_meteo_weather_codes_reject_invalid_data(monkeypatch, value) -> None:
    monkeypatch.setattr("weather_briefing.weather.open_meteo_reference.load_reference_data", lambda filename: value)
    open_meteo_weather_code_descriptions.cache_clear()

    with pytest.raises(ReferenceDataError, match="Open-Meteo weather codes"):
        open_meteo_weather_code_descriptions()


def test_air_quality_guidance_covers_values_above_last_bounded_band() -> None:
    category, guidance = health_guidance(10_000)

    assert category
    assert guidance


def test_load_reference_data_rejects_non_json_filename() -> None:
    with pytest.raises(ReferenceDataError, match="one JSON file"):
        load_reference_data("data.json/nested")


def test_load_reference_data_rejects_non_json_extension() -> None:
    with pytest.raises(ReferenceDataError, match="one JSON file"):
        load_reference_data("data.txt")


def test_load_reference_data_rejects_missing_file() -> None:
    with pytest.raises(ReferenceDataError, match="Unable to load"):
        load_reference_data("nonexistent.json")


def test_reference_value_rejects_missing_path() -> None:
    with pytest.raises(ReferenceDataError, match="Missing reference data field"):
        reference_value("geography.json", "nonexistent", "key")


def test_reference_string_tuple_rejects_non_list_value() -> None:
    with pytest.raises(ReferenceDataError, match="non-empty string list"):
        reference_string_tuple("geography.json", "mainland_china_service_bounds")


@pytest.mark.parametrize("value", [None, "", "   ", 7])
def test_reference_string_rejects_invalid_value(monkeypatch, value) -> None:
    monkeypatch.setattr("weather_briefing.data.resources.reference_value", lambda *args: value)

    with pytest.raises(ReferenceDataError, match="non-empty string"):
        reference_string("provider_defaults.json", "qweather_allergen_index_type")


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ({}, "supported fields"),
        (_telegram_classification_data() | {"description_markers": {}}, "description markers"),
        (_telegram_classification_data() | {"status_reasons": {}}, "statuses"),
        (
            _telegram_classification_data() | {"description_markers": {"CHAT NOT FOUND": "chat-not-found"}},
            "description markers",
        ),
        (
            _telegram_classification_data() | {"description_markers": {"chat not found": "unsafe\nreason"}},
            "description markers",
        ),
        (
            _telegram_classification_data() | {"parameter_reasons": {}},
            "parameters",
        ),
        (
            _telegram_classification_data() | {"parameter_reasons": {"migrate_to_chat_id": "unsafe\nreason"}},
            "parameters",
        ),
        (
            _telegram_classification_data() | {"status_reasons": {"invalid": "api-error"}},
            "statuses",
        ),
        (
            _telegram_classification_data() | {"channel_unavailable_reasons": []},
            "channel availability",
        ),
        (
            _telegram_classification_data() | {"channel_unavailable_reasons": ["chat-not-found", "chat-not-found"]},
            "channel availability",
        ),
        (
            _telegram_classification_data() | {"channel_unavailable_reasons": ["unknown-reason"]},
            "channel availability",
        ),
        (
            _telegram_classification_data() | {"channel_unavailable_reasons": [7]},
            "channel availability",
        ),
        (
            _telegram_classification_data() | {"channel_unavailable_reasons": ["unsafe\nreason"]},
            "channel availability",
        ),
    ),
)
def test_telegram_error_classification_rejects_invalid_data(monkeypatch, value, message) -> None:
    monkeypatch.setattr(data_resources, "load_reference_data", lambda filename: value)
    telegram_error_classification.cache_clear()

    with pytest.raises(ReferenceDataError, match=message):
        telegram_error_classification()


def test_load_reference_data_rejects_non_dict_root(monkeypatch) -> None:
    from weather_briefing.data.resources import load_reference_data

    class FakeResource:
        def joinpath(self, filename):
            return self

        def read_text(self, encoding=None):
            return "42"

    monkeypatch.setattr(
        "weather_briefing.data.resources.resources.files",
        lambda package: FakeResource(),
    )

    with pytest.raises(ReferenceDataError, match="must be an object"):
        load_reference_data("test.json")


def test_load_reference_data_returns_independent_values() -> None:
    first = load_reference_data("localization.json")
    tables = first["tables"]
    assert isinstance(tables, dict)
    tables.clear()

    second = load_reference_data("localization.json")

    assert second["tables"]


def test_reference_value_copies_only_the_selected_value(monkeypatch) -> None:
    import weather_briefing.data.resources as resources_module

    class UnselectedValue:
        def __deepcopy__(self, memo):
            raise AssertionError(  # pragma: no cover - reached only if root copying regresses
                "reference_value must not copy the cached root"
            )

    selected = {"items": ["one"]}
    root = {"selected": selected, "unselected": UnselectedValue()}

    monkeypatch.setattr(resources_module, "_load_reference_data", lambda filename: root)

    first = reference_value("test.json", "selected")
    assert isinstance(first, dict)
    items = first["items"]
    assert isinstance(items, list)
    items.append("changed")

    second = reference_value("test.json", "selected")

    assert second == {"items": ["one"]}
    assert first is not selected


def test_reference_string_tuple_does_not_copy_the_cached_list(monkeypatch) -> None:
    import weather_briefing.data.resources as resources_module

    class CachedStrings(list[str]):
        def __deepcopy__(self, memo):
            raise AssertionError(  # pragma: no cover - reached only if list copying regresses
                "reference_string_tuple must not copy the cached list"
            )

    cached = CachedStrings(["one", "two"])
    monkeypatch.setattr(resources_module, "_load_reference_data", lambda filename: {"selected": cached})

    assert reference_string_tuple("test.json", "selected") == ("one", "two")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"tables": [], "aliases": {}}, "every supported table"),
        ({"tables": {}, "aliases": {}}, "every supported table"),
        (
            {
                "tables": {
                    name: {} for name in ("air_quality", "allergen", "briefing", "qweather", "weather_document")
                },
                "aliases": {},
            },
            "every supported language",
        ),
    ],
)
def test_localization_table_rejects_incomplete_data(monkeypatch, value, message) -> None:
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match=message):
        localization_table("briefing")


def test_localization_table_rejects_missing_fields(monkeypatch) -> None:
    value, tables, _ = _mutable_localization_data()
    english = _localization_language(tables, "briefing", "en")
    del english["weather"]
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Invalid localization fields: briefing:en"):
        localization_table("briefing")


def test_localization_table_rejects_unknown_alias_target(monkeypatch) -> None:
    value, _, aliases = _mutable_localization_data()
    briefing = aliases["briefing"]
    assert _is_string_object_dict(briefing)
    briefing["zh-Hans"] = "missing"
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Invalid localization alias: briefing:zh-Hans"):
        localization_table("briefing")


def test_localization_table_rejects_whitespace_values(monkeypatch) -> None:
    value, tables, _ = _mutable_localization_data()
    _localization_language(tables, "briefing", "en")["weather"] = "   "
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Invalid localization fields: briefing:en"):
        localization_table("briefing")


def test_localization_tables_are_immutable() -> None:
    localization_table.cache_clear()
    table = localization_table("briefing")

    assert isinstance(table, MappingProxyType)
    assert isinstance(table["en"], MappingProxyType)
    assert isinstance(table["zh-Hans"], MappingProxyType)
    assert localization_table("briefing")["en"]["weather"] == "Weather information"
    assert localization_table("briefing")["zh-Hans"]["weather"] == "天气信息"


@pytest.mark.parametrize("aliases", ([], {"unknown": {}}))
def test_localization_table_rejects_invalid_alias_root(monkeypatch, aliases) -> None:
    value, _, _ = _mutable_localization_data()
    value["aliases"] = aliases
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="every supported table"):
        localization_table("briefing")


def test_localization_table_rejects_unknown_table() -> None:
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Unknown localization table: missing"):
        localization_table("missing")


def test_localization_table_rejects_non_object_table(monkeypatch) -> None:
    value, tables, _ = _mutable_localization_data()
    tables["briefing"] = []
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Unknown localization table: briefing"):
        localization_table("briefing")


def test_localization_table_rejects_non_object_labels(monkeypatch) -> None:
    value, tables, _ = _mutable_localization_data()
    briefing = tables["briefing"]
    assert _is_string_object_dict(briefing)
    briefing["en"] = []
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Invalid localization fields: briefing:en"):
        localization_table("briefing")


def test_localization_table_rejects_non_object_table_aliases(monkeypatch) -> None:
    value, _, aliases = _mutable_localization_data()
    aliases["briefing"] = []
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Localization aliases must be an object: briefing"):
        localization_table("briefing")


@pytest.mark.parametrize(
    ("alias", "target"),
    ((7, "zh-CN"), ("ZH", "zh-CN"), ("bad_tag", "zh-CN"), ("fr", 7), ("en", "en")),
)
def test_localization_table_rejects_invalid_aliases(monkeypatch, alias, target) -> None:
    value, _, aliases = _mutable_localization_data()
    aliases["briefing"] = {alias: target}
    monkeypatch.setattr("weather_briefing.data.resources._load_reference_data", lambda filename: value)
    localization_table.cache_clear()

    with pytest.raises(ReferenceDataError, match="Invalid localization alias: briefing"):
        localization_table("briefing")
