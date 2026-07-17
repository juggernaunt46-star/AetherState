from __future__ import annotations

from copy import deepcopy
import json

import pytest

from aetherstate.capability_glossary import content_fingerprint, raw_fingerprint
from aetherstate.narration_fallback_runtime import EMPTY_FALLBACK_TEXT
from aetherstate.narration_plan_runtime import (
    NARRATION_PLAN_SELECTION_SCHEMA,
    NarrationPlanRuntimeError,
    build_default_narration_plan_selection,
    build_narration_plan_request,
    build_narration_realization_plan,
    build_proof_complete_narration_candidate,
    observe_narration_plan_text,
    rebind_narration_realization_plan,
    render_narration_plan_selection,
    validate_narration_realization_plan,
    validate_narration_plan_selection,
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
    canonical_claim_projection,
    fingerprint,
)


def _fp(value: object) -> str:
    return content_fingerprint(value)


def _meaning(
    *,
    target_id: str | None,
    seed: str,
    action_class: str = "weapon_attack",
    capability_id: str = "weapon_attack",
) -> dict:
    return {
        "meaning_ref": _fp({"meaning": seed}),
        "actor_id": "player.arinvale",
        "capability_id": capability_id,
        "invoked_capability_ids": [],
        "action_class": action_class,
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


def _contract() -> dict:
    packet = build_narrator_realization(
        4,
        asserted_settled=[
            {
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
        ],
    )
    post_hash = fingerprint({"ledger": "post", "hp": [2, 3]})
    return build_narration_truth_contract(
        packet,
        known_entities=[
            {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
            {"entity_id": "guard", "label": "Ash Guard", "scope": "current"},
        ],
        opposition_facts=[
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
        ],
        settled_target_outcomes=[
            {
                "schema": TARGET_OUTCOME_SCHEMA,
                "outcome_ref": "outcome.guard.harm",
                "source_event_ref": "settlement.player.hit",
                "construction_ref": _fp({"construction": "player-hit"}),
                "target_id": "guard",
                "target_label": "Ash Guard",
                "effects": [{"kind": "harm", "detail": "harm", "amount": -2}],
            }
        ],
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": post_hash,
            "artifact_fingerprint": packet["fingerprint"],
        },
    )


def _all_kind_contract() -> dict:
    packet = build_narrator_realization(4)
    return build_narration_truth_contract(
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
            "ledger_fingerprint": fingerprint({"ledger": "post", "all-kinds": True}),
            "artifact_fingerprint": packet["fingerprint"],
        },
    )


def _qualitative_contract(
    *,
    outcomes: tuple[str, ...] = ("success",),
    targets: tuple[str | None, ...] = (None,),
    settled_change_kinds: tuple[str, ...] = (),
) -> dict:
    assert len(outcomes) == len(targets)
    rows = []
    known = [
        {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
    ]
    for index, (outcome, target_id) in enumerate(zip(outcomes, targets)):
        rows.append(
            {
                "event_ref": f"settlement.skill.{index}",
                "adapter_id": "narrator.skill-check/1",
                "frame_ref": _fp({"frame": f"skill-{index}"}),
                "event_meaning": _meaning(
                    target_id=target_id,
                    seed=f"skill-{index}",
                    action_class="skill_check",
                    capability_id="brace" if index == 0 else "observe",
                ),
                "outcome_quality": outcome,
                "impact_kind": "none",
                "impact_magnitude": "none",
                "target_state": "not_applicable",
                "settled_change_kinds": list(settled_change_kinds),
            }
        )
        if target_id is not None:
            known.append(
                {
                    "entity_id": target_id,
                    "label": "Ash Guard" if index == 0 else f"Target {index}",
                    "scope": "current",
                }
            )
    packet = build_narrator_realization(4, asserted_settled=rows)
    return build_narration_truth_contract(
        packet,
        known_entities=known,
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": fingerprint(
                {"ledger": "post", "qualitative": outcomes, "targets": targets}
            ),
            "artifact_fingerprint": packet["fingerprint"],
        },
    )


