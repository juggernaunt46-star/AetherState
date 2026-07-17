"""Content-free semantic bindings and independent world-alignment receipts.

The Semantic Fabric recognizes language.  This module freezes how one bounded event consumes
that recognition without granting it mechanical authority, then aligns only an explicitly named
role or predicate against one exact world snapshot.  It deliberately contains no reducer logic.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .capability_glossary import content_fingerprint, normalize_phrase
from .semantic_fabric import CompiledMeaning, SemanticLexMatch


MEANING_BINDING_SCHEMA = "semantic-frame-meaning-binding/1"
WORLD_ALIGNMENT_SCHEMA = "semantic-world-alignment/1"
WORLD_SNAPSHOT_SCHEMA = "semantic-world-alignment-snapshot/1"

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")

_MATCH_REF_FIELDS = {
    "lex_id",
    "concept_id",
    "start",
    "end",
    "entry_fingerprint",
}
_SCOPE_NODE_FIELDS = {
    "scope_ref",
    "kind",
    "span_start",
    "span_end",
    "content_start",
    "content_end",
    "parent_scope_ref",
    "construction_role",
    "evidence_refs",
}
_CONSTRAINT_FIELDS = {
    "constraint_id",
    "scope_ref",
    "target_event_ref",
    "dimension",
    "value",
    "evidence_refs",
}
_FIELD_PROVENANCE_FIELDS = {"field", "value", "defaulted", "evidence_refs"}
_ROLE_EVIDENCE_FIELDS = {"role", "evidence_refs"}
_COORDINATION_EDGE_FIELDS = {
    "relation",
    "from_event_ref",
    "to_event_ref",
    "role",
    "evidence_refs",
}

_CONSTRUCTION_ROLES = {
    "content",
    "name_or_label",
    "mention",
    "ordinary_argument",
    "unresolved",
}
_COORDINATION_RELATIONS = {
    "independent",
    "shared_argument",
    "ordered_dependency",
    "definition_atomic",
    "alternative",
    "condition_consequence",
}
_CONSTRAINT_INTEGRITY = {"valid", "conflict"}
_REQUEST_DISPOSITIONS = {
    "direct_player_request",
    "described",
    "attributed",
    "denied",
    "conditional",
    "unresolved",
}
_MECHANIC_DISPOSITIONS = {
    "candidate",
    "recognition_only",
    "hold_unresolved",
    "invalid_scope_conflict",
}

_RECOGNITION_ONLY_CONSTRAINTS = {
    ("assertion_context", "reported"),
    ("assertion_context", "testimony"),
    ("assertion_context", "remembered"),
    ("assertion_context", "believed"),
    ("assertion_context", "observed_content"),
    ("assertion_context", "quoted"),
    ("assertion_context", "attributed"),
    ("polarity", "negative"),
    ("modality", "question"),
    ("modality", "hypothetical"),
    ("modality", "possible"),
    ("time_scope", "past"),
    ("time_scope", "future"),
}
_HOLD_CONSTRAINT_VALUES = {"unresolved", "uncheckable", "incomplete"}


class SemanticBindingError(ValueError):
    """A meaning binding, adapter contract, or world alignment is malformed."""


# These are code-owned adapters, not corpus authority.  A Translation Memory may add surface
# terms to an existing concept, but changing what that stable concept means cannot silently select
# a different mechanic.  A genuinely new action topology requires an explicit adapter revision.
_ACTION_CONCEPT_CONTRACTS: dict[str, dict[str, Any]] = {
    "action.communicate": {
        "action_class": "communication",
        "required_roles": ("actor", "message"),
        "completion": "actor_and_message_required",
        "features": {"action_class": "communication", "content_authority_separate": True},
    },
    "action.conceal": {
        "action_class": "concealment",
        "required_roles": ("actor", "theme"),
        "completion": "theme_and_visibility_scope_required",
        "features": {"action_class": "concealment", "visibility_change_requires_receipt": True},
    },
    "action.create": {
        "action_class": "creation",
        "required_roles": ("actor", "product"),
        "completion": "product_and_supporting_authority_required",
        "features": {"action_class": "creation", "existence_not_implied_without_receipt": True},
    },
    "action.destroy": {
        "action_class": "destruction",
        "required_roles": ("actor", "patient"),
        "completion": "patient_and_supported_change_required",
        "features": {"action_class": "destruction", "result_not_implied_without_receipt": True},
    },
    "action.detect": {
        "action_class": "detection",
        "required_roles": ("experiencer", "stimulus"),
        "completion": "stimulus_and_supported_knowledge_required",
        "features": {
            "action_class": "detection",
            "hidden_truth_not_implied_without_receipt": True,
        },
    },
    "action.inspect": {
        "action_class": "inspection",
        "required_roles": ("actor", "target"),
        "completion": "actor_target_and_actual_scope_required",
        "features": {"action_class": "inspection", "damage_not_implied": True},
    },
    "action.kill_attempt": {
        "action_class": "kill_attempt",
        "required_roles": ("actor", "target"),
        "completion": "actor_target_and_actual_scope_required",
        "features": {
            "action_class": "kill_attempt",
            "mechanics_require_receipt": True,
            "outcome_not_asserted": True,
        },
    },
    "action.move": {
        "action_class": "movement",
        "required_roles": ("theme",),
        "completion": "theme_and_actual_scope_required",
        "features": {"action_class": "movement", "location_change_candidate": True},
    },
    "action.negotiate": {
        "action_class": "social_influence",
        "required_roles": ("actor", "target"),
        "completion": "target_and_response_or_check_required",
        "features": {"action_class": "social_influence", "target_reaction_not_authored": True},
    },
    "action.repair": {
        "action_class": "repair_or_heal",
        "required_roles": ("actor", "patient"),
        "completion": "patient_and_supported_change_required",
        "features": {
            "action_class": "repair_or_heal",
            "result_not_implied_without_receipt": True,
        },
    },
    "action.restrain": {
        "action_class": "restraint",
        "required_roles": ("actor", "patient"),
        "completion": "patient_and_supported_control_required",
        "features": {"action_class": "restraint", "status_not_implied_without_receipt": True},
    },
    "action.transfer": {
        "action_class": "transfer",
        "required_roles": ("actor", "theme"),
        "completion": "theme_and_counterparty_resolution_required",
        "features": {"action_class": "transfer", "ledger_ownership_not_implied": True},
    },
    "action.transform": {
        "action_class": "transformation",
        "required_roles": ("actor", "patient"),
        "completion": "patient_and_supported_end_state_required",
        "features": {
            "action_class": "transformation",
            "result_not_implied_without_receipt": True,
        },
    },
    "action.use_capability": {
        "action_class": "capability_use",
        "required_roles": ("actor", "capability"),
        "completion": "owned_capability_and_application_required",
        "features": {"action_class": "capability_use", "worldlex_authority_required": True},
    },
    "action.weapon_attack": {
        "action_class": "weapon_attack",
        "required_roles": ("actor", "target"),
        "completion": "actor_target_and_actual_scope_required",
        "features": {"action_class": "weapon_attack", "mechanics_require_receipt": True},
    },
}


def _clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise SemanticBindingError("semantic binding values must be JSON-compatible") from exc


def _stable_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise SemanticBindingError(f"{label} must be a stable identifier")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise SemanticBindingError(f"{label} must be a content fingerprint")
    return value


def semantic_match_ref(match: SemanticLexMatch | Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact content-free identity of one committed recognition match."""
    row = match.as_dict() if isinstance(match, SemanticLexMatch) else dict(match)
    ref = {
        "lex_id": row.get("lex_id"),
        "concept_id": row.get("concept_id"),
        "start": row.get("start"),
        "end": row.get("end"),
        "entry_fingerprint": row.get("entry_fingerprint"),
    }
    return _validate_match_ref(ref)


