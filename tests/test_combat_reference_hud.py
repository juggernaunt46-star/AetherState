"""Focused HUD truth for recent Player checks and their exact combat impact."""

from __future__ import annotations

import pytest

from aetherstate.config import Config
from aetherstate.hud import hud_view


def _rpg_cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _combat_state() -> dict:
    return {
        "meta": {"turn": 7},
        "entities": {"kael": {"name": "Kael"}},
        "player": {"kael": {}},
        "combat": {
            "active": True,
            "started_turn": 5,
            "combatants": {
                "hollowed": {
                    "eid": "hollowed",
                    "name": "Hollowed",
                    "side": "enemy",
                    "kind": "extra",
                    "tier": "standard",
                    "hp": {"cur": 6, "max": 6},
                    "init": 10,
                    "cohort": {"ref": "hollowed-wave", "index": 1, "total": 4},
                },
                "hollowed#2": {
                    "eid": "hollowed#2",
                    "name": "Hollowed",
                    "side": "enemy",
                    "kind": "extra",
                    "tier": "standard",
                    "hp": {"cur": 0, "max": 6},
                    "init": 9,
                    "defeated": True,
                    "cohort": {"ref": "hollowed-wave", "index": 2, "total": 4},
                },
            },
        },
        "rolls": [
            {
                "turn": 6,
                "skill": "stealth",
                "result": 7,
                "tier": "partial",
                "target": None,
                "dmg": None,
            },
            {
                "turn": 7,
                "skill": "elementalism",
                "result": 11,
                "tier": "success",
                "target": None,
                "dmg": None,
            },
            {
                "turn": 7,
                "skill": "elementalism",
                "result": 10,
                "tier": "success",
                "target": None,
                "dmg": 99,
            },
            {
                "turn": 7,
                "skill": "melee",
                "result": 5,
                "tier": "fail",
                "target": "Hollowed #1",
                "dmg": 0,
            },
            {
                "turn": 7,
                "skill": "melee",
                "result": 12,
                "tier": "success",
                "target": "Hollowed #2",
                "dmg": 6,
            },
        ],
    }


def test_recent_rolls_keep_order_duplicates_and_exact_target_impact_truth() -> None:
    view = hud_view(_combat_state(), _rpg_cfg())

    assert [row["skill"] for row in view["rolls"]] == [
        "stealth", "elementalism", "elementalism", "melee", "melee",
    ]
    assert [row["label"] for row in view["rolls"]] == [
        "Stealth", "Elementalism", "Elementalism", "Melee", "Melee",
    ]
    assert [row["impact"]["kind"] for row in view["rolls"]] == [
        "none", "none", "none", "miss", "damage",
    ]
    assert [row["turn"] for row in view["rolls"]] == [6, 7, 7, 7, 7]
    assert view["rolls"][1]["impact"] == {
        "kind": "none",
        "target_id": None,
        "target_label": None,
        "damage": None,
        "text": "No target impact",
    }
    # A malformed positive damage value without a target cannot turn generic success into harm.
    assert view["rolls"][2]["impact"]["kind"] == "none"
    assert view["rolls"][3]["impact"] == {
        "kind": "miss",
        "target_id": "hollowed",
        "target_label": "Hollowed #1",
        "damage": 0,
        "text": "Hollowed #1: no damage",
    }
    # Defeat does not erase which exact cohort row received the committed damage.
    assert view["rolls"][4]["impact"] == {
        "kind": "damage",
        "target_id": "hollowed#2",
        "target_label": "Hollowed #2",
        "damage": 6,
        "text": "Hollowed #2: 6 damage",
    }

    impacts = view["war_room"]["player_impacts"]
    assert [row["impact"] for row in impacts] == [row["impact"] for row in view["rolls"][1:]]
    assert [row["result"] for row in impacts] == [11, 10, 5, 12]


def test_malformed_roll_impact_fails_safe_without_erasing_other_history() -> None:
    state = _combat_state()
    state["rolls"] = [
        None,
        {"turn": 7, "skill": "lore", "result": 8, "tier": "partial",
         "target": "Hollowed #1", "dmg": "three"},
        {"turn": 7, "spec": "1d20", "result": 14},
    ]

    view = hud_view(state, _rpg_cfg())

    assert len(view["rolls"]) == 2
    assert view["rolls"][0]["impact"] == {
        "kind": "unknown",
        "target_id": "hollowed",
        "target_label": "Hollowed #1",
        "damage": None,
        "text": "Hollowed #1: impact unavailable",
    }
    assert view["rolls"][1]["label"] == "1d20"
    assert view["rolls"][1]["impact"]["kind"] == "none"
    # Only actual checks belong in the War Room's Player-impact strip.
    assert [row["skill"] for row in view["war_room"]["player_impacts"]] == ["lore"]

    malformed = hud_view({"meta": {"turn": 7}, "rolls": {"not": "a history"},
                          "combat": ["not", "a", "combat ledger"]}, _rpg_cfg())
    assert malformed["rolls"] == []


