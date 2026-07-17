"""P7 (Bean 2026-07-07) — the gear/inventory CLASS split, worn-at-start auto-equip, and
live_recalc newest-reply ingestion.

Covers: classify_item (pure), item_gain auto-equip onto the paper-doll, the [GEAR]/[INVENTORY]
render split by class (not by slot), the cold-path world-tag ingestion of the FRESH reply, and
source-scoped swipe retraction (the resolved check survives). Every group carries a none-leak or
deterministic-replay guard (RPG invariant 1/4).
"""
from __future__ import annotations

import json
import logging

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


def test_classify_item_expanded_slot_recognition():
    """2026-07-10 (Bean: "high heels don't even go to feet"): the slot heuristic recognizes a much
    wider wardrobe — footwear/dresses/accessories — with plural tolerance, and long-only compound
    suffixes so common non-gear words don't misfire ("husband" is not headgear)."""
    feet = ("high heels", "red stilettos", "ballet flats", "leather loafers", "wool socks",
            "combat boots", "silk slippers", "platform wedges", "running sneakers")
    for nm in feet:
        assert classify_item(nm)["slot"] == "feet", nm
    cases = {"silk evening gown": "body", "black cocktail dress": "body", "tight corset": "body",
             "velvet cloak": "cape", "golden circlet": "head", "silk gloves": "hands",
             "pearl necklace": "neck", "jeweled anklet": "accessory2", "signet ring": "accessory1",
             "studded belt": "waist", "leather leggings": "legs", "oaken buckler": "offhand",
             "worn backpack": "back", "steel gauntlets": "hands", "feathered mask": "face"}
    for nm, slot in cases.items():
        got = classify_item(nm)
        assert got["class"] == "gear" and got["slot"] == slot, (nm, got)
    # false positives the OLD end-anchored suffix match would have produced are gone
    assert classify_item("her jealous husband")["slot"] != "head"      # "…band"
    assert classify_item("a strong handicap")["slot"] != "head"        # "…cap"
    assert classify_item("smoked herring")["slot"] != "accessory1"     # "…ring"


def test_gear_authored_slot_and_prose_effect_flow_through():
    """2026-07-10 (Bean): a gear item can carry an AUTHORED slot (manual/creator — overriding the
    name heuristic) and a PROSE 'aura' effect (appearance/glamour/lore). Both freeze at mint and
    ride the item, [GEAR], and the HUD."""
    from aetherstate.compose import _render_gear
    from aetherstate.config import Config
    from aetherstate.hud import hud_view
    from aetherstate.store import Store
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="gear-aura")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Sera", "kind": "player"},
        {"op": "player_seed", "entity": "Sera", "card": {"resources": {"hp": {"max": 18}}}}],
        "genesis", cfg)
    # a name the heuristic would classify as plain inventory, PINNED to the neck slot with a prose
    # effect — the authored slot must win and the aura must survive to the render surfaces
    r = apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "Sera", "name": "the Murmuring Oath", "slot": "neck",
         "aura": "a black pearl that hushes a room when you enter; strangers lean in"}], "user", cfg)
    assert r.applied, r.quarantined
    st = current_state(store, bid)
    it = next(i for i in st["items"].values() if i["name"] == "the Murmuring Oath")
    assert it["class"] == "gear" and it["slot"] == "neck"          # authored slot won
    assert it["loc"] == "gear:neck" and "hushes a room" in it.get("aura", "")   # equipped + frozen
    gear = _render_gear(st, cfg)
    assert "neck=the Murmuring Oath" in gear and "strangers lean in" in gear    # aura rides [GEAR]
    hv = hud_view(st, cfg)
    doll = [r2 for r2 in hv["players"][0]["gear_slots"] if r2["slot"] == "neck"][0]
    assert doll["item"] and "hushes a room" in doll["item"]["aura"]              # HUD carries it


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


