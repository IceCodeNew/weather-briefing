import json
from typing import Any

import pendulum
import pytest
from apprise import NotifyFormat

from weather_briefing.delivery import (
    BarkTextRenderer,
    DeliveryError,
    DeliveryProvider,
    PlainTextRenderer,
    StdoutPublisher,
    TelegramPublisher,
    telegram_notifier,
)
from weather_briefing.models import Article, RenderedMessage


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


class RecordingNotifier:
    def __init__(self, result: bool | None = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, NotifyFormat]] = []

    async def async_notify(
        self,
        *,
        body: str,
        title: str,
        body_format: NotifyFormat,
    ) -> bool | None:
        self.calls.append((body, title, body_format))
        if self.error is not None:
            raise self.error
        return self.result


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


async def test_delivery_provider_counts_repeated_titles_in_briefing_chunk_limit() -> None:
    publisher = RecordingPublisher()
    delivery = DeliveryProvider(PlainTextRenderer(), publisher, 650, 2)
    message = RenderedMessage("b" * 1281, 1300, "t" * 19)

    with pytest.raises(DeliveryError, match="delivery limit") as error:
        await delivery.publish_briefing(message)

    assert error.value.reason == "message-too-long"
    assert publisher.messages == []


async def test_delivery_provider_logs_verbatim_title_in_payload_length(caplog) -> None:
    article = Article(
        "source",
        "feed",
        "Feed",
        "Title",
        "https://example.invalid/article",
        pendulum.datetime(2026, 7, 24, tz="UTC"),
        "Body",
    )
    delivery = DeliveryProvider(BarkTextRenderer(), RecordingPublisher())

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        await delivery.publish_verbatim(article)

    assert "Rendered verbatim message: visible_characters=9 payload_characters=9" in caplog.text


def test_delivery_provider_applies_configured_limit_to_titled_briefing() -> None:
    delivery = DeliveryProvider(PlainTextRenderer(), NoopPublisher(), 650, 2)

    assert delivery.briefing_fits(RenderedMessage("b" * 960, 980, "t" * 20), 500)
    assert delivery.briefing_fits(RenderedMessage("b" * 480 + "\n" + "b" * 480, 981, "t" * 20), 500)
    assert not delivery.briefing_fits(RenderedMessage("b" * 961, 981, "t" * 20), 500)


@pytest.mark.parametrize("reason", (None, 7, "private detail\nforged-log-line"))
def test_delivery_error_rejects_unsafe_structured_reason(reason) -> None:
    with pytest.raises(ValueError, match="lowercase kebab-case"):
        DeliveryError("Delivery failed", reason=reason)


@pytest.mark.parametrize("value", (None, 0, 1, "true"))
def test_delivery_error_rejects_non_boolean_channel_availability(value) -> None:
    with pytest.raises(TypeError, match="channel_unavailable must be a bool"):
        DeliveryError("Delivery failed", reason="request-error", channel_unavailable=value)


def test_telegram_notifier_configures_apprise_service_variants() -> None:
    normal = telegram_notifier(
        "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "-100123456",
        silent=False,
        timeout_seconds=12.5,
    )
    silent = telegram_notifier(
        "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "-100123456",
        silent=True,
        timeout_seconds=12.5,
    )

    assert len(normal) == 1
    assert "format=text" in normal.urls(privacy=False)[0]
    assert "overflow=split" in normal.urls(privacy=False)[0]
    assert "preview=no" in normal.urls(privacy=False)[0]
    assert "silent=no" in normal.urls(privacy=False)[0]
    assert "silent=yes" in silent.urls(privacy=False)[0]
    assert "rto=12.5" in normal.urls(privacy=False)[0]
    assert "cto=12.5" in normal.urls(privacy=False)[0]
    assert "abcdefghijklmnopqrstuvwxyzABCDE" not in normal.urls(privacy=True)[0]


