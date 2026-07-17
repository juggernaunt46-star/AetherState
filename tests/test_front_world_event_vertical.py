"""Ordinary-turn proof for the front-completion World Event vertical.

The producer contract is intentionally narrow: front authoring freezes only a
bounded turn duration and a faction-scoped spawn eligibility decision.  A
completion remains one code-owned atomic admission, with append-only lifecycle
records for later supersession.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from aetherstate import creator
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.compose import _render_factions, _render_living_tail, _render_world
from aetherstate.config import Config
from aetherstate.hud import hud_view
from aetherstate.state import apply_delta, current_state, world_ops
from aetherstate.store import Store
from aetherstate.world_events import (
    build_world_event_record,
    effective_value,
    future_subject_eligible,
    validate_world_event_record,
)


_MISSING = object()


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _runtime(path: str | Path, tag: str) -> tuple[Config, Store, str, str, str]:
    cfg = _rpg()
    store = Store(path)
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


def _front_op(
    name: str,
    consequence: str,
    *,
    faction: str | None = "Iron Pact",
    duration: object = _MISSING,
    spawn_eligibility: object = _MISSING,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "front_add",
        "name": name,
        "segments": 3,
        "pace": 1,
        "consequence": consequence,
    }
    if faction is not None:
        op["faction"] = faction
    if duration is not _MISSING:
        op["event_duration_turns"] = duration
    if spawn_eligibility is not _MISSING:
        op["spawn_eligibility"] = spawn_eligibility
    return op


def _add_front(
    cfg: Config,
    store: Store,
    session_id: str,
    branch_id: str,
    turn: int,
    op: dict[str, Any],
) -> None:
    result = apply_delta(store, session_id, branch_id, turn, [op], "genesis", cfg)
    assert result.applied and not result.quarantined, result.quarantined


def _prepare_ordinary_completion(
    cfg: Config,
    store: Store,
    session_id: str,
    branch_id: str,
    turns: tuple[int, int, int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Advance one active faction front through three ordinary committed turns."""
    generated: list[dict[str, Any]] = []
    for turn in turns:
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
        if turn != turns[-1]:
            ticked = apply_delta(
                store, session_id, branch_id, turn, generated, "rule", cfg,
            )
            assert ticked.applied and not ticked.quarantined, ticked.quarantined
    repeated = world_ops(
        trigger.state,
        trigger.applied,
        clock_turns=0,
        session_id=session_id,
        branch_id=branch_id,
        turn_index=turns[-1],
    )
    assert repeated == generated
    return generated, trigger.state


def _admissions(generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        op["event"]
        for op in generated
        if op.get("op") == "world_event_admit"
        and op.get("event", {}).get("kind") == "admission"
    ]


