"""Durable Store ownership for Claim Records and World Event Records."""
from __future__ import annotations

import json

import pytest

from aetherstate.canon import CanonMsg, chain
from aetherstate.claim_frame import build_claim_frames, build_claim_record
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record


def _world(seed: str = "a") -> str:
    return "world_" + seed * 32


def _claim(
    session_id: str,
    branch_id: str,
    *,
    turn: int,
    world_id: str,
    text: str = "Mara asserted that the gate is open.",
    visibility: str = "player",
) -> dict:
    meaning = load_default_semantic_fabric().translate(text)
    frames = build_claim_frames(text, meaning)
    assert len(frames) == 1
    return build_claim_record(
        frames[0],
        session_id=session_id,
        branch_id=branch_id,
        world_id=world_id,
        turn=turn,
        source="narrator",
        visibility=visibility,
    )


def _event(
    session_id: str,
    branch_id: str,
    *,
    turn: int,
    world_id: str,
    event_id: str,
    description: str = "The harbor watch is mobilized.",
) -> dict:
    return build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=turn,
        game_time=turn,
        cause_id=f"rule-fixture:{event_id}",
        cause_authority="rule",
        affected_domains=["world"],
        effects=[{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": "world",
            "field": "circumstance",
            "value": description,
            "supported": True,
            "lore": "",
        }],
        description=description,
    )


def _owning_ops(*, claim: dict | None = None, event: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    if claim is not None:
        rows.append({"op": "claim_record", "frame": claim["frame"], "_record": claim})
    if event is not None:
        rows.append({"op": "world_event_admit", "event": event})
    return rows


def _count(store: Store, table: str, branch_id: str | None = None) -> int:
    if branch_id is None:
        row = store.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    else:
        row = store.db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE branch_id=?", (branch_id,)
        ).fetchone()
    return int(row["n"])


def _append_user_turns(store: Store, branch_id: str, count: int) -> None:
    messages = [CanonMsg("user", "", f"{index + 1:016x}") for index in range(count)]
    rows = [
        (message.role, message.content_hash, chain_hash)
        for message, chain_hash in zip(messages, chain(messages))
    ]
    store.append_msgs(branch_id, 0, rows)
    for index, (_role, user_hash, _chain_hash) in enumerate(rows):
        store.record_turn(branch_id, index, "new_turn", "normal")
        store.write_turn_hashes(branch_id, index, user_hash=user_hash)


def test_claim_and_event_publish_atomically_and_exact_retry_is_idempotent() -> None:
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="typed-record-atomic")
    world_id = _world()
    claim = _claim(session_id, branch_id, turn=4, world_id=world_id)
    event = _event(
        session_id,
        branch_id,
        turn=4,
        world_id=world_id,
        event_id="event.harbor.mobilized",
    )

    for _attempt in range(2):
        store.journal_with_receipts(
            branch_id,
            4,
            4,
            _owning_ops(claim=claim, event=event),
            "rule",
            [],
            claim_records=[claim],
            world_event_records=[event],
        )

    assert _count(store, "ops_journal", branch_id) == 2
    assert _count(store, "claim_records", branch_id) == 1
    assert _count(store, "world_event_records", branch_id) == 1
    assert store.claim_records(branch_id) == [claim]
    assert store.world_event_records(branch_id) == [event]


def test_conflicting_duplicate_rolls_back_journal_and_other_typed_records() -> None:
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="typed-record-conflict")
    world_id = _world("b")
    first_claim = _claim(session_id, branch_id, turn=1, world_id=world_id)
    first_event = _event(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        event_id="event.harbor.state",
    )
    store.journal(
        branch_id,
        1,
        1,
        _owning_ops(claim=first_claim, event=first_event),
        "rule",
        claim_records=[first_claim],
        world_event_records=[first_event],
    )
    journal_before = _count(store, "ops_journal", branch_id)

    fresh_claim = _claim(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        text="Vosk denied that the gate is open.",
    )
    conflicting_event = _event(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        event_id=first_event["event_id"],
        description="The harbor watch stood down.",
    )

    with pytest.raises(ValueError, match="identity conflicts"):
        store.journal(
            branch_id,
            1,
            1,
            _owning_ops(claim=fresh_claim, event=conflicting_event),
            "rule",
            claim_records=[fresh_claim],
            world_event_records=[conflicting_event],
        )

    assert _count(store, "ops_journal", branch_id) == journal_before
    assert store.claim_records(branch_id) == [first_claim]
    assert store.world_event_records(branch_id) == [first_event]

    conflicting_claim = _claim(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        visibility="hidden",
    )
    with pytest.raises(ValueError, match="identity conflicts"):
        store.journal(
            branch_id,
            1,
            1,
            _owning_ops(claim=conflicting_claim),
            "rule",
            claim_records=[conflicting_claim],
        )
    assert _count(store, "ops_journal", branch_id) == journal_before


def test_close_reopen_preserves_and_revalidates_typed_records(tmp_path) -> None:
    path = tmp_path / "claim-event-records.sqlite3"
    original = Store(path)
    session_id, branch_id = original.create_session(external_id="typed-record-reopen")
    world_id = _world("c")
    claim = _claim(session_id, branch_id, turn=2, world_id=world_id)
    event = _event(
        session_id,
        branch_id,
        turn=2,
        world_id=world_id,
        event_id="event.reopen",
    )
    original.journal(
        branch_id,
        2,
        2,
        _owning_ops(claim=claim, event=event),
        "rule",
        claim_records=[claim],
        world_event_records=[event],
    )
    original.close()

    reopened = Store(path)
    try:
        assert reopened.claim_records(branch_id) == [claim]
        assert reopened.world_event_records(branch_id) == [event]
    finally:
        reopened.close()


