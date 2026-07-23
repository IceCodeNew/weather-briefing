import pendulum
import pytest

from weather_briefing.air_quality import AirQualityError
from weather_briefing.capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from weather_briefing.models import AirQualitySnapshot, AirQualityTimeKind, WeatherContextSnapshot
from weather_briefing.weather import WeatherContextError


def _weather(*, air_quality: AirQualitySnapshot | None = None) -> WeatherContextSnapshot:
    return WeatherContextSnapshot(
        source_id="weather:test",
        source_name="Test weather",
        source_url="https://example.invalid/weather",
        observed_at=pendulum.datetime(2026, 7, 20, 8, tz="Asia/Singapore"),
        weather_forecast=("forecast",),
        air_quality=air_quality,
    )


def _air_quality() -> AirQualitySnapshot:
    return AirQualitySnapshot(
        source_id="air-quality:test",
        source_name="Test air",
        source_url="https://example.invalid/air",
        effective_at=pendulum.datetime(2026, 7, 20, 8, tz="Asia/Singapore"),
        time_kind=AirQualityTimeKind.OBSERVATION,
        aqi=20,
        aqi_display="20",
        aqi_standard="Test",
        pm25_aqi=None,
        pm25_concentration=None,
        pm25_unit=None,
        category="good",
        health_guidance="ok",
    )


def _metadata() -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="test",
        provider_name="Test",
        capabilities=frozenset({CapabilityName.WEATHER}),
    )


def test_provider_capability_metadata_reports_support() -> None:
    metadata = _metadata()

    assert metadata.supports(CapabilityName.WEATHER)
    assert not metadata.supports(CapabilityName.ALERTS)


async def test_capability_set_supplements_missing_current_air_quality() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()

    class Air:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            assert timezone == "Asia/Singapore"
            return _air_quality()

    provider = CapabilityProviderSet(
        weather=Weather(),
        weather_metadata=_metadata(),
        air_quality=Air(),
    )

    snapshot = await provider.fetch(1, 2)

    assert snapshot.air_quality == _air_quality()


async def test_capability_set_does_not_supplement_dated_context() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            raise AssertionError("dated fetch must use fetch_for_date")  # pragma: no cover

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            assert forecast_date == pendulum.date(2026, 7, 21)
            return _weather()

    class FailingAir:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            raise AssertionError("dated contexts must not use current air quality")  # pragma: no cover

    provider = CapabilityProviderSet(
        weather=Weather(),
        weather_metadata=_metadata(),
        air_quality=FailingAir(),
    )

    snapshot = await provider.fetch_for_date(1, 2, pendulum.date(2026, 7, 21))

    assert snapshot.air_quality is None


async def test_capability_set_requires_an_air_quality_capability_for_current_context() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()

    provider = CapabilityProviderSet(weather=Weather(), weather_metadata=_metadata())

    with pytest.raises(WeatherContextError, match="configure AQICN_API_TOKEN"):
        await provider.fetch(1, 2)


async def test_capability_set_wraps_air_quality_provider_failure() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()

    class Air:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            raise AirQualityError("failed")

    provider = CapabilityProviderSet(weather=Weather(), weather_metadata=_metadata(), air_quality=Air())

    with pytest.raises(WeatherContextError, match="AQICN fallback failed"):
        await provider.fetch(1, 2)


async def test_dated_context_requires_provider_support() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()  # pragma: no cover

    provider = CapabilityProviderSet(weather=Weather(), weather_metadata=_metadata())

    with pytest.raises(WeatherContextError, match="does not support target forecast dates"):
        await provider.fetch_for_date(1, 2, pendulum.date(2026, 7, 21))


async def test_dated_context_rejects_non_callable_fetch_method() -> None:
    class Weather:
        fetch_for_date = 1

        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()  # pragma: no cover

    provider = CapabilityProviderSet(weather=Weather(), weather_metadata=_metadata())

    with pytest.raises(WeatherContextError, match="does not support target forecast dates"):
        await provider.fetch_for_date(1, 2, pendulum.date(2026, 7, 21))


@pytest.mark.anyio
async def test_capability_set_includes_best_effort_supplementary_context() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()

    class Supplement:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()

    class Air:
        async def fetch(self, latitude: float, longitude: float, timezone: str) -> AirQualitySnapshot:
            return _air_quality()

    provider = CapabilityProviderSet(
        weather=Weather(),
        weather_metadata=_metadata(),
        air_quality=Air(),
        supplements=(Supplement(),),
    )

    assert len(await provider.fetch_all(1, 2)) == 2


async def test_capability_set_includes_dated_capable_supplement() -> None:
    forecast_date = pendulum.date(2026, 7, 21)

    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather()  # pragma: no cover

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            requested_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            assert requested_date == forecast_date
            return _weather()

    provider = CapabilityProviderSet(
        weather=Weather(),
        weather_metadata=_metadata(),
        supplements=(Weather(),),
    )

    assert len(await provider.fetch_all(1, 2, forecast_date=forecast_date)) == 2


async def test_capability_set_skips_failed_and_non_dated_supplements() -> None:
    class Weather:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            return _weather(air_quality=_air_quality())

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            return _weather()

    class Supplement:
        async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
            raise WeatherContextError("optional")

    provider = CapabilityProviderSet(
        weather=Weather(),
        weather_metadata=_metadata(),
        supplements=(Supplement(),),
    )

    assert len(await provider.fetch_all(1, 2)) == 1
    assert len(await provider.fetch_all(1, 2, forecast_date=pendulum.date(2026, 7, 21))) == 1
