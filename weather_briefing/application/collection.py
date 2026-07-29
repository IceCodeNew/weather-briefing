"""External context collection stages for one briefing run."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pendulum

from ..capabilities import CapabilityProviderSet
from ..models import (
    AirQualityTimeKind,
    Article,
    FeedConfig,
    ResolvedLocation,
    SourceDocument,
    WeatherContextSnapshot,
)
from ..sources import RSSFeedSource
from ..state import SQLiteStateStore
from ..weather import WeatherContextProvider, fetch_weather_context, snapshot_to_documents

_LOGGER = logging.getLogger("weather_briefing.service")
_AIR_QUALITY_OBSERVATION_MAX_LAG_HOURS = 2


async def collect_rss_articles(
    feeds: tuple[FeedConfig, ...],
    source: RSSFeedSource,
    state: SQLiteStateStore,
    now: pendulum.DateTime,
) -> tuple[Article, ...]:
    """Fetch configured feeds concurrently and update per-source health state."""
    _LOGGER.debug("Fetching %d RSS feed(s)", len(feeds))
    results = await asyncio.gather(*(source.fetch(config) for config in feeds), return_exceptions=True)
    fetched: list[tuple[Article, ...]] = []
    pending_cancellation: BaseException | None = None
    for result, config in zip(results, feeds, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                if pending_cancellation is None:
                    pending_cancellation = result
                continue
            _LOGGER.warning("RSS source %s failed: %s", config.id, result)
            fetched.append(())
            state.record_source_check(config.id, now, None)
            state.record_rss_fetch_failure(config.id)
        else:
            fetched.append(result)
            latest_at = max((article.published_at for article in result), default=None)
            state.record_source_check(config.id, now, latest_at)
            state.record_rss_fetch_success(config.id)
    if pending_cancellation is not None:
        raise pending_cancellation
    articles = tuple(article for group in fetched for article in group)
    _LOGGER.info("Fetched %d article(s) from %d feed(s)", len(articles), len(fetched))
    return articles


async def collect_weather_documents(
    provider: WeatherContextProvider | CapabilityProviderSet | None,
    location: ResolvedLocation,
    forecast_date: pendulum.Date | None,
) -> tuple[SourceDocument, ...]:
    """Fetch every configured weather capability and convert it to source documents."""
    if provider is None:
        return ()
    if isinstance(provider, CapabilityProviderSet):
        snapshots = await provider.fetch_all(
            location.latitude,
            location.longitude,
            forecast_date=forecast_date,
        )
    else:
        snapshots = (await fetch_weather_context(provider, location.latitude, location.longitude, forecast_date),)
    snapshots = _filter_stale_air_quality_observations(snapshots)
    return tuple(document for snapshot in snapshots for document in snapshot_to_documents(snapshot))


def _filter_stale_air_quality_observations(
    snapshots: tuple[WeatherContextSnapshot, ...],
) -> tuple[WeatherContextSnapshot, ...]:
    """Remove observations too old to support a current air-quality conflict."""
    observation_times = tuple(_air_quality_observation_time(snapshot) for snapshot in snapshots)
    available_times = tuple(value for value in observation_times if value is not None)
    if len(available_times) < 2:
        return snapshots

    latest_time = max(available_times)
    earliest_retained_time = latest_time.subtract(hours=_AIR_QUALITY_OBSERVATION_MAX_LAG_HOURS)
    filtered: list[WeatherContextSnapshot] = []
    for snapshot, observation_time in zip(snapshots, observation_times, strict=True):
        air_quality = snapshot.air_quality
        if observation_time is not None and observation_time < earliest_retained_time and air_quality is not None:
            _LOGGER.info(
                "Discarding stale air-quality observation source_id=%s comparison_time=%s "
                "latest_time=%s max_lag_hours=%d",
                air_quality.source_id,
                observation_time.to_iso8601_string(),
                latest_time.to_iso8601_string(),
                _AIR_QUALITY_OBSERVATION_MAX_LAG_HOURS,
            )
            filtered.append(replace(snapshot, air_quality=None))
        else:
            filtered.append(snapshot)
    return tuple(filtered)


def _air_quality_observation_time(snapshot: WeatherContextSnapshot) -> pendulum.DateTime | None:
    """Return the best available time for comparing a current observation."""
    air_quality = snapshot.air_quality
    if air_quality is None or air_quality.time_kind is not AirQualityTimeKind.OBSERVATION:
        return None
    return air_quality.effective_at or snapshot.observed_at
