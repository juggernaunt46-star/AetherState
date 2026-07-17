"""Strictly replay and project one fenced RPG journal window.

The reducer ledger is authoritative, but an operation request is not proof of what the reducer
actually changed.  This module starts from the immutable fenced pre-state, strictly replays only
the newly journaled operations, requires the exact post-state, and emits typed visible facts from
the observed transitions.  It never parses Player or narrator prose.

The policy registry is intentionally welded to :mod:`aetherstate.state`'s reducer inventory.  A
new reducer operation therefore fails this layer until its visibility and replay policy is named.
"""
from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from . import state as _state_module
from .capability_glossary import content_fingerprint, normalize_phrase
from .state import _SPEC, _apply_op, slug
from .turn_lifecycle import journal_window_fingerprint as lifecycle_journal_window_fingerprint


TRANSITION_PROJECTION_SCHEMA = "semantic-journal-transition-projection/4"
TRANSITION_ENTRY_SCHEMA = "semantic-journal-transition/2"
REDUCER_REPLAY_SCHEMA = "aetherstate-state-reducer-replay/1"


def _reducer_replay_fingerprint() -> str:
    """Bind detached projections to the exact reducer implementation that created them.

    ``_SPEC`` catches inventory drift, but it cannot detect a behavior change inside an existing
    operation.  A detached projection is therefore replayable only by the exact normalized
    reducer source and required-field inventory that produced it.  Source-unavailable builds fail
    closed at import instead of silently accepting an unverifiable historical projection.
    """
    try:
        reducer_source = inspect.getsource(_state_module).replace("\r\n", "\n")
    except (OSError, TypeError) as exc:  # pragma: no cover - packaged-source safety boundary
        raise RuntimeError("semantic transition replay requires exact reducer source") from exc
    return content_fingerprint(
        {
            "schema": REDUCER_REPLAY_SCHEMA,
            "required_fields": {
                kind: sorted(fields)
                for kind, fields in sorted(_SPEC.items())
            },
            "state_reducer_module_source": reducer_source,
        }
    )


REDUCER_REPLAY_FINGERPRINT = _reducer_replay_fingerprint()

SEMANTIC_EVIDENCE_OPS = frozenset(
    {
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
    }
)
SILENT_BOOKKEEPING_OPS = frozenset({"clock_tick", "stagnation"})
SILENT_OPS = SEMANTIC_EVIDENCE_OPS | SILENT_BOOKKEEPING_OPS
# Cold indexing remains deliberately unavailable until the caller can supply the exact persisted
# terminal artifact, complete delivery proof, accepted visible graph, and a code-derived index plan.
# A self-hashed inner receipt is not authority.  Keeping the public set empty makes every current
# operation's directional policy fail closed instead of preserving an attractive unsafe seam.
INDEX_ONLY_POST_DELIVERY_OPS: frozenset[str] = frozenset()
DEFERRED_POST_DELIVERY_INDEX_OPS = frozenset({"memory_event", "reveal_fact"})
EXPLICIT_OUTCOME_OPS = frozenset(
    {
        "mechanic_settlement_commit",
        "hp_adj",
        "enemy_intent_set",
        "defeat_resolve",
        "award_exp",
        "combatant_defeat",
        "combat_end",
    }
)
CONDITIONAL_SETTLEMENT_MEMBERS = frozenset(
    {"check", "combatant_spawn", "scene_set", "combatant_hp", "master_tick", "effect_add"}
)
BOOTSTRAP_SOURCES = frozenset({"genesis", "bootstrap"})
T0_BOOTSTRAP_OPERATION_KINDS = frozenset(
    {"craving", "entity_add", "obsession", "player_seed", "presence"}
)
JOURNAL_SOURCES = frozenset({"user", "genesis", "bootstrap", "rule", "extraction"})

# Deliberately static.  Adding a reducer op must update this audited inventory before the module
# will import; it may not inherit the broad transition policy by accident.
EXPECTED_REDUCER_OPS = frozenset(
    {
        "ability_grant", "affinity_adj", "arousal", "award_exp", "battle_end",
        "battle_start", "battle_wave", "capability_assign", "check", "clash_record",
        "clock_tick", "clothing", "combat_end", "combatant_defeat", "combatant_hp",
        "combatant_spawn", "consent_set", "consent_signal", "contact", "craving",
        "defeat_resolve", "effect_add", "effect_remove", "effect_update", "enemy_intent_set",
        "entity_add", "evolve_def", "fact_retire", "freeze", "front_add", "front_reveal",
        "front_tick", "goal", "hp_adj", "item_consume", "item_equip", "item_gain",
        "item_lose", "item_mint", "item_move", "item_transfer", "item_unequip", "level_up",
        "loot_table", "master_tick", "mechanic_settlement_commit", "memory_event", "mood",
        "move_entity", "obsession", "player_seed", "position", "presence", "quest_add",
        "quest_update", "relationship_adj", "resource_change", "reveal_fact", "roll",
        "route_set", "scene_dial", "scene_mode", "scene_set", "semantic_binding_commit",
        "semantic_frame_commit", "semantic_meaning_commit", "semantic_world_alignment_commit",
        "set_attribute", "set_nemesis", "set_soulmate", "stagnation", "stat_spend",
        "tide_set", "time_advance", "unfreeze", "world_flag", "world_identity_set",
    }
)

_MOVEMENT_OPS = frozenset({"move_entity", "presence", "position", "scene_set"})
_TIME_OPS = frozenset({"time_advance"})
_RESOURCE_OPS = frozenset(
    {
        "ability_grant",
        "check",
        "evolve_def",
        "item_consume",
        "item_equip",
        "item_gain",
        "item_lose",
        "item_mint",
        "item_move",
        "item_transfer",
        "item_unequip",
        "level_up",
        "master_tick",
        "resource_change",
        "roll",
        "stat_spend",
    }
)
_STATUS_OPS = frozenset(
    {
        "affinity_adj",
        "arousal",
        "clothing",
        "consent_set",
        "consent_signal",
        "contact",
        "craving",
        "effect_add",
        "effect_remove",
        "effect_update",
        "freeze",
        "mood",
        "obsession",
        "relationship_adj",
        "scene_dial",
        "set_nemesis",
        "set_soulmate",
        "unfreeze",
    }
)

POLICY_FAMILIES: dict[str, frozenset[str]] = {
    "semantic_evidence": SEMANTIC_EVIDENCE_OPS,
    "atomic_settlement": frozenset({"mechanic_settlement_commit"}),
    "world_capability_authoring": frozenset({"world_identity_set", "capability_assign"}),
    "scene_identity_placement": frozenset(
        {"set_attribute", "move_entity", "presence", "entity_add", "scene_set", "scene_mode"}
    ),
    "embodied_affect_social": frozenset(
        {
            "clothing", "position", "contact", "arousal", "mood", "relationship_adj",
            "scene_dial", "obsession", "craving",
        }
    ),
    "consent_safety": frozenset({"consent_signal", "consent_set", "freeze", "unfreeze"}),
    "fact_memory_goal": frozenset({"reveal_fact", "fact_retire", "memory_event", "goal"}),
    "time_dice_bookkeeping": frozenset({"time_advance", "clock_tick", "roll", "stagnation"}),
    "player_operational": frozenset(
        {
            "check", "resource_change", "award_exp", "level_up", "master_tick",
            "defeat_resolve", "stat_spend",
        }
    ),
    "player_authoring": frozenset({"player_seed", "ability_grant", "evolve_def"}),
    "items": frozenset(
        {
            "item_mint", "item_move", "item_equip", "item_unequip", "item_consume",
            "item_transfer", "item_gain", "item_lose",
        }
    ),
    "effects": frozenset({"effect_add", "effect_remove", "effect_update"}),
    "social_world_standing": frozenset(
        {"affinity_adj", "set_soulmate", "set_nemesis", "world_flag"}
    ),
    "quests": frozenset({"quest_add", "quest_update"}),
    "player_hp": frozenset({"hp_adj"}),
    "operational_combat": frozenset(
        {
            "combatant_spawn", "enemy_intent_set", "combatant_hp", "combatant_defeat",
            "combat_end",
        }
    ),
    "combat_record_authoring": frozenset({"clash_record", "loot_table"}),
    "large_battle": frozenset({"battle_start", "tide_set", "battle_wave", "battle_end"}),
    "living_world": frozenset({"front_add", "front_tick", "front_reveal", "route_set"}),
}


def _domain_family(kind: str) -> str:
    matches = [family for family, kinds in POLICY_FAMILIES.items() if kind in kinds]
    if len(matches) != 1:
        raise RuntimeError(f"reducer operation {kind!r} has {len(matches)} domain families")
    return matches[0]


