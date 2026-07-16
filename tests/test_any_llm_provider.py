import json
import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from any_llm import AnyLLM

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.llm import (
    AnyLLMStructuredProvider,
    LLMStructuredOutput,
    create_any_llm_provider,
)


async def test_any_llm_provider_uses_structured_chat_completion() -> None:
    model_result = {
        "headline": "Briefing",
        "headline_source_ids": ["source"],
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": True,
    }
    client = SimpleNamespace(
        acompletion=AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(model_result)))]
            )
        )
    )
    provider = AnyLLMStructuredProvider(
        cast("AnyLLM", client),
        provider="deepseek",
        model="requested-model",
        max_output_tokens=4096,
    )

    result = await provider.summarize("Return JSON", {"input": "数据"})

    client.acompletion.assert_awaited_once_with(
        model="requested-model",
        messages=[
            {"role": "system", "content": "Return JSON"},
            {"role": "user", "content": '{"input": "数据"}'},
        ],
        response_format=LLMStructuredOutput,
        temperature=0.2,
        max_tokens=4096,
    )
    assert result == model_result


async def test_factory_accepts_every_any_llm_completion_provider(monkeypatch) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    def fake_create(provider: str, **options: object) -> SimpleNamespace:
        created.append((provider, options))
        return SimpleNamespace()

    monkeypatch.setattr(AnyLLM, "create", fake_create)
    async with httpx.AsyncClient() as http_client:
        completion_providers = [
            provider
            for provider in AnyLLM.get_supported_providers()
            if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
        ]
        adapters = [create_any_llm_provider(provider, "model", 1024, http_client) for provider in completion_providers]

    assert [adapter.provider for adapter in adapters] == completion_providers
    assert [provider for provider, _ in created] == completion_providers


async def test_factory_rejects_provider_without_completion() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ValueError, match="does not support completion"):
            create_any_llm_provider("voyage", "model", 1024, http_client)


async def test_any_llm_deepseek_uses_injected_logged_http_client(caplog) -> None:
    requests: list[httpx.Request] = []
    model_result = {
        "headline": "Briefing",
        "headline_source_ids": ["source"],
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "completion-id",
                "object": "chat.completion",
                "created": 1,
                "model": "requested-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(model_result),
                        },
                    }
                ],
            },
        )

    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    async with LoggedAsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        sdk_client = AnyLLM.create(
            "deepseek",
            api_key="runtime-key",
            api_base="https://api.example.invalid",
            http_client=http_client,
        )
        provider = AnyLLMStructuredProvider(
            sdk_client,
            provider="deepseek",
            model="requested-model",
            max_output_tokens=4096,
        )

        result = await provider.summarize("Return JSON", {"input": "data"})

    request_body = json.loads(requests[0].content)
    assert requests[0].url.path == "/chat/completions"
    assert request_body["model"] == "requested-model"
    assert request_body["max_tokens"] == 4096
    assert result == model_result
    assert "provider=deepseek operation=chat-completions method=POST" in caplog.text
