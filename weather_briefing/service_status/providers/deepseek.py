"""DeepSeek official service-status provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.data.service_endpoints import DEEPSEEK_STATUS_FEED_URL, DEEPSEEK_STATUS_PAGE_URL
from weather_briefing.registries import ServiceStatusProviderName
from weather_briefing.service_status.feed import StatusFeedProvider
from weather_briefing.service_status.models import ServiceSurface

if TYPE_CHECKING:
    import httpx


def _deepseek_surface(name: str) -> ServiceSurface:
    if "api" in name.casefold():
        return ServiceSurface.API
    return ServiceSurface.WEB


class DeepSeekStatusProvider(StatusFeedProvider):
    """Fetch DeepSeek web-chat and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the DeepSeek official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.DEEPSEEK,
            provider_name="DeepSeek",
            feed_url=DEEPSEEK_STATUS_FEED_URL,
            page_url=DEEPSEEK_STATUS_PAGE_URL,
            classify_component=_deepseek_surface,
        )
