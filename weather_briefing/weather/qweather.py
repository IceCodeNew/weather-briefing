"""QWeather authentication, adapter, and response normalization."""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import jwt
import pendulum

from ..api_client import api_call_extensions
from ..data.resources import reference_string, reference_string_tuple
from ..languages import LanguageSupport, localized_labels
from ..localization import localization_table
from ..models import AirQualitySnapshot, AirQualityTimeKind, WeatherContextSnapshot
from ..time_utils import parse_datetime_with_default_timezone
from .base import WeatherContextError, _is_string_keyed_dict, _safe_provider_error

_LOGGER = logging.getLogger("weather_briefing.weather_context")
QWEATHER_LANGUAGE_SUPPORT = LanguageSupport(
    default="zh-CN",
    supported=("zh-CN", "zh-TW", "en", "ja"),
    api_codes=(("zh-CN", "zh"), ("zh-TW", "zh-hant"), ("en", "en"), ("ja", "ja")),
)
_QWEATHER_FORMATS = localization_table("qweather")


class _QWeatherResponseError(ValueError):
    """Raised for safe, code-defined QWeather response contract errors."""


class QWeatherAuthenticator(Protocol):
    """Generate an authorization header for QWeather requests."""

    def authorization_header(self) -> str:
        """Return a fresh QWeather authorization header."""
        ...


