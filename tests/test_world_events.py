from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate import creator
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.knowledge import polarized_proposition_id, proposition_id
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.world_events import (
    LEGACY_WORLD_EVENT_SCHEMA,
    OVERLAY_ADAPTER_SCHEMA,
    OVERLAY_RECEIPT_SCHEMA,
    SELECTOR_SCHEMA,
    WORLD_EVENT_SCHEMA,
    build_world_event_record,
    effective_domain,
    effective_subject,
    effective_value,
    future_subject_eligible,
    future_subject_identity_resolved,
    future_subject_effects,
    project_world_overlay,
    subject_matches_selector,
    validate_world_event_record,
)


def _event(world_id: str, sid: str, bid: str, *, event_id: str = "event.front.complete", **extra):
    fields = {
        "event_id": event_id,
        "world_id": world_id,
        "session_id": sid,
        "branch_id": bid,
        "turn": extra.pop("turn", 1),
        "game_time": extra.pop("game_time", 10),
        "cause_id": extra.pop("cause_id", "front:storm:complete"),
        "cause_authority": extra.pop("cause_authority", "rule"),
        "affected_domains": extra.pop("affected_domains", ["faction", "enemy_eligibility", "hud"]),
        "effects": extra.pop("effects", [
            {"adapter": "faction.circumstance/1", "domain": "faction", "subject": "faction:watch",
             "field": "circumstance", "value": "mobilized", "supported": True, "lore": ""},
            {"adapter": "spawn.eligibility/1", "domain": "enemy_eligibility", "subject": "kind:raider",
             "field": "eligible", "value": False, "supported": True, "lore": ""},
        ]),
    }
    fields.update(extra)
    return build_world_event_record(**fields)


def _reseal(row: dict) -> dict:
    row = deepcopy(row)
    row.pop("fingerprint", None)
    row["fingerprint"] = content_fingerprint(row)
    return row


def _legacy_event(world_id: str, sid: str, bid: str) -> dict:
    payload = {
        "schema": LEGACY_WORLD_EVENT_SCHEMA,
        "schema_version": "world-event-record-v1",
        "event_id": "event.legacy",
        "world_id": world_id,
        "session_id": sid,
        "branch_id": bid,
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
            "adapter": "world.circumstance/1", "domain": "world", "subject": "world",
            "field": "circumstance", "value": "legacy weather", "supported": True, "lore": "",
        }],
        "description": "legacy weather",
    }
    return _reseal(payload)


def test_persistent_expiring_and_superseding_events_are_deterministic() -> None:
    world = "world_" + "1" * 32
    base = _event(world, "session-one", "branch-one")
    timed = _event(world, "session-one", "branch-one", event_id="event.fog", duration=5, priority=2)
    supersede = _event(
        world, "session-one", "branch-one", event_id="event.fog.superseded", turn=2,
        game_time=13, kind="supersession", relation_target="event.fog", effects=[], affected_domains=[],
    )
    live = project_world_overlay([base, timed], world_id=world, branch_id="branch-one", game_time=12)
    assert live["active_event_ids"] == [base["event_id"], timed["event_id"]]
    ended = project_world_overlay([base, timed, supersede], world_id=world, branch_id="branch-one", game_time=14)
    assert ended["active_event_ids"] == [base["event_id"]]
    assert any(row["status"] == "supersession" for row in ended["history"] if row["event_id"] == "event.fog")


def test_existing_effect_and_future_selector_remain_typed() -> None:
    selector_payload = {"schema": "aetherstate-world-subject-selector/1", "subject_kinds": ["enemy"],
                        "predicates": {"faction": "raiders"}}
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    event = _event("world_" + "2" * 32, "session-two", "branch-two", future_selector=selector)
    validate_world_event_record(event)
    assert subject_matches_selector(selector, {"kind": "enemy", "faction": "raiders"})
    assert not subject_matches_selector(selector, {"kind": "enemy", "faction": "watch"})


