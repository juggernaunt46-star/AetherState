"""Content-free realization contract for the main narrator.

The narrator should not need to know which recognition or alignment layer won.  It receives only
settled event truth, explicitly unresolved events, attributed/non-current context, and closed
inference prohibitions.  Exact mechanic numbers remain in reducer receipts and HUD state.

This module owns the strict ``narrator-realization/1`` data shape, deterministic fingerprinting,
the one qualitative HP-impact projection currently admitted by the weapon-attack adapter, and the
fail-closed state-journal projector consumed at the final narrator directive boundary.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from .capability_glossary import content_fingerprint, normalize_phrase
from .mechanic_settlement import (
    COMBAT_OPENING_CONTRACT,
    NON_SKILL_CHECK_ACTION_CLASSES,
    SKILL_CHECK_CONTRACT,
    WEAPON_ATTACK_CONTRACT,
    validate_mechanic_settlement,
)


NARRATOR_REALIZATION_SCHEMA = "narrator-realization/1"
PLAIN_OUTCOME_CONTRACT_PREFIX = "PLAIN OUTCOME CONTRACT: "
PLAIN_OUTCOME_CONTRACT_END = "[END PLAIN OUTCOME CONTRACT]"
WEAPON_ATTACK_REALIZATION_ADAPTER = "narrator.weapon-attack/1"
SKILL_CHECK_REALIZATION_ADAPTER = "narrator.skill-check/1"
COMBAT_OPENING_REALIZATION_ADAPTER = "narrator.combat-opening/1"

OUTCOME_QUALITIES = (
    "crit_fail",
    "fail",
    "partial",
    "success",
    "crit_success",
    "automatic",
)
IMPACT_KINDS = ("none", "harm")
IMPACT_MAGNITUDES = ("none", "modest", "solid", "severe", "devastating", "decisive")
TARGET_STATES = ("active", "defeated", "not_applicable")
UNRESOLVED_REASONS = (
    "no_complete_settlement",
    "semantic_ambiguity",
    "ineligible",
    "pending_intent",
)
ALLOWED_STAGES = ("attempt_only", "visible_tell_only", "no_performance", "context_only")
ASSERTION_STATUSES = ("asserted", "embedded", "ambiguous")
EMBEDDING_KINDS = (
    "none",
    "reported_or_testified",
    "remembered",
    "believed_or_interpreted",
    "observed",
    "quoted",
    "attributed",
    "mixed",
)
HOLDER_ROLES = ("none", "speaker", "experiencer", "observer", "attributor", "mixed")
POLARITIES = ("positive", "negative", "unknown")
MODALITIES = ("actual", "command", "question", "hypothetical", "possible", "unknown")
TIME_SCOPES = ("current", "past", "future", "atemporal", "unknown")
PERFORMANCE_MODES = (
    "may_perform",
    "must_not_perform",
    "context_only",
    "unresolved_do_not_select",
)
DELIVERY_MODES = ("first_delivery", "lost_reply_retry", "regeneration_retry")
OBJECT_ALIGNMENT_STATUSES = (
    "none",
    "unavailable",
    "positive",
    "false",
    "unresolved",
    "uncheckable",
)
SETTLED_CHANGE_KINDS = (
    "target_admission",
    "scene_transition",
    "hp",
    "cost",
    "mastery",
    "cooldown",
    "consequence",
)

# Stable machine codes, not prompt prose.  Version 1 may reject an inference with these codes but
# may not invent an ad-hoc explanation that quietly expands narrator authority.
FORBIDDEN_INFERENCE_CODES = (
    "only_realized_changes_may_be_world_changes",
    "no_receipt_no_outcome",
    "object_ownership_unproven",
    "unresolved_candidates_must_not_be_selected",
    "attributed_content_is_not_world_truth",
    "pending_event_has_no_impact",
    "no_unstated_player_action",
    "mechanic_numbers_are_not_story_language",
)

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STABLE_REF_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,159}\Z")
_ENTITY_REF_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]*(?:#[1-9][0-9]*)?\Z")
_VERSIONED_ID_RE = re.compile(
    r"[a-z][a-z0-9_.-]*(?:/[a-z][a-z0-9_.-]*)*/[1-9][0-9]*\Z"
)

_OUTER_FIELDS = {
    "schema",
    "turn",
    "delivery_mode",
    "asserted_settled",
    "asserted_unresolved",
    "attributed_noncurrent",
    "forbidden_inference",
    "fingerprint",
}
_SETTLED_FIELDS = {
    "event_ref",
    "adapter_id",
    "frame_ref",
    "event_meaning",
    "outcome_quality",
    "impact_kind",
    "impact_magnitude",
    "target_state",
    "settled_change_kinds",
}
_UNRESOLVED_FIELDS = {
    "event_ref",
    "frame_ref",
    "event_meaning",
    "reason",
    "allowed_stage",
}
_ATTRIBUTED_FIELDS = {
    "event_ref",
    "frame_ref",
    "event_meaning",
}
_EVENT_MEANING_FIELDS = {
    "meaning_ref",
    "actor_id",
    "capability_id",
    "invoked_capability_ids",
    "action_class",
    "target_entity_id",
    "object_relation",
    "target_locus",
    "target_locus_owner_id",
    "assertion_status",
    "embedding_kind",
    "holder_role",
    "holder_entity_id",
    "holder_candidates",
    "polarity",
    "modality",
    "time_scope",
    "ambiguity_candidate_ids",
    "performance_mode",
}
_OBJECT_RELATION_FIELDS = {
    "object_kind_id",
    "linguistic_possessor_id",
    "resolved_instance_ids",
    "proven_owner_id",
    "part_id",
    "alignment_status",
    "alignment_ref",
    "candidate_instance_ids",
}
_FORBIDDEN_FIELDS = {"scope_ref", "code"}


class NarratorRealizationError(ValueError):
    """A narrator realization is malformed or claims authority outside its contract."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NarratorRealizationError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise NarratorRealizationError(f"{label} fields must be strings")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unexpected = sorted(set(value) - fields)
    if unexpected:
        raise NarratorRealizationError(f"{label} has unexpected fields: {unexpected}")
    missing = sorted(fields - set(value))
    if missing:
        raise NarratorRealizationError(f"{label} is missing required fields: {missing}")


def _stable_ref(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _STABLE_REF_RE.fullmatch(value) is None
    ):
        raise NarratorRealizationError(f"{label} must be a stable lowercase reference")
    return value


