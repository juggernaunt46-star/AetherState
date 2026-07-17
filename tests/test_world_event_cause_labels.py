"""Player-safe World Event causes use names without weakening cause privacy."""

from __future__ import annotations

import pytest

from aetherstate.config import Config
from aetherstate.hud import hud_view
from aetherstate.knowledge import select_knowledge
from aetherstate.world_events import build_world_event_record


WORLD_ID = "world_" + "c" * 32
FRONT_ID = "the_iron_pact_publicly_seals_the_east_gate"
FRONT_NAME = "The Iron Pact Publicly Seals the East Gate"
CAUSE_ID = f"front:{FRONT_ID}:completion"
CONSEQUENCE = "The East Gate is sealed by public decree."


def _event(cause_visibility: str, *, cause_id: str = CAUSE_ID) -> dict:
    return build_world_event_record(
        event_id=f"event.cause-label.{cause_visibility}.{cause_id.count(':')}",
        world_id=WORLD_ID,
        session_id="session-cause-label",
        branch_id="branch-cause-label",
        turn=3,
        game_time=3,
        cause_id=cause_id,
        cause_authority="rule",
        cause_visibility=cause_visibility,
        affected_domains=["world"],
        description=CONSEQUENCE,
        effects=[{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": "world",
            "field": "circumstance",
            "value": CONSEQUENCE,
            "supported": True,
            "lore": "",
        }],
    )


def _state(event: dict, *, revealed: bool = True) -> dict:
    return {
        "meta": {"turn": 3},
        "clock": {"minutes": 3},
        "world_identity": {"world_id": WORLD_ID},
        "player": {"player": {"name": "Player"}},
        "fronts": {
            FRONT_ID: {
                "name": FRONT_NAME,
                "revealed": revealed,
                "done": True,
                "filled": 3,
                "segments": 3,
                "consequence": CONSEQUENCE,
            },
        },
        "world_events": [event],
    }


@pytest.mark.parametrize("cause_visibility", ["public", "player"])
def test_revealed_front_cause_uses_display_name_in_player_knowledge_and_hud(
    cause_visibility: str,
) -> None:
    event = _event(cause_visibility)
    state = _state(event)

    row = select_knowledge(
        state, audience="player", actor_id="player", limit=8,
    )["events"][0]
    cfg = Config()
    cfg.specialization.name = "rpg"
    view = hud_view(state, cfg)

    assert row["cause_visible"] is True
    assert row["cause"] == FRONT_NAME
    assert view["knowledge"]["events"][0]["cause"] == FRONT_NAME
    assert view["player_safe_raw"]["knowledge"]["events"][0]["cause"] == FRONT_NAME
    assert event["cause_id"] == CAUSE_ID


@pytest.mark.parametrize(
    ("cause_visibility", "revealed"),
    [("hidden", True), ("public", False)],
)
def test_hidden_or_unrevealed_front_cause_fails_closed_to_exact_player_wording(
    cause_visibility: str,
    revealed: bool,
) -> None:
    event = _event(cause_visibility)
    state = _state(event, revealed=revealed)

    row = select_knowledge(
        state, audience="player", actor_id="player", limit=8,
    )["events"][0]
    cfg = Config()
    cfg.specialization.name = "rpg"
    view = hud_view(state, cfg)

    assert row["cause_visible"] is False
    assert row["cause"] is None
    assert view["knowledge"]["events"][0]["cause"] == "cause not known"
    assert (
        view["player_safe_raw"]["knowledge"]["events"][0]["cause"]
        == "cause not known"
    )
    assert CAUSE_ID not in str(view["knowledge"])
    assert CAUSE_ID not in str(view["player_safe_raw"])
    assert event["cause_id"] == CAUSE_ID


def test_non_front_public_cause_keeps_its_existing_projection() -> None:
    cause_id = "rule:harbor-weather"
    event = _event("public", cause_id=cause_id)
    state = _state(event)

    row = select_knowledge(
        state, audience="player", actor_id="player", limit=8,
    )["events"][0]

    assert row["cause_visible"] is True
    assert row["cause"] == cause_id
    assert event["cause_id"] == cause_id


@pytest.mark.parametrize(
    "fronts",
    [[{"malformed": True}], {FRONT_ID: {"revealed": True, "done": True, "name": ""}}],
)
def test_malformed_or_unlabelled_front_state_cannot_expose_the_raw_cause(
    fronts: object,
) -> None:
    event = _event("public")
    state = _state(event)
    state["fronts"] = fronts

    row = select_knowledge(
        state, audience="player", actor_id="player", limit=8,
    )["events"][0]

    assert row["cause_visible"] is False
    assert row["cause"] is None
    assert event["cause_id"] == CAUSE_ID
