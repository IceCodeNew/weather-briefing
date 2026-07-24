import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pendulum
import pytest

from weather_briefing.llm import LLMError
from weather_briefing.service_status import (
    AnthropicStatusProvider,
    DeepSeekStatusProvider,
    FlashcatStatusProvider,
    KimiStatusProvider,
    OpenAIStatusProvider,
    ServiceComponentStatus,
    ServiceIncident,
    ServiceStatusError,
    ServiceStatusMonitor,
    ServiceStatusSnapshot,
    ServiceSurface,
    StatuspageProvider,
    collect_service_status,
    has_english_explanation,
    render_service_status_notification,
    service_status_fingerprint,
    service_status_providers,
)
from weather_briefing.service_status.flashcat import (
    _find_snapshot_props,
    _greatest_impact,
    _snapshot_props,
)
from weather_briefing.state import SQLiteStateStore


def _summary_payload() -> dict[str, object]:
    return {
        "page": {
            "updated_at": "2026-07-24T09:35:08Z",
        },
        "components": [
            {
                "name": "Web Chat",
                "status": "operational",
                "updated_at": "2026-07-24T09:30:00Z",
                "group": False,
            },
            {
                "name": "API Service",
                "status": "degraded_performance",
                "updated_at": "2026-07-24T09:31:00Z",
                "group": False,
            },
            {
                "name": "Products",
                "status": "degraded_performance",
                "updated_at": "2026-07-24T09:31:00Z",
                "group": True,
            },
        ],
        "incidents": [
            {
                "name": "Elevated API errors",
                "status": "monitoring",
                "impact": "minor",
                "updated_at": "2026-07-24T09:34:00Z",
                "components": [
                    {
                        "name": "API Service",
                    }
                ],
                "incident_updates": [
                    {
                        "body": "A fix has been applied.",
                        "affected_components": [
                            {
                                "name": "API Service",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _surface(name: str) -> ServiceSurface:
    return ServiceSurface.API if "API" in name else ServiceSurface.WEB


def _flashcat_html(
    *,
    active_changes: list[object] | None = None,
    components: list[object] | None = None,
    updated_at: object = 1_774_346_108_000,
) -> str:
    snapshot = {
        "page": {
            "components": components
            if components is not None
            else [
                {
                    "component_id": "web",
                    "name": "Web Chat",
                },
                {
                    "component_id": "api",
                    "name": "API Service",
                },
                {
                    "component_id": "hidden",
                    "name": "Legacy API",
                    "hide_all": True,
                },
            ],
        },
        "active_changes": active_changes or [],
    }
    tree = [
        "$",
        "component",
        None,
        {
            "initialData": snapshot,
            "initialDataUpdatedAt": updated_at,
        },
    ]
    flight_item = [1, "1d:" + json.dumps(tree)]
    return f"<html><script>self.__next_f.push({json.dumps(flight_item)})</script></html>"


def _flight_html(tree: object) -> str:
    flight_item = [1, "1d:" + json.dumps(tree)]
    return f"<html><script>self.__next_f.push({json.dumps(flight_item)})</script></html>"


async def test_flashcat_provider_parses_embedded_snapshot_and_incident() -> None:
    change: dict[str, object] = {
        "title": "API degradation",
        "status": "monitoring",
        "affected_components": [
            {
                "component_id": "api",
                "name": "API Service",
                "status": "degraded",
            }
        ],
        "updates": [
            {
                "at_seconds": 1_774_346_000,
                "description": "Investigating.",
            },
            {
                "at_seconds": 1_774_346_100,
                "description": "A fix is being monitored.",
            },
        ],
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=_flashcat_html(active_changes=[change])))
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert [(component.name, component.status) for component in snapshot.components] == [
        ("Web Chat", "operational"),
        ("API Service", "degraded"),
    ]
    assert snapshot.incidents[0].impact == "degraded"
    assert snapshot.incidents[0].surfaces == (ServiceSurface.API,)
    assert snapshot.incidents[0].detail == "A fix is being monitored."


async def test_flashcat_provider_classifies_status_page_request() -> None:
    seen_identity: object = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_identity
        seen_identity = request.extensions.get("weather_briefing.api_call")
        return httpx.Response(200, text=_flashcat_html())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert seen_identity == ("test", "status-page")


async def test_flashcat_provider_rejects_missing_embedded_snapshot() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<html></html>"))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        )
        with pytest.raises(ServiceStatusError, match="no valid embedded status snapshot"):
            await provider.fetch()


async def _fetch_flashcat_html(html: str) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as client:
        await FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()


async def test_flashcat_provider_wraps_http_failure() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        )
        with pytest.raises(ServiceStatusError, match="Test status request failed"):
            await provider.fetch()


@pytest.mark.parametrize("updated_at", (None, 0, "1774346108000"))
async def test_flashcat_provider_rejects_invalid_snapshot_time(updated_at: object) -> None:
    with pytest.raises(ServiceStatusError, match="initialDataUpdatedAt must be a positive integer"):
        await _fetch_flashcat_html(_flashcat_html(updated_at=updated_at))


@pytest.mark.parametrize(
    ("props", "message"),
    (
        (
            {
                "initialData": {"page": 7, "active_changes": []},
                "initialDataUpdatedAt": 1,
            },
            "embedded field page must be an object",
        ),
        (
            {
                "initialData": {"page": {}, "active_changes": "bad"},
                "initialDataUpdatedAt": 1,
            },
            "embedded field active_changes must be an array",
        ),
    ),
)
async def test_flashcat_provider_rejects_invalid_snapshot_fields(
    props: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ServiceStatusError, match=message):
        await _fetch_flashcat_html(_flight_html(["$", "component", None, props]))


@pytest.mark.parametrize(
    ("components", "message"),
    (
        ([7], "embedded component must be an object"),
        (
            [{"component_id": "hidden", "name": "Hidden", "hide_all": True}],
            "embedded status has no visible components",
        ),
        ([{"component_id": 7, "name": "API"}], "component_id must be a non-empty string"),
    ),
)
async def test_flashcat_provider_rejects_invalid_components(
    components: list[object],
    message: str,
) -> None:
    with pytest.raises(ServiceStatusError, match=message):
        await _fetch_flashcat_html(_flashcat_html(components=components))


@pytest.mark.parametrize(
    ("active_changes", "message"),
    (
        ([7], "active change must be an object"),
        (
            [{"affected_components": [7], "updates": []}],
            "affected component must be an object",
        ),
        (
            [
                {
                    "affected_components": [{"component_id": "api", "name": "API", "status": "degraded"}],
                    "updates": [],
                }
            ],
            "active change has no valid update",
        ),
        (
            [
                {
                    "affected_components": [{"component_id": "api", "name": "API", "status": "degraded"}],
                    "updates": [7],
                }
            ],
            "incident update must be an object",
        ),
        (
            [
                {
                    "affected_components": [{"component_id": "api", "name": "API", "status": "degraded"}],
                    "updates": [{"at_seconds": 0, "description": "Bad time"}],
                }
            ],
            "incident update time must be a positive integer",
        ),
        (
            [
                {
                    "affected_components": [{"component_id": "api", "name": "API", "status": "degraded"}],
                    "updates": [{"at_seconds": 1_774_346_100}],
                }
            ],
            "incident update must have a non-empty description",
        ),
    ),
)
async def test_flashcat_provider_rejects_invalid_active_changes(
    active_changes: list[object],
    message: str,
) -> None:
    with pytest.raises(ServiceStatusError, match=message):
        await _fetch_flashcat_html(_flashcat_html(active_changes=active_changes))


async def test_flashcat_provider_accepts_message_update_and_unknown_impact() -> None:
    change: dict[str, object] = {
        "title": "Unexpected state",
        "status": "identified",
        "affected_components": [
            {
                "component_id": "api",
                "name": "API Service",
                "status": "vendor_specific",
            }
        ],
        "updates": [{"at_seconds": 1_774_346_100, "message": "Vendor update."}],
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=_flashcat_html(active_changes=[change])))
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await FlashcatStatusProvider(
            client,
            provider_id="test",
            provider_name="Test",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert snapshot.incidents[0].impact == "vendor_specific"
    assert snapshot.incidents[0].detail == "Vendor update."
    assert _greatest_impact([]) == "unknown"


def test_flashcat_snapshot_parser_skips_unrelated_and_malformed_flight_data() -> None:
    unrelated_tree = "1d:" + json.dumps({"active_changes": []})
    malformed_tree = '1d:{"active_changes": bad}'
    malformed_items = (
        "",
        "noise",
        "self.__next_f.push(not-json)",
        f"self.__next_f.push({json.dumps('wrong')})",
        f"self.__next_f.push({json.dumps([1])})",
        f"self.__next_f.push({json.dumps([2, '1d:{}'])})",
        f"self.__next_f.push({json.dumps([1, 7])})",
        f"self.__next_f.push({json.dumps([1, 'missing-separator'])})",
        f"self.__next_f.push({json.dumps([1, '1d:{}'])})",
        f"self.__next_f.push({json.dumps([1, malformed_tree])})",
        f"self.__next_f.push({json.dumps([1, unrelated_tree])})",
    )
    html = "".join(f"<script>{item}</script>" for item in malformed_items)

    with pytest.raises(ServiceStatusError, match="no valid embedded status snapshot"):
        _snapshot_props(html, "Test")


def test_flashcat_snapshot_search_recurses_through_mapping_values() -> None:
    props = {
        "initialData": {"page": {}, "active_changes": []},
        "initialDataUpdatedAt": 1,
    }

    assert _find_snapshot_props({"nested": props}) == props


async def test_statuspage_provider_parses_components_and_active_incidents() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_summary_payload()))
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert snapshot.source_id == "service-status:test"
    assert snapshot.observed_at == pendulum.datetime(2026, 7, 24, 9, 35, 8, tz="UTC")
    assert [(component.name, component.surface, component.status) for component in snapshot.components] == [
        ("Web Chat", ServiceSurface.WEB, "operational"),
        ("API Service", ServiceSurface.API, "degraded_performance"),
    ]
    assert len(snapshot.incidents) == 1
    assert snapshot.incidents[0].surfaces == (ServiceSurface.API,)
    assert snapshot.incidents[0].detail == "A fix has been applied."


async def test_statuspage_provider_classifies_summary_request() -> None:
    seen_identity: object = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_identity
        seen_identity = request.extensions.get("weather_briefing.api_call")
        return httpx.Response(200, json=_summary_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert seen_identity == ("test", "status-summary")


async def test_statuspage_provider_rejects_invalid_external_response() -> None:
    payload = _summary_payload()
    payload["components"] = [{"name": "API Service", "status": 7, "updated_at": "2026-07-24T09:31:00Z"}]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        )
        with pytest.raises(ServiceStatusError, match="field status must be a non-empty string"):
            await provider.fetch()


async def _fetch_statuspage_response(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        await StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (httpx.Response(200, text="{invalid"), "response is not valid JSON"),
        (httpx.Response(200, json=[]), "response must be an object"),
        (httpx.Response(200, json={}), "field page must be an object"),
        (
            httpx.Response(
                200,
                json={
                    **_summary_payload(),
                    "components": [7],
                },
            ),
            "status component must be an object",
        ),
        (
            httpx.Response(
                200,
                json={
                    **_summary_payload(),
                    "components": [
                        {
                            "name": "Group",
                            "status": "operational",
                            "updated_at": "2026-07-24T09:31:00Z",
                            "group": True,
                        }
                    ],
                },
            ),
            "has no service components",
        ),
        (
            httpx.Response(200, json={**_summary_payload(), "incidents": [7]}),
            "status incident must be an object",
        ),
        (
            httpx.Response(
                200,
                json={
                    **_summary_payload(),
                    "incidents": [
                        {
                            "name": "Broken",
                            "status": "identified",
                            "impact": "minor",
                            "updated_at": "2026-07-24T09:34:00Z",
                            "incident_updates": [],
                        }
                    ],
                },
            ),
            "incident has no valid update",
        ),
        (
            httpx.Response(
                200,
                json={
                    **_summary_payload(),
                    "incidents": [
                        {
                            "name": "Broken",
                            "status": "identified",
                            "impact": "minor",
                            "updated_at": "2026-07-24T09:34:00Z",
                            "components": "bad",
                            "incident_updates": [{"body": "Update"}],
                        }
                    ],
                },
            ),
            "incident components must be an array or null",
        ),
        (
            httpx.Response(
                200,
                json={
                    **_summary_payload(),
                    "incidents": [
                        {
                            "name": "Broken",
                            "status": "identified",
                            "impact": "minor",
                            "updated_at": "2026-07-24T09:34:00Z",
                            "components": [7],
                            "incident_updates": [{"body": "Update"}],
                        }
                    ],
                },
            ),
            "incident component must be an object",
        ),
    ),
)
async def test_statuspage_provider_rejects_malformed_response(
    response: httpx.Response,
    message: str,
) -> None:
    with pytest.raises(ServiceStatusError, match=message):
        await _fetch_statuspage_response(response)


async def test_statuspage_provider_rejects_non_array_components() -> None:
    with pytest.raises(ServiceStatusError, match="field components must be an array"):
        await _fetch_statuspage_response(httpx.Response(200, json={**_summary_payload(), "components": "bad"}))


async def test_statuspage_incident_may_omit_affected_components() -> None:
    payload = _summary_payload()
    payload["incidents"] = [
        {
            "name": "Unscoped incident",
            "status": "identified",
            "impact": "minor",
            "updated_at": "2026-07-24T09:34:00Z",
            "incident_updates": [
                {
                    "body": "An update without component scope.",
                    "affected_components": None,
                }
            ],
        }
    ]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()

    assert snapshot.incidents[0].surfaces == ()


async def test_statuspage_provider_wraps_http_failure_without_exposing_endpoint() -> None:
    request = httpx.Request("GET", "https://status.example.invalid/api/v2/summary.json")

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret diagnostic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        provider = StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url=str(request.url),
            page_url="https://status.example.invalid",
            classify_component=_surface,
        )
        with pytest.raises(ServiceStatusError, match="Test status request failed") as caught:
            await provider.fetch()

    assert "secret diagnostic" not in str(caught.value)


async def test_collection_keeps_successful_status_when_an_optional_provider_fails(caplog) -> None:
    working = AsyncMock()
    failing = AsyncMock()
    working.fetch.return_value = await _snapshot()
    failing.fetch.side_effect = ServiceStatusError("Unavailable")

    with caplog.at_level("WARNING", logger="weather_briefing.service_status"):
        snapshots = await collect_service_status((working, failing))

    assert [snapshot.source_id for snapshot in snapshots] == ["service-status:test"]
    assert "Service-status provider failed: Unavailable" in caplog.text


async def test_collection_preserves_cancellation() -> None:
    first_canceled = AsyncMock()
    second_canceled = AsyncMock()
    first_canceled.fetch.side_effect = asyncio.CancelledError
    second_canceled.fetch.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await collect_service_status((first_canceled, second_canceled))


async def _snapshot():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_summary_payload()))
    async with httpx.AsyncClient(transport=transport) as client:
        return await StatuspageProvider(
            client,
            provider_id="test",
            provider_name="Test",
            api_url="https://status.example.invalid/api/v2/summary.json",
            page_url="https://status.example.invalid",
            classify_component=_surface,
        ).fetch()


