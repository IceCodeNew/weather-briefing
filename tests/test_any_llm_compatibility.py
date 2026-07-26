from any_llm import AnyLLM
from any_llm.providers.openai.base import BaseOpenAIProvider

from tests.any_llm_helpers import loadable_any_llm_providers
from weather_briefing.data.any_llm_compatibility import (
    UNSUPPORTED_DEFAULT_HEADER_PROVIDERS,
    UNSUPPORTED_JSON_OBJECT_PROVIDERS,
)


def test_default_header_provider_compatibility_matches_the_pinned_sdk() -> None:
    assert {
        "azure",
        "bedrock",
        "cohere",
        "gemini",
        "huggingface",
        "lmstudio",
        "mistral",
        "ollama",
        "sagemaker",
        "vertexai",
        "watsonx",
        "xai",
    } == UNSUPPORTED_DEFAULT_HEADER_PROVIDERS
    loadable = set(loadable_any_llm_providers())
    checkable_blacklist = UNSUPPORTED_DEFAULT_HEADER_PROVIDERS & loadable
    completion_providers = {
        provider for provider in loadable if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
    }
    assert completion_providers > checkable_blacklist


def test_json_object_provider_compatibility_matches_the_pinned_sdk() -> None:
    loadable = set(loadable_any_llm_providers())
    completion_providers = {
        provider for provider in loadable if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
    }
    json_object_providers = {
        provider
        for provider in completion_providers
        if issubclass(AnyLLM.get_provider_class(provider), BaseOpenAIProvider)
    }
    unsupported_providers = completion_providers - json_object_providers

    checkable_blacklist = UNSUPPORTED_JSON_OBJECT_PROVIDERS & loadable
    assert unsupported_providers == checkable_blacklist
    assert json_object_providers | checkable_blacklist == completion_providers
    assert json_object_providers.isdisjoint(checkable_blacklist)
