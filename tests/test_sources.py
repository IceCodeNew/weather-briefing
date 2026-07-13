from unittest.mock import AsyncMock

import httpx
import pytest

from weather_briefing.models import ContextSourceConfig, FeedConfig, SourceDocument
from weather_briefing.sources import HTTPContextSource, RSSSource, SourceFetchError


async def test_rss_source_marks_configured_verbatim_article() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><guid>one</guid><title>Official forecast bulletin</title>
    <link>https://example.invalid/one</link><pubDate>Sun, 12 Jul 2026 23:30:00 GMT</pubDate>
    <description>full body</description></item></channel></rss>"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = RSSSource(client, max_attempts=1)
        articles = await source.fetch(
            FeedConfig(
                id="authority",
                name="Authority",
                url="https://example.invalid/feed",
                verbatim_title_patterns=("forecast bulletin",),
            )
        )

    assert len(articles) == 1
    assert articles[0].published_at.to_iso8601_string() == "2026-07-12T23:30:00Z"
    assert articles[0].published_at.in_timezone("Asia/Shanghai").to_iso8601_string() == "2026-07-13T07:30:00+08:00"
    assert articles[0].is_verbatim is True
    assert articles[0].content == "full body"


async def test_rss_source_uses_content_fields_when_available() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><guid>one</guid><title>Rich article</title>
    <link>https://example.invalid/one</link><pubDate>Sun, 12 Jul 2026 23:30:00 GMT</pubDate>
    <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"><![CDATA[full body]]></content:encoded>
    <description>short summary</description></item></channel></rss>"""

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=xml))) as client:
        articles = await RSSSource(client, max_attempts=1).fetch(
            FeedConfig("feed", "Feed", "https://example.invalid/feed")
        )

    assert len(articles) == 1
    assert articles[0].content == "full body"


async def test_rss_source_skips_entry_without_published_time() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><guid>one</guid><title>No time</title>
    <link>https://example.invalid/one</link><description>body without time</description></item>
    <item><guid>two</guid><title>Has time</title>
    <link>https://example.invalid/two</link><pubDate>Sun, 12 Jul 2026 23:30:00 GMT</pubDate>
    <description>body with time</description></item></channel></rss>"""

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=xml))) as client:
        articles = await RSSSource(client, max_attempts=1).fetch(
            FeedConfig("feed", "Feed", "https://example.invalid/feed")
        )

    assert len(articles) == 1
    assert articles[0].title == "Has time"


async def test_rss_source_rejects_badly_formatted_feed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not xml or rss"))
    ) as client:
        source = RSSSource(client, max_attempts=1)
        with pytest.raises(SourceFetchError, match="RSS parser rejected"):
            await source.fetch(FeedConfig("feed", "Feed", "https://example.invalid/feed"))


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
        source = RSSSource(client, max_attempts=3)
        with pytest.raises(SourceFetchError) as caught:
            await source.fetch(FeedConfig("source", "Source", "https://private.example.invalid"))

    assert attempts == 3
    assert sleep.await_count == 2
    sleep.assert_awaited_with(4.0)
    assert "private.example.invalid" not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_rss_local_offset_is_normalized_then_restored() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><guid>one</guid><title>Local publication</title>
    <link>https://example.invalid/one</link>
    <pubDate>Mon, 13 Jul 2026 11:03:56 +0800</pubDate>
    <description>body</description></item></channel></rss>"""

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=xml))) as client:
        articles = await RSSSource(client, max_attempts=1).fetch(
            FeedConfig("feed", "Feed", "https://example.invalid/feed")
        )

    assert articles[0].published_at.to_iso8601_string() == "2026-07-13T03:03:56Z"
    assert articles[0].published_at.in_timezone("Asia/Shanghai").to_iso8601_string() == "2026-07-13T11:03:56+08:00"


async def test_http_context_source_fetches_successfully() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="context data"))
    ) as client:
        result = await HTTPContextSource(client).fetch(
            ContextSourceConfig(id="ctx", name="Context", url="https://example.invalid/ctx")
        )

    assert isinstance(result, SourceDocument)
    assert result.id == "ctx"
    assert result.content == "context data"


async def test_http_context_source_raises_on_http_error() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(SourceFetchError, match="Context source"):
            await HTTPContextSource(client).fetch(
                ContextSourceConfig(id="ctx", name="Context", url="https://example.invalid/ctx")
            )
