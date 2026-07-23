"""Provider-neutral weather contracts, fallback, and call logging."""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Protocol, TypeGuard, runtime_checkable

import httpx
import pendulum

from ..models import WeatherContextSnapshot

_LOGGER = logging.getLogger("weather_briefing.weather_context")


class WeatherContextError(RuntimeError):
    """Raised when a weather source is unavailable or violates its contract."""


class UnsupportedForecastDateError(WeatherContextError):
    """Raised when a provider cannot fetch an explicit forecast date."""


class WeatherContextProvider(Protocol):
    """Fetch provider-neutral weather context for coordinates."""

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        """Fetch the current weather context for a location."""
        ...


@runtime_checkable
class DatedWeatherContextProvider(Protocol):
    """Fetch weather context for an explicit forecast date."""

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch weather context for a location and forecast date."""
        ...


class LoggedWeatherContextProvider:
    """Record a non-sensitive history of logical weather provider calls."""

    def __init__(self, name: str, provider: WeatherContextProvider) -> None:
        """Wrap a named provider with non-sensitive timing and outcome logs."""
        self._name = name
        self._provider = provider

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        """Fetch weather context while recording non-sensitive call metadata."""
        started_at = time.monotonic()
        _LOGGER.info("Weather API call started provider=%s", self._name)
        try:
            snapshot = await fetch_weather_context(self._provider, latitude, longitude, forecast_date)
        except UnsupportedForecastDateError:
            with suppress(Exception):
                _LOGGER.info(
                    "Weather API call skipped provider=%s duration_ms=%d forecast_date=%s "
                    "reason=unsupported-forecast-date",
                    self._name,
                    _elapsed_milliseconds(started_at),
                    forecast_date,
                )
            raise
        except WeatherContextError as exc:
            _LOGGER.warning(
                "Weather API call failed provider=%s duration_ms=%d reason=%s",
                self._name,
                _elapsed_milliseconds(started_at),
                type(exc).__name__,
            )
            raise
        except Exception as exc:
            _LOGGER.warning(
                "Weather API call failed provider=%s duration_ms=%d reason=%s",
                self._name,
                _elapsed_milliseconds(started_at),
                type(exc).__name__,
            )
            raise
        _LOGGER.info(
            "Weather API call succeeded provider=%s duration_ms=%d source_id=%s observed_at=%s",
            self._name,
            _elapsed_milliseconds(started_at),
            snapshot.source_id,
            snapshot.observed_at.to_iso8601_string(),
        )
        return snapshot

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch logged weather context for an explicit date."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


class FallbackWeatherContextProvider:
    """Try weather providers in configured priority order."""

    def __init__(self, *providers: WeatherContextProvider) -> None:
        """Require and retain weather providers in fallback priority order."""
        if not providers:
            raise ValueError("At least one weather context provider is required")
        self._providers = providers

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        """Return current context from the first successful provider."""
        for provider in self._providers[:-1]:
            try:
                return await fetch_weather_context(provider, latitude, longitude, forecast_date)
            except WeatherContextError:
                continue
        return await fetch_weather_context(self._providers[-1], latitude, longitude, forecast_date)

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Return dated context from the first compatible provider."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


async def fetch_weather_context(
    provider: WeatherContextProvider,
    latitude: float,
    longitude: float,
    forecast_date: pendulum.Date | None,
) -> WeatherContextSnapshot:
    """Fetch current or dated context through a provider capability boundary."""
    if forecast_date is None:
        return await provider.fetch(latitude, longitude)
    if not isinstance(provider, DatedWeatherContextProvider):
        raise UnsupportedForecastDateError(f"{type(provider).__name__} does not support target forecast dates")
    fetch_for_date = provider.fetch_for_date
    if not callable(fetch_for_date):
        raise UnsupportedForecastDateError(f"{type(provider).__name__} does not support target forecast dates")
    return await fetch_for_date(latitude, longitude, forecast_date)


def _elapsed_milliseconds(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def _safe_provider_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)