def _optional_stable_ref(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _stable_ref(value, label)


def _entity_ref(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 160
        or _ENTITY_REF_RE.fullmatch(value) is None
    ):
        raise NarratorRealizationError(f"{label} must be a stable lowercase entity reference")
    return value


def _optional_entity_ref(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _entity_ref(value, label)


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise NarratorRealizationError(f"{label} must be a sha256 content fingerprint")
    return value


def _optional_fingerprint(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, label)


def _versioned_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _VERSIONED_ID_RE.fullmatch(value) is None
    ):
        raise NarratorRealizationError(f"{label} must be a versioned identifier ending in /N")
    return value


def _choice(value: object, choices: tuple[str, ...], label: str) -> str:
    if value not in choices:
        raise NarratorRealizationError(f"{label} is invalid")
    return str(value)


def _canonical_refs(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or _STABLE_REF_RE.fullmatch(item) is None
        for item in value
    ) or value != sorted(set(value)):
        raise NarratorRealizationError(f"{label} must be sorted unique stable references")
    return list(value)


def _canonical_entity_refs(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or len(item) > 160
        or _ENTITY_REF_RE.fullmatch(item) is None
        for item in value
    ) or value != sorted(set(value)):
        raise NarratorRealizationError(
            f"{label} must be sorted unique stable entity references"
        )
    return list(value)


def _semantic_token(value: object) -> str | None:
    """Return a source-free semantic token only for a small, plain canonical phrase.

    ActionFrame owns the interpretation, but its object and locus labels remain strings.  This
    deliberately accepts ordinary one-to-six-word labels and rejects punctuation, control text,
    or prompt-shaped content instead of forwarding it to the narrator.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if len(raw) > 80 or re.fullmatch(
        r"[A-Za-z0-9]+(?:[ _-][A-Za-z0-9]+){0,5}", raw,
    ) is None:
        return None
    token = re.sub(r"[^a-z0-9]+", "_", normalize_phrase(raw)).strip("_")
    if not token or _STABLE_REF_RE.fullmatch(token) is None:
        return None
    return token


def _expected_performance_mode(event: Mapping[str, Any]) -> str:
    if event.get("embedding_kind") != "none" \
            or event.get("modality") not in {"actual", "command"} \
            or event.get("time_scope") != "current":
        return "context_only"
    if event.get("polarity") == "negative":
        return "must_not_perform"
    if event.get("ambiguity_candidate_ids"):
        return "unresolved_do_not_select"
    if event.get("assertion_status") != "asserted":
        return "context_only"
    return "may_perform"


def _validate_event_meaning(value: object, label: str) -> dict[str, Any]:
    event = _mapping(value, label)
    _exact_fields(event, _EVENT_MEANING_FIELDS, label)
    _optional_fingerprint(event["meaning_ref"], f"{label}.meaning_ref")
    _entity_ref(event["actor_id"], f"{label}.actor_id")
    primary = _optional_stable_ref(
        event["capability_id"], f"{label}.capability_id",
    )
    invoked = _canonical_refs(event["invoked_capability_ids"], f"{label}.invoked_capability_ids")
    if primary in invoked:
        raise NarratorRealizationError(f"{label} repeats its primary capability as invoked")
    _stable_ref(event["action_class"], f"{label}.action_class")
    _optional_entity_ref(event["target_entity_id"], f"{label}.target_entity_id")

    relation = _mapping(event["object_relation"], f"{label}.object_relation")
    _exact_fields(relation, _OBJECT_RELATION_FIELDS, f"{label}.object_relation")
    object_kind = _optional_stable_ref(
        relation["object_kind_id"], f"{label}.object_relation.object_kind_id",
    )
    possessor = _optional_entity_ref(
        relation["linguistic_possessor_id"],
        f"{label}.object_relation.linguistic_possessor_id",
    )
    resolved = _canonical_refs(
        relation["resolved_instance_ids"], f"{label}.object_relation.resolved_instance_ids",
    )
    owner = _optional_entity_ref(
        relation["proven_owner_id"], f"{label}.object_relation.proven_owner_id",
    )
    _optional_stable_ref(relation["part_id"], f"{label}.object_relation.part_id")
    alignment_status = _choice(
        relation["alignment_status"], OBJECT_ALIGNMENT_STATUSES,
        f"{label}.object_relation.alignment_status",
    )
    alignment_ref = _optional_fingerprint(
        relation["alignment_ref"], f"{label}.object_relation.alignment_ref",
    )
    candidates = _canonical_refs(
        relation["candidate_instance_ids"],
        f"{label}.object_relation.candidate_instance_ids",
    )
    if object_kind is None:
        if any((possessor, resolved, owner, relation["part_id"], alignment_ref, candidates)) \
                or alignment_status != "none":
            raise NarratorRealizationError(f"{label} has an object relation without an object")
    elif alignment_status == "none":
        raise NarratorRealizationError(f"{label} object relation lacks alignment status")
    if alignment_status == "positive":
        if alignment_ref is None or len(resolved) != 1 or owner is None or candidates:
            raise NarratorRealizationError(f"{label} positive object relation is incomplete")
    elif owner is not None:
        raise NarratorRealizationError(f"{label} non-positive object relation grants ownership")
    if alignment_status in {"positive", "false", "unresolved", "uncheckable"} \
            and alignment_ref is None:
        raise NarratorRealizationError(f"{label} aligned object relation loses its receipt")
    if alignment_status == "unresolved" and (len(candidates) < 2 or resolved):
        raise NarratorRealizationError(f"{label} unresolved object relation loses its candidates")
    if alignment_status in {"uncheckable", "unavailable"} and (resolved or candidates):
        raise NarratorRealizationError(f"{label} unaligned object relation resolves an instance")

    locus_id = _optional_stable_ref(event["target_locus"], f"{label}.target_locus")
    locus_owner = _optional_entity_ref(
        event["target_locus_owner_id"], f"{label}.target_locus_owner_id",
    )
    if locus_id is None and locus_owner is not None:
        raise NarratorRealizationError(f"{label} cannot assign an owner to an omitted locus")

    assertion = _choice(
        event["assertion_status"], ASSERTION_STATUSES, f"{label}.assertion_status",
    )
    embedding = _choice(event["embedding_kind"], EMBEDDING_KINDS, f"{label}.embedding_kind")
    holder_role = _choice(event["holder_role"], HOLDER_ROLES, f"{label}.holder_role")
    expected_holder_role = _EMBEDDING_HOLDER_ROLES.get(embedding, "none")
    if embedding == "mixed":
        expected_holder_role = "mixed"
    if holder_role != expected_holder_role:
        raise NarratorRealizationError(f"{label}.holder_role disagrees with embedding_kind")
    if assertion == "embedded" and embedding == "none" \
            or assertion == "asserted" and embedding != "none":
        raise NarratorRealizationError(f"{label}.assertion_status disagrees with embedding_kind")
    holder_id = _optional_entity_ref(event["holder_entity_id"], f"{label}.holder_entity_id")
    holder_candidates = _canonical_entity_refs(
        event["holder_candidates"], f"{label}.holder_candidates",
    )
    if holder_role == "none" and (holder_id is not None or holder_candidates):
        raise NarratorRealizationError(f"{label} cannot assign a holder when holder_role is none")
    if holder_id is not None and holder_candidates:
        raise NarratorRealizationError(f"{label} cannot be both holder-resolved and ambiguous")
    _choice(event["polarity"], POLARITIES, f"{label}.polarity")
    _choice(event["modality"], MODALITIES, f"{label}.modality")
    _choice(event["time_scope"], TIME_SCOPES, f"{label}.time_scope")
    ambiguity = _canonical_entity_refs(
        event["ambiguity_candidate_ids"], f"{label}.ambiguity_candidate_ids",
    )
    if assertion == "ambiguous" and not ambiguity:
        raise NarratorRealizationError(f"{label} ambiguous assertion loses its candidate IDs")
    if assertion != "ambiguous" and ambiguity:
        raise NarratorRealizationError(f"{label} candidate IDs require ambiguous assertion status")
    performance = _choice(event["performance_mode"], PERFORMANCE_MODES, f"{label}.performance_mode")
    if performance != _expected_performance_mode(event):
        raise NarratorRealizationError(f"{label}.performance_mode disagrees with its qualifiers")
    return deepcopy(dict(event))


def _event_rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise NarratorRealizationError(f"{label} must be a list")
    return value


def _validate_settled(value: object, index: int) -> dict[str, Any]:
    label = f"asserted_settled[{index}]"
    row = _mapping(value, label)
    _exact_fields(row, _SETTLED_FIELDS, label)
    _stable_ref(row["event_ref"], f"{label}.event_ref")
    adapter_id = _versioned_id(row["adapter_id"], f"{label}.adapter_id")
    if adapter_id not in {
        WEAPON_ATTACK_REALIZATION_ADAPTER,
        SKILL_CHECK_REALIZATION_ADAPTER,
        COMBAT_OPENING_REALIZATION_ADAPTER,
    }:
        raise NarratorRealizationError(f"{label}.adapter_id is not admitted by v1")
    _fingerprint(row["frame_ref"], f"{label}.frame_ref")
    event = _validate_event_meaning(row["event_meaning"], f"{label}.event_meaning")
    if event["performance_mode"] != "may_perform" or event["capability_id"] is None:
        raise NarratorRealizationError(f"{label} cannot settle non-performable or incomplete meaning")
    _choice(row["outcome_quality"], OUTCOME_QUALITIES, f"{label}.outcome_quality")
    impact_kind = _choice(row["impact_kind"], IMPACT_KINDS, f"{label}.impact_kind")
    impact = _choice(row["impact_magnitude"], IMPACT_MAGNITUDES, f"{label}.impact_magnitude")
    target_state = _choice(row["target_state"], TARGET_STATES, f"{label}.target_state")
    changes = row["settled_change_kinds"]
    if not isinstance(changes, list) \
            or any(not isinstance(kind, str) or kind not in SETTLED_CHANGE_KINDS
                   for kind in changes) \
            or changes != sorted(set(changes), key=SETTLED_CHANGE_KINDS.index):
        raise NarratorRealizationError(f"{label}.settled_change_kinds is not canonical")
    if adapter_id == WEAPON_ATTACK_REALIZATION_ADAPTER:
        if event["action_class"] != "weapon_attack" or event["target_entity_id"] is None:
            raise NarratorRealizationError(f"{label} weapon meaning lacks its exact target")
        if target_state == "not_applicable":
            raise NarratorRealizationError(f"{label} weapon result needs a target state")
        if impact_kind == "none" and impact != "none":
            raise NarratorRealizationError(f"{label} cannot assign magnitude without harm")
        if impact_kind == "harm" and impact == "none":
            raise NarratorRealizationError(f"{label} harm needs a qualitative magnitude")
        if (impact == "decisive") != (target_state == "defeated"):
            raise NarratorRealizationError(
                f"{label} decisive impact and defeated target state must agree"
            )
        if "hp" not in changes:
            raise NarratorRealizationError(f"{label} weapon result lacks its HP change")
    elif adapter_id == SKILL_CHECK_REALIZATION_ADAPTER:
        if event["action_class"] in NON_SKILL_CHECK_ACTION_CLASSES:
            raise NarratorRealizationError(f"{label} skill adapter requires non-impact meaning")
        if impact_kind != "none" or impact != "none" or target_state != "not_applicable":
            raise NarratorRealizationError(
                f"{label} skill result cannot claim target impact or target state"
            )
        if any(kind not in {"cost", "mastery", "cooldown", "consequence"}
               for kind in changes):
            raise NarratorRealizationError(
                f"{label} skill result claims a change outside skill_check/1"
            )
    else:
        if event["action_class"] != "combat_opening" \
                or event["target_entity_id"] is None:
            raise NarratorRealizationError(
                f"{label} combat-opening meaning lacks its exact primary target"
            )
        if row["outcome_quality"] != "automatic" \
                or impact_kind != "none" or impact != "none" or target_state != "active":
            raise NarratorRealizationError(
                f"{label} combat opening must be automatic, non-impacting, and active"
            )
        if "target_admission" not in changes \
                or any(kind not in {"target_admission", "scene_transition"} for kind in changes):
            raise NarratorRealizationError(
                f"{label} combat opening claims a change outside combat_opening/1"
            )
    return deepcopy(dict(row))


def _validate_unresolved(value: object, index: int) -> dict[str, Any]:
    label = f"asserted_unresolved[{index}]"
    row = _mapping(value, label)
    _exact_fields(row, _UNRESOLVED_FIELDS, label)
    _stable_ref(row["event_ref"], f"{label}.event_ref")
    frame_ref = _optional_fingerprint(row["frame_ref"], f"{label}.frame_ref")
    event = _validate_event_meaning(row["event_meaning"], f"{label}.event_meaning")
    reason = _choice(row["reason"], UNRESOLVED_REASONS, f"{label}.reason")
    allowed_stage = _choice(row["allowed_stage"], ALLOWED_STAGES, f"{label}.allowed_stage")
    expected_stage = "visible_tell_only" if reason == "pending_intent" else (
        "no_performance" if event["performance_mode"] == "must_not_perform" else (
            "context_only" if event["performance_mode"] == "context_only" else "attempt_only"
        )
    )
    if allowed_stage != expected_stage:
        raise NarratorRealizationError(
            f"{label}.allowed_stage does not match its unresolved reason"
        )
    if reason != "pending_intent" and frame_ref is None:
        raise NarratorRealizationError(f"{label}.frame_ref is required for an asserted event")
    if reason != "pending_intent" and event["meaning_ref"] is None:
        raise NarratorRealizationError(f"{label}.event_meaning needs a meaning receipt")
    return deepcopy(dict(row))


def _validate_attributed(value: object, index: int) -> dict[str, Any]:
    label = f"attributed_noncurrent[{index}]"
    row = _mapping(value, label)
    _exact_fields(row, _ATTRIBUTED_FIELDS, label)
    _stable_ref(row["event_ref"], f"{label}.event_ref")
    _fingerprint(row["frame_ref"], f"{label}.frame_ref")
    event = _validate_event_meaning(row["event_meaning"], f"{label}.event_meaning")
    if event["meaning_ref"] is None:
        raise NarratorRealizationError(f"{label}.event_meaning needs a meaning receipt")
    if event["performance_mode"] != "context_only":
        raise NarratorRealizationError(
            f"{label} is direct current content and belongs in an asserted bucket"
        )
    return deepcopy(dict(row))


def _validate_forbidden(value: object, index: int) -> dict[str, Any]:
    label = f"forbidden_inference[{index}]"
    row = _mapping(value, label)
    _exact_fields(row, _FORBIDDEN_FIELDS, label)
    _stable_ref(row["scope_ref"], f"{label}.scope_ref")
    _choice(row["code"], FORBIDDEN_INFERENCE_CODES, f"{label}.code")
    return deepcopy(dict(row))


def qualitative_hp_impact(hp_delta: int, maximum_hp: int, *, defeated: bool) -> str:
    """Project an exact applied HP delta into non-numeric narrator language.

    ``hp_delta`` is the reducer's signed change, so damage is negative.  The defeated flag must
    come from settled post-state rather than being guessed from the delta.  Outcome quality is
    deliberately absent: a critical success can still produce only modest applied harm.
    """
    if isinstance(hp_delta, bool) or not isinstance(hp_delta, int) or hp_delta > 0:
        raise NarratorRealizationError("hp_delta must be a non-positive integer")
    if isinstance(maximum_hp, bool) or not isinstance(maximum_hp, int) or maximum_hp <= 0:
        raise NarratorRealizationError("maximum_hp must be a positive integer")
    if not isinstance(defeated, bool):
        raise NarratorRealizationError("defeated must be a boolean settled post-state")
    damage = -hp_delta
    if damage > maximum_hp:
        raise NarratorRealizationError("applied HP damage cannot exceed maximum_hp")
    if damage == 0:
        if defeated:
            raise NarratorRealizationError("zero applied HP damage cannot newly defeat the target")
        return "none"
    if defeated:
        return "decisive"
    if damage * 100 <= maximum_hp * 15:
        return "modest"
    if damage * 100 <= maximum_hp * 35:
        return "solid"
    if damage * 100 <= maximum_hp * 65:
        return "severe"
    return "devastating"


def validate_narrator_realization(value: object) -> dict[str, Any]:
    """Validate and detach one complete ``narrator-realization/1`` packet."""
    packet = _mapping(value, "narrator realization")
    _exact_fields(packet, _OUTER_FIELDS, "narrator realization")
    if packet["schema"] != NARRATOR_REALIZATION_SCHEMA:
        raise NarratorRealizationError("unsupported narrator realization schema")
    turn = packet["turn"]
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise NarratorRealizationError("narrator realization turn must be a non-negative integer")
    _choice(packet["delivery_mode"], DELIVERY_MODES, "narrator realization delivery_mode")

    settled = [
        _validate_settled(row, index)
        for index, row in enumerate(_event_rows(packet["asserted_settled"], "asserted_settled"))
    ]
    unresolved = [
        _validate_unresolved(row, index)
        for index, row in enumerate(
            _event_rows(packet["asserted_unresolved"], "asserted_unresolved")
        )
    ]
    attributed = [
        _validate_attributed(row, index)
        for index, row in enumerate(
            _event_rows(packet["attributed_noncurrent"], "attributed_noncurrent")
        )
    ]
    forbidden = [
        _validate_forbidden(row, index)
        for index, row in enumerate(
            _event_rows(packet["forbidden_inference"], "forbidden_inference")
        )
    ]

    for label, rows in (
        ("asserted_settled", settled),
        ("asserted_unresolved", unresolved),
        ("attributed_noncurrent", attributed),
    ):
        if rows != sorted(rows, key=lambda row: row["event_ref"]):
            raise NarratorRealizationError(f"{label} must be sorted by event_ref")
    if forbidden != sorted(forbidden, key=lambda row: (row["scope_ref"], row["code"])):
        raise NarratorRealizationError("forbidden_inference must be canonically sorted")

    event_refs = [row["event_ref"] for row in settled + unresolved + attributed]
    if len(event_refs) != len(set(event_refs)):
        raise NarratorRealizationError("event_ref must be unique across realization buckets")
    forbidden_keys = [(row["scope_ref"], row["code"]) for row in forbidden]
    if len(forbidden_keys) != len(set(forbidden_keys)):
        raise NarratorRealizationError("forbidden inference rows must be unique")

    payload = {key: deepcopy(packet[key]) for key in packet if key != "fingerprint"}
    _fingerprint(packet["fingerprint"], "narrator realization fingerprint")
    if packet["fingerprint"] != content_fingerprint(payload):
        raise NarratorRealizationError("narrator realization fingerprint mismatch")
    return deepcopy(dict(packet))


def build_narrator_realization(
    turn: int,
    *,
    delivery_mode: str = "first_delivery",
    asserted_settled: Iterable[Mapping[str, Any]] = (),
    asserted_unresolved: Iterable[Mapping[str, Any]] = (),
    attributed_noncurrent: Iterable[Mapping[str, Any]] = (),
    forbidden_inference: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a canonically ordered realization packet and seal its content fingerprint."""
    settled = sorted((deepcopy(dict(row)) for row in asserted_settled), key=lambda row: row["event_ref"])
    unresolved = sorted(
        (deepcopy(dict(row)) for row in asserted_unresolved), key=lambda row: row["event_ref"]
    )
    attributed = sorted(
        (deepcopy(dict(row)) for row in attributed_noncurrent), key=lambda row: row["event_ref"]
    )
    forbidden = sorted(
        (deepcopy(dict(row)) for row in forbidden_inference),
        key=lambda row: (row["scope_ref"], row["code"]),
    )
    payload = {
        "schema": NARRATOR_REALIZATION_SCHEMA,
        "turn": turn,
        "delivery_mode": delivery_mode,
        "asserted_settled": settled,
        "asserted_unresolved": unresolved,
        "attributed_noncurrent": attributed,
        "forbidden_inference": forbidden,
    }
    return validate_narrator_realization(
        {**payload, "fingerprint": content_fingerprint(payload)}
    )


_ASSERTION_EMBEDDINGS = {
    "reported": "reported_or_testified",
    "testimony": "reported_or_testified",
    "remembered": "remembered",
    "believed": "believed_or_interpreted",
    "observed_content": "observed",
    "quoted": "quoted",
    "attributed": "attributed",
}
_EMBEDDING_HOLDER_ROLES = {
    "reported_or_testified": "speaker",
    "remembered": "experiencer",
    "believed_or_interpreted": "experiencer",
    "observed": "observer",
    "quoted": "speaker",
    "attributed": "attributor",
}


def _journal_payloads(
    state: Mapping[str, Any], key: str, payload_key: str, turn: int, *, optional: bool = False,
) -> list[object]:
    """Detach one current-turn slice before validating payload shape.

    Old journal payloads are not current truth and may predate the active schema.  Their turn
    envelope is the only field inspected.  Current rows remain exact and fail closed.
    """
    rows = state.get(key)
    if rows is None and optional:
        return []
    if not isinstance(rows, list):
        raise NarratorRealizationError(f"{key} journal is absent or malformed")
    current: list[object] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise NarratorRealizationError(f"{key} journal row is malformed")
        row_turn = row.get("turn")
        if isinstance(row_turn, bool) or not isinstance(row_turn, int) or row_turn < 0:
            raise NarratorRealizationError(f"{key} journal turn is malformed")
        if row_turn != turn:
            continue
        if set(row) != {"turn", payload_key}:
            raise NarratorRealizationError(f"{key} current journal row is malformed")
        current.append(deepcopy(row[payload_key]))
    return current


def _unique_by_fingerprint(rows: Iterable[Mapping[str, Any]], label: str) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for row in rows:
        fingerprint = _fingerprint(row.get("fingerprint"), f"{label} fingerprint")
        if fingerprint in indexed:
            raise NarratorRealizationError(f"{label} fingerprints must be unique")
        indexed[fingerprint] = deepcopy(dict(row))
    return indexed


def _one_reachable(
    rows: Iterable[object], *, field: str, reference: str, label: str,
) -> object:
    """Select exactly one raw dependency named by a current frame.

    Unreferenced rows cannot lend authority to the event and are intentionally not interpreted.
    """
    matches = [
        row for row in rows
        if isinstance(row, Mapping) and row.get(field) == reference
    ]
    if len(matches) != 1:
        raise NarratorRealizationError(
            f"{label} reference must resolve to exactly one current journal payload"
        )
    return deepcopy(matches[0])


def current_v3_semantic_authority(state: object) -> bool:
    """Return whether the current turn declares any V3 semantic frame boundary.

    This is deliberately a recognition-only gate.  Once a current V3 row declares authority,
    malformed downstream dependencies suppress legacy numeric narration instead of restoring it.
    """
    if not isinstance(state, Mapping):
        return False
    meta = state.get("meta")
    rows = state.get("semantic_frames")
    turn = meta.get("turn") if isinstance(meta, Mapping) else None
    if isinstance(turn, bool) or not isinstance(turn, int) or not isinstance(rows, list):
        return False
    return any(
        isinstance(row, Mapping)
        and row.get("turn") == turn
        and isinstance(row.get("frame"), Mapping)
        and row["frame"].get("schema") == "semantic-action-frame/3"
        for row in rows
    )


def current_v3_target_ids(state: object) -> frozenset[str]:
    """Return only stable target IDs named by current V3 frame envelopes."""
    if not current_v3_semantic_authority(state) or not isinstance(state, Mapping):
        return frozenset()
    turn = state["meta"]["turn"]
    return frozenset(
        target
        for row in state["semantic_frames"]
        if isinstance(row, Mapping) and row.get("turn") == turn
        and isinstance(row.get("frame"), Mapping)
        and row["frame"].get("schema") == "semantic-action-frame/3"
        and isinstance((target := row["frame"].get("target_entity_id")), str)
        and _STABLE_REF_RE.fullmatch(target) is not None
    )


def _lex_backed_token(
    frame: Mapping[str, Any],
    meaning: Mapping[str, Any],
    *,
    frame_field: str,
    evidence_kind: str,
    referent_kind: str,
    required_feature: str | None = None,
) -> str | None:
    """Admit an exact frame label only when ReferentLex backs its bounded evidence span."""
    token = _semantic_token(frame.get(frame_field))
    if token is None:
        return None
    normalized = normalize_phrase(str(frame[frame_field]))
    evidence = [
        row for row in frame.get("evidence", [])
        if row.get("kind") == evidence_kind
        and normalize_phrase(str(row.get("value") or "")) == normalized
    ]
    if len(evidence) != 1:
        return None
    start, end = evidence[0].get("start"), evidence[0].get("end")
    matches = [
        row for row in meaning.get("matches", [])
        if row.get("lex_id") == "referent"
        and row.get("kind") == referent_kind
        and row.get("start") == start
        and row.get("end") == end
        and (
            required_feature is None
            or (isinstance(row.get("features"), Mapping)
                and row["features"].get(required_feature) is True)
        )
    ]
    return token if matches else None


def _object_relation(
    frame: Mapping[str, Any],
    meaning: Mapping[str, Any],
    alignments: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    object_name = str(frame.get("possessed_object") or "")
    relevant = [
        row for row in alignments
        if row.get("role") == "possessed_object"
        and row.get("predicate_id") == "item.owner_equals_linguistic_possessor"
    ]
    expected_value_ref = content_fingerprint({
        "role": "possessed_object",
        "value": normalize_phrase(object_name),
    })
    if any(row.get("recognized_value_ref") != expected_value_ref
           or row.get("time_scope") != frame.get("time_scope") for row in relevant):
        raise NarratorRealizationError("object alignment and semantic frame predicate disagree")
    if not object_name:
        if relevant or frame.get("possessed_object_instance_id") is not None \
                or frame.get("possessed_object_owner_id") is not None:
            raise NarratorRealizationError("object alignment has no recognized frame object")
        return {
            "object_kind_id": None,
            "linguistic_possessor_id": None,
            "resolved_instance_ids": [],
            "proven_owner_id": None,
            "part_id": None,
            "alignment_status": "none",
            "alignment_ref": None,
            "candidate_instance_ids": [],
        }
    object_token = _semantic_token(object_name)
    if object_token is None:
        raise NarratorRealizationError("recognized object cannot become a safe semantic token")
    if len(relevant) > 1:
        raise NarratorRealizationError("event has multiple object alignments")
    if not relevant:
        if frame.get("possessed_object_instance_id") is not None \
                or frame.get("possessed_object_owner_id") is not None:
            raise NarratorRealizationError("unaligned object cannot carry ledger authority")
        status, alignment_ref, resolved, candidates, owner = "unavailable", None, [], [], None
    else:
        alignment = relevant[0]
        status = str(alignment["status"])
        alignment_ref = str(alignment["fingerprint"])
        resolved = list(alignment.get("resolved_ids") or [])
        candidates = list(alignment.get("candidate_ids") or [])
        owner = alignment.get("positive_authority_value")
        if status == "positive":
            if alignment.get("selection") != "exact" or alignment.get("cardinality") != "one" \
                    or resolved != [frame.get("possessed_object_instance_id")] \
                    or owner != frame.get("possessed_object_owner_id"):
                raise NarratorRealizationError("positive object alignment and semantic frame disagree")
        elif frame.get("possessed_object_instance_id") is not None \
                or frame.get("possessed_object_owner_id") is not None:
            raise NarratorRealizationError("non-positive object alignment carries ledger authority")
    return {
        "object_kind_id": object_token,
        "linguistic_possessor_id": frame.get("linguistic_possessor_id"),
        "resolved_instance_ids": resolved,
        "proven_owner_id": owner,
        "part_id": _lex_backed_token(
            frame,
            meaning,
            frame_field="possessed_object_part",
            evidence_kind="possessed_object_part",
            referent_kind="object_part",
        ),
        "alignment_status": status,
        "alignment_ref": alignment_ref,
        "candidate_instance_ids": candidates,
    }


def _embedding_fields(frame: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    assertions = {
        str(row.get("value"))
        for row in binding.get("constraints", [])
        if row.get("dimension") == "assertion_context"
    }
    embeddings = {_ASSERTION_EMBEDDINGS[value] for value in assertions
                  if value in _ASSERTION_EMBEDDINGS}
    embedding = next(iter(embeddings)) if len(embeddings) == 1 else (
        "mixed" if embeddings else "none"
    )
    holder_role = _EMBEDDING_HOLDER_ROLES.get(embedding, "none")
    if embedding == "mixed":
        holder_role = "mixed"
    assertion_status = "ambiguous" if frame.get("ambiguity") else (
        "embedded" if embedding != "none" else "asserted"
    )
    holder_fields = {
        "speaker": ("holder_entity_id", "speaker_id"),
        "experiencer": ("holder_entity_id", "experiencer_id"),
        "observer": ("holder_entity_id", "observer_id"),
        "attributor": ("holder_entity_id", "attributor_id"),
        "mixed": ("holder_entity_id",),
        "none": (),
    }[holder_role]
    canonical_holders = {
        str(row.get("value"))
        for row in binding.get("field_provenance", [])
        if row.get("field") in holder_fields
        and isinstance(row.get("value"), str)
        and _STABLE_REF_RE.fullmatch(str(row["value"])) is not None
    }
    if len(canonical_holders) > 1:
        raise NarratorRealizationError("meaning binding has conflicting canonical holders")
    return {
        "assertion_status": assertion_status,
        "embedding_kind": embedding,
        "holder_role": holder_role,
        "holder_entity_id": next(iter(canonical_holders)) if canonical_holders else None,
        "holder_candidates": [],
    }


def _event_meaning(
    frame: Mapping[str, Any],
    binding: Mapping[str, Any],
    meaning: Mapping[str, Any],
    object_relation: Mapping[str, Any],
) -> dict[str, Any]:
    embedding = _embedding_fields(frame, binding)
    locus_id = _lex_backed_token(
        frame,
        meaning,
        frame_field="target_locus",
        evidence_kind="target_locus",
        referent_kind="body_part",
        required_feature="target_locus",
    )
    payload = {
        "meaning_ref": frame["meaning_ref"],
        "actor_id": frame["actor_id"],
        "capability_id": frame.get("capability_id"),
        "invoked_capability_ids": list(frame.get("invoked_capability_ids") or []),
        "action_class": frame["action_class"],
        "target_entity_id": frame.get("target_entity_id"),
        "object_relation": deepcopy(dict(object_relation)),
        "target_locus": locus_id,
        "target_locus_owner_id": (
            frame.get("target_locus_owner_id") if locus_id is not None else None
        ),
        **embedding,
        "polarity": frame["polarity"],
        "modality": frame["modality"],
        "time_scope": frame["time_scope"],
        "ambiguity_candidate_ids": list(frame.get("ambiguity") or []),
    }
    payload["performance_mode"] = _expected_performance_mode(payload)
    return payload


def _attributed_projection(frame: Mapping[str, Any], event_meaning: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_ref": frame["event_node_id"],
        "frame_ref": frame["fingerprint"],
        "event_meaning": deepcopy(dict(event_meaning)),
    }


def _unresolved_projection(
    frame: Mapping[str, Any], event_meaning: Mapping[str, Any], *, reason: str,
) -> dict[str, Any]:
    performance = event_meaning["performance_mode"]
    allowed_stage = "no_performance" if performance == "must_not_perform" else (
        "context_only" if performance == "context_only" else "attempt_only"
    )
    return {
        "event_ref": frame["event_node_id"],
        "frame_ref": frame["fingerprint"],
        "event_meaning": deepcopy(dict(event_meaning)),
        "reason": reason,
        "allowed_stage": allowed_stage,
    }


def _settled_change_kinds(receipt: Mapping[str, Any]) -> list[str]:
    """Project receipt change families once, while the receipt retains every exact row."""
    present = {row["kind"] for row in receipt["applied_changes"]}
    return [kind for kind in SETTLED_CHANGE_KINDS if kind in present]


def _weapon_settled_projection(
    frame: Mapping[str, Any], receipt: Mapping[str, Any], event_meaning: Mapping[str, Any],
) -> dict[str, Any]:
    hp_change = next(row for row in receipt["applied_changes"] if row["kind"] == "hp")
    hp = receipt["target_post_state"]["hp"]
    defeated = receipt["outcome"] == "defeat"
    magnitude = qualitative_hp_impact(int(hp_change["delta"]), int(hp["max"]),
                                      defeated=defeated)
    return {
        "event_ref": frame["event_node_id"],
        "adapter_id": WEAPON_ATTACK_REALIZATION_ADAPTER,
        "frame_ref": frame["fingerprint"],
        "event_meaning": deepcopy(dict(event_meaning)),
        "outcome_quality": receipt["outcome_quality"],
        "impact_kind": "none" if hp_change["delta"] == 0 else "harm",
        "impact_magnitude": magnitude,
        "target_state": "defeated" if defeated else "active",
        "settled_change_kinds": _settled_change_kinds(receipt),
    }


def _skill_check_settled_projection(
    frame: Mapping[str, Any], receipt: Mapping[str, Any], event_meaning: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a completed check without turning its optional referent into target authority."""
    return {
        "event_ref": frame["event_node_id"],
        "adapter_id": SKILL_CHECK_REALIZATION_ADAPTER,
        "frame_ref": frame["fingerprint"],
        "event_meaning": deepcopy(dict(event_meaning)),
        "outcome_quality": receipt["outcome_quality"],
        "impact_kind": "none",
        "impact_magnitude": "none",
        "target_state": "not_applicable",
        "settled_change_kinds": _settled_change_kinds(receipt),
    }


def _combat_opening_settled_projection(
    frame: Mapping[str, Any], receipt: Mapping[str, Any], event_meaning: Mapping[str, Any],
) -> dict[str, Any]:
    """Project an automatic opening while its receipt retains every admitted combatant."""
    return {
        "event_ref": frame["event_node_id"],
        "adapter_id": COMBAT_OPENING_REALIZATION_ADAPTER,
        "frame_ref": frame["fingerprint"],
        "event_meaning": deepcopy(dict(event_meaning)),
        "outcome_quality": "automatic",
        "impact_kind": "none",
        "impact_magnitude": "none",
        "target_state": "active",
        "settled_change_kinds": _settled_change_kinds(receipt),
    }


def _settled_projection(
    frame: Mapping[str, Any], receipt: Mapping[str, Any], event_meaning: Mapping[str, Any],
) -> dict[str, Any]:
    if receipt.get("contract_id") == WEAPON_ATTACK_CONTRACT:
        return _weapon_settled_projection(frame, receipt, event_meaning)
    if receipt.get("contract_id") == SKILL_CHECK_CONTRACT:
        return _skill_check_settled_projection(frame, receipt, event_meaning)
    if receipt.get("contract_id") == COMBAT_OPENING_CONTRACT:
        return _combat_opening_settled_projection(frame, receipt, event_meaning)
    raise NarratorRealizationError("settlement contract has no narrator realization adapter")


def _delivery_mode_from_state(state: Mapping[str, Any]) -> str:
    retry = state.get("_settled_retry")
    if retry is None:
        return "first_delivery"
    if not isinstance(retry, Mapping):
        raise NarratorRealizationError("settled retry marker is malformed")
    kind = retry.get("kind")
    if kind == "lost_reply":
        return "lost_reply_retry"
    if kind == "swipe_replay":
        return "regeneration_retry"
    raise NarratorRealizationError("settled retry marker kind is unsupported")


def _build_narrator_realization_from_state(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Strict implementation behind the public fail-closed state projector."""
    meta = state.get("meta")
    if not isinstance(meta, dict):
        raise NarratorRealizationError("state meta is absent or malformed")
    turn = meta.get("turn")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise NarratorRealizationError("state turn is absent or malformed")

    frame_values = _journal_payloads(state, "semantic_frames", "frame", turn)
    if not frame_values:
        return None
    meaning_values = _journal_payloads(state, "semantic_meanings", "meaning", turn)
    binding_values = _journal_payloads(state, "semantic_bindings", "binding", turn)
    alignment_values = _journal_payloads(
        state, "semantic_world_alignments", "alignment", turn, optional=True,
    )
    settlement_values = _journal_payloads(
        state, "mechanic_settlements", "receipt", turn, optional=True,
    )

    from .semantic import ACTION_FRAME_SCHEMA, validate_action_frame_snapshot
    from .semantic_binding import validate_meaning_binding, validate_world_alignment
    from .semantic_fabric import validate_compiled_meaning_receipt

    frames = [validate_action_frame_snapshot(value) for value in frame_values]
    if any(frame.get("schema") != ACTION_FRAME_SCHEMA for frame in frames):
        raise NarratorRealizationError("narrator realization requires a V3 semantic frame")
    _unique_by_fingerprint(frames, "semantic frame")
    event_refs = [str(frame["event_node_id"]) for frame in frames]
    binding_refs = [str(frame["meaning_binding_ref"]) for frame in frames]
    if len(event_refs) != len(set(event_refs)):
        raise NarratorRealizationError("one current event_node_id cannot own multiple frames")
    if len(binding_refs) != len(set(binding_refs)):
        raise NarratorRealizationError("one current meaning binding cannot own multiple frames")

    meaning_index: dict[str, dict[str, Any]] = {}
    binding_index: dict[str, dict[str, Any]] = {}
    alignment_index: dict[str, dict[str, Any]] = {}
    settlements_by_frame: dict[str, dict[str, Any]] = {}
    for frame in frames:
        meaning_ref = str(frame["meaning_ref"])
        meaning = meaning_index.get(meaning_ref)
        if meaning is None:
            meaning = validate_compiled_meaning_receipt(_one_reachable(
                meaning_values,
                field="fingerprint",
                reference=meaning_ref,
                label="semantic meaning",
            ))
            meaning_index[meaning_ref] = meaning

        context = frame.get("context_frame") or {}
        if meaning.get("fingerprint") != meaning_ref \
                or meaning.get("source_fingerprint") != context.get("source_fingerprint") \
                or meaning.get("fabric_fingerprint") != frame.get("fabric_fingerprint") \
                or meaning.get("genre_ids") != context.get("genre_ids"):
            raise NarratorRealizationError("semantic frame and compiled meaning disagree")

        binding_ref = str(frame["meaning_binding_ref"])
        binding = validate_meaning_binding(_one_reachable(
            binding_values,
            field="fingerprint",
            reference=binding_ref,
            label="semantic binding",
        ), meaning_receipt=meaning)
        binding_index[binding_ref] = binding
        if binding.get("event_node_id") != frame.get("event_node_id") \
                or binding.get("source_fingerprint") != context.get("source_fingerprint") \
                or binding.get("event_span") \
                != [context.get("span_start"), context.get("span_end")]:
            raise NarratorRealizationError("semantic frame and meaning binding disagree")

        for alignment_ref in frame.get("world_alignment_refs", []):
            alignment_ref = str(alignment_ref)
            if alignment_ref in alignment_index:
                continue
            alignment = validate_world_alignment(_one_reachable(
                alignment_values,
                field="fingerprint",
                reference=alignment_ref,
                label="semantic alignment",
            ))
            alignment_index[alignment_ref] = alignment

        raw_settlements = [
            value for value in settlement_values
            if isinstance(value, Mapping) and value.get("frame_ref") == frame["fingerprint"]
        ]
        if len(raw_settlements) > 1:
            raise NarratorRealizationError("one semantic frame cannot settle more than once")
        if raw_settlements:
            settlements_by_frame[str(frame["fingerprint"])] = validate_mechanic_settlement(
                raw_settlements[0]
            )

    asserted_settled: list[dict[str, Any]] = []
    asserted_unresolved: list[dict[str, Any]] = []
    attributed_noncurrent: list[dict[str, Any]] = []
    forbidden = {
        (f"turn:{turn}", "only_realized_changes_may_be_world_changes"),
        (f"turn:{turn}", "no_unstated_player_action"),
        (f"turn:{turn}", "mechanic_numbers_are_not_story_language"),
    }

    for frame in sorted(frames, key=lambda row: row["event_node_id"]):
        binding = binding_index.get(str(frame["meaning_binding_ref"]))
        if binding is None or binding.get("meaning_ref") != frame.get("meaning_ref"):
            raise NarratorRealizationError("semantic frame and meaning binding disagree")
        try:
            event_alignments = [alignment_index[ref] for ref in frame["world_alignment_refs"]]
        except KeyError as exc:
            raise NarratorRealizationError(
                "semantic frame alignment reference is unresolved"
            ) from exc
        if any(row.get("recognition_ref") != binding["fingerprint"]
               for row in event_alignments):
            raise NarratorRealizationError("semantic alignment belongs to another binding")
        meaning = meaning_index[str(frame["meaning_ref"])]
        object_relation = _object_relation(frame, meaning, event_alignments)
        event_meaning = _event_meaning(frame, binding, meaning, object_relation)
        if frame.get("possessed_object") and object_relation["alignment_status"] != "positive":
            forbidden.add((frame["event_node_id"], "object_ownership_unproven"))

        receipt = settlements_by_frame.get(frame["fingerprint"])
        mechanic = binding["mechanic_disposition"]
        if mechanic == "recognition_only":
            if receipt is not None:
                raise NarratorRealizationError("noncurrent event cannot have a settlement")
            if event_meaning["performance_mode"] == "must_not_perform":
                asserted_unresolved.append(
                    _unresolved_projection(frame, event_meaning, reason="ineligible")
                )
                forbidden.add((frame["event_node_id"], "no_receipt_no_outcome"))
                if event_meaning["ambiguity_candidate_ids"]:
                    forbidden.add((
                        frame["event_node_id"], "unresolved_candidates_must_not_be_selected",
                    ))
            else:
                attributed_noncurrent.append(_attributed_projection(frame, event_meaning))
                forbidden.add((frame["event_node_id"], "attributed_content_is_not_world_truth"))
            continue
        if mechanic in {"hold_unresolved", "invalid_scope_conflict"}:
            if receipt is not None:
                raise NarratorRealizationError("unresolved event cannot have a settlement")
            asserted_unresolved.append(
                _unresolved_projection(frame, event_meaning, reason="semantic_ambiguity")
            )
            forbidden.add((frame["event_node_id"], "unresolved_candidates_must_not_be_selected"))
            forbidden.add((frame["event_node_id"], "no_receipt_no_outcome"))
            continue
        if mechanic != "candidate":
            raise NarratorRealizationError("meaning binding mechanic disposition is unsupported")

        direct_current = (
            frame["polarity"] == "positive"
            and frame["modality"] in {"actual", "command"}
            and frame["time_scope"] == "current"
            and not frame["ambiguity"]
        )
        if not direct_current:
            if receipt is not None:
                raise NarratorRealizationError("ineligible candidate cannot have a settlement")
            reason = "semantic_ambiguity" if frame["ambiguity"] else "ineligible"
            asserted_unresolved.append(_unresolved_projection(frame, event_meaning, reason=reason))
            forbidden.add((frame["event_node_id"], "unresolved_candidates_must_not_be_selected"))
            forbidden.add((frame["event_node_id"], "no_receipt_no_outcome"))
            continue
        if receipt is None:
            asserted_unresolved.append(
                _unresolved_projection(frame, event_meaning, reason="no_complete_settlement")
            )
            forbidden.add((frame["event_node_id"], "no_receipt_no_outcome"))
            continue
        if receipt["meaning_ref"] != frame["meaning_ref"] \
                or receipt["frame_ref"] != frame["fingerprint"]:
            raise NarratorRealizationError("settlement and semantic frame identity disagree")
        if receipt["contract_id"] == WEAPON_ATTACK_CONTRACT:
            if frame["action_class"] != "weapon_attack" \
                    or receipt["target_post_state"]["combatant_id"] \
                    != frame.get("target_entity_id"):
                raise NarratorRealizationError(
                    "weapon settlement and semantic frame identity disagree"
                )
        elif receipt["contract_id"] == SKILL_CHECK_CONTRACT:
            if frame["action_class"] in NON_SKILL_CHECK_ACTION_CLASSES \
                    or receipt["target_post_state"] is not None:
                raise NarratorRealizationError(
                    "skill settlement and semantic frame identity disagree"
                )
        elif receipt["contract_id"] == COMBAT_OPENING_CONTRACT:
            if frame["action_class"] != "combat_opening" \
                    or receipt["target_post_state"]["combatant_id"] \
                    != frame.get("target_entity_id"):
                raise NarratorRealizationError(
                    "combat opening settlement and semantic frame identity disagree"
                )
        else:
            raise NarratorRealizationError("settlement contract has no narrator adapter")
        asserted_settled.append(_settled_projection(frame, receipt, event_meaning))

    return build_narrator_realization(
        turn,
        delivery_mode=_delivery_mode_from_state(state),
        asserted_settled=asserted_settled,
        asserted_unresolved=asserted_unresolved,
        attributed_noncurrent=attributed_noncurrent,
        forbidden_inference=(
            {"scope_ref": scope_ref, "code": code}
            for scope_ref, code in forbidden
        ),
    )


def build_narrator_realization_from_state(state: object) -> dict[str, Any] | None:
    """Project the latest complete semantic/mechanic journals, or fail closed to no packet.

    This boundary deliberately returns ``None`` instead of a partial realization.  A missing or
    malformed journal must preserve existing narration behavior; it must never make recognition
    look like a settled world change.
    """
    if not isinstance(state, Mapping):
        return None
    try:
        return _build_narrator_realization_from_state(state)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return None


def _display_capability(value: object) -> str:
    """Render one validated stable capability ID without importing registry or source prose."""
    token = str(value or "").strip()
    if not token or _STABLE_REF_RE.fullmatch(token) is None:
        return "Unknown Capability"
    leaf = re.split(r"[.:]", token)[-1]
    return re.sub(r"[_-]+", " ", leaf).strip().title()


def _capability_summary(event: Mapping[str, Any]) -> str:
    primary = _display_capability(event.get("capability_id"))
    invoked = [
        _display_capability(value)
        for value in event.get("invoked_capability_ids") or []
    ]
    return "capability " + primary + (
        " using " + ", ".join(invoked) if invoked else ""
    )


def _plain_realization_summary(packet: Mapping[str, Any]) -> str:
    """Render the small human-readable contract the narrator must obey before the JSON packet.

    The versioned JSON remains the canonical receipt.  This summary deliberately projects only
    validated fields from that receipt so a model does not have to infer the most important
    outcome boundary from one dense encoded line.
    """
    lines: list[str] = []
    for row in packet["asserted_settled"]:
        event = row["event_meaning"]
        capability = _capability_summary(event)
        if row["adapter_id"] == WEAPON_ATTACK_REALIZATION_ADAPTER:
            lines.append(
                "PLAYER ATTACK " + row["event_ref"] + ": settled "
                + row["outcome_quality"] + "; " + capability + "; exact target "
                + str(event["target_entity_id"]) + "; impact "
                + row["impact_kind"] + "/" + row["impact_magnitude"]
                + "; target state " + row["target_state"]
                + ". Narrate this outcome once. Affect no other target and invent no extra "
                  "damage, injury, status, pinning, dismemberment, or defeat."
            )
        elif row["adapter_id"] == SKILL_CHECK_REALIZATION_ADAPTER:
            changes = ", ".join(row["settled_change_kinds"]) or "none"
            referent = str(event["target_entity_id"] or "none")
            lines.append(
                "PLAYER SKILL CHECK " + row["event_ref"] + ": settled "
                + row["outcome_quality"] + "; " + capability
                + "; exact referent " + referent + "; admitted changes " + changes
                + "; TARGET IMPACT NONE. Invent no hit, damage, injury, status, pinning, "
                  "dismemberment, defeat, or other world change."
            )
        else:
            lines.append(
                "COMBAT OPENING " + row["event_ref"] + ": admitted exact target "
                + str(event["target_entity_id"])
                + " and the receipt-backed combatants; no attack has landed. Narrate the "
                  "opening and pending threat without inventing impact, injury, or defeat."
            )
    for row in packet["asserted_unresolved"]:
        event = row["event_meaning"]
        lines.append(
            "PLAYER EVENT " + row["event_ref"] + ": UNRESOLVED (" + row["reason"]
            + "); " + _capability_summary(event)
            + "; exact referent " + str(event["target_entity_id"] or "none")
            + "; allowed stage " + row["allowed_stage"]
            + ". It caused no hit, damage, injury, status, pinning, dismemberment, defeat, "
              "target mutation, or world change."
        )
    if not lines:
        return ""
    return PLAIN_OUTCOME_CONTRACT_PREFIX + " | ".join(lines) + " " \
        + PLAIN_OUTCOME_CONTRACT_END + " "


def extract_plain_outcome_contract(value: object) -> str:
    """Extract only the bounded code-authored plain summary from a rendered turn packet."""
    if not isinstance(value, str):
        return ""
    start = value.find(PLAIN_OUTCOME_CONTRACT_PREFIX)
    if start < 0:
        return ""
    end = value.find(PLAIN_OUTCOME_CONTRACT_END, start)
    if end < 0:
        return ""
    end += len(PLAIN_OUTCOME_CONTRACT_END)
    return value[start:end]


def render_narrator_realization(value: object) -> str:
    """Render one validated, content-free realization as a final narrator directive."""
    packet = validate_narrator_realization(value)
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plain_summary = _plain_realization_summary(packet)
    player_attack_guard = ""
    if any(
        row["adapter_id"] == WEAPON_ATTACK_REALIZATION_ADAPTER
        for row in packet["asserted_settled"]
    ):
        player_attack_guard = (
            "CURRENT PLAYER ATTACK IS ALREADY SETTLED: narrate its recorded impact once in "
            "prose. Do not emit an [hp] tag or line for it, even when impact is none; its HP state is "
            "already committed. Do not copy or fill any generic world-tag template for this "
            "event. This turn-specific rule overrides generic tag examples. "
        )
    return (
        "[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1 — ENGINE-ONLY INPUT; NEVER "
        "QUOTE OR ECHO. Only asserted_settled entries are completed mechanic results; only "
        "their settled_change_kinds are committed ledger changes, and no unlisted target "
        "mutation may be inferred. "
        "event_meaning is the sole source-free interpretation in every bucket. Obey each "
        "performance_mode: must_not_perform is authoritative non-occurrence; context_only is "
        "never current performance; unresolved_do_not_select forbids choosing a candidate. "
        "delivery_mode retries re-narrate the same event and never settle it again. "
        + player_attack_guard + "Obey forbidden_inference. " + plain_summary + encoded
    )
