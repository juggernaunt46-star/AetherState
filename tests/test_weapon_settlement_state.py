"""Atomic state admission for the V3 ``weapon_attack/1`` settlement envelope."""
from __future__ import annotations

from copy import deepcopy

from aetherstate.config import Config
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import build_meaning_binding, semantic_match_ref
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.state import (
    CHECK_ROLL_SHAPE_SCHEMA,
    MASTERY_TICKS,
    THREAT_HP,
    apply_delta,
    current_state,
    is_empty,
)
from aetherstate.store import Store
from aetherstate.tier0 import Tier0Result, _group_weapon_attack_settlements


def _runtime(
    tag: str,
    *,
    existing_target: bool = True,
    path=":memory:",
    abilities: list[str] | None = None,
    ability_defs: dict[str, dict] | None = None,
):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(path)
    sid, branch = store.create_session(external_id=tag)
    frozen_abilities = ability_defs or {"burst": {
        "name": "Burst", "kind": "active", "mechanic": "mod",
        "applies_to": "meridian_pierce", "cooldown_turns": 2,
    }}
    seed = [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"DEX": 10},
            "skills": {"meridian_pierce": 0},
            "abilities": list(abilities if abilities is not None else frozen_abilities),
            "resources": {
                "hp": {"cur": 20, "max": 20},
                "focus": {"cur": 5, "max": 5},
            },
            "defs": {
                "skills": {"meridian_pierce": {
                    "name": "Meridian Pierce",
                    "keyed_stat": "DEX",
                    "governs": ["strike"],
                    "cost": {"focus": 2},
                }},
                "abilities": frozen_abilities,
            },
        }},
    ]
    if existing_target:
        seed.extend([
            {"op": "combatant_spawn", "name": "Iven", "side": "enemy"},
            {"op": "scene_set", "phase": "combat"},
        ])
    result = apply_delta(store, sid, branch, 0, seed, "genesis", cfg)
    assert not result.quarantined
    return cfg, store, sid, branch


def _candidate(turn: int, ordinal: int = 1, *, invoked: tuple[str, ...] = ()) \
        -> tuple[dict, dict, dict]:
    source = "I use Meridian Pierce to strike Iven."
    frame_id = f"t{turn}.f{ordinal}"
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
        actor_id="kael",
        capability_id="meridian_pierce",
        action_class="weapon_attack",
        target_entity_id="iven",
        target_name="Iven",
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id=f"event.{frame_id}",
        invoked_capability_ids=invoked,
        mechanic_disposition="candidate",
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")
    return meaning.receipt_dict(), binding, frame.snapshot(source)


def _ops(
    turn: int,
    *,
    tier: str = "success",
    opening: bool = False,
    include_semantics: bool = True,
    cost: bool = True,
    cooldown: bool = False,
    invoke_in_frame: bool = True,
    invoked: tuple[str, ...] | None = None,
) -> list[dict]:
    meaning, binding, frame = _candidate(
        turn,
        invoked=(
            invoked if invoked is not None
            else (("burst",) if cooldown and invoke_in_frame else ())
        ),
    )
    kept = {
        "crit_fail": [1, 1], "fail": [2, 2], "partial": [3, 4],
        "success": [5, 5], "crit_success": [6, 6],
    }[tier]
    modifier = 1 if cooldown else 0
    check = {
        "op": "check",
        "skill": "meridian_pierce",
        "char": "kael",
        "result": sum(kept) + modifier,
        "tier": tier,
        "_mod": modifier,
        "_declared_mod": 0,
        "_dice": "2d6",
        "_seed": kept,
        "_shape": {
            "schema": CHECK_ROLL_SHAPE_SCHEMA,
            "abilities": ["Burst"] if cooldown else [],
            "applied_passive_ids": [],
            "executed_active_ids": ["burst"] if cooldown else [],
            "base_pool": kept,
            "on_fail": None,
            "fired": None,
            "improved": False,
            "burst": 1 if cooldown else 0,
            "surge": 0,
            "ward": 0,
            "edge": 0,
            "kept": kept,
            "pool": kept,
        },
        "_semantic_frame_ref": frame["fingerprint"],
    }
    if cost:
        check["_cost"] = {"focus": 2 if tier != "fail" else 1}
    if cooldown:
        check["_ability_cd"] = {"burst": turn + 2}
    factor = {"crit_success": 3, "success": 2, "partial": 1}.get(tier, 0)
    strike = {
        "op": "combatant_hp",
        "target": "iven",
        "delta": -factor,
        "reason": f"Meridian Pierce {tier}",
        "_strike": True,
        "_semantic_frame_ref": frame["fingerprint"],
    }
    members: list[dict] = []
    if opening:
        members.extend([
            {"op": "combatant_spawn", "name": "Iven", "side": "enemy", "_cid": "iven",
             "_semantic_frame_ref": frame["fingerprint"]},
            {"op": "scene_set", "phase": "climax", "_floor": True,
             "_semantic_frame_ref": frame["fingerprint"]},
        ])
    members.extend([check, strike])
    mastery = MASTERY_TICKS[tier]
    if mastery:
        members.append({
            "op": "master_tick",
            "char": "kael",
            "skill": "meridian_pierce",
            "amount": mastery,
            "_semantic_frame_ref": frame["fingerprint"],
        })
    if tier == "crit_fail":
        members.append({
            "op": "effect_add",
            "char": "kael",
            "effect": "Strained",
            "kind": "status",
            "_semantic_frame_ref": frame["fingerprint"],
        })
    result = Tier0Result(rule_ops=members)
    _group_weapon_attack_settlements(
        result,
        [frame],
        {frame["frame_id"]: binding},
    )
    semantic_ops = [
        {"op": "semantic_meaning_commit", "meaning": meaning},
        {"op": "semantic_binding_commit", "binding": binding},
        {"op": "semantic_frame_commit", "frame": frame},
    ]
    return (semantic_ops if include_semantics else []) + result.rule_ops


