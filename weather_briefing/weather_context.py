from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any, Protocol, TypeGuard, runtime_checkable

import httpx
import jwt
import pendulum

from .air_quality import AirQualityError, AirQualityProvider, air_quality_to_document, health_guidance
from .allergen import allergen_guidance, allergen_to_document, pollen_type_names
from .api_client import api_call_extensions
from .models import AirQualitySnapshot, AllergenLevel, AllergenSnapshot, SourceDocument, WeatherContextSnapshot
from .reference_data import ReferenceDataError, reference_string, reference_string_tuple
from .time_utils import (
    datetime_timezone_specifier,
    parse_datetime_with_default_timezone,
)

_LOGGER = logging.getLogger("weather_briefing.weather_context")


class WeatherContextError(RuntimeError):
    """Raised when a weather source is unavailable or violates its contract."""


class _QWeatherResponseError(ValueError):
    """Raised for safe, code-defined QWeather response contract errors."""


class WeatherContextProvider(Protocol):
    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot: ...


@runtime_checkable
class DatedWeatherContextProvider(Protocol):
    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot: ...


class LoggedWeatherContextProvider:
    """Record a non-sensitive history of logical weather provider calls."""

    def __init__(self, name: str, provider: WeatherContextProvider) -> None:
        self._name = name
        self._provider = provider

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        started_at = time.monotonic()
        _LOGGER.info("Weather API call started provider=%s", self._name)
        try:
            snapshot = await fetch_weather_context(self._provider, latitude, longitude, forecast_date)
        except WeatherContextError as exc:
            _LOGGER.warning(
                "Weather API call failed provider=%s duration_ms=%d reason=%s",
                self._name,
                _elapsed_milliseconds(started_at),
                exc,
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
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


class QWeatherAuthenticator(Protocol):
    def authorization_header(self) -> str: ...


class QWeatherJWTAuthenticator:
    def __init__(
        self,
        *,
        project_id: str,
        credential_id: str,
        private_key_base64: str,
        lifetime_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 1 <= lifetime_seconds <= 86_400:
            raise ValueError("QWeather JWT lifetime must be between 1 and 86400 seconds")
        self._project_id = project_id
        self._credential_id = credential_id
        try:
            self._private_key = base64.b64decode(private_key_base64, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("QWeather private key must be a Base64-encoded UTF-8 PEM") from None
        self._lifetime_seconds = lifetime_seconds
        self._clock = clock

    def authorization_header(self) -> str:
        issued_at = int(self._clock()) - 30
        token = jwt.encode(
            {
                "sub": self._project_id,
                "iat": issued_at,
                "exp": issued_at + self._lifetime_seconds,
            },
            self._private_key,
            algorithm="EdDSA",
            headers={"kid": self._credential_id},
        )
        return f"Bearer {token}"


class QWeatherProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        authenticator: QWeatherAuthenticator,
        base_url: str,
        index_types: tuple[str, ...] | None = None,
    ) -> None:
        self._client = client
        self._authenticator = authenticator
        self._base_url = base_url
        self._index_types = index_types or reference_string_tuple(
            "provider_defaults.json", "qweather_lifestyle_index_types"
        )
        self._allergen_index_type = reference_string("provider_defaults.json", "qweather_allergen_index_type")

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        operation = "authentication"
        try:
            headers = {"Authorization": self._authenticator.authorization_header()}
            operation = "weather forecast"
            weather_response = await self._client.get(
                f"{self._base_url}/v7/weather/3d",
                params={
                    "location": f"{longitude:.2f},{latitude:.2f}",
                    "lang": "zh",
                    "unit": "m",
                },
                headers=headers,
                extensions=api_call_extensions("qweather", "weather-forecast"),
            )
            weather_response.raise_for_status()
            weather_payload = weather_response.json()
            if weather_payload.get("code") != "200":
                raise WeatherContextError(
                    "QWeather returned a non-success weather status "
                    f"code={_safe_api_status(weather_payload.get('code'))}"
                )
            operation = "weather forecast parsing"
            daily_forecasts = weather_payload.get("daily", ())
            first_forecast_date = next(
                (
                    str(item["fxDate"])
                    for item in daily_forecasts
                    if isinstance(item, dict) and item.get("fxDate") is not None
                ),
                None,
            )
            if forecast_date is None:
                selected_forecasts = daily_forecasts[:2]
            else:
                selected_forecasts = tuple(
                    item
                    for item in daily_forecasts
                    if isinstance(item, dict) and item.get("fxDate") == str(forecast_date)
                )
            weather_forecast = tuple(_format_qweather_day(item) for item in selected_forecasts)
            if not weather_forecast:
                if forecast_date is None:
                    raise WeatherContextError("QWeather returned no daily forecast")
                raise WeatherContextError(f"QWeather returned no forecast for {forecast_date}")

            indices_payload: dict[str, object] = {}
            lifestyle_advice: tuple[str, ...] = ()
            allergen_advice_available = False
            if forecast_date is None or str(forecast_date) == first_forecast_date:
                operation = "lifestyle indices"
                indices_response = await self._client.get(
                    f"{self._base_url}/v7/indices/1d",
                    params={
                        "type": ",".join(self._index_types),
                        "location": f"{longitude:.2f},{latitude:.2f}",
                        "lang": "zh",
                    },
                    headers=headers,
                    extensions=api_call_extensions("qweather", "lifestyle-indices"),
                )
                indices_response.raise_for_status()
                indices_payload = indices_response.json()
                if indices_payload.get("code") != "200":
                    raise WeatherContextError(
                        "QWeather returned a non-success indices status "
                        f"code={_safe_api_status(indices_payload.get('code'))}"
                    )
                daily_indices = tuple(indices_payload.get("daily", ()))
                lifestyle_advice = tuple(_format_qweather_lifestyle(item) for item in daily_indices)
                allergen_advice_available = any(
                    str(item.get("type")) == self._allergen_index_type
                    for item in daily_indices
                    if isinstance(item, dict)
                )
            source_url = str(
                weather_payload.get("fxLink") or indices_payload.get("fxLink") or "https://www.qweather.com/"
            )
            observed_at = parse_datetime_with_default_timezone(
                str(weather_payload.get("updateTime") or indices_payload["updateTime"]),
                "Asia/Shanghai",
                context="QWeather update time",
            )
        except WeatherContextError:
            raise
        except _QWeatherResponseError as exc:
            raise WeatherContextError(f"QWeather {operation} failed: {exc}") from None
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            detail = _safe_provider_error(exc)
            raise WeatherContextError(f"QWeather {operation} failed: {detail}") from None

        air_quality = await self._fetch_air_quality(latitude, longitude, headers, source_url)
        return WeatherContextSnapshot(
            source_id="weather:qweather",
            source_name="QWeather",
            source_url=source_url,
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            lifestyle_advice=lifestyle_advice,
            air_quality=air_quality,
            allergen_advice_available=allergen_advice_available,
        )

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)

    async def _fetch_air_quality(
        self,
        latitude: float,
        longitude: float,
        headers: dict[str, str],
        source_url: str,
    ) -> AirQualitySnapshot | None:
        try:
            response = await self._client.get(
                f"{self._base_url}/airquality/v1/current/{latitude:.2f}/{longitude:.2f}",
                params={"lang": "zh"},
                headers=headers,
                extensions=api_call_extensions("qweather", "air-quality"),
            )
            response.raise_for_status()
            payload = response.json()
            index = _first_mapping(payload, "indexes")
            pm25 = _mapping_by_code(payload, "pollutants", "pm2p5")
            concentration: dict[str, Any] = pm25.get("concentration", {})
            health: dict[str, Any] = index.get("health", {})
            advice: dict[str, Any] = health.get("advice", {})
            aqi = float(index["aqi"])
            return AirQualitySnapshot(
                source_id="air-quality:qweather",
                source_name="QWeather",
                source_url=source_url,
                observed_at=None,
                aqi=aqi,
                aqi_display=str(index.get("aqiDisplay", index["aqi"])),
                aqi_standard=_aqi_standard(index),
                pm25_aqi=_sub_index(pm25, str(index["code"])),
                pm25_concentration=float(concentration["value"]),
                pm25_unit=str(concentration["unit"]),
                category=str(index.get("category", "未知")),
                health_guidance=str(advice.get("generalPopulation") or health.get("effect", "")),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=qweather operation=air-quality reason=%s",
                _safe_provider_error(exc),
            )
            return None


class OpenMeteoProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        weather_base_url: str = "https://api.open-meteo.com",
        air_quality_base_url: str = "https://air-quality-api.open-meteo.com",
        api_key: str | None = None,
    ) -> None:
        self._client = client
        self._weather_base_url = weather_base_url
        self._air_quality_base_url = air_quality_base_url
        self._api_key = api_key

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": ",".join(
                (
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                    "wind_direction_10m_dominant",
                    "uv_index_max",
                )
            ),
            "current": "relative_humidity_2m",
            "timezone": "auto",
        }
        if forecast_date is None:
            params["forecast_days"] = 2
        else:
            params["start_date"] = str(forecast_date)
            params["end_date"] = str(forecast_date)
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(
                f"{self._weather_base_url}/v1/forecast",
                params=params,
                extensions=api_call_extensions("open-meteo", "weather-forecast"),
            )
            response.raise_for_status()
            payload = response.json()
            daily: dict[str, list[object]] = payload["daily"]
            times = daily["time"]
            forecast_count = min(2, len(times)) if forecast_date is None else len(times)
            weather_forecast = tuple(_format_open_meteo_day(daily, index) for index in range(forecast_count))
            if not weather_forecast:
                raise WeatherContextError("Open-Meteo returned no daily forecast")
            current: dict[str, object] = payload["current"]
            observed_at = parse_datetime_with_default_timezone(
                str(current["time"]),
                str(payload["timezone"]),
                context="Open-Meteo weather update time",
            )
        except WeatherContextError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise WeatherContextError(f"Open-Meteo weather forecast failed: {_safe_provider_error(exc)}") from None

        air_quality, allergen = await self._fetch_air_quality_and_allergen(latitude, longitude)
        return WeatherContextSnapshot(
            source_id="weather:open-meteo",
            source_name="Open-Meteo",
            source_url="https://open-meteo.com/",
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            air_quality=air_quality,
            allergen=allergen,
        )

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)

    async def _fetch_air_quality_and_allergen(
        self, latitude: float, longitude: float
    ) -> tuple[AirQualitySnapshot | None, AllergenSnapshot | None]:
        try:
            pollen_types = pollen_type_names()
        except ReferenceDataError as exc:
            _LOGGER.warning(
                "Weather API optional enrichment failed provider=open-meteo operation=allergen reason=%s",
                type(exc).__name__,
            )
            pollen_types = ()
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(
                (
                    "us_aqi",
                    "us_aqi_pm2_5",
                    "pm2_5",
                    *(f"{key}_pollen" for key, _ in pollen_types),
                )
            ),
            "timezone": "auto",
        }
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(
                f"{self._air_quality_base_url}/v1/air-quality",
                params=params,
                extensions=api_call_extensions("open-meteo", "air-quality"),
            )
            response.raise_for_status()
            payload = response.json()
            current: dict[str, object] = payload["current"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
                _safe_provider_error(exc),
            )
            return None, None
        allergen = None
        if pollen_types:
            try:
                allergen = self._parse_allergen(current, payload, pollen_types)
            except ReferenceDataError as exc:
                _LOGGER.warning(
                    "Weather API optional enrichment failed provider=open-meteo operation=allergen reason=%s",
                    type(exc).__name__,
                )
        return self._parse_air_quality(current, payload), allergen

    @staticmethod
    def _parse_air_quality(current: dict[str, Any], payload: dict[str, Any]) -> AirQualitySnapshot | None:
        try:
            aqi = round(float(current["us_aqi"]))
            category, guidance = health_guidance(aqi)
            return AirQualitySnapshot(
                source_id="air-quality:open-meteo",
                source_name="Open-Meteo",
                source_url="https://open-meteo.com/en/docs/air-quality-api",
                observed_at=parse_datetime_with_default_timezone(
                    str(current["time"]),
                    str(payload["timezone"]),
                    context="Open-Meteo air-quality update time",
                ),
                aqi=aqi,
                aqi_display=str(aqi),
                aqi_standard="U.S. AQI",
                pm25_aqi=round(float(current["us_aqi_pm2_5"])),
                pm25_concentration=float(current["pm2_5"]),
                pm25_unit="μg/m³",
                category=category,
                health_guidance=guidance,
            )
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _parse_allergen(
        current: dict[str, Any],
        payload: dict[str, Any],
        pollen_types: tuple[tuple[str, str], ...],
    ) -> AllergenSnapshot | None:
        levels: list[AllergenLevel] = []
        for key, display_name in pollen_types:
            raw = current.get(f"{key}_pollen")
            if raw is None:
                continue
            try:
                concentration = float(raw)
            except (TypeError, ValueError):
                continue
            try:
                category, _ = allergen_guidance(concentration)
            except ValueError:
                continue
            levels.append(AllergenLevel(name=display_name, category=category, concentration=concentration))
        if not levels:
            return None
        max_concentration = max(level.concentration for level in levels)
        overall_category, overall_guidance = allergen_guidance(max_concentration)
        timezone_value = payload.get("timezone")
        observed_at = None
        time_value = current.get("time")
        if time_value is not None and isinstance(timezone_value, str):
            with suppress(TypeError, ValueError):
                observed_at = parse_datetime_with_default_timezone(
                    str(time_value),
                    timezone_value,
                    context="Open-Meteo allergen update time",
                )
        return AllergenSnapshot(
            source_id="allergen:open-meteo",
            source_name="Open-Meteo / CAMS ENSEMBLE 花粉过敏原",
            source_url="https://open-meteo.com/en/docs/air-quality-api",
            observed_at=observed_at,
            levels=tuple(levels),
            overall_category=overall_category,
            health_guidance=overall_guidance,
        )


