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
from aetherstate.state import apply_delta, current_state, empty_state
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record

_WORLD = {"name": "Gallowmere", "genre": "dark_fantasy", "time": "night",
          "opening_scene": "Fog on the causeway.", "opening_quest": "Find the tithe collector.",
          "locations": ["Gallow Hill"]}
_PLAYER = {"name": "Kestrel", "concept": "memory-thief", "sex": "female",
           "appearance": "Wiry, hooded, ink-stained fingers and a scar through one brow.",
           "skills": {"stealth": 2, "persuasion": 1},
           "gear": ["worn oilskin coat", "set of lockpicks"]}


def test_hud_plainly_separates_said_believed_fact_and_world_event():
    cfg = Config()
    cfg.specialization.name = "rpg"
    world_id = "world_" + "a" * 32
    state = empty_state()
    state["player"] = {"player": {"name": "Player"}}
    state["world_identity"] = {"world_id": world_id}
    state["claims"] = [{
        "fingerprint": "sha256:" + "1" * 64, "proposition_id": "prop:gate",
        "proposition": "the gate is open", "speaker": "Mara", "source": "npc:mara",
        "claim_class": "report", "proposition_polarity": "positive", "modality": "asserted",
    }]
    state["beliefs"] = {"player|prop:gate": {
        "holder": "player", "proposition_id": "prop:gate", "stance": "doubts", "source": "claim:mara",
    }}
    state["facts"] = {"fact_gate": {
        "proposition_id": "prop:bell", "statement": "the bell rang", "authority": "rule",
        "cause": "settlement:bell", "status": "accepted",
    }}
    state["world_events"] = [build_world_event_record(
        event_id="event.gate", world_id=world_id, session_id="session-one", branch_id="branch-one",
        turn=1, game_time=0, cause_id="hidden-cause", cause_authority="rule",
        cause_visibility="hidden", affected_domains=["world", "hud"], description="the gate opened",
        effects=[{"adapter": "world.circumstance/1", "domain": "world", "subject": "gate",
                  "field": "circumstance", "value": "open", "supported": True, "lore": ""}],
    )]
    knowledge = hud.hud_view(state, cfg)["knowledge"]
    assert knowledge["claims"][0]["proposition"] == "the gate is open"
    assert knowledge["epistemics"][0]["stance"] == "doubts"
    assert knowledge["facts"][0]["statement"] == "the bell rang"
    assert knowledge["events"][0]["cause"] == "cause not known"


