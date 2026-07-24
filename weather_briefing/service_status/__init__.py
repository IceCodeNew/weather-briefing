"""Official service-status contracts, adapters, and composition."""

from .collection import collect_service_status
from .composition import service_status_providers
from .flashcat import FlashcatStatusProvider
from .formatting import render_service_status_notification
from .models import (
    ServiceComponentStatus,
    ServiceIncident,
    ServiceStatusSnapshot,
    ServiceSurface,
)
from .monitor import (
    ServiceStatusMonitor,
    has_english_explanation,
    service_status_fingerprint,
    service_status_is_unhealthy,
)
from .providers import (
    AnthropicStatusProvider,
    DeepSeekStatusProvider,
    KimiStatusProvider,
    OpenAIStatusProvider,
)
from .statuspage import ServiceStatusError, ServiceStatusProvider, StatuspageProvider

__all__ = [
    "AnthropicStatusProvider",
    "DeepSeekStatusProvider",
    "FlashcatStatusProvider",
    "KimiStatusProvider",
    "OpenAIStatusProvider",
    "ServiceComponentStatus",
    "ServiceIncident",
    "ServiceStatusError",
    "ServiceStatusProvider",
    "ServiceStatusSnapshot",
    "ServiceSurface",
    "StatuspageProvider",
    "ServiceStatusMonitor",
    "collect_service_status",
    "has_english_explanation",
    "render_service_status_notification",
    "service_status_fingerprint",
    "service_status_is_unhealthy",
    "service_status_providers",
]