async def test_concrete_provider_composition_is_independent_and_ordered() -> None:
    async with httpx.AsyncClient() as client:
        providers = service_status_providers(("deepseek", "openai", "anthropic", "kimi"), client)

    assert tuple(type(provider) for provider in providers) == (
        DeepSeekStatusProvider,
        OpenAIStatusProvider,
        AnthropicStatusProvider,
        KimiStatusProvider,
    )


async def test_concrete_provider_composition_rejects_unknown_name() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Unsupported service-status provider: unknown"):
            service_status_providers(("unknown",), client)


async def test_openai_component_classification_distinguishes_web_api_and_other() -> None:
    async with httpx.AsyncClient() as client:
        classifier = OpenAIStatusProvider(client)._classify_component

    assert classifier("ChatGPT Work") is ServiceSurface.WEB
    assert classifier("Responses") is ServiceSurface.API
    assert classifier("VS Code extension") is ServiceSurface.OTHER


async def test_other_concrete_component_classifiers_cover_declared_surfaces() -> None:
    async with httpx.AsyncClient() as client:
        deepseek = DeepSeekStatusProvider(client)._classify_component
        anthropic = AnthropicStatusProvider(client)._classify_component
        kimi = KimiStatusProvider(client)._classify_component

    assert deepseek("API Service") is ServiceSurface.API
    assert deepseek("Instant Mode") is ServiceSurface.WEB
    assert anthropic("Claude API") is ServiceSurface.API
    assert anthropic("claude.ai") is ServiceSurface.WEB
    assert anthropic("Claude Code") is ServiceSurface.OTHER
    assert kimi("Open API") is ServiceSurface.API
    assert kimi("Website") is ServiceSurface.WEB
    assert kimi("Model") is ServiceSurface.OTHER