def test_player_hud_applies_typed_overlay_and_excludes_private_or_untyped_state():
    """One adversarial projection proves both supported UI effects and the privacy floor."""
    import json

    cfg = Config()
    cfg.specialization.name = "rpg"
    world_id = "world_" + "b" * 32
    state = empty_state()
    state.update({
        "meta": {"turn": 6},
        "world_identity": {"world_id": world_id},
        "world_event_branch_id": "branch-ui",
        "scene": {"location_id": "harbor", "phase": "rising"},
        "clock": {"day": 2, "time_of_day": "night", "minutes": 6},
        "entities": {
            "player": {"name": "Player", "kind": "actor", "present": True},
            "mara": {"name": "Mara", "kind": "npc", "present": True},
            "hidden_npc": {"name": "Hidden NPC", "kind": "npc", "present": True},
            "other_a": {"name": "Other A", "kind": "npc"},
            "other_b": {"name": "Other B", "kind": "npc"},
            "guild": {"name": "Harbor Guild", "kind": "faction"},
        },
        "player": {"player": {
            "level": 1, "hp": {"cur": 10, "max": 10}, "stats": {"CUN": 12},
            "skills": {"persuasion": 2}, "abilities": [],
        }},
        "chars": {
            "hidden_npc": {
                "arousal": {"arousal": 77},
                "obsessions": {"secret": {"intensity": 90, "target_kind": "concept"}},
                "goals": [{"text": "PRIVATE_NPC_GOAL_SENTINEL"}],
            },
        },
        "quests": {
            "closed": {"name": "Closed Road", "status": "active"},
            "console_hidden": {"name": "Console Secret", "status": "active"},
        },
        "relationships": {
            "player->mara": {"dims": {"trust": 25}},
            "other_a->other_b": {"dims": {"trust": 90}},
        },
        "affinity": {
            "player->mara": {"value": 20},
            "player->guild": {"value": 10},
        },
        "factions": {"guild": {"circumstances": {"PRIVATE_FACTION_SENTINEL": True}}},
        "consent": {
            "player|mara|romance": {"level": "granted"},
            "other_a|other_b|secret": {"level": "hard_limit"},
        },
        "memories": [{"turn": 5, "text": "RAW_MEMORY_PROSE_SENTINEL"}],
        "world": {"HIDDEN_OBJECTIVE_FLAG_SENTINEL": True},
        "fronts": {
            "hidden_front": {"name": "HIDDEN_FRONT_SENTINEL", "revealed": False},
        },
        "claims": [{
            "claim_id": "claim.safe", "proposition_id": "prop:safe",
            "proposition": "The bell rang.", "speaker": "Mara", "visibility": "public",
            "claim_class": "report", "proposition_polarity": "positive",
            "raw_prose": "RAW_CLAIM_PROSE_SENTINEL",
        }],
        "beliefs": {"hidden_npc|prop:private": {
            "belief_id": "belief.private", "holder": "hidden_npc",
            "proposition_id": "prop:private", "statement": "PRIVATE_NPC_BELIEF_SENTINEL",
            "stance": "knows", "visibility": "actor_scoped",
            "scoped_actors": ["hidden_npc"],
        }},
        "facts": {"hidden": {
            "proposition_id": "prop:hidden", "statement": "HIDDEN_FACT_SENTINEL",
            "status": "accepted", "visibility": "hidden",
        }},
    })
    effects = [
        {"adapter": "world.circumstance/1", "domain": "world", "subject": "world",
         "field": "circumstance", "value": "A storm blocks the bay.", "supported": True},
        {"adapter": "location.circumstance/1", "domain": "location", "subject": "harbor",
         "field": "circumstance", "value": "The piers are flooded.", "supported": True},
        {"adapter": "actor.condition/1", "domain": "actor", "subject": "mara",
         "field": "condition", "value": "Mara is soaked.", "supported": True},
        {"adapter": "faction.circumstance/1", "domain": "faction", "subject": "guild",
         "field": "circumstance", "value": "The guild hall is closed.", "supported": True},
        {"adapter": "quest.availability/1", "domain": "quest", "subject": "closed",
         "field": "available", "value": False, "supported": True},
        {"adapter": "capability.eligibility/1", "domain": "capability_eligibility",
         "subject": "capability:persuasion", "field": "eligible", "value": False,
         "supported": True},
        {"adapter": "relationship.modifier/1", "domain": "relationship",
         "subject": "relationship:player:mara", "field": "modifier", "value": -12,
         "supported": True},
        {"adapter": "reputation.modifier/1", "domain": "reputation", "subject": "guild",
         "field": "modifier", "value": 15, "supported": True},
        {"adapter": "hud.visibility/1", "domain": "hud", "subject": "hud:actor:hidden_npc",
         "field": "visible", "value": False, "supported": True},
        {"adapter": "console.visibility/1", "domain": "console",
         "subject": "console:quest:console_hidden", "field": "visible", "value": False,
         "supported": True},
    ]
    for effect in effects:
        effect["lore"] = ""
    state["world_events"] = [build_world_event_record(
        event_id="event.ui", world_id=world_id, session_id="session-ui",
        branch_id="branch-ui", turn=4, game_time=4, cause_id="HIDDEN_CAUSE_SENTINEL",
        cause_authority="rule", cause_visibility="hidden",
        affected_domains=[effect["domain"] for effect in effects],
        description="The storm changed the harbor.", effects=effects,
    )]

    view = hud.hud_view(state, cfg)
    assert view["scene"]["world_circumstance"] == "A storm blocks the bay."
    assert view["scene"]["location_circumstance"] == "The piers are flooded."
    assert next(row for row in view["cast"] if row["eid"] == "mara")["world_condition"] \
        == "Mara is soaked."
    assert all(row["eid"] != "hidden_npc" for row in view["cast"])
    assert "Hidden NPC" not in view["scene"]["present"]
    assert next(row for row in view["quests"] if row["id"] == "closed")["available"] is False
    assert next(row for row in view["players"][0]["skills"] if row["id"] == "persuasion")[
        "eligible"
    ] is False
    assert view["relationships"][0]["world_modifier"] == -12
    faction = next(row for row in view["factions"] if row["id"] == "guild")
    assert faction["world_circumstance"] == "The guild hall is closed."
    assert faction["reputation_modifier"] == 15
    assert all(row.get("a_id") == "player" or row.get("b_id") == "player"
               for row in view["relationships"])
    assert all(row.get("a_id") == "player" or row.get("b_id") == "player"
               for row in view["consent"])
    assert view["memories"] == []
    assert view["world_flags"] == {"world_circumstance": "A storm blocks the bay."}
    assert view["surface_visibility"]["hud"] == {"actor:hidden_npc": False}
    assert view["surface_visibility"]["console"] == {"quest:console_hidden": False}

    raw = view["player_safe_raw"]
    assert raw["schema"] == "aetherstate-player-inspection/1"
    assert all(row["id"] != "console_hidden" for row in raw["quests"])
    serialized = json.dumps(view)
    for sentinel in (
        "PRIVATE_NPC_GOAL_SENTINEL", "PRIVATE_NPC_BELIEF_SENTINEL",
        "RAW_MEMORY_PROSE_SENTINEL", "RAW_CLAIM_PROSE_SENTINEL", "HIDDEN_FACT_SENTINEL",
        "HIDDEN_OBJECTIVE_FLAG_SENTINEL", "PRIVATE_FACTION_SENTINEL", "HIDDEN_FRONT_SENTINEL",
        "HIDDEN_CAUSE_SENTINEL",
    ):
        assert sentinel not in serialized


