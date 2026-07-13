from weather_briefing.content_cleaners import ContentCleaningRules, HTMLContentCleaner


def test_html_cleaner_removes_page_chrome_without_rewriting_article_text() -> None:
    content = """
    <p>北京市气象台发布<br>今天有阵雨，最高温度32度。</p>
    <!-- analytics marker -->
    <div class="page-modal"><button>关闭</button><p>登录后查看奖励</p></div>
    <script>track()</script><time>2026年7月11日 12:57</time>
    """

    cleaned = HTMLContentCleaner().clean(
        content,
        ContentCleaningRules(
            remove_selectors=(".page-modal",),
            remove_patterns=(r"^北京市气象台发布$",),
        ),
    )

    assert cleaned == "今天有阵雨，最高温度32度。"


def test_html_cleaner_decodes_entities_to_visible_text() -> None:
    cleaned = HTMLContentCleaner().clean("<p>风力 7&amp;ndash;8 级</p>", ContentCleaningRules())

    assert cleaned == "风力 7&ndash;8 级"
