import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

import pendulum
import pytest

from weather_briefing.capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from weather_briefing.llm import LLMError, LLMRequestError
from weather_briefing.models import (
    AirQualitySnapshot,
    AirQualityTimeKind,
    Article,
    ContextSourceConfig,
    FeedConfig,
    RenderedMessage,
    ResolvedLocation,
    SourceDocument,
    Warning,
    WeatherContextSnapshot,
)
from weather_briefing.publishers import DeliveryError, DeliveryProvider
from weather_briefing.render import PlainTextRenderer
from weather_briefing.service import (
    BriefingService,
    _bounded_context_history,
    _context_history_candidates,
    _HistoricalContextOverflow,
    _serialize_context_document,
)
from weather_briefing.state import SQLiteStateStore
from weather_briefing.weather_context import WeatherContextError


@dataclass(frozen=True, slots=True)
class _TestSettings:
    timezone: pendulum.Timezone
    feeds: tuple[FeedConfig, ...] = ()
    context_sources: tuple[ContextSourceConfig, ...] = ()
    rss_stale_hours: int = 24
    rss_failure_threshold: int = 3
    warning_retention_hours: int = 12
    history_hours: int = 48
    llm_history_max_documents: int = 8
    llm_history_max_characters: int = 16_000
    briefing_max_characters: int = 3500
    llm_max_attempts: int = 3


def _is_dict_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(
        isinstance(item, dict) and all(isinstance(k, str) for k in item) for item in value
    )


class EmptyRSSSource:
    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        raise AssertionError("No RSS feed should be requested in this test")


class StaticRSSSource:
    def __init__(self, *articles: Article) -> None:
        self._articles = articles

    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        return self._articles


class FailingRSSSource:
    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
        raise RuntimeError("feed unavailable")


class CanceledRSSSource:
    async def fetch(self, config: FeedConfig) -> tuple[Article, ...]:
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
    async def fetch(self, config: ContextSourceConfig) -> SourceDocument:
        raise AssertionError("No context source should be requested in this test")


class StaticWeatherContextProvider:
    def __init__(self, *, allergen_advice_available: bool = False) -> None:
        self.coordinates: tuple[float, float] | None = None
        self._allergen_advice_available = allergen_advice_available

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:
        self.coordinates = (latitude, longitude)
        return WeatherContextSnapshot(
            source_id="weather:test",
            source_name="Test weather",
            source_url="https://example.invalid/weather",
            observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
            weather_forecast=("Rain later",),
            allergen_advice_available=self._allergen_advice_available,
            air_quality=AirQualitySnapshot(
                source_id="air-quality:test",
                source_name="Test air quality",
                source_url="https://example.invalid/air-quality",
                effective_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
                time_kind=AirQualityTimeKind.OBSERVATION,
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
        include_briefing_advice: bool = False,
    ) -> None:
        self.payload: dict[str, object] | None = None
        self._should_publish = should_publish
        self._include_briefing_advice = include_briefing_advice

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        source_documents: list[dict[str, object]] = []
        context_documents = payload["context_documents"]
        assert _is_dict_list(context_documents)
        source_documents.extend(context_documents)
        for key in ("new_articles", "deferred_articles"):
            group = payload[key]
            assert _is_dict_list(group)
            source_documents.extend(group)
        source_id = str(source_documents[0]["source_id"])
        conclusion = {
            "text": "AQI is 42 under test-standard.",
            "source_ids": [source_id],
        }
        required_topics = payload["required_advice_topics"]
        assert isinstance(required_topics, list)
        advice = [{"topic": topic, "text": conclusion["text"], "source_ids": [source_id]} for topic in required_topics]
        if self._include_briefing_advice and not advice:
            advice = [{"topic": "clothing", **conclusion}]
        return {
            "headline": "Daily briefing",
            "headline_source_ids": [source_id],
            "conclusions": [conclusion],
            "active_warnings": [],
            "resolved_warning_ids": [],
            "advice": advice,
            "disaster_tracking": [],
            "should_publish": self._should_publish,
        }


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[RenderedMessage, bool, bool]] = []

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        self.messages.append((message, single_message, silent))


class FailOncePublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("delivery unavailable")
        await super().publish(message, single_message=single_message, silent=silent)


class UnavailableChannelPublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        self.attempts += 1
        raise DeliveryError(
            "Telegram delivery failed (chat-not-found)",
            reason="chat-not-found",
            channel_unavailable=True,
        )


class FailingVerbatimPublisher(RecordingPublisher):
    def __init__(self, failed_attempts: set[int]) -> None:
        super().__init__()
        self._failed_attempts = failed_attempts
        self.verbatim_attempts: list[str] = []

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        if not single_message:
            self.verbatim_attempts.append(message.body)
            if len(self.verbatim_attempts) in self._failed_attempts:
                raise RuntimeError("verbatim delivery unavailable")
        await super().publish(message, single_message=single_message, silent=silent)


def _location(
    *,
    country_code: str | None = "CN",
    administrative_area: str | None = "Beijing",
    summary_language: str = "zh-CN",
) -> ResolvedLocation:
    return ResolvedLocation(
        id="test",
        name="runtime-region",
        latitude=39.911389,
        longitude=116.380556,
        country_code=country_code,
        administrative_area=administrative_area,
        timezone="Asia/Shanghai",
        is_mainland_china=True,
        summary_language=summary_language,
    )


def _context_document(
    source_id: str,
    content: str,
    *,
    history_summary: str | None = None,
    history_value: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        id=source_id,
        name=f"Source {source_id}",
        url=f"https://example.invalid/{source_id}",
        content=content,
        history_summary=history_summary,
        history_value=history_value,
    )


