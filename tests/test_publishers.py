import json
from typing import Any

import httpx
import pytest

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.data import resources as data_resources
from weather_briefing.data.resources import ReferenceDataError
from weather_briefing.delivery import (
    DeliveryError,
    DeliveryProvider,
    PlainTextRenderer,
    StdoutPublisher,
    TelegramPublisher,
)
from weather_briefing.delivery.telegram import split_message
from weather_briefing.delivery.telegram_reference import telegram_error_classification
from weather_briefing.models import RenderedMessage


class NoopPublisher:
    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        pass


class EnabledDiagnostics:
    def rendered_text_logging_enabled(self) -> bool:
        return True


class FailingDiagnostics:
    def rendered_text_logging_enabled(self) -> bool:
        raise RuntimeError("diagnostic state unavailable")


class CountingDiagnostics:
    def __init__(self) -> None:
        self.checks = 0

    def rendered_text_logging_enabled(self) -> bool:
        self.checks += 1
        return True


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[RenderedMessage] = []
        self.hints: list[tuple[bool, bool]] = []

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        self.messages.append(message)
        self.hints.append((single_message, silent))


def test_delivery_provider_applies_platform_limit_without_leaking_it_into_config() -> None:
    unrestricted = DeliveryProvider(PlainTextRenderer(), NoopPublisher())
    telegram_like = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), 4096)
    bark_like = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), 650, 2)

    assert unrestricted.briefing_limit(5000) == 5000
    assert telegram_like.briefing_limit(5000) == 4096
    assert telegram_like.briefing_limit(3500) == 3500
    assert bark_like.briefing_target(1300) == 650
    assert bark_like.briefing_limit(5000) == 1300
    assert bark_like.briefing_limit(500) == 1000


@pytest.mark.parametrize(
    ("single_message_limit", "briefing_max_messages", "message"),
    (
        (None, 0, "must be positive"),
        (None, -1, "must be positive"),
        (None, False, "must be positive"),
        (None, True, "must be positive"),
        (None, 1.5, "must be positive"),
        (None, 2, "require a single_message_limit"),
    ),
)
def test_delivery_provider_rejects_invalid_briefing_chunk_policy(
    single_message_limit: int | None,
    briefing_max_messages: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DeliveryProvider(
            PlainTextRenderer(),
            NoopPublisher(),
            single_message_limit=single_message_limit,
            briefing_max_messages=briefing_max_messages,
        )


@pytest.mark.parametrize("single_message_limit", (0, -1, False, True, 1.5, "650"))
def test_delivery_provider_rejects_invalid_single_message_limit(single_message_limit: Any) -> None:
    with pytest.raises(ValueError, match="single_message_limit must be positive"):
        DeliveryProvider(
            PlainTextRenderer(),
            NoopPublisher(),
            single_message_limit=single_message_limit,
        )


@pytest.mark.parametrize(("single_message_limit", "briefing_max_messages"), ((None, 1), (650, 2)))
def test_delivery_provider_accepts_positive_integer_briefing_chunk_policy(
    single_message_limit: int | None,
    briefing_max_messages: int,
) -> None:
    delivery = DeliveryProvider(
        PlainTextRenderer(),
        NoopPublisher(),
        single_message_limit=single_message_limit,
        briefing_max_messages=briefing_max_messages,
    )

    assert delivery.briefing_max_messages == briefing_max_messages


async def test_delivery_provider_allows_configured_briefing_splits() -> None:
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher, 650, 2)

    await delivery.publish_briefing(RenderedMessage("briefing", 8), silent=True)

    assert publisher.hints == [(False, True)]


async def test_delivery_provider_rejects_briefing_beyond_configured_chunk_count() -> None:
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher, 650, 2)

    with pytest.raises(DeliveryError, match="delivery limit") as error:
        await delivery.publish_briefing(RenderedMessage("x" * 1301, 1301))

    assert error.value.reason == "message-too-long"
    assert publisher.messages == []


async def test_telegram_publisher_validates_error_metadata_on_construction(monkeypatch) -> None:
    monkeypatch.setattr(data_resources, "load_reference_data", lambda filename: {})
    telegram_error_classification.cache_clear()

    async with httpx.AsyncClient() as client:
        with pytest.raises(ReferenceDataError, match="supported fields"):
            TelegramPublisher(client, "runtime-token", "runtime-chat")


@pytest.mark.parametrize("reason", (None, 7, "private detail\nforged-log-line"))
def test_delivery_error_rejects_unsafe_structured_reason(reason) -> None:
    with pytest.raises(ValueError, match="lowercase kebab-case"):
        DeliveryError("Delivery failed", reason=reason)


