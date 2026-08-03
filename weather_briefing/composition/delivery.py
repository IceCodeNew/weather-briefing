"""Runtime composition of delivery providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.delivery import (
    BarkPublisher,
    BarkTextRenderer,
    DeliveryProvider,
    PlainTextRenderer,
    RenderedTextDiagnostics,
    StdoutPublisher,
    TelegramHTMLRenderer,
    TelegramPublisher,
)
from weather_briefing.registries import PublisherName

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from weather_briefing.config import Settings


def delivery_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None = None,
    *,
    publisher: str | None = None,
) -> DeliveryProvider:
    """Build the configured publisher and renderer pair."""
    selected = publisher or settings.publisher
    builder = PUBLISHER_BUILDERS.get(selected)
    if builder is None:
        msg = f"Unsupported publisher: {selected}"
        raise ValueError(msg)
    return builder(settings, client, diagnostics)


def delivery_providers(
    settings: Settings,
    client: httpx.AsyncClient,
    publishers: tuple[str, ...],
    diagnostics: RenderedTextDiagnostics | None = None,
) -> tuple[DeliveryProvider, ...]:
    """Build an ordered group of delivery targets."""
    if not publishers:
        msg = "At least one publisher is required"
        raise ValueError(msg)
    return tuple(delivery_provider(settings, client, diagnostics, publisher=publisher) for publisher in publishers)


def _build_stdout_publisher(
    settings: Settings,  # noqa: ARG001
    client: httpx.AsyncClient,  # noqa: ARG001
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    return DeliveryProvider(PlainTextRenderer(), StdoutPublisher(), diagnostics=diagnostics)


def _build_telegram_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        msg = "Telegram publisher requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
        raise ValueError(msg)
    return DeliveryProvider(
        TelegramHTMLRenderer(),
        TelegramPublisher(client, settings.telegram_bot_token, settings.telegram_chat_id, diagnostics),
        single_message_limit=TelegramPublisher.MAX_MESSAGE_LENGTH,
        diagnostics=diagnostics,
    )


def _build_bark_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.bark_device_key:
        msg = "Bark publisher requires BARK_DEVICE_KEY"
        raise ValueError(msg)
    return DeliveryProvider(
        BarkTextRenderer(),
        BarkPublisher(
            client,
            settings.bark_device_key,
            settings.bark_encryption_key,
            settings.bark_encryption_iv,
            diagnostics,
            base_url=settings.bark_base_url,
            group=settings.bark_group,
        ),
        single_message_limit=BarkPublisher.MAX_MESSAGE_LENGTH,
        briefing_max_messages=2,
        diagnostics=diagnostics,
    )


PUBLISHER_BUILDERS: dict[
    str,
    Callable[[Settings, httpx.AsyncClient, RenderedTextDiagnostics | None], DeliveryProvider],
] = {
    PublisherName.BARK: _build_bark_publisher,
    PublisherName.STDOUT: _build_stdout_publisher,
    PublisherName.TELEGRAM: _build_telegram_publisher,
}