def test_shared_hud_fails_closed_on_actor_scoped_knowledge_with_multiple_players():
    import json

    state = empty_state()
    state["player"] = {"p1": {"name": "One"}, "p2": {"name": "Two"}}
    state["beliefs"] = {"p1|prop:secret": {
        "belief_id": "belief.p1", "holder": "p1", "proposition_id": "prop:secret",
        "statement": "PLAYER_ONE_PRIVATE_SENTINEL", "stance": "knows",
        "visibility": "actor_scoped", "scoped_actors": ["p1"],
    }}

    view = hud.hud_view(state, Config())
    assert view["knowledge"]["epistemics"] == []
    assert "PLAYER_ONE_PRIVATE_SENTINEL" not in json.dumps(view)


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
                    "governs": ["map", "compare echoes"],
                    "desc": "Separates a ruin's recent echoes from its older history.",
                    "cost": {"ash_focus": 2},
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
    custom_skill = next(s for s in p["skills"] if s["id"] == "ruin_echo_mapping")
    assert custom_skill["cost"] == "Ash Focus 2"
    assert custom_skill["desc"] == \
        "Separates a ruin's recent echoes from its older history."
    assert custom_skill["governs"] == ["map", "compare echoes"]
    assert next(a for a in p["abilities"] if a["id"] == "hookstep_surge")["cost"] == \
        "Ash Focus 3"
    assert "Ash Focus 4/8" in render_header(state, cfg)


def test_hud_bounds_player_definition_and_gear_prose_without_reading_private_actor_state():
    cfg = Config()
    cfg.specialization.name = "rpg"
    private = "PRIVATE_NPC_GOAL_MUST_NOT_REACH_PLAYER_PRESENTATION"
    long_prose = "D" * 5000
    state = {
        "meta": {"turn": 1},
        "entities": {
            "player": {"name": "Player", "kind": "actor"},
            "npc": {"name": "NPC", "kind": "npc"},
        },
        "player": {"player": {
            "stats": {"CUN": 10},
            "skills": {"witness_craft": 1},
            "abilities": ["measured_recall"],
            "defs": {
                "skills": {"witness_craft": {
                    "name": "Witness Craft", "keyed_stat": "CUN",
                    "desc": long_prose,
                    "governs": [f"verb-{i}-" + "x" * 100 for i in range(20)] + [private, 7],
                }},
                "abilities": {"measured_recall": {
                    "name": "Measured Recall", "kind": "passive",
                    "effect": long_prose, "desc": long_prose,
                }},
            },
        }},
        "chars": {"npc": {"goals": [{"text": private}]}},
        "items": {
            "seal": {
                "name": "Witness Seal", "owner": "player", "loc": "gear:accessory1",
                "slot": "accessory1", "type": "accessory", "aura": long_prose,
            },
            "kit": {
                "name": "Field Kit", "owner": "player", "loc": "inventory:loose",
                "type": "tool", "aura": long_prose,
            },
        },
        "gear": {"player": {"accessory1": "seal"}},
        "inventory": {"player": {"loose": ["kit"]}},
    }

    view = hud.hud_view(state, cfg)
    skill = view["players"][0]["skills"][0]
    ability = view["players"][0]["abilities"][0]
    gear = view["players"][0]["gear"][0]
    stowed = view["players"][0]["stowed_gear"][0]["items"][0]

    assert len(skill["desc"]) == 4000 and skill["desc"].endswith("…")
    assert len(skill["governs"]) == 12
    assert all(isinstance(value, str) and len(value) <= 80 for value in skill["governs"])
    assert len(ability["effect"]) == 4000 and len(ability["desc"]) == 4000
    assert len(gear["aura"]) == 4000
    assert len(stowed["aura"]) == 4000
    assert private not in str(view)


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