def _validate_match_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _MATCH_REF_FIELDS:
        raise SemanticBindingError("semantic match reference fields do not match v1")
    lex_id = _stable_id(value.get("lex_id"), "semantic match lex_id")
    concept_id = _stable_id(value.get("concept_id"), "semantic match concept_id")
    if lex_id != "capability" and not concept_id.startswith(lex_id + "."):
        raise SemanticBindingError("semantic match concept belongs to a different Lex")
    start, end = value.get("start"), value.get("end")
    if isinstance(start, bool) or isinstance(end, bool) \
            or not isinstance(start, int) or not isinstance(end, int) \
            or start < 0 or end <= start:
        raise SemanticBindingError("semantic match reference span is invalid")
    _fingerprint(value.get("entry_fingerprint"), "semantic match entry fingerprint")
    return dict(value)


def validate_action_match_contract(match: SemanticLexMatch | Mapping[str, Any]) -> str:
    """Validate one ActionLex row against its closed, code-owned topology adapter."""
    row = match.as_dict() if isinstance(match, SemanticLexMatch) else dict(match)
    if row.get("lex_id") != "action":
        raise SemanticBindingError("action adapter received a non-ActionLex match")
    concept_id = str(row.get("concept_id") or "")
    contract = _ACTION_CONCEPT_CONTRACTS.get(concept_id)
    if contract is None:
        raise SemanticBindingError(f"ActionLex concept has no code-owned adapter: {concept_id}")
    if tuple(row.get("required_roles") or ()) != contract["required_roles"] \
            or row.get("completion") != contract["completion"] \
            or row.get("features") != contract["features"]:
        raise SemanticBindingError(f"ActionLex concept contract changed: {concept_id}")
    return str(contract["action_class"])