def _mechanic_only(ops: list[dict]) -> list[dict]:
    return [
        deepcopy(op) for op in ops
        if op.get("op") == "mechanic_settlement_commit" or "_settlement_ref" in op
    ]


def _mutate_member(ops: list[dict], kind: str, mutate) -> None:
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    index = next(i for i, member in enumerate(wrapper["members"])
                 if member.get("op") == kind)
    mutate(wrapper["members"][index])
    projection = next(
        op for op in ops
        if op.get("_settlement_ref") == wrapper["settlement_ref"]
        and op.get("_settlement_member_index") == index
    )
    mutate(projection)


def _configure_on_fail(
    ops: list[dict],
    *,
    ability_id: str,
    label: str,
    mechanic: str,
    draw_pool: list[int],
    selected: str,
) -> None:
    base_pool = [1, 1]
    final_pool = (
        base_pool + draw_pool
        if mechanic == "extra_die" else (draw_pool if selected == "reroll" else base_pool)
    )
    kept = sorted(final_pool, reverse=True)[:2]

    def mutate(check: dict) -> None:
        check.update({
            "result": sum(kept),
            "tier": "success",
            "_mod": 0,
            "_declared_mod": 0,
            "_seed": kept,
            "_cost": {"focus": 3},
            "_ability_cd": {ability_id: 3},
            "_shape": {
                "schema": CHECK_ROLL_SHAPE_SCHEMA,
                "abilities": [label],
                "applied_passive_ids": [],
                "executed_active_ids": [ability_id],
                "base_pool": base_pool,
                "on_fail": {
                    "ability_id": ability_id,
                    "mechanic": mechanic,
                    "draw_pool": draw_pool,
                    "selected": selected,
                },
                "fired": label,
                "improved": True,
                "pool": final_pool,
                "kept": kept,
                "edge": 0,
                "ward": 0,
                "surge": 0,
                "burst": 0,
            },
        })

    _mutate_member(ops, "check", mutate)


def _assert_no_mechanic_mutation(result, before: dict) -> None:
    assert result.quarantined
    assert result.state.get("rolls") == before.get("rolls")
    assert result.state.get("mechanic_settlements") == before.get("mechanic_settlements")
    assert result.state["combat"]["combatants"]["iven"]["hp"] \
        == before["combat"]["combatants"]["iven"]["hp"]
    assert result.state["player"]["kael"]["resources"]["focus"] \
        == before["player"]["kael"]["resources"]["focus"]


