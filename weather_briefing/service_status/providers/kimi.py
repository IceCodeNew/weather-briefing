"""Kimi official service-status provider."""

from __future__ import annotations

import httpx

from ...data.service_endpoints import KIMI_STATUS_API_URL, KIMI_STATUS_PAGE_URL
from ...registries import ServiceStatusProviderName
from ..models import ServiceSurface
from ..statuspage import StatuspageProvider

_API_COMPONENTS = frozenset({"API Service", "Open API"})
_WEB_COMPONENTS = frozenset(
    {
        "File uploads",
        "Open Platform Portal",
        "Search",
        "Sign In / Sign Up",
        "Website",
    }
)


def _kimi_surface(name: str) -> ServiceSurface:
    if name in _API_COMPONENTS:
        return ServiceSurface.API
    if name in _WEB_COMPONENTS:
        return ServiceSurface.WEB
    return ServiceSurface.OTHER


class KimiStatusProvider(StatuspageProvider):
    """Fetch Moonshot Kimi web-product and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the Kimi official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.KIMI,
            provider_name="Kimi",
            api_url=KIMI_STATUS_API_URL,
            page_url=KIMI_STATUS_PAGE_URL,
            classify_component=_kimi_surface,
        )
