"""Deterministic whole-mechanic receipts for one admitted weapon attack.

The semantic frame decides what the Player attempted.  This module does not reinterpret that
prose and does not apply reducer operations.  It seals one already accepted, post-reducer group
containing the check and every state mutation caused by that check.  The resulting receipt is the
only qualitative weapon-attack result that narrator projection should consume.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from .capability_glossary import content_fingerprint
from .semantic import ACTION_FRAME_SCHEMA, validate_action_frame_snapshot
from .semantic_binding import validate_meaning_binding


MECHANIC_SETTLEMENT_SCHEMA = "mechanic-settlement/1"
WEAPON_ATTACK_CONTRACT = "weapon_attack/1"
SKILL_CHECK_CONTRACT = "skill_check/1"
COMBAT_OPENING_CONTRACT = "combat_opening/1"
# These action classes either already have their own whole-occurrence settlement or still require
# one. A check embedded in any of them is not an independently complete non-impact skill check.
NON_SKILL_CHECK_ACTION_CLASSES = frozenset({
    "weapon_attack",
    "combat_opening",
    "grapple",
    "kill_attempt",
    "grand_kill_attempt",
})
WEAPON_ATTACK_REQUIREMENT_SCHEMA = "weapon-attack-settlement-requirement/1"
WEAPON_ATTACK_GROUP_SCHEMA = "weapon-attack-accepted-group/1"
SKILL_CHECK_REQUIREMENT_SCHEMA = "skill-check-settlement-requirement/1"
SKILL_CHECK_GROUP_SCHEMA = "skill-check-accepted-group/1"
COMBAT_OPENING_REQUIREMENT_SCHEMA = "combat-opening-settlement-requirement/1"
COMBAT_OPENING_GROUP_SCHEMA = "combat-opening-accepted-group/1"
MECHANIC_SETTLEMENT_IDENTITY_SCHEMA = "mechanic-settlement-identity/1"
MECHANIC_SETTLEMENT_REQUEST_SCHEMA = "mechanic-settlement-request/1"

OUTCOMES = ("miss", "hit", "defeat", "resolved")
WEAPON_OUTCOMES = ("miss", "hit", "defeat")
OUTCOME_QUALITIES = (
    "crit_fail", "fail", "partial", "success", "crit_success", "automatic",
)
_WEAPON_OPENING_ADMISSION_CAP = 3

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REFERENCE_RE = re.compile(r"[a-z0-9][a-z0-9_.:#-]{0,159}\Z")

_RECEIPT_FIELDS = {
    "schema",
    "settlement_ref",
    "contract_id",
    "frame_ref",
    "meaning_ref",
    "requirement_fingerprint",
    "accepted_group_fingerprint",
    "receipt_fingerprint",
    "outcome",
    "outcome_quality",
    "applied_changes",
    "target_post_state",
}
_STORE_ROW_FIELDS = {
    "settlement_ref",
    "contract_id",
    "frame_ref",
    "meaning_ref",
    "outcome",
    "outcome_quality",
    "requirement_fingerprint",
    "request_fingerprint",
    "accepted_group_fingerprint",
    "receipt_fingerprint",
    "receipt",
}
_GROUP_FIELDS = {
    "check": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "actor_id",
        "capability_id",
        "target_entity_id",
        "result",
        "outcome_quality",
    },
    "hp": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "pre",
        "delta",
        "post",
        "maximum",
    },
    "cost": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "resource_id",
        "pre",
        "delta",
        "post",
        "maximum",
    },
    "mastery": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "capability_id",
        "pre",
        "delta",
        "post",
    },
    "cooldown": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "ability_id",
        "pre",
        "post",
    },
    "consequence": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "effect_id",
        "pre_state_ref",
        "post_state_ref",
    },
    "target_admission": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "target_entity_id",
        "combatant_id",
        "pre_state_ref",
        "post_state_ref",
    },
    "scene_transition": {
        "kind",
        "frame_ref",
        "meaning_ref",
        "subject_id",
        "pre_state_ref",
        "post_state_ref",
    },
}
_APPLIED_FIELDS = {
    "hp": {"kind", "subject_id", "delta", "post"},
    "cost": {"kind", "subject_id", "resource_id", "delta", "post"},
    "mastery": {"kind", "subject_id", "capability_id", "delta", "post"},
    "cooldown": {"kind", "subject_id", "ability_id", "delta", "post"},
    "consequence": {"kind", "subject_id", "effect_id", "post_state_ref"},
    "target_admission": {
        "kind",
        "subject_id",
        "entity_id",
        "post_state_ref",
    },
    "scene_transition": {"kind", "subject_id", "post_state_ref"},
}
_GROUP_ORDER = {
    "check": 0,
    "target_admission": 1,
    "scene_transition": 2,
    "hp": 3,
    "cost": 4,
    "mastery": 5,
    "cooldown": 6,
    "consequence": 7,
}
_APPLIED_ORDER = {kind: order for kind, order in _GROUP_ORDER.items() if kind != "check"}


class MechanicSettlementError(ValueError):
    """A mechanic group or settlement receipt violates the admitted v1 contract."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MechanicSettlementError(f"{label} must be an object with string fields")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unexpected = sorted(set(value) - fields)
    if unexpected:
        raise MechanicSettlementError(f"{label} has unexpected fields: {unexpected}")
    missing = sorted(fields - set(value))
    if missing:
        raise MechanicSettlementError(f"{label} is missing required fields: {missing}")


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise MechanicSettlementError(f"{label} must be a sha256 content fingerprint")
    return value


