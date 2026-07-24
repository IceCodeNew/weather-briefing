"""Bark push delivery adapter with optional encryption."""

from __future__ import annotations

import logging
import math

import httpx

from ..api_client import api_call_extensions
from ..data.bark import BARK_MAX_MESSAGE_LENGTH, BARK_NOTIFICATION_LEVEL
from ..data.service_endpoints import BARK_BASE_URL
from ..models import RenderedMessage
from .bark_crypto import BarkEncryptor
from .base import DeliveryError, RenderedTextDiagnostics, rendered_text_logging_enabled

_LOGGER = logging.getLogger("weather_briefing.publishers")


class BarkPublisher:
    """Publish messages through Bark, optionally encrypted with AES-GCM."""

    # Content must share APNs' 4 KiB payload with Bark metadata.
    MAX_MESSAGE_LENGTH = BARK_MAX_MESSAGE_LENGTH

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_key: str,
        encryption_key: str | None = None,
        encryption_iv: str | None = None,
        diagnostics: RenderedTextDiagnostics | None = None,
        *,
        base_url: str = BARK_BASE_URL,
        group: str = "weather-briefing",
        encryptor: BarkEncryptor | None = None,
    ) -> None:
        """Configure Bark delivery and optional AES-GCM encryption."""
        if not device_key:
            raise ValueError("Bark device key must not be empty")
        if (encryption_key is None) != (encryption_iv is None):
            raise ValueError("Bark encryption key and IV must be configured together")
        if not group:
            raise ValueError("Bark group must not be empty")
        self._client = client
        self._url = f"{base_url.rstrip('/')}/push"
        self._device_key = device_key
        self._group = group
        self._encryptor = encryptor
        if self._encryptor is None and encryption_key is not None and encryption_iv is not None:
            self._encryptor = BarkEncryptor(encryption_key, encryption_iv)
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
                "Bark single message exceeds the platform limit",
                reason="message-too-long",
            )
        chunks = (message.body,) if single_message else split_plain_message(message.body, self.MAX_MESSAGE_LENGTH)
        _LOGGER.info(
            "Bark delivery prepared: visible_characters=%d payload_characters=%d chunks=%d "
            "single_message=%s silent=%s encrypted=%s",
            message.visible_length,
            len(message.body),
            len(chunks),
            single_message,
            silent,
            self._encryptor is not None,
        )
        log_rendered_text = rendered_text_logging_enabled(self._diagnostics)
        for index, chunk in enumerate(chunks, start=1):
            if log_rendered_text:
                _LOGGER.debug(
                    "Sensitive rendered text diagnostic: stage=bark-chunk-%d-of-%d body=%r",
                    index,
                    len(chunks),
                    chunk,
                )
            parameters: dict[str, object] = {
                "body": chunk,
                "group": self._group,
                "level": "passive" if silent else BARK_NOTIFICATION_LEVEL,
            }
            request_payload = {"device_key": self._device_key, **parameters}
            if self._encryptor is not None:
                encrypted = self._encryptor.encrypt(parameters)
                request_payload = {
                    "device_key": self._device_key,
                    "ciphertext": encrypted.ciphertext,
                    "iv": encrypted.iv,
                }
            try:
                response = await self._client.post(
                    self._url,
                    json=request_payload,
                    extensions=api_call_extensions("bark", "push", response_error_handled=True),
                )
                response.raise_for_status()
                _validate_success_response(response)
            except httpx.HTTPStatusError as exc:
                reason, channel_unavailable = bark_error_reason(exc.response)
                _LOGGER.warning(
                    "Bark delivery rejected index=%d/%d message_visible_characters=%d payload_characters=%d "
                    "status_code=%d reason=%s",
                    index,
                    len(chunks),
                    message.visible_length,
                    len(chunk),
                    exc.response.status_code,
                    reason,
                )
                raise DeliveryError(
                    f"Bark delivery failed ({reason})",
                    reason=reason,
                    channel_unavailable=channel_unavailable,
                ) from None
            except httpx.RequestError as exc:
                _LOGGER.info(
                    "Bark delivery request failed index=%d/%d message_visible_characters=%d payload_characters=%d "
                    "reason=%s",
                    index,
                    len(chunks),
                    message.visible_length,
                    len(chunk),
                    type(exc).__name__,
                )
                raise DeliveryError("Bark delivery failed (request-error)", reason="request-error") from None
            except ValueError:
                _LOGGER.warning(
                    "Bark delivery returned an invalid success response index=%d/%d status_code=%d",
                    index,
                    len(chunks),
                    response.status_code,
                )
                raise DeliveryError("Bark delivery failed (invalid-response)", reason="invalid-response") from None
            _LOGGER.debug(
                "Bark chunk accepted: index=%d/%d payload_characters=%d",
                index,
                len(chunks),
                len(chunk),
            )


def bark_error_reason(response: httpx.Response) -> tuple[str, bool]:
    """Classify a Bark API error without exposing its response body."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            normalized = message.casefold()
            if "failed to get device token" in normalized or "invalid device key" in normalized:
                return "device-key-rejected", True
            if "payloadtoolarge" in normalized or "payload too large" in normalized:
                return "message-too-long", False
    status_reasons = {
        401: "unauthorized",
        403: "forbidden",
        404: "endpoint-not-found",
        413: "message-too-long",
        429: "rate-limited",
    }
    return status_reasons.get(response.status_code, "api-error"), response.status_code == 404


def split_plain_message(body: str, limit: int) -> tuple[str, ...]:
    """Split into display-ready chunks, consuming newlines used as boundaries."""
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


def _validate_success_response(response: httpx.Response) -> None:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Bark response is not JSON") from exc
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 200:
        raise ValueError("Bark response does not confirm success")
