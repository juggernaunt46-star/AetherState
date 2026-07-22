"""Creator (doc 09): World Generator + Character Creator.

Deterministic backbone + assist-LLM fill-the-blanks, persisted as SHIPPED ops (entities /
memory-lore / scene / player_seed) — no new op vocabulary, no new storage families. Coverage:
the registry export, deterministic fills (blanks + player-fields-win), freestyle -> FROZEN defs
snapshot, that every generated op is already in the op vocab (invariant 3: nothing new touches
the stream), op persistence via apply_delta(source='user'), replay-purity of the seeded Player
Card, the control routes end-to-end, and a `none`-session no-leak.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aetherstate import creator, narrator
from aetherstate.compose import render_header
from aetherstate.config import AssistEndpointConfig, Config
from aetherstate.state import (_SPEC, apply_delta, current_state, empty_state,
                               reduce_state, validate_op)
from aetherstate.store import Store


def _complete_world_document(*, name: str = "Rustline", genre: str = "cyberpunk") -> dict:
    """One complete Creator world response; individual tests mutate the failure under test."""
    return {
        "name": name,
        "genre": genre,
        "setting": "Rain-polished towers crowd a harbor where debts are traded as identity.",
        "date": "2088-04-17",
        "time": "night",
        "tone": "tense neon noir",
        "factions": [
            "Glass Choir — brokers stolen voices through the floodlit transit shrines.",
            "Rust Union — keeps the lower docks alive while planning a general strike.",
            "Vanta Court — purchases public offices and quietly erases its rivals.",
            "Lantern Mesh — shelters escaped constructs inside abandoned civic networks.",
        ],
        "locations": [
            "Mirror Quay — cargo cranes cast moving bars of light across black water.",
            "Saint Voltage — a transit shrine built around an outlawed memory archive.",
            "Copper Ward — union tenements surround the city’s last independent foundry.",
            "Vanta Spire — mirrored offices conceal a private courthouse above the clouds.",
            "Floodline Market — traders barter medicine and identities beneath the seawall.",
        ],
        "npcs": [
            {"name": "Mara Venn", "role": "union courier",
             "desc": "She delivers strike plans inside obsolete prosthetic parts.",
             "home": "Copper Ward"},
            {"name": "Ilex Nine", "role": "memory cantor",
             "desc": "They can identify a stolen voice from a single broken syllable.",
             "home": "Saint Voltage"},
            {"name": "Cass Orin", "role": "corporate magistrate",
             "desc": "He sells legal absolution while recording every buyer’s weakness.",
             "home": "Vanta Spire"},
            {"name": "Dena Coil", "role": "flood-market surgeon",
             "desc": "She repairs bodies for anyone who pays in verifiable secrets.",
             "home": "Floodline Market"},
        ],
        "aspects": [
            "A copied voice is accepted as a legal signature unless challenged in person.",
            "The seawall pumps are controlled by three mutually hostile factions.",
            "Constructs may own property but cannot testify against their registered maker.",
            "Public transit records every passenger but forgets them after seven days.",
            "Rain carries conductive dust that makes exposed implants glow blue.",
        ],
        "opening_scene": "At Mirror Quay, Mara Venn presses a warm prosthetic hand into yours.",
        "opening_quest": "Deliver Mara’s hidden strike plan before Vanta Court closes the docks.",
        "loot": {
            tier: [
                {"name": f"{tier} transit chit", "chance": 0.75},
                {"name": f"{tier} sealed medpatch", "chance": 0.35},
            ]
            for tier in ("minion", "standard", "elite", "boss")
        },
        "fronts": [
            {"name": "The Rust Union strikes", "faction": "Rust Union", "segments": 4,
             "consequence": "The lower docks close and union patrols control every crane.",
             "event_duration_turns": 8, "spawn_eligibility": False},
            {"name": "Vanta Court seizes the seawall", "faction": "Vanta Court", "segments": 6,
             "consequence": "Corporate wardens occupy the pumps and ration dry ground.",
             "event_duration_turns": None, "spawn_eligibility": True},
        ],
        "routes": [
            {"a": "Mirror Quay", "b": "Vanta Spire", "segments": 2},
        ],
    }


def _complete_player_document(*, name: str = "Vex") -> dict:
    return {
        "name": name,
        "sex": "female",
        "pronouns": "she/her",
        "species": "human",
        "appearance": "A lean courier in a rain-dark coat, with copper light under one eye.",
        "concept": "Harbor memory runner",
        "stats": {"STR": 10, "DEX": 13, "INT": 12, "CHA": 10, "CUN": 11, "CON": 10},
        "skills": {"stealth": 3, "perception": 2, "athletics": 1},
        "abilities": ["keen_senses"],
        "gear": ["weatherproof courier coat", "sealed memory spindle"],
        "defs": {"skills": [], "abilities": []},
    }


# ------------------------------ deterministic backbone -----------------------------
def test_registry_export_shape():
    rx = creator.registry_export()
    assert {"STR", "DEX", "INT", "CHA", "CUN", "CON"} <= set(rx["stats"])
    assert "stealth" in rx["skills"] and "power_strike" in rx["abilities"]
    assert rx["mod_policy"] == "dnd5e"
    assert "high_fantasy" in rx["genres"] and "morning" in rx["times"]
    assert rx["creator_limits"] == {
        "resource_cost_min": 1, "resource_cost_max": 10000,
    }


def test_deterministic_world_fills_blanks():
    w = creator.deterministic_world({"genre": "cyberpunk", "name": "Rustline"})
    assert w["name"] == "Rustline" and w["genre"] == "cyberpunk"
    assert w["time"] in creator.TIMES and w["setting"]          # blank -> template
    assert len(w["factions"]) >= 3 and w["opening_quest"]


def test_deterministic_world_player_fields_win():
    w = creator.deterministic_world({"genre": "high_fantasy", "setting": "MY PREMISE",
                                     "factions": ["Only Mine"]})
    assert w["setting"] == "MY PREMISE" and w["factions"] == ["Only Mine"]


def test_deterministic_player_filters_unknown_and_freezes_freestyle():
    p = creator.deterministic_player({
        "name": "Vex", "class": "Storm-Touched Skald",
        "skills": {"persuasion": 2, "bogus_skill": 9},
        "abilities": ["silver_tongue", "not_a_real_ability"],
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA", "base_mod": 1},
                              {"name": "Nonsense", "keyed_stat": "ZZZ"}]}})
    assert p["concept"] == "Storm-Touched Skald"
    assert p["skills"] == {"persuasion": 2}                     # bogus id dropped
    assert p["abilities"] == ["silver_tongue"]                 # unknown ability dropped
    assert "stormsong" in p["defs"]["skills"]                  # freestyle FROZEN
    assert p["defs"]["skills"]["stormsong"]["keyed_stat"] == "CHA"
    assert "nonsense" not in p["defs"].get("skills", {})       # invalid keyed_stat rejected


def test_deterministic_player_ranks_and_knows_freestyle_defs():
    """A frozen def skill is rankable and a frozen def ability lands in the KNOWN list — the
    authored custom passive actually applies (registry ∪ defs filtering, not registry-only)."""
    p = creator.deterministic_player({
        "name": "Vex",
        "skills": {"Stormsong": 2, "persuasion": 1, "bogus": 9},
        "abilities": ["Storm Heart", "silver_tongue"],
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA", "base_mod": 1}],
                   "abilities": [{"name": "Storm Heart", "kind": "passive",
                                  "passive_mod": {"skill": "stormsong", "amount": 2}}]}})
    assert p["skills"] == {"stormsong": 2, "persuasion": 1}      # def skill rankable (by name too)
    assert p["abilities"] == ["storm_heart", "silver_tongue"]    # def ability KNOWN, preset kept
    from aetherstate import registry as _reg
    card = {"stats": p["stats"], "skills": p["skills"], "abilities": p["abilities"],
            "defs": p["defs"]}
    # RPG-5 deterministic stat spend: all-baseline stats + ranked skills -> the keyed stat
    # of the top skill gets +2 (CHA 12 -> +1), so: CHA1 + base1 + rank2 + passive2 = 6
    assert _reg.load().effective_mod(card, "stormsong") == 6


def test_deterministic_player_accepts_frozen_definition_maps_from_cards():
    """A committed Narrator card carries defs as id-keyed snapshots, not Creator row lists."""
    p = creator.deterministic_player({
        "name": "Iria", "skills": {"spearline_bind": 2},
        "defs": {"skills": {"spearline_bind": {
            "name": "Spearline Bind", "keyed_stat": "DEX", "base_mod": 0,
            "max_rank": 5, "governs": ["bind", "redirect"], "group": "Maneuvers"}}}})
    assert p["skills"]["spearline_bind"] == 2
    assert p["defs"]["skills"]["spearline_bind"]["name"] == "Spearline Bind"
    assert p["defs"]["skills"]["spearline_bind"]["group"] == "Maneuvers"


def test_deterministic_player_keeps_custom_resources_and_declared_costs():
    p = creator.deterministic_player({
        "name": "Iria", "hp": {"cur": 37, "max": 50},
        "resources": {
            "Ash Focus": {"name": "Ash Focus", "cur": 4, "max": 10000,
                          "color": "#b56cff"},
            "bad_color": {"name": "Bad Color", "cur": 2, "max": 3,
                          "color": "purple"},
        },
        "skills": {"Ashwind Reading": 2},
        "custom": {
            "skills": [{"name": "Ashwind Reading", "keyed_stat": "CUN",
                        "cost": {"Ash Focus": 10000, "mana": 2}}],
            "abilities": [{"name": "Cinder Burst", "kind": "active",
                           "mechanic": "surge",
                           "cost": {"ash_focus": 3, "stamina": 1}}],
        },
    })
    assert p["resources"]["hp"] == {"cur": 37, "max": 50}
    assert p["resources"]["ash_focus"] == {
        "cur": 4, "max": 10000, "name": "Ash Focus", "color": "#b56cff",
    }
    assert "color" not in p["resources"]["bad_color"]
    assert p["defs"]["skills"]["ashwind_reading"]["cost"] == {
        "ash_focus": 10000, "mana": 2,
    }
    assert p["defs"]["abilities"]["cinder_burst"]["cost"] == {
        "ash_focus": 3, "stamina": 1,
    }


def test_creator_blank_active_cost_means_none_instead_of_stamina_fallback():
    p = creator.deterministic_player({
        "name": "Iria",
        "custom": {"abilities": [{
            "name": "Cinder Burst", "kind": "active", "mechanic": "surge",
        }]},
    })
    assert "cost" not in p["defs"]["abilities"]["cinder_burst"]


@pytest.mark.parametrize(("doc", "message"), [
    ({
        "resources": {"Focus": {"name": "Focus", "max": 4}},
        "custom": {"skills": [{
            "name": "Cut", "keyed_stat": "DEX", "cost": {"missing_pool": 2},
        }]},
    }, "unknown resource 'missing_pool'"),
    ({
        "resources": {"Focus": {"name": "Focus", "max": 10000}},
        "custom": {"skills": [{
            "name": "Cut", "keyed_stat": "DEX", "cost": {"Focus": 10001},
        }]},
    }, "between 1 and 10000"),
    ({
        "resources": {"stamina": {"max": 0}},
        "custom": {"skills": [{
            "name": "Cut", "keyed_stat": "DEX", "cost": {"stamina": 1},
        }]},
    }, "unknown resource 'stamina'"),
    ({
        "resources": {"mana": {"max": 0}},
        "custom": {"abilities": [{
            "name": "Flare", "kind": "active", "mechanic": "surge",
            "cost": {"mana": 1},
        }]},
    }, "unknown resource 'mana'"),
    ({
        "resources": {
            "Ash Focus": {"name": "Ash Focus", "max": 4},
            "ash-focus": {"name": "Second Focus", "max": 4},
        },
    }, "resource id collision"),
    ({
        "resources": {
            "first_pool": {"name": "Shared Name", "max": 4},
            "second_pool": {"name": "shared-name", "max": 4},
        },
    }, "resource slug collision"),
    ({
        "resources": {"Focus": {"name": "Focus", "max": 4}},
        "custom": {"skills": [{
            "name": "Cut", "keyed_stat": "DEX",
            "cost": {"Focus": 1, "focus!": 2},
        }]},
    }, "cost collision"),
])
def test_creator_rejects_invalid_resource_contracts(doc, message):
    with pytest.raises(ValueError, match=message):
        creator.deterministic_player(doc)


async def test_author_player_preserves_typed_custom_rank_across_model_defs(monkeypatch):
    authored = _complete_player_document(name="Model Rewrite")
    authored.update({
            "skills": {"spearline_bind": 0, "swordplay": 3},
            "abilities": ["keen_senses"], "defs": {"skills": [{
                "id": "spearline_bind", "name": "Spearline Bind", "keyed_stat": "STR",
                "base_mod": 1, "max_rank": 5,
                "governs": ["strike", "bind", "redirect"],
                "desc": "Model rewrite.", "group": "Model Group"}], "abilities": []}})

    async def fake_chat(*_args, **_kwargs):
        return creator._CreatorReply(json.dumps(authored), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    seed = {
        "name": "Iria Vale", "skills": {"spearline_bind": 2},
        "abilities": ["power_strike"],
        "resources": {"hp": {"max": 500},
                      "resolve": {"name": "Resolve", "cur": 3, "max": 7,
                                  "color": "#77aaff"}},
        "custom": {"skills": [{
            "id": "spearline_bind", "name": "Spearline Bind", "keyed_stat": "DEX",
            "base_mod": 0, "governs": ["bind", "redirect"], "group": "Maneuvers",
            "cost": {"resolve": 2}}]}}
    out = await creator.author_player(
        None, None, SimpleNamespace(base_url="http://author", model="test-model"), seed)
    p = out["doc"]
    assert out["source"] == "llm" and p["name"] == "Iria Vale"
    assert p["skills"]["spearline_bind"] == 2
    assert p["defs"]["skills"]["spearline_bind"]["keyed_stat"] == "DEX"
    assert p["defs"]["skills"]["spearline_bind"]["group"] == "Maneuvers"
    assert "power_strike" in p["abilities"]
    assert p["resources"]["hp"]["max"] == 500
    assert p["resources"]["resolve"] == {
        "cur": 3, "max": 7, "name": "Resolve", "color": "#77aaff",
    }
    assert p["defs"]["skills"]["spearline_bind"]["cost"] == {"resolve": 2}


def test_deterministic_creator_docs_preserve_instruction_notes_without_runtime_promotion():
    world = creator.deterministic_world({"notes": "Use exactly three named fronts."})
    player = creator.deterministic_player({"notes": "Write her as cautious, never comic relief."})
    assert world["notes"] == "Use exactly three named fronts."
    assert player["notes"] == "Write her as cautious, never comic relief."
    assert all("exactly three named fronts" not in str(op) for op in creator.world_to_ops(world))
    assert all("never comic relief" not in str(op) for op in creator.player_to_ops(player))


async def test_world_authoring_retries_direction_breaking_front_count(monkeypatch, caplog):
    direction_breaking = _complete_world_document()  # structurally complete, but only two fronts
    complete = _complete_world_document()
    complete["fronts"].append({
        "name": "The Lantern Mesh frees the constructs",
        "faction": "Lantern Mesh",
        "segments": 5,
        "consequence": "Escaped constructs gain sanctuary and openly patrol Saint Voltage.",
        "event_duration_turns": None,
        "spawn_eligibility": True,
    })
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        doc = direction_breaking if len(calls) == 1 else complete
        return creator._CreatorReply(json.dumps(doc), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    cfg = Config()
    with caplog.at_level("INFO", logger="aetherstate.creator"):
        out = await creator.author_world(
            None,
            cfg,
            SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
            {"notes": (
                "Use exactly three named fronts. Give at least one a duration and at least one "
                "a spawn eligibility effect."
            )},
        )

    assert out["source"] == "llm"
    assert len(out["doc"]["fronts"]) == 3
    assert out["doc"]["notes"].startswith("Use exactly three named fronts.")
    assert len(calls) == 2
    assert {call["max_tokens"] for call in calls} == {32768}
    assert {call["timeout_s"] for call in calls} == {600.0}
    assert all("CREATIVE DIRECTION — CONTROLLING INSTRUCTIONS" in call["user"]
               for call in calls)
    assert all("Use exactly three named fronts." in call["user"] for call in calls)
    assert "CONTROLLING CREATIVE-DIRECTION INSTRUCTIONS" in calls[0]["system"]
    assert "previous response was rejected" in calls[1]["system"].lower()
    assert "requires exactly 3 fronts" in calls[1]["system"].lower()
    assert "Creator completion attempt 1/2 rejected (document_validation); retrying" in caplog.text
    assert "Creator completion attempt 2/2 accepted" in caplog.text
    assert "Lantern Mesh" not in caplog.text
    assert "Rust Union" not in caplog.text


async def test_world_authoring_quarantines_invalid_optional_model_only_front(monkeypatch):
    complete = _complete_world_document()
    complete["fronts"].append({
        "name": "The Unlisted Fleet Claims the Harbor",
        "faction": "A faction absent from the authored world",
        "segments": 5,
        "pace": 1,
        "consequence": "An ungrounded fleet closes every harbor approach to civilian traffic.",
        "event_duration_turns": None,
        "spawn_eligibility": None,
    })
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(complete), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert out["source"] == "llm"
    assert len(calls) == 1
    assert [row["name"] for row in out["doc"]["fronts"]] == [
        "The Rust Union strikes",
        "Vanta Court seizes the seawall",
    ]


async def test_world_authoring_keeps_invalid_player_front_fail_closed(monkeypatch):
    incomplete = _complete_world_document()
    incomplete["fronts"].append({
        "name": "The Player's Named Agenda",
        "faction": "A faction absent from the authored world",
        "segments": 5,
        "consequence": "This row must be completed correctly instead of silently discarded.",
        "event_duration_turns": None,
        "spawn_eligibility": None,
    })
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(incomplete), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"fronts": [{"name": "The Player's Named Agenda"}]},
    )

    assert out["source"] == "error"
    assert len(calls) == 2
    assert "front 3 has invalid fields" in calls[1]["system"].lower()


async def test_world_authoring_keeps_invalid_front_when_default_floor_would_break(monkeypatch):
    incomplete = _complete_world_document()
    incomplete["fronts"] = [
        incomplete["fronts"][0],
        {
            "name": "The Unlisted Fleet Claims the Harbor",
            "faction": "A faction absent from the authored world",
            "segments": 5,
            "consequence": "An ungrounded fleet closes every harbor approach to civilian traffic.",
            "event_duration_turns": None,
            "spawn_eligibility": None,
        },
    ]
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(incomplete), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert out["source"] == "error"
    assert len(calls) == 2


async def test_world_authoring_keeps_invalid_front_required_by_exact_count(monkeypatch):
    incomplete = _complete_world_document()
    incomplete["fronts"].append({
        "name": "The Unlisted Fleet Claims the Harbor",
        "faction": "A faction absent from the authored world",
        "segments": 5,
        "consequence": "An ungrounded fleet closes every harbor approach to civilian traffic.",
        "event_duration_turns": None,
        "spawn_eligibility": None,
    })
    complete = _complete_world_document()
    complete["fronts"].append({
        "name": "The Lantern Mesh Frees the Constructs",
        "faction": "Lantern Mesh",
        "segments": 5,
        "consequence": "Escaped constructs gain sanctuary and openly patrol Saint Voltage.",
        "event_duration_turns": None,
        "spawn_eligibility": True,
    })
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        doc = incomplete if len(calls) == 1 else complete
        return creator._CreatorReply(json.dumps(doc), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": "Create exactly three named fronts."},
    )

    assert out["source"] == "llm"
    assert len(calls) == 2
    assert len(out["doc"]["fronts"]) == 3
    assert "front 3 has invalid fields" in calls[1]["system"].lower()


async def test_world_authoring_normalizes_model_loot_mechanics_before_validation(monkeypatch):
    complete = _complete_world_document()
    complete["loot"]["boss"] = [
        {
            "name": "stormglass memory prism",
            "chance": "0.75",
            "qty_min": "1",
            "qty_max": "2",
        },
        "sealed archive authority",
    ]
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(complete), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert out["source"] == "llm"
    assert len(calls) == 1
    assert out["doc"]["loot"]["boss"] == [
        {
            "name": "stormglass memory prism",
            "qty_min": 1,
            "qty_max": 2,
            "chance": 0.75,
        },
        {
            "name": "sealed archive authority",
            "qty_min": 1,
            "qty_max": 1,
            "chance": 1.0,
        },
    ]


def test_world_validator_honors_explicit_lower_counts_and_no_fronts():
    world = _complete_world_document()
    world["factions"] = world["factions"][:2]
    world["locations"] = world["locations"][:3]
    world["npcs"] = world["npcs"][:2]
    world["fronts"] = []
    world["routes"] = []

    issues = creator._world_validation_issues(world, {
        "notes": (
            "Create exactly 2 distinct factions, exactly 3 named locations, exactly 2 notable "
            "NPCs, and no fronts. Keep the world playable and surprising."
        )
    })

    assert issues == []


def test_world_validator_keeps_rich_defaults_without_explicit_count_direction():
    world = _complete_world_document()
    world["factions"] = world["factions"][:2]
    world["locations"] = world["locations"][:3]
    world["npcs"] = world["npcs"][:2]
    world["fronts"] = []
    world["routes"] = []

    issues = creator._world_validation_issues(world, {"notes": "Keep the mood restrained."})

    assert "factions need at least 4 complete named rows" in issues
    assert "locations need at least 5 complete named rows" in issues
    assert "npcs need at least 4 rows" in issues
    assert "fronts need at least 2 rows" in issues


async def test_world_authoring_accepts_explicit_lower_counts_without_retry(monkeypatch):
    complete = _complete_world_document()
    complete["factions"] = complete["factions"][:2]
    complete["locations"] = complete["locations"][:3]
    complete["npcs"] = complete["npcs"][:2]
    complete["fronts"] = []
    complete["routes"] = []
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(complete), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    notes = (
        "Create exactly 2 distinct factions, exactly 3 named locations, exactly 2 notable NPCs, "
        "and no fronts. Keep the world playable and surprising."
    )

    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": notes},
    )

    assert out["source"] == "llm"
    assert len(calls) == 1
    assert len(out["doc"]["factions"]) == 2
    assert len(out["doc"]["locations"]) == 3
    assert len(out["doc"]["npcs"]) == 2
    assert out["doc"]["fronts"] == []
    assert "richness defaults, not hidden minimums" in calls[0]["system"]
    assert "Fronts are optional" in calls[0]["system"]


async def test_world_authoring_never_applies_empty_truncated_or_dangling_prose(monkeypatch):
    cut = _complete_world_document()
    cut["setting"] = "The harbor survives because the"
    replies = [
        creator._CreatorReply("", "stop"),
        creator._CreatorReply(json.dumps(cut), "stop"),
    ]

    async def fake_chat(*_args, **_kwargs):
        return replies.pop(0)

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert out["source"] == "error"
    assert "doc" not in out
    assert "nothing was loaded" in out["detail"].lower()


async def test_world_authoring_retries_provider_length_stop_as_a_fresh_document(monkeypatch):
    first = _complete_world_document(name="Cut But Parseable")
    second = _complete_world_document(name="Complete Replacement")
    replies = [
        creator._CreatorReply(json.dumps(first), "length"),
        creator._CreatorReply(json.dumps(second), "stop"),
    ]
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return replies.pop(0)

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert out["source"] == "llm"
    assert out["doc"]["name"] == "Complete Replacement"
    assert len(calls) == 2
    assert "finish_reason=length" in calls[1]["system"]
    assert "Start over" in calls[1]["system"]


async def test_world_authoring_does_not_retry_permanent_provider_rejection(monkeypatch):
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise creator._CreatorCallError("main model rejected the request (HTTP 402)")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_world(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {},
    )

    assert calls == 1
    assert out["source"] == "error"
    assert "provider access" in out["detail"].lower()
    assert "try the ai fill again" not in out["detail"].lower()


def test_creator_json_parser_rejects_partial_repair_and_trailing_text():
    with pytest.raises(ValueError, match="invalid or truncated JSON"):
        creator._strict_creator_json_object('{"name":"half"')
    with pytest.raises(ValueError, match="text appeared after"):
        creator._strict_creator_json_object('{"name":"whole"} continue below')


async def test_player_authoring_retries_structurally_incomplete_reply(monkeypatch):
    incomplete = _complete_player_document()
    incomplete.pop("gear")
    replies = [incomplete, _complete_player_document()]
    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(replies.pop(0)), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": "Keep the character severe and practical."},
    )

    assert out["source"] == "llm"
    assert len(out["doc"]["gear"]) == 2
    assert out["doc"]["notes"] == "Keep the character severe and practical."
    assert len(calls) == 2
    assert all("CREATIVE DIRECTION — CONTROLLING INSTRUCTIONS" in call["user"]
               for call in calls)
    assert all("Keep the character severe and practical." in call["user"] for call in calls)
    assert "CONTROLLING CREATIVE-DIRECTION INSTRUCTIONS" in calls[0]["system"]
    assert "previous response was rejected" in calls[1]["system"].lower()


async def test_player_authoring_can_fulfill_requested_custom_resource_and_cost(monkeypatch):
    authored = _complete_player_document(name="Eira Sol")
    authored["gear"] = [
        {"name": "Tuning fork roll", "slot": "waist",
         "effect": "Identifies a bell's dominant memory-tone when struck."},
        {"name": "Salt-blue route coat", "slot": "body",
         "effect": "Keeps its wearer warm and visible during moonlit crossings."},
        {"name": "Hush-bell pendant", "slot": "neck",
         "effect": "Carries one silent memory that only Eira can hear."},
    ]
    authored["resources"] = {
        "resonance": {"name": "Resonance", "cur": 6, "max": 6, "color": "#66ccff"},
    }
    authored["abilities"] = ["keen_senses", "bell_resonance"]
    authored["defs"]["abilities"] = [{
        "id": "bell_resonance", "name": "Bell Resonance", "kind": "active",
        "mechanic": "surge", "applies_to": "perception", "magnitude": 1,
        "group": "Bellcraft", "cost": {"resonance": 1}, "cooldown_turns": 1,
        "effect": "Eira releases a tuned memory-tone to lift one difficult reading.",
        "desc": "Spend Resonance to strengthen one perception check beyond its usual ceiling.",
    }]

    calls = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return creator._CreatorReply(json.dumps(authored), "stop")

    monkeypatch.setattr(creator, "_creator_chat", fake_chat)
    out = await creator.author_player(
        None,
        Config(),
        SimpleNamespace(base_url="http://main", model="main-model", api_key=""),
        {"notes": (
            "Fit her closely to Lumenwake Crossing. Give her a complete backstory rooted in "
            "the orchard-barges and a hopeful personal mystery. Give exactly 3 starting gear "
            "pieces, each with a complete useful effect. Add one custom resource named "
            "Resonance with maximum 6 and current 6, plus one custom ability that spends 1 "
            "Resonance. Keep her capable but not overpowered."
        )},
    )

    assert out["source"] == "llm"
    assert len(calls) == 1
    assert len(out["doc"]["gear"]) == 3
    assert out["doc"]["resources"]["resonance"] == {
        "cur": 6, "max": 6, "name": "Resonance", "color": "#66ccff",
    }
    assert out["doc"]["defs"]["abilities"]["bell_resonance"]["cost"] == {
        "resonance": 1,
    }


def test_world_ops_use_only_shipped_vocab_and_validate():
    ops = creator.world_to_ops({"genre": "high_fantasy", "name": "Aldering"})
    assert ops and all(validate_op(o) is not None for o in ops)
    kinds = {o["op"] for o in ops}
    assert kinds <= set(_SPEC)                                 # every authoring op is validated
    assert kinds <= {
        "world_identity_set", "creator_world_seed", "memory_event", "entity_add",
        "set_attribute", "scene_set", "time_advance", "quest_add",
    }  # Creator source is durable lore metadata; the opening quest remains ledger truth.


def test_player_ops_shape_and_validate():
    ops = creator.player_to_ops({"name": "Kara", "class": "Knight", "species": "Human", "sex": "F"})
    assert [o["op"] for o in ops][:2] == ["entity_add", "player_seed"]
    assert all(validate_op(o) is not None for o in ops)
    card = ops[1]["card"]
    assert card["concept"] == "Knight" and set(card["stats"]) >= {"STR", "DEX"}


def test_player_ops_commit_hp_at_top_level_and_custom_pools_separately():
    ops = creator.player_to_ops({
        "name": "Sable", "hp": {"cur": 31, "max": 45},
        "resources": {"Ruin Echo": {"name": "Ruin Echo", "cur": 2, "max": 6,
                                     "color": "#8844cc"}},
    })
    card = ops[1]["card"]
    assert card["hp"] == {"cur": 31, "max": 45}
    assert "hp" not in card["resources"]
    assert card["resources"]["ruin_echo"] == {
        "cur": 2, "max": 6, "name": "Ruin Echo", "color": "#8844cc",
    }


# ------------------------------ persistence + replay -------------------------------
def _apply(ops, cfg=None, turn=0):
    cfg = cfg or Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="creator-t")
    res = apply_delta(store, sid, bid, turn, ops, "user", cfg)
    return store, bid, res


def test_world_ops_persist_entities_and_lore():
    ops = creator.world_to_ops({"genre": "dark_fantasy", "name": "Gallow",
                                "npcs": [{"name": "The Warden", "role": "jailer"}]})
    store, bid, _ = _apply(ops)
    st = current_state(store, bid)
    kinds = {e.get("kind") for e in st["entities"].values()}
    assert {"faction", "location", "npc"} <= kinds
    assert st["attributes"].get("the_warden", {}).get("role") == "jailer"


def test_player_ops_persist_card_and_defs():
    ops = creator.player_to_ops({
        "name": "Vex", "class": "Skald", "stats": {"CHA": 16},
        "skills": {"persuasion": 2},
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA"}]}})
    store, bid, _ = _apply(ops)
    st = current_state(store, bid)
    player = st["player"]["vex"]
    assert player["concept"] == "Skald" and player["stats"]["CHA"] == 16
    assert player["skills"]["persuasion"] == 2
    assert "stormsong" in player["defs"]["skills"]
    assert st["attributes"].get("vex", {}).get("class") == "Skald"


def test_seeded_player_is_replay_pure():
    ops = creator.player_to_ops({"name": "Mara", "class": "Ranger", "stats": {"DEX": 15}})
    store, bid, _ = _apply(ops)
    live = current_state(store, bid)["player"]
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())["player"]
    assert replay == live                                      # journal -> identical player card


def test_creator_seed_inert_under_none():
    cfg = Config()                                             # specialization.name == "none"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="creator-none")
    apply_delta(store, sid, bid, 0,
                creator.player_to_ops({"name": "Vex", "class": "Skald"}), "user", cfg)
    apply_delta(store, sid, bid, 1,
                creator.world_to_ops({"genre": "sci_fi", "name": "Verge"}), "user", cfg)
    h = render_header(current_state(store, bid), cfg)
    assert "[PLAYER]" not in h and "[DIRECTIVE]" not in h and "[QUEST]" not in h


# ------------------------------ control routes (in-process) ------------------------
async def test_registry_route(client):
    r = await client.get("/aether/registry")
    assert r.status_code == 200 and "STR" in r.json()["stats"]


async def test_creator_page_served(client):
    r = await client.get("/aether/creator")
    assert r.status_code == 200 and "Creator" in r.text
    assert 'id="newDraft"' in r.text
    assert "Clear this Creator draft and start a completely blank world" not in r.text
    assert '$("newDraft").onclick=clearCreatorDraft;' in r.text
    assert "function removeDraftRow" in r.text
    assert "onclick=\"removeDraftRow(this)\"" in r.text
    assert 'if(fromSession && sessionContext?.sid)' in r.text
    assert "const haveForm = !fromSession&&!!(" in r.text
    assert 'refreshPoints(); refreshSkillMods(); scheduleDraft();' in r.text
    assert '$("sr_"+sid).textContent=skillRanks[sid]; refreshSkillMods(); scheduleDraft();' in r.text
    assert "function genreChanged(resetRanks=true)" in r.text
    assert "genreChanged(false);" in r.text
    assert '$("w_notes").value=w.notes||"";' in r.text
    assert '$("c_notes").value=p.notes||"";' in r.text
    assert 'title="skill rank (0-5)"' in r.text
    assert "function readCustomSkillRanks()" in r.text
    assert "Object.assign(skills, readCustomSkillRanks());" in r.text
    assert "const sid=customSkillId(base.id||el[0].value);" in r.text
    assert "_rank:+ranks[sid]||+ranks[rid]||0" in r.text
    assert "Object.assign({id:sid},def" in r.text
    assert 'id="c_resources"' in r.text
    assert 'onclick="addCustomResource()"' in r.text
    assert 'data-field="name"' in r.text and 'data-field="color"' in r.text
    assert "function readCustomResources()" in r.text
    assert "function parseResourceCost(raw)" in r.text
    assert "function resourceCostLimits()" in r.text
    assert "const cost=readCostInput(el[4])" in r.text
    assert "const cost=readCostInput(el[5])" in r.text
    assert "Object.entries(c).map(([rid,amount])" in r.text
    assert "Unknown resource '${label}'. Add that resource bar or remove this cost." in r.text
    assert "Resource slug collision: two rows become '${rid}'." in r.text
    assert 'data-resource-cost placeholder="cost(s)"' in r.text
    assert 'list="aes_resources"' in r.text
    assert "readCustomResources());" in r.text
    assert "addCustomResource(rid,spec)" in r.text
    assert 'chipRow("resources"' in r.text
    assert 'title="Main story model used for every AI auto-fill"' in r.text
    assert 'draft=mode==="world"?worldDoc():playerDoc()' in r.text
    assert 'worldDraft=mode==="player"?worldDoc():null' in r.text
    assert "function mergeAuthorResult" in r.text
    assert "mergeAuthorResult(draft,proposal,current)" in r.text
    assert 'return runCreatorAuthoring("world")' in r.text
    assert 'return runCreatorAuthoring("player")' in r.text
    assert "WORLD_FORM_EPOCH!==context.worldEpoch" in r.text
    assert "PLAYER_FORM_EPOCH!==context.playerEpoch" in r.text
    assert 'const activeSection=mode==="player"?"char":"world"' in r.text
    assert 'setAttribute("aria-busy",String(busy&&section===activeSection))' in r.text
    assert "function canonicalPlayerDocument" in r.text
    assert "return canonicalPlayerDocument({" in r.text
    assert 'id="w_ai_retry"' in r.text and 'id="c_ai_retry"' in r.text


async def test_world_and_player_routes_persist(client):
    g = await client.post("/aether/session/route-t/genesis",
                          json={"card": "A wizard's tower.", "greeting": "Welcome."})
    assert g.status_code == 200
    w = await client.post("/aether/session/route-t/world",
                          json={"world": {"genre": "high_fantasy", "name": "Aldreth"}})
    assert w.status_code == 200 and w.json()["applied"] > 0
    p = await client.post("/aether/session/route-t/player",
                          json={"player": {"name": "Rillian", "class": "Rogue"}})
    assert p.status_code == 200 and p.json()["applied"] > 0
    pre = (await client.get("/aether/session/route-t/creator")).json()
    assert pre["player"] and pre["player"]["concept"] == "Rogue"
    assert pre["world_seeded"] is True


async def test_creator_control_routes_reject_invalid_resource_references(client):
    bad_player = {
        "name": "Bad Cost",
        "custom": {"skills": [{
            "name": "Ghost Spend", "keyed_stat": "CUN",
            "cost": {"removed_pool": 2},
        }]},
    }
    bad_seed = {
        "world": {"name": "No Partial World"},
        "player": bad_player,
    }
    requests = [
        ("/aether/session/resource-bad/player", {"player": bad_player}),
        ("/aether/session/resource-bad-seed/seed", {
            "seed": bad_seed,
            "seed_fingerprint": narrator.seed_fingerprint(bad_seed),
        }),
        ("/aether/session/resource-bad-author/author", {
            "mode": "player", "offline": 1, "doc": bad_player,
        }),
        ("/aether/presets", {
            "kind": "player", "name": "Invalid Resource Preset", "doc": bad_player,
        }),
        ("/aether/narrator-card", {
            "world": {"name": "Invalid Resource Card"}, "player": bad_player,
        }),
    ]
    for path, payload in requests:
        response = await client.post(path, json=payload)
        assert response.status_code == 422, (path, response.text)
        assert "unknown resource 'removed_pool'" in response.json()["error"]

    disabled = await client.post("/aether/session/resource-disabled/player", json={"player": {
        "name": "Disabled Cost", "resources": {"stamina": {"max": 0}},
        "custom": {"abilities": [{
            "name": "Empty Sprint", "kind": "active", "mechanic": "surge",
            "cost": {"stamina": 1},
        }]},
    }})
    assert disabled.status_code == 422
    assert "unknown resource 'stamina'" in disabled.json()["error"]

    pre = (await client.get("/aether/session/resource-bad-seed/creator")).json()
    assert pre["world_seeded"] is False and pre["player"] is None
    presets = (await client.get("/aether/presets")).json()["presets"]
    assert not any(p["name"] == "Invalid Resource Preset" for p in presets)


async def test_creator_control_preserves_maximum_and_multi_resource_cost(client):
    response = await client.post("/aether/session/resource-good/player", json={"player": {
        "name": "Exact Cost",
        "resources": {"Focus": {"name": "Focus", "cur": 10000, "max": 10000}},
        "skills": {"Split Channel": 2},
        "custom": {"skills": [{
            "name": "Split Channel", "keyed_stat": "CUN",
            "cost": {"Focus": 10000, "mana": 2},
        }]},
    }})
    assert response.status_code == 200
    player = (await client.get("/aether/session/resource-good/creator")).json()["player"]
    assert player["defs"]["skills"]["split_channel"]["cost"] == {
        "focus": 10000, "mana": 2,
    }
    assert player["resources"]["focus"]["max"] == 10000


async def test_creator_first_save_mints_session(client):
    """2026-07-06 live repro ('Save failed: HTTP 404'): a brand-new chat has no session row
    until its first message flows through the relay, so a creator-first world/character save
    bounced with 404. The save routes now mint the session by external id — the same row the
    relay adopts when the chat's first stamped message arrives."""
    w = await client.post("/aether/session/st-neverseen1/world",
                          json={"world": {"genre": "high_fantasy", "name": "Firsthold"}})
    assert w.status_code == 200
    j = w.json()
    assert j["applied"] > 0 and j["session_id"]
    p = await client.post("/aether/session/st-neverseen1/player",
                          json={"player": {"name": "Kael", "class": "Warrior"}})
    assert p.status_code == 200 and p.json()["applied"] > 0
    # both saves converged on ONE session, resolvable by the external id
    assert p.json()["session_id"] == j["session_id"]
    sessions = (await client.get("/aether/sessions")).json()["sessions"]
    mine = [s for s in sessions if s["external_id"] == "st-neverseen1"]
    assert len(mine) == 1 and mine[0]["session_id"] == j["session_id"]
    pre = (await client.get("/aether/session/st-neverseen1/creator")).json()
    assert pre["world_seeded"] is True
    assert pre["player"] and pre["player"]["concept"] == "Warrior"


