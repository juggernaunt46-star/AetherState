"""Pure proof construction for one brand-new RPG session's T0 bootstrap.

Session creation and genesis currently precede ordinary turn narration.  This module gives that
caller-owned boundary a detached proof primitive without owning a Store or mutating a session.  It
accepts the exact empty pre-state, the exact ordered journal rows between two high-water marks, and
the exact post-bootstrap state.  The existing semantic transition projector must replay that whole
window at turn zero and reproduce the post-state exactly.

The operation inventory is deliberately narrower than generic ``source="genesis"`` authority.  It
is derived from the current deterministic outputs of :func:`genesis.seed_rules` and
:func:`genesis.seed_player`: rules may emit ``entity_add``, ``presence``, ``obsession``, and
``craving``; player genesis may emit ``entity_add`` and ``player_seed``.  Stage-B model genesis,
extraction, rule settlement, and Player-authored operations are not bootstrap proof inputs.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
import json
import re
from types import MappingProxyType
from typing import Any

from .capability_glossary import content_fingerprint
from .semantic_transition_truth import (
    BOOTSTRAP_SOURCES,
    T0_BOOTSTRAP_OPERATION_KINDS,
    TRANSITION_POLICIES,
    SemanticTransitionTruthError,
    project_journal_transitions,
    validate_transition_projection,
)
from .state import empty_state
from .turn_lifecycle import TurnArtifactError, journal_window_fingerprint


SEMANTIC_BOOTSTRAP_PROOF_SCHEMA = "semantic-bootstrap-proof/1"
T0_BOOTSTRAP_TURN = 0

ALLOWED_T0_BOOTSTRAP_SOURCES = BOOTSTRAP_SOURCES
ALLOWED_T0_BOOTSTRAP_OPERATIONS = T0_BOOTSTRAP_OPERATION_KINDS

# These are asserted against the existing strict projector at import time.  A policy-family move
# must therefore update this audited bootstrap boundary instead of silently inheriting authority.
_EXPECTED_OPERATION_FAMILIES = {
    "craving": "embodied_affect_social",
    "entity_add": "scene_identity_placement",
    "obsession": "embodied_affect_social",
    "player_seed": "player_authoring",
    "presence": "scene_identity_placement",
}
_PROJECTOR_OPERATION_FAMILIES = {
    kind: str(TRANSITION_POLICIES[kind]["family"])
    for kind in sorted(ALLOWED_T0_BOOTSTRAP_OPERATIONS)
}
if _PROJECTOR_OPERATION_FAMILIES != _EXPECTED_OPERATION_FAMILIES:
    raise RuntimeError("T0 bootstrap operation families differ from the strict projector")

T0_BOOTSTRAP_OPERATION_FAMILIES = MappingProxyType(_EXPECTED_OPERATION_FAMILIES)
ALLOWED_T0_BOOTSTRAP_FAMILIES = frozenset(_EXPECTED_OPERATION_FAMILIES.values())

_STORE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROOF_FIELDS = {
    "schema",
    "session_id",
    "branch_id",
    "turn_index",
    "journal_high_water_before",
    "journal_high_water_after",
    "pre_bootstrap_state",
    "pre_bootstrap_state_fingerprint",
    "post_bootstrap_state",
    "post_bootstrap_state_fingerprint",
    "journal_rows",
    "journal_window_fingerprint",
    "allowed_sources",
    "allowed_operations",
    "allowed_operation_families",
    "transition_projection",
    "transition_projection_fingerprint",
    "fingerprint",
}
_JOURNAL_ROW_FIELDS = {"id", "turn_lo", "turn_hi", "source", "ops"}


class SemanticBootstrapProofError(ValueError):
    """A proposed T0 bootstrap is not one exact, replay-complete genesis transition."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SemanticBootstrapProofError(
            "semantic bootstrap proof data must be finite canonical JSON"
        ) from exc


def _json_copy(value: object) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SemanticBootstrapProofError(f"{label} must be an object with string fields")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SemanticBootstrapProofError(f"{label} must be an integer at or above {minimum}")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SemanticBootstrapProofError(f"{label} must be a sha256 fingerprint")
    return value


