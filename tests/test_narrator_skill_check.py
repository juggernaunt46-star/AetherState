"""Source-free narrator realization for completed generic skill checks."""
from __future__ import annotations

from copy import deepcopy
import json
import random

import pytest

from aetherstate import tier0
from aetherstate.compose import _render_directive, compose
from aetherstate.config import Config
from aetherstate.narrator_realization import (
    NarratorRealizationError,
    SKILL_CHECK_REALIZATION_ADAPTER,
    build_narrator_realization,
    build_narrator_realization_from_state,
    render_narrator_realization,
)
from aetherstate.state import apply_delta, current_state, progression_ops
from aetherstate.store import Store
from tests.test_skill_check_settlement_state import _ops, _runtime, _semantic_ops


def _targetless_state():
    cfg, store, session_id, branch_id = _runtime("narrator-skill-targetless")
    result = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )
    assert not result.quarantined
    return cfg, result.state


def _targetful_state():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="narrator-skill-targetful")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "Iven", "kind": "npc"},
            {"op": "player_seed", "entity": "Kael", "card": {
                "stats": {"CUN": 14},
                "skills": {"perception": 3},
                "abilities": [],
                "resources": {"hp": {"max": 20}},
            }},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    tier0_result = tier0.run(
        {"messages": [{
            "role": "user",
            "content": "I study Iven. ((aether.check perception at Iven vs 9))",
        }]},
        "new_turn",
        False,
        current_state(store, branch_id),
        cfg,
        random.Random(4),
        turn=1,
    )
    applied = apply_delta(
        store, session_id, branch_id, 1, tier0_result.rule_ops, "rule", cfg,
    )
    assert not applied.quarantined
    return cfg, applied.state


def _lethal_unframed_enemy_fallout(
    *, player_settlement: bool = True,
) -> tuple[Config, dict]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="narrator-lethal-fallout")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "Orlen", "kind": "npc"},
            {"op": "player_seed", "entity": "Kael", "card": {
                "stats": {"DEX": 14},
                "skills": {"stealth": 3},
                "abilities": [],
                "resources": {"hp": {"max": 3}},
            }},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined

    semantic_turn = _ops(cfg, store, branch_id)
    player = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        semantic_turn if player_settlement else _semantic_ops(semantic_turn),
        "rule",
        cfg,
    )
    assert not player.quarantined
    hurt = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{
            "op": "hp_adj",
            "char": "kael",
            "delta": -3,
            "_opposition": {
                "intent_id": "intent-orlen-measured-cut",
                "move_id": "measured-cut",
                "move_name": "Measured Cut",
                "actor": "orlen",
                "actor_name": "Orlen",
                "target": "kael",
                "target_name": "Kael",
                "total_raw": 8,
                "total": 8,
                "tier": "HITS",
                "damage": 3,
                "delivery": "a hooked glaive",
                "basis": "physical",
                "range": "close",
                "timing": "fast",
                "cadence": "reliable",
                "sensory": "steel bites and breath leaves the body",
            },
        }],
        "rule",
        cfg,
    )
    assert not hurt.quarantined
    opposition = hurt.state["player"]["kael"]["_opposition_last"]
    assert opposition["hp_cur"] == 0 and opposition["hp_max"] == 3
    assert "_semantic_frame_ref" not in next(
        op for op in hurt.applied if op["op"] == "hp_adj"
    )

    defeat = progression_ops(hurt.state, hurt.applied)
    assert [op["op"] for op in defeat] == ["defeat_resolve"]
    assert "_semantic_frame_ref" not in defeat[0]
    resolved = apply_delta(
        store, session_id, branch_id, 1, defeat, "rule", cfg,
    )
    assert not resolved.quarantined
    assert resolved.state["player"]["kael"]["hp"] == {"cur": 1, "max": 3}
    return cfg, resolved.state


