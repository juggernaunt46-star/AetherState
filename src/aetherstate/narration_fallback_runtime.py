"""Proof-complete deterministic narration fallback construction.

The proxy and reducer integrations deliberately live elsewhere.  This module is the pure bridge
between a frozen :mod:`narration_truth_gate` contract and a
:class:`turn_lifecycle.EnvelopeArtifact`: it renders one code-owned plain-text fallback, re-parses
the exact visible UTF-8 text through a separate template grammar, proves the resulting graph equal
to both the realization plan and ledger projection, encodes final OpenAI-compatible wire bytes,
and only then returns an envelope that is eligible for an atomic lifecycle commit.

The visible observer receives opaque occurrence/cause bindings and a public surface lexicon.  It
does not receive or inspect the expected claim graph.  Consequently a defective renderer cannot
certify itself by copying its own interpretation into the expected side of the comparison.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import re
import unicodedata
from typing import Any

from .capability_glossary import normalize_phrase
from .narration_truth_gate import (
    VISIBLE_CONTENT_TYPE,
    VISIBLE_RENDERER_CONFIG_FINGERPRINT,
    VISIBLE_RENDERER_VERSION,
    NarrationTruthGateError,
    validate_narration_truth_contract,
)
from .response_wire import ChatWireError, decode_chat_story, encode_chat_story
from .turn_lifecycle import (
    EnvelopeArtifact,
    TurnArtifactError,
    TurnReservation,
    build_delivery_proof,
    build_envelope,
    canonical_claim_projection,
    fingerprint,
    logical_message_id,
    raw_fingerprint,
    validate_envelope,
    validate_pre_mutation_key,
)


FALLBACK_PLAN_SCHEMA = "narration-realization-plan/1"
FALLBACK_GRAPH_SCHEMA = "narration-fallback-proof-graph/1"
OBSERVATION_CONTEXT_SCHEMA = "narration-fallback-observation-context/1"
FALLBACK_RUNTIME_DIAGNOSTICS_SCHEMA = "narration-fallback-runtime/1"
PLAIN_VISIBLE_SCHEMA = "aetherstate-plain-visible/1"
EMPTY_FALLBACK_TEXT = (
    "The scene holds to what is already established, without adding an unverified outcome."
)

FALLBACK_PHASES = (
    "after_contract_validation",
    "after_construction",
    "after_canonical_rendering",
    "after_expected_graph",
    "after_observed_graph",
    "after_graph_equality",
    "after_ledger_equality",
    "after_wire_encoding",
    "after_gate_receipt",
    "after_envelope",
)

_GRAPH_CLAIM_FIELDS = {
    "slot_index",
    "span_start",
    "span_end",
    "evidence_fingerprint",
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
}
_SEMANTIC_FIELDS = {
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
}
_BINDING_FIELDS = {
    "slot_index",
    "occurrence_ref",
    "cause_ref",
    "construction_ref",
}
_FP_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.IGNORECASE)
_MARKUP_RE = re.compile(
    r"<!--|-->|<\s*/?\s*[a-z!][^>]*>|~~|```|~~~|"
    r"!\[[^\]]*\]\([^)]*\)|\[[^\]]+\]\([^)]*\)|"
    r"::(?:before|after)|\bcontent\s*:|\b(?:style|hidden)\s*=",
    re.IGNORECASE,
)
_MARKDOWN_TRANSFORM_RE = re.compile(
    # The production chat surface normally runs story text through Markdown.  The gated
    # fallback deliberately promises identity/textContent semantics, so reject every bounded
    # Markdown form that can remove delimiters, hide a definition, or restructure a line.
    r"[*_\[\]|]|"
    r"^[ ]{0,3}(?:#{1,6}(?:[ \t]|$)|>(?:[ \t]|$)|[-+](?:[ \t])|"
    r"[0-9]{1,9}[.)](?:[ \t])|(?:={3,}|-{3,})[ \t]*$)|"
    r"^[ ]{4,}|[ \t]+$",
    re.MULTILINE,
)
_MULTIPLICITY_RE = re.compile(r"Among ([1-9][0-9]*) settled targets, (.+)\Z", re.DOTALL)
_INTEGER_RE = re.compile(r"-?[0-9]+\Z")
_DETAIL_RE = re.compile(r"[a-z0-9][a-z0-9 /:_-]{0,159}\Z")


class NarrationFallbackRuntimeError(ValueError):
    """Fallback construction could not produce one proof-complete terminal artifact."""


@dataclass(frozen=True)
class FallbackRealizationPlan:
    """Detached code-authored fallback plan plus its blind-observer input."""

    text: str
    expected_graph: dict[str, Any]
    ledger_graph: dict[str, Any]
    observation_context: dict[str, Any]
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
        raise NarrationFallbackRuntimeError("fallback data must be finite JSON") from exc


def _json_copy(value: object) -> Any:
    return json.loads(_json_bytes(value).decode("utf-8"))


def _is_noncharacter(character: str) -> bool:
    codepoint = ord(character)
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}


def validate_canonical_visible_text(value: object) -> str:
    """Validate the exact identity-rendered text surface; never repair or normalize it."""
    if not isinstance(value, str) or not value.strip():
        raise NarrationFallbackRuntimeError("canonical visible text must be non-empty text")
    if len(value.encode("utf-8")) > 64_000:
        raise NarrationFallbackRuntimeError("canonical visible text exceeds the bounded surface")
    if "\r" in value or unicodedata.normalize("NFC", value) != value:
        raise NarrationFallbackRuntimeError("canonical visible text has normalization drift")
    if (
        _ENTITY_RE.search(value)
        or _MARKUP_RE.search(value)
        or _MARKDOWN_TRANSFORM_RE.search(value)
        or "\\" in value
        or "`" in value
    ):
        raise NarrationFallbackRuntimeError("canonical visible text contains a renderer transform")
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cf", "Cs"} or (category == "Cc" and character != "\n"):
            raise NarrationFallbackRuntimeError("canonical visible text contains control glyphs")
        if _is_noncharacter(character):
            raise NarrationFallbackRuntimeError("canonical visible text contains a noncharacter")
    return value


def _surface_profile() -> dict[str, Any]:
    return {
        "schema": PLAIN_VISIBLE_SCHEMA,
        "content_type": VISIBLE_CONTENT_TYPE,
        "renderer_version": VISIBLE_RENDERER_VERSION,
        "renderer_config_fingerprint": VISIBLE_RENDERER_CONFIG_FINGERPRINT,
        "normalization": "NFC",
        "line_endings": "LF",
        "text_encoding": "UTF-8",
        "transform": "identity",
    }


def _require_fp(value: object, label: str) -> str:
    if not isinstance(value, str) or _FP_RE.fullmatch(value) is None:
        raise NarrationFallbackRuntimeError(f"{label} must be a sha256 fingerprint")
    return value


def _validate_reservation(
    pre_mutation_key: Mapping[str, Any], reservation: TurnReservation
) -> tuple[dict[str, Any], str]:
    key = validate_pre_mutation_key(pre_mutation_key)
    if not isinstance(reservation, TurnReservation):
        raise NarrationFallbackRuntimeError("fallback requires a typed lifecycle reservation")
    if reservation.lifecycle_key != key["lifecycle_key"]:
        raise NarrationFallbackRuntimeError("reservation belongs to a different lifecycle key")
    if reservation.status != "reserved":
        raise NarrationFallbackRuntimeError("fallback requires an active reserved attempt")
    if isinstance(reservation.attempt_index, bool) \
            or not isinstance(reservation.attempt_index, int) \
            or reservation.attempt_index < 0:
        raise NarrationFallbackRuntimeError("reservation attempt index is invalid")
    return key, _require_fp(reservation.request_hash, "reservation request_hash")


def _entity_rows(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = [
        {"entity_id": row["entity_id"], "label": row["label"]}
        for row in contract["known_entities"]
    ]
    for row in rows:
        validate_canonical_visible_text(row["label"])
    return rows


def _label_by_id(contract: Mapping[str, Any]) -> dict[str, str]:
    return {row["entity_id"]: row["label"] for row in _entity_rows(contract)}


def _safe_detail(value: object, label: str) -> str:
    if not isinstance(value, str) or _DETAIL_RE.fullmatch(value) is None:
        raise NarrationFallbackRuntimeError(f"{label} is not safe deterministic template text")
    return value


def _opposition_by_occurrence(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {row["occurrence_ref"]: row for row in contract["opposition_actions"]}


def _pending_by_occurrence(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {row["pending_ref"]: row for row in contract["pending_intents"]}


def _multiplicity_prefix(claim: Mapping[str, Any]) -> str:
    multiplicity = claim["multiplicity"]
    if not isinstance(multiplicity, int) or isinstance(multiplicity, bool) or multiplicity < 1:
        raise NarrationFallbackRuntimeError("fallback multiplicity must be a positive integer")
    return f"Among {multiplicity} settled targets, " if multiplicity > 1 else ""


def _render_claim(
    claim: Mapping[str, Any],
    labels: Mapping[str, str],
    opposition: Mapping[str, Mapping[str, Any]],
    pending: Mapping[str, Mapping[str, Any]],
) -> str:
    subjects = claim["subject_ids"]
    if not isinstance(subjects, list) or len(subjects) != 1 or subjects[0] not in labels:
        raise NarrationFallbackRuntimeError("fallback clauses require one exact known subject")
    subject = labels[subjects[0]]
    actor_id = claim["actor_id"]
    actor = labels.get(actor_id) if actor_id is not None else None
    if actor_id is not None and actor is None:
        raise NarrationFallbackRuntimeError("fallback actor is not a known exact entity")
    kind = claim["kind"]
    detail = _safe_detail(claim["detail"], "fallback claim detail")
    amount = claim["amount"]
    prefix = _multiplicity_prefix(claim)

    if kind == "pending_intent":
        fact = pending.get(claim["occurrence_ref"])
        if fact is None or actor_id != fact["actor_id"] \
                or subjects != [fact["target_id"]] \
                or claim["cause_ref"] != fact["intent_ref"] \
                or claim["construction_ref"] != fact["construction_ref"] \
                or claim["time_scope"] != "future" \
                or detail != normalize_phrase(f"{fact['move_id']} {fact['tell']}") \
                or amount is not None:
            raise NarrationFallbackRuntimeError(
                "pending intent fallback lost its exact future action binding"
            )
        core = validate_canonical_visible_text(fact["visible_text"])
    elif kind == "opposition_action":
        fact = opposition.get(claim["occurrence_ref"])
        if fact is None or actor_id != fact["actor_id"] or subjects != [fact["target_id"]]:
            raise NarrationFallbackRuntimeError("opposition fallback lost its exact action binding")
        expected_detail = normalize_phrase(f"{fact['move_id']} {fact['outcome']}")
        if detail != expected_detail:
            raise NarrationFallbackRuntimeError("opposition fallback detail differs from exact fact")
        if fact["outcome"] == "miss":
            core = f"{actor}'s {fact['move_label']} misses {subject}."
        elif fact["outcome"] == "blocked":
            core = f"{actor}'s {fact['move_label']} is blocked before it affects {subject}."
        else:
            core = f"{actor}'s {fact['move_label']} hits {subject}."
    elif kind == "harm":
        source = f"{actor}'s settled action" if actor is not None else "The settled action"
        if amount is None and detail == "harm":
            core = f"{source} harms {subject}."
        elif amount is None:
            core = f"{source} causes {detail} to {subject}."
        elif isinstance(amount, int) and not isinstance(amount, bool) and amount < 0:
            core = f"{source} deals {-amount} HP of {detail} to {subject}."
        else:
            raise NarrationFallbackRuntimeError("fallback harm requires a negative delta or null")
    elif kind == "defeat":
        if detail == "defeated":
            core = (
                f"{actor}'s settled action defeats {subject}."
                if actor is not None
                else f"{subject} is defeated."
            )
        elif actor is not None:
            core = f"{actor}'s settled action leaves {subject} in defeat state {detail}."
        else:
            core = f"{subject} reaches defeat state {detail}."
    elif kind == "status":
        core = (
            f"{actor}'s settled action leaves {subject} with status {detail}."
            if actor is not None
            else f"{subject} has settled status {detail}."
        )
    elif kind == "resource":
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise NarrationFallbackRuntimeError("fallback resource delta is invalid")
        if actor is not None and amount is None:
            core = (
                f"{actor}'s settled action changes {subject}'s {detail} "
                "without a numeric delta."
            )
        elif actor is not None:
            core = f"{actor}'s settled action changes {subject}'s {detail} by {amount}."
        elif amount is None:
            core = f"{subject}'s {detail} changes without a numeric delta."
        elif isinstance(amount, int) and not isinstance(amount, bool):
            core = f"{subject}'s {detail} changes by {amount}."
        else:
            raise NarrationFallbackRuntimeError("fallback resource delta is invalid")
    elif kind == "time":
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise NarrationFallbackRuntimeError("fallback time delta is invalid")
        world_subject = subjects[0] == "world"
        subject_phrase = "" if world_subject else f" for {subject}"
        if actor is not None and amount is None:
            core = (
                f"{actor}'s settled action changes time {detail}{subject_phrase} "
                "without a numeric delta."
            )
        elif actor is not None:
            core = (
                f"{actor}'s settled action changes time {detail}{subject_phrase} by {amount}."
            )
        elif amount is None and world_subject:
            core = f"Settled time {detail} changes without a numeric delta."
        elif amount is None:
            core = f"For {subject}, settled time {detail} changes without a numeric delta."
        elif world_subject:
            core = f"Settled time {detail} changes by {amount}."
        elif isinstance(amount, int) and not isinstance(amount, bool):
            core = f"For {subject}, settled time {detail} changes by {amount}."
        else:
            raise NarrationFallbackRuntimeError("fallback time delta is invalid")
    elif kind == "movement":
        core = (
            f"{actor}'s settled action moves {subject} to {detail}."
            if actor is not None
            else f"{subject} moves to {detail}."
        )
    elif kind == "world":
        world_subject = subjects[0] == "world"
        if actor is not None and world_subject:
            core = f"{actor}'s settled action changes the world: {detail}."
        elif actor is not None:
            core = f"{actor}'s settled action changes the world for {subject}: {detail}."
        elif world_subject:
            core = f"The settled world change is {detail}."
        else:
            core = f"The settled world change for {subject} is {detail}."
    else:
        raise NarrationFallbackRuntimeError(f"unsupported fallback claim kind: {kind}")
    return prefix + core


def _graph_claim(
    *, slot_index: int, sentence: str, start: int, semantic: Mapping[str, Any]
) -> dict[str, Any]:
    encoded = sentence.encode("utf-8")
    row = {
        "slot_index": slot_index,
        "span_start": start,
        "span_end": start + len(encoded),
        "evidence_fingerprint": raw_fingerprint(encoded),
        **{field: deepcopy(semantic[field]) for field in _SEMANTIC_FIELDS},
    }
    if set(row) != _GRAPH_CLAIM_FIELDS:
        raise NarrationFallbackRuntimeError("fallback graph claim shape is invalid")
    return row


def _build_graph(contract_fingerprint: str, claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [_json_copy(row) for row in claims]
    payload = {
        "schema": FALLBACK_GRAPH_SCHEMA,
        "contract_fingerprint": _require_fp(contract_fingerprint, "contract fingerprint"),
        "claims": rows,
    }
    return {**payload, "fingerprint": fingerprint(payload)}


def _validate_graph(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "contract_fingerprint", "claims", "fingerprint"
    }:
        raise NarrationFallbackRuntimeError("fallback proof graph shape is invalid")
    if value["schema"] != FALLBACK_GRAPH_SCHEMA or not isinstance(value["claims"], list):
        raise NarrationFallbackRuntimeError("fallback proof graph schema is invalid")
    payload = {
        "schema": value["schema"],
        "contract_fingerprint": _require_fp(
            value["contract_fingerprint"], "graph contract fingerprint"
        ),
        "claims": _json_copy(value["claims"]),
    }
    for index, row in enumerate(payload["claims"]):
        if not isinstance(row, dict) or set(row) != _GRAPH_CLAIM_FIELDS:
            raise NarrationFallbackRuntimeError(f"fallback graph claim {index} shape is invalid")
        if row["slot_index"] != index:
            raise NarrationFallbackRuntimeError("fallback graph slots are not canonical")
        _require_fp(row["evidence_fingerprint"], "claim evidence fingerprint")
    supplied = _require_fp(value["fingerprint"], "graph fingerprint")
    if supplied != fingerprint(payload):
        raise NarrationFallbackRuntimeError("fallback proof graph fingerprint mismatch")
    return {**payload, "fingerprint": supplied}


def _context_payload(contract: Mapping[str, Any]) -> dict[str, Any]:
    bindings = [
        {
            "slot_index": index,
            "occurrence_ref": claim["occurrence_ref"],
            "cause_ref": claim["cause_ref"],
            "construction_ref": claim["construction_ref"],
        }
        for index, claim in enumerate(contract["expected_claims"])
    ]
    opposition = [
        {
            "actor_id": row["actor_id"],
            "target_id": row["target_id"],
            "move_id": row["move_id"],
            "move_label": row["move_label"],
        }
        for row in contract["opposition_actions"]
    ]
    pending = [
        {
            "actor_id": row["actor_id"],
            "target_id": row["target_id"],
            "move_id": row["move_id"],
            "tell": row["tell"],
            "opening_kind": row["opening_kind"],
            "visible_text": row["visible_text"],
        }
        for row in contract["pending_intents"]
    ]
    return {
        "schema": OBSERVATION_CONTEXT_SCHEMA,
        "contract_fingerprint": contract["fingerprint"],
        "bindings": bindings,
        "entities": _entity_rows(contract),
        "opposition_lexicon": opposition,
        "pending_intent_lexicon": pending,
    }


def _seal_context(contract: Mapping[str, Any]) -> dict[str, Any]:
    payload = _context_payload(contract)
    return {**payload, "fingerprint": fingerprint(payload)}


def _validate_context(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "contract_fingerprint",
        "bindings",
        "entities",
        "opposition_lexicon",
        "pending_intent_lexicon",
        "fingerprint",
    }:
        raise NarrationFallbackRuntimeError("fallback observation context shape is invalid")
    payload = {key: _json_copy(value[key]) for key in value if key != "fingerprint"}
    if payload["schema"] != OBSERVATION_CONTEXT_SCHEMA:
        raise NarrationFallbackRuntimeError("fallback observation context schema is invalid")
    _require_fp(payload["contract_fingerprint"], "observation contract fingerprint")
    if not isinstance(payload["bindings"], list):
        raise NarrationFallbackRuntimeError("fallback observation bindings must be a list")
    for index, row in enumerate(payload["bindings"]):
        if not isinstance(row, dict) or set(row) != _BINDING_FIELDS or row["slot_index"] != index:
            raise NarrationFallbackRuntimeError("fallback observation bindings are not canonical")
        _require_fp(row["construction_ref"], "binding construction_ref")
    if not isinstance(payload["entities"], list) or not isinstance(
        payload["opposition_lexicon"], list
    ) or not isinstance(payload["pending_intent_lexicon"], list):
        raise NarrationFallbackRuntimeError("fallback surface lexicon is invalid")
    for row in payload["entities"]:
        if not isinstance(row, dict) or set(row) != {"entity_id", "label"}:
            raise NarrationFallbackRuntimeError("fallback entity lexicon is invalid")
        validate_canonical_visible_text(row["label"])
    pending_fields = {
        "actor_id",
        "target_id",
        "move_id",
        "tell",
        "opening_kind",
        "visible_text",
    }
    for row in payload["pending_intent_lexicon"]:
        if not isinstance(row, dict) or set(row) != pending_fields:
            raise NarrationFallbackRuntimeError("fallback pending intent lexicon is invalid")
        if row["opening_kind"] not in {"combat_opening", "following_intent"}:
            raise NarrationFallbackRuntimeError("fallback pending intent kind is invalid")
        if any(
            not isinstance(row[field], str) or not row[field]
            for field in ("actor_id", "target_id", "move_id", "tell")
        ):
            raise NarrationFallbackRuntimeError("fallback pending intent binding is invalid")
        validate_canonical_visible_text(row["visible_text"])
    supplied = _require_fp(value["fingerprint"], "observation context fingerprint")
    if supplied != fingerprint(payload):
        raise NarrationFallbackRuntimeError("fallback observation context fingerprint mismatch")
    return {**payload, "fingerprint": supplied}


def build_fallback_realization_plan(contract: object) -> FallbackRealizationPlan:
    """Build the default code-owned plan from one validated ledger truth contract."""
    try:
        valid = validate_narration_truth_contract(contract)
        labels = _label_by_id(valid)
        opposition = _opposition_by_occurrence(valid)
        pending = _pending_by_occurrence(valid)
        sentences = [
            _render_claim(claim, labels, opposition, pending)
            for claim in valid["expected_claims"]
        ]
        text = " ".join(sentences) if sentences else EMPTY_FALLBACK_TEXT
        validate_canonical_visible_text(text)

        expected_rows: list[dict[str, Any]] = []
        ledger_rows: list[dict[str, Any]] = []
        byte_cursor = 0
        for index, (sentence, claim) in enumerate(zip(sentences, valid["expected_claims"])):
            semantic = {field: deepcopy(claim[field]) for field in _SEMANTIC_FIELDS}
            expected_rows.append(
                _graph_claim(
                    slot_index=index,
                    sentence=sentence,
                    start=byte_cursor,
                    semantic=semantic,
                )
            )
            # Re-project independently from the sealed ledger contract rather than copying the
            # expected graph row.  The comparison below detects future plan-projection drift.
            ledger_semantic = {
                "occurrence_ref": claim["occurrence_ref"],
                "cause_ref": claim["cause_ref"],
                "construction_ref": claim["construction_ref"],
                "actor_id": claim["actor_id"],
                "subject_ids": list(claim["subject_ids"]),
                "kind": claim["kind"],
                "polarity": claim["polarity"],
                "actuality": claim["actuality"],
                "time_scope": claim["time_scope"],
                "multiplicity": claim["multiplicity"],
                "detail": claim["detail"],
                "amount": claim["amount"],
            }
            ledger_rows.append(
                _graph_claim(
                    slot_index=index,
                    sentence=sentence,
                    start=byte_cursor,
                    semantic=ledger_semantic,
                )
            )
            byte_cursor += len(sentence.encode("utf-8")) + (
                1 if index + 1 < len(sentences) else 0
            )

        expected_graph = _build_graph(valid["fingerprint"], expected_rows)
        ledger_graph = _build_graph(valid["fingerprint"], ledger_rows)
        context = _seal_context(valid)
        payload = {
            "schema": FALLBACK_PLAN_SCHEMA,
            "contract_fingerprint": valid["fingerprint"],
            "surface_profile": _surface_profile(),
            "text_fingerprint": raw_fingerprint(text.encode("utf-8")),
            "expected_graph": expected_graph,
            "ledger_graph": ledger_graph,
            "observation_context": context,
        }
        return FallbackRealizationPlan(
            text=text,
            expected_graph=expected_graph,
            ledger_graph=ledger_graph,
            observation_context=context,
            fingerprint=fingerprint(payload),
        )
    except NarrationFallbackRuntimeError:
        raise
    except (NarrationTruthGateError, TypeError, ValueError) as exc:
        raise NarrationFallbackRuntimeError("fallback realization plan is invalid") from exc


def _plan_payload(plan: FallbackRealizationPlan) -> dict[str, Any]:
    return {
        "schema": FALLBACK_PLAN_SCHEMA,
        "contract_fingerprint": plan.expected_graph.get("contract_fingerprint"),
        "surface_profile": _surface_profile(),
        "text_fingerprint": raw_fingerprint(plan.text.encode("utf-8")),
        "expected_graph": _validate_graph(plan.expected_graph),
        "ledger_graph": _validate_graph(plan.ledger_graph),
        "observation_context": _validate_context(plan.observation_context),
    }


def render_fallback_text(plan: FallbackRealizationPlan) -> str:
    """Return exact plan text only when every detached plan field still fingerprints exactly."""
    if not isinstance(plan, FallbackRealizationPlan):
        raise NarrationFallbackRuntimeError("expected a typed fallback realization plan")
    validate_canonical_visible_text(plan.text)
    payload = _plan_payload(plan)
    if plan.fingerprint != fingerprint(payload):
        raise NarrationFallbackRuntimeError("fallback realization plan fingerprint mismatch")
    contract_fp = payload["contract_fingerprint"]
    if any(
        graph["contract_fingerprint"] != contract_fp
        for graph in (payload["expected_graph"], payload["ledger_graph"])
    ) or payload["observation_context"]["contract_fingerprint"] != contract_fp:
        raise NarrationFallbackRuntimeError("fallback plan components bind different contracts")
    return plan.text


def _strip_multiplicity(sentence: str) -> tuple[int, str]:
    match = _MULTIPLICITY_RE.fullmatch(sentence)
    if match is None:
        return 1, sentence
    count = int(match.group(1))
    if count <= 1:
        raise NarrationFallbackRuntimeError("fallback multiplicity prefix is not canonical")
    return count, match.group(2)


def _semantic(
    *,
    actor_id: str | None,
    subject_id: str,
    kind: str,
    multiplicity: int,
    detail: str,
    amount: int | None,
    time_scope: str = "current",
) -> dict[str, Any]:
    return {
        "actor_id": actor_id,
        "subject_ids": [subject_id],
        "kind": kind,
        "polarity": "positive",
        "actuality": "actual",
        "time_scope": time_scope,
        "multiplicity": multiplicity,
        "detail": detail,
        "amount": amount,
    }


def _parse_sentence(sentence: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    multiplicity, core = _strip_multiplicity(sentence)
    entities = context["entities"]
    results: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        result = _semantic(multiplicity=multiplicity, **kwargs)
        key = _json_bytes(result)
        if all(_json_bytes(existing) != key for existing in results):
            results.append(result)

    # Autonomous actions are parsed from their exact actor/move/target surface, but all three
    # outcome words are admitted here.  The later ledger comparison, not this observer, decides
    # which one is true for this occurrence.
    labels = {row["entity_id"]: row["label"] for row in entities}
    for pending in context["pending_intent_lexicon"]:
        if core == pending["visible_text"]:
            add(
                actor_id=pending["actor_id"],
                subject_id=pending["target_id"],
                kind="pending_intent",
                detail=normalize_phrase(f"{pending['move_id']} {pending['tell']}"),
                amount=None,
                time_scope="future",
            )
    for action in context["opposition_lexicon"]:
        actor = labels.get(action["actor_id"])
        subject = labels.get(action["target_id"])
        if actor is None or subject is None:
            continue
        variants = {
            f"{actor}'s {action['move_label']} misses {subject}.": "miss",
            f"{actor}'s {action['move_label']} is blocked before it affects {subject}.": "blocked",
            f"{actor}'s {action['move_label']} hits {subject}.": "hit",
        }
        outcome = variants.get(core)
        if outcome is not None:
            add(
                actor_id=action["actor_id"],
                subject_id=action["target_id"],
                kind="opposition_action",
                detail=normalize_phrase(f"{action['move_id']} {outcome}"),
                amount=None,
            )

    for subject_row in entities:
        subject_id = subject_row["entity_id"]
        subject = subject_row["label"]
        if core == f"{subject} is defeated.":
            add(
                actor_id=None,
                subject_id=subject_id,
                kind="defeat",
                detail="defeated",
                amount=None,
            )
        defeat_prefix = f"{subject} reaches defeat state "
        if core.startswith(defeat_prefix) and core.endswith("."):
            detail = core[len(defeat_prefix):-1]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="defeat",
                    detail=detail,
                    amount=None,
                )
        status_prefix = f"{subject} has settled status "
        if core.startswith(status_prefix) and core.endswith("."):
            detail = core[len(status_prefix):-1]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="status",
                    detail=detail,
                    amount=None,
                )
        movement_prefix = f"{subject} moves to "
        if core.startswith(movement_prefix) and core.endswith("."):
            detail = core[len(movement_prefix):-1]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="movement",
                    detail=detail,
                    amount=None,
                )
        resource_prefix = f"{subject}'s "
        no_delta_suffix = " changes without a numeric delta."
        if core.startswith(resource_prefix) and core.endswith(no_delta_suffix):
            detail = core[len(resource_prefix):-len(no_delta_suffix)]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="resource",
                    detail=detail,
                    amount=None,
                )
        if core.startswith(resource_prefix) and core.endswith(".") and " changes by " in core:
            body = core[len(resource_prefix):-1]
            detail, amount_text = body.rsplit(" changes by ", 1)
            if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="resource",
                    detail=detail,
                    amount=int(amount_text),
                )

        for actor_row in [None, *entities]:
            if actor_row is None:
                actor_id = None
                source = "The settled action"
            else:
                actor_id = actor_row["entity_id"]
                source = f"{actor_row['label']}'s settled action"
            if core == f"{source} harms {subject}.":
                add(
                    actor_id=actor_id,
                    subject_id=subject_id,
                    kind="harm",
                    detail="harm",
                    amount=None,
                )
            caused_prefix = f"{source} causes "
            caused_suffix = f" to {subject}."
            if core.startswith(caused_prefix) and core.endswith(caused_suffix):
                detail = core[len(caused_prefix):-len(caused_suffix)]
                if _DETAIL_RE.fullmatch(detail):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="harm",
                        detail=detail,
                        amount=None,
                    )
            amount_match = re.fullmatch(
                re.escape(source) + r" deals ([1-9][0-9]*) HP of ("
                + _DETAIL_RE.pattern.removesuffix(r"\Z") + r") to "
                + re.escape(subject) + r"\.",
                core,
            )
            if amount_match:
                add(
                    actor_id=actor_id,
                    subject_id=subject_id,
                    kind="harm",
                    detail=amount_match.group(2),
                    amount=-int(amount_match.group(1)),
                )
            if actor_row is None:
                continue
            if core == f"{source} defeats {subject}.":
                add(
                    actor_id=actor_id,
                    subject_id=subject_id,
                    kind="defeat",
                    detail="defeated",
                    amount=None,
                )
            defeat_actor_prefix = f"{source} leaves {subject} in defeat state "
            if core.startswith(defeat_actor_prefix) and core.endswith("."):
                detail = core[len(defeat_actor_prefix):-1]
                if _DETAIL_RE.fullmatch(detail):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="defeat",
                        detail=detail,
                        amount=None,
                    )
            status_actor_prefix = f"{source} leaves {subject} with status "
            if core.startswith(status_actor_prefix) and core.endswith("."):
                detail = core[len(status_actor_prefix):-1]
                if _DETAIL_RE.fullmatch(detail):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="status",
                        detail=detail,
                        amount=None,
                    )
            resource_actor_prefix = f"{source} changes {subject}'s "
            if core.startswith(resource_actor_prefix) and core.endswith(no_delta_suffix):
                detail = core[len(resource_actor_prefix):-len(no_delta_suffix)]
                if _DETAIL_RE.fullmatch(detail):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="resource",
                        detail=detail,
                        amount=None,
                    )
            if (
                core.startswith(resource_actor_prefix)
                and core.endswith(".")
                and " by " in core
            ):
                body = core[len(resource_actor_prefix):-1]
                detail, amount_text = body.rsplit(" by ", 1)
                if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="resource",
                        detail=detail,
                        amount=int(amount_text),
                    )
            movement_actor_prefix = f"{source} moves {subject} to "
            if core.startswith(movement_actor_prefix) and core.endswith("."):
                detail = core[len(movement_actor_prefix):-1]
                if _DETAIL_RE.fullmatch(detail):
                    add(
                        actor_id=actor_id,
                        subject_id=subject_id,
                        kind="movement",
                        detail=detail,
                        amount=None,
                    )
            if subject_id == "world":
                time_actor_prefix = f"{source} changes time "
                if core.startswith(time_actor_prefix) and core.endswith(no_delta_suffix):
                    detail = core[len(time_actor_prefix):-len(no_delta_suffix)]
                    if _DETAIL_RE.fullmatch(detail):
                        add(
                            actor_id=actor_id,
                            subject_id="world",
                            kind="time",
                            detail=detail,
                            amount=None,
                        )
                if core.startswith(time_actor_prefix) and core.endswith(".") and " by " in core:
                    body = core[len(time_actor_prefix):-1]
                    detail, amount_text = body.rsplit(" by ", 1)
                    if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
                        add(
                            actor_id=actor_id,
                            subject_id="world",
                            kind="time",
                            detail=detail,
                            amount=int(amount_text),
                        )
                world_actor_prefix = f"{source} changes the world: "
                if core.startswith(world_actor_prefix) and core.endswith("."):
                    detail = core[len(world_actor_prefix):-1]
                    if _DETAIL_RE.fullmatch(detail):
                        add(
                            actor_id=actor_id,
                            subject_id="world",
                            kind="world",
                            detail=detail,
                            amount=None,
                        )
            else:
                time_actor_prefix = f"{source} changes time "
                time_subject_suffix = f" for {subject}"
                if core.startswith(time_actor_prefix) and core.endswith(no_delta_suffix):
                    body = core[len(time_actor_prefix):-len(no_delta_suffix)]
                    if body.endswith(time_subject_suffix):
                        detail = body[:-len(time_subject_suffix)]
                        if _DETAIL_RE.fullmatch(detail):
                            add(
                                actor_id=actor_id,
                                subject_id=subject_id,
                                kind="time",
                                detail=detail,
                                amount=None,
                            )
                if core.startswith(time_actor_prefix) and core.endswith(".") and " by " in core:
                    body = core[len(time_actor_prefix):-1]
                    before_amount, amount_text = body.rsplit(" by ", 1)
                    if before_amount.endswith(time_subject_suffix):
                        detail = before_amount[:-len(time_subject_suffix)]
                        if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
                            add(
                                actor_id=actor_id,
                                subject_id=subject_id,
                                kind="time",
                                detail=detail,
                                amount=int(amount_text),
                            )
                world_actor_prefix = f"{source} changes the world for {subject}: "
                if core.startswith(world_actor_prefix) and core.endswith("."):
                    detail = core[len(world_actor_prefix):-1]
                    if _DETAIL_RE.fullmatch(detail):
                        add(
                            actor_id=actor_id,
                            subject_id=subject_id,
                            kind="world",
                            detail=detail,
                            amount=None,
                        )

    no_time_delta = "Settled time "
    no_delta_suffix = " changes without a numeric delta."
    if core.startswith(no_time_delta) and core.endswith(no_delta_suffix):
        detail = core[len(no_time_delta):-len(no_delta_suffix)]
        if _DETAIL_RE.fullmatch(detail):
            add(actor_id=None, subject_id="world", kind="time", detail=detail, amount=None)
    if core.startswith(no_time_delta) and core.endswith(".") and " changes by " in core:
        body = core[len(no_time_delta):-1]
        detail, amount_text = body.rsplit(" changes by ", 1)
        if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
            add(
                actor_id=None,
                subject_id="world",
                kind="time",
                detail=detail,
                amount=int(amount_text),
            )
    world_prefix = "The settled world change is "
    if core.startswith(world_prefix) and core.endswith("."):
        detail = core[len(world_prefix):-1]
        if _DETAIL_RE.fullmatch(detail):
            add(actor_id=None, subject_id="world", kind="world", detail=detail, amount=None)
    for subject_row in entities:
        subject_id = subject_row["entity_id"]
        if subject_id == "world":
            continue
        subject = subject_row["label"]
        time_prefix = f"For {subject}, settled time "
        if core.startswith(time_prefix) and core.endswith(no_delta_suffix):
            detail = core[len(time_prefix):-len(no_delta_suffix)]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="time",
                    detail=detail,
                    amount=None,
                )
        if core.startswith(time_prefix) and core.endswith(".") and " changes by " in core:
            body = core[len(time_prefix):-1]
            detail, amount_text = body.rsplit(" changes by ", 1)
            if _DETAIL_RE.fullmatch(detail) and _INTEGER_RE.fullmatch(amount_text):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="time",
                    detail=detail,
                    amount=int(amount_text),
                )
        world_subject_prefix = f"The settled world change for {subject} is "
        if core.startswith(world_subject_prefix) and core.endswith("."):
            detail = core[len(world_subject_prefix):-1]
            if _DETAIL_RE.fullmatch(detail):
                add(
                    actor_id=None,
                    subject_id=subject_id,
                    kind="world",
                    detail=detail,
                    amount=None,
                )
    return results


def _sentence_endpoints(text: str, offset: int) -> list[int]:
    endpoints: list[int] = []
    cursor = offset
    while True:
        marker = text.find(". ", cursor)
        if marker < 0:
            break
        endpoints.append(marker + 1)
        cursor = marker + 2
    if text.endswith("."):
        endpoints.append(len(text))
    return endpoints


def observe_fallback_claim_graph(
    text: object, *, observation_context: object
) -> dict[str, Any]:
    """Blindly parse exact fallback grammar without access to the expected or ledger graph."""
    visible = validate_canonical_visible_text(text)
    context = _validate_context(observation_context)
    bindings = context["bindings"]
    if not bindings:
        if visible != EMPTY_FALLBACK_TEXT:
            raise NarrationFallbackRuntimeError("empty-ledger fallback text is not exact")
        return _build_graph(context["contract_fingerprint"], [])

    solutions: list[list[dict[str, Any]]] = []

    def visit(offset: int, slot_index: int, rows: list[dict[str, Any]]) -> None:
        if len(solutions) > 1:
            return
        if slot_index == len(bindings):
            if offset == len(visible):
                solutions.append(rows)
            return
        for end in _sentence_endpoints(visible, offset):
            sentence = visible[offset:end]
            parsed = _parse_sentence(sentence, context)
            if len(parsed) != 1:
                continue
            binding = bindings[slot_index]
            semantic = {**parsed[0], **{key: binding[key] for key in _BINDING_FIELDS - {"slot_index"}}}
            start_bytes = len(visible[:offset].encode("utf-8"))
            row = _graph_claim(
                slot_index=slot_index,
                sentence=sentence,
                start=start_bytes,
                semantic=semantic,
            )
            next_offset = end + 1 if end < len(visible) and visible[end] == " " else end
            visit(next_offset, slot_index + 1, [*rows, row])

    visit(0, 0, [])
    if len(solutions) != 1:
        raise NarrationFallbackRuntimeError(
            "visible fallback grammar is incomplete, ambiguous, or contains an extra clause"
        )
    return _build_graph(context["contract_fingerprint"], solutions[0])


def build_proof_complete_fallback(
    *,
    contract: object,
    pre_mutation_key: Mapping[str, Any],
    reservation: TurnReservation,
    occurrences: Sequence[Mapping[str, Any]],
    effects: Sequence[Mapping[str, Any]],
    rng_fingerprint: str,
    config_fingerprint: str,
    engine_version: str,
    pre_ledger_hash: str,
    mechanics_post_ledger_hash: str,
    model: str,
    stream: bool,
    consumed_intent_id: str | None = None,
    next_intent_id: str | None = None,
    source_lifecycle_key: str | None = None,
    source_envelope_fingerprint: str | None = None,
    surface_adapter: Callable[[str], str] | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> EnvelopeArtifact:
    """Construct one proof-complete fallback artifact without touching state or visibility."""

    def phase(name: str) -> None:
        if phase_hook is not None:
            phase_hook(name)

    try:
        valid = validate_narration_truth_contract(contract)
        key, request_hash = _validate_reservation(pre_mutation_key, reservation)
        mechanics_hash = _require_fp(mechanics_post_ledger_hash, "mechanics_post_ledger_hash")
        binding = valid["lifecycle_binding"]
        if binding is None:
            raise NarrationFallbackRuntimeError("truth contract has no lifecycle binding")
        if binding["branch_ref"] != key["branch_id"]:
            raise NarrationFallbackRuntimeError("truth contract belongs to another branch")
        if binding["ledger_fingerprint"] != mechanics_hash:
            raise NarrationFallbackRuntimeError("truth contract belongs to another ledger root")
        if binding["artifact_fingerprint"] != valid["realization_fingerprint"]:
            raise NarrationFallbackRuntimeError("truth contract lost narrator realization identity")
        phase("after_contract_validation")

        plan = build_fallback_realization_plan(valid)
        phase("after_construction")
        base_text = render_fallback_text(plan)
        visible_text = surface_adapter(base_text) if surface_adapter is not None else base_text
        visible_text = validate_canonical_visible_text(visible_text)
        phase("after_canonical_rendering")

        expected_graph = _validate_graph(plan.expected_graph)
        phase("after_expected_graph")
        observed_graph = observe_fallback_claim_graph(
            visible_text, observation_context=plan.observation_context
        )
        phase("after_observed_graph")
        expected_projection = canonical_claim_projection(expected_graph)
        observed_projection = canonical_claim_projection(observed_graph)
        if fingerprint(observed_projection) != fingerprint(expected_projection) \
                or observed_graph != expected_graph:
            raise NarrationFallbackRuntimeError("observed fallback graph differs from its plan")
        phase("after_graph_equality")
        ledger_graph = _validate_graph(plan.ledger_graph)
        ledger_projection = canonical_claim_projection(ledger_graph)
        if fingerprint(observed_projection) != fingerprint(ledger_projection) \
                or observed_graph != ledger_graph:
            raise NarrationFallbackRuntimeError("observed fallback graph differs from ledger truth")
        phase("after_ledger_equality")

        artifact_ref = fingerprint(
            {
                "schema": "narration-fallback-wire-ref/1",
                "lifecycle_key": key["lifecycle_key"],
                "attempt_index": reservation.attempt_index,
                "contract_fingerprint": valid["fingerprint"],
                "ledger_root_hash": mechanics_hash,
                "plan_fingerprint": plan.fingerprint,
            }
        )
        wire = encode_chat_story(
            visible_text,
            model=model,
            stream=stream,
            artifact_ref=artifact_ref,
        )
        if decode_chat_story(wire.raw, wire.content_type) != visible_text:
            raise NarrationFallbackRuntimeError("wire codec changed canonical visible text")
        phase("after_wire_encoding")

        renderer_bytes = _json_bytes(_surface_profile())
        visible_bytes = visible_text.encode("utf-8")
        proof = build_delivery_proof(
            wire_bytes=wire.raw,
            content_type=wire.content_type,
            renderer_bytes=renderer_bytes,
            visible_bytes=visible_bytes,
            expected_graph=expected_graph,
            observed_graph=observed_graph,
            ledger_graph=ledger_graph,
            ledger_root_hash=mechanics_hash,
            logical_message_identity=logical_message_id(key),
            gate_reason_code="proved_code_fallback",
        )
        phase("after_gate_receipt")

        attempt_kind = "initial" if reservation.attempt_index == 0 else "swipe"
        artifact = build_envelope(
            pre_mutation_key=key,
            attempt_index=reservation.attempt_index,
            attempt_kind=attempt_kind,
            request_hash=request_hash,
            occurrences=occurrences,
            effects=effects,
            rng_fingerprint=rng_fingerprint,
            config_fingerprint=config_fingerprint,
            engine_version=engine_version,
            pre_ledger_hash=pre_ledger_hash,
            mechanics_post_ledger_hash=mechanics_hash,
            fallback_bytes=wire.raw,
            delivery_proof=proof,
            decision="fallback",
            consumed_intent_id=consumed_intent_id,
            next_intent_id=next_intent_id,
            gate_reason_code="proved_code_fallback",
            diagnostics={
                "schema": FALLBACK_RUNTIME_DIAGNOSTICS_SCHEMA,
                "contract_fingerprint": valid["fingerprint"],
                "plan_fingerprint": plan.fingerprint,
                "observation_context_fingerprint": plan.observation_context["fingerprint"],
                "renderer_version": VISIBLE_RENDERER_VERSION,
                "renderer_config_fingerprint": VISIBLE_RENDERER_CONFIG_FINGERPRINT,
                "visible_text_fingerprint": raw_fingerprint(visible_bytes),
                "visible_glyph_fingerprint": raw_fingerprint(visible_bytes),
                "wire_content_type": wire.content_type,
                "wire_fingerprint": raw_fingerprint(wire.raw),
            },
            source_lifecycle_key=source_lifecycle_key,
            source_envelope_fingerprint=source_envelope_fingerprint,
        )
        validate_envelope(artifact)
        phase("after_envelope")
        return artifact
    except NarrationFallbackRuntimeError:
        raise
    except (NarrationTruthGateError, TurnArtifactError, ChatWireError, TypeError, ValueError) as exc:
        raise NarrationFallbackRuntimeError("proof-complete fallback construction failed") from exc
