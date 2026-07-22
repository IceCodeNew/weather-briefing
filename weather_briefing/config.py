"""Runtime configuration parsing and validation."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, overload

import pendulum
from any_llm import AnyLLM
from apscheduler.triggers.cron import CronTrigger
from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector

from .languages import normalize_language_tag
from .models import FeedConfig, LocationSpec, ResolvedLocation, normalize_jma_office_code
from .reference_data import reference_string_tuple
from .registries import LOCAL_WEATHER_CAPABILITY_PROVIDERS, PublisherName, WeatherProviderName


class ConfigurationError(ValueError):
    """Raised when private runtime configuration is missing or malformed."""


SUPPORTED_WEATHER_PROVIDERS = frozenset(WeatherProviderName)
SUPPORTED_PUBLISHERS = frozenset(PublisherName)
_LOCATION_FILE_LOCK_TIMEOUT_SECONDS = 5.0
_LOCATION_FILE_LOCK_RETRY_SECONDS = 0.05


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


def _json_array(path: Path, content: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigurationError(f"{path} must be a JSON array of objects")
    return value


def _json_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    return _json_array(path, content)


def _lock_location_file(path: Path, file_descriptor: int, operation: int, timeout_action: str) -> None:
    deadline = time.monotonic() + _LOCATION_FILE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(file_descriptor, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigurationError(f"{path} is locked; cannot {timeout_action}") from exc
            time.sleep(min(_LOCATION_FILE_LOCK_RETRY_SECONDS, remaining))


def _locked_location_json_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as locations_file:
            try:
                _lock_location_file(path, locations_file.fileno(), fcntl.LOCK_SH, "read location configuration")
            except OSError as exc:
                raise ConfigurationError(f"Failed to lock location configuration {path} for reading: {exc}") from exc
            content = locations_file.read()
    except OSError as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    return _json_array(path, content)


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


def _required_string_field(item: dict[str, Any], field: str, path: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path}.{field} must be a non-empty string")
    return value.strip()


def _optional_string_field(item: dict[str, Any], field: str, path: str) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    return _required_string_field(item, field, path)


def _configured_weather_providers() -> tuple[str, ...] | None:
    configured = _clean_env(os.getenv("WEATHER_PROVIDERS"))
    if configured is None:
        return None
    providers = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not providers:
        raise ConfigurationError("WEATHER_PROVIDERS cannot be empty")
    unsupported = sorted(set(providers) - SUPPORTED_WEATHER_PROVIDERS)
    if unsupported:
        raise ConfigurationError(f"WEATHER_PROVIDERS contains unsupported providers: {', '.join(unsupported)}")
    _validate_weather_provider_order(providers)
    return providers


def _validate_weather_provider_order(providers: tuple[str, ...]) -> None:
    local_provider_seen = False
    for provider in providers:
        if provider in LOCAL_WEATHER_CAPABILITY_PROVIDERS:
            local_provider_seen = True
        elif local_provider_seen:
            raise ConfigurationError(
                "WEATHER_PROVIDERS must place local capability providers after all primary providers"
            )


def _publisher() -> str:
    publisher = _clean_env(os.getenv("PUBLISHER", "telegram"))
    if publisher not in SUPPORTED_PUBLISHERS:
        raise ConfigurationError(f"PUBLISHER must be one of: {', '.join(sorted(SUPPORTED_PUBLISHERS))}")
    return publisher


def state_path_from_env() -> Path:
    """Return the configured SQLite state path without loading all settings."""
    return Path(_clean_env(os.getenv("BRIEFING_STATE_PATH", "state/weather.sqlite3")))


def weather_providers_for(location: ResolvedLocation, configured: tuple[str, ...] | None) -> tuple[str, ...]:
    """Resolve the configured or region-default weather provider order."""
    if configured is not None:
        _validate_weather_provider_order(configured)
        if location.country_code != "SG" and WeatherProviderName.NEA_SINGAPORE in configured:
            available = tuple(provider for provider in configured if provider != WeatherProviderName.NEA_SINGAPORE)
            if not available:
                raise ConfigurationError("nea-sg is only available for locations identified as Singapore")
            return available
        return configured
    region = _weather_region(location)
    providers = reference_string_tuple("provider_defaults.json", "weather_provider_order", region)
    if region == "JP" and location.jma_office_code is None:
        return tuple(provider for provider in providers if provider != WeatherProviderName.JMA_JAPAN)
    return providers


def _weather_region(location: ResolvedLocation) -> str:
    if location.is_mainland_china:
        return "mainland_china"
    if location.country_code in {"SG", "JP"}:
        return location.country_code
    if location.country_code is None and location.jma_office_code is not None:
        return "JP"
    return "other"


def _locations(path: Path) -> tuple[LocationSpec, ...]:
    items = _locked_location_json_file(path)
    if not items:
        raise ConfigurationError(f"Configure at least one location in {path}")
    locations: list[LocationSpec] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        location_id = _required_string_field(item, "id", item_path)
        name = _optional_string_field(item, "name", item_path)
        if not location_id.replace("-", "").replace("_", "").isalnum():
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
        language_value = item.get("language", "en")
        if not isinstance(language_value, str):
            raise ConfigurationError(f"{item_path}.language must be a basic BCP 47-like language tag")
        try:
            summary_language = normalize_language_tag(language_value)
        except ValueError as exc:
            raise ConfigurationError(f"{item_path}.language must be a basic BCP 47-like language tag") from exc
        jma_office_code_value = item.get("jma_office_code")
        if jma_office_code_value is not None and not isinstance(jma_office_code_value, str):
            raise ConfigurationError(f"{item_path}.jma_office_code must be a six-digit JMA office code")
        try:
            jma_office_code = normalize_jma_office_code(jma_office_code_value)
        except ValueError as exc:
            raise ConfigurationError(f"{item_path}.jma_office_code must be a six-digit JMA office code") from exc
        locations.append(
            LocationSpec(
                location_id,
                name,
                latitude,
                longitude,
                summary_language=summary_language,
                jma_office_code=jma_office_code,
            )
        )
        seen_ids.add(location_id)
    return tuple(locations)


def backfill_location_fields(
    path: Path,
    configured: tuple[LocationSpec, ...],
    resolved: tuple[ResolvedLocation, ...],
) -> bool:
    """Write exact provider-resolved names or coordinates missing from the location file."""
    resolved_by_id = {location.id: location for location in resolved if not location.precision_reduced}
    updates: dict[str, dict[str, str | float]] = {}
    for location in configured:
        resolved_location = resolved_by_id.get(location.id)
        if resolved_location is None:
            continue
        fields: dict[str, str | float] = {}
        if location.name is None:
            if not isinstance(resolved_location.name, str) or not resolved_location.name.strip():
                raise ConfigurationError(f"Resolved name for location {location.id} is invalid")
            fields["name"] = resolved_location.name.strip()
        if location.latitude is None and location.longitude is None:
            if not math.isfinite(resolved_location.latitude) or not -90 <= resolved_location.latitude <= 90:
                raise ConfigurationError(f"Resolved latitude for location {location.id} is invalid")
            if not math.isfinite(resolved_location.longitude) or not -180 <= resolved_location.longitude <= 180:
                raise ConfigurationError(f"Resolved longitude for location {location.id} is invalid")
            fields["latitude"] = resolved_location.latitude
            fields["longitude"] = resolved_location.longitude
        if fields:
            updates[location.id] = fields
    if not updates:
        return False

    try:
        with path.open("r+", encoding="utf-8") as locations_file:
            _lock_location_file(
                path,
                locations_file.fileno(),
                fcntl.LOCK_EX,
                "save resolved location fields",
            )
            items = _json_array(path, locations_file.read())
            changed = False
            for item in items:
                location_id = item.get("id")
                if not isinstance(location_id, str) or location_id not in updates:
                    continue
                for field, value in updates[location_id].items():
                    if item.get(field) is None:
                        item[field] = value
                        changed = True
            if not changed:
                return False

            payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
            locations_file.seek(0)
            locations_file.write(payload)
            locations_file.truncate()
            locations_file.flush()
            os.fsync(locations_file.fileno())
    except PermissionError as exc:
        raise ConfigurationError(f"{path} must be writable to save resolved location fields") from exc
    except OSError as exc:
        raise ConfigurationError(f"Failed to save resolved location fields to {path}: {exc}") from exc
    return True


def _feeds(path: Path) -> tuple[FeedConfig, ...]:
    feeds: list[FeedConfig] = []
    for index, item in enumerate(_json_file(path)):
        item_path = f"{path}[{index}]"
        source_id = _required_string_field(item, "id", item_path)
        source_name = _required_string_field(item, "name", item_path)
        source_url = _required_string_field(item, "url", item_path)
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
    geocoding_api_key: str | None
    geocoding_cache_path: Path
    rss_sources_path: Path
    feeds: tuple[FeedConfig, ...]
    weather_providers: tuple[str, ...] | None
    qweather_project_id: str | None
    qweather_credential_id: str | None
    qweather_private_key: str | None
    qweather_jwt_lifetime_seconds: int
    qweather_base_url: str | None
    nea_api_key: str | None
    open_meteo_api_key: str | None
    aqicn_api_token: str | None
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
            geocoding_api_key=_clean_env(os.getenv("GEOCODING_API_KEY")) or None,
            geocoding_cache_path=Path(_clean_env(os.getenv("GEOCODING_CACHE_PATH", "state/geocoding.json"))),
            rss_sources_path=rss_sources_path,
            feeds=feeds,
            weather_providers=_configured_weather_providers(),
            qweather_project_id=_clean_env(os.getenv("QWEATHER_PROJECT_ID")) or None,
            qweather_credential_id=_clean_env(os.getenv("QWEATHER_CREDENTIAL_ID")) or None,
            qweather_private_key=_clean_env(os.getenv("QWEATHER_PRIVATE_KEY")) or None,
            qweather_jwt_lifetime_seconds=_bounded_positive_integer("QWEATHER_JWT_LIFETIME_SECONDS", 900, 86_400),
            qweather_base_url=(_clean_env(os.getenv("QWEATHER_API_HOST")) or "").rstrip("/") or None,
            nea_api_key=_clean_env(os.getenv("NEA_API_KEY")) or None,
            open_meteo_api_key=_clean_env(os.getenv("OPEN_METEO_API_KEY")) or None,
            aqicn_api_token=_clean_env(os.getenv("AQICN_API_TOKEN")) or None,
            state_path=state_path_from_env(),
            publisher=_publisher(),
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
