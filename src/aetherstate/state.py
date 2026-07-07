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
# ---- RPG specialization vocab (doc 07 §1): the check-op tier ladder (PbtA + crits).
# Single source of truth — validate_op checks it; registry.resolve_tier only emits members.
CHECK_TIERS = ["crit_fail", "fail", "partial", "success", "crit_success"]
# ---- RPG-2 items vocab (docs 06 §3.5 / 07 §1): instance locations + gear slots.
# GEAR_SLOT_ORDER is the render order; the AUTHORITATIVE slot set may be extended per-table via
# the registry (meta.toml `extra_slots`) — validity is baked onto item_equip at _enrich time so
# the reducer never reads the registry (replay purity).
ITEM_LOC_KINDS = {"gear", "inv", "world", "gone"}
GEAR_SLOT_ORDER = ["head", "face", "neck", "shoulders", "body", "cape", "arms", "hands",
                   "mainhand", "offhand", "waist", "legs", "feet", "back",
                   "accessory1", "accessory2"]
GEAR_SLOTS = set(GEAR_SLOT_ORDER)
# ---- RPG-3 effects vocab (doc 05 §5.4): ledger-owned Statuses & Conditions. Truth lives in
# the ledger, not the prose — the LLM PROPOSES (tag protocol / extraction), code COMMITS.
EFFECT_KINDS = {"status", "condition"}
EFFECT_VALENCES = ["negative", "neutral", "positive"]   # dynamic per-record, never hardcoded
# ---- RPG-3b social vocab (docs 05 §5.4-5.6 / 06 §2.4 / 07 §1): affinity ledgers, factions,
# bonds, world flags. Affinity `value` is the CLAMPED LEDGER SUM; the tier is DERIVED at
# render from AFFINITY_TIERS (never stored — like derived_exposure, it cannot drift). The
# per-turn delta cap is the AI-Roguelite anti-swing fix, baked per-op (`_delta`) at _enrich
# so history stays stable even if the constant is tuned later.
AFFINITY_KINDS = {"npc", "faction"}
AFFINITY_CLAMP = (-100, 100)
AFFINITY_DELTA_CLAMP = (-15, 15)
DEVOTED_MIN = 80                       # soulmate-eligibility floor (doc 06 §2.4; linter-checked)
AFFINITY_TIERS = [(80, "Devoted"), (40, "Ally"), (10, "Warm"), (-9, "Neutral"),
                  (-39, "Cold"), (-79, "Hostile")]     # below the last floor -> Nemesis


