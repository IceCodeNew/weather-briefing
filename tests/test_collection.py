import pendulum

from weather_briefing.application.collection import collect_weather_documents
from weather_briefing.capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from weather_briefing.models import (
    AirQualitySnapshot,
    AirQualityTimeKind,
    ResolvedLocation,
    WeatherContextSnapshot,
)


def _air_quality(
    source: str,
    *,
    effective_at: pendulum.DateTime | None,
    time_kind: AirQualityTimeKind = AirQualityTimeKind.OBSERVATION,
) -> AirQualitySnapshot:
    return AirQualitySnapshot(
        source_id=f"air-quality:{source}",
        source_name=source,
        source_url=f"https://example.invalid/{source}/air-quality",
        effective_at=effective_at,
        time_kind=time_kind,
        aqi=50,
        aqi_display="50",
        aqi_standard="test",
        pm25_aqi=None,
        pm25_concentration=10,
        pm25_unit="μg/m³",
        category="good",
        health_guidance="normal activity",
    )


def _snapshot(
    source: str,
    observed_at: pendulum.DateTime,
    *,
    air_quality_effective_at: pendulum.DateTime | None,
    time_kind: AirQualityTimeKind = AirQualityTimeKind.OBSERVATION,
) -> WeatherContextSnapshot:
    return WeatherContextSnapshot(
        source_id=f"weather:{source}",
        source_name=source,
        source_url=f"https://example.invalid/{source}/weather",
        observed_at=observed_at,
        weather_forecast=("forecast",),
        air_quality=_air_quality(source, effective_at=air_quality_effective_at, time_kind=time_kind),
    )


class _Provider:
    def __init__(self, snapshot: WeatherContextSnapshot) -> None:
        self._snapshot = snapshot

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        return self._snapshot

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        return self._snapshot


def _provider_set(*snapshots: WeatherContextSnapshot) -> CapabilityProviderSet:
    metadata = ProviderCapabilities(
        provider_id="test",
        provider_name="Test",
        capabilities=frozenset({CapabilityName.WEATHER, CapabilityName.AIR_QUALITY}),
    )
    return CapabilityProviderSet(
        weather=_Provider(snapshots[0]),
        weather_metadata=metadata,
        supplements=tuple(_Provider(snapshot) for snapshot in snapshots[1:]),
        supplement_metadata=tuple(metadata for _ in snapshots[1:]),
    )


_LOCATION = ResolvedLocation(
    id="test",
    name="Test",
    latitude=39.9,
    longitude=116.3,
    country_code="CN",
    administrative_area="Beijing",
    timezone="Asia/Shanghai",
    is_mainland_china=True,
)


async def test_collection_drops_air_quality_observations_more_than_two_hours_behind() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot("qweather", latest, air_quality_effective_at=None),
        _snapshot("open-meteo", latest, air_quality_effective_at=latest.subtract(hours=8)),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert {document.id for document in documents} == {
        "weather:qweather",
        "air-quality:qweather",
        "weather:open-meteo",
    }


async def test_collection_keeps_air_quality_observations_at_two_hour_boundary() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot("qweather", latest, air_quality_effective_at=None),
        _snapshot("open-meteo", latest, air_quality_effective_at=latest.subtract(hours=2)),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert {document.id for document in documents if document.id.startswith("air-quality:")} == {
        "air-quality:qweather",
        "air-quality:open-meteo",
    }


async def test_collection_does_not_filter_air_quality_forecasts() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot(
            "qweather",
            latest,
            air_quality_effective_at=latest,
            time_kind=AirQualityTimeKind.FORECAST,
        ),
        _snapshot(
            "open-meteo",
            latest,
            air_quality_effective_at=latest.subtract(hours=8),
            time_kind=AirQualityTimeKind.FORECAST,
        ),
    )

    documents = await collect_weather_documents(provider, _LOCATION, pendulum.date(2026, 7, 28))

    assert {document.id for document in documents if document.id.startswith("air-quality:")} == {
        "air-quality:qweather",
        "air-quality:open-meteo",
    }
