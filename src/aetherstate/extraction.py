"""Tier-1 extraction: probe protocol, capability cache/demotion, rung 1-4 ladder, parse/repair.

Sources: 06 A.1-A.4 (rungs, probe, cache/demotion, Venice caveats), 03 SS5 (ladder walk-down,
ONE repair pass per rung, per-op salvage), 04 (prompts via prompts.py), 02 SS11 (StateDelta).

Invariant 3 everywhere: a rung failure never fails the turn — retry down-ladder; total failure
marks the batch failed and the previous state stands. Rung 1 (native grammar/schema fields)
only ever fires at fingerprint-verified LOCAL engines, never blind at hosted APIs (06 A.2 P1).

Thinking models (live eval #1 + design decision 2026-07-03): reasoning is a config
TRADEOFF, not one-size — extraction.thinking = auto|on|off (auto = on iff the model is
detected thinking-capable, e.g. Venice GLM-4.7-flash/GLM-5.2; most locals are not).
When thinking is active, max_tokens uses extraction.thinking_max_tokens so reasoning +
output both fit; when off, Venice gets disable_thinking. PROBES always disable thinking
(a capability check must stay cheap and deterministic). include_venice_system_prompt is
false at venice.ai hosts regardless — a ~1600-token vendor prompt is pure waste here.

Transient vs capability failures: HTTP 429/5xx are NOT validation failures — they never
count toward 06 A.2 demotion strikes, are retried with backoff in _post, and if persistent
abort the ladder (the batch re-runs later; walking down rungs would just hammer the limiter).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from . import prompts
from .state import _SPEC as OP_SPEC          # required-field sets (02 SS11)
from .state import OP_FIELD_ENUMS            # per-op vocabularies (single source of truth)

log = logging.getLogger("aetherstate.extraction")

DELTA_SCHEMA_ID = "aetherstate/delta/2"
LEGACY_DELTA_SCHEMA_ID = "aetherstate/delta/1"
ACCEPTED_DELTA_SCHEMA_IDS = frozenset({DELTA_SCHEMA_ID, LEGACY_DELTA_SCHEMA_ID})


class StateDelta(BaseModel):
    """02 SS11 envelope — deliberately loose here; per-op validation salvages downstream."""
    schema_: str = Field(default=DELTA_SCHEMA_ID, alias="schema")
    turn_range: list = Field(default_factory=lambda: [0, 0])
    ops: list = Field(default_factory=list)

    model_config = dict(populate_by_name=True, extra="ignore")


# ---- rung-2 strict JSON schema (06 A.4: Venice requires ALL fields required; nullable via
# type arrays; ONE stable schema so hosted compilers cache it) -----------------------------
_OP_FIELDS: dict[str, list[str]] = {
    "op": ["string"], "entity": ["string", "null"], "key": ["string", "null"],
    "value": ["string", "number", "boolean", "null"], "to_location": ["string", "null"],
    "present": ["boolean", "null"], "char": ["string", "null"], "item": ["string", "null"],
    "action": ["string", "null"], "moved_to": ["string", "null"],
    "participants": ["array", "null"], "base": ["string", "null"], "anchor": ["string", "null"],
    "detail": ["string", "null"], "from_char": ["string", "null"], "from_part": ["string", "null"],
    "to_char": ["string", "null"], "to_part": ["string", "null"], "type": ["string", "null"],
    "intensity": ["integer", "null"], "object": ["string", "null"],
    "delta": ["integer", "null"], "set": ["integer", "null"], "valence": ["integer", "null"],
    "energy": ["integer", "null"], "dominance": ["integer", "null"],
    "category": ["string", "null"], "signal": ["string", "null"],
    "max_intensity": ["integer", "null"], "dimension": ["string", "null"],
    "reason": ["string", "null"], "learner": ["string", "null"], "holder": ["string", "null"],
    "statement": ["string", "null"], "stance": ["string", "null"],
    "evidence_source": ["string", "null"], "source": ["string", "null"],
    "teller": ["string", "null"], "text": ["string", "null"],
    "importance": ["integer", "null"], "tags": ["array", "null"], "goal": ["string", "null"],
    "minutes": ["integer", "null"], "to_time_of_day": ["string", "null"],
    "target_kind": ["string", "null"], "target": ["string", "null"], "flavor": ["string", "null"],
    "behavior_note": ["string", "null"], "substance": ["string", "null"],
}


# Q17: the 16 extraction-facing op kinds (02 SS11; engine-internal ops are NEVER offered
# to the model — authority quarantines them anyway). SORTED: one stable schema string,
# hosted compilers cache it once (06 A.4).
EXTRACTION_OPS = sorted((
    "set_attribute", "move_entity", "presence", "clothing", "position", "contact",
    "arousal", "mood", "consent_signal", "relationship_adj", "belief_acquire",
    "memory_event", "goal", "time_advance", "obsession", "craving"))

# RPG-2 (doc 07 §4.1): the five PROPOSABLE item ops. Offered to the model ONLY when
# specialization=rpg — a `none` session's wire schema + OP CARD stay byte-identical to 1.0
# (the RPG invariant), and each variant is its own stable string so hosted compilers cache
# both once. check / item_mint / set_* are privileged and NEVER appear on the wire.
RPG_ITEM_OPS = ("item_consume", "item_equip", "item_move", "item_transfer", "item_unequip")
# RPG-3 (doc 05 §5.4): the three PROPOSABLE effect ops — same rpg-only wire discipline.
# check / item_mint / ability_grant / set_* are privileged and NEVER appear on the wire.
RPG_EFFECT_OPS = ("effect_add", "effect_remove", "effect_update")
# RPG-3b (doc 07 §7.7): the two PROPOSABLE social ops — same rpg-only wire discipline.
# Affinity `kind` is DERIVED engine-side (from the target entity's kind) and never rides
# the wire; set_soulmate/set_nemesis are privileged and NEVER appear on the wire.
RPG_SOCIAL_OPS = ("affinity_adj", "world_flag")
# RPG-5 (playtest 2026-07-06 G2/G3/G7): the recording-gap ops — organic item channel
# (mechanics-free unless a registry template grounds them at _enrich), the quest ledger,
# and the clamped HP consequence channel. Proposable; same rpg-only wire discipline.
# award_exp / level_up / master_tick / evolve_def / defeat_resolve are privileged and
# NEVER appear on the wire — progression is code-awarded (doc 10).
RPG_GAP_OPS = ("hp_adj", "item_gain", "item_lose", "quest_add", "quest_update")
# Phase 1 (plan doc 13): the NPC-vs-NPC clash RECORD — the extraction-ladder floor under
# the [clash] tag ceiling. Proposable; unknown participants quarantine (real rows only).
# combatant_spawn/defeat/combat_end/loot_table are privileged and NEVER on the wire;
# combatant_hp is deliberately absent too — live harm rides the [hp] tag channel (batch
# extraction lags the fight and would land on rows that already left the field).
RPG_COMBAT_OPS = ("clash_record",)
EXTRACTION_OPS_RPG = sorted(EXTRACTION_OPS + list(RPG_ITEM_OPS) + list(RPG_EFFECT_OPS)
                            + list(RPG_SOCIAL_OPS) + list(RPG_GAP_OPS)
                            + list(RPG_COMBAT_OPS))
_RPG_OP_FIELDS: dict[str, list[str]] = {
    "instance": ["string", "null"], "to": ["string", "null"], "slot": ["string", "null"],
    "to_owner": ["string", "null"], "amount": ["integer", "null"], "swap": ["boolean", "null"],
    # RPG-3 effect fields. NOTE: "valence" collides with mood's integer field — the flat rpg
    # schema (all fields shared) must admit BOTH, so it widens to integer|string there; the
    # anyOf builder keeps mood integer-only and effect branches string-only (see below), and
    # validate_op quarantines a non-vocabulary effect valence either way.
    "effect": ["string", "null"], "kind": ["string", "null"],
    "valence": ["integer", "string", "null"], "note": ["string", "null"],
    "duration": ["integer", "null"], "stacks": ["integer", "null"],
    # RPG-3b social fields. `target`/`delta`/`reason`/`key`/`value` reuse base _OP_FIELDS
    # types; only world_flag's optional faction scope is new.
    "faction": ["string", "null"],
    # RPG-5 recording-gap fields (item_gain/item_lose/quest_add/quest_update; hp_adj
    # reuses char/delta/reason). `name` is new to the wire; `note` reuses the effect field.
    "name": ["string", "null"], "qty": ["integer", "null"], "detail": ["string", "null"],
    "giver": ["string", "null"], "stakes": ["string", "null"], "quest": ["string", "null"],
    "status": ["string", "null"],
    # Phase 1 clash fields (clash_record): the two participants + how/what changed.
    "a": ["string", "null"], "b": ["string", "null"], "method": ["string", "null"],
    "outcome": ["string", "null"],
}
# RPG-3 branch-level vocabularies/types (anyOf rung only; the flat schema can't carry them
# without disturbing mood — the OP CARD + apply-side validation stay load-bearing there).
_RPG_FIELD_ENUMS: dict[str, dict[str, list]] = {
    "effect_add": {"kind": ["condition", "status"],
                   "valence": ["negative", "neutral", "positive"]},
    "effect_update": {"valence": ["negative", "neutral", "positive"]},
}
_RPG_BRANCH_TYPES: dict[str, list[str]] = {"valence": ["string", "null"]}
_EXTRACTION_FIELD_ENUMS: dict[str, dict[str, list]] = {
    "belief_acquire": {
        "stance": ["believes", "disputes", "doubts", "knows", "rumor", "uncertain"],
        "evidence_source": ["inferred", "overheard", "told", "witnessed"],
    },
}


def _field_enums(kind: str) -> dict[str, list]:
    """Wire-only vocabularies layered over the reducer's shared enum table."""
    return {**OP_FIELD_ENUMS.get(kind, {}), **_EXTRACTION_FIELD_ENUMS.get(kind, {})}

