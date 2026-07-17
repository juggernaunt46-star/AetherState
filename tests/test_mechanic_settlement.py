"""Pure whole-mechanic contract tests for ``weapon_attack/1``."""
from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.mechanic_settlement import (
    MECHANIC_SETTLEMENT_SCHEMA,
    MechanicSettlementError,
    build_weapon_attack_settlement,
    validate_mechanic_settlement,
    validate_mechanic_settlement_row,
    weapon_attack_settlement_ref,
)
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import (
    build_meaning_binding,
    semantic_match_ref,
)
from aetherstate.semantic_fabric import load_default_semantic_fabric


FRAME_BAD = "sha256:" + "f" * 64
MEANING_BAD = "sha256:" + "e" * 64
STATE_BEFORE = content_fingerprint({"state": "before"})
STATE_AFTER = content_fingerprint({"state": "after"})


def _candidate() -> tuple[dict, dict]:
    source = "I use Meridian Pierce to strike Iven."
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(0, len(source)),
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(attack)],
        }],
        role_evidence=[{
            "role": "action",
            "evidence_refs": [semantic_match_ref(attack)],
        }],
    )
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="player",
        capability_id="meridian_pierce",
        action_class="weapon_attack",
        target_entity_id="iven",
        target_name="Iven",
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id="event.f1",
        mechanic_disposition="candidate",
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")
    return frame.snapshot(source), binding


def _check(frame: dict, quality: str = "success", result: int = 10) -> dict:
    return {
        "kind": "check",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "actor_id": "player",
        "capability_id": "meridian_pierce",
        "target_entity_id": "iven",
        "result": result,
        "outcome_quality": quality,
    }


def _hp(frame: dict, *, pre: int = 14, delta: int = -2, post: int = 12) -> dict:
    return {
        "kind": "hp",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "iven",
        "pre": pre,
        "delta": delta,
        "post": post,
        "maximum": 14,
    }


def _cost(frame: dict) -> dict:
    return {
        "kind": "cost",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "resource_id": "spoolcharge",
        "pre": 10,
        "delta": -2,
        "post": 8,
        "maximum": 10,
    }


def _mastery(frame: dict) -> dict:
    return {
        "kind": "mastery",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "capability_id": "meridian_pierce",
        "pre": 1,
        "delta": 3,
        "post": 4,
    }


