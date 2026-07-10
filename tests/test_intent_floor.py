"""Pure-code semantic REFLEX floor (2026-07-10, Bean) — the invariant-2-safe layer of the intent
cascade. Two grounded, deterministic, no-model/no-network layers gated by
[specialization].intent_floor (default on): (a) a stemmer + curated intent lexicon so natural
phrasings map to the OWNED skill they mean; (b) the entity-aware target picker — a strike resolves
to a PRESENT cast member, never a token run off the prose. off = the exact-match keyword floor
(v1.17.0); a `none` session is inert (byte-identical)."""
from __future__ import annotations

import json
import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.state import empty_state


def _rpg_cfg(intent_floor=True):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.intent_floor = intent_floor
    return cfg


def _player_state():
    st = empty_state()
    st["entities"]["wrenna"] = {"kind": "player", "name": "Wrenna", "present": True, "aliases": []}
    st["player"] = {"wrenna": {"eid": "wrenna", "stats": {"CHA": 14, "DEX": 13, "STR": 12},
        "skills": {"persuasion": 2, "stealth": 2, "swordplay": 1, "athletics": 1},
        "abilities": [], "defs": {}}}
    return st


def _combat_state():
    st = _player_state()
    st["entities"]["maren"] = {"kind": "npc", "name": "Maren", "present": True, "aliases": []}
    st["entities"]["pines"] = {"kind": "location", "name": "The Pines", "present": False}
    return st


def _run(msg, cfg, st, klass="new_turn", seed=5, assistant=None):
    msgs = []
    if assistant:
        msgs.append({"role": "assistant", "content": assistant})
    msgs.append({"role": "user", "content": msg})
    return tier0.run({"messages": msgs}, klass, False, st, cfg, random.Random(seed))


def _checks(res):
    return [o for o in res.rule_ops if o.get("op") == "check"]


def _spawns(res):
    return [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]


# ---- (a) stemmer + intent lexicon: a natural phrasing rolls the owned skill it MEANS ----------
def test_lexicon_synonym_fires_owned_skill():
    cfg = _rpg_cfg(True)                              # 'haggle' is not in persuasion's `governs`
    assert any(c["skill"] == "persuasion"
               for c in _checks(_run("I haggle over the price of the horse", cfg, _player_state())))


def test_conjugation_fires_via_stemmer():
    cfg = _rpg_cfg(True)                              # 'sneaked'/'scrambled' aren't literal governs
    assert any(c["skill"] == "stealth"
               for c in _checks(_run("I sneaked past the sentries", cfg, _player_state())))
    assert any(c["skill"] == "athletics"
               for c in _checks(_run("I scrambled up the wall", cfg, _player_state())))


def test_floor_off_reverts_to_exact_keyword_match():
    """intent_floor=False = v1.17.0: a synonym `governs` never listed does NOT fire; a literal
    governs verb still does."""
    off = _rpg_cfg(False)
    assert not any(c["skill"] == "persuasion"
                   for c in _checks(_run("I haggle over the price", off, _player_state())))
    assert any(c["skill"] == "persuasion"
               for c in _checks(_run("I persuade the guard", off, _player_state())))


def test_unowned_skill_synonym_never_fires():
    """Eligibility gate: a synonym of an UNOWNED skill is a non-move (nothing rollable w/o a basis)."""
    cfg = _rpg_cfg(True)
    st = _player_state()
    st["player"]["wrenna"]["skills"].pop("stealth")
    assert not any(c["skill"] == "stealth"
                   for c in _checks(_run("I tiptoe past the sentries", cfg, st)))


# ---- (b) entity-aware target picker: a strike lands on a PRESENT cast member ------------------
def test_strike_stages_present_cast_member_as_foe():
    cfg = _rpg_cfg(True)
    res = _run("I strike at Maren with my blade", cfg, _combat_state(),
               assistant="Maren bars the door, sneering.")
    sp = _spawns(res)
    assert sp and sp[0]["name"] == "Maren" and sp[0].get("char") == "maren"


def test_direction_or_place_never_becomes_a_foe():
    """Thornhale/Redgate: an attack whose object is a direction/place mints NO foe, even with a
    present NPC in the scene who is NOT the one attacked."""
    cfg = _rpg_cfg(True)
    res = _run("I strike toward the far corner with my blade", cfg, _combat_state(),
               assistant="The room is still and empty.")
    assert not _spawns(res)


# ---- invariants: none-inert + deterministic (replay-pure detection) ---------------------------
def test_none_session_is_inert():
    cfg = Config()                                   # specialization = none
    res = _run("I sweet-talk Maren and strike at her with my blade", cfg, _combat_state(),
               assistant="Maren sneers.")
    assert not any(o.get("op") in ("check", "combatant_spawn") for o in res.rule_ops)


def test_detection_is_deterministic():
    """Same state + text + seed -> byte-identical rule ops (the detection layer is a pure function;
    resolution values are baked, so replay from the journal is stable)."""
    cfg = _rpg_cfg(True)

    def norm(res):
        return json.dumps([{k: v for k, v in o.items() if k != "raw"} for o in res.rule_ops],
                          sort_keys=True, default=str)
    a = _run("I strike at Maren with my blade", cfg, _combat_state(), assistant="Maren bars the door.")
    b = _run("I strike at Maren with my blade", cfg, _combat_state(), assistant="Maren bars the door.")
    assert norm(a) == norm(b)
