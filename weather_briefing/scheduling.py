"""Scheduling and final-window delivery policy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    import pendulum

    from .config import Settings
    from .state import SQLiteStateStore


def in_schedule(kind: str, now: pendulum.DateTime, settings: Settings) -> bool:
    """Return whether a task is inside its configured local-time window."""
    if kind == "forecast":
        return now.hour == settings.greeting_hour
    return hour_in_cron(now.hour, settings.hourly_cron)


def hour_in_cron(hour: int, cron_hour: str) -> bool:
    """Return whether an hour matches one APScheduler cron hour expression."""
    if not 0 <= hour <= 23:  # noqa: PLR2004
        return False
    current_hour = datetime(2000, 1, 1, hour, tzinfo=UTC)
    trigger = CronTrigger(hour=cron_hour, timezone=UTC)
    return trigger.get_next_fire_time(None, current_hour) == current_hour


def _is_last_briefing_window(now: pendulum.DateTime, cron_hour: str) -> bool:
    return hour_in_cron(now.hour, cron_hour) and not any(
        hour_in_cron(hour, cron_hour) for hour in range(now.hour + 1, 24)
    )


def briefing_delivery_policy(
    kind: str,
    now: pendulum.DateTime,
    settings: Settings,
    *,
    run_now: bool,
    briefing_sent_today: bool,
) -> tuple[bool, bool]:
    """Return force and silent flags for one scheduled or manual run."""
    if kind != "briefing":
        return False, False
    if run_now:
        return True, False
    if _is_last_briefing_window(now, settings.hourly_cron) and not briefing_sent_today:
        return True, True
    return False, False


def briefing_sent_today(
    kind: str,
    now: pendulum.DateTime,
    settings: Settings,
    state: SQLiteStateStore,
    *,
    run_now: bool,
) -> bool:
    """Read delivery history only when the final briefing window needs it."""
    if kind != "briefing" or run_now or not _is_last_briefing_window(now, settings.hourly_cron):
        return False
    local_now = now.in_timezone(settings.timezone)
    return state.has_briefing_between("briefing", local_now.start_of("day"), local_now)
