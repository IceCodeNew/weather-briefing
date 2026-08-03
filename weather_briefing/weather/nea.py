"""Singapore NEA two-hour nowcast adapter."""

from __future__ import annotations

from typing import Any

import httpx
import pendulum

from weather_briefing.api_client import api_call_extensions
from weather_briefing.data.service_endpoints import NEA_BASE_URL
from weather_briefing.languages import LanguageSupport
from weather_briefing.models import WeatherContextSnapshot
from weather_briefing.time_utils import parse_datetime_with_default_timezone

from .regional_errors import RegionalWeatherProviderError, safe_regional_error

NEA_LANGUAGE_SUPPORT = LanguageSupport.fixed("en")


class NEASingaporeNowcastProvider:
    """Fetch Singapore's official two-hour nowcast from NEA/data.gov.sg."""

    language_support = NEA_LANGUAGE_SUPPORT
    output_language = language_support.default

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = NEA_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        """Configure the public NEA real-time API."""
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:  # noqa: ARG002
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
                msg = "NEA nowcast contains no forecasts"
                raise RegionalWeatherProviderError(msg)  # noqa: TRY301
            forecast_lines = tuple(
                f"{_nea_area(entry.get('area'))}: {entry['forecast']}"
                for entry in forecasts
                if isinstance(entry, dict) and isinstance(entry.get("forecast"), str)
            )
            if not forecast_lines:
                msg = "NEA nowcast contains no valid forecast entries"
                raise RegionalWeatherProviderError(msg)  # noqa: TRY301
            timestamp = item.get("timestamp") or item.get("update_timestamp")
            observed_at = _parse_singapore_time(timestamp)
        except RegionalWeatherProviderError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            msg = f"NEA nowcast failed: {safe_regional_error(exc)}"
            raise RegionalWeatherProviderError(msg) from None
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
        msg = "NEA response must be an object"
        raise RegionalWeatherProviderError(msg)
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        msg = "NEA response data must be an object"
        raise RegionalWeatherProviderError(msg)
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        msg = "NEA response contains no item"
        raise RegionalWeatherProviderError(msg)
    return {str(key): value for key, value in items[0].items()}


def _nea_area(value: object) -> str:
    if isinstance(value, str) and (area := value.strip()):
        return area
    return "Singapore"


def _parse_singapore_time(value: object) -> pendulum.DateTime:
    if not isinstance(value, str) or not value.strip():
        return pendulum.now("Asia/Singapore")
    return parse_datetime_with_default_timezone(value, "Asia/Singapore", context="NEA update time")
