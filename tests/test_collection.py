import pendulum
import pytest

from weather_briefing.application.collection import collect_weather_documents
from weather_briefing.capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from weather_briefing.models import (
    AirQualitySnapshot,
    AirQualityTimeKind,
    AllergenLevel,
    AllergenSnapshot,
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


def _snapshot(  # noqa: PLR0913
    source: str,
    observed_at: pendulum.DateTime,
    *,
    air_quality_effective_at: pendulum.DateTime | None,
    time_kind: AirQualityTimeKind = AirQualityTimeKind.OBSERVATION,
    include_air_quality: bool = True,
    include_allergen: bool = False,
    allergen_observed_at: pendulum.DateTime | None = None,
) -> WeatherContextSnapshot:
    return WeatherContextSnapshot(
        source_id=f"weather:{source}",
        source_name=source,
        source_url=f"https://example.invalid/{source}/weather",
        observed_at=observed_at,
        weather_forecast=("forecast",),
        air_quality=(
            _air_quality(source, effective_at=air_quality_effective_at, time_kind=time_kind)
            if include_air_quality
            else None
        ),
        allergen=(
            AllergenSnapshot(
                source_id=f"allergen:{source}",
                source_name=source,
                source_url=f"https://example.invalid/{source}/allergen",
                observed_at=allergen_observed_at,
                levels=(AllergenLevel("pollen", "low", 1.0),),
                overall_category="low",
                health_guidance="normal activity",
            )
            if include_allergen
            else None
        ),
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


async def test_collection_drops_only_current_documents_that_are_stale() -> None:
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


async def test_collection_drops_stale_weather_snapshot_without_air_quality() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot("qweather", latest, air_quality_effective_at=None),
        _snapshot(
            "open-meteo",
            latest.subtract(hours=8),
            air_quality_effective_at=None,
            include_air_quality=False,
        ),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert {document.id for document in documents} == {
        "weather:qweather",
        "air-quality:qweather",
    }


async def test_collection_keeps_current_documents_at_two_hour_boundary() -> None:
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


async def test_collection_filters_allergen_by_its_own_observation_time() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot("qweather", latest, air_quality_effective_at=None),
        _snapshot(
            "open-meteo",
            latest,
            air_quality_effective_at=latest,
            include_allergen=True,
            allergen_observed_at=latest.subtract(hours=8),
        ),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert "allergen:open-meteo" not in {document.id for document in documents}
    assert "weather:open-meteo" in {document.id for document in documents}


async def test_collection_uses_weather_time_when_allergen_has_no_observation_time() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot(
            "qweather",
            latest,
            air_quality_effective_at=None,
            include_allergen=True,
        ),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert "allergen:qweather" in {document.id for document in documents}


async def test_collection_keeps_forecast_document_while_filtering_current_data() -> None:
    latest = pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai")
    provider = _provider_set(
        _snapshot(
            "qweather",
            latest,
            air_quality_effective_at=latest.subtract(hours=8),
            time_kind=AirQualityTimeKind.FORECAST,
        ),
        _snapshot(
            "open-meteo",
            latest.subtract(hours=8),
            air_quality_effective_at=None,
            include_air_quality=False,
        ),
    )

    documents = await collect_weather_documents(provider, _LOCATION, None)

    assert "air-quality:qweather" in {document.id for document in documents}
    assert "weather:open-meteo" not in {document.id for document in documents}


async def test_collection_does_not_filter_dated_forecast_snapshots() -> None:
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


@pytest.mark.parametrize(
    ("snapshot", "expected_context"),
    [
        (
            _snapshot(
                "qweather",
                pendulum.datetime(2026, 7, 27, 21, tz=None),
                air_quality_effective_at=None,
            ),
            "Weather snapshot weather:qweather observation time",
        ),
        (
            _snapshot(
                "qweather",
                pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai"),
                air_quality_effective_at=pendulum.datetime(2026, 7, 27, 21, tz=None),
            ),
            "Air-quality snapshot air-quality:qweather observation time",
        ),
        (
            _snapshot(
                "qweather",
                pendulum.datetime(2026, 7, 27, 21, tz="Asia/Shanghai"),
                air_quality_effective_at=None,
                include_allergen=True,
                allergen_observed_at=pendulum.datetime(2026, 7, 27, 21, tz=None),
            ),
            "Allergen snapshot allergen:qweather observation time",
        ),
    ],
)
async def test_collection_rejects_ambiguous_current_observation_times(
    snapshot: WeatherContextSnapshot,
    expected_context: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"^{expected_context} must include explicit timezone information$",
    ):
        await collect_weather_documents(_provider_set(snapshot), _LOCATION, None)
