import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pendulum
import pytest
from any_llm.exceptions import LengthFinishReasonError, ProviderError
from any_llm.types.completion import ParsedChatCompletion
from pydantic import BaseModel

from weather_briefing.llm import (
    AnyLLMStructuredProvider,
    LLMError,
    LLMOutputLimitError,
    LLMRequestError,
    LLMStructuredOutput,
    SensitiveLLMDiagnostics,
    parse_result,
)


class _CompletionClientStub:
    def __init__(self, response: object | None = None, *, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> object:
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("Completion response was not configured")
        return self._response


class _DiagnosticsStub:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self.calls = 0

    def rendered_text_logging_enabled(self) -> bool:
        self.calls += 1
        return self._enabled


class _FailingDiagnosticsStub:
    def rendered_text_logging_enabled(self) -> bool:
        raise RuntimeError("diagnostic state unavailable")


def test_sensitive_llm_diagnostics_remains_public() -> None:
    assert SensitiveLLMDiagnostics.__name__ == "SensitiveLLMDiagnostics"


async def test_completion_client_stub_requires_a_configured_response() -> None:
    client = _CompletionClientStub()

    with pytest.raises(AssertionError, match="not configured"):
        await client.acompletion(
            model="model",
            messages=[],
            response_format=LLMStructuredOutput,
            temperature=0.2,
            max_tokens=1024,
        )


def _valid_payload() -> dict[str, Any]:
    return {
        "headline": "Briefing",
        "headline_source_ids": ["source"],
        "conclusions": [],
        "service_status": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "disaster_tracking": [],
        "advice": [],
        "should_publish": True,
    }


def _now() -> pendulum.DateTime:
    return pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")


async def test_sensitive_llm_diagnostics_log_application_owned_request_and_response(caplog) -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_valid_payload())))])
    diagnostics = _DiagnosticsStub(True)
    provider = AnyLLMStructuredProvider(
        _CompletionClientStub(response),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        diagnostics=diagnostics,
    )

    with caplog.at_level(logging.DEBUG, logger="weather_briefing.llm"):
        assert await provider.summarize("private system prompt", {"content": "private source body"}) == _valid_payload()

    assert "private system prompt" in caplog.text
    assert "private source body" in caplog.text
    assert "'headline': 'Briefing'" in caplog.text
    assert "api_key" not in caplog.text
    assert "endpoint" not in caplog.text
    assert diagnostics.calls == 1


async def test_sensitive_llm_diagnostics_remain_silent_when_switch_is_disabled(caplog) -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_valid_payload())))])
    provider = AnyLLMStructuredProvider(
        _CompletionClientStub(response),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        diagnostics=_DiagnosticsStub(False),
    )

    with caplog.at_level(logging.DEBUG, logger="weather_briefing.llm"):
        await provider.summarize("private system prompt", {"content": "private source body"})

    assert "Sensitive LLM" not in caplog.text


async def test_sensitive_llm_diagnostic_state_failure_does_not_affect_request(caplog) -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_valid_payload())))])
    provider = AnyLLMStructuredProvider(
        _CompletionClientStub(response),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        diagnostics=_FailingDiagnosticsStub(),
    )

    with caplog.at_level(logging.DEBUG, logger="weather_briefing.llm"):
        assert await provider.summarize("private system prompt", {}) == _valid_payload()

    assert "Sensitive LLM diagnostic state check failed" in caplog.text
    assert "private system prompt" not in caplog.text


def test_accepts_complete_suppressed_message_with_active_warning() -> None:
    payload = _valid_payload()
    payload.update(
        active_warnings=[
            {
                "id": "warning",
                "title": "Warning",
                "status": "active",
                "detail": "Detail",
                "source_ids": ["source"],
            }
        ],
        advice=[{"topic": "clothing", "text": "Wear layers", "source_ids": ["source"]}],
        conclusions=[{"text": "Cool morning", "source_ids": ["source"]}],
        disaster_tracking=[{"text": "Storm nearby", "source_ids": ["source"]}],
        should_publish=False,
    )

    result = parse_result(payload, _now(), {"source"})

    assert not result.should_publish
    assert result.active_warnings[0].id == "warning"
    assert result.advice[0].topic.value == "clothing"
    assert result.conclusions[0].text == "Cool morning"
    assert result.disaster_tracking[0].text == "Storm nearby"
    assert result.raw_payload == payload


def test_rejects_result_time_without_timezone_before_schema_validation() -> None:
    with pytest.raises(ValueError, match="timezone information"):
        parse_result({}, pendulum.naive(2026, 7, 13, 9), set())


