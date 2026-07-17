from __future__ import annotations

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record


def _runtime(label: str) -> tuple[Config, Store, str, str, str]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=label)
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


def _actor_event(session_id: str, branch_id: str, world_id: str) -> dict:
    return build_world_event_record(
        event_id="event.actor.mara-alert",
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=1,
        game_time=1,
        cause_id="creator:mara-alert",
        cause_authority="creator",
        cause_visibility="public",
        affected_domains=["actor"],
        propagation="existing_subjects",
        subjects=["actor:mara"],
        effects=[{
            "adapter": "actor.condition/1",
            "domain": "actor",
            "subject": "actor:mara",
            "field": "condition",
            "value": "alert",
            "supported": True,
            "lore": "",
        }],
        description="Mara is alert.",
    )


def test_absent_exact_actor_event_rejects_and_never_activates_later() -> None:
    cfg, store, session_id, branch_id, world_id = _runtime("event-absent-actor")
    event = _actor_event(session_id, branch_id, world_id)

    rejected = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{"op": "world_event_admit", "event": event}],
        "user",
        cfg,
    )

    assert not rejected.applied
    assert "does not exist at admission" in rejected.quarantined[0]["reason"]
    assert store.world_event_records(branch_id) == []
    assert not current_state(store, branch_id).get("world_events")

    created = apply_delta(
        store,
        session_id,
        branch_id,
        2,
        [{"op": "entity_add", "name": "Mara", "kind": "npc"}],
        "user",
        cfg,
    )
    assert created.applied and not created.quarantined
    assert not current_state(store, branch_id).get("world_events")


def test_same_batch_existing_actor_then_event_is_admitted_atomically() -> None:
    cfg, store, session_id, branch_id, world_id = _runtime("event-same-batch-actor")
    event = _actor_event(session_id, branch_id, world_id)

    admitted = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [
            {"op": "entity_add", "name": "Mara", "kind": "npc"},
            {"op": "world_event_admit", "event": event},
        ],
        "user",
        cfg,
    )

    assert admitted.applied and not admitted.quarantined
    assert store.world_event_records(branch_id) == [event]
    assert admitted.state["world_overlay"]["active_event_ids"] == [event["event_id"]]
