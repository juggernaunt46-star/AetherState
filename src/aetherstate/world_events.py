"""Immutable admitted World Event Records and deterministic branch-scoped overlays.

Version 1 is retained as a replay format.  Fresh records use version 2, whose
effect adapters, subjects, selectors, causes, and application receipts are
closed, typed, and independently fingerprinted.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .capability_glossary import content_fingerprint


LEGACY_WORLD_EVENT_SCHEMA = "aetherstate-world-event-record/1"
WORLD_EVENT_SCHEMA = "aetherstate-world-event-record/2"
LEGACY_WORLD_OVERLAY_SCHEMA = "aetherstate-world-overlay/1"
WORLD_OVERLAY_SCHEMA = "aetherstate-world-overlay/2"
OVERLAY_ADAPTER_SCHEMA = "aetherstate-world-overlay-adapter/2"
OVERLAY_RECEIPT_SCHEMA = "aetherstate-world-overlay-receipt/1"
SUBJECT_REF_SCHEMA = "aetherstate-world-subject-ref/1"
SELECTOR_SCHEMA = "aetherstate-world-subject-selector/2"
LEGACY_SELECTOR_SCHEMA = "aetherstate-world-subject-selector/1"
CAUSE_REF_SCHEMA = "aetherstate-world-event-cause-ref/1"
WORLDLEX_LINEAGE_SCHEMA = "aetherstate-worldlex-event-lineage/1"

EVENT_KINDS = {"admission", "expiry", "reversal", "supersession"}
CAUSE_AUTHORITIES = {
    "creator", "genesis", "rule", "mechanic_settlement", "semantic_transition_truth",
}
DOMAINS = {
    "world", "location", "actor", "npc_knowledge", "npc_behavior", "enemy_eligibility",
    "capability_eligibility", "quest", "faction", "relationship", "reputation", "retrieval",
    "briefing", "narration", "console", "hud",
}
SUBJECT_KINDS = {
    "world", "location", "actor", "npc", "enemy", "capability", "quest", "faction",
    "relationship", "reputation", "retrieval", "briefing", "narration", "console", "hud",
    "selector",
}
SELECTOR_SUBJECT_KINDS = {
    "world", "location", "actor", "npc", "enemy", "capability", "quest", "faction",
    "relationship", "reputation",
}
ACTOR_KINDS = {"actor", "npc", "enemy", "faction", "world"}
SCOPES = {"branch"}
PROPAGATIONS = {"existing_subjects", "future_subjects", "existing_and_future"}
ACTIVATIONS = {"active", "inactive", "scheduled"}
CAUSE_VISIBILITIES = {"public", "player", "actor_scoped", "hidden"}

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_WORLD_RE = re.compile(r"world_[0-9a-f]{32}\Z")
_FRAME_REF_RE = re.compile(r"(?:frame[.:])?[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TRANSITION_REF_RE = re.compile(r"transition\.[0-9a-f]{32}\Z")
_SELECTOR_FIELDS = {
    "id", "kind", "faction", "name", "tier", "role", "location", "capability_id",
    "definition_id", "tags",
}
_SELECTOR_OPERATORS = {"eq", "not_eq", "in", "contains"}

# adapter id -> domain, field, value kind, permitted subject kinds
_ADAPTER_SPECS: dict[str, tuple[str, str, str, frozenset[str]]] = {
    "world.circumstance/1": ("world", "circumstance", "text", frozenset({"world"})),
    "location.circumstance/1": (
        "location", "circumstance", "text", frozenset({"location"}),
    ),
    "actor.condition/1": (
        "actor", "condition", "text", frozenset({"actor", "npc", "enemy", "selector"}),
    ),
    "npc.knowledge/1": (
        "npc_knowledge", "knowledge", "text", frozenset({"npc", "actor", "selector"}),
    ),
    "npc.behavior/1": (
        "npc_behavior", "behavior", "text", frozenset({"npc", "actor", "selector"}),
    ),
    "faction.circumstance/1": (
        "faction", "circumstance", "text", frozenset({"faction"}),
    ),
    "spawn.eligibility/1": (
        "enemy_eligibility", "eligible", "bool", frozenset({"enemy", "selector"}),
    ),
    "capability.eligibility/1": (
        "capability_eligibility", "eligible", "bool", frozenset({"capability", "selector"}),
    ),
    "quest.availability/1": ("quest", "available", "bool", frozenset({"quest"})),
    "relationship.modifier/1": (
        "relationship", "modifier", "modifier", frozenset({"relationship"}),
    ),
    "reputation.modifier/1": (
        "reputation", "modifier", "modifier", frozenset({"reputation", "faction", "actor"}),
    ),
    "retrieval.context/1": (
        "retrieval", "context", "text", frozenset({"retrieval", "world", "location", "actor"}),
    ),
    "briefing.context/1": (
        "briefing", "context", "text", frozenset({"briefing", "world", "location", "actor"}),
    ),
    "narration.context/1": (
        "narration", "context", "text", frozenset({"narration", "world", "location", "actor"}),
    ),
    "console.visibility/1": ("console", "visible", "bool", frozenset({"console"})),
    "hud.visibility/1": ("hud", "visible", "bool", frozenset({"hud"})),
}
_LEGACY_ADAPTER_IDS = {
    "world.circumstance/1", "location.circumstance/1", "actor.condition/1",
    "faction.circumstance/1", "spawn.eligibility/1", "capability.eligibility/1",
    "quest.availability/1", "relationship.modifier/1", "reputation.modifier/1",
}
_LEGACY_ADAPTER_FIELDS = {
    adapter_id: {"domain": _ADAPTER_SPECS[adapter_id][0], "fields": {_ADAPTER_SPECS[adapter_id][1]}}
    for adapter_id in _LEGACY_ADAPTER_IDS
}

_V2_EVENT_FIELDS = {
    "schema", "schema_version", "event_id", "world_id", "session_id", "branch_id", "turn",
    "game_time", "kind", "relation_target", "actor", "cause_id", "cause_authority",
    "cause_ref", "cause_visibility", "semantic_frame_ref", "settlement_ref",
    "worldlex_lineage", "subjects", "future_selector", "affected_domains", "priority", "scope",
    "propagation", "start", "duration", "reversible", "activation", "effects", "description",
    "fingerprint",
}


def _fingerprinted(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(payload))
    row["fingerprint"] = content_fingerprint(row)
    return row


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields are invalid")


def _id(value: object, label: str) -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"{label} is invalid")
    return text


def _canonical_list(value: object, label: str, *, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} is invalid")
    rows = sorted(set(value))
    if rows != value or (allowed is not None and set(rows) - allowed):
        raise ValueError(f"{label} is invalid")
    return rows


def _legacy_list(value: object, label: str, *, allowed: set[str] | None = None) -> list[str]:
    """Retain v1's uniqueness/type check without retroactively requiring sort order."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    rows = sorted({str(item) for item in value if isinstance(item, str) and item})
    if len(rows) != len(value) or (allowed is not None and set(rows) - allowed):
        raise ValueError(f"{label} is invalid")
    return rows