def _direct_snapshot(
    *,
    web_status: str = "operational",
    api_status: str = "operational",
    detail: str | None = None,
    observed_at: pendulum.DateTime | None = None,
) -> ServiceStatusSnapshot:
    observed_at = observed_at or pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")
    incidents = (
        (
            ServiceIncident(
                name="Elevated API errors",
                status="monitoring",
                impact="minor",
                updated_at=observed_at,
                detail=detail,
                surfaces=(ServiceSurface.API,),
            ),
        )
        if detail is not None
        else ()
    )
    return ServiceStatusSnapshot(
        source_id="service-status:test",
        source_name="Test Status",
        source_url="https://status.example.invalid",
        observed_at=observed_at,
        components=(
            ServiceComponentStatus("Web Chat", ServiceSurface.WEB, web_status, observed_at),
            ServiceComponentStatus("API Service", ServiceSurface.API, api_status, observed_at),
        ),
        incidents=incidents,
    )


def test_direct_notification_separates_surfaces_and_keeps_official_detail() -> None:
    title, body = render_service_status_notification(
        _direct_snapshot(
            web_status="partial_outage",
            api_status="degraded_performance",
            detail="A fix has been applied and the API is recovering.",
        ),
        recovered=False,
    )

    assert title == "Test Status"
    assert "Web services:\n- Web Chat: partial_outage" in body
    assert "API services:\n- API Service: degraded_performance" in body
    assert "A fix has been applied and the API is recovering." in body
    assert "Source: https://status.example.invalid" in body


