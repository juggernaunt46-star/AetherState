"""Independent RED-first oracles for the journal transition truth boundary.

These tests intentionally describe the public contract before
``aetherstate.semantic_transition_truth`` exists.  They do not reuse the existing
post-state-only narrator truth adapters as their oracle.  State paths are RFC 6901
JSON pointers, journal identity is ``(row id, op index)``, and every changed leaf
must be covered exactly once by the transition that produced it.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import importlib
from typing import Any

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.extraction import EXTRACTION_OPS_RPG
from aetherstate.state import _SPEC, apply_delta, current_state, empty_state, reduce_state
from aetherstate.store import Store


TARGET_MODULE = "aetherstate.semantic_transition_truth"
TURN = 7
BRANCH = "branch.transition-matrix"

EXPECTED_POLICY_FAMILIES = {
    "semantic_evidence": frozenset(
        {
            "semantic_meaning_commit",
            "semantic_binding_commit",
            "semantic_world_alignment_commit",
            "semantic_frame_commit",
        }
    ),
    "atomic_settlement": frozenset({"mechanic_settlement_commit"}),
    "world_capability_authoring": frozenset(
        {"world_identity_set", "capability_assign"}
    ),
    "scene_identity_placement": frozenset(
        {
            "set_attribute",
            "move_entity",
            "presence",
            "entity_add",
            "scene_set",
            "scene_mode",
        }
    ),
    "embodied_affect_social": frozenset(
        {
            "clothing",
            "position",
            "contact",
            "arousal",
            "mood",
            "relationship_adj",
            "scene_dial",
            "obsession",
            "craving",
        }
    ),
    "consent_safety": frozenset(
        {"consent_signal", "consent_set", "freeze", "unfreeze"}
    ),
    "fact_memory_goal": frozenset(
        {"reveal_fact", "fact_retire", "memory_event", "goal"}
    ),
    "time_dice_bookkeeping": frozenset(
        {"time_advance", "clock_tick", "roll", "stagnation"}
    ),
    "player_operational": frozenset(
        {
            "check",
            "resource_change",
            "award_exp",
            "level_up",
            "master_tick",
            "defeat_resolve",
            "stat_spend",
        }
    ),
    "player_authoring": frozenset(
        {"player_seed", "ability_grant", "evolve_def"}
    ),
    "items": frozenset(
        {
            "item_mint",
            "item_move",
            "item_equip",
            "item_unequip",
            "item_consume",
            "item_transfer",
            "item_gain",
            "item_lose",
        }
    ),
    "effects": frozenset({"effect_add", "effect_remove", "effect_update"}),
    "social_world_standing": frozenset(
        {"affinity_adj", "set_soulmate", "set_nemesis", "world_flag"}
    ),
    "quests": frozenset({"quest_add", "quest_update"}),
    "player_hp": frozenset({"hp_adj"}),
    "operational_combat": frozenset(
        {
            "combatant_spawn",
            "enemy_intent_set",
            "combatant_hp",
            "combatant_defeat",
            "combat_end",
        }
    ),
    "combat_record_authoring": frozenset({"clash_record", "loot_table"}),
    "large_battle": frozenset(
        {"battle_start", "tide_set", "battle_wave", "battle_end"}
    ),
    "living_world": frozenset(
        {"front_add", "front_tick", "front_reveal", "route_set"}
    ),
}

EXPECTED_REDUCER_OPS = frozenset().union(*EXPECTED_POLICY_FAMILIES.values())
EXPECTED_SILENT_OPS = frozenset(
    {
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
        "clock_tick",
        "stagnation",
    }
)
EXPECTED_INDEX_ONLY_POST_DELIVERY_OPS = frozenset()
DEFERRED_POST_DELIVERY_INDEX_OPS = frozenset({"reveal_fact", "memory_event"})


def _target():
    try:
        return importlib.import_module(TARGET_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != TARGET_MODULE:
            raise
        pytest.fail(
            f"RED: {TARGET_MODULE} does not exist; implement the shared transition projector"
        )


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        assert name in value, f"missing required {name!r} field in {value!r}"
        return value[name]
    assert hasattr(value, name), f"missing required {name!r} field in {value!r}"
    return getattr(value, name)


def _base_state() -> dict:
    state = empty_state()
    state["meta"]["turn"] = TURN
    return state


def _reduced(pre_state: dict, *ops: dict) -> dict:
    state = deepcopy(pre_state)
    reduce_state(state, deepcopy(list(ops)))
    return state


def _journal(*ops: dict, row_id: int = 101, source: str = "rule") -> list[dict]:
    return [
        {
            "id": row_id,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": source,
            "ops": deepcopy(list(ops)),
        }
    ]


def _project(
    pre_state: dict,
    post_state: dict,
    *ops: dict,
    journal_rows: list[dict] | None = None,
    delivery_phase: str = "pre_display",
):
    target = _target()
    return target.project_journal_transitions(
        pre_state=deepcopy(pre_state),
        post_state=deepcopy(post_state),
        journal_rows=deepcopy(journal_rows if journal_rows is not None else _journal(*ops)),
        branch_id=BRANCH,
        turn_index=TURN,
        pre_ledger_hash=content_fingerprint(pre_state),
        post_ledger_hash=content_fingerprint(post_state),
        realization=None,
        delivery_phase=delivery_phase,
        typed_gate_receipt=None,
    )


_MISSING = object()


def _escape_pointer(token: object) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _leaf_values(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        if not value:
            return {path or "/": {}}
        out: dict[str, Any] = {}
        for key in sorted(value, key=str):
            out.update(_leaf_values(value[key], f"{path}/{_escape_pointer(key)}"))
        return out
    if isinstance(value, list):
        if not value:
            return {path or "/": []}
        out = {}
        for index, item in enumerate(value):
            out.update(_leaf_values(item, f"{path}/{index}"))
        return out
    return {path or "/": value}


def _changed_leaf_paths(before: dict, after: dict) -> set[str]:
    left, right = _leaf_values(before), _leaf_values(after)
    return {
        path
        for path in left.keys() | right.keys()
        if left.get(path, _MISSING) != right.get(path, _MISSING)
    }


def _transitions(projection: Any) -> list[Any]:
    rows = _member(projection, "transitions")
    assert isinstance(rows, (list, tuple))
    return list(rows)


def _effects(transition: Any) -> list[Any]:
    rows = _member(transition, "effects")
    assert isinstance(rows, (list, tuple))
    return list(rows)


def _covered_paths(transition: Any) -> set[str]:
    paths = _member(transition, "covered_paths")
    assert isinstance(paths, (list, tuple, set, frozenset))
    return {_canonical_pointer(path) for path in paths}


def _canonical_pointer(path: object) -> str:
    """Treat a missing leading slash as serialization, not a semantic difference."""
    text = str(path)
    return "/" if text == "/" else "/" + text.lstrip("/")


def _assert_one_exact_transition(pre_state: dict, op: dict, expected_paths: set[str]):
    post_state = _reduced(pre_state, op)
    actual_changed = _changed_leaf_paths(pre_state, post_state)
    assert expected_paths <= actual_changed, "fixture no longer exercises its implicit effects"

    projection = _project(pre_state, post_state, op)
    assert _member(projection, "schema") == "semantic-journal-transition-projection/4"
    rows = _transitions(projection)
    assert len(rows) == 1
    transition = rows[0]
    assert _member(transition, "journal_id") == 101
    assert _member(transition, "op_index") == 0
    assert _member(transition, "op_kind") == op["op"]
    assert _member(transition, "status") == "changed"
    assert _covered_paths(transition) == actual_changed
    assert {
        _canonical_pointer(_member(effect, "path")) for effect in _effects(transition)
    } == actual_changed
    return projection, transition, post_state


def test_oracle_inventory_itself_is_77_unique_ops_with_no_family_overlap():
    assert len(EXPECTED_REDUCER_OPS) == 77
    families = list(EXPECTED_POLICY_FAMILIES.values())
    for index, family in enumerate(families):
        assert not any(family & later for later in families[index + 1 :])
    assert len(EXPECTED_SILENT_OPS) == 6
    assert EXPECTED_SILENT_OPS <= EXPECTED_REDUCER_OPS


def test_transition_policy_registry_welds_exactly_to_the_live_77_op_reducer():
    target = _target()
    policies = target.TRANSITION_POLICIES

    assert len(_SPEC) == 77
    assert frozenset(_SPEC) == EXPECTED_REDUCER_OPS
    assert frozenset(policies) == EXPECTED_REDUCER_OPS
    assert frozenset(target.SILENT_OPS) == EXPECTED_SILENT_OPS

    for family, kinds in EXPECTED_POLICY_FAMILIES.items():
        for kind in kinds:
            policy = policies[kind]
            assert _member(policy, "kind") == kind
            assert _member(policy, "family") == family
            assert _member(policy, "priority") == (
                "internal" if kind in EXPECTED_SILENT_OPS else "p0"
            )
            assert _member(policy, "visibility") in {
                "internal",
                "allowed",
                "required",
            }
            assert _member(policy, "actor_source") != "infer_from_subject"
            assert _member(policy, "cause_source") != "infer_from_subject"
            assert _member(policy, "value_source")
            assert _member(policy, "pre_post_proof")
            subject_fields = _member(policy, "subject_fields")
            assert isinstance(subject_fields, (list, tuple, set, frozenset))


def test_only_the_six_explicit_internal_ops_may_omit_narrator_claims():
    target = _target()
    policies = target.TRANSITION_POLICIES
    internal = {
        kind
        for kind, policy in policies.items()
        if _member(policy, "visibility") == "internal"
    }
    assert internal == EXPECTED_SILENT_OPS

    for op in (
        {"op": "clock_tick", "minutes": 4, "_turn": TURN},
        {"op": "stagnation", "value": 0.75, "_turn": TURN},
    ):
        pre_state = _base_state()
        post_state = _reduced(pre_state, op)
        projection = _project(pre_state, post_state, op)
        transition = _transitions(projection)[0]
        assert _member(transition, "status") == "changed"
        assert _covered_paths(transition) == _changed_leaf_paths(pre_state, post_state)
        assert {_member(effect, "visibility") for effect in _effects(transition)} == {
            "internal"
        }
        assert list(_member(projection, "required_facts")) == []


def test_scene_set_projects_location_registry_visit_move_and_pool_recovery():
    pre = _base_state()
    pre["scene"] = {"location_id": "village", "scene_index": 2}
    pre["entities"]["village"] = {
        "kind": "location",
        "name": "Village",
        "aliases": [],
        "location_id": None,
        "present": False,
    }
    pre["player"]["hero"] = {
        "resources": {
            "stamina": {"cur": 2, "max": 8},
            "mana": {"cur": 1, "max": 10},
        }
    }
    op = {
        "op": "scene_set",
        "location": "forest",
        "_loc_create": {"eid": "forest", "name": "Forest"},
        "_canon": True,
        "_loc_alias": "Dark Forest",
        "_prev_loc": "village",
        "_turn": TURN,
    }
    _assert_one_exact_transition(
        pre,
        op,
        {
            "/scene/location_id",
            "/scene/scene_index",
            "/scene/last_move/from",
            "/entities/forest/visits",
            "/entities/forest/last_visit_turn",
            "/player/hero/resources/stamina/cur",
            "/player/hero/resources/mana/cur",
        },
    )


def test_time_advance_projects_clock_rest_resources_craving_and_withdrawal():
    pre = _base_state()
    pre["player"]["hero"] = {
        "resources": {
            "stamina": {"cur": 2, "max": 8},
            "mana": {"cur": 1, "max": 10},
        }
    }
    pre["chars"]["hero"] = {
        "cravings": {
            "aether": {
                "level": 68,
                "dependency": 60,
                "ramp": 5,
                "withdrawal_effects": ["shakes"],
                "_seed": {"withdrawal_level": 70, "withdrawal_dependency": 50},
            }
        },
        "status_effects": [],
    }
    op = {
        "op": "time_advance",
        "to_time_of_day": "night",
        "calendar_note": "First Watch",
        "_turn_mark": True,
        "_turn": TURN,
    }
    _assert_one_exact_transition(
        pre,
        op,
        {
            "/clock/time_of_day",
            "/clock/calendar_note",
            "/clock/last_advance_turn",
            "/player/hero/resources/stamina/cur",
            "/player/hero/resources/mana/cur",
            "/chars/hero/cravings/aether/level",
            "/chars/hero/status_effects/0",
        },
    )


def test_safeword_projects_freeze_identity_and_participant_withdrawal_only():
    pre = _base_state()
    pre["scene"] = {"participants": ["hero", "ally"]}
    pre["consent"] = {
        "hero|ally|touch": {
            "level": "active",
            "max_intensity": None,
            "history": [],
        },
        "stranger|other|touch": {
            "level": "active",
            "max_intensity": None,
            "history": [],
        },
    }
    op = {
        "op": "consent_signal",
        "from_char": "hero",
        "to_char": "ally",
        "category": "touch",
        "signal": "safeword",
        "_turn": TURN,
    }
    _, _, post = _assert_one_exact_transition(
        pre,
        op,
        {
            "/frozen",
            "/frozen_reason",
            "/frozen_turn",
            "/consent/hero|ally|touch/level",
        },
    )
    assert post["consent"]["stranger|other|touch"]["level"] == "active"


def test_level_up_projects_every_code_owned_grant_not_only_the_level_number():
    pre = _base_state()
    pre["player"]["hero"] = {
        "level": 2,
        "hp": {"cur": 5, "max": 10},
        "resources": {
            "stamina": {"cur": 2, "max": 8},
            "mana": {"cur": 1, "max": 10},
        },
        "stat_points": 0,
    }
    op = {
        "op": "level_up",
        "char": "hero",
        "_grants": {"hp": 4, "pool": 2, "stat_points": 1},
        "_turn": TURN,
    }
    _assert_one_exact_transition(
        pre,
        op,
        {
            "/player/hero/level",
            "/player/hero/hp/cur",
            "/player/hero/hp/max",
            "/player/hero/resources/stamina/cur",
            "/player/hero/resources/stamina/max",
            "/player/hero/resources/mana/cur",
            "/player/hero/resources/mana/max",
            "/player/hero/stat_points",
        },
    )


def test_battle_wave_projects_wave_momentum_clamp_and_bounded_log_entry():
    pre = _base_state()
    pre["battle"] = {
        "active": True,
        "name": "Siege",
        "momentum": 2,
        "waves": 1,
        "threat": "standard",
        "foe": "raiders",
        "wave_size": 2,
        "started_turn": 1,
        "log": [],
    }
    op = {"op": "battle_wave", "_delta": 9, "_turn": TURN}
    _, transition, post = _assert_one_exact_transition(
        pre,
        op,
        {"/battle/waves", "/battle/momentum", "/battle/log/0/2"},
    )
    assert post["battle"]["momentum"] == 3
    momentum = next(
        effect for effect in _effects(transition)
        if _canonical_pointer(_member(effect, "path")) == "/battle/momentum"
    )
    assert _member(momentum, "before") == 2
    assert _member(momentum, "after") == 3
    assert _member(momentum, "delta") == 1


def test_front_completion_projects_consequence_visibility_and_terminal_markers():
    pre = _base_state()
    pre["fronts"] = {
        "doom": {
            "name": "Doom",
            "segments": 3,
            "filled": 2,
            "pace": 1,
            "consequence": "The gate falls",
            "revealed": False,
            "done": False,
            "created_turn": 1,
            "log": [],
        }
    }
    op = {
        "op": "front_tick",
        "front": "doom",
        "_delta": 1,
        "reason": "The last seal breaks",
        "_turn": TURN,
    }
    _assert_one_exact_transition(
        pre,
        op,
        {
            "/fronts/doom/filled",
            "/fronts/doom/done",
            "/fronts/doom/revealed",
            "/fronts/doom/filled_turn",
            "/fronts/doom/log/0/1",
        },
    )


def test_item_consume_projects_quantity_indexes_ownership_hp_and_resource_restore():
    pre = _base_state()
    pre["player"]["hero"] = {
        "hp": {"cur": 3, "max": 10},
        "resources": {"mana": {"cur": 1, "max": 5}},
    }
    pre["items"] = {
        "potion.1": {
            "template_id": "potion",
            "name": "Potion",
            "qty": 1,
            "loc": "inv:loose",
            "owner": "hero",
            "on_consume": {"heal": 4, "restore": {"mana": 3}},
        }
    }
    pre["inventory"] = {"hero": {"loose": ["potion.1"]}}
    op = {
        "op": "item_consume",
        "instance": "potion.1",
        "amount": 1,
        "_turn": TURN,
    }
    _assert_one_exact_transition(
        pre,
        op,
        {
            "/items/potion.1/qty",
            "/items/potion.1/loc",
            "/items/potion.1/owner",
            "/inventory/hero/loose/0",
            "/player/hero/hp/cur",
            "/player/hero/resources/mana/cur",
        },
    )


def test_requested_magnitude_never_overrides_clamped_pre_post_truth():
    pre = _base_state()
    pre["player"]["hero"] = {"hp": {"cur": 1, "max": 10}}
    clamp_op = {
        "op": "hp_adj",
        "char": "hero",
        "delta": -999,
        "_delta": -999,
        "_turn": TURN,
    }
    post = _reduced(pre, clamp_op)
    projection = _project(pre, post, clamp_op)
    transition = _transitions(projection)[0]
    hp_effect = next(
        effect for effect in _effects(transition)
        if _canonical_pointer(_member(effect, "path")) == "/player/hero/hp/cur"
    )
    assert _member(hp_effect, "before") == 1
    assert _member(hp_effect, "after") == 0
    assert _member(hp_effect, "delta") == -1


def test_idempotent_noop_is_an_explicit_transition_without_visible_effects():
    no_op_pre = _base_state()
    no_op_pre["entities"]["guard"] = {
        "kind": "character",
        "name": "Guard",
        "aliases": [],
        "location_id": None,
        "present": True,
    }
    no_op = {
        "op": "presence",
        "entity": "guard",
        "present": True,
        "_turn": TURN,
    }
    no_op_post = _reduced(no_op_pre, no_op)
    assert no_op_post == no_op_pre
    no_op_transition = _transitions(_project(no_op_pre, no_op_post, no_op))[0]
    assert _member(no_op_transition, "status") == "no_effect"
    assert _covered_paths(no_op_transition) == set()
    assert _effects(no_op_transition) == []


@pytest.mark.parametrize("mismatch", ["pre_hash", "post_hash", "post_state"])
def test_projector_rejects_any_pre_post_or_replay_mismatch(mismatch: str):
    target = _target()
    pre = _base_state()
    op = {"op": "scene_mode", "mode": "dream", "_turn": TURN}
    post = _reduced(pre, op)
    passed_pre_hash = content_fingerprint(pre)
    passed_post_hash = content_fingerprint(post)
    passed_post = deepcopy(post)
    if mismatch == "pre_hash":
        passed_pre_hash = content_fingerprint({"forged": "pre"})
    elif mismatch == "post_hash":
        passed_post_hash = content_fingerprint({"forged": "post"})
    else:
        passed_post.setdefault("world", {})["forged"] = True
        passed_post_hash = content_fingerprint(passed_post)

    with pytest.raises(target.SemanticTransitionTruthError):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=passed_post,
            journal_rows=_journal(op),
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=passed_pre_hash,
            post_ledger_hash=passed_post_hash,
            realization=None,
            delivery_phase="pre_display",
            typed_gate_receipt=None,
        )


def test_duplicate_journal_identity_is_rejected_before_projection():
    target = _target()
    pre = _base_state()
    first = {"op": "scene_mode", "mode": "dream", "_turn": TURN}
    second = {"op": "scene_dial", "dial": "tension", "set": 20, "_turn": TURN}
    post = _reduced(pre, first, second)
    duplicate_rows = _journal(first, row_id=404) + _journal(second, row_id=404)

    with pytest.raises(target.SemanticTransitionTruthError):
        _project(pre, post, journal_rows=duplicate_rows)


def test_uncovered_changed_path_fails_closed_instead_of_becoming_narrator_authority():
    target = _target()
    pre = _base_state()
    op = {"op": "scene_mode", "mode": "dream", "_turn": TURN}
    post = _reduced(pre, op)
    post["world"]["guard_dead"] = True

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(uncovered|changed path|replay|post-state)",
    ):
        _project(pre, post, op)


def _valid_skill_settlement_window() -> tuple[dict, dict, list[dict]]:
    """Use the real authority path only to obtain a valid immutable journal fixture."""
    from random import Random

    from aetherstate import tier0

    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="transition-settlement-once")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14},
                    "skills": {"stealth": 3},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    pre = deepcopy(current_state(store, branch_id))
    result = tier0.run(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "I slip unseen past the watch. ((aether.check stealth vs 9))",
                }
            ]
        },
        "new_turn",
        False,
        pre,
        cfg,
        Random(3),
        turn=1,
    )
    applied = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        result.rule_ops,
        "rule",
        cfg,
    )
    assert not applied.quarantined
    rows = deepcopy(store.diagnostic_turn(branch_id, 1)["journal"])
    return pre, deepcopy(applied.state), rows


def test_atomic_settlement_projects_once_and_member_rows_cannot_double_claim():
    pre, post, rows = _valid_skill_settlement_window()
    target = _target()
    projection = target.project_journal_transitions(
        pre_state=pre,
        post_state=post,
        journal_rows=rows,
        branch_id=BRANCH,
        turn_index=1,
        pre_ledger_hash=content_fingerprint(pre),
        post_ledger_hash=content_fingerprint(post),
        realization=None,
        delivery_phase="pre_display",
        typed_gate_receipt=None,
    )
    transitions = _transitions(projection)
    wrappers = [
        row for row in transitions
        if _member(row, "op_kind") == "mechanic_settlement_commit"
    ]
    member_identities = {
        (row["id"], op_index)
        for row in rows
        for op_index, op in enumerate(row["ops"])
        if isinstance(op.get("_settlement_ref"), str)
    }
    members = [
        row for row in transitions
        if (_member(row, "journal_id"), _member(row, "op_index"))
        in member_identities
    ]
    assert len(wrappers) == 1
    assert members
    assert _effects(wrappers[0])
    assert all(_effects(member) == [] for member in members)
    effect_paths = [
        _canonical_pointer(_member(effect, "path"))
        for transition in transitions
        for effect in _effects(transition)
    ]
    assert len(effect_paths) == len(set(effect_paths))


def _post_delivery_fixture():
    from aetherstate.turn_lifecycle import build_delivery_proof, fingerprint

    target = _target()
    pre = _base_state()
    ledger_hash = content_fingerprint(pre)
    raw = b'{"text":"The gate is open."}'
    graph = {"claims": []}
    proof = build_delivery_proof(
        wire_bytes=raw,
        content_type="application/json",
        renderer_bytes=raw,
        visible_bytes=b"The gate is open.",
        expected_graph=graph,
        observed_graph=graph,
        ledger_graph=graph,
        ledger_root_hash=ledger_hash,
        logical_message_identity=fingerprint({"message": "transition-index"}),
    )
    reveal = {
        "op": "reveal_fact",
        "learner": "hero",
        "statement": "The gate is open.",
        "source": "witnessed",
        "_turn": TURN,
    }
    memory = {
        "op": "memory_event",
        "text": "The gate was seen open.",
        "_turn": TURN,
    }
    return target, pre, ledger_hash, proof["gate_receipt"], reveal, memory


def test_post_delivery_policy_refuses_every_canonical_extraction_mutation():
    target = _target()
    policies = target.TRANSITION_POLICIES
    assert len(EXTRACTION_OPS_RPG) == 32
    assert set(EXTRACTION_OPS_RPG) <= EXPECTED_REDUCER_OPS
    assert frozenset(target.INDEX_ONLY_POST_DELIVERY_OPS) == (
        EXPECTED_INDEX_ONLY_POST_DELIVERY_OPS
    )
    assert frozenset(target.DEFERRED_POST_DELIVERY_INDEX_OPS) == (
        DEFERRED_POST_DELIVERY_INDEX_OPS
    )

    for kind in EXTRACTION_OPS_RPG:
        assert _member(policies[kind], "post_delivery") == "refuse"


def test_post_delivery_index_refuses_even_a_self_consistent_inner_receipt():
    target, pre, ledger_hash, receipt, reveal, memory = _post_delivery_fixture()
    index_rows = _journal(reveal, memory, source="extraction")
    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(post.delivery indexing is disabled|persisted terminal proof)",
    ):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=pre,
            journal_rows=index_rows,
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=ledger_hash,
            post_ledger_hash=ledger_hash,
            realization=None,
            delivery_phase="post_delivery_index",
            typed_gate_receipt=receipt,
        )

    forged_receipt = deepcopy(receipt)
    forged_receipt["receipt_fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(
        target.SemanticTransitionTruthError,
    ):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=pre,
            journal_rows=index_rows,
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=ledger_hash,
            post_ledger_hash=ledger_hash,
            realization=None,
            delivery_phase="post_delivery_index",
            typed_gate_receipt=forged_receipt,
        )


def test_post_delivery_index_refuses_ledger_mutation_and_non_index_ops():
    target, pre, ledger_hash, receipt, reveal, _memory = _post_delivery_fixture()
    mechanically_changed = _reduced(pre, reveal)
    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(post.delivery|unchanged ledger|index.only)",
    ):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=mechanically_changed,
            journal_rows=_journal(reveal, source="extraction"),
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=ledger_hash,
            post_ledger_hash=content_fingerprint(mechanically_changed),
            realization=None,
            delivery_phase="post_delivery_index",
            typed_gate_receipt=receipt,
        )

    hp = {"op": "hp_adj", "char": "hero", "delta": -1, "_turn": TURN}
    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(post.delivery|fact or memory|index.only)",
    ):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=pre,
            journal_rows=_journal(hp, source="extraction"),
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=ledger_hash,
            post_ledger_hash=ledger_hash,
            realization=None,
            delivery_phase="post_delivery_index",
            typed_gate_receipt=receipt,
        )


def test_visible_turn_cannot_hide_world_mutation_behind_genesis_source():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "guard_dead", "value": True, "_turn": TURN}
    post = _reduced(pre, op)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(bootstrap.*T0|exact T0)",
    ):
        target.project_journal_transitions(
            pre_state=pre,
            post_state=post,
            journal_rows=_journal(op, source="genesis"),
            branch_id=BRANCH,
            turn_index=TURN,
            pre_ledger_hash=content_fingerprint(pre),
            post_ledger_hash=content_fingerprint(post),
        )

def _reseal_detached_entry(entry: dict) -> dict:
    """Recompute every self-hash after an adversarial detached-entry rewrite."""
    value = deepcopy(entry)
    value.pop("construction_ref", None)
    value.pop("entry_ref", None)
    value["cause_ref"] = None
    value["entry_ref"] = "transition." + content_fingerprint(value)[7:39]
    value["cause_ref"] = value["entry_ref"]
    value["construction_ref"] = content_fingerprint(value)
    return value


def _reseal_detached_projection(projection: dict) -> dict:
    value = deepcopy(projection)
    value.pop("fingerprint", None)
    value["fingerprint"] = content_fingerprint(value)
    return value


def _required_facts_for_entry(entry: dict) -> list[dict]:
    grouped = {}
    for fact in entry["facts"]:
        grouped.setdefault(fact["subject_id"], []).append(
            {key: fact[key] for key in ("kind", "detail", "amount")}
        )
    required = []
    for subject_id, effects in sorted(grouped.items()):
        basis = {
            "cause_ref": entry["cause_ref"],
            "construction_ref": entry["construction_ref"],
            "subject_id": subject_id,
            "effects": effects,
        }
        required.append(
            {
                "fact_ref": "transition_fact." + content_fingerprint(basis)[7:39],
                **basis,
            }
        )
    return required


def _reseal_projection_entries(projection: dict, entries: list[dict]) -> dict:
    value = deepcopy(projection)
    value["entries"] = deepcopy(entries)
    value["transitions"] = deepcopy(entries)
    value["required_facts"] = [
        fact
        for entry in entries
        if entry["visibility"] == "required"
        for fact in _required_facts_for_entry(entry)
    ]
    value["metadata_receipts"] = [
        deepcopy(entry)
        for entry in entries
        if entry["visibility"] in {"internal", "bootstrap", "no_effect"}
    ]
    return _reseal_detached_projection(value)


def _operation_binding_case(name: str) -> tuple[dict, dict, dict]:
    """Return a valid operation and a same-family forged operation over one exact pre-state."""
    pre = _base_state()
    if name == "set_attribute":
        original = {
            "op": "set_attribute", "entity": "guard", "key": "rank",
            "value": "captain", "_turn": TURN,
        }
        forged = {**original, "key": "oath", "value": "broken"}
    elif name == "move_entity":
        pre["entities"]["guard"] = {
            "kind": "character", "name": "Guard", "aliases": [],
            "location_id": "gate", "present": True,
        }
        original = {
            "op": "move_entity", "entity": "guard", "to_location": "hall",
            "_turn": TURN,
        }
        forged = {**original, "to_location": "yard"}
    elif name == "presence":
        for entity_id in ("guard", "mage"):
            pre["entities"][entity_id] = {
                "kind": "character", "name": entity_id.title(), "aliases": [],
                "location_id": "gate", "present": False,
            }
        original = {
            "op": "presence", "entity": "guard", "present": True,
            "_turn": TURN,
        }
        forged = {**original, "entity": "mage"}
    elif name == "resource_change":
        pre["player"]["hero"] = {
            "resources": {"mana": {"cur": 2, "max": 5}},
        }
        original = {
            "op": "resource_change", "char": "hero", "resource": "mana",
            "action": "gain", "amount": 2, "_turn": TURN,
        }
        forged = {**original, "amount": 3}
    elif name == "relationship_adj":
        original = {
            "op": "relationship_adj", "from_char": "hero", "to_char": "guard",
            "dimension": "trust", "delta": 3, "_turn": TURN,
        }
        forged = {**original, "dimension": "fear"}
    elif name == "effect_add":
        original = {
            "op": "effect_add", "char": "hero", "effect": "Poisoned",
            "_turn": TURN,
        }
        forged = {**original, "effect": "Burning"}
    elif name == "battle_start":
        original = {"op": "battle_start", "name": "Siege", "_turn": TURN}
        forged = {**original, "name": "Ambush"}
    elif name == "front_add":
        original = {
            "op": "front_add", "name": "Doom", "segments": 4,
            "consequence": "The gate falls", "_fid": "doom", "_turn": TURN,
        }
        forged = {**original, "name": "Plague", "consequence": "The city sickens"}
    elif name == "front_tick":
        pre.setdefault("fronts", {})["doom"] = {
            "name": "Doom", "segments": 4, "filled": 1, "pace": 1,
            "consequence": "The gate falls", "revealed": False, "done": False,
            "created_turn": 1, "log": [],
        }
        original = {
            "op": "front_tick", "front": "doom", "_delta": 1,
            "reason": "The seal cracks", "_turn": TURN,
        }
        forged = {**original, "_delta": 2, "reason": "The seal shatters"}
    elif name == "front_reveal":
        pre.setdefault("fronts", {})
        for front_id in ("doom", "plague"):
            pre["fronts"][front_id] = {
                "name": front_id.title(), "segments": 4, "filled": 1, "pace": 1,
                "consequence": "A consequence", "revealed": False, "done": False,
                "created_turn": 1, "log": [],
            }
        original = {"op": "front_reveal", "front": "doom", "_turn": TURN}
        forged = {**original, "front": "plague"}
    else:  # pragma: no cover - the parametrization below is the exhaustive local inventory
        raise AssertionError(name)
    return pre, original, forged


def _forge_operation_while_retaining_old_effects(
    target: Any,
    valid: dict,
    forged_operation: dict,
) -> dict:
    entry = deepcopy(valid["entries"][0])
    entry["op_kind"] = forged_operation["op"]
    entry["operation"] = deepcopy(forged_operation)
    entry["op_fingerprint"] = content_fingerprint(forged_operation)
    opposition = forged_operation.get("_opposition")
    if isinstance(opposition, Mapping) and isinstance(opposition.get("actor"), str):
        entry["actor_id"] = opposition["actor"]
    elif isinstance(forged_operation.get("from_char"), str):
        entry["actor_id"] = forged_operation["from_char"]
    elif isinstance(forged_operation.get("char"), str):
        entry["actor_id"] = forged_operation["char"]
    else:
        entry["actor_id"] = None
    entry["facts"] = target._facts_for_operation_effects(  # noqa: SLF001 - adversarial oracle
        forged_operation,
        entry["effects"],
    )
    entry["subjects"] = sorted(
        {fact["subject_id"] for fact in entry["facts"]}
        or ({target._subject_from_op(forged_operation)} if entry["changed"] else set())
    )
    entry = _reseal_detached_entry(entry)
    forged = deepcopy(valid)
    forged["entries"] = [entry]
    forged["transitions"] = [deepcopy(entry)]
    forged["required_facts"] = _required_facts_for_entry(entry)
    forged["metadata_receipts"] = []
    return _reseal_detached_projection(forged)


def test_projection_v4_persists_exact_rows_and_a_hash_rooted_reducer_replay_witness():
    target = _target()
    pre = _base_state()
    op = {"op": "set_attribute", "entity": "guard", "key": "rank", "value": "captain", "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)

    assert valid["schema"] == "semantic-journal-transition-projection/4"
    assert valid["journal_rows"] == _journal(op)
    assert valid["pre_state"] == pre
    assert content_fingerprint(valid["pre_state"]) == valid["pre_ledger_hash"]
    assert valid["reducer_schema"] == target.REDUCER_REPLAY_SCHEMA
    assert valid["reducer_fingerprint"] == target.REDUCER_REPLAY_FINGERPRINT
    assert target.validate_transition_projection(valid) == valid


def test_detached_validation_rejects_a_resealed_arbitrary_journal_window_fingerprint():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    forged = _project(pre, _reduced(pre, op), op)
    forged["journal_window_fingerprint"] = "sha256:" + "0" * 64
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)journal window fingerprint.*exact rows",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("journal_id", "position.*exact journal rows"),
        ("op_index", "position.*exact journal rows"),
        ("source", "source.*exact journal rows"),
        ("actor", "actor.*exact operation"),
        ("cause", "cause.*exact operation"),
        ("opposition_actor", "operation.*exact journal rows"),
    ],
)
def test_detached_validation_binds_entry_identity_authority_and_operation_to_exact_rows(
    mutation,
    message,
):
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    entry = deepcopy(valid["entries"][0])
    if mutation == "journal_id":
        entry["journal_id"] = 999
    elif mutation == "op_index":
        entry["op_index"] = 7
    elif mutation == "source":
        entry["source"] = "user"
    elif mutation == "actor":
        entry["actor_id"] = "enemy.forged"
    elif mutation == "opposition_actor":
        entry["operation"]["_opposition"] = {
            "actor": "enemy.forged",
            "target": "hero",
            "move": "strike",
        }
        entry["op_fingerprint"] = content_fingerprint(entry["operation"])
        entry["actor_id"] = "enemy.forged"
    else:
        entry.pop("construction_ref")
        entry.pop("entry_ref")
        entry["cause_ref"] = "frame.forged"
        entry["entry_ref"] = "transition." + content_fingerprint(entry)[7:39]
        entry["construction_ref"] = content_fingerprint(entry)
        forged = _reseal_projection_entries(valid, [entry])
        with pytest.raises(
            target.SemanticTransitionTruthError,
            match=f"(?i){message}",
        ):
            target.validate_transition_projection(forged)
        return
    entry = _reseal_detached_entry(entry)
    forged = _reseal_projection_entries(valid, [entry])

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match=f"(?i){message}",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_ignored_operation_metadata_that_changes_facts():
    target = _target()
    pre = _base_state()
    actual_op = {
        "op": "clothing",
        "char": "hero",
        "action": "don",
        "item": "cloak",
        "_turn": TURN,
    }
    forged_op = {**actual_op, "target": "guard"}
    post = _reduced(pre, actual_op)
    assert _reduced(pre, forged_op) == post
    actual = _project(pre, post, actual_op)
    forged = _project(pre, post, forged_op)
    assert actual["required_facts"] != forged["required_facts"]
    forged["journal_rows"] = deepcopy(actual["journal_rows"])
    forged["journal_window_fingerprint"] = actual["journal_window_fingerprint"]
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)operation.*exact journal rows",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_same_root_set_clear_operation_substitution():
    target = _target()
    pre = _base_state()
    actual_ops = (
        {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN},
        {"op": "world_flag", "key": "vault_open", "value": None, "_turn": TURN},
    )
    forged_ops = (
        {"op": "world_flag", "key": "guard_dead", "value": True, "_turn": TURN},
        {"op": "world_flag", "key": "guard_dead", "value": None, "_turn": TURN},
    )
    post = _reduced(pre, *actual_ops)
    assert _reduced(pre, *forged_ops) == post
    actual = _project(pre, post, *actual_ops)
    forged = _project(pre, post, *forged_ops)
    assert actual["required_facts"] != forged["required_facts"]
    forged["journal_rows"] = deepcopy(actual["journal_rows"])
    forged["journal_window_fingerprint"] = actual["journal_window_fingerprint"]
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)operation.*exact journal rows",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize(
    ("journal_value", "forged_value"),
    [(True, 1), (False, 0), (1, 1.0)],
    ids=["true-versus-one", "false-versus-zero", "integer-versus-float"],
)
def test_detached_validation_rejects_python_equal_typed_set_clear_substitution(
    journal_value, forged_value
):
    target = _target()
    pre = _base_state()
    actual_ops = (
        {
            "op": "world_flag",
            "key": "vault_open",
            "value": journal_value,
            "_turn": TURN,
        },
        {"op": "world_flag", "key": "vault_open", "value": None, "_turn": TURN},
    )
    forged_ops = (
        {
            "op": "world_flag",
            "key": "vault_open",
            "value": forged_value,
            "_turn": TURN,
        },
        {"op": "world_flag", "key": "vault_open", "value": None, "_turn": TURN},
    )
    assert actual_ops[0] == forged_ops[0]
    assert content_fingerprint(actual_ops[0]) != content_fingerprint(forged_ops[0])
    assert content_fingerprint(_reduced(pre, *actual_ops)) == content_fingerprint(
        _reduced(pre, *forged_ops)
    )

    actual = _project(pre, _reduced(pre, *actual_ops), *actual_ops)
    forged_source = _project(pre, _reduced(pre, *forged_ops), *forged_ops)
    assert target.validate_transition_projection(actual) == actual
    assert target.validate_transition_projection(forged_source) == forged_source
    assert actual["entries"][0]["op_fingerprint"] \
        != forged_source["entries"][0]["op_fingerprint"]

    forged = deepcopy(forged_source)
    forged["journal_rows"] = deepcopy(actual["journal_rows"])
    forged["journal_window_fingerprint"] = actual["journal_window_fingerprint"]
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)operation.*exact journal rows",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize(
    ("turn_index", "source", "alias"),
    [
        (1, "rule", True),
        (1, "rule", 1.0),
        (0, "bootstrap", False),
        (0, "bootstrap", 0.0),
    ],
    ids=[
        "t1-integer-versus-true",
        "t1-integer-versus-float",
        "t0-integer-versus-false",
        "t0-integer-versus-float",
    ],
)
def test_detached_validation_rejects_fully_resealed_entry_turn_type_alias(
    turn_index,
    source,
    alias,
):
    target = _target()
    pre = empty_state()
    pre["meta"]["turn"] = turn_index
    if turn_index == 0:
        pre["entities"]["guard"] = {
            "kind": "character",
            "name": "Guard",
            "aliases": [],
            "location_id": "gate",
            "present": False,
        }
        op = {
            "op": "presence",
            "entity": "guard",
            "present": True,
            "_turn": turn_index,
        }
    else:
        op = {
            "op": "world_flag",
            "key": "vault_open",
            "value": True,
            "_turn": turn_index,
        }
    post = _reduced(pre, op)
    rows = [
        {
            "id": 101,
            "turn_lo": turn_index,
            "turn_hi": turn_index,
            "source": source,
            "ops": [deepcopy(op)],
        }
    ]
    valid = target.project_journal_transitions(
        pre_state=pre,
        post_state=post,
        journal_rows=rows,
        branch_id=BRANCH,
        turn_index=turn_index,
        pre_ledger_hash=content_fingerprint(pre),
        post_ledger_hash=content_fingerprint(post),
    )
    assert target.validate_transition_projection(valid) == valid
    assert alias == turn_index
    assert type(alias) is not type(turn_index)

    entry = deepcopy(valid["entries"][0])
    entry["turn_index"] = alias
    entry = _reseal_detached_entry(entry)
    forged = _reseal_projection_entries(valid, [entry])

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)entry turn_index must be an integer",
    ):
        target.validate_transition_projection(forged)


def test_type_only_reducer_change_is_not_misclassified_as_a_noop():
    target = _target()
    pre = _base_state()
    pre["world"]["vault_open"] = True
    op = {"op": "world_flag", "key": "vault_open", "value": 1, "_turn": TURN}
    post = _reduced(pre, op)
    assert pre == post
    assert content_fingerprint(pre) != content_fingerprint(post)

    projection = _project(pre, post, op)
    entry = projection["entries"][0]
    assert entry["changed"] is True
    assert entry["status"] == "changed"
    assert entry["covered_paths"] == ["/world/vault_open"]
    assert entry["effects"][0]["before"] is True
    assert entry["effects"][0]["after"] == 1
    assert not isinstance(entry["effects"][0]["after"], bool)
    assert target.validate_transition_projection(projection) == projection


@pytest.mark.parametrize(
    "duplicate_view",
    ["transitions", "leaf_effects", "required_facts", "metadata_receipts"],
)
def test_detached_validation_rejects_python_equal_typed_duplicate_views(duplicate_view):
    target = _target()
    if duplicate_view == "metadata_receipts":
        pre = _base_state()
        pre["entities"]["guard"] = {
            "kind": "character",
            "name": "Guard",
            "aliases": [],
            "location_id": "gate",
            "present": True,
        }
        op = {"op": "presence", "entity": "guard", "present": True, "_turn": TURN}
    elif duplicate_view == "required_facts":
        pre = _base_state()
        pre["player"]["hero"] = {
            "resources": {"mana": {"cur": 1, "max": 4}},
        }
        op = {
            "op": "resource_change",
            "char": "hero",
            "resource": "mana",
            "action": "gain",
            "amount": 1,
            "_turn": TURN,
        }
    else:
        pre = _base_state()
        op = {
            "op": "set_attribute",
            "entity": "guard",
            "key": "warded",
            "value": True,
            "_turn": TURN,
        }
    valid = _project(pre, _reduced(pre, op), op)
    forged = deepcopy(valid)

    if duplicate_view == "transitions":
        forged["transitions"][0]["operation"]["value"] = 1
    elif duplicate_view == "leaf_effects":
        entry = deepcopy(forged["entries"][0])
        effect = next(row for row in entry["effects"] if row["after"] is True)
        effect["after"] = 1
        entry = _reseal_detached_entry(entry)
        forged = _reseal_projection_entries(forged, [entry])
    elif duplicate_view == "required_facts":
        amount = next(
            effect
            for fact in forged["required_facts"]
            for effect in fact["effects"]
            if effect["amount"] == 1
        )
        amount["amount"] = 1.0
    else:
        forged["metadata_receipts"][0]["operation"]["present"] = 1
    forged = _reseal_detached_projection(forged)

    with pytest.raises(target.SemanticTransitionTruthError):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_reordered_commuting_operations():
    target = _target()
    pre = _base_state()
    first = {"op": "world_flag", "key": "alpha", "value": True, "_turn": TURN}
    second = {"op": "world_flag", "key": "beta", "value": True, "_turn": TURN}
    post = _reduced(pre, first, second)
    assert _reduced(pre, second, first) == post
    actual = _project(pre, post, first, second)
    forged = _project(pre, post, second, first)
    forged["journal_rows"] = deepcopy(actual["journal_rows"])
    forged["journal_window_fingerprint"] = actual["journal_window_fingerprint"]
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)operation.*exact journal rows",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("pre_state", "pre-state.*pre-ledger hash"),
        ("reducer_schema", "reducer replay version"),
        ("reducer_fingerprint", "reducer replay version"),
        ("post_hash", "does not reach the post-ledger hash"),
    ],
)
def test_detached_replay_rejects_a_resealed_root_or_reducer_version_swap(mutation, message):
    target = _target()
    pre = _base_state()
    op = {"op": "set_attribute", "entity": "guard", "key": "rank", "value": "captain", "_turn": TURN}
    forged = _project(pre, _reduced(pre, op), op)
    if mutation == "pre_state":
        forged["pre_state"]["world"]["forged"] = True
    elif mutation == "reducer_schema":
        forged["reducer_schema"] = "aetherstate-state-reducer-replay/999"
    elif mutation == "reducer_fingerprint":
        forged["reducer_fingerprint"] = "sha256:" + "f" * 64
    else:
        forged["post_ledger_hash"] = content_fingerprint({"forged": "post"})
    forged = _reseal_detached_projection(forged)

    with pytest.raises(target.SemanticTransitionTruthError, match=f"(?i){message}"):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize(
    "case",
    [
        "set_attribute",
        "move_entity",
        "presence",
        "resource_change",
        "relationship_adj",
        "effect_add",
        "battle_start",
        "front_add",
        "front_tick",
        "front_reveal",
    ],
)
def test_detached_replay_rejects_fully_resealed_same_family_operation_effect_swaps(case):
    target = _target()
    pre, original, forged_operation = _operation_binding_case(case)
    valid = _project(pre, _reduced(pre, original), original)
    forged = _forge_operation_while_retaining_old_effects(
        target,
        valid,
        forged_operation,
    )

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(exact rooted reducer replay|exact journal rows)",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize("case", ["clamp", "no_op", "structural"])
def test_rooted_detached_replay_accepts_clamps_no_ops_and_structural_side_effects(case):
    target = _target()
    pre = _base_state()
    if case == "clamp":
        pre["player"]["hero"] = {"hp": {"cur": 1, "max": 10}}
        op = {"op": "hp_adj", "char": "hero", "delta": -999, "_turn": TURN}
    elif case == "no_op":
        pre["entities"]["guard"] = {
            "kind": "character", "name": "Guard", "aliases": [],
            "location_id": "gate", "present": True,
        }
        op = {"op": "presence", "entity": "guard", "present": True, "_turn": TURN}
    else:
        op = {
            "op": "front_add", "name": "Doom", "segments": 4,
            "consequence": "The gate falls", "_fid": "doom", "_turn": TURN,
        }
    projection = _project(pre, _reduced(pre, op), op)
    assert target.validate_transition_projection(projection) == projection


def test_detached_validation_rederives_visibility_from_exact_operation_policy():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["visibility"] = "explicit_adapter"
    forged_entry["facts"] = []
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = []
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(visibility.*derivable|operation.*policy)",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_a_resealed_operation_or_cause_swap():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["operation"]["key"] = "guard_dead"
    forged_entry["op_fingerprint"] = content_fingerprint(forged_entry["operation"])
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = deepcopy(valid["required_facts"])
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(target.SemanticTransitionTruthError):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize("faction", [None, "red/court"])
@pytest.mark.parametrize("value", [True, None])
def test_world_flag_projection_binds_slugged_set_clear_scope_and_turn(faction, value):
    pre = _base_state()
    pre["meta"]["turn"] = TURN - 1
    if faction is not None:
        pre["entities"][faction] = {"name": "Red Court"}
        if value is None:
            pre["factions"][faction] = {
                "name": "Red Court",
                "circumstances": {"vault_open": True},
            }
    elif value is None:
        pre["world"]["vault_open"] = True
    op = {
        "op": "world_flag",
        "key": "Vault / OPEN!",
        "value": value,
        "_turn": TURN,
        **({"faction": faction} if faction is not None else {}),
    }

    projection = _project(pre, _reduced(pre, op), op)
    effects = {effect["path"]: effect for effect in projection["entries"][0]["effects"]}
    target_path = (
        "/factions/red~1court/circumstances/vault_open"
        if faction is not None
        else "/world/vault_open"
    )
    assert effects[target_path]["after"] == (
        {"schema": "semantic-missing-value/1"} if value is None else value
    )
    assert effects["/meta/turn"] == {
        "path": "/meta/turn",
        "before": TURN - 1,
        "after": TURN,
        "delta": 1,
        "visibility": "required",
    }


def test_detached_validation_rejects_fully_resealed_world_flag_key_effect_mismatch():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["operation"]["key"] = "guard_dead"
    forged_entry["op_fingerprint"] = content_fingerprint(forged_entry["operation"])
    forged_entry["facts"] = [
        {
            **fact,
            "detail": "flag guard dead true",
        }
        for fact in forged_entry["facts"]
    ]
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = _required_facts_for_entry(forged_entry)
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(world.flag.*exact.*effect|exact rooted reducer replay|exact journal rows)",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize("claim_kind", ["defeat", "status", "harm", "world"])
def test_detached_validation_rejects_resealed_extra_transition_fact(claim_kind):
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["facts"].append(
        {
            "subject_id": "guard",
            "kind": claim_kind,
            "detail": "guard falsely defeated",
            "amount": None,
        }
    )
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = _required_facts_for_entry(forged_entry)
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)facts.*complete exact ordered.*operation and effects",
    ):
        target.validate_transition_projection(forged)


@pytest.mark.parametrize("mutation", ["omit", "duplicate"])
def test_detached_validation_rejects_resealed_missing_or_duplicate_fact(mutation):
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "vault_open", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    if mutation == "omit":
        forged_entry["facts"] = []
    else:
        forged_entry["facts"].append(deepcopy(forged_entry["facts"][0]))
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = _required_facts_for_entry(forged_entry)
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)facts.*complete exact ordered.*operation and effects",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_resealed_noncanonical_fact_order():
    target = _target()
    pre = _base_state()
    pre["player"]["hero"] = {
        "level": 2,
        "hp": {"cur": 5, "max": 10},
        "resources": {"mana": {"cur": 1, "max": 4}},
        "stat_points": 0,
    }
    op = {
        "op": "level_up",
        "char": "hero",
        "_grants": {"hp": 4, "pool": 2, "stat_points": 1},
        "_turn": TURN,
    }
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    assert len(forged_entry["facts"]) > 1
    forged_entry["facts"] = list(reversed(forged_entry["facts"]))
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = _required_facts_for_entry(forged_entry)
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)facts.*complete exact ordered.*operation and effects",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_resealed_visible_source_laundering():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "guard_dead", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["source"] = "genesis"
    forged_entry["visibility"] = "bootstrap"
    forged_entry["facts"] = []
    for effect in forged_entry["effects"]:
        effect["visibility"] = "bootstrap"
    forged_entry = _reseal_detached_entry(forged_entry)
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = []
    forged["metadata_receipts"] = [deepcopy(forged_entry)]
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(bootstrap.*T0|explicit T0|exact journal rows)",
    ):
        target.validate_transition_projection(forged)


def test_detached_validation_rejects_resealed_operation_path_policy_swap():
    target = _target()
    pre = _base_state()
    op = {"op": "world_flag", "key": "guard_dead", "value": True, "_turn": TURN}
    valid = _project(pre, _reduced(pre, op), op)
    forged_entry = deepcopy(valid["entries"][0])
    forged_entry["op_kind"] = "time_advance"
    forged_entry["operation"] = {"op": "time_advance", "minutes": 5, "_turn": TURN}
    forged_entry["op_fingerprint"] = content_fingerprint(forged_entry["operation"])
    forged_entry = _reseal_detached_entry(forged_entry)
    grouped = {}
    for fact in forged_entry["facts"]:
        grouped.setdefault(fact["subject_id"], []).append(
            {key: fact[key] for key in ("kind", "detail", "amount")}
        )
    required = []
    for subject_id, effects in sorted(grouped.items()):
        basis = {
            "cause_ref": forged_entry["cause_ref"],
            "construction_ref": forged_entry["construction_ref"],
            "subject_id": subject_id,
            "effects": effects,
        }
        required.append(
            {
                "fact_ref": "transition_fact." + content_fingerprint(basis)[7:39],
                **basis,
            }
        )
    forged = deepcopy(valid)
    forged["entries"] = [forged_entry]
    forged["transitions"] = [deepcopy(forged_entry)]
    forged["required_facts"] = required
    forged["metadata_receipts"] = []
    forged = _reseal_detached_projection(forged)

    with pytest.raises(
        target.SemanticTransitionTruthError,
        match="(?i)(uncovered state paths|exact rooted reducer replay|exact journal rows)",
    ):
        target.validate_transition_projection(forged)