@pytest.mark.parametrize("value", (None, 0, 1, "true"))
def test_delivery_error_rejects_non_boolean_channel_availability(value) -> None:
    with pytest.raises(TypeError, match="channel_unavailable must be a bool"):
        DeliveryError("Delivery failed", reason="request-error", channel_unavailable=value)


def test_split_message_prefers_line_boundary() -> None:
    assert split_message("first line\nsecond line", 12) == ("first line", "\nsecond line")


def test_split_message_preserves_markup_without_visible_text() -> None:
    assert split_message("<b></b>", 3) == ("<b></b>",)


@pytest.mark.parametrize(("body", "limit"), (("", 0), ("body", 0), ("body", -1)))
def test_split_message_rejects_non_positive_limit(body: str, limit: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        split_message(body, limit)


@pytest.mark.parametrize(
    ("body", "limit", "expected"),
    (
        ("<b>abcdefgh</b>", 5, ("<b>abcde</b>", "<b>fgh</b>")),
        (
            "<b><i>abcdef</i></b>",
            3,
            ("<b><i>abc</i></b>", "<b><i>def</i></b>"),
        ),
        ("<b>ab&amp;cd</b>", 3, ("<b>ab&amp;</b>", "<b>cd</b>")),
        ("<b>ab&#38;cd</b>", 3, ("<b>ab&#38;</b>", "<b>cd</b>")),
        ("<b>abc&amp;d</b>", 3, ("<b>abc</b>", "<b>&amp;d</b>")),
        ("<b>abc</b><i>def</i>", 3, ("<b>abc</b>", "<i>def</i>")),
        ("<b>abc</b><i></i>", 3, ("<b>abc</b>",)),
        (
            "<b>first line\nsecond line</b>",
            12,
            ("<b>first line</b>", "<b>\nsecond line</b>"),
        ),
    ),
)
def test_split_message_balances_html_tags(
    body: str,
    limit: int,
    expected: tuple[str, ...],
) -> None:
    assert split_message(body, limit) == expected


async def test_telegram_publisher_uses_runtime_values(caplog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = TelegramPublisher(client, "runtime-token", "runtime-chat", EnabledDiagnostics())
            await publisher.publish(RenderedMessage("<b>Title</b>\n\nBody", 11))

    assert requests[0].url.path == "/botruntime-token/sendMessage"
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "runtime-chat"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_notification"] is False
    assert (
        "Telegram delivery prepared: visible_characters=11 payload_characters=18 chunks=1 "
        "single_message=False silent=False"
    ) in caplog.text
    assert "Telegram chunk accepted: index=1/1 payload_characters=18" in caplog.text
    assert (
        "Sensitive rendered text diagnostic: stage=telegram-chunk-1-of-1 body='<b>Title</b>\\n\\nBody'"
    ) in caplog.text
    assert "runtime-token" not in caplog.text
    assert "runtime-chat" not in caplog.text


async def test_telegram_publisher_uses_bot_api_silent_delivery(caplog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
            await publisher.publish(RenderedMessage("Final briefing", 14), silent=True)

    payload = json.loads(requests[0].content)
    assert payload["disable_notification"] is True
    assert "single_message=False silent=True" in caplog.text


async def test_telegram_checks_runtime_diagnostics_once_for_multiple_chunks(caplog) -> None:
    diagnostics = CountingDiagnostics()
    body = "x" * (TelegramPublisher.MAX_MESSAGE_LENGTH + 1)

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            publisher = TelegramPublisher(client, "runtime-token", "runtime-chat", diagnostics)
            await publisher.publish(RenderedMessage(body, len(body)))

    assert diagnostics.checks == 1
    assert caplog.text.count("Sensitive rendered text diagnostic: stage=telegram-chunk-") == 2


async def test_rendered_text_is_not_logged_without_runtime_diagnostics(caplog) -> None:
    delivery = DeliveryProvider(PlainTextRenderer(), NoopPublisher())

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        await delivery.publish_alert("Private diagnostic title", "Private diagnostic body")

    assert "Private diagnostic" not in caplog.text


async def test_delivery_logs_rendered_text_when_runtime_diagnostics_are_enabled(caplog) -> None:
    delivery = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), diagnostics=EnabledDiagnostics())

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        await delivery.publish_alert("Diagnostic title", "Diagnostic body")

    assert "Sensitive rendered text diagnostic: stage=alert body='Diagnostic title\\n\\nDiagnostic body'" in caplog.text


async def test_runtime_diagnostics_are_checked_without_debug_logging(caplog) -> None:
    diagnostics = CountingDiagnostics()
    delivery = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), diagnostics=diagnostics)

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        await delivery.publish_alert("Private diagnostic title", "Private diagnostic body")

    assert diagnostics.checks == 1
    assert "Private diagnostic" not in caplog.text