async def test_author_route_no_model_is_honest_error(client):
    """2026-07-06: no reachable model -> source='error' + detail; the window keeps the
    form untouched instead of silently swapping in templates."""
    r = await client.post("/aether/session/route-missing/author",
                          json={"mode": "world", "doc": {"genre": "cyberpunk"}})
    assert r.status_code == 200
    j = r.json()
    assert j["source"] == "error" and "model" in j["detail"]


async def test_author_route_offline_fills_templates_on_request(client):
    """Templates stay one explicit click away (offline:1) — deterministic, no LLM call."""
    r = await client.post("/aether/session/route-missing/author",
                          json={"mode": "world", "offline": 1,
                                "doc": {"genre": "cyberpunk"}})
    j = r.json()
    assert j["source"] == "deterministic" and j["doc"]["genre"] == "cyberpunk"
    assert j["doc"]["setting"]                                  # blanks template-filled
    r2 = await client.post("/aether/session/route-missing/author",
                           json={"mode": "player", "offline": 1, "doc": {"name": "Vex"}})
    assert r2.json()["source"] == "deterministic" and r2.json()["doc"]["name"] == "Vex"

    alias = await client.post("/aether/session/route-missing/author",
                              json={"mode": "character", "offline": 1,
                                    "doc": {"name": "Alias Vex"}})
    assert alias.status_code == 200 and alias.json()["mode"] == "player"

    invalid = await client.post("/aether/session/route-missing/author",
                                json={"mode": "typo", "offline": 1, "doc": {}})
    assert invalid.status_code == 422
    assert invalid.json() == {"error": "mode must be world|player"}

    malformed_cases = [
        ([], "request body must be an object"),
        ({"mode": ["world"], "offline": 1}, "mode must be a string"),
        ({"mode": "world", "offline": 1, "doc": []}, "doc must be an object"),
        ({"mode": "player", "offline": 1, "world": "none"}, "world must be an object"),
        ({"mode": "world", "offline": 1, "model": 7}, "model must be a string"),
    ]
    for body, message in malformed_cases:
        malformed = await client.post("/aether/session/route-missing/author", json=body)
        assert malformed.status_code == 422
        assert malformed.json() == {"error": message}


