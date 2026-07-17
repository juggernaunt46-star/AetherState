"""§F — large-scale battle: micro dice slice + macro battle in prose, waves-when-losing
(the mechanics contract §F, Bean 2026-07-10).

Covers: the battle ledger + ops (battle_start / tide_set / battle_wave / battle_end), the
clamped one-step-per-turn tide, the code-owned momentum -> tide label, the battle referee
(waves while the tide isn't won, victory once it is), the [battle]/[tide] tag protocol + the
OOC surface, the [BATTLE] directive + contract teaching, and — per the RPG invariants — a
`none`-leak guard and deterministic replay.
"""
from __future__ import annotations

from aetherstate import tier0
from aetherstate.compose import _render_battle, render_header
from aetherstate.config import Config
from aetherstate.prompts import rules_contract
from aetherstate.state import (apply_delta, authority_violation, battle_ops, battle_tide,
                               combat_ops, current_state, empty_state, validate_op)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p18")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"STR": 14}, "skills": {"melee": 3},
                  "resources": {"hp": {"max": 24}}}}], "genesis", cfg)
    return store, sid, bid


def _spawn(store, sid, bid, turn, cfg, name="Raider", tier="standard"):
    r = apply_delta(store, sid, bid, turn, [
        {"op": "combatant_spawn", "name": name, "side": "enemy", "tier": tier}], "rule", cfg)
    assert r.applied, r.quarantined
    return r


def _clear_lone_foe(store, sid, bid, turn, cfg, target="Raider"):
    """Strike the lone foe to 0 (rule source = full delta) and run combat_ops — which, with a
    battle active, marks the defeat but does NOT end combat (the referee decides wave vs win)."""
    apply_delta(store, sid, bid, turn, [
        {"op": "combatant_hp", "target": target, "delta": -50, "_strike": True}], "rule", cfg)
    st = current_state(store, bid)
    wr = combat_ops(st, [])
    apply_delta(store, sid, bid, turn, wr, "rule", cfg)
    return wr


# ------------------------------ validation + authority --------------------------------
def test_battle_op_validation_and_authority():
    cfg, st = _rpg_cfg(), empty_state()
    assert validate_op({"op": "battle_start", "name": "The Breach"}) is not None
    assert validate_op({"op": "battle_start", "name": " "}) is None
    assert validate_op({"op": "tide_set", "tide": "winning"}) is not None
    assert validate_op({"op": "tide_set", "tide": "crushing"}) is None
    assert validate_op({"op": "battle_wave"}) is not None
    assert validate_op({"op": "battle_end"}) is not None
    for op in ({"op": "battle_start", "name": "X"}, {"op": "battle_wave"}, {"op": "battle_end"}):
        assert authority_violation(op, "extraction", st, cfg) is not None, op["op"]
        for src in ("user", "genesis", "rule"):
            assert authority_violation(op, src, st, cfg) is None, (op["op"], src)
    # tide_set is PROPOSABLE — the DM's [tide] report may ride extraction
    assert authority_violation({"op": "tide_set", "tide": "losing"}, "extraction", st, cfg) is None


# ------------------------------ the tide (clamped, derived) ---------------------------
def test_tide_steps_one_at_a_time_and_derives_the_label():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "battle_start", "name": "Field"}], "user", cfg)
    assert battle_tide(0) == "holding" and battle_tide(-2) == "losing" and battle_tide(3) == "winning"
    apply_delta(store, sid, bid, 2, [{"op": "tide_set", "tide": "winning", "why": "a"}], "rule", cfg)
    b = current_state(store, bid)["battle"]
    assert b["momentum"] == 1 and battle_tide(b["momentum"]) == "winning"   # one step up
    apply_delta(store, sid, bid, 3, [{"op": "tide_set", "tide": "losing", "why": "b"}], "rule", cfg)
    b = current_state(store, bid)["battle"]
    assert b["momentum"] == 0 and battle_tide(b["momentum"]) == "holding"   # one step, never a jump


def test_tide_with_no_active_battle_is_a_visible_non_move():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [{"op": "tide_set", "tide": "winning"}], "extraction", cfg)
    assert not r.applied and "no active battle" in r.quarantined[0]["reason"]