@pytest.mark.parametrize("field", tuple(_valid_payload()))
def test_rejects_every_missing_required_top_level_field(field: str) -> None:
    payload = _valid_payload()
    del payload[field]

    with pytest.raises(LLMError, match=rf"schema validation failed at {field}"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("headline", None),
        ("headline", "   "),
        ("headline_source_ids", []),
        ("headline_source_ids", "source"),
        ("conclusions", "not-an-array"),
        ("active_warnings", "not-an-array"),
        ("resolved_warning_ids", "warning"),
        ("disaster_tracking", "not-an-array"),
        ("advice", "not-an-array"),
        ("should_publish", "yes"),
    ),
)
def test_rejects_invalid_top_level_field(field: str, value: object) -> None:
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(LLMError, match=rf"schema validation failed at {field}"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize("section", ("conclusions", "disaster_tracking"))
@pytest.mark.parametrize(
    "item",
    (
        "not-an-object",
        {"text": "Claim"},
        {"text": "", "source_ids": ["source"]},
        {"text": 42, "source_ids": ["source"]},
        {"text": "Claim", "source_ids": []},
        {"text": "Claim", "source_ids": [None]},
    ),
)
def test_rejects_invalid_sourced_item(section: str, item: object) -> None:
    payload = _valid_payload()
    payload[section] = [item]

    with pytest.raises(LLMError, match=rf"schema validation failed at {section}.0"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize("field", ("id", "title", "status", "detail", "source_ids"))
def test_rejects_incomplete_warning(field: str) -> None:
    warning = {
        "id": "w1",
        "title": "Warning",
        "status": "active",
        "detail": "Details",
        "source_ids": ["source"],
    }
    del warning[field]
    payload = _valid_payload()
    payload["active_warnings"] = [warning]

    with pytest.raises(LLMError, match=rf"schema validation failed at active_warnings.0.{field}"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize("field", ("id", "title", "status", "detail"))
@pytest.mark.parametrize("value", (None, "", "   ", 42))
def test_rejects_invalid_warning_text(field: str, value: object) -> None:
    warning = {
        "id": "w1",
        "title": "Warning",
        "status": "active",
        "detail": "Details",
        "source_ids": ["source"],
    }
    warning[field] = value
    payload = _valid_payload()
    payload["active_warnings"] = [warning]

    with pytest.raises(LLMError, match=rf"schema validation failed at active_warnings.0.{field}"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize(
    "advice",
    (
        ["not-an-object"],
        [{"text": "Advice", "source_ids": ["source"]}],
        [{"topic": "unknown", "text": "Advice", "source_ids": ["source"]}],
    ),
)
def test_rejects_invalid_advice(advice: object) -> None:
    payload = _valid_payload()
    payload["advice"] = advice

    with pytest.raises(LLMError, match=r"schema validation failed at advice.0"):
        parse_result(payload, _now(), {"source"})


@pytest.mark.parametrize(
    ("section", "item"),
    (
        ("headline_source_ids", ["invented"]),
        ("conclusions", [{"text": "Claim", "source_ids": ["invented"]}]),
        (
            "active_warnings",
            [
                {
                    "id": "w1",
                    "title": "Warning",
                    "status": "active",
                    "detail": "Details",
                    "source_ids": ["invented"],
                }
            ],
        ),
        ("disaster_tracking", [{"text": "Storm", "source_ids": ["invented"]}]),
        ("advice", [{"topic": "mask", "text": "Mask", "source_ids": ["invented"]}]),
    ),
)
def test_rejects_unknown_source_in_every_cited_section(section: str, item: object) -> None:
    payload = _valid_payload()
    payload[section] = deepcopy(item)

    with pytest.raises(LLMError, match="unknown source"):
        parse_result(payload, _now(), {"source"})


def test_rejects_unexpected_response_field() -> None:
    payload = _valid_payload()
    payload["unexpected"] = True

    with pytest.raises(LLMError, match=r"schema validation failed at unexpected"):
        parse_result(payload, _now(), {"source"})


async def test_provider_accepts_typed_parsed_response() -> None:
    parsed = LLMStructuredOutput.model_validate(_valid_payload())
    client = _CompletionClientStub(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, content=None))])
    )
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    assert await provider.summarize("prompt", {}) == _valid_payload()


@pytest.mark.parametrize("content", [None, "", "   "])
async def test_provider_rejects_empty_json_content_as_request_failure(content: object) -> None:
    client = _CompletionClientStub(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMRequestError, match="empty JSON"):
        await provider.summarize("prompt", {})


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (SimpleNamespace(), "no completion choices"),
        (SimpleNamespace(choices=[]), "no completion choices"),
        (SimpleNamespace(choices=[SimpleNamespace()]), "missing a message"),
    ),
)
async def test_provider_rejects_malformed_completion_response(response: object, message: str) -> None:
    client = _CompletionClientStub(response)
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMRequestError, match=message):
        await provider.summarize("prompt", {})


async def test_provider_rejects_invalid_structured_response() -> None:
    client = _CompletionClientStub(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([1, 2, 3])))])
    )
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMError, match="schema validation"):
        await provider.summarize("prompt", {})


async def test_provider_wraps_sdk_error() -> None:
    client = _CompletionClientStub(error=ProviderError("upstream failed"))
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMRequestError, match="LLM request"):
        await provider.summarize("prompt", {})


async def test_provider_classifies_output_token_limit_without_logging_content(caplog) -> None:
    truncated_completion = ParsedChatCompletion[object].model_construct()
    client = _CompletionClientStub(error=LengthFinishReasonError(completion=truncated_completion))
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="test-model",
        max_output_tokens=4096,
    )

    with (
        caplog.at_level(logging.DEBUG, logger="weather_briefing.llm"),
        pytest.raises(LLMOutputLimitError, match="output token limit"),
    ):
        await provider.summarize("private prompt", {"content": "private body"})

    assert "provider=openai" in caplog.text
    assert "model='test-model'" in caplog.text
    assert "max_output_tokens=4096" in caplog.text
    assert "error_type=LengthFinishReasonError" in caplog.text
    assert "private prompt" not in caplog.text
    assert "private body" not in caplog.text


async def test_provider_does_not_mask_programming_errors() -> None:
    client = _CompletionClientStub(error=AttributeError("adapter bug"))
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(AttributeError, match="adapter bug"):
        await provider.summarize("prompt", {})