# ------------------------------ model detection (menu + author route) ---------------
def _chat_reply(content: str, *, finish_reason: str = "stop") -> bytes:
    import json
    return json.dumps({"choices": [{"message": {"content": content},
                                     "finish_reason": finish_reason}]}).encode()


async def test_creator_models_route_lists_and_fails_open(client, mock_upstream):
    import json
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply(body=json.dumps(
        {"data": [{"id": "m-big"}, {"id": "m-small"}]}).encode()))
    r = await client.get("/aether/creator/models")
    assert r.status_code == 200
    eps = r.json()["endpoints"]
    assert eps and eps[0]["models"] == ["m-big", "m-small"]
    # unscripted upstream (500) -> fail-open to an empty list, never an error
    r2 = await client.get("/aether/creator/models")
    assert r2.status_code == 200 and r2.json()["endpoints"][0]["models"] == []


async def test_author_uses_requested_model_without_session(client, mock_upstream):
    """Creator-first flow: no session row yet, but an explicit model pick still AI-authors."""
    import json
    from tests.mock_upstream import Reply
    authored = _complete_world_document()
    authored["fronts"].append({
        "name": "The Lantern Mesh frees the constructs",
        "faction": "Lantern Mesh",
        "segments": 5,
        "consequence": "Escaped constructs gain sanctuary and openly patrol Saint Voltage.",
        "event_duration_turns": None,
        "spawn_eligibility": True,
    })
    mock_upstream.enqueue(Reply(body=_chat_reply(json.dumps(authored))))
    r = await client.post("/aether/session/no-such-session/author",
                          json={"mode": "world", "doc": {
                              "genre": "cyberpunk",
                              "notes": "Use exactly three named fronts and make each consequential.",
                          },
                                "model": "picked-model"})
    j = r.json()
    assert j["source"] == "llm" and j["model"] == "picked-model"
    assert j["doc"]["name"] == "Rustline"
    assert len(j["doc"]["fronts"]) == 3
    request_body = json.loads(mock_upstream.requests[-1].body)
    assert request_body["model"] == "picked-model"
    assert request_body["max_tokens"] == 32768
    assert "Use exactly three named fronts" in request_body["messages"][1]["content"]
    assert j["doc"]["notes"] == "Use exactly three named fronts and make each consequential."


