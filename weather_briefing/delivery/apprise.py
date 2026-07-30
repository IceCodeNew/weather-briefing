"""Notification delivery through Apprise."""

from __future__ import annotations

import logging
from typing import Protocol

from apprise import Apprise, AppriseAsset, NotifyFormat

from ..models import RenderedMessage
from .base import DeliveryError

_LOGGER = logging.getLogger("weather_briefing.publishers")


class AppriseNotifier(Protocol):
    """Expose the asynchronous Apprise notification entry point."""

    async def async_notify(
        self,
        *,
        body: str,
        title: str,
        body_format: NotifyFormat,
    ) -> bool | None:
        """Send one notification."""
        ...


class TelegramPublisher:
    """Publish Telegram messages through configured Apprise services."""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        notifier: AppriseNotifier,
        silent_notifier: AppriseNotifier,
    ) -> None:
        """Configure normal and silent notification variants."""
        self._notifier = notifier
        self._silent_notifier = silent_notifier

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        """Publish one plain-text message and let Apprise handle service overflow."""
        if single_message and message.visible_length > self.MAX_MESSAGE_LENGTH:
            raise DeliveryError(
                "Telegram single message exceeds the platform limit",
                reason="message-too-long",
            )
        _LOGGER.info(
            "Telegram delivery through Apprise prepared: visible_characters=%d payload_characters=%d "
            "single_message=%s silent=%s",
            message.visible_length,
            len(message.body) + len(message.title or ""),
            single_message,
            silent,
        )
        notifier = self._silent_notifier if silent else self._notifier
        try:
            delivered = await notifier.async_notify(
                body=message.body,
                title=message.title or "",
                body_format=NotifyFormat.TEXT,
            )
        except Exception:
            delivered = False
        if delivered is not True:
            _LOGGER.warning("Telegram delivery through Apprise failed")
            raise DeliveryError("Telegram delivery failed", reason="delivery-failed") from None
        _LOGGER.debug("Telegram delivery through Apprise accepted")


def telegram_notifier(
    token: str,
    chat_id: str,
    *,
    silent: bool,
    timeout_seconds: float,
) -> Apprise:
    """Build one validated Telegram notification target."""
    notifier = Apprise(
        asset=AppriseAsset(
            app_id="weather-briefing",
            async_mode=True,
            secure_logging=True,
            storage_mode="memory",
        )
    )
    configured = notifier.add(
        {
            "schema": "tgram",
            "bot_token": token,
            "targets": (chat_id,),
            "format": "text",
            "overflow": "split",
            "preview": False,
            "silent": silent,
            "cto": timeout_seconds,
            "rto": timeout_seconds,
        }
    )
    if not configured:
        raise ValueError("Invalid Telegram publisher configuration")
    return notifier