def _store_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _STORE_ID_RE.fullmatch(value) is None:
        raise SemanticBootstrapProofError(
            f"{label} must be one canonical 32-lowercase-hex Store identity"
        )
    return value


def _canonical_rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SemanticBootstrapProofError("journal_rows must be an exact ordered sequence")
    rows = _json_copy(list(value))
    if not isinstance(rows, list) or not rows:
        raise SemanticBootstrapProofError("T0 bootstrap journal window must not be empty")
    for row_index, raw in enumerate(rows):
        row = _mapping(raw, f"journal_rows[{row_index}]")
        if set(row) != _JOURNAL_ROW_FIELDS:
            raise SemanticBootstrapProofError(
                f"journal_rows[{row_index}] fields are not exact"
            )
        _integer(row["id"], f"journal_rows[{row_index}].id", minimum=1)
        if row["turn_lo"] != T0_BOOTSTRAP_TURN or row["turn_hi"] != T0_BOOTSTRAP_TURN:
            raise SemanticBootstrapProofError("bootstrap journal rows must cover only T0")
        if row["source"] not in ALLOWED_T0_BOOTSTRAP_SOURCES:
            raise SemanticBootstrapProofError(
                f"bootstrap journal source {row['source']!r} is not allowed"
            )
        if not isinstance(row["ops"], list) or not row["ops"]:
            raise SemanticBootstrapProofError("bootstrap journal rows must contain operations")
        for op_index, raw_op in enumerate(row["ops"]):
            op = _mapping(raw_op, f"journal_rows[{row_index}].ops[{op_index}]")
            kind = op.get("op")
            if kind not in ALLOWED_T0_BOOTSTRAP_OPERATIONS:
                raise SemanticBootstrapProofError(
                    f"operation {kind!r} is not in the deterministic T0 bootstrap inventory"
                )
            if op.get("_turn") != T0_BOOTSTRAP_TURN:
                raise SemanticBootstrapProofError("bootstrap operations must be explicitly T0")
            family = TRANSITION_POLICIES[kind]["family"]
            if family != T0_BOOTSTRAP_OPERATION_FAMILIES[kind] \
                    or family not in ALLOWED_T0_BOOTSTRAP_FAMILIES:
                raise SemanticBootstrapProofError(
                    f"operation {kind!r} belongs to a non-bootstrap policy family"
                )
    return rows


def _validate_rpg_player_bootstrap(
    rows: Sequence[Mapping[str, Any]], post_state: Mapping[str, Any]
) -> None:
    player_seed_ops = [
        op
        for row in rows
        for op in row["ops"]
        if op.get("op") == "player_seed"
    ]
    if len(player_seed_ops) != 1:
        raise SemanticBootstrapProofError(
            "brand-new RPG bootstrap requires exactly one player_seed operation"
        )
    player_id = player_seed_ops[0].get("entity")
    players = post_state.get("player")
    entities = post_state.get("entities")
    if not isinstance(player_id, str) or not player_id \
            or not isinstance(players, Mapping) or set(players) != {player_id} \
            or not isinstance(entities, Mapping) \
            or not isinstance(entities.get(player_id), Mapping) \
            or entities[player_id].get("kind") != "player":
        raise SemanticBootstrapProofError(
            "player_seed does not reproduce one exact post-bootstrap Player identity"
        )


def _validate_high_water_window(
    rows: Sequence[Mapping[str, Any]], high_water_before: int, high_water_after: int
) -> None:
    if high_water_after <= high_water_before:
        raise SemanticBootstrapProofError(
            "journal high-water must advance during T0 bootstrap"
        )
    expected_ids = list(range(high_water_before + 1, high_water_after + 1))
    observed_ids = [row["id"] for row in rows]
    if observed_ids != expected_ids:
        raise SemanticBootstrapProofError(
            "bootstrap journal IDs are missing, duplicated, or out of exact high-water order"
        )


