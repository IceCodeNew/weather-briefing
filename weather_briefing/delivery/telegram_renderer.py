"""Telegram HTML rendering."""

from __future__ import annotations

from html import escape, unescape
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from weather_briefing.models import Advice, Article, BriefingResult, Conclusion, RenderedMessage, SourceDocument

from .rendering import article_source_name, briefing_labels

if TYPE_CHECKING:
    from collections.abc import Mapping


class TelegramHTMLRenderer:
    """Render briefings as Telegram-compatible HTML."""

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a sourced briefing as Telegram HTML."""
        labels = briefing_labels(result.output_language)
        source_links = {
            article.id: _html_link(article.url, article_source_name(article)) for article in reference_articles
        }
        source_links.update({document.id: _html_link(document.url, document.name) for document in context})
        lines = [
            (
                f"<b>{_html_text(result.headline)}</b> "
                f"{_html_attribution(result.headline_source_ids, source_links, labels)}"
            ),
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


def _html_text(value: str) -> str:
    return escape(unescape(value), quote=False)


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


def _html_attribution(
    source_ids: tuple[str, ...],
    source_links: dict[str, str],
    labels: Mapping[str, str],
) -> str:
    sources = labels["html_source_separator"].join(dict.fromkeys(source_links[source_id] for source_id in source_ids))
    return labels["attribution"].format(sources=sources)


def _html_message(body: str) -> RenderedMessage:
    visible = BeautifulSoup(body, "html.parser").get_text()
    return RenderedMessage(body=body, visible_length=len(visible))
