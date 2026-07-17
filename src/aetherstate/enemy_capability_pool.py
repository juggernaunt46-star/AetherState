"""Enemy-kit adapter for the generic WorldLex capability-pool lifecycle.

This module translates an already validated ``enemy-kit/1`` into immutable WorldLex definition
references and an exact HP-mechanics snapshot.  It does not generate moves, grant them from a
role or tier, select an intent, admit a production receipt, or mutate runtime state.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .capability_glossary import COMPILER_VERSION, DEFINITION_SCHEMA, content_fingerprint
from .worldlex import (
    AdapterContract,
    AdapterRegistry,
    CapabilityPool,
    DefinitionRef,
    OwnerRef,
    PoolMember,
    SubjectRef,
    validate_adapter_contract,
    validate_pool,
    validate_pool_transition,
)


ENEMY_POOL_COMPILER_VERSION = "enemy-capability-pool/1"
ENEMY_MECHANICS_SCHEMA = "enemy-hp-move-adapter/1"
ENEMY_ADAPTER_ID = ENEMY_MECHANICS_SCHEMA
ENEMY_RECEIPT_ID = "enemy-opposition-hp/1"
ENEMY_CANDIDATES_SCHEMA = "enemy-capability-candidates/1"
ENEMY_BUNDLE_SCHEMA = "enemy-capability-runtime-bundle/1"
RECEIPT_ADMISSION_SCHEMA = "enemy-hp-receipt-admission/1"

MECHANICS_FIELDS = tuple(sorted((
    "id", "name", "primitive", "basis", "channel", "delivery", "range", "timing",
    "cadence", "target_rule", "accuracy", "damage", "tell", "counterplay", "sensory",
    "risk", "forbid", "danger", "reaction",
)))

ENEMY_ADAPTER_CONTRACT = AdapterContract.create(
    adapter_id=ENEMY_ADAPTER_ID,
    receipt_id=ENEMY_RECEIPT_ID,
    definition_schemas=(DEFINITION_SCHEMA,),
    definition_kinds=("enemy_move",),
    consumed_fields=MECHANICS_FIELDS,
    concept_ids=("family.committed_strike", "family.direct_pressure"),
)
_ADAPTER_REGISTRY = AdapterRegistry((ENEMY_ADAPTER_CONTRACT,))

_KIT_FIELDS = {
    "schema", "generator", "tier", "signature_basis", "role_axis", "basis", "grounding",
    "moves", "fingerprint",
}
_KIT_HEADER_FIELDS = _KIT_FIELDS - {"moves"} | {"move_count"}
_MOVE_INPUT_REQUIRED = set(MECHANICS_FIELDS) - {"reaction"}
_MOVE_INPUT_ALLOWED = set(MECHANICS_FIELDS)
_SNAPSHOT_FIELDS = {
    "schema", "adapter_id", "receipt_id", "definition_ref", "kit_fingerprint", "move_index",
    "move", "fingerprint",
}
_BUNDLE_FIELDS = {
    "schema", "compiler_version", "adapter_contract", "kit_header", "definitions", "mechanics",
    "pools", "receipt_admission", "fingerprint",
}
_CANDIDATE_FIELDS = {
    "schema", "compiler_version", "adapter_contract", "kit_header", "definitions", "mechanics",
    "fingerprint",
}
_DEFINITION_FIELDS = {
    "schema", "compiler_version", "definition_id", "revision", "parent_fingerprint", "kind",
    "name", "aliases", "source_wording", "description", "world_id", "owner_scope", "owner_id",
    "authoring_source", "genre_ids", "concept_ids", "basis", "delivery", "semantic_primitive",
    "functional_family", "effect_channel", "target", "range", "area", "timing", "cadence",
    "duration", "availability", "cost", "power_ceiling", "mastery", "prerequisites",
    "side_effects", "counterplay", "risk", "world_scale_potential", "grounding_evidence",
    "support_classification", "receipt_concept_ids", "receipt_validation", "receipt_id",
    "interpretation_mode", "requested_receipt_id", "fingerprint",
}
_WORLD_ID_RE = re.compile(r"world_[0-9a-f]{32}\Z")
_PLAIN_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,159}\Z")
_KIT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{20}\Z")


class EnemyCapabilityPoolError(ValueError):
    """The enemy adapter received forged or incompatible evidence."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise EnemyCapabilityPoolError(f"{label} must be an object with string fields")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields - set(value))
        extra = sorted(set(value) - fields)
        raise EnemyCapabilityPoolError(f"{label} fields do not match; missing={missing}, extra={extra}")


