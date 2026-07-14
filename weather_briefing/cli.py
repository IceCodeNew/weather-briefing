from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
from .config import Settings, state_path_from_env, weather_providers_for
from .geocoding import (
    CachedLocationResolver,
    FallbackGeocodingProvider,
    NominatimGeocodingProvider,
    OpenMeteoGeocodingProvider,
    PrecisionReducingGeocodingProvider,
)
from .llm import (
    DeepSeekProvider,
    LLMProvider,
    OpenAICompatibleChatCompletionsProvider,
)
from .models import ResolvedLocation
from .publishers import DeliveryProvider, RenderedTextDiagnostics, StdoutPublisher, TelegramPublisher
from .render import PlainTextRenderer, TelegramHTMLRenderer
from .service import BriefingService
from .sources import HTTPContextSource, RSSSource
from .state import SQLiteRuntimeDiagnostics, SQLiteStateStore
from .time_utils import parse_aware_datetime
from .weather_context import (
    AirQualitySupplementingWeatherProvider,
    FallbackWeatherContextProvider,
    OpenMeteoProvider,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    WeatherContextProvider,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a stateful weather briefing")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("kind", choices=("daily", "hourly"))
    run_parser.add_argument("--enforce-window", action="store_true")
    run_parser.add_argument("--at", help="Override run time with an ISO-8601 timestamp including UTC offset")
    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("--run-now", action="store_true", help="Run a briefing immediately before scheduling")
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
    if kind == "daily":
        return now.hour == settings.greeting_hour
    return _hour_in_cron(now.hour, settings.hourly_cron)


def _hour_in_cron(hour: int, cron_hour: str) -> bool:
    if not 0 <= hour <= 23:
        return False
    current_hour = datetime(2000, 1, 1, hour, tzinfo=UTC)
    trigger = CronTrigger(hour=cron_hour, timezone=UTC)
    return trigger.get_next_fire_time(None, current_hour) == current_hour


_LOGGER = logging.getLogger("weather_briefing")


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
    logging.root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def run(kind: str, enforce_window: bool, at: str | None = None) -> None:
    settings = Settings.from_env()
    _configure_logging(debug=settings.debug)
    now = _parse_run_time(at, settings.timezone)
    if enforce_window and not _in_schedule(kind, now, settings):
        _LOGGER.info("Skipping delayed %s run outside configured local-time window", kind)
        return
    _LOGGER.info("Starting %s briefing run at %s", kind, now.to_iso8601_string())
    async with AsyncExitStack() as stack:
        diagnostics = stack.enter_context(_runtime_diagnostics(settings.state_path))
        client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True)
        )
        delivery = _delivery_provider(settings, client, diagnostics)
        llm_provider = _llm_provider(settings, client)
        resolver = CachedLocationResolver(
            PrecisionReducingGeocodingProvider(
                FallbackGeocodingProvider(
                    OpenMeteoGeocodingProvider(
                        client,
                        base_url=settings.geocoding_base_url,
                        api_key=settings.geocoding_api_key,
                    ),
                    NominatimGeocodingProvider(
                        client,
                        base_url=settings.nominatim_base_url,
                        user_agent=settings.geocoding_user_agent,
                    ),
                )
            ),
            settings.geocoding_cache_path,
        )
        _LOGGER.info("Resolving %d location(s)", len(settings.locations))
        resolutions = [await resolver.resolve_with_metadata(location) for location in settings.locations]
        for resolution in resolutions:
            location = resolution.location
            if location.precision_reduced and not resolution.from_cache:
                await delivery.publish_alert(
                    "位置匹配需要确认",
                    _precision_reduction_notice(location, settings.locations_path),
                )
        locations = tuple(resolution.location for resolution in resolutions)
        for location in locations:
            _LOGGER.info("Processing location %s (%s)", location.id, location.name)
            with SQLiteStateStore(_location_state_path(settings.state_path, location, len(locations))) as state:
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
                body = await service.run(kind, now)
                if body is not None:
                    _LOGGER.info("Location %s %s briefing published (%d characters)", location.id, kind, len(body))
                else:
                    _LOGGER.info("Location %s %s briefing skipped (no content)", location.id, kind)


