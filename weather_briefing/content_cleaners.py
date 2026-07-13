from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Protocol

from bs4 import BeautifulSoup, Comment
from soupsieve import SelectorSyntaxError

from .reference_data import reference_string_tuple


class ContentCleaningError(ValueError):
    """Raised when source-specific cleaning configuration is invalid."""


class ContentCleaner(Protocol):
    def clean(self, content: str, rules: ContentCleaningRules) -> str: ...


@dataclass(frozen=True, slots=True)
class ContentCleaningRules:
    remove_selectors: tuple[str, ...] = ()
    remove_patterns: tuple[str, ...] = ()


class HTMLContentCleaner:
    def clean(self, content: str, rules: ContentCleaningRules) -> str:
        default_selectors, default_patterns = _default_cleaning_rules()
        soup = BeautifulSoup(content, "html.parser")
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        try:
            for selector in (*default_selectors, *rules.remove_selectors):
                for element in soup.select(selector):
                    element.decompose()
        except SelectorSyntaxError as exc:
            raise ContentCleaningError("Invalid content removal selector") from exc
        try:
            patterns = tuple(
                re.compile(pattern)
                for pattern in (*default_patterns, *rules.remove_patterns)
            )
        except re.error as exc:
            raise ContentCleaningError("Invalid content removal pattern") from exc
        lines = (line.strip() for line in soup.get_text("\n").splitlines())
        return "\n".join(
            line for line in lines if line and not any(pattern.search(line) for pattern in patterns)
        )


@cache
def _default_cleaning_rules() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        reference_string_tuple("content_cleaning.json", "default_remove_selectors"),
        reference_string_tuple("content_cleaning.json", "default_remove_patterns"),
    )