def _validated_payload(value: object) -> dict[str, Any]:
    payload = _json_copy(value)
    proof = _mapping(payload, "semantic bootstrap proof")
    if set(proof) != _PROOF_FIELDS:
        raise SemanticBootstrapProofError("semantic bootstrap proof fields are not exact")
    if proof["schema"] != SEMANTIC_BOOTSTRAP_PROOF_SCHEMA:
        raise SemanticBootstrapProofError("semantic bootstrap proof schema is unsupported")

    supplied_fingerprint = _fingerprint(proof["fingerprint"], "proof fingerprint")
    proof_body = dict(proof)
    proof_body.pop("fingerprint")
    if supplied_fingerprint != content_fingerprint(proof_body):
        raise SemanticBootstrapProofError("semantic bootstrap proof fingerprint is invalid")

    session_id = _store_identity(proof["session_id"], "session_id")
    branch_id = _store_identity(proof["branch_id"], "branch_id")
    if session_id == branch_id:
        raise SemanticBootstrapProofError("session and branch identities must be distinct")
    turn_index = _integer(proof["turn_index"], "turn_index")
    if turn_index != T0_BOOTSTRAP_TURN:
        raise SemanticBootstrapProofError("semantic bootstrap proof must bind exactly T0")

    high_water_before = _integer(
        proof["journal_high_water_before"], "journal_high_water_before"
    )
    high_water_after = _integer(
        proof["journal_high_water_after"], "journal_high_water_after", minimum=1
    )

    expected_family_inventory = dict(sorted(T0_BOOTSTRAP_OPERATION_FAMILIES.items()))
    if proof["allowed_sources"] != sorted(ALLOWED_T0_BOOTSTRAP_SOURCES) \
            or proof["allowed_operations"] != sorted(ALLOWED_T0_BOOTSTRAP_OPERATIONS) \
            or proof["allowed_operation_families"] != expected_family_inventory:
        raise SemanticBootstrapProofError(
            "bootstrap operation/source inventory differs from the audited T0 inventory"
        )

    pre_state = _mapping(proof["pre_bootstrap_state"], "pre_bootstrap_state")
    post_state = _mapping(proof["post_bootstrap_state"], "post_bootstrap_state")
    pre_hash = _fingerprint(
        proof["pre_bootstrap_state_fingerprint"],
        "pre_bootstrap_state_fingerprint",
    )
    post_hash = _fingerprint(
        proof["post_bootstrap_state_fingerprint"],
        "post_bootstrap_state_fingerprint",
    )
    if content_fingerprint(pre_state) != pre_hash \
            or content_fingerprint(post_state) != post_hash:
        raise SemanticBootstrapProofError("bootstrap state fingerprint is invalid")
    if _canonical_bytes(pre_state) != _canonical_bytes(empty_state()):
        raise SemanticBootstrapProofError(
            "pre-bootstrap state is not the exact empty ledger state"
        )

    rows = _canonical_rows(proof["journal_rows"])
    _validate_high_water_window(rows, high_water_before, high_water_after)
    observed_window_fingerprint = journal_window_fingerprint(branch_id, rows)
    if _fingerprint(
        proof["journal_window_fingerprint"], "journal_window_fingerprint"
    ) != observed_window_fingerprint:
        raise SemanticBootstrapProofError("bootstrap journal-window fingerprint is invalid")

    _validate_rpg_player_bootstrap(rows, post_state)

    try:
        projection = validate_transition_projection(proof["transition_projection"])
        recomputed = validate_transition_projection(
            project_journal_transitions(
                pre_state=pre_state,
                post_state=post_state,
                journal_rows=rows,
                branch_id=branch_id,
                turn_index=T0_BOOTSTRAP_TURN,
                pre_ledger_hash=pre_hash,
                post_ledger_hash=post_hash,
                journal_window_fingerprint=observed_window_fingerprint,
                delivery_phase="pre_display",
            )
        )
    except (SemanticTransitionTruthError, TurnArtifactError, TypeError, ValueError) as exc:
        raise SemanticBootstrapProofError(
            "T0 journal window does not strictly reproduce the post-bootstrap state"
        ) from exc
    if _canonical_bytes(projection) != _canonical_bytes(recomputed):
        raise SemanticBootstrapProofError(
            "stored T0 transition projection differs from strict replay"
        )
    if projection["branch_id"] != branch_id \
            or projection["turn_index"] != T0_BOOTSTRAP_TURN \
            or projection["delivery_phase"] != "pre_display" \
            or projection["pre_ledger_hash"] != pre_hash \
            or projection["post_ledger_hash"] != post_hash \
            or projection["journal_window_fingerprint"] != observed_window_fingerprint:
        raise SemanticBootstrapProofError(
            "T0 transition projection lifecycle binding is inconsistent"
        )
    if projection["required_facts"] or projection["allowed_facts"] \
            or projection["metadata_receipts"] != projection["entries"] \
            or any(entry["visibility"] != "bootstrap" for entry in projection["entries"]):
        raise SemanticBootstrapProofError(
            "T0 transition projection contains non-bootstrap visible authority"
        )
    if _fingerprint(
        proof["transition_projection_fingerprint"],
        "transition_projection_fingerprint",
    ) != projection["fingerprint"]:
        raise SemanticBootstrapProofError(
            "T0 transition projection fingerprint is inconsistent"
        )

    return payload


