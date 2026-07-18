"""RPG-5: the recording gaps (playtest 2026-07-06 G1-G8) + the doc-10 progression capstone.

Covers: the R10 world-tag protocol (scene/item/quest/affinity/hp), the organic item
channel (template floor, mechanics-free ceiling, stack-not-dupe), the quest ledger
family, the bounded HP consequence channel, code-awarded XP/level-ups, mastery ticks
with the scene cap + bracket bonus, resource costs on checks, crit-fail consequences,
scope overreach hard-fail, defeat resolution (+ hardcore death), and — per the RPG
invariants — `none`-leak guards and deterministic replay.
"""
from __future__ import annotations

import json
import random

from aetherstate import registry, tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.extraction import delta_json_schema
from aetherstate.prompts import rules_contract, system_prompt
from aetherstate.state import (MASTERY_SCENE_CAP, XP_AWARDS, apply_delta,
                               authority_violation, current_state, empty_state,
                               mastery_bracket, progression_ops, state_summary,
                               translate_path, validate_op, xp_level)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None, hp_cur=None):
    """Kael (player, arcane_gift, pools) + Mira + Suki (present) in an rpg session."""
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="rpg5")
    resources = {"hp": {"max": 20}, "stamina": {"max": 12}, "mana": {"max": 10}}
    if hp_cur is not None:
        resources["hp"] = {"cur": hp_cur, "max": 20}
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Mira"},
        {"op": "entity_add", "name": "Suki"},
        {"op": "presence", "entity": "Suki", "present": True},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"DEX": 14}, "skills": {"stealth": 3, "spellcraft": 1},
                  "abilities": ["arcane_gift"], "resources": resources}}],
        "genesis", cfg)
    return store, sid, bid


class _Rig:
    """Deterministic dice: every die comes up `value` (1 = crit_fail, sides = crit_success)."""

    def __init__(self, value=None, hi=False):
        self.value, self.hi = value, hi

    def randint(self, a, b):
        if self.value is not None:
            return self.value
        return b if self.hi else a


def _run_check(text, state, cfg, rng):
    doc = {"messages": [{"role": "user", "content": text}]}
    return tier0.run(doc, "new_turn", False, state, cfg, rng)


def _player_state(store, bid):
    return current_state(store, bid)


# ------------------------------ validation + authority --------------------------------
def test_rpg5_op_validation_shapes():
    assert validate_op({"op": "item_gain", "char": "k", "name": "rope"}) is not None
    assert validate_op({"op": "item_gain", "char": "k", "name": " "}) is None
    assert validate_op({"op": "item_gain", "char": "k", "name": "rope", "qty": 0}) is None
    assert validate_op({"op": "item_lose", "char": "k", "name": "rope"}) is not None
    assert validate_op({"op": "quest_add", "name": "Find the Killer",
                        "stakes": "serious"}) is not None
    assert validate_op({"op": "quest_add", "name": "x", "stakes": "huge"}) is None
    assert validate_op({"op": "quest_update", "quest": "x", "status": "complete"}) is not None
    assert validate_op({"op": "quest_update", "quest": "x", "status": "won"}) is None
    assert validate_op({"op": "quest_update", "quest": "x"}) is None   # must change something
    assert validate_op({"op": "quest_update", "quest": "x", "note": "lead"}) is not None
    assert validate_op({"op": "hp_adj", "char": "k", "delta": -4}) is not None
    assert validate_op({"op": "hp_adj", "char": "k", "delta": True}) is None
    assert validate_op({"op": "award_exp", "char": "k", "amount": -5}) is None
    assert validate_op({"op": "award_exp", "char": "k", "amount": 25}) is not None
    assert validate_op({"op": "master_tick", "char": "k", "skill": "stealth",
                        "amount": 3}) is not None
    assert validate_op({"op": "evolve_def", "char": "k", "table": "stats", "id": "s",
                        "def": {}}) is None
    assert validate_op({"op": "evolve_def", "char": "k", "table": "skills", "id": "s",
                        "def": {"name": "S"}}) is not None
    assert validate_op({"op": "defeat_resolve", "char": "k", "outcome": "captured"}) is not None
    assert validate_op({"op": "defeat_resolve", "char": "k", "outcome": "vaporized"}) is None