def _zero_impact_weapon_contract() -> dict:
    packet = build_narrator_realization(
        4,
        asserted_settled=[
            {
                "event_ref": "settlement.weapon.no-impact",
                "adapter_id": "narrator.weapon-attack/1",
                "frame_ref": _fp({"frame": "weapon-no-impact"}),
                "event_meaning": _meaning(target_id="guard", seed="weapon-no-impact"),
                "outcome_quality": "fail",
                "impact_kind": "none",
                "impact_magnitude": "none",
                "target_state": "active",
                "settled_change_kinds": ["hp"],
            }
        ],
    )
    return build_narration_truth_contract(
        packet,
        known_entities=[
            {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
            {"entity_id": "guard", "label": "Ash Guard", "scope": "current"},
        ],
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": fingerprint({"ledger": "zero-impact"}),
            "artifact_fingerprint": packet["fingerprint"],
        },
    )


def _unresolved_attempt_contract() -> dict:
    packet = build_narrator_realization(
        4,
        asserted_unresolved=[
            {
                "event_ref": "unresolved.skill.0",
                "frame_ref": _fp({"frame": "unresolved-skill"}),
                "event_meaning": _meaning(
                    target_id="guard",
                    seed="unresolved-skill",
                    action_class="skill_check",
                    capability_id="brace",
                ),
                "reason": "no_complete_settlement",
                "allowed_stage": "attempt_only",
            }
        ],
    )
    return build_narration_truth_contract(
        packet,
        known_entities=[
            {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
            {"entity_id": "guard", "label": "Ash Guard", "scope": "current"},
        ],
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": fingerprint({"ledger": "unresolved"}),
            "artifact_fingerprint": packet["fingerprint"],
        },
    )


def _plan_and_selection() -> tuple[dict, dict]:
    plan = build_narration_realization_plan(_contract())
    return plan, build_default_narration_plan_selection(plan)


def _alternate_selection(plan: dict, selection: dict) -> dict:
    alternate = deepcopy(selection)
    clauses = {row["claim_ref"]: row for row in plan["clauses"]}
    for occurrence in alternate["occurrences"]:
        for selected in occurrence["clauses"]:
            allowed = clauses[selected["claim_ref"]]["allowed_atom_ids"]
            selected["atom_id"] = allowed[-1]
    return alternate


def test_code_authored_plan_is_source_free_typed_and_occurrence_ordered():
    plan, default = _plan_and_selection()
    request = build_narration_plan_request(plan)

    assert plan["schema"] == "narration-realization-plan/1"
    assert plan["required_occurrence_refs"] == plan["allowed_occurrence_refs"]
    assert plan["required_occurrence_refs"] == [
        "settlement.player.hit",
        "opposition.guard.hit",
    ]
    assert [row["occurrence_ref"] for row in request["occurrences"]] \
        == plan["required_occurrence_refs"]
    assert default["schema"] == NARRATION_PLAN_SELECTION_SCHEMA
    assert default["plan_fingerprint"] == plan["fingerprint"]
    assert "I strike" not in json.dumps(plan, ensure_ascii=False)

    required_slot_types = {
        "actor",
        "target",
        "effect",
        "scope",
        "causality",
        "attribution",
        "pending_intent",
    }
    assert all(
        {slot["slot_type"] for slot in clause["slots"]} == required_slot_types
        for clause in plan["clauses"]
    )
    opposition_clause = next(
        row for row in plan["clauses"]
        if row["occurrence_ref"] == "opposition.guard.hit"
    )
    pending = next(
        slot for slot in opposition_clause["slots"]
        if slot["slot_type"] == "pending_intent"
    )
    assert pending["value"] == {
        "intent_ref": "intent.guard.one",
        "consumption": "single_use",
    }