async def test_author_autodetects_model_from_endpoint(client, mock_upstream):
    """Fresh session, nothing proxied yet: the author route detects a model via GET /models
    instead of posting model='' (the bug: backend 400 -> silent deterministic fallback)."""
    import json
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply(body=json.dumps({"data": [{"id": "auto-m"}]}).encode()))
    mock_upstream.enqueue(Reply(body=_chat_reply(json.dumps(
        _complete_world_document(name="Verge", genre="sci_fi")))))
    r = await client.post("/aether/session/still-no-session/author",
                          json={"mode": "world", "doc": {}})
    j = r.json()
    assert j["source"] == "llm" and j["model"] == "auto-m"
    chat = [q for q in mock_upstream.requests if q.path.endswith("/chat/completions")]
    assert json.loads(chat[-1].body)["model"] == "auto-m"


async def test_creator_world_and_character_always_use_main_endpoint_and_large_budget(
        client, mock_upstream, cfg):
    """Creator authoring is independent of extraction=assist and never borrows the small model."""
    from tests.mock_upstream import Reply

    cfg.upstream.model = "main-creator"
    cfg.extraction.mode = "assist"
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="small-helper", base_url="http://assist.invalid/v1", model="tiny-assist",
    )]
    mock_upstream.enqueue(Reply(body=_chat_reply(json.dumps(_complete_world_document()))))
    mock_upstream.enqueue(Reply(body=_chat_reply(json.dumps(_complete_player_document()))))

    world = await client.post("/aether/session/main-only/author", json={
        "mode": "world",
        "doc": {"notes": "Make the fronts permanent, temporary, and superseding."},
    })
    player = await client.post("/aether/session/main-only/author", json={
        "mode": "player",
        "doc": {"notes": "Write a guarded investigator with no comic relief."},
        "world": _complete_world_document(),
    })

    assert world.json()["source"] == player.json()["source"] == "llm"
    chats = [json.loads(req.body) for req in mock_upstream.requests
             if req.path.endswith("/chat/completions")]
    assert [body["model"] for body in chats] == ["main-creator", "main-creator"]
    assert [body["max_tokens"] for body in chats] == [32768, 32768]
    assert "Make the fronts permanent" in chats[0]["messages"][1]["content"]
    assert "guarded investigator" in chats[1]["messages"][1]["content"]
    assert cfg.upstream.model == "main-creator"


