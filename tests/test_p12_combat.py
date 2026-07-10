"""Phase 1 — full combat loop + 3v3 party / War Room (plan doc 13, ratified 2026-07-09).

Covers: combatant instances (snapshot-frozen HP by threat tier, extras vs tracked),
the 3v3 cap, the clamped combatant_hp channel + the [hp] tag reroute, code-derived
player strike damage (outcome tier x weapon magnitude), [foe]/[clash] tag parsing,
the combat referee (auto-spawn floor, ally enlistment, HP-0 defeat -> curated XP +
frozen loot roll, self-ending fights), FULL tracked-NPC wound persistence, the
[WAR]/[ALLY] directive surfaces (deterministic, no journal row), the combatant_alive
linter rule, Creator loot freezing — and, per the RPG invariants, `none`-leak guards
and deterministic replay.
"""
from __future__ import annotations

import json

from aetherstate import tier0
from aetherstate.compose import _render_directive, render_header
from aetherstate.config import Config
from aetherstate.extraction import delta_json_schema
from aetherstate.linter import run as lint_run
from aetherstate.prompts import rules_contract, system_prompt
from aetherstate.state import (THREAT_HP, THREAT_XP, apply_delta, authority_violation,
                               combat_ops, current_state, empty_state, state_summary,
                               validate_op)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    """Kael (player) + Mira (friend) + Suki (present) + Vex (present hostile)."""
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p12")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Mira"},
        {"op": "entity_add", "name": "Suki"},
        {"op": "entity_add", "name": "Vex"},
        {"op": "presence", "entity": "Suki", "present": True},
        {"op": "presence", "entity": "Vex", "present": True},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"DEX": 14}, "skills": {"stealth": 3, "melee": 2},
                  "resources": {"hp": {"max": 20}, "stamina": {"max": 12}}}}],
        "genesis", cfg)
    return store, sid, bid


class _Rig:
    def __init__(self, value):
        self.value = value

    def randint(self, a, b):
        return self.value


def _spawn(store, sid, bid, turn, cfg, name="Bandit", side="enemy", tier="minion", **kw):
    r = apply_delta(store, sid, bid, turn, [
        {"op": "combatant_spawn", "name": name, "side": side, "tier": tier, **kw}],
        "rule", cfg)
    assert r.applied, r.quarantined
    return r


# ------------------------------ validation + authority --------------------------------
def test_p12_op_validation_shapes():
    assert validate_op({"op": "combatant_spawn", "name": "Thug", "side": "enemy"}) is not None
    assert validate_op({"op": "combatant_spawn", "name": " ", "side": "enemy"}) is None
    assert validate_op({"op": "combatant_spawn", "name": "T", "side": "left"}) is None
    assert validate_op({"op": "combatant_spawn", "name": "T", "side": "enemy",
                        "tier": "demigod"}) is None
    assert validate_op({"op": "combatant_hp", "target": "t", "delta": -3}) is not None
    assert validate_op({"op": "combatant_hp", "target": "t", "delta": True}) is None
    assert validate_op({"op": "combatant_defeat", "target": "t"}) is not None
    assert validate_op({"op": "combat_end"}) is not None
    assert validate_op({"op": "clash_record", "a": "A", "b": "B"}) is not None
    assert validate_op({"op": "clash_record", "a": "A", "b": "a"}) is None
    assert validate_op({"op": "loot_table", "tier": "boss", "entries": []}) is not None
    assert validate_op({"op": "loot_table", "tier": "god", "entries": []}) is None


def test_p12_authority_matrix():
    cfg, st = _rpg_cfg(), empty_state()
    for op in ({"op": "combatant_spawn", "name": "T", "side": "enemy"},
               {"op": "combatant_defeat", "target": "t"},
               {"op": "combat_end"},
               {"op": "loot_table", "tier": "minion", "entries": []}):
        assert authority_violation(op, "extraction", st, cfg) is not None, op["op"]
        for src in ("user", "genesis", "rule"):
            assert authority_violation(op, src, st, cfg) is None, (op["op"], src)
    for op in ({"op": "combatant_hp", "target": "t", "delta": -2},
               {"op": "clash_record", "a": "A", "b": "B"}):
        assert authority_violation(op, "extraction", st, cfg) is None, op["op"]


