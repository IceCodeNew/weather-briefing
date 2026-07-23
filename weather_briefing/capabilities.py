"""Composable provider capabilities and location-scoped context assembly."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

import pendulum

from .air_quality import AirQualityError, AirQualityProvider
from .languages import LanguageSupport
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
    language_support: LanguageSupport = field(default_factory=lambda: LanguageSupport.fixed("zh-CN"))

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
    supplements: tuple[ContextCapabilityProvider, ...] = ()
    supplement_metadata: tuple[ProviderCapabilities, ...] = ()

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
            from .weather import WeatherContextError

            raise WeatherContextError("Weather source did not provide air quality; configure AQICN_API_TOKEN")
        try:
            air_quality = await self.air_quality.fetch(
                latitude,
                longitude,
                datetime_timezone_specifier(snapshot.observed_at, context="Weather snapshot time"),
            )
        except AirQualityError:
            from .weather import WeatherContextError

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

    async def fetch_all(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> tuple[WeatherContextSnapshot, ...]:
        """Fetch primary context and skip expected supplement failures."""
        snapshots = [await self.fetch(latitude, longitude, forecast_date=forecast_date)]
        from .weather import WeatherContextError

        for provider in self.supplements:
            try:
                snapshots.append(await _fetch_context(provider, latitude, longitude, forecast_date))
            except (WeatherContextError, ValueError):
                continue
        return tuple(snapshots)


async def _fetch_context(
    provider: ContextCapabilityProvider,
    latitude: float,
    longitude: float,
    forecast_date: pendulum.Date | None,
) -> WeatherContextSnapshot:
    """Route current or dated context through the shared provider boundary."""
    from .weather import fetch_weather_context

    return await fetch_weather_context(provider, latitude, longitude, forecast_date)
