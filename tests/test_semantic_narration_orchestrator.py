from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from aetherstate.canon import content_hash
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_artifact_basis import (
    attach_persisted_narration_basis,
    build_persisted_narration_basis,
    extract_persisted_narration_basis,
)
from aetherstate.narration_fallback_runtime import build_proof_complete_fallback
from aetherstate.narration_plan_runtime import (
    build_default_narration_plan_selection,
    build_narration_realization_plan,
)
from aetherstate.response_wire import decode_chat_story, encode_chat_story
from aetherstate.semantic_narration_orchestrator import (
    SemanticNarrationOrchestratorError,
    build_fallback_promotion_expectation,
    prepare_narration_selection,
    resolve_narration_selection,
)
from aetherstate.semantic_selection_transport import build_semantic_selection_request
from aetherstate.semantic_truth_runtime import build_fenced_runtime_truth_contract
from aetherstate.state import empty_state, reduce_state
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    TurnReservation,
    build_pre_mutation_key,
    fingerprint,
    journal_window_fingerprint,
    validate_envelope,
)


TURN = 6
BRANCH = "branch.orchestration"


def _fixture(*, flag: str = "gate_open", attempt_index: int = 0, stream: bool = False):
    pre = empty_state()
    pre["meta"]["turn"] = TURN
    operation = {"op": "world_flag", "key": flag, "value": True, "_turn": TURN}
    post = deepcopy(pre)
    reduce_state(post, [deepcopy(operation)])
    rows = [
        {
            "id": 101 + attempt_index,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": "rule",
            "ops": [operation],
        }
    ]
    truth = build_fenced_runtime_truth_contract(
        pre_state=pre,
        post_state=post,
        pre_ledger_hash=content_fingerprint(pre),
        post_ledger_hash=content_fingerprint(post),
        journal_rows=rows,
        journal_window_fingerprint=journal_window_fingerprint(BRANCH, rows),
        branch_id=BRANCH,
        turn_index=TURN,
    )
    plan = build_narration_realization_plan(truth.truth_contract)
    basis = build_persisted_narration_basis(
        truth.transition_projection,
        truth.truth_contract,
        plan,
    )
    key = build_pre_mutation_key(
        session_id="session.orchestration",
        branch_id=BRANCH,
        turn_index=TURN,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I wait."),
        pre_ledger_hash=truth.transition_projection["pre_ledger_hash"],
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/orchestration-test-1",
    )
    reservation = TurnReservation(
        key["lifecycle_key"],
        attempt_index,
        fingerprint({"request": key["lifecycle_key"], "attempt": attempt_index}),
        True,
        "reserved",
    )
    fallback = build_proof_complete_fallback(
        contract=truth.truth_contract,
        pre_mutation_key=key,
        reservation=reservation,
        occurrences=[],
        effects=[],
        rng_fingerprint=fingerprint({"rng": 11}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=truth.transition_projection["pre_ledger_hash"],
        mechanics_post_ledger_hash=truth.transition_projection["post_ledger_hash"],
        model="glm-5.2",
        stream=stream,
    )
    fallback = attach_persisted_narration_basis(fallback, basis)
    expectation = build_fallback_promotion_expectation(fallback)
    return fallback, expectation, basis, plan


def _selection_response(plan: dict, *, malformed: bool = False):
    story = "not selection JSON" if malformed else json.dumps(
        build_default_narration_plan_selection(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encode_chat_story(
        story,
        model="glm-5.2",
        stream=False,
        artifact_ref="orchestration-selection-response",
    )


def _prepare(fallback, expectation):
    return prepare_narration_selection(
        fallback,
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "private"}]},
        expectation,
    )


def test_preparation_builds_the_exact_isolated_transport_request():
    fallback, expectation, _basis, plan = _fixture()

    result = _prepare(fallback, expectation)

    assert result.action == "request"
    assert result.reason_code == "selection_request_ready"
    assert result.fallback_artifact is fallback
    assert result.prepared is not None
    assert result.prepared.transport_request == build_semantic_selection_request(
        plan, {"model": "glm-5.2"}
    )
    assert b"private" not in result.prepared.transport_request.request_bytes


@pytest.mark.parametrize("stream", [False, True])
def test_valid_buffered_selection_builds_one_proof_complete_candidate_envelope(stream):
    fallback, expectation, basis, plan = _fixture(stream=stream)
    frozen_envelope = deepcopy(fallback.envelope)
    preparation = _prepare(fallback, expectation)
    response = _selection_response(plan)

    result = resolve_narration_selection(
        preparation,
        fallback,
        expectation,
        response_bytes=response.raw,
        response_content_type=response.content_type,
    )

    assert result.action == "promote"
    assert result.reason_code == "proof_complete_candidate"
    assert fallback.envelope == frozen_envelope
    assert result.artifact is not fallback
    assert result.artifact.fallback_bytes is fallback.fallback_bytes
    assert result.artifact.fallback_bytes == fallback.fallback_bytes
    base = validate_envelope(fallback)
    candidate = validate_envelope(result.artifact)
    assert candidate["gate"]["decision"] == "accept"
    assert candidate["output"]["content_type"] == base["output"]["content_type"]
    assert decode_chat_story(
        result.artifact.accepted_bytes,
        candidate["output"]["content_type"],
    )
    assert extract_persisted_narration_basis(result.artifact) == basis

    for field in base:
        if field not in {"gate", "delivery_proof", "output", "envelope_fingerprint"}:
            assert candidate[field] == base[field]
    assert candidate["output"]["fallback_hash"] == base["output"]["fallback_hash"]
    assert candidate["output"]["fallback_size"] == base["output"]["fallback_size"]
    assert (
        candidate["delivery_proof"]["delivery"]["logical_message_id"]
        == base["delivery_proof"]["delivery"]["logical_message_id"]
    )


@pytest.mark.parametrize("failure", ["timeout", "malformed"])
def test_timeout_or_malformed_selection_returns_the_original_byte_exact_fallback(failure):
    fallback, expectation, _basis, plan = _fixture()
    preparation = _prepare(fallback, expectation)
    response = _selection_response(plan, malformed=True)
    before = deepcopy(fallback.envelope)

    result = resolve_narration_selection(
        preparation,
        fallback,
        expectation,
        response_bytes=None if failure == "timeout" else response.raw,
        response_content_type=None if failure == "timeout" else response.content_type,
        timed_out=failure == "timeout",
    )

    assert result.action == "fallback"
    assert result.artifact is fallback
    assert result.artifact.fallback_bytes is fallback.fallback_bytes
    assert result.artifact.envelope == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("branch_id", "branch.stale"),
        ("turn_index", TURN + 1),
        ("attempt_index", 1),
        ("active_attempt_index", 1),
    ],
)
def test_stale_identity_never_produces_a_request_or_candidate(field, value):
    fallback, expectation, _basis, _plan = _fixture()
    stale = replace(expectation, **{field: value})

    preparation = _prepare(fallback, stale)

    assert preparation.action == "fallback"
    assert preparation.fallback_artifact is fallback


@pytest.mark.parametrize(
    "status",
    [
        {"lifecycle_status": "fallback_final"},
        {"lifecycle_status": "accepted"},
        {"delivery_claimed": True},
        {"active_attempt_index": 1},
    ],
)
def test_validly_sealed_terminal_delivery_state_still_refuses_selection(status):
    fallback, _expectation, _basis, _plan = _fixture()
    expectation = build_fallback_promotion_expectation(fallback, **status)

    preparation = _prepare(fallback, expectation)

    assert preparation.action == "fallback"
    assert preparation.fallback_artifact is fallback


@pytest.mark.parametrize("alias", [False, True])
def test_boolean_active_attempt_alias_cannot_seal_a_promotion_expectation(alias):
    fallback, _expectation, _basis, _plan = _fixture(attempt_index=int(alias))

    with pytest.raises(
        SemanticNarrationOrchestratorError,
        match="active_attempt_index.*non-negative integer",
    ):
        build_fallback_promotion_expectation(
            fallback,
            active_attempt_index=alias,
        )


def test_tampered_transport_and_cross_plan_preparation_fail_back_without_mutation():
    fallback, expectation, _basis, plan = _fixture()
    preparation = _prepare(fallback, expectation)
    assert preparation.prepared is not None
    response = _selection_response(plan)
    object.__setattr__(
        preparation.prepared.transport_request,
        "request_bytes",
        preparation.prepared.transport_request.request_bytes + b" ",
    )

    tampered = resolve_narration_selection(
        preparation,
        fallback,
        expectation,
        response_bytes=response.raw,
        response_content_type=response.content_type,
    )
    assert tampered.action == "fallback"
    assert tampered.artifact is fallback

    other, other_expectation, _other_basis, other_plan = _fixture(flag="other_flag")
    cross_response = _selection_response(other_plan)
    cross = resolve_narration_selection(
        _prepare(fallback, expectation),
        other,
        other_expectation,
        response_bytes=cross_response.raw,
        response_content_type=cross_response.content_type,
    )
    assert cross.action == "fallback"
    assert cross.artifact is other

    cross_selection = resolve_narration_selection(
        _prepare(fallback, expectation),
        fallback,
        expectation,
        response_bytes=cross_response.raw,
        response_content_type=cross_response.content_type,
    )
    assert cross_selection.action == "fallback"
    assert cross_selection.artifact is fallback


def test_identical_retry_inputs_build_identical_requests_bytes_proofs_and_envelopes():
    fallback, expectation, _basis, plan = _fixture()
    first_preparation = _prepare(fallback, expectation)
    second_preparation = _prepare(fallback, expectation)
    response = _selection_response(plan)

    first = resolve_narration_selection(
        first_preparation,
        fallback,
        expectation,
        response_bytes=response.raw,
        response_content_type=response.content_type,
    )
    second = resolve_narration_selection(
        second_preparation,
        fallback,
        expectation,
        response_bytes=response.raw,
        response_content_type=response.content_type,
    )

    assert first_preparation.prepared == second_preparation.prepared
    assert first.action == second.action == "promote"
    assert first.artifact.accepted_bytes == second.artifact.accepted_bytes
    assert first.artifact.envelope == second.artifact.envelope
    assert first.candidate_fingerprint == second.candidate_fingerprint
