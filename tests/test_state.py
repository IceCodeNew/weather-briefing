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


def test_failed_source_check_preserves_last_article_time(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_source_check("source", now, now.subtract(hours=25))
        state.record_source_check("source", now.add(hours=1), None)

        assert state.stale_sources(("source",), now.add(hours=2), 24) == ["source"]


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


def test_stale_source_skips_unknown_source_id(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.stale_sources(("unknown",), now, 24) == []


def test_mark_stale_sources_alerted_with_empty_ids_is_noop(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.mark_stale_sources_alerted((), now)


def test_mark_rss_failure_alerted_with_empty_ids_is_noop(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.mark_rss_failure_alerted((), now)


def test_rss_failure_alert_is_suppressed_until_fetch_success(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.record_rss_fetch_failure("source") == 1
        assert state.rss_sources_requiring_failure_alert(("source",), 2) == []

        assert state.record_rss_fetch_failure("source") == 2
        assert state.rss_sources_requiring_failure_alert(("source",), 2) == ["source"]
        state.mark_rss_failure_alerted(("source",), now)
        assert state.rss_sources_requiring_failure_alert(("source",), 2) == []

        assert state.record_rss_fetch_failure("source") == 3
        assert state.rss_sources_requiring_failure_alert(("source",), 2) == []

        state.record_rss_fetch_success("source")
        assert state.record_rss_fetch_failure("source") == 1
        assert state.rss_sources_requiring_failure_alert(("source",), 1) == ["source"]


def test_rss_failure_alert_sources_are_sorted(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_rss_fetch_failure("beta")
        state.record_rss_fetch_failure("alpha")

        assert state.rss_sources_requiring_failure_alert(("beta", "alpha"), 1) == ["alpha", "beta"]


def test_record_failure_increments_consecutive_count(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.record_failure() == 1
        assert state.record_failure() == 2
        assert state.record_failure() == 3


def test_record_success_resets_failure_count(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_failure()
        state.record_failure()
        state.record_success()
        assert state.record_failure() == 1


def test_record_success_reopens_task_failure_alert(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.task_failure_requires_alert()
        state.mark_task_failure_alerted(now)
        assert not state.task_failure_requires_alert()

        state.record_success()
        assert state.task_failure_requires_alert()


def test_parse_time_rejects_invalid_format() -> None:
    import pytest

    from weather_briefing.state import _parse_time

    with pytest.raises(ValueError, match="fixed-width UTC format"):
        _parse_time("invalid")
