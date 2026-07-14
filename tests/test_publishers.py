import json

import httpx
import pytest

from weather_briefing.models import RenderedMessage
from weather_briefing.publishers import (
    DeliveryError,
    DeliveryProvider,
    StdoutPublisher,
    TelegramPublisher,
    _safe_html_boundary,
    _split_message,
)
from weather_briefing.render import PlainTextRenderer


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

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        self.messages.append(message)


def test_delivery_provider_applies_platform_limit_without_leaking_it_into_config() -> None:
    unrestricted = DeliveryProvider(PlainTextRenderer(), NoopPublisher())
    telegram_like = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), 4096)

    assert unrestricted.briefing_limit(5000) == 5000
    assert telegram_like.briefing_limit(5000) == 4096
    assert telegram_like.briefing_limit(3500) == 3500


def test_split_message_prefers_line_boundary() -> None:
    assert _split_message("first line\nsecond line", 12) == ("first line", "second line")


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
    assert "Telegram delivery prepared: visible_characters=11 payload_characters=18 chunks=1" in caplog.text
    assert "Telegram chunk accepted: index=1/1 payload_characters=18" in caplog.text
    assert (
        "Sensitive rendered text diagnostic: stage=telegram-chunk-1-of-1 body='<b>Title</b>\\n\\nBody'"
    ) in caplog.text
    assert "runtime-token" not in caplog.text
    assert "runtime-chat" not in caplog.text


async def test_telegram_publisher_uses_bot_api_silent_delivery() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        await publisher.publish(RenderedMessage("Final briefing", 14), silent=True)

    payload = json.loads(requests[0].content)
    assert payload["disable_notification"] is True


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


async def test_telegram_error_does_not_expose_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(client, "private-token", "runtime-chat")
        with pytest.raises(DeliveryError) as caught:
            await publisher.publish(RenderedMessage("Body", 4))

    assert "private-token" not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_telegram_rejects_oversized_single_message_before_delivery() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        with pytest.raises(DeliveryError, match="exceeds"):
            await publisher.publish(RenderedMessage("<b>short markup</b>", 4097), single_message=True)


async def test_stdout_publisher_outputs_message_body(capsys) -> None:
    await StdoutPublisher().publish(RenderedMessage("test body", 9))

    assert capsys.readouterr().out.strip() == "test body"


def test_safe_html_boundary_avoids_splitting_inside_html_entity() -> None:
    assert _safe_html_boundary("text with &amp more text", 16) == 10


def test_safe_html_boundary_avoids_splitting_inside_html_tag() -> None:
    assert _safe_html_boundary("text <b>bold</b>", 7) == 5


def test_safe_html_boundary_returns_limit_when_no_boundary_issue() -> None:
    assert _safe_html_boundary("plain text without html", 10) == 10


def test_safe_html_boundary_returns_limit_when_boundary_is_zero() -> None:
    assert _safe_html_boundary("<tag", 1) == 1
