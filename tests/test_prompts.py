from weather_briefing.prompts import SYSTEM_PROMPT


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


def test_prompt_separates_advice_and_avoids_repetition() -> None:
    assert "过敏原信息只能放入 advice" in SYSTEM_PROMPT
    assert "不得使用“原始浓度”" in SYSTEM_PROMPT
    assert "不得在 conclusions 中重复" in SYSTEM_PROMPT


def test_prompt_requires_attribution_and_preserves_source_conflicts() -> None:
    assert "headline_source_ids、overview_source_ids" in SYSTEM_PROMPT
    assert "不得拼接成无争议的单一结论" in SYSTEM_PROMPT
    assert "优先采用可识别的当地权威气象机构" in SYSTEM_PROMPT
    assert "input.required_advice_topics" in SYSTEM_PROMPT
