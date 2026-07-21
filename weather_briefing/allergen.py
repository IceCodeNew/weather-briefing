"""Allergen reference data, guidance, and source conversion."""

from __future__ import annotations

from functools import cache
from math import isfinite

from .languages import localized_labels
from .models import AllergenSnapshot, SourceDocument
from .reference_data import ReferenceDataError, localization_table, reference_value

_ALLERGEN_FORMATS = localization_table("allergen")


def allergen_guidance(concentration: float) -> tuple[str, str]:
    """Return (category, guidance) for a pollen concentration in grains/m³."""
    if not isfinite(concentration) or concentration < 0:
        raise ValueError("Pollen concentration must be finite and non-negative")
    for maximum, category, guidance in _allergen_bands():
        if maximum is None or concentration <= maximum:
            return category, guidance
    raise ReferenceDataError("Allergen guidance must end with an unbounded band")


def allergen_to_document(snapshot: AllergenSnapshot) -> SourceDocument:
    """Convert an allergen snapshot into a citable source document."""
    labels = localized_labels(snapshot.output_language, _ALLERGEN_FORMATS)
    separator = labels["separator"]
    observed_at = (
        snapshot.observed_at.to_iso8601_string() if snapshot.observed_at is not None else labels["unavailable"]
    )
    levels = (
        "\n".join(
            labels["level"].format(
                name=level.name,
                concentration=level.concentration,
                category=level.category,
            )
            for level in snapshot.levels
        )
        or labels["unavailable"]
    )
    history_value = (
        f"{labels['allergens']}{separator}\n{levels}\n"
        f"{labels['overall']}{separator}{snapshot.overall_category}\n"
        f"{labels['health']}{separator}{snapshot.health_guidance}"
    )
    return SourceDocument(
        id=snapshot.source_id,
        name=snapshot.source_name,
        url=snapshot.source_url,
        has_allergen_information=True,
        language=snapshot.output_language,
        content=(
            f"{labels['observed_at']}{separator}{observed_at}\n"
            f"{labels['allergens']}{separator}\n{levels}\n"
            f"{labels['overall']}{separator}{snapshot.overall_category}\n"
            f"{labels['health']}{separator}{snapshot.health_guidance}"
        ),
        history_summary=(
            f"{labels['observed_at']}{separator}{observed_at}\n"
            f"{labels['count']}{separator}{len(snapshot.levels)}\n"
            f"{labels['overall']}{separator}{snapshot.overall_category}"
        ),
        history_value=history_value,
    )


@cache
def pollen_type_names() -> tuple[tuple[str, str], ...]:
    """Return ((api_key, display_name), ...) for supported pollen types."""
    types = reference_value("allergen_guidance.json", "pollen_types")
    if not isinstance(types, dict) or not types:
        raise ReferenceDataError("Allergen pollen types must be a non-empty object")
    return tuple((str(key), str(value)) for key, value in types.items())


@cache
def _allergen_bands() -> tuple[tuple[float | None, str, str], ...]:
    values = reference_value("allergen_guidance.json", "bands")
    if not isinstance(values, list) or not values:
        raise ReferenceDataError("Allergen guidance bands must be a non-empty list")
    bands: list[tuple[float | None, str, str]] = []
    try:
        for value in values:
            maximum = value["maximum_concentration"]
            if maximum is not None:
                maximum = float(maximum)
            bands.append((maximum, str(value["category"]), str(value["guidance"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError("Invalid allergen guidance band") from exc
    bounded_maxima = [maximum for maximum, _, _ in bands[:-1]]
    if bands[-1][0] is not None or any(maximum is None for maximum in bounded_maxima):
        raise ReferenceDataError("Allergen guidance must end with an unbounded band")
    numeric_maxima = [maximum for maximum in bounded_maxima if maximum is not None]
    if numeric_maxima != sorted(set(numeric_maxima)) or any(maximum < 0 for maximum in numeric_maxima):
        raise ReferenceDataError("Allergen guidance bounds must be unique, increasing, and non-negative")
    return tuple(bands)
