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


def test_targeted_observation_check_never_becomes_a_strike_without_offensive_intent():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": (
        "((aether.check perception at Bandit)) I hold position and watch him commit."
    )}]}

    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))

    checks = [op for op in res.rule_ops if op.get("op") == "check"]
    assert checks and checks[0]["skill"] == "perception"
    assert "_target" not in checks[0] and "_dmg" not in checks[0]
    assert not any(op.get("op") == "combatant_hp" for op in res.rule_ops)


def test_nl_attack_with_unresolved_pronoun_drops_the_partial_weapon_check():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I strike with stealth at his back."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(2))    # 2d6=4(+mods) -> miss-ish
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert checks == []                              # no frozen target -> no partial roll/cost leak
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
    frame = next(o["frame"] for o in res.rule_ops if o.get("op") == "semantic_frame_commit")
    assert frame["target_entity_id"] == "hollow_revenant"
    assert frame["target_name"] == "Hollow Revenant"
    assert "occurrence.target_unbound" not in frame["ambiguity"]
    strikes = [o for o in res.rule_ops if o.get("op") == "combatant_hp"]
    assert strikes and strikes[0]["target"] == "hollow_revenant" and strikes[0]["_strike"]
    assert any(o.get("op") == "scene_set" and o.get("phase") == "climax" for o in res.rule_ops)
    r = apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)  # spawn (2) before hp (6)
    row = r.state["combat"]["combatants"]["hollow_revenant"]
    assert row["hp"]["cur"] < row["hp"]["max"]                      # the opening blow LANDED
    assert r.state["scene"]["phase"] == "climax"


def test_floor_world_target_bridge_rejects_negated_and_quoted_attacks():
    assistant = "A hollow revenant drags itself out of the mere."
    for action in (
        "((aether.check melee)) I do not cut into the revenant.",
        'I say, "I cut into the revenant."',
    ):
        cfg = _rpg_cfg()
        store, _sid, bid = _seeded(cfg)
        result = tier0.run(
            {"messages": [
                {"role": "assistant", "content": assistant},
                {"role": "user", "content": action},
            ]},
            "new_turn",
            False,
            current_state(store, bid),
            cfg,
            _Rig(5),
        )

        assert not any(op.get("op") == "combatant_spawn" for op in result.rule_ops)
        assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


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
    ops = combat_ops(st, [{"op": "scene_set", "location": "inn", "phase": "lull",
                           "_prev_loc": "alley"}])
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
    assert drops[0]["dropped_by"] == "rat"
    assert drops[0]["dropped_by_name"] == "Rat"
    assert drops[0]["world_origin_scene"] == st2["scene"].get("scene_index", 0)
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
    opp = tier0._opposition_op(st, cfg, turn=2)
    assert opp is not None
    hit = apply_delta(store, sid, bid, 2, [opp], "rule", cfg)
    follow = combat_ops(hit.state, hit.applied)
    if follow:
        apply_delta(store, sid, bid, 2, follow, "rule", cfg)
    st = current_state(store, bid)
    st["_fresh_checks"] = [{"op": "check", "skill": "melee", "result": 10,
                            "tier": "success", "_target": "Bandit", "_dmg": 2}]
    d1 = _render_directive(st, cfg)
    d2 = _render_directive(st, cfg)
    assert d1 == d2                                       # deterministic, no journal row
    assert "[WAR]" in d1 and f"Bandit {THREAT_HP['elite']}/{THREAT_HP['elite']}" in d1
    assert "(sabre)" in d1 and "[elite]" in d1
    assert "[ALLY] Suki's action" in d1 and "[ENEMY ACTION enemy-action/1]" in d1
    assert "[ENEMY INTENT enemy-intent/1]" in d1
    assert "lands on Bandit for 2 damage" in d1
    header = render_header(st, cfg)
    assert "[WAR]" in header                              # rides the volatile tail


def test_strike_directive_says_current_war_hp_already_includes_damage():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Bandit", "delta": -2, "_strike": True},
    ], "rule", cfg)
    st = current_state(store, bid)
    st["meta"]["turn"] = 2
    st["_fresh_checks"] = [{"op": "check", "skill": "swordplay", "tier": "success",
                            "_target": "Bandit", "_dmg": 2}]

    directive = _render_directive(st, cfg)

    assert "current [WAR] HP" in directive
    assert "ALREADY INCLUDES this hit" in directive
    assert "do not subtract it again or emit another [hp] tag" in directive


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
def test_hud_war_room_payload_exact_hp_ally_die_and_enemy_intent():
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
    assert "die" not in foe
    assert w["intent"]["actor"] == foe["cid"]
    assert all(w["intent"].get(k) for k in
               ("target", "move_name", "danger", "tell", "counterplay"))
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



