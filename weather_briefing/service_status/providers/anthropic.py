"""Anthropic official service-status provider."""

from __future__ import annotations

import httpx

from ...data.service_endpoints import ANTHROPIC_STATUS_FEED_URL, ANTHROPIC_STATUS_PAGE_URL
from ...registries import ServiceStatusProviderName
from ..feed import StatusFeedProvider
from ._surface import keyword_surface


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
