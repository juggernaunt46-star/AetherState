"""Authority-safe Player Lesson intent application at the live Tier-0 boundary."""
from __future__ import annotations

import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.playerlex import PlayerLex
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_atlas import load_default_semantic_atlas
from aetherstate.state import empty_state
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.foe_floor = False
    cfg.specialization.war_room = False
    return cfg


def _state() -> dict:
    state = empty_state()
    state["entities"]["player"] = {
        "kind": "player",
        "name": "Player",
        "present": True,
        "aliases": [],
    }
    state["entities"]["panel"] = {
        "kind": "object",
        "name": "etched panel",
        "present": True,
        "aliases": ["panel"],
    }
    state["player"] = {
        "player": {
            "eid": "player",
            "stats": {"CUN": 14},
            "skills": {"glass_read": 2},
            "abilities": [],
            "resources": {},
            "defs": {
                "skills": {
                    "glass_read": {
                        "name": "Glass Read",
                        "keyed_stat": "CUN",
                        "governs": ["inspect"],
                    }
                }
            },
        }
    }
    return state


def _playerlex(store: Store) -> PlayerLex:
    return PlayerLex(store.db, load_default_semantic_atlas(), store.apply_guard())


def _intent_row(entry: dict, source: str, *, slot: str = "action") -> dict:
    start = source.index("Glass Read")
    return {
        "lesson_id": "lesson_" + "a" * 32,
        "lesson_revision": 1,
        "lesson_fingerprint": "sha256:" + "b" * 64,
        "reason": "scope_and_anchor_match",
        "scope": "every_rpg_turn",
        "intent_slot": slot,
        "anchor": {
            "entry_id": entry["entry_id"],
            "lex_id": entry["lex_id"],
            "concept_id": entry["concept"]["concept_id"],
            "meaning_fingerprint": entry["concept"]["meaning_fingerprint"],
        },
        "source_span": {"start": start, "end": start + len("Glass Read")},
        "approval_source_id": (
            "playerlex."
            + entry["entry_id"].removeprefix("playerlex_")
            + ".r"
            + str(entry["provenance"]["approval_revision"])
        ),
    }


def test_action_intent_resolves_only_an_existing_actionlex_ambiguity():
    source = "I use Glass Read on the panel."
    store = Store(":memory:")
    playerlex = _playerlex(store)
    inspect = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    playerlex.approve(
        kind="name",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.move",
    )

    baseline = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(9),
        recognition_overlay=playerlex.propose,
    )
    corrected = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(9),
        recognition_overlay=playerlex.propose,
        interpretation_overlay=lambda _turn: {
            "selected": [_intent_row(inspect, source)]
        },
    )

    before = baseline.semantic_turn.frames[0]
    after = corrected.semantic_turn.frames[0]
    assert before.action_class == "ambiguous_action"
    assert {item for item in before.ambiguity if item.startswith("action_class.")} == {
        "action_class.inspection",
        "action_class.movement",
    }
    assert after.action_class == "inspection"
    assert not any(item.startswith("action_class.") for item in after.ambiguity)
    assert after.capability_id == before.capability_id == "glass_read"
    assert after.actor_id == before.actor_id == "player"
    assert after.candidates == before.candidates
    assert corrected.intent_applications == [
        {
            "lesson_id": "lesson_" + "a" * 32,
            "lesson_revision": 1,
            "lesson_fingerprint": "sha256:" + "b" * 64,
            "applied": True,
            "reason": "action_ambiguity_resolved",
            "frame_id": after.frame_id,
            "selected_value": "inspection",
            "meaning_binding_ref": after.meaning_binding_ref,
            "frame_fingerprint": next(
                op["frame"]["fingerprint"]
                for op in corrected.rule_ops
                if op.get("op") == "semantic_frame_commit"
                and op["frame"]["frame_id"] == after.frame_id
            ),
        }
    ]


def test_action_intent_abstains_when_current_input_is_already_unambiguous():
    source = "I use Glass Read on the panel."
    store = Store(":memory:")
    playerlex = _playerlex(store)
    inspect = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    result = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(10),
        recognition_overlay=playerlex.propose,
        interpretation_overlay=lambda _turn: {
            "selected": [_intent_row(inspect, source)]
        },
    )

    frame = result.semantic_turn.frames[0]
    assert frame.action_class == "inspection"
    assert result.intent_applications[0]["applied"] is False
    assert result.intent_applications[0]["reason"] == "input_unambiguous"
    assert result.intent_applications[0]["selected_value"] is None


def test_action_intent_never_overrides_a_different_explicit_current_verb():
    source = "I inspect the panel with Glass Read."
    store = Store(":memory:")
    playerlex = _playerlex(store)
    movement = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.move",
    )
    baseline = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(11),
        recognition_overlay=playerlex.propose,
    )
    corrected = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _state(),
        _cfg(),
        random.Random(11),
        recognition_overlay=playerlex.propose,
        interpretation_overlay=lambda _turn: {
            "selected": [_intent_row(movement, source)]
        },
    )

    assert corrected.semantic_turn.frames[0].action_class == (
        baseline.semantic_turn.frames[0].action_class
    )
    assert corrected.semantic_turn.frames[0].action_class != "movement"
    assert corrected.intent_applications[0]["applied"] is False
    assert corrected.intent_applications[0]["reason"] == "current_input_conflict"
    assert corrected.intent_applications[0]["selected_value"] is None


def test_target_intent_narrows_only_an_independently_grounded_exact_span():
    frame = ActionFrame(frame_id="f1", clause_index=0, start=0, end=24)
    preference = {
        "lesson_id": "lesson_" + "c" * 32,
        "intent_slot": "target",
        "source_start": 9,
        "source_end": 13,
        "status": "selected",
    }
    candidates = {"iven", "nera"}
    grounded_by_span = {(9, 13): {"iven"}, (18, 22): {"nera"}}

    narrowed = tier0._apply_intent_target_preference(
        frame,
        candidates,
        grounded_by_span,
        [preference],
    )

    assert narrowed == {"iven"}
    assert preference["status"] == "applied"
    assert preference["reason"] == "target_ambiguity_resolved"
    assert preference["selected_value"] == "iven"


def test_target_intent_refuses_explicit_multi_target_and_same_span_identity_ambiguity():
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=0,
        end=24,
        ambiguity=["occurrence.multiple_targets"],
    )
    explicit = {
        "lesson_id": "lesson_" + "d" * 32,
        "intent_slot": "target",
        "source_start": 9,
        "source_end": 13,
        "status": "selected",
    }
    candidates = {"iven", "nera"}
    assert tier0._apply_intent_target_preference(
        frame,
        candidates,
        {(9, 13): {"iven"}, (18, 22): {"nera"}},
        [explicit],
    ) == candidates
    assert explicit["status"] == "not_applied"
    assert explicit["reason"] == "explicit_multi_target"

    frame.ambiguity = []
    same_span = {**explicit, "status": "selected", "reason": ""}
    assert tier0._apply_intent_target_preference(
        frame,
        candidates,
        {(9, 13): {"iven", "nera"}},
        [same_span],
    ) == candidates
    assert same_span["status"] == "not_applied"
    assert same_span["reason"] == "candidate_ambiguous"
