"""Dice-shaping abilities (2026-07-07 redesign, Bean): a SKILL sets the modifier; an ABILITY
shapes the dice. Passive `edge` (advantage) / `ward` (guard); active `extra_die` / `reroll` /
`surge` (invoked with `use`, cost + cooldown). Distinct from a flat stat buff on purpose.

Every surface is RPG-gated (a `none` session never resolves). Resolution bakes the pool, kept
naturals, cost, and cooldowns into the `check` op, so the reducer + replay read only baked
fields — no registry/RNG at replay.
"""
from __future__ import annotations

import random

from aetherstate import registry, tier0
from aetherstate.config import Config
from aetherstate.state import empty_state, reduce_state


def _rpg(cfg, abilities=None, skills=None, stats=None, resources=None):
    cfg.specialization.name = "rpg"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": {"eid": "kael",
                             "stats": stats or {"DEX": 14, "STR": 14, "CUN": 12},
                             "skills": skills or {"archery": 2, "athletics": 2, "perception": 1},
                             "abilities": abilities or [],
                             "resources": resources or {"stamina": {"max": 10, "cur": 10}}}}
    return st


def _op(res):
    ops = [o for o in res.rule_ops if o["op"] == "check"]
    return ops[0] if ops else None


def _semantic_frame(res):
    return next(
        op["frame"] for op in res.rule_ops
        if op.get("op") == "semantic_frame_commit"
    )


def _run(cfg, content, seed, **kw):
    st = _rpg(cfg, **kw)
    doc = {"messages": [{"role": "user", "content": content}]}
    return _op(tier0.run(doc, "new_turn", False, st, cfg, random.Random(seed)))


_FOCUS_DEFS = {
    "skills": {
        "focus_channel": {
            "name": "Focus Channel", "keyed_stat": "CUN", "group": "fieldcraft",
            "governs": ["channel"], "cost": {"focus": 2},
        },
    },
    "abilities": {
        "focus_burst": {
            "name": "Focus Burst", "kind": "active", "mechanic": "mod", "magnitude": 1,
            "applies_to": "focus_channel", "cost": {"focus": 2},
        },
    },
}


def _focus_state(cfg, current=None):
    resources = ({"focus": {"name": "Focus", "max": 8, "cur": current}}
                 if current is not None else {"stamina": {"max": 10, "cur": 10}})
    st = _rpg(cfg, abilities=["focus_burst"], skills={"focus_channel": 2},
              resources=resources)
    st["player"]["kael"]["defs"] = _FOCUS_DEFS
    return st


def _focus_run(cfg, current, content, seed=7):
    st = _focus_state(cfg, current)
    doc = {"messages": [{"role": "user", "content": content}]}
    return tier0.run(doc, "new_turn", False, st, cfg, random.Random(seed))


# ------------------------------- primitives ---------------------------------------
def test_roll_keep_adds_dice_and_keeps_best():
    kept, pool = registry.roll_keep(2, 1, 6, random.Random(1))
    assert len(pool) == 3 and len(kept) == 2
    assert kept == sorted(pool, reverse=True)[:2]      # advantage: keep the best two


def test_ability_mechanic_normalizes_legacy_and_new():
    assert registry.ability_mechanic({"mechanic": "edge"}) == "edge"
    assert registry.ability_mechanic({"passive_mod": {"skill": "x", "amount": 1}}) == "mod"
    assert registry.ability_mechanic({"kind": "passive"}) == "basis"   # bare marker
    reg = registry.load()
    assert registry.ability_mechanic(reg.abilities["second_wind"]) == "extra_die"
    assert registry.ability_mechanic(reg.abilities["keen_senses"]) == "edge"
    assert registry.ability_mechanic(reg.abilities["silver_tongue"]) == "mod"   # legacy kept


# ------------------------------- passive edge / ward ------------------------------
def test_edge_rolls_an_extra_die_but_no_flat_mod():
    cfg = Config()
    op = _run(cfg, "((aether.check perception vs 8))", 4,
              abilities=["keen_senses"], skills={"perception": 1})
    assert op["_shape"]["edge"] == 1                    # advantage active
    assert len(op["_shape"]["pool"]) == 3               # 2 base + 1 edge die
    assert op["_shape"]["schema"] == "check-roll-shape/1"
    assert op["_shape"]["applied_passive_ids"] == ["keen_senses"]
    assert op["_shape"]["base_pool"] == op["_shape"]["pool"]
    assert op["_seed"] == sorted(op["_shape"]["pool"], reverse=True)[:2]
    # keen_senses is edge now, NOT a flat +1: CUN 12 (+1) + rank 1 = mod 2, nothing more
    assert op["_mod"] == 2


