import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pendulum
import pytest
from any_llm import AnyLLM
from any_llm.exceptions import ProviderError

from weather_briefing.llm import AnyLLMStructuredProvider, LLMError, LLMStructuredOutput, parse_result


def _valid_payload() -> dict[str, Any]:
    return {
        "headline": "Briefing",
        "headline_source_ids": ["source"],
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "disaster_tracking": [],
        "advice": [],
        "should_publish": True,
    }


def _now() -> pendulum.DateTime:
    return pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")


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
    client = SimpleNamespace(
        acompletion=AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, content=None))]
            )
        )
    )
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    assert await provider.summarize("prompt", {}) == _valid_payload()


async def test_provider_rejects_empty_json_content() -> None:
    client = SimpleNamespace(
        acompletion=AsyncMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        )
    )
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMError, match="empty JSON"):
        await provider.summarize("prompt", {})


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (SimpleNamespace(choices=[]), "no completion choices"),
        (SimpleNamespace(choices=[SimpleNamespace()]), "missing a message"),
    ),
)
async def test_provider_rejects_malformed_completion_response(response: object, message: str) -> None:
    client = SimpleNamespace(acompletion=AsyncMock(return_value=response))
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMError, match=message):
        await provider.summarize("prompt", {})


async def test_provider_rejects_invalid_structured_response() -> None:
    client = SimpleNamespace(
        acompletion=AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([1, 2, 3])))]
            )
        )
    )
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMError, match="schema validation"):
        await provider.summarize("prompt", {})


async def test_provider_wraps_sdk_error() -> None:
    client = SimpleNamespace(acompletion=AsyncMock(side_effect=ProviderError("upstream failed")))
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(LLMError, match="LLM request"):
        await provider.summarize("prompt", {})


async def test_provider_does_not_mask_programming_errors() -> None:
    client = SimpleNamespace(acompletion=AsyncMock(side_effect=AttributeError("adapter bug")))
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="openai",
        model="model",
        max_output_tokens=1024,
    )

    with pytest.raises(AttributeError, match="adapter bug"):
        await provider.summarize("prompt", {})