def test_rpg5_authority_matrix():
    cfg, st = _rpg_cfg(), empty_state()
    for kind, extra in (("award_exp", {"amount": 5}), ("level_up", {}),
                        ("master_tick", {"skill": "s", "amount": 1}),
                        ("evolve_def", {"table": "skills", "id": "s", "def": {}}),
                        ("defeat_resolve", {"outcome": "captured"})):
        op = {"op": kind, "char": "k", **extra}
        assert authority_violation(op, "extraction", st, cfg) is not None, kind
        for src in ("user", "genesis", "rule"):
            assert authority_violation(op, src, st, cfg) is None, (kind, src)
    for kind, extra in (("item_gain", {"name": "rope"}), ("item_lose", {"name": "rope"}),
                        ("hp_adj", {"delta": -3})):
        assert authority_violation({"op": kind, "char": "k", **extra},
                                   "extraction", st, cfg) is None, kind
    for op in ({"op": "quest_add", "name": "q"},
               {"op": "quest_update", "quest": "q", "status": "complete"}):
        assert authority_violation(op, "extraction", st, cfg) is None


# ------------------------------ the organic item channel (G2) -------------------------
def test_item_gain_template_floor_and_mechanics_free_ceiling():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Kael", "name": "Healing Draught"},
        {"op": "item_gain", "char": "Kael", "name": "Morwen's Slate"}], "extraction", cfg)
    assert len(r.applied) == 2
    st = current_state(store, bid)
    by_name = {it["name"]: it for it in st["items"].values()}
    assert by_name["Healing Draught"]["template_id"] == "healing_draught"   # curated floor
    assert by_name["Healing Draught"].get("on_consume")                     # mechanics baked
    assert by_name["Morwen's Slate"]["template_id"] is None                 # open ceiling:
    assert by_name["Morwen's Slate"]["mods_snapshot"] == {}                 # NO mechanics
    assert "[INVENTORY]" in render_header(st, cfg)


def test_item_gain_stacks_and_item_lose_checks_the_ledger():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    for t in (1, 2):
        apply_delta(store, sid, bid, t, [
            {"op": "item_gain", "char": "Kael", "name": "Rope"}], "extraction", cfg)
    st = current_state(store, bid)
    ropes = [it for it in st["items"].values() if it["name"] == "Rope"]
    assert len(ropes) == 1 and ropes[0]["qty"] == 2          # stack, never dupe
    r = apply_delta(store, sid, bid, 3, [
        {"op": "item_lose", "char": "Kael", "name": "Rope"}], "extraction", cfg)
    assert len(r.applied) == 1
    assert current_state(store, bid)["items"] and \
        [it for it in current_state(store, bid)["items"].values()
         if it["name"] == "Rope"][0]["qty"] == 1
    r = apply_delta(store, sid, bid, 4, [
        {"op": "item_lose", "char": "Kael", "name": "Crown of Wishes"}], "extraction", cfg)
    assert not r.applied and "not in" in r.quarantined[0]["reason"]   # ledger says no


# ------------------------------ the quest ledger (G3) ---------------------------------
def test_quest_ledger_add_update_render_and_unknown_reject():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "quest_add", "name": "Find the Killer", "stakes": "serious",
         "giver": "Mira", "detail": "Someone gutted Morwen on Deck 178."}],
        "extraction", cfg)
    st = current_state(store, bid)
    assert st["quests"]["find_the_killer"]["status"] == "active"
    assert "[QUEST] Find the Killer (serious)" in render_header(st, cfg)
    r = apply_delta(store, sid, bid, 2, [
        {"op": "quest_update", "quest": "Find the Killer", "status": "complete"}],
        "extraction", cfg)
    assert r.applied
    q = current_state(store, bid)["quests"]["find_the_killer"]
    assert q["status"] == "complete" and q["completed_turn"] == 2
    assert "COMPLETE" in render_header(current_state(store, bid), cfg)
    r = apply_delta(store, sid, bid, 3, [
        {"op": "quest_update", "quest": "Slay the Kraken", "status": "complete"}],
        "extraction", cfg)
    assert not r.applied and "unknown quest" in r.quarantined[0]["reason"]
    assert "quests" in state_summary(current_state(store, bid))


