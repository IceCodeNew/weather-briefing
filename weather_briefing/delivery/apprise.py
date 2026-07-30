"""Notification delivery through Apprise."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Protocol

from apprise import Apprise, AppriseAsset, NotifyFormat
from apprise.plugins import telegram as apprise_telegram
from apprise.plugins.telegram import NotifyTelegram

from ..models import RenderedMessage
from .base import DeliveryError

_LOGGER = logging.getLogger("weather_briefing.publishers")


def _log_telegram_call_failure(
    started_at: float,
    phase: str,
    *,
    status_code: int | None = None,
    reason: str | None = None,
) -> None:
    """Log only bounded diagnostics for a failed Telegram API call."""
    duration_ms = round((time.monotonic() - started_at) * 1000)
    if status_code is not None:
        _LOGGER.warning(
            "API call failed provider=telegram operation=send-message method=POST "
            "duration_ms=%d status_code=%d phase=%s",
            duration_ms,
            status_code,
            phase,
        )
    else:
        _LOGGER.warning(
            "API call failed provider=telegram operation=send-message method=POST duration_ms=%d reason=%s phase=%s",
            duration_ms,
            reason or "unknown",
            phase,
        )


class _StrictNotifyTelegram(NotifyTelegram):
    """Preserve strict Telegram success validation missing from Apprise 1.12."""

    def send(
        self,
        body: str,
        title: str = "",
        notify_type: object = None,
        attach: object = None,
        body_format: NotifyFormat | None = None,
        **kwargs: object,
    ) -> bool:
        """Send text messages sequentially and require Telegram ``ok=true``."""
        if attach is not None or not self.targets:
            return False

        url = f"{self.notify_url}{self.bot_token}/sendMessage"
        headers = {
            "User-Agent": self.app_id,
            "Content-Type": "application/json",
        }
        for chat_id, topic in self.targets:
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "disable_notification": self.silent,
                "disable_web_page_preview": not self.preview,
                "text": body,
            }
            if topic:
                payload["message_thread_id"] = topic
            self.throttle()
            started_at = time.monotonic()
            try:
                response = apprise_telegram.requests.post(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    verify=self.verify_certificate,
                    timeout=self.request_timeout,
                    allow_redirects=self.redirects,
                )
            except apprise_telegram.requests.RequestException as exc:
                _log_telegram_call_failure(
                    started_at,
                    "request-error",
                    reason=type(exc).__name__,
                )
                return False
            if response.status_code != apprise_telegram.requests.codes.ok:
                _log_telegram_call_failure(
                    started_at,
                    "http-error",
                    status_code=response.status_code,
                )
                return False
            try:
                response_payload = json.loads(response.content)
            except (TypeError, ValueError):
                _log_telegram_call_failure(
                    started_at,
                    "invalid-json",
                    status_code=response.status_code,
                )
                return False
            if not isinstance(response_payload, dict):
                _log_telegram_call_failure(
                    started_at,
                    "invalid-response",
                    status_code=response.status_code,
                )
                return False
            if response_payload.get("ok") is not True:
                _log_telegram_call_failure(
                    started_at,
                    "telegram-not-ok",
                    status_code=response.status_code,
                )
                return False
            _LOGGER.info(
                "API call succeeded provider=telegram operation=send-message method=POST duration_ms=%d status_code=%d",
                round((time.monotonic() - started_at) * 1000),
                response.status_code,
            )
        return True


class AppriseNotifier(Protocol):
    """Expose the synchronous Apprise notification entry point."""

    def notify(
        self,
        body: str,
        title: str,
        *,
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
            delivered = await asyncio.to_thread(
                notifier.notify,
                message.body,
                message.title or "",
                body_format=NotifyFormat.TEXT,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Telegram delivery through Apprise failed reason=%s",
                type(exc).__name__,
            )
            delivered = False
        if delivered is not True:
            _LOGGER.debug("Telegram delivery through Apprise rejected")
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
    asset = AppriseAsset(
        app_id="weather-briefing",
        async_mode=False,
        secure_logging=True,
        storage_mode="memory",
    )
    notifier = Apprise(
        asset=asset,
    )
    try:
        service = _StrictNotifyTelegram(
            token,
            (chat_id,),
            asset=asset,
            schema="tgram",
            detect_owner=False,
            include_image=False,
            format="text",
            overflow="split",
            preview=False,
            silent=silent,
            cto=timeout_seconds,
            rto=timeout_seconds,
        )
    except TypeError:
        raise ValueError("Invalid Telegram publisher configuration") from None
    if not service.targets or not notifier.add(service):
        raise ValueError("Invalid Telegram publisher configuration")
    return notifier
