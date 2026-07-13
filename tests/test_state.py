from pathlib import Path

import pendulum

from weather_briefing.models import Article, SourceDocument, Warning
from weather_briefing.state import SQLiteStateStore


def test_articles_are_deduplicated(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    article = Article("id", "source", "Source", "Title", "https://example.invalid", now, "body")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.known_article_ids((article.id,)) == set()
        state.save_articles((article,), now)
        assert state.known_article_ids((article.id,)) == {article.id}
        stored = state.recent_articles(now.add(hours=1), 2)[0]
        assert stored.published_at.to_iso8601_string() == "2026-07-13T01:00:00Z"


def test_source_becomes_stale_after_threshold(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_source_check("source", now.subtract(hours=25), now.subtract(hours=25))
        assert state.stale_sources(("source",), now, 24) == ["source"]


def test_new_empty_source_is_not_immediately_stale(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_source_check("source", now, None)
        assert state.stale_sources(("source",), now, 24) == []


def test_stale_source_alert_is_sent_once_until_a_new_article_arrives(tmp_path: Path) -> None:
    first_article = pendulum.datetime(2026, 7, 11, 8, tz="UTC")
    stale_check = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_source_check("source", first_article, first_article)
        assert state.stale_sources_requiring_alert(("source",), stale_check, 24) == ["source"]

        state.mark_stale_sources_alerted(("source",), stale_check)
        assert state.stale_sources_requiring_alert(("source",), stale_check.add(hours=1), 24) == []

        new_article = stale_check.add(hours=2)
        state.record_source_check("source", new_article, new_article)
        assert state.stale_sources_requiring_alert(("source",), new_article.add(hours=25), 24) == ["source"]


def test_removed_warning_is_resolved_immediately(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    warning = Warning("warning", "Warning", "active", "detail", ("source",), now)
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.update_warnings((warning,), (), now)
        assert state.active_warnings(now, 12) == (warning,)
        state.update_warnings((), ("warning",), now.add(hours=1))
        assert state.active_warnings(now.add(hours=1), 12) == ()


def test_historical_warning_evidence_does_not_refresh_retention(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    warning = Warning("warning", "Warning", "active", "detail", ("old-source",), now)
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.update_warnings((warning,), (), now, {"old-source"})
        later = now.add(hours=13)
        state.update_warnings((warning,), (), later, {"different-new-source"})
        assert state.active_warnings(later, 12) == ()


def test_new_warning_evidence_refreshes_retention(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    warning = Warning("warning", "Warning", "active", "detail", ("source",), now)
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.update_warnings((warning,), (), now, {"source"})
        later = now.add(hours=11)
        state.update_warnings((warning,), (), later, {"source"})
        assert len(state.active_warnings(later.add(hours=11), 12)) == 1


def test_context_snapshots_are_available_for_hourly_change_detection(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    document = SourceDocument(
        "weather:test",
        "Weather API",
        "https://example.invalid/weather",
        "Rain expected at 10:00",
    )
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_context_documents((document,), now)

        assert state.recent_context_documents(now.add(hours=1), 2) == (document,)
        assert state.recent_context_documents(now.add(hours=3), 2) == ()


def test_utc_timestamps_have_stable_lexical_order_with_microseconds(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.db") as state:
        first = pendulum.datetime(2026, 7, 13, 1, tz="UTC")
        second = first.add(microseconds=1)
        state.save_briefing("hourly", "second", second)
        state.save_briefing("hourly", "first", first)

        assert state.recent_briefings(first.add(hours=1), 2) == ("first", "second")
