"""State core: canonical state dict + op validation + mutation authority + pure reducer.

Sources: planning/02 (models SS3-8, StateDelta SS11, authority SS12b), 03 SS5.1 (apply/
quarantine), 03 R4 via reducer (craving ramp/withdrawal on time_advance), 08 E2 (family
apply order), 08 B4 (flashback/dream quarantine), Q13/Q14 (raw-mode neutrality).

Design notes (replay determinism, 03 SS3.3):
- Storage state is ONE JSON dict — the checkpoint/state_at currency. 02's pydantic models
  define the wire/extraction schemas; the reducer owns the canonical storage shape.
- The reducer is a PURE function of (state, ops). Anything config-dependent (craving seeds,
  withdrawal thresholds) is baked INTO the journaled op at apply time ("_seed"), so a config
  change never rewrites history.
- Authority/quarantine run at APPLY time, before journaling: the journal holds only
  authorized ops; replay applies them mechanically.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("aetherstate.state")

SCHEMA = "aetherstate/1"
BIG_TURN = 2**31

# ---- enums (02 SS4-7) -------------------------------------------------------------
BODY_ZONES = {"head", "face", "mouth", "neck", "shoulders", "chest", "breasts", "nipples",
              "back", "arms", "wrists", "hands", "waist", "hips", "ass", "anus", "genitals",
              "thighs", "legs", "feet"}
CONTACT_TYPES = {"touching", "caressing", "gripping", "kissing", "licking", "sucking",
                 "penetrating", "grinding", "restraining", "impact"}
BASE_POSITIONS = {"standing", "sitting", "kneeling", "straddling", "lying_back", "lying_front",
                  "on_all_fours", "bent_over", "held_carried"}
CLOTHING_STATE = {"don": "worn", "open": "opened", "displace": "displaced",
                  "remove": "removed", "destroy": "destroyed"}
# Q24: gear = worn (covers zones -> derived exposure) + carried
# (inventory: NEVER covers). "gear" is accepted as an op alias for "clothing"; the
# journal keeps kind="clothing" (replay compat). Carried = Q22 inventory foundation.
GEAR_WORN = {"base_layer", "top", "bottom", "outerwear", "footwear", "headwear",
             "handwear", "facewear", "eyewear", "neckwear", "armor"}
GEAR_CARRIED = {"load_bearing", "backpack", "pouch", "tool", "weapon", "ammunition",
                "consumable", "medical", "electronics", "accessory"}
GEAR_CATEGORIES = GEAR_WORN | GEAR_CARRIED
REL_DIMS = {"trust", "affection", "respect", "desire", "tension", "fear", "familiarity"}
VALENCED_DIMS = {"trust", "affection", "respect"}          # -100..100; rest 0..100
ACT_CATEGORIES = {"kissing", "manual", "oral_give", "oral_receive", "vaginal", "anal", "toys",
                  "restraint", "impact", "degradation", "praise", "exhibition", "group",
                  "roleplay_scene", "other"}
CONSENT_RANK = {"hard_limit": 0, "withdrawn": 1, "unknown": 2, "soft_limit": 3,
                "hesitant": 4, "granted": 5, "enthusiastic": 6}   # rank up = indulgence-direction
SIGNAL_TO_LEVEL = {"grant": "granted", "enthusiastic": "enthusiastic", "hesitant": "hesitant",
                   "refuse": "withdrawn", "withdraw": "withdrawn"}
TIMES = ["dawn", "morning", "midday", "afternoon", "evening", "night", "late_night"]
SCENE_MODES = {"live", "flashback", "dream"}

# ---- op spec: op -> (required fields, per-field validator hints) (02 SS11) --------
_SPEC: dict[str, set[str]] = {
    "set_attribute": {"entity", "key", "value"},
    "move_entity": {"entity", "to_location"},
    "presence": {"entity", "present"},
    "clothing": {"char", "item", "action"},
    "position": {"participants", "base"},
    "contact": {"action", "from_char", "to_char", "type"},
    "arousal": {"char"},                       # delta or set
    "mood": {"char"},                          # any of valence/energy/dominance
    "consent_signal": {"from_char", "to_char", "category", "signal"},
    "relationship_adj": {"from_char", "to_char", "dimension", "delta"},
    "reveal_fact": {"learner", "statement", "source"},
    "memory_event": {"text"},
    "goal": {"char", "action", "text"},
    "time_advance": set(),                     # minutes or to_time_of_day
    "obsession": {"char", "target_kind", "target"},
    "craving": {"char", "substance", "action"},
    # engine-internal ops (rule/user sources; never emitted by extraction):
    "clock_tick": {"minutes"},                 # R2 scene-minutes counter (no craving ramp)
    "scene_set": set(),                        # location / participants / phase
    "scene_dial": {"dial"},                    # tension | intimacy (organic)
    "scene_mode": {"mode"},                    # 08 B4 live|flashback|dream
    "entity_add": {"name"},
    "consent_set": {"subject", "partner", "category", "level"},   # inspector/OOC direct set
    "freeze": set(), "unfreeze": set(),
    "roll": {"spec", "result"},                # R7 (result pre-rolled at Tier-0)
    "stagnation": {"value"},                   # R6 signal
}

# 08 E2 deterministic family apply-order (freeze first so mid-delta safewords gate the rest)
_ORDER = {"freeze": -1, "unfreeze": 0, "entity_add": 0, "presence": 1, "move_entity": 2,
          "scene_set": 2, "scene_mode": 2, "position": 3, "clothing": 4, "contact": 5,
          "arousal": 6}
_DEFAULT_ORDER = 7

# 02 SS12b families
_FAMILY = {
    "set_attribute": "scene", "move_entity": "scene", "presence": "scene", "clothing": "scene",
    "position": "scene", "contact": "scene", "time_advance": "scene", "clock_tick": "scene",
    "scene_set": "scene", "scene_mode": "scene", "entity_add": "scene", "roll": "scene",
    "stagnation": "scene",
    "reveal_fact": "facts", "memory_event": "facts", "goal": "facts",
    "arousal": "organic", "mood": "organic", "relationship_adj": "organic",
    "obsession": "organic", "craving": "organic", "scene_dial": "organic",
    "consent_signal": "consent", "consent_set": "consent",
    "freeze": "safety", "unfreeze": "safety",
}
# frozen-session suppression set (02 SS6: arousal/escalation/consent families)
_FROZEN_SUPPRESSED = {"arousal", "scene_dial", "consent_signal"}


# Per-op enum vocabularies — SINGLE SOURCE OF TRUTH for wire schemas + validation
# (Q18 addendum). extraction.py derives BOTH schemas from this table: the flat rung-2
# schema takes per-FIELD unions (single-contributor fields keep their list order —
# TIMES stays chronological); the anyOf schema takes per-BRANCH enums verbatim.
# validate_op's checks below must agree — welded by test_enum_table_matches_validate_op.
# ADDING AN OP KIND touches: _SPEC, _FAMILY, this table (if it has enum fields), and in
# extraction.py EXTRACTION_OPS/_OP_ALLOWED/_OP_FIELDS + the OP CARD text — nothing else;
# schemas, scrub, and salvage all derive (Q22: inventory ops are the first customers).
OP_FIELD_ENUMS: dict[str, dict[str, list]] = {
    "clothing": {"action": sorted(CLOTHING_STATE)},
    "position": {"base": sorted(BASE_POSITIONS)},
    "contact": {"action": ["change", "start", "stop"], "type": sorted(CONTACT_TYPES)},
    "consent_signal": {"category": sorted(ACT_CATEGORIES),
                       "signal": sorted(set(SIGNAL_TO_LEVEL) | {"safeword"})},
    "relationship_adj": {"dimension": sorted(REL_DIMS)},
    "reveal_fact": {"source": ["inferred", "overheard", "told", "witnessed"]},
    "goal": {"action": ["abandon", "add", "complete"]},
    "time_advance": {"to_time_of_day": list(TIMES)},
    "obsession": {"target_kind": ["act_category", "concept", "entity", "object", "substance"]},
    "craving": {"action": ["adjust", "consume"]},
}
# 08 B4: non-live scenes quarantine physical/consent mutations (a flashback can't undress the present)
_NONLIVE_SUPPRESSED = {"clothing", "position", "contact", "arousal", "consent_signal",
                       "consent_set", "time_advance", "clock_tick"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_") or "unnamed"


def empty_state() -> dict:
    return {"schema": SCHEMA, "entities": {}, "chars": {}, "attributes": {}, "clothing": {},
            "poses": {}, "contacts": {}, "consent": {}, "relationships": {}, "facts": {},
            "beliefs": {}, "memories": [], "scene": {}, "clock": {"day": 1, "time_of_day": "evening",
            "minutes": 0, "calendar_note": None}, "frozen": False, "rolls": [],
            "meta": {"turn": -1}}


def is_empty(state: dict) -> bool:
    """True when nothing narratively meaningful has been recorded (header should not render)."""
    if not state:
        return True
    return not (state.get("entities") or state.get("chars") or state.get("scene")
                or state.get("consent") or state.get("relationships") or state.get("facts")
                or state.get("rolls") or state.get("frozen"))


# ================================ validation =======================================
def validate_op(op: Any) -> Optional[dict]:
    """03 SS5.1 op_valid(): per-op salvage — a bad op is dropped (reason logged by caller)."""
    if not isinstance(op, dict):
        return None
    if op.get("op") == "gear":              # Q24: alias, journal stays "clothing"
        op = {**op, "op": "clothing"}
    kind = op.get("op")
    spec = _SPEC.get(kind)
    if spec is None or not spec.issubset(op.keys()):
        return None
    try:
        if kind == "clothing":
            if op["action"] not in CLOTHING_STATE:
                return None
            if "category" in op and op["category"] not in GEAR_CATEGORIES:
                return None                  # unknown category: quarantined visibly (Q24)
        if kind == "position" and (op["base"] not in BASE_POSITIONS
                                   or not isinstance(op["participants"], list)):
            return None
        if kind == "contact":
            if op["action"] not in {"start", "stop", "change"} or op["type"] not in CONTACT_TYPES:
                return None
        if kind == "arousal" and not ("delta" in op or "set" in op):
            return None
        if kind == "mood" and not any(k in op for k in ("valence", "energy", "dominance")):
            return None
        if kind == "consent_signal" and (op["category"] not in ACT_CATEGORIES
                                         or op["signal"] not in set(SIGNAL_TO_LEVEL) | {"safeword"}):
            return None
        if kind == "consent_set" and (op["category"] not in ACT_CATEGORIES
                                      or op["level"] not in CONSENT_RANK):
            return None
        if kind == "relationship_adj" and op["dimension"] not in REL_DIMS:
            return None
        if kind == "reveal_fact" and op["source"] not in {"witnessed", "told", "overheard", "inferred"}:
            return None
        if kind == "goal" and op["action"] not in {"add", "complete", "abandon"}:
            return None
        if kind == "time_advance" and not ("minutes" in op or "to_time_of_day" in op):
            return None
        if kind == "time_advance" and op.get("to_time_of_day") not in (None, *TIMES):
            return None
        if kind == "obsession":
            if op["target_kind"] not in {"entity", "act_category", "substance", "object", "concept"}:
                return None
            if not ("delta" in op or "set" in op):
                return None
        if kind == "craving" and op["action"] not in {"consume", "adjust"}:
            return None
        if kind == "scene_mode" and op["mode"] not in SCENE_MODES:
            return None
        if kind == "scene_dial" and (op["dial"] not in {"tension", "intimacy"}
                                     or not ("delta" in op or "set" in op)):
            return None
    except (TypeError, KeyError):
        return None
    return op


# ================================ authority (02 SS12b) =============================
_ENTITY_FIELDS = {"entity", "char", "from_char", "to_char", "learner", "teller",
                  "subject", "partner"}


def resolve_aliases(op: dict, state: dict, source: str) -> tuple[Optional[dict], str]:
    """Names -> entity ids. Unknown + source=extraction/rule -> quarantine, and the name
    feeds the 08 B2 discovery counter. Unknown + source=user -> auto-create (inspector/OOC
    authoring is normal — 02 SS12b)."""
    amap = {}
    for eid, e in state.get("entities", {}).items():
        amap[e.get("name", "").lower()] = eid
        amap[eid] = eid
        for a in e.get("aliases", []):
            amap[str(a).lower()] = eid
    out = dict(op)
    for f in _ENTITY_FIELDS & set(op.keys()):
        name = str(op[f])
        eid = amap.get(name.lower())
        if eid is None:
            if source == "user" or op.get("op") == "entity_add":
                eid = slug(name)
                out.setdefault("_create", []).append({"eid": eid, "name": name})
            else:
                return None, f"unknown entity '{name}' (08 B2 discovery counts evidence)"
        out[f] = eid
    if op.get("op") == "position":
        parts, creates = [], out.setdefault("_create", [])
        for name in op.get("participants", []):
            eid = amap.get(str(name).lower())
            if eid is None:
                if source != "user":
                    return None, f"unknown entity '{name}'"
                eid = slug(name)
                creates.append({"eid": eid, "name": name})
            parts.append(eid)
        out["participants"] = parts
        if not creates:
            out.pop("_create", None)
    return out, ""


def authority_violation(op: dict, source: str, state: dict, cfg) -> Optional[str]:
    """Returns a human-readable rejection reason, or None if the op may apply."""
    kind = op["op"]
    family = _FAMILY.get(kind, "scene")
    frozen = bool(state.get("frozen"))
    raw = cfg.consent.mode == "unrestricted"
    nonlive = state.get("scene", {}).get("mode") in ("flashback", "dream")

    if kind == "consent_signal" and op["signal"] == "safeword":
        return None if not raw else None  # handled at apply: freeze in non-raw, log-only in raw

    if nonlive and kind in _NONLIVE_SUPPRESSED and source != "user":
        return "scene is flashback/dream: physical/consent/clock mutations quarantined (08 B4)"

    if source == "user":
        if family == "safety":
            return None                                    # always allowed (02 SS12b)
        if kind == "consent_set":
            old = state.get("consent", {}).get(
                f'{op["subject"]}|{op["partner"]}|{op["category"]}', {}).get("level", "unknown")
            if CONSENT_RANK[op["level"]] <= CONSENT_RANK.get(old, 2):
                return None                                # safety-direction: always
            if not cfg.manual_override.enabled:
                return "consent upgrade is gated: enable manual_override (02 SS12b)"
            return None
        if family == "organic" and not cfg.manual_override.enabled:
            return "organic values evolve through play: enable manual_override to edit (02 SS12b)"
        return None

    if source == "genesis":                # Q23.2: initialization, not mid-play override
        if kind == "unfreeze":
            return "unfreeze is user-only (02 SS6)"
        return None                            # may set organics/consent/entities at genesis

    if source == "rule":
        if kind == "unfreeze":
            return "unfreeze is user-only (02 SS6)"
        if kind == "consent_set":
            return "consent moves via signals or boundary rules only (02 SS6; boundary rules P4)"
        if frozen and family in ("organic",) and kind in _FROZEN_SUPPRESSED | {"craving", "obsession"}:
            return "session frozen: escalation-family mutations suppressed (02 SS6)"
        return None

    # source == "extraction" (P3 wires this; matrix enforced now for completeness)
    if kind == "entity_add":
        return "entity creation is privileged: discovery counts evidence first (03 SS5.1, 08 B2)"
    if family == "safety":
        return "extraction may only PROPOSE safety changes (02 SS6/SS12b)"
    if kind == "consent_set":
        return "extraction signals consent via consent_signal only"
    if frozen and (kind in _FROZEN_SUPPRESSED or family == "organic"
                   or (kind == "contact" and op.get("action") in ("start", "change"))):
        if not (kind == "consent_signal"
                and op.get("signal") in ("withdraw", "refuse", "safeword")):
            return "session frozen: arousal/escalation/consent mutations suppressed (02 SS6)"
    return None


# ================================ reducer (pure) ====================================
def _clamp(v, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _char(state: dict, eid: str) -> dict:
    return state["chars"].setdefault(eid, {
        "affect": {"valence": 0, "energy": 0, "dominance": 0},
        "arousal": {"arousal": 0, "inhibition": 50, "orgasms_this_scene": 0, "edging": False},
        "goals": [], "secrets": [], "status_effects": [], "obsessions": {}, "cravings": {}})


def _ensure_entities(state: dict, op: dict) -> None:
    for c in op.get("_create", []):
        state["entities"].setdefault(c["eid"], {
            "kind": "character", "name": c["name"], "aliases": [], "location_id": None,
            "present": True})


def _withdrawal_check(state: dict, eid: str, substance: str) -> None:
    c = state["chars"][eid]["cravings"][substance]
    seed = c.get("_seed", {})
    effects = c.get("withdrawal_effects") or [f"withdrawal:{substance}"]
    active = (c["level"] >= seed.get("withdrawal_level", 70)
              and c["dependency"] >= seed.get("withdrawal_dependency", 50))
    st = _char(state, eid)["status_effects"]
    for e in effects:
        if active and e not in st:
            st.append(e)
        if not active and e in st:
            st.remove(e)


def _craving_ramp(state: dict) -> None:
    """03 R4 / 02 SS4.1: deterministic rise on time_advance/scene transition."""
    for eid, ch in state["chars"].items():
        for sub, c in ch.get("cravings", {}).items():
            c["level"] = _clamp(c["level"] + c.get("ramp", 5), 0, 100)
            _withdrawal_check(state, eid, sub)


def _freeze(state: dict, turn: int, reason: str) -> None:
    state["frozen"] = True
    state["frozen_reason"] = reason
    state["frozen_turn"] = turn
    if reason == "safeword":  # 02 SS6: scene-participant consent -> withdrawn
        parts = set(state.get("scene", {}).get("participants", []))
        for key, entry in state.get("consent", {}).items():
            a, b, _ = key.split("|", 2)
            if not parts or a in parts or b in parts:
                entry["level"] = "withdrawn"


def _apply_op(state: dict, op: dict) -> None:  # noqa: C901 — one dispatch table, kept flat on purpose
    kind = op["op"]
    turn = op.get("_turn", state["meta"].get("turn", -1))
    _ensure_entities(state, op)

    if kind == "entity_add":
        eid = op.get("entity") or slug(op["name"])
        e = state["entities"].setdefault(eid, {"kind": op.get("kind", "character"),
                                               "name": op["name"], "aliases": [],
                                               "location_id": None, "present": False})
        for a in op.get("aliases", []):
            if a not in e["aliases"]:
                e["aliases"].append(a)
    elif kind == "set_attribute":
        state["attributes"].setdefault(op["entity"], {})[op["key"]] = op["value"]
    elif kind == "move_entity":
        state["entities"].setdefault(op["entity"], {"kind": "character", "name": op["entity"],
                                                    "aliases": [], "present": True}
                                     )["location_id"] = op["to_location"]
    elif kind == "presence":
        state["entities"].setdefault(op["entity"], {"kind": "character", "name": op["entity"],
                                                    "aliases": []})["present"] = bool(op["present"])
    elif kind == "clothing":
        item = state["clothing"].setdefault(op["char"], {}).setdefault(
            op["item"], {"state": "worn", "covers": [], "slot": None, "layer": 0})
        item["state"] = CLOTHING_STATE[op["action"]]
        item["updated_turn"] = turn
        if op.get("category"):
            item["category"] = op["category"]
        if op.get("category") in GEAR_CARRIED:
            item["covers"] = []              # Q24: carried gear NEVER covers (02 SS5.2)
        elif op.get("covers"):
            item["covers"] = [z for z in op["covers"] if z in BODY_ZONES
                              or z in _custom_zones(state, op["char"])]
        if op.get("moved_to"):
            item["location"] = op["moved_to"]
    elif kind == "position":
        for eid in op["participants"]:
            state["poses"][eid] = {"base": op["base"], "anchor": op.get("anchor"),
                                   "detail": op.get("detail")}
            parts = state["scene"].setdefault("participants", [])
            if eid not in parts:
                parts.append(eid)
    elif kind == "contact":
        key = "|".join((op["from_char"], op.get("from_part", "?"), op["to_char"],
                        op.get("to_part", "?"), op["type"]))
        if op["action"] == "start":
            state["contacts"][key] = {"from_char": op["from_char"],
                                      "from_part": op.get("from_part"),
                                      "to_char": op["to_char"], "to_part": op.get("to_part"),
                                      "type": op["type"],
                                      "intensity": _clamp(op.get("intensity", 1), 0, 3),
                                      "object": op.get("object"), "started_turn": turn}
        elif op["action"] == "change" and key in state["contacts"]:
            state["contacts"][key]["intensity"] = _clamp(op.get("intensity", 1), 0, 3)
        elif op["action"] == "stop":
            if key in state["contacts"]:
                del state["contacts"][key]
            else:  # partial match: stop all edges (from,to,type) when parts unspecified
                gone = [k for k, c in state["contacts"].items()
                        if c["from_char"] == op["from_char"] and c["to_char"] == op["to_char"]
                        and c["type"] == op["type"]]
                for k in gone:
                    del state["contacts"][k]
    elif kind == "arousal":
        a = _char(state, op["char"])["arousal"]
        a["arousal"] = (_clamp(op["set"], 0, 100) if "set" in op
                        else _clamp(a["arousal"] + int(op.get("delta", 0)), 0, 100))
    elif kind == "mood":
        aff = _char(state, op["char"])["affect"]
        for k in ("valence", "energy", "dominance"):
            if k in op:
                aff[k] = _clamp(op[k], -100, 100)
    elif kind == "consent_signal":
        if op["signal"] == "safeword":
            if not op.get("_raw_mode"):      # Q13/Q14: raw = logged as data, never freezes
                _freeze(state, turn, "safeword")
            return
        key = f'{op["from_char"]}|{op["to_char"]}|{op["category"]}'
        entry = state["consent"].setdefault(key, {"level": "unknown", "max_intensity": None,
                                                  "history": []})
        entry["level"] = SIGNAL_TO_LEVEL[op["signal"]]
        if "max_intensity" in op and op["max_intensity"] is not None:
            entry["max_intensity"] = _clamp(op["max_intensity"], 0, 3)
        entry["history"] = (entry["history"] + [[turn, entry["level"]]])[-100:]
    elif kind == "consent_set":
        key = f'{op["subject"]}|{op["partner"]}|{op["category"]}'
        entry = state["consent"].setdefault(key, {"level": "unknown", "max_intensity": None,
                                                  "history": []})
        entry["level"] = op["level"]
        if "max_intensity" in op and op["max_intensity"] is not None:
            entry["max_intensity"] = _clamp(op["max_intensity"], 0, 3)
        entry["history"] = (entry["history"] + [[turn, entry["level"]]])[-100:]
    elif kind == "relationship_adj":
        key = f'{op["from_char"]}->{op["to_char"]}'
        rel = state["relationships"].setdefault(key, {"dims": {}, "history": {}, "labels": []})
        dim = op["dimension"]
        lo = -100 if dim in VALENCED_DIMS else 0
        cur = rel["dims"].get(dim, 0)
        rel["dims"][dim] = _clamp(cur + _clamp(op["delta"], -30, 30), lo, 100)
        hist = rel["history"].setdefault(dim, [])
        hist.append([turn, rel["dims"][dim]])
        del hist[:-100]
    elif kind == "reveal_fact":
        fid = hashlib.blake2b(str(op["statement"]).encode(), digest_size=6).hexdigest()
        state["facts"].setdefault(fid, {"statement": op["statement"], "established_turn": turn,
                                        "is_secret": bool(op.get("is_secret"))})
        state["beliefs"][f'{op["learner"]}|{fid}'] = {
            "stance": "believes_true", "source": op["source"],
            "teller": op.get("teller"), "acquired_turn": turn}
    elif kind == "memory_event":
        tags = list(op.get("tags", []))
        mode = state.get("scene", {}).get("mode")
        if mode in ("flashback", "dream") and mode not in tags:
            tags.append(mode)                              # 08 B4: tag non-live memories
        state["memories"].append({"text": op["text"], "participants": op.get("participants", []),
                                  "importance": _clamp(op.get("importance", 3), 1, 10),
                                  "tags": tags, "turn": turn})
        del state["memories"][:-100]
    elif kind == "goal":
        goals = _char(state, op["char"])["goals"]
        if op["action"] == "add" and op["text"] not in goals:
            goals.append(op["text"])
        elif op["action"] in ("complete", "abandon") and op["text"] in goals:
            goals.remove(op["text"])
    elif kind == "time_advance":
        clock = state["clock"]
        if "minutes" in op:
            clock["minutes"] = int(clock.get("minutes", 0)) + int(op["minutes"])
        if op.get("to_time_of_day"):
            new, old = op["to_time_of_day"], clock.get("time_of_day", "evening")
            if TIMES.index(new) <= TIMES.index(old):       # cycle wrap -> next day
                clock["day"] = int(clock.get("day", 1)) + 1
            clock["time_of_day"] = new
            clock["minutes"] = 0
        if op.get("calendar_note"):
            clock["calendar_note"] = op["calendar_note"]
        _craving_ramp(state)                               # 03 R4 (replay-safe: lives in reducer)
    elif kind == "clock_tick":
        state["clock"]["minutes"] = int(state["clock"].get("minutes", 0)) + int(op["minutes"])
    elif kind == "obsession":
        key = f'{op["target_kind"]}:{slug(op["target"])}'
        obs = _char(state, op["char"])["obsessions"].setdefault(
            key, {"target_kind": op["target_kind"], "target": op["target"], "intensity": 0,
                  "flavor": "other", "behavior_note": None, "history": []})
        obs["intensity"] = (_clamp(op["set"], 0, 100) if "set" in op
                            else _clamp(obs["intensity"] + _clamp(op.get("delta", 0), -30, 30),
                                        0, 100))
        if op.get("flavor"):
            obs["flavor"] = op["flavor"]
        if op.get("behavior_note"):
            obs["behavior_note"] = op["behavior_note"]
        obs["history"] = (obs["history"] + [[turn, obs["intensity"]]])[-100:]
    elif kind == "craving":
        seed = op.get("_seed", {})
        c = _char(state, op["char"])["cravings"].setdefault(
            op["substance"], {"level": 0, "dependency": 0, "ramp": seed.get("ramp", 5),
                              "satisfaction": seed.get("satisfaction", 40),
                              "last_consumed_turn": None, "withdrawal_effects": [],
                              "_seed": seed})
        if op["action"] == "consume":
            c["level"] = _clamp(c["level"] - c.get("satisfaction", 40), 0, 100)
            c["dependency"] = _clamp(c["dependency"] + seed.get("dependency_per_consume", 2),
                                     0, 100)
            c["last_consumed_turn"] = turn
        else:
            c["level"] = _clamp(c["level"] + int(op.get("delta", 0)), 0, 100)
        _withdrawal_check(state, op["char"], op["substance"])
    elif kind == "scene_set":
        sc = state["scene"]
        if "location" in op:
            if sc.get("location_id") != op["location"]:      # scene boundary counter — pure
                sc["scene_index"] = int(sc.get("scene_index", 0)) + 1   # function of ops
            sc["location_id"] = op["location"]               # (replay-safe; 08 L2 cadence)
        if "participants" in op:
            sc["participants"] = [p for p in op["participants"]]
        if "phase" in op:
            sc["phase"] = op["phase"]
        sc.setdefault("mode", "live")
    elif kind == "scene_dial":
        sc = state["scene"]
        cur = sc.get(op["dial"], 0)
        sc[op["dial"]] = (_clamp(op["set"], 0, 100) if "set" in op
                          else _clamp(cur + int(op.get("delta", 0)), 0, 100))
    elif kind == "scene_mode":
        state["scene"]["mode"] = op["mode"]
    elif kind == "freeze":
        _freeze(state, turn, op.get("reason", "user"))
    elif kind == "unfreeze":
        state["frozen"] = False
        state.pop("frozen_reason", None)
    elif kind == "roll":
        state["rolls"].append({"spec": op["spec"], "result": op["result"], "turn": turn})
        del state["rolls"][:-10]
    elif kind == "stagnation":
        state["scene"]["stagnation"] = round(float(op["value"]), 3)
    if turn > state["meta"].get("turn", -1):
        state["meta"]["turn"] = turn


def _custom_zones(state: dict, eid: str) -> set:
    z = state.get("attributes", {}).get(eid, {}).get("zones.custom", [])
    return set(z) if isinstance(z, list) else set()


def reduce_state(state: dict, ops: list[dict]) -> dict:
    """The store.state_at reducer: journaled ops are pre-authorized -> apply mechanically."""
    if not state:
        state = empty_state()
    for op in ops:
        try:
            _apply_op(state, op)
        except Exception as exc:  # invariant 3: one bad journaled op never breaks replay
            log.warning("reducer skipped op %s: %s", op.get("op"), type(exc).__name__)
    return state


# ================================ derived views =====================================
def derived_exposure(state: dict, eid: str) -> list[str]:
    """02 SS5.2: exposure DERIVED, never stored. Zone exposed iff tracked but no worn item covers it."""
    items = state.get("clothing", {}).get(eid, {})
    tracked, covered = set(), set()
    for it in items.values():
        zones = set(it.get("covers", []))
        tracked |= zones
        if it.get("state") == "worn":
            covered |= zones
    return sorted(tracked - covered)


# ================================ apply pipeline ====================================
@dataclass
class ApplyResult:
    applied: list[dict] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)   # each: {"op":..., "reason":...}
    state: dict = field(default_factory=empty_state)
    froze: bool = False
    unfroze: bool = False


def current_state(store, branch_id: str) -> dict:
    return store.state_at(branch_id, BIG_TURN, reduce_state, empty=empty_state())


def _enrich(op: dict, turn: int, cfg) -> dict:
    """Bake config-dependent values into the journaled op (replay determinism, 03 SS3.3)."""
    out = dict(op)
    out["_turn"] = turn
    if op["op"] == "craving":
        d = cfg.drives
        out["_seed"] = {"ramp": d.craving_default_ramp,
                        "satisfaction": d.craving_default_satisfaction,
                        "withdrawal_level": d.withdrawal_level,
                        "withdrawal_dependency": d.withdrawal_dependency,
                        "dependency_per_consume": d.dependency_per_consume}
    if op["op"] == "consent_signal" and op.get("signal") == "safeword" \
            and cfg.consent.mode == "unrestricted":
        out["_raw_mode"] = True
    return out


def apply_delta(store, session_id: str, branch_id: str, turn: int, ops: list,
                source: str, cfg, turn_lo: Optional[int] = None) -> ApplyResult:
    """03 SS5.1: validate op-by-op, resolve aliases, authority-check, apply sequentially,
    journal ONLY what applied, checkpoint on cadence, mirror frozen to the session row."""
    res = ApplyResult(state=current_state(store, branch_id))
    was_frozen = bool(res.state.get("frozen"))
    pending = []
    for op in ops:
        v = validate_op(op)
        if v is None:
            res.quarantined.append({"op": op, "reason": "malformed op (02 SS11 spec)"})
            continue
        pending.append(v)
    pending.sort(key=lambda o: _ORDER.get(o["op"], _DEFAULT_ORDER))   # 08 E2 family order
    for op in pending:
        op2, why = resolve_aliases(op, res.state, source)
        if op2 is None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        why = authority_violation(op2, source, res.state, cfg)
        if why is not None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        op2 = _enrich(op2, turn, cfg)
        try:
            _apply_op(res.state, op2)
        except Exception as exc:
            res.quarantined.append({"op": op, "reason": f"apply error {type(exc).__name__}"})
            continue
        res.applied.append(op2)
    if res.applied:
        store.journal(branch_id, turn_lo if turn_lo is not None else turn, turn,
                      res.applied, source)
        every = cfg.session.checkpoint_every_turns
        if turn >= 0 and every > 0 and turn % every == 0:
            store.checkpoint(branch_id, turn, res.state)
    now_frozen = bool(res.state.get("frozen"))
    res.froze, res.unfroze = (not was_frozen and now_frozen), (was_frozen and not now_frozen)
    if res.froze or res.unfroze:
        store.set_frozen(session_id, now_frozen)
    for q in res.quarantined:
        log.info("quarantined %s op: %s", source, q["reason"])
    return res


# ================================ OOC path translation ==============================
_PATH_RE = re.compile(r"^(scene|clock|char|rel|consent)\.(.+)$")

# Bare-key aliases for common fields (P2 gate feedback, 2026-07-03):
# ((aether.set location Tavern)) == ((aether.set scene.location Tavern))
_BARE_ALIASES = {
    "location": "scene.location", "phase": "scene.phase", "mode": "scene.mode",
    "tension": "scene.tension", "intimacy": "scene.intimacy",
    "time": "clock.time_of_day", "time_of_day": "clock.time_of_day",
    "day": "clock.day", "note": "clock.calendar_note", "calendar_note": "clock.calendar_note",
}


def _unquote(v: str) -> str:
    """Strip ONE matching pair of surrounding quotes (P2 gate feedback: literal quotes stored)."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
        v = v[1:-1].strip()
    return v


