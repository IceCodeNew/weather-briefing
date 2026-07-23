"""Platform-neutral message delivery composition."""

from __future__ import annotations

import logging
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
    diagnostics: RenderedTextDiagnostics | None = None

    def briefing_limit(self, configured_limit: int) -> int:
        """Clamp a configured briefing limit to the platform limit."""
        if self.single_message_limit is None:
            return configured_limit
        return min(configured_limit, self.single_message_limit)

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

    async def publish_verbatim(self, article: Article, *, silent: bool = False) -> None:
        """Render and publish one cleaned article without summarization."""
        message = self.renderer.render_verbatim(article)
        _LOGGER.debug(
            "Rendered verbatim message: visible_characters=%d payload_characters=%d",
            message.visible_length,
            len(message.body),
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
