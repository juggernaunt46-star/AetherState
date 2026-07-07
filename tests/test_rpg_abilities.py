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


def _run(cfg, content, seed, **kw):
    st = _rpg(cfg, **kw)
    doc = {"messages": [{"role": "user", "content": content}]}
    return _op(tier0.run(doc, "new_turn", False, st, cfg, random.Random(seed)))


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
    ops = [{**o, "_turn": 7} for o in t0.rule_ops]
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
