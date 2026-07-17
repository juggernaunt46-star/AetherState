"""Pure whole-mechanic contract tests for ``skill_check/1``."""
from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.mechanic_settlement import (
    SKILL_CHECK_CONTRACT,
    MechanicSettlementError,
    build_skill_check_settlement,
    skill_check_settlement_ref,
    validate_mechanic_settlement,
    validate_mechanic_settlement_row,
)
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import build_meaning_binding
from aetherstate.semantic_fabric import load_default_semantic_fabric


STATE_AFTER = content_fingerprint({"effect": "strained"})


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


def _check(frame: dict, quality: str = "success") -> dict:
    return {
        "kind": "check",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "actor_id": "player",
        "capability_id": "stealth",
        "target_entity_id": frame["target_entity_id"],
        "result": 11,
        "outcome_quality": quality,
    }


def _cost(frame: dict) -> dict:
    return {
        "kind": "cost",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "resource_id": "focus",
        "pre": 6,
        "delta": -2,
        "post": 4,
        "maximum": 6,
    }


def _mastery(frame: dict) -> dict:
    return {
        "kind": "mastery",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "capability_id": "stealth",
        "pre": 1,
        "delta": 2,
        "post": 3,
    }


def _cooldown(frame: dict) -> dict:
    return {
        "kind": "cooldown",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "ability_id": "shadow_step",
        "pre": 0,
        "post": 4,
    }


def _consequence(frame: dict) -> dict:
    return {
        "kind": "consequence",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "effect_id": "strained",
        "pre_state_ref": None,
        "post_state_ref": STATE_AFTER,
    }


def test_empty_change_skill_check_is_a_complete_deterministic_settlement():
    frame, binding = _candidate()

    receipt, store_row = build_skill_check_settlement(
        frame, binding, accepted_group=[_check(frame)],
    )
    again, again_row = build_skill_check_settlement(
        frame, binding, accepted_group=[_check(frame)],
    )

    assert receipt == again and store_row == again_row
    assert receipt["contract_id"] == SKILL_CHECK_CONTRACT
    assert receipt["settlement_ref"] == skill_check_settlement_ref(frame, binding)
    assert receipt["outcome"] == "resolved"
    assert receipt["outcome_quality"] == "success"
    assert receipt["target_post_state"] is None
    assert receipt["applied_changes"] == []
    assert validate_mechanic_settlement(receipt) == receipt
    assert validate_mechanic_settlement_row(store_row) == store_row


def test_skill_check_seals_all_induced_changes_without_target_state():
    frame, binding = _candidate(target="watch")
    rows = [_check(frame), _cost(frame), _mastery(frame), _cooldown(frame)]

    receipt, _row = build_skill_check_settlement(
        frame, binding, accepted_group=reversed(rows),
    )

    assert [row["kind"] for row in receipt["applied_changes"]] == [
        "cost", "mastery", "cooldown",
    ]
    assert receipt["target_post_state"] is None


def test_critical_failure_requires_its_exact_consequence():
    frame, binding = _candidate()

    with pytest.raises(MechanicSettlementError, match="exact consequence"):
        build_skill_check_settlement(
            frame, binding, accepted_group=[_check(frame, "crit_fail")],
        )

    receipt, _row = build_skill_check_settlement(
        frame,
        binding,
        accepted_group=[_check(frame, "crit_fail"), _consequence(frame)],
    )
    assert [row["kind"] for row in receipt["applied_changes"]] == ["consequence"]


def test_skill_check_rejects_impact_members_and_receipt_tampering():
    frame, binding = _candidate()
    hp = {
        "kind": "hp",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "watch",
        "pre": 4,
        "delta": -1,
        "post": 3,
        "maximum": 4,
    }
    with pytest.raises(MechanicSettlementError, match="not admitted"):
        build_skill_check_settlement(
            frame, binding, accepted_group=[_check(frame), hp],
        )

    receipt, _row = build_skill_check_settlement(
        frame, binding, accepted_group=[_check(frame)],
    )
    forged = deepcopy(receipt)
    forged["target_post_state"] = {"combatant_id": "watch", "hp": {"cur": 3, "max": 4}}
    with pytest.raises(MechanicSettlementError, match="cannot claim"):
        validate_mechanic_settlement(forged)


def test_skill_check_rejects_wrong_frame_roles_and_cardinality():
    frame, binding = _candidate()
    wrong = _check(frame)
    wrong["capability_id"] = "persuasion"
    with pytest.raises(MechanicSettlementError, match="semantic action roles"):
        build_skill_check_settlement(frame, binding, accepted_group=[wrong])
    with pytest.raises(MechanicSettlementError, match="exactly one check"):
        build_skill_check_settlement(frame, binding, accepted_group=[])
    with pytest.raises(MechanicSettlementError, match="repeats one mechanic member"):
        build_skill_check_settlement(
            frame, binding, accepted_group=[_check(frame), _check(frame)],
        )
