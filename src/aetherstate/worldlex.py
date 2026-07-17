"""WorldLex Translation Memory contracts.

WorldLex sits after contextual semantic framing and before world/entity binding, immutable
definition admission, assignment, reducer receipts, settlement, and narration.  This module only
validates evidence-bearing data products and exact references.  It never grants a capability,
promotes a definition, selects from a pool, admits a receipt, mutates state, or narrates.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .capability_glossary import GlossaryMatch, content_fingerprint


WORLDLEX_NAME = "WorldLex Translation Memory"
WORLDLEX_SHORT_NAME = "WorldLex"
WORLDLEX_CORE_VERSION = "worldlex-core/1"

CONTEXT_FRAME_SCHEMA = "worldlex-context-frame/1"
TRANSLATION_RESULT_SCHEMA = "worldlex-translation-result/1"
SUBJECT_REF_SCHEMA = "worldlex-subject-ref/1"
OWNER_REF_SCHEMA = "worldlex-owner-ref/1"
DEFINITION_REF_SCHEMA = "worldlex-definition-ref/1"
CAPABILITY_POOL_SCHEMA = "capability-pool/1"
ADAPTER_CONTRACT_SCHEMA = "worldlex-adapter-contract/1"
ADAPTER_REGISTRY_SCHEMA = "worldlex-adapter-registry/1"

POOL_STAGES = ("world_library", "assigned", "spawn_eligible", "runtime")
POOL_CLASSIFICATIONS = frozenset(
    {"recognized", "narration_boundary", "lore_only", "executable"}
)

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORLD_ID_RE = re.compile(r"world_[0-9a-f]{32}\Z")
_KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,159}\Z")
_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_VERSIONED_ID_RE = re.compile(
    r"[a-z][a-z0-9_.-]*(?:/[a-z][a-z0-9_.-]*)*/[1-9][0-9]*\Z"
)
_POLARITIES = frozenset({"positive", "negative", "unknown"})
_MODALITIES = frozenset(
    {"actual", "command", "hypothetical", "question", "possible", "unknown"}
)
_TIME_SCOPES = frozenset({"current", "past", "future", "atemporal", "unknown"})


class WorldLexError(ValueError):
    """A WorldLex data product violates its versioned non-authoritative contract."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorldLexError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise WorldLexError(f"{label} fields must be strings")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    label: str,
) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise WorldLexError(f"{label} has unexpected fields: {unexpected}")
    missing = sorted(required - set(value))
    if missing:
        raise WorldLexError(f"{label} is missing required fields: {missing}")


