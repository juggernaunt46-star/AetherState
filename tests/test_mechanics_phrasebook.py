"""Mechanics Phrasebook proof: local constructions generalize to held-out prose without an LLM.

The templates are the training library. These sentences are separate inflected/filled variants,
plus negative contrasts. The benchmark compares the current detector with the Phrasebook disabled
against the same detector with the supporting rung enabled.
"""
from __future__ import annotations

from aetherstate import tier0
from aetherstate.phrasebook import load, match
from tests.test_intent_floor import _checks, _combat_state, _rpg_cfg, _run, _spawns


def _state():
    state = _combat_state()
    state["entities"]["halvic_orne"] = {
        "kind": "npc", "name": "Halvic Orne", "present": True, "aliases": ["Halvic"],
    }
    return state


def _observed(message: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = _run(message, _rpg_cfg(True), _state(), assistant="Maren and Halvic watch closely.")
    skills = tuple(row["skill"] for row in _checks(result))
    spawns = tuple(row["name"] for row in _spawns(result))
    return skills, spawns


HELD_OUT = [
    ("I pressed Halvic for another five silver.", (("persuasion",), ())),
    ("I am leaning on Halvic for a better rate.", (("persuasion",), ())),
    ("I keep pushing Halvic for hazard pay.", (("persuasion",), ())),
    ("I am holding out for twice the offered pay.", (("persuasion",), ())),
    ("I am sinking my knife into Maren.", (("swordplay",), ("Maren",))),
    ("I am driving my rapier into Maren.", (("swordplay",), ("Maren",))),
    ("I bury my chipped blade in Maren.", (("swordplay",), ("Maren",))),
    ("I put my sword through Maren.", (("swordplay",), ("Maren",))),
    ("I slash at Maren's neck.", (("swordplay",), ("Maren",))),
    ("I am slashing across Maren's ribs.", (("swordplay",), ("Maren",))),
    ("I am wrestling Maren to the ground.", (("athletics",), ("Maren",))),
    ("I take Maren to the ground.", (("athletics",), ("Maren",))),
    ("I throw Maren to the ground.", (("athletics",), ("Maren",))),
]

NEGATIVE_CONTRASTS = [
    "Who pressed Halvic for more coin?",
    '"Press Halvic for more coin," I say.',
    "If I pressed Halvic for more coin, would he listen?",
    "I do not press Halvic for more coin.",
    "Maren presses Halvic for more coin.",
    "I press the seal into warm wax.",
    "I put the report through the mail slot.",
    "I sink into the chair.",
    "I slash prices for the winter sale.",
    "I hold the line.",
    "Maren takes Halvic to the ground.",
]


def test_phrasebook_loads_parameterized_local_constructions():
    rows = load()
    assert {row.id for row in rows} == {
        "bargain.press_for_terms", "weapon.penetrating_attack", "weapon.slashing_attack",
        "grapple.takedown",
    }
    slots = {
        "person": {"halvic": "Halvic Orne", "maren": "Maren"},
        "weapon": {"knife": "knife", "rapier": "rapier"},
    }
    found = match("I am sinking my knife into Maren.", slots)
    assert len(found) == 1
    assert found[0].skill == "swordplay" and found[0].target == "Maren"
    assert found[0].instrument == "knife" and found[0].attack


def test_held_out_phrasebook_benchmark_improves_without_negative_regression(monkeypatch):
    real_match = tier0.match_phrasebook
    monkeypatch.setattr(tier0, "match_phrasebook", lambda *_args, **_kwargs: [])
    baseline = [_observed(message) for message, _expected in HELD_OUT]
    baseline_negatives = [_observed(message) for message in NEGATIVE_CONTRASTS]

    monkeypatch.setattr(tier0, "match_phrasebook", real_match)
    supported = [_observed(message) for message, _expected in HELD_OUT]
    supported_negatives = [_observed(message) for message in NEGATIVE_CONTRASTS]
    expected = [row for _message, row in HELD_OUT]

    baseline_correct = sum(actual == wanted for actual, wanted in zip(baseline, expected))
    supported_correct = sum(actual == wanted for actual, wanted in zip(supported, expected))
    assert supported_correct == len(HELD_OUT)
    assert supported_correct >= baseline_correct + 7
    assert supported_negatives == baseline_negatives
    assert supported_negatives == [((), ())] * len(NEGATIVE_CONTRASTS)


def test_phrasebook_provenance_stays_local_and_out_of_narrator_directive():
    result = _run("I pressed Halvic for another five silver.", _rpg_cfg(True), _state())
    assert [row["skill"] for row in _checks(result)] == ["persuasion"]
    assert result.semantic_turn is not None
    assert any(candidate.source == "construction" for frame in result.semantic_turn.frames
               for candidate in frame.candidates)
    assert all("phrasebook" not in str(op).lower() for op in result.rule_ops)
    assert all("bargain.press_for_terms" not in str(op) for op in result.rule_ops)
