from __future__ import annotations

from copy import deepcopy

import pytest

import aetherstate.semantic_truth_runtime as runtime
from aetherstate.capability_glossary import content_fingerprint, normalize_phrase
from aetherstate.config import Config
from aetherstate.enemy_kits import build_enemy_kit, select_enemy_intent
from aetherstate.narration_fallback_runtime import (
    build_fallback_realization_plan,
    observe_fallback_claim_graph,
)
from aetherstate.semantic_truth_runtime import (
    SemanticTruthRuntimeError,
    build_runtime_truth_contract,
)
from aetherstate.state import _SPEC, apply_delta, current_state
from aetherstate.store import Store


TURN = 3
BRANCH = "branch.inventory"
LEDGER = content_fingerprint({"ledger": "inventory-post"})

EXPECTED_REDUCER_OPS = frozenset(
    {
        "ability_grant",
        "affinity_adj",
        "arousal",
        "award_exp",
        "battle_end",
        "battle_start",
        "battle_wave",
        "belief_acquire",
        "capability_assign",
        "check",
        "claim_record",
        "clash_record",
        "clock_tick",
        "clothing",
        "combat_end",
        "combatant_defeat",
        "combatant_hp",
        "combatant_spawn",
        "consent_set",
        "consent_signal",
        "contact",
        "craving",
        "creator_world_seed",
        "defeat_resolve",
        "effect_add",
        "effect_remove",
        "effect_update",
        "enemy_intent_set",
        "entity_add",
        "evolve_def",
        "fact_admit",
        "fact_retire",
        "freeze",
        "front_add",
        "front_reveal",
        "front_tick",
        "goal",
        "hp_adj",
        "item_consume",
        "item_equip",
        "item_gain",
        "item_lose",
        "item_mint",
        "item_move",
        "item_transfer",
        "item_unequip",
        "level_up",
        "loot_table",
        "master_tick",
        "mechanic_settlement_commit",
        "memory_event",
        "mood",
        "move_entity",
        "obsession",
        "player_seed",
        "position",
        "presence",
        "quest_add",
        "quest_update",
        "relationship_adj",
        "resource_change",
        "reveal_fact",
        "roll",
        "route_set",
        "scene_dial",
        "scene_mode",
        "scene_set",
        "semantic_binding_commit",
        "semantic_frame_commit",
        "semantic_meaning_commit",
        "semantic_world_alignment_commit",
        "set_attribute",
        "set_nemesis",
        "set_soulmate",
        "stagnation",
        "stat_spend",
        "tide_set",
        "time_advance",
        "unfreeze",
        "world_event_admit",
        "world_flag",
        "world_identity_set",
    }
)

EXPLICIT_OUTCOME_ADAPTERS = frozenset(
    {
        "mechanic_settlement_commit",
        "hp_adj",
        "enemy_intent_set",
        "defeat_resolve",
        "award_exp",
        "combatant_defeat",
        "combat_end",
    }
)


def _base_state() -> dict:
    return {
        "meta": {"turn": TURN},
        "entities": {
            "player.eranmor": {"name": "Eranmor", "present": True},
            "guard": {"name": "Guard", "present": True},
        },
        "player": {},
    }


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


def _build(state: dict, *ops: dict) -> dict:
    return build_runtime_truth_contract(
        state,
        branch_id=BRANCH,
        post_ledger_hash=LEDGER,
        turn_index=TURN,
        journal_rows=_journal(*ops),
    )


def test_reducer_inventory_is_exact_and_every_kind_has_one_audited_policy():
    assert len(_SPEC) == 82
    assert frozenset(_SPEC) == EXPECTED_REDUCER_OPS
    policies = (
        runtime._SEMANTIC_EVIDENCE_JOURNAL_OPS,
        runtime._BOOKKEEPING_ONLY_JOURNAL_OPS,
        runtime._SETTLEMENT_MEMBER_JOURNAL_OPS,
        EXPLICIT_OUTCOME_ADAPTERS,
    )
    for index, policy in enumerate(policies):
        assert not any(policy & later for later in policies[index + 1 :])
    audited = frozenset().union(*policies)
    assert audited <= EXPECTED_REDUCER_OPS
    assert len(EXPECTED_REDUCER_OPS - audited) == 63


