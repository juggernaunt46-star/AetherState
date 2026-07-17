"""RPG-4 (the public contract): persistence, location registry + canonicalization, degradation
ladder, inspectors. Exit criteria covered here:
  - generation persists (no regeneration): a discovered place is created once, ever;
  - location-revisit canonicalization: any name variant resolves to the SAME row,
    aliases are learned, visits are counted;
  - wrong-merge guard: ambiguous short names mint a new row instead of guessing;
  - free-prose trim: a pasted description can't become a paragraph-long location id
    (live repro 2026-07-06: vael_cora_the_capital_city_of_the_realm_...);
  - `none` leak: a none session's journaled ops carry no RPG-4 keys and behave as before;
  - deterministic replay: canonicalization is baked at _enrich, never re-resolved;
  - degradation ladder: [specialization].contract="compact" selects the shrunk contract;
  - journal inspector route serves the activity feed.
"""
from __future__ import annotations


from aetherstate import discovery
from aetherstate.config import Config
from aetherstate.prompts import (DM_RULES_CONTRACT, DM_RULES_CONTRACT_COMPACT,
                                 rules_contract)
from aetherstate.state import (apply_delta, canonical_location, current_state, empty_state,
                               reduce_state)
from aetherstate.store import Store


def _rpg_cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _session(cfg, ext="t-rpg4"):
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=ext)
    return store, sid, bid


# ------------------------------ canonicalization core --------------------------------
def test_free_prose_location_trims_to_name_head():
    st = empty_state()
    raw = ("Vael'Cora, the capital city of the realm, a sprawling marble metropolis "
           "built atop the cliffs")
    loc_id, name, new = canonical_location(st, raw)
    assert new and loc_id == "vael_cora" and name == "Vael'Cora"


def test_revisit_any_variant_resolves_to_same_row():
    st = empty_state()
    st["entities"]["the_gilded_lantern"] = {"kind": "location", "name": "The Gilded Lantern",
                                            "aliases": [], "present": False}
    for variant in ("The Gilded Lantern", "the gilded lantern", "Gilded Lantern"):
        loc_id, _, new = canonical_location(st, variant)
        assert (loc_id, new) == ("the_gilded_lantern", False), variant


def test_wrong_merge_guard_ambiguity_mints_new():
    st = empty_state()
    for n in ("North Gate", "South Gate"):
        st["entities"][n.lower().replace(" ", "_")] = {"kind": "location", "name": n,
                                                       "aliases": [], "present": False}
    loc_id, _, new = canonical_location(st, "Gate")     # subset of BOTH -> never a guess
    assert new and loc_id == "gate"
    # unique PARENT resolves (changed 2026-07-09, Emberfall live: "Ashen Maw rim"-style
    # sub-area strings were minting twin locations every session — a unique ≥2-token
    # name-head containment is the parent, while true ambiguity still mints new above)
    loc_id2, _, new2 = canonical_location(st, "North Gate Plaza")
    assert (loc_id2, new2) == ("north_gate", False)
    loc_id3, _, new3 = canonical_location(st, "North")
    assert (loc_id3, new3) == ("north_gate", False)      # unique 1-token subset resolves


def test_scene_set_canonicalizes_under_rpg_and_counts_visits():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    long_raw = "Vael'Cora, the capital city of the realm, a sprawling marble metropolis"
    apply_delta(store, sid, bid, 1, [{"op": "scene_set", "location": long_raw}], "user", cfg)
    st = current_state(store, bid)
    assert st["scene"]["location_id"] == "vael_cora"
    ent = st["entities"]["vael_cora"]
    assert ent["kind"] == "location" and ent["name"] == "Vael'Cora"
    assert ent["visits"] == 1
    # leave, then revisit under a bare variant: same row, visits=2, no new entity
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "The Docks"}], "user", cfg)
    apply_delta(store, sid, bid, 3, [{"op": "scene_set", "location": "Vael'Cora"}], "user", cfg)
    st = current_state(store, bid)
    assert st["scene"]["location_id"] == "vael_cora"
    assert st["entities"]["vael_cora"]["visits"] == 2
    locs = [e for e in st["entities"].values() if e.get("kind") == "location"]
    assert len(locs) == 2                                # vael_cora + the_docks, no dupes
    # re-setting the SAME location is not a boundary: visits unchanged
    apply_delta(store, sid, bid, 4, [{"op": "scene_set", "location": "vael_cora"}], "user", cfg)
    assert current_state(store, bid)["entities"]["vael_cora"]["visits"] == 2


def test_scene_set_learns_alias_variants():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "entity_add", "name": "The Gilded Lantern",
                                      "kind": "location"}], "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "Lantern"}], "user", cfg)
    st = current_state(store, bid)
    assert st["scene"]["location_id"] == "the_gilded_lantern"   # unique-subset resolution
    assert "Lantern" in st["entities"]["the_gilded_lantern"]["aliases"]  # canon self-improves


