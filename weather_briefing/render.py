from __future__ import annotations

from html import escape, unescape
from typing import Protocol

from bs4 import BeautifulSoup

from .models import (
    Advice,
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
        source_links = {
            article.id: _html_link(article.url, _article_source_name(article)) for article in reference_articles
        }
        source_links.update({document.id: _html_link(document.url, document.name) for document in context})
        lines = [
            f"<b>{_html_text(result.headline)}</b> {_html_attribution(result.headline_source_ids, source_links)}",
            "",
            f"{_html_text(result.overview)} {_html_attribution(result.overview_source_ids, source_links)}",
            "",
        ]
        if result.active_warnings:
            lines.extend(["<b>当前生效的气象预警</b>", ""])
            for warning in result.active_warnings:
                lines.append(
                    f"• <b>{_html_text(warning.title)}（{_html_text(warning.status)}）</b>："
                    f"{_html_text(warning.detail)} {_html_attribution(warning.source_ids, source_links)}"
                )
            lines.append("")
        lines.extend(_html_items("天气信息", result.conclusions, source_links))
        lines.extend(_html_items("灾害动态", result.disaster_tracking, source_links))
        lines.extend(_html_items("生活建议", result.advice, source_links))
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
        source_references = {
            article.id: f"{_article_source_name(article)}: {article.url}" for article in reference_articles
        }
        source_references.update({document.id: f"{document.name}: {document.url}" for document in context})
        lines = [
            f"{result.headline} {_plain_attribution(result.headline_source_ids, source_references)}",
            "",
            f"{result.overview} {_plain_attribution(result.overview_source_ids, source_references)}",
            "",
        ]
        if result.active_warnings:
            lines.extend(["当前生效的气象预警", ""])
            for warning in result.active_warnings:
                sources = _plain_attribution(warning.source_ids, source_references)
                lines.append(f"- {warning.title}（{warning.status}）：{warning.detail} {sources}")
            lines.append("")
        lines.extend(_plain_items("天气信息", result.conclusions, source_references))
        lines.extend(_plain_items("灾害动态", result.disaster_tracking, source_references))
        lines.extend(_plain_items("生活建议", result.advice, source_references))
        return _plain_message("\n".join(lines).strip())

    def render_verbatim(self, article: Article) -> RenderedMessage:
        return _plain_message(f"{article.title}\n\n{article.content}")

    def render_alert(self, title: str, body: str) -> RenderedMessage:
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
) -> list[str]:
    if not items:
        return []
    lines = [f"<b>{_html_text(title)}</b>", ""]
    for item in items:
        lines.append(f"• {_html_text(item.text)} {_html_attribution(item.source_ids, source_links)}")
    lines.append("")
    return lines


def _plain_items(
    title: str,
    items: tuple[Conclusion | Advice, ...],
    source_references: dict[str, str],
) -> list[str]:
    if not items:
        return []
    lines = [title, ""]
    for item in items:
        lines.append(f"- {item.text} {_plain_attribution(item.source_ids, source_references)}")
    lines.append("")
    return lines


def _html_attribution(source_ids: tuple[str, ...], source_links: dict[str, str]) -> str:
    sources = "、".join(source_links[source_id] for source_id in source_ids)
    return f"（来源：{sources}）"


def _plain_attribution(source_ids: tuple[str, ...], source_references: dict[str, str]) -> str:
    sources = "；".join(source_references[source_id] for source_id in source_ids)
    return f"（来源：{sources}）"


def _html_message(body: str) -> RenderedMessage:
    visible = BeautifulSoup(body, "html.parser").get_text()
    return RenderedMessage(body=body, visible_length=len(visible))


def _plain_message(body: str) -> RenderedMessage:
    return RenderedMessage(body=body, visible_length=len(body))
