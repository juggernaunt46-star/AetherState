from __future__ import annotations

from copy import deepcopy

import pytest

import aetherstate.semantic_truth_runtime as runtime
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_truth_gate import validate_narration_truth_contract
from aetherstate.narrator_realization import build_narrator_realization
from aetherstate.semantic_truth_runtime import (
    SemanticTruthRuntimeError,
    build_runtime_truth_contract,
)


TURN = 4
BRANCH = "branch.runtime-test"
LEDGER = content_fingerprint({"ledger": "post"})


def _base_state() -> dict:
    return {
        "meta": {"turn": TURN},
        "entities": {
            "player.eranmor": {"name": "Eranmor", "present": True},
            "guard": {"name": "Guard", "present": True},
            "iven": {"name": "Iven", "present": True},
        },
        "player": {},
    }


def _opposition(*, damage: int = 2, immediate_hp: int = 8) -> dict:
    return {
        "id": "intent.guard.3",
        "intent_id": "intent.guard.3",
        "actor": "guard",
        "actor_name": "Guard",
        "move_id": "driving_slash",
        "move_name": "Driving Slash",
        "target": "player.eranmor",
        "target_name": "Eranmor",
        "tier": "HITS" if damage else "MISSES",
        "damage": damage,
        "damage_before": damage,
        "damage_after": damage,
        "damage_saved": 0,
        "effect_id": "dmg.opp.guard.3",
        "turn": TURN,
        "delta": -damage,
        "hp_cur": immediate_hp,
        "hp_max": 10,
    }


def _state_with_opposition(*, damage: int = 2, immediate_hp: int = 8) -> dict:
    state = _base_state()
    state["player"] = {
        "player.eranmor": {
            "hp": {"cur": immediate_hp, "max": 10},
            "_hp_adj_last": {
                "turn": TURN,
                "delta": -damage,
                "toks": ["driving", "slash"],
            },
            "_opposition_last": _opposition(
                damage=damage, immediate_hp=immediate_hp
            ),
        }
    }
    return state