def test_live_recalc_passes_exact_context_to_living_world_referee():
    """A delivered reply must reach the post-ingest world pass without a stale request object."""
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(sid, bid, 6, "new_turn", speaker="Narrator")

    pipe.on_response(
        ctx,
        _json_reply("Rune finds a useful bundle. [item gained | Rune | climbing rope]"),
        "application/json",
    )

    state = current_state(store, bid)
    assert state["clock"]["time_of_day"] == "night"
    assert state["clock"]["last_advance_turn"] == 6


def test_live_recalc_authorizes_foe_first_cohort_and_activates_war_room():
    """The production response caller carries a finite cohort through live ingress authority."""
    from aetherstate.hud import hud_view
    from aetherstate.state import battle_cohort_status

    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(sid, bid, 1, "new_turn", speaker="Narrator",
                      response_key="sanitized-cohort-response")
    reply = (
        "Six shapes break from the ash haze.\n"
        "[foe | Ash Wretch x6 | minion | claws]\n"
        "[battle | Hollow Road Assault | Ash Wretch host | minion]\n"
        "[scene | Hollow Road | climax | present: Rune, Ash Wretch x6]"
    )

    pipe.on_response(ctx, _json_reply(reply), "application/json")

    state = current_state(store, bid)
    cohort = battle_cohort_status(state)
    assert state["battle"]["active"] is True
    assert state["combat"]["active"] is True
    assert cohort and cohort["total"] == 6 and cohort["active"] == 3
    assert cohort["queued"] == 3
    assert isinstance(state["combat"]["pending_intent"], dict)
    view = hud_view(state, cfg)["war_room"]
    assert view["active"] is True
    assert len(view["combatants"]) == 3
    assert view["battle"]["cohort"]["queued"] == 3
    assert view["intent"]["actor_name"] == "Ash Wretch #1"
    start = next(op for op in store.rule_ops_at(bid, 1) if op["op"] == "battle_start")
    assert start["_semantic_ingress"]["channel"] == "narrator_candidate"
    assert start["_semantic_declaration"]["operation_family"] \
        == "battle_cohort_declaration"
    assert pipe.recent_notices(sid) == []


def test_live_recalc_rejects_noncontiguous_cohort_with_visible_notice(caplog):
    cfg = _rpg_cfg()
    store, sid, bid = _seed_player(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(sid, bid, 1, "new_turn", speaker="Narrator")
    reply = (
        "[foe | Ash Wretch x6 | minion | claws]\n"
        "This prose cannot be silently canonicalized into authority.\n"
        "[battle | Hollow Road Assault | Ash Wretch host | minion]"
    )

    with caplog.at_level(logging.WARNING, logger="aetherstate.pipeline"):
        pipe.on_response(ctx, _json_reply(reply), "application/json")

    state = current_state(store, bid)
    assert not state.get("battle", {}).get("active")
    assert not state.get("combat", {}).get("active")
    assert any("contiguous reserved declaration block" in record.getMessage()
               for record in caplog.records)
    notices = pipe.recent_notices(sid)
    assert len(notices) == 1
    assert "War Room could not accept the narrator's battle group" in notices[0]["text"]


def test_live_recalc_never_authorizes_markdown_quoted_cohort_blocks():
    blocks = (
        (
            "The narrator quotes an example:\n```text\n"
            "[foe | Ash Wretch x6 | minion | claws]\n"
            "[battle | Hollow Road Assault | Ash Wretch host | minion]\n```"
        ),
        (
            "The narrator quotes an indented example:\n"
            "    [foe | Ash Wretch x6 | minion | claws]\n"
            "    [battle | Hollow Road Assault | Ash Wretch host | minion]"
        ),
    )
    for reply in blocks:
        cfg = _rpg_cfg()
        store, sid, bid = _seed_player(cfg)
        pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
        ctx = PostContext(sid, bid, 1, "new_turn", speaker="Narrator")

        pipe.on_response(ctx, _json_reply(reply), "application/json")

        state = current_state(store, bid)
        assert not state.get("battle", {}).get("active")
        assert not state.get("combat", {}).get("active")
        assert len(pipe.recent_notices(sid)) == 1


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