class QWeatherJWTAuthenticator:
    """Issue short-lived QWeather EdDSA JWT credentials."""

    def __init__(
        self,
        *,
        project_id: str,
        credential_id: str,
        private_key_base64: str,
        lifetime_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Validate and retain credentials for short-lived QWeather JWTs."""
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
        """Create a Bearer header containing a fresh short-lived JWT."""
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
    """Fetch weather, lifestyle, and air-quality context from QWeather."""

    language_support = QWEATHER_LANGUAGE_SUPPORT

    @property
    def output_language(self) -> str:
        """Return the selected QWeather response language."""
        return self._output_language

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        authenticator: QWeatherAuthenticator,
        base_url: str,
        index_types: tuple[str, ...] | None = None,
        output_language: str = "zh-CN",
    ) -> None:
        """Configure authenticated QWeather access and lifestyle index selection."""
        self._client = client
        self._authenticator = authenticator
        self._base_url = base_url
        self._output_language = QWEATHER_LANGUAGE_SUPPORT.match(output_language)
        self._api_language = QWEATHER_LANGUAGE_SUPPORT.api_code(self._output_language)
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
        """Fetch and normalize QWeather context for a location."""
        operation = "authentication"
        try:
            headers = {"Authorization": self._authenticator.authorization_header()}
            operation = "weather forecast"
            weather_response = await self._client.get(
                f"{self._base_url}/v7/weather/3d",
                params={
                    "location": f"{longitude:.2f},{latitude:.2f}",
                    "lang": self._api_language,
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
            forecast_index: int | None = None
            if forecast_date is None:
                selected_forecasts = daily_forecasts[:2]
            else:
                matching_forecasts = tuple(
                    (index, item)
                    for index, item in enumerate(daily_forecasts)
                    if isinstance(item, dict) and item.get("fxDate") == str(forecast_date)
                )
                selected_forecasts = tuple(item for _, item in matching_forecasts)
                if matching_forecasts:
                    forecast_index = matching_forecasts[0][0]
            weather_forecast = tuple(_format_qweather_day(item, self._output_language) for item in selected_forecasts)
            if not weather_forecast:
                if forecast_date is None:
                    raise WeatherContextError("QWeather returned no daily forecast")
                raise WeatherContextError(f"QWeather returned no forecast for {forecast_date}")

            indices_payload: dict[str, object] = {}
            lifestyle_advice: tuple[str, ...] = ()
            allergen_advice_available = False
            try:
                indices_days = "3d" if forecast_index else "1d"
                indices_response = await self._client.get(
                    f"{self._base_url}/v7/indices/{indices_days}",
                    params={
                        "type": ",".join(self._index_types),
                        "location": f"{longitude:.2f},{latitude:.2f}",
                        "lang": self._api_language,
                    },
                    headers=headers,
                    extensions=api_call_extensions("qweather", "lifestyle-indices"),
                )
                indices_response.raise_for_status()
                parsed_indices_payload = indices_response.json()
                if parsed_indices_payload.get("code") != "200":
                    raise _QWeatherResponseError(
                        f"non-success indices status code={_safe_api_status(parsed_indices_payload.get('code'))}"
                    )
                indices_payload = parsed_indices_payload
                daily_indices = tuple(
                    item
                    for item in indices_payload.get("daily", ())
                    if forecast_date is None or (isinstance(item, dict) and item.get("date") == str(forecast_date))
                )
                lifestyle_advice = tuple(
                    _format_qweather_lifestyle(item, self._output_language) for item in daily_indices
                )
                allergen_advice_available = any(
                    str(item.get("type")) == self._allergen_index_type
                    for item in daily_indices
                    if isinstance(item, dict)
                )
            except (httpx.HTTPError, _QWeatherResponseError, KeyError, TypeError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, _QWeatherResponseError) else _safe_provider_error(exc)
                _LOGGER.warning(
                    "Weather API optional call failed provider=qweather operation=lifestyle-indices reason=%s",
                    reason,
                )
            source_url = str(
                weather_payload.get("fxLink") or indices_payload.get("fxLink") or "https://www.qweather.com/"
            )
            update_time = weather_payload.get("updateTime")
            if not isinstance(update_time, str) or not update_time.strip():
                update_time = indices_payload.get("updateTime")
            if not isinstance(update_time, str) or not update_time.strip():
                raise WeatherContextError("QWeather response is missing a non-empty updateTime")
            observed_at = parse_datetime_with_default_timezone(
                update_time.strip(),
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

        air_quality = await self._fetch_air_quality(
            latitude,
            longitude,
            headers,
            source_url,
            forecast_index=forecast_index,
        )
        return WeatherContextSnapshot(
            source_id="weather:qweather",
            source_name="QWeather",
            source_url=source_url,
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            lifestyle_advice=lifestyle_advice,
            air_quality=air_quality,
            allergen_advice_available=allergen_advice_available,
            output_language=self._output_language,
        )

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch QWeather context for an explicit forecast date."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)

    async def _fetch_air_quality(
        self,
        latitude: float,
        longitude: float,
        headers: dict[str, str],
        source_url: str,
        *,
        forecast_index: int | None,
    ) -> AirQualitySnapshot | None:
        try:
            endpoint = "current" if forecast_index is None else "daily"
            response = await self._client.get(
                f"{self._base_url}/airquality/v1/{endpoint}/{latitude:.2f}/{longitude:.2f}",
                params={"lang": self._api_language},
                headers=headers,
                extensions=api_call_extensions("qweather", "air-quality"),
            )
            response.raise_for_status()
            payload = response.json()
            effective_at = None
            time_kind = AirQualityTimeKind.OBSERVATION
            if forecast_index is not None:
                days = payload["days"]
                if (
                    not isinstance(days, list)
                    or forecast_index >= len(days)
                    or not isinstance(days[forecast_index], dict)
                ):
                    raise ValueError("daily air-quality forecast is unavailable")
                payload = days[forecast_index]
                effective_at = parse_datetime_with_default_timezone(
                    str(payload["forecastStartTime"]),
                    "Asia/Shanghai",
                    context="QWeather air-quality forecast start time",
                )
                time_kind = AirQualityTimeKind.FORECAST
            return _qweather_air_quality_snapshot(
                payload,
                source_url,
                effective_at,
                time_kind,
                self._output_language,
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=qweather operation=air-quality reason=%s",
                _safe_provider_error(exc),
            )
            return None


def _safe_api_status(value: object) -> str:
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit():
        return value
    return "invalid"


def _first_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    values = payload[key]
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        raise ValueError(f"{key} must contain at least one object")
    return values[0]


def _qweather_air_quality_snapshot(
    payload: dict[str, Any],
    source_url: str,
    effective_at: pendulum.DateTime | None,
    time_kind: AirQualityTimeKind,
    output_language: str,
) -> AirQualitySnapshot:
    labels = localized_labels(output_language, _QWEATHER_FORMATS)
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
        effective_at=effective_at,
        time_kind=time_kind,
        aqi=aqi,
        aqi_display=str(index.get("aqiDisplay", index["aqi"])),
        aqi_standard=_aqi_standard(index),
        pm25_aqi=_sub_index(pm25, str(index["code"])),
        pm25_concentration=float(concentration["value"]),
        pm25_unit=str(concentration["unit"]),
        category=str(index.get("category", labels["unknown"])),
        health_guidance=str(advice.get("generalPopulation") or health.get("effect", "")),
        output_language=output_language,
    )


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


def _format_qweather_lifestyle(item: dict[str, object], language: str) -> str:
    labels = localized_labels(language, _QWEATHER_FORMATS)
    name = str(item["name"])
    return labels["lifestyle"].format(
        name=name,
        category=str(item.get("category", labels["unknown"])),
        text=str(item.get("text") or labels["no_details"]),
    )


def _format_qweather_day(item: object, language: str) -> str:
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
    return localized_labels(language, _QWEATHER_FORMATS)["day"].format(
        date=item["fxDate"],
        day=item["textDay"],
        night=item["textNight"],
        minimum=item["tempMin"],
        maximum=item["tempMax"],
        wind=item["windDirDay"],
        scale=item["windScaleDay"],
        humidity=item["humidity"],
        precipitation=item["precip"],
    )
