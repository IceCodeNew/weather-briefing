"""RSS and auxiliary context source adapters."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from time import struct_time
from typing import Protocol

import feedparser
import httpx
import pendulum

from .api_client import api_call_extensions
from .content_cleaners import ContentCleaner, ContentCleaningRules, HTMLContentCleaner
from .models import Article, ContextSourceConfig, FeedConfig, SourceDocument

_LOGGER = logging.getLogger("weather_briefing.sources")


class SourceFetchError(RuntimeError):
    """Raised after a source exhausts all retry attempts."""


class RSSFeedSource(Protocol):
    """Fetch and normalize articles from an RSS feed."""

    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        """Fetch articles for one feed configuration."""
        ...


class ContextDocumentSource(Protocol):
    """Fetch auxiliary context as a citable document."""

    async def fetch(self, config: ContextSourceConfig) -> SourceDocument:
        """Fetch one configured context document."""
        ...


def _entry_time(entry: feedparser.FeedParserDict) -> pendulum.DateTime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return pendulum.datetime(*parsed[:6], tz="UTC")


def _entry_content(entry: feedparser.FeedParserDict) -> str:
    contents = entry.get("content") or []
    if contents:
        return "\n".join(str(item.get("value", "")) for item in contents).strip()
    return str(entry.get("summary", "")).strip()


class RSSSource:
    """Fetch, retry, clean, and normalize RSS feed entries."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_attempts: int,
        retry_min_seconds: float = 3,
        retry_max_seconds: float = 5,
        cleaner: ContentCleaner | None = None,
    ) -> None:
        """Configure RSS retries and optional source-content cleaning."""
        self._client = client
        self._max_attempts = max_attempts
        self._retry_min_seconds = retry_min_seconds
        self._retry_max_seconds = retry_max_seconds
        self._cleaner = cleaner or HTMLContentCleaner()

    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        """Fetch and normalize all usable articles from one RSS source."""
        response_text = await self._fetch_with_retry(config)
        parsed = feedparser.parse(response_text)
        if parsed.bozo and not parsed.entries:
            raise SourceFetchError(f"RSS parser rejected source {config.id}")
        articles: list[Article] = []
        for entry in parsed.entries:
            published_at = _entry_time(entry)
            if published_at is None:
                continue
            url = str(entry.get("link", "")).strip()
            title = str(entry.get("title", "")).strip()
            stable_value = str(entry.get("id") or url or f"{title}:{published_at.isoformat()}")
            article_id = hashlib.sha256(f"{config.id}:{stable_value}".encode()).hexdigest()
            content = self._cleaner.clean(
                _entry_content(entry),
                ContentCleaningRules(
                    config.content_remove_selectors,
                    config.content_remove_patterns,
                ),
            )
            is_verbatim = any(pattern in title for pattern in config.verbatim_title_patterns)
            _LOGGER.debug(
                "Parsed RSS article: source=%s published_at=%s content_characters=%d verbatim=%s",
                config.id,
                published_at.isoformat(),
                len(content),
                is_verbatim,
            )
            if is_verbatim and not content:
                continue
            articles.append(
                Article(
                    id=article_id,
                    source_id=config.id,
                    source_name=config.name,
                    title=title,
                    url=url,
                    published_at=published_at,
                    content=content,
                    is_verbatim=is_verbatim,
                )
            )
        return tuple(articles)

    async def _fetch_with_retry(self, config: FeedConfig) -> str:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(
                    config.url,
                    extensions=api_call_extensions("rss", "fetch"),
                )
                response.raise_for_status()
                return response.text
            except httpx.HTTPError:
                if attempt < self._max_attempts:
                    await asyncio.sleep(random.uniform(self._retry_min_seconds, self._retry_max_seconds))
        raise SourceFetchError(f"RSS source {config.id} failed after {self._max_attempts} attempts") from None


class HTTPContextSource:
    """Fetch auxiliary context documents over HTTP."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Use an injected HTTP client for auxiliary context requests."""
        self._client = client

    async def fetch(self, config: ContextSourceConfig) -> SourceDocument:
        """Fetch one context URL without exposing transport details."""
        try:
            response = await self._client.get(
                config.url,
                extensions=api_call_extensions("http-context", "fetch"),
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise SourceFetchError(f"Context source {config.id} failed") from None
        return SourceDocument(id=config.id, name=config.name, url=config.url, content=response.text)
