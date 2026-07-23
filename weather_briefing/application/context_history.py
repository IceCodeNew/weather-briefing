"""Selection and budgeting of historical context sent to the LLM."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from ..llm import serialize_llm_payload
from ..models import SourceDocument

_FINGERPRINT_CHUNK_CHARACTERS = 64 * 1024
_FINGERPRINT_SINGLE_PASS_CHARACTERS = 4 * _FINGERPRINT_CHUNK_CHARACTERS


@dataclass(frozen=True, slots=True)
class HistoricalContextCandidate:
    """Associate a historical document with its selection role."""

    document: SourceDocument
    role: Literal["latest", "retention_baseline", "recent_change"]


@dataclass(frozen=True, slots=True)
class HistoricalContextOverflow:
    """Identify mandatory context omitted by a configured budget."""

    source_id: str
    role: Literal["latest", "retention_baseline"]
    reason: Literal["document_limit", "character_limit"]
    fingerprint: str
    full_payload_characters: int | None
    compact_payload_characters: int | None


@dataclass(frozen=True, slots=True)
class BoundedContextHistory:
    """Contain selected context and overflow metadata."""

    payload: list[dict[str, object]]
    documents: tuple[SourceDocument, ...]
    serialized_characters: int
    distinct_sources: int
    mandatory_documents: int
    compacted_documents: int
    overflows: tuple[HistoricalContextOverflow, ...]


@dataclass(slots=True)
class _ContextSourceChanges:
    baseline: tuple[int, SourceDocument]
    recent: deque[tuple[int, SourceDocument]]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object:
        """Add bytes to the digest state."""
        ...


def _utf8_length(value: str) -> int:
    return sum(
        len(value[start : start + _FINGERPRINT_CHUNK_CHARACTERS].encode())
        for start in range(0, len(value), _FINGERPRINT_CHUNK_CHARACTERS)
    )


def _update_framed_text_digest(digest: _Digest, value: str) -> None:
    """Hash length-prefixed UTF-8 without allocating bytes for the full value."""
    if len(value) <= _FINGERPRINT_SINGLE_PASS_CHARACTERS:
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
        return
    digest.update(_utf8_length(value).to_bytes(8, byteorder="big"))
    for start in range(0, len(value), _FINGERPRINT_CHUNK_CHARACTERS):
        digest.update(value[start : start + _FINGERPRINT_CHUNK_CHARACTERS].encode())


def _context_overflow_fingerprint(candidate: HistoricalContextCandidate) -> str:
    digest = hashlib.sha256()
    for value in (candidate.role, *_context_document_value(candidate.document)):
        _update_framed_text_digest(digest, value or "")
    return digest.hexdigest()


def serialize_context_document(
    document: SourceDocument,
    *,
    history_role: Literal["latest", "retention_baseline", "recent_change"],
    compact: bool = False,
) -> dict[str, object]:
    """Serialize one historical context document for the LLM."""
    entry: dict[str, object] = {
        "source_id": document.id,
        "name": document.name,
        "url": document.url,
        "language": document.language,
        "content": document.history_summary if compact else document.content,
        "history_role": history_role,
    }
    if compact:
        entry["content_compacted"] = True
        entry["original_content_characters"] = len(document.content)
    return entry


def _context_overflow(
    candidate: HistoricalContextCandidate,
    reason: Literal["document_limit", "character_limit"],
    *,
    full_payload_characters: int | None = None,
    compact_payload_characters: int | None = None,
) -> HistoricalContextOverflow:
    if candidate.role == "recent_change":
        raise ValueError("recent changes are optional and cannot produce overflow alerts")
    return HistoricalContextOverflow(
        source_id=candidate.document.id,
        role=candidate.role,
        reason=reason,
        fingerprint=_context_overflow_fingerprint(candidate),
        full_payload_characters=full_payload_characters,
        compact_payload_characters=compact_payload_characters,
    )


def bounded_context_history(
    documents: tuple[SourceDocument, ...],
    *,
    max_documents: int,
    max_characters: int,
) -> BoundedContextHistory:
    """Select historical documents within count and serialized-size budgets."""
    candidates, omitted_overflows = _context_history_selection(documents, max_documents)
    selected: list[tuple[HistoricalContextCandidate, dict[str, object]]] = []
    overflows = list(omitted_overflows)
    serialized_characters = len("[]")
    for candidate in candidates:
        entry = serialize_context_document(candidate.document, history_role=candidate.role)
        candidate_payload = [*(selected_entry for _, selected_entry in selected), entry]
        candidate_characters = len(serialize_llm_payload(candidate_payload))
        full_payload_characters = candidate_characters
        compact_payload_characters = None
        if candidate_characters <= max_characters:
            selected.append((candidate, entry))
            serialized_characters = candidate_characters
            continue
        if candidate.document.history_summary:
            entry = serialize_context_document(candidate.document, history_role=candidate.role, compact=True)
            candidate_payload = [*(selected_entry for _, selected_entry in selected), entry]
            candidate_characters = len(serialize_llm_payload(candidate_payload))
            compact_payload_characters = candidate_characters
            if candidate_characters <= max_characters:
                selected.append((candidate, entry))
                serialized_characters = candidate_characters
                continue
        if candidate.role != "recent_change":
            compactable: list[tuple[int, int, dict[str, object]]] = []
            for index, (selected_candidate, selected_entry) in enumerate(selected):
                if selected_candidate.document.history_summary and not selected_entry.get("content_compacted"):
                    compact_entry = serialize_context_document(
                        selected_candidate.document,
                        history_role=selected_candidate.role,
                        compact=True,
                    )
                    full_characters = len(serialize_llm_payload(selected_entry))
                    compact_characters = len(serialize_llm_payload(compact_entry))
                    if compact_characters < full_characters:
                        compactable.append((full_characters - compact_characters, index, compact_entry))
            compactable.sort(key=lambda item: item[0], reverse=True)
            for _, index, compact_entry in compactable:
                selected[index] = (selected[index][0], compact_entry)
                selected_payload = [selected_entry for _, selected_entry in selected]
                serialized_characters = len(serialize_llm_payload(selected_payload))
                candidate_payload = [*selected_payload, entry]
                candidate_characters = len(serialize_llm_payload(candidate_payload))
                if entry.get("content_compacted"):
                    compact_payload_characters = candidate_characters
                else:
                    full_payload_characters = candidate_characters
                if candidate_characters <= max_characters:
                    selected.append((candidate, entry))
                    serialized_characters = candidate_characters
                    break
            else:
                overflows.append(
                    _context_overflow(
                        candidate,
                        "character_limit",
                        full_payload_characters=full_payload_characters,
                        compact_payload_characters=compact_payload_characters,
                    )
                )
                continue
            continue
    selected_documents: dict[str, SourceDocument] = {}
    for candidate, _ in selected:
        selected_documents.setdefault(candidate.document.id, candidate.document)
    return BoundedContextHistory(
        payload=[entry for _, entry in selected],
        documents=tuple(selected_documents.values()),
        serialized_characters=serialized_characters,
        distinct_sources=len({document.id for document in documents}),
        mandatory_documents=sum(candidate.role != "recent_change" for candidate in candidates) + len(omitted_overflows),
        compacted_documents=sum(bool(entry.get("content_compacted")) for _, entry in selected),
        overflows=tuple(overflows),
    )


def context_budget_fingerprints(overflows: tuple[HistoricalContextOverflow, ...]) -> dict[str, str]:
    """Combine mandatory overflow fingerprints into one stable value per source."""
    grouped: dict[str, list[str]] = {}
    for overflow in overflows:
        grouped.setdefault(overflow.source_id, []).append(overflow.fingerprint)
    return {
        source_id: hashlib.sha256("\0".join(sorted(fingerprints)).encode()).hexdigest()
        for source_id, fingerprints in grouped.items()
    }


def context_history_candidates(
    documents: tuple[SourceDocument, ...],
    max_documents: int,
) -> tuple[HistoricalContextCandidate, ...]:
    """Return the candidates selected by the document count policy."""
    candidates, _ = _context_history_selection(documents, max_documents)
    return candidates


def _context_history_selection(
    documents: tuple[SourceDocument, ...],
    max_documents: int,
) -> tuple[tuple[HistoricalContextCandidate, ...], tuple[HistoricalContextOverflow, ...]]:
    changes_by_source: dict[str, _ContextSourceChanges] = {}
    for index, document in enumerate(documents):
        source_changes = changes_by_source.get(document.id)
        if source_changes is None:
            snapshot = (index, document)
            changes_by_source[document.id] = _ContextSourceChanges(
                baseline=snapshot,
                recent=deque((snapshot,), maxlen=max_documents),
            )
            continue
        if _context_document_value(source_changes.recent[-1][1]) != _context_document_value(document):
            source_changes.recent.append((index, document))

    source_changes = sorted(changes_by_source.values(), key=lambda changes: changes.recent[-1][0], reverse=True)
    mandatory: list[tuple[int, SourceDocument, Literal["latest", "retention_baseline"]]] = []
    selected_indexes: set[int] = set()

    def add_mandatory(
        snapshot: tuple[int, SourceDocument],
        role: Literal["latest", "retention_baseline"],
    ) -> None:
        if snapshot[0] not in selected_indexes:
            mandatory.append((snapshot[0], snapshot[1], role))
            selected_indexes.add(snapshot[0])

    for changes in source_changes:
        add_mandatory(changes.recent[-1], "latest")
    for changes in source_changes:
        add_mandatory(changes.baseline, "retention_baseline")

    candidates = [
        (index, HistoricalContextCandidate(document, role)) for index, document, role in mandatory[:max_documents]
    ]
    omitted_mandatory = mandatory[max_documents:]

    depth = 2
    while len(candidates) < max_documents:
        added_change = False
        for changes in source_changes:
            if len(changes.recent) > depth:
                snapshot = changes.recent[-depth]
                candidates.append((snapshot[0], HistoricalContextCandidate(snapshot[1], "recent_change")))
                selected_indexes.add(snapshot[0])
                if len(candidates) == max_documents:
                    break
                added_change = True
        if not added_change:
            break
        depth += 1

    return (
        tuple(candidate for _, candidate in candidates),
        tuple(
            _context_overflow(HistoricalContextCandidate(document, role), "document_limit")
            for _, document, role in omitted_mandatory
        ),
    )


def _context_document_value(document: SourceDocument) -> tuple[str, str, str, str, str | None]:
    if document.history_value is not None:
        return document.name, document.url, document.language, document.history_value, None
    return document.name, document.url, document.language, document.content, document.history_summary
