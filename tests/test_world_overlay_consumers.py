from __future__ import annotations

from typing import Any

from aetherstate import creator, memory
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.compose import (
    _render_factions,
    _render_knowledge,
    _render_player,
    _render_quest,
    _render_relations,
    render_header,
)
from aetherstate.config import Config
from aetherstate.enemy_kits import build_enemy_kit
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record


def _runtime(tag: str) -> tuple[Config, Store, str, str, str]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=tag)
    world_id = creator.mint_world_id()
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "world_identity_set", "world_id": world_id}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    return cfg, store, sid, bid, world_id


def _admit(
    cfg: Config,
    store: Store,
    sid: str,
    bid: str,
    world_id: str,
    *,
    event_id: str,
    effects: list[dict[str, Any]],
    subjects: list[str],
    duration: int = 2,
    future_selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=sid,
        branch_id=bid,
        turn=1,
        game_time=1,
        cause_id=f"creator:{event_id}",
        cause_authority="creator",
        cause_visibility="public",
        affected_domains=sorted({str(effect["domain"]) for effect in effects}),
        propagation="existing_and_future" if future_selector else "existing_subjects",
        future_selector=future_selector,
        duration=duration,
        reversible=True,
        subjects=sorted(set(subjects)),
        effects=effects,
        description=f"Overlay consumer proof for {event_id}",
    )
    result = apply_delta(
        store, sid, bid, 1, [{"op": "world_event_admit", "event": event}], "user", cfg,
    )
    assert result.applied and not result.quarantined, result.quarantined
    return event


def _eligibility_effect(adapter: str, domain: str, subject: str, value: bool) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "domain": domain,
        "subject": subject,
        "field": "eligible" if domain != "quest" else "available",
        "value": value,
        "supported": True,
        "lore": "",
    }


def test_capability_event_blocks_existing_use_then_expiry_restores_it() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-capability-use")
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-brawl",
        subjects=["capability:brawl"],
        effects=[_eligibility_effect(
            "capability.eligibility/1", "capability_eligibility", "capability:brawl", False,
        )],
    )
    check = {"op": "check", "skill": "brawl", "result": 9, "tier": "success"}

    blocked = apply_delta(store, sid, bid, 1, [check], "user", cfg)

    assert not blocked.applied
    assert "capability use ineligible" in blocked.quarantined[0]["reason"]
    restored = apply_delta(store, sid, bid, 3, [check], "user", cfg)
    assert restored.applied and not restored.quarantined, restored.quarantined


def test_capability_event_blocks_enemy_pool_before_materialization_then_restores() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-enemy-pool")
    kit = build_enemy_kit("Pact Raider", "standard", "sword", {})
    effects = [
        _eligibility_effect(
            "capability.eligibility/1",
            "capability_eligibility",
            f"capability:{move['id']}",
            False,
        )
        for move in kit["moves"]
    ]
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-pact-kit",
        subjects=[f"capability:{move['id']}" for move in kit["moves"]],
        effects=effects,
    )
    spawn = {
        "op": "combatant_spawn",
        "name": "Pact Raider",
        "side": "enemy",
        "tier": "standard",
        "armament": "sword",
    }

    blocked = apply_delta(store, sid, bid, 1, [spawn], "rule", cfg)

    assert not blocked.applied
    assert "capability pool ineligible" in blocked.quarantined[0]["reason"]
    assert not (current_state(store, bid).get("combat") or {}).get("combatants")
    restored = apply_delta(store, sid, bid, 3, [spawn], "rule", cfg)
    assert restored.applied and not restored.quarantined, restored.quarantined
    combatant = next(iter(restored.state["combat"]["combatants"].values()))
    assert combatant.get("capability_pool")


