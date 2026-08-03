"""Strict parsing for configured HTTP request headers."""

from __future__ import annotations

import json
import os
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

from .base import ConfigurationError
from .environment import clean_env

if TYPE_CHECKING:
    from collections.abc import Mapping

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
        msg = f"{name} must be a valid JSON object"
        raise ConfigurationError(msg) from exc
    if not isinstance(payload, _JSONObject):
        msg = f"{name} must be a JSON object"
        raise ConfigurationError(msg)

    headers: dict[str, str] = {}
    normalized_names: set[str] = set()
    for header_name, header_value in payload:
        if _HEADER_NAME.fullmatch(header_name) is None:
            msg = f"{name} contains an invalid HTTP header name"
            raise ConfigurationError(msg)
        normalized_name = header_name.casefold()
        if normalized_name in normalized_names:
            msg = f"{name} contains duplicate HTTP header names"
            raise ConfigurationError(msg)
        normalized_names.add(normalized_name)
        if not isinstance(header_value, str):
            msg = f"{name} header values must be strings"
            raise ConfigurationError(msg)
        if not header_value.isascii():
            msg = f"{name} header values must contain only ASCII characters"
            raise ConfigurationError(msg)
        if any(ord(character) < 32 or ord(character) == 127 for character in header_value):  # noqa: PLR2004
            msg = f"{name} header values must not contain control characters"
            raise ConfigurationError(msg)
        headers[header_name] = header_value
    return MappingProxyType(headers)
