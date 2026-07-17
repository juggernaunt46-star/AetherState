"""Bounded, code-authored narration plans for proof-gated RPG delivery.

The model never returns story prose to this module.  Code projects a validated narration truth
contract into a finite ``narration-realization-plan/1`` whose phrase atoms, typed slot values,
ordering, default selection, and expected graph fragments are already sealed.  A model may return
only atom and slot IDs in a ``narration-plan-selection/1``.  Code then renders canonical plain text,
observes that exact text through a separately versioned finite-language observer, and compares the
observed graph with both the selected option graph and the ledger graph.

The full branch-neutral semantic basis is retained in the plan rather than represented by a lone
fingerprint.  An exact fork may therefore rebind only the branch reference while preserving the
ledger and realization roots.  This module owns no state mutation or storage.  Its proof-complete
candidate builder returns the exact accepted wire bytes and delivery proof that the lifecycle layer
can atomically promote over an already durable fallback.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from .capability_glossary import content_fingerprint, normalize_phrase, raw_fingerprint
from .narration_fallback_runtime import (
    EMPTY_FALLBACK_TEXT,
    NarrationFallbackRuntimeError,
    validate_canonical_visible_text,
)
from .narration_truth_gate import (
    PENDING_INTENT_FACT_SCHEMA,
    VISIBLE_CONTENT_TYPE,
    VISIBLE_RENDERER_CONFIG_FINGERPRINT,
    VISIBLE_RENDERER_VERSION,
    NarrationTruthGateError,
    validate_narration_truth_contract,
)
from .narrator_realization import (
    SKILL_CHECK_REALIZATION_ADAPTER,
    WEAPON_ATTACK_REALIZATION_ADAPTER,
)
from .response_wire import ChatWireError, decode_chat_story, encode_chat_story
from .turn_lifecycle import (
    TurnArtifactError,
    build_delivery_proof,
    canonical_claim_projection,
)


NARRATION_REALIZATION_PLAN_SCHEMA = "narration-realization-plan/1"
NARRATION_PLAN_REQUEST_SCHEMA = "narration-plan-request/1"
NARRATION_PLAN_SELECTION_SCHEMA = "narration-plan-selection/1"
NARRATION_PLAN_SELECTION_BLUEPRINT_SCHEMA = "narration-plan-selection-blueprint/1"
NARRATION_PLAN_GRAPH_SCHEMA = "narration-plan-proof-graph/1"
NARRATION_PLAN_OBSERVATION_CONTEXT_SCHEMA = "narration-plan-observation-context/1"
NARRATION_PHRASE_ATOM_LIBRARY_VERSION = "aetherstate.narration-phrase-atoms/4"
NARRATION_PLAN_SURFACE_SCHEMA = "aetherstate-plain-visible/1"

QUALITATIVE_ACTION_KIND = "qualitative_action"
PENDING_INTENT_KIND = "pending_intent"
_PLAN_CLAIM_KINDS = frozenset(
    {
        "opposition_action",
        PENDING_INTENT_KIND,
        "harm",
        "defeat",
        "status",
        "resource",
        "time",
        "movement",
        "world",
        QUALITATIVE_ACTION_KIND,
    }
)
_QUALITATIVE_OUTCOME_SURFACES = {
    "crit_fail": "a critical failure",
    "fail": "a failure",
    "partial": "a partial success",
    "success": "a success",
    "crit_success": "a critical success",
    "automatic": "an automatic outcome",
}

MAX_PLAN_CLAIMS = 64
MAX_SELECTION_BYTES = 64_000

_SEMANTIC_FIELDS = (
    "occurrence_ref",
    "cause_ref",
    "construction_ref",
    "actor_id",
    "subject_ids",
    "kind",
    "polarity",
    "actuality",
    "time_scope",
    "multiplicity",
    "detail",
    "amount",
)
_SLOT_TYPES = (
    "actor",
    "target",
    "effect",
    "scope",
    "causality",
    "attribution",
    "pending_intent",
)

# Rendering and observation deliberately use two separately declared tables.  The observer receives
# no selected atom and no expected graph.  A renderer-only defect therefore changes the visible line
# without changing the observer's accepted finite language and fails before a candidate artifact
# exists.
_RENDER_ATOM_TEMPLATES = {
    "claim.actor.direct/1": "{actor}'s settled action applies {effect} to {subject}.",
    "claim.actor.result/1": "{subject}'s settled result from {actor} is {effect}.",
    "claim.system.direct/1": "The settled state applies {effect} to {subject}.",
    "claim.system.result/1": "{subject}'s settled result is {effect}.",
    "opposition.hit.direct/1": "{actor}'s {move} hits {subject}.",
    "opposition.hit.result/1": "{actor} resolves {move} against {subject}: hit.",
    "opposition.miss.direct/1": "{actor}'s {move} misses {subject}.",
    "opposition.miss.result/1": "{actor} resolves {move} against {subject}: miss.",
    "opposition.blocked.direct/1": (
        "{actor}'s {move} is blocked before it affects {subject}."
    ),
    "opposition.blocked.result/1": (
        "{actor} resolves {move} against {subject}: blocked."
    ),
    "qualitative.self.direct/1": (
        "{actor} attempts a {action}; the result is {result}."
    ),
    "qualitative.self.result/1": "{actor}'s {action} resolves as {result}.",
    "qualitative.target.direct/1": (
        "{actor} attempts a {action} with {subject} as the focus; the result is {result}."
    ),
    "qualitative.target.result/1": (
        "{actor}'s {action}, focused on {subject}, resolves as {result}."
    ),
    "qualitative.weapon.direct/1": (
        "{actor}'s weapon attack against {subject} resolves as {result} with no target impact."
    ),
    "qualitative.weapon.result/1": (
        "{actor} resolves a weapon attack against {subject}: {result}, with no target impact."
    ),
    "pending_intent.combat_opening/1": "{intent_text}",
    "pending_intent.following/1": "{intent_text}",
}
_OBSERVER_ATOM_TEMPLATES = {
    "claim.actor.direct/1": "{actor}'s settled action applies {effect} to {subject}.",
    "claim.actor.result/1": "{subject}'s settled result from {actor} is {effect}.",
    "claim.system.direct/1": "The settled state applies {effect} to {subject}.",
    "claim.system.result/1": "{subject}'s settled result is {effect}.",
    "opposition.hit.direct/1": "{actor}'s {move} hits {subject}.",
    "opposition.hit.result/1": "{actor} resolves {move} against {subject}: hit.",
    "opposition.miss.direct/1": "{actor}'s {move} misses {subject}.",
    "opposition.miss.result/1": "{actor} resolves {move} against {subject}: miss.",
    "opposition.blocked.direct/1": (
        "{actor}'s {move} is blocked before it affects {subject}."
    ),
    "opposition.blocked.result/1": (
        "{actor} resolves {move} against {subject}: blocked."
    ),
    "qualitative.self.direct/1": (
        "{actor} attempts a {action}; the result is {result}."
    ),
    "qualitative.self.result/1": "{actor}'s {action} resolves as {result}.",
    "qualitative.target.direct/1": (
        "{actor} attempts a {action} with {subject} as the focus; the result is {result}."
    ),
    "qualitative.target.result/1": (
        "{actor}'s {action}, focused on {subject}, resolves as {result}."
    ),
    "qualitative.weapon.direct/1": (
        "{actor}'s weapon attack against {subject} resolves as {result} with no target impact."
    ),
    "qualitative.weapon.result/1": (
        "{actor} resolves a weapon attack against {subject}: {result}, with no target impact."
    ),
    "pending_intent.combat_opening/1": "{intent_text}",
    "pending_intent.following/1": "{intent_text}",
}
NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT = content_fingerprint(
    {
        "version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "renderer_atoms": _RENDER_ATOM_TEMPLATES,
        "observer_atoms": _OBSERVER_ATOM_TEMPLATES,
    }
)

_PLAN_FIELDS = {
    "schema",
    "phrase_atom_library_version",
    "phrase_atom_library_fingerprint",
    "source_truth_contract_fingerprint",
    "source_lifecycle_binding",
    "semantic_truth_basis",
    "semantic_truth_basis_fingerprint",
    "turn",
    "delivery_mode",
    "lifecycle_binding",
    "surface_profile",
    "ledger_projection_fingerprint",
    "required_occurrence_refs",
    "allowed_occurrence_refs",
    "occurrences",
    "clauses",
    "selection_composition",
    "default_selection_blueprint",
    "default_text",
    "default_expected_graph",
    "ledger_graph",
    "observation_context",
    "fingerprint",
}
_CLAUSE_FIELDS = {
    "clause_index",
    "claim_ref",
    "occurrence_ref",
    "allowed_atom_ids",
    "default_atom_id",
    "slots",
    "surface_values",
    "semantic",
    "atom_variants",
    "fingerprint",
}
_SLOT_FIELDS = {"slot_id", "slot_type", "value", "fingerprint"}
_VARIANT_FIELDS = {
    "atom_id",
    "required_slot_ids",
    "text",
    "text_fingerprint",
    "expected_claim",
    "fingerprint",
}
_OCCURRENCE_FIELDS = {
    "occurrence_ref",
    "required",
    "clause_indexes",
    "claim_refs",
    "fingerprint",
}
_CONTEXT_FIELDS = {
    "schema",
    "phrase_atom_library_version",
    "phrase_atom_library_fingerprint",
    "semantic_truth_basis_fingerprint",
    "clauses",
    "fingerprint",
}
_CONTEXT_CLAUSE_FIELDS = {
    "clause_index",
    "claim_ref",
    "allowed_atom_ids",
    "slots",
    "surface_values",
    "semantic",
    "fingerprint",
}
_GRAPH_FIELDS = {
    "schema",
    "role",
    "semantic_truth_basis_fingerprint",
    "observation_context_fingerprint",
    "claims",
    "fingerprint",
}
_GRAPH_CLAIM_FIELDS = {
    "clause_index",
    "claim_ref",
    "atom_id",
    "span_start",
    "span_end",
    "evidence_fingerprint",
    *_SEMANTIC_FIELDS,
}
_SELECTION_FIELDS = {
    "schema",
    "plan_fingerprint",
    "phrase_atom_library_version",
    "occurrences",
}
_SELECTION_OCCURRENCE_FIELDS = {"occurrence_ref", "clauses"}
_SELECTION_CLAUSE_FIELDS = {"claim_ref", "atom_id", "slot_ids"}


class NarrationPlanRuntimeError(ValueError):
    """A bounded narration plan, selection, observation, or wire proof is invalid."""


@dataclass(frozen=True)
class RenderedNarrationPlanSelection:
    """Exact code-rendered text plus the three proof graphs needed by lifecycle."""

    text: str
    selection: dict[str, Any]
    expected_graph: dict[str, Any]
    observed_graph: dict[str, Any]
    ledger_graph: dict[str, Any]
    plan_fingerprint: str
    selection_fingerprint: str
    fingerprint: str


@dataclass(frozen=True)
class ProofCompleteNarrationCandidate:
    """Accepted wire bytes and proof, ready to bind into an accepted envelope."""

    plan: dict[str, Any]
    rendered: RenderedNarrationPlanSelection
    wire_bytes: bytes
    content_type: str
    wire_fingerprint: str
    delivery_proof: dict[str, Any]
    fingerprint: str


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NarrationPlanRuntimeError("narration plan data must be finite JSON") from exc


def _json_copy(value: object) -> Any:
    return json.loads(_json_bytes(value).decode("utf-8"))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise NarrationPlanRuntimeError(f"{label} must be an object with string fields")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise NarrationPlanRuntimeError(f"{label} fields are not exact")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NarrationPlanRuntimeError(f"{label} must be non-empty text")
    return value


def _require_fp(value: object, label: str) -> str:
    text = _require_text(value, label)
    if len(text) != 71 or not text.startswith("sha256:"):
        raise NarrationPlanRuntimeError(f"{label} must be a sha256 fingerprint")
    try:
        int(text[7:], 16)
    except ValueError as exc:
        raise NarrationPlanRuntimeError(f"{label} must be a sha256 fingerprint") from exc
    return text


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    detached = _json_copy(payload)
    return {**detached, "fingerprint": content_fingerprint(detached)}


def _validate_seal(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    supplied = _require_fp(value.get("fingerprint"), f"{label}.fingerprint")
    payload = {key: _json_copy(item) for key, item in value.items() if key != "fingerprint"}
    if supplied != content_fingerprint(payload):
        raise NarrationPlanRuntimeError(f"{label} fingerprint mismatch")
    return {**payload, "fingerprint": supplied}


def _surface_profile() -> dict[str, Any]:
    return {
        "schema": NARRATION_PLAN_SURFACE_SCHEMA,
        "content_type": VISIBLE_CONTENT_TYPE,
        "renderer_version": VISIBLE_RENDERER_VERSION,
        "renderer_config_fingerprint": VISIBLE_RENDERER_CONFIG_FINGERPRINT,
        "normalization": "NFC",
        "line_endings": "LF",
        "text_encoding": "UTF-8",
        "transform": "identity",
    }


def _surface_text(value: object, label: str) -> str:
    text = _require_text(value, label)
    if "\n" in text or "\r" in text:
        raise NarrationPlanRuntimeError(f"{label} cannot contain line boundaries")
    try:
        return validate_canonical_visible_text(text)
    except NarrationFallbackRuntimeError as exc:
        raise NarrationPlanRuntimeError(f"{label} is not canonical visible text") from exc


def _semantic_row(claim: Mapping[str, Any], label: str) -> dict[str, Any]:
    missing = [field for field in _SEMANTIC_FIELDS if field not in claim]
    if missing:
        raise NarrationPlanRuntimeError(f"{label} lacks semantic fields")
    semantic = {field: _json_copy(claim[field]) for field in _SEMANTIC_FIELDS}
    _require_text(semantic["occurrence_ref"], f"{label}.occurrence_ref")
    _require_text(semantic["cause_ref"], f"{label}.cause_ref")
    _require_fp(semantic["construction_ref"], f"{label}.construction_ref")
    if semantic["actor_id"] is not None:
        _require_text(semantic["actor_id"], f"{label}.actor_id")
    subjects = semantic["subject_ids"]
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise NarrationPlanRuntimeError(
            f"{label} must have exactly one subject while AoE remains deferred"
        )
    _require_text(subjects[0], f"{label}.subject_ids[0]")
    kind = _require_text(semantic["kind"], f"{label}.kind")
    expected_time = "future" if kind == PENDING_INTENT_KIND else "current"
    if semantic["polarity"] != "positive" or semantic["actuality"] != "actual" \
            or semantic["time_scope"] != expected_time:
        raise NarrationPlanRuntimeError(
            f"{label} is not a positive actual {expected_time} claim"
        )
    if semantic["multiplicity"] != 1:
        raise NarrationPlanRuntimeError(
            f"{label} has unsupported multiplicity while AoE remains deferred"
        )
    if kind not in _PLAN_CLAIM_KINDS:
        raise NarrationPlanRuntimeError(f"{label}.kind is unsupported")
    _require_text(semantic["detail"], f"{label}.detail")
    amount = semantic["amount"]
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise NarrationPlanRuntimeError(f"{label}.amount must be an integer or null")
    if kind == QUALITATIVE_ACTION_KIND and amount is not None:
        raise NarrationPlanRuntimeError("qualitative action narration cannot carry an amount")
    if kind == PENDING_INTENT_KIND and amount is not None:
        raise NarrationPlanRuntimeError("pending intent narration cannot carry an amount")
    return semantic


def _effect_surface(semantic: Mapping[str, Any]) -> str:
    kind = semantic["kind"]
    detail = (
        _require_text(semantic["detail"], "qualitative claim detail")
        if kind in {QUALITATIVE_ACTION_KIND, PENDING_INTENT_KIND}
        else _surface_text(semantic["detail"], "claim detail")
    )
    amount = semantic["amount"]
    if kind == QUALITATIVE_ACTION_KIND:
        if amount is not None:
            raise NarrationPlanRuntimeError("qualitative action cannot carry a numeric amount")
        try:
            _capability, _action, outcome, impact = detail.split(":")
            if impact != "impact_none":
                raise ValueError
            surface = _QUALITATIVE_OUTCOME_SURFACES[outcome]
        except (KeyError, ValueError) as exc:
            raise NarrationPlanRuntimeError(
                "qualitative action detail has no exact outcome surface"
            ) from exc
    elif kind == PENDING_INTENT_KIND:
        if amount is not None:
            raise NarrationPlanRuntimeError("pending intent cannot carry a numeric amount")
        surface = f"pending intent {detail}"
    elif kind == "harm":
        if amount is None:
            surface = detail
        elif amount < 0:
            surface = f"{-amount} HP of {detail}"
        else:
            raise NarrationPlanRuntimeError("harm narration requires a negative delta or null")
    elif kind == "defeat":
        if amount is not None:
            raise NarrationPlanRuntimeError("defeat narration cannot carry a numeric amount")
        surface = "defeat" if detail == "defeated" else f"defeat state {detail}"
    elif kind == "status":
        if amount is not None:
            raise NarrationPlanRuntimeError("status narration cannot carry a numeric amount")
        surface = f"status {detail}"
    elif kind == "resource":
        surface = f"{detail} change" if amount is None else f"{detail} change of {amount}"
    elif kind == "time":
        surface = f"time change {detail}" if amount is None else f"time change {detail} by {amount}"
    elif kind == "movement":
        if amount is not None:
            raise NarrationPlanRuntimeError("movement narration cannot carry a numeric amount")
        surface = f"movement to {detail}"
    elif kind == "world":
        if amount is not None:
            raise NarrationPlanRuntimeError("world narration cannot carry a numeric amount")
        surface = f"world change {detail}"
    elif kind == "opposition_action":
        if amount is not None:
            raise NarrationPlanRuntimeError("opposition action narration cannot carry an amount")
        surface = f"opposition action {detail}"
    else:
        raise NarrationPlanRuntimeError(f"unsupported narration claim kind: {kind}")
    return _surface_text(surface, "effect surface")


def _slot(claim_ref: str, slot_type: str, value: object) -> dict[str, Any]:
    if slot_type not in _SLOT_TYPES:
        raise NarrationPlanRuntimeError("typed narration slot kind is unsupported")
    payload = {
        "slot_id": content_fingerprint({"claim_ref": claim_ref, "slot_type": slot_type}),
        "slot_type": slot_type,
        "value": _json_copy(value),
    }
    return _sealed(payload)


def _slot_map(slots: Sequence[Mapping[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)):
        raise NarrationPlanRuntimeError(f"{label} must be an ordered slot list")
    rows: dict[str, dict[str, Any]] = {}
    observed_types: list[str] = []
    for index, raw in enumerate(slots):
        row = _mapping(raw, f"{label}[{index}]")
        _exact_fields(row, _SLOT_FIELDS, f"{label}[{index}]")
        valid = _validate_seal(row, f"{label}[{index}]")
        slot_type = valid["slot_type"]
        if slot_type not in _SLOT_TYPES or slot_type in rows:
            raise NarrationPlanRuntimeError(f"{label} has duplicate or unsupported slot types")
        _require_fp(valid["slot_id"], f"{label}[{index}].slot_id")
        rows[slot_type] = valid
        observed_types.append(slot_type)
    if observed_types != list(_SLOT_TYPES):
        raise NarrationPlanRuntimeError(f"{label} slot order is not canonical")
    return rows


def _surface_values_from_slots(slots: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    rows = _slot_map(slots, "clause slots")
    actor_value = rows["actor"]["value"]
    target_value = _mapping(rows["target"]["value"], "target slot value")
    effect_value = _mapping(rows["effect"]["value"], "effect slot value")
    entity_ids = target_value.get("entity_ids")
    labels = target_value.get("labels")
    if not isinstance(entity_ids, list) or len(entity_ids) != 1 \
            or not isinstance(labels, list) or len(labels) != 1:
        raise NarrationPlanRuntimeError("target slot must bind one exact entity and label")
    values = {
        "actor": "",
        "subject": _surface_text(labels[0], "target label"),
        "effect": _surface_text(effect_value.get("surface_text"), "effect surface"),
        "move": "",
        "action": "",
        "result": "",
        "intent_text": "",
    }
    if actor_value is not None:
        actor = _mapping(actor_value, "actor slot value")
        _require_text(actor.get("entity_id"), "actor slot entity_id")
        values["actor"] = _surface_text(actor.get("label"), "actor label")
    opposition = effect_value.get("opposition")
    if opposition is not None:
        row = _mapping(opposition, "effect opposition binding")
        values["move"] = _surface_text(row.get("move_label"), "opposition move label")
    qualitative = effect_value.get("qualitative")
    if qualitative is not None:
        row = _mapping(qualitative, "effect qualitative binding")
        action_class = _require_text(row.get("action_class"), "qualitative action_class")
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", action_class):
            raise NarrationPlanRuntimeError("qualitative action class is not a canonical token")
        outcome = row.get("outcome_quality")
        if outcome not in _QUALITATIVE_OUTCOME_SURFACES:
            raise NarrationPlanRuntimeError("qualitative result has no code-authored surface")
        capability_id = _require_text(
            row.get("capability_id"), "qualitative capability_id"
        )
        if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", capability_id) is None:
            raise NarrationPlanRuntimeError("qualitative capability is not a canonical token")
        capability_leaf = re.split(r"[.:]", capability_id)[-1]
        capability_surface = re.sub(r"[_-]+", " ", capability_leaf).strip().title()
        invoked = row.get("invoked_capability_ids")
        if not isinstance(invoked, list) or any(
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", value) is None
            for value in invoked
        ):
            raise NarrationPlanRuntimeError("qualitative invoked capabilities are malformed")
        invoked_surfaces = [
            re.sub(r"[_-]+", " ", re.split(r"[.:]", value)[-1]).strip().title()
            for value in invoked
        ]
        action_surface = capability_surface + (
            " check" if action_class == "skill_check" else ""
        )
        if invoked_surfaces:
            action_surface += " using " + ", ".join(invoked_surfaces)
        values["action"] = _surface_text(action_surface, "qualitative action surface")
        values["result"] = _surface_text(
            _QUALITATIVE_OUTCOME_SURFACES[outcome], "qualitative result surface"
        )
    pending = rows["pending_intent"]["value"]
    if isinstance(pending, Mapping) and pending.get("schema") == PENDING_INTENT_FACT_SCHEMA:
        if pending.get("opening_kind") not in {"combat_opening", "following_intent"}:
            raise NarrationPlanRuntimeError("pending intent opening kind is unsupported")
        values["intent_text"] = _surface_text(
            pending.get("visible_text"), "pending intent visible text"
        )
    return values


def _format_atom(
    atom_id: str,
    surface_values: Mapping[str, str],
    *,
    observer: bool,
) -> str:
    templates = _OBSERVER_ATOM_TEMPLATES if observer else _RENDER_ATOM_TEMPLATES
    template = templates.get(atom_id)
    if template is None:
        raise NarrationPlanRuntimeError("phrase atom is not allowlisted")
    try:
        text = template.format_map(surface_values)
    except (KeyError, ValueError) as exc:
        raise NarrationPlanRuntimeError("phrase atom lost a typed surface slot") from exc
    return _surface_text(text, "rendered phrase atom")


def _allowed_atoms(slots: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = _slot_map(slots, "clause slots")
    pending = rows["pending_intent"]["value"]
    if isinstance(pending, Mapping) and pending.get("schema") == PENDING_INTENT_FACT_SCHEMA:
        opening_kind = pending.get("opening_kind")
        if opening_kind not in {"combat_opening", "following_intent"}:
            raise NarrationPlanRuntimeError("pending intent has no exact phrase atom")
        return [f"pending_intent.{opening_kind}/1"]
    effect = _mapping(rows["effect"]["value"], "effect slot value")
    qualitative = effect.get("qualitative")
    if qualitative is not None:
        event = _mapping(qualitative, "qualitative binding")
        if event.get("adapter_id") == WEAPON_ATTACK_REALIZATION_ADAPTER:
            return ["qualitative.weapon.direct/1", "qualitative.weapon.result/1"]
        if event.get("target_id") is None:
            return ["qualitative.self.direct/1", "qualitative.self.result/1"]
        return ["qualitative.target.direct/1", "qualitative.target.result/1"]
    opposition = effect.get("opposition")
    if opposition is not None:
        outcome = _mapping(opposition, "opposition binding").get("outcome")
        if outcome not in {"hit", "miss", "blocked"}:
            raise NarrationPlanRuntimeError("opposition outcome has no phrase atoms")
        return [
            f"opposition.{outcome}.direct/1",
            f"opposition.{outcome}.result/1",
        ]
    if rows["actor"]["value"] is None:
        return ["claim.system.direct/1", "claim.system.result/1"]
    return ["claim.actor.direct/1", "claim.actor.result/1"]


def _graph_claim(
    *,
    clause_index: int,
    claim_ref: str,
    atom_id: str | None,
    sentence: str,
    span_start: int,
    semantic: Mapping[str, Any],
) -> dict[str, Any]:
    encoded = sentence.encode("utf-8")
    row = {
        "clause_index": clause_index,
        "claim_ref": claim_ref,
        "atom_id": atom_id,
        "span_start": span_start,
        "span_end": span_start + len(encoded),
        "evidence_fingerprint": raw_fingerprint(encoded),
        **{field: _json_copy(semantic[field]) for field in _SEMANTIC_FIELDS},
    }
    if set(row) != _GRAPH_CLAIM_FIELDS:
        raise NarrationPlanRuntimeError("narration plan claim graph shape is invalid")
    return row


def _graph(
    *,
    role: str,
    semantic_truth_basis_fingerprint: str,
    observation_context_fingerprint: str,
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema": NARRATION_PLAN_GRAPH_SCHEMA,
        "role": role,
        "semantic_truth_basis_fingerprint": _require_fp(
            semantic_truth_basis_fingerprint, "semantic truth basis fingerprint"
        ),
        "observation_context_fingerprint": _require_fp(
            observation_context_fingerprint, "observation context fingerprint"
        ),
        "claims": [_json_copy(row) for row in claims],
    }
    return _sealed(payload)


def _validate_graph(value: object, label: str) -> dict[str, Any]:
    graph = _mapping(value, label)
    _exact_fields(graph, _GRAPH_FIELDS, label)
    valid = _validate_seal(graph, label)
    if valid["schema"] != NARRATION_PLAN_GRAPH_SCHEMA or not isinstance(valid["claims"], list):
        raise NarrationPlanRuntimeError(f"{label} schema or claims are invalid")
    _require_fp(
        valid["semantic_truth_basis_fingerprint"],
        f"{label}.semantic_truth_basis_fingerprint",
    )
    _require_fp(
        valid["observation_context_fingerprint"],
        f"{label}.observation_context_fingerprint",
    )
    for index, raw in enumerate(valid["claims"]):
        row = _mapping(raw, f"{label}.claims[{index}]")
        _exact_fields(row, _GRAPH_CLAIM_FIELDS, f"{label}.claims[{index}]")
        if row["clause_index"] != index:
            raise NarrationPlanRuntimeError(f"{label} claim ordering is not canonical")
        _semantic_row(row, f"{label}.claims[{index}]")
        _require_fp(row["claim_ref"], f"{label}.claims[{index}].claim_ref")
        _require_fp(
            row["evidence_fingerprint"], f"{label}.claims[{index}].evidence_fingerprint"
        )
    return valid


def _claim_slots(
    claim_ref: str,
    semantic: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    opposition: Mapping[str, Mapping[str, Any]],
    pending_facts: Mapping[str, Mapping[str, Any]],
    player_events: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    actor_id = semantic["actor_id"]
    actor = None
    if actor_id is not None:
        if actor_id not in labels:
            raise NarrationPlanRuntimeError("claim actor is absent from the exact entity lexicon")
        actor = {"entity_id": actor_id, "label": labels[actor_id]}
    subject_id = semantic["subject_ids"][0]
    if subject_id not in labels:
        raise NarrationPlanRuntimeError("claim subject is absent from the exact entity lexicon")

    opposition_binding: dict[str, Any] | None = None
    pending_intent: dict[str, Any] | None = None
    qualitative_binding: dict[str, Any] | None = None
    action = opposition.get(semantic["occurrence_ref"])
    pending_fact = pending_facts.get(semantic["occurrence_ref"])
    if semantic["kind"] == PENDING_INTENT_KIND:
        if pending_fact is None:
            raise NarrationPlanRuntimeError(
                "pending intent claim lacks its exact future action fact"
            )
        expected_detail = normalize_phrase(
            f"{pending_fact['move_id']} {pending_fact['tell']}"
        )
        if (
            semantic["cause_ref"] != pending_fact["intent_ref"]
            or semantic["construction_ref"] != pending_fact["construction_ref"]
            or semantic["actor_id"] != pending_fact["actor_id"]
            or semantic["subject_ids"] != [pending_fact["target_id"]]
            or semantic["detail"] != expected_detail
            or semantic["time_scope"] != "future"
            or semantic["amount"] is not None
        ):
            raise NarrationPlanRuntimeError(
                "pending intent claim differs from its exact future action fact"
            )
        pending_intent = _json_copy(pending_fact)
    elif semantic["kind"] == QUALITATIVE_ACTION_KIND:
        event = player_events.get(semantic["occurrence_ref"])
        if event is None:
            raise NarrationPlanRuntimeError(
                "qualitative claim lacks its exact Player event"
            )
        expected = _qualitative_claim_for_event(
            event,
            mechanically_claimed_occurrences=set(),
        )
        expected_semantic = (
            {field: expected[field] for field in _SEMANTIC_FIELDS}
            if expected is not None
            else None
        )
        if expected is None or expected["claim_ref"] != claim_ref \
                or semantic != expected_semantic:
            raise NarrationPlanRuntimeError(
                "qualitative claim differs from its exact settled Player event"
            )
        qualitative_binding = {
            "event_ref": event["event_ref"],
            "event_state": event["event_state"],
            "adapter_id": event["adapter_id"],
            "frame_ref": event["frame_ref"],
            "meaning_ref": event["meaning_ref"],
            "capability_id": event["capability_id"],
            "invoked_capability_ids": list(event["invoked_capability_ids"]),
            "actor_id": event["actor_id"],
            "target_id": event["target_id"],
            "action_class": event["action_class"],
            "outcome_quality": event["outcome_quality"],
            "impact_kind": event["impact_kind"],
            "target_state": event["target_state"],
            "settled_change_kinds": list(event["settled_change_kinds"]),
        }
    elif semantic["kind"] == "opposition_action":
        if action is None:
            raise NarrationPlanRuntimeError("opposition claim lacks its exact intent binding")
        expected_detail = normalize_phrase(f"{action['move_id']} {action['outcome']}")
        if semantic["actor_id"] != action["actor_id"] \
                or semantic["subject_ids"] != [action["target_id"]] \
                or semantic["detail"] != expected_detail:
            raise NarrationPlanRuntimeError("opposition claim differs from its exact action fact")
        opposition_binding = {
            "move_id": action["move_id"],
            "move_label": action["move_label"],
            "outcome": action["outcome"],
        }
        pending_intent = {
            "intent_ref": action["intent_ref"],
            "consumption": "single_use",
        }
    elif action is not None:
        # Harm or fallout from the same autonomous occurrence remains causally bound but is not the
        # action sentence itself.  Preserve the intent receipt without selecting an action atom.
        pending_intent = {
            "intent_ref": action["intent_ref"],
            "consumption": "single_use",
        }

    effect = {
        "kind": semantic["kind"],
        "detail": semantic["detail"],
        "amount": semantic["amount"],
        "surface_text": _effect_surface(semantic),
        "opposition": opposition_binding,
        "qualitative": qualitative_binding,
    }
    values = {
        "actor": actor,
        "target": {"entity_ids": [subject_id], "labels": [labels[subject_id]]},
        "effect": effect,
        "scope": {
            "polarity": semantic["polarity"],
            "actuality": semantic["actuality"],
            "time_scope": semantic["time_scope"],
            "multiplicity": semantic["multiplicity"],
        },
        "causality": {
            "claim_ref": claim_ref,
            "occurrence_ref": semantic["occurrence_ref"],
            "cause_ref": semantic["cause_ref"],
            "construction_ref": semantic["construction_ref"],
        },
        "attribution": {
            "issuer": (
                "aetherstate.pending-intent/1"
                if pending_fact is not None
                else
                "aetherstate.narrator-realization/1"
                if qualitative_binding is not None
                else "aetherstate.ledger/1"
            ),
            "channel": (
                "future_enemy_intent"
                if pending_fact is not None
                else
                "settled_qualitative_event"
                if qualitative_binding is not None
                else "mechanics"
            ),
            "phase": "prepared" if pending_fact is not None else "settled",
            "actor_id": actor_id,
            "kind": (
                "autonomous_pending_intent"
                if pending_fact is not None
                else
                "autonomous_opposition"
                if semantic["kind"] == "opposition_action"
                else "settled_qualitative_event"
                if qualitative_binding is not None
                else "settled_claim"
            ),
        },
        "pending_intent": pending_intent,
    }
    return [_slot(claim_ref, slot_type, values[slot_type]) for slot_type in _SLOT_TYPES]


def _clause(
    *,
    clause_index: int,
    claim: Mapping[str, Any],
    labels: Mapping[str, str],
    opposition: Mapping[str, Mapping[str, Any]],
    pending_facts: Mapping[str, Mapping[str, Any]],
    player_events: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    claim_ref = _require_fp(claim.get("claim_ref"), "expected claim_ref")
    semantic = _semantic_row(claim, f"expected claim {claim_ref}")
    slots = _claim_slots(
        claim_ref,
        semantic,
        labels=labels,
        opposition=opposition,
        pending_facts=pending_facts,
        player_events=player_events,
    )
    surface_values = _surface_values_from_slots(slots)
    allowed = _allowed_atoms(slots)
    slot_ids = [row["slot_id"] for row in slots]
    variants: list[dict[str, Any]] = []
    for atom_id in allowed:
        text = _format_atom(atom_id, surface_values, observer=False)
        claim_row = _graph_claim(
            clause_index=clause_index,
            claim_ref=claim_ref,
            atom_id=atom_id,
            sentence=text,
            span_start=0,
            semantic=semantic,
        )
        variants.append(
            _sealed(
                {
                    "atom_id": atom_id,
                    "required_slot_ids": slot_ids,
                    "text": text,
                    "text_fingerprint": raw_fingerprint(text.encode("utf-8")),
                    "expected_claim": claim_row,
                }
            )
        )
    payload = {
        "clause_index": clause_index,
        "claim_ref": claim_ref,
        "occurrence_ref": semantic["occurrence_ref"],
        "allowed_atom_ids": allowed,
        "default_atom_id": allowed[0],
        "slots": slots,
        "surface_values": surface_values,
        "semantic": semantic,
        "atom_variants": variants,
    }
    return _sealed(payload)


def _occurrences(clauses: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for clause in clauses:
        ref = clause["occurrence_ref"]
        if ref not in grouped:
            order.append(ref)
            grouped[ref] = []
        grouped[ref].append(clause)
    rows = []
    for ref in order:
        members = grouped[ref]
        rows.append(
            _sealed(
                {
                    "occurrence_ref": ref,
                    "required": True,
                    "clause_indexes": [row["clause_index"] for row in members],
                    "claim_refs": [row["claim_ref"] for row in members],
                }
            )
        )
    return rows


def _selection_blueprint(
    occurrences: Sequence[Mapping[str, Any]],
    clauses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_index = {row["clause_index"]: row for row in clauses}
    return {
        "schema": NARRATION_PLAN_SELECTION_BLUEPRINT_SCHEMA,
        "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "occurrences": [
            {
                "occurrence_ref": occurrence["occurrence_ref"],
                "clauses": [
                    {
                        "claim_ref": by_index[index]["claim_ref"],
                        "atom_id": by_index[index]["default_atom_id"],
                        "slot_ids": [
                            slot["slot_id"] for slot in by_index[index]["slots"]
                        ],
                    }
                    for index in occurrence["clause_indexes"]
                ],
            }
            for occurrence in occurrences
        ],
    }


def _observation_context(
    clauses: Sequence[Mapping[str, Any]],
    semantic_truth_basis_fingerprint: str,
) -> dict[str, Any]:
    context_clauses = []
    for clause in clauses:
        context_clauses.append(
            _sealed(
                {
                    "clause_index": clause["clause_index"],
                    "claim_ref": clause["claim_ref"],
                    "allowed_atom_ids": list(clause["allowed_atom_ids"]),
                    "slots": _json_copy(clause["slots"]),
                    "surface_values": _json_copy(clause["surface_values"]),
                    "semantic": _json_copy(clause["semantic"]),
                }
            )
        )
    return _sealed(
        {
            "schema": NARRATION_PLAN_OBSERVATION_CONTEXT_SCHEMA,
            "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
            "phrase_atom_library_fingerprint": NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT,
            "semantic_truth_basis_fingerprint": semantic_truth_basis_fingerprint,
            "clauses": context_clauses,
        }
    )


def _compose(
    clauses: Sequence[Mapping[str, Any]],
    atom_ids: Sequence[str],
    *,
    semantic_truth_basis_fingerprint: str,
    observation_context_fingerprint: str,
    observer: bool,
) -> tuple[str, dict[str, Any]]:
    if len(clauses) != len(atom_ids):
        raise NarrationPlanRuntimeError("selection does not cover the exact clause order")
    if not clauses:
        text = EMPTY_FALLBACK_TEXT
        validate_canonical_visible_text(text)
        return text, _graph(
            role="blind_observed" if observer else "plan_expected",
            semantic_truth_basis_fingerprint=semantic_truth_basis_fingerprint,
            observation_context_fingerprint=observation_context_fingerprint,
            claims=[],
        )

    lines: list[str] = []
    claims: list[dict[str, Any]] = []
    byte_cursor = 0
    for index, (clause, atom_id) in enumerate(zip(clauses, atom_ids)):
        line = _format_atom(atom_id, clause["surface_values"], observer=observer)
        lines.append(line)
        claims.append(
            _graph_claim(
                clause_index=index,
                claim_ref=clause["claim_ref"],
                atom_id=atom_id,
                sentence=line,
                span_start=byte_cursor,
                semantic=clause["semantic"],
            )
        )
        byte_cursor += len(line.encode("utf-8")) + (1 if index + 1 < len(clauses) else 0)
    text = "\n".join(lines)
    validate_canonical_visible_text(text)
    return text, _graph(
        role="blind_observed" if observer else "plan_expected",
        semantic_truth_basis_fingerprint=semantic_truth_basis_fingerprint,
        observation_context_fingerprint=observation_context_fingerprint,
        claims=claims,
    )


def _ledger_graph(
    clauses: Sequence[Mapping[str, Any]],
    *,
    semantic_truth_basis_fingerprint: str,
    observation_context_fingerprint: str,
) -> dict[str, Any]:
    rows = [
        _graph_claim(
            clause_index=index,
            claim_ref=clause["claim_ref"],
            atom_id=None,
            sentence="",
            span_start=0,
            semantic=clause["semantic"],
        )
        for index, clause in enumerate(clauses)
    ]
    return _graph(
        role="ledger",
        semantic_truth_basis_fingerprint=semantic_truth_basis_fingerprint,
        observation_context_fingerprint=observation_context_fingerprint,
        claims=rows,
    )


def _semantic_truth_basis(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _json_copy(value)
        for key, value in contract.items()
        if key not in {"lifecycle_binding", "fingerprint"}
    }


def _qualitative_claim_for_event(
    event: Mapping[str, Any],
    *,
    mechanically_claimed_occurrences: set[str],
) -> dict[str, Any] | None:
    """Project only settled, no-delta event truth into one bounded qualitative claim."""
    event_ref = event.get("event_ref")
    if event.get("event_state") != "settled" \
            or not isinstance(event_ref, str) or not event_ref \
            or event_ref in mechanically_claimed_occurrences \
            or event.get("impact_kind") != "none":
        return None
    outcome = event.get("outcome_quality")
    if outcome not in _QUALITATIVE_OUTCOME_SURFACES:
        return None
    changes = event.get("settled_change_kinds")
    adapter = event.get("adapter_id")
    action_class = event.get("action_class")
    target_id = event.get("target_id")
    if adapter == SKILL_CHECK_REALIZATION_ADAPTER:
        if action_class != "skill_check" or event.get("target_state") != "not_applicable" \
                or not isinstance(changes, list) \
                or any(kind not in {"cost", "mastery", "cooldown", "consequence"}
                       for kind in changes):
            return None
    elif adapter == WEAPON_ATTACK_REALIZATION_ADAPTER:
        # A weapon adapter always records its HP projection, including exact zero impact.  Only
        # the already-proved no-impact case may become qualitative narration here.
        if action_class != "weapon_attack" or not isinstance(target_id, str) \
                or event.get("target_state") != "active" or changes != ["hp"]:
            return None
    else:
        return None
    actor_id = _require_text(event.get("actor_id"), "qualitative event actor_id")
    frame_ref = _require_fp(event.get("frame_ref"), "qualitative event frame_ref")
    _require_fp(event.get("meaning_ref"), "qualitative event meaning_ref")
    _require_text(action_class, "qualitative event action_class")
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", action_class) is None:
        return None
    if target_id is not None:
        _require_text(target_id, "qualitative event target_id")
    capability_id = _require_text(event.get("capability_id"), "qualitative capability_id")
    if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", capability_id) is None:
        return None
    invoked = event.get("invoked_capability_ids")
    if not isinstance(invoked, list) or any(
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", value) is None
        for value in invoked
    ):
        return None
    subject_id = target_id or actor_id
    payload = {
        "issuer": "aetherstate.narrator-realization/1",
        "channel": "settled_qualitative_event",
        "phase": "settled",
        "occurrence_ref": event_ref,
        "cause_ref": frame_ref,
        "construction_ref": frame_ref,
        "actor_id": actor_id,
        "subject_ids": [subject_id],
        "kind": QUALITATIVE_ACTION_KIND,
        "polarity": "positive",
        "actuality": "actual",
        "time_scope": "current",
        "multiplicity": 1,
        "detail": f"{capability_id}:{action_class}:{outcome}:impact_none",
        "amount": None,
    }
    return {**payload, "claim_ref": content_fingerprint(payload)}


def _ordered_expected_claims(semantic_basis: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Choose one explicit causal narration order without changing ledger set equality."""
    raw_claims = semantic_basis.get("expected_claims")
    if not isinstance(raw_claims, list):
        raise NarrationPlanRuntimeError("semantic truth basis expected_claims must be a list")
    occurrence_order: list[str] = []

    def admit(ref: object) -> None:
        if isinstance(ref, str) and ref and ref not in occurrence_order:
            occurrence_order.append(ref)

    for row in semantic_basis.get("player_events", []):
        if isinstance(row, Mapping):
            admit(row.get("event_ref"))
    for row in semantic_basis.get("opposition_actions", []):
        if isinstance(row, Mapping):
            admit(row.get("occurrence_ref"))
    for row in semantic_basis.get("pending_intents", []):
        if isinstance(row, Mapping):
            admit(row.get("pending_ref"))
    for row in semantic_basis.get("fallout_facts", []):
        if isinstance(row, Mapping):
            admit(row.get("fact_ref"))
    for raw in raw_claims:
        if isinstance(raw, Mapping):
            admit(raw.get("occurrence_ref"))

    occurrence_rank = {ref: index for index, ref in enumerate(occurrence_order)}
    kind_rank = {
        QUALITATIVE_ACTION_KIND: 0,
        "opposition_action": 0,
        PENDING_INTENT_KIND: 0,
        "harm": 1,
        "status": 2,
        "resource": 3,
        "movement": 4,
        "time": 5,
        "world": 6,
        "defeat": 7,
    }
    claims = [
        _json_copy(_mapping(raw, f"expected_claims[{index}]"))
        for index, raw in enumerate(raw_claims)
    ]
    mechanical_occurrences = {
        row.get("occurrence_ref")
        for row in claims
        if isinstance(row.get("occurrence_ref"), str)
    }
    player_events = semantic_basis.get("player_events")
    if not isinstance(player_events, list):
        raise NarrationPlanRuntimeError("semantic truth basis player_events must be a list")
    for index, raw in enumerate(player_events):
        event = _mapping(raw, f"player_events[{index}]")
        qualitative = _qualitative_claim_for_event(
            event,
            mechanically_claimed_occurrences=mechanical_occurrences,
        )
        if qualitative is not None:
            claims.append(qualitative)
    return sorted(
        claims,
        key=lambda row: (
            occurrence_rank.get(row.get("occurrence_ref"), len(occurrence_rank)),
            kind_rank.get(row.get("kind"), 999),
            _json_bytes(row),
        ),
    )