# ------------------------------ R10 world tags (G1/G4/G5/G7) --------------------------
def test_world_tags_scene_presence_item_quest_affinity_hp():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    text = ("The market swallows you. [scene | The Docks | rising | present: Mira]\n"
            "[item gained | {{user}} | Healing Draught]\n"
            "[quest | Find the Killer | new | Track the docks lead]\n"
            "[affinity | Mira | +40 | you saved her]\n"
            "[hp | {{user}} | -50 | shrapnel]")
    ops = tier0._parse_world_tags(text, st)
    kinds = [o["op"] for o in ops]
    assert kinds.count("scene_set") == 1 and "item_gain" in kinds
    assert "quest_add" in kinds and "affinity_adj" in kinds and "hp_adj" in kinds
    scene = next(o for o in ops if o["op"] == "scene_set")
    assert scene["location"] == "The Docks" and scene["phase"] == "rising"
    pres = {(o["entity"], o["present"]) for o in ops if o["op"] == "presence"}
    assert ("Mira", True) in pres and ("suki", False) in pres    # cast REPLACES the stage
    r = apply_delta(store, sid, bid, 1, ops, "extraction", cfg)
    st = current_state(store, bid)
    assert st["scene"]["location_id"] == "the_docks"             # canonicalized + persisted
    assert st["entities"]["the_docks"]["kind"] == "location"
    assert st["affinity"]["kael->mira"]["value"] == 15           # clamp baked (±15)
    assert st["player"]["kael"]["hp"]["cur"] == 15               # ±max(5, max//4) clamp
    assert st["quests"]["find_the_killer"]["status"] == "active"
    assert any(it["name"] == "Healing Draught" for it in st["items"].values())
    # second pass: a known quest updates instead of re-adding
    ops2 = tier0._parse_world_tags("[quest | Find the Killer | complete]", st)
    assert ops2 == [{"op": "quest_update", "quest": "Find the Killer",
                     "status": "complete"}]
    assert r.applied  # sanity: the batch landed


def test_scene_tag_keeps_real_move_but_drops_time_of_day_from_phase():
    cfg = _rpg_cfg()
    store, _sid, bid = _seeded(cfg)
    state = current_state(store, bid)

    ops = tier0._parse_world_tags(
        "[time | +1] "
        "[scene | Council Public Works office | night | present: Kael, Mira]",
        state,
    )

    scene = next(op for op in ops if op["op"] == "scene_set")
    assert scene == {
        "op": "scene_set",
        "location": "Council Public Works office",
        "phase": "opening",
    }
    assert any(op["op"] == "time_advance" for op in ops)


def test_scene_tag_accepts_documented_dramatic_phases():
    cfg = _rpg_cfg()
    store, _sid, bid = _seeded(cfg)
    state = current_state(store, bid)

    for phase in ("opening", "setup", "rising", "climax", "lull"):
        ops = tier0._parse_world_tags(f"[scene | The Docks | {phase}]", state)
        assert ops[0]["phase"] == phase


def test_scene_tag_accepts_explicit_phase_field_label():
    cfg = _rpg_cfg()
    store, _sid, bid = _seeded(cfg)
    state = current_state(store, bid)

    ops = tier0._parse_world_tags(
        "[scene | Lower Siltwright Bend | phase rising | present: Kael]",
        state,
    )

    assert ops[0] == {
        "op": "scene_set",
        "location": "Lower Siltwright Bend",
        "phase": "rising",
    }


def test_world_tags_inert_under_none():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="none5")
    doc = {"messages": [
        {"role": "assistant", "content": "[item gained | Kael | Sword] [scene | Docks]"},
        {"role": "user", "content": "hello"}]}
    res = tier0.run(doc, "new_turn", False, empty_state(), cfg, random.Random(7))
    assert res.proposal_ops == []                                # no fingerprint under none
    assert "item_gain" not in system_prompt(2, rpg=False)
    assert "quest_add" not in json.dumps(delta_json_schema(False))
    assert translate_path("quest.x", "complete", rpg=False) is None
    assert "item_gain" in system_prompt(2, rpg=True)
    assert "[quest |" in rules_contract(_rpg_cfg())