# closed vocabularies — derived from state.OP_FIELD_ENUMS (single source of truth).
# Per-FIELD unions for the flat schema: multi-contributor fields (action) get a sorted
# union; single-contributor fields keep their list VERBATIM (to_time_of_day stays in
# TIMES chronological order) — the flat schema string is byte-identical to the pre-table
# literal, so hosted compiler caches are undisturbed (06 A.4).
def _derive_flat_enums() -> dict[str, list]:
    by_field: dict[str, list[list]] = {}
    for kind in sorted(set(OP_FIELD_ENUMS) | set(_EXTRACTION_FIELD_ENUMS)):
        for f, vals in _field_enums(kind).items():
            by_field.setdefault(f, []).append(list(vals))
    out: dict[str, list] = {"op": EXTRACTION_OPS}
    for f in sorted(by_field):
        lists = by_field[f]
        out[f] = lists[0] if len(lists) == 1 else sorted({v for lst in lists for v in lst})
    return out


_OP_ENUMS: dict[str, list] = _derive_flat_enums()


def delta_json_schema(rpg: bool = False) -> dict:
    op_fields = {**_OP_FIELDS, **_RPG_OP_FIELDS} if rpg else _OP_FIELDS
    op_props: dict = {}
    for k, v in op_fields.items():
        prop: dict = {"type": v}
        if k in _OP_ENUMS:
            enum = (list(EXTRACTION_OPS_RPG) if rpg else list(_OP_ENUMS[k])) if k == "op" \
                else list(_OP_ENUMS[k])
            if "null" in v:              # nullable enum fields must admit null explicitly
                enum = enum + [None]
            prop["enum"] = enum
        elif rpg and k == "kind":        # RPG-3: single-contributor field -> safe flat enum
            prop["enum"] = ["condition", "status", None]
        op_props[k] = prop
    return {
        "name": "aetherstate_delta_rpg" if rpg else "aetherstate_delta",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "schema": {"type": "string"},
                "turn_range": {"type": "array", "items": {"type": "integer"}},
                "ops": {"type": "array", "items": {
                    "type": "object", "properties": op_props,
                    "required": list(op_fields), "additionalProperties": False}},
            },
            "required": ["schema", "turn_range", "ops"],
            "additionalProperties": False,
        },
    }