def test_ward_prevents_a_critical_fumble():
    cfg = Config()
    seed = next(s for s in range(500)
                if _run(cfg, "((aether.check archery vs 8))", s,
                        skills={"archery": 0}, stats={"DEX": 8})["tier"] == "crit_fail")
    warded = _run(cfg, "((aether.check archery vs 8))", seed,
                  abilities=["steady_hand"], skills={"archery": 0}, stats={"DEX": 8})
    assert warded["tier"] == "fail"                    # the guard floors crit_fail up to fail
    assert warded["_shape"]["ward"] == 1


# ------------------------------- active abilities ---------------------------------
def test_extra_die_fires_on_a_miss_and_costs_stamina():
    cfg = Config()
    op = _run(cfg, "((aether.check athletics vs 99 use second_wind))", 5,
              abilities=["second_wind"])
    assert op["_shape"]["fired"] == "Second Wind"
    assert len(op["_shape"]["pool"]) == 3              # base 2 + 1 extra die literally added
    assert op["_shape"]["executed_active_ids"] == ["second_wind"]
    assert op["_shape"]["on_fail"] == {
        "ability_id": "second_wind",
        "mechanic": "extra_die",
        "draw_pool": op["_shape"]["pool"][2:],
        "selected": "augmented",
    }
    assert op["_cost"].get("stamina") == 2             # charged only because it fired
    assert op["_ability_cd"]["second_wind"] > 0        # and it went on cooldown


def test_extra_die_does_not_fire_or_cost_on_a_success():
    cfg = Config()
    seed = next(s for s in range(200)
                if _run(cfg, "((aether.check athletics vs 2 use second_wind))", s,
                        abilities=["second_wind"])["tier"] != "crit_fail")
    op = _run(cfg, "((aether.check athletics vs 2 use second_wind))", seed,
              abilities=["second_wind"])
    assert op["tier"] in ("success", "partial", "crit_success")
    assert op.get("_shape", {}).get("fired") is None   # insurance unused
    assert "_cost" not in op                            # nothing spent


def test_surge_lifts_the_capped_scope_ceiling():
    cfg = Config()
    seed = next(s for s in range(3000)
                if _run(cfg, "((aether.check swordplay scope major))", s,
                        skills={"swordplay": 1}, stats={"STR": 12})["_seed"] == [6, 6])
    capped = _run(cfg, "((aether.check swordplay scope major use power_strike))", seed,
                  skills={"swordplay": 1}, stats={"STR": 12})          # unknown -> no surge
    lifted = _run(cfg, "((aether.check swordplay scope major use power_strike))", seed,
                  abilities=["power_strike"], skills={"swordplay": 1}, stats={"STR": 12})
    assert capped["tier"] == "success"                 # scope caps the natural crit down
    assert lifted["tier"] == "crit_success"            # surge lifts the ceiling one step
    assert lifted["_shape"]["surge"] == 3 and lifted["_cost"]["stamina"] == 2