def test_context_history_keeps_latest_baselines_and_recent_changes() -> None:
    documents = (
        _context_document("weather", "weather baseline"),
        _context_document("weather", "weather baseline"),
        _context_document("weather", "weather changed"),
        _context_document("air", "air baseline"),
        _context_document("air", "air changed"),
        _context_document("weather", "weather latest"),
        _context_document("air", "air latest"),
    )

    selected = _context_history_candidates(documents, max_documents=6)

    assert [(candidate.document.id, candidate.document.content, candidate.role) for candidate in selected] == [
        ("air", "air latest", "latest"),
        ("weather", "weather latest", "latest"),
        ("air", "air baseline", "retention_baseline"),
        ("weather", "weather baseline", "retention_baseline"),
        ("air", "air changed", "recent_change"),
        ("weather", "weather changed", "recent_change"),
    ]


def test_context_history_keeps_summary_only_changes() -> None:
    documents = (
        _context_document("weather", "same content", history_summary="old summary"),
        _context_document("weather", "same content", history_summary="new summary"),
    )

    selected = _context_history_candidates(documents, max_documents=2)

    assert [(candidate.document.history_summary, candidate.role) for candidate in selected] == [
        ("new summary", "latest"),
        ("old summary", "retention_baseline"),
    ]


def test_context_history_collapses_volatile_rendering_with_same_semantic_value() -> None:
    documents = (
        _context_document("weather", "更新时间：08:00\nRain", history_value="Rain"),
        _context_document("weather", "更新时间：09:00\nRain", history_value="Rain"),
    )

    selected = _context_history_candidates(documents, max_documents=2)

    assert [(candidate.document.content, candidate.role) for candidate in selected] == [
        ("更新时间：08:00\nRain", "latest")
    ]


def test_context_history_enforces_document_and_serialized_character_limits() -> None:
    oversized = _context_document("oversized", "private-history" * 20_000)
    compact = _context_document("compact", "current")
    compact_history = _bounded_context_history(
        (compact,),
        max_documents=1,
        max_characters=1_000,
    )
    character_limit = compact_history.serialized_characters

    bounded_history = _bounded_context_history(
        (compact, oversized),
        max_documents=2,
        max_characters=character_limit,
    )

    assert [item["source_id"] for item in bounded_history.payload] == ["compact"]
    assert bounded_history.payload[0]["history_role"] == "latest"
    assert len(bounded_history.payload) <= 2
    assert bounded_history.serialized_characters == len(
        json.dumps(bounded_history.payload, ensure_ascii=False, separators=(",", ":"))
    )
    assert bounded_history.serialized_characters <= character_limit
    assert compact_history.overflows == ()
    assert [(overflow.source_id, overflow.role) for overflow in bounded_history.overflows] == [("oversized", "latest")]
    expected = hashlib.sha256()
    for value in (
        "latest",
        oversized.name,
        oversized.url,
        oversized.language,
        oversized.content,
        "",
    ):
        encoded = value.encode()
        expected.update(len(encoded).to_bytes(8, byteorder="big"))
        expected.update(encoded)
    assert bounded_history.overflows[0].fingerprint == expected.hexdigest()


def test_context_history_overflow_fingerprint_has_unambiguous_field_boundaries() -> None:
    first = _context_document("weather", "content\0summary", history_summary="tail")
    second = _context_document("weather", "content", history_summary="summary\0tail")

    first_history = _bounded_context_history((first,), max_documents=1, max_characters=len("[]"))
    second_history = _bounded_context_history((second,), max_documents=1, max_characters=len("[]"))

    assert first_history.overflows[0].fingerprint != second_history.overflows[0].fingerprint


def test_context_history_overflow_fingerprint_uses_semantic_value() -> None:
    first = _context_document("weather", "更新时间：08:00\nRain", history_value="Rain")
    same_value = _context_document("weather", "更新时间：09:00\nRain", history_value="Rain")
    changed_value = _context_document("weather", "更新时间：09:00\nStorm", history_value="Storm")

    fingerprints = [
        _bounded_context_history((document,), max_documents=1, max_characters=len("[]")).overflows[0].fingerprint
        for document in (first, same_value, changed_value)
    ]

    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[0] != fingerprints[2]


def test_context_history_uses_deterministic_summary_before_skipping_mandatory_document() -> None:
    document = _context_document("weather", "full private history" * 100, history_summary="weather summary")
    summary_entry = {
        "source_id": "weather",
        "name": "Source weather",
        "url": "https://example.invalid/weather",
        "language": "zh-CN",
        "content": "weather summary",
        "history_role": "latest",
        "content_compacted": True,
        "original_content_characters": len(document.content),
    }
    character_limit = len(json.dumps([summary_entry], ensure_ascii=False, separators=(",", ":")))

    bounded_history = _bounded_context_history(
        (document,),
        max_documents=1,
        max_characters=character_limit,
    )

    assert bounded_history.payload == [summary_entry]
    assert [document.id for document in bounded_history.documents] == ["weather"]
    assert bounded_history.serialized_characters == character_limit
    assert bounded_history.overflows == ()


def test_context_history_repacks_selected_entries_before_skipping_mandatory_document() -> None:
    later = _context_document("later", "later full history" * 100, history_summary="later summary")
    earlier = _context_document("earlier", "earlier full history" * 100, history_summary="earlier summary")
    expected = [
        _serialize_context_document(earlier, history_role="latest", compact=True),
        _serialize_context_document(later, history_role="latest", compact=True),
    ]
    earlier_full = _serialize_context_document(earlier, history_role="latest")
    character_limit = len(json.dumps([earlier_full], ensure_ascii=False, separators=(",", ":")))

    bounded_history = _bounded_context_history(
        (later, earlier),
        max_documents=2,
        max_characters=character_limit,
    )

    assert bounded_history.payload == expected
    assert bounded_history.serialized_characters == len(json.dumps(expected, ensure_ascii=False, separators=(",", ":")))
    assert bounded_history.overflows == ()


