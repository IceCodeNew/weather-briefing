"""Delivery contracts, renderers, and platform adapters."""

from .apprise import TelegramPublisher, telegram_notifier
from .bark import BarkPublisher
from .bark_renderer import BarkTextRenderer
from .base import DeliveryError, DeliveryProvider, RenderedTextDiagnostics
from .plain_renderer import PlainTextRenderer
from .stdout import StdoutPublisher

__all__ = [
    "BarkPublisher",
    "BarkTextRenderer",
    "DeliveryError",
    "DeliveryProvider",
    "PlainTextRenderer",
    "RenderedTextDiagnostics",
    "StdoutPublisher",
    "TelegramPublisher",
    "telegram_notifier",
]
