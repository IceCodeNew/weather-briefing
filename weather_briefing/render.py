from __future__ import annotations

from html import escape, unescape
from typing import Protocol

from bs4 import BeautifulSoup

from .models import (
    Article,
    BriefingResult,
    Conclusion,
    RenderedMessage,
    SourceDocument,
)


class MessageRenderer(Protocol):
    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage: ...

    def render_verbatim(self, article: Article) -> RenderedMessage: ...

    def render_alert(self, title: str, body: str) -> RenderedMessage: ...


class TelegramHTMLRenderer:
    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        source_urls = {article.id: article.url for article in reference_articles}
        source_urls.update({document.id: document.url for document in context})
        lines = [f"<b>{_html_text(result.headline)}</b>", "", _html_text(result.overview), ""]
        if result.active_warnings:
            lines.extend(["<b>当前生效的气象预警</b>", ""])
            for warning in result.active_warnings:
                links = " ".join(_html_link(source_urls[source_id]) for source_id in warning.source_ids)
                lines.append(
                    f"• <b>{_html_text(warning.title)}（{_html_text(warning.status)}）</b>："
                    f"{_html_text(warning.detail)} {links}".rstrip()
                )
            lines.append("")
        lines.extend(_html_items("天气信息", result.conclusions, source_urls))
        lines.extend(_html_items("灾害动态", result.disaster_tracking, source_urls))
        lines.extend(_html_items("生活建议", result.advice, source_urls))
        return _html_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
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
        return _html_message(f"<b>{_html_text(title)}</b>\n\n{_html_text(body)}")


class PlainTextRenderer:
    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        source_urls = {article.id: article.url for article in reference_articles}
        source_urls.update({document.id: document.url for document in context})
        lines = [result.headline, "", result.overview, ""]
        if result.active_warnings:
            lines.extend(["当前生效的气象预警", ""])
            for warning in result.active_warnings:
                sources = " ".join(source_urls[item] for item in warning.source_ids)
                lines.append(f"- {warning.title}（{warning.status}）：{warning.detail} {sources}".rstrip())
            lines.append("")
        lines.extend(_plain_items("天气信息", result.conclusions, source_urls))
        lines.extend(_plain_items("灾害动态", result.disaster_tracking, source_urls))
        lines.extend(_plain_items("生活建议", result.advice, source_urls))
        return _plain_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        return _plain_message(f"{article.title}\n\n{article.content}")

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        return _plain_message(f"{title}\n\n{body}")


def _html_text(value: str) -> str:
    return escape(unescape(value), quote=False)


def _html_link(url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">来源</a>'


def _html_items(title: str, items: tuple[Conclusion, ...], source_urls: dict[str, str]) -> list[str]:
    if not items:
        return []
    lines = [f"<b>{_html_text(title)}</b>", ""]
    for item in items:
        links = " ".join(_html_link(source_urls[source_id]) for source_id in item.source_ids)
        lines.append(f"• {_html_text(item.text)} {links}".rstrip())
    lines.append("")
    return lines


def _plain_items(title: str, items: tuple[Conclusion, ...], source_urls: dict[str, str]) -> list[str]:
    if not items:
        return []
    lines = [title, ""]
    for item in items:
        sources = " ".join(source_urls[source_id] for source_id in item.source_ids)
        lines.append(f"- {item.text} {sources}".rstrip())
    lines.append("")
    return lines


def _html_message(body: str) -> RenderedMessage:
    visible = BeautifulSoup(body, "html.parser").get_text()
    return RenderedMessage(body=body, visible_length=len(visible))


def _plain_message(body: str) -> RenderedMessage:
    return RenderedMessage(body=body, visible_length=len(body))
