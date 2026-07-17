"""Scene location truth must not manufacture travel causality.

Genesis and extraction can correct where the scene is anchored, but only Player/rule movement
authority may turn a location boundary into travel, time passage, or combat departure.  Historical
journals already carrying the baked movement marker remain replay-authoritative.
"""
from __future__ import annotations

import pytest

from aetherstate.config import Config
from aetherstate.state import apply_delta, combat_ops, current_state, world_ops
from aetherstate.store import Store


def _rpg_cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _runtime(*, path: str = ":memory:") -> tuple[Config, Store, str, str]:
    cfg = _rpg_cfg()
    store = Store(path)
    sid, bid = store.create_session(external_id="scene-reanchor-causality")
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "The Docks", "kind": "location"},
            {"op": "entity_add", "name": "Old Gate", "kind": "location"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {"hp": {"max": 12}, "resources": {}},
            },
            {"op": "scene_set", "location": "The Docks", "phase": "opening"},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    return cfg, store, sid, bid


@pytest.mark.parametrize("source", ["genesis", "extraction"])
def test_descriptive_reanchor_changes_location_without_movement_or_time(source: str):
    cfg, store, sid, bid = _runtime()

    reanchored = apply_delta(
        store,
        sid,
        bid,
        1,
        [{
            "op": "scene_set",
            "location": "Old Gate",
            "phase": "lull",
            "_prev_loc": "forged_origin",
        }],
        source,
        cfg,
    )

    assert not reanchored.quarantined
    assert reanchored.state["scene"]["location_id"] == "old_gate"
    assert "last_move" not in reanchored.state["scene"]
    assert "_prev_loc" not in reanchored.applied[0]
    assert world_ops(reanchored.state, reanchored.applied, clock_turns=0) == []
    assert reanchored.state["clock"]["time_of_day"] == "evening"


@pytest.mark.parametrize("source", ["user", "rule"])
def test_explicit_movement_authority_bakes_travel_and_pays_time(source: str):
    cfg, store, sid, bid = _runtime()

    moved = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "scene_set", "location": "Old Gate"}],
        source,
        cfg,
    )

    assert not moved.quarantined
    assert moved.applied[0]["_prev_loc"] == "the_docks"
    assert moved.state["scene"]["last_move"] == {
        "from": "the_docks",
        "to": "old_gate",
        "turn": 1,
    }
    consequences = world_ops(moved.state, moved.applied, clock_turns=0)
    assert consequences == [{"op": "time_advance", "to_time_of_day": "night"}]
    settled = apply_delta(store, sid, bid, 1, consequences, "rule", cfg)
    assert settled.state["clock"]["time_of_day"] == "night"


def test_extracted_player_move_keeps_code_owned_travel_fallback():
    cfg, store, sid, bid = _runtime()
    reported = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "move_entity", "entity": "Kael", "to_location": "Old Gate"}],
        "extraction",
        cfg,
    )

    consequences = world_ops(reported.state, reported.applied, clock_turns=0)
    assert consequences == [
        {"op": "scene_set", "location": "old_gate"},
        {"op": "time_advance", "to_time_of_day": "night"},
    ]
    settled = apply_delta(store, sid, bid, 1, consequences, "rule", cfg)
    assert settled.state["scene"]["last_move"] == {
        "from": "the_docks",
        "to": "old_gate",
        "turn": 1,
    }
    assert settled.state["clock"]["time_of_day"] == "night"


@pytest.mark.parametrize("source", ["genesis", "extraction"])
def test_descriptive_reanchor_cannot_close_active_combat(source: str):
    cfg, store, sid, bid = _runtime()
    opened = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "combatant_spawn", "name": "Bandit", "side": "enemy"}],
        "rule",
        cfg,
    )
    assert opened.state["combat"]["active"]

    reanchored = apply_delta(
        store,
        sid,
        bid,
        2,
        [{"op": "scene_set", "location": "Old Gate", "phase": "lull"}],
        source,
        cfg,
    )

    assert "_prev_loc" not in reanchored.applied[0]
    assert reanchored.state["combat"]["active"]
    assert not any(
        op["op"] == "combat_end"
        for op in combat_ops(reanchored.state, reanchored.applied, prepare_intent=False)
    )


def test_historical_journaled_movement_marker_reopens_unchanged(tmp_path):
    db_path = tmp_path / "legacy-scene-replay.sqlite3"
    _cfg, store, _sid, bid = _runtime(path=str(db_path))
    legacy = {
        "op": "scene_set",
        "location": "old_gate",
        "phase": "lull",
        "_prev_loc": "the_docks",
        "_canon": 1,
        "_turn": 1,
    }
    store.journal(bid, 1, 1, [legacy], "extraction")
    expected = current_state(store, bid)
    assert expected["scene"]["last_move"] == {
        "from": "the_docks",
        "to": "old_gate",
        "turn": 1,
    }
    store.close()

    reopened = Store(str(db_path))
    try:
        assert current_state(reopened, bid) == expected
    finally:
        reopened.close()
