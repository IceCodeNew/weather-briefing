from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol, cast

import httpx
import jwt

from .air_quality import AirQualityError, AirQualityProvider, air_quality_to_document, health_guidance
from .models import AirQualitySnapshot, SourceDocument, WeatherContextSnapshot
from .reference_data import reference_string_tuple
from .time_utils import (
    datetime_timezone_specifier,
    parse_datetime_with_default_timezone,
)


class WeatherContextError(RuntimeError):
    """Raised when a weather source is unavailable or violates its contract."""


class WeatherContextProvider(Protocol):
    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot: ...


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

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        try:
            headers = {"Authorization": self._authenticator.authorization_header()}
            weather_response = await self._client.get(
                f"{self._base_url}/v7/weather/3d",
                params={
                    "location": f"{longitude:.2f},{latitude:.2f}",
                    "lang": "zh",
                    "unit": "m",
                },
                headers=headers,
            )
            weather_response.raise_for_status()
            weather_payload = weather_response.json()
            if weather_payload.get("code") != "200":
                raise WeatherContextError("QWeather returned a non-success weather status")
            weather_forecast = tuple(_format_qweather_day(item) for item in weather_payload.get("daily", ())[:2])
            if not weather_forecast:
                raise WeatherContextError("QWeather returned no daily forecast")

            indices_response = await self._client.get(
                f"{self._base_url}/v7/indices/1d",
                params={
                    "type": ",".join(self._index_types),
                    "location": f"{longitude:.2f},{latitude:.2f}",
                    "lang": "zh",
                },
                headers=headers,
            )
            indices_response.raise_for_status()
            indices_payload = indices_response.json()
            if indices_payload.get("code") != "200":
                raise WeatherContextError("QWeather returned a non-success indices status")
            lifestyle_advice = tuple(_format_qweather_lifestyle(item) for item in indices_payload.get("daily", ()))
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
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise WeatherContextError("QWeather request or response validation failed") from None

        air_quality = await self._fetch_air_quality(latitude, longitude, headers)
        return WeatherContextSnapshot(
            source_id="weather:qweather",
            source_name="QWeather",
            source_url=source_url,
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            lifestyle_advice=lifestyle_advice,
            air_quality=air_quality,
        )

    async def _fetch_air_quality(
        self, latitude: float, longitude: float, headers: dict[str, str]
    ) -> AirQualitySnapshot | None:
        try:
            response = await self._client.get(
                f"{self._base_url}/airquality/v1/current/{latitude:.2f}/{longitude:.2f}",
                params={"lang": "zh"},
                headers=headers,
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
                source_name="QWeather air quality",
                source_url=str(_first_attribution(payload) or "https://www.qweather.com/"),
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
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
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

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
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
            "forecast_days": 2,
        }
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(f"{self._weather_base_url}/v1/forecast", params=params)
            response.raise_for_status()
            payload = response.json()
            daily = cast(dict[str, list[object]], payload["daily"])
            times = daily["time"]
            weather_forecast = tuple(_format_open_meteo_day(daily, index) for index in range(min(2, len(times))))
            if not weather_forecast:
                raise WeatherContextError("Open-Meteo returned no daily forecast")
            observed_at = parse_datetime_with_default_timezone(
                str(cast(dict[str, object], payload["current"])["time"]),
                str(payload["timezone"]),
                context="Open-Meteo weather update time",
            )
        except WeatherContextError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise WeatherContextError("Open-Meteo request or response validation failed") from None

        air_quality = await self._fetch_air_quality(latitude, longitude)
        return WeatherContextSnapshot(
            source_id="weather:open-meteo",
            source_name="Open-Meteo",
            source_url="https://open-meteo.com/",
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            air_quality=air_quality,
        )

    async def _fetch_air_quality(self, latitude: float, longitude: float) -> AirQualitySnapshot | None:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "us_aqi,us_aqi_pm2_5,pm2_5",
            "timezone": "auto",
        }
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(f"{self._air_quality_base_url}/v1/air-quality", params=params)
            response.raise_for_status()
            payload = response.json()
            current = cast(dict[str, Any], payload["current"])
            aqi = round(float(current["us_aqi"]))
            category, guidance = health_guidance(aqi)
            return AirQualitySnapshot(
                source_id="air-quality:open-meteo",
                source_name="Open-Meteo air quality",
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
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None


class FallbackWeatherContextProvider:
    def __init__(self, *providers: WeatherContextProvider) -> None:
        if not providers:
            raise ValueError("At least one weather context provider is required")
        self._providers = providers

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        for provider in self._providers[:-1]:
            try:
                return await provider.fetch(latitude, longitude)
            except WeatherContextError:
                continue
        return await self._providers[-1].fetch(latitude, longitude)


class AirQualitySupplementingWeatherProvider:
    def __init__(
        self,
        weather_provider: WeatherContextProvider,
        air_quality_provider: AirQualityProvider | None,
    ) -> None:
        self._weather_provider = weather_provider
        self._air_quality_provider = air_quality_provider

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        snapshot = await self._weather_provider.fetch(latitude, longitude)
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


def snapshot_to_documents(snapshot: WeatherContextSnapshot) -> tuple[SourceDocument, ...]:
    weather = "\n".join(f"- {item}" for item in snapshot.weather_forecast)
    lifestyle = "\n".join(f"- {item}" for item in snapshot.lifestyle_advice) or "不可用"
    documents = [
        SourceDocument(
            id=snapshot.source_id,
            name=snapshot.source_name,
            url=snapshot.source_url,
            content=(
                f"更新时间：{snapshot.observed_at.to_iso8601_string()}\n"
                f"今明天气预报：\n{weather}\n"
                f"生活与出行指数：\n{lifestyle}"
            ),
        )
    ]
    if snapshot.air_quality is not None:
        documents.append(air_quality_to_document(snapshot.air_quality))
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


def _first_attribution(payload: dict[str, object]) -> str | None:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    attributions = metadata.get("attributions", ())
    if not isinstance(attributions, list) or not attributions:
        return None
    return str(attributions[0])


def _aqi_standard(index: dict[str, Any]) -> str:
    code = str(index["code"])
    name = str(index.get("name") or code)
    return name if name == code else f"{name}（{code}）"


def _format_qweather_lifestyle(item: object) -> str:
    if not isinstance(item, dict):
        raise ValueError("QWeather lifestyle index must be an object")
    values = cast(dict[str, Any], item)
    name = str(values["name"])
    category = str(values.get("category", "未知"))
    text = str(values.get("text") or "无详细建议")
    return f"{name}（{category}）：{text}"


def _format_qweather_day(item: object) -> str:
    if not isinstance(item, dict):
        raise ValueError("QWeather daily forecast must be an object")
    values = cast(dict[str, Any], item)
    return (
        f"{values['fxDate']}：{values['textDay']}转{values['textNight']}，"
        f"{values['tempMin']}~{values['tempMax']}℃，"
        f"{values['windDirDay']}{values['windScaleDay']}级，"
        f"相对湿度{values['humidity']}%，预计降水量{values['precip']}毫米"
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
