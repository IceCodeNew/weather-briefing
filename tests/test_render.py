from datetime import datetime
from zoneinfo import ZoneInfo

from weather_briefing.models import Article, BriefingResult, Conclusion
from weather_briefing.render import PlainTextRenderer, TelegramHTMLRenderer


def test_render_briefing_uses_safe_telegram_html() -> None:
    now = datetime(2026, 7, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
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
