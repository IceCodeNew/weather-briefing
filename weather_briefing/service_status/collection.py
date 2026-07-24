"""Best-effort collection of independent official service-status sources."""

from __future__ import annotations

import asyncio
import logging

from .models import ServiceStatusSnapshot
from .statuspage import ServiceStatusProvider

_LOGGER = logging.getLogger("weather_briefing.service_status")


async def collect_service_status(
    providers: tuple[ServiceStatusProvider, ...],
) -> tuple[ServiceStatusSnapshot, ...]:
    """Fetch providers concurrently without letting one optional source block others."""
    results = await asyncio.gather(*(provider.fetch() for provider in providers), return_exceptions=True)
    snapshots: list[ServiceStatusSnapshot] = []
    pending_cancellation: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                if pending_cancellation is None:
                    pending_cancellation = result
                continue
            _LOGGER.warning("Service-status provider failed: %s", result)
            continue
        snapshots.append(result)
    if pending_cancellation is not None:
        raise pending_cancellation
    _LOGGER.info("Fetched %d/%d service-status source(s)", len(snapshots), len(providers))
    return tuple(snapshots)
