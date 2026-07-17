"""Tier-0 operation grouping for the atomic ``weapon_attack/1`` boundary."""
from __future__ import annotations

from copy import deepcopy
import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.mechanic_settlement import (
    WEAPON_ATTACK_CONTRACT,
    weapon_attack_settlement_ref,
)
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import build_meaning_binding, semantic_match_ref
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.state import empty_state
from aetherstate.tier0 import Tier0Result, _group_weapon_attack_settlements


def _candidate(
    frame_id: str = "f1",
    *,
    target_id: str | None = "iven",
    target_name: str = "Iven",
) -> tuple[dict, dict]:
    source = f"I use Meridian Pierce to strike {target_name}."
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
    binding = build_meaning_binding(
        meaning,
        binding_id=f"binding.{frame_id}",
        event_node_id=f"event.{frame_id}",
        event_span=(0, len(source)),
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(attack)],
        }],
        role_evidence=[{
            "role": "action",
            "evidence_refs": [semantic_match_ref(attack)],
        }],
    )
    frame = ActionFrame(
        frame_id=frame_id,
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="player",
        capability_id="meridian_pierce",
        action_class="weapon_attack",
        target_entity_id=target_id,
        target_name=target_name,
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id=f"event.{frame_id}",
        mechanic_disposition="candidate",
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")
    return frame.snapshot(source), binding


def _check(frame: dict, tier: str = "success") -> dict:
    return {
        "op": "check",
        "skill": "meridian_pierce",
        "char": "player",
        "result": 11,
        "tier": tier,
        "_semantic_frame_ref": frame["fingerprint"],
    }


def _strike(frame: dict, delta: int = -4, *, target: str | None = None) -> dict:
    return {
        "op": "combatant_hp",
        "target": target or str(frame["target_entity_id"]),
        "delta": delta,
        "reason": "Meridian Pierce",
        "_strike": True,
        "_semantic_frame_ref": frame["fingerprint"],
    }


def _group(ops: list[dict], *candidates: tuple[dict, dict]) -> Tier0Result:
    res = Tier0Result(rule_ops=deepcopy(ops))
    _group_weapon_attack_settlements(
        res,
        [frame for frame, _binding in candidates],
        {frame["frame_id"]: binding for frame, binding in candidates},
    )
    return res


def _wrapper(result: Tier0Result) -> dict:
    wrappers = [op for op in result.rule_ops if op.get("op") == "mechanic_settlement_commit"]
    assert len(wrappers) == 1
    return wrappers[0]


def _assert_contiguous_projection(result: Tier0Result, wrapper: dict) -> list[dict]:
    index = result.rule_ops.index(wrapper)
    members = wrapper["members"]
    projections = result.rule_ops[index + 1:index + 1 + len(members)]
    assert len(projections) == len(members)
    for member_index, (member, projection) in enumerate(zip(members, projections, strict=True)):
        assert "_settlement_ref" not in member
        assert "_settlement_member_index" not in member
        assert projection == {
            **member,
            "_settlement_ref": wrapper["settlement_ref"],
            "_settlement_member_index": member_index,
        }
    return projections


def test_hit_becomes_one_fixed_contract_wrapper_plus_exact_top_level_projections():
    frame, binding = _candidate()
    semantic_commit = {"op": "semantic_frame_commit", "frame": frame}
    mastery = {
        "op": "master_tick",
        "char": "player",
        "skill": "meridian_pierce",
        "amount": 3,
        "_semantic_frame_ref": frame["fingerprint"],
    }
    result = _group(
        [semantic_commit, _check(frame), _strike(frame), mastery, {"op": "clock_tick"}],
        (frame, binding),
    )

    wrapper = _wrapper(result)
    assert wrapper["contract_id"] == WEAPON_ATTACK_CONTRACT
    assert wrapper["settlement_ref"] == weapon_attack_settlement_ref(frame, binding)
    assert wrapper["frame_ref"] == frame["fingerprint"]
    assert wrapper["_semantic_frame_ref"] == frame["fingerprint"]
    assert [member["op"] for member in wrapper["members"]] == [
        "check", "combatant_hp", "master_tick",
    ]
    _assert_contiguous_projection(result, wrapper)
    assert result.rule_ops[0] == semantic_commit
    assert result.rule_ops[-1] == {"op": "clock_tick"}


def test_delta_zero_miss_is_a_complete_weapon_settlement_not_a_missing_outcome():
    frame, binding = _candidate()
    result = _group([_check(frame, "fail"), _strike(frame, 0)], (frame, binding))

    wrapper = _wrapper(result)
    assert [member["op"] for member in wrapper["members"]] == ["check", "combatant_hp"]
    assert wrapper["members"][1]["delta"] == 0
    _assert_contiguous_projection(result, wrapper)


def test_opening_group_places_every_scene_grounded_band_member_in_the_settlement():
    frame, binding = _candidate()
    ref = frame["fingerprint"]
    primary = {
        "op": "combatant_spawn",
        "name": "Iven",
        "side": "enemy",
        "_cid": "iven",
        "_semantic_frame_ref": ref,
    }
    band_extra = {
        "op": "combatant_spawn",
        "name": "Mara",
        "side": "enemy",
        "_cid": "mara",
        "_floor": True,
        "_semantic_frame_ref": ref,
    }
    scene = {
        "op": "scene_set",
        "phase": "climax",
        "_floor": True,
        "_semantic_frame_ref": ref,
    }
    unrelated_same_ref = {
        "op": "effect_add",
        "char": "player",
        "effect": "Inspired",
        "kind": "status",
        "_semantic_frame_ref": ref,
    }
    result = _group(
        [primary, band_extra, scene, _check(frame), _strike(frame), unrelated_same_ref],
        (frame, binding),
    )

    wrapper = _wrapper(result)
    assert [member["op"] for member in wrapper["members"]] == [
        "combatant_spawn", "combatant_spawn", "scene_set", "check", "combatant_hp",
    ]
    assert [member["_cid"] for member in wrapper["members"][:2]] == ["iven", "mara"]
    assert unrelated_same_ref in result.rule_ops
    _assert_contiguous_projection(result, wrapper)


def test_incomplete_weapon_group_drops_deferred_band_instead_of_leaking_a_spawn():
    frame, binding = _candidate()
    ref = frame["fingerprint"]
    band_extra = {
        "op": "combatant_spawn",
        "name": "Mara",
        "side": "enemy",
        "_cid": "mara",
        "_floor": True,
        "_semantic_frame_ref": ref,
    }
    result = _group([band_extra, _check(frame)], (frame, binding))

    assert result.rule_ops == []
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


def test_unresolved_weapon_attack_drops_all_frame_bound_mutations():
    frame, binding = _candidate(target_id=None, target_name="Baser Hollow x6")
    check = _check(frame)
    check["_cost"] = {"mana": 2}
    check["_ability_cd"] = {"fire_focus": 5}
    strike = _strike(frame, target="baser_hollow_x6")
    mastery = {
        "op": "master_tick",
        "char": "player",
        "skill": "meridian_pierce",
        "amount": 3,
        "_semantic_frame_ref": frame["fingerprint"],
    }
    semantic_commit = {"op": "semantic_frame_commit", "frame": frame}
    result = _group(
        [semantic_commit, check, strike, mastery, {"op": "clock_tick"}],
        (frame, binding),
    )

    assert result.rule_ops == [semantic_commit, {"op": "clock_tick"}]
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


def test_multiple_frame_groups_are_disjoint_and_keep_source_order():
    first = _candidate("f1", target_id="iven", target_name="Iven")
    second = _candidate("f2", target_id="mara", target_name="Mara")
    first_frame, _ = first
    second_frame, _ = second
    result = _group(
        [
            _check(first_frame),
            _strike(first_frame),
            {"op": "clock_tick"},
            _check(second_frame),
            _strike(second_frame),
        ],
        first,
        second,
    )

    wrappers = [op for op in result.rule_ops if op.get("op") == "mechanic_settlement_commit"]
    assert [op["frame_ref"] for op in wrappers] == [
        first_frame["fingerprint"],
        second_frame["fingerprint"],
    ]
    assert result.rule_ops.index({"op": "clock_tick"}) \
        > result.rule_ops.index(wrappers[0]) \
        and result.rule_ops.index({"op": "clock_tick"}) < result.rule_ops.index(wrappers[1])
    for wrapper in wrappers:
        _assert_contiguous_projection(result, wrapper)


def test_duplicate_exact_check_cardinality_fails_closed_without_partial_grouping():
    frame, binding = _candidate()
    ops = [_check(frame), _check(frame), _strike(frame)]
    result = _group(ops, (frame, binding))

    assert result.rule_ops == []
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


T2_FIREBALL = "I focus my magic through my staff and unleash a ball of fire at the nearest enemy."
NON_FOE_ICE = "I focus my magic through my staff and blast pointed ice on the lake."


def _fireball_state(*foes: tuple[str, str]) -> dict:
    state = empty_state()
    state["entities"]["seraphine_vaulric"] = {
        "kind": "player",
        "name": "Seraphine Vaulric",
        "present": True,
        "aliases": [],
    }
    state["player"] = {
        "seraphine_vaulric": {
            "eid": "seraphine_vaulric",
            "stats": {"INT": 16},
            "skills": {"spellcraft": 5},
            "abilities": [],
            "resources": {"mana": {"name": "Mana", "cur": 30, "max": 30}},
            "_resource_cost_policy": "strict/1",
            "defs": {"skills": {"spellcraft": {
                "name": "Spellcraft",
                "keyed_stat": "INT",
                "governs": ["channel"],
                "cost": {"mana": 2},
            }}},
        },
    }
    state["scene"]["phase"] = "climax"
    state["combat"] = {
        "active": True,
        "combatants": {
            cid: {
                "id": cid,
                "eid": None,
                "name": name,
                "side": "enemy",
                "defeated": False,
                "hp": {"cur": 6, "max": 6},
            }
            for cid, name in foes
        },
        "started_turn": 1,
        "history": [],
    }
    return state


def _fireball_result(state: dict, source: str = T2_FIREBALL):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.intent_floor = True
    cfg.specialization.foe_floor = True
    cfg.specialization.war_room = True
    return tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        state,
        cfg,
        random.Random(7),
        turn=2,
    )


