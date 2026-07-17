"""Focused regressions captured from the 2026-07-17 disposable Creator playtest."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aetherstate import creator, narrator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _complete_character(*, gear: list) -> dict:
    """Return a structurally complete character proposal with caller-selected gear."""
    return {
        "name": "Kael Vey",
        "sex": "male",
        "pronouns": "he/him",
        "species": "human",
        "appearance": (
            "A patient mediator in a charcoal coat, with steady grey eyes and ink-stained "
            "fingers."
        ),
        "concept": "Harbor witness and mediator",
        "stats": {"STR": 10, "DEX": 10, "INT": 12, "CHA": 13, "CUN": 11, "CON": 10},
        "skills": {"persuasion": 3, "perception": 2},
        "abilities": ["keen_senses"],
        "gear": gear,
        "defs": {"skills": [], "abilities": []},
    }


async def test_character_direction_requires_structured_named_gear_and_retries(monkeypatch):
    effect = "Reveals forged testimony with a cold blue glimmer."
    direction = (
        "Starting gear must include an object named Brass Witness-Seal, pinned to accessory1, "
        f"whose effect is {effect}"
    )
    first = _complete_character(
        gear=["Brass Witness-Seal", "weatherproof case"],
    )
    repaired = _complete_character(
        gear=[
            {
                "name": "Brass Witness-Seal",
                "slot": "accessory1",
                "effect": effect,
            },
            "weatherproof case",
        ],
    )
    replies = [first, repaired]
    calls: list[dict] = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(replies.pop(0)), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": direction},
    )

    assert out["source"] == "llm"
    assert len(calls) == 2
    assert calls[0]["user"] == calls[1]["user"]
    assert all(call["user"].count(direction) == 1 for call in calls)
    assert "previous response was rejected" in calls[1]["system"].lower()
    witness = next(
        row
        for row in out["doc"]["gear"]
        if isinstance(row, dict) and row.get("name") == "Brass Witness-Seal"
    )
    assert witness == {
        "name": "Brass Witness-Seal",
        "slot": "accessory1",
        "effect": effect,
    }


_WITNESS_EFFECT = (
    "Stores any testimony he records and reveals any later alteration as a cold blue fracture."
)
_ALL_GEAR_DIRECTION = (
    "Every starting gear row must be an object with a finished name and finished effect. "
    "Starting gear must include an object named Brass Witness-Seal, pinned to accessory1, "
    f"whose effect is: {_WITNESS_EFFECT}"
)
_COMPLETE_EFFECT_GEAR = [
    {
        "name": "Brass Witness-Seal",
        "slot": "accessory1",
        "effect": _WITNESS_EFFECT,
    },
    {
        "name": "weatherproof mediator coat",
        "slot": "body",
        "effect": "Sheds harbor rain without muffling his voice.",
    },
    {
        "name": "sealed ink case",
        "slot": "back",
        "effect": "Keeps witness notes dry and preserves their original order.",
    },
]


@pytest.mark.parametrize(
    "first_gear",
    [
        [
            {
                "name": "Brass Witness-Seal",
                "slot": "accessory1",
                "effect": "Stores any testimony he records",
            },
            "weatherproof mediator coat",
            {"name": "sealed ink case", "slot": "back", "effect": ""},
        ],
        [
            {
                "name": "Brass Witness-Seal",
                "slot": "accessory1",
                "effect": "Stores any testimony he records",
            },
            _COMPLETE_EFFECT_GEAR[1],
            _COMPLETE_EFFECT_GEAR[2],
        ],
        [
            _COMPLETE_EFFECT_GEAR[0],
            "weatherproof mediator coat",
            {"name": "sealed ink case", "slot": "back", "effect": ""},
        ],
    ],
    ids=("live-composite", "cut-off-witness-effect-only", "unfinished-other-gear-only"),
)
async def test_character_direction_rejects_unfinished_or_semantically_cut_off_gear(
        monkeypatch, first_gear):
    replies = [
        _complete_character(gear=first_gear),
        _complete_character(gear=_COMPLETE_EFFECT_GEAR),
    ]
    calls: list[dict] = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(replies.pop(0)), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": _ALL_GEAR_DIRECTION},
    )

    assert out["source"] == "llm"
    assert len(calls) == 2
    assert calls[0]["user"] == calls[1]["user"]
    assert all(call["user"].count(_ALL_GEAR_DIRECTION) == 1 for call in calls)
    assert "previous response was rejected" in calls[1]["system"].lower()
    assert all(
        isinstance(row, dict) and row.get("name") and row.get("effect")
        for row in out["doc"]["gear"]
    )
    witness = next(
        row for row in out["doc"]["gear"] if row.get("name") == "Brass Witness-Seal"
    )
    assert witness == {
        "name": "Brass Witness-Seal",
        "slot": "accessory1",
        "effect": _WITNESS_EFFECT,
    }


def _long_direction(label: str) -> str:
    return (
        f"{label} direction begins. "
        + (
            "Maintain deliberate pacing, literal continuity, complete sentences, and every "
            "explicitly requested distinction. "
        ) * 48
        + f"{label} direction ends at this final sentinel."
    )


def _long_world_prose(label: str) -> str:
    head = f"{label} BEGIN. "
    sentence = (
        "This authored passage preserves named actors, exact evidence, physical continuity, "
        "and the difference between a report and admitted truth. "
    )
    tail = f"{label} END AT THE FINAL SENTINEL."
    paragraphs: list[str] = []
    while len(head + "".join(paragraphs) + sentence + tail) <= 7900:
        paragraphs.append(sentence)
    text = head + "".join(paragraphs) + tail
    assert 7700 < len(text) <= 8000
    return text


def _long_named_row(name: str, label: str) -> str:
    text = (
        f"{name} — "
        + (
            f"{label} keeps its own agenda, material details, disputed history, and visible "
            "relationship to the harbor. "
        ) * 8
        + f"{label} ROW END."
    )
    assert 520 < len(text) < 2000
    return text


def test_narrator_card_seed_keeps_long_world_truth_but_excludes_direction():
    setting = _long_world_prose("SETTING")
    opening_scene = _long_world_prose("OPENING-SCENE")
    opening_quest = _long_world_prose("OPENING-QUEST")
    direction = _long_direction("PRIVATE-CARD-DIRECTION")
    world = creator.deterministic_world({
        "name": "Longform Claimfall",
        "genre": "dark_fantasy",
        "setting": setting,
        "opening_scene": opening_scene,
        "opening_quest": opening_quest,
        "notes": direction,
    })
    assert world["notes"] == direction

    direct_seed = narrator.seed_payload(world, None)["world"]
    card = narrator.build_card(world)
    card_seed = card["data"]["extensions"]["aetherstate"]["seed"]["world"]
    for seed in (direct_seed, card_seed):
        assert seed["setting"] == setting
        assert seed["opening_scene"] == opening_scene
        assert seed["opening_quest"] == opening_quest
        assert "notes" not in seed
        assert direction not in json.dumps(seed, ensure_ascii=False)
    assert direction not in json.dumps(card["data"], ensure_ascii=False)


def test_world_user_carries_complete_prefilled_long_fields():
    setting = _long_world_prose("PROMPT-SETTING")
    opening_scene = _long_world_prose("PROMPT-OPENING-SCENE")
    opening_quest = _long_world_prose("PROMPT-OPENING-QUEST")
    faction = _long_named_row("Lantern Guild", "FACTION")
    location = _long_named_row("Lantern Refuge", "LOCATION")
    aspect = _long_named_row("Testimony Covenant", "ASPECT")
    user = creator._world_user({
        "setting": setting,
        "opening_scene": opening_scene,
        "opening_quest": opening_quest,
        "factions": [faction],
        "locations": [location],
        "aspects": [aspect],
    })

    for complete_value in (
            setting, opening_scene, opening_quest, faction, location, aspect):
        assert user.count(complete_value) == 1


def test_character_user_carries_complete_prefilled_card_shapes():
    appearance = _long_named_row("Kael Vey", "APPEARANCE")
    slots = (
        "head", "face", "neck", "shoulders", "body", "cape", "arms", "hands",
        "mainhand", "offhand", "waist", "legs", "feet", "back", "accessory1", "accessory2",
    )
    gear = [
        {
            "name": f"Witness Kit {index + 1:02d}",
            "slot": slots[index % len(slots)],
            "effect": (
                f"Gear effect {index + 1:02d} preserves its exact authored meaning and ends "
                "with this complete sentence."
            ),
        }
        for index in range(32)
    ]
    custom = {
        "skills": [{
            "id": "harbor_testimony_analysis",
            "name": "Harbor Testimony Analysis",
            "keyed_stat": "CUN",
            "base_mod": 2,
            "max_rank": 7,
            "governs": ["compare", "qualify", "source"],
            "desc": (
                "Separates firsthand testimony, inference, rumor, and admitted fact without "
                "collapsing their authority."
            ),
            "requires_ability": "witness_seal_attunement",
            "group": "Civic Disciplines",
            "cost": {"resolve": 17},
        }],
        "abilities": [{
            "id": "witness_seal_attunement",
            "name": "Witness-Seal Attunement",
            "kind": "active",
            "mechanic": "reroll",
            "applies_to": "harbor_testimony_analysis",
            "magnitude": 2,
            "group": "Civic Disciplines",
            "cost": {"resolve": 23},
            "cooldown_turns": 4,
            "resolution_mod": 3,
            "effect": "Rechecks one disputed record while preserving every original qualifier.",
            "desc": "A complete authored mechanical definition, not merely a display name.",
        }],
    }
    user = creator._char_user({
        "appearance": appearance,
        "gear": gear,
        "custom": custom,
    }, None)

    assert user.count(appearance) == 1
    for row in gear:
        assert user.count(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) == 1
    for rows in custom.values():
        for row in rows:
            assert user.count(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) == 1


async def test_long_world_and_character_directions_survive_normalization_and_main_prompt(
        monkeypatch):
    world_notes = _long_direction("WORLD-5000")
    character_notes = _long_direction("CHARACTER-5000")
    assert 4000 < len(world_notes) <= 32768
    assert 4000 < len(character_notes) <= 32768
    assert creator.deterministic_world({"notes": world_notes})["notes"] == world_notes
    assert creator.deterministic_player({"notes": character_notes})["notes"] == character_notes

    replies = [
        _full_world(),
        _complete_character(gear=_COMPLETE_EFFECT_GEAR),
    ]
    calls: list[dict] = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(replies.pop(0)), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    world_out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": world_notes},
    )
    character_out = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": character_notes},
    )

    assert world_out["source"] == "llm"
    assert character_out["source"] == "llm"
    assert world_out["doc"]["notes"] == world_notes
    assert character_out["doc"]["notes"] == character_notes
    assert len(calls) == 2
    assert calls[0]["user"].count(world_notes) == 1
    assert calls[1]["user"].count(character_notes) == 1


def _full_world() -> dict:
    return {
        "name": "Claimfall Harbor",
        "genre": "dark_fantasy",
        "setting": "A storm-walled harbor survives through testimony, rationing, and old pacts.",
        "date": "Year 312 of the Harbor Compact",
        "time": "night",
        "tone": "patient council drama under gathering dread",
        "factions": [
            "Lantern Guild — shelters witnesses and keeps the refuge lamps burning.",
            "Ash Watch — enforces curfew while concealing its divided command.",
            "Iron Pact — controls the East Gate and bargains with armed pilgrims.",
            "Tidebound Council — records public facts but fears every disputed claim.",
        ],
        "locations": [
            "Lantern Refuge — a fortified hospice above a clean stone cistern.",
            "East Gate — an iron portcullis overlooking the pilgrim road.",
            "Council Rotunda — a circular archive where testimony becomes public record.",
            "Ash Barracks — a smoke-dark watch house built into the seawall.",
            "Salt Market — a covered quay where ration chits change hands.",
        ],
        "npcs": [
            {
                "name": "Warden Selene Ashford",
                "role": "Lantern Guild warden",
                "desc": "She records every promise before deciding whom to trust.",
                "home": "Lantern Refuge",
            },
            {
                "name": "Mara Pell",
                "role": "refuge witness",
                "desc": "She remembers voices precisely but distrusts official summaries.",
                "home": "Council Rotunda",
            },
            {
                "name": "Vosk Arden",
                "role": "Iron Pact envoy",
                "desc": "He answers accusations with exact denials and no volunteered detail.",
                "home": "East Gate",
            },
            {
                "name": "Ryn Vale",
                "role": "harbor runner",
                "desc": "They carry warnings faster than anyone can verify them.",
                "home": "Salt Market",
            },
        ],
        "aspects": [
            "A witnessed statement is not a fact until the Council admits it.",
            "The refuge cistern is protected by an older civic covenant.",
            "Curfew bells divide each night into four legal watches.",
            "Only the Iron Pact may raise or lower the East Gate seal.",
            "Hidden causes remain sealed until a privileged record reveals them.",
        ],
        "opening_scene": (
            "At Lantern Refuge, Warden Selene Ashford waits beside the cistern ledger."
        ),
        "opening_quest": "Separate the East Gate rumors from facts before the final curfew bell.",
        "loot": {
            tier: [
                {"name": f"{tier} harbor ration chit", "chance": 0.75},
                {"name": f"{tier} sealed lamp oil", "chance": 0.35},
            ]
            for tier in ("minion", "standard", "elite", "boss")
        },
        "fronts": [
            {
                "name": "Ash Watch Curfew",
                "faction": "Ash Watch",
                "segments": 4,
                "consequence": "The refuge doors remain barred for one full turn.",
                "event_duration_turns": 1,
                "spawn_eligibility": False,
            },
            {
                "name": "Iron Pact Seals East Gate",
                "faction": "Iron Pact",
                "segments": 4,
                "consequence": "Iron Pact patrols bar armed pilgrims from the harbor.",
                "event_duration_turns": None,
                "spawn_eligibility": False,
            },
        ],
        "routes": [
            {"a": "Lantern Refuge", "b": "East Gate", "segments": 3},
        ],
    }


def test_committed_world_roundtrip_preserves_creator_fields_for_narrator_card():
    world = _full_world()
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="creator-live-roundtrip")
    apply_delta(store, sid, bid, 0, creator.world_to_ops(world), "user", cfg)

    rebuilt = creator.world_from_state(current_state(store, bid))
    card_world = narrator.build_card(rebuilt)["data"]["extensions"]["aetherstate"]["seed"]["world"]
    front = next(
        (row for row in card_world.get("fronts", []) if row.get("name") == "Ash Watch Curfew"),
        {},
    )
    npc = next(
        (row for row in card_world.get("npcs", []) if row.get("name") == "Warden Selene Ashford"),
        {},
    )
    standard_loot = card_world.get("loot", {}).get("standard") or []
    route = next(
        (
            row
            for row in card_world.get("routes", [])
            if {row.get("a"), row.get("b")} == {"Lantern Refuge", "East Gate"}
        ),
        {},
    )
    observed = {
        "tone": card_world.get("tone"),
        "faction": next(
            (row for row in card_world.get("factions", []) if row.startswith("Lantern Guild")),
            "",
        ),
        "location": next(
            (row for row in card_world.get("locations", []) if row.startswith("Lantern Refuge")),
            "",
        ),
        "npc_home": npc.get("home"),
        "standard_loot": [
            {"name": row.get("name"), "chance": row.get("chance")} for row in standard_loot
        ],
        "front_duration": front.get("event_duration_turns"),
        "front_spawn_eligibility": front.get("spawn_eligibility"),
        "route_endpoints": {route.get("a"), route.get("b")},
        "route_segments": route.get("segments"),
    }
    assert observed == {
        "tone": world["tone"],
        "faction": world["factions"][0],
        "location": world["locations"][0],
        "npc_home": "Lantern Refuge",
        "standard_loot": world["loot"]["standard"],
        "front_duration": 1,
        "front_spawn_eligibility": False,
        "route_endpoints": {world["routes"][0]["a"], world["routes"][0]["b"]},
        "route_segments": world["routes"][0]["segments"],
    }


def test_world_validator_rejects_every_post_validation_count_and_length_cut() -> None:
    over = _full_world()
    over["setting"] = "A complete sentence. " * 600
    over["factions"] = over["factions"] * 6
    over["routes"] = over["routes"] * 25
    over["loot"]["standard"] = over["loot"]["standard"] * 7
    over["extras"] = [{
        "label": "Long lore",
        "text": "A complete sentence. " * 500,
    }]

    issues = creator._world_validation_issues(over, {})

    assert any("setting exceeds" in issue for issue in issues)
    assert any("factions exceed" in issue for issue in issues)
    assert any("routes exceed" in issue for issue in issues)
    assert any("loot.standard" in issue for issue in issues)
    assert any("world extra" in issue for issue in issues)


def test_character_validator_rejects_incomplete_custom_mechanics_and_hidden_cuts() -> None:
    doc = _complete_character(gear=_COMPLETE_EFFECT_GEAR)
    doc["appearance"] = "A complete physical sentence. " * 180
    doc["gear"] = _COMPLETE_EFFECT_GEAR * 11
    doc["extras"] = [{"label": "History", "text": "Because the"}]
    doc["defs"] = {"skills": [{
        "id": "witness_analysis",
        "name": "Witness Analysis",
        "keyed_stat": "CUN",
        "base_mod": 1,
        "max_rank": 5,
        "governs": ["compare"],
        "desc": "Separates reports from facts.",
    }], "abilities": [{
        "id": "witness_seal",
        "name": "Witness Seal",
        "kind": "active",
        "mechanic": "reroll",
        "applies_to": "witness_analysis",
        "magnitude": 1,
        "cooldown_turns": 1,
        "effect": "Rechecks one report.",
        "desc": "Because the",
    }]}

    issues = creator._player_validation_issues(doc, creator.registry.load(), seed={})

    assert any("appearance exceeds" in issue for issue in issues)
    assert any("gear exceeds" in issue for issue in issues)
    assert any("character extra" in issue for issue in issues)
    assert any("incomplete mechanics" in issue for issue in issues)
    assert any("incomplete kind" in issue for issue in issues)


async def test_character_seed_keeps_all_32_gear_rows_after_main_completion(monkeypatch) -> None:
    seed_gear = [
        {
            "name": f"Complete witness tool {index + 1:02d}",
            "slot": creator._DIRECTION_GEAR_SLOTS[
                index % len(creator._DIRECTION_GEAR_SLOTS)
            ],
            "effect": f"Tool {index + 1:02d} preserves its complete recorded purpose.",
        }
        for index in range(32)
    ]
    proposal = _complete_character(gear=["weatherproof coat", "dry ledger case"])

    async def fake_chat(*_args, **_kwargs):
        return creator._CreatorReply(json.dumps(proposal), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    result = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"gear": seed_gear},
    )

    assert result["source"] == "llm"
    assert len(result["doc"]["gear"]) == 32
    assert result["doc"]["gear"] == seed_gear


def test_main_creator_schemas_expose_level_extras_and_complete_visible_mechanics() -> None:
    world_system = creator._WORLD_SYSTEM
    character_system = creator._char_system(creator.registry.load())

    assert '"extras":[{"label":str,"text":str}]' in world_system
    assert '"level":int' in character_system
    assert '"extras":[{"label":str,"text":str}]' in character_system
    assert '"governs":[str],"desc":str' in character_system
    assert '"effect":str,"desc":str' in character_system