def translate_path(path: str, value: str) -> Optional[dict]:
    """((aether.set path value)) / PATCH paths -> typed ops (02 SS12b surfaces).
    Unknown path -> None (rejected with visible reason — never silently applied)."""
    path = _BARE_ALIASES.get(path.strip().lower(), path.strip())
    m = _PATH_RE.match(path)
    if not m:
        return None
    root, rest = m.groups()
    v = _unquote(value)

    def num(x):
        try:
            return int(x)
        except ValueError:
            return x

    if root == "scene":
        if rest in ("location", "location_id"):
            return {"op": "scene_set", "location": v}
        if rest == "phase":
            return {"op": "scene_set", "phase": v}
        if rest == "mode" and v in SCENE_MODES:
            return {"op": "scene_mode", "mode": v}
        if rest in ("tension", "intimacy"):
            return {"op": "scene_dial", "dial": rest, "set": num(v)}
        return None
    if root == "clock":
        if rest == "time_of_day" and v in TIMES:
            return {"op": "time_advance", "to_time_of_day": v}
        if rest == "day":
            return None  # day moves via time_advance wraps only (monotonic clock, L6)
        if rest in ("note", "calendar_note"):
            return {"op": "time_advance", "minutes": 0, "calendar_note": v}
        return None
    if root == "char":
        parts = rest.split(".")
        if len(parts) < 2:
            return None
        who = parts[0]
        if parts[1] == "arousal":
            return {"op": "arousal", "char": who, "set": num(v)}
        if parts[1] == "affect" and len(parts) == 3 and parts[2] in ("valence", "energy", "dominance"):
            return {"op": "mood", "char": who, parts[2]: num(v)}
        if parts[1] == "attr" and len(parts) >= 3:
            return {"op": "set_attribute", "entity": who, "key": ".".join(parts[2:]), "value": num(v)}
        if parts[1] == "present":
            return {"op": "presence", "entity": who, "present": v.lower() in ("1", "true", "yes")}
        if parts[1] == "location":
            return {"op": "move_entity", "entity": who, "to_location": v}
        if parts[1] == "craving" and len(parts) == 4 and parts[3] == "level":
            return {"op": "craving", "char": who, "substance": parts[2], "action": "adjust",
                    "delta": num(v)}
        if parts[1] == "obsession" and len(parts) == 4 and parts[3] == "intensity":
            tk, _, tgt = parts[2].partition(":")
            if tk in {"entity", "act_category", "substance", "object", "concept"} and tgt:
                return {"op": "obsession", "char": who, "target_kind": tk, "target": tgt,
                        "set": num(v)}
        return None
    if root == "rel":
        m2 = re.match(r"^(.+?)->(.+?)\.([a-z_]+)$", rest)
        if m2 and m2.group(3) in REL_DIMS:
            return {"op": "relationship_adj", "from_char": m2.group(1), "to_char": m2.group(2),
                    "dimension": m2.group(3), "delta": num(v), "reason": "user edit"}
        return None
    if root == "consent":
        m2 = re.match(r"^(.+?)->(.+?)\.([a-z_]+)$", rest)
        if m2 and m2.group(3) in ACT_CATEGORIES and v in CONSENT_RANK:
            return {"op": "consent_set", "subject": m2.group(1), "partner": m2.group(2),
                    "category": m2.group(3), "level": v}
        return None
    return None


def state_summary(state: dict) -> dict:
    """Read-only inspector 'Now' view payload (10 SS3 core; full inspector lands P6)."""
    return {
        "schema": state.get("schema", SCHEMA),
        "frozen": bool(state.get("frozen")),
        "frozen_reason": state.get("frozen_reason"),
        "scene": state.get("scene", {}),
        "clock": state.get("clock", {}),
        "entities": {eid: {"name": e.get("name"), "present": e.get("present", False),
                           "location": e.get("location_id")}
                     for eid, e in state.get("entities", {}).items()},
        "chars": state.get("chars", {}),
        "exposure": {eid: derived_exposure(state, eid) for eid in state.get("clothing", {})},
        "clothing": state.get("clothing", {}),
        "poses": state.get("poses", {}),
        "contacts": list(state.get("contacts", {}).values()),
        "consent": state.get("consent", {}),
        "relationships": state.get("relationships", {}),
        "facts": state.get("facts", {}),
        "beliefs": state.get("beliefs", {}),
        "memories": state.get("memories", [])[-20:],
        "rolls": state.get("rolls", []),
        "turn": state.get("meta", {}).get("turn", -1),
    }
