"""Platform-neutral domain models and runtime configuration records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import pendulum

from .languages import normalize_language_tag


def normalize_jma_office_code(value: object) -> str | None:
    """Normalize an optional six-digit JMA forecast office code."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("JMA office code must contain six digits (ASCII)")
    normalized = value.strip()
    if len(normalized) != 6 or not normalized.isascii() or not normalized.isdigit():
        raise ValueError("JMA office code must contain six digits (ASCII)")
    return normalized


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """Describe an RSS feed and its location-specific content rules."""

    id: str
    name: str
    url: str
    verbatim_title_patterns: tuple[str, ...] = ()
    forecast_title_patterns: tuple[str, ...] = ()
    content_remove_selectors: tuple[str, ...] = ()
    content_remove_patterns: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocationSpec:
    """Represent the user-provided identity or coordinates of a location."""

    id: str
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    summary_language: str = "en"
    jma_office_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize location-level runtime configuration."""
        object.__setattr__(self, "summary_language", normalize_language_tag(self.summary_language))
        object.__setattr__(self, "jma_office_code", normalize_jma_office_code(self.jma_office_code))


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """Represent a location resolved to stable coordinates and metadata."""

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
    summary_language: str = "en"
    jma_office_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize runtime configuration retained after geocoding."""
        object.__setattr__(self, "summary_language", normalize_language_tag(self.summary_language))
        object.__setattr__(self, "jma_office_code", normalize_jma_office_code(self.jma_office_code))


@dataclass(frozen=True, slots=True)
class LocationResolution:
    """Pair a resolved location with its cache provenance."""

    location: ResolvedLocation
    from_cache: bool


@dataclass(frozen=True, slots=True)
class Article:
    """Represent cleaned source content ready for briefing orchestration."""

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
    """Represent citable non-article context supplied to the LLM."""

    id: str
    name: str
    url: str
    content: str
    language: str = "zh-CN"
    has_allergen_information: bool = False
    history_summary: str | None = None
    history_value: str | None = None

    def __post_init__(self) -> None:
        """Normalize the language before persistence or LLM serialization."""
        object.__setattr__(self, "language", normalize_language_tag(self.language))


@dataclass(frozen=True, slots=True)
class BriefingRecord:
    """Represent a previously published briefing."""

    kind: str
    body: str
    published_at: pendulum.DateTime


class AirQualityTimeKind(StrEnum):
    """Describe whether an air-quality timestamp is observed or forecast."""

    OBSERVATION = "observation"
    FORECAST = "forecast"


@dataclass(frozen=True, slots=True)
class AirQualitySnapshot:
    """Represent provider-neutral air-quality data and guidance."""

    source_id: str
    source_name: str
    source_url: str
    effective_at: pendulum.DateTime | None
    time_kind: AirQualityTimeKind
    aqi: float
    aqi_display: str
    aqi_standard: str
    pm25_aqi: float | None
    pm25_concentration: float | None
    pm25_unit: str | None
    category: str
    health_guidance: str
    output_language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class AllergenLevel:
    """Represent the concentration and category of one allergen."""

    name: str
    category: str
    concentration: float


@dataclass(frozen=True, slots=True)
class AllergenSnapshot:
    """Represent provider-neutral allergen observations and guidance."""

    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime | None
    levels: tuple[AllergenLevel, ...]
    overall_category: str
    health_guidance: str
    output_language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class WeatherContextSnapshot:
    """Collect weather, air-quality, allergen, and lifestyle context."""

    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime
    weather_forecast: tuple[str, ...]
    lifestyle_advice: tuple[str, ...] = ()
    air_quality: AirQualitySnapshot | None = None
    allergen: AllergenSnapshot | None = None
    allergen_advice_available: bool = False
    output_language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class Warning:
    """Represent an active warning and the evidence that confirms it."""

    id: str
    title: str
    status: str
    detail: str
    source_ids: tuple[str, ...]
    last_confirmed_at: pendulum.DateTime


@dataclass(frozen=True, slots=True)
class Conclusion:
    """Represent a sourced conclusion emitted by the LLM."""

    text: str
    source_ids: tuple[str, ...]


class AdviceTopic(StrEnum):
    """Enumerate supported structured lifestyle advice topics."""

    CLOTHING = "clothing"
    DEHUMIDIFICATION = "dehumidification"
    EXERCISE = "exercise"
    MASK = "mask"
    ALLERGEN = "allergen"


@dataclass(frozen=True, slots=True)
class Advice:
    """Represent sourced lifestyle advice for one topic."""

    topic: AdviceTopic
    text: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefingResult:
    """Represent a validated, platform-neutral LLM briefing result."""

    headline: str
    headline_source_ids: tuple[str, ...]
    conclusions: tuple[Conclusion, ...]
    active_warnings: tuple[Warning, ...] = ()
    resolved_warning_ids: tuple[str, ...] = ()
    advice: tuple[Advice, ...] = ()
    disaster_tracking: tuple[Conclusion, ...] = ()
    output_language: str = "en"
    raw_payload: dict[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        """Normalize the language used by delivery renderers."""
        object.__setattr__(self, "output_language", normalize_language_tag(self.output_language))


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """Pair rendered message content with its platform-visible length."""

    body: str
    visible_length: int
    title: str | None = None
