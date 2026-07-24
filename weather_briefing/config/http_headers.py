"""Strict parsing for configured HTTP request headers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from types import MappingProxyType

from .base import ConfigurationError
from .environment import clean_env

_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class _JSONObject(list[tuple[str, object]]):
    """Preserve JSON object entries so duplicate names can be rejected."""


def headers_from_env(name: str) -> Mapping[str, str]:
    """Read an immutable JSON object containing valid HTTP header fields."""
    configured = clean_env(os.getenv(name))
    if not configured:
        return MappingProxyType({})
    try:
        payload = json.loads(configured, object_pairs_hook=_JSONObject)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must be a valid JSON object") from exc
    if not isinstance(payload, _JSONObject):
        raise ConfigurationError(f"{name} must be a JSON object")

    headers: dict[str, str] = {}
    normalized_names: set[str] = set()
    for header_name, header_value in payload:
        if _HEADER_NAME.fullmatch(header_name) is None:
            raise ConfigurationError(f"{name} contains an invalid HTTP header name")
        normalized_name = header_name.casefold()
        if normalized_name in normalized_names:
            raise ConfigurationError(f"{name} contains duplicate HTTP header names")
        normalized_names.add(normalized_name)
        if not isinstance(header_value, str):
            raise ConfigurationError(f"{name} header values must be strings")
        if not header_value.isascii():
            raise ConfigurationError(f"{name} header values must contain only ASCII characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in header_value):
            raise ConfigurationError(f"{name} header values must not contain control characters")
        headers[header_name] = header_value
    return MappingProxyType(headers)
