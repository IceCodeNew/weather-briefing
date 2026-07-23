"""Telegram Bot API delivery adapter."""

from __future__ import annotations

import logging
from html.parser import HTMLParser

import httpx

from ..api_client import api_call_extensions
from ..models import RenderedMessage
from .base import DeliveryError, RenderedTextDiagnostics, rendered_text_logging_enabled
from .telegram_reference import telegram_error_classification

_LOGGER = logging.getLogger("weather_briefing.publishers")


class TelegramPublisher:
    """Publish rendered HTML messages through the Telegram Bot API."""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        diagnostics: RenderedTextDiagnostics | None = None,
    ) -> None:
        """Configure Telegram delivery and optional sensitive-text diagnostics."""
        telegram_error_classification()
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
        """Publish one message, splitting it only when allowed."""
        if single_message and message.visible_length > self.MAX_MESSAGE_LENGTH:
            raise DeliveryError(
                "Telegram single message exceeds the platform limit",
                reason="message-too-long",
            )
        chunks = (message.body,) if single_message else split_message(message.body, self.MAX_MESSAGE_LENGTH)
        _LOGGER.info(
            "Telegram delivery prepared: visible_characters=%d payload_characters=%d chunks=%d "
            "single_message=%s silent=%s",
            message.visible_length,
            len(message.body),
            len(chunks),
            single_message,
            silent,
        )
        log_rendered_text = rendered_text_logging_enabled(self._diagnostics)
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
                    extensions=api_call_extensions(
                        "telegram",
                        "send-message",
                        response_error_handled=True,
                    ),
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                reason, channel_unavailable = telegram_error_reason(exc.response)
                _LOGGER.warning(
                    "Telegram delivery rejected index=%d/%d message_visible_characters=%d payload_characters=%d "
                    "status_code=%d reason=%s",
                    index,
                    len(chunks),
                    message.visible_length,
                    len(chunk),
                    exc.response.status_code,
                    reason,
                )
                raise DeliveryError(
                    f"Telegram delivery failed ({reason})",
                    reason=reason,
                    channel_unavailable=channel_unavailable,
                ) from None
            except httpx.RequestError as exc:
                _LOGGER.info(
                    "Telegram delivery request failed index=%d/%d message_visible_characters=%d payload_characters=%d "
                    "reason=%s",
                    index,
                    len(chunks),
                    message.visible_length,
                    len(chunk),
                    type(exc).__name__,
                )
                raise DeliveryError(
                    "Telegram delivery failed (request-error)",
                    reason="request-error",
                ) from None
            _LOGGER.debug(
                "Telegram chunk accepted: index=%d/%d payload_characters=%d",
                index,
                len(chunks),
                len(chunk),
            )


def telegram_error_reason(response: httpx.Response) -> tuple[str, bool]:
    """Classify a Telegram API error without logging its response body."""
    classification = telegram_error_classification()
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and type(parameters.get("migrate_to_chat_id")) is int:
            reason = classification.parameter_reasons["migrate_to_chat_id"]
            return reason, reason in classification.channel_unavailable_reasons

        description = payload.get("description")
        if isinstance(description, str):
            normalized = description.casefold()
            for marker, reason in classification.description_markers:
                if marker in normalized:
                    return reason, reason in classification.channel_unavailable_reasons

    reason = classification.status_reasons.get(response.status_code, "api-error")
    return reason, reason in classification.channel_unavailable_reasons


def split_message(body: str, limit: int) -> tuple[str, ...]:
    """Split Telegram HTML into independently valid chunks."""
    if limit <= 0:
        raise ValueError("Message split limit must be positive")
    if len(body) <= limit:
        return (body,)
    chunker = _TelegramHTMLChunker(limit)
    chunker.feed(body)
    chunker.close()
    return chunker.finish()


class _TelegramHTMLChunker(HTMLParser):
    """Split Telegram HTML while making every chunk independently valid."""

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=False)
        self._limit = limit
        self._chunks: list[str] = []
        self._parts: list[str] = []
        self._open_tags: list[tuple[str, str]] = []
        self._visible_length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._visible_length == self._limit:
            self._finish_chunk()
        start_tag = self.get_starttag_text()
        assert start_tag is not None
        self._parts.append(start_tag)
        self._open_tags.append((tag, start_tag))

    def handle_endtag(self, tag: str) -> None:
        self._parts.append(f"</{tag}>")
        self._open_tags.pop()

    def handle_data(self, data: str) -> None:
        while True:
            available = self._limit - self._visible_length
            if available == 0:
                self._finish_chunk()
                available = self._limit
            if len(data) <= available:
                self._parts.append(data)
                self._visible_length += len(data)
                return
            split_at = data.rfind("\n", 0, available + 1)
            if split_at > 0:
                self._parts.append(data[:split_at])
                self._visible_length += split_at
                data = data[split_at:]
            else:
                self._parts.append(data[:available])
                self._visible_length += available
                data = data[available:]
            self._finish_chunk()

    def handle_entityref(self, name: str) -> None:
        self._append_entity(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append_entity(f"&#{name};")

    def finish(self) -> tuple[str, ...]:
        """Flush and return all accumulated chunks."""
        self._finish_chunk()
        return tuple(self._chunks)

    def _append_entity(self, value: str) -> None:
        if self._visible_length == self._limit:
            self._finish_chunk()
        self._parts.append(value)
        self._visible_length += 1

    def _finish_chunk(self) -> None:
        if self._visible_length == 0:
            return
        closing_tags = (f"</{tag}>" for tag, _ in reversed(self._open_tags))
        self._chunks.append("".join((*self._parts, *closing_tags)))
        self._parts = [start_tag for _, start_tag in self._open_tags]
        self._visible_length = 0
