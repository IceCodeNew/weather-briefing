"""Conversion of weather snapshots into citable source documents."""

from __future__ import annotations

from ..air_quality import air_quality_to_document
from ..allergen import allergen_to_document
from ..languages import localized_labels
from ..localization import localization_table
from ..models import SourceDocument, WeatherContextSnapshot

_WEATHER_DOCUMENT_LABELS = localization_table("weather_document")


def snapshot_to_documents(snapshot: WeatherContextSnapshot) -> tuple[SourceDocument, ...]:
    """Convert a weather snapshot into citable LLM source documents."""
    labels = localized_labels(snapshot.output_language, _WEATHER_DOCUMENT_LABELS)
    weather = "\n".join(f"- {item}" for item in snapshot.weather_forecast)
    lifestyle = "\n".join(f"- {item}" for item in snapshot.lifestyle_advice) or labels["unavailable"]
    weather_summary = snapshot.weather_forecast[0] if snapshot.weather_forecast else labels["unavailable"]
    separator = labels["separator"]
    section_separator = labels["section_separator"]
    history_value = (
        f"{labels['forecast']}{section_separator}\n{weather}\n{labels['lifestyle']}{section_separator}\n{lifestyle}"
    )
    documents = [
        SourceDocument(
            id=snapshot.source_id,
            name=snapshot.source_name,
            url=snapshot.source_url,
            has_allergen_information=snapshot.allergen_advice_available,
            content=(
                f"{labels['updated_at']}{separator}{snapshot.observed_at.to_iso8601_string()}\n"
                f"{labels['forecast']}{section_separator}\n{weather}\n"
                f"{labels['lifestyle']}{section_separator}\n{lifestyle}"
            ),
            language=snapshot.output_language,
            history_summary=(
                f"{labels['updated_at']}{separator}{snapshot.observed_at.to_iso8601_string()}\n"
                f"{labels['summary']}{separator}{weather_summary}\n"
                f"{labels['lifestyle_count']}{separator}{len(snapshot.lifestyle_advice)}"
            ),
            history_value=history_value,
        )
    ]
    if snapshot.air_quality is not None:
        documents.append(air_quality_to_document(snapshot.air_quality))
    if snapshot.allergen is not None:
        documents.append(allergen_to_document(snapshot.allergen))
    return tuple(documents)