# ------------------------------ spawn: frozen instances -------------------------------
def test_spawn_bakes_tier_hp_and_caps_at_3v3():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Rat", tier="minion")
    _spawn(store, sid, bid, 1, cfg, name="Brute", tier="boss", armament="great club")
    st = current_state(store, bid)
    cb = st["combat"]
    assert cb["active"] and cb["started_turn"] == 1
    by_name = {r["name"]: r for r in cb["combatants"].values()}
    assert by_name["Rat"]["hp"] == {"cur": THREAT_HP["minion"], "max": THREAT_HP["minion"]}
    assert by_name["Brute"]["hp"]["max"] == THREAT_HP["boss"]
    assert by_name["Brute"]["armament"] == "great club"
    assert by_name["Rat"]["kind"] == "extra" and by_name["Rat"]["loot"]
    _spawn(store, sid, bid, 1, cfg, name="Third")
    r = apply_delta(store, sid, bid, 1, [
        {"op": "combatant_spawn", "name": "Fourth", "side": "enemy"}], "rule", cfg)
    assert not r.applied and "3v3" in r.quarantined[0]["reason"]


def test_tracked_spawn_references_entity_and_rejects_twins():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Vex", tier="standard", char="Vex")
    st = current_state(store, bid)
    row = next(iter(st["combat"]["combatants"].values()))
    assert row["kind"] == "tracked" and row["eid"] == "vex"
    r = apply_delta(store, sid, bid, 1, [
        {"op": "combatant_spawn", "name": "Vex", "side": "enemy", "char": "Vex"}],
        "rule", cfg)
    assert not r.applied and "already on the field" in r.quarantined[0]["reason"]


# ------------------------------ the harm channel --------------------------------------
def test_combatant_hp_clamps_proposals_and_rejects_unknown():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Brute", tier="boss")   # 44 max -> clamp 11
    r = apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Brute", "delta": -40}], "extraction", cfg)
    assert r.applied
    st = current_state(store, bid)
    row = next(iter(st["combat"]["combatants"].values()))
    assert row["hp"]["cur"] == THREAT_HP["boss"] - 11            # max(5, 44//4) = 11
    r = apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Nobody", "delta": -3}], "extraction", cfg)
    assert not r.applied and "not a live combatant" in r.quarantined[0]["reason"]


def test_hp_tag_reroutes_to_combatant_rows():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    ops = tier0.parse_reply_tags(
        "Steel sings. [hp | Bandit | -4 | a clean cut]\n[hp | Kael | -2 | grazed]", st)
    kinds = {(o["op"], o.get("target") or o.get("char")) for o in ops}
    assert ("combatant_hp", "bandit") in kinds          # the foe: rerouted to the row
    assert ("hp_adj", "kael") in kinds                  # the player: the classic channel


# ------------------------------ player strike damage ----------------------------------
def test_check_at_target_deals_tiered_weapon_damage():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user",
                         "content": "((aether.check melee at Bandit)) I cut him down."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))    # 2d6=10 -> success
    strikes = [o for o in res.rule_ops if o.get("op") == "combatant_hp"]
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert checks and checks[0]["_target"] == "Bandit" and checks[0]["_dmg"] == 2
    assert strikes and strikes[0]["delta"] == -2 and strikes[0]["_strike"] is True
    r = apply_delta(store, sid, bid, 2, res.rule_ops, "rule", cfg)
    st2 = r.state
    row = next(iter(st2["combat"]["combatants"].values()))
    assert row["hp"]["cur"] == THREAT_HP["standard"] - 2         # exact, code-decided


def test_nl_attack_binds_the_lone_foe_and_miss_draws_no_blood():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I strike with stealth at his back."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(2))    # 2d6=4(+mods) -> miss-ish
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert checks and checks[0].get("_target") == "Bandit"       # lone foe + attack verb
    if checks[0]["tier"] in ("fail", "crit_fail"):
        assert not [o for o in res.rule_ops if o.get("op") == "combatant_hp"]


