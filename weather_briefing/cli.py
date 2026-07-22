"""Command-line composition, scheduling, and one-shot execution."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pendulum
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from . import __version__
from .air_quality import (
    AirQualityProvider,
    AQICNProvider,
)
from .api_client import LoggedAsyncClient
from .capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from .config import Settings, state_path_from_env, weather_providers_for
from .geocoding import (
    CachedLocationResolver,
    FallbackGeocodingProvider,
    NominatimGeocodingProvider,
    OpenMeteoGeocodingProvider,
    PrecisionReducingGeocodingProvider,
)
from .llm import AnyLLMStructuredProvider, create_any_llm_provider
from .models import ResolvedLocation
from .publishers import DeliveryProvider, RenderedTextDiagnostics, StdoutPublisher, TelegramPublisher
from .regional_weather import (
    JMA_LANGUAGE_SUPPORT,
    NEA_LANGUAGE_SUPPORT,
    JMAJapanForecastProvider,
    NEASingaporeNowcastProvider,
)
from .registries import LOCAL_WEATHER_CAPABILITY_PROVIDERS, PublisherName, WeatherProviderName
from .render import PlainTextRenderer, TelegramHTMLRenderer
from .service import BriefingService
from .sources import HTTPContextSource, RSSSource
from .state import SQLiteRuntimeDiagnostics, SQLiteStateStore
from .time_utils import parse_aware_datetime
from .weather_context import (
    OPEN_METEO_LANGUAGE_SUPPORT,
    QWEATHER_LANGUAGE_SUPPORT,
    FallbackWeatherContextProvider,
    LoggedWeatherContextProvider,
    OpenMeteoProvider,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    WeatherContextProvider,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for runs, daemon, and diagnostics."""
    parser = argparse.ArgumentParser(description="Generate a stateful weather briefing")
    parser.add_argument("-V", "--version", action=_VersionAction, nargs=0)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("kind", choices=("forecast", "briefing"))
    timing_group = run_parser.add_mutually_exclusive_group()
    timing_group.add_argument("--enforce-window", action="store_true")
    timing_group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the selected one-shot task immediately; briefings also publish deferred information",
    )
    run_time_group = run_parser.add_mutually_exclusive_group()
    run_time_group.add_argument("--at", help="Override run time with an ISO-8601 timestamp including UTC offset")
    run_time_group.add_argument("--date", help="Generate a forecast for a local date in YYYY-MM-DD format")
    subparsers.add_parser("daemon")
    diagnostics_parser = subparsers.add_parser("diagnostics")
    diagnostics_topics = diagnostics_parser.add_subparsers(dest="diagnostics_topic", required=True)
    rendered_text_parser = diagnostics_topics.add_parser("rendered-text")
    rendered_text_actions = rendered_text_parser.add_subparsers(dest="diagnostics_action", required=True)
    enable_parser = rendered_text_actions.add_parser("enable")
    enable_parser.add_argument(
        "--for",
        dest="duration_seconds",
        required=True,
        type=_diagnostic_duration_seconds,
        metavar="DURATION",
        help="Enable sensitive rendered-text logging temporarily, for example 15m or 1h (maximum 24h)",
    )
    rendered_text_actions.add_parser("status")
    rendered_text_actions.add_parser("disable")
    return parser