def _validate_lifecycle_binding(
    value: object,
    *,
    realization_fingerprint: str,
) -> dict[str, str]:
    row = _mapping(value, "plan lifecycle binding")
    if set(row) != {"branch_ref", "ledger_fingerprint", "artifact_fingerprint"}:
        raise NarrationPlanRuntimeError("plan lifecycle binding fields are not exact")
    binding = {
        "branch_ref": _require_text(row["branch_ref"], "lifecycle branch_ref"),
        "ledger_fingerprint": _require_fp(
            row["ledger_fingerprint"], "lifecycle ledger_fingerprint"
        ),
        "artifact_fingerprint": _require_fp(
            row["artifact_fingerprint"], "lifecycle artifact_fingerprint"
        ),
    }
    if binding["artifact_fingerprint"] != realization_fingerprint:
        raise NarrationPlanRuntimeError("lifecycle binding lost realization identity")
    return binding


def _build_plan_from_basis(
    semantic_basis: Mapping[str, Any],
    *,
    source_truth_contract_fingerprint: str,
    source_lifecycle_binding: Mapping[str, Any] | None = None,
    lifecycle_binding: Mapping[str, Any],
) -> dict[str, Any]:
    basis = _json_copy(semantic_basis)
    basis_fp = content_fingerprint(basis)
    realization_fp = _require_fp(
        basis.get("realization_fingerprint"), "basis realization_fingerprint"
    )
    current_binding = _validate_lifecycle_binding(
        lifecycle_binding,
        realization_fingerprint=realization_fp,
    )
    origin_binding = _validate_lifecycle_binding(
        source_lifecycle_binding or current_binding,
        realization_fingerprint=realization_fp,
    )
    source_fp = _require_fp(
        source_truth_contract_fingerprint, "source truth contract fingerprint"
    )
    if source_fp != content_fingerprint(
        {**basis, "lifecycle_binding": origin_binding}
    ):
        raise NarrationPlanRuntimeError(
            "source truth contract fingerprint differs from the complete retained basis"
        )
    expected = _ordered_expected_claims(basis)
    known = basis.get("known_entities")
    opposition_rows = basis.get("opposition_actions")
    pending_rows = basis.get("pending_intents")
    player_event_rows = basis.get("player_events")
    if not isinstance(known, list) or not isinstance(opposition_rows, list) \
            or not isinstance(pending_rows, list) \
            or not isinstance(player_event_rows, list):
        raise NarrationPlanRuntimeError("semantic truth basis lacks typed narration rows")
    if len(expected) > MAX_PLAN_CLAIMS:
        raise NarrationPlanRuntimeError("narration plan exceeds the bounded claim count")

    labels: dict[str, str] = {}
    for index, raw in enumerate(known):
        row = _mapping(raw, f"known_entities[{index}]")
        entity_id = _require_text(row.get("entity_id"), f"known_entities[{index}].entity_id")
        label = _surface_text(row.get("label"), f"known_entities[{index}].label")
        if entity_id in labels and labels[entity_id] != label:
            raise NarrationPlanRuntimeError("entity labels conflict inside narration plan")
        labels[entity_id] = label
    opposition = {
        _require_text(row.get("occurrence_ref"), "opposition occurrence_ref"): row
        for row in (
            _mapping(raw, f"opposition_actions[{index}]")
            for index, raw in enumerate(opposition_rows)
        )
    }
    pending_facts = {
        _require_text(row.get("pending_ref"), "pending intent occurrence_ref"): row
        for row in (
            _mapping(raw, f"pending_intents[{index}]")
            for index, raw in enumerate(pending_rows)
        )
    }
    if len(pending_facts) != len(pending_rows):
        raise NarrationPlanRuntimeError("pending intent references must be unique")
    player_events = {
        _require_text(row.get("event_ref"), "Player event_ref"): row
        for row in (
            _mapping(raw, f"player_events[{index}]")
            for index, raw in enumerate(player_event_rows)
        )
    }
    if len(player_events) != len(player_event_rows):
        raise NarrationPlanRuntimeError("Player event references must be unique")
    clauses = [
        _clause(
            clause_index=index,
            claim=_mapping(claim, f"expected_claims[{index}]"),
            labels=labels,
            opposition=opposition,
            pending_facts=pending_facts,
            player_events=player_events,
        )
        for index, claim in enumerate(expected)
    ]
    claim_refs = [row["claim_ref"] for row in clauses]
    if len(claim_refs) != len(set(claim_refs)):
        raise NarrationPlanRuntimeError("narration plan claim references must be unique")
    occurrences = _occurrences(clauses)
    occurrence_refs = [row["occurrence_ref"] for row in occurrences]
    context = _observation_context(clauses, basis_fp)
    default_atoms = [row["default_atom_id"] for row in clauses]
    default_text, default_graph = _compose(
        clauses,
        default_atoms,
        semantic_truth_basis_fingerprint=basis_fp,
        observation_context_fingerprint=context["fingerprint"],
        observer=False,
    )
    ledger_graph = _ledger_graph(
        clauses,
        semantic_truth_basis_fingerprint=basis_fp,
        observation_context_fingerprint=context["fingerprint"],
    )
    ledger_projection_fp = content_fingerprint(canonical_claim_projection(ledger_graph))
    payload = {
        "schema": NARRATION_REALIZATION_PLAN_SCHEMA,
        "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "phrase_atom_library_fingerprint": NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT,
        "source_truth_contract_fingerprint": source_fp,
        "source_lifecycle_binding": origin_binding,
        "semantic_truth_basis": basis,
        "semantic_truth_basis_fingerprint": basis_fp,
        "turn": basis.get("turn"),
        "delivery_mode": basis.get("delivery_mode"),
        "lifecycle_binding": current_binding,
        "surface_profile": _surface_profile(),
        "ledger_projection_fingerprint": ledger_projection_fp,
        "required_occurrence_refs": occurrence_refs,
        "allowed_occurrence_refs": list(occurrence_refs),
        "occurrences": occurrences,
        "clauses": clauses,
        "selection_composition": {
            "separator": "LF",
            "ordering": "exact_clause_index",
            "expected_graphs": "factorized_atom_variants/1",
            "unplanned_clauses": "forbidden",
        },
        "default_selection_blueprint": _selection_blueprint(occurrences, clauses),
        "default_text": default_text,
        "default_expected_graph": default_graph,
        "ledger_graph": ledger_graph,
        "observation_context": context,
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def build_narration_realization_plan(contract: object) -> dict[str, Any]:
    """Build one complete finite narration language from a typed runtime truth contract."""
    try:
        valid = validate_narration_truth_contract(contract)
        lifecycle = valid["lifecycle_binding"]
        if lifecycle is None:
            raise NarrationPlanRuntimeError("runtime narration plan requires lifecycle identity")
        binding = _validate_lifecycle_binding(
            lifecycle,
            realization_fingerprint=valid["realization_fingerprint"],
        )
        plan = _build_plan_from_basis(
            _semantic_truth_basis(valid),
            source_truth_contract_fingerprint=valid["fingerprint"],
            source_lifecycle_binding=binding,
            lifecycle_binding=binding,
        )
        return validate_narration_realization_plan(plan)
    except NarrationPlanRuntimeError:
        raise
    except (NarrationTruthGateError, NarrationFallbackRuntimeError, TurnArtifactError) as exc:
        raise NarrationPlanRuntimeError("narration realization plan construction failed") from exc


def validate_narration_realization_plan(value: object) -> dict[str, Any]:
    """Validate a detached complete plan basis and every finite phrase option."""
    plan = _mapping(value, "narration realization plan")
    _exact_fields(plan, _PLAN_FIELDS, "narration realization plan")
    if plan["schema"] != NARRATION_REALIZATION_PLAN_SCHEMA:
        raise NarrationPlanRuntimeError("unsupported narration realization plan schema")
    if plan["phrase_atom_library_version"] != NARRATION_PHRASE_ATOM_LIBRARY_VERSION \
            or plan["phrase_atom_library_fingerprint"] \
            != NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT:
        raise NarrationPlanRuntimeError("narration phrase atom library identity drifted")
    _require_fp(plan["source_truth_contract_fingerprint"], "source truth contract fingerprint")
    basis = _mapping(plan["semantic_truth_basis"], "semantic truth basis")
    basis_fp = _require_fp(
        plan["semantic_truth_basis_fingerprint"], "semantic truth basis fingerprint"
    )
    if basis_fp != content_fingerprint(basis):
        raise NarrationPlanRuntimeError("semantic truth basis fingerprint mismatch")
    if plan["turn"] != basis.get("turn") or plan["delivery_mode"] != basis.get("delivery_mode"):
        raise NarrationPlanRuntimeError("plan turn or delivery mode differs from semantic truth")
    binding = _validate_lifecycle_binding(
        plan["lifecycle_binding"],
        realization_fingerprint=_require_fp(
            basis.get("realization_fingerprint"), "basis realization_fingerprint"
        ),
    )
    expected = _build_plan_from_basis(
        basis,
        source_truth_contract_fingerprint=plan["source_truth_contract_fingerprint"],
        source_lifecycle_binding=_mapping(
            plan["source_lifecycle_binding"], "source lifecycle binding"
        ),
        lifecycle_binding=binding,
    )
    if _json_bytes(expected) != _json_bytes(plan):
        raise NarrationPlanRuntimeError("narration realization plan is not its exact code-authored form")
    return _json_copy(expected)


def rebind_narration_realization_plan(
    plan: object,
    *,
    branch_ref: str,
) -> dict[str, Any]:
    """Purely rotate fork lineage while retaining the exact truth and ledger roots."""
    valid = validate_narration_realization_plan(plan)
    new_branch = _require_text(branch_ref, "fork branch_ref")
    if new_branch == valid["lifecycle_binding"]["branch_ref"]:
        return valid
    binding = {
        **valid["lifecycle_binding"],
        "branch_ref": new_branch,
    }
    rebound = _build_plan_from_basis(
        valid["semantic_truth_basis"],
        source_truth_contract_fingerprint=valid["source_truth_contract_fingerprint"],
        source_lifecycle_binding=valid["source_lifecycle_binding"],
        lifecycle_binding=binding,
    )
    return validate_narration_realization_plan(rebound)


def build_narration_plan_request(plan: object) -> dict[str, Any]:
    """Return the bounded model-facing option/slot catalog; it contains no source prose."""
    valid = validate_narration_realization_plan(plan)
    by_index = {row["clause_index"]: row for row in valid["clauses"]}
    payload = {
        "schema": NARRATION_PLAN_REQUEST_SCHEMA,
        "plan_fingerprint": valid["fingerprint"],
        "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "phrase_atom_library_fingerprint": NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT,
        "required_occurrence_refs": list(valid["required_occurrence_refs"]),
        "allowed_occurrence_refs": list(valid["allowed_occurrence_refs"]),
        "occurrences": [
            {
                "occurrence_ref": occurrence["occurrence_ref"],
                "clauses": [
                    {
                        "claim_ref": by_index[index]["claim_ref"],
                        "allowed_atom_ids": list(by_index[index]["allowed_atom_ids"]),
                        "slot_catalog": _json_copy(by_index[index]["slots"]),
                    }
                    for index in occurrence["clause_indexes"]
                ],
            }
            for occurrence in valid["occurrences"]
        ],
        "response_contract": {
            "schema": NARRATION_PLAN_SELECTION_SCHEMA,
            "return_only": ["occurrence_ref", "claim_ref", "atom_id", "slot_ids"],
            "prose": "forbidden",
            "values": "forbidden",
        },
    }
    return _sealed(payload)


def build_default_narration_plan_selection(plan: object) -> dict[str, Any]:
    """Return the exact deterministic default IDs, suitable for no-model fallback selection."""
    valid = validate_narration_realization_plan(plan)
    blueprint = valid["default_selection_blueprint"]
    return {
        "schema": NARRATION_PLAN_SELECTION_SCHEMA,
        "plan_fingerprint": valid["fingerprint"],
        "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "occurrences": _json_copy(blueprint["occurrences"]),
    }


def _selection_object(value: object) -> Mapping[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SELECTION_BYTES:
            raise NarrationPlanRuntimeError("model selection exceeds the bounded JSON size")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise NarrationPlanRuntimeError(
                "model output is not one exact narration plan selection object"
            ) from exc
        return _mapping(decoded, "model narration plan selection")
    return _mapping(value, "narration plan selection")


def validate_narration_plan_selection(value: object, plan: object) -> dict[str, Any]:
    """Validate IDs only; any model-supplied prose or slot value is an extra field and rejects."""
    valid_plan = validate_narration_realization_plan(plan)
    selection = _selection_object(value)
    _exact_fields(selection, _SELECTION_FIELDS, "narration plan selection")
    if selection["schema"] != NARRATION_PLAN_SELECTION_SCHEMA \
            or selection["plan_fingerprint"] != valid_plan["fingerprint"] \
            or selection["phrase_atom_library_version"] \
            != NARRATION_PHRASE_ATOM_LIBRARY_VERSION:
        raise NarrationPlanRuntimeError("narration plan selection identity mismatch")
    supplied_occurrences = selection["occurrences"]
    expected_occurrences = valid_plan["occurrences"]
    if not isinstance(supplied_occurrences, list) \
            or len(supplied_occurrences) != len(expected_occurrences):
        raise NarrationPlanRuntimeError("selection does not cover exact required occurrences")

    clauses_by_index = {row["clause_index"]: row for row in valid_plan["clauses"]}
    canonical_occurrences: list[dict[str, Any]] = []
    for occurrence_index, (supplied_raw, expected_occurrence) in enumerate(
        zip(supplied_occurrences, expected_occurrences)
    ):
        supplied = _mapping(supplied_raw, f"selection occurrences[{occurrence_index}]")
        _exact_fields(
            supplied,
            _SELECTION_OCCURRENCE_FIELDS,
            f"selection occurrences[{occurrence_index}]",
        )
        if supplied["occurrence_ref"] != expected_occurrence["occurrence_ref"]:
            raise NarrationPlanRuntimeError("selection occurrence order or identity changed")
        selected_clauses = supplied["clauses"]
        expected_indexes = expected_occurrence["clause_indexes"]
        if not isinstance(selected_clauses, list) \
                or len(selected_clauses) != len(expected_indexes):
            raise NarrationPlanRuntimeError("selection misses or duplicates a required clause")
        canonical_clauses: list[dict[str, Any]] = []
        for clause_offset, (selected_raw, clause_index) in enumerate(
            zip(selected_clauses, expected_indexes)
        ):
            selected = _mapping(
                selected_raw,
                f"selection occurrences[{occurrence_index}].clauses[{clause_offset}]",
            )
            _exact_fields(
                selected,
                _SELECTION_CLAUSE_FIELDS,
                f"selection occurrences[{occurrence_index}].clauses[{clause_offset}]",
            )
            clause = clauses_by_index[clause_index]
            if selected["claim_ref"] != clause["claim_ref"]:
                raise NarrationPlanRuntimeError("selection claim identity changed")
            atom_id = selected["atom_id"]
            if atom_id not in clause["allowed_atom_ids"]:
                raise NarrationPlanRuntimeError("selection chose an unknown phrase atom")
            slot_ids = selected["slot_ids"]
            expected_slot_ids = [slot["slot_id"] for slot in clause["slots"]]
            if slot_ids != expected_slot_ids:
                raise NarrationPlanRuntimeError("selection changed, omitted, or duplicated slot IDs")
            canonical_clauses.append(
                {
                    "claim_ref": clause["claim_ref"],
                    "atom_id": atom_id,
                    "slot_ids": list(expected_slot_ids),
                }
            )
        canonical_occurrences.append(
            {
                "occurrence_ref": expected_occurrence["occurrence_ref"],
                "clauses": canonical_clauses,
            }
        )
    return {
        "schema": NARRATION_PLAN_SELECTION_SCHEMA,
        "plan_fingerprint": valid_plan["fingerprint"],
        "phrase_atom_library_version": NARRATION_PHRASE_ATOM_LIBRARY_VERSION,
        "occurrences": canonical_occurrences,
    }


def _selected_atom_ids(selection: Mapping[str, Any]) -> list[str]:
    return [
        clause["atom_id"]
        for occurrence in selection["occurrences"]
        for clause in occurrence["clauses"]
    ]


def _validate_observation_context(value: object) -> dict[str, Any]:
    context = _mapping(value, "narration plan observation context")
    _exact_fields(context, _CONTEXT_FIELDS, "narration plan observation context")
    valid = _validate_seal(context, "narration plan observation context")
    if valid["schema"] != NARRATION_PLAN_OBSERVATION_CONTEXT_SCHEMA \
            or valid["phrase_atom_library_version"] \
            != NARRATION_PHRASE_ATOM_LIBRARY_VERSION \
            or valid["phrase_atom_library_fingerprint"] \
            != NARRATION_PHRASE_ATOM_LIBRARY_FINGERPRINT:
        raise NarrationPlanRuntimeError("observation context library identity mismatch")
    _require_fp(
        valid["semantic_truth_basis_fingerprint"],
        "observation semantic truth basis fingerprint",
    )
    if not isinstance(valid["clauses"], list):
        raise NarrationPlanRuntimeError("observation context clauses must be a list")
    for index, raw in enumerate(valid["clauses"]):
        row = _mapping(raw, f"observation context clauses[{index}]")
        _exact_fields(row, _CONTEXT_CLAUSE_FIELDS, f"observation context clauses[{index}]")
        sealed = _validate_seal(row, f"observation context clauses[{index}]")
        if sealed["clause_index"] != index:
            raise NarrationPlanRuntimeError("observation context clause order changed")
        _require_fp(sealed["claim_ref"], "observation claim_ref")
        slots = _slot_map(sealed["slots"], "observation slots")
        expected_surface = _surface_values_from_slots(list(slots.values()))
        if sealed["surface_values"] != expected_surface:
            raise NarrationPlanRuntimeError("observation surface values differ from typed slots")
        semantic = _semantic_row(sealed["semantic"], "observation semantic claim")
        if semantic["occurrence_ref"] \
                != slots["causality"]["value"]["occurrence_ref"]:
            raise NarrationPlanRuntimeError("observation occurrence binding changed")
        if sealed["allowed_atom_ids"] != _allowed_atoms(list(slots.values())):
            raise NarrationPlanRuntimeError("observation atom allowlist changed")
    return valid


def observe_narration_plan_text(
    text: object,
    observation_context: object,
) -> dict[str, Any]:
    """Blindly recognize exact code-atom lines without access to selection or expected graph."""
    try:
        visible = validate_canonical_visible_text(text)
    except NarrationFallbackRuntimeError as exc:
        raise NarrationPlanRuntimeError("candidate visible text is not canonical") from exc
    context = _validate_observation_context(observation_context)
    clauses = context["clauses"]
    if not clauses:
        if visible != EMPTY_FALLBACK_TEXT:
            raise NarrationPlanRuntimeError("empty truth plan has non-default visible text")
        return _graph(
            role="blind_observed",
            semantic_truth_basis_fingerprint=context["semantic_truth_basis_fingerprint"],
            observation_context_fingerprint=context["fingerprint"],
            claims=[],
        )
    lines = visible.split("\n")
    if len(lines) != len(clauses):
        raise NarrationPlanRuntimeError("visible text misses or adds a planned clause")
    claims = []
    byte_cursor = 0
    for index, (line, clause) in enumerate(zip(lines, clauses)):
        matches = [
            atom_id
            for atom_id in clause["allowed_atom_ids"]
            if _format_atom(atom_id, clause["surface_values"], observer=True) == line
        ]
        if len(matches) != 1:
            raise NarrationPlanRuntimeError(
                "visible clause is absent from or ambiguous within the observer atom language"
            )
        claims.append(
            _graph_claim(
                clause_index=index,
                claim_ref=clause["claim_ref"],
                atom_id=matches[0],
                sentence=line,
                span_start=byte_cursor,
                semantic=clause["semantic"],
            )
        )
        byte_cursor += len(line.encode("utf-8")) + (1 if index + 1 < len(lines) else 0)
    return _graph(
        role="blind_observed",
        semantic_truth_basis_fingerprint=context["semantic_truth_basis_fingerprint"],
        observation_context_fingerprint=context["fingerprint"],
        claims=claims,
    )


def render_narration_plan_selection(
    plan: object,
    selection: object,
) -> RenderedNarrationPlanSelection:
    """Code-render a valid ID selection and prove its visible graph against ledger truth."""
    valid_plan = validate_narration_realization_plan(plan)
    valid_selection = validate_narration_plan_selection(selection, valid_plan)
    atom_ids = _selected_atom_ids(valid_selection)
    text, expected_graph = _compose(
        valid_plan["clauses"],
        atom_ids,
        semantic_truth_basis_fingerprint=valid_plan["semantic_truth_basis_fingerprint"],
        observation_context_fingerprint=valid_plan["observation_context"]["fingerprint"],
        observer=False,
    )
    observed_graph = observe_narration_plan_text(
        text,
        valid_plan["observation_context"],
    )
    ledger_graph = _validate_graph(valid_plan["ledger_graph"], "plan ledger graph")
    if expected_graph["claims"] != observed_graph["claims"]:
        raise NarrationPlanRuntimeError("blind observed graph differs from selected atom graph")
    projections = [
        canonical_claim_projection(graph)
        for graph in (expected_graph, observed_graph, ledger_graph)
    ]
    if not (projections[0] == projections[1] == projections[2]):
        raise NarrationPlanRuntimeError("selected narration graph differs from ledger truth")
    selection_fp = content_fingerprint(valid_selection)
    payload = {
        "schema": "rendered-narration-plan-selection/1",
        "plan_fingerprint": valid_plan["fingerprint"],
        "selection_fingerprint": selection_fp,
        "text_fingerprint": raw_fingerprint(text.encode("utf-8")),
        "expected_graph_fingerprint": expected_graph["fingerprint"],
        "observed_graph_fingerprint": observed_graph["fingerprint"],
        "ledger_graph_fingerprint": ledger_graph["fingerprint"],
    }
    return RenderedNarrationPlanSelection(
        text=text,
        selection=_json_copy(valid_selection),
        expected_graph=_json_copy(expected_graph),
        observed_graph=_json_copy(observed_graph),
        ledger_graph=_json_copy(ledger_graph),
        plan_fingerprint=valid_plan["fingerprint"],
        selection_fingerprint=selection_fp,
        fingerprint=content_fingerprint(payload),
    )


def build_proof_complete_narration_candidate(
    plan: object,
    selection: object,
    *,
    model: str,
    stream: bool,
    logical_message_identity: str,
) -> ProofCompleteNarrationCandidate:
    """Build exact accepted wire bytes and a proof suitable for ``build_envelope``.

    The caller must persist the returned complete ``plan`` and validated ``selection`` with the
    accepted envelope.  On retry/reopen it replays ``wire_bytes``; it does not rebuild from current
    code.  A fork may use :func:`rebind_narration_realization_plan` before first delivery.
    """
    try:
        valid_plan = validate_narration_realization_plan(plan)
        rendered = render_narration_plan_selection(valid_plan, selection)
        message_id = _require_fp(logical_message_identity, "logical_message_identity")
        artifact_ref = content_fingerprint(
            {
                "schema": "narration-plan-wire-ref/1",
                "logical_message_identity": message_id,
                "plan_fingerprint": valid_plan["fingerprint"],
                "selection_fingerprint": rendered.selection_fingerprint,
                "ledger_fingerprint": valid_plan["lifecycle_binding"]["ledger_fingerprint"],
            }
        )
        wire = encode_chat_story(
            rendered.text,
            model=model,
            stream=stream,
            artifact_ref=artifact_ref,
        )
        decoded = decode_chat_story(wire.raw, wire.content_type)
        canonical = validate_canonical_visible_text(decoded)
        if canonical != rendered.text \
                or canonical.encode("utf-8") != rendered.text.encode("utf-8"):
            raise NarrationPlanRuntimeError("accepted wire differs from code-rendered visible text")
        renderer_bytes = _json_bytes(valid_plan["surface_profile"])
        proof = build_delivery_proof(
            wire_bytes=wire.raw,
            content_type=wire.content_type,
            renderer_bytes=renderer_bytes,
            visible_bytes=rendered.text.encode("utf-8"),
            expected_graph=rendered.expected_graph,
            observed_graph=rendered.observed_graph,
            ledger_graph=rendered.ledger_graph,
            ledger_root_hash=valid_plan["lifecycle_binding"]["ledger_fingerprint"],
            logical_message_identity=message_id,
            gate_reason_code="proved_plan_selection",
            artifact_kind="accepted",
        )
        payload = {
            "schema": "proof-complete-narration-candidate/1",
            "plan_fingerprint": valid_plan["fingerprint"],
            "rendered_fingerprint": rendered.fingerprint,
            "wire_fingerprint": raw_fingerprint(wire.raw),
            "content_type": wire.content_type,
            "delivery_proof_fingerprint": proof["proof_fingerprint"],
        }
        return ProofCompleteNarrationCandidate(
            plan=_json_copy(valid_plan),
            rendered=rendered,
            wire_bytes=wire.raw,
            content_type=wire.content_type,
            wire_fingerprint=raw_fingerprint(wire.raw),
            delivery_proof=_json_copy(proof),
            fingerprint=content_fingerprint(payload),
        )
    except NarrationPlanRuntimeError:
        raise
    except (
        ChatWireError,
        NarrationFallbackRuntimeError,
        TurnArtifactError,
        TypeError,
        ValueError,
    ) as exc:
        raise NarrationPlanRuntimeError("proof-complete narration candidate construction failed") from exc
