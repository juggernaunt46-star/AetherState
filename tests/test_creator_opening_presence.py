"""Creator opening-scene presence is exact, local, deterministic, and replayable."""
from __future__ import annotations

import json

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state, empty_state, reduce_state
from aetherstate.store import Store


WORLD = {
    "world_id": "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "name": "Iron Line",
    "genre": "high_fantasy",
    "setting": (
        "Known figures include Archivist Neris, who keeps the sealed campaign records."
    ),
    "locations": [
        "Iron Gate — a fortified threshold beneath the western wall",
        "Archive Hall — a quiet vault of sealed records",
    ],
    "npcs": [
        {
            "name": "Marshal Varo",
            "role": "spear-and-shield marshal",
            "desc": "Marshal Varo commands the first iron line.",
            "home": "Iron Gate",
        },
        {
            "name": "Captain Sera",
            "role": "watch captain",
            "desc": "Captain Sera commands the relief watch.",
            "home": "Iron Gate",
        },
        {
            "name": "Archivist Neris",
            "role": "archivist",
            "desc": "Archivist Neris keeps the sealed campaign records.",
            "home": "Archive Hall",
        },
    ],
    "aspects": ["Known figures: Captain Sera oversees the western relief watch."],
    "opening_scene": (
        "At Iron Gate, Marshal Varo stands beneath the portcullis with spear in hand. "
        "Marshal Varo waits for the Player to cross the first iron line."
    ),
    "opening_quest": "Ask Archivist Neris who ordered the gate sealed.",
}


def _presence_ops(ops: list[dict]) -> list[dict]:
    return [op for op in ops if op.get("op") == "presence"]


def _present_entity_ids(state: dict) -> set[str]:
    return {
        str(entity_id)
        for entity_id, entity in (state.get("entities") or {}).items()
        if isinstance(entity, dict) and entity.get("present")
    }


def test_exact_npc_named_in_opening_scene_gets_one_turn_zero_presence_op():
    presence = _presence_ops(creator.world_to_ops(WORLD))

    assert presence == [
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]


def test_shared_home_and_names_outside_opening_scene_do_not_gain_presence():
    presence = _presence_ops(creator.world_to_ops(WORLD))

    assert not any(op.get("entity") == "captain_sera" for op in presence), (
        "sharing Marshal Varo's authored home must not imply opening-scene presence"
    )
    assert not any(op.get("entity") == "archivist_neris" for op in presence), (
        "quest, description, and known-figure mentions are not opening-scene presence"
    )


def test_opening_presence_is_unique_and_byte_value_deterministic():
    first = creator.world_to_ops(WORLD)
    second = creator.world_to_ops(WORLD)

    assert first == second
    assert json.dumps(first, sort_keys=True, separators=(",", ":")).encode("utf-8") == (
        json.dumps(second, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert _presence_ops(first) == [
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]


def test_opening_presence_uses_name_boundaries_and_deduplicates_npc_rows():
    world = dict(
        WORLD,
        npcs=[
            {"name": "Ann", "home": "Iron Gate"},
            {"name": "Anna", "home": "Iron Gate"},
            {"name": "Anna", "home": "Iron Gate"},
            {"name": "Varo", "home": "Iron Gate"},
            {"name": "Marshal Varo", "home": "Iron Gate"},
        ],
        opening_scene="At Iron Gate, Anna waits beside Marshal Varo.",
    )

    assert _presence_ops(creator.world_to_ops(world)) == [
        {"op": "presence", "entity": "anna", "present": True},
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]

    separate = dict(world, opening_scene="Marshal Varo signals; Varo answers from the wall.")
    assert _presence_ops(creator.world_to_ops(separate)) == [
        {"op": "presence", "entity": "varo", "present": True},
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]


def test_explicit_absence_does_not_stage_presence_or_overmatch_other_negation():
    for scene in (
        "Marshal Varo is absent from Iron Gate.",
        "The opening proceeds without Marshal Varo.",
        "Marshal Varo has not yet arrived at Iron Gate.",
        "Marshal Varo has left Iron Gate.",
        "Marshal Varo isn't here; the gate is unguarded.",
    ):
        assert not _presence_ops(creator.world_to_ops(dict(WORLD, opening_scene=scene)))

    present_scene = "Marshal Varo has not advanced, aimed, shifted stance, or attacked."
    assert _presence_ops(creator.world_to_ops(dict(WORLD, opening_scene=present_scene))) == [
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]


def test_coordinated_plural_absence_keeps_every_offstage_npc_out_of_opening_presence():
    scene = (
        "Marshal Varo stands at Iron Gate. "
        "Captain Sera and Archivist Neris are elsewhere and not present."
    )

    assert _presence_ops(creator.world_to_ops(dict(WORLD, opening_scene=scene))) == [
        {"op": "presence", "entity": "marshal_varo", "present": True},
    ]


def test_absent_longest_name_also_suppresses_nested_shorter_names():
    world = dict(
        WORLD,
        npcs=[
            {"name": "Varo", "home": "Iron Gate"},
            {"name": "Marshal", "home": "Iron Gate"},
            {"name": "Marshal Varo", "home": "Iron Gate"},
        ],
    )
    for scene in (
        "The gate opens without Marshal Varo.",
        "Marshal Varo wasn't here when the gate opened.",
        "Marshal Varo was absent from the line.",
    ):
        assert not _presence_ops(creator.world_to_ops(dict(world, opening_scene=scene)))


def test_presence_op_follows_its_entity_and_precedes_opening_memory():
    ops = creator.world_to_ops(WORLD)
    add_at = next(
        index for index, op in enumerate(ops)
        if op.get("op") == "entity_add" and op.get("name") == "Marshal Varo"
    )
    presence_at = next(index for index, op in enumerate(ops) if op.get("op") == "presence")
    opening_at = next(
        index for index, op in enumerate(ops)
        if op.get("op") == "memory_event"
        and str(op.get("text", "")).startswith("Opening scene:")
    )

    assert add_at < presence_at < opening_at


def test_opening_presence_applies_and_replays_as_the_exact_present_set():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    try:
        session_id, branch_id = store.create_session(
            external_id="creator-opening-presence"
        )
        result = apply_delta(
            store,
            session_id,
            branch_id,
            0,
            creator.world_to_ops(WORLD),
            "user",
            cfg,
        )

        assert not result.quarantined
        assert _present_entity_ids(current_state(store, branch_id)) == {"marshal_varo"}
        replayed = store.state_at(
            branch_id,
            10**9,
            reduce_state,
            empty=empty_state(),
        )
        assert _present_entity_ids(replayed) == {"marshal_varo"}
    finally:
        store.close()