def affinity_tier(value) -> str:
    """Derived tier label (doc 06 §2.4). Rendered/inspected, never stored."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    for floor, label in AFFINITY_TIERS:
        if v >= floor:
            return label
    return "Nemesis"


# ---- RPG-5 progression & recording vocab (doc 10; playtest 2026-07-06 G-series) ------
# Curated, Bean-editable constants: code awards XP/mastery, the LLM never types a number
# that sticks unclamped. Quests are a first-class ledger family (G3); items gain an organic
# acquisition channel (G2); HP gains a bounded consequence channel (G7).
QUEST_STATUSES = ["abandoned", "active", "complete", "failed"]
QUEST_STAKES = ["epic", "minor", "serious"]              # sorted (stable wire schema)
XP_AWARDS = {"quest_minor": 25, "quest_serious": 75, "quest_epic": 150,
             "goal": 15, "defeat": 50, "faction_tier": 30}
LEVEL_GRANTS = {"hp": 4, "pool": 2, "stat_points": 1}    # per level_up, baked at _enrich
MASTERY_CAP = 120                                        # the hard ceiling (Bean: "a limit")
MASTERY_BRACKETS = [(100, "Grandmaster"), (60, "Master"), (30, "Expert"),
                    (10, "Adept"), (0, "Novice")]
MASTERY_TICKS = {"crit_success": 4, "success": 3, "partial": 1, "fail": 1, "crit_fail": 0}
MASTERY_SCENE_CAP = 6            # anti-grind (doc 10 M2): ticks per skill per scene
DEFEAT_OUTCOMES = {"captured", "wake_safe", "robbed", "rescued", "death"}
HP_ADJ_MIN_CAP = 5               # per-op swing clamp floor: max(5, hp.max // 4)


def mastery_bracket(m) -> tuple[str, int]:
    """(label, bonus). The bonus joins effective_mod — the curated bracket bump
    (doc 10 §4), the evolution floor that needs no assist model."""
    try:
        v = int(m)
    except (TypeError, ValueError):
        v = 0
    for i, (floor, label) in enumerate(MASTERY_BRACKETS):
        if v >= floor:
            return label, len(MASTERY_BRACKETS) - 1 - i
    return "Novice", 0


def xp_level(xp) -> int:
    """Cumulative curve: level L needs 50*L*(L-1) total XP (L2=100, L3=300, L4=600…)."""
    try:
        v = max(0, int(xp))
    except (TypeError, ValueError):
        v = 0
    lvl = 1
    while 50 * (lvl + 1) * lvl <= v and lvl < 999:
        lvl += 1
    return lvl

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
    # RPG specialization (Q27 / doc 05): privileged ops (genesis/user); never extraction.
    "player_seed": {"entity"},                 # seed/replace the Player Card record
    "check": {"skill", "result", "tier"},      # RPG-1 R8 resolution record (rule/user; Tier-0)
    # RPG-2 items (doc 07 §2): item_mint is privileged (user/genesis/rule; never extraction);
    # the rest are PROPOSABLE (extraction may propose) and transactional at apply (doc 07 §7).
    "item_mint": {"template", "owner"},
    "item_move": {"instance", "to"},
    "item_equip": {"instance", "slot"},
    "item_unequip": {"instance"},
    "item_consume": {"instance"},
    "item_transfer": {"instance", "to_owner"},
    # RPG-3 effects (doc 05 §5.4): all three are PROPOSABLE (tag protocol + extraction);
    # optional fields: kind/valence/note/duration/stacks. ability_grant is PRIVILEGED —
    # the eligibility gate's acquisition route (you earn power in-world; never extraction).
    "effect_add": {"char", "effect"},
    "effect_remove": {"char", "effect"},
    "effect_update": {"char", "effect"},
    "ability_grant": {"char", "ability"},
    # RPG-3b social plane (docs 05 §5.4-5.6 / 07 §7.7-7.8): affinity_adj + world_flag are
    # PROPOSABLE (extraction may propose; delta clamped per turn); set_soulmate/set_nemesis
    # are PRIVILEGED — bonds are earned and set deliberately, extraction may only nudge
    # affinity. `target` may be null on the bond ops (= clear the bond).
    "affinity_adj": {"target", "delta"},
    "set_soulmate": {"target"},
    "set_nemesis": {"target"},
    "world_flag": {"key", "value"},
    # RPG-5 recording gaps (playtest 2026-07-06 G1-G8): item_gain/item_lose (the organic
    # acquisition channel — templateless names commit MECHANICS-FREE; a registry-template
    # name grounds its mechanics), quest_add/quest_update (the quest ledger family), and
    # hp_adj (bounded consequence channel) are PROPOSABLE (tag protocol + rpg wire).
    "item_gain": {"char", "name"},
    "item_lose": {"char", "name"},
    "quest_add": {"name"},
    "quest_update": {"quest"},
    "hp_adj": {"char", "delta"},
    # RPG-5 progression (doc 10): ALL PRIVILEGED (rule/user/genesis; extraction rejected).
    # Growth is code-awarded from resolved play — never asserted, never typed by a model.
    "award_exp": {"char", "amount"},
    "level_up": {"char"},
    "master_tick": {"char", "skill", "amount"},
    "evolve_def": {"char", "table", "id", "def"},
    "defeat_resolve": {"char", "outcome"},
    "stat_spend": {"char", "stat"},            # spend a banked stat point: +1 stat, -1 point
}

# 08 E2 deterministic family apply-order (freeze first so mid-delta safewords gate the rest)
_ORDER = {"freeze": -1, "unfreeze": 0, "entity_add": 0, "presence": 1, "move_entity": 2,
          "scene_set": 2, "scene_mode": 2, "item_mint": 2, "item_gain": 2,
          "item_transfer": 3, "position": 3,
          "clothing": 4, "item_move": 4, "item_equip": 4, "item_unequip": 4, "item_lose": 4,
          "contact": 5, "item_consume": 5, "effect_add": 5, "arousal": 6, "effect_update": 6,
          "effect_remove": 6, "hp_adj": 6, "award_exp": 8, "level_up": 9,
          "defeat_resolve": 9}
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
    "player_seed": "player",                   # RPG specialization (privileged)
    "check": "scene",                          # RPG-1: resolution record (like roll; scene family)
    "item_mint": "scene", "item_move": "scene", "item_equip": "scene",   # RPG-2 (doc 07 §3)
    "item_unequip": "scene", "item_consume": "scene", "item_transfer": "scene",
    "effect_add": "scene", "effect_remove": "scene", "effect_update": "scene",   # RPG-3
    "ability_grant": "player",             # RPG-3: privileged, like player_seed
    "affinity_adj": "organic",             # RPG-3b: evolves through play (doc 07 §3);
    #                                        frozen-suppression + manual_override for free
    "set_soulmate": "facts", "set_nemesis": "facts",   # narrative truth; privileged (§5.1 guard)
    "world_flag": "facts",                 # extraction may propose world truth (doc 07 §3)
    "item_gain": "scene", "item_lose": "scene", "hp_adj": "scene",    # RPG-5 (G2/G7)
    "quest_add": "facts", "quest_update": "facts",                    # RPG-5 (G3)
    "award_exp": "player", "level_up": "player", "master_tick": "player",   # RPG-5
    "evolve_def": "player", "defeat_resolve": "player",               # progression (privileged)
    "stat_spend": "player",                                            # spend a banked stat point
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
    # RPG-5 (rpg wire only — the fields are absent from the base _OP_FIELDS, so the base
    # flat schema stays byte-identical; the rpg schema attaches these enums).
    "quest_add": {"stakes": list(QUEST_STAKES)},
    "quest_update": {"status": list(QUEST_STATUSES)},
}
# 08 B4: non-live scenes quarantine physical/consent mutations (a flashback can't undress the present)
_NONLIVE_SUPPRESSED = {"clothing", "position", "contact", "arousal", "consent_signal",
                       "consent_set", "time_advance", "clock_tick", "check",
                       "item_mint", "item_move", "item_equip", "item_unequip",   # a flashback
                       "item_consume", "item_transfer",               # can't touch live items
                       "effect_add", "effect_remove", "effect_update",   # ...or live effects
                       "item_gain", "item_lose", "hp_adj", "defeat_resolve"}   # RPG-5: nor
#                        grant items / deal harm — a dream can't rob or wound the present


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_") or "unnamed"


def empty_state() -> dict:
    return {"schema": SCHEMA, "entities": {}, "chars": {}, "attributes": {}, "clothing": {},
            "poses": {}, "contacts": {}, "consent": {}, "relationships": {}, "facts": {},
            "beliefs": {}, "memories": [], "scene": {}, "clock": {"day": 1, "time_of_day": "evening",
            "minutes": 0, "calendar_note": None}, "frozen": False, "rolls": [], "player": {},
            "items": {}, "gear": {}, "inventory": {}, "effects": {}, "quests": {},
            "affinity": {}, "factions": {}, "world": {}, "meta": {"turn": -1}}


def is_empty(state: dict) -> bool:
    """True when nothing narratively meaningful has been recorded (header should not render)."""
    if not state:
        return True
    return not (state.get("entities") or state.get("chars") or state.get("scene")
                or state.get("consent") or state.get("relationships") or state.get("facts")
                or state.get("rolls") or state.get("frozen") or state.get("player"))


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
        if kind == "player_seed":
            if not str(op.get("entity", "")).strip():
                return None
            if "card" in op and not isinstance(op["card"], dict):
                return None
        if kind == "check" and op["tier"] not in CHECK_TIERS:   # RPG-1 R8 (doc 07 §5)
            return None
        if kind == "item_mint" and int(op.get("qty", 1)) < 1:   # RPG-2 items (doc 07 §5)
            return None
        if kind in ("item_move", "item_unequip", "item_transfer") and "to" in op \
                and str(op["to"]).split(":", 1)[0] not in ITEM_LOC_KINDS:
            return None
        if kind == "item_equip" and not isinstance(op["slot"], str):
            return None                # slot-vs-profile validity is baked at _enrich (doc 07 §5)
        if kind == "item_consume" and int(op.get("amount", 1)) < 1:
            return None
        if kind == "stat_spend" and not str(op.get("stat", "")).strip():   # spend a stat point
            return None
        if kind in ("effect_add", "effect_update", "effect_remove"):   # RPG-3 (doc 05 §5.4)
            if not str(op.get("effect", "")).strip():
                return None
            if "kind" in op and op["kind"] is not None and op["kind"] not in EFFECT_KINDS:
                return None
            if "valence" in op and op["valence"] is not None \
                    and op["valence"] not in EFFECT_VALENCES:
                return None                # catches mood-typed ints too — quarantined visibly
            if "duration" in op and op["duration"] is not None and int(op["duration"]) < 0:
                return None
            if "stacks" in op and op["stacks"] is not None and int(op["stacks"]) < 1:
                return None
        if kind == "ability_grant":
            if not str(op.get("ability", "")).strip():
                return None
            if "def" in op and op["def"] is not None and not isinstance(op["def"], dict):
                return None
        if kind == "affinity_adj":                     # RPG-3b (doc 07 §5)
            if isinstance(op["delta"], bool) or not isinstance(op["delta"], (int, float)):
                return None
            if "kind" in op and op["kind"] is not None and op["kind"] not in AFFINITY_KINDS:
                return None
        if kind in ("set_soulmate", "set_nemesis"):    # target may be null = clear the bond;
            if op["target"] is not None and not str(op["target"]).strip():   # eligibility +
                return None                            # uniqueness are linter/reducer concerns
            if "demote_label" in op and op["demote_label"] is not None \
                    and not isinstance(op["demote_label"], str):
                return None
        if kind == "world_flag":                       # scalar truth only — structures stay
            if not str(op.get("key", "")).strip():     # in entities/facts (doc 05 §5.6)
                return None
            if op["value"] is not None and not isinstance(op["value"], (str, int, float, bool)):
                return None
        if kind in ("item_gain", "item_lose"):         # RPG-5 (doc 07 §7 addendum)
            if not str(op.get("name", "")).strip():
                return None
            if kind == "item_gain" and op.get("qty") is not None and int(op["qty"]) < 1:
                return None
        if kind == "quest_add":                        # RPG-5 quest ledger (G3)
            if not str(op.get("name", "")).strip():
                return None
            if "stakes" in op and op["stakes"] is not None and op["stakes"] not in QUEST_STAKES:
                return None
        if kind == "quest_update":
            if not str(op.get("quest", "")).strip():
                return None
            if "status" in op and op["status"] is not None \
                    and op["status"] not in QUEST_STATUSES:
                return None
            if op.get("status") is None and not str(op.get("note") or "").strip():
                return None                            # an update must change something
        if kind == "hp_adj" and (isinstance(op["delta"], bool)
                                 or not isinstance(op["delta"], (int, float))):
            return None
        if kind == "award_exp" and (isinstance(op["amount"], bool)
                                    or not isinstance(op["amount"], (int, float))
                                    or int(op["amount"]) < 0):
            return None
        if kind == "master_tick" and (not str(op.get("skill", "")).strip()
                                      or int(op.get("amount", 0)) < 0):
            return None
        if kind == "evolve_def":
            if op["table"] not in ("skills", "abilities") \
                    or not str(op.get("id", "")).strip() or not isinstance(op["def"], dict):
                return None
        if kind == "defeat_resolve" and op["outcome"] not in DEFEAT_OUTCOMES:
            return None
    except (TypeError, KeyError, ValueError):
        return None
    return op


# ================================ authority (02 SS12b) =============================
_ENTITY_FIELDS = {"entity", "char", "from_char", "to_char", "learner", "teller",
                  "subject", "partner", "owner", "to_owner"}   # owner/to_owner: RPG-2 (doc 07 §2)
# RPG-3b (doc 07 §2): fields that name an entity only on SPECIFIC ops. `target` cannot join
# _ENTITY_FIELDS globally — obsession's target may be a substance/concept and must never be
# alias-resolved. Affinity/bond targets and a world_flag's faction ARE real entities: unknown
# names quarantine (extraction/rule) or auto-create (user), exactly like any other reference.
_ENTITY_FIELDS_BY_OP = {"affinity_adj": {"target"}, "set_soulmate": {"target"},
                        "set_nemesis": {"target"}, "world_flag": {"faction"}}


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
    extra = _ENTITY_FIELDS_BY_OP.get(op.get("op"), set())
    for f in (_ENTITY_FIELDS | extra) & set(op.keys()):
        if f in extra and op[f] is None:
            continue                       # null bond target = clear (doc 07 §7.8)
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


# ---- RPG-4 location registry & canonicalization (doc 05 §9) -------------------------
_LOC_ARTICLES = ("the ", "a ", "an ")
_LOC_HEAD_RE = re.compile(r"[,;:.(]|\s[—–-]\s")   # name head before prose; keeps 'Vael-Cora'


def _norm_loc(name: str) -> str:
    """Comparison key for a location name: lowercase, leading article stripped,
    punctuation collapsed. Pure text function."""
    t = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower()).strip()
    for a in _LOC_ARTICLES:
        if t.startswith(a):
            t = t[len(a):]
            break
    return re.sub(r"\s+", " ", t).strip()


def canonical_location(state: dict, raw: str) -> tuple[str, str, bool]:
    """(loc_id, display_name, is_new). RPG-4: one place, one row — a revisit under any
    name variant resolves to the SAME location entity instead of minting another.

    Resolution ladder over entities kind=location: exact id → exact name/alias
    (article-stripped, case-insensitive) → unique token-subset match (raw ≤4 tokens AND
    exactly one candidate — the wrong-merge guard) → NEW. Free prose is trimmed to its
    name head first (live repro 2026-07-06: a pasted description became the location id
    vael_cora_the_capital_city_of_the_realm_…). Reads only state — the caller (_enrich)
    bakes the result into the journaled op, so replay never re-resolves."""
    text = str(raw or "").strip()
    head = _LOC_HEAD_RE.split(text, 1)[0].strip() or text
    words = head.split()
    if len(words) > 6:                                   # a name is short; prose is not
        head = " ".join(words[:6])
    key = _norm_loc(head)
    ents = state.get("entities", {}) or {}
    if not key:
        return (slug(text)[:64] or "unknown_location"), (text or "unknown location"), True
    if text in ents and (ents[text] or {}).get("kind") == "location":
        return text, (ents[text] or {}).get("name", text), False
    locs = [(eid, e) for eid, e in ents.items() if (e or {}).get("kind") == "location"]
    for eid, e in locs:
        names = [e.get("name", "")] + [str(a) for a in e.get("aliases", [])]
        if any(_norm_loc(n) == key for n in names if n):
            return eid, e.get("name", head), False
    toks = set(key.split())
    if toks and len(toks) <= 4:
        hits = [eid for eid, e in locs
                if toks <= set(_norm_loc(e.get("name", "")).split())
                or any(toks <= set(_norm_loc(a).split()) for a in e.get("aliases", []))]
        if len(hits) == 1:                               # ambiguous → new row, never a guess
            return hits[0], (ents[hits[0]] or {}).get("name", head), False
    return slug(head), head, True


def authority_violation(op: dict, source: str, state: dict, cfg) -> Optional[str]:
    """Returns a human-readable rejection reason, or None if the op may apply."""
    kind = op["op"]
    family = _FAMILY.get(kind, "scene")
    frozen = bool(state.get("frozen"))
    raw = cfg.consent.mode == "unrestricted"
    nonlive = state.get("scene", {}).get("mode") in ("flashback", "dream")

    if kind == "player_seed":                  # privileged: initialization only (doc 05 §5.1)
        return None if source in ("user", "genesis") else \
            "player card is privileged: genesis or the user only (doc 05 §5.1)"

    if kind == "ability_grant":                # RPG-3: power is ACQUIRED in-world, never asserted
        return None if source in ("user", "genesis", "rule") else \
            "abilities are earned in-world: the engine grants them (quest/ritual/user) — " \
            "extraction may only witness, not bestow (doc 10)"

    if kind in ("award_exp", "level_up", "master_tick", "evolve_def", "defeat_resolve"):
        return None if source in ("user", "genesis", "rule") else \
            "progression is code-awarded: XP, levels, mastery, and defeat are earned " \
            "through resolved play, never asserted (doc 10)"   # RPG-5

    if kind == "stat_spend":                   # spending a banked point is the player's call
        return None if source in ("user", "genesis", "rule") else \
            "stat points are spent by the player, never asserted by a model (doc 10)"

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
    if kind == "check":                        # RPG-1: extraction never rolls (doc 07 §5.1)
        return "checks resolve at Tier-0 (rule) or via the user only; extraction never rolls"
    if kind == "item_mint":                    # RPG-2: extraction never creates items (doc 07 §5.1)
        return "item minting is privileged: user/genesis/rule only — extraction may move, not mint"
    if kind in ("set_soulmate", "set_nemesis"):   # RPG-3b (doc 07 §5.1)
        return "bond pointers are privileged: extraction may only nudge affinity, " \
               "not set soulmate/nemesis (doc 06 §2.4)"
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


class OpReject(ValueError):
    """Raised by a reducer arm when a transactional precondition fails (doc 07 §7): apply_delta
    quarantines the op with THIS message and state is left untouched (invariant 3). Journaled
    ops never contain a rejected op, so replay never sees one."""


# ---- RPG-2 item-index helpers (doc 07 §7/§8) — thin, pure state readers/writers. The
# instance's `loc` is the single source of truth; `gear`/`inventory` are derived indexes the
# render blocks read. Every mutation goes remove -> add with rollback on failure, which is what
# makes one-instance-one-place hold by construction (and the linter rule a pure safety net).
def _default_container(state: dict, eid) -> str:
    return "loose"                             # the always-available unbounded pseudo-container


def _container_capacity(state: dict, cid: str):
    it = state.get("items", {}).get(cid)
    cap = (it or {}).get("capacity")
    try:
        return int(cap) if cap is not None else None
    except (TypeError, ValueError):
        return None


def _index_remove(state: dict, iid: str) -> None:
    for slots in state.get("gear", {}).values():
        for s in [s for s, x in slots.items() if x == iid]:
            del slots[s]
    for conts in state.get("inventory", {}).values():
        for lst in conts.values():
            while iid in lst:
                lst.remove(iid)


def _index_add(state: dict, owner, iid: str, loc: str) -> bool:
    """Reflect `loc` into the gear/inventory index. False = capacity/slot conflict (caller
    rolls back and rejects). world/gone are indexed nowhere and always succeed."""
    kind0, _, rest = str(loc).partition(":")
    if kind0 == "gear":
        if not owner or not rest:
            return False
        slots = state.setdefault("gear", {}).setdefault(owner, {})
        if slots.get(rest):
            return False
        slots[rest] = iid
        return True
    if kind0 == "inv":
        if not owner or not rest:
            return False
        lst = state.setdefault("inventory", {}).setdefault(owner, {}).setdefault(rest, [])
        cap = _container_capacity(state, rest)
        if cap is not None and iid not in lst and len(lst) >= cap:
            return False
        if iid not in lst:
            lst.append(iid)
        return True
    return kind0 in ("world", "gone")


def _resolve_instance(state: dict, token) -> Optional[str]:
    """Exact instance id, else a UNIQUE case-insensitive name match among live instances —
    extraction references items by the names the [GEAR]/[INVENTORY] blocks render (doc 07 §2:
    instance refs are validated against state, not a static enum). Ambiguous/unknown -> None."""
    items = state.get("items", {})
    t = str(token or "").strip()
    if t in items:
        return t
    low = t.lower()
    hits = [iid for iid, it in items.items()
            if (it or {}).get("loc") != "gone" and str((it or {}).get("name", "")).lower() == low]
    return hits[0] if len(hits) == 1 else None


# ---- RPG-3b social helpers — pure state readers -------------------------------------
def _player_eid(state: dict) -> Optional[str]:
    """The (one) Player Card eid — affinity/bonds are measured FROM the player (doc 06 §2.4)."""
    for eid, rec in (state.get("player") or {}).items():
        if isinstance(rec, dict):
            return eid
    return None


# ---- RPG-3 effect helpers (doc 05 §5.4) — pure state readers ------------------------
def _entity_sex(state: dict, eid: str) -> str:
    """Best-known sex for an entity: the `sex`/`gender` attribute (creator writes it),
    else the Player Card's pronouns. Empty = unknown (a data-driven `requires` gate then
    REJECTS with a visible reason — never guesses)."""
    attrs = state.get("attributes", {}).get(eid, {}) or {}
    for k in ("sex", "gender"):
        v = str(attrs.get(k) or "").strip().lower()
        if v:
            return v
    pron = str((state.get("player", {}).get(eid) or {}).get("pronouns", "")).lower()
    if pron.startswith("she"):
        return "female"
    if pron.startswith("he"):
        return "male"
    return ""


def _resolve_effect_key(effs: dict, token) -> Optional[str]:
    """Ledger key for `token` among ONE entity's effects: exact id, slug, else unique
    case-insensitive display-name match (tags/extraction reference effects by the names
    the [EFFECTS] block renders — same contract as _resolve_instance)."""
    t = str(token or "").strip()
    if t in effs:
        return t
    t2 = slug(t)
    if t2 in effs:
        return t2
    low = t.lower()
    hits = [k for k, r in effs.items()
            if str((r or {}).get("name", "")).lower() == low]
    return hits[0] if len(hits) == 1 else None


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


def _apply_player_seed(state: dict, op: dict) -> None:
    """Expand a Player Card seed into the runtime `player` record (RPG genesis, doc 06 §2.2).
    Privileged (authority gates source); tolerant of a partial seed so the [PLAYER] block can
    render from turn 0. The player is also an ordinary entities row, marked kind='player'.

    ONE PLAYER PER SESSION (2026-07-06 live repro: a genesis-default 'Player' rode alongside
    the Creator-saved character and the narrator treated the real one as an NPC). player_seed
    is seed/REPLACE, as the op table always said: seeding entity B while entity A holds the
    card drops A's record — a genesis-default placeholder vanishes entirely (entity+attrs),
    an authored predecessor stays in the world demoted to kind='npc'. Pure state+op reads —
    replay-safe."""
    eid = op["entity"]
    card = op.get("card") or {}
    players = state.setdefault("player", {})
    for other in [k for k in players if k != eid]:
        old = players.pop(other)
        if isinstance(old, dict) and old.get("genesis_default"):
            state.get("entities", {}).pop(other, None)     # placeholder: no trace remains
            state.get("attributes", {}).pop(other, None)
        else:
            ent = state.get("entities", {}).get(other)
            if isinstance(ent, dict):
                ent["kind"] = "npc"                        # a real predecessor stays in-world
    rec = players.setdefault(
        eid, {"eid": eid, "level": 1, "xp": 0, "hp": {"cur": 0, "max": 0}, "resources": {},
              "stats": {}, "skills": {}, "abilities": [], "cooldowns": {},
              "soulmate": None, "nemesis": None})
    if card.get("_genesis_default"):
        rec["genesis_default"] = True                      # marks the replace-me placeholder
    else:
        rec.pop("genesis_default", None)                   # an authored card sheds the mark
    if "level" in card:
        rec["level"] = _clamp(card.get("level", rec["level"]), 1, 999)
    if card.get("concept"):
        rec["concept"] = str(card["concept"])
    if card.get("pronouns"):
        rec["pronouns"] = str(card["pronouns"])
    if isinstance(card.get("stats"), dict):
        rec["stats"] = {str(k): _clamp(v, -99, 999) for k, v in card["stats"].items()}
    if isinstance(card.get("skills"), dict):
        rec["skills"] = {str(k): _clamp(v, 0, 99) for k, v in card["skills"].items()}
    if isinstance(card.get("abilities"), list):
        rec["abilities"] = [str(a) for a in card["abilities"]]
    if isinstance(card.get("resources"), dict):
        for rname, spec in card["resources"].items():
            if not isinstance(spec, dict):
                continue
            mx = _clamp(spec.get("max", spec.get("cur", 0)), 0, 10**6)
            cur = _clamp(spec.get("cur", mx), 0, mx)
            if str(rname).lower() == "hp":
                rec["hp"] = {"cur": cur, "max": mx}
            else:
                rec["resources"][str(rname)] = {"cur": cur, "max": mx}
    if isinstance(card.get("hp"), dict):            # hp may also be given directly
        hp = card["hp"]
        mx = _clamp(hp.get("max", hp.get("cur", rec["hp"]["max"])), 0, 10**6)
        rec["hp"] = {"cur": _clamp(hp.get("cur", mx), 0, mx), "max": mx}
    if isinstance(card.get("defs"), dict):          # per-character FROZEN defs (Q27 overlay, doc 09 §1)
        defs = rec.setdefault("defs", {})           # freestyle / mastery-evolved skills+abilities live here;
        for table in ("skills", "abilities", "stats"):   # resolver reads them snapshot-first over the registry
            src_t = card["defs"].get(table)
            if isinstance(src_t, dict):
                dst = defs.setdefault(table, {})
                for k, v in src_t.items():
                    if isinstance(v, dict):         # stored as authored — a frozen snapshot of fixed numbers
                        dst[str(k)] = v
    ent = state.get("entities", {}).get(eid)
    if ent is not None:
        ent["kind"] = "player"


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
        if not any(m.get("text") == op["text"]             # 2026-07-07: a double-clicked
                   for m in state["memories"][-20:]):      # creator save duplicated every
            state["memories"].append({                     # lore row — exact-dupe guard
                "text": op["text"], "participants": op.get("participants", []),
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
        if op.get("to_time_of_day"):                       # RPG-5 (doc 10 §6): a real time
            for p in (state.get("player") or {}).values():   # skip is rest — pools refill
                if not isinstance(p, dict):                   # (stamina full, others +50%)
                    continue
                for rname, r in (p.get("resources") or {}).items():
                    if isinstance(r, dict) and r.get("max"):
                        full = str(rname).lower() == "stamina"
                        gain = int(r["max"]) if full else max(1, int(r["max"]) // 2)
                        r["cur"] = _clamp(int(r.get("cur", 0)) + gain, 0, int(r["max"]))
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
            lc = op.get("_loc_create")
            if lc:                                           # RPG-4: canonical registry row —
                state["entities"].setdefault(str(lc["eid"]), {   # generated once, persisted,
                    "kind": "location", "name": lc["name"], "aliases": [],   # never regenerated
                    "location_id": None, "present": False})
            boundary = sc.get("location_id") != op["location"]
            if boundary:                                     # scene boundary counter — pure
                sc["scene_index"] = int(sc.get("scene_index", 0)) + 1   # function of ops
                for p in (state.get("player") or {}).values():   # RPG-5 (doc 10 §6): a scene
                    for r in (p.get("resources") or {}).values() \
                            if isinstance(p, dict) else ():      # change catches the breath —
                        if isinstance(r, dict) and r.get("max"):   # +25% of max, curated
                            r["cur"] = _clamp(int(r.get("cur", 0)) + max(1, int(r["max"]) // 4),
                                              0, int(r["max"]))
            sc["location_id"] = op["location"]               # (replay-safe; 08 L2 cadence)
            ent = state["entities"].get(op["location"])
            if op.get("_canon") and isinstance(ent, dict):   # RPG-4 (op-driven: none untouched)
                if boundary:
                    ent["visits"] = int(ent.get("visits", 0)) + 1
                    ent["last_visit_turn"] = turn
                al = op.get("_loc_alias")
                if al and al != ent.get("name") and al not in ent.get("aliases", []):
                    ent.setdefault("aliases", []).append(al)
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
    elif kind == "check":                       # RPG-1 R8: a richer roll (skill + PbtA tier)
        state["rolls"].append({"skill": op["skill"], "result": op["result"],
                               "tier": op["tier"], "mod": op.get("_mod"),
                               "dice": op.get("_dice"), "dc": op.get("dc"),
                               "char": op.get("char"), "turn": turn,
                               "shape": op.get("_shape")})   # 2026-07-07: dice-shaping audit
        del state["rolls"][:-10]
        cost = op.get("_cost")                  # RPG-5 (doc 10 §5.4): resource cost charged
        pl = state.get("player", {}).get(op.get("char")) if op.get("char") else None
        if isinstance(cost, dict) and isinstance(pl, dict):
            for rname, amt in cost.items():
                pool = pl.get("hp") if rname == "hp" else (pl.get("resources") or {}).get(rname)
                if isinstance(pool, dict):
                    pool["cur"] = _clamp(int(pool.get("cur", 0)) - max(0, int(amt)),
                                         0, int(pool.get("max", 0)))
        cds = op.get("_ability_cd")             # 2026-07-07: active-ability cooldowns (baked)
        if isinstance(cds, dict) and isinstance(pl, dict):
            cd = pl.setdefault("ability_cd", {})
            for aid, ready in cds.items():
                try:
                    cd[str(aid)] = max(int(cd.get(str(aid), 0)), int(ready))
                except (TypeError, ValueError):
                    continue
    elif kind == "item_mint":                   # RPG-2 (doc 07 §7.2): instance from template
        iid, snap = op.get("_iid"), op.get("_snapshot") or {}
        if not iid or not snap:                 # template unknown at _enrich -> visible reject
            raise OpReject(f"unknown item template '{op.get('template')}' "
                           f"(add it to registry items.toml — nothing freestyle)")
        owner = op["owner"]
        items = state.setdefault("items", {})
        loc = str(op.get("to") or f"inv:{_default_container(state, owner)}")
        rec = {"template_id": op["template"], "name": snap.get("name", op["template"]),
               "qty": max(1, int(op.get("qty", 1))), "loc": loc, "owner": owner,
               "mods_snapshot": dict(snap.get("mods") or {}), "minted_turn": turn}
        for k in ("slot", "covers", "on_consume", "stackable", "max_stack", "capacity",
                  "is_container", "worn", "type"):
            if k in snap:
                rec[k] = snap[k]
        if op.get("bound"):
            rec["bound"] = True
        items[iid] = rec
        if not _index_add(state, owner, iid, loc):
            del items[iid]                      # rollback: full container / busy slot (doc 07 §7.2)
            raise OpReject(f"cannot mint into {loc}: container full or slot busy")
    elif kind == "item_move":                   # RPG-2 (doc 07 §7.3): within-owner relocation
        iid = _resolve_instance(state, op["instance"])
        it = state.get("items", {}).get(iid) if iid else None
        if not it:
            raise OpReject(f"unknown item instance '{op['instance']}'")
        new = str(op["to"])
        if new.split(":", 1)[0] in ("inv", "gear") and not it.get("owner"):
            raise OpReject("unowned item: use item_transfer to give it an owner first")
        old = it["loc"]
        _index_remove(state, iid)
        if not _index_add(state, it.get("owner"), iid, new):
            _index_add(state, it.get("owner"), iid, old)       # transactional rollback
            raise OpReject(f"cannot move to {new}: container full or slot busy")
        it["loc"] = new
        if new.split(":", 1)[0] in ("world", "gone"):
            it["owner"] = None
    elif kind == "item_equip":                  # RPG-2 (doc 07 §7.4): inv -> gear:<slot>
        iid = _resolve_instance(state, op["instance"])
        it = state.get("items", {}).get(iid) if iid else None
        slot = str(op["slot"])
        if not it:
            raise OpReject(f"unknown item instance '{op['instance']}'")
        if not op.get("_slot_ok", slot in GEAR_SLOTS):
            raise OpReject(f"unknown gear slot '{slot}' (profile slot map)")
        owner = it.get("owner")
        if not owner:
            raise OpReject("unowned item: transfer it to a character before equipping")
        occ = state.setdefault("gear", {}).setdefault(owner, {}).get(slot)
        if occ and occ != iid:
            if not op.get("swap"):
                raise OpReject(f"slot {slot} is occupied (set swap to displace the current item)")
            dest = f"inv:{_default_container(state, owner)}"   # atomic swap: displace incumbent
            _index_remove(state, occ)
            if not _index_add(state, owner, occ, dest):
                _index_add(state, owner, occ, f"gear:{slot}")  # restore -> reject
                raise OpReject("swap failed: no room to displace the equipped item")
            state["items"][occ]["loc"] = dest
        old = it["loc"]
        _index_remove(state, iid)
        if not _index_add(state, owner, iid, f"gear:{slot}"):
            _index_add(state, owner, iid, old)                 # transactional rollback
            raise OpReject(f"slot {slot} is occupied")
        it["loc"] = f"gear:{slot}"              # gear mods are DERIVED at render/resolution,
        #                                         never written into player stats (doc 07 §7.4)
    elif kind == "item_unequip":                # RPG-2 (doc 07 §7.4): gear -> inv (or given loc)
        iid = _resolve_instance(state, op["instance"])
        it = state.get("items", {}).get(iid) if iid else None
        if not it or not str(it.get("loc", "")).startswith("gear:"):
            raise OpReject(f"'{op['instance']}' is not equipped")
        owner = it.get("owner")
        dest = str(op.get("to") or f"inv:{_default_container(state, owner)}")
        old = it["loc"]
        _index_remove(state, iid)
        if not _index_add(state, owner, iid, dest):
            _index_add(state, owner, iid, old)                 # transactional rollback
            raise OpReject(f"cannot unequip to {dest}: container full")
        it["loc"] = dest
        if dest.split(":", 1)[0] in ("world", "gone"):
            it["owner"] = None
    elif kind == "item_consume":                # RPG-2 (doc 07 §7.5): qty--; baked on_consume only
        iid = _resolve_instance(state, op["instance"])
        it = state.get("items", {}).get(iid) if iid else None
        if not it or int(it.get("qty", 0)) < 1:
            raise OpReject(f"unknown or depleted item instance '{op['instance']}'")
        it["qty"] = max(0, int(it.get("qty", 1)) - max(1, int(op.get("amount", 1))))
        eff = it.get("on_consume") or {}        # read from the INSTANCE (baked at mint) — never
        owner = it.get("owner")                 # the registry (replay purity, doc 07 §7.5)
        pl = state.get("player", {}).get(owner) if owner else None
        if pl and isinstance(eff, dict):
            if isinstance(eff.get("heal"), int) and isinstance(pl.get("hp"), dict):
                pl["hp"]["cur"] = _clamp(pl["hp"]["cur"] + eff["heal"], 0,
                                         pl["hp"].get("max", 0))
            if isinstance(eff.get("restore"), dict):           # bounded resource restore
                for rname, amt in eff["restore"].items():
                    r = (pl.get("resources") or {}).get(str(rname))
                    if isinstance(r, dict):
                        r["cur"] = _clamp(r.get("cur", 0) + int(amt), 0, r.get("max", 0))
        if it["qty"] == 0:
            _index_remove(state, iid)
            it["loc"], it["owner"] = "gone", None
    elif kind == "item_transfer":               # RPG-2 (doc 07 §7.6): atomic owner change
        iid = _resolve_instance(state, op["instance"])
        it = state.get("items", {}).get(iid) if iid else None
        if not it:
            raise OpReject(f"unknown item instance '{op['instance']}'")
        new_owner = op["to_owner"]
        dest = str(op.get("to") or f"inv:{_default_container(state, new_owner)}")
        if dest.split(":", 1)[0] not in ("inv", "gear"):
            raise OpReject(f"bad transfer destination '{dest}'")
        old_loc, old_owner = it["loc"], it.get("owner")
        _index_remove(state, iid)
        if not _index_add(state, new_owner, iid, dest):        # partial failure -> FULL rollback
            _index_add(state, old_owner, iid, old_loc)         # (the AI-Roguelite duplication
            raise OpReject(f"cannot transfer to {dest}: container full or slot busy")   # guard)
        it["loc"], it["owner"] = dest, new_owner
    elif kind == "effect_add":                  # RPG-3 (doc 05 §5.4): commit to the ledger
        eid = op["char"]
        snap = op.get("_snapshot") or {}        # preset bake (_enrich) — {} = open vocabulary
        req = str(snap.get("requires") or "").lower()
        if req == "female" and _entity_sex(state, eid) != "female":
            nm = snap.get("name") or op["effect"]
            raise OpReject(f"'{nm}' requires a female character — no in-world basis "
                           f"(set the sex attribute / card pronouns first)")
        effs = state.setdefault("effects", {}).setdefault(eid, {})
        fid = op.get("_eff_id") or _resolve_effect_key(effs, op["effect"]) or slug(op["effect"])
        rec = effs.get(fid)
        if rec is None:                         # new effect: preset snapshot, op fields win
            rec = {"id": fid,
                   "name": str(snap.get("name") or str(op["effect"]).strip()),
                   "kind": str(op.get("kind") or snap.get("kind") or "condition"),
                   "valence": str(op.get("valence") or snap.get("valence") or "neutral"),
                   "stacks": _clamp(op.get("stacks", 1), 1, 99) or 1,
                   "gained_turn": turn,
                   "mods": dict(snap.get("mods") or {}),   # engine-owned; never from the wire
                   "preset": bool(snap)}
            dur = op.get("duration", snap.get("duration"))
            if dur is not None:
                rec["duration"] = max(0, int(dur))
            if op.get("note"):
                rec["note"] = str(op["note"])
            effs[fid] = rec
        else:                                   # re-applied: refresh the clock, merge fields
            rec["gained_turn"] = turn
            if op.get("stacks") is not None:
                rec["stacks"] = _clamp(op["stacks"], 1, 99) or 1
            if op.get("valence"):
                rec["valence"] = str(op["valence"])
            if op.get("duration") is not None:
                rec["duration"] = max(0, int(op["duration"]))
            if op.get("note"):
                rec["note"] = str(op["note"])
    elif kind == "effect_remove":               # RPG-3: lift it from the ledger
        eid = op["char"]
        effs = state.get("effects", {}).get(eid) or {}
        fid = _resolve_effect_key(effs, op["effect"])
        if fid is None:
            raise OpReject(f"'{op['effect']}' is not affecting {eid} — nothing to remove "
                           f"(the ledger, not the prose, is what's true)")
        del effs[fid]
    elif kind == "effect_update":               # RPG-3: the dynamic-valence channel
        eid = op["char"]
        effs = state.get("effects", {}).get(eid) or {}
        fid = _resolve_effect_key(effs, op["effect"])
        if fid is None:
            raise OpReject(f"'{op['effect']}' is not affecting {eid} — add it before "
                           f"updating it")
        rec = effs[fid]
        if op.get("valence"):
            rec["valence"] = str(op["valence"])   # blessing/perspective reframes it (doc 05 §5.4)
        if op.get("stacks") is not None:
            rec["stacks"] = _clamp(op["stacks"], 1, 99) or 1
        if op.get("duration") is not None:
            rec["duration"] = max(0, int(op["duration"]))
        if op.get("note"):
            rec["note"] = str(op["note"])
    elif kind == "ability_grant":               # RPG-3: the earned-acquisition route (doc 10)
        eid = op["char"]
        pl = state.get("player", {}).get(eid)
        if not isinstance(pl, dict):
            raise OpReject(f"no player card for '{eid}': abilities are granted to tracked "
                           f"characters with a card (seed one first)")
        aid = slug(op["ability"])
        adef = op.get("def")
        if isinstance(adef, dict):              # freestyle-authored -> FROZEN per-character def
            pl.setdefault("defs", {}).setdefault("abilities", {})[aid] = adef
        elif not op.get("_known") and aid not in ((pl.get("defs") or {}).get("abilities") or {}):
            raise OpReject(f"unknown ability '{op['ability']}': add a registry entry or a "
                           f"frozen def — power is acquired in-world, never declared")
        known = pl.setdefault("abilities", [])
        if aid not in known:
            known.append(aid)
    elif kind == "affinity_adj":                # RPG-3b (doc 07 §7.7): reason-tagged ledger
        peid = _player_eid(state)
        if peid is None:
            raise OpReject("no Player Card: affinity is measured from the player "
                           "(seed one first — doc 06 §2.4)")
        tgt = op["target"]
        if tgt == peid:
            raise OpReject("affinity toward the player themselves is not a thing")
        kindv = op.get("kind") or ("faction" if state.get("entities", {}).get(tgt, {})
                                   .get("kind") == "faction" else "npc")
        entry = state.setdefault("affinity", {}).setdefault(
            f"{peid}->{tgt}", {"value": 0, "kind": kindv, "ledger": [], "labels": []})
        d = _clamp(op.get("_delta", op["delta"]), *AFFINITY_DELTA_CLAMP)
        entry["ledger"].append({"turn": turn, "delta": d,
                                "reason": str(op.get("reason") or "")[:120]})
        del entry["ledger"][:-50]               # bounded tail (memory keeps the long story)
        entry["value"] = _clamp(entry["value"] + d, *AFFINITY_CLAMP)   # tier DERIVED at render
        if kindv == "faction":                  # factions carry structured world state too
            f = state.setdefault("factions", {}).setdefault(tgt, {"circumstances": {}})
            f.setdefault("name", state.get("entities", {}).get(tgt, {}).get("name", tgt))
    elif kind in ("set_soulmate", "set_nemesis"):   # RPG-3b (doc 07 §7.8): demote-then-set
        ptr = "soulmate" if kind == "set_soulmate" else "nemesis"
        peid = _player_eid(state)
        pl = state.get("player", {}).get(peid) if peid else None
        if not isinstance(pl, dict):
            raise OpReject(f"no Player Card: a {ptr} is the player's bond (seed one first)")
        tgt = op["target"]                      # None -> clear the bond
        incumbent = pl.get(ptr)
        if incumbent and incumbent != tgt:      # DEMOTE first — uniqueness by construction
            aff = state.setdefault("affinity", {})
            if f"{peid}->{incumbent}" in aff:
                labels = aff[f"{peid}->{incumbent}"].setdefault("labels", [])
                lbl = str(op.get("demote_label")
                          or ("beloved" if ptr == "soulmate" else "rival"))
                if lbl not in labels:
                    labels.append(lbl)
        pl[ptr] = tgt                           # THEN set (or clear); the one_soulmate
        #                                         linter owns eligibility (doc 08 §4)
    elif kind == "world_flag":                  # RPG-3b (doc 05 §5.6): standing world truth
        key = slug(op["key"])
        val = op["value"]                       # null = clear the flag
        if op.get("faction"):                   # faction-scoped circumstance
            fid = op["faction"]
            f = state.setdefault("factions", {}).setdefault(fid, {"circumstances": {}})
            f.setdefault("name", state.get("entities", {}).get(fid, {}).get("name", fid))
            circ = f.setdefault("circumstances", {})
            if val is None:
                circ.pop(key, None)
            else:
                circ[key] = val
                while len(circ) > 24:           # bounded, oldest-first (pure fn of ops)
                    circ.pop(next(iter(circ)))
        else:
            w = state.setdefault("world", {})
            if val is None:
                w.pop(key, None)
            else:
                w[key] = val
                while len(w) > 32:
                    w.pop(next(iter(w)))
    elif kind == "item_gain":                   # RPG-5 (G2): the organic acquisition channel.
        owner, name = op["char"], str(op["name"]).strip()   # Re-tagged acquisitions STACK on
        items = state.setdefault("items", {})               # the existing row — never a dupe
        low = name.lower()                                  # (the AI-Roguelite failure mode).
        hit = next((i for i, it0 in items.items()
                    if (it0 or {}).get("owner") == owner and (it0 or {}).get("loc") != "gone"
                    and str((it0 or {}).get("name", "")).lower() == low), None)
        if hit:
            items[hit]["qty"] = int(items[hit].get("qty", 1)) + max(1, int(op.get("qty", 1)))
        else:
            base = slug(name)[:48]
            iid, n = base, 1
            while iid in items:                 # pure fn of (state, op): replay-deterministic
                n += 1
                iid = f"{base}#{n}"
            snap = op.get("_snapshot") or {}    # template bake (_enrich) — {} = mechanics-free
            loc = f"inv:{_default_container(state, owner)}"
            rec = {"template_id": snap.get("_template"), "name": snap.get("name", name),
                   "qty": max(1, int(op.get("qty", 1))), "loc": loc, "owner": owner,
                   "mods_snapshot": dict(snap.get("mods") or {}), "minted_turn": turn}
            for k in ("slot", "covers", "on_consume", "stackable", "max_stack",
                      "capacity", "is_container", "worn", "type"):
                if k in snap:
                    rec[k] = snap[k]
            items[iid] = rec
            if not _index_add(state, owner, iid, loc):
                del items[iid]                  # transactional rollback (doc 07 §7)
                raise OpReject(f"cannot add '{name}': inventory full")
    elif kind == "item_lose":                   # RPG-5 (G2): narrated loss, ledger-checked
        owner, name = op["char"], str(op["name"]).strip()
        items = state.get("items", {})
        low = name.lower()
        hits = [i for i, it0 in items.items()
                if (it0 or {}).get("owner") == owner and (it0 or {}).get("loc") != "gone"
                and str((it0 or {}).get("name", "")).lower() == low]
        if not hits:
            iid0 = _resolve_instance(state, name)
            if iid0 and (items.get(iid0) or {}).get("owner") == owner:
                hits = [iid0]
        if not hits:
            raise OpReject(f"'{name}' is not in {owner}'s ledger — nothing to lose "
                           f"(the ledger, not the prose, is what's true)")
        it = items[hits[0]]
        q = int(it.get("qty", 1))
        if q > 1:
            it["qty"] = q - 1
        else:
            _index_remove(state, hits[0])
            it["loc"], it["owner"] = "gone", None
    elif kind == "quest_add":                   # RPG-5 (G3): the quest ledger family
        qid = slug(op["name"])[:64]
        qs = state.setdefault("quests", {})
        if qid not in qs:                       # near-dupe guard (2026-07-07 live repro:
            new_toks = {w for w in qid.split("_") if len(w) >= 4}   # 'takoba_arena' beside
            for k, r in qs.items():             # 'pass_the_..._at_takoba_arena') — an ACTIVE
                if (r or {}).get("status") != "active" or not new_toks:   # quest whose name
                    continue                    # tokens contain/are-contained by the new
                old_toks = {w for w in k.split("_") if len(w) >= 4}   # name is the SAME quest
                if old_toks and (new_toks <= old_toks or old_toks <= new_toks):
                    qid = k
                    break
        if qid in qs:                           # re-add refreshes, never duplicates
            q = qs[qid]
            q["updated_turn"] = turn
            if op.get("detail"):
                q["detail"] = str(op["detail"])[:300]
        else:
            q = {"name": str(op["name"]).strip()[:120], "status": "active",
                 "created_turn": turn, "updated_turn": turn}
            for k in ("detail", "giver"):
                if op.get(k):
                    q[k] = str(op[k])[:300]
            if op.get("stakes") in QUEST_STAKES:
                q["stakes"] = op["stakes"]
            qs[qid] = q
            while len(qs) > 40:                 # bounded: settled quests age out first
                dead = next((k for k, r in qs.items()
                             if (r or {}).get("status") != "active"), next(iter(qs)))
                qs.pop(dead)
    elif kind == "quest_update":
        qs = state.setdefault("quests", {})
        tok = str(op["quest"]).strip()
        qid = tok if tok in qs else slug(tok)[:64]
        if qid not in qs:
            low = tok.lower()
            hits = [k for k, r in qs.items()
                    if str((r or {}).get("name", "")).lower() == low]
            if len(hits) != 1:                  # token-subset fallback, UNIQUE hit only
                toks = {w for w in slug(tok).split("_") if len(w) >= 4}
                hits = [k for k in qs if toks and (
                    toks <= {w for w in k.split("_") if len(w) >= 4}
                    or {w for w in k.split("_") if len(w) >= 4} <= toks)] if toks else []
            if len(hits) != 1:
                raise OpReject(f"unknown quest '{tok}' — record it first "
                               f"([quest | {tok} | new])")
            qid = hits[0]
        q = qs[qid]
        if op.get("status") in QUEST_STATUSES and q.get("status") != op["status"]:
            q["status"] = op["status"]
            if op["status"] == "complete":
                q["completed_turn"] = turn
        if op.get("note"):
            q["note"] = str(op["note"])[:300]
        q["updated_turn"] = turn
    elif kind == "hp_adj":                      # RPG-5 (G7): bounded consequence channel —
        pl = state.get("player", {}).get(op["char"])   # the narrator proposes severity, the
        if not isinstance(pl, dict) or not isinstance(pl.get("hp"), dict) \
                or not pl["hp"].get("max"):     # clamp (baked) owns the number
            raise OpReject(f"no HP pool for '{op['char']}' — hp_adj tracks the Player Card")
        hp = pl["hp"]
        d = int(op.get("_delta", op["delta"]))
        hp["cur"] = _clamp(int(hp.get("cur", hp["max"])) + d, 0, int(hp["max"]))
    elif kind == "award_exp":                   # RPG-5 progression (privileged, code-awarded)
        pl = state.get("player", {}).get(op["char"])
        if not isinstance(pl, dict):
            raise OpReject("no Player Card: XP is the player's (seed one first)")
        pl["xp"] = _clamp(int(pl.get("xp", 0)) + max(0, int(op["amount"])), 0, 10**7)
    elif kind == "level_up":
        pl = state.get("player", {}).get(op["char"])
        if not isinstance(pl, dict):
            raise OpReject("no Player Card to level")
        pl["level"] = _clamp(int(pl.get("level", 1)) + 1, 1, 999)
        g = op.get("_grants") or {}             # curated grants, baked at _enrich
        hp = pl.setdefault("hp", {"cur": 0, "max": 0})
        inc = int(g.get("hp", 0))
        hp["max"] = _clamp(int(hp.get("max", 0)) + inc, 0, 10**6)
        hp["cur"] = _clamp(int(hp.get("cur", 0)) + inc, 0, hp["max"])
        for r in (pl.get("resources") or {}).values():
            if isinstance(r, dict):
                r["max"] = _clamp(int(r.get("max", 0)) + int(g.get("pool", 0)), 0, 10**6)
                r["cur"] = _clamp(int(r.get("cur", 0)) + int(g.get("pool", 0)), 0, r["max"])
        pl["stat_points"] = int(pl.get("stat_points", 0)) + int(g.get("stat_points", 0))
    elif kind == "stat_spend":                  # spend a banked point: +1 to a stat, -1 point.
        pl = state.get("player", {}).get(op["char"])   # TRANSACTIONAL — reject before mutation.
        if not isinstance(pl, dict):
            raise OpReject("no Player Card to raise")
        if int(pl.get("stat_points", 0)) < 1:
            raise OpReject("no banked stat points to spend")
        stat = str(op["stat"]).upper()
        stats = pl.setdefault("stats", {})
        if stat not in stats:
            raise OpReject(f"unknown stat {stat!r} on this card")
        cap = int(op.get("_max", 20))           # registry max baked at _enrich (replay-pure)
        if int(stats[stat]) >= cap:
            raise OpReject(f"{stat} is already at its max ({cap})")
        stats[stat] = int(stats[stat]) + 1
        pl["stat_points"] = int(pl.get("stat_points", 0)) - 1
    elif kind == "master_tick":                 # RPG-5 (doc 10 §4): use grows mastery —
        pl = state.get("player", {}).get(op["char"])   # scene-capped so spam can't cheese it
        if not isinstance(pl, dict):
            raise OpReject("no Player Card for mastery")
        sid = str(op["skill"])
        mastery = pl.setdefault("mastery", {})
        si = state.get("scene", {}).get("scene_index", 0)
        ms = pl.get("mastery_scene")
        if not isinstance(ms, dict) or ms.get("scene_index") != si:
            ms = {"scene_index": si, "ticks": {}}
            pl["mastery_scene"] = ms
        used = int(ms["ticks"].get(sid, 0))
        amt = min(max(0, int(op["amount"])), max(0, MASTERY_SCENE_CAP - used))
        if amt:
            ms["ticks"][sid] = used + amt
            mastery[sid] = min(MASTERY_CAP, int(mastery.get(sid, 0)) + amt)
    elif kind == "evolve_def":                  # RPG-5: the Q27 re-snapshot loop lands here —
        pl = state.get("player", {}).get(op["char"])   # a new FROZEN def version; the journal
        if not isinstance(pl, dict):            # keeps every prior version for replay
            raise OpReject("no Player Card to evolve")
        table = str(op["table"])
        if table not in ("skills", "abilities") or not isinstance(op.get("def"), dict):
            raise OpReject("evolve_def needs table skills|abilities and an authored def")
        pl.setdefault("defs", {}).setdefault(table, {})[str(op["id"])] = dict(op["def"])
    elif kind == "defeat_resolve":              # RPG-5 (doc 10 §7): defeat, not death (unless
        pl = state.get("player", {}).get(op["char"])            # hardcore chose the outcome)
        if not isinstance(pl, dict) or not isinstance(pl.get("hp"), dict):
            raise OpReject("no Player Card to defeat")
        outcome = str(op["outcome"])
        pl["defeated"] = {"turn": turn, "outcome": outcome}
        hp = pl["hp"]
        if outcome == "death":                  # hardcore: the ledger records it; nothing revives
            hp["cur"] = 0
        else:
            hp["cur"] = max(1, int(hp.get("max", 0)) // 4)
            if outcome == "robbed":             # consequences via ordinary item state
                for iid, it in state.get("items", {}).items():
                    if (it or {}).get("owner") == op["char"] \
                            and str(it.get("loc", "")).startswith("inv:") \
                            and not it.get("bound"):
                        _index_remove(state, iid)
                        it["loc"], it["owner"] = "world", None
        eff = op.get("_effect") or {}           # baked condition (Battered / Dead)
        if eff.get("id"):
            effs = state.setdefault("effects", {}).setdefault(op["char"], {})
            effs[eff["id"]] = {**eff, "gained_turn": turn}
    elif kind == "stagnation":
        state["scene"]["stagnation"] = round(float(op["value"]), 3)
    elif kind == "player_seed":
        _apply_player_seed(state, op)
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


# ---- RPG-3b faction cascade (doc 05 §5.4) — pure, deterministic, cold-path ----------
def faction_cascade_ops(state: dict, applied: list[dict], factor: float = 0.1) -> list[dict]:
    """Extraction-applied NPC affinity shifts ripple to the NPC's faction (membership = the
    entity's `faction` attribute), scaled by `factor` and HALVED on negatives — the
    AI-Roguelite anti-death-spiral default; treat as tunable ([specialization]
    faction_cascade). Returns rule-source affinity_adj ops for the caller to apply —
    propose-then-commit, journaled, never a hidden reducer side-effect (pillar 2). Cascades
    never chain: a faction-targeted op emits nothing."""
    out: list[dict] = []
    if not isinstance(state, dict) or not applied or not factor:
        return out
    ents = state.get("entities", {})
    amap: dict[str, str] = {}
    for eid, e in ents.items():
        amap[str((e or {}).get("name", "")).lower()] = eid
        amap[eid] = eid
    for op in applied:
        if not isinstance(op, dict) or op.get("op") != "affinity_adj":
            continue
        tgt = op.get("target")
        if ents.get(tgt, {}).get("kind") == "faction":
            continue                            # no chains
        fac = state.get("attributes", {}).get(tgt, {}).get("faction")
        fid = amap.get(str(fac or "").strip().lower())
        if not fid or ents.get(fid, {}).get("kind") != "faction":
            continue
        try:
            d = float(op.get("_delta", op.get("delta", 0)))
        except (TypeError, ValueError):
            continue
        raw = d * float(factor) * (0.5 if d < 0 else 1.0)
        step = int(raw + 0.5) if raw > 0 else (int(raw - 0.5) if raw < 0 else 0)
        if step:
            out.append({"op": "affinity_adj", "target": fid, "delta": step,
                        "kind": "faction",
                        "reason": f"standing with {ents.get(tgt, {}).get('name', tgt)}"})
    return out


# ---- RPG-5 progression pass (doc 10) — pure, deterministic, journaled ---------------
def _defeat_outcome(state: dict, peid: str) -> str:
    """Contextual non-lethal outcome CLASS, code-decided (doc 10 M5): judged from the
    player's standing with whoever is present. The LLM flavors within the class."""
    aff = state.get("affinity") or {}
    present = [eid for eid, e in (state.get("entities") or {}).items()
               if isinstance(e, dict) and e.get("present") and eid != peid]
    worst, best = 0, 0
    for eid in present:
        rec = aff.get(f"{peid}->{eid}")
        if isinstance(rec, dict):
            v = int(rec.get("value", 0))
            worst, best = min(worst, v), max(best, v)
    if worst <= -40:
        return "captured"
    if worst <= -10:
        return "robbed"
    if best >= 10:
        return "rescued"
    return "wake_safe"


