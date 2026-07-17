"""Project one committed RPG turn into the pre-display narration truth contract.

This module is a read-only bridge between reducer-owned state and the pure narration truth gate.
It does not interpret Player prose, settle mechanics, or trust model output.  Every current-turn
HP or defeat fact must be reconstructable from a complete semantic settlement or the frozen
autonomous-opposition receipt; otherwise construction fails closed before lifecycle commit.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .capability_glossary import content_fingerprint, normalize_phrase
from .compose import (
    DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
    DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
    deterministic_first_intent_components,
    render_deterministic_first_intent_text,
)
from .enemy_kits import intent_matches_frozen_kit
from .mechanic_settlement import (
    COMBAT_OPENING_CONTRACT,
    SKILL_CHECK_CONTRACT,
    WEAPON_ATTACK_CONTRACT,
    MechanicSettlementError,
    validate_mechanic_settlement,
    validate_mechanic_settlement_row,
)
from .narration_truth_gate import (
    FALLOUT_FACT_SCHEMA,
    OPPOSITION_FACT_SCHEMA,
    PENDING_INTENT_FACT_SCHEMA,
    TARGET_OUTCOME_SCHEMA,
    NarrationTruthGateError,
    build_narration_truth_contract,
)
from .state import combatant_label
from .narrator_realization import (
    NarratorRealizationError,
    build_narrator_realization,
    build_narrator_realization_from_state,
    validate_narrator_realization,
)
from .semantic_transition_truth import (
    SemanticTransitionTruthError,
    project_journal_transitions,
    validate_transition_projection,
)


_SEMANTIC_EVIDENCE_JOURNAL_OPS = frozenset(
    {
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
    }
)

# These operations deliberately change engine bookkeeping without asserting a fictional outcome.
# Keep this list tiny and exhaustive: adding a reducer operation here is an explicit decision that
# its state is not a narrator claim. ``clock_tick`` advances only the internal scene-minutes counter
# (unlike ``time_advance`` it triggers no time-of-day, recovery, craving, or world consequence), and
# ``stagnation`` is the Director's repetition signal. Everything else needs an exact projection or
# fails closed.
_BOOKKEEPING_ONLY_JOURNAL_OPS = frozenset({"clock_tick", "stagnation"})

# Only these reducer members can inherit a mechanic settlement reference. Restricting the marker
# prevents a forged/future ``_settlement_ref`` on an unrelated mutation from borrowing a receipt.
_SETTLEMENT_MEMBER_JOURNAL_OPS = frozenset(
    {
        "check",
        "combatant_spawn",
        "scene_set",
        "combatant_hp",
        "master_tick",
        "effect_add",
    }
)


class SemanticTruthRuntimeError(ValueError):
    """Committed state cannot be projected into one complete narration truth contract."""


FENCED_RUNTIME_TRUTH_RESULT_SCHEMA = "fenced-runtime-narration-truth/1"


@dataclass(frozen=True, slots=True)
class FencedRuntimeTruthResult:
    """Detached immutable persistence input for one fenced narration artifact.

    The validated contract and transition projection are stored as canonical JSON text, not as
    mutable Python containers.  Accessors return detached copies so downstream artifact builders
    cannot change the proof basis after lifecycle construction.
    """

    fingerprint: str
    truth_contract_fingerprint: str
    transition_projection_fingerprint: str
    _truth_contract_json: str = dataclass_field(repr=False)
    _transition_projection_json: str = dataclass_field(repr=False)

    @classmethod
    def _from_validated(
        cls,
        truth_contract: Mapping[str, Any],
        transition_projection: Mapping[str, Any],
    ) -> FencedRuntimeTruthResult:
        contract = deepcopy(dict(truth_contract))
        projection = deepcopy(dict(transition_projection))
        basis = {
            "schema": FENCED_RUNTIME_TRUTH_RESULT_SCHEMA,
            "truth_contract": contract,
            "transition_projection": projection,
        }
        return cls(
            fingerprint=content_fingerprint(basis),
            truth_contract_fingerprint=str(contract["fingerprint"]),
            transition_projection_fingerprint=str(projection["fingerprint"]),
            _truth_contract_json=json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            _transition_projection_json=json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    @property
    def truth_contract(self) -> dict[str, Any]:
        return json.loads(self._truth_contract_json)

    @property
    def transition_projection(self) -> dict[str, Any]:
        return json.loads(self._transition_projection_json)

    def to_persistence_payload(self) -> dict[str, Any]:
        return {
            "schema": FENCED_RUNTIME_TRUTH_RESULT_SCHEMA,
            "truth_contract": self.truth_contract,
            "transition_projection": self.transition_projection,
            "fingerprint": self.fingerprint,
        }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SemanticTruthRuntimeError(f"{label} must be an object with string fields")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticTruthRuntimeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise SemanticTruthRuntimeError(f"{label} must be at least {minimum}")
    return value


def _plain(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SemanticTruthRuntimeError(f"{label} must be text")
    text = " ".join(value.split())
    if not text:
        raise SemanticTruthRuntimeError(f"{label} must not be empty")
    return text


def _derived_ref(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}.{content_fingerprint(payload)[7:39]}"


def _delivery_mode(state: Mapping[str, Any]) -> str:
    marker = state.get("_settled_retry")
    if marker is None:
        return "first_delivery"
    row = _mapping(marker, "settled retry marker")
    kind = row.get("kind")
    if kind == "lost_reply":
        return "lost_reply_retry"
    if kind == "swipe_replay":
        return "regeneration_retry"
    raise SemanticTruthRuntimeError("settled retry marker kind is unsupported")


def _journal_payloads(
    state: Mapping[str, Any], key: str, payload_key: str, turn: int
) -> list[dict[str, Any]]:
    raw = state.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SemanticTruthRuntimeError(f"{key} journal must be a list")
    current: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        row = _mapping(value, f"{key}[{index}]")
        if set(row) != {"turn", payload_key}:
            raise SemanticTruthRuntimeError(f"{key}[{index}] journal row shape is invalid")
        row_turn = _integer(row["turn"], f"{key}[{index}].turn", minimum=0)
        payload = _mapping(row[payload_key], f"{key}[{index}].{payload_key}")
        if row_turn == turn:
            current.append(deepcopy(dict(payload)))
    return current


def _validated_settlement_receipts_by_turn(
    state: Mapping[str, Any], *, label: str
) -> dict[int, dict[str, dict[str, Any]]]:
    """Validate the complete retained settlement ledger without lending history to this turn."""
    raw = state.get("mechanic_settlements")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise SemanticTruthRuntimeError(f"{label} mechanic settlement ledger must be a list")
    by_turn: dict[int, dict[str, dict[str, Any]]] = {}
    for index, value in enumerate(raw):
        row = _mapping(value, f"{label} mechanic_settlements[{index}]")
        if set(row) != {"turn", "receipt"}:
            raise SemanticTruthRuntimeError(
                f"{label} mechanic_settlements[{index}] row shape is invalid"
            )
        row_turn = _integer(
            row["turn"], f"{label} mechanic_settlements[{index}].turn", minimum=0
        )
        receipt = validate_mechanic_settlement(row["receipt"])
        settlement_ref = receipt["settlement_ref"]
        current = by_turn.setdefault(row_turn, {})
        if settlement_ref in current:
            raise SemanticTruthRuntimeError(
                f"{label} repeats one mechanic settlement receipt in turn {row_turn}"
            )
        current[settlement_ref] = receipt
    return by_turn


def _validate_current_settlement_window(
    *,
    pre_state: object,
    post_state: object,
    journal_rows: Sequence[Mapping[str, Any]],
    turn_index: int,
) -> None:
    """Prove exact settlement wrapper/member cardinality before replay or projection.

    The immutable prepared wrapper and its validated receipt are the authority.  State deltas are
    deliberately not used to guess which child operations belong to the settlement: every indexed
    child must be the exact top-level projection of one wrapper member in the same current journal
    row, and every declared member must have exactly one such child.
    """
    turn = _integer(turn_index, "settlement window turn", minimum=0)
    pre = _mapping(pre_state, "settlement window pre-state")
    post = _mapping(post_state, "settlement window post-state")
    if isinstance(journal_rows, (str, bytes, bytearray)) or not isinstance(
        journal_rows, Sequence
    ):
        raise SemanticTruthRuntimeError(
            "settlement window journal_rows must be an exact ordered sequence"
        )

    # Locations are (journal-row ordinal, op ordinal, source, detached operation).  Keeping the
    # row identity prevents a current child from borrowing a wrapper retained elsewhere in the
    # bounded window.
    wrappers: dict[str, list[tuple[int, int, str, dict[str, Any]]]] = {}
    children: dict[
        str, dict[int, list[tuple[int, int, str, dict[str, Any]]]]
    ] = {}
    standalone_members: list[tuple[int, int, str, dict[str, Any]]] = []
    modern_standalone_members: list[tuple[int, int, str, dict[str, Any]]] = []
    for row_index, raw_row in enumerate(journal_rows):
        row = _mapping(raw_row, f"settlement journal_rows[{row_index}]")
        source = row.get("source")
        ops = row.get("ops")
        if not isinstance(source, str) or not isinstance(ops, list):
            raise SemanticTruthRuntimeError(
                f"settlement journal_rows[{row_index}] source or ops is invalid"
            )
        for op_index, raw_op in enumerate(ops):
            op = dict(
                _mapping(
                    raw_op,
                    f"settlement journal_rows[{row_index}].ops[{op_index}]",
                )
            )
            kind = op.get("op")
            is_wrapper = kind == "mechanic_settlement_commit"
            has_ref = "_settlement_ref" in op
            has_index = "_settlement_member_index" in op
            if not is_wrapper and not has_ref and not has_index:
                # Current V3 members are complete only inside their indexed settlement.  If the
                # wrapper and both projection markers are missing, the remaining operation must
                # not be reclassified as an ordinary transition.  Treat any frame-marked member
                # as modern here so absent, historical, cross-frame, malformed, and wrong-turn
                # references all remain incomplete.  A truly unframed legacy row keeps its
                # compatibility path when no current settlement is present.
                if kind in _SETTLEMENT_MEMBER_JOURNAL_OPS \
                        and "_semantic_frame_ref" in op:
                    modern_standalone_members.append(
                        (row_index, op_index, source, deepcopy(op))
                    )
                if (
                    source == "rule"
                    and kind in _SETTLEMENT_MEMBER_JOURNAL_OPS
                    and op.get("_turn", turn) == turn
                ):
                    standalone_members.append(
                        (row_index, op_index, source, deepcopy(op))
                    )
                continue
            if op.get("_turn", turn) != turn:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement occurrence belongs to another turn"
                )
            if source != "rule":
                raise SemanticTruthRuntimeError(
                    "mechanic settlement occurrence must come from the rule journal"
                )
            if is_wrapper:
                settlement_ref = op.get("settlement_ref")
                if not isinstance(settlement_ref, str) or not settlement_ref:
                    raise SemanticTruthRuntimeError(
                        "mechanic settlement wrapper lacks an exact settlement_ref"
                    )
                wrappers.setdefault(settlement_ref, []).append(
                    (row_index, op_index, source, deepcopy(op))
                )
                if has_ref or has_index:
                    raise SemanticTruthRuntimeError(
                        "mechanic settlement wrapper cannot also be an indexed child"
                    )
                continue
            if has_ref != has_index:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child needs both reference and member index"
                )
            settlement_ref = op.get("_settlement_ref")
            member_index = op.get("_settlement_member_index")
            if not isinstance(settlement_ref, str) or not settlement_ref:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child reference is invalid"
                )
            if (
                isinstance(member_index, bool)
                or not isinstance(member_index, int)
                or member_index < 0
            ):
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child index is invalid"
                )
            if kind not in _SETTLEMENT_MEMBER_JOURNAL_OPS:
                raise SemanticTruthRuntimeError(
                    f"operation {kind!r} cannot be a mechanic settlement child"
                )
            children.setdefault(settlement_ref, {}).setdefault(member_index, []).append(
                (row_index, op_index, source, deepcopy(op))
            )

    if modern_standalone_members:
        raise SemanticTruthRuntimeError(
            "current V3 mechanic member requires one complete indexed mechanic settlement"
        )

    pre_receipts_by_turn = _validated_settlement_receipts_by_turn(pre, label="pre-state")
    post_receipts_by_turn = _validated_settlement_receipts_by_turn(post, label="post-state")
    pre_receipt_refs = {
        settlement_ref
        for receipts in pre_receipts_by_turn.values()
        for settlement_ref in receipts
    }
    post_current_receipts = post_receipts_by_turn.get(turn, {})
    all_refs = set(wrappers) | set(children)

    if pre_receipts_by_turn.get(turn):
        raise SemanticTruthRuntimeError(
            "current settlement truth cannot borrow a pre-state receipt"
        )
    if set(post_current_receipts) != set(wrappers):
        raise SemanticTruthRuntimeError(
            "current post-state settlement receipts need exactly their current wrappers"
        )
    if any(settlement_ref in pre_receipt_refs for settlement_ref in all_refs):
        raise SemanticTruthRuntimeError(
            "current settlement window cannot reuse a historical receipt identity"
        )

    for settlement_ref in sorted(all_refs | set(post_current_receipts)):
        wrapper_rows = wrappers.get(settlement_ref, [])
        if len(wrapper_rows) != 1:
            raise SemanticTruthRuntimeError(
                "mechanic settlement window needs exactly one wrapper per settlement_ref"
            )
        wrapper_row_index, wrapper_op_index, _source, wrapper = wrapper_rows[0]
        try:
            receipt = validate_mechanic_settlement(wrapper.get("receipt"))
            store_row = validate_mechanic_settlement_row(wrapper.get("_store_row"))
        except MechanicSettlementError as exc:
            raise SemanticTruthRuntimeError(str(exc)) from exc
        if (
            wrapper.get("_settlement_prepared") is not True
            or wrapper.get("settlement_ref") != receipt["settlement_ref"]
            or wrapper.get("contract_id") != receipt["contract_id"]
            or wrapper.get("frame_ref") != receipt["frame_ref"]
            or wrapper.get("_semantic_frame_ref") != receipt["frame_ref"]
            or store_row["receipt"] != receipt
            or post_current_receipts.get(settlement_ref) != receipt
        ):
            raise SemanticTruthRuntimeError(
                "mechanic settlement wrapper disagrees with its canonical receipt"
            )
        members = wrapper.get("members")
        if not isinstance(members, list) or any(
            not isinstance(member, Mapping) for member in members
        ):
            raise SemanticTruthRuntimeError(
                "mechanic settlement wrapper members must be an exact list"
            )
        if any(
            "_settlement_ref" in member or "_settlement_member_index" in member
            for member in members
        ):
            raise SemanticTruthRuntimeError(
                "mechanic settlement wrapper contains an indexed nested member"
            )
        indexed = children.get(settlement_ref, {})
        expected_indexes = set(range(len(members)))
        if set(indexed) != expected_indexes:
            raise SemanticTruthRuntimeError(
                "mechanic settlement children are missing, gapped, or extra"
            )
        for member_index, member in enumerate(members):
            observed = indexed[member_index]
            if len(observed) != 1:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child occurrence is duplicated"
                )
            child_row_index, child_op_index, _child_source, child = observed[0]
            if child_row_index != wrapper_row_index or child_op_index <= wrapper_op_index:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child cannot borrow a detached wrapper"
                )
            expected_child = {
                **deepcopy(dict(member)),
                "_settlement_ref": settlement_ref,
                "_settlement_member_index": member_index,
            }
            if child != expected_child:
                raise SemanticTruthRuntimeError(
                    "mechanic settlement child kind, index, or payload is not exact"
                )

    if wrappers or children:
        for _row_index, _op_index, _source, _standalone in standalone_members:
            raise SemanticTruthRuntimeError(
                "current mechanic settlement window cannot admit an unindexed standalone "
                "member-family operation"
            )


def _human_label(entity_id: str) -> str:
    words = entity_id.replace("#", " ").replace("_", " ").replace(".", " ").split()
    return " ".join(word.capitalize() for word in words) or entity_id


class _Labels:
    def __init__(
        self,
        state: Mapping[str, Any],
        frames: list[dict[str, Any]],
    ) -> None:
        self._state = state
        self._frames = frames
        self._exact: dict[str, str] = {}

    def bind(self, entity_id: object, label: object, source: str) -> str:
        ref = _plain(entity_id, f"{source} entity id")
        text = _plain(label, f"{source} label")
        prior = self._exact.get(ref)
        if prior is not None and prior != text:
            raise SemanticTruthRuntimeError(
                f"entity {ref} has conflicting current-turn labels {prior!r} and {text!r}"
            )
        self._exact[ref] = text
        return text

    def resolve(self, entity_id: object, explicit: object | None = None) -> str:
        ref = _plain(entity_id, "entity id")
        if explicit is not None:
            return self.bind(ref, explicit, "committed fact")
        if ref in self._exact:
            return self._exact[ref]

        entities = self._state.get("entities")
        entity = entities.get(ref) if isinstance(entities, Mapping) else None
        if isinstance(entity, Mapping) and str(entity.get("name") or "").strip():
            return self.bind(ref, entity["name"], "entity ledger")

        combat = self._state.get("combat")
        rows = combat.get("combatants") if isinstance(combat, Mapping) else None
        if isinstance(rows, Mapping):
            direct = rows.get(ref)
            if isinstance(direct, Mapping) and str(direct.get("name") or "").strip():
                return self.bind(ref, direct["name"], "combat ledger")
            matches = [
                row
                for row in rows.values()
                if isinstance(row, Mapping) and row.get("eid") == ref
                and str(row.get("name") or "").strip()
            ]
            if len(matches) == 1:
                return self.bind(ref, matches[0]["name"], "combat ledger")

        players = self._state.get("player")
        player = players.get(ref) if isinstance(players, Mapping) else None
        if isinstance(player, Mapping) and str(player.get("name") or "").strip():
            return self.bind(ref, player["name"], "Player ledger")

        for frame in self._frames:
            if frame.get("target_entity_id") == ref and str(frame.get("target_name") or "").strip():
                return self.bind(ref, frame["target_name"], "semantic frame")
        return self.bind(ref, _human_label(ref), "stable identity")


def _realization(
    state: Mapping[str, Any],
    *,
    turn: int,
    frames: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    packet = build_narrator_realization_from_state(state)
    if packet is None:
        if frames or settlement_rows:
            raise SemanticTruthRuntimeError(
                "current semantic journals cannot produce a complete narrator realization"
            )
        return build_narrator_realization(turn, delivery_mode=_delivery_mode(state))
    valid = validate_narrator_realization(packet)
    if valid["turn"] != turn:
        raise SemanticTruthRuntimeError("narrator realization belongs to another turn")
    return valid


def _validate_admitted_semantic_chains(
    state: Mapping[str, Any],
    *,
    turn: int,
    frames: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
    realization: Mapping[str, Any],
) -> None:
    """Require a complete semantic chain only when a mechanic actually cites it.

    Recognition/binding/alignment rows are useful diagnostic metadata even when the Player merely
    looks, speaks, or attempts something unsupported. They are not fictional outcomes and may be
    retained without creating a narrator claim. Once a mechanic settlement cites a frame, however,
    the entire current-turn chain and its realization are mandatory and exact.
    """
    if not settlement_rows:
        return
    meanings = _journal_payloads(state, "semantic_meanings", "meaning", turn)
    bindings = _journal_payloads(state, "semantic_bindings", "binding", turn)
    alignments = _journal_payloads(
        state, "semantic_world_alignments", "alignment", turn
    )

    def index_by_fingerprint(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for row in rows:
            ref = _plain(row.get("fingerprint"), f"{label} fingerprint")
            if ref in indexed:
                raise SemanticTruthRuntimeError(f"current {label} fingerprint is duplicated")
            indexed[ref] = row
        return indexed

    frame_by_ref = index_by_fingerprint(frames, "semantic frame")
    meaning_by_ref = index_by_fingerprint(meanings, "semantic meaning")
    binding_by_ref = index_by_fingerprint(bindings, "semantic binding")
    alignment_by_ref = index_by_fingerprint(alignments, "semantic world alignment")
    realized = {
        row["frame_ref"]
        for bucket in (
            realization["asserted_settled"],
            realization["asserted_unresolved"],
            realization["attributed_noncurrent"],
        )
        for row in bucket
    }

    for raw in settlement_rows:
        receipt = validate_mechanic_settlement(raw)
        frame_ref = receipt["frame_ref"]
        frame = frame_by_ref.get(frame_ref)
        if frame is None:
            raise SemanticTruthRuntimeError(
                "admitted mechanic settlement lacks its current semantic frame"
            )
        meaning_ref = _plain(frame.get("meaning_ref"), "semantic frame meaning_ref")
        binding_ref = _plain(
            frame.get("meaning_binding_ref"), "semantic frame meaning_binding_ref"
        )
        alignment_refs = frame.get("world_alignment_refs")
        if not isinstance(alignment_refs, list) or any(
            not isinstance(ref, str) or not ref for ref in alignment_refs
        ):
            raise SemanticTruthRuntimeError(
                "semantic frame world alignment references are malformed"
            )
        if meaning_ref not in meaning_by_ref:
            raise SemanticTruthRuntimeError(
                "admitted semantic frame lacks its current meaning receipt"
            )
        binding = binding_by_ref.get(binding_ref)
        if binding is None or binding.get("meaning_ref") != meaning_ref:
            raise SemanticTruthRuntimeError(
                "admitted semantic frame lacks its exact current meaning binding"
            )
        for alignment_ref in alignment_refs:
            alignment = alignment_by_ref.get(alignment_ref)
            if alignment is None or alignment.get("recognition_ref") != binding_ref:
                raise SemanticTruthRuntimeError(
                    "admitted semantic frame lacks an exact current world alignment"
                )
        if frame_ref not in realized:
            raise SemanticTruthRuntimeError(
                "admitted semantic frame is absent from narrator realization"
            )


def _opposition_fact(
    player_id: str,
    card: Mapping[str, Any],
    *,
    turn: int,
    labels: _Labels,
) -> dict[str, Any] | None:
    raw = card.get("_opposition_last")
    if raw is None:
        return None
    row = _mapping(raw, f"player {player_id} opposition receipt")
    action_turn = _integer(row.get("turn"), "opposition receipt turn", minimum=0)
    if action_turn != turn:
        return None

    target_id = _plain(row.get("target"), "opposition target")
    if target_id != player_id:
        raise SemanticTruthRuntimeError("opposition receipt targets a different Player")
    actor_id = _plain(row.get("actor"), "opposition actor")
    actor_label = labels.bind(actor_id, row.get("actor_name"), "opposition receipt")
    target_label = labels.bind(target_id, row.get("target_name"), "opposition receipt")
    move_id = _plain(row.get("move_id"), "opposition move id")
    move_label = _plain(row.get("move_name"), "opposition move label")
    intent_ref = _plain(row.get("intent_id"), "opposition intent id")
    effect_id = _plain(row.get("effect_id"), "opposition effect id")
    delta = _integer(row.get("delta"), "opposition HP delta")
    damage = _integer(row.get("damage"), "opposition damage", minimum=0)
    if delta > 0 or damage != -delta:
        raise SemanticTruthRuntimeError("opposition damage and committed HP delta disagree")
    hp_current = _integer(row.get("hp_cur"), "opposition immediate HP", minimum=0)
    hp_maximum = _integer(row.get("hp_max"), "opposition maximum HP", minimum=1)
    if hp_current > hp_maximum:
        raise SemanticTruthRuntimeError("opposition immediate HP exceeds its maximum")

    tier = _plain(row.get("tier"), "opposition result tier").upper()
    if tier not in {"CRITS", "HITS", "GRAZES", "MISSES"}:
        raise SemanticTruthRuntimeError("opposition result tier is unsupported")
    reaction = row.get("reaction")
    blocked = isinstance(reaction, Mapping) and reaction.get("applied") is True
    if damage > 0 and tier == "MISSES":
        raise SemanticTruthRuntimeError("opposition miss cannot carry HP harm")
    outcome = "hit" if damage > 0 else ("blocked" if blocked else "miss")

    damage_after = row.get("damage_after")
    if damage_after is not None and _integer(
        damage_after, "opposition damage_after", minimum=0
    ) != damage:
        raise SemanticTruthRuntimeError("opposition damage_after disagrees with HP delta")
    damage_before = row.get("damage_before")
    damage_saved = row.get("damage_saved")
    if damage_before is not None or damage_saved is not None:
        before = _integer(damage_before, "opposition damage_before", minimum=0)
        saved = _integer(damage_saved, "opposition damage_saved", minimum=0)
        if before < damage or before - damage != saved:
            raise SemanticTruthRuntimeError("opposition mitigation arithmetic is inconsistent")

    construction_payload = {
        "schema": "runtime-opposition-construction/1",
        "turn": turn,
        "intent_ref": intent_ref,
        "effect_id": effect_id,
        "actor_id": actor_id,
        "target_id": target_id,
        "move_id": move_id,
        "tier": tier,
        "delta": delta,
        "hp_cur": hp_current,
        "hp_max": hp_maximum,
        "outcome": outcome,
    }
    construction_ref = content_fingerprint(construction_payload)
    occurrence_ref = _derived_ref("opposition", construction_payload)
    effects = [] if damage == 0 else [{"kind": "harm", "detail": "hp", "amount": delta}]
    return {
        "schema": OPPOSITION_FACT_SCHEMA,
        "occurrence_ref": occurrence_ref,
        "intent_ref": intent_ref,
        "construction_ref": construction_ref,
        "actor_id": actor_id,
        "actor_label": actor_label,
        "target_id": target_id,
        "target_label": target_label,
        "move_id": move_id,
        "move_label": move_label,
        "outcome": outcome,
        "effects": effects,
    }


def _opposition_facts(
    state: Mapping[str, Any], *, turn: int, labels: _Labels
) -> list[dict[str, Any]]:
    players = state.get("player")
    if players is None:
        return []
    players = _mapping(players, "Player ledger")
    facts: list[dict[str, Any]] = []
    for player_id, value in players.items():
        card = _mapping(value, f"Player card {player_id}")
        fact = _opposition_fact(player_id, card, turn=turn, labels=labels)
        hp_last = card.get("_hp_adj_last")
        current_hp_receipt = False
        if hp_last is not None:
            hp_row = _mapping(hp_last, f"Player {player_id} HP receipt")
            hp_turn = _integer(hp_row.get("turn"), "Player HP receipt turn", minimum=0)
            if hp_turn == turn:
                current_hp_receipt = True
                hp_delta = _integer(hp_row.get("delta"), "Player HP receipt delta")
                if fact is None or fact["effects"] and fact["effects"][0]["amount"] != hp_delta:
                    raise SemanticTruthRuntimeError(
                        "current Player HP change lacks one exact autonomous opposition cause"
                    )
                if not fact["effects"] and hp_delta != 0:
                    raise SemanticTruthRuntimeError(
                        "current Player HP change and opposition outcome disagree"
                    )
        if fact is not None:
            if not current_hp_receipt:
                raise SemanticTruthRuntimeError(
                    "current opposition action lacks its committed Player HP receipt"
                )
            facts.append(fact)
    return sorted(facts, key=lambda row: row["occurrence_ref"])


def _pending_intent_facts(
    state: Mapping[str, Any],
    *,
    turn: int,
    labels: _Labels,
    combat_opening: bool,
) -> list[dict[str, Any]]:
    """Project one fully bound future enemy action and its exact response-window prose."""
    combat = state.get("combat")
    if not isinstance(combat, Mapping):
        return []
    raw = combat.get("pending_intent")
    if raw is None:
        return []
    intent = _mapping(raw, "pending enemy intent")
    prepared_turn = _integer(
        intent.get("prepared_turn"), "pending enemy intent prepared turn", minimum=0
    )
    if prepared_turn != turn:
        return []
    if combat.get("active") is not True:
        raise SemanticTruthRuntimeError("current pending enemy intent requires active combat")
    actor_id = _plain(intent.get("actor"), "pending enemy intent actor")
    target_id = _plain(intent.get("target"), "pending enemy intent target")
    move_id = _plain(intent.get("move_id"), "pending enemy intent move id")
    intent_ref = _plain(intent.get("id"), "pending enemy intent id")
    rows = combat.get("combatants")
    actor = rows.get(actor_id) if isinstance(rows, Mapping) else None
    if not isinstance(actor, Mapping) or not intent_matches_frozen_kit(intent, actor):
        raise SemanticTruthRuntimeError(
            "pending enemy intent is not an exact member of its frozen actor kit"
        )
    base_actor_label = labels.bind(
        actor_id,
        intent.get("actor_name") or actor.get("name"),
        "pending enemy intent",
    )
    target_label = labels.bind(target_id, intent.get("target_name"), "pending enemy intent")
    move_label = _plain(intent.get("move_name"), "pending enemy intent move name")
    tell = _plain(intent.get("tell"), "pending enemy intent visible tell")
    opening_components = deterministic_first_intent_components(state)
    if combat_opening:
        if opening_components is None:
            raise SemanticTruthRuntimeError(
                "combat opening lacks its exact deterministic first-intent response window"
            )
        if (
            opening_components["actor_id"] != actor_id
            or opening_components["target_id"] != target_id
            or opening_components["move_id"] != move_id
            or opening_components["target_label"] != target_label
            or opening_components["move_label"] != move_label
            or opening_components["tell"] != tell
        ):
            raise SemanticTruthRuntimeError(
                "combat opening response window differs from its pending intent"
            )
        actor_label = opening_components["actor_label"]
        separation = opening_components["separation"]
        visible_text = opening_components["visible_text"]
        opening_kind = "combat_opening"
    else:
        actor_label = combatant_label(dict(actor), base_actor_label)
        separation = ""
        visible_text = render_deterministic_first_intent_text(
            actor_label,
            tell,
            separation=separation,
        )
        opening_kind = "following_intent"
    if not visible_text:
        raise SemanticTruthRuntimeError("pending enemy intent has no exact visible response window")
    intent_snapshot = deepcopy(dict(intent))
    intent_fingerprint = content_fingerprint(intent_snapshot)
    construction_payload = {
        "schema": "runtime-pending-intent-construction/2",
        "turn": turn,
        "opening_kind": opening_kind,
        "intent_snapshot": intent_snapshot,
        "intent_fingerprint": intent_fingerprint,
        "actor_label": actor_label,
        "target_label": target_label,
        "move_label": move_label,
        "tell": tell,
        "separation": separation,
        "response_window": DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
        "response_window_text": DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
        "visible_text": visible_text,
    }
    construction_ref = content_fingerprint(construction_payload)
    return [
        {
            "schema": PENDING_INTENT_FACT_SCHEMA,
            "pending_ref": f"pending_intent.{construction_ref[7:39]}",
            "intent_ref": intent_ref,
            "intent_fingerprint": intent_fingerprint,
            "construction_ref": construction_ref,
            "prepared_turn": prepared_turn,
            "opening_kind": opening_kind,
            "actor_id": actor_id,
            "actor_label": actor_label,
            "target_id": target_id,
            "target_label": target_label,
            "move_id": move_id,
            "move_label": move_label,
            "tell": tell,
            "separation": separation,
            "response_window": DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
            "response_window_text": DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
            "visible_text": visible_text,
            "intent_snapshot": intent_snapshot,
        }
    ]


def _semantic_evidence_is_recorded(
    op: Mapping[str, Any], state: Mapping[str, Any], *, turn: int
) -> bool:
    locations = {
        "semantic_meaning_commit": ("semantic_meanings", "meaning"),
        "semantic_binding_commit": ("semantic_bindings", "binding"),
        "semantic_world_alignment_commit": (
            "semantic_world_alignments",
            "alignment",
        ),
        "semantic_frame_commit": ("semantic_frames", "frame"),
    }
    kind = str(op.get("op") or "")
    location = locations.get(kind)
    if location is None:
        return False
    ledger_key, payload_key = location
    payload = op.get(payload_key)
    if not isinstance(payload, Mapping):
        return False
    return any(
        row == dict(payload)
        for row in _journal_payloads(state, ledger_key, payload_key, turn)
    )


def _settlement_outcomes(
    settlement_rows: list[dict[str, Any]],
    realization: Mapping[str, Any],
    *,
    labels: _Labels,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[str],
    set[str],
    set[str],
]:
    settled_by_frame = {
        row["frame_ref"]: row for row in realization["asserted_settled"]
    }
    if len(settled_by_frame) != len(realization["asserted_settled"]):
        raise SemanticTruthRuntimeError("one frame appears twice in settled realization")

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    fallout_grouped: dict[tuple[str, str], dict[str, Any]] = {}
    hp_targets: set[str] = set()
    defeated_targets: set[str] = set()
    seen_receipts: set[str] = set()
    settlement_refs: set[str] = set()

    def add_effect(
        event: Mapping[str, Any],
        receipt: Mapping[str, Any],
        target_id: str,
        effect: dict[str, Any],
    ) -> None:
        key = (str(event["event_ref"]), target_id)
        group = grouped.setdefault(
            key,
            {
                "event": event,
                "receipt": receipt,
                "target_id": target_id,
                "effects": [],
            },
        )
        if group["receipt"]["receipt_fingerprint"] != receipt["receipt_fingerprint"]:
            raise SemanticTruthRuntimeError(
                "one Player occurrence has conflicting settlement receipts"
            )
        if effect in group["effects"]:
            raise SemanticTruthRuntimeError("mechanic settlement repeats one narrator effect")
        group["effects"].append(effect)

    def add_fallout(
        event: Mapping[str, Any],
        receipt: Mapping[str, Any],
        subject_id: str,
        effect: dict[str, Any],
    ) -> None:
        key = (str(event["event_ref"]), subject_id)
        group = fallout_grouped.setdefault(
            key,
            {
                "event": event,
                "receipt": receipt,
                "subject_id": subject_id,
                "effects": [],
            },
        )
        if group["receipt"]["receipt_fingerprint"] != receipt["receipt_fingerprint"]:
            raise SemanticTruthRuntimeError(
                "one Player occurrence has conflicting settlement receipts"
            )
        if effect in group["effects"]:
            raise SemanticTruthRuntimeError("mechanic settlement repeats one narrator effect")
        group["effects"].append(effect)

    for raw in settlement_rows:
        receipt = validate_mechanic_settlement(raw)
        receipt_ref = receipt["receipt_fingerprint"]
        if receipt_ref in seen_receipts:
            raise SemanticTruthRuntimeError("current mechanic settlement receipt is duplicated")
        seen_receipts.add(receipt_ref)
        settlement_refs.add(receipt["settlement_ref"])
        event = settled_by_frame.get(receipt["frame_ref"])
        if event is None:
            raise SemanticTruthRuntimeError(
                "current mechanic settlement has no exact narrator occurrence"
            )
        if receipt["contract_id"] not in {
            COMBAT_OPENING_CONTRACT,
            SKILL_CHECK_CONTRACT,
            WEAPON_ATTACK_CONTRACT,
        }:
            raise SemanticTruthRuntimeError("current mechanic settlement contract is unsupported")
        # Receipts retain every exact admission row.  Narrator realization names each canonical
        # change family once, so compare the stable first occurrence of each already-sorted kind.
        applied_kinds = list(dict.fromkeys(
            row["kind"] for row in receipt["applied_changes"]
        ))
        if event["settled_change_kinds"] != applied_kinds:
            raise SemanticTruthRuntimeError(
                "mechanic settlement changes disagree with narrator realization"
            )
        actor_id = _plain(event["event_meaning"]["actor_id"], "settled Player actor")
        for change in receipt["applied_changes"]:
            kind = change["kind"]
            if kind == "hp":
                target_id = _plain(change["subject_id"], "weapon settlement target")
                if event["event_meaning"]["target_entity_id"] != target_id:
                    raise SemanticTruthRuntimeError(
                        "weapon settlement and narrator occurrence target different entities"
                    )
                delta = _integer(change["delta"], "weapon settlement HP delta")
                hp_targets.add(target_id)
                if delta < 0:
                    add_effect(
                        event,
                        receipt,
                        target_id,
                        {"kind": "harm", "detail": "hp", "amount": delta},
                    )
                if receipt["outcome"] == "defeat":
                    add_effect(
                        event,
                        receipt,
                        target_id,
                        {"kind": "defeat", "detail": "defeated", "amount": None},
                    )
                    defeated_targets.add(target_id)
            elif kind == "cost":
                add_fallout(
                    event,
                    receipt,
                    actor_id,
                    {
                        "kind": "resource",
                        "detail": _plain(change["resource_id"], "settled resource id"),
                        "amount": _integer(change["delta"], "settled resource delta"),
                    },
                )
            elif kind == "mastery":
                capability_id = _plain(
                    change["capability_id"], "settled mastery capability"
                )
                add_fallout(
                    event,
                    receipt,
                    actor_id,
                    {
                        "kind": "resource",
                        "detail": f"mastery {capability_id}",
                        "amount": _integer(change["delta"], "settled mastery delta"),
                    },
                )
            elif kind == "cooldown":
                ability_id = _plain(change["ability_id"], "settled cooldown ability")
                add_fallout(
                    event,
                    receipt,
                    actor_id,
                    {
                        "kind": "time",
                        "detail": f"cooldown {ability_id}",
                        "amount": _integer(change["delta"], "settled cooldown delta"),
                    },
                )
            elif kind == "consequence":
                add_fallout(
                    event,
                    receipt,
                    actor_id,
                    {
                        "kind": "status",
                        "detail": _plain(change["effect_id"], "settled consequence effect"),
                        "amount": None,
                    },
                )
            elif kind == "target_admission":
                if receipt["contract_id"] == COMBAT_OPENING_CONTRACT:
                    # Opening admission is code-owned setup, not a visible fictional outcome.
                    # The exact pending-intent fact below carries the one response-window beat.
                    continue
                target_id = _plain(change["entity_id"], "admitted target")
                add_fallout(
                    event,
                    receipt,
                    target_id,
                    {"kind": "world", "detail": "target admitted", "amount": None},
                )
            elif kind == "scene_transition":
                if receipt["contract_id"] == COMBAT_OPENING_CONTRACT:
                    continue
                add_fallout(
                    event,
                    receipt,
                    "world",
                    {"kind": "world", "detail": "scene transition", "amount": None},
                )
            else:
                raise SemanticTruthRuntimeError(
                    f"settlement change {kind!r} has no narration truth projection"
                )

    outcomes: list[dict[str, Any]] = []
    for group in grouped.values():
        event = group["event"]
        receipt = group["receipt"]
        target_id = group["target_id"]
        effects = group["effects"]
        target_label = "world" if target_id == "world" else labels.resolve(target_id)
        outcome_payload = {
            "schema": "runtime-target-outcome-construction/1",
            "settlement_ref": receipt["settlement_ref"],
            "receipt_fingerprint": receipt["receipt_fingerprint"],
            "event_ref": event["event_ref"],
            "target_id": target_id,
            "effects": effects,
        }
        outcomes.append(
            {
                "schema": TARGET_OUTCOME_SCHEMA,
                "outcome_ref": _derived_ref("outcome", outcome_payload),
                "source_event_ref": event["event_ref"],
                "construction_ref": content_fingerprint(outcome_payload),
                "target_id": target_id,
                "target_label": target_label,
                "effects": effects,
            }
        )

    fallout: list[dict[str, Any]] = []
    for group in fallout_grouped.values():
        event = group["event"]
        receipt = group["receipt"]
        subject_id = group["subject_id"]
        effects = group["effects"]
        subject_label = "world" if subject_id == "world" else labels.resolve(subject_id)
        construction_payload = {
            "schema": "runtime-settlement-fallout-construction/1",
            "settlement_ref": receipt["settlement_ref"],
            "receipt_fingerprint": receipt["receipt_fingerprint"],
            "event_ref": event["event_ref"],
            "subject_id": subject_id,
            "effects": effects,
        }
        fallout.append(
            {
                "schema": FALLOUT_FACT_SCHEMA,
                "fact_ref": _derived_ref("fallout.settlement", construction_payload),
                "cause_ref": event["event_ref"],
                "construction_ref": content_fingerprint(construction_payload),
                "subject_id": subject_id,
                "subject_label": subject_label,
                "effects": effects,
            }
        )
    return (
        sorted(outcomes, key=lambda row: row["outcome_ref"]),
        sorted(fallout, key=lambda row: row["fact_ref"]),
        hp_targets,
        defeated_targets,
        settlement_refs,
    )


def _player_fallout(
    state: Mapping[str, Any],
    opposition: list[dict[str, Any]],
    *,
    turn: int,
    labels: _Labels,
) -> tuple[list[dict[str, Any]], set[str]]:
    players = state.get("player")
    if not isinstance(players, Mapping):
        return [], set()
    opposition_by_target = {row["target_id"]: row for row in opposition}
    if len(opposition_by_target) != len(opposition):
        raise SemanticTruthRuntimeError("one Player has multiple current opposition actions")
    facts: list[dict[str, Any]] = []
    defeated_players: set[str] = set()
    for player_id, value in players.items():
        card = _mapping(value, f"Player card {player_id}")
        defeated = card.get("defeated")
        if defeated is None:
            continue
        row = _mapping(defeated, f"Player {player_id} defeat receipt")
        defeat_turn = _integer(row.get("turn"), "Player defeat turn", minimum=0)
        if defeat_turn != turn:
            continue
        outcome = _plain(row.get("outcome"), "Player defeat outcome")
        action = opposition_by_target.get(player_id)
        if action is None:
            raise SemanticTruthRuntimeError(
                "current Player defeat lacks its exact autonomous opposition occurrence"
            )
        if not any(
            effect["kind"] == "harm" and effect["amount"] < 0
            for effect in action["effects"]
        ):
            raise SemanticTruthRuntimeError("a non-harm opposition action cannot cause defeat")
        raw_last = _mapping(
            card.get("_opposition_last"), f"Player {player_id} opposition receipt"
        )
        if _integer(raw_last.get("hp_cur"), "opposition immediate HP", minimum=0) != 0:
            raise SemanticTruthRuntimeError(
                "Player defeat disagrees with opposition immediate HP"
            )
        construction_payload = {
            "schema": "runtime-defeat-fallout-construction/1",
            "turn": turn,
            "player_id": player_id,
            "outcome": outcome,
            "cause_ref": action["occurrence_ref"],
            "opposition_construction_ref": action["construction_ref"],
        }
        facts.append(
            {
                "schema": FALLOUT_FACT_SCHEMA,
                "fact_ref": _derived_ref("fallout.defeat", construction_payload),
                "cause_ref": action["occurrence_ref"],
                "construction_ref": content_fingerprint(construction_payload),
                "subject_id": player_id,
                "subject_label": labels.resolve(player_id),
                "effects": [
                    {"kind": "defeat", "detail": outcome, "amount": None}
                ],
            }
        )
        defeated_players.add(player_id)
    return facts, defeated_players


def _award_exp_fallout(
    journal_rows: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    realization: Mapping[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    turn: int,
    labels: _Labels,
) -> list[dict[str, Any]]:
    """Project code-awarded victory XP back to its exact defeated target occurrence."""
    operations = [
        op
        for _source, op in _validated_journal_operations(journal_rows, turn=turn)
        if op.get("op") == "award_exp"
    ]
    if not operations:
        return []
    player_events = {
        row["frame_ref"]: row
        for row in realization["asserted_settled"]
    }
    if len(player_events) != len(realization["asserted_settled"]):
        raise SemanticTruthRuntimeError(
            "settled narrator realization repeats one semantic frame"
        )

    totals: dict[str, int] = {}
    checked: list[tuple[int, dict[str, Any], Mapping[str, Any], int]] = []
    for index, op in enumerate(operations):
        player_id = _plain(op.get("char"), "experience award Player")
        amount = _integer(op.get("amount"), "experience award amount", minimum=0)
        frame_ref = _plain(
            op.get("_semantic_frame_ref"), "experience award semantic frame"
        )
        event = player_events.get(frame_ref)
        if event is None or event["event_meaning"]["actor_id"] != player_id:
            raise SemanticTruthRuntimeError(
                "experience award lacks its exact settled Player occurrence"
            )
        reason = normalize_phrase(str(op.get("reason") or ""))
        defeated = [
            row
            for row in outcomes
            if row["source_event_ref"] == event["event_ref"]
            and any(effect["kind"] == "defeat" for effect in row["effects"])
            and reason == normalize_phrase(f"defeated {row['target_label']}")
        ]
        if len(defeated) != 1:
            raise SemanticTruthRuntimeError(
                "experience award does not identify one exact defeated target"
            )
        totals[player_id] = totals.get(player_id, 0) + amount
        checked.append((index, op, event, amount))

    players = _mapping(state.get("player"), "Player ledger")
    for player_id, total in totals.items():
        card = _mapping(players.get(player_id), f"Player card {player_id}")
        post = _integer(card.get("xp"), f"Player {player_id} experience", minimum=0)
        # At the hard cap the pre-turn value is not reconstructable from post-state plus the
        # journal request. Fail closed instead of claiming a delta that may have been clamped.
        if post >= 10**7 or post < total:
            raise SemanticTruthRuntimeError(
                "experience award post-state does not prove its exact applied delta"
            )

    facts: list[dict[str, Any]] = []
    for index, op, event, amount in checked:
        player_id = str(op["char"])
        construction_payload = {
            "schema": "runtime-experience-award-construction/1",
            "turn": turn,
            "journal_ordinal": index,
            "event_ref": event["event_ref"],
            "frame_ref": event["frame_ref"],
            "player_id": player_id,
            "amount": amount,
            "reason": normalize_phrase(str(op.get("reason") or "")),
        }
        facts.append(
            {
                "schema": FALLOUT_FACT_SCHEMA,
                "fact_ref": _derived_ref("fallout.experience", construction_payload),
                "cause_ref": event["event_ref"],
                "construction_ref": content_fingerprint(construction_payload),
                "subject_id": player_id,
                "subject_label": labels.resolve(player_id),
                "effects": [
                    {"kind": "resource", "detail": "experience", "amount": amount}
                ],
            }
        )
    return facts


def _combat_end_fallout(
    state: Mapping[str, Any],
    *,
    turn: int,
    player_fallout: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    combat = state.get("combat")
    if combat is None:
        return []
    combat = _mapping(combat, "combat ledger")
    history = combat.get("history")
    if history is None:
        return []
    if not isinstance(history, list):
        raise SemanticTruthRuntimeError("combat history must be a list")
    current: list[Mapping[str, Any]] = []
    for index, value in enumerate(history):
        row = _mapping(value, f"combat history[{index}]")
        row_turn = _integer(row.get("turn"), f"combat history[{index}].turn", minimum=0)
        if row_turn == turn:
            current.append(row)
    if len(current) > 1:
        raise SemanticTruthRuntimeError("one turn cannot contain multiple combat endings")
    if not current:
        return []
    row = current[0]
    outcome = _plain(row.get("outcome"), "combat ending outcome")
    construction_payload = {
        "schema": "runtime-combat-end-construction/1",
        "turn": turn,
        "started_turn": row.get("started_turn"),
        "outcome": outcome,
        "defeated": deepcopy(row.get("defeated")),
        "survivors": deepcopy(row.get("survivors")),
        "loot": deepcopy(row.get("loot")),
    }
    for field in ("defeated", "survivors", "loot"):
        value = construction_payload[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SemanticTruthRuntimeError(f"combat ending {field} must be a text list")
    exact_causes: set[str] = set()
    if outcome == "defeat":
        exact_causes.update(
            fact["cause_ref"]
            for fact in player_fallout
            if any(effect["kind"] == "defeat" for effect in fact["effects"])
        )
    elif outcome == "victory":
        exact_causes.update(
            fact["source_event_ref"]
            for fact in outcomes
            if any(effect["kind"] == "defeat" for effect in fact["effects"])
        )
    cause_ref = (
        next(iter(exact_causes))
        if len(exact_causes) == 1
        else _derived_ref("combat_end", construction_payload)
    )
    construction_payload["cause_ref"] = cause_ref
    construction_ref = content_fingerprint(construction_payload)
    return [
        {
            "schema": FALLOUT_FACT_SCHEMA,
            "fact_ref": _derived_ref("fallout.combat_end", construction_payload),
            "cause_ref": cause_ref,
            "construction_ref": construction_ref,
            "subject_id": "world",
            "subject_label": "world",
            "effects": [
                {
                    "kind": "world",
                    "detail": f"combat ended {outcome}",
                    "amount": None,
                }
            ],
        }
    ]


def _check_combatant_coverage(
    state: Mapping[str, Any],
    *,
    turn: int,
    hp_targets: set[str],
    defeated_targets: set[str],
) -> None:
    combat = state.get("combat")
    rows = combat.get("combatants") if isinstance(combat, Mapping) else None
    if not isinstance(rows, Mapping):
        return
    for combatant_id, value in rows.items():
        row = _mapping(value, f"combatant {combatant_id}")
        identities = {str(combatant_id)}
        if isinstance(row.get("eid"), str) and row["eid"]:
            identities.add(row["eid"])
        struck_turn = row.get("_struck_turn")
        if struck_turn is not None and _integer(
            struck_turn, f"combatant {combatant_id} strike turn", minimum=0
        ) == turn and not identities.intersection(hp_targets):
            raise SemanticTruthRuntimeError(
                "current combatant HP change lacks its exact mechanic settlement"
            )
        defeated_turn = row.get("defeated_turn")
        if defeated_turn is not None and _integer(
            defeated_turn, f"combatant {combatant_id} defeat turn", minimum=0
        ) == turn and not identities.intersection(defeated_targets):
            raise SemanticTruthRuntimeError(
                "current combatant defeat lacks its exact settled Player occurrence"
            )


def _journal_op_is_covered(
    op: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    turn: int,
    settlement_refs: set[str],
    opposition: list[dict[str, Any]],
    pending_intents: list[dict[str, Any]],
    fallout: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    realization: Mapping[str, Any],
) -> bool:
    kind = op.get("op")
    if kind == "clock_tick":
        minutes = op.get("minutes")
        return isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0
    if kind == "stagnation":
        value = op.get("value")
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0.0 <= float(value) <= 1.0
        )
    if kind in _SEMANTIC_EVIDENCE_JOURNAL_OPS:
        return _semantic_evidence_is_recorded(op, state, turn=turn)
    settlement_ref = op.get("settlement_ref") \
        if kind == "mechanic_settlement_commit" else op.get("_settlement_ref")
    if (
        isinstance(settlement_ref, str)
        and settlement_ref in settlement_refs
        and (
            kind == "mechanic_settlement_commit"
            or kind in _SETTLEMENT_MEMBER_JOURNAL_OPS
        )
    ):
        if kind == "combatant_spawn" and isinstance(op.get("_initial_intent"), Mapping):
            initial = op["_initial_intent"]
            return any(
                row["intent_ref"] == initial.get("id")
                and row["actor_id"] == initial.get("actor")
                and row["move_id"] == initial.get("move_id")
                and row["intent_snapshot"] == dict(initial)
                for row in pending_intents
            )
        return True

    if kind == "hp_adj" and isinstance(op.get("_opposition"), Mapping):
        meta = op["_opposition"]
        intent_id = str(meta.get("intent_id") or meta.get("id") or "")
        target_id = str(op.get("char") or meta.get("target") or "")
        matches = [
            row
            for row in opposition
            if row["intent_ref"] == intent_id and row["target_id"] == target_id
        ]
        if len(matches) != 1:
            return False
        action = matches[0]
        if meta.get("actor") != action["actor_id"] or meta.get("move_id") != action["move_id"]:
            return False
        delta = op.get("_delta", op.get("delta"))
        if isinstance(delta, bool) or not isinstance(delta, int):
            return False
        expected = next(
            (effect["amount"] for effect in action["effects"] if effect["kind"] == "harm"),
            0,
        )
        if delta != expected:
            return False
        players = state.get("player")
        card = players.get(target_id) if isinstance(players, Mapping) else None
        last = card.get("_opposition_last") if isinstance(card, Mapping) else None
        if not isinstance(last, Mapping):
            return False
        return bool(
            isinstance(op.get("_effect_id"), str)
            and op["_effect_id"] == last.get("effect_id")
        )

    if kind == "enemy_intent_set":
        combat = state.get("combat")
        pending = combat.get("pending_intent") if isinstance(combat, Mapping) else None
        baked = op.get("_intent")
        if not isinstance(pending, Mapping) or not isinstance(baked, Mapping):
            return False
        if dict(baked) != dict(pending) or op.get("actor") != pending.get("actor"):
            return False
        return any(
            row["intent_ref"] == pending.get("id")
            and row["actor_id"] == pending.get("actor")
            and row["move_id"] == pending.get("move_id")
            and row["intent_snapshot"] == dict(pending)
            for row in pending_intents
        )

    if kind == "defeat_resolve":
        subject = str(op.get("char") or "")
        detail = normalize_phrase(str(op.get("outcome") or ""))
        return any(
            row["subject_id"] == subject
            and any(
                effect["kind"] == "defeat" and effect["detail"] == detail
                for effect in row["effects"]
            )
            for row in fallout
        )

    if kind == "award_exp":
        frame_ref = op.get("_semantic_frame_ref")
        if not isinstance(frame_ref, str):
            return False
        events = [
            row
            for row in realization["asserted_settled"]
            if row["frame_ref"] == frame_ref
        ]
        if len(events) != 1 or events[0]["event_meaning"]["actor_id"] != op.get("char"):
            return False
        amount = op.get("amount")
        return isinstance(amount, int) and not isinstance(amount, bool) and any(
            row["cause_ref"] == events[0]["event_ref"]
            and row["subject_id"] == op.get("char")
            and any(
                effect == {"kind": "resource", "detail": "experience", "amount": amount}
                for effect in row["effects"]
            )
            for row in fallout
        )

    if kind == "combatant_defeat":
        target = str(op.get("target") or "")
        direct = any(
            row["target_id"] == target
            and any(effect["kind"] == "defeat" for effect in row["effects"])
            for row in outcomes
        )
        if direct:
            return True
        frame_ref = op.get("_semantic_frame_ref")
        if not isinstance(frame_ref, str):
            return False
        frame_events = {
            row["event_ref"]
            for bucket in (
                realization["asserted_settled"],
                realization["asserted_unresolved"],
                realization["attributed_noncurrent"],
            )
            for row in bucket
            if row["frame_ref"] == frame_ref
        }
        return len(frame_events) == 1 and any(
            row["source_event_ref"] in frame_events
            and any(effect["kind"] == "defeat" for effect in row["effects"])
            for row in outcomes
        )

    if kind == "combat_end":
        detail = normalize_phrase(f"combat ended {op.get('outcome') or 'resolved'}")
        matches = [
            row
            for row in fallout
            if row["subject_id"] == "world"
            and any(
                effect["kind"] == "world" and effect["detail"] == detail
                for effect in row["effects"]
            )
        ]
        if len(matches) != 1:
            return False
        frame_ref = op.get("_semantic_frame_ref")
        if frame_ref is None:
            return True
        events = [
            row
            for row in realization["asserted_settled"]
            if row["frame_ref"] == frame_ref
        ]
        return len(events) == 1 and matches[0]["cause_ref"] == events[0]["event_ref"]
    return False


def _validated_journal_operations(
    journal_rows: Sequence[Mapping[str, Any]],
    *,
    turn: int,
) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(journal_rows, (str, bytes, bytearray)) or not isinstance(
        journal_rows, Sequence
    ):
        raise SemanticTruthRuntimeError("current-turn journal rows must be a sequence")
    current: list[tuple[str, dict[str, Any]]] = []
    for index, value in enumerate(journal_rows):
        row = _mapping(value, f"current-turn journal[{index}]")
        if set(row) != {"id", "turn_lo", "turn_hi", "source", "ops"}:
            raise SemanticTruthRuntimeError(
                f"current-turn journal[{index}] row shape is invalid"
            )
        _integer(row["id"], f"current-turn journal[{index}].id", minimum=0)
        lo = _integer(row["turn_lo"], f"current-turn journal[{index}].turn_lo")
        hi = _integer(row["turn_hi"], f"current-turn journal[{index}].turn_hi")
        if lo > hi or not lo <= turn <= hi:
            raise SemanticTruthRuntimeError(
                f"current-turn journal[{index}] does not cover projection turn"
            )
        source = _plain(row["source"], f"current-turn journal[{index}].source")
        ops = row["ops"]
        if not isinstance(ops, list):
            raise SemanticTruthRuntimeError(f"current-turn journal[{index}].ops must be a list")
        if source in {"genesis", "bootstrap"}:
            continue
        if source not in {"rule", "user", "extraction"} and ops:
            raise SemanticTruthRuntimeError(
                f"current-turn journal source {source!r} is unsupported"
            )
        for op_index, raw_op in enumerate(ops):
            op = _mapping(raw_op, f"current-turn journal[{index}].ops[{op_index}]")
            kind = op.get("op")
            if not isinstance(kind, str) or not kind:
                raise SemanticTruthRuntimeError("current-turn journal operation has no kind")
            current.append((source, deepcopy(dict(op))))
    return current


def _validate_journal_coverage(
    journal_rows: Sequence[Mapping[str, Any]],
    *,
    state: Mapping[str, Any],
    turn: int,
    settlement_refs: set[str],
    opposition: list[dict[str, Any]],
    pending_intents: list[dict[str, Any]],
    fallout: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    realization: Mapping[str, Any],
    transition_projection: Mapping[str, Any] | None = None,
) -> None:
    operations = _validated_journal_operations(journal_rows, turn=turn)
    projected_entries: list[Mapping[str, Any]] | None = None
    required_by_construction: dict[str, list[Mapping[str, Any]]] = {}
    if transition_projection is not None:
        projected_entries = [
            row
            for row in transition_projection["entries"]
            if row["source"] not in {"genesis", "bootstrap"}
        ]
        if len(projected_entries) != len(operations):
            raise SemanticTruthRuntimeError(
                "fenced transition projection and journal operation counts disagree"
            )
        for row in transition_projection["required_facts"]:
            required_by_construction.setdefault(row["construction_ref"], []).append(row)

    for index, (_source, op) in enumerate(operations):
        kind = str(op["op"])
        if projected_entries is not None:
            entry = projected_entries[index]
            if (
                entry["source"] != _source
                or entry["op_kind"] != kind
                or entry["op_fingerprint"] != content_fingerprint(op)
            ):
                raise SemanticTruthRuntimeError(
                    "fenced transition projection does not match exact journal order"
                )
            visibility = entry["visibility"]
            if visibility == "required":
                facts = required_by_construction.get(entry["construction_ref"], [])
                if not entry["changed"] or not facts:
                    raise SemanticTruthRuntimeError(
                        "required transition has no exact projected narration fact"
                    )
                if any(row["cause_ref"] != entry["cause_ref"] for row in facts):
                    raise SemanticTruthRuntimeError(
                        "required transition fact borrows another occurrence cause"
                    )
                continue
            if visibility in {"internal", "no_effect"}:
                continue
            if visibility != "explicit_adapter":
                raise SemanticTruthRuntimeError(
                    f"journal operation {kind!r} has unsupported transition visibility"
                )
        if not _journal_op_is_covered(
            op,
            state=state,
            turn=turn,
            settlement_refs=settlement_refs,
            opposition=opposition,
            pending_intents=pending_intents,
            fallout=fallout,
            outcomes=outcomes,
            realization=realization,
        ):
            raise SemanticTruthRuntimeError(
                f"current-turn journal operation {kind!r} has no exact narration truth claim"
            )


def _transition_fallout_facts(
    projection: Mapping[str, Any],
    *,
    labels: _Labels,
    opposition: Sequence[Mapping[str, Any]],
    existing_fallout: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert exact required transition occurrences into existing gate fact rows.

    Explicit adapters are deliberately absent here: they must pass their established adapter
    checks before this function runs.  Construction and cause identities make it impossible for a
    generic transition fact to cover, duplicate, or borrow one explicit occurrence.
    """
    required_entries = {
        row["construction_ref"]: row
        for row in projection["entries"]
        if row["visibility"] == "required"
    }
    explicit_authority_refs = {
        ref
        for row in projection["entries"]
        if row["visibility"] == "explicit_adapter"
        for ref in (row["entry_ref"], row["construction_ref"], row["cause_ref"])
    }
    if len(required_entries) != sum(
        row["visibility"] == "required" for row in projection["entries"]
    ):
        raise SemanticTruthRuntimeError(
            "required transition construction identity is duplicated"
        )

    occupied_refs = {
        *(row["occurrence_ref"] for row in opposition),
        *(row["fact_ref"] for row in existing_fallout),
        *(row["outcome_ref"] for row in outcomes),
    }
    existing_claims: set[tuple[Any, ...]] = set()
    for row in opposition:
        existing_claims.update(
            (
                row["occurrence_ref"],
                row["target_id"],
                effect["kind"],
                effect["detail"],
                effect["amount"],
            )
            for effect in row["effects"]
        )
    for row in existing_fallout:
        existing_claims.update(
            (
                row["cause_ref"],
                row["subject_id"],
                effect["kind"],
                effect["detail"],
                effect["amount"],
            )
            for effect in row["effects"]
        )
    for row in outcomes:
        existing_claims.update(
            (
                row["source_event_ref"],
                row["target_id"],
                effect["kind"],
                effect["detail"],
                effect["amount"],
            )
            for effect in row["effects"]
        )

    projected: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for raw in projection["required_facts"]:
        fact = deepcopy(dict(raw))
        entry = required_entries.get(fact["construction_ref"])
        if entry is None or fact["cause_ref"] != entry["cause_ref"]:
            raise SemanticTruthRuntimeError(
                "required transition fact has no exact transition occurrence"
            )
        if fact["cause_ref"] in explicit_authority_refs:
            raise SemanticTruthRuntimeError(
                "required transition fact borrows explicit-adapter authority"
            )
        fact_ref = fact["fact_ref"]
        if fact_ref in occupied_refs or fact_ref in seen_refs:
            raise SemanticTruthRuntimeError(
                "transition fact identity duplicates an existing narration occurrence"
            )
        seen_refs.add(fact_ref)
        for effect in fact["effects"]:
            claim = (
                fact["cause_ref"],
                fact["subject_id"],
                effect["kind"],
                effect["detail"],
                effect["amount"],
            )
            if claim in existing_claims:
                raise SemanticTruthRuntimeError(
                    "transition fact duplicates an explicit-adapter claim"
                )
        subject_id = fact["subject_id"]
        projected.append(
            {
                "schema": FALLOUT_FACT_SCHEMA,
                **fact,
                "subject_label": (
                    "world" if subject_id == "world" else labels.resolve(subject_id)
                ),
            }
        )
    if len(projected) != len(projection["required_facts"]):
        raise SemanticTruthRuntimeError("required transition fact count changed during merge")
    return projected


