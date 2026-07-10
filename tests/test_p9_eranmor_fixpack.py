"""2026-07-10 Eranmor fix pack: the slow-turns / lost-roll / silent-War-Room findings.

Covers: lost-turn RE-SERVE (the same action re-sent after an empty settled reply re-serves
the already-settled check -- no re-roll, no double clock/cost/cooldown; a reply that exists
means a normal turn; none stays inert), directive anchoring (lost_reply + blocked-ability
clauses + the newest-message sentence), the [CHECK] dialect healer (the DM's invented
bracket grammar arms R8b), the off-protocol [PROTOCOL] nudge, the foe floor (an attacked,
DM-narrated target opens the War Room; body parts / ungrounded names never spawn; replay
pure; none inert), forward stamp jumps clamped to head+1 (see test_l3_sessions), the
sticky rules contract (degrades full->compact, never drops), the LEDGER TAGS de-attractor,
and rpg injection depth 1.
"""
from __future__ import annotations

import json
import random

from aetherstate import prompts, tier0
from aetherstate.compose import Component, _render_directive, compose, govern, splice
from aetherstate.config import RPG_PROFILE, Config
from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta, current_state, empty_state, reduce_state
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None, ext="p9"):
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=ext)
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Vex", "kind": "player"},
        {"op": "player_seed", "entity": "Vex",
         "card": {"stats": {"CHA": 14}, "skills": {"persuasion": 3, "swordplay": 2},
                  "resources": {"hp": {"max": 20}}}}],
        "genesis", cfg)
    return store, sid, bid


class _Rig:
    def __init__(self, value=4):
        self.value = value

    def randint(self, a, b):
        return self.value


def _doc(assistant, user):
    return {"messages": [{"role": "user", "content": "earlier"},
                         {"role": "assistant", "content": assistant},
                         {"role": "user", "content": user}]}


def _body(assistant, user):
    return json.dumps({"model": "m", "messages": _doc(assistant, user)["messages"]}).encode()


def _injected(out_bytes):
    doc = json.loads(out_bytes)
    return "\n".join(m.get("content", "") for m in doc.get("messages", [])
                      if isinstance(m, dict) and m.get("role") == "system")


# ------------------------------ lost-turn re-serve ------------------------------------
def test_reserve_lost_turn_reserves_settled_check_no_reroll():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg,
                    rng=random.Random(11))
    body = _body("The hollow lunges across the fire.",
                 "((aether.check persuasion)) Vex pushes.")
    out1, ctx1 = pipe.process(Stamp(session="p9", gen_type="normal", turn=1, user="Bean"),
                              body)
    checks0 = [o for o in store.rule_ops_at(bid, ctx1.turn_index) if o.get("op") == "check"]
    assert len(checks0) == 1
    tier0_roll = checks0[0]["tier"]
    # the reply is LOST: no assistant text ever lands for that turn; the user re-sends.
    out2, ctx2 = pipe.process(Stamp(session="p9", gen_type="normal", turn=2, user="Bean"),
                              body)
    assert ctx2.turn_index == ctx1.turn_index + 1
    assert store.rule_ops_at(bid, ctx2.turn_index) == [], \
        "the retry must journal NOTHING (no re-roll, no double clock/cost/cooldown)"
    allchecks = [o for t in range(0, ctx2.turn_index + 1)
                 for o in store.rule_ops_at(bid, t) if o.get("op") == "check"]
    assert len(allchecks) == 1, "one player action = one resolution"
    inj = _injected(out2)
    assert "ALREADY SETTLED" in inj and tier0_roll.upper() in inj
    assert "NEWEST message" in inj


def test_reserve_only_when_reply_was_actually_lost():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg,
                    rng=random.Random(11))
    body = _body("The hollow lunges.", "((aether.check persuasion)) Vex pushes.")
    out1, ctx1 = pipe.process(Stamp(session="p9", gen_type="normal", turn=1, user="Bean"),
                              body)
    store.write_turn_text(bid, ctx1.turn_index, assistant_text="Narrator: it lands.")
    out2, ctx2 = pipe.process(Stamp(session="p9", gen_type="normal", turn=2, user="Bean"),
                              body)
    checks2 = [o for o in store.rule_ops_at(bid, ctx2.turn_index) if o.get("op") == "check"]
    assert len(checks2) == 1, "a reply that EXISTS means a genuinely new turn (re-roll)"


