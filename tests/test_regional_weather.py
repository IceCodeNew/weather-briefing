import httpx
import pendulum
import pytest

from weather_briefing.regional_weather import (
    NEA_LANGUAGE_SUPPORT,
    JMAJapanForecastProvider,
    NEASingaporeNowcastProvider,
    RegionalWeatherProviderError,
    _first_item,
    _jma_forecast_lines,
    _parse_japan_time,
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
    japan_now = pendulum.datetime(2026, 7, 20, 11, tz="Asia/Tokyo")
    monkeypatch.setattr(pendulum, "now", lambda timezone: singapore_now if timezone == "Asia/Singapore" else japan_now)

    assert _parse_singapore_time(None) == singapore_now
    assert _parse_japan_time(None) == japan_now


@pytest.mark.parametrize("office_code", ("13000", None))
async def test_jma_provider_rejects_invalid_office_code(office_code: str | None) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="six digits"):
            JMAJapanForecastProvider(client, office_code=office_code)


@pytest.mark.parametrize(
    ("payload", "message"),
    (([], "non-empty array"), ([{}], "no time series"), ([{"timeSeries": [None]}], "no usable")),
)
async def test_jma_provider_rejects_invalid_responses(payload: object, message: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(RegionalWeatherProviderError, match=message):
            await JMAJapanForecastProvider(client, office_code="130000").fetch(1.0, 1.0)


async def test_jma_provider_wraps_transport_errors() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(RegionalWeatherProviderError, match="HTTP 500"):
            await JMAJapanForecastProvider(client, office_code="130000").fetch(1.0, 1.0)


def test_jma_forecast_line_parser_skips_invalid_entries_and_defaults_area_name() -> None:
    series: list[object] = [
        None,
        {
            "areas": [
                None,
            ]
        },
    ]
    assert _jma_forecast_lines(series) == ()
    assert _jma_forecast_lines([{"areas": None}]) == ()
    assert _jma_forecast_lines([{"areas": [{"area": {}, "weathers": ["晴れ"], "winds": [7]}]}]) == ("日本: 晴れ",)
    assert _jma_forecast_lines([{"areas": [{"weathers": [], "winds": ["北の風"]}]}]) == ("日本の風: 北の風",)


@pytest.mark.parametrize("name", (None, "", "  "))
def test_jma_forecast_line_parser_defaults_missing_or_blank_area_name(name: object) -> None:
    area = {"area": {"name": name}, "weathers": ["晴れ"]}

    assert _jma_forecast_lines([{"areas": [area]}]) == ("日本: 晴れ",)


def test_jma_forecast_line_parser_preserves_all_areas() -> None:
    areas = [
        {"area": {"name": "東京都"}, "weathers": ["晴れ"], "winds": ["南の風"]},
        {"area": {"name": "伊豆諸島"}, "weathers": ["曇り"], "winds": ["東の風"]},
    ]

    assert _jma_forecast_lines([{"areas": areas}]) == (
        "東京都: 晴れ",
        "東京都の風: 南の風",
        "伊豆諸島: 曇り",
        "伊豆諸島の風: 東の風",
    )


def test_jma_forecast_line_parser_selects_target_date_and_skips_invalid_times() -> None:
    series = {
        "timeDefines": [None, "invalid", "2026-07-21T00:00:00+09:00"],
        "areas": [{"weathers": ["晴れ", "雨", "曇り"], "winds": ["南の風"]}],
    }

    assert _jma_forecast_lines([series], forecast_date=pendulum.date(2026, 7, 21)) == ("日本: 曇り",)
    assert _jma_forecast_lines([series], forecast_date=pendulum.date(2026, 7, 22)) == ()
    assert _jma_forecast_lines([{"areas": []}], forecast_date=pendulum.date(2026, 7, 21)) == ()


async def test_jma_provider_rejects_unavailable_target_date() -> None:
    payload = [
        {
            "timeSeries": [
                {
                    "timeDefines": ["2026-07-20T00:00:00+09:00"],
                    "areas": [{"weathers": ["晴れ"]}],
                }
            ]
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(RegionalWeatherProviderError, match="no usable entries for 2026-07-21"):
            await JMAJapanForecastProvider(client, office_code="130000").fetch_for_date(
                1.0,
                1.0,
                pendulum.date(2026, 7, 21),
            )


async def test_jma_provider_normalizes_forecast_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/130000.json")
        return httpx.Response(
            200,
            json=[
                {
                    "reportDatetime": "2026-07-20T05:00:00+09:00",
                    "timeSeries": [
                        {
                            "areas": [
                                {
                                    "area": {"name": "東京都"},
                                    "weathers": ["晴れ"],
                                    "winds": ["南の風"],
                                }
                            ]
                        }
                    ],
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await JMAJapanForecastProvider(client, office_code="130000").fetch(1.0, 1.0)

    assert snapshot.output_language == "ja"
    assert snapshot.observed_at == pendulum.datetime(2026, 7, 20, 5, tz="Asia/Tokyo")
    assert snapshot.weather_forecast == ("東京都: 晴れ", "東京都の風: 南の風")
