"""Environment value parsing and provider selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, overload

from apscheduler.triggers.cron import CronTrigger

from weather_briefing.data.resources import reference_string_tuple
from weather_briefing.languages import normalize_language_tag
from weather_briefing.registries import (
    LOCAL_WEATHER_CAPABILITY_PROVIDERS,
    PublisherName,
    ServiceStatusProviderName,
    WeatherProviderName,
)

from .base import ConfigurationError

if TYPE_CHECKING:
    from weather_briefing.models import ResolvedLocation

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
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":  # noqa: PLR2004
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
        msg = f"{name} must be an integer"
        raise ConfigurationError(msg) from exc


def positive_integer(name: str, default: int) -> int:
    """Read one positive integer environment value."""
    value = integer(name, default)
    if value <= 0:
        msg = f"{name} must be greater than zero"
        raise ConfigurationError(msg)
    return value


def bounded_positive_integer(name: str, default: int, maximum: int) -> int:
    """Read one positive integer with an upper bound."""
    value = positive_integer(name, default)
    if value > maximum:
        msg = f"{name} cannot exceed {maximum}"
        raise ConfigurationError(msg)
    return value


def bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read one integer within inclusive bounds."""
    value = integer(name, default)
    if not minimum <= value <= maximum:
        msg = f"{name} must be between {minimum} and {maximum}"
        raise ConfigurationError(msg)
    return value


def cron_hour(name: str, default: str) -> str:
    """Read and validate an APScheduler hour expression."""
    value = clean_env(os.getenv(name, default))
    if not value:
        msg = f"{name} must not be empty"
        raise ConfigurationError(msg)
    try:
        CronTrigger(hour=value)
    except ValueError as exc:
        msg = f"{name} must be a valid APScheduler hour expression"
        raise ConfigurationError(msg) from exc
    return value


def cron_expression(name: str, default: str) -> str:
    """Read and validate a standard five-field cron expression."""
    value = clean_env(os.getenv(name, default))
    if not value:
        msg = f"{name} must not be empty"
        raise ConfigurationError(msg)
    try:
        CronTrigger.from_crontab(value)
    except ValueError as exc:
        msg = f"{name} must be a valid five-field cron expression"
        raise ConfigurationError(msg) from exc
    return value


def number(name: str, default: float) -> float:
    """Read one floating-point environment value."""
    try:
        return float(clean_env(os.getenv(name, str(default))))
    except ValueError as exc:
        msg = f"{name} must be a number"
        raise ConfigurationError(msg) from exc


def positive_float(name: str, default: float) -> float:
    """Read one positive floating-point environment value."""
    value = number(name, default)
    if value <= 0:
        msg = f"{name} must be greater than zero"
        raise ConfigurationError(msg)
    return value


def boolean(name: str, *, default: bool) -> bool:
    """Read one strict boolean environment value."""
    value = clean_env(os.getenv(name, str(default))).strip().casefold()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no", ""}:
        return False
    msg = f"{name} must be one of: true, false, 1, 0, yes, no"
    raise ConfigurationError(msg)


def configured_weather_providers() -> tuple[str, ...] | None:
    """Read and validate an explicit weather provider order."""
    configured = clean_env(os.getenv("WEATHER_PROVIDERS"))
    if configured is None:
        return None
    providers = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not providers:
        msg = "WEATHER_PROVIDERS cannot be empty"
        raise ConfigurationError(msg)
    unsupported = sorted(set(providers) - SUPPORTED_WEATHER_PROVIDERS)
    if unsupported:
        msg = f"WEATHER_PROVIDERS contains unsupported providers: {', '.join(unsupported)}"
        raise ConfigurationError(msg)
    validate_weather_provider_order(providers)
    return providers


def configured_service_status_providers() -> tuple[str, ...]:
    """Read the enabled official service-status providers."""
    configured = clean_env(os.getenv("SERVICE_STATUS_PROVIDERS", ""))
    if not configured:
        return ()
    providers = tuple(item.strip() for item in configured.split(","))
    if any(not provider for provider in providers):
        msg = "SERVICE_STATUS_PROVIDERS cannot contain empty entries"
        raise ConfigurationError(msg)
    unsupported = sorted(set(providers) - SUPPORTED_SERVICE_STATUS_PROVIDERS)
    if unsupported:
        raise ConfigurationError("SERVICE_STATUS_PROVIDERS contains unsupported providers: " + ", ".join(unsupported))
    if len(providers) != len(set(providers)):
        msg = "SERVICE_STATUS_PROVIDERS cannot contain duplicates"
        raise ConfigurationError(msg)
    return providers


def configured_service_status_publishers(default: str) -> tuple[str, ...]:
    """Read comma-separated service-status publishers with a weather fallback."""
    configured = clean_env(os.getenv("SERVICE_STATUS_PUBLISHERS", default))
    if not configured:
        msg = "SERVICE_STATUS_PUBLISHERS cannot be empty"
        raise ConfigurationError(msg)
    publishers = tuple(item.strip() for item in configured.split(","))
    if not any(publishers):
        msg = "SERVICE_STATUS_PUBLISHERS cannot be empty"
        raise ConfigurationError(msg)
    if any(not publisher for publisher in publishers):
        msg = "SERVICE_STATUS_PUBLISHERS cannot contain empty entries"
        raise ConfigurationError(msg)
    unsupported = sorted(set(publishers) - SUPPORTED_PUBLISHERS)
    if unsupported:
        raise ConfigurationError("SERVICE_STATUS_PUBLISHERS contains unsupported publishers: " + ", ".join(unsupported))
    if len(publishers) != len(set(publishers)):
        msg = "SERVICE_STATUS_PUBLISHERS cannot contain duplicates"
        raise ConfigurationError(msg)
    return publishers


def configured_service_status_language() -> str:
    """Read the language used for direct service-status notifications."""
    value = clean_env(os.getenv("SERVICE_STATUS_LANGUAGE", "en"))
    try:
        language = normalize_language_tag(value)
    except ValueError as exc:
        msg = "SERVICE_STATUS_LANGUAGE must be a supported language tag"
        raise ConfigurationError(msg) from exc
    if language not in SUPPORTED_SERVICE_STATUS_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_SERVICE_STATUS_LANGUAGES))
        msg = f"SERVICE_STATUS_LANGUAGE must be one of: {supported}"
        raise ConfigurationError(msg)
    return language


def validate_weather_provider_order(providers: tuple[str, ...]) -> None:
    """Require local capability providers after primary providers."""
    local_provider_seen = False
    for provider in providers:
        if provider in LOCAL_WEATHER_CAPABILITY_PROVIDERS:
            local_provider_seen = True
        elif local_provider_seen:
            msg = "WEATHER_PROVIDERS must place local capability providers after all primary providers"
            raise ConfigurationError(msg)


def publisher() -> str:
    """Read the configured delivery publisher."""
    selected = clean_env(os.getenv("PUBLISHER", "telegram"))
    if selected not in SUPPORTED_PUBLISHERS:
        msg = f"PUBLISHER must be one of: {', '.join(sorted(SUPPORTED_PUBLISHERS))}"
        raise ConfigurationError(msg)
    return selected


def path_from_env(name: str, default: str) -> Path:
    """Read one non-empty filesystem path environment value."""
    value = clean_env(os.getenv(name, default))
    if not value:
        msg = f"{name} must not be empty"
        raise ConfigurationError(msg)
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
                msg = "nea-sg is only available for locations identified as Singapore"
                raise ConfigurationError(msg)
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
