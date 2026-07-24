"""Stateful notification orchestration for official service-status messages."""

from __future__ import annotations

import logging
import re
from typing import Protocol

import pendulum

from ..llm import LLMError
from ..notifications import NotificationDecisionProvider
from ..state import ServiceStatusMessageState
from .collection import collect_service_status
from .models import ServiceStatusMessage, ServiceStatusSnapshot
from .statuspage import ServiceStatusProvider

_LOGGER = logging.getLogger("weather_briefing.service_status")
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")


class ServiceStatusStateStore(Protocol):
    """Persist observed and successfully handled official messages."""

    def service_status_message_state(
        self,
        source_id: str,
        incident_id: str,
    ) -> ServiceStatusMessageState | None:
        """Return the durable state for one incident."""
        ...

    def observe_service_status_message(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        title: str,
        status: str,
        body: str,
        observed_at: pendulum.DateTime,
    ) -> None:
        """Record an official message before evaluating or delivering it."""
        ...

    def mark_service_status_message_handled(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        title: str,
        status: str,
        body: str,
        handled_at: pendulum.DateTime,
    ) -> None:
        """Record successful delivery or an intentional skip."""
        ...

    def mark_service_status_message_decided(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        should_notify: bool,
    ) -> None:
        """Persist one notification-value decision."""
        ...

    def service_status_delivered_publishers(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
    ) -> frozenset[str]:
        """Return publisher IDs already sent this revision."""
        ...

    def mark_service_status_message_delivered(
        self,
        source_id: str,
        incident_id: str,
        revision_id: str,
        publisher_id: str,
        delivered_at: pendulum.DateTime,
    ) -> None:
        """Record one successful publisher delivery."""
        ...


class ServiceStatusTranslator(Protocol):
    """Translate an official message only when its language requires it."""

    async def translate_service_status(
        self,
        title: str,
        body: str,
        target_language: str,
    ) -> tuple[str, str]:
        """Return a faithful translated title and body."""
        ...


class ServiceStatusDelivery(Protocol):
    """Publish one official status message."""

    async def publish_alert(self, title: str, body: str) -> None:
        """Deliver a status notification."""
        ...


class ServiceStatusMonitor:
    """Evaluate and publish meaningful revisions of official incident messages."""

    def __init__(
        self,
        providers: tuple[ServiceStatusProvider, ...],
        state: ServiceStatusStateStore,
        deliveries: tuple[tuple[str, ServiceStatusDelivery], ...],
        decision_provider: NotificationDecisionProvider,
        translator: ServiceStatusTranslator | None = None,
        language: str = "en",
    ) -> None:
        """Configure providers, durable state, notification policy, and delivery."""
        self._providers = providers
        self._state = state
        self._deliveries = deliveries
        self._decision_provider = decision_provider
        self._translator = translator
        self._language = language

    async def run(self, now: pendulum.DateTime) -> int:
        """Publish meaningful changed messages, returning the delivery-event count."""
        snapshots = await collect_service_status(self._providers)
        published = 0
        for snapshot in snapshots:
            for message in snapshot.messages:
                if await self._process_message(snapshot, message, now):
                    published += 1
        return published

    async def _process_message(
        self,
        snapshot: ServiceStatusSnapshot,
        message: ServiceStatusMessage,
        now: pendulum.DateTime,
    ) -> bool:
        previous = self._state.service_status_message_state(snapshot.source_id, message.incident_id)
        if previous is not None and previous.handled_revision_id == message.revision_id:
            return False
        self._state.observe_service_status_message(
            snapshot.source_id,
            message.incident_id,
            message.revision_id,
            message.title,
            message.status,
            message.body,
            message.published_at,
        )
        if previous is None and message.status == "resolved":
            self._mark_handled(snapshot, message, now)
            return False
        if (
            previous is not None
            and previous.decided_revision_id == message.revision_id
            and previous.should_notify is not None
        ):
            should_notify = previous.should_notify
        else:
            decision = await self._decision_provider.assess_notification(
                _notification_payload(snapshot, message, previous)
            )
            should_notify = decision.should_notify
            self._state.mark_service_status_message_decided(
                snapshot.source_id,
                message.incident_id,
                message.revision_id,
                should_notify,
            )
        if not should_notify:
            self._mark_handled(snapshot, message, now)
            return False
        title, body = await self._localized_message(message)
        rendered_body = f"{body}\n\n{message.url}"
        delivered = self._state.service_status_delivered_publishers(
            snapshot.source_id,
            message.incident_id,
            message.revision_id,
        )
        for publisher_id, delivery in self._deliveries:
            if publisher_id in delivered:
                continue
            await delivery.publish_alert(title, rendered_body)
            self._state.mark_service_status_message_delivered(
                snapshot.source_id,
                message.incident_id,
                message.revision_id,
                publisher_id,
                now,
            )
        self._mark_handled(snapshot, message, now)
        return True

    def _mark_handled(
        self,
        snapshot: ServiceStatusSnapshot,
        message: ServiceStatusMessage,
        now: pendulum.DateTime,
    ) -> None:
        self._state.mark_service_status_message_handled(
            snapshot.source_id,
            message.incident_id,
            message.revision_id,
            message.title,
            message.status,
            message.body,
            now,
        )

    async def _localized_message(self, message: ServiceStatusMessage) -> tuple[str, str]:
        if self._translator is None or official_message_matches(message, self._language):
            return message.title, message.body
        try:
            return await self._translator.translate_service_status(
                message.title,
                message.body,
                self._language,
            )
        except LLMError as exc:
            _LOGGER.warning(
                "Service-status translation failed; using the official original text error_type=%s",
                type(exc).__name__,
            )
            return message.title, message.body


def _notification_payload(
    snapshot: ServiceStatusSnapshot,
    message: ServiceStatusMessage,
    previous: ServiceStatusMessageState | None,
) -> dict[str, object]:
    current = {
        "title": message.title,
        "status": message.status,
        "body": message.body,
        "surfaces": [surface.value for surface in message.surfaces],
        "published_at": message.published_at.to_iso8601_string(),
    }
    previous_message: dict[str, object] | None = None
    if (
        previous is not None
        and previous.handled_title is not None
        and previous.handled_status is not None
        and previous.handled_body is not None
    ):
        previous_message = {
            "title": previous.handled_title,
            "status": previous.handled_status,
            "body": previous.handled_body,
        }
    return {
        "notification_kind": "service_status",
        "source": snapshot.source_name,
        "previous": previous_message,
        "current": current,
    }


def official_message_matches(message: ServiceStatusMessage, target_language: str) -> bool:
    """Forward English or target-language official text without translation."""
    text = f"{message.title}\n{message.body}"
    if _looks_english(message.body) or _looks_english(text):
        return True
    if target_language == "zh-CN":
        return bool(_HAN.search(text)) and not _KANA.search(text)
    if target_language == "ja":
        return bool(_KANA.search(text))
    return target_language == "en" and _looks_english(text)


def _looks_english(text: str) -> bool:
    ascii_letters = len(_ASCII_LETTER.findall(text))
    language_markers = len(_HAN.findall(text)) + len(_KANA.findall(text))
    return ascii_letters >= 20 and (language_markers == 0 or ascii_letters >= language_markers * 3)