def test_reserve_knob_off_restores_reroll():
    cfg = _rpg_cfg()
    cfg.session.reserve_lost_turns = False
    store, sid, bid = _seeded(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg,
                    rng=random.Random(11))
    body = _body("The hollow lunges.", "((aether.check persuasion)) Vex pushes.")
    out1, ctx1 = pipe.process(Stamp(session="p9", gen_type="normal", turn=1, user="Bean"),
                              body)
    out2, ctx2 = pipe.process(Stamp(session="p9", gen_type="normal", turn=2, user="Bean"),
                              body)
    checks2 = [o for o in store.rule_ops_at(bid, ctx2.turn_index) if o.get("op") == "check"]
    assert len(checks2) == 1


def test_reserve_none_session_stays_inert():
    cfg = Config()
    store, sid, bid = _seeded(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg,
                    rng=random.Random(11))
    body = _body("A quiet night.", "Vex pushes.")
    out1, ctx1 = pipe.process(Stamp(session="p9", gen_type="normal", turn=1, user="Bean"),
                              body)
    out2, ctx2 = pipe.process(Stamp(session="p9", gen_type="normal", turn=2, user="Bean"),
                              body)
    ops2 = store.rule_ops_at(bid, ctx2.turn_index)
    assert any(o.get("op") == "clock_tick" for o in ops2), \
        "no checks exist under none -> the normal path runs (byte-identical behavior)"
    assert "[DIRECTIVE]" not in _injected(out2)


# ------------------------------ directive anchoring -----------------------------------
def test_directive_lost_and_blocked_clauses():
    st = {"meta": {"turn": 5},
          "_fresh_checks": [{"op": "check", "skill": "swordplay", "tier": "partial",
                             "lost_reply": True,
                             "_ability_blocked": [{"name": "Three-pronged Slash",
                                                   "why": "still recharging (ready turn 6)"}]}]}
    out = _render_directive(st, None)
    assert "ALREADY SETTLED" in out
    assert "did NOT ride this roll" in out and "plain attempt" in out
    assert "NEWEST message" in out


def test_contract_wording_newest_message():
    text = prompts.rules_contract(_rpg_cfg())
    assert "reaches you next turn" not in text
    assert "NEWEST message" in text


