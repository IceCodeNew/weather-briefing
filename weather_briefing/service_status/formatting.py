"""Deterministic direct notifications for official service status."""

from __future__ import annotations

from ..languages import localized_labels
from ..localization import localization_table
from .models import ServiceStatusSnapshot, ServiceSurface

_LABELS = localization_table("service_status")
_SURFACE_LABEL_KEYS = {
    ServiceSurface.WEB: "web_services",
    ServiceSurface.API: "api_services",
    ServiceSurface.OTHER: "other_services",
}


def render_service_status_notification(
    snapshot: ServiceStatusSnapshot,
    *,
    recovered: bool,
    language: str = "en",
) -> tuple[str, str]:
    """Return a direct alert title and body without LLM transformation."""
    labels = localized_labels(language, _LABELS)
    if recovered:
        return (
            labels["recovery_title"].format(source_name=snapshot.source_name),
            "\n".join(
                (
                    labels["recovered"],
                    labels["source"].format(url=snapshot.source_url),
                )
            ),
        )

    sections: list[str] = []
    for surface in ServiceSurface:
        affected = tuple(
            component
            for component in snapshot.components
            if component.surface is surface and component.status != "operational"
        )
        if affected:
            sections.append(
                f"{labels[_SURFACE_LABEL_KEYS[surface]]}:\n"
                + "\n".join(f"- {component.name}: {component.status}" for component in affected)
            )
    if snapshot.incidents:
        sections.append(
            f"{labels['active_incidents']}:\n"
            + "\n".join(
                labels["incident"].format(
                    name=incident.name,
                    status=incident.status,
                    impact=incident.impact,
                    detail=incident.detail,
                )
                for incident in snapshot.incidents
            )
        )
    if not sections:
        sections.append(labels["generic_issue"])
    sections.append(labels["source"].format(url=snapshot.source_url))
    return snapshot.source_name, "\n\n".join(sections)
