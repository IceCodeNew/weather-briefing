"""RSS source configuration loading and validation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector

from weather_briefing.models import FeedConfig

from .base import ConfigurationError
from .files import json_file, required_string_field

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _optional_string_array(
    item: dict[str, Any],
    source_id: str,
    field: str,
    *,
    validator: Callable[[str], object] | None = None,
) -> tuple[str, ...]:
    value = item.get(field)
    if value is None:
        return ()
    if not isinstance(value, list):
        msg = f"RSS source {source_id} field {field} must be a JSON array"
        raise ConfigurationError(msg)
    entries: list[str] = []
    for index, entry in enumerate(value):
        path = f"RSS source {source_id} field {field}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            msg = f"{path} must be a non-empty string"
            raise ConfigurationError(msg)
        if validator is not None:
            try:
                validator(entry)
            except (re.error, SelectorSyntaxError) as exc:
                msg = f"{path} is invalid"
                raise ConfigurationError(msg) from exc
        entries.append(entry)
    return tuple(entries)


def load_feeds(path: Path) -> tuple[FeedConfig, ...]:
    """Load and validate optional RSS source configuration."""
    feeds: list[FeedConfig] = []
    for index, item in enumerate(json_file(path)):
        item_path = f"{path}[{index}]"
        source_id = required_string_field(item, "id", item_path)
        source_name = required_string_field(item, "name", item_path)
        source_url = required_string_field(item, "url", item_path)
        feeds.append(
            FeedConfig(
                id=source_id,
                name=source_name,
                url=source_url,
                verbatim_title_patterns=_optional_string_array(item, source_id, "verbatim_title_patterns"),
                forecast_title_patterns=_optional_string_array(item, source_id, "forecast_title_patterns"),
                content_remove_selectors=_optional_string_array(
                    item,
                    source_id,
                    "content_remove_selectors",
                    validator=compile_selector,
                ),
                content_remove_patterns=_optional_string_array(
                    item,
                    source_id,
                    "content_remove_patterns",
                    validator=re.compile,
                ),
                location_ids=_optional_string_array(item, source_id, "location_ids"),
            )
        )
    return tuple(feeds)
