from pathlib import Path

import httpx
import pytest

from weather_briefing.geocoding import (
    CachedLocationResolver,
    FallbackGeocodingProvider,
    GeocodingError,
    NominatimGeocodingProvider,
    OpenMeteoGeocodingProvider,
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
        result = await OpenMeteoGeocodingProvider(client).geocode(
            LocationSpec("beijing", "北京市西城区中南海")
        )

    assert result.latitude == 39.911389
    assert result.country_code == "CN"
    assert result.is_mainland_china is True


async def test_open_meteo_geocoder_selects_the_matching_result() -> None:
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
        result = await OpenMeteoGeocodingProvider(client).geocode(
            LocationSpec("example", "中国北京市西城区中南海")
        )

    assert result.latitude == 39.911389
    assert result.longitude == 116.380556


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
        result = await NominatimGeocodingProvider(
            client, user_agent="weather-briefing-test/1"
        ).geocode(LocationSpec("beijing", "北京市西城区中南海地区"))

    assert result.latitude == 39.911389
    assert result.longitude == 116.380556


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

    first = await resolver.resolve(location)
    second = await resolver.resolve(location)

    assert first == second
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
    resolver = CachedLocationResolver(NeverCalledGeocoder(), cache_path)

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
