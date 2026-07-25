from any_llm import AnyLLM
from any_llm.providers.openai.base import BaseOpenAIProvider

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
    completion_providers = {
        provider
        for provider in AnyLLM.get_supported_providers()
        if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
    }

    assert completion_providers > UNSUPPORTED_DEFAULT_HEADER_PROVIDERS


def test_json_object_provider_compatibility_matches_the_pinned_sdk() -> None:
    completion_providers = {
        provider
        for provider in AnyLLM.get_supported_providers()
        if AnyLLM.get_provider_class(provider).SUPPORTS_COMPLETION
    }
    json_object_providers = {
        provider
        for provider in completion_providers
        if issubclass(AnyLLM.get_provider_class(provider), BaseOpenAIProvider)
    }
    unsupported_providers = completion_providers - json_object_providers

    assert unsupported_providers == UNSUPPORTED_JSON_OBJECT_PROVIDERS
    assert json_object_providers | UNSUPPORTED_JSON_OBJECT_PROVIDERS == completion_providers
    assert json_object_providers.isdisjoint(UNSUPPORTED_JSON_OBJECT_PROVIDERS)