def _event_meaning() -> dict:
    return {
        "meaning_ref": content_fingerprint({"meaning": "player attack"}),
        "actor_id": "player.eranmor",
        "capability_id": "weapon_attack",
        "invoked_capability_ids": [],
        "action_class": "weapon_attack",
        "target_entity_id": "iven",
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


def _player_packet(
    *, defeated: bool = False, with_cost: bool = False
) -> tuple[dict, str, str]:
    frame_ref = content_fingerprint({"frame": "player attack"})
    event_ref = "event.player.attack"
    packet = build_narrator_realization(
        TURN,
        asserted_settled=[
            {
                "event_ref": event_ref,
                "adapter_id": "narrator.weapon-attack/1",
                "frame_ref": frame_ref,
                "event_meaning": _event_meaning(),
                "outcome_quality": "success",
                "impact_kind": "harm",
                "impact_magnitude": "decisive" if defeated else "solid",
                "target_state": "defeated" if defeated else "active",
                "settled_change_kinds": ["hp", *(["cost"] if with_cost else [])],
            }
        ],
    )
    return packet, frame_ref, event_ref


def _weapon_receipt(
    frame_ref: str, *, defeated: bool = False, with_cost: bool = False
) -> dict:
    meaning_ref = _event_meaning()["meaning_ref"]
    delta = -3 if defeated else -2
    post = 0 if defeated else 8
    identity = {
        "schema": "mechanic-settlement-identity/1",
        "contract_id": "weapon_attack/1",
        "frame_ref": frame_ref,
        "meaning_ref": meaning_ref,
        "target_entity_id": "iven",
    }
    applied_changes = [
        {"kind": "hp", "subject_id": "iven", "delta": delta, "post": post}
    ]
    if with_cost:
        applied_changes.append(
            {
                "kind": "cost",
                "subject_id": "player.eranmor",
                "resource_id": "stamina",
                "delta": -1,
                "post": 4,
            }
        )
    payload = {
        "schema": "mechanic-settlement/1",
        "settlement_ref": content_fingerprint(identity),
        "contract_id": "weapon_attack/1",
        "frame_ref": frame_ref,
        "meaning_ref": meaning_ref,
        "requirement_fingerprint": content_fingerprint({"requirement": "attack"}),
        "accepted_group_fingerprint": content_fingerprint({"group": "attack"}),
        "outcome": "defeat" if defeated else "hit",
        "outcome_quality": "success",
        "applied_changes": applied_changes,
        "target_post_state": {
            "combatant_id": "iven",
            "hp": {"cur": post, "max": 10},
        },
    }
    return {**payload, "receipt_fingerprint": content_fingerprint(payload)}


def _install_admitted_frame(state: dict, frame_ref: str) -> None:
    """Install the complete current semantic chain required by an admitted fixture."""
    meaning_ref = _event_meaning()["meaning_ref"]
    binding_ref = content_fingerprint({"binding": "player attack"})
    state["semantic_meanings"] = [
        {"turn": TURN, "meaning": {"fingerprint": meaning_ref}}
    ]
    state["semantic_bindings"] = [
        {
            "turn": TURN,
            "binding": {
                "fingerprint": binding_ref,
                "meaning_ref": meaning_ref,
            },
        }
    ]
    state["semantic_world_alignments"] = []
    state["semantic_frames"] = [
        {
            "turn": TURN,
            "frame": {
                "fingerprint": frame_ref,
                "meaning_ref": meaning_ref,
                "meaning_binding_ref": binding_ref,
                "world_alignment_refs": [],
                "target_entity_id": "iven",
                "target_name": "Iven",
            },
        }
    ]


def _journal(*ops: dict, source: str = "rule") -> list[dict]:
    return [
        {
            "id": 1,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": source,
            "ops": list(ops),
        }
    ]


def test_empty_committed_turn_builds_one_valid_empty_truth_contract():
    state = _base_state()

    contract = build_runtime_truth_contract(
        state, branch_id=BRANCH, post_ledger_hash=LEDGER
    )

    assert validate_narration_truth_contract(contract) == contract
    assert contract["turn"] == TURN
    assert contract["player_events"] == []
    assert contract["opposition_actions"] == []
    assert contract["fallout_facts"] == []
    assert contract["expected_claims"] == []
    assert contract["lifecycle_binding"] == {
        "branch_ref": BRANCH,
        "ledger_fingerprint": LEDGER,
        "artifact_fingerprint": contract["realization_fingerprint"],
    }


def test_empty_state_can_bind_to_explicit_lifecycle_turn_without_mutation():
    state = _base_state()
    state["meta"]["turn"] = -1
    before = deepcopy(state)

    contract = build_runtime_truth_contract(
        state,
        branch_id=BRANCH,
        post_ledger_hash=LEDGER,
        turn_index=TURN,
        journal_rows=[],
    )

    assert state == before
    assert contract["turn"] == TURN
    assert contract["expected_claims"] == []


def test_state_ahead_of_explicit_lifecycle_turn_fails_closed():
    state = _base_state()
    state["meta"]["turn"] = TURN + 1

    with pytest.raises(SemanticTruthRuntimeError, match="ahead of lifecycle"):
        build_runtime_truth_contract(
            state,
            branch_id=BRANCH,
            post_ledger_hash=LEDGER,
            turn_index=TURN,
        )


def test_behind_state_with_current_journal_mutation_cannot_use_empty_turn_override():
    state = _base_state()
    state["meta"]["turn"] = -1

    with pytest.raises(SemanticTruthRuntimeError, match="empty current ledger"):
        build_runtime_truth_contract(
            state,
            branch_id=BRANCH,
            post_ledger_hash=LEDGER,
            turn_index=TURN,
            journal_rows=_journal({"op": "effect_add", "char": "guard"}),
        )


def test_current_opposition_keeps_intent_action_and_exact_hp_as_separate_truth():
    state = _state_with_opposition()
    before = deepcopy(state)

    contract = build_runtime_truth_contract(
        state, branch_id=BRANCH, post_ledger_hash=LEDGER
    )

    assert state == before
    action = contract["opposition_actions"][0]
    assert action["intent_ref"] == "intent.guard.3"
    assert action["actor_id"] == "guard"
    assert action["target_id"] == "player.eranmor"
    assert action["move_id"] == "driving_slash"
    assert action["outcome"] == "hit"
    assert action["effects"] == [{"kind": "harm", "detail": "hp", "amount": -2}]
    claims = {row["kind"]: row for row in contract["expected_claims"]}
    assert claims["opposition_action"]["cause_ref"] == "intent.guard.3"
    assert claims["harm"]["cause_ref"] == action["occurrence_ref"]
    assert claims["harm"]["actor_id"] == "guard"
    assert claims["harm"]["amount"] == -2
    assert all(row["construction_ref"] != "player.frame" for row in claims.values())


def test_exact_opposition_journal_operation_is_covered_by_its_own_truth():
    state = _state_with_opposition()
    opposition_op = {
        "op": "hp_adj",
        "char": "player.eranmor",
        "delta": -2,
        "_delta": -2,
        "_effect_id": "dmg.opp.guard.3",
        "_opposition": {
            "intent_id": "intent.guard.3",
            "actor": "guard",
            "move_id": "driving_slash",
            "target": "player.eranmor",
        },
    }

    contract = build_runtime_truth_contract(
        state,
        branch_id=BRANCH,
        post_ledger_hash=LEDGER,
        turn_index=TURN,
        journal_rows=_journal(opposition_op),
    )

    assert contract["opposition_actions"][0]["intent_ref"] == "intent.guard.3"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "effect_add", "char": "guard", "effect": "burning"},
        {"op": "resource_change", "char": "player.eranmor", "delta": -1},
        {"op": "time_advance", "minutes": 5},
        {"op": "move_entity", "char": "guard", "destination": "gate"},
        {"op": "world_flag", "key": "gate_open", "value": True},
    ],
)
def test_unprojected_standalone_state_changes_fail_closed(op):
    with pytest.raises(SemanticTruthRuntimeError, match="no exact narration truth claim"):
        build_runtime_truth_contract(
            _base_state(),
            branch_id=BRANCH,
            post_ledger_hash=LEDGER,
            turn_index=TURN,
            journal_rows=_journal(op),
        )