def _allowed_roots(kind: str) -> frozenset[str]:
    groups: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
        (SEMANTIC_EVIDENCE_OPS, frozenset({
            "semantic_meanings", "semantic_bindings", "semantic_world_alignments",
            "semantic_frames", "meta",
        })),
        (frozenset({"mechanic_settlement_commit"}), frozenset({
            "mechanic_settlements", "_mechanic_settlement_projection_members", "rolls",
            "combat", "scene", "entities", "player", "effects", "capability_assignments",
            "battle", "meta",
        })),
        (frozenset({"world_identity_set"}), frozenset({"world_identity", "meta"})),
        (frozenset({"capability_assign"}), frozenset({"capability_assignments", "meta"})),
        (frozenset({"entity_add", "move_entity", "presence"}), frozenset({"entities", "meta"})),
        (frozenset({"set_attribute"}), frozenset({"attributes", "meta"})),
        (frozenset({"scene_set"}), frozenset({"scene", "entities", "player", "meta"})),
        (frozenset({"scene_mode", "scene_dial", "stagnation"}), frozenset({"scene", "meta"})),
        (frozenset({"clothing"}), frozenset({"clothing", "meta"})),
        (frozenset({"position"}), frozenset({"poses", "scene", "meta"})),
        (frozenset({"contact"}), frozenset({"contacts", "meta"})),
        (frozenset({"arousal", "mood", "obsession", "goal"}), frozenset({"chars", "meta"})),
        (frozenset({"craving"}), frozenset({"chars", "effects", "meta"})),
        (frozenset({"consent_set"}), frozenset({"consent", "meta"})),
        (frozenset({"consent_signal"}), frozenset({
            "consent", "frozen", "frozen_reason", "frozen_turn", "contacts", "poses",
            "scene", "meta",
        })),
        (frozenset({"freeze", "unfreeze"}), frozenset({
            "frozen", "frozen_reason", "frozen_turn", "contacts", "poses", "scene", "meta",
        })),
        (frozenset({"relationship_adj"}), frozenset({"relationships", "meta"})),
        (frozenset({"reveal_fact"}), frozenset({"facts", "beliefs", "meta"})),
        (frozenset({"fact_retire"}), frozenset({"facts", "meta"})),
        (frozenset({"memory_event"}), frozenset({"memories", "meta"})),
        (frozenset({"time_advance"}), frozenset({"clock", "player", "chars", "effects", "meta"})),
        (frozenset({"clock_tick"}), frozenset({"clock", "meta"})),
        (frozenset({"roll", "check"}), frozenset({"rolls", "player", "meta"})),
        (frozenset({"resource_change", "award_exp", "level_up", "master_tick", "stat_spend"}), frozenset({"player", "meta"})),
        (frozenset({"player_seed", "ability_grant", "evolve_def"}), frozenset({"player", "meta"})),
        (frozenset({
            "item_mint", "item_move", "item_equip", "item_unequip", "item_consume",
            "item_transfer", "item_gain", "item_lose",
        }), frozenset({"items", "gear", "inventory", "player", "meta"})),
        (frozenset({"effect_add", "effect_remove", "effect_update"}), frozenset({"effects", "meta"})),
        (frozenset({"affinity_adj"}), frozenset({"affinity", "factions", "meta"})),
        (frozenset({"set_soulmate", "set_nemesis"}), frozenset({"player", "affinity", "meta"})),
        (frozenset({"world_flag"}), frozenset({"world", "factions", "meta"})),
        (frozenset({"quest_add", "quest_update"}), frozenset({"quests", "meta"})),
        (frozenset({"hp_adj"}), frozenset({"player", "combat", "meta"})),
        (frozenset({"defeat_resolve"}), frozenset({"player", "items", "inventory", "gear", "effects", "meta"})),
        (frozenset({"combatant_spawn"}), frozenset({"combat", "battle", "capability_assignments", "meta"})),
        (frozenset({"enemy_intent_set", "combatant_hp"}), frozenset({"combat", "meta"})),
        (frozenset({"combatant_defeat"}), frozenset({"combat", "items", "meta"})),
        (frozenset({"combat_end"}), frozenset({"combat", "attributes", "effects", "meta"})),
        (frozenset({"clash_record"}), frozenset({"clashes", "facts", "meta"})),
        (frozenset({"loot_table"}), frozenset({"loot", "meta"})),
        (frozenset({"battle_start", "tide_set", "battle_wave", "battle_end"}), frozenset({"battle", "meta"})),
        (frozenset({"front_add", "front_tick", "front_reveal"}), frozenset({"fronts", "meta"})),
        (frozenset({"route_set"}), frozenset({"routes", "meta"})),
    )
    matches = [roots for kinds, roots in groups if kind in kinds]
    if len(matches) != 1:
        raise RuntimeError(f"reducer operation {kind!r} has {len(matches)} path policies")
    return matches[0]


def _policy_inventory() -> dict[str, dict[str, Any]]:
    if frozenset(_SPEC) != EXPECTED_REDUCER_OPS:
        missing = sorted(EXPECTED_REDUCER_OPS - frozenset(_SPEC))
        added = sorted(frozenset(_SPEC) - EXPECTED_REDUCER_OPS)
        raise RuntimeError(
            f"semantic transition policy inventory drifted; missing={missing}, added={added}"
        )
    policies: dict[str, dict[str, Any]] = {}
    for kind in EXPECTED_REDUCER_OPS:
        subject_candidates = {
            "subject", "char", "entity", "owner", "from_char", "to_char", "target",
            "learner", "combatant", "front",
        }
        policies[kind] = {
            "kind": kind,
            "family": _domain_family(kind),
            "priority": "internal" if kind in SILENT_OPS else "p0",
            "visibility": "internal" if kind in SILENT_OPS else "required",
            "subject_fields": sorted(subject_candidates & set(_SPEC[kind])),
            "actor_source": "typed_operation_actor_fields",
            "cause_source": "semantic_frame_settlement_or_transition_identity",
            "value_source": "strict_pre_post_leaf_delta",
            "pre_post_proof": "strict_reducer_replay_exact_post_state",
            "post_delivery": "refuse",
            "allowed_roots": sorted(_allowed_roots(kind)),
        }
    return dict(sorted(policies.items()))


TRANSITION_POLICIES = _policy_inventory()

_PROJECTION_FIELDS = {
    "schema",
    "reducer_schema",
    "reducer_fingerprint",
    "branch_id",
    "turn_index",
    "delivery_phase",
    "pre_state",
    "pre_ledger_hash",
    "post_ledger_hash",
    "journal_window_fingerprint",
    "journal_rows",
    "entries",
    "transitions",
    "required_facts",
    "allowed_facts",
    "metadata_receipts",
    "fingerprint",
}
_ENTRY_FIELDS = {
    "schema",
    "branch_id",
    "turn_index",
    "journal_id",
    "op_index",
    "source",
    "op_kind",
    "operation",
    "op_fingerprint",
    "before_fingerprint",
    "after_fingerprint",
    "changed",
    "status",
    "visibility",
    "subjects",
    "actor_id",
    "cause_ref",
    "covered_paths",
    "facts",
    "effects",
    "entry_ref",
    "construction_ref",
}
_TRANSITION_FACT_FIELDS = {"subject_id", "kind", "detail", "amount"}
_TRANSITION_EFFECT_FIELDS = {"path", "before", "after", "delta", "visibility"}
_REQUIRED_FACT_FIELDS = {
    "fact_ref",
    "cause_ref",
    "construction_ref",
    "subject_id",
    "effects",
}
_CLAIM_KINDS = frozenset(
    {"opposition_action", "harm", "defeat", "status", "resource", "time", "movement", "world"}
)


