"""Pure pre-display truth gate for completed RPG narration.

This module deliberately owns no streaming, proxy, state, or compare-and-swap integration.  It
accepts already validated ledger truth and two independently produced descriptions of the exact
candidate bytes:

* a producer-declared typed span graph; and
* a verifier graph produced without seeing that declaration.

Both graphs must agree exactly.  Their positive, actual, current mechanic claims must then equal
the ledger-derived expected graph.  Missing graphs, uncertain extraction, malformed provenance,
mechanic tags, unsupported claims, or disagreement select a deterministic code-authored fallback.

Construction is an upstream requirement.  The gate compares immutable construction and authority
references; it never repairs an actor, target, negation, cause, or occurrence by borrowing material
from another clause.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .capability_glossary import content_fingerprint, normalize_phrase, raw_fingerprint
from .compose import (
    DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
    DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
    render_deterministic_first_intent_text,
)
from .narrator_realization import (
    SKILL_CHECK_REALIZATION_ADAPTER,
    WEAPON_ATTACK_REALIZATION_ADAPTER,
    validate_narrator_realization,
)


NARRATION_TRUTH_CONTRACT_SCHEMA = "narration-truth-contract/1"
NARRATION_CLAIM_GRAPH_SCHEMA = "narration-claim-graph/1"
NARRATION_TRUTH_DECISION_SCHEMA = "narration-truth-decision/1"
OPPOSITION_FACT_SCHEMA = "narration-opposition-fact/1"
PENDING_INTENT_FACT_SCHEMA = "narration-pending-intent-fact/1"
FALLOUT_FACT_SCHEMA = "narration-fallout-fact/1"
TARGET_OUTCOME_SCHEMA = "narration-target-outcome/1"
CONSTRUCTION_PROVENANCE_REQUIREMENT = "upstream-bound-claim-graph/1"
VISIBLE_CONTENT_TYPE = "text/plain; charset=utf-8"
VISIBLE_RENDERER_VERSION = "aetherstate.safe-plain-text/1"
_VISIBLE_RENDERER_CONFIG = {
    "normalization": "NFC",
    "html": "forbidden",
    "html_entities": "forbidden",
    "markdown_links_images_strike_fences": "forbidden",
    "bidi_and_invisible_controls": "forbidden",
    "transform": "identity",
}
VISIBLE_RENDERER_CONFIG_FINGERPRINT = content_fingerprint(_VISIBLE_RENDERER_CONFIG)

CLAIM_KINDS = (
    "opposition_action",
    "pending_intent",
    "harm",
    "defeat",
    "status",
    "resource",
    "time",
    "movement",
    "world",
)
POLARITIES = ("positive", "negative", "uncertain")
ACTUALITIES = ("actual", "hypothetical", "reported", "uncertain")
TIME_SCOPES = ("current", "past", "future", "atemporal", "uncertain")
ENTITY_SCOPES = ("current", "queued", "offscreen", "reference")
GRAPH_ROLES = ("producer", "verifier", "offline_expected", "fallback")
GRAPH_STATUSES = ("complete", "uncertain")

_CONTRACT_FIELDS = {
    "schema",
    "turn",
    "delivery_mode",
    "realization_fingerprint",
    "construction_provenance_requirement",
    "realization_forbidden_codes",
    "lifecycle_binding",
    "visible_surface_profile",
    "expected_realization_plan",
    "known_entities",
    "player_events",
    "opposition_actions",
    "pending_intents",
    "fallout_facts",
    "settled_target_outcomes",
    "expected_claims",
    "fingerprint",
}
_LIFECYCLE_FIELDS = {"branch_ref", "ledger_fingerprint", "artifact_fingerprint"}
_VISIBLE_PLAN_FIELDS = {
    "schema",
    "content_type",
    "renderer_version",
    "renderer_config_fingerprint",
    "wire_fingerprint",
    "visible_text_fingerprint",
    "visible_glyph_fingerprint",
    "fingerprint",
}
_GRAPH_FIELDS = {
    "schema",
    "role",
    "candidate_fingerprint",
    "status",
    "issuer",
    "channel",
    "phase",
    "claims",
    "fingerprint",
}
_CLAIM_INPUT_FIELDS = {
    "span_start",
    "span_end",
    "clause_index",
    "occurrence_ref",
    "cause_ref",
    "authority_ref",
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
_CLAIM_FIELDS = _CLAIM_INPUT_FIELDS | {
    "issuer",
    "channel",
    "phase",
    "evidence_fingerprint",
    "claim_ref",
}
_EXPECTED_CLAIM_FIELDS = {
    "claim_ref",
    "issuer",
    "channel",
    "phase",
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
_OPPOSITION_INPUT_FIELDS = {
    "schema",
    "occurrence_ref",
    "intent_ref",
    "construction_ref",
    "actor_id",
    "actor_label",
    "target_id",
    "target_label",
    "move_id",
    "move_label",
    "outcome",
    "effects",
}
_OPPOSITION_FIELDS = _OPPOSITION_INPUT_FIELDS | {"fingerprint"}
_PENDING_INTENT_INPUT_FIELDS = {
    "schema",
    "pending_ref",
    "intent_ref",
    "intent_fingerprint",
    "construction_ref",
    "prepared_turn",
    "opening_kind",
    "actor_id",
    "actor_label",
    "target_id",
    "target_label",
    "move_id",
    "move_label",
    "tell",
    "separation",
    "response_window",
    "response_window_text",
    "visible_text",
    "intent_snapshot",
}
_PENDING_INTENT_FIELDS = _PENDING_INTENT_INPUT_FIELDS | {"fingerprint"}
_FALLOUT_INPUT_FIELDS = {
    "schema",
    "fact_ref",
    "cause_ref",
    "construction_ref",
    "subject_id",
    "subject_label",
    "effects",
}
_FALLOUT_FIELDS = _FALLOUT_INPUT_FIELDS | {"fingerprint"}
_TARGET_OUTCOME_INPUT_FIELDS = {
    "schema",
    "outcome_ref",
    "source_event_ref",
    "construction_ref",
    "target_id",
    "target_label",
    "effects",
}
_TARGET_OUTCOME_FIELDS = _TARGET_OUTCOME_INPUT_FIELDS | {"fingerprint"}
_EFFECT_FIELDS = {"kind", "detail", "amount"}
_KNOWN_INPUT_FIELDS = {"entity_id", "label", "scope"}
_KNOWN_FIELDS = _KNOWN_INPUT_FIELDS | {"aliases"}

_FP_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REF_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{0,199}\Z")
_ISSUER_RE = re.compile(r"[a-z][a-z0-9_.-]*(?:/[a-z0-9_.-]+)*/[1-9][0-9]*\Z")
_MECHANIC_TAG_RE = re.compile(
    r"\[(?:hp|foe|effect|state|resource|time|clock|status|combat|war|player|check|roll|"
    r"enemy\s+action|enemy\s+intent|directive|aether)\b[^\]]*\]|"
    r"<(?:hp|status|resource|state|effect)\b[^>]*>",
    re.IGNORECASE,
)
_UNSAFE_VISIBLE_RE = re.compile(
    r"<!--|-->|<\s*/?\s*[a-z][^>]*>|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);|"
    r"!\[[^\]]*\]\([^)]*\)|\[[^\]]+\]\([^)]*\)|~~|\x60{3}|~~~|"
    r"::(?:before|after)|\bcontent\s*:|[*_\[\]|]|"
    r"^[ ]{0,3}(?:#{1,6}(?:[ \t]|$)|>(?:[ \t]|$)|[-+](?:[ \t])|"
    r"[0-9]{1,9}[.)](?:[ \t])|(?:={3,}|-{3,})[ \t]*$)|"
    r"^[ ]{4,}|[ \t]+$",
    re.IGNORECASE | re.MULTILINE,
)
_INVISIBLE_OR_BIDI = frozenset(
    {
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2060",
        "\u2061",
        "\u2062",
        "\u2063",
        "\u2064",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)
_MASS_RE = re.compile(
    r"\b(?:all\s+(?:six|6|of\s+them)|(?:two|three|four|five|six|seven|eight|nine|ten|\d+)"
    r"\s+(?:foes?|enemies|soldiers|guards|hollows?|people)|dozens?|hundreds?|several|many|"
    r"multiple|the\s+entire\s+(?:horde|host|group|wave|army|cohort))\b.{0,50}"
    r"\b(?:die|dies|fall|falls|perish|perishes|burn|burns|collapse|collapses|are\s+slain|"
    r"are\s+killed|are\s+destroyed)\b|\b(?:wipes?\s+out|slaughters?)\s+(?:them\s+all|"
    r"the\s+(?:horde|host|group|wave|army|cohort))\b",
    re.IGNORECASE,
)

_KIND_PATTERNS = {
    "harm": re.compile(
        r"\b(?:harm(?:s|ed)?|hurt(?:s)?|wound(?:s|ed)?|injur(?:e|es|ed)|"
        r"burn(?:s|ed|ing)?|scorch(?:es|ed)?|bleed(?:s|ing)?|pierc(?:e|es|ed)|"
        r"impal(?:e|es|ed)|slash(?:es|ed)?|damage(?:s|d)?|crush(?:es|ed)?|"
        r"maul(?:s|ed)?|tear(?:s)?|tore|rips?|hits?|strikes?)\b"
    ),
    "defeat": re.compile(
        r"\b(?:die|dies|dead|defeated|slain|killed|destroyed|annihilated|"
        r"collapses?\s+lifeless|falls?\s+lifeless|erased\s+from\s+existence)\b"
    ),
    "status": re.compile(
        r"\b(?:stunned|poisoned|frozen|paraly[sz]ed|pinned|restrained|blinded|"
        r"silenced|knocked\s+prone|immobilized|banished|cursed)\b"
    ),
    "resource": re.compile(
        r"\b(?:mana|stamina|focus|energy|charges?|ammo|spell\s+slots?|resource)\b.{0,24}"
        r"\b(?:loses?|lost|spends?|spent|gains?|gained|drains?|drained|drops?|rises?)\b|"
        r"\b(?:loses?|lost|spends?|spent|gains?|gained|drains?|drained)\b.{0,24}"
        r"\b(?:mana|stamina|focus|energy|charges?|ammo|spell\s+slots?|resource)\b"
    ),
    "time": re.compile(
        r"\b(?:an?\s+hour|hours|minutes|days|weeks|the\s+clock|time)\b.{0,24}"
        r"\b(?:passes?|passed|advances?|advanced|jumps?|skips?|rewinds?)\b|"
        r"\b(?:later\s+that\s+day|the\s+next\s+day|days?\s+later)\b"
    ),
    "movement": re.compile(
        r"\b(?:teleported|hurled|thrown|knocked\s+back|pushed|pulled|dragged|"
        r"forced\s+back|displaced|banished)\b"
    ),
    "world": re.compile(
        r"\b(?:gate|door|wall|bridge|tower|building|floor|ceiling|room|city|reality|world)"
        r"\b.{0,30}\b(?:opens?|closes?|collapses?|shatters?|breaks?|falls?|floods?|"
        r"burns?|vanishes|is\s+destroyed)\b"
    ),
}
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|dont|doesnt|didnt|cannot|cant|wont|"
    r"don\s+t|doesn\s+t|didn\s+t|can\s+t|won\s+t)\b"
)
_HYPOTHETICAL_RE = re.compile(r"\b(?:if|would|could|might|may|imagine|suppose|perhaps)\b")
_REPORTED_RE = re.compile(r"\b(?:says?|said|claims?|reports?|heard|remembers?|believes?)\b")
_PAST_RE = re.compile(r"\b(?:was|were|had|earlier|before|yesterday|previously)\b")
_FUTURE_RE = re.compile(r"\b(?:will|shall|soon|later|tomorrow|going\s+to)\b")
_MISS_RE = re.compile(r"\b(?:misses?|missed|fails?\s+to\s+connect|goes?\s+wide)\b")
_BLOCK_RE = re.compile(r"\b(?:blocked|deflected|absorbed|stopped)\b")
_HIT_RE = re.compile(r"\b(?:hits?|strikes?|lands?|connects?|harms?|hurts?|damages?)\b")
_SAFE_NONLIVING = frozenset(
    {
        "air",
        "clouds",
        "darkness",
        "echo",
        "fire",
        "flame",
        "hope",
        "light",
        "night",
        "shadow",
        "shadows",
        "silence",
        "sky",
        "sound",
        "torch",
        "wind",
    }
)

VIOLATION_CODES = (
    "malformed_contract",
    "malformed_reply",
    "missing_lifecycle_binding",
    "missing_code_authored_realization_plan",
    "candidate_realization_plan_mismatch",
    "unsafe_visible_surface",
    "normalization_drift",
    "missing_producer_graph",
    "malformed_producer_graph",
    "uncertain_producer_graph",
    "missing_verifier_graph",
    "malformed_verifier_graph",
    "uncertain_verifier_graph",
    "malformed_offline_expected_graph",
    "uncertain_offline_expected_graph",
    "producer_verifier_graph_disagreement",
    "offline_graph_disagreement",
    "claim_extraction_uncertain",
    "verifier_graph_omits_surface_claim",
    "mechanic_tag",
    "mass_or_aggregate_casualty",
    "expected_claim_omitted",
    "opposition_action_omitted",
    "opposition_action_misattributed",
    "autonomous_intent_repeated",
    "unsupported_named_target_harm",
    "unsupported_named_target_defeat",
    "unsupported_named_target_status",
    "unsupported_named_target_resource",
    "unsupported_time_change",
    "unsupported_named_target_movement",
    "unsupported_world_change",
    "unsupported_opposition_action",
    "unsupported_pending_intent",
)
_VIOLATION_ORDER = {code: index for index, code in enumerate(VIOLATION_CODES)}


class NarrationTruthGateError(ValueError):
    """A truth contract, exact fact, or candidate claim graph is malformed."""


@dataclass(frozen=True)
class NarrationTruthDecision:
    """Pure gate output.

    visible_claim_graph is the producer graph when allowed and the code-authored fallback graph
    otherwise.  The credential-free receipt contains hashes, counts, and stable reason codes only.
    """

    visible_text: str
    visible_claim_graph: dict[str, Any]
    receipt: dict[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NarrationTruthGateError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise NarrationTruthGateError(
            f"{label} fields differ; unexpected={extra}, missing={missing}"
        )


def _plain(value: object, label: str, *, limit: int = 160) -> str:
    if not isinstance(value, str):
        raise NarrationTruthGateError(f"{label} must be text")
    text = " ".join(value.split())
    if not text or len(text) > limit or any(mark in text for mark in "\r\n[]{}<>"):
        raise NarrationTruthGateError(f"{label} is not bounded plain text")
    return text


def _ref(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        raise NarrationTruthGateError(f"{label} must be a stable lowercase reference")
    return value


def _fp(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _FP_RE.fullmatch(value):
        raise NarrationTruthGateError(f"{label} must be a sha256 fingerprint")
    return value


def _issuer(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ISSUER_RE.fullmatch(value):
        raise NarrationTruthGateError(f"{label} must be a versioned issuer")
    return value


def _candidate_fingerprint(text: str) -> str:
    return raw_fingerprint(text.encode("utf-8"))


def _visible_surface_profile() -> dict[str, Any]:
    return {
        "content_type": VISIBLE_CONTENT_TYPE,
        "renderer_version": VISIBLE_RENDERER_VERSION,
        "renderer_config_fingerprint": VISIBLE_RENDERER_CONFIG_FINGERPRINT,
    }


def _safe_visible_text(text: str) -> tuple[bool, list[str]]:
    codes: list[str] = []
    if unicodedata.normalize("NFC", text) != text:
        codes.append("normalization_drift")
    if (
        _UNSAFE_VISIBLE_RE.search(text)
        or any(character in _INVISIBLE_OR_BIDI for character in text)
        or any(
            unicodedata.category(character) == "Cc" and character != "\n"
            for character in text
        )
    ):
        codes.append("unsafe_visible_surface")
    return not codes, codes


def _visible_glyph_fingerprint(text: str) -> str:
    return raw_fingerprint(unicodedata.normalize("NFC", text).encode("utf-8"))


def _visible_plan(text: str) -> dict[str, Any]:
    safe, codes = _safe_visible_text(text)
    if not safe:
        raise NarrationTruthGateError(
            f"code-authored visible plan is unsafe: {','.join(codes)}"
        )
    payload = {
        "schema": "narration-visible-plan/1",
        **_visible_surface_profile(),
        "wire_fingerprint": _candidate_fingerprint(text),
        "visible_text_fingerprint": _candidate_fingerprint(text),
        "visible_glyph_fingerprint": _visible_glyph_fingerprint(text),
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def _validate_visible_plan(value: object, label: str) -> dict[str, Any]:
    row = _mapping(value, label)
    _exact_fields(row, _VISIBLE_PLAN_FIELDS, label)
    if row["schema"] != "narration-visible-plan/1":
        raise NarrationTruthGateError(f"{label} schema is unsupported")
    if {
        "content_type": row["content_type"],
        "renderer_version": row["renderer_version"],
        "renderer_config_fingerprint": row["renderer_config_fingerprint"],
    } != _visible_surface_profile():
        raise NarrationTruthGateError(f"{label} visible surface profile mismatch")
    for field in (
        "wire_fingerprint",
        "visible_text_fingerprint",
        "visible_glyph_fingerprint",
        "fingerprint",
    ):
        _fp(row[field], f"{label}.{field}")
    payload = {key: row[key] for key in row if key != "fingerprint"}
    if row["fingerprint"] != content_fingerprint(payload):
        raise NarrationTruthGateError(f"{label} fingerprint mismatch")
    return deepcopy(dict(row))


def _default_label(entity_id: str) -> str:
    words = normalize_phrase(entity_id).split()
    return " ".join(word.capitalize() for word in words) or entity_id


def _aliases(entity_id: str, label: str) -> list[str]:
    values = {normalize_phrase(entity_id), normalize_phrase(label)}
    label_words = normalize_phrase(label).split()
    if label_words:
        last = label_words[-1]
        if len(last) >= 4 and not re.fullmatch(r"x?\d+", last):
            values.add(last)
        if re.fullmatch(r"x?\d+", last) and len(label_words) > 1:
            values.add(" ".join(label_words[:-1]))
    return sorted(value for value in values if value)


def _canonical_effects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NarrationTruthGateError(f"{label} must be a list")
    effects: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        row = _mapping(raw, f"{label}[{index}]")
        _exact_fields(row, _EFFECT_FIELDS, f"{label}[{index}]")
        kind = row["kind"]
        if kind not in CLAIM_KINDS or kind in {"opposition_action", "pending_intent"}:
            raise NarrationTruthGateError(f"{label}[{index}].kind is unsupported")
        detail = normalize_phrase(_plain(row["detail"], f"{label}[{index}].detail", limit=100))
        amount = row["amount"]
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise NarrationTruthGateError(f"{label}[{index}].amount must be an integer or null")
        if kind == "harm" and amount is not None and amount >= 0:
            raise NarrationTruthGateError(f"{label}[{index}] harm amount must be a negative delta")
        if kind in {"defeat", "status", "movement", "world"} and amount is not None:
            raise NarrationTruthGateError(f"{label}[{index}] cannot carry an amount")
        effects.append({"kind": kind, "detail": detail, "amount": amount})
    effects.sort(key=lambda row: (row["kind"], row["detail"], row["amount"] or 0))
    keys = [(row["kind"], row["detail"], row["amount"]) for row in effects]
    if len(keys) != len(set(keys)):
        raise NarrationTruthGateError(f"{label} repeats an effect")
    return effects


def _sealed_row(
    value: object,
    *,
    label: str,
    input_fields: set[str],
    sealed_fields: set[str],
    build_payload: Any,
) -> dict[str, Any]:
    row = _mapping(value, label)
    sealed = "fingerprint" in row
    _exact_fields(row, sealed_fields if sealed else input_fields, label)
    payload = build_payload(row)
    fingerprint = content_fingerprint(payload)
    if sealed and row["fingerprint"] != fingerprint:
        raise NarrationTruthGateError(f"{label} fingerprint mismatch")
    return {**payload, "fingerprint": fingerprint}


def _opposition_row(value: object, label: str) -> dict[str, Any]:
    def payload(row: Mapping[str, Any]) -> dict[str, Any]:
        outcome = row["outcome"]
        if outcome not in {"hit", "miss", "blocked"}:
            raise NarrationTruthGateError(f"{label}.outcome is unsupported")
        effects = _canonical_effects(row["effects"], f"{label}.effects")
        if outcome == "miss" and effects:
            raise NarrationTruthGateError(f"{label} miss cannot carry effects")
        return {
            "schema": OPPOSITION_FACT_SCHEMA
            if row["schema"] == OPPOSITION_FACT_SCHEMA
            else (_ for _ in ()).throw(
                NarrationTruthGateError(f"{label} schema is unsupported")
            ),
            "occurrence_ref": _ref(row["occurrence_ref"], f"{label}.occurrence_ref"),
            "intent_ref": _ref(row["intent_ref"], f"{label}.intent_ref"),
            "construction_ref": _fp(row["construction_ref"], f"{label}.construction_ref"),
            "actor_id": _ref(row["actor_id"], f"{label}.actor_id"),
            "actor_label": _plain(row["actor_label"], f"{label}.actor_label"),
            "target_id": _ref(row["target_id"], f"{label}.target_id"),
            "target_label": _plain(row["target_label"], f"{label}.target_label"),
            "move_id": _ref(row["move_id"], f"{label}.move_id"),
            "move_label": _plain(row["move_label"], f"{label}.move_label"),
            "outcome": outcome,
            "effects": effects,
        }

    return _sealed_row(
        value,
        label=label,
        input_fields=_OPPOSITION_INPUT_FIELDS,
        sealed_fields=_OPPOSITION_FIELDS,
        build_payload=payload,
    )


def _pending_intent_row(value: object, label: str) -> dict[str, Any]:
    """Validate one fully state-bound future enemy action and its exact visible response window."""

    def payload(row: Mapping[str, Any]) -> dict[str, Any]:
        if row["schema"] != PENDING_INTENT_FACT_SCHEMA:
            raise NarrationTruthGateError(f"{label} schema is unsupported")
        prepared_turn = row["prepared_turn"]
        if isinstance(prepared_turn, bool) or not isinstance(prepared_turn, int) \
                or prepared_turn < 0:
            raise NarrationTruthGateError(
                f"{label}.prepared_turn must be a non-negative integer"
            )
        opening_kind = row["opening_kind"]
        if opening_kind not in {"combat_opening", "following_intent"}:
            raise NarrationTruthGateError(f"{label}.opening_kind is unsupported")

        snapshot = _mapping(row["intent_snapshot"], f"{label}.intent_snapshot")
        if any(not isinstance(key, str) for key in snapshot):
            raise NarrationTruthGateError(
                f"{label}.intent_snapshot fields must be text"
            )
        snapshot = deepcopy(dict(snapshot))
        if snapshot.get("schema") != "enemy-intent/1":
            raise NarrationTruthGateError(
                f"{label}.intent_snapshot schema is unsupported"
            )
        intent_fingerprint = content_fingerprint(snapshot)
        if row["intent_fingerprint"] != intent_fingerprint:
            raise NarrationTruthGateError(f"{label}.intent_fingerprint mismatch")

        intent_ref = _ref(row["intent_ref"], f"{label}.intent_ref")
        actor_id = _ref(row["actor_id"], f"{label}.actor_id")
        target_id = _ref(row["target_id"], f"{label}.target_id")
        move_id = _ref(row["move_id"], f"{label}.move_id")
        tell = _plain(row["tell"], f"{label}.tell")
        actor_label = _plain(row["actor_label"], f"{label}.actor_label")
        target_label = _plain(row["target_label"], f"{label}.target_label")
        move_label = _plain(row["move_label"], f"{label}.move_label")
        if (
            snapshot.get("id") != intent_ref
            or snapshot.get("actor") != actor_id
            or snapshot.get("target") != target_id
            or snapshot.get("move_id") != move_id
            or snapshot.get("target_name") != target_label
            or snapshot.get("move_name") != move_label
            or snapshot.get("tell") != tell
            or snapshot.get("prepared_turn") != prepared_turn
        ):
            raise NarrationTruthGateError(
                f"{label} differs from its exact pending intent snapshot"
            )

        separation = row["separation"]
        if not isinstance(separation, str) or len(separation) > 80 \
                or separation != " ".join(separation.split()) \
                or any(mark in separation for mark in "\r\n[]{}<>"):
            raise NarrationTruthGateError(f"{label}.separation is not bounded plain text")
        if row["response_window"] != DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW \
                or row["response_window_text"] != DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT:
            raise NarrationTruthGateError(f"{label} response window identity drifted")
        visible_text = row["visible_text"]
        expected_text = render_deterministic_first_intent_text(
            actor_label,
            tell,
            separation=separation,
        )
        if visible_text != expected_text:
            raise NarrationTruthGateError(f"{label}.visible_text is not exact")
        safe, codes = _safe_visible_text(visible_text)
        if not safe:
            raise NarrationTruthGateError(
                f"{label}.visible_text is unsafe: {','.join(codes)}"
            )

        construction_payload = {
            "schema": "runtime-pending-intent-construction/2",
            "turn": prepared_turn,
            "opening_kind": opening_kind,
            "intent_snapshot": snapshot,
            "intent_fingerprint": intent_fingerprint,
            "actor_label": actor_label,
            "target_label": target_label,
            "move_label": move_label,
            "tell": tell,
            "separation": separation,
            "response_window": DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
            "response_window_text": DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
            "visible_text": expected_text,
        }
        construction_ref = content_fingerprint(construction_payload)
        if row["construction_ref"] != construction_ref:
            raise NarrationTruthGateError(f"{label}.construction_ref mismatch")
        pending_ref = _ref(row["pending_ref"], f"{label}.pending_ref")
        if pending_ref != f"pending_intent.{construction_ref[7:39]}":
            raise NarrationTruthGateError(f"{label}.pending_ref is not canonical")
        return {
            "schema": PENDING_INTENT_FACT_SCHEMA,
            "pending_ref": pending_ref,
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
            "visible_text": expected_text,
            "intent_snapshot": snapshot,
        }

    return _sealed_row(
        value,
        label=label,
        input_fields=_PENDING_INTENT_INPUT_FIELDS,
        sealed_fields=_PENDING_INTENT_FIELDS,
        build_payload=payload,
    )


def _fallout_row(value: object, label: str) -> dict[str, Any]:
    def payload(row: Mapping[str, Any]) -> dict[str, Any]:
        if row["schema"] != FALLOUT_FACT_SCHEMA:
            raise NarrationTruthGateError(f"{label} schema is unsupported")
        effects = _canonical_effects(row["effects"], f"{label}.effects")
        if not effects:
            raise NarrationTruthGateError(f"{label} must carry at least one effect")
        return {
            "schema": FALLOUT_FACT_SCHEMA,
            "fact_ref": _ref(row["fact_ref"], f"{label}.fact_ref"),
            "cause_ref": _ref(row["cause_ref"], f"{label}.cause_ref"),
            "construction_ref": _fp(row["construction_ref"], f"{label}.construction_ref"),
            "subject_id": _ref(row["subject_id"], f"{label}.subject_id"),
            "subject_label": _plain(row["subject_label"], f"{label}.subject_label"),
            "effects": effects,
        }

    return _sealed_row(
        value,
        label=label,
        input_fields=_FALLOUT_INPUT_FIELDS,
        sealed_fields=_FALLOUT_FIELDS,
        build_payload=payload,
    )


def _target_outcome_row(value: object, label: str) -> dict[str, Any]:
    def payload(row: Mapping[str, Any]) -> dict[str, Any]:
        if row["schema"] != TARGET_OUTCOME_SCHEMA:
            raise NarrationTruthGateError(f"{label} schema is unsupported")
        effects = _canonical_effects(row["effects"], f"{label}.effects")
        if not effects:
            raise NarrationTruthGateError(f"{label} must carry at least one effect")
        return {
            "schema": TARGET_OUTCOME_SCHEMA,
            "outcome_ref": _ref(row["outcome_ref"], f"{label}.outcome_ref"),
            "source_event_ref": _ref(row["source_event_ref"], f"{label}.source_event_ref"),
            "construction_ref": _fp(row["construction_ref"], f"{label}.construction_ref"),
            "target_id": _ref(row["target_id"], f"{label}.target_id"),
            "target_label": _plain(row["target_label"], f"{label}.target_label"),
            "effects": effects,
        }

    return _sealed_row(
        value,
        label=label,
        input_fields=_TARGET_OUTCOME_INPUT_FIELDS,
        sealed_fields=_TARGET_OUTCOME_FIELDS,
        build_payload=payload,
    )


def _known_row(value: object, label: str) -> dict[str, Any]:
    row = _mapping(value, label)
    _exact_fields(row, _KNOWN_FIELDS if "aliases" in row else _KNOWN_INPUT_FIELDS, label)
    entity_id = _ref(row["entity_id"], f"{label}.entity_id")
    entity_label = _plain(row["label"], f"{label}.label")
    scope = row["scope"]
    if scope not in ENTITY_SCOPES:
        raise NarrationTruthGateError(f"{label}.scope is unsupported")
    aliases = _aliases(entity_id, entity_label)
    if "aliases" in row and row["aliases"] != aliases:
        raise NarrationTruthGateError(f"{label}.aliases are not canonical")
    return {"entity_id": entity_id, "label": entity_label, "scope": scope, "aliases": aliases}


def _player_events(packet: Mapping[str, Any], labels: Mapping[str, str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for state, bucket in (
        ("settled", packet["asserted_settled"]),
        ("unresolved", packet["asserted_unresolved"]),
        ("noncurrent", packet["attributed_noncurrent"]),
    ):
        for row in bucket:
            meaning = row["event_meaning"]
            actor_id = meaning["actor_id"]
            target_id = meaning["target_entity_id"]
            events.append(
                {
                    "event_ref": row["event_ref"],
                    "event_state": state,
                    "adapter_id": row.get("adapter_id"),
                    "frame_ref": row["frame_ref"],
                    "meaning_ref": meaning["meaning_ref"],
                    "capability_id": meaning["capability_id"],
                    "invoked_capability_ids": list(meaning["invoked_capability_ids"]),
                    "actor_id": actor_id,
                    "actor_label": labels.get(actor_id, _default_label(actor_id)),
                    "target_id": target_id,
                    "target_label": labels.get(target_id, _default_label(target_id))
                    if target_id
                    else None,
                    "action_class": meaning["action_class"],
                    "outcome_quality": row.get("outcome_quality"),
                    "impact_kind": row.get("impact_kind"),
                    "target_state": row.get("target_state"),
                    "settled_change_kinds": list(row.get("settled_change_kinds", [])),
                }
            )
    return sorted(events, key=lambda row: row["event_ref"])


def _expected_claim(
    *,
    occurrence_ref: str,
    cause_ref: str,
    construction_ref: str,
    actor_id: str | None,
    subject_ids: list[str],
    kind: str,
    multiplicity: int,
    detail: str,
    amount: int | None,
    time_scope: str = "current",
) -> dict[str, Any]:
    payload = {
        "issuer": "aetherstate.ledger/1",
        "channel": "mechanics",
        "phase": "settled",
        "occurrence_ref": occurrence_ref,
        "cause_ref": cause_ref,
        "construction_ref": construction_ref,
        "actor_id": actor_id,
        "subject_ids": sorted(subject_ids),
        "kind": kind,
        "polarity": "positive",
        "actuality": "actual",
        "time_scope": time_scope,
        "multiplicity": multiplicity,
        "detail": normalize_phrase(detail),
        "amount": amount,
    }
    return {**payload, "claim_ref": content_fingerprint(payload)}


def _derive_expected_claims(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    outcomes_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for row in contract["settled_target_outcomes"]:
        outcomes_by_source.setdefault(row["source_event_ref"], []).append(row)

    player_by_ref = {row["event_ref"]: row for row in contract["player_events"]}
    opposition_by_ref = {
        row["occurrence_ref"]: row for row in contract["opposition_actions"]
    }
    fallout_by_ref = {row["fact_ref"]: row for row in contract["fallout_facts"]}

    for event in contract["player_events"]:
        if event["event_state"] != "settled" or event["event_ref"] in outcomes_by_source:
            continue
        target_id = event["target_id"]
        if (
            target_id
            and event["adapter_id"] == WEAPON_ATTACK_REALIZATION_ADAPTER
            and event["impact_kind"] == "harm"
        ):
            claims.append(
                _expected_claim(
                    occurrence_ref=event["event_ref"],
                    cause_ref=event["frame_ref"],
                    construction_ref=event["frame_ref"],
                    actor_id=event["actor_id"],
                    subject_ids=[target_id],
                    kind="harm",
                    multiplicity=1,
                    detail="harm",
                    amount=None,
                )
            )
            if event["target_state"] == "defeated":
                claims.append(
                    _expected_claim(
                        occurrence_ref=event["event_ref"],
                        cause_ref=event["frame_ref"],
                        construction_ref=event["frame_ref"],
                        actor_id=event["actor_id"],
                        subject_ids=[target_id],
                        kind="defeat",
                        multiplicity=1,
                        detail="defeated",
                        amount=None,
                    )
                )

    for action in contract["opposition_actions"]:
        claims.append(
            _expected_claim(
                occurrence_ref=action["occurrence_ref"],
                cause_ref=action["intent_ref"],
                construction_ref=action["construction_ref"],
                actor_id=action["actor_id"],
                subject_ids=[action["target_id"]],
                kind="opposition_action",
                multiplicity=1,
                detail=f"{action['move_id']} {action['outcome']}",
                amount=None,
            )
        )
        if action["occurrence_ref"] not in outcomes_by_source:
            for effect in action["effects"]:
                claims.append(
                    _expected_claim(
                        occurrence_ref=action["occurrence_ref"],
                        cause_ref=action["occurrence_ref"],
                        construction_ref=action["construction_ref"],
                        actor_id=action["actor_id"],
                        subject_ids=[action["target_id"]],
                        kind=effect["kind"],
                        multiplicity=1,
                        detail=effect["detail"],
                        amount=effect["amount"],
                    )
                )

    for intent in contract["pending_intents"]:
        claims.append(
            _expected_claim(
                occurrence_ref=intent["pending_ref"],
                cause_ref=intent["intent_ref"],
                construction_ref=intent["construction_ref"],
                actor_id=intent["actor_id"],
                subject_ids=[intent["target_id"]],
                kind="pending_intent",
                multiplicity=1,
                detail=f"{intent['move_id']} {intent['tell']}",
                amount=None,
                time_scope="future",
            )
        )

    for fact in contract["fallout_facts"]:
        for effect in fact["effects"]:
            claims.append(
                _expected_claim(
                    occurrence_ref=fact["fact_ref"],
                    cause_ref=fact["cause_ref"],
                    construction_ref=fact["construction_ref"],
                    actor_id=None,
                    subject_ids=[fact["subject_id"]],
                    kind=effect["kind"],
                    multiplicity=1,
                    detail=effect["detail"],
                    amount=effect["amount"],
                )
            )

    for source_ref, outcomes in outcomes_by_source.items():
        source = player_by_ref.get(source_ref) or opposition_by_ref.get(source_ref)
        if source is None and source_ref not in fallout_by_ref:
            raise NarrationTruthGateError(
                f"settled target outcomes cite unknown source {source_ref}"
            )
        actor_id = source.get("actor_id") if source else None
        multiplicity = len({row["target_id"] for row in outcomes})
        for outcome in outcomes:
            for effect in outcome["effects"]:
                claims.append(
                    _expected_claim(
                        occurrence_ref=source_ref,
                        cause_ref=source_ref,
                        construction_ref=outcome["construction_ref"],
                        actor_id=actor_id,
                        subject_ids=[outcome["target_id"]],
                        kind=effect["kind"],
                        multiplicity=multiplicity,
                        detail=effect["detail"],
                        amount=effect["amount"],
                    )
                )

    return sorted(
        claims,
        key=lambda row: (
            row["occurrence_ref"],
            row["kind"],
            row["subject_ids"],
            row["detail"],
            row["claim_ref"],
        ),
    )


def build_narration_truth_contract(
    narrator_realization: object,
    *,
    opposition_facts: Iterable[Mapping[str, Any]] = (),
    pending_intents: Iterable[Mapping[str, Any]] = (),
    fallout_facts: Iterable[Mapping[str, Any]] = (),
    settled_target_outcomes: Iterable[Mapping[str, Any]] = (),
    known_entities: Iterable[Mapping[str, Any]] = (),
    lifecycle_binding: Mapping[str, Any] | None = None,
    expected_realization_text: str | None = None,
) -> dict[str, Any]:
    """Build a sealed ledger-derived claim graph.

    Every supplied external fact requires an upstream construction fingerprint.  Exact target
    outcomes are generic multiplicity truth for already settled occurrences; this function does
    not create area mechanics, select targets, roll outcomes, or alter state.
    """
    packet = validate_narrator_realization(narrator_realization)
    opposition = sorted(
        (_opposition_row(row, f"opposition_facts[{index}]") for index, row in enumerate(opposition_facts)),
        key=lambda row: row["occurrence_ref"],
    )
    pending = sorted(
        (
            _pending_intent_row(row, f"pending_intents[{index}]")
            for index, row in enumerate(pending_intents)
        ),
        key=lambda row: row["pending_ref"],
    )
    fallout = sorted(
        (_fallout_row(row, f"fallout_facts[{index}]") for index, row in enumerate(fallout_facts)),
        key=lambda row: row["fact_ref"],
    )
    outcomes = sorted(
        (
            _target_outcome_row(row, f"settled_target_outcomes[{index}]")
            for index, row in enumerate(settled_target_outcomes)
        ),
        key=lambda row: row["outcome_ref"],
    )

    entities: dict[str, dict[str, Any]] = {}

    def add_entity(entity_id: str, label: str, scope: str) -> None:
        existing = entities.get(entity_id)
        if existing and existing["label"] != label:
            raise NarrationTruthGateError(f"entity {entity_id} has conflicting labels")
        rank = {"reference": 0, "offscreen": 1, "queued": 2, "current": 3}
        if not existing or rank[scope] > rank[existing["scope"]]:
            entities[entity_id] = {
                "entity_id": entity_id,
                "label": label,
                "scope": scope,
                "aliases": _aliases(entity_id, label),
            }

    for index, raw in enumerate(known_entities):
        row = _known_row(raw, f"known_entities[{index}]")
        add_entity(row["entity_id"], row["label"], row["scope"])
    add_entity("world", "world", "current")
    for bucket in (
        packet["asserted_settled"],
        packet["asserted_unresolved"],
        packet["attributed_noncurrent"],
    ):
        for row in bucket:
            meaning = row["event_meaning"]
            actor = meaning["actor_id"]
            add_entity(actor, entities.get(actor, {}).get("label", _default_label(actor)), "current")
            target = meaning["target_entity_id"]
            if target:
                add_entity(
                    target,
                    entities.get(target, {}).get("label", _default_label(target)),
                    "current",
                )
    for row in opposition:
        add_entity(row["actor_id"], row["actor_label"], "current")
        add_entity(row["target_id"], row["target_label"], "current")
    for row in pending:
        add_entity(row["actor_id"], row["actor_label"], "current")
        add_entity(row["target_id"], row["target_label"], "current")
    for row in fallout:
        add_entity(row["subject_id"], row["subject_label"], "current")
    for row in outcomes:
        add_entity(row["target_id"], row["target_label"], "current")

    labels = {entity_id: row["label"] for entity_id, row in entities.items()}
    player_events = _player_events(packet, labels)
    player_by_ref = {row["event_ref"]: row for row in player_events}
    source_refs = set(player_by_ref)
    source_refs.update(row["occurrence_ref"] for row in opposition)
    source_refs.update(row["fact_ref"] for row in fallout)
    for row in outcomes:
        if row["source_event_ref"] not in source_refs:
            raise NarrationTruthGateError(
                f"settled target outcome {row['outcome_ref']} has no exact source occurrence"
            )
        player = player_by_ref.get(row["source_event_ref"])
        if player:
            if player["event_state"] != "settled":
                raise NarrationTruthGateError("target outcomes require a settled Player occurrence")
            harmful = any(effect["kind"] in {"harm", "defeat"} for effect in row["effects"])
            if harmful and not (
                player["impact_kind"] == "harm"
                and "hp" in player["settled_change_kinds"]
                and player["adapter_id"] != SKILL_CHECK_REALIZATION_ADAPTER
            ):
                raise NarrationTruthGateError(
                    "target outcomes cannot lend HP authority to a no-impact skill check"
                )

    occurrence_refs = [row["occurrence_ref"] for row in opposition]
    intent_refs = [row["intent_ref"] for row in opposition]
    if len(occurrence_refs) != len(set(occurrence_refs)):
        raise NarrationTruthGateError("opposition occurrence_ref must be unique")
    if len(intent_refs) != len(set(intent_refs)):
        raise NarrationTruthGateError("one autonomous intent cannot produce two current actions")
    pending_refs = [row["pending_ref"] for row in pending]
    pending_intent_refs = [row["intent_ref"] for row in pending]
    if len(pending_refs) != len(set(pending_refs)):
        raise NarrationTruthGateError("pending intent occurrence_ref must be unique")
    if len(pending_intent_refs) != len(set(pending_intent_refs)):
        raise NarrationTruthGateError("one future intent cannot produce two pending facts")
    if set(intent_refs).intersection(pending_intent_refs):
        raise NarrationTruthGateError(
            "one enemy intent cannot be both settled and pending on the current turn"
        )
    if len(pending) > 1:
        raise NarrationTruthGateError("one turn cannot expose multiple pending enemy actions")
    target_keys = [(row["source_event_ref"], row["target_id"]) for row in outcomes]
    if len(target_keys) != len(set(target_keys)):
        raise NarrationTruthGateError("one source occurrence repeats an exact target outcome")

    lifecycle: dict[str, Any] | None = None
    if lifecycle_binding is not None:
        raw_lifecycle = _mapping(lifecycle_binding, "lifecycle_binding")
        _exact_fields(raw_lifecycle, _LIFECYCLE_FIELDS, "lifecycle_binding")
        lifecycle = {
            "branch_ref": _ref(
                raw_lifecycle["branch_ref"], "lifecycle_binding.branch_ref"
            ),
            "ledger_fingerprint": _fp(
                raw_lifecycle["ledger_fingerprint"],
                "lifecycle_binding.ledger_fingerprint",
            ),
            "artifact_fingerprint": _fp(
                raw_lifecycle["artifact_fingerprint"],
                "lifecycle_binding.artifact_fingerprint",
            ),
        }
    if expected_realization_text is not None and not isinstance(
        expected_realization_text, str
    ):
        raise NarrationTruthGateError("expected_realization_text must be text or null")

    payload: dict[str, Any] = {
        "schema": NARRATION_TRUTH_CONTRACT_SCHEMA,
        "turn": packet["turn"],
        "delivery_mode": packet["delivery_mode"],
        "realization_fingerprint": packet["fingerprint"],
        "construction_provenance_requirement": CONSTRUCTION_PROVENANCE_REQUIREMENT,
        "realization_forbidden_codes": sorted(
            {row["code"] for row in packet["forbidden_inference"]}
        ),
        "lifecycle_binding": lifecycle,
        "visible_surface_profile": _visible_surface_profile(),
        "expected_realization_plan": (
            _visible_plan(expected_realization_text)
            if expected_realization_text is not None
            else None
        ),
        "known_entities": sorted(entities.values(), key=lambda row: row["entity_id"]),
        "player_events": player_events,
        "opposition_actions": opposition,
        "pending_intents": pending,
        "fallout_facts": fallout,
        "settled_target_outcomes": outcomes,
        "expected_claims": [],
    }
    payload["expected_claims"] = _derive_expected_claims(payload)
    return validate_narration_truth_contract(
        {**payload, "fingerprint": content_fingerprint(payload)}
    )


def validate_narration_truth_contract(value: object) -> dict[str, Any]:
    """Validate and detach one immutable truth contract."""
    contract = _mapping(value, "narration truth contract")
    _exact_fields(contract, _CONTRACT_FIELDS, "narration truth contract")
    if contract["schema"] != NARRATION_TRUTH_CONTRACT_SCHEMA:
        raise NarrationTruthGateError("unsupported narration truth contract schema")
    turn = contract["turn"]
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise NarrationTruthGateError("contract turn must be a non-negative integer")
    if contract["delivery_mode"] not in {
        "first_delivery",
        "lost_reply_retry",
        "regeneration_retry",
    }:
        raise NarrationTruthGateError("contract delivery mode is unsupported")
    _fp(contract["realization_fingerprint"], "contract realization_fingerprint")
    if contract["construction_provenance_requirement"] != CONSTRUCTION_PROVENANCE_REQUIREMENT:
        raise NarrationTruthGateError("contract loses the upstream construction requirement")
    if (
        not isinstance(contract["realization_forbidden_codes"], list)
        or contract["realization_forbidden_codes"]
        != sorted(set(contract["realization_forbidden_codes"]))
    ):
        raise NarrationTruthGateError("contract forbidden codes are not canonical")
    lifecycle = contract["lifecycle_binding"]
    if lifecycle is not None:
        lifecycle = _mapping(lifecycle, "contract lifecycle_binding")
        _exact_fields(lifecycle, _LIFECYCLE_FIELDS, "contract lifecycle_binding")
        _ref(lifecycle["branch_ref"], "contract lifecycle branch_ref")
        _fp(lifecycle["ledger_fingerprint"], "contract lifecycle ledger_fingerprint")
        _fp(lifecycle["artifact_fingerprint"], "contract lifecycle artifact_fingerprint")
    if contract["visible_surface_profile"] != _visible_surface_profile():
        raise NarrationTruthGateError("contract visible surface profile mismatch")
    if contract["expected_realization_plan"] is not None:
        _validate_visible_plan(
            contract["expected_realization_plan"],
            "contract expected_realization_plan",
        )

    known = [
        _known_row(row, f"contract known_entities[{index}]")
        for index, row in enumerate(contract["known_entities"])
    ]
    if known != sorted(known, key=lambda row: row["entity_id"]):
        raise NarrationTruthGateError("contract known entities are not sorted")
    if len({row["entity_id"] for row in known}) != len(known):
        raise NarrationTruthGateError("contract repeats a known entity")

    if not isinstance(contract["player_events"], list):
        raise NarrationTruthGateError("contract player_events must be a list")
    opposition = [
        _opposition_row(row, f"contract opposition_actions[{index}]")
        for index, row in enumerate(contract["opposition_actions"])
    ]
    pending = [
        _pending_intent_row(row, f"contract pending_intents[{index}]")
        for index, row in enumerate(contract["pending_intents"])
    ]
    fallout = [
        _fallout_row(row, f"contract fallout_facts[{index}]")
        for index, row in enumerate(contract["fallout_facts"])
    ]
    outcomes = [
        _target_outcome_row(row, f"contract settled_target_outcomes[{index}]")
        for index, row in enumerate(contract["settled_target_outcomes"])
    ]
    detached = deepcopy(dict(contract))
    detached["known_entities"] = known
    detached["opposition_actions"] = opposition
    detached["pending_intents"] = pending
    detached["fallout_facts"] = fallout
    detached["settled_target_outcomes"] = outcomes

    expected = detached["expected_claims"]
    if not isinstance(expected, list):
        raise NarrationTruthGateError("contract expected_claims must be a list")
    for index, claim in enumerate(expected):
        row = _mapping(claim, f"expected_claims[{index}]")
        _exact_fields(row, _EXPECTED_CLAIM_FIELDS, f"expected_claims[{index}]")
        _fp(row["claim_ref"], f"expected_claims[{index}].claim_ref")
        _fp(row["construction_ref"], f"expected_claims[{index}].construction_ref")
    if expected != _derive_expected_claims(detached):
        raise NarrationTruthGateError("contract expected graph disagrees with its exact facts")
    if len(pending) > 1 or len({row["pending_ref"] for row in pending}) != len(pending):
        raise NarrationTruthGateError("contract repeats a pending enemy action")
    if {row["intent_ref"] for row in opposition}.intersection(
        row["intent_ref"] for row in pending
    ):
        raise NarrationTruthGateError(
            "contract reuses one intent as settled and pending"
        )

    payload = {key: deepcopy(detached[key]) for key in detached if key != "fingerprint"}
    _fp(detached["fingerprint"], "contract fingerprint")
    if detached["fingerprint"] != content_fingerprint(payload):
        raise NarrationTruthGateError("contract fingerprint mismatch")
    return deepcopy(detached)


def _span_bytes(text: str, start: int, end: int) -> bytes:
    encoded = text.encode("utf-8")
    if start < 0 or end <= start or end > len(encoded):
        raise NarrationTruthGateError("claim span is outside candidate bytes")
    span = encoded[start:end]
    try:
        span.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NarrationTruthGateError("claim span splits a UTF-8 code point") from exc
    return span


def _claim_row(
    raw: object,
    *,
    text: str | None,
    issuer: str,
    channel: str,
    phase: str,
    label: str,
    sealed: bool,
) -> dict[str, Any]:
    row = _mapping(raw, label)
    _exact_fields(row, _CLAIM_FIELDS if sealed else _CLAIM_INPUT_FIELDS, label)
    start = row["span_start"]
    end = row["span_end"]
    clause = row["clause_index"]
    for field, number in (("span_start", start), ("span_end", end), ("clause_index", clause)):
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise NarrationTruthGateError(f"{label}.{field} must be a non-negative integer")
    if end <= start:
        raise NarrationTruthGateError(f"{label} span is empty")
    kind = row["kind"]
    if kind not in CLAIM_KINDS:
        raise NarrationTruthGateError(f"{label}.kind is unsupported")
    polarity = row["polarity"]
    actuality = row["actuality"]
    time_scope = row["time_scope"]
    if polarity not in POLARITIES or actuality not in ACTUALITIES or time_scope not in TIME_SCOPES:
        raise NarrationTruthGateError(f"{label} has unsupported typed truth fields")
    subjects = row["subject_ids"]
    if (
        not isinstance(subjects, list)
        or not subjects
        or subjects != sorted(set(subjects))
    ):
        raise NarrationTruthGateError(f"{label}.subject_ids must be sorted unique references")
    subjects = [_ref(subject, f"{label}.subject_ids") for subject in subjects]
    multiplicity = row["multiplicity"]
    if (
        isinstance(multiplicity, bool)
        or not isinstance(multiplicity, int)
        or multiplicity < len(subjects)
    ):
        raise NarrationTruthGateError(f"{label}.multiplicity is invalid")
    amount = row["amount"]
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise NarrationTruthGateError(f"{label}.amount must be an integer or null")
    payload = {
        "issuer": issuer,
        "channel": channel,
        "phase": phase,
        "span_start": start,
        "span_end": end,
        "clause_index": clause,
        "evidence_fingerprint": (
            raw_fingerprint(_span_bytes(text, start, end))
            if text is not None
            else _fp(row["evidence_fingerprint"], f"{label}.evidence_fingerprint")
        ),
        "occurrence_ref": _ref(row["occurrence_ref"], f"{label}.occurrence_ref", optional=True),
        "cause_ref": _ref(row["cause_ref"], f"{label}.cause_ref", optional=True),
        "authority_ref": _fp(row["authority_ref"], f"{label}.authority_ref", optional=True),
        "construction_ref": _fp(row["construction_ref"], f"{label}.construction_ref"),
        "actor_id": _ref(row["actor_id"], f"{label}.actor_id", optional=True),
        "subject_ids": subjects,
        "kind": kind,
        "polarity": polarity,
        "actuality": actuality,
        "time_scope": time_scope,
        "multiplicity": multiplicity,
        "detail": normalize_phrase(_plain(row["detail"], f"{label}.detail", limit=100)),
        "amount": amount,
    }
    claim_ref = content_fingerprint(payload)
    if sealed:
        if (
            row["issuer"] != issuer
            or row["channel"] != channel
            or row["phase"] != phase
            or row["claim_ref"] != claim_ref
        ):
            raise NarrationTruthGateError(f"{label} provenance or fingerprint mismatch")
    return {**payload, "claim_ref": claim_ref}


def _build_claim_graph(
    candidate_text: str,
    claims: Iterable[Mapping[str, Any]],
    *,
    role: str,
    issuer: str,
    channel: str,
    phase: str,
    status: str,
) -> dict[str, Any]:
    if not isinstance(candidate_text, str):
        raise NarrationTruthGateError("candidate text must be text")
    if role not in GRAPH_ROLES or status not in GRAPH_STATUSES:
        raise NarrationTruthGateError("claim graph role or status is unsupported")
    issuer = _issuer(issuer, "claim graph issuer")
    rows = [
        _claim_row(
            row,
            text=candidate_text,
            issuer=issuer,
            channel=channel,
            phase=phase,
            label=f"claims[{index}]",
            sealed=False,
        )
        for index, row in enumerate(claims)
    ]
    rows.sort(key=lambda row: (row["span_start"], row["span_end"], row["claim_ref"]))
    payload = {
        "schema": NARRATION_CLAIM_GRAPH_SCHEMA,
        "role": role,
        "candidate_fingerprint": _candidate_fingerprint(candidate_text),
        "status": status,
        "issuer": issuer,
        "channel": channel,
        "phase": phase,
        "claims": rows,
    }
    return {**payload, "fingerprint": content_fingerprint(payload)}


def build_producer_claim_graph(
    candidate_text: str,
    claims: Iterable[Mapping[str, Any]],
    *,
    issuer: str = "narrator.producer/1",
    status: str = "complete",
) -> dict[str, Any]:
    """Seal a producer-declared graph; this declaration never certifies itself."""
    return _build_claim_graph(
        candidate_text,
        claims,
        role="producer",
        issuer=issuer,
        channel="candidate_bytes",
        phase="producer_declaration",
        status=status,
    )


def build_verifier_claim_graph(
    candidate_text: str,
    claims: Iterable[Mapping[str, Any]],
    *,
    issuer: str,
    status: str = "complete",
) -> dict[str, Any]:
    """Seal claims supplied by an independent candidate-byte verifier.

    The caller/integration must ensure this issuer did not see the producer declaration.
    """
    if not issuer.startswith("independent."):
        raise NarrationTruthGateError("verifier issuer must be independently namespaced")
    return _build_claim_graph(
        candidate_text,
        claims,
        role="verifier",
        issuer=issuer,
        channel="candidate_bytes",
        phase="independent_verification",
        status=status,
    )


def build_offline_expected_claim_graph(
    candidate_text: str,
    claims: Iterable[Mapping[str, Any]],
    *,
    issuer: str = "offline.human-authored/1",
    status: str = "complete",
) -> dict[str, Any]:
    """Seal an optional independently authored adversarial-test expectation."""
    return _build_claim_graph(
        candidate_text,
        claims,
        role="offline_expected",
        issuer=issuer,
        channel="candidate_bytes",
        phase="offline_expectation",
        status=status,
    )


def validate_narration_claim_graph(
    value: object,
    *,
    expected_role: str | None = None,
    candidate_text: str | None = None,
) -> dict[str, Any]:
    """Validate a sealed typed span graph and, when supplied, its exact candidate bytes."""
    graph = _mapping(value, "narration claim graph")
    _exact_fields(graph, _GRAPH_FIELDS, "narration claim graph")
    if graph["schema"] != NARRATION_CLAIM_GRAPH_SCHEMA:
        raise NarrationTruthGateError("unsupported narration claim graph schema")
    role = graph["role"]
    if role not in GRAPH_ROLES or (expected_role is not None and role != expected_role):
        raise NarrationTruthGateError("narration claim graph role mismatch")
    issuer = _issuer(graph["issuer"], "narration claim graph issuer")
    expected_provenance = {
        "producer": ("candidate_bytes", "producer_declaration"),
        "verifier": ("candidate_bytes", "independent_verification"),
        "offline_expected": ("candidate_bytes", "offline_expectation"),
        "fallback": ("fallback_bytes", "code_fallback"),
    }[role]
    if (graph["channel"], graph["phase"]) != expected_provenance:
        raise NarrationTruthGateError("narration claim graph channel or phase mismatch")
    if role == "verifier" and not issuer.startswith("independent."):
        raise NarrationTruthGateError("verifier graph issuer is not independent")
    if graph["status"] not in GRAPH_STATUSES:
        raise NarrationTruthGateError("narration claim graph status is unsupported")
    candidate_fp = _fp(graph["candidate_fingerprint"], "claim graph candidate_fingerprint")
    if candidate_text is not None and candidate_fp != _candidate_fingerprint(candidate_text):
        raise NarrationTruthGateError("claim graph belongs to different candidate bytes")
    if not isinstance(graph["claims"], list):
        raise NarrationTruthGateError("claim graph claims must be a list")
    claims = [
        _claim_row(
            row,
            text=None,
            issuer=issuer,
            channel=graph["channel"],
            phase=graph["phase"],
            label=f"claim graph claims[{index}]",
            sealed=True,
        )
        for index, row in enumerate(graph["claims"])
    ]
    if claims != sorted(claims, key=lambda row: (row["span_start"], row["span_end"], row["claim_ref"])):
        raise NarrationTruthGateError("claim graph claims are not canonically sorted")
    if candidate_text is not None:
        for index, claim in enumerate(claims):
            observed = raw_fingerprint(
                _span_bytes(candidate_text, claim["span_start"], claim["span_end"])
            )
            if observed != claim["evidence_fingerprint"]:
                raise NarrationTruthGateError(
                    f"claim graph claims[{index}] evidence fingerprint mismatch"
                )
    detached = deepcopy(dict(graph))
    detached["claims"] = claims
    payload = {key: detached[key] for key in detached if key != "fingerprint"}
    _fp(detached["fingerprint"], "claim graph fingerprint")
    if detached["fingerprint"] != content_fingerprint(payload):
        raise NarrationTruthGateError("claim graph fingerprint mismatch")
    return deepcopy(detached)


def _span_projection(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        claim["span_start"],
        claim["span_end"],
        claim["clause_index"],
        claim["evidence_fingerprint"],
        claim["occurrence_ref"],
        claim["cause_ref"],
        claim["authority_ref"],
        claim["actor_id"],
        tuple(claim["subject_ids"]),
        claim["kind"],
        claim["polarity"],
        claim["actuality"],
        claim["time_scope"],
        claim["multiplicity"],
        claim["detail"],
        claim["amount"],
    )


def _ledger_projection(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        claim["occurrence_ref"],
        claim["cause_ref"],
        claim["construction_ref"],
        claim["actor_id"],
        tuple(claim["subject_ids"]),
        claim["kind"],
        claim["polarity"],
        claim["actuality"],
        claim["time_scope"],
        claim["multiplicity"],
        claim["detail"],
        claim["amount"],
    )


def _candidate_ledger_projection(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        claim["occurrence_ref"],
        claim["cause_ref"],
        claim["authority_ref"],
        claim["actor_id"],
        tuple(claim["subject_ids"]),
        claim["kind"],
        claim["polarity"],
        claim["actuality"],
        claim["time_scope"],
        claim["multiplicity"],
        claim["detail"],
        claim["amount"],
    )


def _mask_dialogue(text: str) -> str:
    return re.sub(
        r'"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’',
        lambda match: " " * len(match.group(0)),
        text,
    )


def _byte_offset(text: str, character_offset: int) -> int:
    return len(text[:character_offset].encode("utf-8"))


def _clauses(text: str) -> list[tuple[int, int, int, str]]:
    masked = _mask_dialogue(text)
    rows: list[tuple[int, int, int, str]] = []
    start = 0
    index = 0
    for match in re.finditer(r"[;.!?\r\n]+", masked):
        end = match.end()
        raw = masked[start:end]
        if raw.strip():
            rows.append((index, _byte_offset(masked, start), _byte_offset(masked, end), raw))
            index += 1
        start = end
    if masked[start:].strip():
        rows.append(
            (
                index,
                _byte_offset(masked, start),
                _byte_offset(masked, len(masked)),
                masked[start:],
            )
        )
    return rows


def _entity_mentions(clause: str, contract: Mapping[str, Any]) -> list[tuple[str, int]]:
    normalized = normalize_phrase(clause)
    mentions: list[tuple[str, int]] = []
    for entity in contract["known_entities"]:
        positions = [
            normalized.find(alias)
            for alias in entity["aliases"]
            if alias and normalized.find(alias) >= 0
        ]
        if positions:
            mentions.append((entity["entity_id"], min(positions)))
    return sorted(mentions, key=lambda item: item[1])


def _typed_surface_markers(
    text: str, contract: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    markers: list[dict[str, Any]] = []
    uncertain = False
    for clause_index, start, end, raw in _clauses(text):
        normalized = normalize_phrase(raw)
        mentions = _entity_mentions(raw, contract)
        for kind, pattern in _KIND_PATTERNS.items():
            found = pattern.search(normalized)
            if not found:
                continue
            polarity = "negative" if _NEGATION_RE.search(normalized) else "positive"
            if _HYPOTHETICAL_RE.search(normalized):
                actuality = "hypothetical"
            elif _REPORTED_RE.search(normalized):
                actuality = "reported"
            else:
                actuality = "actual"
            if _FUTURE_RE.search(normalized):
                time_scope = "future"
            elif _PAST_RE.search(normalized):
                time_scope = "past"
            else:
                time_scope = "current"

            if kind in {"time", "world"}:
                subjects = ["world"]
                actor_id = None
            else:
                verb_position = found.start()
                before = [item for item in mentions if item[1] < verb_position]
                after = [item for item in mentions if item[1] >= verb_position]
                pronoun_object = re.search(r"\b(?:them|him|her|it)\b", normalized[found.end() :])
                if after:
                    subjects = [entity_id for entity_id, _ in after]
                    actor_id = before[-1][0] if before else None
                elif before and not pronoun_object:
                    immediate = normalized[:verb_position].split()
                    if immediate and immediate[-1] in _SAFE_NONLIVING:
                        continue
                    subjects = [entity_id for entity_id, _ in before]
                    actor_id = None
                elif pronoun_object:
                    uncertain = True
                    continue
                else:
                    words = normalized[:verb_position].split()
                    if words and words[-1] in _SAFE_NONLIVING:
                        continue
                    uncertain = True
                    continue
            subjects = sorted(set(subjects))
            markers.append(
                {
                    "clause_index": clause_index,
                    "span_start": start,
                    "span_end": end,
                    "kind": kind,
                    "subject_ids": subjects,
                    "actor_id": actor_id,
                    "polarity": polarity,
                    "actuality": actuality,
                    "time_scope": time_scope,
                    "multiplicity": len(subjects),
                }
            )
    return markers, uncertain


def _surface_marker_covered(marker: Mapping[str, Any], claims: Iterable[Mapping[str, Any]]) -> bool:
    matching = [
        claim
        for claim in claims
        if claim["clause_index"] == marker["clause_index"]
        and claim["kind"] == marker["kind"]
        and claim["polarity"] == marker["polarity"]
        and claim["actuality"] == marker["actuality"]
        and claim["time_scope"] == marker["time_scope"]
        and claim["multiplicity"] == marker["multiplicity"]
    ]
    covered = sorted(
        {
            subject
            for claim in matching
            for subject in claim["subject_ids"]
        }
    )
    return covered == marker["subject_ids"]


def _opposition_surface_count(text: str, contract: Mapping[str, Any], action: Mapping[str, Any]) -> int:
    actor_aliases = next(
        row["aliases"]
        for row in contract["known_entities"]
        if row["entity_id"] == action["actor_id"]
    )
    target_aliases = next(
        row["aliases"]
        for row in contract["known_entities"]
        if row["entity_id"] == action["target_id"]
    )
    move_words = {
        word
        for word in normalize_phrase(action["move_label"]).split()
        if len(word) >= 4
    }
    count = 0
    for _index, _start, _end, raw in _clauses(text):
        normalized = normalize_phrase(raw)
        if _NEGATION_RE.search(normalized) or _HYPOTHETICAL_RE.search(normalized):
            continue
        actor = any(alias in normalized for alias in actor_aliases)
        target = any(alias in normalized for alias in target_aliases)
        move = any(word in normalized.split() for word in move_words)
        if action["outcome"] == "miss":
            outcome = bool(_MISS_RE.search(normalized))
        elif action["outcome"] == "blocked":
            outcome = bool(_BLOCK_RE.search(normalized))
        else:
            outcome = bool(_HIT_RE.search(normalized) or _KIND_PATTERNS["harm"].search(normalized))
        if actor and target and (move or outcome) and outcome:
            count += 1
    return count


def _fallback_parts(
    contract: Mapping[str, Any] | None,
) -> tuple[str, list[tuple[str, Mapping[str, Any]]]]:
    if not contract or not contract["expected_claims"]:
        return (
            "The scene holds to what is already established, without adding an unverified outcome.",
            [],
        )
    labels = {row["entity_id"]: row["label"] for row in contract["known_entities"]}
    pending = {row["pending_ref"]: row for row in contract["pending_intents"]}
    parts: list[tuple[str, Mapping[str, Any]]] = []
    for claim in contract["expected_claims"]:
        actor = labels.get(claim["actor_id"], "The settled action") if claim["actor_id"] else None
        subject = labels.get(claim["subject_ids"][0], claim["subject_ids"][0])
        kind = claim["kind"]
        if kind == "pending_intent":
            fact = pending.get(claim["occurrence_ref"])
            if fact is None or claim["cause_ref"] != fact["intent_ref"]:
                raise NarrationTruthGateError(
                    "pending intent fallback lost its exact occurrence binding"
                )
            sentence = fact["visible_text"]
        elif kind == "opposition_action":
            detail_words = claim["detail"].split()
            outcome = detail_words[-1]
            move = " ".join(word.capitalize() for word in detail_words[:-1]) or "settled move"
            if outcome == "miss":
                sentence = f"{actor}'s {move} misses {subject}."
            elif outcome == "blocked":
                sentence = f"{actor}'s {move} is blocked before it affects {subject}."
            else:
                sentence = f"{actor}'s {move} hits {subject}."
        elif kind == "harm":
            if claim["amount"] is None:
                sentence = f"{actor}'s settled action harms {subject}."
            else:
                sentence = (
                    f"{actor}'s settled action deals {-claim['amount']} HP of harm to {subject}."
                )
        elif kind == "defeat":
            sentence = f"{subject} is defeated."
        elif kind == "status":
            sentence = f"{subject} is {claim['detail']}."
        elif kind == "resource":
            sentence = f"{subject}'s {claim['detail']} changes by {claim['amount']}."
        elif kind == "time":
            sentence = f"The {claim['detail']} changes by {claim['amount']}."
        elif kind == "movement":
            sentence = f"{subject} moves to {claim['detail']}."
        else:
            sentence = f"The settled world change is {claim['detail']}."
        parts.append((sentence, claim))
    return " ".join(sentence for sentence, _claim in parts), parts


def _fallback_graph(
    text: str, parts: list[tuple[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    byte_cursor = 0
    for clause_index, (sentence, expected) in enumerate(parts):
        encoded = sentence.encode("utf-8")
        start = byte_cursor
        end = start + len(encoded)
        claims.append(
            {
                "span_start": start,
                "span_end": end,
                "clause_index": clause_index,
                "occurrence_ref": expected["occurrence_ref"],
                "cause_ref": expected["cause_ref"],
                "authority_ref": expected["construction_ref"],
                "construction_ref": content_fingerprint(
                    {"fallback_claim": expected["claim_ref"], "sentence": sentence}
                ),
                "actor_id": expected["actor_id"],
                "subject_ids": expected["subject_ids"],
                "kind": expected["kind"],
                "polarity": expected["polarity"],
                "actuality": expected["actuality"],
                "time_scope": expected["time_scope"],
                "multiplicity": expected["multiplicity"],
                "detail": expected["detail"],
                "amount": expected["amount"],
            }
        )
        byte_cursor = end + (1 if clause_index + 1 < len(parts) else 0)
    return _build_claim_graph(
        text,
        claims,
        role="fallback",
        issuer="aetherstate.truth-gate/1",
        channel="fallback_bytes",
        phase="code_fallback",
        status="complete",
    )


def _violation_for_kind(kind: str) -> str:
    return {
        "harm": "unsupported_named_target_harm",
        "defeat": "unsupported_named_target_defeat",
        "status": "unsupported_named_target_status",
        "resource": "unsupported_named_target_resource",
        "time": "unsupported_time_change",
        "movement": "unsupported_named_target_movement",
        "world": "unsupported_world_change",
        "opposition_action": "unsupported_opposition_action",
        "pending_intent": "unsupported_pending_intent",
    }[kind]


def _ordered_violations(codes: Iterable[str]) -> list[str]:
    return sorted(set(codes), key=lambda code: (_VIOLATION_ORDER.get(code, 999), code))


def assess_narration(
    candidate_text: object,
    contract: object,
    *,
    producer_graph: object | None = None,
    verifier_graph: object | None = None,
    offline_expected_graph: object | None = None,
    offline_test_mode: bool = False,
) -> NarrationTruthDecision:
    """Assess one complete candidate before any bytes become visible.

    This is a pure decision.  A later integration must buffer the complete reply, obtain the blind
    verifier graph, and atomically persist attempt/decision/visibility state.  This function does
    not claim to provide those lifecycle guarantees.
    """
    violations: list[str] = []
    valid_contract: dict[str, Any] | None = None
    try:
        valid_contract = validate_narration_truth_contract(contract)
    except (NarrationTruthGateError, TypeError, ValueError):
        violations.append("malformed_contract")

    text = candidate_text if isinstance(candidate_text, str) else ""
    if not isinstance(candidate_text, str) or not candidate_text.strip():
        violations.append("malformed_reply")
    candidate_fp = _candidate_fingerprint(text)
    _surface_safe, surface_codes = _safe_visible_text(text)
    violations.extend(surface_codes)

    fallback_text, fallback_parts = _fallback_parts(valid_contract)
    fallback_graph = _fallback_graph(fallback_text, fallback_parts)
    fallback_plan = _visible_plan(fallback_text)
    if valid_contract:
        if valid_contract["lifecycle_binding"] is None:
            violations.append("missing_lifecycle_binding")
        expected_plan = valid_contract["expected_realization_plan"]
        if expected_plan is None:
            if not (offline_test_mode and offline_expected_graph is not None):
                violations.append("missing_code_authored_realization_plan")
        elif (
            expected_plan["wire_fingerprint"] != candidate_fp
            or expected_plan["visible_text_fingerprint"] != candidate_fp
            or expected_plan["visible_glyph_fingerprint"]
            != _visible_glyph_fingerprint(text)
        ):
            violations.append("candidate_realization_plan_mismatch")

    graphs: dict[str, dict[str, Any] | None] = {
        "producer": None,
        "verifier": None,
        "offline_expected": None,
    }
    for role, supplied, missing_code, malformed_code, uncertain_code in (
        (
            "producer",
            producer_graph,
            "missing_producer_graph",
            "malformed_producer_graph",
            "uncertain_producer_graph",
        ),
        (
            "verifier",
            verifier_graph,
            "missing_verifier_graph",
            "malformed_verifier_graph",
            "uncertain_verifier_graph",
        ),
    ):
        if supplied is None:
            violations.append(missing_code)
            continue
        try:
            graph = validate_narration_claim_graph(
                supplied, expected_role=role, candidate_text=text
            )
            graphs[role] = graph
            if graph["status"] != "complete":
                violations.append(uncertain_code)
        except (NarrationTruthGateError, TypeError, ValueError):
            violations.append(malformed_code)

    if offline_expected_graph is not None:
        try:
            graph = validate_narration_claim_graph(
                offline_expected_graph,
                expected_role="offline_expected",
                candidate_text=text,
            )
            graphs["offline_expected"] = graph
            if graph["status"] != "complete":
                violations.append("uncertain_offline_expected_graph")
        except (NarrationTruthGateError, TypeError, ValueError):
            violations.append("malformed_offline_expected_graph")

    producer = graphs["producer"]
    verifier = graphs["verifier"]
    offline = graphs["offline_expected"]
    if producer and verifier:
        producer_projection = Counter(_span_projection(row) for row in producer["claims"])
        verifier_projection = Counter(_span_projection(row) for row in verifier["claims"])
        if producer_projection != verifier_projection:
            violations.append("producer_verifier_graph_disagreement")
        if offline:
            offline_projection = Counter(_span_projection(row) for row in offline["claims"])
            if offline_projection != verifier_projection:
                violations.append("offline_graph_disagreement")

    if valid_contract and verifier:
        known_ids = {row["entity_id"] for row in valid_contract["known_entities"]}
        for claim in verifier["claims"]:
            if (
                claim["polarity"] == "uncertain"
                or claim["actuality"] == "uncertain"
                or claim["time_scope"] == "uncertain"
                or any(subject not in known_ids for subject in claim["subject_ids"])
            ):
                violations.append("claim_extraction_uncertain")

        actual_claims = [
            row
            for row in verifier["claims"]
            if row["polarity"] == "positive"
            and row["actuality"] == "actual"
            and row["time_scope"] in {"current", "future"}
        ]
        expected_counter = Counter(
            _ledger_projection(row) for row in valid_contract["expected_claims"]
        )
        observed_counter = Counter(_candidate_ledger_projection(row) for row in actual_claims)
        missing = expected_counter - observed_counter
        extra = observed_counter - expected_counter
        if missing:
            violations.append("expected_claim_omitted")
            expected_by_projection = {
                _ledger_projection(row): row for row in valid_contract["expected_claims"]
            }
            if any(
                expected_by_projection[key]["kind"] == "opposition_action"
                for key in missing
            ):
                violations.append("opposition_action_omitted")
        for key, count in extra.items():
            kind = key[5]
            violations.append(_violation_for_kind(kind))
            if kind == "opposition_action" and count > 0:
                violations.append("opposition_action_misattributed")
        for action in valid_contract["opposition_actions"]:
            count = _opposition_surface_count(text, valid_contract, action)
            if count == 0:
                violations.append("opposition_action_omitted")
            elif count > 1:
                violations.append("autonomous_intent_repeated")

        markers, uncertain = _typed_surface_markers(text, valid_contract)
        if uncertain:
            violations.append("claim_extraction_uncertain")
        for marker in markers:
            if not _surface_marker_covered(marker, verifier["claims"]):
                violations.append("verifier_graph_omits_surface_claim")

    if _MECHANIC_TAG_RE.search(text):
        violations.append("mechanic_tag")
    if _MASS_RE.search(text):
        violations.append("mass_or_aggregate_casualty")

    if valid_contract:
        fallback_counter = Counter(
            _candidate_ledger_projection(row) for row in fallback_graph["claims"]
        )
        ledger_counter = Counter(
            _ledger_projection(row) for row in valid_contract["expected_claims"]
        )
        if fallback_counter != ledger_counter:
            violations.append("malformed_contract")

    ordered = _ordered_violations(violations)
    allowed = not ordered
    visible_text = text if allowed else fallback_text
    visible_graph = producer if allowed and producer else fallback_graph

    producer_fp = producer["fingerprint"] if producer else None
    verifier_fp = verifier["fingerprint"] if verifier else None
    offline_fp = offline["fingerprint"] if offline else None
    contract_fp = valid_contract["fingerprint"] if valid_contract else None
    lifecycle = valid_contract["lifecycle_binding"] if valid_contract else None
    attempt_fp = content_fingerprint(
        {
            "contract_fingerprint": contract_fp,
            "candidate_fingerprint": candidate_fp,
            "producer_graph_fingerprint": producer_fp,
            "verifier_graph_fingerprint": verifier_fp,
            "offline_expected_graph_fingerprint": offline_fp,
        }
    )
    receipt_payload = {
        "schema": NARRATION_TRUTH_DECISION_SCHEMA,
        "verdict": "allow" if allowed else "fallback",
        "integration_scope": "pure_pre_display_no_streaming_or_cas",
        "evaluation_mode": "offline_test" if offline_test_mode else "runtime",
        "turn": valid_contract["turn"] if valid_contract else None,
        "branch_fingerprint": (
            content_fingerprint({"branch_ref": lifecycle["branch_ref"]})
            if lifecycle
            else None
        ),
        "ledger_fingerprint": lifecycle["ledger_fingerprint"] if lifecycle else None,
        "artifact_fingerprint": lifecycle["artifact_fingerprint"] if lifecycle else None,
        "contract_fingerprint": contract_fp,
        "candidate_fingerprint": candidate_fp,
        "candidate_wire_fingerprint": candidate_fp,
        "candidate_content_type": VISIBLE_CONTENT_TYPE,
        "renderer_version": VISIBLE_RENDERER_VERSION,
        "renderer_config_fingerprint": VISIBLE_RENDERER_CONFIG_FINGERPRINT,
        "candidate_visible_glyph_fingerprint": _visible_glyph_fingerprint(text),
        "producer_graph_fingerprint": producer_fp,
        "verifier_graph_fingerprint": verifier_fp,
        "offline_expected_graph_fingerprint": offline_fp,
        "attempt_fingerprint": attempt_fp,
        "fallback_fingerprint": _candidate_fingerprint(fallback_text),
        "fallback_wire_fingerprint": fallback_plan["wire_fingerprint"],
        "fallback_visible_glyph_fingerprint": fallback_plan[
            "visible_glyph_fingerprint"
        ],
        "fallback_graph_fingerprint": fallback_graph["fingerprint"],
        "visible_text_fingerprint": _candidate_fingerprint(visible_text),
        "visible_graph_fingerprint": visible_graph["fingerprint"],
        "expected_claim_count": (
            len(valid_contract["expected_claims"]) if valid_contract else 0
        ),
        "candidate_claim_count": len(verifier["claims"]) if verifier else 0,
        "violation_codes": ordered,
    }
    receipt = {
        **receipt_payload,
        "fingerprint": content_fingerprint(receipt_payload),
    }
    return NarrationTruthDecision(
        visible_text=visible_text,
        visible_claim_graph=deepcopy(visible_graph),
        receipt=receipt,
    )
