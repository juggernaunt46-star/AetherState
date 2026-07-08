"""P7 (Bean 2026-07-07) — the gear/inventory CLASS split, worn-at-start auto-equip, and
live_recalc newest-reply ingestion.

Covers: classify_item (pure), item_gain auto-equip onto the paper-doll, the [GEAR]/[INVENTORY]
render split by class (not by slot), the cold-path world-tag ingestion of the FRESH reply, and
source-scoped swipe retraction (the resolved check survives). Every group carries a none-leak or
deterministic-replay guard (RPG invariant 1/4).
"""
from __future__ import annotations

import json

from aetherstate import tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline, PostContext
from aetherstate.session_engine import SessionEngine
from aetherstate.state import (apply_delta, classify_item, current_state, empty_state,
                               item_is_gear, reduce_state)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seed_player(cfg, hp_max=30):
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p7")
    card = {"stats": {"DEX": 14}, "skills": {"stealth": 3}, "resources": {"hp": {"max": hp_max}}}
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Rune", "kind": "player"},
                                     {"op": "player_seed", "entity": "Rune", "card": card}],
                "genesis", cfg)
    return store, sid, bid


def _json_reply(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


# ------------------------------ classify_item (pure) --------------------------------
def test_classify_item_gear_vs_inventory():
    assert classify_item("Iron Longsword")["class"] == "gear"
    assert classify_item("Iron Longsword")["slot"] == "mainhand"
    assert classify_item("worn leather satchel")["slot"] == "back"
    assert classify_item("battered iron helm")["slot"] == "head"
    assert classify_item("steel cuirass")["slot"] == "body"
    assert classify_item("set of lockpicks")["class"] == "gear"      # a tool: gear, no body slot
    assert classify_item("set of lockpicks")["slot"] is None
    for nm in ("health potion", "smartphone", "vial of perfume", "iron ore", "ration pack"):
        assert classify_item(nm)["class"] == "inv", nm
    # a curated template snapshot wins over the name heuristic
    assert classify_item("x", {"worn": True, "slot": "head", "type": "armor"})["slot"] == "head"
    assert classify_item("x", {"on_consume": {"heal": 8}})["class"] == "inv"


# ------------------------------ worn-at-start auto-equip ----------------------------
def test_starting_worn_gear_auto_equips_to_paperdoll():
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Rune", "name": "battered iron helm"},
        {"op": "item_gain", "char": "Rune", "name": "long steel sword"},
        {"op": "item_gain", "char": "Rune", "name": "health potion"},
        {"op": "item_gain", "char": "Rune", "name": "set of lockpicks"}], "user", cfg)
    st = current_state(store, bid)
    gear = st["gear"]["rune"]
    assert st["items"][gear["head"]]["name"] == "battered iron helm"   # WORN -> paper-doll
    assert gear.get("mainhand")                                        # the sword equipped
    lines = render_header(st, cfg).split("\n")
    gear_line = next(x for x in lines if x.startswith("[GEAR]"))
    inv_line = next(x for x in lines if x.startswith("[INVENTORY]"))
    assert "head=battered iron helm" in gear_line
    assert "stowed: set of lockpicks" in gear_line                    # tool = gear, carried
    assert "health potion" in inv_line and "set of lockpicks" not in inv_line


def test_item_is_gear_helper_and_render_split_by_class():
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Rune", "name": "healing salve"},
        {"op": "item_gain", "char": "Rune", "name": "climbing rope"}], "user", cfg)
    st = current_state(store, bid)
    by_name = {it["name"]: it for it in st["items"].values()}
    assert item_is_gear(by_name["climbing rope"]) is True             # rope: gear
    assert item_is_gear(by_name["healing salve"]) is False            # salve: inventory


def test_auto_equip_is_replay_pure():
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Rune", "name": "iron helm"},
        {"op": "item_gain", "char": "Rune", "name": "health potion"}], "user", cfg)
    live = current_state(store, bid)
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay["gear"] == live["gear"] and replay["items"] == live["items"]
    assert replay["inventory"] == live["inventory"]