class SemanticTransitionTruthError(ValueError):
    """A fenced journal window cannot prove its exact reducer transition or visible truth."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SemanticTransitionTruthError(f"{label} must be an object with string fields")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticTransitionTruthError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise SemanticTransitionTruthError(f"{label} must be at least {minimum}")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise SemanticTransitionTruthError(f"{label} must be a sha256 fingerprint")
    return value


def _canonical_equal(left: object, right: object) -> bool:
    """Compare typed finite JSON truth, never Python's coercive value equality.

    Python deliberately considers ``True == 1``, ``False == 0``, and ``1 == 1.0``.
    Those aliases are unacceptable at a replay boundary: journal payloads, reducer effects, and
    duplicated projection views must remain byte-semantically exact.  ``content_fingerprint`` is
    the project's canonical finite-JSON encoding, so it also preserves the existing rejection of
    NaN, infinity, and non-JSON values.
    """
    return content_fingerprint(left) == content_fingerprint(right)


def _safe_words(value: object, *, fallback: str, limit: int = 96) -> str:
    text = normalize_phrase(str(value or ""))
    text = re.sub(r"[^a-z0-9 .:/_-]+", " ", text)
    text = " ".join(text.split())[:limit].strip(" .:/_-")
    return text or fallback


def _assert_operation_shape(operation: Mapping[str, Any], kind: str) -> None:
    """Enforce the reducer inventory's stable journal shape without re-running live admission.

    Journal replay can include historically admitted values that a later live validator has made
    narrower, so detached proof must not call mutable ``validate_op``.  It does, however, require
    the exact operation identity, every reducer-required field, and an explicit integer turn.
    """
    if operation.get("op") != kind or not _SPEC[kind].issubset(operation):
        raise SemanticTransitionTruthError(
            f"journal operation {kind!r} does not satisfy its reducer-required shape"
        )
    explicit_turn = operation.get("_turn")
    if isinstance(explicit_turn, bool) or not isinstance(explicit_turn, int):
        raise SemanticTransitionTruthError(
            f"journal operation {kind!r} lacks one explicit integer turn"
        )


def _subject_from_op(op: Mapping[str, Any]) -> str:
    subject = op.get("subject")
    if isinstance(subject, Mapping):
        for field in ("subject_id", "id", "ref"):
            if isinstance(subject.get(field), str) and subject[field].strip():
                return subject[field].strip()
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    for field in (
        "char",
        "entity",
        "owner",
        "from_char",
        "target",
        "learner",
        "combatant",
        "front",
    ):
        value = op.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "world"


def _numeric_delta(before: object, after: object) -> int | None:
    if any(isinstance(value, bool) for value in (before, after)):
        return None
    if isinstance(before, int) and isinstance(after, int):
        return after - before
    return None


_MISSING = object()
_MISSING_VALUE = {"schema": "semantic-missing-value/1"}


def _pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _leaf_values(value: object, path: str = "") -> dict[str, object]:
    if isinstance(value, Mapping):
        if not value:
            return {path or "/": {}}
        rows: dict[str, object] = {}
        for key in sorted(value, key=str):
            rows.update(_leaf_values(value[key], f"{path}/{_pointer_token(key)}"))
        return rows
    if isinstance(value, list):
        if not value:
            return {path or "/": []}
        rows = {}
        for index, item in enumerate(value):
            rows.update(_leaf_values(item, f"{path}/{index}"))
        return rows
    return {path or "/": deepcopy(value)}


def _changed_leaf_rows(before: object, after: object) -> list[dict[str, Any]]:
    left = _leaf_values(before)
    right = _leaf_values(after)
    rows: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        pre = left.get(path, _MISSING)
        post = right.get(path, _MISSING)
        if pre is not _MISSING and post is not _MISSING and _canonical_equal(pre, post):
            continue
        before_value = deepcopy(_MISSING_VALUE if pre is _MISSING else pre)
        after_value = deepcopy(_MISSING_VALUE if post is _MISSING else post)
        rows.append(
            {
                "path": path,
                "before": before_value,
                "after": after_value,
                "delta": (
                    _numeric_delta(pre, post)
                    if pre is not _MISSING and post is not _MISSING
                    else None
                ),
            }
        )
    return rows


def _changed_paths(before: object, after: object) -> list[str]:
    return [row["path"] for row in _changed_leaf_rows(before, after)]


def _assert_covered_paths(kind: str, changed_paths: Sequence[str]) -> None:
    allowed = set(TRANSITION_POLICIES[kind]["allowed_roots"])
    uncovered = sorted(
        path
        for path in changed_paths
        if path.lstrip("/").split("/", 1)[0].replace("~1", "/").replace("~0", "~")
        not in allowed
    )
    if uncovered:
        raise SemanticTransitionTruthError(
            f"journal operation {kind!r} changed uncovered state paths {uncovered}"
        )


def _assert_world_flag_effect_binding(
    operation: Mapping[str, Any],
    effects: Sequence[Mapping[str, Any]],
    *,
    changed: bool,
) -> None:
    """Bind detached ``world_flag`` authority to its exact reducer leaf transition.

    A domain-level root policy is not enough for this operation: two different flag keys share the
    same ``world``/``factions`` roots.  Detached validation therefore derives the reducer's exact
    slugged target path and verifies the set/delete value plus the only structural, bounded-eviction,
    and turn effects that the reducer can produce.  A changed operation without a target-leaf effect
    fails closed because the detached artifact cannot prove an idempotent set or absent-key clear.
    """
    if operation.get("op") != "world_flag" or not changed:
        return

    by_path: dict[str, Mapping[str, Any]] = {}
    for effect in effects:
        path = str(effect["path"])
        if path in by_path:
            raise SemanticTransitionTruthError(
                "world_flag operation is not bound to one exact reducer effect per path"
            )
        by_path[path] = effect
        expected_delta = _numeric_delta(effect["before"], effect["after"])
        if effect["delta"] != expected_delta:
            raise SemanticTransitionTruthError(
                "world_flag operation effect delta is not exact reducer truth"
            )

    turn_effect = by_path.pop("/meta/turn", None)
    if turn_effect is not None:
        before_turn = turn_effect["before"]
        exact_turn = operation["_turn"]
        if (
            isinstance(before_turn, bool)
            or not isinstance(before_turn, int)
            or before_turn >= exact_turn
            or isinstance(turn_effect["after"], bool)
            or not isinstance(turn_effect["after"], int)
            or turn_effect["after"] != exact_turn
        ):
            raise SemanticTransitionTruthError(
                "world_flag operation is not bound to its exact reducer turn effect"
            )

    key = _pointer_token(slug(operation["key"]))
    faction = operation.get("faction")
    value = operation["value"]
    if faction:
        faction_token = _pointer_token(faction)
        faction_root = f"/factions/{faction_token}"
        circumstances_root = f"{faction_root}/circumstances"
        target_path = f"{circumstances_root}/{key}"
        name_path = f"{faction_root}/name"
        allowed_structural_paths = {"/factions", circumstances_root}
    else:
        circumstances_root = "/world"
        target_path = f"{circumstances_root}/{key}"
        name_path = None
        allowed_structural_paths = {circumstances_root}

    target_effect = by_path.get(target_path)
    if target_effect is None:
        raise SemanticTransitionTruthError(
            "world_flag operation is not bound to its exact target leaf effect"
        )
    if value is None:
        if target_effect["after"] != _MISSING_VALUE \
                or target_effect["before"] == _MISSING_VALUE:
            raise SemanticTransitionTruthError(
                "world_flag clear is not bound to its exact target deletion effect"
            )
    elif content_fingerprint(target_effect["after"]) != content_fingerprint(value) \
            or content_fingerprint(target_effect["before"]) == content_fingerprint(
                target_effect["after"]
            ):
        raise SemanticTransitionTruthError(
            "world_flag set is not bound to its exact target value effect"
        )

    for path, effect in by_path.items():
        if path == target_path:
            continue
        before = effect["before"]
        after = effect["after"]
        if path in allowed_structural_paths:
            added_children = before == {} and after == _MISSING_VALUE
            removed_children = before == _MISSING_VALUE and after == {}
            if not (added_children or removed_children):
                raise SemanticTransitionTruthError(
                    "world_flag operation has an inexact structural leaf effect"
                )
            if path == "/factions" and not added_children:
                raise SemanticTransitionTruthError(
                    "world_flag operation cannot remove the factions root"
                )
            continue
        if name_path is not None and path == name_path:
            if before != _MISSING_VALUE or after == _MISSING_VALUE:
                raise SemanticTransitionTruthError(
                    "world_flag faction name setup is not an exact reducer effect"
                )
            continue
        if path.startswith(f"{circumstances_root}/"):
            if value is None or before == _MISSING_VALUE or after != _MISSING_VALUE:
                raise SemanticTransitionTruthError(
                    "world_flag operation contains an unrelated flag effect"
                )
            # Setting a new value may evict older siblings from the reducer's bounded mapping.
            continue
        raise SemanticTransitionTruthError(
            "world_flag operation contains an effect outside its exact reducer scope"
        )


def _sparse_transition_states(
    effects: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the changed mapping leaves needed to rederive an operation's primary fact.

    Full pre/post states remain outside the detached projection by design.  Each changed leaf is
    present, however, so operation-specific fact construction can be checked without trusting the
    supplied fact text. Numeric JSON-pointer tokens are kept as mapping keys; primary fact rules
    consume the named ledger mappings and do not depend on unchanged list structure.
    """

    def set_leaf(target: dict[str, Any], pointer: str, value: object) -> None:
        if value == _MISSING_VALUE:
            return
        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer.lstrip("/").split("/")
            if token
        ]
        if not tokens:
            return
        cursor = target
        for token in tokens[:-1]:
            child = cursor.get(token)
            if not isinstance(child, dict):
                child = {}
                cursor[token] = child
            cursor = child
        cursor[tokens[-1]] = deepcopy(value)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for effect in effects:
        set_leaf(before, str(effect["path"]), effect["before"])
        set_leaf(after, str(effect["path"]), effect["after"])
    return before, after


