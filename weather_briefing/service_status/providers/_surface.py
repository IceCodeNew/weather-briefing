"""Shared conservative service-surface classification."""

from ..models import ServiceSurface


def keyword_surface(name: str) -> ServiceSurface:
    """Classify only component names with an explicit service-surface marker."""
    normalized = name.casefold()
    if "api" in normalized:
        return ServiceSurface.API
    if any(marker in normalized for marker in ("web", ".ai", "chat", "console", "portal", "sign in", "search")):
        return ServiceSurface.WEB
    return ServiceSurface.OTHER