@pytest.mark.parametrize(
    "kind",
    sorted(
        EXPECTED_REDUCER_OPS
        - runtime._SEMANTIC_EVIDENCE_JOURNAL_OPS
        - runtime._BOOKKEEPING_ONLY_JOURNAL_OPS
    ),
)
def test_every_nonmetadata_reducer_kind_fails_closed_without_exact_projection(kind: str):
    with pytest.raises(SemanticTruthRuntimeError):
        _build(_base_state(), {"op": kind})


@pytest.mark.parametrize(
    ("op", "passes"),
    [
        ({"op": "clock_tick", "minutes": 3}, True),
        ({"op": "clock_tick", "minutes": -1}, False),
        ({"op": "clock_tick", "minutes": True}, False),
        ({"op": "stagnation", "value": 0.82}, True),
        ({"op": "stagnation", "value": 1.01}, False),
        ({"op": "stagnation", "value": "0.5"}, False),
    ],
)
def test_bookkeeping_only_admission_is_exact_and_cannot_hide_world_time(op: dict, passes: bool):
    if passes:
        assert _build(_base_state(), op)["expected_claims"] == []
    else:
        with pytest.raises(SemanticTruthRuntimeError):
            _build(_base_state(), op)


def test_actual_store_diagnostic_clock_tick_terminalizes_as_empty_mechanics():
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="truth-inventory-clock")
    result = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "clock_tick", "minutes": 3}],
        "rule",
        cfg,
    )
    diagnostic = store.diagnostic_turn(branch_id, 0)

    contract = build_runtime_truth_contract(
        result.state,
        branch_id=branch_id,
        post_ledger_hash=content_fingerprint(result.state),
        turn_index=0,
        journal_rows=diagnostic["journal"],
    )

    assert diagnostic["journal"][0]["ops"][0]["op"] == "clock_tick"
    assert contract["expected_claims"] == []
    assert current_state(store, branch_id)["clock"]["minutes"] == 3


@pytest.mark.parametrize(
    ("kind", "ledger_key", "payload_key"),
    [
        ("semantic_meaning_commit", "semantic_meanings", "meaning"),
        ("semantic_binding_commit", "semantic_bindings", "binding"),
        (
            "semantic_world_alignment_commit",
            "semantic_world_alignments",
            "alignment",
        ),
    ],
)
def test_unsettled_semantic_metadata_requires_exact_durable_row_but_no_claim(
    kind: str, ledger_key: str, payload_key: str
):
    payload = {"fingerprint": content_fingerprint({"kind": kind})}
    state = _base_state()
    state[ledger_key] = [{"turn": TURN, payload_key: deepcopy(payload)}]
    op = {"op": kind, payload_key: payload}

    assert _build(state, op)["expected_claims"] == []

    forged = deepcopy(op)
    forged[payload_key] = {
        "fingerprint": content_fingerprint({"kind": kind, "forged": True})
    }
    with pytest.raises(SemanticTruthRuntimeError):
        _build(state, forged)


def test_unsettled_frame_still_requires_a_complete_realization():
    frame = {"fingerprint": content_fingerprint({"frame": "incomplete"})}
    state = _base_state()
    state["semantic_frames"] = [{"turn": TURN, "frame": deepcopy(frame)}]

    with pytest.raises(SemanticTruthRuntimeError, match="complete narrator realization"):
        _build(state, {"op": "semantic_frame_commit", "frame": frame})


def test_unrelated_op_cannot_borrow_a_real_settlement_reference():
    realization = {
        "asserted_settled": [],
        "asserted_unresolved": [],
        "attributed_noncurrent": [],
    }
    common = {
        "state": _base_state(),
        "turn": TURN,
        "settlement_refs": {"sha256:" + "1" * 64},
        "opposition": [],
        "pending_intents": [],
        "fallout": [],
        "outcomes": [],
        "realization": realization,
    }
    forged = {
        "op": "world_flag",
        "key": "gate_open",
        "value": True,
        "_settlement_ref": "sha256:" + "1" * 64,
    }
    member = {
        "op": "check",
        "_settlement_ref": "sha256:" + "1" * 64,
    }

    assert runtime._journal_op_is_covered(forged, **common) is False
    assert runtime._journal_op_is_covered(member, **common) is True


