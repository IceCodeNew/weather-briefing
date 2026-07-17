"""Runtime configuration parsing and validation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, overload

import pendulum
from any_llm import AnyLLM
from apscheduler.triggers.cron import CronTrigger
from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector

from .models import ContextSourceConfig, FeedConfig, LocationSpec, ResolvedLocation
from .reference_data import reference_string_tuple


class ConfigurationError(ValueError):
    """Raised when private runtime configuration is missing or malformed."""


@overload
def _clean_env(value: str) -> str: ...


@overload
def _clean_env(value: None) -> None: ...


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _first_configured(*names: str) -> str | None:
    for name in names:
        if value := _clean_env(os.getenv(name, "")):
            return value
    return None


def _integer(name: str, default: int) -> int:
    try:
        return int(_clean_env(os.getenv(name, str(default))))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _positive_integer(name: str, default: int) -> int:
    value = _integer(name, default)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _bounded_positive_integer(name: str, default: int, maximum: int) -> int:
    value = _positive_integer(name, default)
    if value > maximum:
        raise ConfigurationError(f"{name} cannot exceed {maximum}")
    return value


def _bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _integer(name, default)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _cron_hour(name: str, default: str) -> str:
    value = _clean_env(os.getenv(name, default))
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    try:
        CronTrigger(hour=value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid APScheduler hour expression") from exc
    return value


def _float(name: str, default: float) -> float:
    try:
        return float(_clean_env(os.getenv(name, str(default))))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _positive_float(name: str, default: float) -> float:
    value = _float(name, default)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = _clean_env(os.getenv(name, str(default))).strip().casefold()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{name} must be one of: true, false, 1, 0, yes, no")


def _json_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigurationError(f"{path} must be a JSON array of objects")
    return value


def _optional_string_array(
    item: dict[str, Any],
    source_id: str,
    field: str,
    *,
    validator: Callable[[str], object] | None = None,
) -> tuple[str, ...]:
    value = item.get(field)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError(f"RSS source {source_id} field {field} must be a JSON array")
    entries: list[str] = []
    for index, entry in enumerate(value):
        path = f"RSS source {source_id} field {field}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigurationError(f"{path} must be a non-empty string")
        if validator is not None:
            try:
                validator(entry)
            except (re.error, SelectorSyntaxError) as exc:
                raise ConfigurationError(f"{path} is invalid") from exc
        entries.append(entry)
    return tuple(entries)


def _context_source(item: object, index: int) -> ContextSourceConfig:
    if not isinstance(item, dict):
        raise ConfigurationError(f"CONTEXT_SOURCES_JSON[{index}] must be a JSON object")

    values: dict[str, str] = {}
    for field in ("id", "name", "url"):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"CONTEXT_SOURCES_JSON[{index}].{field} must be a non-empty string")
        values[field] = value.strip()
    return ContextSourceConfig(id=values["id"], name=values["name"], url=values["url"])


def _configured_weather_providers() -> tuple[str, ...] | None:
    configured = _clean_env(os.getenv("WEATHER_PROVIDERS"))
    if configured is None:
        return None
    providers = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not providers:
        raise ConfigurationError("WEATHER_PROVIDERS cannot be empty")
    return providers


def state_path_from_env() -> Path:
    """Return the configured SQLite state path without loading all settings."""
    return Path(_clean_env(os.getenv("BRIEFING_STATE_PATH", "state/weather.sqlite3")))


def weather_providers_for(location: ResolvedLocation, configured: tuple[str, ...] | None) -> tuple[str, ...]:
    """Resolve the configured or region-default weather provider order."""
    if configured is not None:
        return configured
    region = "mainland_china" if location.is_mainland_china else "other"
    return reference_string_tuple("provider_defaults.json", "weather_provider_order", region)


def _locations(path: Path) -> tuple[LocationSpec, ...]:
    items = _json_file(path)
    if not items:
        raise ConfigurationError(f"Configure at least one location in {path}")
    locations: list[LocationSpec] = []
    seen_ids: set[str] = set()
    for item in items:
        location_id = str(item.get("id", "")).strip()
        name_value = item.get("name")
        name = str(name_value).strip() if name_value is not None else None
        name = name or None
        if not location_id or not location_id.replace("-", "").replace("_", "").isalnum():
            raise ConfigurationError("Location id must use letters, numbers, '-' or '_'")
        if location_id in seen_ids:
            raise ConfigurationError(f"Duplicate location id: {location_id}")
        latitude_value = item.get("latitude")
        longitude_value = item.get("longitude")
        if (latitude_value is None) != (longitude_value is None):
            raise ConfigurationError(f"Location {location_id} must provide both latitude and longitude or neither")
        try:
            latitude = float(latitude_value) if latitude_value is not None else None
            longitude = float(longitude_value) if longitude_value is not None else None
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Location {location_id} coordinates must be numbers") from exc
        if latitude is not None and not -90 <= latitude <= 90:
            raise ConfigurationError(f"Location {location_id} latitude is out of range")
        if longitude is not None and not -180 <= longitude <= 180:
            raise ConfigurationError(f"Location {location_id} longitude is out of range")
        if name is None and latitude is None:
            raise ConfigurationError(f"Location {location_id} must provide a name or coordinates")
        locations.append(LocationSpec(location_id, name, latitude, longitude))
        seen_ids.add(location_id)
    return tuple(locations)


def _feeds(path: Path) -> tuple[FeedConfig, ...]:
    feeds: list[FeedConfig] = []
    for item in _json_file(path):
        source_id = str(item.get("id") or "").strip()
        source_name = str(item.get("name") or "").strip()
        source_url = str(item.get("url") or "").strip()
        if not source_id:
            raise ConfigurationError("RSS source must have an id")
        if not source_name:
            raise ConfigurationError(f"RSS source {source_id} must have a public display name")
        if not source_url:
            raise ConfigurationError(f"RSS source {source_id} must have a URL")
        feeds.append(
            FeedConfig(
                id=source_id,
                name=source_name,
                url=source_url,
                verbatim_title_patterns=_optional_string_array(item, source_id, "verbatim_title_patterns"),
                forecast_title_patterns=_optional_string_array(item, source_id, "forecast_title_patterns"),
                content_remove_selectors=_optional_string_array(
                    item,
                    source_id,
                    "content_remove_selectors",
                    validator=compile_selector,
                ),
                content_remove_patterns=_optional_string_array(
                    item,
                    source_id,
                    "content_remove_patterns",
                    validator=re.compile,
                ),
                location_ids=_optional_string_array(item, source_id, "location_ids"),
            )
        )
    return tuple(feeds)


@dataclass(frozen=True, slots=True)
class Settings:
    """Collect validated runtime settings used to compose the application."""

    api_key: str | None
    llm_provider: str
    llm_model: str
    llm_base_url: str | None
    llm_max_output_tokens: int
    llm_max_attempts: int
    http_timeout_seconds: float
    timezone: pendulum.Timezone
    locations_path: Path
    locations: tuple[LocationSpec, ...]
    geocoding_base_url: str
    geocoding_api_key: str | None
    nominatim_base_url: str
    geocoding_user_agent: str
    geocoding_cache_path: Path
    rss_sources_path: Path
    feeds: tuple[FeedConfig, ...]
    context_sources: tuple[ContextSourceConfig, ...]
    weather_providers: tuple[str, ...] | None
    qweather_project_id: str | None
    qweather_credential_id: str | None
    qweather_private_key: str | None
    qweather_jwt_lifetime_seconds: int
    qweather_base_url: str | None
    qweather_index_types: tuple[str, ...]
    open_meteo_weather_base_url: str
    open_meteo_air_quality_base_url: str
    open_meteo_api_key: str | None
    aqicn_api_token: str | None
    aqicn_base_url: str
    state_path: Path
    publisher: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    rss_max_attempts: int
    rss_retry_min_seconds: float
    rss_retry_max_seconds: float
    rss_stale_hours: int
    rss_failure_threshold: int
    warning_retention_hours: int
    history_hours: int
    llm_history_max_documents: int
    llm_history_max_characters: int
    briefing_max_characters: int
    greeting_hour: int
    greeting_minute: int
    hourly_cron: str
    debug: bool

    @classmethod
    def from_env(cls) -> Settings:
        """Load and validate application settings from the environment."""
        locations_path = Path(_clean_env(os.getenv("BRIEFING_LOCATIONS_FILE", "locations.json")))
        rss_sources_path = Path(_clean_env(os.getenv("RSS_SOURCES_FILE", "rss-sources.json")))
        feeds = _feeds(rss_sources_path)
        context_raw = _clean_env(os.getenv("CONTEXT_SOURCES_JSON", "[]"))
        try:
            context_items = json.loads(context_raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("CONTEXT_SOURCES_JSON must contain valid JSON") from exc
        if not isinstance(context_items, list):
            raise ConfigurationError("CONTEXT_SOURCES_JSON must be a JSON array of objects")
        context_sources = tuple(_context_source(item, index) for index, item in enumerate(context_items))
        try:
            timezone = pendulum.timezone(_clean_env(os.getenv("BRIEFING_TIMEZONE", "Asia/Shanghai")))
        except (ValueError, KeyError) as exc:
            raise ConfigurationError("Invalid timezone") from exc
        retry_min = _float("RSS_RETRY_MIN_SECONDS", 3)
        retry_max = _float("RSS_RETRY_MAX_SECONDS", 5)
        if retry_min < 0 or retry_max < retry_min:
            raise ConfigurationError("RSS retry delay range is invalid")
        briefing_max_characters = _positive_integer("BRIEFING_MAX_CHARACTERS", 3500)
        daily_cron_hour = _bounded_integer("GREETING_HOUR", 8, 0, 23)
        daily_cron_minute = _bounded_integer("GREETING_MINUTE", 0, 0, 59)
        hourly_cron = _cron_hour("BRIEFING_CRON", "9-23")
        llm_provider = _clean_env(os.getenv("LLM_PROVIDER", "deepseek"))
        if llm_provider not in AnyLLM.get_supported_providers():
            raise ConfigurationError(f"Unsupported LLM_PROVIDER: {llm_provider}")
        if not AnyLLM.get_provider_class(llm_provider).SUPPORTS_COMPLETION:
            raise ConfigurationError(f"LLM_PROVIDER does not support completion: {llm_provider}")
        llm_model = _clean_env(os.getenv("LLM_MODEL"))
        if llm_provider == "deepseek":
            api_key = _clean_env(os.getenv("DEEPSEEK_API_KEY")) or None
            llm_model = llm_model or _clean_env(os.getenv("DEEPSEEK_MODEL"))
            llm_base_url = _first_configured("DEEPSEEK_API_BASE", "DEEPSEEK_BASE_URL")
        else:
            api_key = None
            llm_base_url = None
        if not llm_model:
            raise ConfigurationError("Missing required environment variable: LLM_MODEL")
        locations = _locations(locations_path)
        location_ids = {location.id for location in locations}
        unknown_feed_locations = {
            location_id for feed in feeds for location_id in feed.location_ids if location_id not in location_ids
        }
        if unknown_feed_locations:
            raise ConfigurationError(
                "RSS sources reference unknown location ids: " + ", ".join(sorted(unknown_feed_locations))
            )
        return cls(
            api_key=api_key,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url.rstrip("/") if llm_base_url else None,
            llm_max_output_tokens=_positive_integer("LLM_MAX_OUTPUT_TOKENS", 8192),
            llm_max_attempts=_positive_integer("LLM_MAX_ATTEMPTS", 3),
            http_timeout_seconds=_positive_float("HTTP_TIMEOUT_SECONDS", 30),
            timezone=timezone,
            locations_path=locations_path,
            locations=locations,
            geocoding_base_url=_clean_env(
                os.getenv("GEOCODING_BASE_URL", "https://geocoding-api.open-meteo.com")
            ).rstrip("/"),
            geocoding_api_key=_clean_env(os.getenv("GEOCODING_API_KEY")) or None,
            nominatim_base_url=_clean_env(
                os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
            ).rstrip("/"),
            geocoding_user_agent=_clean_env(
                os.getenv(
                    "GEOCODING_USER_AGENT",
                    "weather-briefing/0.1 (+https://github.com/IceCodeNew/weather-briefing)",
                )
            ),
            geocoding_cache_path=Path(_clean_env(os.getenv("GEOCODING_CACHE_PATH", "state/geocoding.json"))),
            rss_sources_path=rss_sources_path,
            feeds=feeds,
            context_sources=context_sources,
            weather_providers=_configured_weather_providers(),
            qweather_project_id=_clean_env(os.getenv("QWEATHER_PROJECT_ID")) or None,
            qweather_credential_id=_clean_env(os.getenv("QWEATHER_CREDENTIAL_ID")) or None,
            qweather_private_key=_clean_env(os.getenv("QWEATHER_PRIVATE_KEY")) or None,
            qweather_jwt_lifetime_seconds=_bounded_positive_integer("QWEATHER_JWT_LIFETIME_SECONDS", 900, 86_400),
            qweather_base_url=(_clean_env(os.getenv("QWEATHER_API_HOST")) or "").rstrip("/") or None,
            qweather_index_types=tuple(
                item.strip()
                for item in _clean_env(
                    os.getenv(
                        "QWEATHER_INDEX_TYPES",
                        ",".join(reference_string_tuple("provider_defaults.json", "qweather_lifestyle_index_types")),
                    )
                ).split(",")
                if item.strip()
            ),
            open_meteo_weather_base_url=_clean_env(
                os.getenv("OPEN_METEO_WEATHER_BASE_URL", "https://api.open-meteo.com")
            ).rstrip("/"),
            open_meteo_air_quality_base_url=_clean_env(
                os.getenv(
                    "OPEN_METEO_AIR_QUALITY_BASE_URL",
                    "https://air-quality-api.open-meteo.com",
                )
            ).rstrip("/"),
            open_meteo_api_key=_clean_env(os.getenv("OPEN_METEO_API_KEY")) or None,
            aqicn_api_token=_clean_env(os.getenv("AQICN_API_TOKEN")) or None,
            aqicn_base_url=_clean_env(os.getenv("AQICN_BASE_URL", "https://api.waqi.info")).rstrip("/"),
            state_path=state_path_from_env(),
            publisher=_clean_env(os.getenv("PUBLISHER", "telegram")),
            telegram_bot_token=_clean_env(os.getenv("TELEGRAM_BOT_TOKEN")) or None,
            telegram_chat_id=_clean_env(os.getenv("TELEGRAM_CHAT_ID")) or None,
            rss_max_attempts=_positive_integer("RSS_MAX_ATTEMPTS", 3),
            rss_retry_min_seconds=retry_min,
            rss_retry_max_seconds=retry_max,
            rss_stale_hours=_positive_integer("RSS_STALE_HOURS", 24),
            rss_failure_threshold=_positive_integer("RSS_FAILURE_THRESHOLD", 3),
            warning_retention_hours=_positive_integer("WARNING_RETENTION_HOURS", 12),
            history_hours=_positive_integer("HISTORY_HOURS", 48),
            llm_history_max_documents=_bounded_positive_integer("LLM_HISTORY_MAX_DOCUMENTS", 8, 256),
            llm_history_max_characters=_bounded_integer(
                "LLM_HISTORY_MAX_CHARACTERS",
                16_000,
                2,
                1_000_000,
            ),
            briefing_max_characters=briefing_max_characters,
            greeting_hour=daily_cron_hour,
            greeting_minute=daily_cron_minute,
            hourly_cron=hourly_cron,
            debug=_boolean("DEBUG", False),
        )