# ------------------------------ progression: XP / levels ------------------------------
def test_xp_curve_and_quest_completion_awards():
    assert xp_level(0) == 1 and xp_level(99) == 1 and xp_level(100) == 2
    assert xp_level(300) == 3 and xp_level(599) == 3 and xp_level(600) == 4
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "quest_add", "name": "Find the Killer", "stakes": "serious"}],
        "extraction", cfg)
    r = apply_delta(store, sid, bid, 2, [
        {"op": "quest_update", "quest": "Find the Killer", "status": "complete"}],
        "extraction", cfg)
    pro = progression_ops(current_state(store, bid), r.applied)
    awards = [o for o in pro if o["op"] == "award_exp"]
    assert awards and awards[0]["amount"] == XP_AWARDS["quest_serious"]
    r2 = apply_delta(store, sid, bid, 2, pro, "rule", cfg)
    assert current_state(store, bid)["player"]["kael"]["xp"] == 75
    assert r2.applied


def test_level_up_grants_and_multi_threshold():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "award_exp", "char": "Kael", "amount": 120}], "rule", cfg)
    pro = progression_ops(current_state(store, bid), r.applied)
    lvls = [o for o in pro if o["op"] == "level_up"]
    assert len(lvls) == 1                                        # 120 XP -> level 2
    apply_delta(store, sid, bid, 1, pro, "rule", cfg)
    pl = current_state(store, bid)["player"]["kael"]
    assert pl["level"] == 2 and pl["hp"]["max"] == 24            # +4 HP baked grant
    assert pl["resources"]["stamina"]["max"] == 14               # +2 pools
    assert pl["stat_points"] == 1                                # banked, spendable later
    assert "stat pt unspent" in render_header(current_state(store, bid), cfg)


# ------------------------------ progression: mastery ----------------------------------
def test_mastery_ticks_scene_cap_bracket_bonus_and_bake():
    assert mastery_bracket(0) == ("Novice", 0) and mastery_bracket(10) == ("Adept", 1)
    assert mastery_bracket(60) == ("Master", 3) and mastery_bracket(120) == ("Grandmaster", 4)
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "master_tick", "char": "Kael", "skill": "stealth", "amount": 4},
        {"op": "master_tick", "char": "Kael", "skill": "stealth", "amount": 4}],
        "rule", cfg)
    pl = current_state(store, bid)["player"]["kael"]
    assert pl["mastery"]["stealth"] == MASTERY_SCENE_CAP        # scene cap: 4+4 -> 6
    assert len(r.applied) == 2
    r = apply_delta(store, sid, bid, 2, [
        {"op": "master_tick", "char": "Kael", "skill": "stealth", "amount": 4}], "rule", cfg)
    assert current_state(store, bid)["player"]["kael"]["mastery"]["stealth"] == 6  # same scene
    apply_delta(store, sid, bid, 3, [{"op": "scene_set", "location": "docks"}], "rule", cfg)
    r = apply_delta(store, sid, bid, 4, [
        {"op": "master_tick", "char": "Kael", "skill": "stealth", "amount": 4}], "rule", cfg)
    assert r.applied[0].get("_bracket_up") == "Adept"           # 6 + 4 = 10 crosses, baked
    pl = current_state(store, bid)["player"]["kael"]
    assert pl["mastery"]["stealth"] == 10
    reg = registry.load(cfg)
    base = reg.effective_mod({"skills": {"stealth": 3}, "stats": {}}, "stealth")
    bumped = reg.effective_mod({"skills": {"stealth": 3}, "stats": {},
                                "mastery": {"stealth": 10}}, "stealth")
    assert bumped == base + 1                                   # the curated bracket bump


