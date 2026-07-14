from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

_LOGGER = logging.getLogger("weather_briefing.api_client")
_API_CALL_EXTENSION = "weather_briefing.api_call"
_SAFE_LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def api_call_extensions(provider: str, operation: str) -> dict[str, object]:
    """Attach non-sensitive API identity to an HTTPX request."""
    if _SAFE_LABEL.fullmatch(provider) is None:
        raise ValueError("API provider must be a lowercase kebab-case label")
    if _SAFE_LABEL.fullmatch(operation) is None:
        raise ValueError("API operation must be a lowercase kebab-case label")
    return {_API_CALL_EXTENSION: (provider, operation)}


class LoggedAsyncClient(httpx.AsyncClient):
    """Record outbound HTTP calls without logging request data."""

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        provider, operation = _api_call_identity(request)
        method = _safe_http_method(request.method)
        started_at = time.monotonic()
        _LOGGER.info(
            "API call started provider=%s operation=%s method=%s",
            provider,
            operation,
            method,
        )
        try:
            response = await super().send(request, **kwargs)
        except Exception as exc:
            _LOGGER.warning(
                "API call failed provider=%s operation=%s method=%s duration_ms=%d reason=%s",
                provider,
                operation,
                method,
                _elapsed_milliseconds(started_at),
                type(exc).__name__,
            )
            raise

        if response.is_error:
            _LOGGER.warning(
                "API call failed provider=%s operation=%s method=%s duration_ms=%d status_code=%d",
                provider,
                operation,
                method,
                _elapsed_milliseconds(started_at),
                response.status_code,
            )
        else:
            _LOGGER.info(
                "API call succeeded provider=%s operation=%s method=%s duration_ms=%d status_code=%d",
                provider,
                operation,
                method,
                _elapsed_milliseconds(started_at),
                response.status_code,
            )
        return response


def _api_call_identity(request: httpx.Request) -> tuple[str, str]:
    value = request.extensions.get(_API_CALL_EXTENSION)
    if not isinstance(value, tuple) or len(value) != 2:
        return "unclassified", "request"
    provider, operation = value
    if not isinstance(provider, str) or not isinstance(operation, str):
        return "unclassified", "request"
    if _SAFE_LABEL.fullmatch(provider) is None or _SAFE_LABEL.fullmatch(operation) is None:
        return "unclassified", "request"
    return provider, operation


def _safe_http_method(method: str) -> str:
    if method.isascii() and method.isalpha() and method.isupper():
        return method
    return "INVALID"


def _elapsed_milliseconds(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)