def _validate_fingerprint(value: Mapping[str, Any], label: str) -> None:
    payload = {key: deepcopy(item) for key, item in value.items() if key != "fingerprint"}
    if value.get("fingerprint") != content_fingerprint(payload):
        raise ValueError(f"{label} fingerprint mismatch")


def _subject_key(subject: Mapping[str, Any]) -> str:
    return "world" if subject["kind"] == "world" else f"{subject['kind']}:{subject['id']}"


def _subject_from_legacy(value: object, *, domain: str, world_id: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _validate_subject_ref(value, world_id=world_id)
    text = _id(value, "effect subject")
    if text == "world":
        kind, subject_id = "world", world_id
    elif ":" in text:
        prefix, subject_id = text.split(":", 1)
        aliases = {"kind": "selector"}
        kind = aliases.get(prefix, prefix)
    else:
        defaults = {
            "world": "world", "location": "location", "actor": "actor",
            "npc_knowledge": "npc", "npc_behavior": "npc", "enemy_eligibility": "selector",
            "capability_eligibility": "selector", "quest": "quest", "faction": "faction",
            "relationship": "relationship", "reputation": "reputation", "retrieval": "retrieval",
            "briefing": "briefing", "narration": "narration", "console": "console", "hud": "hud",
        }
        kind, subject_id = defaults[domain], text
    if kind == "world":
        subject_id = world_id
    payload = {"schema": SUBJECT_REF_SCHEMA, "kind": kind, "id": subject_id}
    return _validate_subject_ref(_fingerprinted(payload), world_id=world_id)


def _validate_subject_ref(value: object, *, world_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("subject reference must be an object")
    row = deepcopy(dict(value))
    _exact_fields(row, {"schema", "kind", "id", "fingerprint"}, "subject reference")
    if row["schema"] != SUBJECT_REF_SCHEMA or row["kind"] not in SUBJECT_KINDS:
        raise ValueError("subject reference schema or kind is invalid")
    _id(row["id"], "subject reference id")
    if row["kind"] == "world" and row["id"] != world_id:
        raise ValueError("world subject reference names another world")
    _validate_fingerprint(row, "subject reference")
    return row


def _validate_cause_ref(value: object, *, cause_id: str, authority: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record cause_ref must be an object")
    row = deepcopy(dict(value))
    _exact_fields(row, {"schema", "authority", "id", "fingerprint"}, "event cause reference")
    if row["schema"] != CAUSE_REF_SCHEMA or row["authority"] != authority or row["id"] != cause_id:
        raise ValueError("World Event Record cause reference does not match its cause")
    _validate_fingerprint(row, "event cause reference")
    return row


def _validate_worldlex_lineage(value: object, *, world_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record WorldLex lineage must be an object")
    row = deepcopy(dict(value))
    fields = {
        "schema", "world_id", "definition_ref", "assignment_ref", "eligibility_ref",
        "adapter_ref", "receipt_ref", "fingerprint",
    }
    _exact_fields(row, fields, "WorldLex event lineage")
    if row["schema"] != WORLDLEX_LINEAGE_SCHEMA or row["world_id"] != world_id:
        raise ValueError("WorldLex event lineage belongs to another world")
    refs = [
        row["definition_ref"], row["assignment_ref"], row["eligibility_ref"],
        row["adapter_ref"], row["receipt_ref"],
    ]
    if all(ref is None for ref in refs):
        raise ValueError("WorldLex event lineage must contain an exact reference")
    for ref in refs:
        if ref is not None:
            _id(ref, "WorldLex event lineage reference")
    _validate_fingerprint(row, "WorldLex event lineage")
    return row


def _validate_scalar(value: object, label: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{label} must be a JSON scalar")


def _selector_predicates_from_legacy(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": str(field), "operator": "eq", "value": deepcopy(expected)}
        for field, expected in sorted(value.items())
    ]


def _upgrade_selector(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record future selector is malformed")
    if value.get("schema") == SELECTOR_SCHEMA:
        return _validate_selector(value)
    if value.get("schema") != LEGACY_SELECTOR_SCHEMA:
        raise ValueError("World Event Record future selector schema is unsupported")
    _validate_legacy_selector(value)
    payload = {
        "schema": SELECTOR_SCHEMA,
        "subject_kinds": sorted(value["subject_kinds"]),
        "predicates": _selector_predicates_from_legacy(value["predicates"]),
    }
    return _validate_selector(_fingerprinted(payload))


def _validate_legacy_selector(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record future selector is malformed")
    row = deepcopy(dict(value))
    _exact_fields(row, {"schema", "subject_kinds", "predicates", "fingerprint"}, "future selector")
    if row["schema"] != LEGACY_SELECTOR_SCHEMA:
        raise ValueError("World Event Record future selector schema is unsupported")
    _validate_fingerprint(row, "World Event Record future selector")
    return row


def _validate_selector(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record future selector is malformed")
    row = deepcopy(dict(value))
    _exact_fields(row, {"schema", "subject_kinds", "predicates", "fingerprint"}, "future selector")
    if row["schema"] != SELECTOR_SCHEMA:
        raise ValueError("World Event Record future selector schema is unsupported")
    kinds = _canonical_list(row["subject_kinds"], "future selector subject_kinds")
    if not kinds or set(kinds) - SELECTOR_SUBJECT_KINDS:
        raise ValueError("World Event Record future selector needs a subject kind")
    predicates = row["predicates"]
    if not isinstance(predicates, list):
        raise ValueError("World Event Record future selector predicates must be a list")
    prior: tuple[str, str] | None = None
    for predicate in predicates:
        if not isinstance(predicate, dict) or set(predicate) != {"field", "operator", "value"}:
            raise ValueError("World Event Record future selector predicate is malformed")
        field, operator = predicate["field"], predicate["operator"]
        if field not in _SELECTOR_FIELDS or operator not in _SELECTOR_OPERATORS:
            raise ValueError("World Event Record future selector predicate is unsupported")
        current = (field, operator)
        if prior is not None and current <= prior:
            raise ValueError("World Event Record future selector predicates are not canonical")
        prior = current
        if operator in {"in", "contains"}:
            if not isinstance(predicate["value"], list) or not predicate["value"]:
                raise ValueError("selector membership predicate needs a non-empty list")
            for item in predicate["value"]:
                _validate_scalar(item, "selector predicate value")
        else:
            _validate_scalar(predicate["value"], "selector predicate value")
    _validate_fingerprint(row, "World Event Record future selector")
    return row


def _adapter_parts(adapter_id: str) -> tuple[str, int]:
    name, separator, raw_version = adapter_id.rpartition("/")
    if not separator or not raw_version.isdigit() or int(raw_version) <= 0:
        raise ValueError("overlay adapter identity is invalid")
    return name, int(raw_version)


def _adapter_envelope(adapter_id: str) -> dict[str, Any]:
    spec = _ADAPTER_SPECS.get(adapter_id)
    if spec is None:
        raise ValueError("World Event Record uses an unsupported typed adapter")
    name, version = _adapter_parts(adapter_id)
    payload = {
        "schema": OVERLAY_ADAPTER_SCHEMA,
        "adapter_id": name,
        "version": version,
        "domain": spec[0],
        "field": spec[1],
        "value_type": spec[2],
    }
    return _fingerprinted(payload)


def _adapter_identity(value: object) -> str:
    if isinstance(value, str):
        if value not in _ADAPTER_SPECS:
            raise ValueError("World Event Record uses an unsupported typed adapter")
        return value
    adapter = _validate_adapter(value)
    return f"{adapter['adapter_id']}/{adapter['version']}"


def _validate_adapter(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record adapter envelope must be an object")
    row = deepcopy(dict(value))
    fields = {"schema", "adapter_id", "version", "domain", "field", "value_type", "fingerprint"}
    _exact_fields(row, fields, "overlay adapter")
    if row["schema"] != OVERLAY_ADAPTER_SCHEMA or isinstance(row["version"], bool) \
            or not isinstance(row["version"], int) or row["version"] <= 0:
        raise ValueError("overlay adapter schema or version is invalid")
    adapter_id = f"{row['adapter_id']}/{row['version']}"
    spec = _ADAPTER_SPECS.get(adapter_id)
    if spec is None or (row["domain"], row["field"], row["value_type"]) != spec[:3]:
        raise ValueError("overlay adapter contract is unsupported")
    _validate_fingerprint(row, "overlay adapter")
    return row


def _validate_effect_value(value: object, kind: str, *, supported: bool) -> None:
    if not supported:
        if value is not None:
            raise ValueError("lore-only effects cannot alter the effective overlay")
        return
    if kind == "bool" and not isinstance(value, bool):
        raise ValueError("overlay effect requires a boolean value")
    if kind == "text" and (not isinstance(value, str) or not value.strip() or len(value) > 2000):
        raise ValueError("overlay effect requires bounded non-empty text")
    if kind == "modifier" and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not -100 <= value <= 100
    ):
        raise ValueError("overlay modifier must be a number from -100 through 100")


def _receipt_for(
    event_id: str, adapter: Mapping[str, Any], subject: Mapping[str, Any], *, domain: str,
    field: str, value: object, supported: bool, lore: str,
) -> dict[str, Any]:
    application_fingerprint = content_fingerprint({
        "event_id": event_id,
        "adapter_fingerprint": adapter["fingerprint"],
        "subject_fingerprint": subject["fingerprint"],
        "domain": domain,
        "field": field,
        "value": deepcopy(value),
        "supported": supported,
        "lore": lore,
    })
    payload = {
        "schema": OVERLAY_RECEIPT_SCHEMA,
        "receipt_id": f"overlay-receipt:{application_fingerprint.removeprefix('sha256:')[:24]}",
        "event_id": event_id,
        "adapter_fingerprint": adapter["fingerprint"],
        "subject_fingerprint": subject["fingerprint"],
        "application_fingerprint": application_fingerprint,
        "field": field,
    }
    return _fingerprinted(payload)


def _validate_receipt(value: object, *, event_id: str, adapter: Mapping[str, Any],
                      subject: Mapping[str, Any], domain: str, field: str, effect_value: object,
                      supported: bool, lore: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record adapter receipt must be an object")
    row = deepcopy(dict(value))
    fields = {
        "schema", "receipt_id", "event_id", "adapter_fingerprint", "subject_fingerprint",
        "application_fingerprint", "field", "fingerprint",
    }
    _exact_fields(row, fields, "overlay adapter receipt")
    if row["schema"] != OVERLAY_RECEIPT_SCHEMA:
        raise ValueError("overlay adapter receipt schema is invalid")
    _id(row["receipt_id"], "overlay adapter receipt id")
    if row["event_id"] != event_id or row["adapter_fingerprint"] != adapter["fingerprint"] \
            or row["subject_fingerprint"] != subject["fingerprint"] or row["field"] != field:
        raise ValueError("overlay adapter receipt does not bind its exact effect")
    _validate_fingerprint(row, "overlay adapter receipt")
    if row != _receipt_for(
        event_id, adapter, subject, domain=domain, field=field, value=effect_value,
        supported=supported, lore=lore,
    ):
        raise ValueError("overlay adapter receipt identity is not deterministic")
    return row


def _upgrade_effect(effect: object, *, event_id: str, world_id: str) -> dict[str, Any]:
    if not isinstance(effect, Mapping):
        raise ValueError("World Event Record effect is malformed")
    if set(effect) == {
        "adapter", "domain", "subject", "field", "value", "supported", "lore", "receipt",
    } and isinstance(effect.get("adapter"), Mapping):
        return _validate_v2_effect(effect, event_id=event_id, world_id=world_id)
    if set(effect) != {"adapter", "domain", "subject", "field", "value", "supported", "lore"}:
        raise ValueError("World Event Record effect is malformed")
    adapter_id = _adapter_identity(effect["adapter"])
    spec = _ADAPTER_SPECS[adapter_id]
    adapter = _adapter_envelope(adapter_id)
    subject = _subject_from_legacy(effect["subject"], domain=spec[0], world_id=world_id)
    upgraded = {
        "adapter": adapter,
        "domain": effect["domain"],
        "subject": subject,
        "field": effect["field"],
        "value": deepcopy(effect["value"]),
        "supported": effect["supported"],
        "lore": effect["lore"],
        "receipt": _receipt_for(
            event_id, adapter, subject, domain=str(effect["domain"]), field=str(effect["field"]),
            value=effect["value"], supported=bool(effect["supported"]), lore=str(effect["lore"]),
        ),
    }
    return _validate_v2_effect(upgraded, event_id=event_id, world_id=world_id)


def _validate_v2_effect(value: object, *, event_id: str, world_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record effect is malformed")
    row = deepcopy(dict(value))
    _exact_fields(
        row,
        {"adapter", "domain", "subject", "field", "value", "supported", "lore", "receipt"},
        "World Event Record effect",
    )
    adapter = _validate_adapter(row["adapter"])
    adapter_id = f"{adapter['adapter_id']}/{adapter['version']}"
    spec = _ADAPTER_SPECS[adapter_id]
    if row["domain"] != spec[0] or row["field"] != spec[1]:
        raise ValueError("World Event Record effect differs from its adapter contract")
    subject = _validate_subject_ref(row["subject"], world_id=world_id)
    if subject["kind"] not in spec[3]:
        raise ValueError("World Event Record effect subject kind is unsupported by its adapter")
    if not isinstance(row["supported"], bool) or not isinstance(row["lore"], str) \
            or len(row["lore"]) > 4000:
        raise ValueError("World Event Record support boundary is malformed")
    _validate_effect_value(row["value"], spec[2], supported=row["supported"])
    receipt = _validate_receipt(
        row["receipt"], event_id=event_id, adapter=adapter, subject=subject, domain=row["domain"],
        field=row["field"], effect_value=row["value"], supported=row["supported"], lore=row["lore"],
    )
    row["adapter"], row["subject"], row["receipt"] = adapter, subject, receipt
    return row


def _validate_v1_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the historical shape without retroactively imposing v2 fields."""
    row = deepcopy(dict(value))
    fingerprint = row.pop("fingerprint", None)
    if fingerprint != content_fingerprint(row):
        raise ValueError("World Event Record fingerprint mismatch")
    _id(row.get("event_id"), "World Event Record event_id")
    if not _WORLD_RE.fullmatch(str(row.get("world_id") or "")):
        raise ValueError("World Event Record world_id is invalid")
    for key in ("session_id", "branch_id", "cause_id", "schema_version"):
        _id(row.get(key), f"World Event Record {key}")
    if row.get("kind") not in EVENT_KINDS or row.get("cause_authority") not in CAUSE_AUTHORITIES:
        raise ValueError("World Event Record kind or cause authority is invalid")
    for key in ("turn", "game_time"):
        if isinstance(row.get(key), bool) or not isinstance(row.get(key), int) or row[key] < 0:
            raise ValueError(f"World Event Record {key} is invalid")
    if isinstance(row.get("priority"), bool) or not isinstance(row.get("priority"), int):
        raise ValueError("World Event Record priority is invalid")
    _legacy_list(row.get("affected_domains"), "affected_domains", allowed=DOMAINS)
    effects = row.get("effects")
    if not isinstance(effects, list):
        raise ValueError("World Event Record effects must be a list")
    for effect in effects:
        if not isinstance(effect, dict) or set(effect) != {
            "adapter", "domain", "subject", "field", "value", "supported", "lore",
        }:
            raise ValueError("World Event Record effect is malformed")
        contract = _LEGACY_ADAPTER_FIELDS.get(str(effect.get("adapter")))
        if contract is None or effect.get("domain") != contract["domain"] \
                or effect.get("field") not in contract["fields"]:
            raise ValueError("World Event Record uses an unsupported typed adapter")
        if not isinstance(effect.get("supported"), bool) or not isinstance(effect.get("lore"), str):
            raise ValueError("World Event Record support boundary is malformed")
        if effect["supported"] is False and effect["value"] is not None:
            raise ValueError("lore-only effects cannot alter the effective overlay")
    target = row.get("relation_target")
    if row["kind"] == "admission" and target is not None:
        raise ValueError("admission cannot target another event")
    if row["kind"] != "admission":
        _id(target, "World Event Record relation target")
    duration = row.get("duration")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
    ):
        raise ValueError("World Event Record duration is invalid")
    if row.get("cause_visibility") not in CAUSE_VISIBILITIES:
        raise ValueError("World Event Record cause visibility is invalid")
    if row.get("future_selector") is not None:
        _validate_legacy_selector(row["future_selector"])
    return deepcopy(dict(value))


def validate_world_event_record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("World Event Record must be an object")
    if value.get("schema") == LEGACY_WORLD_EVENT_SCHEMA:
        return _validate_v1_record(value)
    if value.get("schema") != WORLD_EVENT_SCHEMA:
        raise ValueError("unsupported World Event Record schema")
    row = deepcopy(dict(value))
    _exact_fields(row, _V2_EVENT_FIELDS, "World Event Record")
    _validate_fingerprint(row, "World Event Record")
    if row["schema_version"] != "world-event-record-v2":
        raise ValueError("World Event Record schema version is invalid")
    event_id = _id(row["event_id"], "World Event Record event_id")
    if not _WORLD_RE.fullmatch(str(row["world_id"] or "")):
        raise ValueError("World Event Record world_id is invalid")
    world_id = str(row["world_id"])
    for key in ("session_id", "branch_id", "cause_id"):
        _id(row[key], f"World Event Record {key}")
    for key in ("turn", "game_time", "start"):
        if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
            raise ValueError(f"World Event Record {key} is invalid")
    if isinstance(row["priority"], bool) or not isinstance(row["priority"], int):
        raise ValueError("World Event Record priority is invalid")
    if row["kind"] not in EVENT_KINDS or row["cause_authority"] not in CAUSE_AUTHORITIES:
        raise ValueError("World Event Record kind or cause authority is invalid")
    _validate_cause_ref(row["cause_ref"], cause_id=row["cause_id"], authority=row["cause_authority"])
    if row["cause_visibility"] not in CAUSE_VISIBILITIES:
        raise ValueError("World Event Record cause visibility is invalid")
    if row["actor"] is not None:
        actor = _validate_subject_ref(row["actor"], world_id=world_id)
        if actor["kind"] not in ACTOR_KINDS:
            raise ValueError("World Event Record actor kind is invalid")
    if row["semantic_frame_ref"] is not None \
            and not _FRAME_REF_RE.fullmatch(str(row["semantic_frame_ref"])):
        raise ValueError("World Event Record semantic frame reference is invalid")
    if row["settlement_ref"] is not None:
        _id(row["settlement_ref"], "World Event Record settlement reference")
    authority = str(row["cause_authority"])
    if authority in {"creator", "genesis", "rule"}:
        if row["semantic_frame_ref"] is not None or row["settlement_ref"] is not None:
            raise ValueError("privileged World Event cause carries a second authority route")
    elif authority == "mechanic_settlement":
        if row["settlement_ref"] is None or row["semantic_frame_ref"] is None \
                or row["cause_id"] != row["settlement_ref"] \
                or _FINGERPRINT_RE.fullmatch(str(row["settlement_ref"])) is None \
                or _FINGERPRINT_RE.fullmatch(str(row["semantic_frame_ref"])) is None:
            raise ValueError("mechanic World Event cause lacks its exact settlement and frame")
    elif authority == "semantic_transition_truth":
        if row["settlement_ref"] is not None or row["semantic_frame_ref"] is None \
                or _TRANSITION_REF_RE.fullmatch(str(row["cause_id"])) is None \
                or _FINGERPRINT_RE.fullmatch(str(row["semantic_frame_ref"])) is None:
            raise ValueError("semantic World Event cause lacks one exact transition proof")
    _validate_worldlex_lineage(row["worldlex_lineage"], world_id=world_id)
    if not isinstance(row["subjects"], list):
        raise ValueError("World Event Record subjects must be a list")
    subjects = [_validate_subject_ref(subject, world_id=world_id) for subject in row["subjects"]]
    if [_subject_key(subject) for subject in subjects] != sorted({_subject_key(s) for s in subjects}):
        raise ValueError("World Event Record subjects are not canonical")
    selector = _validate_selector(row["future_selector"]) if row["future_selector"] is not None else None
    affected = _canonical_list(row["affected_domains"], "affected_domains", allowed=DOMAINS)
    if row["scope"] not in SCOPES or row["propagation"] not in PROPAGATIONS \
            or row["activation"] not in ACTIVATIONS:
        raise ValueError("World Event Record scope, propagation, or activation is invalid")
    duration = row["duration"]
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
    ):
        raise ValueError("World Event Record duration is invalid")
    if not isinstance(row["reversible"], bool) or not isinstance(row["description"], str) \
            or len(row["description"]) > 4000:
        raise ValueError("World Event Record reversible flag or description is invalid")
    if not isinstance(row["effects"], list):
        raise ValueError("World Event Record effects must be a list")
    effects = [
        _validate_v2_effect(effect, event_id=event_id, world_id=world_id)
        for effect in row["effects"]
    ]
    effect_cells = [
        (effect["domain"], _subject_key(effect["subject"]), effect["field"])
        for effect in effects
    ]
    if len(effect_cells) != len(set(effect_cells)):
        raise ValueError("World Event Record writes one overlay cell more than once")
    effect_domains = {effect["domain"] for effect in effects}
    if not effect_domains.issubset(set(affected)):
        raise ValueError("World Event Record effects are missing from affected_domains")
    if row["kind"] == "admission":
        if row["relation_target"] is not None:
            raise ValueError("admission cannot target another event")
        if row["propagation"] in {"future_subjects", "existing_and_future"} and selector is None:
            raise ValueError("future propagation requires a typed future selector")
    else:
        _id(row["relation_target"], "World Event Record relation target")
        if effects or subjects or selector is not None or affected or duration is not None:
            raise ValueError("terminal World Event Records cannot carry new effects or selectors")
        if row["propagation"] != "existing_subjects" or row["reversible"]:
            raise ValueError("terminal World Event Record lifecycle fields are invalid")
    return row


def _cause_ref(cause_id: str, authority: str) -> dict[str, Any]:
    return _fingerprinted({"schema": CAUSE_REF_SCHEMA, "authority": authority, "id": cause_id})


def _actor_ref(value: object, *, world_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _validate_subject_ref(value, world_id=world_id)
    payload = {"schema": SUBJECT_REF_SCHEMA, "kind": "actor", "id": _id(value, "event actor")}
    return _validate_subject_ref(_fingerprinted(payload), world_id=world_id)


def build_world_event_record(**fields: Any) -> dict[str, Any]:
    """Build a fresh v2 record, upgrading the former compact effect/selector inputs."""
    world_id = str(fields["world_id"])
    event_id = str(fields["event_id"])
    kind = fields.get("kind", "admission")
    cause_id = str(fields["cause_id"])
    authority = str(fields["cause_authority"])
    propagation = fields.get("propagation", "existing_subjects")
    selector = _upgrade_selector(fields.get("future_selector"))
    if selector is not None and "propagation" not in fields:
        propagation = "existing_and_future"
    effects = [
        _upgrade_effect(effect, event_id=event_id, world_id=world_id)
        for effect in deepcopy(fields.get("effects", []))
    ]
    subjects = [
        _subject_from_legacy(subject, domain="actor", world_id=world_id)
        for subject in deepcopy(fields.get("subjects", []))
    ]
    subjects.sort(key=_subject_key)
    actor = _actor_ref(fields.get("actor"), world_id=world_id)
    if actor is not None and not isinstance(fields.get("actor"), Mapping):
        actor_text = str(fields.get("actor"))
        actor = next(
            (
                deepcopy(subject) for subject in subjects
                if subject["kind"] in ACTOR_KINDS and subject["id"] == actor_text
            ),
            actor,
        )
    payload = {
        "schema": WORLD_EVENT_SCHEMA,
        "schema_version": "world-event-record-v2",
        "event_id": event_id,
        "world_id": world_id,
        "session_id": fields["session_id"],
        "branch_id": fields["branch_id"],
        "turn": int(fields["turn"]),
        "game_time": int(fields.get("game_time", 0)),
        "kind": kind,
        "relation_target": fields.get("relation_target"),
        "actor": actor,
        "cause_id": cause_id,
        "cause_authority": authority,
        "cause_ref": deepcopy(fields.get("cause_ref")) or _cause_ref(cause_id, authority),
        "cause_visibility": fields.get("cause_visibility", "public"),
        "semantic_frame_ref": fields.get("semantic_frame_ref"),
        "settlement_ref": fields.get("settlement_ref"),
        "worldlex_lineage": deepcopy(fields.get("worldlex_lineage")),
        "subjects": subjects,
        "future_selector": selector,
        "affected_domains": sorted(set(fields.get("affected_domains", []))),
        "priority": int(fields.get("priority", 0)),
        "scope": fields.get("scope", "branch"),
        "propagation": propagation,
        "start": int(fields.get("start", fields.get("game_time", 0))),
        "duration": fields.get("duration"),
        "reversible": bool(fields.get("reversible", False)),
        "activation": fields.get("activation", "active"),
        "effects": effects,
        "description": str(fields.get("description", "")),
    }
    if kind != "admission":
        payload.update({
            "subjects": [], "future_selector": None, "affected_domains": [], "duration": None,
            "reversible": False, "propagation": "existing_subjects", "effects": [],
        })
    return validate_world_event_record(_fingerprinted(payload))


def _record_adapter_id(effect: Mapping[str, Any]) -> str:
    adapter = effect.get("adapter")
    if isinstance(adapter, str):
        return adapter
    return _adapter_identity(adapter)


def _record_subject_key(effect: Mapping[str, Any]) -> str:
    subject = effect.get("subject")
    if isinstance(subject, Mapping):
        return _subject_key(subject)
    return str(subject)


def _record_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    return int(row["priority"]), int(row["game_time"]), str(row["event_id"])


def project_world_overlay(
    records: list[dict[str, Any]], *, world_id: str, branch_id: str, game_time: int,
    session_id: str | None = None, source_branch_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fold one explicit Ledger view using replayable game time only.

    By default only records originating on ``branch_id`` are visible.  Callers may explicitly
    include inherited origin branches with ``source_branch_ids``; sibling records remain excluded.
    """
    if isinstance(game_time, bool) or not isinstance(game_time, int) or game_time < 0:
        raise ValueError("WorldOverlay game_time is invalid")
    if not _WORLD_RE.fullmatch(str(world_id)):
        raise ValueError("WorldOverlay world_id is invalid")
    _id(branch_id, "WorldOverlay branch_id")
    permitted_branches = {branch_id}
    if source_branch_ids is not None:
        if not isinstance(source_branch_ids, Sequence) or isinstance(source_branch_ids, (str, bytes)):
            raise ValueError("WorldOverlay source_branch_ids must be a sequence")
        permitted_branches.update(_id(item, "WorldOverlay source branch") for item in source_branch_ids)

    valid = [validate_world_event_record(row) for row in records]
    if session_id is None:
        inferred = {
            str(row["session_id"]) for row in valid
            if row["world_id"] == world_id and row["branch_id"] in permitted_branches
        }
        if len(inferred) > 1:
            raise ValueError("WorldOverlay record view spans more than one session")
        session_id = next(iter(inferred), "")
    if session_id:
        _id(session_id, "WorldOverlay session_id")
    inherited_branches = permitted_branches - {branch_id}
    scoped_rows = [
        row for row in valid
        if row["world_id"] == world_id
        and row["branch_id"] in permitted_branches
        and (
            row["session_id"] == session_id
            or row["branch_id"] in inherited_branches
        )
    ]
    by_event_id: dict[str, dict[str, Any]] = {}
    for row in scoped_rows:
        prior = by_event_id.get(row["event_id"])
        if prior is not None and prior != row:
            raise ValueError("WorldOverlay contains conflicting immutable event identities")
        by_event_id[row["event_id"]] = row
    scoped = list(by_event_id.values())
    scoped.sort(key=_record_sort_key)
    admissions = {row["event_id"]: row for row in scoped if row["kind"] == "admission"}
    terminal_candidates: dict[str, list[dict[str, Any]]] = {}
    for row in scoped:
        if row["kind"] == "admission":
            continue
        target = admissions.get(str(row["relation_target"]))
        if target is None:
            raise ValueError("World Event terminal relation target is missing from this Ledger view")
        if row["kind"] == "reversal" and not bool(target.get("reversible")):
            raise ValueError("World Event reversal targets an irreversible admission")
        if row["game_time"] < target["game_time"] or row["turn"] < target["turn"]:
            raise ValueError("World Event terminal relation predates its admission")
        terminal_candidates.setdefault(target["event_id"], []).append(row)

    winning_terminal: dict[str, dict[str, Any]] = {}
    for target, candidates in terminal_candidates.items():
        active_candidates = [
            row for row in candidates
            if row.get("activation") == "active" and int(row["start"]) <= game_time
        ]
        if active_candidates:
            winning_terminal[target] = max(active_candidates, key=_record_sort_key)

    active: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    effective: dict[str, dict[str, dict[str, Any]]] = {}
    for row in scoped:
        if row["kind"] != "admission":
            winner = winning_terminal.get(str(row["relation_target"]))
            status = "winning_terminal" if winner and winner["event_id"] == row["event_id"] \
                else "scheduled_terminal" if int(row["start"]) > game_time \
                else "terminal_conflict_lost"
            history.append({
                "event_id": row["event_id"], "kind": row["kind"], "status": status,
                "relation_target": row["relation_target"],
            })
            continue
        status = "active"
        terminal = winning_terminal.get(row["event_id"])
        if row.get("activation") == "inactive":
            status = "inactive"
        elif game_time < int(row["start"]) or row.get("activation") == "scheduled":
            status = "scheduled"
        elif terminal is not None:
            status = terminal["kind"]
        elif row["duration"] is not None and game_time >= row["start"] + row["duration"]:
            status = "expired_by_duration"
        if status == "active":
            active.append(row)
            for effect in row["effects"]:
                if not effect["supported"]:
                    continue
                domain = effective.setdefault(effect["domain"], {})
                subject = domain.setdefault(_record_subject_key(effect), {})
                adapter_id = _record_adapter_id(effect)
                subject[effect["field"]] = {
                    "value": deepcopy(effect["value"]),
                    "event_id": row["event_id"],
                    "priority": row["priority"],
                    "cause_visibility": row["cause_visibility"],
                    "subject": deepcopy(effect.get("subject")),
                    "adapter": adapter_id,
                    "adapter_fingerprint": (
                        effect["adapter"].get("fingerprint")
                        if isinstance(effect.get("adapter"), Mapping) else None
                    ),
                    "receipt": deepcopy(effect.get("receipt")),
                }
        history.append({
            "event_id": row["event_id"], "kind": row["kind"], "status": status,
            "relation_target": row["relation_target"],
        })
    payload = {
        "schema": WORLD_OVERLAY_SCHEMA,
        "world_id": world_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "source_branch_ids": sorted(permitted_branches - {branch_id}),
        "game_time": game_time,
        "active_event_ids": [row["event_id"] for row in active],
        "effective": effective,
        "history": history,
    }
    return _fingerprinted(payload)


def _predicate_matches(predicate: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    actual = candidate.get(predicate["field"])
    expected = predicate["value"]
    operator = predicate["operator"]
    if operator == "eq":
        return actual == expected
    if operator == "not_eq":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "contains":
        return isinstance(actual, (list, tuple, set, frozenset)) \
            and all(item in actual for item in expected)
    return False


def subject_matches_selector(selector: Mapping[str, Any] | None, candidate: Mapping[str, Any]) -> bool:
    if selector is None or not isinstance(candidate, Mapping):
        return False
    try:
        if selector.get("schema") == LEGACY_SELECTOR_SCHEMA:
            row = _validate_legacy_selector(selector)
            if not isinstance(row["subject_kinds"], list) or not isinstance(row["predicates"], dict):
                return False
            return candidate.get("kind") in row["subject_kinds"] and all(
                candidate.get(key) == value for key, value in row["predicates"].items()
            )
        row = _validate_selector(selector)
    except (TypeError, ValueError):
        return False
    return candidate.get("kind") in row["subject_kinds"] \
        and all(_predicate_matches(predicate, candidate) for predicate in row["predicates"])


def project_state_overlay(
    state: Mapping[str, Any], *, game_time: int | None = None,
) -> dict[str, Any]:
    records = [row for row in (state.get("world_events") or []) if isinstance(row, dict)]
    if not records:
        return {}
    latest = records[-1]
    source_branch_ids = state.get("world_event_source_branch_ids")
    if not isinstance(source_branch_ids, list):
        source_branch_ids = []
    branch_id = str(state.get("world_event_branch_id") or latest["branch_id"])
    # Fresh v2 records use the retained turn index as their replayable game-time clock.  This
    # remains stable when a lower-order ``time_advance`` changes the descriptive day segment in
    # the same atomic batch.  Historical v1-only states keep their original scene-minute basis,
    # and detached projection fixtures without state metadata retain the explicit clock value.
    state_turn = (state.get("meta") or {}).get("turn")
    has_fresh_v2 = any(row.get("schema") == WORLD_EVENT_SCHEMA for row in records)
    if game_time is not None:
        if isinstance(game_time, bool) or not isinstance(game_time, int) or game_time < 0:
            raise ValueError("WorldOverlay game_time is invalid")
        projection_time = game_time
    else:
        projection_time = int(state_turn) if has_fresh_v2 \
            and isinstance(state_turn, int) and not isinstance(state_turn, bool) and state_turn >= 0 \
            else int((state.get("clock") or {}).get("minutes", 0))
    return project_world_overlay(
        records,
        world_id=str(latest["world_id"]),
        session_id=str(latest["session_id"]),
        branch_id=branch_id,
        source_branch_ids=source_branch_ids,
        game_time=projection_time,
    )


def effective_domain(overlay: Mapping[str, Any], domain: str) -> dict[str, Any]:
    """Return a detached effective domain for any supported downstream consumer."""
    if domain not in DOMAINS:
        raise ValueError("unsupported WorldOverlay domain")
    value = ((overlay.get("effective") or {}).get(domain) or {})
    return deepcopy(value) if isinstance(value, dict) else {}


def effective_subject(overlay: Mapping[str, Any], domain: str, subject: str) -> dict[str, Any]:
    value = effective_domain(overlay, domain).get(subject) or {}
    return deepcopy(value) if isinstance(value, dict) else {}


def effective_value(
    overlay: Mapping[str, Any], domain: str, subject: str, field: str, default: Any = None,
) -> Any:
    row = effective_subject(overlay, domain, subject).get(field)
    return deepcopy(row.get("value")) if isinstance(row, dict) else deepcopy(default)


def front_identity_visible_to_player(state: Mapping[str, Any], front_id: str) -> bool:
    """Keep a completed hidden front's causal identity out of Player-facing surfaces.

    Legacy fronts without immutable events retain their historical ``revealed`` behavior.
    Once a World Event exists, its frozen cause visibility is authoritative: the visible
    consequence may be narrated while the causal agenda/name remains hidden.
    """
    front = (state.get("fronts") or {}).get(str(front_id))
    if not isinstance(front, Mapping) or not front.get("revealed"):
        return False
    cause_id = f"front:{front_id}:completion"
    admissions = [
        row for row in state.get("world_events") or []
        if isinstance(row, Mapping)
        and row.get("kind") == "admission"
        and row.get("cause_id") == cause_id
    ]
    if not admissions:
        return True
    latest = max(
        admissions,
        key=lambda row: (
            int(row.get("turn", -1)),
            int(row.get("game_time", -1)),
            str(row.get("event_id") or ""),
        ),
    )
    return str(latest.get("cause_visibility") or "hidden") in {"public", "player"}


def _candidate_subject_ids(candidate: Mapping[str, Any]) -> set[str]:
    values = {
        str(candidate.get(field) or "")
        for field in ("id", "capability_id", "definition_id")
    }
    name = str(candidate.get("name") or "")
    if name:
        values.add(name)
    return {value for value in values if value}


def _effect_matches_candidate(
    event: Mapping[str, Any], effect: Mapping[str, Any], candidate: Mapping[str, Any],
) -> bool:
    subject = effect.get("subject")
    if not isinstance(subject, Mapping):
        return False
    if subject.get("kind") == "selector":
        return event.get("propagation") in {"future_subjects", "existing_and_future"} \
            and subject_matches_selector(event.get("future_selector"), candidate)
    subject_kind = str(subject.get("kind") or "")
    candidate_kind = str(candidate.get("kind") or "")
    compatible = subject_kind == candidate_kind \
        or subject_kind == "actor" and candidate_kind in {"actor", "npc", "enemy"}
    return compatible and str(subject.get("id") or "") in _candidate_subject_ids(candidate)


def future_subject_identity_resolved(
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    adapter_id: str = "spawn.eligibility/1",
    game_time: int | None = None,
) -> bool:
    """Fresh-admission gate for fields required by active selector-scoped eligibility.

    Reducer replay deliberately does not call this helper: historical factionless spawn rows
    remain replay-compatible, while every new admission must carry enough stable identity for an
    active selector to decide without treating omission as a bypass.
    """
    overlay = project_state_overlay(state, game_time=game_time)
    active = set(overlay.get("active_event_ids") or [])
    for event in state.get("world_events") or []:
        if not isinstance(event, Mapping) or event.get("event_id") not in active \
                or event.get("propagation") not in {"future_subjects", "existing_and_future"}:
            continue
        selector = event.get("future_selector")
        for effect in event.get("effects") or []:
            if not isinstance(effect, Mapping) or effect.get("supported") is not True:
                continue
            try:
                if _record_adapter_id(effect) != adapter_id:
                    continue
            except (TypeError, ValueError):
                continue
            if _selector_candidate_is_indeterminate(selector, candidate):
                return False
    return True


def _selector_candidate_is_indeterminate(
    selector: Mapping[str, Any] | None, candidate: Mapping[str, Any],
) -> bool:
    """Fail closed when an active eligibility selector could apply but identity is missing."""
    if selector is None:
        return False
    try:
        if selector.get("schema") == LEGACY_SELECTOR_SCHEMA:
            row = _validate_legacy_selector(selector)
            subject_kinds = row["subject_kinds"]
            fields = list(row["predicates"])
        else:
            row = _validate_selector(selector)
            subject_kinds = row["subject_kinds"]
            fields = [predicate["field"] for predicate in row["predicates"]]
    except (TypeError, ValueError):
        return True
    if candidate.get("kind") not in subject_kinds:
        return False
    return any(
        field not in candidate or candidate.get(field) is None or candidate.get(field) == ""
        for field in fields
    )


def _eligibility_decision(
    state: Mapping[str, Any], candidate: Mapping[str, Any], *, adapter_id: str,
    game_time: int | None = None,
) -> bool:
    """Fold one adapter's exact and selector-scoped active decisions in Ledger order."""
    overlay = project_state_overlay(state, game_time=game_time)
    active = set(overlay.get("active_event_ids") or [])
    decision = True
    ordered = sorted(
        (
            event for event in state.get("world_events") or []
            if isinstance(event, dict) and event.get("event_id") in active
        ),
        key=_record_sort_key,
    )
    for event in ordered:
        for effect in event.get("effects") or []:
            if not isinstance(effect, Mapping) or effect.get("supported") is not True:
                continue
            try:
                effect_adapter_id = _record_adapter_id(effect)
            except (TypeError, ValueError):
                continue
            if effect_adapter_id != adapter_id:
                continue
            if _effect_matches_candidate(event, effect, candidate) \
                    and isinstance(effect.get("value"), bool):
                decision = effect["value"]
    return decision


def future_subject_eligible(
    state: Mapping[str, Any], candidate: Mapping[str, Any], *, game_time: int | None = None,
) -> bool:
    """Resolve active spawn eligibility for an exact existing/future subject candidate."""
    return _eligibility_decision(
        state, candidate, adapter_id="spawn.eligibility/1", game_time=game_time,
    )


def capability_eligible(
    state: Mapping[str, Any], candidate: Mapping[str, Any], *, game_time: int | None = None,
) -> bool:
    """Resolve active eligibility before a capability is assigned, pooled, or executed."""
    normalized = {**dict(candidate), "kind": "capability"}
    return _eligibility_decision(
        state, normalized, adapter_id="capability.eligibility/1", game_time=game_time,
    )


def future_subject_effects(
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    domains: Sequence[str] | None = None,
    game_time: int | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Project supported selector-scoped effects for one concrete future subject.

    Nothing is copied into authoritative entity state.  Consumers receive a detached derived view,
    so expiry, reversal, supersession, retry, and replay all use the immutable event fold.
    """
    requested = set(domains or DOMAINS)
    if requested - DOMAINS:
        raise ValueError("unsupported WorldOverlay domain")
    overlay = project_state_overlay(state, game_time=game_time)
    active = set(overlay.get("active_event_ids") or [])
    projected: dict[str, dict[str, dict[str, Any]]] = {}
    ordered = sorted(
        (
            event for event in state.get("world_events") or []
            if isinstance(event, dict) and event.get("event_id") in active
        ),
        key=_record_sort_key,
    )
    for event in ordered:
        if event.get("propagation") not in {"future_subjects", "existing_and_future"} \
                or not subject_matches_selector(event.get("future_selector"), candidate):
            continue
        for effect in event.get("effects") or []:
            if not isinstance(effect, Mapping) or effect.get("supported") is not True \
                    or effect.get("domain") not in requested \
                    or not _effect_matches_candidate(event, effect, candidate):
                continue
            subject = effect.get("subject")
            if not isinstance(subject, Mapping):
                continue
            projected.setdefault(str(effect["domain"]), {})[str(effect["field"])] = {
                "value": deepcopy(effect.get("value")),
                "event_id": event.get("event_id"),
                "cause_visibility": event.get("cause_visibility"),
                "adapter": _record_adapter_id(effect),
                "receipt": deepcopy(effect.get("receipt")),
            }
    return projected
