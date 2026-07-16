import base64
from typing import TypeGuard

import httpx
import jwt
import pendulum
import pytest

from weather_briefing.models import AirQualitySnapshot, AllergenSnapshot, WeatherContextSnapshot
from weather_briefing.reference_data import ReferenceDataError
from weather_briefing.time_utils import parse_aware_datetime
from weather_briefing.weather_context import (
    AirQualitySupplementingWeatherProvider,
    FallbackWeatherContextProvider,
    LoggedWeatherContextProvider,
    OpenMeteoProvider,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    WeatherContextError,
    snapshot_to_documents,
)


class StaticAuthenticator:
    def authorization_header(self) -> str:
        return "Bearer runtime-token"


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


_QWEATHER_DAILY_ITEM = {
    "fxDate": "2026-07-13",
    "textDay": "晴",
    "textNight": "晴",
    "tempMin": "20",
    "tempMax": "30",
    "windDirDay": "南风",
    "windScaleDay": "3-4",
    "humidity": "60",
    "precip": "0.0",
}


def _qweather_weather_response(*, fx_link: str | None = None) -> httpx.Response:
    payload: dict[str, object] = {
        "code": "200",
        "updateTime": "2026-07-13T08:00",
        "daily": [_QWEATHER_DAILY_ITEM],
    }
    if fx_link is not None:
        payload["fxLink"] = fx_link
    return httpx.Response(200, json=payload)


def _qweather_successful_indices_response() -> httpx.Response:
    return httpx.Response(200, json={"code": "200", "daily": []})


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
                            "type": "1",
                            "name": "运动指数",
                            "category": "适宜",
                            "text": "适宜进行户外运动。",
                        },
                        {
                            "type": "7",
                            "name": "过敏指数",
                            "category": "不易发",
                            "text": "天气条件不易诱发过敏。",
                        },
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
    assert snapshot.lifestyle_advice == (
        "运动指数（适宜）：适宜进行户外运动。",
        "过敏指数（不易发）：天气条件不易诱发过敏。",
    )
    assert snapshot.allergen_advice_available
    assert snapshot.air_quality is not None
    assert snapshot.air_quality.source_name == "QWeather"
    assert snapshot.air_quality.source_url == "https://www.qweather.com/weather/test.html"
    assert snapshot.air_quality.aqi_standard == "中国环境空气质量指数（cn-mee）"
    assert snapshot.air_quality.observed_at is None
    documents = snapshot_to_documents(snapshot)
    assert [item.id for item in documents] == [
        "weather:qweather",
        "air-quality:qweather",
    ]
    assert documents[0].has_allergen_information


async def test_qweather_provider_selects_requested_future_date() -> None:
    target_date = pendulum.date(2026, 7, 15)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "updateTime": "2026-07-13T08:00",
                    "daily": [
                        {**_QWEATHER_DAILY_ITEM, "fxDate": "2026-07-13"},
                        {**_QWEATHER_DAILY_ITEM, "fxDate": "2026-07-14", "tempMax": "31"},
                        {**_QWEATHER_DAILY_ITEM, "fxDate": "2026-07-15", "tempMax": "32"},
                    ],
                },
            )
        assert request.url.path != "/v7/indices/1d", "Future forecasts must not request one-day lifestyle indices"
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(39.9, 116.3, forecast_date=target_date)

    assert len(snapshot.weather_forecast) == 1
    assert snapshot.weather_forecast[0].startswith("2026-07-15：")
    assert "20~32℃" in snapshot.weather_forecast[0]
    assert snapshot.lifestyle_advice == ()


async def test_qweather_provider_keeps_lifestyle_indices_for_first_forecast_date() -> None:
    target_date = pendulum.date(2026, 7, 13)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "updateTime": "2026-07-12T23:55",
                    "daily": [{**_QWEATHER_DAILY_ITEM, "fxDate": str(target_date)}],
                },
            )
        if request.url.path == "/v7/indices/1d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "daily": [{"name": "运动指数", "category": "适宜", "text": "今天适宜运动"}],
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch_for_date(39.9, 116.3, target_date)

    assert snapshot.lifestyle_advice == ("运动指数（适宜）：今天适宜运动",)


