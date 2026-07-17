"""Persistence boundary for whole-mechanic settlement receipts."""
from __future__ import annotations

import json
import sqlite3

import pytest

from aetherstate.canon import CanonMsg, chain
from aetherstate.store import Store


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _mechanic_receipt(name: str, *, outcome: str = "hit") -> dict:
    settlement_ref = _hash(name)
    receipt = {
        "accepted_group_fingerprint": _hash("a"),
        "applied_changes": [
            {"kind": "hp", "subject_id": "iven", "delta": -2, "post": 12},
        ],
        "contract_id": "weapon_attack/1",
        "frame_ref": _hash("f"),
        "meaning_ref": _hash("m"),
        "outcome": outcome,
        "outcome_quality": "success",
        "receipt_fingerprint": _hash("c"),
        "requirement_fingerprint": _hash("q"),
        "schema": "mechanic-settlement/1",
        "settlement_ref": settlement_ref,
        "target_post_state": {"combatant_id": "iven", "hp": {"cur": 12, "max": 14}},
    }
    return {
        "settlement_ref": settlement_ref,
        "contract_id": receipt["contract_id"],
        "frame_ref": receipt["frame_ref"],
        "meaning_ref": receipt["meaning_ref"],
        "outcome": receipt["outcome"],
        "outcome_quality": receipt["outcome_quality"],
        "requirement_fingerprint": receipt["requirement_fingerprint"],
        "request_fingerprint": _hash("r"),
        "accepted_group_fingerprint": receipt["accepted_group_fingerprint"],
        "receipt_fingerprint": receipt["receipt_fingerprint"],
        "receipt": receipt,
    }


def _effect_receipt() -> dict:
    return {
        "effect_id": "dmg_settlement_store",
        "family": "combatant_hp",
        "target": "iven",
        "direction": "harm",
        "delta": -2,
        "payload_hash": "effect-payload",
        "owner": "code",
    }


def _count(store: Store, table: str, branch_id: str | None = None) -> int:
    if branch_id is None:
        row = store.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    else:
        row = store.db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE branch_id=?", (branch_id,)
        ).fetchone()
    return int(row["n"])


def _append_user_turns(store: Store, branch_id: str, count: int) -> None:
    """Give fork fixtures an exact transcript/turn spine instead of ledger-only history."""
    messages: list[CanonMsg] = []
    for index in range(count):
        user_hash = f"{index + 1:016x}"
        messages.append(CanonMsg("user", "", user_hash))
    rows = [
        (message.role, message.content_hash, chain_hash)
        for message, chain_hash in zip(messages, chain(messages))
    ]
    store.append_msgs(branch_id, 0, rows)
    for index, (_role, user_hash, _chain_hash) in enumerate(rows):
        store.record_turn(branch_id, index, "new_turn", "normal")
        store.write_turn_hashes(branch_id, index, user_hash=user_hash)


