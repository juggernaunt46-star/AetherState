"""Out-of-band authority for privileged semantic ingress.

Text can describe an operation, but text is never permission to perform it.  This module keeps
those two concerns separate: trusted code issues a sealed, scope-bound receipt and a parser must
validate that live receipt before interpreting privileged syntax.  Serialized receipts are useful
for audit and replay evidence, but can only become live authority through the explicit trusted
rehydration path.

The first integration is deliberately narrow: the finite battle-cohort declaration already
understood by :mod:`aetherstate.tier0`.  The wrapper below is the authority boundary; the existing
Tier-0 parser remains a grammar implementation and does not mint authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Sequence

from .capability_glossary import content_fingerprint, raw_fingerprint


SEMANTIC_INGRESS_SCHEMA = "semantic-ingress-authority/1"
SEMANTIC_DECLARATION_SCHEMA = "authorized-semantic-declaration/1"
CAPABILITY_INGRESS_SCHEMA = "semantic-capability-ingress-authority/1"
CODE_AUTHORITY_VERSION = "semantic-ingress-code-authority/1"

COHORT_GRAMMAR_ID = "aetherstate.combat.cohort"
COHORT_GRAMMAR_VERSION = "1"
COHORT_DECLARATION_ID = "battle-cohort.strict/1"
OP_BATTLE_COHORT_DECLARATION = "battle_cohort_declaration"

CAPABILITY_AUTHORITY_GRAMMAR_ID = "worldlex.capability.authority"
CAPABILITY_AUTHORITY_GRAMMAR_VERSION = "1"
CAPABILITY_ADMISSION_DECLARATION_ID = "worldlex.capability-admission/1"
OP_CAPABILITY_ADMISSION = "capability_admission"


class SemanticIngressError(ValueError):
    """Raised when semantic syntax lacks an exact, live authority binding."""


_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_VERSIONED_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,223}/[1-9][0-9]*$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORLD_ID_RE = re.compile(r"^world_[0-9a-f]{32}$")

_CHANNELS = frozenset(
    {
        "trusted_control",
        "creator",
        "genesis",
        "player_input",
        "narrator_candidate",
        "extraction",
        "rule_internal",
        "prompt_display",
        "hud_display",
    }
)
_AUTHORING_PHASES = frozenset(
    {
        "bootstrap",
        "pre_settlement",
        "candidate_proposal",
        "gate_admission",
        "cold_extraction",
        "replay",
        "display_only",
    }
)


def _require_stable_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID_RE.fullmatch(value):
        raise SemanticIngressError(f"{label} must be a stable identity")
    return value


def _require_kind(value: object, label: str) -> str:
    if not isinstance(value, str) or not _KIND_RE.fullmatch(value):
        raise SemanticIngressError(f"{label} must be a canonical kind")
    return value


def _require_fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise SemanticIngressError(f"{label} must be an exact sha256 fingerprint")
    return value


def _require_versioned_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _VERSIONED_ID_RE.fullmatch(value):
        raise SemanticIngressError(f"{label} must be a versioned identity")
    return value


def _source_bytes(source: bytes | str) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        return source.encode("utf-8")
    raise SemanticIngressError("semantic source must be exact bytes or text")


@dataclass(frozen=True, slots=True)
class IngressScope:
    """The exact live occurrence to which an ingress receipt belongs."""

    session_id: str
    branch_id: str
    turn_index: int
    attempt_id: str
    source_start: int
    source_end: int

    def __post_init__(self) -> None:
        _require_stable_id(self.session_id, "session_id")
        _require_stable_id(self.branch_id, "branch_id")
        _require_stable_id(self.attempt_id, "attempt_id")
        if isinstance(self.turn_index, bool) or not isinstance(self.turn_index, int) or self.turn_index < 0:
            raise SemanticIngressError("turn_index must be a non-negative integer")
        if (
            isinstance(self.source_start, bool)
            or not isinstance(self.source_start, int)
            or self.source_start < 0
        ):
            raise SemanticIngressError("source_start must be a non-negative byte offset")
        if (
            isinstance(self.source_end, bool)
            or not isinstance(self.source_end, int)
            or self.source_end <= self.source_start
        ):
            raise SemanticIngressError("source_end must follow source_start")

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "branch_id": self.branch_id,
            "turn_index": self.turn_index,
            "attempt_id": self.attempt_id,
            "source_span": [self.source_start, self.source_end],
        }


@dataclass(frozen=True, slots=True)
class SemanticIngressContext:
    """Out-of-band producer, channel, authoring phase, and occurrence binding."""

    issuer_kind: str
    issuer_id: str
    channel: str
    authoring_phase: str
    scope: IngressScope

    def __post_init__(self) -> None:
        _require_kind(self.issuer_kind, "issuer_kind")
        _require_stable_id(self.issuer_id, "issuer_id")
        if self.channel not in _CHANNELS:
            raise SemanticIngressError("channel is not a recognized semantic ingress channel")
        if self.authoring_phase not in _AUTHORING_PHASES:
            raise SemanticIngressError("authoring_phase is not recognized")
        if type(self.scope) is not IngressScope:
            raise SemanticIngressError("scope must be a typed IngressScope")

    def as_dict(self) -> dict[str, Any]:
        return {
            "issuer_kind": self.issuer_kind,
            "issuer_id": self.issuer_id,
            "channel": self.channel,
            "authoring_phase": self.authoring_phase,
            **self.scope.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ResourceAuthorityRef:
    """Exact resource receipt identity admitted by a capability settlement."""

    receipt_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        _require_stable_id(self.receipt_id, "resource receipt_id")
        _require_fingerprint(self.fingerprint, "resource receipt fingerprint")

    def as_dict(self) -> dict[str, str]:
        return {"receipt_id": self.receipt_id, "fingerprint": self.fingerprint}

    @classmethod
    def _from_dict(cls, value: object) -> ResourceAuthorityRef:
        if not isinstance(value, Mapping) or set(value) != {"receipt_id", "fingerprint"}:
            raise SemanticIngressError("resource receipt evidence has an invalid shape")
        return cls(receipt_id=value["receipt_id"], fingerprint=value["fingerprint"])


@dataclass(frozen=True, slots=True)
class CapabilityIngressAuthority:
    """All exact identities required to admit one capability operation family."""

    world_id: str
    world_fingerprint: str
    definition_id: str
    definition_revision: int
    definition_fingerprint: str
    owner_kind: str
    owner_id: str
    owner_world_id: str
    owner_fingerprint: str
    subject_kind: str
    subject_id: str
    subject_world_id: str
    subject_fingerprint: str
    assignment_id: str
    assignment_fingerprint: str
    adapter_id: str
    adapter_fingerprint: str
    resource_pool_id: str
    resource_pool_fingerprint: str
    resource_receipts: tuple[ResourceAuthorityRef, ...]
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.world_id, str) or not _WORLD_ID_RE.fullmatch(self.world_id):
            raise SemanticIngressError("world_id must be an exact stable world identity")
        _require_fingerprint(self.world_fingerprint, "world_fingerprint")
        _require_stable_id(self.definition_id, "definition_id")
        if (
            isinstance(self.definition_revision, bool)
            or not isinstance(self.definition_revision, int)
            or self.definition_revision < 1
        ):
            raise SemanticIngressError("definition_revision must be a positive integer")
        _require_fingerprint(self.definition_fingerprint, "definition_fingerprint")
        _require_kind(self.owner_kind, "owner_kind")
        _require_stable_id(self.owner_id, "owner_id")
        _require_kind(self.subject_kind, "subject_kind")
        _require_stable_id(self.subject_id, "subject_id")
        if self.owner_world_id != self.world_id or self.subject_world_id != self.world_id:
            raise SemanticIngressError("owner and subject must belong to the same exact world")
        _require_fingerprint(self.owner_fingerprint, "owner_fingerprint")
        _require_fingerprint(self.subject_fingerprint, "subject_fingerprint")
        _require_stable_id(self.assignment_id, "assignment_id")
        _require_fingerprint(self.assignment_fingerprint, "assignment_fingerprint")
        _require_versioned_id(self.adapter_id, "adapter_id")
        _require_fingerprint(self.adapter_fingerprint, "adapter_fingerprint")
        _require_stable_id(self.resource_pool_id, "resource_pool_id")
        _require_fingerprint(self.resource_pool_fingerprint, "resource_pool_fingerprint")
        if not isinstance(self.resource_receipts, tuple) or not self.resource_receipts:
            raise SemanticIngressError("resource_receipts must contain typed receipt identities")
        if any(type(ref) is not ResourceAuthorityRef for ref in self.resource_receipts):
            raise SemanticIngressError("resource_receipts must contain typed receipt identities")
        receipt_ids = tuple(ref.receipt_id for ref in self.resource_receipts)
        if len(set(receipt_ids)) != len(receipt_ids) or receipt_ids != tuple(sorted(receipt_ids)):
            raise SemanticIngressError("resource receipt identities must be unique and canonical")

        expected = content_fingerprint(self._body())
        if self.fingerprint and self.fingerprint != expected:
            raise SemanticIngressError("capability authority fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)

    def _body(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_INGRESS_SCHEMA,
            "world_id": self.world_id,
            "world_fingerprint": self.world_fingerprint,
            "definition_id": self.definition_id,
            "definition_revision": self.definition_revision,
            "definition_fingerprint": self.definition_fingerprint,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "owner_world_id": self.owner_world_id,
            "owner_fingerprint": self.owner_fingerprint,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "subject_world_id": self.subject_world_id,
            "subject_fingerprint": self.subject_fingerprint,
            "assignment_id": self.assignment_id,
            "assignment_fingerprint": self.assignment_fingerprint,
            "adapter_id": self.adapter_id,
            "adapter_fingerprint": self.adapter_fingerprint,
            "resource_pool_id": self.resource_pool_id,
            "resource_pool_fingerprint": self.resource_pool_fingerprint,
            "resource_receipts": [ref.as_dict() for ref in self.resource_receipts],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._body(), "fingerprint": self.fingerprint}

    @classmethod
    def _from_dict(cls, value: object) -> CapabilityIngressAuthority:
        body_keys = {
            "schema",
            "world_id",
            "world_fingerprint",
            "definition_id",
            "definition_revision",
            "definition_fingerprint",
            "owner_kind",
            "owner_id",
            "owner_world_id",
            "owner_fingerprint",
            "subject_kind",
            "subject_id",
            "subject_world_id",
            "subject_fingerprint",
            "assignment_id",
            "assignment_fingerprint",
            "adapter_id",
            "adapter_fingerprint",
            "resource_pool_id",
            "resource_pool_fingerprint",
            "resource_receipts",
            "fingerprint",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != body_keys
            or value.get("schema") != CAPABILITY_INGRESS_SCHEMA
        ):
            raise SemanticIngressError("capability authority evidence has an invalid shape")
        refs = value["resource_receipts"]
        if not isinstance(refs, list):
            raise SemanticIngressError("capability resource receipt evidence must be a list")
        return cls(
            world_id=value["world_id"],
            world_fingerprint=value["world_fingerprint"],
            definition_id=value["definition_id"],
            definition_revision=value["definition_revision"],
            definition_fingerprint=value["definition_fingerprint"],
            owner_kind=value["owner_kind"],
            owner_id=value["owner_id"],
            owner_world_id=value["owner_world_id"],
            owner_fingerprint=value["owner_fingerprint"],
            subject_kind=value["subject_kind"],
            subject_id=value["subject_id"],
            subject_world_id=value["subject_world_id"],
            subject_fingerprint=value["subject_fingerprint"],
            assignment_id=value["assignment_id"],
            assignment_fingerprint=value["assignment_fingerprint"],
            adapter_id=value["adapter_id"],
            adapter_fingerprint=value["adapter_fingerprint"],
            resource_pool_id=value["resource_pool_id"],
            resource_pool_fingerprint=value["resource_pool_fingerprint"],
            resource_receipts=tuple(ResourceAuthorityRef._from_dict(ref) for ref in refs),
            fingerprint=value["fingerprint"],
        )


@dataclass(frozen=True, slots=True)
class _RoutePolicy:
    authorization_basis: str
    operation_families: frozenset[str]


_COHORT_ROUTE_SUFFIX = (
    COHORT_GRAMMAR_ID,
    COHORT_GRAMMAR_VERSION,
    COHORT_DECLARATION_ID,
)
_CAPABILITY_ROUTE_SUFFIX = (
    CAPABILITY_AUTHORITY_GRAMMAR_ID,
    CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
    CAPABILITY_ADMISSION_DECLARATION_ID,
)
_ROUTES: dict[tuple[str, str, str, str, str, str], _RoutePolicy] = {
    ("control", "trusted_control", "bootstrap", *_COHORT_ROUTE_SUFFIX): _RoutePolicy(
        "code-issued:trusted-control/bootstrap",
        frozenset({OP_BATTLE_COHORT_DECLARATION}),
    ),
    ("creator", "creator", "bootstrap", *_COHORT_ROUTE_SUFFIX): _RoutePolicy(
        "code-issued:creator/bootstrap",
        frozenset({OP_BATTLE_COHORT_DECLARATION}),
    ),
    ("genesis", "genesis", "bootstrap", *_COHORT_ROUTE_SUFFIX): _RoutePolicy(
        "code-issued:genesis/bootstrap",
        frozenset({OP_BATTLE_COHORT_DECLARATION}),
    ),
    ("narrator", "narrator_candidate", "candidate_proposal", *_COHORT_ROUTE_SUFFIX): _RoutePolicy(
        "code-issued:narrator/candidate-proposal",
        frozenset({OP_BATTLE_COHORT_DECLARATION}),
    ),
    ("control", "trusted_control", "gate_admission", *_CAPABILITY_ROUTE_SUFFIX): _RoutePolicy(
        "code-issued:worldlex/capability-admission",
        frozenset({OP_CAPABILITY_ADMISSION}),
    ),
}


def _route_policy(
    context: SemanticIngressContext,
    grammar_id: str,
    grammar_version: str,
    declaration_id: str,
    operation_families: Sequence[str],
) -> tuple[_RoutePolicy, tuple[str, ...]]:
    if not isinstance(grammar_id, str) or not _STABLE_ID_RE.fullmatch(grammar_id):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    if not isinstance(grammar_version, str) or not _VERSION_RE.fullmatch(grammar_version):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    if not isinstance(declaration_id, str) or not _VERSIONED_ID_RE.fullmatch(declaration_id):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    if isinstance(operation_families, (str, bytes)) or not isinstance(operation_families, Sequence):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    families = tuple(operation_families)
    if not families or any(not isinstance(item, str) or not _KIND_RE.fullmatch(item) for item in families):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    if len(set(families)) != len(families):
        raise SemanticIngressError("semantic ingress operation families must be unique")
    canonical = tuple(sorted(families))
    key = (
        context.issuer_kind,
        context.channel,
        context.authoring_phase,
        grammar_id,
        grammar_version,
        declaration_id,
    )
    policy = _ROUTES.get(key)
    if policy is None or not set(canonical).issubset(policy.operation_families):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    return policy, canonical


_AUTHORITY_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class SemanticIngressAuthority:
    """A sealed, live receipt.  Direct construction is intentionally unavailable."""

    context: SemanticIngressContext
    grammar_id: str
    grammar_version: str
    declaration_id: str
    allowed_operation_families: tuple[str, ...]
    source_fingerprint: str
    source_span_fingerprint: str
    authorization_basis: str
    capability_authority: CapabilityIngressAuthority | None
    receipt_id: str
    fingerprint: str
    _seal: object

    def __init__(
        self,
        *,
        context: SemanticIngressContext,
        grammar_id: str,
        grammar_version: str,
        declaration_id: str,
        allowed_operation_families: tuple[str, ...],
        source_fingerprint: str,
        source_span_fingerprint: str,
        authorization_basis: str,
        capability_authority: CapabilityIngressAuthority | None,
        receipt_id: str,
        fingerprint: str,
        _seal: object,
    ) -> None:
        if _seal is not _AUTHORITY_SEAL:
            raise TypeError("SemanticIngressAuthority can only be issued by trusted code")
        for name, value in (
            ("context", context),
            ("grammar_id", grammar_id),
            ("grammar_version", grammar_version),
            ("declaration_id", declaration_id),
            ("allowed_operation_families", allowed_operation_families),
            ("source_fingerprint", source_fingerprint),
            ("source_span_fingerprint", source_span_fingerprint),
            ("authorization_basis", authorization_basis),
            ("capability_authority", capability_authority),
            ("receipt_id", receipt_id),
            ("fingerprint", fingerprint),
            ("_seal", _seal),
        ):
            object.__setattr__(self, name, value)

    def _body(self) -> dict[str, Any]:
        return {
            "schema": SEMANTIC_INGRESS_SCHEMA,
            "code_authority_version": CODE_AUTHORITY_VERSION,
            **self.context.as_dict(),
            "grammar_id": self.grammar_id,
            "grammar_version": self.grammar_version,
            "declaration_id": self.declaration_id,
            "allowed_operation_families": list(self.allowed_operation_families),
            "source_fingerprint": self.source_fingerprint,
            "source_span_fingerprint": self.source_span_fingerprint,
            "authorization_basis": self.authorization_basis,
            "capability_authority": (
                self.capability_authority.as_dict() if self.capability_authority else None
            ),
        }

    def as_evidence(self) -> dict[str, Any]:
        """Return replay-safe JSON evidence, not live authority."""

        return {
            **self._body(),
            "receipt_id": self.receipt_id,
            "fingerprint": self.fingerprint,
        }


def _make_authority(
    *,
    context: SemanticIngressContext,
    grammar_id: str,
    grammar_version: str,
    declaration_id: str,
    allowed_operation_families: tuple[str, ...],
    source_fingerprint: str,
    source_span_fingerprint: str,
    capability_authority: CapabilityIngressAuthority | None,
    expected_authorization_basis: object = None,
    expected_receipt_id: object = None,
    expected_fingerprint: object = None,
) -> SemanticIngressAuthority:
    policy, families = _route_policy(
        context,
        grammar_id,
        grammar_version,
        declaration_id,
        allowed_operation_families,
    )
    _require_fingerprint(source_fingerprint, "source_fingerprint")
    _require_fingerprint(source_span_fingerprint, "source_span_fingerprint")
    if OP_CAPABILITY_ADMISSION in families:
        if type(capability_authority) is not CapabilityIngressAuthority:
            raise SemanticIngressError("capability admission requires full capability authority")
    elif capability_authority is not None:
        raise SemanticIngressError("capability authority cannot lend authority to this declaration")

    provisional = SemanticIngressAuthority(
        context=context,
        grammar_id=grammar_id,
        grammar_version=grammar_version,
        declaration_id=declaration_id,
        allowed_operation_families=families,
        source_fingerprint=source_fingerprint,
        source_span_fingerprint=source_span_fingerprint,
        authorization_basis=policy.authorization_basis,
        capability_authority=capability_authority,
        receipt_id="pending",
        fingerprint="pending",
        _seal=_AUTHORITY_SEAL,
    )
    body = provisional._body()
    receipt_id = "ingress_" + content_fingerprint(body).removeprefix("sha256:")
    fingerprint = content_fingerprint({**body, "receipt_id": receipt_id})
    if (
        expected_authorization_basis is not None
        and expected_authorization_basis != policy.authorization_basis
    ):
        raise SemanticIngressError("semantic ingress receipt identity or fingerprint mismatch")
    if expected_receipt_id is not None and expected_receipt_id != receipt_id:
        raise SemanticIngressError("semantic ingress receipt identity or fingerprint mismatch")
    if expected_fingerprint is not None and expected_fingerprint != fingerprint:
        raise SemanticIngressError("semantic ingress receipt identity or fingerprint mismatch")
    return SemanticIngressAuthority(
        context=context,
        grammar_id=grammar_id,
        grammar_version=grammar_version,
        declaration_id=declaration_id,
        allowed_operation_families=families,
        source_fingerprint=source_fingerprint,
        source_span_fingerprint=source_span_fingerprint,
        authorization_basis=policy.authorization_basis,
        capability_authority=capability_authority,
        receipt_id=receipt_id,
        fingerprint=fingerprint,
        _seal=_AUTHORITY_SEAL,
    )


def _validate_scope_against_source(scope: IngressScope, source: bytes) -> None:
    if scope.source_end > len(source):
        raise SemanticIngressError("semantic ingress source span exceeds the exact source bytes")


def issue_semantic_ingress_authority(
    source: bytes | str,
    *,
    context: SemanticIngressContext,
    grammar_id: str,
    grammar_version: str,
    declaration_id: str,
    operation_families: Sequence[str],
    capability_authority: CapabilityIngressAuthority | None = None,
) -> SemanticIngressAuthority:
    """Issue a live receipt from trusted out-of-band context.

    Callers cannot supply an authorization basis.  It is derived from the exact, closed route
    table above, so a producer, channel, or phase change fails closed.
    """

    if type(context) is not SemanticIngressContext:
        raise SemanticIngressError("semantic ingress requires a typed out-of-band context")
    if isinstance(operation_families, (str, bytes)) or not isinstance(operation_families, Sequence):
        raise SemanticIngressError("semantic ingress is not an authorized ingress route")
    payload = _source_bytes(source)
    _validate_scope_against_source(context.scope, payload)
    span = payload[context.scope.source_start : context.scope.source_end]
    return _make_authority(
        context=context,
        grammar_id=grammar_id,
        grammar_version=grammar_version,
        declaration_id=declaration_id,
        allowed_operation_families=tuple(operation_families),
        source_fingerprint=raw_fingerprint(payload),
        source_span_fingerprint=raw_fingerprint(span),
        capability_authority=capability_authority,
    )


def _assert_live_authority(authority: object) -> SemanticIngressAuthority:
    if (
        type(authority) is not SemanticIngressAuthority
        or getattr(authority, "_seal", None) is not _AUTHORITY_SEAL
    ):
        raise SemanticIngressError("parser requires live typed authority, not bytes or evidence")
    return authority


def validate_semantic_ingress_authority(
    authority: object,
    source: bytes | str,
    *,
    expected_context: SemanticIngressContext,
    grammar_id: str,
    grammar_version: str,
    declaration_id: str,
    operation_family: str,
) -> SemanticIngressAuthority:
    """Validate a sealed receipt at a parser or admission boundary."""

    receipt = _assert_live_authority(authority)
    if type(expected_context) is not SemanticIngressContext:
        raise SemanticIngressError("validation requires the expected typed ingress context")
    if receipt.context != expected_context:
        raise SemanticIngressError("semantic ingress producer, channel, phase, or scope mismatch")
    policy, families = _route_policy(
        receipt.context,
        receipt.grammar_id,
        receipt.grammar_version,
        receipt.declaration_id,
        receipt.allowed_operation_families,
    )
    rebuilt = _make_authority(
        context=receipt.context,
        grammar_id=receipt.grammar_id,
        grammar_version=receipt.grammar_version,
        declaration_id=receipt.declaration_id,
        allowed_operation_families=families,
        source_fingerprint=receipt.source_fingerprint,
        source_span_fingerprint=receipt.source_span_fingerprint,
        capability_authority=receipt.capability_authority,
        expected_authorization_basis=receipt.authorization_basis,
        expected_receipt_id=receipt.receipt_id,
        expected_fingerprint=receipt.fingerprint,
    )
    if policy.authorization_basis != rebuilt.authorization_basis:
        raise SemanticIngressError("semantic ingress authorization basis mismatch")
    if (receipt.grammar_id, receipt.grammar_version, receipt.declaration_id) != (
        grammar_id,
        grammar_version,
        declaration_id,
    ):
        raise SemanticIngressError("semantic ingress grammar or declaration mismatch")
    if operation_family not in receipt.allowed_operation_families:
        raise SemanticIngressError("semantic ingress operation family is not authorized")

    payload = _source_bytes(source)
    _validate_scope_against_source(receipt.context.scope, payload)
    scope = receipt.context.scope
    span = payload[scope.source_start : scope.source_end]
    if (
        raw_fingerprint(payload) != receipt.source_fingerprint
        or raw_fingerprint(span) != receipt.source_span_fingerprint
    ):
        raise SemanticIngressError("semantic ingress exact source fingerprint mismatch")
    return receipt


_REPLAY_REHYDRATOR_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class TrustedReplayRehydrator:
    """Opaque capability proving that replay evidence came through trusted storage code."""

    _seal: object

    def __init__(self, *, _seal: object) -> None:
        if _seal is not _REPLAY_REHYDRATOR_SEAL:
            raise TypeError("TrustedReplayRehydrator is code-issued")
        object.__setattr__(self, "_seal", _seal)


def trusted_replay_rehydrator() -> TrustedReplayRehydrator:
    """Create the opaque token used only by a trusted journal replay boundary."""

    return TrustedReplayRehydrator(_seal=_REPLAY_REHYDRATOR_SEAL)


_EVIDENCE_KEYS = {
    "schema",
    "code_authority_version",
    "receipt_id",
    "issuer_kind",
    "issuer_id",
    "channel",
    "authoring_phase",
    "grammar_id",
    "grammar_version",
    "declaration_id",
    "allowed_operation_families",
    "session_id",
    "branch_id",
    "turn_index",
    "attempt_id",
    "source_span",
    "source_fingerprint",
    "source_span_fingerprint",
    "authorization_basis",
    "capability_authority",
    "fingerprint",
}


def rehydrate_semantic_ingress_authority(
    evidence: object,
    source: bytes | str,
    *,
    expected_scope: IngressScope,
    rehydrator: TrustedReplayRehydrator | None,
) -> SemanticIngressAuthority:
    """Rehydrate an original receipt for replay without reauthorizing current input."""

    if (
        type(rehydrator) is not TrustedReplayRehydrator
        or getattr(rehydrator, "_seal", None) is not _REPLAY_REHYDRATOR_SEAL
    ):
        raise SemanticIngressError("semantic ingress replay requires a trusted rehydrator")
    if type(expected_scope) is not IngressScope:
        raise SemanticIngressError("semantic ingress replay requires an exact typed scope")
    if not isinstance(evidence, Mapping) or set(evidence) != _EVIDENCE_KEYS:
        raise SemanticIngressError("semantic ingress evidence has an invalid shape")
    if (
        evidence.get("schema") != SEMANTIC_INGRESS_SCHEMA
        or evidence.get("code_authority_version") != CODE_AUTHORITY_VERSION
    ):
        raise SemanticIngressError("semantic ingress evidence schema is not supported")
    source_span = evidence["source_span"]
    if not isinstance(source_span, list) or len(source_span) != 2:
        raise SemanticIngressError("semantic ingress evidence source span is invalid")
    scope = IngressScope(
        session_id=evidence["session_id"],
        branch_id=evidence["branch_id"],
        turn_index=evidence["turn_index"],
        attempt_id=evidence["attempt_id"],
        source_start=source_span[0],
        source_end=source_span[1],
    )
    if scope != expected_scope:
        raise SemanticIngressError("semantic ingress replay scope is stale or divergent")
    context = SemanticIngressContext(
        issuer_kind=evidence["issuer_kind"],
        issuer_id=evidence["issuer_id"],
        channel=evidence["channel"],
        authoring_phase=evidence["authoring_phase"],
        scope=scope,
    )
    capability_evidence = evidence["capability_authority"]
    capability = (
        None if capability_evidence is None else CapabilityIngressAuthority._from_dict(capability_evidence)
    )
    families = evidence["allowed_operation_families"]
    if not isinstance(families, list):
        raise SemanticIngressError("semantic ingress operation family evidence is invalid")
    receipt = _make_authority(
        context=context,
        grammar_id=evidence["grammar_id"],
        grammar_version=evidence["grammar_version"],
        declaration_id=evidence["declaration_id"],
        allowed_operation_families=tuple(families),
        source_fingerprint=evidence["source_fingerprint"],
        source_span_fingerprint=evidence["source_span_fingerprint"],
        capability_authority=capability,
        expected_authorization_basis=evidence["authorization_basis"],
        expected_receipt_id=evidence["receipt_id"],
        expected_fingerprint=evidence["fingerprint"],
    )
    # Rehydration proves the original receipt and its exact bytes.  It deliberately does not mint
    # a new replay-phase receipt or reinterpret current user input as an original declaration.
    validate_semantic_ingress_authority(
        receipt,
        source,
        expected_context=context,
        grammar_id=receipt.grammar_id,
        grammar_version=receipt.grammar_version,
        declaration_id=receipt.declaration_id,
        operation_family=receipt.allowed_operation_families[0],
    )
    return receipt


_COHORT_BATTLE_TAG = (
    r"\[\s*battle\s*\|[^\[\]\r\n|]+(?:\|[^\[\]\r\n|]+){1,3}\]"
)
_COHORT_TIDE_TAG = (
    r"\[\s*tide\s*\|[^\[\]\r\n|]+(?:\|[^\[\]\r\n|]+){0,1}\]"
)
_COHORT_FOE_TAG = (
    r"\[\s*foe\s*\|[^\[\]\r\n|]+(?:\|[^\[\]\r\n|]+){1,2}\]"
)
_COHORT_BLOCK_BODY = (
    rf"(?:{_COHORT_BATTLE_TAG}\s*(?:{_COHORT_TIDE_TAG}\s*)?{_COHORT_FOE_TAG}"
    rf"|{_COHORT_FOE_TAG}\s*(?:{_COHORT_TIDE_TAG}\s*{_COHORT_BATTLE_TAG}"
    rf"|{_COHORT_BATTLE_TAG}(?:\s*{_COHORT_TIDE_TAG})?))"
)
_STRICT_COHORT_BLOCK_RE = re.compile(
    rf"\A\s*{_COHORT_BLOCK_BODY}\s*\Z",
    re.IGNORECASE,
)
_RESERVED_COHORT_BLOCK_RE = re.compile(
    rf"^[ \t]*{_COHORT_BLOCK_BODY}[ \t]*(?=\r?$)",
    re.IGNORECASE | re.MULTILINE,
)


def locate_reserved_cohort_declaration(source: bytes | str) -> tuple[int, int]:
    """Return the exact UTF-8 byte span of one line-isolated narrator cohort block.

    The narrator contract permits either the documented battle-first order or the emitted
    foe-first order.  Tags quoted inside prose, separated by prose, or repeated in multiple
    candidate blocks are not a reserved declaration and therefore cannot receive authority.
    """

    payload = _source_bytes(source)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticIngressError("cohort declaration must be exact UTF-8 text") from exc
    matches = list(_RESERVED_COHORT_BLOCK_RE.finditer(text))
    safe_matches: list[re.Match[str]] = []
    for match in matches:
        # Four-space/tab indentation is a Markdown code quotation, not a live reserved line.
        block_lines = match.group(0).splitlines()
        if any(
            line and (
                "\t" in line[: len(line) - len(line.lstrip(" \t"))]
                or len(line) - len(line.lstrip(" ")) >= 4
            )
            for line in block_lines
        ):
            continue
        # A tag-only line inside a fenced example is still quoted prose.  Track fences before
        # the candidate without reconstructing or normalizing any of the declaration bytes.
        fence: tuple[str, int] | None = None
        for line in text[: match.start()].splitlines():
            marker = re.match(r"^[ ]{0,3}(`{3,}|~{3,})", line)
            if marker is None:
                continue
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
        if fence is None:
            safe_matches.append(match)
    matches = safe_matches
    if len(matches) != 1:
        raise SemanticIngressError(
            "narrator cohort tags must form one contiguous reserved declaration block"
        )
    match = matches[0]
    start = len(text[: match.start()].encode("utf-8"))
    end = len(text[: match.end()].encode("utf-8"))
    return start, end


@dataclass(frozen=True, slots=True)
class AuthorizedSemanticDeclaration:
    """Immutable, auditable parser result bound to its ingress receipt."""

    receipt_id: str
    receipt_fingerprint: str
    operation_family: str
    source_start: int
    source_end: int
    source_span_fingerprint: str
    _operation_json: tuple[str, ...]
    fingerprint: str

    @property
    def operations(self) -> tuple[dict[str, Any], ...]:
        """Return fresh operation objects so callers cannot mutate declaration evidence."""

        return tuple(json.loads(item) for item in self._operation_json)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema": SEMANTIC_DECLARATION_SCHEMA,
            "receipt_id": self.receipt_id,
            "receipt_fingerprint": self.receipt_fingerprint,
            "operation_family": self.operation_family,
            "source_span": [self.source_start, self.source_end],
            "source_span_fingerprint": self.source_span_fingerprint,
            "operations": list(self.operations),
            "fingerprint": self.fingerprint,
        }


def _authorized_declaration(
    receipt: SemanticIngressAuthority,
    operation_family: str,
    operations: Sequence[Mapping[str, Any]],
) -> AuthorizedSemanticDeclaration:
    operation_json = tuple(
        json.dumps(op, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for op in operations
    )
    scope = receipt.context.scope
    body = {
        "schema": SEMANTIC_DECLARATION_SCHEMA,
        "receipt_id": receipt.receipt_id,
        "receipt_fingerprint": receipt.fingerprint,
        "operation_family": operation_family,
        "source_span": [scope.source_start, scope.source_end],
        "source_span_fingerprint": receipt.source_span_fingerprint,
        "operations": [json.loads(item) for item in operation_json],
    }
    return AuthorizedSemanticDeclaration(
        receipt_id=receipt.receipt_id,
        receipt_fingerprint=receipt.fingerprint,
        operation_family=operation_family,
        source_start=scope.source_start,
        source_end=scope.source_end,
        source_span_fingerprint=receipt.source_span_fingerprint,
        _operation_json=operation_json,
        fingerprint=content_fingerprint(body),
    )


def parse_authorized_cohort_declaration(
    source: bytes | str,
    state: dict[str, Any],
    *,
    authority: object,
    expected_context: SemanticIngressContext,
) -> AuthorizedSemanticDeclaration:
    """Parse one strict finite-cohort block only after exact authority validation."""

    receipt = validate_semantic_ingress_authority(
        authority,
        source,
        expected_context=expected_context,
        grammar_id=COHORT_GRAMMAR_ID,
        grammar_version=COHORT_GRAMMAR_VERSION,
        declaration_id=COHORT_DECLARATION_ID,
        operation_family=OP_BATTLE_COHORT_DECLARATION,
    )
    if not isinstance(state, dict):
        raise SemanticIngressError("cohort grammar requires a state snapshot")
    payload = _source_bytes(source)
    scope = receipt.context.scope
    span_bytes = payload[scope.source_start : scope.source_end]
    try:
        text = span_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticIngressError("cohort declaration must be exact UTF-8 text") from exc
    if not _STRICT_COHORT_BLOCK_RE.fullmatch(text):
        raise SemanticIngressError("source span is not one strict cohort declaration")

    # Tier-0 is used only as a pure grammar here.  The receipt has already established authority,
    # and this wrapper verifies that the grammar produced precisely the allowed operation family.
    from . import tier0

    # A parser receives a snapshot, not permission to mutate the caller's live state object.
    operations = tier0.parse_combat_tags(text, deepcopy(state), allow_large_battle=True)
    starts = [op for op in operations if op.get("op") == "battle_start"]
    spawns = [op for op in operations if op.get("op") == "combatant_spawn"]
    if len(starts) != 1 or not spawns:
        raise SemanticIngressError("authorized cohort grammar did not produce one finite cohort")
    cohort = starts[0].get("cohort")
    if (
        not isinstance(cohort, dict)
        or cohort.get("schema") != "battle-cohort/1"
        or isinstance(cohort.get("total"), bool)
        or not isinstance(cohort.get("total"), int)
        or not 2 <= cohort["total"] <= tier0.BATTLE_COHORT_CAP
    ):
        raise SemanticIngressError("authorized cohort grammar produced an invalid cohort plan")
    allowed_ops = {"battle_start", "combatant_spawn", "tide_set"}
    if any(op.get("op") not in allowed_ops for op in operations):
        raise SemanticIngressError("cohort declaration escaped its allowed operation family")
    cohort_ref = cohort.get("id")
    if len(spawns) > min(cohort["total"], 3) or any(
        op.get("side") != "enemy"
        or op.get("cohort_ref") != cohort_ref
        or op.get("cohort_index") != index
        or op.get("name") != cohort.get("name")
        or op.get("tier") != cohort.get("tier")
        or op.get("armament", "") != cohort.get("armament", "")
        for index, op in enumerate(spawns, 1)
    ):
        raise SemanticIngressError("cohort declaration produced non-canonical spawn identities")
    return _authorized_declaration(receipt, OP_BATTLE_COHORT_DECLARATION, operations)


__all__ = [
    "CAPABILITY_ADMISSION_DECLARATION_ID",
    "CAPABILITY_AUTHORITY_GRAMMAR_ID",
    "CAPABILITY_AUTHORITY_GRAMMAR_VERSION",
    "COHORT_DECLARATION_ID",
    "COHORT_GRAMMAR_ID",
    "COHORT_GRAMMAR_VERSION",
    "OP_BATTLE_COHORT_DECLARATION",
    "OP_CAPABILITY_ADMISSION",
    "AuthorizedSemanticDeclaration",
    "CapabilityIngressAuthority",
    "IngressScope",
    "ResourceAuthorityRef",
    "SemanticIngressAuthority",
    "SemanticIngressContext",
    "SemanticIngressError",
    "TrustedReplayRehydrator",
    "issue_semantic_ingress_authority",
    "locate_reserved_cohort_declaration",
    "parse_authorized_cohort_declaration",
    "rehydrate_semantic_ingress_authority",
    "trusted_replay_rehydrator",
    "validate_semantic_ingress_authority",
]