# ------------------------------ the referee: waves-when-losing ------------------------
def test_battle_sends_a_wave_when_the_tide_is_not_won():
    """Bean's core loop: the Player clears their War Room slice, but the macro battle isn't won,
    so a fresh wave presses in — never colliding with the just-cleared foes (battle_ops runs
    AFTER combat_ops's defeats land)."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "The Breach", "momentum": -1, "foe": "Raider",
         "wave_size": 2, "threat": "standard"}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Raider", tier="standard")
    wr = _clear_lone_foe(store, sid, bid, 2, cfg)
    assert not any(o["op"] == "combat_end" for o in wr)      # a battle is running -> no end yet
    st = current_state(store, bid)
    bw = battle_ops(st, [])
    spawns = [o for o in bw if o["op"] == "combatant_spawn"]
    assert len(spawns) == 2 and all(o["name"] == "Raider" for o in spawns)   # the next wave
    assert any(o["op"] == "battle_wave" for o in bw)
    r = apply_delta(store, sid, bid, 2, bw, "rule", cfg)
    b = r.state["battle"]
    assert b["waves"] == 1 and b["momentum"] == 0            # -1 + the wave-clear nudge (+1)
    live = [x for x in r.state["combat"]["combatants"].values()
            if not x["defeated"] and x["side"] == "enemy"]
    assert len(live) == 2                                    # the wave is on the field, cap-safe


def test_no_wave_once_the_tide_is_won():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "Field", "momentum": 1}], "user", cfg)   # winning
    _spawn(store, sid, bid, 1, cfg, name="Raider", tier="standard")
    _clear_lone_foe(store, sid, bid, 2, cfg)
    st = current_state(store, bid)
    bw = battle_ops(st, [])
    assert any(o["op"] == "battle_end" and o["outcome"] == "victory" for o in bw)
    assert any(o["op"] == "combat_end" for o in bw)
    assert not any(o["op"] == "combatant_spawn" for o in bw)   # the field is won — no more waves
    r = apply_delta(store, sid, bid, 2, bw, "rule", cfg)
    assert not r.state["battle"]["active"] and not r.state["combat"]["active"]


def test_player_defeat_ends_the_battle_too():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "Field", "momentum": -2}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Raider", tier="standard")
    st = current_state(store, bid)
    ops = combat_ops(st, [{"op": "defeat_resolve", "char": "kael", "outcome": "captured"}])
    assert {"op": "battle_end", "outcome": "defeat"} in ops
    assert any(o["op"] == "combat_end" and o["outcome"] == "defeat" for o in ops)


# ------------------------------ the tag protocol + OOC --------------------------------
def test_battle_and_tide_tags_parse():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    ops = tier0.parse_battle_tags(
        "It's a rout. [battle | Siege of Stonefield | Orc | elite]\n"
        "[tide | losing | the west gate has fallen]", st)
    start = next(o for o in ops if o["op"] == "battle_start")
    assert start["name"] == "Siege of Stonefield" and start["threat"] == "elite" \
        and start["foe"] == "Orc"
    assert {"op": "tide_set", "tide": "losing", "why": "the west gate has fallen"} in ops


def test_valid_battle_tide_and_ally_channels_never_trigger_an_ignored_tag_nudge():
    text = ("[battle | Siege of Stonefield | Orc | elite]\n"
            "[tide | losing | the west gate has fallen]\n"
            "[ally | Marshal Varo | elite | spear]")

    assert tier0._scan_off_protocol(text) == []


def test_ooc_battle_commands():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    r1 = tier0.run({"messages": [{"role": "user",
                                  "content": "((aether.battle Siege of Stonefield)) go"}]},
                   "new_turn", False, st, cfg)
    assert {"op": "battle_start", "name": "Siege of Stonefield"} in r1.user_ops
    r2 = tier0.run({"messages": [{"role": "user",
                                  "content": "((aether.battle tide winning)) yes"}]},
                   "new_turn", False, st, cfg)
    assert {"op": "tide_set", "tide": "winning"} in r2.user_ops
    r3 = tier0.run({"messages": [{"role": "user", "content": "((aether.battle end)) done"}]},
                   "new_turn", False, st, cfg)
    assert {"op": "battle_end", "outcome": "called"} in r3.user_ops


# ------------------------------ the [BATTLE] directive + contract ---------------------
def test_battle_directive_and_contract_gate():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "the Breach", "momentum": -1}], "user", cfg)
    st = current_state(store, bid)
    d = _render_battle(st, cfg)
    assert "[BATTLE]" in d and "LOSING" in d and "the Breach" in d
    rc = rules_contract(cfg)
    assert "[battle" in rc and "[tide" in rc                 # the DM is taught both channels
    cfg.specialization.large_battle = False
    assert _render_battle(st, cfg) == ""                     # knob off: the block is silent


# ------------------------------ none-leak (RPG invariant 1) ---------------------------
def test_none_session_carries_no_battle_fingerprint():
    cfg = Config()                                           # specialization = none
    assert "battle" not in empty_state()                     # checkpoints byte-identical
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="none-battle")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Tam"}], "user", cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user",
                         "content": "[battle | War] [tide | losing] ((aether.battle X))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg)
    battle_ops_kinds = {"battle_start", "tide_set", "battle_wave", "battle_end"}
    assert all(o.get("op") not in battle_ops_kinds
               for o in (res.user_ops + res.rule_ops + res.proposal_ops))
    assert "[BATTLE]" not in render_header(st, cfg)
    r = apply_delta(store, sid, bid, 1, [{"op": "battle_start", "name": "X"}], "extraction", cfg)
    assert not r.applied                                     # privileged even under none


# ------------------------------ replay determinism ------------------------------------
def test_battle_replays_deterministically():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "Field", "momentum": -2, "foe": "Raider",
         "wave_size": 2}], "user", cfg)
    _spawn(store, sid, bid, 1, cfg, name="Raider", tier="standard")
    _clear_lone_foe(store, sid, bid, 2, cfg)
    st = current_state(store, bid)
    apply_delta(store, sid, bid, 2, battle_ops(st, []), "rule", cfg)
    s1 = current_state(store, bid)
    s2 = current_state(store, bid)                           # fresh replay of the journal
    assert s1 == s2
    assert s1["battle"]["waves"] == 1 and s1["battle"]["momentum"] == -1   # -2 + wave nudge
