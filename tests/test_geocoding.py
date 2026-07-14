from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from weather_briefing.geocoding import (
    CachedLocationResolver,
    FallbackGeocodingProvider,
    GeocodingError,
    NominatimGeocodingProvider,
    OpenMeteoGeocodingProvider,
    PrecisionReducingGeocodingProvider,
    possibly_mainland_china,
)
from weather_briefing.models import LocationSpec, ResolvedLocation
from weather_briefing.reference_data import reference_value


async def test_open_meteo_geocoder_resolves_coordinates_and_country() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "北京市西城区中南海"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "中南海",
                        "latitude": 39.911389,
                        "longitude": 116.380556,
                        "country_code": "CN",
                        "admin1": "北京市",
                        "timezone": "Asia/Shanghai",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("beijing", "北京市西城区中南海"))

    assert result.latitude == 39.911389
    assert result.country_code == "CN"
    assert result.is_mainland_china is True


async def test_open_meteo_geocoder_rejects_broad_first_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["count"] == "5"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "北京",
                        "latitude": 39.9042,
                        "longitude": 116.4074,
                        "country": "中国",
                        "country_code": "CN",
                        "admin1": "北京市",
                        "timezone": "Asia/Shanghai",
                    },
                    {
                        "name": "中南海",
                        "latitude": 39.911389,
                        "longitude": 116.380556,
                        "country": "中国",
                        "country_code": "CN",
                        "admin1": "北京市",
                        "admin2": "西城区",
                        "timezone": "Asia/Shanghai",
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("example", "中国北京市西城区中南海"))

    assert result.latitude == 39.911389
    assert result.longitude == 116.380556
    assert result.matched_name == "中南海"


async def test_nominatim_fallback_resolves_detailed_place_name() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "39.911389",
                    "lon": "116.380556",
                    "display_name": "详细中文地名, 西城区, 北京市, 中国",
                    "address": {"country_code": "cn", "state": "北京市"},
                }
            ],
        )

    class EmptyGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("no city match")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FallbackGeocodingProvider(
            EmptyGeocoder(),
            NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1"),
        )
        result = await provider.geocode(LocationSpec("beijing", "详细中文地名"))

    assert result.country_code == "CN"
    assert result.is_mainland_china is True
    assert requests[0].headers["user-agent"] == "weather-briefing-test/1"
    assert requests[0].url.params["addressdetails"] == "1"


async def test_nominatim_normalizes_region_suffix_and_rejects_unrelated_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "北京市西城区中南海"
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "39.911389",
                    "lon": "116.380556",
                    "display_name": "中南海, 西城区, 北京市, 中国",
                    "address": {"country_code": "cn", "state": "北京市"},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1").geocode(
            LocationSpec("beijing", "北京市西城区中南海地区")
        )

    assert result.latitude == 39.911389
    assert result.longitude == 116.380556


async def test_geocoder_reduces_precision_after_full_address_fails() -> None:
    queries: list[str] = []

    class RoadLevelGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            queries.append(location.name)
            if location.name.endswith("1号"):
                raise GeocodingError("no building match")
            return ResolvedLocation(
                location.id,
                location.name,
                39.911389,
                116.380556,
                "CN",
                "北京市",
                "Asia/Shanghai",
                True,
                matched_name="中南海, 西城区, 北京市, 中国",
            )

    original_name = "中国北京市西城区中南海1号"
    result = await PrecisionReducingGeocodingProvider(RoadLevelGeocoder()).geocode(
        LocationSpec("example", original_name)
    )

    assert queries == [original_name, "中国北京市西城区中南海"]
    assert result.name == original_name
    assert result.matched_name == "中南海, 西城区, 北京市, 中国"
    assert result.precision_reduced is True


async def test_resolver_caches_name_lookup(tmp_path: Path) -> None:
    class RecordingGeocoder:
        calls = 0

        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            self.calls += 1
            return ResolvedLocation(
                location.id,
                location.name,
                39.911389,
                116.380556,
                "CN",
                "北京市",
                "Asia/Shanghai",
                True,
            )

    geocoder = RecordingGeocoder()
    resolver = CachedLocationResolver(geocoder, tmp_path / "geocoding.json")
    location = LocationSpec("beijing", "北京市西城区中南海")

    first = await resolver.resolve_with_metadata(location)
    second = await resolver.resolve_with_metadata(location)

    assert first.location == second.location
    assert first.from_cache is False
    assert second.from_cache is True
    assert geocoder.calls == 1


