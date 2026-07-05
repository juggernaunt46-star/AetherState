"""P2 fixtures: state core — reducer, derived exposure, drives, checkpoint replay (02, 03 SS3.3/R4)."""
from __future__ import annotations

from aetherstate.config import Config
from aetherstate.state import (BIG_TURN, apply_delta, current_state, empty_state, reduce_state,
                               derived_exposure, validate_op)
from aetherstate.store import Store


def mk():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    return cfg, store, sid, bid


def user(store, sid, bid, turn, ops, cfg):
    return apply_delta(store, sid, bid, turn, ops, "user", cfg)


def test_exposure_derived_from_layers():
    """02 SS5.2 worked example: opened blouse + displaced bra -> breasts/nipples exposed."""
    cfg, store, sid, bid = mk()
    user(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kira"},
        {"op": "clothing", "char": "Kira", "item": "silk blouse", "action": "don",
         "covers": ["chest", "breasts", "shoulders", "back"]},
        {"op": "clothing", "char": "Kira", "item": "bra", "action": "don",
         "covers": ["breasts", "nipples"]},
        {"op": "clothing", "char": "Kira", "item": "skirt", "action": "don",
         "covers": ["hips", "thighs", "ass"]}], cfg)
    r = user(store, sid, bid, 1, [
        {"op": "clothing", "char": "Kira", "item": "silk blouse", "action": "open"},
        {"op": "clothing", "char": "Kira", "item": "bra", "action": "displace"}], cfg)
    exposed = derived_exposure(r.state, "kira")
    assert "breasts" in exposed and "nipples" in exposed
    assert "hips" not in exposed and "ass" not in exposed      # skirt still worn
    assert "chest" in exposed                                   # blouse opened


def test_contact_graph_start_stop():
    cfg, store, sid, bid = mk()
    r = user(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Dane"}, {"op": "entity_add", "name": "Kira"},
        {"op": "contact", "action": "start", "from_char": "Dane", "from_part": "hands",
         "to_char": "Kira", "to_part": "hips", "type": "gripping", "intensity": 2}], cfg)
    assert len(r.state["contacts"]) == 1
    r = user(store, sid, bid, 1, [
        {"op": "contact", "action": "stop", "from_char": "Dane", "from_part": "hands",
         "to_char": "Kira", "to_part": "hips", "type": "gripping"}], cfg)
    assert r.state["contacts"] == {}


def test_relationship_delta_clamped():
    """02 SS11: relationship_adj delta -30..30; dims clamp to their ranges."""
    cfg, store, sid, bid = mk()
    cfg.manual_override.enabled = True   # organic edit by user needs override (Q11)
    ops = [{"op": "entity_add", "name": "A"}, {"op": "entity_add", "name": "B"},
           {"op": "relationship_adj", "from_char": "A", "to_char": "B",
            "dimension": "trust", "delta": 90, "reason": "test"}]
    r = user(store, sid, bid, 0, ops, cfg)
    assert r.state["relationships"]["a->b"]["dims"]["trust"] == 30   # 90 clamped to +30
    for i in range(1, 5):
        r = user(store, sid, bid, i, [{"op": "relationship_adj", "from_char": "A",
                                       "to_char": "B", "dimension": "trust", "delta": 30,
                                       "reason": "t"}], cfg)
    assert r.state["relationships"]["a->b"]["dims"]["trust"] == 100  # cap


def test_time_advance_wraps_day_and_ramps_cravings():
    """03 R4 / 02 SS4.1: ramp on time_advance; withdrawal effects at thresholds; consume drops."""
    cfg, store, sid, bid = mk()
    cfg.manual_override.enabled = True
    user(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Vess"},
        {"op": "craving", "char": "Vess", "substance": "wine", "action": "adjust", "delta": 62}], cfg)
    # dependency high enough for withdrawal via repeated consumes
    for i in range(1, 26):
        user(store, sid, bid, i, [{"op": "craving", "char": "Vess", "substance": "wine",
                                   "action": "consume"}], cfg)
    st = current_state(store, bid)
    assert st["chars"]["vess"]["cravings"]["wine"]["dependency"] >= 50
    # night -> morning wraps the day and ramps craving level
    before = st["clock"]["day"]
    r = user(store, sid, bid, 26, [{"op": "time_advance", "to_time_of_day": "morning"}], cfg)
    assert r.state["clock"]["day"] == before + 1
    lvl = r.state["chars"]["vess"]["cravings"]["wine"]["level"]
    for i in range(27, 27 + 20):
        r = user(store, sid, bid, i, [{"op": "time_advance", "minutes": 60}], cfg)
    c = r.state["chars"]["vess"]["cravings"]["wine"]
    assert c["level"] > lvl and c["level"] >= 70
    assert "withdrawal:wine" in r.state["chars"]["vess"]["status_effects"]
    r = user(store, sid, bid, 47, [{"op": "craving", "char": "Vess", "substance": "wine",
                                    "action": "consume"}], cfg)
    assert r.state["chars"]["vess"]["cravings"]["wine"]["level"] <= c["level"] - 30


def test_flashback_tags_memories_and_freezes_clock():
    """08 B4: non-live scene -> memories tagged, physical/clock mutations quarantined (rule)."""
    cfg, store, sid, bid = mk()
    user(store, sid, bid, 0, [{"op": "entity_add", "name": "Mara"},
                              {"op": "scene_mode", "mode": "flashback"}], cfg)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "clock_tick", "minutes": 3},
        {"op": "memory_event", "text": "a memory from before", "participants": ["Mara"]}],
        "rule", cfg)
    assert any(q["reason"].startswith("scene is flashback") for q in r.quarantined)
    assert r.state["memories"][-1]["tags"] == ["flashback"]
    assert r.state["clock"]["minutes"] == 0


def test_checkpoint_replay_determinism():
    """03 SS3.3: state_at = checkpoint + replay; a mid-history query time-scrubs correctly."""
    cfg, store, sid, bid = mk()
    cfg.manual_override.enabled = True
    user(store, sid, bid, 0, [{"op": "entity_add", "name": "A"}], cfg)
    for t in range(1, 45):
        user(store, sid, bid, t, [{"op": "arousal", "char": "A", "set": t}], cfg)
    assert store.db.execute("SELECT COUNT(*) c FROM checkpoints").fetchone()["c"] >= 2
    full = store.state_at(bid, BIG_TURN, reduce_state, empty=empty_state())
    assert full["chars"]["a"]["arousal"]["arousal"] == 44
    mid = store.state_at(bid, 10, reduce_state, empty=empty_state())
    assert mid["chars"]["a"]["arousal"]["arousal"] == 10        # the inspector scrubber primitive


def test_reducer_survives_bad_journaled_op():
    st = reduce_state(empty_state(), [{"op": "arousal"}, {"nonsense": True},
                                      {"op": "scene_set", "location": "Tavern"}])
    assert st["scene"]["location_id"] == "Tavern"               # invariant 3


def test_validate_op_rejects_malformed():
    assert validate_op({"op": "clothing", "char": "A", "item": "x", "action": "shred"}) is None
    assert validate_op({"op": "contact", "action": "start", "from_char": "A", "to_char": "B",
                        "type": "levitating"}) is None
    assert validate_op({"op": "consent_signal", "from_char": "A", "to_char": "B",
                        "category": "vaginal", "signal": "grant"}) is not None
    assert validate_op("not a dict") is None