def test_rollback_retract_and_session_delete_follow_record_ownership() -> None:
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="typed-record-lifecycle")
    world_id = _world("d")
    early_claim = _claim(session_id, branch_id, turn=1, world_id=world_id)
    early_event = _event(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        event_id="event.early",
    )
    late_claim = _claim(
        session_id,
        branch_id,
        turn=3,
        world_id=world_id,
        text="Vosk denied that the gate is open.",
    )
    late_event = _event(
        session_id,
        branch_id,
        turn=3,
        world_id=world_id,
        event_id="event.late",
    )
    for turn, claim, event in (
        (1, early_claim, early_event),
        (3, late_claim, late_event),
    ):
        store.journal(
            branch_id,
            turn,
            turn,
            _owning_ops(claim=claim, event=event),
            "rule",
            claim_records=[claim],
            world_event_records=[event],
        )

    store.rollback_to(branch_id, 1)
    assert store.claim_records(branch_id) == [early_claim]
    assert store.world_event_records(branch_id) == [early_event]

    extraction_claim = _claim(
        session_id,
        branch_id,
        turn=2,
        world_id=world_id,
        text="Ryn reported that the bell rang.",
    )
    rule_event = _event(
        session_id,
        branch_id,
        turn=2,
        world_id=world_id,
        event_id="event.rule.owned",
    )
    store.journal(
        branch_id,
        2,
        2,
        _owning_ops(claim=extraction_claim),
        "extraction",
        claim_records=[extraction_claim],
    )
    store.journal(
        branch_id,
        2,
        2,
        _owning_ops(event=rule_event),
        "rule",
        world_event_records=[rule_event],
    )

    store.retract_extraction_at(branch_id, 2)
    assert store.claim_records(branch_id) == [early_claim]
    assert store.world_event_records(branch_id) == [early_event, rule_event]

    store.session_delete(session_id)
    assert _count(store, "claim_records") == 0
    assert _count(store, "world_event_records") == 0


def test_forks_inherit_prefix_records_but_sibling_writes_stay_isolated() -> None:
    store = Store(":memory:")
    session_id, parent = store.create_session(external_id="typed-record-forks")
    _append_user_turns(store, parent, 2)
    world_id = _world("e")
    parent_claim = _claim(session_id, parent, turn=0, world_id=world_id)
    parent_event = _event(
        session_id,
        parent,
        turn=0,
        world_id=world_id,
        event_id="event.parent",
    )
    store.journal(
        parent,
        0,
        0,
        _owning_ops(claim=parent_claim, event=parent_event),
        "rule",
        claim_records=[parent_claim],
        world_event_records=[parent_event],
    )

    first_child = store.fork_branch(parent, at_pos=1, fork_turn=0)
    sibling = store.fork_branch(parent, at_pos=1, fork_turn=0)
    assert store.knowledge_record_scope(first_child) == {
        "session_id": session_id,
        "branch_id": first_child,
        "source_branch_ids": [parent],
    }
    assert store.knowledge_record_scope(sibling) == {
        "session_id": session_id,
        "branch_id": sibling,
        "source_branch_ids": [parent],
    }
    assert store.claim_records(first_child) == [parent_claim]
    assert store.world_event_records(first_child) == [parent_event]
    assert store.claim_records(sibling) == [parent_claim]
    assert store.world_event_records(sibling) == [parent_event]

    child_claim = _claim(
        session_id,
        first_child,
        turn=1,
        world_id=world_id,
        text="Ryn reported that the eastern gate opened.",
    )
    child_event = _event(
        session_id,
        first_child,
        turn=1,
        world_id=world_id,
        event_id="event.child.only",
    )
    store.journal(
        first_child,
        1,
        1,
        _owning_ops(claim=child_claim, event=child_event),
        "rule",
        claim_records=[child_claim],
        world_event_records=[child_event],
    )

    assert store.claim_records(first_child) == [parent_claim, child_claim]
    assert store.world_event_records(first_child) == [parent_event, child_event]
    assert store.claim_records(sibling) == [parent_claim]
    assert store.world_event_records(sibling) == [parent_event]
    assert store.claim_records(parent) == [parent_claim]
    assert store.world_event_records(parent) == [parent_event]


def test_record_reads_detect_json_and_fingerprint_column_tampering() -> None:
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="typed-record-tamper")
    world_id = _world("f")
    claim = _claim(session_id, branch_id, turn=1, world_id=world_id)
    event = _event(
        session_id,
        branch_id,
        turn=1,
        world_id=world_id,
        event_id="event.tamper",
    )
    store.journal(
        branch_id,
        1,
        1,
        _owning_ops(claim=claim, event=event),
        "rule",
        claim_records=[claim],
        world_event_records=[event],
    )

    changed_claim = json.loads(
        store.db.execute(
            "SELECT record_json FROM claim_records WHERE branch_id=?", (branch_id,)
        ).fetchone()["record_json"]
    )
    changed_claim["visibility"] = "hidden"
    store.db.execute(
        "UPDATE claim_records SET record_json=? WHERE branch_id=?",
        (json.dumps(changed_claim, sort_keys=True, separators=(",", ":")), branch_id),
    )
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        store.claim_records(branch_id)

    store.db.execute(
        "UPDATE world_event_records SET fingerprint=? WHERE branch_id=?",
        ("sha256:" + "0" * 64, branch_id),
    )
    with pytest.raises(ValueError, match="fingerprint column diverged"):
        store.world_event_records(branch_id)