def test_event_admission_is_idempotent_cross_world_closed_and_replayable() -> None:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="event-ledger")
    world_id = creator.mint_world_id()
    apply_delta(store, sid, bid, 0, [{"op": "world_identity_set", "world_id": world_id}], "user", cfg)
    event = _event(
        world_id, sid, bid, game_time=1,
        cause_id="creator:event-ledger", cause_authority="creator",
    )
    first = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "user", cfg
    )
    assert first.applied and first.state["world_overlay"]["active_event_ids"] == [event["event_id"]]
    duplicate = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "user", cfg
    )
    assert duplicate.applied
    assert len(current_state(store, bid)["world_events"]) == 1

    forged = deepcopy(event)
    forged["world_id"] = creator.mint_world_id()
    payload = {key: forged[key] for key in forged if key != "fingerprint"}
    forged["fingerprint"] = content_fingerprint(payload)
    rejected = apply_delta(
        store, sid, bid, 2, [{"op": "world_event_admit", "event": forged}], "user", cfg
    )
    assert not rejected.applied
    assert "cross-world" in rejected.quarantined[0]["reason"]


def test_specialization_none_cannot_admit_event() -> None:
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="event-none")
    world_id = creator.mint_world_id()
    apply_delta(store, sid, bid, 0, [{"op": "world_identity_set", "world_id": world_id}], "user", cfg)
    event = _event(
        world_id, sid, bid, game_time=0,
        cause_id="creator:event-none", cause_authority="creator",
    )
    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "user", cfg
    )
    assert not result.applied
    assert not current_state(store, bid).get("world_events")


def test_claim_belief_and_fact_admission_are_typed_separately() -> None:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="knowledge-split")
    result = apply_delta(store, sid, bid, 0, [{
        "op": "belief_acquire", "holder": "mara", "proposition_id": "prop:gate",
        "stance": "believes", "source": "claim:vosk",
    }], "extraction", cfg)
    assert result.applied
    assert not result.state["facts"]
    assert result.state["beliefs"]["mara|prop:gate"]["stance"] == "believes"

    fact = apply_delta(store, sid, bid, 1, [{
        "op": "fact_admit", "proposition_id": proposition_id("the bell rang"),
        "statement": "the bell rang",
        "cause": "mechanic:settlement:bell", "authority": "mechanic_settlement",
    }], "rule", cfg)
    assert fact.applied
    accepted = next(iter(fact.state["facts"].values()))
    assert accepted["proposition_id"] == polarized_proposition_id("the bell rang")
    assert accepted["proposition_identity"] == proposition_id("the bell rang")
    assert fact.state["beliefs"]["mara|prop:gate"]["proposition_id"] == "prop:gate"


def test_fresh_v2_adapter_subject_selector_and_receipt_are_closed_and_fingerprinted() -> None:
    world = "world_" + "3" * 32
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["enemy"],
        "predicates": {"faction": "raiders", "tier": "standard"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    event = _event(world, "session-three", "branch-three", future_selector=selector)

    assert event["schema"] == WORLD_EVENT_SCHEMA
    effect = event["effects"][0]
    assert effect["adapter"]["schema"] == OVERLAY_ADAPTER_SCHEMA
    assert effect["receipt"]["schema"] == OVERLAY_RECEIPT_SCHEMA
    assert effect["receipt"]["event_id"] == event["event_id"]
    assert effect["receipt"]["adapter_fingerprint"] == effect["adapter"]["fingerprint"]
    assert event["future_selector"]["schema"] == SELECTOR_SCHEMA
    assert event["future_selector"]["predicates"] == [
        {"field": "faction", "operator": "eq", "value": "raiders"},
        {"field": "tier", "operator": "eq", "value": "standard"},
    ]
    validate_world_event_record(event)


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(scope="global"),
    lambda row: row.update(start=-1),
    lambda row: row.update(activation="caller_claimed_active"),
    lambda row: row.update(subjects="world"),
    lambda row: row.update(actor="untyped"),
    lambda row: row.update(worldlex_lineage="forged"),
])
def test_v2_rejects_malformed_top_level_authority_shapes(mutation) -> None:
    event = _event("world_" + "4" * 32, "session-four", "branch-four")
    mutation(event)
    with pytest.raises(ValueError):
        validate_world_event_record(_reseal(event))