def test_default_and_alternate_multi_occurrence_selection_render_exact_proof_graphs():
    plan, default = _plan_and_selection()
    alternate = _alternate_selection(plan, default)

    default_artifact = render_narration_plan_selection(plan, default)
    alternate_artifact = render_narration_plan_selection(plan, alternate)
    again = render_narration_plan_selection(plan, alternate)

    assert default_artifact.text != alternate_artifact.text
    assert default_artifact.text.splitlines()[0].startswith("Arinvale")
    assert alternate_artifact == again
    assert [row["occurrence_ref"] for row in alternate_artifact.expected_graph["claims"]] \
        == [row["occurrence_ref"] for row in plan["clauses"]]
    assert alternate_artifact.expected_graph["claims"] \
        == alternate_artifact.observed_graph["claims"]
    projections = [
        canonical_claim_projection(graph)
        for graph in (
            alternate_artifact.expected_graph,
            alternate_artifact.observed_graph,
            alternate_artifact.ledger_graph,
        )
    ]
    assert projections[0] == projections[1] == projections[2]


@pytest.mark.parametrize("stream", [False, True])
def test_rendered_artifact_plugs_directly_into_existing_accepted_delivery_proof(
    stream: bool,
):
    plan, default = _plan_and_selection()
    candidate = build_proof_complete_narration_candidate(
        plan,
        default,
        model="test-narrator",
        stream=stream,
        logical_message_identity=fingerprint({"logical-message": "test"}),
    )
    proof = candidate.delivery_proof

    assert candidate.plan == plan
    assert decode_chat_story(candidate.wire_bytes, candidate.content_type) \
        == candidate.rendered.text
    assert proof["artifact_kind"] == "accepted"
    assert proof["comparisons"] == {
        "mode": "semantic_claim_multiset",
        "observed_equals_expected": True,
        "observed_matches_ledger": True,
    }


def test_every_supported_non_aoe_claim_kind_has_two_exact_code_atoms():
    plan = build_narration_realization_plan(_all_kind_contract())
    default = build_default_narration_plan_selection(plan)
    alternate = _alternate_selection(plan, default)
    first = render_narration_plan_selection(plan, default)
    second = render_narration_plan_selection(plan, alternate)

    assert first.text != second.text
    assert {row["kind"] for row in first.expected_graph["claims"]} == {
        "harm",
        "defeat",
        "status",
        "resource",
        "time",
        "movement",
        "world",
    }
    assert all(len(row["allowed_atom_ids"]) == 2 for row in plan["clauses"])
    assert first.expected_graph["claims"] == first.observed_graph["claims"]
    assert second.expected_graph["claims"] == second.observed_graph["claims"]


def test_empty_ledger_plan_remains_exact_claim_free_code_fallback():
    packet = build_narrator_realization(4)
    plan = build_narration_realization_plan(
        build_narration_truth_contract(
            packet,
            lifecycle_binding={
                "branch_ref": "branch.empty",
                "ledger_fingerprint": fingerprint({"ledger": "empty"}),
                "artifact_fingerprint": packet["fingerprint"],
            },
        )
    )
    artifact = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )

    assert plan["required_occurrence_refs"] == []
    assert artifact.expected_graph["claims"] == []
    assert artifact.observed_graph["claims"] == []
    assert artifact.ledger_graph["claims"] == []