def test_none_session_untouched_by_rpg4():
    cfg = Config()
    assert cfg.specialization.name == "none"
    store, sid, bid = _session(cfg)
    raw = "Vael'Cora, the capital city of the realm, a sprawling marble metropolis"
    res = apply_delta(store, sid, bid, 1, [{"op": "scene_set", "location": raw}], "user", cfg)
    op = res.applied[0]
    assert "_canon" not in op and "_loc_create" not in op and "_loc_alias" not in op
    st = current_state(store, bid)
    assert st["scene"]["location_id"] == raw             # exact pre-RPG-4 behaviour
    assert "vael_cora" not in st["entities"]
    row = store.db.execute("SELECT ops FROM ops_journal WHERE branch_id=?", (bid,)).fetchone()
    assert "_canon" not in row["ops"]                    # journal bytes carry no fingerprint


def test_replay_reproduces_canonical_state():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "scene_set",
                                      "location": "The Gilded Lantern, a smoky dockside inn"}],
                "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "Gilded Lantern"}],
                "user", cfg)
    live = current_state(store, bid)
    replayed = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replayed["scene"]["location_id"] == live["scene"]["location_id"]
    assert replayed["entities"]["the_gilded_lantern"] == live["entities"]["the_gilded_lantern"]


# ------------------------------ location discovery (persist once) --------------------
def test_location_discovery_persists_once():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    text = "You duck into the Gilded Lantern as the rain starts."
    st = current_state(store, bid)
    assert discovery.observe_locations(store, cfg, sid, bid, 1, text, st) == []  # 1 turn: not yet
    st = current_state(store, bid)
    created = discovery.observe_locations(store, cfg, sid, bid, 2, text, st)
    assert created == ["Gilded Lantern"]                 # >=2 turns of evidence -> persisted
    st = current_state(store, bid)
    assert st["entities"]["gilded_lantern"]["kind"] == "location"
    # third mention, any variant: already canonical -> NEVER regenerated
    again = discovery.observe_locations(store, cfg, sid, bid, 3,
                                        "Back at the Gilded Lantern.", current_state(store, bid))
    assert again == []
    locs = [e for e in current_state(store, bid)["entities"].values()
            if e.get("kind") == "location"]
    assert len(locs) == 1


def test_scan_locations_cues():
    got = discovery.scan_locations("She walked into the Gilded Lantern near Harborfall.")
    assert "Gilded Lantern" in got and "Harborfall" in got
    assert discovery.scan_locations("He spoke to Mira softly.") == set()   # 'to' is not a cue


# ------------------------------ degradation ladder (D7) ------------------------------
def test_compact_contract_knob():
    cfg = _rpg_cfg()
    assert rules_contract(cfg).startswith(DM_RULES_CONTRACT[:40])          # default: full
    cfg.specialization.contract = "compact"
    rc = rules_contract(cfg)
    assert rc.startswith("[RULES]") and DM_RULES_CONTRACT_COMPACT in rc
    assert len(DM_RULES_CONTRACT_COMPACT) < len(DM_RULES_CONTRACT) / 2     # actually shrunk
    assert "What will you do?" in rc                                       # non-negotiables kept
    assert "[DIRECTIVE]" in rc


# ------------------------------ inspector route --------------------------------------
async def test_journal_route_serves_activity_feed(client):
    await client.post("/aether/session/rpg4-t/world",
                      json={"world": {"genre": "high_fantasy", "name": "Aldreth"}})
    r = await client.get("/aether/session/rpg4-t/journal?limit=10")
    assert r.status_code == 200
    d = r.json()
    assert d["entries"] and all({"turn", "source", "op", "brief"} <= set(e) for e in d["entries"])
    assert any(e["op"] == "entity_add" for e in d["entries"])
    assert (await client.get("/aether/session/nope-t/journal")).status_code == 404


async def test_patch_after_creator_save_is_not_shadowed(client):
    """2026-07-06 live repro: on a fresh creator-first session (head −1, creator/genesis
    checkpoint at 0) a Console/PATCH edit landed at turn −1, reported applied=1, and was
    silently shadowed by the checkpoint replay horizon. _user_ops now lands past it."""
    await client.post("/aether/session/shadow-t/world",
                      json={"world": {"genre": "high_fantasy", "name": "Aldreth",
                                      "locations": ["Harborfall"]}})
    r = await client.patch("/aether/session/shadow-t/state",
                           json={"path": "scene.location", "value": "The Sunken Library"})
    assert r.status_code == 200 and r.json()["applied"] == 1
    st = (await client.get("/aether/session/shadow-t/state")).json()["state"]
    assert st["scene"]["location_id"] == "The Sunken Library"   # visible, not shadowed
