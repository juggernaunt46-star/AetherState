"""A newer exact Creator genesis supersedes any older cold-model worker."""
from __future__ import annotations

import asyncio

from aetherstate import assist, narrator
from aetherstate.canon import CanonMsg, chain
from aetherstate.state import current_state


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


async def test_forced_structured_genesis_revokes_an_inflight_stage_b(
    client, proxy_app, cfg, monkeypatch,
):
    sid = "structured-genesis-supersedes-stage-b"
    seed = {
        "world": {
            "name": "Supersession Reach",
            "world_id": "world_99999999999999999999999999999999",
        },
        "player": {"name": "Rook Vale", "concept": "witness"},
    }
    fingerprint = narrator.seed_fingerprint(seed)
    admitted = await client.post(f"/aether/session/{sid}/seed", json={
        "seed": seed,
        "seed_fingerprint": fingerprint,
    })
    assert admitted.status_code == 200 and admitted.json()["complete"] is True

    cfg.upstream.model = "mock-model"
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_stage_b(*_args, **_kwargs):
        started.set()
        await release.wait()
        return '[{"op":"memory_event","text":"superseded Stage B mutation"}]'

    monkeypatch.setattr(assist, "_chat", delayed_stage_b)
    stale = await client.post(f"/aether/session/{sid}/genesis", json={
        "card": "THE WORLD - Supersession Reach",
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": "sha256:" + "0" * 64,
    })
    assert stale.status_code == 200
    assert stale.json()["structured_seed"] is False
    assert stale.json()["scheduled"] is True
    await asyncio.wait_for(started.wait(), timeout=2)

    verified = await client.post(f"/aether/session/{sid}/genesis?force=1", json={
        "card": "THE WORLD - Supersession Reach",
        "card_role": "narrator",
        "structured_seed": True,
        "seed_fingerprint": fingerprint,
    })
    assert verified.status_code == 200
    assert verified.json()["structured_seed"] is True

    release.set()
    await proxy_app.state.jobs.drain(timeout=2)
    store = proxy_app.state.store
    row = _session(store, sid)
    state = current_state(store, row["active_branch"])
    assert store.genesis_state(row["session_id"]) == "done"
    assert all(
        memory.get("text") != "superseded Stage B mutation"
        for memory in state.get("memories", [])
        if isinstance(memory, dict)
    )


async def test_inflight_stage_b_cannot_publish_to_a_branch_that_became_inactive(
    client, proxy_app, cfg, monkeypatch,
):
    sid = "stage-b-inactive-branch"
    cfg.upstream.model = "mock-model"
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_stage_b(*_args, **_kwargs):
        started.set()
        await release.wait()
        return '[{"op":"memory_event","text":"inactive branch mutation"}]'

    monkeypatch.setattr(assist, "_chat", delayed_stage_b)
    scheduled = await client.post(f"/aether/session/{sid}/genesis", json={
        "card": "Name: Branch Walker",
        "greeting": "The crossing begins.",
    })
    assert scheduled.status_code == 200 and scheduled.json()["scheduled"] is True
    await asyncio.wait_for(started.wait(), timeout=2)

    store = proxy_app.state.store
    row = _session(store, sid)
    source_branch = row["active_branch"]
    _append_turn_zero(store, source_branch)
    child_branch = store.fork_branch(source_branch, at_pos=1, fork_turn=0)

    release.set()
    await proxy_app.state.jobs.drain(timeout=2)
    assert store.genesis_state(row["session_id"]) == "rules"
    for branch_id in (source_branch, child_branch):
        state = current_state(store, branch_id)
        assert all(
            memory.get("text") != "inactive branch mutation"
            for memory in state.get("memories", [])
            if isinstance(memory, dict)
        )
