"""Compatibility exports for regional weather providers."""

from .weather import (
    JMA_LANGUAGE_SUPPORT,
    NEA_LANGUAGE_SUPPORT,
    JMAJapanForecastProvider,
    NEASingaporeNowcastProvider,
    RegionalWeatherProviderError,
)

__all__ = [
    "JMA_LANGUAGE_SUPPORT",
    "NEA_LANGUAGE_SUPPORT",
    "JMAJapanForecastProvider",
    "NEASingaporeNowcastProvider",
    "RegionalWeatherProviderError",
]