def test_hit_wrapper_is_sole_mutator_and_persists_one_mechanic_and_effect_receipt():
    cfg, store, sid, branch = _runtime("settlement-hit")
    result = apply_delta(store, sid, branch, 1, _ops(1), "rule", cfg)

    assert not result.quarantined
    assert sum(op["op"] == "mechanic_settlement_commit" for op in result.applied) == 1
    assert result.state["combat"]["combatants"]["iven"]["hp"]["cur"] \
        == THREAT_HP["standard"] - 2
    assert len(result.state["rolls"]) == 1
    assert result.state["player"]["kael"]["resources"]["focus"]["cur"] == 3
    assert result.state["player"]["kael"]["mastery"]["meridian_pierce"] == 3
    assert len(result.state["mechanic_settlements"]) == 1
    assert store.db.execute("SELECT COUNT(*) FROM mechanic_settlement_receipts").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 1


def test_zero_damage_miss_is_a_complete_settlement():
    cfg, store, sid, branch = _runtime("settlement-miss")
    result = apply_delta(store, sid, branch, 1, _ops(1, tier="fail"), "rule", cfg)

    assert not result.quarantined
    receipt = result.state["mechanic_settlements"][0]["receipt"]
    assert receipt["outcome"] == "miss"
    assert receipt["outcome_quality"] == "fail"
    assert result.state["combat"]["combatants"]["iven"]["hp"]["cur"] \
        == THREAT_HP["standard"]
    assert result.state["player"]["kael"]["resources"]["focus"]["cur"] == 4


def test_opening_and_member_failure_are_all_or_none():
    cfg, store, sid, branch = _runtime("settlement-opening-fault", existing_target=False)
    ops = _ops(1, opening=True)
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    check_index = next(i for i, member in enumerate(wrapper["members"])
                       if member["op"] == "check")
    wrapper["members"][check_index]["_cost"] = {"missing_pool": 2}
    ref = wrapper["settlement_ref"]
    projection = next(op for op in ops
                      if op.get("_settlement_ref") == ref
                      and op.get("_settlement_member_index") == check_index)
    projection["_cost"] = {"missing_pool": 2}

    before = deepcopy(current_state(store, branch))
    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert any("cost or cooldown" in row["reason"] for row in result.quarantined)
    assert "combat" not in result.state
    assert "mechanic_settlements" not in result.state
    assert result.state.get("rolls") == before.get("rolls")
    assert store.db.execute("SELECT COUNT(*) FROM mechanic_settlement_receipts").fetchone()[0] == 0


def test_three_foe_opening_is_one_atomic_settlement_and_keeps_the_war_round():
    from aetherstate.hud import hud_view

    cfg, store, sid, branch = _runtime(
        "settlement-opening-three-foes", existing_target=False
    )
    ops = _ops(1, opening=True)
    semantic_ops = [
        deepcopy(op)
        for op in ops
        if op.get("op") in {
            "semantic_meaning_commit",
            "semantic_binding_commit",
            "semantic_frame_commit",
        }
    ]
    wrapper = deepcopy(next(
        op for op in ops if op.get("op") == "mechanic_settlement_commit"
    ))
    frame_ref = wrapper["frame_ref"]
    wrapper["members"][1:1] = [
        {
            "op": "combatant_spawn",
            "name": "Iven",
            "side": "enemy",
            "_cid": cid,
            "_floor": True,
            "_semantic_frame_ref": frame_ref,
        }
        for cid in ("iven#2", "iven#3")
    ]
    projections = []
    for index, member in enumerate(wrapper["members"]):
        projection = deepcopy(member)
        projection["_settlement_ref"] = wrapper["settlement_ref"]
        projection["_settlement_member_index"] = index
        projections.append(projection)

    result = apply_delta(
        store,
        sid,
        branch,
        1,
        [*semantic_ops, wrapper, *projections],
        "rule",
        cfg,
    )

    assert not result.quarantined
    enemies = [
        row
        for row in result.state["combat"]["combatants"].values()
        if row["side"] == "enemy"
    ]
    assert len(enemies) == 3
    receipt = result.state["mechanic_settlements"][0]["receipt"]
    admissions = [
        change for change in receipt["applied_changes"]
        if change["kind"] == "target_admission"
    ]
    assert [change["subject_id"] for change in admissions] == [
        "iven", "iven#2", "iven#3",
    ]
    war = hud_view(result.state, cfg)["war_room"]
    assert war["active"] and war["round"] >= 1
    assert len([row for row in war["combatants"] if row["side"] == "enemy"]) == 3


