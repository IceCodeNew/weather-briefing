"""Shared contracts and primitives for platform-specific renderers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..languages import LanguageSupport
from ..localization import localization_table
from ..models import Article, BriefingResult, RenderedMessage, SourceDocument

_BRIEFING_LABELS = localization_table("briefing")
_BRIEFING_LANGUAGE_SUPPORT = LanguageSupport(
    default="en",
    supported=tuple(_BRIEFING_LABELS),
)


class MessageRenderer(Protocol):
    """Render platform-neutral briefing data for one delivery platform."""

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a validated briefing and its citable references."""
        ...

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render an article without summarizing its cleaned content."""
        ...

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert."""
        ...


def briefing_labels(language: str) -> Mapping[str, str]:
    """Return localized labels for the closest supported language."""
    selected = _BRIEFING_LANGUAGE_SUPPORT.match(language)
    return _BRIEFING_LABELS[selected]


def article_source_name(article: Article) -> str:
    """Return a visible article source name with a stable fallback."""
    return article.source_name.strip() or article.source_id


def ordered_source_ids(result: BriefingResult) -> list[str]:
    """Return cited source IDs in first-visible-use order."""
    ordered = list(result.headline_source_ids)
    for items in (result.conclusions, result.active_warnings, result.disaster_tracking, result.advice):
        for item in items:
            ordered.extend(item.source_ids)
    return list(dict.fromkeys(ordered))


def plain_attribution(
    source_ids: tuple[str, ...],
    source_references: dict[str, str],
    labels: Mapping[str, str],
    *,
    numbered: bool = False,
) -> str:
    """Render one deduplicated plain-text source attribution."""
    source_values = tuple(dict.fromkeys(source_references[source_id] for source_id in source_ids))
    if numbered:
        return "".join(source_values)
    sources = labels["plain_source_separator"].join(source_values)
    return labels["attribution"].format(sources=sources)


def plain_message(body: str, *, title: str | None = None) -> RenderedMessage:
    """Build a plain rendered message with its platform-visible length."""
    normalized_title = title.strip() or None if title is not None else None
    return RenderedMessage(
        body=body,
        visible_length=len(body) + len(normalized_title or ""),
        title=normalized_title,
    )
