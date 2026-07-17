"""Typed entity-patient roles for possession and body-locus constructions."""
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
    cfg.specialization.combat_opening_primer = True
    return cfg


def _state(*, active: bool = True) -> dict:
    state = empty_state()
    state["entities"] = {
        "mage": {"kind": "player", "name": "Mage", "present": True},
        "ana": {"kind": "npc", "name": "Ana", "present": True},
        "bo": {"kind": "npc", "name": "Bo", "present": True},
    }
    state["player"] = {"mage": {
        "eid": "mage",
        "stats": {"STR": 16},
        "skills": {"brawl": 3},
        "abilities": [],
        "resources": {},
        "defs": {"skills": {
            "brawl": {
                "name": "Brawl",
                "keyed_stat": "STR",
                "governs": ["strike"],
            },
        }},
    }}
    state["combat"] = {
        "active": active,
        "combatants": ({
            entity_id: {
                "id": entity_id,
                "eid": entity_id,
                "name": name,
                "side": "enemy",
                "defeated": False,
                "hp": {"cur": 14, "max": 14},
            }
            for entity_id, name in (("ana", "Ana"), ("bo", "Bo"))
        } if active else {}),
    }
    return state


def _run(text: str):
    return tier0.run(
        {"messages": [{"role": "user", "content": text}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(7),
        turn=4,
    )


def _frame(result) -> dict:
    frames = [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
    ]
    assert len(frames) == 1
    assert result.semantic_turn is not None
    assert result.semantic_turn.occurrence_graph is not None
    assert len(result.semantic_turn.occurrence_graph["occurrences"]) == 1
    return frames[0]


def _occurrence_targets(result) -> list[str]:
    graph = result.semantic_turn.occurrence_graph
    return [
        target["identity"]
        for occurrence in graph["occurrences"]
        for target in occurrence["targets"]
    ]


def _hp_targets(result) -> list[str]:
    return [
        str(member["target"])
        for settlement in result.rule_ops
        if settlement.get("op") == "mechanic_settlement_commit"
        for member in settlement.get("members") or ()
        if member.get("op") == "combatant_hp"
    ]


@pytest.mark.parametrize(
    ("text", "possessed_object", "object_part"),
    [
        ("I strike Ana's bell.", "bell", ""),
        ("I strike Ana’s bell.", "bell", ""),
        ("I strike Ana's sword hilt.", "sword", "hilt"),
        ("I strike the bell of Ana.", "", ""),
        ("I strike at the bell of Ana.", "", ""),
    ],
)
def test_owned_object_possessor_is_never_lent_as_the_hp_patient(
    text: str,
    possessed_object: str,
    object_part: str,
):
    result = _run(text)
    frame = _frame(result)

    assert frame["target_entity_id"] is None
    assert frame["target_name"] is None
    assert frame["possessed_object"] == possessed_object
    assert frame["possessed_object_part"] == object_part
    if possessed_object:
        assert frame["linguistic_possessor_id"] == "ana"
        assert not any(row["kind"] == "target" for row in frame["evidence"])
        assert any(
            row["kind"] == "linguistic_possessor" and row.get("value") == "ana"
            for row in frame["evidence"]
        )
    assert "occurrence.target_unbound" in frame["ambiguity"]
    assert _occurrence_targets(result) == []
    assert _hp_targets(result) == []


@pytest.mark.parametrize(
    ("text", "locus"),
    [
        ("I strike Ana's ribs.", "ribs"),
        ("I strike Ana’s ribs.", "ribs"),
        ("I strike Ana's sword arm.", "arm"),
        ("I strike the ribs of Ana.", "ribs"),
        ("I strike at the ribs of Ana.", "ribs"),
    ],
)
def test_referentlex_body_locus_may_bind_its_named_whole_as_the_patient(
    text: str,
    locus: str,
):
    result = _run(text)
    frame = _frame(result)

    assert frame["target_entity_id"] == "ana"
    assert frame["target_name"] == "Ana"
    assert frame["target_locus"] == locus
    assert frame["target_locus_owner_id"] == "ana"
    assert frame["possessed_object"] == ""
    assert frame["ambiguity"] == []
    assert _occurrence_targets(result) == ["ana"]
    assert result.semantic_turn.occurrence_graph["occurrences"][0]["targets"][0][
        "source"
    ] == "body_locus_owner"
    assert _hp_targets(result) == ["ana"]


@pytest.mark.parametrize(
    "text",
    [
        "I strike her bell.",
        "I strike her ribs.",
    ],
)
def test_possessive_pronoun_without_an_exact_local_antecedent_abstains(text: str):
    result = _run(text)
    frame = _frame(result)

    assert frame["target_entity_id"] is None
    assert frame["target_locus_owner_id"] is None
    assert "occurrence.target_unbound" in frame["ambiguity"]
    assert _occurrence_targets(result) == []
    assert _hp_targets(result) == []


@pytest.mark.parametrize(
    ("text", "locus"),
    [
        ("I strike Ana.", ""),
        ("I strike Ana in her ribs.", "ribs"),
        ("I strike Ana with her bell.", ""),
    ],
)
def test_explicit_entity_patient_remains_exact_with_pronominal_adjuncts(
    text: str,
    locus: str,
):
    result = _run(text)
    frame = _frame(result)

    assert frame["target_entity_id"] == "ana"
    assert frame["target_locus"] == locus
    assert frame["target_locus_owner_id"] == ("ana" if locus else None)
    assert _occurrence_targets(result) == ["ana"]
    assert _hp_targets(result) == ["ana"]


def test_cross_owner_instrument_cannot_replace_the_explicit_patient():
    result = _run("I strike Bo with Ana's sword.")
    frame = _frame(result)

    assert frame["target_entity_id"] == "bo"
    assert frame["target_name"] == "Bo"
    assert frame["possessed_object"] == "sword"
    assert frame["linguistic_possessor_id"] == "ana"
    assert frame["ambiguity"] == []
    assert any(
        row["kind"] == "target" and row.get("value") == "bo"
        for row in frame["evidence"]
    )
    assert any(
        row["kind"] == "linguistic_possessor" and row.get("value") == "ana"
        for row in frame["evidence"]
    )
    assert _occurrence_targets(result) == ["bo"]
    assert _hp_targets(result) == ["bo"]


def test_exact_local_possessor_may_anchor_a_later_pronominal_body_locus():
    result = _run("I strike past Ana's bell and into her ribs.")
    frame = _frame(result)

    assert frame["target_entity_id"] == "ana"
    assert frame["target_locus"] == "ribs"
    assert frame["target_locus_owner_id"] == "ana"
    assert frame["possessed_object"] == "bell"
    assert frame["linguistic_possessor_id"] == "ana"
    assert frame["ambiguity"] == []
    assert _occurrence_targets(result) == ["ana"]
    assert result.semantic_turn.occurrence_graph["occurrences"][0]["targets"][0][
        "source"
    ] == "pronominal_body_locus_owner"
    assert _hp_targets(result) == ["ana"]


@pytest.mark.parametrize(
    "text",
    [
        "I strike past Ana's bell; I strike her ribs.",
        "I strike past Ana's bell. I strike her ribs.",
        "I strike past Ana's bell and I strike her ribs.",
    ],
)
def test_pronominal_body_locus_never_borrows_a_possessor_across_occurrences(text: str):
    result = _run(text)
    frames = [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
    ]

    assert len(frames) == 2
    assert all(frame["target_entity_id"] is None for frame in frames)
    assert all(
        target == []
        for target in (
            occurrence["targets"]
            for occurrence in result.semantic_turn.occurrence_graph["occurrences"]
        )
    )
    assert _hp_targets(result) == []


def test_explicit_patient_owns_pronominal_locus_even_with_a_different_instrument_owner():
    result = _run("I strike Bo with Ana's sword into his ribs.")
    frame = _frame(result)

    assert frame["target_entity_id"] == "bo"
    assert frame["target_locus"] == "ribs"
    assert frame["target_locus_owner_id"] == "bo"
    assert frame["linguistic_possessor_id"] == "ana"
    assert _occurrence_targets(result) == ["bo"]
    assert _hp_targets(result) == ["bo"]


def test_combat_opening_uses_the_same_typed_patient_contract():
    state = _state(active=False)

    def target(text: str):
        return tier0.combat_opening_target(
            {"messages": [{"role": "user", "content": text}]},
            state,
            _cfg(),
            "new_turn",
        )

    assert target("I strike Ana's bell.") is None
    assert target("I strike the bell of Ana.") is None
    assert target("I strike at the bell of Ana.") is None
    assert target("I strike Ana's ribs.") == ("ana", "Ana")
    assert target("I strike the ribs of Ana.") == ("ana", "Ana")
    assert target("I strike at the ribs of Ana.") == ("ana", "Ana")
    assert target("I strike Ana.") == ("ana", "Ana")
    assert target("I strike Bo with Ana's sword.") == ("bo", "Bo")


def test_missing_referentlex_fails_possession_closed_without_losing_direct_target(
    monkeypatch: pytest.MonkeyPatch,
):
    state = _state(active=False)

    def unavailable(_genre_ids):
        raise OSError("referent lex unavailable")

    monkeypatch.setattr(tier0, "_semantic_grammar", unavailable)

    def target(text: str):
        return tier0.combat_opening_target(
            {"messages": [{"role": "user", "content": text}]},
            state,
            _cfg(),
            "new_turn",
        )

    assert target("I strike Ana's bell.") is None
    assert target("I strike Ana's ribs.") is None
    assert target("I strike the ribs of Ana.") is None
    assert target("I strike Ana.") == ("ana", "Ana")