@dataclass(frozen=True, slots=True)
class SemanticBootstrapProof:
    """Immutable proof handle whose structured accessors always return detached JSON copies."""

    fingerprint: str
    session_id: str
    branch_id: str
    turn_index: int
    journal_high_water_before: int
    journal_high_water_after: int
    pre_bootstrap_state_fingerprint: str
    post_bootstrap_state_fingerprint: str
    journal_window_fingerprint: str
    transition_projection_fingerprint: str
    _payload_json: str = dataclass_field(repr=False)

    @classmethod
    def _from_validated(cls, payload: Mapping[str, Any]) -> SemanticBootstrapProof:
        payload_json = _canonical_bytes(payload).decode("utf-8")
        return cls(
            fingerprint=str(payload["fingerprint"]),
            session_id=str(payload["session_id"]),
            branch_id=str(payload["branch_id"]),
            turn_index=int(payload["turn_index"]),
            journal_high_water_before=int(payload["journal_high_water_before"]),
            journal_high_water_after=int(payload["journal_high_water_after"]),
            pre_bootstrap_state_fingerprint=str(
                payload["pre_bootstrap_state_fingerprint"]
            ),
            post_bootstrap_state_fingerprint=str(
                payload["post_bootstrap_state_fingerprint"]
            ),
            journal_window_fingerprint=str(payload["journal_window_fingerprint"]),
            transition_projection_fingerprint=str(
                payload["transition_projection_fingerprint"]
            ),
            _payload_json=payload_json,
        )

    @property
    def pre_bootstrap_state(self) -> dict[str, Any]:
        return self.to_persistence_payload()["pre_bootstrap_state"]

    @property
    def post_bootstrap_state(self) -> dict[str, Any]:
        return self.to_persistence_payload()["post_bootstrap_state"]

    @property
    def journal_rows(self) -> list[dict[str, Any]]:
        return self.to_persistence_payload()["journal_rows"]

    @property
    def transition_projection(self) -> dict[str, Any]:
        return self.to_persistence_payload()["transition_projection"]

    @property
    def allowed_sources(self) -> tuple[str, ...]:
        return tuple(self.to_persistence_payload()["allowed_sources"])

    @property
    def allowed_operations(self) -> tuple[str, ...]:
        return tuple(self.to_persistence_payload()["allowed_operations"])

    @property
    def allowed_operation_families(self) -> dict[str, str]:
        return self.to_persistence_payload()["allowed_operation_families"]

    def to_persistence_payload(self) -> dict[str, Any]:
        """Return a detached canonical payload suitable for a caller-owned atomic write."""
        return json.loads(self._payload_json)


