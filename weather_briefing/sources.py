from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import datetime
from time import struct_time
from zoneinfo import ZoneInfo

import feedparser
import httpx

from .content_cleaners import ContentCleaner, ContentCleaningRules, HTMLContentCleaner
from .models import Article, ContextSourceConfig, FeedConfig, SourceDocument


class SourceFetchError(RuntimeError):
    """Raised after a source exhausts all retry attempts."""


def _entry_time(entry: feedparser.FeedParserDict, timezone: ZoneInfo) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(timezone)


def _entry_content(entry: feedparser.FeedParserDict) -> str:
    contents = entry.get("content") or []
    if contents:
        return "\n".join(str(item.get("value", "")) for item in contents).strip()
    return str(entry.get("summary", "")).strip()


class RSSSource:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timezone: ZoneInfo,
        max_attempts: int,
        retry_min_seconds: float = 3,
        retry_max_seconds: float = 5,
        cleaner: ContentCleaner | None = None,
    ) -> None:
        self._client = client
        self._timezone = timezone
        self._max_attempts = max_attempts
        self._retry_min_seconds = retry_min_seconds
        self._retry_max_seconds = retry_max_seconds
        self._cleaner = cleaner or HTMLContentCleaner()

    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        response_text = await self._fetch_with_retry(config)
        parsed = feedparser.parse(response_text)
        if parsed.bozo and not parsed.entries:
            raise SourceFetchError(f"RSS parser rejected source {config.id}")
        articles: list[Article] = []
        for entry in parsed.entries:
            published_at = _entry_time(entry, self._timezone)
            if published_at is None:
                continue
            url = str(entry.get("link", "")).strip()
            title = str(entry.get("title", "")).strip()
            stable_value = str(entry.get("id") or url or f"{title}:{published_at.isoformat()}")
            article_id = hashlib.sha256(f"{config.id}:{stable_value}".encode()).hexdigest()
            articles.append(
                Article(
                    id=article_id,
                    source_id=config.id,
                    source_name=config.name,
                    title=title,
                    url=url,
                    published_at=published_at,
                    content=self._cleaner.clean(
                        _entry_content(entry),
                        ContentCleaningRules(
                            config.content_remove_selectors,
                            config.content_remove_patterns,
                        ),
                    ),
                    is_verbatim=any(pattern in title for pattern in config.verbatim_title_patterns),
                )
            )
        return tuple(articles)

    async def _fetch_with_retry(self, config: FeedConfig) -> str:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(config.url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError:
                if attempt < self._max_attempts:
                    await asyncio.sleep(
                        random.uniform(self._retry_min_seconds, self._retry_max_seconds)
                    )
        raise SourceFetchError(
            f"RSS source {config.id} failed after {self._max_attempts} attempts"
        ) from None


class HTTPContextSource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch(self, config: ContextSourceConfig) -> SourceDocument:
        try:
            response = await self._client.get(config.url)
            response.raise_for_status()
        except httpx.HTTPError:
            raise SourceFetchError(f"Context source {config.id} failed") from None
        return SourceDocument(id=config.id, name=config.name, url=config.url, content=response.text)
