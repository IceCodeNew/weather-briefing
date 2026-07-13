from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from weather_briefing.llm import LLMError
from weather_briefing.models import (
    AirQualitySnapshot,
    Article,
    FeedConfig,
    RenderedMessage,
    ResolvedLocation,
    WeatherContextSnapshot,
)
from weather_briefing.publishers import DeliveryProvider
from weather_briefing.render import PlainTextRenderer
from weather_briefing.service import BriefingService
from weather_briefing.state import SQLiteStateStore


class EmptyRSSSource:
    async def fetch(self, config: object) -> tuple[object, ...]:
        raise AssertionError("No RSS feed should be requested in this test")


class StaticRSSSource:
    def __init__(self, article: Article) -> None:
        self._article = article

    async def fetch(self, config: object) -> tuple[Article, ...]:
        return (self._article,)


class FailingRSSSource:
    async def fetch(self, config: object) -> tuple[Article, ...]:
        raise RuntimeError("feed unavailable")


class EmptyContextSource:
    async def fetch(self, config: object) -> object:
        raise AssertionError("No context source should be requested in this test")


class StaticWeatherContextProvider:
    def __init__(self) -> None:
        self.coordinates: tuple[float, float] | None = None

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        self.coordinates = (latitude, longitude)
        return WeatherContextSnapshot(
            source_id="weather:test",
            source_name="Test weather",
            source_url="https://example.invalid/weather",
            observed_at="2026-07-13T08:00:00+00:00",
            weather_forecast=("Rain later",),
            air_quality=AirQualitySnapshot(
                source_id="air-quality:test",
                source_name="Test air quality",
                source_url="https://example.invalid/air-quality",
                observed_at="2026-07-13T08:00:00+00:00",
                aqi=42,
                aqi_display="42",
                aqi_standard="test-standard",
                pm25_aqi=21,
                pm25_concentration=12,
                pm25_unit="µg/m³",
                category="good",
                health_guidance="Normal activity is suitable.",
            ),
        )


class RecordingLLM:
    def __init__(
        self,
        *,
        should_publish: bool = True,
        include_hourly_advice: bool = False,
    ) -> None:
        self.payload: dict[str, object] | None = None
        self._should_publish = should_publish
        self._include_hourly_advice = include_hourly_advice

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        context = cast(list[dict[str, object]], payload["context_documents"])
        articles = cast(list[dict[str, object]], payload["new_articles"])
        source_id = str((context or articles)[0]["source_id"])
        conclusion = {
            "text": "AQI is 42 under test-standard.",
            "source_ids": [source_id],
        }
        return {
            "headline": "Daily briefing",
            "overview": "Air quality is good.",
            "conclusions": [conclusion],
            "active_warnings": [],
            "resolved_warning_ids": [],
            "advice": (
                [conclusion]
                if payload["mode"] == "daily" or self._include_hourly_advice
                else []
            ),
            "disaster_tracking": [],
            "should_publish": self._should_publish,
        }


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[RenderedMessage, bool]] = []

    async def publish(self, message: RenderedMessage, *, single_message: bool = False) -> None:
        self.messages.append((message, single_message))


def _location() -> ResolvedLocation:
    return ResolvedLocation(
        id="test",
        name="runtime-region",
        latitude=39.911389,
        longitude=116.380556,
        country_code="CN",
        administrative_area="Beijing",
        timezone="Asia/Shanghai",
        is_mainland_china=True,
    )


async def test_daily_briefing_uses_configured_coordinates_and_air_quality_context(
    tmp_path: Path,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        task_failure_threshold=3,
        rss_stale_hours=24,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    weather_context = StaticWeatherContextProvider()
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = datetime(2026, 7, 13, 8, tzinfo=timezone)

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
            weather_context,
        )
        body = await service.run("daily", now)

    assert weather_context.coordinates == (39.911389, 116.380556)
    assert llm.payload is not None
    context_documents = cast(list[dict[str, str]], llm.payload["context_documents"])
    assert len(context_documents) == 2
    air_document = next(item for item in context_documents if item["source_id"] == "air-quality:test")
    assert air_document["url"] == "https://example.invalid/air-quality"
    assert "AQI：42（标准：test-standard" in air_document["content"]
    assert "PM2.5 原始浓度：12 µg/m³" in air_document["content"]
    assert body is not None
    assert publisher.messages == [(RenderedMessage(body, len(body)), True)]


async def test_hourly_briefing_also_uses_the_llm_provider(tmp_path: Path) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 13, 9, tzinfo=timezone)
    article = Article(
        id="article-id",
        source_id="feed",
        source_name="Weather feed",
        title="Hourly update",
        url="https://example.invalid/hourly",
        published_at=datetime(2026, 7, 12, 23, 30, tzinfo=ZoneInfo("UTC")),
        content="New weather information",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Weather feed", "https://example.invalid/rss"),),
        context_sources=(),
        task_failure_threshold=3,
        rss_stale_hours=24,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, StaticRSSSource(article)),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
        )
        await service.run("hourly", now)

    assert llm.payload is not None
    assert llm.payload["mode"] == "hourly"
    assert cast(list[dict[str, object]], llm.payload["new_articles"])[0]["source_id"] == "article-id"
    assert len(publisher.messages) == 1


async def test_hourly_api_only_update_can_be_remembered_without_delivery(
    tmp_path: Path,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        task_failure_threshold=3,
        rss_stale_hours=24,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    llm = RecordingLLM(should_publish=False)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    weather_context = StaticWeatherContextProvider()
    now = datetime(2026, 7, 13, 9, tzinfo=timezone)

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
            weather_context,
        )
        result = await service.run("hourly", now)
        remembered = state.recent_context_documents(now, 1)

    assert result is None
    assert llm.payload is not None
    assert llm.payload["mode"] == "hourly"
    assert publisher.messages == []
    assert {document.id for document in remembered} == {
        "weather:test",
        "air-quality:test",
    }


@pytest.mark.parametrize(
    ("kind", "llm", "message"),
    (
        ("hourly", RecordingLLM(include_hourly_advice=True), "must not repeat"),
        ("daily", RecordingLLM(should_publish=False), "should_publish=true"),
    ),
)
async def test_service_rejects_mode_specific_llm_contract_violations(
    tmp_path: Path,
    kind: str,
    llm: RecordingLLM,
    message: str,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        task_failure_threshold=3,
        rss_stale_hours=24,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / f"{kind}.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        with pytest.raises(LLMError, match="validation failed") as error:
            await service.run(kind, datetime(2026, 7, 13, 9, tzinfo=timezone))

    assert message in str(error.value.__cause__)
    assert publisher.messages == []


async def test_failure_alert_is_sent_only_when_threshold_is_first_reached(
    tmp_path: Path,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/feed"),),
        context_sources=(),
        task_failure_threshold=3,
        rss_stale_hours=24,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = datetime(2026, 7, 13, 9, tzinfo=timezone)

    with SQLiteStateStore(tmp_path / "failure.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, FailingRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            delivery,
        )
        for attempt in range(4):
            with pytest.raises(RuntimeError, match="feed unavailable"):
                await service.run("hourly", now + timedelta(hours=attempt))

    assert len(publisher.messages) == 1
    assert "连续失败 3 次" in publisher.messages[0][0].body