def test_unaffordable_active_is_a_notice_not_a_block():
    cfg = Config()
    st = _rpg(cfg, abilities=["second_wind"], resources={"stamina": {"max": 10, "cur": 0}})
    doc = {"messages": [{"role": "user",
                         "content": "((aether.check athletics vs 99 use second_wind))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(5))
    assert _op(res) is not None                         # the roll still happened (routed)
    assert _op(res).get("_shape", {}).get("fired") is None
    assert any("not enough" in n.lower() for n in res.notices)


def test_custom_pool_reserves_skill_cost_before_active_ability():
    cfg = Config()
    tight = _focus_run(
        cfg, 3, "((aether.check focus_channel vs 2 use focus_burst))")
    tight_op = _op(tight)
    assert tight_op is not None                         # skill still resolves
    assert tight_op["_cost"]["focus"] <= 2            # the reducer never has to clamp an overbook
    assert tight_op["_ability_blocked"] == [
        {"name": "Focus Burst", "why": "not enough resources"},
    ]
    assert any("not enough resources to use Focus Burst" in n for n in tight.notices)

    exact = _focus_run(
        cfg, 4, "((aether.check focus_channel vs 2 use focus_burst))")
    exact_op = _op(exact)
    assert exact_op["_shape"]["burst"] == 1
    assert exact_op["_cost"] == {"focus": 4}           # skill 2 + admitted active 2


def test_active_invocation_survives_as_canonical_event_evidence_even_if_not_executed():
    cfg = Config()
    admitted = _focus_run(
        cfg, 4, "((aether.check focus_channel vs 2 use focus_burst))",
    )
    blocked = _focus_run(
        cfg, 3, "((aether.check focus_channel vs 2 use focus_burst))",
    )
    natural = _focus_run(cfg, 4, "I use Focus Burst to channel the ward.")
    plain = _focus_run(cfg, 4, "((aether.check focus_channel vs 2))")

    for result in (admitted, blocked, natural):
        frame = _semantic_frame(result)
        assert frame["invoked_capability_ids"] == ["focus_burst"]
        assert any(
            row.get("kind") == "invoked_capability"
            and row.get("value") == "focus_burst"
            for row in frame["evidence"]
        )
    assert _semantic_frame(plain)["invoked_capability_ids"] == []


def test_declared_modifier_is_bounded_and_frozen_with_exact_command_evidence():
    cfg = Config()
    state = _rpg(cfg, skills={"athletics": 2})
    admitted = tier0.run(
        {"messages": [{"role": "user",
                        "content": "((aether.check athletics +3 vs 8))"}]},
        "new_turn", False, state, cfg, random.Random(7),
    )
    frame = _semantic_frame(admitted)
    check = _op(admitted)
    assert frame["declared_modifier"] == check["_declared_mod"] == 3
    assert [row["value"] for row in frame["evidence"]
            if row["kind"] == "declared_modifier"] == [3]

    rejected = tier0.run(
        {"messages": [{"role": "user",
                        "content": "((aether.check athletics +21 vs 8))"}]},
        "new_turn", False, state, cfg, random.Random(7),
    )
    assert _op(rejected) is None
    assert any("outside the code-owned" in notice for notice in rejected.notices)


def test_reroll_receipt_keeps_both_roll_phases_and_stable_ability_id():
    cfg = Config()
    state = _rpg(cfg, abilities=["measured_reprise"], skills={"athletics": 2})
    state["player"]["kael"]["defs"] = {"abilities": {"measured_reprise": {
        "name": "Measured Reprise", "kind": "active", "mechanic": "reroll",
        "magnitude": 1, "applies_to": "athletics", "cost": {"stamina": 1},
        "cooldown_turns": 2,
    }}}
    result = tier0.run(
        {"messages": [{"role": "user",
                        "content": "((aether.check athletics vs 99 use measured_reprise))"}]},
        "new_turn", False, state, cfg, random.Random(11),
    )
    shape = _op(result)["_shape"]
    assert shape["executed_active_ids"] == ["measured_reprise"]
    assert len(shape["base_pool"]) == len(shape["on_fail"]["draw_pool"]) == 2
    assert shape["on_fail"]["mechanic"] == "reroll"
    assert shape["on_fail"]["selected"] in {"base", "reroll"}
    expected_pool = (
        shape["on_fail"]["draw_pool"]
        if shape["on_fail"]["selected"] == "reroll" else shape["base_pool"]
    )
    assert shape["pool"] == expected_pool


def test_custom_pool_reservations_carry_across_multiple_checks():
    cfg = Config()
    two_checks = ("((aether.check focus_channel vs 2)) "
                  "((aether.check focus_channel vs 2))")
    tight = _focus_run(cfg, 3, two_checks)
    tight_ops = [op for op in tight.rule_ops if op["op"] == "check"]
    assert len(tight_ops) == 1
    assert tight_ops[0]["_cost"] == {"focus": 2}
    assert any("not enough focus" in n for n in tight.notices)

    exact = _focus_run(cfg, 4, two_checks)
    exact_ops = [op for op in exact.rule_ops if op["op"] == "check"]
    assert len(exact_ops) == 2
    assert sum(op["_cost"]["focus"] for op in exact_ops) == 4


def test_failed_skill_releases_half_cost_before_the_next_check():
    cfg = Config()
    res = _focus_run(
        cfg, 3,
        "((aether.check focus_channel vs 99)) ((aether.check focus_channel vs 2))",
        seed=5,
    )
    ops = [op for op in res.rule_ops if op["op"] == "check"]
    assert [op["tier"] for op in ops] == ["fail", "success"]
    assert [op["_cost"] for op in ops] == [{"focus": 1}, {"focus": 2}]


def test_untracked_custom_skill_cost_blocks_the_roll():
    cfg = Config()
    st = _focus_state(cfg)                             # no focus pool on this older card
    doc = {"messages": [{"role": "user",
                           "content": "((aether.check focus_channel vs 2 use focus_burst))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    assert _op(res) is None
    assert any("not enough focus" in notice.lower() for notice in res.notices)


def test_untracked_custom_ability_cost_blocks_only_that_ability():
    cfg = Config()
    st = _rpg(cfg, abilities=["focus_burst"], skills={"athletics": 2},
              resources={"stamina": {"max": 10, "cur": 10}})
    focus_burst = {**_FOCUS_DEFS["abilities"]["focus_burst"], "applies_to": "athletics"}
    st["player"]["kael"]["defs"] = {"abilities": {"focus_burst": focus_burst}}
    doc = {"messages": [{"role": "user",
                           "content": "((aether.check athletics vs 2 use focus_burst))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    op = _op(res)
    assert op is not None                              # the underlying free skill still rolls
    # Current V3 checks always carry the complete canonical shape receipt.  A blocked shaper is
    # represented by its neutral value plus an empty executed-active set, not a missing field.
    assert op["_shape"]["burst"] == 0
    assert op["_shape"]["executed_active_ids"] == []
    assert op["_ability_blocked"] == [
        {"name": "Focus Burst", "why": "not enough resources"},
    ]


def test_shared_registry_cost_keeps_the_narrow_legacy_waiver():
    """Old cards may lack built-in pools; that exception cannot match a frozen custom def."""
    cfg = Config()
    st = _rpg(cfg, abilities=["power_strike"], skills={"swordplay": 2},
              resources={"focus": {"max": 4, "cur": 4}})
    doc = {"messages": [{"role": "user",
                           "content": "((aether.check swordplay vs 2 use power_strike))"}]}
    op = _op(tier0.run(doc, "new_turn", False, st, cfg, random.Random(7)))
    assert op is not None and op["_shape"]["surge"] == 3
    assert "_cost" not in op                          # shared-registry compatibility only


def test_complete_player_seed_makes_shared_registry_costs_strict_too():
    """The compatibility waiver belongs to old checkpoints, never a normal Creator snapshot."""
    cfg = Config()
    cfg.specialization.name = "rpg"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True}
    reduce_state(st, [{"op": "player_seed", "entity": "kael", "card": {
        "stats": {"STR": 14},
        "skills": {"swordplay": 2},
        "abilities": ["power_strike"],
        "resources": {"focus": {"max": 4, "cur": 4}},
    }, "_turn": 0}])
    doc = {"messages": [{"role": "user",
                           "content": "((aether.check swordplay vs 2 use power_strike))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    op = _op(res)
    assert op is not None and op["_shape"]["surge"] == 0
    assert op["_shape"]["executed_active_ids"] == []
    assert op["_ability_blocked"] == [
        {"name": "Power Strike", "why": "not enough resources"},
    ]


def test_cooldown_blocks_reuse_with_a_notice():
    cfg = Config()
    st = _rpg(cfg, abilities=["second_wind"])
    st["player"]["kael"]["ability_cd"] = {"second_wind": 999}   # recharging far into the future
    st["meta"] = {"turn": 5}
    doc = {"messages": [{"role": "user",
                         "content": "((aether.check athletics vs 99 use second_wind))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(5))
    assert _op(res).get("_shape", {}).get("fired") is None
    assert any("recharging" in n.lower() for n in res.notices)


def test_use_unknown_ability_is_a_notice():
    cfg = Config()
    res = tier0.run({"messages": [{"role": "user",
                                   "content": "((aether.check athletics use nonesuch))"}]},
                    "new_turn", False, _rpg(cfg), cfg, random.Random(5))
    assert _op(res) is not None
    assert any("don't know" in n.lower() for n in res.notices)


def test_none_session_ignores_use_and_shaping():
    cfg = Config()                                      # specialization stays "none"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": {"eid": "kael", "stats": {"DEX": 14}, "skills": {"athletics": 2},
                             "abilities": ["second_wind"],
                             "resources": {"stamina": {"max": 10, "cur": 10}}}}
    res = tier0.run({"messages": [{"role": "user",
                                   "content": "((aether.check athletics use second_wind))"}]},
                    "new_turn", False, st, cfg, random.Random(5))
    assert not [o for o in res.rule_ops if o["op"] == "check"]   # no resolution under none


def test_dice_shaping_replays_deterministically():
    cfg = Config()
    st = _rpg(cfg, abilities=["second_wind"])
    t0 = tier0.run({"messages": [{"role": "user",
                                  "content": "((aether.check athletics vs 99 use second_wind))"}]},
                   "new_turn", False, st, cfg, random.Random(11))
    ops = [
        {
            key: value for key, value in o.items()
            if key not in {
                "_semantic_frame_ref", "_settlement_ref", "_settlement_member_index",
            }
        }
        | {"_turn": 7}
        for o in t0.rule_ops if o.get("op") == "check"
    ]
    a, b = empty_state(), empty_state()
    for s in (a, b):
        s["player"] = {"kael": {"eid": "kael", "stats": {"STR": 14}, "skills": {"athletics": 2},
                                "abilities": ["second_wind"],
                                "resources": {"stamina": {"max": 10, "cur": 10}}}}
        reduce_state(s, [dict(o) for o in ops])
    assert a["rolls"][-1] == b["rolls"][-1]             # identical journaled outcome (replay-pure)
    assert a["rolls"][-1]["shape"]["fired"] == "Second Wind"
    assert a["player"]["kael"]["resources"]["stamina"]["cur"] == 8     # 10 - 2 charged
    assert a["player"]["kael"].get("ability_cd", {}).get("second_wind")


def test_hud_surfaces_ability_mechanics_paperdoll_and_dice_rules():
    from aetherstate.hud import hud_view
    cfg = Config()
    st = _rpg(cfg, abilities=["second_wind", "keen_senses", "silver_tongue"])
    v = hud_view(st, cfg)
    p = v["players"][0]
    mechs = {a["mechanic"] for a in p["abilities"]}
    assert {"extra_die", "edge", "mod"} <= mechs
    sw = next(a for a in p["abilities"] if a["id"] == "second_wind")
    assert sw["active"] and sw["cost"] and sw["mechanic_label"] and sw["group"] == "technique"
    # the paper-doll exposes every equip position with a weapon/armor/trinket kind
    assert any(s["slot"] == "mainhand" and s["kind"] == "weapon" for s in p["gear_slots"])
    assert any(s["kind"] == "armor" for s in p["gear_slots"])
    # dice rules are visible to the player
    assert v["rules"]["dice"] and v["rules"]["thresholds"] and v["rules"]["mechanics"]


def test_creator_freezes_authored_dice_shaper_with_its_mechanic():
    from aetherstate import creator
    p = creator.deterministic_player({
        "name": "Rue", "class": "Duelist", "skills": {"swordplay": 2}, "abilities": ["Riposte"],
        "custom": {"abilities": [{"name": "Riposte", "kind": "active", "mechanic": "extra_die",
                                  "applies_to": "swordplay", "magnitude": 1,
                                  "cost": {"stamina": 2}, "cooldown_turns": 2,
                                  "group": "technique", "desc": "A parry-and-strike."}]}})
    d = p["defs"]["abilities"]["riposte"]
    assert d["mechanic"] == "extra_die" and d["kind"] == "active"       # mechanic survives freeze
    assert d["cost"] == {"stamina": 2} and d["cooldown_turns"] == 2
    assert d["applies_to"] == "swordplay" and d["group"] == "technique"
    card = {"abilities": p["abilities"], "defs": p["defs"]}             # resolver sees it as usable
    assert registry.ability_mechanic(registry.load().known_abilities(card)["riposte"]) == "extra_die"


def test_extra_die_records_whether_it_actually_improved_the_tier():
    cfg = Config()
    # an impossible DC: the extra die fires (and is paid) but CANNOT lift the tier -> improved False
    op = _run(cfg, "((aether.check athletics vs 99 use second_wind))", 5, abilities=["second_wind"])
    assert op["_shape"]["fired"] == "Second Wind" and op["_shape"]["improved"] is False
    # a beatable DC: somewhere the extra die turns the miss around -> improved True (the directive
    # only says "it turned the roll" in that case, so a fail is never narrated as a rescue)
    seed = next((s for s in range(600)
                 if (_run(cfg, "((aether.check athletics vs 8 use second_wind))", s,
                          abilities=["second_wind"], stats={"STR": 10}) or {})
                 .get("_shape", {}).get("improved")), None)
    assert seed is not None
    op2 = _run(cfg, "((aether.check athletics vs 8 use second_wind))", seed,
               abilities=["second_wind"], stats={"STR": 10})
    assert op2["_shape"]["improved"] is True and op2["tier"] != "fail"