def test_lost_projection_fails_closed_before_any_member_mutates():
    cfg, store, sid, branch = _runtime("settlement-lost-projection")
    semantic = _ops(1)[:3]
    committed = apply_delta(store, sid, branch, 1, semantic, "rule", cfg)
    assert not committed.quarantined
    group = _mechanic_only(_ops(1))[0:-1]
    before = deepcopy(current_state(store, branch))

    result = apply_delta(store, sid, branch, 1, group, "rule", cfg)

    assert result.quarantined
    assert not result.applied
    assert result.state == before


def test_exact_retry_is_duplicate_without_journal_and_changed_retry_conflicts():
    cfg, store, sid, branch = _runtime("settlement-retry")
    ops = _ops(1)
    first = apply_delta(store, sid, branch, 1, ops, "rule", cfg)
    assert not first.quarantined
    journal_before = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]
    state_before = deepcopy(current_state(store, branch))

    exact = apply_delta(store, sid, branch, 1, deepcopy(ops), "rule", cfg)
    assert not exact.applied and not exact.quarantined
    assert exact.duplicates
    assert current_state(store, branch) == state_before
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == journal_before

    changed = _mechanic_only(ops)
    wrapper = next(op for op in changed if op["op"] == "mechanic_settlement_commit")
    check_index = next(i for i, member in enumerate(wrapper["members"])
                       if member["op"] == "check")
    wrapper["members"][check_index]["result"] += 1
    next(op for op in changed
         if op.get("_settlement_member_index") == check_index)["result"] += 1
    conflict = apply_delta(store, sid, branch, 1, changed, "rule", cfg)
    assert conflict.quarantined and not conflict.applied
    assert current_state(store, branch) == state_before


def test_critical_failure_captures_cost_and_exact_consequence():
    cfg, store, sid, branch = _runtime("settlement-crit-fail")
    result = apply_delta(store, sid, branch, 1, _ops(1, tier="crit_fail"), "rule", cfg)

    assert not result.quarantined
    receipt = result.state["mechanic_settlements"][0]["receipt"]
    assert receipt["outcome_quality"] == "crit_fail"
    assert {row["kind"] for row in receipt["applied_changes"]} == {
        "hp", "cost", "consequence",
    }
    assert "strained" in result.state["effects"]["kael"]


def test_active_cooldown_is_recomputed_and_captured_with_the_check_cost():
    cfg, store, sid, branch = _runtime("settlement-cooldown")
    result = apply_delta(store, sid, branch, 1, _ops(1, cooldown=True), "rule", cfg)

    assert not result.quarantined
    receipt = result.state["mechanic_settlements"][0]["receipt"]
    assert {row["kind"] for row in receipt["applied_changes"]} >= {"cost", "cooldown"}
    assert result.state["player"]["kael"]["ability_cd"]["burst"] == 3


def test_active_ability_cannot_be_transplanted_without_semantic_invocation():
    cfg, store, sid, branch = _runtime("settlement-active-transplant")
    result = apply_delta(
        store, sid, branch, 1,
        _ops(1, cooldown=True, invoke_in_frame=False), "rule", cfg,
    )

    assert result.quarantined
    assert any("semantic invocation" in row["reason"] for row in result.quarantined)
    assert "mechanic_settlements" not in result.state
    assert result.state["player"]["kael"]["resources"]["focus"]["cur"] == 5


def test_twenty_die_passive_edge_pool_rejects_without_mechanic_mutation():
    cfg, store, sid, branch = _runtime(
        "settlement-edge-overstuff",
        abilities=["edge"],
        ability_defs={"edge": {
            "name": "Exact Edge", "kind": "passive", "mechanic": "edge",
            "magnitude": 1, "applies_to": "meridian_pierce",
        }},
    )
    ops = _ops(1)

    def overstuff(check: dict) -> None:
        pool = [5, 5] + [1] * 18
        check["_shape"].update({
            "abilities": ["Exact Edge"],
            "applied_passive_ids": ["edge"],
            "base_pool": pool,
            "pool": pool,
            "edge": 1,
        })

    _mutate_member(ops, "check", overstuff)
    before = deepcopy(current_state(store, branch))
    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert any("base pool" in row["reason"] for row in result.quarantined)
    _assert_no_mechanic_mutation(result, before)