def _has_projection_turn_marker(state: Mapping[str, Any], turn: int) -> bool:
    for journal_key in (
        "semantic_frames",
        "semantic_meanings",
        "semantic_bindings",
        "semantic_world_alignments",
        "mechanic_settlements",
    ):
        rows = state.get(journal_key)
        if isinstance(rows, list) and any(
            isinstance(row, Mapping) and row.get("turn") == turn for row in rows
        ):
            return True
    players = state.get("player")
    if isinstance(players, Mapping):
        for card in players.values():
            if not isinstance(card, Mapping):
                continue
            for marker in ("_hp_adj_last", "_opposition_last", "defeated"):
                row = card.get(marker)
                if isinstance(row, Mapping) and row.get("turn") == turn:
                    return True
    combat = state.get("combat")
    if isinstance(combat, Mapping):
        rows = combat.get("combatants")
        if isinstance(rows, Mapping) and any(
            isinstance(row, Mapping)
            and (row.get("_struck_turn") == turn or row.get("defeated_turn") == turn)
            for row in rows.values()
        ):
            return True
        history = combat.get("history")
        if isinstance(history, list) and any(
            isinstance(row, Mapping) and row.get("turn") == turn for row in history
        ):
            return True
    return False


def _known_entities(
    realization: Mapping[str, Any],
    opposition: list[dict[str, Any]],
    pending_intents: list[dict[str, Any]],
    fallout: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    labels: _Labels,
) -> list[dict[str, str]]:
    identities: set[str] = set()
    for bucket in (
        realization["asserted_settled"],
        realization["asserted_unresolved"],
        realization["attributed_noncurrent"],
    ):
        for row in bucket:
            meaning = row["event_meaning"]
            identities.add(meaning["actor_id"])
            if meaning["target_entity_id"] is not None:
                identities.add(meaning["target_entity_id"])
    for row in opposition:
        identities.update((row["actor_id"], row["target_id"]))
    display_labels: dict[str, str] = {}
    for row in pending_intents:
        identities.update((row["actor_id"], row["target_id"]))
        for identity, label in (
            (row["actor_id"], row["actor_label"]),
            (row["target_id"], row["target_label"]),
        ):
            if identity in display_labels and display_labels[identity] != label:
                raise SemanticTruthRuntimeError(
                    "pending intent assigns conflicting exact entity labels"
                )
            display_labels[identity] = label
    identities.update(row["subject_id"] for row in fallout if row["subject_id"] != "world")
    identities.update(row["target_id"] for row in outcomes if row["target_id"] != "world")
    return [
        {
            "entity_id": ref,
            "label": display_labels.get(ref, labels.resolve(ref)),
            "scope": "current",
        }
        for ref in sorted(identities)
    ]


