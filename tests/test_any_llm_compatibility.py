from any_llm import AnyLLM

from weather_briefing.data.any_llm_compatibility import UNSUPPORTED_DEFAULT_HEADER_PROVIDERS


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