def test_strike_then_dm_hp_tag_on_same_foe_counts_once():
    """fix B (2026-07-10): the code-decided player strike is authoritative and pre-applied; the
    DM re-narrating that SAME blow as a same-turn [hp | <foe>] tag must not double it. A chip
    hit on a DIFFERENT foe still lands, and a later turn's harm on the struck foe applies."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    _spawn(store, sid, bid, 1, cfg, name="Thug", tier="standard")
    apply_delta(store, sid, bid, 2, [                       # the player's bound strike: -3
        {"op": "combatant_hp", "target": "Bandit", "delta": -3, "_strike": True}], "rule", cfg)
    apply_delta(store, sid, bid, 2, [                       # same turn: the DM re-tags the SAME
        {"op": "combatant_hp", "target": "Bandit", "delta": -3},   # blow (dropped) + a chip on
        {"op": "combatant_hp", "target": "Thug", "delta": -2}], "extraction", cfg)  # the OTHER
    rows = current_state(store, bid)["combat"]["combatants"]
    assert rows["bandit"]["hp"]["cur"] == THREAT_HP["standard"] - 3   # strike counted ONCE
    assert rows["thug"]["hp"]["cur"] == THREAT_HP["standard"] - 2     # other foe's chip lands
    apply_delta(store, sid, bid, 3, [                       # a LATER turn's harm applies fine
        {"op": "combatant_hp", "target": "Bandit", "delta": -4}], "extraction", cfg)
    assert current_state(store, bid)["combat"]["combatants"]["bandit"]["hp"]["cur"] \
        == THREAT_HP["standard"] - 7


def test_floor_staged_foe_takes_the_opening_strike_and_raises_the_phase():
    """fix C (2026-07-10): attacking a DM-narrated hostile with no live combatant stages the foe
    AND lands the opening strike the SAME turn (was: the spawn journaled after the check, so the
    first blow whiffed). The scene also rises to a combat phase."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)                          # Kael has melee 2; no live foes
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant",
         "content": "A hollow revenant drags itself out of the mere, jaw unhinged, wading in."},
        {"role": "user", "content": "((aether.check melee at Hollow Revenant)) I cut it down."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))       # 2d6=10 -> success
    spawn = next(o for o in res.rule_ops if o.get("op") == "combatant_spawn")
    assert spawn.get("_floor") and spawn.get("_cid") == "hollow_revenant"
    strikes = [o for o in res.rule_ops if o.get("op") == "combatant_hp"]
    assert strikes and strikes[0]["target"] == "hollow_revenant" and strikes[0]["_strike"]
    assert any(o.get("op") == "scene_set" and o.get("phase") == "climax" for o in res.rule_ops)
    r = apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)  # spawn (2) before hp (6)
    row = r.state["combat"]["combatants"]["hollow_revenant"]
    assert row["hp"]["cur"] < row["hp"]["max"]                      # the opening blow LANDED
    assert r.state["scene"]["phase"] == "climax"


