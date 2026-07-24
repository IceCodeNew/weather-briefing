import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pendulum
import pytest

from weather_briefing.llm import LLMError
from weather_briefing.notifications import NotificationDecision
from weather_briefing.service_status import (
    AnthropicStatusProvider,
    DeepSeekStatusProvider,
    KimiStatusProvider,
    OpenAIStatusProvider,
    ServiceStatusError,
    ServiceStatusMessage,
    ServiceStatusMonitor,
    ServiceStatusSnapshot,
    ServiceSurface,
    StatusFeedProvider,
    collect_service_status,
    official_message_matches,
    service_status_providers,
)
from weather_briefing.service_status.providers._surface import keyword_surface
from weather_briefing.service_status.providers.deepseek import _deepseek_surface
from weather_briefing.service_status.providers.kimi import _kimi_surface
from weather_briefing.service_status.providers.openai import _openai_surface
from weather_briefing.state import SQLiteStateStore


def _rss(
    *,
    incident_id: str = "incident-1",
    title: str = "Elevated API errors",
    published: str = "Fri, 24 Jul 2026 11:34:20 GMT",
    summary: str = (
        "<b>Status: Monitoring</b><br/><br/>"
        "We have applied the mitigation and are monitoring the recovery."
        "<br/><br/><b>Affected components</b><ul>"
        "<li>ChatGPT (Operational)</li><li>Responses API (Operational)</li></ul>"
    ),
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Status</title>
    <item><guid>https://status.example/{incident_id}</guid><link>https://status.example/{incident_id}</link>
    <title>{title}</title><pubDate>{published}</pubDate>
    <description><![CDATA[{summary}]]></description></item>
    </channel></rss>""".encode()


def _classic_rss(
    *,
    status: str = "Resolved",
    body: str = "This incident has been resolved.",
    title: str = "Elevated errors",
) -> bytes:
    summary = (
        "<p><small>Jul <var>24</var>, <var>10:22</var> UTC</small><br/>"
        f"<strong>{status}</strong> - {body}</p>"
        "<p><small>Jul <var>24</var>, <var>09:09</var> UTC</small><br/>"
        "<strong>Investigating</strong> - We are investigating.</p>"
    )
    return _rss(title=title, summary=summary)


def _provider(
    client: httpx.AsyncClient,
    *,
    classify=lambda name: ServiceSurface.API if "API" in name else ServiceSurface.WEB,
) -> StatusFeedProvider:
    return StatusFeedProvider(
        client,
        provider_id="test",
        provider_name="Test",
        feed_url="https://status.example/history.rss",
        page_url="https://status.example",
        classify_component=classify,
    )


async def test_status_feed_parses_openai_style_message_and_surfaces() -> None:
    seen_identity: object = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_identity
        seen_identity = request.extensions.get("weather_briefing.api_call")
        return httpx.Response(200, content=_rss())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        snapshot = await _provider(client).fetch()

    message = snapshot.messages[0]
    assert snapshot.source_id == "service-status:test"
    assert message.status == "monitoring"
    assert message.body == "We have applied the mitigation and are monitoring the recovery."
    assert message.surfaces == (ServiceSurface.WEB, ServiceSurface.API)
    assert seen_identity == ("test", "status-feed")


async def test_status_feed_parses_classic_history_and_latest_update() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_classic_rss()))
    ) as client:
        message = (await _provider(client).fetch()).messages[0]

    assert message.status == "resolved"
    assert message.body == "This incident has been resolved."
    assert message.published_at == pendulum.datetime(2026, 7, 24, 11, 34, 20, tz="UTC")


async def test_status_feed_parses_flashcat_label_and_bilingual_message() -> None:
    summary = (
        "<p><strong>Status:</strong> resolved</p>"
        "<p>本次问题已解决，服务已恢复。 The incident has been resolved.</p>"
        "<p><strong>Affected components:</strong> "
        "DeepSeek API服务(API Service), DeepSeek 网页端(Web Service)</p>"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_rss(summary=summary)))
    ) as client:
        message = (await _provider(client).fetch()).messages[0]

    assert message.status == "resolved"
    assert message.body == "本次问题已解决，服务已恢复。 The incident has been resolved."
    assert message.surfaces == (ServiceSurface.API, ServiceSurface.WEB)


@pytest.mark.parametrize("status", (404, 503))
async def test_status_feed_wraps_http_errors(status: int) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        with pytest.raises(ServiceStatusError, match="feed request failed"):
            await _provider(client).fetch()


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"not a feed", "feed is invalid"),
        (b"<?xml version='1.0'?><rss><channel></channel></rss>", "no incident messages"),
        (_rss(title=""), "field title"),
        (_rss(published=""), "no publication time"),
        (_rss(published="bad"), "invalid publication time"),
        (_rss(published="Fri, 24 Jul 2026 11:34:20"), "has no UTC offset"),
        (_rss(summary="<p>No status</p>"), "has no status"),
        (_rss(summary="<b>Status:</b><br/>Update"), "empty status"),
        (_rss(summary="<b>Status: Monitoring</b>"), "empty body"),
        (_rss(summary="<p><strong>Status:</strong> monitoring</p>"), "empty body"),
        (_rss(summary="<strong>Monitoring</strong>"), "no update paragraph"),
    ),
)
async def test_status_feed_rejects_invalid_contract(payload: bytes, message: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        with pytest.raises(ServiceStatusError, match=message):
            await _provider(client).fetch()


def test_revision_changes_only_with_official_revision_content() -> None:
    async def fetch(payload: bytes) -> ServiceStatusMessage:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
        ) as client:
            return (await _provider(client).fetch()).messages[0]

    first = asyncio.run(fetch(_rss()))
    same = asyncio.run(fetch(_rss()))
    changed = asyncio.run(fetch(_rss(summary="<b>Status: Resolved</b><br/><br/>Recovered.")))

    assert first.revision_id == same.revision_id
    assert first.revision_id != changed.revision_id


async def test_classic_feed_accepts_update_without_hyphen_separator() -> None:
    payload = _rss(
        summary="<p><strong>Monitoring</strong> Mitigation is being monitored.</p>",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        message = (await _provider(client).fetch()).messages[0]

    assert message.body == "Mitigation is being monitored."


async def test_labeled_status_ignores_non_paragraph_siblings() -> None:
    payload = _rss(
        summary=(
            "<p><strong>Status:</strong> monitoring</p>metadata<span></span><p>Mitigation is being monitored.</p>"
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        message = (await _provider(client).fetch()).messages[0]

    assert message.body == "Mitigation is being monitored."


@pytest.mark.parametrize(
    ("classifier", "name", "expected"),
    (
        (keyword_surface, "Public API", ServiceSurface.API),
        (keyword_surface, "Web console", ServiceSurface.WEB),
        (keyword_surface, "Batch jobs", ServiceSurface.OTHER),
        (_deepseek_surface, "API Service", ServiceSurface.API),
        (_deepseek_surface, "Web Chat", ServiceSurface.WEB),
        (_kimi_surface, "API Service", ServiceSurface.API),
        (_kimi_surface, "Website", ServiceSurface.WEB),
        (_kimi_surface, "Model inference", ServiceSurface.OTHER),
        (_openai_surface, "Responses", ServiceSurface.API),
        (_openai_surface, "Conversations", ServiceSurface.WEB),
        (_openai_surface, "Model inference", ServiceSurface.OTHER),
    ),
)
def test_provider_surface_classification(
    classifier,
    name: str,
    expected: ServiceSurface,
) -> None:
    assert classifier(name) is expected


@pytest.mark.parametrize(
    ("provider_type", "expected_host"),
    (
        (DeepSeekStatusProvider, "status.deepseek.com"),
        (OpenAIStatusProvider, "status.openai.com"),
        (AnthropicStatusProvider, "status.claude.com"),
        (KimiStatusProvider, "status.moonshot.cn"),
    ),
)
async def test_official_providers_use_their_history_feed(
    provider_type: (
        type[DeepSeekStatusProvider]
        | type[OpenAIStatusProvider]
        | type[AnthropicStatusProvider]
        | type[KimiStatusProvider]
    ),
    expected_host: str,
) -> None:
    seen_host = ""

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_host
        seen_host = request.url.host
        return httpx.Response(200, content=_classic_rss())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await provider_type(client).fetch()

    assert seen_host == expected_host


async def test_collection_is_best_effort_and_preserves_cancellation(caplog) -> None:
    working = AsyncMock()
    working.fetch.return_value = _snapshot(_message(status="resolved"))
    failing = AsyncMock()
    failing.fetch.side_effect = ServiceStatusError("unavailable")

    with caplog.at_level("WARNING", logger="weather_briefing.service_status"):
        snapshots = await collect_service_status((working, failing))

    assert len(snapshots) == 1
    assert "unavailable" in caplog.text

    canceled = AsyncMock()
    canceled.fetch.side_effect = asyncio.CancelledError()
    also_canceled = AsyncMock()
    also_canceled.fetch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect_service_status((canceled, also_canceled))


def test_composition_builds_independent_official_providers() -> None:
    async def check() -> None:
        async with httpx.AsyncClient() as client:
            providers = service_status_providers(("deepseek", "openai", "anthropic", "kimi"), client)
            assert [type(provider) for provider in providers] == [
                DeepSeekStatusProvider,
                OpenAIStatusProvider,
                AnthropicStatusProvider,
                KimiStatusProvider,
            ]
            with pytest.raises(ValueError, match="Unsupported service-status provider"):
                service_status_providers(("unknown",), client)

    asyncio.run(check())


def _message(
    *,
    incident_id: str = "incident-1",
    revision_id: str = "revision-1",
    title: str = "Elevated API errors",
    status: str = "investigating",
    body: str = "We are investigating elevated API errors.",
    surfaces: tuple[ServiceSurface, ...] = (ServiceSurface.API,),
) -> ServiceStatusMessage:
    return ServiceStatusMessage(
        incident_id=incident_id,
        revision_id=revision_id,
        title=title,
        status=status,
        body=body,
        url=f"https://status.example/{incident_id}",
        published_at=pendulum.datetime(2026, 7, 24, 10, tz="UTC"),
        surfaces=surfaces,
    )


def _snapshot(*messages: ServiceStatusMessage) -> ServiceStatusSnapshot:
    return ServiceStatusSnapshot(
        source_id="service-status:test",
        source_name="Test Status",
        source_url="https://status.example",
        observed_at=pendulum.datetime(2026, 7, 24, 10, tz="UTC"),
        messages=messages,
    )


def _monitor_dependencies(
    snapshot: ServiceStatusSnapshot,
    *,
    should_notify: bool = True,
) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    provider = AsyncMock()
    provider.fetch.return_value = snapshot
    delivery = AsyncMock()
    decision = AsyncMock()
    decision.assess_notification.return_value = NotificationDecision(should_notify)
    translator = AsyncMock()
    return provider, delivery, decision, translator


async def test_initial_resolved_history_is_a_silent_baseline(tmp_path: Path) -> None:
    message = _message(status="resolved")
    provider, delivery, decision, translator = _monitor_dependencies(_snapshot(message))
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        published = await ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator).run(
            pendulum.now("UTC")
        )
        stored = state.service_status_message_state("service-status:test", message.incident_id)

    assert published == 0
    assert stored is not None and stored.handled_revision_id == message.revision_id
    decision.assess_notification.assert_not_awaited()
    delivery.publish_alert.assert_not_awaited()


async def test_meaningful_active_message_is_forwarded_to_every_delivery(tmp_path: Path) -> None:
    message = _message()
    provider, first, decision, translator = _monitor_dependencies(_snapshot(message))
    second = AsyncMock()
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor(
            (provider,),
            state,
            (("first", first), ("second", second)),
            decision,
            translator,
        )
        assert await monitor.run(pendulum.now("UTC")) == 1
        assert await monitor.run(pendulum.now("UTC")) == 0

    expected_body = f"{message.body}\n\n{message.url}"
    first.publish_alert.assert_awaited_once_with(message.title, expected_body)
    second.publish_alert.assert_awaited_once_with(message.title, expected_body)
    translator.translate_service_status.assert_not_awaited()
    decision.assess_notification.assert_awaited_once()


async def test_unworthy_revision_is_handled_without_delivery(tmp_path: Path) -> None:
    message = _message()
    provider, delivery, decision, translator = _monitor_dependencies(
        _snapshot(message),
        should_notify=False,
    )
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator)
        assert await monitor.run(pendulum.now("UTC")) == 0
        assert await monitor.run(pendulum.now("UTC")) == 0

    delivery.publish_alert.assert_not_awaited()
    decision.assess_notification.assert_awaited_once()


async def test_changed_revision_supplies_previous_official_message_to_decision(tmp_path: Path) -> None:
    first_message = _message()
    second_message = replace(
        first_message,
        revision_id="revision-2",
        status="resolved",
        body="This incident has been resolved.",
    )
    provider, delivery, decision, translator = _monitor_dependencies(_snapshot(first_message))
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator)
        await monitor.run(pendulum.now("UTC"))
        provider.fetch.return_value = _snapshot(second_message)
        await monitor.run(pendulum.now("UTC"))

    payload = decision.assess_notification.await_args_list[1].args[0]
    assert payload["notification_kind"] == "service_status"
    assert payload["previous"] == {
        "title": first_message.title,
        "status": first_message.status,
        "body": first_message.body,
    }
    assert payload["current"]["status"] == "resolved"


async def test_delivery_failure_retries_the_same_revision(tmp_path: Path) -> None:
    message = _message()
    provider, delivery, decision, translator = _monitor_dependencies(_snapshot(message))
    delivery.publish_alert.side_effect = [RuntimeError("down"), None]
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator)
        with pytest.raises(RuntimeError, match="down"):
            await monitor.run(pendulum.now("UTC"))
        stored = state.service_status_message_state("service-status:test", message.incident_id)
        assert stored is not None and stored.handled_revision_id is None
        assert await monitor.run(pendulum.now("UTC")) == 1

    assert delivery.publish_alert.await_count == 2


async def test_partial_delivery_failure_retries_only_pending_publishers(tmp_path: Path) -> None:
    message = _message()
    provider, first, decision, translator = _monitor_dependencies(_snapshot(message))
    second = AsyncMock()
    second.publish_alert.side_effect = [RuntimeError("down"), None]
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor(
            (provider,),
            state,
            (("first", first), ("second", second)),
            decision,
            translator,
        )
        with pytest.raises(RuntimeError, match="down"):
            await monitor.run(pendulum.now("UTC"))
        assert state.service_status_delivered_publishers(
            "service-status:test",
            message.incident_id,
            message.revision_id,
        ) == {"first"}
        assert await monitor.run(pendulum.now("UTC")) == 1

    first.publish_alert.assert_awaited_once()
    assert second.publish_alert.await_count == 2
    decision.assess_notification.assert_awaited_once()


async def test_mismatched_official_language_is_translated(tmp_path: Path) -> None:
    message = _message(title="検索障害", body="検索サービスでエラーが発生しています。")
    provider, delivery, decision, translator = _monitor_dependencies(_snapshot(message))
    translator.translate_service_status.return_value = (
        "搜索服务故障",
        "搜索服务发生错误。",
    )
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        await ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator, "zh-CN").run(
            pendulum.now("UTC")
        )

    translator.translate_service_status.assert_awaited_once_with(
        message.title,
        message.body,
        "zh-CN",
    )
    delivery.publish_alert.assert_awaited_once_with(
        "搜索服务故障",
        f"搜索服务发生错误。\n\n{message.url}",
    )


async def test_translation_failure_falls_back_to_official_text(tmp_path: Path, caplog) -> None:
    message = _message(title="検索障害", body="検索サービスでエラーが発生しています。")
    provider, delivery, decision, translator = _monitor_dependencies(_snapshot(message))
    translator.translate_service_status.side_effect = LLMError("unavailable")
    with (
        SQLiteStateStore(tmp_path / "state.sqlite3") as state,
        caplog.at_level("WARNING", logger="weather_briefing.service_status"),
    ):
        await ServiceStatusMonitor((provider,), state, (("test", delivery),), decision, translator, "zh-CN").run(
            pendulum.now("UTC")
        )

    delivery.publish_alert.assert_awaited_once_with(
        message.title,
        f"{message.body}\n\n{message.url}",
    )
    assert "error_type=LLMError" in caplog.text


@pytest.mark.parametrize(
    ("title", "body", "target", "expected"),
    (
        ("API incident", "We are investigating elevated errors.", "zh-CN", True),
        ("搜索请求出现大量报错", "This incident has been resolved.", "en", True),
        ("搜索服务故障", "搜索服务发生错误。", "zh-CN", True),
        ("検索障害", "検索サービスでエラーが発生しています。", "ja", True),
        ("搜索服务故障", "搜索服务发生错误。", "ja", False),
        ("障害", "調査中", "en", False),
    ),
)
def test_official_message_language_matching(
    title: str,
    body: str,
    target: str,
    expected: bool,
) -> None:
    assert official_message_matches(_message(title=title, body=body), target) is expected


def test_state_rejects_handling_a_changed_observation(tmp_path: Path) -> None:
    now = pendulum.now("UTC")
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        state.observe_service_status_message(
            "source",
            "incident",
            "new",
            "Title",
            "monitoring",
            "Body",
            now,
        )
        with pytest.raises(RuntimeError, match="changed before handling"):
            state.mark_service_status_message_handled(
                "source",
                "incident",
                "old",
                "Title",
                "monitoring",
                "Body",
                now,
            )


def test_state_rejects_deciding_a_changed_observation(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        state.observe_service_status_message(
            "source",
            "incident",
            "new",
            "Title",
            "monitoring",
            "Body",
            pendulum.now("UTC"),
        )
        with pytest.raises(RuntimeError, match="changed before its decision"):
            state.mark_service_status_message_decided(
                "source",
                "incident",
                "old",
                True,
            )