def delta_json_schema_anyof(rpg: bool = False) -> dict:
    """Q18 addendum: per-op-type branches — a branch only HAS its own fields, so filling
    another op's fields is structurally impossible (mega-op filler dies at the schema
    level; the token win goes to budget/thinking-off tiers, Q16). Venice strict verified
    2026-07-03 (probe_anyof.py) to ACCEPT anyOf and ENFORCE branch fields — but NOT enum
    values (E10 "pin"), so the OP CARD and apply-side validation stay load-bearing.
    Used at RUNG 2 ONLY, capability-gated per endpoint (caps.anyof, probed alongside
    P2/P3), flat-schema fallback everywhere else. Wire surface matches the flat schema:
    branches carry _OP_ALLOWED ∩ _OP_FIELDS (apply-side optionals covers/is_secret/
    calendar_note stay off the wire). Branch enums are PER-OP (goal.action is just
    add|complete|abandon here, not the flat union) — tighter vocabulary at zero tokens."""
    op_fields = {**_OP_FIELDS, **_RPG_OP_FIELDS} if rpg else _OP_FIELDS
    branches = []
    for kind in (EXTRACTION_OPS_RPG if rpg else EXTRACTION_OPS):
        fields = sorted(_OP_ALLOWED[kind] & set(op_fields))
        props: dict = {"op": {"type": "string", "enum": [kind]}}
        for f in fields:
            # Branch types are PER-OP (RPG-3): a base op keeps its base type even under rpg
            # (mood.valence stays integer), and an effect branch narrows shared names to its
            # own type (effect valence is string-only) — the flat union never leaks in here.
            if kind in EXTRACTION_OPS and f in _OP_FIELDS:
                ftype = _OP_FIELDS[f]
            elif kind in RPG_EFFECT_OPS and f in _RPG_BRANCH_TYPES:
                ftype = _RPG_BRANCH_TYPES[f]
            else:
                ftype = op_fields[f]
            prop: dict = {"type": ftype}
            enum = _field_enums(kind).get(f)
            if enum is None and rpg:
                enum = _RPG_FIELD_ENUMS.get(kind, {}).get(f)
            if enum is not None:
                prop["enum"] = (list(enum) + [None]) if "null" in ftype else list(enum)
            props[f] = prop
        branches.append({"type": "object", "properties": props,
                         "required": ["op"] + fields, "additionalProperties": False})
    return {
        "name": "aetherstate_delta_v2_rpg" if rpg else "aetherstate_delta_v2",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "schema": {"type": "string"},
                "turn_range": {"type": "array", "items": {"type": "integer"}},
                "ops": {"type": "array", "items": {"anyOf": branches}},
            },
            "required": ["schema", "turn_range", "ops"],
            "additionalProperties": False,
        },
    }


# ---- parse + repair (03 SS5 parse_and_validate) -------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)")


def strip_fences_and_prose(text: str) -> str:
    text = _FENCE.sub("", text.strip())
    start = text.find("{")
    if start < 0:
        return text
    return text[start:_last_balanced(text, start)]


