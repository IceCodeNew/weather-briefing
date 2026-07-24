"""Application-facing service-status provider contract."""

from __future__ import annotations

from typing import Protocol

from .models import ServiceStatusSnapshot


class ServiceStatusError(RuntimeError):
    """Report an unavailable or invalid official service-status response."""


class ServiceStatusProvider(Protocol):
    """Fetch one provider-neutral set of official incident messages."""

    async def fetch(self) -> ServiceStatusSnapshot:
        """Return current official incident messages."""
        ...