async def test_creator_model_menu_exposes_main_models_only(client, mock_upstream, cfg):
    from tests.mock_upstream import Reply

    cfg.upstream.model = "configured-main"
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="small-helper", base_url="http://assist.invalid/v1", model="tiny-assist",
    )]
    mock_upstream.enqueue(Reply(body=json.dumps({"data": [{"id": "detected-main"}]}).encode()))

    payload = (await client.get("/aether/creator/models")).json()
    assert payload["endpoints"] == [{
        "target": "main",
        "base_url": cfg.upstream.base_url,
        "default": "configured-main",
        "models": ["configured-main", "detected-main"],
    }]


# ------------------------------ filled boxes ride as context (2026-07-06) -----------
def test_world_user_prompt_carries_every_filled_field():
    seed = {"setting": "S" * 3000, "npcs": [{"name": "Maren", "role": "witch", "desc": "creditor"}],
            "opening_scene": "A skiff at dawn", "opening_quest": "Pay the debt",
            "notes": "gothic tides"}
    u = creator._world_user(seed)
    assert "Maren" in u and "witch" in u             # npcs were dropped before the fix
    assert "A skiff at dawn" in u and "Pay the debt" in u
    assert "gothic tides" in u
    assert "S" * 2500 in u                           # long settings ride (not clipped to 2000)


