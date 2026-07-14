from __future__ import annotations

from functools import cache
from math import isfinite

from .models import AllergenSnapshot, SourceDocument
from .reference_data import ReferenceDataError, reference_value


def allergen_guidance(concentration: float) -> tuple[str, str]:
    """Return (category, guidance) for a pollen concentration in grains/m³."""
    if not isfinite(concentration) or concentration < 0:
        raise ValueError("Pollen concentration must be finite and non-negative")
    for maximum, category, guidance in _allergen_bands():
        if maximum is None or concentration <= maximum:
            return category, guidance
    raise ReferenceDataError("Allergen guidance must end with an unbounded band")


def allergen_to_document(snapshot: AllergenSnapshot) -> SourceDocument:
    observed_at = snapshot.observed_at.to_iso8601_string() if snapshot.observed_at is not None else "不可用"
    levels = (
        "\n".join(f"- {level.name}：{level.concentration:g} 粒/m³（{level.category}）" for level in snapshot.levels)
        or "不可用"
    )
    return SourceDocument(
        id=snapshot.source_id,
        name=snapshot.source_name,
        url=snapshot.source_url,
        content=(
            f"观测时间：{observed_at}\n"
            f"花粉过敏原：\n{levels}\n"
            f"总体等级：{snapshot.overall_category}\n"
            f"健康提示：{snapshot.health_guidance}"
        ),
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
