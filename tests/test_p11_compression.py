"""Compression items 2-5 (plan doc 13, ratified 2026-07-09).

Item 2: [injection].briefing_style compact knob (+[KEY] legend on the contract; verbose
default renders exactly as before). Item 3: fact lifecycle — near-restatement supersede,
fact_retire (user/rule only), L10 premises skip retired, memory near-dupe guard. Item 4:
roll phase-out pinned (stale rolls never ride). Item 5: presence-scoping — absent NPCs'
physical/effect/drive detail stays ledger-only (bonds ride); active-quest detail top-3."""
from __future__ import annotations

from aetherstate import compose, linter
from aetherstate.config import Config
from aetherstate.prompts import rules_contract
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _rpg_cfg(style="verbose") -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.injection.briefing_style = style
    return cfg


def _session(cfg):
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="t-p11")
    return store, sid, bid


# ------------------------------ item 3: fact lifecycle --------------------------------
def test_fact_near_restatement_supersedes():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "reveal_fact", "learner": "Bean",
                                      "statement": "The harbor gate is sealed at night",
                                      "source": "witnessed"}], "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "reveal_fact", "learner": "Bean",
                                      "statement": "The harbor gate is sealed at night now",
                                      "source": "witnessed"}], "user", cfg)
    st = current_state(store, bid)
    facts = st["facts"]
    assert len(facts) == 2
    retired = [f for f in facts.values() if f.get("retired_turn") is not None]
    live = [f for f in facts.values() if f.get("retired_turn") is None]
    assert len(retired) == 1 and len(live) == 1
    assert retired[0]["statement"] == "The harbor gate is sealed at night"
    assert retired[0]["superseded_by"] in facts
    # DISTINCT facts about the same subject never auto-retire (precision over recall)
    apply_delta(store, sid, bid, 3, [{"op": "reveal_fact", "learner": "Bean",
                                      "statement": "The harbor gate was built by dwarves",
                                      "source": "told"}], "user", cfg)
    st = current_state(store, bid)
    assert sum(1 for f in st["facts"].values()
               if f.get("retired_turn") is None) == 2


def test_fact_retire_op_and_authority():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "reveal_fact", "learner": "Bean",
                                      "statement": "Greta owes the Iron Pact money",
                                      "source": "overheard"}], "user", cfg)
    r = apply_delta(store, sid, bid, 2, [{"op": "fact_retire",
                                          "statement": "Greta owes Iron Pact money"}],
                    "user", cfg)
    st = r.state
    assert all(f.get("retired_turn") is not None for f in st["facts"].values())
    # the model can never erase truth: extraction-source retire is REJECTED
    r2 = apply_delta(store, sid, bid, 3, [{"op": "fact_retire",
                                           "statement": "anything"}], "extraction", cfg)
    assert len(r2.quarantined) == 1 and not r2.applied


def test_l10_premises_skip_retired_facts():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "reveal_fact", "learner": "Bean",
                                      "statement": "The bridge is out",
                                      "source": "witnessed"}], "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "fact_retire",
                                      "statement": "The bridge is out"}], "user", cfg)
    prem = linter._ledger_premises(current_state(store, bid))
    assert not any("bridge" in s.lower() for _, s in prem)


def test_memory_near_dupe_guard():
    cfg = _rpg_cfg()
    store, sid, bid = _session(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "memory_event", "text": "World lore: the Spire hums at dusk each day"},
        {"op": "memory_event", "text": "World lore: the Spire hums at dusk each new day"},
        {"op": "memory_event", "text": "A completely different memory about breakfast"},
    ], "user", cfg)
    st = current_state(store, bid)
    assert len(st["memories"]) == 2               # the near-restatement never landed twice


# ------------------------------ items 2 + 5: render forms ------------------------------
def _render_state() -> dict:
    return {
        "meta": {"turn": 5},
        "scene": {"location_id": "docks"},
        "clock": {"day": 1},
        "chars": {"kira": {"obsessions": {}, "cravings": {}, "goals": []}},
        "player": {"bean": {"level": 1, "stats": {"STR": 10},
                            "soulmate": "mira"}},
        "entities": {
            "bean": {"name": "Bean", "kind": "player", "present": True},
            "greta": {"name": "Greta", "kind": "npc", "present": True},
            "kira": {"name": "Kira", "kind": "character", "present": False},
            "mira": {"name": "Mira", "kind": "character", "present": False},
        },
        "attributes": {},
        "poses": {"greta": {"base": "standing"}, "kira": {"base": "kneeling"}},
        "clothing": {"kira": {"cloak": {"state": "worn"}}},
        "effects": {"kira": {"e1": {"id": "e1", "name": "Poisoned", "valence": "negative"}},
                    "mira": {"e2": {"id": "e2", "name": "Cursed", "valence": "negative"}}},
        "quests": {
            "q1": {"name": "Old quest", "status": "active", "note": "an old note",
                   "updated_turn": 1},
            "q2": {"name": "Quest two", "status": "active", "note": "note2",
                   "updated_turn": 2},
            "q3": {"name": "Quest three", "status": "active", "note": "note3",
                   "updated_turn": 3},
            "q4": {"name": "Fresh quest", "status": "active",
                   "note": "the fresh detailed note", "updated_turn": 4},
        },
        "rolls": [{"spec": "1d20", "result": 11, "turn": 2}],   # STALE — must never render
    }


def test_scoping_absent_detail_stays_ledger_only():
    cfg = _rpg_cfg()
    h = compose.render_header(_render_state(), cfg)
    assert "kneeling" not in h and "cloak" not in h    # absent Kira: no [PHYSICAL] detail
    assert "Poisoned" not in h                         # absent Kira: no [EFFECTS] detail
    assert "Mira: Cursed(-)" in h                      # bond exception: the soulmate rides
    assert "standing" in h                             # present Greta renders as ever


def test_quest_detail_top3_older_actives_name_only():
    cfg = _rpg_cfg()
    h = compose.render_header(_render_state(), cfg)
    assert "Fresh quest (" in h or "Fresh quest —" in h or "fresh detailed note" in h
    assert "Old quest" in h and "an old note" not in h  # oldest active: name-only


def test_stale_roll_never_rides():
    cfg = _rpg_cfg()
    h = compose.render_header(_render_state(), cfg)
    assert "[ROLL]" not in h and "1d20" not in h        # item 4 pinned
    st = _render_state()
    st["rolls"].append({"spec": "2d6", "result": 7, "turn": 5})
    h2 = compose.render_header(st, cfg)
    assert "[ROLL] 2d6 = 7" in h2                       # the CURRENT turn's roll rides


def test_compact_forms_and_verbose_default():
    st = _render_state()
    hv = compose.render_header(st, _rpg_cfg())
    hc = compose.render_header(st, _rpg_cfg("compact"))
    assert "present: " in hv and "Stats: " in hv        # verbose = the 1.11 forms exactly
    assert "here: " in hc and "St: " in hc and "present: " not in hc
    assert len(hc) < len(hv)
    # the legend rides the CONTRACT (cacheable), only under compact
    assert "[KEY]" in rules_contract(_rpg_cfg("compact"))
    assert "[KEY]" not in rules_contract(_rpg_cfg())


def test_compact_is_pure_render_no_state_change():
    """The knob changes BYTES, never truth: same state in, same journal untouched."""
    st = _render_state()
    before = str(st)
    compose.render_header(st, _rpg_cfg("compact"))
    compose.render_header(st, _rpg_cfg())
    assert str(st) == before
