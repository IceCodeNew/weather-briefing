"""Application-facing LLM contracts."""

from __future__ import annotations

import json
from typing import Protocol


class LLMError(RuntimeError):
    """Raised when the model output violates its contract."""


class LLMRequestError(LLMError):
    """Raised when an LLM request fails independently of the output contract."""


class LLMOutputLimitError(LLMError):
    """Raised when an LLM response is truncated at its output token limit."""


class LLMProvider(Protocol):
    """Produce a structured briefing payload from validated source context."""

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Return one structured model response."""
        ...


class SensitiveLLMDiagnostics(Protocol):
    """Expose the runtime switch for application-owned sensitive LLM diagnostics."""

    def rendered_text_logging_enabled(self) -> bool:
        """Return whether temporary sensitive diagnostics are enabled."""
        ...


def serialize_llm_payload(payload: object) -> str:
    """Serialize an LLM payload consistently for budgeting and transport."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