def test_capability_event_blocks_existing_enemy_move_then_restores() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-existing-enemy-use")
    spawn = apply_delta(
        store,
        sid,
        bid,
        0,
        [{
            "op": "combatant_spawn",
            "name": "Pact Raider",
            "side": "enemy",
            "tier": "standard",
            "armament": "sword",
        }],
        "rule",
        cfg,
    )
    assert spawn.applied and not spawn.quarantined, spawn.quarantined
    combatant = next(iter(spawn.state["combat"]["combatants"].values()))
    moves = combatant["kit"]["moves"]
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-existing-pact-kit",
        subjects=[f"capability:{move['id']}" for move in moves],
        effects=[
            _eligibility_effect(
                "capability.eligibility/1",
                "capability_eligibility",
                f"capability:{move['id']}",
                False,
            )
            for move in moves
        ],
    )
    intent = {"op": "enemy_intent_set", "actor": combatant["id"]}

    blocked = apply_delta(store, sid, bid, 1, [intent], "rule", cfg)

    assert not blocked.applied
    assert "enemy capability use ineligible" in blocked.quarantined[0]["reason"]
    restored = apply_delta(store, sid, bid, 3, [intent], "rule", cfg)
    assert restored.applied and not restored.quarantined, restored.quarantined
    assert restored.state["combat"]["pending_intent"]


def test_future_npc_selector_blocks_generic_materialization_then_restores() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-future-npc")
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["npc"],
        "predicates": {"faction": "iron_pact"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-pact-npcs",
        subjects=["selector:pact-npcs"],
        future_selector=selector,
        effects=[_eligibility_effect(
            "spawn.eligibility/1", "enemy_eligibility", "selector:pact-npcs", False,
        )],
    )
    npc = {
        "op": "entity_add", "name": "Pact Envoy", "kind": "npc", "faction": "iron_pact",
    }

    blocked = apply_delta(store, sid, bid, 1, [npc], "user", cfg)

    assert not blocked.applied
    assert "future actor ineligible" in blocked.quarantined[0]["reason"]
    restored = apply_delta(store, sid, bid, 3, [npc], "user", cfg)
    assert restored.applied and not restored.quarantined, restored.quarantined
    assert "pact_envoy" in restored.state["entities"]


def test_future_actor_gate_cannot_be_bypassed_by_user_alias_auto_creation() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-alias-actor")
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["npc"],
        "predicates": {"name": "Forbidden Envoy"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-forbidden-envoy",
        subjects=["selector:forbidden-envoy"],
        future_selector=selector,
        effects=[_eligibility_effect(
            "spawn.eligibility/1", "enemy_eligibility", "selector:forbidden-envoy", False,
        )],
    )

    blocked = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "set_attribute", "entity": "Forbidden Envoy", "key": "role", "value": "spy"}],
        "user",
        cfg,
    )

    assert not blocked.applied
    assert "future actor ineligible" in blocked.quarantined[0]["reason"]
    assert "forbidden_envoy" not in current_state(store, bid)["entities"]


def test_unavailable_existing_quest_cannot_mutate_until_event_expires() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-quest")
    added = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "quest_add", "name": "Recover Relic"}],
        "user",
        cfg,
    )
    assert added.applied and not added.quarantined
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.block-recover-relic",
        subjects=["quest:recover_relic"],
        effects=[_eligibility_effect(
            "quest.availability/1", "quest", "quest:recover_relic", False,
        )],
    )
    update = {"op": "quest_update", "quest": "Recover Relic", "status": "complete"}

    blocked = apply_delta(store, sid, bid, 1, [update], "user", cfg)

    assert not blocked.applied
    assert "quest unavailable" in blocked.quarantined[0]["reason"]
    assert current_state(store, bid)["quests"]["recover_relic"]["status"] == "active"
    restored = apply_delta(store, sid, bid, 3, [update], "user", cfg)
    assert restored.applied and not restored.quarantined, restored.quarantined
    assert restored.state["quests"]["recover_relic"]["status"] == "complete"


