import logging
from unittest.mock import AsyncMock, call

import pytest

from weather_briefing.llm import FallbackLLMProvider, LLMError, LLMRequestError
from weather_briefing.notifications import NotificationDecision


def _provider() -> AsyncMock:
    provider = AsyncMock()
    provider.summarize.return_value = {"provider": "result"}
    provider.assess_notification.return_value = NotificationDecision(True)
    provider.translate_service_status.return_value = ("title", "body")
    return provider


@pytest.mark.parametrize(
    ("operation", "args", "expected"),
    (
        ("summarize", ("system", {"input": "value"}), {"provider": "fallback"}),
        ("assess_notification", ({"input": "value"},), NotificationDecision(True)),
        ("translate_service_status", ("title", "body", "en"), ("translated", "content")),
    ),
)
async def test_request_failure_uses_fallback(operation: str, args: tuple[object, ...], expected: object) -> None:
    primary = _provider()
    fallback = _provider()
    getattr(primary, operation).side_effect = LLMRequestError("primary unavailable")
    getattr(fallback, operation).return_value = expected
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    result = await getattr(provider, operation)(*args)

    assert result == expected
    getattr(primary, operation).assert_awaited_once_with(*args)
    getattr(fallback, operation).assert_awaited_once_with(*args)


async def test_successful_primary_request_does_not_use_fallback() -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.return_value = {"provider": "primary"}
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    result = await provider.summarize("system", {"input": "value"})

    assert result == {"provider": "primary"}
    fallback.summarize.assert_not_awaited()


async def test_request_failure_pins_fallback_for_later_repairs() -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.side_effect = LLMRequestError("primary unavailable")
    fallback.summarize.side_effect = (
        {"provider": "invalid"},
        {"provider": "repaired"},
    )
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    first_result = await provider.summarize("system", {"attempt": 1})
    second_result = await provider.summarize("repair", {"attempt": 2})

    assert first_result == {"provider": "invalid"}
    assert second_result == {"provider": "repaired"}
    primary.summarize.assert_awaited_once_with("system", {"attempt": 1})
    assert fallback.summarize.await_args_list == [
        call("system", {"attempt": 1}),
        call("repair", {"attempt": 2}),
    ]


async def test_request_failure_pins_fallback_across_operations() -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.side_effect = LLMRequestError("primary unavailable")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    await provider.summarize("system", {"input": "value"})
    decision = await provider.assess_notification({"notification": "value"})

    assert decision == NotificationDecision(True)
    primary.assess_notification.assert_not_awaited()
    fallback.assess_notification.assert_awaited_once_with({"notification": "value"})


async def test_output_contract_failure_does_not_use_fallback() -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.side_effect = LLMError("invalid response")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    with pytest.raises(LLMError, match="invalid response"):
        await provider.summarize("system", {"input": "value"})

    fallback.summarize.assert_not_awaited()


async def test_fallback_failure_preserves_primary_as_context() -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.side_effect = LLMRequestError("primary unavailable")
    fallback.summarize.side_effect = LLMRequestError("fallback unavailable")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    with pytest.raises(LLMRequestError, match="fallback unavailable") as exc_info:
        await provider.summarize("system", {"input": "value"})

    assert isinstance(exc_info.value.__context__, LLMRequestError)
    assert str(exc_info.value.__context__) == "primary unavailable"


async def test_fallback_log_excludes_exception_details(caplog) -> None:
    primary = _provider()
    fallback = _provider()
    primary.summarize.side_effect = LLMRequestError("private upstream detail")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    with caplog.at_level(logging.WARNING, logger="weather_briefing.llm"):
        await provider.summarize("system", {"input": "value"})

    assert "operation=summarize primary=primary fallback=fallback" in caplog.text
    assert "private upstream detail" not in caplog.text


async def test_close_releases_both_providers() -> None:
    primary = _provider()
    fallback = _provider()
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    await provider.aclose()

    primary.aclose.assert_awaited_once_with()
    fallback.aclose.assert_awaited_once_with()


async def test_primary_close_failure_still_closes_fallback() -> None:
    primary = _provider()
    fallback = _provider()
    primary.aclose.side_effect = RuntimeError("cleanup failed")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await provider.aclose()

    fallback.aclose.assert_awaited_once_with()


async def test_both_close_failures_are_preserved() -> None:
    primary = _provider()
    fallback = _provider()
    primary.aclose.side_effect = RuntimeError("primary cleanup failed")
    fallback.aclose.side_effect = ValueError("fallback cleanup failed")
    provider = FallbackLLMProvider(
        primary,
        fallback,
        primary_name="primary",
        fallback_name="fallback",
    )

    with pytest.raises(BaseExceptionGroup) as exc_info:
        await provider.aclose()

    assert [str(error) for error in exc_info.value.exceptions] == [
        "primary cleanup failed",
        "fallback cleanup failed",
    ]