def test_supplied_unresolved_ambiguous_and_malformed_targets_are_unknown() -> None:
    state = _combat_state()
    state["rolls"] = [
        {"turn": 7, "skill": "melee", "result": 1, "tier": "fail",
         "target": "Unknown Raider", "dmg": 4},
        {"turn": 7, "skill": "melee", "result": 2, "tier": "partial",
         "target": "Hollowed", "dmg": 4},
        {"turn": 7, "skill": "melee", "result": 3, "tier": "partial",
         "target": "Hollowed", "dmg": 0},
        {"turn": 7, "skill": "melee", "result": 4, "tier": "success",
         "target": {"label": "Unknown Raider"}, "dmg": 4},
        {"turn": 7, "skill": "melee", "result": 5, "tier": "success",
         "target": ["Hollowed #1"], "dmg": 0},
        {"turn": 7, "skill": "melee", "result": 6, "tier": "success",
         "target": 17, "dmg": 4},
        {"turn": 7, "skill": "melee", "result": 7, "tier": "success",
         "target": True, "dmg": 0},
    ]

    view = hud_view(state, _rpg_cfg())
    impacts = [row["impact"] for row in view["rolls"]]

    assert [impact["kind"] for impact in impacts] == ["unknown"] * 7
    assert [impact["target_id"] for impact in impacts] == [None] * 7
    assert [impact["damage"] for impact in impacts] == [None] * 7
    assert [impact["target_label"] for impact in impacts] == [
        "Unknown Raider", "Hollowed", "Hollowed", "Unknown Raider", None, None, None,
    ]
    assert [row["result"] for row in view["rolls"]] == list(range(1, 8))

    war_impacts = view["war_room"]["player_impacts"]
    assert [row["result"] for row in war_impacts] == list(range(1, 8))
    assert [row["impact"] for row in war_impacts] == impacts


def test_only_absent_null_or_blank_targets_have_no_target_impact() -> None:
    state = _combat_state()
    state["rolls"] = [
        {"turn": 7, "skill": "melee", "result": 1, "dmg": 99},
        {"turn": 7, "skill": "melee", "result": 2, "target": None, "dmg": 99},
        {"turn": 7, "skill": "melee", "result": 3, "target": "", "dmg": 99},
        {"turn": 7, "skill": "melee", "result": 4, "target": "   ", "dmg": 99},
    ]

    view = hud_view(state, _rpg_cfg())

    assert [row["impact"] for row in view["rolls"]] == [
        {
            "kind": "none",
            "target_id": None,
            "target_label": None,
            "damage": None,
            "text": "No target impact",
        }
    ] * 4
    assert [row["impact"] for row in view["war_room"]["player_impacts"]] == [
        row["impact"] for row in view["rolls"]
    ]


@pytest.mark.parametrize("malformed_target", [{}, [], False, 0])
def test_falsey_malformed_supplied_target_is_unknown(malformed_target: object) -> None:
    state = _combat_state()
    state["rolls"] = [
        {"turn": 7, "skill": "melee", "result": 12,
         "target": malformed_target, "dmg": 4},
    ]

    view = hud_view(state, _rpg_cfg())

    assert view["rolls"][0]["impact"] == {
        "kind": "unknown",
        "target_id": None,
        "target_label": None,
        "damage": None,
        "text": "Impact unavailable",
    }
    assert view["war_room"]["player_impacts"][0]["impact"] == view["rolls"][0]["impact"]


@pytest.mark.parametrize(
    "malformed_state",
    [
        ["truthy", "non-mapping", "state"],
        {"meta": ["truthy", "non-mapping", "meta"]},
        {"player": ["truthy", "non-mapping", "player"]},
    ],
    ids=("top-level-state", "meta-container", "player-container"),
)
def test_hud_view_truthy_malformed_containers_fail_open_without_mechanics(
    malformed_state: object,
) -> None:
    view = hud_view(malformed_state, _rpg_cfg())

    assert view["turn"] == -1
    assert view["frozen"] is False
    assert view["players"] == []
    assert view["rolls"] == []
    assert view["war_room"]["active"] is False
    assert view["war_room"].get("combatants", []) == []
    assert view["war_room"].get("player_impacts", []) == []
    assert view["war_room"].get("intent") is None
    assert view["war_room"].get("opposition") is None
