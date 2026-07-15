import httpx
import pytest

from weather_briefing.air_quality import AirQualityError, AQICNProvider, air_quality_to_document, health_guidance
from weather_briefing.reference_data import ReferenceDataError


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
    assert "PM2.5 不可用" in document.content
    assert "原始浓度" not in document.content
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


async def test_aqicn_rejects_non_ok_status() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "error", "data": "..."}))
    ) as client:
        with pytest.raises(AirQualityError, match="non-success status"):
            await AQICNProvider(
                client,
                token="token",
                base_url="https://api.example.invalid",
            ).fetch(0, 0, "UTC")


async def test_aqicn_rejects_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(AirQualityError, match="AQICN request"):
            await AQICNProvider(
                client,
                token="token",
                base_url="https://api.example.invalid",
            ).fetch(0, 0, "UTC")


async def test_aqicn_rejects_missing_data_key() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"}))
    ) as client:
        with pytest.raises(AirQualityError, match="AQICN request"):
            await AQICNProvider(
                client,
                token="token",
                base_url="https://api.example.invalid",
            ).fetch(0, 0, "UTC")


async def test_aqicn_observed_at_returns_none_for_non_dict_time() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "aqi": 42,
                        "time": "not-a-dict",
                        "city": {
                            "name": "Test",
                            "url": "https://example.invalid/",
                        },
                        "iaqi": {},
                    },
                },
            )
        )
    ) as client:
        snapshot = await AQICNProvider(
            client,
            token="token",
            base_url="https://api.example.invalid",
        ).fetch(0, 0, "UTC")

    assert snapshot.observed_at is None


async def test_aqicn_observed_at_returns_none_for_empty_time_string() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "aqi": 42,
                        "time": {"s": "  ", "iso": "  "},
                        "city": {
                            "name": "Test",
                            "url": "https://example.invalid/",
                        },
                        "iaqi": {},
                    },
                },
            )
        )
    ) as client:
        snapshot = await AQICNProvider(
            client,
            token="token",
            base_url="https://api.example.invalid",
        ).fetch(0, 0, "UTC")

    assert snapshot.observed_at is None


def test_health_guidance_unbounded_band_required(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "10", "category": "bad", "guidance": "do not go out"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="must end with an unbounded band"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_bands_must_be_non_empty_list(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [],
    )
    with pytest.raises(ReferenceDataError, match="non-empty list"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_bands_must_be_list(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: "not-a-list",
    )
    with pytest.raises(ReferenceDataError, match="non-empty list"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_invalid_band_entry(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "10", "category": "bad"},
            {"maximum_aqi": None, "category": "good", "guidance": "ok"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="Invalid air quality guidance band"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_bounded_last_band(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "10", "category": "bad", "guidance": "avoid"},
            {"maximum_aqi": "20", "category": "worse", "guidance": "stay inside"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="must end with an unbounded band"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_middle_none_bounded(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": None, "category": "bad", "guidance": "avoid"},
            {"maximum_aqi": "20", "category": "worse", "guidance": "stay inside"},
            {"maximum_aqi": None, "category": "worst", "guidance": "hide"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="must end with an unbounded band"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_non_unique_bounds(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "10", "category": "bad", "guidance": "avoid"},
            {"maximum_aqi": "10", "category": "worse", "guidance": "stay inside"},
            {"maximum_aqi": None, "category": "worst", "guidance": "hide"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="must be unique, increasing"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_air_quality_guidance_negative_bound(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "-5", "category": "bad", "guidance": "avoid"},
            {"maximum_aqi": None, "category": "worst", "guidance": "hide"},
        ],
    )
    with pytest.raises(ReferenceDataError, match="must be unique, increasing, and non-negative"):
        _guidance_bands()

    _guidance_bands.cache_clear()


def test_health_guidance_uses_unbounded_last_band(monkeypatch) -> None:
    from weather_briefing.air_quality import _guidance_bands

    _guidance_bands.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.air_quality.reference_value",
        lambda filename, *path: [
            {"maximum_aqi": "50", "category": "优", "guidance": "Good"},
            {"maximum_aqi": None, "category": "差", "guidance": "Bad"},
        ],
    )
    category, guidance = health_guidance(999)
    assert category == "差"
    assert guidance == "Bad"

    _guidance_bands.cache_clear()