def test_twenty_die_extra_die_phase_rejects_without_mechanic_mutation():
    cfg, store, sid, branch = _runtime(
        "settlement-extra-overstuff",
        abilities=["second_chance"],
        ability_defs={"second_chance": {
            "name": "Second Chance", "kind": "active", "mechanic": "extra_die",
            "magnitude": 1, "applies_to": "meridian_pierce",
            "cost": {"focus": 1}, "cooldown_turns": 2,
        }},
    )
    ops = _ops(1, invoked=("second_chance",))
    _configure_on_fail(
        ops,
        ability_id="second_chance",
        label="Second Chance",
        mechanic="extra_die",
        draw_pool=[5, 5] + [1] * 16,
        selected="augmented",
    )
    before = deepcopy(current_state(store, branch))
    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert any("extra-die draw" in row["reason"] for row in result.quarantined)
    _assert_no_mechanic_mutation(result, before)


def test_exact_reroll_phases_settle_and_missing_or_wrong_selection_rejects():
    reroll_def = {"reroll": {
        "name": "Measured Reprise", "kind": "active", "mechanic": "reroll",
        "magnitude": 1, "applies_to": "meridian_pierce",
        "cost": {"focus": 1}, "cooldown_turns": 2,
    }}
    cfg, store, sid, branch = _runtime(
        "settlement-reroll-valid", abilities=["reroll"], ability_defs=reroll_def,
    )
    valid = _ops(1, invoked=("reroll",))
    _configure_on_fail(
        valid, ability_id="reroll", label="Measured Reprise", mechanic="reroll",
        draw_pool=[5, 5], selected="reroll",
    )
    accepted = apply_delta(store, sid, branch, 1, valid, "rule", cfg)
    assert not accepted.quarantined
    assert accepted.state["combat"]["combatants"]["iven"]["hp"]["cur"] \
        == THREAT_HP["standard"] - 2

    for tag, mutate, reason in (
        ("missing", lambda shape: shape.__setitem__("on_fail", None), "on-fail phase"),
        ("wrong", lambda shape: shape["on_fail"].__setitem__("selected", "base"),
         "selected the wrong phase"),
    ):
        cfg, store, sid, branch = _runtime(
            f"settlement-reroll-{tag}", abilities=["reroll"], ability_defs=reroll_def,
        )
        ops = _ops(1, invoked=("reroll",))
        _configure_on_fail(
            ops, ability_id="reroll", label="Measured Reprise", mechanic="reroll",
            draw_pool=[5, 5], selected="reroll",
        )
        _mutate_member(ops, "check", lambda check: mutate(check["_shape"]))
        before = deepcopy(current_state(store, branch))
        rejected = apply_delta(store, sid, branch, 1, ops, "rule", cfg)
        assert any(reason in row["reason"] for row in rejected.quarantined)
        _assert_no_mechanic_mutation(rejected, before)


def test_duplicate_display_name_cannot_transplant_a_different_active_id():
    twin_defs = {
        "burst": {
            "name": "Twin", "kind": "active", "mechanic": "mod",
            "magnitude": 1, "applies_to": "meridian_pierce",
        },
        "other": {
            "name": "Twin", "kind": "active", "mechanic": "mod",
            "magnitude": 1, "applies_to": "meridian_pierce",
        },
    }
    cfg, store, sid, branch = _runtime(
        "settlement-stable-active-id",
        abilities=["burst", "other"],
        ability_defs=twin_defs,
    )
    ops = _ops(1, invoked=("burst",))

    def transplant(check: dict) -> None:
        check.update({"_mod": 1, "result": 11})
        check["_shape"].update({
            "abilities": ["Twin"],
            "executed_active_ids": ["other"],
            "burst": 1,
        })

    _mutate_member(ops, "check", transplant)
    before = deepcopy(current_state(store, branch))
    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert any("semantic invocation" in row["reason"] for row in result.quarantined)
    _assert_no_mechanic_mutation(result, before)


def test_cooling_active_and_declared_modifier_transplant_both_fail_closed():
    cfg, store, sid, branch = _runtime("settlement-cooldown-ready-guard")
    first = apply_delta(store, sid, branch, 1, _ops(1, cooldown=True), "rule", cfg)
    assert not first.quarantined
    before = deepcopy(current_state(store, branch))
    cooling = apply_delta(store, sid, branch, 2, _ops(2, cooldown=True), "rule", cfg)
    assert any("still cooling down" in row["reason"] for row in cooling.quarantined)
    _assert_no_mechanic_mutation(cooling, before)

    cfg, store, sid, branch = _runtime("settlement-modifier-transplant")
    ops = _ops(1)

    def transplant_modifier(check: dict) -> None:
        check.update({"_declared_mod": 1, "_mod": 1, "result": 11})

    _mutate_member(ops, "check", transplant_modifier)
    before = deepcopy(current_state(store, branch))
    rejected = apply_delta(store, sid, branch, 1, ops, "rule", cfg)
    assert any("semantic frame" in row["reason"] for row in rejected.quarantined)
    _assert_no_mechanic_mutation(rejected, before)


