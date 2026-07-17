"""Committed Creator Player documents remain complete when regenerated as a Narrator card."""
from __future__ import annotations

from aetherstate import creator
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


_APPEARANCE = (
    "A patient mediator in a charcoal coat, with steady grey eyes and ink-stained fingers."
)
_WITNESS_EFFECT = "Reveals forged testimony with a cold blue glimmer."
_LENS_EFFECT = "Makes erased harbor script briefly visible in reflected lamplight."
_EXTRAS = [{
    "label": "Testimony Method",
    "text": "Kael never treats a report as fact until its source and evidence are recorded.",
}]


def _full_player() -> dict:
    return {
        "name": "Kael Vey",
        "sex": "male",
        "pronouns": "he/him",
        "species": "human",
        "appearance": _APPEARANCE,
        "concept": "Harbor witness and mediator",
        "level": 4,
        "stats": {"STR": 9, "DEX": 10, "INT": 12, "CHA": 15, "CUN": 13, "CON": 11},
        "skills": {"persuasion": 3, "perception": 2, "witness_reading": 2},
        "abilities": ["keen_senses", "seal_attunement"],
        "defs": {
            "skills": [{
                "id": "witness_reading",
                "name": "Witness Reading",
                "keyed_stat": "CUN",
                "base_mod": 1,
                "max_rank": 5,
                "governs": ["compare", "question", "verify"],
                "desc": "Separates direct testimony from inference and repeated rumor.",
                "group": "Civic Disciplines",
                "cost": {"resolve": 2},
            }],
            "abilities": [{
                "id": "seal_attunement",
                "name": "Seal Attunement",
                "kind": "passive",
                "mechanic": "ward",
                "applies_to": "witness_reading",
                "magnitude": 1,
                "group": "Civic Disciplines",
                "effect": "The witness-seal steadies Kael when testimony conflicts.",
                "desc": "A trained bond with Claimfall Harbor's civic witness seals.",
            }],
        },
        "resources": {
            "hp": {"cur": 26, "max": 34},
            "resolve": {
                "name": "Resolve",
                "cur": 4,
                "max": 9,
                "color": "#4455aa",
            },
        },
        "gear": [
            {
                "name": "Brass Witness-Seal",
                "slot": "accessory1",
                "effect": _WITNESS_EFFECT,
            },
            "sealed testimony forms",
            "weatherproof satchel",
        ],
        "extras": list(_EXTRAS),
    }


def _hp(document: dict) -> dict:
    direct = document.get("hp")
    if isinstance(direct, dict):
        return direct
    return (document.get("resources") or {}).get("hp") or {}


def _gear_entry(document: dict, name: str):
    for entry in document.get("gear") or []:
        if isinstance(entry, str) and entry == name:
            return entry
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _assert_complete_player(document: dict) -> None:
    expected = _full_player()
    assert document["name"] == expected["name"]
    assert document["sex"] == expected["sex"]
    assert document["species"] == expected["species"]
    assert document["appearance"] == expected["appearance"]
    assert document["concept"] == expected["concept"]
    assert document["pronouns"] == expected["pronouns"]
    assert document["level"] == expected["level"]
    assert document["stats"] == expected["stats"]
    assert document["skills"]["persuasion"] == 3
    assert document["skills"]["perception"] == 2
    assert document["skills"]["witness_reading"] == 2
    assert {"keen_senses", "seal_attunement"} <= set(document["abilities"])
    assert document["defs"]["skills"]["witness_reading"]["cost"] == {"resolve": 2}
    assert document["defs"]["abilities"]["seal_attunement"]["mechanic"] == "ward"
    assert _hp(document) == {"cur": 26, "max": 34}
    assert document["resources"]["resolve"] == {
        "cur": 4,
        "max": 9,
        "name": "Resolve",
        "color": "#4455aa",
    }
    assert _gear_entry(document, "Brass Witness-Seal") == {
        "name": "Brass Witness-Seal",
        "slot": "accessory1",
        "effect": _WITNESS_EFFECT,
    }
    assert _gear_entry(document, "sealed testimony forms") == "sealed testimony forms"
    assert document["extras"] == _EXTRAS
    assert not {
        "eid",
        "xp",
        "cooldowns",
        "soulmate",
        "nemesis",
        "_resource_cost_policy",
    } & set(document)