def test_journal_effect_and_mechanic_receipts_commit_atomically_with_canonical_json():
    store = Store(":memory:")
    _sid, branch = store.create_session(external_id="mechanic-store-atomic")
    mechanic = _mechanic_receipt("1")

    store.journal_with_receipts(
        branch,
        4,
        4,
        [{"op": "mechanic_settlement_commit", "receipt": mechanic["receipt"]}],
        "rule",
        [_effect_receipt()],
        mechanic_receipts=[mechanic],
    )

    assert _count(store, "ops_journal", branch) == 1
    assert _count(store, "effect_receipts", branch) == 1
    found = store.mechanic_settlement_receipts(branch, [mechanic["settlement_ref"]])
    row = found[mechanic["settlement_ref"]]
    assert row["turn_index"] == 4
    assert row["contract_id"] == "weapon_attack/1"
    assert row["source"] == "rule" and row["status"] == "committed"
    assert json.loads(row["receipt_json"]) == mechanic["receipt"]
    assert row["receipt_json"] == json.dumps(
        mechanic["receipt"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_mechanic_receipt_conflict_rolls_back_journal_and_effect_receipt():
    store = Store(":memory:")
    _sid, branch = store.create_session(external_id="mechanic-store-rollback")
    mechanic = _mechanic_receipt("2")

    with pytest.raises(sqlite3.IntegrityError):
        store.journal_with_receipts(
            branch,
            2,
            2,
            [{"op": "check"}],
            "rule",
            [_effect_receipt()],
            mechanic_receipts=[mechanic, mechanic],
        )

    assert _count(store, "ops_journal", branch) == 0
    assert _count(store, "effect_receipts", branch) == 0
    assert _count(store, "mechanic_settlement_receipts", branch) == 0


def test_opening_an_older_database_adds_the_receipt_table_without_losing_state(tmp_path):
    path = tmp_path / "pre-mechanic-receipt.sqlite3"
    original = Store(path)
    sid, _branch = original.create_session(external_id="older-mechanic-store")
    original.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE mechanic_settlement_receipts")
    connection.commit()
    connection.close()

    reopened = Store(path)
    try:
        assert reopened.get_or_create_session("older-mechanic-store")["session_id"] == sid
        tables = {
            row["name"]
            for row in reopened.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "mechanic_settlement_receipts" in tables
    finally:
        reopened.close()


def test_fork_and_rollback_follow_the_receipt_turn_boundary():
    store = Store(":memory:")
    _sid, branch = store.create_session(external_id="mechanic-store-fork")
    _append_user_turns(store, branch, 4)
    first = _mechanic_receipt("3")
    later = _mechanic_receipt("4")
    store.journal_with_receipts(
        branch, 1, 1, [{"op": "first"}], "rule", [], mechanic_receipts=[first]
    )
    store.journal_with_receipts(
        branch, 3, 3, [{"op": "later"}], "rule", [], mechanic_receipts=[later]
    )

    fork = store.fork_branch(branch, at_pos=2, fork_turn=1)
    assert set(store.mechanic_settlement_receipts(
        fork, [first["settlement_ref"], later["settlement_ref"]]
    )) == {first["settlement_ref"]}

    store.rollback_to(branch, 1)
    assert set(store.mechanic_settlement_receipts(
        branch, [first["settlement_ref"], later["settlement_ref"]]
    )) == {first["settlement_ref"]}


def test_extraction_retraction_removes_only_extraction_owned_settlement_receipts():
    store = Store(":memory:")
    _sid, branch = store.create_session(external_id="mechanic-store-retract")
    rule = _mechanic_receipt("5")
    extraction = _mechanic_receipt("6")
    store.journal_with_receipts(
        branch, 2, 2, [{"op": "rule"}], "rule", [], mechanic_receipts=[rule]
    )
    store.journal_with_receipts(
        branch,
        2,
        2,
        [{"op": "extraction"}],
        "extraction",
        [],
        mechanic_receipts=[extraction],
    )

    store.retract_extraction_at(branch, 2)

    assert set(store.mechanic_settlement_receipts(
        branch, [rule["settlement_ref"], extraction["settlement_ref"]]
    )) == {rule["settlement_ref"]}


def test_receipt_occupancy_blocks_empty_branch_discard_and_dead_prune_cleans_source():
    store = Store(":memory:")
    _sid, source = store.create_session(external_id="mechanic-store-branch-lifecycle")
    occupied = store.fork_branch(source, at_pos=0, fork_turn=-1)
    _append_user_turns(store, occupied, 1)
    receipt = _mechanic_receipt("7")
    store.journal_with_receipts(
        occupied, 0, 0, [{"op": "receipt"}], "rule", [], mechanic_receipts=[receipt]
    )

    store.fork_branch(source, at_pos=0, fork_turn=-1, discard_empty_branch=occupied)
    assert store.db.execute(
        "SELECT 1 FROM branches WHERE branch_id=?", (occupied,)
    ).fetchone() is not None

    child = store.fork_branch(occupied, at_pos=1, fork_turn=0,
                              kill_source=True, prune_keep=0)
    assert not store.mechanic_settlement_receipts(occupied, [receipt["settlement_ref"]])
    assert receipt["settlement_ref"] in store.mechanic_settlement_receipts(
        child, [receipt["settlement_ref"]]
    )


def test_session_delete_removes_mechanic_settlement_receipts():
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="mechanic-store-delete")
    receipt = _mechanic_receipt("8")
    store.journal_with_receipts(
        branch, 1, 1, [{"op": "receipt"}], "rule", [], mechanic_receipts=[receipt]
    )

    store.session_delete(sid)

    assert _count(store, "mechanic_settlement_receipts") == 0
