"""Journal-ready WorldLex capability assignment materialization.

An assignment authorizes one exact stored definition revision for one canonical subject.  It bakes
all definition and optional adapter inputs needed by replay, but never claims reducer admission or
execution.  Replay validates the baked record and does not consult the definition repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .capability_glossary import DEFINITION_SCHEMA, GlossaryError, content_fingerprint
from .worldlex import (
    WORLDLEX_CORE_VERSION,
    AdapterContract,
    DefinitionRef,
    OwnerRef,
    SubjectRef,
    WorldLexError as CoreWorldLexError,
)
from .worldlex_store import WorldLexStore


ASSIGNMENT_SCHEMA = "capability-assignment/1"
ASSIGNMENT_IDENTITY_SCHEMA = "capability-assignment-identity/1"
ADAPTER_PAYLOAD_SCHEMA = "worldlex-adapter-payload/1"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASSIGNMENT_ID_RE = re.compile(r"assignment_[0-9a-f]{64}\Z")
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,159}\Z")
_OWNER_SCOPES = frozenset({"world", "actor", "enemy_blueprint"})


class AssignmentError(ValueError):
    """Base error for capability assignment materialization or replay validation."""


class AssignmentResolutionError(AssignmentError):
    """An exact stored definition reference cannot be resolved."""


class AssignmentScopeError(AssignmentError):
    """The canonical subject is outside the stored definition's ownership scope."""


class AssignmentAdapterError(AssignmentError):
    """An adapter contract or deterministic payload is incompatible or forged."""


class AssignmentValidationError(AssignmentError):
    """A serialized assignment violates the immutable assignment contract."""


def _canonical_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AssignmentValidationError(f"{label} must be an object with string fields")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise AssignmentValidationError(f"{label} must contain finite JSON data") from exc
    if not isinstance(result, dict):
        raise AssignmentValidationError(f"{label} must be a JSON object")
    return result


