import json

import httpx
import pendulum
import pytest

from weather_briefing.llm import (
    LLMError,
    OpenAICompatibleChatCompletionsProvider,
    parse_result,
)


def test_rejects_model_invented_source() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [{"text": "Claim", "source_ids": ["invented"]}],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }
    with pytest.raises(LLMError, match="unknown source"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            {"real"},
        )


def test_rejects_suppressed_message_with_active_warning() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [
            {
                "id": "warning",
                "title": "Warning",
                "status": "active",
                "detail": "Detail",
                "source_ids": ["source"],
            }
        ],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": False,
    }

    with pytest.raises(LLMError, match="active warnings require"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            {"source"},
        )


def test_rejects_result_time_without_timezone() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(ValueError, match="timezone information"):
        parse_result(payload, pendulum.naive(2026, 7, 13, 9), set())


def test_rejects_conclusions_not_an_array() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": "not-an-array",
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="must be an array"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_conclusion_entry_not_a_dict() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": ["not-a-dict"],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="entries must be objects"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_conclusion_without_source_ids() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [{"text": "Claim"}],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="must cite at least one source"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_active_warnings_not_an_array() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": "not-an-array",
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="active_warnings must be an array"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_warning_entry_not_a_dict() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": ["not-a-dict"],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="active_warnings entries must be objects"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_warning_without_source_ids() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [{"id": "w1", "title": "W", "status": "active", "detail": "D"}],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="Active warnings must cite at least one source"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_warning_with_unknown_source_id() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [{"id": "w1", "title": "W", "status": "active", "detail": "D", "source_ids": ["unknown"]}],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(LLMError, match="unknown source ID"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


def test_rejects_non_boolean_should_publish() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": "yes",
    }

    with pytest.raises(LLMError, match="should_publish must be a boolean"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            set(),
        )


async def test_openai_provider_rejects_empty_json_content() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}}]},
            )
        )
    ) as client:
        provider = OpenAICompatibleChatCompletionsProvider(
            client,
            api_key="key",
            base_url="https://api.example.invalid/v1",
            model="model",
            max_output_tokens=1024,
        )
        with pytest.raises(LLMError, match="empty JSON"):
            await provider.summarize("prompt", {})


async def test_openai_provider_rejects_non_dict_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps([1, 2, 3])}}]},
            )
        )
    ) as client:
        provider = OpenAICompatibleChatCompletionsProvider(
            client,
            api_key="key",
            base_url="https://api.example.invalid/v1",
            model="model",
            max_output_tokens=1024,
        )
        with pytest.raises(LLMError, match="JSON object"):
            await provider.summarize("prompt", {})


async def test_openai_provider_rejects_http_error_response() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        provider = OpenAICompatibleChatCompletionsProvider(
            client,
            api_key="key",
            base_url="https://api.example.invalid/v1",
            model="model",
            max_output_tokens=1024,
        )
        with pytest.raises(LLMError, match="LLM request"):
            await provider.summarize("prompt", {})
