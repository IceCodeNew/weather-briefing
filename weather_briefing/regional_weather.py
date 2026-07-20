"""Local public weather capabilities for Singapore and Japan."""

from __future__ import annotations

from typing import Any

import httpx
import pendulum

from .api_client import api_call_extensions
from .languages import LanguageSupport
from .models import WeatherContextSnapshot, normalize_jma_office_code
from .time_utils import parse_datetime_with_default_timezone
from .weather_context import WeatherContextError


class RegionalWeatherProviderError(WeatherContextError):
    """Raised when a local public weather response violates its contract."""


NEA_LANGUAGE_SUPPORT = LanguageSupport.fixed("en")
JMA_LANGUAGE_SUPPORT = LanguageSupport.fixed("ja")


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


class JMAJapanForecastProvider:
    """Fetch Japan Meteorological Agency prefecture forecast JSON."""

    language_support = JMA_LANGUAGE_SUPPORT
    output_language = language_support.default

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://www.jma.go.jp/bosai/forecast/data/forecast",
        office_code: str | None,
    ) -> None:
        """Configure JMA forecast data for one forecast office."""
        normalized_office_code = normalize_jma_office_code(office_code)
        if normalized_office_code is None:
            raise ValueError("JMA office code must contain six digits")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._office_code = normalized_office_code

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        """Fetch and normalize JMA's daily and time-series forecast."""
        return await self._fetch(latitude, longitude, forecast_date=None)

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch JMA forecast entries for one target date."""
        return await self._fetch(latitude, longitude, forecast_date=forecast_date)

    async def _fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None,
    ) -> WeatherContextSnapshot:
        try:
            response = await self._client.get(
                f"{self._base_url}/{self._office_code}.json",
                extensions=api_call_extensions("jma-jp", "forecast"),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
                raise RegionalWeatherProviderError("JMA forecast must be a non-empty array")
            report = payload[0]
            time_series = report.get("timeSeries")
            if not isinstance(time_series, list) or not time_series:
                raise RegionalWeatherProviderError("JMA forecast contains no time series")
            forecast_lines = _jma_forecast_lines(time_series, forecast_date=forecast_date)
            if not forecast_lines:
                target = f" for {forecast_date}" if forecast_date is not None else ""
                raise RegionalWeatherProviderError(f"JMA forecast contains no usable entries{target}")
            observed_at = _parse_japan_time(report.get("reportDatetime"))
        except RegionalWeatherProviderError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RegionalWeatherProviderError(f"JMA forecast failed: {_safe_regional_error(exc)}") from None
        return WeatherContextSnapshot(
            source_id="weather:jma-jp",
            source_name="Japan Meteorological Agency",
            source_url="https://www.jma.go.jp/bosai/forecast/",
            observed_at=observed_at,
            weather_forecast=forecast_lines,
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


def _parse_japan_time(value: object) -> pendulum.DateTime:
    if not isinstance(value, str) or not value.strip():
        return pendulum.now("Asia/Tokyo")
    return parse_datetime_with_default_timezone(value, "Asia/Tokyo", context="JMA report time")


def _jma_forecast_lines(
    time_series: list[object],
    *,
    forecast_date: pendulum.Date | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    for series in time_series[:2]:
        if not isinstance(series, dict):
            continue
        forecast_index = _jma_forecast_index(series, forecast_date)
        if forecast_index is None:
            continue
        areas = series.get("areas")
        if not isinstance(areas, list):
            continue
        for area in areas:
            if not isinstance(area, dict):
                continue
            area_metadata = area.get("area")
            area_name = area_metadata.get("name") if isinstance(area_metadata, dict) else None
            name = "日本"
            if isinstance(area_name, str) and (configured_name := area_name.strip()):
                name = configured_name
            if weather := _jma_text_at(area.get("weathers"), forecast_index):
                lines.append(f"{name}: {weather}")
            if wind := _jma_text_at(area.get("winds"), forecast_index):
                lines.append(f"{name}の風: {wind}")
    return tuple(lines)


def _jma_forecast_index(series: dict[Any, Any], forecast_date: pendulum.Date | None) -> int | None:
    if forecast_date is None:
        return 0
    time_defines = series.get("timeDefines")
    if not isinstance(time_defines, list):
        return None
    for index, value in enumerate(time_defines):
        if not isinstance(value, str):
            continue
        try:
            effective_at = parse_datetime_with_default_timezone(value, "Asia/Tokyo", context="JMA forecast time")
        except ValueError:
            continue
        if effective_at.in_timezone("Asia/Tokyo").date() == forecast_date:
            return index
    return None


def _jma_text_at(value: object, index: int) -> str | None:
    if not isinstance(value, list) or index >= len(value):
        return None
    selected = value[index]
    return selected if isinstance(selected, str) else None