def test_context_history_repacks_multiple_entries_when_one_summary_is_insufficient() -> None:
    third = _context_document("third", "z" * 1_000, history_summary="z" * 20)
    second = _context_document("second", "y" * 500, history_summary="y" * 300)
    first = _context_document("first", "x" * 500, history_summary="x" * 300)
    first_full = _serialize_context_document(first, history_role="latest")
    second_full = _serialize_context_document(second, history_role="latest")
    character_limit = len(json.dumps([first_full, second_full], ensure_ascii=False, separators=(",", ":")))

    bounded_history = _bounded_context_history(
        (third, second, first),
        max_documents=3,
        max_characters=character_limit,
    )

    assert [entry.get("content_compacted", False) for entry in bounded_history.payload] == [True, True, True]
    assert bounded_history.overflows == ()


def test_context_history_ignores_unhelpful_prior_summaries_when_repacking() -> None:
    overflowing = _context_document("overflowing", "z" * 1_000)
    unhelpful = _context_document("unhelpful", "short", history_summary="long summary" * 100)
    plain = _context_document("plain", "plain")
    plain_entry = _serialize_context_document(plain, history_role="latest")
    unhelpful_entry = _serialize_context_document(unhelpful, history_role="latest")
    character_limit = len(json.dumps([plain_entry, unhelpful_entry], ensure_ascii=False, separators=(",", ":")))

    bounded_history = _bounded_context_history(
        (overflowing, unhelpful, plain),
        max_documents=3,
        max_characters=character_limit,
    )

    assert bounded_history.payload == [plain_entry, unhelpful_entry]
    assert [(overflow.source_id, overflow.role) for overflow in bounded_history.overflows] == [
        ("overflowing", "latest")
    ]


def test_context_history_reports_mandatory_documents_excluded_by_count_limit() -> None:
    documents = (
        _context_document("weather", "weather baseline"),
        _context_document("air", "air baseline"),
        _context_document("weather", "weather latest"),
        _context_document("air", "air latest"),
    )

    bounded_history = _bounded_context_history(
        documents,
        max_documents=3,
        max_characters=10_000,
    )

    assert [(item["source_id"], item["history_role"]) for item in bounded_history.payload] == [
        ("air", "latest"),
        ("weather", "latest"),
        ("air", "retention_baseline"),
    ]
    assert [(overflow.source_id, overflow.role) for overflow in bounded_history.overflows] == [
        ("weather", "retention_baseline")
    ]


def test_context_history_reports_mandatory_overflow_but_silently_drops_recent_change() -> None:
    documents = tuple(
        _context_document("weather", content, history_summary="summary" * 100)
        for content in ("baseline", "recent", "latest")
    )

    bounded_history = _bounded_context_history(
        documents,
        max_documents=3,
        max_characters=len("[]"),
    )

    assert bounded_history.payload == []
    assert bounded_history.documents == ()
    assert bounded_history.serialized_characters == len("[]")
    assert [overflow.role for overflow in bounded_history.overflows] == ["latest", "retention_baseline"]


async def test_bounded_history_excludes_omitted_sources_from_citation_validation(tmp_path: Path) -> None:
    class OmittedHistoryCitationLLM:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.payload = payload
            return {
                "headline": "Invalid historical citation",
                "headline_source_ids": ["omitted-history"],
                "conclusions": [{"text": "Invalid claim", "source_ids": ["omitted-history"]}],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": True,
            }

    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
        timezone=timezone,
        llm_history_max_documents=1,
        llm_max_attempts=1,
    )
    llm = OmittedHistoryCitationLLM()
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)

    with SQLiteStateStore(tmp_path / "bounded-citations.sqlite3") as state:
        state.save_context_documents(
            (_context_document("omitted-history", "older history"),),
            now.subtract(hours=2),
        )
        state.save_context_documents(
            (_context_document("selected-history", "newer history"),),
            now.subtract(hours=1),
        )
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(LLMError, match="validation failed") as error:
            await service.run("forecast", now)

    assert "unknown source IDs: ['omitted-history']" in str(error.value.__cause__)
    assert llm.payload is not None
    recent_context = llm.payload["recent_context_documents"]
    assert _is_dict_list(recent_context)
    assert [item["source_id"] for item in recent_context] == ["selected-history"]


async def test_context_budget_alert_is_deduplicated_until_recovery(tmp_path: Path) -> None:
    settings = _TestSettings(timezone=pendulum.timezone("Asia/Shanghai"))
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=settings.timezone)
    overflow = _HistoricalContextOverflow("private-source", "latest", "fingerprint")

    with SQLiteStateStore(tmp_path / "context-budget.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
        )
        await service._publish_context_budget_alert((overflow,), now)
        await service._publish_context_budget_alert((overflow,), now.add(hours=1))
        await service._publish_context_budget_alert((), now.add(hours=2))
        await service._publish_context_budget_alert((overflow,), now.add(hours=3))

    assert len(publisher.messages) == 2
    assert all("private-source" in message.body for message, _, _ in publisher.messages)


async def test_context_budget_alert_delivery_failure_is_retried(tmp_path: Path, caplog) -> None:
    settings = _TestSettings(timezone=pendulum.timezone("Asia/Shanghai"))
    publisher = FailOncePublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 9, tz=settings.timezone)
    overflow = _HistoricalContextOverflow("private-source", "latest", "fingerprint")

    with SQLiteStateStore(tmp_path / "context-budget-retry.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
        )
        with caplog.at_level(logging.ERROR):
            await service._publish_context_budget_alert((overflow,), now)
        await service._publish_context_budget_alert((overflow,), now.add(hours=1))

    assert publisher.attempts == 2
    assert len(publisher.messages) == 1
    assert "Failed to publish or record context budget alert" in caplog.text


