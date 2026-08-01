"""Delivery contracts, renderers, and platform adapters."""

from .bark import BarkPublisher
from .bark_renderer import BarkTextRenderer
from .base import DeliveryError, DeliveryProvider, RenderedTextDiagnostics
from .plain_renderer import PlainTextRenderer
from .stdout import StdoutPublisher
from .telegram import TelegramPublisher
from .telegram_renderer import TelegramHTMLRenderer

__all__ = [
    "BarkPublisher",
    "BarkTextRenderer",
    "DeliveryError",
    "DeliveryProvider",
    "PlainTextRenderer",
    "RenderedTextDiagnostics",
    "StdoutPublisher",
    "TelegramHTMLRenderer",
    "TelegramPublisher",
]
