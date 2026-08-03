"""Anthropic official service-status provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.data.service_endpoints import ANTHROPIC_STATUS_FEED_URL, ANTHROPIC_STATUS_PAGE_URL
from weather_briefing.registries import ServiceStatusProviderName
from weather_briefing.service_status.feed import StatusFeedProvider

from ._surface import keyword_surface

if TYPE_CHECKING:
    import httpx


class AnthropicStatusProvider(StatusFeedProvider):
    """Fetch Anthropic Claude web, console, and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the Anthropic official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.ANTHROPIC,
            provider_name="Anthropic",
            feed_url=ANTHROPIC_STATUS_FEED_URL,
            page_url=ANTHROPIC_STATUS_PAGE_URL,
            classify_component=keyword_surface,
        )
