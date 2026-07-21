import logging

import httpx
import pytest

from weather_briefing.api_client import LoggedAsyncClient, api_call_context, api_call_extensions


async def test_logged_client_records_annotated_success_without_request_data(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    private_url = "https://private.example.test/secret/path?token=secret"

    async with LoggedAsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="private response"))
    ) as client:
        await client.get(
            private_url,
            headers={"Authorization": "Bearer secret"},
            extensions=api_call_extensions("open-meteo", "weather-forecast"),
        )

    assert "API call started provider=open-meteo operation=weather-forecast method=GET" in caplog.text
    assert "API call succeeded provider=open-meteo operation=weather-forecast method=GET" in caplog.text
    assert "status_code=200" in caplog.text
    assert "private.example.test" not in caplog.text
    assert "secret" not in caplog.text
    assert "private response" not in caplog.text


async def test_logged_client_records_http_failure_status(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")

    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        response = await client.post(
            "https://example.test/api",
            extensions=api_call_extensions("telegram", "send-message"),
        )

    assert response.status_code == 503
    assert "API call failed provider=telegram operation=send-message method=POST" in caplog.text
    assert "status_code=503" in caplog.text


async def test_logged_client_leaves_handled_response_error_warning_to_adapter(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")

    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400))) as client:
        response = await client.post(
            "https://example.test/api",
            extensions=api_call_extensions(
                "telegram",
                "send-message",
                response_error_handled=True,
            ),
        )

    assert response.status_code == 400
    assert "API call returned handled error provider=telegram operation=send-message" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.parametrize("value", (None, 0, 1, "true"))
def test_api_call_extensions_rejects_non_boolean_response_error_ownership(value) -> None:
    with pytest.raises(TypeError, match="response_error_handled must be a bool"):
        api_call_extensions("telegram", "send-message", response_error_handled=value)


async def test_logged_client_records_exception_type_without_message(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private transport detail", request=request)

    async with LoggedAsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(httpx.ConnectError, match="private transport detail"):
            await client.get(
                "https://example.test/api",
                extensions=api_call_extensions("aqicn", "air-quality"),
            )

    assert "API call failed provider=aqicn operation=air-quality method=GET" in caplog.text
    assert "reason=ConnectError" in caplog.text
    assert "private transport detail" not in caplog.text


async def test_logged_client_records_unannotated_requests_as_unclassified(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")

    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        await client.get("https://example.test/api")

    assert "API call started provider=unclassified operation=request method=GET" in caplog.text


async def test_context_classifies_sdk_request_without_extensions(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with api_call_context("deepseek", "chat-completions"):
            await client.get("https://private.example.invalid/v1/chat/completions")

    assert "API call started provider=deepseek operation=chat-completions method=GET" in caplog.text


async def test_request_extensions_override_sdk_context(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with api_call_context("deepseek", "chat-completions"):
            await client.get(
                "https://private.example.invalid/weather",
                extensions=api_call_extensions("open-meteo", "weather-forecast"),
            )

    assert "API call started provider=open-meteo operation=weather-forecast method=GET" in caplog.text


@pytest.mark.parametrize(
    "identity",
    ("invalid", ("provider",), (1, "operation"), ("unsafe label", "operation")),
)
async def test_logged_client_treats_malformed_metadata_as_unclassified(caplog, identity: object) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")

    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        await client.get(
            "https://example.test/api",
            extensions={"weather_briefing.api_call": identity},
        )

    assert "provider=unclassified operation=request" in caplog.text


async def test_logged_client_rejects_untrusted_method_in_log(caplog) -> None:
    caplog.set_level(logging.INFO, logger="weather_briefing.api_client")
    request = httpx.Request(
        "GET\nforged-log-line",
        "https://example.test/api",
        extensions=api_call_extensions("provider", "operation"),
    )

    async with LoggedAsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        await client.send(request)

    assert "method=INVALID" in caplog.text
    assert "forged-log-line" not in caplog.text


@pytest.mark.parametrize("label", ("", "Open-Meteo", "private endpoint", "line\nbreak"))
def test_api_call_extensions_rejects_unsafe_labels(label: str) -> None:
    with pytest.raises(ValueError, match="lowercase kebab-case"):
        api_call_extensions(label, "operation")
    with pytest.raises(ValueError, match="lowercase kebab-case"):
        api_call_extensions("provider", label)