def test_claim_free_settled_skill_event_gets_bounded_qualitative_narration():
    contract = _qualitative_contract(outcomes=("success",), targets=(None,))
    plan = build_narration_realization_plan(contract)
    selection = build_default_narration_plan_selection(plan)
    rendered = render_narration_plan_selection(plan, selection)
    alternate = render_narration_plan_selection(
        plan,
        _alternate_selection(plan, selection),
    )

    assert contract["expected_claims"] == []
    assert plan["required_occurrence_refs"] == ["settlement.skill.0"]
    assert "Arinvale" in rendered.text
    assert "Brace check" in rendered.text
    assert "success" in rendered.text
    assert "Brace" in rendered.text
    assert "damage" not in rendered.text.lower()
    assert "hp" not in rendered.text.lower()
    assert [row["kind"] for row in rendered.expected_graph["claims"]] \
        == ["qualitative_action"]
    assert rendered.expected_graph["claims"] == rendered.observed_graph["claims"]
    assert canonical_claim_projection(rendered.expected_graph) \
        == canonical_claim_projection(rendered.ledger_graph)
    assert alternate.text != rendered.text
    assert canonical_claim_projection(alternate.expected_graph) \
        == canonical_claim_projection(rendered.expected_graph)
    assert all(
        set(clause) == {"claim_ref", "atom_id", "slot_ids"}
        for occurrence in selection["occurrences"]
        for clause in occurrence["clauses"]
    )


@pytest.mark.parametrize(
    ("outcome", "surface"),
    [
        ("crit_fail", "critical failure"),
        ("fail", "failure"),
        ("partial", "partial success"),
        ("success", "success"),
        ("crit_success", "critical success"),
        ("automatic", "an automatic outcome"),
    ],
)
def test_every_typed_qualitative_outcome_has_one_exact_code_surface(
    outcome: str,
    surface: str,
):
    plan = build_narration_realization_plan(
        _qualitative_contract(outcomes=(outcome,), targets=(None,))
    )
    rendered = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )

    assert surface in rendered.text
    assert rendered.expected_graph["claims"][0]["detail"] \
        == f"brace:skill_check:{outcome}:impact_none"


def test_targeted_qualitative_event_preserves_exact_actor_target_and_result():
    contract = _qualitative_contract(outcomes=("partial",), targets=("guard",))
    plan = build_narration_realization_plan(contract)
    rendered = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )
    claim = rendered.expected_graph["claims"][0]

    assert "Arinvale" in rendered.text
    assert "Ash Guard" in rendered.text
    assert "partial success" in rendered.text
    assert claim["actor_id"] == "player.arinvale"
    assert claim["subject_ids"] == ["guard"]
    assert claim["detail"] == "brace:skill_check:partial:impact_none"


def test_zero_impact_weapon_attempt_gets_result_without_invented_harm():
    contract = _zero_impact_weapon_contract()
    plan = build_narration_realization_plan(contract)
    rendered = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )

    assert contract["expected_claims"] == []
    assert "Arinvale" in rendered.text
    assert "weapon attack" in rendered.text
    assert "Ash Guard" in rendered.text
    assert "failure" in rendered.text
    assert "harm" not in rendered.text.lower()
    assert "damage" not in rendered.text.lower()
    assert rendered.expected_graph["claims"][0]["kind"] == "qualitative_action"


@pytest.mark.parametrize("slot_type", ["actor", "target", "effect"])
def test_qualitative_selection_cannot_substitute_actor_target_or_result(slot_type: str):
    plan = build_narration_realization_plan(
        _qualitative_contract(outcomes=("success",), targets=("guard",))
    )
    selection = build_default_narration_plan_selection(plan)
    selected = selection["occurrences"][0]["clauses"][0]
    slot_index = next(
        index
        for index, row in enumerate(plan["clauses"][0]["slots"])
        if row["slot_type"] == slot_type
    )
    selected["slot_ids"][slot_index] = _fp(
        {"forged_qualitative_slot": slot_type}
    )

    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, selection)


def test_qualitative_selection_cannot_invent_effect_or_mechanical_atom():
    plan = build_narration_realization_plan(_qualitative_contract())
    selection = build_default_narration_plan_selection(plan)
    mechanical = deepcopy(selection)
    mechanical["occurrences"][0]["clauses"][0]["atom_id"] = "claim.actor.direct/1"
    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, mechanical)

    invented = deepcopy(selection)
    invented["occurrences"][0]["clauses"][0]["effect"] = {
        "kind": "harm",
        "amount": -999,
    }
    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, invented)


