import base64

import httpx
import jwt
import pendulum
import pytest

from weather_briefing.models import AirQualitySnapshot, WeatherContextSnapshot
from weather_briefing.time_utils import parse_aware_datetime
from weather_briefing.weather_context import (
    AirQualitySupplementingWeatherProvider,
    FallbackWeatherContextProvider,
    OpenMeteoProvider,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    WeatherContextError,
    snapshot_to_documents,
)


class StaticAuthenticator:
    def authorization_header(self) -> str:
        return "Bearer runtime-token"


def test_qweather_jwt_authenticator_delegates_eddsa_signing_to_pyjwt(monkeypatch) -> None:
    private_pem = "-----BEGIN PRIVATE KEY-----\ntest-key\n-----END PRIVATE KEY-----\n"
    encode_call: dict[str, object] = {}

    def fake_encode(
        payload: dict[str, object],
        key: str,
        *,
        algorithm: str,
        headers: dict[str, str],
    ) -> str:
        encode_call.update(
            payload=payload,
            key=key,
            algorithm=algorithm,
            headers=headers,
        )
        return "signed-token"

    monkeypatch.setattr(jwt, "encode", fake_encode)
    authenticator = QWeatherJWTAuthenticator(
        project_id="project-id",
        credential_id="credential-id",
        private_key_base64=base64.b64encode(private_pem.encode()).decode(),
        lifetime_seconds=900,
        clock=lambda: 1_700_000_000,
    )

    assert authenticator.authorization_header() == "Bearer signed-token"
    assert encode_call == {
        "payload": {
            "sub": "project-id",
            "iat": 1_699_999_970,
            "exp": 1_700_000_870,
        },
        "key": private_pem,
        "algorithm": "EdDSA",
        "headers": {"kid": "credential-id"},
    }


