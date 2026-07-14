from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
import pendulum

from .api_client import api_call_extensions
from .models import BriefingResult, Conclusion, Warning
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

    def conclusions(key: str) -> tuple[Conclusion, ...]:
        values = payload.get(key, [])
        if not isinstance(values, list):
            raise LLMError(f"{key} must be an array")
        parsed: list[Conclusion] = []
        for value in values:
            if not isinstance(value, dict):
                raise LLMError(f"{key} entries must be objects")
            source_ids = tuple(str(item) for item in value.get("source_ids", []))
            if not source_ids:
                raise LLMError(f"{key} entries must cite at least one source ID")
            unknown = set(source_ids) - valid_source_ids
            if unknown:
                raise LLMError(f"Model cited unknown source IDs: {sorted(unknown)}")
            parsed.append(Conclusion(text=str(value["text"]), source_ids=source_ids))
        return tuple(parsed)

    warning_values = payload.get("active_warnings", [])
    if not isinstance(warning_values, list):
        raise LLMError("active_warnings must be an array")
    warnings: list[Warning] = []
    for value in warning_values:
        if not isinstance(value, dict):
            raise LLMError("active_warnings entries must be objects")
        source_ids = tuple(str(item) for item in value.get("source_ids", []))
        if not source_ids:
            raise LLMError("Active warnings must cite at least one source ID")
        if set(source_ids) - valid_source_ids:
            raise LLMError("Warning cited an unknown source ID")
        warnings.append(
            Warning(
                id=str(value["id"]),
                title=str(value["title"]),
                status=str(value["status"]),
                detail=str(value["detail"]),
                source_ids=source_ids,
                last_confirmed_at=now,
            )
        )
    should_publish = payload.get("should_publish", True)
    if not isinstance(should_publish, bool):
        raise LLMError("should_publish must be a boolean")
    return BriefingResult(
        headline=str(payload["headline"]),
        overview=str(payload["overview"]),
        conclusions=conclusions("conclusions"),
        active_warnings=tuple(warnings),
        resolved_warning_ids=tuple(str(item) for item in payload.get("resolved_warning_ids", [])),
        advice=conclusions("advice"),
        disaster_tracking=conclusions("disaster_tracking"),
        should_publish=should_publish,
        raw_payload=dict(payload),
    )
