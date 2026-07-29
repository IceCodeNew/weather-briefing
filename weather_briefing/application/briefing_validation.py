"""Rendered briefing output constraints."""

from __future__ import annotations

from collections.abc import Callable

from ..delivery import DeliveryProvider
from ..llm import LLMError
from ..models import AdviceTopic, Article, BriefingResult, SourceDocument


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


def briefing_result_validator(
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
            raise LLMError("briefing must not repeat lifestyle advice")
        missing_advice_topics = set(required_topics) - {item.topic for item in candidate.advice}
        if missing_advice_topics:
            missing = ", ".join(sorted(topic.value for topic in missing_advice_topics))
            raise LLMError(f"forecast advice is missing required topics: {missing}")
        if any(
            item.topic is AdviceTopic.ALLERGEN and allergen_source_ids.isdisjoint(item.source_ids)
            for item in candidate.advice
        ):
            raise LLMError("allergen advice must cite a current allergen-capable source")
        if not delivery.briefing_fits(candidate_message, configured_max_characters):
            raise LLMError(
                f"briefing has {candidate_message.visible_length} visible characters; "
                f"limit is {delivery_limit}; rendered fields do not fit the delivery chunks"
            )

    return validate
