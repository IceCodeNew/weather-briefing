"""Information-type-neutral notification decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    """State whether one information change is worth interrupting the user."""

    should_notify: bool


class NotificationDecisionProvider(Protocol):
    """Evaluate notification value independently from content generation."""

    async def assess_notification(self, payload: dict[str, object]) -> NotificationDecision:
        """Return whether the supplied information change merits a notification."""
        ...