def test_long_structured_gear_effect_survives_complete_store_roundtrip():
    effect = (
        "When Kael records testimony, the witness-seal preserves the exact speaker, wording, "
        "sequence, and declared source without promoting the report into fact. "
        + (
            "Each etched line keeps its original qualifiers, doubts, omissions, and corrections "
            "visible to later readers. "
        ) * 12
        + "The final mark glows blue only when a later copy changes that preserved record."
    )
    assert 240 < len(effect) < 4000
    player = _full_player()
    player["gear"] = [{
        "name": "Longform Brass Witness-Seal",
        "slot": "accessory1",
        "effect": effect,
    }]
    cfg = Config()

    normalized = creator.deterministic_player(player, cfg)
    assert _gear_entry(normalized, "Longform Brass Witness-Seal")["effect"] == effect
    ops = creator.player_to_ops(normalized, cfg)
    gain = next(
        op for op in ops
        if op.get("op") == "item_gain" and op.get("name") == "Longform Brass Witness-Seal"
    )
    assert gain["aura"] == effect

    store = Store(":memory:")
    sid, branch = store.create_session(external_id="creator-long-gear-effect")
    apply_delta(store, sid, branch, 0, ops, "user", cfg)
    state = current_state(store, branch)
    item = next(
        row for row in state["items"].values()
        if row.get("name") == "Longform Brass Witness-Seal"
    )
    assert item["aura"] == effect
    regenerated = creator.player_from_state(state)
    assert _gear_entry(regenerated, "Longform Brass Witness-Seal") == {
        "name": "Longform Brass Witness-Seal",
        "slot": "accessory1",
        "effect": effect,
    }


async def test_committed_narrator_card_seed_preserves_complete_player(client):
    sid = "creator-player-card-roundtrip"
    saved = await client.post(
        f"/aether/session/{sid}/player",
        json={"player": _full_player()},
    )
    assert saved.status_code == 200 and saved.json()["applied"] > 0

    response = await client.get(f"/aether/session/{sid}/narrator-card.json")
    assert response.status_code == 200
    seed = response.json()["data"]["extensions"]["aetherstate"]["seed"]
    _assert_complete_player(seed["player"])


def test_regenerated_player_uses_current_gear_and_reseeds_typed_state():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="creator-current-gear")
    apply_delta(store, sid, branch, 0, creator.player_to_ops(_full_player(), cfg), "user", cfg)
    apply_delta(store, sid, branch, 1, [{
        "op": "item_lose",
        "char": "Kael Vey",
        "name": "weatherproof satchel",
    }], "user", cfg)
    apply_delta(store, sid, branch, 2, [{
        "op": "item_gain",
        "char": "Kael Vey",
        "name": "Saltglass Monocle",
        "slot": "face",
        "aura": _LENS_EFFECT,
    }], "user", cfg)

    regenerated = creator.player_from_state(current_state(store, branch))
    names = {
        entry if isinstance(entry, str) else entry.get("name")
        for entry in regenerated.get("gear") or []
    }
    assert "weatherproof satchel" not in names
    assert {"Brass Witness-Seal", "sealed testimony forms", "Saltglass Monocle"} <= names
    assert _gear_entry(regenerated, "Saltglass Monocle") == {
        "name": "Saltglass Monocle",
        "slot": "face",
        "effect": _LENS_EFFECT,
    }

    restored_store = Store(":memory:")
    restored_sid, restored_branch = restored_store.create_session(
        external_id="creator-current-gear-restored"
    )
    apply_delta(
        restored_store,
        restored_sid,
        restored_branch,
        0,
        creator.player_to_ops(regenerated, cfg),
        "user",
        cfg,
    )
    restored = current_state(restored_store, restored_branch)
    player_id = next(iter(restored["player"]))
    assert restored["attributes"][player_id]["sex"] == "male"
    assert restored["attributes"][player_id]["species"] == "human"
    assert restored["attributes"][player_id]["appearance"] == _APPEARANCE
    assert restored["player"][player_id]["creator_extras"] == _EXTRAS
    assert "weatherproof satchel" not in {
        item.get("name")
        for item in restored["items"].values()
        if item.get("owner") == player_id and item.get("loc") != "gone"
    }
    lens = next(
        item
        for item in restored["items"].values()
        if item.get("owner") == player_id and item.get("name") == "Saltglass Monocle"
    )
    assert lens["loc"] == "gear:face"
    assert lens["slot"] == "face"
    assert lens["aura"] == _LENS_EFFECT
    assert creator.player_from_state(restored)["extras"] == _EXTRAS
