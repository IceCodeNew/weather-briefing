"""Location configuration loading and resolved field backfill."""

from __future__ import annotations

import json
import math
import os
import stat
from contextlib import suppress
from pathlib import Path

from ..languages import normalize_language_tag
from ..models import LocationSpec, ResolvedLocation, normalize_jma_office_code
from .base import ConfigurationError
from .files import json_file, optional_string_field, required_string_field


def load_locations(path: Path) -> tuple[LocationSpec, ...]:
    """Load and validate configured locations."""
    items = json_file(path)
    if not items:
        raise ConfigurationError(f"Configure at least one location in {path}")
    locations: list[LocationSpec] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        location_id = required_string_field(item, "id", item_path)
        name = optional_string_field(item, "name", item_path)
        if not location_id.replace("-", "").replace("_", "").isalnum():
            raise ConfigurationError("Location id must use letters, numbers, '-' or '_'")
        if location_id in seen_ids:
            raise ConfigurationError(f"Duplicate location id: {location_id}")
        latitude_value = item.get("latitude")
        longitude_value = item.get("longitude")
        if (latitude_value is None) != (longitude_value is None):
            raise ConfigurationError(f"Location {location_id} must provide both latitude and longitude or neither")
        try:
            latitude = float(latitude_value) if latitude_value is not None else None
            longitude = float(longitude_value) if longitude_value is not None else None
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Location {location_id} coordinates must be numbers") from exc
        if latitude is not None and not -90 <= latitude <= 90:
            raise ConfigurationError(f"Location {location_id} latitude is out of range")
        if longitude is not None and not -180 <= longitude <= 180:
            raise ConfigurationError(f"Location {location_id} longitude is out of range")
        if name is None and latitude is None:
            raise ConfigurationError(f"Location {location_id} must provide a name or coordinates")
        language_value = item.get("language", "en")
        if not isinstance(language_value, str):
            raise ConfigurationError(f"{item_path}.language must be a basic BCP 47-like language tag")
        try:
            summary_language = normalize_language_tag(language_value)
        except ValueError as exc:
            raise ConfigurationError(f"{item_path}.language must be a basic BCP 47-like language tag") from exc
        jma_office_code_value = item.get("jma_office_code")
        if jma_office_code_value is not None and not isinstance(jma_office_code_value, str):
            raise ConfigurationError(f"{item_path}.jma_office_code must be a six-digit JMA office code")
        try:
            jma_office_code = normalize_jma_office_code(jma_office_code_value)
        except ValueError as exc:
            raise ConfigurationError(f"{item_path}.jma_office_code must be a six-digit JMA office code") from exc
        locations.append(
            LocationSpec(
                location_id,
                name,
                latitude,
                longitude,
                summary_language=summary_language,
                jma_office_code=jma_office_code,
            )
        )
        seen_ids.add(location_id)
    return tuple(locations)


def _valid_coordinate(value: object, minimum: float, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and minimum <= value <= maximum


def backfill_location_fields(
    path: Path,
    configured: tuple[LocationSpec, ...],
    resolved: tuple[ResolvedLocation, ...],
) -> bool:
    """Write exact provider-resolved names or coordinates missing from the location file."""
    resolved_by_id = {location.id: location for location in resolved if not location.precision_reduced}
    updates: dict[str, dict[str, str | float]] = {}
    for location in configured:
        resolved_location = resolved_by_id.get(location.id)
        if resolved_location is None:
            continue
        fields: dict[str, str | float] = {}
        if location.name is None:
            if not isinstance(resolved_location.name, str) or not resolved_location.name.strip():
                raise ConfigurationError(f"Resolved name for location {location.id} is invalid")
            fields["name"] = resolved_location.name.strip()
        if location.latitude is None and location.longitude is None:
            if not _valid_coordinate(resolved_location.latitude, -90, 90):
                raise ConfigurationError(f"Resolved latitude for location {location.id} is invalid")
            if not _valid_coordinate(resolved_location.longitude, -180, 180):
                raise ConfigurationError(f"Resolved longitude for location {location.id} is invalid")
            fields["latitude"] = resolved_location.latitude
            fields["longitude"] = resolved_location.longitude
        if fields:
            updates[location.id] = fields
    if not updates:
        return False

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        items = json_file(path)
        changed = False
        for item in items:
            location_id = item.get("id")
            if not isinstance(location_id, str) or location_id not in updates:
                continue
            fields = updates[location_id]
            if "name" in fields and item.get("name") is None:
                item["name"] = fields["name"]
                changed = True
            if "latitude" in fields and item.get("latitude") is None and item.get("longitude") is None:
                item["latitude"] = fields["latitude"]
                item["longitude"] = fields["longitude"]
                changed = True
        if not changed:
            return False

        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8") as locations_file:
            locations_file.write(payload)
            locations_file.flush()
            os.fsync(locations_file.fileno())
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary.replace(path)
    except PermissionError as exc:
        raise ConfigurationError(f"{path} must be writable to save resolved location fields") from exc
    except OSError as exc:
        raise ConfigurationError(f"Failed to save resolved location fields to {path}: {exc}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    return True