def test_telegram_notifier_rejects_invalid_apprise_configuration() -> None:
    with pytest.raises(ValueError, match="Invalid Telegram publisher configuration"):
        telegram_notifier(
            "invalid-token",
            "-100123456",
            silent=False,
            timeout_seconds=12.5,
        )


async def test_apprise_publisher_uses_text_and_selects_silent_notifier(caplog) -> None:
    normal = RecordingNotifier()
    silent = RecordingNotifier()
    publisher = TelegramPublisher(normal, silent)
    message = RenderedMessage("<b>Title</b>\n\nBody", 11)

    with caplog.at_level("DEBUG", logger="weather_briefing.publishers"):
        await publisher.publish(message, silent=True)

    assert normal.calls == []
    assert silent.calls == [("<b>Title</b>\n\nBody", "", NotifyFormat.TEXT)]
    assert (
        "Telegram delivery through Apprise prepared: visible_characters=11 payload_characters=18 "
        "single_message=False silent=True"
    ) in caplog.text
    assert "Telegram delivery through Apprise accepted" in caplog.text


async def test_apprise_telegram_integration_splits_text_and_delivers_silently(monkeypatch) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    class SuccessfulResponse:
        status_code = 200
        content = b'{"ok":true}'

    def post(url: str, **kwargs: object) -> SuccessfulResponse:
        requests.append((url, kwargs))
        return SuccessfulResponse()

    monkeypatch.setattr("apprise.plugins.telegram.requests.post", post)
    normal = telegram_notifier(
        "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "-100123456",
        silent=False,
        timeout_seconds=12.5,
    )
    silent = telegram_notifier(
        "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "-100123456",
        silent=True,
        timeout_seconds=12.5,
    )
    publisher = TelegramPublisher(normal, silent)
    body = "x" * 4097

    await publisher.publish(RenderedMessage(body, 4097), silent=True)

    assert len(requests) == 2
    payloads = []
    for url, kwargs in requests:
        assert url == "https://api.telegram.org/bot123456:abcdefghijklmnopqrstuvwxyzABCDE/sendMessage"
        data = kwargs["data"]
        assert isinstance(data, str)
        payloads.append(json.loads(data))
    assert all(payload["chat_id"] == -100123456 for payload in payloads)
    assert all(payload["disable_notification"] is True for payload in payloads)
    assert all(payload["disable_web_page_preview"] is True for payload in payloads)
    assert all(payload["parse_mode"] == "HTML" for payload in payloads)
    assert "".join(payload["text"] for payload in payloads) == body


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


@pytest.mark.parametrize(
    "notifier",
    (
        RecordingNotifier(result=False),
        RecordingNotifier(result=None),
        RecordingNotifier(error=RuntimeError("private provider detail")),
    ),
)
async def test_apprise_failure_is_safe_and_structured(
    notifier: RecordingNotifier,
    caplog,
) -> None:
    publisher = TelegramPublisher(notifier, RecordingNotifier())

    with (
        caplog.at_level("INFO", logger="weather_briefing.publishers"),
        pytest.raises(DeliveryError, match="Telegram delivery failed") as caught,
    ):
        await publisher.publish(RenderedMessage("Private body", 12))

    assert caught.value.reason == "delivery-failed"
    assert caught.value.__cause__ is None
    assert "private provider detail" not in caplog.text
    warnings = [record for record in caplog.records if record.levelno == 30]
    assert [record.getMessage() for record in warnings] == ["Telegram delivery through Apprise failed"]


async def test_apprise_rejects_oversized_single_message_before_delivery() -> None:
    notifier = RecordingNotifier()
    publisher = TelegramPublisher(notifier, RecordingNotifier())

    with pytest.raises(DeliveryError, match="exceeds") as caught:
        await publisher.publish(RenderedMessage("<b>short markup</b>", 4097), single_message=True)

    assert caught.value.reason == "message-too-long"
    assert notifier.calls == []


async def test_stdout_publisher_outputs_message_body(capsys) -> None:
    await StdoutPublisher().publish(RenderedMessage("test body", 9))

    assert capsys.readouterr().out.strip() == "test body"
