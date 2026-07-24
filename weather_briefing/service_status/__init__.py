"""Official service-status contracts, adapters, and composition."""

from .collection import collect_service_status
from .composition import service_status_providers
from .feed import StatusFeedProvider
from .models import ServiceStatusMessage, ServiceStatusSnapshot, ServiceSurface
from .monitor import ServiceStatusMonitor, official_message_matches
from .providers import (
    AnthropicStatusProvider,
    DeepSeekStatusProvider,
    KimiStatusProvider,
    OpenAIStatusProvider,
)
from .statuspage import ServiceStatusError, ServiceStatusProvider

__all__ = [
    "AnthropicStatusProvider",
    "DeepSeekStatusProvider",
    "KimiStatusProvider",
    "OpenAIStatusProvider",
    "ServiceStatusError",
    "ServiceStatusMessage",
    "ServiceStatusProvider",
    "ServiceStatusSnapshot",
    "ServiceSurface",
    "StatusFeedProvider",
    "ServiceStatusMonitor",
    "collect_service_status",
    "official_message_matches",
    "service_status_providers",
]