async def test_qweather_provider_rejects_unavailable_forecast_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v7/weather/3d"
        return httpx.Response(
            200,
            json={
                "code": "200",
                "updateTime": "2026-07-13T08:00",
                "daily": [{**_QWEATHER_DAILY_ITEM, "fxDate": "2026-07-13"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        )
        with pytest.raises(WeatherContextError, match="no forecast for 2026-07-15"):
            await provider.fetch(39.9, 116.3, forecast_date=pendulum.date(2026, 7, 15))


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
    assert snapshot.air_quality.source_name == "Open-Meteo"
    assert snapshot.air_quality.aqi_standard == "U.S. AQI"
    assert snapshot.air_quality.pm25_concentration == 9.5


async def test_open_meteo_provider_requests_only_selected_future_date() -> None:
    target_date = pendulum.date(2026, 7, 15)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "weather.example.invalid":
            assert request.url.params["start_date"] == "2026-07-15"
            assert request.url.params["end_date"] == "2026-07-15"
            assert "forecast_days" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "timezone": "Asia/Shanghai",
                    "current": {"time": "2026-07-13T22:00"},
                    "daily": {
                        "time": ["2026-07-15"],
                        "weather_code": [61],
                        "temperature_2m_max": [29],
                        "temperature_2m_min": [22],
                        "apparent_temperature_max": [31],
                        "apparent_temperature_min": [23],
                        "precipitation_sum": [8],
                        "precipitation_probability_max": [80],
                        "wind_speed_10m_max": [18],
                        "wind_gusts_10m_max": [28],
                        "wind_direction_10m_dominant": [120],
                        "uv_index_max": [3],
                    },
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(
            client,
            weather_base_url="https://weather.example.invalid",
            air_quality_base_url="https://air.example.invalid",
        ).fetch_for_date(39.9, 116.3, target_date)

    assert len(snapshot.weather_forecast) == 1
    assert snapshot.weather_forecast[0].startswith("2026-07-15：")


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


async def test_weather_provider_preserves_forecast_date_during_fallback() -> None:
    target_date = pendulum.date(2026, 7, 15)
    received_dates: list[pendulum.Date | None] = []
    undated_calls: list[tuple[float, float]] = []
    expected = WeatherContextSnapshot(
        source_id="weather:fallback",
        source_name="Fallback weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("2026-07-15 forecast",),
    )

    class UndatedProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            undated_calls.append((latitude, longitude))
            return expected

    class SuccessfulProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return expected

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            received_dates.append(forecast_date)
            return await self.fetch(latitude, longitude)

    undated_provider = UndatedProvider()
    assert await undated_provider.fetch(1, 2) == expected
    provider = FallbackWeatherContextProvider(undated_provider, SuccessfulProvider())

    assert await provider.fetch_for_date(1, 2, target_date) == expected
    assert received_dates == [target_date]
    assert undated_calls == [(1, 2)]


async def test_weather_provider_logs_failed_fallback_and_successful_call(caplog) -> None:
    expected = WeatherContextSnapshot(
        source_id="weather:fallback",
        source_name="Fallback weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )

    class FailingProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            raise WeatherContextError("QWeather weather forecast failed: HTTP 401")

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            return await self.fetch(latitude, longitude)

    class SuccessfulProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return expected

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            return await self.fetch(latitude, longitude)

    provider = FallbackWeatherContextProvider(
        LoggedWeatherContextProvider("qweather", FailingProvider()),
        LoggedWeatherContextProvider("open-meteo", SuccessfulProvider()),
    )

    with caplog.at_level("INFO", logger="weather_briefing.weather_context"):
        assert await provider.fetch_for_date(1, 2, pendulum.date(2026, 7, 15)) == expected

    assert "Weather API call started provider=qweather" in caplog.text
    assert "Weather API call failed provider=qweather" in caplog.text
    assert "reason=QWeather weather forecast failed: HTTP 401" in caplog.text
    assert "Weather API call succeeded provider=open-meteo" in caplog.text
    assert "source_id=weather:fallback" in caplog.text


async def test_weather_provider_logs_unexpected_error_type_without_message(caplog) -> None:
    class UnexpectedFailureProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            raise RuntimeError("sensitive upstream detail")

    provider = LoggedWeatherContextProvider("test-provider", UnexpectedFailureProvider())

    with (
        caplog.at_level("WARNING", logger="weather_briefing.weather_context"),
        pytest.raises(RuntimeError, match="sensitive upstream detail"),
    ):
        await provider.fetch(1, 2)

    assert "Weather API call failed provider=test-provider" in caplog.text
    assert "reason=RuntimeError" in caplog.text
    assert "sensitive upstream detail" not in caplog.text


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

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            return await self.fetch(latitude, longitude)

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

    assert (await provider.fetch_for_date(1, 2, pendulum.date(2026, 7, 15))).air_quality == air


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


def test_qweather_jwt_rejects_invalid_lifetime() -> None:
    with pytest.raises(ValueError, match="between 1 and 86400"):
        QWeatherJWTAuthenticator(
            project_id="p",
            credential_id="c",
            private_key_base64=base64.b64encode(b"key").decode(),
            lifetime_seconds=0,
        )


def test_qweather_jwt_rejects_invalid_base64_key() -> None:
    with pytest.raises(ValueError, match="Base64-encoded"):
        QWeatherJWTAuthenticator(
            project_id="p",
            credential_id="c",
            private_key_base64="!!!invalid-base64!!!",
        )


async def test_qweather_rejects_non_success_weather_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(200, json={"code": "400"})
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeatherContextError, match="non-success weather status code=400"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_does_not_log_untrusted_api_status(caplog) -> None:
    untrusted_status = "400\nforged-log-entry"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": untrusted_status})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LoggedWeatherContextProvider(
            "qweather",
            QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ),
        )
        with (
            caplog.at_level("WARNING", logger="weather_briefing.weather_context"),
            pytest.raises(WeatherContextError, match="non-success weather status code=invalid"),
        ):
            await provider.fetch(1, 2)

    assert untrusted_status not in caplog.text
    assert "reason=QWeather returned a non-success weather status code=invalid" in caplog.text


async def test_qweather_rejects_empty_daily_forecast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(
                200,
                json={"code": "200", "updateTime": "2026-07-13T08:00", "daily": []},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeatherContextError, match="no daily forecast"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_air_quality_failure_is_logged_without_failing_weather(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("WARNING", logger="weather_briefing.weather_context"):
            snapshot = await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)

    assert snapshot.air_quality is None
    assert "provider=qweather operation=air-quality reason=HTTP 500" in caplog.text


async def test_open_meteo_rejects_empty_forecast() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "timezone": "UTC",
                    "current": {"time": "2026-07-13T08:00"},
                    "daily": {"time": [], "weather_code": []},
                },
            )
        )
    ) as client:
        with pytest.raises(WeatherContextError, match="no daily forecast"):
            await OpenMeteoProvider(client).fetch(1, 2)


