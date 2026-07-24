"""External context collection stages for one briefing run."""

from __future__ import annotations

import asyncio
import logging

import pendulum

from ..capabilities import CapabilityProviderSet
from ..models import Article, FeedConfig, ResolvedLocation, SourceDocument
from ..sources import RSSFeedSource
from ..state import SQLiteStateStore
from ..weather import WeatherContextProvider, fetch_weather_context, snapshot_to_documents

_LOGGER = logging.getLogger("weather_briefing.service")


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
    return tuple(document for snapshot in snapshots for document in snapshot_to_documents(snapshot))
