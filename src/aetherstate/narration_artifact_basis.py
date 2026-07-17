"""Immutable transition, truth, and narration-plan basis for terminal artifacts.

The production gate needs more than three detached fingerprints.  This module seals the complete
validated source transition projection, source narration truth contract, and current finite
narration plan into one JSON record.  Source truth never changes on a fork; only the plan's current
branch binding may rotate through the persisted-basis rebind below.

The envelope helpers are deliberately pure.  They retain every existing fallback/accepted byte
and every proof field, add the validated basis under diagnostics, and recompute only the outer
envelope fingerprint.  They own no storage, lifecycle mutation, delivery, or model interaction.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from .capability_glossary import content_fingerprint
from .narration_plan_runtime import (
    NarrationPlanRuntimeError,
    build_narration_realization_plan,
    validate_narration_realization_plan,
)
from .narration_truth_gate import (
    NarrationTruthGateError,
    validate_narration_truth_contract,
)
from .semantic_transition_truth import (
    SemanticTransitionTruthError,
    validate_transition_projection,
)
from .turn_lifecycle import (
    EnvelopeArtifact,
    ReplayArtifact,
    TurnArtifactError,
    TurnReservation,
    build_envelope,
    fingerprint,
    logical_message_id,
    raw_fingerprint,
    validate_delivery_proof,
    validate_envelope,
    validate_pre_mutation_key,
)


PERSISTED_NARRATION_BASIS_SCHEMA = "persisted-narration-artifact-basis/1"
NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY = "persisted_narration_basis"

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_BASIS_FIELDS = {
    "schema",
    "source_transition_projection",
    "source_transition_projection_fingerprint",
    "source_truth_contract",
    "source_truth_contract_fingerprint",
    "narration_realization_plan",
    "narration_plan_fingerprint",
    "source_lifecycle_binding",
    "current_lifecycle_binding",
    "fingerprint",
}
_SOURCE_LIFECYCLE_FIELDS = {
    "branch_ref",
    "turn_index",
    "pre_ledger_fingerprint",
    "post_ledger_fingerprint",
    "journal_window_fingerprint",
    "transition_projection_fingerprint",
    "truth_contract_fingerprint",
    "narration_plan_fingerprint",
    "artifact_fingerprint",
}
_CURRENT_LIFECYCLE_FIELDS = {
    "branch_ref",
    "turn_index",
    "post_ledger_fingerprint",
    "artifact_fingerprint",
    "source_branch_ref",
    "source_journal_window_fingerprint",
    "source_transition_projection_fingerprint",
    "source_truth_contract_fingerprint",
    "narration_plan_fingerprint",
}


class NarrationArtifactBasisError(ValueError):
    """A persisted narration basis or its replay-envelope binding is not exact."""


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NarrationArtifactBasisError("narration artifact basis must be finite JSON") from exc


def _json_copy(value: object) -> Any:
    return json.loads(_json_bytes(value).decode("utf-8"))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise NarrationArtifactBasisError(f"{label} must be an object with string fields")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise NarrationArtifactBasisError(f"{label} fields are not exact")


def _require_fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NarrationArtifactBasisError(f"{label} must be a sha256 fingerprint")
    return value


def _validated_records(
    transition_projection: object,
    truth_contract: object,
    narration_plan: object,
    *,
    require_current_code_origin: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    transition = validate_transition_projection(_json_copy(transition_projection))
    contract = validate_narration_truth_contract(_json_copy(truth_contract))
    plan = validate_narration_realization_plan(_json_copy(narration_plan))
    if transition["delivery_phase"] != "pre_display":
        raise NarrationArtifactBasisError(
            "persisted production narration basis requires a pre-display transition"
        )
    lifecycle = contract["lifecycle_binding"]
    if lifecycle is None:
        raise NarrationArtifactBasisError("source truth contract has no lifecycle binding")

    if require_current_code_origin:
        origin_plan = build_narration_realization_plan(contract)
    else:
        # Reconstruct only the persisted source-branch identity.  Validation/reopen/swipe/fork
        # must not silently replace the frozen plan with whatever a newer builder would author.
        origin_payload = _json_copy(plan)
        origin_payload["lifecycle_binding"] = _json_copy(
            plan["source_lifecycle_binding"]
        )
        origin_payload.pop("fingerprint", None)
        origin_plan = {
            **origin_payload,
            "fingerprint": content_fingerprint(origin_payload),
        }
    current_payload = _json_copy(origin_plan)
    current_payload["lifecycle_binding"] = _json_copy(plan["lifecycle_binding"])
    current_payload.pop("fingerprint", None)
    expected_current_plan = {
        **current_payload,
        "fingerprint": content_fingerprint(current_payload),
    }
    if _json_bytes(plan) != _json_bytes(expected_current_plan):
        raise NarrationArtifactBasisError(
            "narration plan is not the exact source-contract plan or its branch-only rebind"
        )

    source_plan_binding = plan["source_lifecycle_binding"]
    current_plan_binding = plan["lifecycle_binding"]
    source_identity = (
        transition["branch_id"],
        transition["turn_index"],
        transition["post_ledger_hash"],
        contract["realization_fingerprint"],
    )
    contract_identity = (
        lifecycle["branch_ref"],
        contract["turn"],
        lifecycle["ledger_fingerprint"],
        lifecycle["artifact_fingerprint"],
    )
    plan_source_identity = (
        source_plan_binding["branch_ref"],
        plan["turn"],
        source_plan_binding["ledger_fingerprint"],
        source_plan_binding["artifact_fingerprint"],
    )
    plan_current_identity = (
        current_plan_binding["branch_ref"],
        plan["turn"],
        current_plan_binding["ledger_fingerprint"],
        current_plan_binding["artifact_fingerprint"],
    )
    if source_identity != contract_identity or source_identity != plan_source_identity:
        raise NarrationArtifactBasisError(
            "transition, truth contract, and source plan lifecycle identities differ"
        )
    if plan_current_identity[1:] != source_identity[1:]:
        raise NarrationArtifactBasisError(
            "current plan changed the source turn, post-ledger, or artifact identity"
        )
    if plan["source_truth_contract_fingerprint"] != contract["fingerprint"]:
        raise NarrationArtifactBasisError("narration plan belongs to another truth contract")
    return transition, contract, plan, origin_plan


def _basis_from_records(
    transition: Mapping[str, Any],
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    origin_plan: Mapping[str, Any],
) -> dict[str, Any]:
    source = {
        "branch_ref": transition["branch_id"],
        "turn_index": transition["turn_index"],
        "pre_ledger_fingerprint": transition["pre_ledger_hash"],
        "post_ledger_fingerprint": transition["post_ledger_hash"],
        "journal_window_fingerprint": transition["journal_window_fingerprint"],
        "transition_projection_fingerprint": transition["fingerprint"],
        "truth_contract_fingerprint": contract["fingerprint"],
        "narration_plan_fingerprint": origin_plan["fingerprint"],
        "artifact_fingerprint": contract["realization_fingerprint"],
    }
    current = {
        "branch_ref": plan["lifecycle_binding"]["branch_ref"],
        "turn_index": plan["turn"],
        "post_ledger_fingerprint": plan["lifecycle_binding"]["ledger_fingerprint"],
        "artifact_fingerprint": plan["lifecycle_binding"]["artifact_fingerprint"],
        "source_branch_ref": source["branch_ref"],
        "source_journal_window_fingerprint": source["journal_window_fingerprint"],
        "source_transition_projection_fingerprint": transition["fingerprint"],
        "source_truth_contract_fingerprint": contract["fingerprint"],
        "narration_plan_fingerprint": plan["fingerprint"],
    }
    payload = {
        "schema": PERSISTED_NARRATION_BASIS_SCHEMA,
        "source_transition_projection": _json_copy(transition),
        "source_transition_projection_fingerprint": transition["fingerprint"],
        "source_truth_contract": _json_copy(contract),
        "source_truth_contract_fingerprint": contract["fingerprint"],
        "narration_realization_plan": _json_copy(plan),
        "narration_plan_fingerprint": plan["fingerprint"],
        "source_lifecycle_binding": source,
        "current_lifecycle_binding": current,
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def build_persisted_narration_basis(
    transition_projection: object,
    truth_contract: object,
    narration_plan: object,
) -> dict[str, Any]:
    """Seal one complete production basis from already built immutable records."""
    try:
        records = _validated_records(
            transition_projection,
            truth_contract,
            narration_plan,
            require_current_code_origin=True,
        )
        return _json_copy(_basis_from_records(*records))
    except NarrationArtifactBasisError:
        raise
    except (
        NarrationPlanRuntimeError,
        NarrationTruthGateError,
        SemanticTransitionTruthError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ) as exc:
        raise NarrationArtifactBasisError(
            f"persisted narration basis record validation failed: {exc}"
        ) from exc


def validate_persisted_narration_basis(value: object) -> dict[str, Any]:
    """Validate and detach a complete persisted basis without consulting mutable state."""
    try:
        basis = _mapping(_json_copy(value), "persisted narration basis")
        _exact_fields(basis, _BASIS_FIELDS, "persisted narration basis")
        if basis["schema"] != PERSISTED_NARRATION_BASIS_SCHEMA:
            raise NarrationArtifactBasisError("persisted narration basis schema is unsupported")
        supplied = _require_fingerprint(basis["fingerprint"], "basis fingerprint")
        payload = {key: _json_copy(item) for key, item in basis.items() if key != "fingerprint"}
        if supplied != content_fingerprint(payload):
            raise NarrationArtifactBasisError("persisted narration basis fingerprint mismatch")
        _require_fingerprint(
            basis["source_transition_projection_fingerprint"],
            "source transition projection fingerprint",
        )
        _require_fingerprint(
            basis["source_truth_contract_fingerprint"],
            "source truth contract fingerprint",
        )
        _require_fingerprint(basis["narration_plan_fingerprint"], "narration plan fingerprint")
        _exact_fields(
            _mapping(basis["source_lifecycle_binding"], "source lifecycle binding"),
            _SOURCE_LIFECYCLE_FIELDS,
            "source lifecycle binding",
        )
        _exact_fields(
            _mapping(basis["current_lifecycle_binding"], "current lifecycle binding"),
            _CURRENT_LIFECYCLE_FIELDS,
            "current lifecycle binding",
        )
        records = _validated_records(
            basis["source_transition_projection"],
            basis["source_truth_contract"],
            basis["narration_realization_plan"],
            require_current_code_origin=False,
        )
        expected = _basis_from_records(*records)
        if _json_bytes(expected) != _json_bytes(basis):
            raise NarrationArtifactBasisError(
                "persisted narration basis is not its exact code-authored form"
            )
        return _json_copy(expected)
    except NarrationArtifactBasisError:
        raise
    except (
        NarrationPlanRuntimeError,
        NarrationTruthGateError,
        SemanticTransitionTruthError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ) as exc:
        raise NarrationArtifactBasisError(
            f"persisted narration basis record validation failed: {exc}"
        ) from exc


def rebind_persisted_narration_basis(
    basis: object,
    *,
    branch_ref: str,
) -> dict[str, Any]:
    """Rotate only current fork lineage while preserving source transition and truth bytes."""
    try:
        valid = validate_persisted_narration_basis(basis)
        if not isinstance(branch_ref, str) or not branch_ref:
            raise NarrationArtifactBasisError("persisted basis fork branch is empty")
        rebound_payload = _json_copy(valid["narration_realization_plan"])
        rebound_payload["lifecycle_binding"]["branch_ref"] = branch_ref
        rebound_payload.pop("fingerprint", None)
        rebound_plan = {
            **rebound_payload,
            "fingerprint": content_fingerprint(rebound_payload),
        }
        records = _validated_records(
            valid["source_transition_projection"],
            valid["source_truth_contract"],
            rebound_plan,
            require_current_code_origin=False,
        )
        return _json_copy(_basis_from_records(*records))
    except NarrationArtifactBasisError:
        raise
    except (NarrationPlanRuntimeError, TypeError, ValueError) as exc:
        raise NarrationArtifactBasisError("persisted basis fork rebind failed") from exc


def _validate_envelope_basis_binding(
    envelope: Mapping[str, Any],
    basis: Mapping[str, Any],
) -> None:
    current = _mapping(basis["current_lifecycle_binding"], "current lifecycle binding")
    source = _mapping(basis["source_lifecycle_binding"], "source lifecycle binding")
    key = _mapping(envelope.get("pre_mutation_key"), "envelope pre-mutation key")
    attempt = _mapping(envelope.get("attempt"), "envelope attempt")
    ledger = _mapping(envelope.get("ledger"), "envelope ledger")
    if key.get("branch_id") != current["branch_ref"]:
        raise NarrationArtifactBasisError("envelope branch differs from current narration basis")
    if key.get("turn_index") != current["turn_index"]:
        raise NarrationArtifactBasisError("envelope turn differs from current narration basis")
    if ledger.get("mechanics_post_hash") != current["post_ledger_fingerprint"] \
            or attempt.get("ledger_anchor_hash") != current["post_ledger_fingerprint"]:
        raise NarrationArtifactBasisError(
            "envelope post-ledger differs from current narration basis"
        )
    if current["source_branch_ref"] != source["branch_ref"] \
            or current["source_journal_window_fingerprint"] \
            != source["journal_window_fingerprint"]:
        raise NarrationArtifactBasisError("basis source/current lifecycle linkage is invalid")
    diagnostics = _mapping(envelope.get("diagnostics"), "envelope diagnostics")
    diagnostic_contract = diagnostics.get("contract_fingerprint")
    if diagnostic_contract != basis["source_truth_contract_fingerprint"]:
        raise NarrationArtifactBasisError(
            "envelope fallback contract differs from persisted source truth"
        )


def attach_persisted_narration_basis(
    artifact: EnvelopeArtifact,
    basis: object,
) -> EnvelopeArtifact:
    """Attach a basis by changing only diagnostics and the outer envelope fingerprint."""
    try:
        envelope = validate_envelope(artifact)
        valid_basis = validate_persisted_narration_basis(basis)
        _validate_envelope_basis_binding(envelope, valid_basis)
        diagnostics = _json_copy(
            _mapping(envelope.get("diagnostics"), "envelope diagnostics")
        )
        existing = diagnostics.get(NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY)
        if existing is not None:
            valid_existing = validate_persisted_narration_basis(existing)
            if valid_existing["fingerprint"] != valid_basis["fingerprint"]:
                raise NarrationArtifactBasisError(
                    "envelope already carries a different persisted narration basis"
                )
        diagnostics[NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY] = valid_basis
        rebuilt = _json_copy(envelope)
        rebuilt["diagnostics"] = diagnostics
        rebuilt.pop("envelope_fingerprint", None)
        rebuilt["envelope_fingerprint"] = fingerprint(rebuilt)
        attached = EnvelopeArtifact(
            rebuilt,
            artifact.fallback_bytes,
            artifact.accepted_bytes,
        )
        validated = validate_envelope(attached)
        for field in envelope:
            if field not in {"diagnostics", "envelope_fingerprint"} \
                    and validated[field] != envelope[field]:
                raise NarrationArtifactBasisError(
                    "basis attachment changed frozen envelope truth or proof"
                )
        return attached
    except NarrationArtifactBasisError:
        raise
    except (TurnArtifactError, TypeError, ValueError) as exc:
        raise NarrationArtifactBasisError(
            f"persisted basis envelope attachment failed: {exc}"
        ) from exc


def derive_swipe_fallback_from_persisted_basis(
    source_fallback: EnvelopeArtifact,
    source_replay: ReplayArtifact,
    reservation: TurnReservation,
) -> EnvelopeArtifact:
    """Derive a swipe fallback solely from one persisted proof-carrying fallback.

    A narration retry is not another semantic turn.  Its authority is the exact settled
    occurrence/effect envelope, fallback wire proof, truth contract, realization plan, and
    narration basis already persisted for the active attempt.  Only the attempt/request identity
    and explicit immediate-source lineage may change here; no state, journal, reducer, contract
    builder, fallback renderer, or model is consulted.
    """
    try:
        source = validate_envelope(source_fallback)
        active = _validate_replay_artifact(source_replay)
        if source["gate"]["decision"] != "fallback" \
                or source["output"]["selected"] != "fallback" \
                or source_fallback.accepted_bytes is not None:
            raise NarrationArtifactBasisError(
                "swipe derivation requires the persisted fallback artifact"
            )
        if not isinstance(reservation, TurnReservation) \
                or isinstance(reservation.attempt_index, bool) \
                or not isinstance(reservation.attempt_index, int) \
                or reservation.attempt_index < 0 \
                or reservation.status != "reserved":
            raise NarrationArtifactBasisError(
                "swipe derivation requires one reserved narration attempt"
            )
        if reservation.lifecycle_key != source["lifecycle_key"] \
                or source_replay.lifecycle_key != source["lifecycle_key"]:
            raise NarrationArtifactBasisError(
                "swipe reservation and source fallback belong to different lifecycles"
            )
        if source["attempt"]["index"] != source_replay.attempt_index:
            raise NarrationArtifactBasisError(
                "swipe source fallback is not the selected source replay attempt"
            )
        if reservation.attempt_index <= source_replay.attempt_index:
            raise NarrationArtifactBasisError(
                "swipe attempt must follow its persisted source attempt"
            )
        source_fp = _require_fingerprint(
            active.get("envelope_fingerprint"),
            "source envelope fingerprint",
        )
        stable_source_fields = (
            "lifecycle_key",
            "pre_mutation_key",
            "attempt",
            "occurrences",
            "occurrence_fingerprint",
            "effects",
            "effect_fingerprint",
            "runtime",
            "ledger",
            "pending_intent",
            "candidate_declarations",
            "candidate_declaration_fingerprint",
            "diagnostics",
            "lineage",
        )
        if any(source.get(field) != active.get(field) for field in stable_source_fields) \
                or source["output"]["fallback_hash"] \
                != active["output"]["fallback_hash"] \
                or source["output"]["fallback_size"] \
                != active["output"]["fallback_size"] \
                or source["output"]["content_type"] \
                != active["output"]["content_type"]:
            raise NarrationArtifactBasisError(
                "swipe source fallback differs from its selected source replay"
            )
        basis = extract_persisted_narration_basis(source_fallback)
        runtime = source["runtime"]
        ledger = source["ledger"]
        pending = source["pending_intent"]
        derived = build_envelope(
            pre_mutation_key=source["pre_mutation_key"],
            attempt_index=reservation.attempt_index,
            attempt_kind="swipe",
            request_hash=reservation.request_hash,
            occurrences=source["occurrences"],
            effects=source["effects"],
            rng_fingerprint=runtime["rng_fingerprint"],
            config_fingerprint=runtime["config_fingerprint"],
            engine_version=runtime["engine_version"],
            pre_ledger_hash=ledger["pre_hash"],
            mechanics_post_ledger_hash=ledger["mechanics_post_hash"],
            fallback_bytes=source_fallback.fallback_bytes,
            delivery_proof=source["delivery_proof"],
            consumed_intent_id=pending["consumed_intent_id"],
            next_intent_id=pending["next_intent_id"],
            gate_reason_code=source["gate"]["reason_code"],
            diagnostics=source["diagnostics"],
            source_lifecycle_key=source_replay.lifecycle_key,
            source_envelope_fingerprint=source_fp,
        )
        valid = validate_envelope(derived)
        derived_basis = extract_persisted_narration_basis(derived)
        if derived_basis != basis:
            raise NarrationArtifactBasisError(
                "swipe derivation changed its persisted narration basis"
            )
        immutable_fields = (
            "pre_mutation_key",
            "occurrences",
            "occurrence_fingerprint",
            "effects",
            "effect_fingerprint",
            "runtime",
            "ledger",
            "pending_intent",
            "candidate_declarations",
            "candidate_declaration_fingerprint",
            "gate",
            "delivery_proof",
            "output",
            "diagnostics",
        )
        if any(valid[field] != source[field] for field in immutable_fields) \
                or derived.fallback_bytes != source_fallback.fallback_bytes \
                or derived.accepted_bytes is not None:
            raise NarrationArtifactBasisError(
                "swipe derivation changed persisted semantic truth or proof bytes"
            )
        return derived
    except NarrationArtifactBasisError:
        raise
    except (TurnArtifactError, TypeError, ValueError) as exc:
        raise NarrationArtifactBasisError(
            f"persisted swipe fallback derivation failed: {exc}"
        ) from exc


def _validate_replay_artifact(replay: ReplayArtifact) -> dict[str, Any]:
    envelope = _mapping(_json_copy(replay.envelope), "replay envelope")
    supplied = envelope.get("envelope_fingerprint")
    body = {key: _json_copy(item) for key, item in envelope.items() if key != "envelope_fingerprint"}
    if supplied != fingerprint(body):
        raise NarrationArtifactBasisError("replay envelope fingerprint is invalid")
    key = validate_pre_mutation_key(_mapping(envelope.get("pre_mutation_key"), "replay key"))
    attempt = _mapping(envelope.get("attempt"), "replay attempt")
    ledger = _mapping(envelope.get("ledger"), "replay ledger")
    if replay.lifecycle_key != envelope.get("lifecycle_key") \
            or replay.lifecycle_key != key["lifecycle_key"] \
            or replay.attempt_index != attempt.get("index"):
        raise NarrationArtifactBasisError("replay lifecycle identity differs from its envelope")
    if fingerprint(envelope.get("occurrences")) != envelope.get("occurrence_fingerprint") \
            or fingerprint(envelope.get("effects")) != envelope.get("effect_fingerprint") \
            or fingerprint(envelope.get("candidate_declarations")) \
            != envelope.get("candidate_declaration_fingerprint"):
        raise NarrationArtifactBasisError("replay envelope payload fingerprints are invalid")
    if raw_fingerprint(replay.payload) != replay.payload_hash:
        raise NarrationArtifactBasisError("replay payload hash is invalid")
    gate = _mapping(envelope.get("gate"), "replay gate")
    decision = gate.get("decision")
    output = _mapping(envelope.get("output"), "replay output")
    selected_hash = output.get("fallback_hash" if decision == "fallback" else "accepted_hash")
    selected_size = output.get("fallback_size" if decision == "fallback" else "accepted_size")
    if decision not in {"fallback", "accept"} \
            or selected_hash != replay.payload_hash \
            or selected_size != len(replay.payload) \
            or output.get("content_type") != replay.content_type:
        raise NarrationArtifactBasisError("replay payload differs from selected envelope output")
    proof = validate_delivery_proof(
        _mapping(envelope.get("delivery_proof"), "replay delivery proof"),
        replay.payload,
    )
    delivery = proof["delivery"]
    expected_artifact_kind = "fallback" if decision == "fallback" else "accepted"
    if proof["artifact_kind"] != expected_artifact_kind \
            or proof["ledger_root_hash"] != ledger.get("terminal_post_hash") \
            or proof["gate_receipt"]["receipt_fingerprint"] \
            != gate.get("receipt_fingerprint") \
            or delivery["logical_message_id"] != logical_message_id(key) \
            or delivery["logical_message_id"] != replay.logical_message_id \
            or delivery["selected_artifact_digest"] != replay.selected_artifact_digest \
            or output.get("selected_artifact_digest") != replay.selected_artifact_digest \
            or delivery["content_type"] != replay.content_type:
        raise NarrationArtifactBasisError("replay proof identity differs from selected artifact")
    expected_status = "accepted" if decision == "accept" else {
        "fallback_ready",
        "fallback_final",
    }
    if isinstance(expected_status, str):
        status_valid = replay.status == expected_status
    else:
        status_valid = replay.status in expected_status
    if not status_valid:
        raise NarrationArtifactBasisError("replay status contradicts its envelope decision")
    return _json_copy(envelope)


def extract_persisted_narration_basis(
    replay_envelope: EnvelopeArtifact | ReplayArtifact,
) -> dict[str, Any]:
    """Extract and validate an embedded basis from a proof-carrying live or replay artifact."""
    try:
        if isinstance(replay_envelope, EnvelopeArtifact):
            envelope = validate_envelope(replay_envelope)
        elif isinstance(replay_envelope, ReplayArtifact):
            envelope = _validate_replay_artifact(replay_envelope)
        else:
            raise NarrationArtifactBasisError(
                "basis extraction requires a proof-carrying EnvelopeArtifact or ReplayArtifact"
            )
        diagnostics = _mapping(envelope.get("diagnostics"), "envelope diagnostics")
        embedded = diagnostics.get(NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY)
        if embedded is None:
            raise NarrationArtifactBasisError(
                "replay envelope has no embedded persisted narration basis"
            )
        basis = validate_persisted_narration_basis(embedded)
        _validate_envelope_basis_binding(envelope, basis)
        return basis
    except NarrationArtifactBasisError:
        raise
    except (TurnArtifactError, TypeError, ValueError) as exc:
        raise NarrationArtifactBasisError(
            f"persisted basis replay extraction failed: {exc}"
        ) from exc
