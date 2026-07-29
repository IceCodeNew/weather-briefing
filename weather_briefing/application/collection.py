"""External context collection stages for one briefing run."""

from __future__ import annotations

import asyncio
import logging

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
_CURRENT_DOCUMENT_MAX_LAG_HOURS = 2


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
    timed_documents = tuple(item for snapshot in snapshots for item in _snapshot_documents_with_times(snapshot))
    if forecast_date is not None:
        return tuple(document for document, _ in timed_documents)
    return _filter_stale_current_documents(timed_documents)


def _filter_stale_current_documents(
    timed_documents: tuple[tuple[SourceDocument, pendulum.DateTime | None], ...],
) -> tuple[SourceDocument, ...]:
    """Remove current documents too old to support a comparison."""
    available_times = tuple(observed_at for _, observed_at in timed_documents if observed_at is not None)
    latest_time = max(available_times)
    earliest_retained_time = latest_time.subtract(hours=_CURRENT_DOCUMENT_MAX_LAG_HOURS)
    retained = tuple(
        document
        for document, observed_at in timed_documents
        if observed_at is None or observed_at >= earliest_retained_time
    )
    for document, observed_at in timed_documents:
        if observed_at is not None and observed_at < earliest_retained_time:
            _LOGGER.info(
                "Discarding stale current document source_id=%s observed_at=%s latest_time=%s max_lag_hours=%d",
                document.id,
                observed_at.to_iso8601_string(),
                latest_time.to_iso8601_string(),
                _CURRENT_DOCUMENT_MAX_LAG_HOURS,
            )
    return retained


def _snapshot_documents_with_times(
    snapshot: WeatherContextSnapshot,
) -> tuple[tuple[SourceDocument, pendulum.DateTime | None], ...]:
    """Attach current observation times to documents that can expire."""
    observation_times = {snapshot.source_id: snapshot.observed_at}
    air_quality = snapshot.air_quality
    if air_quality is not None and air_quality.time_kind is AirQualityTimeKind.OBSERVATION:
        observation_times[air_quality.source_id] = air_quality.effective_at or snapshot.observed_at
    allergen = snapshot.allergen
    if allergen is not None:
        observation_times[allergen.source_id] = allergen.observed_at or snapshot.observed_at
    return tuple((document, observation_times.get(document.id)) for document in snapshot_to_documents(snapshot))