def _event_ops(generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [op["event"] for op in generated if op.get("op") == "world_event_admit"]


def _adapter_id(effect: dict[str, Any]) -> str:
    adapter = effect["adapter"]
    return f"{adapter['adapter_id']}/{adapter['version']}"


def _effect(event: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    return next(row for row in event["effects"] if _adapter_id(row) == adapter_id)


def _reseal(row: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(row)
    row.pop("fingerprint", None)
    row["fingerprint"] = content_fingerprint(row)
    return row


def _compact_effects(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "adapter": _adapter_id(effect),
            "domain": effect["domain"],
            "subject": deepcopy(effect["subject"]),
            "field": effect["field"],
            "value": deepcopy(effect["value"]),
            "supported": effect["supported"],
            "lore": effect["lore"],
        }
        for effect in event["effects"]
    ]


def _rebuild_event(event: dict[str, Any], **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "event_id": event["event_id"],
        "world_id": event["world_id"],
        "session_id": event["session_id"],
        "branch_id": event["branch_id"],
        "turn": event["turn"],
        "game_time": event["game_time"],
        "kind": event["kind"],
        "relation_target": event["relation_target"],
        "actor": deepcopy(event["actor"]),
        "cause_id": event["cause_id"],
        "cause_authority": event["cause_authority"],
        "cause_visibility": event["cause_visibility"],
        "semantic_frame_ref": event["semantic_frame_ref"],
        "settlement_ref": event["settlement_ref"],
        "worldlex_lineage": deepcopy(event["worldlex_lineage"]),
        "subjects": deepcopy(event["subjects"]),
        "future_selector": deepcopy(event["future_selector"]),
        "affected_domains": list(event["affected_domains"]),
        "priority": event["priority"],
        "scope": event["scope"],
        "propagation": event["propagation"],
        "start": event["start"],
        "duration": event["duration"],
        "reversible": event["reversible"],
        "activation": event["activation"],
        "effects": _compact_effects(event),
        "description": event["description"],
    }
    fields.update(changes)
    return build_world_event_record(**fields)


def test_front_add_freezes_bounded_duration_and_faction_spawn_decision() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-authoring-bounds")
    op = _front_op(
        "The Iron Pact Rearms",
        "The Pact closes the harbor.",
        duration=999,
        spawn_eligibility=False,
    )

    _add_front(cfg, store, sid, bid, 0, op)
    op["event_duration_turns"] = 1
    op["spawn_eligibility"] = True

    frozen = current_state(store, bid)["fronts"]["the_iron_pact_rearms"]
    assert frozen["event_duration_turns"] == 100
    assert frozen["spawn_eligibility"] is False


@pytest.mark.parametrize(
    "op",
    [
        _front_op("Zero Duration", "No effect.", duration=0),
        _front_op("Boolean Duration", "No effect.", duration=True),
        _front_op("String Duration", "No effect.", duration="3"),
        _front_op("Integer Spawn", "No effect.", spawn_eligibility=0),
        _front_op(
            "Factionless Spawn",
            "No faction can own this selector.",
            faction=None,
            spawn_eligibility=False,
        ),
    ],
)
def test_front_add_rejects_invalid_optional_event_controls(op: dict[str, Any]) -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", f"invalid-{op['name']}")

    result = apply_delta(store, sid, bid, 0, [op], "genesis", cfg)

    assert not result.applied
    assert result.quarantined
    assert not current_state(store, bid).get("fronts")


def test_completion_is_one_deterministic_turn_timed_existing_and_future_admission() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-deterministic-admission")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op(
            "The Iron Pact Rearms",
            "The Pact closes the harbor.",
            duration=2,
            spawn_eligibility=False,
        ),
    )
    generated, _trigger_state = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))

    assert len(_admissions(generated)) == 1
    event = _admissions(generated)[0]
    circumstance = _effect(event, "faction.circumstance/1")
    spawn = _effect(event, "spawn.eligibility/1")
    assert event["turn"] == event["game_time"] == event["start"] == 3
    assert event["duration"] == 2
    assert event["propagation"] == "existing_and_future"
    assert event["affected_domains"] == sorted(
        {"faction", "enemy_eligibility", "briefing", "narration", "console", "hud"}
    )
    assert circumstance["subject"]["kind"] == "faction"
    assert circumstance["subject"]["id"] == "iron_pact"
    assert circumstance["value"] == "The Pact closes the harbor."
    assert spawn["subject"]["kind"] == "selector"
    assert spawn["value"] is False
    assert event["future_selector"]["subject_kinds"] == ["enemy"]
    assert event["future_selector"]["predicates"] == [
        {"field": "faction", "operator": "eq", "value": "iron_pact"}
    ]

    # A lower-order clock mutation in the same atomic batch cannot stale the
    # event or replace replayable turn time with wall/world-clock minutes.
    committed = apply_delta(
        store,
        sid,
        bid,
        3,
        [{"op": "time_advance", "minutes": 120}, *generated],
        "rule",
        cfg,
    )
    assert not committed.quarantined, committed.quarantined
    state = current_state(store, bid)
    assert state["clock"]["minutes"] == 120
    assert state["world_overlay"]["game_time"] == 3
    assert state["world_overlay"]["active_event_ids"] == [event["event_id"]]
    assert effective_value(
        state["world_overlay"], "faction", "faction:iron_pact", "circumstance",
    ) == "The Pact closes the harbor."


