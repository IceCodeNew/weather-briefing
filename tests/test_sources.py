from datetime import date
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from weather_briefing.models import FeedConfig
from weather_briefing.sources import RSSSource, SourceFetchError


async def test_rss_source_marks_configured_verbatim_article() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><guid>one</guid><title>Official forecast bulletin</title>
    <link>https://example.invalid/one</link><pubDate>Sun, 12 Jul 2026 23:30:00 GMT</pubDate>
    <description>full body</description></item></channel></rss>"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = RSSSource(client, timezone=ZoneInfo("Asia/Shanghai"), max_attempts=1)
        articles = await source.fetch(
            FeedConfig(
                id="authority",
                name="Authority",
                url="https://example.invalid/feed",
                verbatim_title_patterns=("forecast bulletin",),
            )
        )

    assert len(articles) == 1
    assert articles[0].published_at.date() == date(2026, 7, 13)
    assert articles[0].is_verbatim is True
    assert articles[0].content == "full body"


async def test_rss_source_retries_with_three_to_five_second_delay(monkeypatch) -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    sleep = AsyncMock()
    monkeypatch.setattr("weather_briefing.sources.asyncio.sleep", sleep)
    monkeypatch.setattr("weather_briefing.sources.random.uniform", lambda low, high: 4.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = RSSSource(client, timezone=ZoneInfo("Asia/Shanghai"), max_attempts=3)
        with pytest.raises(SourceFetchError) as caught:
            await source.fetch(FeedConfig("source", "Source", "https://private.example.invalid"))

    assert attempts == 3
    assert sleep.await_count == 2
    sleep.assert_awaited_with(4.0)
    assert "private.example.invalid" not in str(caught.value)
    assert caught.value.__cause__ is None
