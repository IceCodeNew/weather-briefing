"""Provider-neutral service-status domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pendulum


class ServiceSurface(StrEnum):
    """Distinguish user-facing web services from programmatic APIs."""

    WEB = "web"
    API = "api"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ServiceComponentStatus:
    """Represent the current state of one published service component."""

    name: str
    surface: ServiceSurface
    status: str
    updated_at: pendulum.DateTime


@dataclass(frozen=True, slots=True)
class ServiceIncident:
    """Represent one unresolved incident and its latest official update."""

    name: str
    status: str
    impact: str
    updated_at: pendulum.DateTime
    detail: str
    surfaces: tuple[ServiceSurface, ...]


@dataclass(frozen=True, slots=True)
class ServiceStatusSnapshot:
    """Collect one provider's current component and incident status."""

    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime
    components: tuple[ServiceComponentStatus, ...]
    incidents: tuple[ServiceIncident, ...]
