"""Dedicated Chat Character Card contract.

Chat cards remain ordinary SillyTavern V2 character cards.  Their AetherState
metadata carries only an immutable Character Core and optional World seed; it
must never reuse the RPG Narrator/Player envelope.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json

import pytest


CORE = {
    "schema": "aetherstate-character-core/1",
    "revision": 1,
    "name": "Mara",
    "description": "A night-shift paramedic with a dry sense of humor.",
    "personality": "Direct, observant, private, and protective.",
    "scenario": "Mara and {{user}} meet after her shift.",
    "first_message": "You are still awake, {{user}}?",
    "example_dialogue": "Mara: I noticed. I just did not want to corner you.",
    "anchors": ["Does not pretend to know facts she has not learned."],
    "boundaries": ["Never speaks or acts for the Persona."],
}

WORLD = {
    "name": "Rainline",
    "genre": "modern_drama",
    "setting": "A rain-soaked coastal city where night shifts overlap.",
}

STARTING_CONTINUITY = {
    "schema": "aetherstate-chat-starting-continuity/1",
    "memories": [{
        "memory_id": "memory.first-shift",
        "text": "Mara remembers the Persona waiting up after her first night shift.",
        "visibility": "shared",
    }],
    "player_visible_possessions_conditions": [{
        "record_id": "condition.sprained-wrist",
        "kind": "condition",
        "summary": "Mara's left wrist is lightly sprained.",
    }],
    "character_knowledge": [{
        "knowledge_id": "knowledge.favorite-tea",
        "statement": "Mara knows the Persona prefers jasmine tea.",
        "visibility": "character_private",
    }],
    "relationship_causes": [{
        "cause_id": "cause.waited-up",
        "from": {"role": "character"},
        "to": {"role": "current_persona"},
        "dimension": "trust",
        "quality": "gain",
        "reason": "The Persona waited up for Mara after a difficult shift.",
        "cause_ref": {
            "kind": "creator",
            "fingerprint": "sha256:" + "3" * 64,
        },
    }],
    "agreement_revisions": [{
        "schema": "aetherstate-relationship-agreement/1",
        "agreement_id": "agreement.starting",
        "revision": 1,
        "action": "create",
        "parties": [{"role": "character"}, {"role": "current_persona"}],
        "exclusivity": "exclusive",
        "allowed_outside_acts": [],
        "requires_disclosure": False,
        "disclosure_deadline": None,
        "effective_turn": 0,
        "assent": [],
    }],
    "open_threads": [{
        "schema": "aetherstate-continuity-thread-transition/1",
        "thread_id": "thread.weekend-plan",
        "revision": 1,
        "action": "create",
        "kind": "plan",
        "summary": "They still need to choose where to go this weekend.",
        "participants": [{"role": "character"}, {"role": "current_persona"}],
        "status": "open",
    }],
}


def _chat_card():
    return importlib.import_module("aetherstate.chat_card")


def _extract_chara(png: bytes) -> dict:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    while offset < len(png):
        length = int.from_bytes(png[offset:offset + 4], "big")
        kind = png[offset + 4:offset + 8]
        data = png[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"tEXt":
            keyword, _, value = data.partition(b"\x00")
            if keyword == b"chara":
                return json.loads(base64.b64decode(value).decode("utf-8"))
    raise AssertionError("no embedded V2 character card")


def test_chat_card_preserves_normal_v2_fields_and_has_only_chat_metadata():
    chat_card = _chat_card()
    card = chat_card.build_card(CORE)
    data = card["data"]

    assert card == {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    assert data["name"] == CORE["name"]
    assert data["description"] == CORE["description"]
    assert data["personality"] == CORE["personality"]
    assert data["scenario"] == CORE["scenario"]
    assert data["first_mes"] == CORE["first_message"]
    assert data["mes_example"] == CORE["example_dialogue"]

    metadata = data["extensions"]["aetherstate"]
    assert metadata["role"] == "character"
    assert metadata["mode"] == "chat"
    assert metadata["core"] == CORE
    assert metadata["core_fingerprint"] == chat_card.core_fingerprint(CORE)
    assert metadata["core_envelope_fingerprint"] == chat_card.chat_envelope_fingerprint(
        CORE, None,
    )
    assert metadata["seed"] == {}

    encoded = json.dumps(card, sort_keys=True)
    assert "NARRATOR" not in encoded
    assert "[PLAYER]" not in encoded
    assert "player_seed" not in encoded
    assert "aether-world-" not in encoded
    assert data["system_prompt"] == ""
    assert data["post_history_instructions"] == ""
    assert "rpg" not in data["tags"]


def test_chat_card_omits_world_cleanly_and_admits_optional_world_atomically():
    chat_card = _chat_card()
    worldless = chat_card.build_card(CORE)["data"]["extensions"]["aetherstate"]
    with_world = chat_card.build_card(CORE, world=WORLD)["data"]["extensions"]["aetherstate"]

    assert "world" not in worldless["seed"]
    assert with_world["seed"] == {"world": WORLD}
    assert (
        with_world["core_envelope_fingerprint"]
        == chat_card.chat_envelope_fingerprint(CORE, WORLD)
    )
    assert (
        with_world["core_envelope_fingerprint"]
        != worldless["core_envelope_fingerprint"]
    )


def test_core_fingerprint_is_exact_canonical_and_changes_with_one_core_field():
    chat_card = _chat_card()
    reordered = {key: CORE[key] for key in reversed(CORE)}
    changed = {**CORE, "personality": CORE["personality"] + " Quietly stubborn."}

    assert chat_card.core_fingerprint(CORE) == chat_card.core_fingerprint(reordered)
    assert chat_card.core_fingerprint(CORE) != chat_card.core_fingerprint(changed)
    assert chat_card.core_fingerprint(CORE).startswith("sha256:")


def test_malformed_coded_core_falls_back_atomically_to_ordinary_fields():
    chat_card = _chat_card()
    ordinary = {
        "name": "Ordinary Mara",
        "description": "Ordinary description.",
        "personality": "Ordinary personality.",
        "scenario": "Ordinary scenario.",
        "first_mes": "Ordinary greeting.",
        "mes_example": "Ordinary example.",
    }
    malformed = {
        **CORE,
        "name": "Coded Mara",
        "anchors": "not-a-list",
    }

    fallback = chat_card.ordinary_core(ordinary)
    with pytest.raises(ValueError):
        chat_card.validate_core(malformed)
    assert fallback["name"] == "Ordinary Mara"
    assert fallback["anchors"] == []
    assert fallback["boundaries"] == []


def test_private_creator_direction_never_enters_portable_chat_card():
    chat_card = _chat_card()
    private = "PRIVATE CREATOR DIRECTION MUST NOT ENTER THE CARD"
    authored = {**CORE, "notes": private, "creative_direction": private}
    card = chat_card.build_card(authored)

    assert private not in json.dumps(card)
    assert "notes" not in card["data"]["extensions"]["aetherstate"]["core"]
    assert "creative_direction" not in card["data"]["extensions"]["aetherstate"]["core"]


def test_chat_card_json_and_png_inspection_return_ordinary_and_validated_metadata():
    chat_card = _chat_card()
    from aetherstate import narrator

    card = chat_card.build_card(CORE, world=WORLD)
    json_back = chat_card.inspect_card_json_or_png(
        json.dumps(card).encode("utf-8"), "application/json",
    )
    png = narrator.card_png(card, WORLD)
    png_back = chat_card.inspect_card_json_or_png(png, "image/png")

    assert _extract_chara(png) == card
    assert json_back == png_back
    assert json_back["name"] == CORE["name"]
    assert json_back["first_mes"] == CORE["first_message"]
    assert json_back["extensions"]["aetherstate"]["core"] == CORE


@pytest.mark.parametrize("content_type,payload", [
    ("application/json", b"[]"),
    ("image/png", b"not a png"),
])
def test_chat_card_inspection_rejects_malformed_or_non_card_payloads(content_type, payload):
    chat_card = _chat_card()
    with pytest.raises(ValueError):
        chat_card.inspect_card_json_or_png(payload, content_type)


def test_chat_card_validates_portable_starting_continuity_without_changing_core_envelope():
    chat_card = _chat_card()
    baseline = chat_card.build_card(CORE, world=WORLD)
    card = chat_card.build_card(CORE, world=WORLD, continuity=STARTING_CONTINUITY)
    metadata = card["data"]["extensions"]["aetherstate"]

    assert metadata["core_fingerprint"] == baseline["data"]["extensions"]["aetherstate"][
        "core_fingerprint"
    ]
    assert metadata["core_envelope_fingerprint"] == baseline["data"]["extensions"]["aetherstate"][
        "core_envelope_fingerprint"
    ]
    continuity = metadata["seed"]["continuity"]
    assert continuity["schema"] == "aetherstate-chat-starting-continuity/1"
    for family in (
        "memories",
        "player_visible_possessions_conditions",
        "character_knowledge",
        "relationship_causes",
        "agreement_revisions",
        "open_threads",
    ):
        assert continuity[family]
        assert all(
            row["record_fingerprint"].startswith("sha256:")
            for row in continuity[family]
        )
    assert "chat_continuity_seed_receipts" not in metadata
    assert "character:" not in json.dumps(continuity)
    assert "persona:" not in json.dumps(continuity)

    inspected = chat_card.inspect_card_json_or_png(
        json.dumps(card).encode("utf-8"), "application/json",
    )
    assert inspected["extensions"]["aetherstate"]["seed"]["continuity"] == continuity

    forged = copy.deepcopy(STARTING_CONTINUITY)
    forged["agreement_revisions"][0]["parties"][1] = {
        "kind": "category",
        "label": "men",
    }
    with pytest.raises(ValueError, match="placeholder"):
        chat_card.build_card(CORE, continuity=forged)


def _portable_seed_with_two_lineages(chat_card):
    from aetherstate.chat_continuity import validate_starting_continuity

    seed = copy.deepcopy(STARTING_CONTINUITY)
    first = validate_starting_continuity(seed)
    agreement = {
        **copy.deepcopy(seed["agreement_revisions"][0]),
        "revision": 2,
        "action": "amend",
        "supersedes_fingerprint": first["agreement_revisions"][0]["record_fingerprint"],
        "exclusivity": "open",
        "allowed_outside_acts": ["kissing"],
    }
    thread = {
        **copy.deepcopy(seed["open_threads"][0]),
        "revision": 2,
        "action": "update",
        "supersedes_fingerprint": first["open_threads"][0]["record_fingerprint"],
        "summary": "They narrowed the weekend plan to two choices.",
    }
    seed["agreement_revisions"].append(agreement)
    seed["open_threads"].append(thread)
    return seed


def _recognized_evidence(kind, text):
    canonical = lambda value: json.dumps(  # noqa: E731 - local exact fingerprint helper
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    row = {
        "schema": "aetherstate-recognized-evidence/1",
        "kind": kind,
        "message_fingerprint": "sha256:" + hashlib.sha256(
            b"portable-message\0" + text.encode("utf-8"),
        ).hexdigest(),
        "start": 0,
        "end": len(text),
        "accepted": True,
        "code_sealed": True,
    }
    row["fingerprint"] = "sha256:" + hashlib.sha256(
        b"aetherstate-recognized-evidence/1\0" + canonical(row),
    ).hexdigest()
    return row


def test_portable_starting_continuity_accepts_exact_sequential_lineages():
    chat_card = _chat_card()
    from aetherstate.chat_continuity import validate_starting_continuity

    validated = validate_starting_continuity(
        _portable_seed_with_two_lineages(chat_card),
    )

    assert [row["revision"] for row in validated["agreement_revisions"]] == [1, 2]
    assert [row["revision"] for row in validated["open_threads"]] == [1, 2]
    assert (
        validated["agreement_revisions"][1]["supersedes_fingerprint"]
        == validated["agreement_revisions"][0]["record_fingerprint"]
    )
    assert (
        validated["open_threads"][1]["supersedes_fingerprint"]
        == validated["open_threads"][0]["record_fingerprint"]
    )


def test_portable_agreement_transition_normalizes_acting_party_role_placeholder():
    from aetherstate.chat_continuity import validate_starting_continuity

    seed = _portable_seed_with_two_lineages(_chat_card())
    admitted = validate_starting_continuity(seed)
    transition = {
        **copy.deepcopy(seed["agreement_revisions"][-1]),
        "revision": 3,
        "action": "withdraw",
        "supersedes_fingerprint": admitted["agreement_revisions"][-1][
            "record_fingerprint"
        ],
        "acting_party": {"role": "character"},
        "evidence": _recognized_evidence(
            "agreement_transition",
            "The Character explicitly withdraws the agreement.",
        ),
    }
    seed["agreement_revisions"].append(transition)

    validated = validate_starting_continuity(seed)

    assert validated["agreement_revisions"][-1]["acting_party"] == {
        "role": "character",
    }
    card = _chat_card().build_card(CORE, continuity=seed)
    assert card["data"]["extensions"]["aetherstate"]["seed"]["continuity"][
        "agreement_revisions"
    ][-1]["acting_party"] == {"role": "character"}


@pytest.mark.parametrize(
    ("family", "mutation"),
    [
        ("agreement_revisions", "duplicate"),
        ("agreement_revisions", "gap"),
        ("agreement_revisions", "bad_predecessor"),
        ("agreement_revisions", "forged_record_fingerprint"),
        ("open_threads", "duplicate"),
        ("open_threads", "gap"),
        ("open_threads", "bad_predecessor"),
        ("open_threads", "forged_record_fingerprint"),
    ],
)
def test_portable_starting_continuity_rejects_broken_list_lineage(
    family,
    mutation,
):
    chat_card = _chat_card()
    from aetherstate.chat_continuity import validate_starting_continuity

    seed = _portable_seed_with_two_lineages(chat_card)
    rows = seed[family]
    if mutation == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif mutation == "gap":
        rows[1]["revision"] = 3
    elif mutation == "bad_predecessor":
        rows[1]["supersedes_fingerprint"] = "sha256:" + "f" * 64
    else:
        rows[0]["record_fingerprint"] = "sha256:" + "f" * 64

    with pytest.raises(ValueError):
        validate_starting_continuity(seed)


async def test_chat_character_author_route_builds_worldless_card_and_roundtrips_import(
    client,
    mock_upstream,
    cfg,
):
    from tests.mock_upstream import Reply

    authored = {
        **CORE,
        "name": "Model Mara",
        "description": "A complete model-authored description.",
    }
    body = json.dumps({
        "choices": [{
            "message": {"content": json.dumps(authored)},
            "finish_reason": "stop",
        }],
    }).encode()
    cfg.upstream.model = "chat-main"
    mock_upstream.enqueue(Reply(body=body))

    author = await client.post(
        "/aether/session/chat-creator-author/author",
        json={
            "mode": "chat_character",
            "doc": {**CORE, "name": "Player Mara", "description": ""},
        },
    )
    assert author.status_code == 200
    result = author.json()
    assert result["source"] == "llm"
    assert result["mode"] == "chat_character"
    assert result["doc"]["name"] == "Player Mara"
    assert result["doc"]["description"] == authored["description"]
    assert result["doc"] == _chat_card().validate_core(result["doc"])
    assert not {
        "player", "stats", "skills", "abilities", "resources", "gear", "world_id",
    }.intersection(result["doc"])

    built = await client.post("/aether/chat-card", json={"core": result["doc"]})
    assert built.status_code == 200
    payload = built.json()
    assert payload["world_fingerprint"] == ""
    assert payload["metadata"]["seed"] == {}

    inspected = await client.post("/aether/chat-card/inspect", json={
        "filename": "Player-Mara.png",
        "data_b64": payload["png_b64"],
    })
    assert inspected.status_code == 200
    assert inspected.json()["extensions"]["aetherstate"]["core"] == result["doc"]


async def test_worldless_chat_creator_prefill_and_session_projection_use_core_identity(client):
    from aetherstate.chat_continuity import validate_starting_continuity

    ordinary = {
        "name": CORE["name"],
        "description": CORE["description"],
        "personality": CORE["personality"],
        "scenario": CORE["scenario"],
        "first_mes": CORE["first_message"],
        "mes_example": CORE["example_dialogue"],
    }
    admitted = await client.post(
        "/aether/session/chat-creator-prefill/chat-core",
        json={
            "card": ordinary,
            "persona": "Bean-Persona.png",
            "continuity": STARTING_CONTINUITY,
        },
    )
    assert admitted.status_code == 200, admitted.text
    identity = admitted.json()

    prefill = await client.get("/aether/session/chat-creator-prefill/creator")
    assert prefill.status_code == 200
    document = prefill.json()
    assert document["experience_mode"] == "chat"
    assert document["core"] == _chat_card().ordinary_core(ordinary)
    assert document["player"] is None
    assert document["world"] is None
    assert document["world_seeded"] is False
    assert document["continuity"] == validate_starting_continuity(STARTING_CONTINUITY)
    assert document["continuity_results"]
    assert all(row["accepted"] for row in document["continuity_results"])

    projected = await client.get("/aether/sessions")
    assert projected.status_code == 200
    session = next(
        row for row in projected.json()["sessions"]
        if row["external_id"] == "chat-creator-prefill"
    )
    assert session["experience_mode"] == "chat"
    assert session["player_name"] == CORE["name"]
    assert session["world_name"] == ""
    assert session["card_revision"] == identity["core_fingerprint"][7:23]


async def test_bound_chat_creator_save_admits_starting_continuity_and_prefills_status(client):
    from aetherstate.chat_continuity import validate_starting_continuity

    ordinary = {
        "name": CORE["name"],
        "description": CORE["description"],
        "personality": CORE["personality"],
        "scenario": CORE["scenario"],
        "first_mes": CORE["first_message"],
        "mes_example": CORE["example_dialogue"],
    }
    initial = await client.post(
        "/aether/session/chat-creator-bound-save/chat-core",
        json={"card": ordinary, "persona": "Bean-Persona.png"},
    )
    assert initial.status_code == 200, initial.text
    core = _chat_card().ordinary_core(ordinary)

    saved = await client.post(
        "/aether/session/chat-creator-bound-save/chat-core",
        json={"bound": True, "core": core, "continuity": STARTING_CONTINUITY},
    )
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["complete"] is True
    assert result["applied"] == 6
    assert len(result["continuity_results"]) == 6
    assert all(row["accepted"] for row in result["continuity_results"])
    assert all(row["already_present"] is False for row in result["continuity_results"])

    prefill = await client.get("/aether/session/chat-creator-bound-save/creator")
    assert prefill.status_code == 200
    document = prefill.json()
    assert document["core"] == core
    assert document["continuity"] == validate_starting_continuity(STARTING_CONTINUITY)
    assert len(document["continuity_results"]) == 6
    assert all(row["accepted"] for row in document["continuity_results"])