def _fireball_frame(result) -> dict:
    return next(
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
        and op["frame"].get("capability_id") == "spellcraft"
    )


def test_t2_relative_enemy_binds_the_sole_live_combatant_before_weapon_settlement():
    result = _fireball_result(_fireball_state(("baser_hollow_x6", "Baser Hollow x6")))
    frame = _fireball_frame(result)

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "baser_hollow_x6"
    assert frame["target_name"] == "Baser Hollow x6"
    target_evidence = [row for row in frame["evidence"] if row["kind"] == "target"]
    assert len(target_evidence) == 1
    assert T2_FIREBALL[target_evidence[0]["start"]:target_evidence[0]["end"]] \
        == "the nearest enemy"
    assert target_evidence[0]["value"] == "baser_hollow_x6"

    wrapper = next(
        op for op in result.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
        and op.get("contract_id") == WEAPON_ATTACK_CONTRACT
    )
    assert wrapper["frame_ref"] == frame["fingerprint"]
    assert [member["op"] for member in wrapper["members"]] == [
        "check", "combatant_hp", "master_tick",
    ]
    assert wrapper["members"][0]["_cost"] == {"mana": 2}
    assert wrapper["members"][1]["target"] == "baser_hollow_x6"
    assert wrapper["members"][1]["delta"] == -2


