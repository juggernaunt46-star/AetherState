"""Tier-0 closure tests for the atomic generic ``skill_check/1`` boundary."""
from __future__ import annotations

from copy import deepcopy

from aetherstate.mechanic_settlement import (
    SKILL_CHECK_CONTRACT,
    skill_check_settlement_ref,
)
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import build_meaning_binding
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.tier0 import Tier0Result, _group_skill_check_settlements


def _candidate(*, target: str | None = None) -> tuple[dict, dict]:
    source = "I sneak past the watch."
    meaning = load_default_semantic_fabric().translate(source)
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.skill.1",
        event_node_id="event.skill.1",
        event_span=(0, len(source)),
    )
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="player",
        capability_id="stealth",
        action_class="skill_check",
        target_entity_id=target,
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id="event.skill.1",
        mechanic_disposition="candidate",
    )
    frame.add_evidence("action", 2, 7, "skill_check")
    return frame.snapshot(source), binding


def _check(frame: dict, tier: str = "success") -> dict:
    return {
        "op": "check",
        "skill": "stealth",
        "char": "player",
        "result": 10,
        "tier": tier,
        "_semantic_frame_ref": frame["fingerprint"],
    }


def _mastery(frame: dict, amount: int = 3) -> dict:
    return {
        "op": "master_tick",
        "char": "player",
        "skill": "stealth",
        "amount": amount,
        "_semantic_frame_ref": frame["fingerprint"],
    }


def _group(ops: list[dict], frame: dict, binding: dict) -> Tier0Result:
    result = Tier0Result(rule_ops=deepcopy(ops))
    _group_skill_check_settlements(
        result,
        [frame],
        {frame["frame_id"]: binding},
    )
    return result


def _wrapper(result: Tier0Result) -> dict:
    rows = [op for op in result.rule_ops if op.get("op") == "mechanic_settlement_commit"]
    assert len(rows) == 1
    return rows[0]


def test_complete_targetless_check_becomes_one_wrapper_with_exact_projections():
    frame, binding = _candidate()
    unrelated = {"op": "clock_tick", "minutes": 3}
    result = _group([_check(frame), _mastery(frame), unrelated], frame, binding)

    wrapper = _wrapper(result)
    assert wrapper["contract_id"] == SKILL_CHECK_CONTRACT
    assert wrapper["settlement_ref"] == skill_check_settlement_ref(frame, binding)
    assert [member["op"] for member in wrapper["members"]] == ["check", "master_tick"]
    start = result.rule_ops.index(wrapper)
    assert result.rule_ops[start + 1:start + 3] == [
        {**member, "_settlement_ref": wrapper["settlement_ref"],
         "_settlement_member_index": index}
        for index, member in enumerate(wrapper["members"])
    ]
    assert unrelated in result.rule_ops


def test_contextual_target_does_not_admit_hp_scene_or_target_state_members():
    frame, binding = _candidate(target="watch")
    impact = {
        "op": "combatant_hp",
        "target": "watch",
        "delta": -1,
        "_strike": True,
        "_semantic_frame_ref": frame["fingerprint"],
    }
    scene = {
        "op": "scene_set",
        "phase": "climax",
        "_semantic_frame_ref": frame["fingerprint"],
    }
    result = _group([_check(frame), impact, _mastery(frame), scene], frame, binding)

    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)
    assert result.rule_ops == []
    assert any("atomic mechanic group" in notice for notice in result.notices)


def test_duplicate_or_incomplete_check_fails_closed_without_raw_fallback():
    frame, binding = _candidate()
    unrelated = {"op": "clock_tick", "minutes": 3}
    for ops in (
        [_check(frame), _check(frame), _mastery(frame), unrelated],
        [_check(frame), unrelated],
    ):
        result = _group(ops, frame, binding)
        assert result.rule_ops == [unrelated]
        assert not any(op.get("op") == "check" for op in result.rule_ops)


def test_critical_failure_requires_and_wraps_its_exact_consequence():
    frame, binding = _candidate()
    consequence = {
        "op": "effect_add",
        "char": "player",
        "effect": "Strained",
        "kind": "status",
        "_semantic_frame_ref": frame["fingerprint"],
    }
    result = _group([_check(frame, "crit_fail"), consequence], frame, binding)

    assert [member["op"] for member in _wrapper(result)["members"]] == [
        "check", "effect_add",
    ]
