"""Platform-specific rendering of validated briefing results."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape, unescape
from typing import Protocol

from bs4 import BeautifulSoup

from ..languages import LanguageSupport
from ..localization import localization_table
from ..models import (
    Advice,
    Article,
    BriefingResult,
    Conclusion,
    RenderedMessage,
    SourceDocument,
)

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


class TelegramHTMLRenderer:
    """Render briefings as Telegram-compatible HTML."""

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a sourced briefing as Telegram HTML."""
        labels = _briefing_labels(result.output_language)
        source_links = {
            article.id: _html_link(article.url, _article_source_name(article)) for article in reference_articles
        }
        source_links.update({document.id: _html_link(document.url, document.name) for document in context})
        lines = [
            f"<b>{_html_text(result.headline)}</b> "
            f"{_html_attribution(result.headline_source_ids, source_links, labels)}",
            "",
        ]
        lines.extend(_html_items(labels["weather"], result.conclusions, source_links, labels))
        if result.active_warnings:
            lines.extend([f"<b>{labels['warnings']}</b>", ""])
            lines.extend(
                (
                    f"• <b>{_html_text(warning.title)}{labels['status_open']}"
                    f"{_html_text(warning.status)}{labels['status_close']}</b>"
                    f"{labels['detail_separator']}{_html_text(warning.detail)} "
                    f"{_html_attribution(warning.source_ids, source_links, labels)}"
                )
                for warning in result.active_warnings
            )
            lines.append("")
        lines.extend(_html_items(labels["disasters"], result.disaster_tracking, source_links, labels))
        lines.extend(_html_items(labels["advice"], result.advice, source_links, labels))
        return _html_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render cleaned article content as Telegram HTML."""
        return _html_message(
            "\n".join(
                (
                    f"<b>{_html_text(article.title)}</b>",
                    "",
                    _html_text(article.content),
                )
            )
        )

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an escaped Telegram HTML alert."""
        return _html_message(f"<b>{_html_text(title)}</b>\n\n{_html_text(body)}")


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
        labels = _briefing_labels(result.output_language)
        source_references = {
            article.id: self._source_reference(_article_source_name(article), article.url)
            for article in reference_articles
        }
        source_references.update(
            {document.id: self._source_reference(document.name, document.url) for document in context}
        )
        source_footer = None
        if self._number_sources:
            source_references, source_footer = _numbered_source_references(result, source_references, labels)
        headline_sources = _plain_attribution(
            result.headline_source_ids,
            source_references,
            labels,
            numbered=self._number_sources,
        )
        lines = [
            f"{result.headline} {headline_sources}",
            "",
        ]
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
                sources = _plain_attribution(
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
        return _plain_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render cleaned article content as plain text."""
        return _plain_message(f"{article.title}\n\n{article.content}")

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert as plain text."""
        return _plain_message(f"{title}\n\n{body}")

    def _source_reference(self, name: str, url: str) -> str:
        if not self._include_source_urls:
            return name
        return f"{name}: {url}"


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
        labels = _briefing_labels(result.output_language)
        source_references = {
            article.id: self._source_reference(_article_source_name(article), article.url)
            for article in reference_articles
        }
        source_references.update(
            {document.id: self._source_reference(document.name, document.url) for document in context}
        )
        numbered_references, source_footer = _bark_numbered_source_references(result, source_references)
        lines = [
            f"{result.headline} "
            f"{_plain_attribution(result.headline_source_ids, numbered_references, labels, numbered=True)}"
        ]
        lines.extend(_compact_plain_items(None, result.conclusions, numbered_references, labels))
        if result.active_warnings:
            lines.append(labels["warnings"])
            lines.extend(
                f"{warning.title}{labels['status_open']}{warning.status}{labels['status_close']}"
                f"{labels['detail_separator']}{warning.detail} "
                f"{_plain_attribution(warning.source_ids, numbered_references, labels, numbered=True)}"
                for warning in result.active_warnings
            )
        lines.extend(_compact_plain_items(labels["disasters"], result.disaster_tracking, numbered_references, labels))
        lines.extend(_compact_plain_items(labels["advice"], result.advice, numbered_references, labels))
        lines.append(source_footer)
        return _plain_message("\n".join(lines).strip())


def _html_text(value: str) -> str:
    return escape(unescape(value), quote=False)


def _article_source_name(article: Article) -> str:
    return article.source_name.strip() or article.source_id


def _html_link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{_html_text(label)}</a>'


def _html_items(
    title: str,
    items: tuple[Conclusion | Advice, ...],
    source_links: dict[str, str],
    labels: Mapping[str, str],
) -> list[str]:
    if not items:
        return []
    lines = [f"<b>{_html_text(title)}</b>", ""]
    lines.extend(
        f"• {_html_text(item.text)} {_html_attribution(item.source_ids, source_links, labels)}" for item in items
    )
    lines.append("")
    return lines


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
        f"- {item.text} {_plain_attribution(item.source_ids, source_references, labels, numbered=numbered_sources)}"
        for item in items
    )
    lines.append("")
    return lines


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
        f"{item.text} {_plain_attribution(item.source_ids, source_references, labels, numbered=True)}" for item in items
    )
    return lines


