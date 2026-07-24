from unittest.mock import patch

import pytest

from weather_briefing.data.prompts import SYSTEM_PROMPT, _load_system_prompt


@pytest.mark.parametrize(
    "error",
    [OSError("unreadable"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")],
    ids=["io-error", "decode-error"],
)
def test_system_prompt_load_failure_is_actionable(error: Exception) -> None:
    with (
        patch("importlib.resources.files", side_effect=error),
        pytest.raises(RuntimeError, match="Unable to load system prompt: system_prompt.txt"),
    ):
        _load_system_prompt()


def test_prompt_limits_disasters_to_the_location_scope() -> None:
    assert "只影响海淀区则排除" in SYSTEM_PROMPT
    assert "明确说明无影响" in SYSTEM_PROMPT
    assert "disaster_tracking 必须为空" in SYSTEM_PROMPT
    assert "完整地点名为地域判断主依据" in SYSTEM_PROMPT
    assert "只是可选定位提示" in SYSTEM_PROMPT


def test_prompt_uses_actionable_publication_threshold() -> None:
    assert "是否需要采取准备行动" in SYSTEM_PROMPT
    assert "约一小时后影响当前地区的降雨" in SYSTEM_PROMPT
    assert "降雨概率和雨量" in SYSTEM_PROMPT
    assert "普通天气复述" in SYSTEM_PROMPT
    assert "content_compacted=true" in SYSTEM_PROMPT
    assert "不得补全被省略的细节" in SYSTEM_PROMPT


def test_prompt_separates_advice_and_avoids_repetition() -> None:
    assert "过敏原信息只能放入 advice" in SYSTEM_PROMPT
    assert "不得使用“原始浓度”" in SYSTEM_PROMPT
    assert "与口罩或运动建议合并为一项" in SYSTEM_PROMPT
    assert "同一事实只在最合适的章节表达一次" in SYSTEM_PROMPT
    assert "不得原样复述或改写后重复表达" in SYSTEM_PROMPT


def test_prompt_compares_primary_language_before_translating() -> None:
    assert "output_language 的主语言相同时" in SYSTEM_PROMPT


def test_prompt_condenses_the_overview_into_the_headline() -> None:
    assert "概括当下最重要且需要行动的信息" in SYSTEM_PROMPT
    assert "服务故障更紧急时可以作为标题" in SYSTEM_PROMPT
    assert "不要另写摘要段落" in SYSTEM_PROMPT
    assert "conclusions 通常合并为 1 至 2 项" in SYSTEM_PROMPT
    assert "不得重复 headline 已表达的事实" in SYSTEM_PROMPT
    assert "- overview:" not in SYSTEM_PROMPT
    assert "overview_source_ids" not in SYSTEM_PROMPT


def test_prompt_keeps_service_status_separate_and_surface_specific() -> None:
    assert "- service_status:" in SYSTEM_PROMPT
    assert "只写入 service_status" in SYSTEM_PROMPT
    assert "网页服务与 API 服务必须分别表述" in SYSTEM_PROMPT
    assert "不得把一个服务面的故障泛化为整个厂商故障" in SYSTEM_PROMPT
    assert "当前各服务面均正常且没有未解决事件时 service_status 必须为空" in SYSTEM_PROMPT


def test_prompt_keeps_each_advice_topic_concise() -> None:
    assert "每个 advice topic 只写一个短句" in SYSTEM_PROMPT
    assert "只保留明确行动和不可省略的数值或等级" in SYSTEM_PROMPT


def test_prompt_uses_a_soft_briefing_target_and_hard_output_limits() -> None:
    assert "briefing_target_characters" in SYSTEM_PROMPT
    assert "briefing_max_characters" in SYSTEM_PROMPT
    assert "llm_max_output_tokens" in SYSTEM_PROMPT
    assert "所需的最少来源" in SYSTEM_PROMPT


def test_prompt_requires_attribution_and_preserves_source_conflicts() -> None:
    assert "headline_source_ids 以及 conclusions" in SYSTEM_PROMPT
    assert "只能包含纯文本" in SYSTEM_PROMPT
    assert "不得使用 Markdown" in SYSTEM_PROMPT
    assert "不得拼接成无争议的单一结论" in SYSTEM_PROMPT
    assert "优先采用可识别的当地权威气象机构" in SYSTEM_PROMPT
    assert "input.required_advice_topics" in SYSTEM_PROMPT
