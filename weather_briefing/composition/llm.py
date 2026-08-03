"""Runtime composition of LLM providers."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from weather_briefing.llm import CompleteLLMProvider, SensitiveLLMDiagnostics, any_llm
from weather_briefing.llm import fallback as fallback_module

if TYPE_CHECKING:
    from weather_briefing.config import Settings


async def llm_provider(
    settings: Settings,
    diagnostics: SensitiveLLMDiagnostics | None = None,
) -> CompleteLLMProvider:
    """Build the configured primary and optional fallback LLM adapters."""
    primary = any_llm.create_any_llm_provider(
        settings.llm_provider,
        settings.llm_model,
        settings.llm_max_output_tokens,
        api_key=settings.api_key,
        api_base=settings.llm_base_url,
        extra_headers=settings.llm_extra_headers,
        diagnostics=diagnostics,
    )
    if settings.llm_fallback_provider is None or settings.llm_fallback_model is None:
        return primary
    async with AsyncExitStack() as stack:
        stack.push_async_callback(primary.aclose)
        fallback = any_llm.create_any_llm_provider(
            settings.llm_fallback_provider,
            settings.llm_fallback_model,
            settings.llm_max_output_tokens,
            extra_headers=settings.llm_fallback_extra_headers,
            diagnostics=diagnostics,
        )
        stack.push_async_callback(fallback.aclose)
        provider = fallback_module.FallbackLLMProvider(
            primary,
            fallback,
            primary_name=settings.llm_provider,
            fallback_name=settings.llm_fallback_provider,
        )
        stack.pop_all()
        return provider
