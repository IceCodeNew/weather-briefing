import json
import logging
import os
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from anthropic import BadRequestError as AnthropicBadRequestError
from any_llm import AnyLLM
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletionMessage
from openai import AsyncOpenAI, BadRequestError
from pydantic import BaseModel, ValidationError

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.llm import (
    AnyLLMStructuredProvider,
    FallbackLLMProvider,
    LazyServiceStatusLLM,
    LLMRequestError,
    LLMStructuredOutput,
    create_any_llm_provider,
)
from weather_briefing.llm.schema import NotificationDecisionOutput, ServiceStatusTranslationOutput
from weather_briefing.notifications import NotificationDecision


class _CompletionClientStub:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any] | ChatCompletionMessage],
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


def _openai_bad_request(response: httpx.Response) -> Exception:
    return BadRequestError("Upstream request failed", response=response, body={"error": "upstream"})


def _anthropic_bad_request(response: httpx.Response) -> Exception:
    return AnthropicBadRequestError("Upstream request failed", response=response, body={"error": "upstream"})


def _httpx_connection_error(response: httpx.Response) -> Exception:
    return httpx.ConnectError("Upstream connection failed", request=response.request)


class _ProviderStatusError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__("Upstream request failed")
        self.response = response


def _provider_status_error(response: httpx.Response) -> Exception:
    return _ProviderStatusError(response)


class _RequestMetadataTransportError(Exception):
    def __init__(self, method: str = "POST") -> None:
        super().__init__("Upstream connection failed")
        self.request = SimpleNamespace(method=method, url="https://api.example.invalid/chat/completions")


class _RequestInfoTransportError(Exception):
    def __init__(self) -> None:
        super().__init__("Upstream connection failed")
        self.request_info = SimpleNamespace(method="GET", real_url="https://api.example.invalid/models")


class _BrokenRequestMetadataError(Exception):
    def __init__(self, message: str, *, response: object | None = None) -> None:
        super().__init__(message)
        self.response = response

    @property
    def request(self) -> object:
        raise RuntimeError("broken request metadata")


class _BrokenURL:
    def __str__(self) -> str:
        raise RuntimeError("broken URL metadata")


class _BrokenURLMetadataError(Exception):
    def __init__(self) -> None:
        super().__init__("Upstream connection failed")
        self.request = SimpleNamespace(method="POST", url=_BrokenURL())


async def test_service_status_llm_is_created_only_on_first_operation() -> None:
    provider = AsyncMock()
    provider.assess_notification.return_value = NotificationDecision(True)
    provider.translate_service_status.return_value = ("Translated", "Translated body")
    factory = AsyncMock(return_value=provider)
    lazy = LazyServiceStatusLLM(factory)

    await lazy.aclose()
    factory.assert_not_called()

    assert await lazy.assess_notification({"current": {}}) == NotificationDecision(True)
    assert await lazy.translate_service_status("Title", "Body", "en") == (
        "Translated",
        "Translated body",
    )
    await lazy.aclose()

    factory.assert_awaited_once_with()
    provider.assess_notification.assert_awaited_once_with({"current": {}})
    provider.translate_service_status.assert_awaited_once_with("Title", "Body", "en")
    provider.aclose.assert_awaited_once()


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
                {"role": "user", "content": '{"input":"数据"}'},
            ],
            "response_format": LLMStructuredOutput,
            "temperature": 0.2,
            "max_tokens": 4096,
        }
    ]
    assert result == model_result


async def test_any_llm_provider_translates_service_status_with_a_narrow_schema() -> None:
    client = _CompletionClientStub(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"title":"API incident","body":"API error rates are elevated."}',
                    )
                )
            ]
        )
    )
    provider = AnyLLMStructuredProvider(
        client,
        provider="deepseek",
        model="requested-model",
        max_output_tokens=4096,
    )

    result = await provider.translate_service_status(
        "API 服务异常",
        "API 服务错误率升高。",
        "en",
    )

    assert result == ("API incident", "API error rates are elevated.")
    assert client.calls[0]["response_format"] is ServiceStatusTranslationOutput
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["max_tokens"] == 2048
    messages = client.calls[0]["messages"]
    assert isinstance(messages, list)
    assert messages[1] == {
        "role": "user",
        "content": '{"title":"API 服务异常","body":"API 服务错误率升高。"}',
    }


async def test_any_llm_provider_assesses_notification_value_with_a_narrow_schema() -> None:
    client = _CompletionClientStub(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"should_notify":false}'),
                )
            ]
        )
    )
    provider = AnyLLMStructuredProvider(
        client,
        provider="deepseek",
        model="requested-model",
        max_output_tokens=4096,
    )

    result = await provider.assess_notification(
        {
            "notification_kind": "service_status",
            "current": {"status": "monitoring"},
        }
    )

    assert not result.should_notify
    assert client.calls[0]["response_format"] is NotificationDecisionOutput
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["max_tokens"] == 256


