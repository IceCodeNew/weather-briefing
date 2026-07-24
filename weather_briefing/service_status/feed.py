"""Official RSS/Atom status-message adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import feedparser
import httpx
import pendulum
from bs4 import BeautifulSoup, Tag

from ..api_client import api_call_extensions
from .models import ServiceStatusMessage, ServiceStatusSnapshot, ServiceSurface
from .statuspage import ServiceStatusError

_STATUS_PREFIX = re.compile(r"^status\s*:\s*", re.IGNORECASE)


class StatusFeedProvider:
    """Convert an official status-page feed into revision-aware messages."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        provider_id: str,
        provider_name: str,
        feed_url: str,
        page_url: str,
        classify_component: Callable[[str], ServiceSurface],
    ) -> None:
        """Configure one official feed and conservative surface classifier."""
        self._client = client
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._feed_url = feed_url
        self._page_url = page_url
        self._classify_component = classify_component

    async def fetch(self) -> ServiceStatusSnapshot:
        """Fetch and strictly validate official incident messages."""
        try:
            response = await self._client.get(
                self._feed_url,
                extensions=api_call_extensions(self._provider_id, "status-feed"),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceStatusError(f"{self._provider_name} status feed request failed") from exc
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise ServiceStatusError(f"{self._provider_name} status feed is invalid")
        messages = tuple(self._parse_entry(entry) for entry in parsed.entries)
        if not messages:
            raise ServiceStatusError(f"{self._provider_name} status feed has no incident messages")
        observed_at = max(message.published_at for message in messages)
        return ServiceStatusSnapshot(
            source_id=f"service-status:{self._provider_id}",
            source_name=f"{self._provider_name} Status",
            source_url=self._page_url,
            observed_at=observed_at,
            messages=messages,
        )

    def _parse_entry(self, entry: feedparser.FeedParserDict) -> ServiceStatusMessage:
        incident_id = _required_entry_text(entry, "id", self._provider_name)
        title = _required_entry_text(entry, "title", self._provider_name)
        url = str(entry.get("link") or incident_id).strip()
        published_at = _entry_time(entry, self._provider_name)
        summary = _required_entry_text(entry, "summary", self._provider_name)
        status, body, affected_components = _parse_official_summary(summary, self._provider_name)
        surface_names = affected_components or (title,)
        classified_surfaces = {self._classify_component(name) for name in surface_names}
        surfaces = tuple(surface for surface in ServiceSurface if surface in classified_surfaces)
        revision_payload = "\0".join((incident_id, title, status, body, *(surface.value for surface in surfaces)))
        revision_id = hashlib.sha256(revision_payload.encode()).hexdigest()
        return ServiceStatusMessage(
            incident_id=incident_id,
            revision_id=revision_id,
            title=title,
            status=status,
            body=body,
            url=url,
            published_at=published_at,
            surfaces=surfaces,
        )


def _entry_time(entry: feedparser.FeedParserDict, provider_name: str) -> pendulum.DateTime:
    value = entry.get("published") or entry.get("updated")
    if not isinstance(value, str) or not value.strip():
        raise ServiceStatusError(f"{provider_name} status message has no publication time")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ServiceStatusError(f"{provider_name} status message has an invalid publication time") from exc
    if parsed.tzinfo is None:
        raise ServiceStatusError(f"{provider_name} status message publication time has no UTC offset")
    return pendulum.instance(parsed)


def _parse_official_summary(
    summary: str,
    provider_name: str,
) -> tuple[str, str, tuple[str, ...]]:
    soup = BeautifulSoup(summary, "html.parser")
    status_node = soup.find(["b", "strong"])
    if not isinstance(status_node, Tag):
        raise ServiceStatusError(f"{provider_name} status message has no status")
    status_text = status_node.get_text(" ", strip=True)
    inline_status = _STATUS_PREFIX.sub("", status_text).strip()
    status = inline_status.casefold()
    status_container = status_node.find_parent("p")
    if not status and isinstance(status_container, Tag):
        status = status_container.get_text(" ", strip=True).removeprefix(status_text).strip().casefold()
    if not status:
        raise ServiceStatusError(f"{provider_name} status message has an empty status")
    affected_components = [
        item.get_text(" ", strip=True).rsplit(" (", maxsplit=1)[0]
        for item in soup.find_all("li")
        if item.get_text(" ", strip=True)
    ]
    for paragraph in soup.find_all("p"):
        paragraph_text = paragraph.get_text(" ", strip=True)
        if not paragraph_text.casefold().startswith("affected components:"):
            continue
        _, _, component_text = paragraph_text.partition(":")
        affected_components.extend(
            component.strip().rsplit("(", maxsplit=1)[0].strip()
            for component in component_text.split(",")
            if component.strip()
        )
    if status_text.casefold().startswith("status"):
        body_parts: list[str] = []
        if inline_status:
            for sibling in status_node.next_siblings:
                if isinstance(sibling, Tag) and sibling.name in {"b", "strong"}:
                    break
                text = sibling.get_text(" ", strip=True) if isinstance(sibling, Tag) else str(sibling).strip()
                if text:
                    body_parts.append(text)
        if not body_parts and isinstance(status_container, Tag):
            for sibling in status_container.next_siblings:
                if not isinstance(sibling, Tag):
                    continue
                text = sibling.get_text(" ", strip=True)
                if text.casefold().startswith("affected components:"):
                    break
                if text:
                    body_parts.append(text)
        body = " ".join(body_parts).strip()
    else:
        paragraph = status_node.find_parent("p")
        if not isinstance(paragraph, Tag):
            raise ServiceStatusError(f"{provider_name} status message has no update paragraph")
        paragraph_text = paragraph.get_text(" ", strip=True)
        _, separator, body = paragraph_text.partition(" - ")
        if not separator:
            body = paragraph_text.removeprefix(status_text).strip(" -")
    if not body:
        raise ServiceStatusError(f"{provider_name} status message has an empty body")
    return status, body, tuple(affected_components)


def _required_entry_text(
    entry: feedparser.FeedParserDict,
    field: str,
    provider_name: str,
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ServiceStatusError(f"{provider_name} status message field {field} must be non-empty")
    return value.strip()