async def test_resolver_rejects_obsolete_cache_record(tmp_path: Path) -> None:
    class NeverCalledGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise AssertionError("Invalid cache data must not trigger geocoding")

    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text(
        '{"example:Example":{"id":"example","name":"Example"}}',
        encoding="utf-8",
    )
    resolver = CachedLocationResolver(
        NeverCalledGeocoder(),
        cache_path,
    )

    with pytest.raises(GeocodingError, match="Invalid cached geocoding record"):
        await resolver.resolve(LocationSpec("example", "Example"))


async def test_coordinates_skip_geocoding_and_use_extreme_bounds(
    tmp_path: Path,
) -> None:
    class FailingGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise AssertionError("Coordinates must not trigger a geocoding request")

    resolver = CachedLocationResolver(FailingGeocoder(), tmp_path / "cache.json")

    beijing = await resolver.resolve(LocationSpec("beijing", "Beijing", 39.9, 116.4))
    outside = await resolver.resolve(LocationSpec("outside", "Example", 0.0, 0.0))
    latitude = reference_value("geography.json", "mainland_china_service_bounds", "latitude")
    longitude = reference_value("geography.json", "mainland_china_service_bounds", "longitude")

    assert beijing.is_mainland_china is True
    assert outside.is_mainland_china is False
    assert possibly_mainland_china(latitude["maximum"], longitude["maximum"]) is True
    assert possibly_mainland_china(latitude["maximum"] + 0.01, longitude["maximum"]) is False
    assert possibly_mainland_china(latitude["minimum"] - 0.01, longitude["minimum"]) is False


async def test_nominatim_requires_user_agent() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="identifying User-Agent"):
            NominatimGeocodingProvider(client, user_agent="  ")


async def test_fallback_geocoding_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError, match="At least one"):
        FallbackGeocodingProvider()


async def test_fallback_geocoding_raises_when_all_providers_fail() -> None:
    class FailingProvider:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("first failure")

    with pytest.raises(GeocodingError, match="No geocoder could resolve"):
        await FallbackGeocodingProvider(FailingProvider()).geocode(LocationSpec("test", "Test"))


async def test_cached_resolver_handles_broken_cache_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text("not-json", encoding="utf-8")

    class FailingProvider:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("test failure")

    resolver = CachedLocationResolver(
        FallbackGeocodingProvider(FailingProvider()),
        cache_path,
    )

    with pytest.raises(GeocodingError, match="readable JSON"):
        await resolver.resolve(LocationSpec("test", "Test"))


async def test_cached_resolver_rejects_non_dict_cache_root(tmp_path: Path) -> None:
    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text("[1, 2, 3]", encoding="utf-8")

    class FailingProvider:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("test failure")

    resolver = CachedLocationResolver(
        FallbackGeocodingProvider(FailingProvider()),
        cache_path,
    )

    with pytest.raises(GeocodingError, match="object"):
        await resolver.resolve(LocationSpec("test", "Test"))


async def test_nominatim_handles_invalid_response_structure() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[{"lat": "x", "lon": "y", "display_name": "test", "address": {}}],
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="response validation failed"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "test"))


async def test_open_meteo_passes_api_key_when_provided() -> None:
    handler_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        handler_requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Test City",
                        "latitude": 51.5,
                        "longitude": -0.1,
                        "country_code": "GB",
                        "admin1": "England",
                        "timezone": "Europe/London",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoGeocodingProvider(client, api_key="test-api-key").geocode(
            LocationSpec("test", "Test City")
        )

    assert handler_requests[0].url.params["apikey"] == "test-api-key"
    assert result.is_mainland_china is False


async def test_open_meteo_handles_empty_results() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))
    ) as client:
        with pytest.raises(GeocodingError, match="No geocoding result"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test"))


async def test_open_meteo_handles_results_not_a_list() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": "not-a-list"}))
    ) as client:
        with pytest.raises(GeocodingError, match="No geocoding result"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test"))


async def test_open_meteo_handles_no_matching_result() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Other",
                            "latitude": 0,
                            "longitude": 0,
                            "country_code": "XX",
                        }
                    ]
                },
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="No matching geocoding result"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test City"))


