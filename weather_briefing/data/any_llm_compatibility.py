"""Compatibility metadata for the pinned any-llm SDK."""

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
