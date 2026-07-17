from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _store(label: str) -> tuple[Store, str, str]:
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=label)
    return store, session_id, branch_id


def _journal_count(store: Store) -> int:
    return int(store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0])


@pytest.mark.parametrize(
    ("world", "left_kind", "right_kind"),
    [
        (
            {
                "genre": "high_fantasy",
                "factions": ["Shared Name - the faction description."],
                "locations": ["Shared-Name - the location description."],
            },
            "faction",
            "location",
        ),
        (
            {
                "genre": "high_fantasy",
                "locations": ["Shared Name - the location description."],
                "npcs": [{"name": "Shared-Name", "role": "keeper"}],
            },
            "location",
            "npc",
        ),
    ],
)
def test_world_to_ops_rejects_cross_kind_slug_collisions(
    world: dict, left_kind: str, right_kind: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"shared_name.*{left_kind}.*{right_kind}",
    ):
        creator.world_to_ops(world)


def test_world_model_validation_reports_cross_kind_slug_collisions() -> None:
    issues = creator._world_validation_issues({
        "factions": ["Shared Name - the faction description."],
        "locations": ["Shared-Name - the location description."],
        "npcs": [],
    })

    assert any(
        "shared_name" in issue and "faction" in issue and "location" in issue
        for issue in issues
    )


def test_forged_creator_world_collision_rejects_the_whole_batch() -> None:
    cfg = Config()
    store, session_id, branch_id = _store("creator-namespace-forged-world")
    ops = creator.world_to_ops({"genre": "high_fantasy", "name": "Namespace Test"})
    faction = next(op for op in ops if op.get("op") == "entity_add"
                   and op.get("kind") == "faction")
    location = next(op for op in ops if op.get("op") == "entity_add"
                    and op.get("kind") == "location")
    location["name"] = faction["name"]

    rejected = apply_delta(store, session_id, branch_id, 0, ops, "user", cfg)

    assert not rejected.applied
    assert any("entity namespace collision" in row["reason"]
               for row in rejected.quarantined)
    assert not current_state(store, branch_id)["world_identity"]
    assert not current_state(store, branch_id)["entities"]
    assert _journal_count(store) == 0


def test_cross_kind_collision_with_existing_world_entity_is_atomic() -> None:
    cfg = Config()
    store, session_id, branch_id = _store("existing-world-kind-collision")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "entity_add", "name": "Shared Name", "kind": "faction"}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    before = deepcopy(current_state(store, branch_id))
    journal_before = _journal_count(store)

    rejected = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [
            {"op": "entity_add", "name": "Shared-Name", "kind": "location"},
            {"op": "memory_event", "text": "This must not partly commit."},
        ],
        "user",
        cfg,
    )

    assert not rejected.applied
    assert any(
        "entity namespace collision" in row["reason"]
        and "faction" in row["reason"]
        and "location" in row["reason"]
        for row in rejected.quarantined
    )
    assert current_state(store, branch_id) == before
    assert _journal_count(store) == journal_before


@pytest.mark.parametrize("world_kind", ["location", "faction", "npc"])
def test_player_name_collision_with_existing_world_entity_is_atomic(world_kind: str) -> None:
    cfg = Config()
    store, session_id, branch_id = _store(f"player-existing-{world_kind}")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "entity_add", "name": "The Tower", "kind": world_kind}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    before = deepcopy(current_state(store, branch_id))
    journal_before = _journal_count(store)

    player_ops = creator.player_to_ops({"name": "The-Tower"})
    player_ops.append({"op": "memory_event", "text": "This must not partly commit."})
    rejected = apply_delta(
        store, session_id, branch_id, 1, player_ops, "user", cfg,
    )

    assert not rejected.applied
    assert any(
        "entity namespace collision" in row["reason"]
        and "player" in row["reason"]
        and world_kind in row["reason"]
        for row in rejected.quarantined
    )
    assert current_state(store, branch_id) == before
    assert _journal_count(store) == journal_before


def test_world_and_same_batch_player_name_collision_is_atomic() -> None:
    cfg = Config()
    store, session_id, branch_id = _store("player-pending-world-collision")
    world_ops = creator.world_to_ops({
        "genre": "high_fantasy",
        "name": "Pending Namespace",
        "npcs": [{"name": "Mara Venn", "role": "warden"}],
    })
    player_ops = creator.player_to_ops({"name": "Mara-Venn"})

    rejected = apply_delta(
        store, session_id, branch_id, 0, world_ops + player_ops, "user", cfg,
    )

    assert not rejected.applied
    assert any("entity namespace collision" in row["reason"]
               for row in rejected.quarantined)
    assert not current_state(store, branch_id)["world_identity"]
    assert not current_state(store, branch_id)["entities"]
    assert not current_state(store, branch_id)["player"]
    assert _journal_count(store) == 0


def test_same_batch_player_collision_with_world_alias_is_atomic() -> None:
    cfg = Config()
    store, session_id, branch_id = _store("player-pending-world-alias-collision")
    ops = [{
        "op": "entity_add",
        "name": "The Bell Keeper",
        "kind": "npc",
        "aliases": ["Mara Venn"],
    }]
    ops.extend(creator.player_to_ops({"name": "Mara-Venn"}))

    rejected = apply_delta(store, session_id, branch_id, 0, ops, "user", cfg)

    assert not rejected.applied
    assert any("entity namespace collision" in row["reason"]
               for row in rejected.quarantined)
    assert not current_state(store, branch_id)["entities"]
    assert not current_state(store, branch_id)["player"]
    assert _journal_count(store) == 0


def test_same_player_resave_remains_allowed() -> None:
    cfg = Config()
    store, session_id, branch_id = _store("player-resave")
    first = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        creator.player_to_ops({"name": "Mara Venn", "class": "Scout"}),
        "user",
        cfg,
    )
    assert first.applied and not first.quarantined

    second = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        creator.player_to_ops({"name": "Mara-Venn", "class": "Warden"}),
        "user",
        cfg,
    )

    assert second.applied and not second.quarantined
    assert second.state["entities"]["mara_venn"]["kind"] == "player"
    assert second.state["player"]["mara_venn"]["concept"] == "Warden"


def test_historical_colliding_player_journal_still_replays() -> None:
    store, _session_id, branch_id = _store("legacy-player-collision-replay")
    legacy_ops = [
        {"op": "entity_add", "name": "The Tower", "kind": "location", "_turn": 0},
        {"op": "player_seed", "entity": "the_tower", "card": {"level": 1}, "_turn": 0},
    ]
    store.journal(branch_id, 0, 0, legacy_ops, "user")

    replayed = current_state(store, branch_id)

    assert replayed["entities"]["the_tower"]["kind"] == "player"
    assert replayed["player"]["the_tower"]["level"] == 1