def test_completion_cannot_publish_front_or_legacy_projections_without_event_group() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-atomic-missing-event")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op("The Iron Pact Rearms", "The Pact closes the harbor."),
    )
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    stripped = [op for op in generated if op.get("op") != "world_event_admit"]

    rejected = apply_delta(store, sid, bid, 3, stripped, "rule", cfg)

    assert not any(
        op.get("op") in {"front_tick", "world_flag", "memory_event"}
        for op in rejected.applied
    )
    state = current_state(store, bid)
    front = state["fronts"]["the_iron_pact_rearms"]
    assert front["filled"] == 2
    assert front["done"] is False
    assert not state.get("world_events")
    assert "the_iron_pact_rearms" not in state.get("world", {})
    assert any("atomic" in row["reason"] for row in rejected.quarantined)


def test_exact_completion_retry_does_not_duplicate_immutable_records() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-atomic-retry")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op("The Iron Pact Rearms", "The Pact closes the harbor."),
    )
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    first = apply_delta(store, sid, bid, 3, generated, "rule", cfg)
    assert not first.quarantined, first.quarantined
    first_records = deepcopy(current_state(store, bid)["world_events"])

    retry = apply_delta(store, sid, bid, 3, generated, "rule", cfg)

    assert not retry.quarantined, retry.quarantined
    assert current_state(store, bid)["world_events"] == first_records
    assert len(first_records) == 1


def test_hidden_completed_front_exposes_consequence_without_causal_identity() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-hidden-cause")
    front_name = "The Iron Pact Secretly Seals the East Gate"
    front_id = "the_iron_pact_secretly_seals_the_east_gate"
    consequence = "The East Gate is sealed until further notice."
    _add_front(cfg, store, sid, bid, 0, _front_op(front_name, consequence))
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))

    committed = apply_delta(store, sid, bid, 3, generated, "rule", cfg)
    assert committed.applied and not committed.quarantined, committed.quarantined
    state = current_state(store, bid)
    event = next(row for row in state["world_events"] if row["kind"] == "admission")
    assert event["cause_visibility"] == "hidden"

    faction_context = _render_factions(state)
    world_context = _render_world(state)
    living_tail = _render_living_tail(state, cfg)
    player_view = hud_view(state, cfg)
    player_raw = player_view["player_safe_raw"]

    assert consequence in faction_context
    assert world_context == ""
    for surface in (faction_context, world_context, living_tail):
        assert front_name not in surface
        assert front_id not in surface
    assert player_view["fronts"] == []
    assert player_raw["fronts"] == []
    assert player_view["factions"][0]["world_circumstance"] == consequence
    assert player_raw["factions"][0]["world_circumstance"] == consequence
    assert player_view["knowledge"]["events"][0]["what_happened"] == consequence
    assert player_view["knowledge"]["events"][0]["cause"] == "cause not known"


def test_explicitly_revealed_front_keeps_public_causal_identity_after_completion() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-public-cause")
    front_name = "The Iron Pact Publicly Seals the East Gate"
    front_id = "the_iron_pact_publicly_seals_the_east_gate"
    consequence = "The East Gate is sealed by public decree."
    _add_front(cfg, store, sid, bid, 0, _front_op(front_name, consequence))
    revealed = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "front_reveal", "front": front_id}],
        "extraction",
        cfg,
    )
    assert revealed.applied and not revealed.quarantined, revealed.quarantined
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    committed = apply_delta(store, sid, bid, 3, generated, "rule", cfg)
    assert committed.applied and not committed.quarantined, committed.quarantined
    state = current_state(store, bid)

    event = next(row for row in state["world_events"] if row["kind"] == "admission")
    assert event["cause_visibility"] == "public"
    assert front_id in _render_factions(state)
    assert _render_world(state) == ""
    assert front_name in _render_living_tail(state, cfg)
    player_view = hud_view(state, cfg)
    assert player_view["fronts"][0]["name"] == front_name
    assert player_view["knowledge"]["events"][0]["cause"] == front_name
    assert event["cause_id"] == f"front:{front_id}:completion"


