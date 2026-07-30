import json

import httpx
import pytest

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.delivery.base import DeliveryError
from weather_briefing.delivery.serverchan import (
    ServerChan3Publisher,
    ServerChanTurboPublisher,
    serverchan_error_reason,
)
from weather_briefing.models import RenderedMessage


class EnabledDiagnostics:
    def rendered_text_logging_enabled(self) -> bool:
        return True


@pytest.mark.parametrize("sendkey", ("", "sctp123tToken", "SCTbad-key"))
async def test_serverchan_turbo_rejects_non_turbo_sendkeys(sendkey: str) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="must start with SCT"):
            ServerChanTurboPublisher(client, sendkey)


@pytest.mark.parametrize("sendkey", ("", "SCTtoken", "sctp1t!"))
async def test_serverchan_3_rejects_non_sc3_sendkeys(sendkey: str) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match=r"sctp\{uid\}t\{token\}"):
            ServerChan3Publisher(client, sendkey)


async def test_serverchan_turbo_uses_the_turbo_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "message": "success"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = ServerChanTurboPublisher(client, "SCTprivate123")
        await publisher.publish(
            RenderedMessage("**Weather** details", 31, "Weather"),
            single_message=True,
            silent=True,
        )

    request = requests[0]
    assert request.url.host == "sctapi.ftqq.com"
    assert request.url.path == "/SCTprivate123.send"
    assert json.loads(request.content) == {
        "title": "Weather",
        "desp": "**Weather** details",
    }


async def test_serverchan_3_uses_its_uid_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "message": "success"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = ServerChan3Publisher(client, "sctp123tPrivate")
        await publisher.publish(RenderedMessage("Weather details", 22, "Weather"))

    request = requests[0]
    assert request.url.host == "123.push.ft07.com"
    assert request.url.path == "/send/sctp123tPrivate.send"
    assert json.loads(request.content) == {
        "title": "Weather",
        "desp": "Weather details",
    }


@pytest.mark.parametrize("title", (None, "line one\nline two", "x" * 33))
async def test_serverchan_publishers_reject_invalid_titles_before_delivery(title: str | None) -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("ServerChan request reached transport"))
    async with httpx.AsyncClient(transport=transport) as client:
        publisher = ServerChanTurboPublisher(client, "SCTprivate")
        with pytest.raises(DeliveryError, match="title is invalid") as caught:
            await publisher.publish(RenderedMessage("Body", 4, title))

    assert caught.value.reason == "invalid-title"


async def test_serverchan_diagnostics_include_message_but_not_sendkey(caplog) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 0, "message": "success"}))

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=transport) as client:
            publisher = ServerChanTurboPublisher(client, "SCTprivate", EnabledDiagnostics())
            await publisher.publish(RenderedMessage("Private body", 25, "Private title"))

    assert "Private title" in caplog.text
    assert "Private body" in caplog.text
    assert "SCTprivate" not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected_reason", "channel_unavailable"),
    (
        (401, "unauthorized", True),
        (403, "forbidden", True),
        (404, "endpoint-not-found", True),
        (413, "message-too-long", False),
        (429, "rate-limited", False),
        (500, "api-error", False),
    ),
)
def test_serverchan_http_error_classification(
    status_code: int,
    expected_reason: str,
    channel_unavailable: bool,
) -> None:
    response = httpx.Response(status_code)

    assert serverchan_error_reason(response) == (expected_reason, channel_unavailable)


async def test_serverchan_http_error_does_not_log_private_values(caplog) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(403, json={"message": "private provider detail"}))

    caplog.set_level("WARNING", logger="httpx")
    with caplog.at_level("INFO", logger="weather_briefing"):
        async with LoggedAsyncClient(transport=transport) as client:
            publisher = ServerChan3Publisher(client, "sctp123tPrivate")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match="forbidden") as caught:  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 25, "Private title"))

    assert caught.value.channel_unavailable is True
    assert "sctp123tPrivate" not in caplog.text
    assert "Private title" not in caplog.text
    assert "Private body" not in caplog.text
    assert "private provider detail" not in caplog.text


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    (
        (httpx.Response(200, text="not-json"), "invalid-response"),
        (httpx.Response(200, json=["not", "an", "object"]), "invalid-response"),
        (httpx.Response(200, json={"code": True, "message": "success"}), "invalid-response"),
        (httpx.Response(200, json={"code": 0, "message": 1}), "invalid-response"),
        (httpx.Response(200, json={"code": 1, "message": "private detail"}), "api-error"),
    ),
)
async def test_serverchan_rejects_unsuccessful_responses(
    response: httpx.Response,
    expected_reason: str,
    caplog,
) -> None:
    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            publisher = ServerChanTurboPublisher(client, "SCTprivate")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match=expected_reason):  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 25, "Private title"))

    assert "private detail" not in caplog.text
    assert "Private title" not in caplog.text
    assert "Private body" not in caplog.text


async def test_serverchan_request_error_does_not_log_private_values(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = ServerChanTurboPublisher(client, "SCTprivate")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match="request-error"):  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 25, "Private title"))

    assert "private network detail" not in caplog.text
    assert "SCTprivate" not in caplog.text
    assert "Private title" not in caplog.text
    assert "Private body" not in caplog.text
