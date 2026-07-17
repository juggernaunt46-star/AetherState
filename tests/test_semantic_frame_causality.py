"""Exact semantic-frame causality through derived RPG referee passes.

The semantic vertical does not stop at the first settled mechanic.  These fixtures prove that a
derived consequence keeps the exact frame only when all of *its own* current causes agree.  They
also exercise journal replay so provenance cannot be a transient in-memory decoration.
"""
from __future__ import annotations

from aetherstate import creator
from aetherstate.canon import canonicalize, chain
from aetherstate.config import Config
from aetherstate.semantic import ActionFrame
from aetherstate.state import (
    THREAT_XP,
    apply_delta,
    battle_ops,
    combat_ops,
    current_state,
    faction_cascade_ops,
    progression_ops,
    world_ops,
)
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _frame(
    target_id: str,
    *,
    frame_id: str = "f1",
    capability_id: str = "melee",
    action_class: str = "weapon_attack",
) -> dict:
    source = f"Kael uses {capability_id} toward {target_id}."
    frame = ActionFrame(
        frame_id=frame_id,
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="kael",
        capability_id=capability_id,
        action_class=action_class,
        target_entity_id=target_id,
        target_name=target_id.replace("_", " ").title(),
        polarity="positive",
        modality="actual",
        time_scope="current",
    )
    start = source.index(capability_id)
    frame.add_evidence("capability", start, start + len(capability_id), capability_id)
    return frame.snapshot(source)


def _runtime(tag: str, *, enemies: tuple[str, ...] = (), hp: int = 20,
             db=":memory:"):
    cfg = _cfg()
    store = Store(db)
    sid, bid = store.create_session(external_id=tag)
    ops = [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {
            "op": "player_seed",
            "entity": "Kael",
            "card": {
                "stats": {"STR": 14},
                "skills": {"melee": 3, "travel": 1},
                "resources": {"hp": {"cur": hp, "max": hp}},
            },
        },
    ]
    for name in enemies:
        ops.extend(
            [
                {"op": "entity_add", "name": name},
                {"op": "presence", "entity": name, "present": True},
            ]
        )
    seeded = apply_delta(store, sid, bid, 0, ops, "genesis", cfg)
    assert not seeded.quarantined
    for name in enemies:
        spawned = apply_delta(
            store,
            sid,
            bid,
            0,
            [
                {
                    "op": "combatant_spawn",
                    "name": name,
                    "side": "enemy",
                    "char": name,
                    "tier": "minion",
                }
            ],
            "rule",
            cfg,
        )
        assert not spawned.quarantined
        state = current_state(store, bid)
        cid, _eid = _combatant(state, name)
        softened = apply_delta(
            store,
            sid,
            bid,
            0,
            # A minion has 6 HP and the reducer's bounded swing accepts -5 exactly: leave it
            # alive at 1 so the referenced turn-1 hit, not fixture setup, is the lethal cause.
            [{"op": "combatant_hp", "target": cid, "delta": -5}],
            "rule",
            cfg,
        )
        assert not softened.quarantined
    return cfg, store, sid, bid


def _combatant(state: dict, name: str) -> tuple[str, str]:
    for cid, row in state["combat"]["combatants"].items():
        if row["name"] == name:
            return cid, row["eid"]
    raise AssertionError(f"missing combatant {name}")


def _assert_replays(store: Store, bid: str, expected: dict) -> None:
    assert current_state(store, bid) == expected


def test_enemy_defeat_xp_and_combat_end_replay_the_attack_frame():
    cfg, store, sid, bid = _runtime("semantic-cause-enemy", enemies=("Bandit",))
    cid, eid = _combatant(current_state(store, bid), "Bandit")
    frame = _frame(eid)
    hit = apply_delta(
        store,
        sid,
        bid,
        1,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            {
                "op": "combatant_hp",
                "target": cid,
                "delta": -999,
                "_semantic_frame_ref": frame["fingerprint"],
            },
        ],
        "rule",
        cfg,
    )
    assert not hit.quarantined

    derived = combat_ops(hit.state, hit.applied, prepare_intent=False)
    assert {op["op"] for op in derived} == {"combatant_defeat", "award_exp", "combat_end"}
    assert all(op.get("_semantic_frame_ref") == frame["fingerprint"] for op in derived)

    settled = apply_delta(store, sid, bid, 1, derived, "rule", cfg)
    assert not settled.quarantined
    journal = store.rule_ops_at(bid, 1)
    replayed = current_state(store, bid)
    assert all(
        op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in journal
        if op.get("op") in {"combatant_defeat", "award_exp", "combat_end"}
    )
    assert replayed["player"]["kael"]["xp"] == THREAT_XP["minion"]
    assert not replayed["combat"]["active"]
    _assert_replays(store, bid, settled.state)


