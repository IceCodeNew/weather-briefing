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

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a sourced briefing as plain text."""
        labels = _briefing_labels(result.output_language)
        source_references = {
            article.id: f"{_article_source_name(article)}: {article.url}" for article in reference_articles
        }
        source_references.update({document.id: f"{document.name}: {document.url}" for document in context})
        lines = [
            f"{result.headline} {_plain_attribution(result.headline_source_ids, source_references, labels)}",
            "",
        ]
        lines.extend(_plain_items(labels["weather"], result.conclusions, source_references, labels))
        if result.active_warnings:
            lines.extend([labels["warnings"], ""])
            for warning in result.active_warnings:
                sources = _plain_attribution(warning.source_ids, source_references, labels)
                lines.append(
                    f"- {warning.title}{labels['status_open']}{warning.status}{labels['status_close']}"
                    f"{labels['detail_separator']}{warning.detail} {sources}"
                )
            lines.append("")
        lines.extend(_plain_items(labels["disasters"], result.disaster_tracking, source_references, labels))
        lines.extend(_plain_items(labels["advice"], result.advice, source_references, labels))
        return _plain_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render cleaned article content as plain text."""
        return _plain_message(f"{article.title}\n\n{article.content}")

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert as plain text."""
        return _plain_message(f"{title}\n\n{body}")


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
) -> list[str]:
    if not items:
        return []
    lines = [title, ""]
    lines.extend(f"- {item.text} {_plain_attribution(item.source_ids, source_references, labels)}" for item in items)
    lines.append("")
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
) -> str:
    sources = labels["plain_source_separator"].join(
        dict.fromkeys(source_references[source_id] for source_id in source_ids)
    )
    return labels["attribution"].format(sources=sources)


def _briefing_labels(language: str) -> Mapping[str, str]:
    selected = _BRIEFING_LANGUAGE_SUPPORT.match(language)
    return _BRIEFING_LABELS[selected]


def _html_message(body: str) -> RenderedMessage:
    visible = BeautifulSoup(body, "html.parser").get_text()
    return RenderedMessage(body=body, visible_length=len(visible))


def _plain_message(body: str) -> RenderedMessage:
    return RenderedMessage(body=body, visible_length=len(body))
