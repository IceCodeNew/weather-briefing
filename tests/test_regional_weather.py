import httpx
import pendulum
import pytest

from weather_briefing.regional_weather import (
    NEA_LANGUAGE_SUPPORT,
    NEASingaporeNowcastProvider,
    RegionalWeatherProviderError,
    _first_item,
    _parse_singapore_time,
)
from weather_briefing.weather_context import snapshot_to_documents


async def test_nea_nowcast_provider_normalizes_v2_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/two-hr-forecast")
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "timestamp": "2026-07-20T10:00:00+08:00",
                            "forecasts": [
                                {"area": "Central", "forecast": "Thundery Showers"},
                                {"area": "West", "forecast": "Fair"},
                            ],
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await NEASingaporeNowcastProvider(client).fetch(1.3, 103.8)

    assert snapshot.output_language == "en"
    assert NEASingaporeNowcastProvider.language_support == NEA_LANGUAGE_SUPPORT
    assert snapshot.observed_at == pendulum.datetime(2026, 7, 20, 10, tz="Asia/Singapore")
    assert "Central: Thundery Showers" in snapshot.weather_forecast[0]
    document = snapshot_to_documents(snapshot)[0]
    assert "Weather forecast:\n- Next two hours:" in document.content
    assert "今明天气预报" not in document.content


async def test_nea_nowcast_sends_api_key_and_supports_legacy_timestamp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "update_timestamp": "2026-07-20T10:00:00+08:00",
                        "forecasts": [{"forecast": "Fair"}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await NEASingaporeNowcastProvider(client, api_key="key").fetch(1.3, 103.8)

    assert "Singapore: Fair" in snapshot.weather_forecast[0]


@pytest.mark.parametrize("area", (None, "", "  "))
async def test_nea_nowcast_defaults_missing_or_blank_area(area: object) -> None:
    payload = {
        "items": [
            {
                "timestamp": "2026-07-20T10:00:00+08:00",
                "forecasts": [{"area": area, "forecast": "Fair"}],
            }
        ]
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        snapshot = await NEASingaporeNowcastProvider(client).fetch(1.3, 103.8)

    assert "Singapore: Fair" in snapshot.weather_forecast[0]


@pytest.mark.parametrize(
    "payload",
    (
        {"data": {"items": [{"forecasts": []}]}},
        {"data": {"items": [{"forecasts": [None]}]}},
    ),
)
async def test_nea_nowcast_rejects_empty_or_invalid_forecasts(payload: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(RegionalWeatherProviderError, match="no forecasts|no valid"):
            await NEASingaporeNowcastProvider(client).fetch(1.3, 103.8)


async def test_nea_nowcast_wraps_transport_errors() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(RegionalWeatherProviderError, match="HTTP 500"):
            await NEASingaporeNowcastProvider(client).fetch(1.3, 103.8)


async def test_nea_nowcast_sanitizes_non_status_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RegionalWeatherProviderError, match="ConnectError") as exc_info:
            await NEASingaporeNowcastProvider(client).fetch(1.3, 103.8)

    assert "private endpoint details" not in str(exc_info.value)


@pytest.mark.parametrize("payload", ([], {"data": []}, {"data": {"items": []}}))
def test_nea_first_item_rejects_invalid_shapes(payload: object) -> None:
    with pytest.raises(RegionalWeatherProviderError):
        _first_item(payload)


def test_regional_time_parsers_fall_back_when_timestamp_is_missing(monkeypatch) -> None:
    singapore_now = pendulum.datetime(2026, 7, 20, 10, tz="Asia/Singapore")
    monkeypatch.setattr(pendulum, "now", lambda timezone: singapore_now)

    assert _parse_singapore_time(None) == singapore_now
