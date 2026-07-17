from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unicodedata

import pytest

from aetherstate.canon import content_hash
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_fallback_runtime import (
    EMPTY_FALLBACK_TEXT,
    FALLBACK_PHASES,
    NarrationFallbackRuntimeError,
    build_fallback_realization_plan,
    build_proof_complete_fallback,
    observe_fallback_claim_graph,
    render_fallback_text,
    validate_canonical_visible_text,
)
from aetherstate.narration_truth_gate import (
    FALLOUT_FACT_SCHEMA,
    OPPOSITION_FACT_SCHEMA,
    TARGET_OUTCOME_SCHEMA,
    build_narration_truth_contract,
)
from aetherstate.narrator_realization import build_narrator_realization
from aetherstate.response_wire import decode_chat_story
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    TurnReservation,
    build_pre_mutation_key,
    fingerprint,
    validate_envelope,
)


def _fp(value: object) -> str:
    return content_fingerprint(value)


def _meaning(*, target_id: str, seed: str) -> dict:
    return {
        "meaning_ref": _fp({"meaning": seed}),
        "actor_id": "player.arinvale",
        "capability_id": "weapon_attack",
        "invoked_capability_ids": [],
        "action_class": "weapon_attack",
        "target_entity_id": target_id,
        "object_relation": {
            "object_kind_id": None,
            "linguistic_possessor_id": None,
            "resolved_instance_ids": [],
            "proven_owner_id": None,
            "part_id": None,
            "alignment_status": "none",
            "alignment_ref": None,
            "candidate_instance_ids": [],
        },
        "target_locus": None,
        "target_locus_owner_id": None,
        "assertion_status": "asserted",
        "embedding_kind": "none",
        "holder_role": "none",
        "holder_entity_id": None,
        "holder_candidates": [],
        "polarity": "positive",
        "modality": "actual",
        "time_scope": "current",
        "ambiguity_candidate_ids": [],
        "performance_mode": "may_perform",
    }


def _weapon_row() -> dict:
    return {
        "event_ref": "settlement.player.hit",
        "adapter_id": "narrator.weapon-attack/1",
        "frame_ref": _fp({"frame": "player-hit"}),
        "event_meaning": _meaning(target_id="guard", seed="player-hit"),
        "outcome_quality": "success",
        "impact_kind": "harm",
        "impact_magnitude": "solid",
        "target_state": "active",
        "settled_change_kinds": ["hp"],
    }


