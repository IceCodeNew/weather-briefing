"""OpenAI official service-status provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.data.service_endpoints import OPENAI_STATUS_FEED_URL, OPENAI_STATUS_PAGE_URL
from weather_briefing.registries import ServiceStatusProviderName
from weather_briefing.service_status.feed import StatusFeedProvider
from weather_briefing.service_status.models import ServiceSurface

if TYPE_CHECKING:
    import httpx

_API_COMPONENTS = frozenset(
    {
        "Ads API",
        "Audio",
        "Batch",
        "Codex API",
        "Compliance API",
        "Embeddings",
        "Files",
        "Fine-tuning",
        "Images",
        "Moderations",
        "Responses",
    }
)
_WEB_COMPONENTS = frozenset(
    {
        "Ads Manager",
        "ChatGPT Atlas",
        "ChatGPT Work",
        "Connectors/Apps",
        "Conversations",
        "Deep Research",
        "GPTs",
        "Login",
        "Search",
        "Sites",
        "Sora",
    }
)


def _openai_surface(name: str) -> ServiceSurface:
    if name in _API_COMPONENTS:
        return ServiceSurface.API
    if name in _WEB_COMPONENTS:
        return ServiceSurface.WEB
    return ServiceSurface.OTHER


class OpenAIStatusProvider(StatusFeedProvider):
    """Fetch OpenAI web-product and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the OpenAI official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.OPENAI,
            provider_name="OpenAI",
            feed_url=OPENAI_STATUS_FEED_URL,
            page_url=OPENAI_STATUS_PAGE_URL,
            classify_component=_openai_surface,
        )