def _transition_effects(
    before: Mapping[str, Any], after: Mapping[str, Any], *, visibility: str
) -> list[dict[str, Any]]:
    return [
        {**row, "visibility": visibility}
        for row in _changed_leaf_rows(before, after)
    ]


def _player_card(state: Mapping[str, Any], player_id: str) -> Mapping[str, Any]:
    players = state.get("player")
    value = players.get(player_id) if isinstance(players, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _nested_numeric_delta(
    before: Mapping[str, Any], after: Mapping[str, Any], *path: str
) -> int | None:
    left: object = before
    right: object = after
    for key in path:
        left = left.get(key) if isinstance(left, Mapping) else None
        right = right.get(key) if isinstance(right, Mapping) else None
    return _numeric_delta(left, right)


def _effect(subject_id: str, kind: str, detail: str, amount: int | None = None) -> dict[str, Any]:
    return {
        "subject_id": subject_id or "world",
        "kind": kind,
        "detail": _safe_words(detail, fallback="settled change"),
        "amount": amount,
    }


def _primary_effect(
    op: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    kind = str(op["op"])
    subject = _subject_from_op(op)

    if kind == "freeze":
        return _effect("world", "world", f"session frozen {_safe_words(op.get('reason'), fallback='safety')}")
    if kind == "unfreeze":
        return _effect("world", "world", "session resumed")
    if kind == "presence":
        return _effect(subject, "world", "present" if bool(op.get("present")) else "absent")
    if kind in {"move_entity", "scene_set"}:
        destination = op.get("to_location") if kind == "move_entity" else op.get("location")
        return _effect(subject if kind == "move_entity" else "world", "movement", _safe_words(destination, fallback="scene changed"))
    if kind == "time_advance":
        before_clock = before.get("clock") if isinstance(before.get("clock"), Mapping) else {}
        after_clock = after.get("clock") if isinstance(after.get("clock"), Mapping) else {}
        amount = _numeric_delta(before_clock.get("minutes"), after_clock.get("minutes"))
        detail = _safe_words(after_clock.get("time_of_day"), fallback="time advanced")
        return _effect("world", "time", detail, amount)
    if kind == "resource_change":
        raw_rid = str(op.get("resource") or "")
        rid = _safe_words(raw_rid, fallback="resource")
        pre = _player_card(before, subject).get("resources")
        post = _player_card(after, subject).get("resources")
        pre_pool = pre.get(raw_rid) if isinstance(pre, Mapping) else None
        post_pool = post.get(raw_rid) if isinstance(post, Mapping) else None
        amount = _numeric_delta(
            pre_pool.get("cur") if isinstance(pre_pool, Mapping) else None,
            post_pool.get("cur") if isinstance(post_pool, Mapping) else None,
        )
        return _effect(subject, "resource", rid, amount)
    if kind == "level_up":
        amount = _numeric_delta(
            _player_card(before, subject).get("level"),
            _player_card(after, subject).get("level"),
        )
        return _effect(subject, "resource", "level", amount)
    if kind == "master_tick":
        skill = str(op.get("skill") or "")
        amount = _nested_numeric_delta(
            _player_card(before, subject), _player_card(after, subject), "mastery", skill
        )
        return _effect(
            subject,
            "resource",
            f"mastery {_safe_words(skill, fallback='skill')}",
            amount,
        )
    if kind == "stat_spend":
        return _effect(subject, "resource", f"stat {_safe_words(op.get('stat'), fallback='attribute')}", 1)
    if kind == "roll":
        return _effect(subject, "world", f"roll {_safe_words(op.get('spec'), fallback='resolved')} resolved")
    if kind == "check":
        return _effect(subject, "world", f"{_safe_words(op.get('skill'), fallback='check')} {_safe_words(op.get('tier'), fallback='resolved')}")
    if kind in {"effect_add", "effect_remove", "effect_update"}:
        action = {"effect_add": "applied", "effect_remove": "removed", "effect_update": "updated"}[kind]
        return _effect(subject, "status", f"{_safe_words(op.get('effect'), fallback='effect')} {action}")
    if kind in _STATUS_OPS:
        detail_parts = [kind.replace("_", " ")]
        for field in ("category", "dimension", "dial", "substance", "target", "level", "signal"):
            if op.get(field) is not None:
                detail_parts.append(_safe_words(op[field], fallback=field))
        amount = None
        if kind == "affinity_adj":
            pre_affinity = before.get("affinity") if isinstance(before.get("affinity"), Mapping) else {}
            post_affinity = after.get("affinity") if isinstance(after.get("affinity"), Mapping) else {}
            keys = sorted(set(pre_affinity) | set(post_affinity))
            changed = [
                key
                for key in keys
                if _nested_numeric_delta(pre_affinity, post_affinity, key, "value") not in {None, 0}
            ]
            if len(changed) == 1:
                amount = _nested_numeric_delta(
                    pre_affinity, post_affinity, changed[0], "value"
                )
        elif kind == "relationship_adj":
            relation = f"{op.get('from_char')}->{op.get('to_char')}"
            dimension = str(op.get("dimension") or "")
            amount = _nested_numeric_delta(
                before, after, "relationships", relation, "dims", dimension
            )
        return _effect(subject, "status", " ".join(detail_parts), amount)
    if kind in _RESOURCE_OPS:
        detail = kind.replace("_", " ")
        for field in ("ability", "id", "instance", "name", "template", "slot"):
            if op.get(field) is not None:
                detail = f"{detail} {_safe_words(op[field], fallback=field)}"
                break
        return _effect(subject, "resource", detail)
    if kind == "battle_start":
        return _effect("world", "world", f"battle started {_safe_words(op.get('name'), fallback='battle')}")
    if kind == "battle_wave":
        battle = after.get("battle") if isinstance(after.get("battle"), Mapping) else {}
        return _effect("world", "world", f"battle wave {_safe_words(battle.get('waves'), fallback='advanced')}")
    if kind == "battle_end":
        return _effect("world", "world", f"battle ended {_safe_words(op.get('outcome'), fallback='resolved')}")
    if kind == "tide_set":
        return _effect("world", "world", f"battle tide {_safe_words(op.get('tide'), fallback='changed')}")
    if kind == "front_tick":
        return _effect("world", "world", f"front {_safe_words(op.get('front'), fallback='unknown')} advanced")
    if kind == "front_reveal":
        return _effect("world", "world", f"front {_safe_words(op.get('front'), fallback='unknown')} revealed")
    if kind == "front_add":
        return _effect("world", "world", f"front added {_safe_words(op.get('name'), fallback='front')}")
    if kind == "world_flag":
        scope = _safe_words(op.get("faction"), fallback="world")
        value = _safe_words(op.get("value"), fallback="cleared")
        return _effect(scope if scope != "world" else "world", "world", f"flag {_safe_words(op.get('key'), fallback='state')} {value}")
    if kind in _MOVEMENT_OPS:
        return _effect(subject, "movement", kind.replace("_", " "))
    if kind in _TIME_OPS:
        return _effect(subject, "time", kind.replace("_", " "))
    return _effect(subject, "world", f"{kind.replace('_', ' ')} settled")


def _secondary_player_effects(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pre_players = before.get("player") if isinstance(before.get("player"), Mapping) else {}
    post_players = after.get("player") if isinstance(after.get("player"), Mapping) else {}
    for player_id in sorted(set(pre_players) | set(post_players)):
        pre = pre_players.get(player_id) if isinstance(pre_players.get(player_id), Mapping) else {}
        post = post_players.get(player_id) if isinstance(post_players.get(player_id), Mapping) else {}
        for field, detail in (("xp", "experience"), ("level", "level"), ("stat_points", "stat points")):
            delta = _numeric_delta(pre.get(field), post.get(field))
            if delta:
                rows.append(_effect(player_id, "resource", detail, delta))
        pre_hp = pre.get("hp") if isinstance(pre.get("hp"), Mapping) else {}
        post_hp = post.get("hp") if isinstance(post.get("hp"), Mapping) else {}
        for field, detail in (("cur", "hp"), ("max", "hp capacity")):
            delta = _numeric_delta(pre_hp.get(field), post_hp.get(field))
            if delta:
                rows.append(_effect(player_id, "resource", detail, delta))
        pre_resources = pre.get("resources") if isinstance(pre.get("resources"), Mapping) else {}
        post_resources = post.get("resources") if isinstance(post.get("resources"), Mapping) else {}
        for rid in sorted(set(pre_resources) | set(post_resources)):
            pre_pool = pre_resources.get(rid) if isinstance(pre_resources.get(rid), Mapping) else {}
            post_pool = post_resources.get(rid) if isinstance(post_resources.get(rid), Mapping) else {}
            for field, suffix in (("cur", ""), ("max", " capacity")):
                delta = _numeric_delta(pre_pool.get(field), post_pool.get(field))
                if delta:
                    rows.append(_effect(player_id, "resource", f"{rid}{suffix}", delta))
    return rows


def _secondary_entity_effects(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pre_entities = before.get("entities") if isinstance(before.get("entities"), Mapping) else {}
    post_entities = after.get("entities") if isinstance(after.get("entities"), Mapping) else {}
    for entity_id in sorted(set(pre_entities) | set(post_entities)):
        pre = pre_entities.get(entity_id) if isinstance(pre_entities.get(entity_id), Mapping) else {}
        post = post_entities.get(entity_id) if isinstance(post_entities.get(entity_id), Mapping) else {}
        if not _canonical_equal(pre.get("location_id"), post.get("location_id")) \
                and post.get("location_id") is not None:
            rows.append(_effect(entity_id, "movement", _safe_words(post.get("location_id"), fallback="location changed")))
        if not _canonical_equal(pre.get("present"), post.get("present")) \
                and isinstance(post.get("present"), bool):
            rows.append(_effect(entity_id, "world", "present" if post["present"] else "absent"))
    return rows


def _secondary_effect_ledger(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pre_all = before.get("effects") if isinstance(before.get("effects"), Mapping) else {}
    post_all = after.get("effects") if isinstance(after.get("effects"), Mapping) else {}
    for subject in sorted(set(pre_all) | set(post_all)):
        pre = pre_all.get(subject) if isinstance(pre_all.get(subject), Mapping) else {}
        post = post_all.get(subject) if isinstance(post_all.get(subject), Mapping) else {}
        for effect_id in sorted(set(pre) | set(post)):
            if _canonical_equal(pre.get(effect_id), post.get(effect_id)):
                continue
            action = "removed" if effect_id not in post else "applied" if effect_id not in pre else "updated"
            record = post.get(effect_id) if isinstance(post.get(effect_id), Mapping) else pre.get(effect_id)
            label = record.get("name") if isinstance(record, Mapping) else effect_id
            rows.append(_effect(subject, "status", f"{_safe_words(label, fallback=effect_id)} {action}"))
    return rows


def _dedupe_effects(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (row["subject_id"], row["kind"], row["detail"], row.get("amount"))
        unique.setdefault(key, dict(row))
    return [unique[key] for key in sorted(unique, key=lambda item: tuple(str(part) for part in item))]


def _facts_for_transition(
    op: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return _dedupe_effects(
        [
            _primary_effect(op, before, after),
            *_secondary_player_effects(before, after),
            *_secondary_entity_effects(before, after),
            *_secondary_effect_ledger(before, after),
        ]
    )


def _facts_for_operation_effects(
    op: Mapping[str, Any], effects: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Derive the one canonical visible fact set from an operation and its exact leaf effects.

    Detached proof does not retain whole ledger states.  Fact authority therefore cannot depend on
    unchanged state that is absent from the proof: both construction and detached validation use
    the same sparse pre/post states reconstructed from the exact changed leaves.  A time advance
    with no changed minute leaf canonically reports a zero-minute delta rather than leaving the
    result dependent on whether an historical ledger happened to store an unchanged clock value.
    """
    before, after = _sparse_transition_states(effects)
    primary = _primary_effect(op, before, after)
    if op.get("op") == "time_advance" and primary.get("amount") is None:
        primary = {**primary, "amount": 0}
    return _dedupe_effects(
        [
            primary,
            *_secondary_player_effects(before, after),
            *_secondary_entity_effects(before, after),
            *_secondary_effect_ledger(before, after),
        ]
    )


def _journal_window(value: object, *, turn_index: int) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SemanticTransitionTruthError("journal_rows must be an exact ordered sequence")
    rows: list[dict[str, Any]] = []
    prior_id = -1
    for index, raw in enumerate(value):
        row = _mapping(raw, f"journal_rows[{index}]")
        if set(row) != {"id", "turn_lo", "turn_hi", "source", "ops"}:
            raise SemanticTransitionTruthError(f"journal_rows[{index}] shape is invalid")
        row_id = _integer(row["id"], f"journal_rows[{index}].id", minimum=0)
        if row_id <= prior_id:
            raise SemanticTransitionTruthError("journal window IDs must be unique and increasing")
        prior_id = row_id
        lo = _integer(row["turn_lo"], f"journal_rows[{index}].turn_lo")
        hi = _integer(row["turn_hi"], f"journal_rows[{index}].turn_hi")
        if lo > hi or not lo <= turn_index <= hi:
            raise SemanticTransitionTruthError("journal window row does not cover the fenced turn")
        source = row["source"]
        if source not in JOURNAL_SOURCES:
            raise SemanticTransitionTruthError(f"journal source {source!r} is unsupported")
        if source in BOOTSTRAP_SOURCES and (
            turn_index != 0 or lo != 0 or hi != 0
        ):
            raise SemanticTransitionTruthError(
                "bootstrap journal authority is restricted to the exact T0 window"
            )
        if not isinstance(row["ops"], list):
            raise SemanticTransitionTruthError("journal row ops must be a list")
        ops: list[dict[str, Any]] = []
        for op_index, raw_op in enumerate(row["ops"]):
            op = dict(_mapping(raw_op, f"journal_rows[{index}].ops[{op_index}]"))
            kind = op.get("op")
            if kind not in TRANSITION_POLICIES:
                raise SemanticTransitionTruthError(f"journal operation {kind!r} has no policy")
            if op.get("_turn", turn_index) != turn_index:
                raise SemanticTransitionTruthError("journal operation belongs to another turn")
            _assert_operation_shape(op, str(kind))
            if source in BOOTSTRAP_SOURCES and (
                kind not in T0_BOOTSTRAP_OPERATION_KINDS or op.get("_turn") != 0
            ):
                raise SemanticTransitionTruthError(
                    "bootstrap source cannot authorize this operation or an implicit turn"
                )
            ops.append(deepcopy(op))
        rows.append(
            {"id": row_id, "turn_lo": lo, "turn_hi": hi, "source": source, "ops": ops}
        )
    return rows


def _validate_settlement_occurrence_relations(
    rows: Sequence[Mapping[str, Any]], *, turn_index: int
) -> None:
    """Require one complete current wrapper and exact indexed view for every V3 member."""
    wrappers: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    children: dict[str, dict[int, list[tuple[int, int, dict[str, Any]]]]] = {}
    unframed_standalone: list[tuple[int, int, dict[str, Any]]] = []
    modern_standalone: list[tuple[int, int, dict[str, Any]]] = []

    for row_index, row in enumerate(rows):
        source = row["source"]
        for op_index, raw_operation in enumerate(row["ops"]):
            operation = deepcopy(dict(raw_operation))
            kind = operation.get("op")
            is_wrapper = kind == "mechanic_settlement_commit"
            has_ref = "_settlement_ref" in operation
            has_index = "_settlement_member_index" in operation

            if not is_wrapper and not has_ref and not has_index:
                if kind not in CONDITIONAL_SETTLEMENT_MEMBERS:
                    continue
                if "_semantic_frame_ref" in operation:
                    modern_standalone.append((row_index, op_index, operation))
                elif source == "rule" and operation.get("_turn") == turn_index:
                    unframed_standalone.append((row_index, op_index, operation))
                continue

            if source != "rule" or operation.get("_turn") != turn_index:
                raise SemanticTransitionTruthError(
                    "current mechanic settlement rows must come from the exact rule turn"
                )
            if is_wrapper:
                if has_ref or has_index:
                    raise SemanticTransitionTruthError(
                        "mechanic settlement wrapper cannot also be an indexed member"
                    )
                settlement_ref = operation.get("settlement_ref")
                if not isinstance(settlement_ref, str) or not settlement_ref:
                    raise SemanticTransitionTruthError(
                        "mechanic settlement wrapper lacks an exact settlement_ref"
                    )
                wrappers.setdefault(settlement_ref, []).append(
                    (row_index, op_index, operation)
                )
                continue

            if has_ref != has_index:
                raise SemanticTransitionTruthError(
                    "mechanic settlement member needs both reference and member index"
                )
            settlement_ref = operation.get("_settlement_ref")
            member_index = operation.get("_settlement_member_index")
            if not isinstance(settlement_ref, str) or not settlement_ref:
                raise SemanticTransitionTruthError(
                    "mechanic settlement member reference is invalid"
                )
            if isinstance(member_index, bool) or not isinstance(member_index, int) \
                    or member_index < 0:
                raise SemanticTransitionTruthError(
                    "mechanic settlement member index is invalid"
                )
            if kind not in CONDITIONAL_SETTLEMENT_MEMBERS:
                raise SemanticTransitionTruthError(
                    f"operation {kind!r} cannot be a mechanic settlement member"
                )
            children.setdefault(settlement_ref, {}).setdefault(member_index, []).append(
                (row_index, op_index, operation)
            )

    if modern_standalone:
        raise SemanticTransitionTruthError(
            "current V3 mechanic member requires one complete indexed mechanic settlement"
        )

    all_refs = set(wrappers) | set(children)
    for settlement_ref in sorted(all_refs):
        wrapper_rows = wrappers.get(settlement_ref, [])
        if len(wrapper_rows) != 1:
            raise SemanticTransitionTruthError(
                "mechanic settlement window needs exactly one wrapper per settlement_ref"
            )
        wrapper_row_index, wrapper_op_index, wrapper = wrapper_rows[0]
        members = wrapper.get("members")
        if not isinstance(members, list) or any(
            not isinstance(member, Mapping) for member in members
        ):
            raise SemanticTransitionTruthError(
                "mechanic settlement wrapper members must be an exact list"
            )
        if any(
            "_settlement_ref" in member or "_settlement_member_index" in member
            for member in members
        ):
            raise SemanticTransitionTruthError(
                "mechanic settlement wrapper contains an indexed nested member"
            )
        indexed = children.get(settlement_ref, {})
        if set(indexed) != set(range(len(members))):
            raise SemanticTransitionTruthError(
                "mechanic settlement members are missing, gapped, or extra"
            )
        for member_index, member in enumerate(members):
            observed = indexed[member_index]
            if len(observed) != 1:
                raise SemanticTransitionTruthError(
                    "mechanic settlement member occurrence is duplicated"
                )
            child_row_index, child_op_index, child = observed[0]
            if child_row_index != wrapper_row_index or child_op_index <= wrapper_op_index:
                raise SemanticTransitionTruthError(
                    "mechanic settlement member cannot use a detached wrapper"
                )
            expected = {
                **deepcopy(dict(member)),
                "_settlement_ref": settlement_ref,
                "_settlement_member_index": member_index,
            }
            if not _canonical_equal(child, expected):
                raise SemanticTransitionTruthError(
                    "mechanic settlement member kind, index, or payload is not exact"
                )

    if all_refs and unframed_standalone:
        raise SemanticTransitionTruthError(
            "current mechanic settlement window cannot include an unindexed standalone member"
        )


def project_journal_transitions(
    *,
    pre_state: object,
    post_state: object,
    journal_rows: object,
    branch_id: str,
    turn_index: int,
    pre_ledger_hash: str,
    post_ledger_hash: str,
    journal_window_fingerprint: str | None = None,
    realization: object = None,
    delivery_phase: str = "pre_display",
    typed_gate_receipt: object = None,
) -> dict[str, Any]:
    """Strictly replay one exact fenced journal window and project its visible transitions."""
    if set(TRANSITION_POLICIES) != set(_SPEC):
        raise SemanticTransitionTruthError("transition policy inventory differs from reducer inventory")
    if not isinstance(branch_id, str) or not branch_id.strip():
        raise SemanticTransitionTruthError("branch_id must be non-empty text")
    turn = _integer(turn_index, "turn_index", minimum=0)
    pre = deepcopy(dict(_mapping(pre_state, "pre_state")))
    post = deepcopy(dict(_mapping(post_state, "post_state")))
    pre_hash = _fingerprint(pre_ledger_hash, "pre_ledger_hash")
    post_hash = _fingerprint(post_ledger_hash, "post_ledger_hash")
    if content_fingerprint(pre) != pre_hash or content_fingerprint(post) != post_hash:
        raise SemanticTransitionTruthError("fenced ledger hashes do not match supplied states")
    rows = _journal_window(journal_rows, turn_index=turn)
    _validate_settlement_occurrence_relations(rows, turn_index=turn)
    observed_window_fingerprint = lifecycle_journal_window_fingerprint(branch_id, rows)
    if journal_window_fingerprint is not None and _fingerprint(
        journal_window_fingerprint, "journal_window_fingerprint"
    ) != observed_window_fingerprint:
        raise SemanticTransitionTruthError(
            "fenced journal window fingerprint does not match supplied rows"
        )

    if delivery_phase != "pre_display":
        raise SemanticTransitionTruthError(
            "post-delivery indexing is disabled until an exact persisted terminal proof and "
            "accepted-graph-derived index plan are supplied"
        )
    if typed_gate_receipt is not None:
        raise SemanticTransitionTruthError("pre-display projection cannot consume a delivery receipt")
    del realization  # reserved for exact frame/actor attribution without granting prose authority

    cursor = deepcopy(pre)
    entries: list[dict[str, Any]] = []
    for row in rows:
        for op_index, op in enumerate(row["ops"]):
            before = deepcopy(cursor)
            try:
                _apply_op(cursor, deepcopy(op))
            except Exception as exc:
                raise SemanticTransitionTruthError(
                    f"strict journal replay failed for {op.get('op')!r}"
                ) from exc
            after = deepcopy(cursor)
            changed = not _canonical_equal(before, after)
            kind = str(op["op"])
            changed_paths = _changed_paths(before, after)
            _assert_covered_paths(kind, changed_paths)
            settlement_member = kind in CONDITIONAL_SETTLEMENT_MEMBERS and isinstance(
                op.get("_settlement_ref"), str
            )
            if row["source"] in BOOTSTRAP_SOURCES:
                visibility = "bootstrap"
                facts: list[dict[str, Any]] = []
            elif kind in SILENT_OPS:
                visibility = "internal"
                facts = []
            elif kind in EXPLICIT_OUTCOME_OPS or settlement_member:
                visibility = "explicit_adapter"
                facts = []
            else:
                visibility = "required" if changed else "no_effect"
                facts = []
            effect_visibility = (
                "internal"
                if kind in SILENT_OPS
                else "bootstrap" if row["source"] in BOOTSTRAP_SOURCES else "required"
            )
            transition_effects = (
                []
                if not changed or settlement_member
                else _transition_effects(before, after, visibility=effect_visibility)
            )
            if visibility == "required":
                facts = _facts_for_operation_effects(op, transition_effects)
            actor_id = None
            opposition = op.get("_opposition")
            if isinstance(opposition, Mapping) and isinstance(opposition.get("actor"), str):
                actor_id = opposition["actor"]
            elif isinstance(op.get("from_char"), str):
                actor_id = op["from_char"]
            elif isinstance(op.get("char"), str):
                actor_id = op["char"]
            cause_ref = None
            for field in ("_semantic_frame_ref", "_settlement_ref"):
                if isinstance(op.get(field), str) and op[field]:
                    cause_ref = op[field]
                    break
            basis = {
                "schema": TRANSITION_ENTRY_SCHEMA,
                "branch_id": branch_id,
                "turn_index": turn,
                "journal_id": row["id"],
                "op_index": op_index,
                "source": row["source"],
                "op_kind": kind,
                "operation": deepcopy(op),
                "op_fingerprint": content_fingerprint(op),
                "before_fingerprint": content_fingerprint(before),
                "after_fingerprint": content_fingerprint(after),
                "changed": changed,
                "status": "changed" if changed else "no_effect",
                "visibility": visibility,
                "subjects": sorted(
                    {fact["subject_id"] for fact in facts}
                    or ({_subject_from_op(op)} if changed else set())
                ),
                "actor_id": actor_id,
                "cause_ref": cause_ref,
                "covered_paths": changed_paths,
                "facts": facts,
                "effects": transition_effects,
            }
            basis["entry_ref"] = "transition." + content_fingerprint(basis)[7:39]
            if basis["cause_ref"] is None:
                basis["cause_ref"] = basis["entry_ref"]
            basis["construction_ref"] = content_fingerprint(basis)
            entries.append(basis)

    if not _canonical_equal(cursor, post) or content_fingerprint(cursor) != post_hash:
        raise SemanticTransitionTruthError(
            "strict replay of the fenced journal window differs from the post-state"
        )
    required_facts: list[dict[str, Any]] = []
    metadata_receipts: list[dict[str, Any]] = []
    for entry in entries:
        if entry["visibility"] == "required":
            grouped: dict[str, list[dict[str, Any]]] = {}
            for fact in entry["facts"]:
                grouped.setdefault(fact["subject_id"], []).append(
                    {key: fact[key] for key in ("kind", "detail", "amount")}
                )
            for subject_id, effects in sorted(grouped.items()):
                fact_basis = {
                    "cause_ref": entry["cause_ref"],
                    "construction_ref": entry["construction_ref"],
                    "subject_id": subject_id,
                    "effects": effects,
                }
                required_facts.append(
                    {
                        "fact_ref": "transition_fact." + content_fingerprint(fact_basis)[7:39],
                        **fact_basis,
                    }
                )
        elif entry["visibility"] in {"internal", "bootstrap", "no_effect"}:
            metadata_receipts.append(entry)
    payload = {
        "schema": TRANSITION_PROJECTION_SCHEMA,
        "reducer_schema": REDUCER_REPLAY_SCHEMA,
        "reducer_fingerprint": REDUCER_REPLAY_FINGERPRINT,
        "branch_id": branch_id,
        "turn_index": turn,
        "delivery_phase": delivery_phase,
        "pre_state": deepcopy(pre),
        "pre_ledger_hash": pre_hash,
        "post_ledger_hash": post_hash,
        "journal_window_fingerprint": observed_window_fingerprint,
        "journal_rows": deepcopy(rows),
        "entries": entries,
        "transitions": entries,
        "required_facts": required_facts,
        "allowed_facts": [],
        "metadata_receipts": metadata_receipts,
    }
    payload["fingerprint"] = content_fingerprint(payload)
    return validate_transition_projection(payload)


def validate_transition_projection(value: object) -> dict[str, Any]:
    """Validate a detached projection without replaying mutable current code or state."""
    projection = deepcopy(dict(_mapping(value, "transition projection")))
    if set(projection) != _PROJECTION_FIELDS:
        raise SemanticTransitionTruthError("transition projection fields are not exact")
    supplied = projection.pop("fingerprint", None)
    if projection.get("schema") != TRANSITION_PROJECTION_SCHEMA:
        raise SemanticTransitionTruthError("transition projection schema is unsupported")
    if supplied != content_fingerprint(projection):
        raise SemanticTransitionTruthError("transition projection fingerprint is invalid")
    if projection.get("reducer_schema") != REDUCER_REPLAY_SCHEMA \
            or projection.get("reducer_fingerprint") != REDUCER_REPLAY_FINGERPRINT:
        raise SemanticTransitionTruthError(
            "transition projection reducer replay version is not exact"
        )
    pre_hash = _fingerprint(projection.get("pre_ledger_hash"), "pre_ledger_hash")
    _fingerprint(projection.get("post_ledger_hash"), "post_ledger_hash")
    supplied_window_fingerprint = _fingerprint(
        projection.get("journal_window_fingerprint"), "journal_window_fingerprint"
    )
    replay_cursor = deepcopy(dict(_mapping(projection.get("pre_state"), "pre_state")))
    if content_fingerprint(replay_cursor) != pre_hash:
        raise SemanticTransitionTruthError(
            "detached transition pre-state is not rooted in the exact pre-ledger hash"
        )
    if not isinstance(projection.get("branch_id"), str) or not projection["branch_id"].strip():
        raise SemanticTransitionTruthError("transition projection branch identity is invalid")
    turn = _integer(projection.get("turn_index"), "turn_index", minimum=0)
    normalized_rows = _journal_window(projection.get("journal_rows"), turn_index=turn)
    _validate_settlement_occurrence_relations(normalized_rows, turn_index=turn)
    if not _canonical_equal(projection.get("journal_rows"), normalized_rows):
        raise SemanticTransitionTruthError(
            "detached transition journal rows are not in their exact normalized form"
        )
    expected_window_fingerprint = lifecycle_journal_window_fingerprint(
        projection["branch_id"], normalized_rows
    )
    if supplied_window_fingerprint != expected_window_fingerprint:
        raise SemanticTransitionTruthError(
            "detached transition journal window fingerprint does not match its exact rows"
        )
    expected_entry_manifest = [
        {
            "journal_id": row["id"],
            "op_index": op_index,
            "source": row["source"],
            "operation": deepcopy(operation),
        }
        for row in normalized_rows
        for op_index, operation in enumerate(row["ops"])
    ]
    if projection.get("delivery_phase") != "pre_display":
        raise SemanticTransitionTruthError(
            "detached post-delivery projections are disabled without a persisted terminal proof"
        )
    entries = projection.get("entries")
    if not isinstance(entries, list) or not _canonical_equal(
        projection.get("transitions"), entries
    ):
        raise SemanticTransitionTruthError("transition projection entries must be a list")
    if len(entries) != len(expected_entry_manifest):
        raise SemanticTransitionTruthError(
            "transition entries do not map one-to-one to the exact journal rows"
        )
    refs: set[str] = set()
    prior_position = (-1, -1)
    for index, raw in enumerate(entries):
        entry = _mapping(raw, f"transition entries[{index}]")
        expected_manifest = expected_entry_manifest[index]
        if set(entry) != _ENTRY_FIELDS:
            raise SemanticTransitionTruthError("transition entry fields are not exact")
        entry_turn = _integer(
            entry.get("turn_index"),
            "transition entry turn_index",
            minimum=0,
        )
        if entry.get("schema") != TRANSITION_ENTRY_SCHEMA \
                or entry.get("branch_id") != projection["branch_id"] \
                or not _canonical_equal(entry_turn, projection["turn_index"]):
            raise SemanticTransitionTruthError("transition entry identity is inconsistent")
        position = (
            _integer(entry.get("journal_id"), "transition journal_id", minimum=0),
            _integer(entry.get("op_index"), "transition op_index", minimum=0),
        )
        if position != (
            expected_manifest["journal_id"],
            expected_manifest["op_index"],
        ):
            raise SemanticTransitionTruthError(
                "transition entry position does not match the exact journal rows"
            )
        if position <= prior_position:
            raise SemanticTransitionTruthError("transition entries are not in exact journal order")
        prior_position = position
        if entry.get("source") not in JOURNAL_SOURCES:
            raise SemanticTransitionTruthError("transition entry source is unsupported")
        if entry.get("source") != expected_manifest["source"]:
            raise SemanticTransitionTruthError(
                "transition entry source does not match the exact journal rows"
            )
        ref = entry.get("entry_ref")
        if not isinstance(ref, str) or not ref.startswith("transition.") or ref in refs:
            raise SemanticTransitionTruthError("transition entry identity is invalid or duplicated")
        refs.add(ref)
        kind = entry.get("op_kind")
        if kind not in TRANSITION_POLICIES:
            raise SemanticTransitionTruthError("transition entry operation has no policy")
        operation = deepcopy(dict(_mapping(
            entry.get("operation"), f"transition entries[{index}].operation"
        )))
        if not _canonical_equal(operation, expected_manifest["operation"]):
            raise SemanticTransitionTruthError(
                "transition entry operation does not match the exact journal rows"
            )
        _assert_operation_shape(operation, str(kind))
        operation_turn = _integer(
            operation.get("_turn"),
            "transition operation turn_index",
            minimum=0,
        )
        if operation.get("op") != kind \
                or not _canonical_equal(operation_turn, projection["turn_index"]) \
                or entry.get("op_fingerprint") != content_fingerprint(operation):
            raise SemanticTransitionTruthError(
                "transition operation payload, turn, kind, or fingerprint is inconsistent"
            )
        if entry.get("source") in BOOTSTRAP_SOURCES and (
            not _canonical_equal(projection["turn_index"], 0)
            or kind not in T0_BOOTSTRAP_OPERATION_KINDS
            or not _canonical_equal(operation_turn, 0)
        ):
            raise SemanticTransitionTruthError(
                "detached bootstrap authority is restricted to explicit T0 operations"
            )
        replay_before = deepcopy(replay_cursor)
        try:
            _apply_op(replay_cursor, deepcopy(operation))
        except Exception as exc:
            raise SemanticTransitionTruthError(
                f"detached exact reducer replay failed for {kind!r}"
            ) from exc
        replay_after = deepcopy(replay_cursor)
        replay_changed = not _canonical_equal(replay_before, replay_after)
        replay_paths = _changed_paths(replay_before, replay_after)
        _assert_covered_paths(str(kind), replay_paths)
        settlement_member = kind in CONDITIONAL_SETTLEMENT_MEMBERS and isinstance(
            operation.get("_settlement_ref"), str
        )
        expected_visibility = (
            "bootstrap"
            if entry.get("source") in BOOTSTRAP_SOURCES
            else "internal"
            if kind in SILENT_OPS
            else "explicit_adapter"
            if kind in EXPLICIT_OUTCOME_OPS or settlement_member
            else "required"
            if replay_changed
            else "no_effect"
        )
        if entry.get("visibility") != expected_visibility:
            raise SemanticTransitionTruthError(
                "transition visibility is not derivable from its operation and source policy"
            )
        if entry.get("visibility") not in {
            "bootstrap",
            "internal",
            "explicit_adapter",
            "required",
            "no_effect",
        }:
            raise SemanticTransitionTruthError("transition entry visibility is unsupported")
        for field in ("op_fingerprint", "before_fingerprint", "after_fingerprint"):
            _fingerprint(entry.get(field), f"transition entry {field}")
        if not isinstance(entry.get("changed"), bool):
            raise SemanticTransitionTruthError("transition changed marker must be boolean")
        if entry.get("before_fingerprint") != content_fingerprint(replay_before) \
                or entry.get("after_fingerprint") != content_fingerprint(replay_after):
            raise SemanticTransitionTruthError(
                "transition state fingerprints do not match exact rooted reducer replay"
            )
        if entry["changed"] != replay_changed:
            raise SemanticTransitionTruthError(
                "transition changed marker does not match exact rooted reducer replay"
            )
        expected_status = "changed" if replay_changed else "no_effect"
        if entry.get("status") != expected_status:
            raise SemanticTransitionTruthError("transition status contradicts its state delta")
        subjects = entry.get("subjects")
        if not isinstance(subjects, list) or subjects != sorted(set(subjects)) \
                or any(not isinstance(subject, str) or not subject for subject in subjects):
            raise SemanticTransitionTruthError("transition subjects are not canonical")
        expected_actor = None
        opposition = operation.get("_opposition")
        if isinstance(opposition, Mapping) and isinstance(opposition.get("actor"), str):
            expected_actor = opposition["actor"]
        elif isinstance(operation.get("from_char"), str):
            expected_actor = operation["from_char"]
        elif isinstance(operation.get("char"), str):
            expected_actor = operation["char"]
        if entry.get("actor_id") != expected_actor:
            raise SemanticTransitionTruthError(
                "transition actor is not derivable from its exact operation"
            )
        if entry.get("actor_id") is not None and (
            not isinstance(entry["actor_id"], str) or not entry["actor_id"]
        ):
            raise SemanticTransitionTruthError("transition actor identity is invalid")
        if not isinstance(entry.get("cause_ref"), str) or not entry["cause_ref"]:
            raise SemanticTransitionTruthError("transition cause identity is invalid")
        paths = entry.get("covered_paths")
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise SemanticTransitionTruthError("transition covered paths are invalid")
        if paths != replay_paths:
            raise SemanticTransitionTruthError(
                "transition changed paths do not match exact rooted reducer replay"
            )
        _assert_covered_paths(kind, paths)
        if not isinstance(entry.get("facts"), list) or not isinstance(entry.get("effects"), list):
            raise SemanticTransitionTruthError("transition entry facts must be a list")
        for fact in entry["facts"]:
            if not isinstance(fact, Mapping) or set(fact) != _TRANSITION_FACT_FIELDS:
                raise SemanticTransitionTruthError("transition fact fields are not exact")
            if not isinstance(fact["subject_id"], str) or not fact["subject_id"] \
                    or fact["kind"] not in _CLAIM_KINDS \
                    or not isinstance(fact["detail"], str) or not fact["detail"] \
                    or (fact["amount"] is not None and (
                        isinstance(fact["amount"], bool) or not isinstance(fact["amount"], int)
                    )):
                raise SemanticTransitionTruthError("transition fact is invalid")
        effect_paths: list[str] = []
        for effect in entry["effects"]:
            if not isinstance(effect, Mapping) or set(effect) != _TRANSITION_EFFECT_FIELDS:
                raise SemanticTransitionTruthError("transition leaf effect fields are not exact")
            if not isinstance(effect["path"], str) or not effect["path"].startswith("/") \
                    or effect["visibility"] not in {"internal", "bootstrap", "required"} \
                    or (effect["delta"] is not None and (
                        isinstance(effect["delta"], bool) or not isinstance(effect["delta"], int)
                    )):
                raise SemanticTransitionTruthError("transition leaf effect is invalid")
            effect_paths.append(effect["path"])
        if effect_paths != entry["covered_paths"]:
            raise SemanticTransitionTruthError(
                "transition leaf effects do not exactly cover the changed paths"
            )
        expected_effect_visibility = (
            "internal"
            if kind in SILENT_OPS
            else "bootstrap"
            if entry["source"] in BOOTSTRAP_SOURCES
            else "required"
        )
        replay_effects = (
            []
            if not replay_changed or settlement_member
            else _transition_effects(
                replay_before,
                replay_after,
                visibility=expected_effect_visibility,
            )
        )
        if not _canonical_equal(entry["effects"], replay_effects):
            raise SemanticTransitionTruthError(
                "transition leaf effects are not the complete exact rooted reducer replay"
            )
        _assert_world_flag_effect_binding(
            operation,
            entry["effects"],
            changed=replay_changed,
        )
        if not replay_changed or settlement_member:
            if entry["effects"]:
                raise SemanticTransitionTruthError(
                    "no-effect or settlement-member transition cannot carry leaf effects"
                )
        else:
            if any(
                effect["visibility"] != expected_effect_visibility
                for effect in entry["effects"]
            ):
                raise SemanticTransitionTruthError(
                    "transition leaf visibility is not derivable from operation policy"
                )
        if expected_visibility != "required" and entry["facts"]:
            raise SemanticTransitionTruthError(
                "non-required transition cannot carry independently required facts"
            )
        if expected_visibility == "required":
            expected_facts = _facts_for_operation_effects(operation, entry["effects"])
            if not _canonical_equal(entry["facts"], expected_facts):
                raise SemanticTransitionTruthError(
                    "transition facts are not the complete exact ordered set derivable from its "
                    "operation and effects"
                )
        detached = dict(entry)
        construction = detached.pop("construction_ref", None)
        if construction != content_fingerprint(detached):
            raise SemanticTransitionTruthError("transition construction fingerprint is invalid")
        ref_basis = dict(detached)
        ref_basis.pop("entry_ref")
        if entry["cause_ref"] == ref:
            ref_basis["cause_ref"] = None
        if ref != "transition." + content_fingerprint(ref_basis)[7:39]:
            raise SemanticTransitionTruthError("transition entry reference is invalid")
        external_cause = None
        for field in ("_semantic_frame_ref", "_settlement_ref"):
            if isinstance(operation.get(field), str) and operation[field]:
                external_cause = operation[field]
                break
        if entry["cause_ref"] != (external_cause or ref):
            raise SemanticTransitionTruthError(
                "transition cause is not derivable from its exact operation"
            )
        expected_subjects = sorted(
            {fact["subject_id"] for fact in entry["facts"]}
            or ({_subject_from_op(operation)} if entry["changed"] else set())
        )
        if entry["subjects"] != expected_subjects:
            raise SemanticTransitionTruthError(
                "transition subjects are not derivable from operation truth"
            )
    if content_fingerprint(replay_cursor) != projection["post_ledger_hash"]:
        raise SemanticTransitionTruthError(
            "detached exact reducer replay does not reach the post-ledger hash"
        )
    required = projection.get("required_facts")
    allowed = projection.get("allowed_facts")
    metadata = projection.get("metadata_receipts")
    if not isinstance(required, list) or allowed != [] or not isinstance(metadata, list):
        raise SemanticTransitionTruthError("transition projection aggregate fields are invalid")
    expected_required: list[dict[str, Any]] = []
    expected_metadata: list[dict[str, Any]] = []
    for entry in entries:
        if entry["visibility"] == "required":
            grouped: dict[str, list[dict[str, Any]]] = {}
            for fact in entry["facts"]:
                grouped.setdefault(fact["subject_id"], []).append(
                    {key: fact[key] for key in ("kind", "detail", "amount")}
                )
            for subject_id, effects in sorted(grouped.items()):
                basis = {
                    "cause_ref": entry["cause_ref"],
                    "construction_ref": entry["construction_ref"],
                    "subject_id": subject_id,
                    "effects": effects,
                }
                expected_required.append(
                    {
                        "fact_ref": "transition_fact." + content_fingerprint(basis)[7:39],
                        **basis,
                    }
                )
        elif entry["visibility"] in {"internal", "bootstrap", "no_effect"}:
            expected_metadata.append(entry)
    if not _canonical_equal(required, expected_required) \
            or not _canonical_equal(metadata, expected_metadata):
        raise SemanticTransitionTruthError("transition projection aggregates are not derivable")
    for fact in required:
        if not isinstance(fact, Mapping) or set(fact) != _REQUIRED_FACT_FIELDS:
            raise SemanticTransitionTruthError("required transition fact fields are not exact")
    projection["fingerprint"] = supplied
    return projection