def _compose_texts(cfg: Config, state: dict) -> tuple[str, str]:
    output, _kept = compose(
        {"messages": [{"role": "user", "content": "I survey the eastern vault."}]},
        state,
        cfg,
        None,
        "new_turn",
    )
    system = next(
        message["content"] for message in output["messages"]
        if message.get("role") == "system"
        and "[AETHERSTATE TURN PACKET" in message.get("content", "")
    )
    current = next(
        message["content"] for message in output["messages"]
        if message.get("role") == "user"
    )
    return system, current


def test_targetless_skill_realization_is_qualitative_and_source_free():
    _cfg, state = _targetless_state()
    packet = build_narrator_realization_from_state(state)

    assert packet is not None
    assert packet["asserted_unresolved"] == []
    settled = packet["asserted_settled"]
    assert len(settled) == 1
    row = settled[0]
    assert row["adapter_id"] == SKILL_CHECK_REALIZATION_ADAPTER
    assert row["event_meaning"]["capability_id"] == "stealth"
    assert row["event_meaning"]["target_entity_id"] is None
    assert row["impact_kind"] == row["impact_magnitude"] == "none"
    assert row["target_state"] == "not_applicable"
    assert row["settled_change_kinds"] == ["mastery"]

    encoded = json.dumps(packet, sort_keys=True)
    assert "I slip unseen past the watch" not in encoded
    for raw_mechanic_field in ('"applied_changes"', '"delta"', '"post"', '"cur"', '"max"'):
        assert raw_mechanic_field not in encoded
    rendered = render_narrator_realization(packet)
    assert "only their settled_change_kinds are committed ledger changes" in rendered
    assert "PLAYER SKILL CHECK" in rendered
    assert "capability Stealth" in rendered
    assert "exact referent none" in rendered
    assert "TARGET IMPACT NONE" in rendered
    assert "Invent no hit, damage, injury" in rendered

    system, current = _compose_texts(_cfg, state)
    assert system.count("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1") == 1
    assert current.count("[AETHER P0 CURRENT CODE-SETTLED RESULT — INPUT ONLY]") == 1
    assert "capability Stealth" in current
    assert "exact referent none" in current
    assert "TARGET IMPACT NONE" in current
    assert "prior assistant action descriptions are continuity only" in current
    assert "\"asserted_settled\"" not in current


def test_targetful_skill_keeps_referent_as_meaning_without_target_authority():
    _cfg, state = _targetful_state()
    receipt = state["mechanic_settlements"][0]["receipt"]
    packet = build_narrator_realization_from_state(state)

    assert receipt["target_post_state"] is None
    assert packet is not None
    row = packet["asserted_settled"][0]
    assert row["event_meaning"]["target_entity_id"] == "iven"
    assert row["impact_kind"] == row["impact_magnitude"] == "none"
    assert row["target_state"] == "not_applicable"
    assert set(row["settled_change_kinds"]) <= {
        "cost", "mastery", "cooldown", "consequence",
    }

    forged = deepcopy(row)
    forged["target_state"] = "active"
    with pytest.raises(NarratorRealizationError, match="cannot claim target impact"):
        build_narrator_realization(
            1,
            asserted_settled=[forged],
            forbidden_inference=packet["forbidden_inference"],
        )


def test_v3_player_settlement_stays_separate_from_lethal_enemy_fallout():
    cfg, mixed = _lethal_unframed_enemy_fallout()
    packet = build_narrator_realization_from_state(mixed)

    assert packet is not None and len(packet["asserted_settled"]) == 1
    opposition = mixed["player"]["kael"]["_opposition_last"]
    assert "_semantic_frame_ref" not in opposition
    assert "_settlement_event_ref" not in opposition

    system, current = _compose_texts(cfg, mixed)
    exact_realization = render_narrator_realization(packet)
    assert system.count(exact_realization) == 1
    assert system.count("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1") == 1
    assert system.count("[ENEMY ACTION enemy-action/1]") == 1
    assert current.count("[ENEMY ACTION enemy-action/1]") == 1
    assert "[ENEMY INTENT enemy-intent/1]" not in system
    assert "[ENEMY INTENT enemy-intent/1]" not in current
    assert "SETTLED ENEMY ACTION OUTPUT SHAPE" in current
    assert "HP immediately after this impact was 0/3" in system
    assert "HP immediately after this impact was 0/3" in current
    assert "[PLAYER] Kael" in system and "HP 1/3" in system
    assert "current HP is 0/3" not in system
    assert "current HP is 0/3" not in current
    assert "use [PLAYER] and defeat state for any later post-fallout/current HP" in system
    assert "NARRATE CODE-SETTLED FALLOUT" in system
    assert "separate occurrences unless exact occurrence references link them" in system
    assert "does not cancel, replace, undo, or become the cause" in system
    assert "does not imply that settlement failed or did not happen" in system
    for contradictory in (
        "Narrate exactly these outcomes",
        "Narrate exactly this outcome",
        "always resolves the Player's NEWEST message",
    ):
        assert contradictory not in system
        assert contradictory not in current


