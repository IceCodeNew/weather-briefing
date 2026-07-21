import logging
import traceback
from dataclasses import replace
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
    _location_name_matches,
    _mainland_china_rules,
    _specific_location_name,
    possibly_mainland_china,
)
from weather_briefing.models import LocationSpec, ResolvedLocation
from weather_briefing.reference_data import ReferenceDataError, reference_value


class _NeverCalledGeocoder:
    def __init__(self) -> None:
        self.calls = 0

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        self.calls += 1
        raise AssertionError("Unexpected forward geocoding")


class _NeverCalledReverseGeocoder:
    def __init__(self) -> None:
        self.calls = 0

    async def reverse_geocode(self, location: LocationSpec) -> ResolvedLocation:
        self.calls += 1
        raise AssertionError("Unexpected reverse geocoding")


async def test_never_called_geocoders_fail_when_invoked() -> None:
    location = LocationSpec("example", "Example")
    forward_geocoder = _NeverCalledGeocoder()
    reverse_geocoder = _NeverCalledReverseGeocoder()

    with pytest.raises(AssertionError, match="forward geocoding"):
        await forward_geocoder.geocode(location)
    with pytest.raises(AssertionError, match="reverse geocoding"):
        await reverse_geocoder.reverse_geocode(location)

    assert forward_geocoder.calls == 1
    assert reverse_geocoder.calls == 1


def _required_test_location_name(location: LocationSpec) -> str:
    assert location.name is not None
    return location.name


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


async def test_nominatim_accepts_comma_qualified_location_with_intermediate_administrative_areas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Example Plaza, Exampleland"
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "1.0",
                    "lon": "2.0",
                    "display_name": "Example Plaza, Central District, Exampleland",
                    "address": {"country_code": "ex"},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1").geocode(
            LocationSpec("example", "Example Plaza, Exampleland")
        )

    assert result.latitude == 1.0
    assert result.country_code == "EX"


async def test_nominatim_rejects_comma_qualified_location_without_its_country_constraint() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[
                    {
                        "lat": "1.0",
                        "lon": "2.0",
                        "display_name": "Example Plaza, Central District, Differentland",
                        "address": {"country_code": "dl"},
                    }
                ],
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="returned no result"):
            await NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1").geocode(
                LocationSpec("example", "Example Plaza, Exampleland")
            )


async def test_nominatim_reverse_geocoder_resolves_name_and_administrative_area() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reverse"
        assert request.url.params["lat"] == "39.911389"
        assert request.url.params["lon"] == "116.380556"
        assert request.url.params["format"] == "jsonv2"
        assert request.url.params["addressdetails"] == "1"
        assert request.extensions["weather_briefing.api_call"] == ("nominatim", "reverse-geocoding")
        return httpx.Response(
            200,
            json={
                "display_name": "中南海, 西城区, 北京市, 中国",
                "address": {"country_code": "cn", "state": "北京市"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1").reverse_geocode(
            LocationSpec("beijing", latitude=39.911389, longitude=116.380556)
        )

    assert result.name == "中南海, 西城区, 北京市, 中国"
    assert result.latitude == 39.911389
    assert result.longitude == 116.380556
    assert result.country_code == "CN"
    assert result.administrative_area == "北京市"
    assert result.is_mainland_china is True


async def test_nominatim_reverse_geocoder_requires_coordinates() -> None:
    async with httpx.AsyncClient() as client:
        provider = NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1")
        with pytest.raises(GeocodingError, match="requires coordinates"):
            await provider.reverse_geocode(LocationSpec("beijing", latitude=39.911389))


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"display_name": "Example", "address": []},
        {"display_name": "", "address": {}},
    ),
)
async def test_nominatim_reverse_geocoder_rejects_invalid_response(payload: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        provider = NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1")
        with pytest.raises(GeocodingError, match="reverse geocoding failed"):
            await provider.reverse_geocode(LocationSpec("example", latitude=1.0, longitude=2.0))


async def test_nominatim_reverse_geocoder_handles_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        provider = NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1")
        with pytest.raises(GeocodingError, match="reverse geocoding failed"):
            await provider.reverse_geocode(LocationSpec("example", latitude=1.0, longitude=2.0))


async def test_nominatim_reverse_geocoder_rate_limits_consecutive_requests(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monotonic_values = iter((100.0, 100.0, 100.25, 100.25))

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("weather_briefing.geocoding.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "weather_briefing.geocoding.time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"display_name": "Example", "address": {}}))
    ) as client:
        provider = NominatimGeocodingProvider(client, user_agent="weather-briefing-test/1")
        location = LocationSpec("example", latitude=1.0, longitude=2.0)
        await provider.reverse_geocode(location)
        await provider.reverse_geocode(location)

    assert sleep_calls == [pytest.approx(0.75)]