def test_char_user_prompt_carries_world_and_custom():
    world = {"genre": "sci_fi", "aspects": ["FTL is rationed"],
             "locations": ["Dock 12"], "npcs": [{"name": "Vala", "role": "fixer", "desc": ""}]}
    seed = {"concept": "Void Cantor", "skills": {"stealth": 2},
            "custom": {"abilities": [{"name": "Null Hymn", "kind": "active"}]}}
    u = creator._char_user(seed, world)
    assert "FTL is rationed" in u and "Dock 12" in u and "Vala" in u
    assert "stealth=2" in u and "Null Hymn" in u


def test_json_or_none_strips_fences_and_prose():
    """2026-07-06 live repro: GLM fenced its creator JSON — parse must survive fences
    and prose prefixes."""
    from aetherstate.assist import _json_or_none
    assert _json_or_none('```json\n{"name":"Tidefall"}\n```') == {"name": "Tidefall"}
    assert _json_or_none('Here you go:\n{"name":"Tidefall"}') == {"name": "Tidefall"}
    assert _json_or_none("no json at all") is None


# ------------------------------ presets + committed-state review (2026-07-06) -------
async def test_presets_roundtrip(client):
    r = await client.post("/aether/presets", json={
        "kind": "world", "name": "Tidefall", "doc": {"genre": "dark_fantasy", "name": "Tidefall"}})
    assert r.status_code == 200
    pid = r.json()["preset_id"]
    lst = (await client.get("/aether/presets")).json()["presets"]
    assert any(p["preset_id"] == pid and p["kind"] == "world" for p in lst)
    got = (await client.get(f"/aether/presets/{pid}")).json()
    assert got["doc"]["genre"] == "dark_fantasy"
    # upsert by (kind, name) — same id, new doc
    r2 = await client.post("/aether/presets", json={
        "kind": "world", "name": "Tidefall", "doc": {"genre": "sci_fi"}})
    assert r2.json()["preset_id"] == pid
    assert (await client.get(f"/aether/presets/{pid}")).json()["doc"]["genre"] == "sci_fi"
    assert (await client.delete(f"/aether/presets/{pid}")).status_code == 200
    assert (await client.get(f"/aether/presets/{pid}")).status_code == 404
    bad = await client.post("/aether/presets", json={"kind": "nope", "name": "x", "doc": {}})
    assert bad.status_code == 422


