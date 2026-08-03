"""Compact Bark plain-text rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .plain_renderer import PlainTextRenderer
from .rendering import article_source_name, briefing_labels, ordered_source_ids, plain_attribution, plain_message

if TYPE_CHECKING:
    from collections.abc import Mapping

    from weather_briefing.models import Advice, Article, BriefingResult, Conclusion, RenderedMessage, SourceDocument


class BarkTextRenderer(PlainTextRenderer):
    """Render compact Bark briefings without source URLs."""

    def __init__(self) -> None:
        """Omit source URLs from Bark briefing attributions."""
        super().__init__(include_source_urls=False, number_sources=True)

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a compact briefing intended for at most two Bark messages."""
        labels = briefing_labels(result.output_language)
        source_references = {
            article.id: self._source_reference(article_source_name(article), article.url)
            for article in reference_articles
        }
        source_references.update(
            {document.id: self._source_reference(document.name, document.url) for document in context}
        )
        numbered_references, source_footer = _bark_numbered_source_references(result, source_references)
        title = (
            f"{result.headline} "
            f"{plain_attribution(result.headline_source_ids, numbered_references, labels, numbered=True)}"
        )
        lines: list[str] = []
        lines.extend(_compact_plain_items(None, result.conclusions, numbered_references, labels))
        if result.active_warnings:
            lines.append(labels["warnings"])
            lines.extend(
                f"{warning.title}{labels['status_open']}{warning.status}{labels['status_close']}"
                f"{labels['detail_separator']}{warning.detail} "
                f"{plain_attribution(warning.source_ids, numbered_references, labels, numbered=True)}"
                for warning in result.active_warnings
            )
        lines.extend(_compact_plain_items(labels["disasters"], result.disaster_tracking, numbered_references, labels))
        lines.extend(_compact_plain_items(labels["advice"], result.advice, numbered_references, labels))
        lines.append(source_footer)
        return plain_message("\n".join(lines).strip(), title=title.strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render an article with its title in Bark's title field."""
        return plain_message(article.content.strip(), title=article.title)

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert with separate Bark title and body fields."""
        return plain_message(body.strip(), title=title)


def _compact_plain_items(
    title: str | None,
    items: tuple[Conclusion | Advice, ...],
    source_references: dict[str, str],
    labels: Mapping[str, str],
) -> list[str]:
    if not items:
        return []
    lines = [title] if title is not None else []
    lines.extend(
        f"{item.text} {plain_attribution(item.source_ids, source_references, labels, numbered=True)}" for item in items
    )
    return lines


def _bark_numbered_source_references(
    result: BriefingResult,
    source_references: dict[str, str],
) -> tuple[dict[str, str], str]:
    numbered_references: dict[str, str] = {}
    numbers_by_name: dict[str, str] = {}
    source_lines: list[str] = []
    for source_id in ordered_source_ids(result):
        source_name = " ".join(source_references[source_id].split()) or source_id
        normalized_name = source_name.casefold()
        number = numbers_by_name.get(normalized_name)
        if number is None:
            number = f"[{len(numbers_by_name) + 1}]"
            numbers_by_name[normalized_name] = number
            source_lines.append(f"{number} {source_name}")
        numbered_references[source_id] = number
    return numbered_references, "\n".join(source_lines)