def test_two_qualitative_occurrences_cannot_lend_slots_or_be_duplicated_or_omitted():
    plan = build_narration_realization_plan(
        _qualitative_contract(
            outcomes=("success", "fail"),
            targets=("guard", "scout"),
        )
    )
    selection = build_default_narration_plan_selection(plan)
    first = selection["occurrences"][0]["clauses"][0]
    second = selection["occurrences"][1]["clauses"][0]
    lent = deepcopy(selection)
    lent["occurrences"][0]["clauses"][0]["slot_ids"] = list(second["slot_ids"])
    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, lent)

    duplicated = deepcopy(selection)
    duplicated["occurrences"].append(deepcopy(selection["occurrences"][0]))
    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, duplicated)

    missing = deepcopy(selection)
    missing["occurrences"].pop()
    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, missing)

    assert first["claim_ref"] != second["claim_ref"]


def test_qualitative_plan_wire_replay_and_fork_rebind_are_exact_and_deterministic():
    contract = _qualitative_contract(outcomes=("crit_success",), targets=("guard",))
    first_plan = build_narration_realization_plan(contract)
    second_plan = build_narration_realization_plan(contract)
    first_selection = build_default_narration_plan_selection(first_plan)
    second_selection = build_default_narration_plan_selection(second_plan)
    message_id = fingerprint({"logical-message": "qualitative-deterministic"})
    first = build_proof_complete_narration_candidate(
        first_plan,
        first_selection,
        model="test-narrator",
        stream=False,
        logical_message_identity=message_id,
    )
    second = build_proof_complete_narration_candidate(
        second_plan,
        second_selection,
        model="test-narrator",
        stream=False,
        logical_message_identity=message_id,
    )
    child = rebind_narration_realization_plan(first_plan, branch_ref="branch.child")

    assert first_plan == second_plan
    assert first_selection == second_selection
    assert first == second
    assert first.wire_bytes == second.wire_bytes
    assert child["semantic_truth_basis"] == first_plan["semantic_truth_basis"]
    assert child["source_lifecycle_binding"] == first_plan["source_lifecycle_binding"]
    assert child["lifecycle_binding"]["branch_ref"] == "branch.child"
    assert render_narration_plan_selection(
        child, build_default_narration_plan_selection(child)
    ).text == first.rendered.text


def test_paid_skill_check_keeps_capability_and_result_without_inventing_resource_amounts():
    contract = _qualitative_contract(settled_change_kinds=("cost",))
    plan = build_narration_realization_plan(contract)
    rendered = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )

    assert len(plan["clauses"]) == 1
    assert "Brace check" in rendered.text
    assert "success" in rendered.text
    assert "damage" not in rendered.text.lower()
    assert "cost" not in rendered.text.lower()
    assert rendered.expected_graph["claims"][0]["detail"] \
        == "brace:skill_check:success:impact_none"


