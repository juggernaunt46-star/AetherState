"""Fresh-foe swipe retries require an actual first hostile spawn receipt."""
from __future__ import annotations

import json
import random

from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.enemy_rolls = True
    cfg.specialization.combat_opening_primer = True
    cfg.injection.max_tokens = 2400
    cfg.injection.priorities["combat_primer"] = 74
    return cfg


def _pipeline(external_id: str, *, opening_enemy: bool = False):
    cfg = _cfg()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id=external_id)
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {"athletics": 1},
            "resources": {"hp": {"max": 24}},
        }},
    ], "genesis", cfg)
    assert seeded.applied
    if opening_enemy:
        first = apply_delta(store, sid, branch, 0, [{
            "op": "combatant_spawn", "name": "Gate Guard", "side": "enemy",
            "tier": "standard", "armament": "spear",
        }], "rule", cfg)
        assert first.applied and first.applied[0]["side"] == "enemy"
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, rng=random.Random(73))
    body = json.dumps({
        "model": "m",
        "messages": [{"role": "user", "content": "I hold my ground."}],
    }).encode()
    _normal, ctx = pipe.process(
        Stamp(session=external_id, gen_type="normal", turn=1, user="Kael"), body)
    assert ctx is not None
    return cfg, store, sid, branch, pipe, body, ctx


def _swipe(pipe: Pipeline, body: bytes, external_id: str) -> str:
    packet, ctx = pipe.process(
        Stamp(session=external_id, gen_type="swipe", turn=1, user="Kael"), body)
    assert ctx is not None and ctx.klass == "swipe"
    return packet.decode()


def _system_text(packet: str) -> str:
    return "\n".join(
        str(row.get("content", ""))
        for row in json.loads(packet)["messages"]
        if row.get("role") == "system"
    )


def test_first_enemy_spawn_receipt_keeps_fresh_foe_retry_boundary():
    external_id = "fresh-foe-first-enemy"
    _cfg_, store, sid, branch, pipe, body, ctx = _pipeline(external_id)
    spawned = apply_delta(store, sid, branch, ctx.turn_index, [{
        "op": "combatant_spawn", "name": "Spear Marshal", "side": "enemy",
        "tier": "standard", "armament": "spear and shield",
    }], "rule", _cfg_)
    receipt = spawned.applied[0]
    assert receipt["op"] == "combatant_spawn" and receipt["side"] == "enemy"
    assert receipt.get("_cid") and receipt.get("_initial_intent")

    packet = _swipe(pipe, body, external_id)

    assert "FRESH-FOE NARRATION RETRY" in packet
    assert "[ENEMY INTENT enemy-intent/1]" not in packet
    assert "\n[WAR] round " not in _system_text(packet)


def test_ally_spawn_receipt_keeps_ordinary_swipe_behavior():
    external_id = "fresh-foe-ally"
    _cfg_, store, sid, branch, pipe, body, ctx = _pipeline(external_id)
    spawned = apply_delta(store, sid, branch, ctx.turn_index, [{
        "op": "combatant_spawn", "name": "Shieldmate", "side": "ally",
        "tier": "standard", "armament": "shield and mace",
    }], "rule", _cfg_)
    receipt = spawned.applied[0]
    assert receipt["op"] == "combatant_spawn" and receipt["side"] == "ally"
    assert receipt.get("_cid") and not receipt.get("_initial_intent")

    packet = _swipe(pipe, body, external_id)

    assert "ALREADY SETTLED" in packet and "combatant spawn" in packet
    assert "FRESH-FOE NARRATION RETRY" not in packet
    assert "\n[WAR] round " in _system_text(packet)


def test_mid_combat_enemy_reinforcement_keeps_action_and_intent_visible_on_swipe():
    external_id = "fresh-foe-reinforcement"
    _cfg_, store, sid, branch, pipe, body, ctx = _pipeline(
        external_id, opening_enemy=True)
    settled = store.rule_ops_at(branch, ctx.turn_index)
    assert any(isinstance(op.get("_opposition"), dict) for op in settled)
    reinforced = apply_delta(store, sid, branch, ctx.turn_index, [{
        "op": "combatant_spawn", "name": "Crossbow Reserve", "side": "enemy",
        "tier": "standard", "armament": "crossbow",
    }], "rule", _cfg_)
    receipt = reinforced.applied[0]
    assert receipt["op"] == "combatant_spawn" and receipt["side"] == "enemy"
    assert receipt.get("_cid") and not receipt.get("_initial_intent")

    packet = _swipe(pipe, body, external_id)

    assert "ALREADY SETTLED" in packet and "combatant spawn" in packet
    assert "FRESH-FOE NARRATION RETRY" not in packet
    assert "[ENEMY ACTION enemy-action/1]" in packet
    assert "[ENEMY INTENT enemy-intent/1]" in packet
    assert "\n[WAR] round " in _system_text(packet)
