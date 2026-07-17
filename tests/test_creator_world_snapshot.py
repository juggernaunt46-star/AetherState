from __future__ import annotations

from copy import deepcopy

from aetherstate import creator
from aetherstate.canon import CanonMsg, chain
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state, validate_op
from aetherstate.store import Store


def _world() -> dict:
    return creator.ensure_world_identity({
        "name": "Claimfall Harbor",
        "genre": "dark_fantasy",
        "setting": (
            "A rain-dark civic harbor survives by recording testimony without confusing reports "
            "with accepted truth."
        ),
        "date": "The Ninth Rain",
        "time": "evening",
        "tone": "patient civic mystery",
        "factions": [
            "Lantern Guild — neutral recorders protect disputed testimony.",
            "Iron Pact — dock enforcers want the eastern gate sealed.",
            "Tide Court — magistrates trade public certainty for private favors.",
            "Refuge Council — residents defend the cistern and its witnesses.",
        ],
        "locations": [
            "Lantern Refuge — a dry hall where witnesses can speak separately.",
            "East Gate — a chain bridge whose status is openly disputed.",
            "Cistern Court — clean water is guarded below a glass roof.",
            "Archive Steps — sealed ledgers travel through brass speaking tubes.",
            "Rain Market — rumors change hands beside practical supplies.",
        ],
        "npcs": [{
            "name": "Warden Selene",
            "role": "Lantern Guild warden",
            "desc": "She records exact words and labels every unresolved source.",
            "home": "Lantern Refuge",
        }],
        "aspects": [
            "A statement is not accepted fact merely because a respected person repeats it.",
            "Public seals establish custody, not truth.",
            "Harbor clocks advance only through recorded turns.",
            "Hidden causes remain hidden from ordinary inspection.",
            "Conflicting reports stay separate until privileged evidence settles them.",
        ],
        "opening_scene": "At Lantern Refuge, Warden Selene opens a blank testimony ledger.",
        "opening_quest": "Record the East Gate reports without declaring either report proven.",
        "extras": [{
            "label": "Witness custom",
            "text": "Every testimony entry preserves speaker, addressee, quotation, and doubt.",
        }],
        "fronts": [{
            "name": "The Iron Pact seals the gate",
            "faction": "Iron Pact",
            "segments": 4,
            "pace": 1,
            "consequence": "The East Gate closes and Iron Pact patrols become eligible.",
            "event_duration_turns": 6,
            "spawn_eligibility": True,
        }],
        "routes": [{"a": "Lantern Refuge", "b": "East Gate", "segments": 2}],
        "notes": "Direction is draft-only and must never enter the committed source snapshot.",
    })


def _append_turn_zero(store: Store, branch_id: str) -> None:
    message = CanonMsg("user", "", "1" * 16)
    chain_hash = chain([message])[0]
    store.append_msgs(branch_id, 0, [(message.role, message.content_hash, chain_hash)])
    store.record_turn(branch_id, 0, "new_turn", "normal")
    store.write_turn_hashes(branch_id, 0, user_hash=message.content_hash)


def test_world_to_ops_seals_typed_source_without_draft_direction() -> None:
    world = _world()
    ops = creator.world_to_ops(world)
    source = next(op for op in ops if op["op"] == "creator_world_seed")

    assert validate_op(source) is not None
    assert source["document"]["world_id"] == world["world_id"]
    assert source["document"]["setting"] == world["setting"]
    assert source["document"]["extras"] == world["extras"]
    assert "notes" not in source["document"]
    assert world["notes"] not in str(source)


