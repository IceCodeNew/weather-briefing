"""Provider-neutral official service-status message models."""

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
class ServiceStatusMessage:
    """Represent the latest official message for one incident."""

    incident_id: str
    revision_id: str
    title: str
    status: str
    body: str
    url: str
    published_at: pendulum.DateTime
    surfaces: tuple[ServiceSurface, ...]


@dataclass(frozen=True, slots=True)
class ServiceStatusSnapshot:
    """Collect the official incident messages currently exposed by one provider."""

    source_id: str
    source_name: str
    source_url: str
    observed_at: pendulum.DateTime
    messages: tuple[ServiceStatusMessage, ...]
