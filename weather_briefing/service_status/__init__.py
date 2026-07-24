"""Official service-status contracts, adapters, and composition."""

from .composition import service_status_providers
from .documents import service_status_to_document
from .flashcat import FlashcatStatusProvider
from .models import (
    ServiceComponentStatus,
    ServiceIncident,
    ServiceStatusSnapshot,
    ServiceSurface,
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
    "service_status_providers",
    "service_status_to_document",
]