# ---------------- 3v3 party formation (2026-07-10, Bean: "3v3 is missing") --------------
def test_companion_role_enlists_without_high_affinity():
    """The floor used to enlist a present friend ONLY at affinity >= 40 (the Ally tier), which
    rarely climbs that high in play, so the party side never formed. Now an authored companion-
    class role (or a soulmate / close relationship dim) grounds a comrade at near-zero affinity:
    Mira has a 'loyal companion' role and no accrued standing, yet still stands with the Player
    when violence breaks out — the bond IS the basis (pillars 4/6)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role", "value": "loyal companion"},
        {"op": "scene_set", "location": "alley", "phase": "ambush"}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")   # a fight is underway
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") in spawns                     # companion role -> ally, affinity ~0
    assert ("Suki", "ally") not in spawns                 # present but no bond: not conscripted


def test_soulmate_enlists_and_a_stranger_does_not():
    """Two edges of the grounded floor: a soulmate always stands with the Player; a merely
    present neutral stranger has no in-world basis and does NOT get conscripted."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_soulmate", "target": "Suki"},         # Suki: bonded (Suki is present)
        {"op": "scene_set", "location": "alley", "phase": "ambush"}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")   # a fight is underway
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Suki", "ally") in spawns                     # the soulmate fights beside you
    assert ("Mira", "ally") not in spawns                 # neutral stranger: no basis, no slot


def test_dm_ally_tag_spawns_tracked_and_extra():
    """The symmetric channel the DM never had: [ally | name | tier? | weapon?] brings a present
    companion onto the Player's side, exactly like [foe] does for the enemy side. A known cast
    member is TRACKED (wounds persist); an unknown name is a procedural extra ally."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_foe_tags(
        "You are not alone. [ally | Suki | standard | twin daggers]\n"
        "[ally | Hired Blade | minion]", st)
    a = {o["name"]: o for o in ops}
    assert a["Suki"]["side"] == "ally" and a["Suki"]["char"] == "suki"     # known -> tracked
    assert a["Suki"]["armament"] == "twin daggers" and a["Suki"]["tier"] == "standard"
    assert a["Hired Blade"]["side"] == "ally" and "char" not in a["Hired Blade"]   # -> extra
    r = apply_delta(store, sid, bid, 1, ops, "rule", cfg)
    assert r.applied
    sides = {row["name"]: row["side"]
             for row in current_state(store, bid)["combat"]["combatants"].values()}
    assert sides["Suki"] == "ally" and sides["Hired Blade"] == "ally"


def test_foe_and_ally_tags_coexist_in_one_reply():
    """A single DM reply can stand both sides up: some [foe] tags and an [ally] tag parse into
    combatant_spawns on the correct sides, and the [foe] path stays byte-identical to before."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_foe_tags(
        "[foe | Dock Thug | minion | pipe]\n[ally | Suki]\n[foe | Vex | elite]", st)
    by_side = {}
    for o in ops:
        by_side.setdefault(o["side"], []).append(o["name"])
    assert by_side["enemy"] == ["Dock Thug", "Vex"]       # foe order preserved, enemy side
    assert by_side["ally"] == ["Suki"]                    # the ally rides the same parser
    assert next(o for o in ops if o["name"] == "Vex")["char"] == "vex"   # known -> tracked