class FallbackWeatherContextProvider:
    def __init__(self, *providers: WeatherContextProvider) -> None:
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
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


async def fetch_weather_context(
    provider: WeatherContextProvider,
    latitude: float,
    longitude: float,
    forecast_date: pendulum.Date | None,
) -> WeatherContextSnapshot:
    if forecast_date is None:
        return await provider.fetch(latitude, longitude)
    if not isinstance(provider, DatedWeatherContextProvider):
        raise WeatherContextError(f"{type(provider).__name__} does not support target forecast dates")
    return await provider.fetch_for_date(latitude, longitude, forecast_date)


def _elapsed_milliseconds(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def _safe_provider_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _safe_api_status(value: object) -> str:
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit():
        return value
    return "invalid"


class AirQualitySupplementingWeatherProvider:
    def __init__(
        self,
        weather_provider: WeatherContextProvider,
        air_quality_provider: AirQualityProvider | None,
    ) -> None:
        self._weather_provider = weather_provider
        self._air_quality_provider = air_quality_provider

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        snapshot = await fetch_weather_context(self._weather_provider, latitude, longitude, forecast_date)
        if snapshot.air_quality is not None:
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
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)


def snapshot_to_documents(snapshot: WeatherContextSnapshot) -> tuple[SourceDocument, ...]:
    weather = "\n".join(f"- {item}" for item in snapshot.weather_forecast)
    lifestyle = "\n".join(f"- {item}" for item in snapshot.lifestyle_advice) or "不可用"
    documents = [
        SourceDocument(
            id=snapshot.source_id,
            name=snapshot.source_name,
            url=snapshot.source_url,
            has_allergen_information=snapshot.allergen_advice_available,
            content=(
                f"更新时间：{snapshot.observed_at.to_iso8601_string()}\n"
                f"今明天气预报：\n{weather}\n"
                f"生活与出行指数：\n{lifestyle}"
            ),
        )
    ]
    if snapshot.air_quality is not None:
        documents.append(air_quality_to_document(snapshot.air_quality))
    if snapshot.allergen is not None:
        documents.append(allergen_to_document(snapshot.allergen))
    return tuple(documents)