async def test_geocoder_reduces_precision_after_full_address_fails() -> None:
    queries: list[str] = []

    class RoadLevelGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            location_name = _required_test_location_name(location)
            queries.append(location_name)
            if location_name.endswith("1号"):
                raise GeocodingError("no building match")
            return ResolvedLocation(
                location.id,
                location_name,
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
            location_name = _required_test_location_name(location)
            return ResolvedLocation(
                location.id,
                location_name,
                39.911389,
                116.380556,
                "CN",
                "北京市",
                "Asia/Shanghai",
                True,
            )

    geocoder = RecordingGeocoder()
    cache_path = tmp_path / "geocoding.json"
    resolver = CachedLocationResolver(geocoder, cache_path)
    location = LocationSpec(
        "beijing",
        "北京市西城区中南海",
        summary_language="zh-CN",
        jma_office_code="130000",
    )

    first = await resolver.resolve_with_metadata(location)
    second = await resolver.resolve_with_metadata(
        LocationSpec("beijing", "北京市西城区中南海", summary_language="ja", jma_office_code="270000")
    )

    assert first.location.summary_language == "zh-CN"
    assert first.location.jma_office_code == "130000"
    assert second.location.summary_language == "ja"
    assert second.location.jma_office_code == "270000"
    assert first.location == replace(second.location, summary_language="zh-CN", jma_office_code="130000")
    assert first.from_cache is False
    assert second.from_cache is True
    assert geocoder.calls == 1
    cache_text = cache_path.read_text(encoding="utf-8")
    assert "summary_language" not in cache_text
    assert "jma_office_code" not in cache_text


@pytest.mark.parametrize("name", (None, "  "))
async def test_resolver_reverse_geocodes_coordinate_only_location_and_caches_result(
    tmp_path: Path,
    name: str | None,
) -> None:
    class RecordingReverseGeocoder:
        calls = 0

        async def reverse_geocode(self, location: LocationSpec) -> ResolvedLocation:
            self.calls += 1
            assert location.latitude is not None
            assert location.longitude is not None
            return ResolvedLocation(
                location.id,
                "中南海, 西城区, 北京市, 中国",
                location.latitude,
                location.longitude,
                "CN",
                "北京市",
                None,
                True,
                matched_name="中南海, 西城区, 北京市, 中国",
            )

    reverse_geocoder = RecordingReverseGeocoder()
    forward_geocoder = _NeverCalledGeocoder()
    cache_path = tmp_path / "geocoding.json"
    resolver = CachedLocationResolver(
        forward_geocoder,
        cache_path,
        reverse_provider=reverse_geocoder,
    )
    location = LocationSpec(
        "beijing",
        name,
        latitude=39.911389,
        longitude=116.380556,
        summary_language="zh-CN",
        jma_office_code="130000",
    )

    first = await resolver.resolve_with_metadata(location)
    second = await resolver.resolve_with_metadata(replace(location, summary_language="ja", jma_office_code="270000"))

    assert first.location.name == "中南海, 西城区, 北京市, 中国"
    assert first.location.summary_language == "zh-CN"
    assert first.from_cache is False
    assert first.location.jma_office_code == "130000"
    assert second.location == replace(first.location, summary_language="ja", jma_office_code="270000")
    assert second.from_cache is True
    assert reverse_geocoder.calls == 1
    cache_text = cache_path.read_text(encoding="utf-8")
    assert '"beijing:coords:39.9113890,116.3805560"' in cache_text
    assert "summary_language" not in cache_text
    assert "jma_office_code" not in cache_text
    assert forward_geocoder.calls == 0


async def test_resolver_requires_reverse_provider_for_coordinate_only_location(tmp_path: Path) -> None:
    forward_geocoder = _NeverCalledGeocoder()
    resolver = CachedLocationResolver(forward_geocoder, tmp_path / "geocoding.json")

    with pytest.raises(GeocodingError, match="No reverse geocoder configured"):
        await resolver.resolve(LocationSpec("example", latitude=1.0, longitude=2.0))
    assert forward_geocoder.calls == 0


@pytest.mark.parametrize("name", (None, "  "))
async def test_forward_geocoder_requires_location_name(name: str | None) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(GeocodingError, match="Forward geocoding requires a name"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("missing", name))


async def test_forward_geocoder_strips_programmatic_location_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "Example"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Example",
                        "latitude": 1.0,
                        "longitude": 2.0,
                        "country_code": "XX",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("example", "  Example  "))

    assert result.name == "Example"


