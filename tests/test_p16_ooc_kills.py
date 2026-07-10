"""Out-of-combat kill gating + stealth / grand-event kills (2026-07-10, Bean).

Outside an active fight you cannot simply DECLARE a kill on a present target: a stealth/concealed
approach makes it a real roll (success = a silent kill + XP), a grand working (epic/mythic scope,
ritual/reality-warp + a roll) kills by prose + XP, and anything else is a routed NON-MOVE. Inside
combat, kills come from HP (the War Room). Inert under `none`.
"""
from __future__ import annotations

from aetherstate import tier0
from aetherstate.compose import _render_directive
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _rpg():
    c = Config()
    c.specialization.name = "rpg"
    return c


class _Rig:
    def __init__(self, v):
        self.value = v

    def randint(self, a, b):
        return self.value


def _world(cfg):
    """Ash (player, stealth + an ungated reality-shaping skill) + a present Sentry npc."""
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p16")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Ash", "kind": "player"},
        {"op": "entity_add", "name": "Sentry", "kind": "npc"},
        {"op": "presence", "entity": "Sentry", "present": True},
        {"op": "player_seed", "entity": "Ash",
         "card": {"stats": {"DEX": 16, "INT": 14},
                  "skills": {"stealth": 3, "reality_shaping": 3},
                  "defs": {"skills": {"reality_shaping": {
                      "name": "Reality Shaping", "keyed_stat": "INT", "base_mod": 2,
                      "governs": ["unmake", "erase", "warp", "shape", "annihilate"]}}},
                  "resources": {"hp": {"max": 20}}}}],
        "genesis", cfg)
    return store, sid, bid


def test_declared_kill_with_no_basis_is_a_routed_non_move():
    cfg = _rpg()
    store, sid, bid = _world(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I walk over and kill the Sentry."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    assert "NON-MOVE" in res.kill_note and "Sentry" in res.kill_note
    assert not any(o["op"] in ("effect_add", "award_exp", "presence") for o in res.rule_ops)
    assert any("non-move" in n for n in res.notices)               # visible to the player too


def test_stealth_kill_success_slays_awards_xp_and_removes_from_scene():
    cfg = _rpg()
    store, sid, bid = _world(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user",
                         "content": "((aether.check stealth)) I creep up and slit the Sentry."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(6))     # 2d6=12 -> success/crit
    assert "STEALTH KILL" in res.kill_note
    kinds = [o["op"] for o in res.rule_ops]
    assert "effect_add" in kinds and "award_exp" in kinds and "presence" in kinds
    slain = next(o for o in res.rule_ops if o["op"] == "effect_add")
    assert slain["effect"] == "Slain" and slain["char"] == "sentry"
    assert next(o for o in res.rule_ops if o["op"] == "award_exp")["amount"] >= 40
    r = apply_delta(store, sid, bid, 1, res.rule_ops, "rule", cfg)
    assert r.state["entities"]["sentry"].get("present") is False   # gone from the scene
    d = _render_directive({**r.state, "_kill_note": res.kill_note}, cfg)
    assert "STEALTH KILL" in d                                     # rides the [DIRECTIVE]


def test_stealth_kill_non_success_does_not_slay():
    cfg = _rpg()
    store = Store(":memory:")                          # a WEAK-stealth character so a low roll
    sid, bid = store.create_session(external_id="p16b")   # actually misses (mod can't lift it)
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Ash", "kind": "player"},
        {"op": "entity_add", "name": "Sentry", "kind": "npc"},
        {"op": "presence", "entity": "Sentry", "present": True},
        {"op": "player_seed", "entity": "Ash",
         "card": {"stats": {"DEX": 8}, "skills": {"stealth": 1},
                  "resources": {"hp": {"max": 20}}}}], "genesis", cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user",
                         "content": "((aether.check stealth)) I creep up and slit the Sentry."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(2))     # 2d6=4, DEX-1+rank1 -> a miss
    assert res.kill_note and "STEALTH KILL" not in res.kill_note   # a botch/fail, never a kill
    assert not any(o["op"] == "award_exp" for o in res.rule_ops)   # no XP -> nothing died


def test_grand_working_kills_by_prose_and_awards_more_xp():
    cfg = _rpg()
    store, sid, bid = _world(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": (
        "((aether.check reality_shaping scope epic)) I speak the unmaking word and erase "
        "the Sentry from existence.")}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(6))
    assert "GRAND WORKING" in res.kill_note
    assert any(o["op"] == "award_exp" and o["amount"] >= 60 for o in res.rule_ops)


def test_none_session_never_gates_or_kills():
    cfg = Config()                                                 # specialization = none
    store, sid, bid = _world(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I kill the Sentry."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    assert res.kill_note == ""                                     # no RPG kill gating under none
    assert not any(o["op"] in ("effect_add", "award_exp", "presence") for o in res.rule_ops)


def test_kill_inside_active_combat_is_left_to_hp():
    cfg = _rpg()
    store, sid, bid = _world(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "combatant_spawn", "name": "Sentry", "side": "enemy", "char": "sentry"}],
        "rule", cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content": "I kill the Sentry now!"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, _Rig(5))
    assert res.kill_note == ""                                     # combat active -> HP resolves it