def test_t2_relative_enemy_abstains_with_two_live_foes_and_leaks_no_mechanics():
    result = _fireball_result(_fireball_state(
        ("baser_hollow_x6", "Baser Hollow x6"),
        ("ashen_hollow", "Ashen Hollow"),
    ))
    frame = _fireball_frame(result)

    assert frame["target_entity_id"] is None
    assert frame["target_name"] is None
    assert set(frame["ambiguity"]) >= {"baser_hollow_x6", "ashen_hollow"}
    target_evidence = [row for row in frame["evidence"] if row["kind"] == "target"]
    assert len(target_evidence) == 1
    assert T2_FIREBALL[target_evidence[0]["start"]:target_evidence[0]["end"]] \
        == "the nearest enemy"
    assert set(target_evidence[0]["value"]) == {"baser_hollow_x6", "ashen_hollow"}
    assert not any(
        op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


def test_explicit_non_foe_target_never_falls_back_to_the_sole_live_enemy():
    result = _fireball_result(
        _fireball_state(("baser_hollow_x6", "Baser Hollow x6")),
        source=NON_FOE_ICE,
    )
    frame = _fireball_frame(result)

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] is None
    assert frame["target_name"] is None
    assert not any(row["kind"] == "target" for row in frame["evidence"])
    assert not any(
        op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )
