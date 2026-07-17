from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

import aetherstate.narration_artifact_basis as basis_module
from aetherstate.canon import content_hash
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_artifact_basis import (
    NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY,
    NarrationArtifactBasisError,
    attach_persisted_narration_basis,
    build_persisted_narration_basis,
    derive_swipe_fallback_from_persisted_basis,
    extract_persisted_narration_basis,
    rebind_persisted_narration_basis,
    validate_persisted_narration_basis,
)
from aetherstate.narration_fallback_runtime import build_proof_complete_fallback
from aetherstate.narration_plan_runtime import (
    build_default_narration_plan_selection,
    build_narration_realization_plan,
    build_proof_complete_narration_candidate,
)
from aetherstate.narration_truth_gate import build_narration_truth_contract
from aetherstate.narrator_realization import build_narrator_realization
from aetherstate.semantic_transition_truth import project_journal_transitions
from aetherstate.state import empty_state, reduce_state
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    ReplayArtifact,
    TurnReservation,
    TurnLifecycleStore,
    build_envelope,
    build_pre_mutation_key,
    fingerprint,
    logical_message_id,
    raw_fingerprint,
    validate_envelope,
)


TURN = 4
SOURCE_BRANCH = "branch.main"
CHILD_BRANCH = "branch.child"


def _transition() -> dict:
    pre = empty_state()
    pre["meta"]["turn"] = TURN
    operation = {"op": "clock_tick", "minutes": 1, "_turn": TURN}
    post = deepcopy(pre)
    reduce_state(post, [deepcopy(operation)])
    rows = [
        {
            "id": 101,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": "rule",
            "ops": [operation],
        }
    ]
    return project_journal_transitions(
        pre_state=pre,
        post_state=post,
        journal_rows=rows,
        branch_id=SOURCE_BRANCH,
        turn_index=TURN,
        pre_ledger_hash=content_fingerprint(pre),
        post_ledger_hash=content_fingerprint(post),
    )


def _records() -> tuple[dict, dict, dict]:
    transition = _transition()
    realization = build_narrator_realization(TURN)
    contract = build_narration_truth_contract(
        realization,
        lifecycle_binding={
            "branch_ref": SOURCE_BRANCH,
            "ledger_fingerprint": transition["post_ledger_hash"],
            "artifact_fingerprint": realization["fingerprint"],
        },
    )
    return transition, contract, build_narration_realization_plan(contract)


def _basis() -> dict:
    return build_persisted_narration_basis(*_records())


