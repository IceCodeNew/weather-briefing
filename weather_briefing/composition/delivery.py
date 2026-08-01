"""Runtime composition of delivery providers."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ..config import Settings
from ..delivery import (
    BarkPublisher,
    BarkTextRenderer,
    DeliveryProvider,
    PlainTextRenderer,
    RenderedTextDiagnostics,
    StdoutPublisher,
    TelegramHTMLRenderer,
    TelegramPublisher,
)
from ..registries import PublisherName


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
        raise ValueError(f"Unsupported publisher: {selected}")
    return builder(settings, client, diagnostics)


def delivery_providers(
    settings: Settings,
    client: httpx.AsyncClient,
    publishers: tuple[str, ...],
    diagnostics: RenderedTextDiagnostics | None = None,
) -> tuple[DeliveryProvider, ...]:
    """Build an ordered group of delivery targets."""
    if not publishers:
        raise ValueError("At least one publisher is required")
    return tuple(delivery_provider(settings, client, diagnostics, publisher=publisher) for publisher in publishers)


def _build_stdout_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    return DeliveryProvider(PlainTextRenderer(), StdoutPublisher(), diagnostics=diagnostics)


def _build_telegram_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError("Telegram publisher requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
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
        raise ValueError("Bark publisher requires BARK_DEVICE_KEY")
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
