from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
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
from .config import Settings, weather_providers_for
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
from .publishers import DeliveryProvider, StdoutPublisher, TelegramPublisher
from .render import PlainTextRenderer, TelegramHTMLRenderer
from .service import BriefingService
from .sources import HTTPContextSource, RSSSource
from .state import SQLiteStateStore
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
    return parser


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


async def run(kind: str, enforce_window: bool, at: str | None = None) -> None:
    settings = Settings.from_env()
    now = _parse_run_time(at, settings.timezone)
    if enforce_window and not _in_schedule(kind, now, settings):
        print(f"Skipping delayed {kind} run outside configured local-time window")
        return
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=True
    ) as client:
        delivery = _delivery_provider(settings, client)
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
        resolutions = [
            await resolver.resolve_with_metadata(location)
            for location in settings.locations
        ]
        for resolution in resolutions:
            location = resolution.location
            if location.precision_reduced and not resolution.from_cache:
                await delivery.publish_alert(
                    "位置匹配需要确认",
                    _precision_reduction_notice(location, settings.locations_path),
                )
        locations = tuple(resolution.location for resolution in resolutions)
        for location in locations:
            with SQLiteStateStore(
                _location_state_path(settings.state_path, location, len(locations))
            ) as state:
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
                await service.run(kind, now)


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


def _delivery_provider(settings: Settings, client: httpx.AsyncClient) -> DeliveryProvider:
    if settings.publisher == "stdout":
        return DeliveryProvider(PlainTextRenderer(), StdoutPublisher())
    if settings.publisher == "telegram":
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise ValueError("Telegram publisher requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return DeliveryProvider(
            TelegramHTMLRenderer(),
            TelegramPublisher(client, settings.telegram_bot_token, settings.telegram_chat_id),
            TelegramPublisher.MAX_MESSAGE_LENGTH,
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
        providers[0]
        if len(providers) == 1
        else FallbackWeatherContextProvider(*providers)
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


def _location_state_path(
    base_path: Path, location: ResolvedLocation, location_count: int
) -> Path:
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


def _build_weather_provider(
    name: str, settings: Settings, client: httpx.AsyncClient
) -> WeatherContextProvider:
    builder = WEATHER_PROVIDER_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unsupported weather provider: {name}")
    return builder(settings, client)


def _build_qweather(
    settings: Settings, client: httpx.AsyncClient
) -> WeatherContextProvider:
    project_id = settings.qweather_project_id
    credential_id = settings.qweather_credential_id
    private_key = settings.qweather_private_key
    base_url = settings.qweather_base_url
    if not project_id or not credential_id or not private_key or not base_url:
        raise ValueError(
            "QWeather provider requires project, credential, private key, and API host settings"
        )
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


def _build_open_meteo(
    settings: Settings, client: httpx.AsyncClient
) -> WeatherContextProvider:
    return OpenMeteoProvider(
        client,
        weather_base_url=settings.open_meteo_weather_base_url,
        air_quality_base_url=settings.open_meteo_air_quality_base_url,
        api_key=settings.open_meteo_api_key,
    )


def _aqicn_provider(
    settings: Settings, client: httpx.AsyncClient
) -> AirQualityProvider | None:
    if not settings.aqicn_api_token:
        return None
    return AQICNProvider(
        client,
        token=settings.aqicn_api_token,
        base_url=settings.aqicn_base_url,
    )


WEATHER_PROVIDER_BUILDERS: dict[
    str, Callable[[Settings, httpx.AsyncClient], WeatherContextProvider]
] = {
    "qweather": _build_qweather,
    "open-meteo": _build_open_meteo,
}


def _parse_run_time(
    value: str | None, timezone: pendulum.Timezone
) -> pendulum.DateTime:
    if value is None:
        return pendulum.now(timezone)
    return parse_aware_datetime(value, context="Run time").in_timezone(timezone)


async def daemon(run_now: bool = False) -> None:
    settings = Settings.from_env()
    if run_now:
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


def main() -> None:
    load_dotenv(override=False)
    args = build_parser().parse_args()
    if args.command == "daemon":
        asyncio.run(daemon(args.run_now))
    else:
        asyncio.run(run(args.kind, args.enforce_window, args.at))


if __name__ == "__main__":
    main()
