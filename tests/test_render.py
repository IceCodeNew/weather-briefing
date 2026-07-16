import pendulum
import pytest

from weather_briefing.models import (
    Advice,
    AdviceTopic,
    Article,
    BriefingResult,
    Conclusion,
    SourceDocument,
    Warning,
)
from weather_briefing.render import PlainTextRenderer, TelegramHTMLRenderer


def test_render_briefing_uses_safe_telegram_html() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article(
        "source",
        "feed",
        "Feed",
        "Forecast <alert>",
        "https://example.invalid/?a=1&b=2",
        now,
        "Body & details",
    )
    result = BriefingResult(
        "Daily <Forecast>",
        ("source",),
        (Conclusion("Carry an umbrella", ("source",)),),
    )

    rendered = TelegramHTMLRenderer().render_briefing(result, (article,), ())

    assert rendered.body.startswith("<b>Daily &lt;Forecast&gt;</b> （来源：")
    assert '<a href="https://example.invalid/?a=1&amp;b=2">Feed</a>' in rendered.body
    assert rendered.visible_length == len(
        "Daily <Forecast> （来源：Feed）\n\n天气信息\n\n• Carry an umbrella （来源：Feed）"
    )


def test_plain_text_renderer_uses_the_same_structured_briefing() -> None:
    context = SourceDocument("source", "Source", "https://example.invalid/source", "")
    result = BriefingResult("Daily", ("source",), ())

    rendered = PlainTextRenderer().render_briefing(result, (), (context,))

    assert rendered.body == "Daily （来源：Source: https://example.invalid/source）"
    assert "<b>" not in rendered.body


@pytest.mark.parametrize("renderer", (TelegramHTMLRenderer(), PlainTextRenderer()))
def test_renderers_fail_when_a_source_reference_is_missing(
    renderer: TelegramHTMLRenderer | PlainTextRenderer,
) -> None:
    result = BriefingResult("Daily", ("missing",), ())

    with pytest.raises(KeyError, match="missing"):
        renderer.render_briefing(result, (), ())


def test_renderers_fall_back_to_source_id_for_legacy_blank_name() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "  ", "Title", "https://example.invalid/a", now, "Body")
    result = BriefingResult(
        "Daily",
        ("source",),
        (Conclusion("Update", ("source",)),),
    )

    html = TelegramHTMLRenderer().render_briefing(result, (article,), ())
    plain = PlainTextRenderer().render_briefing(result, (article,), ())

    assert '<a href="https://example.invalid/a">feed</a>' in html.body
    assert "feed: https://example.invalid/a" in plain.body


def test_telegram_html_renderer_uses_context_source_name_as_attribution() -> None:
    context = SourceDocument(
        "allergen:open-meteo",
        "Open-Meteo / CAMS ENSEMBLE 花粉过敏原",
        "https://open-meteo.com/en/docs/air-quality-api",
        "Pollen data",
    )
    result = BriefingResult(
        "Daily",
        ("allergen:open-meteo",),
        (),
        advice=(Advice(AdviceTopic.ALLERGEN, "花粉浓度较高", ("allergen:open-meteo",)),),
    )

    rendered = TelegramHTMLRenderer().render_briefing(result, (), (context,))

    assert (
        '<a href="https://open-meteo.com/en/docs/air-quality-api">Open-Meteo / CAMS ENSEMBLE 花粉过敏原</a>'
    ) in rendered.body


def test_plain_text_renderer_uses_context_source_name_as_attribution() -> None:
    context = SourceDocument(
        "allergen:open-meteo",
        "Open-Meteo / CAMS ENSEMBLE 花粉过敏原",
        "https://open-meteo.com/en/docs/air-quality-api",
        "Pollen data",
    )
    result = BriefingResult(
        "Daily",
        ("allergen:open-meteo",),
        (),
        advice=(Advice(AdviceTopic.ALLERGEN, "花粉浓度较高", ("allergen:open-meteo",)),),
    )

    rendered = PlainTextRenderer().render_briefing(result, (), (context,))

    assert ("Open-Meteo / CAMS ENSEMBLE 花粉过敏原: https://open-meteo.com/en/docs/air-quality-api") in rendered.body


