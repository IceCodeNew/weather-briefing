"""Standard-output delivery adapter."""

from __future__ import annotations

from ..models import RenderedMessage


class StdoutPublisher:
    """Write rendered messages to standard output."""

    async def publish(
        self,
        message: RenderedMessage,
        *,
        single_message: bool = False,
        silent: bool = False,
    ) -> None:
        """Print the rendered body and ignore platform delivery hints."""
        print(message.body)
