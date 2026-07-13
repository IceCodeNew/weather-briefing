from __future__ import annotations

from dataclasses import dataclass, field

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
    name: str
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
class WeatherContextSnapshot:
    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime
    weather_forecast: tuple[str, ...]
    lifestyle_advice: tuple[str, ...] = ()
    air_quality: AirQualitySnapshot | None = None


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


@dataclass(frozen=True, slots=True)
class BriefingResult:
    headline: str
    overview: str
    conclusions: tuple[Conclusion, ...]
    active_warnings: tuple[Warning, ...] = ()
    resolved_warning_ids: tuple[str, ...] = ()
    advice: tuple[Conclusion, ...] = ()
    disaster_tracking: tuple[Conclusion, ...] = ()
    should_publish: bool = True
    raw_payload: dict[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    body: str
    visible_length: int
