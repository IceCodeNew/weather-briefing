"""Conversion of service-status snapshots into citable source documents."""

from __future__ import annotations

from ..models import SourceDocument
from .models import ServiceStatusSnapshot, ServiceSurface

_SURFACE_LABELS = {
    ServiceSurface.WEB: "Web services",
    ServiceSurface.API: "API services",
    ServiceSurface.OTHER: "Other services",
}


def service_status_to_document(snapshot: ServiceStatusSnapshot) -> SourceDocument:
    """Convert one service-status snapshot into citable LLM context."""
    sections: list[str] = [f"Updated at: {snapshot.observed_at.to_iso8601_string()}"]
    history_sections: list[str] = []
    for surface in ServiceSurface:
        components = tuple(component for component in snapshot.components if component.surface is surface)
        if not components:
            continue
        lines = [f"- {component.name}: {component.status}" for component in components]
        section = f"{_SURFACE_LABELS[surface]}:\n" + "\n".join(lines)
        sections.append(section)
        history_sections.append(section)
    if snapshot.incidents:
        incident_lines = [
            (
                f"- {incident.name}: status={incident.status}; impact={incident.impact}; "
                f"updated_at={incident.updated_at.to_iso8601_string()}; detail={incident.detail}"
            )
            for incident in snapshot.incidents
        ]
        incident_section = "Active incidents:\n" + "\n".join(incident_lines)
        sections.append(incident_section)
        history_sections.append(incident_section)
    else:
        sections.append("Active incidents: none")
        history_sections.append("Active incidents: none")
    non_operational = tuple(component for component in snapshot.components if component.status != "operational")
    return SourceDocument(
        id=snapshot.source_id,
        name=snapshot.source_name,
        url=snapshot.source_url,
        content="\n".join(sections),
        language="en",
        history_summary=(
            f"Updated at: {snapshot.observed_at.to_iso8601_string()}\n"
            f"Non-operational components: {len(non_operational)}\n"
            f"Active incidents: {len(snapshot.incidents)}"
        ),
        history_value="\n".join(history_sections),
    )
