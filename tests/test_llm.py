import pendulum
import pytest

from weather_briefing.llm import LLMError, parse_result


def test_rejects_model_invented_source() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [{"text": "Claim", "source_ids": ["invented"]}],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }
    with pytest.raises(LLMError, match="unknown source"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            {"real"},
        )


def test_rejects_suppressed_message_with_active_warning() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [
            {
                "id": "warning",
                "title": "Warning",
                "status": "active",
                "detail": "Detail",
                "source_ids": ["source"],
            }
        ],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
        "should_publish": False,
    }

    with pytest.raises(LLMError, match="active warnings require"):
        parse_result(
            payload,
            pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai"),
            {"source"},
        )


def test_rejects_result_time_without_timezone() -> None:
    payload = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    with pytest.raises(ValueError, match="timezone information"):
        parse_result(payload, pendulum.naive(2026, 7, 13, 9), set())