def progression_ops(state: dict, applied: list[dict], hardcore: bool = False) -> list[dict]:
    """RPG-5 (doc 10): code-awarded progression — XP from quest/goal completion and positive
    standing-tier crossings; level_up when the curve is crossed; defeat_resolve when the
    Player's HP hits 0. Pure reader over (state, this batch's applied ops); returns
    PRIVILEGED rule ops for the caller to apply — propose-then-commit, journaled, never a
    hidden reducer side-effect (pillar 2). Values are curated (XP_AWARDS), never model-typed."""
    out: list[dict] = []
    peid = _player_eid(state)
    if not peid:
        return out
    pl = state.get("player", {}).get(peid) or {}
    turn = state.get("meta", {}).get("turn", -1)
    xp_add = 0
    for op in (applied or []):
        if not isinstance(op, dict):
            continue
        k = op.get("op")
        if k == "quest_update" and op.get("status") == "complete":
            qs = state.get("quests") or {}
            tok = str(op.get("quest", "")).strip()
            q = qs.get(tok) or qs.get(slug(tok)[:64]) or next(
                (r for r in qs.values()
                 if str((r or {}).get("name", "")).lower() == tok.lower()), {})
            stakes = str((q or {}).get("stakes") or "minor")
            amt = XP_AWARDS.get(f"quest_{stakes}", XP_AWARDS["quest_minor"])
            out.append({"op": "award_exp", "char": peid, "amount": amt,
                        "reason": f"quest complete: {(q or {}).get('name', tok)}"})
            xp_add += amt
        elif k == "goal" and op.get("action") == "complete":
            out.append({"op": "award_exp", "char": peid, "amount": XP_AWARDS["goal"],
                        "reason": f"completed: {str(op.get('text', ''))[:60]}"})
            xp_add += XP_AWARDS["goal"]
        elif k == "defeat_resolve":
            pass                                 # defeat never awards the defeated
        elif k == "affinity_adj":
            rec = (state.get("affinity") or {}).get(f"{peid}->{op.get('target')}")
            if isinstance(rec, dict):
                try:
                    d = int(op.get("_delta", op.get("delta", 0)) or 0)
                except (TypeError, ValueError):
                    d = 0
                after = int(rec.get("value", 0))
                if d > 0 and after >= 40 \
                        and affinity_tier(after) != affinity_tier(after - d):
                    out.append({"op": "award_exp", "char": peid,
                                "amount": XP_AWARDS["faction_tier"],
                                "reason": f"standing risen to {affinity_tier(after)}"})
                    xp_add += XP_AWARDS["faction_tier"]
    # level-ups from the projected total (may cross several thresholds at once)
    want = xp_level(int(pl.get("xp", 0)) + xp_add)
    have = int(pl.get("level", 1))
    for _ in range(max(0, want - have)):
        out.append({"op": "level_up", "char": peid})
    # defeat: HP floored at 0 and not already resolved this turn (death is final)
    hp = pl.get("hp") or {}
    defeated = pl.get("defeated") or {}
    if isinstance(hp, dict) and int(hp.get("max", 0)) > 0 and int(hp.get("cur", 1)) <= 0 \
            and str(defeated.get("outcome", "")) != "death" \
            and int(defeated.get("turn", -10**9)) != turn:
        out.append({"op": "defeat_resolve", "char": peid,
                    "outcome": "death" if hardcore else _defeat_outcome(state, peid)})
    return out


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


