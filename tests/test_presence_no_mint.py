"""Regression (Bean bug, 2026-07-08): presence & move_entity REFER to a known entity and
never MINT one. A scene/present tag naming a PLACE, a SKILL, or a custom-skill could be
conjured into a present 'character' that polluted the cast and the LLM briefing (often flagged
'here'); presence by display name also twinned a known NPC ('Marla' vs 'marla'). Entity
creation is the privileged, evidence-gated discovery path — these ops only refer to it."""
from __future__ import annotations

from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _mk():
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    return Config(), store, sid, bid


def test_presence_and_move_never_mint_a_place_or_skill():
    cfg, store, sid, bid = _mk()
    apply_delta(store, sid, bid, 1,
                [{"op": "presence", "entity": "Arinvale", "present": True}], "extraction", cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "move_entity", "entity": "Stormcall", "to_location": "tower"}],
                "extraction", cfg)
    ents = current_state(store, bid)["entities"]
    assert ents == {}, f"a place/skill was minted as a present character: {list(ents)}"


def test_presence_by_display_name_does_not_twin_the_entity():
    cfg, store, sid, bid = _mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "entity_add", "name": "Marla"},
        {"op": "presence", "entity": "Marla", "present": True},   # display name, not the slug
    ], "user", cfg)
    ents = current_state(store, bid)["entities"]
    assert list(ents) == ["marla"], f"presence twinned the entity: {list(ents)}"
    assert ents["marla"]["present"] is True


def test_present_tag_toggles_a_known_npc_added_by_display_name():
    cfg, store, sid, bid = _mk()
    apply_delta(store, sid, bid, 1,
                [{"op": "entity_add", "name": "The Warden", "kind": "npc"}], "user", cfg)
    apply_delta(store, sid, bid, 2,
                [{"op": "presence", "entity": "The Warden", "present": True}], "extraction", cfg)
    ents = current_state(store, bid)["entities"]
    assert list(ents) == ["the_warden"], f"present-tag twinned the NPC: {list(ents)}"
    assert ents["the_warden"]["present"] is True


def test_effect_by_display_name_lands_on_the_canonical_row():
    cfg, store, sid, bid = _mk()
    apply_delta(store, sid, bid, 1,
                [{"op": "entity_add", "name": "The Warden", "kind": "npc"}], "user", cfg)
    apply_delta(store, sid, bid, 2,
                [{"op": "effect_add", "char": "The Warden", "effect": "poisoned"}], "extraction", cfg)
    effs = current_state(store, bid).get("effects") or {}
    assert list(effs) == ["the_warden"], f"status keyed off a phantom entity: {list(effs)}"


def test_user_source_presence_move_never_auto_create():
    # source='user' (OOC / Console / PATCH) used to AUTO-CREATE unknown refs as kind=character;
    # presence/move must be refer-only there too — dropped, not minted.
    cfg, store, sid, bid = _mk()
    r = apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Arinvale", "present": True},
        {"op": "move_entity", "entity": "Stormcall", "to_location": "tower"},
    ], "user", cfg)
    assert r.applied == [], "a place/skill was auto-created via a user-source scene op"
    assert current_state(store, bid)["entities"] == {}


def test_presence_never_stages_a_location_or_faction():
    cfg, store, sid, bid = _mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "entity_add", "name": "Highmoor", "kind": "location"},
        {"op": "entity_add", "name": "The Ash Order", "kind": "faction"},
    ], "user", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "presence", "entity": "Highmoor", "present": True},
        {"op": "presence", "entity": "The Ash Order", "present": True},
    ], "extraction", cfg)
    ents = current_state(store, bid)["entities"]
    assert ents["highmoor"].get("present") is not True, "a location was staged as present"
    assert ents["the_ash_order"].get("present") is not True, "a faction was staged as present"
