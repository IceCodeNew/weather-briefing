"""Validated application settings composition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pendulum
from any_llm import AnyLLM

from ..models import FeedConfig, LocationSpec
from .base import ConfigurationError
from .environment import (
    boolean,
    bounded_integer,
    bounded_positive_integer,
    clean_env,
    configured_weather_providers,
    cron_hour,
    first_configured,
    number,
    path_from_env,
    positive_float,
    positive_integer,
    publisher,
    state_path_from_env,
)
from .feeds import load_feeds
from .locations import load_locations


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
        locations_path = path_from_env("BRIEFING_LOCATIONS_FILE", "locations.json")
        rss_sources_path = path_from_env("RSS_SOURCES_FILE", "rss-sources.json")
        feeds = load_feeds(rss_sources_path)
        try:
            timezone = pendulum.timezone(clean_env(os.getenv("BRIEFING_TIMEZONE", "Asia/Shanghai")))
        except (ValueError, KeyError) as exc:
            raise ConfigurationError("Invalid timezone") from exc
        retry_min = number("RSS_RETRY_MIN_SECONDS", 3)
        retry_max = number("RSS_RETRY_MAX_SECONDS", 5)
        if retry_min < 0 or retry_max < retry_min:
            raise ConfigurationError("RSS retry delay range is invalid")
        briefing_max_characters = positive_integer("BRIEFING_MAX_CHARACTERS", 3500)
        daily_cron_hour = bounded_integer("GREETING_HOUR", 8, 0, 23)
        daily_cron_minute = bounded_integer("GREETING_MINUTE", 0, 0, 59)
        hourly_cron = cron_hour("BRIEFING_CRON", "9-23")
        llm_provider = clean_env(os.getenv("LLM_PROVIDER", "deepseek"))
        if llm_provider not in AnyLLM.get_supported_providers():
            raise ConfigurationError(f"Unsupported LLM_PROVIDER: {llm_provider}")
        if not AnyLLM.get_provider_class(llm_provider).SUPPORTS_COMPLETION:
            raise ConfigurationError(f"LLM_PROVIDER does not support completion: {llm_provider}")
        llm_model = clean_env(os.getenv("LLM_MODEL"))
        if llm_provider == "deepseek":
            api_key = clean_env(os.getenv("DEEPSEEK_API_KEY")) or None
            llm_model = llm_model or clean_env(os.getenv("DEEPSEEK_MODEL"))
            llm_base_url = first_configured("DEEPSEEK_API_BASE", "DEEPSEEK_BASE_URL")
        else:
            api_key = None
            llm_base_url = None
        if not llm_model:
            raise ConfigurationError("Missing required environment variable: LLM_MODEL")
        locations = load_locations(locations_path)
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
            llm_max_output_tokens=positive_integer("LLM_MAX_OUTPUT_TOKENS", 8192),
            llm_max_attempts=positive_integer("LLM_MAX_ATTEMPTS", 3),
            http_timeout_seconds=positive_float("HTTP_TIMEOUT_SECONDS", 30),
            timezone=timezone,
            locations_path=locations_path,
            locations=locations,
            geocoding_api_key=clean_env(os.getenv("GEOCODING_API_KEY")) or None,
            geocoding_cache_path=path_from_env("GEOCODING_CACHE_PATH", "state/geocoding.json"),
            rss_sources_path=rss_sources_path,
            feeds=feeds,
            weather_providers=configured_weather_providers(),
            qweather_project_id=clean_env(os.getenv("QWEATHER_PROJECT_ID")) or None,
            qweather_credential_id=clean_env(os.getenv("QWEATHER_CREDENTIAL_ID")) or None,
            qweather_private_key=clean_env(os.getenv("QWEATHER_PRIVATE_KEY")) or None,
            qweather_jwt_lifetime_seconds=bounded_positive_integer("QWEATHER_JWT_LIFETIME_SECONDS", 900, 86_400),
            qweather_base_url=(clean_env(os.getenv("QWEATHER_API_HOST")) or "").rstrip("/") or None,
            nea_api_key=clean_env(os.getenv("NEA_API_KEY")) or None,
            open_meteo_api_key=clean_env(os.getenv("OPEN_METEO_API_KEY")) or None,
            aqicn_api_token=clean_env(os.getenv("AQICN_API_TOKEN")) or None,
            state_path=state_path_from_env(),
            publisher=publisher(),
            telegram_bot_token=clean_env(os.getenv("TELEGRAM_BOT_TOKEN")) or None,
            telegram_chat_id=clean_env(os.getenv("TELEGRAM_CHAT_ID")) or None,
            rss_max_attempts=positive_integer("RSS_MAX_ATTEMPTS", 3),
            rss_retry_min_seconds=retry_min,
            rss_retry_max_seconds=retry_max,
            rss_stale_hours=positive_integer("RSS_STALE_HOURS", 24),
            rss_failure_threshold=positive_integer("RSS_FAILURE_THRESHOLD", 3),
            warning_retention_hours=positive_integer("WARNING_RETENTION_HOURS", 12),
            history_hours=positive_integer("HISTORY_HOURS", 48),
            llm_history_max_documents=bounded_positive_integer("LLM_HISTORY_MAX_DOCUMENTS", 8, 256),
            llm_history_max_characters=bounded_integer("LLM_HISTORY_MAX_CHARACTERS", 16_000, 2, 1_000_000),
            briefing_max_characters=briefing_max_characters,
            greeting_hour=daily_cron_hour,
            greeting_minute=daily_cron_minute,
            hourly_cron=hourly_cron,
            debug=boolean("DEBUG", False),
        )
