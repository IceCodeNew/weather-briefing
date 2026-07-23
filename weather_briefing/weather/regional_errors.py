"""Shared contract error for regional public weather adapters."""

import httpx

from .base import WeatherContextError


class RegionalWeatherProviderError(WeatherContextError):
    """Raised when a local public weather response violates its contract."""


def safe_regional_error(exc: Exception) -> str:
    """Return a non-sensitive category for a regional provider failure."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