def _cooldown(frame: dict) -> dict:
    return {
        "kind": "cooldown",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "player",
        "ability_id": "piercing_surge",
        "pre": 0,
        "post": 5,
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


def _admission(frame: dict) -> dict:
    return {
        "kind": "target_admission",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "target_entity_id": "iven",
        "combatant_id": "iven",
        "pre_state_ref": None,
        "post_state_ref": STATE_AFTER,
    }


def _scene(frame: dict) -> dict:
    return {
        "kind": "scene_transition",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": "scene",
        "pre_state_ref": STATE_BEFORE,
        "post_state_ref": STATE_AFTER,
    }


def _build(
    frame: dict,
    binding: dict,
    rows: list[dict],
    *,
    post: int = 12,
    opening: dict[str, bool] | None = None,
) -> tuple[dict, dict]:
    return build_weapon_attack_settlement(
        frame,
        binding,
        accepted_group=rows,
        target_post_state={"combatant_id": "iven", "hp": {"cur": post, "max": 14}},
        opening_requirements=opening or {
            "target_admission": False,
            "scene_transition": False,
        },
    )


def test_builder_is_deterministic_idempotent_and_store_ready():
    frame, binding = _candidate()
    rows = [_check(frame), _hp(frame), _cost(frame), _mastery(frame), _cooldown(frame)]

    receipt, store_row = _build(frame, binding, rows)
    reversed_receipt, reversed_row = _build(frame, binding, list(reversed(rows)))

    assert receipt == reversed_receipt
    assert store_row == reversed_row
    assert receipt["schema"] == MECHANIC_SETTLEMENT_SCHEMA
    assert receipt["settlement_ref"] == weapon_attack_settlement_ref(frame, binding)
    assert receipt["outcome"] == "hit"
    assert receipt["outcome_quality"] == "success"
    assert [row["kind"] for row in receipt["applied_changes"]] == [
        "hp", "cost", "mastery", "cooldown",
    ]
    assert validate_mechanic_settlement(receipt) == receipt
    assert validate_mechanic_settlement_row(store_row) == store_row
    assert store_row["receipt"] == receipt


def test_settlement_identity_is_stable_while_the_accepted_request_detects_change():
    frame, binding = _candidate()
    first, first_row = _build(frame, binding, [_check(frame, result=10), _hp(frame)])
    second, second_row = _build(frame, binding, [_check(frame, result=11), _hp(frame)])

    assert first["settlement_ref"] == second["settlement_ref"]
    assert first["accepted_group_fingerprint"] != second["accepted_group_fingerprint"]
    assert first_row["request_fingerprint"] != second_row["request_fingerprint"]
    assert first["outcome"] == second["outcome"] == "hit"
    assert first["outcome_quality"] == second["outcome_quality"] == "success"


def test_validator_rejects_structural_and_fingerprint_tampering():
    frame, binding = _candidate()
    receipt, store_row = _build(frame, binding, [_check(frame), _hp(frame)])

    extra = deepcopy(receipt)
    extra["damage_prose"] = "severe"
    with pytest.raises(MechanicSettlementError, match="unexpected fields"):
        validate_mechanic_settlement(extra)

    changed = deepcopy(receipt)
    changed["outcome"] = "defeat"
    payload = {key: value for key, value in changed.items() if key != "receipt_fingerprint"}
    changed["receipt_fingerprint"] = content_fingerprint(payload)
    with pytest.raises(MechanicSettlementError, match="disagrees with settled HP"):
        validate_mechanic_settlement(changed)

    bad_row = deepcopy(store_row)
    bad_row["request_fingerprint"] = FRAME_BAD
    with pytest.raises(MechanicSettlementError, match="request fingerprint mismatch"):
        validate_mechanic_settlement_row(bad_row)


@pytest.mark.parametrize(
    ("quality", "pre", "delta", "post", "outcome"),
    [
        ("fail", 12, 0, 12, "miss"),
        ("partial", 14, -1, 13, "hit"),
        ("success", 14, -2, 12, "hit"),
        ("crit_success", 2, -2, 0, "defeat"),
    ],
)
def test_miss_hit_and_defeat_are_derived_once_from_the_accepted_hp_change(
    quality: str,
    pre: int,
    delta: int,
    post: int,
    outcome: str,
):
    frame, binding = _candidate()
    receipt, _row = _build(
        frame,
        binding,
        [_check(frame, quality), _hp(frame, pre=pre, delta=delta, post=post)],
        post=post,
    )

    assert receipt["outcome"] == outcome
    assert receipt["outcome_quality"] == quality
    assert receipt["target_post_state"]["hp"]["cur"] == post


def test_critical_failure_requires_and_seals_its_exact_consequence():
    frame, binding = _candidate()
    rows = [_check(frame, "crit_fail"), _hp(frame, pre=12, delta=0, post=12)]
    with pytest.raises(MechanicSettlementError, match="needs its exact consequence"):
        _build(frame, binding, rows)

    receipt, _row = _build(frame, binding, [*rows, _consequence(frame)])
    assert receipt["outcome"] == "miss"
    assert receipt["applied_changes"][-1] == {
        "kind": "consequence",
        "subject_id": "player",
        "effect_id": "strained",
        "post_state_ref": STATE_AFTER,
    }


@pytest.mark.parametrize(
    ("member", "field", "bad_value", "message"),
    [
        ("check", "frame_ref", FRAME_BAD, "different semantic frame"),
        ("check", "meaning_ref", MEANING_BAD, "different semantic meaning"),
        ("check", "target_entity_id", "mara", "semantic action roles"),
        ("hp", "subject_id", "mara", "different combatant"),
    ],
)
def test_cross_frame_meaning_and_target_members_fail_closed(
    member: str,
    field: str,
    bad_value: str,
    message: str,
):
    frame, binding = _candidate()
    rows = [_check(frame), _hp(frame)]
    index = 0 if member == "check" else 1
    rows[index][field] = bad_value
    with pytest.raises(MechanicSettlementError, match=message):
        _build(frame, binding, rows)


def test_target_post_state_must_match_the_exact_hp_member():
    frame, binding = _candidate()
    with pytest.raises(MechanicSettlementError, match="target_post_state does not match"):
        _build(frame, binding, [_check(frame), _hp(frame)], post=11)

    with pytest.raises(MechanicSettlementError, match="different semantic target"):
        build_weapon_attack_settlement(
            frame,
            binding,
            accepted_group=[_check(frame), _hp(frame)],
            target_post_state={"combatant_id": "mara", "hp": {"cur": 12, "max": 14}},
            opening_requirements={"target_admission": False, "scene_transition": False},
        )


@pytest.mark.parametrize("keep", [{"check"}, {"hp"}, set()])
def test_incomplete_atomic_group_is_rejected(keep: set[str]):
    frame, binding = _candidate()
    rows = [row for row in [_check(frame), _hp(frame)] if row["kind"] in keep]
    with pytest.raises(MechanicSettlementError, match="exactly one check and one HP"):
        _build(frame, binding, rows)


def test_duplicate_member_and_check_hp_disagreement_are_rejected():
    frame, binding = _candidate()
    with pytest.raises(MechanicSettlementError, match="repeats one mechanic member"):
        _build(frame, binding, [_check(frame), _hp(frame), _hp(frame)])
    with pytest.raises(MechanicSettlementError, match="requires negative HP damage"):
        _build(
            frame,
            binding,
            [_check(frame, "success"), _hp(frame, pre=12, delta=0, post=12)],
        )


def test_opening_requirements_close_target_admission_and_scene_transition():
    frame, binding = _candidate()
    rows = [_check(frame), _hp(frame), _admission(frame), _scene(frame)]
    opening = {"target_admission": True, "scene_transition": True}
    receipt, _row = _build(frame, binding, rows, opening=opening)

    assert [row["kind"] for row in receipt["applied_changes"][:2]] == [
        "target_admission", "scene_transition",
    ]
    with pytest.raises(MechanicSettlementError, match="target_admission count"):
        _build(frame, binding, rows[0:2] + [_scene(frame)], opening=opening)
    with pytest.raises(MechanicSettlementError, match="scene_transition count"):
        _build(
            frame,
            binding,
            [_check(frame), _hp(frame), _admission(frame)],
            opening=opening,
        )
    with pytest.raises(MechanicSettlementError, match="target_admission count"):
        _build(frame, binding, rows)


def test_non_candidate_or_non_v3_frame_cannot_request_settlement():
    frame, binding = _candidate()
    blocked = deepcopy(binding)
    blocked.update({
        "constraint_integrity": "conflict",
        "request_disposition": "unresolved",
        "mechanic_disposition": "invalid_scope_conflict",
        "reason_refs": [],
    })
    blocked_payload = {key: value for key, value in blocked.items() if key != "fingerprint"}
    blocked["fingerprint"] = content_fingerprint(blocked_payload)
    blocked_frame = deepcopy(frame)
    blocked_frame["meaning_binding_ref"] = blocked["fingerprint"]
    blocked_frame_payload = {
        key: value for key, value in blocked_frame.items() if key != "fingerprint"
    }
    blocked_frame["fingerprint"] = content_fingerprint(blocked_frame_payload)

    with pytest.raises(MechanicSettlementError, match="not a mechanic candidate"):
        _build(blocked_frame, blocked, [_check(blocked_frame), _hp(blocked_frame)])

    legacy = deepcopy(frame)
    legacy["schema"] = "semantic-action-frame/2"
    del legacy["meaning_binding_ref"]
    del legacy["event_node_id"]
    del legacy["world_alignment_refs"]
    del legacy["invoked_capability_ids"]
    del legacy["declared_modifier"]
    legacy_payload = {key: value for key, value in legacy.items() if key != "fingerprint"}
    legacy["fingerprint"] = content_fingerprint(legacy_payload)
    with pytest.raises(MechanicSettlementError, match="requires a V3"):
        _build(legacy, binding, [_check(legacy), _hp(legacy)])