def build_semantic_bootstrap_proof(
    *,
    session_id: str,
    branch_id: str,
    pre_bootstrap_state: object,
    post_bootstrap_state: object,
    journal_high_water_before: int,
    journal_high_water_after: int,
    journal_rows: object,
    turn_index: int = T0_BOOTSTRAP_TURN,
) -> SemanticBootstrapProof:
    """Build one immutable T0 proof without reading or mutating Store/session state."""
    try:
        session_ref = _store_identity(session_id, "session_id")
        branch_ref = _store_identity(branch_id, "branch_id")
        if session_ref == branch_ref:
            raise SemanticBootstrapProofError("session and branch identities must be distinct")
        exact_turn_index = _integer(turn_index, "turn_index")
        if exact_turn_index != T0_BOOTSTRAP_TURN:
            raise SemanticBootstrapProofError("semantic bootstrap proof must bind exactly T0")
        before = _integer(journal_high_water_before, "journal_high_water_before")
        after = _integer(
            journal_high_water_after, "journal_high_water_after", minimum=1
        )
        pre_state = _json_copy(pre_bootstrap_state)
        post_state = _json_copy(post_bootstrap_state)
        rows = _canonical_rows(journal_rows)
        pre_mapping = _mapping(pre_state, "pre_bootstrap_state")
        post_mapping = _mapping(post_state, "post_bootstrap_state")
        if _canonical_bytes(pre_mapping) != _canonical_bytes(empty_state()):
            raise SemanticBootstrapProofError(
                "pre-bootstrap state is not the exact empty ledger state"
            )
        _validate_high_water_window(rows, before, after)
        _validate_rpg_player_bootstrap(rows, post_mapping)
        pre_hash = content_fingerprint(pre_state)
        post_hash = content_fingerprint(post_state)
        window_fingerprint = journal_window_fingerprint(branch_ref, rows)
        projection = project_journal_transitions(
            pre_state=pre_state,
            post_state=post_state,
            journal_rows=rows,
            branch_id=branch_ref,
            turn_index=T0_BOOTSTRAP_TURN,
            pre_ledger_hash=pre_hash,
            post_ledger_hash=post_hash,
            journal_window_fingerprint=window_fingerprint,
            delivery_phase="pre_display",
        )
        payload = {
            "schema": SEMANTIC_BOOTSTRAP_PROOF_SCHEMA,
            "session_id": session_ref,
            "branch_id": branch_ref,
            "turn_index": T0_BOOTSTRAP_TURN,
            "journal_high_water_before": before,
            "journal_high_water_after": after,
            "pre_bootstrap_state": pre_state,
            "pre_bootstrap_state_fingerprint": pre_hash,
            "post_bootstrap_state": post_state,
            "post_bootstrap_state_fingerprint": post_hash,
            "journal_rows": rows,
            "journal_window_fingerprint": window_fingerprint,
            "allowed_sources": sorted(ALLOWED_T0_BOOTSTRAP_SOURCES),
            "allowed_operations": sorted(ALLOWED_T0_BOOTSTRAP_OPERATIONS),
            "allowed_operation_families": dict(
                sorted(T0_BOOTSTRAP_OPERATION_FAMILIES.items())
            ),
            "transition_projection": projection,
            "transition_projection_fingerprint": projection["fingerprint"],
        }
        payload["fingerprint"] = content_fingerprint(payload)
        return SemanticBootstrapProof._from_validated(_validated_payload(payload))
    except SemanticBootstrapProofError:
        raise
    except (SemanticTransitionTruthError, TurnArtifactError, TypeError, ValueError) as exc:
        raise SemanticBootstrapProofError(str(exc)) from exc


def validate_semantic_bootstrap_proof(value: object) -> SemanticBootstrapProof:
    """Validate a detached proof payload and return a new immutable proof handle."""
    raw = value.to_persistence_payload() if isinstance(value, SemanticBootstrapProof) else value
    try:
        return SemanticBootstrapProof._from_validated(_validated_payload(raw))
    except SemanticBootstrapProofError:
        raise
    except (SemanticTransitionTruthError, TurnArtifactError, TypeError, ValueError) as exc:
        raise SemanticBootstrapProofError(str(exc)) from exc


def semantic_bootstrap_persistence_payload(value: object) -> dict[str, Any]:
    """Validate ``value`` and return a detached canonical persistence payload."""
    return validate_semantic_bootstrap_proof(value).to_persistence_payload()