def test_unresolved_attempt_remains_generic_until_stage_and_reason_are_retained():
    contract = _unresolved_attempt_contract()
    plan = build_narration_realization_plan(contract)
    rendered = render_narration_plan_selection(
        plan,
        build_default_narration_plan_selection(plan),
    )

    assert contract["player_events"][0]["event_state"] == "unresolved"
    assert contract["player_events"][0]["outcome_quality"] is None
    assert plan["clauses"] == []
    assert rendered.text == EMPTY_FALLBACK_TEXT


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_atom",
        "missing_occurrence",
        "duplicate_occurrence",
        "reordered_occurrence",
        "missing_clause",
        "duplicate_clause",
        "reordered_clause",
        "wrong_claim",
        "substituted_actor",
        "substituted_target",
        "substituted_effect",
        "substituted_cause",
        "substituted_pending_intent",
        "substituted_value",
        "extra_clause_field",
        "wrong_plan",
        "wrong_library",
    ],
)
def test_unknown_duplicate_missing_or_substituted_selection_fails_closed(mutation: str):
    plan, selection = _plan_and_selection()
    bad = deepcopy(selection)
    first_occurrence = bad["occurrences"][0]
    first_clause = first_occurrence["clauses"][0]

    if mutation == "unknown_atom":
        first_clause["atom_id"] = "model.free-form/999"
    elif mutation == "missing_occurrence":
        bad["occurrences"].pop()
    elif mutation == "duplicate_occurrence":
        bad["occurrences"].append(deepcopy(first_occurrence))
    elif mutation == "reordered_occurrence":
        bad["occurrences"].reverse()
    elif mutation == "missing_clause":
        first_occurrence["clauses"].pop()
    elif mutation == "duplicate_clause":
        first_occurrence["clauses"].append(deepcopy(first_clause))
    elif mutation == "reordered_clause":
        bad["occurrences"][1]["clauses"].reverse()
    elif mutation == "wrong_claim":
        first_clause["claim_ref"] = _fp({"forged": "claim"})
    elif mutation.startswith("substituted_"):
        slot_type = mutation.removeprefix("substituted_")
        if slot_type == "value":
            first_clause["value"] = {"actor_id": "guard.forged"}
        else:
            if slot_type == "cause":
                slot_type = "causality"
            plan_clause = next(
                row for row in plan["clauses"]
                if row["claim_ref"] == first_clause["claim_ref"]
            )
            slot_index = next(
                index for index, row in enumerate(plan_clause["slots"])
                if row["slot_type"] == slot_type
            )
            first_clause["slot_ids"][slot_index] = _fp(
                {"forged_slot": slot_type}
            )
    elif mutation == "extra_clause_field":
        first_clause["prose"] = "The guard dies."
    elif mutation == "wrong_plan":
        bad["plan_fingerprint"] = _fp({"plan": "forged"})
    elif mutation == "wrong_library":
        bad["phrase_atom_library_version"] = "model-atoms/1"

    with pytest.raises(NarrationPlanRuntimeError):
        render_narration_plan_selection(plan, bad)


@pytest.mark.parametrize(
    "raw",
    [
        "The guard dies.",
        '{"schema":"narration-plan-selection/1"} trailing prose',
        '```json\n{"schema":"narration-plan-selection/1"}\n```',
        "[]",
    ],
)
def test_free_form_or_wrapped_model_output_never_becomes_a_selection(raw: str):
    plan, _selection = _plan_and_selection()
    with pytest.raises(NarrationPlanRuntimeError):
        validate_narration_plan_selection(raw, plan)


def test_exact_json_model_selection_is_accepted_but_extra_top_level_text_is_not():
    plan, selection = _plan_and_selection()
    raw = json.dumps(selection, ensure_ascii=False)

    assert validate_narration_plan_selection(raw, plan) == selection
    with pytest.raises(NarrationPlanRuntimeError):
        validate_narration_plan_selection(raw + "\nI add a death.", plan)


def test_blind_observer_rejects_changed_or_extra_visible_clause():
    plan, selection = _plan_and_selection()
    artifact = render_narration_plan_selection(plan, selection)

    with pytest.raises(NarrationPlanRuntimeError):
        observe_narration_plan_text(
            artifact.text.replace("hits", "kills", 1),
            plan["observation_context"],
        )
    with pytest.raises(NarrationPlanRuntimeError):
        observe_narration_plan_text(
            artifact.text + "\nThe guard dies.",
            plan["observation_context"],
        )


