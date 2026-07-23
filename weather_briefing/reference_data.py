"""Compatibility exports for packaged reference data."""

from .data.resources import (
    ReferenceDataError,
    load_reference_data,
    reference_string,
    reference_string_tuple,
    reference_value,
)
from .localization import localization_table
from .weather.open_meteo_reference import open_meteo_weather_code_descriptions

__all__ = [
    "ReferenceDataError",
    "load_reference_data",
    "localization_table",
    "open_meteo_weather_code_descriptions",
    "reference_string",
    "reference_string_tuple",
    "reference_value",
]
