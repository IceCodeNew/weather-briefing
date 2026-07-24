"""Platform-neutral message delivery composition."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Protocol

from ..models import Article, BriefingResult, RenderedMessage, SourceDocument
from .renderers import MessageRenderer

_LOGGER = logging.getLogger("weather_briefing.publishers")
_SAFE_DELIVERY_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class Publisher(Protocol):
    """Transport a rendered message to its destination."""

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        """Publish one rendered message with delivery hints."""
        ...


class RenderedTextDiagnostics(Protocol):
    """Expose the runtime switch for sensitive rendered-text logging."""

    def rendered_text_logging_enabled(self) -> bool:
        """Return whether sensitive rendered-text logging is enabled."""
        ...


@dataclass(frozen=True, slots=True)
class DeliveryProvider:
    """Bind a platform renderer to its message transport."""

    renderer: MessageRenderer
    publisher: Publisher
    single_message_limit: int | None = None
    briefing_max_messages: int = 1
    diagnostics: RenderedTextDiagnostics | None = None

    def __post_init__(self) -> None:
        """Validate the delivery policy independently of the transport."""
        if self.single_message_limit is not None and (
            type(self.single_message_limit) is not int or self.single_message_limit <= 0
        ):
            raise ValueError("single_message_limit must be positive")
        if type(self.briefing_max_messages) is not int or self.briefing_max_messages <= 0:
            raise ValueError("briefing_max_messages must be positive")
        if self.briefing_max_messages > 1 and self.single_message_limit is None:
            raise ValueError("split briefings require a single_message_limit")

    def briefing_limit(self, configured_limit: int) -> int:
        """Clamp a configured briefing limit to the platform limit."""
        if self.single_message_limit is None:
            return configured_limit
        per_message_limit = min(configured_limit, self.single_message_limit)
        return per_message_limit * self.briefing_max_messages

    def briefing_target(self, configured_limit: int) -> int:
        """Prefer one message while allowing the configured aggregate limit."""
        if self.single_message_limit is None:
            return configured_limit
        return min(configured_limit, self.single_message_limit)

    def briefing_fits(self, message: RenderedMessage, configured_limit: int | None = None) -> bool:
        """Return whether a rendered briefing fits the configured chunk policy."""
        if self.single_message_limit is None:
            return configured_limit is None or message.visible_length <= configured_limit
        per_message_limit = self.single_message_limit
        if configured_limit is not None:
            per_message_limit = min(configured_limit, per_message_limit)
        if message.title is None:
            return message.visible_length <= per_message_limit * self.briefing_max_messages
        body_limit = per_message_limit - len(message.title)
        return body_limit > 0 and len(split_plain_message(message.body, body_limit)) <= self.briefing_max_messages

    def render_briefing(
        self,
        result: BriefingResult,
        reference_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
    ) -> RenderedMessage:
        """Render a briefing with the configured platform renderer."""
        return self.renderer.render_briefing(result, reference_articles, context)

    async def publish_rendered(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        """Publish an already rendered message with delivery hints."""
        log_rendered_text(self.diagnostics, "briefing", message.body)
        await self.publisher.publish(message, single_message=single_message, silent=silent)

    async def publish_briefing(self, message: RenderedMessage, *, silent: bool = False) -> None:
        """Publish a briefing according to its platform chunk policy."""
        if not self.briefing_fits(message):
            raise DeliveryError("Briefing exceeds the delivery limit", reason="message-too-long")
        await self.publish_rendered(
            message,
            single_message=self.briefing_max_messages == 1,
            silent=silent,
        )

    async def publish_verbatim(self, article: Article, *, silent: bool = False) -> None:
        """Render and publish one cleaned article without summarization."""
        message = self.renderer.render_verbatim(article)
        _LOGGER.debug(
            "Rendered verbatim message: visible_characters=%d payload_characters=%d",
            message.visible_length,
            len(message.body) + len(message.title or ""),
        )
        log_rendered_text(self.diagnostics, "verbatim", message.body)
        await self.publisher.publish(message, silent=silent)

    async def publish_alert(self, title: str, body: str) -> None:
        """Render and publish an operational alert."""
        message = self.renderer.render_alert(title, body)
        log_rendered_text(self.diagnostics, "alert", message.body)
        await self.publisher.publish(message)


class DeliveryError(RuntimeError):
    """Raised without exposing private delivery endpoint details."""

    def __init__(self, message: str, *, reason: str, channel_unavailable: bool = False) -> None:
        """Describe a delivery failure using a safe structured reason."""
        if not isinstance(reason, str) or _SAFE_DELIVERY_REASON.fullmatch(reason) is None:
            raise ValueError("Delivery error reason must be a lowercase kebab-case label")
        if not isinstance(channel_unavailable, bool):
            raise TypeError("channel_unavailable must be a bool")
        super().__init__(message)
        self.reason = reason
        self.channel_unavailable = channel_unavailable


def split_plain_message(body: str, limit: int) -> tuple[str, ...]:
    """Split text into the fewest chunks, consuming newlines used as boundaries."""
    if limit <= 0:
        raise ValueError("Message split limit must be positive")
    if not body or len(body) <= limit:
        return (body,)
    chunks: list[str] = []
    remaining = body
    while len(remaining) > limit:
        remaining_chunk_count = math.ceil(len(remaining) / limit)
        earliest_split = len(remaining) - (remaining_chunk_count - 1) * limit
        newline_at = remaining.rfind("\n", earliest_split, limit + 1)
        if newline_at >= earliest_split:
            chunks.append(remaining[:newline_at])
            remaining = remaining[newline_at + 1 :]
        else:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def log_rendered_text(diagnostics: RenderedTextDiagnostics | None, stage: str, body: str) -> None:
    """Log application-owned rendered text when its temporary switch is enabled."""
    if rendered_text_logging_enabled(diagnostics):
        _LOGGER.debug("Sensitive rendered text diagnostic: stage=%s body=%r", stage, body)


def rendered_text_logging_enabled(diagnostics: RenderedTextDiagnostics | None) -> bool:
    """Read the diagnostic switch without affecting delivery on failure."""
    if diagnostics is None:
        return False
    try:
        enabled = diagnostics.rendered_text_logging_enabled()
    except Exception:
        _LOGGER.warning("Rendered text diagnostic state check failed", exc_info=True)
        return False
    return enabled and _LOGGER.isEnabledFor(logging.DEBUG)