@pytest.mark.parametrize(
    ("kind", "historical_ids"),
    (("briefing", ["historical-verbatim"]), ("forecast", ["historical-verbatim", "historical-summary"])),
)
def test_build_payload_serializes_article_groups_consistently(
    tmp_path: Path,
    kind: str,
    historical_ids: list[str],
) -> None:
    now = pendulum.datetime(2026, 7, 13, 8, tz="Asia/Shanghai")

    def article(identifier: str, *, is_verbatim: bool = False) -> Article:
        return Article(
            id=identifier,
            source_id="feed",
            source_name=f"Publisher {identifier}",
            title=f"Title {identifier}",
            url=f"https://example.invalid/{identifier}",
            published_at=now.add(minutes=len(identifier)),
            content=f"Content {identifier}",
            is_verbatim=is_verbatim,
        )

    def expected(item: Article) -> dict[str, object]:
        return {
            "source_id": item.id,
            "publisher": item.source_name,
            "title": item.title,
            "url": item.url,
            "published_at": item.published_at.isoformat(),
            "content": item.content,
            "verbatim": item.is_verbatim,
        }

    new = article("new")
    deferred = article("deferred")
    historical_verbatim = article("historical-verbatim", is_verbatim=True)
    historical_summary = article("historical-summary")
    historical = (historical_verbatim, historical_summary)

    with SQLiteStateStore(tmp_path / f"{kind}.sqlite3") as state:
        service = object.__new__(BriefingService)
        service._settings = _TestSettings(timezone=pendulum.timezone("Asia/Shanghai"))
        service._location = _location()
        service._state = state
        payload = service._build_payload(
            kind,
            now,
            None,
            (new,),
            (deferred,),
            historical,
            (),
            (),
            (),
        )

    assert payload["new_articles"] == [expected(new)]
    assert payload["deferred_articles"] == [expected(deferred)]
    assert payload["historical_articles"] == [expected(item) for item in historical if item.id in historical_ids]


