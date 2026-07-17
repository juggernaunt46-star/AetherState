"""Pure orchestration from one durable fallback to one bounded narration candidate.

This module performs no I/O, reducer call, pointer update, or ledger mutation.  It validates an
already proof-complete fallback and its embedded persisted narration basis, builds the isolated
IDs-only selector request, validates one buffered response, and constructs a promotion candidate.
Every failure returns the caller's original fallback artifact unchanged.  The lifecycle owner must
still atomically recheck the active attempt, delivery claim, and fallback fingerprint when it
promotes the returned candidate.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .capability_glossary import content_fingerprint
from .narration_artifact_basis import (
    NarrationArtifactBasisError,
    extract_persisted_narration_basis,
)
from .narration_plan_runtime import (
    NarrationPlanRuntimeError,
    ProofCompleteNarrationCandidate,
    build_proof_complete_narration_candidate,
)
from .semantic_selection_transport import (
    SealedSelectionTransportRequest,
    SemanticSelectionTransportError,
    build_semantic_selection_request,
    parse_semantic_selection_response,
)
from .turn_lifecycle import (
    EnvelopeArtifact,
    TurnArtifactError,
    build_envelope,
    logical_message_id,
    raw_fingerprint,
    validate_envelope,
)


FALLBACK_PROMOTION_EXPECTATION_SCHEMA = "fallback-promotion-expectation/1"
PREPARED_NARRATION_SELECTION_SCHEMA = "prepared-narration-selection/1"

_FALLBACK_READY_STATUS = "fallback_ready"
_JSON_CONTENT_TYPE = "application/json"
_SSE_CONTENT_TYPE = "text/event-stream"


class SemanticNarrationOrchestratorError(ValueError):
    """A fallback, persisted plan, selection, or candidate identity is inconsistent."""


@dataclass(frozen=True)
class FallbackPromotionExpectation:
    """Lifecycle facts that the storage owner must read and later CAS again at promotion."""

    lifecycle_key: str
    branch_id: str
    turn_index: int
    attempt_index: int
    active_attempt_index: int
    fallback_envelope_fingerprint: str
    post_ledger_hash: str
    lifecycle_status: str
    delivery_claimed: bool
    fingerprint: str


@dataclass(frozen=True)
class PreparedNarrationSelection:
    """Sealed request plus the exact fallback and plan identities it may answer for."""

    transport_request: SealedSelectionTransportRequest
    fallback_identity_fingerprint: str
    fallback_envelope_fingerprint: str
    persisted_basis_fingerprint: str
    plan_fingerprint: str
    logical_message_identity: str
    output_content_type: str
    stream: bool
    lifecycle_status: str
    delivery_claimed: bool
    fingerprint: str


@dataclass(frozen=True)
class NarrationSelectionPreparation:
    """Pure preparation decision: send the sealed request or replay the given fallback."""

    action: str
    reason_code: str
    fallback_artifact: EnvelopeArtifact
    prepared: PreparedNarrationSelection | None


@dataclass(frozen=True)
class NarrationPromotionDecision:
    """Pure terminal decision for the lifecycle owner to CAS-promote or replay unchanged."""

    action: str
    reason_code: str
    artifact: EnvelopeArtifact
    prepared_fingerprint: str | None
    selection_fingerprint: str | None
    candidate_fingerprint: str | None


def _expectation_payload(
    *,
    lifecycle_key: str,
    branch_id: str,
    turn_index: int,
    attempt_index: int,
    active_attempt_index: int,
    fallback_envelope_fingerprint: str,
    post_ledger_hash: str,
    lifecycle_status: str,
    delivery_claimed: bool,
) -> dict[str, Any]:
    return {
        "schema": FALLBACK_PROMOTION_EXPECTATION_SCHEMA,
        "lifecycle_key": lifecycle_key,
        "branch_id": branch_id,
        "turn_index": turn_index,
        "attempt_index": attempt_index,
        "active_attempt_index": active_attempt_index,
        "fallback_envelope_fingerprint": fallback_envelope_fingerprint,
        "post_ledger_hash": post_ledger_hash,
        "lifecycle_status": lifecycle_status,
        "delivery_claimed": delivery_claimed,
    }


def _require_lifecycle_index(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticNarrationOrchestratorError(
            f"{label} must be a non-negative integer"
        )
    return value


def build_fallback_promotion_expectation(
    fallback_artifact: EnvelopeArtifact,
    *,
    lifecycle_status: str = _FALLBACK_READY_STATUS,
    active_attempt_index: int | None = None,
    delivery_claimed: bool = False,
) -> FallbackPromotionExpectation:
    """Seal lifecycle facts around a validated fallback without changing it.

    ``lifecycle_status``, ``active_attempt_index``, and ``delivery_claimed`` must come from the
    lifecycle store in production.  This pure record is evidence for orchestration, not a
    substitute for the promotion transaction's final CAS.
    """
    envelope = validate_envelope(fallback_artifact)
    extract_persisted_narration_basis(fallback_artifact)
    key = envelope["pre_mutation_key"]
    attempt = envelope["attempt"]
    active = attempt["index"] if active_attempt_index is None else active_attempt_index
    active = _require_lifecycle_index(active, "active_attempt_index")
    payload = _expectation_payload(
        lifecycle_key=envelope["lifecycle_key"],
        branch_id=key["branch_id"],
        turn_index=key["turn_index"],
        attempt_index=attempt["index"],
        active_attempt_index=active,
        fallback_envelope_fingerprint=envelope["envelope_fingerprint"],
        post_ledger_hash=envelope["ledger"]["mechanics_post_hash"],
        lifecycle_status=lifecycle_status,
        delivery_claimed=delivery_claimed,
    )
    return FallbackPromotionExpectation(
        lifecycle_key=payload["lifecycle_key"],
        branch_id=payload["branch_id"],
        turn_index=payload["turn_index"],
        attempt_index=payload["attempt_index"],
        active_attempt_index=payload["active_attempt_index"],
        fallback_envelope_fingerprint=payload["fallback_envelope_fingerprint"],
        post_ledger_hash=payload["post_ledger_hash"],
        lifecycle_status=payload["lifecycle_status"],
        delivery_claimed=payload["delivery_claimed"],
        fingerprint=content_fingerprint(payload),
    )


def _validate_expectation(
    expectation: object,
    envelope: Mapping[str, Any],
) -> FallbackPromotionExpectation:
    if not isinstance(expectation, FallbackPromotionExpectation):
        raise SemanticNarrationOrchestratorError(
            "fallback promotion expectation is not sealed"
        )
    _require_lifecycle_index(expectation.turn_index, "turn_index")
    _require_lifecycle_index(expectation.attempt_index, "attempt_index")
    _require_lifecycle_index(
        expectation.active_attempt_index, "active_attempt_index"
    )
    payload = _expectation_payload(
        lifecycle_key=expectation.lifecycle_key,
        branch_id=expectation.branch_id,
        turn_index=expectation.turn_index,
        attempt_index=expectation.attempt_index,
        active_attempt_index=expectation.active_attempt_index,
        fallback_envelope_fingerprint=expectation.fallback_envelope_fingerprint,
        post_ledger_hash=expectation.post_ledger_hash,
        lifecycle_status=expectation.lifecycle_status,
        delivery_claimed=expectation.delivery_claimed,
    )
    if expectation.fingerprint != content_fingerprint(payload):
        raise SemanticNarrationOrchestratorError(
            "fallback promotion expectation fingerprint is invalid"
        )
    key = envelope["pre_mutation_key"]
    attempt = envelope["attempt"]
    ledger = envelope["ledger"]
    observed = (
        envelope["lifecycle_key"],
        key["branch_id"],
        key["turn_index"],
        attempt["index"],
        envelope["envelope_fingerprint"],
        ledger["mechanics_post_hash"],
    )
    expected = (
        expectation.lifecycle_key,
        expectation.branch_id,
        expectation.turn_index,
        expectation.attempt_index,
        expectation.fallback_envelope_fingerprint,
        expectation.post_ledger_hash,
    )
    if observed != expected:
        raise SemanticNarrationOrchestratorError(
            "fallback promotion expectation is stale or cross-bound"
        )
    if expectation.active_attempt_index != expectation.attempt_index:
        raise SemanticNarrationOrchestratorError(
            "fallback attempt is not the active lifecycle attempt"
        )
    if expectation.lifecycle_status != _FALLBACK_READY_STATUS \
            or expectation.delivery_claimed is not False:
        raise SemanticNarrationOrchestratorError(
            "fallback is terminal or already delivery-claimed"
        )
    return expectation


def _validated_fallback_context(
    fallback_artifact: EnvelopeArtifact,
    expectation: object,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], FallbackPromotionExpectation]:
    envelope = validate_envelope(fallback_artifact)
    if envelope["gate"]["decision"] != "fallback" \
            or envelope["output"]["selected"] != "fallback" \
            or envelope["output"]["accepted_hash"] is not None \
            or envelope["output"]["accepted_size"] is not None \
            or envelope["candidate_declarations"] \
            or envelope["ledger"]["terminal_post_hash"] \
            != envelope["ledger"]["mechanics_post_hash"]:
        raise SemanticNarrationOrchestratorError(
            "narration selection requires one unpromoted proof-complete fallback"
        )
    proof = envelope["delivery_proof"]
    if proof["artifact_kind"] != "fallback" \
            or proof["delivery"]["artifact_kind"] != "fallback" \
            or proof["delivery"]["logical_message_id"] \
            != logical_message_id(envelope["pre_mutation_key"]):
        raise SemanticNarrationOrchestratorError(
            "fallback proof lost its terminal message identity"
        )
    valid_expectation = _validate_expectation(expectation, envelope)
    basis = extract_persisted_narration_basis(fallback_artifact)
    plan = basis["narration_realization_plan"]
    current = basis["current_lifecycle_binding"]
    source = basis["source_lifecycle_binding"]
    if current["branch_ref"] != valid_expectation.branch_id \
            or current["turn_index"] != valid_expectation.turn_index \
            or current["post_ledger_fingerprint"] != valid_expectation.post_ledger_hash \
            or source["journal_window_fingerprint"] \
            != current["source_journal_window_fingerprint"]:
        raise SemanticNarrationOrchestratorError(
            "persisted contract or journal identity differs from fallback lifecycle"
        )
    return envelope, basis, plan, valid_expectation


def _prepared_payload(
    *,
    transport_request: SealedSelectionTransportRequest,
    fallback_identity_fingerprint: str,
    fallback_envelope_fingerprint: str,
    persisted_basis_fingerprint: str,
    plan_fingerprint: str,
    logical_message_identity: str,
    output_content_type: str,
    stream: bool,
    lifecycle_status: str,
    delivery_claimed: bool,
) -> dict[str, Any]:
    return {
        "schema": PREPARED_NARRATION_SELECTION_SCHEMA,
        "transport": {
            "model": transport_request.model,
            "plan_fingerprint": transport_request.plan_fingerprint,
            "plan_request_fingerprint": transport_request.plan_request_fingerprint,
            "request_fingerprint": transport_request.request_fingerprint,
            "observed_request_fingerprint": raw_fingerprint(
                transport_request.request_bytes
            ),
        },
        "fallback_identity_fingerprint": fallback_identity_fingerprint,
        "fallback_envelope_fingerprint": fallback_envelope_fingerprint,
        "persisted_basis_fingerprint": persisted_basis_fingerprint,
        "plan_fingerprint": plan_fingerprint,
        "logical_message_identity": logical_message_identity,
        "output_content_type": output_content_type,
        "stream": stream,
        "lifecycle_status": lifecycle_status,
        "delivery_claimed": delivery_claimed,
    }


def _build_prepared(
    transport_request: SealedSelectionTransportRequest,
    envelope: Mapping[str, Any],
    basis: Mapping[str, Any],
    plan: Mapping[str, Any],
    expectation: FallbackPromotionExpectation,
) -> PreparedNarrationSelection:
    content_type = envelope["output"]["content_type"]
    if content_type not in {_JSON_CONTENT_TYPE, _SSE_CONTENT_TYPE}:
        raise SemanticNarrationOrchestratorError(
            "fallback wire type cannot be preserved by the candidate adapter"
        )
    stream = content_type == _SSE_CONTENT_TYPE
    message_id = envelope["delivery_proof"]["delivery"]["logical_message_id"]
    payload = _prepared_payload(
        transport_request=transport_request,
        fallback_identity_fingerprint=expectation.fingerprint,
        fallback_envelope_fingerprint=envelope["envelope_fingerprint"],
        persisted_basis_fingerprint=basis["fingerprint"],
        plan_fingerprint=plan["fingerprint"],
        logical_message_identity=message_id,
        output_content_type=content_type,
        stream=stream,
        lifecycle_status=expectation.lifecycle_status,
        delivery_claimed=expectation.delivery_claimed,
    )
    return PreparedNarrationSelection(
        transport_request=transport_request,
        fallback_identity_fingerprint=payload["fallback_identity_fingerprint"],
        fallback_envelope_fingerprint=payload["fallback_envelope_fingerprint"],
        persisted_basis_fingerprint=payload["persisted_basis_fingerprint"],
        plan_fingerprint=payload["plan_fingerprint"],
        logical_message_identity=payload["logical_message_identity"],
        output_content_type=payload["output_content_type"],
        stream=payload["stream"],
        lifecycle_status=payload["lifecycle_status"],
        delivery_claimed=payload["delivery_claimed"],
        fingerprint=content_fingerprint(payload),
    )


def _validate_prepared(
    prepared: object,
    envelope: Mapping[str, Any],
    basis: Mapping[str, Any],
    plan: Mapping[str, Any],
    expectation: FallbackPromotionExpectation,
) -> PreparedNarrationSelection:
    if not isinstance(prepared, PreparedNarrationSelection):
        raise SemanticNarrationOrchestratorError(
            "narration selection preparation is not sealed"
        )
    payload = _prepared_payload(
        transport_request=prepared.transport_request,
        fallback_identity_fingerprint=prepared.fallback_identity_fingerprint,
        fallback_envelope_fingerprint=prepared.fallback_envelope_fingerprint,
        persisted_basis_fingerprint=prepared.persisted_basis_fingerprint,
        plan_fingerprint=prepared.plan_fingerprint,
        logical_message_identity=prepared.logical_message_identity,
        output_content_type=prepared.output_content_type,
        stream=prepared.stream,
        lifecycle_status=prepared.lifecycle_status,
        delivery_claimed=prepared.delivery_claimed,
    )
    if prepared.fingerprint != content_fingerprint(payload):
        raise SemanticNarrationOrchestratorError(
            "narration selection preparation fingerprint is invalid"
        )
    expected = (
        expectation.fingerprint,
        envelope["envelope_fingerprint"],
        basis["fingerprint"],
        plan["fingerprint"],
        envelope["delivery_proof"]["delivery"]["logical_message_id"],
        envelope["output"]["content_type"],
        envelope["output"]["content_type"] == _SSE_CONTENT_TYPE,
        expectation.lifecycle_status,
        expectation.delivery_claimed,
    )
    observed = (
        prepared.fallback_identity_fingerprint,
        prepared.fallback_envelope_fingerprint,
        prepared.persisted_basis_fingerprint,
        prepared.plan_fingerprint,
        prepared.logical_message_identity,
        prepared.output_content_type,
        prepared.stream,
        prepared.lifecycle_status,
        prepared.delivery_claimed,
    )
    if observed != expected \
            or prepared.transport_request.plan_fingerprint != plan["fingerprint"]:
        raise SemanticNarrationOrchestratorError(
            "prepared selection belongs to another fallback, plan, or delivery state"
        )
    return prepared


def prepare_narration_selection(
    fallback_artifact: EnvelopeArtifact,
    original_request: object,
    expectation: object,
    *,
    reasoning_hard_off: bool = False,
) -> NarrationSelectionPreparation:
    """Return an exact sealed selector request or the unchanged fallback decision."""
    try:
        envelope, basis, plan, valid_expectation = _validated_fallback_context(
            fallback_artifact,
            expectation,
        )
        transport = build_semantic_selection_request(
            plan,
            original_request,
            reasoning_hard_off=reasoning_hard_off,
        )
        prepared = _build_prepared(
            transport,
            envelope,
            basis,
            plan,
            valid_expectation,
        )
        return NarrationSelectionPreparation(
            action="request",
            reason_code="selection_request_ready",
            fallback_artifact=fallback_artifact,
            prepared=prepared,
        )
    except (
        NarrationArtifactBasisError,
        NarrationPlanRuntimeError,
        SemanticNarrationOrchestratorError,
        SemanticSelectionTransportError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ):
        return NarrationSelectionPreparation(
            action="fallback",
            reason_code="selection_request_rejected",
            fallback_artifact=fallback_artifact,
            prepared=None,
        )


def _candidate_envelope(
    fallback_artifact: EnvelopeArtifact,
    envelope: Mapping[str, Any],
    basis: Mapping[str, Any],
    candidate: ProofCompleteNarrationCandidate,
) -> EnvelopeArtifact:
    runtime = envelope["runtime"]
    ledger = envelope["ledger"]
    pending = envelope["pending_intent"]
    lineage = envelope["lineage"]
    terminal = build_envelope(
        pre_mutation_key=envelope["pre_mutation_key"],
        attempt_index=envelope["attempt"]["index"],
        attempt_kind=envelope["attempt"]["kind"],
        request_hash=envelope["attempt"]["request_hash"],
        occurrences=envelope["occurrences"],
        effects=envelope["effects"],
        rng_fingerprint=runtime["rng_fingerprint"],
        config_fingerprint=runtime["config_fingerprint"],
        engine_version=runtime["engine_version"],
        pre_ledger_hash=ledger["pre_hash"],
        mechanics_post_ledger_hash=ledger["mechanics_post_hash"],
        terminal_post_ledger_hash=ledger["terminal_post_hash"],
        fallback_bytes=fallback_artifact.fallback_bytes,
        delivery_proof=candidate.delivery_proof,
        decision="accept",
        accepted_bytes=candidate.wire_bytes,
        candidate_declarations=envelope["candidate_declarations"],
        consumed_intent_id=pending["consumed_intent_id"],
        next_intent_id=pending["next_intent_id"],
        gate_reason_code="proved_plan_selection",
        diagnostics=envelope["diagnostics"],
        source_lifecycle_key=lineage["source_lifecycle_key"],
        source_envelope_fingerprint=lineage["source_envelope_fingerprint"],
    )
    validated = validate_envelope(terminal)
    stable_fields = set(envelope) - {
        "gate",
        "delivery_proof",
        "output",
        "envelope_fingerprint",
    }
    if any(validated[field] != envelope[field] for field in stable_fields):
        raise SemanticNarrationOrchestratorError(
            "candidate envelope changed frozen fallback truth or identity"
        )
    if validated["output"]["content_type"] != envelope["output"]["content_type"] \
            or validated["output"]["fallback_hash"] != envelope["output"]["fallback_hash"] \
            or validated["output"]["fallback_size"] != envelope["output"]["fallback_size"] \
            or terminal.fallback_bytes is not fallback_artifact.fallback_bytes \
            or terminal.fallback_bytes != fallback_artifact.fallback_bytes \
            or validated["delivery_proof"]["delivery"]["logical_message_id"] \
            != envelope["delivery_proof"]["delivery"]["logical_message_id"]:
        raise SemanticNarrationOrchestratorError(
            "candidate envelope changed immutable fallback replay or delivery identity"
        )
    candidate_basis = extract_persisted_narration_basis(terminal)
    if candidate_basis["fingerprint"] != basis["fingerprint"]:
        raise SemanticNarrationOrchestratorError(
            "candidate envelope changed persisted contract or journal identity"
        )
    return terminal


def _fallback_decision(
    fallback_artifact: EnvelopeArtifact,
    *,
    reason_code: str,
    prepared_fingerprint: str | None,
) -> NarrationPromotionDecision:
    return NarrationPromotionDecision(
        action="fallback",
        reason_code=reason_code,
        artifact=fallback_artifact,
        prepared_fingerprint=prepared_fingerprint,
        selection_fingerprint=None,
        candidate_fingerprint=None,
    )


def resolve_narration_selection(
    preparation: object,
    fallback_artifact: EnvelopeArtifact,
    expectation: object,
    *,
    response_bytes: bytes | None,
    response_content_type: str | None,
    timed_out: bool = False,
) -> NarrationPromotionDecision:
    """Resolve one buffered selection into a pure promote/fallback decision.

    The returned ``promote`` artifact is only a candidate.  The lifecycle owner must atomically
    recheck the same expectation, active pointer, and absence of a delivery claim before storing it.
    """
    prepared_fp = (
        preparation.prepared.fingerprint
        if isinstance(preparation, NarrationSelectionPreparation)
        and isinstance(preparation.prepared, PreparedNarrationSelection)
        else None
    )
    if timed_out:
        return _fallback_decision(
            fallback_artifact,
            reason_code="selection_timeout",
            prepared_fingerprint=prepared_fp,
        )
    if not isinstance(preparation, NarrationSelectionPreparation) \
            or preparation.action != "request" \
            or preparation.prepared is None \
            or preparation.fallback_artifact is not fallback_artifact:
        return _fallback_decision(
            fallback_artifact,
            reason_code="selection_not_prepared",
            prepared_fingerprint=prepared_fp,
        )

    try:
        envelope, basis, plan, valid_expectation = _validated_fallback_context(
            fallback_artifact,
            expectation,
        )
        prepared = _validate_prepared(
            preparation.prepared,
            envelope,
            basis,
            plan,
            valid_expectation,
        )
        selection = parse_semantic_selection_response(
            prepared.transport_request,
            plan,
            response_bytes,
            response_content_type,
        )
    except (
        NarrationArtifactBasisError,
        NarrationPlanRuntimeError,
        SemanticNarrationOrchestratorError,
        SemanticSelectionTransportError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ):
        return _fallback_decision(
            fallback_artifact,
            reason_code="selection_invalid_or_stale",
            prepared_fingerprint=prepared_fp,
        )

    try:
        candidate = build_proof_complete_narration_candidate(
            plan,
            selection,
            model=prepared.transport_request.model,
            stream=prepared.stream,
            logical_message_identity=prepared.logical_message_identity,
        )
        terminal = _candidate_envelope(
            fallback_artifact,
            envelope,
            basis,
            candidate,
        )
        if validate_envelope(fallback_artifact) != envelope:
            raise SemanticNarrationOrchestratorError(
                "candidate construction mutated its fallback input"
            )
        return NarrationPromotionDecision(
            action="promote",
            reason_code="proof_complete_candidate",
            artifact=terminal,
            prepared_fingerprint=prepared.fingerprint,
            selection_fingerprint=candidate.rendered.selection_fingerprint,
            candidate_fingerprint=candidate.fingerprint,
        )
    except (
        NarrationArtifactBasisError,
        NarrationPlanRuntimeError,
        SemanticNarrationOrchestratorError,
        SemanticSelectionTransportError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ):
        return _fallback_decision(
            fallback_artifact,
            reason_code="candidate_proof_rejected",
            prepared_fingerprint=prepared_fp,
        )