def test_checks_emit_master_ticks_and_crit_fail_consequence():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = _player_state(store, bid)
    res = _run_check("((aether.check stealth))", st, cfg, _Rig(hi=True))   # crit_success
    kinds = [o["op"] for o in res.rule_ops]
    assert "check" in kinds and "master_tick" in kinds
    tick = next(o for o in res.rule_ops if o["op"] == "master_tick")
    assert tick["amount"] == 4 and tick["skill"] == "stealth"
    res = _run_check("((aether.check stealth))", st, cfg, _Rig(value=1))   # crit_fail
    ch = next(o for o in res.rule_ops if o["op"] == "check")
    assert ch["tier"] == "crit_fail"
    eff = [o for o in res.rule_ops if o["op"] == "effect_add"]
    assert eff and eff[0]["effect"] == "Strained"               # the consequence channel
    assert not [o for o in res.rule_ops if o["op"] == "master_tick"]   # crit_fail: no tick


def test_scope_overreach_hard_fails_and_backfires():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = _player_state(store, bid)
    res = _run_check("((aether.check spellcraft scope mythic))", st, cfg, _Rig(hi=True))
    ch = next(o for o in res.rule_ops if o["op"] == "check")
    assert ch["_scope_over"] >= 3 and ch["tier"] in ("fail", "crit_fail")   # outright fail
    assert any("fails outright" in n for n in res.notices)
    res = _run_check("((aether.check spellcraft scope mythic))", st, cfg, _Rig(value=1))
    eff = [o for o in res.rule_ops if o["op"] == "effect_add"]
    assert eff and eff[0]["effect"] == "Backlash"               # overreach bites back


# ------------------------------ progression: resources --------------------------------
def test_resource_cost_gate_and_charge():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = _player_state(store, bid)
    res = _run_check("((aether.check spellcraft))", st, cfg, _Rig(hi=True))
    ch = next(o for o in res.rule_ops if o["op"] == "check")
    assert ch["_cost"] == {"mana": 2}                           # frozen registry cost, baked
    apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    assert current_state(store, bid)["player"]["kael"]["resources"]["mana"]["cur"] == 8
    drained = json.loads(json.dumps(st))
    drained["player"]["kael"]["resources"]["mana"]["cur"] = 1
    res = _run_check("((aether.check spellcraft))", drained, cfg, _Rig(hi=True))
    assert not [o for o in res.rule_ops if o["op"] == "check"]  # cannot attempt: not a roll
    assert any("not enough mana" in n for n in res.notices)
    # a scene change recovers pools (curated regen — pure reducer)
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "canteen"}], "rule", cfg)
    assert current_state(store, bid)["player"]["kael"]["resources"]["mana"]["cur"] == 10


# ------------------------------ defeat & hardcore --------------------------------------
def test_hp_zero_triggers_defeat_and_hardcore_death():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg, hp_cur=3)
    apply_delta(store, sid, bid, 1, [
        {"op": "affinity_adj", "target": "Suki", "delta": 12}], "extraction", cfg)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "hp_adj", "char": "Kael", "delta": -5}], "extraction", cfg)
    st = current_state(store, bid)
    assert st["player"]["kael"]["hp"]["cur"] == 0
    pro = progression_ops(st, r.applied)
    d = [o for o in pro if o["op"] == "defeat_resolve"]
    assert d and d[0]["outcome"] == "rescued"                   # Suki present + warm
    r2 = apply_delta(store, sid, bid, 1, pro, "rule", cfg)
    pl = current_state(store, bid)["player"]["kael"]
    assert pl["defeated"]["outcome"] == "rescued" and pl["hp"]["cur"] == 5   # max // 4
    assert "battered" in current_state(store, bid)["effects"]["kael"]
    assert "DEFEATED" in render_header(current_state(store, bid), cfg)
    assert r2.applied
    # hardcore: same trigger routes to death and the ledger records it
    pro2 = progression_ops({**st, "player": {"kael": {**st["player"]["kael"],
                                                      "defeated": {}}}},
                           r.applied, hardcore=True)
    assert any(o["op"] == "defeat_resolve" and o["outcome"] == "death" for o in pro2)


def test_defeat_robbed_drops_carried_items():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Kael", "name": "Coin Purse"}], "extraction", cfg)
    r = apply_delta(store, sid, bid, 2, [
        {"op": "defeat_resolve", "char": "Kael", "outcome": "robbed"}], "rule", cfg)
    st = current_state(store, bid)
    purse = next(it for it in st["items"].values() if it["name"] == "Coin Purse")
    assert purse["loc"] == "world" and purse["owner"] is None   # consequences are state
    assert r.applied


