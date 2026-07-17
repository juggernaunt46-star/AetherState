from __future__ import annotations

import re

import pytest

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


WORLD_ID = re.compile(r"^world_[0-9a-f]{32}$")


def test_creator_mints_and_preserves_one_canonical_world_lineage():
    first = creator.ensure_world_identity({"name": "Tidefall", "genre": "sci_fi"})
    assert WORLD_ID.fullmatch(first["world_id"])

    again = creator.ensure_world_identity(first)
    assert again["world_id"] == first["world_id"]
    assert creator.deterministic_world(first)["world_id"] == first["world_id"]

    identity = creator.world_to_ops(first)[0]
    assert identity == {"op": "world_identity_set", "world_id": first["world_id"]}


@pytest.mark.parametrize("bad", ["tidefall", "world_ABC", "world_1234", "world_" + "g" * 32])
def test_creator_rejects_noncanonical_world_identity(bad: str):
    with pytest.raises(ValueError, match="world_id"):
        creator.ensure_world_identity({"world_id": bad})


def test_world_identity_is_journaled_immutable_and_replay_exact():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="world-identity")
    world_id = creator.mint_world_id()
    first = apply_delta(store, sid, bid, 0,
                        [{"op": "world_identity_set", "world_id": world_id}],
                        "user", cfg)
    assert first.state["world_identity"]["world_id"] == world_id
    assert store.worldlex.get_world_lineage(world_id).world_id == world_id

    retry = apply_delta(store, sid, bid, 1,
                        [{"op": "world_identity_set", "world_id": world_id}],
                        "user", cfg)
    assert retry.applied and not retry.quarantined

    conflict = apply_delta(store, sid, bid, 2,
                           [{"op": "world_identity_set",
                             "world_id": creator.mint_world_id()}],
                           "user", cfg)
    assert not conflict.applied
    assert "different world lineage" in conflict.quarantined[0]["reason"]

    replay = current_state(store, bid)
    assert replay["world_identity"] == first.state["world_identity"]


def test_world_identity_conflict_rejects_entire_world_batch():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="world-batch-conflict")
    first = creator.ensure_world_identity({
        "name": "Tidefall", "genre": "sci_fi", "setting": "A drowned orbital polity."
    })
    accepted = apply_delta(store, sid, bid, 0, creator.world_to_ops(first), "user", cfg)
    assert accepted.applied and not accepted.quarantined
    journal_rows = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]

    intruder = creator.ensure_world_identity({
        "name": "Intruder", "genre": "fantasy", "setting": "A different world."
    })
    rejected = apply_delta(store, sid, bid, 1, creator.world_to_ops(intruder), "user", cfg)

    assert not rejected.applied
    assert rejected.quarantined[0]["op"]["op"] == "world_identity_set"
    assert "different world lineage" in rejected.quarantined[0]["reason"]
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == journal_rows
    replay = current_state(store, bid)
    assert replay["world_identity"]["world_id"] == first["world_id"]
    assert all("Intruder" not in event.get("text", "") for event in replay["memories"])


def test_world_lineage_rolls_back_when_journal_fails(monkeypatch):
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="world-transaction")
    world_id = creator.mint_world_id()

    def fail_journal(*_args, **_kwargs):
        raise RuntimeError("injected journal failure")

    monkeypatch.setattr(store, "journal", fail_journal)
    with pytest.raises(RuntimeError, match="injected journal failure"):
        apply_delta(store, sid, bid, 0,
                    [{"op": "world_identity_set", "world_id": world_id}],
                    "user", cfg)

    assert store.worldlex.get_world_lineage(world_id) is None
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0
    assert not current_state(store, bid)["world_identity"]


async def test_world_identity_survives_creator_preset_card_and_second_session(client):
    saved = await client.post("/aether/session/world-a/world", json={"world": {
        "name": "Tidefall", "genre": "sci_fi", "setting": "A drowned orbital polity."
    }})
    assert saved.status_code == 200
    world_id = saved.json()["world_id"]
    assert WORLD_ID.fullmatch(world_id)

    committed = (await client.get("/aether/session/world-a/creator")).json()["world"]
    assert committed["world_id"] == world_id

    preset = await client.post("/aether/presets", json={
        "kind": "world", "name": "Tidefall", "doc": committed
    })
    assert preset.status_code == 200 and preset.json()["world_id"] == world_id
    preset_doc = (await client.get(f"/aether/presets/{preset.json()['preset_id']}")).json()["doc"]
    assert preset_doc["world_id"] == world_id

    card = (await client.get("/aether/session/world-a/narrator-card.json")).json()
    seed = card["data"]["extensions"]["aetherstate"]["seed"]
    assert seed["world"]["world_id"] == world_id

    second = await client.post("/aether/session/world-b/seed", json={"seed": seed})
    assert second.status_code == 200 and second.json()["world_id"] == world_id
    second_world = (await client.get("/aether/session/world-b/creator")).json()["world"]
    assert second_world["world_id"] == world_id


async def test_creator_world_conflict_returns_409_without_claiming_submitted_id(client):
    accepted = await client.post("/aether/session/world-conflict/world", json={"world": {
        "name": "Tidefall", "genre": "sci_fi", "setting": "A drowned orbital polity."
    }})
    assert accepted.status_code == 200
    committed_world_id = accepted.json()["world_id"]

    submitted_world_id = creator.mint_world_id()
    rejected = await client.post("/aether/session/world-conflict/world", json={"world": {
        "world_id": submitted_world_id,
        "name": "Intruder",
        "genre": "fantasy",
        "setting": "A different world.",
    }})

    assert rejected.status_code == 409
    body = rejected.json()
    assert body["applied"] == 0
    assert body["rejected"][0]["op"] == "world_identity_set"
    assert "world_id" not in body
    committed = (await client.get("/aether/session/world-conflict/creator")).json()["world"]
    assert committed["world_id"] == committed_world_id


async def test_creator_page_keeps_world_identity_in_drafts_and_presets(client):
    page = (await client.get("/aether/creator")).text
    assert "world_id:ensureWorldId()" in page
    assert "WORLD_ID=r.world_id" in page
    assert "WORLD_ID=/^world_" in page