def _first_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    values = payload[key]
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        raise ValueError(f"{key} must contain at least one object")
    return values[0]


def _mapping_by_code(payload: dict[str, Any], key: str, code: str) -> dict[str, Any]:
    values = payload[key]
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a list")
    for value in values:
        if isinstance(value, dict) and value.get("code") == code:
            return value
    raise ValueError(f"{key} does not contain {code}")


def _sub_index(pollutant: dict[str, Any], standard: str) -> float | None:
    values = pollutant.get("subIndexes", ())
    if not isinstance(values, list):
        return None
    for value in values:
        if isinstance(value, dict) and value.get("code") == standard:
            return float(value["aqi"])
    return None


def _aqi_standard(index: dict[str, object]) -> str:
    code = str(index["code"])
    name = str(index.get("name") or code)
    return name if name == code else f"{name}（{code}）"


def _format_qweather_lifestyle(item: dict[str, object]) -> str:
    name = str(item["name"])
    category = str(item.get("category", "未知"))
    text = str(item.get("text") or "无详细建议")
    return f"{name}（{category}）：{text}"


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _format_qweather_day(item: object) -> str:
    if not _is_string_keyed_dict(item):
        raise TypeError("daily forecast entries must be objects")
    required_fields = (
        "fxDate",
        "textDay",
        "textNight",
        "tempMin",
        "tempMax",
        "windDirDay",
        "windScaleDay",
        "humidity",
        "precip",
    )
    if missing_field := next((field for field in required_fields if field not in item), None):
        raise _QWeatherResponseError(f"daily forecast missing required field: {missing_field}")
    return (
        f"{item['fxDate']}：{item['textDay']}转{item['textNight']}，"
        f"{item['tempMin']}~{item['tempMax']}℃，"
        f"{item['windDirDay']}{item['windScaleDay']}级，"
        f"相对湿度{item['humidity']}%，预计降水量{item['precip']}毫米"
    )


def _format_open_meteo_day(daily: dict[str, list[object]], index: int) -> str:
    return (
        f"{daily['time'][index]}：WMO天气代码{daily['weather_code'][index]}，"
        f"{daily['temperature_2m_min'][index]}~{daily['temperature_2m_max'][index]}℃，"
        f"体感{daily['apparent_temperature_min'][index]}~"
        f"{daily['apparent_temperature_max'][index]}℃，"
        f"预计降水{daily['precipitation_sum'][index]}毫米，"
        f"最高降水概率{daily['precipitation_probability_max'][index]}%，"
        f"最大风速{daily['wind_speed_10m_max'][index]}千米/小时，"
        f"最大阵风{daily['wind_gusts_10m_max'][index]}千米/小时，"
        f"主导风向{daily['wind_direction_10m_dominant'][index]}°，"
        f"最高紫外线指数{daily['uv_index_max'][index]}"
    )
