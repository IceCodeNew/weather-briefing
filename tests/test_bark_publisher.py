import base64
import json
import re

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from weather_briefing.api_client import LoggedAsyncClient
from weather_briefing.delivery.bark import BarkPublisher, bark_error_reason, split_plain_message
from weather_briefing.delivery.bark_crypto import BarkEncryptor
from weather_briefing.delivery.base import DeliveryError
from weather_briefing.models import RenderedMessage


class EnabledDiagnostics:
    def rendered_text_logging_enabled(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("key", "expected_ciphertext"),
    (
        ("k" * 16, "M31imDGJqdEEszZWcWfgiMTooJniyhmvnRIIrUSrWDVi3FxcD9cl1Dyre2wQ8a5I2bhFyBrGPeMEwp1+Uw=="),
        ("k" * 24, "QyuvztP2Yktq2khUWXw6hYrjLnhuc+kC44lyGOaVYBRJC4vsYaB6RewayvX+4ZKjUUIifRWDtIiVFq/KgA=="),
        ("k" * 32, "a9CkBbe9d0qkskyAzKyb4pV/WjH6D1J/JLm0EOUTMcoTpJEs10gqANkkLKaOPqVXL0yo0AaxElYjmLmynw=="),
    ),
)
def test_bark_encryptor_matches_the_app_gcm_wire_format(
    key: str,
    expected_ciphertext: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr("secrets.token_urlsafe", lambda _: "fixed-iv-123")
    encryptor = BarkEncryptor(key, "initial-iv-1")

    encrypted = encryptor.encrypt({"body": "Encrypted weather", "level": "active"})

    assert encrypted.iv == "fixed-iv-123"
    assert encrypted.ciphertext == expected_ciphertext
    plaintext = AESGCM(key.encode()).decrypt(
        encrypted.iv.encode(),
        base64.b64decode(encrypted.ciphertext),
        None,
    )
    assert json.loads(plaintext) == {"body": "Encrypted weather", "level": "active"}
    assert len(base64.b64decode(encrypted.ciphertext)) == len(plaintext) + 16


@pytest.mark.parametrize("key", ("short", "k" * 17, "密" * 16))
def test_bark_encryptor_rejects_incompatible_app_keys(key: str) -> None:
    with pytest.raises(ValueError, match="Bark encryption key"):
        BarkEncryptor(key, "fixed-iv-123")


@pytest.mark.parametrize("iv", ("short", "longer-than-twelve", "密" * 12))
def test_bark_encryptor_rejects_incompatible_app_ivs(iv: str) -> None:
    with pytest.raises(ValueError, match="Bark GCM IV"):
        BarkEncryptor("k" * 32, iv)


async def test_bark_publisher_rejects_empty_device_key() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="device key must not be empty"):
            BarkPublisher(client, "", "k" * 32, "fixed-iv-123")


async def test_bark_publisher_rejects_partial_encryption_config() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="key and IV must be configured together"):
            BarkPublisher(client, "device", "k" * 32)
        with pytest.raises(ValueError, match="key and IV must be configured together"):
            BarkPublisher(client, "device", encryption_iv="fixed-iv-123")


async def test_bark_publisher_sends_plaintext_when_encryption_is_not_configured() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = BarkPublisher(
            client,
            "private/device",
            base_url="https://bark.example.invalid/prefix",
            group="custom-group",
        )
        await publisher.publish(RenderedMessage("Plain weather body", 31, "Weather title"))

    request = requests[0]
    assert request.url.path == "/prefix/push"
    assert json.loads(request.content) == {
        "device_key": "private/device",
        "body": "Plain weather body",
        "group": "custom-group",
        "level": "timeSensitive",
        "title": "Weather title",
    }


async def test_bark_publisher_omits_empty_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = BarkPublisher(client, "device")
        await publisher.publish(RenderedMessage("Weather body", 12, ""))

    assert "title" not in json.loads(requests[0].content)


async def test_bark_publisher_rejects_empty_group() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="group must not be empty"):
            BarkPublisher(client, "device", group="")


async def test_bark_publisher_sends_only_encrypted_runtime_values(caplog) -> None:
    requests: list[httpx.Request] = []
    key = "k" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "message": "success", "timestamp": 1})

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = BarkPublisher(
                client,
                "private/device",
                key,
                "fixed-iv-123",
                EnabledDiagnostics(),
                base_url="https://bark.example.invalid",
                encryptor=BarkEncryptor(key, "fixed-iv-123"),
            )
            await publisher.publish(RenderedMessage("Private weather body", 33, "Private title"), silent=True)

    request = requests[0]
    request_payload = json.loads(request.content)
    assert len(request_payload["iv"]) == 12
    assert re.fullmatch(r"[A-Za-z0-9_-]{12}", request_payload["iv"])
    plaintext = AESGCM(key.encode()).decrypt(
        request_payload["iv"].encode(),
        base64.b64decode(request_payload["ciphertext"]),
        None,
    )
    assert request.url.path == "/push"
    assert request_payload["device_key"] == "private/device"
    assert json.loads(plaintext) == {
        "body": "Private weather body",
        "group": "weather-briefing",
        "level": "passive",
        "title": "Private title",
    }
    assert "Private weather body" not in request.content.decode()
    assert "Private title" not in request.content.decode()
    assert "Private weather body" in caplog.text
    assert "Private title" in caplog.text
    assert "private/device" not in caplog.text
    assert key not in caplog.text
    assert "Bark chunk accepted: index=1/1 payload_characters=33" in caplog.text