def _key_and_reservation(*, turn: int = 4) -> tuple[dict, TurnReservation]:
    key = build_pre_mutation_key(
        session_id="session.main",
        branch_id="branch.main",
        turn_index=turn,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I strike the guard."),
        pre_ledger_hash=fingerprint({"ledger": "pre", "hp": [3, 5]}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    request_hash = fingerprint({"request": key["lifecycle_key"], "attempt": 0})
    return key, TurnReservation(key["lifecycle_key"], 0, request_hash, True, "reserved")


def _contract(*, populated: bool) -> tuple[dict, str, str]:
    pre_hash = fingerprint({"ledger": "pre", "hp": [3, 5]})
    post_hash = fingerprint({"ledger": "post", "hp": [2, 3]})
    packet = build_narrator_realization(
        4,
        asserted_settled=[_weapon_row()] if populated else [],
    )
    opposition = []
    outcomes = []
    known = [
        {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
        {"entity_id": "guard", "label": "Ash Guard", "scope": "current"},
    ]
    if populated:
        opposition = [
            {
                "schema": OPPOSITION_FACT_SCHEMA,
                "occurrence_ref": "opposition.guard.hit",
                "intent_ref": "intent.guard.one",
                "construction_ref": _fp({"construction": "guard-hit"}),
                "actor_id": "guard",
                "actor_label": "Ash Guard",
                "target_id": "player.arinvale",
                "target_label": "Arinvale",
                "move_id": "heavy_commitment",
                "move_label": "Heavy Commitment",
                "outcome": "hit",
                "effects": [{"kind": "harm", "detail": "harm", "amount": -1}],
            }
        ]
        outcomes = [
            {
                "schema": TARGET_OUTCOME_SCHEMA,
                "outcome_ref": "outcome.guard.harm",
                "source_event_ref": "settlement.player.hit",
                "construction_ref": _fp({"construction": "player-hit"}),
                "target_id": "guard",
                "target_label": "Ash Guard",
                "effects": [{"kind": "harm", "detail": "harm", "amount": -2}],
            }
        ]
    contract = build_narration_truth_contract(
        packet,
        known_entities=known,
        opposition_facts=opposition,
        settled_target_outcomes=outcomes,
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": post_hash,
            "artifact_fingerprint": packet["fingerprint"],
        },
    )
    return contract, pre_hash, post_hash


def _build_kwargs(*, populated: bool, stream: bool = False) -> dict:
    contract, pre_hash, post_hash = _contract(populated=populated)
    key, reservation = _key_and_reservation()
    occurrences = (
        [
            {"occurrence_id": "occ.player.hit", "settlement_ref": "settlement.player.hit"},
            {"occurrence_id": "occ.guard.hit", "settlement_ref": "opposition.guard.hit"},
        ]
        if populated
        else []
    )
    effects = (
        [
            {"effect_id": "effect.player.hp", "occurrence_id": "occ.player.hit"},
            {"effect_id": "effect.guard.hp", "occurrence_id": "occ.guard.hit"},
        ]
        if populated
        else []
    )
    return {
        "contract": contract,
        "pre_mutation_key": key,
        "reservation": reservation,
        "occurrences": occurrences,
        "effects": effects,
        "rng_fingerprint": fingerprint({"rng": 7}),
        "config_fingerprint": fingerprint({"difficulty": "normal"}),
        "engine_version": "test-engine/1",
        "pre_ledger_hash": pre_hash,
        "mechanics_post_ledger_hash": post_hash,
        "model": "test-narrator",
        "stream": stream,
        "consumed_intent_id": "intent.guard.one" if populated else None,
        "next_intent_id": "intent.guard.two" if populated else None,
    }


def test_empty_ledger_fallback_is_proof_complete_before_envelope_return():
    artifact = build_proof_complete_fallback(**_build_kwargs(populated=False))
    envelope = validate_envelope(artifact)

    assert isinstance(artifact, EnvelopeArtifact)
    assert decode_chat_story(artifact.fallback_bytes, envelope["output"]["content_type"]) \
        == EMPTY_FALLBACK_TEXT
    proof = envelope["delivery_proof"]
    assert proof["comparisons"] == {
        "mode": "exact_json",
        "observed_equals_expected": True,
        "observed_matches_ledger": True,
    }
    assert proof["gate_receipt"]["comparison_mode"] == "exact_json"
    assert proof["expected_graph"] == proof["observed_graph"] == proof["ledger_graph"]
    assert proof["expected_graph"]["claims"] == []
    assert envelope["gate"]["decision"] == "fallback"
    assert envelope["diagnostics"]["renderer_version"] == "aetherstate.safe-plain-text/1"


def test_exact_player_harm_and_autonomous_opposition_keep_separate_causes():
    artifact = build_proof_complete_fallback(**_build_kwargs(populated=True))
    envelope = validate_envelope(artifact)
    story = decode_chat_story(artifact.fallback_bytes, envelope["output"]["content_type"])
    claims = envelope["delivery_proof"]["observed_graph"]["claims"]

    assert "Heavy Commitment hits Arinvale" in story
    assert "deals 1 HP of harm to Arinvale" in story
    assert "deals 2 HP of harm to Ash Guard" in story
    opposition = [row for row in claims if row["kind"] == "opposition_action"]
    harms = [row for row in claims if row["kind"] == "harm"]
    assert len(opposition) == 1
    assert {row["actor_id"] for row in harms} == {"guard", "player.arinvale"}
    assert {row["cause_ref"] for row in harms} == {
        "opposition.guard.hit",
        "settlement.player.hit",
    }
    assert opposition[0]["cause_ref"] == "intent.guard.one"
    assert envelope["delivery_proof"]["comparisons"]["mode"] \
        == "semantic_claim_multiset"


@pytest.mark.parametrize("stream", [False, True])
def test_wire_encoding_is_byte_deterministic_and_decodes_to_exact_visible_text(stream: bool):
    left = build_proof_complete_fallback(**_build_kwargs(populated=True, stream=stream))
    right = build_proof_complete_fallback(**_build_kwargs(populated=True, stream=stream))
    left_envelope = validate_envelope(left)

    assert left.fallback_bytes == right.fallback_bytes
    assert left.envelope == right.envelope
    assert decode_chat_story(
        left.fallback_bytes, left_envelope["output"]["content_type"]
    ) == render_fallback_text(build_fallback_realization_plan(_contract(populated=True)[0]))


def test_observer_does_not_read_expected_graph_and_tampered_plan_cannot_render():
    contract, _pre, _post = _contract(populated=True)
    plan = build_fallback_realization_plan(contract)
    observed = observe_fallback_claim_graph(
        plan.text, observation_context=plan.observation_context
    )
    tampered_expected = deepcopy(plan.expected_graph)
    tampered_expected["claims"][0]["detail"] = "miss"
    tampered = replace(plan, expected_graph=tampered_expected)

    assert observed == plan.expected_graph
    assert observe_fallback_claim_graph(
        plan.text, observation_context=plan.observation_context
    ) == observed
    with pytest.raises(NarrationFallbackRuntimeError, match="graph fingerprint mismatch"):
        render_fallback_text(tampered)


def test_tampered_visible_fallback_fails_before_wire_or_envelope():
    kwargs = _build_kwargs(populated=True)
    kwargs["surface_adapter"] = lambda text: text.replace(
        "Heavy Commitment hits", "Heavy Commitment misses"
    )

    with pytest.raises(NarrationFallbackRuntimeError, match="differs from its plan"):
        build_proof_complete_fallback(**kwargs)


def test_code_templates_reobserve_every_supported_non_aoe_claim_kind():
    packet = build_narrator_realization(4)
    post_hash = fingerprint({"ledger": "post", "all-kinds": True})
    contract = build_narration_truth_contract(
        packet,
        known_entities=[
            {"entity_id": "guard", "label": "Ash Guard", "scope": "current"},
        ],
        fallout_facts=[
            {
                "schema": FALLOUT_FACT_SCHEMA,
                "fact_ref": "fallout.guard",
                "cause_ref": "cause.guard",
                "construction_ref": _fp({"construction": "all-kinds"}),
                "subject_id": "guard",
                "subject_label": "Ash Guard",
                "effects": [
                    {"kind": "harm", "detail": "fire harm", "amount": -3},
                    {"kind": "defeat", "detail": "slain", "amount": None},
                    {"kind": "status", "detail": "poisoned", "amount": None},
                    {"kind": "resource", "detail": "focus", "amount": -2},
                    {"kind": "time", "detail": "minutes", "amount": 5},
                    {"kind": "movement", "detail": "north gate", "amount": None},
                    {"kind": "world", "detail": "gate opened", "amount": None},
                ],
            }
        ],
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": post_hash,
            "artifact_fingerprint": packet["fingerprint"],
        },
    )
    plan = build_fallback_realization_plan(contract)
    observed = observe_fallback_claim_graph(
        plan.text, observation_context=plan.observation_context
    )

    assert observed == plan.expected_graph == plan.ledger_graph
    assert {claim["kind"] for claim in observed["claims"]} == {
        "harm", "defeat", "status", "resource", "time", "movement", "world"
    }
    assert "3 HP of fire harm" in plan.text
    assert "For Ash Guard, settled time minutes changes by 5" in plan.text


@pytest.mark.parametrize(
    "unsafe",
    [
        "The guard is <span hidden>not</span> dead.",
        "The guard is &not; dead.",
        "The guard is ~~not~~ dead.",
        "The guard is [not](https://invalid.example) dead.",
        "The guard is not\u202edead.",
        "The guard is not\u200bdead.",
        "The guard is not dead.::before { content: ' alive'; }",
        "The guard is not\r\ndead.",
        unicodedata.normalize("NFD", "Árinvale remains standing."),
    ],
)
def test_renderer_entity_bidi_invisible_and_normalization_surfaces_fail_closed(unsafe: str):
    with pytest.raises(NarrationFallbackRuntimeError):
        validate_canonical_visible_text(unsafe)


def test_safe_nfc_unicode_plain_text_is_an_exact_positive_control():
    text = "Arinvale remains at Vael’Cora."
    assert validate_canonical_visible_text(text) == text


@pytest.mark.parametrize("fault_phase", FALLBACK_PHASES)
def test_every_phase_fault_returns_no_artifact(fault_phase: str):
    seen: list[str] = []

    def fault(phase: str) -> None:
        seen.append(phase)
        if phase == fault_phase:
            raise RuntimeError(f"fault:{phase}")

    kwargs = _build_kwargs(populated=True)
    kwargs["phase_hook"] = fault
    with pytest.raises(RuntimeError, match=f"fault:{fault_phase}"):
        build_proof_complete_fallback(**kwargs)
    assert fault_phase in seen


def test_wrong_reservation_or_contract_ledger_cannot_build_an_artifact():
    kwargs = _build_kwargs(populated=True)
    reservation = kwargs["reservation"]
    kwargs["reservation"] = replace(reservation, lifecycle_key=fingerprint({"other": True}))
    with pytest.raises(NarrationFallbackRuntimeError, match="different lifecycle key"):
        build_proof_complete_fallback(**kwargs)

    kwargs = _build_kwargs(populated=True)
    contract = deepcopy(kwargs["contract"])
    contract["lifecycle_binding"]["ledger_fingerprint"] = fingerprint({"wrong": "ledger"})
    # The contract fingerprint is intentionally left stale: sealed truth cannot be edited in place.
    kwargs["contract"] = contract
    with pytest.raises(NarrationFallbackRuntimeError):
        build_proof_complete_fallback(**kwargs)


@pytest.mark.parametrize("alias", [False, True])
def test_boolean_reservation_attempt_alias_cannot_build_a_fallback(alias):
    kwargs = _build_kwargs(populated=True)
    kwargs["reservation"] = replace(
        kwargs["reservation"], attempt_index=alias
    )

    with pytest.raises(NarrationFallbackRuntimeError, match="attempt index"):
        build_proof_complete_fallback(**kwargs)
