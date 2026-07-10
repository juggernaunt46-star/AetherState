"""Phase 0b — notables gate + anti-main-character + L11 (plan doc 13, ratified 2026-07-09).

Covers: home anchors (creator -> frozen set_attribute), the R5 arrival gate (assistant-text
basis only — player speculation stages no one), [NEARBY] rendering (anchor matches the scene;
anchored-elsewhere = zero tokens; rpg-gated none-leak), the knows-player line ([RELATIONS]
stranger / by-reputation / affinity-tier precedence), the L9 open-bracket door (pillar 14),
and L11 (decision-voice + verbatim-quote protection; inert without rpg)."""
from __future__ import annotations

import random

from aetherstate import compose, creator, linter, tier0
from aetherstate.config import Config


def _rpg_cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _state(present=False, home="", faction="", affinity=None) -> dict:
    st = {
        "meta": {"turn": 3},
        "scene": {"location_id": "rusty_flagon"},
        "clock": {"day": 1},
        "chars": {},
        "player": {"bean": {"level": 1}},
        "entities": {
            "bean": {"name": "Bean", "kind": "player", "present": True},
            "greta": {"name": "Greta", "kind": "npc", "present": present},
            "rusty_flagon": {"name": "The Rusty Flagon", "kind": "location"},
        },
        "attributes": {"greta": {"role": "tavern keeper"}},
    }
    if home:
        st["attributes"]["greta"]["home"] = home
    if faction:
        st["attributes"]["greta"]["faction"] = faction
        st["entities"]["iron_pact"] = {"name": "Iron Pact", "kind": "faction"}
    if affinity:
        st["affinity"] = affinity
    return st


def _doc(user: str, assistant: str) -> dict:
    return {"model": "m", "messages": [
        {"role": "assistant", "content": assistant},
        {"role": "user", "content": user}]}


# ------------------------------ home anchors (creator) --------------------------------
def test_world_to_ops_carries_home_anchor():
    ops = creator.world_to_ops({
        "name": "W", "locations": ["The Rusty Flagon — a dockside tavern"],
        "npcs": [{"name": "Greta", "role": "keeper", "home": "The Rusty Flagon"}]})
    assert any(o.get("op") == "set_attribute" and o.get("key") == "home"
               and o.get("value") == "The Rusty Flagon" and o.get("entity") == "greta"
               for o in ops)


# ------------------------------ R5: arrivals need an assistant basis ------------------
def test_r5_arrival_needs_assistant_basis():
    cfg = _rpg_cfg()
    st = _state()
    t0 = tier0.run(_doc("I hope Greta arrives soon.", "The common room hums."),
                   "new_turn", False, st, cfg, random.Random(1))
    assert not any(o.get("op") == "presence" and o.get("present")
                   for o in t0.rule_ops), "player speculation staged a notable"
    t0 = tier0.run(_doc("I wait by the fire.", "Greta arrives with a tray of mugs."),
                   "new_turn", False, st, cfg, random.Random(1))
    assert any(o.get("op") == "presence" and o.get("present") and o.get("entity") == "greta"
               for o in t0.rule_ops), "narrated arrival should stage"
    t0 = tier0.run(_doc("Greta leaves the room.", ""),
                   "new_turn", False, st, cfg, random.Random(1))
    assert any(o.get("op") == "presence" and o.get("present") is False
               for o in t0.rule_ops), "departures still scan both sides"


# ------------------------------ [NEARBY] anchor gate -----------------------------------
def test_nearby_renders_only_on_anchor_match():
    cfg = _rpg_cfg()
    hdr = compose.render_header(_state(home="The Rusty Flagon"), cfg)
    assert "[NEARBY]" in hdr and "Greta (tavern keeper)" in hdr and "stranger" in hdr
    hdr2 = compose.render_header(_state(home="Harbor Gate"), cfg)
    assert "[NEARBY]" not in hdr2, "anchored elsewhere must spend zero tokens"
    hdr3 = compose.render_header(_state(present=True, home="The Rusty Flagon"), cfg)
    assert "[NEARBY]" not in hdr3, "already on scene — no duplicate surface"