@pytest.mark.parametrize(
    ("provider_name", "error_factory", "operation", "args", "message"),
    (
        (
            "openai",
            _openai_bad_request,
            "summarize",
            ("Return JSON", {"input": "data"}),
            "LLM request failed",
        ),
        (
            "anthropic",
            _anthropic_bad_request,
            "summarize",
            ("Return JSON", {"input": "data"}),
            "LLM request failed",
        ),
        (
            "openrouter",
            _httpx_connection_error,
            "summarize",
            ("Return JSON", {"input": "data"}),
            "LLM request failed",
        ),
        (
            "gemini",
            _provider_status_error,
            "summarize",
            ("Return JSON", {"input": "data"}),
            "LLM request failed",
        ),
        (
            "openai",
            _openai_bad_request,
            "assess_notification",
            ({"current": {"status": "operational"}},),
            "LLM notification decision request failed",
        ),
        (
            "openai",
            _openai_bad_request,
            "translate_service_status",
            ("Incident", "Elevated errors", "en"),
            "LLM translation request failed",
        ),
    ),
)
async def test_factory_normalizes_provider_native_request_errors(
    monkeypatch,
    provider_name: str,
    error_factory: Callable[[httpx.Response], Exception],
    operation: str,
    args: tuple[object, ...],
    message: str,
) -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat/completions")
    response = httpx.Response(400, request=request)
    error = error_factory(response)
    client = AsyncMock(spec=AnyLLM)
    client.acompletion.side_effect = error
    monkeypatch.delenv("ANY_LLM_UNIFIED_EXCEPTIONS", raising=False)
    monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: client)
    provider = create_any_llm_provider(provider_name, "requested-model", 4096)

    with pytest.raises(LLMRequestError, match=f"^{message}$") as exc_info:
        await getattr(provider, operation)(*args)

    assert exc_info.value.__cause__ is error
    assert "ANY_LLM_UNIFIED_EXCEPTIONS" not in os.environ


async def test_factory_preserves_sdk_output_validation_errors(monkeypatch) -> None:
    with pytest.raises(ValidationError) as validation:
        LLMStructuredOutput.model_validate({})
    client = AsyncMock(spec=AnyLLM)
    client.acompletion.side_effect = validation.value
    monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: client)
    provider = create_any_llm_provider("openai", "requested-model", 4096)

    with pytest.raises(ValidationError) as propagated:
        await provider.summarize("Return JSON", {"input": "data"})

    assert propagated.value is validation.value


async def test_any_llm_client_does_not_mask_payload_serialization_errors() -> None:
    client = AsyncMock(spec=AnyLLM)
    provider = AnyLLMStructuredProvider(
        client,
        provider="openai",
        model="requested-model",
        max_output_tokens=4096,
    )

    with pytest.raises(TypeError, match="not JSON serializable"):
        await provider.summarize("Return JSON", {"input": object()})

    client.acompletion.assert_not_awaited()


async def test_protocol_client_normalizes_transport_errors() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat/completions")
    error = httpx.ConnectError("Upstream connection failed", request=request)
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(LLMRequestError, match="^LLM request failed$") as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value.__cause__ is error


async def test_protocol_client_normalizes_request_metadata_errors() -> None:
    error = _RequestMetadataTransportError()
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(LLMRequestError, match="^LLM request failed$") as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value.__cause__ is error


async def test_protocol_client_normalizes_request_info_errors() -> None:
    error = _RequestInfoTransportError()
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(LLMRequestError, match="^LLM request failed$") as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value.__cause__ is error


async def test_protocol_client_preserves_non_http_request_metadata() -> None:
    error = _RequestMetadataTransportError(method="FETCH")
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(_RequestMetadataTransportError) as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value is error


async def test_protocol_client_preserves_error_when_metadata_inspection_fails() -> None:
    error = _BrokenRequestMetadataError("Upstream connection failed")
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(_BrokenRequestMetadataError) as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value is error


async def test_protocol_client_uses_response_when_request_metadata_fails() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat/completions")
    response = httpx.Response(503, request=request)
    error = _BrokenRequestMetadataError("Upstream request failed", response=response)
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(LLMRequestError, match="^LLM request failed$") as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value.__cause__ is error


async def test_protocol_client_preserves_error_when_url_conversion_fails() -> None:
    error = _BrokenURLMetadataError()
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
        normalize_native_errors=True,
    )

    with pytest.raises(_BrokenURLMetadataError) as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value is error


async def test_protocol_client_preserves_native_errors_without_opt_in() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat/completions")
    error = httpx.ConnectError("Upstream connection failed", request=request)
    client = AsyncMock()
    client.acompletion.side_effect = error
    provider = AnyLLMStructuredProvider(
        client,
        provider="wrapped-provider",
        model="requested-model",
        max_output_tokens=4096,
    )

    with pytest.raises(httpx.ConnectError) as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value is error


