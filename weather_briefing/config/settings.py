"""Validated application settings composition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pendulum
from any_llm import AnyLLM

from ..data.bark import BARK_DEFAULT_LLM_MAX_OUTPUT_TOKENS, BARK_MAX_MESSAGE_LENGTH
from ..data.service_endpoints import BARK_BASE_URL
from ..models import FeedConfig, LocationSpec
from ..registries import PublisherName
from .base import ConfigurationError
from .environment import (
    boolean,
    bounded_integer,
    bounded_positive_integer,
    clean_env,
    configured_service_status_language,
    configured_service_status_providers,
    configured_service_status_publishers,
    configured_weather_providers,
    cron_expression,
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

_DEFAULT_LLM_MAX_OUTPUT_TOKENS = 8192


@dataclass(frozen=True, slots=True)
class Settings:
    """Collect validated runtime settings used to compose the application."""

    api_key: str | None
    llm_provider: str
    llm_model: str
    llm_base_url: str | None
    llm_fallback_provider: str | None
    llm_fallback_model: str | None
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
    weather_briefings_enabled: bool
    weather_providers: tuple[str, ...] | None
    service_status_providers: tuple[str, ...]
    service_status_publishers: tuple[str, ...]
    service_status_language: str
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
    bark_device_key: str | None
    bark_base_url: str
    bark_group: str
    bark_encryption_key: str | None
    bark_encryption_iv: str | None
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
    service_status_cron: str
    debug: bool

    @classmethod
    def from_env(cls) -> Settings:
        """Load and validate application settings from the environment."""
        locations_path = path_from_env("BRIEFING_LOCATIONS_FILE", "locations.json")
        rss_sources_path = path_from_env("RSS_SOURCES_FILE", "rss-sources.json")
        service_status_providers = configured_service_status_providers()
        service_status_enabled = bool(service_status_providers)
        weather_briefings_enabled = locations_path.is_file()
        service_status_only_requested = os.getenv("SERVICE_STATUS_PROVIDERS") is not None and service_status_enabled
        if not weather_briefings_enabled and not service_status_only_requested:
            load_locations(locations_path)
        feeds = load_feeds(rss_sources_path) if weather_briefings_enabled else ()
        try:
            timezone = pendulum.timezone(clean_env(os.getenv("BRIEFING_TIMEZONE", "Asia/Shanghai")))
        except (ValueError, KeyError) as exc:
            raise ConfigurationError("Invalid timezone") from exc
        retry_min = number("RSS_RETRY_MIN_SECONDS", 3)
        retry_max = number("RSS_RETRY_MAX_SECONDS", 5)
        if retry_min < 0 or retry_max < retry_min:
            raise ConfigurationError("RSS retry delay range is invalid")
        selected_publisher = publisher()
        service_status_publishers = configured_service_status_publishers(selected_publisher)
        bark_selected = selected_publisher == PublisherName.BARK
        bark_configured = bark_selected or (service_status_enabled and PublisherName.BARK in service_status_publishers)
        telegram_configured = selected_publisher == PublisherName.TELEGRAM or (
            service_status_enabled and PublisherName.TELEGRAM in service_status_publishers
        )
        telegram_bot_token = clean_env(os.getenv("TELEGRAM_BOT_TOKEN")) or None
        telegram_chat_id = clean_env(os.getenv("TELEGRAM_CHAT_ID")) or None
        if telegram_configured:
            if telegram_bot_token is None:
                raise ConfigurationError("Missing required environment variable: TELEGRAM_BOT_TOKEN")
            if telegram_chat_id is None:
                raise ConfigurationError("Missing required environment variable: TELEGRAM_CHAT_ID")
        if bark_selected:
            briefing_max_characters = bounded_positive_integer(
                "BRIEFING_MAX_CHARACTERS",
                BARK_MAX_MESSAGE_LENGTH,
                BARK_MAX_MESSAGE_LENGTH,
            )
        else:
            briefing_max_characters = positive_integer("BRIEFING_MAX_CHARACTERS", 3500)
        llm_max_output_tokens = positive_integer(
            "LLM_MAX_OUTPUT_TOKENS",
            BARK_DEFAULT_LLM_MAX_OUTPUT_TOKENS if bark_selected else _DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        )
        daily_cron_hour = bounded_integer("GREETING_HOUR", 8, 0, 23)
        daily_cron_minute = bounded_integer("GREETING_MINUTE", 0, 0, 59)
        hourly_cron = cron_hour("BRIEFING_CRON", "9-23")
        service_status_cron = cron_expression("SERVICE_STATUS_CRON", "*/5 * * * *")
        llm_provider = clean_env(os.getenv("LLM_PROVIDER", "deepseek"))
        _validate_llm_provider("LLM_PROVIDER", llm_provider)
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
        llm_fallback_provider = clean_env(os.getenv("LLM_FALLBACK_PROVIDER")) or None
        llm_fallback_model = clean_env(os.getenv("LLM_FALLBACK_MODEL")) or None
        if (llm_fallback_provider is None) != (llm_fallback_model is None):
            raise ConfigurationError("LLM_FALLBACK_PROVIDER and LLM_FALLBACK_MODEL must be configured together")
        if llm_fallback_provider is not None:
            _validate_llm_provider("LLM_FALLBACK_PROVIDER", llm_fallback_provider)
        locations = load_locations(locations_path) if weather_briefings_enabled else ()
        location_ids = {location.id for location in locations}
        unknown_feed_locations = {
            location_id for feed in feeds for location_id in feed.location_ids if location_id not in location_ids
        }
        if unknown_feed_locations:
            raise ConfigurationError(
                "RSS sources reference unknown location ids: " + ", ".join(sorted(unknown_feed_locations))
            )
        bark_device_key = None
        bark_base_url = BARK_BASE_URL
        bark_group = "weather-briefing"
        bark_encryption_key = None
        bark_encryption_iv = None
        if bark_configured:
            bark_device_key = clean_env(os.getenv("BARK_DEVICE_KEY")) or None
            if bark_device_key is None:
                raise ConfigurationError("Missing required environment variable: BARK_DEVICE_KEY")
            bark_encryption_key = clean_env(os.getenv("BARK_ENCRYPTION_KEY")) or None
            if bark_encryption_key is not None:
                try:
                    encoded_bark_key = bark_encryption_key.encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ConfigurationError("BARK_ENCRYPTION_KEY must contain only ASCII characters") from exc
                if len(encoded_bark_key) not in {16, 24, 32}:
                    raise ConfigurationError("BARK_ENCRYPTION_KEY must contain 16, 24, or 32 ASCII characters")
            bark_encryption_iv = clean_env(os.getenv("BARK_ENCRYPTION_IV")) or None
            if bark_encryption_iv is not None:
                try:
                    encoded_bark_iv = bark_encryption_iv.encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ConfigurationError("BARK_ENCRYPTION_IV must contain only ASCII characters") from exc
                if len(encoded_bark_iv) != 12:
                    raise ConfigurationError("BARK_ENCRYPTION_IV must contain exactly 12 ASCII characters")
            if (bark_encryption_key is None) != (bark_encryption_iv is None):
                raise ConfigurationError("BARK_ENCRYPTION_KEY and BARK_ENCRYPTION_IV must be configured together")
            bark_base_url = clean_env(os.getenv("BARK_BASE_URL", BARK_BASE_URL)).rstrip("/")
            try:
                parsed_bark_base_url = urlsplit(bark_base_url)
                hostname = parsed_bark_base_url.hostname
                port = parsed_bark_base_url.port
            except ValueError as exc:
                raise ConfigurationError("BARK_BASE_URL must be a valid absolute HTTP(S) URL") from exc
            if (
                parsed_bark_base_url.scheme not in {"http", "https"}
                or hostname is None
                or parsed_bark_base_url.username is not None
                or parsed_bark_base_url.password is not None
                or port == 0
                or parsed_bark_base_url.query
                or parsed_bark_base_url.fragment
            ):
                raise ConfigurationError(
                    "BARK_BASE_URL must be an absolute HTTP(S) URL without credentials or parameters"
                )
            bark_group = clean_env(os.getenv("BARK_GROUP", "weather-briefing"))
            if not bark_group:
                raise ConfigurationError("BARK_GROUP must not be empty")
        return cls(
            api_key=api_key,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url.rstrip("/") if llm_base_url else None,
            llm_fallback_provider=llm_fallback_provider,
            llm_fallback_model=llm_fallback_model,
            llm_max_output_tokens=llm_max_output_tokens,
            llm_max_attempts=positive_integer("LLM_MAX_ATTEMPTS", 3),
            http_timeout_seconds=positive_float("HTTP_TIMEOUT_SECONDS", 30),
            timezone=timezone,
            locations_path=locations_path,
            locations=locations,
            geocoding_api_key=clean_env(os.getenv("GEOCODING_API_KEY")) or None,
            geocoding_cache_path=path_from_env("GEOCODING_CACHE_PATH", "state/geocoding.json"),
            rss_sources_path=rss_sources_path,
            feeds=feeds,
            weather_briefings_enabled=weather_briefings_enabled,
            weather_providers=configured_weather_providers(),
            service_status_providers=service_status_providers,
            service_status_publishers=service_status_publishers,
            service_status_language=configured_service_status_language(),
            qweather_project_id=clean_env(os.getenv("QWEATHER_PROJECT_ID")) or None,
            qweather_credential_id=clean_env(os.getenv("QWEATHER_CREDENTIAL_ID")) or None,
            qweather_private_key=clean_env(os.getenv("QWEATHER_PRIVATE_KEY")) or None,
            qweather_jwt_lifetime_seconds=bounded_positive_integer("QWEATHER_JWT_LIFETIME_SECONDS", 900, 86_400),
            qweather_base_url=(clean_env(os.getenv("QWEATHER_API_HOST")) or "").rstrip("/") or None,
            nea_api_key=clean_env(os.getenv("NEA_API_KEY")) or None,
            open_meteo_api_key=clean_env(os.getenv("OPEN_METEO_API_KEY")) or None,
            aqicn_api_token=clean_env(os.getenv("AQICN_API_TOKEN")) or None,
            state_path=state_path_from_env(),
            publisher=selected_publisher,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            bark_device_key=bark_device_key,
            bark_base_url=bark_base_url,
            bark_group=bark_group,
            bark_encryption_key=bark_encryption_key,
            bark_encryption_iv=bark_encryption_iv,
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
            service_status_cron=service_status_cron,
            debug=boolean("DEBUG", False),
        )


def _validate_llm_provider(setting_name: str, provider: str) -> None:
    """Require a known any-llm provider with completion support."""
    if provider not in AnyLLM.get_supported_providers():
        raise ConfigurationError(f"Unsupported {setting_name}: {provider}")
    if not AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION:
        raise ConfigurationError(f"{setting_name} does not support completion: {provider}")
