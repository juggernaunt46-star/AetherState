"""Creator prose that passes authoring validation must survive committed reconstruction.

Creative-direction notes intentionally stop at the editable draft.  These tests exercise only
the authored world and Player fields that own durable state.
"""
from __future__ import annotations

import json

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _rpg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.clock_turns = 0
    return cfg


def _prose(subject: str, minimum: int) -> str:
    sentences: list[str] = []
    index = 1
    while len(" ".join(sentences)) <= minimum:
        sentences.append(
            f"{subject} detail {index} preserves its exact authored wording through reconstruction."
        )
        index += 1
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


def test_long_composite_faction_and_location_descriptions_survive_store_roundtrip(
    tmp_path,
) -> None:
    faction_description = _prose("Faction", 700)
    location_description = _prose("Location", 750)
    direction = "Write with restrained dread and never address the Player as a reader."
    assert 400 < len(faction_description) < 2000
    assert 400 < len(location_description) < 2000
    world = {
        "name": "The Glass Archive",
        "genre": "custom",
        "setting": "A city preserves civic history in saltglass vaults.",
        "factions": [f"Archive Union - {faction_description}"],
        "locations": [f"Witness Rotunda - {location_description}"],
        "notes": direction,
    }

    normalized = creator.deterministic_world(world)
    assert normalized["factions"] == world["factions"]
    assert normalized["locations"] == world["locations"]
    assert normalized["notes"] == direction
    ops = creator.world_to_ops(normalized)
    assert direction not in json.dumps(ops, ensure_ascii=False)

    state = _committed_state(tmp_path, "long-composite-world", ops, _rpg())
    rebuilt = creator.world_from_state(state)
    assert "notes" not in rebuilt
    assert rebuilt["factions"] == [
        f"Archive Union \N{EM DASH} {faction_description}"
    ]
    assert rebuilt["locations"] == [
        f"Witness Rotunda \N{EM DASH} {location_description}"
    ]


def test_long_front_consequence_survives_store_roundtrip(tmp_path) -> None:
    consequence = _prose("Front consequence", 700)
    assert 300 < len(consequence) < 4000
    world = {
        "name": "The Closing Harbor",
        "genre": "custom",
        "fronts": [{
            "name": "The Harbor Chain Rises",
            "segments": 4,
            "pace": 1,
            "consequence": consequence,
        }],
    }
    ops = creator.world_to_ops(world)
    front_op = next(op for op in ops if op.get("op") == "front_add")
    assert front_op["consequence"] == consequence

    state = _committed_state(tmp_path, "long-front-consequence", ops, _rpg())
    assert state["fronts"]["the_harbor_chain_rises"]["consequence"] == consequence
    rebuilt = creator.world_from_state(state)
    assert rebuilt["fronts"][0]["consequence"] == consequence


def test_long_player_creator_extra_survives_store_roundtrip_but_direction_stays_draft_only(
    tmp_path,
) -> None:
    extra = _prose("Player chronicle", 2600)
    direction = "Use close third-person narration, concrete senses, and complete every scene beat."
    assert 2000 < len(extra) < 8000
    player = {
        "name": "Iria Vale",
        "concept": "Civic witness",
        "extras": [{"label": "Witness Chronicle", "text": extra}],
        "notes": direction,
    }

    normalized = creator.deterministic_player(player, _rpg())
    assert normalized["extras"] == player["extras"]
    assert normalized["notes"] == direction
    ops = creator.player_to_ops(normalized, _rpg())
    assert direction not in json.dumps(ops, ensure_ascii=False)

    state = _committed_state(tmp_path, "long-player-extra", ops, _rpg())
    player_id = next(iter(state["player"]))
    assert state["player"][player_id]["creator_extras"] == player["extras"]
    rebuilt = creator.player_from_state(state)
    assert "notes" not in rebuilt
    assert rebuilt["extras"] == player["extras"]


def test_long_npc_and_custom_capability_prose_survives_store_roundtrip(tmp_path) -> None:
    npc_description = _prose("NPC", 900)
    skill_description = _prose("Skill", 950)
    ability_description = _prose("Ability description", 1000)
    ability_effect = _prose("Ability effect", 1050)
    for value in (
        npc_description,
        skill_description,
        ability_description,
        ability_effect,
    ):
        assert 400 < len(value) < 4000

    cfg = _rpg()
    world_ops = creator.world_to_ops({
        "name": "The Witness City",
        "genre": "custom",
        "npcs": [{
            "name": "Archivist Sera",
            "role": "chief witness",
            "desc": npc_description,
        }],
    })
    player_ops = creator.player_to_ops({
        "name": "Iria Vale",
        "concept": "Archive investigator",
        "skills": {"forensic_cartography": 2},
        "abilities": ["archive_resonance"],
        "custom": {
            "skills": [{
                "id": "forensic_cartography",
                "name": "Forensic Cartography",
                "keyed_stat": "INT",
                "desc": skill_description,
            }],
            "abilities": [{
                "id": "archive_resonance",
                "name": "Archive Resonance",
                "kind": "active",
                "mechanic": "reroll",
                "applies_to": "forensic_cartography",
                "desc": ability_description,
                "effect": ability_effect,
            }],
        },
    }, cfg)

    state = _committed_state(
        tmp_path,
        "long-owned-creator-prose",
        [*world_ops, *player_ops],
        cfg,
    )
    rebuilt_world = creator.world_from_state(state)
    npc = next(row for row in rebuilt_world["npcs"] if row["name"] == "Archivist Sera")
    assert npc["desc"] == npc_description

    rebuilt_player = creator.player_from_state(state)
    skill = rebuilt_player["defs"]["skills"]["forensic_cartography"]
    ability = rebuilt_player["defs"]["abilities"]["archive_resonance"]
    assert skill["desc"] == skill_description
    assert ability["desc"] == ability_description
    assert ability["effect"] == ability_effect