@pytest.mark.parametrize(
    "cached",
    (
        '"invalid"',
        '{"id":"example","name":"Example"}',
        '{"id":"example","name":"Example","latitude":1,"longitude":2,'
        '"country_code":null,"administrative_area":null,"timezone":null,'
        '"is_mainland_china":false,"summary_language":"english"}',
    ),
)
async def test_resolver_rejects_invalid_cached_reverse_record(tmp_path: Path, cached: str) -> None:
    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text(f'{{"example:coords:1.0000000,2.0000000":{cached}}}', encoding="utf-8")
    forward_geocoder = _NeverCalledGeocoder()
    reverse_geocoder = _NeverCalledReverseGeocoder()
    resolver = CachedLocationResolver(
        forward_geocoder,
        cache_path,
        reverse_provider=reverse_geocoder,
    )

    with pytest.raises(GeocodingError, match="Invalid cached reverse geocoding record"):
        await resolver.resolve(LocationSpec("example", latitude=1.0, longitude=2.0))
    assert forward_geocoder.calls == 0
    assert reverse_geocoder.calls == 0


@pytest.mark.parametrize(
    "cached",
    (
        '{"id":"example","name":"Example"}',
        '{"id":"example","name":"Example","latitude":1,"longitude":2,'
        '"country_code":null,"administrative_area":null,"timezone":null,'
        '"is_mainland_china":false,"summary_language":"english"}',
    ),
)
async def test_resolver_rejects_obsolete_cache_record(tmp_path: Path, cached: str) -> None:
    class NeverCalledGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise AssertionError("Invalid cache data must not trigger geocoding")

    cache_path = tmp_path / "geocoding.json"
    cache_path.write_text(
        f'{{"example:Example":{cached}}}',
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

    beijing = await resolver.resolve(LocationSpec("beijing", "  Beijing  ", 39.9, 116.4))
    outside = await resolver.resolve(LocationSpec("outside", "Tokyo", 1.0, 1.0, jma_office_code="130000"))
    latitude = reference_value("geography.json", "mainland_china_service_bounds", "latitude")
    longitude = reference_value("geography.json", "mainland_china_service_bounds", "longitude")

    assert beijing.is_mainland_china is True
    assert beijing.name == "Beijing"
    assert outside.is_mainland_china is False
    assert outside.jma_office_code == "130000"
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


async def test_fallback_geocoding_preserves_only_safe_cause_type() -> None:
    class FailingProvider:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("failure for Private Home Address", cause_type=httpx.ConnectError)

    with pytest.raises(GeocodingError) as caught:
        await FallbackGeocodingProvider(FailingProvider()).geocode(LocationSpec("home", "Private Home Address"))

    assert str(caught.value) == "No geocoder could resolve location: home (ConnectError)"
    assert caught.value.cause_type is httpx.ConnectError


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


async def test_open_meteo_handles_empty_results(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.geocoding")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))
    ) as client:
        with pytest.raises(GeocodingError, match="returned no results"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test"))

    assert "candidate_count=0 outcome=no-results" in caplog.text


async def test_open_meteo_handles_results_not_a_list() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": "not-a-list"}))
    ) as client:
        with pytest.raises(GeocodingError, match="returned an invalid response"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test"))


async def test_open_meteo_ignores_null_fields_when_matching_results() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": None,
                            "latitude": 1.0,
                            "longitude": 2.0,
                            "country_code": "EX",
                        }
                    ]
                },
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="returned no matching result"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "None"))


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
        with pytest.raises(GeocodingError, match="returned no matching result"):
            await OpenMeteoGeocodingProvider(client).geocode(LocationSpec("test", "Test City"))


async def test_open_meteo_handles_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(GeocodingError, match="Open-Meteo geocoding failed"):
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
        with pytest.raises(GeocodingError, match="returned no result"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "Test"))


async def test_nominatim_handles_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(GeocodingError, match="geocoding request failed"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("test", "Test"))


async def test_forward_geocoding_failure_traceback_omits_private_location_name() -> None:
    private_name = "Private Home Address"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed for {private_name}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GeocodingError) as caught:
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("home", private_name))

    rendered_traceback = "".join(traceback.format_exception(caught.value))
    assert private_name not in rendered_traceback
    assert "location: home" in rendered_traceback
    assert "ConnectError" in rendered_traceback