async def test_runtime_diagnostic_failure_does_not_block_delivery(caplog) -> None:
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher, diagnostics=FailingDiagnostics())

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        await delivery.publish_alert("Diagnostic title", "Diagnostic body")

    assert publisher.messages == [RenderedMessage("Diagnostic title\n\nDiagnostic body", 33)]
    assert "Rendered text diagnostic state check failed" in caplog.text


async def test_telegram_error_logs_safe_api_reason_without_private_response(caplog) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found; private response detail",
            },
        )

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = TelegramPublisher(client, "private-token", "runtime-chat")
            with pytest.raises(DeliveryError, match="chat-not-found") as caught:  # pragma: no branch
                await publisher.publish(RenderedMessage("Body", 4))

    assert "private-token" not in str(caught.value)
    assert "runtime-chat" not in caplog.text
    assert "private response detail" not in caplog.text
    assert "Telegram delivery prepared: visible_characters=4 payload_characters=4 chunks=1" in caplog.text
    assert "status_code=400 reason=chat-not-found" in caplog.text
    assert caught.value.__cause__ is None
    assert caught.value.reason == "chat-not-found"
    assert caught.value.channel_unavailable is True


async def test_telegram_failure_emits_one_warning_with_classification(caplog) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json={"description": "chat not found"}))

    with caplog.at_level("INFO"):
        async with LoggedAsyncClient(transport=transport) as client:
            publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
            with pytest.raises(DeliveryError, match="chat-not-found"):  # pragma: no branch
                await publisher.publish(RenderedMessage("Body", 4))

    warnings = [record for record in caplog.records if record.levelno == 30]
    assert len(warnings) == 1
    assert warnings[0].name == "weather_briefing.publishers"
    assert warnings[0].getMessage().endswith("status_code=400 reason=chat-not-found")


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_reason", "expected_channel_unavailable"),
    (
        (400, {"description": "Bad Request: can't parse entities at byte offset 12"}, "invalid-html", False),
        (400, {"description": "Bad Request: message is too long"}, "message-too-long", False),
        (
            400,
            {"description": "Bad Request: not enough rights to send text messages"},
            "insufficient-rights",
            True,
        ),
        (400, {"parameters": {"migrate_to_chat_id": -100123}}, "chat-migrated", True),
        (401, {"description": "Unauthorized"}, "bot-token-rejected", True),
        (404, {"description": "Not Found"}, "bot-endpoint-not-found", True),
        (429, {"description": "Too Many Requests: retry later"}, "rate-limited", False),
        (400, {"description": 123}, "api-error", False),
        (500, {"description": "private provider detail"}, "api-error", False),
    ),
)
async def test_telegram_error_classification(
    status_code: int,
    payload: dict[str, object],
    expected_reason: str,
    expected_channel_unavailable: bool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, json=payload))
    ) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        with pytest.raises(DeliveryError, match=expected_reason) as caught:
            await publisher.publish(RenderedMessage("Body", 4))

    assert caught.value.channel_unavailable is expected_channel_unavailable


async def test_telegram_malformed_error_response_uses_status_classification() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="private provider response"))
    ) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        with pytest.raises(DeliveryError, match="forbidden"):
            await publisher.publish(RenderedMessage("Body", 4))


async def test_telegram_request_error_logs_chunk_context_without_private_detail(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
            with pytest.raises(DeliveryError, match="request-error"):  # pragma: no branch
                await publisher.publish(RenderedMessage("Body", 4))

    assert "Telegram delivery request failed index=1/1 message_visible_characters=4 payload_characters=4" in caplog.text
    assert "reason=ConnectError" in caplog.text
    assert "private network detail" not in caplog.text


async def test_telegram_rejects_oversized_single_message_before_delivery() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        with pytest.raises(DeliveryError, match="exceeds") as caught:
            await publisher.publish(RenderedMessage("<b>short markup</b>", 4097), single_message=True)

    assert caught.value.reason == "message-too-long"
    assert caught.value.channel_unavailable is False


async def test_stdout_publisher_outputs_message_body(capsys) -> None:
    await StdoutPublisher().publish(RenderedMessage("test body", 9))

    assert capsys.readouterr().out.strip() == "test body"
