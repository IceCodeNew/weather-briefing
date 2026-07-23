import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

import pendulum
import pytest

from weather_briefing.models import Article, BriefingRecord, SourceDocument, Warning
from weather_briefing.state import SQLiteRuntimeDiagnostics, SQLiteStateStore


def test_rendered_text_diagnostics_can_be_enabled_and_disabled(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 14, 7, tz="UTC")
    expires_at = now.add(minutes=15)
    with SQLiteRuntimeDiagnostics(tmp_path / "state.db") as diagnostics:
        assert diagnostics.rendered_text_logging_until(now) is None

        diagnostics.enable_rendered_text_logging(expires_at)
        assert diagnostics.rendered_text_logging_until(now) == expires_at

        diagnostics.disable_rendered_text_logging()
        assert diagnostics.rendered_text_logging_until(now) is None


def test_rendered_text_diagnostics_reports_current_enabled_state(tmp_path: Path) -> None:
    with SQLiteRuntimeDiagnostics(tmp_path / "state.db") as diagnostics:
        diagnostics.enable_rendered_text_logging(pendulum.now("UTC").add(minutes=15))
        assert diagnostics.rendered_text_logging_enabled()

        diagnostics.disable_rendered_text_logging()
        assert not diagnostics.rendered_text_logging_enabled()


def test_rendered_text_diagnostics_changes_are_visible_across_connections(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 14, 7, tz="UTC")
    state_path = tmp_path / "state.db"
    with (
        SQLiteRuntimeDiagnostics(state_path) as daemon_diagnostics,
        SQLiteRuntimeDiagnostics(state_path) as cli_diagnostics,
    ):
        assert daemon_diagnostics.rendered_text_logging_until(now) is None

        expires_at = now.add(minutes=15)
        cli_diagnostics.enable_rendered_text_logging(expires_at)
        assert daemon_diagnostics.rendered_text_logging_until(now) == expires_at

        cli_diagnostics.disable_rendered_text_logging()
        assert daemon_diagnostics.rendered_text_logging_until(now) is None


