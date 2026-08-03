"""Validated LLM summarization with bounded contract-repair attempts."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from weather_briefing.data.prompts import SYSTEM_PROMPT
from weather_briefing.llm import LLMError, LLMProvider, LLMRequestError, parse_result

if TYPE_CHECKING:
    from collections.abc import Callable

    import pendulum

    from weather_briefing.models import BriefingResult

_LOGGER = logging.getLogger("weather_briefing.service")


async def summarize_validated(  # noqa: PLR0913
    provider: LLMProvider,
    payload: dict[str, object],
    now: pendulum.DateTime,
    valid_source_ids: set[str],
    allowed_resolved_warning_ids: set[str],
    *,
    max_attempts: int,
    output_language: str,
    validator: Callable[[BriefingResult], None],
) -> BriefingResult:
    """Summarize and retry only responses that violate the output contract."""
    instructions = SYSTEM_PROMPT
    current_payload: dict[str, object] = payload
    last_error: LLMError | None = None
    for attempt in range(max_attempts):
        raw_result: dict[str, object] | None = None
        try:
            _LOGGER.debug("LLM summarization attempt %d/%d", attempt + 1, max_attempts)
            raw_result = await provider.summarize(instructions, current_payload)
            parsed_result = parse_result(raw_result, now, valid_source_ids)
            result = replace(
                parsed_result,
                output_language=output_language,
            )
            validator(result)
            _LOGGER.debug("LLM summarization successful on attempt %d/%d", attempt + 1, max_attempts)
        except LLMRequestError:
            raise
        except LLMError as exc:
            last_error = exc
            _LOGGER.debug("LLM validation failure (attempt %d/%d): %s", attempt + 1, max_attempts, exc)
            if attempt + 1 < max_attempts:
                instructions = f"{SYSTEM_PROMPT}\n上一版 JSON 未通过验证。请只修复契约错误：{exc}"  # noqa: RUF001
                repair_payload: dict[str, object] = {
                    "original_input": payload,
                    "allowed_source_ids": sorted(valid_source_ids),
                    "allowed_resolved_warning_ids": sorted(allowed_resolved_warning_ids),
                }
                if raw_result is not None:
                    repair_payload["previous_invalid_response"] = raw_result
                current_payload = repair_payload
        else:
            return result
    msg = "LLM output validation failed after configured attempts"
    raise LLMError(msg) from last_error
