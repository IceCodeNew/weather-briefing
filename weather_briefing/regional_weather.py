"""Local public weather capabilities for Singapore and Japan."""

from __future__ import annotations

from typing import Any

import httpx
import pendulum

from .api_client import api_call_extensions
from .languages import LanguageSupport
from .models import WeatherContextSnapshot
from .time_utils import parse_datetime_with_default_timezone
from .weather_context import WeatherContextError


class RegionalWeatherProviderError(WeatherContextError):
    """Raised when a local public weather response violates its contract."""


NEA_LANGUAGE_SUPPORT = LanguageSupport.fixed("en")


class NEASingaporeNowcastProvider:
    """Fetch Singapore's official two-hour nowcast from NEA/data.gov.sg."""

    language_support = NEA_LANGUAGE_SUPPORT
    output_language = language_support.default

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://api-open.data.gov.sg",
        api_key: str | None = None,
    ) -> None:
        """Configure the public NEA real-time API."""
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        """Fetch and normalize the current two-hour sector nowcast."""
        headers = {"x-api-key": self._api_key} if self._api_key else {}
        try:
            response = await self._client.get(
                f"{self._base_url}/v2/real-time/api/two-hr-forecast",
                headers=headers,
                extensions=api_call_extensions("nea-sg", "two-hour-nowcast"),
            )
            response.raise_for_status()
            payload = response.json()
            item = _first_item(payload)
            forecasts = item.get("forecasts")
            if not isinstance(forecasts, list) or not forecasts:
                raise RegionalWeatherProviderError("NEA nowcast contains no forecasts")
            forecast_lines = tuple(
                f"{_nea_area(entry.get('area'))}: {entry['forecast']}"
                for entry in forecasts
                if isinstance(entry, dict) and isinstance(entry.get("forecast"), str)
            )
            if not forecast_lines:
                raise RegionalWeatherProviderError("NEA nowcast contains no valid forecast entries")
            timestamp = item.get("timestamp") or item.get("update_timestamp")
            observed_at = _parse_singapore_time(timestamp)
        except RegionalWeatherProviderError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RegionalWeatherProviderError(f"NEA nowcast failed: {_safe_regional_error(exc)}") from None
        return WeatherContextSnapshot(
            source_id="weather:nea-sg-nowcast",
            source_name="Singapore NEA two-hour nowcast",
            source_url="https://www.nea.gov.sg/corporate-functions/weather",
            observed_at=observed_at,
            weather_forecast=("Next two hours:\n" + "\n".join(forecast_lines),),
            output_language=self.output_language,
        )


def _first_item(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RegionalWeatherProviderError("NEA response must be an object")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise RegionalWeatherProviderError("NEA response data must be an object")
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise RegionalWeatherProviderError("NEA response contains no item")
    return {str(key): value for key, value in items[0].items()}


def _nea_area(value: object) -> str:
    if isinstance(value, str) and (area := value.strip()):
        return area
    return "Singapore"


def _safe_regional_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _parse_singapore_time(value: object) -> pendulum.DateTime:
    if not isinstance(value, str) or not value.strip():
        return pendulum.now("Asia/Singapore")
    return parse_datetime_with_default_timezone(value, "Asia/Singapore", context="NEA update time")
