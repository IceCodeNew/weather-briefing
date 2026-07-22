"""Core briefing orchestration and output contract enforcement."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import pendulum

from .capabilities import CapabilityProviderSet
from .llm import LLMError, LLMProvider, LLMRequestError, parse_result, serialize_llm_payload
from .models import (
    AdviceTopic,
    Article,
    BriefingResult,
    ContextSourceConfig,
    FeedConfig,
    ResolvedLocation,
    SourceDocument,
    Warning,
)
from .prompts import SYSTEM_PROMPT
from .publishers import DeliveryError, DeliveryProvider
from .sources import ContextDocumentSource, RSSFeedSource
from .state import SQLiteStateStore
from .time_utils import require_aware_datetime
from .weather_context import WeatherContextProvider, fetch_weather_context, snapshot_to_documents

_LOGGER = logging.getLogger("weather_briefing.service")
_FINGERPRINT_CHUNK_CHARACTERS = 64 * 1024
_FINGERPRINT_SINGLE_PASS_CHARACTERS = 4 * _FINGERPRINT_CHUNK_CHARACTERS


@dataclass(frozen=True, slots=True)
class _HistoricalContextCandidate:
    document: SourceDocument
    role: Literal["latest", "retention_baseline", "recent_change"]


@dataclass(frozen=True, slots=True)
class _HistoricalContextOverflow:
    source_id: str
    role: Literal["latest", "retention_baseline"]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _BoundedContextHistory:
    payload: list[dict[str, object]]
    documents: tuple[SourceDocument, ...]
    serialized_characters: int
    overflows: tuple[_HistoricalContextOverflow, ...]


@dataclass(slots=True)
class _ContextSourceChanges:
    baseline: tuple[int, SourceDocument]
    recent: deque[tuple[int, SourceDocument]]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object:
        """Add bytes to the digest state."""
        ...


def _utf8_length(value: str) -> int:
    return sum(
        len(value[start : start + _FINGERPRINT_CHUNK_CHARACTERS].encode())
        for start in range(0, len(value), _FINGERPRINT_CHUNK_CHARACTERS)
    )


def _update_framed_text_digest(digest: _Digest, value: str) -> None:
    """Hash length-prefixed UTF-8 without allocating bytes for the full value."""
    if len(value) <= _FINGERPRINT_SINGLE_PASS_CHARACTERS:
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
        return
    digest.update(_utf8_length(value).to_bytes(8, byteorder="big"))
    for start in range(0, len(value), _FINGERPRINT_CHUNK_CHARACTERS):
        digest.update(value[start : start + _FINGERPRINT_CHUNK_CHARACTERS].encode())


def _context_overflow_fingerprint(candidate: _HistoricalContextCandidate) -> str:
    digest = hashlib.sha256()
    for value in (candidate.role, *_context_document_value(candidate.document)):
        _update_framed_text_digest(digest, value or "")
    return digest.hexdigest()


def _serialize_article(article: Article) -> dict[str, object]:
    return {
        "source_id": article.id,
        "publisher": article.source_name,
        "title": article.title,
        "url": article.url,
        "published_at": article.published_at.isoformat(),
        "content": article.content,
        "verbatim": article.is_verbatim,
    }


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
    def context_sources(self) -> tuple[ContextSourceConfig, ...]:
        """Return configured auxiliary context sources."""
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
        context_source: ContextDocumentSource,
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
        self._context_source = context_source
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
        _LOGGER.debug("Fetching %d RSS feed(s)", len(feeds))
        results = await asyncio.gather(
            *(self._rss_source.fetch(config) for config in feeds),
            return_exceptions=True,
        )
        fetched: list[tuple[Article, ...]] = []
        pending_cancellation: BaseException | None = None
        for result, config in zip(results, feeds, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    if pending_cancellation is None:
                        pending_cancellation = result
                    continue
                _LOGGER.warning("RSS source %s failed: %s", config.id, result)
                fetched.append(())
                self._state.record_source_check(config.id, now, None)
                self._state.record_rss_fetch_failure(config.id)
            else:
                fetched.append(result)
                latest_at = max((article.published_at for article in result), default=None)
                self._state.record_source_check(config.id, now, latest_at)
                self._state.record_rss_fetch_success(config.id)
        if pending_cancellation is not None:
            raise pending_cancellation
        all_articles = tuple(article for group in fetched for article in group)
        _LOGGER.info("Fetched %d article(s) from %d feed(s)", len(all_articles), len(fetched))
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
        context_items = list(
            await asyncio.gather(*(self._context_source.fetch(config) for config in self._settings.context_sources))
        )
        if self._weather_context_provider is not None:
            if isinstance(self._weather_context_provider, CapabilityProviderSet):
                weather_contexts = await self._weather_context_provider.fetch_all(
                    self._location.latitude,
                    self._location.longitude,
                    forecast_date=forecast_date,
                )
            else:
                weather_contexts = (
                    await fetch_weather_context(
                        self._weather_context_provider,
                        self._location.latitude,
                        self._location.longitude,
                        forecast_date,
                    ),
                )
            for weather_context in weather_contexts:
                context_items.extend(snapshot_to_documents(weather_context))
        context = tuple(context_items)
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
        payload = self._build_payload(
            kind,
            now,
            forecast_date,
            new_articles,
            deferred_articles,
            historical_articles,
            context,
            bounded_history.payload,
            active_warnings,
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

        result = await self._summarize(payload, now, valid_source_ids, validator=validate_result)
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

    async def _summarize(
        self,
        payload: dict[str, object],
        now: pendulum.DateTime,
        valid_source_ids: set[str],
        validator: Callable[[BriefingResult], None],
    ) -> BriefingResult:
        instructions = SYSTEM_PROMPT
        current_payload: dict[str, object] = payload
        last_error: LLMError | None = None
        for attempt in range(self._settings.llm_max_attempts):
            raw_result: dict[str, object] | None = None
            try:
                _LOGGER.debug("LLM summarization attempt %d/%d", attempt + 1, self._settings.llm_max_attempts)
                raw_result = await self._llm.summarize(instructions, current_payload)
                result = replace(
                    parse_result(raw_result, now, valid_source_ids),
                    output_language=self._location.summary_language,
                )
                validator(result)
                _LOGGER.debug(
                    "LLM summarization successful on attempt %d/%d", attempt + 1, self._settings.llm_max_attempts
                )
                return result
            except LLMRequestError:
                raise
            except LLMError as exc:
                last_error = exc
                _LOGGER.debug(
                    "LLM validation failure (attempt %d/%d): %s", attempt + 1, self._settings.llm_max_attempts, exc
                )
                if attempt + 1 < self._settings.llm_max_attempts:
                    instructions = f"{SYSTEM_PROMPT}\n上一版 JSON 未通过验证。请只修复契约错误：{exc}"
                    repair_payload: dict[str, object] = {
                        "original_input": payload,
                        "allowed_source_ids": sorted(valid_source_ids),
                        "allowed_resolved_warning_ids": payload["allowed_resolved_warning_ids"],
                    }
                    if raw_result is not None:
                        repair_payload["previous_invalid_response"] = raw_result
                    current_payload = repair_payload
        raise LLMError("LLM output validation failed after configured attempts") from last_error

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

    def _build_payload(
        self,
        kind: str,
        now: pendulum.DateTime,
        forecast_date: pendulum.Date | None,
        articles: tuple[Article, ...],
        deferred_articles: tuple[Article, ...],
        historical_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
        historical_context: list[dict[str, object]],
        active_warnings: tuple[Warning, ...],
    ) -> dict[str, object]:
        location_scope = {"full_name": self._location.name}
        if self._location.administrative_area:
            location_scope["administrative_area"] = self._location.administrative_area
        if self._location.country_code:
            location_scope["country_code"] = self._location.country_code
        return {
            "mode": kind,
            "output_language": self._location.summary_language,
            "now": now.isoformat(),
            "forecast_date": str(forecast_date or now.in_timezone(self._settings.timezone).date()),
            "region": self._location.name,
            "location_scope": location_scope,
            "new_articles": [_serialize_article(article) for article in articles],
            "deferred_articles": [_serialize_article(article) for article in deferred_articles],
            "historical_articles": [
                _serialize_article(article)
                for article in historical_articles
                if kind == "forecast" or article.is_verbatim
            ],
            "context_documents": [
                {
                    "source_id": item.id,
                    "name": item.name,
                    "url": item.url,
                    "language": item.language,
                    "content": item.content,
                }
                for item in context
            ],
            "recent_context_documents": historical_context,
            "recent_briefings": [
                {
                    "mode": briefing.kind,
                    "published_at": briefing.published_at.in_timezone(self._settings.timezone).isoformat(),
                    "body": briefing.body,
                }
                for briefing in self._state.recent_briefings(now, self._settings.history_hours)
            ],
            "currently_active_warnings": [
                {
                    "id": warning.id,
                    "title": warning.title,
                    "status": warning.status,
                    "detail": warning.detail,
                    "source_ids": warning.source_ids,
                    "last_confirmed_at": warning.last_confirmed_at.isoformat(),
                }
                for warning in active_warnings
            ],
        }


def _unique_articles(articles: tuple[Article, ...]) -> tuple[Article, ...]:
    return tuple({article.id: article for article in articles}.values())


def _serialize_context_document(
    document: SourceDocument,
    *,
    history_role: Literal["latest", "retention_baseline", "recent_change"],
    compact: bool = False,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "source_id": document.id,
        "name": document.name,
        "url": document.url,
        "language": document.language,
        "content": document.history_summary if compact else document.content,
        "history_role": history_role,
    }
    if compact:
        entry["content_compacted"] = True
        entry["original_content_characters"] = len(document.content)
    return entry


def _bounded_context_history(
    documents: tuple[SourceDocument, ...],
    *,
    max_documents: int,
    max_characters: int,
) -> _BoundedContextHistory:
    candidates, omitted_overflows = _context_history_selection(documents, max_documents)
    selected: list[tuple[_HistoricalContextCandidate, dict[str, object]]] = []
    overflows = list(omitted_overflows)
    serialized_characters = len("[]")
    for candidate in candidates:
        entry = _serialize_context_document(candidate.document, history_role=candidate.role)
        candidate_payload = [*(selected_entry for _, selected_entry in selected), entry]
        candidate_characters = len(serialize_llm_payload(candidate_payload))
        if candidate_characters <= max_characters:
            selected.append((candidate, entry))
            serialized_characters = candidate_characters
            continue
        if candidate.document.history_summary:
            entry = _serialize_context_document(
                candidate.document,
                history_role=candidate.role,
                compact=True,
            )
            candidate_payload = [*(selected_entry for _, selected_entry in selected), entry]
            candidate_characters = len(serialize_llm_payload(candidate_payload))
            if candidate_characters <= max_characters:
                selected.append((candidate, entry))
                serialized_characters = candidate_characters
                continue
        if candidate.role != "recent_change":
            compactable: list[tuple[int, int, dict[str, object]]] = []
            for index, (selected_candidate, selected_entry) in enumerate(selected):
                if selected_candidate.document.history_summary and not selected_entry.get("content_compacted"):
                    compact_entry = _serialize_context_document(
                        selected_candidate.document,
                        history_role=selected_candidate.role,
                        compact=True,
                    )
                    full_characters = len(serialize_llm_payload(selected_entry))
                    compact_characters = len(serialize_llm_payload(compact_entry))
                    if compact_characters < full_characters:
                        compactable.append((full_characters - compact_characters, index, compact_entry))
            compactable.sort(key=lambda item: item[0], reverse=True)
            for _, index, compact_entry in compactable:
                selected[index] = (selected[index][0], compact_entry)
                selected_payload = [selected_entry for _, selected_entry in selected]
                serialized_characters = len(serialize_llm_payload(selected_payload))
                candidate_payload = [*selected_payload, entry]
                candidate_characters = len(serialize_llm_payload(candidate_payload))
                if candidate_characters <= max_characters:
                    selected.append((candidate, entry))
                    serialized_characters = candidate_characters
                    break
            else:
                fingerprint = _context_overflow_fingerprint(candidate)
                overflows.append(_HistoricalContextOverflow(candidate.document.id, candidate.role, fingerprint))
                continue
            continue
    selected_documents: dict[str, SourceDocument] = {}
    for candidate, _ in selected:
        selected_documents.setdefault(candidate.document.id, candidate.document)
    return _BoundedContextHistory(
        payload=[entry for _, entry in selected],
        documents=tuple(selected_documents.values()),
        serialized_characters=serialized_characters,
        overflows=tuple(overflows),
    )


def _context_budget_fingerprints(overflows: tuple[_HistoricalContextOverflow, ...]) -> dict[str, str]:
    """Combine mandatory overflow fingerprints into one stable value per source."""
    grouped: dict[str, list[str]] = {}
    for overflow in overflows:
        grouped.setdefault(overflow.source_id, []).append(overflow.fingerprint)
    return {
        source_id: hashlib.sha256("\0".join(sorted(fingerprints)).encode()).hexdigest()
        for source_id, fingerprints in grouped.items()
    }


def _context_history_candidates(
    documents: tuple[SourceDocument, ...],
    max_documents: int,
) -> tuple[_HistoricalContextCandidate, ...]:
    candidates, _ = _context_history_selection(documents, max_documents)
    return candidates


def _context_history_selection(
    documents: tuple[SourceDocument, ...],
    max_documents: int,
) -> tuple[tuple[_HistoricalContextCandidate, ...], tuple[_HistoricalContextOverflow, ...]]:
    changes_by_source: dict[str, _ContextSourceChanges] = {}
    for index, document in enumerate(documents):
        source_changes = changes_by_source.get(document.id)
        if source_changes is None:
            snapshot = (index, document)
            changes_by_source[document.id] = _ContextSourceChanges(
                baseline=snapshot,
                recent=deque((snapshot,), maxlen=max_documents),
            )
            continue
        if _context_document_value(source_changes.recent[-1][1]) != _context_document_value(document):
            source_changes.recent.append((index, document))

    source_changes = sorted(changes_by_source.values(), key=lambda changes: changes.recent[-1][0], reverse=True)
    mandatory: list[tuple[int, SourceDocument, Literal["latest", "retention_baseline"]]] = []
    selected_indexes: set[int] = set()

    def add_mandatory(
        snapshot: tuple[int, SourceDocument],
        role: Literal["latest", "retention_baseline"],
    ) -> None:
        if snapshot[0] not in selected_indexes:
            mandatory.append((snapshot[0], snapshot[1], role))
            selected_indexes.add(snapshot[0])

    for changes in source_changes:
        add_mandatory(changes.recent[-1], "latest")
    for changes in source_changes:
        add_mandatory(changes.baseline, "retention_baseline")

    candidates = [
        (index, _HistoricalContextCandidate(document, role)) for index, document, role in mandatory[:max_documents]
    ]
    omitted_mandatory = mandatory[max_documents:]

    depth = 2
    while len(candidates) < max_documents:
        added_change = False
        for changes in source_changes:
            if len(changes.recent) > depth:
                snapshot = changes.recent[-depth]
                candidates.append((snapshot[0], _HistoricalContextCandidate(snapshot[1], "recent_change")))
                selected_indexes.add(snapshot[0])
                if len(candidates) == max_documents:
                    break
                added_change = True
        if not added_change:
            break
        depth += 1

    return (
        tuple(candidate for _, candidate in candidates),
        tuple(
            _HistoricalContextOverflow(
                document.id,
                role,
                _context_overflow_fingerprint(_HistoricalContextCandidate(document, role)),
            )
            for _, document, role in omitted_mandatory
        ),
    )


def _context_document_value(document: SourceDocument) -> tuple[str, str, str, str, str | None]:
    if document.history_value is not None:
        return document.name, document.url, document.language, document.history_value, None
    return document.name, document.url, document.language, document.content, document.history_summary


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
