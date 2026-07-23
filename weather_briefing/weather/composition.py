"""Composition policies for enriching weather context."""

from __future__ import annotations

from dataclasses import replace

import pendulum

from ..air_quality import AirQualityError, AirQualityProvider
from ..models import WeatherContextSnapshot
from ..time_utils import datetime_timezone_specifier
from .base import WeatherContextError, WeatherContextProvider, fetch_weather_context


class AirQualitySupplementingWeatherProvider:
    """Fill missing weather-provider air quality from a dedicated provider."""

    def __init__(
        self,
        weather_provider: WeatherContextProvider,
        air_quality_provider: AirQualityProvider | None,
    ) -> None:
        """Compose weather context with an optional air-quality fallback."""
        self._weather_provider = weather_provider
        self._air_quality_provider = air_quality_provider

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        """Fetch current weather context and supplement missing air quality."""
        snapshot = await fetch_weather_context(self._weather_provider, latitude, longitude, forecast_date)
        if snapshot.air_quality is not None:
            return snapshot
        if forecast_date is not None:
            return snapshot
        if self._air_quality_provider is None:
            raise WeatherContextError("Weather source did not provide air quality; configure AQICN_API_TOKEN")
        try:
            air_quality = await self._air_quality_provider.fetch(
                latitude,
                longitude,
                datetime_timezone_specifier(
                    snapshot.observed_at,
                    context="Weather snapshot time",
                ),
            )
        except AirQualityError:
            raise WeatherContextError("Weather source did not provide air quality and AQICN fallback failed") from None
        return replace(snapshot, air_quality=air_quality)

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch dated weather context and supplement missing air quality."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)