async def test_open_meteo_handles_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(GeocodingError, match="Geocoding request or response validation failed"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test"))


async def test_nominatim_queries_with_removable_terms_retry_on_mismatch() -> None:
    queries_received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries_received.append(str(request.url.params["q"]))
        q = str(request.url.params["q"])
        if q == "中国Test地区":
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": "51.5",
                        "lon": "-0.1",
                        "display_name": "Test City, England, GB",
                        "address": {"country_code": "gb", "state": "England"},
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "40.0",
                    "lon": "120.0",
                    "display_name": "Some other location",
                    "address": {},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NominatimGeocodingProvider(client, user_agent="test").geocode(
            LocationSpec("test", "中国Test地区")
        )

    assert len(queries_received) == 2
    assert result.latitude == 51.5


async def test_nominatim_handles_results_not_a_list() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json="not-a-list"))
    ) as client:
        with pytest.raises(GeocodingError, match="No Nominatim result"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "Test"))


async def test_nominatim_handles_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(GeocodingError, match="Nominatim request failed"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "Test"))


async def test_nominatim_handles_no_matching_results() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[
                    {
                        "lat": "1",
                        "lon": "2",
                        "display_name": "nothing related to query",
                        "address": {},
                    }
                ],
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="No Nominatim result"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "Test"))


async def test_nominatim_rate_limits_consecutive_requests(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monotonic_values = iter((100.0, 100.0, 100.25, 100.25))

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("weather_briefing.geocoding.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "weather_briefing.geocoding.time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "51.5",
                    "lon": "-0.1",
                    "display_name": "Test City, England, GB",
                    "address": {"country_code": "gb", "state": "England"},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NominatimGeocodingProvider(client, user_agent="test")
        await provider.geocode(LocationSpec("test", "Test City"))
        await provider.geocode(LocationSpec("test", "Test City"))

    assert calls == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.75)


async def test_precision_reducing_provider_exhausts_all_candidates() -> None:
    calls: list[str] = []

    class FailingGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            calls.append(location.name)
            raise GeocodingError("no match")

    with pytest.raises(GeocodingError, match="No geocoder could resolve location at a safe precision"):
        await PrecisionReducingGeocodingProvider(FailingGeocoder()).geocode(
            LocationSpec("test", "中国北京市西城区中南海1号")
        )

    assert calls == ["中国北京市西城区中南海1号", "中国北京市西城区中南海"]


async def test_precision_reducing_provider_continues_after_geocoding_error() -> None:
    calls: list[str] = []

    class PartialGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            calls.append(location.name)
            if "1号" in location.name:
                raise GeocodingError("no building match")
            return ResolvedLocation(
                location.id,
                location.name,
                39.9,
                116.3,
                "CN",
                "北京市",
                "Asia/Shanghai",
                True,
            )

    result = await PrecisionReducingGeocodingProvider(PartialGeocoder()).geocode(
        LocationSpec("test", "中国北京市西城区中南海1号")
    )

    assert result.precision_reduced is True
    assert len(calls) == 2


async def test_cached_resolver_rejects_non_dict_cached_value(tmp_path: Path) -> None:
    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text('{"test:Test":"not-a-dict"}', encoding="utf-8")

    class FailingProvider:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("test failure")

    resolver = CachedLocationResolver(
        FallbackGeocodingProvider(FailingProvider()),
        cache_path,
    )

    with pytest.raises(GeocodingError, match="Invalid cached geocoding record"):
        await resolver.resolve(LocationSpec("test", "Test"))


async def test_cached_resolver_handles_writes_to_new_directory(tmp_path: Path) -> None:
    class RecordingGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            return ResolvedLocation(
                location.id,
                location.name,
                1.0,
                2.0,
                None,
                None,
                None,
                False,
            )

    cache_dir = tmp_path / "nested" / "path"
    resolver = CachedLocationResolver(RecordingGeocoder(), cache_dir / "geocoding.json")
    result = await resolver.resolve(LocationSpec("test", "Test"))

    assert result.latitude == 1.0
    assert (cache_dir / "geocoding.json").exists()
