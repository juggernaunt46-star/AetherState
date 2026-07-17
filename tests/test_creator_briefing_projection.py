"""Committed Creator prose reaches the narrator and Player HUD at the correct scope."""
from __future__ import annotations

from aetherstate import creator, hud
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.clock_turns = 0
    cfg.specialization.war_room = False
    cfg.specialization.large_battle = False
    return cfg


def _complete_prose(subject: str, minimum: int, ending: str) -> str:
    sentences: list[str] = []
    index = 1
    while len(" ".join(sentences)) <= minimum:
        sentences.append(
            f"{subject} sentence {index} preserves concrete authored detail for the narrator."
        )
        index += 1
    sentences.append(ending)
    return " ".join(sentences)


def _committed_state(tmp_path, tag: str, ops: list[dict], cfg: Config) -> dict:
    path = tmp_path / f"{tag}.sqlite3"
    store = Store(path)
    session_id, branch_id = store.create_session(external_id=tag)
    result = apply_delta(store, session_id, branch_id, 0, ops, "user", cfg)
    assert result.applied
    assert result.quarantined == []
    store.close()

    reopened = Store(path)
    try:
        return current_state(reopened, branch_id)
    finally:
        reopened.close()


def test_full_player_creator_prose_reaches_narrator_briefing_and_hud(tmp_path) -> None:
    appearance = _complete_prose(
        "Appearance",
        450,
        "The final appearance detail is an unbroken silver scar beneath Iria's left eye.",
    )
    skill_description = _complete_prose(
        "Skill",
        500,
        "The final skill detail distinguishes direct evidence from copied testimony.",
    )
    ability_effect = _complete_prose(
        "Ability effect",
        520,
        "The final ability effect permits one careful reroll after comparing both records.",
    )
    ability_description = _complete_prose(
        "Ability description",
        540,
        "The final ability description identifies the civic seal that grants this technique.",
    )
    gear_aura = _complete_prose(
        "Gear aura",
        260,
        "The final etched line glows blue only when preserved wording has been changed.",
    )
    world_direction = "PRIVATE WORLD DIRECTION MUST NEVER ENTER THE BRIEFING."
    player_direction = "PRIVATE PLAYER DIRECTION MUST NEVER ENTER THE BRIEFING."
    assert len(gear_aura) > 140

    cfg = _rpg()
    world_ops = creator.world_to_ops({
        "name": "Claimfall Harbor",
        "genre": "custom",
        "notes": world_direction,
    })
    player_ops = creator.player_to_ops({
        "name": "Iria Vale",
        "concept": "Civic witness",
        "appearance": appearance,
        "skills": {"witness_reading": 2},
        "abilities": ["seal_attunement"],
        "gear": [{
            "name": "Longform Witness Seal",
            "slot": "accessory1",
            "effect": gear_aura,
        }],
        "custom": {
            "skills": [{
                "id": "witness_reading",
                "name": "Witness Reading",
                "keyed_stat": "CUN",
                "governs": ["compare", "qualify", "source"],
                "desc": skill_description,
            }],
            "abilities": [{
                "id": "seal_attunement",
                "name": "Seal Attunement",
                "kind": "active",
                "mechanic": "reroll",
                "applies_to": "witness_reading",
                "effect": ability_effect,
                "desc": ability_description,
            }],
        },
        "notes": player_direction,
    }, cfg)
    state = _committed_state(
        tmp_path,
        "creator-player-briefing",
        [*world_ops, *player_ops],
        cfg,
    )

    briefing = render_header(state, cfg)
    for exact in (
        appearance,
        skill_description,
        ability_effect,
        ability_description,
        gear_aura,
        "governs: compare, qualify, source",
    ):
        assert exact in briefing
    assert gear_aura.endswith(
        "The final etched line glows blue only when preserved wording has been changed."
    )
    assert world_direction not in briefing
    assert player_direction not in briefing

    view = hud.hud_view(state, cfg)
    player = view["players"][0]
    assert player["appearance"] == appearance
    ability = next(row for row in player["abilities"] if row["id"] == "seal_attunement")
    assert ability["effect"] == ability_effect
    assert ability["desc"] == ability_description
    gear = next(row for row in player["gear"] if row["name"] == "Longform Witness Seal")
    assert gear["aura"] == gear_aura


def test_briefing_projects_only_current_present_and_nearby_scene_descriptions(tmp_path) -> None:
    current_location_description = (
        "Saltglass windows turn every spoken oath into a pale reflection across the floor."
    )
    unrelated_location_description = (
        "UNRELATED LOCATION DESCRIPTION must not spend narrator briefing tokens."
    )
    present_description = (
        "Mara records each speaker's exact words before she writes any interpretation."
    )
    nearby_description = (
        "Sera keeps the local witness index and can be found here without being staged on scene."
    )
    off_scene_description = (
        "UNRELATED OFF-SCENE NPC DESCRIPTION must not dump into the narrator briefing."
    )
    direction = "PRIVATE SCENE DIRECTION MUST REMAIN ON THE EDITABLE CREATOR DRAFT."
    cfg = _rpg()
    world_ops = creator.world_to_ops({
        "name": "Scoped City",
        "genre": "custom",
        "locations": [
            f"Witness Rotunda - {current_location_description}",
            f"North Annex - {unrelated_location_description}",
        ],
        "npcs": [
            {
                "name": "Mara Pell",
                "role": "present witness",
                "desc": present_description,
                "home": "Witness Rotunda",
            },
            {
                "name": "Archivist Sera",
                "role": "nearby archivist",
                "desc": nearby_description,
                "home": "Witness Rotunda",
            },
            {
                "name": "Vosk Arden",
                "role": "annex envoy",
                "desc": off_scene_description,
                "home": "North Annex",
            },
        ],
        "opening_scene": "At Witness Rotunda, Mara Pell waits beside the civic ledger.",
        "notes": direction,
    })

    state = _committed_state(tmp_path, "creator-scene-briefing", world_ops, cfg)
    briefing = render_header(state, cfg)
    assert current_location_description in briefing
    assert present_description in briefing
    assert nearby_description in briefing
    assert unrelated_location_description not in briefing
    assert off_scene_description not in briefing
    assert direction not in briefing
    assert "[SCENE LORE]" in briefing
    assert "[NEARBY]" in briefing
