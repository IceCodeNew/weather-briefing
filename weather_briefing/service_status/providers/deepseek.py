"""DeepSeek official service-status provider."""

from __future__ import annotations

import httpx

from ...data.service_endpoints import DEEPSEEK_STATUS_PAGE_URL
from ...registries import ServiceStatusProviderName
from ..flashcat import FlashcatStatusProvider
from ..models import ServiceSurface


def _deepseek_surface(name: str) -> ServiceSurface:
    if "api" in name.casefold():
        return ServiceSurface.API
    return ServiceSurface.WEB


class DeepSeekStatusProvider(FlashcatStatusProvider):
    """Fetch DeepSeek web-chat and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the DeepSeek official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.DEEPSEEK,
            provider_name="DeepSeek",
            page_url=DEEPSEEK_STATUS_PAGE_URL,
            classify_component=_deepseek_surface,
        )
