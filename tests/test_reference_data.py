import pytest

from weather_briefing.air_quality import health_guidance
from weather_briefing.reference_data import (
    ReferenceDataError,
    load_reference_data,
    reference_string_tuple,
    reference_value,
)


def test_packaged_reference_data_is_available() -> None:
    assert reference_value("geography.json", "mainland_china_service_bounds", "latitude")
    assert reference_string_tuple(
        "geography.json",
        "mainland_china_geocoding_precision_reduction_patterns",
    )
    assert reference_string_tuple("content_cleaning.json", "default_remove_selectors")
    assert reference_string_tuple("provider_defaults.json", "qweather_lifestyle_index_types")


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