def test_lethal_opposition_projects_immediate_harm_defeat_and_combat_end_fallout():
    state = _state_with_opposition(damage=2, immediate_hp=0)
    state["player"]["player.eranmor"]["hp"] = {"cur": 1, "max": 10}
    state["player"]["player.eranmor"]["defeated"] = {
        "turn": TURN,
        "outcome": "wake_safe",
    }
    state["combat"] = {
        "active": False,
        "combatants": {},
        "history": [
            {
                "turn": TURN,
                "started_turn": 1,
                "defeated": [],
                "survivors": ["Guard"],
                "loot": [],
                "outcome": "defeat",
            }
        ],
    }

    contract = build_runtime_truth_contract(
        state, branch_id=BRANCH, post_ledger_hash=LEDGER
    )

    action = contract["opposition_actions"][0]
    facts = {row["subject_id"]: row for row in contract["fallout_facts"]}
    assert facts["player.eranmor"]["cause_ref"] == action["occurrence_ref"]
    assert facts["player.eranmor"]["effects"] == [
        {"kind": "defeat", "detail": "wake safe", "amount": None}
    ]
    assert facts["world"]["effects"] == [
        {"kind": "world", "detail": "combat ended defeat", "amount": None}
    ]
    assert facts["world"]["cause_ref"] == action["occurrence_ref"]
    claims = {(row["kind"], row["detail"]): row for row in contract["expected_claims"]}
    assert claims[("harm", "hp")]["amount"] == -2
    assert claims[("defeat", "wake safe")]["cause_ref"] == action["occurrence_ref"]
    assert claims[("world", "combat ended defeat")]["actor_id"] is None