async def test_prefill_returns_committed_world_doc(client):
    await client.post("/aether/session/rev-t/genesis",
                      json={"card": "A tower.", "greeting": "Hello."})
    await client.post("/aether/session/rev-t/world", json={"world": {
        "genre": "high_fantasy", "name": "Aldreth", "setting": "Nine feuding baronies.",
        "factions": ["The Ash Order"], "locations": ["Highmoor"],
        "npcs": [{"name": "Serane", "role": "seneschal", "desc": "keeps the keys"}],
        "aspects": ["Magic is taxed."], "opening_quest": "Find the heir."}})
    pre = (await client.get("/aether/session/rev-t/creator")).json()
    w = pre["world"]
    assert pre["world_seeded"] and w
    assert w["name"] == "Aldreth" and w["genre"] == "high_fantasy"
    assert w["setting"] == "Nine feuding baronies."
    assert "The Ash Order" in w["factions"] and "Highmoor" in w["locations"]
    assert w["npcs"] and w["npcs"][0]["name"] == "Serane" and w["npcs"][0]["role"] == "seneschal"
    assert "Magic is taxed." in w["aspects"] and w["opening_quest"] == "Find the heir."


async def test_connection_persists_upstream_default_model(client):
    r = await client.post("/aether/connection", json={"target": "upstream",
                                                      "base_url": "http://mock-upstream/v1",
                                                      "model": "glm-main"})
    assert r.status_code == 200 and r.json()["upstream"]["model"] == "glm-main"
    g = (await client.get("/aether/connection")).json()
    assert g["upstream"]["model"] == "glm-main"