def test_failed_roll_discount_cannot_retroactively_afford_an_active_ability():
    ability_defs = {"overbooked": {
        "name": "Overbooked Reprise", "kind": "active", "mechanic": "reroll",
        "magnitude": 1, "applies_to": "meridian_pierce",
        "cost": {"focus": 4}, "cooldown_turns": 2,
    }}
    cfg, store, sid, branch = _runtime(
        "settlement-full-reservation-affordability",
        abilities=["overbooked"],
        ability_defs=ability_defs,
    )
    ops = _ops(1, invoked=("overbooked",))
    _configure_on_fail(
        ops,
        ability_id="overbooked",
        label="Overbooked Reprise",
        mechanic="reroll",
        draw_pool=[5, 5],
        selected="reroll",
    )
    _mutate_member(
        ops,
        "check",
        lambda check: check.__setitem__("_cost", {"focus": 5}),
    )
    before = deepcopy(current_state(store, branch))

    rejected = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert any("cannot afford" in row["reason"] for row in rejected.quarantined)
    _assert_no_mechanic_mutation(rejected, before)


def test_policy_tampering_cost_result_damage_and_consequence_all_fail_closed():
    mutations = ("cost", "cooldown", "result", "damage", "consequence")
    for mutation in mutations:
        cfg, store, sid, branch = _runtime(f"settlement-policy-{mutation}")
        tier = "crit_fail" if mutation == "consequence" else "success"
        ops = _ops(1, tier=tier, cooldown=mutation == "cooldown")
        wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
        ref = wrapper["settlement_ref"]
        if mutation == "cost":
            index = next(i for i, member in enumerate(wrapper["members"])
                         if member["op"] == "check")
            wrapper["members"][index].pop("_cost")
            next(op for op in ops if op.get("_settlement_ref") == ref
                 and op.get("_settlement_member_index") == index).pop("_cost")
        elif mutation == "cooldown":
            index = next(i for i, member in enumerate(wrapper["members"])
                         if member["op"] == "check")
            wrapper["members"][index].pop("_ability_cd")
            next(op for op in ops if op.get("_settlement_ref") == ref
                 and op.get("_settlement_member_index") == index).pop("_ability_cd")
        elif mutation == "result":
            index = next(i for i, member in enumerate(wrapper["members"])
                         if member["op"] == "check")
            wrapper["members"][index]["result"] = 999
            next(op for op in ops if op.get("_settlement_ref") == ref
                 and op.get("_settlement_member_index") == index)["result"] = 999
        elif mutation == "damage":
            index = next(i for i, member in enumerate(wrapper["members"])
                         if member["op"] == "combatant_hp")
            wrapper["members"][index]["delta"] = -14
            next(op for op in ops if op.get("_settlement_ref") == ref
                 and op.get("_settlement_member_index") == index)["delta"] = -14
        else:
            index = next(i for i, member in enumerate(wrapper["members"])
                         if member["op"] == "effect_add")
            wrapper["members"][index]["effect"] = "Backlash"
            next(op for op in ops if op.get("_settlement_ref") == ref
                 and op.get("_settlement_member_index") == index)["effect"] = "Backlash"

        before = deepcopy(current_state(store, branch))
        result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)
        assert result.quarantined
        assert "mechanic_settlements" not in result.state
        assert result.state["combat"]["combatants"]["iven"]["hp"] \
            == before["combat"]["combatants"]["iven"]["hp"]


