"""OpenAI official service-status provider."""

from __future__ import annotations

import httpx

from ...data.service_endpoints import OPENAI_STATUS_API_URL, OPENAI_STATUS_PAGE_URL
from ...registries import ServiceStatusProviderName
from ..models import ServiceSurface
from ..statuspage import StatuspageProvider

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


class OpenAIStatusProvider(StatuspageProvider):
    """Fetch OpenAI web-product and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the OpenAI official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.OPENAI,
            provider_name="OpenAI",
            api_url=OPENAI_STATUS_API_URL,
            page_url=OPENAI_STATUS_PAGE_URL,
            classify_component=_openai_surface,
        )
