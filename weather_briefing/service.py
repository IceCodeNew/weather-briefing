from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pendulum

from .config import Settings
from .llm import LLMError, LLMProvider, parse_result
from .models import Article, BriefingResult, ResolvedLocation, SourceDocument, Warning
from .prompts import SYSTEM_PROMPT
from .publishers import DeliveryProvider
from .sources import HTTPContextSource, RSSSource
from .state import SQLiteStateStore
from .time_utils import require_aware_datetime
from .weather_context import WeatherContextProvider, snapshot_to_documents

_LOGGER = logging.getLogger("weather_briefing.service")


class BriefingService:
    def __init__(
        self,
        settings: Settings,
        location: ResolvedLocation,
        state: SQLiteStateStore,
        rss_source: RSSSource,
        context_source: HTTPContextSource,
        llm: LLMProvider,
        delivery: DeliveryProvider,
        ops_delivery: DeliveryProvider,
        weather_context_provider: WeatherContextProvider | None = None,
    ) -> None:
        self._settings = settings
        self._location = location
        self._state = state
        self._rss_source = rss_source
        self._context_source = context_source
        self._llm = llm
        self._delivery = delivery
        self._ops_delivery = ops_delivery
        self._weather_context_provider = weather_context_provider

    async def run(self, kind: str, now: pendulum.DateTime | None = None) -> str | None:
        current_time = require_aware_datetime(now or pendulum.now(self._settings.timezone), context="Briefing run time")
        try:
            body = await self._run(kind, current_time)
        except Exception as exc:
            self._state.record_failure()
            exc.add_note("Briefing run failed")
            try:
                if self._state.task_failure_requires_alert():
                    await self._ops_delivery.publish_alert(
                        "天气简报任务执行失败",
                        "任务执行失败，请检查运行日志、天气 API 及私密源配置。",
                    )
                    self._state.mark_task_failure_alerted(current_time)
            except Exception:
                _LOGGER.exception("Failed to publish or record briefing failure alert")
            raise
        self._state.record_success()
        return body

    async def _run(self, kind: str, now: pendulum.DateTime) -> str | None:
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
                "天气 RSS 源持续获取失败",
                f"以下 RSS 源已连续失败 {self._settings.rss_failure_threshold} 次：{', '.join(rss_failure_alert_ids)}",
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
                "天气 RSS 源长时间无更新",
                f"以下源超过 {self._settings.rss_stale_hours} 小时无新文章：{', '.join(stale)}",
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
            if kind == "daily"
            and self._is_forecast_article(article)
            and yesterday_start <= article.published_at < today_start
        )
        known = self._state.known_article_ids(
            tuple(article.id for article in (*todays_articles, *bootstrap_candidates))
        )
        new_articles = tuple(article for article in todays_articles if article.id not in known)
        bootstrap_articles = tuple(article for article in bootstrap_candidates if article.id not in known)
        context_items = list(
            await asyncio.gather(*(self._context_source.fetch(config) for config in self._settings.context_sources))
        )
        if self._weather_context_provider is not None:
            weather_context = await self._weather_context_provider.fetch(
                self._location.latitude, self._location.longitude
            )
            context_items.extend(snapshot_to_documents(weather_context))
        context = tuple(context_items)
        historical_context = self._state.recent_context_documents(now, self._settings.history_hours)
        reference_context = _unique_documents((*historical_context, *context))
        active_warnings = self._state.active_warnings(now, self._settings.warning_retention_hours)
        if not new_articles and not bootstrap_articles and not context and not active_warnings:
            _LOGGER.info("Skipping briefing: no new articles, context, or warnings")
            return None
        historical_articles = _unique_articles(
            (*self._state.recent_articles(now, self._settings.history_hours), *bootstrap_articles)
        )
        source_articles = _unique_articles((*historical_articles, *new_articles))
        _LOGGER.debug(
            "%d new article(s), %d historical article(s), %d active warning(s), %d context document(s)",
            len(new_articles),
            len(historical_articles),
            len(active_warnings),
            len(context),
        )
        payload = self._build_payload(
            kind,
            now,
            new_articles,
            historical_articles,
            context,
            historical_context,
            active_warnings,
        )
        briefing_limit = self._delivery.briefing_limit(self._settings.briefing_max_characters)
        payload["output_constraints"] = {"briefing_max_characters": briefing_limit}
        valid_source_ids = {article.id for article in source_articles} | {document.id for document in reference_context}

        def validate_length(candidate: BriefingResult) -> None:
            candidate_message = self._delivery.render_briefing(candidate, source_articles, reference_context)
            if kind == "hourly" and candidate.advice:
                raise LLMError("hourly briefing must not repeat lifestyle advice")
            if kind == "daily" and not candidate.should_publish:
                raise LLMError("daily briefing must set should_publish=true")
            if candidate_message.visible_length > briefing_limit:
                raise LLMError(
                    f"briefing has {candidate_message.visible_length} visible characters; limit is {briefing_limit}"
                )

        result = await self._summarize(payload, now, valid_source_ids, validator=validate_length)
        message = self._delivery.render_briefing(
            result,
            source_articles,
            reference_context,
        )
        if kind == "hourly" and not result.should_publish:
            _LOGGER.info("Hourly briefing skipped: should_publish=False")
            self._save_result_state(
                kind,
                now,
                new_articles,
                bootstrap_articles,
                context,
                result,
                body=None,
            )
            return None

        await self._delivery.publish_rendered(message, single_message=True)
        for article in new_articles:
            if article.is_verbatim:
                _LOGGER.debug(
                    "Publishing verbatim article: source=%s published_at=%s content_characters=%d",
                    article.source_id,
                    article.published_at.isoformat(),
                    len(article.content),
                )
                await self._delivery.publish_verbatim(article)
                _LOGGER.info(
                    "Verbatim article published: source=%s published_at=%s",
                    article.source_id,
                    article.published_at.isoformat(),
                )
        self._save_result_state(
            kind,
            now,
            new_articles,
            bootstrap_articles,
            context,
            result,
            body=message.body,
        )
        return message.body

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

    def _save_result_state(
        self,
        kind: str,
        now: pendulum.DateTime,
        new_articles: tuple[Article, ...],
        bootstrap_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
        result: BriefingResult,
        *,
        body: str | None,
    ) -> None:
        self._state.save_articles(_unique_articles((*new_articles, *bootstrap_articles)), now)
        self._state.save_context_documents(context, now)
        if body is not None:
            self._state.save_briefing(kind, body, now)
        current_source_ids = {article.id for article in new_articles} | {document.id for document in context}
        self._state.update_warnings(
            result.active_warnings,
            result.resolved_warning_ids,
            now,
            current_source_ids,
        )

    async def _summarize(
        self,
        payload: dict[str, object],
        now: pendulum.DateTime,
        valid_source_ids: set[str],
        validator: Callable[[BriefingResult], None] | None = None,
    ) -> BriefingResult:
        instructions = SYSTEM_PROMPT
        current_payload: dict[str, object] = payload
        last_error: LLMError | None = None
        for attempt in range(self._settings.llm_max_attempts):
            raw_result: dict[str, object] | None = None
            try:
                _LOGGER.debug("LLM summarization attempt %d/%d", attempt + 1, self._settings.llm_max_attempts)
                raw_result = await self._llm.summarize(instructions, current_payload)
                result = parse_result(raw_result, now, valid_source_ids)
                if validator is not None:
                    validator(result)
                _LOGGER.debug(
                    "LLM summarization successful on attempt %d/%d", attempt + 1, self._settings.llm_max_attempts
                )
                return result
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
        articles: tuple[Article, ...],
        historical_articles: tuple[Article, ...],
        context: tuple[SourceDocument, ...],
        historical_context: tuple[SourceDocument, ...],
        active_warnings: tuple[Warning, ...],
    ) -> dict[str, object]:
        return {
            "mode": kind,
            "now": now.isoformat(),
            "region": self._location.name,
            "location_id": self._location.id,
            "coordinates": {
                "latitude": self._location.latitude,
                "longitude": self._location.longitude,
            },
            "new_articles": [
                {
                    "source_id": article.id,
                    "publisher": article.source_name,
                    "title": article.title,
                    "url": article.url,
                    "published_at": article.published_at.isoformat(),
                    "content": article.content,
                    "verbatim": article.is_verbatim,
                }
                for article in articles
            ],
            "historical_articles": [
                {
                    "source_id": article.id,
                    "publisher": article.source_name,
                    "title": article.title,
                    "url": article.url,
                    "published_at": article.published_at.isoformat(),
                    "content": article.content,
                    "verbatim": article.is_verbatim,
                }
                for article in historical_articles
                if kind == "daily" or article.is_verbatim
            ],
            "context_documents": [
                {"source_id": item.id, "name": item.name, "url": item.url, "content": item.content} for item in context
            ],
            "recent_context_documents": [
                {
                    "source_id": item.id,
                    "name": item.name,
                    "url": item.url,
                    "content": item.content,
                }
                for item in historical_context
            ],
            "recent_briefings": self._state.recent_briefings(now, self._settings.history_hours),
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


def _unique_documents(
    documents: tuple[SourceDocument, ...],
) -> tuple[SourceDocument, ...]:
    return tuple({document.id: document for document in documents}.values())