def test_player_weapon_receipt_projects_exact_hp_and_defeat_target_outcome(monkeypatch):
    packet, frame_ref, event_ref = _player_packet(defeated=True)
    monkeypatch.setattr(
        runtime, "build_narrator_realization_from_state", lambda _state: packet
    )
    state = _base_state()
    _install_admitted_frame(state, frame_ref)
    state["mechanic_settlements"] = [
        {"turn": TURN, "receipt": _weapon_receipt(frame_ref, defeated=True)}
    ]
    state["combat"] = {
        "active": True,
        "combatants": {
            "iven": {
                "name": "Iven",
                "eid": "iven",
                "hp": {"cur": 0, "max": 10},
                "_struck_turn": TURN,
                "defeated": True,
                "defeated_turn": TURN,
            }
        },
        "history": [],
    }

    contract = build_runtime_truth_contract(
        state, branch_id=BRANCH, post_ledger_hash=LEDGER
    )

    outcome = contract["settled_target_outcomes"][0]
    assert outcome["source_event_ref"] == event_ref
    assert outcome["target_id"] == "iven"
    assert outcome["effects"] == [
        {"kind": "defeat", "detail": "defeated", "amount": None},
        {"kind": "harm", "detail": "hp", "amount": -3},
    ]
    claims = {(row["kind"], row["amount"]): row for row in contract["expected_claims"]}
    assert claims[("harm", -3)]["occurrence_ref"] == event_ref
    assert claims[("defeat", None)]["actor_id"] == "player.eranmor"


def test_settlement_cost_is_separate_fallout_without_inflating_target_multiplicity(
    monkeypatch,
):
    packet, frame_ref, event_ref = _player_packet(with_cost=True)
    receipt = _weapon_receipt(frame_ref, with_cost=True)
    monkeypatch.setattr(
        runtime, "build_narrator_realization_from_state", lambda _state: packet
    )
    state = _base_state()
    _install_admitted_frame(state, frame_ref)
    state["mechanic_settlements"] = [{"turn": TURN, "receipt": receipt}]
    state["combat"] = {
        "active": True,
        "combatants": {
            "iven": {
                "name": "Iven",
                "eid": "iven",
                "hp": {"cur": 8, "max": 10},
                "_struck_turn": TURN,
                "defeated": False,
            }
        },
        "history": [],
    }
    journal = _journal(
        {
            "op": "mechanic_settlement_commit",
            "settlement_ref": receipt["settlement_ref"],
        },
        {
            "op": "combatant_hp",
            "_settlement_ref": receipt["settlement_ref"],
        },
        {
            "op": "check",
            "_settlement_ref": receipt["settlement_ref"],
        },
    )

    contract = build_runtime_truth_contract(
        state,
        branch_id=BRANCH,
        post_ledger_hash=LEDGER,
        turn_index=TURN,
        journal_rows=journal,
    )

    assert contract["settled_target_outcomes"][0]["target_id"] == "iven"
    fallout = contract["fallout_facts"][0]
    assert fallout["cause_ref"] == event_ref
    assert fallout["subject_id"] == "player.eranmor"
    assert fallout["effects"] == [
        {"kind": "resource", "detail": "stamina", "amount": -1}
    ]
    claims = {(row["kind"], row["detail"]): row for row in contract["expected_claims"]}
    assert claims[("harm", "hp")]["multiplicity"] == 1
    assert claims[("resource", "stamina")]["multiplicity"] == 1


