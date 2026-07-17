"""Durability and replay boundaries for immutable World Event records.

These tests deliberately use public Store/Pipeline seams.  They do not mint
records through extraction or mutate any real session data.
"""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from aetherstate import creator, tier0
from aetherstate.canon import CanonMsg, chain
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record, effective_value


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.clock_turns = 0
    cfg.specialization.war_room = False
    cfg.specialization.large_battle = False
    cfg.extraction.mode = "off"
    return cfg


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


def _world_event(
    session_id: str,
    branch_id: str,
    world_id: str,
    *,
    event_id: str,
    turn: int,
    kind: str = "admission",
    relation_target: str | None = None,
) -> dict:
    description = "The harbor chain is raised."
    return build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=turn,
        game_time=turn,
        start=turn,
        kind=kind,
        relation_target=relation_target,
        cause_id=f"creator:{event_id}",
        cause_authority="creator",
        priority=1 if kind != "admission" else 0,
        affected_domains=[] if kind != "admission" else ["world"],
        effects=[] if kind != "admission" else [{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": "world",
            "field": "circumstance",
            "value": description,
            "supported": True,
            "lore": "",
        }],
        description=(
            "The parent harbor-chain event is superseded."
            if kind == "supersession" else description
        ),
    )


def test_pre_feature_sqlite_open_adds_typed_record_tables_without_losing_old_rows(
    tmp_path,
) -> None:
    """A database made before Claim/Event tables existed upgrades additively."""
    path = tmp_path / "pre-claim-event.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions(
          session_id TEXT PRIMARY KEY, external_id TEXT UNIQUE, anchor_hash TEXT,
          frontend TEXT DEFAULT 'unknown', active_branch TEXT, frozen INTEGER DEFAULT 0,
          created_at REAL, last_seen REAL
        );
        CREATE TABLE branches(
          branch_id TEXT PRIMARY KEY, session_id TEXT, parent_branch TEXT, forked_at INTEGER,
          status TEXT DEFAULT 'live', head_turn INTEGER DEFAULT -1
        );
        CREATE TABLE ops_journal(
          id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_lo INTEGER, turn_hi INTEGER,
          ops TEXT, source TEXT, ts REAL
        );
        CREATE TABLE legacy_sentinel(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    connection.execute(
        "INSERT INTO sessions(session_id, external_id, active_branch, created_at, last_seen) "
        "VALUES(?,?,?,?,?)",
        ("legacy-session", "legacy-external", "legacy-branch", 11.0, 12.0),
    )
    connection.execute(
        "INSERT INTO branches(branch_id, session_id, head_turn) VALUES(?,?,?)",
        ("legacy-branch", "legacy-session", 4),
    )
    legacy_ops = json.dumps([{"op": "memory_event", "text": "Old row survives."}])
    connection.execute(
        "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts) "
        "VALUES(?,?,?,?,?,?)",
        ("legacy-branch", 4, 4, legacy_ops, "legacy", 13.0),
    )
    connection.execute(
        "INSERT INTO legacy_sentinel(key, value) VALUES(?,?)",
        ("keep", "untouched"),
    )
    connection.commit()
    connection.close()

    store = Store(path)
    try:
        tables = {
            str(row["name"])
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            str(row["name"])
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert {"claim_records", "world_event_records"} <= tables
        assert {"idx_claim_records_turn", "idx_world_event_records_turn"} <= indexes
        assert tuple(store.db.execute(
            "SELECT external_id, active_branch FROM sessions WHERE session_id=?",
            ("legacy-session",),
        ).fetchone()) == ("legacy-external", "legacy-branch")
        assert tuple(store.db.execute(
            "SELECT session_id, head_turn FROM branches WHERE branch_id=?",
            ("legacy-branch",),
        ).fetchone()) == ("legacy-session", 4)
        journal = store.db.execute(
            "SELECT turn_lo, turn_hi, ops, source FROM ops_journal WHERE branch_id=?",
            ("legacy-branch",),
        ).fetchone()
        assert tuple(journal) == (4, 4, legacy_ops, "legacy")
        assert store.db.execute(
            "SELECT value FROM legacy_sentinel WHERE key='keep'"
        ).fetchone()["value"] == "untouched"
        assert store.claim_records("legacy-branch") == []
        assert store.world_event_records("legacy-branch") == []
    finally:
        store.close()


def test_forked_state_inherits_parent_overlay_but_child_supersession_is_isolated(
    tmp_path,
) -> None:
    cfg = _rpg()
    path = tmp_path / "forked-world-events.sqlite3"
    store = Store(path)
    session_id, parent = store.create_session(external_id="event-overlay-fork")
    _append_user_turns(store, parent, 2)
    world_id = creator.mint_world_id()
    seeded = apply_delta(
        store,
        session_id,
        parent,
        0,
        [{"op": "world_identity_set", "world_id": world_id}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    admission = _world_event(
        session_id,
        parent,
        world_id,
        event_id="event.parent.harbor-chain",
        turn=0,
    )
    admitted = apply_delta(
        store,
        session_id,
        parent,
        0,
        [{"op": "world_event_admit", "event": admission}],
        "user",
        cfg,
    )
    assert admitted.applied and not admitted.quarantined

    child = store.fork_branch(parent, at_pos=1, fork_turn=0)
    sibling = store.fork_branch(parent, at_pos=1, fork_turn=0)
    for branch_id in (parent, child, sibling):
        state = current_state(store, branch_id)
        assert state["world_overlay"]["active_event_ids"] == [admission["event_id"]]
        assert effective_value(
            state["world_overlay"], "world", "world", "circumstance"
        ) == "The harbor chain is raised."

    terminal = _world_event(
        session_id,
        child,
        world_id,
        event_id="event.child.harbor-chain-supersession",
        turn=1,
        kind="supersession",
        relation_target=admission["event_id"],
    )
    superseded = apply_delta(
        store,
        session_id,
        child,
        1,
        [{"op": "world_event_admit", "event": terminal}],
        "user",
        cfg,
    )
    assert superseded.applied and not superseded.quarantined

    # Reopen before inspecting the branches.  This makes the assertions exercise replay from the
    # durable journal and record tables instead of relying on the state returned by admission.
    store.close()
    store = Store(path)

    child_state = current_state(store, child)
    assert child_state["world_overlay"]["active_event_ids"] == []
    history = {
        row["event_id"]: row["status"]
        for row in child_state["world_overlay"]["history"]
    }
    assert history[admission["event_id"]] == "supersession"
    assert history[terminal["event_id"]] == "winning_terminal"
    assert effective_value(
        child_state["world_overlay"], "world", "world", "circumstance"
    ) is None

    for branch_id in (parent, sibling):
        isolated = current_state(store, branch_id)
        assert isolated["world_overlay"]["active_event_ids"] == [admission["event_id"]]
        assert terminal["event_id"] not in {
            row["event_id"] for row in isolated["world_events"]
        }
        assert effective_value(
            isolated["world_overlay"], "world", "world", "circumstance"
        ) == "The harbor chain is raised."
    assert [row["event_id"] for row in store.world_event_records(child)] == [
        admission["event_id"], terminal["event_id"]
    ]
    assert [row["event_id"] for row in store.world_event_records(sibling)] == [
        admission["event_id"]
    ]
    assert [row["event_id"] for row in store.world_event_records(parent)] == [
        admission["event_id"]
    ]
    store.close()


def test_cross_session_fork_keeps_parent_overlay_after_first_child_event() -> None:
    cfg = _rpg()
    store = Store(":memory:")
    parent_session, parent = store.create_session(external_id="event-cross-session-parent")
    _append_user_turns(store, parent, 2)
    world_id = creator.mint_world_id()
    assert apply_delta(
        store,
        parent_session,
        parent,
        0,
        [{"op": "world_identity_set", "world_id": world_id}],
        "user",
        cfg,
    ).applied
    parent_event = _world_event(
        parent_session,
        parent,
        world_id,
        event_id="event.parent.cross-session",
        turn=0,
    )
    assert apply_delta(
        store,
        parent_session,
        parent,
        0,
        [{"op": "world_event_admit", "event": parent_event}],
        "user",
        cfg,
    ).applied

    child_session, empty_child = store.create_session(
        external_id="event-cross-session-child"
    )
    child = store.fork_branch(
        parent,
        at_pos=1,
        fork_turn=0,
        new_session_id=child_session,
        discard_empty_branch=empty_child,
    )
    child_event = _world_event(
        child_session,
        child,
        world_id,
        event_id="event.child.cross-session",
        turn=1,
    )
    admitted = apply_delta(
        store,
        child_session,
        child,
        1,
        [{"op": "world_event_admit", "event": child_event}],
        "user",
        cfg,
    )
    assert admitted.applied and not admitted.quarantined

    replayed = current_state(store, child)
    assert replayed["world_overlay"]["active_event_ids"] == [
        parent_event["event_id"],
        child_event["event_id"],
    ]
    assert store.world_event_origin_branches(child) == [parent]
    store.close()


def _front_pipeline(monkeypatch, tag: str) -> tuple[Store, Pipeline, bytes, str]:
    cfg = _rpg()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=tag)
    world_id = creator.mint_world_id()
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "world_identity_set", "world_id": world_id},
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 12},
                    "skills": {},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
            {
                "op": "front_add",
                "name": "The Iron Pact Rearms",
                "faction": "Iron Pact",
                "segments": 3,
                "pace": 1,
                "consequence": "The Pact closes the harbor.",
            },
        ],
        "genesis",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined, seeded.quarantined
    prepared = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "setup one"},
            {"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "setup two"},
        ],
        "rule",
        cfg,
    )
    assert prepared.applied and not prepared.quarantined, prepared.quarantined
    assert prepared.state["fronts"]["the_iron_pact_rearms"]["filled"] == 2

    calls: list[str] = []

    def controlled_run(_doc, klass, duplicate, *_args, **_kwargs):
        calls.append(klass)
        if klass not in {"new_session", "new_turn"} or duplicate:
            return tier0.Tier0Result()
        return tier0.Tier0Result(rule_ops=[{
            "op": "affinity_adj",
            "target": "Iron Pact",
            "delta": -1,
            "reason": "ordinary turn touches the front faction",
        }])

    monkeypatch.setattr(tier0, "run", controlled_run)
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(9),
        player_lessons_service=None,
    )
    body = json.dumps({
        "model": "world-event-lifecycle",
        "messages": [{"role": "user", "content": "I confront the Iron Pact."}],
    }).encode("utf-8")
    return store, pipe, body, branch_id