def _detached(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise AssignmentValidationError(f"{label} must be a sha256 content fingerprint")
    return value


def _acquisition_source(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE_RE.fullmatch(value):
        raise AssignmentValidationError(
            "acquisition_source must be a stable lowercase identifier"
        )
    return value


def _acquisition_turn(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssignmentValidationError("acquisition_turn must be a non-negative integer")
    return value


def _definition_from(value: DefinitionRef | Mapping[str, Any]) -> DefinitionRef:
    if isinstance(value, DefinitionRef):
        return value
    try:
        return DefinitionRef.from_dict(value)
    except (CoreWorldLexError, TypeError) as exc:
        raise AssignmentValidationError("definition must be an exact canonical reference") from exc


def _subject_from(value: SubjectRef | Mapping[str, Any]) -> SubjectRef:
    if isinstance(value, SubjectRef):
        return value
    try:
        return SubjectRef.from_dict(value)
    except (CoreWorldLexError, TypeError) as exc:
        raise AssignmentValidationError("subject must be a canonical subject reference") from exc


def _adapter_from(value: AdapterContract | Mapping[str, Any]) -> AdapterContract:
    if isinstance(value, AdapterContract):
        return value
    try:
        return AdapterContract.from_dict(value)
    except (CoreWorldLexError, TypeError) as exc:
        raise AssignmentAdapterError("adapter_contract is invalid or forged") from exc


def _validate_definition_snapshot(
    snapshot: Mapping[str, Any],
    definition: DefinitionRef,
) -> dict[str, Any]:
    snapshot = _canonical_object(snapshot, "definition_snapshot")
    required = {
        "schema",
        "definition_id",
        "revision",
        "fingerprint",
        "world_id",
        "kind",
        "owner_scope",
        "owner_id",
    }
    missing = sorted(required - snapshot.keys())
    if missing:
        raise AssignmentValidationError(f"definition_snapshot is missing fields: {missing}")
    if snapshot["schema"] != DEFINITION_SCHEMA:
        raise AssignmentValidationError(f"definition_snapshot schema must be {DEFINITION_SCHEMA}")

    fingerprint = _fingerprint(snapshot["fingerprint"], "definition fingerprint")
    fingerprint_payload = dict(snapshot)
    del fingerprint_payload["fingerprint"]
    try:
        recomputed = content_fingerprint(fingerprint_payload)
    except GlossaryError as exc:
        raise AssignmentValidationError("definition_snapshot fingerprint payload is invalid") from exc
    if fingerprint != recomputed:
        raise AssignmentValidationError("definition_snapshot fingerprint does not match its content")

    owner_scope = snapshot["owner_scope"]
    if owner_scope not in _OWNER_SCOPES:
        raise AssignmentValidationError("definition_snapshot has an unsupported owner_scope")
    try:
        stored_ref = DefinitionRef(
            definition_schema=snapshot["schema"],
            definition_id=snapshot["definition_id"],
            revision=snapshot["revision"],
            fingerprint=fingerprint,
            world_id=snapshot["world_id"],
            kind=snapshot["kind"],
            owner=OwnerRef(
                kind=owner_scope,
                id=snapshot["owner_id"],
                world_id=snapshot["world_id"],
            ),
        )
    except CoreWorldLexError as exc:
        raise AssignmentValidationError(
            "definition_snapshot does not contain a canonical exact definition reference"
        ) from exc
    if stored_ref != definition:
        raise AssignmentResolutionError(
            "definition reference does not exactly match the stored definition snapshot"
        )
    return snapshot


def _validate_scope(subject: SubjectRef, definition: DefinitionRef) -> None:
    if subject.world_id != definition.world_id:
        raise AssignmentScopeError("subject and definition must belong to the same world")
    owner = definition.owner
    if owner.world_id != definition.world_id:
        raise AssignmentScopeError("definition owner and definition must belong to the same world")
    if owner.kind == "world":
        if owner.id != definition.world_id:
            raise AssignmentScopeError("world-scoped definition owner must be its exact world_id")
        return
    if owner.kind in {"actor", "enemy_blueprint"}:
        if subject.kind != owner.kind or subject.id != owner.id:
            raise AssignmentScopeError(
                f"{owner.kind}-scoped definition must be assigned to its exact owner subject"
            )
        return
    raise AssignmentScopeError("definition has an unsupported assignment owner scope")


def _validate_adapter_compatibility(
    contract: AdapterContract,
    definition: DefinitionRef,
    definition_snapshot: Mapping[str, Any],
) -> None:
    if definition.definition_schema not in contract.definition_schemas:
        raise AssignmentAdapterError("definition schema is outside the adapter contract")
    if definition.kind not in contract.definition_kinds:
        raise AssignmentAdapterError("definition kind is outside the adapter contract")
    missing_fields = sorted(set(contract.consumed_fields) - set(definition_snapshot))
    if missing_fields:
        raise AssignmentAdapterError(
            f"adapter consumed fields are absent from the definition: {missing_fields}"
        )
    raw_concepts = definition_snapshot.get("concept_ids")
    if not isinstance(raw_concepts, list) or any(not isinstance(item, str) for item in raw_concepts):
        raise AssignmentAdapterError("definition concept_ids must be a list of strings")
    missing_concepts = sorted(set(contract.concept_ids) - set(raw_concepts))
    if missing_concepts:
        raise AssignmentAdapterError(
            f"adapter concept coverage is absent from the definition: {missing_concepts}"
        )


def _build_adapter_payload(
    contract: AdapterContract,
    definition: DefinitionRef,
    definition_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_adapter_compatibility(contract, definition, definition_snapshot)
    payload: dict[str, Any] = {
        "schema": ADAPTER_PAYLOAD_SCHEMA,
        "adapter_id": contract.adapter_id,
        "adapter_fingerprint": contract.fingerprint,
        "receipt_id": contract.receipt_id,
        "definition_fingerprint": definition.fingerprint,
        "consumed_fields": {
            name: _detached({"value": definition_snapshot[name]})["value"]
            for name in contract.consumed_fields
        },
        "concept_ids": list(contract.concept_ids),
        "receipt_admitted": False,
        "executable": False,
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def _validate_adapter_payload(
    payload: Mapping[str, Any],
    contract: AdapterContract,
    definition: DefinitionRef,
    definition_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _canonical_object(payload, "adapter_payload")
    fields = {
        "schema",
        "adapter_id",
        "adapter_fingerprint",
        "receipt_id",
        "definition_fingerprint",
        "consumed_fields",
        "concept_ids",
        "receipt_admitted",
        "executable",
        "fingerprint",
    }
    if set(payload) != fields:
        raise AssignmentAdapterError("adapter_payload fields do not match its versioned schema")
    expected = _build_adapter_payload(contract, definition, definition_snapshot)
    if payload != expected:
        raise AssignmentAdapterError(
            "adapter_payload does not match the contract and baked definition snapshot"
        )
    return payload


@dataclass(frozen=True)
class CapabilityAssignment:
    """One immutable authorization record suitable for direct journal embedding."""

    world_id: str
    subject: SubjectRef
    definition: DefinitionRef
    acquisition_source: str
    acquisition_turn: int
    _definition_snapshot: dict[str, Any] = field(repr=False)
    _adapter_contract: AdapterContract | None = field(default=None, repr=False)
    _adapter_payload: dict[str, Any] | None = field(default=None, repr=False)
    recognized: bool = True
    authorized: bool = True
    executable: bool = False
    assignment_id: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.subject, SubjectRef) or not isinstance(self.definition, DefinitionRef):
            raise AssignmentValidationError("assignment requires canonical subject and definition refs")
        if self.world_id != self.subject.world_id or self.world_id != self.definition.world_id:
            raise AssignmentScopeError(
                "assignment, subject, and definition must belong to the same world"
            )
        source = _acquisition_source(self.acquisition_source)
        turn = _acquisition_turn(self.acquisition_turn)
        snapshot = _validate_definition_snapshot(self._definition_snapshot, self.definition)
        object.__setattr__(self, "_definition_snapshot", snapshot)
        _validate_scope(self.subject, self.definition)

        contract = self._adapter_contract
        adapter_payload = self._adapter_payload
        if contract is None:
            if adapter_payload is not None:
                raise AssignmentAdapterError(
                    "adapter_payload cannot appear without an adapter_contract"
                )
        else:
            if not isinstance(contract, AdapterContract):
                raise AssignmentAdapterError("adapter_contract must already be validated")
            if adapter_payload is None:
                raise AssignmentAdapterError(
                    "adapter_contract requires its deterministic adapter_payload"
                )
            adapter_payload = _validate_adapter_payload(
                adapter_payload, contract, self.definition, snapshot
            )
            object.__setattr__(self, "_adapter_payload", adapter_payload)

        if self.recognized is not True or self.authorized is not True or self.executable is not False:
            raise AssignmentValidationError(
                "assignment authorizes recognized meaning but never claims execution"
            )
        object.__setattr__(self, "acquisition_source", source)
        object.__setattr__(self, "acquisition_turn", turn)

        expected_id = self._expected_assignment_id()
        if not self.assignment_id:
            object.__setattr__(self, "assignment_id", expected_id)
        elif not _ASSIGNMENT_ID_RE.fullmatch(self.assignment_id) or self.assignment_id != expected_id:
            raise AssignmentValidationError("assignment_id does not match its deterministic identity")

        expected_fingerprint = content_fingerprint(self._payload())
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        elif (
            not _SHA256_RE.fullmatch(self.fingerprint)
            or self.fingerprint != expected_fingerprint
        ):
            raise AssignmentValidationError(
                "assignment fingerprint does not match its complete immutable payload"
            )

    @property
    def definition_snapshot(self) -> dict[str, Any]:
        return _detached(self._definition_snapshot)

    @property
    def adapter_contract_snapshot(self) -> dict[str, Any] | None:
        return None if self._adapter_contract is None else self._adapter_contract.as_dict()

    @property
    def adapter_payload(self) -> dict[str, Any] | None:
        return None if self._adapter_payload is None else _detached(self._adapter_payload)

    def _body(self) -> dict[str, Any]:
        return {
            "schema": ASSIGNMENT_SCHEMA,
            "worldlex_version": WORLDLEX_CORE_VERSION,
            "world_id": self.world_id,
            "subject": self.subject.as_dict(),
            "definition": self.definition.as_dict(),
            "acquisition_source": self.acquisition_source,
            "acquisition_turn": self.acquisition_turn,
            "definition_snapshot": self.definition_snapshot,
            "adapter_contract": self.adapter_contract_snapshot,
            "adapter_payload": self.adapter_payload,
            "recognized": True,
            "authorized": True,
            "executable": False,
        }

    def _expected_assignment_id(self) -> str:
        identity = {
            "schema": ASSIGNMENT_IDENTITY_SCHEMA,
            "assignment": self._body(),
        }
        return "assignment_" + content_fingerprint(identity).split(":", 1)[1]

    def _payload(self) -> dict[str, Any]:
        return {**self._body(), "assignment_id": self.assignment_id}

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}


def materialize_assignment(
    repository: WorldLexStore,
    *,
    definition: DefinitionRef | Mapping[str, Any],
    subject: SubjectRef | Mapping[str, Any],
    acquisition_source: str,
    acquisition_turn: int,
    adapter_contract: AdapterContract | Mapping[str, Any] | None = None,
) -> CapabilityAssignment:
    """Resolve and bake one exact stored revision for journal admission.

    This cold-path function always queries by the supplied revision.  ``validate_assignment`` is the
    replay path and intentionally requires no repository.
    """
    if not isinstance(repository, WorldLexStore):
        raise TypeError("repository must be a WorldLexStore")
    definition = _definition_from(definition)
    subject = _subject_from(subject)

    stored = repository.get_definition(
        definition.world_id,
        definition.definition_id,
        definition.revision,
    )
    if stored is None:
        fingerprint_match = repository.get_by_fingerprint(definition.fingerprint)
        if fingerprint_match is not None:
            if fingerprint_match.get("world_id") != definition.world_id:
                raise AssignmentScopeError(
                    "definition fingerprint belongs to a different world"
                )
            raise AssignmentResolutionError(
                "definition fingerprint belongs to a different id or revision"
            )
        raise AssignmentResolutionError("exact stored definition revision does not exist")
    if stored.get("fingerprint") != definition.fingerprint:
        raise AssignmentResolutionError(
            "stored definition revision does not match the requested fingerprint"
        )
    stored = _validate_definition_snapshot(stored, definition)
    _validate_scope(subject, definition)

    contract = None if adapter_contract is None else _adapter_from(adapter_contract)
    adapter_payload = (
        None
        if contract is None
        else _build_adapter_payload(contract, definition, stored)
    )
    return CapabilityAssignment(
        world_id=definition.world_id,
        subject=subject,
        definition=definition,
        acquisition_source=acquisition_source,
        acquisition_turn=acquisition_turn,
        _definition_snapshot=stored,
        _adapter_contract=contract,
        _adapter_payload=adapter_payload,
    )


def validate_assignment(value: Mapping[str, Any]) -> CapabilityAssignment:
    """Validate a baked assignment for replay without reading WorldLexStore."""
    value = _canonical_object(value, "capability assignment")
    fields = {
        "schema",
        "worldlex_version",
        "assignment_id",
        "world_id",
        "subject",
        "definition",
        "acquisition_source",
        "acquisition_turn",
        "definition_snapshot",
        "adapter_contract",
        "adapter_payload",
        "recognized",
        "authorized",
        "executable",
        "fingerprint",
    }
    if set(value) != fields:
        raise AssignmentValidationError(
            "capability assignment fields do not match capability-assignment/1"
        )
    if value["schema"] != ASSIGNMENT_SCHEMA:
        raise AssignmentValidationError("unsupported capability assignment schema")
    if value["worldlex_version"] != WORLDLEX_CORE_VERSION:
        raise AssignmentValidationError("unsupported WorldLex core version")

    definition = _definition_from(value["definition"])
    subject = _subject_from(value["subject"])
    raw_contract = value["adapter_contract"]
    contract = None if raw_contract is None else _adapter_from(raw_contract)
    raw_payload = value["adapter_payload"]
    payload = None if raw_payload is None else _canonical_object(raw_payload, "adapter_payload")
    return CapabilityAssignment(
        world_id=value["world_id"],
        subject=subject,
        definition=definition,
        acquisition_source=value["acquisition_source"],
        acquisition_turn=value["acquisition_turn"],
        _definition_snapshot=_canonical_object(
            value["definition_snapshot"], "definition_snapshot"
        ),
        _adapter_contract=contract,
        _adapter_payload=payload,
        recognized=value["recognized"],
        authorized=value["authorized"],
        executable=value["executable"],
        assignment_id=value["assignment_id"],
        fingerprint=_fingerprint(value["fingerprint"], "assignment fingerprint"),
    )


__all__ = [
    "ADAPTER_PAYLOAD_SCHEMA",
    "ASSIGNMENT_SCHEMA",
    "AssignmentAdapterError",
    "AssignmentError",
    "AssignmentResolutionError",
    "AssignmentScopeError",
    "AssignmentValidationError",
    "CapabilityAssignment",
    "materialize_assignment",
    "validate_assignment",
]
