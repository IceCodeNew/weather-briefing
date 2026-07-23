from unittest.mock import patch

import pytest

from weather_briefing.data.prompts import SYSTEM_PROMPT, _load_system_prompt


def test_system_prompt_load_failure_is_actionable() -> None:
    with (
        patch("weather_briefing.data.prompts.resources.files", side_effect=OSError("unreadable")),
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
    assert "将当下最重要的天气概况浓缩其中" in SYSTEM_PROMPT
    assert "不要另写摘要段落" in SYSTEM_PROMPT
    assert "- overview:" not in SYSTEM_PROMPT
    assert "overview_source_ids" not in SYSTEM_PROMPT


def test_prompt_requires_attribution_and_preserves_source_conflicts() -> None:
    assert "headline_source_ids 以及 conclusions" in SYSTEM_PROMPT
    assert "不得拼接成无争议的单一结论" in SYSTEM_PROMPT
    assert "优先采用可识别的当地权威气象机构" in SYSTEM_PROMPT
    assert "input.required_advice_topics" in SYSTEM_PROMPT
