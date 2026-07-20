"""Small, provider-neutral language metadata used at provider boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def normalize_language_tag(value: str) -> str:
    """Validate a basic BCP 47-like tag and normalize its separator casing."""
    value = value.strip()
    if _LANGUAGE_TAG.fullmatch(value) is None:
        raise ValueError("Language must be a basic BCP 47-like language tag")
    parts = value.split("-")
    return "-".join([parts[0].lower(), *(part.title() if len(part) == 4 else part.upper() for part in parts[1:])])


@dataclass(frozen=True, slots=True)
class LanguageSupport:
    """Describe fixed or selectable output languages of a provider."""

    default: str
    supported: tuple[str, ...]
    api_codes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Validate and normalize configured language tags."""
        default = normalize_language_tag(self.default)
        supported = tuple(normalize_language_tag(value) for value in self.supported)
        if not supported or default not in supported or len(set(supported)) != len(supported):
            raise ValueError("Language support must contain a unique supported default language")
        api_codes: list[tuple[str, str]] = []
        for language, code in self.api_codes:
            normalized = normalize_language_tag(language)
            if normalized not in supported or not isinstance(code, str) or not code.strip():
                raise ValueError("Language API codes must map supported languages to non-empty strings")
            api_codes.append((normalized, code.strip()))
        if api_codes and (
            len({language for language, _ in api_codes}) != len(api_codes)
            or {language for language, _ in api_codes} != set(supported)
        ):
            raise ValueError("Language API codes must uniquely cover every supported language")
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "supported", supported)
        object.__setattr__(self, "api_codes", tuple(api_codes))

    @property
    def selectable(self) -> bool:
        """Return whether the provider can select among multiple output languages."""
        return len(self.supported) > 1

    def select(self, requested: str | None) -> str:
        """Select a requested supported language or use the provider default."""
        if requested is None:
            return self.default
        normalized = normalize_language_tag(requested)
        if normalized not in self.supported:
            raise ValueError(f"Provider does not support output language: {normalized}")
        return normalized

    def match(self, requested: str | None) -> str:
        """Return the closest supported language for a user output request."""
        if requested is None:
            return self.default
        normalized = normalize_language_tag(requested)
        parts = normalized.split("-")
        while parts:
            candidate = "-".join(parts)
            if candidate in self.supported:
                return candidate
            parts.pop()
        return self.default

    def api_code(self, selected: str | None = None) -> str:
        """Return the provider wire-language code for a selected language."""
        language = self.select(selected)
        if not self.api_codes:
            return language
        return dict(self.api_codes)[language]

    @classmethod
    def fixed(cls, language: str) -> LanguageSupport:
        """Create metadata for a provider with one fixed output language."""
        return cls(default=language, supported=(language,))


def localized_labels(language: str, translations: Mapping[str, Mapping[str, str]]) -> Mapping[str, str]:
    """Return scaffold labels for an exact or primary language tag."""
    normalized = normalize_language_tag(language)
    for candidate in (normalized, normalized.split("-", maxsplit=1)[0]):
        if candidate in translations:
            return translations[candidate]
    raise ValueError(f"No document scaffold labels for language: {normalized}")
