"""Construction of the provider-neutral briefing input payload."""

from __future__ import annotations

import pendulum

from ..models import Article, ResolvedLocation, SourceDocument, Warning
from ..state import SQLiteStateStore


def serialize_article(article: Article) -> dict[str, object]:
    """Serialize one article for the structured LLM input."""
    return {
        "source_id": article.id,
        "publisher": article.source_name,
        "title": article.title,
        "url": article.url,
        "published_at": article.published_at.isoformat(),
        "content": article.content,
        "verbatim": article.is_verbatim,
    }


def build_briefing_payload(
    *,
    kind: str,
    now: pendulum.DateTime,
    forecast_date: pendulum.Date | None,
    location: ResolvedLocation,
    timezone: pendulum.Timezone,
    state: SQLiteStateStore,
    history_hours: int,
    articles: tuple[Article, ...],
    deferred_articles: tuple[Article, ...],
    historical_articles: tuple[Article, ...],
    context: tuple[SourceDocument, ...],
    historical_context: list[dict[str, object]],
    active_warnings: tuple[Warning, ...],
) -> dict[str, object]:
    """Build the complete structured input without invoking external services."""
    location_scope = {"full_name": location.name}
    if location.administrative_area:
        location_scope["administrative_area"] = location.administrative_area
    if location.country_code:
        location_scope["country_code"] = location.country_code
    return {
        "mode": kind,
        "output_language": location.summary_language,
        "now": now.isoformat(),
        "forecast_date": str(forecast_date or now.in_timezone(timezone).date()),
        "region": location.name,
        "location_scope": location_scope,
        "new_articles": [serialize_article(article) for article in articles],
        "deferred_articles": [serialize_article(article) for article in deferred_articles],
        "historical_articles": [
            serialize_article(article) for article in historical_articles if kind == "forecast" or article.is_verbatim
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
                "published_at": briefing.published_at.in_timezone(timezone).isoformat(),
                "body": briefing.body,
            }
            for briefing in state.recent_briefings(now, history_hours)
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
