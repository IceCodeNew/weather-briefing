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
                    "time": {"s": "2026-07-13 08:00:00"},
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
        ).fetch(39.911389, 116.380556)

    assert snapshot.aqi == 120
    assert snapshot.aqi_standard == "US EPA"
    assert snapshot.pm25_aqi == 100
    assert snapshot.pm25_concentration is None
    document = air_quality_to_document(snapshot)
    assert "AQI：120（标准：US EPA" in document.content
    assert "PM2.5 原始浓度：不可用" in document.content
    assert "折算" not in document.content