@pytest.mark.parametrize(
    "missing_field",
    (
        "time",
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
    ),
)
async def test_open_meteo_forecast_identifies_missing_required_field(missing_field: str) -> None:
    response = _open_meteo_weather_response()
    daily = response["daily"]
    assert _is_string_keyed_dict(daily)
    del daily[missing_field]

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        with pytest.raises(
            WeatherContextError,
            match=f"weather forecast parsing failed: daily forecast missing required field: {missing_field}",
        ):
            await OpenMeteoProvider(client).fetch(1, 2)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("daily", [], "daily forecast must be an object"),
        ("weather_code", 1, "daily forecast field must be an array: weather_code"),
        ("weather_code", [], "daily forecast field has no value at index 0: weather_code"),
    ),
)
async def test_open_meteo_forecast_identifies_invalid_field_shape(field: str, value: object, message: str) -> None:
    response = _open_meteo_weather_response()
    if field == "daily":
        response[field] = value
    else:
        daily = response["daily"]
        assert _is_string_keyed_dict(daily)
        daily[field] = value

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        with pytest.raises(WeatherContextError, match=f"weather forecast parsing failed: {message}"):
            await OpenMeteoProvider(client).fetch(1, 2)


async def test_open_meteo_air_quality_failure_is_logged_without_failing_weather(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(
                200,
                json={
                    "timezone": "UTC",
                    "current": {"time": "2026-07-13T08:00"},
                    "daily": {
                        "time": ["2026-07-13"],
                        "weather_code": [1],
                        "temperature_2m_max": [30],
                        "temperature_2m_min": [20],
                        "apparent_temperature_max": [31],
                        "apparent_temperature_min": [21],
                        "precipitation_sum": [0],
                        "precipitation_probability_max": [10],
                        "wind_speed_10m_max": [10],
                        "wind_gusts_10m_max": [15],
                        "wind_direction_10m_dominant": [90],
                        "uv_index_max": [5],
                    },
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("WARNING", logger="weather_briefing.weather_context"):
            snapshot = await OpenMeteoProvider(client).fetch(1, 2)

    assert snapshot.air_quality is None
    assert "provider=open-meteo operation=air-quality reason=HTTP 500" in caplog.text


async def test_fallback_weather_provider_requires_at_least_one_provider() -> None:
    from weather_briefing.weather_context import FallbackWeatherContextProvider

    with pytest.raises(ValueError, match="At least one"):
        FallbackWeatherContextProvider()


async def test_supplement_when_air_quality_present_skips_fallback() -> None:
    air = AirQualitySnapshot(
        source_id="air-quality:test",
        source_name="Test",
        source_url="https://example.invalid",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        aqi=42,
        aqi_display="42",
        aqi_standard="US EPA",
        pm25_aqi=None,
        pm25_concentration=None,
        pm25_unit=None,
        category="good",
        health_guidance="ok",
    )
    weather = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
        air_quality=air,
    )

    class WeatherProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return weather

    class FailingAirProvider:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            raise AssertionError("should not be called")

    provider = AirQualitySupplementingWeatherProvider(WeatherProvider(), FailingAirProvider())

    assert (await provider.fetch(1, 2)).air_quality == air


async def test_aqicn_fallback_raises_weather_context_error_on_air_quality_error() -> None:
    from weather_briefing.air_quality import AirQualityError

    weather = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )

    class WeatherProvider:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return weather

    class FailingAirProvider:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            raise AirQualityError("aqicn failed")

    provider = AirQualitySupplementingWeatherProvider(WeatherProvider(), FailingAirProvider())

    with pytest.raises(WeatherContextError, match="AQICN fallback failed"):
        await provider.fetch(1, 2)


async def test_qweather_rejects_non_success_indices_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response()
        if request.url.path == "/v7/indices/1d":
            return httpx.Response(200, json={"code": "400"})
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeatherContextError, match="non-success indices status"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_rejects_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(WeatherContextError, match="weather forecast failed: HTTP 500"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_rejects_jwt_error(monkeypatch) -> None:
    class FailingAuthenticator:
        def authorization_header(self) -> str:
            raise jwt.PyJWTError("test jwt failure")

    async with httpx.AsyncClient() as client:
        with pytest.raises(WeatherContextError, match="authentication failed: PyJWTError"):
            await QWeatherProvider(
                client,
                authenticator=FailingAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_open_meteo_passes_api_key() -> None:
    handler_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        handler_requests.append(request)
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "current": {"time": "2026-07-13T08:00"},
                "daily": {
                    "time": ["2026-07-13"],
                    "weather_code": [1],
                    "temperature_2m_max": [30],
                    "temperature_2m_min": [20],
                    "apparent_temperature_max": [31],
                    "apparent_temperature_min": [21],
                    "precipitation_sum": [0],
                    "precipitation_probability_max": [10],
                    "wind_speed_10m_max": [10],
                    "wind_gusts_10m_max": [15],
                    "wind_direction_10m_dominant": [90],
                    "uv_index_max": [5],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client, api_key="test-api-key").fetch(1, 2)

    assert snapshot.source_id == "weather:open-meteo"
    assert handler_requests[0].url.params["apikey"] == "test-api-key"


async def test_open_meteo_rejects_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(WeatherContextError, match="weather forecast failed: HTTP 500"):
            await OpenMeteoProvider(client).fetch(1, 2)


async def test_open_meteo_air_quality_passes_api_key() -> None:
    air_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(
                200,
                json={
                    "timezone": "UTC",
                    "current": {"time": "2026-07-13T08:00"},
                    "daily": {
                        "time": ["2026-07-13"],
                        "weather_code": [1],
                        "temperature_2m_max": [30],
                        "temperature_2m_min": [20],
                        "apparent_temperature_max": [31],
                        "apparent_temperature_min": [21],
                        "precipitation_sum": [0],
                        "precipitation_probability_max": [10],
                        "wind_speed_10m_max": [10],
                        "wind_gusts_10m_max": [15],
                        "wind_direction_10m_dominant": [90],
                        "uv_index_max": [5],
                    },
                },
            )
        air_requests.append(request)
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client, api_key="test-api-key").fetch(1, 2)

    assert snapshot.air_quality is not None
    assert air_requests[0].url.params["apikey"] == "test-api-key"


async def test_snapshot_to_documents_without_air_quality() -> None:
    snapshot = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Test",
        source_url="https://example.invalid/",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
    )

    documents = snapshot_to_documents(snapshot)

    assert [doc.id for doc in documents] == ["weather:test"]


async def test_qweather_air_quality_parses_invalid_indexes_gracefully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={"indexes": [{"code": "cn-mee", "aqi": 50}], "pollutants": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is None


async def test_qweather_air_quality_handles_missing_pollutant_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
                    }
                ],
                "pollutants": [
                    {
                        "code": "pm2p5",
                        "concentration": {"value": 10.0, "unit": "μg/m3"},
                        "subIndexes": [],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is not None
    assert snapshot.air_quality.pm25_aqi is None


async def test_qweather_air_quality_parse_failure_due_to_non_dict_indexes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "indexes": "not-a-list",
                "pollutants": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is None


async def test_qweather_lifestyle_handles_non_dict_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response()
        if request.url.path == "/v7/indices/1d":
            return httpx.Response(200, json={"code": "200", "daily": ["not-a-dict"]})
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeatherContextError, match="lifestyle indices failed: TypeError"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_forecast_handles_non_dict_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "updateTime": "2026-07-13T08:00",
                    "daily": [
                        _QWEATHER_DAILY_ITEM,
                        "not-a-dict",
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeatherContextError, match="weather forecast parsing failed: TypeError"):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


@pytest.mark.parametrize(
    "missing_field",
    (
        "fxDate",
        "textDay",
        "textNight",
        "tempMin",
        "tempMax",
        "windDirDay",
        "windScaleDay",
        "humidity",
        "precip",
    ),
)
async def test_qweather_forecast_identifies_missing_required_field(missing_field: str) -> None:
    incomplete_forecast = {key: value for key, value in _QWEATHER_DAILY_ITEM.items() if key != missing_field}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v7/weather/3d"
        return httpx.Response(
            200,
            json={
                "code": "200",
                "updateTime": "2026-07-13T08:00",
                "daily": [incomplete_forecast],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            WeatherContextError,
            match=f"weather forecast parsing failed: daily forecast missing required field: {missing_field}",
        ):
            await QWeatherProvider(
                client,
                authenticator=StaticAuthenticator(),
                base_url="https://api.example.invalid",
            ).fetch(1, 2)


async def test_qweather_air_quality_handles_non_list_pollutants() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
                    }
                ],
                "pollutants": "not-a-list",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is None


async def test_qweather_air_quality_handles_missing_pm2p5_pollutant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
                    }
                ],
                "pollutants": [
                    {
                        "code": "no2",
                        "concentration": {"value": 10.0, "unit": "μg/m3"},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is None


async def test_qweather_air_quality_handles_non_list_subindexes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
                    }
                ],
                "pollutants": [
                    {
                        "code": "pm2p5",
                        "concentration": {"value": 22.0, "unit": "μg/m3"},
                        "subIndexes": "not-a-list",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is not None
    assert snapshot.air_quality.pm25_aqi is None


async def test_qweather_air_quality_subindex_code_not_matching_standard() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
                    }
                ],
                "pollutants": [
                    {
                        "code": "pm2p5",
                        "concentration": {"value": 22.0, "unit": "μg/m3"},
                        "subIndexes": [{"code": "cn-mep", "aqi": 70}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await QWeatherProvider(
            client,
            authenticator=StaticAuthenticator(),
            base_url="https://api.example.invalid",
        ).fetch(1, 2)

    assert snapshot.air_quality is not None
    assert snapshot.air_quality.pm25_aqi is None


async def test_qweather_air_quality_handles_non_dict_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": "not-a-dict",
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
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
        ).fetch(1, 2)

    assert snapshot.air_quality is not None
    assert snapshot.air_quality.pm25_aqi == 68


def _open_meteo_weather_response() -> dict[str, object]:
    return {
        "timezone": "UTC",
        "current": {"time": "2026-07-13T08:00"},
        "daily": {
            "time": ["2026-07-13"],
            "weather_code": [1],
            "temperature_2m_max": [30],
            "temperature_2m_min": [20],
            "apparent_temperature_max": [31],
            "apparent_temperature_min": [21],
            "precipitation_sum": [0],
            "precipitation_probability_max": [10],
            "wind_speed_10m_max": [10],
            "wind_gusts_10m_max": [15],
            "wind_direction_10m_dominant": [90],
            "uv_index_max": [5],
        },
    }


async def test_open_meteo_provider_returns_allergen_when_pollen_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        assert request.url.path == "/v1/air-quality"
        current_params = request.url.params["current"]
        assert "birch_pollen" in current_params
        assert "grass_pollen" in current_params
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Berlin",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                    "alder_pollen": 0,
                    "birch_pollen": 15,
                    "grass_pollen": 3,
                    "mugwort_pollen": None,
                    "olive_pollen": None,
                    "ragweed_pollen": None,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(
            client,
            weather_base_url="https://weather.example.invalid",
            air_quality_base_url="https://air.example.invalid",
        ).fetch(52.52, 13.41)

    assert snapshot.air_quality is not None
    assert snapshot.allergen is not None
    assert snapshot.allergen.source_id == "allergen:open-meteo"
    assert snapshot.allergen.source_name == "Open-Meteo / CAMS ENSEMBLE 花粉过敏原"
    level_names = {level.name for level in snapshot.allergen.levels}
    assert {"桤木", "桦木", "禾本"} == level_names
    birch = next(level for level in snapshot.allergen.levels if level.name == "桦木")
    assert birch.category == "中"
    assert birch.concentration == 15
    assert snapshot.allergen.overall_category == "中"
    documents = snapshot_to_documents(snapshot)
    assert "allergen:open-meteo" in [doc.id for doc in documents]


async def test_open_meteo_provider_returns_no_allergen_when_pollen_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client).fetch(39.91, 116.38)

    assert snapshot.air_quality is not None
    assert snapshot.allergen is None


async def test_open_meteo_allergen_skips_invalid_pollen_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Berlin",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                    "birch_pollen": "nan",
                    "mugwort_pollen": -1,
                    "olive_pollen": "inf",
                    "ragweed_pollen": "not-a-number",
                    "grass_pollen": 5,
                    "alder_pollen": None,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client).fetch(52.52, 13.41)

    assert snapshot.allergen is not None
    level_names = {level.name for level in snapshot.allergen.levels}
    assert level_names == {"禾本"}