@pytest.mark.parametrize(
    ("location", "expected_scope"),
    (
        (
            _location(),
            {
                "full_name": "runtime-region",
                "administrative_area": "Beijing",
                "country_code": "CN",
            },
        ),
        (
            _location(country_code=None, administrative_area=None),
            {"full_name": "runtime-region"},
        ),
        (
            _location(summary_language="en"),
            {
                "full_name": "runtime-region",
                "administrative_area": "Beijing",
                "country_code": "CN",
            },
        ),
    ),
)
async def test_forecast_uses_configured_coordinates_and_air_quality_context(
    tmp_path: Path,
    location: ResolvedLocation,
    expected_scope: dict[str, str],
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
    weather_provider = CapabilityProviderSet(
        weather=weather_context,
        weather_metadata=ProviderCapabilities(
            provider_id="test",
            provider_name="Test weather",
            capabilities=frozenset({CapabilityName.WEATHER, CapabilityName.AIR_QUALITY}),
        ),
    )
    llm = RecordingLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        state.save_briefing("briefing", "Earlier update", now.subtract(hours=1))
        service = BriefingService(
            settings,
            location,
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
            weather_provider,
        )
        body = await service.run("forecast", now)

    assert weather_context.coordinates == (39.911389, 116.380556)
    assert llm.payload is not None
    context_documents = llm.payload["context_documents"]
    assert _is_dict_list(context_documents)
    assert len(context_documents) == 2
    air_document = next(item for item in context_documents if item["source_id"] == "air-quality:test")
    assert air_document["url"] == "https://example.invalid/air-quality"
    content = air_document["content"]
    assert isinstance(content, str)
    assert "AQI：42（标准：test-standard" in content
    assert "PM2.5 12 µg/m³" in content
    assert "原始浓度" not in content
    assert llm.payload["location_scope"] == expected_scope
    assert llm.payload["output_language"] == location.summary_language
    assert "coordinates" not in llm.payload
    assert "location_id" not in llm.payload
    assert llm.payload["required_advice_topics"] == [
        "clothing",
        "dehumidification",
        "exercise",
        "mask",
    ]
    recent_briefings = llm.payload["recent_briefings"]
    assert _is_dict_list(recent_briefings)
    assert recent_briefings == [
        {
            "mode": "briefing",
            "published_at": "2026-07-13T07:00:00+08:00",
            "body": "Earlier update",
        }
    ]
    assert body is not None
    if location.summary_language == "en":
        assert "Weather information" in body
        assert "天气信息" not in body
    assert publisher.messages == [(RenderedMessage(body, len(body)), True, False)]


async def test_forecast_rejects_missing_allergen_advice_when_input_contains_it(tmp_path: Path) -> None:
    class MissingAllergenAdviceLLM(RecordingLLM):
        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            result = await super().summarize(system_prompt, payload)
            advice = result["advice"]
            assert _is_dict_list(advice)
            result["advice"] = [item for item in advice if item["topic"] != "allergen"]
            return result

    settings = _TestSettings(
        timezone=pendulum.timezone("Asia/Shanghai"),
        feeds=(),
        context_sources=(),
        llm_max_attempts=1,
    )
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    now = pendulum.datetime(2026, 7, 13, 8, tz=settings.timezone)

    with SQLiteStateStore(tmp_path / "missing-allergen.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            MissingAllergenAdviceLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(allergen_advice_available=True),
        )
        with pytest.raises(LLMError, match="validation failed") as error:
            await service.run("forecast", now)

    assert "missing required topics: allergen" in str(error.value.__cause__)


async def test_forecast_rejects_allergen_advice_without_allergen_source(tmp_path: Path) -> None:
    class WrongAllergenSourceLLM(RecordingLLM):
        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            result = await super().summarize(system_prompt, payload)
            advice = result["advice"]
            assert _is_dict_list(advice)
            allergen_advice = next(item for item in advice if item["topic"] == "allergen")
            allergen_advice["source_ids"] = ["air-quality:test"]
            return result

    settings = _TestSettings(
        timezone=pendulum.timezone("Asia/Shanghai"),
        feeds=(),
        context_sources=(),
        llm_max_attempts=1,
    )
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    now = pendulum.datetime(2026, 7, 13, 8, tz=settings.timezone)

    with SQLiteStateStore(tmp_path / "wrong-allergen-source.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            WrongAllergenSourceLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(allergen_advice_available=True),
        )
        with pytest.raises(LLMError, match="validation failed") as error:
            await service.run("forecast", now)

    assert "must cite a current allergen-capable source" in str(error.value.__cause__)


async def test_forecast_date_is_separate_from_run_time_and_reaches_weather_provider(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    run_time = pendulum.datetime(2026, 7, 13, 22, 30, tz=timezone)
    target_date = pendulum.date(2026, 7, 15)
    settings = _TestSettings(
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

    class TargetDateWeatherProvider:
        def __init__(self) -> None:
            self.forecast_date: pendulum.Date | None = None

        async def fetch(
            self,
            latitude: float,
            longitude: float,
        ) -> WeatherContextSnapshot:
            return await StaticWeatherContextProvider().fetch(latitude, longitude)

        async def fetch_for_date(
            self,
            latitude: float,
            longitude: float,
            forecast_date: pendulum.Date,
        ) -> WeatherContextSnapshot:
            self.forecast_date = forecast_date
            return await self.fetch(latitude, longitude)

    weather_provider = TargetDateWeatherProvider()
    llm = RecordingLLM()
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    with SQLiteStateStore(tmp_path / "future-forecast.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
            weather_provider,
        )
        await service.run("forecast", run_time, forecast_date=target_date)

    assert weather_provider.forecast_date == target_date
    assert llm.payload is not None
    assert llm.payload["now"] == "2026-07-13T22:30:00+08:00"
    assert llm.payload["forecast_date"] == "2026-07-15"


async def test_forecast_date_is_rejected_for_briefing_mode() -> None:
    service = object.__new__(BriefingService)
    service._settings = _TestSettings(timezone=pendulum.timezone("Asia/Shanghai"))

    with pytest.raises(ValueError, match="only supported in forecast mode"):
        await service.run("briefing", forecast_date=pendulum.date(2026, 7, 15))


async def test_briefing_also_uses_the_llm_provider(tmp_path: Path) -> None:
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
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        await service.run("briefing", now)

    assert llm.payload is not None
    assert llm.payload["mode"] == "briefing"
    new_articles = llm.payload["new_articles"]
    assert _is_dict_list(new_articles)
    assert new_articles[0]["source_id"] == "article-id"
    assert len(publisher.messages) == 1


async def test_briefing_api_only_update_can_be_remembered_without_delivery(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
        state.save_context_documents(
            (_context_document("weather:test", "sensitive historical body"),),
            now.subtract(hours=1),
        )
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
            weather_context,
        )
        with caplog.at_level(logging.DEBUG, logger="weather_briefing.service"):
            result = await service.run("briefing", now)
        remembered = state.recent_context_documents(now, 1)

    assert result is None
    assert llm.payload is not None
    assert llm.payload["mode"] == "briefing"
    assert publisher.messages == []
    assert "input_documents=1 selected_documents=1 serialized_characters=" in caplog.text
    assert "LLM payload prepared: serialized_characters=" in caplog.text
    assert "sensitive historical body" not in caplog.text
    assert {document.id for document in remembered} == {
        "weather:test",
        "air-quality:test",
    }


async def test_unchanged_active_warning_does_not_force_briefing_delivery(tmp_path: Path) -> None:
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
    settings = _TestSettings(
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
                "headline_source_ids": list(warning.source_ids),
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
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            UnchangedWarningLLM(),
            delivery,
            delivery,
        )

        assert await service.run("briefing", now.add(hours=1)) is None
        assert state.active_warnings(now.add(hours=1), 12)[0].id == warning.id

    assert publisher.messages == []


async def test_unknown_resolved_warning_id_is_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    article = Article(
        "warning-article",
        "feed",
        "Weather feed",
        "Warning issued",
        "https://example.invalid/warning",
        now,
        "Heat warning is active",
    )
    warning = Warning(
        "heat-warning",
        "Heat warning",
        "active",
        "Heat warning is active",
        (article.id,),
        now,
    )
    settings = _TestSettings(timezone=timezone, llm_max_attempts=2)

    class ResolvingWarningLLM:
        def __init__(self) -> None:
            self.attempts = 0

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.attempts += 1
            assert "input.allowed_resolved_warning_ids" in system_prompt
            assert payload["allowed_resolved_warning_ids"] == [warning.id]
            return {
                "headline": "Warning update",
                "headline_source_ids": [article.id],
                "conclusions": [],
                "active_warnings": [],
                "resolved_warning_ids": ["invented-warning", "invented-warning", warning.id],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": False,
            }

    llm = ResolvingWarningLLM()
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    with SQLiteStateStore(tmp_path / "warning.sqlite3") as state:
        state.save_articles((article,), now)
        state.update_warnings((warning,), (), now, {article.id})
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )

        with caplog.at_level("WARNING", logger="weather_briefing.service"):
            assert await service.run("briefing", now.add(hours=1)) is None
        assert state.active_warnings(now.add(hours=1), 12) == ()

    assert llm.attempts == 1
    assert publisher.messages == []
    assert "Ignoring 1 distinct resolved warning ID(s) that are not currently active" in caplog.text


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
    settings = _TestSettings(
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
            sources: list[dict[str, object]] = []
            for key in ("new_articles", "deferred_articles"):
                group = payload[key]
                assert _is_dict_list(group)
                sources.extend(group)
            source_id = str(sources[0]["source_id"])
            return {
                "headline": "Accumulated update",
                "headline_source_ids": [source_id],
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
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        assert await service.run("briefing", now) is None
        assert state.pending_articles() == (article,)

        assert await service.run("briefing", now.add(hours=23)) is not None
        assert state.pending_articles() == ()
        assert state.known_article_ids((article.id,)) == {article.id}

    first_new = llm.payloads[0]["new_articles"]
    assert _is_dict_list(first_new)
    assert first_new[0]["source_id"] == article.id
    assert llm.payloads[0]["deferred_articles"] == []
    assert llm.payloads[1]["new_articles"] == []
    second_deferred = llm.payloads[1]["deferred_articles"]
    assert _is_dict_list(second_deferred)
    assert second_deferred[0]["source_id"] == article.id
    assert len(publisher.messages) == 1


async def test_forced_briefing_publishes_deferred_information_and_clears_pending_state(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 15, tz=timezone)
    article = Article(
        id="deferred-temperature",
        source_id="feed",
        source_name="Weather feed",
        title="Afternoon temperature",
        url="https://example.invalid/temperature",
        published_at=now,
        content="The temperature was 31 C at 15:00",
        is_verbatim=True,
    )
    settings = _TestSettings(
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
    llm = RecordingLLM(should_publish=False)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "forced.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        assert await service.run("briefing", now) is None
        assert state.pending_articles() == (article,)

        body = await service.run(
            "briefing",
            now.add(hours=7),
            force_publish=True,
            silent=True,
        )

        assert body is not None
        assert state.pending_articles() == ()

    assert llm.payload is not None
    deferred = llm.payload["deferred_articles"]
    assert _is_dict_list(deferred)
    assert deferred[0]["source_id"] == article.id
    assert publisher.messages[0] == (RenderedMessage(body, len(body)), True, True)
    assert len(publisher.messages) == 2
    assert publisher.messages[1][1:] == (False, True)


async def test_final_window_keeps_worthy_briefing_notifications_enabled(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 23, tz=timezone)
    settings = _TestSettings(
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

    with SQLiteStateStore(tmp_path / "worthy-final.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(should_publish=True),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        body = await service.run(
            "briefing",
            now,
            force_publish=True,
            silent=True,
        )

    assert body is not None
    assert publisher.messages == [(RenderedMessage(body, len(body)), True, False)]


@pytest.mark.parametrize(
    ("kind", "llm", "message"),
    (
        ("briefing", RecordingLLM(include_briefing_advice=True), "must not repeat"),
        ("forecast", RecordingLLM(should_publish=False), "should_publish=true"),
    ),
)
async def test_service_rejects_mode_specific_llm_contract_violations(
    tmp_path: Path,
    kind: str,
    llm: RecordingLLM,
    message: str,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
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
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
            FailingWeatherContextProvider(),
        )
        # First failure: alert fires immediately
        with pytest.raises(WeatherContextError, match="weather context unavailable") as error:
            await service.run("briefing", now)
        assert error.value.__notes__ == ["Briefing run failed"]
        assert len(publisher.messages) == 1
        assert "任务执行失败" in publisher.messages[0][0].body

        # Second consecutive failure: no new alert
        with pytest.raises(WeatherContextError, match="weather context unavailable") as error:
            await service.run("briefing", now.add(hours=1))
        assert error.value.__notes__ == ["Briefing run failed"]
        assert len(publisher.messages) == 1


async def test_task_failure_alert_delivery_failure_is_retried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            DeliveryProvider(PlainTextRenderer(), ops_publisher),
            FailingWeatherContextProvider(),
        )

        with (
            caplog.at_level("ERROR", logger="weather_briefing.service"),
            pytest.raises(WeatherContextError, match="weather context unavailable") as error,
        ):
            await service.run("briefing", now)

        with pytest.raises(WeatherContextError, match="weather context unavailable"):
            await service.run("briefing", now.add(hours=1))
        assert len(ops_publisher.messages) == 1
        assert "任务执行失败" in ops_publisher.messages[0][0].body

        with pytest.raises(WeatherContextError, match="weather context unavailable"):
            await service.run("briefing", now.add(hours=2))
        assert len(ops_publisher.messages) == 1

    assert error.value.__notes__ == ["Briefing run failed"]
    assert "Failed to publish or record briefing failure alert" in caplog.text


async def test_task_failure_alert_skips_unavailable_shared_delivery_channel(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(timezone=timezone, llm_max_attempts=1)
    publisher = UnavailableChannelPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "unavailable-delivery.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        with (
            caplog.at_level("INFO", logger="weather_briefing.service"),
            pytest.raises(DeliveryError, match="chat-not-found") as caught,
        ):
            await service.run("briefing", pendulum.datetime(2026, 7, 13, 9, tz=timezone))

    assert caught.value.reason == "chat-not-found"
    assert publisher.attempts == 1
    assert "Failure alert skipped reason=delivery-channel-unavailable original_reason=chat-not-found" in caplog.text


async def test_failure_recording_error_does_not_mask_task_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(timezone=timezone, llm_max_attempts=1)
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    def fail_to_record_failure(state: SQLiteStateStore) -> int:
        raise RuntimeError("private database path")

    monkeypatch.setattr(SQLiteStateStore, "record_failure", fail_to_record_failure)
    with SQLiteStateStore(tmp_path / "failure-recording.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
            FailingWeatherContextProvider(),
        )
        with (
            caplog.at_level("ERROR", logger="weather_briefing.service"),
            pytest.raises(WeatherContextError, match="weather context unavailable") as error,
        ):
            await service.run("briefing", pendulum.datetime(2026, 7, 13, 9, tz=timezone))

    assert error.value.__notes__ == ["Briefing run failed", "Failure state could not be recorded"]
    assert "Failed to record briefing failure: RuntimeError" in caplog.text
    assert "private database path" not in caplog.text


async def test_forecast_publishes_verbatim_articles(tmp_path: Path, caplog) -> None:
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
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            StaticRSSSource(verbatim),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
        )
        await service.run("forecast", now)

    assert len(publisher.messages) == 2
    assert publisher.messages[1][0].body == "Forecast bulletin\n\nRaw forecast"
    assert publisher.messages[1][1] is False
    assert (
        "Publishing verbatim article: source=feed published_at=2026-07-13T00:00:00+00:00 content_characters=12"
    ) in caplog.text
    assert "Rendered verbatim message: visible_characters=31 payload_characters=31" in caplog.text
    assert "Verbatim article published: source=feed published_at=2026-07-13T00:00:00+00:00" in caplog.text


async def test_failed_first_verbatim_is_retried_without_republishing_briefing(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    verbatim = Article(
        "verbatim",
        "feed",
        "Feed",
        "Forecast bulletin",
        "https://example.invalid/verbatim",
        now,
        "Raw forecast",
        is_verbatim=True,
    )
    settings = _TestSettings(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        llm_max_attempts=1,
    )
    publisher = FailingVerbatimPublisher({1})
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    ops_delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    with SQLiteStateStore(tmp_path / "state.db") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(verbatim),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            ops_delivery,
        )

        with pytest.raises(RuntimeError, match="verbatim delivery unavailable"):
            await service.run("forecast", now)

        assert len(state.recent_briefings(now, 1)) == 1
        assert tuple(item.article for item in state.pending_verbatim_deliveries()) == (verbatim,)

        assert await service.run("forecast", now.add(hours=1)) is None

        assert state.pending_verbatim_deliveries() == ()
        assert len(state.recent_briefings(now.add(hours=1), 2)) == 1

    assert publisher.verbatim_attempts == ["Forecast bulletin\n\nRaw forecast"] * 2
    assert sum(single_message for _, single_message, _ in publisher.messages) == 1


async def test_failed_later_verbatim_retries_only_unacknowledged_item(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    first = Article(
        "first",
        "feed",
        "Feed",
        "First bulletin",
        "https://example.invalid/first",
        now,
        "First body",
        is_verbatim=True,
    )
    second = Article(
        "second",
        "feed",
        "Feed",
        "Second bulletin",
        "https://example.invalid/second",
        now.add(minutes=1),
        "Second body",
        is_verbatim=True,
    )
    settings = _TestSettings(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        llm_max_attempts=1,
    )
    publisher = FailingVerbatimPublisher({2})
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    ops_delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    with SQLiteStateStore(tmp_path / "state.db") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(first, second),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            ops_delivery,
        )

        with pytest.raises(RuntimeError, match="verbatim delivery unavailable"):
            await service.run("forecast", now)

        assert tuple(item.article for item in state.pending_verbatim_deliveries()) == (second,)

        assert await service.run("forecast", now.add(hours=1)) is None

        assert state.pending_verbatim_deliveries() == ()

    assert publisher.verbatim_attempts == [
        "First bulletin\n\nFirst body",
        "Second bulletin\n\nSecond body",
        "Second bulletin\n\nSecond body",
    ]
    assert sum(single_message for _, single_message, _ in publisher.messages) == 1


async def test_verbatim_acknowledgement_failure_keeps_at_least_once_retry(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    verbatim = Article(
        "verbatim",
        "feed",
        "Feed",
        "Forecast bulletin",
        "https://example.invalid/verbatim",
        now,
        "Raw forecast",
        is_verbatim=True,
    )
    settings = _TestSettings(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    ops_delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    state_path = tmp_path / "state.db"
    with SQLiteStateStore(state_path) as state:
        with closing(sqlite3.connect(state_path)) as connection:
            connection.executescript(
                """CREATE TRIGGER abort_verbatim_ack BEFORE DELETE ON verbatim_delivery_queue
                BEGIN SELECT RAISE(ABORT, 'acknowledgement unavailable'); END;"""
            )
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(verbatim),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            ops_delivery,
        )
        with pytest.raises(sqlite3.IntegrityError, match="acknowledgement unavailable"):
            await service.run("forecast", now)

        assert tuple(item.article for item in state.pending_verbatim_deliveries()) == (verbatim,)

        with closing(sqlite3.connect(state_path)) as connection:
            connection.execute("DROP TRIGGER abort_verbatim_ack")
            connection.commit()
        assert await service.run("forecast", now.add(hours=1)) is None

        assert state.pending_verbatim_deliveries() == ()

    assert sum(single_message for _, single_message, _ in publisher.messages) == 1
    assert sum(not single_message for _, single_message, _ in publisher.messages) == 2


async def test_failed_main_checkpoint_leaves_no_partial_result_state(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 8, tz=timezone)
    article = Article(
        "article",
        "feed",
        "Feed",
        "Forecast bulletin",
        "https://example.invalid/article",
        now,
        "Raw forecast",
        is_verbatim=True,
    )
    settings = _TestSettings(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        llm_max_attempts=1,
    )
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)
    ops_delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())
    state_path = tmp_path / "state.db"

    with SQLiteStateStore(state_path) as state:
        with closing(sqlite3.connect(state_path)) as connection:
            connection.executescript(
                """CREATE TRIGGER abort_briefing_insert BEFORE INSERT ON briefings
                BEGIN SELECT RAISE(ABORT, 'briefing insert failed'); END;"""
            )
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(sqlite3.IntegrityError, match="briefing insert failed"):
            await service.run("forecast", now)

        assert state.known_article_ids((article.id,)) == set()
        assert state.pending_articles() == ()
        assert state.recent_briefings(now, 1) == ()
        assert state.recent_context_documents(now, 1) == ()
        assert state.pending_verbatim_deliveries() == ()

    assert len(publisher.messages) == 1
    assert publisher.messages[0][1] is True


async def test_run_returns_none_when_no_content_and_no_warnings(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        result = await service.run("briefing", now)

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
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            ops_delivery,
        )
        await service.run("briefing", now)

    assert len(ops_publisher.messages) >= 1
    assert "长时间无更新" in ops_publisher.messages[0][0].body


class FailingOnceLLM:
    def __init__(self, *, fail_before_response: bool = False, omit_headline: bool = False) -> None:
        self.attempts = 0
        self._fail_before_response = fail_before_response
        self._omit_headline = omit_headline

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.attempts += 1
        if self.attempts == 1:
            if self._fail_before_response:
                raise LLMError("contract validation failed before a response was available")
            invalid_result = {
                "headline": "Briefing",
                "headline_source_ids": ["invented"],
                "conclusions": [{"text": "Claim", "source_ids": ["invented"]}],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": True,
            }
            if self._omit_headline:
                del invalid_result["headline"]
            return invalid_result
        assert ("previous_invalid_response" in payload) is not self._fail_before_response
        allowed_source_ids = payload["allowed_source_ids"]
        assert isinstance(allowed_source_ids, list)
        assert payload["allowed_resolved_warning_ids"] == []
        source_id = str(allowed_source_ids[0])
        return {
            "headline": "Briefing",
            "headline_source_ids": [source_id],
            "conclusions": [],
            "active_warnings": [],
            "resolved_warning_ids": [],
            "advice": [],
            "disaster_tracking": [],
            "should_publish": True,
        }


@pytest.mark.parametrize(
    ("fail_before_response", "omit_headline"),
    ((False, False), (True, False), (False, True)),
)
async def test_llm_retry_on_validation_failure(tmp_path: Path, fail_before_response: bool, omit_headline: bool) -> None:
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
    settings = _TestSettings(
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
    llm = FailingOnceLLM(fail_before_response=fail_before_response, omit_headline=omit_headline)
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "retry.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        body = await service.run("briefing", now)

    assert body is not None
    assert llm.attempts == 2


async def test_llm_request_failure_does_not_enter_contract_repair(tmp_path: Path) -> None:
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
    settings = _TestSettings(
        timezone=timezone,
        feeds=(FeedConfig("feed", "Feed", "https://example.invalid/rss"),),
        context_sources=(),
        rss_stale_hours=24,
        rss_failure_threshold=3,
        warning_retention_hours=12,
        history_hours=48,
        briefing_max_characters=3500,
        llm_max_attempts=3,
    )

    class RequestFailingLLM:
        def __init__(self) -> None:
            self.attempts = 0

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.attempts += 1
            raise LLMRequestError("request failed")

    llm = RequestFailingLLM()
    delivery = DeliveryProvider(PlainTextRenderer(), RecordingPublisher())

    with SQLiteStateStore(tmp_path / "request-failure.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        with pytest.raises(LLMRequestError, match="request failed"):
            await service.run("briefing", now)

    assert llm.attempts == 1


async def test_briefing_exceeding_character_limit_is_rejected(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 13, 9, tz=timezone)
    settings = _TestSettings(
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
            context_documents = payload["context_documents"]
            assert _is_dict_list(context_documents)
            source_id = str(context_documents[0]["source_id"])
            return {
                "headline": "A" * 100,
                "headline_source_ids": [source_id],
                "conclusions": [],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": True,
            }

    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher)

    with SQLiteStateStore(tmp_path / "long.sqlite3") as state:
        service = BriefingService(
            settings,
            _location(),
            state,
            EmptyRSSSource(),
            EmptyContextSource(),
            LongLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        with pytest.raises(LLMError, match="validation failed") as exc_info:
            await service.run("briefing", now)

    assert exc_info.value.__cause__ is not None
    assert "visible characters; limit is 10" in str(exc_info.value.__cause__)


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
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            StaticRSSSource(article),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
        )
        body = await service.run("forecast", now)

    assert body is None
    assert llm.payload is None
    assert publisher.messages == []


async def test_rss_failure_does_not_crash_forecast_with_weather_context(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            FailingRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )
        body = await service.run("forecast", now)

    assert body is not None
    assert len(publisher.messages) == 1


async def test_rss_cancellation_aborts_task_without_recording_failure(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            CanceledRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(asyncio.CancelledError):
            await service.run("briefing", now)

        assert state.rss_sources_requiring_failure_alert(("canceled-feed",), 1) == []

    assert publisher.messages == []


async def test_rss_cancellation_records_other_completed_feed_results(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            MixedOutcomeRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            delivery,
            StaticWeatherContextProvider(),
        )

        with pytest.raises(asyncio.CancelledError):
            await service.run("briefing", now)

        assert state.rss_sources_requiring_failure_alert(("recovered-feed",), 1) == []
        assert state.rss_sources_requiring_failure_alert(("failing-feed",), 1) == ["failing-feed"]


async def test_rss_failure_alert_is_sent_after_threshold(
    tmp_path: Path,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            FailingRSSSource(),
            EmptyContextSource(),
            llm,
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )
        # First failure: no alert yet (threshold is 2)
        await service.run("briefing", now)
        assert ops_publisher.messages == []

        # Second failure: alert should trigger
        await service.run("briefing", now.add(hours=1))
        assert len(ops_publisher.messages) == 1
        assert "已连续至少 2 个调度轮次获取失败" in ops_publisher.messages[0][0].body

        # Third failure: no new alert (already alerted)
        await service.run("briefing", now.add(hours=2))
        assert len(ops_publisher.messages) == 1


async def test_failed_rss_alert_delivery_is_retried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    settings = _TestSettings(
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
            settings,
            _location(),
            state,
            FailingRSSSource(),
            EmptyContextSource(),
            RecordingLLM(),
            delivery,
            ops_delivery,
            StaticWeatherContextProvider(),
        )

        with caplog.at_level("ERROR", logger="weather_briefing.service"):
            await service.run("briefing", now)
        assert state.rss_sources_requiring_failure_alert(("fail-feed",), 1) == ["fail-feed"]
        assert len(publisher.messages) == 1

        await service.run("briefing", now.add(hours=1))
        assert state.rss_sources_requiring_failure_alert(("fail-feed",), 1) == []

    assert "Failed to publish or record RSS health alert" in caplog.text
    assert any("持续获取失败" in message.body for message, _, _ in ops_publisher.messages)