def test_rendered_text_diagnostics_fail_fast_when_database_is_locked(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    with (
        SQLiteRuntimeDiagnostics(state_path) as diagnostics,
        closing(sqlite3.connect(state_path)) as lock_connection,
    ):
        lock_connection.execute("BEGIN EXCLUSIVE")
        started_at = monotonic()

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            diagnostics.rendered_text_logging_until()

        assert monotonic() - started_at < 1


def test_rendered_text_diagnostics_expire_automatically(tmp_path: Path, caplog) -> None:
    now = pendulum.datetime(2026, 7, 14, 7, tz="UTC")
    with SQLiteRuntimeDiagnostics(tmp_path / "state.db") as diagnostics:
        diagnostics.enable_rendered_text_logging(now.add(minutes=15))
        caplog.clear()

        with caplog.at_level("WARNING", logger="weather_briefing.state"):
            assert diagnostics.rendered_text_logging_until(now.add(minutes=16)) is None
            assert diagnostics.rendered_text_logging_until(now.add(minutes=17)) is None

    assert caplog.text.count("Sensitive rendered text diagnostic logging expired") == 1


def test_articles_are_deduplicated(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    article = Article("id", "source", "Source", "Title", "https://example.invalid", now, "body")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.known_article_ids((article.id,)) == set()
        state.save_articles((article,), now)
        assert state.known_article_ids((article.id,)) == {article.id}
        stored = state.recent_articles(now.add(hours=1), 2)[0]
        assert stored.published_at.to_iso8601_string() == "2026-07-13T01:00:00Z"


def test_pending_articles_remain_until_marked_processed(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    article = Article("id", "source", "Source", "Title", "https://example.invalid", now, "body")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_pending_articles((article,), now)

        assert state.known_article_ids((article.id,)) == set()
        assert state.pending_articles() == (article,)

        state.mark_articles_processed((article,), now.add(hours=1))

        assert state.pending_articles() == ()
        assert state.known_article_ids((article.id,)) == {article.id}


def test_published_result_is_committed_with_verbatim_queue(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    regular = Article("regular", "source", "Source", "Regular", "https://example.invalid/r", now, "regular")
    verbatim = Article(
        "verbatim",
        "source",
        "Source",
        "Verbatim",
        "https://example.invalid/v",
        now,
        "verbatim",
        is_verbatim=True,
    )
    document = SourceDocument("context", "Context", "https://example.invalid/c", "context")
    warning = Warning("warning", "Warning", "active", "detail", (regular.id,), now)

    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_pending_articles((regular, verbatim), now.subtract(hours=1))
        state.commit_result(
            kind="briefing",
            body="Published briefing",
            articles=(regular, verbatim),
            context_documents=(document,),
            active_warnings=(warning,),
            resolved_warning_ids=(),
            recorded_at=now,
            verbatim_silent=True,
        )

        assert state.pending_articles() == ()
        assert state.known_article_ids((regular.id, verbatim.id)) == {regular.id, verbatim.id}
        assert tuple(record.body for record in state.recent_briefings(now, 1)) == ("Published briefing",)
        assert state.recent_context_documents(now, 1) == (document,)
        assert state.active_warnings(now, 1) == (warning,)
        queued = state.pending_verbatim_deliveries()
        assert tuple(delivery.article for delivery in queued) == (verbatim,)
        assert tuple(delivery.silent for delivery in queued) == (True,)


def test_unpublished_result_commits_pending_state_without_delivery_queue(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    article = Article(
        "verbatim",
        "source",
        "Source",
        "Verbatim",
        "https://example.invalid/v",
        now,
        "verbatim",
        is_verbatim=True,
    )
    document = SourceDocument("context", "Context", "https://example.invalid/c", "context")
    warning = Warning("warning", "Warning", "active", "detail", (article.id,), now)

    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.commit_result(
            kind="briefing",
            body=None,
            articles=(article,),
            context_documents=(document,),
            active_warnings=(warning,),
            resolved_warning_ids=(),
            recorded_at=now,
            verbatim_silent=False,
        )

        assert state.pending_articles() == (article,)
        assert state.known_article_ids((article.id,)) == set()
        assert state.recent_briefings(now, 1) == ()
        assert state.recent_context_documents(now, 1) == (document,)
        assert state.active_warnings(now, 1) == (warning,)
        assert state.pending_verbatim_deliveries() == ()


def test_verbatim_queue_preserves_order_and_acknowledges_one_item(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    articles = tuple(
        Article(
            identifier,
            "source",
            "Source",
            identifier,
            f"https://example.invalid/{identifier}",
            now,
            identifier,
            is_verbatim=True,
        )
        for identifier in ("second-by-id", "first-by-id")
    )

    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.commit_result(
            kind="briefing",
            body="Published briefing",
            articles=articles,
            context_documents=(),
            active_warnings=(),
            resolved_warning_ids=(),
            recorded_at=now,
            verbatim_silent=False,
        )

        queued = state.pending_verbatim_deliveries()
        assert tuple(delivery.article for delivery in queued) == articles
        assert tuple(delivery.silent for delivery in queued) == (False, False)

        state.acknowledge_verbatim_delivery(articles[0].id)

        assert tuple(delivery.article for delivery in state.pending_verbatim_deliveries()) == (articles[1],)


def test_verbatim_queue_rejects_missing_article_reference(tmp_path: Path) -> None:
    with (
        SQLiteStateStore(tmp_path / "state.db") as state,
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"),
    ):
        state._connection.execute(
            """INSERT INTO verbatim_delivery_queue(article_id, silent, queued_at)
            VALUES (?, ?, ?)""",
            ("missing", False, "2026-07-13T01:00:00.000000Z"),
        )


def test_result_checkpoint_rolls_back_all_state_and_can_be_retried(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    state_path = tmp_path / "state.db"
    article = Article(
        "verbatim",
        "source",
        "Source",
        "Verbatim",
        "https://example.invalid/v",
        now,
        "verbatim",
        is_verbatim=True,
    )
    document = SourceDocument("context", "Context", "https://example.invalid/c", "context")
    old_warning = Warning("old-warning", "Old", "active", "old", (article.id,), now.subtract(hours=1))
    new_warning = Warning("new-warning", "New", "active", "new", (article.id,), now)

    with SQLiteStateStore(state_path) as state:
        state.save_pending_articles((article,), now.subtract(hours=1))
        state.update_warnings((old_warning,), (), now.subtract(hours=1), {article.id})
        with closing(sqlite3.connect(state_path)) as connection:
            connection.executescript(
                """CREATE TRIGGER abort_warning_insert BEFORE INSERT ON warnings
                BEGIN SELECT RAISE(ABORT, 'warning insert failed'); END;"""
            )

        with pytest.raises(sqlite3.IntegrityError, match="warning insert failed"):
            state.commit_result(
                kind="briefing",
                body="Published briefing",
                articles=(article,),
                context_documents=(document,),
                active_warnings=(new_warning,),
                resolved_warning_ids=(old_warning.id,),
                recorded_at=now,
                verbatim_silent=True,
            )

        assert state.pending_articles() == (article,)
        assert state.known_article_ids((article.id,)) == set()
        assert state.recent_briefings(now, 1) == ()
        assert state.recent_context_documents(now, 1) == ()
        assert state.active_warnings(now, 2) == (old_warning,)
        assert state.pending_verbatim_deliveries() == ()

        with closing(sqlite3.connect(state_path)) as connection:
            connection.execute("DROP TRIGGER abort_warning_insert")
            connection.commit()

        state.commit_result(
            kind="briefing",
            body="Published briefing",
            articles=(article,),
            context_documents=(document,),
            active_warnings=(new_warning,),
            resolved_warning_ids=(old_warning.id,),
            recorded_at=now,
            verbatim_silent=True,
        )

        assert state.pending_articles() == ()
        assert state.known_article_ids((article.id,)) == {article.id}
        assert tuple(record.body for record in state.recent_briefings(now, 1)) == ("Published briefing",)
        assert state.recent_context_documents(now, 1) == (document,)
        assert state.active_warnings(now, 1) == (new_warning,)
        assert tuple(delivery.article for delivery in state.pending_verbatim_deliveries()) == (article,)


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


def test_context_snapshots_are_available_for_briefing_change_detection(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    document = SourceDocument(
        "weather:test",
        "Weather API",
        "https://example.invalid/weather",
        "Rain expected at 10:00",
        history_summary="Rain expected",
        history_value="Rain expected",
    )
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_context_documents((document,), now)

        assert state.recent_context_documents(now.add(hours=1), 2) == (document,)
        assert state.recent_context_documents(now.add(hours=3), 2) == ()


def test_context_snapshot_language_is_persisted(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    document = SourceDocument(
        "weather:jma",
        "JMA",
        "https://example.invalid/jma",
        "雨",
        language="ja",
    )

    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_context_documents((document,), now)

        assert state.recent_context_documents(now.add(hours=1), 2) == (document,)


def test_existing_context_snapshot_schema_adds_history_fields(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-state.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """CREATE TABLE context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                name TEXT NOT NULL, url TEXT NOT NULL, content TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )"""
        )
        connection.commit()
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    document = SourceDocument(
        "weather:test",
        "Weather API",
        "https://example.invalid/weather",
        "Full weather history",
        history_summary="Weather summary",
        history_value="Weather value",
    )

    with SQLiteStateStore(database_path) as state:
        state.save_context_documents((document,), now)
        assert state.recent_context_documents(now, 1) == (document,)


def test_utc_timestamps_have_stable_lexical_order_with_microseconds(tmp_path: Path) -> None:
    with SQLiteStateStore(tmp_path / "state.db") as state:
        first = pendulum.datetime(2026, 7, 13, 1, tz="UTC")
        second = first.add(microseconds=1)
        state.save_briefing("briefing", "second", second)
        state.save_briefing("briefing", "first", first)

        assert state.recent_briefings(first.add(hours=1), 2) == (
            BriefingRecord("briefing", "first", first),
            BriefingRecord("briefing", "second", second),
        )


def test_briefing_delivery_can_be_checked_within_local_day_bounds(tmp_path: Path) -> None:
    timezone = pendulum.timezone("Asia/Shanghai")
    now = pendulum.datetime(2026, 7, 14, 16, tz=timezone)
    day_start = now.start_of("day")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.save_briefing("briefing", "yesterday", day_start.subtract(minutes=1))
        state.save_briefing("forecast", "today forecast", day_start.add(hours=8))

        assert not state.has_briefing_between("briefing", day_start, now)

        state.save_briefing("briefing", "today briefing", day_start.add(hours=12))

        assert state.has_briefing_between("briefing", day_start, now)


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
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        state.record_failure()
        state.record_failure()
        state.record_success(now, history_hours=48, warning_retention_hours=12)
        assert state.record_failure() == 1


def test_record_success_reopens_task_failure_alert(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    with SQLiteStateStore(tmp_path / "state.db") as state:
        assert state.task_failure_requires_alert()
        state.mark_task_failure_alerted(now)
        assert not state.task_failure_requires_alert()

        state.record_success(now, history_hours=48, warning_retention_hours=12)
        assert state.task_failure_requires_alert()


def test_record_success_prunes_expired_history_and_preserves_boundaries(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    history_boundary = now.subtract(hours=48)
    warning_boundary = now.subtract(hours=12)
    state_path = tmp_path / "state.db"
    with SQLiteStateStore(state_path) as state:
        old_article = Article(
            "old-article",
            "source",
            "Source",
            "Old",
            "https://example.invalid/old",
            history_boundary.subtract(hours=1),
            "old",
        )
        boundary_article = Article(
            "boundary-article",
            "source",
            "Source",
            "Boundary",
            "https://example.invalid/boundary",
            history_boundary,
            "boundary",
        )
        state.save_articles((old_article,), history_boundary.subtract(seconds=1))
        state.save_articles((boundary_article,), history_boundary)
        state.save_briefing("briefing", "old", history_boundary.subtract(seconds=1))
        state.save_briefing("briefing", "boundary", history_boundary)

        old_document = SourceDocument("old-context", "Old", "https://example.invalid/old-context", "old")
        boundary_document = SourceDocument(
            "boundary-context",
            "Boundary",
            "https://example.invalid/boundary-context",
            "boundary",
        )
        state.save_context_documents((old_document,), history_boundary.subtract(seconds=1))
        state.save_context_documents((boundary_document,), history_boundary)

        old_warning = Warning("old-warning", "Old", "active", "old", ("source",), now)
        boundary_warning = Warning("boundary-warning", "Boundary", "active", "boundary", ("source",), now)
        state.update_warnings((old_warning,), (), warning_boundary.subtract(seconds=1), {"source"})
        state.update_warnings((boundary_warning,), (), warning_boundary, {"source"})

        state.record_success(now, history_hours=48, warning_retention_hours=12)

    with closing(sqlite3.connect(state_path)) as connection:
        assert connection.execute("SELECT id FROM articles ORDER BY id").fetchall() == [("boundary-article",)]
        assert connection.execute("SELECT body FROM briefings ORDER BY body").fetchall() == [("boundary",)]
        assert connection.execute("SELECT source_id FROM context_snapshots ORDER BY source_id").fetchall() == [
            ("boundary-context",)
        ]
        assert connection.execute("SELECT id FROM warnings ORDER BY id").fetchall() == [("boundary-warning",)]


def test_record_success_retains_articles_referenced_by_verbatim_queue(tmp_path: Path) -> None:
    now = pendulum.datetime(2026, 7, 13, 9, tz="Asia/Shanghai")
    processed_at = now.subtract(hours=49)
    article = Article(
        "queued",
        "source",
        "Source",
        "Queued",
        "https://example.invalid/queued",
        processed_at,
        "queued body",
        is_verbatim=True,
    )
    state_path = tmp_path / "state.db"

    with SQLiteStateStore(state_path) as state:
        state.commit_result(
            kind="briefing",
            body="Published briefing",
            articles=(article,),
            context_documents=(),
            active_warnings=(),
            resolved_warning_ids=(),
            recorded_at=processed_at,
            verbatim_silent=False,
        )

        state.record_success(now, history_hours=48, warning_retention_hours=12)

        assert tuple(delivery.article for delivery in state.pending_verbatim_deliveries()) == (article,)

        state.acknowledge_verbatim_delivery(article.id)
        state.record_success(now, history_hours=48, warning_retention_hours=12)

    with closing(sqlite3.connect(state_path)) as connection:
        assert connection.execute("SELECT id FROM articles").fetchall() == []


def test_parse_time_rejects_invalid_format() -> None:
    import pytest

    from weather_briefing.persistence.serialization import _parse_time

    with pytest.raises(ValueError, match="fixed-width UTC format"):
        _parse_time("invalid")