def _html_attribution(
    source_ids: tuple[str, ...],
    source_links: dict[str, str],
    labels: Mapping[str, str],
) -> str:
    sources = labels["html_source_separator"].join(dict.fromkeys(source_links[source_id] for source_id in source_ids))
    return labels["attribution"].format(sources=sources)


def _plain_attribution(
    source_ids: tuple[str, ...],
    source_references: dict[str, str],
    labels: Mapping[str, str],
    *,
    numbered: bool = False,
) -> str:
    source_values = tuple(dict.fromkeys(source_references[source_id] for source_id in source_ids))
    if numbered:
        return "".join(source_values)
    sources = labels["plain_source_separator"].join(source_values)
    return labels["attribution"].format(sources=sources)


def _numbered_source_references(
    result: BriefingResult,
    source_references: dict[str, str],
    labels: Mapping[str, str],
) -> tuple[dict[str, str], str]:
    ordered_source_ids = _ordered_source_ids(result)
    numbered_references = {source_id: f"[{index}]" for index, source_id in enumerate(ordered_source_ids, start=1)}
    source_list = labels["plain_source_separator"].join(
        f"{numbered_references[source_id]} {source_references[source_id]}" for source_id in ordered_source_ids
    )
    footer = f"{labels['sources']}{labels['detail_separator']}{source_list}"
    return numbered_references, footer


def _bark_numbered_source_references(
    result: BriefingResult,
    source_references: dict[str, str],
) -> tuple[dict[str, str], str]:
    numbered_references: dict[str, str] = {}
    numbers_by_name: dict[str, str] = {}
    source_lines: list[str] = []
    for source_id in _ordered_source_ids(result):
        source_name = " ".join(source_references[source_id].split())
        normalized_name = source_name.casefold()
        number = numbers_by_name.get(normalized_name)
        if number is None:
            number = f"[{len(numbers_by_name) + 1}]"
            numbers_by_name[normalized_name] = number
            source_lines.append(f"{number} {source_name}")
        numbered_references[source_id] = number
    return numbered_references, "\n".join(source_lines)


def _ordered_source_ids(result: BriefingResult) -> list[str]:
    ordered_source_ids = list(result.headline_source_ids)
    for items in (result.conclusions, result.active_warnings, result.disaster_tracking, result.advice):
        for item in items:
            ordered_source_ids.extend(item.source_ids)
    return list(dict.fromkeys(ordered_source_ids))


def _briefing_labels(language: str) -> Mapping[str, str]:
    selected = _BRIEFING_LANGUAGE_SUPPORT.match(language)
    return _BRIEFING_LABELS[selected]


def _html_message(body: str) -> RenderedMessage:
    visible = BeautifulSoup(body, "html.parser").get_text()
    return RenderedMessage(body=body, visible_length=len(visible))


def _plain_message(body: str) -> RenderedMessage:
    return RenderedMessage(body=body, visible_length=len(body))