def test_nearby_and_stranger_none_leak():
    cfg = Config()                        # specialization = none
    hdr = compose.render_header(_state(present=True, home="The Rusty Flagon"), cfg)
    assert "[NEARBY]" not in hdr and "stranger" not in hdr


# ------------------------------ knows-player (anti-main-character) --------------------
def test_relations_stranger_reputation_and_tier_precedence():
    cfg = _rpg_cfg()
    hdr = compose.render_header(_state(present=True), cfg)
    assert "Greta: stranger" in hdr
    hdr = compose.render_header(_state(
        present=True, faction="Iron Pact",
        affinity={"bean->iron_pact": {"value": -12, "kind": "faction"}}), cfg)
    assert "by reputation (Iron Pact" in hdr
    hdr = compose.render_header(_state(
        present=True, affinity={"bean->greta": {"value": 25}}), cfg)
    assert "Greta: stranger" not in hdr   # the affinity tier line owns a known NPC


# ------------------------------ L9 door + L11 (pillar 14) -----------------------------
def test_l9_bracket_door():
    cfg = _rpg_cfg()
    st = _state()
    reply = 'Bean says "I will help you." He shoulders the pack.'
    v = linter.run(st, reply, cfg, user_name="Bean", user_text="I look around.")
    assert any(x.rule == "L9" for x in v), "no door — L9 fires as ever"
    v = linter.run(st, reply, cfg, user_name="Bean",
                   user_text="[I persuade Greta to help.]")
    assert not any(x.rule == "L9" for x in v), "open bracket intent opens the door"
    v = linter.run(st, reply, cfg, user_name="Bean",
                   user_text="[OOC: keep it short] I look around.")
    assert any(x.rule == "L9" for x in v), "an OOC bracket is not a door"


def test_l11_decision_voice():
    cfg = _rpg_cfg()
    st = _state()
    v = linter.run(st, "Bean agrees to the terms without hesitation.", cfg,
                   user_name="Bean", user_text="I stare at the contract.")
    assert any(x.rule == "L11" for x in v)
    v = linter.run(st, 'Greta grins. "You agree, then?"', cfg,
                   user_name="Bean", user_text="I stare at the contract.")
    assert not any(x.rule == "L11" for x in v), "NPC dialogue is not agency theft"
    v = linter.run(st, "Bean agrees to the terms.", cfg,
                   user_name="Bean", user_text="[I accept whatever deal she offers.]")
    assert not any(x.rule == "L11" for x in v), "the open door allows deciding"


def test_l11_verbatim_quote_protection():
    cfg = _rpg_cfg()
    st = _state()
    ut = '[I say to Greta "Could you follow me, for old time\'s sake?"]'
    ok = ('Bean steps closer. "Could you follow me, for old time\'s sake?" '
          "Greta blinks at the old phrase.")
    bad = 'Bean steps closer. "You should come with me right now." Greta blinks.'
    assert not any(x.rule == "L11" for x in
                   linter.run(st, ok, cfg, user_name="Bean", user_text=ut))
    assert any(x.rule == "L11" for x in
               linter.run(st, bad, cfg, user_name="Bean", user_text=ut))


def test_l11_inert_without_rpg():
    cfg = Config()                        # base session: L11 never fires, L9 unchanged
    st = _state()
    v = linter.run(st, "Bean agrees to the terms.", cfg,
                   user_name="Bean", user_text="I wait.")
    assert not any(x.rule == "L11" for x in v)
    reply = 'Bean says "fine." He nods.'
    v = linter.run(st, reply, cfg, user_name="Bean",
                   user_text="[I persuade Greta.]")
    assert any(x.rule == "L9" for x in v), "the door is rpg-gated — base L9 byte-identical"