def _optional_fingerprint(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, label)


def _reference(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _REFERENCE_RE.fullmatch(value) is None
    ):
        raise MechanicSettlementError(f"{label} must be a stable lowercase reference")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MechanicSettlementError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise MechanicSettlementError(f"{label} must be at least {minimum}")
    return value


def _target_post_state(value: object) -> dict[str, Any]:
    row = _mapping(value, "target_post_state")
    _exact_fields(row, {"combatant_id", "hp"}, "target_post_state")
    target = _reference(row["combatant_id"], "target_post_state.combatant_id")
    hp = _mapping(row["hp"], "target_post_state.hp")
    _exact_fields(hp, {"cur", "max"}, "target_post_state.hp")
    current = _integer(hp["cur"], "target_post_state.hp.cur", minimum=0)
    maximum = _integer(hp["max"], "target_post_state.hp.max", minimum=1)
    if current > maximum:
        raise MechanicSettlementError("target_post_state current HP exceeds maximum HP")
    return {"combatant_id": target, "hp": {"cur": current, "max": maximum}}


def _opening_requirements(value: object) -> dict[str, bool]:
    row = _mapping(value, "opening_requirements")
    fields = {"target_admission", "scene_transition"}
    _exact_fields(row, fields, "opening_requirements")
    if any(not isinstance(row[field], bool) for field in fields):
        raise MechanicSettlementError("opening_requirements values must be booleans")
    return {field: bool(row[field]) for field in sorted(fields)}


def _group_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = str(row["kind"])
    if kind == "check":
        suffix = (str(row["actor_id"]), str(row["capability_id"]))
    elif kind == "hp":
        suffix = (str(row["subject_id"]),)
    elif kind == "cost":
        suffix = (str(row["subject_id"]), str(row["resource_id"]))
    elif kind == "mastery":
        suffix = (str(row["subject_id"]), str(row["capability_id"]))
    elif kind == "cooldown":
        suffix = (str(row["subject_id"]), str(row["ability_id"]))
    elif kind == "consequence":
        suffix = (str(row["subject_id"]), str(row["effect_id"]))
    elif kind == "target_admission":
        suffix = (str(row["combatant_id"]), str(row["target_entity_id"]))
    else:
        suffix = (str(row["subject_id"]),)
    return (_GROUP_ORDER[kind], *suffix)


def _applied_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = str(row["kind"])
    if kind == "hp":
        suffix = (str(row["subject_id"]),)
    elif kind == "cost":
        suffix = (str(row["subject_id"]), str(row["resource_id"]))
    elif kind == "mastery":
        suffix = (str(row["subject_id"]), str(row["capability_id"]))
    elif kind == "cooldown":
        suffix = (str(row["subject_id"]), str(row["ability_id"]))
    elif kind == "consequence":
        suffix = (str(row["subject_id"]), str(row["effect_id"]))
    elif kind == "target_admission":
        suffix = (str(row["subject_id"]), str(row["entity_id"]))
    else:
        suffix = (str(row["subject_id"]),)
    return (_APPLIED_ORDER[kind], *suffix)


def _outcome_for_hp(delta: int, post: int) -> str:
    if delta == 0:
        if post == 0:
            raise MechanicSettlementError("a miss cannot target an already defeated combatant")
        return "miss"
    if delta > 0:
        raise MechanicSettlementError("weapon_attack/1 cannot heal its target")
    return "defeat" if post == 0 else "hit"


def _check_quality_matches_hp(quality: str, delta: int) -> None:
    if quality in {"crit_fail", "fail"} and delta != 0:
        raise MechanicSettlementError("a failed weapon check cannot carry HP damage")
    if quality in {"partial", "success", "crit_success"} and delta >= 0:
        raise MechanicSettlementError("a landed weapon check requires negative HP damage")


