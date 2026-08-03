"""Structured completion transport and SDK resource lifecycle."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from inspect import isawaitable
from typing import TYPE_CHECKING, Protocol, TypeAlias

from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError, LengthFinishReasonError
from pydantic import BaseModel, ValidationError

from weather_briefing.api_client import api_call_context

from .base import LLMRequestError

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOGGER = logging.getLogger("weather_briefing.llm")

ResponseFormat: TypeAlias = dict[str, object]


class LLMCompletionClient(Protocol):
    """Expose the any-llm completion operation used by the application adapter."""

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: ResponseFormat,
        temperature: float,
        max_tokens: int,
    ) -> object:
        """Request one asynchronous structured completion."""
        ...


@contextmanager
def _normalize_request_errors(
    message: str,
    *,
    normalize_completion_errors: bool,
) -> Iterator[None]:
    """Normalize failures escaping an application-owned completion client."""
    try:
        yield
    except (LengthFinishReasonError, ValidationError):
        raise
    except AnyLLMError as exc:
        raise LLMRequestError(message) from exc
    except Exception as exc:
        if normalize_completion_errors:
            raise LLMRequestError(message) from exc
        raise


async def complete_structured(  # noqa: PLR0913
    client: AnyLLM | LLMCompletionClient,
    *,
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    response_format: type[BaseModel],
    temperature: float,
    max_tokens: int,
    request_error_message: str,
    normalize_completion_errors: bool,
) -> object:
    """Execute one prompt-constrained JSON Object completion."""
    request_messages, request_response_format = structured_output_request(messages, response_format)
    with (
        api_call_context(provider, "chat-completions"),
        _normalize_request_errors(
            request_error_message,
            normalize_completion_errors=normalize_completion_errors,
        ),
    ):
        if isinstance(client, AnyLLM):
            return await client.acompletion(
                model=model,
                messages=[dict(message) for message in request_messages],
                response_format=request_response_format,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return await client.acompletion(
            model=model,
            messages=request_messages,
            response_format=request_response_format,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def structured_output_request(
    messages: list[dict[str, str]],
    response_format: type[BaseModel],
) -> tuple[list[dict[str, str]], ResponseFormat]:
    """Prepare prompt-constrained JSON Object transport."""
    if not messages or messages[-1].get("role") != "user":
        msg = "JSON Object structured output requires a final user message"
        raise ValueError(msg)
    content = messages[-1].get("content")
    if not isinstance(content, str):
        msg = "JSON Object structured output final user message must include string content"
        raise ValueError(msg)  # noqa: TRY004
    schema = json.dumps(response_format.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    final_message = {
        **messages[-1],
        "content": (
            f"{content}\n\n"
            "Return only a JSON object matching this JSON Schema exactly. "
            "Do not wrap it in Markdown fences.\n"
            f"{schema}"
        ),
    }
    return [*messages[:-1], final_message], {"type": "json_object"}


async def close_llm_resource(resource: object) -> bool:
    """Close one SDK resource without replacing a task failure during cleanup."""
    close = getattr(resource, "aclose", None)
    if not callable(close):
        close = getattr(resource, "close", None)
    if not callable(close):
        return False
    try:
        result = close()
        if isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to close LLM SDK resource type=%s error_type=%s",
            type(resource).__name__,
            type(exc).__name__,
        )
        return False
    return True
