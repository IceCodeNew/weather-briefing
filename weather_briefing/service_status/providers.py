"""Official AI service-status provider adapters."""

from __future__ import annotations

import httpx

from ..data.service_endpoints import (
    ANTHROPIC_STATUS_API_URL,
    ANTHROPIC_STATUS_PAGE_URL,
    DEEPSEEK_STATUS_PAGE_URL,
    KIMI_STATUS_API_URL,
    KIMI_STATUS_PAGE_URL,
    OPENAI_STATUS_API_URL,
    OPENAI_STATUS_PAGE_URL,
)
from ..registries import ServiceStatusProviderName
from .flashcat import FlashcatStatusProvider
from .models import ServiceSurface
from .statuspage import StatuspageProvider


def _keyword_surface(name: str) -> ServiceSurface:
    normalized = name.casefold()
    if "api" in normalized:
        return ServiceSurface.API
    if any(marker in normalized for marker in ("web", ".ai", "chat", "console", "portal", "sign in", "search")):
        return ServiceSurface.WEB
    return ServiceSurface.OTHER


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


class AnthropicStatusProvider(StatuspageProvider):
    """Fetch Anthropic Claude web, console, and API status."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Configure the Anthropic official status endpoint."""
        super().__init__(
            client,
            provider_id=ServiceStatusProviderName.ANTHROPIC,
            provider_name="Anthropic",
            api_url=ANTHROPIC_STATUS_API_URL,
            page_url=ANTHROPIC_STATUS_PAGE_URL,
            classify_component=_keyword_surface,
        )


_OPENAI_API_COMPONENTS = frozenset(
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
_OPENAI_WEB_COMPONENTS = frozenset(
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
    if name in _OPENAI_API_COMPONENTS:
        return ServiceSurface.API
    if name in _OPENAI_WEB_COMPONENTS:
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


_KIMI_API_COMPONENTS = frozenset({"API Service", "Open API"})
_KIMI_WEB_COMPONENTS = frozenset(
    {
        "File uploads",
        "Open Platform Portal",
        "Search",
        "Sign In / Sign Up",
        "Website",
    }
)


def _kimi_surface(name: str) -> ServiceSurface:
    if name in _KIMI_API_COMPONENTS:
        return ServiceSurface.API
    if name in _KIMI_WEB_COMPONENTS:
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