def test_floor_stages_the_named_band():
    """Attacking into a group the DM's prose NUMBERS ('three cutthroats') stages the whole band,
    capped at the 3v3 enemy side, not just the one the Player struck (Bean 2026-07-10)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant",
         "content": "Three Dockside cutthroats fan out across the wharf, blades held low."},
        {"role": "user", "content": "((aether.check melee)) I cut into the nearest cutthroat."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    spawns = [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]
    assert len(spawns) == 3 and all(s["side"] == "enemy" for s in spawns)   # the whole band
    assert len({s["_cid"] for s in spawns}) == 3          # three distinct rows
    r = apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    foes = [row for row in r.state["combat"]["combatants"].values() if row["side"] == "enemy"]
    assert len(foes) == 3
    from aetherstate.hud import hud_view

    war = hud_view(r.state, cfg)["war_room"]
    assert war["active"] and war["round"] >= 1
    assert len([row for row in war["combatants"] if row["side"] == "enemy"]) == 3
    from aetherstate.narrator_realization import build_narrator_realization_from_state

    packet = build_narrator_realization_from_state(r.state)
    assert packet is not None
    assert packet["asserted_settled"][0]["settled_change_kinds"].count(
        "target_admission"
    ) == 1


def test_lone_foe_prose_stages_exactly_one():
    """No count word -> the floor still stages exactly one foe (the group path is opt-in on an
    explicit number in the fiction; a solo enemy never balloons into a phantom crowd)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant", "content": "A lone Dockside cutthroat steps from the shadow."},
        {"role": "user", "content": "((aether.check melee)) I cut into the cutthroat."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    spawns = [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]
    assert len(spawns) == 1


def test_contract_teaches_ally_and_exact_enemy_intent_action_channels():
    """Full and compact contracts teach the future-intent/exact-action boundary."""
    cfg = _rpg_cfg()
    full = rules_contract(cfg)
    assert "[ally" in full                                # full contract teaches the party
    assert "engine enemy-intent header" in full and "frozen FUTURE move" in full
    assert "engine enemy-action header" in full and "exact committed move" in full
    assert "INPUT ONLY" in full and "In that same reply" in full
    assert "HUD exposes it before the Player acts" in full
    assert "[ENEMY INTENT]" not in full and "[ENEMY ACTION]" not in full
    assert "actor, target, delivery" in full and "counterplay" in full
    assert "never substitute another attack, status, spell, target, effect" in full
    cfg.specialization.contract = "compact"
    compact = rules_contract(cfg)
    assert "[ally" in compact                             # compact contract too
    assert "engine enemy-intent header" in compact
    assert "engine enemy-action header" in compact
    assert "INPUT ONLY" in compact and "in that same reply" in compact
    assert "HUD exposes it before the Player acts" in compact
    assert "[ENEMY INTENT]" not in compact and "[ENEMY ACTION]" not in compact
    assert "actor, target, delivery" in compact and "counterplay" in compact
    cfg.specialization.contract = "full"
    cfg.specialization.war_room = False
    off = rules_contract(cfg)
    assert "[ally" not in off                             # gated with the board
    assert "enemy-intent header" not in off and "enemy-action header" not in off


def test_ooc_ally_recruit_resolves_a_present_companion():
    """The player's own recruit path, surfaced in the HUD: ((aether.ally <name>)) stages a
    present companion on the Player's side, name resolved to the known entity."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "((aether.ally Suki)) to me!"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg)
    ally = [o for o in res.user_ops if o["op"] == "combatant_spawn"]
    assert ally and ally[0]["side"] == "ally" and ally[0]["char"] == "suki"


def test_none_session_ignores_the_ally_tag_and_channel():
    """RPG invariant 1: an [ally] tag leaves no fingerprint under `none` — no spawn, and the
    channel never rides a non-rpg wire."""
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="none-ally")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Tam"}], "user", cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "[ally | Tam] ((aether.ally Tam)) fight!"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg)
    assert all(o.get("op") != "combatant_spawn" for o in (res.user_ops + res.rule_ops))
    assert "[ally" not in system_prompt(4, rpg=False)     # rpg-only, like [foe]


def test_party_spawns_replay_deterministically():
    """Both sides staged via the DM's tags replay to the identical ledger (baked at apply)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_foe_tags("[foe | Bandit | standard]\n[ally | Suki | standard]", st)
    apply_delta(store, sid, bid, 1, ops, "rule", cfg)
    s1 = current_state(store, bid)
    s2 = current_state(store, bid)                         # fresh replay of the journal
    assert s1 == s2
    sides = sorted((r["name"], r["side"]) for r in s1["combat"]["combatants"].values())
    assert ("Bandit", "enemy") in sides and ("Suki", "ally") in sides



# --------- common-enemy allies (2026-07-10, Bean's caravan-ambush example) -------------
def test_escort_enlists_on_common_enemy_without_bond():
    """Bean's caravan: hired escorts with NO personal bond (neutral affinity) fight the
    ambushers beside the Player because they share the enemy and are not hostile to each other.
    A guard/mercenary/escort role is the grounded 'on your side of this fight' basis."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role", "value": "hired caravan guard"}],
        "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")   # ambush: a foe is up
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") in spawns                     # the escort fights the common enemy
    assert ("Suki", "ally") not in spawns                 # present but no tie: not conscripted


def test_hostile_guard_never_enlists_as_ally():
    """Bean's second question ('are they hostile to each other?'): a guard who is HOSTILE to the
    Player is a foe, not an ally — a shared battlefield never overrides enmity."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role", "value": "turncoat guard"}],
        "user", cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "affinity_adj", "target": "Mira", "delta": -15, "reason": "she turned on us"}],
        "extraction", cfg)
    _spawn(store, sid, bid, 2, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    assert st["affinity"]["kael->mira"]["value"] <= -10
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") not in spawns                 # enmity wins over the shared fight


def test_authored_hostile_guard_never_enlists_as_ally_without_an_affinity_row():
    """Creator-authored hostility is already a grounded enemy label; it needs no later affinity."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role",
         "value": "hostile windlass-crossbow guard"}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, [])
              if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") not in spawns


def test_bare_sentry_role_is_not_mistaken_for_a_player_summon():
    """A mundane sentry is an occupation, not evidence that the Player created the NPC."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role",
         "value": "windlass-crossbow sentry"}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, [])
              if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") not in spawns