def test_typed_fact_visibility_influences_retrieval_without_private_npc_leak() -> None:
    cfg, store, sid, bid, _world_id = _runtime("overlay-typed-retrieval")
    cfg.memory.top_k = 1
    knowledge = apply_delta(
        store,
        sid,
        bid,
        1,
        [{
            "op": "fact_admit",
            "statement": "The harbor cache holds the missing seal",
            "cause": "creator:test:harbor-cache",
            "authority": "creator",
        }],
        "user",
        cfg,
    )
    assert knowledge.applied and not knowledge.quarantined
    private = apply_delta(
        store,
        sid,
        bid,
        1,
        [{
            "op": "belief_acquire",
            "holder": "mara",
            "statement": "The forest cache holds the missing seal",
            "stance": "knows",
            "evidence_source": "witnessed",
        }],
        "extraction",
        cfg,
    )
    assert private.applied and not private.quarantined
    memories = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "memory_event", "text": "We searched the harbor cache beside the crane"},
            {"op": "memory_event", "text": "We searched the forest cache beneath the oak"},
        ],
        "user",
        cfg,
    )
    memory.index_applied(store, sid, bid, memories.applied, memories.state)

    selected = memory.retrieve(
        store, cfg, bid, current_state(store, bid), "Which cache held the seal?", now_turn=3,
    )

    assert len(selected) == 1
    assert "harbor cache" in selected[0]["text"]


def test_retrieval_adapter_changes_selection_before_top_k() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-retrieval-adapter")
    cfg.memory.top_k = 1
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.retrieval-harbor-bell",
        subjects=["retrieval:world"],
        effects=[{
            "adapter": "retrieval.context/1",
            "domain": "retrieval",
            "subject": "retrieval:world",
            "field": "context",
            "value": "harbor bell signal",
            "supported": True,
            "lore": "",
        }],
        duration=10,
    )
    memories = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "memory_event", "text": "The harbor bell signal opened the sea gate"},
            {"op": "memory_event", "text": "Forest berries ripened under cold rain"},
        ],
        "user",
        cfg,
    )
    memory.index_applied(store, sid, bid, memories.applied, memories.state)

    selected = memory.retrieve(
        store, cfg, bid, current_state(store, bid), "What matters now?", now_turn=3,
    )

    assert len(selected) == 1
    assert "harbor bell" in selected[0]["text"]


