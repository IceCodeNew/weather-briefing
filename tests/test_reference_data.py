from weather_briefing.reference_data import reference_string_tuple


def test_packaged_reference_data_is_available() -> None:
    assert reference_string_tuple("provider_defaults.json", "qweather_lifestyle_index_types")
