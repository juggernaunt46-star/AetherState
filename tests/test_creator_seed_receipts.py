"""Portable Narrator seeds commit as one exact, durable, receipt-backed decision."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest

from aetherstate import control, creator, genesis, narrator
from aetherstate.canon import CanonMsg, chain
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _seed(*, setting: str = "A bridge of witnessed vows.") -> dict:
    return {
        "world": {
            "name": "Receipt Reach",
            "world_id": "world_88888888888888888888888888888888",
            "setting": setting,
        },
        "player": {"name": "Rook Vale", "concept": "witness"},
    }


def _session(store, external_id: str):
    return store.db.execute(
        "SELECT * FROM sessions WHERE external_id=?", (external_id,),
    ).fetchone()


def _append_turn_zero(store, branch_id: str) -> None:
    message = CanonMsg("user", "", "1" * 16)
    chain_hash = chain([message])[0]
    store.append_msgs(branch_id, 0, [(message.role, message.content_hash, chain_hash)])
    store.record_turn(branch_id, 0, "new_turn", "normal")
    store.write_turn_hashes(branch_id, 0, user_hash=message.content_hash)


def _user_journal_admissions(store, branch_id: str) -> list[list[dict]]:
    rows = store.db.execute(
        "SELECT ops FROM ops_journal WHERE branch_id=? AND source='user' ORDER BY id",
        (branch_id,),
    ).fetchall()
    return [json.loads(row["ops"]) for row in rows]


def _assert_committed_sources_match_receipt(store, row, receipt: dict) -> dict:
    state = current_state(store, row["active_branch"])
    if receipt["world_requested"]:
        assert (state.get("creator_world") or {}).get("document") == receipt["world_source"]
    if receipt["player_requested"]:
        player = (state.get("player") or {}).get(receipt["player_id"])
        assert isinstance(player, dict)
        assert player.get("creator_source") == receipt["player_source"]
    return state


async def _post_seed(client, sid: str, seed: dict):
    fingerprint = narrator.seed_fingerprint(seed)
    return await client.post(
        f"/aether/session/{sid}/seed",
        json={"seed": seed, "seed_fingerprint": fingerprint},
    )


async def test_exact_seed_receipt_is_durable_and_sequentially_idempotent(client, proxy_app):
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)

    first = await _post_seed(client, "receipt-exact", seed)
    second = await _post_seed(client, "receipt-exact", seed)

    assert first.status_code == second.status_code == 200
    assert first.json()["seed_fingerprint"] == fingerprint
    assert first.json()["complete"] is True and first.json()["applied"] > 0
    assert second.json()["complete"] is True and second.json()["applied"] == 0
    assert second.json()["already_present"] is True
    store = proxy_app.state.store
    row = _session(store, "receipt-exact")
    receipts = store.db.execute(
        "SELECT * FROM creator_seed_receipts WHERE session_id=?", (row["session_id"],),
    ).fetchall()
    assert len(receipts) == 1
    assert receipts[0]["seed_fingerprint"] == fingerprint
    assert json.loads(receipts[0]["seed_json"]) == seed
    assert store.creator_seed_receipt(row["session_id"], fingerprint)["seed"] == seed


async def test_seed_status_never_infers_receipt_from_matching_creator_state(client, proxy_app):
    sid = "receipt-legacy-migration"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    world = await client.post(f"/aether/session/{sid}/world", json={"world": seed["world"]})
    player = await client.post(f"/aether/session/{sid}/player", json={"player": seed["player"]})
    assert world.status_code == player.status_code == 200

    pending = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert pending.status_code == 202
    assert pending.json()["complete"] is False
    assert pending.json()["pending"] is True

    migrated = await _post_seed(client, sid, seed)
    assert migrated.status_code == 200
    assert migrated.json()["complete"] is True
    assert migrated.json()["applied"] == 0
    assert migrated.json()["migrated"] is True

    changed = _seed(setting="A different exact source behind the same names and slugs.")
    refused = await _post_seed(client, sid, changed)
    assert refused.status_code == 409
    assert refused.json()["complete"] is False
    assert "different exact Narrator seed" in refused.json()["error"]
    store = proxy_app.state.store
    row = _session(store, sid)
    assert store.db.execute(
        "SELECT COUNT(*) FROM creator_seed_receipts WHERE session_id=?", (row["session_id"],),
    ).fetchone()[0] == 1


async def test_late_partial_quarantine_rolls_back_state_session_and_receipt(
    client, proxy_app, monkeypatch,
):
    real_apply = control.apply_delta

    def apply_then_quarantine(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        result.quarantined.append({
            "op": {"op": "forced_partial"},
            "reason": "forced late partial quarantine",
        })
        return result

    monkeypatch.setattr(control, "apply_delta", apply_then_quarantine)
    response = await _post_seed(client, "receipt-partial-rollback", _seed())

    assert response.status_code == 409
    assert response.json()["complete"] is False
    store = proxy_app.state.store
    assert _session(store, "receipt-partial-rollback") is None
    assert store.db.execute("SELECT COUNT(*) FROM creator_seed_receipts").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert store.db.execute(
        "SELECT COUNT(*) FROM worldlex_world_lineages"
    ).fetchone()[0] == 0


async def test_submitted_count_mismatch_without_quarantine_also_rolls_back(
    client, proxy_app, monkeypatch,
):
    real_apply = control.apply_delta

    def apply_then_underreport(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        result.submitted_applied -= 1
        return result

    monkeypatch.setattr(control, "apply_delta", apply_then_underreport)
    response = await _post_seed(client, "receipt-count-rollback", _seed())

    assert response.status_code == 409
    assert response.json()["rejected"] == []
    store = proxy_app.state.store
    assert _session(store, "receipt-count-rollback") is None
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM creator_seed_receipts").fetchone()[0] == 0


async def test_receipt_insert_failure_rolls_back_the_already_applied_seed(
    client, proxy_app, monkeypatch,
):
    store = proxy_app.state.store

    def fail_receipt(**_kwargs):
        raise RuntimeError("forced receipt disk failure")

    monkeypatch.setattr(store, "persist_creator_seed_receipt", fail_receipt)
    response = await _post_seed(client, "receipt-write-rollback", _seed())

    assert response.status_code == 500
    assert response.json()["complete"] is False
    assert _session(store, "receipt-write-rollback") is None
    assert store.db.execute("SELECT COUNT(*) FROM creator_seed_receipts").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0


async def test_concurrent_exact_seed_requests_single_flight_through_one_receipt(
    client, proxy_app,
):
    seed = _seed()
    first, second = await asyncio.gather(
        _post_seed(client, "receipt-concurrent", seed),
        _post_seed(client, "receipt-concurrent", seed),
    )

    assert first.status_code == second.status_code == 200
    assert sorted([first.json()["applied"], second.json()["applied"]])[0] == 0
    assert max(first.json()["applied"], second.json()["applied"]) > 0
    store = proxy_app.state.store
    row = _session(store, "receipt-concurrent")
    receipt = store.creator_seed_receipt_for_session(row["session_id"])
    assert store.db.execute(
        "SELECT COUNT(*) FROM creator_seed_receipts WHERE session_id=?", (row["session_id"],),
    ).fetchone()[0] == 1
    admissions = _user_journal_admissions(store, row["active_branch"])
    assert len(admissions) == 1
    assert len(admissions[0]) == receipt["applied_ops"]
    _assert_committed_sources_match_receipt(store, row, receipt)


async def test_concurrent_different_sources_cannot_mix_or_share_a_receipt(
    client, proxy_app,
):
    first_seed = _seed(setting="First exact source.")
    second_seed = _seed(setting="Second exact source.")
    first, second = await asyncio.gather(
        _post_seed(client, "receipt-concurrent-conflict", first_seed),
        _post_seed(client, "receipt-concurrent-conflict", second_seed),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first_seed if first.status_code == 200 else second_seed
    store = proxy_app.state.store
    row = _session(store, "receipt-concurrent-conflict")
    receipt = store.creator_seed_receipt_for_session(row["session_id"])
    assert receipt["seed_fingerprint"] == narrator.seed_fingerprint(winner)
    assert receipt["seed"] == winner
    assert store.db.execute(
        "SELECT COUNT(*) FROM creator_seed_receipts WHERE session_id=?", (row["session_id"],),
    ).fetchone()[0] == 1
    admissions = _user_journal_admissions(store, row["active_branch"])
    assert len(admissions) == 1
    assert len(admissions[0]) == receipt["applied_ops"]
    state = _assert_committed_sources_match_receipt(store, row, receipt)
    assert (state.get("creator_world") or {}).get("document", {}).get("setting") \
        == winner["world"]["setting"]


async def test_runtime_hp_and_encounter_progress_preserve_exact_seed_receipt(
    client, proxy_app, cfg,
):
    sid = "receipt-runtime-progress"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    admitted = await _post_seed(client, sid, seed)
    assert admitted.status_code == 200

    store = proxy_app.state.store
    row = _session(store, sid)
    receipt = store.creator_seed_receipt(row["session_id"], fingerprint)
    before = _assert_committed_sources_match_receipt(store, row, receipt)
    player_id = receipt["player_id"]
    hp_before = before["player"][player_id]["hp"]["cur"]
    progressed = apply_delta(
        store,
        row["session_id"],
        row["active_branch"],
        control._next_turn(store, row["active_branch"]),
        [
            {"op": "hp_adj", "char": player_id, "delta": -1,
             "reason": "ordinary runtime damage"},
            {"op": "entity_add", "name": "Runtime Tide Wisp", "kind": "npc"},
        ],
        "user",
        cfg,
    )
    assert progressed.submitted_applied == 2
    assert progressed.quarantined == []

    status = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert status.status_code == 200
    assert status.json()["complete"] is True
    after = _assert_committed_sources_match_receipt(store, row, receipt)
    assert after["player"][player_id]["hp"]["cur"] == hp_before - 1
    assert after["entities"]["runtime_tide_wisp"]["kind"] == "npc"


async def test_world_only_seed_receipt_reports_exact_requested_flags(client, proxy_app):
    sid = "receipt-world-only"
    seed = {"world": {
        "name": "World Only Reach",
        "world_id": "world_77777777777777777777777777777777",
        "setting": "A world waiting for its Player.",
    }}
    fingerprint = narrator.seed_fingerprint(seed)
    admitted = await _post_seed(client, sid, seed)

    assert admitted.status_code == 200
    assert admitted.json()["world_requested"] is True
    assert admitted.json()["player_requested"] is False
    assert admitted.json()["world_seeded"] is True
    assert admitted.json()["player_seeded"] is False
    assert admitted.json()["complete"] is True
    status = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert status.status_code == 200
    assert status.json()["world_requested"] is True
    assert status.json()["player_requested"] is False
    store = proxy_app.state.store
    row = _session(store, sid)
    receipt = store.creator_seed_receipt(row["session_id"], fingerprint)
    assert receipt["world_source"] is not None and receipt["player_source"] is None
    state = _assert_committed_sources_match_receipt(store, row, receipt)
    assert state.get("player") == {}


async def test_player_only_seed_receipt_reports_exact_requested_flags(client, proxy_app):
    sid = "receipt-player-only"
    seed = {"player": {
        "name": "Rook Alone",
        "concept": "worldless witness",
        "resources": {"hp": {"cur": 7, "max": 12}},
    }}
    fingerprint = narrator.seed_fingerprint(seed)
    admitted = await _post_seed(client, sid, seed)

    assert admitted.status_code == 200
    assert admitted.json()["world_requested"] is False
    assert admitted.json()["player_requested"] is True
    assert admitted.json()["world_seeded"] is False
    assert admitted.json()["player_seeded"] is True
    assert admitted.json()["complete"] is True
    status = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert status.status_code == 200
    assert status.json()["world_requested"] is False
    assert status.json()["player_requested"] is True
    store = proxy_app.state.store
    row = _session(store, sid)
    receipt = store.creator_seed_receipt(row["session_id"], fingerprint)
    assert receipt["world_source"] is None and receipt["player_source"] is not None
    state = _assert_committed_sources_match_receipt(store, row, receipt)
    assert state.get("creator_world") in (None, {})
    assert state.get("world_identity") in (None, {})


async def test_well_formed_unreceipted_fingerprint_cannot_suppress_genesis(
    client, proxy_app,
):
    sid = "receipt-genesis-unreceipted"
    fingerprint = narrator.seed_fingerprint(_seed())
    response = await client.post(f"/aether/session/{sid}/genesis", json={
        "card": "Narrator prose without an admitted portable seed.",
        "greeting": "Opening.",
        "speaker": "Narrator",
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": fingerprint,
    })

    assert response.status_code == 200
    assert response.json()["structured_seed"] is False
    store = proxy_app.state.store
    row = _session(store, sid)
    assert row is not None
    assert store.creator_seed_receipt(row["session_id"], fingerprint) is None


async def test_seed_status_waits_for_commit_off_event_loop(client, monkeypatch):
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    started, release = threading.Event(), threading.Event()
    real_apply = control.apply_delta

    def paused_apply(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        started.set()
        assert release.wait(2), "test did not release paused seed transaction"
        return result

    monkeypatch.setattr(control, "apply_delta", paused_apply)
    admission = asyncio.create_task(_post_seed(client, "receipt-waiting", seed))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
    status = asyncio.create_task(client.get(
        "/aether/session/receipt-waiting/seed-status",
        params={"seed_fingerprint": fingerprint},
    ))
    await asyncio.sleep(0.02)
    assert not status.done()
    release.set()

    admitted, confirmed = await asyncio.gather(admission, status)
    assert admitted.status_code == confirmed.status_code == 200
    assert confirmed.json()["complete"] is True


async def test_stale_receipt_fails_closed_then_exact_retry_reseeds(client, proxy_app):
    sid = "receipt-stale-reseed"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    admitted = await _post_seed(client, sid, seed)
    assert admitted.status_code == 200
    store = proxy_app.state.store
    row = _session(store, sid)
    branch = row["active_branch"]

    store.rollback_to(branch, -1)
    stale = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert stale.status_code == 409
    assert stale.json()["complete"] is False
    assert "stale" in stale.json()["error"]

    reseeded = await _post_seed(client, sid, seed)
    assert reseeded.status_code == 200
    assert reseeded.json()["complete"] is True
    assert reseeded.json()["applied"] > 0
    confirmed = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert confirmed.status_code == 200


async def test_inherited_fork_keeps_receipt_valid_but_preseed_fork_requires_reseed(
    client, proxy_app,
):
    sid = "receipt-fork-lineage"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    assert (await _post_seed(client, sid, seed)).status_code == 200
    store = proxy_app.state.store
    row = _session(store, sid)
    _append_turn_zero(store, row["active_branch"])

    inherited = store.fork_branch(row["active_branch"], at_pos=1, fork_turn=0)
    inherited_status = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert inherited_status.status_code == 200
    receipt = store.creator_seed_receipt(row["session_id"], fingerprint)
    assert receipt["branch_id"] != inherited

    empty = store.fork_branch(inherited, at_pos=0, fork_turn=-1)
    stale = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert stale.status_code == 409
    reseeded = await _post_seed(client, sid, seed)
    assert reseeded.status_code == 200 and reseeded.json()["applied"] > 0
    refreshed = store.creator_seed_receipt(row["session_id"], fingerprint)
    assert refreshed["branch_id"] == empty


async def test_reauthoring_source_invalidates_receipt_and_structured_genesis(
    client, proxy_app,
):
    sid = "receipt-source-changed"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    assert (await _post_seed(client, sid, seed)).status_code == 200
    changed_player = {**seed["player"], "concept": "different authored source"}
    changed = await client.post(
        f"/aether/session/{sid}/player", json={"player": changed_player},
    )
    assert changed.status_code == 200 and changed.json()["applied"] > 0

    stale = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    genesis = await client.post(f"/aether/session/{sid}/genesis", json={
        "card": "Receipt-backed narration only.",
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": fingerprint,
    })
    assert stale.status_code == 409 and stale.json()["complete"] is False
    assert genesis.status_code == 200
    assert genesis.json()["structured_seed"] is False

    store = proxy_app.state.store
    row = _session(store, sid)
    store.session_delete(row["session_id"])
    assert store.db.execute("SELECT COUNT(*) FROM creator_seed_receipts").fetchone()[0] == 0
    missing = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert missing.status_code == 404


async def test_seed_fingerprint_validation_fails_before_session_creation(client, proxy_app):
    seed = _seed()
    malformed = await client.post("/aether/session/fingerprint-malformed/seed", json={
        "seed": seed, "seed_fingerprint": "sha256:BAD",
    })
    mismatch = await client.post("/aether/session/fingerprint-mismatch/seed", json={
        "seed": seed, "seed_fingerprint": "sha256:" + "0" * 64,
    })

    assert malformed.status_code == mismatch.status_code == 422
    assert "seed_fingerprint" in malformed.json()["error"]
    assert "does not match" in mismatch.json()["error"]
    store = proxy_app.state.store
    assert _session(store, "fingerprint-malformed") is None
    assert _session(store, "fingerprint-mismatch") is None


async def test_missing_fingerprint_is_deliberate_legacy_compatibility(client):
    seed = _seed()
    response = await client.post(
        "/aether/session/fingerprint-legacy/seed", json={"seed": seed},
    )

    assert response.status_code == 200
    assert response.json()["seed_fingerprint"] == narrator.seed_fingerprint(seed)
    assert response.json()["complete"] is True


async def test_corrupt_durable_receipt_fails_closed_for_status_seed_and_genesis(
    client, proxy_app,
):
    sid = "receipt-corrupt"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    assert (await _post_seed(client, sid, seed)).status_code == 200
    store = proxy_app.state.store
    row = _session(store, sid)
    with store.transaction():
        store.db.execute(
            "UPDATE creator_seed_receipts SET seed_json='{}' WHERE session_id=?",
            (row["session_id"],),
        )

    status = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    retry = await _post_seed(client, sid, seed)
    genesis = await client.post(f"/aether/session/{sid}/genesis", json={
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": fingerprint,
    })

    assert status.status_code == retry.status_code == 409
    assert status.json()["complete"] is retry.json()["complete"] is False
    assert genesis.status_code == 200
    assert genesis.json()["structured_seed"] is False


async def test_receipt_integrity_digest_covers_every_trusted_authority_family(
    client, proxy_app,
):
    sid = "receipt-whole-authority-integrity"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    assert (await _post_seed(client, sid, seed)).status_code == 200
    store = proxy_app.state.store
    session = _session(store, sid)
    row = dict(store.db.execute(
        "SELECT * FROM creator_seed_receipts WHERE session_id=?",
        (session["session_id"],),
    ).fetchone())
    assert row["receipt_fingerprint"].startswith("sha256:")

    changed_world = json.loads(row["world_source_json"])
    changed_world["setting"] = "A substituted normalized World source."
    changed_player = json.loads(row["player_source_json"])
    changed_player["concept"] = "substituted normalized Character source"
    mutations = [
        {"world_source_json": json.dumps(changed_world)},
        {"player_source_json": json.dumps(changed_player)},
        {"player_requested": 0, "player_source_json": "null", "player_id": ""},
        {"world_id": "world_" + "2" * 32},
        {"player_id": "forged_player"},
        {"branch_id": "forged_branch"},
        {"admitted_turn": int(row["admitted_turn"]) + 1},
        {"applied_ops": int(row["applied_ops"]) + 1},
        {"migrated": 1},
        {"committed_at": float(row["committed_at"]) + 1.0},
        {"receipt_fingerprint": "sha256:" + "0" * 64},
    ]
    for changes in mutations:
        assignments = ", ".join(f"{column}=?" for column in changes)
        with store.transaction():
            store.db.execute(
                f"UPDATE creator_seed_receipts SET {assignments} WHERE session_id=?",
                (*changes.values(), session["session_id"]),
            )
        with pytest.raises(ValueError, match="integrity"):
            store.creator_seed_receipt(session["session_id"], fingerprint)
        status = await client.get(
            f"/aether/session/{sid}/seed-status",
            params={"seed_fingerprint": fingerprint},
        )
        assert status.status_code == 409
        assert "integrity" in status.json()["error"]
        with store.transaction():
            store.db.execute(
                f"UPDATE creator_seed_receipts SET {assignments} WHERE session_id=?",
                (*(row[column] for column in changes), session["session_id"]),
            )
        assert store.creator_seed_receipt(session["session_id"], fingerprint) is not None


async def test_unsigned_legacy_receipt_is_resealed_only_by_exact_admission(
    client, proxy_app,
):
    sid = "receipt-lazy-integrity-migration"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    first = await _post_seed(client, sid, seed)
    assert first.status_code == 200 and first.json()["applied"] > 0
    store = proxy_app.state.store
    session = _session(store, sid)
    journal_before = store.journal_high_water()
    with store.transaction():
        store.db.execute(
            "UPDATE creator_seed_receipts SET receipt_fingerprint='' WHERE session_id=?",
            (session["session_id"],),
        )

    unsigned = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert unsigned.status_code == 202
    assert unsigned.json()["complete"] is False
    assert unsigned.json()["pending"] is True

    resealed = await _post_seed(client, sid, seed)
    assert resealed.status_code == 200
    assert resealed.json()["complete"] is True
    assert resealed.json()["applied"] == 0
    assert resealed.json()["migrated"] is True
    assert store.journal_high_water() == journal_before
    receipt = store.creator_seed_receipt(session["session_id"], fingerprint)
    assert receipt["receipt_fingerprint"].startswith("sha256:")


def test_pre_integrity_receipt_schema_adds_unsigned_digest_column_fail_closed(tmp_path):
    path = tmp_path / "legacy-seed-receipt.sqlite3"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE creator_seed_receipts(
          session_id TEXT, seed_fingerprint TEXT, branch_id TEXT, seed_json TEXT,
          world_source_json TEXT, player_source_json TEXT,
          world_requested INTEGER, player_requested INTEGER, world_id TEXT, player_id TEXT,
          admitted_turn INTEGER, applied_ops INTEGER, migrated INTEGER DEFAULT 0,
          committed_at REAL,
          PRIMARY KEY(session_id, seed_fingerprint))
    """)
    db.execute(
        "INSERT INTO creator_seed_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-session", fingerprint, "legacy-branch", json.dumps(seed),
            json.dumps(seed["world"]), json.dumps(seed["player"]),
            1, 1, seed["world"]["world_id"], "rook_vale", 0, 9, 0, 1.0,
        ),
    )
    db.commit()
    db.close()

    migrated = Store(path)
    columns = {
        row["name"] for row in migrated.db.execute(
            "PRAGMA table_info(creator_seed_receipts)"
        ).fetchall()
    }
    assert "receipt_fingerprint" in columns
    stored = migrated.db.execute(
        "SELECT receipt_fingerprint FROM creator_seed_receipts"
    ).fetchone()
    assert stored["receipt_fingerprint"] == ""
    assert migrated.creator_seed_receipt("legacy-session", fingerprint) is None
    migrated.close()