def test_telegram_html_renders_active_warnings_section() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title", "https://example.invalid/a", now, "Body")
    warning = Warning("w1", "暴雨", "active", "暴雨预警", ("source",), now)
    result = BriefingResult(
        "Daily",
        ("source",),
        (),
        active_warnings=(warning,),
    )

    rendered = TelegramHTMLRenderer().render_briefing(result, (article,), ())

    assert "<b>气象预警</b>" in rendered.body
    assert "暴雨" in rendered.body
    assert '<a href="https://example.invalid/a">Feed</a>' in rendered.body


def test_plain_text_renders_active_warnings_section() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title", "https://example.invalid/a", now, "Body")
    warning = Warning("w1", "暴雨", "active", "暴雨预警", ("source",), now)
    result = BriefingResult(
        "Daily",
        ("source",),
        (),
        active_warnings=(warning,),
    )

    rendered = PlainTextRenderer().render_briefing(result, (article,), ())

    assert "气象预警" in rendered.body
    assert "暴雨" in rendered.body
    assert "https://example.invalid/a" in rendered.body
    assert "Feed: https://example.invalid/a" in rendered.body


@pytest.mark.parametrize("renderer", (TelegramHTMLRenderer(), PlainTextRenderer()))
def test_briefing_sections_follow_the_compact_order(
    renderer: TelegramHTMLRenderer | PlainTextRenderer,
) -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    context = SourceDocument("source", "Source", "https://example.invalid/weather", "")
    warning = Warning("w1", "暴雨", "生效", "注意防范", ("source",), now)
    result = BriefingResult(
        "今日闷热，午后有雨",
        ("source",),
        (Conclusion("最高气温38℃", ("source",)),),
        active_warnings=(warning,),
        advice=(Advice(AdviceTopic.EXERCISE, "避免高温时段运动", ("source",)),),
        disaster_tracking=(Conclusion("台风向西北方向移动", ("source",)),),
    )

    body = renderer.render_briefing(result, (), (context,)).body

    assert body.index("天气信息") < body.index("气象预警")
    assert body.index("气象预警") < body.index("自然灾害动态")
    assert body.index("自然灾害动态") < body.index("生活建议")


@pytest.mark.parametrize("renderer", (TelegramHTMLRenderer(), PlainTextRenderer()))
def test_attribution_deduplicates_only_identical_named_links(
    renderer: TelegramHTMLRenderer | PlainTextRenderer,
) -> None:
    context = (
        SourceDocument("weather", "QWeather", "https://example.invalid/shanghai", ""),
        SourceDocument("air", "QWeather", "https://example.invalid/shanghai", ""),
        SourceDocument("warning", "QWeather", "https://example.invalid/warning", ""),
    )
    result = BriefingResult("今日炎热", ("weather", "air", "warning"), ())

    body = renderer.render_briefing(result, (), context).body

    assert body.count("https://example.invalid/shanghai") == 1
    assert body.count("https://example.invalid/warning") == 1
    assert body.count("QWeather") == 2


def test_telegram_html_render_verbatim() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title & <details>", "https://example.invalid/a", now, "Body content")

    rendered = TelegramHTMLRenderer().render_verbatim(article)

    assert rendered.body == "<b>Title &amp; &lt;details&gt;</b>\n\nBody content"


def test_telegram_html_render_alert() -> None:
    rendered = TelegramHTMLRenderer().render_alert("Alert Title", "Alert <Body>")

    assert rendered.body == "<b>Alert Title</b>\n\nAlert &lt;Body&gt;"


def test_plain_text_render_verbatim() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title", "https://example.invalid/a", now, "Body content")

    rendered = PlainTextRenderer().render_verbatim(article)

    assert rendered.body == "Title\n\nBody content"


def test_plain_text_render_alert() -> None:
    rendered = PlainTextRenderer().render_alert("Alert Title", "Alert Body")

    assert rendered.body == "Alert Title\n\nAlert Body"