def test_supported_overlay_domains_reach_relevant_briefing_without_offscreen_leak() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-briefing-consumers")
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player", "present": True},
            {"op": "entity_add", "name": "Mara", "kind": "npc", "present": True},
            {"op": "entity_add", "name": "Vess", "kind": "npc", "present": False},
            {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
            {"op": "entity_add", "name": "Harbor", "kind": "location"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"STR": 12},
                    "skills": {"brawl": 2},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
            {"op": "quest_add", "name": "Recover Relic"},
            {"op": "scene_set", "location": "Harbor", "participants": ["Kael", "Mara"]},
        ],
        "genesis",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined, seeded.quarantined
    effects = [
        _eligibility_effect(
            "capability.eligibility/1", "capability_eligibility", "capability:brawl", False,
        ),
        _eligibility_effect(
            "quest.availability/1", "quest", "quest:recover_relic", False,
        ),
        {
            "adapter": "actor.condition/1", "domain": "actor", "subject": "npc:mara",
            "field": "condition", "value": "wounded but mobile", "supported": True, "lore": "",
        },
        {
            "adapter": "location.circumstance/1", "domain": "location",
            "subject": "location:harbor", "field": "circumstance",
            "value": "the sea gate is sealed", "supported": True, "lore": "",
        },
        {
            "adapter": "relationship.modifier/1", "domain": "relationship",
            "subject": "relationship:kael:mara", "field": "modifier",
            "value": 60, "supported": True, "lore": "",
        },
        {
            "adapter": "reputation.modifier/1", "domain": "reputation",
            "subject": "faction:iron_pact", "field": "modifier",
            "value": -50, "supported": True, "lore": "",
        },
        {
            "adapter": "npc.knowledge/1", "domain": "npc_knowledge",
            "subject": "npc:mara", "field": "knowledge",
            "value": "Mara knows the bell code", "supported": True, "lore": "",
        },
        {
            "adapter": "npc.knowledge/1", "domain": "npc_knowledge",
            "subject": "npc:vess", "field": "knowledge",
            "value": "Vess knows the vault cipher", "supported": True, "lore": "",
        },
        {
            "adapter": "npc.behavior/1", "domain": "npc_behavior",
            "subject": "npc:mara", "field": "behavior",
            "value": "Mara checks the western door before speaking", "supported": True,
            "lore": "",
        },
        {
            "adapter": "npc.behavior/1", "domain": "npc_behavior",
            "subject": "npc:vess", "field": "behavior",
            "value": "Vess avoids the lantern windows", "supported": True, "lore": "",
        },
        {
            "adapter": "briefing.context/1", "domain": "briefing",
            "subject": "briefing:world", "field": "context",
            "value": "The council is waiting for a formal witness", "supported": True,
            "lore": "",
        },
        {
            "adapter": "narration.context/1", "domain": "narration",
            "subject": "narration:world", "field": "context",
            "value": "Keep the sealed sea gate audible in the scene", "supported": True,
            "lore": "",
        },
    ]
    subjects = [str(effect["subject"]) for effect in effects]
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.briefing-consumers",
        subjects=subjects,
        effects=effects,
        duration=10,
    )
    state = current_state(store, bid)

    assert "Brawl" in _render_player(state, cfg)
    assert "[unavailable]" in _render_player(state, cfg)
    assert "Recover Relic" not in _render_quest(state, cfg)
    assert "Mara: Ally" in _render_relations(state, cfg)
    assert "Iron Pact: Hostile" in _render_factions(state)
    knowledge = _render_knowledge(state)
    assert "Mara knows the bell code" in knowledge
    assert "Vess knows the vault cipher" not in knowledge
    assert "Mara checks the western door before speaking" in knowledge
    assert "Vess avoids the lantern windows" not in knowledge
    assert "The council is waiting for a formal witness" in knowledge
    assert "Keep the sealed sea gate audible in the scene" in knowledge
    header = render_header(state, cfg)
    assert "Mara condition: wounded but mobile" in header
    assert "location circumstance: the sea gate is sealed" in header


def test_future_npc_knowledge_projects_to_actor_created_after_event() -> None:
    cfg, store, sid, bid, world_id = _runtime("overlay-future-npc-knowledge")
    player = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player", "present": True},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {"skills": {}, "abilities": [], "resources": {"hp": {"max": 20}}},
            },
        ],
        "genesis",
        cfg,
    )
    assert player.applied and not player.quarantined
    selector_payload = {
        "schema": "aetherstate-world-subject-selector/1",
        "subject_kinds": ["npc"],
        "predicates": {"faction": "iron_pact"},
    }
    selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
    _admit(
        cfg,
        store,
        sid,
        bid,
        world_id,
        event_id="event.future-pact-knowledge",
        subjects=["selector:future-pact-member"],
        future_selector=selector,
        effects=[
            {
                "adapter": "npc.knowledge/1",
                "domain": "npc_knowledge",
                "subject": "selector:future-pact-member",
                "field": "knowledge",
                "value": "Pact members know the sealed harbor route",
                "supported": True,
                "lore": "",
            },
            {
                "adapter": "npc.behavior/1",
                "domain": "npc_behavior",
                "subject": "selector:future-pact-member",
                "field": "behavior",
                "value": "Pact members test every harbor seal before entry",
                "supported": True,
                "lore": "",
            },
        ],
        duration=10,
    )
    created = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "entity_add", "name": "Lysa", "kind": "npc", "present": True,
             "faction": "iron_pact"},
            {"op": "set_attribute", "entity": "Lysa", "key": "faction",
             "value": "iron_pact"},
        ],
        "user",
        cfg,
    )
    assert created.applied and not created.quarantined, created.quarantined

    knowledge = _render_knowledge(current_state(store, bid))

    assert "npc_knowledge:Lysa=Pact members know the sealed harbor route" in knowledge
    assert "npc_behavior:Lysa=Pact members test every harbor seal before entry" in knowledge
