from weather_briefing.air_quality import health_guidance
from weather_briefing.reference_data import reference_string_tuple, reference_value


def test_packaged_reference_data_is_available() -> None:
    assert reference_value("geography.json", "mainland_china_service_bounds", "latitude")
    assert reference_string_tuple("content_cleaning.json", "default_remove_selectors")
    assert reference_string_tuple("provider_defaults.json", "qweather_lifestyle_index_types")


def test_air_quality_guidance_covers_values_above_last_bounded_band() -> None:
    category, guidance = health_guidance(10_000)

    assert category
    assert guidance