def test_recovery_notification_uses_predefined_text() -> None:
    title, body = render_service_status_notification(_direct_snapshot(), recovered=True)

    assert title == "Test Status recovered"
    assert "All monitored services are operational" in body


def test_direct_notification_has_safe_fallback_for_unclassified_issue() -> None:
    _, body = render_service_status_notification(_direct_snapshot(), recovered=False)

    assert "The official status page reports an active service issue." in body


@pytest.mark.parametrize(
    ("language", "web_label", "recovery_text"),
    (
        ("en", "Web services", "All monitored services are operational"),
        ("zh-CN", "网页服务", "所有受监控服务均已恢复正常"),
        ("ja", "Web サービス", "監視対象のサービスはすべて正常"),
    ),
)
def test_predefined_notifications_cover_readme_languages(
    language: str,
    web_label: str,
    recovery_text: str,
) -> None:
    _, issue_body = render_service_status_notification(
        _direct_snapshot(web_status="partial_outage"),
        recovered=False,
        language=language,
    )
    _, recovery_body = render_service_status_notification(
        _direct_snapshot(),
        recovered=True,
        language=language,
    )

    assert web_label in issue_body
    assert recovery_text in recovery_body


def test_fingerprint_ignores_observation_times_and_component_order() -> None:
    first = _direct_snapshot(api_status="degraded_performance", detail="API errors are elevated.")
    later = first.observed_at.add(minutes=5)
    second = replace(
        first,
        observed_at=later,
        components=tuple(replace(component, updated_at=later) for component in reversed(first.components)),
        incidents=tuple(replace(incident, updated_at=later) for incident in first.incidents),
    )

    assert service_status_fingerprint(first) == service_status_fingerprint(second)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("A fix has been applied and API error rates are recovering.", True),
        ("API 服务错误率升高，正在处理。", False),
        ("API 障害を確認しています。", False),
        ("503", False),
    ),
)
def test_english_explanation_detection(text: str, expected: bool) -> None:
    assert has_english_explanation(text) is expected