def _enrich(op: dict, turn: int, cfg, state: Optional[dict] = None) -> dict:
    """Bake config/registry-dependent values into the journaled op (replay determinism,
    03 SS3.3; doc 07 §6 extends the bake to item reference data). `state` is the pre-apply
    snapshot — used only to generate a fresh, unique instance id at mint."""
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
    if op["op"] == "item_mint":                # RPG-2 (doc 07 §6): bake the template snapshot +
        from . import registry as _registry    # the generated iid — never re-read at replay, so
        try:                                    # editing items.toml can't rewrite a minted item
            tpl = _registry.load(cfg).items.get(str(op.get("template", "")))
        except Exception:
            tpl = None
        if isinstance(tpl, dict):
            out["_snapshot"] = {k: tpl[k] for k in (
                "name", "worn", "slot", "mods", "covers", "on_consume", "stackable",
                "max_stack", "capacity", "is_container", "type") if k in tpl}
            existing = (state or {}).get("items", {})
            n = sum(1 for i in existing if str(i).startswith(str(op["template"]) + "#")) + 1
            iid = f'{op["template"]}#{n}'
            while iid in existing:
                n += 1
                iid = f'{op["template"]}#{n}'
            out["_iid"] = iid
    if op["op"] == "item_equip":               # RPG-2: slot validity vs the profile-extended set,
        from . import registry as _registry    # baked so the reducer stays registry-free
        try:
            out["_slot_ok"] = str(op.get("slot", "")) in _registry.load(cfg).slots
        except Exception:
            out["_slot_ok"] = str(op.get("slot", "")) in GEAR_SLOTS
    if op["op"] == "effect_add":               # RPG-3: bake the PRESET snapshot (floor for weak
        from . import registry as _registry    # models); an unknown name stays open-vocabulary
        try:                                    # — it still commits, with no engine mechanics
            reg = _registry.load(cfg)
            fid = reg.resolve_effect(str(op.get("effect", "")))
        except Exception:
            fid = None
        if fid:
            e = reg.effects.get(fid) or {}
            out["_eff_id"] = fid
            out["_snapshot"] = {k: e[k] for k in (
                "name", "kind", "valence", "mods", "duration", "requires") if k in e}
    if op["op"] == "stat_spend":               # bake the stat's registry max (replay-pure clamp)
        from . import registry as _registry
        try:
            sdef = _registry.load(cfg).stats.get(str(op.get("stat", "")).upper()) or {}
            out["_max"] = int(sdef.get("max", 20))
        except Exception:
            out["_max"] = 20
    if op["op"] == "ability_grant":            # RPG-3: registry membership baked (replay purity)
        from . import registry as _registry
        try:
            out["_known"] = slug(str(op.get("ability", ""))) in _registry.load(cfg).abilities
        except Exception:
            out["_known"] = False
    if op["op"] == "affinity_adj":             # RPG-3b (doc 07 §6): the per-turn clamp is baked
        out["_delta"] = _clamp(op.get("delta", 0), *AFFINITY_DELTA_CLAMP)   # so history is
                                               # stable even if the constant is tuned later
    if op["op"] == "item_gain":                # RPG-5 (G2): template-snapshot floor — a name
        from . import registry as _registry    # matching a curated template grounds its
        try:                                    # mechanics; anything else commits MECHANICS-FREE
            reg = _registry.load(cfg)           # (no power minted from prose)
            want = str(op.get("name", "")).strip().lower()
            tid = next((t for t, tp in reg.items.items()
                        if t == slug(want) or str((tp or {}).get("name", "")).lower() == want),
                       None)
        except Exception:
            tid = None
        if tid:
            tpl = reg.items.get(tid) or {}
            out["_snapshot"] = {**{k: tpl[k] for k in (
                "name", "worn", "slot", "mods", "covers", "on_consume", "stackable",
                "max_stack", "capacity", "is_container", "type") if k in tpl},
                "_template": tid}
    if op["op"] == "hp_adj":                   # RPG-5 (G7): per-op swing clamp baked — the
        try:                                    # narrator proposes, the clamp owns the number
            mx = int((((state or {}).get("player", {}).get(op.get("char")) or {})
                      .get("hp") or {}).get("max", 0))
        except Exception:
            mx = 0
        cap = max(HP_ADJ_MIN_CAP, mx // 4)
        out["_delta"] = _clamp(op.get("delta", 0), -cap, cap)
    if op["op"] == "level_up":                 # RPG-5: curated grants baked (tuning the table
        out.setdefault("_grants", dict(LEVEL_GRANTS))   # later never rewrites old level-ups)
    if op["op"] == "master_tick":              # RPG-5: bracket crossing baked for the Q27
        try:                                    # evolution hook (jobs reads _bracket_up)
            pl = ((state or {}).get("player", {}) or {}).get(op.get("char")) or {}
            sid = str(op.get("skill"))
            cur = int((pl.get("mastery") or {}).get(sid, 0))
            si = ((state or {}).get("scene", {}) or {}).get("scene_index", 0)
            ms = pl.get("mastery_scene") if isinstance(pl.get("mastery_scene"), dict) else {}
            used = int((ms.get("ticks") or {}).get(sid, 0)) if ms.get("scene_index") == si else 0
            eff_amt = min(max(0, int(op.get("amount", 0))), max(0, MASTERY_SCENE_CAP - used))
            b0 = mastery_bracket(cur)[0]
            b1 = mastery_bracket(min(MASTERY_CAP, cur + eff_amt))[0]
            if b1 != b0:
                out["_bracket_up"] = b1
        except Exception:
            pass
    if op["op"] == "defeat_resolve":           # RPG-5: the outcome's condition baked
        if str(op.get("outcome")) == "death":
            out.setdefault("_effect", {"id": "dead", "name": "Dead", "kind": "condition",
                                       "valence": "negative", "mods": {}, "preset": True})
        else:
            out.setdefault("_effect", {"id": "battered", "name": "Battered",
                                       "kind": "condition", "valence": "negative",
                                       "duration": 6, "mods": {"all": -1}, "preset": True})
    if op["op"] == "scene_set" and "location" in op \
            and getattr(cfg, "specialization", None) is not None \
            and cfg.specialization.name == "rpg":
        # RPG-4 (doc 05 §9): canonical location baked into the journaled op — replay never
        # re-resolves; a `none` session's ops stay byte-identical (no _canon keys).
        loc_id, disp, is_new = canonical_location(state or {}, op["location"])
        out["location"] = loc_id
        out["_canon"] = 1
        if is_new:
            out["_loc_create"] = {"eid": loc_id, "name": disp}
        else:
            raw_head = _LOC_HEAD_RE.split(str(op["location"]).strip(), 1)[0].strip()
            ent = (state or {}).get("entities", {}).get(loc_id) or {}
            names = [ent.get("name", "")] + [str(a) for a in ent.get("aliases", [])]
            if raw_head and all(_norm_loc(raw_head) != _norm_loc(n) for n in names if n):
                out["_loc_alias"] = raw_head   # learn the variant: canon self-improves
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
        op2 = _enrich(op2, turn, cfg, res.state)
        try:
            _apply_op(res.state, op2)
        except OpReject as exc:                 # transactional reject (doc 07 §7): visible reason
            res.quarantined.append({"op": op, "reason": str(exc)})
            continue
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


def translate_path(path: str, value: str, rpg: bool = False) -> Optional[dict]:
    """((aether.set path value)) / PATCH paths -> typed ops (02 SS12b surfaces).
    Unknown path -> None (rejected with visible reason — never silently applied).
    `rpg` unlocks the RPG-3b paths (world.<key> / affinity.<target> / player.soulmate|
    nemesis) — gated so a `none` session's command surface is unchanged (invariant 3)."""
    path = _BARE_ALIASES.get(path.strip().lower(), path.strip())
    if rpg:
        low = path.lower()
        v0 = _unquote(value)
        clear = v0.strip().lower() in ("none", "null", "clear", "-", "")
        if low.startswith("world.") and len(path) > 6:
            val: Any = None if clear else v0
            if isinstance(val, str):                    # scalar coercion: true/false/yes/no/int/str
                if val.lower() in ("true", "false", "yes", "no", "on", "off"):
                    val = val.lower() in ("true", "yes", "on")
                else:
                    try:
                        val = int(val)
                    except ValueError:
                        pass
            return {"op": "world_flag", "key": path[6:], "value": val}
        if low.startswith("affinity.") and len(path) > 9:
            try:
                return {"op": "affinity_adj", "target": path[9:], "delta": int(v0),
                        "reason": "user edit"}
            except ValueError:
                return None                             # delta must be a signed integer
        if low in ("player.soulmate", "player.nemesis"):
            return {"op": "set_soulmate" if low.endswith("soulmate") else "set_nemesis",
                    "target": None if clear else v0}
        if low.startswith("quest.") and len(path) > 6:     # RPG-5 (G3): quest ledger paths
            tok = path[6:]
            if clear or v0.lower() in ("abandoned", "abandon", "drop"):
                return {"op": "quest_update", "quest": tok, "status": "abandoned"}
            vv = v0.lower()
            if vv in ("new", "add", "start", "started"):
                return {"op": "quest_add", "name": tok.replace("_", " ")}
            if vv in ("complete", "completed", "done"):
                return {"op": "quest_update", "quest": tok, "status": "complete"}
            if vv in ("failed", "fail"):
                return {"op": "quest_update", "quest": tok, "status": "failed"}
            if vv == "active":
                return {"op": "quest_update", "quest": tok, "status": "active"}
            return {"op": "quest_update", "quest": tok, "note": v0}
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
                           "location": e.get("location_id"),
                           "kind": e.get("kind", "character")}
                     for eid, e in state.get("entities", {}).items()},
        "attributes": state.get("attributes", {}),
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
        "player": state.get("player", {}),
        "items": state.get("items", {}),
        "gear": state.get("gear", {}),
        "inventory": state.get("inventory", {}),
        "effects": state.get("effects", {}),
        "quests": state.get("quests", {}),
        # RPG-3b: affinity with the DERIVED tier attached (the ledger tail trimmed for the
        # payload), plus factions + world flags — the Console renders these directly.
        "affinity": {k: {**{kk: vv for kk, vv in rec.items() if kk != "ledger"},
                         "ledger": (rec.get("ledger") or [])[-10:],
                         "tier": affinity_tier(rec.get("value", 0))}
                     for k, rec in state.get("affinity", {}).items()
                     if isinstance(rec, dict)},
        "factions": state.get("factions", {}),
        "world": state.get("world", {}),
        "turn": state.get("meta", {}).get("turn", -1),
    }