def test_dependent_band_spawn_requires_successful_same_turn_primary_settlement():
    cfg, store, sid, branch = _runtime("settlement-dependent-spawn")
    ops = _ops(1)
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    ref = wrapper["settlement_ref"]
    check_index = next(i for i, member in enumerate(wrapper["members"])
                       if member["op"] == "check")
    wrapper["members"][check_index]["result"] = 999
    next(op for op in ops if op.get("_settlement_ref") == ref
         and op.get("_settlement_member_index") == check_index)["result"] = 999
    ops.append({
        "op": "combatant_spawn", "name": "Iven's Retainer", "side": "enemy",
        "_requires_settlement_ref": ref,
    })

    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert result.quarantined
    assert all(row.get("name") != "Iven's Retainer"
               for row in result.state["combat"]["combatants"].values())


def test_late_opening_failure_rolls_back_worldlex_materialization():
    cfg, store, sid, branch = _runtime("settlement-worldlex-rollback", existing_target=False)
    apply_delta(
        store, sid, branch, 0,
        [{"op": "world_identity_set", "world_id": "world_" + "8" * 32}],
        "genesis", cfg,
    )
    ops = _ops(1, opening=True)
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    ref = wrapper["settlement_ref"]
    spawn_index = next(i for i, member in enumerate(wrapper["members"])
                       if member["op"] == "combatant_spawn")
    wrapper["members"][spawn_index]["name"] = "Mara"
    next(op for op in ops if op.get("_settlement_ref") == ref
         and op.get("_settlement_member_index") == spawn_index)["name"] = "Mara"
    before = store.db.execute(
        "SELECT COUNT(*) FROM worldlex_capability_definitions"
    ).fetchone()[0]

    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    after = store.db.execute(
        "SELECT COUNT(*) FROM worldlex_capability_definitions"
    ).fetchone()[0]
    assert result.quarantined and "combat" not in result.state
    assert after == before


def test_reopen_replays_wrapper_once_and_later_occurrence_of_same_prose_is_distinct(tmp_path):
    path = tmp_path / "settlement.sqlite3"
    cfg, store, sid, branch = _runtime("settlement-reopen", path=path)
    first = apply_delta(store, sid, branch, 1, _ops(1), "rule", cfg)
    assert not first.quarantined
    after_first = deepcopy(first.state)
    store.db.close()

    reopened = Store(path)
    assert current_state(reopened, branch) == after_first
    second = apply_delta(reopened, sid, branch, 2, _ops(2), "rule", cfg)
    assert not second.quarantined
    refs = [row["receipt"]["settlement_ref"] for row in second.state["mechanic_settlements"]]
    assert len(refs) == 2 and len(set(refs)) == 2
    assert len(second.state["rolls"]) == 2


def test_seventeen_current_turn_semantic_events_survive_dependency_order_and_are_visible():
    cfg, store, sid, branch = _runtime("semantic-seventeen")
    ops: list[dict] = []
    for ordinal in range(1, 18):
        source = f"I inspect numbered gate {ordinal}."
        frame_id = f"t1.f{ordinal}"
        meaning = load_default_semantic_fabric().translate(source)
        action = next(match for match in meaning.for_lex("action")
                      if match.concept_id == "action.inspect")
        binding = build_meaning_binding(
            meaning,
            binding_id=f"binding.{frame_id}",
            event_node_id=f"event.{frame_id}",
            event_span=(0, len(source)),
            field_provenance=[{
                "field": "action_class", "value": "inspect", "defaulted": False,
                "evidence_refs": [semantic_match_ref(action)],
            }],
            role_evidence=[{"role": "action", "evidence_refs": [semantic_match_ref(action)]}],
        )
        frame = ActionFrame(
            frame_id=frame_id, clause_index=ordinal - 1, start=0, end=len(source),
            actor_id="kael", capability_id="perception", action_class="inspect",
            polarity="positive", modality="actual", time_scope="current",
            meaning_ref=meaning.receipt_dict()["fingerprint"],
            fabric_fingerprint=meaning.fabric_fingerprint,
            meaning_binding_ref=binding["fingerprint"], event_node_id=f"event.{frame_id}",
            mechanic_disposition="candidate",
        )
        frame.add_evidence("action", action.start, action.end, "inspect")
        ops.extend([
            {"op": "semantic_meaning_commit", "meaning": meaning.receipt_dict()},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_frame_commit", "frame": frame.snapshot(source)},
        ])

    result = apply_delta(store, sid, branch, 1, ops, "rule", cfg)

    assert not result.quarantined
    assert len(result.state["semantic_meanings"]) == 17
    assert len(result.state["semantic_bindings"]) == 17
    assert len(result.state["semantic_frames"]) == 17
    assert not is_empty(result.state)
