from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import pendulum


@dataclass(frozen=True, slots=True)
class FeedConfig:
    id: str
    name: str
    url: str
    verbatim_title_patterns: tuple[str, ...] = ()
    forecast_title_patterns: tuple[str, ...] = ()
    content_remove_selectors: tuple[str, ...] = ()
    content_remove_patterns: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextSourceConfig:
    id: str
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class LocationSpec:
    id: str
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    id: str
    name: str
    latitude: float
    longitude: float
    country_code: str | None
    administrative_area: str | None
    timezone: str | None
    is_mainland_china: bool
    matched_name: str | None = None
    precision_reduced: bool = False


@dataclass(frozen=True, slots=True)
class LocationResolution:
    location: ResolvedLocation
    from_cache: bool


@dataclass(frozen=True, slots=True)
class Article:
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: pendulum.DateTime
    content: str
    is_verbatim: bool = False


@dataclass(frozen=True, slots=True)
class SourceDocument:
    id: str
    name: str
    url: str
    content: str
    has_allergen_information: bool = False


@dataclass(frozen=True, slots=True)
class BriefingRecord:
    kind: str
    body: str
    published_at: pendulum.DateTime


@dataclass(frozen=True, slots=True)
class AirQualitySnapshot:
    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime | None
    aqi: float
    aqi_display: str
    aqi_standard: str
    pm25_aqi: float | None
    pm25_concentration: float | None
    pm25_unit: str | None
    category: str
    health_guidance: str


@dataclass(frozen=True, slots=True)
class AllergenLevel:
    name: str
    category: str
    concentration: float


@dataclass(frozen=True, slots=True)
class AllergenSnapshot:
    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime | None
    levels: tuple[AllergenLevel, ...]
    overall_category: str
    health_guidance: str


@dataclass(frozen=True, slots=True)
class WeatherContextSnapshot:
    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime
    weather_forecast: tuple[str, ...]
    lifestyle_advice: tuple[str, ...] = ()
    air_quality: AirQualitySnapshot | None = None
    allergen: AllergenSnapshot | None = None
    allergen_advice_available: bool = False


@dataclass(frozen=True, slots=True)
class Warning:
    id: str
    title: str
    status: str
    detail: str
    source_ids: tuple[str, ...]
    last_confirmed_at: pendulum.DateTime


@dataclass(frozen=True, slots=True)
class Conclusion:
    text: str
    source_ids: tuple[str, ...]


class AdviceTopic(StrEnum):
    CLOTHING = "clothing"
    DEHUMIDIFICATION = "dehumidification"
    EXERCISE = "exercise"
    MASK = "mask"
    ALLERGEN = "allergen"


@dataclass(frozen=True, slots=True)
class Advice:
    topic: AdviceTopic
    text: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefingResult:
    headline: str
    overview: str
    headline_source_ids: tuple[str, ...]
    overview_source_ids: tuple[str, ...]
    conclusions: tuple[Conclusion, ...]
    active_warnings: tuple[Warning, ...] = ()
    resolved_warning_ids: tuple[str, ...] = ()
    advice: tuple[Advice, ...] = ()
    disaster_tracking: tuple[Conclusion, ...] = ()
    should_publish: bool = True
    raw_payload: dict[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    body: str
    visible_length: int