async def test_open_meteo_allergen_handles_missing_time_gracefully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        return httpx.Response(
            200,
            json={
                "current": {
                    "birch_pollen": 5,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client).fetch(52.52, 13.41)

    assert snapshot.allergen is not None
    assert snapshot.allergen.observed_at is None


def test_open_meteo_allergen_handles_invalid_time_gracefully() -> None:
    snapshot = OpenMeteoProvider._parse_allergen(
        {"time": "not-a-time", "birch_pollen": 5},
        {"timezone": "Europe/Berlin"},
        (("birch", "桦木"),),
    )

    assert snapshot is not None
    assert snapshot.observed_at is None


async def test_open_meteo_allergen_reference_failure_keeps_air_quality(monkeypatch) -> None:
    def fail_to_load_pollen_types() -> tuple[tuple[str, str], ...]:
        raise ReferenceDataError("invalid allergen data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        assert request.url.params["current"] == "us_aqi,us_aqi_pm2_5,pm2_5"
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Berlin",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                },
            },
        )

    monkeypatch.setattr(
        "weather_briefing.weather_context.pollen_type_names",
        fail_to_load_pollen_types,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client).fetch(52.52, 13.41)

    assert snapshot.air_quality is not None
    assert snapshot.allergen is None


async def test_open_meteo_allergen_guidance_failure_keeps_air_quality(monkeypatch) -> None:
    def fail_to_parse_allergen(*args) -> None:
        raise ReferenceDataError("invalid allergen guidance")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/forecast":
            return httpx.Response(200, json=_open_meteo_weather_response())
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Berlin",
                "current": {
                    "time": "2026-07-13T08:00",
                    "us_aqi": 42,
                    "us_aqi_pm2_5": 35,
                    "pm2_5": 9.5,
                    "birch_pollen": 5,
                },
            },
        )

    monkeypatch.setattr(OpenMeteoProvider, "_parse_allergen", fail_to_parse_allergen)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await OpenMeteoProvider(client).fetch(52.52, 13.41)

    assert snapshot.air_quality is not None
    assert snapshot.allergen is None


async def test_snapshot_to_documents_includes_allergen_document() -> None:
    snapshot = WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Test",
        source_url="https://example.invalid/",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        weather_forecast=("forecast",),
        allergen=AllergenSnapshot(
            source_id="allergen:test",
            source_name="Test allergen",
            source_url="https://example.invalid/allergen",
            observed_at=None,
            levels=(),
            overall_category="无",
            health_guidance="无花粉。",
        ),
    )

    documents = snapshot_to_documents(snapshot)

    assert [doc.id for doc in documents] == ["weather:test", "allergen:test"]


async def test_qweather_air_quality_handles_empty_attributions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v7/weather/3d":
            return _qweather_weather_response(fx_link="https://www.qweather.com/")
        if request.url.path == "/v7/indices/1d":
            return _qweather_successful_indices_response()
        return httpx.Response(
            200,
            json={
                "metadata": {"attributions": []},
                "indexes": [
                    {
                        "code": "cn-mee",
                        "aqi": 68,
                        "aqiDisplay": "68",
                        "category": "良",
                        "health": {"advice": {"generalPopulation": "ok"}},
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
        ).fetch(1, 2)

    assert snapshot.air_quality is not None
    assert snapshot.air_quality.pm25_aqi == 68
