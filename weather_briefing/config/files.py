"""Shared JSON configuration file parsing."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .base import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


def json_array(path: Path, content: str) -> list[dict[str, Any]]:
    """Parse a JSON array of objects with a path-specific error."""
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"{path} must contain readable JSON"
        raise ConfigurationError(msg) from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        msg = f"{path} must be a JSON array of objects"
        raise ConfigurationError(msg)
    return value


def json_file(path: Path) -> list[dict[str, Any]]:
    """Read an optional JSON array configuration file."""
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"{path} must contain readable JSON"
        raise ConfigurationError(msg) from exc
    return json_array(path, content)


def required_string_field(item: dict[str, Any], field: str, path: str) -> str:
    """Read a required non-empty string field."""
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        msg = f"{path}.{field} must be a non-empty string"
        raise ConfigurationError(msg)
    return value.strip()


def optional_string_field(item: dict[str, Any], field: str, path: str) -> str | None:
    """Read an optional non-empty string field."""
    value = item.get(field)
    if value is None:
        return None
    return required_string_field(item, field, path)