# ------------------------------ live_recalc: newest reply ---------------------------
def test_live_recalc_ingests_fresh_reply_tags_at_this_turn():
    cfg = _rpg_cfg()                                   # live_recalc defaults True
    store, sid, bid = _seed_player(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(sid, bid, 1, "new_turn", speaker="Narrator")
    reply = ("Steel meets flesh. [hp | Rune | -6 | a deep gash] "
             "[item gained | Rune | health potion] "
             "[quest | Escape the Deep | new | the walls are closing]")
    pipe.on_response(ctx, _json_reply(reply), "application/json")
    st = current_state(store, bid)
    assert st["player"]["rune"]["hp"]["cur"] == 24     # the NEWEST reply committed on ITS turn
    assert any(it["name"] == "health potion" for it in st["items"].values())
    assert any("escape" in str(q.get("name", "")).lower()
               for q in (st.get("quests") or {}).values())


def test_live_recalc_inert_under_none():
    cfg = Config()                                     # none session
    store, sid, bid = _seed_player(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(sid, bid, 1, "new_turn", speaker="Narrator")
    before = current_state(store, bid)
    pipe._ingest_reply_tags(ctx, "[hp | Rune | -9 | wound] [item gained | Rune | sword]")
    assert current_state(store, bid) == before         # cold-path tag ingest does nothing


def test_legacy_hot_path_tags_when_live_recalc_off():
    import random
    cfg = _rpg_cfg()
    cfg.extraction.live_recalc = False                 # opt back into lag-1
    store, sid, bid = _seed_player(cfg)
    doc = {"messages": [{"role": "assistant", "content": "[hp | Rune | -4 | cut]"},
                        {"role": "user", "content": "onward"}]}
    res = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(1))
    assert any(o["op"] == "hp_adj" for o in res.proposal_ops)   # legacy hot path still parses


# ------------------------------ swipe: source-scoped retraction ---------------------
def test_swipe_retracts_extraction_but_keeps_the_check():
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    # a resolved check (rule) + a tag-sourced wound (extraction) both land at turn 1
    apply_delta(store, sid, bid, 1, [{"op": "check", "skill": "stealth", "result": 9,
                "tier": "partial", "char": "rune", "_mod": 5, "_dice": "2d6", "_seed": [4, 5]}],
                "rule", cfg)
    apply_delta(store, sid, bid, 1, [{"op": "hp_adj", "char": "rune", "delta": -6}],
                "extraction", cfg)
    assert current_state(store, bid)["player"]["rune"]["hp"]["cur"] == 24
    store.retract_extraction_at(bid, 1)                # the swipe path
    st = current_state(store, bid)
    assert st["player"]["rune"]["hp"]["cur"] == 30     # extraction wound retracted...
    assert any(r.get("tier") == "partial" for r in st.get("rolls", []))   # ...check survives


# ------------------------------ creator: free-form categories + extras --------------
def test_custom_skill_and_ability_carry_free_form_category():
    from aetherstate import creator
    cfg = _rpg_cfg()
    doc = {"name": "Vex", "custom": {
        "skills": [{"name": "Pyromancy", "keyed_stat": "INT", "group": "Spells"}],
        "abilities": [{"name": "Chrome Arm", "kind": "passive", "group": "Cyber-Ware",
                       "passive_mod": {"skill": "pyromancy", "amount": 1}}]}}
    p = creator.deterministic_player(doc, cfg)
    assert p["defs"]["skills"]["pyromancy"]["group"] == "Spells"        # free-form category frozen
    assert p["defs"]["abilities"]["chrome_arm"]["group"] == "Cyber-Ware"


def test_world_and_char_extras_become_retrievable_lore():
    from aetherstate import creator
    w = {"name": "Aeth", "genre": "sci_fi",
         "extras": [{"label": "The Sundering", "text": "The old empire shattered in a night."}]}
    wops = creator.world_to_ops(w)
    assert any(o["op"] == "memory_event" and "The Sundering" in o["text"] for o in wops)
    p = {"name": "Vex", "extras": [{"label": "Backstory", "text": "Raised in the undercity."}]}
    pops = creator.player_to_ops(p, _rpg_cfg())
    assert any(o["op"] == "memory_event" and "Backstory" in o["text"] for o in pops)