def test_exact_fork_rebind_rotates_only_lineage_and_keeps_complete_plan_basis():
    plan, selection = _plan_and_selection()
    rebound = rebind_narration_realization_plan(plan, branch_ref="branch.child")
    child_selection = build_default_narration_plan_selection(rebound)

    assert rebound["fingerprint"] != plan["fingerprint"]
    assert rebound["lifecycle_binding"] == {
        **plan["lifecycle_binding"],
        "branch_ref": "branch.child",
    }
    assert rebound["semantic_truth_basis"] == plan["semantic_truth_basis"]
    assert rebound["semantic_truth_basis_fingerprint"] \
        == plan["semantic_truth_basis_fingerprint"]
    assert rebound["source_lifecycle_binding"] == plan["source_lifecycle_binding"]
    assert rebound["clauses"] == plan["clauses"]
    assert rebound["ledger_graph"] == plan["ledger_graph"]
    assert render_narration_plan_selection(plan, selection).text \
        == render_narration_plan_selection(rebound, child_selection).text


def test_model_request_exposes_values_but_selection_can_return_ids_only():
    plan, selection = _plan_and_selection()
    request = build_narration_plan_request(plan)

    catalog = request["occurrences"][0]["clauses"][0]["slot_catalog"]
    assert all(set(row) == {"slot_id", "slot_type", "value", "fingerprint"} for row in catalog)
    selected_clause = selection["occurrences"][0]["clauses"][0]
    assert set(selected_clause) == {"claim_ref", "atom_id", "slot_ids"}
    assert request["response_contract"]["values"] == "forbidden"


def test_plan_request_selection_and_final_wire_are_byte_deterministic():
    first_plan = build_narration_realization_plan(_contract())
    second_plan = build_narration_realization_plan(_contract())
    first_selection = build_default_narration_plan_selection(first_plan)
    second_selection = build_default_narration_plan_selection(second_plan)
    message_id = fingerprint({"logical-message": "deterministic"})
    first = build_proof_complete_narration_candidate(
        first_plan,
        first_selection,
        model="test-narrator",
        stream=True,
        logical_message_identity=message_id,
    )
    second = build_proof_complete_narration_candidate(
        second_plan,
        second_selection,
        model="test-narrator",
        stream=True,
        logical_message_identity=message_id,
    )

    assert first_plan == second_plan
    assert build_narration_plan_request(first_plan) \
        == build_narration_plan_request(second_plan)
    assert first_selection == second_selection
    assert first == second
    assert first.wire_bytes == second.wire_bytes


def test_self_resealed_atom_text_cannot_replace_the_code_authored_plan_basis():
    plan, _selection = _plan_and_selection()
    bad = deepcopy(plan)
    clause = bad["clauses"][0]
    variant = clause["atom_variants"][0]
    replacement = "The guard dies."
    variant["text"] = replacement
    variant["text_fingerprint"] = raw_fingerprint(replacement.encode("utf-8"))
    variant["expected_claim"]["span_end"] = len(replacement.encode("utf-8"))
    variant["expected_claim"]["evidence_fingerprint"] = raw_fingerprint(
        replacement.encode("utf-8")
    )
    variant_payload = {key: value for key, value in variant.items() if key != "fingerprint"}
    variant["fingerprint"] = content_fingerprint(variant_payload)
    clause_payload = {key: value for key, value in clause.items() if key != "fingerprint"}
    clause["fingerprint"] = content_fingerprint(clause_payload)
    plan_payload = {key: value for key, value in bad.items() if key != "fingerprint"}
    bad["fingerprint"] = content_fingerprint(plan_payload)

    with pytest.raises(NarrationPlanRuntimeError):
        validate_narration_realization_plan(bad)


def test_plan_rejects_deferred_multi_target_multiplicity_instead_of_flattening_it():
    contract = _contract()
    bad = deepcopy(contract)
    bad["expected_claims"][0]["multiplicity"] = 2
    payload = {key: value for key, value in bad.items() if key != "fingerprint"}
    bad["fingerprint"] = content_fingerprint(payload)

    with pytest.raises(NarrationPlanRuntimeError):
        build_narration_realization_plan(bad)