def _key_and_reservation(*, attempt_index: int = 0) -> tuple[dict, TurnReservation]:
    transition = _transition()
    key = build_pre_mutation_key(
        session_id="session.main",
        branch_id=SOURCE_BRANCH,
        turn_index=TURN,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I wait."),
        pre_ledger_hash=transition["pre_ledger_hash"],
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    request_hash = fingerprint(
        {"request": key["lifecycle_key"], "attempt": attempt_index}
    )
    return key, TurnReservation(
        key["lifecycle_key"], attempt_index, request_hash, True, "reserved"
    )


def _fallback(*, attempt_index: int = 0) -> EnvelopeArtifact:
    transition, contract, _plan = _records()
    key, reservation = _key_and_reservation(attempt_index=attempt_index)
    return build_proof_complete_fallback(
        contract=contract,
        pre_mutation_key=key,
        reservation=reservation,
        occurrences=[],
        effects=[],
        rng_fingerprint=fingerprint({"rng": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=transition["pre_ledger_hash"],
        mechanics_post_ledger_hash=transition["post_ledger_hash"],
        model="test-narrator",
        stream=False,
    )


def _accepted() -> EnvelopeArtifact:
    transition, _contract, plan = _records()
    key, reservation = _key_and_reservation()
    fallback = _fallback()
    fallback_envelope = validate_envelope(fallback)
    candidate = build_proof_complete_narration_candidate(
        plan,
        build_default_narration_plan_selection(plan),
        model="test-narrator",
        stream=False,
        logical_message_identity=logical_message_id(key),
    )
    return build_envelope(
        pre_mutation_key=key,
        attempt_index=0,
        attempt_kind="initial",
        request_hash=reservation.request_hash,
        occurrences=[],
        effects=[],
        rng_fingerprint=fingerprint({"rng": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=transition["pre_ledger_hash"],
        mechanics_post_ledger_hash=transition["post_ledger_hash"],
        fallback_bytes=fallback.fallback_bytes,
        delivery_proof=candidate.delivery_proof,
        decision="accept",
        accepted_bytes=candidate.wire_bytes,
        gate_reason_code="proved_plan_selection",
        diagnostics=fallback_envelope["diagnostics"],
    )


def _reseal(value: dict) -> dict:
    payload = {key: deepcopy(item) for key, item in value.items() if key != "fingerprint"}
    return {**payload, "fingerprint": content_fingerprint(payload)}


def _reseal_basis(value: dict) -> dict:
    return _reseal(value)


def _replay(artifact: EnvelopeArtifact) -> ReplayArtifact:
    envelope = validate_envelope(artifact)
    delivery = envelope["delivery_proof"]["delivery"]
    return ReplayArtifact(
        lifecycle_key=envelope["lifecycle_key"],
        attempt_index=envelope["attempt"]["index"],
        status="fallback_ready",
        content_type=envelope["output"]["content_type"],
        payload=artifact.fallback_bytes,
        payload_hash=raw_fingerprint(artifact.fallback_bytes),
        envelope=envelope,
        logical_message_id=delivery["logical_message_id"],
        selected_artifact_digest=delivery["selected_artifact_digest"],
    )


def _accepted_replay(artifact: EnvelopeArtifact) -> ReplayArtifact:
    envelope = validate_envelope(artifact)
    delivery = envelope["delivery_proof"]["delivery"]
    assert artifact.accepted_bytes is not None
    return ReplayArtifact(
        lifecycle_key=envelope["lifecycle_key"],
        attempt_index=envelope["attempt"]["index"],
        status="accepted",
        content_type=envelope["output"]["content_type"],
        payload=artifact.accepted_bytes,
        payload_hash=raw_fingerprint(artifact.accepted_bytes),
        envelope=envelope,
        logical_message_id=delivery["logical_message_id"],
        selected_artifact_digest=delivery["selected_artifact_digest"],
    )


def test_build_seals_complete_records_and_exact_source_current_identity() -> None:
    transition, contract, plan = _records()
    basis = build_persisted_narration_basis(transition, contract, plan)

    assert basis["schema"] == "persisted-narration-artifact-basis/1"
    assert basis["source_transition_projection"] == transition
    assert basis["source_truth_contract"] == contract
    assert basis["narration_realization_plan"] == plan
    assert basis["source_lifecycle_binding"] == {
        "branch_ref": SOURCE_BRANCH,
        "turn_index": TURN,
        "pre_ledger_fingerprint": transition["pre_ledger_hash"],
        "post_ledger_fingerprint": transition["post_ledger_hash"],
        "journal_window_fingerprint": transition["journal_window_fingerprint"],
        "transition_projection_fingerprint": transition["fingerprint"],
        "truth_contract_fingerprint": contract["fingerprint"],
        "narration_plan_fingerprint": plan["fingerprint"],
        "artifact_fingerprint": contract["realization_fingerprint"],
    }
    assert basis["current_lifecycle_binding"] == {
        "branch_ref": SOURCE_BRANCH,
        "turn_index": TURN,
        "post_ledger_fingerprint": transition["post_ledger_hash"],
        "artifact_fingerprint": contract["realization_fingerprint"],
        "source_branch_ref": SOURCE_BRANCH,
        "source_journal_window_fingerprint": transition[
            "journal_window_fingerprint"
        ],
        "source_transition_projection_fingerprint": transition["fingerprint"],
        "source_truth_contract_fingerprint": contract["fingerprint"],
        "narration_plan_fingerprint": plan["fingerprint"],
    }
    assert validate_persisted_narration_basis(basis) == basis


def test_build_and_validation_are_deep_json_copies() -> None:
    transition, contract, plan = _records()
    basis = build_persisted_narration_basis(transition, contract, plan)
    frozen = deepcopy(basis)

    transition["entries"][0]["op_kind"] = "forged"
    contract["known_entities"][0]["label"] = "forged"
    plan["surface_profile"]["renderer_version"] = "forged"
    assert basis == frozen

    detached = validate_persisted_narration_basis(basis)
    detached["source_transition_projection"]["entries"][0]["source"] = "forged"
    assert basis == frozen


def test_forged_transition_row_rejects_even_after_nested_and_outer_resealing() -> None:
    forged = deepcopy(_basis())
    transition = forged["source_transition_projection"]
    transition["entries"][0]["branch_id"] = "branch.forged-row"
    transition["transitions"][0]["branch_id"] = "branch.forged-row"
    forged["source_transition_projection"] = _reseal(transition)
    forged["source_transition_projection_fingerprint"] = forged[
        "source_transition_projection"
    ]["fingerprint"]
    forged["source_lifecycle_binding"]["transition_projection_fingerprint"] = forged[
        "source_transition_projection_fingerprint"
    ]
    forged["current_lifecycle_binding"][
        "source_transition_projection_fingerprint"
    ] = forged["source_transition_projection_fingerprint"]
    forged = _reseal_basis(forged)

    with pytest.raises(NarrationArtifactBasisError, match="transition"):
        validate_persisted_narration_basis(forged)


def test_wrong_nested_hash_and_journal_binding_fail_closed() -> None:
    wrong_hash = deepcopy(_basis())
    wrong_hash["source_transition_projection_fingerprint"] = fingerprint(
        {"forged": "projection"}
    )
    wrong_hash = _reseal_basis(wrong_hash)
    with pytest.raises(NarrationArtifactBasisError, match="exact code-authored form"):
        validate_persisted_narration_basis(wrong_hash)

    wrong_window = deepcopy(_basis())
    wrong_window["source_lifecycle_binding"]["journal_window_fingerprint"] = fingerprint(
        {"forged": "window"}
    )
    wrong_window = _reseal_basis(wrong_window)
    with pytest.raises(NarrationArtifactBasisError, match="exact code-authored form"):
        validate_persisted_narration_basis(wrong_window)


def test_branch_post_ledger_and_plan_cross_lending_fail_closed() -> None:
    branch = deepcopy(_basis())
    branch["current_lifecycle_binding"]["branch_ref"] = "branch.forged"
    branch = _reseal_basis(branch)
    with pytest.raises(NarrationArtifactBasisError, match="exact code-authored form"):
        validate_persisted_narration_basis(branch)

    ledger = deepcopy(_basis())
    ledger["current_lifecycle_binding"]["post_ledger_fingerprint"] = fingerprint(
        {"forged": "ledger"}
    )
    ledger = _reseal_basis(ledger)
    with pytest.raises(NarrationArtifactBasisError, match="exact code-authored form"):
        validate_persisted_narration_basis(ledger)

    plan = deepcopy(_basis())
    rebound = rebind_persisted_narration_basis(plan, branch_ref=CHILD_BRANCH)
    plan["narration_realization_plan"] = rebound["narration_realization_plan"]
    plan["narration_plan_fingerprint"] = rebound["narration_plan_fingerprint"]
    plan = _reseal_basis(plan)
    with pytest.raises(NarrationArtifactBasisError, match="exact code-authored form"):
        validate_persisted_narration_basis(plan)


def test_swipe_reopen_and_json_roundtrip_reuse_the_exact_basis() -> None:
    basis = _basis()
    roundtrip = json.loads(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert validate_persisted_narration_basis(roundtrip) == basis
    assert _basis() == basis

    initial = attach_persisted_narration_basis(_fallback(), basis)
    swipe = attach_persisted_narration_basis(_fallback(attempt_index=1), basis)
    assert extract_persisted_narration_basis(initial) == basis
    assert extract_persisted_narration_basis(swipe) == basis
    assert extract_persisted_narration_basis(_replay(initial)) == basis
    assert attach_persisted_narration_basis(initial, basis) == initial


def test_swipe_derivation_reuses_exact_persisted_truth_proof_and_bytes(
    monkeypatch,
) -> None:
    basis = _basis()
    source = attach_persisted_narration_basis(_fallback(), basis)
    frozen_source = deepcopy(source)
    _key, reservation = _key_and_reservation(attempt_index=1)

    def current_builder_must_not_run(_contract):
        raise AssertionError("swipe must not rebuild the persisted narration plan")

    monkeypatch.setattr(
        basis_module,
        "build_narration_realization_plan",
        current_builder_must_not_run,
    )
    derived = derive_swipe_fallback_from_persisted_basis(
        source,
        _replay(source),
        reservation,
    )
    source_valid = validate_envelope(source)
    derived_valid = validate_envelope(derived)

    assert source == frozen_source
    assert derived.fallback_bytes == source.fallback_bytes
    assert derived.accepted_bytes is None
    assert derived_valid["attempt"] == {
        "index": 1,
        "kind": "swipe",
        "request_hash": reservation.request_hash,
        "ledger_anchor_hash": source_valid["ledger"]["mechanics_post_hash"],
    }
    assert derived_valid["delivery_proof"] == source_valid["delivery_proof"]
    assert derived_valid["occurrences"] == source_valid["occurrences"]
    assert derived_valid["effects"] == source_valid["effects"]
    assert derived_valid["runtime"] == source_valid["runtime"]
    assert derived_valid["ledger"] == source_valid["ledger"]
    assert extract_persisted_narration_basis(derived) == basis


def test_swipe_derivation_uses_the_selected_terminal_replay_as_lineage_authority() -> None:
    basis = _basis()
    source_fallback = attach_persisted_narration_basis(_fallback(), basis)
    accepted = attach_persisted_narration_basis(_accepted(), basis)
    source_replay = _accepted_replay(accepted)
    _key, reservation = _key_and_reservation(attempt_index=1)

    derived = derive_swipe_fallback_from_persisted_basis(
        source_fallback,
        source_replay,
        reservation,
    )

    assert derived.envelope["lineage"] == {
        "source_lifecycle_key": source_replay.lifecycle_key,
        "source_envelope_fingerprint": source_replay.envelope[
            "envelope_fingerprint"
        ],
    }


def test_swipe_derivation_rejects_an_inactive_source_attempt() -> None:
    basis = _basis()
    inactive = attach_persisted_narration_basis(_fallback(), basis)
    active = attach_persisted_narration_basis(_fallback(attempt_index=1), basis)
    _key, reservation = _key_and_reservation(attempt_index=2)

    with pytest.raises(NarrationArtifactBasisError, match="source replay attempt"):
        derive_swipe_fallback_from_persisted_basis(
            inactive,
            _replay(active),
            reservation,
        )


@pytest.mark.parametrize("alias", [False, True])
def test_swipe_derivation_rejects_boolean_reservation_attempt_alias(alias) -> None:
    source = attach_persisted_narration_basis(_fallback(), _basis())
    _key, reservation = _key_and_reservation(attempt_index=1)
    forged = replace(reservation, attempt_index=alias)

    with pytest.raises(NarrationArtifactBasisError, match="reserved narration attempt"):
        derive_swipe_fallback_from_persisted_basis(
            source,
            _replay(source),
            forged,
        )


def test_swipe_derivation_rejects_tampered_persisted_receipt() -> None:
    source = attach_persisted_narration_basis(_fallback(), _basis())
    forged_envelope = deepcopy(source.envelope)
    forged_envelope["delivery_proof"]["gate_receipt"]["reason_code"] = "forged"
    forged_envelope.pop("envelope_fingerprint")
    forged_envelope["envelope_fingerprint"] = fingerprint(forged_envelope)
    forged = EnvelopeArtifact(forged_envelope, source.fallback_bytes)
    _key, reservation = _key_and_reservation(attempt_index=1)

    with pytest.raises(NarrationArtifactBasisError, match="derivation|proof|fallback"):
        derive_swipe_fallback_from_persisted_basis(
            forged,
            _replay(source),
            reservation,
        )


def test_fork_rebind_rotates_only_current_plan_and_preserves_source_isolation() -> None:
    source = _basis()
    frozen = deepcopy(source)
    child = rebind_persisted_narration_basis(source, branch_ref=CHILD_BRANCH)

    assert source == frozen
    assert child["fingerprint"] != source["fingerprint"]
    assert child["source_transition_projection"] == source[
        "source_transition_projection"
    ]
    assert child["source_truth_contract"] == source["source_truth_contract"]
    assert child["source_lifecycle_binding"] == source["source_lifecycle_binding"]
    assert child["narration_realization_plan"]["source_lifecycle_binding"] \
        == source["narration_realization_plan"]["source_lifecycle_binding"]
    assert child["narration_realization_plan"]["semantic_truth_basis"] \
        == source["narration_realization_plan"]["semantic_truth_basis"]
    assert child["narration_realization_plan"]["lifecycle_binding"]["branch_ref"] \
        == CHILD_BRANCH
    assert child["current_lifecycle_binding"]["branch_ref"] == CHILD_BRANCH
    assert child["current_lifecycle_binding"]["source_branch_ref"] == SOURCE_BRANCH
    assert validate_persisted_narration_basis(child) == child


def test_fork_rebases_exact_accepted_artifact_and_embedded_basis_without_source_mutation(
    monkeypatch,
) -> None:
    source = attach_persisted_narration_basis(_accepted(), _basis())
    frozen_source = deepcopy(source)
    source_envelope = validate_envelope(source)
    source_key = source_envelope["pre_mutation_key"]
    child_key = build_pre_mutation_key(
        session_id="session.child",
        branch_id=CHILD_BRANCH,
        turn_index=source_key["turn_index"],
        accepted_prefix_pos=source_key["accepted_prefix_pos"],
        accepted_head_hash=source_key["accepted_head_hash"],
        player_input_hash=source_key["player_input_hash"],
        pre_ledger_hash=source_key["pre_ledger_hash"],
        pending_intent_fingerprint=source_key["pending_intent_fingerprint"],
        semantic_contract_version=source_key["semantic_contract_version"],
    )

    def current_builder_must_not_run(_contract):
        raise AssertionError("fork must rebind the persisted plan instead of rebuilding it")

    monkeypatch.setattr(
        basis_module,
        "build_narration_realization_plan",
        current_builder_must_not_run,
    )
    request_hash = fingerprint({"child": child_key["lifecycle_key"]})
    rebased = TurnLifecycleStore._rebase_envelope(
        source_envelope,
        child_key,
        request_hash,
        source_envelope["lifecycle_key"],
        source_envelope["envelope_fingerprint"],
    )
    child = EnvelopeArtifact(rebased, source.fallback_bytes, source.accepted_bytes)
    validate_envelope(child)
    child_basis = extract_persisted_narration_basis(child)
    source_basis = extract_persisted_narration_basis(source)

    assert source == frozen_source
    assert child.fallback_bytes == source.fallback_bytes
    assert child.accepted_bytes == source.accepted_bytes
    assert child_basis["source_transition_projection"] \
        == source_basis["source_transition_projection"]
    assert child_basis["source_truth_contract"] == source_basis["source_truth_contract"]
    assert child_basis["source_lifecycle_binding"] \
        == source_basis["source_lifecycle_binding"]
    assert child_basis["current_lifecycle_binding"]["branch_ref"] == CHILD_BRANCH
    assert rebased["pre_mutation_key"]["session_id"] == "session.child"
    assert rebased["lifecycle_key"] == child_key["lifecycle_key"]
    assert rebased["lineage"] == {
        "source_lifecycle_key": source_envelope["lifecycle_key"],
        "source_envelope_fingerprint": source_envelope["envelope_fingerprint"],
    }


def test_attach_changes_only_diagnostics_and_envelope_fingerprint() -> None:
    artifact = _fallback()
    before = validate_envelope(artifact)
    basis = _basis()
    attached = attach_persisted_narration_basis(artifact, basis)
    after = validate_envelope(attached)

    assert attached.fallback_bytes is artifact.fallback_bytes
    assert attached.accepted_bytes is artifact.accepted_bytes
    assert after["delivery_proof"] == before["delivery_proof"]
    assert after["diagnostics"][NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY] == basis
    for key in before:
        if key not in {"diagnostics", "envelope_fingerprint"}:
            assert after[key] == before[key]
    assert after["envelope_fingerprint"] != before["envelope_fingerprint"]


def test_attach_preserves_accepted_and_fallback_bytes_and_accepted_proof() -> None:
    artifact = _accepted()
    before = validate_envelope(artifact)
    attached = attach_persisted_narration_basis(artifact, _basis())
    after = validate_envelope(attached)

    assert attached.fallback_bytes is artifact.fallback_bytes
    assert attached.accepted_bytes is artifact.accepted_bytes
    assert attached.fallback_bytes == artifact.fallback_bytes
    assert attached.accepted_bytes == artifact.accepted_bytes
    assert after["delivery_proof"] == before["delivery_proof"]
    assert after["output"] == before["output"]
    assert extract_persisted_narration_basis(attached) == _basis()


def test_attach_and_extract_reject_wrong_current_envelope_or_embedded_basis() -> None:
    source = _basis()
    child = rebind_persisted_narration_basis(source, branch_ref=CHILD_BRANCH)
    with pytest.raises(NarrationArtifactBasisError, match="branch"):
        attach_persisted_narration_basis(_fallback(), child)

    attached = attach_persisted_narration_basis(_fallback(), source)
    forged_envelope = deepcopy(attached.envelope)
    forged_basis = deepcopy(source)
    forged_basis["current_lifecycle_binding"]["turn_index"] = TURN + 1
    forged_basis = _reseal_basis(forged_basis)
    forged_envelope["diagnostics"][NARRATION_ARTIFACT_BASIS_DIAGNOSTIC_KEY] = forged_basis
    forged_envelope.pop("envelope_fingerprint")
    forged_envelope["envelope_fingerprint"] = fingerprint(forged_envelope)
    forged = EnvelopeArtifact(
        forged_envelope, attached.fallback_bytes, attached.accepted_bytes
    )
    with pytest.raises(NarrationArtifactBasisError):
        extract_persisted_narration_basis(forged)


def test_extract_requires_a_proof_carrying_envelope_with_embedded_basis() -> None:
    with pytest.raises(NarrationArtifactBasisError, match="embedded"):
        extract_persisted_narration_basis(_fallback())

    with pytest.raises(NarrationArtifactBasisError, match="proof-carrying"):
        extract_persisted_narration_basis({"envelope": "untrusted"})