def test_v3_lethal_enemy_fallout_without_player_settlement_invents_none():
    cfg, mixed = _lethal_unframed_enemy_fallout(player_settlement=False)

    packet = build_narrator_realization_from_state(mixed)
    assert packet is not None and packet["asserted_settled"] == []
    system, current = _compose_texts(cfg, mixed)
    assert system.count(render_narrator_realization(packet)) == 1
    assert system.count("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1") == 1
    assert '"adapter_id":"narrator.skill-check/1"' not in system
    assert system.count("[ENEMY ACTION enemy-action/1]") == 1
    assert current.count("[ENEMY ACTION enemy-action/1]") == 1
    assert "[ENEMY INTENT enemy-intent/1]" not in system
    assert "[ENEMY INTENT enemy-intent/1]" not in current
    assert "NARRATE CODE-SETTLED FALLOUT" in system
    assert "asserted_settled Player result from NARRATOR REALIZATION, if any" in system
    assert "always resolves the Player's NEWEST message" not in system
    assert "always resolves the Player's NEWEST message" not in current


def test_non_v3_defeat_keeps_the_legacy_directive_contract():
    state = {
        "meta": {"turn": 3},
        "entities": {"kael": {"name": "Kael"}},
        "player": {"kael": {"defeated": {"turn": 3, "outcome": "captured"}}},
        "rolls": [],
    }

    directive = _render_directive(state)
    assert directive.startswith("[DIRECTIVE] NARRATE:")
    assert "Narrate exactly this outcome" in directive
    assert "always resolves the Player's NEWEST message" in directive
    assert "NARRATE CODE-SETTLED FALLOUT" not in directive


@pytest.mark.parametrize(
    ("kind", "delivery_mode"),
    [("lost_reply", "lost_reply_retry"), ("swipe_replay", "regeneration_retry")],
)
def test_retry_changes_delivery_only_not_the_settled_skill_event(kind: str, delivery_mode: str):
    _cfg, state = _targetless_state()
    first = build_narrator_realization_from_state(state)
    retry_state = deepcopy(state)
    retry_state["_settled_retry"] = {"kind": kind}
    retry = build_narrator_realization_from_state(retry_state)

    assert first is not None and retry is not None
    assert first["delivery_mode"] == "first_delivery"
    assert retry["delivery_mode"] == delivery_mode
    assert retry["asserted_settled"] == first["asserted_settled"]
    assert retry["asserted_unresolved"] == first["asserted_unresolved"]


def test_corrupt_skill_receipt_fails_closed_without_legacy_numeric_reactivation():
    cfg, state = _targetless_state()
    roll = deepcopy(state["rolls"][-1])
    state["_fresh_rolls"] = [deepcopy(roll)]
    state["_fresh_checks"] = [deepcopy(roll)]
    state["mechanic_settlements"][0]["receipt"]["applied_changes"][0]["post"] += 1

    assert build_narrator_realization_from_state(state) is None
    output, _kept = compose(
        {"messages": [{"role": "user", "content": "I sneak past the watch."}]},
        state,
        cfg,
        None,
        "new_turn",
    )
    text = "\n".join(str(message.get("content") or "") for message in output["messages"])
    assert "NARRATOR REALIZATION UNAVAILABLE" in text
    assert "2d6 =" not in text
    assert "the stealth check resolved as" not in text
