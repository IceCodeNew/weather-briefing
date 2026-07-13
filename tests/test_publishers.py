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
    async def publish(self, message: RenderedMessage, *, single_message: bool = False) -> None:
        pass


def test_delivery_provider_applies_platform_limit_without_leaking_it_into_config() -> None:
    unrestricted = DeliveryProvider(PlainTextRenderer(), NoopPublisher())
    telegram_like = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), 4096)

    assert unrestricted.briefing_limit(5000) == 5000
    assert telegram_like.briefing_limit(5000) == 4096
    assert telegram_like.briefing_limit(3500) == 3500


def test_split_message_prefers_line_boundary() -> None:
    assert _split_message("first line\nsecond line", 12) == ("first line", "second line")


async def test_telegram_publisher_uses_runtime_values() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(client, "runtime-token", "runtime-chat")
        await publisher.publish(RenderedMessage("<b>Title</b>\n\nBody", 11))

    assert requests[0].url.path == "/botruntime-token/sendMessage"
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "runtime-chat"
    assert payload["parse_mode"] == "HTML"


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
