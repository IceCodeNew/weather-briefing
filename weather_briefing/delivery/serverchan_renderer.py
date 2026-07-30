"""Markdown rendering for the two ServerChan products."""

from __future__ import annotations

import re

from ..data.serverchan import SERVERCHAN_TITLE_MAX_CHARACTERS
from ..models import Article, BriefingResult, RenderedMessage, SourceDocument
from .plain_renderer import PlainTextRenderer
from .rendering import plain_message

_DEFAULT_TITLE = "weather-briefing"


class _ServerChanRenderer(PlainTextRenderer):
    """Share ServerChan title handling without merging product adapters."""

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a sourced Markdown briefing with a separate API title."""
        rendered = super().render_briefing(result, reference_articles, context)
        return self._message(rendered.body, result.headline)

    def render_verbatim(self, article: Article) -> RenderedMessage:
        """Render an article with its title in the API title field."""
        return self._message(article.content, article.title)

    def render_alert(self, title: str, body: str) -> RenderedMessage:
        """Render an operational alert with separate title and body fields."""
        return self._message(body, title)

    def _message(self, body: str, title: str) -> RenderedMessage:
        return plain_message(self._format_body(body), title=_serverchan_title(title))

    def _format_body(self, body: str) -> str:
        return body.strip()


class ServerChanTurboRenderer(_ServerChanRenderer):
    """Render Markdown for ServerChan Turbo."""


class ServerChan3Renderer(_ServerChanRenderer):
    """Render Markdown using ServerChan 3's double-newline convention."""

    def _format_body(self, body: str) -> str:
        return re.sub(r"\n+", "\n\n", body.strip())


def _serverchan_title(title: str) -> str:
    normalized = " ".join(title.split()) or _DEFAULT_TITLE
    return normalized[:SERVERCHAN_TITLE_MAX_CHARACTERS]