def _validate_candidate_frame_for_contract(
    frame_value: object,
    binding_value: object,
    *,
    contract_id: str,
    action_class: str | None,
    require_target: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        frame = validate_action_frame_snapshot(frame_value)
        binding = validate_meaning_binding(binding_value)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise MechanicSettlementError(
            f"{contract_id} frame or binding is malformed"
        ) from exc

    if frame.get("schema") != ACTION_FRAME_SCHEMA:
        raise MechanicSettlementError(f"{contract_id} requires a V3 semantic action frame")
    if action_class is not None and frame.get("action_class") != action_class:
        raise MechanicSettlementError(
            f"semantic action frame is not a {action_class.replace('_', ' ')}"
        )
    if binding.get("mechanic_disposition") != "candidate" \
            or binding.get("request_disposition") != "direct_player_request" \
            or binding.get("constraint_integrity") != "valid":
        raise MechanicSettlementError("semantic meaning binding is not a mechanic candidate")
    if frame.get("polarity") != "positive" \
            or frame.get("modality") not in {"actual", "command"} \
            or frame.get("time_scope") != "current" \
            or frame.get("ambiguity"):
        raise MechanicSettlementError("semantic action frame is not direct, current, and unambiguous")

    actor = _reference(frame.get("actor_id"), "semantic action actor")
    _reference(frame.get("capability_id"), "semantic action capability")
    target = frame.get("target_entity_id")
    if require_target:
        _reference(target, "semantic action target")
    elif target is not None:
        _reference(target, "semantic action target")
    if not actor:
        raise MechanicSettlementError("semantic action frame has no actor")
    _fingerprint(frame.get("fingerprint"), "semantic action frame reference")
    meaning_ref = _fingerprint(frame.get("meaning_ref"), "semantic meaning reference")
    binding_ref = _fingerprint(binding.get("fingerprint"), "semantic binding reference")
    context = _mapping(frame.get("context_frame"), "semantic context frame")
    exact = (
        frame.get("meaning_binding_ref") == binding_ref
        and binding.get("meaning_ref") == meaning_ref
        and frame.get("event_node_id") == binding.get("event_node_id")
        and binding.get("source_fingerprint") == context.get("source_fingerprint")
        and binding.get("event_span") == [context.get("span_start"), context.get("span_end")]
    )
    if not exact:
        raise MechanicSettlementError("semantic frame and candidate binding do not describe one event")
    return frame, binding


def _validate_candidate_frame(
    frame_value: object,
    binding_value: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return _validate_candidate_frame_for_contract(
        frame_value,
        binding_value,
        contract_id=WEAPON_ATTACK_CONTRACT,
        action_class="weapon_attack",
        require_target=True,
    )


def _validate_skill_check_candidate_frame(
    frame_value: object,
    binding_value: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    frame, binding = _validate_candidate_frame_for_contract(
        frame_value,
        binding_value,
        contract_id=SKILL_CHECK_CONTRACT,
        action_class=None,
        require_target=False,
    )
    if frame.get("action_class") in NON_SKILL_CHECK_ACTION_CLASSES:
        raise MechanicSettlementError(
            "skill_check/1 cannot borrow a frame owned by an impact settlement"
        )
    return frame, binding


def _validate_combat_opening_candidate_frame(
    frame_value: object,
    binding_value: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return _validate_candidate_frame_for_contract(
        frame_value,
        binding_value,
        contract_id=COMBAT_OPENING_CONTRACT,
        action_class="combat_opening",
        require_target=True,
    )


def _validate_group_row(
    value: object,
    index: int,
    *,
    frame: Mapping[str, Any],
    contract_id: str = WEAPON_ATTACK_CONTRACT,
) -> dict[str, Any]:
    label = f"accepted_group[{index}]"
    row = _mapping(value, label)
    kind = row.get("kind")
    if contract_id == WEAPON_ATTACK_CONTRACT:
        allowed = set(_GROUP_FIELDS)
    elif contract_id == SKILL_CHECK_CONTRACT:
        allowed = {"check", "cost", "mastery", "cooldown", "consequence"}
    elif contract_id == COMBAT_OPENING_CONTRACT:
        allowed = {"target_admission", "scene_transition"}
    else:
        allowed = set()
    if kind not in allowed:
        raise MechanicSettlementError(f"{label}.kind is not admitted by {contract_id}")
    _exact_fields(row, _GROUP_FIELDS[str(kind)], label)
    if row["frame_ref"] != frame["fingerprint"]:
        raise MechanicSettlementError(f"{label} belongs to a different semantic frame")
    if row["meaning_ref"] != frame["meaning_ref"]:
        raise MechanicSettlementError(f"{label} belongs to a different semantic meaning")

    actor = str(frame["actor_id"])
    capability = str(frame["capability_id"])
    target = frame.get("target_entity_id")
    if kind == "check":
        if row["actor_id"] != actor or row["capability_id"] != capability \
                or row["target_entity_id"] != target:
            raise MechanicSettlementError("accepted check does not match the semantic action roles")
        _reference(row["actor_id"], f"{label}.actor_id")
        _reference(row["capability_id"], f"{label}.capability_id")
        if row["target_entity_id"] is not None:
            _reference(row["target_entity_id"], f"{label}.target_entity_id")
        _integer(row["result"], f"{label}.result")
        if row["outcome_quality"] not in OUTCOME_QUALITIES:
            raise MechanicSettlementError(f"{label}.outcome_quality is invalid")
    elif kind == "hp":
        if row["subject_id"] != target:
            raise MechanicSettlementError("accepted HP change targets a different combatant")
        _reference(row["subject_id"], f"{label}.subject_id")
        pre = _integer(row["pre"], f"{label}.pre", minimum=1)
        delta = _integer(row["delta"], f"{label}.delta")
        post = _integer(row["post"], f"{label}.post", minimum=0)
        maximum = _integer(row["maximum"], f"{label}.maximum", minimum=1)
        if pre > maximum or post > maximum or pre + delta != post:
            raise MechanicSettlementError("accepted HP change is not one exact reducer transition")
        _outcome_for_hp(delta, post)
    elif kind == "cost":
        if row["subject_id"] != actor:
            raise MechanicSettlementError("accepted cost belongs to a different actor")
        _reference(row["subject_id"], f"{label}.subject_id")
        _reference(row["resource_id"], f"{label}.resource_id")
        pre = _integer(row["pre"], f"{label}.pre", minimum=0)
        delta = _integer(row["delta"], f"{label}.delta")
        post = _integer(row["post"], f"{label}.post", minimum=0)
        maximum = _integer(row["maximum"], f"{label}.maximum", minimum=1)
        if delta >= 0 or pre > maximum or post > maximum or pre + delta != post:
            raise MechanicSettlementError("accepted cost is not one exact resource spend")
    elif kind == "mastery":
        if row["subject_id"] != actor or row["capability_id"] != capability:
            raise MechanicSettlementError("accepted mastery change belongs to a different action")
        _reference(row["subject_id"], f"{label}.subject_id")
        _reference(row["capability_id"], f"{label}.capability_id")
        pre = _integer(row["pre"], f"{label}.pre", minimum=0)
        delta = _integer(row["delta"], f"{label}.delta", minimum=1)
        post = _integer(row["post"], f"{label}.post", minimum=0)
        if pre + delta != post:
            raise MechanicSettlementError("accepted mastery change is not one exact transition")
    elif kind == "cooldown":
        if row["subject_id"] != actor:
            raise MechanicSettlementError("accepted cooldown belongs to a different actor")
        _reference(row["subject_id"], f"{label}.subject_id")
        _reference(row["ability_id"], f"{label}.ability_id")
        pre = _integer(row["pre"], f"{label}.pre", minimum=0)
        post = _integer(row["post"], f"{label}.post", minimum=0)
        if post <= pre:
            raise MechanicSettlementError("accepted cooldown must advance its ready turn")
    elif kind == "consequence":
        if row["subject_id"] != actor:
            raise MechanicSettlementError("accepted consequence belongs to a different actor")
        _reference(row["subject_id"], f"{label}.subject_id")
        _reference(row["effect_id"], f"{label}.effect_id")
        before = _optional_fingerprint(row["pre_state_ref"], f"{label}.pre_state_ref")
        after = _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
        if before == after:
            raise MechanicSettlementError("accepted consequence did not change its exact state")
    elif kind == "target_admission":
        _reference(row["target_entity_id"], f"{label}.target_entity_id")
        _reference(row["combatant_id"], f"{label}.combatant_id")
        if row["pre_state_ref"] is not None:
            raise MechanicSettlementError("target admission requires the combatant to be absent")
        _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
    else:
        if row["subject_id"] != "scene":
            raise MechanicSettlementError("accepted scene transition must target the scene ledger")
        before = _fingerprint(row["pre_state_ref"], f"{label}.pre_state_ref")
        after = _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
        if before == after:
            raise MechanicSettlementError("accepted scene transition did not change scene state")
    return deepcopy(dict(row))


def _canonical_accepted_group(
    values: Iterable[Mapping[str, Any]],
    *,
    frame: Mapping[str, Any],
    opening_requirements: Mapping[str, bool] | None = None,
    contract_id: str = WEAPON_ATTACK_CONTRACT,
) -> list[dict[str, Any]]:
    try:
        raw = list(values)
    except TypeError as exc:
        raise MechanicSettlementError("accepted_group must be iterable") from exc
    rows = [
        _validate_group_row(
            value, index, frame=frame, contract_id=contract_id,
        )
        for index, value in enumerate(raw)
    ]
    rows.sort(key=_group_key)
    keys = [_group_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise MechanicSettlementError("accepted group repeats one mechanic member")

    checks = [row for row in rows if row["kind"] == "check"]
    hp_rows = [row for row in rows if row["kind"] == "hp"]
    consequences = [row for row in rows if row["kind"] == "consequence"]
    if contract_id == SKILL_CHECK_CONTRACT:
        if len(checks) != 1:
            raise MechanicSettlementError("skill_check/1 needs exactly one check member")
        quality = str(checks[0]["outcome_quality"])
        if quality == "crit_fail" and len(consequences) != 1:
            raise MechanicSettlementError(
                "a critical failure needs its exact consequence member"
            )
        if quality != "crit_fail" and consequences:
            raise MechanicSettlementError(
                "only a critical failure may carry a consequence member"
            )
        return rows
    if contract_id == COMBAT_OPENING_CONTRACT:
        if checks or hp_rows or consequences:
            raise MechanicSettlementError(
                "combat_opening/1 cannot carry a check, HP result, or consequence"
            )
        admissions = [row for row in rows if row["kind"] == "target_admission"]
        primary_admissions = [
            row for row in admissions
            if row["target_entity_id"] == frame.get("target_entity_id")
        ]
        if not 1 <= len(admissions) <= _WEAPON_OPENING_ADMISSION_CAP \
                or len(primary_admissions) != 1:
            raise MechanicSettlementError(
                "combat_opening/1 needs one to three admissions and one exact frame target"
            )
        scene_count = sum(row["kind"] == "scene_transition" for row in rows)
        expected_scene = (
            1 if opening_requirements and opening_requirements["scene_transition"] else 0
        )
        if scene_count != expected_scene:
            raise MechanicSettlementError(
                "combat_opening/1 scene transition does not match its pre-state requirement"
            )
        return rows
    if len(checks) != 1 or len(hp_rows) != 1:
        raise MechanicSettlementError("weapon_attack/1 needs exactly one check and one HP member")
    quality = str(checks[0]["outcome_quality"])
    _check_quality_matches_hp(quality, int(hp_rows[0]["delta"]))
    if quality == "crit_fail" and len(consequences) != 1:
        raise MechanicSettlementError("a critical failure needs its exact consequence member")
    if quality != "crit_fail" and consequences:
        raise MechanicSettlementError("only a critical failure may carry a consequence member")
    admissions = [row for row in rows if row["kind"] == "target_admission"]
    expected_admission = bool(
        opening_requirements and opening_requirements["target_admission"]
    )
    primary_admissions = [
        row for row in admissions
        if row["target_entity_id"] == frame.get("target_entity_id")
        and row["combatant_id"] == frame.get("target_entity_id")
    ]
    if (
        (expected_admission and not 1 <= len(admissions) <= _WEAPON_OPENING_ADMISSION_CAP)
        or (not expected_admission and admissions)
        or (expected_admission and len(primary_admissions) != 1)
    ):
        raise MechanicSettlementError(
            "accepted group target_admission count does not match its opening requirement"
        )
    scene_count = sum(row["kind"] == "scene_transition" for row in rows)
    expected_scene = 1 if opening_requirements and opening_requirements["scene_transition"] else 0
    if scene_count != expected_scene:
        raise MechanicSettlementError(
            "accepted group scene_transition count does not match its opening requirement"
        )
    return rows


def _applied_change(row: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = str(row["kind"])
    if kind == "check":
        return None
    if kind == "hp":
        return {
            "kind": "hp",
            "subject_id": row["subject_id"],
            "delta": row["delta"],
            "post": row["post"],
        }
    if kind == "cost":
        return {
            "kind": "cost",
            "subject_id": row["subject_id"],
            "resource_id": row["resource_id"],
            "delta": row["delta"],
            "post": row["post"],
        }
    if kind == "mastery":
        return {
            "kind": "mastery",
            "subject_id": row["subject_id"],
            "capability_id": row["capability_id"],
            "delta": row["delta"],
            "post": row["post"],
        }
    if kind == "cooldown":
        return {
            "kind": "cooldown",
            "subject_id": row["subject_id"],
            "ability_id": row["ability_id"],
            "delta": int(row["post"]) - int(row["pre"]),
            "post": row["post"],
        }
    if kind == "consequence":
        return {
            "kind": "consequence",
            "subject_id": row["subject_id"],
            "effect_id": row["effect_id"],
            "post_state_ref": row["post_state_ref"],
        }
    if kind == "target_admission":
        return {
            "kind": "target_admission",
            "subject_id": row["combatant_id"],
            "entity_id": row["target_entity_id"],
            "post_state_ref": row["post_state_ref"],
        }
    return {
        "kind": "scene_transition",
        "subject_id": row["subject_id"],
        "post_state_ref": row["post_state_ref"],
    }


def _settlement_identity(
    *,
    contract_id: str,
    frame_ref: str,
    meaning_ref: str,
    target_entity_id: str | None,
) -> dict[str, Any]:
    return {
        "schema": MECHANIC_SETTLEMENT_IDENTITY_SCHEMA,
        "contract_id": contract_id,
        "frame_ref": frame_ref,
        "meaning_ref": meaning_ref,
        "target_entity_id": target_entity_id,
    }


def _request_fingerprint(receipt: Mapping[str, Any]) -> str:
    return content_fingerprint({
        "schema": MECHANIC_SETTLEMENT_REQUEST_SCHEMA,
        "settlement_ref": receipt["settlement_ref"],
        "requirement_fingerprint": receipt["requirement_fingerprint"],
        "accepted_group_fingerprint": receipt["accepted_group_fingerprint"],
    })


def weapon_attack_settlement_ref(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
) -> str:
    """Return the stable pre-apply identity used to detect retries before any mutation."""
    frame, _binding = _validate_candidate_frame(frame_value, binding_value)
    return content_fingerprint(_settlement_identity(
        contract_id=WEAPON_ATTACK_CONTRACT,
        frame_ref=str(frame["fingerprint"]),
        meaning_ref=str(frame["meaning_ref"]),
        target_entity_id=str(frame["target_entity_id"]),
    ))


def skill_check_settlement_ref(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
) -> str:
    """Return the stable identity for one exact non-impact skill resolution."""
    frame, _binding = _validate_skill_check_candidate_frame(frame_value, binding_value)
    return content_fingerprint(_settlement_identity(
        contract_id=SKILL_CHECK_CONTRACT,
        frame_ref=str(frame["fingerprint"]),
        meaning_ref=str(frame["meaning_ref"]),
        # The frame fingerprint already seals any optional referent.  This contract does not
        # claim a target-state mutation, so target identity is deliberately absent here too.
        target_entity_id=None,
    ))


def combat_opening_settlement_ref(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
) -> str:
    """Return the stable identity for one exact code-owned combat opening."""
    frame, _binding = _validate_combat_opening_candidate_frame(frame_value, binding_value)
    return content_fingerprint(_settlement_identity(
        contract_id=COMBAT_OPENING_CONTRACT,
        frame_ref=str(frame["fingerprint"]),
        meaning_ref=str(frame["meaning_ref"]),
        target_entity_id=str(frame["target_entity_id"]),
    ))


def validate_mechanic_settlement(value: object) -> dict[str, Any]:
    """Validate and detach one complete ``mechanic-settlement/1`` receipt."""
    receipt = _mapping(value, "mechanic settlement")
    _exact_fields(receipt, _RECEIPT_FIELDS, "mechanic settlement")
    if receipt["schema"] != MECHANIC_SETTLEMENT_SCHEMA:
        raise MechanicSettlementError("unsupported mechanic settlement schema")
    contract_id = receipt["contract_id"]
    if contract_id not in {
        WEAPON_ATTACK_CONTRACT,
        SKILL_CHECK_CONTRACT,
        COMBAT_OPENING_CONTRACT,
    }:
        raise MechanicSettlementError("unsupported mechanic settlement contract")
    for field in (
        "settlement_ref",
        "frame_ref",
        "meaning_ref",
        "requirement_fingerprint",
        "accepted_group_fingerprint",
        "receipt_fingerprint",
    ):
        _fingerprint(receipt[field], f"mechanic settlement {field}")
    if receipt["outcome"] not in OUTCOMES:
        raise MechanicSettlementError("mechanic settlement outcome is invalid")
    if receipt["outcome_quality"] not in OUTCOME_QUALITIES:
        raise MechanicSettlementError("mechanic settlement outcome_quality is invalid")

    if contract_id in {WEAPON_ATTACK_CONTRACT, COMBAT_OPENING_CONTRACT}:
        target: dict[str, Any] | None = _target_post_state(receipt["target_post_state"])
        if contract_id == WEAPON_ATTACK_CONTRACT and receipt["outcome"] not in WEAPON_OUTCOMES:
            raise MechanicSettlementError("weapon settlement outcome is invalid")
        if contract_id == COMBAT_OPENING_CONTRACT and (
            receipt["outcome"] != "resolved" or receipt["outcome_quality"] != "automatic"
        ):
            raise MechanicSettlementError(
                "combat_opening/1 must record one automatic resolved transition"
            )
    else:
        target = None
        if receipt["target_post_state"] is not None:
            raise MechanicSettlementError(
                "skill_check/1 cannot claim a target_post_state"
            )
        if receipt["outcome"] != "resolved":
            raise MechanicSettlementError("skill_check/1 outcome must be resolved")
    changes = receipt["applied_changes"]
    if not isinstance(changes, list):
        raise MechanicSettlementError("mechanic settlement applied_changes must be a list")
    clean_changes: list[dict[str, Any]] = []
    for index, value_row in enumerate(changes):
        label = f"applied_changes[{index}]"
        row = _mapping(value_row, label)
        kind = row.get("kind")
        if kind not in _APPLIED_FIELDS:
            raise MechanicSettlementError(f"{label}.kind is invalid")
        if contract_id == SKILL_CHECK_CONTRACT and kind not in {
            "cost", "mastery", "cooldown", "consequence",
        }:
            raise MechanicSettlementError(
                f"{label}.kind is not admitted by skill_check/1"
            )
        if contract_id == COMBAT_OPENING_CONTRACT and kind not in {
            "target_admission", "scene_transition",
        }:
            raise MechanicSettlementError(
                f"{label}.kind is not admitted by combat_opening/1"
            )
        _exact_fields(row, _APPLIED_FIELDS[str(kind)], label)
        _reference(row["subject_id"], f"{label}.subject_id")
        if kind == "hp":
            if target is None:
                raise MechanicSettlementError("non-impact settlement cannot carry HP change")
            delta = _integer(row["delta"], f"{label}.delta")
            post = _integer(row["post"], f"{label}.post", minimum=0)
            if row["subject_id"] != target["combatant_id"] or post != target["hp"]["cur"]:
                raise MechanicSettlementError("settled HP row does not match target_post_state")
            if -delta > target["hp"]["max"]:
                raise MechanicSettlementError("settled HP damage exceeds the target maximum")
            outcome = _outcome_for_hp(delta, post)
        elif kind == "cost":
            _reference(row["resource_id"], f"{label}.resource_id")
            if _integer(row["delta"], f"{label}.delta") >= 0:
                raise MechanicSettlementError("settled cost delta must be negative")
            _integer(row["post"], f"{label}.post", minimum=0)
        elif kind == "mastery":
            _reference(row["capability_id"], f"{label}.capability_id")
            _integer(row["delta"], f"{label}.delta", minimum=1)
            _integer(row["post"], f"{label}.post", minimum=0)
        elif kind == "cooldown":
            _reference(row["ability_id"], f"{label}.ability_id")
            _integer(row["delta"], f"{label}.delta", minimum=1)
            _integer(row["post"], f"{label}.post", minimum=0)
        elif kind == "consequence":
            _reference(row["effect_id"], f"{label}.effect_id")
            _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
        elif kind == "target_admission":
            if target is None:
                raise MechanicSettlementError(
                    "settlement without a target cannot carry target admission"
                )
            _reference(row["entity_id"], f"{label}.entity_id")
            _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
        else:
            if target is None:
                raise MechanicSettlementError(
                    "settlement without a target cannot carry a scene transition"
                )
            if row["subject_id"] != "scene":
                raise MechanicSettlementError(
                    "settled scene transition must target the scene ledger"
                )
            _fingerprint(row["post_state_ref"], f"{label}.post_state_ref")
        clean_changes.append(deepcopy(dict(row)))

    if clean_changes != sorted(clean_changes, key=_applied_key):
        raise MechanicSettlementError("mechanic settlement applied_changes are not canonical")
    keys = [_applied_key(row) for row in clean_changes]
    if len(keys) != len(set(keys)):
        raise MechanicSettlementError("mechanic settlement repeats one applied change")
    hp_rows = [row for row in clean_changes if row["kind"] == "hp"]
    consequences = [row for row in clean_changes if row["kind"] == "consequence"]
    if contract_id == WEAPON_ATTACK_CONTRACT:
        if len(hp_rows) != 1:
            raise MechanicSettlementError("mechanic settlement needs exactly one HP result")
        if receipt["outcome"] != outcome:
            raise MechanicSettlementError("mechanic settlement outcome disagrees with settled HP")
        _check_quality_matches_hp(str(receipt["outcome_quality"]), int(hp_rows[0]["delta"]))
        admissions = [row for row in clean_changes if row["kind"] == "target_admission"]
        primary_admissions = [
            row for row in admissions
            if row["subject_id"] == target["combatant_id"]
            and row["entity_id"] == target["combatant_id"]
        ]
        if len(admissions) > _WEAPON_OPENING_ADMISSION_CAP \
                or (admissions and len(primary_admissions) != 1):
            raise MechanicSettlementError(
                "settled target admissions do not contain one exact semantic target"
            )
    elif contract_id == COMBAT_OPENING_CONTRACT:
        if hp_rows or consequences:
            raise MechanicSettlementError(
                "combat_opening/1 cannot carry HP or consequence changes"
            )
        admissions = [row for row in clean_changes if row["kind"] == "target_admission"]
        primary_admissions = [
            row for row in admissions if row["entity_id"] == target["combatant_id"]
        ]
        scenes = [row for row in clean_changes if row["kind"] == "scene_transition"]
        if not 1 <= len(admissions) <= _WEAPON_OPENING_ADMISSION_CAP \
                or len(primary_admissions) != 1 or len(scenes) > 1:
            raise MechanicSettlementError(
                "combat opening receipt lacks exact admissions or scene cardinality"
            )
    if contract_id != COMBAT_OPENING_CONTRACT \
            and receipt["outcome_quality"] == "crit_fail" and len(consequences) != 1:
        raise MechanicSettlementError("critical failure settlement lacks its consequence")
    if receipt["outcome_quality"] != "crit_fail" and consequences:
        raise MechanicSettlementError("non-critical settlement carries a consequence")

    identity = _settlement_identity(
        contract_id=str(contract_id),
        frame_ref=str(receipt["frame_ref"]),
        meaning_ref=str(receipt["meaning_ref"]),
        target_entity_id=(target["combatant_id"] if target is not None else None),
    )
    if receipt["settlement_ref"] != content_fingerprint(identity):
        raise MechanicSettlementError("mechanic settlement identity mismatch")
    payload = {key: receipt[key] for key in receipt if key != "receipt_fingerprint"}
    if receipt["receipt_fingerprint"] != content_fingerprint(payload):
        raise MechanicSettlementError("mechanic settlement receipt fingerprint mismatch")
    return deepcopy(dict(receipt))


def validate_mechanic_settlement_row(value: object) -> dict[str, Any]:
    """Validate the exact persistence row consumed by ``Store.journal_with_receipts``."""
    row = _mapping(value, "mechanic settlement persistence row")
    _exact_fields(row, _STORE_ROW_FIELDS, "mechanic settlement persistence row")
    receipt = validate_mechanic_settlement(row["receipt"])
    for field in _STORE_ROW_FIELDS - {"request_fingerprint", "receipt"}:
        if row[field] != receipt[field]:
            raise MechanicSettlementError(f"persistence row {field} disagrees with its receipt")
    _fingerprint(row["request_fingerprint"], "mechanic settlement request_fingerprint")
    if row["request_fingerprint"] != _request_fingerprint(receipt):
        raise MechanicSettlementError("mechanic settlement request fingerprint mismatch")
    return deepcopy(dict(row))


def _store_row_for_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "settlement_ref": receipt["settlement_ref"],
        "contract_id": receipt["contract_id"],
        "frame_ref": receipt["frame_ref"],
        "meaning_ref": receipt["meaning_ref"],
        "outcome": receipt["outcome"],
        "outcome_quality": receipt["outcome_quality"],
        "requirement_fingerprint": receipt["requirement_fingerprint"],
        "request_fingerprint": _request_fingerprint(receipt),
        "accepted_group_fingerprint": receipt["accepted_group_fingerprint"],
        "receipt_fingerprint": receipt["receipt_fingerprint"],
        "receipt": receipt,
    }
    return validate_mechanic_settlement_row(row)


def build_combat_opening_settlement(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
    *,
    accepted_group: Iterable[Mapping[str, Any]],
    target_post_state: Mapping[str, Any],
    opening_requirements: Mapping[str, bool],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seal one complete code-owned combat opening and every admitted foe."""
    frame, binding = _validate_combat_opening_candidate_frame(frame_value, binding_value)
    target = _target_post_state(target_post_state)
    if target["combatant_id"] != frame["target_entity_id"]:
        raise MechanicSettlementError(
            "combat opening target_post_state belongs to another semantic target"
        )
    opening = _opening_requirements(opening_requirements)
    if not opening["target_admission"]:
        raise MechanicSettlementError(
            "combat_opening/1 requires a previously absent primary combatant"
        )
    rows = _canonical_accepted_group(
        accepted_group,
        frame=frame,
        opening_requirements=opening,
        contract_id=COMBAT_OPENING_CONTRACT,
    )
    requirement = {
        "schema": COMBAT_OPENING_REQUIREMENT_SCHEMA,
        "contract_id": COMBAT_OPENING_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "meaning_binding_ref": binding["fingerprint"],
        "event_node_id": frame["event_node_id"],
        "actor_id": frame["actor_id"],
        "capability_id": frame["capability_id"],
        "target_entity_id": frame["target_entity_id"],
        "action_class": frame["action_class"],
        "required_group_kinds": ["target_admission"],
        "optional_group_kinds": ["scene_transition"],
        "opening_requirements": opening,
    }
    group_payload = {
        "schema": COMBAT_OPENING_GROUP_SCHEMA,
        "contract_id": COMBAT_OPENING_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "entries": rows,
    }
    applied = [change for row in rows if (change := _applied_change(row)) is not None]
    applied.sort(key=_applied_key)
    payload = {
        "schema": MECHANIC_SETTLEMENT_SCHEMA,
        "settlement_ref": combat_opening_settlement_ref(frame, binding),
        "contract_id": COMBAT_OPENING_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "requirement_fingerprint": content_fingerprint(requirement),
        "accepted_group_fingerprint": content_fingerprint(group_payload),
        "outcome": "resolved",
        "outcome_quality": "automatic",
        "applied_changes": applied,
        "target_post_state": target,
    }
    receipt = validate_mechanic_settlement({
        **payload,
        "receipt_fingerprint": content_fingerprint(payload),
    })
    return receipt, _store_row_for_receipt(receipt)


def build_skill_check_settlement(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
    *,
    accepted_group: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seal one complete accepted ``skill_check/1`` resolution and its Store row."""
    frame, binding = _validate_skill_check_candidate_frame(frame_value, binding_value)
    rows = _canonical_accepted_group(
        accepted_group,
        frame=frame,
        contract_id=SKILL_CHECK_CONTRACT,
    )
    check = next(row for row in rows if row["kind"] == "check")
    requirement = {
        "schema": SKILL_CHECK_REQUIREMENT_SCHEMA,
        "contract_id": SKILL_CHECK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "meaning_binding_ref": binding["fingerprint"],
        "event_node_id": frame["event_node_id"],
        "actor_id": frame["actor_id"],
        "capability_id": frame["capability_id"],
        "target_entity_id": frame.get("target_entity_id"),
        "action_class": frame["action_class"],
        "required_group_kinds": ["check"],
        "optional_group_kinds": ["cost", "mastery", "cooldown", "consequence"],
    }
    group_payload = {
        "schema": SKILL_CHECK_GROUP_SCHEMA,
        "contract_id": SKILL_CHECK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "entries": rows,
    }
    applied = [change for row in rows if (change := _applied_change(row)) is not None]
    applied.sort(key=_applied_key)
    payload = {
        "schema": MECHANIC_SETTLEMENT_SCHEMA,
        "settlement_ref": skill_check_settlement_ref(frame, binding),
        "contract_id": SKILL_CHECK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "requirement_fingerprint": content_fingerprint(requirement),
        "accepted_group_fingerprint": content_fingerprint(group_payload),
        "outcome": "resolved",
        "outcome_quality": check["outcome_quality"],
        "applied_changes": applied,
        "target_post_state": None,
    }
    receipt = validate_mechanic_settlement({
        **payload,
        "receipt_fingerprint": content_fingerprint(payload),
    })
    return receipt, _store_row_for_receipt(receipt)


def build_weapon_attack_settlement(
    frame_value: Mapping[str, Any],
    binding_value: Mapping[str, Any],
    *,
    accepted_group: Iterable[Mapping[str, Any]],
    target_post_state: Mapping[str, Any],
    opening_requirements: Mapping[str, bool],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seal one complete accepted ``weapon_attack/1`` group and its Store row.

    Group rows contain post-reducer facts, including pre/post values for every mutation.  The
    builder rejects partial groups, cross-frame members, independently interpreted outcomes, and
    any critical failure whose code-owned consequence is not included.
    """
    frame, binding = _validate_candidate_frame(frame_value, binding_value)
    target = _target_post_state(target_post_state)
    if target["combatant_id"] != frame["target_entity_id"]:
        raise MechanicSettlementError("target_post_state belongs to a different semantic target")
    opening = _opening_requirements(opening_requirements)
    rows = _canonical_accepted_group(
        accepted_group,
        frame=frame,
        opening_requirements=opening,
    )
    check = next(row for row in rows if row["kind"] == "check")
    hp = next(row for row in rows if row["kind"] == "hp")
    if target["hp"] != {"cur": hp["post"], "max": hp["maximum"]}:
        raise MechanicSettlementError("target_post_state does not match the accepted HP transition")

    requirement = {
        "schema": WEAPON_ATTACK_REQUIREMENT_SCHEMA,
        "contract_id": WEAPON_ATTACK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "meaning_binding_ref": binding["fingerprint"],
        "event_node_id": frame["event_node_id"],
        "actor_id": frame["actor_id"],
        "capability_id": frame["capability_id"],
        "target_entity_id": frame["target_entity_id"],
        "action_class": frame["action_class"],
        "required_group_kinds": ["check", "hp"],
        "optional_group_kinds": [
            "cost",
            "mastery",
            "cooldown",
            "consequence",
            "target_admission",
            "scene_transition",
        ],
        "opening_requirements": opening,
    }
    group_payload = {
        "schema": WEAPON_ATTACK_GROUP_SCHEMA,
        "contract_id": WEAPON_ATTACK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "entries": rows,
    }
    applied = [change for row in rows if (change := _applied_change(row)) is not None]
    applied.sort(key=_applied_key)
    payload = {
        "schema": MECHANIC_SETTLEMENT_SCHEMA,
        "settlement_ref": weapon_attack_settlement_ref(frame, binding),
        "contract_id": WEAPON_ATTACK_CONTRACT,
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "requirement_fingerprint": content_fingerprint(requirement),
        "accepted_group_fingerprint": content_fingerprint(group_payload),
        "outcome": _outcome_for_hp(int(hp["delta"]), int(hp["post"])),
        "outcome_quality": check["outcome_quality"],
        "applied_changes": applied,
        "target_post_state": target,
    }
    receipt = validate_mechanic_settlement({
        **payload,
        "receipt_fingerprint": content_fingerprint(payload),
    })
    return receipt, _store_row_for_receipt(receipt)
