from __future__ import annotations

from aetherstate.config import Config
from aetherstate.enemy_capability_pool import reconstruct_enemy_kit
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


WORLD = "world_" + "d" * 32


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _seed(store: Store, cfg: Config, external_id: str = "enemy-worldlex") -> tuple[str, str]:
    sid, branch = store.create_session(external_id=external_id)
    result = apply_delta(store, sid, branch, 0, [
        {"op": "world_identity_set", "world_id": WORLD},
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"DEX": 12}, "skills": {"brawl": 1},
            "resources": {"hp": {"max": 20}},
        }},
    ], "genesis", cfg)
    assert len(result.applied) == 3, result.quarantined
    return sid, branch


def test_world_bound_enemy_spawn_uses_runtime_pool_and_replays_after_reopen(tmp_path):
    cfg = _rpg()
    path = tmp_path / "enemy-worldlex.sqlite3"
    store = Store(path)
    sid, branch = _seed(store, cfg)
    spawned = apply_delta(store, sid, branch, 1, [{
        "op": "combatant_spawn",
        "name": "Bandit",
        "side": "enemy",
        "tier": "standard",
        "armament": "rifle",
        "_capability_pool": {"forged": True},
        "_capability_assignments": [{"forged": True}],
    }], "rule", cfg)
    assert spawned.applied, spawned.quarantined
    journaled = spawned.applied[0]
    assert journaled["_capability_pool"].get("forged") is None
    assert all(item.get("forged") is None for item in journaled["_capability_assignments"])
    assert reconstruct_enemy_kit(journaled["_capability_pool"]) == journaled["_kit"]
    assert journaled["_kit_source"] == "worldlex-runtime-pool"

    row = spawned.state["combat"]["combatants"][journaled["_cid"]]
    assert row["kit"] == journaled["_kit"]
    assert row["kit_source"] == "worldlex-runtime-pool"
    assert row["capability_pool"] == journaled["_capability_pool"]
    assert len(row["capability_assignment_ids"]) == len(row["kit"]["moves"])
    assert set(row["capability_assignment_ids"]) <= set(
        spawned.state["capability_assignments"]
    )
    assert all(
        member["executable"] is True
        for member in row["capability_pool"]["pools"]["runtime"]["members"]
    )
    intent = spawned.state["combat"]["pending_intent"]
    assert intent["move_id"] in {move["id"] for move in row["kit"]["moves"]}

    definition_count = store.db.execute(
        "SELECT COUNT(*) FROM worldlex_capability_definitions WHERE world_id=?", (WORLD,)
    ).fetchone()[0]
    assert definition_count == len(row["kit"]["moves"])
    exact_state = current_state(store, branch)
    store.close()

    reopened = Store(path)
    try:
        replay = current_state(reopened, branch)
        assert replay == exact_state
        replay_row = replay["combat"]["combatants"][journaled["_cid"]]
        assert reconstruct_enemy_kit(replay_row["capability_pool"]) == replay_row["kit"]
        assert reopened.db.execute(
            "SELECT COUNT(*) FROM worldlex_capability_definitions WHERE world_id=?", (WORLD,)
        ).fetchone()[0] == definition_count
    finally:
        reopened.close()


def test_same_enemy_kit_reuses_definitions_but_gets_subject_exact_assignments():
    cfg = _rpg()
    store = Store(":memory:")
    sid, branch = _seed(store, cfg, "enemy-worldlex-repeat")
    first = apply_delta(store, sid, branch, 1, [{
        "op": "combatant_spawn", "name": "Bandit", "side": "enemy",
        "tier": "standard", "armament": "sword",
    }], "rule", cfg)
    second = apply_delta(store, sid, branch, 2, [{
        "op": "combatant_spawn", "name": "Bandit", "side": "enemy",
        "tier": "standard", "armament": "sword",
    }], "rule", cfg)
    assert first.applied and second.applied, (first.quarantined, second.quarantined)
    first_op, second_op = first.applied[0], second.applied[0]
    assert first_op["_kit"] == second_op["_kit"]
    assert first_op["_capability_pool"]["definitions"] \
        == second_op["_capability_pool"]["definitions"]
    assert first_op["_cid"] != second_op["_cid"]
    assert {
        item["subject"]["id"] for item in first_op["_capability_assignments"]
    } == {first.state["combat"]["combatants"][first_op["_cid"]]["capability_subject"]["id"]}
    assert {
        item["subject"]["id"] for item in second_op["_capability_assignments"]
    } == {second.state["combat"]["combatants"][second_op["_cid"]]["capability_subject"]["id"]}
    assert not ({
        item["assignment_id"] for item in first_op["_capability_assignments"]
    } & {
        item["assignment_id"] for item in second_op["_capability_assignments"]
    })
    assert store.db.execute(
        "SELECT COUNT(*) FROM worldlex_capability_definitions WHERE world_id=?", (WORLD,)
    ).fetchone()[0] == len(first_op["_kit"]["moves"])


def test_pool_activation_is_rpg_world_bound_and_unsupported_device_stays_out():
    rpg = _rpg()
    legacy = Store(":memory:")
    legacy_sid, legacy_branch = legacy.create_session(external_id="legacy-enemy")
    legacy_spawn = apply_delta(legacy, legacy_sid, legacy_branch, 0, [{
        "op": "combatant_spawn", "name": "Carrier", "side": "enemy",
        "tier": "standard", "armament": "EMP projector",
    }], "rule", rpg)
    assert "_kit" in legacy_spawn.applied[0]
    assert "_capability_pool" not in legacy_spawn.applied[0]

    none = Config()
    none_store = Store(":memory:")
    none_sid, none_branch = none_store.create_session(external_id="none-enemy")
    apply_delta(none_store, none_sid, none_branch, 0,
                [{"op": "world_identity_set", "world_id": WORLD}], "user", none)
    none_spawn = apply_delta(none_store, none_sid, none_branch, 1, [{
        "op": "combatant_spawn", "name": "Carrier", "side": "enemy",
        "tier": "standard", "armament": "EMP projector",
    }], "rule", none)
    assert "_kit" not in none_spawn.applied[0]
    assert "_capability_pool" not in none_spawn.applied[0]

    live = Store(":memory:")
    live_sid, live_branch = _seed(live, rpg, "unsupported-enemy")
    live_spawn = apply_delta(live, live_sid, live_branch, 1, [{
        "op": "combatant_spawn", "name": "Carrier", "side": "enemy",
        "tier": "standard", "armament": "EMP projector",
    }], "rule", rpg)
    assert live_spawn.applied, live_spawn.quarantined
    kit = reconstruct_enemy_kit(live_spawn.applied[0]["_capability_pool"])
    assert all(move["delivery"] != "EMP projector" for move in kit["moves"])