def test_snapshot_survives_memory_eviction_and_opening_quest_uses_note() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="creator-world-memory-eviction")
    world = _world()

    accepted = apply_delta(
        store, session_id, branch_id, 0, creator.world_to_ops(world), "user", cfg,
    )
    assert accepted.applied and not accepted.quarantined
    snapshot = accepted.state["creator_world"]
    assert snapshot["schema"] == "aetherstate-creator-world-snapshot/1"
    assert snapshot["authority_ceiling"] == "creator_lore_only"
    assert snapshot["establishes_objective_truth"] is False
    assert snapshot["admits_world_event"] is False
    assert snapshot["fingerprint"].startswith("sha256:")
    assert "notes" not in snapshot["document"]

    filler = [
        {"op": "memory_event", "text": f"Organic campaign memory {index:03d}."}
        for index in range(150)
    ]
    filled = apply_delta(store, session_id, branch_id, 1, filler, "user", cfg)
    assert filled.applied and not filled.quarantined
    assert len(filled.state["memories"]) == 100

    rebuilt = creator.world_from_state(filled.state)
    for key in (
        "name", "genre", "setting", "date", "time", "tone", "aspects",
        "opening_scene", "opening_quest", "extras",
    ):
        assert rebuilt[key] == creator.deterministic_world(world)[key]
    quest = next(iter(filled.state["quests"].values()))
    assert quest["note"] == world["opening_quest"]
    assert "detail" not in quest


def test_snapshot_replay_reopen_fork_and_same_document_retry_are_exact(tmp_path) -> None:
    cfg = Config()
    db_path = tmp_path / "creator-world.sqlite3"
    store = Store(db_path)
    session_id, parent = store.create_session(external_id="creator-world-reopen")
    _append_turn_zero(store, parent)
    world = _world()
    ops = creator.world_to_ops(world)
    accepted = apply_delta(store, session_id, parent, 0, ops, "user", cfg)
    assert accepted.applied and not accepted.quarantined
    fingerprint = accepted.state["creator_world"]["fingerprint"]

    source = next(op for op in ops if op["op"] == "creator_world_seed")
    retry = apply_delta(store, session_id, parent, 1, [source], "user", cfg)
    assert retry.applied and not retry.quarantined
    assert retry.state["creator_world"]["fingerprint"] == fingerprint

    child = store.fork_branch(parent, at_pos=1, fork_turn=0)
    assert current_state(store, child)["creator_world"]["fingerprint"] == fingerprint
    store.close()

    reopened = Store(db_path)
    try:
        for branch_id in (parent, child):
            state = current_state(reopened, branch_id)
            assert state["creator_world"]["fingerprint"] == fingerprint
            assert creator.world_from_state(state)["opening_scene"] == world["opening_scene"]
    finally:
        reopened.close()


def test_changed_snapshot_conflict_rejects_the_entire_same_world_batch() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="creator-world-immutable")
    world = _world()
    first = apply_delta(store, session_id, branch_id, 0, creator.world_to_ops(world), "user", cfg)
    assert first.applied and not first.quarantined
    journal_before = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]

    changed = deepcopy(world)
    changed["setting"] = "A replacement setting must not rewrite the committed Creator source."
    rejected = apply_delta(
        store, session_id, branch_id, 1, creator.world_to_ops(changed), "user", cfg,
    )

    assert not rejected.applied
    assert any("immutable" in row["reason"] for row in rejected.quarantined)
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == journal_before
    assert creator.world_from_state(current_state(store, branch_id))["setting"] == world["setting"]


def test_legacy_world_without_snapshot_still_migrates_from_prefixed_memories() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="creator-world-legacy")
    world = _world()
    legacy_ops = [op for op in creator.world_to_ops(world) if op["op"] != "creator_world_seed"]

    accepted = apply_delta(store, session_id, branch_id, 0, legacy_ops, "user", cfg)
    assert accepted.applied and not accepted.quarantined
    assert not accepted.state.get("creator_world")
    rebuilt = creator.world_from_state(accepted.state)
    assert rebuilt["name"] == world["name"]
    assert rebuilt["setting"] == world["setting"]
    assert rebuilt["opening_scene"] == world["opening_scene"]
    assert rebuilt["opening_quest"] == world["opening_quest"]
    assert rebuilt["extras"] == world["extras"]


def test_malformed_snapshot_makes_initial_world_batch_atomic() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="creator-world-malformed")
    world = _world()
    ops = creator.world_to_ops(world)
    source = next(op for op in ops if op["op"] == "creator_world_seed")
    source["document"]["notes"] = "forged runtime direction"

    rejected = apply_delta(store, session_id, branch_id, 0, ops, "user", cfg)

    assert not rejected.applied
    assert rejected.quarantined
    assert not current_state(store, branch_id)["world_identity"]
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0