# ------------------------------ the [CHECK] dialect healer ----------------------------
def test_bracket_check_line_arms_like_r8b():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("[CHECK] Vex melee approach | target: baser Hollow (1) | skill: Persuasion+2\n"
               "[AWAIT]",
               "Vex leans in and pushes, voice low and even.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert len(checks) == 1 and checks[0]["skill"] == "persuasion"
    assert checks[0].get("_dm_called") is True


def test_bracket_check_healer_yields_to_proper_syntax():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("That is an ((aether.check persuasion)) if you push.\n"
               "[CHECK] something else | skill: Swordplay",
               "Vex leans in and pushes.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert len(checks) == 1 and checks[0]["skill"] == "persuasion"


# ------------------------------ the off-protocol nudge --------------------------------
def test_off_protocol_heads_collected_and_rendered():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("The camp burns.\n"
               "[TAGS] scene_active | hollow_rift_open | combat_imminent\n[AWAIT]",
               "Vex runs for the wagons.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(3))
    assert "TAGS" in res.off_protocol and "AWAIT" in res.off_protocol
    state["_protocol_nudge"] = res.off_protocol
    out = _render_directive(state, cfg)
    assert "[PROTOCOL]" in out and "((aether.check <skill>))" in out


def test_known_tags_do_not_trigger_the_nudge():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("Steel bites. [status gained | Vex | Bleeding | negative]\n"
               "[scene | rift edge | climax | present: Vex]",
               "Vex presses the wound.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(3))
    assert res.off_protocol == []


# ------------------------------ the foe floor -----------------------------------------
_DM_HORDE = ("They pour from the treeline, flesh the color of wet ash - the baser sort, "
             "a hollow walking up the spear shaft toward the boy.")


def test_foe_floor_stages_attacked_dm_narrated_target():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc(_DM_HORDE, "((aether.check swordplay)) I attack the baser Hollow at the head.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    spawns = [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]
    assert len(spawns) == 1 and spawns[0]["side"] == "enemy"
    assert spawns[0]["name"].lower() == "baser hollow", spawns[0]["name"]
    apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    st = current_state(store, bid)
    assert st["combat"]["active"] is True
    rows = [r for r in st["combat"]["combatants"].values() if not r.get("defeated")]
    assert rows and rows[0]["side"] == "enemy"
    out = _render_directive(st, cfg)
    assert "[OPPOSITION]" in out, "a live foe arms the enemy die with no [scene] tag needed"


def test_foe_floor_ignores_body_parts_and_ungrounded_names():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("The camp is quiet; embers settle.",
               "((aether.check swordplay)) I attack the dragon king at the head.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    assert not [o for o in res.rule_ops if o.get("op") == "combatant_spawn"], \
        "no in-world basis (the DM never narrated it) -> nothing spawns"


def test_foe_floor_knob_and_none_inert():
    cfg = _rpg_cfg()
    cfg.specialization.foe_floor = False
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc(_DM_HORDE, "((aether.check swordplay)) I attack the baser Hollow.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    assert not [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]
    none_cfg = Config()
    store2, sid2, bid2 = _seeded(none_cfg)
    res2 = tier0.run(doc, "new_turn", False, current_state(store2, bid2), none_cfg,
                     random.Random(7))
    assert not [o for o in res2.rule_ops if o.get("op") in ("check", "combatant_spawn")]


def test_foe_floor_replay_deterministic():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc(_DM_HORDE, "((aether.check swordplay)) I attack the baser Hollow.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(6))
    apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    live = current_state(store, bid)
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay["combat"] == live["combat"]
    assert replay["rolls"][-1] == live["rolls"][-1]


# ------------------------------ the sticky contract -----------------------------------
def test_govern_never_drops_the_rules_contract():
    cfg = Config()
    cfg.injection.max_tokens = 120
    comps = [Component("state_header", "H" * 400, 100),
             Component("director_note", "N" * 200, 80),
             Component("rules_contract", "R" * 4000, 72),
             Component("memories", "M" * 400, 60)]
    kept = {c.cls for c in govern(comps, cfg)}
    assert "rules_contract" in kept
    assert "memories" not in kept


def test_compose_degrades_contract_to_compact_under_pressure():
    cfg = _rpg_cfg()
    cfg.injection.max_tokens = 400
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("The camp is quiet.", "Vex waits.")
    out, kept = compose(doc, state, cfg, Stamp(session="p9", user="Bean"), "new_turn")
    assert any(k["cls"] == "rules_contract" for k in kept)
    inj = "\n".join(m.get("content", "") for m in out["messages"]
                     if isinstance(m, dict) and m.get("role") == "system")
    assert "A GAME with dice, not chat." in inj, "compact rung selected under pressure"
    assert "You are the Game Master of a mechanical RPG" not in inj
    assert "LEDGER TAGS" in inj, "the tag grammar rides even on the compact rung"


def test_compose_keeps_full_contract_when_it_fits():
    cfg = _rpg_cfg()
    cfg.injection.max_tokens = 6000
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("The camp is quiet.", "Vex waits.")
    out, kept = compose(doc, state, cfg, Stamp(session="p9", user="Bean"), "new_turn")
    inj = "\n".join(m.get("content", "") for m in out["messages"]
                     if isinstance(m, dict) and m.get("role") == "system")
    assert "You are the Game Master of a mechanical RPG" in inj


# ------------------------------ de-attractor + depth -----------------------------------
def test_tags_header_renamed_and_versions_bumped():
    text = prompts.rules_contract(_rpg_cfg())
    assert "LEDGER TAGS" in text
    assert "\n[TAGS]" not in text
    assert prompts.EFFECTS_PROTOCOL_VERSION == "world-tags/6"
    assert prompts.DM_CONTRACT_VERSION == "dm-rules/8"


def test_rpg_profile_depth_one_places_block_above_newest_message():
    assert RPG_PROFILE["injection"]["depth"] == 1
    cfg = Config()
    cfg.injection.depth = 1
    doc = {"messages": [{"role": "user", "content": "a"},
                        {"role": "assistant", "content": "b"},
                        {"role": "user", "content": "newest"}]}
    out = splice(doc, "STATE", cfg)
    assert out["messages"][-2]["content"] == "STATE"
    assert out["messages"][-1]["content"] == "newest"


# ------------------------------ notices ring ------------------------------------------
def test_pipeline_notice_ring_feeds_hud():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg,
                    rng=random.Random(11))
    body = _body("The hollow lunges.",
                 "((aether.check swordplay use three_pronged_slash)) I strike.")
    out1, ctx1 = pipe.process(Stamp(session="p9", gen_type="normal", turn=1, user="Bean"),
                              body)
    notes = pipe.recent_notices(ctx1.session_id)
    assert notes and any("three_pronged_slash" in n["text"] or "don't know" in n["text"]
                         for n in notes), notes
