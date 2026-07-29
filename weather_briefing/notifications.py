"""Information-type-neutral notification decisions."""

from __future__ import annotations

from typing import Protocol

from .notification_decision import NotificationDecision


class NotificationDecisionProvider(Protocol):
    """Evaluate notification value independently from content generation."""

    async def assess_notification(self, payload: dict[str, object]) -> NotificationDecision:
        """Return whether the supplied information change merits a notification."""
        ...
