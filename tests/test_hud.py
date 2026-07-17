"""Player-facing HUD view (2026-07-07): hud.hud_view + the GET /session/{sid}/hud route.

The HUD is the ONE resolved projection both the SillyTavern extension window and the web
Console render, so these assert the payload the human actually sees: appearance (a field that
did not exist before), effective skill mods, resources, statuses, drives, gear/inventory, scene,
quests. Plus fail-open on empty state, the route end-to-end, and that a `none` session leaks
nothing to the wire (the HUD is a control-route read, off the relay)."""
from __future__ import annotations

from aetherstate import creator, hud
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store

_WORLD = {"name": "Gallowmere", "genre": "dark_fantasy", "time": "night",
          "opening_scene": "Fog on the causeway.", "opening_quest": "Find the tithe collector.",
          "locations": ["Gallow Hill"]}
_PLAYER = {"name": "Kestrel", "concept": "memory-thief", "sex": "female",
           "appearance": "Wiry, hooded, ink-stained fingers and a scar through one brow.",
           "skills": {"stealth": 2, "persuasion": 1},
           "gear": ["worn oilskin coat", "set of lockpicks"]}


def _rpg_session():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="hud-t")
    apply_delta(store, sid, bid, 0, creator.world_to_ops(_WORLD), "user", cfg)
    apply_delta(store, sid, bid, 1, creator.player_to_ops(_PLAYER, cfg), "user", cfg)
    return cfg, store, bid


def test_hud_view_resolves_the_player():
    cfg, store, bid = _rpg_session()
    v = hud.hud_view(current_state(store, bid), cfg)
    assert v["spec"] == "rpg"
    assert v["scene"]["location"] and v["scene"]["time_of_day"] == "night"
    p = v["players"][0]
    assert p["name"] == "Kestrel"
    assert "scar" in p["appearance"]                       # appearance surfaced (was missing)
    assert p["hp"]["max"] and "stamina" in p["resources"]
    assert any(s["key"] == "DEX" for s in p["stats"])      # stats carry modifiers
    assert all("mod" in s for s in p["stats"])
    labels = {s["label"].lower(): s["mod"] for s in p["skills"]}
    assert "stealth" in labels and isinstance(labels["stealth"], int)   # EFFECTIVE mod, resolved
    # starting gear became instances: the worn coat auto-equips onto the paper-doll, the
    # lockpicks (a tool) stow as gear — neither is "inventory" (Bean 2026-07-07 gear split).
    equipped = [g["name"] for g in p["gear"]]
    stowed = [i["name"] for c in p.get("stowed_gear", []) for i in c["items"]]
    assert "worn oilskin coat" in equipped
    assert "set of lockpicks" in stowed
    assert any(q["name"] for q in v["quests"])             # opening quest visible


def test_hud_labels_custom_resources_and_their_skill_costs():
    cfg = Config()
    cfg.specialization.name = "rpg"
    state = {
        "meta": {"turn": 3},
        "entities": {"sable": {"name": "Sable Quill"}},
        "player": {"sable": {
            "level": 1,
            "hp": {"cur": 20, "max": 20},
            "resources": {
                "ash_focus": {"name": "Ash Focus", "cur": 4, "max": 8,
                              "color": "#B56CFF"},
                "unsafe": {"name": "Unsafe Pool", "cur": 1, "max": 2,
                           "color": "red;display:none"},
            },
            "stats": {"CUN": 12},
            "skills": {"ruin_echo_mapping": 2},
            "abilities": ["hookstep_surge"],
            "defs": {
                "skills": {"ruin_echo_mapping": {
                    "name": "Ruin Echo Mapping", "keyed_stat": "CUN", "base_mod": 0,
                    "governs": ["map"], "cost": {"ash_focus": 2},
                }},
                "abilities": {"hookstep_surge": {
                    "name": "Hookstep Surge", "kind": "active", "mechanic": "surge",
                    "applies_to": "ruin_echo_mapping", "cost": {"ash_focus": 3},
                }},
            },
        }},
    }

    p = hud.hud_view(state, cfg)["players"][0]
    assert p["resources"]["ash_focus"] == {
        "name": "Ash Focus", "cur": 4, "max": 8, "color": "#b56cff",
    }
    assert "color" not in p["resources"]["unsafe"]
    assert next(s for s in p["skills"] if s["id"] == "ruin_echo_mapping")["cost"] == \
        "Ash Focus 2"
    assert next(a for a in p["abilities"] if a["id"] == "hookstep_surge")["cost"] == \
        "Ash Focus 3"
    assert "Ash Focus 4/8" in render_header(state, cfg)


def test_appearance_persists_as_attribute():
    cfg, store, bid = _rpg_session()
    st = current_state(store, bid)
    assert (st.get("attributes", {}).get("kestrel", {}) or {}).get("appearance")
    # and it flows into the resolved view
    assert hud.hud_view(st, cfg)["players"][0]["appearance"]


def test_hud_view_fail_open_on_empty():
    v = hud.hud_view({}, Config())
    assert v["players"] == [] and v["quests"] == [] and "spec" in v
    assert v["scene"]["location"] == "" and not v["scene"]["present"]
    import json
    assert json.loads(json.dumps(v))                       # JSON-serializable


