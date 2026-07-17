"""Regression tests for the 2026-07-11 bench fix pack (Emberfall regression test):
 - parse_foe_tags never twins a foe already LIVE on the field (the `purge#2` bug);
 - a defeated same-name foe does not block a genuinely new spawn;
 - merge_baseline_skills gives every Player Card the universal skill floor (authored ranks kept).
"""
from aetherstate.config import Config
from aetherstate.state import (BASELINE_SKILLS, apply_delta, current_state,
                               merge_baseline_skills, resolve_combatant)
from aetherstate.store import Store
from aetherstate.tier0 import parse_foe_tags


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _combat_state():
    cfg = _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p19")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Rhoswen", "kind": "player"},
        {"op": "combatant_spawn", "name": "Purge", "side": "enemy", "tier": "standard"}],
        "rule", cfg)
    return store, sid, bid, cfg


def test_foe_tag_does_not_twin_a_live_combatant():
    store, sid, bid, _cfg = _combat_state()
    state = current_state(store, bid)
    assert resolve_combatant(state, "Purge")            # it is already on the field
    assert parse_foe_tags("[foe | Purge | standard]", state) == []   # re-tag -> no twin


def test_foe_tag_still_spawns_a_genuinely_new_foe():
    store, sid, bid, _cfg = _combat_state()
    state = current_state(store, bid)
    ops = parse_foe_tags("[foe | Ashling | minion]", state)
    assert len(ops) == 1 and ops[0]["name"] == "Ashling" and ops[0]["side"] == "enemy"


def test_foe_tag_respawns_after_the_prior_foe_is_defeated():
    store, sid, bid, cfg = _combat_state()
    cid = resolve_combatant(current_state(store, bid), "Purge")
    apply_delta(store, sid, bid, 1, [{"op": "combatant_defeat", "target": cid}], "rule", cfg)
    state = current_state(store, bid)
    assert len(parse_foe_tags("[foe | Purge | standard]", state)) == 1   # dead -> new is fine


def test_merge_baseline_skills_fills_absent_and_preserves_authored():
    merged = merge_baseline_skills({"stealth": 3})
    for sid in BASELINE_SKILLS:
        assert merged[sid] == 0
    assert merged["stealth"] == 3                        # authored rank preserved
    assert merge_baseline_skills({"perception": 4})["perception"] == 4   # authored baseline wins
    assert set(BASELINE_SKILLS) <= set(merge_baseline_skills({}))


def test_seeded_player_owns_the_baseline_floor():
    from aetherstate.creator import player_to_ops
    ops = player_to_ops({"name": "Diver", "skills": {"stealth": 2}}, _rpg_cfg())
    seed = next(o for o in ops if o["op"] == "player_seed")
    for sid in BASELINE_SKILLS:
        assert seed["card"]["skills"].get(sid) == 0
    assert seed["card"]["skills"].get("stealth") == 2
