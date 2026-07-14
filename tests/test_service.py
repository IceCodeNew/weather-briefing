import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pendulum
import pytest

from weather_briefing.llm import LLMError
from weather_briefing.models import (
    AirQualitySnapshot,
    Article,
    FeedConfig,
    RenderedMessage,
    ResolvedLocation,
    Warning,
    WeatherContextSnapshot,
)
from weather_briefing.publishers import DeliveryProvider
from weather_briefing.render import PlainTextRenderer
from weather_briefing.service import BriefingService
from weather_briefing.state import SQLiteStateStore
from weather_briefing.weather_context import WeatherContextError


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


class CanceledRSSSource:
    async def fetch(self, config: object) -> tuple[Article, ...]:
        raise asyncio.CancelledError


class MixedOutcomeRSSSource:
    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        if config.id.startswith("canceled-"):
            raise asyncio.CancelledError
        if config.id == "failing-feed":
            raise RuntimeError("feed unavailable")
        return ()


class FailingWeatherContextProvider:
    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        raise WeatherContextError("weather context unavailable")


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
            observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
            weather_forecast=("Rain later",),
            air_quality=AirQualitySnapshot(
                source_id="air-quality:test",
                source_name="Test air quality",
                source_url="https://example.invalid/air-quality",
                observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
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
            "advice": ([conclusion] if payload["mode"] == "daily" or self._include_hourly_advice else []),
            "disaster_tracking": [],
            "should_publish": self._should_publish,
        }


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[RenderedMessage, bool]] = []

    async def publish(self, message: RenderedMessage, *, single_message: bool = False) -> None:
        self.messages.append((message, single_message))


class FailOncePublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(self, message: RenderedMessage, *, single_message: bool = False) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("delivery unavailable")
        await super().publish(message, single_message=single_message)


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
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    weather_context = StaticWeatherContextProvider()
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)

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
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    article = Article(
        id="article-id",
        source_id="feed",
        source_name="Weather feed",
        title="Hourly update",
        url="https://example.invalid/hourly",
        published_at=pendulum.datetime(2026, 7, 12, 23, 30, tz="UTC"),
        content="New weather information",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Weather feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
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
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    llm = RecordingLLM(should_publish=False)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    weather_context = StaticWeatherContextProvider()
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

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


async def test_unchanged_active_warning_does_not_force_hourly_delivery(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    article = Article(
        "warning-source",
        "feed",
        "Weather feed",
        "Warning issued",
        "https://example.invalid/warning",
        now,
        "Heat warning remains active",
    )
    warning = Warning(
        "heat-warning",
        "Heat warning",
        "active",
        "No material change",
        (article.id,),
        now,
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )

    class UnchangedWarningLLM:
        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            return {
                "headline": "Warning unchanged",
                "overview": "No material change.",
                "conclusions": [],
                "active_warnings": [
                    {
                        "id": warning.id,
                        "title": warning.title,
                        "status": warning.status,
                        "detail": warning.detail,
                        "source_ids": list(warning.source_ids),
                    }
                ],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": False,
            }

    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    with SQLiteStateStore(tmp_path / "warning.sqlite3") as state:
        state.save_articles((article,), now)
        state.update_warnings((warning,), (), now, {article.id})
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            UnchangedWarningLLM(),
            delivery,
            delivery,
        )

        assert await service.run("hourly", now.add(hours=1)) is None
        assert state.active_warnings(now.add(hours=1), 12)[0].id == warning.id

    assert publisher.messages == []


async def test_unpublished_article_is_included_until_a_later_briefing_is_published(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    article = Article(
        id="deferred-article",
        source_id="feed",
        source_name="Weather feed",
        title="Minor update",
        url="https://example.invalid/minor",
        published_at=now,
        content="A small change that may become relevant later",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Weather feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )

    class PublishingOnSecondRunLLM:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.payloads.append(payload)
            sources = (
                *cast(list[dict[str, object]], payload["new_articles"]),
                *cast(list[dict[str, object]], payload["deferred_articles"]),
            )
            source_id = str(sources[0]["source_id"])
            return {
                "headline": "Accumulated update",
                "overview": "Changes are now worth sending.",
                "conclusions": [{"text": "Accumulated change", "source_ids": [source_id]}],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": len(self.payloads) == 2,
            }

    llm = PublishingOnSecondRunLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "deferred.sqlite3") as state:
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
        assert await service.run("hourly", now) is None
        assert state.pending_articles() == (article,)

        assert await service.run("hourly", now.add(hours=23)) is not None
        assert state.pending_articles() == ()
        assert state.known_article_ids((article.id,)) == {article.id}

    assert cast(list[dict[str, object]], llm.payloads[0]["new_articles"])[0]["source_id"] == article.id
    assert llm.payloads[0]["deferred_articles"] == []
    assert llm.payloads[1]["new_articles"] == []
    assert cast(list[dict[str, object]], llm.payloads[1]["deferred_articles"])[0]["source_id"] == article.id
    assert len(publisher.messages) == 1


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
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    ops_publisher = RecordingPublisher()
    ops_delivery = DeliveryProvider(PlainTextRenderer(), ops_publisher)

    with SQLiteStateStore(tmp_path / f"{kind}.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )
        with pytest.raises(LLMError, match="validation failed") as error:
            await service.run(kind, pendulum.datetime(2026, 7, 13, 9, tz=timezone))

    assert message in str(error.value.__cause__)
    assert publisher.messages == []


async def test_task_failure_alert_is_sent_only_on_first_consecutive_failure(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "failure.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            delivery,
            FailingWeatherContextProvider(),
        )
        # First failure: alert fires immediately
        with pytest.raises(WeatherContextError, match="weather context unavailable") as error:
            await service.run("hourly", now)
        assert error.value.__notes__ == ["Briefing run failed"]
        assert len(publisher.messages) == 1
        assert "任务执行失败" in publisher.messages[0][0].body

        # Second consecutive failure: no new alert
        with pytest.raises(WeatherContextError, match="weather context unavailable") as error:
            await service.run("hourly", now.add(hours=1))
        assert error.value.__notes__ == ["Briefing run failed"]
        assert len(publisher.messages) == 1


async def test_task_failure_alert_delivery_failure_is_retried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    ops_publisher = FailOncePublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "failure-alert.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            DeliveryProvider(PlainTextRenderer(), ops_publisher),
            FailingWeatherContextProvider(),
        )

        with (
            caplog.at_level("ERROR", logger="weather_briefing.service"),
            pytest.raises(WeatherContextError, match="weather context unavailable") as error,
        ):
            await service.run("hourly", now)

        with pytest.raises(WeatherContextError, match="weather context unavailable"):
            await service.run("hourly", now.add(hours=1))
        assert len(ops_publisher.messages) == 1
        assert "任务执行失败" in ops_publisher.messages[0][0].body

        with pytest.raises(WeatherContextError, match="weather context unavailable"):
            await service.run("hourly", now.add(hours=2))
        assert len(ops_publisher.messages) == 1

    assert error.value.__notes__ == ["Briefing run failed"]
    assert "Failed to publish or record briefing failure alert" in caplog.text


async def test_daily_briefing_publishes_verbatim_articles(tmp_path: Path, caplog) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    verbatim = Article(
        id="verbatim-id",
        source_id="feed",
        source_name="Feed",
        title="Forecast bulletin",
        url="https://example.invalid/v",
        published_at=now,
        content="Raw forecast",
        is_verbatim=True,
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with caplog.at_level("DEBUG"), SQLiteStateStore(tmp_path / "v.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, StaticRSSSource(verbatim)),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            delivery,
        )
        await service.run("daily", now)

    assert len(publisher.messages) == 2
    assert publisher.messages[1][0].body == "Forecast bulletin\n\nRaw forecast"
    assert publisher.messages[1][1] is False
    assert (
        "Publishing verbatim article: source=feed published_at=2026-07-13T08:00:00+08:00 content_characters=12"
    ) in caplog.text
    assert "Rendered verbatim message: visible_characters=31 payload_characters=31" in caplog.text
    assert "Verbatim article published: source=feed published_at=2026-07-13T08:00:00+08:00" in caplog.text


async def test_run_returns_none_when_no_content_and_no_warnings(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "empty.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
        )
        result = await service.run("hourly", now)

    assert result is None


async def test_stale_feed_triggers_ops_alert(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    yesterday = now.subtract(days=1)
    article = Article(
        id="article-id",
        source_id="feed",
        source_name="Feed",
        title="Old article",
        url="https://example.invalid/old",
        published_at=yesterday,
        content="content",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=1,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    llm = RecordingLLM()
    ops_publisher = RecordingPublisher()
    ops_delivery = DeliveryProvider(PlainTextRenderer(), ops_publisher)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "stale.sqlite3") as state:
        state.record_source_check("feed", yesterday, yesterday)
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, StaticRSSSource(article)),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            ops_delivery,
        )
        await service.run("hourly", now)

    assert len(ops_publisher.messages) >= 1
    assert "长时间无更新" in ops_publisher.messages[0][0].body


class FailingOnceLLM:
    def __init__(self) -> None:
        self.attempts = 0

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.attempts += 1
        if self.attempts == 1:
            return {
                "headline": "Briefing",
                "overview": "Overview",
                "conclusions": [{"text": "Claim", "source_ids": ["invented"]}],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
            }
        return {
            "headline": "Briefing",
            "overview": "Overview",
            "conclusions": [],
            "active_warnings": [],
            "resolved_warning_ids": [],
            "advice": [],
            "disaster_tracking": [],
        }


async def test_llm_retry_on_validation_failure(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    article = Article(
        id="article-id",
        source_id="feed",
        source_name="Feed",
        title="Article",
        url="https://example.invalid/a",
        published_at=now,
        content="content",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=2,
    )
    llm = FailingOnceLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "retry.sqlite3") as state:
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
        body = await service.run("hourly", now)

    assert body is not None
    assert llm.attempts == 2


async def test_briefing_exceeding_character_limit_is_rejected(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=10,
        llm_max_attempts=1,
    )

    class LongLLM:
        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            return {
                "headline": "A" * 100,
                "overview": "B" * 100,
                "conclusions": [],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
            }

    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "long.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, EmptyRSSSource()),
            cast(Any, EmptyContextSource()),
            LongLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        with pytest.raises(LLMError, match="validation failed"):
            await service.run("hourly", now)


async def test_is_forecast_article_returns_false_for_unknown_feed(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    article = Article(
        id="article-id",
        source_id="unknown-feed",
        source_name="Unknown",
        title="Some content",
        url="https://example.invalid/a",
        published_at=now.subtract(days=1),
        content="content",
    )
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("known-feed", "Known", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    llm = RecordingLLM()

    with SQLiteStateStore(tmp_path / "unknown.sqlite3") as state:
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
        body = await service.run("daily", now)

    assert body is None
    assert llm.payload is None
    assert publisher.messages == []


async def test_rss_failure_does_not_crash_daily_task_with_weather_context(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("failing-feed", "Failing", "https://example.invalid/feed"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)

    with SQLiteStateStore(tmp_path / "rss-fail.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, FailingRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        body = await service.run("daily", now)

    assert body is not None
    assert len(publisher.messages) == 1


async def test_rss_cancellation_aborts_task_without_recording_failure(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("canceled-feed", "Canceled", "https://example.invalid/feed"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=1,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "rss-canceled.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, CanceledRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(asyncio.CancelledError):
            await service.run("hourly", now)

        assert state.rss_sources_requiring_failure_alert(("canceled-feed",), 1) == []

    assert publisher.messages == []


async def test_rss_cancellation_records_other_completed_feed_results(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(
            FeedConfig("canceled-feed", "Canceled", "https://example.invalid/canceled"),
            FeedConfig("canceled-other", "Also canceled", "https://example.invalid/canceled-other"),
            FeedConfig("recovered-feed", "Recovered", "https://example.invalid/recovered"),
            FeedConfig("failing-feed", "Failing", "https://example.invalid/failing"),
        ),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=1,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "rss-canceled-results.sqlite3") as state:
        state.record_rss_fetch_failure("recovered-feed")
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, MixedOutcomeRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(asyncio.CancelledError):
            await service.run("hourly", now)

        assert state.rss_sources_requiring_failure_alert(("recovered-feed",), 1) == []
        assert state.rss_sources_requiring_failure_alert(("failing-feed",), 1) == ["failing-feed"]


async def test_rss_failure_alert_is_sent_after_threshold(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("fail-feed", "Failing", "https://example.invalid/feed"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=2,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    llm = RecordingLLM()
    ops_publisher = RecordingPublisher()
    ops_delivery = DeliveryProvider(PlainTextRenderer(), ops_publisher)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "rss-alert.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, FailingRSSSource()),
            cast(Any, EmptyContextSource()),
            llm,
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )
        # First failure: no alert yet (threshold is 2)
        await service.run("hourly", now)
        assert ops_publisher.messages == []

        # Second failure: alert should trigger
        await service.run("hourly", now.add(hours=1))
        assert len(ops_publisher.messages) == 1
        assert "持续获取失败" in ops_publisher.messages[0][0].body

        # Third failure: no new alert (already alerted)
        await service.run("hourly", now.add(hours=2))
        assert len(ops_publisher.messages) == 1


async def test_failed_rss_alert_delivery_is_retried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = SimpleNamespace(
        timezone=timezone,
        feeds=(FeedConfig("fail-feed", "Failing", "https://example.invalid/feed"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=1,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=1,
    )
    ops_publisher = FailOncePublisher()
    ops_delivery = DeliveryProvider(PlainTextRenderer(), ops_publisher)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "rss-alert-retry.sqlite3") as state:
        service = BriefingService(
            cast(Any, settings),
            _location(),
            state,
            cast(Any, FailingRSSSource()),
            cast(Any, EmptyContextSource()),
            RecordingLLM(),
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )

        with caplog.at_level("ERROR", logger="weather_briefing.service"):
            await service.run("hourly", now)
        assert state.rss_sources_requiring_failure_alert(("fail-feed",), 1) == ["fail-feed"]
        assert len(publisher.messages) == 1

        await service.run("hourly", now.add(hours=1))
        assert state.rss_sources_requiring_failure_alert(("fail-feed",), 1) == []

    assert "Failed to publish or record RSS health alert" in caplog.text
    assert any("持续获取失败" in message.body for message, _ in ops_publisher.messages)