def _build_runtime_truth_contract(
    state: object,
    *,
    branch_id: str,
    post_ledger_hash: str,
    turn_index: int | None = None,
    journal_rows: Sequence[Mapping[str, Any]] = (),
    transition_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Internal common builder for legacy and fenced production truth contracts.

    The returned value is the existing validated ``narration-truth-contract/1`` shape and can be
    passed directly to the deterministic fallback runtime.  ``branch_id`` and
    ``post_ledger_hash`` and optional ``turn_index`` are caller-owned lifecycle identities; the
    realization fingerprint is used as the immutable artifact input binding until the terminal
    fallback artifact is built.  A state turn behind ``turn_index`` is accepted only for a truly
    empty current ledger (for example, pure roleplay); state ahead of lifecycle is always rejected.
    Exact ``Store.diagnostic_turn(...)["journal"]`` rows may be supplied for systemic mutation
    coverage.  An effectful current operation without an exact claim fails construction.
    """
    try:
        snapshot = _mapping(state, "runtime state")
        meta = _mapping(snapshot.get("meta"), "runtime state meta")
        state_turn = _integer(meta.get("turn"), "runtime state turn", minimum=-1)
        if turn_index is None:
            if state_turn < 0:
                raise SemanticTruthRuntimeError(
                    "empty state requires an explicit lifecycle turn_index"
                )
            turn = state_turn
        else:
            turn = _integer(turn_index, "lifecycle turn_index", minimum=0)
            if state_turn > turn:
                raise SemanticTruthRuntimeError("runtime state is ahead of lifecycle turn")
            if state_turn < turn:
                has_journal_ops = any(
                    isinstance(row, Mapping)
                    and isinstance(row.get("ops"), list)
                    and bool(row["ops"])
                    for row in journal_rows
                )
                if _has_projection_turn_marker(snapshot, turn) or has_journal_ops:
                    raise SemanticTruthRuntimeError(
                        "lifecycle turn override is allowed only for an empty current ledger"
                    )
        projection = None
        if transition_projection is not None:
            projection = validate_transition_projection(transition_projection)
            if (
                projection["delivery_phase"] != "pre_display"
                or projection["branch_id"] != branch_id
                or projection["turn_index"] != turn
                or projection["post_ledger_hash"] != post_ledger_hash
            ):
                raise SemanticTruthRuntimeError(
                    "fenced transition projection lifecycle binding is inconsistent"
                )
        frames = _journal_payloads(snapshot, "semantic_frames", "frame", turn)
        settlement_rows = _journal_payloads(
            snapshot, "mechanic_settlements", "receipt", turn
        )
        realization = _realization(
            snapshot,
            turn=turn,
            frames=frames,
            settlement_rows=settlement_rows,
        )
        _validate_admitted_semantic_chains(
            snapshot,
            turn=turn,
            frames=frames,
            settlement_rows=settlement_rows,
            realization=realization,
        )
        labels = _Labels(snapshot, frames)
        opposition = _opposition_facts(snapshot, turn=turn, labels=labels)
        (
            outcomes,
            settlement_fallout,
            hp_targets,
            defeated_targets,
            settlement_refs,
        ) = _settlement_outcomes(settlement_rows, realization, labels=labels)
        combat_opening = any(
            isinstance(row, Mapping)
            and row.get("contract_id") == COMBAT_OPENING_CONTRACT
            for row in settlement_rows
        )
        pending_intents = _pending_intent_facts(
            snapshot,
            turn=turn,
            labels=labels,
            combat_opening=combat_opening,
        )
        player_fallout, _defeated_players = _player_fallout(
            snapshot, opposition, turn=turn, labels=labels
        )
        experience_fallout = _award_exp_fallout(
            journal_rows,
            snapshot,
            realization,
            outcomes,
            turn=turn,
            labels=labels,
        )
        fallout = [
            *settlement_fallout,
            *player_fallout,
            *experience_fallout,
            *_combat_end_fallout(
                snapshot,
                turn=turn,
                player_fallout=player_fallout,
                outcomes=outcomes,
            ),
        ]
        _check_combatant_coverage(
            snapshot,
            turn=turn,
            hp_targets=hp_targets,
            defeated_targets=defeated_targets,
        )
        _validate_journal_coverage(
            journal_rows,
            state=snapshot,
            turn=turn,
            settlement_refs=settlement_refs,
            opposition=opposition,
            pending_intents=pending_intents,
            fallout=fallout,
            outcomes=outcomes,
            realization=realization,
            transition_projection=projection,
        )
        if projection is not None:
            fallout.extend(
                _transition_fallout_facts(
                    projection,
                    labels=labels,
                    opposition=opposition,
                    existing_fallout=fallout,
                    outcomes=outcomes,
                )
            )
        known = _known_entities(
            realization, opposition, pending_intents, fallout, outcomes, labels
        )
        return build_narration_truth_contract(
            realization,
            opposition_facts=opposition,
            pending_intents=pending_intents,
            fallout_facts=fallout,
            settled_target_outcomes=outcomes,
            known_entities=known,
            lifecycle_binding={
                "branch_ref": branch_id,
                "ledger_fingerprint": post_ledger_hash,
                "artifact_fingerprint": realization["fingerprint"],
            },
        )
    except SemanticTruthRuntimeError:
        raise
    except (
        KeyError,
        MechanicSettlementError,
        NarrationTruthGateError,
        NarratorRealizationError,
        SemanticTransitionTruthError,
        TypeError,
        ValueError,
    ) as exc:
        raise SemanticTruthRuntimeError(str(exc)) from exc


def build_runtime_truth_contract(
    state: object,
    *,
    branch_id: str,
    post_ledger_hash: str,
    turn_index: int | None = None,
    journal_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the legacy post-state truth contract used by pure unit fixtures.

    This compatibility path intentionally keeps its established fail-closed operation coverage.
    Production lifecycle code must use :func:`build_fenced_runtime_truth_contract`, which binds
    exact pre-state, journal window, strict replay, and post-state into one persistence result.
    """
    return _build_runtime_truth_contract(
        state,
        branch_id=branch_id,
        post_ledger_hash=post_ledger_hash,
        turn_index=turn_index,
        journal_rows=journal_rows,
    )


def build_fenced_runtime_truth_contract(
    *,
    pre_state: object,
    post_state: object,
    pre_ledger_hash: str,
    post_ledger_hash: str,
    journal_rows: Sequence[Mapping[str, Any]],
    journal_window_fingerprint: str,
    branch_id: str,
    turn_index: int,
) -> FencedRuntimeTruthResult:
    """Build one production-only, proof-carrying truth and transition persistence input."""
    try:
        _validate_current_settlement_window(
            pre_state=pre_state,
            post_state=post_state,
            journal_rows=journal_rows,
            turn_index=turn_index,
        )
        projection = validate_transition_projection(
            project_journal_transitions(
                pre_state=pre_state,
                post_state=post_state,
                journal_rows=journal_rows,
                branch_id=branch_id,
                turn_index=turn_index,
                pre_ledger_hash=pre_ledger_hash,
                post_ledger_hash=post_ledger_hash,
                journal_window_fingerprint=journal_window_fingerprint,
                delivery_phase="pre_display",
            )
        )
        contract = _build_runtime_truth_contract(
            post_state,
            branch_id=branch_id,
            post_ledger_hash=post_ledger_hash,
            turn_index=turn_index,
            journal_rows=journal_rows,
            transition_projection=projection,
        )
        return FencedRuntimeTruthResult._from_validated(contract, projection)
    except SemanticTruthRuntimeError:
        raise
    except (SemanticTransitionTruthError, TypeError, ValueError) as exc:
        raise SemanticTruthRuntimeError(str(exc)) from exc
