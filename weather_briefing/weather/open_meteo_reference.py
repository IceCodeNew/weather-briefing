"""Validated Open-Meteo weather code metadata."""

from __future__ import annotations

from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING

from weather_briefing.data.resources import ReferenceDataError, load_reference_data

if TYPE_CHECKING:
    from collections.abc import Mapping


@cache
def open_meteo_weather_code_descriptions() -> Mapping[int, str]:
    """Return validated English descriptions for Open-Meteo WMO weather codes."""
    value = load_reference_data("open_meteo_weather_codes.json")
    descriptions = value.get("descriptions_en")
    if set(value) != {"descriptions_en"} or not isinstance(descriptions, dict) or not descriptions:
        msg = "Open-Meteo weather codes must contain English descriptions"
        raise ReferenceDataError(msg)

    validated: dict[int, str] = {}
    for code, description in descriptions.items():
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not code.isdigit()
            or len(code) > 2  # noqa: PLR2004
            or not isinstance(description, str)
            or not description.strip()
        ):
            msg = "Open-Meteo weather codes must map numeric codes to descriptions"
            raise ReferenceDataError(msg)
        numeric_code = int(code)
        if str(numeric_code) != code:
            msg = "Open-Meteo weather codes must map numeric codes to descriptions"
            raise ReferenceDataError(msg)
        validated[numeric_code] = description
    return MappingProxyType(validated)
