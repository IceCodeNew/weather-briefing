"""Standard-output delivery adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weather_briefing.models import RenderedMessage


class StdoutPublisher:
    """Write rendered messages to standard output."""

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,  # noqa: ARG002
        silent: bool = False,  # noqa: ARG002
    ) -> None:
        """Print the rendered body and ignore platform delivery hints."""
        print(message.body)  # noqa: T201