async def test_bark_publisher_rotates_the_iv_for_each_chunk(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    generated_ivs = iter(("abcdefghijkl", "mnopqrstuvwx"))
    monkeypatch.setattr("secrets.token_urlsafe", lambda _: next(generated_ivs))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = BarkPublisher(client, "device", "k" * 32, "fixed-iv-123")
        body = "x" * (BarkPublisher.MAX_MESSAGE_LENGTH + 1)
        await publisher.publish(RenderedMessage(body, len(body)))

    ivs = [json.loads(request.content)["iv"] for request in requests]
    assert ivs == ["abcdefghijkl", "mnopqrstuvwx"]


async def test_bark_rejects_oversized_single_message_before_delivery() -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("Bark request reached transport"))
    async with httpx.AsyncClient(transport=transport) as client:
        publisher = BarkPublisher(client, "device", "k" * 32, "fixed-iv-123")
        with pytest.raises(DeliveryError, match="exceeds") as caught:
            await publisher.publish(RenderedMessage("x" * 651, 651), single_message=True)

    assert caught.value.reason == "message-too-long"


async def test_bark_publisher_repeats_title_and_reserves_its_length_for_each_chunk() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    title = "t" * 10
    body = "b" * 641
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = BarkPublisher(client, "device")
        await publisher.publish(RenderedMessage(body, 651, title))

    payloads = [json.loads(request.content) for request in requests]
    assert [payload["title"] for payload in payloads] == [title, title]
    assert [len(payload["body"]) for payload in payloads] == [640, 1]
    assert all(len(payload["title"]) + len(payload["body"]) <= 650 for payload in payloads)


async def test_bark_publisher_logs_repeated_title_payload_characters(caplog) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 200}))
    title = "t" * 10
    body = "b" * 641

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=transport) as client:
            publisher = BarkPublisher(client, "device")
            await publisher.publish(RenderedMessage(body, 651, title))

    assert "payload_characters=661 chunks=2" in caplog.text


async def test_bark_publisher_rejects_title_that_leaves_no_body_capacity() -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("Bark request reached transport"))
    async with httpx.AsyncClient(transport=transport) as client:
        publisher = BarkPublisher(client, "device")
        with pytest.raises(DeliveryError, match="exceeds") as caught:
            await publisher.publish(RenderedMessage("body", 654, "t" * 650))

    assert caught.value.reason == "message-too-long"


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_reason", "channel_unavailable"),
    (
        (400, {"message": "failed to get device token: private detail"}, "device-key-rejected", True),
        (400, {"message": "APNs PayloadTooLarge"}, "message-too-long", False),
        (401, {}, "unauthorized", False),
        (403, {}, "forbidden", False),
        (404, {}, "endpoint-not-found", True),
        (413, {}, "message-too-long", False),
        (429, {}, "rate-limited", False),
        (500, {"message": "private provider detail"}, "api-error", False),
    ),
)
def test_bark_error_classification(
    status_code: int,
    payload: dict[str, object],
    expected_reason: str,
    channel_unavailable: bool,
) -> None:
    response = httpx.Response(status_code, json=payload)

    assert bark_error_reason(response) == (expected_reason, channel_unavailable)


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(500, text="not-json"),
        httpx.Response(500, json=["not", "an", "object"]),
    ),
)
def test_bark_malformed_error_response_uses_status_classification(response: httpx.Response) -> None:
    assert bark_error_reason(response) == ("api-error", False)


async def test_bark_error_logs_safe_reason_without_private_response(caplog) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(400, json={"message": "failed to get device token: private detail"})
    )

    with caplog.at_level("INFO"):
        async with LoggedAsyncClient(transport=transport) as client:
            publisher = BarkPublisher(client, "private-device", "k" * 32, "fixed-iv-123")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match="device-key-rejected") as caught:  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 12))

    assert "private-device" not in caplog.text
    assert "Private body" not in caplog.text
    assert "private detail" not in caplog.text
    assert caught.value.channel_unavailable is True
    warnings = [record for record in caplog.records if record.levelno == 30]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"code": "200"}),
        httpx.Response(200, json={"code": 500, "message": "private detail"}),
    ),
)
async def test_bark_rejects_invalid_success_response(response: httpx.Response, caplog) -> None:
    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            publisher = BarkPublisher(client, "private-device", "k" * 32, "fixed-iv-123")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match="invalid-response"):  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 12))

    assert "private detail" not in caplog.text
    assert "Private body" not in caplog.text


async def test_bark_request_error_does_not_log_private_detail(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    with caplog.at_level("INFO", logger="weather_briefing.publishers"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = BarkPublisher(client, "private-device", "k" * 32, "fixed-iv-123")
            # The alternate pytest.raises exit is assertion machinery and adds no behavior coverage.
            with pytest.raises(DeliveryError, match="request-error"):  # pragma: no branch
                await publisher.publish(RenderedMessage("Private body", 12))

    assert "private network detail" not in caplog.text
    assert "private-device" not in caplog.text
    assert "Private body" not in caplog.text


def test_split_plain_message_consumes_line_boundary() -> None:
    chunks = split_plain_message("first line\nsecond line", 12)

    assert chunks == ("first line", "second line")
    assert all(not chunk.startswith("\n") and not chunk.endswith("\n") for chunk in chunks)


def test_split_plain_message_omits_empty_chunk_after_final_boundary() -> None:
    chunks = split_plain_message("x" * 12 + "\n", 12)

    assert chunks == ("x" * 12,)


def test_split_plain_message_preserves_non_boundary_newlines() -> None:
    chunks = split_plain_message("x" * 100 + "\n" + "y" * 1199, 650)

    assert chunks == ("x" * 100 + "\n" + "y" * 549, "y" * 650)


@pytest.mark.parametrize("limit", (0, -1))
def test_split_plain_message_rejects_non_positive_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        split_plain_message("body", limit)
