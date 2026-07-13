import httpx

from weather_briefing.air_quality import AQICNProvider, air_quality_to_document


async def test_aqicn_provider_labels_aqi_standard_without_converting_pm25() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/feed/geo:39.911389;116.380556/"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "aqi": 120,
                    "time": {"iso": "2026-07-13T08:00:00+08:00"},
                    "city": {
                        "name": "Test station",
                        "url": "https://aqicn.org/city/test/",
                    },
                    "iaqi": {"pm25": {"v": 100}},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await AQICNProvider(
            client,
            token="runtime-token",
            base_url="https://api.example.invalid",
        ).fetch(39.911389, 116.380556, "Asia/Shanghai")

    assert snapshot.aqi == 120
    assert snapshot.aqi_standard == "US EPA"
    assert snapshot.observed_at is not None
    assert snapshot.observed_at.to_iso8601_string() == "2026-07-13T08:00:00+08:00"
    assert snapshot.pm25_aqi == 100
    assert snapshot.pm25_concentration is None
    document = air_quality_to_document(snapshot)
    assert "AQI：120（标准：US EPA" in document.content
    assert "PM2.5 原始浓度：不可用" in document.content
    assert "折算" not in document.content


async def test_aqicn_keeps_valid_aqi_when_official_time_has_no_offset() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "aqi": 42,
                    "time": {"s": "2026-07-13 08:00:00"},
                    "city": {
                        "name": "Test station",
                        "url": "https://aqicn.org/city/test/",
                    },
                    "iaqi": {},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await AQICNProvider(
            client,
            token="runtime-token",
            base_url="https://api.example.invalid",
        ).fetch(39.911389, 116.380556, "Asia/Shanghai")

    assert snapshot.aqi == 42
    assert snapshot.observed_at is not None
    assert snapshot.observed_at.to_iso8601_string() == "2026-07-13T08:00:00+08:00"


async def test_aqicn_response_timezone_overrides_queried_location_timezone() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "aqi": 42,
                    "time": {"s": "2026-07-13 09:00:00", "tz": "+09:00"},
                    "city": {
                        "name": "Test station",
                        "url": "https://aqicn.org/city/test/",
                    },
                    "iaqi": {},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await AQICNProvider(
            client,
            token="runtime-token",
            base_url="https://api.example.invalid",
        ).fetch(35.0, 139.0, "Asia/Tokyo")

    assert snapshot.observed_at is not None
    assert snapshot.observed_at.to_iso8601_string() == "2026-07-13T09:00:00+09:00"