def _last_balanced(text: str, start: int) -> int:
    """End index of the outermost object, tolerating a truncated tail (repair closes it)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def repair_json(text: str) -> str:
    """03 SS5: brace balance, trailing commas, single->double quotes, unquoted keys,
    truncated-tail closure. Cheap heuristics — the validator remains the judge."""
    t = _TRAILING_COMMA.sub(r"\1", text)
    t = _UNQUOTED_KEY.sub(r'\1"\2"\3', t)
    # truncated tail: close open strings/brackets in nesting order
    depth_stack, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth_stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and depth_stack:
            depth_stack.pop()
    if in_str:
        t += '"'
    t = re.sub(r",\s*$", "", t)
    t += "".join(reversed(depth_stack))
    return t


# Q18: per-op allowed fields (02 SS11 / OP CARD; includes apply-side optionals that the
# flat wire schema doesn't carry: covers, is_secret, calendar_note). The mega-op defense —
# Venice strict all-fields-required + enums nudges models to FILL every field with a valid
# value instead of null; one emitted op arrives wearing ten ops' fields. Scrubbing keeps
# ONLY the op kind's own fields: the journal stays clean and spurious values can never
# leak into state or matchers. Splitting a merged op back apart is deliberately NOT
# attempted (filler vs real embedded change is not deterministically distinguishable) —
# the prompt rule makes the model emit separate ops; the scrub guarantees hygiene.
_OP_ALLOWED: dict[str, set[str]] = {
    "set_attribute": {"entity", "key", "value"},
    "move_entity": {"entity", "to_location"},
    "presence": {"entity", "present"},
    "clothing": {"char", "item", "action", "moved_to", "covers"},
    "position": {"participants", "base", "anchor", "detail"},
    "contact": {"action", "from_char", "from_part", "to_char", "to_part", "type",
                "intensity", "object"},
    "arousal": {"char", "delta", "set"},
    "mood": {"char", "valence", "energy", "dominance"},
    "consent_signal": {"from_char", "to_char", "category", "signal", "max_intensity"},
    "relationship_adj": {"from_char", "to_char", "dimension", "delta", "reason"},
    "belief_acquire": {"holder", "statement", "stance", "evidence_source", "teller"},
    "memory_event": {"text", "participants", "importance", "tags"},
    "goal": {"char", "action", "text"},
    "time_advance": {"minutes", "to_time_of_day", "calendar_note"},
    "obsession": {"char", "target_kind", "target", "delta", "set", "flavor",
                  "behavior_note"},
    "craving": {"char", "substance", "action", "delta"},
    # RPG-2 proposable item ops (doc 07 §4.1) — scrub rows; on the wire only under rpg
    "item_move": {"instance", "to"},
    "item_equip": {"instance", "slot", "swap"},
    "item_unequip": {"instance", "to"},
    "item_consume": {"instance", "amount"},
    "item_transfer": {"instance", "to_owner", "to"},
    # RPG-3 proposable effect ops (doc 05 §5.4) — scrub rows; on the wire only under rpg.
    # `mods` is deliberately ABSENT: mechanics come from the preset bake, never the model.
    "effect_add": {"char", "effect", "kind", "valence", "note", "duration", "stacks"},
    "effect_remove": {"char", "effect"},
    "effect_update": {"char", "effect", "valence", "note", "duration", "stacks"},
    # RPG-3b proposable social ops (doc 07 §7.7) — scrub rows; on the wire only under rpg.
    # affinity `kind` is deliberately ABSENT (derived from the target entity — the model
    # never types records); the bond ops are absent entirely (privileged).
    "affinity_adj": {"target", "delta", "reason"},
    "world_flag": {"key", "value", "faction"},
    # RPG-5 recording-gap ops (playtest G2/G3/G7) — scrub rows; on the wire only under rpg.
    "item_gain": {"char", "name", "qty"},
    "item_lose": {"char", "name"},
    "quest_add": {"name", "detail", "giver", "stakes"},
    "quest_update": {"quest", "status", "note"},
    "hp_adj": {"char", "delta", "reason"},
    # Phase 1 clash record — scrub row; on the wire only under rpg (plan doc 13).
    "clash_record": {"a", "b", "method", "outcome"},
}


def scrub_op(op: dict) -> dict:
    """Keep only the op kind's own fields. Unknown kinds pass through untouched —
    downstream validation quarantines them with a visible reason (03 SS5.1)."""
    allowed = _OP_ALLOWED.get(op.get("op"))
    if allowed is None:
        return op
    dropped = set(op) - allowed - {"op"}
    if dropped:
        log.debug("scrubbed %d foreign fields off %s: %s",
                  len(dropped), op.get("op"), sorted(dropped))
    return {k: v for k, v in op.items() if k == "op" or k in allowed}


def enum_salvage(op: dict) -> dict:
    """Q18 addendum: providers enforce schema STRUCTURE, not enum VALUES (E10 "pin"
    passed rung-2 strict in both modes). REQUIRED-field violations are left intact —
    state.validate_op quarantines them visibly at apply (state.py authority choke point;
    never silently applied). OPTIONAL enum fields carrying out-of-vocabulary values are
    dropped here instead, so one bad subfield doesn't cost the whole op at apply
    (03 SS5.1 per-op salvage; e.g. time_advance keeps its minutes when to_time_of_day
    arrives as "midnight")."""
    kind = op.get("op")
    enums = _field_enums(str(kind))
    if not enums:
        return op
    required = OP_SPEC.get(kind, set())
    for f, vocab in enums.items():
        if f in op and f not in required and op[f] is not None and op[f] not in vocab:
            log.debug("dropped out-of-vocab optional %s.%s=%r", kind, f, op[f])
            op = {k: v for k, v in op.items() if k != f}
    return op


def parse_and_validate(text: str) -> Optional[StateDelta]:
    """Returns a StateDelta (possibly ops=[]) or None. Empty ops is a VALID success (Shot C)."""
    if not text:
        return None
    core = strip_fences_and_prose(text)
    doc: Any = None
    for candidate in (core, repair_json(core), repair_json(core).replace("'", '"')):
        try:
            doc = json.loads(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(doc, dict):
        return None
    try:
        delta = StateDelta.model_validate(doc)
    except Exception:
        return None
    if delta.schema_ not in ACCEPTED_DELTA_SCHEMA_IDS:
        return None
    # rung-2 strict schema pads every field: drop the null padding, then scrub fields
    # that don't belong to the op kind (Q18 mega-op defense)
    normalized: list[dict] = []
    for raw_op in delta.ops:
        if not isinstance(raw_op, dict):
            continue
        op = {k: v for k, v in raw_op.items() if v is not None}
        if op.get("op") == "reveal_fact":
            # V1 was a cached extraction proposal, not replay authority. Preserve only its
            # actor-specific epistemic meaning; never mint objective facthood from model prose.
            if delta.schema_ != LEGACY_DELTA_SCHEMA_ID:
                continue
            holder = str(op.get("learner") or "").strip()
            statement = str(op.get("statement") or "").strip()
            evidence_source = str(op.get("source") or "").strip()
            if not holder or not statement or evidence_source not in {
                "inferred", "overheard", "told", "witnessed",
            }:
                continue
            translated = {
                "op": "belief_acquire",
                "holder": holder,
                "statement": statement,
                "stance": "believes",
                "evidence_source": evidence_source,
            }
            teller = str(op.get("teller") or "").strip()
            if teller:
                translated["teller"] = teller
            normalized.append(translated)
            continue
        normalized.append(enum_salvage(scrub_op(op)))
    delta.schema_ = DELTA_SCHEMA_ID
    delta.ops = normalized
    return delta


# ---- rung-1 native structured output (06 A.2 P1, A.3) --------------------------------------
# Generic JSON GBNF (llama.cpp json.gbnf shape). Rung 1's guarantee is token-level JSON
# validity (06 A.1); field-level checking stays with the per-op validator (03 SS5) — so one
# stable grammar serves every engine that takes raw GBNF.
GBNF_JSON = r"""root ::= object
value ::= object | array | string | number | ("true" | "false" | "null") ws
object ::= "{" ws ( string ":" ws value ("," ws string ":" ws value)* )? "}" ws
array ::= "[" ws ( value ("," ws value)* )? "]" ws
string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" (["\\bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]) )* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws
ws ::= [ \t\n]*"""

# engine -> (request body field, payload kind). Engines absent here (ollama /v1, lmstudio,
# tabby) have no native OAI-body field per 06 A.3 — the P2/P3 probes cover them.
_NATIVE: dict[str, tuple[str, str]] = {
    "llamacpp": ("json_schema", "schema"),     # llama.cpp converts schema -> GBNF internally
    "vllm": ("guided_json", "schema"),
    "koboldcpp": ("grammar", "gbnf"),
    "ooba": ("grammar_string", "gbnf"),
}

# substring tells searched in GET /models body + response headers (lowercased)
_ENGINE_TELLS: tuple[tuple[str, str], ...] = (
    ("llama.cpp", "llamacpp"), ("llamacpp", "llamacpp"), ("llama-cpp", "llamacpp"),
    ("koboldcpp", "koboldcpp"), ("vllm", "vllm"), ("tabby", "tabby"),
    ("text-generation-webui", "ooba"), ("oobabooga", "ooba"),
    ("lmstudio", "lmstudio"), ("lm studio", "lmstudio"), ("ollama", "ollama"),
)
_PORT_HINTS = {11434: "ollama", 1234: "lmstudio", 5001: "koboldcpp"}


def is_local_host(base_url: str) -> bool:
    """Loopback / RFC1918 / .local|.lan hosts — the only endpoints P1 may ever probe."""
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def is_venice_host(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "venice.ai" or host.endswith(".venice.ai")


# model-id tells for thinking/reasoning capability (auto mode; conservative list)
_THINKING_TELLS = ("glm-4.7", "glm-5", "deepseek-r1", "qwq", "reasoner", "thinking")


def thinking_supported(model: str) -> bool:
    m = (model or "").lower()
    return any(t in m for t in _THINKING_TELLS)


def thinking_active(cfg, ep: "Endpoint") -> bool:
    """extraction.thinking: on | off | auto (= on iff the model is thinking-capable)."""
    mode = cfg.extraction.thinking
    if mode == "on":
        return True
    if mode == "off":
        return False
    return thinking_supported(ep.model)


def _vendor_params(body: dict, ep: "Endpoint", thinking: bool) -> dict:
    """Venice: reasoning follows the thinking mode; the ~1600-token Venice system prompt
    is ALWAYS excluded (pure budget waste for extraction). Other hosts: no vendor block —
    thinking control there is model/server-side; we only size max_tokens for it."""
    if is_venice_host(ep.base_url):
        body["venice_parameters"] = {"disable_thinking": not thinking,
                                     "include_venice_system_prompt": False}
    return body


# ---- probe protocol + capability cache (06 A.2) --------------------------------------------
_PROBE_SCHEMA = {"name": "probe", "strict": True,
                 "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"], "additionalProperties": False}}
_PROBE_MSGS = [{"role": "user", "content": 'Reply with JSON: {"ok": true}'}]

# Q18 addendum: tiny two-branch anyOf probe (same shape evals/probe_anyof.py verified
# live). Verdict is cached in caps.anyof: 1 accepted / 0 rejected / -1 unprobed.
_ANYOF_PROBE = {"name": "anyof_probe", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["ops"],
    "properties": {"ops": {"type": "array", "items": {"anyOf": [
        {"type": "object", "additionalProperties": False, "required": ["op", "char"],
         "properties": {"op": {"type": "string", "enum": ["mood"]},
                        "char": {"type": "string"}}},
        {"type": "object", "additionalProperties": False, "required": ["op", "minutes"],
         "properties": {"op": {"type": "string", "enum": ["time_advance"]},
                        "minutes": {"type": "integer"}}}]}}}}}
_ANYOF_PROBE_MSGS = [{"role": "user", "content":
                      'Reply with JSON: {"ops":[{"op":"time_advance","minutes":30}]}'}]

_TRANSIENT_RETRIES = 2               # per _post call: 429/5xx retried with backoff
_BACKOFF_CAP_S = 15.0


@dataclass
class Endpoint:
    base_url: str
    model: str
    api_key: str = ""
    assist_tier: bool = False        # 04 SS5: small local models keep the OP CARD on rung 1-2


class TransientUpstreamError(Exception):
    """429/5xx that survived retries — not a capability signal (no demotion strike)."""

    def __init__(self, status: int, snippet: str) -> None:
        super().__init__(f"upstream {status}")
        self.status, self.snippet = status, snippet


class Ladder:
    """Capability-aware extraction against one endpoint. Probe once, cache, demote on strikes."""

    def __init__(self, store, cfg, get_client) -> None:
        self.store, self.cfg = store, cfg
        self.get_client = get_client
        self._forced_native: dict[tuple[str, str], str] = {}   # force_rung=1 dialect, in-memory
        self.last_rung: Optional[int] = None                   # rung of the last result
        self.last_raw: Optional[str] = None                    # raw model text of last extract (evals/debug)
        self.retry_sleep = asyncio.sleep                       # injectable for tests

    # -- probing --
    async def rung_for(self, ep: Endpoint) -> int:
        force = self.cfg.upstream.force_rung
        if force:                                # 06 A.2: force_rung ALWAYS wins, probe skipped
            rung = max(1, min(4, force))
            if rung == 1 and (ep.base_url, ep.model) not in self._forced_native:
                engine = await self._fingerprint(ep)
                dialect = engine if engine in _NATIVE else "llamacpp"
                if engine not in _NATIVE:
                    log.warning("force_rung=1 at unfingerprinted endpoint %s — assuming "
                                "llama.cpp json_schema dialect", ep.base_url)
                self._forced_native[(ep.base_url, ep.model)] = dialect
            return rung
        row = self.store.caps_get(ep.base_url, ep.model)
        ttl = self.cfg.upstream.probe_ttl_days * 86400
        if row and (time.time() - row["probed_at"]) < ttl:
            # Q18: an anyof verdict left unprobed (e.g. transient 429 during the probe)
            # is retried here even within TTL — one tiny call, then cached like the rest.
            if (row["rung"] == 2 and row["anyof"] == -1
                    and self.cfg.extraction.use_anyof):
                verdict = await self._probe_anyof(ep)
                if verdict is not None:
                    self.store.caps_set(ep.base_url, ep.model, row["rung"], anyof=verdict)
            return row["rung"]
        rung, engine = await self._probe(ep)
        anyof = None
        if rung == 2 and self.cfg.extraction.use_anyof:
            anyof = await self._probe_anyof(ep)          # alongside P2/P3 (Q18 addendum)
        self.store.caps_set(ep.base_url, ep.model, rung, native=engine, anyof=anyof)
        log.info("probe: %s %s -> rung %d (engine=%s, anyof=%s)", ep.base_url, ep.model,
                 rung, engine or "unknown", {1: "yes", 0: "no"}.get(anyof, "unprobed"))
        return rung

    async def _fingerprint(self, ep: Endpoint) -> str:
        """06 A.2 step 1 — engine tag or ''. Zero generation cost, and only ever attempted
        against local/self-hosted hosts: hosted APIs and unknown remotes never see P1."""
        if not is_local_host(ep.base_url):
            return ""
        blob = ""
        try:
            client = self.get_client()
            headers = {}
            key = ep.api_key or self.cfg.upstream.api_key
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = await client.get(ep.base_url.rstrip("/") + "/models", headers=headers)
            blob = (resp.text or "").lower() + " " + \
                " ".join(f"{k}:{v}" for k, v in resp.headers.items()).lower()
        except Exception as exc:
            log.debug("fingerprint GET /models failed: %s", type(exc).__name__)
        for tell, engine in _ENGINE_TELLS:
            if tell in blob:
                return engine
        return _PORT_HINTS.get(urlparse(ep.base_url).port or 0, "")

    async def _probe(self, ep: Endpoint) -> tuple[int, str]:
        """P1 (fingerprinted locals only) -> P2 json_schema -> P3 json_object -> 4.
        Cost: unknown/hosted endpoints see at most 2 tiny calls, once per TTL (06 A.2)."""
        engine = await self._fingerprint(ep)
        if engine in _NATIVE:
            fld, kind = _NATIVE[engine]
            try:
                body = {"model": ep.model, "messages": _PROBE_MSGS, "max_tokens": 30,
                        "temperature": 0,
                        fld: _PROBE_SCHEMA["schema"] if kind == "schema" else GBNF_JSON}
                doc = json.loads(strip_fences_and_prose(
                    await self._post(ep, _vendor_params(body, ep, thinking=False))))
                if isinstance(doc, dict) and isinstance(doc.get("ok"), bool):
                    return 1, engine
            except Exception as exc:
                log.debug("probe rung 1 (%s) failed: %s", engine, type(exc).__name__)
        for rung, rf in ((2, {"type": "json_schema", "json_schema": _PROBE_SCHEMA}),
                         (3, {"type": "json_object"})):
            try:
                body = {"model": ep.model, "messages": _PROBE_MSGS, "max_tokens": 30,
                        "temperature": 0, "response_format": rf}
                text = await self._post(ep, _vendor_params(body, ep, thinking=False))
                doc = json.loads(strip_fences_and_prose(text))
                if isinstance(doc, dict) and isinstance(doc.get("ok"), bool):
                    return rung, engine
            except Exception as exc:
                log.debug("probe rung %d failed: %s", rung, type(exc).__name__)
        return 4, engine

    # -- calling --
    async def _post(self, ep: Endpoint, body: dict) -> str:
        """One generation. 429/5xx retried with backoff (Retry-After honored) — if they
        persist, TransientUpstreamError: NOT a capability failure (06 A.2 strikes are for
        validation failures only; live eval #1: one heavy call tripped Venice's limiter
        and the old path burned every remaining rung AND case on instant 429s)."""
        client = self.get_client()
        headers = {"content-type": "application/json"}
        key = ep.api_key or self.cfg.upstream.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = ep.base_url.rstrip("/") + "/chat/completions"
        resp = None
        for attempt in range(_TRANSIENT_RETRIES + 1):
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _TRANSIENT_RETRIES:
                    delay = 0.0
                    try:
                        delay = float(resp.headers.get("retry-after") or 0)
                    except ValueError:
                        pass
                    delay = min(delay if delay > 0 else 2.0 * (attempt + 1), _BACKOFF_CAP_S)
                    log.warning("upstream %d — retrying in %.1fs (%d/%d)",
                                resp.status_code, delay, attempt + 1, _TRANSIENT_RETRIES)
                    await self.retry_sleep(delay)
                    continue
                raise TransientUpstreamError(resp.status_code, resp.text[:300])
            break
        if resp.status_code >= 400:
            log.warning("upstream %d: %s", resp.status_code, resp.text[:300])
            raise httpx.HTTPStatusError(f"upstream {resp.status_code}", request=resp.request,
                                        response=resp)
        doc = resp.json()
        msg = (doc.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        if not content and msg.get("reasoning_content"):
            # Venice thinking models with reasoning left ON put everything here (eval #1).
            log.warning("empty content with reasoning_content present (%d chars) — thinking "
                        "not disabled at %s? Falling back to reasoning_content.",
                        len(msg["reasoning_content"]), ep.base_url)
            content = msg["reasoning_content"]
        return content

    async def _probe_anyof(self, ep: Endpoint) -> Optional[int]:
        """ONE tiny call (06 A.2 cost discipline). 1 = accepted (branch-conformant
        reply), 0 = rejected/nonconforming, None = transient upstream — verdict stays
        unprobed and rung_for retries it next time. Probes are always thinking-off."""
        body = {"model": ep.model, "messages": _ANYOF_PROBE_MSGS, "max_tokens": 60,
                "temperature": 0,
                "response_format": {"type": "json_schema", "json_schema": _ANYOF_PROBE}}
        try:
            text = await self._post(ep, _vendor_params(body, ep, thinking=False))
            doc = json.loads(strip_fences_and_prose(text))
            ops = doc.get("ops") if isinstance(doc, dict) else None
            ok = (isinstance(ops, list) and bool(ops) and isinstance(ops[0], dict)
                  and ops[0].get("op") in {"mood", "time_advance"})
            return 1 if ok else 0
        except TransientUpstreamError:
            return None
        except Exception as exc:
            log.debug("anyof probe rejected: %s", type(exc).__name__)
            return 0

    def _rpg(self) -> bool:
        """RPG wire vocabulary gate (doc 07 §4.1): item ops are offered only under rpg so a
        `none` session's extraction requests stay byte-identical to 1.0."""
        spec = getattr(self.cfg, "specialization", None)
        return spec is not None and getattr(spec, "name", "none") == "rpg"

    def _wire_schema(self, ep: Endpoint) -> dict:
        """Rung-2 schema selection: anyOf per-op branches where the endpoint's strict
        mode verified them (caps.anyof == 1), flat otherwise. Fail-safe is always flat;
        a lying endpoint (probe ok, real calls fail) is handled by the EXISTING
        strike/demotion machinery — anyOf adds no new failure mode."""
        if self.cfg.extraction.use_anyof:
            row = self.store.caps_get(ep.base_url, ep.model)
            if row is not None and row["anyof"] == 1:
                return delta_json_schema_anyof(self._rpg())
        return delta_json_schema(self._rpg())

    def _native_dialect(self, ep: Endpoint) -> str:
        d = self._forced_native.get((ep.base_url, ep.model))
        if d:
            return d
        row = self.store.caps_get(ep.base_url, ep.model)
        return (row["native"] if row else "") or "llamacpp"

    def _body(self, ep: Endpoint, rung: int, system: str, user: str) -> dict:
        thinking = thinking_active(self.cfg, ep)
        body = {"model": ep.model, "temperature": 0,
                "max_tokens": (self.cfg.extraction.thinking_max_tokens if thinking
                               else self.cfg.extraction.max_tokens),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if rung == 1:
            fld, kind = _NATIVE.get(self._native_dialect(ep), _NATIVE["llamacpp"])
            body[fld] = delta_json_schema(self._rpg())["schema"] if kind == "schema" \
                else GBNF_JSON
        elif rung == 2:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": self._wire_schema(ep)}
        elif rung == 3:
            body["response_format"] = {"type": "json_object"}
        return _vendor_params(body, ep, thinking)

    # -- the ladder (03 SS5) --
    async def extract(self, ep: Endpoint, state_snapshot: str, characters: str,
                      t0: int, t1: int, exchange: str,
                      context: str = "") -> Optional[StateDelta]:
        seed = await self.rung_for(ep)
        self.last_raw = None
        user = prompts.user_message(state_snapshot, characters, t0, t1, exchange,
                                    self.cfg.extraction.language_hint, ep.assist_tier,
                                    context=context)
        include_card = not self.cfg.extraction.trim_op_card
        for rung in range(seed, 5):
            system = prompts.system_prompt(rung, ep.assist_tier, include_card,
                                           rpg=self._rpg())
            try:
                raw = await self._post(ep, self._body(ep, rung, system, user))
                self.last_raw = raw
            except TransientUpstreamError as exc:       # 429/5xx: abort, no strike, retry later
                log.warning("rung %d aborted (transient upstream %d — no strike): %s",
                            rung, exc.status, exc.snippet)
                break
            except httpx.HTTPStatusError as exc:        # 4xx: capability signal
                log.warning("rung %d call failed: upstream %d: %s", rung,
                            exc.response.status_code, exc.response.text[:300])
                self._strike(ep, rung, seed)
                continue
            except Exception as exc:
                log.warning("rung %d call failed: %s", rung, type(exc).__name__)
                self._strike(ep, rung, seed)
                continue
            delta = parse_and_validate(raw)
            if delta is None:                           # ONE repair pass per rung (03 SS5)
                try:
                    fixed = await self._post(ep, self._body(
                        ep, rung, system, prompts.repair_prompt("invalid JSON", raw[:2000])))
                    self.last_raw = fixed
                    delta = parse_and_validate(fixed)
                except Exception:
                    delta = None
            if delta is not None:
                if rung == seed:      # success at a LOWER rung doesn't absolve the seed rung:
                    self.store.caps_ok(ep.base_url, ep.model)   # 06 A.2 counts per-rung failures
                self.last_rung = rung
                return delta
            self._strike(ep, rung, seed)
        self.last_rung = None
        return None                                     # non-fatal: previous state stands

    def _strike(self, ep: Endpoint, rung: int, seed: int) -> None:
        """06 A.2: 3 consecutive VALIDATION failures at the SEED rung -> demote one rung
        (floor 4). Transient upstream errors never reach here. Re-promotion via re-probe."""
        if rung != seed or seed >= 4:
            return
        fails = self.store.caps_fail(ep.base_url, ep.model)
        if fails >= 3:
            self.store.caps_set(ep.base_url, ep.model, seed + 1)
            log.warning("demoted %s %s to rung %d after %d validation failures",
                        ep.base_url, ep.model, seed + 1, fails)
