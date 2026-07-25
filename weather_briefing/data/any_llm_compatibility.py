"""Compatibility metadata for the pinned any-llm SDK."""

UNSUPPORTED_JSON_OBJECT_PROVIDERS = frozenset(
    {
        "anthropic",
        "azure",
        "azureanthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "gemini",
        "groq",
        "huggingface",
        "lmstudio",
        "mistral",
        "ollama",
        "sagemaker",
        "together",
        "vertexai",
        "vertexaianthropic",
        "watsonx",
        "xai",
    }
)

UNSUPPORTED_DEFAULT_HEADER_PROVIDERS = frozenset(
    {
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
    }
)