async def test_qweather_provider_returns_weather_lifestyle_and_air_quality() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer runtime-token"
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "updateTime": "2026-07-13T08:00",
                    "fxLink": "https://www.qweather.com/weather/test.html",
                    "daily": [
                        {
                            "fxDate": "2026-07-13",
                            "textDay": "多云",
                            "textNight": "阵雨",
                            "tempMin": "28",
                            "tempMax": "35",
                            "windDirDay": "东南风",
                            "windScaleDay": "3-4",
                            "humidity": "85",
                            "precip": "2.0",
                        },
                        {
                            "fxDate": "2026-07-14",
                            "textDay": "阵雨",
                            "textNight": "阴",
                            "tempMin": "27",
                            "tempMax": "33",
                            "windDirDay": "东风",
                            "windScaleDay": "4-5",
                            "humidity": "90",
                            "precip": "8.0",
                        },
                    ],
                },
            )
        if request.url.path == "/v7/indices/1d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "daily": [
                        {
                            "name": "运动指数",
                            "category": "适宜",
                            "text": "适宜进行户外运动。",
                        }
                    ],
                },
            )
        assert request.url.path.endswith("/39.91/116.38")
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "name": "中国环境空气质量指数",
                        "aqi": 68,
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "可以正常进行户外活动。"}},
                    }
                ],
                "pollutants": [
                    {
                        "code": "pm2p5",
                        "concentration": {"value": 22.0, "unit": "μg/m3"},
                        "subIndexes": [{"code": "cn-mee", "aqi": 68}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(39.911389, 116.380556)

    assert snapshot.source_id == "weather:qweather"
    assert snapshot.observed_at.to_iso8601_string() == "2026-07-13T08:00:00+08:00"
    assert len(snapshot.weather_forecast) == 2
    assert snapshot.lifestyle_advice == ("运动指数（适宜）：适宜进行户外运动。",)
    assert snapshot.air_quality is not None
    assert snapshot.air_quality.aqi_standard == "中国环境空气质量指数（cn-mee）"
    assert snapshot.air_quality.observed_at is None
    documents = snapshot_to_documents(snapshot)
    assert [item.id for item in documents] == [
        "weather:qweather",
        "air-quality:qweather",
    ]


async def test_open_meteo_provider_returns_global_weather_and_air_quality() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "weather.example.invalid":
            assert request.url.path == "/v1/forecast"
            assert request.url.params["forecast_days"] == "2"
            return httpx.Response(
                200,
                json={
                    "timezone": "Europe/Berlin",
                    "utc_offset_seconds": 7200,
                    "current": {"time": "2026-07-13T08:00", "relative_humidity_2m": 60},
                    "daily": {
                        "time": ["2026-07-13", "2026-07-14"],
                        "weather_code": [2, 61],
                        "temperature_2m_max": [30, 28],
                        "temperature_2m_min": [20, 19],
                        "apparent_temperature_max": [31, 29],
                        "apparent_temperature_min": [21, 20],
                        "precipitation_sum": [0, 5],
                        "precipitation_probability_max": [10, 80],
                        "wind_speed_10m_max": [12, 18],
                        "wind_gusts_10m_max": [20, 28],
                        "wind_direction_10m_dominant": [90, 120],
                        "uv_index_max": [7, 3],
                    },
                },
            )
        assert request.url.path == "/v1/air-quality"
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Berlin",
                "utc_offset_seconds": 7200,
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(
            client,
            weather_base_url="https://weather.example.invalid",
            air_quality_base_url="https://air.example.invalid",
        ).fetch(52.52, 13.41)

    assert len(snapshot.weather_forecast) == 2
    assert "WMO天气代码2" in snapshot.weather_forecast[0]
    assert snapshot.observed_at.to_iso8601_string() == "2026-07-13T08:00:00+02:00"
    assert snapshot.observed_at.timezone_name == "Europe/Berlin"
    assert snapshot.air_quality is not None
    assert snapshot.air_quality.aqi_standard == "U.S. AQI"
    assert snapshot.air_quality.pm25_concentration == 9.5


async def test_weather_provider_falls_back_after_primary_weather_failure() -> None:
    expected = WeatherContextSnapshot(
        source_id="weather:fallback",
        source_name="Fallback weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )

    class FailingProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            raise WeatherContextError("expected failure")

    class SuccessfulProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return expected

    provider = FallbackWeatherContextProvider(FailingProvider(), SuccessfulProvider())

    assert await provider.fetch(1, 2) == expected


async def test_missing_weather_air_quality_requires_optional_aqicn_configuration() -> None:
    snapshot = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )

    class WeatherProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return snapshot

    provider = AirQualitySupplementingWeatherProvider(WeatherProvider(), None)

    with pytest.raises(WeatherContextError, match="AQICN_API_TOKEN"):
        await provider.fetch(1, 2)


async def test_aqicn_supplements_weather_without_air_quality() -> None:
    weather = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )
    air = AirQualitySnapshot(
        source_id="air-quality:aqicn",
        source_name="AQICN",
        source_url="https://example.invalid/air",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        aqi=42,
        aqi_display="42",
        aqi_standard="US EPA",
        pm25_aqi=35,
        pm25_concentration=None,
        pm25_unit=None,
        category="优",
        health_guidance="Normal activity",
    )

    class WeatherProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return weather

    class AirProvider:
        async def fetch(
            self,
            latitude: float,
            longitude: float,
            timezone: str,
        ) -> AirQualitySnapshot:
            assert timezone == "UTC"
            return air

    provider = AirQualitySupplementingWeatherProvider(WeatherProvider(), AirProvider())

    assert (await provider.fetch(1, 2)).air_quality == air


async def test_aqicn_receives_explicit_offset_when_weather_time_has_no_timezone_name() -> None:
    weather = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Weather",
        source_url="https://example.invalid/weather",
        observed_at=parse_aware_datetime(
            "2026-07-13T08:00:00+05:30",
            context="Test weather snapshot time",
        ),
        weather_forecast=("forecast",),
    )
    air = AirQualitySnapshot(
        source_id="air-quality:aqicn",
        source_name="AQICN",
        source_url="https://example.invalid/air",
        observed_at=None,
        aqi=42,
        aqi_display="42",
        aqi_standard="US EPA",
        pm25_aqi=None,
        pm25_concentration=None,
        pm25_unit=None,
        category="优",
        health_guidance="Normal activity",
    )

    class WeatherProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return weather

    class AirProvider:
        async def fetch(
            self,
            latitude: float,
            longitude: float,
            timezone: str,
        ) -> AirQualitySnapshot:
            assert timezone == "+05:30"
            return air

    provider = AirQualitySupplementingWeatherProvider(WeatherProvider(), AirProvider())

    assert (await provider.fetch(1, 2)).air_quality == air