def test_mixed_enemy_frames_stay_per_cause_and_do_not_claim_one_combat_end():
    cfg, store, sid, bid = _runtime(
        "semantic-cause-mixed", enemies=("Bandit One", "Bandit Two")
    )
    before = current_state(store, bid)
    one_cid, one_eid = _combatant(before, "Bandit One")
    two_cid, two_eid = _combatant(before, "Bandit Two")
    one = _frame(one_eid, frame_id="f1")
    two = _frame(two_eid, frame_id="f2")
    hit = apply_delta(
        store,
        sid,
        bid,
        1,
        [
            {"op": "semantic_frame_commit", "frame": one},
            {"op": "semantic_frame_commit", "frame": two},
            {
                "op": "combatant_hp",
                "target": one_cid,
                "delta": -999,
                "_semantic_frame_ref": one["fingerprint"],
            },
            {
                "op": "combatant_hp",
                "target": two_cid,
                "delta": -999,
                "_semantic_frame_ref": two["fingerprint"],
            },
        ],
        "rule",
        cfg,
    )
    assert not hit.quarantined

    derived = combat_ops(hit.state, hit.applied, prepare_intent=False)
    defeats = {op["target"]: op for op in derived if op["op"] == "combatant_defeat"}
    awards = {op["reason"].removeprefix("defeated "): op for op in derived if op["op"] == "award_exp"}
    ending = next(op for op in derived if op["op"] == "combat_end")
    assert defeats[one_cid]["_semantic_frame_ref"] == one["fingerprint"]
    assert defeats[two_cid]["_semantic_frame_ref"] == two["fingerprint"]
    assert awards["Bandit One"]["_semantic_frame_ref"] == one["fingerprint"]
    assert awards["Bandit Two"]["_semantic_frame_ref"] == two["fingerprint"]
    assert "_semantic_frame_ref" not in ending

    settled = apply_delta(store, sid, bid, 1, derived, "rule", cfg)
    assert not settled.quarantined
    assert not current_state(store, bid)["combat"]["active"]
    _assert_replays(store, bid, settled.state)


def test_autonomous_enemy_defeat_and_combat_end_remain_unframed_and_replay():
    cfg, store, sid, bid = _runtime(
        "semantic-cause-player-defeat", enemies=("Raider",), hp=5
    )
    before = current_state(store, bid)
    _cid, enemy_eid = _combatant(before, "Raider")
    intent = before["combat"]["pending_intent"]
    frame = _frame(enemy_eid)
    hurt = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            {
                "op": "hp_adj",
                "char": "kael",
                "delta": -999,
                "_opposition": {
                    "intent_id": intent["id"],
                    "actor": intent["actor"],
                    "actor_name": intent["actor_name"],
                    "move_id": intent["move_id"],
                    "move_name": intent["move_name"],
                    "target": "kael",
                    "target_name": "Kael",
                },
                "_semantic_frame_ref": frame["fingerprint"],
            },
        ],
        "rule",
        cfg,
    )
    assert not hurt.quarantined
    opposition = next(op for op in hurt.applied if op.get("op") == "hp_adj")
    assert opposition["_delta"] == -5
    assert "_semantic_frame_ref" not in opposition

    ending = combat_ops(
        hurt.state, hurt.applied, prepare_intent=False
    )
    combat_end = next(op for op in ending if op["op"] == "combat_end")
    assert combat_end["outcome"] == "defeat"
    assert "_semantic_frame_ref" not in combat_end
    ended = apply_delta(store, sid, bid, 2, ending, "rule", cfg)
    assert not ended.quarantined

    defeat = progression_ops(ended.state, hurt.applied)
    assert [op["op"] for op in defeat] == ["defeat_resolve"]
    assert "_semantic_frame_ref" not in defeat[0]
    resolved = apply_delta(store, sid, bid, 2, defeat, "rule", cfg)
    assert not resolved.quarantined

    journal = store.rule_ops_at(bid, 2)
    assert all(
        "_semantic_frame_ref" not in op
        for op in journal
        if op.get("op") in {"hp_adj", "defeat_resolve", "combat_end"}
    )
    replayed = current_state(store, bid)
    assert replayed["player"]["kael"]["defeated"]["outcome"] == "wake_safe"
    assert not replayed["combat"]["active"]
    _assert_replays(store, bid, resolved.state)


