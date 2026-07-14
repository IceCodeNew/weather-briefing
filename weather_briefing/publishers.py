from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .api_client import api_call_extensions
from .models import Article, BriefingResult, RenderedMessage, SourceDocument
from .render import MessageRenderer

_LOGGER = logging.getLogger("weather_briefing.publishers")


class Publisher(Protocol):
    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None: ...


class RenderedTextDiagnostics(Protocol):
    def rendered_text_logging_enabled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeliveryProvider:
    """Bind a platform renderer to its message transport."""

    renderer: MessageRenderer
    publisher: Publisher
    single_message_limit: int | None = None
    diagnostics: RenderedTextDiagnostics | None = None

    def briefing_limit(self, configured_limit: int) -> int:
        if self.single_message_limit is None:
            return configured_limit
        return min(configured_limit, self.single_message_limit)

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        return self.renderer.render_briefing(result, reference_articles, context)

    async def publish_rendered(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        _log_rendered_text(self.diagnostics, "briefing", message.body)
        await self.publisher.publish(message, single_message=single_message, silent=silent)

    async def publish_verbatim(self, article: Article, *, silent: bool = False) -> None:
        message = self.renderer.render_verbatim(article)
        _LOGGER.debug(
            "Rendered verbatim message: visible_characters=%d payload_characters=%d",
            message.visible_length,
            len(message.body),
        )
        _log_rendered_text(self.diagnostics, "verbatim", message.body)
        await self.publisher.publish(message, silent=silent)

    async def publish_alert(self, title: str, body: str) -> None:
        message = self.renderer.render_alert(title, body)
        _log_rendered_text(self.diagnostics, "alert", message.body)
        await self.publisher.publish(message)


class StdoutPublisher:
    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        print(message.body)


class DeliveryError(RuntimeError):
    """Raised without exposing private delivery endpoint details."""


class TelegramPublisher:
    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        diagnostics: RenderedTextDiagnostics | None = None,
    ) -> None:
        self._client = client
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._diagnostics = diagnostics

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        if single_message and message.visible_length > self.MAX_MESSAGE_LENGTH:
            raise DeliveryError("Telegram single message exceeds the platform limit")
        chunks = (message.body,) if single_message else _split_message(message.body, self.MAX_MESSAGE_LENGTH)
        _LOGGER.debug(
            "Telegram delivery prepared: visible_characters=%d payload_characters=%d chunks=%d single_message=%s",
            message.visible_length,
            len(message.body),
            len(chunks),
            single_message,
        )
        log_rendered_text = _rendered_text_logging_enabled(self._diagnostics)
        for index, chunk in enumerate(chunks, start=1):
            if log_rendered_text:
                _LOGGER.debug(
                    "Sensitive rendered text diagnostic: stage=telegram-chunk-%d-of-%d body=%r",
                    index,
                    len(chunks),
                    chunk,
                )
            try:
                response = await self._client.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "link_preview_options": {"is_disabled": True},
                        "disable_notification": silent,
                    },
                    extensions=api_call_extensions("telegram", "send-message"),
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise DeliveryError("Telegram delivery failed") from None
            _LOGGER.debug(
                "Telegram chunk accepted: index=%d/%d payload_characters=%d",
                index,
                len(chunks),
                len(chunk),
            )


def _log_rendered_text(
    diagnostics: RenderedTextDiagnostics | None,
    stage: str,
    body: str,
) -> None:
    if _rendered_text_logging_enabled(diagnostics):
        _LOGGER.debug("Sensitive rendered text diagnostic: stage=%s body=%r", stage, body)


def _rendered_text_logging_enabled(diagnostics: RenderedTextDiagnostics | None) -> bool:
    if diagnostics is None:
        return False
    try:
        enabled = diagnostics.rendered_text_logging_enabled()
    except Exception:
        _LOGGER.warning("Rendered text diagnostic state check failed", exc_info=True)
        return False
    return enabled and _LOGGER.isEnabledFor(logging.DEBUG)


def _split_message(body: str, limit: int) -> tuple[str, ...]:
    if len(body) <= limit:
        return (body,)
    chunks: list[str] = []
    remaining = body
    while remaining:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = _safe_html_boundary(remaining, limit)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return tuple(chunks)


def _safe_html_boundary(value: str, limit: int) -> int:
    boundary = limit
    last_entity_start = value.rfind("&", 0, boundary)
    last_entity_end = value.rfind(";", 0, boundary)
    if last_entity_start > last_entity_end:
        boundary = last_entity_start
    last_tag_start = value.rfind("<", 0, boundary)
    last_tag_end = value.rfind(">", 0, boundary)
    if last_tag_start > last_tag_end:
        boundary = last_tag_start
    return boundary or limit
