"""Hidden front causes stay out of every compatibility narration path."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx

from aetherstate import assist, creator, director, memory
from aetherstate.config import Config
from aetherstate.extraction import Endpoint
from aetherstate.hud import _memories
from aetherstate.state import (
    _front_completion_event_records,
    apply_delta,
    current_state,
    world_ops,
)
from aetherstate.store import Store
from aetherstate.world_events import project_state_overlay
from tests.mock_upstream import MockUpstream, Reply


SECRET_NAME = "The Iron Pact Secretly Seals the East Gate"
SECRET_ID = "the_iron_pact_secretly_seals_the_east_gate"
CONSEQUENCE = "The East Gate is sealed until further notice."


def _runtime(
    tag: str,
    *,
    cfg: Config | None = None,
    store: Store | None = None,
) -> tuple[Config, Store, str, str, str]:
    cfg = cfg or Config()
    cfg.specialization.name = "rpg"
    cfg.director.beat_libraries = ["rpg_adventure"]
    store = store or Store(":memory:")
    session_id, branch_id = store.create_session(external_id=tag)
    world_id = creator.mint_world_id()
    identity = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "world_identity_set", "world_id": world_id}],
        "user",
        cfg,
    )
    assert identity.applied and not identity.quarantined
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14},
                    "skills": {"stealth": 2},
                    "resources": {"hp": {"max": 20}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    return cfg, store, session_id, branch_id, world_id


def _add_front(
    cfg: Config,
    store: Store,
    session_id: str,
    branch_id: str,
    *,
    consequence: str = CONSEQUENCE,
    duration: int | None = None,
) -> None:
    op = {
        "op": "front_add",
        "name": SECRET_NAME,
        "faction": "Iron Pact",
        "segments": 3,
        "pace": 1,
        "consequence": consequence,
    }
    if duration is not None:
        op["event_duration_turns"] = duration
    result = apply_delta(store, session_id, branch_id, 0, [op], "genesis", cfg)
    assert result.applied and not result.quarantined, result.quarantined


def _completion_ops(
    cfg: Config,
    store: Store,
    session_id: str,
    branch_id: str,
) -> list[dict]:
    generated: list[dict] = []
    for turn in (1, 2, 3):
        trigger = apply_delta(
            store,
            session_id,
            branch_id,
            turn,
            [{"op": "affinity_adj", "target": "Iron Pact", "delta": -1}],
            "rule",
            cfg,
        )
        assert trigger.applied and not trigger.quarantined
        generated = world_ops(
            trigger.state,
            trigger.applied,
            clock_turns=0,
            session_id=session_id,
            branch_id=branch_id,
            turn_index=turn,
        )
        if turn < 3:
            ticked = apply_delta(
                store, session_id, branch_id, turn, generated, "rule", cfg,
            )
            assert ticked.applied and not ticked.quarantined, ticked.quarantined
    return generated


def _memory_text(applied: list[dict]) -> str:
    return next(op["text"] for op in applied if op.get("op") == "memory_event")


def test_hidden_completion_memory_and_director_note_stay_cause_neutral_after_expiry() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("hidden-cause-private")
    _add_front(cfg, store, session_id, branch_id, duration=1)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined, committed.quarantined

    event = next(
        row for row in committed.state["world_events"] if row["kind"] == "admission"
    )
    assert event["cause_visibility"] == "hidden"
    compatibility_memory = _memory_text(committed.applied)
    assert compatibility_memory == f"World event — {CONSEQUENCE}"
    assert SECRET_NAME not in compatibility_memory
    assert SECRET_ID not in compatibility_memory

    memory.index_applied(
        store, session_id, branch_id, committed.applied, committed.state,
    )
    advanced = apply_delta(
        store,
        session_id,
        branch_id,
        4,
        [{"op": "affinity_adj", "target": "Iron Pact", "delta": 1}],
        "rule",
        cfg,
    )
    assert advanced.applied and not advanced.quarantined
    state = current_state(store, branch_id)
    history = {
        row["event_id"]: row["status"]
        for row in project_state_overlay(state)["history"]
    }
    assert history[event["event_id"]] == "expired_by_duration"

    assert director.bindings({"binds": "front"}, state, set()) == []
    staged = director.stage(
        store, cfg, session_id, branch_id, 4, state, [], user_name="Kael",
    )
    assert SECRET_NAME not in staged
    assert SECRET_ID not in staged

    rows = memory.retrieve(store, cfg, branch_id, state, "East Gate", 4)
    recall = "\n".join(memory.recall_lines(rows, 4))
    assert CONSEQUENCE in recall
    assert SECRET_NAME not in recall
    assert SECRET_ID not in recall


def test_public_completion_preserves_named_memory_and_director_fallout() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("public-cause-visible")
    _add_front(cfg, store, session_id, branch_id)
    revealed = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "front_reveal", "front": SECRET_ID}],
        "extraction",
        cfg,
    )
    assert revealed.applied and not revealed.quarantined
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined, committed.quarantined
    event = next(
        row for row in committed.state["world_events"] if row["kind"] == "admission"
    )
    assert event["cause_visibility"] == "public"
    assert _memory_text(committed.applied) == (
        f"World event — {SECRET_NAME}: {CONSEQUENCE}"
    )

    advanced = apply_delta(
        store,
        session_id,
        branch_id,
        4,
        [{"op": "affinity_adj", "target": "Iron Pact", "delta": 1}],
        "rule",
        cfg,
    )
    assert advanced.applied and not advanced.quarantined
    state = current_state(store, branch_id)
    bindings = director.bindings({"binds": "front"}, state, set())
    assert bindings == [{
        "front": SECRET_ID,
        "front_name": SECRET_NAME,
        "front_consequence": CONSEQUENCE,
    }]
    staged = director.stage(
        store, cfg, session_id, branch_id, 4, state, [], user_name="Kael",
    )
    assert SECRET_NAME in staged
    assert CONSEQUENCE in staged


def test_missing_event_group_quarantines_hidden_cause_neutral_memory_companion() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("hidden-cause-atomic")
    _add_front(cfg, store, session_id, branch_id)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    compatibility_memory = next(
        op for op in generated if op.get("op") == "memory_event"
    )
    assert compatibility_memory["text"] == f"World event — {CONSEQUENCE}"
    stripped = [op for op in generated if op.get("op") != "world_event_admit"]

    rejected = apply_delta(
        store, session_id, branch_id, 3, stripped, "rule", cfg,
    )

    assert not any(
        op.get("op") in {"front_tick", "world_flag", "memory_event"}
        for op in rejected.applied
    )
    assert any("atomic" in row["reason"] for row in rejected.quarantined)
    state = current_state(store, branch_id)
    assert state["fronts"][SECRET_ID]["done"] is False
    assert all(CONSEQUENCE not in row.get("text", "") for row in state["memories"])


def test_fresh_front_requires_public_consequence_and_legacy_fallback_is_cause_neutral() -> None:
    cfg, store, session_id, branch_id, world_id = _runtime("empty-consequence")
    invalid = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{
            "op": "front_add",
            "name": SECRET_NAME,
            "faction": "Iron Pact",
            "segments": 3,
            "consequence": "",
        }],
        "genesis",
        cfg,
    )
    assert not invalid.applied
    assert invalid.quarantined
    assert SECRET_ID not in current_state(store, branch_id).get("fronts", {})

    legacy = deepcopy(current_state(store, branch_id))
    legacy.setdefault("fronts", {})[SECRET_ID] = {
        "name": SECRET_NAME,
        "faction": "iron_pact",
        "segments": 3,
        "filled": 2,
        "pace": 1,
        "consequence": "",
        "revealed": False,
        "done": False,
        "created_turn": 0,
        "log": [],
    }
    records = _front_completion_event_records(
        legacy,
        front_id=SECRET_ID,
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=3,
    )
    admission = next(row for row in records if row["kind"] == "admission")
    assert admission["cause_visibility"] == "hidden"
    assert admission["description"] == "A world change has come to a head."
    assert SECRET_NAME not in admission["description"]
    assert SECRET_ID not in admission["description"]
    assert admission["effects"][0]["value"] == admission["description"]


def test_legacy_hidden_memory_is_sanitized_at_index_retrieval_and_reflection() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("legacy-memory-projection")
    _add_front(cfg, store, session_id, branch_id)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined

    legacy_text = f"World event \N{EM DASH} {SECRET_NAME}: {CONSEQUENCE}"
    indexed = memory.index_applied(
        store,
        session_id,
        branch_id,
        [{"op": "memory_event", "text": legacy_text, "_turn": 3}],
        committed.state,
    )
    assert indexed == 1
    indexed_text = store.memories_candidates(branch_id)[0]["text"]
    assert indexed_text == f"World event \N{EM DASH} {CONSEQUENCE}"

    legacy_id = store.memories_add(
        session_id,
        branch_id,
        tier="episodic",
        text=legacy_text,
        participants=[],
        location_id=None,
        tags=[],
        importance=10,
        created_turn=3,
        scene_index=0,
    )
    selected = memory.retrieve(
        store, cfg, branch_id, committed.state, "East Gate", now_turn=4,
    )
    assert selected
    assert all(SECRET_NAME not in row["text"] for row in selected)
    assert any(CONSEQUENCE in row["text"] for row in selected)
    stored = store.db.execute(
        "SELECT text FROM memories WHERE memory_id=?", (legacy_id,),
    ).fetchone()
    assert stored["text"] == legacy_text

    legacy_hud_state = deepcopy(committed.state)
    legacy_hud_state.setdefault("memories", []).append({"turn": 3, "text": legacy_text})
    hud_memories = _memories(legacy_hud_state)
    assert all(SECRET_NAME not in row["text"] for row in hud_memories)
    assert any(CONSEQUENCE in row["text"] for row in hud_memories)
    assert legacy_hud_state["memories"][-1]["text"] == legacy_text

    reflected_state = deepcopy(committed.state)
    reflected_state.setdefault("scene", {})["scene_index"] = 2
    cfg.memory.reflection_every_scenes = 1
    assert memory.reflect(
        store, cfg, session_id, branch_id, reflected_state,
    ) == 1
    summaries = store.summaries_unsynthesized(branch_id, 10)
    assert summaries
    assert all(SECRET_NAME not in row["text"] for row in summaries)
    assert any(CONSEQUENCE in row["text"] for row in summaries)


async def test_session_search_projects_legacy_hidden_memory_without_rewriting_history(
    cfg: Config,
    proxy_app,
    client,
) -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime(
        "legacy-search-projection",
        cfg=cfg,
        store=proxy_app.state.store,
    )
    _add_front(cfg, store, session_id, branch_id)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined

    legacy_text = f"World event \N{EM DASH} {SECRET_NAME}: {CONSEQUENCE}"
    legacy_id = store.memories_add(
        session_id,
        branch_id,
        tier="episodic",
        text=legacy_text,
        participants=[],
        location_id=None,
        tags=[],
        importance=10,
        created_turn=3,
        scene_index=0,
    )
    response = await client.get(
        "/aether/session/legacy-search-projection/search",
        params={"q": "East Gate", "limit": 8},
    )
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert hits
    assert all(SECRET_NAME not in row["text"] for row in hits)
    assert any(CONSEQUENCE in row["text"] for row in hits)
    stored = store.db.execute(
        "SELECT text FROM memories WHERE memory_id=?", (legacy_id,),
    ).fetchone()
    assert stored["text"] == legacy_text


async def test_assist_reflection_never_receives_or_reemits_legacy_hidden_cause() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("legacy-assist-reflection")
    _add_front(cfg, store, session_id, branch_id)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined

    legacy_text = f"World event \N{EM DASH} {SECRET_NAME}: {CONSEQUENCE}"
    member_id = store.memories_add(
        session_id,
        branch_id,
        tier="episodic",
        text=legacy_text,
        participants=[],
        location_id=None,
        tags=[],
        importance=10,
        created_turn=3,
        scene_index=0,
    )
    summary_id = store.memories_add(
        session_id,
        branch_id,
        tier="summary",
        text=legacy_text,
        participants=[],
        location_id=None,
        tags=["summary"],
        importance=10,
        created_turn=3,
        scene_index=0,
    )
    store.memories_set_parent([member_id], summary_id)

    mock = MockUpstream()
    response_doc = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "summary": f"{SECRET_NAME} caused the closure.",
                    "facts": [f"{SECRET_NAME} remains responsible."],
                }),
            },
        }],
    }
    mock.enqueue(Reply(body=json.dumps(response_doc).encode()))
    upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock),
        base_url="http://mock-upstream",
    )
    try:
        upgraded = await assist.synthesize(
            store,
            cfg,
            lambda: upstream,
            Endpoint(base_url="http://mock-upstream/v1", model="tiny"),
            session_id,
            branch_id,
        )
    finally:
        await upstream.aclose()
    assert upgraded == 1
    sent = json.loads(mock.requests[0].body)["messages"][1]["content"]
    assert SECRET_NAME not in sent
    assert CONSEQUENCE in sent

    rows = store.db.execute(
        "SELECT memory_id, tier, text FROM memories WHERE branch_id=?",
        (branch_id,),
    ).fetchall()
    synthesized = next(row for row in rows if row["memory_id"] == summary_id)
    semantic = next(row for row in rows if row["tier"] == "semantic")
    assert SECRET_NAME not in synthesized["text"]
    assert SECRET_NAME not in semantic["text"]
    original = next(row for row in rows if row["memory_id"] == member_id)
    assert original["text"] == legacy_text
