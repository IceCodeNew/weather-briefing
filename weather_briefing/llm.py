from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
import pendulum

from .api_client import api_call_extensions
from .models import Advice, AdviceTopic, BriefingResult, Conclusion, Warning
from .time_utils import require_aware_datetime


class LLMError(RuntimeError):
    """Raised when the model response is unavailable or violates its contract."""


class LLMProvider(Protocol):
    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]: ...


class OpenAICompatibleChatCompletionsProvider:
    API_PROVIDER = "openai-compatible"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_output_tokens: int,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._max_output_tokens = max_output_tokens

    @property
    def base_url(self) -> str:
        return self._base_url

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": self._max_output_tokens,
                },
                extensions=api_call_extensions(self.API_PROVIDER, "chat-completions"),
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not content:
                raise LLMError("LLM returned empty JSON content")
            result = json.loads(content)
        except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("LLM request or structured response failed") from exc
        if not isinstance(result, dict):
            raise LLMError("LLM response must be a JSON object")
        return result


class DeepSeekProvider(OpenAICompatibleChatCompletionsProvider):
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    API_PROVIDER = "deepseek"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        super().__init__(
            client,
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_output_tokens=max_output_tokens,
        )


def parse_result(
    payload: Mapping[str, Any],
    now: pendulum.DateTime,
    valid_source_ids: set[str],
) -> BriefingResult:
    require_aware_datetime(now, context="Briefing result time")

    def string_ids(value: Mapping[str, Any], field: str) -> tuple[str, ...]:
        raw_ids = value.get(field, [])
        if not isinstance(raw_ids, list):
            raise LLMError(f"{field} must be an array")
        if not all(isinstance(item, str) and item.strip() for item in raw_ids):
            raise LLMError(f"{field} must contain non-empty strings")
        return tuple(raw_ids)

    def cited_source_ids(value: Mapping[str, Any], field: str) -> tuple[str, ...]:
        parsed = string_ids(value, field)
        if not parsed:
            raise LLMError(f"{field} must cite at least one source ID")
        unknown = set(parsed) - valid_source_ids
        if unknown:
            raise LLMError(f"Model cited unknown source IDs: {sorted(unknown)}")
        return parsed

    def sourced_text(value: Mapping[str, Any], key: str) -> str:
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"{key} entries must contain non-empty text")
        return text

    def parse_sourced_text_items(key: str) -> tuple[Conclusion, ...]:
        values = payload.get(key, [])
        if not isinstance(values, list):
            raise LLMError(f"{key} must be an array")
        parsed: list[Conclusion] = []
        for value in values:
            if not isinstance(value, dict):
                raise LLMError(f"{key} entries must be objects")
            parsed.append(
                Conclusion(
                    text=sourced_text(value, key),
                    source_ids=cited_source_ids(value, "source_ids"),
                )
            )
        return tuple(parsed)

    def advice() -> tuple[Advice, ...]:
        values = payload.get("advice", [])
        if not isinstance(values, list):
            raise LLMError("advice must be an array")
        parsed: list[Advice] = []
        for value in values:
            if not isinstance(value, dict):
                raise LLMError("advice entries must be objects")
            try:
                topic = AdviceTopic(str(value["topic"]))
            except (KeyError, ValueError):
                allowed = ", ".join(item.value for item in AdviceTopic)
                raise LLMError(f"advice entries must use a valid topic: {allowed}") from None
            parsed.append(
                Advice(
                    topic=topic,
                    text=sourced_text(value, "advice"),
                    source_ids=cited_source_ids(value, "source_ids"),
                )
            )
        return tuple(parsed)

    warning_values = payload.get("active_warnings", [])
    if not isinstance(warning_values, list):
        raise LLMError("active_warnings must be an array")
    warnings: list[Warning] = []
    for value in warning_values:
        if not isinstance(value, dict):
            raise LLMError("active_warnings entries must be objects")
        warnings.append(
            Warning(
                id=str(value["id"]),
                title=str(value["title"]),
                status=str(value["status"]),
                detail=str(value["detail"]),
                source_ids=cited_source_ids(value, "source_ids"),
                last_confirmed_at=now,
            )
        )
    should_publish = payload.get("should_publish", True)
    if not isinstance(should_publish, bool):
        raise LLMError("should_publish must be a boolean")
    parsed_conclusions = parse_sourced_text_items("conclusions")
    parsed_advice = advice()
    parsed_disaster_tracking = parse_sourced_text_items("disaster_tracking")
    return BriefingResult(
        headline=str(payload["headline"]),
        headline_source_ids=cited_source_ids(payload, "headline_source_ids"),
        conclusions=parsed_conclusions,
        active_warnings=tuple(warnings),
        resolved_warning_ids=string_ids(payload, "resolved_warning_ids"),
        advice=parsed_advice,
        disaster_tracking=parsed_disaster_tracking,
        should_publish=should_publish,
        raw_payload=dict(payload),
    )