def test_lethal_player_settlement_projects_tracked_cid_xp_and_combat_end_cascade(
    monkeypatch,
):
    packet, frame_ref, event_ref = _player_packet(defeated=True)
    receipt = _weapon_receipt(frame_ref, defeated=True)
    monkeypatch.setattr(
        runtime, "build_narrator_realization_from_state", lambda _state: packet
    )
    state = _base_state()
    _install_admitted_frame(state, frame_ref)
    state["mechanic_settlements"] = [{"turn": TURN, "receipt": receipt}]
    state["player"] = {
        "player.eranmor": {"name": "Eranmor", "xp": 50}
    }
    state["combat"] = {
        "active": False,
        "combatants": {},
        "history": [
            {
                "turn": TURN,
                "started_turn": 1,
                "defeated": ["Iven"],
                "survivors": [],
                "loot": [],
                "outcome": "victory",
            }
        ],
    }
    journal = _journal(
        {
            "op": "mechanic_settlement_commit",
            "settlement_ref": receipt["settlement_ref"],
        },
        {
            "op": "combatant_hp",
            "_settlement_ref": receipt["settlement_ref"],
        },
        {
            "op": "combatant_defeat",
            "target": "iven-combat-row",
            "_semantic_frame_ref": frame_ref,
        },
        {
            "op": "award_exp",
            "char": "player.eranmor",
            "amount": 50,
            "reason": "defeated Iven",
            "_semantic_frame_ref": frame_ref,
        },
        {
            "op": "combat_end",
            "outcome": "victory",
            "_semantic_frame_ref": frame_ref,
        },
    )

    contract = build_runtime_truth_contract(
        state,
        branch_id=BRANCH,
        post_ledger_hash=LEDGER,
        turn_index=TURN,
        journal_rows=journal,
    )

    fallout = {
        (effect["kind"], effect["detail"]): row
        for row in contract["fallout_facts"]
        for effect in row["effects"]
    }
    assert fallout[("resource", "experience")]["cause_ref"] == event_ref
    assert fallout[("world", "combat ended victory")]["cause_ref"] == event_ref
    claims = {(row["kind"], row["detail"]): row for row in contract["expected_claims"]}
    assert claims[("resource", "experience")]["amount"] == 50
    assert claims[("world", "combat ended victory")]["cause_ref"] == event_ref


def test_current_player_hp_change_without_exact_opposition_fails_closed():
    state = _base_state()
    state["player"] = {
        "player.eranmor": {
            "hp": {"cur": 8, "max": 10},
            "_hp_adj_last": {"turn": TURN, "delta": -2, "toks": ["wound"]},
        }
    }

    with pytest.raises(SemanticTruthRuntimeError, match="lacks one exact"):
        build_runtime_truth_contract(
            state, branch_id=BRANCH, post_ledger_hash=LEDGER
        )


def test_malformed_opposition_arithmetic_fails_before_contract_construction():
    state = _state_with_opposition()
    state["player"]["player.eranmor"]["_opposition_last"]["damage"] = 7

    with pytest.raises(SemanticTruthRuntimeError, match="damage and committed HP delta"):
        build_runtime_truth_contract(
            state, branch_id=BRANCH, post_ledger_hash=LEDGER
        )


def test_current_opposition_requires_a_same_turn_hp_receipt():
    state = _state_with_opposition()
    state["player"]["player.eranmor"]["_hp_adj_last"]["turn"] = TURN - 1

    with pytest.raises(SemanticTruthRuntimeError, match="committed Player HP receipt"):
        build_runtime_truth_contract(
            state, branch_id=BRANCH, post_ledger_hash=LEDGER
        )


def test_current_player_defeat_without_autonomous_cause_fails_closed():
    state = _base_state()
    state["player"] = {
        "player.eranmor": {
            "hp": {"cur": 1, "max": 10},
            "defeated": {"turn": TURN, "outcome": "wake_safe"},
        }
    }

    with pytest.raises(SemanticTruthRuntimeError, match="defeat lacks"):
        build_runtime_truth_contract(
            state, branch_id=BRANCH, post_ledger_hash=LEDGER
        )


def test_current_combatant_hp_or_defeat_cannot_disappear_without_settlement():
    state = _base_state()
    state["combat"] = {
        "active": True,
        "history": [],
        "combatants": {
            "guard": {
                "name": "Guard",
                "eid": "guard",
                "hp": {"cur": 0, "max": 10},
                "_struck_turn": TURN,
                "defeated": True,
                "defeated_turn": TURN,
            }
        },
    }

    with pytest.raises(SemanticTruthRuntimeError, match="HP change lacks"):
        build_runtime_truth_contract(
            state, branch_id=BRANCH, post_ledger_hash=LEDGER
        )


def test_lost_reply_empty_turn_preserves_realization_delivery_mode():
    state = _base_state()
    state["_settled_retry"] = {"kind": "lost_reply"}

    contract = build_runtime_truth_contract(
        state, branch_id=BRANCH, post_ledger_hash=LEDGER
    )

    assert contract["delivery_mode"] == "lost_reply_retry"
