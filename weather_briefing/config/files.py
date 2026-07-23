"""Shared JSON configuration file parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ConfigurationError


def json_array(path: Path, content: str) -> list[dict[str, Any]]:
    """Parse a JSON array of objects with a path-specific error."""
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigurationError(f"{path} must be a JSON array of objects")
    return value


def json_file(path: Path) -> list[dict[str, Any]]:
    """Read an optional JSON array configuration file."""
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"{path} must contain readable JSON") from exc
    return json_array(path, content)


def required_string_field(item: dict[str, Any], field: str, path: str) -> str:
    """Read a required non-empty string field."""
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path}.{field} must be a non-empty string")
    return value.strip()


def optional_string_field(item: dict[str, Any], field: str, path: str) -> str | None:
    """Read an optional non-empty string field."""
    value = item.get(field)
    if value is None:
        return None
    return required_string_field(item, field, path)
