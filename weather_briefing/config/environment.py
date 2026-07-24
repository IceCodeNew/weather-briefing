"""Environment value parsing and provider selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import overload

from apscheduler.triggers.cron import CronTrigger

from ..data.resources import reference_string_tuple
from ..languages import normalize_language_tag
from ..models import ResolvedLocation
from ..registries import (
    LOCAL_WEATHER_CAPABILITY_PROVIDERS,
    PublisherName,
    ServiceStatusProviderName,
    WeatherProviderName,
)
from .base import ConfigurationError

SUPPORTED_WEATHER_PROVIDERS = frozenset(WeatherProviderName)
SUPPORTED_SERVICE_STATUS_PROVIDERS = frozenset(ServiceStatusProviderName)
SUPPORTED_SERVICE_STATUS_LANGUAGES = frozenset({"en", "ja", "zh-CN"})
SUPPORTED_PUBLISHERS = frozenset(PublisherName)


@overload
def clean_env(value: str) -> str: ...


@overload
def clean_env(value: None) -> None: ...


def clean_env(value: str | None) -> str | None:
    """Strip environment whitespace and one matching quote pair."""
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def first_configured(*names: str) -> str | None:
    """Return the first non-empty configured environment value."""
    for name in names:
        if value := clean_env(os.getenv(name, "")):
            return value
    return None


def integer(name: str, default: int) -> int:
    """Read one integer environment value."""
    try:
        return int(clean_env(os.getenv(name, str(default))))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def positive_integer(name: str, default: int) -> int:
    """Read one positive integer environment value."""
    value = integer(name, default)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def bounded_positive_integer(name: str, default: int, maximum: int) -> int:
    """Read one positive integer with an upper bound."""
    value = positive_integer(name, default)
    if value > maximum:
        raise ConfigurationError(f"{name} cannot exceed {maximum}")
    return value


def bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read one integer within inclusive bounds."""
    value = integer(name, default)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def cron_hour(name: str, default: str) -> str:
    """Read and validate an APScheduler hour expression."""
    value = clean_env(os.getenv(name, default))
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    try:
        CronTrigger(hour=value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid APScheduler hour expression") from exc
    return value


def cron_expression(name: str, default: str) -> str:
    """Read and validate a standard five-field cron expression."""
    value = clean_env(os.getenv(name, default))
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    try:
        CronTrigger.from_crontab(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid five-field cron expression") from exc
    return value


def number(name: str, default: float) -> float:
    """Read one floating-point environment value."""
    try:
        return float(clean_env(os.getenv(name, str(default))))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def positive_float(name: str, default: float) -> float:
    """Read one positive floating-point environment value."""
    value = number(name, default)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def boolean(name: str, default: bool) -> bool:
    """Read one strict boolean environment value."""
    value = clean_env(os.getenv(name, str(default))).strip().casefold()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{name} must be one of: true, false, 1, 0, yes, no")


def configured_weather_providers() -> tuple[str, ...] | None:
    """Read and validate an explicit weather provider order."""
    configured = clean_env(os.getenv("WEATHER_PROVIDERS"))
    if configured is None:
        return None
    providers = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not providers:
        raise ConfigurationError("WEATHER_PROVIDERS cannot be empty")
    unsupported = sorted(set(providers) - SUPPORTED_WEATHER_PROVIDERS)
    if unsupported:
        raise ConfigurationError(f"WEATHER_PROVIDERS contains unsupported providers: {', '.join(unsupported)}")
    validate_weather_provider_order(providers)
    return providers


def configured_service_status_providers() -> tuple[str, ...]:
    """Read the enabled official service-status providers."""
    configured = clean_env(os.getenv("SERVICE_STATUS_PROVIDERS", ""))
    if not configured:
        return ()
    providers = tuple(item.strip() for item in configured.split(",") if item.strip())
    unsupported = sorted(set(providers) - SUPPORTED_SERVICE_STATUS_PROVIDERS)
    if unsupported:
        raise ConfigurationError("SERVICE_STATUS_PROVIDERS contains unsupported providers: " + ", ".join(unsupported))
    if len(providers) != len(set(providers)):
        raise ConfigurationError("SERVICE_STATUS_PROVIDERS cannot contain duplicates")
    return providers


def configured_service_status_publishers(default: str) -> tuple[str, ...]:
    """Read comma-separated service-status publishers with a weather fallback."""
    configured = clean_env(os.getenv("SERVICE_STATUS_PUBLISHERS", default))
    if not configured:
        raise ConfigurationError("SERVICE_STATUS_PUBLISHERS cannot be empty")
    publishers = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not publishers:
        raise ConfigurationError("SERVICE_STATUS_PUBLISHERS cannot be empty")
    unsupported = sorted(set(publishers) - SUPPORTED_PUBLISHERS)
    if unsupported:
        raise ConfigurationError("SERVICE_STATUS_PUBLISHERS contains unsupported publishers: " + ", ".join(unsupported))
    if len(publishers) != len(set(publishers)):
        raise ConfigurationError("SERVICE_STATUS_PUBLISHERS cannot contain duplicates")
    return publishers


def configured_service_status_language() -> str:
    """Read the language used for direct service-status notifications."""
    value = clean_env(os.getenv("SERVICE_STATUS_LANGUAGE", "en"))
    try:
        language = normalize_language_tag(value)
    except ValueError as exc:
        raise ConfigurationError("SERVICE_STATUS_LANGUAGE must be a supported language tag") from exc
    if language not in SUPPORTED_SERVICE_STATUS_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_SERVICE_STATUS_LANGUAGES))
        raise ConfigurationError(f"SERVICE_STATUS_LANGUAGE must be one of: {supported}")
    return language


def validate_weather_provider_order(providers: tuple[str, ...]) -> None:
    """Require local capability providers after primary providers."""
    local_provider_seen = False
    for provider in providers:
        if provider in LOCAL_WEATHER_CAPABILITY_PROVIDERS:
            local_provider_seen = True
        elif local_provider_seen:
            raise ConfigurationError(
                "WEATHER_PROVIDERS must place local capability providers after all primary providers"
            )


def publisher() -> str:
    """Read the configured delivery publisher."""
    selected = clean_env(os.getenv("PUBLISHER", "telegram"))
    if selected not in SUPPORTED_PUBLISHERS:
        raise ConfigurationError(f"PUBLISHER must be one of: {', '.join(sorted(SUPPORTED_PUBLISHERS))}")
    return selected


def path_from_env(name: str, default: str) -> Path:
    """Read one non-empty filesystem path environment value."""
    value = clean_env(os.getenv(name, default))
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    return Path(value)


def state_path_from_env() -> Path:
    """Return the configured SQLite state path without loading all settings."""
    return path_from_env("BRIEFING_STATE_PATH", "state/weather.sqlite3")


def weather_providers_for(location: ResolvedLocation, configured: tuple[str, ...] | None) -> tuple[str, ...]:
    """Resolve the configured or region-default weather provider order."""
    if configured is not None:
        validate_weather_provider_order(configured)
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