def test_duration_expires_by_turn_restores_spawn_and_replays_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "front-event-replay.sqlite3"
    cfg, store, sid, bid, _world = _runtime(path, "front-duration-replay")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op(
            "The Iron Pact Rearms",
            "The Pact closes the harbor.",
            duration=2,
            spawn_eligibility=False,
        ),
    )
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    committed = apply_delta(store, sid, bid, 3, generated, "rule", cfg)
    assert not committed.quarantined, committed.quarantined
    event = _admissions(generated)[0]
    enemy = {"kind": "enemy", "faction": "iron_pact", "name": "Pact Raider"}

    at_four = apply_delta(
        store, sid, bid, 4, [{"op": "clock_tick", "minutes": 600}], "rule", cfg,
    )
    assert at_four.applied and not at_four.quarantined
    live = current_state(store, bid)
    assert live["world_overlay"]["game_time"] == 4
    assert live["world_overlay"]["active_event_ids"] == [event["event_id"]]
    assert future_subject_eligible(live, enemy) is False
    blocked = apply_delta(
        store,
        sid,
        bid,
        4,
        [{"op": "combatant_spawn", "name": "Pact Raider", "side": "enemy",
          "faction": "iron_pact"}],
        "rule",
        cfg,
    )
    assert not blocked.applied
    assert "ineligible" in blocked.quarantined[0]["reason"]

    before_reopen = current_state(store, bid)
    store.close()
    store = Store(path)
    reopened = current_state(store, bid)
    assert reopened["world_events"] == before_reopen["world_events"]
    assert reopened["world_overlay"] == before_reopen["world_overlay"]
    assert future_subject_eligible(reopened, enemy) is False

    at_five = apply_delta(
        store, sid, bid, 5, [{"op": "clock_tick", "minutes": 1}], "rule", cfg,
    )
    assert at_five.applied and not at_five.quarantined
    expired = current_state(store, bid)
    assert expired["clock"]["minutes"] == 601
    assert expired["world_overlay"]["game_time"] == 5
    assert expired["world_overlay"]["active_event_ids"] == []
    assert next(
        row for row in expired["world_overlay"]["history"]
        if row["event_id"] == event["event_id"]
    )["status"] == "expired_by_duration"
    assert effective_value(
        expired["world_overlay"], "faction", "faction:iron_pact", "circumstance",
    ) is None
    assert future_subject_eligible(expired, enemy) is True
    restored = apply_delta(
        store,
        sid,
        bid,
        5,
        [{"op": "combatant_spawn", "name": "Pact Raider", "side": "enemy",
          "faction": "iron_pact"}],
        "rule",
        cfg,
    )
    assert restored.applied and not restored.quarantined, restored.quarantined


def test_later_front_on_same_circumstance_appends_deterministic_supersession() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-supersession")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op(
            "The Iron Pact Rearms",
            "The Pact closes the harbor.",
            spawn_eligibility=False,
        ),
    )
    first_generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    first_commit = apply_delta(store, sid, bid, 3, first_generated, "rule", cfg)
    assert not first_commit.quarantined, first_commit.quarantined
    first = _admissions(first_generated)[0]

    _add_front(
        cfg,
        store,
        sid,
        bid,
        4,
        _front_op(
            "The Iron Pact Demobilizes",
            "The Pact opens the harbor under a new truce.",
            spawn_eligibility=True,
        ),
    )
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (5, 6, 7))
    events = _event_ops(generated)
    admissions = [event for event in events if event["kind"] == "admission"]
    terminals = [event for event in events if event["kind"] == "supersession"]

    assert len(admissions) == len(terminals) == 1
    later, terminal = admissions[0], terminals[0]
    assert terminal["relation_target"] == first["event_id"]
    assert terminal["turn"] == terminal["game_time"] == terminal["start"] == 7
    assert terminal["event_id"] not in {first["event_id"], later["event_id"]}
    committed = apply_delta(store, sid, bid, 7, generated, "rule", cfg)
    assert not committed.quarantined, committed.quarantined

    state = current_state(store, bid)
    assert state["world_events"][0] == first
    assert len(state["world_events"]) == 3
    assert {event["event_id"] for event in state["world_events"]} == {
        first["event_id"], later["event_id"], terminal["event_id"],
    }
    assert [event["kind"] for event in state["world_events"]].count("admission") == 2
    assert [event["kind"] for event in state["world_events"]].count("supersession") == 1
    history = {row["event_id"]: row for row in state["world_overlay"]["history"]}
    assert history[first["event_id"]]["status"] == "supersession"
    assert history[terminal["event_id"]]["status"] == "winning_terminal"
    assert state["world_overlay"]["active_event_ids"] == [later["event_id"]]
    assert effective_value(
        state["world_overlay"], "faction", "faction:iron_pact", "circumstance",
    ) == "The Pact opens the harbor under a new truce."
    assert future_subject_eligible(
        state, {"kind": "enemy", "faction": "iron_pact", "name": "Pact Raider"},
    ) is True