@pytest.mark.parametrize(
    ("replay_kind", "gen_type", "turn", "expected_class"),
    (
        ("duplicate_transport", "normal", 1, "new_session"),
        ("continue", "continue", 1, "continue"),
        ("swipe", "swipe", 1, "swipe"),
        ("lost_reply", "normal", 2, "new_turn"),
    ),
)
def test_front_completion_event_is_single_across_transport_and_narration_replays(
    monkeypatch,
    replay_kind: str,
    gen_type: str,
    turn: int,
    expected_class: str,
) -> None:
    tag = f"front-event-{replay_kind}"
    store, pipe, body, branch_id = _front_pipeline(monkeypatch, tag)
    initial_stamp = Stamp(
        session=tag,
        gen_type="normal",
        turn=1,
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )
    first_packet, first = pipe.process(initial_stamp, body)
    assert first is not None and first.klass == "new_session"
    baseline = current_state(store, branch_id)
    assert baseline["fronts"]["the_iron_pact_rearms"]["filled"] == 3
    assert baseline["fronts"]["the_iron_pact_rearms"]["done"] is True
    assert len(baseline["world_events"]) == 1
    event_id = baseline["world_events"][0]["event_id"]

    replay_stamp = initial_stamp if replay_kind == "duplicate_transport" else Stamp(
        session=tag,
        gen_type=gen_type,
        turn=turn,
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )
    replay_packet, replay = pipe.process(replay_stamp, body)

    assert replay is not None and replay.klass == expected_class
    if replay_kind == "duplicate_transport":
        assert replay.network_duplicate is True
        assert replay_packet == first_packet
    after = current_state(store, branch_id)
    assert after["fronts"]["the_iron_pact_rearms"]["filled"] == 3
    assert after["fronts"]["the_iron_pact_rearms"]["done"] is True
    assert [row["event_id"] for row in after["world_events"]] == [event_id]
    assert [row["event_id"] for row in store.world_event_records(branch_id)] == [event_id]
    journal_ops = [
        op
        for row in store.db.execute(
            "SELECT ops FROM ops_journal WHERE branch_id=? ORDER BY id", (branch_id,)
        ).fetchall()
        for op in json.loads(row["ops"])
    ]
    assert sum(op.get("op") == "world_event_admit" for op in journal_ops) == 1
    assert sum(
        op.get("op") == "front_tick"
        and op.get("front") == "the_iron_pact_rearms"
        for op in journal_ops
    ) == 3  # two setup ticks plus exactly one completion tick
    if replay_kind == "lost_reply":
        assert store.rule_ops_at(branch_id, replay.turn_index) == []