def _pending_state() -> tuple[dict, dict]:
    kit = build_enemy_kit("Guard", "standard", "sword", {})
    actor = {
        "id": "guard",
        "name": "Guard",
        "side": "enemy",
        "hp": {"cur": 10, "max": 10},
        "defeated": False,
        "kit": kit,
    }
    intent = select_enemy_intent(actor, TURN, "player.eranmor", "Eranmor")
    assert intent is not None
    state = _base_state()
    state["combat"] = {
        "active": True,
        "combatants": {"guard": actor},
        "pending_intent": intent,
        "history": [],
    }
    return state, intent


def test_exact_new_pending_intent_becomes_one_sealed_future_truth():
    state, intent = _pending_state()
    contract = _build(
        state,
        {
            "op": "enemy_intent_set",
            "actor": "guard",
            "_intent": deepcopy(intent),
        },
    )

    assert contract["fallout_facts"] == []
    assert len(contract["pending_intents"]) == 1
    fact = contract["pending_intents"][0]
    assert fact["intent_ref"] == intent["id"]
    assert fact["intent_snapshot"] == intent
    assert fact["actor_id"] == "guard"
    assert fact["target_id"] == "player.eranmor"
    assert fact["move_id"] == intent["move_id"]
    assert fact["tell"] == intent["tell"]
    assert fact["opening_kind"] == "following_intent"
    assert fact["response_window"] == "before_impact"
    assert fact["visible_text"].startswith(f"Guard: {intent['tell']}")
    assert len(contract["expected_claims"]) == 1
    claim = contract["expected_claims"][0]
    assert claim == {
        **claim,
        "occurrence_ref": fact["pending_ref"],
        "cause_ref": intent["id"],
        "construction_ref": fact["construction_ref"],
        "actor_id": "guard",
        "subject_ids": ["player.eranmor"],
        "kind": "pending_intent",
        "time_scope": "future",
        "detail": normalize_phrase(f"{intent['move_id']} {intent['tell']}"),
        "amount": None,
    }
    plan = build_fallback_realization_plan(contract)
    assert plan.text == fact["visible_text"]
    assert observe_fallback_claim_graph(
        plan.text, observation_context=plan.observation_context
    ) == plan.expected_graph == plan.ledger_graph


@pytest.mark.parametrize("field", ["id", "actor", "move_id", "target"])
def test_pending_intent_journal_metamorphics_cannot_substitute_identity(field: str):
    state, intent = _pending_state()
    forged = deepcopy(intent)
    forged[field] = f"forged-{field}"

    with pytest.raises(SemanticTruthRuntimeError):
        _build(
            state,
            {"op": "enemy_intent_set", "actor": "guard", "_intent": forged},
        )


def test_tracked_combatant_cid_can_map_to_exact_semantic_target_eid_by_frame():
    frame_ref = "sha256:" + "2" * 64
    realization = {
        "asserted_settled": [
            {
                "frame_ref": frame_ref,
                "event_ref": "event.player.strike",
            }
        ],
        "asserted_unresolved": [],
        "attributed_noncurrent": [],
    }
    outcome = {
        "source_event_ref": "event.player.strike",
        "target_id": "entity.guard",
        "effects": [{"kind": "defeat", "detail": "defeated", "amount": None}],
    }

    covered = runtime._journal_op_is_covered(
        {
            "op": "combatant_defeat",
            "target": "guard-combat-row",
            "_semantic_frame_ref": frame_ref,
        },
        state=_base_state(),
        turn=TURN,
        settlement_refs=set(),
        opposition=[],
        pending_intents=[],
        fallout=[],
        outcomes=[outcome],
        realization=realization,
    )

    assert covered is True
