"""Geographic reference rules and location-name matching."""

from __future__ import annotations

import re
from functools import cache

from ..reference_data import ReferenceDataError, reference_string_tuple, reference_value


@cache
def mainland_china_rules() -> tuple[float, float, float, float, frozenset[str]]:
    """Load and validate broad mainland China service rules."""
    try:
        latitude = reference_value("geography.json", "mainland_china_service_bounds", "latitude")
        longitude = reference_value("geography.json", "mainland_china_service_bounds", "longitude")
        excluded_areas = reference_string_tuple("geography.json", "mainland_china_excluded_administrative_areas")
        rules = (
            float(latitude["minimum"]),
            float(latitude["maximum"]),
            float(longitude["minimum"]),
            float(longitude["maximum"]),
            frozenset(area.casefold() for area in excluded_areas),
        )
        latitude_min, latitude_max, longitude_min, longitude_max, _ = rules
        if not -90 <= latitude_min < latitude_max <= 90:
            raise ReferenceDataError("Invalid mainland China latitude bounds")
        if not -180 <= longitude_min < longitude_max <= 180:
            raise ReferenceDataError("Invalid mainland China longitude bounds")
        return rules
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError("Invalid mainland China geography reference data") from exc


def possibly_mainland_china(latitude: float, longitude: float) -> bool:
    """Return whether coordinates fall inside the broad service bounds."""
    latitude_min, latitude_max, longitude_min, longitude_max, _ = mainland_china_rules()
    return latitude_min <= latitude <= latitude_max and longitude_min <= longitude <= longitude_max


def is_geocoded_mainland(country_code: str | None, administrative_area: str | None) -> bool:
    """Classify a structured geocoding result as mainland China."""
    if country_code != "CN":
        return False
    normalized = (administrative_area or "").casefold()
    return normalized not in mainland_china_rules()[4]


def nominatim_queries(name: str) -> tuple[str, ...]:
    """Return normalized and original Nominatim queries in priority order."""
    normalized = normalized_location_name(name)
    return tuple(dict.fromkeys((normalized, name)))


def nominatim_result_matches(name: str, result: dict[str, object]) -> bool:
    """Return whether one Nominatim result satisfies all name qualifiers."""
    return location_name_matches(name, str(result.get("display_name", "")))


def open_meteo_result_matches(name: str, result: dict[str, object]) -> bool:
    """Return whether one Open-Meteo result satisfies all name qualifiers."""
    result_description = " ".join(
        str(value)
        for field in ("name", "admin1", "admin2", "admin3", "admin4", "country")
        if (value := result.get(field))
    )
    return location_name_matches(name, result_description)


def location_name_matches(name: str, result_description: str) -> bool:
    """Match location qualifiers in order while allowing intervening administrative areas."""
    position = 0
    normalized_description = result_description.casefold()
    for component in location_name_components(name):
        normalized_component = component.casefold()
        match = re.compile(rf"(?<!\w){re.escape(normalized_component)}(?!\w)").search(
            normalized_description,
            position,
        )
        if match is None:
            return False
        position = match.end()
    return True


def location_name_components(name: str) -> tuple[str, ...]:
    """Return the specific place plus any comma-qualified geographic constraints."""
    normalized = normalized_location_name(name)
    components = tuple(
        specific_location_name(component) for component in re.split(r"[,，]", normalized) if component.strip()
    )
    return components or (specific_location_name(normalized),)


def specific_location_name(name: str) -> str:
    """Remove leading Chinese administrative divisions from a place name."""
    normalized = normalized_location_name(name)
    suffix_characters = reference_value(
        "geography.json",
        "nominatim_name_rules",
        "mainland_china_administrative_division_suffix_characters",
    )
    if not isinstance(suffix_characters, str) or not suffix_characters:
        raise ReferenceDataError("Nominatim administrative division suffix characters must be a string")
    return re.sub(rf"^.*[{re.escape(suffix_characters)}]", "", normalized).strip() or normalized


def normalized_location_name(name: str) -> str:
    """Remove configured provider-noise terms from a location name."""
    normalized = name
    for term in reference_string_tuple("geography.json", "nominatim_name_rules", "removable_terms"):
        normalized = normalized.replace(term, "")
    return normalized.strip()


def lower_precision_location_names(name: str) -> tuple[str, ...]:
    """Return safe progressively broader Chinese location names."""
    patterns = reference_string_tuple(
        "geography.json",
        "mainland_china_geocoding_precision_reduction_patterns",
    )
    candidates: list[str] = []
    current = name.strip()
    for pattern in patterns:
        reduced = re.sub(pattern, "", current).strip(" ,，")
        if reduced and reduced != name and reduced not in candidates:
            candidates.append(reduced)
        current = reduced
    return tuple(candidates)
