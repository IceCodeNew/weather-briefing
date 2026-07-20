"""Composable provider capabilities and location-scoped context assembly."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

import pendulum

from .air_quality import AirQualityError, AirQualityProvider
from .models import WeatherContextSnapshot
from .time_utils import datetime_timezone_specifier


class CapabilityName(StrEnum):
    """Identify independently replaceable weather data capabilities."""

    WEATHER = "weather"
    AIR_QUALITY = "air-quality"
    ALLERGEN = "allergen"
    LIFESTYLE = "lifestyle"
    ALERTS = "alerts"
    NOWCAST = "nowcast"


class ContextCapabilityProvider(Protocol):
    """Fetch the normalized weather context supplied by a weather capability."""

    async def fetch(
        self,
        latitude: float,
        longitude: float,
    ) -> WeatherContextSnapshot:
        """Fetch normalized context for a location."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Describe the capabilities exposed by one provider adapter."""

    provider_id: str
    provider_name: str
    capabilities: frozenset[CapabilityName]

    def supports(self, capability: CapabilityName) -> bool:
        """Return whether this provider exposes a capability."""
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class CapabilityProviderSet:
    """Compose independent weather and air-quality capabilities."""

    weather: ContextCapabilityProvider
    weather_metadata: ProviderCapabilities
    air_quality: AirQualityProvider | None = None
    air_quality_metadata: ProviderCapabilities | None = None

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        """Fetch weather and fill a missing current air-quality capability."""
        snapshot = await _fetch_context(self.weather, latitude, longitude, forecast_date)
        if snapshot.air_quality is not None or forecast_date is not None:
            return snapshot
        if self.air_quality is None:
            from .weather_context import WeatherContextError

            raise WeatherContextError("Weather source did not provide air quality; configure AQICN_API_TOKEN")
        try:
            air_quality = await self.air_quality.fetch(
                latitude,
                longitude,
                datetime_timezone_specifier(snapshot.observed_at, context="Weather snapshot time"),
            )
        except AirQualityError:
            from .weather_context import WeatherContextError

            raise WeatherContextError("Weather source did not provide air quality and AQICN fallback failed") from None
        return replace(snapshot, air_quality=air_quality)

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch a dated context through the composed capabilities."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


async def _fetch_context(
    provider: ContextCapabilityProvider,
    latitude: float,
    longitude: float,
    forecast_date: pendulum.Date | None,
) -> WeatherContextSnapshot:
    """Call providers that support either current or dated context."""
    if forecast_date is None:
        return await provider.fetch(latitude, longitude)
    from .weather_context import DatedWeatherContextProvider, WeatherContextError

    if not isinstance(provider, DatedWeatherContextProvider):
        raise WeatherContextError(f"{type(provider).__name__} does not support target forecast dates")
    fetch_for_date = provider.fetch_for_date
    if not callable(fetch_for_date):
        raise WeatherContextError(f"{type(provider).__name__} does not support target forecast dates")
    return await fetch_for_date(latitude, longitude, forecast_date)
