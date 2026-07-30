"""Delivery adapters for ServerChan Turbo and ServerChan 3."""

from __future__ import annotations

import logging
import re

import httpx

from ..api_client import api_call_extensions
from ..data.serverchan import (
    SERVERCHAN_3_PUSH_DOMAIN,
    SERVERCHAN_3_SENDKEY_PATTERN,
    SERVERCHAN_TITLE_MAX_CHARACTERS,
    SERVERCHAN_TURBO_BASE_URL,
    SERVERCHAN_TURBO_SENDKEY_PATTERN,
)
from ..models import RenderedMessage
from .base import DeliveryError, RenderedTextDiagnostics, rendered_text_logging_enabled

_LOGGER = logging.getLogger("weather_briefing.publishers")


class _ServerChanPublisher:
    """Share transport behavior while keeping product endpoints explicit."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        provider_id: str,
        display_name: str,
        diagnostics: RenderedTextDiagnostics | None,
    ) -> None:
        self._client = client
        self._url = url
        self._provider_id = provider_id
        self._display_name = display_name
        self._diagnostics = diagnostics

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        """Publish one title and Markdown body without exposing the SendKey."""
        title = message.title
        if title is None or "\n" in title or len(title) > SERVERCHAN_TITLE_MAX_CHARACTERS:
            raise DeliveryError(
                f"{self._display_name} title is invalid",
                reason="invalid-title",
            )
        _LOGGER.info(
            "%s delivery prepared: visible_characters=%d payload_characters=%d single_message=%s silent=%s",
            self._display_name,
            message.visible_length,
            len(title) + len(message.body),
            single_message,
            silent,
        )
        if rendered_text_logging_enabled(self._diagnostics):
            _LOGGER.debug(
                "Sensitive rendered text diagnostic: stage=%s title=%r body=%r",
                self._provider_id,
                title,
                message.body,
            )
        try:
            response = await self._client.post(
                self._url,
                json={"title": title, "desp": message.body},
                extensions=api_call_extensions(
                    self._provider_id,
                    "send",
                    response_error_handled=True,
                ),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            reason, channel_unavailable = serverchan_error_reason(exc.response)
            _LOGGER.warning(
                "%s delivery rejected: message_visible_characters=%d payload_characters=%d status_code=%d reason=%s",
                self._display_name,
                message.visible_length,
                len(title) + len(message.body),
                exc.response.status_code,
                reason,
            )
            raise DeliveryError(
                f"{self._display_name} delivery failed ({reason})",
                reason=reason,
                channel_unavailable=channel_unavailable,
            ) from None
        except httpx.RequestError as exc:
            _LOGGER.info(
                "%s delivery request failed: message_visible_characters=%d payload_characters=%d reason=%s",
                self._display_name,
                message.visible_length,
                len(title) + len(message.body),
                type(exc).__name__,
            )
            raise DeliveryError(
                f"{self._display_name} delivery failed (request-error)",
                reason="request-error",
            ) from None
        response_reason = _response_error_reason(response)
        if response_reason is not None:
            _LOGGER.warning(
                "%s delivery returned an unsuccessful response: status_code=%d reason=%s",
                self._display_name,
                response.status_code,
                response_reason,
            )
            raise DeliveryError(
                f"{self._display_name} delivery failed ({response_reason})",
                reason=response_reason,
            )
        _LOGGER.debug("%s message accepted: payload_characters=%d", self._display_name, len(title) + len(message.body))


class ServerChanTurboPublisher(_ServerChanPublisher):
    """Publish messages through ServerChan Turbo."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        sendkey: str,
        diagnostics: RenderedTextDiagnostics | None = None,
    ) -> None:
        """Configure the Turbo endpoint for one SCT SendKey."""
        if re.fullmatch(SERVERCHAN_TURBO_SENDKEY_PATTERN, sendkey) is None:
            raise ValueError("ServerChan Turbo SendKey must start with SCT and contain only letters and digits")
        super().__init__(
            client,
            f"{SERVERCHAN_TURBO_BASE_URL}/{sendkey}.send",
            "serverchan-turbo",
            "ServerChan Turbo",
            diagnostics,
        )


class ServerChan3Publisher(_ServerChanPublisher):
    """Publish messages through ServerChan 3."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        sendkey: str,
        diagnostics: RenderedTextDiagnostics | None = None,
    ) -> None:
        """Configure the user-specific ServerChan 3 endpoint."""
        match = re.fullmatch(SERVERCHAN_3_SENDKEY_PATTERN, sendkey)
        if match is None:
            raise ValueError("ServerChan 3 SendKey must use the sctp{uid}t{token} format")
        uid = match.group("uid")
        super().__init__(
            client,
            f"https://{uid}.{SERVERCHAN_3_PUSH_DOMAIN}/send/{sendkey}.send",
            "serverchan-3",
            "ServerChan 3",
            diagnostics,
        )


def serverchan_error_reason(response: httpx.Response) -> tuple[str, bool]:
    """Classify HTTP failures without inspecting or logging private payloads."""
    status_reasons = {
        401: "unauthorized",
        403: "forbidden",
        404: "endpoint-not-found",
        413: "message-too-long",
        429: "rate-limited",
    }
    reason = status_reasons.get(response.status_code, "api-error")
    return reason, response.status_code in {401, 403, 404}


def _response_error_reason(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return "invalid-response"
    if (
        not isinstance(payload, dict)
        or type(payload.get("code")) is not int
        or not isinstance(payload.get("message"), str)
    ):
        return "invalid-response"
    if payload["code"] != 0:
        return "api-error"
    return None