def action_classes_for_matches(
    matches: Iterable[SemanticLexMatch | Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return stable topology classes; free-form corpus values never select mechanics."""
    classes: set[str] = set()
    for match in matches:
        row = match.as_dict() if isinstance(match, SemanticLexMatch) else dict(match)
        if row.get("lex_id") != "action":
            continue
        concept_id = str(row.get("concept_id") or "")
        if concept_id.startswith("action.frame."):
            continue
        classes.add(validate_action_match_contract(row))
    classes.discard("capability_use")
    return tuple(sorted(classes))


def _match_ref_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(ref["start"]),
        int(ref["end"]),
        str(ref["lex_id"]),
        str(ref["concept_id"]),
        str(ref["entry_fingerprint"]),
    )


def _canonical_match_refs(values: Iterable[object]) -> list[dict[str, Any]]:
    refs = [_validate_match_ref(value) for value in values]
    ordered = sorted(refs, key=_match_ref_key)
    if len({_match_ref_key(ref) for ref in ordered}) != len(ordered):
        raise SemanticBindingError("semantic binding repeats an evidence reference")
    return ordered


def _derive_dispositions(
    constraints: Iterable[Mapping[str, Any]], integrity: str,
) -> tuple[str, str, list[str]]:
    rows = list(constraints)
    reasons = sorted(str(row["constraint_id"]) for row in rows)
    if integrity == "conflict":
        return "unresolved", "invalid_scope_conflict", reasons
    if any(str(row["value"]) in _HOLD_CONSTRAINT_VALUES for row in rows):
        return "unresolved", "hold_unresolved", reasons
    values = {(str(row["dimension"]), str(row["value"])) for row in rows}
    if values & _RECOGNITION_ONLY_CONSTRAINTS:
        if any(dimension == "polarity" and value == "negative" for dimension, value in values):
            request = "denied"
        elif any(dimension == "modality" and value == "hypothetical"
                 for dimension, value in values):
            request = "conditional"
        elif any(dimension == "assertion_context" for dimension, _value in values):
            request = "attributed"
        else:
            request = "described"
        return request, "recognition_only", reasons
    return "direct_player_request", "candidate", reasons


def build_meaning_binding(
    meaning: CompiledMeaning,
    *,
    binding_id: str,
    event_node_id: str,
    event_span: tuple[int, int],
    scope_nodes: Iterable[Mapping[str, Any]] = (),
    constraints: Iterable[Mapping[str, Any]] = (),
    constraint_integrity: str = "valid",
    field_provenance: Iterable[Mapping[str, Any]] = (),
    role_evidence: Iterable[Mapping[str, Any]] = (),
    coordination_edges: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Freeze one event-local projection of an already compiled recognition receipt."""
    start, end = event_span
    if isinstance(start, bool) or isinstance(end, bool) \
            or not isinstance(start, int) or not isinstance(end, int) \
            or start < 0 or end <= start:
        raise SemanticBindingError("meaning binding event span is invalid")
    clean_constraints = [_clone(row) for row in constraints]
    request, mechanic, reasons = _derive_dispositions(
        clean_constraints, constraint_integrity,
    )
    payload = {
        "schema": MEANING_BINDING_SCHEMA,
        "binding_id": _stable_id(binding_id, "meaning binding id"),
        "meaning_ref": meaning.receipt_dict()["fingerprint"],
        "source_fingerprint": meaning.source_fingerprint,
        "event_node_id": _stable_id(event_node_id, "semantic event node id"),
        "event_span": [start, end],
        "scope_nodes": [_clone(row) for row in scope_nodes],
        "constraints": clean_constraints,
        "constraint_integrity": constraint_integrity,
        "request_disposition": request,
        "mechanic_disposition": mechanic,
        "reason_refs": reasons,
        "field_provenance": [_clone(row) for row in field_provenance],
        "role_evidence": [_clone(row) for row in role_evidence],
        "coordination_edges": [_clone(row) for row in coordination_edges],
        "authority": "recognition_only",
    }
    return validate_meaning_binding({**payload, "fingerprint": content_fingerprint(payload)})


def _receipt_match_keys(meaning_receipt: Mapping[str, Any]) -> set[tuple[Any, ...]]:
    matches = meaning_receipt.get("matches")
    if not isinstance(matches, list):
        raise SemanticBindingError("meaning receipt has no match evidence")
    return {_match_ref_key(semantic_match_ref(match)) for match in matches}


def validate_meaning_binding(
    value: object,
    *,
    meaning_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate a content-free binding and optionally its exact meaning receipt."""
    fields = {
        "schema",
        "binding_id",
        "meaning_ref",
        "source_fingerprint",
        "event_node_id",
        "event_span",
        "scope_nodes",
        "constraints",
        "constraint_integrity",
        "request_disposition",
        "mechanic_disposition",
        "reason_refs",
        "field_provenance",
        "role_evidence",
        "coordination_edges",
        "authority",
        "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SemanticBindingError("meaning binding fields do not match v1")
    if value.get("schema") != MEANING_BINDING_SCHEMA \
            or value.get("authority") != "recognition_only":
        raise SemanticBindingError("meaning binding schema or authority is invalid")
    _stable_id(value.get("binding_id"), "meaning binding id")
    _stable_id(value.get("event_node_id"), "semantic event node id")
    _fingerprint(value.get("meaning_ref"), "meaning binding receipt reference")
    _fingerprint(value.get("source_fingerprint"), "meaning binding source fingerprint")
    _fingerprint(value.get("fingerprint"), "meaning binding fingerprint")
    span = value.get("event_span")
    if not isinstance(span, list) or len(span) != 2 \
            or any(isinstance(item, bool) or not isinstance(item, int) for item in span) \
            or span[0] < 0 or span[1] <= span[0]:
        raise SemanticBindingError("meaning binding event span is invalid")
    if value.get("constraint_integrity") not in _CONSTRAINT_INTEGRITY \
            or value.get("request_disposition") not in _REQUEST_DISPOSITIONS \
            or value.get("mechanic_disposition") not in _MECHANIC_DISPOSITIONS:
        raise SemanticBindingError("meaning binding disposition is invalid")

    all_refs: list[dict[str, Any]] = []
    scopes = value.get("scope_nodes")
    if not isinstance(scopes, list):
        raise SemanticBindingError("meaning binding scope_nodes must be a list")
    scope_ids: set[str] = set()
    for row in scopes:
        if not isinstance(row, dict) or set(row) != _SCOPE_NODE_FIELDS:
            raise SemanticBindingError("meaning binding scope node fields do not match v1")
        scope_ref = _stable_id(row.get("scope_ref"), "semantic scope reference")
        if scope_ref in scope_ids:
            raise SemanticBindingError("meaning binding repeats a scope reference")
        scope_ids.add(scope_ref)
        _stable_id(row.get("kind"), "semantic scope kind")
        if row.get("construction_role") not in _CONSTRUCTION_ROLES:
            raise SemanticBindingError("semantic scope construction role is invalid")
        parent = row.get("parent_scope_ref")
        if parent is not None:
            _stable_id(parent, "semantic parent scope reference")
        starts = (row.get("span_start"), row.get("content_start"))
        ends = (row.get("span_end"), row.get("content_end"))
        if any(isinstance(item, bool) or not isinstance(item, int) for item in (*starts, *ends)) \
                or row["span_start"] < 0 or row["span_end"] <= row["span_start"] \
                or row["content_start"] < row["span_start"] \
                or row["content_end"] > row["span_end"] \
                or row["content_end"] <= row["content_start"]:
            raise SemanticBindingError("semantic scope spans are invalid")
        refs = _canonical_match_refs(row.get("evidence_refs") or ())
        if refs != row.get("evidence_refs"):
            raise SemanticBindingError("semantic scope evidence is not canonical")
        all_refs.extend(refs)
    if any(row.get("parent_scope_ref") not in scope_ids
           for row in scopes if row.get("parent_scope_ref") is not None):
        raise SemanticBindingError("semantic scope parent is not present")

    constraints = value.get("constraints")
    if not isinstance(constraints, list):
        raise SemanticBindingError("meaning binding constraints must be a list")
    constraint_ids: set[str] = set()
    for row in constraints:
        if not isinstance(row, dict) or set(row) != _CONSTRAINT_FIELDS:
            raise SemanticBindingError("semantic constraint fields do not match v1")
        constraint_id = _stable_id(row.get("constraint_id"), "semantic constraint id")
        if constraint_id in constraint_ids:
            raise SemanticBindingError("meaning binding repeats a constraint id")
        constraint_ids.add(constraint_id)
        if row.get("scope_ref") not in scope_ids:
            raise SemanticBindingError("semantic constraint cites an unknown scope")
        if row.get("target_event_ref") != value["event_node_id"]:
            raise SemanticBindingError("semantic constraint targets a different event")
        _stable_id(row.get("dimension"), "semantic constraint dimension")
        _stable_id(row.get("value"), "semantic constraint value")
        refs = _canonical_match_refs(row.get("evidence_refs") or ())
        if refs != row.get("evidence_refs"):
            raise SemanticBindingError("semantic constraint evidence is not canonical")
        all_refs.extend(refs)

    provenance = value.get("field_provenance")
    if not isinstance(provenance, list):
        raise SemanticBindingError("meaning binding field provenance must be a list")
    seen_fields: set[str] = set()
    for row in provenance:
        if not isinstance(row, dict) or set(row) != _FIELD_PROVENANCE_FIELDS:
            raise SemanticBindingError("semantic field provenance fields do not match v1")
        field = _stable_id(row.get("field"), "semantic provenance field")
        if field in seen_fields or not isinstance(row.get("value"), str) \
                or not row["value"] or not isinstance(row.get("defaulted"), bool):
            raise SemanticBindingError("semantic field provenance is invalid")
        seen_fields.add(field)
        refs = _canonical_match_refs(row.get("evidence_refs") or ())
        if refs != row.get("evidence_refs") or (not refs and not row["defaulted"]):
            raise SemanticBindingError("semantic field provenance evidence is invalid")
        all_refs.extend(refs)

    roles = value.get("role_evidence")
    if not isinstance(roles, list):
        raise SemanticBindingError("meaning binding role evidence must be a list")
    seen_roles: set[str] = set()
    for row in roles:
        if not isinstance(row, dict) or set(row) != _ROLE_EVIDENCE_FIELDS:
            raise SemanticBindingError("semantic role evidence fields do not match v1")
        role = _stable_id(row.get("role"), "semantic evidence role")
        if role in seen_roles:
            raise SemanticBindingError("meaning binding repeats a role evidence field")
        seen_roles.add(role)
        refs = _canonical_match_refs(row.get("evidence_refs") or ())
        if refs != row.get("evidence_refs") or not refs:
            raise SemanticBindingError("semantic role evidence must cite recognition")
        all_refs.extend(refs)

    edges = value.get("coordination_edges")
    if not isinstance(edges, list):
        raise SemanticBindingError("meaning binding coordination edges must be a list")
    for row in edges:
        if not isinstance(row, dict) or set(row) != _COORDINATION_EDGE_FIELDS \
                or row.get("relation") not in _COORDINATION_RELATIONS:
            raise SemanticBindingError("semantic coordination edge is invalid")
        _stable_id(row.get("from_event_ref"), "coordination source event")
        _stable_id(row.get("to_event_ref"), "coordination target event")
        role = row.get("role")
        if role is not None:
            _stable_id(role, "coordination shared role")
        refs = _canonical_match_refs(row.get("evidence_refs") or ())
        if refs != row.get("evidence_refs"):
            raise SemanticBindingError("semantic coordination evidence is not canonical")
        all_refs.extend(refs)

    request, mechanic, reasons = _derive_dispositions(
        constraints, str(value["constraint_integrity"]),
    )
    if value.get("request_disposition") != request \
            or value.get("mechanic_disposition") != mechanic \
            or value.get("reason_refs") != reasons:
        raise SemanticBindingError("meaning binding disposition was not derived from constraints")

    if meaning_receipt is not None:
        if value["meaning_ref"] != meaning_receipt.get("fingerprint") \
                or value["source_fingerprint"] != meaning_receipt.get("source_fingerprint"):
            raise SemanticBindingError("meaning binding belongs to a different receipt")
        known = _receipt_match_keys(meaning_receipt)
        unknown = sorted({_match_ref_key(ref) for ref in all_refs} - known)
        if unknown:
            raise SemanticBindingError("meaning binding cites recognition absent from its receipt")

    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value["fingerprint"] != content_fingerprint(payload):
        raise SemanticBindingError("meaning binding fingerprint mismatch")
    return _clone(value)


def world_alignment_snapshot(state: Mapping[str, Any]) -> str:
    """Fingerprint only the state fields authoritative for identity, items, and time."""
    item_model_present = "items" in state and isinstance(state.get("items"), dict)
    items = state.get("items") if item_model_present else None
    projection = {
        "schema": WORLD_SNAPSHOT_SCHEMA,
        "world_identity": _clone(state.get("world_identity") or {}),
        "entities": _clone(state.get("entities") or {}),
        "item_model_present": item_model_present,
        "items": _clone(items),
        "clock": _clone(state.get("clock") or {}),
        "state_turn": int(((state.get("meta") or {}).get("turn", -1))),
    }
    return content_fingerprint(projection)


def build_possessed_object_alignment(
    state: Mapping[str, Any],
    *,
    recognition_ref: str,
    object_name: str,
    linguistic_possessor_id: str | None,
    time_scope: str = "current",
    cardinality: str = "one",
    selection: str = "exact",
) -> dict[str, Any]:
    """Align a grammatical possessive without treating grammar as ledger ownership."""
    _fingerprint(recognition_ref, "world alignment recognition reference")
    normalized = normalize_phrase(object_name)
    value_ref = content_fingerprint({"role": "possessed_object", "value": normalized})
    model = state.get("items") if "items" in state else None
    candidates: list[str] = []
    resolved: list[str] = []
    positive: str | None = None
    status = "uncheckable"
    # The current ledger cannot prove a historical or anticipated ownership predicate.  Until
    # versioned world snapshots exist for those times, preserve the recognition but grant no
    # item identity or owner authority.
    if time_scope != "current":
        model = None
    if isinstance(model, dict):
        candidates = sorted(
            str(instance_id)
            for instance_id, item in model.items()
            if isinstance(item, dict)
            and normalize_phrase(item.get("name", "")) == normalized
        )
        if not candidates:
            status = "false"
        elif cardinality == "one" and len(candidates) != 1:
            status = "unresolved"
        else:
            resolved = candidates if cardinality in {"all", "set"} else candidates[:1]
            owners = {
                str((model.get(instance_id) or {}).get("owner") or "")
                for instance_id in resolved
            }
            if linguistic_possessor_id and owners == {linguistic_possessor_id}:
                status = "positive"
                positive = linguistic_possessor_id
            else:
                status = "false"
    payload = {
        "schema": WORLD_ALIGNMENT_SCHEMA,
        "role": "possessed_object",
        "predicate_id": "item.owner_equals_linguistic_possessor",
        "recognition_ref": recognition_ref,
        "recognized_value_ref": value_ref,
        "world_snapshot_ref": world_alignment_snapshot(state),
        "time_scope": time_scope,
        "cardinality": cardinality,
        "selection": selection,
        "status": status,
        "candidate_ids": candidates if status == "unresolved" else [],
        "resolved_ids": resolved,
        "positive_authority_value": positive,
    }
    return validate_world_alignment({**payload, "fingerprint": content_fingerprint(payload)})


def validate_world_alignment(value: object) -> dict[str, Any]:
    fields = {
        "schema",
        "role",
        "predicate_id",
        "recognition_ref",
        "recognized_value_ref",
        "world_snapshot_ref",
        "time_scope",
        "cardinality",
        "selection",
        "status",
        "candidate_ids",
        "resolved_ids",
        "positive_authority_value",
        "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields \
            or value.get("schema") != WORLD_ALIGNMENT_SCHEMA:
        raise SemanticBindingError("world alignment fields do not match v1")
    for key in ("role", "predicate_id", "time_scope", "cardinality", "selection"):
        _stable_id(value.get(key), f"world alignment {key}")
    for key in (
        "recognition_ref",
        "recognized_value_ref",
        "world_snapshot_ref",
        "fingerprint",
    ):
        _fingerprint(value.get(key), f"world alignment {key}")
    status = value.get("status")
    if status not in {"positive", "false", "unresolved", "uncheckable"}:
        raise SemanticBindingError("world alignment status is invalid")
    candidates, resolved = value.get("candidate_ids"), value.get("resolved_ids")
    for label, rows in (("candidate", candidates), ("resolved", resolved)):
        if not isinstance(rows, list) or any(not isinstance(item, str) or not item for item in rows) \
                or rows != sorted(set(rows)):
            raise SemanticBindingError(f"world alignment {label} IDs are invalid")
    positive = value.get("positive_authority_value")
    if positive is not None and (not isinstance(positive, str) or not positive):
        raise SemanticBindingError("world alignment positive authority is invalid")
    if status == "positive" and (not resolved or positive is None):
        raise SemanticBindingError("positive world alignment needs resolved IDs and authority")
    if status != "positive" and positive is not None:
        raise SemanticBindingError("non-positive world alignment cannot grant authority")
    if status == "unresolved" and (len(candidates) < 2 or resolved):
        raise SemanticBindingError("unresolved world alignment must preserve candidates")
    if status in {"uncheckable", "unresolved"} and resolved:
        raise SemanticBindingError("uncheckable or unresolved alignment cannot resolve an item")
    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value["fingerprint"] != content_fingerprint(payload):
        raise SemanticBindingError("world alignment fingerprint mismatch")
    return _clone(value)