def _plain_copy(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnemyCapabilityPoolError(f"{label} must be finite JSON data") from exc


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise EnemyCapabilityPoolError(f"{label} must be a non-empty trimmed string")
    return value


def _external_ref(value: object, label: str) -> str:
    value = _nonempty_string(value, label)
    if len(value) > 240:
        raise EnemyCapabilityPoolError(f"{label} is too long")
    return value


def _fingerprint_payload(value: Mapping[str, Any]) -> str:
    return content_fingerprint(dict(value))


def _kit_fingerprint(kit_without_fingerprint: Mapping[str, Any]) -> str:
    material = json.dumps(
        kit_without_fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=10).hexdigest()


def _validate_move(value: object, *, basis: set[str], label: str) -> dict[str, Any]:
    move = _mapping(value, label)
    if not _MOVE_INPUT_REQUIRED <= set(move) or set(move) - _MOVE_INPUT_ALLOWED:
        missing = sorted(_MOVE_INPUT_REQUIRED - set(move))
        extra = sorted(set(move) - _MOVE_INPUT_ALLOWED)
        raise EnemyCapabilityPoolError(f"{label} fields do not match; missing={missing}, extra={extra}")
    for field in _MOVE_INPUT_REQUIRED - {"accuracy", "damage", "counterplay"}:
        _nonempty_string(move[field], f"{label}.{field}")
    if move["basis"] not in basis:
        raise EnemyCapabilityPoolError(f"{label}.basis is not grounded by the kit")
    if move["channel"] != "hp" or move["target_rule"] != "player":
        raise EnemyCapabilityPoolError(f"{label} is outside the enemy HP adapter contract")
    for field, lower, upper in (("accuracy", -2, 2), ("damage", -2, 3)):
        number = move[field]
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise EnemyCapabilityPoolError(f"{label}.{field} is outside the frozen runtime bounds")
    counters = move["counterplay"]
    if not isinstance(counters, list) or not counters or len(counters) > 3:
        raise EnemyCapabilityPoolError(f"{label}.counterplay must contain one to three strings")
    for counter in counters:
        _nonempty_string(counter, f"{label}.counterplay")
    reaction = move.get("reaction")
    if reaction is not None:
        reaction = _mapping(reaction, f"{label}.reaction")
        reaction_fields = {"schema", "kind", "trigger", "cost", "effect"}
        _exact_fields(reaction, reaction_fields, f"{label}.reaction")
        for field in reaction_fields:
            _nonempty_string(reaction[field], f"{label}.reaction.{field}")
    result = {field: _plain_copy(move.get(field), f"{label}.{field}") for field in MECHANICS_FIELDS}
    return result


def validate_enemy_kit(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an exact frozen ``enemy-kit/1`` without consulting the upstream generator."""
    kit = _mapping(value, "enemy kit")
    _exact_fields(kit, _KIT_FIELDS, "enemy kit")
    if kit["schema"] != "enemy-kit/1":
        raise EnemyCapabilityPoolError("unsupported enemy kit schema")
    if not isinstance(kit["generator"], str) or not re.fullmatch(r"enemy-grammar/[1-9][0-9]*", kit["generator"]):
        raise EnemyCapabilityPoolError("enemy kit generator must be versioned")
    for field in ("tier", "signature_basis", "role_axis"):
        _nonempty_string(kit[field], f"enemy kit {field}")
    if not isinstance(kit["basis"], list) or not kit["basis"]:
        raise EnemyCapabilityPoolError("enemy kit basis must be a non-empty list")
    basis_values = []
    for item in kit["basis"]:
        basis_values.append(_nonempty_string(item, "enemy kit basis"))
    if len(basis_values) != len(set(basis_values)) or kit["signature_basis"] not in basis_values:
        raise EnemyCapabilityPoolError("enemy kit basis values must be unique and contain the signature")
    grounding = _mapping(kit["grounding"], "enemy kit grounding")
    _plain_copy(grounding, "enemy kit grounding")
    raw_moves = kit["moves"]
    if not isinstance(raw_moves, list) or not 2 <= len(raw_moves) <= 4:
        raise EnemyCapabilityPoolError("enemy kit must contain two to four exact moves")
    moves = [
        _validate_move(move, basis=set(basis_values), label=f"enemy kit move {index}")
        for index, move in enumerate(raw_moves)
    ]
    ids = [move["id"] for move in moves]
    if len(ids) != len(set(ids)):
        raise EnemyCapabilityPoolError("enemy kit move ids must be unique")
    fingerprint = kit["fingerprint"]
    if not isinstance(fingerprint, str) or not _KIT_FINGERPRINT_RE.fullmatch(fingerprint):
        raise EnemyCapabilityPoolError("enemy kit fingerprint must be a 20-character lowercase hash")
    frozen = {key: _plain_copy(kit[key], f"enemy kit {key}") for key in _KIT_FIELDS - {"fingerprint"}}
    if _kit_fingerprint(frozen) != fingerprint:
        raise EnemyCapabilityPoolError("enemy kit fingerprint does not match its payload")
    frozen["moves"] = moves
    return {**frozen, "fingerprint": fingerprint}


def _functional_family(move: Mapping[str, Any]) -> str:
    if move["damage"] > 0 or move["cadence"] == "setup" or any(
        token in move["timing"] for token in ("wind-up", "charged", "committed")
    ):
        return "family.committed_strike"
    return "family.direct_pressure"


def _grounding_evidence(kit: Mapping[str, Any], move: Mapping[str, Any]) -> list[str]:
    evidence = [f"enemy-kit:{kit['fingerprint']}", f"move:{move['id']}"]
    grounding = kit["grounding"]
    name = grounding.get("name")
    armament = grounding.get("armament")
    if isinstance(name, str) and name:
        evidence.append(name)
    if isinstance(armament, str) and armament:
        evidence.append(armament)
    identity = grounding.get("identity")
    if isinstance(identity, list):
        evidence.extend(item for item in identity if isinstance(item, str) and item)
    return list(dict.fromkeys(evidence))


def _compile_definition(
    kit: Mapping[str, Any], move: Mapping[str, Any], index: int, world_id: str,
) -> dict[str, Any]:
    family = _functional_family(move)
    definition_id = f"enemy_move.{kit['fingerprint']}.{index}.{move['id']}"
    record: dict[str, Any] = {
        "schema": DEFINITION_SCHEMA,
        "compiler_version": COMPILER_VERSION,
        "definition_id": definition_id,
        "revision": 1,
        "parent_fingerprint": None,
        "kind": "enemy_move",
        "name": move["name"],
        "aliases": [],
        "source_wording": move["name"],
        "description": f"Frozen enemy move translated from {kit['generator']}.",
        "world_id": world_id,
        "owner_scope": "world",
        "owner_id": world_id,
        "authoring_source": "enemy-kit/1",
        "genre_ids": [],
        "concept_ids": [family, "primitive.strike", f"basis.{move['basis']}"],
        "basis": move["basis"],
        "delivery": move["delivery"],
        "semantic_primitive": "strike",
        "functional_family": family,
        "effect_channel": move["channel"],
        "target": move["target_rule"],
        "range": move["range"],
        "area": "single_target",
        "timing": move["timing"],
        "cadence": move["cadence"],
        "duration": None,
        "availability": None,
        "cost": {},
        "power_ceiling": move["danger"],
        "mastery": None,
        "prerequisites": {},
        "side_effects": [],
        "counterplay": list(move["counterplay"]),
        "risk": [] if move["risk"] == "none" else [move["risk"]],
        "world_scale_potential": "personal",
        "grounding_evidence": _grounding_evidence(kit, move),
        "support_classification": [
            {"concept_id": family, "classification": "narration_boundary"},
            {"concept_id": "primitive.strike", "classification": "lore_only"},
            {"concept_id": f"basis.{move['basis']}", "classification": "lore_only"},
        ],
        "receipt_concept_ids": [family],
        "receipt_validation": {
            "admitted": False,
            "reason": "WorldLex adapter compilation never admits a reducer receipt.",
            "receipt_concept_ids": [family],
        },
        "receipt_id": None,
        "interpretation_mode": "explicit_only",
        "requested_receipt_id": ENEMY_RECEIPT_ID,
    }
    record["fingerprint"] = _fingerprint_payload(record)
    return record


def _definition_ref(definition: Mapping[str, Any]) -> DefinitionRef:
    owner = OwnerRef(kind="world", id=definition["owner_id"], world_id=definition["world_id"])
    return DefinitionRef(
        definition_schema=definition["schema"],
        definition_id=definition["definition_id"],
        revision=definition["revision"],
        fingerprint=definition["fingerprint"],
        world_id=definition["world_id"],
        kind=definition["kind"],
        owner=owner,
    )


def validate_enemy_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    definition = _mapping(value, "enemy capability definition")
    _exact_fields(definition, _DEFINITION_FIELDS, "enemy capability definition")
    if definition["schema"] != DEFINITION_SCHEMA or definition["compiler_version"] != COMPILER_VERSION:
        raise EnemyCapabilityPoolError("unsupported enemy capability definition schema")
    if definition["kind"] != "enemy_move" or definition["revision"] != 1:
        raise EnemyCapabilityPoolError("enemy definition must be an exact first-revision enemy_move")
    if not isinstance(definition["definition_id"], str) or not _PLAIN_ID_RE.fullmatch(definition["definition_id"]):
        raise EnemyCapabilityPoolError("enemy definition id must be stable and lowercase")
    if not isinstance(definition["world_id"], str) or not _WORLD_ID_RE.fullmatch(definition["world_id"]):
        raise EnemyCapabilityPoolError("enemy definition world id is invalid")
    if definition["owner_scope"] != "world" or definition["owner_id"] != definition["world_id"]:
        raise EnemyCapabilityPoolError("enemy candidates remain world-owned before assignment")
    if definition["receipt_id"] is not None or definition["requested_receipt_id"] != ENEMY_RECEIPT_ID:
        raise EnemyCapabilityPoolError("enemy definition cannot claim receipt admission")
    fingerprint = definition["fingerprint"]
    payload = {key: _plain_copy(item, f"enemy definition {key}") for key, item in definition.items() if key != "fingerprint"}
    if fingerprint != _fingerprint_payload(payload):
        raise EnemyCapabilityPoolError("enemy definition fingerprint does not match its payload")
    result = dict(payload)
    result["fingerprint"] = fingerprint
    _ADAPTER_REGISTRY.require_binding(
        adapter_id=ENEMY_ADAPTER_ID,
        receipt_id=ENEMY_RECEIPT_ID,
        definition=_definition_ref(result),
    )
    return result


def _compile_snapshot(
    kit: Mapping[str, Any], move: Mapping[str, Any], index: int, definition: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema": ENEMY_MECHANICS_SCHEMA,
        "adapter_id": ENEMY_ADAPTER_ID,
        "receipt_id": ENEMY_RECEIPT_ID,
        "definition_ref": _definition_ref(definition).as_dict(),
        "kit_fingerprint": kit["fingerprint"],
        "move_index": index,
        "move": {field: _plain_copy(move[field], f"mechanics {field}") for field in MECHANICS_FIELDS},
    }
    return {**payload, "fingerprint": _fingerprint_payload(payload)}


def validate_enemy_mechanics(value: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _mapping(value, "enemy mechanics snapshot")
    _exact_fields(snapshot, _SNAPSHOT_FIELDS, "enemy mechanics snapshot")
    if snapshot["schema"] != ENEMY_MECHANICS_SCHEMA or snapshot["adapter_id"] != ENEMY_ADAPTER_ID:
        raise EnemyCapabilityPoolError("unsupported enemy mechanics adapter")
    if snapshot["receipt_id"] != ENEMY_RECEIPT_ID or ENEMY_ADAPTER_ID == ENEMY_RECEIPT_ID:
        raise EnemyCapabilityPoolError("enemy mechanics receipt identity is invalid")
    if not isinstance(snapshot["kit_fingerprint"], str) or not _KIT_FINGERPRINT_RE.fullmatch(snapshot["kit_fingerprint"]):
        raise EnemyCapabilityPoolError("enemy mechanics kit fingerprint is invalid")
    index = snapshot["move_index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 3:
        raise EnemyCapabilityPoolError("enemy mechanics move index is invalid")
    move = _mapping(snapshot["move"], "enemy mechanics move")
    _exact_fields(move, set(MECHANICS_FIELDS), "enemy mechanics move")
    basis = {_nonempty_string(move["basis"], "enemy mechanics basis")}
    normalized_move = _validate_move(move, basis=basis, label="enemy mechanics move")
    definition_ref = DefinitionRef.from_dict(_mapping(snapshot["definition_ref"], "definition ref"))
    _ADAPTER_REGISTRY.require_binding(
        adapter_id=ENEMY_ADAPTER_ID, receipt_id=ENEMY_RECEIPT_ID, definition=definition_ref,
    )
    payload = {
        "schema": snapshot["schema"],
        "adapter_id": snapshot["adapter_id"],
        "receipt_id": snapshot["receipt_id"],
        "definition_ref": definition_ref.as_dict(),
        "kit_fingerprint": snapshot["kit_fingerprint"],
        "move_index": index,
        "move": normalized_move,
    }
    if snapshot["fingerprint"] != _fingerprint_payload(payload):
        raise EnemyCapabilityPoolError("enemy mechanics fingerprint does not match its payload")
    return {**payload, "fingerprint": snapshot["fingerprint"]}


def seal_enemy_hp_receipt_admission(
    admission_refs: Mapping[str, str], *, authority_ref: str,
) -> dict[str, Any]:
    """Seal code-owned receipt evidence after the HP reducer has admitted exact move refs.

    This helper is a trusted integration seam, not a parser for model or caller input.  Spawn code
    must discard caller-supplied admission objects and invoke this only from reducer-owned facts.
    """
    refs = _mapping(admission_refs, "receipt admission refs")
    normalized = {
        _nonempty_string(move_id, "receipt admission move id"): _external_ref(
            ref, "receipt admission ref"
        )
        for move_id, ref in refs.items()
    }
    payload = {
        "schema": RECEIPT_ADMISSION_SCHEMA,
        "adapter_id": ENEMY_ADAPTER_ID,
        "receipt_id": ENEMY_RECEIPT_ID,
        "contract_fingerprint": ENEMY_ADAPTER_CONTRACT.fingerprint,
        "authority_ref": _external_ref(authority_ref, "receipt admission authority_ref"),
        "refs": {key: normalized[key] for key in sorted(normalized)},
    }
    return {**payload, "fingerprint": _fingerprint_payload(payload)}


def validate_enemy_hp_receipt_admission(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _mapping(value, "enemy HP receipt admission")
    fields = {
        "schema", "adapter_id", "receipt_id", "contract_fingerprint", "authority_ref", "refs",
        "fingerprint",
    }
    _exact_fields(evidence, fields, "enemy HP receipt admission")
    if evidence["schema"] != RECEIPT_ADMISSION_SCHEMA:
        raise EnemyCapabilityPoolError("unsupported enemy HP receipt admission schema")
    if evidence["adapter_id"] != ENEMY_ADAPTER_ID or evidence["receipt_id"] != ENEMY_RECEIPT_ID:
        raise EnemyCapabilityPoolError("receipt admission adapter/receipt binding is invalid")
    if evidence["contract_fingerprint"] != ENEMY_ADAPTER_CONTRACT.fingerprint:
        raise EnemyCapabilityPoolError("receipt admission contract fingerprint is invalid")
    _external_ref(evidence["authority_ref"], "receipt admission authority_ref")
    refs = _mapping(evidence["refs"], "receipt admission refs")
    normalized = {
        _nonempty_string(move_id, "receipt admission move id"): _external_ref(
            ref, "receipt admission ref"
        )
        for move_id, ref in refs.items()
    }
    if list(refs) != sorted(refs):
        raise EnemyCapabilityPoolError("receipt admission refs must use canonical key order")
    payload = {
        key: _plain_copy(item, f"receipt admission {key}")
        for key, item in evidence.items()
        if key != "fingerprint"
    }
    payload["refs"] = {key: normalized[key] for key in sorted(normalized)}
    if evidence["fingerprint"] != _fingerprint_payload(payload):
        raise EnemyCapabilityPoolError("receipt admission fingerprint does not match its payload")
    return {**payload, "fingerprint": evidence["fingerprint"]}


def _normalize_refs(value: Mapping[str, str], label: str) -> dict[str, str]:
    value = _mapping(value, label)
    result = {
        _nonempty_string(move_id, f"{label} move id"): _external_ref(ref, f"{label} ref")
        for move_id, ref in value.items()
    }
    return {key: result[key] for key in sorted(result)}


def _members(
    move_ids: Iterable[str], definitions: Mapping[str, Mapping[str, Any]], *, stage: str,
    assignment_refs: Mapping[str, str], eligibility_refs: Mapping[str, str],
    admission_refs: Mapping[str, str],
) -> list[PoolMember]:
    result = []
    for move_id in move_ids:
        definition = _definition_ref(definitions[move_id])
        if stage == "world_library":
            member = PoolMember(definition, True, False, False)
        elif stage == "assigned":
            member = PoolMember(
                definition, True, True, False, assignment_ref=assignment_refs[move_id],
            )
        elif stage == "spawn_eligible":
            member = PoolMember(
                definition, True, True, False,
                assignment_ref=assignment_refs[move_id],
                eligibility_ref=eligibility_refs[move_id],
                adapter_id=ENEMY_ADAPTER_ID,
                receipt_id=ENEMY_RECEIPT_ID,
            )
        else:
            member = PoolMember(
                definition, True, True, True,
                assignment_ref=assignment_refs[move_id],
                eligibility_ref=eligibility_refs[move_id],
                adapter_id=ENEMY_ADAPTER_ID,
                receipt_id=ENEMY_RECEIPT_ID,
                admission_ref=admission_refs[move_id],
                classification="executable",
            )
        result.append(member)
    return result


def compile_enemy_candidates(
    kit: Mapping[str, Any], *, world_id: str,
) -> dict[str, Any]:
    """Translate a frozen kit into definitions/mechanics before assignment exists."""
    frozen_kit = validate_enemy_kit(kit)
    if not isinstance(world_id, str) or not _WORLD_ID_RE.fullmatch(world_id):
        raise EnemyCapabilityPoolError("world_id must be world_ followed by 32 lowercase hex digits")
    definitions_by_move = {
        move["id"]: _compile_definition(frozen_kit, move, index, world_id)
        for index, move in enumerate(frozen_kit["moves"])
    }
    mechanics = [
        _compile_snapshot(frozen_kit, move, index, definitions_by_move[move["id"]])
        for index, move in enumerate(frozen_kit["moves"])
    ]
    ordered_definitions = sorted(definitions_by_move.values(), key=lambda item: item["definition_id"])
    ordered_mechanics = sorted(mechanics, key=lambda item: item["definition_ref"]["definition_id"])
    kit_header = {
        key: _plain_copy(frozen_kit[key], f"kit header {key}")
        for key in _KIT_FIELDS - {"moves"}
    }
    kit_header["move_count"] = len(frozen_kit["moves"])
    payload = {
        "schema": ENEMY_CANDIDATES_SCHEMA,
        "compiler_version": ENEMY_POOL_COMPILER_VERSION,
        "adapter_contract": ENEMY_ADAPTER_CONTRACT.as_dict(),
        "kit_header": kit_header,
        "definitions": ordered_definitions,
        "mechanics": ordered_mechanics,
    }
    return {**payload, "fingerprint": _fingerprint_payload(payload)}


def validate_enemy_candidates(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the append-ready, pre-assignment output of :func:`compile_enemy_candidates`."""
    candidates = _mapping(value, "enemy capability candidates")
    _exact_fields(candidates, _CANDIDATE_FIELDS, "enemy capability candidates")
    if candidates["schema"] != ENEMY_CANDIDATES_SCHEMA \
            or candidates["compiler_version"] != ENEMY_POOL_COMPILER_VERSION:
        raise EnemyCapabilityPoolError("unsupported enemy capability candidates schema")
    contract = validate_adapter_contract(_mapping(candidates["adapter_contract"], "adapter contract"))
    if contract != ENEMY_ADAPTER_CONTRACT:
        raise EnemyCapabilityPoolError("enemy candidate adapter contract does not match this compiler")
    header = _mapping(candidates["kit_header"], "kit header")
    _exact_fields(header, _KIT_HEADER_FIELDS, "kit header")
    if isinstance(header["move_count"], bool) or not isinstance(header["move_count"], int) \
            or not 2 <= header["move_count"] <= 4:
        raise EnemyCapabilityPoolError("kit header move_count is invalid")
    if not isinstance(header["fingerprint"], str) \
            or not _KIT_FINGERPRINT_RE.fullmatch(header["fingerprint"]):
        raise EnemyCapabilityPoolError("kit header fingerprint is invalid")
    raw_definitions = candidates["definitions"]
    raw_mechanics = candidates["mechanics"]
    if not isinstance(raw_definitions, list) or not isinstance(raw_mechanics, list):
        raise EnemyCapabilityPoolError("candidate definitions and mechanics must be lists")
    definitions = [validate_enemy_definition(item) for item in raw_definitions]
    mechanics = [validate_enemy_mechanics(item) for item in raw_mechanics]
    if len(definitions) != header["move_count"] or len(mechanics) != len(definitions):
        raise EnemyCapabilityPoolError("candidate counts do not match the frozen kit")
    if definitions != sorted(definitions, key=lambda item: item["definition_id"]):
        raise EnemyCapabilityPoolError("candidate definitions must use canonical order")
    if mechanics != sorted(mechanics, key=lambda item: item["definition_ref"]["definition_id"]):
        raise EnemyCapabilityPoolError("candidate mechanics must use canonical order")
    definition_index = {item["fingerprint"]: item for item in definitions}
    if len(definition_index) != len(definitions):
        raise EnemyCapabilityPoolError("candidates contain duplicate definitions")
    positions = set()
    move_ids = set()
    for snapshot in mechanics:
        definition = definition_index.get(snapshot["definition_ref"]["fingerprint"])
        if definition is None or _definition_ref(definition).as_dict() != snapshot["definition_ref"]:
            raise EnemyCapabilityPoolError("candidate mechanics references an absent definition")
        if snapshot["kit_fingerprint"] != header["fingerprint"]:
            raise EnemyCapabilityPoolError("candidate mechanics belongs to a different kit")
        move = snapshot["move"]
        expected_definition_id = (
            f"enemy_move.{header['fingerprint']}.{snapshot['move_index']}.{move['id']}"
        )
        if definition["definition_id"] != expected_definition_id:
            raise EnemyCapabilityPoolError("candidate definition identity was forged")
        for definition_field, move_field in (
            ("name", "name"), ("basis", "basis"), ("delivery", "delivery"),
            ("effect_channel", "channel"), ("range", "range"), ("timing", "timing"),
            ("cadence", "cadence"), ("target", "target_rule"),
        ):
            if definition[definition_field] != move[move_field]:
                raise EnemyCapabilityPoolError("candidate mechanics disagrees with its definition")
        if snapshot["move_index"] in positions or snapshot["move"]["id"] in move_ids:
            raise EnemyCapabilityPoolError("candidate mechanics positions and move ids must be unique")
        positions.add(snapshot["move_index"])
        move_ids.add(snapshot["move"]["id"])
    if positions != set(range(header["move_count"])):
        raise EnemyCapabilityPoolError("candidate mechanics positions are not complete")
    ordered_moves = []
    for snapshot in sorted(mechanics, key=lambda item: item["move_index"]):
        move = _plain_copy(snapshot["move"], "candidate mechanics move")
        if move["reaction"] is None:
            del move["reaction"]
        ordered_moves.append(move)
    kit_payload = {
        key: _plain_copy(item, f"candidate kit header {key}")
        for key, item in header.items()
        if key not in {"fingerprint", "move_count"}
    }
    kit_payload["moves"] = ordered_moves
    if _kit_fingerprint(kit_payload) != header["fingerprint"]:
        raise EnemyCapabilityPoolError("candidate mechanics cannot reconstruct the frozen kit")
    payload = {
        key: _plain_copy(item, f"enemy candidates {key}")
        for key, item in candidates.items()
        if key != "fingerprint"
    }
    if candidates["fingerprint"] != _fingerprint_payload(payload):
        raise EnemyCapabilityPoolError("enemy candidates fingerprint does not match its payload")
    return {**payload, "fingerprint": candidates["fingerprint"]}


def compile_enemy_capability_bundle(
    candidates: Mapping[str, Any], *, subject_id: str,
    assignment_refs: Mapping[str, str], eligibility_refs: Mapping[str, str],
    receipt_admission: Mapping[str, Any],
) -> dict[str, Any]:
    """Finalize externally narrowed WorldLex stages from append-ready candidates.

    Mapping keys are frozen move ids.  Each successive key set must be a subset of the preceding
    stage: candidates -> assignments -> eligibility -> code-owned receipt admissions.
    """
    frozen_candidates = validate_enemy_candidates(candidates)
    if not isinstance(subject_id, str) or not _PLAIN_ID_RE.fullmatch(subject_id):
        raise EnemyCapabilityPoolError("subject_id must be a stable lowercase identifier")
    assignments = _normalize_refs(assignment_refs, "assignment refs")
    eligibility = _normalize_refs(eligibility_refs, "eligibility refs")
    admission = validate_enemy_hp_receipt_admission(receipt_admission)
    admission_refs = dict(admission["refs"])
    ordered_definitions = frozen_candidates["definitions"]
    ordered_mechanics = frozen_candidates["mechanics"]
    frozen_kit_header = frozen_candidates["kit_header"]
    world_ids = {definition["world_id"] for definition in ordered_definitions}
    if len(world_ids) != 1:
        raise EnemyCapabilityPoolError("enemy candidates must belong to one exact world")
    world_id = next(iter(world_ids))
    kit_ids = [
        snapshot["move"]["id"]
        for snapshot in sorted(ordered_mechanics, key=lambda item: item["move_index"])
    ]
    kit_id_set = set(kit_ids)
    if not set(assignments) <= kit_id_set:
        raise EnemyCapabilityPoolError("assignment evidence names a move absent from the frozen kit")
    if not set(eligibility) <= set(assignments):
        raise EnemyCapabilityPoolError("eligibility evidence cannot grant an unassigned move")
    if not set(admission_refs) <= set(eligibility):
        raise EnemyCapabilityPoolError("admission evidence cannot grant an ineligible move")
    definition_by_fingerprint = {item["fingerprint"]: item for item in ordered_definitions}
    definitions_by_move = {
        snapshot["move"]["id"]: definition_by_fingerprint[snapshot["definition_ref"]["fingerprint"]]
        for snapshot in ordered_mechanics
    }

    library = CapabilityPool.create(
        pool_id=f"enemy.library.{frozen_kit_header['fingerprint']}",
        stage="world_library",
        world_id=world_id,
        subject=SubjectRef("world", world_id, world_id),
        members=_members(
            kit_ids, definitions_by_move, stage="world_library", assignment_refs=assignments,
            eligibility_refs=eligibility, admission_refs=admission_refs,
        ),
    )
    subject = SubjectRef("enemy", subject_id, world_id)
    pool_id = f"enemy.pool.{frozen_kit_header['fingerprint']}"
    assigned = CapabilityPool.create(
        pool_id=pool_id,
        stage="assigned",
        world_id=world_id,
        subject=subject,
        members=_members(
            assignments, definitions_by_move, stage="assigned", assignment_refs=assignments,
            eligibility_refs=eligibility, admission_refs=admission_refs,
        ),
        parent_fingerprint=library.fingerprint,
    )
    context_fingerprint = content_fingerprint({
        "kit_fingerprint": frozen_kit_header["fingerprint"],
        "subject": subject.as_dict(),
        "eligibility_refs": eligibility,
    })
    eligible = CapabilityPool.create(
        pool_id=pool_id,
        stage="spawn_eligible",
        world_id=world_id,
        subject=subject,
        members=_members(
            eligibility, definitions_by_move, stage="spawn_eligible", assignment_refs=assignments,
            eligibility_refs=eligibility, admission_refs=admission_refs,
        ),
        parent_fingerprint=assigned.fingerprint,
        context_fingerprint=context_fingerprint,
    )
    runtime = CapabilityPool.create(
        pool_id=pool_id,
        stage="runtime",
        world_id=world_id,
        subject=subject,
        members=_members(
            admission_refs, definitions_by_move, stage="runtime", assignment_refs=assignments,
            eligibility_refs=eligibility, admission_refs=admission_refs,
        ),
        parent_fingerprint=eligible.fingerprint,
        context_fingerprint=context_fingerprint,
    )
    validate_pool_transition(library, assigned)
    validate_pool_transition(assigned, eligible)
    validate_pool_transition(eligible, runtime)

    payload = {
        "schema": ENEMY_BUNDLE_SCHEMA,
        "compiler_version": ENEMY_POOL_COMPILER_VERSION,
        "adapter_contract": ENEMY_ADAPTER_CONTRACT.as_dict(),
        "kit_header": frozen_kit_header,
        "definitions": ordered_definitions,
        "mechanics": ordered_mechanics,
        "pools": {
            "world_library": library.as_dict(),
            "assigned": assigned.as_dict(),
            "spawn_eligible": eligible.as_dict(),
            "runtime": runtime.as_dict(),
        },
        "receipt_admission": admission,
    }
    return {**payload, "fingerprint": _fingerprint_payload(payload)}


def validate_enemy_capability_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate all immutable definitions, mechanics, evidence, pools, and cross-links."""
    bundle = _mapping(value, "enemy capability bundle")
    _exact_fields(bundle, _BUNDLE_FIELDS, "enemy capability bundle")
    if bundle["schema"] != ENEMY_BUNDLE_SCHEMA or bundle["compiler_version"] != ENEMY_POOL_COMPILER_VERSION:
        raise EnemyCapabilityPoolError("unsupported enemy capability bundle schema")
    contract = validate_adapter_contract(_mapping(bundle["adapter_contract"], "adapter contract"))
    if contract != ENEMY_ADAPTER_CONTRACT:
        raise EnemyCapabilityPoolError("enemy bundle adapter contract does not match this compiler")
    candidate_payload = {
        "schema": ENEMY_CANDIDATES_SCHEMA,
        "compiler_version": bundle["compiler_version"],
        "adapter_contract": bundle["adapter_contract"],
        "kit_header": bundle["kit_header"],
        "definitions": bundle["definitions"],
        "mechanics": bundle["mechanics"],
    }
    validate_enemy_candidates({
        **candidate_payload,
        "fingerprint": _fingerprint_payload(candidate_payload),
    })
    header = _mapping(bundle["kit_header"], "kit header")
    _exact_fields(header, _KIT_HEADER_FIELDS, "kit header")
    if not isinstance(header["move_count"], int) or isinstance(header["move_count"], bool) \
            or not 2 <= header["move_count"] <= 4:
        raise EnemyCapabilityPoolError("kit header move_count is invalid")
    definitions_raw = bundle["definitions"]
    mechanics_raw = bundle["mechanics"]
    if not isinstance(definitions_raw, list) or not isinstance(mechanics_raw, list):
        raise EnemyCapabilityPoolError("bundle definitions and mechanics must be lists")
    definitions = [validate_enemy_definition(item) for item in definitions_raw]
    mechanics = [validate_enemy_mechanics(item) for item in mechanics_raw]
    if len(definitions) != header["move_count"] or len(mechanics) != len(definitions):
        raise EnemyCapabilityPoolError("bundle candidate counts do not match the frozen kit")
    if definitions != sorted(definitions, key=lambda item: item["definition_id"]):
        raise EnemyCapabilityPoolError("bundle definitions must use canonical order")
    if mechanics != sorted(mechanics, key=lambda item: item["definition_ref"]["definition_id"]):
        raise EnemyCapabilityPoolError("bundle mechanics must use canonical order")
    definition_index = {item["fingerprint"]: item for item in definitions}
    if len(definition_index) != len(definitions):
        raise EnemyCapabilityPoolError("bundle contains duplicate enemy definitions")
    for snapshot in mechanics:
        ref = snapshot["definition_ref"]
        definition = definition_index.get(ref["fingerprint"])
        if definition is None or _definition_ref(definition).as_dict() != ref:
            raise EnemyCapabilityPoolError("enemy mechanics references an absent or forged definition")
        move = snapshot["move"]
        for definition_field, move_field in (
            ("name", "name"), ("basis", "basis"), ("delivery", "delivery"),
            ("effect_channel", "channel"), ("range", "range"), ("timing", "timing"),
            ("cadence", "cadence"), ("target", "target_rule"),
        ):
            if definition[definition_field] != move[move_field]:
                raise EnemyCapabilityPoolError("enemy mechanics disagrees with its definition")
        if snapshot["kit_fingerprint"] != header["fingerprint"]:
            raise EnemyCapabilityPoolError("enemy mechanics belongs to a different frozen kit")
    pools_raw = _mapping(bundle["pools"], "enemy pools")
    _exact_fields(pools_raw, {"world_library", "assigned", "spawn_eligible", "runtime"}, "enemy pools")
    pools = {stage: validate_pool(_mapping(pools_raw[stage], f"{stage} pool")) for stage in pools_raw}
    if any(pools[stage].stage != stage for stage in pools):
        raise EnemyCapabilityPoolError("enemy pool is stored under the wrong stage")
    validate_pool_transition(pools["world_library"], pools["assigned"])
    validate_pool_transition(pools["assigned"], pools["spawn_eligible"])
    validate_pool_transition(pools["spawn_eligible"], pools["runtime"])
    all_refs = {item["fingerprint"] for item in definitions}
    if {member.definition.fingerprint for member in pools["world_library"].members} != all_refs:
        raise EnemyCapabilityPoolError("world library does not contain exactly the frozen kit definitions")
    admission = validate_enemy_hp_receipt_admission(
        _mapping(bundle["receipt_admission"], "receipt admission")
    )
    runtime_by_move = {
        next(snapshot["move"]["id"] for snapshot in mechanics
             if snapshot["definition_ref"] == member.definition.as_dict()): member
        for member in pools["runtime"].members
    }
    if set(runtime_by_move) != set(admission["refs"]):
        raise EnemyCapabilityPoolError("runtime pool does not match explicit receipt admissions")
    for move_id, member in runtime_by_move.items():
        if member.admission_ref != admission["refs"][move_id]:
            raise EnemyCapabilityPoolError("runtime pool admission evidence was forged")
    payload = {key: _plain_copy(item, f"bundle {key}") for key, item in bundle.items() if key != "fingerprint"}
    if bundle["fingerprint"] != _fingerprint_payload(payload):
        raise EnemyCapabilityPoolError("enemy bundle fingerprint does not match its payload")
    return {**payload, "fingerprint": bundle["fingerprint"]}


def reconstruct_enemy_kit(value: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the original kit exactly when the runtime stage retained every candidate."""
    bundle = validate_enemy_capability_bundle(value)
    runtime = validate_pool(bundle["pools"]["runtime"])
    runtime_refs = {member.definition.fingerprint for member in runtime.members}
    mechanics = [
        snapshot for snapshot in bundle["mechanics"]
        if snapshot["definition_ref"]["fingerprint"] in runtime_refs
    ]
    if len(mechanics) != bundle["kit_header"]["move_count"]:
        raise EnemyCapabilityPoolError("a narrowed runtime bundle cannot reconstruct the original full kit")
    mechanics.sort(key=lambda item: item["move_index"])
    if [item["move_index"] for item in mechanics] != list(range(len(mechanics))):
        raise EnemyCapabilityPoolError("enemy mechanics positions cannot reconstruct the original order")
    header = dict(bundle["kit_header"])
    move_count = header.pop("move_count")
    fingerprint = header.pop("fingerprint")
    moves = []
    for item in mechanics:
        move = _plain_copy(item["move"], "reconstructed move")
        if move["reaction"] is None:
            del move["reaction"]
        moves.append(move)
    kit = {**header, "moves": moves}
    if len(kit["moves"]) != move_count or _kit_fingerprint(kit) != fingerprint:
        raise EnemyCapabilityPoolError("runtime mechanics cannot reconstruct the original kit fingerprint")
    return {**kit, "fingerprint": fingerprint}
