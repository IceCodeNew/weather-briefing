import pendulum

from weather_briefing.models import Article, BriefingResult, Conclusion, SourceDocument, Warning
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
        "Warm & humid",
        (Conclusion("Carry an umbrella", ("source",)),),
    )

    rendered = TelegramHTMLRenderer().render_briefing(result, (article,), ())

    assert rendered.body.startswith("<b>Daily &lt;Forecast&gt;</b>")
    assert "Warm &amp; humid" in rendered.body
    assert '<a href="https://example.invalid/?a=1&amp;b=2">来源</a>' in rendered.body
    assert rendered.visible_length == len("Daily <Forecast>\n\nWarm & humid\n\n天气信息\n\n• Carry an umbrella 来源")


def test_plain_text_renderer_uses_the_same_structured_briefing() -> None:
    result = BriefingResult("Daily", "Overview", ())

    rendered = PlainTextRenderer().render_briefing(result, (), ())

    assert rendered.body == "Daily\n\nOverview"
    assert "<b>" not in rendered.body


def test_telegram_html_renderer_uses_context_source_name_as_attribution() -> None:
    context = SourceDocument(
        "allergen:open-meteo",
        "Open-Meteo / CAMS ENSEMBLE 花粉过敏原",
        "https://open-meteo.com/en/docs/air-quality-api",
        "Pollen data",
    )
    result = BriefingResult(
        "Daily",
        "Overview",
        (),
        advice=(Conclusion("花粉浓度较高", ("allergen:open-meteo",)),),
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
        "Overview",
        (),
        advice=(Conclusion("花粉浓度较高", ("allergen:open-meteo",)),),
    )

    rendered = PlainTextRenderer().render_briefing(result, (), (context,))

    assert ("Open-Meteo / CAMS ENSEMBLE 花粉过敏原: https://open-meteo.com/en/docs/air-quality-api") in rendered.body


def test_telegram_html_renders_active_warnings_section() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title", "https://example.invalid/a", now, "Body")
    warning = Warning("w1", "暴雨", "active", "暴雨预警", ("source",), now)
    result = BriefingResult(
        "Daily",
        "Overview",
        (),
        active_warnings=(warning,),
    )

    rendered = TelegramHTMLRenderer().render_briefing(result, (article,), ())

    assert "<b>当前生效的气象预警</b>" in rendered.body
    assert "暴雨" in rendered.body
    assert '<a href="https://example.invalid/a">来源</a>' in rendered.body


def test_plain_text_renders_active_warnings_section() -> None:
    now = pendulum.datetime(2026, 7, 11, 8, tz="Asia/Shanghai")
    article = Article("source", "feed", "Feed", "Title", "https://example.invalid/a", now, "Body")
    warning = Warning("w1", "暴雨", "active", "暴雨预警", ("source",), now)
    result = BriefingResult(
        "Daily",
        "Overview",
        (),
        active_warnings=(warning,),
    )

    rendered = PlainTextRenderer().render_briefing(result, (article,), ())

    assert "当前生效的气象预警" in rendered.body
    assert "暴雨" in rendered.body
    assert "https://example.invalid/a" in rendered.body


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