def test_supersession_and_replacement_admission_are_one_atomic_group() -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", "front-atomic-supersession")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op("The Iron Pact Rearms", "The Pact closes the harbor."),
    )
    first_ops, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    first = apply_delta(store, sid, bid, 3, first_ops, "rule", cfg)
    assert not first.quarantined, first.quarantined
    original_records = deepcopy(current_state(store, bid)["world_events"])
    _add_front(
        cfg,
        store,
        sid,
        bid,
        4,
        _front_op("The Iron Pact Demobilizes", "The Pact opens the harbor."),
    )
    replacement_ops, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (5, 6, 7))
    without_terminal = [
        op for op in replacement_ops
        if not (
            op.get("op") == "world_event_admit"
            and op.get("event", {}).get("kind") == "supersession"
        )
    ]

    rejected = apply_delta(store, sid, bid, 7, without_terminal, "rule", cfg)

    state = current_state(store, bid)
    assert state["world_events"] == original_records
    front = state["fronts"]["the_iron_pact_demobilizes"]
    assert front["filled"] == 2
    assert front["done"] is False
    assert any("atomic" in row["reason"] for row in rejected.quarantined)


@pytest.mark.parametrize("tamper", ["duration", "selector", "spawn_value", "game_time"])
def test_front_completion_rejects_structurally_valid_tampering(tamper: str) -> None:
    cfg, store, sid, bid, _world = _runtime(":memory:", f"front-tamper-{tamper}")
    _add_front(
        cfg,
        store,
        sid,
        bid,
        0,
        _front_op(
            "The Iron Pact Rearms",
            "The Pact closes the harbor.",
            duration=2,
            spawn_eligibility=False,
        ),
    )
    generated, _ = _prepare_ordinary_completion(cfg, store, sid, bid, (1, 2, 3))
    exact = _admissions(generated)[0]
    assert exact["duration"] == 2
    assert exact["future_selector"] is not None

    changes: dict[str, Any] = {}
    if tamper == "duration":
        changes["duration"] = 3
    elif tamper == "selector":
        selector = deepcopy(exact["future_selector"])
        selector["predicates"] = [
            {"field": "faction", "operator": "eq", "value": "other_faction"}
        ]
        changes["future_selector"] = _reseal(selector)
    elif tamper == "spawn_value":
        effects = _compact_effects(exact)
        next(row for row in effects if row["adapter"] == "spawn.eligibility/1")["value"] = True
        changes["effects"] = effects
    else:
        changes["game_time"] = 4
    replacement = _rebuild_event(exact, **changes)
    validate_world_event_record(replacement)
    tampered_ops = [
        {"op": "world_event_admit", "event": replacement}
        if op.get("op") == "world_event_admit" and op.get("event", {}).get("kind") == "admission"
        else deepcopy(op)
        for op in generated
    ]

    result = apply_delta(store, sid, bid, 3, tampered_ops, "rule", cfg)

    assert not any(op.get("op") == "world_event_admit" for op in result.applied)
    assert not current_state(store, bid).get("world_events")
    reason = next(
        row["reason"] for row in result.quarantined
        if row["op"].get("op") == "world_event_admit"
    )
    assert "stale or forged" in reason or "deterministic completion projection" in reason
