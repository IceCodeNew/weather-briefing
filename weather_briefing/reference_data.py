from __future__ import annotations

import json
from functools import cache
from importlib import resources
from pathlib import PurePath
from typing import Any

from . import data


class ReferenceDataError(RuntimeError):
    """Raised when packaged domain reference data is missing or malformed."""


@cache
def load_reference_data(filename: str) -> dict[str, object]:
    if PurePath(filename).name != filename or not filename.endswith(".json"):
        raise ReferenceDataError("Reference data filename must identify one JSON file")
    try:
        text = resources.files(data).joinpath(filename).read_text(encoding="utf-8")
        value = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ReferenceDataError(f"Unable to load reference data: {filename}") from exc
    if not isinstance(value, dict):
        raise ReferenceDataError(f"Reference data root must be an object: {filename}")
    return value


def reference_value(filename: str, *path: str) -> Any:
    value: Any = load_reference_data(filename)
    try:
        for key in path:
            value = value[key]
    except (KeyError, TypeError) as exc:
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Missing reference data field: {filename}:{joined_path}") from exc
    return value


def reference_string(filename: str, *path: str) -> str:
    value = reference_value(filename, *path)
    if not isinstance(value, str) or not value.strip():
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Reference data field must be a non-empty string: {filename}:{joined_path}")
    return value


def reference_string_tuple(filename: str, *path: str) -> tuple[str, ...]:
    value = reference_value(filename, *path)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        joined_path = ".".join(path)
        raise ReferenceDataError(f"Reference data field must be a non-empty string list: {filename}:{joined_path}")
    return tuple(value)