# ------------------------------ roomier authoring clamps ---------------------------
def test_wider_authoring_clamps_still_clamp():
    w = creator.deterministic_world({"genre": "modern", "setting": "x" * 10000,
                                     "factions": [f"f{i}" for i in range(40)]})
    assert len(w["setting"]) == 8000                     # much roomier, still bounded
    assert len(w["factions"]) == 20                      # roomier list cap, still a cap
    p = creator.deterministic_player({
        "name": "Vex",
        "custom": {"skills": [{"name": "Storm Song", "keyed_stat": "CHA"}],
                   "abilities": [{"name": "Iron Hide", "kind": "passive",
                                  "passive_mod": {"skill": "Storm Song", "amount": 9}}]}})
    pm = p["defs"]["abilities"]["iron_hide"]["passive_mod"]
    assert pm == {"skill": "storm_song", "amount": 5}    # name slugged to id; amount clamped ±5
    # (2026-07-06: the target must EXIST now — a dead reference no longer freezes as real)


# ------------------------------ 2026-07-06 live-playtest fixes ------------------------------
def test_split_name_desc_mints_clean_entity_ids():
    """'Name — description' faction/location lines: the NAME is the entity, the description
    becomes an attribute — no more 80-char slug ids (Creator cousin of the vael_cora bug)."""
    ops = creator.world_to_ops({
        "genre": "sci_fi", "name": "Kessler Deep",
        "factions": ["The Lattice Combine — salvage cartel that controls the docking spines"],
        "locations": ["Spindle Market — pressurized bazaar, neutral ground by treaty"]})
    adds = [o for o in ops if o["op"] == "entity_add"]
    names = {o["name"] for o in adds}
    assert "The Lattice Combine" in names and "Spindle Market" in names
    assert all("—" not in o["name"] and len(o["name"]) <= 80 for o in adds)
    descs = [o for o in ops if o["op"] == "set_attribute" and o["key"] == "description"]
    assert any(o["entity"] == "the_lattice_combine" for o in descs)
    scene = [o for o in ops if o["op"] == "scene_set"]
    assert scene and scene[0]["location"] == "spindle_market"


def test_split_name_desc_plain_lines_untouched():
    name, desc = creator._split_name_desc("Rookhollow")
    assert name == "Rookhollow" and desc == ""


def test_passive_target_resolves_or_dies():
    """GLM authored a passive boosting 'vac_ops' while minting 'vacuum_operations' — the mod
    never applied and LOOKED real. Now: resolved when unambiguous, dropped when not."""
    p = creator.deterministic_player({
        "name": "Rell",
        "custom": {"skills": [{"name": "Vacuum Operations", "keyed_stat": "CON"},
                              {"name": "Neural Lace Intrusion", "keyed_stat": "INT"}],
                   "abilities": [
                       {"name": "Wrongfix", "kind": "passive",
                        "passive_mod": {"skill": "vac_ops", "amount": 1}},
                       {"name": "Ghost Step", "kind": "passive",
                        "passive_mod": {"skill": "lace_intrusion", "amount": 1}},
                       {"name": "Dead Ref", "kind": "passive",
                        "passive_mod": {"skill": "warp_gazing", "amount": 2}}]}})
    ab = p["defs"]["abilities"]
    assert ab["wrongfix"]["passive_mod"]["skill"] == "vacuum_operations"
    assert ab["ghost_step"]["passive_mod"]["skill"] == "neural_lace_intrusion"
    assert "passive_mod" not in ab["dead_ref"]          # unresolvable -> dropped, not fake


def test_defs_requires_ability_kept_when_satisfiable():
    p = creator.deterministic_player({
        "name": "Rell",
        "custom": {"skills": [{"name": "Systems Intrusion", "keyed_stat": "INT",
                               "requires_ability": "Neural Lace"},
                              {"name": "Ghost Craft", "keyed_stat": "INT",
                               "requires_ability": "nonexistent_gift"}],
                   "abilities": [{"name": "Neural Lace", "kind": "basis",
                                  "effect": "Basis for Systems Intrusion."}]}})
    sk = p["defs"]["skills"]
    assert sk["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert "requires_ability" not in sk["ghost_craft"]  # unsatisfiable gate stripped
    assert p["defs"]["abilities"]["neural_lace"]["kind"] == "basis"


def test_registry_export_carries_genre_packs():
    rx = creator.registry_export()
    assert "sci_fi" in rx["genre_packs"]
    pack = rx["genre_packs"]["sci_fi"]
    assert "spellcraft" in pack["hide_skills"]          # no Spellcraft on a sci-fi sheet
    assert pack["skills"]["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert rx["concept_hints"]["sci_fi"]


def test_inject_pack_defs_freezes_ranked_pack_entries():
    """Ranks on genre-pack ids must survive deterministic_player: the pack entry (and the
    basis ability its gate needs) is frozen into custom defs before the fill."""
    pack = creator.GENRE_PACKS["sci_fi"]
    doc = creator._inject_pack_defs(
        {"name": "Rell", "skills": {"gunnery": 2, "systems_intrusion": 3},
         "abilities": ["combat_stims"]}, pack)
    p = creator.deterministic_player(doc)
    assert p["skills"]["gunnery"] == 2 and p["skills"]["systems_intrusion"] == 3
    assert "gunnery" in p["defs"]["skills"]
    assert p["defs"]["skills"]["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert "neural_lace" in p["defs"]["abilities"]      # basis auto-frozen with its gate
    assert "combat_stims" in p["defs"]["abilities"]
    assert "combat_stims" in p["abilities"]


def test_pack_gate_is_live_after_seed():
    """End-to-end: a pack-built card carries the def's requires_ability and the eligibility
    gate reads it. Ranking the gated skill at CREATION auto-freezes its basis (intended:
    creation is where capability is built); without that def the gate stays closed."""
    from aetherstate import registry as _registry
    pack = creator.GENRE_PACKS["sci_fi"]
    reg = _registry.load(None)
    doc = creator._inject_pack_defs({"name": "Rell", "skills": {"systems_intrusion": 2}}, pack)
    p = creator.deterministic_player(doc)
    card = {"skills": p["skills"], "abilities": p["abilities"], "defs": p["defs"],
            "stats": p["stats"]}
    entry = reg.skill_entry("systems_intrusion", card)
    assert entry.get("requires_ability") == "neural_lace"
    assert reg.has_ability(card, "neural_lace")          # basis frozen with the ranked skill
    # A card whose def carries the gate but NOT the basis: the gate stays closed (non-move).
    bare = creator.deterministic_player({
        "name": "Kel",
        "custom": {"skills": [dict(pack["skills"]["systems_intrusion"],
                                   id="systems_intrusion",
                                   requires_ability="neural_lace")]}})
    # requires_ability survives only when satisfiable — freezing the gate without its basis
    # would brick the skill, so _coerce_defs strips it (validated design, see F5 post-pass).
    bcard = {"skills": bare["skills"], "abilities": [], "defs": bare["defs"], "stats": bare["stats"]}
    assert not reg.has_ability(bcard, "neural_lace")     # no basis def, no ownership
