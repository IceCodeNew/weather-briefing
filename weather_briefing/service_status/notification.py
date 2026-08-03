"""Service-status notification assessment input."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.notification_decision import NotificationAssessment
from weather_briefing.notification_decision.policies import SERVICE_STATUS_NOTIFICATION_KIND

if TYPE_CHECKING:
    from weather_briefing.persistence.service_status import ServiceStatusMessageState

    from .models import ServiceStatusMessage, ServiceStatusSnapshot


def service_status_notification_assessment(
    snapshot: ServiceStatusSnapshot,
    message: ServiceStatusMessage,
    previous: ServiceStatusMessageState | None,
) -> NotificationAssessment:
    """Build one type-specific assessment from official status messages."""
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
        if previous.handled_surfaces is not None:
            previous_message["surfaces"] = [surface.value for surface in previous.handled_surfaces]
    return NotificationAssessment(
        kind=SERVICE_STATUS_NOTIFICATION_KIND,
        payload={
            "source": snapshot.source_name,
            "previous": previous_message,
            "current": current,
        },
    )
