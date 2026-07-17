"""Polarity stays with the exact event governed by an explicit check."""
from __future__ import annotations

import random

import pytest

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.state import empty_state


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.foe_floor = True
    cfg.specialization.intent_floor = True
    return cfg


def _state() -> dict:
    state = empty_state()
    state["entities"]["mage"] = {
        "kind": "player", "name": "Mage", "present": True, "aliases": [],
    }
    state["entities"]["iven"] = {
        "kind": "npc", "name": "Iven", "present": True, "aliases": [],
    }
    state["player"] = {"mage": {
        "eid": "mage",
        "stats": {"INT": 16},
        "skills": {"elementalism": 4},
        "abilities": ["ice_focus"],
        "resources": {"mana": {"name": "Mana", "max": 30, "cur": 30}},
        "_resource_cost_policy": "strict/1",
        "defs": {
            "skills": {
                "elementalism": {
                    "name": "Elementalism",
                    "keyed_stat": "INT",
                    "governs": ["rain"],
                    "cost": {"mana": 1},
                },
            },
            "abilities": {
                "ice_focus": {
                    "name": "Ice Focus",
                    "kind": "active",
                    "mechanic": "surge",
                    "magnitude": 1,
                    "applies_to": "elementalism",
                    "cost": {"mana": 2},
                    "cooldown_turns": 2,
                },
            },
        },
    }}
    return state


def _run(companion: str):
    return tier0.run(
        {"messages": [
            {"role": "assistant", "content": "Iven braces for the Mage's next move."},
            {"role": "user", "content": (
                "((aether.check elementalism use ice_focus)) "
                f"((aether.check elementalism)) {companion}"
            )},
        ]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(7),
        turn=4,
    )


def _frames(result) -> list[dict]:
    return [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
    ]


def _settlements(result) -> list[dict]:
    return [
        op for op in result.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
    ]


def test_unrelated_negated_preface_does_not_negate_the_governed_attack():
    result = _run(
        "I don't waste a moment, I focus and unleash a tornado of pure "
        "vaporizing fire at Iven."
    )
    frames = _frames(result)

    assert [(frame["action_class"], frame["polarity"]) for frame in frames] == [
        ("skill_check", "positive"),
        ("weapon_attack", "positive"),
    ]
    assert frames[1]["target_entity_id"] == "iven"
    assert len(_settlements(result)) == 2
    assert len([op for op in result.rule_ops if op.get("op") == "combatant_hp"]) == 1


@pytest.mark.parametrize(
    ("companion", "expected_polarity", "expected_settlements"),
    [
        (
            "I focus and unleash a tornado of pure vaporizing fire at Iven.",
            "positive",
            2,
        ),
        (
            "I don't unleash a tornado of pure vaporizing fire at Iven.",
            "negative",
            1,
        ),
    ],
    ids=("direct-positive", "direct-negative"),
)
def test_direct_event_polarity_controls_remain_exact(
    companion: str,
    expected_polarity: str,
    expected_settlements: int,
):
    result = _run(companion)
    frames = _frames(result)

    assert frames[1]["polarity"] == expected_polarity
    assert len(_settlements(result)) == expected_settlements