def _llm_provider(settings: Settings, client: httpx.AsyncClient) -> LLMProvider:
    if settings.llm_provider == "deepseek":
        if settings.llm_base_url:
            return DeepSeekProvider(
                client,
                api_key=settings.api_key,
                model=settings.llm_model,
                max_output_tokens=settings.llm_max_output_tokens,
                base_url=settings.llm_base_url,
            )
        return DeepSeekProvider(
            client,
            api_key=settings.api_key,
            model=settings.llm_model,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url:
            raise ValueError("OpenAI-compatible provider requires LLM_BASE_URL")
        return OpenAICompatibleChatCompletionsProvider(
            client,
            api_key=settings.api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _delivery_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None = None,
) -> DeliveryProvider:
    if settings.publisher == "stdout":
        return DeliveryProvider(PlainTextRenderer(), StdoutPublisher(), diagnostics=diagnostics)
    if settings.publisher == "telegram":
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
    raise ValueError(f"Unsupported publisher: {settings.publisher}")


def _weather_context_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    location: ResolvedLocation,
) -> WeatherContextProvider:
    names = weather_providers_for(location, settings.weather_providers)
    providers: list[WeatherContextProvider] = []
    for name in names:
        if name == "qweather" and not _qweather_is_configured(settings):
            if settings.weather_providers is not None:
                raise ValueError("Explicit QWeather provider is missing JWT configuration")
            continue
        providers.append(_build_weather_provider(name, settings, client))
    if not providers:
        raise ValueError("No configured weather provider is available")
    weather_provider: WeatherContextProvider = (
        providers[0] if len(providers) == 1 else FallbackWeatherContextProvider(*providers)
    )
    return AirQualitySupplementingWeatherProvider(
        weather_provider,
        _aqicn_provider(settings, client),
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
    matched_name = location.matched_name or "未提供匹配名称"
    return (
        f"配置地点“{location.name}”无法直接解析，已降低精度匹配为“{matched_name}”（纬度 "
        f"{location.latitude:.7f}，经度 {location.longitude:.7f}）。请确认该位置是否正确；确认后将坐标写入 "
        f"{locations_path}，可避免后续再次查询和猜测。"
    )


def _build_weather_provider(name: str, settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
    builder = WEATHER_PROVIDER_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unsupported weather provider: {name}")
    return builder(settings, client)


def _build_qweather(settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
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
    "qweather": _build_qweather,
    "open-meteo": _build_open_meteo,
}


def _parse_run_time(value: str | None, timezone: pendulum.Timezone) -> pendulum.DateTime:
    if value is None:
        return pendulum.now(timezone)
    return parse_aware_datetime(value, context="Run time").in_timezone(timezone)


async def daemon(run_now: bool = False) -> None:
    settings = Settings.from_env()
    _configure_logging(debug=settings.debug)
    _LOGGER.info("Starting weather-briefing daemon (timezone: %s)", settings.timezone.name)
    if run_now:
        _LOGGER.info("Running initial briefing")
        await run("hourly", False)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run,
        CronTrigger(
            hour=settings.greeting_hour,
            minute=settings.greeting_minute,
            timezone=settings.timezone,
        ),
        args=("daily", False),
        max_instances=1,
    )
    scheduler.add_job(
        run,
        CronTrigger(
            hour=settings.hourly_cron,
            minute=0,
            timezone=settings.timezone,
        ),
        args=("hourly", False),
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
    load_dotenv(override=False)
    args = build_parser().parse_args()
    _configure_logging(debug=False)
    try:
        if args.command == "daemon":
            asyncio.run(daemon(args.run_now))
        elif args.command == "diagnostics":
            _manage_rendered_text_diagnostics(
                args.diagnostics_action,
                getattr(args, "duration_seconds", None),
            )
        else:
            asyncio.run(run(args.kind, args.enforce_window, args.at))
    except Exception:
        _LOGGER.exception("weather-briefing terminated with an error")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