def test_neutral_bystander_without_tie_is_not_conscripted():
    """Grounding (pillar 4): a present neutral NPC with no bond, no martial/escort role, and no
    allied faction is NOT dragged into the fight — presence alone is never a basis."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role", "value": "fruit vendor"}],
        "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") not in spawns


def test_shared_faction_enlists_against_the_common_enemy():
    """An NPC in the Player's own faction stands with them when the shared enemy attacks — the
    enemy-of-my-enemy read, grounded on the faction ledger rather than a personal bond."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Kael", "key": "faction", "value": "Wardens"},
        {"op": "set_attribute", "entity": "Mira", "key": "faction", "value": "Wardens"}],
        "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Mira", "ally") in spawns


def test_common_enemy_enlist_needs_an_active_fight():
    """The shared enemy IS the basis: with no foe on the field, an escort standing around is not
    auto-spawned into a phantom combat (the referee only enlists when a fight is real)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "set_attribute", "entity": "Mira", "key": "role", "value": "caravan escort"}],
        "user", cfg)
    st = current_state(store, bid)                        # no foe, no combat
    spawns = [o for o in combat_ops(st, []) if o["op"] == "combatant_spawn"]
    assert spawns == []                                   # nobody enlists without a fight



# --------- player summons / conjurations / creations (2026-07-10, Bean: "VERY important") ----
def test_player_summon_enlists_as_ally():
    """A thing the Player summons/conjures/creates fights on their side by construction. A summon
    whose 'summoner' attribute is the Player enlists automatically when the fight is on."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "entity_add", "name": "Spectral Wolf"},
        {"op": "presence", "entity": "Spectral Wolf", "present": True},
        {"op": "set_attribute", "entity": "Spectral Wolf", "key": "summoner", "value": "Kael"}],
        "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Spectral Wolf", "ally") in spawns


def test_summon_typed_creature_enlists_when_not_hostile():
    """A summon-TYPED non-hostile creature near the Player reads as a friendly conjuration and
    stands with them (weak-model floor: no explicit ownership needed if it isn't your enemy)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "entity_add", "name": "Flame Wisp"},
        {"op": "presence", "entity": "Flame Wisp", "present": True},
        {"op": "set_attribute", "entity": "Flame Wisp", "key": "type", "value": "conjured elemental"}],
        "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Flame Wisp", "ally") in spawns


def test_wild_hostile_summon_is_not_your_ally():
    """A summon-typed entity HOSTILE to the Player (an enemy caster's construct) is a foe, not
    your ally — the non-hostile gate holds for the summon basis too."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "entity_add", "name": "Bone Golem"},
        {"op": "presence", "entity": "Bone Golem", "present": True},
        {"op": "set_attribute", "entity": "Bone Golem", "key": "type", "value": "construct"}],
        "user", cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "affinity_adj", "target": "Bone Golem", "delta": -15, "reason": "it lunges at us"}],
        "extraction", cfg)
    _spawn(store, sid, bid, 2, cfg, name="Bandit", tier="standard")
    st = current_state(store, bid)
    spawns = {(o["name"], o["side"]) for o in combat_ops(st, []) if o["op"] == "combatant_spawn"}
    assert ("Bone Golem", "ally") not in spawns
