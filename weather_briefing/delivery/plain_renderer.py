"""General plain-text rendering."""

from __future__ import annotations

from collections.abc import Mapping

from ..models import Advice, Article, BriefingResult, Conclusion, RenderedMessage, SourceDocument
from .rendering import (
    article_source_name,
    briefing_labels,
    ordered_source_ids,
    plain_attribution,
    plain_message,
)


class PlainTextRenderer:
    """Render briefings for stdout and other plain-text transports."""

    def __init__(self, *, include_source_urls: bool = True, number_sources: bool = False) -> None:
        """Configure whether briefing attributions include source URLs."""
        self._include_source_urls = include_source_urls
        self._number_sources = number_sources

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a sourced briefing as plain text."""
        labels = briefing_labels(result.output_language)
        source_references = {
            article.id: self._source_reference(article_source_name(article), article.url)
            for article in reference_articles
        }
        source_references.update(
            {document.id: self._source_reference(document.name, document.url) for document in context}
        )
        source_footer = None
        if self._number_sources:
            source_references, source_footer = _numbered_source_references(result, source_references, labels)
        headline_sources = plain_attribution(
            result.headline_source_ids,
            source_references,
            labels,
            numbered=self._number_sources,
        )
        lines = [f"{result.headline} {headline_sources}", ""]
        lines.extend(
            _plain_items(
                labels["weather"],
                result.conclusions,
                source_references,
                labels,
                numbered_sources=self._number_sources,
            )
        )
        if result.active_warnings:
            lines.extend([labels["warnings"], ""])
            for warning in result.active_warnings:
                sources = plain_attribution(
                    warning.source_ids,
                    source_references,
                    labels,
                    numbered=self._number_sources,
                )
                lines.append(
                    f"- {warning.title}{labels['status_open']}{warning.status}{labels['status_close']}"
                    f"{labels['detail_separator']}{warning.detail} {sources}"
                )
            lines.append("")
        lines.extend(
            _plain_items(
                labels["disasters"],
                result.disaster_tracking,
                source_references,
                labels,
                numbered_sources=self._number_sources,
            )
        )
        lines.extend(
            _plain_items(
                labels["advice"],
                result.advice,
                source_references,
                labels,
                numbered_sources=self._number_sources,
            )
        )
        if source_footer is not None:
            lines.append(source_footer)
        return plain_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render cleaned article content as plain text."""
        return plain_message(f"{article.title}\n\n{article.content}")

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert as plain text."""
        return plain_message(f"{title}\n\n{body}")

    def _source_reference(self, name: str, url: str) -> str:
        if not self._include_source_urls:
            return name
        return f"{name}: {url}"


def _plain_items(
    title: str,
    items: tuple[Conclusion | Advice, ...],
    source_references: dict[str, str],
    labels: Mapping[str, str],
    *,
    numbered_sources: bool = False,
) -> list[str]:
    if not items:
        return []
    lines = [title, ""]
    lines.extend(
        f"- {item.text} {plain_attribution(item.source_ids, source_references, labels, numbered=numbered_sources)}"
        for item in items
    )
    lines.append("")
    return lines


def _numbered_source_references(
    result: BriefingResult,
    source_references: dict[str, str],
    labels: Mapping[str, str],
) -> tuple[dict[str, str], str]:
    source_ids = ordered_source_ids(result)
    numbered_references = {source_id: f"[{index}]" for index, source_id in enumerate(source_ids, start=1)}
    source_list = labels["plain_source_separator"].join(
        f"{numbered_references[source_id]} {source_references[source_id]}" for source_id in source_ids
    )
    footer = f"{labels['sources']}{labels['detail_separator']}{source_list}"
    return numbered_references, footer