class _VersionAction(argparse.Action):
    """Resolve development Git metadata only when version output is requested."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        print(f"{parser.prog} {_display_version()}")
        parser.exit()


def _display_version() -> str:
    """Add Git revision details to development versions when available."""
    if not __version__.endswith("-dev"):
        return __version__

    repository_root = Path(__file__).resolve().parents[1]
    try:
        git_metadata = subprocess.run(
            (
                "git",
                "-C",
                str(repository_root),
                "rev-parse",
                "--show-toplevel",
                "--short=7",
                "HEAD",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if len(git_metadata) != 2 or Path(git_metadata[0]).resolve() != repository_root:
            return __version__
        revision = git_metadata[1]
        status = subprocess.run(
            ("git", "-C", str(repository_root), "status", "--porcelain"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return __version__

    version = __version__.removesuffix("-dev")
    dirty = "-dirty" if status else ""
    return f"{version}{dirty}-g{revision}"


_DIAGNOSTIC_DURATION_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smh])$")


def _diagnostic_duration_seconds(value: str) -> int:
    match = _DIAGNOSTIC_DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("duration must use a positive value followed by s, m, or h")
    multipliers = {"s": 1, "m": 60, "h": 3600}
    seconds = int(match.group("value")) * multipliers[match.group("unit")]
    if seconds > 24 * 60 * 60:
        raise argparse.ArgumentTypeError("duration cannot exceed 24h")
    return seconds


def _in_schedule(kind: str, now: pendulum.DateTime, settings: Settings) -> bool:
    if kind == "forecast":
        return now.hour == settings.greeting_hour
    return _hour_in_cron(now.hour, settings.hourly_cron)


def _hour_in_cron(hour: int, cron_hour: str) -> bool:
    if not 0 <= hour <= 23:
        return False
    current_hour = datetime(2000, 1, 1, hour, tzinfo=UTC)
    trigger = CronTrigger(hour=cron_hour, timezone=UTC)
    return trigger.get_next_fire_time(None, current_hour) == current_hour


def _is_last_briefing_window(now: pendulum.DateTime, cron_hour: str) -> bool:
    return _hour_in_cron(now.hour, cron_hour) and not any(
        _hour_in_cron(hour, cron_hour) for hour in range(now.hour + 1, 24)
    )


def _briefing_delivery_policy(
    kind: str,
    now: pendulum.DateTime,
    settings: Settings,
    *,
    run_now: bool,
    briefing_sent_today: bool,
) -> tuple[bool, bool]:
    if kind != "briefing":
        return False, False
    if run_now:
        return True, False
    if _is_last_briefing_window(now, settings.hourly_cron) and not briefing_sent_today:
        return True, True
    return False, False


def _briefing_sent_today(
    kind: str,
    now: pendulum.DateTime,
    settings: Settings,
    state: SQLiteStateStore,
    *,
    run_now: bool,
) -> bool:
    if kind != "briefing" or run_now or not _is_last_briefing_window(now, settings.hourly_cron):
        return False
    local_now = now.in_timezone(settings.timezone)
    return state.has_briefing_between("briefing", local_now.start_of("day"), local_now)


_LOGGER = logging.getLogger("weather_briefing")
_SENSITIVE_SDK_LOGGERS = ("any_llm", "openai", "httpx", "httpcore")


@contextmanager
def _runtime_diagnostics(path: Path) -> Iterator[RenderedTextDiagnostics | None]:
    try:
        diagnostics = SQLiteRuntimeDiagnostics(path)
    except (OSError, sqlite3.Error):
        _LOGGER.warning(
            "Runtime diagnostics unavailable; continuing without sensitive rendered text logging",
            exc_info=True,
        )
        yield None
        return
    with diagnostics:
        yield diagnostics


def _configure_logging(*, debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    _fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not _LOGGER.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_fmt)
        _LOGGER.addHandler(handler)
    _LOGGER.setLevel(level)
    _LOGGER.propagate = False
    if not logging.root.handlers:
        root_handler = logging.StreamHandler(sys.stderr)
        root_handler.setFormatter(_fmt)
        logging.root.addHandler(root_handler)
    logging.root.setLevel(logging.WARNING)
    for handler in logging.root.handlers:
        handler.setLevel(logging.WARNING)
    for logger_name in _SENSITIVE_SDK_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


async def run(
    kind: str,
    enforce_window: bool,
    at: str | None = None,
    *,
    forecast_date: str | None = None,
    run_now: bool = False,
) -> None:
    """Compose dependencies and execute one task across configured locations."""
    settings = Settings.from_env()
    _configure_logging(debug=settings.debug)
    if forecast_date is not None and kind != "forecast":
        raise ValueError("--date is only supported for run forecast")
    now = _parse_run_time(at, settings.timezone)
    target_forecast_date = _parse_forecast_date(forecast_date) if forecast_date is not None else None
    if target_forecast_date is not None and target_forecast_date < now.in_timezone(settings.timezone).date():
        raise ValueError("--date cannot be earlier than the current local date; use --at for historical tests")
    if enforce_window and not _in_schedule(kind, now, settings):
        _LOGGER.info("Skipping delayed %s run outside configured local-time window", kind)
        return
    _LOGGER.info("Starting %s run at %s", kind, now.to_iso8601_string())
    async with AsyncExitStack() as stack:
        diagnostics = stack.enter_context(_runtime_diagnostics(settings.state_path))
        client = await stack.enter_async_context(
            LoggedAsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True)
        )
        delivery = _delivery_provider(settings, client, diagnostics)
        llm_provider = _llm_provider(settings, diagnostics)
        stack.push_async_callback(llm_provider.aclose)
        nominatim_provider = NominatimGeocodingProvider(
            client,
            base_url=settings.nominatim_base_url,
            user_agent=settings.geocoding_user_agent,
        )
        resolver = CachedLocationResolver(
            PrecisionReducingGeocodingProvider(
                FallbackGeocodingProvider(
                    OpenMeteoGeocodingProvider(
                        client,
                        base_url=settings.geocoding_base_url,
                        api_key=settings.geocoding_api_key,
                    ),
                    nominatim_provider,
                )
            ),
            settings.geocoding_cache_path,
            reverse_provider=nominatim_provider,
        )
        _LOGGER.info("Resolving %d location(s)", len(settings.locations))
        resolutions = [await resolver.resolve_with_metadata(location) for location in settings.locations]
        for resolution in resolutions:
            location = resolution.location
            if location.precision_reduced and not resolution.from_cache:
                await delivery.publish_alert(
                    "Location match requires confirmation",
                    _precision_reduction_notice(location, settings.locations_path),
                )
        locations = tuple(resolution.location for resolution in resolutions)
        for location in locations:
            _LOGGER.info("Processing location %s", location.id)
            _LOGGER.debug("Location %s display name: %s", location.id, location.name)
            with SQLiteStateStore(_location_state_path(settings.state_path, location, len(locations))) as state:
                briefing_sent_today = _briefing_sent_today(kind, now, settings, state, run_now=run_now)
                force_publish, silent = _briefing_delivery_policy(
                    kind,
                    now,
                    settings,
                    run_now=run_now,
                    briefing_sent_today=briefing_sent_today,
                )
                service = BriefingService(
                    settings,
                    location,
                    state,
                    RSSSource(
                        client,
                        max_attempts=settings.rss_max_attempts,
                        retry_min_seconds=settings.rss_retry_min_seconds,
                        retry_max_seconds=settings.rss_retry_max_seconds,
                    ),
                    HTTPContextSource(client),
                    llm_provider,
                    delivery,
                    delivery,
                    _weather_context_provider(settings, client, location),
                )
                body = await service.run(
                    kind,
                    now,
                    forecast_date=target_forecast_date,
                    force_publish=force_publish,
                    silent=silent,
                )
                if body is not None:
                    _LOGGER.info("Location %s %s published (%d characters)", location.id, kind, len(body))
                else:
                    _LOGGER.info("Location %s %s skipped (no content)", location.id, kind)


def _llm_provider(
    settings: Settings,
    diagnostics: RenderedTextDiagnostics | None = None,
) -> AnyLLMStructuredProvider:
    return create_any_llm_provider(
        settings.llm_provider,
        settings.llm_model,
        settings.llm_max_output_tokens,
        api_key=settings.api_key,
        api_base=settings.llm_base_url,
        diagnostics=diagnostics,
    )


def _delivery_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None = None,
) -> DeliveryProvider:
    builder = PUBLISHER_BUILDERS.get(settings.publisher)
    if builder is None:
        raise ValueError(f"Unsupported publisher: {settings.publisher}")
    return builder(settings, client, diagnostics)


def _build_stdout_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    return DeliveryProvider(PlainTextRenderer(), StdoutPublisher(), diagnostics=diagnostics)


def _build_telegram_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError("Telegram publisher requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return DeliveryProvider(
        TelegramHTMLRenderer(),
        TelegramPublisher(
            client,
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            diagnostics,
        ),
        single_message_limit=TelegramPublisher.MAX_MESSAGE_LENGTH,
        diagnostics=diagnostics,
    )


PUBLISHER_BUILDERS: dict[
    str,
    Callable[[Settings, httpx.AsyncClient, RenderedTextDiagnostics | None], DeliveryProvider],
] = {
    PublisherName.STDOUT: _build_stdout_publisher,
    PublisherName.TELEGRAM: _build_telegram_publisher,
}

_WEATHER_PROVIDER_METADATA: dict[str, ProviderCapabilities] = {
    WeatherProviderName.QWEATHER: ProviderCapabilities(
        provider_id=WeatherProviderName.QWEATHER,
        provider_name="QWeather",
        capabilities=frozenset(
            {
                CapabilityName.WEATHER,
                CapabilityName.AIR_QUALITY,
                CapabilityName.LIFESTYLE,
            }
        ),
        language_support=QWEATHER_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.OPEN_METEO: ProviderCapabilities(
        provider_id=WeatherProviderName.OPEN_METEO,
        provider_name="Open-Meteo",
        capabilities=frozenset(
            {
                CapabilityName.WEATHER,
                CapabilityName.AIR_QUALITY,
                CapabilityName.ALLERGEN,
            }
        ),
        language_support=OPEN_METEO_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.NEA_SINGAPORE: ProviderCapabilities(
        provider_id=WeatherProviderName.NEA_SINGAPORE,
        provider_name="Singapore NEA",
        capabilities=frozenset({CapabilityName.NOWCAST}),
        language_support=NEA_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.JMA_JAPAN: ProviderCapabilities(
        provider_id=WeatherProviderName.JMA_JAPAN,
        provider_name="Japan JMA",
        capabilities=frozenset({CapabilityName.WEATHER}),
        language_support=JMA_LANGUAGE_SUPPORT,
    ),
}


def _weather_provider_metadata(names: Sequence[str]) -> ProviderCapabilities:
    """Describe capabilities common to every active fallback provider."""
    metadata: list[ProviderCapabilities] = []
    for name in names:
        item = _WEATHER_PROVIDER_METADATA.get(name)
        if item is None:
            raise ValueError(f"Weather provider {name!r} has no capability metadata")
        metadata.append(item)
    if len(metadata) == 1:
        return metadata[0]
    capabilities = metadata[0].capabilities
    for item in metadata[1:]:
        capabilities &= item.capabilities
    return ProviderCapabilities(
        provider_id="weather-composite",
        provider_name="Weather provider composite",
        capabilities=capabilities,
    )


def _weather_context_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    location: ResolvedLocation,
) -> CapabilityProviderSet:
    names = weather_providers_for(location, settings.weather_providers)
    if (
        settings.weather_providers is not None
        and WeatherProviderName.NEA_SINGAPORE in settings.weather_providers
        and WeatherProviderName.NEA_SINGAPORE not in names
    ):
        reason = "missing-country-code" if location.country_code is None else "known-non-singapore-country"
        _LOGGER.warning("Skipping explicit NEA provider reason=%s", reason)
    jma_available = location.jma_office_code is not None and location.country_code in {None, "JP"}
    if settings.weather_providers is not None and WeatherProviderName.JMA_JAPAN in names and not jma_available:
        reason = "missing-jma-office-code" if location.jma_office_code is None else "known-non-japan-country"
        _LOGGER.warning("Skipping explicit JMA provider reason=%s", reason)
    main_names = [name for name in names if name not in LOCAL_WEATHER_CAPABILITY_PROVIDERS]
    supplement_names = [
        name
        for name in names
        if name in LOCAL_WEATHER_CAPABILITY_PROVIDERS and (name != WeatherProviderName.JMA_JAPAN or jma_available)
    ]
    if not main_names:
        main_names, supplement_names = supplement_names, []
    providers: list[WeatherContextProvider] = []
    active_names: list[str] = []
    for name in main_names:
        if name == WeatherProviderName.QWEATHER and not _qweather_is_configured(settings):
            if settings.weather_providers is not None:
                raise ValueError("Explicit QWeather provider is missing JWT configuration")
            continue
        providers.append(
            LoggedWeatherContextProvider(
                name,
                _build_weather_provider(
                    name,
                    settings,
                    client,
                    location.summary_language,
                    jma_office_code=location.jma_office_code,
                ),
            )
        )
        active_names.append(name)
    if not providers:
        raise ValueError("No configured weather provider is available")
    supplements = tuple(
        LoggedWeatherContextProvider(
            name,
            _build_weather_provider(
                name,
                settings,
                client,
                location.summary_language,
                jma_office_code=location.jma_office_code,
            ),
        )
        for name in supplement_names
    )
    _LOGGER.info(
        "Weather provider order providers=%s",
        ",".join(
            name
            for name in (*main_names, *supplement_names)
            if name != WeatherProviderName.QWEATHER or _qweather_is_configured(settings)
        ),
    )
    weather_provider: WeatherContextProvider = (
        providers[0] if len(providers) == 1 else FallbackWeatherContextProvider(*providers)
    )
    air_quality_provider = _aqicn_provider(settings, client)
    return CapabilityProviderSet(
        weather=weather_provider,
        weather_metadata=_weather_provider_metadata(active_names),
        air_quality=air_quality_provider,
        air_quality_metadata=(
            ProviderCapabilities(
                provider_id="air-quality:aqicn",
                provider_name="AQICN",
                capabilities=frozenset({CapabilityName.AIR_QUALITY}),
            )
            if air_quality_provider is not None
            else None
        ),
        supplements=supplements,
        supplement_metadata=tuple(_WEATHER_PROVIDER_METADATA[name] for name in supplement_names),
    )


def _qweather_is_configured(settings: Settings) -> bool:
    return all(
        (
            settings.qweather_project_id,
            settings.qweather_credential_id,
            settings.qweather_private_key,
            settings.qweather_base_url,
        )
    )


def _location_state_path(base_path: Path, location: ResolvedLocation, location_count: int) -> Path:
    if location_count == 1:
        return base_path
    suffix = base_path.suffix or ".sqlite3"
    return base_path.with_name(f"{base_path.stem}-{location.id}{suffix}")


def _precision_reduction_notice(location: ResolvedLocation, locations_path: Path) -> str:
    matched_name = location.matched_name or "no matched name provided"
    return (
        f'The configured location "{location.name}" could not be resolved exactly and was matched at reduced '
        f'precision as "{matched_name}" (latitude {location.latitude:.7f}, longitude {location.longitude:.7f}). '
        f"Confirm that this location is correct. Add the coordinates to {locations_path} to avoid future lookups "
        "and approximation."
    )


def _build_weather_provider(
    name: str,
    settings: Settings,
    client: httpx.AsyncClient,
    output_language: str = "en",
    *,
    jma_office_code: str | None = None,
) -> WeatherContextProvider:
    builder = WEATHER_PROVIDER_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unsupported weather provider: {name}")
    if name == WeatherProviderName.QWEATHER:
        return _build_qweather(settings, client, output_language=output_language)
    if name == WeatherProviderName.NEA_SINGAPORE:
        return NEASingaporeNowcastProvider(
            client,
            base_url=settings.nea_base_url,
            api_key=settings.nea_api_key,
        )
    if name == WeatherProviderName.JMA_JAPAN:
        return _build_jma(settings, client, office_code=jma_office_code)
    return builder(settings, client)


def _build_qweather(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    output_language: str = "en",
) -> WeatherContextProvider:
    project_id = settings.qweather_project_id
    credential_id = settings.qweather_credential_id
    private_key = settings.qweather_private_key
    base_url = settings.qweather_base_url
    if not project_id or not credential_id or not private_key or not base_url:
        raise ValueError("QWeather provider requires project, credential, private key, and API host settings")
    return QWeatherProvider(
        client,
        authenticator=QWeatherJWTAuthenticator(
            project_id=project_id,
            credential_id=credential_id,
            private_key_base64=private_key,
            lifetime_seconds=settings.qweather_jwt_lifetime_seconds,
        ),
        base_url=base_url,
        index_types=settings.qweather_index_types,
        output_language=output_language,
    )


def _build_nea(settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
    return NEASingaporeNowcastProvider(client, base_url=settings.nea_base_url, api_key=settings.nea_api_key)


def _build_jma(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    office_code: str | None = None,
) -> WeatherContextProvider:
    if office_code is None:
        raise ValueError("JMA provider requires locations.json jma_office_code")
    return JMAJapanForecastProvider(
        client,
        base_url=settings.jma_base_url,
        office_code=office_code,
    )


def _build_open_meteo(settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
    return OpenMeteoProvider(
        client,
        weather_base_url=settings.open_meteo_weather_base_url,
        air_quality_base_url=settings.open_meteo_air_quality_base_url,
        api_key=settings.open_meteo_api_key,
    )


def _aqicn_provider(settings: Settings, client: httpx.AsyncClient) -> AirQualityProvider | None:
    if not settings.aqicn_api_token:
        return None
    return AQICNProvider(
        client,
        token=settings.aqicn_api_token,
        base_url=settings.aqicn_base_url,
    )


WEATHER_PROVIDER_BUILDERS: dict[str, Callable[[Settings, httpx.AsyncClient], WeatherContextProvider]] = {
    WeatherProviderName.QWEATHER: _build_qweather,
    WeatherProviderName.OPEN_METEO: _build_open_meteo,
    WeatherProviderName.NEA_SINGAPORE: _build_nea,
    WeatherProviderName.JMA_JAPAN: _build_jma,
}


def _parse_run_time(value: str | None, timezone: pendulum.Timezone) -> pendulum.DateTime:
    if value is None:
        return pendulum.now(timezone)
    return parse_aware_datetime(value, context="Run time").in_timezone(timezone)


def _parse_forecast_date(value: str) -> pendulum.Date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Forecast date must use YYYY-MM-DD")
    try:
        target = date.fromisoformat(value)
        return pendulum.date(target.year, target.month, target.day)
    except ValueError:
        raise ValueError("Forecast date must be a valid date") from None


async def daemon() -> None:
    """Run the in-process forecast and briefing scheduler indefinitely."""
    settings = Settings.from_env()
    _configure_logging(debug=settings.debug)
    _LOGGER.info("Starting weather-briefing daemon (timezone: %s)", settings.timezone.name)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run,
        CronTrigger(
            hour=settings.greeting_hour,
            minute=settings.greeting_minute,
            timezone=settings.timezone,
        ),
        args=("forecast", True),
        max_instances=1,
    )
    scheduler.add_job(
        run,
        CronTrigger(
            hour=settings.hourly_cron,
            minute=0,
            timezone=settings.timezone,
        ),
        args=("briefing", True),
        max_instances=1,
    )
    scheduler.start()
    await asyncio.Event().wait()


def _manage_rendered_text_diagnostics(action: str, duration_seconds: int | None = None) -> None:
    with SQLiteRuntimeDiagnostics(state_path_from_env()) as diagnostics:
        if action == "enable":
            if duration_seconds is None:
                raise ValueError("Rendered text diagnostics require a duration")
            expires_at = pendulum.now("UTC").add(seconds=duration_seconds)
            diagnostics.enable_rendered_text_logging(expires_at)
            print(
                "Rendered text diagnostic logging enabled until "
                f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
            )
            return
        if action == "disable":
            diagnostics.disable_rendered_text_logging()
            print("Rendered text diagnostic logging disabled")
            return
        if action == "status":
            expires_at = diagnostics.rendered_text_logging_until()
            if expires_at is None:
                print("Rendered text diagnostic logging is disabled")
            else:
                print(
                    "Rendered text diagnostic logging is enabled until "
                    f"{expires_at.to_iso8601_string()}; rendered bodies require DEBUG logging"
                )
            return
        raise ValueError(f"Unsupported rendered text diagnostics action: {action}")


def main() -> None:
    """Parse command-line arguments and dispatch the selected command."""
    load_dotenv(override=False)
    args = build_parser().parse_args()
    _configure_logging(debug=False)
    try:
        if args.command == "daemon":
            asyncio.run(daemon())
        elif args.command == "diagnostics":
            _manage_rendered_text_diagnostics(
                args.diagnostics_action,
                getattr(args, "duration_seconds", None),
            )
        else:
            asyncio.run(
                run(
                    args.kind,
                    args.enforce_window,
                    args.at,
                    forecast_date=args.date,
                    run_now=args.run_now,
                )
            )
    except Exception:
        _LOGGER.exception("weather-briefing terminated with an error")
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover - console-script bootstrap
    main()
