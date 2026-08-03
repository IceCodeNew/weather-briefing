"""Configuration contract required by briefing orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import pendulum

    from weather_briefing.models import FeedConfig


class BriefingSettings(Protocol):
    """Expose only settings consumed by the briefing use case."""

    @property
    def timezone(self) -> pendulum.Timezone:
        """Return the briefing timezone."""
        ...

    @property
    def feeds(self) -> tuple[FeedConfig, ...]:
        """Return configured RSS feeds."""
        ...

    @property
    def rss_stale_hours(self) -> int:
        """Return the RSS staleness threshold in hours."""
        ...

    @property
    def rss_failure_threshold(self) -> int:
        """Return the consecutive RSS failure alert threshold."""
        ...

    @property
    def warning_retention_hours(self) -> int:
        """Return the active-warning retention window in hours."""
        ...

    @property
    def history_hours(self) -> int:
        """Return the retained briefing context window in hours."""
        ...

    @property
    def llm_history_max_documents(self) -> int:
        """Return the maximum historical context snapshots sent to the LLM."""
        ...

    @property
    def llm_history_max_characters(self) -> int:
        """Return the serialized character budget for historical context."""
        ...

    @property
    def briefing_max_characters(self) -> int:
        """Return the configured briefing character budget."""
        ...

    @property
    def llm_max_output_tokens(self) -> int:
        """Return the configured structured output token budget."""
        ...

    @property
    def llm_max_attempts(self) -> int:
        """Return the maximum LLM validation attempts."""
        ...
