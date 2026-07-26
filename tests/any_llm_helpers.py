"""Shared AnyLLM test helpers."""

from any_llm import AnyLLM


def loadable_any_llm_providers() -> tuple[str, ...]:
    """Return providers whose SDK classes can be imported."""
    result: list[str] = []
    for provider in AnyLLM.get_supported_providers():
        try:
            AnyLLM.get_provider_class(provider)
            result.append(provider)
        except ImportError:
            pass
    return tuple(result)
