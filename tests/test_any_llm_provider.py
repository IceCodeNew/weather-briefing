import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from any_llm import AnyLLM
from pydantic import BaseModel

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.llm import (
    AnyLLMStructuredProvider,
    LLMStructuredOutput,
    create_any_llm_provider,
)


class _CompletionClientStub:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> object:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self._response


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
    client = _CompletionClientStub(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(model_result)))])
    )
    provider = AnyLLMStructuredProvider(
        client,
        provider="deepseek",
        model="requested-model",
        max_output_tokens=4096,
    )

    result = await provider.summarize("Return JSON", {"input": "数据"})

    assert client.calls == [
        {
            "model": "requested-model",
            "messages": [
                {"role": "system", "content": "Return JSON"},
                {"role": "user", "content": '{"input": "数据"}'},
            ],
            "response_format": LLMStructuredOutput,
            "temperature": 0.2,
            "max_tokens": 4096,
        }
    ]
    assert result == model_result


async def test_factory_accepts_every_any_llm_completion_provider(monkeypatch) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    def fake_create(provider: str, **options: object) -> _CompletionClientStub:
        created.append((provider, options))
        return _CompletionClientStub(SimpleNamespace())

    monkeypatch.setattr(AnyLLM, "create", fake_create)
    completion_providers = [
        provider
        for provider in AnyLLM.get_supported_providers()
        if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
    ]
    adapters = [create_any_llm_provider(provider, "model", 1024) for provider in completion_providers]

    assert [adapter.provider for adapter in adapters] == completion_providers
    assert [provider for provider, _ in created] == completion_providers
    assert all("http_client" not in options and "max_retries" not in options for _, options in created)


async def test_factory_owned_provider_closes_underlying_sdk_clients(monkeypatch) -> None:
    closed: list[str] = []

    class AsyncSDKClient:
        async def aclose(self) -> None:
            closed.append("async")

    class SyncSDKClient:
        def close(self) -> None:
            closed.append("sync")

    class SDKProvider(_CompletionClientStub):
        def __init__(self) -> None:
            super().__init__(SimpleNamespace())
            self.client = AsyncSDKClient()
            self.duplicate_client = self.client
            self.responses_client = SyncSDKClient()

    sdk_provider = SDKProvider()
    monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: sdk_provider)

    provider = create_any_llm_provider("deepseek", "model", 1024)
    await provider.aclose()

    assert closed == ["async", "sync"]


async def test_owned_llm_client_cleanup_failure_continues_with_nested_resources(caplog) -> None:
    closed: list[str] = []

    class NestedClient:
        def close(self) -> None:
            closed.append("nested")

    class FailingClient(_CompletionClientStub):
        def __init__(self) -> None:
            super().__init__(SimpleNamespace())
            self.client = NestedClient()

        async def aclose(self) -> None:
            raise RuntimeError("cleanup failed")

    provider = AnyLLMStructuredProvider(
        FailingClient(),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        owns_client=True,
    )

    await provider.aclose()

    assert closed == ["nested"]
    assert "Failed to close LLM SDK resource type=FailingClient" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "cleanup failed" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_owned_llm_client_without_attribute_storage_is_safe(caplog) -> None:
    class SlottedClient:
        __slots__ = ("acompletion",)
        acompletion: AsyncMock

        def __init__(self) -> None:
            self.acompletion = AsyncMock()

    provider = AnyLLMStructuredProvider(
        SlottedClient(),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        owns_client=True,
    )

    with caplog.at_level(logging.DEBUG, logger="weather_briefing.llm"):
        await provider.aclose()

    assert "LLM SDK resource has no discoverable nested resources type=SlottedClient" in caplog.text


async def test_owned_llm_client_with_top_level_close_stops_nested_discovery() -> None:
    closed = False

    class CloseableClient(_CompletionClientStub):
        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    provider = AnyLLMStructuredProvider(
        CloseableClient(SimpleNamespace()),
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
        owns_client=True,
    )

    await provider.aclose()

    assert closed


async def test_borrowed_llm_client_is_not_closed() -> None:
    class BorrowedClient(_CompletionClientStub):
        def __init__(self) -> None:
            super().__init__(SimpleNamespace())
            self.aclose = AsyncMock()

    client = BorrowedClient()
    provider = AnyLLMStructuredProvider(
        client,
        provider="deepseek",
        model="model",
        max_output_tokens=1024,
    )

    await provider.aclose()

    client.aclose.assert_not_awaited()


async def test_factory_rejects_provider_without_completion() -> None:
    with pytest.raises(ValueError, match="does not support completion"):
        create_any_llm_provider("voyage", "model", 1024)


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
