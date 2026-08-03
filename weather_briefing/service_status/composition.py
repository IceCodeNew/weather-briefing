"""Runtime composition of official service-status providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.registries import ServiceStatusProviderName

from .providers import (
    AnthropicStatusProvider,
    DeepSeekStatusProvider,
    KimiStatusProvider,
    OpenAIStatusProvider,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from .statuspage import ServiceStatusProvider

_BUILDERS: dict[str, Callable[[httpx.AsyncClient], ServiceStatusProvider]] = {
    ServiceStatusProviderName.DEEPSEEK: DeepSeekStatusProvider,
    ServiceStatusProviderName.OPENAI: OpenAIStatusProvider,
    ServiceStatusProviderName.ANTHROPIC: AnthropicStatusProvider,
    ServiceStatusProviderName.KIMI: KimiStatusProvider,
}


def service_status_providers(
    names: tuple[str, ...],
    client: httpx.AsyncClient,
) -> tuple[ServiceStatusProvider, ...]:
    """Build the configured independent service-status providers."""
    providers: list[ServiceStatusProvider] = []
    for name in names:
        builder = _BUILDERS.get(name)
        if builder is None:
            msg = f"Unsupported service-status provider: {name}"
            raise ValueError(msg)
        providers.append(builder(client))
    return tuple(providers)
