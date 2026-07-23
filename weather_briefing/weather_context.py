"""Compatibility exports for weather context providers."""

from .weather import (
    OPEN_METEO_LANGUAGE_SUPPORT,
    QWEATHER_LANGUAGE_SUPPORT,
    AirQualitySupplementingWeatherProvider,
    DatedWeatherContextProvider,
    FallbackWeatherContextProvider,
    LoggedWeatherContextProvider,
    OpenMeteoProvider,
    QWeatherAuthenticator,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    UnsupportedForecastDateError,
    WeatherContextError,
    WeatherContextProvider,
    fetch_weather_context,
    snapshot_to_documents,
)

__all__ = [
    "OPEN_METEO_LANGUAGE_SUPPORT",
    "QWEATHER_LANGUAGE_SUPPORT",
    "AirQualitySupplementingWeatherProvider",
    "DatedWeatherContextProvider",
    "FallbackWeatherContextProvider",
    "LoggedWeatherContextProvider",
    "OpenMeteoProvider",
    "QWeatherAuthenticator",
    "QWeatherJWTAuthenticator",
    "QWeatherProvider",
    "UnsupportedForecastDateError",
    "WeatherContextError",
    "WeatherContextProvider",
    "fetch_weather_context",
    "snapshot_to_documents",
]