def test_legacy_stamped_opposition_reopens_and_forks_without_lending_its_player_frame(tmp_path):
    db = tmp_path / "legacy-opposition-frame.sqlite3"
    cfg, store, sid, bid = _runtime(
        "semantic-cause-legacy-opposition", enemies=("Raider",), hp=5, db=db,
    )
    before = current_state(store, bid)
    _cid, enemy_eid = _combatant(before, "Raider")
    intent = before["combat"]["pending_intent"]
    frame = _frame(enemy_eid)
    committed = apply_delta(
        store, sid, bid, 2,
        [{"op": "semantic_frame_commit", "frame": frame}],
        "rule", cfg,
    )
    assert not committed.quarantined

    # Simulate a journal row written before the causality repair.  Replay must preserve its baked
    # mechanics and bytes, but new descendants must not borrow the inverted Player frame.
    legacy_opposition = {
        "op": "hp_adj",
        "char": "kael",
        "delta": -5,
        "_delta": -5,
        "_turn": 2,
        "_opposition": {
            "intent_id": intent["id"],
            "actor": intent["actor"],
            "actor_name": intent["actor_name"],
            "move_id": intent["move_id"],
            "move_name": intent["move_name"],
            "target": "kael",
            "target_name": "Kael",
        },
        "_semantic_frame_ref": frame["fingerprint"],
    }
    store.journal(bid, 2, 2, [legacy_opposition], "rule")
    legacy_state = current_state(store, bid)
    assert legacy_state["player"]["kael"]["hp"]["cur"] == 0
    assert store.rule_ops_at(bid, 2)[-1]["_semantic_frame_ref"] == frame["fingerprint"]
    transcript = canonicalize([
        {"role": "user", "content": "I brace against the raider."},
    ])
    heads = chain(transcript)
    store.append_msgs(
        bid,
        0,
        [
            (message.role, message.content_hash, head)
            for message, head in zip(transcript, heads)
        ],
    )
    store.record_turn(bid, 2, "normal", "normal")
    store.write_turn_hashes(bid, 2, user_hash=transcript[0].content_hash)
    store.close()

    reopened = Store(db)
    try:
        replayed = current_state(reopened, bid)
        assert replayed == legacy_state
        forked = reopened.fork_branch(bid, at_pos=len(transcript), fork_turn=2)
        assert current_state(reopened, forked) == legacy_state

        ending = combat_ops(replayed, [legacy_opposition], prepare_intent=False)
        defeat = progression_ops(replayed, [legacy_opposition])
        assert all(
            "_semantic_frame_ref" not in op
            for op in [*ending, *defeat]
            if op.get("op") in {"combat_end", "defeat_resolve"}
        )
    finally:
        reopened.close()


def _world_runtime(tag: str):
    cfg, store, sid, bid = _runtime(tag)
    identity = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "world_identity_set", "world_id": creator.mint_world_id()}],
        "user",
        cfg,
    )
    assert identity.applied and not identity.quarantined
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
            {"op": "entity_add", "name": "The Docks", "kind": "location"},
            {"op": "entity_add", "name": "Old Gate", "kind": "location"},
            {"op": "scene_set", "location": "The Docks"},
            {
                "op": "front_add",
                "name": "The Iron Pact Rearms",
                "faction": "Iron Pact",
                "segments": 3,
                "pace": 1,
                "consequence": "The Pact marches.",
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    prep1 = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "prep 1"}],
        "rule",
        cfg,
    )
    assert not prep1.quarantined
    prep2 = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "prep 2"},
            {"op": "time_advance", "to_time_of_day": "late_night"},
        ],
        "rule",
        cfg,
    )
    assert not prep2.quarantined
    return cfg, store, sid, bid