async def test_completion_programming_error_does_not_switch_to_fallback(monkeypatch) -> None:
    error = TypeError("SDK programming failure")
    primary_client = AsyncMock(spec=AnyLLM)
    primary_client.acompletion.side_effect = error
    monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: primary_client)
    fallback_client = AsyncMock(spec=AnyLLM)
    provider = FallbackLLMProvider(
        create_any_llm_provider("openai", "primary-model", 4096),
        AnyLLMStructuredProvider(
            fallback_client,
            provider="anthropic",
            model="fallback-model",
            max_output_tokens=4096,
        ),
        primary_name="openai/primary-model",
        fallback_name="anthropic/fallback-model",
    )

    with pytest.raises(TypeError, match="SDK programming failure") as exc_info:
        await provider.summarize("Return JSON", {"input": "data"})

    assert exc_info.value is error
    fallback_client.acompletion.assert_not_awaited()


async def test_provider_native_request_error_switches_to_fallback(monkeypatch) -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat/completions")
    response = httpx.Response(400, request=request)
    error = BadRequestError(
        "Upstream request failed",
        response=response,
        body={"error": "upstream"},
    )
    primary_client = AsyncMock(spec=AnyLLM)
    primary_client.acompletion.side_effect = error
    monkeypatch.delenv("ANY_LLM_UNIFIED_EXCEPTIONS", raising=False)
    monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: primary_client)
    fallback_result = {
        "headline": "Fallback briefing",
        "headline_source_ids": ["source"],
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": True,
    }
    fallback_client = _CompletionClientStub(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(fallback_result)))])
    )
    provider = FallbackLLMProvider(
        create_any_llm_provider("openai", "primary-model", 4096),
        AnyLLMStructuredProvider(
            fallback_client,
            provider="openai",
            model="fallback-model",
            max_output_tokens=4096,
        ),
        primary_name="openai/primary-model",
        fallback_name="openai/fallback-model",
    )

    result = await provider.summarize("Return JSON", {"input": "data"})

    assert result == fallback_result
    primary_client.acompletion.assert_awaited_once()
    assert len(fallback_client.calls) == 1


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
    assert all(
        "default_headers" not in options and "http_client" not in options and "max_retries" not in options
        for _, options in created
    )


@pytest.mark.parametrize("provider", ("anthropic", "deepseek"))
def test_factory_passes_configured_headers_through_client_args(monkeypatch, provider: str) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    def fake_create(provider: str, **options: object) -> _CompletionClientStub:
        created.append((provider, options))
        return _CompletionClientStub(SimpleNamespace())

    monkeypatch.setattr(AnyLLM, "create", fake_create)
    headers = {"User-Agent": "weather-briefing/1", "X-Tenant": "test"}

    create_any_llm_provider(provider, "model", 1024, extra_headers=headers)

    assert created == [
        (
            provider,
            {
                "api_key": None,
                "api_base": None,
                "default_headers": headers,
            },
        )
    ]


def test_factory_uses_the_canonical_provider_for_header_validation(monkeypatch) -> None:
    create = Mock()
    monkeypatch.setattr(AnyLLM, "create", create)

    with pytest.raises(ValueError, match="Custom headers are not supported for any-llm provider: mistral"):
        create_any_llm_provider(
            "MISTRAL",
            "model",
            1024,
            extra_headers={"User-Agent": "weather-briefing/1"},
        )

    create.assert_not_called()


def test_factory_rejects_headers_for_an_unsupported_provider(monkeypatch) -> None:
    create = Mock()
    monkeypatch.setattr(AnyLLM, "create", create)

    with pytest.raises(ValueError, match="Custom headers are not supported for any-llm provider: mistral"):
        create_any_llm_provider(
            "mistral",
            "model",
            1024,
            extra_headers={"User-Agent": "weather-briefing/1"},
        )

    create.assert_not_called()


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


@pytest.mark.parametrize("provider_name", ("deepseek", "openai", "openrouter"))
async def test_openai_compatible_providers_send_configured_headers(
    monkeypatch,
    caplog,
    provider_name: str,
) -> None:
    requests: list[httpx.Request] = []
    private_header_name = "X-Private-Token"
    private_header_value = "private-value"
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

    def init_client(
        sdk_provider: BaseOpenAIProvider,
        api_key: str | None = None,
        api_base: str | None = None,
        *,
        default_headers: Mapping[str, str] | None = None,
        **_: object,
    ) -> None:
        sdk_provider.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers=default_headers,
            http_client=LoggedAsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(BaseOpenAIProvider, "_init_client", init_client)
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    provider = create_any_llm_provider(
        provider_name,
        "requested-model",
        4096,
        api_key="runtime-key",
        api_base="https://api.example.invalid",
        extra_headers={
            "User-Agent": "weather-briefing-test/1",
            private_header_name: private_header_value,
        },
    )

    try:
        result = await provider.summarize("Return JSON", {"input": "data"})
    finally:
        await provider.aclose()

    assert result == model_result
    assert requests[0].headers["user-agent"] == "weather-briefing-test/1"
    assert requests[0].headers[private_header_name] == private_header_value
    assert private_header_name not in caplog.text
    assert private_header_value not in caplog.text


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