def test_v2_rejects_forged_cause_adapter_value_and_receipt() -> None:
    event = _event("world_" + "5" * 32, "session-five", "branch-five")

    forged_cause = deepcopy(event)
    forged_cause["cause_ref"]["id"] = "another:cause"
    forged_cause["cause_ref"] = _reseal(forged_cause["cause_ref"])
    with pytest.raises(ValueError, match="does not match"):
        validate_world_event_record(_reseal(forged_cause))

    wrong_value = deepcopy(event)
    wrong_value["effects"][1]["value"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        validate_world_event_record(_reseal(wrong_value))

    changed_valid_value = deepcopy(event)
    changed_valid_value["effects"][1]["value"] = True
    with pytest.raises(ValueError, match="identity"):
        validate_world_event_record(_reseal(changed_valid_value))

    forged_adapter = deepcopy(event)
    forged_adapter["effects"][0]["adapter"]["domain"] = "quest"
    forged_adapter["effects"][0]["adapter"] = _reseal(forged_adapter["effects"][0]["adapter"])
    with pytest.raises(ValueError, match="contract"):
        validate_world_event_record(_reseal(forged_adapter))

    forged_receipt = deepcopy(event)
    forged_receipt["effects"][0]["receipt"]["event_id"] = "event.other"
    forged_receipt["effects"][0]["receipt"] = _reseal(forged_receipt["effects"][0]["receipt"])
    with pytest.raises(ValueError, match="exact effect"):
        validate_world_event_record(_reseal(forged_receipt))


def test_historical_v1_record_remains_validation_and_projection_compatible() -> None:
    world = "world_" + "6" * 32
    event = _legacy_event(world, "session-six", "branch-six")
    assert validate_world_event_record(event) == event
    overlay = project_world_overlay(
        [event], world_id=world, session_id="session-six", branch_id="branch-six", game_time=0,
    )
    assert overlay["active_event_ids"] == ["event.legacy"]
    assert effective_value(overlay, "world", "world", "circumstance") == "legacy weather"


def test_projection_filters_world_session_and_branch_unless_origins_are_explicit() -> None:
    world = "world_" + "7" * 32
    parent = _event(world, "session-seven", "parent", event_id="event.parent")
    child = _event(world, "session-seven", "child", event_id="event.child", priority=2)
    sibling = _event(world, "session-seven", "sibling", event_id="event.sibling", priority=3)
    other_session = _event(world, "session-other", "child", event_id="event.other", priority=4)

    exact = project_world_overlay(
        [parent, child, sibling, other_session], world_id=world, session_id="session-seven",
        branch_id="child", game_time=20,
    )
    assert exact["active_event_ids"] == ["event.child"]

    inherited = project_world_overlay(
        [parent, child, sibling, other_session], world_id=world, session_id="session-seven",
        branch_id="child", source_branch_ids=["parent"], game_time=20,
    )
    assert inherited["active_event_ids"] == ["event.parent", "event.child"]
    assert "event.sibling" not in inherited["active_event_ids"]
    assert "event.other" not in inherited["active_event_ids"]

    cross_session_parent = _event(
        world, "session-parent", "parent", event_id="event.cross-session-parent",
    )
    cross_session_child = _event(
        world, "session-child", "child", event_id="event.cross-session-child", priority=5,
    )
    cross_session = project_world_overlay(
        [cross_session_parent, cross_session_child],
        world_id=world,
        session_id="session-child",
        branch_id="child",
        source_branch_ids=["parent"],
        game_time=20,
    )
    assert cross_session["active_event_ids"] == [
        "event.cross-session-parent",
        "event.cross-session-child",
    ]

    with pytest.raises(ValueError, match="more than one session"):
        project_world_overlay(
            [child, other_session], world_id=world, branch_id="child", game_time=20,
        )

    conflicting_identity = deepcopy(child)
    conflicting_identity["description"] = "same identity, different immutable bytes"
    conflicting_identity = _reseal(conflicting_identity)
    with pytest.raises(ValueError, match="conflicting immutable"):
        project_world_overlay(
            [child, conflicting_identity], world_id=world, session_id="session-seven",
            branch_id="child", game_time=20,
        )


def test_terminal_rules_are_deterministic_and_reversal_requires_reversible() -> None:
    world = "world_" + "8" * 32
    irreversible = _event(world, "session-eight", "branch-eight", event_id="event.fixed")
    reversal = _event(
        world, "session-eight", "branch-eight", event_id="event.fixed.reversal", kind="reversal",
        relation_target="event.fixed", game_time=12, start=12, priority=5,
    )
    with pytest.raises(ValueError, match="irreversible"):
        project_world_overlay(
            [irreversible, reversal], world_id=world, session_id="session-eight",
            branch_id="branch-eight", game_time=12,
        )

    reversible = _event(
        world, "session-eight", "branch-eight", event_id="event.change", reversible=True,
    )
    expiry = _event(
        world, "session-eight", "branch-eight", event_id="event.change.expiry", kind="expiry",
        relation_target="event.change", game_time=12, start=12, priority=2,
    )
    supersession = _event(
        world, "session-eight", "branch-eight", event_id="event.change.supersession",
        kind="supersession", relation_target="event.change", game_time=12, start=12, priority=3,
    )
    before = project_world_overlay(
        [reversible, expiry, supersession], world_id=world, session_id="session-eight",
        branch_id="branch-eight", game_time=11,
    )
    assert before["active_event_ids"] == ["event.change"]
    after = project_world_overlay(
        [reversible, expiry, supersession], world_id=world, session_id="session-eight",
        branch_id="branch-eight", game_time=12,
    )
    admission_history = next(row for row in after["history"] if row["event_id"] == "event.change")
    assert admission_history["status"] == "supersession"
    assert next(row for row in after["history"] if row["event_id"] == expiry["event_id"])[
        "status"
    ] == "terminal_conflict_lost"
    assert next(row for row in after["history"] if row["event_id"] == supersession["event_id"])[
        "status"
    ] == "winning_terminal"


def test_duration_uses_replayable_game_time_and_unsupported_effects_remain_lore_only() -> None:
    world = "world_" + "9" * 32
    timed = _event(
        world, "session-nine", "branch-nine", event_id="event.timed", start=10, duration=3,
        effects=[{
            "adapter": "world.circumstance/1", "domain": "world", "subject": "world",
            "field": "circumstance", "value": "brief rain", "supported": True, "lore": "",
        }, {
            "adapter": "actor.condition/1", "domain": "actor", "subject": "actor:mara",
            "field": "condition", "value": None, "supported": False,
            "lore": "Mara might dislike rain, but no actor mechanic is admitted.",
        }],
        affected_domains=["actor", "world"],
    )
    live = project_world_overlay(
        [timed], world_id=world, session_id="session-nine", branch_id="branch-nine", game_time=12,
    )
    assert effective_value(live, "world", "world", "circumstance") == "brief rain"
    assert effective_domain(live, "actor") == {}
    ended = project_world_overlay(
        [timed], world_id=world, session_id="session-nine", branch_id="branch-nine", game_time=13,
    )
    assert ended["active_event_ids"] == []
    assert next(row for row in ended["history"] if row["event_id"] == "event.timed")["status"] \
        == "expired_by_duration"


def test_future_eligibility_supports_typed_predicates_and_true_override() -> None:
    world = "world_" + "a" * 32
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["enemy"],
        "predicates": {"faction": "raiders"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    denied = _event(
        world, "session-ten", "branch-ten", event_id="event.denied", priority=1,
        future_selector=selector,
    )
    allowed = _event(
        world, "session-ten", "branch-ten", event_id="event.allowed", priority=2,
        future_selector=selector,
        effects=[{
            "adapter": "spawn.eligibility/1", "domain": "enemy_eligibility",
            "subject": "kind:raider", "field": "eligible", "value": True,
            "supported": True, "lore": "",
        }],
        affected_domains=["enemy_eligibility"],
    )
    state = {
        "world_events": [denied, allowed],
        "clock": {"minutes": 20},
    }
    assert future_subject_eligible(state, {"kind": "enemy", "faction": "raiders"})
    assert future_subject_eligible(state, {"kind": "enemy", "faction": "watch"})

    state["world_events"] = [denied]
    assert not future_subject_eligible(state, {"kind": "enemy", "faction": "raiders"})
    assert future_subject_eligible(state, {"kind": "enemy"})  # historical replay compatibility
    assert not future_subject_identity_resolved(state, {"kind": "enemy"})
    assert future_subject_identity_resolved(
        state, {"kind": "enemy", "faction": "watch"},
    )
    assert future_subject_eligible(state, {"kind": "npc"})
    assert subject_matches_selector(
        denied["future_selector"], {"kind": "enemy", "faction": "raiders"}
    )


def test_future_effects_require_selector_template_or_exact_subject_identity() -> None:
    world = "world_" + "c" * 32
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["npc"],
        "predicates": {"faction": "watch"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    exact = _event(
        world,
        "session-twelve",
        "branch-twelve",
        event_id="event.exact-mara",
        propagation="existing_and_future",
        future_selector=selector,
        subjects=["npc:mara"],
        effects=[{
            "adapter": "npc.knowledge/1",
            "domain": "npc_knowledge",
            "subject": "npc:mara",
            "field": "knowledge",
            "value": "Mara alone knows the bell code",
            "supported": True,
            "lore": "",
        }],
        affected_domains=["npc_knowledge"],
    )
    template = _event(
        world,
        "session-twelve",
        "branch-twelve",
        event_id="event.watch-template",
        priority=2,
        propagation="existing_and_future",
        future_selector=selector,
        subjects=["selector:watch-members"],
        effects=[{
            "adapter": "npc.knowledge/1",
            "domain": "npc_knowledge",
            "subject": "selector:watch-members",
            "field": "knowledge",
            "value": "Watch members know the public watchword",
            "supported": True,
            "lore": "",
        }],
        affected_domains=["npc_knowledge"],
    )
    state = {"world_events": [exact, template], "clock": {"minutes": 20}}

    lysa = future_subject_effects(
        state,
        {"id": "lysa", "name": "Lysa", "kind": "npc", "faction": "watch"},
        domains=["npc_knowledge"],
    )
    assert lysa["npc_knowledge"]["knowledge"]["value"] \
        == "Watch members know the public watchword"

    state["world_events"] = [exact]
    assert future_subject_effects(
        state,
        {"id": "lysa", "name": "Lysa", "kind": "npc", "faction": "watch"},
        domains=["npc_knowledge"],
    ) == {}
    mara = future_subject_effects(
        state,
        {"id": "mara", "name": "Mara", "kind": "npc", "faction": "watch"},
        domains=["npc_knowledge"],
    )
    assert mara["npc_knowledge"]["knowledge"]["value"] == "Mara alone knows the bell code"


def test_event_rejects_duplicate_writes_to_one_overlay_cell() -> None:
    world = "world_" + "d" * 32
    with pytest.raises(ValueError, match="one overlay cell"):
        _event(
            world,
            "session-thirteen",
            "branch-thirteen",
            effects=[
                {
                    "adapter": "faction.circumstance/1",
                    "domain": "faction",
                    "subject": "faction:watch",
                    "field": "circumstance",
                    "value": "mobilized",
                    "supported": True,
                    "lore": "",
                },
                {
                    "adapter": "faction.circumstance/1",
                    "domain": "faction",
                    "subject": "faction:watch",
                    "field": "circumstance",
                    "value": "disbanded",
                    "supported": True,
                    "lore": "",
                },
            ],
            affected_domains=["faction"],
        )


def test_effective_overlay_helpers_return_detached_consumer_views() -> None:
    world = "world_" + "b" * 32
    event = _event(world, "session-eleven", "branch-eleven")
    overlay = project_world_overlay(
        [event], world_id=world, session_id="session-eleven", branch_id="branch-eleven",
        game_time=20,
    )
    faction = effective_subject(overlay, "faction", "faction:watch")
    assert faction["circumstance"]["value"] == "mobilized"
    faction["circumstance"]["value"] = "tampered"
    assert effective_value(overlay, "faction", "faction:watch", "circumstance") == "mobilized"
    with pytest.raises(ValueError, match="unsupported"):
        effective_domain(overlay, "not-a-domain")
