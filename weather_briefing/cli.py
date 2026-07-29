"""Command-line composition, scheduling, and one-shot execution."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path

import pendulum
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from .api_client import LoggedAsyncClient
from .command_parser import build_parser
from .composition.delivery import delivery_provider as _delivery_provider
from .composition.delivery import delivery_providers as _delivery_providers
from .composition.llm import llm_provider as _llm_provider
from .composition.notifications import notification_decision_service as _notification_decision_service
from .composition.weather import weather_context_provider as _weather_context_provider
from .config import ConfigurationError, Settings, backfill_location_fields, state_path_from_env
from .geocoding import (
    CachedLocationResolver,
    FallbackGeocodingProvider,
    NominatimGeocodingProvider,
    OpenMeteoGeocodingProvider,
    PrecisionReducingGeocodingProvider,
)
from .llm import LazyServiceStatusLLM
from .models import ResolvedLocation
from .notification_decision.policies import SERVICE_STATUS_NOTIFICATION_KIND, WEATHER_NOTIFICATION_KIND
from .persistence import locking as persistence_locking
from .runtime_diagnostics import configure_logging as _configure_logging
from .runtime_diagnostics import manage_rendered_text_diagnostics as _manage_rendered_text_diagnostics
from .runtime_diagnostics import runtime_diagnostics as _runtime_diagnostics
from .scheduling import briefing_delivery_policy as _briefing_delivery_policy
from .scheduling import briefing_sent_today as _briefing_sent_today
from .scheduling import in_schedule as _in_schedule
from .service import BriefingService
from .service_status import ServiceStatusMonitor
from .service_status import service_status_providers as _service_status_providers
from .sources import RSSSource
from .state import SQLiteStateStore
from .time_utils import parse_aware_datetime

_LOGGER = logging.getLogger("weather_briefing")


def _save_resolved_location_fields(settings: Settings, locations: tuple[ResolvedLocation, ...]) -> None:
    try:
        changed = backfill_location_fields(settings.locations_path, settings.locations, locations)
    except ConfigurationError as exc:
        _LOGGER.warning(
            "Could not save resolved location fields; continuing without updating %s: %s",
            settings.locations_path,
            exc,
        )
        return
    if changed:
        _LOGGER.info("Saved missing resolved fields to the location configuration")


async def run(
    kind: str,
    enforce_window: bool,
    at: str | None = None,
    *,
    forecast_date: str | None = None,
    run_now: bool = False,
) -> None:
    """Compose dependencies and execute one task across configured locations."""
    async with persistence_locking.serialized_state_run(state_path_from_env()):
        await _run_unlocked(
            kind,
            enforce_window,
            at,
            forecast_date=forecast_date,
            run_now=run_now,
        )


async def _run_unlocked(
    kind: str,
    enforce_window: bool,
    at: str | None,
    *,
    forecast_date: str | None,
    run_now: bool,
) -> None:
    settings = await asyncio.to_thread(Settings.from_env)
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
        llm_provider = await _llm_provider(settings, diagnostics)
        stack.push_async_callback(llm_provider.aclose)
        notification_decisions = _notification_decision_service(
            llm_provider,
            (WEATHER_NOTIFICATION_KIND,),
        )
        nominatim_provider = NominatimGeocodingProvider(client)
        resolver = CachedLocationResolver(
            PrecisionReducingGeocodingProvider(
                FallbackGeocodingProvider(
                    OpenMeteoGeocodingProvider(
                        client,
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
        locations = tuple(resolution.location for resolution in resolutions)
        await asyncio.to_thread(_save_resolved_location_fields, settings, locations)
        for resolution in resolutions:
            location = resolution.location
            if location.precision_reduced and not resolution.from_cache:
                await delivery.publish_alert(
                    "Location match requires confirmation",
                    _precision_reduction_notice(location, settings.locations_path),
                )
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
                    llm_provider,
                    notification_decisions,
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


async def run_service_status() -> None:
    """Poll and directly publish official service-status changes."""
    state_path = state_path_from_env()
    async with persistence_locking.serialized_state_run(state_path):
        settings = await asyncio.to_thread(Settings.from_env)
        _configure_logging(debug=settings.debug)
        if not settings.service_status_providers:
            _LOGGER.info("Skipping service-status run because no providers are configured")
            return
        async with AsyncExitStack() as stack:
            diagnostics = stack.enter_context(_runtime_diagnostics(settings.state_path))
            client = await stack.enter_async_context(
                LoggedAsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True)
            )
            delivery_providers = _delivery_providers(
                settings,
                client,
                settings.service_status_publishers,
                diagnostics,
            )
            deliveries = tuple(zip(settings.service_status_publishers, delivery_providers, strict=True))
            service_status_llm = LazyServiceStatusLLM(lambda: _llm_provider(settings, diagnostics))
            stack.push_async_callback(service_status_llm.aclose)
            notification_decisions = _notification_decision_service(
                service_status_llm,
                (SERVICE_STATUS_NOTIFICATION_KIND,),
            )
            with SQLiteStateStore(settings.state_path) as state:
                monitor = ServiceStatusMonitor(
                    _service_status_providers(settings.service_status_providers, client),
                    state.service_status,
                    deliveries,
                    notification_decisions,
                    service_status_llm,
                    settings.service_status_language,
                )
                published = await monitor.run(pendulum.now(settings.timezone))
            _LOGGER.info("Service-status run published %d notification(s)", published)


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
    state_path = state_path_from_env()
    with persistence_locking.daemon_state_owner(state_path):
        await _daemon(state_path)


async def _daemon(state_path: Path) -> None:
    async with persistence_locking.serialized_state_run(state_path):
        settings = await asyncio.to_thread(Settings.from_env)
    _configure_logging(debug=settings.debug)
    _LOGGER.info("Starting weather-briefing daemon (timezone: %s)", settings.timezone.name)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    if settings.weather_briefings_enabled:
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
    if settings.service_status_providers:
        scheduler.add_job(
            run_service_status,
            CronTrigger.from_crontab(
                settings.service_status_cron,
                timezone=settings.timezone,
            ),
            max_instances=1,
        )
    scheduler.start()
    await asyncio.Event().wait()


def main() -> None:
    """Parse command-line arguments and dispatch the selected command."""
    load_dotenv(override=False)
    args = build_parser().parse_args()
    _configure_logging(debug=False)
    try:
        if args.command == "daemon":
            asyncio.run(daemon())
        elif args.command == "service-status":
            asyncio.run(run_service_status())
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
