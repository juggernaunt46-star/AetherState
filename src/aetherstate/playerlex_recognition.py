"""Recognition-only PlayerLex projection into a compiled Semantic Fabric meaning.

PlayerLex supplies local approval identity and exact source spans.  The verified Semantic
Fabric supplies every semantic field that later binding code may consume.  This boundary never
creates a capability candidate, assignment, mechanic, settlement, or world record.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .capability_glossary import content_fingerprint
from .semantic_fabric import (
    CompiledMeaning,
    SemanticFabric,
    SemanticFabricError,
    SemanticLexMatch,
)


PLAYERLEX_PROPOSAL_SCHEMA = "playerlex-recognition-proposal/1"
PLAYERLEX_CANDIDATE_SCHEMA = "playerlex-recognition-candidate/1"
PLAYERLEX_SURFACE_BASELINE = "playerlex"
PLAYERLEX_MATCH_SCORE = 100

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTRY_ID_RE = re.compile(r"playerlex_([0-9a-f]{32})\Z")
_LEX_IDS = frozenset({"capability", "referent", "scene", "action"})


def _approval_source_id(candidate: Mapping[str, Any]) -> str | None:
    """Return one prose-free, replay-safe reference to the local approval revision."""
    entry_id = candidate.get("entry_id")
    revision = candidate.get("entry_revision")
    provenance = candidate.get("provenance")
    match = _ENTRY_ID_RE.fullmatch(entry_id) if isinstance(entry_id, str) else None
    if (
        match is None
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(provenance, Mapping)
        or provenance.get("approval") != "explicit_local"
        or provenance.get("approved_via") != "local_control_api"
        or provenance.get("approval_revision") != revision
    ):
        return None
    approved_at = provenance.get("approved_at")
    if (
        isinstance(approved_at, bool)
        or not isinstance(approved_at, (int, float))
        or not math.isfinite(float(approved_at))
        or float(approved_at) < 0
    ):
        return None
    return f"playerlex.{match.group(1)}.r{revision}"


def _proposal_identity(
    candidate: Mapping[str, Any],
) -> tuple[str, str, str, Mapping[str, Any]] | None:
    if (
        candidate.get("schema") != PLAYERLEX_CANDIDATE_SCHEMA
        or candidate.get("status") not in (None, "current")
        or candidate.get("recognized") is not True
        or candidate.get("authorized") is not False
        or candidate.get("executable") is not False
        or candidate.get("requires_context_binding") is not True
    ):
        return None
    lex_id = candidate.get("lex_id")
    concept = candidate.get("concept")
    if not isinstance(lex_id, str) or lex_id not in _LEX_IDS or not isinstance(concept, Mapping):
        return None
    concept_id = concept.get("concept_id")
    meaning_fingerprint = concept.get("meaning_fingerprint")
    if (
        not isinstance(concept_id, str)
        or not concept_id
        or concept.get("lex_id") != lex_id
        or not isinstance(meaning_fingerprint, str)
        or _FINGERPRINT_RE.fullmatch(meaning_fingerprint) is None
    ):
        return None
    return lex_id, concept_id, meaning_fingerprint, concept


def _source_span(candidate: Mapping[str, Any], source_text: str) -> tuple[int, int, str] | None:
    span = candidate.get("source_span")
    if not isinstance(span, Mapping):
        return None
    start, end, text = span.get("start"), span.get("end"), span.get("text")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end > len(source_text)
        or not isinstance(text, str)
        or not text
        or len(text) != end - start
        or source_text[start:end] != text
    ):
        return None
    return start, end, text


def _capability_projection(
    fabric: SemanticFabric,
    concept_id: str,
    meaning_fingerprint: str,
    concept: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...], str, Mapping[str, Any], tuple[str, ...]] | None:
    glossary = fabric.capability_glossary
    try:
        classification = glossary.concept_classification(concept_id)
        _entry_fingerprint, source_ids = glossary.concept_lineage(concept_id)
    except (KeyError, SemanticFabricError, TypeError, ValueError):
        return None
    if (
        classification.get("meaning_fingerprint") != meaning_fingerprint
        or concept.get("concept_kind") != classification.get("concept_kind")
        or list(concept.get("domain_shelves") or ()) != list(classification.get("domain_shelves") or ())
    ):
        return None
    return (
        str(classification["concept_kind"]),
        ("actor",),
        ("target", "object", "locus", "instrument"),
        "context_binding_required",
        {
            "categories": list(classification["domain_shelves"]),
            "meaning_facets": dict(classification["meaning_facets"]),
            "meaning_fingerprint": meaning_fingerprint,
        },
        tuple(str(item) for item in source_ids),
    )


def _pack_projection(
    fabric: SemanticFabric,
    lex_id: str,
    concept_id: str,
    meaning_fingerprint: str,
    concept: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...], str, Mapping[str, Any], tuple[str, ...]] | None:
    try:
        entry = fabric.entry(concept_id)
    except (KeyError, SemanticFabricError, TypeError, ValueError):
        return None
    if (
        entry.lex_id != lex_id
        or entry.meaning_fingerprint != meaning_fingerprint
        or concept.get("concept_kind") != entry.kind
    ):
        return None
    return (
        entry.kind,
        tuple(entry.required_roles),
        tuple(entry.optional_roles),
        entry.completion,
        entry.features,
        tuple(entry.source_ids),
    )


def _semantic_match(
    candidate: Mapping[str, Any],
    *,
    fabric: SemanticFabric,
    source_text: str,
) -> SemanticLexMatch | None:
    identity = _proposal_identity(candidate)
    span = _source_span(candidate, source_text)
    approval_source = _approval_source_id(candidate)
    if identity is None or span is None or approval_source is None:
        return None
    lex_id, concept_id, meaning_fingerprint, concept = identity
    start, end, matched_phrase = span
    projection = (
        _capability_projection(fabric, concept_id, meaning_fingerprint, concept)
        if lex_id == "capability"
        else _pack_projection(fabric, lex_id, concept_id, meaning_fingerprint, concept)
    )
    if projection is None:
        return None
    kind, required_roles, optional_roles, completion, features, semantic_sources = projection
    return SemanticLexMatch(
        lex_id=lex_id,
        concept_id=concept_id,
        kind=kind,
        matched_phrase=matched_phrase,
        start=start,
        end=end,
        score=PLAYERLEX_MATCH_SCORE,
        genres=("*",),
        required_roles=required_roles,
        optional_roles=optional_roles,
        completion=completion,
        features=features,
        source_ids=tuple(dict.fromkeys((*semantic_sources, approval_source))),
        # The approved current meaning fingerprint is semantic identity evidence only.  It is
        # deliberately not a PlayerLex authority or mechanic receipt.
        entry_fingerprint=meaning_fingerprint,
        surface_baseline=PLAYERLEX_SURFACE_BASELINE,
    )


def _match_identity(match: SemanticLexMatch) -> tuple[Any, ...]:
    return (
        match.lex_id,
        match.concept_id,
        match.start,
        match.end,
        match.matched_phrase,
        match.entry_fingerprint,
        match.surface_baseline,
    )


def _canonical_matches(matches: list[SemanticLexMatch]) -> tuple[SemanticLexMatch, ...]:
    """Coalesce duplicate approvals, then retain same-Lex same-span polysemy."""
    unique: dict[tuple[Any, ...], SemanticLexMatch] = {}
    for match in matches:
        key = _match_identity(match)
        previous = unique.get(key)
        if previous is None:
            unique[key] = match
            continue
        unique[key] = replace(
            previous,
            genres=tuple(dict.fromkeys((*previous.genres, *match.genres))),
            source_ids=tuple(sorted(set(previous.source_ids) | set(match.source_ids))),
        )

    rows = list(unique.values())
    concepts_by_span: dict[tuple[str, int, int], set[str]] = {}
    for match in rows:
        concepts_by_span.setdefault((match.lex_id, match.start, match.end), set()).add(match.concept_id)
    rows = [
        replace(match, ambiguity=tuple(sorted(concepts)))
        if len(concepts := concepts_by_span[(match.lex_id, match.start, match.end)]) > 1
        else replace(match, ambiguity=())
        for match in rows
    ]
    rows.sort(
        key=lambda item: (
            item.start,
            -(item.end - item.start),
            -item.score,
            item.lex_id,
            item.concept_id,
            item.entry_fingerprint,
            item.surface_baseline,
            item.source_ids,
        )
    )
    return tuple(rows)


def merge_playerlex_proposal(
    base: CompiledMeaning,
    proposal: Mapping[str, Any],
    *,
    fabric: SemanticFabric,
    source_text: str,
) -> CompiledMeaning:
    """Merge current PlayerLex matches without trusting PlayerLex semantic valence.

    ``source_text`` must reproduce the base source fingerprint, and every candidate span must equal
    its exact in-bounds source slice.  Refused, stale, corrupt, malformed, and authority-claiming rows
    never enter the compiled meaning.  An empty or unusable proposal returns ``base`` itself,
    preserving the no-overlay path's exact bytes and object identity.
    """
    if (
        not isinstance(base, CompiledMeaning)
        or not isinstance(proposal, Mapping)
        or not isinstance(source_text, str)
        or base.source_fingerprint != content_fingerprint(source_text)
    ):
        return base
    raw_matches = proposal.get("matches")
    if (
        proposal.get("schema") != PLAYERLEX_PROPOSAL_SCHEMA
        or not isinstance(raw_matches, list)
        or isinstance(proposal.get("match_count"), bool)
        or proposal.get("match_count") != len(raw_matches)
    ):
        return base
    overlays = [
        match
        for raw in raw_matches
        if isinstance(raw, Mapping)
        and (match := _semantic_match(raw, fabric=fabric, source_text=source_text)) is not None
    ]
    if not overlays:
        return base

    matches = _canonical_matches([*base.matches, *overlays])
    unresolved = tuple(
        sorted(set(base.unresolved) | {concept_id for match in matches for concept_id in match.ambiguity})
    )
    return CompiledMeaning(
        source_fingerprint=base.source_fingerprint,
        fabric_fingerprint=base.fabric_fingerprint,
        genre_ids=base.genre_ids,
        matches=matches,
        unresolved=unresolved,
    )
