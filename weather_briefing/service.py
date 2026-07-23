"""Core briefing orchestration and output contract enforcement."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

import pendulum

from .application.collection import collect_rss_articles, collect_weather_documents
from .application.context_history import (
    HistoricalContextOverflow as _HistoricalContextOverflow,
)
from .application.context_history import bounded_context_history as _bounded_context_history
from .application.context_history import context_budget_fingerprints as _context_budget_fingerprints
from .application.payloads import build_briefing_payload
from .application.summarization import summarize_validated
from .delivery import DeliveryError, DeliveryProvider
from .llm import LLMError, LLMProvider, serialize_llm_payload
from .models import (
    AdviceTopic,
    Article,
    BriefingResult,
    FeedConfig,
    ResolvedLocation,
    SourceDocument,
)
from .sources import RSSFeedSource
from .state import SQLiteStateStore
from .time_utils import require_aware_datetime
from .weather import WeatherContextProvider

_LOGGER = logging.getLogger("weather_briefing.service")


class BriefingSettings(Protocol):
    """Expose the settings required by briefing orchestration."""

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
    def llm_max_attempts(self) -> int:
        """Return the maximum LLM validation attempts."""
        ...


class BriefingService:
    """Orchestrate source collection, validation, state, and delivery."""

    def __init__(
        self,
        settings: BriefingSettings,
        location: ResolvedLocation,
        state: SQLiteStateStore,
        rss_source: RSSFeedSource,
        llm: LLMProvider,
        delivery: DeliveryProvider,
        ops_delivery: DeliveryProvider,
        weather_context_provider: WeatherContextProvider | None = None,
    ) -> None:
        """Compose briefing orchestration from its location-scoped dependencies."""
        self._settings = settings
        self._location = location
        self._state = state
        self._rss_source = rss_source
        self._llm = llm
        self._delivery = delivery
        self._ops_delivery = ops_delivery
        self._weather_context_provider = weather_context_provider

    async def run(
        self,
        kind: str,
        now: pendulum.DateTime | None = None,
        *,
        forecast_date: pendulum.Date | None = None,
        force_publish: bool = False,
        silent: bool = False,
    ) -> str | None:
        """Run one forecast or briefing task and persist its outcome."""
        current_time = require_aware_datetime(now or pendulum.now(self._settings.timezone), context="Briefing run time")
        if forecast_date is not None and kind != "forecast":
            raise ValueError("Forecast date is only supported in forecast mode")
        try:
            body = await self._run(
                kind,
                current_time,
                forecast_date=forecast_date,
                force_publish=force_publish,
                silent=silent,
            )
        except Exception as exc:
            exc.add_note("Briefing run failed")
            try:
                self._state.record_failure()
            except Exception as record_error:
                exc.add_note("Failure state could not be recorded")
                _LOGGER.error("Failed to record briefing failure: %s", type(record_error).__name__)
            else:
                try:
                    if self._state.task_failure_requires_alert():
                        if (
                            isinstance(exc, DeliveryError)
                            and exc.channel_unavailable
                            and self._ops_delivery is self._delivery
                        ):
                            _LOGGER.info(
                                "Failure alert skipped reason=delivery-channel-unavailable original_reason=%s",
                                exc.reason,
                            )
                        else:
                            await self._ops_delivery.publish_alert(
                                "Weather briefing task failed",
                                "The task failed. Check the application logs, weather APIs, and private source "
                                "configuration.",
                            )
                            self._state.mark_task_failure_alerted(current_time)
                except Exception:
                    _LOGGER.exception("Failed to publish or record briefing failure alert")
            raise
        self._state.record_success(
            current_time,
            history_hours=self._settings.history_hours,
            warning_retention_hours=self._settings.warning_retention_hours,
        )
        return body

    async def _run(
        self,
        kind: str,
        now: pendulum.DateTime,
        *,
        forecast_date: pendulum.Date | None,
        force_publish: bool,
        silent: bool,
    ) -> str | None:
        await self._deliver_pending_verbatim()
        feeds = tuple(
            feed for feed in self._settings.feeds if not feed.location_ids or self._location.id in feed.location_ids
        )
        all_articles = await collect_rss_articles(feeds, self._rss_source, self._state, now)
        rss_failure_alert_ids = self._state.rss_sources_requiring_failure_alert(
            tuple(config.id for config in feeds),
            self._settings.rss_failure_threshold,
        )
        if rss_failure_alert_ids:
            await self._publish_rss_health_alert(
                "Weather RSS sources repeatedly failed",
                f"The following RSS sources failed for at least {self._settings.rss_failure_threshold} "
                f"consecutive scheduled runs: {', '.join(rss_failure_alert_ids)}",
                lambda: self._state.mark_rss_failure_alerted(tuple(rss_failure_alert_ids), now),
            )
        stale = self._state.stale_sources_requiring_alert(
            tuple(config.id for config in feeds),
            now,
            self._settings.rss_stale_hours,
        )
        if stale:
            _LOGGER.warning("Stale RSS source(s): %s", ", ".join(stale))
            await self._publish_rss_health_alert(
                "Weather RSS sources have not updated",
                f"The following sources have no new articles within the configured "
                f"{self._settings.rss_stale_hours}-hour threshold: {', '.join(stale)}",
                lambda: self._state.mark_stale_sources_alerted(tuple(stale), now),
            )
        local_now = now.in_timezone(self._settings.timezone)
        today_start = local_now.start_of("day")
        tomorrow_start = today_start.add(days=1)
        yesterday_start = today_start.subtract(days=1)
        todays_articles = tuple(
            article for article in all_articles if today_start <= article.published_at < tomorrow_start
        )
        bootstrap_candidates = tuple(
            article
            for article in all_articles
            if kind == "forecast"
            and self._is_forecast_article(article)
            and yesterday_start <= article.published_at < today_start
        )
        deferred_articles = self._state.pending_articles()
        deferred_ids = {article.id for article in deferred_articles}
        known = self._state.known_article_ids(
            tuple(article.id for article in (*todays_articles, *bootstrap_candidates))
        )
        new_articles = tuple(
            article for article in todays_articles if article.id not in known and article.id not in deferred_ids
        )
        bootstrap_articles = tuple(
            article for article in bootstrap_candidates if article.id not in known and article.id not in deferred_ids
        )
        unpublished_articles = _unique_articles((*deferred_articles, *new_articles, *bootstrap_articles))
        context = await collect_weather_documents(self._weather_context_provider, self._location, forecast_date)
        historical_context = self._state.recent_context_documents(now, self._settings.history_hours)
        bounded_history = _bounded_context_history(
            historical_context,
            max_documents=self._settings.llm_history_max_documents,
            max_characters=self._settings.llm_history_max_characters,
        )
        _LOGGER.debug(
            "Historical context bounded: input_documents=%d selected_documents=%d serialized_characters=%d",
            len(historical_context),
            len(bounded_history.payload),
            bounded_history.serialized_characters,
        )
        await self._publish_context_budget_alert(bounded_history.overflows, now)
        reference_context = _unique_documents((*bounded_history.documents, *context))
        active_warnings = self._state.active_warnings(now, self._settings.warning_retention_hours)
        if not unpublished_articles and not context and not active_warnings:
            _LOGGER.info("Skipping briefing: no new articles, context, or warnings")
            return None
        historical_articles = _unique_articles(
            (*self._state.recent_articles(now, self._settings.history_hours), *bootstrap_articles)
        )
        source_articles = _unique_articles((*historical_articles, *unpublished_articles))
        _LOGGER.debug(
            "%d new article(s), %d deferred article(s), %d historical article(s), "
            "%d active warning(s), %d context document(s)",
            len(new_articles),
            len(deferred_articles),
            len(historical_articles),
            len(active_warnings),
            len(context),
        )
        payload = build_briefing_payload(
            kind=kind,
            now=now,
            forecast_date=forecast_date,
            location=self._location,
            timezone=self._settings.timezone,
            state=self._state,
            history_hours=self._settings.history_hours,
            articles=new_articles,
            deferred_articles=deferred_articles,
            historical_articles=historical_articles,
            context=context,
            historical_context=bounded_history.payload,
            active_warnings=active_warnings,
        )
        active_warning_ids = {warning.id for warning in active_warnings}
        payload["allowed_resolved_warning_ids"] = sorted(active_warning_ids)
        briefing_limit = self._delivery.briefing_limit(self._settings.briefing_max_characters)
        payload["output_constraints"] = {"briefing_max_characters": briefing_limit}
        required_advice_topics = _required_advice_topics(kind, context)
        payload["required_advice_topics"] = [topic.value for topic in required_advice_topics]
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "LLM payload prepared: serialized_characters=%d",
                len(serialize_llm_payload(payload)),
            )
        allergen_source_ids = {document.id for document in context if document.has_allergen_information}
        valid_source_ids = {article.id for article in source_articles} | {document.id for document in reference_context}

        def validate_result(candidate: BriefingResult) -> None:
            candidate_message = self._delivery.render_briefing(candidate, source_articles, reference_context)
            if kind == "briefing" and candidate.advice:
                raise LLMError("briefing must not repeat lifestyle advice")
            if kind == "forecast" and not candidate.should_publish:
                raise LLMError("forecast must set should_publish=true")
            missing_advice_topics = set(required_advice_topics) - {item.topic for item in candidate.advice}
            if missing_advice_topics:
                missing = ", ".join(sorted(topic.value for topic in missing_advice_topics))
                raise LLMError(f"forecast advice is missing required topics: {missing}")
            if any(
                item.topic is AdviceTopic.ALLERGEN and allergen_source_ids.isdisjoint(item.source_ids)
                for item in candidate.advice
            ):
                raise LLMError("allergen advice must cite a current allergen-capable source")
            if candidate_message.visible_length > briefing_limit:
                raise LLMError(
                    f"briefing has {candidate_message.visible_length} visible characters; limit is {briefing_limit}"
                )

        result = await summarize_validated(
            self._llm,
            payload,
            now,
            valid_source_ids,
            max_attempts=self._settings.llm_max_attempts,
            output_language=self._location.summary_language,
            validator=validate_result,
        )
        unknown_resolved_warning_ids = set(result.resolved_warning_ids) - active_warning_ids
        if unknown_resolved_warning_ids:
            _LOGGER.warning(
                "Ignoring %d distinct resolved warning ID(s) that are not currently active",
                len(unknown_resolved_warning_ids),
            )
            result = replace(
                result,
                resolved_warning_ids=tuple(
                    warning_id for warning_id in result.resolved_warning_ids if warning_id in active_warning_ids
                ),
            )
        message = self._delivery.render_briefing(
            result,
            source_articles,
            reference_context,
        )
        if kind == "briefing" and not result.should_publish and not force_publish:
            _LOGGER.info("Briefing skipped: should_publish=False")
            self._save_result_state(
                kind,
                now,
                unpublished_articles,
                context,
                result,
                body=None,
                verbatim_silent=False,
            )
            return None

        publish_silently = silent and kind == "briefing" and not result.should_publish
        await self._delivery.publish_rendered(message, single_message=True, silent=publish_silently)
        self._save_result_state(
            kind,
            now,
            unpublished_articles,
            context,
            result,
            body=message.body,
            verbatim_silent=publish_silently,
        )
        await self._deliver_pending_verbatim()
        return message.body

    async def _deliver_pending_verbatim(self) -> None:
        for delivery in self._state.pending_verbatim_deliveries():
            article = delivery.article
            _LOGGER.debug(
                "Publishing verbatim article: source=%s published_at=%s content_characters=%d",
                article.source_id,
                article.published_at.isoformat(),
                len(article.content),
            )
            await self._delivery.publish_verbatim(article, silent=delivery.silent)
            self._state.acknowledge_verbatim_delivery(article.id)
            _LOGGER.info(
                "Verbatim article published: source=%s published_at=%s",
                article.source_id,
                article.published_at.isoformat(),
            )

    async def _publish_rss_health_alert(
        self,
        title: str,
        body: str,
        mark_alerted: Callable[[], None],
    ) -> None:
        try:
            await self._ops_delivery.publish_alert(title, body)
            mark_alerted()
        except Exception:
            _LOGGER.exception("Failed to publish or record RSS health alert")

    async def _publish_context_budget_alert(
        self,
        overflows: tuple[_HistoricalContextOverflow, ...],
        now: pendulum.DateTime,
    ) -> None:
        fingerprints = _context_budget_fingerprints(overflows)
        try:
            source_ids = self._state.context_budget_sources_requiring_alert(fingerprints)
            if not source_ids:
                return
            await self._ops_delivery.publish_alert(
                "Weather history exceeds the LLM input budget",
                "The latest value or window baseline for the following sources still cannot fit after "
                "deterministic compaction: " + ", ".join(source_ids),
            )
            self._state.mark_context_budget_alerted(
                {source_id: fingerprints[source_id] for source_id in source_ids},
                now,
            )
        except Exception:
            _LOGGER.exception("Failed to publish or record context budget alert")

    def _save_result_state(
        self,
        kind: str,
        now: pendulum.DateTime,
        unpublished_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
        result: BriefingResult,
        *,
        body: str | None,
        verbatim_silent: bool,
    ) -> None:
        self._state.commit_result(
            kind=kind,
            body=body,
            articles=unpublished_articles,
            context_documents=context,
            active_warnings=result.active_warnings,
            resolved_warning_ids=result.resolved_warning_ids,
            recorded_at=now,
            verbatim_silent=verbatim_silent,
        )

    def _is_forecast_article(self, article: Article) -> bool:
        feed = next(
            (
                config
                for config in self._settings.feeds
                if config.id == article.source_id
                and (not config.location_ids or self._location.id in config.location_ids)
            ),
            None,
        )
        if feed is None:
            return False
        return article.is_verbatim or any(pattern in article.title for pattern in feed.forecast_title_patterns)


def _unique_articles(articles: tuple[Article, ...]) -> tuple[Article, ...]:
    return tuple({article.id: article for article in articles}.values())


def _required_advice_topics(
    kind: str,
    context: tuple[SourceDocument, ...],
) -> tuple[AdviceTopic, ...]:
    if kind != "forecast":
        return ()
    topics = [
        AdviceTopic.CLOTHING,
        AdviceTopic.DEHUMIDIFICATION,
        AdviceTopic.EXERCISE,
        AdviceTopic.MASK,
    ]
    if any(document.has_allergen_information for document in context):
        topics.append(AdviceTopic.ALLERGEN)
    return tuple(topics)


def _unique_documents(
    documents: tuple[SourceDocument, ...],
) -> tuple[SourceDocument, ...]:
    return tuple({document.id: document for document in documents}.values())
