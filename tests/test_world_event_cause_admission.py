"""Fresh World Event admission requires one exact privileged or code-owned cause.

These tests intentionally stay above pure record validation.  They exercise the live
``apply_delta`` authority boundary, except for the explicit historical-replay assertion.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import random

import pytest

from aetherstate import creator, tier0
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.genesis import _parse_ops
from aetherstate.state import apply_delta, current_state, empty_state, reduce_state, world_ops
from aetherstate.store import Store
from aetherstate.world_events import (
    LEGACY_WORLD_EVENT_SCHEMA,
    WORLDLEX_LINEAGE_SCHEMA,
    build_world_event_record,
)


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _runtime(tag: str) -> tuple[Config, Store, str, str, str]:
    cfg = _rpg()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=tag)
    world_id = creator.mint_world_id()
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "world_identity_set", "world_id": world_id}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    return cfg, store, session_id, branch_id, world_id


def _event(
    *,
    session_id: str,
    branch_id: str,
    world_id: str,
    authority: str,
    cause_id: str,
    turn: int = 1,
    game_time: int | None = None,
    event_id: str = "event.cause.boundary",
    semantic_frame_ref: str | None = None,
    settlement_ref: str | None = None,
    worldlex_lineage: dict | None = None,
) -> dict:
    return build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=turn,
        game_time=turn if game_time is None else game_time,
        cause_id=cause_id,
        cause_authority=authority,
        semantic_frame_ref=semantic_frame_ref,
        settlement_ref=settlement_ref,
        worldlex_lineage=worldlex_lineage,
        subjects=[f"world:{world_id}"],
        affected_domains=["world"],
        effects=[{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": f"world:{world_id}",
            "field": "circumstance",
            "value": "the cause boundary held",
            "supported": True,
            "lore": "",
        }],
        description="the cause boundary held",
    )


@pytest.mark.parametrize(
    ("authority", "source", "accepted"),
    [
        ("creator", "user", True),
        ("creator", "genesis", False),
        ("creator", "rule", False),
        ("genesis", "genesis", True),
        ("genesis", "user", False),
        ("genesis", "rule", False),
    ],
)
def test_privileged_event_authority_maps_to_exact_ingress_only(
    authority: str, source: str, accepted: bool,
) -> None:
    cfg, store, sid, bid, world = _runtime(f"event-ingress-{authority}-{source}")
    event = _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority=authority,
        cause_id=f"{authority}:authored-boundary",
    )

    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], source, cfg,
    )

    assert bool(result.applied) is accepted
    assert bool(current_state(store, bid).get("world_events")) is accepted
    if accepted:
        assert not result.quarantined
    else:
        assert result.quarantined


def test_stage_b_model_json_cannot_admit_a_structurally_valid_event() -> None:
    _cfg, _store, sid, bid, world = _runtime("event-stage-b-json")
    event = _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority="genesis",
        cause_id="genesis:model-proposal",
    )
    raw = json.dumps([
        {"op": "world_event_admit", "event": event},
        {"op": "entity_add", "name": "Mara", "kind": "npc"},
    ])

    parsed = _parse_ops(raw, card="Name: Mara", prompt="Mara waits by the gate.")

    assert parsed == [{"op": "entity_add", "name": "Mara", "kind": "npc"}]


def test_arbitrary_rule_cause_is_rejected_even_when_the_record_is_well_formed() -> None:
    cfg, store, sid, bid, world = _runtime("event-arbitrary-rule")
    event = _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority="rule",
        cause_id="rule:caller-asserted",
    )

    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
    )

    assert not result.applied
    assert "approved code-owned producer" in result.quarantined[0]["reason"]


def _front_completion(tag: str) -> tuple[Config, Store, str, str, str, list[dict], dict]:
    cfg, store, sid, bid, world = _runtime(tag)
    seeded = apply_delta(
        store,
        sid,
        bid,
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
            {
                "op": "front_add",
                "name": "The Iron Pact Rearms",
                "faction": "Iron Pact",
                "segments": 3,
                "pace": 1,
                "consequence": "The Pact marches on the Docks.",
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    for turn in (1, 2):
        ticked = apply_delta(
            store,
            sid,
            bid,
            turn,
            [{
                "op": "front_tick",
                "front": "the_iron_pact_rearms",
                "reason": f"deterministic tick {turn}",
            }],
            "rule",
            cfg,
        )
        assert ticked.applied and not ticked.quarantined
    trigger = apply_delta(
        store,
        sid,
        bid,
        3,
        [{"op": "affinity_adj", "target": "Iron Pact", "delta": -1}],
        "rule",
        cfg,
    )
    assert trigger.applied and not trigger.quarantined
    generated = world_ops(
        trigger.state,
        trigger.applied,
        clock_turns=0,
        session_id=sid,
        branch_id=bid,
        turn_index=3,
    )
    event = next(op["event"] for op in generated if op.get("op") == "world_event_admit")
    return cfg, store, sid, bid, world, generated, event


def test_only_the_genuine_deterministic_front_completion_projection_is_admitted() -> None:
    cfg, store, sid, bid, _world, generated, event = _front_completion(
        "event-front-exact"
    )
    expected_id = "event.front." + hashlib.sha256(
        f"{event['world_id']}|{bid}|3|the_iron_pact_rearms|complete".encode()
    ).hexdigest()[:24]

    result = apply_delta(store, sid, bid, 3, generated, "rule", cfg)

    assert not result.quarantined
    assert event["event_id"] == expected_id
    assert event["cause_id"] == "front:the_iron_pact_rearms:completion"
    assert event["effects"][0]["adapter"]["adapter_id"] == "faction.circumstance"
    assert event["effects"][0]["subject"]["id"] == "iron_pact"
    assert event["effects"][0]["value"] == "The Pact marches on the Docks."
    assert [row["event_id"] for row in current_state(store, bid)["world_events"]] == [expected_id]


def _replace_front_event(generated: list[dict], replacement: dict) -> list[dict]:
    return [
        {"op": "world_event_admit", "event": replacement}
        if op.get("op") == "world_event_admit" else deepcopy(op)
        for op in generated
    ]


@pytest.mark.parametrize("tamper", ["consequence", "subject", "game_time"])
def test_front_completion_rejects_tampered_projection(tamper: str) -> None:
    cfg, store, sid, bid, world, generated, exact = _front_completion(
        f"event-front-tamper-{tamper}"
    )
    consequence = (
        "The Pact secretly retreats." if tamper == "consequence" else exact["description"]
    )
    subject = "faction:other_faction" if tamper == "subject" else "faction:iron_pact"
    game_time = exact["game_time"] + 1 if tamper == "game_time" else exact["game_time"]
    replacement = build_world_event_record(
        event_id=exact["event_id"],
        world_id=world,
        session_id=sid,
        branch_id=bid,
        turn=3,
        game_time=game_time,
        start=exact["start"],
        cause_id=exact["cause_id"],
        cause_authority="rule",
        cause_visibility=exact["cause_visibility"],
        actor=exact["actor"],
        affected_domains=exact["affected_domains"],
        priority=exact["priority"],
        scope=exact["scope"],
        propagation=exact["propagation"],
        reversible=exact["reversible"],
        subjects=[subject],
        effects=[{
            "adapter": "faction.circumstance/1",
            "domain": "faction",
            "subject": subject,
            "field": "circumstance",
            "value": consequence,
            "supported": True,
            "lore": "",
        }],
        description=consequence,
    )

    result = apply_delta(
        store,
        sid,
        bid,
        3,
        _replace_front_event(generated, replacement),
        "rule",
        cfg,
    )

    assert not any(op.get("op") == "world_event_admit" for op in result.applied)
    reason = next(
        row["reason"] for row in result.quarantined
        if row["op"].get("op") == "world_event_admit"
    )
    expected = "game time is stale or forged" if tamper == "game_time" \
        else "differs from its deterministic completion projection"
    assert expected in reason


def _legacy_event(world_id: str, session_id: str, branch_id: str) -> dict:
    payload = {
        "schema": LEGACY_WORLD_EVENT_SCHEMA,
        "schema_version": "world-event-record-v1",
        "event_id": "event.legacy.replay",
        "world_id": world_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "turn": 1,
        "game_time": 0,
        "kind": "admission",
        "relation_target": None,
        "actor": None,
        "cause_id": "legacy:rule",
        "cause_authority": "rule",
        "cause_visibility": "public",
        "semantic_frame_ref": None,
        "settlement_ref": None,
        "worldlex_lineage": None,
        "subjects": ["world"],
        "future_selector": None,
        "affected_domains": ["world"],
        "priority": 0,
        "scope": "branch",
        "propagation": "existing_subjects",
        "start": 0,
        "duration": None,
        "reversible": False,
        "activation": "active",
        "effects": [{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": "world",
            "field": "circumstance",
            "value": "historical weather",
            "supported": True,
            "lore": "",
        }],
        "description": "historical weather",
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def test_v1_is_replay_only_not_a_fresh_admission_route() -> None:
    cfg, store, sid, bid, world = _runtime("event-v1-replay-only")
    event = _legacy_event(world, sid, bid)

    fresh = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
    )

    assert not fresh.applied
    assert "historical World Event records are replay-only" in fresh.quarantined[0]["reason"]

    historical = empty_state()
    historical["world_identity"] = {"world_id": world}
    historical["meta"]["turn"] = 1
    reduce_state(
        historical,
        [{"op": "world_event_admit", "event": event, "_turn": 1}],
    )
    assert historical["world_events"] == [event]


def _settled_runtime(tag: str) -> tuple[Config, Store, str, str, str, dict]:
    cfg, store, sid, bid, world = _runtime(tag)
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14},
                    "skills": {"stealth": 3},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    generated = tier0.run(
        {"messages": [{
            "role": "user",
            "content": "I slip unseen past the watch. ((aether.check stealth vs 9))",
        }]},
        "new_turn",
        False,
        current_state(store, bid),
        cfg,
        random.Random(3),
        turn=1,
    )
    settled = apply_delta(store, sid, bid, 1, generated.rule_ops, "rule", cfg)
    assert not settled.quarantined
    receipt = settled.state["mechanic_settlements"][0]["receipt"]
    return cfg, store, sid, bid, world, receipt


def _mechanic_event(
    sid: str,
    bid: str,
    world: str,
    receipt: dict,
    *,
    event_id: str,
    turn: int = 1,
    game_time: int | None = None,
    settlement_ref: str | None = None,
    frame_ref: str | None = None,
) -> dict:
    settlement = settlement_ref or receipt["settlement_ref"]
    frame = frame_ref or receipt["frame_ref"]
    return _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority="mechanic_settlement",
        cause_id=settlement,
        turn=turn,
        game_time=game_time,
        event_id=event_id,
        semantic_frame_ref=frame,
        settlement_ref=settlement,
    )


def test_exact_current_turn_mechanic_settlement_can_admit_one_event() -> None:
    cfg, store, sid, bid, world, receipt = _settled_runtime("event-mechanic-exact")
    game_time = 1
    event = _mechanic_event(
        sid, bid, world, receipt, event_id="event.mechanic.exact", game_time=game_time,
    )

    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
    )

    assert result.applied and not result.quarantined
    assert current_state(store, bid)["world_events"][-1] == event


def test_mechanic_cause_requires_both_exact_receipt_and_frame() -> None:
    cfg, store, sid, bid, world, receipt = _settled_runtime("event-mechanic-forgery")
    game_time = 1
    with pytest.raises(ValueError, match="exact settlement and frame"):
        _event(
            session_id=sid,
            branch_id=bid,
            world_id=world,
            authority="mechanic_settlement",
            cause_id=receipt["settlement_ref"],
            settlement_ref=receipt["settlement_ref"],
        )

    forged_ref = "sha256:" + "1" * 64
    wrong_frame = "sha256:" + "2" * 64
    attempts = [
        _mechanic_event(
            sid,
            bid,
            world,
            receipt,
            event_id="event.mechanic.forged",
            game_time=game_time,
            settlement_ref=forged_ref,
        ),
        _mechanic_event(
            sid,
            bid,
            world,
            receipt,
            event_id="event.mechanic.wrong-frame",
            game_time=game_time,
            frame_ref=wrong_frame,
        ),
    ]
    for event in attempts:
        result = apply_delta(
            store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
        )
        assert not result.applied and result.quarantined
    assert "no exact current-turn settlement receipt" in apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "world_event_admit", "event": attempts[0]}],
        "rule",
        cfg,
    ).quarantined[0]["reason"]
    assert "conflicts with its settlement receipt" in apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "world_event_admit", "event": attempts[1]}],
        "rule",
        cfg,
    ).quarantined[0]["reason"]

    wrong_turn = _mechanic_event(
        sid,
        bid,
        world,
        receipt,
        event_id="event.mechanic.wrong-turn",
        turn=2,
        game_time=2,
    )
    stale = apply_delta(
        store, sid, bid, 2, [{"op": "world_event_admit", "event": wrong_turn}], "rule", cfg,
    )
    assert not stale.applied
    assert "no exact current-turn settlement receipt" in stale.quarantined[0]["reason"]


def test_semantic_transition_truth_stays_closed_without_atomic_proof() -> None:
    cfg, store, sid, bid, world = _runtime("event-semantic-transition")
    event = _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority="semantic_transition_truth",
        cause_id="transition." + "a" * 32,
        semantic_frame_ref="sha256:" + "b" * 64,
    )

    disabled = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
    )
    assert not disabled.applied
    assert disabled.quarantined[0]["reason"] == "Semantic Transition Truth event admission is disabled"

    cfg.specialization.semantic_truth_gate = True
    unproven = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "rule", cfg,
    )
    assert not unproven.applied
    assert unproven.quarantined[0]["reason"] \
        == "Semantic Transition Truth event lacks an atomically committed transition entry"


def _lineage(world_id: str, field: str, ref: str) -> dict:
    payload = {
        "schema": WORLDLEX_LINEAGE_SCHEMA,
        "world_id": world_id,
        "definition_ref": None,
        "assignment_ref": None,
        "eligibility_ref": None,
        "adapter_ref": None,
        "receipt_ref": None,
    }
    payload[field] = ref
    return {**payload, "fingerprint": content_fingerprint(payload)}


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("definition_ref", "unknown or cross-world WorldLex definition"),
        ("assignment_ref", "unknown WorldLex assignment reference"),
        ("eligibility_ref", "unknown WorldLex eligibility reference"),
        ("adapter_ref", "unknown WorldLex adapter reference"),
        ("receipt_ref", "unknown WorldLex receipt reference"),
    ],
)
def test_unknown_worldlex_lineage_cannot_supply_event_authority(field: str, reason: str) -> None:
    cfg, store, sid, bid, world = _runtime(f"event-worldlex-{field}")
    event = _event(
        session_id=sid,
        branch_id=bid,
        world_id=world,
        authority="creator",
        cause_id="creator:worldlex-lineage",
        worldlex_lineage=_lineage(world, field, f"unknown.{field}"),
    )

    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "user", cfg,
    )

    assert not result.applied
    assert reason in result.quarantined[0]["reason"]