async def test_monitor_records_initial_healthy_state_without_publishing(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.fetch.return_value = _direct_snapshot()
    delivery = AsyncMock()

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        published = await ServiceStatusMonitor((provider,), state, delivery).run(
            pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")
        )
        stored = state.service_status_state("service-status:test")

    assert published == 0
    delivery.publish_alert.assert_not_awaited()
    assert stored is not None
    assert stored.notified_unhealthy is False


def test_state_rejects_notification_for_an_unobserved_fingerprint(tmp_path: Path) -> None:
    with (
        SQLiteStateStore(tmp_path / "state.sqlite3") as state,
        pytest.raises(
            RuntimeError,
            match="observation changed before notification was recorded",
        ),
    ):
        state.mark_service_status_notified(
            "service-status:test",
            "missing",
            True,
            pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC"),
        )


async def test_monitor_publishes_issue_once_then_recovery(tmp_path: Path) -> None:
    provider = AsyncMock()
    issue = _direct_snapshot(
        api_status="degraded_performance",
        detail="API error rates are elevated and a fix is being monitored.",
    )
    provider.fetch.return_value = issue
    delivery = AsyncMock()
    now = pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor((provider,), state, delivery)
        assert await monitor.run(now) == 1
        assert await monitor.run(now.add(minutes=5)) == 0
        provider.fetch.return_value = _direct_snapshot(observed_at=now.add(minutes=10))
        assert await monitor.run(now.add(minutes=10)) == 1

    assert delivery.publish_alert.await_count == 2
    recovery_call = delivery.publish_alert.await_args_list[-1]
    assert recovery_call.args[0] == "Test Status recovered"


async def test_monitor_retries_delivery_before_marking_state_notified(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.fetch.return_value = _direct_snapshot(
        api_status="partial_outage",
        detail="API requests are failing for some customers.",
    )
    delivery = AsyncMock()
    delivery.publish_alert.side_effect = [RuntimeError("delivery failed"), None]
    now = pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        monitor = ServiceStatusMonitor((provider,), state, delivery)
        with pytest.raises(RuntimeError, match="delivery failed"):
            await monitor.run(now)
        failed_state = state.service_status_state("service-status:test")
        assert failed_state is not None
        assert failed_state.notified_fingerprint is None
        assert await monitor.run(now.add(minutes=5)) == 1


async def test_monitor_translates_only_non_english_incident_details(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.fetch.return_value = _direct_snapshot(
        api_status="degraded_performance",
        detail="API 服务错误率升高，正在处理。",
    )
    delivery = AsyncMock()
    translator = AsyncMock()
    translator.translate_service_status.return_value = "API error rates are elevated and remediation is in progress."

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        await ServiceStatusMonitor((provider,), state, delivery, translator).run(
            pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")
        )

    translator.translate_service_status.assert_awaited_once_with(
        "API 服务错误率升高，正在处理。",
        "en",
    )
    assert "API error rates are elevated" in delivery.publish_alert.await_args.args[1]


async def test_monitor_uses_english_official_detail_without_llm(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.fetch.return_value = _direct_snapshot(
        api_status="degraded_performance",
        detail="API error rates are elevated and remediation is in progress.",
    )
    delivery = AsyncMock()
    translator = AsyncMock()

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        await ServiceStatusMonitor((provider,), state, delivery, translator).run(
            pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")
        )

    translator.translate_service_status.assert_not_awaited()


async def test_monitor_falls_back_to_official_text_when_translation_fails(
    tmp_path: Path,
    caplog,
) -> None:
    provider = AsyncMock()
    official_detail = "API 服务错误率升高，正在处理。"
    provider.fetch.return_value = _direct_snapshot(
        api_status="degraded_performance",
        detail=official_detail,
    )
    delivery = AsyncMock()
    translator = AsyncMock()
    translator.translate_service_status.side_effect = LLMError("translation unavailable")

    with SQLiteStateStore(tmp_path / "state.sqlite3") as state:
        await ServiceStatusMonitor((provider,), state, delivery, translator).run(
            pendulum.datetime(2026, 7, 24, 9, 35, tz="UTC")
        )

    assert official_detail in delivery.publish_alert.await_args.args[1]
    assert "using the official original text" in caplog.text
    assert "translation unavailable" not in caplog.text
