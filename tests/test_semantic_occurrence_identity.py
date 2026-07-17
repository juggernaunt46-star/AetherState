"""Turn-scoped semantic occurrence identity for exact retry versus repeated play."""
from __future__ import annotations

import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.state import empty_state


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _combat_state() -> dict:
    state = empty_state()
    state["entities"] = {
        "player": {"kind": "player", "name": "Player", "present": True},
        "iven": {"kind": "npc", "name": "Iven", "present": True},
    }
    state["player"] = {
        "player": {
            "eid": "player",
            "stats": {"DEX": 14},
            "skills": {"swordplay": 2},
            "abilities": [],
            "resources": {},
            "defs": {
                "skills": {
                    "swordplay": {
                        "name": "Swordplay",
                        "keyed_stat": "DEX",
                        "governs": ["strike"],
                    },
                },
            },
        },
    }
    state["combat"] = {
        "active": True,
        "combatants": {
            "iven": {
                "id": "iven",
                "eid": "iven",
                "name": "Iven",
                "side": "enemy",
                "defeated": False,
                "hp": {"cur": 14, "max": 14},
            },
        },
    }
    return state


def _multi_event_state() -> dict:
    state = empty_state()
    state["entities"] = {
        "player": {"kind": "player", "name": "Player", "present": True},
        "iven": {"kind": "npc", "name": "Iven", "present": True},
        "nera": {"kind": "npc", "name": "Nera", "present": True},
    }
    state["player"] = {
        "player": {
            "eid": "player",
            "stats": {"CUN": 14},
            "skills": {"kiln_song_vibrometry": 3},
            "abilities": [],
            "resources": {},
            "defs": {
                "skills": {
                    "kiln_song_vibrometry": {
                        "name": "Kiln-Song Vibrometry",
                        "keyed_stat": "CUN",
                        "governs": ["inspect", "study", "read resonance"],
                    },
                },
            },
        },
    }
    return state


def _run(turn: int | None, state: dict | None = None):
    return tier0.run(
        {"messages": [{"role": "user", "content": "I use Swordplay to strike Iven."}]},
        "new_turn",
        False,
        state or _combat_state(),
        _cfg(),
        random.Random(11),
        turn=turn,
    )


def _op(result, kind: str) -> dict:
    return next(op for op in result.rule_ops if op.get("op") == kind)


def test_same_turn_retry_keeps_identity_but_later_repetition_gets_a_new_occurrence():
    first = _run(7)
    retry = _run(7)
    later = _run(8)

    first_frame = _op(first, "semantic_frame_commit")["frame"]
    retry_frame = _op(retry, "semantic_frame_commit")["frame"]
    later_frame = _op(later, "semantic_frame_commit")["frame"]
    first_settlement = _op(first, "mechanic_settlement_commit")
    retry_settlement = _op(retry, "mechanic_settlement_commit")
    later_settlement = _op(later, "mechanic_settlement_commit")

    assert first_frame["frame_id"] == "t7.f1"
    assert first_frame["event_node_id"] == "event.t7.f1"
    assert first_frame["fingerprint"] == retry_frame["fingerprint"]
    assert first_settlement["settlement_ref"] == retry_settlement["settlement_ref"]

    assert later_frame["frame_id"] == "t8.f1"
    assert later_frame["event_node_id"] == "event.t8.f1"
    assert first_frame["meaning_ref"] == later_frame["meaning_ref"]
    assert first_frame["meaning_binding_ref"] != later_frame["meaning_binding_ref"]
    assert first_frame["fingerprint"] != later_frame["fingerprint"]
    assert first_settlement["settlement_ref"] != later_settlement["settlement_ref"]


def test_direct_caller_without_turn_uses_the_next_ledger_turn_deterministically():
    state = _combat_state()
    state["meta"]["turn"] = 12

    first = _run(None, state)
    retry = _run(None, state)
    first_frame = _op(first, "semantic_frame_commit")["frame"]
    retry_frame = _op(retry, "semantic_frame_commit")["frame"]

    assert first_frame["frame_id"] == "t13.f1"
    assert first_frame["event_node_id"] == "event.t13.f1"
    assert first_frame["fingerprint"] == retry_frame["fingerprint"]


def test_late_earlier_kill_candidate_does_not_steal_a_utility_checks_frame():
    source = "I kill Iven, but I use Kiln-Song Vibrometry to inspect Nera."
    result = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _multi_event_state(),
        _cfg(),
        random.Random(11),
        turn=4,
    )

    frames = [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
    ]
    by_capability = {frame["capability_id"]: frame for frame in frames}
    assert by_capability["kill_attempt"]["frame_id"] == "t4.f1"
    inspection = by_capability["kiln_song_vibrometry"]
    assert inspection["frame_id"] == "t4.f2"

    check = next(op for op in result.rule_ops if op.get("op") == "check")
    assert check["skill"] == "kiln_song_vibrometry"
    assert check["_semantic_frame_ref"] == inspection["fingerprint"]


def test_repeated_capability_events_keep_separate_span_anchored_frames():
    source = (
        "I use Kiln-Song Vibrometry to inspect Iven, and I use "
        "Kiln-Song Vibrometry to inspect Nera."
    )
    result = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        _multi_event_state(),
        _cfg(),
        random.Random(11),
        turn=5,
    )

    frames = [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
        and op["frame"]["capability_id"] == "kiln_song_vibrometry"
    ]
    checks = [op for op in result.rule_ops if op.get("op") == "check"]
    assert [frame["frame_id"] for frame in frames] == ["t5.f1", "t5.f2"]
    assert len(checks) == 2
    assert {check["_semantic_frame_ref"] for check in checks} == {
        frame["fingerprint"] for frame in frames
    }