def test_floor_names_the_target_not_the_weapon_or_a_location():
    """Redgate live (2026-07-10): "lunge FROM THE PINES and stab MY SHORTSWORD into the nearest
    cutthroat" must stage the CUTTHROAT — not the movement-verb's location object, not the
    Player's weapon. The targeting-preposition object ("into <foe>") wins; weapon/location words
    are skipped."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)                     # Kael, no live foes
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant",
         "content": "Three Redgate cutthroats dice by the fire in the pines of the overlook."},
        {"role": "user", "content": ("((aether.check melee)) I lunge from the pines and stab my "
                                     "shortsword into the nearest Redgate cutthroat.")}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    spawn = next((o for o in res.rule_ops if o.get("op") == "combatant_spawn"), None)
    assert spawn is not None, "the floor should still stage a foe"
    nm = spawn["name"].lower()
    assert "cutthroat" in nm and "sword" not in nm and "pine" not in nm


def test_peaceful_scene_never_stages_a_phantom_foe():
    """Thornhale live ROOT-CAUSE regression (2026-07-10): a PEACEFUL message ('slip out to the
    STABLE and scry Fenn') fired Stealth + Hexcraft checks, but the old attack-verb regex matched
    "stab"le and 'slip OUT' staged a foe named 'Out' — the phantom [WAR]/[DIRECTIVE] then wrecked
    the scene. Now: the fixed regex ignores 'stable', and only a COMBAT skill can arm the floor,
    so a stealth/hexcraft turn stages no foe, no strike, no war room."""
    cfg = _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p12peace")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Wren", "kind": "player"},
        {"op": "entity_add", "name": "Fenn"},
        {"op": "presence", "entity": "Fenn", "present": True},
        {"op": "player_seed", "entity": "Wren",
         "card": {"stats": {"DEX": 14, "INT": 13}, "skills": {"stealth": 3, "hexcraft": 2},
                  "defs": {"skills": {
                      "stealth": {"name": "Stealth", "keyed_stat": "DEX",
                                  "governs": ["sneak", "slip", "creep", "hide"]},
                      "hexcraft": {"name": "Hexcraft", "keyed_stat": "INT",
                                   "governs": ["scry", "ward", "curse", "hex"]}}},
                  "resources": {"hp": {"max": 20}, "mana": {"max": 8}}}}], "genesis", cfg)
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant", "content": "Old Fenn crouches in the stable, drawing circles."},
        {"role": "user", "content": "I slip out back to the stable and scry Fenn for a hex."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(4))
    assert res.checks, "the stealth/hexcraft checks should still fire (they're legit)"
    assert not any(o.get("op") == "combatant_spawn" for o in res.rule_ops)   # NO phantom foe
    assert not any(o.get("op") == "combatant_hp" for o in res.rule_ops)      # NO phantom strike
    assert not any(o.get("op") == "scene_set" and o.get("phase") == "climax"
                   for o in res.rule_ops)                                    # scene stays peaceful
    r = apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    assert not (r.state.get("combat") or {}).get("active")                   # war room never armed


def test_initiative_order_ranks_combatants_and_renders_everywhere():
    """Explicit initiative (2026-07-10, Bean): every LIVE combatant + the Player is ranked by a
    curated, baked init score (replay-pure); the order rides the [INIT] directive line AND the
    HUD war-room payload; a minion never leads a +DEX player or an elite."""
    from aetherstate.compose import _initiative_order
    from aetherstate.hud import hud_view
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)                     # Kael DEX 14 (+2)
    _spawn(store, sid, bid, 1, cfg, name="Goblin", tier="minion")   # _mod 0 -> low init
    _spawn(store, sid, bid, 1, cfg, name="Ogre", tier="elite")      # _mod 2 -> high init
    st = current_state(store, bid)
    rows = st["combat"]["combatants"]
    assert all(isinstance(r.get("init"), int) for r in rows.values())   # baked on the rows
    order = _initiative_order(st, cfg)
    names = [nm for _s, nm, _sd in order]
    assert set(names) == {"Kael", "Goblin", "Ogre"} and names[-1] == "Goblin"   # minion last
    hv = hud_view(st, cfg)                              # HUD carries the same order
    assert [o["name"] for o in hv["war_room"]["order"]] == names
    st["_fresh_checks"] = []                            # the directive renders an [INIT] line
    d = _render_directive(st, cfg)
    assert "[INIT]" in d and "Kael" in d and "Goblin" in d


# ------------------------------ the DM's combat tags -----------------------------------
def test_foe_tag_parses_tier_armament_and_tracked_names():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_foe_tags(
        "It gets ugly. [foe | Dock Thug | minion | uses a rusty pipe]\n"
        "[foe | Vex | elite]", st)
    assert ops[0] == {"op": "combatant_spawn", "name": "Dock Thug", "side": "enemy",
                      "tier": "minion", "armament": "a rusty pipe"}
    assert ops[1]["char"] == "vex" and ops[1]["tier"] == "elite"   # known NPC: tracked


def test_clash_tag_records_on_real_rows_only():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_reply_tags(
        "[clash | Mira vs Suki | knives in the dark | Mira left bleeding]", st)
    assert ops == [{"op": "clash_record", "a": "Mira", "b": "Suki",
                    "method": "knives in the dark", "outcome": "Mira left bleeding"}]
    r = apply_delta(store, sid, bid, 1, ops, "extraction", cfg)
    assert r.applied
    st2 = current_state(store, bid)
    assert st2["clashes"][0]["outcome"] == "Mira left bleeding"
    assert any("clashed with" in f["statement"] for f in st2["facts"].values())
    bad = apply_delta(store, sid, bid, 1, [
        {"op": "clash_record", "a": "Mira", "b": "Nobody Known"}], "extraction", cfg)
    assert not bad.applied                                # unknown participant: quarantined


# ------------------------------ the combat referee -------------------------------------
def test_referee_defeat_awards_tier_xp_and_ends_the_fight():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Rat", tier="minion")
    apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Rat", "delta": -6, "_strike": True}], "rule", cfg)
    st = current_state(store, bid)
    ops = combat_ops(st, [])
    kinds = [o["op"] for o in ops]
    assert kinds == ["combatant_defeat", "award_exp", "combat_end"]
    assert ops[1]["amount"] == THREAT_XP["minion"]
    r = apply_delta(store, sid, bid, 2, ops, "rule", cfg)
    assert len(r.applied) == 3
    st2 = current_state(store, bid)
    assert st2["player"]["kael"]["xp"] == THREAT_XP["minion"]
    assert not st2["combat"]["active"] and not st2["combat"]["combatants"]
    h = st2["combat"]["history"][-1]
    assert h["defeated"] == ["Rat"] and h["outcome"] == "victory"


def test_referee_autospawns_hostiles_and_enlists_friends_on_combat_phase():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "affinity_adj", "target": "Vex", "delta": -12, "reason": "he drew steel"},
        {"op": "affinity_adj", "target": "Suki", "delta": 15, "reason": "a"}],
        "extraction", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "affinity_adj", "target": "Suki", "delta": 15, "reason": "b"}],
        "extraction", cfg)
    apply_delta(store, sid, bid, 3, [
        {"op": "affinity_adj", "target": "Suki", "delta": 15, "reason": "c"},
        {"op": "scene_set", "location": "alley", "phase": "ambush"}], "extraction", cfg)
    st = current_state(store, bid)
    ops = combat_ops(st, [])
    spawns = {(o["name"], o["side"]) for o in ops if o["op"] == "combatant_spawn"}
    assert ("Vex", "enemy") in spawns                    # present hostile enlists as foe
    assert ("Suki", "ally") in spawns                    # Ally-tier friend fights beside you
    assert ("Mira", "ally") not in spawns                # absent: no basis, no slot


def test_referee_ends_combat_when_the_scene_moves_on():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    ops = combat_ops(st, [{"op": "scene_set", "location": "inn", "phase": "lull"}])
    assert [o["op"] for o in ops][-1] == "combat_end"
    assert ops[-1]["outcome"] == "resolved"


# ------------------------------ wound persistence (ratified: FULL) ---------------------
def test_tracked_wounds_persist_and_reload_on_respawn():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Suki", side="ally", tier="standard", char="Suki")
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Suki", "delta": -11, "_strike": True}],
        "rule", cfg)
    apply_delta(store, sid, bid, 3, [{"op": "combat_end"}], "user", cfg)
    st = current_state(store, bid)
    assert st["attributes"]["suki"]["hp"] == {"cur": 3, "max": 14}   # the toll is REAL
    assert "wounded" in st["effects"]["suki"]            # below half: visibly Wounded
    assert not st["combat"]["combatants"]                # extras + rows cleared
    _spawn(store, sid, bid, 4, cfg, name="Suki", side="ally", tier="standard", char="Suki")
    st2 = current_state(store, bid)
    row = next(r for r in st2["combat"]["combatants"].values() if r["eid"] == "suki")
    assert row["hp"] == {"cur": 3, "max": 14}            # wounds carry into the next fight


# ------------------------------ frozen loot ---------------------------------------------
def test_creator_frozen_loot_table_wins_over_registry_floor():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "loot_table", "tier": "minion",
         "entries": [{"name": "Spent Chit", "qty_min": 2, "qty_max": 2, "chance": 1.0}]}],
        "user", cfg)
    _spawn(store, sid, bid, 2, cfg, name="Rat", tier="minion")
    st = current_state(store, bid)
    row = next(iter(st["combat"]["combatants"].values()))
    assert row["loot"] == [{"name": "Spent Chit", "qty_min": 2, "qty_max": 2, "chance": 1.0}]
    apply_delta(store, sid, bid, 3, [
        {"op": "combatant_hp", "target": "Rat", "delta": -6, "_strike": True}], "rule", cfg)
    st = current_state(store, bid)
    r = apply_delta(store, sid, bid, 3, combat_ops(st, []), "rule", cfg)
    assert r.applied
    st2 = current_state(store, bid)
    drops = [it for it in st2["items"].values() if it["name"] == "Spent Chit"]
    assert drops and drops[0]["qty"] == 2 and drops[0]["loc"] == "world"
    assert "Spent Chit" in st2["combat"]["history"][-1]["loot"]


def test_creator_norm_loot_and_world_to_ops_freeze():
    from aetherstate import creator
    w = {"name": "W", "genre": "cyberpunk",
         "loot": {"minion": [{"name": "cred chip", "chance": 0.5}, "burner phone"],
                  "weird": [{"name": "x"}]}}
    ops = creator.world_to_ops(w)
    lt = [o for o in ops if o["op"] == "loot_table"]
    assert len(lt) == 1 and lt[0]["tier"] == "minion"
    names = [e["name"] for e in lt[0]["entries"]]
    assert names == ["cred chip", "burner phone"]


# ------------------------------ the directive surfaces ---------------------------------
def test_directive_renders_war_board_ally_die_and_strike_clause():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="elite", armament="sabre")
    _spawn(store, sid, bid, 1, cfg, name="Suki", side="ally", tier="standard", char="Suki")
    st = current_state(store, bid)
    st["_fresh_checks"] = [{"op": "check", "skill": "melee", "result": 10,
                            "tier": "success", "_target": "Bandit", "_dmg": 2}]
    d1 = _render_directive(st, cfg)
    d2 = _render_directive(st, cfg)
    assert d1 == d2                                       # deterministic, no journal row
    assert "[WAR]" in d1 and f"Bandit {THREAT_HP['elite']}/{THREAT_HP['elite']}" in d1
    assert "(sabre)" in d1 and "[elite]" in d1
    assert "[ALLY] Suki's action" in d1 and "[OPPOSITION]" in d1
    assert "lands on Bandit for 2 damage" in d1
    header = render_header(st, cfg)
    assert "[WAR]" in header                              # rides the volatile tail


def test_war_room_knob_off_hides_the_board():
    cfg = _rpg_cfg()
    cfg.specialization.war_room = False
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    d = _render_directive(st, cfg)
    assert "[WAR]" not in d and "[ALLY]" not in d
    assert "WAR ROOM" not in rules_contract(cfg)


# ------------------------------ the linter net ------------------------------------------
def test_combatant_alive_flags_prose_kills_of_live_rows():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    v = lint_run(st, "One thrust and the Bandit drops dead in the gutter.", cfg,
                 turn=2, user_name="Kael")
    assert any(x.rule == "combatant_alive" for x in v)
    apply_delta(store, sid, bid, 2, [
        {"op": "combatant_defeat", "target": "Bandit"}], "rule", cfg)
    st2 = current_state(store, bid)
    v2 = lint_run(st2, "One thrust and the Bandit drops dead in the gutter.", cfg,
                  turn=2, user_name="Kael")
    assert not any(x.rule == "combatant_alive" for x in v2)   # ledger agrees: no lie


# ------------------------------ HUD / summary surfaces ---------------------------------
def test_hud_war_room_payload_exact_hp_and_dice():
    from aetherstate.hud import hud_view
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard", armament="knife")
    _spawn(store, sid, bid, 1, cfg, name="Suki", side="ally", char="Suki")
    st = current_state(store, bid)
    w = hud_view(st, cfg)["war_room"]
    assert w["active"] and w["round"] >= 1
    foe = next(c for c in w["combatants"] if c["side"] == "enemy")
    ally = next(c for c in w["combatants"] if c["side"] == "ally")
    assert foe["hp"] == {"cur": 14, "max": 14} and foe["armament"] == "knife"
    assert foe["die"]["tier"] in ("MISSES", "GRAZES", "HITS", "CRITS")
    assert ally["die"]["total"] and ally["kind"] == "tracked"
    assert "combat" in state_summary(st) and "clashes" in state_summary(st)


# ------------------------------ OOC surface ---------------------------------------------
def test_ooc_foe_and_combat_end_commands():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user",
                         "content": "((aether.foe Dock Thug minion rusty pipe)) go."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg)
    spawns = [o for o in res.user_ops if o["op"] == "combatant_spawn"]
    assert spawns == [{"op": "combatant_spawn", "name": "Dock Thug", "side": "enemy",
                       "tier": "minion", "armament": "rusty pipe"}]
    doc2 = {"messages": [{"role": "user", "content": "((aether.ally Suki)) with me!"}]}
    res2 = tier0.run(doc2, "new_turn", False, st, cfg)
    ally = [o for o in res2.user_ops if o["op"] == "combatant_spawn"]
    assert ally and ally[0]["side"] == "ally" and ally[0]["char"] == "suki"
    doc3 = {"messages": [{"role": "user", "content": "((aether.combat end)) enough."}]}
    res3 = tier0.run(doc3, "new_turn", False, st, cfg)
    assert {"op": "combat_end", "outcome": "called"} in res3.user_ops


# ------------------------------ none-leak (RPG invariant 1) -----------------------------
def test_none_session_carries_no_combat_fingerprint():
    cfg = Config()                                        # specialization = none
    assert "combat" not in empty_state()                  # checkpoints byte-identical
    assert "clash_record" not in json.dumps(delta_json_schema(rpg=False))
    assert "clash_record" not in system_prompt(4, rpg=False)
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="none-leak")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Tam"}], "user", cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I attack the bandit! [foe | Thug]"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg)
    assert not res.user_ops or all(o["op"] != "combatant_spawn" for o in res.user_ops)
    assert not res.rule_ops or all("combatant" not in o["op"] for o in res.rule_ops)
    assert "[WAR]" not in render_header(st, cfg)
    assert "[INIT]" not in render_header(st, cfg)          # initiative is rpg-only (2026-07-10)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "combatant_spawn", "name": "Thug", "side": "enemy"}], "extraction", cfg)
    assert not r.applied                                  # privileged even under none


# ------------------------------ replay determinism --------------------------------------
def test_p12_journal_replays_deterministically():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "loot_table", "tier": "standard",
         "entries": [{"name": "coin", "chance": 1.0}]}], "user", cfg)
    _spawn(store, sid, bid, 2, cfg, name="Bandit", tier="standard")
    _spawn(store, sid, bid, 2, cfg, name="Suki", side="ally", char="Suki")
    apply_delta(store, sid, bid, 3, [
        {"op": "combatant_hp", "target": "Bandit", "delta": -14, "_strike": True}],
        "rule", cfg)
    st = current_state(store, bid)
    apply_delta(store, sid, bid, 3, combat_ops(st, []), "rule", cfg)
    s1 = current_state(store, bid)
    s2 = current_state(store, bid)                        # fresh replay of the journal
    assert s1 == s2
    assert s1["combat"]["history"][-1]["defeated"] == ["Bandit"]
    assert any(it["name"] == "coin" for it in s1["items"].values())
    assert s1["player"]["kael"]["xp"] == THREAT_XP["standard"]