def _plain_id(value: object, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _ID_RE.fullmatch(value):
        raise WorldLexError(f"{label} must be a stable lowercase identifier")
    return value


def _world_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _WORLD_ID_RE.fullmatch(value):
        raise WorldLexError(f"{label} must be world_ followed by 32 lowercase hex digits")
    return value


def _kind(value: object, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _KIND_RE.fullmatch(value):
        raise WorldLexError(f"{label} must be a stable lowercase kind")
    return value


def _versioned_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _VERSIONED_ID_RE.fullmatch(value)
    ):
        raise WorldLexError(f"{label} must be a versioned identifier ending in /N")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorldLexError(f"{label} must be a sha256 content fingerprint")
    return value


def _optional_fingerprint(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, label)


def _external_ref(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 240:
        raise WorldLexError(f"{label} must be a non-empty stable external reference")
    return value


def _string_tuple(
    value: object,
    label: str,
    *,
    validator: str = "kind",
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise WorldLexError(f"{label} must be a list of strings")
    validate = _kind if validator == "kind" else _versioned_id
    result = tuple(sorted({validate(item, label) for item in value}))
    if not allow_empty and not result:
        raise WorldLexError(f"{label} must not be empty")
    return result


def _canonical_payload_fingerprint(payload: Mapping[str, Any]) -> str:
    return content_fingerprint(dict(payload))


@dataclass(frozen=True)
class SubjectRef:
    """Typed identity of the entity or world object a downstream pool concerns."""

    kind: str
    id: str
    world_id: str

    def __post_init__(self) -> None:
        _kind(self.kind, "subject kind")
        _plain_id(self.id, "subject id")
        _world_id(self.world_id, "subject world_id")

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": SUBJECT_REF_SCHEMA,
            "kind": self.kind,
            "id": self.id,
            "world_id": self.world_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SubjectRef:
        value = _mapping(value, "subject reference")
        fields = {"schema", "kind", "id", "world_id"}
        _exact_fields(value, allowed=fields, required=fields, label="subject reference")
        if value["schema"] != SUBJECT_REF_SCHEMA:
            raise WorldLexError("unsupported subject reference schema")
        return cls(
            kind=_kind(value["kind"], "subject kind"),
            id=_plain_id(value["id"], "subject id"),
            world_id=_world_id(value["world_id"], "subject world_id"),
        )


@dataclass(frozen=True)
class OwnerRef:
    """Typed owner identity carried by an immutable downstream definition reference."""

    kind: str
    id: str
    world_id: str

    def __post_init__(self) -> None:
        _kind(self.kind, "owner kind")
        _plain_id(self.id, "owner id")
        _world_id(self.world_id, "owner world_id")

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": OWNER_REF_SCHEMA,
            "kind": self.kind,
            "id": self.id,
            "world_id": self.world_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OwnerRef:
        value = _mapping(value, "owner reference")
        fields = {"schema", "kind", "id", "world_id"}
        _exact_fields(value, allowed=fields, required=fields, label="owner reference")
        if value["schema"] != OWNER_REF_SCHEMA:
            raise WorldLexError("unsupported owner reference schema")
        return cls(
            kind=_kind(value["kind"], "owner kind"),
            id=_plain_id(value["id"], "owner id"),
            world_id=_world_id(value["world_id"], "owner world_id"),
        )


@dataclass(frozen=True)
class ContextFrame:
    """Reference to semantic context decided before WorldLex performs translation."""

    frame_id: str
    source_id: str
    source_fingerprint: str
    span_start: int
    span_end: int
    polarity: str
    modality: str
    time_scope: str
    quoted: bool
    genre_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _plain_id(self.frame_id, "context frame_id")
        _plain_id(self.source_id, "context source_id")
        _fingerprint(self.source_fingerprint, "context source_fingerprint")
        if isinstance(self.span_start, bool) or not isinstance(self.span_start, int):
            raise WorldLexError("context span_start must be an integer")
        if isinstance(self.span_end, bool) or not isinstance(self.span_end, int):
            raise WorldLexError("context span_end must be an integer")
        if self.span_start < 0 or self.span_end <= self.span_start:
            raise WorldLexError("context span must be non-empty and ordered")
        if self.polarity not in _POLARITIES:
            raise WorldLexError("unsupported context polarity")
        if self.modality not in _MODALITIES:
            raise WorldLexError("unsupported context modality")
        if self.time_scope not in _TIME_SCOPES:
            raise WorldLexError("unsupported context time_scope")
        if not isinstance(self.quoted, bool):
            raise WorldLexError("context quoted must be a boolean")
        genres = _string_tuple(self.genre_ids, "context genre_ids")
        if genres != self.genre_ids:
            object.__setattr__(self, "genre_ids", genres)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_FRAME_SCHEMA,
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "polarity": self.polarity,
            "modality": self.modality,
            "time_scope": self.time_scope,
            "quoted": self.quoted,
            "genre_ids": list(self.genre_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextFrame:
        value = _mapping(value, "context frame")
        fields = {
            "schema",
            "frame_id",
            "source_id",
            "source_fingerprint",
            "span_start",
            "span_end",
            "polarity",
            "modality",
            "time_scope",
            "quoted",
            "genre_ids",
        }
        _exact_fields(value, allowed=fields, required=fields, label="context frame")
        if value["schema"] != CONTEXT_FRAME_SCHEMA:
            raise WorldLexError("unsupported context frame schema")
        return cls(
            frame_id=_plain_id(value["frame_id"], "context frame_id"),
            source_id=_plain_id(value["source_id"], "context source_id"),
            source_fingerprint=_fingerprint(
                value["source_fingerprint"], "context source_fingerprint"
            ),
            span_start=value["span_start"],
            span_end=value["span_end"],
            polarity=str(value["polarity"]),
            modality=str(value["modality"]),
            time_scope=str(value["time_scope"]),
            quoted=value["quoted"],
            genre_ids=_string_tuple(value["genre_ids"], "context genre_ids"),
        )


@dataclass(frozen=True)
class TranslationCandidate:
    """Recognition evidence only; the authority fields are fixed false by contract."""

    concept_id: str
    label: str
    categories: tuple[str, ...]
    concept_type: str
    matched_phrase: str
    score: int
    genre_ids: tuple[str, ...]
    baseline: str
    recognized: bool = True
    authorized: bool = False
    executable: bool = False
    requires_context_binding: bool = True

    def __post_init__(self) -> None:
        _plain_id(self.concept_id, "translation concept_id")
        if not isinstance(self.label, str) or not self.label.strip():
            raise WorldLexError("translation candidate label must not be empty")
        if not isinstance(self.matched_phrase, str) or not self.matched_phrase.strip():
            raise WorldLexError("translation matched_phrase must not be empty")
        _kind(self.concept_type, "translation concept_type")
        categories = _string_tuple(
            self.categories, "translation categories", allow_empty=False
        )
        genres = _string_tuple(self.genre_ids, "translation genre_ids")
        if categories != self.categories:
            object.__setattr__(self, "categories", categories)
        if genres != self.genre_ids:
            object.__setattr__(self, "genre_ids", genres)
        if isinstance(self.score, bool) or not isinstance(self.score, int):
            raise WorldLexError("translation candidate score must be an integer")
        if not isinstance(self.baseline, str) or not self.baseline.strip():
            raise WorldLexError("translation baseline must not be empty")
        if (
            self.recognized is not True
            or self.authorized is not False
            or self.executable is not False
            or self.requires_context_binding is not True
        ):
            raise WorldLexError(
                "WorldLex recognition cannot authorize or execute a candidate"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "label": self.label,
            "categories": list(self.categories),
            "concept_type": self.concept_type,
            "matched_phrase": self.matched_phrase,
            "score": self.score,
            "genre_ids": list(self.genre_ids),
            "baseline": self.baseline,
            "recognized": True,
            "authorized": False,
            "executable": False,
            "requires_context_binding": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TranslationCandidate:
        value = _mapping(value, "translation candidate")
        fields = {
            "concept_id",
            "label",
            "categories",
            "concept_type",
            "matched_phrase",
            "score",
            "genre_ids",
            "baseline",
            "recognized",
            "authorized",
            "executable",
            "requires_context_binding",
        }
        _exact_fields(value, allowed=fields, required=fields, label="translation candidate")
        return cls(
            concept_id=_plain_id(value["concept_id"], "translation concept_id"),
            label=str(value["label"]),
            categories=_string_tuple(
                value["categories"], "translation categories", allow_empty=False
            ),
            concept_type=_kind(value["concept_type"], "translation concept_type"),
            matched_phrase=str(value["matched_phrase"]),
            score=value["score"],
            genre_ids=_string_tuple(value["genre_ids"], "translation genre_ids"),
            baseline=str(value["baseline"]),
            recognized=value["recognized"],
            authorized=value["authorized"],
            executable=value["executable"],
            requires_context_binding=value["requires_context_binding"],
        )

    @classmethod
    def from_glossary_match(
        cls, value: GlossaryMatch | Mapping[str, Any]
    ) -> TranslationCandidate:
        if isinstance(value, GlossaryMatch):
            return cls(
                concept_id=value.concept_id,
                label=value.label,
                categories=tuple(value.categories),
                concept_type=value.concept_type,
                matched_phrase=value.matched_phrase,
                score=value.score,
                genre_ids=tuple(value.genre_ids),
                baseline=value.baseline,
            )
        value = _mapping(value, "glossary match")
        fields = {
            "concept_id",
            "label",
            "categories",
            "concept_type",
            "matched_phrase",
            "score",
            "genre_ids",
            "baseline",
            "recognized",
            "authorized",
            "executable",
            "requires_context_binding",
            "receipt_ids",
        }
        required = fields - {"receipt_ids"}
        _exact_fields(value, allowed=fields, required=required, label="glossary match")
        if value.get("recognized") is not True or value.get("authorized") is not False \
                or value.get("executable") is not False \
                or value.get("requires_context_binding") is not True:
            raise WorldLexError("glossary match cannot authorize or execute a candidate")
        return cls(
            concept_id=_plain_id(value.get("concept_id"), "glossary concept_id"),
            label=str(value.get("label", "")),
            categories=_string_tuple(
                value.get("categories", ()), "glossary categories", allow_empty=False
            ),
            concept_type=_kind(value.get("concept_type"), "glossary concept_type"),
            matched_phrase=str(value.get("matched_phrase", "")),
            score=value.get("score"),
            genre_ids=_string_tuple(value.get("genre_ids", ()), "glossary genre_ids"),
            baseline=str(value.get("baseline", "")),
        )


@dataclass(frozen=True)
class TranslationResult:
    """One deterministic WorldLex recognition result for an existing context frame."""

    frame: ContextFrame
    memory_fingerprints: tuple[str, ...]
    candidates: tuple[TranslationCandidate, ...]
    abstained: bool
    abstention_reason: str
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.abstained, bool):
            raise WorldLexError("translation abstained must be a boolean")
        if not isinstance(self.abstention_reason, str):
            raise WorldLexError("translation abstention_reason must be a string")
        memories = tuple(sorted({_fingerprint(item, "memory fingerprint") for item in self.memory_fingerprints}))
        if not memories:
            raise WorldLexError("translation result requires at least one memory fingerprint")
        if memories != self.memory_fingerprints:
            raise WorldLexError("translation memory fingerprints must be unique and sorted")
        ordered = tuple(
            sorted(
                self.candidates,
                key=lambda item: (-item.score, item.concept_id, item.matched_phrase),
            )
        )
        if ordered != self.candidates:
            raise WorldLexError("translation candidates must use canonical ranking order")
        concept_ids = [candidate.concept_id for candidate in self.candidates]
        if len(concept_ids) != len(set(concept_ids)):
            raise WorldLexError("translation result contains duplicate concept ids")
        if self.candidates:
            if self.abstained or self.abstention_reason:
                raise WorldLexError("a translated candidate result cannot also abstain")
        elif not self.abstained or not self.abstention_reason.strip():
            raise WorldLexError("an empty translation result must give an abstention reason")
        expected = _canonical_payload_fingerprint(self._payload())
        if self.fingerprint != expected:
            raise WorldLexError("translation result fingerprint does not match its payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": TRANSLATION_RESULT_SCHEMA,
            "worldlex_version": WORLDLEX_CORE_VERSION,
            "frame": self.frame.as_dict(),
            "memory_fingerprints": list(self.memory_fingerprints),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    @classmethod
    def create(
        cls,
        *,
        frame: ContextFrame,
        memory_fingerprints: Iterable[str],
        candidates: Iterable[TranslationCandidate],
        abstention_reason: str = "",
    ) -> TranslationResult:
        if not isinstance(frame, ContextFrame):
            raise WorldLexError("translation result requires a validated ContextFrame")
        memories = tuple(
            sorted({_fingerprint(item, "memory fingerprint") for item in memory_fingerprints})
        )
        candidate_values = tuple(candidates)
        if any(not isinstance(item, TranslationCandidate) for item in candidate_values):
            raise WorldLexError("translation result accepts only TranslationCandidate values")
        ordered = tuple(
            sorted(
                candidate_values,
                key=lambda item: (-item.score, item.concept_id, item.matched_phrase),
            )
        )
        abstained = not ordered
        payload = {
            "schema": TRANSLATION_RESULT_SCHEMA,
            "worldlex_version": WORLDLEX_CORE_VERSION,
            "frame": frame.as_dict(),
            "memory_fingerprints": list(memories),
            "candidates": [candidate.as_dict() for candidate in ordered],
            "abstained": abstained,
            "abstention_reason": abstention_reason,
        }
        return cls(
            frame=frame,
            memory_fingerprints=memories,
            candidates=ordered,
            abstained=abstained,
            abstention_reason=abstention_reason,
            fingerprint=_canonical_payload_fingerprint(payload),
        )


def build_translation_result(
    *,
    frame: ContextFrame,
    matches: Iterable[GlossaryMatch | Mapping[str, Any]],
    memory_fingerprints: Iterable[str],
    abstention_reason: str = "",
) -> TranslationResult:
    """Convert existing glossary matches into recognition-only WorldLex evidence."""
    candidates = tuple(TranslationCandidate.from_glossary_match(match) for match in matches)
    return TranslationResult.create(
        frame=frame,
        memory_fingerprints=memory_fingerprints,
        candidates=candidates,
        abstention_reason=abstention_reason,
    )


def validate_translation_result(value: Mapping[str, Any]) -> TranslationResult:
    value = _mapping(value, "translation result")
    fields = {
        "schema",
        "worldlex_version",
        "frame",
        "memory_fingerprints",
        "candidates",
        "abstained",
        "abstention_reason",
        "fingerprint",
    }
    _exact_fields(value, allowed=fields, required=fields, label="translation result")
    if value["schema"] != TRANSLATION_RESULT_SCHEMA:
        raise WorldLexError("unsupported translation result schema")
    if value["worldlex_version"] != WORLDLEX_CORE_VERSION:
        raise WorldLexError("unsupported WorldLex core version")
    raw_candidates = value["candidates"]
    if not isinstance(raw_candidates, list):
        raise WorldLexError("translation candidates must be a list")
    memories = value["memory_fingerprints"]
    if not isinstance(memories, list):
        raise WorldLexError("translation memory_fingerprints must be a list")
    return TranslationResult(
        frame=ContextFrame.from_dict(_mapping(value["frame"], "context frame")),
        memory_fingerprints=tuple(memories),
        candidates=tuple(
            TranslationCandidate.from_dict(_mapping(item, "translation candidate"))
            for item in raw_candidates
        ),
        abstained=value["abstained"],
        abstention_reason=value["abstention_reason"],
        fingerprint=_fingerprint(value["fingerprint"], "translation result fingerprint"),
    )


@dataclass(frozen=True)
class DefinitionRef:
    """Exact immutable revision reference; it never means the referenced owner acquired it."""

    definition_schema: str
    definition_id: str
    revision: int
    fingerprint: str
    world_id: str
    kind: str
    owner: OwnerRef

    def __post_init__(self) -> None:
        _versioned_id(self.definition_schema, "definition schema")
        _plain_id(self.definition_id, "definition id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise WorldLexError("definition revision must be an exact positive integer")
        _fingerprint(self.fingerprint, "definition fingerprint")
        _world_id(self.world_id, "definition world_id")
        _kind(self.kind, "definition kind")
        if self.owner.world_id != self.world_id:
            raise WorldLexError("definition owner and definition must belong to the same world")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DEFINITION_REF_SCHEMA,
            "definition_schema": self.definition_schema,
            "definition_id": self.definition_id,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "world_id": self.world_id,
            "kind": self.kind,
            "owner": self.owner.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DefinitionRef:
        value = _mapping(value, "definition reference")
        fields = {
            "schema",
            "definition_schema",
            "definition_id",
            "revision",
            "fingerprint",
            "world_id",
            "kind",
            "owner",
        }
        _exact_fields(value, allowed=fields, required=fields, label="definition reference")
        if value["schema"] != DEFINITION_REF_SCHEMA:
            raise WorldLexError("unsupported definition reference schema")
        if "revision" not in value:
            raise WorldLexError("definition reference requires an exact revision")
        return cls(
            definition_schema=_versioned_id(value["definition_schema"], "definition schema"),
            definition_id=_plain_id(value["definition_id"], "definition id"),
            revision=value["revision"],
            fingerprint=_fingerprint(value["fingerprint"], "definition fingerprint"),
            world_id=_world_id(value["world_id"], "definition world_id"),
            kind=_kind(value["kind"], "definition kind"),
            owner=OwnerRef.from_dict(_mapping(value["owner"], "owner reference")),
        )


def validate_definition_ref(value: Mapping[str, Any]) -> DefinitionRef:
    """Validate shape and exact identity; store existence remains a downstream authority check."""
    return DefinitionRef.from_dict(value)


@dataclass(frozen=True)
class AdapterContract:
    """Versioned shape compatibility between definitions and a distinct reducer receipt."""

    adapter_id: str
    receipt_id: str
    definition_schemas: tuple[str, ...]
    definition_kinds: tuple[str, ...]
    consumed_fields: tuple[str, ...]
    concept_ids: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        _versioned_id(self.adapter_id, "adapter id")
        _versioned_id(self.receipt_id, "receipt id")
        if self.adapter_id == self.receipt_id:
            raise WorldLexError("adapter identity and reducer receipt identity must remain distinct")
        schemas = _string_tuple(
            self.definition_schemas,
            "adapter definition_schemas",
            validator="versioned",
            allow_empty=False,
        )
        kinds = _string_tuple(
            self.definition_kinds, "adapter definition_kinds", allow_empty=False
        )
        concepts = tuple(sorted({_plain_id(item, "adapter concept_id") for item in self.concept_ids}))
        fields = tuple(sorted({_field_name(item) for item in self.consumed_fields}))
        if not fields:
            raise WorldLexError("adapter consumed_fields must not be empty")
        if schemas != self.definition_schemas or kinds != self.definition_kinds \
                or concepts != self.concept_ids or fields != self.consumed_fields:
            raise WorldLexError("adapter contract lists must be unique and sorted")
        expected = _canonical_payload_fingerprint(self._payload())
        if self.fingerprint != expected:
            raise WorldLexError("adapter contract fingerprint does not match its payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": ADAPTER_CONTRACT_SCHEMA,
            "adapter_id": self.adapter_id,
            "receipt_id": self.receipt_id,
            "definition_schemas": list(self.definition_schemas),
            "definition_kinds": list(self.definition_kinds),
            "consumed_fields": list(self.consumed_fields),
            "concept_ids": list(self.concept_ids),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    @classmethod
    def create(
        cls,
        *,
        adapter_id: str,
        receipt_id: str,
        definition_schemas: Iterable[str],
        definition_kinds: Iterable[str],
        consumed_fields: Iterable[str],
        concept_ids: Iterable[str] = (),
    ) -> AdapterContract:
        adapter_id = _versioned_id(adapter_id, "adapter id")
        receipt_id = _versioned_id(receipt_id, "receipt id")
        if adapter_id == receipt_id:
            raise WorldLexError("adapter identity and reducer receipt identity must remain distinct")
        schemas = _string_tuple(
            definition_schemas,
            "adapter definition_schemas",
            validator="versioned",
            allow_empty=False,
        )
        kinds = _string_tuple(
            definition_kinds, "adapter definition_kinds", allow_empty=False
        )
        fields = tuple(sorted({_field_name(item) for item in consumed_fields}))
        if not fields:
            raise WorldLexError("adapter consumed_fields must not be empty")
        concepts = tuple(sorted({_plain_id(item, "adapter concept_id") for item in concept_ids}))
        payload = {
            "schema": ADAPTER_CONTRACT_SCHEMA,
            "adapter_id": adapter_id,
            "receipt_id": receipt_id,
            "definition_schemas": list(schemas),
            "definition_kinds": list(kinds),
            "consumed_fields": list(fields),
            "concept_ids": list(concepts),
        }
        return cls(
            adapter_id=adapter_id,
            receipt_id=receipt_id,
            definition_schemas=schemas,
            definition_kinds=kinds,
            consumed_fields=fields,
            concept_ids=concepts,
            fingerprint=_canonical_payload_fingerprint(payload),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterContract:
        value = _mapping(value, "adapter contract")
        fields = {
            "schema",
            "adapter_id",
            "receipt_id",
            "definition_schemas",
            "definition_kinds",
            "consumed_fields",
            "concept_ids",
            "fingerprint",
        }
        _exact_fields(value, allowed=fields, required=fields, label="adapter contract")
        if value["schema"] != ADAPTER_CONTRACT_SCHEMA:
            raise WorldLexError("unsupported adapter contract schema")
        schemas = value["definition_schemas"]
        kinds = value["definition_kinds"]
        fields_value = value["consumed_fields"]
        concepts = value["concept_ids"]
        for item, label in (
            (schemas, "adapter definition_schemas"),
            (kinds, "adapter definition_kinds"),
            (fields_value, "adapter consumed_fields"),
            (concepts, "adapter concept_ids"),
        ):
            if not isinstance(item, list):
                raise WorldLexError(f"{label} must be a list")
        return cls(
            adapter_id=_versioned_id(value["adapter_id"], "adapter id"),
            receipt_id=_versioned_id(value["receipt_id"], "receipt id"),
            definition_schemas=tuple(schemas),
            definition_kinds=tuple(kinds),
            consumed_fields=tuple(fields_value),
            concept_ids=tuple(concepts),
            fingerprint=_fingerprint(value["fingerprint"], "adapter contract fingerprint"),
        )


def _field_name(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not _FIELD_RE.fullmatch(value):
        raise WorldLexError("adapter consumed field names must be lowercase identifiers")
    return value


def validate_adapter_contract(value: Mapping[str, Any]) -> AdapterContract:
    """Validate a serialized adapter shape contract without admitting its receipt."""
    return AdapterContract.from_dict(value)


class AdapterRegistry:
    """Registry of shape contracts only; registration never admits a reducer receipt."""

    def __init__(self, contracts: Iterable[AdapterContract] = ()) -> None:
        self._contracts: dict[str, AdapterContract] = {}
        for contract in contracts:
            self.register(contract)

    def register(self, contract: AdapterContract) -> None:
        if not isinstance(contract, AdapterContract):
            raise WorldLexError("adapter registry accepts only validated AdapterContract values")
        if contract.adapter_id in self._contracts:
            raise WorldLexError(f"duplicate adapter id: {contract.adapter_id}")
        self._contracts[contract.adapter_id] = contract

    def require_binding(
        self,
        *,
        adapter_id: str,
        receipt_id: str,
        definition: DefinitionRef,
    ) -> AdapterContract:
        """Validate declared shape compatibility without claiming admission or executability."""
        if not isinstance(definition, DefinitionRef):
            raise WorldLexError("adapter binding requires an exact DefinitionRef")
        contract = self._contracts.get(adapter_id)
        if contract is None:
            raise WorldLexError(f"unknown adapter id: {adapter_id}")
        if receipt_id != contract.receipt_id:
            raise WorldLexError("receipt identity does not match the adapter contract")
        if adapter_id == receipt_id:
            raise WorldLexError("adapter identity and reducer receipt identity must remain distinct")
        if definition.definition_schema not in contract.definition_schemas:
            raise WorldLexError("definition schema is outside the adapter contract")
        if definition.kind not in contract.definition_kinds:
            raise WorldLexError("definition kind is outside the adapter contract")
        return contract

    def as_dict(self) -> dict[str, Any]:
        contracts = [self._contracts[key].as_dict() for key in sorted(self._contracts)]
        payload = {"schema": ADAPTER_REGISTRY_SCHEMA, "contracts": contracts}
        return {**payload, "fingerprint": _canonical_payload_fingerprint(payload)}


@dataclass(frozen=True)
class PoolMember:
    """One exact definition reference plus externally evidenced lifecycle state."""

    definition: DefinitionRef
    recognized: bool
    authorized: bool
    executable: bool
    assignment_ref: str | None = None
    eligibility_ref: str | None = None
    adapter_id: str | None = None
    receipt_id: str | None = None
    admission_ref: str | None = None
    classification: str = "recognized"

    def __post_init__(self) -> None:
        if self.recognized is not True:
            raise WorldLexError("a capability pool member must retain recognized meaning")
        if not isinstance(self.authorized, bool) or not isinstance(self.executable, bool):
            raise WorldLexError("pool authority flags must be booleans")
        if self.executable and not self.authorized:
            raise WorldLexError("an executable pool member must have external authorization evidence")
        _external_ref(self.assignment_ref, "pool assignment_ref")
        _external_ref(self.eligibility_ref, "pool eligibility_ref")
        _external_ref(self.admission_ref, "pool admission_ref")
        if (self.adapter_id is None) != (self.receipt_id is None):
            raise WorldLexError("pool adapter_id and receipt_id must appear together")
        if self.adapter_id is not None:
            _versioned_id(self.adapter_id, "pool adapter_id")
            _versioned_id(self.receipt_id, "pool receipt_id")
            if self.adapter_id == self.receipt_id:
                raise WorldLexError("pool adapter and reducer receipt identities must remain distinct")
        if self.admission_ref is not None and self.receipt_id is None:
            raise WorldLexError("pool admission_ref requires a distinct adapter and receipt")
        if self.classification not in POOL_CLASSIFICATIONS:
            raise WorldLexError("unsupported pool member classification")
        if self.executable != (self.classification == "executable"):
            raise WorldLexError("pool executable flag and classification must agree")

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.definition.definition_schema,
            self.definition.definition_id,
            self.definition.revision,
            self.definition.fingerprint,
            self.definition.world_id,
            self.definition.kind,
            self.definition.owner.kind,
            self.definition.owner.id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition.as_dict(),
            "recognized": self.recognized,
            "authorized": self.authorized,
            "executable": self.executable,
            "assignment_ref": self.assignment_ref,
            "eligibility_ref": self.eligibility_ref,
            "adapter_id": self.adapter_id,
            "receipt_id": self.receipt_id,
            "admission_ref": self.admission_ref,
            "classification": self.classification,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PoolMember:
        value = _mapping(value, "pool member")
        fields = {
            "definition",
            "recognized",
            "authorized",
            "executable",
            "assignment_ref",
            "eligibility_ref",
            "adapter_id",
            "receipt_id",
            "admission_ref",
            "classification",
        }
        _exact_fields(value, allowed=fields, required=fields, label="pool member")
        return cls(
            definition=DefinitionRef.from_dict(
                _mapping(value["definition"], "definition reference")
            ),
            recognized=value["recognized"],
            authorized=value["authorized"],
            executable=value["executable"],
            assignment_ref=_external_ref(value["assignment_ref"], "pool assignment_ref"),
            eligibility_ref=_external_ref(value["eligibility_ref"], "pool eligibility_ref"),
            adapter_id=value["adapter_id"],
            receipt_id=value["receipt_id"],
            admission_ref=_external_ref(value["admission_ref"], "pool admission_ref"),
            classification=str(value["classification"]),
        )


@dataclass(frozen=True)
class CapabilityPool:
    """Generic staged pool envelope; it carries no cardinality or selection policy."""

    pool_id: str
    stage: str
    world_id: str
    subject: SubjectRef
    members: tuple[PoolMember, ...]
    parent_fingerprint: str | None
    context_fingerprint: str | None
    fingerprint: str

    def __post_init__(self) -> None:
        _plain_id(self.pool_id, "pool id")
        _world_id(self.world_id, "pool world_id")
        if self.stage not in POOL_STAGES:
            raise WorldLexError("unsupported capability pool stage")
        if self.subject.world_id != self.world_id:
            raise WorldLexError("pool subject and pool must belong to the same world")
        if tuple(sorted(self.members, key=lambda member: member.identity)) != self.members:
            raise WorldLexError("pool members must use canonical definition order")
        identities = [member.identity for member in self.members]
        if len(identities) != len(set(identities)):
            raise WorldLexError("capability pool contains duplicate exact definition references")
        _optional_fingerprint(self.parent_fingerprint, "pool parent_fingerprint")
        _optional_fingerprint(self.context_fingerprint, "pool context_fingerprint")
        self._validate_stage()
        expected = _canonical_payload_fingerprint(self._payload())
        if self.fingerprint != expected:
            raise WorldLexError("pool fingerprint does not match its payload")

    def _validate_stage(self) -> None:
        if self.stage == "world_library":
            if self.parent_fingerprint is not None or self.context_fingerprint is not None:
                raise WorldLexError("world_library cannot have a parent or runtime context")
            for member in self.members:
                if member.authorized or member.executable or any(
                    value is not None
                    for value in (
                        member.assignment_ref,
                        member.eligibility_ref,
                        member.adapter_id,
                        member.receipt_id,
                        member.admission_ref,
                    )
                ):
                    raise WorldLexError("world_library records recognition without authority")
            return

        if self.parent_fingerprint is None:
            raise WorldLexError(f"{self.stage} requires an exact parent fingerprint")
        if self.stage in {"spawn_eligible", "runtime"}:
            if self.context_fingerprint is None:
                raise WorldLexError(f"{self.stage} requires an exact context fingerprint")
        elif self.context_fingerprint is not None:
            raise WorldLexError("assigned pool cannot claim runtime context eligibility")

        for member in self.members:
            if not member.authorized or member.assignment_ref is None:
                raise WorldLexError(f"{self.stage} requires external assignment_ref evidence")
            if self.stage == "assigned":
                if member.executable or any(
                    value is not None
                    for value in (
                        member.eligibility_ref,
                        member.adapter_id,
                        member.receipt_id,
                        member.admission_ref,
                    )
                ):
                    raise WorldLexError("assigned pool cannot claim eligibility or execution")
            elif self.stage == "spawn_eligible":
                if member.executable:
                    raise WorldLexError("spawn_eligible pool members cannot be executable")
                if member.eligibility_ref is None:
                    raise WorldLexError("spawn_eligible requires external eligibility_ref evidence")
                if member.admission_ref is not None:
                    raise WorldLexError("spawn_eligible cannot claim receipt admission")
            else:
                if member.eligibility_ref is None:
                    raise WorldLexError("runtime requires external eligibility_ref evidence")
                if member.executable and member.admission_ref is None:
                    raise WorldLexError("runtime executable member requires admission_ref evidence")
                if not member.executable and member.admission_ref is not None:
                    raise WorldLexError("non-executable runtime member cannot claim receipt admission")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_POOL_SCHEMA,
            "worldlex_version": WORLDLEX_CORE_VERSION,
            "pool_id": self.pool_id,
            "stage": self.stage,
            "world_id": self.world_id,
            "subject": self.subject.as_dict(),
            "members": [member.as_dict() for member in self.members],
            "parent_fingerprint": self.parent_fingerprint,
            "context_fingerprint": self.context_fingerprint,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    @classmethod
    def create(
        cls,
        *,
        pool_id: str,
        stage: str,
        world_id: str,
        subject: SubjectRef,
        members: Iterable[PoolMember],
        parent_fingerprint: str | None = None,
        context_fingerprint: str | None = None,
    ) -> CapabilityPool:
        if not isinstance(subject, SubjectRef):
            raise WorldLexError("capability pool requires a validated SubjectRef")
        member_values = tuple(members)
        if any(not isinstance(member, PoolMember) for member in member_values):
            raise WorldLexError("capability pool accepts only validated PoolMember values")
        ordered = tuple(sorted(member_values, key=lambda member: member.identity))
        payload = {
            "schema": CAPABILITY_POOL_SCHEMA,
            "worldlex_version": WORLDLEX_CORE_VERSION,
            "pool_id": pool_id,
            "stage": stage,
            "world_id": world_id,
            "subject": subject.as_dict(),
            "members": [member.as_dict() for member in ordered],
            "parent_fingerprint": parent_fingerprint,
            "context_fingerprint": context_fingerprint,
        }
        return cls(
            pool_id=pool_id,
            stage=stage,
            world_id=world_id,
            subject=subject,
            members=ordered,
            parent_fingerprint=parent_fingerprint,
            context_fingerprint=context_fingerprint,
            fingerprint=_canonical_payload_fingerprint(payload),
        )


def validate_pool(value: Mapping[str, Any]) -> CapabilityPool:
    value = _mapping(value, "capability pool")
    fields = {
        "schema",
        "worldlex_version",
        "pool_id",
        "stage",
        "world_id",
        "subject",
        "members",
        "parent_fingerprint",
        "context_fingerprint",
        "fingerprint",
    }
    _exact_fields(value, allowed=fields, required=fields, label="capability pool")
    if value["schema"] != CAPABILITY_POOL_SCHEMA:
        raise WorldLexError("unsupported capability pool schema")
    if value["worldlex_version"] != WORLDLEX_CORE_VERSION:
        raise WorldLexError("unsupported WorldLex core version")
    raw_members = value["members"]
    if not isinstance(raw_members, list):
        raise WorldLexError("capability pool members must be a list")
    return CapabilityPool(
        pool_id=_plain_id(value["pool_id"], "pool id"),
        stage=str(value["stage"]),
        world_id=_world_id(value["world_id"], "pool world_id"),
        subject=SubjectRef.from_dict(_mapping(value["subject"], "subject reference")),
        members=tuple(
            PoolMember.from_dict(_mapping(item, "pool member")) for item in raw_members
        ),
        parent_fingerprint=_optional_fingerprint(
            value["parent_fingerprint"], "pool parent_fingerprint"
        ),
        context_fingerprint=_optional_fingerprint(
            value["context_fingerprint"], "pool context_fingerprint"
        ),
        fingerprint=_fingerprint(value["fingerprint"], "pool fingerprint"),
    )


def validate_pool_transition(
    previous: CapabilityPool | Mapping[str, Any],
    current: CapabilityPool | Mapping[str, Any],
) -> CapabilityPool:
    """Validate externally produced lifecycle evidence without performing a promotion."""
    previous = previous if isinstance(previous, CapabilityPool) else validate_pool(previous)
    current = current if isinstance(current, CapabilityPool) else validate_pool(current)
    previous_index = POOL_STAGES.index(previous.stage)
    current_index = POOL_STAGES.index(current.stage)
    if current_index != previous_index + 1:
        raise WorldLexError("capability pools may advance exactly one stage at a time")
    if current.world_id != previous.world_id:
        raise WorldLexError("capability pool world cannot change across stages")
    if previous.stage != "world_library" and (
        current.pool_id != previous.pool_id or current.subject != previous.subject
    ):
        raise WorldLexError("assigned capability pool identity cannot change across stages")
    if current.parent_fingerprint != previous.fingerprint:
        raise WorldLexError("capability pool parent fingerprint does not match prior stage")
    previous_members = {member.identity: member for member in previous.members}
    current_members = {member.identity: member for member in current.members}
    if not set(current_members) <= set(previous_members):
        raise WorldLexError("capability pool stages cannot add an unlisted definition")
    for identity, next_member in current_members.items():
        prior_member = previous_members[identity]
        if previous.stage != "world_library" \
                and next_member.assignment_ref != prior_member.assignment_ref:
            raise WorldLexError("capability pool assignment evidence cannot change across stages")
        if previous.stage == "spawn_eligible":
            if next_member.eligibility_ref != prior_member.eligibility_ref:
                raise WorldLexError("capability pool eligibility evidence cannot change at runtime")
            if (next_member.adapter_id, next_member.receipt_id) != (
                prior_member.adapter_id,
                prior_member.receipt_id,
            ):
                raise WorldLexError("capability pool adapter/receipt binding cannot change at runtime")
    if previous.stage == "spawn_eligible" \
            and current.context_fingerprint != previous.context_fingerprint:
        raise WorldLexError("runtime pool must retain the exact eligibility context")
    return current