async def test_hud_route_end_to_end(client):
    await client.post("/aether/specialization", json={"name": "rpg"})
    await client.post("/aether/session/hud-r/world", json={"world": _WORLD})
    await client.post("/aether/session/hud-r/player", json={"player": _PLAYER})
    r = await client.get("/aether/session/hud-r/hud")
    assert r.status_code == 200
    body = r.json()
    assert body["spec"] == "rpg" and body["players"]
    p = body["players"][0]
    assert p["name"] == "Kestrel" and "scar" in p["appearance"]
    assert p["skills"] and (p["gear"] or p["stowed_gear"])   # gear split: coat equipped, picks stowed


async def test_hud_route_none_session_has_no_player(client):
    """Under `none` the route still answers (off the relay) but tracks no player — and the
    wire stays byte-identical (no RPG blocks in the state payload)."""
    await client.post("/aether/session/hud-none/world", json={"world": _WORLD})
    r = await client.get("/aether/session/hud-none/hud")
    assert r.status_code == 200
    assert r.json()["spec"] == "none" and r.json()["players"] == []


# ---- editable HUD backing: the stat_spend op + op ids in the payload (2026-07-07) ----
def test_stat_spend_raises_a_stat_and_is_privileged():
    from aetherstate.state import authority_violation, validate_op
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="ss-spend")
    apply_delta(store, sid, bid, 0,
                creator.player_to_ops({"name": "Ada", "skills": {"stealth": 1}}, cfg), "user", cfg)
    before = current_state(store, bid)["player"]["ada"]["stats"]["STR"]
    apply_delta(store, sid, bid, 1, [{"op": "level_up", "char": "Ada"}], "user", cfg)  # grants 1 pt
    r = apply_delta(store, sid, bid, 2, [{"op": "stat_spend", "char": "Ada", "stat": "STR"}], "user", cfg)
    p = current_state(store, bid)["player"]["ada"]
    assert p["stats"]["STR"] == before + 1 and int(p.get("stat_points", 0)) == 0 and len(r.applied) == 1
    # spending with no banked points is a transactional reject (visible, nothing journaled)
    r2 = apply_delta(store, sid, bid, 3, [{"op": "stat_spend", "char": "Ada", "stat": "STR"}], "user", cfg)
    assert len(r2.applied) == 0 and r2.quarantined
    # extraction may never spend (privileged, like progression)
    assert authority_violation({"op": "stat_spend", "char": "Ada", "stat": "STR"},
                               "extraction", current_state(store, bid), cfg)
    assert validate_op({"op": "stat_spend", "char": "Ada", "stat": "STR"}) is not None


def test_stat_spend_is_replay_pure():
    from aetherstate.state import empty_state, reduce_state
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="ss-replay")
    apply_delta(store, sid, bid, 0, creator.player_to_ops({"name": "Ada"}, cfg), "user", cfg)
    apply_delta(store, sid, bid, 1, [{"op": "level_up", "char": "Ada"}], "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "stat_spend", "char": "Ada", "stat": "STR"}], "user", cfg)
    live = current_state(store, bid)["player"]["ada"]
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())["player"]["ada"]
    assert replay == live                                  # baked _max -> replay reproduces it


def test_hud_exposes_op_ids_for_controls():
    cfg, store, bid = _rpg_session()
    p = hud.hud_view(current_state(store, bid), cfg)["players"][0]
    ids = [g["iid"] for g in p["gear"]] + \
          [i["iid"] for c in (p.get("stowed_gear", []) + p["inventory"]) for i in c["items"]]
    assert ids and all(ids)                                # every tracked item has an instance id


# ---- comprehensive view: effect kind/disease + the whole cast (2026-07-07) ----
def _rpg_store(ext):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=ext)
    return cfg, store, sid, bid


def test_hud_effect_kind_labels_status_condition_disease():
    cfg, store, sid, bid = _rpg_store("eff")
    apply_delta(store, sid, bid, 0, creator.player_to_ops({"name": "Ada"}, cfg), "user", cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "effect_add", "char": "Ada", "effect": "diseased", "note": "swamp fever"},
        {"op": "effect_add", "char": "Ada", "effect": "bleeding"},
        {"op": "effect_add", "char": "Ada", "effect": "blessed"}], "user", cfg)
    p = hud.hud_view(current_state(store, bid), cfg)["players"][0]
    kinds = {e["name"]: e["kind_label"] for e in p["effects"]}
    assert kinds.get("Diseased") == "Disease"        # a condition flavored as illness reads as Disease
    assert kinds.get("Bleeding") == "Status"
    assert kinds.get("Blessed") == "Condition"
    assert any(e["note"] == "swamp fever" for e in p["effects"])


def test_hud_cast_surfaces_the_whole_tracked_cast():
    cfg, store, sid, bid = _rpg_store("cast")
    apply_delta(store, sid, bid, 0,
                creator.world_to_ops({"name": "W", "npcs": [{"name": "The Warden"}]}), "user", cfg)
    apply_delta(store, sid, bid, 1, creator.player_to_ops({"name": "Ada"}, cfg), "user", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "presence", "entity": "The Warden", "present": True},
        {"op": "effect_add", "char": "The Warden", "effect": "poisoned"}], "user", cfg)
    v = hud.hud_view(current_state(store, bid), cfg)
    warden = [c for c in v["cast"] if c["name"] == "The Warden"]
    assert warden and warden[0]["present"]           # the NPC is in the cast, present
    assert any(e["name"] == "Poisoned" and e["kind_label"] == "Status"
               for e in warden[0]["effects"])        # with its status surfaced + kind-labelled