# ------------------------------ 2026-07-07 live playtest fix pack ----------------------
def test_quest_near_dupe_merges_and_updates_resolve():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "quest_add",
         "name": "Pass the provisional hero license practical exam at Takoba Arena."}],
        "extraction", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "quest_add", "name": "Takoba Arena"}], "extraction", cfg)
    qs = current_state(store, bid)["quests"]
    assert len(qs) == 1                                          # merged, not duplicated
    r = apply_delta(store, sid, bid, 3, [
        {"op": "quest_update", "quest": "Takoba Arena", "status": "complete"}],
        "extraction", cfg)
    assert r.applied and list(current_state(store, bid)["quests"].values()
                              )[0]["status"] == "complete"


def test_tag_char_maps_player_name_tokens():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)                # player entity name is 'Kael'
    assert tier0._tag_char("{{user}}", st) == "kael"
    assert tier0._tag_char("Kael", st) == "kael"  # first-name token -> the player, never a twin
    assert tier0._tag_char("Mira", st) == "Mira"  # other names untouched


def test_discovery_known_names_include_tokens():
    from aetherstate import discovery
    st = {"entities": {"kaji_hoshino": {"name": "Kaji Hoshino", "aliases": []}}}
    known = discovery.known_names(st)
    assert "kaji" in known and "hoshino" in known and "kaji hoshino" in known


def test_heal_stray_quotes_salvages_glm_json():
    from aetherstate.assist import _json_or_none
    broken = ('```json\n{"name":"Musutafu","aspects":["The hero "Hawks" patrols the coast",'
              '"Quirks cost"],"tone":"heroic"}\n```\ntrailing commentary { not json }')
    doc = _json_or_none(broken)
    assert isinstance(doc, dict) and doc["tone"] == "heroic"
    assert any("Hawks" in a for a in doc["aspects"])


def test_memory_event_exact_dupe_guard():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="dupe")
    for t in (0, 1):
        apply_delta(store, sid, bid, t, [
            {"op": "memory_event", "text": "World lore: Quirks manifest by age four."}],
            "user", cfg)
    assert len(current_state(store, bid)["memories"]) == 1


def test_creator_defs_abilities_are_known_and_baseline_stats_spend():
    from aetherstate import creator
    doc = {"name": "Kaji", "skills": {"kintsugi_mend": 3, "jury_rig": 2},
           "abilities": [],                       # GLM listed the Quirk only under defs
           "custom": {"skills": [
               {"id": "kintsugi_mend", "name": "Kintsugi Mend", "keyed_stat": "CON"},
               {"id": "jury_rig", "name": "Jury-Rig", "keyed_stat": "CUN"}],
               "abilities": [{"id": "kintsugi", "name": "Kintsugi", "kind": "passive"}]}}
    p = creator.deterministic_player(doc, Config())
    assert "kintsugi" in p["abilities"]           # your own frozen ability is KNOWN
    assert p["stats"]["CON"] > 10 and p["stats"]["CUN"] > 10   # deterministic stat spend
    assert sum(p["stats"].values()) > 60          # more than all-baseline


# ------------------------------ replay determinism -------------------------------------
def test_rpg5_journal_replays_deterministically():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "quest_add", "name": "Q", "stakes": "epic"},
        {"op": "item_gain", "char": "Kael", "name": "Rope"},
        {"op": "hp_adj", "char": "Kael", "delta": -3},
        {"op": "affinity_adj", "target": "Mira", "delta": 9}], "extraction", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "award_exp", "char": "Kael", "amount": 120},
        {"op": "level_up", "char": "Kael"},
        {"op": "master_tick", "char": "Kael", "skill": "stealth", "amount": 5}], "rule", cfg)
    s1 = current_state(store, bid)
    s2 = current_state(store, bid)                               # fresh replay of the journal
    assert s1 == s2
    assert s1["player"]["kael"]["level"] == 2
    assert s1["player"]["kael"]["mastery"]["stealth"] == 5
