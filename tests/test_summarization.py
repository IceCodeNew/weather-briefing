import pendulum

from weather_briefing.application.summarization import summarize_validated
from weather_briefing.llm import LLMOutputLimitError
from weather_briefing.models import BriefingResult


async def test_repair_payload_uses_explicit_warning_ids() -> None:
    payload: dict[str, object] = {"mode": "briefing"}

    class RepairingProvider:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return {}
            return {
                "headline": "Weather update",
                "headline_source_ids": ["source-1"],
                "conclusions": [],
                "service_status": [],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": True,
            }

    provider = RepairingProvider()
    result = await summarize_validated(
        provider,
        payload,
        pendulum.datetime(2026, 7, 23, tz="UTC"),
        {"source-1"},
        {"warning-2", "warning-1"},
        max_attempts=2,
        output_language="en",
        validator=lambda candidate: None,
    )

    assert isinstance(result, BriefingResult)
    assert provider.payloads[1]["original_input"] is payload
    assert provider.payloads[1]["allowed_resolved_warning_ids"] == ["warning-1", "warning-2"]


async def test_output_token_limit_retries_with_contract_repair() -> None:
    payload: dict[str, object] = {"mode": "forecast"}

    class TruncatedProvider:
        def __init__(self) -> None:
            self.instructions: list[str] = []

        async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            self.instructions.append(system_prompt)
            if len(self.instructions) == 1:
                raise LLMOutputLimitError("LLM response reached output token limit")
            return {
                "headline": "Weather update",
                "headline_source_ids": ["source-1"],
                "conclusions": [],
                "service_status": [],
                "active_warnings": [],
                "resolved_warning_ids": [],
                "advice": [],
                "disaster_tracking": [],
                "should_publish": True,
            }

    provider = TruncatedProvider()
    result = await summarize_validated(
        provider,
        payload,
        pendulum.datetime(2026, 7, 24, tz="UTC"),
        {"source-1"},
        set(),
        max_attempts=2,
        output_language="en",
        validator=lambda candidate: None,
    )

    assert isinstance(result, BriefingResult)
    assert len(provider.instructions) == 2
    assert "LLM response reached output token limit" in provider.instructions[1]
