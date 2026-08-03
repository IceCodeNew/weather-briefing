"""Rendered briefing output constraints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.llm import LLMError
from weather_briefing.models import AdviceTopic, Article, BriefingResult, SourceDocument

if TYPE_CHECKING:
    from collections.abc import Callable

    from weather_briefing.delivery import DeliveryProvider


def required_advice_topics(
    kind: str,
    context: tuple[SourceDocument, ...],
) -> tuple[AdviceTopic, ...]:
    """Return forecast advice topics required by available source context."""
    if kind != "forecast":
        return ()
    topics = [
        AdviceTopic.CLOTHING,
        AdviceTopic.DEHUMIDIFICATION,
        AdviceTopic.EXERCISE,
        AdviceTopic.MASK,
    ]
    if any(document.has_allergen_information for document in context):
        topics.append(AdviceTopic.ALLERGEN)
    return tuple(topics)


def briefing_result_validator(  # noqa: PLR0913
    *,
    kind: str,
    delivery: DeliveryProvider,
    source_articles: tuple[Article, ...],
    reference_context: tuple[SourceDocument, ...],
    required_topics: tuple[AdviceTopic, ...],
    allergen_source_ids: set[str],
    configured_max_characters: int,
    delivery_limit: int,
) -> Callable[[BriefingResult], None]:
    """Build a validator for domain and rendered-delivery constraints."""

    def validate(candidate: BriefingResult) -> None:
        candidate_message = delivery.render_briefing(candidate, source_articles, reference_context)
        if kind == "briefing" and candidate.advice:
            msg = "briefing must not repeat lifestyle advice"
            raise LLMError(msg)
        missing_advice_topics = set(required_topics) - {item.topic for item in candidate.advice}
        if missing_advice_topics:
            missing = ", ".join(sorted(topic.value for topic in missing_advice_topics))
            msg = f"forecast advice is missing required topics: {missing}"
            raise LLMError(msg)
        if any(
            item.topic is AdviceTopic.ALLERGEN and allergen_source_ids.isdisjoint(item.source_ids)
            for item in candidate.advice
        ):
            msg = "allergen advice must cite a current allergen-capable source"
            raise LLMError(msg)
        if not delivery.briefing_fits(candidate_message, configured_max_characters):
            msg = (
                f"briefing has {candidate_message.visible_length} visible characters; "
                f"limit is {delivery_limit}; rendered fields do not fit the delivery chunks"
            )
            raise LLMError(msg)

    return validate