def test_seed_receipt_survives_store_reopen(tmp_path):
    path = tmp_path / "seed-receipt.sqlite3"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    first = Store(path)
    session_id, branch_id = first.create_session(external_id="receipt-reopen")
    first.persist_creator_seed_receipt(
        session_id=session_id,
        seed_fingerprint=fingerprint,
        branch_id=branch_id,
        seed=seed,
        world_source={**seed["world"], "genre": "modern"},
        player_source=seed["player"],
        world_requested=True,
        player_requested=True,
        world_id=seed["world"]["world_id"],
        player_id="rook_vale",
        admitted_turn=0,
        applied_ops=17,
        migrated=False,
    )
    first.close()

    reopened = Store(path)
    receipt = reopened.creator_seed_receipt(session_id, fingerprint)
    assert receipt is not None
    assert receipt["seed"] == seed
    assert receipt["world_source"]["world_id"] == seed["world"]["world_id"]
    assert receipt["player_source"] == seed["player"]
    reopened.close()


async def test_genesis_receipt_proof_and_done_marker_share_one_store_fence(
    client, proxy_app, cfg, monkeypatch,
):
    sid = "receipt-genesis-fence"
    seed = _seed()
    fingerprint = narrator.seed_fingerprint(seed)
    assert (await _post_seed(client, sid, seed)).status_code == 200
    store = proxy_app.state.store
    row = _session(store, sid)
    started, release, mutation_done = threading.Event(), threading.Event(), threading.Event()
    observed_marker: list[str] = []
    real_seed_rules = genesis.seed_rules

    def paused_seed_rules(*args, **kwargs):
        started.set()
        assert release.wait(2), "test did not release fenced genesis"
        return real_seed_rules(*args, **kwargs)

    monkeypatch.setattr(genesis, "seed_rules", paused_seed_rules)
    genesis_request = asyncio.create_task(client.post(f"/aether/session/{sid}/genesis", json={
        "card": "Exact portable seed already admitted.",
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": fingerprint,
    }))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)

    def mutate_source_after_fence() -> None:
        with store.transaction():
            observed_marker.append(store.genesis_state(row["session_id"]))
            active = _session(store, sid)["active_branch"]
            changed = {**seed["player"], "concept": "post-genesis reauthor"}
            apply_delta(
                store,
                row["session_id"],
                active,
                control._next_turn(store, active),
                creator.player_to_ops(changed, cfg),
                "user",
                cfg,
            )
        mutation_done.set()

    mutation = threading.Thread(target=mutate_source_after_fence, daemon=True)
    mutation.start()
    await asyncio.sleep(0.02)
    assert not mutation_done.is_set()
    release.set()
    response = await genesis_request
    assert await asyncio.wait_for(asyncio.to_thread(mutation_done.wait, 2), timeout=3)
    mutation.join(timeout=1)

    assert response.status_code == 200
    assert response.json()["structured_seed"] is True
    assert observed_marker == ["done"]
    stale = await client.get(
        f"/aether/session/{sid}/seed-status",
        params={"seed_fingerprint": fingerprint},
    )
    assert stale.status_code == 409