def test_travel_and_its_day_front_consequences_replay_but_idle_does_not_claim_a_frame():
    cfg, store, sid, bid = _world_runtime("semantic-cause-travel")
    frame = _frame("old_gate", capability_id="travel", action_class="travel")
    moved = apply_delta(
        store,
        sid,
        bid,
        3,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            {
                "op": "scene_set",
                "location": "Old Gate",
                "_semantic_frame_ref": frame["fingerprint"],
            },
        ],
        "rule",
        cfg,
    )
    assert not moved.quarantined
    consequences = world_ops(
        moved.state,
        moved.applied,
        clock_turns=6,
        session_id=sid,
        branch_id=bid,
        turn_index=3,
    )
    assert [op["op"] for op in consequences] == [
        "time_advance",
        "front_tick",
        "world_flag",
        "memory_event",
        "world_event_admit",
    ]
    assert all(
        op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in consequences[:-1]
    )
    # The immutable event has its own rule-owned front-completion cause.  It keeps that authority
    # distinct instead of borrowing the Player ActionFrame that triggered this day's clock tick.
    assert consequences[-1]["event"]["cause_authority"] == "rule"
    assert consequences[-1]["event"]["semantic_frame_ref"] is None
    applied = apply_delta(store, sid, bid, 3, consequences, "rule", cfg)
    assert not applied.quarantined
    replayed = current_state(store, bid)
    assert replayed["clock"]["day"] == 2
    assert replayed["fronts"]["the_iron_pact_rearms"]["done"]
    _assert_replays(store, bid, applied.state)

    idle_cfg, idle_store, idle_sid, idle_bid = _world_runtime("semantic-cause-idle")
    idle_seed = apply_delta(
        idle_store,
        idle_sid,
        idle_bid,
        8,
        [{"op": "memory_event", "text": "The road waits."}],
        "rule",
        idle_cfg,
    )
    assert not idle_seed.quarantined
    idle = world_ops(
        idle_seed.state,
        idle_seed.applied,
        clock_turns=6,
        session_id=idle_sid,
        branch_id=idle_bid,
        turn_index=8,
    )
    assert [op["op"] for op in idle] == [
        "time_advance",
        "front_tick",
        "world_flag",
        "memory_event",
        "world_event_admit",
    ]
    assert all("_semantic_frame_ref" not in op for op in idle)
    idle_applied = apply_delta(idle_store, idle_sid, idle_bid, 8, idle, "rule", idle_cfg)
    assert not idle_applied.quarantined
    _assert_replays(idle_store, idle_bid, idle_applied.state)


def test_faction_cascade_and_cleared_battle_wave_inherit_their_exact_causes():
    cfg, store, sid, bid = _runtime("semantic-cause-faction")
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Mira"},
            {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
            {"op": "set_attribute", "entity": "Mira", "key": "faction", "value": "Iron Pact"},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    frame = _frame("mira", capability_id="diplomacy", action_class="social_action")
    changed = apply_delta(
        store,
        sid,
        bid,
        1,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            {
                "op": "affinity_adj",
                "target": "Mira",
                "delta": 20,
                "_semantic_frame_ref": frame["fingerprint"],
            },
        ],
        "rule",
        cfg,
    )
    assert not changed.quarantined
    cascade = faction_cascade_ops(changed.state, changed.applied, 0.1)
    assert len(cascade) == 1
    assert cascade[0]["_semantic_frame_ref"] == frame["fingerprint"]
    cascaded = apply_delta(store, sid, bid, 1, cascade, "rule", cfg)
    assert not cascaded.quarantined
    _assert_replays(store, bid, cascaded.state)

    battle_cfg, battle_store, battle_sid, battle_bid = _runtime(
        "semantic-cause-battle", enemies=("Raider",)
    )
    opened = apply_delta(
        battle_store,
        battle_sid,
        battle_bid,
        0,
        [
            {
                "op": "battle_start",
                "name": "The Breach",
                "momentum": -1,
                "foe": "Raider",
                "wave_size": 1,
            }
        ],
        "user",
        battle_cfg,
    )
    assert not opened.quarantined
    cid, eid = _combatant(opened.state, "Raider")
    strike = _frame(eid)
    hit = apply_delta(
        battle_store,
        battle_sid,
        battle_bid,
        2,
        [
            {"op": "semantic_frame_commit", "frame": strike},
            {
                "op": "combatant_hp",
                "target": cid,
                "delta": -999,
                "_semantic_frame_ref": strike["fingerprint"],
            },
        ],
        "rule",
        battle_cfg,
    )
    referee = combat_ops(hit.state, hit.applied, prepare_intent=False)
    defeated = apply_delta(battle_store, battle_sid, battle_bid, 2, referee, "rule", battle_cfg)
    assert not defeated.quarantined
    wave = battle_ops(defeated.state, defeated.applied)
    assert {op["op"] for op in wave} == {"combatant_spawn", "battle_wave"}
    assert all(op.get("_semantic_frame_ref") == strike["fingerprint"] for op in wave)
    waved = apply_delta(battle_store, battle_sid, battle_bid, 2, wave, "rule", battle_cfg)
    assert not waved.quarantined
    assert current_state(battle_store, battle_bid)["battle"]["waves"] == 1
    _assert_replays(battle_store, battle_bid, waved.state)
