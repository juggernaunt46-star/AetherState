"""Crash-safe semantic turn reservation, settlement, delivery, replay, and fork primitives.

This module deliberately does not know about HTTP streaming or the reducer.  It supplies the
transaction fence those layers must enter: a mechanical commit is inseparable from a truthful,
proof-carrying fallback artifact, while a narrator-derived declaration is provisional until its
ledger writes and accepted wire artifact are durably promoted in the same transaction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, TYPE_CHECKING

from .canon import SEED
from .response_wire import (
    ChatWireError,
    JSON_CONTENT_TYPE,
    SSE_CONTENT_TYPE,
    decode_chat_story,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from .store import Store


TURN_KEY_SCHEMA = "semantic-turn-key/2"
TURN_ENVELOPE_SCHEMA = "semantic-turn-envelope/1"
DELIVERY_PROOF_SCHEMA = "narration-delivery-proof/1"
GATE_RECEIPT_SCHEMA = "narration-truth-gate-receipt/1"
CLAIM_PROJECTION_SCHEMA = "narration-claim-projection/1"
JOURNAL_WINDOW_SCHEMA = "semantic-journal-window/1"
EMPTY_PREFIX_HASH = hashlib.blake2b(SEED, digest_size=8).hexdigest()

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT_KINDS = frozenset({"initial", "swipe"})
_ATTEMPT_FIELDS = frozenset({"index", "kind", "request_hash", "ledger_anchor_hash"})
_DECISIONS = frozenset({"fallback", "accept"})
_ARTIFACT_KINDS = frozenset({"fallback", "accepted"})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_turn_lifecycles(
  lifecycle_key TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  accepted_prefix_pos INTEGER NOT NULL,
  accepted_head_hash TEXT NOT NULL,
  player_input_hash TEXT NOT NULL,
  semantic_contract_version TEXT NOT NULL,
  key_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',
  initial_request_hash TEXT NOT NULL,
  active_attempt_index INTEGER,
  base_envelope_fingerprint TEXT,
  terminal_envelope_fingerprint TEXT,
  pre_ledger_hash TEXT,
  mechanics_post_ledger_hash TEXT,
  post_ledger_hash TEXT,
  occurrence_fingerprint TEXT,
  effect_fingerprint TEXT,
  rng_fingerprint TEXT,
  config_fingerprint TEXT,
  engine_version TEXT,
  consumed_intent_id TEXT,
  next_intent_id TEXT,
  source_lifecycle_key TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(branch_id, turn_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_turn_consumed_intent
  ON semantic_turn_lifecycles(branch_id, consumed_intent_id)
  WHERE consumed_intent_id IS NOT NULL AND status='committed';
CREATE TABLE IF NOT EXISTS semantic_turn_attempts(
  lifecycle_key TEXT NOT NULL,
  attempt_index INTEGER NOT NULL,
  attempt_kind TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  ledger_anchor_hash TEXT,
  status TEXT NOT NULL DEFAULT 'reserved',
  refusal_code TEXT,
  fallback_envelope_fingerprint TEXT,
  fallback_envelope_json TEXT,
  fallback_bytes BLOB,
  fallback_hash TEXT,
  terminal_envelope_fingerprint TEXT,
  terminal_envelope_json TEXT,
  accepted_bytes BLOB,
  accepted_hash TEXT,
  logical_message_id TEXT,
  selected_artifact_digest TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(lifecycle_key, attempt_index),
  UNIQUE(lifecycle_key, request_hash)
);
CREATE TABLE IF NOT EXISTS semantic_turn_delivery_claims(
  lifecycle_key TEXT NOT NULL,
  attempt_index INTEGER NOT NULL,
  logical_message_id TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'claimed',
  claimed_at REAL NOT NULL,
  PRIMARY KEY(lifecycle_key, attempt_index)
);
CREATE TABLE IF NOT EXISTS semantic_turn_delivery_completions(
  lifecycle_key TEXT NOT NULL,
  attempt_index INTEGER NOT NULL,
  logical_message_id TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'completed',
  completed_at REAL NOT NULL,
  PRIMARY KEY(lifecycle_key, attempt_index)
);
"""


class TurnLifecycleError(RuntimeError):
    """Base error for lifecycle contract violations."""


class TurnDivergenceError(TurnLifecycleError):
    """The supplied canonical transcript identity is not the branch identity."""


class TurnReservationConflict(TurnLifecycleError):
    """A different owner or attempt already holds the requested lifecycle slot."""


class TurnArtifactError(TurnLifecycleError):
    """An envelope or delivery proof is incomplete, malformed, or inconsistent."""


@dataclass(frozen=True)
class TurnReservation:
    lifecycle_key: str
    attempt_index: int
    request_hash: str
    created: bool
    status: str


@dataclass(frozen=True)
class EnvelopeArtifact:
    envelope: dict[str, Any]
    fallback_bytes: bytes
    accepted_bytes: Optional[bytes] = None


@dataclass(frozen=True)
class ReplayArtifact:
    lifecycle_key: str
    attempt_index: int
    status: str
    content_type: str
    payload: bytes
    payload_hash: str
    envelope: dict[str, Any]
    logical_message_id: str
    selected_artifact_digest: str


@dataclass(frozen=True)
class FencedMutationOutput:
    """The real reducer result and its actual post-state, before lifecycle observation."""

    result: Any
    post_state: object


@dataclass(frozen=True)
class FencedMutationObservation:
    """Sealed pre/post state and exact journal window supplied to the artifact factory."""

    result: Any
    pre_state: Any
    pre_hash: str
    post_state: Any
    post_ledger_hash: str
    journal_rows: list[dict[str, Any]]
    journal_window_fingerprint: str


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TurnArtifactError("lifecycle data must be finite JSON") from exc


def fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def raw_fingerprint(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise TurnArtifactError("wire artifacts must be exact bytes")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _accepted_visible_surface_hash(wire_bytes: bytes, content_type: str) -> str:
    """Decode one accepted chat artifact through its exact declared wire adapter.

    A content hash for wire bytes and another for visible bytes are not sufficient: two unrelated
    byte strings can each honestly match their own hash.  Accepted narration therefore uses only
    the two code-owned chat wire types and proves that the story decoded from the exact wire is the
    same UTF-8 surface whose claim graph was checked.
    """
    if content_type not in {JSON_CONTENT_TYPE, SSE_CONTENT_TYPE}:
        raise TurnArtifactError("accepted narration content type is not a canonical chat wire type")
    stripped = wire_bytes.lstrip()
    if content_type == JSON_CONTENT_TYPE and stripped.startswith(b"data:"):
        raise TurnArtifactError("accepted narration wire differs from its declared content type")
    if content_type == SSE_CONTENT_TYPE and not stripped.startswith(b"data:"):
        raise TurnArtifactError("accepted narration wire differs from its declared content type")
    try:
        story = decode_chat_story(wire_bytes, content_type)
        visible = story.encode("utf-8")
    except (ChatWireError, UnicodeError, ValueError) as exc:
        raise TurnArtifactError("accepted narration wire has no canonical visible surface") from exc
    return raw_fingerprint(visible)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurnArtifactError(f"{name} must be a non-empty string")
    return value


def _require_sha(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise TurnArtifactError(f"{name} must be a sha256 fingerprint")
    return text


def _require_head(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _HEX16.fullmatch(text) is None:
        raise TurnArtifactError(f"{name} must be a canonical 16-hex hash")
    return text


def _require_non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TurnArtifactError(f"{name} must be a non-negative integer")
    return value


def _copy_json(value: object) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def journal_window_fingerprint(
    branch_id: str, journal_rows: Sequence[Mapping[str, Any]]
) -> str:
    """Bind one branch's ordered content-bearing journal rows without turn-wide lending."""
    if isinstance(journal_rows, (str, bytes, bytearray)):
        raise TurnArtifactError("journal rows must be an ordered sequence")
    rows = _copy_json(list(journal_rows))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TurnArtifactError("journal rows must contain canonical mappings")
    return fingerprint({
        "schema": JOURNAL_WINDOW_SCHEMA,
        "branch_id": _require_text(branch_id, "journal branch_id"),
        "rows": rows,
    })


def build_pre_mutation_key(
    *,
    session_id: str,
    branch_id: str,
    turn_index: int,
    accepted_prefix_pos: int,
    accepted_head_hash: str,
    player_input_hash: str,
    pre_ledger_hash: str,
    pending_intent_fingerprint: str,
    semantic_contract_version: str,
) -> dict[str, Any]:
    """Seal the exact branch state a turn is allowed to mutate.

    ``accepted_prefix_pos`` is the count of already accepted canonical messages.  Its head is the
    chain hash at ``pos - 1``; position zero uses :data:`EMPTY_PREFIX_HASH`.  The Player input is
    separately bound so an unappended current message can still be reserved without ambiguity.
    """
    turn_index = _require_non_negative_integer(turn_index, "turn_index")
    accepted_prefix_pos = _require_non_negative_integer(
        accepted_prefix_pos, "accepted_prefix_pos"
    )
    head = _require_head(accepted_head_hash, "accepted_head_hash")
    if accepted_prefix_pos == 0 and head != EMPTY_PREFIX_HASH:
        raise TurnArtifactError("an empty accepted prefix must use EMPTY_PREFIX_HASH")
    body = {
        "schema": TURN_KEY_SCHEMA,
        "session_id": _require_text(session_id, "session_id"),
        "branch_id": _require_text(branch_id, "branch_id"),
        "turn_index": turn_index,
        "accepted_prefix_pos": accepted_prefix_pos,
        "accepted_head_hash": head,
        "player_input_hash": _require_head(player_input_hash, "player_input_hash"),
        "pre_ledger_hash": _require_sha(pre_ledger_hash, "pre_ledger_hash"),
        "pending_intent_fingerprint": _require_sha(
            pending_intent_fingerprint, "pending_intent_fingerprint"
        ),
        "semantic_contract_version": _require_text(
            semantic_contract_version, "semantic_contract_version"
        ),
    }
    return {**body, "lifecycle_key": fingerprint(body)}


def validate_pre_mutation_key(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = build_pre_mutation_key(
        session_id=value.get("session_id"),
        branch_id=value.get("branch_id"),
        turn_index=value.get("turn_index"),
        accepted_prefix_pos=value.get("accepted_prefix_pos"),
        accepted_head_hash=value.get("accepted_head_hash"),
        player_input_hash=value.get("player_input_hash"),
        pre_ledger_hash=value.get("pre_ledger_hash"),
        pending_intent_fingerprint=value.get("pending_intent_fingerprint"),
        semantic_contract_version=value.get("semantic_contract_version"),
    )
    if set(value) != set(expected) or value.get("schema") != TURN_KEY_SCHEMA \
            or value.get("lifecycle_key") != expected["lifecycle_key"]:
        raise TurnArtifactError("pre-mutation key is not its exact canonical form")
    return expected


def logical_message_id(pre_mutation_key: Mapping[str, Any]) -> str:
    key = validate_pre_mutation_key(pre_mutation_key)
    return fingerprint({
        "schema": "semantic-logical-message/2",
        "session_id": key["session_id"],
        "branch_id": key["branch_id"],
        "turn_index": key["turn_index"],
        "accepted_prefix_pos": key["accepted_prefix_pos"],
        "accepted_head_hash": key["accepted_head_hash"],
        "player_input_hash": key["player_input_hash"],
        "pre_ledger_hash": key["pre_ledger_hash"],
        "pending_intent_fingerprint": key["pending_intent_fingerprint"],
        "semantic_contract_version": key["semantic_contract_version"],
    })


_CLAIM_PROJECTION_FIELDS = (
    "occurrence_ref",
    "cause_ref",
    "actor_id",
    "subject_ids",
    "kind",
    "polarity",
    "actuality",
    "time_scope",
    "multiplicity",
    "detail",
    "amount",
)


def canonical_claim_projection(graph: object) -> dict[str, Any]:
    """Project role-different narration graphs onto exact ledger-comparable claims.

    Producer, verifier, fallback, and expected-ledger graphs intentionally have different issuers,
    spans, evidence, channels, and claim references.  Comparing their full JSON would therefore
    reject valid truth.  This projection drops only that wrapper/provenance material, maps a
    candidate ``authority_ref`` to the ledger's ``construction_ref``, sorts without deduplicating,
    and preserves every field that can change the meaning or causal identity of a claim.

    Non-claim payloads retain the older strict full-JSON comparison mode.
    """
    detached = _copy_json(graph)
    claims = detached.get("claims") if isinstance(detached, dict) else None
    semantic_wrapper = isinstance(detached, dict) and any(
        field in detached for field in ("role", "issuer", "channel", "phase")
    )
    semantic_rows = isinstance(claims, list) and any(
        isinstance(claim, dict)
        and ("occurrence_ref" in claim or "authority_ref" in claim or "construction_ref" in claim)
        for claim in claims
    )
    if not isinstance(detached, dict) or "claims" not in detached \
            or not (semantic_wrapper or semantic_rows):
        return {
            "schema": CLAIM_PROJECTION_SCHEMA,
            "mode": "exact_json",
            "value": detached,
        }
    if not isinstance(claims, list):
        raise TurnArtifactError("claim graph claims must be a list")
    rows: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise TurnArtifactError(f"claim graph claims[{index}] must be an object")
        missing = [field for field in _CLAIM_PROJECTION_FIELDS if field not in claim]
        authority = claim.get("authority_ref", claim.get("construction_ref"))
        if missing or not isinstance(authority, str) or not authority:
            raise TurnArtifactError(
                f"claim graph claims[{index}] lacks canonical causal fields"
            )
        subjects = claim.get("subject_ids")
        if not isinstance(subjects, list) or any(
            not isinstance(subject, str) or not subject for subject in subjects
        ):
            raise TurnArtifactError(
                f"claim graph claims[{index}].subject_ids must be ordered references"
            )
        row = {field: _copy_json(claim[field]) for field in _CLAIM_PROJECTION_FIELDS}
        row["authority_ref"] = authority
        rows.append(row)
    rows.sort(key=_canonical_bytes)
    return {
        "schema": CLAIM_PROJECTION_SCHEMA,
        "mode": "semantic_claim_multiset",
        "claims": rows,
    }


def _delivery_truth_identities(
    *,
    artifact_kind: str,
    gate_reason_code: str,
    expected: object,
    observed: object,
    ledger: object,
    expected_projection: object,
    observed_projection: object,
    ledger_projection: object,
    comparison_mode: str,
    ledger_root_hash: str,
) -> tuple[dict[str, Any], str, str]:
    if artifact_kind not in _ARTIFACT_KINDS:
        raise TurnArtifactError("delivery artifact kind is unsupported")
    receipt_basis = {
        "schema": GATE_RECEIPT_SCHEMA,
        "decision": "accept",
        "reason_code": _require_text(gate_reason_code, "gate_reason_code"),
        "artifact_kind": artifact_kind,
        "expected_graph_fingerprint": fingerprint(expected),
        "observed_graph_fingerprint": fingerprint(observed),
        "ledger_graph_fingerprint": fingerprint(ledger),
        "expected_projection_fingerprint": fingerprint(expected_projection),
        "observed_projection_fingerprint": fingerprint(observed_projection),
        "ledger_projection_fingerprint": fingerprint(ledger_projection),
        "comparison_mode": comparison_mode,
        "ledger_root_hash": _require_sha(ledger_root_hash, "ledger_root_hash"),
    }
    receipt_basis_fingerprint = fingerprint(receipt_basis)
    proof_basis_fingerprint = fingerprint({
        "schema": "narration-delivery-proof-basis/1",
        "artifact_kind": artifact_kind,
        "receipt_basis_fingerprint": receipt_basis_fingerprint,
        "ledger_root_hash": receipt_basis["ledger_root_hash"],
        "expected_graph_fingerprint": receipt_basis["expected_graph_fingerprint"],
        "observed_graph_fingerprint": receipt_basis["observed_graph_fingerprint"],
        "ledger_graph_fingerprint": receipt_basis["ledger_graph_fingerprint"],
        "expected_projection_fingerprint": receipt_basis[
            "expected_projection_fingerprint"
        ],
        "observed_projection_fingerprint": receipt_basis[
            "observed_projection_fingerprint"
        ],
        "ledger_projection_fingerprint": receipt_basis[
            "ledger_projection_fingerprint"
        ],
        "comparison_mode": comparison_mode,
        "verdict": "pass",
    })
    return receipt_basis, receipt_basis_fingerprint, proof_basis_fingerprint


def _selected_artifact_digest(
    *,
    logical_message_identity: str,
    artifact_kind: str,
    wire_hash: str,
    content_type: str,
    renderer_hash: str,
    visible_hash: str,
    ledger_root_hash: str,
    receipt_basis_fingerprint: str,
    proof_basis_fingerprint: str,
) -> str:
    return fingerprint({
        "schema": "semantic-selected-artifact/2",
        "logical_message_id": _require_sha(
            logical_message_identity, "logical_message_identity"
        ),
        "artifact_kind": artifact_kind,
        "wire_hash": _require_sha(wire_hash, "wire_hash"),
        "content_type": _require_text(content_type, "content_type"),
        "renderer_hash": _require_sha(renderer_hash, "renderer_hash"),
        "visible_hash": _require_sha(visible_hash, "visible_hash"),
        "ledger_root_hash": _require_sha(ledger_root_hash, "ledger_root_hash"),
        "receipt_basis_fingerprint": _require_sha(
            receipt_basis_fingerprint, "receipt_basis_fingerprint"
        ),
        "proof_basis_fingerprint": _require_sha(
            proof_basis_fingerprint, "proof_basis_fingerprint"
        ),
    })


def build_delivery_proof(
    *,
    wire_bytes: bytes,
    content_type: str,
    renderer_bytes: bytes,
    visible_bytes: bytes,
    expected_graph: object,
    observed_graph: object,
    ledger_graph: object,
    ledger_root_hash: str,
    logical_message_identity: str,
    gate_reason_code: str = "truth_match",
    artifact_kind: str = "fallback",
    phase_hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Build proof that exact bytes render only the ledger-authorized claim graph.

    The optional hook exists for deterministic phase-fault tests.  Every hook fires before any
    lifecycle settlement transaction begins, so a failure cannot leave mechanical state behind.
    """
    def phase(name: str) -> None:
        if phase_hook is not None:
            phase_hook(name)

    wire_hash = raw_fingerprint(wire_bytes)
    renderer_hash = raw_fingerprint(renderer_bytes)
    visible_hash = raw_fingerprint(visible_bytes)
    if artifact_kind == "accepted" \
            and _accepted_visible_surface_hash(wire_bytes, content_type) != visible_hash:
        raise TurnArtifactError(
            "accepted narration wire story differs from the proven visible surface"
        )
    phase("fallback_construction")

    expected = _copy_json(expected_graph)
    observed = _copy_json(observed_graph)
    ledger = _copy_json(ledger_graph)
    phase("observed_extraction")

    expected_projection = canonical_claim_projection(expected)
    observed_projection = canonical_claim_projection(observed)
    ledger_projection = canonical_claim_projection(ledger)
    modes = {
        expected_projection["mode"], observed_projection["mode"], ledger_projection["mode"]
    }
    if len(modes) != 1:
        raise TurnArtifactError("delivery graphs do not share one canonical comparison mode")
    comparison_mode = next(iter(modes))
    observed_equals_expected = fingerprint(observed_projection) == fingerprint(expected_projection)
    phase("observed_expected_comparison")
    observed_matches_ledger = fingerprint(observed_projection) == fingerprint(ledger_projection)
    phase("observed_ledger_comparison")
    if not observed_equals_expected or not observed_matches_ledger:
        raise TurnArtifactError("delivery graph does not exactly match expectation and ledger")

    message_id = _require_sha(logical_message_identity, "logical_message_identity")
    receipt_basis, receipt_basis_fp, proof_basis_fp = _delivery_truth_identities(
        artifact_kind=artifact_kind,
        gate_reason_code=gate_reason_code,
        expected=expected,
        observed=observed,
        ledger=ledger,
        expected_projection=expected_projection,
        observed_projection=observed_projection,
        ledger_projection=ledger_projection,
        comparison_mode=comparison_mode,
        ledger_root_hash=ledger_root_hash,
    )
    artifact_digest = _selected_artifact_digest(
        logical_message_identity=message_id,
        artifact_kind=artifact_kind,
        wire_hash=wire_hash,
        content_type=content_type,
        renderer_hash=renderer_hash,
        visible_hash=visible_hash,
        ledger_root_hash=receipt_basis["ledger_root_hash"],
        receipt_basis_fingerprint=receipt_basis_fp,
        proof_basis_fingerprint=proof_basis_fp,
    )
    receipt = {
        **receipt_basis,
        "receipt_basis_fingerprint": receipt_basis_fp,
        "proof_basis_fingerprint": proof_basis_fp,
        "selected_artifact_digest": artifact_digest,
    }
    receipt["receipt_fingerprint"] = fingerprint(receipt)
    phase("receipt_construction")
    proof = {
        "schema": DELIVERY_PROOF_SCHEMA,
        "artifact_kind": artifact_kind,
        "proof_basis_fingerprint": proof_basis_fp,
        "expected_graph": expected,
        "observed_graph": observed,
        "ledger_graph": ledger,
        "claim_projections": {
            "expected": expected_projection,
            "observed": observed_projection,
            "ledger": ledger_projection,
        },
        "ledger_root_hash": receipt["ledger_root_hash"],
        "comparisons": {
            "mode": comparison_mode,
            "observed_equals_expected": True,
            "observed_matches_ledger": True,
        },
        "verdict": "pass",
        "gate_receipt": receipt,
        "delivery": {
            "logical_message_id": message_id,
            "content_type": content_type,
            "wire_hash": wire_hash,
            "renderer_hash": renderer_hash,
            "visible_hash": visible_hash,
            "selected_artifact_digest": artifact_digest,
            "artifact_kind": artifact_kind,
        },
    }
    proof["proof_fingerprint"] = fingerprint(proof)
    return proof


def validate_delivery_proof(proof: Mapping[str, Any], selected_bytes: bytes) -> dict[str, Any]:
    value = _copy_json(proof)
    supplied = value.pop("proof_fingerprint", None)
    if value.get("schema") != DELIVERY_PROOF_SCHEMA or supplied != fingerprint(value):
        raise TurnArtifactError("delivery proof fingerprint is invalid")
    comparisons = value.get("comparisons")
    if not isinstance(comparisons, dict) \
            or comparisons.get("mode") not in {"exact_json", "semantic_claim_multiset"} \
            or comparisons.get("observed_equals_expected") is not True \
            or comparisons.get("observed_matches_ledger") is not True \
            or value.get("verdict") != "pass":
        raise TurnArtifactError("delivery proof is not a passing exact comparison")
    expected_projection = canonical_claim_projection(value.get("expected_graph"))
    observed_projection = canonical_claim_projection(value.get("observed_graph"))
    ledger_projection = canonical_claim_projection(value.get("ledger_graph"))
    stored_projections = value.get("claim_projections")
    if stored_projections != {
        "expected": expected_projection,
        "observed": observed_projection,
        "ledger": ledger_projection,
    } or comparisons.get("mode") != expected_projection["mode"] \
            or fingerprint(observed_projection) != fingerprint(expected_projection) \
            or fingerprint(observed_projection) != fingerprint(ledger_projection):
        raise TurnArtifactError("delivery proof canonical claim projections are not equal")
    ledger_root = _require_sha(value.get("ledger_root_hash"), "ledger_root_hash")
    artifact_kind = value.get("artifact_kind")
    if artifact_kind not in _ARTIFACT_KINDS:
        raise TurnArtifactError("delivery proof artifact kind is invalid")
    delivery = value.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("wire_hash") != raw_fingerprint(selected_bytes):
        raise TurnArtifactError("delivery proof does not bind the selected exact wire bytes")
    if artifact_kind == "accepted" and _accepted_visible_surface_hash(
        selected_bytes, delivery.get("content_type")
    ) != delivery.get("visible_hash"):
        raise TurnArtifactError(
            "accepted delivery proof does not bind wire story to its visible surface"
        )
    message_id = _require_sha(delivery.get("logical_message_id"), "logical_message_id")
    receipt = value.get("gate_receipt")
    if not isinstance(receipt, dict):
        raise TurnArtifactError("typed gate receipt is missing")
    receipt_basis, receipt_basis_fp, proof_basis_fp = _delivery_truth_identities(
        artifact_kind=artifact_kind,
        gate_reason_code=receipt.get("reason_code"),
        expected=value.get("expected_graph"),
        observed=value.get("observed_graph"),
        ledger=value.get("ledger_graph"),
        expected_projection=expected_projection,
        observed_projection=observed_projection,
        ledger_projection=ledger_projection,
        comparison_mode=comparisons.get("mode"),
        ledger_root_hash=ledger_root,
    )
    expected_digest = _selected_artifact_digest(
        logical_message_identity=message_id,
        artifact_kind=artifact_kind,
        wire_hash=delivery.get("wire_hash"),
        content_type=delivery.get("content_type"),
        renderer_hash=delivery.get("renderer_hash"),
        visible_hash=delivery.get("visible_hash"),
        ledger_root_hash=ledger_root,
        receipt_basis_fingerprint=receipt_basis_fp,
        proof_basis_fingerprint=proof_basis_fp,
    )
    if delivery.get("artifact_kind") != artifact_kind \
            or delivery.get("selected_artifact_digest") != expected_digest \
            or value.get("proof_basis_fingerprint") != proof_basis_fp:
        raise TurnArtifactError(
            "selected artifact digest is not bound to terminal truth and message identity"
        )
    receipt_body = dict(receipt)
    receipt_fp = receipt_body.pop("receipt_fingerprint", None)
    if any(receipt_body.get(field) != expected for field, expected in receipt_basis.items()) \
            or receipt_body.get("receipt_basis_fingerprint") != receipt_basis_fp \
            or receipt_body.get("proof_basis_fingerprint") != proof_basis_fp \
            or receipt_fp != fingerprint(receipt_body) \
            or receipt_body.get("selected_artifact_digest") != expected_digest:
        raise TurnArtifactError("typed gate receipt is invalid")
    value["gate_receipt"] = receipt
    value["proof_fingerprint"] = supplied
    return value


def _rebind_delivery_message(
    proof: Mapping[str, Any], logical_message_identity: str
) -> dict[str, Any]:
    """Rebind a previously validated proof to a fork-local logical message identity."""
    value = _copy_json(proof)
    supplied = value.pop("proof_fingerprint", None)
    if value.get("schema") != DELIVERY_PROOF_SCHEMA or supplied != fingerprint(value):
        raise TurnArtifactError("cannot rebind a corrupted delivery proof")
    delivery = value.get("delivery")
    receipt = value.get("gate_receipt")
    if not isinstance(delivery, dict) or not isinstance(receipt, dict):
        raise TurnArtifactError("cannot rebind an incomplete delivery proof")
    message_id = _require_sha(logical_message_identity, "logical_message_identity")
    artifact_digest = _selected_artifact_digest(
        logical_message_identity=message_id,
        artifact_kind=value.get("artifact_kind"),
        wire_hash=delivery.get("wire_hash"),
        content_type=delivery.get("content_type"),
        renderer_hash=delivery.get("renderer_hash"),
        visible_hash=delivery.get("visible_hash"),
        ledger_root_hash=value.get("ledger_root_hash"),
        receipt_basis_fingerprint=receipt.get("receipt_basis_fingerprint"),
        proof_basis_fingerprint=value.get("proof_basis_fingerprint"),
    )
    delivery["logical_message_id"] = message_id
    delivery["selected_artifact_digest"] = artifact_digest
    receipt["selected_artifact_digest"] = artifact_digest
    receipt_body = dict(receipt)
    receipt_body.pop("receipt_fingerprint", None)
    receipt["receipt_fingerprint"] = fingerprint(receipt_body)
    value["proof_fingerprint"] = fingerprint(value)
    return value


def _ordered_items(items: Sequence[Mapping[str, Any]], id_field: str, label: str) -> list[dict[str, Any]]:
    out = [_copy_json(item) for item in items]
    ids = [item.get(id_field) if isinstance(item, dict) else None for item in out]
    if any(not isinstance(item_id, str) or not item_id for item_id in ids) \
            or len(ids) != len(set(ids)):
        raise TurnArtifactError(f"{label} require unique non-empty {id_field} values")
    return out


def build_envelope(
    *,
    pre_mutation_key: Mapping[str, Any],
    attempt_index: int,
    attempt_kind: str,
    request_hash: str,
    occurrences: Sequence[Mapping[str, Any]],
    effects: Sequence[Mapping[str, Any]],
    rng_fingerprint: str,
    config_fingerprint: str,
    engine_version: str,
    pre_ledger_hash: str,
    mechanics_post_ledger_hash: str,
    fallback_bytes: bytes,
    delivery_proof: Mapping[str, Any],
    decision: str = "fallback",
    accepted_bytes: Optional[bytes] = None,
    terminal_post_ledger_hash: Optional[str] = None,
    candidate_declarations: Sequence[Mapping[str, Any]] = (),
    consumed_intent_id: Optional[str] = None,
    next_intent_id: Optional[str] = None,
    gate_reason_code: str = "truth_match",
    diagnostics: Optional[Mapping[str, Any]] = None,
    source_lifecycle_key: Optional[str] = None,
    source_envelope_fingerprint: Optional[str] = None,
) -> EnvelopeArtifact:
    """Build an immutable fallback or accepted turn envelope.

    A fallback is a terminal-safe replay artifact even while a creative candidate is pending.  An
    accepted envelope may additionally bind narrator-authorized declarations and their resulting
    ledger root; those declarations are not applied by this pure builder.
    """
    key = validate_pre_mutation_key(pre_mutation_key)
    attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
    if attempt_kind not in _ATTEMPT_KINDS or decision not in _DECISIONS:
        raise TurnArtifactError("attempt kind or gate decision is invalid")
    if (attempt_index == 0) != (attempt_kind == "initial"):
        raise TurnArtifactError("only attempt zero is the initial narration attempt")
    request = _require_sha(request_hash, "request_hash")
    pre_hash = _require_sha(pre_ledger_hash, "pre_ledger_hash")
    mechanics_hash = _require_sha(mechanics_post_ledger_hash, "mechanics_post_ledger_hash")
    terminal_hash = _require_sha(
        terminal_post_ledger_hash or mechanics_hash, "terminal_post_ledger_hash"
    )
    fallback_hash = raw_fingerprint(fallback_bytes)
    declarations = _ordered_items(candidate_declarations, "declaration_id", "declarations")
    if decision == "fallback":
        if accepted_bytes is not None or declarations or terminal_hash != mechanics_hash:
            raise TurnArtifactError("fallback cannot retain candidate bytes, declarations, or state")
        selected = fallback_bytes
    else:
        if accepted_bytes is None:
            raise TurnArtifactError("accepted envelope requires exact accepted bytes")
        if declarations and attempt_kind != "initial":
            raise TurnArtifactError("swipe narration cannot introduce new ledger declarations")
        if not declarations and terminal_hash != mechanics_hash:
            raise TurnArtifactError("ledger root may change only for bound candidate declarations")
        selected = accepted_bytes
    proof = validate_delivery_proof(delivery_proof, selected)
    expected_artifact_kind = "fallback" if decision == "fallback" else "accepted"
    if proof["artifact_kind"] != expected_artifact_kind:
        raise TurnArtifactError("delivery proof artifact kind differs from envelope decision")
    if proof["ledger_root_hash"] != terminal_hash:
        raise TurnArtifactError("delivery proof is not bound to the selected ledger root")
    expected_message_id = logical_message_id(key)
    if proof["delivery"]["logical_message_id"] != expected_message_id:
        raise TurnArtifactError("delivery proof belongs to a different logical message")
    ordered_occurrences = _ordered_items(occurrences, "occurrence_id", "occurrences")
    ordered_effects = _ordered_items(effects, "effect_id", "effects")
    occurrence_ids = {item["occurrence_id"] for item in ordered_occurrences}
    if any(item.get("occurrence_id") not in occurrence_ids for item in ordered_effects):
        raise TurnArtifactError("every effect must bind an occurrence in this envelope")
    if consumed_intent_id and next_intent_id and consumed_intent_id == next_intent_id:
        raise TurnArtifactError("a consumed pending intent cannot be reissued as the next intent")
    if bool(source_lifecycle_key) != bool(source_envelope_fingerprint):
        raise TurnArtifactError("lineage source key and envelope fingerprint are inseparable")
    output = {
        "selected": decision,
        "content_type": proof["delivery"]["content_type"],
        "fallback_hash": fallback_hash,
        "fallback_size": len(fallback_bytes),
        "accepted_hash": raw_fingerprint(accepted_bytes) if accepted_bytes is not None else None,
        "accepted_size": len(accepted_bytes) if accepted_bytes is not None else None,
        "selected_artifact_digest": proof["delivery"]["selected_artifact_digest"],
    }
    envelope = {
        "schema": TURN_ENVELOPE_SCHEMA,
        "lifecycle_key": key["lifecycle_key"],
        "pre_mutation_key": key,
        "attempt": {
            "index": attempt_index,
            "kind": attempt_kind,
            "request_hash": request,
            "ledger_anchor_hash": mechanics_hash,
        },
        "occurrences": ordered_occurrences,
        "occurrence_fingerprint": fingerprint(ordered_occurrences),
        "effects": ordered_effects,
        "effect_fingerprint": fingerprint(ordered_effects),
        "runtime": {
            "rng_fingerprint": _require_sha(rng_fingerprint, "rng_fingerprint"),
            "config_fingerprint": _require_sha(config_fingerprint, "config_fingerprint"),
            "engine_version": _require_text(engine_version, "engine_version"),
        },
        "ledger": {
            "pre_hash": pre_hash,
            "mechanics_post_hash": mechanics_hash,
            "terminal_post_hash": terminal_hash,
        },
        "pending_intent": {
            "consumed_intent_id": consumed_intent_id,
            "next_intent_id": next_intent_id,
        },
        "candidate_declarations": declarations,
        "candidate_declaration_fingerprint": fingerprint(declarations),
        "gate": {
            "decision": decision,
            "reason_code": _require_text(gate_reason_code, "gate_reason_code"),
            "receipt_fingerprint": proof["gate_receipt"]["receipt_fingerprint"],
        },
        "delivery_proof": proof,
        "output": output,
        "diagnostics": _copy_json(diagnostics or {}),
        "lineage": {
            "source_lifecycle_key": source_lifecycle_key,
            "source_envelope_fingerprint": source_envelope_fingerprint,
        },
    }
    envelope["envelope_fingerprint"] = fingerprint(envelope)
    return EnvelopeArtifact(envelope, fallback_bytes, accepted_bytes)


def validate_envelope(artifact: EnvelopeArtifact) -> dict[str, Any]:
    if not isinstance(artifact, EnvelopeArtifact):
        raise TurnArtifactError("expected an EnvelopeArtifact")
    envelope = _copy_json(artifact.envelope)
    supplied = envelope.pop("envelope_fingerprint", None)
    if envelope.get("schema") != TURN_ENVELOPE_SCHEMA or supplied != fingerprint(envelope):
        raise TurnArtifactError("envelope fingerprint is invalid")
    key = validate_pre_mutation_key(envelope.get("pre_mutation_key", {}))
    if envelope.get("lifecycle_key") != key["lifecycle_key"]:
        raise TurnArtifactError("envelope and pre-mutation key disagree")
    attempt = envelope.get("attempt")
    if not isinstance(attempt, Mapping) or set(attempt) != _ATTEMPT_FIELDS:
        raise TurnArtifactError("envelope attempt fields are not exact")
    attempt_index = _require_non_negative_integer(
        attempt.get("index"), "envelope attempt index"
    )
    attempt_kind = attempt.get("kind")
    if attempt_kind not in _ATTEMPT_KINDS \
            or (attempt_index == 0) != (attempt_kind == "initial"):
        raise TurnArtifactError("envelope attempt kind or index is invalid")
    _require_sha(attempt.get("request_hash"), "envelope attempt request_hash")
    _require_sha(
        attempt.get("ledger_anchor_hash"), "envelope attempt ledger_anchor_hash"
    )
    output = envelope.get("output", {})
    if output.get("fallback_hash") != raw_fingerprint(artifact.fallback_bytes) \
            or output.get("fallback_size") != len(artifact.fallback_bytes):
        raise TurnArtifactError("fallback bytes do not match envelope")
    decision = envelope.get("gate", {}).get("decision")
    selected = artifact.fallback_bytes if decision == "fallback" else artifact.accepted_bytes
    if decision not in _DECISIONS or selected is None:
        raise TurnArtifactError("envelope has no selectable terminal artifact")
    if decision == "accept":
        if output.get("accepted_hash") != raw_fingerprint(selected) \
                or output.get("accepted_size") != len(selected):
            raise TurnArtifactError("accepted bytes do not match envelope")
    validate_delivery_proof(envelope.get("delivery_proof", {}), selected)
    if fingerprint(envelope.get("occurrences")) != envelope.get("occurrence_fingerprint") \
            or fingerprint(envelope.get("effects")) != envelope.get("effect_fingerprint") \
            or fingerprint(envelope.get("candidate_declarations")) \
            != envelope.get("candidate_declaration_fingerprint"):
        raise TurnArtifactError("ordered envelope payload fingerprints are invalid")
    envelope["envelope_fingerprint"] = supplied
    return envelope


class TurnLifecycleStore:
    """SQLite-backed CAS coordinator attached to :class:`aetherstate.store.Store`."""

    def __init__(self, store: "Store") -> None:
        self.store = store
        self.db = store.db
        self._lock = store._lock
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    def count(self, branch_id: Optional[str] = None) -> int:
        with self._lock:
            if branch_id is None:
                row = self.db.execute("SELECT COUNT(*) AS n FROM semantic_turn_lifecycles").fetchone()
            else:
                row = self.db.execute(
                    "SELECT COUNT(*) AS n FROM semantic_turn_lifecycles WHERE branch_id=?",
                    (branch_id,),
                ).fetchone()
        return int(row["n"])

    def reserve(
        self, pre_mutation_key: Mapping[str, Any], *, request_hash: Optional[str] = None
    ) -> TurnReservation:
        key = validate_pre_mutation_key(pre_mutation_key)
        request = _require_sha(
            request_hash or fingerprint({"lifecycle_key": key["lifecycle_key"], "attempt": 0}),
            "request_hash",
        )
        now = time.time()
        with self.store.transaction():
            occupied = self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=? AND turn_index=?",
                (key["branch_id"], key["turn_index"]),
            ).fetchone()
            if occupied is not None:
                if occupied["lifecycle_key"] != key["lifecycle_key"] \
                        or occupied["initial_request_hash"] != request:
                    raise TurnDivergenceError("turn slot is already bound to different input or head")
                attempt = self.db.execute(
                    "SELECT status FROM semantic_turn_attempts"
                    " WHERE lifecycle_key=? AND attempt_index=0",
                    (key["lifecycle_key"],),
                ).fetchone()
                return TurnReservation(key["lifecycle_key"], 0, request, False, attempt["status"])

            branch = self.db.execute(
                "SELECT b.session_id, b.status FROM branches b WHERE b.branch_id=?",
                (key["branch_id"],),
            ).fetchone()
            if branch is None or branch["session_id"] != key["session_id"] \
                    or branch["status"] != "live":
                raise TurnDivergenceError("session/branch identity is absent or not live")
            pos = key["accepted_prefix_pos"]
            if pos == 0:
                actual_head = EMPTY_PREFIX_HASH
            else:
                row = self.db.execute(
                    "SELECT chain_hash FROM branch_msgs WHERE branch_id=? AND pos=?",
                    (key["branch_id"], pos - 1),
                ).fetchone()
                actual_head = row["chain_hash"] if row is not None else None
            if actual_head != key["accepted_head_hash"]:
                raise TurnDivergenceError("accepted transcript head diverged before reservation")
            current = self.db.execute(
                "SELECT content_hash FROM branch_msgs WHERE branch_id=? AND pos=?",
                (key["branch_id"], pos),
            ).fetchone()
            if current is not None and current["content_hash"] != key["player_input_hash"]:
                raise TurnDivergenceError("current Player input diverged before reservation")
            recorded = self.db.execute(
                "SELECT user_hash FROM turns WHERE branch_id=? AND turn_index=?",
                (key["branch_id"], key["turn_index"]),
            ).fetchone()
            if recorded is not None and recorded["user_hash"] not in (None, key["player_input_hash"]):
                raise TurnDivergenceError("recorded Player turn hash diverged before reservation")
            self.db.execute(
                "INSERT INTO semantic_turn_lifecycles("
                " lifecycle_key, session_id, branch_id, turn_index, accepted_prefix_pos,"
                " accepted_head_hash, player_input_hash, semantic_contract_version, key_json,"
                " initial_request_hash, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key["lifecycle_key"], key["session_id"], key["branch_id"],
                    key["turn_index"], pos, key["accepted_head_hash"], key["player_input_hash"],
                    key["semantic_contract_version"], _canonical_bytes(key).decode("utf-8"),
                    request, now, now,
                ),
            )
            self.db.execute(
                "INSERT INTO semantic_turn_attempts(lifecycle_key, attempt_index, attempt_kind,"
                " request_hash, status, created_at, updated_at) VALUES(?,0,'initial',?,'reserved',?,?)",
                (key["lifecycle_key"], request, now, now),
            )
        return TurnReservation(key["lifecycle_key"], 0, request, True, "reserved")

    def reserve_swipe(
        self,
        lifecycle_key: str,
        *,
        request_hash: str,
        expected_post_ledger_hash: str,
        allow_regate: bool = True,
        refusal_code: str = "swipe_disabled",
    ) -> TurnReservation:
        request = _require_sha(request_hash, "request_hash")
        expected = _require_sha(expected_post_ledger_hash, "expected_post_ledger_hash")
        now = time.time()
        with self.store.transaction():
            lifecycle = self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?", (lifecycle_key,)
            ).fetchone()
            if lifecycle is None or lifecycle["status"] != "committed":
                raise TurnReservationConflict("swipe requires a committed lifecycle")
            if lifecycle["post_ledger_hash"] != expected:
                raise TurnDivergenceError("swipe ledger root differs from the committed turn")

            # A terminal artifact becomes a completed client-visible generation only after the exact
            # artifact has crossed both the durable first-byte claim and the separate normal-stream
            # completion boundary.  A claim alone is deliberately insufficient: cancellation or a
            # crash after claim but before/during emission must still recover this attempt.  Without
            # a stable frontend generation id, fail closed on every incomplete active artifact and
            # replay it without reserving, moving the pointer, or mutating the count a second time.
            active = None
            if lifecycle["active_attempt_index"] is not None:
                active = self.db.execute(
                    "SELECT * FROM semantic_turn_attempts"
                    " WHERE lifecycle_key=? AND attempt_index=?",
                    (lifecycle_key, int(lifecycle["active_attempt_index"])),
                ).fetchone()
            if active is not None:
                claim = self.db.execute(
                    "SELECT * FROM semantic_turn_delivery_claims"
                    " WHERE lifecycle_key=? AND attempt_index=?",
                    (lifecycle_key, int(active["attempt_index"])),
                ).fetchone()
                completion = self.db.execute(
                    "SELECT * FROM semantic_turn_delivery_completions"
                    " WHERE lifecycle_key=? AND attempt_index=?",
                    (lifecycle_key, int(active["attempt_index"])),
                ).fetchone()
                if claim is not None and (
                    claim["status"] != "claimed"
                    or claim["logical_message_id"] != active["logical_message_id"]
                    or claim["artifact_digest"] != active["selected_artifact_digest"]
                ):
                        raise TurnReservationConflict(
                            "active delivery claim differs from its terminal artifact"
                        )
                if completion is not None and (
                    claim is None
                    or completion["status"] != "completed"
                    or completion["logical_message_id"] != active["logical_message_id"]
                    or completion["artifact_digest"] != active["selected_artifact_digest"]
                ):
                    raise TurnReservationConflict(
                        "active completion differs from its claimed terminal artifact"
                    )
                if completion is None:
                    if active["status"] not in {
                        "fallback_ready", "accepted", "fallback_final",
                    }:
                        raise TurnReservationConflict(
                            "active attempt has no terminal proof artifact to recover"
                        )
                    # Validate the complete stored artifact before granting replay authority.
                    self._replay_row(active)
                    return TurnReservation(
                        lifecycle_key,
                        int(active["attempt_index"]),
                        str(active["request_hash"]),
                        False,
                        str(active["status"]),
                    )
            prior = self.db.execute(
                "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND request_hash=?",
                (lifecycle_key, request),
            ).fetchone()
            if prior is not None:
                return TurnReservation(
                    lifecycle_key, int(prior["attempt_index"]), request, False, prior["status"]
                )
            in_flight = self.db.execute(
                "SELECT 1 FROM semantic_turn_attempts WHERE lifecycle_key=? AND status='reserved'",
                (lifecycle_key,),
            ).fetchone()
            if in_flight is not None:
                raise TurnReservationConflict("another narration attempt is still in flight")
            row = self.db.execute(
                "SELECT COALESCE(MAX(attempt_index), -1) + 1 AS n FROM semantic_turn_attempts"
                " WHERE lifecycle_key=?",
                (lifecycle_key,),
            ).fetchone()
            index = int(row["n"])
            status = "reserved" if allow_regate else "refused"
            self.db.execute(
                "INSERT INTO semantic_turn_attempts(lifecycle_key, attempt_index, attempt_kind,"
                " request_hash, ledger_anchor_hash, status, refusal_code, created_at, updated_at)"
                " VALUES(?,?,'swipe',?,?,?,?,?,?)",
                (
                    lifecycle_key, index, request, expected, status,
                    None if allow_regate else _require_text(refusal_code, "refusal_code"), now, now,
                ),
            )
        return TurnReservation(lifecycle_key, index, request, True, status)

    def _attempt_for_artifact(self, envelope: Mapping[str, Any]) -> sqlite3.Row:
        attempt = envelope["attempt"]
        attempt_index = _require_non_negative_integer(
            attempt.get("index"), "artifact attempt index"
        )
        row = self.db.execute(
            "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
            (envelope["lifecycle_key"], attempt_index),
        ).fetchone()
        if row is None or int(row["attempt_index"]) != attempt_index \
                or row["attempt_kind"] != attempt["kind"] \
                or row["request_hash"] != attempt["request_hash"]:
            raise TurnReservationConflict("artifact does not own this narration reservation")
        return row

    def _attempt_for_reservation(
        self, reservation: TurnReservation
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not isinstance(reservation, TurnReservation):
            raise TurnReservationConflict("factory settlement requires a TurnReservation")
        try:
            attempt_index = _require_non_negative_integer(
                reservation.attempt_index, "reservation attempt index"
            )
        except TurnArtifactError as exc:
            raise TurnReservationConflict(
                "factory settlement reservation attempt index is invalid"
            ) from exc
        row = self.db.execute(
            "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
            (reservation.lifecycle_key, attempt_index),
        ).fetchone()
        lifecycle = self.db.execute(
            "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
            (reservation.lifecycle_key,),
        ).fetchone()
        if row is None or lifecycle is None \
                or int(row["attempt_index"]) != attempt_index \
                or row["request_hash"] != reservation.request_hash:
            raise TurnReservationConflict("factory does not own this lifecycle reservation")
        return row, lifecycle

    def _verify_reserved_fence(self, lifecycle: sqlite3.Row) -> dict[str, Any]:
        """Recheck canonical input/head identity immediately before a real mutation."""
        key = validate_pre_mutation_key(json.loads(lifecycle["key_json"]))
        columns = (
            (key["lifecycle_key"], lifecycle["lifecycle_key"]),
            (key["session_id"], lifecycle["session_id"]),
            (key["branch_id"], lifecycle["branch_id"]),
            (key["turn_index"], lifecycle["turn_index"]),
            (key["accepted_prefix_pos"], lifecycle["accepted_prefix_pos"]),
            (key["accepted_head_hash"], lifecycle["accepted_head_hash"]),
            (key["player_input_hash"], lifecycle["player_input_hash"]),
            (key["semantic_contract_version"], lifecycle["semantic_contract_version"]),
        )
        if any(left != right for left, right in columns):
            raise TurnDivergenceError("reserved lifecycle columns no longer match its sealed key")
        branch = self.db.execute(
            "SELECT session_id, status, head_turn FROM branches WHERE branch_id=?",
            (key["branch_id"],),
        ).fetchone()
        if branch is None or branch["session_id"] != key["session_id"] \
                or branch["status"] != "live" or int(branch["head_turn"]) > key["turn_index"]:
            raise TurnDivergenceError("reserved branch is absent, closed, or advanced")
        pos = key["accepted_prefix_pos"]
        if pos == 0:
            actual_head = EMPTY_PREFIX_HASH
        else:
            head = self.db.execute(
                "SELECT chain_hash FROM branch_msgs WHERE branch_id=? AND pos=?",
                (key["branch_id"], pos - 1),
            ).fetchone()
            actual_head = head["chain_hash"] if head is not None else None
        if actual_head != key["accepted_head_hash"]:
            raise TurnDivergenceError("reserved transcript head diverged before mutation")
        current = self.db.execute(
            "SELECT content_hash FROM branch_msgs WHERE branch_id=? AND pos=?",
            (key["branch_id"], pos),
        ).fetchone()
        if current is not None and current["content_hash"] != key["player_input_hash"]:
            raise TurnDivergenceError("reserved Player input diverged before mutation")
        later = self.db.execute(
            "SELECT 1 FROM branch_msgs WHERE branch_id=? AND pos>? LIMIT 1",
            (key["branch_id"], pos),
        ).fetchone()
        if later is not None:
            raise TurnDivergenceError("reserved transcript advanced past the Player input")
        recorded = self.db.execute(
            "SELECT user_hash FROM turns WHERE branch_id=? AND turn_index=?",
            (key["branch_id"], key["turn_index"]),
        ).fetchone()
        if recorded is not None and recorded["user_hash"] not in (None, key["player_input_hash"]):
            raise TurnDivergenceError("reserved turn now belongs to different Player input")
        return key

    def _persist_fallback(
        self,
        row: sqlite3.Row,
        lifecycle: sqlite3.Row,
        envelope: Mapping[str, Any],
        artifact: EnvelopeArtifact,
    ) -> ReplayArtifact:
        """Persist a validated fallback; caller owns the surrounding Store transaction."""
        attempt = envelope["attempt"]
        if row["lifecycle_key"] != envelope["lifecycle_key"] \
                or int(row["attempt_index"]) != attempt["index"] \
                or row["request_hash"] != attempt["request_hash"]:
            raise TurnReservationConflict("fallback persistence crossed attempt identity")
        expected_post = envelope["ledger"]["mechanics_post_hash"]
        proof = envelope["delivery_proof"]
        now = time.time()
        attempt_update = self.db.execute(
            "UPDATE semantic_turn_attempts SET ledger_anchor_hash=?, status='fallback_ready',"
            " fallback_envelope_fingerprint=?, fallback_envelope_json=?, fallback_bytes=?,"
            " fallback_hash=?, logical_message_id=?, selected_artifact_digest=?, updated_at=?"
            " WHERE lifecycle_key=? AND attempt_index=? AND status='reserved'",
            (
                expected_post, envelope["envelope_fingerprint"],
                _canonical_bytes(envelope).decode("utf-8"), artifact.fallback_bytes,
                envelope["output"]["fallback_hash"], proof["delivery"]["logical_message_id"],
                proof["delivery"]["selected_artifact_digest"], now,
                envelope["lifecycle_key"], attempt["index"],
            ),
        )
        if attempt_update.rowcount != 1:
            raise TurnReservationConflict("narration attempt lost its fallback CAS")
        if attempt["kind"] == "initial":
            cur = self.db.execute(
                "UPDATE semantic_turn_lifecycles SET status='committed',"
                " active_attempt_index=?, base_envelope_fingerprint=?,"
                " terminal_envelope_fingerprint=?, pre_ledger_hash=?,"
                " mechanics_post_ledger_hash=?, post_ledger_hash=?,"
                " occurrence_fingerprint=?, effect_fingerprint=?, rng_fingerprint=?,"
                " config_fingerprint=?, engine_version=?, consumed_intent_id=?,"
                " next_intent_id=?, updated_at=? WHERE lifecycle_key=? AND status='reserved'",
                (
                    attempt["index"], envelope["envelope_fingerprint"],
                    envelope["envelope_fingerprint"], envelope["ledger"]["pre_hash"],
                    expected_post, expected_post, envelope["occurrence_fingerprint"],
                    envelope["effect_fingerprint"], envelope["runtime"]["rng_fingerprint"],
                    envelope["runtime"]["config_fingerprint"],
                    envelope["runtime"]["engine_version"],
                    envelope["pending_intent"]["consumed_intent_id"],
                    envelope["pending_intent"]["next_intent_id"], now,
                    envelope["lifecycle_key"],
                ),
            )
        else:
            if lifecycle["status"] != "committed":
                raise TurnReservationConflict("swipe lifecycle is no longer committed")
            cur = self.db.execute(
                "UPDATE semantic_turn_lifecycles SET active_attempt_index=?,"
                " base_envelope_fingerprint=?, terminal_envelope_fingerprint=?,"
                " updated_at=? WHERE lifecycle_key=? AND status='committed'",
                (
                    attempt["index"], envelope["envelope_fingerprint"],
                    envelope["envelope_fingerprint"], now, envelope["lifecycle_key"],
                ),
            )
        if cur.rowcount != 1:
            raise TurnReservationConflict("lifecycle reservation disappeared during commit")
        stored = self.db.execute(
            "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
            (envelope["lifecycle_key"], attempt["index"]),
        ).fetchone()
        return self._replay_row(stored)

    def commit_mutation_with_fallback(
        self,
        artifact: EnvelopeArtifact,
        mutation_callback: Optional[Callable[[], str]] = None,
        *,
        swipe_callback: Optional[Callable[[], str]] = None,
    ) -> ReplayArtifact:
        """Atomically commit mechanics and a proof-complete deterministic fallback.

        The callback may use any nested :class:`Store` transaction helper.  It must return the
        observed post-ledger fingerprint.  A duplicate call replays bytes and never invokes the
        callback, which makes concurrent/retry settlement idempotent.
        """
        envelope = validate_envelope(artifact)
        if envelope["gate"]["decision"] != "fallback":
            raise TurnArtifactError("mechanical settlement requires a fallback envelope")
        attempt = envelope["attempt"]
        if attempt["kind"] == "initial" and mutation_callback is None:
            raise TurnArtifactError("initial settlement requires the fenced reducer callback")
        if attempt["kind"] == "initial" and swipe_callback is not None:
            raise TurnArtifactError("initial settlement cannot run a swipe callback")
        if attempt["kind"] == "swipe" and mutation_callback is not None:
            raise TurnArtifactError("swipe narration cannot rerun or mutate mechanics")
        expected_post = envelope["ledger"]["mechanics_post_hash"]
        try:
            with self.store.transaction():
                row = self._attempt_for_artifact(envelope)
                if row["status"] in {"fallback_ready", "accepted", "fallback_final"}:
                    return self._replay_row(row)
                if row["status"] != "reserved":
                    raise TurnReservationConflict("narration attempt cannot settle from this state")
                lifecycle = self.db.execute(
                    "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                    (envelope["lifecycle_key"],),
                ).fetchone()
                if attempt["kind"] == "initial":
                    if lifecycle is None or lifecycle["status"] != "reserved":
                        raise TurnReservationConflict("initial lifecycle is no longer reserved")
                    self._verify_reserved_fence(lifecycle)
                    assert mutation_callback is not None
                    observed_post = mutation_callback()
                    if observed_post != expected_post:
                        raise TurnArtifactError("reducer commit returned a different ledger root")
                else:
                    self._validate_swipe_fallback(lifecycle, row, envelope)
                    if swipe_callback is not None:
                        observed_post = swipe_callback()
                        if observed_post != expected_post:
                            raise TurnArtifactError(
                                "swipe session mutation changed the committed ledger root"
                            )
                return self._persist_fallback(row, lifecycle, envelope, artifact)
        except sqlite3.IntegrityError as exc:
            raise TurnReservationConflict("single-use intent or lifecycle identity was already used") from exc

    def commit_mutation_with_fallback_factory(
        self,
        reservation: TurnReservation,
        mutation_callback: Callable[[], FencedMutationOutput],
        artifact_factory: Callable[[FencedMutationObservation], EnvelopeArtifact],
        *,
        expected_pre_ledger_hash: Optional[str] = None,
        pre_state_callback: Optional[Callable[[], object]] = None,
    ) -> ReplayArtifact:
        """Commit a real reducer result and a fallback derived from that result atomically.

        Production usage is intentionally two callbacks::

            def mutate():
                result = apply()
                return FencedMutationOutput(result=result, post_state=result.state)

            replay = store.turn_lifecycle.commit_mutation_with_fallback_factory(
                reservation,
                mutate,
                lambda observed: build_envelope(
                    ...,
                    mechanics_post_ledger_hash=observed.post_ledger_hash,
                    terminal_post_ledger_hash=observed.post_ledger_hash,
                    delivery_proof=build_delivery_proof(
                        ..., ledger_root_hash=observed.post_ledger_hash
                    ),
                ),
            )

        In real code ``apply`` should be a small function so it can return its result and state.
        Both callbacks run inside the same Store transaction after the canonical reservation fence
        is revalidated.  The pre-state and journal high-water are captured after that fence.  The
        post-ledger hash and exact ordered rows inserted by the mutation are computed here from JSON
        snapshots; neither is accepted from a callback.  Any callback, proof, render, observation,
        envelope, CAS, or persistence failure rolls the reducer back.  A duplicate returns durable
        replay before invoking the pre-state, mutation, or artifact callback.

        This API owns initial mechanical settlement only.  A swipe has no mutation callback and
        continues through ``commit_mutation_with_fallback`` after ``reserve_swipe``.
        """
        if not callable(mutation_callback) or not callable(artifact_factory):
            raise TurnArtifactError("factory settlement requires mutation and artifact callbacks")
        if (expected_pre_ledger_hash is None) != (pre_state_callback is None):
            raise TurnArtifactError(
                "pre-ledger fencing requires both an expected hash and a state callback"
            )
        expected_pre = (
            _require_sha(expected_pre_ledger_hash, "expected_pre_ledger_hash")
            if expected_pre_ledger_hash is not None else None
        )
        if pre_state_callback is not None and not callable(pre_state_callback):
            raise TurnArtifactError("pre-ledger state callback is not callable")
        try:
            with self.store.transaction():
                row, lifecycle = self._attempt_for_reservation(reservation)
                if row["status"] in {"fallback_ready", "accepted", "fallback_final"}:
                    return self._replay_row(row)
                if row["status"] != "reserved" or row["attempt_kind"] != "initial" \
                        or int(row["attempt_index"]) != 0 or lifecycle["status"] != "reserved":
                    raise TurnReservationConflict(
                        "factory settlement requires the reserved initial attempt"
                    )
                key = self._verify_reserved_fence(lifecycle)
                observed_pre_state = None
                # Legacy factory call sites may omit the state reader; retain their sealed key
                # hash while exposing ``None`` for the unavailable snapshot. Production truth
                # settlement supplies the reader and proves this hash from the copied state.
                observed_pre_hash = key["pre_ledger_hash"]
                if pre_state_callback is not None:
                    observed_pre_state = _copy_json(pre_state_callback())
                    observed_pre_hash = fingerprint(observed_pre_state)
                    if expected_pre != key["pre_ledger_hash"] \
                            or observed_pre_hash != expected_pre:
                        raise TurnDivergenceError(
                            "reserved pre-ledger or pending-intent state diverged before mutation"
                        )
                journal_high_water = self.store.journal_high_water()
                output = mutation_callback()
                if not isinstance(output, FencedMutationOutput):
                    raise TurnArtifactError(
                        "mutation callback must return FencedMutationOutput(result, post_state)"
                    )
                post_state = _copy_json(output.post_state)
                post_hash = fingerprint(post_state)
                journal_terminal_high_water = self.store.journal_high_water()
                journal_rows = _copy_json(self.store.journal_window(
                    key["branch_id"],
                    after_id=journal_high_water,
                    through_id=journal_terminal_high_water,
                ))
                journal_fingerprint = journal_window_fingerprint(
                    key["branch_id"], journal_rows
                )
                observation = FencedMutationObservation(
                    result=output.result,
                    pre_state=observed_pre_state,
                    pre_hash=observed_pre_hash,
                    post_state=post_state,
                    post_ledger_hash=post_hash,
                    journal_rows=journal_rows,
                    journal_window_fingerprint=journal_fingerprint,
                )
                artifact = artifact_factory(observation)
                if observed_pre_state is not None \
                        and fingerprint(observation.pre_state) != observed_pre_hash:
                    raise TurnArtifactError(
                        "fenced pre-state observation changed during artifact construction"
                    )
                if fingerprint(observation.post_state) != post_hash:
                    raise TurnArtifactError(
                        "fenced post-state observation changed during artifact construction"
                    )
                if journal_window_fingerprint(
                    key["branch_id"], observation.journal_rows
                ) != journal_fingerprint:
                    raise TurnArtifactError(
                        "fenced journal observation changed during artifact construction"
                    )
                current_high_water = self.store.journal_high_water()
                current_journal_rows = self.store.journal_window(
                    key["branch_id"],
                    after_id=journal_high_water,
                    through_id=current_high_water,
                )
                if current_high_water != journal_terminal_high_water \
                        or journal_window_fingerprint(
                            key["branch_id"], current_journal_rows
                        ) != journal_fingerprint:
                    raise TurnArtifactError(
                        "fenced journal window changed after mutation observation"
                    )
                envelope = validate_envelope(artifact)
                artifact_attempt = envelope["attempt"]
                if envelope["gate"]["decision"] != "fallback" \
                        or envelope["pre_mutation_key"] != key \
                        or artifact_attempt.get("index") != int(row["attempt_index"]) \
                        or artifact_attempt.get("kind") != row["attempt_kind"] \
                        or artifact_attempt.get("request_hash") != row["request_hash"]:
                    raise TurnArtifactError(
                        "fallback factory returned an artifact for a different reservation"
                    )
                ledger = envelope["ledger"]
                if expected_pre is not None and ledger["pre_hash"] != expected_pre:
                    raise TurnArtifactError(
                        "fallback factory did not bind the fenced pre-ledger root"
                    )
                if artifact_attempt.get("ledger_anchor_hash") != post_hash \
                        or ledger["mechanics_post_hash"] != post_hash \
                        or ledger["terminal_post_hash"] != post_hash \
                        or envelope["delivery_proof"]["ledger_root_hash"] != post_hash:
                    raise TurnArtifactError(
                        "fallback factory did not bind the actual reducer post-ledger root"
                    )
                artifact_row = self._attempt_for_artifact(envelope)
                if artifact_row["lifecycle_key"] != row["lifecycle_key"] \
                        or int(artifact_row["attempt_index"]) != int(row["attempt_index"]):
                    raise TurnReservationConflict("fallback factory crossed reservation identity")
                return self._persist_fallback(row, lifecycle, envelope, artifact)
        except sqlite3.IntegrityError as exc:
            raise TurnReservationConflict(
                "single-use intent or lifecycle identity was already used"
            ) from exc

    def _validate_swipe_fallback(
        self,
        lifecycle: Optional[sqlite3.Row],
        attempt: sqlite3.Row,
        envelope: Mapping[str, Any],
    ) -> None:
        if lifecycle is None or lifecycle["status"] != "committed":
            raise TurnReservationConflict("swipe lost its committed lifecycle")
        if lifecycle["active_attempt_index"] is None:
            raise TurnReservationConflict("swipe lifecycle has no active source artifact")
        source_attempt = self.db.execute(
            "SELECT * FROM semantic_turn_attempts"
            " WHERE lifecycle_key=? AND attempt_index=?",
            (lifecycle["lifecycle_key"], int(lifecycle["active_attempt_index"])),
        ).fetchone()
        source_fingerprint = None
        if source_attempt is not None and source_attempt["status"] in {
            "fallback_ready", "accepted", "fallback_final",
        }:
            source_fingerprint = (
                source_attempt["terminal_envelope_fingerprint"]
                or source_attempt["fallback_envelope_fingerprint"]
            )
        lineage = envelope.get("lineage")
        if not isinstance(lineage, Mapping) \
                or lineage.get("source_lifecycle_key") != lifecycle["lifecycle_key"] \
                or lineage.get("source_envelope_fingerprint") \
                != lifecycle["terminal_envelope_fingerprint"] \
                or source_fingerprint != lifecycle["terminal_envelope_fingerprint"]:
            raise TurnDivergenceError(
                "swipe artifact is not derived from the active terminal source artifact"
            )
        ledger = envelope["ledger"]
        if attempt["ledger_anchor_hash"] != lifecycle["post_ledger_hash"] \
                or ledger["mechanics_post_hash"] != lifecycle["post_ledger_hash"] \
                or ledger["terminal_post_hash"] != lifecycle["post_ledger_hash"]:
            raise TurnDivergenceError("swipe artifact is not anchored to the unchanged ledger")
        frozen = (
            (ledger["pre_hash"], lifecycle["pre_ledger_hash"]),
            (envelope["occurrence_fingerprint"], lifecycle["occurrence_fingerprint"]),
            (envelope["effect_fingerprint"], lifecycle["effect_fingerprint"]),
            (envelope["runtime"]["rng_fingerprint"], lifecycle["rng_fingerprint"]),
            (envelope["runtime"]["config_fingerprint"], lifecycle["config_fingerprint"]),
            (envelope["runtime"]["engine_version"], lifecycle["engine_version"]),
            (envelope["pending_intent"]["consumed_intent_id"], lifecycle["consumed_intent_id"]),
            (envelope["pending_intent"]["next_intent_id"], lifecycle["next_intent_id"]),
        )
        if any(left != right for left, right in frozen):
            raise TurnDivergenceError("swipe artifact changed frozen semantic or mechanical truth")

    def promote_candidate(
        self,
        artifact: EnvelopeArtifact,
        provisional_callback: Optional[Callable[[], str]] = None,
    ) -> ReplayArtifact:
        """CAS-promote accepted bytes and provisional declarations as one durable observation.

        The callback is invoked only for an accepted artifact with non-empty candidate declarations,
        and its writes share the artifact transaction.  Gate rejection is represented by retaining
        or finalizing the fallback and cannot invoke candidate-derived writes.
        """
        envelope = validate_envelope(artifact)
        decision = envelope["gate"]["decision"]
        declarations = envelope["candidate_declarations"]
        if decision == "fallback" and provisional_callback is not None:
            raise TurnArtifactError("gate rejection cannot run candidate-derived state writes")
        if decision == "accept" and bool(declarations) != bool(provisional_callback):
            raise TurnArtifactError("bound candidate declarations require exactly one provisional callback")
        now = time.time()
        with self.store.transaction():
            row = self._attempt_for_artifact(envelope)
            if row["status"] in {"accepted", "fallback_final"}:
                if row["terminal_envelope_fingerprint"] != envelope["envelope_fingerprint"]:
                    raise TurnReservationConflict("attempt already promoted to a different artifact")
                replay = self._replay_row(row)
                lifecycle = self.db.execute(
                    "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                    (envelope["lifecycle_key"],),
                ).fetchone()
                newer = self.db.execute(
                    "SELECT 1 FROM semantic_turn_attempts"
                    " WHERE lifecycle_key=? AND attempt_index>? LIMIT 1",
                    (envelope["lifecycle_key"], int(row["attempt_index"])),
                ).fetchone()
                if lifecycle is None or lifecycle["status"] != "committed" \
                        or lifecycle["active_attempt_index"] is None \
                        or int(lifecycle["active_attempt_index"]) != int(row["attempt_index"]) \
                        or lifecycle["base_envelope_fingerprint"] \
                        != row["fallback_envelope_fingerprint"] \
                        or lifecycle["terminal_envelope_fingerprint"] \
                        != row["terminal_envelope_fingerprint"] \
                        or newer is not None:
                    raise TurnReservationConflict(
                        "promoted artifact is stale because a newer attempt exists or is active"
                    )
                return replay
            if row["status"] != "fallback_ready":
                raise TurnReservationConflict("candidate promotion requires a durable fallback")
            attempt_index = int(row["attempt_index"])
            base = json.loads(row["fallback_envelope_json"])
            self._validate_promotion(base, envelope, artifact)
            expected_active_fingerprint = row["fallback_envelope_fingerprint"]
            expected_current_post = base["ledger"]["terminal_post_hash"]
            lifecycle = self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                (envelope["lifecycle_key"],),
            ).fetchone()
            newer = self.db.execute(
                "SELECT 1 FROM semantic_turn_attempts"
                " WHERE lifecycle_key=? AND attempt_index>? LIMIT 1",
                (envelope["lifecycle_key"], attempt_index),
            ).fetchone()
            if lifecycle is None or lifecycle["status"] != "committed" \
                    or lifecycle["active_attempt_index"] is None \
                    or int(lifecycle["active_attempt_index"]) != attempt_index \
                    or lifecycle["base_envelope_fingerprint"] != expected_active_fingerprint \
                    or lifecycle["terminal_envelope_fingerprint"] != expected_active_fingerprint \
                    or lifecycle["post_ledger_hash"] != expected_current_post \
                    or row["ledger_anchor_hash"] != expected_current_post:
                raise TurnReservationConflict(
                    "candidate promotion lost its exact active lifecycle CAS anchor"
                )
            if newer is not None:
                raise TurnReservationConflict(
                    "candidate promotion is stale because a newer attempt exists"
                )
            delivery_claim = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (envelope["lifecycle_key"], attempt_index),
            ).fetchone()
            if delivery_claim is not None and (
                decision != "fallback"
                or delivery_claim["logical_message_id"]
                != envelope["delivery_proof"]["delivery"]["logical_message_id"]
                or delivery_claim["artifact_digest"]
                != envelope["delivery_proof"]["delivery"]["selected_artifact_digest"]
            ):
                raise TurnReservationConflict(
                    "a delivery-claimed fallback cannot be replaced or reinterpreted"
                )
            expected_terminal = envelope["ledger"]["terminal_post_hash"]
            if provisional_callback is not None:
                observed = provisional_callback()
                if observed != expected_terminal:
                    raise TurnArtifactError("provisional declaration commit returned wrong ledger root")
            elif expected_terminal != envelope["ledger"]["mechanics_post_hash"]:
                raise TurnArtifactError("unmodified promotion cannot change the ledger root")
            status = "accepted" if decision == "accept" else "fallback_final"
            attempt_update = self.db.execute(
                "UPDATE semantic_turn_attempts SET status=?, terminal_envelope_fingerprint=?,"
                " terminal_envelope_json=?, accepted_bytes=?, accepted_hash=?,"
                " logical_message_id=?, selected_artifact_digest=?, updated_at=?"
                " WHERE lifecycle_key=? AND attempt_index=? AND status='fallback_ready'"
                " AND fallback_envelope_fingerprint=? AND ledger_anchor_hash=?"
                " AND NOT EXISTS(SELECT 1 FROM semantic_turn_attempts AS newer"
                " WHERE newer.lifecycle_key=semantic_turn_attempts.lifecycle_key"
                " AND newer.attempt_index>semantic_turn_attempts.attempt_index)",
                (
                    status, envelope["envelope_fingerprint"],
                    _canonical_bytes(envelope).decode("utf-8"), artifact.accepted_bytes,
                    envelope["output"]["accepted_hash"],
                    envelope["delivery_proof"]["delivery"]["logical_message_id"],
                    envelope["delivery_proof"]["delivery"]["selected_artifact_digest"], now,
                    envelope["lifecycle_key"], attempt_index,
                    expected_active_fingerprint, expected_current_post,
                ),
            )
            if attempt_update.rowcount != 1:
                raise TurnReservationConflict("candidate promotion lost its attempt CAS")
            lifecycle_update = self.db.execute(
                "UPDATE semantic_turn_lifecycles SET active_attempt_index=?,"
                " terminal_envelope_fingerprint=?, post_ledger_hash=?, updated_at=?"
                " WHERE lifecycle_key=? AND status='committed' AND active_attempt_index=?"
                " AND base_envelope_fingerprint=? AND terminal_envelope_fingerprint=?"
                " AND post_ledger_hash=?"
                " AND NOT EXISTS(SELECT 1 FROM semantic_turn_attempts AS newer"
                " WHERE newer.lifecycle_key=semantic_turn_lifecycles.lifecycle_key"
                " AND newer.attempt_index>semantic_turn_lifecycles.active_attempt_index)",
                (
                    attempt_index, envelope["envelope_fingerprint"],
                    expected_terminal, now, envelope["lifecycle_key"], attempt_index,
                    expected_active_fingerprint, expected_active_fingerprint,
                    expected_current_post,
                ),
            )
            if lifecycle_update.rowcount != 1:
                raise TurnReservationConflict("candidate promotion lost its lifecycle CAS")
            stored = self.db.execute(
                "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
                (envelope["lifecycle_key"], attempt_index),
            ).fetchone()
            replay = self._replay_row(stored)
        return replay

    def claim_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
    ) -> ReplayArtifact:
        """CAS-claim the exact active proof artifact immediately before its first byte.

        The same identity is idempotent while it remains active.  An inactive attempt, stale swipe,
        mismatched message, or mismatched artifact digest fails closed and never changes payload or
        active-attempt state.
        """
        lifecycle_ref = _require_sha(lifecycle_key, "lifecycle_key")
        message_ref = _require_sha(
            expected_logical_message_id, "expected_logical_message_id"
        )
        artifact_ref = _require_sha(expected_artifact_digest, "expected_artifact_digest")
        attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
        with self.store.transaction():
            lifecycle = self.db.execute(
                "SELECT status, active_attempt_index FROM semantic_turn_lifecycles"
                " WHERE lifecycle_key=?",
                (lifecycle_ref,),
            ).fetchone()
            if lifecycle is None or lifecycle["status"] != "committed" \
                    or lifecycle["active_attempt_index"] is None \
                    or int(lifecycle["active_attempt_index"]) != attempt_index:
                raise TurnReservationConflict(
                    "delivery claim targets an inactive or stale narration attempt"
                )
            row = self.db.execute(
                "SELECT * FROM semantic_turn_attempts"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if row is None or row["status"] not in {
                "fallback_ready", "accepted", "fallback_final"
            }:
                raise TurnReservationConflict(
                    "delivery claim requires an active terminal proof artifact"
                )
            if row["logical_message_id"] != message_ref \
                    or row["selected_artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "delivery claim identity does not match the active proof artifact"
                )
            existing = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if existing is not None:
                if existing["logical_message_id"] != message_ref \
                        or existing["artifact_digest"] != artifact_ref \
                        or existing["status"] != "claimed":
                    raise TurnReservationConflict(
                        "delivery attempt is already claimed by a different artifact"
                    )
                return self._replay_row(row)
            self.db.execute(
                "INSERT INTO semantic_turn_delivery_claims("
                " lifecycle_key, attempt_index, logical_message_id, artifact_digest,"
                " status, claimed_at) VALUES(?,?,?,?, 'claimed', ?)",
                (lifecycle_ref, attempt_index, message_ref, artifact_ref, time.time()),
            )
            stored = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if stored is None or stored["logical_message_id"] != message_ref \
                    or stored["artifact_digest"] != artifact_ref:
                raise TurnReservationConflict("delivery claim CAS was not durably observed")
            return self._replay_row(row)

    def verify_claimed_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
        expected_session_id: str,
        expected_branch_id: str,
        expected_turn_index: int,
    ) -> ReplayArtifact:
        """Read-only verification for the exact artifact selected by the cold response path.

        Unlike :meth:`claim_delivery`, this method can never create authority.  The first-byte
        path must already have durably claimed the still-active attempt.  Holding the Store
        transaction across this verification and the cold-path writes prevents a concurrent
        swipe from retiring the artifact between the check and those writes.
        """
        lifecycle_ref = _require_sha(lifecycle_key, "lifecycle_key")
        message_ref = _require_sha(
            expected_logical_message_id, "expected_logical_message_id"
        )
        artifact_ref = _require_sha(expected_artifact_digest, "expected_artifact_digest")
        session_ref = _require_text(expected_session_id, "expected_session_id")
        branch_ref = _require_text(expected_branch_id, "expected_branch_id")
        attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
        expected_turn_index = _require_non_negative_integer(
            expected_turn_index, "expected_turn_index"
        )
        with self._lock:
            lifecycle = self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                (lifecycle_ref,),
            ).fetchone()
            if lifecycle is None or lifecycle["status"] != "committed" \
                    or lifecycle["active_attempt_index"] is None \
                    or int(lifecycle["active_attempt_index"]) != attempt_index:
                raise TurnReservationConflict(
                    "cold response targets an inactive or stale narration attempt"
                )
            if lifecycle["session_id"] != session_ref \
                    or lifecycle["branch_id"] != branch_ref \
                    or int(lifecycle["turn_index"]) != expected_turn_index:
                raise TurnReservationConflict(
                    "cold response context differs from its lifecycle identity"
                )
            claim = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if claim is None or claim["status"] != "claimed" \
                    or claim["logical_message_id"] != message_ref \
                    or claim["artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "cold response has no exact durable first-byte claim"
                )
            row = self.db.execute(
                "SELECT * FROM semantic_turn_attempts"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if row is None or row["logical_message_id"] != message_ref \
                    or row["selected_artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "cold response differs from its selected terminal artifact"
                )
            replay = self._replay_row(row)
            key = replay.envelope["pre_mutation_key"]
            if key["session_id"] != session_ref or key["branch_id"] != branch_ref \
                    or int(key["turn_index"]) != expected_turn_index:
                raise TurnReservationConflict(
                    "cold response envelope differs from its delivery context"
                )
            return replay

    def complete_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
    ) -> ReplayArtifact:
        """Seal normal completion of the exact active claimed artifact.

        The proxy calls this only after its one-chunk semantic payload generator resumes normally
        after ``yield``.  Cancellation, disconnect, or a crash leaves no completion row, so the
        next swipe-shaped request must recover this same artifact rather than advancing context.
        """
        lifecycle_ref = _require_sha(lifecycle_key, "lifecycle_key")
        message_ref = _require_sha(
            expected_logical_message_id, "expected_logical_message_id"
        )
        artifact_ref = _require_sha(expected_artifact_digest, "expected_artifact_digest")
        attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
        with self.store.transaction():
            lifecycle = self.db.execute(
                "SELECT status, active_attempt_index FROM semantic_turn_lifecycles"
                " WHERE lifecycle_key=?",
                (lifecycle_ref,),
            ).fetchone()
            if lifecycle is None or lifecycle["status"] != "committed" \
                    or lifecycle["active_attempt_index"] is None \
                    or int(lifecycle["active_attempt_index"]) != attempt_index:
                raise TurnReservationConflict(
                    "delivery completion targets an inactive or stale narration attempt"
                )
            row = self.db.execute(
                "SELECT * FROM semantic_turn_attempts"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if row is None or row["status"] not in {
                "fallback_ready", "accepted", "fallback_final",
            } or row["logical_message_id"] != message_ref \
                    or row["selected_artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "delivery completion differs from the active terminal artifact"
                )
            claim = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if claim is None or claim["status"] != "claimed" \
                    or claim["logical_message_id"] != message_ref \
                    or claim["artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "delivery completion requires the exact durable first-byte claim"
                )
            existing = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_completions"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if existing is not None:
                if existing["status"] != "completed" \
                        or existing["logical_message_id"] != message_ref \
                        or existing["artifact_digest"] != artifact_ref:
                    raise TurnReservationConflict(
                        "delivery attempt is completed by a different artifact"
                    )
                return self._replay_row(row)
            self.db.execute(
                "INSERT INTO semantic_turn_delivery_completions("
                " lifecycle_key, attempt_index, logical_message_id, artifact_digest,"
                " status, completed_at) VALUES(?,?,?,?, 'completed', ?)",
                (lifecycle_ref, attempt_index, message_ref, artifact_ref, time.time()),
            )
            stored = self.db.execute(
                "SELECT * FROM semantic_turn_delivery_completions"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_ref, attempt_index),
            ).fetchone()
            if stored is None or stored["status"] != "completed" \
                    or stored["logical_message_id"] != message_ref \
                    or stored["artifact_digest"] != artifact_ref:
                raise TurnReservationConflict(
                    "delivery completion CAS was not durably observed"
                )
            return self._replay_row(row)

    @staticmethod
    def _validate_promotion(
        base: Mapping[str, Any], terminal: Mapping[str, Any], artifact: EnvelopeArtifact
    ) -> None:
        stable_paths = (
            "lifecycle_key", "pre_mutation_key", "attempt", "occurrences",
            "occurrence_fingerprint", "effects", "effect_fingerprint", "runtime",
            "pending_intent", "lineage", "diagnostics",
        )
        if any(base.get(name) != terminal.get(name) for name in stable_paths):
            raise TurnArtifactError("candidate promotion changed frozen semantic/mechanical truth")
        if base.get("ledger", {}).get("pre_hash") != terminal.get("ledger", {}).get("pre_hash") \
                or base.get("ledger", {}).get("mechanics_post_hash") \
                != terminal.get("ledger", {}).get("mechanics_post_hash"):
            raise TurnArtifactError("candidate promotion changed settled ledger ancestry")
        if base.get("output", {}).get("fallback_hash") \
                != terminal.get("output", {}).get("fallback_hash") \
                or base.get("output", {}).get("fallback_size") \
                != terminal.get("output", {}).get("fallback_size") \
                or base.get("output", {}).get("content_type") \
                != terminal.get("output", {}).get("content_type") \
                or raw_fingerprint(artifact.fallback_bytes) \
                != base.get("output", {}).get("fallback_hash"):
            raise TurnArtifactError("candidate promotion changed fallback replay truth")
        if base.get("delivery_proof", {}).get("delivery", {}).get("logical_message_id") \
                != terminal.get("delivery_proof", {}).get("delivery", {}).get("logical_message_id"):
            raise TurnArtifactError("candidate promotion changed logical message identity")

    def replay(
        self,
        lifecycle_key: str,
        *,
        attempt_index: Optional[int] = None,
        reason: str = "reopen",
    ) -> ReplayArtifact:
        if reason not in {"retry", "lost_reply", "reopen"}:
            raise TurnLifecycleError("unsupported replay reason")
        if attempt_index is not None:
            attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
        with self._lock:
            if attempt_index is None:
                lifecycle = self.db.execute(
                    "SELECT active_attempt_index FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                    (lifecycle_key,),
                ).fetchone()
                if lifecycle is None or lifecycle["active_attempt_index"] is None:
                    raise TurnReservationConflict("lifecycle has no proof-carrying active artifact")
                attempt_index = int(lifecycle["active_attempt_index"])
            row = self.db.execute(
                "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_key, attempt_index),
            ).fetchone()
            if row is None:
                raise TurnReservationConflict("narration attempt does not exist")
            return self._replay_row(row)

    def fallback_artifact(
        self,
        lifecycle_key: str,
        *,
        attempt_index: Optional[int] = None,
    ) -> EnvelopeArtifact:
        """Return the exact persisted fallback behind an active or named terminal attempt.

        Accepted narration is allowed to replace only the selected bytes.  A later swipe must
        inherit the already proved fallback, not regenerate one from the accepted prose or from
        mutable current state.  ``_replay_row`` first validates the complete stored attempt,
        terminal artifact, lifecycle anchors, and active pointer before the fallback is detached.
        """
        if attempt_index is not None:
            attempt_index = _require_non_negative_integer(attempt_index, "attempt_index")
        with self._lock:
            if attempt_index is None:
                lifecycle = self.db.execute(
                    "SELECT active_attempt_index FROM semantic_turn_lifecycles"
                    " WHERE lifecycle_key=?",
                    (lifecycle_key,),
                ).fetchone()
                if lifecycle is None or lifecycle["active_attempt_index"] is None:
                    raise TurnReservationConflict(
                        "lifecycle has no proof-carrying active fallback"
                    )
                attempt_index = int(lifecycle["active_attempt_index"])
            row = self.db.execute(
                "SELECT * FROM semantic_turn_attempts"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (lifecycle_key, attempt_index),
            ).fetchone()
            if row is None:
                raise TurnReservationConflict("narration attempt does not exist")
            self._replay_row(row)
            fallback = bytes(row["fallback_bytes"])
            envelope = json.loads(row["fallback_envelope_json"])
            artifact = EnvelopeArtifact(envelope, fallback)
            valid = validate_envelope(artifact)
            if valid["gate"]["decision"] != "fallback" \
                    or valid["envelope_fingerprint"] \
                    != row["fallback_envelope_fingerprint"] \
                    or valid["output"]["fallback_hash"] != row["fallback_hash"]:
                raise TurnArtifactError(
                    "stored fallback columns are inconsistent"
                )
            return artifact

    def _replay_row(self, row: sqlite3.Row) -> ReplayArtifact:
        if row["fallback_bytes"] is None or row["fallback_envelope_json"] is None:
            raise TurnReservationConflict("narration attempt has no complete fallback artifact")
        fallback = bytes(row["fallback_bytes"])
        accepted = bytes(row["accepted_bytes"]) if row["accepted_bytes"] is not None else None
        status = row["status"]
        if status == "refused":
            raise TurnReservationConflict(f"narration attempt refused: {row['refusal_code']}")
        if status not in {"fallback_ready", "accepted", "fallback_final"}:
            raise TurnReservationConflict("narration attempt has no durable replay artifact")
        try:
            fallback_envelope = json.loads(row["fallback_envelope_json"])
            base = validate_envelope(EnvelopeArtifact(fallback_envelope, fallback))
            if base["gate"]["decision"] != "fallback" \
                    or base["envelope_fingerprint"] != row["fallback_envelope_fingerprint"] \
                    or base["output"]["fallback_hash"] != row["fallback_hash"]:
                raise TurnArtifactError("stored fallback columns are inconsistent")

            if status == "fallback_ready":
                if accepted is not None or row["accepted_hash"] is not None \
                        or row["terminal_envelope_json"] is not None \
                        or row["terminal_envelope_fingerprint"] is not None:
                    raise TurnArtifactError("unpromoted fallback contains terminal columns")
                valid = base
                payload = fallback
                payload_hash = row["fallback_hash"]
                stored_envelope_fingerprint = row["fallback_envelope_fingerprint"]
            else:
                if row["terminal_envelope_json"] is None \
                        or row["terminal_envelope_fingerprint"] is None:
                    raise TurnArtifactError("terminal narration columns are incomplete")
                terminal_envelope = json.loads(row["terminal_envelope_json"])
                if status == "accepted":
                    if accepted is None or row["accepted_hash"] is None:
                        raise TurnArtifactError("accepted narration artifact is incomplete")
                    valid = validate_envelope(
                        EnvelopeArtifact(terminal_envelope, fallback, accepted)
                    )
                    if valid["gate"]["decision"] != "accept" \
                            or valid["output"]["accepted_hash"] != row["accepted_hash"]:
                        raise TurnArtifactError("accepted terminal columns are inconsistent")
                    payload = accepted
                    payload_hash = row["accepted_hash"]
                else:
                    if accepted is not None or row["accepted_hash"] is not None:
                        raise TurnArtifactError("final fallback contains accepted bytes")
                    valid = validate_envelope(EnvelopeArtifact(terminal_envelope, fallback))
                    if valid["gate"]["decision"] != "fallback":
                        raise TurnArtifactError("final fallback has an accepted decision")
                    payload = fallback
                    payload_hash = row["fallback_hash"]
                self._validate_promotion(
                    base,
                    valid,
                    EnvelopeArtifact(terminal_envelope, fallback, accepted),
                )
                stored_envelope_fingerprint = row["terminal_envelope_fingerprint"]

            attempt = valid["attempt"]
            row_identity = (
                (row["lifecycle_key"], valid["lifecycle_key"]),
                (int(row["attempt_index"]), attempt["index"]),
                (row["attempt_kind"], attempt["kind"]),
                (row["request_hash"], attempt["request_hash"]),
                (row["ledger_anchor_hash"], attempt["ledger_anchor_hash"]),
            )
            if any(left != right for left, right in row_identity):
                raise TurnArtifactError("attempt row differs from its sealed envelope")

            lifecycle = self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                (row["lifecycle_key"],),
            ).fetchone()
            key = valid["pre_mutation_key"]
            lifecycle_identity = (
                (lifecycle["lifecycle_key"] if lifecycle is not None else None,
                 key["lifecycle_key"]),
                (lifecycle["session_id"] if lifecycle is not None else None,
                 key["session_id"]),
                (lifecycle["branch_id"] if lifecycle is not None else None,
                 key["branch_id"]),
                (int(lifecycle["turn_index"]) if lifecycle is not None else None,
                 key["turn_index"]),
                (int(lifecycle["accepted_prefix_pos"]) if lifecycle is not None else None,
                 key["accepted_prefix_pos"]),
                (lifecycle["accepted_head_hash"] if lifecycle is not None else None,
                 key["accepted_head_hash"]),
                (lifecycle["player_input_hash"] if lifecycle is not None else None,
                 key["player_input_hash"]),
                (lifecycle["semantic_contract_version"] if lifecycle is not None else None,
                 key["semantic_contract_version"]),
            )
            if lifecycle is None or lifecycle["status"] != "committed" \
                    or any(left != right for left, right in lifecycle_identity):
                raise TurnArtifactError("lifecycle row differs from its sealed envelope")
            is_active_attempt = lifecycle["active_attempt_index"] is not None \
                and int(lifecycle["active_attempt_index"]) == int(row["attempt_index"])
            if is_active_attempt and (
                lifecycle["base_envelope_fingerprint"]
                != row["fallback_envelope_fingerprint"]
                or lifecycle["terminal_envelope_fingerprint"]
                != stored_envelope_fingerprint
            ):
                raise TurnArtifactError(
                    "active lifecycle does not bind its exact base and terminal envelopes"
                )

            frozen = (
                (lifecycle["pre_ledger_hash"], base["ledger"]["pre_hash"]),
                (lifecycle["occurrence_fingerprint"], base["occurrence_fingerprint"]),
                (lifecycle["effect_fingerprint"], base["effect_fingerprint"]),
                (lifecycle["rng_fingerprint"], base["runtime"]["rng_fingerprint"]),
                (lifecycle["config_fingerprint"], base["runtime"]["config_fingerprint"]),
                (lifecycle["engine_version"], base["runtime"]["engine_version"]),
                (lifecycle["consumed_intent_id"],
                 base["pending_intent"]["consumed_intent_id"]),
                (lifecycle["next_intent_id"], base["pending_intent"]["next_intent_id"]),
            )
            expected_anchor = (
                lifecycle["mechanics_post_ledger_hash"]
                if attempt["kind"] == "initial" else lifecycle["post_ledger_hash"]
            )
            if any(left != right for left, right in frozen) \
                    or base["ledger"]["mechanics_post_hash"] != expected_anchor \
                    or base["ledger"]["terminal_post_hash"] != expected_anchor \
                    or attempt["ledger_anchor_hash"] != expected_anchor \
                    or valid["ledger"]["terminal_post_hash"] != lifecycle["post_ledger_hash"]:
                raise TurnArtifactError("stored envelope differs from committed lifecycle truth")

            delivery = valid["delivery_proof"]["delivery"]
            if valid["envelope_fingerprint"] != stored_envelope_fingerprint \
                    or raw_fingerprint(payload) != payload_hash \
                    or row["logical_message_id"] != delivery["logical_message_id"] \
                    or row["selected_artifact_digest"] \
                    != delivery["selected_artifact_digest"] \
                    or valid["output"]["content_type"] != delivery["content_type"] \
                    or valid["output"]["selected_artifact_digest"] \
                    != delivery["selected_artifact_digest"]:
                raise TurnArtifactError("stored replay selection is inconsistent")
        except (
            TurnArtifactError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OverflowError,
        ) as exc:
            raise TurnReservationConflict("stored narration artifact failed validation") from exc
        return ReplayArtifact(
            row["lifecycle_key"], int(row["attempt_index"]), row["status"],
            valid["output"]["content_type"], payload, payload_hash, valid,
            row["logical_message_id"], row["selected_artifact_digest"],
        )

    def fork_prefix(
        self, source_branch: str, child_branch: str, at_pos: int, fork_turn: int
    ) -> None:
        """Copy proof-carrying prefix envelopes with child-local keys and explicit lineage."""
        child = self.db.execute(
            "SELECT session_id FROM branches WHERE branch_id=?", (child_branch,)
        ).fetchone()
        if child is None:
            raise TurnLifecycleError("child branch does not exist")
        self.assert_fork_prefix_ready(source_branch, at_pos, fork_turn)
        rows = self.db.execute(
            "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?"
            " AND turn_index<=? AND accepted_prefix_pos<? ORDER BY turn_index",
            (source_branch, fork_turn, at_pos),
        ).fetchall()
        for source in rows:
            source_key = json.loads(source["key_json"])
            child_key = build_pre_mutation_key(
                session_id=child["session_id"], branch_id=child_branch,
                turn_index=source_key["turn_index"],
                accepted_prefix_pos=source_key["accepted_prefix_pos"],
                accepted_head_hash=source_key["accepted_head_hash"],
                player_input_hash=source_key["player_input_hash"],
                pre_ledger_hash=source_key["pre_ledger_hash"],
                pending_intent_fingerprint=source_key["pending_intent_fingerprint"],
                semantic_contract_version=source_key["semantic_contract_version"],
            )
            active_source_index = int(source["active_attempt_index"])
            attempts = self.db.execute(
                "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=?"
                " AND attempt_index<=?"
                " AND status IN ('fallback_ready','accepted','fallback_final') ORDER BY attempt_index",
                (source["lifecycle_key"], active_source_index),
            ).fetchall()
            if not attempts:
                raise TurnReservationConflict(
                    "semantic fork prefix has no terminal attempt to copy"
                )
            rebased: list[tuple[sqlite3.Row, dict[str, Any], Optional[dict[str, Any]], str]] = []
            for attempt in attempts:
                replay = self._replay_row(attempt)
                request = fingerprint({
                    "source_request_hash": attempt["request_hash"],
                    "child_lifecycle_key": child_key["lifecycle_key"],
                    "attempt_index": int(attempt["attempt_index"]),
                })
                fallback_source = validate_envelope(EnvelopeArtifact(
                    json.loads(attempt["fallback_envelope_json"]),
                    bytes(attempt["fallback_bytes"]),
                ))
                source_terminal_fingerprint = (
                    attempt["terminal_envelope_fingerprint"]
                    or attempt["fallback_envelope_fingerprint"]
                )
                fallback = self._rebase_envelope(
                    fallback_source, child_key, request,
                    source["lifecycle_key"], source_terminal_fingerprint,
                )
                terminal = None
                if attempt["terminal_envelope_json"]:
                    terminal = self._rebase_envelope(
                        replay.envelope, child_key, request,
                        source["lifecycle_key"], source_terminal_fingerprint,
                    )
                rebased.append((attempt, fallback, terminal, request))
            now = time.time()
            active_pair = next(pair for pair in rebased if int(pair[0]["attempt_index"]) == active_source_index)
            active_envelope = active_pair[2] or active_pair[1]
            first_request = rebased[0][3]
            self.db.execute(
                "INSERT INTO semantic_turn_lifecycles("
                " lifecycle_key, session_id, branch_id, turn_index, accepted_prefix_pos,"
                " accepted_head_hash, player_input_hash, semantic_contract_version, key_json,"
                " status, initial_request_hash, active_attempt_index, base_envelope_fingerprint,"
                " terminal_envelope_fingerprint, pre_ledger_hash, mechanics_post_ledger_hash,"
                " post_ledger_hash, occurrence_fingerprint, effect_fingerprint, rng_fingerprint,"
                " config_fingerprint, engine_version, consumed_intent_id, next_intent_id,"
                " source_lifecycle_key, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    child_key["lifecycle_key"], child_key["session_id"], child_branch,
                    child_key["turn_index"], child_key["accepted_prefix_pos"],
                    child_key["accepted_head_hash"], child_key["player_input_hash"],
                    child_key["semantic_contract_version"],
                    _canonical_bytes(child_key).decode("utf-8"), "committed", first_request,
                    active_source_index, active_pair[1]["envelope_fingerprint"],
                    active_envelope["envelope_fingerprint"], active_envelope["ledger"]["pre_hash"],
                    active_envelope["ledger"]["mechanics_post_hash"],
                    active_envelope["ledger"]["terminal_post_hash"],
                    active_envelope["occurrence_fingerprint"], active_envelope["effect_fingerprint"],
                    active_envelope["runtime"]["rng_fingerprint"],
                    active_envelope["runtime"]["config_fingerprint"],
                    active_envelope["runtime"]["engine_version"],
                    active_envelope["pending_intent"]["consumed_intent_id"],
                    active_envelope["pending_intent"]["next_intent_id"],
                    source["lifecycle_key"], now, now,
                ),
            )
            for attempt, fallback, terminal, request in rebased:
                chosen = terminal or fallback
                self.db.execute(
                    "INSERT INTO semantic_turn_attempts("
                    " lifecycle_key, attempt_index, attempt_kind, request_hash, ledger_anchor_hash,"
                    " status, fallback_envelope_fingerprint, fallback_envelope_json, fallback_bytes,"
                    " fallback_hash, terminal_envelope_fingerprint, terminal_envelope_json,"
                    " accepted_bytes, accepted_hash, logical_message_id, selected_artifact_digest,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        child_key["lifecycle_key"], attempt["attempt_index"], attempt["attempt_kind"],
                        request, attempt["ledger_anchor_hash"], attempt["status"],
                        fallback["envelope_fingerprint"], _canonical_bytes(fallback).decode("utf-8"),
                        attempt["fallback_bytes"], attempt["fallback_hash"],
                        terminal["envelope_fingerprint"] if terminal else None,
                        _canonical_bytes(terminal).decode("utf-8") if terminal else None,
                        attempt["accepted_bytes"], attempt["accepted_hash"],
                        chosen["delivery_proof"]["delivery"]["logical_message_id"],
                        chosen["delivery_proof"]["delivery"]["selected_artifact_digest"], now, now,
                    ),
                )

    def assert_fork_prefix_ready(
        self, source_branch: str, at_pos: int, fork_turn: int
    ) -> None:
        """Fail before mutation unless every inherited semantic turn has a terminal artifact.

        A semantic lifecycle must be selected by both cuts or by neither: its accepted Player input
        is inside ``[0, at_pos)`` exactly when its turn is not later than ``fork_turn``.  Allowing
        either axis alone would copy transcript without state/proof, or state without its transcript
        and proof.  Every selected active attempt is revalidated so a reserved, missing,
        nonterminal, or corrupt artifact cannot be laundered into a child branch.
        """
        if isinstance(at_pos, bool) or not isinstance(at_pos, int) or at_pos < 0:
            raise TurnReservationConflict("semantic fork position is invalid")
        if isinstance(fork_turn, bool) or not isinstance(fork_turn, int):
            raise TurnReservationConflict("semantic fork turn is invalid")
        with self._lock:
            source = self.db.execute(
                "SELECT session_id, head_turn FROM branches WHERE branch_id=?",
                (source_branch,),
            ).fetchone()
            if source is None:
                raise TurnReservationConflict("semantic fork source branch does not exist")

            messages = self.db.execute(
                "SELECT pos, role, content_hash, chain_hash FROM branch_msgs"
                " WHERE branch_id=? ORDER BY pos",
                (source_branch,),
            ).fetchall()
            if any(int(message["pos"]) != index for index, message in enumerate(messages)):
                raise TurnReservationConflict("semantic fork source transcript is not contiguous")
            if at_pos > len(messages):
                raise TurnReservationConflict("semantic fork position exceeds source transcript")

            # A stored chain hash is evidence only when it still commits to the exact canonical
            # role/content rows that precede it.  Checking only the lifecycle's accepted head left
            # the Player row itself (and earlier assistant/text rows) replaceable while retaining a
            # plausible-looking terminal artifact.  Rebuild every selected prefix link from the
            # canonical seed before any child, relink, or edit mutation is allowed.
            canonical_head = SEED
            try:
                for message in messages[:at_pos]:
                    role = message["role"]
                    content = message["content_hash"]
                    if not isinstance(role, str) or not isinstance(content, str) \
                            or _HEX16.fullmatch(content) is None:
                        raise ValueError("invalid canonical message identity")
                    canonical_head = hashlib.blake2b(
                        canonical_head + role.encode() + bytes.fromhex(content),
                        digest_size=8,
                    ).digest()
                    if message["chain_hash"] != canonical_head.hex():
                        raise ValueError("stored canonical chain differs")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise TurnReservationConflict(
                    "semantic fork source transcript chain is corrupt"
                ) from exc

            turns = self.db.execute(
                "SELECT turn_index, user_hash FROM turns WHERE branch_id=? ORDER BY turn_index",
                (source_branch,),
            ).fetchall()
            prefix = messages[:at_pos]
            player_count = sum(
                1 for message in prefix if message["role"] in {"user", "text"}
            )
            if player_count == 0:
                expected_fork_turn = -1
            else:
                if player_count > len(turns):
                    raise TurnReservationConflict(
                        "semantic fork transcript has no matching recorded turn"
                    )
                expected_fork_turn = int(turns[player_count - 1]["turn_index"])
            if fork_turn != expected_fork_turn:
                raise TurnReservationConflict(
                    "semantic fork cut splits transcript and turn source prefix"
                )
            if fork_turn > int(source["head_turn"]):
                raise TurnReservationConflict("semantic fork turn exceeds source branch head")

            # Do not select by the mutable physical branch column first.  A detached lifecycle row
            # could otherwise disappear from this source and be laundered as a legacy turn.  The
            # sealed key is the authority; reconcile every row globally, then select this source by
            # that sealed identity.
            sealed_lifecycles: list[tuple[sqlite3.Row, dict[str, Any]]] = []
            for lifecycle in self.db.execute(
                "SELECT * FROM semantic_turn_lifecycles ORDER BY created_at, lifecycle_key"
            ).fetchall():
                try:
                    sealed_key = validate_pre_mutation_key(
                        json.loads(lifecycle["key_json"])
                    )
                except (
                    TurnArtifactError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    KeyError,
                ) as exc:
                    raise TurnReservationConflict(
                        "semantic fork lifecycle key is corrupt"
                    ) from exc
                key_identity = (
                    (sealed_key["lifecycle_key"], lifecycle["lifecycle_key"]),
                    (sealed_key["session_id"], lifecycle["session_id"]),
                    (sealed_key["branch_id"], lifecycle["branch_id"]),
                    (sealed_key["turn_index"], int(lifecycle["turn_index"])),
                    (sealed_key["accepted_prefix_pos"],
                     int(lifecycle["accepted_prefix_pos"])),
                    (sealed_key["accepted_head_hash"], lifecycle["accepted_head_hash"]),
                    (sealed_key["player_input_hash"], lifecycle["player_input_hash"]),
                    (sealed_key["semantic_contract_version"],
                     lifecycle["semantic_contract_version"]),
                )
                if any(left != right for left, right in key_identity):
                    raise TurnReservationConflict(
                        "semantic fork lifecycle columns differ from its sealed key"
                    )
                sealed_lifecycles.append((lifecycle, sealed_key))

            lifecycles = [
                (lifecycle, sealed_key)
                for lifecycle, sealed_key in sealed_lifecycles
                if sealed_key["branch_id"] == source_branch
            ]
            lifecycles.sort(key=lambda pair: int(pair[0]["turn_index"]))
            for lifecycle, sealed_key in lifecycles:
                inherits_transcript = int(lifecycle["accepted_prefix_pos"]) < at_pos
                inherits_turn = int(lifecycle["turn_index"]) <= fork_turn
                if inherits_transcript != inherits_turn:
                    raise TurnReservationConflict(
                        "semantic fork cut splits transcript and turn lifecycle"
                    )
                if not inherits_transcript:
                    continue
                if lifecycle["branch_id"] != source_branch \
                        or lifecycle["session_id"] != source["session_id"]:
                    raise TurnReservationConflict(
                        "semantic fork lifecycle differs from its source branch identity"
                    )
                player_pos = int(lifecycle["accepted_prefix_pos"])
                if player_pos >= len(messages):
                    raise TurnReservationConflict(
                        "semantic fork lifecycle Player position is missing"
                    )
                player_message = messages[player_pos]
                if player_message["role"] not in {"user", "text"} \
                        or player_message["content_hash"] != lifecycle["player_input_hash"]:
                    raise TurnReservationConflict(
                        "semantic fork lifecycle differs from its source Player message"
                    )
                actual_head = (
                    EMPTY_PREFIX_HASH
                    if player_pos == 0
                    else messages[player_pos - 1]["chain_hash"]
                )
                if actual_head != lifecycle["accepted_head_hash"]:
                    raise TurnReservationConflict(
                        "semantic fork lifecycle differs from its source transcript head"
                    )
                player_ordinal = sum(
                    1
                    for message in messages[:player_pos + 1]
                    if message["role"] in {"user", "text"}
                )
                if player_ordinal <= 0 or player_ordinal > len(turns):
                    raise TurnReservationConflict(
                        "semantic fork lifecycle has no matching source turn"
                    )
                source_turn = turns[player_ordinal - 1]
                if int(source_turn["turn_index"]) != int(lifecycle["turn_index"]) \
                        or source_turn["user_hash"] != lifecycle["player_input_hash"]:
                    raise TurnReservationConflict(
                        "semantic fork lifecycle differs from its exact source turn"
                    )
                active_index = lifecycle["active_attempt_index"]
                if lifecycle["status"] != "committed" or active_index is None:
                    raise TurnReservationConflict(
                        "semantic fork prefix contains a reserved or nonterminal lifecycle"
                    )
                active_index = int(active_index)
                if active_index < 0:
                    raise TurnReservationConflict(
                        "semantic fork prefix has no terminal active attempt"
                    )
                attempts = self.db.execute(
                    "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=?"
                    " AND attempt_index<=? ORDER BY attempt_index",
                    (lifecycle["lifecycle_key"], active_index),
                ).fetchall()
                if [int(attempt["attempt_index"]) for attempt in attempts] \
                        != list(range(active_index + 1)):
                    raise TurnReservationConflict(
                        "semantic fork prefix attempt history is incomplete"
                    )
                for attempt in attempts:
                    if attempt["status"] not in {
                        "fallback_ready", "accepted", "fallback_final",
                    }:
                        raise TurnReservationConflict(
                            "semantic fork prefix contains an older nonterminal attempt"
                        )
                    self._replay_row(attempt)

    @staticmethod
    def _rebase_envelope(
        envelope: dict[str, Any], child_key: dict[str, Any], request_hash: str,
        source_lifecycle_key: str, source_envelope_fingerprint: str,
    ) -> dict[str, Any]:
        value = copy.deepcopy(envelope)
        value["lifecycle_key"] = child_key["lifecycle_key"]
        value["pre_mutation_key"] = child_key
        value["attempt"]["request_hash"] = request_hash
        value["lineage"] = {
            "source_lifecycle_key": source_lifecycle_key,
            "source_envelope_fingerprint": source_envelope_fingerprint,
        }
        diagnostics = value.get("diagnostics")
        if isinstance(diagnostics, dict) \
                and "persisted_narration_basis" in diagnostics:
            # Local import avoids the module-level lifecycle/basis validation cycle.  Forking
            # rotates only the basis's current branch; the source transition, truth contract,
            # journal window, realization plan basis, and accepted/fallback bytes remain exact.
            from .narration_artifact_basis import rebind_persisted_narration_basis

            diagnostics["persisted_narration_basis"] = rebind_persisted_narration_basis(
                diagnostics["persisted_narration_basis"],
                branch_ref=child_key["branch_id"],
            )
        message_id = logical_message_id(child_key)
        value["delivery_proof"] = _rebind_delivery_message(
            value["delivery_proof"], message_id
        )
        delivery = value["delivery_proof"]["delivery"]
        receipt = value["delivery_proof"]["gate_receipt"]
        value["gate"]["receipt_fingerprint"] = receipt["receipt_fingerprint"]
        value["output"]["selected_artifact_digest"] = delivery["selected_artifact_digest"]
        value.pop("envelope_fingerprint", None)
        value["envelope_fingerprint"] = fingerprint(value)
        return value

    def delete_after(self, branch_id: str, turn_index: int) -> None:
        keys = [row["lifecycle_key"] for row in self.db.execute(
            "SELECT lifecycle_key FROM semantic_turn_lifecycles"
            " WHERE branch_id=? AND turn_index>?", (branch_id, turn_index)
        ).fetchall()]
        if keys:
            marks = ",".join("?" for _ in keys)
            self.db.execute(
                f"DELETE FROM semantic_turn_delivery_completions"
                f" WHERE lifecycle_key IN ({marks})", keys
            )
            self.db.execute(
                f"DELETE FROM semantic_turn_delivery_claims WHERE lifecycle_key IN ({marks})", keys
            )
            self.db.execute(
                f"DELETE FROM semantic_turn_attempts WHERE lifecycle_key IN ({marks})", keys
            )
        self.db.execute(
            "DELETE FROM semantic_turn_lifecycles WHERE branch_id=? AND turn_index>?",
            (branch_id, turn_index),
        )

    def delete_branch(self, branch_id: str) -> None:
        keys = [row["lifecycle_key"] for row in self.db.execute(
            "SELECT lifecycle_key FROM semantic_turn_lifecycles WHERE branch_id=?", (branch_id,)
        ).fetchall()]
        if keys:
            marks = ",".join("?" for _ in keys)
            self.db.execute(
                f"DELETE FROM semantic_turn_delivery_completions"
                f" WHERE lifecycle_key IN ({marks})", keys
            )
            self.db.execute(
                f"DELETE FROM semantic_turn_delivery_claims WHERE lifecycle_key IN ({marks})", keys
            )
            self.db.execute(
                f"DELETE FROM semantic_turn_attempts WHERE lifecycle_key IN ({marks})", keys
            )
        self.db.execute("DELETE FROM semantic_turn_lifecycles WHERE branch_id=?", (branch_id,))
