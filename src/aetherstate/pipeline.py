"""Per-request enrichment pipeline: observe -> Tier-0 -> apply -> compose -> forward,
plus the post-stream half: response tee -> turn-text capture -> discovery -> extraction.

Hot path (pre-forward, deterministic sub-ms — Q15): observe, Tier-0, authority apply,
header compose. Cold path (strictly post-stream, 03 SS1): assistant-text capture, entity
discovery (08 B2), and Tier-1 scheduling via the JobRunner. Every step is fail-open
(invariant 1/2).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Optional
from urllib.parse import urlparse

from . import (__version__, compose, director, discovery, genesis, linter, promptcache,
               semantic_ingress, tier0)
from .canon import canonicalize, content_hash
from .claim_ingress import claim_ops_from_text
from .config import Config
from .narration_fallback_runtime import build_proof_complete_fallback
from .narration_artifact_basis import derive_swipe_fallback_from_persisted_basis
from .narration_pre_display_guard import (
    NarrationGuardBasis,
    build_narration_guard_basis,
    guard_narration_story,
)
from .narrator_realization import (build_narrator_realization_from_state,
                                   narrator_realization_owns_turn)
from .response_wire import decode_chat_story
from .semantic_bootstrap_runtime import build_semantic_bootstrap_proof
from .semantic_narration_orchestrator import (
    FallbackPromotionExpectation,
    NarrationSelectionPreparation,
    build_fallback_promotion_expectation,
    prepare_narration_selection,
    resolve_narration_selection,
)
from .session_engine import SessionEngine
from . import memory
from .state import (_norm_loc, apply_delta, assign_damage_effect_ids, battle_ops, combat_ops,
                    current_state, empty_state, progression_ops, reduce_state,
                    resolve_entity_ref, world_ops)
from .stamps import Stamp
from .store import Store
from .turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    FencedMutationOutput,
    ReplayArtifact,
    build_pre_mutation_key,
    fingerprint,
    raw_fingerprint,
    validate_pre_mutation_key,
)

log = logging.getLogger("aetherstate.pipeline")

SEMANTIC_RUNTIME_CONTRACT_VERSION = "semantic-prevention-runtime/1"
_DEFAULT_PLAYERLEX_SERVICE = object()
_DEFAULT_PLAYER_LESSONS_SERVICE = object()

_NARRATION_GUARD_TRACE_REASON_CODES = frozenset({
    "roll_outcome_conflict",
    "unsettled_combatant_impact",
    "unsettled_combatant_defeat",
    "candidate_wire_or_guard_unavailable",
    "fallback_encoding_unavailable",
})


class SemanticGateConflict(ValueError):
    """A visible RPG request cannot safely select or extend a semantic artifact."""


def _live_recalc(cfg) -> bool:
    """True when the newest reply is ingested immediately (default). Bean 2026-07-07."""
    return bool(getattr(getattr(cfg, "extraction", None), "live_recalc", True))


def _drop_redundant_scene_restatement(
    ops: list[dict], state: dict, combat_tags: list[dict]
) -> tuple[list[dict], bool]:
    """Ignore a narrator scene tag that only re-labels the current phase.

    The wire contract requires ``[scene]`` when location or on-stage cast changes; ``phase`` is
    optional metadata on that real boundary. Repeating the same location and already-present cast
    is not authority to advance the dramatic phase. A combat declaration is exempt because its
    code-owned spawn is itself the scene boundary even when the location remains unchanged.
    """
    scene_ops = [op for op in ops if op.get("op") == "scene_set"]
    if not scene_ops or combat_tags:
        return ops, False
    scene = state.get("scene") if isinstance(state.get("scene"), dict) else {}
    current_location = str(scene.get("location_id") or scene.get("location") or "")
    if not current_location or any(
        _norm_loc(str(op.get("location") or "")) != _norm_loc(current_location)
        for op in scene_ops
    ):
        return ops, False
    entities = state.get("entities") if isinstance(state.get("entities"), dict) else {}
    presence_ops = [op for op in ops if op.get("op") == "presence"]
    for op in presence_ops:
        entity = resolve_entity_ref(state, op.get("entity"))
        row = entities.get(entity)
        if not isinstance(row, dict) or bool(row.get("present")) != bool(op.get("present")):
            return ops, False
    return [op for op in ops if op.get("op") not in {"scene_set", "presence"}], True


_PICKUP_ACTION_RE = re.compile(
    r"\b(?:pick(?:ing|ed)?\s+up|collect(?:ing|ed|s)?|gather(?:ing|ed|s)?|"
    r"grab(?:bing|bed|s)?|loot(?:ing|ed|s)?|retriev(?:e|es|ed|ing)|recover(?:ing|ed|s)?)\b",
    re.IGNORECASE,
)
_ITEM_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_ITEM_REFERENCE_STOP = frozenset({"a", "an", "the", "few", "some", "several", "of"})


def _item_reference_terms(text: object) -> set[str]:
    terms: set[str] = set()
    for raw in _ITEM_WORD_RE.findall(str(text or "").lower()):
        word = raw[:-1] if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss") else raw
        if word not in _ITEM_REFERENCE_STOP:
            terms.add(word)
    return terms


def _bind_reply_pickups(
    ops: list[dict], state: dict, player_text: str,
) -> tuple[list[dict], int]:
    """Turn a narrated pickup into custody transfer of one exact world instance.

    Organic gifts/finds still use ``item_gain``. When the Player explicitly retrieves a named
    field object, however, a loosely renamed reply tag may not mint a substitute. The object and
    optional defeated source must resolve uniquely; reachability is enforced by ``item_transfer``.
    """
    if not _PICKUP_ACTION_RE.search(player_text or ""):
        return ops, 0
    player_terms = _item_reference_terms(player_text)
    world_items = [
        (iid, item) for iid, item in ((state.get("items") or {}).items())
        if isinstance(item, dict) and item.get("owner") is None
        and str(item.get("loc") or "").split(":", 1)[0] == "world"
    ]
    bound: list[dict] = []
    rejected = 0
    for op in ops:
        if op.get("op") != "item_gain":
            bound.append(op)
            continue
        gain_terms = _item_reference_terms(op.get("name"))
        candidates = [
            (iid, item) for iid, item in world_items
            if (_item_reference_terms(item.get("name")) & player_terms)
            and (_item_reference_terms(item.get("name")) & gain_terms)
        ]
        source_hits = [
            pair for pair in candidates
            if _item_reference_terms(pair[1].get("dropped_by_name"))
            and _item_reference_terms(pair[1].get("dropped_by_name")) <= player_terms
        ]
        if source_hits:
            candidates = source_hits
        if not candidates:
            bound.append(op)  # no field-object reference: preserve the organic acquisition path
            continue
        if len(candidates) != 1:
            rejected += 1
            continue
        iid, item = candidates[0]
        tagged_qty = op.get("qty")
        if tagged_qty is not None and int(tagged_qty) != int(item.get("qty", 1)):
            rejected += 1
            continue
        bound.append({"op": "item_transfer", "instance": iid, "to_owner": op["char"]})
    return bound, rejected


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _last_user_action_hash(doc: dict) -> str:
    """Content-free identity for the newest Player action, including stripped OOC commands."""
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return ""
    text = next((_message_text(message.get("content")) for message in reversed(messages)
                 if isinstance(message, dict) and message.get("role") == "user"), "")
    normalized = " ".join(text.split())
    return content_hash(normalized) if normalized else ""


def _first_enemy_spawn_receipt(ops: list[dict], state: dict, source_turn: int) -> bool:
    """True only when ``ops`` introduced this combat's first hostile instance.

    ``combatant_spawn`` is shared by allies and later reinforcements, so the family name alone
    cannot establish the fresh-foe narration boundary. Match an applied enemy spawn's baked cid
    against the bounded live combat board, then require that no enemy row predates this turn.
    """
    try:
        turn = int(source_turn)
    except (TypeError, ValueError):
        return False
    spawn_ids = {
        str(op.get("_cid"))
        for op in ops
        if isinstance(op, dict)
        and op.get("op") == "combatant_spawn"
        and op.get("side") == "enemy"
        and op.get("_cid")
        # A known opponent staged before generation already exposed its frozen intent in the
        # original packet. A swipe must preserve that same visible intent, not regress to the
        # narrator-[foe] no-intent boundary used for post-response introductions.
        and op.get("_intro_intent_visible") != "known-opponent-opening/1"
    }
    if not spawn_ids:
        return False
    rows = ((state.get("combat") or {}).get("combatants") or {}).values()
    enemies = [row for row in rows
               if isinstance(row, dict) and row.get("side") == "enemy"]
    if not enemies:
        return False
    try:
        first_turn = min(int(row["spawned_turn"]) for row in enemies)
    except (KeyError, TypeError, ValueError):
        return False
    return first_turn == turn and any(
        str(row.get("id")) in spawn_ids and int(row.get("spawned_turn", -1)) == turn
        for row in enemies
    )


_FIRST_INTENT_LOCAL_RESPONSE_BLOCKING_OPS = {
    # Any settled Player mechanic needs narrator realization beside the exact future tell. A local
    # one-sentence tell is safe only when it cannot hide a current code-owned result.  The one
    # exception is the proof-bound ``combat_opening/1`` transition itself: it admits the target and
    # opens the scene without resolving an attack, HP change, or other consequence.
    "hp_adj", "combatant_hp", "combatant_defeat", "defeat_resolve", "combat_end", "battle_end",
}

_FIRST_INTENT_SEMANTIC_EVIDENCE_OPS = {
    "semantic_meaning_commit",
    "semantic_binding_commit",
    "semantic_frame_commit",
}


def _exact_turn(value, turn_index: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == turn_index


def _canonical_sha256_ref(value) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    digest = value[7:]
    if digest != digest.lower():
        return False
    try:
        int(digest, 16)
    except ValueError:
        return False
    return True


def _safe_first_intent_bookkeeping(op: dict, turn_index: int) -> bool:
    """Allow only state-neutral semantic evidence and the normal three-minute clock row."""
    family = op.get("op")
    if family in _FIRST_INTENT_SEMANTIC_EVIDENCE_OPS:
        return _exact_turn(op.get("_turn"), turn_index) \
            and "_settlement_ref" not in op
    if family == "clock_tick":
        return op == {"op": "clock_tick", "minutes": 3, "_turn": turn_index}
    return False


def _live_current_enemy(state: dict, actor: str, turn_index: int) -> Optional[dict]:
    combat = state.get("combat")
    if not isinstance(combat, dict) or combat.get("active") is not True:
        return None
    combatants = combat.get("combatants")
    if not isinstance(combatants, dict):
        return None
    row = combatants.get(actor)
    if not isinstance(row, dict) or row.get("id") != actor or row.get("side") != "enemy" \
            or row.get("defeated") is not False \
            or not _exact_turn(row.get("spawned_turn"), turn_index):
        return None
    hp = row.get("hp")
    if not isinstance(hp, dict):
        return None
    cur, maximum = hp.get("cur"), hp.get("max")
    if not isinstance(cur, int) or isinstance(cur, bool) or cur <= 0 \
            or not isinstance(maximum, int) or isinstance(maximum, bool) \
            or maximum < cur:
        return None
    return row


def _journal_ops_at(store: Store, branch_id: str, turn_index: int) -> list[tuple[str, dict]]:
    """Exact-turn durable journal rows with their authority source."""
    with store._lock:
        rows = store.db.execute(
            "SELECT source, ops FROM ops_journal WHERE branch_id=? AND turn_lo=? AND turn_hi=? "
            "ORDER BY id",
            (branch_id, int(turn_index), int(turn_index)),
        ).fetchall()
    out: list[tuple[str, dict]] = []
    for row in rows:
        try:
            decoded = json.loads(row["ops"])
            if not isinstance(decoded, list) or not all(isinstance(op, dict) for op in decoded):
                return [("invalid", {"op": "__invalid_journal_operation__"})]
            out.extend((str(row["source"]), op) for op in decoded)
        except (json.JSONDecodeError, TypeError, ValueError):
            return [("invalid", {"op": "__invalid_journal_operation__"})]
    return out


def _proof_bound_combat_opening_spawn(
        journal: list[tuple[str, dict]], state: dict, turn_index: int,
        actor: str, intent: dict) -> bool:
    """Match one exact opening envelope, its projections, and its persisted receipt.

    The deterministic first-intent sentence is intentionally narrower than ordinary narration.
    A caller-written ``contract_id`` must therefore not turn an arbitrary settlement into the
    local response exception.  Require the live prepared wrapper, its validated Store row, every
    indexed projection byte-for-byte, and both state-side persistence records before recognizing
    the exact primary enemy spawn.
    """
    from .mechanic_settlement import (
        COMBAT_OPENING_CONTRACT,
        MechanicSettlementError,
        validate_mechanic_settlement,
        validate_mechanic_settlement_row,
    )

    if not isinstance(turn_index, int) or isinstance(turn_index, bool) \
            or not isinstance(actor, str) or not actor \
            or not isinstance(intent, dict) or intent.get("schema") != "enemy-intent/1" \
            or intent.get("actor") != actor \
            or not _exact_turn(intent.get("prepared_turn"), turn_index) \
            or (state.get("combat") or {}).get("pending_intent") != intent:
        return False
    primary_board_row = _live_current_enemy(state, actor, turn_index)
    if primary_board_row is None:
        return False

    wrappers = [
        (source, op) for source, op in journal
        if op.get("op") == "mechanic_settlement_commit"
    ]
    if len(wrappers) != 1:
        return False
    source, wrapper = wrappers[0]
    wrapper_keys = {
        "op", "contract_id", "settlement_ref", "frame_ref", "members",
        "_semantic_frame_ref", "_turn", "_request_fingerprint",
        "_settlement_prepared", "receipt", "_store_row",
    }
    if source != "rule" or set(wrapper) != wrapper_keys \
            or wrapper.get("contract_id") != COMBAT_OPENING_CONTRACT \
            or wrapper.get("_settlement_prepared") is not True \
            or not _exact_turn(wrapper.get("_turn"), turn_index) \
            or not _canonical_sha256_ref(wrapper.get("_request_fingerprint")):
        return False

    receipt = wrapper.get("receipt")
    store_row = wrapper.get("_store_row")
    try:
        clean_receipt = validate_mechanic_settlement(receipt)
        clean_store_row = validate_mechanic_settlement_row(store_row)
    except (MechanicSettlementError, TypeError, ValueError):
        return False
    if clean_receipt != receipt or clean_store_row != store_row \
            or clean_store_row.get("receipt") != receipt:
        return False

    settlement_ref = str(receipt.get("settlement_ref") or "")
    frame_ref = str(receipt.get("frame_ref") or "")
    if not _canonical_sha256_ref(settlement_ref) or not _canonical_sha256_ref(frame_ref) \
            or receipt.get("contract_id") != COMBAT_OPENING_CONTRACT \
            or store_row.get("contract_id") != COMBAT_OPENING_CONTRACT \
            or store_row.get("settlement_ref") != settlement_ref \
            or store_row.get("frame_ref") != frame_ref \
            or store_row.get("receipt_fingerprint") != receipt.get("receipt_fingerprint") \
            or not _canonical_sha256_ref(store_row.get("request_fingerprint")) \
            or wrapper.get("settlement_ref") != settlement_ref \
            or wrapper.get("frame_ref") != frame_ref \
            or wrapper.get("_semantic_frame_ref") != frame_ref:
        return False

    members = wrapper.get("members")
    if not isinstance(members, list) or not members \
            or not all(
                isinstance(member, dict)
                and _exact_turn(member.get("_turn"), turn_index)
                and member.get("_semantic_frame_ref") == frame_ref
                for member in members
            ):
        return False
    projections = [
        op for projection_source, op in journal
        if projection_source == "rule" and op.get("_settlement_ref") == settlement_ref
    ]
    if len(projections) != len(members):
        return False
    projection_indices = [op.get("_settlement_member_index") for op in projections]
    if any(
        not isinstance(index, int) or isinstance(index, bool)
        for index in projection_indices
    ) or sorted(projection_indices) != list(range(len(members))):
        return False
    projections.sort(key=lambda op: op["_settlement_member_index"])
    for index, (member, projection) in enumerate(zip(members, projections)):
        expected = deepcopy(member)
        expected["_settlement_ref"] = settlement_ref
        expected["_settlement_member_index"] = index
        if projection != expected:
            return False

    projection_ids = {id(op) for op in projections}
    wrapper_id = id(wrapper)
    clock_rows = 0
    for journal_source, op in journal:
        if journal_source != "rule":
            return False
        if id(op) == wrapper_id or id(op) in projection_ids:
            continue
        if not _safe_first_intent_bookkeeping(op, turn_index):
            return False
        if op.get("op") == "clock_tick":
            clock_rows += 1
            if clock_rows > 1:
                return False

    public_rows = state.get("mechanic_settlements") or []
    private_rows = state.get("_mechanic_settlement_projection_members") or []
    if not isinstance(public_rows, list) or not isinstance(private_rows, list):
        return False
    if any(
        isinstance(row, dict) and row.get("turn") == turn_index
        and not _exact_turn(row.get("turn"), turn_index)
        for row in [*public_rows, *private_rows]
    ):
        return False
    current_public = [
        row for row in public_rows
        if isinstance(row, dict) and _exact_turn(row.get("turn"), turn_index)
    ]
    current_private = [
        row for row in private_rows
        if isinstance(row, dict) and _exact_turn(row.get("turn"), turn_index)
    ]
    expected_public = {"turn": turn_index, "receipt": receipt}
    expected_private = {
        "turn": turn_index,
        "settlement_ref": settlement_ref,
        "request_fingerprint": wrapper["_request_fingerprint"],
        "members": projections,
    }
    if current_public != [expected_public] or current_private != [expected_private]:
        return False

    spawns = [op for op in projections if op.get("op") == "combatant_spawn"]
    scenes = [op for op in projections if op.get("op") == "scene_set"]
    if not 1 <= len(spawns) <= 3 or len(scenes) != 1 \
            or scenes[0].get("phase") != "climax" or scenes[0].get("_floor") is not True \
            or len(spawns) + 1 != len(projections):
        return False
    spawn_ids = [op.get("_cid") for op in spawns]
    if not all(isinstance(value, str) and value for value in spawn_ids) \
            or len(set(spawn_ids)) != len(spawn_ids):
        return False
    for spawn_id, spawn in zip(spawn_ids, spawns):
        if spawn.get("side") != "enemy" \
                or _live_current_enemy(state, spawn_id, turn_index) is None:
            return False

    matching_spawns = [
        op for op in projections
        if op.get("op") == "combatant_spawn"
        and op.get("side") == "enemy"
        and op.get("_intro_intent_visible") == "known-opponent-opening/1"
        and op.get("_cid") == actor
        and op.get("_initial_intent") == intent
    ]
    visible_opening_spawns = [
        op for op in spawns
        if op.get("_intro_intent_visible") == "known-opponent-opening/1"
    ]
    changes = receipt.get("applied_changes")
    if not isinstance(changes, list):
        return False
    admissions = [row for row in changes if row.get("kind") == "target_admission"]
    scene_changes = [row for row in changes if row.get("kind") == "scene_transition"]
    target_state = receipt.get("target_post_state")
    return bool(
        len(matching_spawns) == len(visible_opening_spawns) == 1
        and len(changes) == len(spawns) + 1
        and len(admissions) == len(spawns)
        and len(scene_changes) == 1
        and {row.get("entity_id") for row in admissions} == set(spawn_ids)
        and isinstance(target_state, dict)
        and target_state.get("combatant_id") == actor
        and target_state.get("hp") == primary_board_row.get("hp")
    )


def _proof_bound_legacy_combat_opening_spawn(
        journal: list[tuple[str, dict]], state: dict, turn_index: int,
        actor: str, intent: dict) -> bool:
    """Preserve old rule-staged openings without lending them any other current mechanic."""
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) \
            or not isinstance(actor, str) or not actor \
            or not isinstance(intent, dict) or intent.get("schema") != "enemy-intent/1" \
            or intent.get("actor") != actor \
            or not _exact_turn(intent.get("prepared_turn"), turn_index) \
            or (state.get("combat") or {}).get("pending_intent") != intent \
            or _live_current_enemy(state, actor, turn_index) is None:
        return False
    spawns: list[dict] = []
    clock_rows = 0
    for source, op in journal:
        if source != "rule":
            return False
        if op.get("op") == "combatant_spawn":
            spawns.append(op)
            continue
        if not _safe_first_intent_bookkeeping(op, turn_index):
            return False
        if op.get("op") == "clock_tick":
            clock_rows += 1
            if clock_rows > 1:
                return False
    current_public = [
        row for row in state.get("mechanic_settlements") or []
        if isinstance(row, dict) and row.get("turn") == turn_index
    ]
    current_private = [
        row for row in state.get("_mechanic_settlement_projection_members") or []
        if isinstance(row, dict) and row.get("turn") == turn_index
    ]
    return bool(
        not current_public and not current_private and len(spawns) == 1
        and spawns[0].get("side") == "enemy"
        and spawns[0].get("_cid") == actor
        and spawns[0].get("_intro_intent_visible") == "known-opponent-opening/1"
        and spawns[0].get("_initial_intent") == intent
        and _exact_turn(spawns[0].get("_turn"), turn_index)
    )


def _deterministic_first_intent_story(
        store: Store, res, state: dict, cfg: Config, stamp: Optional[Stamp]) -> str:
    """Authorize the one local first-intent sentence from durable current-turn evidence."""
    spec = getattr(cfg, "specialization", None)
    if spec is None or spec.name != "rpg" or not getattr(spec, "war_room", True) \
            or not getattr(spec, "enemy_rolls", True) \
            or stamp is None or stamp.card_role != "narrator" \
            or res.klass.value not in ("new_session", "new_turn", "swipe"):
        return ""
    if not isinstance(res.turn_index, int) or isinstance(res.turn_index, bool):
        return ""
    state_turn = (state.get("meta") or {}).get("turn")
    if not _exact_turn(state_turn, res.turn_index):
        return ""
    combat = state.get("combat") or {}
    intent = combat.get("pending_intent")
    if not isinstance(intent, dict):
        return ""
    actor = intent.get("actor")
    if not isinstance(actor, str) or not actor:
        return ""
    journal = _journal_ops_at(store, res.branch_id, res.turn_index)
    if any(
        op.get("op") in _FIRST_INTENT_LOCAL_RESPONSE_BLOCKING_OPS
        or isinstance(op.get("_opposition"), dict)
        for _source, op in journal
    ):
        return ""
    has_settlement_wrapper = any(
        op.get("op") == "mechanic_settlement_commit" for _source, op in journal
    )
    proof_bound_opening = _proof_bound_combat_opening_spawn(
        journal, state, res.turn_index, actor, intent
    ) if has_settlement_wrapper else False
    legacy_opening = not has_settlement_wrapper and _proof_bound_legacy_combat_opening_spawn(
        journal, state, res.turn_index, actor, intent
    )
    if not proof_bound_opening and not legacy_opening:
        return ""
    return compose.deterministic_first_intent_story(state)


def _reasoning_controls_receipt(doc: dict) -> dict:
    """Allowlisted, content-free scalar proof of the final outbound reasoning controls."""
    reasoning = doc.get("reasoning") if isinstance(doc.get("reasoning"), dict) else {}
    venice = (doc.get("venice_parameters")
              if isinstance(doc.get("venice_parameters"), dict) else {})

    def exact_bool(mapping: dict, key: str) -> Optional[bool]:
        value = mapping.get(key)
        return value if isinstance(value, bool) else None

    effort_present = "reasoning_effort" in doc or "effort" in reasoning
    payload = {
        "schema": "reasoning-controls/1",
        "reasoning_enabled": exact_bool(reasoning, "enabled"),
        "reasoning_effort_present": effort_present,
        "venice_disable_thinking": exact_bool(venice, "disable_thinking"),
        "venice_strip_thinking_response": exact_bool(venice, "strip_thinking_response"),
    }
    payload["hard_off"] = bool(
        payload["reasoning_enabled"] is False
        and not payload["reasoning_effort_present"]
        and payload["venice_disable_thinking"] is True
        and payload["venice_strip_thinking_response"] is True
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {**payload, "fingerprint": content_hash(canonical)}


def _packet_manifest(doc: dict) -> dict:
    """Content-free shape of the exact narrator request for local turn tracing."""
    rows = []
    for index, message in enumerate(doc.get("messages") or []):
        if not isinstance(message, dict):
            rows.append({"index": index, "role": "invalid", "kind": "invalid",
                         "chars": 0, "hash": ""})
            continue
        role = str(message.get("role") or "")
        text = _message_text(message.get("content"))
        if text.startswith("[AETHERSTATE NARRATOR CONTRACT "):
            kind = "narrator_contract"
        elif text.startswith(compose.TURN_PACKET_START):
            kind = "aether_turn_packet"
        elif text.startswith("[AETHER P0]") or text.startswith("[AETHER P1]"):
            kind = "player_current"
        elif text.startswith("[CURRENT REQUEST DIRECTIVE"):
            kind = "player_with_directive"  # legacy enriched packets
        elif role == "system":
            kind = "st_system"
        elif role in ("user", "assistant"):
            kind = f"history_{role}"
        else:
            kind = "other"
        rows.append({"index": index, "role": role, "kind": kind, "chars": len(text),
                     "hash": content_hash(text) if text else ""})
    return {
        "model": str(doc.get("model") or ""),
        "stream": bool(doc.get("stream")),
        "request_fields": sorted(str(key) for key in doc if key != "messages"),
        "reasoning_controls": _reasoning_controls_receipt(doc),
        "messages": rows,
        "sentinel_present": "<<AETHER:" in json.dumps(doc.get("messages") or [],
                                                        ensure_ascii=False),
    }


def _diagnostic_narrator_packet(
    doc: dict,
    *,
    redacted_texts: tuple[str, ...] = (),
) -> dict:
    """Return only the exact AetherState-owned blocks sent to the narrator.

    The full request contains chat history and the newest Player prose.  Those bytes are not
    needed to diagnose semantic transfer and must not enter the diagnostic file.  The turn packet
    and the prefix attached ahead of the newest message are engine-owned and end at explicit
    sentinels, so they can be retained without retaining the Player message that follows.
    """
    redactions = [
        {
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "marker": f"[LOCAL RESPONSE REDACTED sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}]",
        }
        for text in redacted_texts
        if text
    ]

    def _redact_player_lessons(text: str) -> str:
        header = f"[PLAYER LESSONS {compose.PLAYER_LESSONS_VERSION} "
        cursor = 0
        while True:
            start = text.find(header, cursor)
            if start < 0:
                break
            boundaries = [
                pos for pos in (
                    text.find("\n[", start + 1),
                    text.find("\n" + compose.TURN_PACKET_END, start + 1),
                ) if pos >= 0
            ]
            end = min(boundaries) if boundaries else len(text)
            private_component = text[start:end]
            digest = hashlib.sha256(private_component.encode("utf-8")).hexdigest()
            receipt = {
                "chars": len(private_component),
                "sha256": digest,
                "marker": f"[PLAYER LESSONS REDACTED sha256:{digest}]",
            }
            if not any(
                row["chars"] == receipt["chars"] and row["sha256"] == receipt["sha256"]
                for row in redactions
            ):
                redactions.append(receipt)
            text = text[:start] + receipt["marker"] + text[end:]
            cursor = start + len(receipt["marker"])
        return text

    def _redact(text: str) -> str:
        for raw, receipt in zip((value for value in redacted_texts if value), redactions):
            text = text.replace(raw, receipt["marker"])
        return _redact_player_lessons(text)

    blocks: list[dict] = []
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        messages = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts = [(None, content)]
        elif isinstance(content, list):
            parts = [
                (part_index, part.get("text"))
                for part_index, part in enumerate(content)
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
        else:
            parts = []
        for part_index, text in parts:
            start = text.find(compose.TURN_PACKET_START)
            if start >= 0:
                end = text.find(compose.TURN_PACKET_END, start)
                if end >= 0:
                    end += len(compose.TURN_PACKET_END)
                    blocks.append({
                        "message_index": message_index,
                        "part_index": part_index,
                        "role": str(message.get("role") or ""),
                        "kind": "turn_packet",
                        "text": _redact(text[start:end]),
                    })
            if not (text.startswith("[AETHER P0]\n") or text.startswith("[AETHER P1]\n")):
                continue
            markers = (
                "[PLAYER'S NEWEST MESSAGE — respond to this now]",
                "[CURRENT CONTINUATION TARGET — continue this prose now]",
            )
            marker_positions = [(text.find(marker), marker) for marker in markers]
            marker_positions = [(pos, marker) for pos, marker in marker_positions if pos >= 0]
            if not marker_positions:
                continue
            marker_pos, marker = min(marker_positions, key=lambda row: row[0])
            end = marker_pos + len(marker)
            if text[end:end + 2] == "\r\n":
                end += 2
            elif text[end:end + 1] == "\n":
                end += 1
            blocks.append({
                "message_index": message_index,
                "part_index": part_index,
                "role": str(message.get("role") or ""),
                "kind": "current_directive",
                "text": _redact(text[:end]),
            })
    canonical = json.dumps(blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "narrator_blocks": blocks,
        "narrator_blocks_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "narrator_redactions": [
            {"chars": row["chars"], "sha256": row["sha256"]} for row in redactions
        ],
    }


def _narrator_reasoning_hard_off_required(cfg, stamp: Optional[Stamp]) -> bool:
    """Return whether this exact narrator request targets Venice with hard-off enabled."""
    if not getattr(getattr(cfg, "upstream", None), "disable_narrator_reasoning", True):
        return False
    if not stamp or stamp.card_role != "narrator":
        return False
    try:
        host = (urlparse(str(cfg.upstream.base_url)).hostname or "").lower()
    except (TypeError, ValueError):
        return False
    return host == "venice.ai" or host.endswith(".venice.ai")


def _apply_narrator_reasoning_default(doc: dict, cfg, stamp: Optional[Stamp]) -> bool:
    """Hard-disable Venice reasoning on the latency-critical typed narrator path by default.

    GLM 5.2 advertises reasoning but no effort dial. Venice's outer `reasoning.enabled=false`
    and `disable_thinking=true` switches are therefore the reliable binary control. Scope this to
    typed narrator traffic on a Venice upstream: Creator, extraction, other providers, and
    untyped transparent relay requests remain untouched. The local config switch is the explicit
    escape hatch for users who prefer frontend/provider defaults.
    """
    if not _narrator_reasoning_hard_off_required(cfg, stamp):
        return False

    before = json.dumps({
        "reasoning": doc.get("reasoning"),
        "reasoning_effort": doc.get("reasoning_effort"),
        "venice_parameters": doc.get("venice_parameters"),
    }, sort_keys=True, default=str)
    reasoning = dict(doc.get("reasoning") or {}) if isinstance(doc.get("reasoning"), dict) else {}
    reasoning.pop("effort", None)
    reasoning["enabled"] = False
    doc["reasoning"] = reasoning
    doc.pop("reasoning_effort", None)
    venice = (dict(doc.get("venice_parameters") or {})
              if isinstance(doc.get("venice_parameters"), dict) else {})
    venice["disable_thinking"] = True
    venice["strip_thinking_response"] = True
    doc["venice_parameters"] = venice
    after = json.dumps({
        "reasoning": doc.get("reasoning"),
        "reasoning_effort": doc.get("reasoning_effort"),
        "venice_parameters": doc.get("venice_parameters"),
    }, sort_keys=True, default=str)
    return after != before


@dataclass
class LocalResponse:
    """A complete local assistant reply that the relay may serve without an upstream call."""
    story: str
    model: str
    stream: bool
    provenance: str


@dataclass(frozen=True)
class PendingSemanticSelection:
    """One sealed IDs-only selector request bound to an exact durable fallback attempt."""

    fallback_artifact: EnvelopeArtifact
    expectation: FallbackPromotionExpectation
    preparation: NarrationSelectionPreparation


@dataclass
class PostContext:
    """What the response tee needs to finish the turn after the stream ends."""
    session_id: str
    branch_id: str
    turn_index: int
    klass: str
    speaker: Optional[str] = None
    card_role: Optional[str] = None
    card: str = ""                # Q23: genesis stage-B inputs (new_session only)
    opening: str = ""
    evolutions: Optional[list] = None   # RPG-5 crossings; cold path schedules re-authoring
    enriched: bool = False              # request was enriched; feeds prompt-cache statistics
    response_key: str = ""              # exact request identity; first completed response wins
    network_duplicate: bool = False     # observability only; response_key owns idempotence
    suppress_cold_path: bool = False    # cache-miss retry cannot attach to newer live state
    local_response: Optional[LocalResponse] = None
    request_model: str = ""             # safe response/latency correlation; never credentials
    semantic_replay: Optional[ReplayArtifact] = None
    semantic_selection: Optional[PendingSemanticSelection] = None
    semantic_gate: bool = False
    semantic_error: str = ""
    semantic_status: int = 503
    narration_guard: Optional[NarrationGuardBasis] = None
    narration_guard_replaced: bool = False
    narration_guard_reasons: tuple[str, ...] = ()
    player_lesson_receipt_turn: Optional[int] = None
    player_lesson_ids: tuple[str, ...] = ()
    player_lesson_delivered: bool = False
    player_lesson_intent_ids: tuple[str, ...] = ()
    player_lesson_intent_applications_pending: tuple[dict, ...] = ()


class Pipeline:
    def __init__(
        self,
        store: Store,
        engine: SessionEngine,
        cfg: Config,
        jobs=None,
        rng: Optional[random.Random] = None,
        playerlex_service=_DEFAULT_PLAYERLEX_SERVICE,
        player_lessons_service=_DEFAULT_PLAYER_LESSONS_SERVICE,
    ) -> None:
        self.store, self.engine, self.cfg, self.jobs = store, engine, cfg, jobs
        self.rng = rng or random.Random()
        if playerlex_service is _DEFAULT_PLAYERLEX_SERVICE:
            self.playerlex_service = self._initialize_playerlex_service()
        else:
            if playerlex_service is not None \
                    and not callable(getattr(playerlex_service, "propose", None)):
                raise TypeError("playerlex_service must expose propose(text)")
            self.playerlex_service = playerlex_service
        if player_lessons_service is _DEFAULT_PLAYER_LESSONS_SERVICE:
            self.player_lessons_service = self._initialize_player_lessons_service()
        else:
            required = ("select", "rehydrate", "mark_delivered")
            if player_lessons_service is not None and any(
                not callable(getattr(player_lessons_service, name, None)) for name in required
            ):
                raise TypeError(
                    "player_lessons_service must expose select, rehydrate, and mark_delivered"
                )
            self.player_lessons_service = player_lessons_service
        # ---- Phase 0a: prompt-cache plumbing (all in-memory, all fail-open) ----
        self.cache = promptcache.CacheStats()
        self._last_docs: OrderedDict[str, dict] = OrderedDict()   # sid -> last enriched doc
        # Exact network retries get their exact already-enriched packet.  The body hash and
        # authoritative turn identity prevent a later turn from receiving stale transient data.
        self._request_packets: OrderedDict[str, tuple[bytes, PostContext]] = OrderedDict()
        self._completed_responses: OrderedDict[str, float] = OrderedDict()
        self._prewarm_at: dict[str, float] = {}                   # sid -> monotonic cooldown
        # 2026-07-10 (Eranmor, pillar 17): tier0 notices used to die in the proxy log —
        # "recharging"/non-move/unknown-skill were invisible to the player. A bounded
        # in-memory ring per session feeds the HUD rolls lane (transient UX, not state).
        self._notices: OrderedDict[str, list] = OrderedDict()
        self._transport_errors: OrderedDict[str, dict] = OrderedDict()
        if getattr(cfg.upstream, "cache_key", True) \
                and cfg.injection.placement == "system_merge":
            log.info("prompt-cache: placement=system_merge splices volatile state into the "
                     "FIRST system message — every turn invalidates the provider's prefix "
                     "cache; placement=depth (default) keeps volatile bytes at the tail")

    def _initialize_playerlex_service(self):
        """Initialize one process-local service before any live turn transaction begins."""
        try:
            from .playerlex import PlayerLex
            from .semantic_atlas import load_default_semantic_atlas

            return PlayerLex(
                self.store.db,
                load_default_semantic_atlas(),
                self.store.apply_guard(),
            )
        except Exception as exc:
            # PlayerLex is optional recognition.  A foreign/corrupt local catalog must remain
            # visible to its control surface without taking the shared semantic path offline.
            log.warning(
                "PlayerLex live recognition unavailable during initialization (%s)",
                type(exc).__name__,
            )
            return None

    def _playerlex_proposal(self, text: str) -> Mapping:
        """Call the cached local proposal service once and fail open to an empty overlay."""
        service = self.playerlex_service
        if service is None:
            return {
                "schema": "playerlex-recognition-proposal/1",
                "match_count": 0,
                "matches": [],
                "refused": [],
            }
        try:
            proposal = service.propose(text)
        except Exception as exc:
            log.warning("PlayerLex live recognition unavailable (%s)", type(exc).__name__)
            proposal = None
        if isinstance(proposal, Mapping):
            return proposal
        return {
            "schema": "playerlex-recognition-proposal/1",
            "match_count": 0,
            "matches": [],
            "refused": [],
        }

    def _initialize_player_lessons_service(self):
        """Initialize the separate local narration-preference service once per process."""
        try:
            from .player_lessons import PlayerLessons

            return PlayerLessons(
                self.store.db,
                playerlex=self.playerlex_service,
                lock=self.store.apply_guard(),
            )
        except Exception as exc:
            # Player Lessons are prompt-only and fail open. Their own Console surface will retry
            # initialization and report a concrete local-storage error to the Player.
            log.warning(
                "Player Lessons unavailable during initialization (%s)",
                type(exc).__name__,
            )
            return None

    def _player_lessons_lifecycle_guard(self):
        """Return the service guard that orders live use against mutation and cache eviction."""
        service = self.player_lessons_service
        guard = getattr(service, "lifecycle_guard", None) if service is not None else None
        return guard() if callable(guard) else nullcontext()

    def bind_playerlex_service(self, service) -> None:
        """Atomically publish a recovered PlayerLex service to both live consumers."""
        if service is not None and not callable(getattr(service, "propose", None)):
            raise TypeError("playerlex_service must expose propose(text)")
        with self._player_lessons_lifecycle_guard():
            lessons = self.player_lessons_service
            bind = getattr(lessons, "bind_playerlex", None) if lessons is not None else None
            if service is not None and callable(bind):
                bind(service)
            # Publish last. A concurrent turn therefore observes old/old or new/new, never a new
            # recognition service paired with a lesson service that still rejects its anchors.
            self.playerlex_service = service

    def _repair_player_lesson_intent_applications(
        self, ctx: Optional[PostContext]
    ) -> bool:
        """Retry one content-free application receipt without rerunning interpretation."""
        if ctx is None or not ctx.player_lesson_intent_applications_pending:
            return True
        service = self.player_lessons_service
        record = getattr(service, "repair_intent_applications", None) \
            if service is not None else None
        if not callable(record) and service is not None:
            record = getattr(service, "record_intent_applications", None)
        if not callable(record):
            return False
        try:
            record(
                ctx.branch_id,
                ctx.turn_index,
                list(ctx.player_lesson_intent_applications_pending),
            )
        except Exception as exc:
            log.warning(
                "Player Lesson intent application receipt repair unavailable (%s)",
                type(exc).__name__,
            )
            return False
        ctx.player_lesson_intent_applications_pending = ()
        return True

    def mark_player_lessons_delivered(self, ctx: Optional[PostContext]) -> bool:
        """Mark only preferences carried by a request that reached the upstream narrator.

        Selection and prompt composition are not delivery.  The proxy calls this after the
        upstream request returns response headers, so local responses, truth-gated selectors,
        configuration failures, and connection failures cannot produce false delivery evidence.
        """
        if ctx is None:
            return False
        self._repair_player_lesson_intent_applications(ctx)
        if ctx.player_lesson_delivered or not ctx.player_lesson_ids:
            return False
        receipt_turn = ctx.player_lesson_receipt_turn
        service = self.player_lessons_service
        if service is None or receipt_turn is None:
            return False
        try:
            service.mark_delivered(
                ctx.branch_id,
                receipt_turn,
                ctx.player_lesson_ids,
            )
        except Exception as exc:
            # Delivery observability never blocks an already transported narrator request.  Leave
            # the flag false so an exact retry can finish the content-free receipt idempotently.
            log.warning(
                "Player Lessons delivery marker unavailable (%s)",
                type(exc).__name__,
            )
            return False
        ctx.player_lesson_delivered = True
        return True

    def forget_player_lesson(self, lesson_id: str) -> dict[str, int]:
        """Forget active process-local prompt copies after secure lesson removal."""
        removed_packets = 0
        for request_key, (_packet, ctx) in tuple(self._request_packets.items()):
            if lesson_id in ctx.player_lesson_ids \
                    or lesson_id in ctx.player_lesson_intent_ids:
                del self._request_packets[request_key]
                removed_packets += 1
        # Prewarm documents contain rendered prompt text but intentionally carry no durable lesson
        # identity.  Clear the bounded caches rather than risk retaining removed private text.
        removed_prewarm = len(self._last_docs)
        self._last_docs.clear()
        self._prewarm_at.clear()
        return {
            "request_packets": removed_packets,
            "prewarm_documents": removed_prewarm,
        }

    @staticmethod
    def _recognized_meanings(t0_result) -> tuple[tuple[str, str, str], ...]:
        """Project exact recognition identities without carrying source prose into receipts."""
        from .semantic_fabric import load_default_semantic_fabric

        semantic_turn = getattr(t0_result, "semantic_turn", None)
        meaning = getattr(semantic_turn, "compiled_meaning", None)
        matches = getattr(meaning, "matches", ())
        try:
            fabric = load_default_semantic_fabric()
        except Exception:
            fabric = None
        out: set[tuple[str, str, str]] = set()
        for match in matches:
            lex_id = str(getattr(match, "lex_id", ""))
            concept_id = str(getattr(match, "concept_id", ""))
            surface_baseline = str(getattr(match, "surface_baseline", ""))
            fingerprint = ""
            features = getattr(match, "features", None)
            if surface_baseline == "playerlex":
                fingerprint = str(getattr(match, "entry_fingerprint", ""))
            elif lex_id == "capability" and isinstance(features, Mapping):
                fingerprint = str(features.get("meaning_fingerprint") or "")
            elif fabric is not None and lex_id in {"referent", "scene", "action"}:
                try:
                    entry = fabric.entry(concept_id)
                    if entry.lex_id == lex_id:
                        fingerprint = str(entry.meaning_fingerprint)
                except Exception:
                    fingerprint = ""
            if lex_id and concept_id and fingerprint:
                out.add((lex_id, concept_id, fingerprint))
        return tuple(sorted(out))

    @staticmethod
    def _recognized_intent_meanings(semantic_turn) -> tuple[dict[str, object], ...]:
        """Project exact PlayerLex approval spans without copying matched Player prose."""
        meaning = getattr(semantic_turn, "compiled_meaning", None)
        matches = getattr(meaning, "matches", ())
        rows: list[dict[str, object]] = []
        for match in matches:
            if str(getattr(match, "surface_baseline", "")) != "playerlex":
                continue
            lex_id = str(getattr(match, "lex_id", ""))
            concept_id = str(getattr(match, "concept_id", ""))
            meaning_fingerprint = str(getattr(match, "entry_fingerprint", ""))
            start, end = getattr(match, "start", None), getattr(match, "end", None)
            if (
                lex_id not in {"action", "referent"}
                or not concept_id
                or not meaning_fingerprint.startswith("sha256:")
                or isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end <= start
            ):
                continue
            for source_id in getattr(match, "source_ids", ()):
                source_id = str(source_id)
                if not source_id.startswith("playerlex.") or ".r" not in source_id:
                    continue
                rows.append(
                    {
                        "lex_id": lex_id,
                        "concept_id": concept_id,
                        "meaning_fingerprint": meaning_fingerprint,
                        "source_start": start,
                        "source_end": end,
                        "approval_source_id": source_id,
                    }
                )
        rows.sort(
            key=lambda row: (
                int(row["source_start"]),
                int(row["source_end"]),
                str(row["lex_id"]),
                str(row["concept_id"]),
                str(row["approval_source_id"]),
            )
        )
        return tuple(rows)

    @staticmethod
    def _player_lesson_rows(value) -> list[dict]:
        """Accept the service's bounded projection while failing closed on malformed rows."""
        if isinstance(value, Mapping):
            value = value.get("lessons", value.get("selected", []))
        if not isinstance(value, (list, tuple)):
            return []
        return [dict(row) for row in value[:5] if isinstance(row, Mapping)]

    def _cached_player_lessons_current(self, ctx: PostContext) -> bool:
        """Fail closed when a cached narration packet's frozen lessons are no longer usable."""
        if not ctx.player_lesson_ids:
            return True
        service = self.player_lessons_service
        receipt_turn = ctx.player_lesson_receipt_turn
        rehydrate = getattr(service, "rehydrate", None) if service is not None else None
        if receipt_turn is None or not callable(rehydrate):
            return False
        try:
            selection = rehydrate(ctx.branch_id, receipt_turn)
        except Exception as exc:
            log.warning(
                "Player Lesson cached selection recheck unavailable (%s)",
                type(exc).__name__,
            )
            return False
        current_ids = tuple(
            str(row["lesson_id"])
            for row in self._player_lesson_rows(selection)
            if isinstance(row.get("lesson_id"), str)
        )
        return current_ids == ctx.player_lesson_ids

    def _semantic_truth_gate_enabled(self) -> bool:
        spec = getattr(self.cfg, "specialization", None)
        return bool(
            spec is not None
            and getattr(spec, "name", "none") == "rpg"
            and getattr(spec, "semantic_truth_gate", False)
        )

    def _semantic_explicit_passthrough(self, stamp: Optional[Stamp]) -> bool:
        """Honor only an already-bound session's explicit passthrough kill switch."""
        if stamp is None or not stamp.session:
            return False
        try:
            row = self.store.db.execute(
                "SELECT mode FROM sessions WHERE external_id=?", (stamp.session,)
            ).fetchone()
            return bool(row is not None and row["mode"] == "passthrough")
        except Exception:
            return False

    @staticmethod
    def _semantic_failure_context(
        stamp: Optional[Stamp], res=None, *, status: int = 503, code: str
    ) -> PostContext:
        effective = (res.stamp if res is not None else None) or stamp
        return PostContext(
            res.session_id if res is not None else "",
            res.branch_id if res is not None else "",
            int(res.turn_index) if res is not None else -1,
            res.klass.value if res is not None else str(getattr(stamp, "gen_type", "unknown")),
            speaker=(effective.speaker if effective else None),
            card_role=(effective.card_role if effective else None),
            suppress_cold_path=True,
            request_model="",
            semantic_gate=True,
            semantic_error=code,
            semantic_status=status,
        )

    def _semantic_ephemeral_snapshot(self) -> dict:
        """Capture process-local mutations that the SQLite proof transaction cannot roll back."""
        try:
            rng_state = self.rng.getstate()
        except AttributeError:
            rng_state = None
        jobs_models = getattr(self.jobs, "models", None) if self.jobs is not None else None
        jobs_users = getattr(self.jobs, "user_names", None) if self.jobs is not None else None
        return {
            "rng_state": rng_state,
            "last_docs": OrderedDict(self._last_docs),
            "request_packets": OrderedDict(self._request_packets),
            "notices": deepcopy(self._notices),
            "jobs_models": dict(jobs_models) if isinstance(jobs_models, dict) else None,
            "jobs_users": dict(jobs_users) if isinstance(jobs_users, dict) else None,
        }

    def _semantic_observation_snapshot(self) -> dict:
        """Capture SessionEngine state that cannot participate in SQLite rollback."""
        # The bounded request-dedup map is cheap to copy.  The prefix index may contain an entire
        # long-running library of chats, so never clone it on every turn; on the rare rollback path
        # rebuild it from the authoritative Store exactly as restart recovery does.
        return {"dedup": deepcopy(self.engine._dedup)}

    def _restore_semantic_observation(self, snapshot: dict) -> None:
        self.engine.index = SessionEngine(self.store, self.engine.cfg).index
        self.engine._dedup = deepcopy(snapshot["dedup"])

    def _bootstrap_semantic_new_session(self, res, body: bytes) -> None:
        """Prove and persist deterministic T0 before a new visible turn can be reserved.

        The caller holds one outer Store transaction spanning SessionEngine's fresh session rows,
        deterministic genesis, the strict replay proof, and proof persistence.  Stage-B model
        genesis is deliberately excluded and remains a later cold-path concern.
        """
        if res.klass.value != "new_session":
            return
        if getattr(res, "replay_reason", "") in {"resume_reserved", "lost_reply"}:
            if self.store.semantic_bootstrap_proof(res.session_id, res.branch_id) is None:
                raise SemanticGateConflict(
                    "replayed fresh semantic turn lost its committed bootstrap proof"
                )
            return
        if bool(res.duplicate):
            if self.store.semantic_bootstrap_proof(res.session_id, res.branch_id) is None:
                raise SemanticGateConflict(
                    "duplicate fresh semantic request has no committed bootstrap proof"
                )
            return
        if isinstance(res.turn_index, bool) or int(res.turn_index) <= 0:
            raise SemanticGateConflict(
                "fresh semantic RPG sessions require a separately proved T0 before visible T1"
            )
        if self.store.semantic_bootstrap_proof(res.session_id) is not None:
            raise SemanticGateConflict("fresh semantic session already has a bootstrap proof")
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SemanticGateConflict("semantic bootstrap request is not valid JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            raise SemanticGateConflict("semantic bootstrap request has no canonical messages")

        before = self.store.journal_high_water()
        pre_state = current_state(self.store, res.branch_id)
        effective = res.stamp
        genesis.seed_rules(
            self.store,
            self.cfg,
            res.session_id,
            res.branch_id,
            document,
            speaker=(effective.speaker if effective else "") or "",
            card_role=(effective.card_role if effective else "") or "",
        )
        genesis.seed_player(
            self.store,
            self.cfg,
            res.session_id,
            res.branch_id,
            document,
        )
        after = self.store.journal_high_water()
        rows = self.store.journal_window(
            res.branch_id,
            after_id=before,
            through_id=after,
        )
        post_state = current_state(self.store, res.branch_id)
        proof = build_semantic_bootstrap_proof(
            session_id=res.session_id,
            branch_id=res.branch_id,
            pre_bootstrap_state=pre_state,
            post_bootstrap_state=post_state,
            journal_high_water_before=before,
            journal_high_water_after=after,
            journal_rows=rows,
        )
        self.store.persist_semantic_bootstrap_proof(proof)
        pending = (post_state.get("combat") or {}).get("pending_intent")
        key = self._semantic_pre_mutation_key(
            res,
            body,
            pre_ledger_hash=proof.post_bootstrap_state_fingerprint,
            pending_intent_fingerprint=fingerprint(
                pending if isinstance(pending, dict) else None
            ),
        )
        request_hash = self._semantic_initial_request_hash(key, res.klass.value, body)
        self.store.write_turn_hashes(
            res.branch_id,
            res.turn_index,
            user_hash=key["player_input_hash"],
        )
        reservation = self.store.turn_lifecycle.reserve(key, request_hash=request_hash)
        if not reservation.created or reservation.status != "reserved":
            raise SemanticGateConflict(
                "fresh semantic T1 lifecycle was not reserved with its bootstrap proof"
            )

    def _restore_semantic_ephemeral(self, snapshot: dict) -> None:
        rng_state = snapshot.get("rng_state")
        if rng_state is not None:
            try:
                self.rng.setstate(rng_state)
            except AttributeError:
                pass
        self._last_docs = OrderedDict(snapshot["last_docs"])
        self._request_packets = OrderedDict(snapshot["request_packets"])
        self._notices = deepcopy(snapshot["notices"])
        if self.jobs is not None:
            models = getattr(self.jobs, "models", None)
            if isinstance(models, dict) and snapshot["jobs_models"] is not None:
                models.clear()
                models.update(snapshot["jobs_models"])
            users = getattr(self.jobs, "user_names", None)
            if isinstance(users, dict) and snapshot["jobs_users"] is not None:
                users.clear()
                users.update(snapshot["jobs_users"])

    def _semantic_request_key(self, res, body: bytes) -> str:
        attempt_index = 0
        if res.klass.value == "swipe":
            deferred_count = getattr(res, "deferred_swipe_count", None)
            if isinstance(deferred_count, int) and not isinstance(deferred_count, bool):
                attempt_index = deferred_count
            else:
                row = self.store.db.execute(
                    "SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                    (res.branch_id, res.turn_index),
                ).fetchone()
                attempt_index = int(row["swipe_count"] or 0) if row else 0
        body_key = hashlib.blake2b(body, digest_size=16).hexdigest()
        return (
            f"{res.branch_id}:{res.turn_index}:{res.klass.value}:"
            f"{attempt_index}:{body_key}"
        )

    @staticmethod
    def _semantic_initial_request_hash(key: dict, turn_class: str, body: bytes) -> str:
        return fingerprint({
            "schema": "semantic-turn-request/1",
            "lifecycle_key": key["lifecycle_key"],
            "turn_class": str(turn_class),
            "request_bytes": raw_fingerprint(body),
        })

    def _semantic_pre_mutation_key(
        self,
        res,
        body: bytes,
        *,
        pre_ledger_hash: str,
        pending_intent_fingerprint: str,
    ) -> dict:
        doc = json.loads(body)
        messages = doc.get("messages")
        if not isinstance(messages, list):
            raise ValueError("semantic turn has no canonical transcript")
        canon = canonicalize(messages)
        player = next(
            (row for row in reversed(canon) if row.role in {"user", "text"}),
            None,
        )
        if player is None:
            raise ValueError("semantic turn has no current Player input")
        rows = self.store.get_msgs(res.branch_id)
        if not rows:
            raise ValueError("semantic turn has no persisted canonical input")
        current = rows[-1]
        if current["role"] not in {"user", "text"} \
                or current["content_hash"] != player.content_hash:
            raise ValueError("semantic turn input differs from the canonical branch tip")
        position = int(current["pos"])
        accepted_head = (
            EMPTY_PREFIX_HASH if position == 0 else str(rows[position - 1]["chain_hash"])
        )
        return build_pre_mutation_key(
            session_id=res.session_id,
            branch_id=res.branch_id,
            turn_index=int(res.turn_index),
            accepted_prefix_pos=position,
            accepted_head_hash=accepted_head,
            player_input_hash=player.content_hash,
            pre_ledger_hash=pre_ledger_hash,
            pending_intent_fingerprint=pending_intent_fingerprint,
            semantic_contract_version=SEMANTIC_RUNTIME_CONTRACT_VERSION,
        )

    def _semantic_config_fingerprint(self) -> str:
        spec = getattr(self.cfg, "specialization", None)
        spec_payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(vars(spec or {}))
        spec_payload.pop("narrator_card_dir", None)
        extraction = getattr(self.cfg, "extraction", None)
        session = getattr(self.cfg, "session", None)
        return fingerprint({
            "schema": "semantic-runtime-config/1",
            "specialization": spec_payload,
            "extraction": {
                "mode": getattr(extraction, "mode", ""),
                "live_recalc": bool(getattr(extraction, "live_recalc", True)),
            },
            "session": {
                "reserve_lost_turns": bool(getattr(session, "reserve_lost_turns", True)),
            },
            "user_identity_hash": content_hash(
                str(getattr(getattr(self.cfg, "user_guard", None), "name", "") or "")
            ),
        })

    def _semantic_rng_fingerprint(self) -> str:
        try:
            state = self.rng.getstate()
        except AttributeError:
            state = repr(self.rng)
        return fingerprint({"schema": "semantic-rng-state/1", "state": repr(state)})

    @staticmethod
    def _semantic_contract_rows(contract: dict) -> tuple[list[dict], list[dict]]:
        occurrences: dict[str, dict] = {}
        effects: list[dict] = []
        for claim in contract.get("expected_claims") or []:
            occurrence_id = str(claim["occurrence_ref"])
            row = occurrences.setdefault(occurrence_id, {
                "occurrence_id": occurrence_id,
                "cause_refs": [],
                "construction_refs": [],
                "actor_id": claim.get("actor_id"),
                "subject_ids": [],
            })
            for field, value in (
                ("cause_refs", claim.get("cause_ref")),
                ("construction_refs", claim.get("construction_ref")),
            ):
                if value not in row[field]:
                    row[field].append(value)
            for subject in claim.get("subject_ids") or []:
                if subject not in row["subject_ids"]:
                    row["subject_ids"].append(subject)
            effects.append({
                "effect_id": str(claim["claim_ref"]),
                "occurrence_id": occurrence_id,
                "kind": claim["kind"],
                "detail": claim["detail"],
                "amount": claim.get("amount"),
                "polarity": claim["polarity"],
                "actuality": claim["actuality"],
                "time_scope": claim["time_scope"],
                "multiplicity": claim["multiplicity"],
            })
        ordered_occurrences = []
        for occurrence_id in sorted(occurrences):
            row = occurrences[occurrence_id]
            row["cause_refs"].sort()
            row["construction_refs"].sort()
            row["subject_ids"].sort()
            ordered_occurrences.append(row)
        return ordered_occurrences, sorted(effects, key=lambda row: row["effect_id"])

    @staticmethod
    def _semantic_intent_ids(contract: dict, state: dict) -> tuple[str | None, str | None]:
        consumed = sorted({
            str(row["intent_ref"])
            for row in contract.get("opposition_actions") or []
            if row.get("intent_ref")
        })
        if len(consumed) > 1:
            raise ValueError("one semantic turn consumed multiple opposition intents")
        pending = (state.get("combat") or {}).get("pending_intent")
        next_intent = str(pending.get("id")) if isinstance(pending, dict) and pending.get("id") else None
        consumed_id = consumed[0] if consumed else None
        if consumed_id is not None and next_intent == consumed_id:
            raise ValueError("consumed opposition intent remained current")
        return consumed_id, next_intent

    def _semantic_replay_context(self, res, body: bytes, replay: ReplayArtifact,
                                  stamp: Optional[Stamp]) -> PostContext:
        effective = res.stamp or stamp
        try:
            request_model = str(json.loads(body).get("model") or "")
        except (json.JSONDecodeError, AttributeError, TypeError):
            request_model = ""
        ctx = PostContext(
            res.session_id,
            res.branch_id,
            res.turn_index,
            res.klass.value,
            speaker=(effective.speaker if effective else None),
            card_role=(effective.card_role if effective else None),
            response_key=(
                f"semantic:{replay.lifecycle_key}:{replay.attempt_index}:"
                f"{replay.selected_artifact_digest}"
            ),
            request_model=request_model,
            semantic_replay=replay,
            semantic_gate=True,
        )
        self._semantic_bind_replay(ctx, replay)
        return ctx

    @staticmethod
    def _semantic_bind_replay(ctx: PostContext, replay: ReplayArtifact) -> None:
        """Bind a response context only to the exact artifact the proxy may next deliver."""
        ctx.semantic_replay = replay
        ctx.semantic_selection = None
        ctx.semantic_gate = True
        ctx.semantic_error = ""
        ctx.local_response = None
        ctx.response_key = (
            f"semantic:{replay.lifecycle_key}:{replay.attempt_index}:"
            f"{replay.selected_artifact_digest}"
        )

    def _semantic_selection_expectation(
        self,
        fallback: EnvelopeArtifact,
        replay: ReplayArtifact,
    ) -> FallbackPromotionExpectation:
        """Read the current lifecycle facts that the promotion transaction must CAS again."""
        with self.store._lock:
            lifecycle = self.store.db.execute(
                "SELECT session_id, branch_id, turn_index, status, active_attempt_index"
                " FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                (replay.lifecycle_key,),
            ).fetchone()
            attempt = self.store.db.execute(
                "SELECT status, fallback_envelope_fingerprint"
                " FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
                (replay.lifecycle_key, replay.attempt_index),
            ).fetchone()
            delivery_claim = self.store.db.execute(
                "SELECT 1 FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (replay.lifecycle_key, replay.attempt_index),
            ).fetchone()
        key = fallback.envelope.get("pre_mutation_key") or {}
        if lifecycle is None or attempt is None \
                or lifecycle["status"] != "committed" \
                or lifecycle["session_id"] != key.get("session_id") \
                or lifecycle["branch_id"] != key.get("branch_id") \
                or int(lifecycle["turn_index"]) != key.get("turn_index") \
                or attempt["status"] != replay.status \
                or attempt["fallback_envelope_fingerprint"] \
                != fallback.envelope.get("envelope_fingerprint") \
                or lifecycle["active_attempt_index"] is None:
            raise SemanticGateConflict(
                "semantic selection fallback differs from its durable lifecycle"
            )
        return build_fallback_promotion_expectation(
            fallback,
            lifecycle_status=str(attempt["status"]),
            active_attempt_index=int(lifecycle["active_attempt_index"]),
            delivery_claimed=delivery_claim is not None,
        )

    def _semantic_promote_or_replay(self, artifact: EnvelopeArtifact) -> ReplayArtifact:
        """CAS one accepted/fallback artifact, or recover only the current safe replay."""
        lifecycle_key = str(artifact.envelope.get("lifecycle_key") or "")
        try:
            return self.store.turn_lifecycle.promote_candidate(artifact)
        except Exception:
            try:
                return self.store.turn_lifecycle.replay(
                    lifecycle_key,
                    reason="reopen",
                )
            except Exception as replay_error:
                raise SemanticGateConflict(
                    "semantic selection has no recoverable proof-carrying artifact"
                ) from replay_error

    def _semantic_route_replay(
        self,
        res,
        original_request: bytes,
        replay: ReplayArtifact,
        stamp: Optional[Stamp],
        ctx: Optional[PostContext] = None,
    ) -> tuple[bytes, PostContext]:
        """Return a sealed selector request for fallback-ready, otherwise replay only."""
        if ctx is None:
            ctx = self._semantic_replay_context(res, original_request, replay, stamp)
        else:
            self._semantic_bind_replay(ctx, replay)
        if replay.status in {"accepted", "fallback_final"}:
            # The proxy must claim and replay this artifact; these bytes are not an upstream call.
            return original_request, ctx
        if replay.status != "fallback_ready":
            raise SemanticGateConflict("semantic narration attempt is not proof-ready")

        fallback = EnvelopeArtifact(replay.envelope, replay.payload)
        try:
            expectation = self._semantic_selection_expectation(fallback, replay)
            preparation = prepare_narration_selection(
                fallback,
                original_request,
                expectation,
                reasoning_hard_off=_narrator_reasoning_hard_off_required(
                    self.cfg,
                    res.stamp or stamp,
                ),
            )
        except Exception:
            preparation = None
        if preparation is None or preparation.action != "request" \
                or preparation.prepared is None:
            terminal = self._semantic_promote_or_replay(fallback)
            self._semantic_bind_replay(ctx, terminal)
            return original_request, ctx

        ctx.semantic_selection = PendingSemanticSelection(
            fallback_artifact=fallback,
            expectation=expectation,
            preparation=preparation,
        )
        # This is a fresh, code-owned non-stream packet.  No enriched transcript or Player prose
        # from ``original_request`` is forwarded alongside it.
        return preparation.prepared.transport_request.request_bytes, ctx

    def complete_semantic_selection(
        self,
        ctx: PostContext,
        response_bytes: Optional[bytes] = None,
        response_content_type: Optional[str] = None,
        *,
        timed_out: bool = False,
        upstream_error: bool = False,
    ) -> ReplayArtifact:
        """Resolve one fully buffered selector response and CAS its safe terminal artifact.

        The proxy calls this before any selector bytes can become visible.  An upstream error,
        timeout, malformed response, stale attempt, concurrent swipe, or prior delivery claim can
        expose only the exact persisted fallback/current terminal replay, never the raw response.
        """
        if not isinstance(ctx, PostContext) or not ctx.semantic_gate:
            raise SemanticGateConflict("semantic selection has no gated response context")
        pending = ctx.semantic_selection
        if pending is None:
            replay = ctx.semantic_replay
            if replay is None:
                raise SemanticGateConflict("semantic selection has no proof-carrying replay")
            try:
                current = self.store.turn_lifecycle.replay(
                    replay.lifecycle_key,
                    reason="reopen",
                )
            except Exception as exc:
                raise SemanticGateConflict(
                    "semantic selection terminal replay is unavailable"
                ) from exc
            self._semantic_bind_replay(ctx, current)
            return current

        fallback_key = pending.fallback_artifact.envelope.get("pre_mutation_key") or {}
        fallback_attempt = pending.fallback_artifact.envelope.get("attempt") or {}
        replay = ctx.semantic_replay
        if replay is None \
                or ctx.session_id != fallback_key.get("session_id") \
                or ctx.branch_id != fallback_key.get("branch_id") \
                or ctx.turn_index != fallback_key.get("turn_index") \
                or replay.lifecycle_key != pending.expectation.lifecycle_key \
                or replay.attempt_index != fallback_attempt.get("index") \
                or replay.status != "fallback_ready" \
                or replay.envelope.get("envelope_fingerprint") \
                != pending.expectation.fallback_envelope_fingerprint:
            raise SemanticGateConflict(
                "semantic selection context differs from its sealed fallback attempt"
            )
        raw = None if upstream_error else response_bytes
        content_type = None if upstream_error else response_content_type
        try:
            decision = resolve_narration_selection(
                pending.preparation,
                pending.fallback_artifact,
                pending.expectation,
                response_bytes=raw,
                response_content_type=content_type,
                timed_out=bool(timed_out),
            )
            artifact = decision.artifact
        except Exception:
            # The pure resolver is fail-closed, but retain the exact fallback even if a future
            # implementation defect escapes its documented rejection set.
            artifact = pending.fallback_artifact
        replay = self._semantic_promote_or_replay(artifact)
        self._semantic_bind_replay(ctx, replay)
        return replay

    def _process_truth_gated(self, stamp: Optional[Stamp], body: bytes, res,
                             ) -> tuple[bytes, PostContext]:
        """Atomically settle one RPG turn with its proof-complete fallback artifact."""
        if getattr(res, "replay_reason", "") == "lost_reply":
            row = self.store.db.execute(
                "SELECT lifecycle_key FROM semantic_turn_lifecycles"
                " WHERE branch_id=? AND turn_index=? AND status='committed'",
                (res.branch_id, res.turn_index),
            ).fetchone()
            if row is None:
                raise SemanticGateConflict("lost reply has no proof-carrying semantic turn")
            replay = self.store.turn_lifecycle.replay(
                row["lifecycle_key"], reason="lost_reply"
            )
            return self._semantic_route_replay(res, body, replay, stamp)
        if res.klass.value == "swipe":
            return self._process_truth_gated_swipe(stamp, body, res)
        if res.klass.value == "continue":
            raise SemanticGateConflict(
                "semantic continuation is disabled until it has an explicit realization contract"
            )
        if res.klass.value not in {"new_turn", "new_session", "edit_fork", "impersonate"}:
            raise ValueError("unsupported semantic turn class")

        occupied = self.store.db.execute(
            "SELECT key_json FROM semantic_turn_lifecycles"
            " WHERE branch_id=? AND turn_index=?",
            (res.branch_id, res.turn_index),
        ).fetchone()
        if occupied is None:
            pre_state = current_state(self.store, res.branch_id)
            pre_hash = fingerprint(pre_state)
            pending = (pre_state.get("combat") or {}).get("pending_intent")
            pending_hash = fingerprint(pending if isinstance(pending, dict) else None)
            key = self._semantic_pre_mutation_key(
                res,
                body,
                pre_ledger_hash=pre_hash,
                pending_intent_fingerprint=pending_hash,
            )
        else:
            key = validate_pre_mutation_key(json.loads(occupied["key_json"]))
            candidate = self._semantic_pre_mutation_key(
                res,
                body,
                pre_ledger_hash=key["pre_ledger_hash"],
                pending_intent_fingerprint=key["pending_intent_fingerprint"],
            )
            if candidate != key:
                raise SemanticGateConflict(
                    "turn slot belongs to a different canonical semantic input"
                )
            pre_hash = key["pre_ledger_hash"]
        request_hash = self._semantic_initial_request_hash(key, res.klass.value, body)
        reservation = self.store.turn_lifecycle.reserve(key, request_hash=request_hash)
        if reservation.status in {"fallback_ready", "fallback_final", "accepted"}:
            replay = self.store.turn_lifecycle.replay(
                reservation.lifecycle_key,
                attempt_index=reservation.attempt_index,
                reason="retry",
            )
            return self._semantic_route_replay(res, body, replay, stamp)
        if reservation.status != "reserved":
            raise ValueError("semantic turn reservation is not settleable")

        config_hash = self._semantic_config_fingerprint()
        rng_hash = self._semantic_rng_fingerprint()
        try:
            request_doc = json.loads(body)
            model = str(request_doc.get("model") or "aetherstate-semantic")
            stream = bool(request_doc.get("stream"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            model, stream = "aetherstate-semantic", False
        holder: dict[str, object] = {}
        fresh_res = replace(res, duplicate=False)

        def mutate() -> FencedMutationOutput:
            packet, ctx = self._process_observed(stamp, body, fresh_res)
            if ctx is None:
                raise ValueError("semantic enrichment produced no response context")
            post_state = current_state(self.store, res.branch_id)
            holder["packet"] = packet
            holder["ctx"] = ctx
            return FencedMutationOutput(result=(packet, ctx), post_state=post_state)

        def build_artifact(observed):
            from .narration_artifact_basis import (
                attach_persisted_narration_basis,
                build_persisted_narration_basis,
            )
            from .narration_plan_runtime import build_narration_realization_plan
            from .semantic_truth_runtime import build_fenced_runtime_truth_contract

            if observed.pre_state is None or observed.pre_hash is None:
                raise ValueError("semantic truth settlement lacks a fenced pre-state observation")
            truth = build_fenced_runtime_truth_contract(
                pre_state=observed.pre_state,
                post_state=observed.post_state,
                pre_ledger_hash=observed.pre_hash,
                post_ledger_hash=observed.post_ledger_hash,
                journal_rows=observed.journal_rows,
                journal_window_fingerprint=observed.journal_window_fingerprint,
                branch_id=res.branch_id,
                turn_index=res.turn_index,
            )
            contract = truth.truth_contract
            occurrences, effects = self._semantic_contract_rows(contract)
            consumed_intent, next_intent = self._semantic_intent_ids(
                contract, observed.post_state
            )
            fallback = build_proof_complete_fallback(
                contract=contract,
                pre_mutation_key=key,
                reservation=reservation,
                occurrences=occurrences,
                effects=effects,
                rng_fingerprint=rng_hash,
                config_fingerprint=config_hash,
                engine_version=f"aetherstate/{__version__};{SEMANTIC_RUNTIME_CONTRACT_VERSION}",
                pre_ledger_hash=observed.pre_hash,
                mechanics_post_ledger_hash=observed.post_ledger_hash,
                model=model,
                stream=stream,
                consumed_intent_id=consumed_intent,
                next_intent_id=next_intent,
            )
            plan = build_narration_realization_plan(contract)
            basis = build_persisted_narration_basis(
                truth.transition_projection,
                contract,
                plan,
            )
            return attach_persisted_narration_basis(fallback, basis)

        ephemeral = self._semantic_ephemeral_snapshot()
        try:
            replay = self.store.turn_lifecycle.commit_mutation_with_fallback_factory(
                reservation,
                mutate,
                build_artifact,
                expected_pre_ledger_hash=pre_hash,
                pre_state_callback=lambda: current_state(self.store, res.branch_id),
            )
        except Exception:
            self._restore_semantic_ephemeral(ephemeral)
            raise
        packet = holder.get("packet", body)
        ctx = holder.get("ctx")
        if not isinstance(packet, bytes) or not isinstance(ctx, PostContext):
            ctx = self._semantic_replay_context(res, body, replay, stamp)
        else:
            ctx.local_response = None
        return self._semantic_route_replay(res, body, replay, stamp, ctx)

    def _process_truth_gated_swipe(self, stamp: Optional[Stamp], body: bytes, res,
                                    ) -> tuple[bytes, PostContext]:
        row = self.store.db.execute(
            "SELECT * FROM semantic_turn_lifecycles"
            " WHERE branch_id=? AND turn_index=? AND status='committed'",
            (res.branch_id, res.turn_index),
        ).fetchone()
        if row is None:
            raise ValueError("swipe has no committed semantic turn")
        lifecycle_key = str(row["lifecycle_key"])
        request_hash = fingerprint({
            "schema": "semantic-swipe-request/1",
            "lifecycle_key": lifecycle_key,
            "request_bytes": raw_fingerprint(body),
            "request_key": self._semantic_request_key(res, body),
        })
        reservation = self.store.turn_lifecycle.reserve_swipe(
            lifecycle_key,
            request_hash=request_hash,
            expected_post_ledger_hash=str(row["post_ledger_hash"]),
        )
        if reservation.status in {"fallback_ready", "fallback_final", "accepted"}:
            replay = self.store.turn_lifecycle.replay(
                lifecycle_key,
                attempt_index=reservation.attempt_index,
                reason="retry",
            )
            self.engine.finalize_deferred_swipe_index(res)
            if not reservation.created:
                # An incomplete active delivery is already a proof-complete terminal artifact.
                # Recover it byte-for-byte without reopening model selection or promoting its
                # fallback_ready row: either would mutate durable lifecycle state during retry.
                ctx = self._semantic_replay_context(res, body, replay, stamp)
                return body, ctx
            return self._semantic_route_replay(res, body, replay, stamp)
        if reservation.status != "reserved":
            raise ValueError("semantic swipe reservation was refused")

        base = self.store.turn_lifecycle.replay(lifecycle_key, reason="reopen")
        source_fallback = self.store.turn_lifecycle.fallback_artifact(
            lifecycle_key,
            attempt_index=base.attempt_index,
        )
        artifact = derive_swipe_fallback_from_persisted_basis(
            source_fallback,
            base,
            reservation,
        )
        def apply_swipe() -> str:
            self.engine.apply_deferred_swipe_db(res)
            return fingerprint(current_state(self.store, res.branch_id))

        replay = self.store.turn_lifecycle.commit_mutation_with_fallback(
            artifact,
            swipe_callback=apply_swipe,
        )
        self.engine.finalize_deferred_swipe_index(res)
        return self._semantic_route_replay(res, body, replay, stamp)

    # ------------------------------------------------------------------ hot path
    def process(self, stamp: Optional[Stamp], body: bytes) -> tuple[bytes, Optional[PostContext]]:
        """Return one request under the Player Lessons use-versus-mutation ordering guard."""
        with self._player_lessons_lifecycle_guard():
            return self._process_with_player_lessons_guard(stamp, body)

    def _process_with_player_lessons_guard(
        self, stamp: Optional[Stamp], body: bytes
    ) -> tuple[bytes, Optional[PostContext]]:
        """Returns (bytes to forward, tee context | None) after lifecycle ordering is held."""
        gate_enabled = self._semantic_truth_gate_enabled()
        gate_exempt = bool(
            stamp is not None and stamp.gen_type == "quiet"
        ) or self._semantic_explicit_passthrough(stamp)
        if not gate_enabled or gate_exempt:
            res = self.engine.observe(stamp, body)
            if res is None:                   # quiet gen / non-chat payload: passthrough
                return body, None
            return self._process_observed(stamp, body, res)

        res = None
        try:
            observation = self._semantic_observation_snapshot()
            try:
                with self.store.transaction():
                    res = self.engine.observe(stamp, body, defer_swipe=True)
                    if res is None:
                        raise SemanticGateConflict(
                            "visible RPG request has no canonical semantic session resolution"
                        )
                    self._bootstrap_semantic_new_session(res, body)
            except Exception:
                self._restore_semantic_observation(observation)
                raise
            if self.store.session_mode(res.session_id) == "passthrough":
                return body, None
            return self._process_truth_gated(stamp, body, res)
        except SemanticGateConflict as exc:
            log.warning("semantic truth gate refused conflicting turn: %s", type(exc).__name__)
            return body, self._semantic_failure_context(
                stamp,
                res,
                status=409,
                code="semantic_turn_conflict",
            )
        except Exception as exc:
            # A gated RPG turn is deliberately fail-closed. The lifecycle transaction already
            # rolled back mechanics; never expose unchecked upstream prose here.
            log.error("semantic truth gate refused turn: %s", type(exc).__name__)
            return body, self._semantic_failure_context(
                stamp,
                res,
                status=503,
                code="semantic_turn_unavailable",
            )

    def _process_observed(self, stamp: Optional[Stamp], body: bytes, res,
                          ) -> tuple[bytes, Optional[PostContext]]:
        """Established enrichment flow after session identity has been resolved once."""
        # The stamp parsed from this request outranks any session-resolved historical stamp.  A
        # cross-role duplicate must never inherit narrator authority from the earlier request.
        request_stamp = stamp or res.stamp
        narrator_request = request_stamp is not None and request_stamp.card_role == "narrator"
        if getattr(self.cfg.server, "turn_trace", False):
            trace_stamp = request_stamp
            log.info("TURN_TRACE %s", json.dumps({
                "event": "request", "ts": time.time(), "session": res.session_id,
                "branch": res.branch_id, "turn": res.turn_index,
                "class": res.klass.value, "duplicate": res.duplicate,
                "speaker": (trace_stamp.speaker if trace_stamp else None),
                "card_role": (trace_stamp.card_role if trace_stamp else None),
            }, separators=(",", ":"), ensure_ascii=False))
        if self.store.session_mode(res.session_id) == "passthrough":
            return body, None                 # 05 SS7: per-session kill-switch — byte-exact
        body_key = hashlib.blake2b(body, digest_size=16).hexdigest()
        attempt_index = 0
        if res.klass.value == "swipe":
            row = self.store.db.execute(
                "SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                (res.branch_id, res.turn_index)).fetchone()
            attempt_index = int(row["swipe_count"] or 0) if row else 0
        request_key = (f"{res.branch_id}:{res.turn_index}:{res.klass.value}:"
                       f"{attempt_index}:{body_key}")
        if res.duplicate and request_key in self._request_packets:
            packet, cached_ctx = self._request_packets[request_key]
            cached_narration_allowed = not cached_ctx.player_lesson_ids or narrator_request
            if not cached_narration_allowed or not self._cached_player_lessons_current(cached_ctx):
                for lesson_id in cached_ctx.player_lesson_ids:
                    self.forget_player_lesson(lesson_id)
            else:
                self._repair_player_lesson_intent_applications(cached_ctx)
                self._request_packets.move_to_end(request_key)
                # Reuse wire bytes, not original cold-path work: a transport duplicate must not
                # reschedule genesis/evolution side effects when its second response is teed.
                duplicate_ctx = PostContext(
                    res.session_id, res.branch_id, res.turn_index, res.klass.value,
                    speaker=cached_ctx.speaker, card_role=cached_ctx.card_role,
                    card=cached_ctx.card, opening=cached_ctx.opening,
                    evolutions=cached_ctx.evolutions, enriched=cached_ctx.enriched,
                    response_key=request_key, network_duplicate=True,
                    local_response=cached_ctx.local_response,
                    request_model=cached_ctx.request_model,
                    semantic_replay=cached_ctx.semantic_replay,
                    semantic_gate=cached_ctx.semantic_gate,
                    semantic_error=cached_ctx.semantic_error,
                    narration_guard=cached_ctx.narration_guard,
                    player_lesson_receipt_turn=cached_ctx.player_lesson_receipt_turn,
                    player_lesson_ids=cached_ctx.player_lesson_ids,
                    player_lesson_delivered=cached_ctx.player_lesson_delivered,
                    player_lesson_intent_ids=cached_ctx.player_lesson_intent_ids,
                    player_lesson_intent_applications_pending=(
                        cached_ctx.player_lesson_intent_applications_pending
                    ),
                )
                return packet, duplicate_ctx
        if res.duplicate:
            # Never compose an evicted old request against the branch's newest state. The raw
            # request is the safest wire fallback, and its response cannot enter the cold path.
            log.warning("network duplicate packet cache miss; cold path suppressed")
            return body, PostContext(
                res.session_id, res.branch_id, res.turn_index, res.klass.value,
                speaker=(res.stamp.speaker if res.stamp else None),
                card_role=(res.stamp.card_role if res.stamp else None),
                response_key=request_key, network_duplicate=True, suppress_cold_path=True)
        doc = json.loads(body)
        doc, user_context_cleaned = compose.without_attached_user_context(doc)
        action_hash = _last_user_action_hash(doc)
        if action_hash:
            self.store.write_turn_hashes(res.branch_id, res.turn_index, user_hash=action_hash)
        changed = user_context_cleaned
        card = opening = ""
        if res.stamp and res.stamp.card_role == "narrator" and res.stamp.speaker:
            self.store.narrator_speaker_set(res.session_id, res.stamp.speaker)
        if res.klass.value == "new_session" and not res.duplicate:   # Q23 stage A: inline
            card, opening = genesis.card_and_prompt(doc)             # rules seed (sub-ms)
            genesis.seed_rules(self.store, self.cfg, res.session_id, res.branch_id, doc,
                               speaker=(res.stamp.speaker if res.stamp else "") or "",
                               card_role=(res.stamp.card_role if res.stamp else "") or "")
            genesis.seed_player(self.store, self.cfg, res.session_id, res.branch_id, doc)
        if not res.duplicate and res.klass.value == "swipe":
            self._swipe_rollback_guard(res)
        state = current_state(self.store, res.branch_id)
        # Prompt-only combat transition signal. It is computed from the clean Player request and
        # pre-turn ledger before Tier-0 can open the War Room, then carried only into composition.
        # It grants no foe, roll, hostility, or state change.
        opening_assessment = tier0.combat_opening_assessment(
            doc, state, self.cfg, res.klass.value, duplicate=res.duplicate)
        combat_opening = opening_assessment.prompt_signal

        evolutions: list = []
        t0 = None
        reserved = None
        intent_lesson_ids: tuple[str, ...] = ()
        intent_receipt_frozen = False
        intent_application_pending: tuple[dict, ...] = ()
        if not res.duplicate:                 # 08 S7: retries never double-apply
            clean_doc = tier0._strip_ooc(doc) or doc
            reserved = self._reserve_lost_turn(res, doc, state)
            if reserved is None:
                lesson_service = self.player_lessons_service
                intent_selector = None
                intent_mode = compose.current_narration_mode(
                    state,
                    self.cfg,
                    combat_opening=combat_opening,
                )
                if lesson_service is not None \
                        and callable(getattr(lesson_service, "select_intent", None)) \
                        and res.klass.value in ("new_session", "new_turn") \
                        and bool(action_hash) \
                        and bool(intent_mode) \
                        and getattr(
                            getattr(self.cfg, "specialization", None), "name", "none"
                        ) == "rpg" \
                        and not self._semantic_truth_gate_enabled():
                    def intent_selector(semantic_turn):
                        nonlocal intent_lesson_ids, intent_receipt_frozen
                        selected = lesson_service.select_intent(
                            branch_id=res.branch_id,
                            turn_index=res.turn_index,
                            user_hash=action_hash,
                            narration_mode=intent_mode,
                            recognized_meanings=self._recognized_intent_meanings(semantic_turn),
                        )
                        rows = self._player_lesson_rows(selected)
                        intent_lesson_ids = tuple(
                            str(row["lesson_id"])
                            for row in rows
                            if isinstance(row.get("lesson_id"), str)
                        )
                        intent_receipt_frozen = True
                        return selected
                t0 = tier0.run(
                    doc,
                    res.klass.value,
                    res.duplicate,
                    state,
                    self.cfg,
                    self.rng,
                    turn=res.turn_index,
                    opening_assessment=opening_assessment,
                    recognition_overlay=(
                        self._playerlex_proposal
                        if self.playerlex_service is not None
                        else None
                    ),
                    interpretation_overlay=intent_selector,
                )
                if t0.doc is not None:        # OOC spans stripped (03 R1)
                    doc = t0.doc
                    changed = True
            elif clean_doc != doc:
                doc = clean_doc
                changed = True
            if reserved is not None:
                # 2026-07-10 (Eranmor): the SAME action re-sent after a lost generation —
                # its rolls/costs/cooldowns are already journaled at the previous turn. One
                # player action = one resolution: re-serve the settled checks on THIS
                # [DIRECTIVE] and apply nothing new (no re-roll, no double clock/cost).
                checks = reserved["checks"]
                rolls = reserved.get("rolls") or []
                prior_opp = reserved.get("opposition")
                kind = reserved["kind"]
                state["_fresh_checks"] = checks
                state["_fresh_rolls"] = rolls
                state["_settled_retry"] = {
                    "kind": kind,
                    "source_turn": reserved.get("source_turn"),
                    "families": reserved.get("families") or [],
                }
                if kind == "swipe_replay" and reserved.get("first_enemy_spawn"):
                    # A swipe rewrites the original introduction prose, not the next combat beat.
                    # The rule-owned spawn/intent survives mechanically, but the replacement
                    # narrator request must keep the same fresh-foe no-telegraph boundary.
                    state["_fresh_foe_retry"] = True
                if isinstance(prior_opp, dict):
                    peid = next(iter(state.get("player") or {}), "")
                    live_player = (state.get("player") or {}).get(peid) or {}
                    live_opp = (live_player.get("_opposition_last")
                                if isinstance(live_player, dict) else None)
                    if isinstance(live_opp, dict) and live_opp.get("turn") == prior_opp.get("turn"):
                        prior_opp = {**prior_opp,
                                     "hp_cur": live_opp.get("hp_cur"),
                                     "hp_max": live_opp.get("hp_max")}
                    state["_reserved_opposition"] = prior_opp
                label = "lost reply" if kind == "lost_reply" else "same-turn swipe"
                log.info("re-serve: %d settled check(s), %d raw roll(s)%s for %s",
                         len(checks), len(rolls), " plus enemy action" if prior_opp else "", label)
                notices = [f"{label}: re-serving the settled "
                           f"{str(c.get('skill', 'check'))} roll ({c.get('tier')})"
                           for c in checks]
                if prior_opp:
                    notices.append(f"{label}: re-serving the settled enemy action")
                if rolls:
                    notices.append(f"{label}: re-serving {len(rolls)} settled raw roll(s)")
                self._notice(res, notices)
                self._capture_user_text(doc, res)
            else:
                applied_now: list = []
                if t0.user_ops:               # user source FIRST: freeze gates the rule batch
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.user_ops, "user", self.cfg)
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                if t0.rule_ops:
                    t0.rule_ops = assign_damage_effect_ids(
                        t0.rule_ops, res.branch_id, res.turn_index, "code", basis="tier0")
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.rule_ops, "rule", self.cfg)
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                if t0.proposal_ops:           # R9/R10: model-authored ledger tags apply
                    t0.proposal_ops = assign_damage_effect_ids(
                        t0.proposal_ops, res.branch_id, res.turn_index, "reply_tag",
                        basis="legacy")
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.proposal_ops, "extraction", self.cfg)   # clamped
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                state, evolutions = self._progress(res, state, applied_now)   # RPG-5 (doc 10)
                # [DIRECTIVE] shows EXACTLY the checks resolved THIS request (not
                # turn-matched): reliable delivery + no stale rolls confusing the model.
                state["_fresh_checks"] = [o for o in applied_now
                                          if isinstance(o, dict) and o.get("op") == "check"]
                if not state["_fresh_checks"] and not getattr(t0, "kill_note", "") \
                        and getattr(t0, "turn_guidance", ""):
                    state["_turn_guidance"] = t0.turn_guidance
                if t0.off_protocol:
                    state["_protocol_nudge"] = t0.off_protocol
                if getattr(t0, "kill_note", ""):        # out-of-combat kill outcome (2026-07-10)
                    state["_kill_note"] = t0.kill_note
                for n in t0.notices:
                    log.info("tier0 notice: %s", n)
                self._notice(res, t0.notices)
                self._capture_user_text(doc, res)
                if intent_receipt_frozen and callable(
                    getattr(self.player_lessons_service, "record_intent_applications", None)
                ):
                    applications = tuple(
                        dict(row)
                        for row in t0.intent_applications
                        if isinstance(row, Mapping)
                    )
                    try:
                        self.player_lessons_service.record_intent_applications(
                            res.branch_id,
                            res.turn_index,
                            list(applications),
                        )
                    except Exception as exc:
                        # The code-owned turn is already committed.  Leave the application receipt
                        # pending so an idempotent repair can finish observability without reranking.
                        log.warning(
                            "Player Lesson intent application receipt unavailable (%s)",
                            type(exc).__name__,
                        )
                        intent_application_pending = applications

        player_lesson_rows: list[dict] = []
        player_lesson_receipt_turn: Optional[int] = None
        lesson_service = self.player_lessons_service
        if lesson_service is not None \
                and getattr(getattr(self.cfg, "specialization", None), "name", "none") == "rpg" \
                and narrator_request \
                and not self._semantic_truth_gate_enabled():
            try:
                if res.klass.value in ("new_session", "new_turn") and reserved is None \
                        and t0 is not None and action_hash:
                    narration_mode = compose.current_narration_mode(
                        state,
                        self.cfg,
                        combat_opening=combat_opening,
                    )
                    if narration_mode:
                        selected = lesson_service.select(
                            branch_id=res.branch_id,
                            turn_index=res.turn_index,
                            user_hash=action_hash,
                            narration_mode=narration_mode,
                            recognized_meanings=self._recognized_meanings(t0),
                        )
                        player_lesson_rows = self._player_lesson_rows(selected)
                        player_lesson_receipt_turn = res.turn_index
                else:
                    source_turn = None
                    if reserved is not None:
                        source_turn = reserved.get("source_turn")
                    elif res.klass.value in ("continue", "swipe", "edit_fork"):
                        source_turn = res.turn_index
                    if isinstance(source_turn, int) and not isinstance(source_turn, bool) \
                            and source_turn >= 0:
                        rehydrated = lesson_service.rehydrate(res.branch_id, source_turn)
                        player_lesson_rows = self._player_lesson_rows(rehydrated)
                        player_lesson_receipt_turn = source_turn
            except Exception as exc:
                # The lesson layer owns prompt preferences only. State settlement and narration
                # remain available when its local catalog or receipt is unavailable.
                log.warning("Player Lessons retrieval unavailable (%s)", type(exc).__name__)
                player_lesson_rows = []
                player_lesson_receipt_turn = None

        if self.jobs is not None and isinstance(doc.get("model"), str):
            self.jobs.models[res.session_id] = doc["model"]

        recall = [
            memory.player_safe_memory_text(line, state)
            for line in self.store.read_recall(res.session_id)
        ]                                                    # Q15: one SELECT on the hot path
        note, l9 = "", None                                  # + two tiny indexed reads (03 SS9)
        try:
            note = memory.player_safe_memory_text(
                self.store.read_note(res.session_id), state,
            )
            if self.cfg.user_guard.enabled and self.cfg.user_guard.mode == "prevent_and_correct":
                l9 = self.store.lint_l9_evidence(
                    res.branch_id, res.turn_index - self.cfg.consent.guard_escalate_turns)
        except Exception:                                    # fail-open: base guard + no note
            note, l9 = "", None
        if getattr(self.cfg, "specialization", None) is not None \
                and self.cfg.specialization.name == "rpg" \
                and res.stamp and res.stamp.card_role == "narrator":
            doc, contract_changed = compose.ensure_narrator_envelope(doc)
            changed = changed or contract_changed
        out_doc, kept = compose.compose(doc, state, self.cfg, res.stamp or stamp,
                                        res.klass.value, recall=recall, note=note,
                                        guard_evidence=l9,
                                        combat_opening=combat_opening,
                                        player_lessons=player_lesson_rows)
        if out_doc is not None:
            doc = out_doc
            changed = True
        delivered_ids = tuple(next(
            (
                row.get("lesson_ids", [])
                for row in kept
                if isinstance(row, dict) and row.get("cls") == "player_lessons"
            ),
            [],
        ))
        changed = _apply_narrator_reasoning_default(doc, self.cfg, res.stamp or stamp) or changed
        local_response = None
        try:
            response_stamp = res.stamp or stamp
            story = _deterministic_first_intent_story(
                self.store, res, state, self.cfg, response_stamp
            )
            if story:
                local_response = LocalResponse(
                    story=story,
                    model=str(doc.get("model") or ""),
                    stream=bool(doc.get("stream")),
                    provenance=compose.DETERMINISTIC_FIRST_INTENT_VERSION,
                )
        except Exception as exc:
            # This optimization owns no state.  Any uncertainty returns to the established
            # upstream narrator path with the already-enriched request intact.
            log.warning("deterministic first-intent response failed open: %s", type(exc).__name__)
        try:
            self.store.write_slice(res.session_id, res.turn_index, kept)
        except Exception:                     # slice row is observability, never load-bearing
            pass
        if changed and local_response is None \
                and getattr(self.cfg.upstream, "cache_key", True):
            # Phase 0a: every turn of a conversation routes to the same warm provider
            # cache. ONLY on requests the engine already changed — an untouched request
            # stays byte-identical (transparency), so `none`-untouched wires carry nothing.
            promptcache.add_cache_key(doc, res.session_id)
            if getattr(self.cfg.upstream, "include_usage", False):
                promptcache.add_usage_probe(doc)
            if delivered_ids:
                # Prewarm bypasses the real narrator delivery callback. Never retain or resend a
                # private lesson component on that bonus path; a real new turn owns all use.
                self._last_docs.pop(res.session_id, None)
                self._prewarm_at.pop(res.session_id, None)
            else:
                self._last_docs[res.session_id] = doc      # prewarm source (bounded LRU)
                self._last_docs.move_to_end(res.session_id)
                while len(self._last_docs) > promptcache.LAST_DOCS_MAX:
                    self._last_docs.popitem(last=False)
        if getattr(self.cfg.server, "turn_trace", False):
            log.info("TURN_TRACE %s", json.dumps({
                "event": "packet", "ts": time.time(), "session": res.session_id,
                "branch": res.branch_id, "turn": res.turn_index,
                "response_provenance": (
                    {
                        "source": "local",
                        "kind": local_response.provenance,
                        "chars": len(local_response.story),
                        "hash": content_hash(local_response.story),
                    }
                    if local_response is not None else {"source": "upstream"}
                ),
                **_packet_manifest(doc),
                **_diagnostic_narrator_packet(
                    doc,
                    redacted_texts=(local_response.story,) if local_response is not None else (),
                ),
            }, separators=(",", ":"), ensure_ascii=False))
        narration_guard = None
        response_stamp = res.stamp or stamp
        spec = getattr(self.cfg, "specialization", None)
        if local_response is None and spec is not None and spec.name == "rpg" \
                and getattr(spec, "narration_pre_display_guard", True) \
                and response_stamp is not None and response_stamp.card_role == "narrator":
            try:
                retry = state.get("_settled_retry")
                mechanic_turn = (
                    retry.get("source_turn")
                    if isinstance(retry, dict)
                    and isinstance(retry.get("source_turn"), int)
                    and not isinstance(retry.get("source_turn"), bool)
                    else res.turn_index
                )
                evidence = self.store.diagnostic_turn(res.branch_id, mechanic_turn)
                narration_guard = build_narration_guard_basis(
                    state,
                    branch_id=res.branch_id,
                    turn_index=res.turn_index,
                    journal_rows=evidence["journal"],
                )
            except Exception as exc:
                log.warning("narration pre-display guard preparation unavailable: %s",
                            type(exc).__name__)
        ctx = PostContext(res.session_id, res.branch_id, res.turn_index, res.klass.value,
                          speaker=(res.stamp.speaker if res.stamp else None),
                          card_role=(res.stamp.card_role if res.stamp else None),
                          card=card, opening=opening,
                          evolutions=evolutions or None,
                          enriched=changed and local_response is None,
                           response_key=request_key, local_response=local_response,
                           request_model=str(doc.get("model") or ""),
                           narration_guard=narration_guard,
                           player_lesson_receipt_turn=player_lesson_receipt_turn,
                           player_lesson_ids=delivered_ids,
                           player_lesson_intent_ids=intent_lesson_ids,
                           player_lesson_intent_applications_pending=(
                               intent_application_pending
                           ))
        # One transient persistence failure is repaired before even a local deterministic response
        # can leave this guarded turn. Later exact duplicates/upstream acknowledgement retry the
        # same immutable content-free payload without rerunning interpretation.
        self._repair_player_lesson_intent_applications(ctx)
        packet = compose.to_bytes(doc) if changed else body
        if not res.duplicate:
            self._request_packets[request_key] = (packet, ctx)
            self._request_packets.move_to_end(request_key)
            while len(self._request_packets) > 512:
                self._request_packets.popitem(last=False)
        return packet, ctx

    def prewarm_doc(self, session_id: str) -> Optional[dict]:
        """Phase 0a stretch: the session's last enriched doc IF the per-session
        prewarm cooldown allows — claims the cooldown slot when it returns one.
        None = nothing remembered (fresh proxy / never enriched) or on cooldown."""
        doc = self._last_docs.get(session_id)
        if doc is None:
            return None
        now = time.monotonic()
        prior = self._prewarm_at.get(session_id)
        if prior is not None and now - prior < promptcache.PREWARM_COOLDOWN_S:
            return None
        self._prewarm_at[session_id] = now
        return doc

    def _progress(self, res, state: dict, applied: list) -> tuple[dict, list]:
        """RPG-5 hot-path progression pass (µs, pure arithmetic + one apply): XP awards,
        level-ups, and defeat resolution derived from THIS turn's applied ops; also
        collects mastery-bracket crossings for the cold-path Q27 evolution hook.
        Fail-open — any error leaves state exactly as it was (invariant 1)."""
        evolutions: list = []
        try:
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg" or not state.get("player"):
                return state, evolutions
            for op in applied:
                if op.get("op") == "master_tick" and op.get("_bracket_up"):
                    evolutions.append((op.get("char"), "skills", op.get("skill"),
                                       op.get("_bracket_up")))
            if getattr(spec, "war_room", True):   # Phase 1: the combat referee runs FIRST —
                wr = combat_ops(
                    state, applied,
                    prepare_intent=bool(getattr(spec, "enemy_rolls", True)))
                if wr:
                    r0 = apply_delta(self.store, res.session_id, res.branch_id,
                                     res.turn_index, wr, "rule", self.cfg)
                    state = r0.state
                    applied = list(applied) + r0.applied
                    self._index_memories(res, r0)
                if getattr(spec, "large_battle", True):   # §F: waves / battle settle, after the
                    bw = battle_ops(state, applied)       # defeats above have landed
                    if bw:
                        r0 = apply_delta(self.store, res.session_id, res.branch_id,
                                         res.turn_index, bw, "rule", self.cfg)
                        state = r0.state
                        applied = list(applied) + r0.applied
                        self._index_memories(res, r0)
            pro = progression_ops(state, applied,
                                  hardcore=getattr(spec, "hardcore", False))
            if pro:
                r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                pro, "rule", self.cfg)
                state = r.state
                applied = list(applied) + r.applied
                self._index_memories(res, r)
            if getattr(spec, "living_world", True):   # Phase 2: the world moves — travel
                lw = world_ops(state, applied,        # time, the idle clock, faction fronts
                               clock_turns=getattr(spec, "clock_turns", 6),
                               session_id=res.session_id, branch_id=res.branch_id,
                               turn_index=res.turn_index)
                if lw:
                    rl = apply_delta(self.store, res.session_id, res.branch_id,
                                     res.turn_index, lw, "rule", self.cfg)
                    state = rl.state
                    self._index_memories(res, rl)
            return state, evolutions
        except Exception as exc:
            log.warning("progression pass failed open: %s", type(exc).__name__)
        return state, evolutions

    def _index_memories(self, res, r) -> None:
        """Mirror user/rule memory_event ops into the retrieval index (fail-open)."""
        try:
            memory.index_applied(self.store, res.session_id, res.branch_id,
                                 r.applied, r.state)
        except Exception as exc:
            log.warning("memory index skipped: %s", type(exc).__name__)

    def _reserve_lost_turn(self, res, doc: dict, state: dict) -> Optional[dict]:
        """Reserve mechanics for an RPG narration retry before Tier-0 can consume RNG.

        A same-turn swipe always preserves the settled action, even when real prose exists. A
        byte-identical next turn is reserved only when the previous reply was actually lost and
        the configured lost-reply safeguard is on. None mode remains wire-inert. Fail open.
        """
        try:
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg":
                return None
            if res.klass.value not in ("new_turn", "swipe"):
                return None
            kind = "swipe_replay" if res.klass.value == "swipe" else "lost_reply"
            if kind == "lost_reply" \
                    and not getattr(self.cfg.session, "reserve_lost_turns", True):
                return None
            if res.klass.value == "swipe":
                pt = res.turn_index
            else:
                prev = self.store.db.execute(
                    "SELECT MAX(turn_index) AS t FROM turns WHERE branch_id=? AND turn_index<?",
                    (res.branch_id, res.turn_index)).fetchone()
                pt = prev["t"] if prev else None
            if pt is None or pt < 0:
                return None
            incoming_hash = _last_user_action_hash(doc)
            rows = self.store.get_turn_texts(res.branch_id, pt, pt)
            text_row = rows[0] if rows else None
            prev_user = text_row["user_text"] if text_row else ""
            previous = self.store.db.execute(
                "SELECT user_hash, assistant_hash FROM turns "
                "WHERE branch_id=? AND turn_index=?",
                (res.branch_id, pt)).fetchone()
            prior_hash = str(previous["user_hash"] or "") if previous is not None else ""
            if kind == "lost_reply" and previous is None:
                return None
            if kind == "lost_reply" and previous["assistant_hash"] not in (None, ""):
                return None
            if kind == "lost_reply" and prior_hash and incoming_hash \
                    and prior_hash != incoming_hash:
                return None
            if kind == "lost_reply" and text_row is not None \
                    and text_row["assistant_text"] not in (None, ""):
                return None                    # the reply exists — a genuinely new turn
            name = (self.cfg.user_guard.name
                    or (res.stamp.user if res.stamp and res.stamp.user else "") or "User")
            msgs = doc.get("messages", [])
            text = next((tier0._msg_text(m.get("content")) for m in reversed(msgs)
                         if isinstance(m, dict) and m.get("role") == "user"), "")
            text = " ".join(text.split())
            if kind == "lost_reply" and not (prior_hash and incoming_hash) \
                    and (not text or f"{name}: {text}" != prev_user):
                return None
            settled = self.store.rule_ops_at(res.branch_id, pt)
            source_turn = pt
            if not settled and kind == "lost_reply" and incoming_hash:
                candidates = self.store.db.execute(
                    "SELECT turn_index FROM turns WHERE branch_id=? AND turn_index<? "
                    "AND user_hash=? ORDER BY turn_index DESC LIMIT 8",
                    (res.branch_id, pt, incoming_hash)).fetchall()
                for candidate in candidates:
                    prior_ops = self.store.rule_ops_at(res.branch_id, candidate["turn_index"])
                    if prior_ops:
                        source_turn = candidate["turn_index"]
                        settled = prior_ops
                        break
            checks = [dict(o, settled_retry=True, retry_kind=kind,
                           lost_reply=(kind == "lost_reply"))
                      for o in settled
                      if o.get("op") == "check" and o.get("tier")]
            rolls = [dict(o, settled_retry=True, retry_kind=kind,
                          lost_reply=(kind == "lost_reply"))
                     for o in settled if o.get("op") == "roll" and o.get("spec")]
            opposition_op = next((o for o in reversed(settled)
                                  if isinstance(o.get("_opposition"), dict)), None)
            opposition = None
            if isinstance(opposition_op, dict):
                opposition = {**opposition_op["_opposition"],
                              "effect_id": opposition_op.get("_effect_id"),
                              "turn": source_turn, "delta": opposition_op.get("delta", 0),
                              "settled_retry": True, "retry_kind": kind,
                              "lost_reply": kind == "lost_reply"}
            # A swipe is always a narration retry.  Even a mechanically empty original turn
            # must not become a fresh action merely because it has no receipts.
            return {"kind": kind, "checks": checks, "rolls": rolls,
                    "opposition": opposition, "source_turn": source_turn,
                    "families": sorted({str(op.get("op")) for op in settled if op.get("op")}),
                    "first_enemy_spawn": (
                        kind == "swipe_replay"
                        and _first_enemy_spawn_receipt(settled, state, source_turn)
                    )}
        except Exception as exc:
            log.warning("re-serve detection failed open: %s: %s", type(exc).__name__, exc)
            return None

    def _notice(self, res, msgs: list) -> None:
        """Pillar 17: mirror tier0 notices into the per-session HUD ring (fail-open)."""
        try:
            if not msgs:
                return
            ring = self._notices.setdefault(res.session_id, [])
            ring.extend({"turn": res.turn_index, "ts": time.time(),
                         "text": str(m)[:200]} for m in msgs[:6])
            del ring[:-12]
            self._notices.move_to_end(res.session_id)
            while len(self._notices) > 64:
                self._notices.popitem(last=False)
        except Exception:
            pass

    def recent_notices(self, session_id: str) -> list[dict]:
        """The HUD's notice feed (newest last). Transient — a restart clears it."""
        try:
            return list(self._notices.get(session_id, ()))
        except Exception:
            return []

    def on_upstream_error(self, ctx: Optional[PostContext], status: int, raw: bytes) -> None:
        """Record one bounded, transient upstream rejection for the player-facing HUD."""
        if ctx is None:
            return
        try:
            message = f"Upstream rejected this turn (HTTP {int(status)})"
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                candidate = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(candidate, dict):
                    candidate = candidate.get("message")
                if not isinstance(candidate, str) and isinstance(payload, dict):
                    candidate = payload.get("message")
                if isinstance(candidate, str) and candidate.strip():
                    message = " ".join(candidate.split())[:300]
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                pass
            self._transport_errors[ctx.session_id] = {
                "status": int(status),
                "turn": ctx.turn_index,
                "ts": time.time(),
                "message": message,
            }
            self._transport_errors.move_to_end(ctx.session_id)
            while len(self._transport_errors) > 64:
                self._transport_errors.popitem(last=False)
        except Exception:
            pass

    def transport_error(self, session_id: str) -> Optional[dict]:
        """Latest unresolved upstream rejection for a session, if any."""
        try:
            error = self._transport_errors.get(session_id)
            return dict(error) if error else None
        except Exception:
            return None

    def _capture_user_text(self, doc: dict, res) -> None:
        """Retain the NEW user message (post-OOC-strip) for extraction context (01 SS7)."""
        name = (self.cfg.user_guard.name
                or (res.stamp.user if res.stamp and res.stamp.user else "") or "User")
        if self.jobs is not None:            # CHARACTERS [USER] mark (04 SS1.2)
            self.jobs.user_names[res.session_id] = name
        if res.klass.value not in ("new_turn", "new_session", "impersonate"):
            return
        msgs = doc.get("messages", [])
        text = next((tier0._msg_text(m.get("content")) for m in reversed(msgs)
                     if isinstance(m, dict) and m.get("role") == "user"), "")
        text = " ".join(text.split())        # OOC strip can leave double spaces
        if not text:
            return
        self.store.write_turn_text(res.branch_id, res.turn_index,
                                   user_text=f"{name}: {text}")

    def _swipe_rollback_guard(self, res) -> None:
        """Retract only narrator extraction before a same-turn narration retry.

        Rule-owned mechanics survive in both extraction modes: a narration retry cannot reroll
        or roll back the Player action, opposition receipt, costs, HP, or next enemy intent.
        Replacement prose is retained for continuity but is not another extraction opportunity.
        """
        if res.klass.value != "swipe":
            return
        # Source-scoped in every extraction mode: narrator-authored facts may be regenerated,
        # while rule-owned rolls, costs, HP receipts, enemy actions, and future intent survive.
        self.store.retract_extraction_at(res.branch_id, res.turn_index)

    # ------------------------------------------------------------------ cold path
    def _remember_completed_response(self, response_key: str | None) -> None:
        """Suppress an exact transport retry only after its durable reply commit succeeds."""
        if not response_key:
            return
        self._completed_responses[response_key] = time.monotonic()
        self._completed_responses.move_to_end(response_key)
        while len(self._completed_responses) > 512:
            self._completed_responses.popitem(last=False)

    def _on_semantic_response(self, ctx: PostContext, raw: bytes, content_type: str) -> None:
        """Persist only the exact proof-carrying artifact; never re-extract mechanics."""
        replay = ctx.semantic_replay
        if replay is None:
            raise ValueError("semantic response has no terminal artifact")
        completed = False
        # Keep active-attempt verification and every cold-path write under the same Store lock and
        # transaction.  A concurrently terminalized swipe therefore wins cleanly: an older
        # stream's delayed finally-block becomes a no-op instead of restoring retired text/hash.
        with self.store.transaction():
            verified = self.store.turn_lifecycle.verify_claimed_delivery(
                replay.lifecycle_key,
                replay.attempt_index,
                expected_logical_message_id=replay.logical_message_id,
                expected_artifact_digest=replay.selected_artifact_digest,
                expected_session_id=ctx.session_id,
                expected_branch_id=ctx.branch_id,
                expected_turn_index=ctx.turn_index,
            )
            if raw != verified.payload \
                    or raw_fingerprint(raw) != verified.payload_hash \
                    or content_type.strip().lower() != verified.content_type.strip().lower():
                raise ValueError("semantic response differs from its claimed terminal artifact")
            text = decode_chat_story(raw, verified.content_type)
            if ctx.response_key and ctx.response_key in self._completed_responses:
                return
            speaker = ctx.speaker or "Narrator"
            self.store.write_turn_text(
                ctx.branch_id,
                ctx.turn_index,
                assistant_text=f"{speaker}: {text.strip()}",
            )
            self.store.write_turn_hashes(
                ctx.branch_id,
                ctx.turn_index,
                assistant_hash=content_hash(text.strip()),
            )
            self._ingest_delivered_claims(ctx, text)
            self.store.mark_extraction(
                ctx.branch_id,
                ctx.turn_index,
                ctx.turn_index,
                "skipped",
            )
            completed = True
        if completed:
            self._remember_completed_response(ctx.response_key)
        if completed:
            self._transport_errors.pop(ctx.session_id, None)

    def on_response(self, ctx: Optional[PostContext], raw: bytes, content_type: str) -> None:
        """Called by the proxy tee AFTER the stream ends. Never raises."""
        if ctx is None:
            return
        try:
            if ctx.suppress_cold_path:
                return
            if ctx.semantic_gate:
                self._on_semantic_response(ctx, raw, content_type)
                return
            if ctx.narration_guard is not None:
                # Compliance checks are cold-path diagnostics. They must never delay, replace,
                # truncate, or otherwise corrupt the response that was already streamed.
                self.guard_response(ctx, raw, content_type)
            text = _response_text(raw, content_type)
            if ctx.response_key and text and text.strip():
                if ctx.response_key in self._completed_responses:
                    log.info("network duplicate response ignored after first completion")
                    return
            if ctx.enriched:                 # Phase 0a: cache observability (pillar 17)
                self.cache.observe(ctx.session_id,
                                   promptcache.parse_usage(raw, content_type))
            if text and text.strip():
                speaker = ctx.speaker or "Narrator"
                # The visible prose, its exact hash, and every recognition-only Claim Record are
                # one publication.  A crash or failed claim write rolls the entire reply back, so
                # the same transport delivery remains safe to retry instead of being suppressed
                # by an in-memory completion marker with missing durable claims.
                with self.store.transaction():
                    self.store.write_turn_text(
                        ctx.branch_id,
                        ctx.turn_index,
                        assistant_text=f"{speaker}: {text.strip()}",
                    )
                    self.store.write_turn_hashes(
                        ctx.branch_id,
                        ctx.turn_index,
                        assistant_hash=content_hash(text.strip()),
                    )
                    if ctx.klass == "swipe":
                        self.store.mark_extraction(
                            ctx.branch_id,
                            ctx.turn_index,
                            ctx.turn_index,
                            "skipped",
                        )
                    else:
                        self._ingest_delivered_claims(ctx, text)
                self._remember_completed_response(ctx.response_key)
            self._transport_errors.pop(ctx.session_id, None)
            if ctx.klass == "swipe":
                # A swipe/regenerate is narration-only.  Its final prose replaces the abandoned
                # text for continuity, but neither inline tags nor Tier-1 may reinterpret the
                # alternate wording as a second state/mechanics settlement.
                log.info("same-turn narration retry stored without cold-path extraction")
                return
            self._ingest_reply_tags(ctx, text)   # live_recalc: newest reply's world-tags NOW
            self._discover(ctx)               # 08 B2 Tier-0 evidence pass (fail-open)
            self._recall_pass(ctx)            # P4/Q15: keep recall fresh in rules-only mode
            self._lint_pass(ctx, text)        # 03 SS9 (full in off/rules; L9-only otherwise)
            self._genesis_pass(ctx)           # Q23 stage B: assist-LLM seed (cold path)
            self._evolve_pass(ctx)            # RPG-5: Q27 mastery re-authoring (cold path)
            if self.jobs is not None:
                # settle the head NOW so Tier-1 extracts the newest reply on its OWN cold path
                # (Bean 07-07). Skip turn-0: let genesis stage-B seed the world first, exactly as
                # before, so opening-turn extraction still lands on turn 1's cold path.
                if _live_recalc(self.cfg) and ctx.klass != "new_session":
                    try:
                        self.store.settle_head(ctx.branch_id)
                    except Exception:
                        pass
                self.jobs.notify(ctx.session_id, ctx.branch_id, ctx.turn_index)
        except Exception as exc:
            log.warning("response tee failed open: %s", type(exc).__name__)

    def guard_response(
        self,
        ctx: PostContext,
        raw: bytes,
        content_type: str,
    ) -> tuple[bytes, str]:
        """Evaluate narration as a cold-path advisory and preserve upstream wire bytes exactly."""
        basis = ctx.narration_guard
        if basis is None:
            return raw, content_type
        reasons: tuple[str, ...] = ()
        try:
            exact_state = self.store.state_at(
                ctx.branch_id,
                ctx.turn_index,
                reduce_state,
                empty=empty_state(),
            )
            story = decode_chat_story(raw, content_type)
            decision = guard_narration_story(
                basis,
                exact_state,
                story,
                self.cfg,
                klass=ctx.klass,
                user_name=str(getattr(self.cfg.user_guard, "name", "") or ""),
                user_aliases=tuple(getattr(self.cfg.user_guard, "aliases", ()) or ()),
            )
            if decision.accepted:
                reasons = ()
            else:
                reasons = decision.reasons
                log.warning("narration guard advisory: %s", ",".join(reasons))
        except Exception as exc:
            reasons = ("candidate_wire_or_guard_unavailable",)
            log.warning("narration guard failed open: %s", type(exc).__name__)

        ctx.narration_guard_replaced = False
        ctx.narration_guard_reasons = reasons
        return raw, content_type

    def record_response_trace(
        self,
        ctx: Optional[PostContext],
        *,
        source: str,
        status: int,
        headers_ms: float | None,
        first_chunk_ms: float | None,
        total_ms: float,
        byte_count: int,
        content_sha256: str,
        content_type: str = "",
        error_type: str = "",
    ) -> None:
        """Persist a content-free response receipt plus authoritative post-response evidence.

        This deliberately accepts no request headers, authorization, endpoint, raw request, or
        raw response argument.  Model output is represented only by byte count and SHA-256.
        """
        if ctx is None or not getattr(self.cfg.server, "turn_trace", False):
            return
        try:
            guard_replaced = bool(getattr(ctx, "narration_guard_replaced", False))
            guard_reason_codes: list[str] = []
            raw_reasons = getattr(ctx, "narration_guard_reasons", ())
            if raw_reasons:
                if isinstance(raw_reasons, str):
                    raw_reasons = (raw_reasons,)
                elif not isinstance(raw_reasons, (tuple, list, set, frozenset)):
                    raw_reasons = ()
                for raw_reason in raw_reasons:
                    reason = str(raw_reason)
                    if reason in _NARRATION_GUARD_TRACE_REASON_CODES \
                            and reason not in guard_reason_codes:
                        guard_reason_codes.append(reason)
                if not guard_reason_codes:
                    # Never serialize an unexpected value from the guarded candidate path.  The
                    # receipt remains useful and content-free even if local reason plumbing drifts.
                    guard_reason_codes.append("guard_reason_unavailable")
            state = current_state(self.store, ctx.branch_id)
            canonical_state = json.dumps(
                state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            evidence = self.store.diagnostic_turn(ctx.branch_id, ctx.turn_index)

            def _ms(value: float | None) -> float | None:
                return None if value is None else round(max(0.0, float(value)), 3)

            log.info("TURN_TRACE %s", json.dumps({
                "event": "response",
                "ts": time.time(),
                "session": ctx.session_id,
                "branch": ctx.branch_id,
                "turn": ctx.turn_index,
                "class": ctx.klass,
                "model": ctx.request_model,
                "response": {
                    "source": str(source)[:24],
                    "status": int(status),
                    "content_type": str(content_type).split(";", 1)[0][:80],
                    "bytes": max(0, int(byte_count)),
                    "sha256": str(content_sha256)[:64],
                    "error_type": str(error_type)[:80],
                    "narration_guard_replaced": guard_replaced,
                    "narration_guard_reason_codes": guard_reason_codes,
                },
                "latency_ms": {
                    "headers": _ms(headers_ms),
                    "first_chunk": _ms(first_chunk_ms),
                    "total": _ms(total_ms),
                },
                "lineage": evidence["lineage"],
                "journal": evidence["journal"],
                "state_hash": hashlib.sha256(canonical_state.encode("utf-8")).hexdigest(),
                "state": state,
            }, separators=(",", ":"), ensure_ascii=False))
        except Exception as exc:
            log.warning("response trace failed open: %s", type(exc).__name__)

    def _ingest_delivered_claims(self, ctx: PostContext, text: Optional[str]) -> None:
        """Recognize claim structure only after one ordinary reply is durably delivered."""
        if ctx.klass not in {"new_turn", "new_session"} or not text or not text.strip():
            return
        spec = getattr(self.cfg, "specialization", None)
        if spec is None or spec.name != "rpg":
            return
        ops = claim_ops_from_text(
            text,
            ingress="narrator",
            source_id=ctx.speaker or "narrator",
        )
        if not ops:
            return
        result = apply_delta(
            self.store,
            ctx.session_id,
            ctx.branch_id,
            ctx.turn_index,
            ops,
            "rule",
            self.cfg,
        )
        if result.quarantined:
            log.info(
                "delivered ClaimLex recognition quarantined %d row(s)",
                len(result.quarantined),
            )

    def _ingest_reply_tags(self, ctx: PostContext, text: Optional[str]) -> None:
        """live_recalc (Bean 2026-07-07): parse the DM's FRESH reply's world/effect tags
        (R9/R10) the instant its stream ends and commit them to the ledger at THIS turn
        (source='extraction'), so state reflects the NEWEST output — not the reply before it.
        rpg-gated; narration retries never call this method. Fail-open — any error leaves the
        ledger exactly as it was (invariant 3)."""
        try:
            if not (text and text.strip()) or not _live_recalc(self.cfg):
                return
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg":
                return
            state = current_state(self.store, ctx.branch_id)
            ops = tier0.parse_reply_tags(text, state)
            ops = assign_damage_effect_ids(ops, ctx.branch_id, ctx.turn_index, "reply_tag",
                                           basis=content_hash(text))
            combat_tags = tier0.parse_combat_tags(
                text, state,
                allow_large_battle=bool(getattr(spec, "large_battle", True)),
            ) if getattr(spec, "war_room", True) else []
            ops, redundant_scene = _drop_redundant_scene_restatement(
                ops, state, combat_tags
            )
            if redundant_scene:
                log.info(
                    "live narrator scene tag rejected: location and cast were unchanged"
                )
                self._notice(ctx, [
                    "The narrator repeated the current scene without a move or cast change, "
                    "so AetherState ignored that scene record."
                ])
            realization = build_narrator_realization_from_state(state)
            realization_owns_turn = narrator_realization_owns_turn(
                realization, ctx.turn_index,
            )
            if realization_owns_turn and (ops or combat_tags):
                # The code-owned settlement has already committed every authorized change.  A
                # reply tag on that same turn can only duplicate it or invent an unlisted change;
                # neither is narrator authority.  Keep legacy narrator-tag behavior on turns
                # without a complete current realization.
                log.info(
                    "live narrator tag ingest rejected %d op(s) by current realization",
                    len(ops) + len(combat_tags),
                )
                self._notice(ctx, [
                    "The narrator proposed a world change outside this turn's settled result, "
                    "so AetherState ignored it."
                ])
                ops = []
                combat_tags = []
            pickup_rejected = 0
            if ops:
                turn_rows = self.store.get_turn_texts(
                    ctx.branch_id, ctx.turn_index, ctx.turn_index,
                )
                player_text = str(turn_rows[-1]["user_text"] or "") if turn_rows else ""
                ops, pickup_rejected = _bind_reply_pickups(ops, state, player_text)
            cohort_candidate = any(
                op.get("op") == "battle_start" and isinstance(op.get("cohort"), dict)
                for op in combat_tags
            )
            semantic_apply: dict = {}
            if cohort_candidate:
                try:
                    source_start, source_end = \
                        semantic_ingress.locate_reserved_cohort_declaration(text)
                    attempt_basis = ctx.response_key or (
                        f"{ctx.branch_id}:{ctx.turn_index}:{content_hash(text)}"
                    )
                    scope = semantic_ingress.IngressScope(
                        session_id=ctx.session_id,
                        branch_id=ctx.branch_id,
                        turn_index=ctx.turn_index,
                        attempt_id=("reply." + hashlib.sha256(
                            attempt_basis.encode("utf-8")
                        ).hexdigest()),
                        source_start=source_start,
                        source_end=source_end,
                    )
                    context = semantic_ingress.SemanticIngressContext(
                        issuer_kind="narrator",
                        issuer_id="narrator.main",
                        channel="narrator_candidate",
                        authoring_phase="candidate_proposal",
                        scope=scope,
                    )
                    authority = semantic_ingress.issue_semantic_ingress_authority(
                        text,
                        context=context,
                        grammar_id=semantic_ingress.COHORT_GRAMMAR_ID,
                        grammar_version=semantic_ingress.COHORT_GRAMMAR_VERSION,
                        declaration_id=semantic_ingress.COHORT_DECLARATION_ID,
                        operation_families=(
                            semantic_ingress.OP_BATTLE_COHORT_DECLARATION,
                        ),
                    )
                    declaration = tier0.parse_authorized_cohort_tags(
                        text, state, authority=authority, expected_context=context,
                    )
                    declared_ops = list(declaration.operations)
                    if declared_ops != combat_tags:
                        raise semantic_ingress.SemanticIngressError(
                            "combat tags exist outside the authorized cohort declaration"
                        )
                    combat_tags = declared_ops
                    semantic_apply = {
                        "semantic_declaration": declaration,
                        "semantic_authority": authority,
                        "semantic_context": context,
                        "semantic_source": text,
                    }
                except semantic_ingress.SemanticIngressError as exc:
                    # Combat fails closed; unrelated reply/world tags still enter their own path.
                    combat_tags = []
                    log.warning("live cohort authorization rejected: %s", str(exc)[:200])
                    self._notice(ctx, [
                        "War Room could not accept the narrator's battle group, so it did not "
                        "activate from those tags."
                    ])
            applied: list = []
            # Parse every privileged combat channel against the same pre-reply snapshot and
            # apply it as one ordered batch.  battle_start therefore precedes its counted cohort
            # spawns, while extraction still sees every freshly introduced combatant.
            if combat_tags:
                r = apply_delta(self.store, ctx.session_id, ctx.branch_id,
                                ctx.turn_index, combat_tags, "rule", self.cfg,
                                **semantic_apply)
                state = r.state
                applied += r.applied
                self._index_memories(ctx, r)
                for q in r.quarantined:
                    log.warning("live combat tag quarantined: %s", q.get("reason", ""))
                if cohort_candidate and r.quarantined:
                    self._notice(ctx, [
                        "War Room could not accept the narrator's battle group, so it did not "
                        "activate from those tags."
                    ])
            if ops:
                r = apply_delta(self.store, ctx.session_id, ctx.branch_id, ctx.turn_index,
                                ops, "extraction", self.cfg)
                state = r.state
                applied += r.applied
                self._index_memories(ctx, r)
                for q in r.quarantined:
                    log.info("live tag quarantined: %s", q.get("reason", ""))
                    if "world item is not in the current scene" in str(q.get("reason", "")):
                        pickup_rejected += 1
            if pickup_rejected:
                self._notice(ctx, [
                    "The narrated pickup did not resolve to one exact item in this scene, "
                    "so AetherState left the world and inventory unchanged."
                ])
            if getattr(spec, "war_room", True):
                wr = combat_ops(
                    state, applied,
                    prepare_intent=bool(getattr(spec, "enemy_rolls", True)))
                if wr:                            # loot, combat_end — on the fresh reply's
                    r = apply_delta(self.store, ctx.session_id, ctx.branch_id,   # own turn
                                    ctx.turn_index, wr, "rule", self.cfg)
                    state = r.state
                    applied += r.applied
                    self._index_memories(ctx, r)
                if getattr(spec, "large_battle", True):   # §F: waves-when-losing / battle settle,
                    bw = battle_ops(state, applied)       # AFTER combat_ops so defeats have landed
                    if bw:
                        r = apply_delta(self.store, ctx.session_id, ctx.branch_id,
                                        ctx.turn_index, bw, "rule", self.cfg)
                        state = r.state
                        applied += r.applied
                        self._index_memories(ctx, r)
            if applied and getattr(spec, "living_world", True):
                # Phase 2 (2026-07-09 Cinderveil live): the living-world referee must read
                # the FRESH reply's ops too — this path applied the player's move_entity,
                # but only jobs/_progress ran world_ops, so travel time and the camera
                # never followed a reply-committed move (the ladder later dedups it away).
                lw = world_ops(state, applied,
                               clock_turns=getattr(spec, "clock_turns", 6),
                               session_id=ctx.session_id, branch_id=ctx.branch_id,
                               turn_index=ctx.turn_index)
                if lw:
                    r = apply_delta(self.store, ctx.session_id, ctx.branch_id,
                                    ctx.turn_index, lw, "rule", self.cfg)
                    self._index_memories(ctx, r)
                    log.info("living world (live path): %d op(s) applied", len(r.applied))
        except Exception as exc:
            log.warning("live tag ingest failed open: %s: %s",
                        type(exc).__name__, str(exc)[:200])

    def _recall_pass(self, ctx: PostContext) -> None:
        """Cold-path recall staging when NO extraction job will run for this session
        (extraction jobs do their own precompute with the settled exchange). Fail-open."""
        try:
            if self.cfg.extraction.mode not in ("off", "rules"):
                return                        # jobs path owns it
            state = current_state(self.store, ctx.branch_id)
            rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index,
                                             ctx.turn_index)
            q = " ".join(t for r in rows for t in (r["user_text"], r["assistant_text"]) if t)
            memory.reflect(self.store, self.cfg, ctx.session_id, ctx.branch_id, state)
            memory.precompute_recall(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                                     state, q, ctx.turn_index)
        except Exception as exc:
            log.warning("recall pass skipped: %s", type(exc).__name__)

    def _genesis_pass(self, ctx: PostContext) -> None:
        """Q23 stage B: schedule the full-matrix LLM seed after turn 1's stream ends.
        off/rules extraction -> stage A is the whole product (mark done). Fail-open."""
        try:
            if ctx.klass != "new_session" or not ctx.card:
                return
            if self.cfg.extraction.mode in ("off", "rules") or self.jobs is None:
                if self.store.genesis_state(ctx.session_id) == "rules":
                    self.store.genesis_mark(ctx.session_id, "done")
                return
            ep, _, _ = self.jobs.endpoint_for(ctx.session_id)
            t = asyncio.get_running_loop().create_task(
                genesis.seed_llm(self.store, self.cfg, self.jobs.ladder.get_client, ep,
                                 ctx.session_id, ctx.branch_id, ctx.card, ctx.opening,
                                 speaker=ctx.speaker or "", card_role=ctx.card_role or ""))
            self.jobs._tasks.add(t)
            t.add_done_callback(self.jobs._tasks.discard)
        except Exception as exc:
            log.warning("genesis schedule failed open: %s", type(exc).__name__)

    def _evolve_pass(self, ctx: PostContext) -> None:
        """RPG-5 (doc 10 §4 / Q27): a mastery bracket crossed this turn schedules a cold-path
        assist re-authoring of that skill's frozen def. Fail-open at every step — without an
        assist model the curated bracket bonus (registry.effective_mod) IS the evolution."""
        try:
            if not ctx.evolutions or self.jobs is None:
                return
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg":
                return
            from . import creator as _creator
            ep, _, _ = self.jobs.endpoint_for(ctx.session_id)
            for (char, table, sid, bracket) in ctx.evolutions[:2]:   # bounded per turn
                t = asyncio.get_running_loop().create_task(
                    _creator.evolve_def_snapshot(
                        self.store, self.cfg, self.jobs.ladder.get_client, ep,
                        ctx.session_id, ctx.branch_id, str(char), str(table), str(sid),
                        str(bracket), turn=ctx.turn_index))
                self.jobs._tasks.add(t)
                t.add_done_callback(self.jobs._tasks.discard)
        except Exception as exc:
            log.warning("evolve schedule failed open: %s", type(exc).__name__)

    def _lint_pass(self, ctx: PostContext, text: Optional[str]) -> None:
        """Cold-path lint. off/rules extraction: the Tier-0 apply IS the post-apply
        snapshot -> full L1-L9 here. main/assist: the batch job runs the full pass
        post-extraction-apply; only L9 (prose-only, needs no snapshot) runs NOW so the
        guard can escalate on the very next turn (Q12). Cooldown dedups the overlap."""
        try:
            if not self.cfg.linter.enabled or not (text and text.strip()):
                return
            name = (self.cfg.user_guard.name
                    or (self.jobs.user_names.get(ctx.session_id, "") if self.jobs else ""))
            aliases = tuple(self.cfg.user_guard.aliases)
            full = self.cfg.extraction.mode in ("off", "rules")
            state = current_state(self.store, ctx.branch_id)
            cfg = self.cfg
            if not full:                      # L9-only quick pass (see docstring)
                import copy
                cfg = copy.deepcopy(self.cfg)
                cfg.linter.rules_off = sorted(set(cfg.linter.rules_off)
                                              | {f"L{i}" for i in range(1, 9)})
            utext = ""
            try:                              # L9 door + L11 read the player's message (0b)
                rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index,
                                                 ctx.turn_index)
                utext = next((r["user_text"] or "" for r in rows), "")
            except Exception:
                utext = ""
            fresh = linter.lint_turn(self.store, cfg, ctx.session_id, ctx.branch_id,
                                     ctx.turn_index, state, text, klass=ctx.klass,
                                     user_name=name, user_aliases=aliases,
                                     user_text=utext)
            if full:                      # Tier-0 apply IS the post-apply snapshot (03 SS8)
                director.stage(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                               ctx.turn_index, state, fresh, user_name=name,
                               user_aliases=aliases)
            else:                         # batch job owns the note; consume the stale one
                self.store.write_note(ctx.session_id, ctx.turn_index + 1, "")
        except Exception as exc:              # invariant 3: linter never breaks the turn
            log.warning("lint pass skipped: %s", type(exc).__name__)

    def _discover(self, ctx: PostContext) -> None:
        """Entity discovery over this turn's captured prose (08 B2). Any error stays here."""
        try:
            if self.cfg.extraction.mode == "off":
                return
            rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index, ctx.turn_index)
            text = "\n".join((r["user_text"] or "") + "\n" + (r["assistant_text"] or "")
                             for r in rows)
            if not text.strip():
                return
            state = current_state(self.store, ctx.branch_id)
            guard = self.cfg.user_guard.name or \
                (self.jobs.user_names.get(ctx.session_id, "") if self.jobs else "")
            known = discovery.known_names(state, (guard, ctx.speaker or ""))
            discovery.observe_text(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                                   ctx.turn_index, text, known)
            if getattr(self.cfg, "specialization", None) is not None \
                    and self.cfg.specialization.name == "rpg":
                # RPG-4: places persist once too — rpg-gated so a `none` session's journal
                # stays byte-identical (invariant: no fingerprint under none).
                discovery.observe_locations(self.store, self.cfg, ctx.session_id,
                                            ctx.branch_id, ctx.turn_index, text, state)
        except Exception as exc:
            log.warning("discovery pass failed open: %s", type(exc).__name__)


def _response_text(raw: bytes, content_type: str) -> Optional[str]:
    """Assistant text from a completed response: SSE deltas or plain JSON body."""
    if b"data:" in raw[:256] or "text/event-stream" in (content_type or ""):
        parts = []
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                doc = json.loads(payload)
                ch = (doc.get("choices") or [{}])[0]
                parts.append((ch.get("delta") or {}).get("content")
                             or (ch.get("message") or {}).get("content") or "")
            except (json.JSONDecodeError, ValueError, AttributeError, IndexError):
                continue
        return "".join(parts)
    try:
        doc = json.loads(raw)
        ch = (doc.get("choices") or [{}])[0]
        return (ch.get("message") or {}).get("content") or (ch.get("text") or "")
    except (json.JSONDecodeError, ValueError, AttributeError, IndexError):
        return None