async def test_nominatim_logs_rejected_candidate_count_without_location_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_name = "Private Plaza"
    provider_candidate = "Unrelated Provider Candidate"
    caplog.set_level(logging.INFO, logger="weather_briefing.geocoding")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[
                    {
                        "lat": "1",
                        "lon": "2",
                        "display_name": provider_candidate,
                        "address": {},
                    }
                ],
            )
        )
    ) as client:
        with pytest.raises(GeocodingError, match="returned no result"):
            await NominatimGeocodingProvider(client, user_agent="test").geocode(LocationSpec("example", private_name))

    assert "provider=nominatim location_id=example query_attempt=1 candidate_count=1 outcome=no-match" in caplog.text
    assert private_name not in caplog.text
    assert provider_candidate not in caplog.text


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
            calls.append(_required_test_location_name(location))
            raise GeocodingError("no match")

    with pytest.raises(GeocodingError, match="No geocoder could resolve location at a safe precision"):
        await PrecisionReducingGeocodingProvider(FailingGeocoder()).geocode(
            LocationSpec("test", "中国北京市西城区中南海1号")
        )

    assert calls == ["中国北京市西城区中南海1号", "中国北京市西城区中南海"]


async def test_precision_reducing_provider_keeps_international_trailing_digits() -> None:
    calls: list[str] = []

    class FailingGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            calls.append(_required_test_location_name(location))
            raise GeocodingError("no match")

    with pytest.raises(GeocodingError, match="No geocoder could resolve location at a safe precision"):
        await PrecisionReducingGeocodingProvider(FailingGeocoder()).geocode(
            LocationSpec("example", "Example Street 123")
        )

    assert calls == ["Example Street 123"]


async def test_precision_reducing_provider_preserves_only_safe_cause_type() -> None:
    class FailingGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            raise GeocodingError("failure for Private Home Address", cause_type=httpx.ConnectError)

    with pytest.raises(GeocodingError) as caught:
        await PrecisionReducingGeocodingProvider(FailingGeocoder()).geocode(
            LocationSpec("home", "Private Home Address")
        )

    assert str(caught.value) == "No geocoder could resolve location at a safe precision: home (ConnectError)"
    assert caught.value.cause_type is httpx.ConnectError


async def test_precision_reducing_provider_continues_after_geocoding_error() -> None:
    calls: list[str] = []

    class PartialGeocoder:
        async def geocode(self, location: LocationSpec) -> ResolvedLocation:
            location_name = _required_test_location_name(location)
            calls.append(location_name)
            if "1号" in location_name:
                raise GeocodingError("no building match")
            return ResolvedLocation(
                location.id,
                location_name,
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
            location_name = _required_test_location_name(location)
            return ResolvedLocation(
                location.id,
                location_name,
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


def test_mainland_china_rules_rejects_invalid_latitude_bounds(monkeypatch) -> None:
    _mainland_china_rules.cache_clear()

    def fake_value(filename: str, *path: str) -> object:
        if "latitude" in path:
            return {"minimum": "100", "maximum": "50"}
        if "longitude" in path:
            return {"minimum": "73", "maximum": "136"}
        raise AssertionError(f"Unexpected call: {filename} {path}")

    monkeypatch.setattr("weather_briefing.geocoding.reference_value", fake_value)
    monkeypatch.setattr(
        "weather_briefing.geocoding.reference_string_tuple",
        lambda *_: (),
    )
    with pytest.raises(ReferenceDataError, match="latitude"):
        _mainland_china_rules()


def test_mainland_china_rules_rejects_invalid_longitude_bounds(monkeypatch) -> None:
    _mainland_china_rules.cache_clear()

    def fake_value(filename: str, *path: str) -> object:
        if "latitude" in path:
            return {"minimum": "18", "maximum": "54"}
        if "longitude" in path:
            return {"minimum": "200", "maximum": "250"}
        raise AssertionError(f"Unexpected call: {filename} {path}")

    monkeypatch.setattr("weather_briefing.geocoding.reference_value", fake_value)
    monkeypatch.setattr(
        "weather_briefing.geocoding.reference_string_tuple",
        lambda *_: (),
    )
    with pytest.raises(ReferenceDataError, match="longitude"):
        _mainland_china_rules()


def test_mainland_china_rules_handles_corrupt_reference_data(monkeypatch) -> None:
    _mainland_china_rules.cache_clear()
    monkeypatch.setattr(
        "weather_briefing.geocoding.reference_value",
        lambda *_: (_ for _ in ()).throw(KeyError("missing")),
    )
    with pytest.raises(ReferenceDataError, match="mainland China geography"):
        _mainland_china_rules()


def test_specific_location_name_rejects_non_string_suffix(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.geocoding.reference_value",
        lambda *_: 123,
    )
    with pytest.raises(ReferenceDataError, match="suffix characters must be a string"):
        _specific_location_name("北京")


def test_location_name_matching_advances_by_normalized_component_length() -> None:
    assert not _location_name_matches("Straße, e", "Straße")


def test_location_name_matching_rejects_qualifier_substrings() -> None:
    assert not _location_name_matches("Example, Iran", "Example, Tirana")
    assert _location_name_matches("Example, Iran", "Example, Central District, Iran")
