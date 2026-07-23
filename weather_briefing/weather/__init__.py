"""Weather contracts, provider adapters, composition, and context conversion."""

from .base import (
    DatedWeatherContextProvider,
    FallbackWeatherContextProvider,
    LoggedWeatherContextProvider,
    UnsupportedForecastDateError,
    WeatherContextError,
    WeatherContextProvider,
    fetch_weather_context,
)
from .composition import AirQualitySupplementingWeatherProvider
from .documents import snapshot_to_documents
from .jma import JMA_LANGUAGE_SUPPORT, JMAJapanForecastProvider
from .nea import NEA_LANGUAGE_SUPPORT, NEASingaporeNowcastProvider
from .open_meteo import OPEN_METEO_LANGUAGE_SUPPORT, OpenMeteoProvider
from .qweather import (
    QWEATHER_LANGUAGE_SUPPORT,
    QWeatherAuthenticator,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
)
from .regional_errors import RegionalWeatherProviderError

__all__ = [
    "JMA_LANGUAGE_SUPPORT",
    "NEA_LANGUAGE_SUPPORT",
    "OPEN_METEO_LANGUAGE_SUPPORT",
    "QWEATHER_LANGUAGE_SUPPORT",
    "AirQualitySupplementingWeatherProvider",
    "DatedWeatherContextProvider",
    "FallbackWeatherContextProvider",
    "JMAJapanForecastProvider",
    "LoggedWeatherContextProvider",
    "NEASingaporeNowcastProvider",
    "OpenMeteoProvider",
    "QWeatherAuthenticator",
    "QWeatherJWTAuthenticator",
    "QWeatherProvider",
    "RegionalWeatherProviderError",
    "UnsupportedForecastDateError",
    "WeatherContextError",
    "WeatherContextProvider",
    "fetch_weather_context",
    "snapshot_to_documents",
]
