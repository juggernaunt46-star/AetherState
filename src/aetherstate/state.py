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
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .enemy_capability_pool import (
    EnemyCapabilityPoolError,
    compile_enemy_candidates,
    compile_enemy_capability_bundle,
    reconstruct_enemy_kit,
    seal_enemy_hp_receipt_admission,
    validate_enemy_capability_bundle,
)
from .enemy_kits import (build_enemy_kit, grounded_actor_armament,
                         intent_matches_frozen_kit, select_enemy_intent)
from .knowledge import (
    normalize_actor_id,
    normalized_proposition,
    polarized_proposition_id,
    proposition_id,
)
from .worldlex import DefinitionRef, SubjectRef, validate_pool
from .worldlex_assignment import (AssignmentError, materialize_assignment,
                                  validate_assignment)
from .worldlex_store import WorldLexError
from .world_events import project_world_overlay, validate_world_event_record
from .turn_trace import canonical_sha256, emit_turn_trace, safe_token, text_receipt

log = logging.getLogger("aetherstate.state")

SCHEMA = "aetherstate/1"
BIG_TURN = 2**31
_SEMANTIC_FRAME_REF_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _replay_intent_v1_ok(intent: Any, row: Any) -> bool:
    """Immutable enemy-intent/1 membership check used only while replaying baked journals.

    Live generation may evolve, but an already accepted v1 kit/intent pair remains historical
    authority.  Keeping this reducer check local prevents a future generator matcher from
    silently changing old combat state.
    """
    if not isinstance(intent, dict) or not isinstance(row, dict):
        return False
    kit = row.get("kit")
    if intent.get("schema") != "enemy-intent/1" or not isinstance(kit, dict) \
            or kit.get("schema") != "enemy-kit/1" \
            or str(intent.get("actor")) != str(row.get("id")):
        return False
    moves = kit.get("moves")
    if not isinstance(moves, list):
        return False
    move = next((candidate for candidate in moves if isinstance(candidate, dict)
                 and candidate.get("id") == intent.get("move_id")), None)
    if not isinstance(move, dict):
        return False
    fields = {"name": "move_name", "primitive": "primitive", "basis": "basis",
              "channel": "channel", "target_rule": "target_rule", "delivery": "delivery",
              "range": "range", "timing": "timing", "cadence": "cadence",
              "danger": "danger", "accuracy": "accuracy", "damage": "damage",
              "tell": "tell", "counterplay": "counterplay", "sensory": "sensory",
              "risk": "risk", "forbid": "forbid", "reaction": "reaction"}
    return all(intent.get(intent_key) == move.get(move_key)
               for move_key, intent_key in fields.items())

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
CHECK_ROLL_SHAPE_SCHEMA = "check-roll-shape/1"
# Universal baseline skills every Player Card owns at rank 0 (2026-07-11): the genre-neutral human
# floor — anyone can observe, move their body, talk, and fight at a basic level — so an authored kit
# that fully specializes (e.g. all diving skills) never leaves the player unable to attempt a
# perception, social, physical, or melee action. All are non-gated registry skills; authored ranks
# always win, these only fill ABSENT ids. Baked into the player_seed op at seed time (genesis +
# creator) so the reducer stores what it is given (replay-pure); rpg-gated by the Player Card only
# existing under rpg.
BASELINE_SKILLS = ("perception", "athletics", "persuasion", "brawl")


def merge_baseline_skills(skills):
    """Return `skills` with each BASELINE_SKILLS id present at >= rank 0 (authored ranks kept)."""
    out = dict(skills) if isinstance(skills, dict) else {}
    for sid in BASELINE_SKILLS:
        out.setdefault(sid, 0)
    return out


# ---- RPG-2 items vocab (docs 06 §3.5 / 07 §1): instance locations + gear slots.
# GEAR_SLOT_ORDER is the render order; the AUTHORITATIVE slot set may be extended per-table via
# the registry (meta.toml `extra_slots`) — validity is baked onto item_equip at _enrich time so
# the reducer never reads the registry (replay purity).
ITEM_LOC_KINDS = {"gear", "inv", "world", "gone"}
GEAR_SLOT_ORDER = ["head", "face", "neck", "shoulders", "body", "cape", "arms", "hands",
                   "mainhand", "offhand", "waist", "legs", "feet", "back",
                   "accessory1", "accessory2"]
GEAR_SLOTS = set(GEAR_SLOT_ORDER)
# ---- Item CLASS: gear vs inventory (Bean 2026-07-07) --------------------------------------
# "Gear = weapons, equipment, tools, accessories, bags; inventory = consumables, phones,
# perfume, materials, everything else." The split is by CLASS, not merely by whether a piece
# is in a body slot right now: a sheathed sword is still gear. Worn gear whose slot is free
# AUTO-EQUIPS on acquisition, so starting kit shows on the paper-doll (the worn-at-start fix).
# Deterministic + template-aware -> baked at _enrich so replay stays pure (the reducer never
# re-derives it). A curated template snapshot is authoritative; a name heuristic is the floor.
GEAR_TYPES = {"weapon", "armor", "armour", "clothing", "shield", "tool", "accessory",
              "trinket", "container", "bag", "pack", "equipment", "gear", "focus", "wand",
              "staff", "implement", "instrument", "kit", "cloak", "cape", "ring", "amulet",
              "boots", "gloves", "helm", "helmet", "belt", "gadget", "cyberware", "augment",
              "wearable", "apparel", "garment", "outfit"}
INV_TYPES = {"consumable", "potion", "food", "drink", "material", "ingredient", "misc",
             "key", "phone", "device", "perfume", "cosmetic", "currency", "money", "coin",
             "valuable", "document", "note", "letter", "scroll", "drug", "component",
             "junk", "treasure", "gem", "supply", "supplies", "ration", "medicine",
             "quest_item", "keepsake", "photo", "datapad", "chip"}
# ordered name-token -> gear slot: the paper-doll auto-equip heuristic for free-form kit.
# Ordered SPECIFIC-first (the first hint that matches wins). Greatly widened 2026-07-10 (Bean:
# "high heels don't even go to feet") — plurals are listed explicitly because the compound-suffix
# match in _hit is end-anchored (it catches "longCOAT" but not the plural "coatS").
_SLOT_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("helm", "helmet", "hood", "circlet", "crown", "coif", "cap", "hat", "headband", "hairband",
      "tiara", "diadem", "coronet", "headdress", "headpiece", "turban", "beret", "bonnet",
      "fedora", "cowl", "cloche", "fascinator", "veil", "bandana", "kerchief", "hairpin",
      "hairclip", "barrette", "wreath"), "head"),
    (("goggles", "visor", "spectacles", "glasses", "monocle", "mask", "respirator", "eyepatch",
      "blindfold", "eyewear", "faceplate"), "face"),
    (("amulet", "necklace", "pendant", "torc", "torque", "collar", "choker", "locket", "medallion",
      "gorget", "cravat", "necktie", "ascot", "lanyard"), "neck"),
    (("cloak", "mantle", "cape", "robe", "robes", "shawl", "poncho", "drape", "stole",
      "capelet"), "cape"),
    (("pauldron", "pauldrons", "spaulder", "spaulders", "epaulet", "epaulette", "epaulettes",
      "mantlet", "shrug", "shoulderguard"), "shoulders"),
    (("vambrace", "vambraces", "bracer", "bracers", "armguard", "armguards", "rerebrace",
      "rerebraces", "couter", "couters"), "arms"),   # forearm/upper-arm armor -> its own slot
    (("gauntlet", "gauntlets", "glove", "gloves",
      "mitt", "mitts", "mitten", "mittens", "handwraps", "cestus"), "hands"),
    (("heel", "heels", "stiletto", "stilettos", "stilettoes", "pumps", "flats",
      "loafer", "loafers", "moccasin", "moccasins", "slipper", "slippers", "clog", "clogs",
      "wedges", "sneaker", "sneakers", "trainer", "trainers", "brogue", "brogues",
      "oxfords", "mules", "espadrille", "espadrilles", "boot", "boots",
      "shoe", "shoes", "sandal", "sandals", "greave", "greaves", "sabaton", "sabatons",
      "footwear", "sock", "socks", "stocking", "stockings", "tabi", "galoshes", "wader",
      "waders", "cleat", "cleats"), "feet"),
    (("belt", "sash", "girdle", "bandolier", "holster", "cummerbund", "waistband", "obi"), "waist"),
    (("legging", "leggings", "trousers", "pants", "breeches", "kilt", "skirt", "jeans", "tights",
      "chaps", "culottes", "pantaloons", "hakama", "shorts", "slacks", "jodhpurs",
      "cuisses", "chausses", "chausse"), "legs"),
    (("ring", "signet"), "accessory1"),
    (("satchel", "backpack", "rucksack", "knapsack", "haversack", "bag", "quiver",
      "wineskin"), "back"),
    (("shield", "buckler", "aegis", "targe", "grimoire"), "offhand"),
    (("sword", "blade", "knife", "dagger", "axe", "mace", "spear", "lance", "staff", "wand",
      "gun", "pistol", "rifle", "bow", "longbow", "shortbow", "warbow", "crossbow",
      "hammer", "katana", "cleaver", "machete",
      "baton", "club", "sabre", "saber", "revolver", "blaster", "launcher", "polearm",
      "halberd", "scythe", "sickle", "whip", "flail", "rapier", "scimitar", "glaive", "falchion",
      "warhammer", "greatsword", "longsword", "shortsword", "handaxe", "hatchet", "trident",
      "naginata", "nunchaku", "shuriken", "sling", "musket", "carbine", "shotgun"), "mainhand"),
    (("dress", "gown", "corset", "bodice", "kirtle", "chemise", "negligee", "leotard", "bodysuit",
      "catsuit", "armor", "armour", "cuirass", "breastplate", "vest", "jacket", "coat", "mail",
      "plate", "hauberk", "tunic", "shirt", "gambeson", "harness", "suit", "chestplate",
      "carapace", "duster", "parka", "overalls", "jerkin", "doublet", "brigandine", "kimono",
      "kaftan", "blouse", "sweater", "hoodie", "raincoat", "chestpiece", "flak", "rig", "exosuit",
      "exoframe", "cardigan", "tabard", "surcoat", "cassock", "frock", "smock", "toga", "sari",
      "cheongsam", "waistcoat", "poncho"), "body"),
    (("bracelet", "bangle", "talisman", "brooch", "badge", "insignia", "anklet",
      "earring", "earrings", "piercing", "cufflink", "cufflinks", "wristband", "armband",
      "charm", "charms", "trinket", "fetish"),
     "accessory2"),
]
# gear-class by NAME but with no natural body slot -> carried as "stowed gear" (tools/kits/bags)
_GEAR_NAME_TOKENS = {"lockpick", "lockpicks", "picks", "toolkit", "toolset", "tools", "tool",
                     "multitool", "kit", "medkit", "medpack", "rope", "grapple", "grappling",
                     "crowbar", "lantern", "torch", "compass", "spyglass", "binoculars",
                     "scanner", "toolbox", "pouch", "case", "holster", "sheath", "scabbard",
                     "bandolier", "webbing"}
_ITEM_WORD_RE = re.compile(r"[a-z0-9]+")


def classify_item(name: str, snap: Optional[dict] = None) -> dict:
    """Decide an item's CLASS ('gear'|'inv'), whether it is WORN, and which gear SLOT it
    occupies. A curated template snapshot wins; otherwise a deterministic name heuristic
    grounds it (weak-model floor). Pure fn of (name, snapshot) -> baked at _enrich."""
    snap = snap if isinstance(snap, dict) else {}
    t = str(snap.get("type", "")).strip().lower()
    if snap.get("worn") and snap.get("slot"):                 # explicit template signal wins
        return {"class": "gear", "worn": True, "slot": str(snap["slot"]), "type": t or "gear"}
    if snap.get("on_consume") or t in INV_TYPES:
        return {"class": "inv", "worn": False, "slot": None, "type": t or "consumable"}
    if snap.get("is_container"):
        return {"class": "gear", "worn": bool(snap.get("worn")),
                "slot": str(snap.get("slot") or "back"), "type": t or "container"}
    toks = _ITEM_WORD_RE.findall(str(name or "").lower())

    def _hit(hints) -> bool:                          # exact, plural, or a LONG compound suffix:
        for tk in toks:                               # "gloves"->glove, "breastplate"->plate;
            tks = tk[:-1] if tk.endswith("s") and len(tk) > 3 else tk   # plural-tolerant match
            for h in hints:
                if tk == h or tks == h:               # exact / singular-of-plural
                    return True
                if len(h) >= 5 and len(tk) > len(h) and tk.endswith(h):   # compound suffix, LONG
                    return True                       # hints only (kills band->husband, cap->…)
        return False
    if toks:
        for hints, slot in _SLOT_HINTS:
            if _hit(hints):
                return {"class": "gear", "worn": True, "slot": slot, "type": t or "gear"}
        if _hit(_GEAR_NAME_TOKENS):                   # a tool/kit/bag: gear, but no body slot
            return {"class": "gear", "worn": False, "slot": None, "type": t or "tool"}
    if t in GEAR_TYPES:
        return {"class": "gear", "worn": False, "slot": None, "type": t}
    return {"class": "inv", "worn": False, "slot": None, "type": t or "misc"}


def item_is_gear(it: dict) -> bool:
    """True if an item belongs to the GEAR surface (weapons/armor/tools/accessories/bags),
    False if it is INVENTORY (consumables/materials/devices/…). Reads the baked class,
    re-deriving for instances minted before classification existed (fail-open)."""
    if not isinstance(it, dict):
        return False
    c = it.get("class")
    if c in ("gear", "inv"):
        return c == "gear"
    return classify_item(str(it.get("name", "")), it)["class"] == "gear"


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

# ---- Phase 1 combat / War Room vocab (plan doc 13, ratified 2026-07-09) --------------
# Combatant INSTANCES are snapshot-frozen at spawn (replay-pure): HP by threat tier, an
# armament tag, and the loot row all bake into the journaled op. Two classes (Bean's split):
# EXTRAS are unnamed-procedural rows that evaporate at combat_end; TRACKED combatants
# REFERENCE their entity row so wounds persist after the fight (FULL persistence, ratified).
# 3v3 cap, player included on the ally side. All numbers curated — never model-typed.
COMBAT_SIDES = {"ally", "enemy"}
THREAT_TIERS = ["minion", "standard", "elite", "boss"]
THREAT_HP = {"minion": 6, "standard": 14, "elite": 26, "boss": 44}
THREAT_XP = {"minion": 15, "standard": 30, "elite": 60, "boss": 120}
THREAT_MOD = {"minion": 0, "standard": 1, "elite": 2, "boss": 3}
COMBAT_SIDE_CAP = 3              # per side; the player occupies one ally slot (Bean: 3v3)
COMBAT_HISTORY_CAP = 10          # settled fights kept for the War Room / Console record
CLASH_CAP = 20                   # recorded NPC-vs-NPC clashes kept (prose fights, no dice)
_COMBAT_PHASES = ("climax", "combat", "battle", "fight", "ambush")   # shares R8c's gate
# Ally-enlistment basis (2026-07-10, Bean: "3v3 is missing"). The floor used to enlist a present
# friend ONLY at affinity >= ALLY_STANDING — but affinity rarely climbs that high in play, so the
# party side never formed. The real in-world basis for a comrade-in-arms is a BOND, not a rarely-
# moved number (pillars 4/6): a soulmate, an authored companion-class role/label, or a genuinely
# close relationship dim all ground a companion who'd fight beside you. Curated, never model-typed.
ALLY_STANDING = 40               # deep standing (the Ally affinity tier) — still a valid basis
HOSTILE_STANDING = -10           # an enemy is never an ally (and the present-hostile enlist bar)
_ALLY_ROLE_WORDS = ("companion", "ally", "comrade", "partner", "lover", "beloved", "spouse",
                    "husband", "wife", "sworn", "retainer", "bodyguard", "guardian", "protector",
                    "squire", "familiar", "confidant", "friend", "sidekick", "second")
_AUTHORED_HOSTILE_WORDS = frozenset(("hostile", "enemy", "foe", "adversary", "opponent"))
# Common-enemy basis (2026-07-10, Bean's caravan example): a present NPC on the PLAYER's side of
# an active fight — an escort / hired blade / caravan guard who fights the ambushers to survive —
# even with NO personal bond, so long as they aren't hostile to the Player. Grounded in a
# protective/martial role or an allied faction (the enemy-of-my-enemy read), never a bare presence.
_GUARD_ROLE_WORDS = ("guard", "guardian", "escort", "mercenary", "merc", "sellsword", "soldier",
                     "hireling", "protector", "bodyguard", "retainer", "knight", "warrior",
                     "defender", "sentinel", "watchman", "watchmen", "guardsman", "champion",
                     "fighter", "warden", "ranger", "veteran", "enforcer", "outrider",
                     "caravaneer", "arms", "guardsmen", "sworn", "vanguard", "companion", "ally")
# Player summon/conjuration/creation (2026-07-10, Bean: "VERY important"): a thing the Player
# CALLED INTO BEING fights on their side by construction — its existence is owed to them. Grounded
# on an ownership attribute pointing at the Player, or a summon-typed non-hostile entity.
_SUMMONER_KEYS = ("summoner", "conjurer", "creator", "master", "owner", "summoned_by",
                  "controller", "caster")
_SUMMON_WORDS = ("summon", "summoned", "conjured", "conjuration", "conjure", "familiar",
                 "construct", "elemental", "golem", "thrall", "homunculus", "servitor",
                 "automaton", "turret", "drone", "specter", "spectre", "apparition",
                 "wisp", "totem", "creation", "simulacrum", "duplicate", "manifestation",
                 "avatar", "companion")
# curated fallback loot rows (registry/loot.toml overrides; a Creator-frozen state["loot"]
# table wins over both). Baked into combatant_defeat at _enrich -> replay never re-rolls.
_LOOT_FALLBACK = {
    "minion": [{"name": "a few coins", "qty_min": 1, "qty_max": 4, "chance": 0.7}],
    "standard": [{"name": "coin purse", "qty_min": 1, "qty_max": 1, "chance": 0.8},
                 {"name": "worn weapon", "qty_min": 1, "qty_max": 1, "chance": 0.35}],
    "elite": [{"name": "heavy coin purse", "qty_min": 1, "qty_max": 1, "chance": 0.9},
              {"name": "quality arm or trinket", "qty_min": 1, "qty_max": 1, "chance": 0.6}],
    "boss": [{"name": "trove of coin", "qty_min": 1, "qty_max": 1, "chance": 1.0},
             {"name": "signature relic", "qty_min": 1, "qty_max": 1, "chance": 0.9}],
}


# ---- Large-scale battle (plan doc 13 §F, Bean 2026-07-10) ----------------------------
# The player fights their MICRO slice on the dice (the War Room bubble); the MACRO battle —
# army-on-army, the rest of the field — lives in PROSE, the DM reporting how it goes. Only the
# OUTCOME is tracked: a momentum (code-owned, clamped) whose sign is the TIDE for the player
# (losing / holding / winning). A battle that isn't yet won keeps sending fresh WAVES into the
# War Room until it turns. All numbers curated — never model-typed (the DM only proposes tide).
BATTLE_TIDES = ("losing", "holding", "winning")            # negative / neutral / positive
BATTLE_MOMENTUM_CLAMP = (-3, 3)
BATTLE_WAVE_CAP = 8                # a runaway guard: a battle self-resolves after this many waves
BATTLE_WAVE_DEFAULT = 2            # foes per wave (never past the 3v3 enemy side)
BATTLE_COHORT_SCHEMA = "battle-cohort/1"
BATTLE_COHORT_CAP = COMBAT_SIDE_CAP * (BATTLE_WAVE_CAP + 1)  # 3 opening + eight 3-foe waves


def _validated_battle_cohort(value: Any) -> Optional[dict]:
    """Normalize one finite cohort descriptor without accepting aggregate combat mechanics."""
    if not isinstance(value, dict) or value.get("schema") != BATTLE_COHORT_SCHEMA:
        return None
    ref = str(value.get("id") or "").strip()
    name = str(value.get("name") or "").strip()
    total = value.get("total")
    tier = str(value.get("tier") or "standard").strip().lower()
    armament = str(value.get("armament") or "").strip()
    if not ref or re.fullmatch(r"[a-z0-9_]{1,64}", ref) is None:
        return None
    if not name or len(name) > 60 or isinstance(total, bool) or not isinstance(total, int) \
            or not 2 <= total <= BATTLE_COHORT_CAP:
        return None
    if tier not in THREAT_TIERS or len(armament) > 60:
        return None
    return {"schema": BATTLE_COHORT_SCHEMA, "id": ref, "name": name, "total": total,
            "tier": tier, "armament": armament}


def battle_tide(momentum) -> str:
    """Derive the player-facing tide from the code-owned momentum (never stored — like the
    affinity tier, it cannot drift)."""
    try:
        m = int(momentum)
    except (TypeError, ValueError):
        m = 0
    return "winning" if m >= 1 else "losing" if m <= -1 else "holding"


def _battle(state: dict) -> dict:
    """The large-scale-battle ledger (created lazily so a pre-1.20 checkpoint replays untouched)."""
    return state.setdefault("battle", {"active": False, "name": "", "momentum": 0, "waves": 0,
                                       "threat": "standard", "foe": "reinforcements",
                                       "wave_size": BATTLE_WAVE_DEFAULT, "started_turn": None,
                                       "log": []})


def battle_active(state: dict) -> bool:
    return bool((state.get("battle") or {}).get("active"))


def _combat(state: dict) -> dict:
    """The combat ledger (created lazily so a pre-1.13 checkpoint replays untouched)."""
    return state.setdefault("combat", {"active": False, "combatants": {},
                                       "started_turn": None, "history": []})


def combat_active(state: dict) -> bool:
    return bool((state.get("combat") or {}).get("active"))


class CombatReferenceStatus(str, Enum):
    """Mechanical usability of one exact state-owned combat reference."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    DEFEATED = "defeated"
    QUEUED = "queued"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CombatReferenceCandidate:
    """One committed combat row or one planned cohort ordinal.

    Queued ordinals deliberately have no ``combatant_id``.  Their plan identity and visible label
    are stable, but they are not mechanically targetable until the reducer admits the exact row.
    """

    combatant_id: Optional[str]
    label: str
    name: str
    state: str
    side: str
    cohort_ref: Optional[str] = None
    cohort_index: Optional[int] = None
    cohort_total: Optional[int] = None


@dataclass(frozen=True)
class CombatReferenceResult:
    """Total, immutable result from the live-combat reference boundary."""

    query: str
    normalized: str
    status: CombatReferenceStatus
    match_kind: Optional[str]
    selected: Optional[CombatReferenceCandidate]
    candidates: tuple[CombatReferenceCandidate, ...]


def live_combatants(state: dict, side: Optional[str] = None) -> list[dict]:
    """Non-defeated combatant rows, spawn order (dict order = journal order = stable)."""
    rows = [r for r in ((state.get("combat") or {}).get("combatants") or {}).values()
            if isinstance(r, dict) and not r.get("defeated")]
    return [r for r in rows if side is None or r.get("side") == side]


def combatant_label(row: Any, fallback: str = "?") -> str:
    """Stable visible name for one row; cohort members are numbered without changing kit identity."""
    if not isinstance(row, dict):
        return fallback
    name = str(row.get("name") or fallback)
    cohort = row.get("cohort")
    if isinstance(cohort, dict):
        index = cohort.get("index")
        total = cohort.get("total")
        if isinstance(index, int) and not isinstance(index, bool) and isinstance(total, int) \
                and not isinstance(total, bool) and 1 <= index <= total:
            return f"{name} #{index}"
    return name


def battle_cohort_status(state: dict) -> Optional[dict]:
    """Player/narrator-safe finite cohort counts derived from committed state only."""
    battle = state.get("battle") or {}
    cohort = battle.get("cohort")
    spec = _validated_battle_cohort(cohort)
    if spec is None:
        return None
    try:
        spawned = max(0, min(spec["total"], int(cohort.get("spawned", 0))))
        queued = max(0, min(spec["total"] - spawned, int(cohort.get("remaining",
                                                                     spec["total"] - spawned))))
    except (TypeError, ValueError):
        return None
    rows = ((state.get("combat") or {}).get("combatants") or {}).values()
    active = sum(
        isinstance(row, dict) and not row.get("defeated")
        and isinstance(row.get("cohort"), dict)
        and row["cohort"].get("ref") == spec["id"]
        for row in rows
    )
    return {**spec, "spawned": spawned, "active": active,
            "defeated": max(0, spawned - active), "queued": queued}


_COMBAT_REFERENCE_ORDINAL_WORDS = (
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
    "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth",
    "twenty-first", "twenty-second", "twenty-third", "twenty-fourth", "twenty-fifth",
    "twenty-sixth", "twenty-seventh",
)


def _combat_reference_numeric_ordinal(index: int) -> str:
    suffix = "th" if 10 < index % 100 < 14 else {
        1: "st", 2: "nd", 3: "rd",
    }.get(index % 10, "th")
    return f"{index}{suffix}"


_COMBAT_REFERENCE_ORDINALS = {
    form: index
    for index, word in enumerate(_COMBAT_REFERENCE_ORDINAL_WORDS, start=1)
    for form in (word, word.replace("-", " "))
}
_COMBAT_REFERENCE_ORDINALS.update({str(index): index for index in range(1, 28)})
_COMBAT_REFERENCE_ORDINALS.update({
    _combat_reference_numeric_ordinal(index): index for index in range(1, 28)
})
_COMBAT_REFERENCE_ORDINAL_RE = re.compile(
    r"^(?:the\s+)?(?P<ordinal>"
    + "|".join(
        re.escape(ordinal)
        for ordinal in sorted(_COMBAT_REFERENCE_ORDINALS, key=len, reverse=True)
    )
    + r")\s+(?P<base>[a-z0-9][a-z0-9 -]*)$"
)
_COMBAT_REFERENCE_PRONOUN_RE = re.compile(
    r"^(?:he|him|his|she|her|hers|it|its|they|them|their|theirs|this one|that one|those|these)\b"
)
_COMBAT_REFERENCE_POSSESSIVE_RE = re.compile(r"(?:'s\b|s'(?:\s|$))")


def _normalize_combat_reference(value: Any) -> str:
    text = str(value or "").replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").casefold().strip()
    text = re.sub(r"[.!?,;:]+$", "", text).strip()
    return re.sub(r"\s+", " ", text)


def _combat_reference_word_forms(word: str) -> frozenset[str]:
    """Small regular morphology surface for cohort head nouns, never creature-specific."""
    normalized = re.sub(r"[^a-z0-9]", "", word.casefold())
    if not normalized:
        return frozenset()
    forms = {normalized}
    if len(normalized) > 4 and normalized.endswith("ies"):
        forms.add(normalized[:-3] + "y")
    if len(normalized) > 4 and normalized.endswith("ves"):
        forms.update((normalized[:-3] + "f", normalized[:-3] + "fe"))
    if len(normalized) > 4 and normalized.endswith("es"):
        forms.add(normalized[:-2])
    if len(normalized) > 3 and normalized.endswith("s") and not normalized.endswith("ss"):
        forms.add(normalized[:-1])
    if len(normalized) > 4 and normalized.endswith("ed"):
        stem = normalized[:-2]
        forms.add(stem)
        if len(stem) > 2 and stem[-1] == stem[-2]:
            forms.add(stem[:-1])
    return frozenset(forms)


def _combat_reference_phrase_forms(value: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    if not words:
        return frozenset()
    prefix = words[:-1]
    return frozenset(" ".join((*prefix, tail))
                     for tail in _combat_reference_word_forms(words[-1]))


def _combat_reference_head_forms(value: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return _combat_reference_word_forms(words[-1]) if words else frozenset()


def combat_reference_candidates(state: dict) -> tuple[CombatReferenceCandidate, ...]:
    """Project committed rows plus exact queued cohort ordinals in deterministic ledger order."""
    if not isinstance(state, dict):
        return ()
    combat = state.get("combat")
    rows = combat.get("combatants") if isinstance(combat, dict) else None
    if not isinstance(rows, dict):
        rows = {}
    candidates: list[CombatReferenceCandidate] = []
    seen_ordinals: set[tuple[str, int]] = set()
    for cid, row in rows.items():
        if not isinstance(row, dict):
            continue
        combatant_id = str(cid)
        name = str(row.get("name") or "").strip()
        if not combatant_id or not name:
            continue
        cohort_ref: Optional[str] = None
        cohort_index: Optional[int] = None
        cohort_total: Optional[int] = None
        cohort = row.get("cohort")
        if isinstance(cohort, dict):
            ref = str(cohort.get("ref") or "").strip()
            index = cohort.get("index")
            total = cohort.get("total")
            if ref and isinstance(index, int) and not isinstance(index, bool) \
                    and isinstance(total, int) and not isinstance(total, bool) \
                    and 1 <= index <= total <= BATTLE_COHORT_CAP:
                cohort_ref, cohort_index, cohort_total = ref, index, total
                seen_ordinals.add((ref, index))
        candidates.append(CombatReferenceCandidate(
            combatant_id=combatant_id,
            label=combatant_label(row, combatant_id),
            name=name,
            state="defeated" if row.get("defeated") else "active",
            side=str(row.get("side") or ""),
            cohort_ref=cohort_ref,
            cohort_index=cohort_index,
            cohort_total=cohort_total,
        ))

    battle = state.get("battle")
    cohort = battle.get("cohort") if isinstance(battle, dict) and battle.get("active") else None
    spec = _validated_battle_cohort(cohort)
    if spec is None or not isinstance(cohort, dict):
        return tuple(candidates)
    spawned = cohort.get("spawned")
    remaining = cohort.get("remaining")
    if isinstance(spawned, bool) or not isinstance(spawned, int) \
            or isinstance(remaining, bool) or not isinstance(remaining, int) \
            or not 0 <= spawned <= spec["total"] \
            or remaining != spec["total"] - spawned:
        return tuple(candidates)
    for index in range(spawned + 1, spec["total"] + 1):
        if (spec["id"], index) in seen_ordinals:
            continue
        candidates.append(CombatReferenceCandidate(
            combatant_id=None,
            label=f"{spec['name']} #{index}",
            name=spec["name"],
            state="queued",
            side="enemy",
            cohort_ref=spec["id"],
            cohort_index=index,
            cohort_total=spec["total"],
        ))
    return tuple(candidates)


def _combat_reference_result(
    query: str,
    normalized: str,
    match_kind: Optional[str],
    candidates: tuple[CombatReferenceCandidate, ...] = (),
) -> CombatReferenceResult:
    if not candidates:
        return CombatReferenceResult(
            query, normalized, CombatReferenceStatus.UNKNOWN, match_kind, None, (),
        )
    if len(candidates) != 1:
        return CombatReferenceResult(
            query, normalized, CombatReferenceStatus.AMBIGUOUS,
            match_kind, None, candidates,
        )
    selected = candidates[0]
    status = {
        "active": CombatReferenceStatus.RESOLVED,
        "defeated": CombatReferenceStatus.DEFEATED,
        "queued": CombatReferenceStatus.QUEUED,
    }.get(selected.state, CombatReferenceStatus.UNKNOWN)
    if status is CombatReferenceStatus.UNKNOWN:
        return CombatReferenceResult(query, normalized, status, match_kind, None, ())
    return CombatReferenceResult(query, normalized, status, match_kind, selected, candidates)


def _combat_reference_ordinal(value: str) -> tuple[Optional[int], str]:
    match = _COMBAT_REFERENCE_ORDINAL_RE.fullmatch(value)
    if match is None:
        return None, ""
    return _COMBAT_REFERENCE_ORDINALS[match.group("ordinal")], match.group("base")


def resolve_combat_reference(state: dict, token: Any) -> CombatReferenceResult:
    """Resolve one already-extracted entity reference without inventing target semantics.

    This boundary accepts state-owned ids, exact visible labels/names, and anchored cohort ordinals.
    Possessions, body loci, essential-self language, pronouns, and surrounding sentence grammar must
    be classified upstream; passing them here returns ``UNKNOWN`` rather than silently selecting an
    owner.  Exact defeated and queued identities remain visible but are never mechanically live.
    """
    query = str(token or "").strip()
    normalized = _normalize_combat_reference(query)
    if not normalized or _COMBAT_REFERENCE_PRONOUN_RE.match(normalized) \
            or _COMBAT_REFERENCE_POSSESSIVE_RE.search(normalized):
        return _combat_reference_result(query, normalized, None)
    candidates = combat_reference_candidates(state)
    if not candidates:
        return _combat_reference_result(query, normalized, None)

    # A first cohort row often has an internal id equal to the normalized bare cohort name
    # (``hollowed``).  That transport identity must not turn ordinary lowercase Player prose into
    # an implicit ``#1``.  Discriminated ids (underscores / ``#N``) and non-cohort ids stay valid.
    interpretations: list[tuple[str, tuple[CombatReferenceCandidate, ...]]] = []
    id_hits = tuple(
        candidate for candidate in candidates
        if candidate.combatant_id is not None
        and _normalize_combat_reference(candidate.combatant_id) == normalized
        and (
            candidate.cohort_ref is None
            or _normalize_combat_reference(candidate.name) != normalized
        )
    )
    if id_hits:
        interpretations.append(("combatant_id", id_hits))

    label_hits = tuple(candidate for candidate in candidates
                       if _normalize_combat_reference(candidate.label) == normalized)
    if label_hits:
        interpretations.append(("visible_label", label_hits))

    name_hits = tuple(candidate for candidate in candidates
                      if _normalize_combat_reference(candidate.name) == normalized)
    if name_hits:
        interpretations.append(("exact_name", name_hits))

    ordinal, base = _combat_reference_ordinal(normalized)
    if ordinal is not None:
        indexed = tuple(candidate for candidate in candidates
                        if candidate.cohort_index == ordinal)
        exact_phrase = tuple(candidate for candidate in indexed
                             if _combat_reference_phrase_forms(base)
                             & _combat_reference_phrase_forms(candidate.name))
        ordinal_matches = set(exact_phrase)
        if len(re.findall(r"[a-z0-9]+", base)) == 1:
            ordinal_matches.update(
                candidate for candidate in indexed
                if _combat_reference_head_forms(base)
                & _combat_reference_head_forms(candidate.name)
            )
        ordinal_hits = tuple(candidate for candidate in indexed
                             if candidate in ordinal_matches)
        if ordinal_hits:
            interpretations.append(("cohort_ordinal", ordinal_hits))

    if not interpretations:
        return _combat_reference_result(query, normalized, None)
    matched = {candidate for _kind, hits in interpretations for candidate in hits}
    exact_hits = tuple(candidate for candidate in candidates if candidate in matched)
    hit_sets = [frozenset(hits) for _kind, hits in interpretations]
    match_kind = interpretations[0][0] \
        if len(exact_hits) == 1 or all(hits == hit_sets[0] for hits in hit_sets[1:]) \
        else "exact_identity"
    return _combat_reference_result(query, normalized, match_kind, exact_hits)


def resolve_combatant(state: dict, token) -> Optional[str]:
    """Compatibility wrapper returning only one mechanically active combat row id.

    Existing reducers remain ``Optional[str]`` callers.  The structured API above carries exact
    ambiguity, defeated, and queued reasons for new semantic construction.  A unique active legacy
    non-cohort row may still win over an identically named defeated row, but cohort ambiguity is
    never collapsed by this compatibility path.
    """
    # Reducer journals already carry exact internal ids.  Preserve that trusted compatibility
    # channel even when the first cohort id spells like its bare public base; new construction must
    # use ``resolve_combat_reference`` and cannot receive this transport-only shortcut.
    query = str(token or "").strip()
    rows = ((state.get("combat") or {}).get("combatants") or {}) \
        if isinstance(state, dict) else {}
    if isinstance(rows, dict) and query in rows and isinstance(rows[query], dict) \
            and not rows[query].get("defeated"):
        return query
    result = resolve_combat_reference(state, token)
    if result.status is CombatReferenceStatus.RESOLVED and result.selected is not None:
        return result.selected.combatant_id
    if result.status is CombatReferenceStatus.AMBIGUOUS:
        if any(candidate.cohort_ref is not None for candidate in result.candidates):
            return None
        active = tuple(candidate for candidate in result.candidates
                       if candidate.state == "active" and candidate.combatant_id is not None)
        return active[0].combatant_id if len(active) == 1 else None
    if result.status is not CombatReferenceStatus.UNKNOWN:
        # An exact defeated or queued identity is a refusal, not permission to retarget a broader
        # active label through the legacy token-subset compatibility path below.
        return None

    # Historical reducer inputs may use a unique token from a non-cohort combatant label.  Keep
    # that convenience here, never in the structured Player-reference boundary above.
    normalized = _normalize_combat_reference(query)
    if not normalized or _COMBAT_REFERENCE_PRONOUN_RE.match(normalized) \
            or _COMBAT_REFERENCE_POSSESSIVE_RE.search(normalized) \
            or re.search(r"\d|#", normalized):
        return None
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    subset_hits = tuple(
        candidate for candidate in combat_reference_candidates(state)
        if tokens and tokens <= set(re.findall(r"[a-z0-9]+", candidate.label.casefold()))
    )
    subset_result = _combat_reference_result(
        query, normalized, "unique_token_subset" if subset_hits else None, subset_hits,
    )
    if subset_result.status is CombatReferenceStatus.RESOLVED \
            and subset_result.selected is not None:
        return subset_result.selected.combatant_id
    if subset_result.status is not CombatReferenceStatus.AMBIGUOUS \
            or any(candidate.cohort_ref is not None for candidate in subset_result.candidates):
        return None
    active = tuple(candidate for candidate in subset_result.candidates
                   if candidate.state == "active" and candidate.combatant_id is not None)
    return active[0].combatant_id if len(active) == 1 else None


# ---- op spec: op -> (required fields, per-field validator hints) (02 SS11) --------
_SPEC: dict[str, set[str]] = {
    "semantic_meaning_commit": {"meaning"},
    "semantic_binding_commit": {"binding"},
    "semantic_world_alignment_commit": {"alignment"},
    "semantic_frame_commit": {"frame"},
    "claim_record": {"frame"},
    "fact_admit": {"statement", "cause"},
    "belief_acquire": {"holder", "stance"},
    "mechanic_settlement_commit": {
        "contract_id", "settlement_ref", "frame_ref", "members",
    },
    "world_identity_set": {"world_id"},
    # Creator-only, typed source document used to rebuild cards after long campaigns.  This is
    # presentation/lore metadata, never objective event truth and never an extraction surface.
    "creator_world_seed": {"document"},
    "capability_assign": {"definition", "subject", "acquisition_source"},
    "world_event_admit": {"event"},
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
    "fact_retire": set(),                      # compression item 3: `fact` id OR `statement`
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
    # Generic non-HP resource mutation for code-owned mechanics. It is deliberately absent from
    # the extraction wire: future reducers/Tier-0 rules may gain, spend, or set a DECLARED pool,
    # while narration and free-form user prose cannot mint or move resource points.
    "resource_change": {"char", "resource", "action", "amount"},
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
    # Phase 1 combat / War Room (plan doc 13, ratified). combatant_spawn / combatant_defeat /
    # combat_end / loot_table are PRIVILEGED (rule/user/genesis — the narrator's [foe] tag is
    # validated and re-sourced as rule by the pipeline); combatant_hp and
    # clash_record are PROPOSABLE (tag protocol + rpg wire, clamped/checked at apply).
    "combatant_spawn": {"name", "side"},
    "enemy_intent_set": {"actor"},
    "combatant_hp": {"target", "delta"},
    "combatant_defeat": {"target"},
    "combat_end": set(),
    "clash_record": {"a", "b"},                # NPC-vs-NPC: record, never resolve (no dice)
    "loot_table": {"tier", "entries"},         # Creator/assist-frozen loot rows (pillar 18)
    # Large-scale battle (plan doc 13 §F, Bean 2026-07-10). battle_start / battle_wave /
    # battle_end are PRIVILEGED (rule/user/genesis — the DM's [battle] tag opens it and the
    # code referee sends waves); tide_set is PROPOSABLE (the DM's [tide] tag reports the macro,
    # clamped +/-1 step per turn — code owns the pace).
    "battle_start": {"name"},
    "tide_set": {"tide"},
    "battle_wave": set(),
    "battle_end": set(),
    # Phase 2 living world (plan doc 13, ratified). front_add / front_tick / route_set are
    # PRIVILEGED (fronts are authored frozen and advanced by CODE — the world_ops referee);
    # front_reveal is PROPOSABLE (a rumor heard in the fiction is witnessed truth — the
    # [rumor] tag / name-mention floor surface a hidden clock, never advance it).
    "front_add": {"name", "segments", "consequence"},
    "front_tick": {"front"},
    "front_reveal": {"front"},
    "route_set": {"a", "b", "segments"},
}

# 08 E2 deterministic family apply-order (freeze first so mid-delta safewords gate the rest)
_ORDER = {"semantic_meaning_commit": -5,
          "semantic_binding_commit": -4,
          "semantic_world_alignment_commit": -3,
          "semantic_frame_commit": -2,
          "mechanic_settlement_commit": 1,
          "freeze": -1, "unfreeze": 0, "world_identity_set": 0, "creator_world_seed": 0,
          "capability_assign": 1,
          "entity_add": 0, "presence": 1, "move_entity": 2,
          "scene_set": 2, "scene_mode": 2, "item_mint": 2, "item_gain": 2,
          "item_transfer": 3, "position": 3,
          "clothing": 4, "item_move": 4, "item_equip": 4, "item_unequip": 4, "item_lose": 4,
          "contact": 5, "item_consume": 5, "effect_add": 5, "arousal": 6, "effect_update": 6,
          "effect_remove": 6, "hp_adj": 6, "resource_change": 7,
          "award_exp": 8, "level_up": 9,
          "defeat_resolve": 9,
          "loot_table": 1, "combatant_spawn": 2, "enemy_intent_set": 3,
          "combatant_hp": 6,   # Phase 1: spawn/intent before harm
          "combatant_defeat": 8, "combat_end": 9,                     # harm; settle last
          "battle_start": 1, "tide_set": 6, "battle_wave": 2,         # §F: open early, wave with
          "battle_end": 9,                                            # the spawns, settle last
          "front_add": 1, "route_set": 1, "front_reveal": 5, "front_tick": 8,
          # A front completion is the first code-owned event cause.  Apply its exact clock
          # receipt before validating and publishing the immutable event in the same batch.
          "world_event_admit": 9}   # Phase 2 + World Event admission
_DEFAULT_ORDER = 7

# 02 SS12b families
_FAMILY = {
    "semantic_meaning_commit": "facts",
    "semantic_binding_commit": "facts",
    "semantic_world_alignment_commit": "facts",
    "semantic_frame_commit": "facts",
    "claim_record": "facts",
    "fact_admit": "facts",
    "belief_acquire": "facts",
    "mechanic_settlement_commit": "facts",
    "world_identity_set": "facts",
    "creator_world_seed": "facts",
    "capability_assign": "facts",
    "world_event_admit": "facts",
    "set_attribute": "scene", "move_entity": "scene", "presence": "scene", "clothing": "scene",
    "position": "scene", "contact": "scene", "time_advance": "scene", "clock_tick": "scene",
    "scene_set": "scene", "scene_mode": "scene", "entity_add": "scene", "roll": "scene",
    "stagnation": "scene",
    "reveal_fact": "facts", "memory_event": "facts", "goal": "facts",
    "fact_retire": "facts",                # compression item 3 (privileged: never extraction)
    "arousal": "organic", "mood": "organic", "relationship_adj": "organic",
    "obsession": "organic", "craving": "organic", "scene_dial": "organic",
    "consent_signal": "consent", "consent_set": "consent",
    "freeze": "safety", "unfreeze": "safety",
    "player_seed": "player",                   # RPG specialization (privileged)
    "resource_change": "player",               # internal declared-pool lifecycle mutation
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
    "combatant_spawn": "scene", "enemy_intent_set": "scene",
    "combatant_hp": "scene",              # Phase 1 combat: the
    "combatant_defeat": "scene", "combat_end": "scene",               # fight is scene truth;
    "clash_record": "facts", "loot_table": "facts",                   # records are facts
    "battle_start": "scene", "battle_wave": "scene", "battle_end": "scene",   # §F: the battle
    "tide_set": "scene",                                              # is scene/combat truth
    "front_add": "facts", "front_tick": "facts",                      # Phase 2: the living
    "front_reveal": "facts", "route_set": "facts",                    # world is world truth
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
                       "mechanic_settlement_commit",
                       "resource_change",
                       "item_mint", "item_move", "item_equip", "item_unequip",   # a flashback
                       "item_consume", "item_transfer",               # can't touch live items
                       "effect_add", "effect_remove", "effect_update",   # ...or live effects
                       "item_gain", "item_lose", "hp_adj", "defeat_resolve",   # RPG-5: nor
                       "combatant_spawn", "enemy_intent_set", "combatant_hp",
                       "combatant_defeat",  # grant items /
                       "combat_end",   # deal harm — a dream can't rob, wound, or start a war
                       "battle_start", "battle_wave", "battle_end", "tide_set"}   # §F: nor a war


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_") or "unnamed"


def empty_state() -> dict:
    return {"schema": SCHEMA, "entities": {}, "chars": {}, "attributes": {}, "clothing": {},
            "poses": {}, "contacts": {}, "consent": {}, "relationships": {}, "facts": {},
            "beliefs": {}, "epistemic_history": [], "claims": [], "propositions": {},
            "memories": [], "scene": {}, "clock": {"day": 1, "time_of_day": "evening",
            "minutes": 0, "calendar_note": None}, "frozen": False, "rolls": [], "player": {},
            "items": {}, "gear": {}, "inventory": {}, "effects": {}, "quests": {},
            "affinity": {}, "factions": {}, "world": {}, "world_identity": {},
            "creator_world": {},
            "world_events": [], "world_overlay": {},
            "meta": {"turn": -1}}


def is_empty(state: dict) -> bool:
    """True when nothing narratively meaningful has been recorded (header should not render)."""
    if not state:
        return True
    return not (state.get("entities") or state.get("chars") or state.get("scene")
                or state.get("consent") or state.get("relationships") or state.get("facts")
                or state.get("beliefs") or state.get("claims")
                or state.get("rolls") or state.get("frozen") or state.get("player")
                or state.get("semantic_meanings") or state.get("semantic_bindings")
                or state.get("semantic_world_alignments") or state.get("semantic_frames")
                or state.get("mechanic_settlements") or state.get("world_events")
                or state.get("creator_world") or state.get("claims"))


def _trim_receipt_ledger(rows: list[dict], current_turn: int, older_cap: int = 16) -> None:
    """Retain every receipt in the active turn; cap only history from older turns."""
    current = [row for row in rows if isinstance(row, dict) and row.get("turn") == current_turn]
    older = [row for row in rows if not isinstance(row, dict) or row.get("turn") != current_turn]
    historical_allowance = max(0, int(older_cap) - len(current))
    retained_older = older[-historical_allowance:] if historical_allowance else []
    rows[:] = retained_older + current


_CREATOR_WORLD_DOCUMENT_KEYS = frozenset({
    "world_id", "parent_world_id", "name", "genre", "setting", "date", "time", "tone",
    "factions", "locations", "npcs", "aspects", "opening_scene", "opening_quest", "extras",
    "loot", "fronts", "routes",
})
_CREATOR_WORLD_SCALAR_KEYS = frozenset({
    "world_id", "parent_world_id", "name", "genre", "setting", "date", "time", "tone",
    "opening_scene", "opening_quest",
})
_CREATOR_WORLD_LIST_KEYS = frozenset({
    "factions", "locations", "npcs", "aspects", "extras", "fronts", "routes",
})
_CREATOR_PLAYER_SOURCE_KEYS = frozenset({
    "name", "sex", "pronouns", "species", "appearance", "concept", "level", "stats",
    "skills", "abilities", "defs", "gear", "extras", "resources",
})


def _creator_world_snapshot(document: object, turn: int) -> dict:
    """Validate and seal the Creator's normalized world source document.

    The snapshot exists only so card regeneration does not depend on the rolling memory cache.
    It carries no fact/event authority.  Directions are deliberately outside the accepted key set,
    and the canonical fingerprint makes a same-world replacement conflict visible on replay.
    """
    if not isinstance(document, dict):
        raise ValueError("Creator world source document must be an object")
    unknown = set(document) - _CREATOR_WORLD_DOCUMENT_KEYS
    if unknown:
        raise ValueError("Creator world source document contains unsupported fields")
    if any(not isinstance(document.get(key, ""), str) for key in _CREATOR_WORLD_SCALAR_KEYS):
        raise ValueError("Creator world source document has a non-text scalar field")
    if any(not isinstance(document.get(key, []), list) for key in _CREATOR_WORLD_LIST_KEYS):
        raise ValueError("Creator world source document has a non-list collection")
    if not isinstance(document.get("loot", {}), dict):
        raise ValueError("Creator world source document loot must be an object")
    world_id = str(document.get("world_id") or "")
    parent_world_id = str(document.get("parent_world_id") or "")
    if re.fullmatch(r"world_[0-9a-f]{32}", world_id) is None:
        raise ValueError("Creator world source document lacks a canonical world identity")
    if parent_world_id and (
            re.fullmatch(r"world_[0-9a-f]{32}", parent_world_id) is None
            or parent_world_id == world_id):
        raise ValueError("Creator world source document has an invalid parent identity")
    if not str(document.get("name") or "").strip() \
            or not str(document.get("genre") or "").strip():
        raise ValueError("Creator world source document lacks its name or genre")
    try:
        canonical_document = json.loads(json.dumps(
            document, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ))
        encoded = json.dumps(
            canonical_document, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Creator world source document must contain finite JSON data") from exc
    if len(encoded) > 1024 * 1024:
        raise ValueError("Creator world source document exceeds the one-megabyte safety bound")
    from .capability_glossary import content_fingerprint

    record = {
        "schema": "aetherstate-creator-world-snapshot/1",
        "world_id": world_id,
        "authored_turn": int(turn),
        "authority_ceiling": "creator_lore_only",
        "establishes_objective_truth": False,
        "admits_world_event": False,
        "document": canonical_document,
    }
    record["fingerprint"] = content_fingerprint(record)
    return record


def _creator_player_source(document: object) -> dict:
    """Validate the non-authoritative authored Player snapshot carried by player_seed."""
    if not isinstance(document, dict):
        raise OpReject("Creator player source document must be an object")
    if set(document) - _CREATOR_PLAYER_SOURCE_KEYS:
        raise OpReject("Creator player source document contains unsupported fields")
    for key in ("name", "sex", "pronouns", "species", "appearance", "concept"):
        if not isinstance(document.get(key, ""), str):
            raise OpReject("Creator player source document has a non-text scalar field")
    if not isinstance(document.get("level", 1), int) \
            or isinstance(document.get("level", 1), bool):
        raise OpReject("Creator player source document has an invalid level")
    for key in ("stats", "skills", "defs", "resources"):
        if not isinstance(document.get(key, {}), dict):
            raise OpReject(f"Creator player source document {key} must be an object")
    for key in ("abilities", "gear", "extras"):
        if not isinstance(document.get(key, []), list):
            raise OpReject(f"Creator player source document {key} must be a list")
    return deepcopy(document)


# ================================ validation =======================================
_SETTLEMENT_MEMBER_KINDS = {
    "combatant_spawn", "scene_set", "check", "combatant_hp", "master_tick", "effect_add",
}
_WEAPON_STRIKE_FACTOR = {"crit_success": 3, "success": 2, "partial": 1}


def _weapon_magnitude_policy(state: dict, actor: str) -> int:
    """Independent copy of the frozen equipped-weapon damage policy at admission."""
    best = 0
    for item in (state.get("items") or {}).values():
        if not isinstance(item, dict) or item.get("owner") != actor \
                or not str(item.get("loc", "")).startswith("gear:"):
            continue
        damage = (item.get("mods_snapshot") or {}).get("damage")
        if isinstance(damage, int):
            slot = str(item.get("loc", ""))[5:]
            rank = 3 if slot == "mainhand" else 2 if slot == "offhand" else 1
            best = max(best, damage * 10 + rank)
    return max(1, best // 10) if best else 1


def _validate_weapon_settlement_members_shape(op: dict) -> None:
    """Validate the exact Tier-0 envelope shape without consulting mutable state."""
    from .mechanic_settlement import WEAPON_ATTACK_CONTRACT

    if op.get("contract_id") != WEAPON_ATTACK_CONTRACT:
        raise ValueError("unsupported mechanic settlement contract")
    if op.get("settlement_ref") != op.get("settlement_ref", "").strip() \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("settlement_ref") or "")) is None:
        raise ValueError("malformed mechanic settlement reference")
    if op.get("frame_ref") != op.get("_semantic_frame_ref") \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("frame_ref") or "")) is None:
        raise ValueError("mechanic settlement frame reference mismatch")
    members = op.get("members")
    if not isinstance(members, list) or not 2 <= len(members) <= 7:
        raise ValueError("weapon attack settlement has an invalid member count")
    counts: dict[str, int] = {}
    member_order = {
        "combatant_spawn": 0, "scene_set": 1, "check": 2,
        "combatant_hp": 3, "master_tick": 4, "effect_add": 5,
    }
    ranks: list[int] = []
    for member in members:
        if not isinstance(member, dict) or member.get("op") not in _SETTLEMENT_MEMBER_KINDS:
            raise ValueError("weapon attack settlement contains an unsupported member")
        if member.get("_semantic_frame_ref") != op["frame_ref"]:
            raise ValueError("weapon attack settlement member belongs to another frame")
        if "_settlement_ref" in member or "_settlement_member_index" in member:
            raise ValueError("nested mechanic settlement members cannot be projections")
        ranks.append(member_order[str(member["op"])])
        counts[str(member["op"])] = counts.get(str(member["op"]), 0) + 1
    if ranks != sorted(ranks):
        raise ValueError("weapon attack settlement members are not in reducer order")
    if counts.get("check") != 1 or counts.get("combatant_hp") != 1:
        raise ValueError("weapon attack settlement needs one check and one strike")
    if counts.get("combatant_spawn", 0) > COMBAT_SIDE_CAP or any(
        counts.get(kind, 0) > 1
        for kind in _SETTLEMENT_MEMBER_KINDS
        if kind != "combatant_spawn"
    ):
        raise ValueError("weapon attack settlement repeats a member kind")
    check = next(member for member in members if member["op"] == "check")
    strike = next(member for member in members if member["op"] == "combatant_hp")
    if check.get("tier") not in CHECK_TIERS or strike.get("_strike") is not True:
        raise ValueError("weapon attack settlement lacks a settled strike")
    mastery = [member for member in members if member["op"] == "master_tick"]
    expected_mastery = int(MASTERY_TICKS[str(check["tier"])])
    if (expected_mastery > 0) != bool(mastery):
        raise ValueError("weapon attack settlement mastery cardinality is incomplete")
    if mastery and (
            mastery[0].get("char") != check.get("char")
            or mastery[0].get("skill") != check.get("skill")
            or mastery[0].get("amount") != expected_mastery):
        raise ValueError("weapon attack settlement mastery does not match its check")
    consequences = [member for member in members if member["op"] == "effect_add"]
    if (check.get("tier") == "crit_fail") != bool(consequences):
        raise ValueError("weapon attack settlement consequence cardinality is incomplete")
    if consequences and (
            consequences[0].get("char") != check.get("char")
            or consequences[0].get("effect")
            != ("Backlash" if int(check.get("_scope_over", 0) or 0) else "Strained")
            or consequences[0].get("kind") != "status"):
        raise ValueError("weapon attack settlement consequence does not match its check")
    scenes = [member for member in members if member["op"] == "scene_set"]
    if scenes and (scenes[0].get("_floor") is not True or scenes[0].get("phase") != "climax"):
        raise ValueError("weapon attack scene transition is not the code-owned combat floor")


def _validate_skill_check_settlement_members_shape(op: dict) -> None:
    """Validate a complete non-impact check envelope without consulting mutable state."""
    from .mechanic_settlement import SKILL_CHECK_CONTRACT

    if op.get("contract_id") != SKILL_CHECK_CONTRACT:
        raise ValueError("unsupported skill check settlement contract")
    if op.get("settlement_ref") != op.get("settlement_ref", "").strip() \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("settlement_ref") or "")) is None:
        raise ValueError("malformed mechanic settlement reference")
    if op.get("frame_ref") != op.get("_semantic_frame_ref") \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("frame_ref") or "")) is None:
        raise ValueError("mechanic settlement frame reference mismatch")
    members = op.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 3:
        raise ValueError("skill check settlement has an invalid member count")
    allowed = {"check", "master_tick", "effect_add"}
    member_order = {"check": 0, "master_tick": 1, "effect_add": 2}
    counts: dict[str, int] = {}
    ranks: list[int] = []
    for member in members:
        if not isinstance(member, dict) or member.get("op") not in allowed:
            raise ValueError("skill check settlement contains an unsupported member")
        if member.get("_semantic_frame_ref") != op["frame_ref"]:
            raise ValueError("skill check settlement member belongs to another frame")
        if "_settlement_ref" in member or "_settlement_member_index" in member:
            raise ValueError("nested mechanic settlement members cannot be projections")
        kind = str(member["op"])
        ranks.append(member_order[kind])
        counts[kind] = counts.get(kind, 0) + 1
    if ranks != sorted(ranks):
        raise ValueError("skill check settlement members are not in reducer order")
    if counts.get("check") != 1 or any(count > 1 for count in counts.values()):
        raise ValueError("skill check settlement needs one exact check and unique side effects")
    check = next(member for member in members if member["op"] == "check")
    if check.get("tier") not in CHECK_TIERS:
        raise ValueError("skill check settlement lacks a settled check")
    mastery = [member for member in members if member["op"] == "master_tick"]
    expected_mastery = int(MASTERY_TICKS[str(check["tier"])])
    if (expected_mastery > 0) != bool(mastery):
        raise ValueError("skill check settlement mastery cardinality is incomplete")
    if mastery and (
            mastery[0].get("char") != check.get("char")
            or mastery[0].get("skill") != check.get("skill")
            or mastery[0].get("amount") != expected_mastery):
        raise ValueError("skill check settlement mastery does not match its check")
    consequences = [member for member in members if member["op"] == "effect_add"]
    if (check.get("tier") == "crit_fail") != bool(consequences):
        raise ValueError("skill check settlement consequence cardinality is incomplete")
    if consequences and (
            consequences[0].get("char") != check.get("char")
            or consequences[0].get("effect")
            != ("Backlash" if int(check.get("_scope_over", 0) or 0) else "Strained")
            or consequences[0].get("kind") != "status"):
        raise ValueError("skill check settlement consequence does not match its check")


def _validate_combat_opening_settlement_members_shape(op: dict) -> None:
    """Validate the immutable 1-3 foe opening envelope before any reducer work."""
    from .mechanic_settlement import COMBAT_OPENING_CONTRACT

    if op.get("contract_id") != COMBAT_OPENING_CONTRACT:
        raise ValueError("unsupported mechanic settlement contract")
    if op.get("settlement_ref") != op.get("settlement_ref", "").strip() \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("settlement_ref") or "")) is None:
        raise ValueError("malformed mechanic settlement reference")
    if op.get("frame_ref") != op.get("_semantic_frame_ref") \
            or _SEMANTIC_FRAME_REF_RE.fullmatch(str(op.get("frame_ref") or "")) is None:
        raise ValueError("mechanic settlement frame reference mismatch")
    members = op.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= COMBAT_SIDE_CAP + 1:
        raise ValueError("combat opening settlement has an invalid member count")
    spawns = [member for member in members if isinstance(member, dict)
              and member.get("op") == "combatant_spawn"]
    scenes = [member for member in members if isinstance(member, dict)
              and member.get("op") == "scene_set"]
    if not 1 <= len(spawns) <= COMBAT_SIDE_CAP or len(scenes) > 1 \
            or len(spawns) + len(scenes) != len(members):
        raise ValueError("combat opening settlement needs one to three spawns and at most one scene")
    if members[:len(spawns)] != spawns or members[len(spawns):] != scenes:
        raise ValueError("combat opening settlement members are not in reducer order")
    cids: list[str] = []
    for member in members:
        if member.get("_semantic_frame_ref") != op["frame_ref"]:
            raise ValueError("combat opening member belongs to another frame")
        if "_settlement_ref" in member or "_settlement_member_index" in member:
            raise ValueError("nested mechanic settlement members cannot be projections")
        if member.get("op") == "combatant_spawn":
            cid = str(member.get("_cid") or "")
            if not cid or member.get("side") != "enemy":
                raise ValueError("combat opening spawn lacks a stable enemy identity")
            cids.append(cid)
        elif member.get("_floor") is not True or member.get("phase") != "climax":
            raise ValueError("combat opening scene is not the code-owned combat floor")
    if len(cids) != len(set(cids)):
        raise ValueError("combat opening settlement repeats a combatant identity")


def _validate_settlement_members_shape(op: dict) -> None:
    from .mechanic_settlement import (
        COMBAT_OPENING_CONTRACT,
        SKILL_CHECK_CONTRACT,
        WEAPON_ATTACK_CONTRACT,
    )

    if op.get("contract_id") == WEAPON_ATTACK_CONTRACT:
        _validate_weapon_settlement_members_shape(op)
    elif op.get("contract_id") == SKILL_CHECK_CONTRACT:
        _validate_skill_check_settlement_members_shape(op)
    elif op.get("contract_id") == COMBAT_OPENING_CONTRACT:
        _validate_combat_opening_settlement_members_shape(op)
    else:
        raise ValueError("unsupported mechanic settlement contract")


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
        if "_semantic_frame_ref" in op:
            ref = op.get("_semantic_frame_ref")
            if not isinstance(ref, str) or _SEMANTIC_FRAME_REF_RE.fullmatch(ref) is None:
                return None
        marker_ref = op.get("_settlement_ref")
        marker_index = op.get("_settlement_member_index")
        if (marker_ref is None) != (marker_index is None):
            return None
        if marker_ref is not None and (
                _SEMANTIC_FRAME_REF_RE.fullmatch(str(marker_ref)) is None
                or isinstance(marker_index, bool) or not isinstance(marker_index, int)
                or marker_index < 0):
            return None
        dependency_ref = op.get("_requires_settlement_ref")
        if dependency_ref is not None and (
                kind != "combatant_spawn"
                or _SEMANTIC_FRAME_REF_RE.fullmatch(str(dependency_ref)) is None):
            return None
        if kind == "semantic_meaning_commit":
            from .semantic_fabric import validate_compiled_meaning_receipt

            validate_compiled_meaning_receipt(op.get("meaning"))
        if kind == "semantic_binding_commit":
            from .semantic_binding import validate_meaning_binding

            validate_meaning_binding(op.get("binding"))
        if kind == "semantic_world_alignment_commit":
            from .semantic_binding import validate_world_alignment

            validate_world_alignment(op.get("alignment"))
        if kind == "semantic_frame_commit":
            from .semantic import validate_action_frame_snapshot

            validate_action_frame_snapshot(op.get("frame"))
        if kind == "claim_record":
            from .claim_frame import validate_claim_frame

            validate_claim_frame(op.get("frame"))
        if kind == "mechanic_settlement_commit":
            _validate_settlement_members_shape(op)
        if kind == "world_identity_set":
            world_id = str(op.get("world_id") or "")
            parent = str(op.get("parent_world_id") or "")
            if not re.fullmatch(r"world_[0-9a-f]{32}", world_id):
                return None
            if parent and (not re.fullmatch(r"world_[0-9a-f]{32}", parent)
                           or parent == world_id):
                return None
        if kind == "creator_world_seed":
            _creator_world_snapshot(op.get("document"), 0)
        if kind == "world_event_admit":
            validate_world_event_record(op.get("event"))
        if kind == "entity_add" and "present" in op \
                and not isinstance(op["present"], bool):
            return None
        if kind == "capability_assign":
            from .worldlex import AdapterContract, DefinitionRef, SubjectRef

            if not isinstance(op.get("definition"), dict) \
                    or not isinstance(op.get("subject"), dict):
                return None
            DefinitionRef.from_dict(op["definition"])
            SubjectRef.from_dict(op["subject"])
            acquisition_source = op.get("acquisition_source")
            if not isinstance(acquisition_source, str) \
                    or not acquisition_source.strip() \
                    or acquisition_source != acquisition_source.strip() \
                    or len(acquisition_source) > 160:
                return None
            if op.get("adapter_contract") is not None:
                if not isinstance(op["adapter_contract"], dict):
                    return None
                AdapterContract.from_dict(op["adapter_contract"])
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
        if kind == "fact_admit":
            if not all(isinstance(op.get(key), str) and op[key].strip()
                       for key in ("statement", "cause")):
                return None
            if op.get("proposition_id") is not None \
                    and (not isinstance(op["proposition_id"], str)
                         or not op["proposition_id"].strip()):
                return None
            if op.get("visibility", "public") not in {
                "public", "player", "actor_scoped", "hidden"
            }:
                return None
        if kind == "belief_acquire" and (
            op.get("stance") not in {"knows", "believes", "doubts", "disputes", "uncertain", "rumor"}
            or not isinstance(op.get("holder"), str) or not op["holder"].strip()
            or not (
                isinstance(op.get("statement"), str) and op["statement"].strip()
                or isinstance(op.get("proposition_id"), str) and op["proposition_id"].strip()
            )
            or not isinstance(op.get("source") or op.get("evidence_source"), str)
            or not str(op.get("source") or op.get("evidence_source")).strip()
            or op.get("visibility", "actor_scoped") not in {
                "public", "player", "actor_scoped", "hidden"
            }
        ):
            return None
        if kind == "goal" and op["action"] not in {"add", "complete", "abandon"}:
            return None
        if kind == "fact_retire" and not (op.get("fact") or op.get("statement")):
            return None                        # needs a fid or a statement to match
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
        if kind == "resource_change":
            rid = str(op.get("resource") or "")
            action = op.get("action")
            amount = op.get("amount")
            if not str(op.get("char") or "").strip() \
                    or not rid or rid == "hp" or _player_resource_id(rid) != rid \
                    or action not in {"gain", "spend", "set"} \
                    or isinstance(amount, bool) or not isinstance(amount, int) \
                    or amount < (0 if action == "set" else 1) or amount > 10**6:
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
            if (not isinstance(op.get("name"), str) or not op["name"].strip()
                    or len(op["name"].strip()) > 120):
                return None
            for field, limit in (("note", 8000), ("detail", 8000), ("giver", 300)):
                if op.get(field) is not None and (
                        not isinstance(op[field], str) or len(op[field].strip()) > limit):
                    return None
            if "stakes" in op and op["stakes"] is not None and op["stakes"] not in QUEST_STAKES:
                return None
        if kind == "quest_update":
            if (not isinstance(op.get("quest"), str) or not op["quest"].strip()
                    or len(op["quest"].strip()) > 120):
                return None
            if op.get("note") is not None and (
                    not isinstance(op["note"], str) or len(op["note"].strip()) > 8000):
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
        if kind == "combatant_spawn":              # Phase 1: instances are typed at the door
            if not str(op.get("name", "")).strip() or op["side"] not in COMBAT_SIDES:
                return None
            if op.get("tier") is not None and op["tier"] not in THREAT_TIERS:
                return None
            if op.get("char") is not None and not str(op["char"]).strip():
                return None
            if op.get("faction") is not None and (
                op["side"] != "enemy"
                or re.fullmatch(r"[a-z0-9_]{1,64}", str(op["faction"])) is None
            ):
                return None
            cohort_ref = op.get("cohort_ref")
            cohort_index = op.get("cohort_index")
            if (cohort_ref is None) != (cohort_index is None):
                return None
            if cohort_ref is not None and (
                    op["side"] != "enemy"
                    or re.fullmatch(r"[a-z0-9_]{1,64}", str(cohort_ref)) is None
                    or isinstance(cohort_index, bool) or not isinstance(cohort_index, int)
                    or not 1 <= cohort_index <= BATTLE_COHORT_CAP):
                return None
        if kind == "enemy_intent_set" and not str(op.get("actor", "")).strip():
            return None
        if kind == "combatant_hp":
            if not str(op.get("target", "")).strip():
                return None
            if isinstance(op["delta"], bool) or not isinstance(op["delta"], (int, float)):
                return None
        if kind == "combatant_defeat" and not str(op.get("target", "")).strip():
            return None
        if kind == "clash_record":                 # record, never resolve (plan doc 13)
            a, b = str(op.get("a", "")).strip(), str(op.get("b", "")).strip()
            if not a or not b or a.lower() == b.lower():
                return None
        if kind == "loot_table":
            if op["tier"] not in THREAT_TIERS or not isinstance(op["entries"], list):
                return None
        if kind == "battle_start":                 # §F: named and finite cohorts typed at the door
            if not str(op.get("name", "")).strip():
                return None
            if "cohort" in op and _validated_battle_cohort(op.get("cohort")) is None:
                return None
        if kind == "tide_set" and op.get("tide") not in BATTLE_TIDES:         # losing/holding/win
            return None
        if kind == "front_add":                    # Phase 2: an authored clock, typed at the door
            if not str(op.get("name", "")).strip() \
                    or not str(op.get("consequence", "")).strip():
                return None
            int(op["segments"])                    # int-able or the op is dropped
            duration = op.get("event_duration_turns")
            if duration is not None and (
                isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
            ):
                return None
            if op.get("spawn_eligibility") is not None and (
                not isinstance(op.get("spawn_eligibility"), bool)
                or not str(op.get("faction") or "").strip()
            ):
                return None
        if kind in ("front_tick", "front_reveal") and not str(op.get("front", "")).strip():
            return None
        if kind == "route_set":
            if not str(op.get("a", "")).strip() or not str(op.get("b", "")).strip():
                return None
            int(op["segments"])
    except (RuntimeError, TypeError, KeyError, ValueError, WorldLexError):
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
                        "set_nemesis": {"target"}, "world_flag": {"faction"},
                        # Phase 1: a clash lands on REAL rows — unknown names quarantine
                        # (and feed discovery); combatant_hp's target is deliberately NOT
                        # here (extras are combatant rows, not entities — resolved at apply)
                        "clash_record": {"a", "b"}}

# resolve_aliases sentinel: skip this op silently — NO quarantine and NO discovery-counter feed
# (distinct from a None-return quarantine). Used when presence/move_entity name a non-entity.
_DROP_OP = "\x00aes-drop-scene-ref"

_NARRATOR_REF_FIELDS = (_ENTITY_FIELDS | {"target", "a", "b", "from", "to"})


def _protect_narrator(op: dict, speaker: str) -> tuple[Optional[dict], str]:
    """Final authority boundary: a typed frontend narrator is never a world entity."""
    protected = str(speaker or "").strip().casefold()
    if not protected:
        return op, ""
    out = dict(op)
    if op.get("op") == "entity_add" \
            and str(op.get("name", "")).strip().casefold() == protected:
        return None, f"typed narrator '{speaker}' cannot become an entity"
    for key in _NARRATOR_REF_FIELDS:
        if key in op and str(op.get(key, "")).strip().casefold() == protected:
            return None, f"typed narrator '{speaker}' cannot be a world reference"
    if isinstance(op.get("participants"), list):
        out["participants"] = [p for p in op["participants"]
                               if str(p).strip().casefold() != protected]
        if op.get("op") == "position" and not out["participants"]:
            return None, f"typed narrator '{speaker}' cannot have a world position"
    return out, ""


def resolve_aliases(op: dict, state: dict, source: str) -> tuple[Optional[dict], str]:
    """Names -> entity ids. Unknown + source=extraction/rule -> quarantine, and the name
    feeds the 08 B2 discovery counter. Unknown + source=user -> auto-create (inspector/OOC
    authoring is normal — 02 SS12b).

    EXCEPT presence & move_entity: these only REFER to a cast member — they never MINT one and
    are never a basis for discovery. A scene/present tag (or OOC/PATCH) naming a place, a skill,
    or a typo resolves to a KNOWN entity or the op is DROPPED — not auto-created under
    source=user, not quarantined-and-fed-to-the-discovery-counter under extraction/rule. Without
    this, places/skills the DM named in a `present:` list were minted as present 'characters'
    that polluted the cast and the LLM briefing (Bean, 2026-07-08)."""
    amap = {}
    for eid, e in state.get("entities", {}).items():
        amap[e.get("name", "").lower()] = eid
        amap[eid] = eid
        for a in e.get("aliases", []):
            amap[str(a).lower()] = eid
    # ``_create`` is a historical reducer carrier, never caller authority.  Strip any supplied
    # value and rebuild the exact minimal set from unresolved user references below.
    out = {key: value for key, value in op.items() if key != "_create"}
    creates: list[dict] = []
    creates_by_eid: dict[str, dict] = {}

    def stage_user_entity(name: str) -> str:
        eid = slug(name)
        if eid not in creates_by_eid:
            row = {"eid": eid, "name": name}
            creates_by_eid[eid] = row
            creates.append(row)
        # Later references in the same owning occurrence must bind the same staged identity.
        amap[name.lower()] = eid
        amap[eid] = eid
        return eid

    if op.get("op") in ("presence", "move_entity"):
        eid = amap.get(str(op.get("entity", "")).lower())
        if eid is None:
            return None, _DROP_OP          # refer-only: a non-entity reference vanishes silently
        out["entity"] = eid
        return out, ""
    extra = _ENTITY_FIELDS_BY_OP.get(op.get("op"), set())
    entity_fields = (_ENTITY_FIELDS | extra) & set(op.keys())
    if op.get("op") == "capability_assign":
        entity_fields.discard("subject")  # structured WorldLex SubjectRef, never a name alias
    if op.get("op") == "entity_add":
        entity_fields.discard("entity")  # chosen identity, not a reference to an existing entity
    for f in sorted(entity_fields):
        if f in extra and op[f] is None:
            continue                       # null bond target = clear (doc 07 §7.8)
        name = str(op[f])
        eid = amap.get(name.lower())
        if eid is None:
            if source == "user":
                eid = stage_user_entity(name)
            else:
                return None, f"unknown entity '{name}' (08 B2 discovery counts evidence)"
        out[f] = eid
    if op.get("op") == "position":
        parts = []
        for name in op.get("participants", []):
            eid = amap.get(str(name).lower())
            if eid is None:
                if source != "user":
                    return None, f"unknown entity '{name}'"
                eid = stage_user_entity(str(name))
            parts.append(eid)
        out["participants"] = parts
    if creates:
        out["_create"] = creates
    return out, ""


def _expand_user_alias_occurrences(op: dict, turn: int) -> list[dict]:
    """Turn one live user alias carrier into explicit replay/projector occurrences.

    Historical journals retain ``_create`` on their owning operation and replay through
    ``_ensure_entities``.  New live applies instead journal one exact ``entity_add`` per staged
    identity before the owning operation.  The initial ``present=True`` preserves the historical
    user-authoring result without lending the owning operation an ``entities``-root mutation.
    """
    owning = dict(op)
    raw_creates = owning.pop("_create", None)
    if raw_creates is None:
        return [owning]
    if not isinstance(raw_creates, list) or not raw_creates:
        raise OpReject("user alias creation carrier is malformed")
    occurrences: list[dict] = []
    seen: set[str] = set()
    for raw in raw_creates:
        if not isinstance(raw, dict) or set(raw) != {"eid", "name"}:
            raise OpReject("user alias creation carrier is malformed")
        eid, name = str(raw["eid"]), str(raw["name"])
        if not name or eid != slug(name):
            raise OpReject("user alias creation identity is malformed")
        if eid in seen:
            continue
        seen.add(eid)
        occurrences.append({
            "op": "entity_add",
            "entity": eid,
            "name": name,
            "kind": "character",
            "present": True,
            "_turn": turn,
        })
    if not occurrences:
        raise OpReject("user alias creation carrier has no unique identity")
    occurrences.append(owning)
    return occurrences


# ---- RPG-4 location registry & canonicalization (doc 05 §9) -------------------------
_LOC_ARTICLES = ("the ", "a ", "an ")
_LOC_HEAD_RE = re.compile(r"[,;:.(·•|]|\s[—–-]\s")   # name head before prose; keeps 'Vael-Cora'
#             GLM-5.2 separates sub-locations with a MIDDLE DOT ("Vael Thyrr · temple
#             quarter") — split on it too or the whole string mints a twin (2026-07-09)


_FACT_STOP = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "is",
                        "are", "was", "were", "has", "have", "it", "its", "that", "this"})


def _fact_tokens(text: str) -> frozenset:
    """Content tokens for fact/memory similarity (compression item 3). Pure text math —
    deterministic at apply time, so supersede decisions bake into the journal order."""
    return frozenset(t for t in re.findall(r"[a-z0-9']+", str(text or "").lower())
                     if t not in _FACT_STOP)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
    # PARENT rung (2026-07-09 Cinderveil live): the extractor/DM often writes a known place
    # PLUS a sub-area specifier ("Ashen Maw rim", "Vael Thyrr archive hall"). When exactly
    # one location's own name-HEAD tokens (≥2 — never a one-word swallow) are all contained
    # in the raw head's tokens, that place is the parent — resolve to it, never mint a twin.
    if toks:
        phits = []
        for eid, e in locs:
            chead = _LOC_HEAD_RE.split(str(e.get("name", "")).strip(), 1)[0].strip()
            ctoks = set(_norm_loc(chead).split())
            if len(ctoks) >= 2 and ctoks <= toks:
                phits.append(eid)
        if len(set(phits)) == 1:
            return phits[0], (ents[phits[0]] or {}).get("name", head), False
    return slug(head), head, True


_PLAYER_ATTRIBUTE_STRUCTURE_KEYS = frozenset({
    "eid", "hp", "health", "resources", "resource", "stats", "stat", "skills", "skill",
    "abilities", "ability", "cooldowns", "cooldown", "ability_cd", "mastery", "level", "xp",
    "experience", "stat_points", "defs",
})


def _extraction_player_attribute_violation(op: dict, source: str,
                                           state: dict) -> Optional[str]:
    """Keep generic extraction attributes out of Player-owned mechanic namespaces.

    Historical extraction rows remain replayable; this is an apply-time admission boundary for
    new proposals. Descriptive attributes are still allowed, but HP, arbitrary declared resource
    pools, and Player-card structures cannot acquire a second, contradictory value in
    ``state.attributes``.
    """
    if source != "extraction" or op.get("op") != "set_attribute":
        return None
    entity = str(op.get("entity") or "")
    player = (state.get("player") or {}).get(entity)
    if not isinstance(player, dict):
        return None

    raw_key = str(op.get("key") or "").strip()
    root_text = re.split(r"[.\[\]/:]", raw_key, maxsplit=1)[0]
    root_key = slug(root_text)
    full_key = slug(raw_key)
    player_structures = set(_PLAYER_ATTRIBUTE_STRUCTURE_KEYS)
    player_structures.update(slug(str(key)) for key in player)
    if root_key in player_structures or full_key in player_structures:
        return f"extraction cannot shadow code-owned Player field '{raw_key}' with set_attribute"

    resource_names: set[str] = set()
    for resource_id, row in (player.get("resources") or {}).items():
        resource_names.add(slug(str(resource_id)))
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            resource_names.add(slug(str(row["name"])))
    if root_key in resource_names or full_key in resource_names:
        return f"extraction cannot shadow declared Player resource '{raw_key}' with set_attribute"
    return None


def _committed_cohort_spawn_authorized(state: dict, op: dict) -> bool:
    """Validate one later code-derived actor against the committed finite plan."""
    battle = state.get("battle") or {}
    cohort = battle.get("cohort")
    spec = _validated_battle_cohort(cohort)
    if not battle.get("active") or spec is None or not isinstance(cohort, dict):
        return False
    try:
        index = int(op.get("cohort_index"))
        spawned = int(cohort.get("spawned", 0))
        initial_count = int(cohort.get("initial_count", 0))
        remaining = int(cohort.get("remaining", spec["total"] - spawned))
    except (TypeError, ValueError):
        return False
    if index != spawned + 1 or index <= initial_count or remaining <= 0:
        return False
    return (
        op.get("side") == "enemy"
        and op.get("cohort_ref") == spec["id"]
        and index <= spec["total"]
        and str(op.get("name") or "").strip() == spec["name"]
        and str(op.get("tier") or "standard") == spec["tier"]
        and str(op.get("armament") or "").strip() == spec["armament"]
    )


def authority_violation(
    op: dict,
    source: str,
    state: dict,
    cfg,
    *,
    semantic_ingress_authorized: bool = False,
) -> Optional[str]:
    """Returns a human-readable rejection reason, or None if the op may apply."""
    kind = op["op"]
    family = _FAMILY.get(kind, "scene")
    frozen = bool(state.get("frozen"))
    raw = cfg.consent.mode == "unrestricted"
    nonlive = state.get("scene", {}).get("mode") in ("flashback", "dream")

    player_attribute_violation = _extraction_player_attribute_violation(op, source, state)
    if player_attribute_violation is not None:
        return player_attribute_violation

    if "_requires_settlement_ref" in op and source != "rule":
        return "mechanic-dependent scene admission is code-owned and rule-only"

    if kind in (
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
        "mechanic_settlement_commit",
        "claim_record",
    ) \
            or "_semantic_frame_ref" in op \
            or "_settlement_ref" in op:
        if source != "rule":
            return "semantic receipts and action frames are code-owned: only a trusted rule may " \
                   "commit or reference one"
        spec = getattr(cfg, "specialization", None)
        if spec is None or spec.name != "rpg":
            return "semantic action frames are RPG-only; specialization=none remains inert"

    if kind == "fact_admit":
        authority = str(op.get("authority") or "")
        cause = str(op.get("cause") or "")
        allowed = {
            "user": {"creator"},
            "genesis": {"genesis"},
            "rule": {"rule", "mechanic_settlement", "semantic_transition_truth"},
        }
        if authority not in allowed.get(source, set()):
            return "accepted fact authority does not match its privileged ingress"
        exact_prefixes = {
            "creator": ("creator:",),
            "genesis": ("genesis:",),
            "rule": ("rule:", "tier0-"),
            "mechanic_settlement": ("mechanic:settlement:",),
            "semantic_transition_truth": ("semantic-transition-truth:",),
        }
        if not cause.startswith(exact_prefixes[authority]):
            return "accepted fact cause lacks its exact privileged or code-owned identity"
        return None

    if kind == "belief_acquire" and source == "extraction":
        claim_id = str(op.get("claim_id") or "").strip()
        if claim_id:
            statement = str(op.get("statement") or "").strip()
            teller = str(op.get("teller") or "").strip()
            evidence = str(op.get("evidence_source") or op.get("source") or "").strip()
            try:
                identity = proposition_id(statement)
                polarity = normalized_proposition(statement)[1]
            except ValueError:
                return "extraction belief Claim Record link lacks an exact proposition"
            matches = [
                row for row in state.get("claims") or []
                if isinstance(row, dict) and row.get("claim_id") == claim_id
            ]
            frame = matches[0].get("frame") if len(matches) == 1 else None
            if not isinstance(frame, dict) \
                    or evidence not in {"told", "overheard"} \
                    or not teller \
                    or str(frame.get("speaker") or "").casefold() != teller.casefold() \
                    or frame.get("proposition_identity") != identity \
                    or frame.get("proposition_polarity") != polarity:
                return "extraction belief Claim Record link is forged, ambiguous, or mismatched"
        return None

    if kind == "player_seed":                  # privileged: initialization only (doc 05 §5.1)
        return None if source in ("user", "genesis") else \
            "player card is privileged: genesis or the user only (doc 05 §5.1)"

    if kind == "world_identity_set":
        return None if source in ("user", "genesis", "rule") else \
            "world identity is minted or imported only at a privileged authoring boundary"

    if kind == "creator_world_seed":
        return None if source in ("user", "genesis") else \
            "Creator world source is authored only by the user or genesis boundary"

    if kind == "capability_assign":
        return None if source in ("user", "genesis", "rule") else \
            "capability acquisition is ledger-authorized: models may recognize meaning, " \
            "but only the engine or user may assign it"

    if kind == "world_event_admit":
        if source not in ("user", "genesis", "rule"):
            return "world events require an exact privileged or code-settled cause"
        spec = getattr(cfg, "specialization", None)
        if spec is None or spec.name != "rpg":
            return "world events are RPG-only; specialization=none remains inert"
        event = op.get("event") or {}
        current_world = str((state.get("world_identity") or {}).get("world_id") or "")
        if not current_world or event.get("world_id") != current_world:
            return "world event belongs to a stale, forged, or cross-world identity"
        if source == "rule" and event.get("cause_authority") not in {
            "rule", "mechanic_settlement", "semantic_transition_truth"
        }:
            return "rule event cause authority does not match its ingress"
        if source == "user" and event.get("cause_authority") != "creator":
            return "Creator event cause authority does not match its ingress"
        if source == "genesis" and event.get("cause_authority") != "genesis":
            return "authored event cause authority does not match its ingress"
        return None

    if kind == "ability_grant":                # RPG-3: power is ACQUIRED in-world, never asserted
        return None if source in ("user", "genesis", "rule") else \
            "abilities are earned in-world: the engine grants them (quest/ritual/user) — " \
            "extraction may only witness, not bestow (doc 10)"

    if kind == "resource_change":
        if source != "rule":
            return "resource pools are code-owned: only a trusted rule may gain, spend, or set " \
                   "an already-declared non-HP pool"
        spec = getattr(cfg, "specialization", None)
        if spec is None or spec.name != "rpg":
            return "resource pool rules are RPG-only; specialization=none remains inert"

    if kind in ("award_exp", "level_up", "master_tick", "evolve_def", "defeat_resolve"):
        return None if source in ("user", "genesis", "rule") else \
            "progression is code-awarded: XP, levels, mastery, and defeat are earned " \
            "through resolved play, never asserted (doc 10)"   # RPG-5

    if kind == "stat_spend":                   # spending a banked point is the player's call
        return None if source in ("user", "genesis", "rule") else \
            "stat points are spent by the player, never asserted by a model (doc 10)"

    if kind in ("front_add", "front_tick", "route_set"):
        return None if source in ("user", "genesis", "rule") else \
            "the living world is engine-owned: fronts are authored frozen and advanced by " \
            "code (world_ops) — a model may only surface one via [rumor] (plan doc 13)"

    if kind == "battle_start" and isinstance(op.get("cohort"), dict):
        if source not in ("user", "genesis", "rule"):
            return "finite cohort declarations require a privileged source and live ingress receipt"
        if not semantic_ingress_authorized:
            return "finite cohort syntax is not authority: a live scope-bound ingress receipt " \
                   "must admit the declaration before mutation"
        return None

    if kind == "combatant_spawn" and op.get("cohort_ref") is not None:
        if semantic_ingress_authorized and source in ("user", "genesis", "rule"):
            return None
        if source != "rule":
            return "finite cohort actors are admitted only by the live declaration or the " \
                   "code referee's committed cohort plan"
        if not _committed_cohort_spawn_authorized(state, op):
            return "cohort spawn lacks live declaration authority or an exact committed-wave derivation"
        return None

    if kind == "combatant_spawn" and "_intro_intent_visible" in op \
            and (source != "rule"
                 or op.get("_intro_intent_visible") != "known-opponent-opening/1"):
        return "known-opponent opening provenance is an exact code-owned rule receipt"

    if kind in ("combatant_spawn", "combatant_defeat", "combat_end", "loot_table"):
        return None if source in ("user", "genesis", "rule") else \
            "combat instances are engine-owned: spawn/defeat/end and loot tables are " \
            "privileged — the DM introduces foes via the [foe] tag (validated, re-sourced), " \
            "never by writing state (plan doc 13)"   # Phase 1: code resolves, the model narrates

    if kind == "enemy_intent_set" and source != "rule":
        return "enemy intent is selected only by the code referee from a frozen enemy kit"

    if kind == "hp_adj" and isinstance(op.get("_opposition"), dict) and source != "rule":
        return "enemy action receipts are code-owned; only the rule path may commit opposition"

    if kind in ("battle_start", "battle_wave", "battle_end"):   # §F: the macro battle is engine-
        return None if source in ("user", "genesis", "rule") else \
            "the large-scale battle is engine-owned: it opens by the [battle] tag (validated, " \
            "re-sourced) and the referee sends the waves — a model narrates the macro and " \
            "reports the tide with [tide], but never writes the battle (plan doc 13 §F)"
    # tide_set is PROPOSABLE (the DM's [tide] report) — clamped +/-1 step/turn at apply; it
    # falls through to the default proposable path (no privileged guard here).

    if kind == "fact_retire":                  # compression item 3: only code or the user may
        return None if source in ("user", "genesis", "rule") else \
            "facts are retired by the engine or the user, never by a model — truth " \
            "leaves the ledger the same way it entered: through authority"

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


_PLAYER_RESOURCE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_CUSTOM_PLAYER_RESOURCES = 20
_AUTOMATIC_PLAYER_RESOURCES = frozenset({"stamina", "mana"})


def _player_resource_id(value: Any) -> str:
    """Canonical mechanics key for a Player resource; empty/punctuation-only ids reject."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _player_resource_row(spec: dict, default_max: int = 0) -> dict:
    """Clamp one journaled Player pool while retaining its bounded display metadata."""
    mx = _clamp(spec.get("max", spec.get("cur", default_max)), 0, 10**6)
    cur = _clamp(spec.get("cur", spec.get("current", mx)), 0, mx)
    row: dict = {"cur": cur, "max": mx}
    name = str(spec.get("name") or "").strip()[:60]
    if name:
        row["name"] = name
    color = str(spec.get("color") or "").strip()
    if _PLAYER_RESOURCE_COLOR_RE.fullmatch(color):
        row["color"] = color
    return row


def _int_or(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _char(state: dict, eid: str) -> dict:
    return state["chars"].setdefault(eid, {
        "affect": {"valence": 0, "energy": 0, "dominance": 0},
        "arousal": {"arousal": 0, "inhibition": 50, "orgasms_this_scene": 0, "edging": False},
        "goals": [], "secrets": [], "status_effects": [], "obsessions": {}, "cravings": {}})


def resolve_entity_ref(state: dict, token) -> Optional[str]:
    """Resolve a token to an ALREADY-EXISTING entity id, or None. Matches by exact id, then
    slug, then case-insensitive name/alias. Presence & movement REFER to a known entity with
    this and never MINT one: creating a person is the privileged, evidence-gated discovery
    path (03 §5.1), so a scene/present tag naming a PLACE, a SKILL, or a typo must resolve to
    a real cast member or be dropped — never conjured into a present 'character'. It also
    collapses the display-name/slug twin ('Marla' vs 'marla'). Pure over `state` (no config,
    registry, or RNG) -> replay-deterministic."""
    t = str(token or "").strip()
    if not t:
        return None
    ents = state.get("entities") or {}
    if t in ents:
        return t
    s = slug(t)
    if s in ents:
        return s
    low = t.lower()
    for eid, e in ents.items():
        if not isinstance(e, dict):
            continue
        if str(e.get("name", "")).lower() == low:
            return eid
        if any(str(a).lower() == low for a in (e.get("aliases") or [])):
            return eid
    return None


def _ensure_entities(state: dict, op: dict) -> None:
    for c in op.get("_create", []):
        state["entities"].setdefault(c["eid"], {
            "kind": "character", "name": c["name"], "aliases": [], "location_id": None,
            "present": True})


class OpReject(ValueError):
    """Raised by a reducer arm when a transactional precondition fails (doc 07 §7): apply_delta
    quarantines the op with THIS message and state is left untouched (invariant 3). Journaled
    ops never contain a rejected op, so replay never sees one."""


def _inherit_semantic_frame_ref(op: dict, exact_causes: list[dict]) -> dict:
    """Copy one frame reference only when the complete causal set agrees.

    Derived referee passes receive a whole current batch, but that batch is not one semantic
    action.  Selecting the first or last reference would silently merge unrelated Player actions.
    Callers therefore pass only the operations that exactly caused *this* consequence.  Legacy,
    autonomous, reconciliation, malformed, missing-reference, and mixed-reference sets all remain
    deliberately unreferenced; apply/replay performs the stronger committed-frame authority check.
    """
    out = dict(op)
    out.pop("_semantic_frame_ref", None)
    causes = list(exact_causes or [])
    if not causes:
        return out
    # A code-owned opposition receipt is an autonomous enemy occurrence.  It cannot lend a
    # current Player frame to defeat, combat-end, or any other derived consequence.
    if any(isinstance(cause, dict) and isinstance(cause.get("_opposition"), dict)
           for cause in causes):
        return out
    refs: list[str] = []
    for cause in causes:
        if not isinstance(cause, dict):
            return out
        ref = cause.get("_semantic_frame_ref")
        if not isinstance(ref, str) or _SEMANTIC_FRAME_REF_RE.fullmatch(ref) is None:
            return out
        refs.append(ref)
    if len(set(refs)) == 1:
        out["_semantic_frame_ref"] = refs[0]
    return out


def _semantic_meaning_for_frame(state: dict, frame: dict, turn: int) -> Optional[dict]:
    """Resolve the content-free recognition receipt referenced by a V2 action frame."""
    if frame.get("schema") == "semantic-action-frame/1":
        return None
    ref = frame.get("meaning_ref")
    rows = state.get("semantic_meanings")
    if not isinstance(ref, str) or not isinstance(rows, list):
        raise OpReject("semantic action frame has no committed meaning receipt")
    meaning = next(
        (
            row.get("meaning") for row in reversed(rows)
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("meaning"), dict)
            and row["meaning"].get("fingerprint") == ref
        ),
        None,
    )
    if meaning is None:
        raise OpReject("semantic action frame meaning is not committed for this turn")
    try:
        from .semantic_fabric import validate_compiled_meaning_receipt

        meaning = validate_compiled_meaning_receipt(meaning)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise OpReject("committed semantic meaning receipt is malformed") from exc
    context = frame.get("context_frame") or {}
    if meaning.get("fabric_fingerprint") != frame.get("fabric_fingerprint") \
            or meaning.get("source_fingerprint") != context.get("source_fingerprint") \
            or meaning.get("genre_ids") != context.get("genre_ids"):
        raise OpReject("semantic action frame does not match its meaning receipt")
    return meaning


def _semantic_meaning_for_binding(state: dict, binding: dict, turn: int) -> dict:
    """Resolve and revalidate the exact same-turn meaning consumed by one binding."""
    ref = binding.get("meaning_ref")
    rows = state.get("semantic_meanings")
    if not isinstance(ref, str) or not isinstance(rows, list):
        raise OpReject("semantic meaning binding has no committed meaning receipt")
    meaning = next(
        (
            row.get("meaning") for row in reversed(rows)
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("meaning"), dict)
            and row["meaning"].get("fingerprint") == ref
        ),
        None,
    )
    if meaning is None:
        raise OpReject("semantic meaning binding receipt is not committed for this turn")
    try:
        from .semantic_binding import validate_meaning_binding
        from .semantic_fabric import validate_compiled_meaning_receipt

        meaning = validate_compiled_meaning_receipt(meaning)
        validate_meaning_binding(binding, meaning_receipt=meaning)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise OpReject("semantic meaning binding does not match its meaning receipt") from exc
    return meaning


def _semantic_binding_by_ref(state: dict, ref: object, turn: int) -> dict:
    """Resolve one exact same-turn binding from the detached receipt ledger."""
    if not isinstance(ref, str) or _SEMANTIC_FRAME_REF_RE.fullmatch(ref) is None:
        raise OpReject("semantic meaning binding reference is malformed")
    rows = state.get("semantic_bindings")
    if not isinstance(rows, list):
        raise OpReject("semantic meaning binding reference has no committed binding ledger")
    binding = next(
        (
            row.get("binding") for row in reversed(rows)
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("binding"), dict)
            and row["binding"].get("fingerprint") == ref
        ),
        None,
    )
    if binding is None:
        raise OpReject("semantic meaning binding is not committed for this turn")
    try:
        from .semantic_binding import validate_meaning_binding

        binding = validate_meaning_binding(binding)
    except (TypeError, ValueError) as exc:
        raise OpReject("committed semantic meaning binding is malformed") from exc
    return binding


def _semantic_alignment_by_ref(state: dict, ref: object, turn: int) -> dict:
    """Resolve one exact same-turn world alignment from its detached receipt ledger."""
    if not isinstance(ref, str) or _SEMANTIC_FRAME_REF_RE.fullmatch(ref) is None:
        raise OpReject("semantic world alignment reference is malformed")
    rows = state.get("semantic_world_alignments")
    if not isinstance(rows, list):
        raise OpReject("semantic world alignment reference has no committed alignment ledger")
    alignment = next(
        (
            row.get("alignment") for row in reversed(rows)
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("alignment"), dict)
            and row["alignment"].get("fingerprint") == ref
        ),
        None,
    )
    if alignment is None:
        raise OpReject("semantic world alignment is not committed for this turn")
    try:
        from .semantic_binding import validate_world_alignment

        alignment = validate_world_alignment(alignment)
    except (TypeError, ValueError) as exc:
        raise OpReject("committed semantic world alignment is malformed") from exc
    return alignment


def _validate_possessed_object_alignment(frame: dict, alignments: list[dict]) -> None:
    """Require positive exact ledger evidence before V3 may claim an item or its owner."""
    item_id = frame.get("possessed_object_instance_id")
    owner_id = frame.get("possessed_object_owner_id")
    if item_id is None and owner_id is None:
        return
    if not item_id or not owner_id:
        raise OpReject("semantic action frame item identity and ownership require one exact pair")

    from .capability_glossary import content_fingerprint, normalize_phrase

    recognized_value_ref = content_fingerprint({
        "role": "possessed_object",
        "value": normalize_phrase(frame.get("possessed_object", "")),
    })
    relevant = [
        alignment for alignment in alignments
        if alignment.get("role") == "possessed_object"
        and alignment.get("predicate_id") == "item.owner_equals_linguistic_possessor"
        and alignment.get("recognized_value_ref") == recognized_value_ref
    ]
    positive = [
        alignment for alignment in relevant
        if alignment.get("status") == "positive"
        and alignment.get("selection") == "exact"
        and alignment.get("cardinality") == "one"
        and alignment.get("time_scope") == frame.get("time_scope")
        and alignment.get("resolved_ids") == [item_id]
        and alignment.get("positive_authority_value") == owner_id
        and frame.get("linguistic_possessor_id") == owner_id
    ]
    if len(positive) != 1:
        raise OpReject(
            "semantic action frame item ownership lacks one positive exact world alignment"
        )


def _semantic_receipts_for_frame(
    state: dict, frame: dict, turn: int,
) -> tuple[Optional[dict], Optional[dict], list[dict]]:
    """Resolve the receipt chain required by this frame schema without granting execution."""
    meaning = _semantic_meaning_for_frame(state, frame, turn)
    if frame.get("schema") != "semantic-action-frame/3":
        return meaning, None, []

    binding = _semantic_binding_by_ref(state, frame.get("meaning_binding_ref"), turn)
    try:
        from .semantic_binding import validate_meaning_binding

        binding = validate_meaning_binding(binding, meaning_receipt=meaning)
    except (TypeError, ValueError) as exc:
        raise OpReject("semantic action frame binding does not match its meaning receipt") from exc
    context = frame.get("context_frame") or {}
    if binding.get("meaning_ref") != frame.get("meaning_ref") \
            or binding.get("source_fingerprint") != context.get("source_fingerprint") \
            or binding.get("event_node_id") != frame.get("event_node_id"):
        raise OpReject("semantic action frame does not match its exact event binding")

    alignments = [
        _semantic_alignment_by_ref(state, ref, turn)
        for ref in frame.get("world_alignment_refs", [])
    ]
    if any(alignment.get("recognition_ref") != binding.get("fingerprint")
           for alignment in alignments):
        raise OpReject("semantic world alignment belongs to a different event binding")
    _validate_possessed_object_alignment(frame, alignments)
    return meaning, binding, alignments


def _semantic_frame_for_op(state: dict, op: dict, turn: int) -> Optional[dict]:
    """Resolve one baked frame reference without consulting the source sentence.

    A reference is authority only when the exact snapshot was committed earlier in this turn.
    This also runs during replay through ``_apply_op``; legacy journal operations without a
    reference remain valid and require no migration.
    """
    ref = op.get("_semantic_frame_ref")
    if ref is None:
        return None
    if not isinstance(ref, str) or _SEMANTIC_FRAME_REF_RE.fullmatch(ref) is None:
        raise OpReject("semantic action frame reference is malformed")
    rows = state.get("semantic_frames")
    if not isinstance(rows, list):
        raise OpReject("semantic action frame reference has no committed frame ledger")
    frame = next(
        (
            row.get("frame")
            for row in reversed(rows)
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("frame"), dict)
            and row["frame"].get("fingerprint") == ref
        ),
        None,
    )
    if frame is None:
        raise OpReject("semantic action frame reference is not committed for this turn")
    try:
        from .semantic import validate_action_frame_snapshot

        frame = validate_action_frame_snapshot(frame)
    except (TypeError, ValueError) as exc:
        raise OpReject("committed semantic action frame is malformed") from exc
    _meaning, binding, _alignments = _semantic_receipts_for_frame(state, frame, turn)

    if binding is not None and binding.get("mechanic_disposition") != "candidate":
        raise OpReject("semantic action frame binding abstains from mechanic execution")

    if frame.get("polarity") != "positive" \
            or frame.get("modality") not in ("actual", "command") \
            or frame.get("time_scope") != "current" \
            or frame.get("ambiguity"):
        raise OpReject("semantic action frame abstains from mechanic execution")

    kind = op.get("op")
    if kind == "check" and (
            op.get("skill") != frame.get("capability_id")
            or op.get("char") != frame.get("actor_id")):
        raise OpReject("check identity does not match its semantic action frame")
    if kind == "combatant_spawn" and op.get("char") is not None \
            and op.get("char") != frame.get("target_entity_id"):
        raise OpReject("tracked combatant does not match its semantic action target")
    if kind == "combatant_hp":
        target_id = frame.get("target_entity_id")
        target = str(op.get("target") or "")
        exact = bool(target_id) and target == target_id
        if not exact:
            combatants = ((state.get("combat") or {}).get("combatants") or {})
            row = combatants.get(target) if isinstance(combatants, dict) else None
            exact = isinstance(row, dict) and bool(target_id) \
                and str(row.get("eid") or row.get("id") or "") == target_id
        if not exact:
            raise OpReject("combat damage target does not match its semantic action frame")
    return frame


# ---- RPG-2 item-index helpers (doc 07 §7/§8) — thin, pure state readers/writers. The
# instance's `loc` is the single source of truth; `gear`/`inventory` are derived indexes the
# render blocks read. Every mutation goes remove -> add with rollback on failure, which is what
# makes one-instance-one-place hold by construction (and the linter rule a pure safety net).
def _default_container(state: dict, eid) -> str:
    return "loose"                             # the always-available unbounded pseudo-container


def _auto_loc(state: dict, owner, snap: Optional[dict]) -> str:
    """The default landing spot for a freshly acquired item (mint/gain). Worn gear whose
    slot is FREE auto-equips onto the paper-doll (the worn-at-start fix); everything else is
    carried. Pure fn of (state, owner, baked snapshot) — replay-deterministic."""
    cls = classify_item(str((snap or {}).get("name", "")), snap)
    if owner and cls["worn"] and cls["slot"]:
        slot = cls["slot"]
        if not ((state.get("gear") or {}).get(owner) or {}).get(slot):
            return f"gear:{slot}"
    return f"inv:{_default_container(state, owner)}"


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


def _stamp_world_item_origin(state: dict, item: dict) -> None:
    """Bind an ownerless world item to the scene in which it was dropped.

    A canonical location survives leaving and returning. Older/untagged scenes have no location
    identity, so their occurrence index is the conservative fallback: once the scene changes,
    code cannot safely claim that the same ground is under the Player again.
    """
    scene = state.get("scene") if isinstance(state.get("scene"), dict) else {}
    item["world_origin_location"] = str(scene.get("location_id") or "") or None
    item["world_origin_scene"] = int(scene.get("scene_index", 0))


def _world_item_is_here(state: dict, item: dict) -> bool:
    """Return whether a provenance-bearing world item is reachable in the current scene."""
    if str(item.get("loc") or "").split(":", 1)[0] != "world" or item.get("owner"):
        return False
    scene = state.get("scene") if isinstance(state.get("scene"), dict) else {}
    origin_location = str(item.get("world_origin_location") or "")
    if origin_location:
        return origin_location == str(scene.get("location_id") or "")
    if "world_origin_scene" not in item:
        return False
    return int(item["world_origin_scene"]) == int(scene.get("scene_index", 0))


_ITEM_REFERENCE_STOP = frozenset({"a", "an", "the", "few", "some", "several", "of"})


def _item_reference_terms(text: object) -> set[str]:
    """Conservative singularized item words used only to detect custody collisions."""
    terms: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        word = raw[:-1] if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss") else raw
        if word not in _ITEM_REFERENCE_STOP:
            terms.add(word)
    return terms


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


def _player_target_eid(state: dict) -> Optional[str]:
    """Resolve a combat target even while a genesis batch has not applied player_seed yet."""
    if eid := _player_eid(state):
        return eid
    for eid, rec in (state.get("entities") or {}).items():
        if isinstance(rec, dict) and rec.get("kind") == "player":
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
    if isinstance(card.get("creator_extras"), list):
        # Typed Creator metadata for faithful card regeneration. The matching memory_event ops
        # remain the relevance-indexed lore path; this snapshot is never gameplay authority and
        # avoids guessing old extras back out of arbitrary prose.
        rec["creator_extras"] = [
            {"label": str(row.get("label") or "Note").strip()[:60],
             "text": str(row.get("text") or "").strip()[:8000]}
            for row in card["creator_extras"][:20]
            if isinstance(row, dict) and str(row.get("text") or "").strip()
        ]
    if "creator_source" in card:
        # Authoring provenance only: prefill reads this instead of projecting mutable HP,
        # resources, and inventory back into a new starting seed.  Partial runtime player_seed
        # ops omit it and therefore cannot rewrite the committed Creator source accidentally.
        rec["creator_source"] = _creator_player_source(card.get("creator_source"))
    if isinstance(card.get("stats"), dict):
        rec["stats"] = {str(k): _clamp(v, -99, 999) for k, v in card["stats"].items()}
    # Creator commits both ownership tables on every finalized card. Their joint presence is the
    # unambiguous COMPLETE capability-snapshot boundary: ranks, known abilities, and the frozen
    # definitions that make either one executable must all be replaced together. Older
    # hand-authored partial player_seed ops may still update one field at a time.
    complete_capability_snapshot = isinstance(card.get("skills"), dict) \
        and isinstance(card.get("abilities"), list)
    if complete_capability_snapshot:
        # Derived from the journaled full-card shape, so old journals replay into the strict policy
        # without a schema migration. Checkpoints that predate it retain only the narrow shared-
        # registry waiver; their frozen per-character definitions are strict regardless.
        rec["_resource_cost_policy"] = "strict/1"
    if isinstance(card.get("skills"), dict):
        rec["skills"] = {str(k): _clamp(v, 0, 99) for k, v in card["skills"].items()}
    if isinstance(card.get("abilities"), list):
        rec["abilities"] = [str(a) for a in card["abilities"]]
    if isinstance(card.get("resources"), dict):
        replacement: dict = {}
        custom_count = 0
        for rname, spec in card["resources"].items():
            if not isinstance(spec, dict):
                continue
            rid = _player_resource_id(rname)
            if not rid:
                continue
            row = _player_resource_row(spec)
            if rid == "hp":
                rec["hp"] = row
                continue
            if rid not in ("stamina", "mana") and rid not in replacement:
                if custom_count >= _MAX_CUSTOM_PLAYER_RESOURCES:
                    continue
                custom_count += 1
            replacement[rid] = row
        # An explicit map is the complete non-HP pool snapshot. Omitting `resources` remains a
        # partial seed and leaves existing pools alone; supplying it can deliberately remove one.
        rec["resources"] = replacement
    if isinstance(card.get("hp"), dict):            # hp may also be given directly
        rec["hp"] = _player_resource_row(card["hp"], int(rec["hp"].get("max", 0)))
    card_defs = card.get("defs")
    if complete_capability_snapshot:
        # A normal Creator re-save is replacement, including the empty case. Keeping a def that
        # the replacement omitted makes the old custom skill resolvable even after its rank and
        # resource bar disappeared, because frozen definitions are capability evidence. Build a
        # fresh snapshot so stale authority cannot leak across a card replacement.
        replacement_defs: dict = {}
        if isinstance(card_defs, dict):
            for table in ("skills", "abilities", "stats"):
                src_t = card_defs.get(table)
                if not isinstance(src_t, dict):
                    continue
                rows = {str(k): dict(v) for k, v in src_t.items() if isinstance(v, dict)}
                if rows:
                    replacement_defs[table] = rows
        if replacement_defs:
            rec["defs"] = replacement_defs
        else:
            rec.pop("defs", None)
    elif isinstance(card_defs, dict):               # legacy partial seed: merge named tables
        defs = rec.setdefault("defs", {})            # freestyle / mastery-evolved definitions
        for table in ("skills", "abilities", "stats"):
            src_t = card_defs.get(table)
            if isinstance(src_t, dict):
                dst = defs.setdefault(table, {})
                for k, v in src_t.items():
                    if isinstance(v, dict):          # authored frozen snapshot of fixed numbers
                        dst[str(k)] = dict(v)
    ent = state.get("entities", {}).get(eid)
    if ent is not None:
        ent["kind"] = "player"


def _validated_spawn_capability_pool(
    state: dict, op: dict, cid: str, turn: int,
) -> tuple[dict | None, list[dict]]:
    """Replay-pure validation for a code-baked enemy pool and its exact assignments."""
    raw_bundle = op.get("_capability_pool")
    raw_assignments = op.get("_capability_assignments")
    if raw_bundle is None:
        if raw_assignments is not None:
            raise OpReject("enemy capability assignments require their exact pool bundle")
        return None, []
    if not isinstance(raw_bundle, dict) or not isinstance(raw_assignments, list):
        raise OpReject("enemy capability pool evidence is malformed")
    if op.get("_kit_source") != "worldlex-runtime-pool":
        raise OpReject("enemy capability pool is not the declared kit authority")
    try:
        bundle = validate_enemy_capability_bundle(raw_bundle)
        reconstructed = reconstruct_enemy_kit(bundle)
        if reconstructed != op.get("_kit"):
            raise EnemyCapabilityPoolError(
                "enemy capability pool no longer matches the frozen kit"
            )
        assigned_pool = validate_pool(bundle["pools"]["assigned"])
        assignments = [validate_assignment(item) for item in raw_assignments]
    except (EnemyCapabilityPoolError, AssignmentError, WorldLexError,
            KeyError, TypeError, ValueError) as exc:
        raise OpReject(str(exc)) from exc

    world_id = str((state.get("world_identity") or {}).get("world_id") or "")
    if not world_id or assigned_pool.world_id != world_id:
        raise OpReject("enemy capability pool belongs to a different world")
    if assigned_pool.subject != SubjectRef("enemy", _enemy_pool_subject_id(cid), world_id):
        raise OpReject("enemy capability pool belongs to a different spawned subject")
    if assignments != sorted(assignments, key=lambda item: item.definition.definition_id):
        raise OpReject("enemy capability assignments are not in canonical definition order")
    by_definition: dict[str, Any] = {}
    for assignment in assignments:
        fingerprint = assignment.definition.fingerprint
        if fingerprint in by_definition:
            raise OpReject("enemy capability assignments contain a duplicate definition")
        if assignment.subject != assigned_pool.subject \
                or assignment.world_id != world_id \
                or assignment.acquisition_turn != turn \
                or assignment.acquisition_source \
                != f"enemy_spawn.kit.{bundle['kit_header']['fingerprint']}":
            raise OpReject("enemy capability assignment provenance does not match this spawn")
        by_definition[fingerprint] = assignment
    if set(by_definition) != {
        member.definition.fingerprint for member in assigned_pool.members
    }:
        raise OpReject("enemy assignments do not exactly match the assigned pool")
    for member in assigned_pool.members:
        assignment = by_definition[member.definition.fingerprint]
        if assignment.definition != member.definition \
                or member.assignment_ref != assignment.assignment_id:
            raise OpReject("enemy pool assignment evidence is forged")

    existing = state.get("capability_assignments") or {}
    payloads = [assignment.as_dict() for assignment in assignments]
    for payload in payloads:
        prior = existing.get(payload["assignment_id"])
        if prior is not None and prior != payload:
            raise OpReject("enemy capability assignment identity is already immutable")
    return bundle, payloads


def _apply_op(state: dict, op: dict) -> None:  # noqa: C901 — one dispatch table, kept flat on purpose
    kind = op["op"]
    turn = op.get("_turn", state["meta"].get("turn", -1))
    if "_settlement_ref" in op:
        _apply_mechanic_projection(state, op, turn)
        return
    if kind == "mechanic_settlement_commit":
        _semantic_frame_for_op(state, op, turn)
        _apply_prepared_mechanic_settlement(state, op, turn)
        return
    if "_requires_settlement_ref" in op:
        public, private = _state_settlement_pair(
            state, str(op.get("_requires_settlement_ref") or ""), turn
        )
        if public is None or private is None:
            raise OpReject("mechanic-dependent scene admission lacks its same-turn settlement")
    _semantic_frame_for_op(state, op, turn)
    _ensure_entities(state, op)

    if kind == "semantic_meaning_commit":
        try:
            from .semantic_fabric import validate_compiled_meaning_receipt

            meaning = validate_compiled_meaning_receipt(op.get("meaning"))
        except (RuntimeError, TypeError, ValueError) as exc:
            raise OpReject("semantic meaning receipt is malformed") from exc
        meanings = state.get("semantic_meanings")
        if not isinstance(meanings, list):
            meanings = []
            state["semantic_meanings"] = meanings
        prior = next((
            row for row in meanings
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("meaning"), dict)
            and row["meaning"].get("fingerprint") == meaning.get("fingerprint")
        ), None)
        if prior is not None:
            if prior.get("meaning") != meaning:
                raise OpReject("semantic meaning receipt identity conflicts with prior state")
            return
        meanings.append({"turn": turn, "meaning": json.loads(json.dumps(meaning))})
        _trim_receipt_ledger(meanings, turn)
    elif kind == "claim_record":
        try:
            from .claim_frame import validate_claim_frame, validate_claim_record

            frame = validate_claim_frame(op.get("frame"))
            record = validate_claim_record(op["_record"]) if isinstance(op.get("_record"), dict) \
                else None
        except (TypeError, ValueError) as exc:
            raise OpReject("Claim Record frame is malformed") from exc
        claims = state.setdefault("claims", [])
        identity = record["claim_id"] if record is not None else frame["fingerprint"]
        prior = next((
            row for row in claims
            if isinstance(row, dict)
            and (row.get("claim_id") or row.get("fingerprint")) == identity
        ), None)
        stored = json.loads(json.dumps(record if record is not None else {**frame, "turn": turn}))
        if prior is not None:
            if prior != stored:
                raise OpReject("Claim Record identity conflicts with prior recognition")
            return
        claims.append(stored)
        state.setdefault("propositions", {}).setdefault(frame["proposition_id"], {
            "statement": frame["proposition"],
            "authority": "recognition_only",
            "identity": frame.get("proposition_identity") or frame["proposition_id"],
        })
    elif kind == "fact_admit":
        proposition_id = str(op["proposition_id"])
        proposition_identity = str(op.get("proposition_identity") or proposition_id)
        facts = state.setdefault("facts", {})
        fid = str(op.get("_fact_id") or (
            "fact_" + hashlib.sha256(proposition_id.encode()).hexdigest()[:16]
        ))
        row = dict(op.get("_record") or {
            "schema": "aetherstate-fact-record/2",
            "fact_id": fid,
            "proposition_id": proposition_id,
            "statement": str(op["statement"]),
            "cause": str(op["cause"]),
            "authority": str(op.get("authority") or "privileged"),
            "established_turn": turn,
            "status": "accepted",
            "visibility": str(op.get("visibility") or "public"),
            "scoped_actors": list(op.get("scoped_actors") or []),
        })
        prior = facts.get(fid)
        if prior is not None and prior != row:
            raise OpReject("Fact Record identity conflicts with prior accepted truth")
        # Preserve the historical compression contract for typed facts without weakening their
        # provenance.  A near-restatement may supersede only an active record on the same exact
        # causal/authority line; unrelated facts that merely share vocabulary remain independent.
        # The prior row stays in state as immutable history and the reducer bakes only lifecycle
        # metadata, so replay reaches the same result from the journal order.
        new_tokens = _fact_tokens(row.get("statement", ""))
        if new_tokens:
            for old_id, old in facts.items():
                if old_id == fid or not isinstance(old, dict) \
                        or old.get("retired_turn") is not None:
                    continue
                if old.get("cause") != row.get("cause") \
                        or old.get("authority") != row.get("authority"):
                    continue
                old_tokens = _fact_tokens(old.get("statement", ""))
                if old_tokens and _jaccard(new_tokens, old_tokens) >= 0.75:
                    old["status"] = "superseded"
                    old["retired_turn"] = turn
                    old["superseded_by"] = fid
        facts[fid] = row
        state.setdefault("propositions", {})[proposition_id] = {
            "statement": row["statement"], "authority": row["authority"],
            "identity": row.get("proposition_identity") or proposition_identity,
        }
    elif kind == "belief_acquire":
        proposition_id = str(op["proposition_id"])
        holder = str(op["holder"])
        key = f"{holder}|{proposition_id}"
        row = dict(op.get("_record") or {
            "schema": "aetherstate-epistemic-record/2",
            "belief_id": str(op.get("_belief_id") or ""),
            "holder": holder,
            "proposition_id": proposition_id,
            "statement": str(op.get("statement") or ""),
            "stance": str(op["stance"]),
            "source": str(op.get("source") or op.get("evidence_source")),
            "claim_id": op.get("claim_id"),
            "acquired_turn": turn,
            "visibility": str(op.get("visibility") or "actor_scoped"),
            "scoped_actors": list(op.get("scoped_actors") or [holder]),
            "status": "current",
        })
        beliefs = state.setdefault("beliefs", {})
        prior = beliefs.get(key)
        if isinstance(prior, dict) and prior != row:
            retired = {**prior, "status": "superseded", "superseded_turn": turn,
                       "superseded_by": row.get("belief_id")}
            history = state.setdefault("epistemic_history", [])
            if not any(existing.get("belief_id") == retired.get("belief_id")
                       for existing in history if isinstance(existing, dict)):
                history.append(retired)
        beliefs[key] = row
        state.setdefault("propositions", {}).setdefault(proposition_id, {
            "statement": row.get("statement", ""),
            "authority": "epistemic_only",
            "identity": row.get("proposition_identity")
            or op.get("proposition_identity") or proposition_id,
        })
    elif kind == "world_event_admit":
        try:
            event = validate_world_event_record(op.get("event"))
        except (TypeError, ValueError) as exc:
            raise OpReject("World Event Record is malformed") from exc
        current_world = str((state.get("world_identity") or {}).get("world_id") or "")
        if not current_world or event["world_id"] != current_world:
            raise OpReject("World Event Record belongs to another world")
        if event["turn"] != turn:
            raise OpReject("World Event Record turn is stale or forged")
        records = state.setdefault("world_events", [])
        prior = next((row for row in records if row.get("event_id") == event["event_id"]), None)
        if prior is not None:
            if prior != event:
                raise OpReject("World Event Record identity is immutable")
            return
        if event["kind"] != "admission" and not any(
            row.get("event_id") == event["relation_target"] for row in records
        ):
            raise OpReject("World Event Record relation target is missing")
        records.append(json.loads(json.dumps(event)))
        overlay_branch = str(state.get("world_event_branch_id") or event["branch_id"])
        inherited_origins = state.get("world_event_source_branch_ids")
        if not isinstance(inherited_origins, list):
            inherited_origins = []
        state["world_overlay"] = project_world_overlay(
            records,
            world_id=current_world,
            session_id=event["session_id"],
            branch_id=overlay_branch,
            source_branch_ids=inherited_origins,
            game_time=turn if event.get("schema") == "aetherstate-world-event-record/2"
            else int((state.get("clock") or {}).get("minutes", 0)),
        )
    elif kind == "semantic_binding_commit":
        try:
            from .semantic_binding import validate_meaning_binding

            binding = validate_meaning_binding(op.get("binding"))
        except (TypeError, ValueError) as exc:
            raise OpReject("semantic meaning binding is malformed") from exc
        occurrence = str(binding.get("event_node_id") or "").removeprefix("event.")
        if not re.fullmatch(rf"t{int(turn)}\.f[1-9][0-9]*", occurrence) \
                or binding.get("binding_id") != f"binding.{occurrence}":
            raise OpReject("semantic meaning binding lacks its canonical turn occurrence id")
        _semantic_meaning_for_binding(state, binding, turn)
        bindings = state.get("semantic_bindings")
        if not isinstance(bindings, list):
            bindings = []
            state["semantic_bindings"] = bindings
        same_id = next((
            row for row in bindings
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("binding"), dict)
            and row["binding"].get("binding_id") == binding.get("binding_id")
        ), None)
        if same_id is not None \
                and same_id["binding"].get("fingerprint") != binding.get("fingerprint"):
            raise OpReject("semantic meaning binding identity conflicts with prior state")
        same_event = next((
            row for row in bindings
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("binding"), dict)
            and row["binding"].get("event_node_id") == binding.get("event_node_id")
        ), None)
        if same_event is not None \
                and same_event["binding"].get("fingerprint") != binding.get("fingerprint"):
            raise OpReject("semantic event already has a different meaning binding this turn")
        prior = next((
            row for row in bindings
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("binding"), dict)
            and row["binding"].get("fingerprint") == binding.get("fingerprint")
        ), None)
        if prior is not None:
            if prior.get("binding") != binding:
                raise OpReject("semantic meaning binding identity conflicts with prior state")
            return
        bindings.append({"turn": turn, "binding": json.loads(json.dumps(binding))})
        _trim_receipt_ledger(bindings, turn)
    elif kind == "semantic_world_alignment_commit":
        try:
            from .semantic_binding import validate_world_alignment

            alignment = validate_world_alignment(op.get("alignment"))
        except (TypeError, ValueError) as exc:
            raise OpReject("semantic world alignment is malformed") from exc
        binding = _semantic_binding_by_ref(state, alignment.get("recognition_ref"), turn)
        _semantic_meaning_for_binding(state, binding, turn)
        alignments = state.get("semantic_world_alignments")
        if not isinstance(alignments, list):
            alignments = []
            state["semantic_world_alignments"] = alignments
        identity = (
            alignment.get("recognition_ref"),
            alignment.get("role"),
            alignment.get("predicate_id"),
            alignment.get("recognized_value_ref"),
        )
        same_identity = next((
            row for row in alignments
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("alignment"), dict)
            and (
                row["alignment"].get("recognition_ref"),
                row["alignment"].get("role"),
                row["alignment"].get("predicate_id"),
                row["alignment"].get("recognized_value_ref"),
            ) == identity
        ), None)
        if same_identity is not None \
                and same_identity["alignment"].get("fingerprint") \
                != alignment.get("fingerprint"):
            raise OpReject("semantic world alignment identity conflicts with prior state")
        prior = next((
            row for row in alignments
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("alignment"), dict)
            and row["alignment"].get("fingerprint") == alignment.get("fingerprint")
        ), None)
        if prior is not None:
            if prior.get("alignment") != alignment:
                raise OpReject("semantic world alignment identity conflicts with prior state")
            return
        alignments.append({"turn": turn, "alignment": json.loads(json.dumps(alignment))})
        _trim_receipt_ledger(alignments, turn)
    elif kind == "semantic_frame_commit":
        try:
            from .semantic import validate_action_frame_snapshot

            frame = validate_action_frame_snapshot(op.get("frame"))
        except (TypeError, ValueError) as exc:
            raise OpReject("semantic action frame snapshot is malformed") from exc
        if frame.get("schema") == "semantic-action-frame/3":
            frame_id = str(frame.get("frame_id") or "")
            if re.fullmatch(rf"t{int(turn)}\.f[1-9][0-9]*", frame_id) is None \
                    or frame.get("event_node_id") != f"event.{frame_id}" \
                    or frame.get("meaning_binding_ref") is None:
                raise OpReject("semantic V3 frame lacks its canonical turn occurrence identity")
            binding = _semantic_binding_by_ref(state, frame.get("meaning_binding_ref"), turn)
            if binding.get("binding_id") != f"binding.{frame_id}" \
                    or binding.get("event_node_id") != frame.get("event_node_id"):
                raise OpReject("semantic V3 frame and binding occurrence identities disagree")
        _semantic_receipts_for_frame(state, frame, turn)
        frames = state.get("semantic_frames")
        if not isinstance(frames, list):
            frames = []
            state["semantic_frames"] = frames
        prior = next((
            row for row in frames
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("frame"), dict)
            and row["frame"].get("fingerprint") == frame.get("fingerprint")
        ), None)
        is_v3 = frame.get("schema") == "semantic-action-frame/3"
        same_frame_id = next((
            row for row in frames
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("frame"), dict)
            and is_v3
            and row["frame"].get("frame_id") == frame.get("frame_id")
        ), None)
        if same_frame_id is not None \
                and same_frame_id["frame"].get("fingerprint") != frame.get("fingerprint"):
            raise OpReject("semantic frame occurrence identity conflicts with prior state")
        if prior is not None:
            if prior.get("frame") != frame:
                raise OpReject("semantic action frame identity conflicts with prior state")
            return
        same_event = next((
            row for row in frames
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("frame"), dict)
            and is_v3
            and row["frame"].get("event_node_id") == frame.get("event_node_id")
        ), None)
        same_binding = next((
            row for row in frames
            if isinstance(row, dict)
            and row.get("turn") == turn
            and isinstance(row.get("frame"), dict)
            and is_v3
            and frame.get("meaning_binding_ref") is not None
            and row["frame"].get("meaning_binding_ref") == frame.get("meaning_binding_ref")
        ), None)
        if any(row is not None and row["frame"].get("fingerprint") != frame.get("fingerprint")
               for row in (same_event, same_binding)):
            raise OpReject(
                "semantic event or meaning binding already has a different action frame this turn"
            )
        # JSON cloning prevents a caller from mutating the committed snapshot after apply.
        frames.append({"turn": turn, "frame": json.loads(json.dumps(frame))})
        _trim_receipt_ledger(frames, turn)
    elif kind == "world_identity_set":
        current = state.get("world_identity")
        if isinstance(current, dict) and current.get("world_id"):
            if current.get("world_id") != op["world_id"]:
                raise OpReject("this session is already bound to a different world lineage")
            if str(current.get("parent_world_id") or "") != str(op.get("parent_world_id") or ""):
                raise OpReject("world lineage parent cannot change after identity is committed")
            return
        identity = {"schema": "world-identity/1", "world_id": op["world_id"],
                    "created_turn": turn}
        if op.get("parent_world_id"):
            identity["parent_world_id"] = op["parent_world_id"]
        state["world_identity"] = identity
    elif kind == "creator_world_seed":
        expected = _creator_world_snapshot(op.get("document"), turn)
        supplied = op.get("_snapshot")
        if supplied is not None and supplied != expected:
            raise OpReject("Creator world source snapshot fingerprint is forged or stale")
        identity = state.get("world_identity") or {}
        if expected["world_id"] != identity.get("world_id"):
            raise OpReject("Creator world source belongs to a stale or cross-world identity")
        current = state.get("creator_world")
        if isinstance(current, dict) and current.get("fingerprint"):
            if current.get("world_id") != expected["world_id"] \
                    or current.get("document") != expected["document"]:
                raise OpReject("Creator world source is immutable after it is committed")
            return
        state["creator_world"] = expected
    elif kind == "capability_assign":
        raw_assignment = op.get("_assignment")
        if not isinstance(raw_assignment, dict):
            raise OpReject("capability assignment is missing its baked authority record")
        try:
            assignment = validate_assignment(raw_assignment)
        except AssignmentError as exc:
            raise OpReject(str(exc)) from exc
        identity = state.get("world_identity") or {}
        if assignment.world_id != identity.get("world_id"):
            raise OpReject("capability assignment belongs to a different world")
        if assignment.definition.as_dict() != op.get("definition") \
                or assignment.subject.as_dict() != op.get("subject") \
                or assignment.acquisition_source != op.get("acquisition_source") \
                or assignment.acquisition_turn != turn:
            raise OpReject("capability assignment does not match its journal op")
        payload = assignment.as_dict()
        rows = state.setdefault("capability_assignments", {})
        existing = rows.get(assignment.assignment_id)
        if existing is not None:
            if existing != payload:
                raise OpReject("capability assignment identity is already immutable")
            return
        rows[assignment.assignment_id] = payload
    elif kind == "entity_add":
        eid = op.get("entity") or slug(op["name"])
        entity_kind = op.get("kind", "character")
        initially_present = bool(op.get("present", False)) \
            and entity_kind not in ("location", "faction")
        e = state["entities"].setdefault(eid, {"kind": entity_kind,
                                               "name": op["name"], "aliases": [],
                                               "location_id": None,
                                               "present": initially_present})
        for a in op.get("aliases", []):
            if a not in e["aliases"]:
                e["aliases"].append(a)
    elif kind == "set_attribute":
        state["attributes"].setdefault(op["entity"], {})[op["key"]] = op["value"]
    elif kind == "move_entity":
        eid = resolve_entity_ref(state, op["entity"])   # refer to a known entity, never mint one
        if eid is not None:
            state["entities"][eid]["location_id"] = op["to_location"]
    elif kind == "presence":
        eid = resolve_entity_ref(state, op["entity"])   # a place/skill/typo resolves to nothing
        ent = state["entities"].get(eid) if eid is not None else None
        if ent is not None and ent.get("kind") not in ("location", "faction"):
            ent["present"] = bool(op["present"])         # never mint; never stage a place/faction
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
        # Compression item 3 (2026-07-09): a near-restatement SUPERSEDES the older record —
        # the old fact is retired (kept, labeled), never deleted; L10 and the briefing stop
        # reading it. Deterministic (pure token math over the op sequence) so replay holds.
        new_toks = _fact_tokens(op["statement"])
        if new_toks:
            for ofid, f in state["facts"].items():
                if ofid == fid or not isinstance(f, dict) or f.get("retired_turn") is not None:
                    continue
                old_toks = _fact_tokens(f.get("statement", ""))
                if old_toks and _jaccard(new_toks, old_toks) >= 0.75:
                    f["retired_turn"] = turn
                    f["superseded_by"] = fid
        state["facts"].setdefault(fid, {"statement": op["statement"], "established_turn": turn,
                                        "is_secret": bool(op.get("is_secret"))})
        state["beliefs"][f'{op["learner"]}|{fid}'] = {
            "stance": "believes_true", "source": op["source"],
            "teller": op.get("teller"), "acquired_turn": turn}
    elif kind == "fact_retire":                 # compression item 3: retire by fid or by text
        target = None
        if op.get("fact") and op["fact"] in state["facts"]:
            target = op["fact"]
        elif op.get("statement"):
            want = _fact_tokens(op["statement"])
            best = 0.0
            for ofid, f in state["facts"].items():
                if not isinstance(f, dict) or f.get("retired_turn") is not None:
                    continue
                sim = _jaccard(want, _fact_tokens(f.get("statement", "")))
                if sim > best and sim >= 0.5:   # user-directed: looser match, still one target
                    target, best = ofid, sim
        if target is not None:
            state["facts"][target]["status"] = "retired"
            state["facts"][target]["retired_turn"] = turn
    elif kind == "memory_event":
        tags = list(op.get("tags", []))
        mode = state.get("scene", {}).get("mode")
        if mode in ("flashback", "dream") and mode not in tags:
            tags.append(mode)                              # 08 B4: tag non-live memories
        new_m = _fact_tokens(op["text"])                   # compression item 3 (2026-07-09):
        if not any(m.get("text") == op["text"]             # the 07-07 exact-dupe guard widens
                   or (new_m and _jaccard(new_m, _fact_tokens(m.get("text", ""))) >= 0.85)
                   for m in state["memories"][-20:]):      # to NEAR-dupes — a restated lore
            state["memories"].append({                     # line no longer rides recall twice
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
            for p in (state.get("player") or {}).values():   # skip is rest: only the shipped
                if not isinstance(p, dict):                   # stamina/mana lifecycle fires
                    continue
                for rname, r in (p.get("resources") or {}).items():
                    rid = _player_resource_id(rname)
                    if rid in _AUTOMATIC_PLAYER_RESOURCES \
                            and isinstance(r, dict) and r.get("max"):
                        full = rid == "stamina"
                        gain = int(r["max"]) if full else max(1, int(r["max"]) // 2)
                        r["cur"] = _clamp(int(r.get("cur", 0)) + gain, 0, int(r["max"]))
        if op.get("_turn_mark"):                           # Phase 2 (rpg-baked only): the
            state["clock"]["last_advance_turn"] = turn     # idle auto-tick counts from here
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
                for p in (state.get("player") or {}).values():   # RPG-5: a scene change
                    pools = (p.get("resources") or {}) if isinstance(p, dict) else {}
                    for rname, r in pools.items():                # catches only the shipped
                        if _player_resource_id(rname) in _AUTOMATIC_PLAYER_RESOURCES \
                                and isinstance(r, dict) and r.get("max"):   # pools' breath
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
            if op.get("_prev_loc") and boundary:             # Phase 2: travel is committed
                sc["last_move"] = {"from": op["_prev_loc"],  # truth (rpg-baked ops only; a
                                   "to": op["location"], "turn": turn}   # none op has no key)
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
                               "shape": op.get("_shape"),    # 2026-07-07: dice-shaping audit
                               # Legacy saved-roll provenance; no current narrator path sets it.
                               "dm_called": bool(op.get("_dm_called")),
                               "target": op.get("_target"),  # Phase 1: strike audit for the
                               "dmg": op.get("_dmg")})       # HUD/directive (None off-combat)
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
    elif kind == "resource_change":             # trusted generic non-HP pool lifecycle
        pl = state.get("player", {}).get(op["char"])
        if not isinstance(pl, dict):
            raise OpReject("no Player Card for resource change")
        rid = str(op.get("resource") or "")
        if rid == "hp" or _player_resource_id(rid) != rid:
            raise OpReject("resource change requires an exact declared non-HP resource id")
        pool = (pl.get("resources") or {}).get(rid)
        if not isinstance(pool, dict):
            raise OpReject(f"undeclared Player resource '{rid}'")
        mx = _int_or(pool.get("max"), -1)
        cur = _int_or(pool.get("cur"), 0)
        if mx < 0 or not 0 <= cur <= mx:
            raise OpReject(f"malformed Player resource '{rid}'")
        amount = int(op["amount"])
        action = str(op["action"])
        if action == "spend":
            if amount > cur:
                raise OpReject(
                    f"not enough {rid}: {cur}/{amount} available for code-owned spend")
            pool["cur"] = cur - amount
        elif action == "gain":
            pool["cur"] = min(mx, cur + amount)
        elif action == "set":
            if amount > mx:
                raise OpReject(f"cannot set {rid} to {amount}: declared maximum is {mx}")
            pool["cur"] = amount
        else:                                      # replay defense; live validation rejects first
            raise OpReject(f"unknown resource action '{action}'")
    elif kind == "item_mint":                   # RPG-2 (doc 07 §7.2): instance from template
        iid, snap = op.get("_iid"), op.get("_snapshot") or {}
        if not iid or not snap:                 # template unknown at _enrich -> visible reject
            raise OpReject(f"unknown item template '{op.get('template')}' "
                           f"(add it to registry items.toml — nothing freestyle)")
        owner = op["owner"]
        items = state.setdefault("items", {})
        loc = str(op.get("to") or _auto_loc(state, owner, snap))   # worn gear auto-equips
        rec = {"template_id": op["template"], "name": snap.get("name", op["template"]),
               "qty": max(1, int(op.get("qty", 1))), "loc": loc, "owner": owner,
               "mods_snapshot": dict(snap.get("mods") or {}), "minted_turn": turn}
        for k in ("slot", "covers", "on_consume", "stackable", "max_stack", "capacity",
                  "is_container", "worn", "type", "class"):
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
            if new.split(":", 1)[0] == "world":
                _stamp_world_item_origin(state, it)
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
            if dest.split(":", 1)[0] == "world":
                _stamp_world_item_origin(state, it)
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
        if op.get("_world_pickup_checked") and not op.get("_world_pickup_here"):
            raise OpReject("world item is not in the current scene; remote pickup rejected")
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
        eid = resolve_entity_ref(state, op["char"]) or op["char"]   # canonical cast row (or open-vocab)
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
        eid = resolve_entity_ref(state, op["char"]) or op["char"]
        effs = state.get("effects", {}).get(eid) or {}
        fid = _resolve_effect_key(effs, op["effect"])
        if fid is None:
            raise OpReject(f"'{op['effect']}' is not affecting {eid} — nothing to remove "
                           f"(the ledger, not the prose, is what's true)")
        del effs[fid]
    elif kind == "effect_update":               # RPG-3: the dynamic-valence channel
        eid = resolve_entity_ref(state, op["char"]) or op["char"]
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
        led = entry["ledger"]
        last = led[-1] if led else None
        reason = str(op.get("reason") or "")[:120]
        if (reason and last and last.get("turn") == turn and last.get("delta") == d
                and str(last.get("reason") or "") == reason):
            return                          # same-turn identical REASONED proposal (the DM tag
        #                                     and the extraction ladder both reported the same
        #                                     fact) -> count once; blank-reason repeats stay
        led.append({"turn": turn, "delta": d,
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
        if op.get("_world_item_gain_conflict"):
            raise OpReject(
                "item gain overlaps existing world custody; bind and transfer one exact instance"
            )
        owner = op["char"]                                  # Re-tagged acquisitions STACK on the
        name, _embed = _split_item_qty(op["name"])          # existing row — never a dupe (the
        items = state.setdefault("items", {})               # AI-Roguelite failure mode). Counts
        low = name.lower()                                  # ride qty, never the name.
        gain_qty = max(1, int(op["qty"]) if op.get("qty") is not None else (_embed or 1))
        hit = next((i for i, it0 in items.items()
                    if (it0 or {}).get("owner") == owner and (it0 or {}).get("loc") != "gone"
                    and str((it0 or {}).get("name", "")).lower() == low), None)
        if hit:
            if items[hit].get("_gain_turn") != turn:        # same-turn dupe (tag + extraction both
                items[hit]["qty"] = int(items[hit].get("qty", 1)) + gain_qty   # fired) -> ignore;
                items[hit]["_gain_turn"] = turn             # a LATER turn's re-gain genuinely stacks
        else:
            base = slug(name)[:48]
            iid, n = base, 1
            while iid in items:                 # pure fn of (state, op): replay-deterministic
                n += 1
                iid = f"{base}#{n}"
            snap = op.get("_snapshot") or {}    # template + classification bake (_enrich)
            loc = _auto_loc(state, owner, snap)  # worn gear auto-equips; else carried
            rec = {"template_id": snap.get("_template"), "name": name,
                   "qty": gain_qty, "loc": loc, "owner": owner, "_gain_turn": turn,
                   "mods_snapshot": dict(snap.get("mods") or {}), "minted_turn": turn}
            for k in ("slot", "covers", "on_consume", "stackable", "max_stack",
                      "capacity", "is_container", "worn", "type", "class", "aura"):
                if k in snap:
                    rec[k] = snap[k]
            items[iid] = rec
            if not _index_add(state, owner, iid, loc):
                del items[iid]                  # transactional rollback (doc 07 §7)
                raise OpReject(f"cannot add '{name}': inventory full")
    elif kind == "item_lose":                   # RPG-5 (G2): narrated loss/consume, ledger-checked
        owner = op["char"]
        name, _le = _split_item_qty(op["name"])
        lose_qty = max(1, int(op["qty"]) if op.get("qty") is not None else (_le or 1))
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
        if q > lose_qty:
            it["qty"] = q - lose_qty
        else:                                   # last one(s) used up -> remove from the ledger
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
            note = op.get("note") or op.get("detail")
            if note:
                q["note"] = str(note)[:8000]
        else:
            q = {"name": str(op["name"]).strip()[:120], "status": "active",
                 "created_turn": turn, "updated_turn": turn}
            note = op.get("note") or op.get("detail")
            if note:
                q["note"] = str(note)[:8000]
            if op.get("giver"):
                q["giver"] = str(op["giver"])[:300]
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
            q["note"] = str(op["note"])[:8000]
        q["updated_turn"] = turn
    elif kind == "hp_adj":                      # RPG-5 (G7): bounded consequence channel —
        pl = state.get("player", {}).get(op["char"])   # the narrator proposes severity, the
        if not isinstance(pl, dict) or not isinstance(pl.get("hp"), dict) \
                or not pl["hp"].get("max"):     # clamp (baked) owns the number
            raise OpReject(f"no HP pool for '{op['char']}' — hp_adj tracks the Player Card")
        hp = pl["hp"]
        d = int(op.get("_delta", op["delta"]))
        last = pl.get("_hp_adj_last") or {}
        toks = _fact_tokens(str(op.get("reason") or ""))
        prev = frozenset(last.get("toks") or ())
        if not op.get("_effect_id") and last.get("turn") == turn \
                and last.get("delta") == d and toks and prev:
            # ONE wound reported twice — the [hp] tag (hot path, verbatim) AND the cold-path
            # ladder's paraphrase both land it. Jaccard alone misses when the paraphrase
            # COMPRESSES the reason (2026-07-09 Cinderveil: a -2 graze landed as -4; 2026-07-10
            # Hollowmere live: "…grazes his ribs below the plate" vs "pike-butt graze below
            # breastplate" → J=0.30, the -1 applied twice → -2). Containment (overlap / the
            # shorter reason) is paraphrase-robust; genuinely distinct same-magnitude wounds
            # share far fewer nouns (measured <=0.5 across side/arm/leg/shoulder controls), so
            # 0.6 dedups the re-report without eating a real second wound.
            contain = len(toks & prev) / min(len(toks), len(prev))
            if _jaccard(toks, prev) >= 0.5 or contain >= 0.6:
                return                          # same-turn re-report of one wound: count once
        pl["_hp_adj_last"] = {"turn": turn, "delta": d, "toks": sorted(toks)}
        if isinstance(op.get("_opposition"), dict):
            pl["_opposition_last"] = {**op["_opposition"],
                                      "effect_id": op.get("_effect_id"), "turn": turn,
                                      "delta": d, "hp_cur": _clamp(
                                          int(hp.get("cur", hp["max"])) + d,
                                          0, int(hp["max"])), "hp_max": hp["max"]}
            cb = state.get("combat") or {}
            pending = cb.get("pending_intent") if isinstance(cb, dict) else None
            if isinstance(pending, dict) and pending.get("id") == op["_opposition"].get("intent_id"):
                cb["pending_intent"] = None
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
        for rname, r in (pl.get("resources") or {}).items():
            if _player_resource_id(rname) in _AUTOMATIC_PLAYER_RESOURCES \
                    and isinstance(r, dict):
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
                        _stamp_world_item_origin(state, it)
        eff = op.get("_effect") or {}           # baked condition (Battered / Dead)
        if eff.get("id"):
            effs = state.setdefault("effects", {}).setdefault(op["char"], {})
            effs[eff["id"]] = {**eff, "gained_turn": turn}
    elif kind == "combatant_spawn":             # Phase 1: a snapshot-frozen combat instance
        from .world_events import future_subject_eligible

        if not future_subject_eligible(state, {
            "kind": "enemy" if op.get("side") == "enemy" else "ally",
            "id": str(op.get("_cid") or op.get("char") or slug(str(op.get("name") or ""))),
            "faction": op.get("faction"),
            "name": op.get("name"),
            "tier": op.get("tier", "standard"),
            "role": op.get("role"),
            "location": op.get("location"),
            "tags": op.get("tags"),
        }, game_time=turn):
            raise OpReject("an active WorldOverlay makes this future spawn ineligible")
        cb = _combat(state)
        rows = cb["combatants"]
        cohort_state = None
        if op.get("cohort_ref") is not None:
            battle = state.get("battle") or {}
            cohort_state = battle.get("cohort")
            cohort_spec = _validated_battle_cohort(cohort_state)
            index = op.get("cohort_index")
            cohort_row = cohort_state if isinstance(cohort_state, dict) else {}
            expected = int(cohort_row.get("spawned", 0)) + 1
            if not battle.get("active") or cohort_spec is None \
                    or op.get("cohort_ref") != cohort_spec["id"]:
                raise OpReject("cohort spawn does not belong to the active battle")
            if index != expected or index > cohort_spec["total"] \
                    or int(cohort_row.get("remaining", 0)) <= 0:
                raise OpReject("cohort spawn is not the exact next queued actor")
            if str(op.get("name") or "").strip() != cohort_spec["name"] \
                    or (op.get("tier") or "standard") != cohort_spec["tier"] \
                    or str(op.get("armament") or "").strip() != cohort_spec["armament"]:
                raise OpReject("cohort spawn identity, tier, or armament drifted from its plan")
        eid = op.get("char")                    # tracked: references a REAL entity row
        if eid and any(isinstance(r, dict) and r.get("eid") == eid
                       and not r.get("defeated") for r in rows.values()):
            raise OpReject(f"{op['name']} is already on the field")
        side = op["side"]
        cap = COMBAT_SIDE_CAP - 1 if side == "ally" else COMBAT_SIDE_CAP   # player holds an
        if len(live_combatants(state, side)) >= cap:                       # ally slot (3v3)
            raise OpReject(f"the {side} side is full — 3v3 is the cap (plan doc 13)")
        cid = op.get("_cid") or slug(op["name"])[:32]
        if cid in rows:
            n = 2
            while f"{cid}#{n}" in rows:
                n += 1
            cid = f"{cid}#{n}"
        capability_pool, capability_assignments = _validated_spawn_capability_pool(
            state, op, cid, turn
        )
        hp = dict(op.get("_hp") or {"cur": THREAT_HP["standard"], "max": THREAT_HP["standard"]})
        rows[cid] = {"id": cid, "name": str(op["name"]).strip()[:60], "side": side,
                     "kind": "tracked" if eid else "extra", "eid": eid,
                     "tier": op.get("tier") or "standard", "hp": hp,
                     "armament": str(op.get("armament") or "")[:60],
                     "mod": int(op.get("_mod", 0)), "loot": list(op.get("_loot") or []),
                     "init": int(op.get("_init", THREAT_MOD.get(op.get("tier") or "standard", 1)
                                            * 10)),   # baked turn-order score (2026-07-10)
                      "defeated": False, "spawned_turn": turn, "dropped": []}
        if op.get("faction"):
            rows[cid]["faction"] = op["faction"]
        if cohort_state is not None:
            total = int(cohort_state["total"])
            index = int(op["cohort_index"])
            rows[cid]["cohort"] = {"ref": str(cohort_state["id"]),
                                    "index": index, "total": total}
            cohort_state["spawned"] = index
            cohort_state["remaining"] = total - index
        if isinstance(op.get("_kit"), dict):
            rows[cid]["kit"] = dict(op["_kit"])
            if op.get("_kit_source"):
                rows[cid]["kit_source"] = op["_kit_source"]
        if capability_pool is not None:
            rows[cid]["capability_pool"] = capability_pool
            rows[cid]["capability_subject"] = capability_pool["pools"]["assigned"]["subject"]
            rows[cid]["capability_assignment_ids"] = [
                item["assignment_id"] for item in capability_assignments
            ]
            assignment_rows = state.setdefault("capability_assignments", {})
            for item in capability_assignments:
                assignment_rows[item["assignment_id"]] = item
        if not cb["active"]:
            cb["active"] = True
            cb["started_turn"] = turn
        initial = op.get("_initial_intent")
        if side == "enemy" and not isinstance(cb.get("pending_intent"), dict) \
                and isinstance(initial, dict) \
                and _replay_intent_v1_ok(initial, rows[cid]):
            cb["pending_intent"] = dict(initial)
    elif kind == "enemy_intent_set":
        cb = _combat(state)
        intent = op.get("_intent")
        rows = cb.get("combatants") or {}
        row = rows.get(str((intent or {}).get("actor"))) if isinstance(intent, dict) else None
        baked_kit = op.get("_kit")
        candidate = ({**row, "kit": dict(baked_kit)}
                     if isinstance(row, dict) and isinstance(baked_kit, dict) else row)
        if not isinstance(row, dict) or row.get("defeated") or row.get("side") != "enemy" \
                or int((row.get("hp") or {}).get("cur", 0)) <= 0 \
                or not _replay_intent_v1_ok(intent, candidate):
            raise OpReject("enemy intent must name a live enemy and one move from its frozen kit")
        if isinstance(baked_kit, dict):
            row["kit"] = dict(baked_kit)
        cb["pending_intent"] = dict(intent)
    elif kind == "combatant_hp":                # the clamped harm channel for combatant rows
        cid = resolve_combatant(state, op["target"])
        if cid is None:
            raise OpReject(f"'{op['target']}' is not a live combatant — the War Room "
                           f"ledger, not the prose, is what's true")
        row = state["combat"]["combatants"][cid]
        # fix B (2026-07-10): the Player's code-decided strike (_strike) is authoritative and
        # already applied; the DM re-narrating that same blow as a same-turn [hp | <foe>] tag is
        # a DOUBLE (the contract says the strike toll is pre-applied — don't re-tag it). Drop a
        # NON-strike harm on a foe the player struck THIS turn; ally/DM harm on OTHER foes (or a
        # foe the player didn't strike) is unaffected, so multi-foe chip damage still lands.
        if not op.get("_effect_id") and not op.get("_strike") \
                and int(row.get("_struck_turn", -10**9)) == turn:
            return
        d = int(op.get("_delta", op["delta"]))
        row["hp"]["cur"] = _clamp(int(row["hp"].get("cur", row["hp"]["max"])) + d,
                                  0, int(row["hp"]["max"]))
        if op.get("_strike"):
            row["_struck_turn"] = turn
    elif kind == "combatant_defeat":            # code-detected: HP 0 -> defeat + frozen loot
        rows = (state.get("combat") or {}).get("combatants") or {}
        cid = op["target"] if op["target"] in rows else resolve_combatant(state, op["target"])
        row = rows.get(cid) if cid else None
        if not isinstance(row, dict):
            raise OpReject(f"'{op['target']}' is not on the field — nothing to defeat")
        row["defeated"], row["defeated_turn"] = True, turn
        row["hp"]["cur"] = 0
        pending = (state.get("combat") or {}).get("pending_intent")
        if isinstance(pending, dict) and pending.get("actor") == cid:
            state["combat"]["pending_intent"] = None
        items = state.setdefault("items", {})
        for drop in (op.get("_loot_drop") or []):   # baked at _enrich: replay never re-rolls
            iid = str(drop.get("_iid") or slug(str(drop.get("name", "loot")))[:48])
            n = 1
            while iid in items:
                n += 1
                iid = f"{str(drop.get('_iid') or slug(str(drop.get('name', 'loot')))[:48])}#{n}"
            items[iid] = {"template_id": None, "name": str(drop.get("name", "loot"))[:80],
                          "qty": max(1, int(drop.get("qty", 1))), "loc": "world",
                          "owner": None, "mods_snapshot": {}, "minted_turn": turn,
                          "class": "inv", "dropped_by": cid,
                          "dropped_by_name": str(row.get("name") or cid)[:80]}
            _stamp_world_item_origin(state, items[iid])
            # A reply pickup resolves to this exact field instance; it never mints a substitute.
            row["dropped"].append(items[iid]["name"])
    elif kind == "combat_end":                  # extras evaporate; tracked wounds PERSIST
        cb = state.get("combat")
        if not isinstance(cb, dict) or not cb.get("active"):
            raise OpReject("no active combat to end")
        rows = cb.get("combatants") or {}
        defeated, survivors, loot = [], [], []
        for row in rows.values():
            if not isinstance(row, dict):
                continue
            (defeated if row.get("defeated") else survivors).append(str(row.get("name", "?")))
            loot += list(row.get("dropped") or [])
            eid = row.get("eid")
            if eid and eid in state.get("entities", {}):   # FULL wound persistence (ratified):
                hp = row.get("hp") or {}                   # the fight's toll lands on the row
                state.setdefault("attributes", {}).setdefault(eid, {})["hp"] = {
                    "cur": int(hp.get("cur", 0)), "max": int(hp.get("max", 1))}
                if row.get("defeated"):
                    effs = state.setdefault("effects", {}).setdefault(eid, {})
                    effs["battered"] = {"id": "battered", "name": "Battered",
                                        "kind": "condition", "valence": "negative",
                                        "duration": 6, "mods": {"all": -1}, "preset": True,
                                        "gained_turn": turn}
                elif int(hp.get("cur", 1)) * 2 < int(hp.get("max", 1)):
                    effs = state.setdefault("effects", {}).setdefault(eid, {})
                    effs.setdefault("wounded", {"id": "wounded", "name": "Wounded",
                                                "kind": "condition", "valence": "negative",
                                                "mods": {"all": -1}, "preset": True,
                                                "gained_turn": turn})   # heals in-world (routed)
        cb["history"].append({"turn": turn, "started_turn": cb.get("started_turn"),
                              "defeated": defeated, "survivors": survivors, "loot": loot,
                              "outcome": str(op.get("outcome") or "resolved")})
        del cb["history"][:-COMBAT_HISTORY_CAP]
        cb["active"], cb["started_turn"], cb["combatants"] = False, None, {}
        cb["pending_intent"] = None
    elif kind == "clash_record":                # NPC-vs-NPC: prose fight, outcome RECORDED
        a, b = op["a"], op["b"]
        rec = {"a": a, "b": b, "method": str(op.get("method") or "")[:120],
               "outcome": str(op.get("outcome") or "")[:160], "turn": turn}
        cl = state.setdefault("clashes", [])
        if not any(c.get("a") == a and c.get("b") == b and c.get("turn") == turn
                   for c in cl):                # same-turn dupe (tag + ladder both saw it)
            cl.append(rec)
            del cl[:-CLASH_CAP]
            na = state.get("entities", {}).get(a, {}).get("name", a)
            nb = state.get("entities", {}).get(b, {}).get("name", b)
            stmt = f"{na} clashed with {nb}" \
                + (f" ({rec['method']})" if rec["method"] else "") \
                + (f" — {rec['outcome']}" if rec["outcome"] else "")
            fid = hashlib.blake2b(stmt.encode(), digest_size=6).hexdigest()
            state["facts"].setdefault(fid, {"statement": stmt, "established_turn": turn})
    elif kind == "loot_table":                  # Creator/assist-authored rows, FROZEN here
        entries = []
        for e in op["entries"][:12]:
            if not isinstance(e, dict) or not str(e.get("name", "")).strip():
                continue
            entries.append({"name": str(e["name"]).strip()[:80],
                            "qty_min": max(1, int(e.get("qty_min", e.get("qty", 1)) or 1)),
                            "qty_max": max(1, int(e.get("qty_max", e.get("qty", 1)) or 1)),
                            "chance": min(1.0, max(0.0, float(e.get("chance", 1.0))))})
        if entries:
            state.setdefault("loot", {})[op["tier"]] = entries
    elif kind == "battle_start":                # §F: open (or re-open) the large-scale battle
        b = _battle(state)
        if not b.get("active"):                 # seed-once while live; a settled battle re-opens
            b.update({"active": True, "name": str(op["name"]).strip()[:80] or "the battle",
                      "momentum": _clamp(int(op.get("momentum", 0)), *BATTLE_MOMENTUM_CLAMP),
                      "waves": 0,
                      "threat": op["threat"] if op.get("threat") in THREAT_TIERS else "standard",
                      "foe": (str(op.get("foe") or "").strip()[:60] or "reinforcements"),
                       "wave_size": _clamp(int(op.get("wave_size", BATTLE_WAVE_DEFAULT)),
                                           1, COMBAT_SIDE_CAP),
                       "started_turn": turn, "log": [], "outcome": None})
            b.pop("cohort", None)               # a later ordinary battle never inherits old queues
            cohort = _validated_battle_cohort(op.get("cohort"))
            if cohort is not None:
                b["cohort"] = {**cohort, "spawned": 0, "remaining": cohort["total"]}
                ingress = op.get("_semantic_ingress")
                declaration = op.get("_semantic_declaration")
                if isinstance(ingress, dict) and isinstance(declaration, dict):
                    # Replay-safe audit evidence.  It was live-validated before journaling; this
                    # serialized copy is never treated as fresh authority by ``apply_delta``.
                    b["cohort"]["ingress"] = deepcopy(ingress)
                    b["cohort"]["declaration"] = deepcopy(declaration)
                    b["cohort"]["initial_count"] = max(
                        0,
                        min(
                            cohort["total"],
                            int(op.get("_cohort_initial_count", 0)),
                        ),
                    )
    elif kind == "tide_set":                    # §F: the DM's macro report -> one clamped step
        b = state.get("battle")
        if not (isinstance(b, dict) and b.get("active")):
            raise OpReject("no active battle — there is no tide to turn")
        b["momentum"] = _clamp(int(b.get("momentum", 0)) + int(op.get("_delta", 0)),
                               *BATTLE_MOMENTUM_CLAMP)
        b["log"] = (b.get("log") or [])[-19:] + [[turn, "tide", str(op.get("why", ""))[:80]]]
    elif kind == "battle_wave":                 # §F: the referee sent a fresh wave (record + nudge)
        b = state.get("battle")
        if not (isinstance(b, dict) and b.get("active")):
            raise OpReject("no active battle for a wave")
        b["waves"] = int(b.get("waves", 0)) + 1
        b["momentum"] = _clamp(int(b.get("momentum", 0)) + int(op.get("_delta", 1)),
                               *BATTLE_MOMENTUM_CLAMP)
        b["log"] = (b.get("log") or [])[-19:] + [[turn, "wave", int(b["waves"])]]
    elif kind == "battle_end":                  # §F: the macro battle settles
        b = state.get("battle")
        if not (isinstance(b, dict) and b.get("active")):
            raise OpReject("no active battle to end")
        b["active"], b["outcome"], b["ended_turn"] = \
            False, str(op.get("outcome") or "resolved"), turn
        b["log"] = (b.get("log") or [])[-19:] + [[turn, "end", b["outcome"]]]
    elif kind == "front_add":                   # Phase 2: an authored clock, frozen at creation
        fid = str(op.get("_fid") or slug(str(op.get("name", "")))[:64])
        fronts = state.setdefault("fronts", {})
        if fid and fid not in fronts:           # seed-once: re-seeding never resets progress
            fronts[fid] = {"name": str(op["name"]).strip()[:80],
                           "faction": slug(str(op["faction"]))[:64] if op.get("faction") else None,
                           "segments": int(op.get("_segments", op.get("segments", 6))),
                           "filled": 0, "pace": int(op.get("_pace", 1)),
                           "consequence": str(op.get("consequence", "")).strip()[:4000],
                           "revealed": False, "done": False,
                            "created_turn": turn, "log": []}
            if op.get("_event_duration_turns") is not None:
                fronts[fid]["event_duration_turns"] = int(op["_event_duration_turns"])
            if isinstance(op.get("spawn_eligibility"), bool):
                fronts[fid]["spawn_eligibility"] = op["spawn_eligibility"]
    elif kind == "front_tick":
        rec = (state.get("fronts") or {}).get(str(op.get("front", "")))
        if isinstance(rec, dict) and not rec.get("done"):
            segs = max(1, int(rec.get("segments", 6)))
            rec["filled"] = min(segs, int(rec.get("filled", 0)) + int(op.get("_delta", 1)))
            rec["log"] = (rec.get("log") or [])[-19:] + [[turn, str(op.get("reason", ""))[:80]]]
            if rec["filled"] >= segs:           # the clock strikes: consequence is world truth
                rec["done"] = True
                rec["revealed"] = True          # a consequence on-screen is its own rumor
                rec["filled_turn"] = turn
    elif kind == "front_reveal":
        rec = (state.get("fronts") or {}).get(str(op.get("front", "")))
        if isinstance(rec, dict) and not rec.get("revealed"):
            rec["revealed"] = True
            rec["revealed_turn"] = turn
    elif kind == "route_set":                   # travel-time edge (undirected; slugged at bake)
        a, b = str(op.get("a", "")), str(op.get("b", ""))
        if a and b and a != b:
            state.setdefault("routes", {})["|".join(sorted((a, b)))] = \
                int(op.get("_segments", op.get("segments", 1)))
    elif kind == "stagnation":
        state["scene"]["stagnation"] = round(float(op["value"]), 3)
    elif kind == "player_seed":
        _apply_player_seed(state, op)
    if turn > state["meta"].get("turn", -1):
        state["meta"]["turn"] = turn


def _custom_zones(state: dict, eid: str) -> set:
    z = state.get("attributes", {}).get(eid, {}).get("zones.custom", [])
    return set(z) if isinstance(z, list) else set()


_ITEM_QTY_TAIL = re.compile(
    r"[\s,;:.\u2013-]*[(\[]?\s*"
    r"(?:x\s*(\d+)|(\d+)\s*x|(\d+)\s*"
    r"(?:doses?|vials?|charges?|units?|rounds?|shots?|uses?|pieces?|pcs|servings?|"
    r"portions?|packs?|bundles?|sticks?|count|ct)\b)"
    r"\s*[)\]]?\s*$", re.IGNORECASE)


# leading DIGIT multiplier at the head of a name: "5x Health Potion Vials", "3 x Torches",
# "x5 Bandages" -> 5/3/5 (Bean 2026-07-11: prefix counts; the tail regex only caught suffixes).
# The explicit x/× is required so a name that merely STARTS with a number ("7 League Boots",
# "9mm Rounds") is never mis-split into a count.
_ITEM_QTY_HEAD = re.compile(r"^\s*(?:(\d+)\s*[x×]|[x×]\s*(\d+))\s+", re.IGNORECASE)
_ITEM_WORD_COUNTS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                     "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
                     "dozen": 12, "pair": 2, "couple": 2, "brace": 2}
_ITEM_FLAG_RE = re.compile(r"\s*[(\[]\s*(worn|equipped|carried|stowed)\s*[)\]]", re.IGNORECASE)


def _worn_flag(name):
    """An explicit '(worn)'/'(equipped)' or '(carried)'/'(stowed)' tag in a free-form item
    name is an author signal -- it wins over the name heuristic (creator gear field, DM item
    tags). Returns True / False / None. Pure fn of the name -> replay-safe."""
    m = _ITEM_FLAG_RE.search(str(name or ""))
    if not m:
        return None
    return m.group(1).lower() in ("worn", "equipped")


def _singular(word: str) -> str:
    """Best-effort de-plural for a counted head noun ('Coins'->'Coin', 'boxes'->'box').
    Possessives, 'ss' words and short words are left alone -- a floor, never clever."""
    w = str(word or "")
    if len(w) >= 5 and w.lower().endswith(("ches", "shes")):
        return w[:-2]                            # torches->torch, dishes->dish (-es after ch/sh)
    if len(w) >= 4 and w.lower().endswith("es") and w[-3].lower() in "sxz":
        return w[:-2]
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss") and not w.endswith("'s"):
        return w[:-1]
    return w


def _split_item_qty(name):
    """Pull a count out of an item name into a quantity, so the ledger uses xN instead of
    baking multiplicity into the name: a trailing DIGIT count ('Verdan Sap Vial (30 doses)'
    -> x30, 'Health Potion x3' -> x3) AND a leading NUMBER-WORD ("two spent King's Coins on
    a cord" -> "spent King's Coin on a cord" x2 -- the counted noun is singularized so later
    gains/loses match by name). '(worn)'/'(carried)' author tags are stripped here (they ride
    classification, not the name). A non-count paren ('(parchment)') is left alone.
    Returns (clean_name, qty|None). Deterministic -> replay-pure."""
    raw = re.sub(r"\s{2,}", " ", _ITEM_FLAG_RE.sub(" ", str(name or ""))).strip()
    m = _ITEM_QTY_TAIL.search(raw)
    if m:
        q = next((int(g) for g in m.groups() if g), None)
        clean = raw[:m.start()].strip(" ,;:.\u2013-([")
        return (clean or raw), q
    mh = _ITEM_QTY_HEAD.match(raw)              # leading digit multiplier: "5x Health Potion
    if mh:                                      # Vials" -> ("Health Potion Vial", 5)
        q = next((int(g) for g in mh.groups() if g), None)
        rest = raw[mh.end():].split()
        if rest and q:
            cut = len(rest)                     # singularize the counted head noun, mirroring
            for i, w in enumerate(rest):        # the number-word branch below
                if i > 0 and w.lower() in ("on", "of", "in", "with", "from", "for"):
                    cut = i
                    break
            if cut > 0:
                rest = rest[:cut - 1] + [_singular(rest[cut - 1])] + rest[cut:]
            clean = " ".join(rest).strip()
            if clean:
                return clean, q
    toks = raw.split()
    lead = toks[0].lower() if toks else ""
    if lead in _ITEM_WORD_COUNTS and len(toks) >= 2:
        q = _ITEM_WORD_COUNTS[lead]
        rest = toks[1:]
        if rest and rest[0].lower() == "of" and len(rest) >= 2:   # "pair of boots"
            rest = rest[1:]
        cut = len(rest)                    # singularize the counted head noun: the last word
        for i, w in enumerate(rest):       # before a prepositional tail ("Coins" in
            if i > 0 and w.lower() in ("on", "of", "in", "with", "from", "for"):
                cut = i                    # "spent King's Coins on a cord")
                break
        if cut > 0:
            rest = rest[:cut - 1] + [_singular(rest[cut - 1])] + rest[cut:]
        clean = " ".join(rest).strip(" ,;:.\u2013-([")
        if clean:
            return clean, q
    return raw, None


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
            out.append(_inherit_semantic_frame_ref(
                {"op": "affinity_adj", "target": fid, "delta": step,
                 "kind": "faction",
                 "reason": f"standing with {ents.get(tgt, {}).get('name', tgt)}"},
                [op],
            ))
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
    current_awarded_xp = 0
    xp_causes: list[dict] = []
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
            out.append(_inherit_semantic_frame_ref(
                {"op": "award_exp", "char": peid, "amount": amt,
                 "reason": f"quest complete: {(q or {}).get('name', tok)}"},
                [op],
            ))
            xp_add += amt
            xp_causes.append(op)
        elif k == "goal" and op.get("action") == "complete":
            out.append(_inherit_semantic_frame_ref(
                {"op": "award_exp", "char": peid, "amount": XP_AWARDS["goal"],
                 "reason": f"completed: {str(op.get('text', ''))[:60]}"},
                [op],
            ))
            xp_add += XP_AWARDS["goal"]
            xp_causes.append(op)
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
                    out.append(_inherit_semantic_frame_ref(
                        {"op": "award_exp", "char": peid,
                         "amount": XP_AWARDS["faction_tier"],
                         "reason": f"standing risen to {affinity_tier(after)}"},
                        [op],
                    ))
                    xp_add += XP_AWARDS["faction_tier"]
                    xp_causes.append(op)
        elif k == "award_exp" and op.get("char") == peid:
            try:
                amount = int(op.get("amount", 0))
                if amount > 0:
                    xp_causes.append(op)
                    current_awarded_xp += amount
            except (TypeError, ValueError):
                pass
    # level-ups from the projected total (may cross several thresholds at once)
    want = xp_level(int(pl.get("xp", 0)) + xp_add)
    have = int(pl.get("level", 1))
    # `pl.xp` already includes award_exp operations from this batch. Remove them to distinguish
    # a genuinely current threshold crossing from an old under-levelled-state reconciliation.
    baseline_want = xp_level(max(0, int(pl.get("xp", 0)) - current_awarded_xp))
    for level in range(have + 1, want + 1):
        out.append(_inherit_semantic_frame_ref(
            {"op": "level_up", "char": peid}, xp_causes if level > baseline_want else [],
        ))
    # defeat: HP floored at 0 and not already resolved this turn (death is final)
    hp = pl.get("hp") or {}
    defeated = pl.get("defeated") or {}
    if isinstance(hp, dict) and int(hp.get("max", 0)) > 0 and int(hp.get("cur", 1)) <= 0 \
            and str(defeated.get("outcome", "")) != "death" \
            and int(defeated.get("turn", -10**9)) != turn:
        hp_causes = [
            op for op in (applied or [])
            if isinstance(op, dict) and op.get("op") == "hp_adj"
            and op.get("char") == peid
            and _int_or(op.get("_delta", op.get("delta", 0)), 0) != 0
        ]
        out.append(_inherit_semantic_frame_ref(
            {"op": "defeat_resolve", "char": peid,
             "outcome": "death" if hardcore else _defeat_outcome(state, peid)},
            hp_causes,
        ))
    return out


# ---- Phase 1 combat pass (plan doc 13) — pure, deterministic, journaled ----------------
def combat_gate(state: dict) -> bool:
    """The War Room's floor trigger: the scene PHASE says violence (same vocabulary R8c's
    opposition die uses) or a combat-ish world flag is truthy. A hostile merely being
    present does NOT open the war room — talking to an enemy is still talking."""
    phase = str((state.get("scene") or {}).get("phase", "")).lower()
    if phase in _COMBAT_PHASES:
        return True
    for k, v in (state.get("world") or {}).items():
        if str(k).lower() in ("combat", "battle", "fight", "under_attack") and v \
                and str(v).lower() not in ("no", "false", "0", "none"):
            return True
    return False


def _authored_hostile(eid: str, attrs: dict) -> bool:
    """An explicit authored hostility label is stronger than a generic ally-role heuristic."""
    role = str((attrs.get(eid) or {}).get("role", "")).lower() if isinstance(attrs, dict) else ""
    return bool(set(re.findall(r"[a-z]+", role)) & _AUTHORED_HOSTILE_WORDS)


def _companion_basis(peid: str, eid: str, pl: dict, aff: dict,
                     rels: dict, attrs: dict) -> bool:
    """Is this present NPC grounded as a comrade who'd enlist beside the player? An in-world
    basis, never a bare number (pillars 4/6): a soulmate bond, deep standing (Ally tier), a
    close relationship dim, or an authored companion-class role/label. A current enemy (standing
    at or below the hostile bar) is never an ally, whatever a stray label says."""
    rec = aff.get(f"{peid}->{eid}") if isinstance(aff, dict) else None
    val = int(rec.get("value", 0)) if isinstance(rec, dict) else 0
    if val <= HOSTILE_STANDING or _authored_hostile(eid, attrs):
        return False                                # an enemy is never an ally
    if eid and eid == pl.get("soulmate"):
        return True
    if val >= ALLY_STANDING:                        # deep standing (kept)
        return True
    rel = (rels.get(f"{peid}->{eid}") or {}) if isinstance(rels, dict) else {}
    dims = rel.get("dims") or {}
    if int(dims.get("trust", 0)) >= 40 or int(dims.get("affection", 0)) >= 40 \
            or int(dims.get("desire", 0)) >= 50:    # a genuinely close bond (not a stranger)
        return True
    role = str((attrs.get(eid) or {}).get("role", "")).lower() if isinstance(attrs, dict) else ""
    labels = " ".join(str(x).lower() for x in (rec.get("labels") or [])) \
        if isinstance(rec, dict) else ""
    rlabels = " ".join(str(x).lower() for x in (rel.get("labels") or []))
    toks = set(re.findall(r"[a-z]+", f"{role} {labels} {rlabels}"))   # authored comrade role/label
    return any(w in toks for w in _ALLY_ROLE_WORDS)


def _shares_the_fight(peid: str, eid: str, aff: dict, attrs: dict) -> bool:
    """Common-enemy basis (Bean 2026-07-10): a present NPC who fights the SAME foes as the Player
    and is NOT hostile to them enlists on the Player's side even with no personal bond — the
    caravan escort surviving an ambush. Grounded, not bare presence: a protective/martial role,
    or an allied/shared faction (a neutral bystander with neither is not conscripted). Called
    only when an active enemy threat is already on the field (the shared enemy is the basis)."""
    rec = aff.get(f"{peid}->{eid}") if isinstance(aff, dict) else None
    if isinstance(rec, dict) and int(rec.get("value", 0)) <= HOSTILE_STANDING:
        return False                                # mutually hostile -> never an ally
    if _authored_hostile(eid, attrs):
        return False                                # Creator-authored hostile role is also enmity
    role = str((attrs.get(eid) or {}).get("role", "")).lower() if isinstance(attrs, dict) else ""
    if any(w in set(re.findall(r"[a-z]+", role)) for w in _GUARD_ROLE_WORDS):
        return True                                 # here to fight/protect (escort, guard, merc)
    pfac = (attrs.get(peid) or {}).get("faction") if isinstance(attrs, dict) else None
    nfac = (attrs.get(eid) or {}).get("faction") if isinstance(attrs, dict) else None
    if nfac and pfac and nfac == pfac:
        return True                                 # same faction as the Player
    if nfac:                                        # the Player stands with their faction
        frec = aff.get(f"{peid}->{nfac}") if isinstance(aff, dict) else None
        if isinstance(frec, dict) and int(frec.get("value", 0)) >= ALLY_STANDING:
            return True
    return False


def _is_player_summon(peid: str, eid: str, aff: dict, attrs: dict) -> bool:
    """A Player summon / conjuration / creation fights on the Player's side (Bean 2026-07-10) —
    it exists because the Player called it into being, so it is an ally by construction. Grounded:
    an ownership attribute (summoner/conjurer/creator/owner/…) pointing at the Player, or a
    summon-typed entity that is not hostile to them (its caster stands with the Player)."""
    a = (attrs.get(eid) or {}) if isinstance(attrs, dict) else {}
    for k in _SUMMONER_KEYS:                         # explicit ownership -> definitionally yours
        v = str(a.get(k, "")).strip().lower()
        if v and v in (peid, "player", "you", "self", "{{user}}", "{{char}}"):
            return True
    rec = aff.get(f"{peid}->{eid}") if isinstance(aff, dict) else None
    if isinstance(rec, dict) and int(rec.get("value", 0)) <= HOSTILE_STANDING:
        return False                                 # a hostile 'elemental' is a foe, not a summon
    if _authored_hostile(eid, attrs):
        return False                                 # authored enemy construct/sentry is not yours
    typ = " ".join(str(a.get(k, "")) for k in ("role", "type", "species", "class", "descriptor"))
    return bool(set(re.findall(r"[a-z]+", typ.lower())) & set(_SUMMON_WORDS))


def combat_ops(state: dict, applied: list[dict], *, prepare_intent: bool = True) -> list[dict]:
    """Phase 1 (ratified doc 13): the code-side combat referee. Pure reader over
    (state, this batch's applied ops); returns PRIVILEGED rule ops for the caller to
    apply — propose-then-commit, journaled, never a hidden reducer side-effect.

    Floor (weak-model guarantee): when the scene phase turns to violence, PRESENT tracked
    hostiles become enemy combatants and present friends (Ally-tier standing / soulmate)
    enlist beside the player, no tag required. Defeat is CODE-DETECTED (HP 0) -> curated
    XP by threat tier + the frozen loot roll; combat ends itself when a side falls, the
    player is defeated, or a non-combat phase accompanies a real journaled location departure.
    Phase prose alone cannot dismiss live combatants; a same-location de-escalation needs the
    explicit user/code-owned combat-end channel. Extras arrive only through the DM's [foe] tag /
    OOC (there is no in-world basis for inventing them code-side)."""
    out: list[dict] = []
    peid = _player_eid(state)
    if not peid:
        return out
    cb = state.get("combat") or {}
    active = bool(cb.get("active"))
    ents = state.get("entities") or {}
    aff = state.get("affinity") or {}
    rels = state.get("relationships") or {}
    attrs = state.get("attributes") or {}
    pl = (state.get("player") or {}).get(peid) or {}
    batch = [op for op in (applied or []) if isinstance(op, dict)]
    fresh_enemy_causes = [
        op for op in batch
        if op.get("op") == "combatant_spawn" and op.get("side") == "enemy"
    ]

    def _standing(eid: str) -> int:
        rec = aff.get(f"{peid}->{eid}")
        return int(rec.get("value", 0)) if isinstance(rec, dict) else 0

    present = [eid for eid, e in ents.items()
               if isinstance(e, dict) and e.get("present") and eid != peid
               and e.get("kind") in ("character", "npc")]
    on_field = {r.get("eid") for r in (cb.get("combatants") or {}).values()
                if isinstance(r, dict) and not r.get("defeated") and r.get("eid")}
    gate = combat_gate(state)
    if gate or active:                      # a tagged combat phase OR live foes already on the
        want_foes = 0                       # field is a fight — enlist without needing the phase

        if not active:                          # open the war room: present hostiles enlist
            hostiles = sorted([e for e in present if _standing(e) <= HOSTILE_STANDING],
                              key=lambda e: (_standing(e), e))[:COMBAT_SIDE_CAP]
            for eid in hostiles:
                out.append({"op": "combatant_spawn", "name": ents[eid].get("name", eid),
                            "side": "enemy", "char": eid})
            want_foes = len(hostiles)
        if active or want_foes:                 # allies enlist beside the player (cap 2)
            slots = (COMBAT_SIDE_CAP - 1) - len([1 for r in (cb.get("combatants") or
                                                             {}).values()
                                                 if isinstance(r, dict)
                                                 and not r.get("defeated")
                                                 and r.get("side") == "ally"])
            friends = sorted([e for e in present if e not in on_field
                              and (_companion_basis(peid, e, pl, aff, rels, attrs)
                                   or _shares_the_fight(peid, e, aff, attrs)
                                   or _is_player_summon(peid, e, aff, attrs))],
                             key=lambda e: (-_standing(e), e))
            for eid in friends[:max(0, slots)]:
                out.append({"op": "combatant_spawn", "name": ents[eid].get("name", eid),
                            "side": "ally", "char": eid})
    if not active:
        return out
    # defeat detection: HP floored -> privileged defeat + curated XP (enemy rows only)
    live_enemies = 0
    enemy_clear_causes: list[dict] = []
    for cid, row in (cb.get("combatants") or {}).items():
        if not isinstance(row, dict) or row.get("defeated"):
            continue
        if _int_or((row.get("hp") or {}).get("cur", 1), 0) <= 0:
            hp_causes = [
                op for op in batch
                if op.get("op") == "combatant_hp"
                and resolve_combatant(state, op.get("target")) == cid
                and _int_or(op.get("_delta", op.get("delta", 0)), 0) != 0
            ]
            out.append(_inherit_semantic_frame_ref(
                {"op": "combatant_defeat", "target": cid}, hp_causes,
            ))
            if row.get("side") == "enemy":
                # One victory may clear several enemies. A missing current cause is retained as
                # an explicit non-authoritative sentinel so another enemy's frame cannot be
                # selected for the whole combat ending.
                enemy_clear_causes.extend(hp_causes or [{}])
                out.append(_inherit_semantic_frame_ref(
                    {"op": "award_exp", "char": peid,
                     "amount": THREAT_XP.get(str(row.get("tier")),
                                             THREAT_XP["standard"]),
                     "reason": f"defeated {row.get('name', cid)}"},
                    hp_causes,
                ))
        elif row.get("side") == "enemy":
            live_enemies += 1
    # the fight settles itself: last foe falls, the player falls, or the scene moves on
    player_defeat_causes = [
        op for op in batch
        if op.get("op") == "defeat_resolve" and op.get("char") == peid
    ]
    if not player_defeat_causes:
        player_defeat_causes = [
            op for op in batch
            if op.get("op") == "hp_adj" and op.get("char") == peid
            and _int_or(op.get("_delta", op.get("delta", 0)), 0) != 0
        ]
    player_fell = any(op.get("op") == "defeat_resolve" for op in batch) \
        or _int_or((pl.get("defeated") or {}).get("turn", -10**9), -10**9) \
        == _int_or((state.get("meta") or {}).get("turn", -1), -1) \
        or _int_or((pl.get("hp") or {}).get("cur", 1), 0) <= 0
    # An explicit hostile spawn in this referee batch owns the newly opened fight.  Narrators
    # commonly introduce that foe while the same reply's descriptive scene phase is still
    # ``rising``; treating that phase as a departure would immediately erase the spawn and its
    # first visible intent.  A later non-combat scene transition (with no fresh hostile) still
    # closes an existing fight normally.
    fresh_enemy_spawn = bool(fresh_enemy_causes)
    # A narrator phase label is descriptive, not authority to dismiss live combatants.  Only a
    # journaled location boundary (``_prev_loc`` is baked by _enrich) plus a non-combat phase
    # proves that the scene actually moved on.  This keeps same-location tags such as
    # ``[scene | north_sluice | rising]`` from erasing a live foe and its prepared intent.
    phase_left_causes = [
        op for op in batch
        if op.get("op") == "scene_set"
        and op.get("_prev_loc") and op.get("location")
        and op.get("_prev_loc") != op.get("location")
        and op.get("phase") is not None
        and str(op.get("phase")).lower() not in _COMBAT_PHASES
    ]
    phase_left = not fresh_enemy_spawn and bool(phase_left_causes)
    battle_on = bool((state.get("battle") or {}).get("active"))
    if live_enemies == 0:
        if battle_on and not player_fell and not phase_left:
            pass    # §F: a large battle is running -> battle_ops decides wave vs. win AFTER these
                    # defeats apply (so a fresh wave never collides with the just-cleared foes)
        else:
            out.append(_inherit_semantic_frame_ref(
                {"op": "combat_end", "outcome": "victory"}, enemy_clear_causes,
            ))
    elif player_fell:
        if battle_on:
            out.append(_inherit_semantic_frame_ref(
                {"op": "battle_end", "outcome": "defeat"}, player_defeat_causes,
            ))
        out.append(_inherit_semantic_frame_ref(
            {"op": "combat_end", "outcome": "defeat"}, player_defeat_causes,
        ))
    elif phase_left:
        if battle_on:
            out.append(_inherit_semantic_frame_ref(
                {"op": "battle_end", "outcome": "resolved"}, phase_left_causes,
            ))
        out.append(_inherit_semantic_frame_ref(
            {"op": "combat_end", "outcome": "resolved"}, phase_left_causes,
        ))
    # One combat-level future action is the truthful choice surface.  Select it only when the
    # fight will continue; a defeated/HP-zero actor is never allowed to retain the commitment.
    ending = any(isinstance(op, dict) and op.get("op") == "combat_end" for op in out)
    if prepare_intent and live_enemies > 0 and not player_fell and not phase_left and not ending:
        rows = cb.get("combatants") or {}
        pending = cb.get("pending_intent")
        prow = rows.get(str((pending or {}).get("actor"))) if isinstance(pending, dict) else None
        turn = _int_or((state.get("meta") or {}).get("turn", -1), -1)
        prepared = (_int_or(pending.get("prepared_turn"), -10**9)
                    if isinstance(pending, dict)
                    and not isinstance(pending.get("prepared_turn"), bool) else -10**9)
        pname = str(((state.get("entities") or {}).get(peid) or {}).get("name") or peid)
        valid_pending = isinstance(prow, dict) and prow.get("side") == "enemy" \
            and not prow.get("defeated") \
            and _int_or((prow.get("hp") or {}).get("cur", 0), 0) > 0 \
            and intent_matches_frozen_kit(pending, prow) \
            and pending.get("target") == peid and pending.get("target_name") == pname \
            and prepared in (turn - 1, turn)
        if not valid_pending:
            candidates = [(cid, row) for cid, row in rows.items()
                          if isinstance(row, dict) and row.get("side") == "enemy"
                          and not row.get("defeated")
                          and _int_or((row.get("hp") or {}).get("cur", 0), 0) > 0]
            if candidates:
                last = pl.get("_opposition_last") if isinstance(pl, dict) else None
                last_actor = str(last.get("actor") or "") if isinstance(last, dict) else ""
                ids = [cid for cid, _row in candidates]
                actor = ids[0]
                if last_actor in ids and len(ids) > 1:
                    actor = ids[(ids.index(last_actor) + 1) % len(ids)]
                out.append(_inherit_semantic_frame_ref(
                    {"op": "enemy_intent_set", "actor": actor}, fresh_enemy_causes,
                ))
    return out


def _battle_wave_ops(state: dict) -> list[dict]:
    """§F: the next wave of the large-scale battle presses into the War Room — a small band of
    foes matching the battle's threat + the wave record. Curated size/tier/name (never
    model-typed); the reducer suffixes duplicate cids so a wave of like-named foes coexists."""
    b = state.get("battle") or {}
    size = _clamp(int(b.get("wave_size", BATTLE_WAVE_DEFAULT)), 1, COMBAT_SIDE_CAP)
    cohort = b.get("cohort")
    cohort_spec = _validated_battle_cohort(cohort)
    if cohort_spec is not None:
        spawned = max(0, min(cohort_spec["total"], int(cohort.get("spawned", 0))))
        remaining = max(0, min(cohort_spec["total"] - spawned,
                               int(cohort.get("remaining", cohort_spec["total"] - spawned))))
        live = len(live_combatants(state, "enemy"))
        # A finite count is its own runaway guard.  Fill the ordinary 3-foe War Room slice so
        # the maximum accepted x27 is exactly three opening actors plus eight queued waves.
        size = min(COMBAT_SIDE_CAP, remaining, max(0, COMBAT_SIDE_CAP - live))
        if size <= 0:
            return []
        out = []
        for index in range(spawned + 1, spawned + size + 1):
            spawn = {"op": "combatant_spawn", "name": cohort_spec["name"],
                     "side": "enemy", "tier": cohort_spec["tier"],
                     "cohort_ref": cohort_spec["id"], "cohort_index": index}
            if cohort_spec["armament"]:
                spawn["armament"] = cohort_spec["armament"]
            out.append(spawn)
        out.append({"op": "battle_wave"})
        return out
    tier = b.get("threat") if b.get("threat") in THREAT_TIERS else "standard"
    foe = str(b.get("foe") or "reinforcements").strip() or "reinforcements"
    out: list[dict] = [{"op": "combatant_spawn", "name": foe, "side": "enemy", "tier": tier}
                       for _ in range(size)]
    out.append({"op": "battle_wave"})
    return out


def battle_ops(state: dict, applied: list[dict]) -> list[dict]:
    """§F large-scale-battle referee — runs AFTER combat_ops (so the turn's defeats are already
    applied). Pure reader; returns PRIVILEGED rule ops. When a battle is active and the Player's
    War Room slice has CLEARED (no live enemy rows) but the macro tide is not yet won, the next
    wave presses in (Bean: 'more waves until it gets better'); once the tide is winning (or a
    runaway guard trips), the battle — and the fight — settle. Inert without an active battle."""
    b = state.get("battle") or {}
    if not b.get("active"):
        return []
    cb = state.get("combat") or {}
    cohort = b.get("cohort")
    cohort_spec = _validated_battle_cohort(cohort)
    if not cb.get("active"):
        # A finite plan can safely arm its first queued wave even when another same-batch spawn
        # was rejected.  Ordinary open-ended battles keep the legacy inert behavior.
        return _battle_wave_ops(state) if cohort_spec is not None \
            and int(cohort.get("remaining", 0)) > 0 else []
    live_enemies = [r for r in (cb.get("combatants") or {}).values()
                    if isinstance(r, dict) and not r.get("defeated")
                    and r.get("side") == "enemy"
                    and int((r.get("hp") or {}).get("cur", 1)) > 0]
    if live_enemies:
        return []                                   # the Player's slice is still contested — wait
    batch = [op for op in (applied or []) if isinstance(op, dict)]
    rows = cb.get("combatants") or {}
    defeat_causes = [
        op for op in batch
        if op.get("op") == "combatant_defeat"
        and isinstance(rows.get(str(op.get("target", ""))), dict)
        and rows[str(op.get("target", ""))].get("side") == "enemy"
    ]
    tide_causes = [op for op in batch if op.get("op") == "tide_set"]
    mom = int(b.get("momentum", 0))
    if cohort_spec is not None:
        if int(cohort.get("remaining", 0)) > 0:
            return [
                _inherit_semantic_frame_ref(op, defeat_causes)
                for op in _battle_wave_ops(state)
            ]
        settle_causes = [*defeat_causes, *tide_causes]
        return [
            _inherit_semantic_frame_ref(
                {"op": "battle_end", "outcome": "victory"}, settle_causes,
            ),
            _inherit_semantic_frame_ref(
                {"op": "combat_end", "outcome": "victory"}, settle_causes,
            ),
        ]
    if mom <= 0 and int(b.get("waves", 0)) < BATTLE_WAVE_CAP:   # the wider battle isn't won yet
        return [                                    # -> another wave into the War Room
            _inherit_semantic_frame_ref(op, defeat_causes)
            for op in _battle_wave_ops(state)
        ]
    settle_causes = [*defeat_causes, *tide_causes]
    return [
        _inherit_semantic_frame_ref(
            {"op": "battle_end", "outcome": "victory" if mom >= 1 else "resolved"},
            settle_causes,
        ),
        _inherit_semantic_frame_ref(
            {"op": "combat_end", "outcome": "victory"}, settle_causes,
        ),
    ]


# ---- Phase 2 living-world pass (plan doc 13, ratified) — pure, deterministic, journaled ----
def travel_cost(state: dict, a: str, b: str) -> int:
    """Committed route override between two canon locations, else the inferred 1-segment
    default. Reads only state — callers bake the result into the ops they emit."""
    key = "|".join(sorted((str(a), str(b))))
    try:
        return max(1, min(4, int((state.get("routes") or {}).get(key, 1))))
    except (TypeError, ValueError):
        return 1


def _lw_tokens(*texts) -> set:
    """Normalized >=4-char name tokens for front-touch matching (the discovery pattern)."""
    out = set()
    for t in texts:
        for w in re.split(r"[^a-z0-9]+", str(t or "").lower()):
            if len(w) >= 4:
                out.add(w)
    return out


def _event_effect_conflict_key(effect: object) -> tuple[str, str, str, str] | None:
    """Return the typed overlay cell one supported effect owns."""
    if not isinstance(effect, dict) or effect.get("supported") is not True:
        return None
    subject = effect.get("subject")
    if not isinstance(subject, dict):
        return None
    domain, field = effect.get("domain"), effect.get("field")
    kind, subject_id = subject.get("kind"), subject.get("id")
    if not all(isinstance(value, str) and value for value in (domain, field, kind, subject_id)):
        return None
    return domain, kind, subject_id, field


def _front_completion_cause_visibility(front: dict) -> str:
    """Freeze whether the authored agenda itself was known before it completed."""
    return "public" if front.get("revealed_turn") is not None else "hidden"


def _front_completion_consequence(front: dict, front_id: str) -> str:
    """Return Player-safe event fallout, including a neutral legacy-state fallback."""
    consequence = str(front.get("consequence") or "").strip()
    if consequence:
        return consequence
    if _front_completion_cause_visibility(front) == "hidden":
        return "A world change has come to a head."
    return f"the {front.get('name', front_id)} agenda comes to a head"


def _front_completion_memory_text(
    front: dict,
    front_id: str,
    consequence: str | None = None,
) -> str:
    """Project compatibility recall without disclosing a hidden front's identity."""
    visible_consequence = consequence or _front_completion_consequence(front, front_id)
    if _front_completion_cause_visibility(front) == "public":
        return f"World event \N{EM DASH} {front.get('name', front_id)}: {visible_consequence}"
    return f"World event \N{EM DASH} {visible_consequence}"


def _front_completion_event_records(
    state: dict,
    *,
    front_id: str,
    world_id: str,
    session_id: str,
    branch_id: str,
    turn: int,
) -> list[dict]:
    """Build the complete deterministic record group for one front completion.

    The group contains zero or more append-only supersession terminals followed by the new
    admission.  It can be rebuilt both before and after the front tick, which lets fresh admission,
    validation, retry, and replay share one byte-exact source of truth.
    """
    front = (state.get("fronts") or {}).get(front_id)
    if not isinstance(front, dict):
        return []
    from .capability_glossary import content_fingerprint
    from .world_events import build_world_event_record

    consequence = _front_completion_consequence(front, front_id)
    faction = str(front.get("faction") or "")
    subject = f"faction:{faction}" if faction else "world"
    domain = "faction" if faction else "world"
    adapter = "faction.circumstance/1" if faction else "world.circumstance/1"
    affected = {domain, "briefing", "narration", "console", "hud"}
    effects = [{
        "adapter": adapter,
        "domain": domain,
        "subject": subject,
        "field": "circumstance",
        "value": consequence,
        "supported": True,
        "lore": "",
    }]
    selector = None
    propagation = "existing_subjects"
    spawn_eligibility = front.get("spawn_eligibility")
    if isinstance(spawn_eligibility, bool) and faction:
        selector_payload = {
            "schema": "aetherstate-world-subject-selector/1",
            "subject_kinds": ["enemy"],
            "predicates": {"faction": faction},
        }
        selector = {**selector_payload, "fingerprint": content_fingerprint(selector_payload)}
        effects.append({
            "adapter": "spawn.eligibility/1",
            "domain": "enemy_eligibility",
            "subject": f"selector:front.{front_id}.enemy",
            "field": "eligible",
            "value": spawn_eligibility,
            "supported": True,
            "lore": "",
        })
        affected.add("enemy_eligibility")
        propagation = "existing_and_future"

    event_id = "event.front." + hashlib.sha256(
        f"{world_id}|{branch_id}|{turn}|{front_id}|complete".encode()
    ).hexdigest()[:24]
    # Explicit prior reveal makes the cause public.  Auto-reveal on completion deliberately does
    # not expose why a previously hidden front reached its consequence.
    cause_visibility = _front_completion_cause_visibility(front)
    duration = front.get("event_duration_turns")
    admission = build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=session_id,
        branch_id=branch_id,
        turn=turn,
        game_time=turn,
        cause_id=f"front:{front_id}:completion",
        cause_authority="rule",
        cause_visibility=cause_visibility,
        actor=faction or None,
        affected_domains=sorted(affected),
        priority=10,
        scope="branch",
        propagation=propagation,
        future_selector=selector,
        duration=int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else None,
        reversible=True,
        subjects=[subject],
        effects=effects,
        description=consequence,
    )

    records = [row for row in state.get("world_events") or [] if isinstance(row, dict)]
    active_ids: set[str] = set()
    if records:
        try:
            source_branches = state.get("world_event_source_branch_ids")
            overlay = project_world_overlay(
                records,
                world_id=world_id,
                session_id=session_id,
                branch_id=branch_id,
                source_branch_ids=source_branches if isinstance(source_branches, list) else [],
                game_time=turn,
            )
            active_ids = {str(value) for value in overlay.get("active_event_ids") or []}
        except (TypeError, ValueError):
            active_ids = set()
    new_cells = {
        key for key in (_event_effect_conflict_key(effect) for effect in admission["effects"])
        if key is not None
    }
    prior_by_id = {
        str(row.get("event_id")): row for row in records
        if row.get("kind") == "admission" and isinstance(row.get("event_id"), str)
    }
    targets = {
        event_id_value for event_id_value in active_ids
        if event_id_value != event_id
        and event_id_value in prior_by_id
        and new_cells.intersection({
            key for key in (
                _event_effect_conflict_key(effect)
                for effect in prior_by_id[event_id_value].get("effects") or []
            ) if key is not None
        })
    }
    # Exact retries rebuild the original terminals even though their targets are no longer active.
    targets.update(
        str(row.get("relation_target")) for row in records
        if row.get("kind") == "supersession"
        and row.get("cause_id") == f"front:{front_id}:completion"
        and str(row.get("relation_target") or "") in prior_by_id
    )
    terminals: list[dict] = []
    for target in sorted(targets):
        terminal_id = "event.front." + hashlib.sha256(
            f"{world_id}|{branch_id}|{turn}|{front_id}|supersedes|{target}".encode()
        ).hexdigest()[:24]
        terminals.append(build_world_event_record(
            event_id=terminal_id,
            world_id=world_id,
            session_id=session_id,
            branch_id=branch_id,
            turn=turn,
            game_time=turn,
            kind="supersession",
            relation_target=target,
            cause_id=f"front:{front_id}:completion",
            cause_authority="rule",
            cause_visibility=cause_visibility,
            actor=faction or None,
            affected_domains=[],
            priority=10,
            scope="branch",
            propagation="existing_subjects",
            start=turn,
            reversible=False,
            subjects=[],
            effects=[],
            description=f"{consequence} supersedes {target}",
        ))
    return [*terminals, admission]


def world_ops(
    state: dict,
    applied: list[dict],
    clock_turns: int = 6,
    *,
    session_id: str | None = None,
    branch_id: str | None = None,
    turn_index: int | None = None,
) -> list[dict]:
    """Phase 2 (ratified plan doc 13): the living-world referee. Pure reader over
    (state, this batch's applied ops); returns PRIVILEGED rule ops for the caller to
    apply — propose-then-commit, journaled, never a hidden reducer side-effect.

    Floor (weak-model guarantee): the world moves even if the model never cooperates.
    Travel between canon locations consumes clock segments (route override, default 1);
    idle turns advance the clock on a curated cadence; authored faction FRONTS tick
    deterministically off committed triggers (day pace, the Player touching the faction,
    quests resolving against it, its people falling in combat) and a front that FILLS
    commits a world-event + flag the DM is directed to narrate. Rumor-gating is a RENDER
    concern (HUD/briefing) — this pass advances hidden clocks exactly like visible ones."""
    out: list[dict] = []
    if not state.get("player"):
        return out
    turn = int((state.get("meta") or {}).get("turn", -1))
    clock = state.get("clock") or {}
    tod = str(clock.get("time_of_day", "evening"))
    ti = TIMES.index(tod) if tod in TIMES else 4
    batch = [op for op in (applied or []) if isinstance(op, dict)]
    advanced_causes = [
        op for op in batch
        if op.get("op") == "time_advance" and op.get("to_time_of_day")
    ]
    advanced = bool(advanced_causes)
    move = None                              # the LAST committed location change this batch
    for op in batch:
        if op.get("op") == "scene_set" and op.get("_prev_loc") \
                and op.get("location") and op["_prev_loc"] != op["location"]:
            move = op
    pmove = None                             # (from, to) — the camera follows the player
    pmove_cause = None
    if move is None:
        # Floor repair (2026-07-09 live, Cinderveil bench): a DM that never emits a [scene]
        # tag still yields extraction move_entity ops on the PLAYER. The scene — and the
        # travel clock — must follow the player deterministically (pillars 6/12), not the
        # model's tag discipline. Only KNOWN locations move the camera (never mint here).
        dest = None
        pids = set((state.get("player") or {}).keys())
        for op in batch:
            if op.get("op") == "move_entity" and op.get("to_location") \
                    and resolve_entity_ref(state, op.get("entity")) in pids:
                dest = str(op["to_location"])
                pmove_cause = op
        if dest:
            sc = state.get("scene") or {}
            cur = str(sc.get("location_id") or sc.get("location") or "")
            loc_id, _disp, is_new = canonical_location(state, dest)
            if not is_new and loc_id and loc_id != cur:
                pmove = (cur, loc_id)
                out.append(_inherit_semantic_frame_ref(
                    {"op": "scene_set", "location": loc_id}, [pmove_cause],
                ))
    if move is not None and not advanced:    # travel consumes time (an explicit advance wins)
        cost = travel_cost(state, move["_prev_loc"], move["location"])
        out.append(_inherit_semantic_frame_ref(
            {"op": "time_advance", "to_time_of_day": TIMES[(ti + cost) % len(TIMES)]},
            [move],
        ))
    elif pmove is not None and not advanced:  # the player-move fallback pays the same toll
        cost = travel_cost(state, pmove[0], pmove[1]) if pmove[0] else 1
        out.append(_inherit_semantic_frame_ref(
            {"op": "time_advance", "to_time_of_day": TIMES[(ti + cost) % len(TIMES)]},
            [pmove_cause],
        ))
    elif not advanced and clock_turns > 0:   # the idle floor: stories drift forward too
        if turn - int(clock.get("last_advance_turn", -1)) >= int(clock_turns):
            out.append({"op": "time_advance", "to_time_of_day": TIMES[(ti + 1) % len(TIMES)]})
    fronts = state.get("fronts") or {}
    if not fronts:
        return out
    day_wrap_causes = [
        op for op in batch
        if op.get("op") == "time_advance" and op.get("_day_wrap")
    ]
    day_wrap_causes.extend(
        op for op in out
        if op.get("op") == "time_advance" and op.get("to_time_of_day")
        and TIMES.index(op["to_time_of_day"]) <= ti
    )
    day_wrap = bool(day_wrap_causes)
    day = int(clock.get("day", 1)) + (1 if day_wrap else 0)
    combat_rows = (state.get("combat") or {}).get("combatants") or {}
    for fid, f in sorted(fronts.items()):
        if not isinstance(f, dict) or f.get("done"):
            continue
        fac = str(f.get("faction") or "")
        toks = _lw_tokens(f.get("name"), fac.replace("_", " "))
        reason = None
        trigger_causes: list[dict] = []
        if day_wrap and day % max(1, int(f.get("pace", 1))) == 0:
            reason = f"day {day}: the agenda advances on its own"
            trigger_causes = list(day_wrap_causes)
        if reason is None:
            for op in batch:
                k = op.get("op")
                match_reason = None
                if k == "affinity_adj" and fac and slug(str(op.get("target", ""))) == fac:
                    match_reason = "the Player's dealings with the faction shifted its designs"
                elif k == "world_flag" and fac \
                        and slug(str(op.get("faction") or "")) == fac:
                    match_reason = "world truth moved around the faction"
                elif k == "quest_update" and str(op.get("status")) == "complete" \
                        and toks & _lw_tokens(op.get("quest"), op.get("note")):
                    match_reason = "a resolved quest touched the agenda"
                elif k == "combatant_defeat":
                    row = combat_rows.get(str(op.get("target", "")))
                    if isinstance(row, dict) and row.get("side") == "enemy" \
                            and toks & _lw_tokens(row.get("name")):
                        match_reason = "its people met defeat at the Player's hand"
                if match_reason:
                    trigger_causes.append(op)
                    if reason is None:
                        reason = match_reason
        if not reason:
            continue
        out.append(_inherit_semantic_frame_ref(
            {"op": "front_tick", "front": fid, "reason": reason}, trigger_causes,
        ))
        if int(f.get("filled", 0)) + 1 >= max(1, int(f.get("segments", 6))):
            cons = _front_completion_consequence(f, fid)
            out.append(_inherit_semantic_frame_ref(
                {"op": "world_flag", "key": fid, "value": "come to a head",
                 **({"faction": fac} if fac else {})},
                trigger_causes,
            ))
            out.append({"op": "memory_event",
                        "text": _front_completion_memory_text(f, fid, cons)})
            out[-1] = _inherit_semantic_frame_ref(out[-1], trigger_causes)
            world_id = str((state.get("world_identity") or {}).get("world_id") or "")
            if session_id and branch_id and re.fullmatch(r"world_[0-9a-f]{32}", world_id):
                event_turn = int(turn if turn_index is None else turn_index)
                out.extend(
                    {"op": "world_event_admit", "event": event}
                    for event in _front_completion_event_records(
                        state,
                        front_id=fid,
                        world_id=world_id,
                        session_id=session_id,
                        branch_id=branch_id,
                        turn=event_turn,
                    )
                )
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
_SEMANTIC_INGRESS_JOURNAL_KEYS = frozenset(
    {"_semantic_ingress", "_semantic_declaration", "_cohort_initial_count"}
)


def _prepare_semantic_ingress_batch(
    ops: list,
    state: dict,
    session_id: str,
    branch_id: str,
    turn: int,
    *,
    semantic_declaration: object,
    semantic_authority: object,
    semantic_context: object,
    semantic_source: object,
) -> tuple[list, bool, str]:
    """Revalidate one typed declaration and attach JSON-only journal evidence.

    The live authority objects remain out of band.  Reserved evidence supplied inside an op is
    rejected rather than trusted or silently retained.
    """
    original = list(ops)
    if any(
        isinstance(op, dict) and _SEMANTIC_INGRESS_JOURNAL_KEYS.intersection(op)
        for op in original
    ):
        return original, False, "serialized semantic ingress evidence cannot authorize an apply"

    has_declaration = any(
        isinstance(op, dict)
        and op.get("op") == "battle_start"
        and isinstance(op.get("cohort"), dict)
        for op in original
    )
    supplied = any(
        value is not None
        for value in (
            semantic_declaration,
            semantic_authority,
            semantic_context,
            semantic_source,
        )
    )
    if not has_declaration and not supplied:
        return original, False, ""
    if not has_declaration:
        return original, False, "semantic ingress authority was supplied without its declaration"
    if not all(
        value is not None
        for value in (
            semantic_declaration,
            semantic_authority,
            semantic_context,
            semantic_source,
        )
    ):
        return original, False, "finite cohort declaration requires complete live ingress context"

    copied = deepcopy(original)

    try:
        from .semantic_ingress import (
            AuthorizedSemanticDeclaration,
            SemanticIngressAuthority,
            SemanticIngressContext,
            parse_authorized_cohort_declaration,
        )

        if type(semantic_declaration) is not AuthorizedSemanticDeclaration \
                or type(semantic_authority) is not SemanticIngressAuthority \
                or type(semantic_context) is not SemanticIngressContext:
            raise ValueError("finite cohort admission requires typed live ingress objects")
        scope = semantic_context.scope
        if (
            scope.session_id != session_id
            or scope.branch_id != branch_id
            or scope.turn_index != turn
        ):
            raise ValueError("semantic ingress scope does not match this apply identity")
        parsed = parse_authorized_cohort_declaration(
            semantic_source,
            state,
            authority=semantic_authority,
            expected_context=semantic_context,
        )
        if parsed.as_evidence() != semantic_declaration.as_evidence():
            raise ValueError("semantic declaration differs from the live parser result")
        declared_ops = list(parsed.operations)
        if copied != declared_ops:
            raise ValueError("apply operations differ from the authorized declaration")

        receipt_evidence = json.loads(
            json.dumps(
                semantic_authority.as_evidence(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        declaration_evidence = json.loads(
            json.dumps(
                parsed.as_evidence(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        return copied, False, str(exc)

    initial_count = sum(
        isinstance(op, dict)
        and op.get("op") == "combatant_spawn"
        and op.get("cohort_ref") is not None
        for op in copied
    )
    prepared: list = []
    for op in copied:
        if not isinstance(op, dict):
            prepared.append(op)
            continue
        admitted = {
            **op,
            "_semantic_ingress": deepcopy(receipt_evidence),
            "_semantic_declaration": deepcopy(declaration_evidence),
        }
        if op.get("op") == "battle_start" and isinstance(op.get("cohort"), dict):
            admitted["_cohort_initial_count"] = initial_count
        prepared.append(admitted)
    return prepared, True, ""


@dataclass
class ApplyResult:
    applied: list[dict] = field(default_factory=list)
    # Number of caller-submitted operations that applied. ``applied`` is the durable
    # occurrence stream and can contain code-authored companion occurrences (for example,
    # an ``entity_add`` emitted before an unknown user alias's owning operation).
    submitted_applied: int = 0
    quarantined: list[dict] = field(default_factory=list)   # each: {"op":..., "reason":...}
    duplicates: list[dict] = field(default_factory=list)    # identical EffectId no-ops
    state: dict = field(default_factory=empty_state)
    froze: bool = False
    unfroze: bool = False


def _attach_world_event_branch_view(store, branch_id: str, state: dict) -> dict:
    """Attach Store-owned retrieval lineage and the matching active overlay to a state view."""
    if state.get("claims") or state.get("facts") or state.get("beliefs") \
            or state.get("epistemic_history"):
        state["knowledge_record_scope"] = store.knowledge_record_scope(branch_id)
    if state.get("world_events"):
        state["world_event_branch_id"] = branch_id
        state["world_event_source_branch_ids"] = store.world_event_origin_branches(branch_id)
        try:
            from .world_events import project_state_overlay

            state["world_overlay"] = project_state_overlay(state)
        except Exception:
            state["world_overlay"] = {}
    return state


def current_state(store, branch_id: str) -> dict:
    source_branch_ids = store.world_event_origin_branches(branch_id)

    def reduce_branch_state(state: dict, ops: list[dict]) -> dict:
        # Forked journals retain each immutable event's origin branch.  Supply the Store-owned
        # child view before replaying an event batch so a child terminal can target an inherited
        # parent admission.  Attaching this only when events exist keeps event-free replay byte
        # compatible with the historical empty-state shape.
        if state.get("world_events") or any(
            isinstance(op, dict) and op.get("op") == "world_event_admit" for op in ops
        ):
            state["world_event_branch_id"] = branch_id
            state["world_event_source_branch_ids"] = list(source_branch_ids)
        return reduce_state(state, ops)

    state = store.state_at(branch_id, BIG_TURN, reduce_branch_state, empty=empty_state())
    return _attach_world_event_branch_view(store, branch_id, state)


_DAMAGE_OPS = {"hp_adj", "combatant_hp"}


def _damage_target(op: dict) -> str:
    key = "char" if op.get("op") == "hp_adj" else "target"
    return str(op.get(key, "")).strip().casefold()


def _damage_delta(op: dict) -> int:
    return int(op.get("_delta", op.get("delta", 0)))


def _damage_direction(op: dict) -> str:
    delta = _damage_delta(op)
    return "harm" if delta < 0 else "heal" if delta > 0 else "neutral"


def _damage_payload_hash(op: dict) -> str:
    payload = {"family": str(op.get("op")), "target": _damage_target(op),
               "delta": _damage_delta(op)}
    return hashlib.blake2b(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                           digest_size=16).hexdigest()


def assign_damage_effect_ids(ops: list[dict], branch_id: str, turn: int, owner: str,
                             basis: str = "", canonical: bool = False) -> list[dict]:
    """Attach stable local identities only to damage operations; all other ops pass through."""
    out = [deepcopy(op) if isinstance(op, dict) else op for op in ops]
    positions = [i for i, op in enumerate(out)
                 if isinstance(op, dict) and op.get("op") in _DAMAGE_OPS]
    if canonical:
        positions.sort(key=lambda i: json.dumps(
            {"family": out[i].get("op"), "target": _damage_target(out[i]),
             "delta": int(out[i].get("delta", 0)), "reason": str(out[i].get("reason", ""))},
            sort_keys=True, separators=(",", ":")))
    for ordinal, pos in enumerate(positions):
        op = out[pos]
        op.setdefault("_effect_owner", owner)
        if not op.get("_effect_id"):
            material = json.dumps(
                [branch_id, int(turn), owner, basis, op.get("op"), _damage_target(op), ordinal],
                separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.blake2b(material.encode(), digest_size=12).hexdigest()
            op["_effect_id"] = f"dmg_{digest}"
        settlement_ref = op.get("_settlement_ref")
        member_index = op.get("_settlement_member_index")
        if settlement_ref is not None and isinstance(member_index, int):
            wrappers = [
                candidate for candidate in out
                if isinstance(candidate, dict)
                and candidate.get("op") == "mechanic_settlement_commit"
                and candidate.get("settlement_ref") == settlement_ref
            ]
            if len(wrappers) == 1:
                members = wrappers[0].get("members")
                if isinstance(members, list) and 0 <= member_index < len(members) \
                        and isinstance(members[member_index], dict) \
                        and members[member_index].get("op") in _DAMAGE_OPS:
                    members[member_index]["_effect_id"] = op["_effect_id"]
                    members[member_index]["_effect_owner"] = op["_effect_owner"]
    return out


_TRACE_OP_ID_KEYS = (
    ("char", "char"),
    ("target", "target"),
    ("entity", "entity"),
    ("faction", "faction"),
    ("_effect_id", "effect_id"),
    ("_effect_owner", "effect_owner"),
)
_TRACE_OP_NUMBER_KEYS = (("delta", "delta"), ("_delta", "internal_delta"))


def _trace_op(op: Any) -> dict:
    if not isinstance(op, dict):
        return {
            "op": "invalid",
            "value_type_receipt": text_receipt(type(op).__name__),
        }
    fields = sorted(str(key) for key in op)
    kind = safe_token(op.get("op"))
    out = {
        "op": kind or "other",
        "field_count": len(fields),
        "fields_sha256": canonical_sha256(fields),
        "payload_sha256": canonical_sha256(op),
    }
    if kind is None and op.get("op"):
        out["op_receipt"] = text_receipt(op.get("op"))
    for source_key, trace_key in _TRACE_OP_ID_KEYS:
        if source_key not in op or op[source_key] is None:
            continue
        value = safe_token(op[source_key])
        if value is not None:
            out[trace_key] = value
        else:
            out[f"{trace_key}_receipt"] = text_receipt(op[source_key])
    for source_key, trace_key in _TRACE_OP_NUMBER_KEYS:
        value = op.get(source_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[trace_key] = value
    for key in ("name", "reason"):
        if isinstance(op.get(key), str) and op[key]:
            out[f"{key}_receipt"] = text_receipt(op[key])
    return out


_SCENE_MOVEMENT_SOURCES = frozenset({"user", "rule"})


def _enrich(op: dict, turn: int, cfg, state: Optional[dict] = None,
            source: str = "rule", *, session_id: Optional[str] = None,
            branch_id: Optional[str] = None) -> dict:
    """Bake config/registry-dependent values into the journaled op (replay determinism,
    03 SS3.3; doc 07 §6 extends the bake to item reference data). `state` is the pre-apply
    snapshot — used only to generate a fresh, unique instance id at mint.

    ``source`` is also the live causal authority for scene movement.  Genesis and extraction may
    correct the descriptive scene anchor, but only an explicit user/rule transition can bake the
    movement marker consumed by travel time, last-move presentation, and combat departure.  Replay
    bypasses enrichment and therefore preserves every already-journaled marker byte-for-byte.
    """
    out = dict(op)
    out["_turn"] = turn
    if op["op"] in {"fact_admit", "belief_acquire"}:
        from .capability_glossary import content_fingerprint

        statement = str(op.get("statement") or "").strip()
        supplied_pid = str(op.get("proposition_id") or "").strip()
        if statement:
            derived_pid = polarized_proposition_id(statement)
            proposition_identity = proposition_id(statement)
            if supplied_pid and supplied_pid not in {derived_pid, proposition_identity}:
                raise ValueError("supplied proposition identity does not match statement")
            out["proposition_id"] = derived_pid
            out["proposition_identity"] = proposition_identity
            out["statement"] = statement
        elif supplied_pid:
            out["proposition_id"] = supplied_pid
            proposition_row = (
                ((state or {}).get("propositions") or {}).get(supplied_pid) or {}
            )
            statement = str(proposition_row.get("statement", "")).strip()
            out["proposition_identity"] = str(
                proposition_row.get("identity") or supplied_pid
            )
            if statement:
                out["statement"] = statement
        if op["op"] == "fact_admit":
            pid = str(out["proposition_id"])
            fact_id = "fact_" + hashlib.sha256(pid.encode()).hexdigest()[:16]
            record = {
                "schema": "aetherstate-fact-record/2",
                "fact_id": fact_id,
                "session_id": str(session_id or "historical"),
                "branch_id": str(branch_id or "historical"),
                "world_id": str(
                    ((state or {}).get("world_identity") or {}).get("world_id")
                    or "world_unbound"
                ),
                "turn": int(turn),
                "proposition_id": pid,
                "proposition_identity": str(out.get("proposition_identity") or pid),
                "statement": statement,
                "proposition_polarity": normalized_proposition(statement)[1],
                "cause": str(op["cause"]),
                "authority": str(op.get("authority") or source),
                "ingress": str(source),
                "authority_ceiling": "objective_fact",
                "visibility": str(op.get("visibility") or "public"),
                "scoped_actors": sorted({str(v) for v in op.get("scoped_actors") or []}),
                "status": "accepted",
            }
            record["fingerprint"] = content_fingerprint(record)
            out["_fact_id"] = fact_id
            out["_record"] = record
        else:
            raw_holder = str(op["holder"])
            resolved_holder = resolve_entity_ref(state or {}, raw_holder) or raw_holder
            holder = normalize_actor_id(resolved_holder) or slug(resolved_holder)
            evidence = str(op.get("evidence_source") or op.get("source"))
            pid = str(out["proposition_id"])
            scoped = sorted({
                normalize_actor_id(resolve_entity_ref(state or {}, str(value)) or str(value))
                or slug(str(value))
                for value in op.get("scoped_actors") or [holder]
            })
            identity_payload = {
                "holder": holder,
                "proposition_id": pid,
                "turn": int(turn),
                "stance": str(op["stance"]),
                "source": evidence,
                "evidence_source": evidence,
                "ingress": str(source),
                "authority_ceiling": "actor_epistemic_only",
                "claim_id": op.get("claim_id"),
            }
            belief_id = "belief:" + content_fingerprint(identity_payload).removeprefix("sha256:")
            record = {
                "schema": "aetherstate-epistemic-record/2",
                "belief_id": belief_id,
                "session_id": str(session_id or "historical"),
                "branch_id": str(branch_id or "historical"),
                "world_id": str(
                    ((state or {}).get("world_identity") or {}).get("world_id")
                    or "world_unbound"
                ),
                "turn": int(turn),
                "holder": holder,
                "proposition_id": pid,
                "proposition_identity": str(out.get("proposition_identity") or pid),
                "statement": statement,
                "proposition_polarity": normalized_proposition(statement)[1]
                if statement else "unspecified",
                "stance": str(op["stance"]),
                "source": evidence,
                "teller": op.get("teller"),
                "claim_id": op.get("claim_id"),
                "ingress": str(source),
                "authority_ceiling": "actor_epistemic_only",
                "establishes_truth": False,
                "admits_world_event": False,
                "visibility": str(op.get("visibility") or "actor_scoped"),
                "scoped_actors": scoped,
                "status": "current",
            }
            record["fingerprint"] = content_fingerprint(record)
            out["source"] = evidence
            out["holder"] = holder
            out["scoped_actors"] = scoped
            out["_belief_id"] = belief_id
            out["_record"] = record
    if op["op"] == "claim_record":
        from .claim_frame import build_claim_record, validate_claim_frame

        frame = validate_claim_frame(op.get("frame"))
        ingress = str(frame.get("ingress") or "code")
        record_source = slug(str(frame.get("source") or ingress))
        world_id = str(((state or {}).get("world_identity") or {}).get("world_id") or "world_unbound")
        try:
            occurrence_index = max(1, int(str(frame.get("frame_id") or "1").rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            occurrence_index = 1
        scoped = [
            str(value) for value in (frame.get("speaker"), frame.get("addressee")) if value
        ]
        out["frame"] = json.loads(json.dumps(frame))
        out["_record"] = build_claim_record(
            frame,
            session_id=str(session_id or "historical"),
            branch_id=str(branch_id or "historical"),
            world_id=world_id,
            turn=int(turn),
            source=record_source,
            occurrence_index=occurrence_index,
            visibility=str(op.get("visibility") or "public"),
            scoped_actors=op.get("scoped_actors") or scoped,
        )
    if op["op"] == "semantic_meaning_commit":
        from .semantic_fabric import validate_compiled_meaning_receipt

        meaning = validate_compiled_meaning_receipt(op.get("meaning"))
        out["meaning"] = json.loads(json.dumps(meaning))
    if op["op"] == "semantic_binding_commit":
        from .semantic_binding import validate_meaning_binding

        binding = validate_meaning_binding(op.get("binding"))
        out["binding"] = json.loads(json.dumps(binding))
    if op["op"] == "semantic_world_alignment_commit":
        from .semantic_binding import validate_world_alignment

        alignment = validate_world_alignment(op.get("alignment"))
        out["alignment"] = json.loads(json.dumps(alignment))
    if op["op"] == "semantic_frame_commit":
        from .semantic import validate_action_frame_snapshot

        frame = validate_action_frame_snapshot(op.get("frame"))
        out["frame"] = json.loads(json.dumps(frame))
    if op["op"] == "creator_world_seed":
        # Caller-supplied sealing evidence is never trusted.  Rebuild it from the validated source
        # document and current replayable turn at the privileged Creator boundary.
        out.pop("_snapshot", None)
        out["document"] = json.loads(json.dumps(
            op.get("document"), ensure_ascii=False, sort_keys=True, allow_nan=False,
        ))
        out["_snapshot"] = _creator_world_snapshot(out["document"], turn)
    if op["op"] == "capability_assign":
        # Callers submit refs and provenance only. The live apply path resolves and bakes
        # authority from append-only WorldLex storage; caller-supplied snapshots are ignored.
        out.pop("_assignment", None)
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
            snap = {k: tpl[k] for k in (
                "name", "worn", "slot", "mods", "covers", "on_consume", "stackable",
                "max_stack", "capacity", "is_container", "type") if k in tpl}
            snap.setdefault("name", str(op.get("template", "")))
            cls = classify_item(snap.get("name", ""), snap)   # gear vs inv + auto-equip slot
            snap["class"] = cls["class"]
            if cls["worn"]:
                snap["worn"] = True
            if cls["slot"]:
                snap.setdefault("slot", cls["slot"])
            snap["type"] = snap.get("type") or cls["type"]
            out["_snapshot"] = snap
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
    if op["op"] == "tide_set":                 # §F: the DM's macro report -> a single clamped
        cur = int(((state or {}).get("battle") or {}).get("momentum", 0))   # step toward the tide
        target = {"winning": BATTLE_MOMENTUM_CLAMP[1], "losing": BATTLE_MOMENTUM_CLAMP[0],
                  "holding": 0}.get(op.get("tide"), 0)
        out["_delta"] = 1 if target > cur else -1 if target < cur else 0   # engine owns the pace:
    if op["op"] == "battle_wave":              # at most one step/turn. a cleared wave nudges the
        out["_delta"] = int(op.get("_delta", 1))   # tide toward the player (their slice won)
    if op["op"] == "item_transfer":
        iid = _resolve_instance(state or {}, op.get("instance"))
        item = ((state or {}).get("items") or {}).get(iid) if iid else None
        if isinstance(item, dict) and str(item.get("loc") or "").split(":", 1)[0] == "world":
            # Bake the custody check into new journal rows. Historical rows have no marker and
            # retain their historical replay meaning; all fresh world pickups fail closed.
            out["_world_pickup_checked"] = True
            out["_world_pickup_here"] = _world_item_is_here(state or {}, item)
    if op["op"] == "item_gain":                # RPG-5 (G2): template-snapshot floor — a name
        if source == "extraction":
            wanted_terms = _item_reference_terms(op.get("name"))
            conflicts = sorted(
                iid for iid, item in (((state or {}).get("items") or {}).items())
                if isinstance(item, dict) and item.get("owner") is None
                and str(item.get("loc") or "").split(":", 1)[0] == "world"
                and wanted_terms & _item_reference_terms(item.get("name"))
            )
            if conflicts:
                # Tier-1 cannot prove whether pickup wording denotes one of these instances or a
                # genuinely new object. Fail closed; the live reply path can bind an exact transfer.
                out["_world_item_gain_conflict"] = conflicts
        from . import registry as _registry    # matching a curated template grounds its
        tid = None                             # mechanics; anything else commits MECHANICS-FREE
        try:                                   # (no power minted from prose) but STILL classified
            reg = _registry.load(cfg)          # gear vs inventory + an auto-equip slot (floor)
            want = str(op.get("name", "")).strip().lower()
            tid = next((t for t, tp in reg.items.items()
                        if t == slug(want) or str((tp or {}).get("name", "")).lower() == want),
                       None)
        except Exception:
            tid = None
        snap: dict = {}
        if tid:
            tpl = reg.items.get(tid) or {}
            snap = {k: tpl[k] for k in (
                "name", "worn", "slot", "mods", "covers", "on_consume", "stackable",
                "max_stack", "capacity", "is_container", "type") if k in tpl}
            snap["_template"] = tid
        snap.setdefault("name", str(op.get("name", "")))
        want_slot = str(op.get("slot") or "").strip().lower()   # authored/MANUAL slot wins over
        if want_slot in GEAR_SLOTS:                             # the name heuristic (Creator row /
            snap["slot"], snap["worn"], snap["class"] = want_slot, True, "gear"   # ((aether.equip)))
        aura = str(op.get("aura") or op.get("effect") or "").strip()   # 2026-07-10 (Bean): a gear
        if aura:                                                       # PROSE/glamour/lore effect
            # Match the complete Creator prose allowance. The old 240-character clamp could
            # amputate a validated sentence only after the main-model response had passed.
            snap["aura"] = aura[:4000]                                 # that MATTERS to narration
        cls = classify_item(snap.get("name", ""), snap)
        snap["class"] = cls["class"]
        if cls["worn"]:
            snap["worn"] = True
        if cls["slot"]:
            snap.setdefault("slot", cls["slot"])
        snap["type"] = snap.get("type") or cls["type"]
        wf = _worn_flag(op.get("name"))     # explicit author tag ('(worn)'/'(carried)') wins
        if wf is True:
            snap["class"] = "gear"
            snap["worn"] = True
            if not snap.get("slot"):
                snap["slot"] = "body"
        elif wf is False:
            snap["worn"] = False
            snap.pop("slot", None)
        out["_snapshot"] = snap
    if op["op"] == "hp_adj":                   # RPG-5 (G7): per-op swing clamp baked — the
        try:                                    # narrator proposes, the clamp owns the number
            mx = int((((state or {}).get("player", {}).get(op.get("char")) or {})
                      .get("hp") or {}).get("max", 0))
        except Exception:
            mx = 0
        cap = max(HP_ADJ_MIN_CAP, mx // 4)
        out["_delta"] = _clamp(op.get("delta", 0), -cap, cap)
        if isinstance(op.get("_opposition"), dict):
            out["_opposition"] = {**op["_opposition"],
                                  "damage": max(0, -int(out["_delta"]))}
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
    if op["op"] == "combatant_spawn":          # Phase 1: freeze the instance AT SPAWN —
        # Unknown extra fields are accepted for forward compatibility, so overwrite internal
        # snapshots explicitly: callers cannot smuggle a kit or prepared action into the ledger.
        out.pop("_kit", None)
        out.pop("_initial_intent", None)
        out.pop("_capability_pool", None)
        out.pop("_capability_assignments", None)
        out.pop("_kit_source", None)
        entity_id = op.get("char")
        supplied_faction = op.get("faction")
        stored_faction = (((state or {}).get("attributes") or {}).get(entity_id) or {}).get(
            "faction"
        ) if entity_id else None
        supplied_faction_id = resolve_entity_ref(state or {}, supplied_faction) \
            if supplied_faction else None
        stored_faction_id = resolve_entity_ref(state or {}, stored_faction) \
            if stored_faction else None
        for faction_id in (supplied_faction_id, stored_faction_id):
            if faction_id is not None and (((state or {}).get("entities") or {}).get(
                    faction_id) or {}).get("kind") != "faction":
                raise OpReject("combatant faction must resolve to one stable faction entity")
        if supplied_faction and supplied_faction_id is None:
            raise OpReject("combatant faction must resolve to one stable faction entity")
        if stored_faction and stored_faction_id is None:
            raise OpReject("tracked combatant has an invalid committed faction")
        if supplied_faction_id and stored_faction_id \
                and supplied_faction_id != stored_faction_id:
            raise OpReject("combatant faction conflicts with the tracked actor's committed faction")
        faction_id = stored_faction_id or supplied_faction_id
        if faction_id:
            out["faction"] = faction_id
        else:
            out.pop("faction", None)
        rows = ((state or {}).get("combat") or {}).get("combatants") or {}
        base = slug(str(op.get("name", "foe")))[:32] or "foe"
        cid, n = base, 1
        while cid in rows:
            n += 1
            cid = f"{base}#{n}"
        out["_cid"] = cid
        tier = op.get("tier") if op.get("tier") in THREAT_TIERS else "standard"
        out.setdefault("tier", tier)
        hp = {"cur": THREAT_HP[tier], "max": THREAT_HP[tier]}
        eid = op.get("char")                    # tracked: WOUNDS PERSIST — a prior fight's
        prior = (((state or {}).get("attributes") or {}).get(eid) or {}).get("hp") \
            if eid else None                    # toll is the next fight's starting HP
        if isinstance(prior, dict) and int(prior.get("max", 0)) > 0:
            hp = {"cur": _clamp(prior.get("cur", prior["max"]), 0, int(prior["max"])),
                  "max": int(prior["max"])}
        out["_hp"] = hp
        out["_mod"] = THREAT_MOD.get(tier, 1)   # procedural logical stats, curated by tier
        _tb = 0                                 # initiative (2026-07-10, Bean): a curated turn-
        for _ch in cid:                         # order score — the tier's logical stat scaled,
            _tb = (_tb * 31 + ord(_ch)) & 0x7FFF   # plus a STABLE per-combatant tiebreak, baked
        out["_init"] = out["_mod"] * 10 + (_tb % 10)   # so replay stays pure (no re-roll)
        table = ((state or {}).get("loot") or {}).get(tier)   # frozen table wins (pillar 18);
        if not isinstance(table, list) or not table:          # registry rows are the floor
            try:
                from . import registry as _registry
                table = (_registry.load(cfg).loot or {}).get(tier)
            except Exception:
                table = None
        out["_loot"] = list(table) if isinstance(table, list) else list(_LOOT_FALLBACK[tier])
        spec = getattr(cfg, "specialization", None)
        if spec is not None and spec.name == "rpg" and getattr(spec, "war_room", True):
            identity: dict = {}
            if eid:
                attrs = ((state or {}).get("attributes") or {}).get(eid)
                if isinstance(attrs, dict):
                    identity.update(attrs)
                entity = ((state or {}).get("entities") or {}).get(eid)
                if isinstance(entity, dict) and entity.get("kind"):
                    identity.setdefault("type", entity.get("kind"))
            armament = str(op.get("armament") or "") or grounded_actor_armament(identity)
            if armament:
                out["armament"] = armament
            kit = build_enemy_kit(str(op.get("name", "foe")), tier, armament, identity)
            out["_kit"] = kit
            combat = (state or {}).get("combat") or {}
            if op.get("side") == "enemy" \
                    and getattr(spec, "enemy_rolls", True) \
                    and not isinstance(combat.get("pending_intent"), dict):
                peid = _player_target_eid(state or {}) or "player"
                pname = str((((state or {}).get("entities") or {}).get(peid) or {}).get("name") or peid)
                initial = select_enemy_intent(
                    {"id": cid, "name": str(op.get("name") or cid), "kit": kit},
                    turn, peid, pname)
                if initial is not None:
                    out["_initial_intent"] = initial
    if op["op"] == "enemy_intent_set":
        out.pop("_intent", None)
        out.pop("_kit", None)
        cid = resolve_combatant(state or {}, op.get("actor"))
        row = (((state or {}).get("combat") or {}).get("combatants") or {}).get(cid or "")
        if isinstance(row, dict) and row.get("side") == "enemy" and not row.get("defeated") \
                and _int_or((row.get("hp") or {}).get("cur", 0), 0) > 0:
            peid = _player_target_eid(state or {}) or "player"
            pname = str((((state or {}).get("entities") or {}).get(peid) or {}).get("name") or peid)
            player = ((state or {}).get("player") or {}).get(peid) or {}
            last = player.get("_opposition_last") if isinstance(player, dict) else None
            # Cadence history is actor-local. Borrowing the last move from another live enemy
            # makes the freshly rotated intent fail its own frozen-kit identity check, so every
            # later referee pass journals the same replacement again.
            previous = str(last.get("move_id") or "") \
                if isinstance(last, dict) and str(last.get("actor") or "") == str(cid) else ""
            kit = row.get("kit") if isinstance(row.get("kit"), dict) else None
            if not kit or not kit.get("moves"):
                # Pre-kit saves migrate through this privileged journal op.  Replay consumes the
                # baked snapshot below; it never calls the generator or silently rewrites history.
                identity: dict = {}
                eid = row.get("eid")
                attrs = ((state or {}).get("attributes") or {}).get(eid)
                if isinstance(attrs, dict):
                    identity.update(attrs)
                entity = ((state or {}).get("entities") or {}).get(eid)
                if isinstance(entity, dict) and entity.get("kind"):
                    identity.setdefault("type", entity.get("kind"))
                kit = build_enemy_kit(str(row.get("name") or cid), str(row.get("tier") or "standard"),
                                      str(row.get("armament") or ""), identity)
                out["_kit"] = kit
            intent = select_enemy_intent({**row, "kit": kit}, turn, peid, pname,
                                         previous_move_id=previous)
            if intent is not None:
                out["_intent"] = intent
    if op["op"] == "combatant_hp":             # per-op swing clamp, mirrored from hp_adj
        cid = resolve_combatant(state or {}, op.get("target"))
        row = (((state or {}).get("combat") or {}).get("combatants") or {}).get(cid) or {}
        mx = int((row.get("hp") or {}).get("max", 0))
        cap = max(HP_ADJ_MIN_CAP, mx // 4)
        if not op.get("_strike"):              # a code-decided player strike is exact — only
            out["_delta"] = _clamp(op.get("delta", 0), -cap, cap)   # proposals get clamped
    if op["op"] == "combatant_defeat":         # deterministic loot roll, baked — replay-pure
        import random as _rnd
        rows = (((state or {}).get("combat") or {}).get("combatants") or {})
        cid = op.get("target") if op.get("target") in rows \
            else resolve_combatant(state or {}, op.get("target"))
        row = rows.get(cid) or {}
        seed = int(hashlib.md5(f"loot:{turn}:{cid}".encode()).hexdigest()[:8], 16)
        rng = _rnd.Random(seed)
        drops, existing = [], set(((state or {}).get("items") or {}).keys())
        for e in (row.get("loot") or []):
            if not isinstance(e, dict) or not str(e.get("name", "")).strip():
                continue
            if rng.random() > float(e.get("chance", 1.0)):
                continue
            lo = max(1, int(e.get("qty_min", e.get("qty", 1)) or 1))
            hi = max(lo, int(e.get("qty_max", e.get("qty", 1)) or 1))
            iid, base2, k = None, slug(str(e["name"]))[:48], 0
            iid = base2
            while iid in existing:
                k += 1
                iid = f"{base2}#{k + 1}"
            existing.add(iid)
            drops.append({"name": str(e["name"]).strip(), "qty": rng.randint(lo, hi),
                          "_iid": iid})
        out["_loot_drop"] = drops
        out["_xp"] = THREAT_XP.get(str(row.get("tier")), THREAT_XP["standard"])
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
        # Internal movement evidence is always recomputed from live state.  A new caller cannot
        # forge it, while historical rows retain it because replay calls the reducer directly.
        out.pop("_prev_loc", None)
        prev = (state or {}).get("scene", {}).get("location_id")
        if source in _SCENE_MOVEMENT_SOURCES and prev and prev != out["location"]:
            out["_prev_loc"] = prev            # travel cost + [TRAVEL] + departure derive from it
    rpg = getattr(cfg, "specialization", None) is not None \
        and cfg.specialization.name == "rpg"
    if op["op"] == "time_advance" and rpg:     # Phase 2 (rpg-baked only — a `none` session's
        out["_turn_mark"] = 1                  # ops stay byte-identical): idle-tick anchor +
        new = op.get("to_time_of_day")         # day-wrap mark for day-paced fronts
        cur = str(((state or {}).get("clock") or {}).get("time_of_day", "evening"))
        if new in TIMES and cur in TIMES and TIMES.index(new) <= TIMES.index(cur):
            out["_day_wrap"] = 1
    if op["op"] == "front_add":
        out["_fid"] = slug(str(op.get("name", "")))[:64]
        out["_segments"] = _clamp(op.get("segments", 6), 3, 12)
        out["_pace"] = _clamp(op.get("pace", 1), 1, 3)
        if op.get("event_duration_turns") is not None:
            out["_event_duration_turns"] = _clamp(op["event_duration_turns"], 1, 100)
    if op["op"] == "front_tick":
        out["_delta"] = 1                      # one segment per tick, always — pace lives in
        #                                        the trigger cadence, never the step size
    if op["op"] == "front_reveal":             # resolve id | display name against committed rows
        ref = str(op.get("front", "")).strip()
        fronts = (state or {}).get("fronts") or {}
        if ref not in fronts:
            low = ref.lower()
            for fid, f in fronts.items():
                if isinstance(f, dict) and str(f.get("name", "")).lower() == low:
                    out["front"] = fid
                    break
            else:
                hit = slug(ref)[:64]
                if hit in fronts:
                    out["front"] = hit
    if op["op"] == "route_set":
        out["a"], out["b"] = slug(str(op.get("a", "")))[:64], slug(str(op.get("b", "")))[:64]
        out["_segments"] = _clamp(op.get("segments", 1), 1, 4)
    return out


def _materialize_enemy_capability_pool(
    store,
    *,
    kit: dict,
    world_id: str,
    subject_id: str,
    turn: int,
) -> tuple[dict, list[dict], dict]:
    """Translate one frozen enemy kit through real WorldLex evidence.

    Definitions enter the append-only world library first. Exact assignments then authorize the
    spawned enemy's ownership without claiming execution. The enemy HP adapter supplies separate,
    code-owned eligibility and receipt-admission evidence before a runtime pool can execute.
    """
    candidates = compile_enemy_candidates(kit, world_id=world_id)
    definitions = {
        definition["fingerprint"]: definition
        for definition in candidates["definitions"]
    }
    for definition in candidates["definitions"]:
        store.worldlex.append_definition(definition, expected_world_id=world_id)

    subject = SubjectRef("enemy", subject_id, world_id)
    assignments_by_move: dict[str, dict] = {}
    assignment_refs: dict[str, str] = {}
    eligibility_refs: dict[str, str] = {}
    admission_refs: dict[str, str] = {}
    for mechanics in sorted(candidates["mechanics"], key=lambda item: item["move_index"]):
        move_id = mechanics["move"]["id"]
        definition = definitions[mechanics["definition_ref"]["fingerprint"]]
        assignment = materialize_assignment(
            store.worldlex,
            definition=DefinitionRef.from_dict(mechanics["definition_ref"]),
            subject=subject,
            acquisition_source=f"enemy_spawn.kit.{kit['fingerprint']}",
            acquisition_turn=turn,
        )
        assignments_by_move[move_id] = assignment.as_dict()
        assignment_refs[move_id] = assignment.assignment_id
        eligibility_refs[move_id] = (
            f"eligibility:enemy-kit:{kit['fingerprint']}:{subject_id}:{turn}:{move_id}"
        )
        admission_refs[move_id] = (
            f"admission:enemy-opposition-hp:{kit['fingerprint']}:{subject_id}:{turn}:{move_id}:"
            f"{definition['fingerprint']}"
        )

    receipt_admission = seal_enemy_hp_receipt_admission(
        admission_refs,
        authority_ref="code:aetherstate.enemy-opposition-hp/1",
    )
    bundle = compile_enemy_capability_bundle(
        candidates,
        subject_id=subject_id,
        assignment_refs=assignment_refs,
        eligibility_refs=eligibility_refs,
        receipt_admission=receipt_admission,
    )
    reconstructed = reconstruct_enemy_kit(bundle)
    if reconstructed != kit:
        raise EnemyCapabilityPoolError(
            "runtime capability pool did not reconstruct the exact frozen enemy kit"
        )
    assignments = sorted(
        assignments_by_move.values(),
        key=lambda item: item["definition"]["definition_id"],
    )
    return bundle, assignments, reconstructed


def _enemy_pool_subject_id(combatant_id: str) -> str:
    """Stable WorldLex id for a combat row whose legacy id may contain ``#``."""
    return "enemy." + str(combatant_id).replace("#", ":")


def _world_overlay_operation_violation(
    state: dict, op: dict, *, turn: int | None = None,
) -> Optional[str]:
    """Fail closed before a consumer materializes Store-owned side effects."""
    try:
        from .world_events import (
            capability_eligible,
            future_subject_eligible,
            future_subject_identity_resolved,
            project_state_overlay,
        )

        kind = str(op.get("op") or "")
        if kind == "entity_add":
            raw_kind = str(op.get("kind") or "character")
            candidate_kind = {
                "character": "npc", "npc": "npc", "player": "actor", "enemy": "enemy",
            }.get(raw_kind, raw_kind)
            if candidate_kind in {"actor", "npc", "enemy"}:
                candidate = {
                    field: op.get(field) for field in (
                        "faction", "name", "tier", "role", "location", "tags",
                    )
                }
                candidate.update({
                    "id": str(op.get("entity") or slug(str(op.get("name") or ""))),
                    "kind": candidate_kind,
                })
                if not future_subject_eligible(state, candidate, game_time=turn):
                    return "an active WorldOverlay makes this future actor ineligible"
        if kind == "combatant_spawn":
            candidate = {
                "kind": "enemy" if op.get("side") == "enemy" else "ally",
                "id": str(op.get("_cid") or op.get("char") or slug(str(op.get("name") or ""))),
                **{
                    field: op.get(field) for field in (
                        "faction", "name", "role", "location", "tags",
                    )
                },
                "tier": op.get("tier", "standard"),
            }
            if not future_subject_identity_resolved(state, candidate, game_time=turn):
                return "an active WorldOverlay requires stable faction identity for this spawn"
            if not future_subject_eligible(state, candidate, game_time=turn):
                return "an active WorldOverlay makes this future spawn ineligible"
            kit = op.get("_kit")
            if isinstance(kit, dict):
                for index, move in enumerate(kit.get("moves") or []):
                    if not isinstance(move, dict):
                        continue
                    move_id = str(move.get("id") or "")
                    definition_id = (
                        f"enemy_move.{kit.get('fingerprint')}.{index}.{move_id}"
                    )
                    if not capability_eligible(state, {
                        "id": move_id,
                        "capability_id": move_id,
                        "definition_id": definition_id,
                        "name": move.get("name"),
                        "tags": [move.get("primitive"), move.get("basis")],
                    }, game_time=turn):
                        return (
                            "an active WorldOverlay makes this enemy capability pool ineligible"
                        )
            return None
        effective = (project_state_overlay(state, game_time=turn).get("effective") or {})
        if kind == "capability_assign":
            definition = op.get("definition") or {}
            subject = op.get("subject") or {}
            definition_id = str(definition.get("definition_id") or "")
            capability_id = str(definition.get("capability_id") or definition_id)
            if not capability_eligible(state, {
                "id": capability_id,
                "capability_id": capability_id,
                "definition_id": definition_id,
                "name": definition.get("name"),
                "tags": definition.get("concept_ids") or [],
                "owner_id": subject.get("id"),
            }, game_time=turn):
                return "an active WorldOverlay makes this capability ineligible"
        if kind in {"check", "ability_grant"}:
            values = [op.get("skill")] if kind == "check" else [op.get("ability")]
            if kind == "check":
                values.extend(op.get("use") or [])
            for value in values:
                capability_id = slug(str(value or ""))
                if capability_id and not capability_eligible(state, {
                    "id": capability_id,
                    "capability_id": capability_id,
                    "name": str(value or ""),
                }, game_time=turn):
                    return "an active WorldOverlay makes this capability use ineligible"
        if kind == "enemy_intent_set" and isinstance(op.get("_intent"), dict):
            intent = op["_intent"]
            actor = str(intent.get("actor") or "")
            combatant = (((state.get("combat") or {}).get("combatants") or {}).get(actor) or {})
            kit = op.get("_kit") if isinstance(op.get("_kit"), dict) else combatant.get("kit")
            move_id = str(intent.get("move_id") or "")
            moves = kit.get("moves") if isinstance(kit, dict) else []
            index = next((
                i for i, move in enumerate(moves or [])
                if isinstance(move, dict) and move.get("id") == move_id
            ), -1)
            definition_id = (
                f"enemy_move.{kit.get('fingerprint')}.{index}.{move_id}"
                if isinstance(kit, dict) and index >= 0 else ""
            )
            if move_id and not capability_eligible(state, {
                "id": move_id,
                "capability_id": move_id,
                "definition_id": definition_id,
                "name": intent.get("move_name"),
            }, game_time=turn):
                return "an active WorldOverlay makes this enemy capability use ineligible"
        if kind in {"quest_add", "quest_update"}:
            token = str(op.get("name") if kind == "quest_add" else op.get("quest") or "")
            qid = token if token in (state.get("quests") or {}) else slug(token)[:64]
            keys = {qid, "quest:" + qid}
            for key, fields in (effective.get("quest") or {}).items():
                row = (fields or {}).get("available") if isinstance(fields, dict) else None
                if str(key) in keys and isinstance(row, dict) and row.get("value") is False:
                    return "an active WorldOverlay makes this quest unavailable"
    except Exception:
        # A malformed or incompatible event view cannot silently grant eligibility. Historical
        # states without events keep their old behavior; a state claiming event authority closes.
        return "WorldOverlay could not be projected safely" if state.get("world_events") else None
    return None


def _nested_ref_exists(value: object, ref: str, fields: set[str]) -> bool:
    """Find an exact typed reference without treating arbitrary prose as evidence."""
    if isinstance(value, dict):
        if any(str(value.get(field) or "") == ref for field in fields):
            return True
        return any(_nested_ref_exists(child, ref, fields) for child in value.values())
    if isinstance(value, list):
        return any(_nested_ref_exists(child, ref, fields) for child in value)
    return False


def _world_event_lineage_violation(store, state: dict, event: dict) -> Optional[str]:
    lineage = event.get("worldlex_lineage")
    if not isinstance(lineage, dict):
        return None
    world_id = str(event.get("world_id") or "")
    definition_ref = lineage.get("definition_ref")
    if definition_ref is not None:
        try:
            definition = store.worldlex.get_by_fingerprint(str(definition_ref))
        except (TypeError, ValueError, WorldLexError):
            definition = None
        if not isinstance(definition, dict) or definition.get("world_id") != world_id:
            return "World Event Record has an unknown or cross-world WorldLex definition"

    assignment_ref = lineage.get("assignment_ref")
    if assignment_ref is not None and not _nested_ref_exists(
        state.get("capability_assignments") or {},
        str(assignment_ref),
        {"assignment_id", "assignment_ref"},
    ):
        return "World Event Record has an unknown WorldLex assignment reference"

    eligibility_ref = lineage.get("eligibility_ref")
    if eligibility_ref is not None and not _nested_ref_exists(
        state.get("combat") or {}, str(eligibility_ref), {"eligibility_ref"}
    ):
        return "World Event Record has an unknown WorldLex eligibility reference"

    event_effects = event.get("effects") or []
    adapter_ref = lineage.get("adapter_ref")
    if adapter_ref is not None and not (
        _nested_ref_exists(event_effects, str(adapter_ref), {"fingerprint", "adapter_ref"})
        or _nested_ref_exists(
            state.get("capability_assignments") or {},
            str(adapter_ref),
            {"adapter_ref", "adapter_fingerprint", "fingerprint"},
        )
    ):
        return "World Event Record has an unknown WorldLex adapter reference"

    receipt_ref = lineage.get("receipt_ref")
    if receipt_ref is not None and not (
        _nested_ref_exists(
            event_effects,
            str(receipt_ref),
            {"receipt_ref", "receipt_id", "application_fingerprint", "fingerprint"},
        )
        or _nested_ref_exists(
            state.get("combat") or {},
            str(receipt_ref),
            {"receipt_ref", "admission_ref", "receipt_id", "fingerprint"},
        )
    ):
        return "World Event Record has an unknown WorldLex receipt reference"
    return None


def _front_event_cause_violation(state: dict, event: dict, turn: int) -> Optional[str]:
    match = re.fullmatch(r"front:([^:]+):completion", str(event.get("cause_id") or ""))
    if match is None:
        return "rule World Event cause is not an approved code-owned producer"
    fid = match.group(1)
    front = (state.get("fronts") or {}).get(fid)
    if not isinstance(front, dict) or not front.get("done") \
            or int(front.get("filled_turn", -1)) != turn \
            or int(front.get("filled", 0)) < max(1, int(front.get("segments", 6))):
        return "front World Event cause is missing its exact current-turn completion"
    expected = _front_completion_event_records(
        state,
        front_id=fid,
        world_id=str(event.get("world_id") or ""),
        session_id=str(event.get("session_id") or ""),
        branch_id=str(event.get("branch_id") or ""),
        turn=turn,
    )
    if event not in expected:
        return "front World Event differs from its deterministic completion projection"
    return None


def _world_event_existing_actor_violation(state: dict, event: dict) -> Optional[str]:
    """Require exact actor effects to name a subject that exists at admission.

    Selector subjects are the typed template for future actors.  An exact actor/npc/enemy
    reference must never remain dormant and start affecting a later entity that happens to reuse
    its id.
    """
    if event.get("kind") != "admission" \
            or event.get("propagation") not in {"existing_subjects", "existing_and_future"}:
        return None
    entities = state.get("entities") or {}
    combatants = ((state.get("combat") or {}).get("combatants") or {})
    compatible_kinds = {
        "actor": {"actor", "character", "player", "npc", "enemy"},
        "npc": {"npc", "character"},
        "enemy": {"enemy"},
    }
    for effect in event.get("effects") or []:
        if not isinstance(effect, dict) or effect.get("supported") is not True:
            continue
        subject = effect.get("subject")
        if not isinstance(subject, dict):
            continue
        subject_kind = str(subject.get("kind") or "")
        if subject_kind not in compatible_kinds:
            continue
        subject_id = str(subject.get("id") or "")
        entity = entities.get(subject_id)
        entity_kind = str(entity.get("kind") or "") if isinstance(entity, dict) else ""
        exists = entity_kind in compatible_kinds[subject_kind]
        if subject_kind == "enemy" and subject_id in combatants:
            exists = True
        if not exists:
            return (
                "World Event exact existing actor subject does not exist at admission: "
                f"{subject_kind}:{subject_id}"
            )
    return None


def _world_event_cause_violation(
    store, state: dict, event: dict, source: str, cfg, turn: int,
) -> Optional[str]:
    """Resolve one fresh event cause against privileged or code-owned receipts."""
    if event.get("schema") != "aetherstate-world-event-record/2":
        return "historical World Event records are replay-only"
    if event.get("turn") != turn:
        return "World Event Record turn is stale or forged"
    if int(event.get("game_time", -1)) != turn:
        return "World Event Record game time is stale or forged"

    authority = str(event.get("cause_authority") or "")
    exact_source = {"creator": "user", "genesis": "genesis"}.get(authority)
    if exact_source is not None:
        if source != exact_source:
            return f"{authority} World Event cause does not match its privileged ingress"
        if not str(event.get("cause_id") or "").startswith(authority + ":"):
            return f"{authority} World Event cause lacks its exact privileged identity"
    elif authority == "rule":
        if source != "rule":
            return "rule World Event cause does not match its code ingress"
        problem = _front_event_cause_violation(state, event, turn)
        if problem is not None:
            return problem
    elif authority == "mechanic_settlement":
        if source != "rule":
            return "mechanic World Event cause is rule-only"
        ref = str(event.get("settlement_ref") or "")
        public, private = _state_settlement_pair(state, ref, turn)
        try:
            from .mechanic_settlement import validate_mechanic_settlement

            receipt = validate_mechanic_settlement(
                public.get("receipt") if isinstance(public, dict) else None
            )
        except (TypeError, ValueError):
            return "mechanic World Event cause has no exact current-turn settlement receipt"
        if not isinstance(private, dict) or private.get("settlement_ref") != ref \
                or receipt.get("settlement_ref") != ref \
                or receipt.get("frame_ref") != event.get("semantic_frame_ref"):
            return "mechanic World Event cause conflicts with its settlement receipt"
    elif authority == "semantic_transition_truth":
        spec = getattr(cfg, "specialization", None)
        if source != "rule" or spec is None or spec.name != "rpg" \
                or not getattr(spec, "semantic_truth_gate", False):
            return "Semantic Transition Truth event admission is disabled"
        # The current lifecycle proof is committed only after reducer publication.  Accepting it
        # here would require a second, non-atomic journal row, so this route remains fail-closed
        # until the event and transition entry share one transaction.
        return "Semantic Transition Truth event lacks an atomically committed transition entry"
    else:
        return "World Event Record cause authority is unsupported"
    lineage_violation = _world_event_lineage_violation(store, state, event)
    if lineage_violation is not None:
        return lineage_violation
    return _world_event_existing_actor_violation(state, event)


def _claim_lineage_violation(state: dict, frame: dict, turn: int) -> Optional[str]:
    """Bind a fresh ClaimFrame to the exact meaning receipt committed for this turn."""
    receipts = [
        row.get("meaning") for row in state.get("semantic_meanings") or []
        if isinstance(row, dict) and row.get("turn") == turn and isinstance(row.get("meaning"), dict)
    ]
    matching = next((
        receipt for receipt in receipts
        if receipt.get("source_fingerprint") == frame.get("source_fingerprint")
        and receipt.get("fabric_fingerprint") == frame.get("fabric_fingerprint")
    ), None)
    if matching is None:
        return "Claim Record lacks its exact current-turn semantic meaning receipt"
    match = next((
        row for row in matching.get("matches") or []
        if isinstance(row, dict)
        and row.get("lex_id") == "claim"
        and row.get("entry_fingerprint") == frame.get("meaning_ref")
        and row.get("concept_id") == frame.get("claim_concept_id")
        and row.get("start") == (frame.get("governor_span") or {}).get("start")
        and row.get("end") == (frame.get("governor_span") or {}).get("end")
    ), None)
    if match is None:
        return "Claim Record governor is stale, forged, or absent from its meaning receipt"
    receipt_ambiguity = sorted({
        str(value) for value in match.get("ambiguity") or []
        if isinstance(value, str) and value
    })
    frame_ambiguity = sorted({
        str(value) for value in frame.get("ambiguity") or []
        if isinstance(value, str) and value
    })
    if frame_ambiguity != receipt_ambiguity:
        return "Claim Record ambiguity differs from its exact meaning receipt"
    if receipt_ambiguity:
        return "Claim Record governor is ambiguous and cannot bind a durable occurrence"
    receipt_anchors = sorted({
        str(value) for value in match.get("source_ids") or []
        if isinstance(value, str) and value.startswith("playerlex.")
    })
    frame_anchors = sorted({
        str(value) for value in frame.get("playerlex_anchor_refs") or []
        if isinstance(value, str)
    })
    if frame_anchors != receipt_anchors:
        return "Claim Record PlayerLex anchor is stale, forged, or absent from its meaning receipt"
    return None


def _settlement_fingerprint(value: object) -> str:
    from .capability_glossary import content_fingerprint

    return content_fingerprint(value)


def _settlement_request_fingerprint(op: dict) -> str:
    return _settlement_fingerprint({
        "schema": "mechanic-settlement-runtime-request/1",
        "contract_id": op.get("contract_id"),
        "settlement_ref": op.get("settlement_ref"),
        "frame_ref": op.get("frame_ref"),
        "members": op.get("members"),
    })


def _settlement_target_cid(state: dict, target_entity_id: object) -> Optional[str]:
    """Resolve only the exact frame target, including a combat row mapped through ``eid``."""
    target = str(target_entity_id or "")
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not isinstance(rows, dict) or not target:
        return None
    direct = rows.get(target)
    if isinstance(direct, dict) and not direct.get("defeated"):
        return target
    hits = [
        str(cid) for cid, row in rows.items()
        if isinstance(row, dict) and not row.get("defeated")
        and str(row.get("eid") or row.get("id") or "") == target
    ]
    return hits[0] if len(hits) == 1 else None


def _settlement_projection_rows(turn: int, settlement_ref: str,
                                request_fingerprint: str,
                                members: list[dict]) -> dict:
    projections = []
    for index, member in enumerate(members):
        projections.append({
            **deepcopy(member),
            "_settlement_ref": settlement_ref,
            "_settlement_member_index": index,
        })
    return {
        "turn": int(turn),
        "settlement_ref": settlement_ref,
        "request_fingerprint": request_fingerprint,
        "members": projections,
    }


def _apply_mechanic_projection(state: dict, op: dict, turn: int) -> None:
    """Verify one visible child projection; the settlement envelope already mutated state."""
    ref = str(op.get("_settlement_ref") or "")
    index = op.get("_settlement_member_index")
    rows = state.get("_mechanic_settlement_projection_members")
    ledger = next((
        row for row in reversed(rows or [])
        if isinstance(row, dict) and row.get("turn") == turn
        and row.get("settlement_ref") == ref
    ), None)
    if ledger is None:
        raise OpReject("mechanic projection has no same-turn committed settlement")
    members = ledger.get("members")
    if isinstance(index, bool) or not isinstance(index, int) \
            or not isinstance(members, list) or not 0 <= index < len(members):
        raise OpReject("mechanic projection member index is invalid")
    if op != members[index]:
        raise OpReject("mechanic projection payload differs from its settled member")


def _settlement_opening_requirements(state: dict, frame: dict) -> dict[str, bool]:
    return {
        "target_admission": _settlement_target_cid(
            state, frame.get("target_entity_id")
        ) is None,
        # An already-active combat is mechanically open even when an older checkpoint has no
        # combat scene phase. Tier0 emits no synthetic scene_set in that case, so Store must
        # judge the same pre-state fact rather than demand a projection that never belonged.
        "scene_transition": not combat_active(state)
        and str((state.get("scene") or {}).get("phase") or "").lower() not in _COMBAT_PHASES,
    }


def _settlement_effect_row(state: dict, member: dict) -> tuple[str, Optional[dict]]:
    subject = resolve_entity_ref(state, member.get("char")) or str(member.get("char") or "")
    rows = (state.get("effects") or {}).get(subject) or {}
    effect_id = str(member.get("_eff_id") or "")
    if not effect_id:
        effect_id = _resolve_effect_key(rows, member.get("effect")) or slug(member.get("effect"))
    row = rows.get(effect_id) if isinstance(rows, dict) else None
    return effect_id, deepcopy(row) if isinstance(row, dict) else None


def _settlement_player_pool(state: dict, actor: str, resource_id: str) -> Optional[dict]:
    player = (state.get("player") or {}).get(actor)
    if not isinstance(player, dict):
        return None
    if resource_id == "hp":
        pool = player.get("hp")
    else:
        pool = (player.get("resources") or {}).get(resource_id)
    return pool if isinstance(pool, dict) else None


def _settlement_check_rows(
    before: dict,
    after: dict,
    member: dict,
    frame: dict,
) -> list[dict]:
    """Derive one check and every reducer-owned cost/cooldown transition."""
    actor = str(frame["actor_id"])
    capability = str(frame["capability_id"])
    base = {"frame_ref": frame["fingerprint"], "meaning_ref": frame["meaning_ref"]}
    rows = [{
        "kind": "check",
        **base,
        "actor_id": actor,
        "capability_id": capability,
        "target_entity_id": frame.get("target_entity_id"),
        "result": int(member["result"]),
        "outcome_quality": str(member["tier"]),
    }]
    costs = member.get("_cost") or {}
    if not isinstance(costs, dict):
        raise OpReject("settled check cost receipt is malformed")
    for resource_id in sorted(costs):
        amount = costs[resource_id]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise OpReject("settled check cost is not a positive exact spend")
        pre_pool = _settlement_player_pool(before, actor, str(resource_id))
        post_pool = _settlement_player_pool(after, actor, str(resource_id))
        if not isinstance(pre_pool, dict) or not isinstance(post_pool, dict):
            raise OpReject("settled check cost names an undeclared resource")
        pre = int(pre_pool.get("cur", 0))
        post = int(post_pool.get("cur", 0))
        maximum = int(post_pool.get("max", 0))
        if post != pre - amount:
            raise OpReject("settled check did not pay its complete resource cost")
        rows.append({
            "kind": "cost", **base, "subject_id": actor,
            "resource_id": str(resource_id), "pre": pre,
            "delta": post - pre, "post": post, "maximum": maximum,
        })
    cooldowns = member.get("_ability_cd") or {}
    if not isinstance(cooldowns, dict):
        raise OpReject("settled check cooldown receipt is malformed")
    before_player = (before.get("player") or {}).get(actor) or {}
    after_player = (after.get("player") or {}).get(actor) or {}
    for ability_id in sorted(cooldowns):
        pre = int((before_player.get("ability_cd") or {}).get(ability_id, 0))
        post = int((after_player.get("ability_cd") or {}).get(ability_id, 0))
        if post <= pre or post != int(cooldowns[ability_id]):
            raise OpReject("settled check did not commit its exact cooldown")
        rows.append({
            "kind": "cooldown", **base, "subject_id": actor,
            "ability_id": str(ability_id), "pre": pre, "post": post,
        })
    return rows


def _settlement_mastery_row(
    before: dict,
    after: dict,
    frame: dict,
) -> Optional[dict]:
    actor = str(frame["actor_id"])
    capability = str(frame["capability_id"])
    before_player = (before.get("player") or {}).get(actor) or {}
    after_player = (after.get("player") or {}).get(actor) or {}
    pre = int((before_player.get("mastery") or {}).get(capability, 0))
    post = int((after_player.get("mastery") or {}).get(capability, 0))
    if post <= pre:
        return None
    return {
        "kind": "mastery",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": actor,
        "capability_id": capability,
        "pre": pre,
        "delta": post - pre,
        "post": post,
    }


def _settlement_consequence_row(
    before: dict,
    after: dict,
    member: dict,
    frame: dict,
) -> dict:
    actor = str(frame["actor_id"])
    effect_id, pre_effect = _settlement_effect_row(before, member)
    after_id, post_effect = _settlement_effect_row(after, member)
    if effect_id != after_id or post_effect is None:
        raise OpReject("settled critical-failure consequence did not commit")
    return {
        "kind": "consequence",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "subject_id": actor,
        "effect_id": effect_id,
        "pre_state_ref": (
            _settlement_fingerprint(pre_effect) if pre_effect is not None else None
        ),
        "post_state_ref": _settlement_fingerprint(post_effect),
    }


def _settlement_group_after_apply(state: dict, op: dict, turn: int) \
        -> tuple[dict, list[dict], dict, dict]:
    """Replay a baked group on a clone and derive its exact post-reducer public receipt."""
    from .mechanic_settlement import (
        MechanicSettlementError,
        build_weapon_attack_settlement,
        weapon_attack_settlement_ref,
    )

    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict) or frame.get("schema") != "semantic-action-frame/3" \
            or frame.get("action_class") != "weapon_attack":
        raise OpReject("mechanic settlement requires one candidate V3 weapon-attack frame")
    binding = _semantic_binding_by_ref(state, frame.get("meaning_binding_ref"), turn)
    try:
        expected_ref = weapon_attack_settlement_ref(frame, binding)
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    if op.get("frame_ref") != frame.get("fingerprint") \
            or op.get("settlement_ref") != expected_ref:
        raise OpReject("mechanic settlement identity does not match its semantic event")

    members = op.get("members")
    if not isinstance(members, list):
        raise OpReject("prepared mechanic settlement has no member group")
    opening = _settlement_opening_requirements(state, frame)
    spawn_count = sum(member.get("op") == "combatant_spawn" for member in members)
    scene_count = sum(member.get("op") == "scene_set" for member in members)
    valid_spawn_count = (
        1 <= spawn_count <= COMBAT_SIDE_CAP
        if opening["target_admission"]
        else spawn_count == 0
    )
    if not valid_spawn_count or scene_count != int(opening["scene_transition"]):
        raise OpReject("mechanic opening members do not match the pre-state requirements")

    target = str(frame["target_entity_id"])
    clone = deepcopy(state)
    group: list[dict] = []
    for member in members:
        kind = str(member.get("op") or "")
        if member.get("_turn") != turn or validate_op(member) is None:
            raise OpReject("mechanic settlement contains a malformed baked member")
        before = deepcopy(clone)
        try:
            _apply_op(clone, member)
        except OpReject:
            raise
        except Exception as exc:
            raise OpReject(f"mechanic member failed: {type(exc).__name__}") from exc

        base = {"frame_ref": frame["fingerprint"], "meaning_ref": frame["meaning_ref"]}
        if kind == "check":
            group.extend(_settlement_check_rows(before, clone, member, frame))
        elif kind == "combatant_spawn":
            cid = str(member.get("_cid") or "")
            before_rows = ((before.get("combat") or {}).get("combatants") or {})
            after_rows = ((clone.get("combat") or {}).get("combatants") or {})
            target_row = after_rows.get(cid)
            if not cid or cid in before_rows or not isinstance(target_row, dict):
                raise OpReject("weapon opening did not admit one exact combatant")
            member_entity = str(member.get("char") or cid)
            primary = target in {cid, str(member.get("char") or "")}
            admission_entity = target if primary else member_entity
            admission_combatant = target if primary else cid
            admission_state = {
                "combat": clone.get("combat"),
                "target": target_row,
                "capability_assignments": clone.get("capability_assignments"),
            }
            group.append({
                "kind": "target_admission", **base,
                "target_entity_id": admission_entity,
                "combatant_id": admission_combatant,
                "pre_state_ref": None,
                "post_state_ref": _settlement_fingerprint(admission_state),
            })
        elif kind == "scene_set":
            group.append({
                "kind": "scene_transition", **base, "subject_id": "scene",
                "pre_state_ref": _settlement_fingerprint(before.get("scene") or {}),
                "post_state_ref": _settlement_fingerprint(clone.get("scene") or {}),
            })
        elif kind == "combatant_hp":
            pre_cid = _settlement_target_cid(before, target)
            post_cid = _settlement_target_cid(clone, target)
            if pre_cid is None or post_cid != pre_cid:
                raise OpReject("weapon strike target diverged from its semantic frame")
            pre_row = ((before.get("combat") or {}).get("combatants") or {})[pre_cid]
            post_row = ((clone.get("combat") or {}).get("combatants") or {})[post_cid]
            pre = int((pre_row.get("hp") or {}).get("cur", 0))
            post = int((post_row.get("hp") or {}).get("cur", 0))
            maximum = int((post_row.get("hp") or {}).get("max", 0))
            group.append({
                "kind": "hp", **base, "subject_id": target,
                "pre": pre, "delta": post - pre, "post": post, "maximum": maximum,
            })
        elif kind == "master_tick":
            if mastery := _settlement_mastery_row(before, clone, frame):
                group.append(mastery)
        elif kind == "effect_add":
            group.append(_settlement_consequence_row(before, clone, member, frame))

    cid = _settlement_target_cid(clone, target)
    if cid is None:
        raise OpReject("settled weapon target is not present after the reducer group")
    target_row = ((clone.get("combat") or {}).get("combatants") or {}).get(cid) or {}
    hp = target_row.get("hp") or {}
    try:
        receipt, store_row = build_weapon_attack_settlement(
            frame,
            binding,
            accepted_group=group,
            target_post_state={
                "combatant_id": target,
                "hp": {"cur": int(hp.get("cur", 0)), "max": int(hp.get("max", 0))},
            },
            opening_requirements=opening,
        )
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    return clone, group, receipt, store_row


def _combat_opening_group_after_apply(state: dict, op: dict, turn: int) \
        -> tuple[dict, list[dict], dict, dict]:
    """Replay one exact combat opening and seal every admission plus its scene floor."""
    from .mechanic_settlement import (
        MechanicSettlementError,
        build_combat_opening_settlement,
        combat_opening_settlement_ref,
    )

    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict) or frame.get("schema") != "semantic-action-frame/3" \
            or frame.get("action_class") != "combat_opening":
        raise OpReject("combat opening settlement requires one candidate V3 opening frame")
    binding = _semantic_binding_by_ref(state, frame.get("meaning_binding_ref"), turn)
    try:
        expected_ref = combat_opening_settlement_ref(frame, binding)
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    if op.get("frame_ref") != frame.get("fingerprint") \
            or op.get("settlement_ref") != expected_ref:
        raise OpReject("combat opening settlement identity does not match its semantic event")

    members = op.get("members")
    if not isinstance(members, list):
        raise OpReject("prepared combat opening settlement has no member group")
    opening = _settlement_opening_requirements(state, frame)
    spawn_count = sum(member.get("op") == "combatant_spawn" for member in members)
    scene_count = sum(member.get("op") == "scene_set" for member in members)
    if not opening["target_admission"] or not 1 <= spawn_count <= COMBAT_SIDE_CAP \
            or scene_count != int(opening["scene_transition"]):
        raise OpReject("combat opening members do not match the exact pre-state requirements")

    target = str(frame["target_entity_id"])
    clone = deepcopy(state)
    group: list[dict] = []
    for member in members:
        kind = str(member.get("op") or "")
        if member.get("_turn") != turn or validate_op(member) is None:
            raise OpReject("combat opening settlement contains a malformed baked member")
        before = deepcopy(clone)
        try:
            _apply_op(clone, member)
        except OpReject:
            raise
        except Exception as exc:
            raise OpReject(f"combat opening member failed: {type(exc).__name__}") from exc

        base = {"frame_ref": frame["fingerprint"], "meaning_ref": frame["meaning_ref"]}
        if kind == "combatant_spawn":
            cid = str(member.get("_cid") or "")
            before_rows = ((before.get("combat") or {}).get("combatants") or {})
            after_rows = ((clone.get("combat") or {}).get("combatants") or {})
            target_row = after_rows.get(cid)
            if not cid or cid in before_rows or not isinstance(target_row, dict):
                raise OpReject("combat opening did not admit one exact combatant")
            member_entity = str(member.get("char") or cid)
            primary = target in {cid, str(member.get("char") or "")}
            admission_entity = target if primary else member_entity
            admission_combatant = target if primary else cid
            admission_state = {
                "combat": clone.get("combat"),
                "target": target_row,
                "capability_assignments": clone.get("capability_assignments"),
            }
            group.append({
                "kind": "target_admission", **base,
                "target_entity_id": admission_entity,
                "combatant_id": admission_combatant,
                "pre_state_ref": None,
                "post_state_ref": _settlement_fingerprint(admission_state),
            })
        elif kind == "scene_set":
            group.append({
                "kind": "scene_transition", **base, "subject_id": "scene",
                "pre_state_ref": _settlement_fingerprint(before.get("scene") or {}),
                "post_state_ref": _settlement_fingerprint(clone.get("scene") or {}),
            })
        else:
            raise OpReject("combat opening settlement contains a non-opening member")

    cid = _settlement_target_cid(clone, target)
    if cid is None:
        raise OpReject("combat opening primary target is absent after the reducer group")
    target_row = ((clone.get("combat") or {}).get("combatants") or {}).get(cid) or {}
    hp = target_row.get("hp") or {}
    try:
        receipt, store_row = build_combat_opening_settlement(
            frame,
            binding,
            accepted_group=group,
            target_post_state={
                "combatant_id": target,
                "hp": {"cur": int(hp.get("cur", 0)), "max": int(hp.get("max", 0))},
            },
            opening_requirements=opening,
        )
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    return clone, group, receipt, store_row


def _skill_check_group_after_apply(state: dict, op: dict, turn: int) \
        -> tuple[dict, list[dict], dict, dict]:
    """Replay one exact non-impact check group and derive its post-reducer receipt."""
    from .mechanic_settlement import (
        MechanicSettlementError,
        NON_SKILL_CHECK_ACTION_CLASSES,
        build_skill_check_settlement,
        skill_check_settlement_ref,
    )

    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict) or frame.get("schema") != "semantic-action-frame/3" \
            or frame.get("action_class") in NON_SKILL_CHECK_ACTION_CLASSES:
        raise OpReject("skill settlement requires one candidate V3 non-impact check frame")
    binding = _semantic_binding_by_ref(state, frame.get("meaning_binding_ref"), turn)
    try:
        expected_ref = skill_check_settlement_ref(frame, binding)
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    if op.get("frame_ref") != frame.get("fingerprint") \
            or op.get("settlement_ref") != expected_ref:
        raise OpReject("skill settlement identity does not match its semantic event")

    members = op.get("members")
    if not isinstance(members, list):
        raise OpReject("prepared skill settlement has no member group")
    clone = deepcopy(state)
    group: list[dict] = []
    for member in members:
        kind = str(member.get("op") or "")
        if member.get("_turn") != turn or validate_op(member) is None:
            raise OpReject("skill settlement contains a malformed baked member")
        before = deepcopy(clone)
        try:
            _apply_op(clone, member)
        except OpReject:
            raise
        except Exception as exc:
            raise OpReject(f"skill settlement member failed: {type(exc).__name__}") from exc
        if kind == "check":
            group.extend(_settlement_check_rows(before, clone, member, frame))
        elif kind == "master_tick":
            if mastery := _settlement_mastery_row(before, clone, frame):
                group.append(mastery)
        elif kind == "effect_add":
            group.append(_settlement_consequence_row(before, clone, member, frame))
        else:
            raise OpReject("skill settlement contains an impact member")
    try:
        receipt, store_row = build_skill_check_settlement(
            frame,
            binding,
            accepted_group=group,
        )
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    return clone, group, receipt, store_row


def _settlement_group_after_apply_for_contract(state: dict, op: dict, turn: int) \
        -> tuple[dict, list[dict], dict, dict]:
    from .mechanic_settlement import (
        COMBAT_OPENING_CONTRACT,
        SKILL_CHECK_CONTRACT,
        WEAPON_ATTACK_CONTRACT,
    )

    if op.get("contract_id") == WEAPON_ATTACK_CONTRACT:
        return _settlement_group_after_apply(state, op, turn)
    if op.get("contract_id") == SKILL_CHECK_CONTRACT:
        return _skill_check_group_after_apply(state, op, turn)
    if op.get("contract_id") == COMBAT_OPENING_CONTRACT:
        return _combat_opening_group_after_apply(state, op, turn)
    raise OpReject("unsupported mechanic settlement contract")


def _apply_prepared_mechanic_settlement(state: dict, op: dict, turn: int) -> None:
    """Replay and verify one immutable whole-mechanic journal envelope."""
    from .mechanic_settlement import (
        MechanicSettlementError,
        validate_mechanic_settlement,
        validate_mechanic_settlement_row,
    )

    if op.get("_settlement_prepared") is not True:
        raise OpReject("mechanic settlement was not prepared by the live authority boundary")
    try:
        baked_receipt = validate_mechanic_settlement(op.get("receipt"))
        baked_store_row = validate_mechanic_settlement_row(op.get("_store_row"))
    except MechanicSettlementError as exc:
        raise OpReject(str(exc)) from exc
    request_fingerprint = str(op.get("_request_fingerprint") or "")
    if _SEMANTIC_FRAME_REF_RE.fullmatch(request_fingerprint) is None:
        raise OpReject("mechanic settlement request fingerprint is malformed")

    public_rows = state.get("mechanic_settlements") or []
    private_rows = state.get("_mechanic_settlement_projection_members") or []
    prior_public = next((
        row for row in reversed(public_rows)
        if isinstance(row, dict) and row.get("turn") == turn
        and isinstance(row.get("receipt"), dict)
        and row["receipt"].get("settlement_ref") == op.get("settlement_ref")
    ), None)
    prior_private = next((
        row for row in reversed(private_rows)
        if isinstance(row, dict) and row.get("turn") == turn
        and row.get("settlement_ref") == op.get("settlement_ref")
    ), None)
    if (prior_public is None) != (prior_private is None):
        raise OpReject("mechanic settlement public and projection ledgers diverged")
    if prior_public is not None:
        expected_private = _settlement_projection_rows(
            turn, str(op["settlement_ref"]), request_fingerprint, list(op["members"])
        )
        if prior_public != {"turn": turn, "receipt": baked_receipt} \
                or prior_private != expected_private:
            raise OpReject("mechanic settlement identity conflicts with prior state")
        return

    clone, _group, receipt, store_row = _settlement_group_after_apply_for_contract(
        state, op, turn,
    )
    if receipt != baked_receipt or store_row != baked_store_row:
        raise OpReject("replayed mechanic settlement differs from its immutable receipt")
    clone.setdefault("mechanic_settlements", []).append({"turn": turn, "receipt": receipt})
    _trim_receipt_ledger(clone["mechanic_settlements"], turn)
    private = _settlement_projection_rows(
        turn, str(op["settlement_ref"]), request_fingerprint, list(op["members"])
    )
    clone.setdefault("_mechanic_settlement_projection_members", []).append(private)
    _trim_receipt_ledger(clone["_mechanic_settlement_projection_members"], turn)
    state.clear()
    state.update(clone)


def _materialize_settlement_spawn(store, op: dict, state: dict, turn: int) -> dict:
    """Apply the same WorldLex pool authority used by an ordinary live enemy spawn."""
    if op.get("op") != "combatant_spawn" or op.get("side") != "enemy" \
            or not isinstance(op.get("_kit"), dict):
        return op
    world_id = str((state.get("world_identity") or {}).get("world_id") or "")
    if not world_id:
        return op
    bundle, assignments, reconstructed = _materialize_enemy_capability_pool(
        store,
        kit=op["_kit"],
        world_id=world_id,
        subject_id=_enemy_pool_subject_id(op["_cid"]),
        turn=turn,
    )
    if reconstructed != op["_kit"]:
        raise EnemyCapabilityPoolError("enemy pool activation parity failed")
    out = dict(op)
    out["_capability_pool"] = bundle
    out["_capability_assignments"] = assignments
    out["_kit"] = reconstructed
    out["_kit_source"] = "worldlex-runtime-pool"
    if isinstance(out.get("_initial_intent"), dict):
        peid = _player_target_eid(state) or "player"
        pname = str((((state.get("entities") or {}).get(peid) or {}).get("name")) or peid)
        pool_intent = select_enemy_intent(
            {"id": out["_cid"], "name": str(out.get("name") or out["_cid"]),
             "kit": reconstructed},
            turn,
            peid,
            pname,
        )
        if pool_intent != out["_initial_intent"]:
            raise EnemyCapabilityPoolError("WorldLex pool changed the prepared enemy intent")
        out["_initial_intent"] = pool_intent
    return out


def _expected_weapon_check_policy(state: dict, check: dict, turn: int, cfg) \
        -> tuple[dict[str, int], dict[str, int]]:
    """Recompute frozen skill cost and every auditable activated-ability cost/cooldown."""
    from . import registry

    actor = str(check.get("char") or "")
    player = (state.get("player") or {}).get(actor)
    if not isinstance(player, dict):
        raise OpReject("weapon check has no frozen Player Card")
    reg = registry.load(cfg)
    skill_id = str(check.get("skill") or "")
    resources = player.get("resources") or {}

    def _pool(resource_id: str) -> object:
        return player.get("hp") if resource_id == "hp" else resources.get(resource_id)

    expected_cost: dict[str, int] = {}
    admission_cost: dict[str, int] = {}
    for resource_id, amount in registry.skill_cost(reg.skill_entry(skill_id, player)).items():
        if isinstance(_pool(resource_id), dict):
            admission_cost[resource_id] = int(amount)
            expected_cost[resource_id] = (
                max(1, (int(amount) + 1) // 2)
                if check.get("tier") == "fail" else int(amount)
            )

    shape = check.get("_shape") or {}
    if not isinstance(shape, dict) or shape.get("schema") != CHECK_ROLL_SHAPE_SCHEMA:
        raise OpReject("weapon check lacks its versioned roll-shape receipt")
    executed_active_ids = shape.get("executed_active_ids")
    if not isinstance(executed_active_ids, list):
        raise OpReject("weapon check executed ability IDs are malformed")
    known = reg.known_abilities(player)
    expected_cd: dict[str, int] = {}
    for ability_id in executed_active_ids:
        definition = known.get(str(ability_id))
        if not isinstance(definition, dict) or not registry.ability_is_active(definition) \
                or not registry.ability_applies(definition, skill_id):
            raise OpReject("weapon check executed ability differs from frozen policy")
        ready_turn = int((player.get("ability_cd") or {}).get(str(ability_id), 0) or 0)
        if ready_turn > int(turn):
            raise OpReject("weapon check executed an ability that is still cooling down")
        for resource_id, amount in registry.skill_cost(definition).items():
            if isinstance(_pool(resource_id), dict):
                admission_cost[resource_id] = admission_cost.get(resource_id, 0) + int(amount)
                expected_cost[resource_id] = expected_cost.get(resource_id, 0) + int(amount)
        cooldown = int((definition or {}).get("cooldown_turns", 0) or 0)
        if cooldown > 0:
            expected_cd[str(ability_id)] = int(turn) + cooldown
    # Tier-0 reserves the full skill price before it admits any activated ability.  A later failed
    # roll may reduce the settled skill charge, but that reduction cannot retroactively make an
    # otherwise unaffordable ability executable.
    for resource_id, amount in admission_cost.items():
        pool = _pool(resource_id)
        if not isinstance(pool, dict) or int(pool.get("cur", 0) or 0) < int(amount):
            raise OpReject("weapon check cannot afford its frozen capability cost")
    return dict(sorted(expected_cost.items())), dict(sorted(expected_cd.items()))


def _validate_weapon_check_resolution(state: dict, check: dict, turn: int, cfg,
                                      frame: dict) -> None:
    """Recompute the qualitative check from frozen dice, card, ability, and scope policy."""
    from . import registry

    actor = str(check.get("char") or "")
    player = (state.get("player") or {}).get(actor)
    if not isinstance(player, dict):
        raise OpReject("weapon check has no frozen Player Card")
    reg = registry.load(cfg)
    dice = registry.dice_spec(reg, cfg)
    parsed = registry.parse_dice(dice)
    if parsed is None or check.get("_dice") != dice:
        raise OpReject("weapon check dice differ from the frozen resolution policy")
    n_keep, sides, flat = parsed
    seed = check.get("_seed")
    if not isinstance(seed, list) or len(seed) != n_keep \
            or any(isinstance(value, bool) or not isinstance(value, int)
                   or not 1 <= value <= sides for value in seed):
        raise OpReject("weapon check lacks its exact final kept dice")

    skill_id = str(check.get("skill") or "")
    declared_mod = check.get("_declared_mod")
    if isinstance(declared_mod, bool) or not isinstance(declared_mod, int):
        raise OpReject("weapon check declared modifier receipt is malformed")
    if declared_mod != frame.get("declared_modifier"):
        raise OpReject("weapon check declared modifier differs from its semantic frame")
    expected_mod = reg.effective_mod(player, skill_id) + int(flat) + declared_mod
    expected_mod += registry.gear_skill_mod(state, actor, skill_id)
    expected_mod += registry.effect_skill_mod(state, actor, skill_id, turn)

    shape = check.get("_shape")
    shape_fields = {
        "schema", "abilities", "applied_passive_ids", "executed_active_ids",
        "base_pool", "on_fail", "fired", "improved", "pool", "kept",
        "edge", "ward", "surge", "burst",
    }
    if not isinstance(shape, dict) or set(shape) != shape_fields \
            or shape.get("schema") != CHECK_ROLL_SHAPE_SCHEMA:
        raise OpReject("weapon check roll-shape receipt is malformed")

    def _canonical_ids(value: object, label: str) -> list[str]:
        if not isinstance(value, list) or value != sorted(set(value)) \
                or any(not isinstance(item, str) or not item for item in value):
            raise OpReject(f"weapon check {label} are not canonical")
        return list(value)

    passive_ids = _canonical_ids(shape.get("applied_passive_ids"), "passive ability IDs")
    active_ids = _canonical_ids(shape.get("executed_active_ids"), "active ability IDs")
    invoked_ids = frame.get("invoked_capability_ids") or []
    if not isinstance(invoked_ids, list) or not set(active_ids) <= set(invoked_ids):
        raise OpReject("weapon check active abilities differ from its semantic invocation frame")

    ward = 0
    edge = 0
    surge_lift = 0
    burst = 0
    surge = 0
    known = reg.known_abilities(player)
    required_passive_ids: list[str] = []
    for ability_id, definition in known.items():
        if not registry.ability_applies(definition, skill_id):
            continue
        mechanic = registry.ability_mechanic(definition)
        if not registry.ability_is_active(definition) and mechanic in {"edge", "ward"}:
            required_passive_ids.append(str(ability_id))
    if passive_ids != sorted(required_passive_ids):
        raise OpReject("weapon check omitted or transplanted a frozen passive ability")

    applied_labels: list[str] = []
    for ability_id in passive_ids:
        definition = known.get(ability_id)
        if not isinstance(definition, dict) or registry.ability_is_active(definition) \
                or not registry.ability_applies(definition, skill_id):
            raise OpReject("weapon check passive ability differs from frozen policy")
        mechanic = registry.ability_mechanic(definition)
        magnitude = max(1, registry.ability_magnitude(definition, 1))
        if mechanic == "edge":
            edge += magnitude
        elif mechanic == "ward":
            ward = max(ward, magnitude)
        else:
            raise OpReject("weapon check names a non-shaping passive ability")
        applied_labels.append(str(definition.get("name", ability_id)))

    on_fail_ids: list[str] = []
    for ability_id in active_ids:
        definition = known.get(ability_id)
        if not isinstance(definition, dict) or not registry.ability_is_active(definition) \
                or not registry.ability_applies(definition, skill_id):
            raise OpReject("weapon check active ability differs from frozen policy")
        mechanic = registry.ability_mechanic(definition)
        magnitude = max(1, registry.ability_magnitude(definition, 1))
        if mechanic == "surge":
            surge += max(1, registry.ability_magnitude(definition, 2))
            surge_lift += 1
        elif mechanic == "mod":
            burst += magnitude
        elif mechanic == "edge":
            edge += magnitude
        elif mechanic == "ward":
            ward = max(ward, magnitude)
        elif mechanic in registry.ON_FAIL_MECHANICS:
            on_fail_ids.append(ability_id)
        else:
            raise OpReject("weapon check names a non-executable active ability")
        applied_labels.append(str(definition.get("name", ability_id)))
    if len(on_fail_ids) > 1:
        raise OpReject("weapon check executes more than one on-fail ability")

    expected_mod += surge + burst
    if any(isinstance(shape.get(field), bool) or not isinstance(shape.get(field), int)
           for field in ("ward", "edge", "surge", "burst")) \
            or shape["ward"] != ward or shape["edge"] != edge \
            or shape["surge"] != surge or shape["burst"] != burst:
        raise OpReject("weapon check ability shaping differs from frozen definitions")
    if shape.get("abilities") != sorted(set(applied_labels)):
        raise OpReject("weapon check display labels differ from its stable ability IDs")
    if not isinstance(shape.get("improved"), bool) \
            or shape.get("fired") is not None and not isinstance(shape.get("fired"), str):
        raise OpReject("weapon check on-fail display receipt is malformed")

    scope = check.get("scope")
    over = 0
    if scope is not None:
        scope_rank = {"minor": 0, "standard": 1, "major": 2, "epic": 3, "mythic": 4}.get(
            str(scope)
        )
        if scope_rank is None:
            scope_rank = 1
        band = min(4, int((player.get("skills") or {}).get(skill_id, 0)))
        over = max(0, scope_rank - band)
        if over:
            expected_mod -= 2 * over
    if check.get("_mod") != expected_mod:
        raise OpReject("weapon check modifier differs from its frozen resolution policy")
    dc = check.get("dc")
    tiers = registry.tiers_model(reg, cfg)

    def _settle(naturals: list[int]) -> tuple[str, int]:
        settled_tier, settled_total = registry.resolve_tier(
            naturals, expected_mod, sides, dc, tiers,
        )
        if ward >= 1 and settled_tier == "crit_fail":
            settled_tier = "fail"
        if ward >= 2 and settled_tier == "fail":
            settled_tier = "partial"
        if over:
            ceiling = min(len(CHECK_TIERS) - 1, max(2, 4 - over) + surge_lift)
            if CHECK_TIERS.index(settled_tier) > ceiling:
                settled_tier = CHECK_TIERS[ceiling]
        return settled_tier, settled_total

    def _pool(value: object, length: int, label: str) -> list[int]:
        if not isinstance(value, list) or len(value) != length \
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or not 1 <= item <= sides for item in value):
            raise OpReject(f"weapon check {label} differs from its exact dice policy")
        return list(value)

    base_pool = _pool(shape.get("base_pool"), n_keep + edge, "base pool")
    base_kept = sorted(base_pool, reverse=True)[:n_keep]
    base_tier, base_total = _settle(base_kept)
    final_pool = base_pool
    tier, total = base_tier, base_total
    expected_fired: str | None = None
    expected_improved = False
    on_fail = shape.get("on_fail")
    if on_fail_ids:
        ability_id = on_fail_ids[0]
        definition = known[ability_id]
        mechanic = registry.ability_mechanic(definition)
        if not isinstance(on_fail, dict) or set(on_fail) != {
                "ability_id", "mechanic", "draw_pool", "selected"} \
                or on_fail.get("ability_id") != ability_id \
                or on_fail.get("mechanic") != mechanic:
            raise OpReject("weapon check on-fail phase is malformed")
        if over >= 3 or base_tier not in {"fail", "crit_fail"}:
            raise OpReject("weapon check on-fail ability lacked a failed base roll")
        expected_fired = str(definition.get("name", ability_id))
        if mechanic == "extra_die":
            magnitude = max(1, registry.ability_magnitude(definition, 1))
            draw_pool = _pool(on_fail.get("draw_pool"), magnitude, "extra-die draw")
            if on_fail.get("selected") != "augmented":
                raise OpReject("weapon check extra-die selection is malformed")
            final_pool = base_pool + draw_pool
            final_kept = sorted(final_pool, reverse=True)[:n_keep]
            tier, total = _settle(final_kept)
        elif mechanic == "reroll":
            draw_pool = _pool(on_fail.get("draw_pool"), n_keep + edge, "reroll pool")
            reroll_kept = sorted(draw_pool, reverse=True)[:n_keep]
            reroll_tier, reroll_total = _settle(reroll_kept)
            use_reroll = CHECK_TIERS.index(reroll_tier) >= CHECK_TIERS.index(base_tier)
            selected = "reroll" if use_reroll else "base"
            if on_fail.get("selected") != selected:
                raise OpReject("weapon check reroll selected the wrong phase")
            if use_reroll:
                final_pool, tier, total = draw_pool, reroll_tier, reroll_total
        else:
            raise OpReject("weapon check on-fail mechanic is unsupported")
        expected_improved = CHECK_TIERS.index(tier) > CHECK_TIERS.index(base_tier)
    elif on_fail is not None:
        raise OpReject("weapon check carries an unexecuted on-fail phase")

    final_pool = _pool(shape.get("pool"), len(final_pool), "final pool")
    expected_pool = (
        base_pool + list(on_fail["draw_pool"])
        if on_fail_ids and on_fail["mechanic"] == "extra_die"
        else (
            list(on_fail["draw_pool"])
            if on_fail_ids and on_fail["mechanic"] == "reroll"
            and on_fail["selected"] == "reroll"
            else base_pool
        )
    )
    if final_pool != expected_pool:
        raise OpReject("weapon check final pool does not follow its roll phases")
    kept = sorted(final_pool, reverse=True)[:n_keep]
    if shape.get("kept") != kept or seed != kept:
        raise OpReject("weapon check kept dice differ from its exact roll phases")
    if shape.get("fired") != expected_fired or shape.get("improved") != expected_improved:
        raise OpReject("weapon check on-fail result differs from its roll phases")

    if int(check.get("_scope_over", 0) or 0) != over:
        raise OpReject("weapon check scope receipt differs from frozen mastery policy")
    if over >= 3:
        forced = "crit_fail" if over >= 4 else "fail"
        if CHECK_TIERS.index(tier) > CHECK_TIERS.index(forced):
            tier = forced
    if check.get("result") != total or check.get("tier") != tier:
        raise OpReject("weapon check result or tier differs from deterministic resolution")


def _validate_settled_check_policy(
    state: dict,
    raw_check: dict,
    turn: int,
    cfg,
    frame: dict,
) -> None:
    """Run the one strict roll/capability policy validator for every check contract."""
    _validate_weapon_check_resolution(
        state, raw_check, turn, cfg, frame
    )
    expected_cost, expected_cd = _expected_weapon_check_policy(state, raw_check, turn, cfg)
    actual_cost = raw_check.get("_cost") or {}
    actual_cd = raw_check.get("_ability_cd") or {}
    if actual_cost != expected_cost or actual_cd != expected_cd:
        raise OpReject("settled check cost or cooldown differs from its frozen capability policy")


def _bake_settlement_members(
    store,
    op: dict,
    state: dict,
    turn: int,
    cfg,
) -> list[dict]:
    clone = deepcopy(state)
    baked_members: list[dict] = []
    for raw_member in op["members"]:
        if validate_op(raw_member) is None:
            raise OpReject("mechanic settlement member is malformed")
        member, why = resolve_aliases(raw_member, clone, "rule")
        if member is None:
            raise OpReject(why if why != _DROP_OP else "mechanic member lost its exact referent")
        why = authority_violation(member, "rule", clone, cfg)
        if why is not None:
            raise OpReject(why)
        member = _enrich(member, turn, cfg, clone)
        try:
            member = _materialize_settlement_spawn(store, member, clone, turn)
        except (EnemyCapabilityPoolError, AssignmentError, WorldLexError,
                KeyError, TypeError, ValueError) as exc:
            raise OpReject(str(exc)) from exc
        try:
            _apply_op(clone, member)
        except OpReject:
            raise
        except Exception as exc:
            raise OpReject(f"mechanic member failed: {type(exc).__name__}") from exc
        baked_members.append(member)
    return baked_members


def _prepared_settlement_op(
    op: dict,
    state: dict,
    turn: int,
    cfg,
    baked_members: list[dict],
) -> dict:
    return {
        **_enrich({**op, "members": baked_members}, turn, cfg, state),
        "members": baked_members,
        "_request_fingerprint": _settlement_request_fingerprint(op),
        "_settlement_prepared": True,
    }


def _prepare_live_weapon_settlement(store, op: dict, state: dict, turn: int, cfg) -> dict:
    """Materialize every member on an evolving clone, then seal its post-reducer receipt."""
    _validate_settlement_members_shape(op)
    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict):
        raise OpReject("weapon settlement has no canonical semantic frame")
    raw_check = next(member for member in op["members"] if member.get("op") == "check")
    raw_strike = next(
        member for member in op["members"] if member.get("op") == "combatant_hp"
    )
    _validate_settled_check_policy(state, raw_check, turn, cfg, frame)
    factor = int(_WEAPON_STRIKE_FACTOR.get(str(raw_check.get("tier") or ""), 0))
    shape = raw_check.get("_shape") or {}
    surge = bool(shape.get("surge")) if isinstance(shape, dict) else False
    expected_damage = _weapon_magnitude_policy(
        state, str(raw_check.get("char") or "")
    ) * factor + (1 if factor and surge else 0)
    if int(raw_strike.get("delta", 0)) != -expected_damage \
            or raw_check.get("_dmg", expected_damage) != expected_damage:
        raise OpReject("weapon strike magnitude differs from its frozen damage policy")
    baked_members = _bake_settlement_members(store, op, state, turn, cfg)
    prepared = _prepared_settlement_op(op, state, turn, cfg, baked_members)
    # Recompute from the real pre-state.  The preparation clone above proves each member can
    # apply; this pass builds the immutable receipt through the same replay reducer used later.
    _clone, _group, receipt, store_row = _settlement_group_after_apply(state, prepared, turn)
    prepared["receipt"] = receipt
    prepared["_store_row"] = store_row
    return prepared


def _prepare_live_skill_check_settlement(
    store, op: dict, state: dict, turn: int, cfg,
) -> dict:
    """Prepare one exact skill resolution through the same strict check policy."""
    from .mechanic_settlement import NON_SKILL_CHECK_ACTION_CLASSES

    _validate_settlement_members_shape(op)
    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict) \
            or frame.get("action_class") in NON_SKILL_CHECK_ACTION_CLASSES:
        raise OpReject("skill settlement has no canonical non-impact check frame")
    raw_check = next(member for member in op["members"] if member.get("op") == "check")
    _validate_settled_check_policy(state, raw_check, turn, cfg, frame)
    baked_members = _bake_settlement_members(store, op, state, turn, cfg)
    prepared = _prepared_settlement_op(op, state, turn, cfg, baked_members)
    _clone, _group, receipt, store_row = _skill_check_group_after_apply(
        state, prepared, turn,
    )
    prepared["receipt"] = receipt
    prepared["_store_row"] = store_row
    return prepared


def _prepare_live_combat_opening_settlement(
    store, op: dict, state: dict, turn: int, cfg,
) -> dict:
    """Materialize one complete opening before exposing any combat or pending intent."""
    _validate_settlement_members_shape(op)
    frame = _semantic_frame_for_op(state, op, turn)
    if not isinstance(frame, dict) or frame.get("action_class") != "combat_opening":
        raise OpReject("combat opening settlement has no canonical opening frame")
    baked_members = _bake_settlement_members(store, op, state, turn, cfg)
    prepared = _prepared_settlement_op(op, state, turn, cfg, baked_members)
    _clone, _group, receipt, store_row = _combat_opening_group_after_apply(
        state, prepared, turn,
    )
    prepared["receipt"] = receipt
    prepared["_store_row"] = store_row
    return prepared


def _prepare_live_mechanic_settlement(store, op: dict, state: dict, turn: int, cfg) -> dict:
    from .mechanic_settlement import (
        COMBAT_OPENING_CONTRACT,
        SKILL_CHECK_CONTRACT,
        WEAPON_ATTACK_CONTRACT,
    )

    if op.get("contract_id") == WEAPON_ATTACK_CONTRACT:
        return _prepare_live_weapon_settlement(store, op, state, turn, cfg)
    if op.get("contract_id") == SKILL_CHECK_CONTRACT:
        return _prepare_live_skill_check_settlement(store, op, state, turn, cfg)
    if op.get("contract_id") == COMBAT_OPENING_CONTRACT:
        return _prepare_live_combat_opening_settlement(store, op, state, turn, cfg)
    raise OpReject("unsupported mechanic settlement contract")


def _enemy_narration_evidence(state: dict, privileged_ops: list[dict],
                              turn_lo: int, turn_hi: int) -> dict:
    """Durable evidence used to keep narrator extraction below enemy action authority.

    Combat rows cover the live path; privileged journal ops cover delayed extraction,
    restarts, and a same-turn ``combat_end`` that has already cleared those rows.
    """
    lo, hi = sorted((int(turn_lo), int(turn_hi)))
    fresh = False
    fresh_actor_refs: set[str] = set()
    rows = ((state.get("combat") or {}).get("combatants") or {})
    for cid, row in rows.items():
        if not isinstance(row, dict) or row.get("side") != "enemy":
            continue
        spawned = _int_or(row.get("spawned_turn"), -10**9)
        if lo <= spawned <= hi:
            fresh = True
            fresh_actor_refs.add(str(cid))
            if row.get("eid"):
                fresh_actor_refs.add(str(row["eid"]))
            if eid := resolve_entity_ref(state, row.get("name")):
                fresh_actor_refs.add(eid)

    settled = False
    for op in privileged_ops:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "combatant_spawn" and op.get("side") == "enemy":
            fresh = True
            for ref in (op.get("char"), op.get("_cid")):
                if ref:
                    fresh_actor_refs.add(str(ref))
            if eid := resolve_entity_ref(state, op.get("name")):
                fresh_actor_refs.add(eid)
        elif op.get("op") == "hp_adj" and isinstance(op.get("_opposition"), dict):
            settled = True

    player = _player_target_eid(state)
    return {"player": player, "fresh": fresh,
            "fresh_actor_refs": fresh_actor_refs, "settled": settled}


def _enemy_narration_violation(op: dict, source: str, evidence: dict) -> Optional[str]:
    """Reject narrator-authored consequences that exceed the active enemy adapter.

    The current enemy adapter may settle one code-owned Player-HP receipt.  A new foe's
    introduction precedes even that receipt; it may establish identity and readiness only.
    User, genesis, and rule operations never pass through this extraction-only gate.
    """
    if source != "extraction" or not evidence:
        return None
    player = evidence.get("player")
    if not player:
        return None
    kind = op.get("op")
    targets_player = op.get("char") == player
    effect_mutation = kind in ("effect_add", "effect_update", "effect_remove") \
        and targets_player

    if evidence.get("fresh"):
        negative_hp = kind == "hp_adj" and targets_player \
            and float(op.get("delta", 0)) < 0
        fresh_contact = kind == "contact" and op.get("to_char") == player \
            and str(op.get("from_char")) in evidence.get("fresh_actor_refs", set())
        if negative_hp or effect_mutation or fresh_contact:
            return "new foe has not acted: narrator extraction cannot commit Player harm, " \
                   "status, or contact before the first visible intent"

    if evidence.get("settled") and effect_mutation:
        return "enemy action receipt is HP-only: narrator extraction cannot add, update, " \
               "or remove a Player effect"
    return None


def _settled_semantic_narration_evidence(
    state: dict, privileged_ops: list[dict], turn_lo: int, turn_hi: int,
) -> dict[int, frozenset[str]]:
    """Index code-settled semantic targets from durable rule-journal receipts.

    Tier-1 extraction can run after a restart or across a delayed turn range, so current
    transient state is not enough.  The journaled settlement wrapper and its exact frame are the
    authority: narration may describe the receipt, but cannot add a persistent target status or
    contact that the receipt never admitted.
    """
    from .mechanic_settlement import validate_mechanic_settlement

    lo, hi = sorted((int(turn_lo), int(turn_hi)))
    frames: dict[tuple[int, str], dict] = {}
    for op in privileged_ops:
        if not isinstance(op, dict) or op.get("op") != "semantic_frame_commit" \
                or not isinstance(op.get("frame"), dict):
            continue
        event_turn = _int_or(op.get("_turn"), -1)
        fingerprint = str(op["frame"].get("fingerprint") or "")
        if lo <= event_turn <= hi and fingerprint:
            frames[(event_turn, fingerprint)] = op["frame"]

    by_turn: dict[int, set[str]] = {}
    combatants = ((state.get("combat") or {}).get("combatants") or {})
    for op in privileged_ops:
        if not isinstance(op, dict) or op.get("op") != "mechanic_settlement_commit":
            continue
        event_turn = _int_or(op.get("_turn"), -1)
        if not lo <= event_turn <= hi:
            continue
        try:
            receipt = validate_mechanic_settlement(op.get("receipt"))
        except (KeyError, RuntimeError, TypeError, ValueError):
            continue
        if receipt.get("frame_ref") != op.get("frame_ref"):
            continue

        refs: set[str] = set()
        frame = frames.get((event_turn, str(receipt["frame_ref"])))
        if isinstance(frame, dict) and frame.get("target_entity_id"):
            refs.add(str(frame["target_entity_id"]))
        post = receipt.get("target_post_state")
        if isinstance(post, dict) and post.get("combatant_id"):
            refs.add(str(post["combatant_id"]))

        expanded = set(refs)
        for cid, row in combatants.items():
            if not isinstance(row, dict):
                continue
            eid = str(row.get("eid") or "")
            if str(cid) in refs or (eid and eid in refs):
                expanded.add(str(cid))
                if eid:
                    expanded.add(eid)
        if expanded:
            by_turn.setdefault(event_turn, set()).update(expanded)
    return {event_turn: frozenset(refs) for event_turn, refs in by_turn.items()}


def _settled_semantic_narration_violation(
    op: dict, source: str, evidence: dict[int, frozenset[str]],
) -> Optional[str]:
    """Reject persistent target mutations absent from a settled V3 mechanic receipt."""
    if source != "extraction" or not evidence:
        return None
    explicit_turn = op.get("_turn")
    if isinstance(explicit_turn, int) and not isinstance(explicit_turn, bool):
        targets = evidence.get(explicit_turn, frozenset())
    else:
        # Normal Tier-1 deltas carry one turn_range for the batch, not a trusted turn per op. An
        # unmarked mutation can therefore describe any exchange in that bounded range. Union the
        # code-owned targets instead of silently treating every op as belonging to `turn_hi`.
        targets = frozenset(
            target for turn_targets in evidence.values() for target in turn_targets
        )
    if not targets:
        return None
    kind = op.get("op")
    if kind in ("effect_add", "effect_update", "effect_remove") \
            and str(op.get("char") or "") in targets:
        return "settled semantic target is code-owned: narrator extraction cannot add, " \
               "update, or remove an effect absent from the mechanic receipt"
    if kind == "contact" and {
        str(op.get("from_char") or ""), str(op.get("to_char") or ""),
    }.intersection(targets):
        return "settled semantic target is code-owned: narrator extraction cannot persist " \
               "contact absent from the mechanic receipt"
    return None


def _settlement_batch_failures(pending: list[dict]) -> dict[int, str]:
    """Prove wrappers and marked top-level projections form one exact indexed set."""
    wrappers: dict[str, list[dict]] = {}
    projections: dict[str, dict[int, list[dict]]] = {}
    settlement_evidence: list[dict] = []
    for op in pending:
        if op.get("op") == "mechanic_settlement_commit":
            wrappers.setdefault(str(op.get("settlement_ref") or ""), []).append(op)
            settlement_evidence.append(op)
        if ("_settlement_ref" in op or "_settlement_member_index" in op) \
                and op.get("op") != "mechanic_settlement_commit":
            settlement_evidence.append(op)
        if "_settlement_ref" in op:
            ref = str(op.get("_settlement_ref") or "")
            index = op.get("_settlement_member_index")
            if isinstance(index, int) and not isinstance(index, bool):
                projections.setdefault(ref, {}).setdefault(index, []).append(op)

    failures: dict[int, str] = {}
    all_refs = set(wrappers) | set(projections)
    for ref in all_refs:
        wrapper_rows = wrappers.get(ref, [])
        reason = ""
        if len(wrapper_rows) != 1:
            reason = "mechanic settlement batch needs exactly one wrapper per identity"
        else:
            wrapper = wrapper_rows[0]
            members = wrapper.get("members") or []
            indexed = projections.get(ref, {})
            if set(indexed) != set(range(len(members))) \
                    or any(len(rows) != 1 for rows in indexed.values()):
                reason = "mechanic settlement projections are incomplete or duplicated"
            else:
                for index, member in enumerate(members):
                    expected = {
                        **member,
                        "_settlement_ref": ref,
                        "_settlement_member_index": index,
                    }
                    if indexed[index][0] != expected:
                        reason = "mechanic settlement projection payload does not match its member"
                        break
        if reason:
            for row in wrapper_rows:
                failures[id(row)] = reason
            for rows in projections.get(ref, {}).values():
                for row in rows:
                    failures[id(row)] = reason

    # A projection with all three provenance fields removed used to fall through as an ordinary
    # rule operation. Comparing payloads is not enough: a caller can also change the target, skill,
    # effect, scene, or spawned actor and turn the stripped row into a second code-owned occurrence.
    # Any current wrapper or indexed-child evidence closes the standalone escape hatch, including
    # an incomplete or duplicated group whose settlement operations will themselves quarantine.
    # Every current V3 mechanic occurrence is now grouped.  Once any settlement evidence exists,
    # no unindexed member-family operation may coexist with it: a separately framed raw check can
    # otherwise lose its mastery/cost/consequence while still looking independently authoritative.
    # A window with no settlement evidence retains historical standalone compatibility.
    if settlement_evidence:
        for candidate in pending:
            if candidate.get("op") == "mechanic_settlement_commit" \
                    or "_settlement_ref" in candidate \
                    or "_settlement_member_index" in candidate:
                continue
            if candidate.get("op") not in _SETTLEMENT_MEMBER_KINDS:
                continue
            failures[id(candidate)] = (
                "current mechanic settlement window cannot admit an unindexed standalone "
                "member-family operation"
            )
    return failures


def _front_event_batch_failures(
    pending: list[dict],
    state: dict,
    *,
    session_id: str,
    branch_id: str,
    turn: int,
) -> dict[int, str]:
    """Require one exact, complete immutable event group for every finishing front tick.

    The rule pass publishes a front completion and its zero-or-more supersession terminals plus
    admission in one Ledger batch.  Per-record validation alone is insufficient: removing one
    member could otherwise leave a completed front or a partial event history in the returned
    state.  Exact current-turn retries are accepted when the front already completed this turn.
    """
    event_rows: dict[str, list[dict]] = {}
    for op in pending:
        if op.get("op") != "world_event_admit":
            continue
        event = op.get("event")
        if not isinstance(event, dict):
            continue
        match = re.fullmatch(r"front:([^:]+):completion", str(event.get("cause_id") or ""))
        if match is not None:
            event_rows.setdefault(match.group(1), []).append(op)

    fronts = state.get("fronts") or {}
    finishing_ticks: dict[str, list[dict]] = {}
    for op in pending:
        if op.get("op") != "front_tick":
            continue
        fid = str(op.get("front") or "")
        front = fronts.get(fid)
        if not isinstance(front, dict) or front.get("done"):
            continue
        segments = max(1, int(front.get("segments", 6)))
        if int(front.get("filled", 0)) + 1 >= segments:
            finishing_ticks.setdefault(fid, []).append(op)

    failures: dict[int, str] = {}
    for fid in sorted(set(event_rows) | set(finishing_ticks)):
        rows = event_rows.get(fid, [])
        ticks = finishing_ticks.get(fid, [])
        front = fronts.get(fid)
        reason = ""
        already_complete = isinstance(front, dict) \
            and front.get("done") is True \
            and int(front.get("filled_turn", -1)) == turn \
            and int(front.get("filled", 0)) >= max(1, int(front.get("segments", 6)))
        if not isinstance(front, dict):
            reason = "front World Event group has no committed front"
        elif len(ticks) > 1:
            reason = "front completion batch contains duplicate finishing ticks"
        elif not already_complete and len(ticks) != 1:
            reason = "front World Event group lacks its exact finishing tick"
        else:
            world_id = str((state.get("world_identity") or {}).get("world_id") or "")
            expected = _front_completion_event_records(
                state,
                front_id=fid,
                world_id=world_id,
                session_id=session_id,
                branch_id=branch_id,
                turn=turn,
            )
            submitted = [row.get("event") for row in rows]
            expected_json = sorted(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
                for record in expected
            )
            submitted_json = sorted(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
                for record in submitted
            )
            if not expected or submitted_json != expected_json:
                if any(
                    isinstance(record, dict) and record.get("game_time") != turn
                    for record in submitted
                ):
                    reason = "World Event Record game time is stale or forged"
                else:
                    reason = (
                        "front World Event differs from its deterministic completion projection; "
                        "the atomic supersession and admission set is incomplete or changed"
                    )
        if not reason:
            continue
        for row in [*rows, *ticks]:
            failures[id(row)] = reason
        # Do not allow the legacy briefing projections to leak a completion whose immutable event
        # set was rejected.  These rows are deterministic companions emitted by world_ops.
        expected_memory = ""
        if isinstance(front, dict):
            expected_memory = _front_completion_memory_text(front, fid)
        for candidate in pending:
            if candidate.get("op") == "world_flag" \
                    and candidate.get("key") == fid \
                    and candidate.get("value") == "come to a head":
                failures[id(candidate)] = reason
            elif candidate.get("op") == "memory_event" \
                    and candidate.get("text") == expected_memory:
                failures[id(candidate)] = reason
    return failures


def _unsettled_skill_check_batch_failures(
    pending: list[dict], state: dict, turn: int,
) -> dict[int, str]:
    """Reject V3 mechanics whose owning complete settlement envelope was stripped.

    Unsupported legacy impact lanes remain untouched until they receive their own contract.  For
    the implemented weapon, combat-opening, and non-impact-check lanes, however, a frame reference
    can never authorize only part of the occurrence.
    """
    frames: dict[str, dict] = {}
    for op in pending:
        if op.get("op") != "semantic_frame_commit":
            continue
        frame = op.get("frame")
        if isinstance(frame, dict) \
                and frame.get("schema") == "semantic-action-frame/3" \
                and isinstance(frame.get("fingerprint"), str):
            frames[frame["fingerprint"]] = frame
    for row in state.get("semantic_frames") or []:
        frame = row.get("frame") if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("turn") == turn \
                and isinstance(frame, dict) \
                and frame.get("schema") == "semantic-action-frame/3" \
                and isinstance(frame.get("fingerprint"), str):
            frames[frame["fingerprint"]] = frame

    # Preserve the owning semantic cause when an exact receipt chain deliberately abstains.
    # Batch cardinality runs before the detached receipts are applied, so without this lookup a
    # recognition-only frame is mislabeled as merely missing a settlement envelope.  This changes
    # diagnosis only: the same raw mechanic still fails closed, and candidate frames still require
    # one complete settlement.
    meanings: dict[str, dict] = {}
    bindings: dict[str, dict] = {}
    for row in state.get("semantic_meanings") or []:
        value = row.get("meaning") if isinstance(row, dict) and row.get("turn") == turn else None
        if isinstance(value, dict) and isinstance(value.get("fingerprint"), str):
            meanings[value["fingerprint"]] = value
    for row in state.get("semantic_bindings") or []:
        value = row.get("binding") if isinstance(row, dict) and row.get("turn") == turn else None
        if isinstance(value, dict) and isinstance(value.get("fingerprint"), str):
            bindings[value["fingerprint"]] = value
    for op in pending:
        if op.get("op") == "semantic_meaning_commit" and isinstance(op.get("meaning"), dict):
            value = op["meaning"]
            if isinstance(value.get("fingerprint"), str):
                meanings[value["fingerprint"]] = value
        elif op.get("op") == "semantic_binding_commit" and isinstance(op.get("binding"), dict):
            value = op["binding"]
            if isinstance(value.get("fingerprint"), str):
                bindings[value["fingerprint"]] = value

    def _exact_semantic_disposition(frame: dict) -> Optional[str]:
        """Classify only one fully valid current-turn meaning -> binding -> frame chain."""
        binding = bindings.get(str(frame.get("meaning_binding_ref") or ""))
        meaning = meanings.get(str(frame.get("meaning_ref") or ""))
        if binding is None or meaning is None:
            return None
        try:
            from .semantic import validate_action_frame_snapshot
            from .semantic_binding import validate_meaning_binding
            from .semantic_fabric import validate_compiled_meaning_receipt

            frame = validate_action_frame_snapshot(frame)
            meaning = validate_compiled_meaning_receipt(meaning)
            binding = validate_meaning_binding(binding, meaning_receipt=meaning)
        except (RuntimeError, TypeError, ValueError):
            return None
        frame_id = str(frame.get("frame_id") or "")
        context = frame.get("context_frame") or {}
        if re.fullmatch(rf"t{int(turn)}\.f[1-9][0-9]*", frame_id) is None \
                or frame.get("event_node_id") != f"event.{frame_id}" \
                or binding.get("fingerprint") != frame.get("meaning_binding_ref") \
                or binding.get("meaning_ref") != frame.get("meaning_ref") \
                or binding.get("binding_id") != f"binding.{frame_id}" \
                or binding.get("event_node_id") != frame.get("event_node_id") \
                or binding.get("source_fingerprint") != context.get("source_fingerprint") \
                or binding.get("event_span") \
                != [context.get("span_start"), context.get("span_end")] \
                or meaning.get("fingerprint") != frame.get("meaning_ref") \
                or meaning.get("fabric_fingerprint") != frame.get("fabric_fingerprint") \
                or meaning.get("source_fingerprint") != context.get("source_fingerprint") \
                or meaning.get("genre_ids") != context.get("genre_ids"):
            return None
        disposition = binding.get("mechanic_disposition")
        if disposition != "candidate":
            return "binding_abstains"
        if frame.get("polarity") != "positive" \
                or frame.get("modality") not in ("actual", "command") \
                or frame.get("time_scope") != "current" \
                or frame.get("ambiguity"):
            return "frame_abstains"
        return "candidate"

    unindexed_by_frame: dict[str, list[dict]] = {}
    for op in pending:
        ref = op.get("_semantic_frame_ref")
        if op.get("_settlement_ref") is None and isinstance(ref, str) and ref in frames:
            unindexed_by_frame.setdefault(ref, []).append(op)

    failures: dict[int, str] = {}
    for ref, rows in unindexed_by_frame.items():
        disposition = _exact_semantic_disposition(frames[ref])
        for op in rows:
            if op.get("op") in _SETTLEMENT_MEMBER_KINDS:
                if disposition == "binding_abstains":
                    reason = "semantic action frame binding abstains from mechanic execution"
                elif disposition == "frame_abstains":
                    reason = "semantic action frame abstains from mechanic execution"
                else:
                    # Candidate frames require one complete wrapper.  Missing, malformed,
                    # wrong-turn, and not-yet-implemented impact chains are unresolved too; none
                    # may gain raw-operation authority from an accompanying scene/spawn/HP row.
                    reason = "current V3 mechanics require one complete mechanic settlement"
                failures[id(op)] = (
                    reason
                )
    return failures


def _state_settlement_pair(state: dict, settlement_ref: str, turn: int) \
        -> tuple[Optional[dict], Optional[dict]]:
    public = next((
        row for row in reversed(state.get("mechanic_settlements") or [])
        if isinstance(row, dict) and row.get("turn") == turn
        and isinstance(row.get("receipt"), dict)
        and row["receipt"].get("settlement_ref") == settlement_ref
    ), None)
    private = next((
        row for row in reversed(state.get("_mechanic_settlement_projection_members") or [])
        if isinstance(row, dict) and row.get("turn") == turn
        and row.get("settlement_ref") == settlement_ref
    ), None)
    return public, private


def _store_settlement_row(row: Any) -> Optional[dict]:
    if row is None:
        return None
    try:
        receipt = json.loads(row["receipt_json"])
        return {
            "settlement_ref": row["settlement_ref"],
            "contract_id": row["contract_id"],
            "frame_ref": row["frame_ref"],
            "meaning_ref": row["meaning_ref"],
            "outcome": row["outcome"],
            "outcome_quality": row["outcome_quality"],
            "requirement_fingerprint": row["requirement_fingerprint"],
            "request_fingerprint": row["request_fingerprint"],
            "accepted_group_fingerprint": row["accepted_group_fingerprint"],
            "receipt_fingerprint": row["receipt_fingerprint"],
            "receipt": receipt,
        }
    except (KeyError, TypeError, ValueError):
        return None


def _semantic_receipt_duplicate(state: dict, op: dict, turn: int) -> bool:
    kind = op.get("op")
    mapping = {
        "semantic_meaning_commit": ("semantic_meanings", "meaning"),
        "semantic_binding_commit": ("semantic_bindings", "binding"),
        "semantic_world_alignment_commit": ("semantic_world_alignments", "alignment"),
        "semantic_frame_commit": ("semantic_frames", "frame"),
    }
    if kind not in mapping:
        return False
    ledger_name, field_name = mapping[kind]
    value = op.get(field_name)
    fingerprint = value.get("fingerprint") if isinstance(value, dict) else None
    return any(
        isinstance(row, dict) and row.get("turn") == turn and row.get(field_name) == value
        and isinstance(row.get(field_name), dict)
        and row[field_name].get("fingerprint") == fingerprint
        for row in state.get(ledger_name) or []
    )


_WORLD_ENTITY_NAMESPACE_KINDS = frozenset({"faction", "location", "npc"})
_PLAYER_ENTITY_NAMESPACE_KIND = "player"


def _entity_namespace_keys(
    entity_id: object, name: object = "", aliases: object = None,
) -> frozenset[str]:
    """Canonical keys that can resolve to one entity in the shared ledger namespace."""
    raw_keys = [entity_id, name]
    if isinstance(aliases, list):
        raw_keys.extend(aliases)
    return frozenset(
        key for key in (slug(str(value or "")) for value in raw_keys)
        if key != "unnamed"
    )


def _entity_namespace_kinds_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    relevant = _WORLD_ENTITY_NAMESPACE_KINDS | {_PLAYER_ENTITY_NAMESPACE_KIND}
    return left in relevant and right in relevant


def _fresh_entity_namespace_violation(
    state: dict, pending: list[dict],
) -> Optional[tuple[dict, str]]:
    """Fail a fresh batch before any shared world/Player id can change meaning.

    Historical journals replay through the reducer unchanged.  This admission-only preflight
    protects both ordinary Creator output and forged/direct batches while keeping old saves
    deterministic and readable.
    """
    declarations: list[tuple[frozenset[str], str]] = []
    for entity_id, entity in (state.get("entities") or {}).items():
        if not isinstance(entity, dict):
            continue
        kind = str(entity.get("kind") or "character")
        if kind not in _WORLD_ENTITY_NAMESPACE_KINDS | {_PLAYER_ENTITY_NAMESPACE_KIND}:
            continue
        declarations.append((
            _entity_namespace_keys(
                entity_id, entity.get("name"), entity.get("aliases"),
            ),
            kind,
        ))
    for entity_id in (state.get("player") or {}):
        declarations.append((
            _entity_namespace_keys(entity_id), _PLAYER_ENTITY_NAMESPACE_KIND,
        ))

    for op in pending:
        kind = ""
        keys: frozenset[str] = frozenset()
        if op.get("op") == "entity_add":
            kind = str(op.get("kind") or "character")
            if kind in _WORLD_ENTITY_NAMESPACE_KINDS | {_PLAYER_ENTITY_NAMESPACE_KIND}:
                keys = _entity_namespace_keys(
                    op.get("entity") or slug(str(op.get("name") or "")),
                    op.get("name"),
                    op.get("aliases"),
                )
        elif op.get("op") == "player_seed":
            kind = _PLAYER_ENTITY_NAMESPACE_KIND
            raw_entity = op.get("entity")
            resolved = resolve_entity_ref(state, raw_entity)
            keys = _entity_namespace_keys(resolved or raw_entity, raw_entity)
        if not kind or not keys:
            continue

        for prior_keys, prior_kind in declarations:
            shared = keys & prior_keys
            if not shared or not _entity_namespace_kinds_conflict(prior_kind, kind):
                continue
            entity_id = sorted(shared)[0]
            return op, (
                f"entity namespace collision: '{entity_id}' cannot be both "
                f"{prior_kind} and {kind}"
            )
        declarations.append((keys, kind))
    return None


def apply_delta(store, session_id: str, branch_id: str, turn: int, ops: list,
                source: str, cfg, turn_lo: Optional[int] = None,
                required_extraction: Optional[tuple[int, int]] = None, *,
                semantic_declaration: object = None,
                semantic_authority: object = None,
                semantic_context: object = None,
                semantic_source: object = None) -> ApplyResult:
    """03 SS5.1: validate op-by-op, resolve aliases, authority-check, apply sequentially,
    journal ONLY what applied, checkpoint on cadence, mirror frozen to the session row.

    Cold Tier-1 callers may bind a commit to a still-pending extraction range. The check runs
    inside the same Store transaction as the reducer, so a narration swipe either retires the
    range first (nothing applies) or waits and retracts the completed extraction afterward.
    """
    with store.transaction():
        return _apply_delta_locked(store, session_id, branch_id, turn, ops, source, cfg,
                                   turn_lo=turn_lo,
                                   required_extraction=required_extraction,
                                   semantic_declaration=semantic_declaration,
                                   semantic_authority=semantic_authority,
                                   semantic_context=semantic_context,
                                   semantic_source=semantic_source)


def _migrate_fresh_legacy_knowledge_op(op: object) -> object:
    """Translate a fresh legacy reveal into actor-relative belief only.

    Historical journals still replay ``reveal_fact`` in the pure reducer.  New
    ingress never receives that combined fact-plus-belief authority.
    """
    if not isinstance(op, dict) or op.get("op") != "reveal_fact":
        return op
    statement = str(op.get("statement") or "").strip()
    holder = str(op.get("learner") or "").strip()
    evidence = str(op.get("source") or "inferred").strip()
    if not statement or not holder:
        return {"op": "belief_acquire"}
    migrated = {
        "op": "belief_acquire",
        "holder": holder,
        "statement": statement,
        "stance": "believes",
        "evidence_source": evidence,
        "source": f"legacy_reveal_fact_migration:{evidence}",
        "visibility": "actor_scoped",
        "scoped_actors": [holder],
    }
    if op.get("teller"):
        migrated["teller"] = op["teller"]
    return migrated


def _apply_delta_locked(store, session_id: str, branch_id: str, turn: int, ops: list,
                        source: str, cfg, turn_lo: Optional[int] = None,
                        required_extraction: Optional[tuple[int, int]] = None, *,
                        semantic_declaration: object = None,
                        semantic_authority: object = None,
                        semantic_context: object = None,
                        semantic_source: object = None) -> ApplyResult:
    res = ApplyResult(state=current_state(store, branch_id))
    ops = [_migrate_fresh_legacy_knowledge_op(op) for op in ops]
    if required_extraction is not None:
        lo, hi = required_extraction
        if not store.extraction_pending_range(branch_id, lo, hi):
            log.info("retired extraction [%d,%d] rejected before %s apply", lo, hi, source)
            return res
    ops, live_semantic_ingress, ingress_error = _prepare_semantic_ingress_batch(
        ops,
        res.state,
        session_id,
        branch_id,
        turn,
        semantic_declaration=semantic_declaration,
        semantic_authority=semantic_authority,
        semantic_context=semantic_context,
        semantic_source=semantic_source,
    )
    if ingress_error:
        res.quarantined.extend(
            {"op": op, "reason": ingress_error}
            for op in ops
        )
        return res
    was_frozen = bool(res.state.get("frozen"))
    enemy_evidence: dict = {}
    semantic_narration_evidence: dict[int, frozenset[str]] = {}
    spec = getattr(cfg, "specialization", None)
    if source == "extraction" and spec is not None and spec.name == "rpg":
        lo = turn if turn_lo is None else turn_lo
        privileged_ops = store.rule_ops_between(branch_id, lo, turn)
        semantic_narration_evidence = _settled_semantic_narration_evidence(
            res.state, privileged_ops, lo, turn,
        )
        if getattr(spec, "war_room", True):
            enemy_evidence = _enemy_narration_evidence(res.state, privileged_ops, lo, turn)
    pending = []
    identity_validation_failed = False
    creator_world_validation_failed = False
    narrator = ""
    try:
        narrator = store.narrator_speaker(session_id)
    except Exception:
        pass
    for op in ops:
        v = validate_op(op)
        if v is None:
            res.quarantined.append({"op": op, "reason": "malformed op (02 SS11 spec)"})
            if isinstance(op, dict) and op.get("op") == "world_identity_set":
                identity_validation_failed = True
            if isinstance(op, dict) and op.get("op") == "creator_world_seed":
                creator_world_validation_failed = True
            continue
        if v.get("op") == "world_event_admit":
            event = v.get("event") or {}
            if event.get("session_id") != session_id or event.get("branch_id") != branch_id:
                res.quarantined.append({
                    "op": op,
                    "reason": "World Event Record has a stale, forged, or cross-branch Ledger identity",
                })
                continue
        v, why = _protect_narrator(v, narrator)
        if v is None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        pending.append(v)
    live_semantic_ops = {
        id(op)
        for op in pending
        if live_semantic_ingress and isinstance(op.get("_semantic_ingress"), dict)
    }
    batch_failures = {
        **_unsettled_skill_check_batch_failures(pending, res.state, turn),
        **_settlement_batch_failures(pending),
        **(
            _front_event_batch_failures(
                pending,
                res.state,
                session_id=session_id,
                branch_id=branch_id,
                turn=turn,
            )
            if source == "rule"
            else {}
        ),
    }
    if batch_failures:
        kept = []
        for op in pending:
            reason = batch_failures.get(id(op))
            if reason:
                res.quarantined.append({"op": op, "reason": reason})
            else:
                kept.append(op)
        pending = kept
    for settlement_ref in sorted({
        str(op.get("settlement_ref")) for op in pending
        if op.get("op") == "mechanic_settlement_commit"
    }):
        positions = [
            index for index, op in enumerate(pending)
            if op.get("settlement_ref") == settlement_ref
            or op.get("_settlement_ref") == settlement_ref
        ]
        group = [pending[index] for index in positions]
        assigned = assign_damage_effect_ids(
            group,
            branch_id,
            turn,
            "code",
            basis=f"mechanic-settlement:{settlement_ref}",
        )
        for index, assigned_op in zip(positions, assigned, strict=True):
            pending[index] = assigned_op
    pending.sort(key=lambda o: _ORDER.get(o["op"], _DEFAULT_ORDER))   # 08 E2 family order
    namespace_violation = _fresh_entity_namespace_violation(res.state, pending)
    if namespace_violation is not None:
        failed_op, reason = namespace_violation
        res.quarantined.append({"op": failed_op, "reason": reason})
        pending = []
    identity_ops = [op for op in pending if op.get("op") == "world_identity_set"]
    if identity_validation_failed or creator_world_validation_failed:
        pending = []
    elif identity_ops:
        first = identity_ops[0]
        first_identity = (
            first["world_id"],
            str(first.get("parent_world_id") or ""),
        )
        conflicting = next(
            (
                op for op in identity_ops[1:]
                if (op["world_id"], str(op.get("parent_world_id") or "")) != first_identity
            ),
            None,
        )
        why = authority_violation(first, source, res.state, cfg)
        current = res.state.get("world_identity") or {}
        if conflicting is not None:
            res.quarantined.append({
                "op": conflicting,
                "reason": "one batch cannot bind more than one world lineage",
            })
            pending = []
        elif why is not None:
            res.quarantined.append({"op": first, "reason": why})
            pending = []
        elif current.get("world_id") and (
            current.get("world_id") != first_identity[0]
            or str(current.get("parent_world_id") or "") != first_identity[1]
        ):
            res.quarantined.append({
                "op": first,
                "reason": "this session is already bound to a different world lineage",
            })
            pending = []
        else:
            try:
                store.worldlex.ensure_world_lineage(
                    first_identity[0], parent_world_id=first.get("parent_world_id")
                )
            except WorldLexError as exc:
                res.quarantined.append({"op": first, "reason": str(exc)})
                pending = []
    creator_ops = [op for op in pending if op.get("op") == "creator_world_seed"]
    if creator_ops:
        first = creator_ops[0]
        document = first.get("document") or {}
        target_world_id = str((res.state.get("world_identity") or {}).get("world_id") or "")
        if identity_ops:
            target_world_id = str(identity_ops[0].get("world_id") or "")
        reason = None
        if any(op.get("document") != document for op in creator_ops[1:]):
            reason = "one batch cannot commit more than one Creator world source"
        elif document.get("world_id") != target_world_id:
            reason = "Creator world source belongs to a stale or cross-world identity"
        else:
            current_creator = res.state.get("creator_world") or {}
            if current_creator.get("fingerprint") \
                    and current_creator.get("document") != document:
                reason = "Creator world source is immutable after it is committed"
        if reason is not None:
            res.quarantined.append({"op": first, "reason": reason})
            pending = []
    effect_ids = [str(op.get("_effect_id")) for op in pending
                  if op.get("op") in _DAMAGE_OPS and op.get("_effect_id")]
    existing_receipts = store.effect_receipts(branch_id, effect_ids)
    settlement_refs = [
        str(op.get("settlement_ref")) for op in pending
        if op.get("op") == "mechanic_settlement_commit" and op.get("settlement_ref")
    ]
    existing_settlements = store.mechanic_settlement_receipts(branch_id, settlement_refs)
    new_receipts: dict[str, dict] = {}
    new_mechanic_receipts: dict[str, dict] = {}
    prepared_projections: dict[str, list[dict]] = {}
    duplicate_settlements: set[str] = set()
    failed_settlements: dict[str, str] = {}
    for op in pending:
        if "_settlement_ref" in op:
            ref = str(op.get("_settlement_ref") or "")
            if ref in duplicate_settlements:
                res.duplicates.append(op)
                continue
            if ref in failed_settlements:
                res.quarantined.append({"op": op, "reason": failed_settlements[ref]})
                continue
            index = int(op["_settlement_member_index"])
            projections = prepared_projections.get(ref)
            if not isinstance(projections, list) or not 0 <= index < len(projections):
                reason = "mechanic projection was reached without its committed wrapper"
                failed_settlements[ref] = reason
                res.quarantined.append({"op": op, "reason": reason})
                continue
            projection = projections[index]
            try:
                _apply_op(res.state, projection)
            except Exception as exc:
                reason = f"mechanic projection verification failed: {type(exc).__name__}"
                failed_settlements[ref] = reason
                res.quarantined.append({"op": op, "reason": reason})
                continue
            res.applied.append(projection)
            res.submitted_applied += 1
            continue

        if op.get("op") == "mechanic_settlement_commit":
            ref = str(op.get("settlement_ref") or "")
            why = authority_violation(op, source, res.state, cfg)
            if why is not None:
                failed_settlements[ref] = why
                res.quarantined.append({"op": op, "reason": why})
                continue
            request_fingerprint = _settlement_request_fingerprint(op)
            state_public, state_private = _state_settlement_pair(res.state, ref, turn)
            stored_raw = existing_settlements.get(ref)
            stored = _store_settlement_row(stored_raw)
            exact_retry = False
            if stored_raw is not None or state_public is not None or state_private is not None:
                try:
                    from .mechanic_settlement import validate_mechanic_settlement_row

                    exact_retry = (
                        stored is not None
                        and int(stored_raw["turn_index"]) == turn
                        and validate_mechanic_settlement_row(stored) == stored
                        and state_public == {"turn": turn, "receipt": stored["receipt"]}
                        and isinstance(state_private, dict)
                        and state_private.get("request_fingerprint") == request_fingerprint
                        and state_private.get("settlement_ref") == ref
                    )
                except (KeyError, TypeError, ValueError):
                    exact_retry = False
                if exact_retry:
                    duplicate_settlements.add(ref)
                    res.duplicates.append(op)
                else:
                    reason = "mechanic settlement retry conflicts with Store or state authority"
                    failed_settlements[ref] = reason
                    res.quarantined.append({"op": op, "reason": reason})
                continue
            try:
                # WorldLex definitions/assignments share Store's SQLite transaction.  A nested
                # savepoint keeps preparation side effects provisional until every member and
                # the resulting receipt have passed; caught rejection therefore rolls them back.
                with store.transaction():
                    prepared = _prepare_live_mechanic_settlement(
                        store, op, res.state, turn, cfg
                    )
                    damage_members = [
                        member for member in prepared["members"]
                        if member.get("op") in _DAMAGE_OPS
                    ]
                    from .mechanic_settlement import (
                        COMBAT_OPENING_CONTRACT,
                        SKILL_CHECK_CONTRACT,
                        WEAPON_ATTACK_CONTRACT,
                    )

                    damage_receipt: dict | None = None
                    if prepared.get("contract_id") == WEAPON_ATTACK_CONTRACT:
                        if len(damage_members) != 1:
                            raise OpReject(
                                "weapon settlement does not contain one exact damage member"
                            )
                        damage = damage_members[0]
                        effect_id = str(damage.get("_effect_id") or "")
                        if not effect_id:
                            raise OpReject(
                                "weapon settlement damage lacks its code-owned EffectId"
                            )
                        payload_hash = _damage_payload_hash(damage)
                        effect_prior = existing_receipts.get(effect_id) \
                            or new_receipts.get(effect_id)
                        if effect_prior is not None:
                            raise OpReject(
                                "mechanic settlement and damage receipt authority diverged"
                            )
                        damage_receipt = {
                            "effect_id": effect_id,
                            "family": str(damage["op"]),
                            "target": _damage_target(damage),
                            "direction": _damage_direction(damage),
                            "delta": _damage_delta(damage),
                            "payload_hash": payload_hash,
                            "owner": str(damage.get("_effect_owner") or "unknown"),
                        }
                    elif prepared.get("contract_id") == SKILL_CHECK_CONTRACT:
                        if damage_members:
                            raise OpReject("skill settlement cannot emit a damage member")
                    elif prepared.get("contract_id") == COMBAT_OPENING_CONTRACT:
                        if damage_members:
                            raise OpReject("combat opening settlement cannot emit a damage member")
                    else:
                        raise OpReject("unsupported mechanic settlement contract")
                    _apply_op(res.state, prepared)
            except OpReject as exc:
                reason = str(exc)
                failed_settlements[ref] = reason
                res.quarantined.append({"op": op, "reason": reason})
                continue
            except Exception as exc:
                reason = f"mechanic settlement preparation failed: {type(exc).__name__}"
                failed_settlements[ref] = reason
                res.quarantined.append({"op": op, "reason": reason})
                continue
            private = _state_settlement_pair(res.state, ref, turn)[1]
            prepared_projections[ref] = deepcopy(private["members"])
            res.applied.append(prepared)
            res.submitted_applied += 1
            new_mechanic_receipts[ref] = deepcopy(prepared["_store_row"])
            if damage_receipt is not None:
                new_receipts[str(damage_receipt["effect_id"])] = damage_receipt
            continue

        if _semantic_receipt_duplicate(res.state, op, turn):
            res.duplicates.append(op)
            continue

        op2, why = resolve_aliases(op, res.state, source)
        if op2 is None:
            if why != _DROP_OP:            # _DROP_OP = benign refer-only skip (presence/move to a
                res.quarantined.append({"op": op, "reason": why})   # non-entity): no discovery feed
            continue
        why = _enemy_narration_violation(op2, source, enemy_evidence)
        if why is not None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        why = _settled_semantic_narration_violation(
            op2, source, semantic_narration_evidence,
        )
        if why is not None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        why = authority_violation(
            op2,
            source,
            res.state,
            cfg,
            semantic_ingress_authorized=id(op) in live_semantic_ops,
        )
        if why is not None:
            res.quarantined.append({"op": op, "reason": why})
            continue
        try:
            op2 = _enrich(
                op2, turn, cfg, res.state, source=source,
                session_id=session_id, branch_id=branch_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            res.quarantined.append({"op": op, "reason": str(exc)})
            continue
        if op2.get("op") == "claim_record":
            record = op2.get("_record") or {}
            current_world = str((res.state.get("world_identity") or {}).get("world_id") or "")
            expected_world = current_world or "world_unbound"
            if record.get("session_id") != session_id \
                    or record.get("branch_id") != branch_id \
                    or record.get("turn") != turn \
                    or record.get("world_id") != expected_world:
                res.quarantined.append({
                    "op": op,
                    "reason": "Claim Record has a stale, forged, cross-world, or cross-branch occurrence",
                })
                continue
            claim_violation = _claim_lineage_violation(res.state, op2["frame"], turn)
            if claim_violation is not None:
                res.quarantined.append({"op": op, "reason": claim_violation})
                continue
        if op2.get("op") == "world_event_admit":
            event_violation = _world_event_cause_violation(
                store, res.state, op2["event"], source, cfg, turn,
            )
            if event_violation is not None:
                res.quarantined.append({"op": op, "reason": event_violation})
                continue
        overlay_violation = _world_overlay_operation_violation(res.state, op2, turn=turn)
        if overlay_violation is not None:
            res.quarantined.append({"op": op, "reason": overlay_violation})
            continue
        if op2.get("op") == "hp_adj" and isinstance(op2.get("_opposition"), dict):
            # New autonomous enemy actions have no Player-owned semantic cause.  Strip a bad
            # caller stamp while preserving the exact baked action and historical journal replay.
            op2.pop("_semantic_frame_ref", None)
        if op2.get("op") == "capability_assign":
            try:
                from .worldlex import DefinitionRef, SubjectRef

                definition = DefinitionRef.from_dict(op2["definition"])
                subject = SubjectRef.from_dict(op2["subject"])
                world_id = str((res.state.get("world_identity") or {}).get("world_id") or "")
                if not world_id:
                    raise AssignmentError("seed a stable world before assigning a capability")
                if definition.world_id != world_id or subject.world_id != world_id:
                    raise AssignmentError("capability assignment belongs to a different world")
                if subject.kind == "world" and subject.id != world_id:
                    raise AssignmentError("world subject must name the session's exact world")
                if subject.kind == "actor" \
                        and subject.id not in (res.state.get("entities") or {}):
                    raise AssignmentError("actor subject does not exist in the state ledger")
                assignment = materialize_assignment(
                    store.worldlex,
                    definition=definition,
                    subject=subject,
                    acquisition_source=op2["acquisition_source"],
                    acquisition_turn=turn,
                    adapter_contract=op2.get("adapter_contract"),
                )
                op2["_assignment"] = assignment.as_dict()
            except (AssignmentError, WorldLexError, KeyError, TypeError, ValueError) as exc:
                res.quarantined.append({"op": op, "reason": str(exc)})
                continue
        if op2.get("op") == "combatant_spawn" and op2.get("side") == "enemy" \
                and isinstance(op2.get("_kit"), dict):
            world_id = str((res.state.get("world_identity") or {}).get("world_id") or "")
            if world_id:
                try:
                    bundle, assignments, reconstructed = _materialize_enemy_capability_pool(
                        store,
                        kit=op2["_kit"],
                        world_id=world_id,
                        subject_id=_enemy_pool_subject_id(op2["_cid"]),
                        turn=turn,
                    )
                    if reconstructed != op2["_kit"]:
                        raise EnemyCapabilityPoolError("enemy pool activation parity failed")
                    op2["_capability_pool"] = bundle
                    op2["_capability_assignments"] = assignments
                    op2["_kit"] = reconstructed
                    op2["_kit_source"] = "worldlex-runtime-pool"
                    if isinstance(op2.get("_initial_intent"), dict):
                        peid = _player_target_eid(res.state) or "player"
                        pname = str(
                            (((res.state.get("entities") or {}).get(peid) or {}).get("name"))
                            or peid
                        )
                        pool_intent = select_enemy_intent(
                            {
                                "id": op2["_cid"],
                                "name": str(op2.get("name") or op2["_cid"]),
                                "kit": reconstructed,
                            },
                            turn,
                            peid,
                            pname,
                        )
                        if pool_intent != op2["_initial_intent"]:
                            raise EnemyCapabilityPoolError(
                                "WorldLex pool changed the prepared enemy intent"
                            )
                        op2["_initial_intent"] = pool_intent
                except (EnemyCapabilityPoolError, AssignmentError, WorldLexError,
                        KeyError, TypeError, ValueError) as exc:
                    res.quarantined.append({"op": op, "reason": str(exc)})
                    continue
        if op2.get("op") == "combatant_hp" and op2.get("_strike") is True \
                and op2.get("_semantic_frame_ref"):
            try:
                strike_frame = _semantic_frame_for_op(res.state, op2, turn)
            except OpReject as exc:
                res.quarantined.append({"op": op, "reason": str(exc)})
                continue
            if isinstance(strike_frame, dict) \
                    and strike_frame.get("schema") == "semantic-action-frame/3" \
                    and strike_frame.get("action_class") == "weapon_attack":
                res.quarantined.append({
                    "op": op,
                    "reason": "current V3 weapon damage requires one complete mechanic settlement",
                })
                continue
        is_damage = op2.get("op") in _DAMAGE_OPS
        owner = str(op2.get("_effect_owner", ""))
        if is_damage and owner in ("reply_tag", "extraction"):
            blockers = ("code",) if owner == "reply_tag" else ("code", "reply_tag")
            claim = store.damage_claim(branch_id, turn, turn, str(op2["op"]),
                                       _damage_target(op2), _damage_direction(op2), blockers)
            if claim is None and _damage_direction(op2) != "neutral":
                claim = store.damage_claim(branch_id, turn, turn, str(op2["op"]),
                                           _damage_target(op2), "neutral", blockers)
            if claim is not None:
                res.quarantined.append({
                    "op": op,
                    "reason": (f"damage restatement blocked by {claim['owner']} effect "
                               f"{claim['effect_id']}")})
                continue
        effect_id = str(op2.get("_effect_id", "")) if is_damage else ""
        payload_hash = _damage_payload_hash(op2) if effect_id else ""
        prior = existing_receipts.get(effect_id) or new_receipts.get(effect_id)
        if prior is not None:
            if str(prior["payload_hash"]) == payload_hash:
                res.duplicates.append(op2)
            else:
                res.quarantined.append({
                    "op": op,
                    "reason": f"EffectId conflict for {effect_id}: payload changed"})
            continue
        try:
            occurrences = _expand_user_alias_occurrences(op2, turn)
            if len(occurrences) == 1:
                _apply_op(res.state, occurrences[0])
            else:
                # Alias creation and its owning operation are one state-level decision. Stage
                # the whole occurrence sequence against a copy so an owning reducer rejection
                # cannot leak an entity into state or the journal.
                candidate_state = deepcopy(res.state)
                for occurrence in occurrences:
                    occurrence_overlay_violation = _world_overlay_operation_violation(
                        candidate_state, occurrence, turn=turn,
                    )
                    if occurrence_overlay_violation is not None:
                        raise OpReject(occurrence_overlay_violation)
                    _apply_op(candidate_state, occurrence)
                res.state = candidate_state
        except OpReject as exc:                 # transactional reject (doc 07 §7): visible reason
            res.quarantined.append({"op": op, "reason": str(exc)})
            continue
        except Exception as exc:
            res.quarantined.append({"op": op, "reason": f"apply error {type(exc).__name__}"})
            continue
        res.applied.extend(occurrences)
        res.submitted_applied += 1
        if effect_id:
            new_receipts[effect_id] = {
                "effect_id": effect_id, "family": str(op2["op"]),
                "target": _damage_target(op2), "direction": _damage_direction(op2),
                "delta": _damage_delta(op2), "payload_hash": payload_hash,
                "owner": owner or "unknown"}
    if res.applied:
        lo = turn_lo if turn_lo is not None else turn
        durable_claims = [
            op["_record"] for op in res.applied
            if op.get("op") == "claim_record" and isinstance(op.get("_record"), dict)
        ]
        durable_events = [
            op["event"] for op in res.applied
            if op.get("op") == "world_event_admit" and isinstance(op.get("event"), dict)
        ]
        if new_receipts or new_mechanic_receipts or durable_claims or durable_events:
            store.journal_with_receipts(branch_id, lo, turn, res.applied, source,
                                        list(new_receipts.values()),
                                        mechanic_receipts=list(new_mechanic_receipts.values()),
                                        claim_records=durable_claims,
                                        world_event_records=durable_events)
        else:
            store.journal(branch_id, lo, turn, res.applied, source)
        every = cfg.session.checkpoint_every_turns
        if turn >= 0 and every > 0 and turn % every == 0:
            store.checkpoint(branch_id, turn, res.state)
        # A first event is published to the Store in this transaction.  Return the same derived
        # branch lineage and overlay that close/reopen replay will expose, without persisting those
        # view-only fields inside the journal or checkpoint.
        _attach_world_event_branch_view(store, branch_id, res.state)
    now_frozen = bool(res.state.get("frozen"))
    res.froze, res.unfroze = (not was_frozen and now_frozen), (was_frozen and not now_frozen)
    if res.froze or res.unfroze:
        store.set_frozen(session_id, now_frozen)
    for q in res.quarantined:
        log.info("quarantined %s op: %s", source, q["reason"])
    if getattr(getattr(cfg, "server", None), "turn_trace", False):
        try:
            damage_after = []
            for op in res.applied:
                if op.get("op") == "hp_adj":
                    hp = (((res.state.get("player") or {}).get(op.get("char")) or {}).get("hp")
                          or {})
                elif op.get("op") == "combatant_hp":
                    cid = resolve_combatant(res.state, op.get("target"))
                    hp = ((((res.state.get("combat") or {}).get("combatants") or {}).get(cid)
                           or {}).get("hp") or {})
                else:
                    continue
                damage = {
                    "cur": hp.get("cur"),
                    "max": hp.get("max"),
                }
                for key, value in (
                    ("effect_id", op.get("_effect_id")),
                    ("family", op.get("op")),
                    ("target", _damage_target(op)),
                ):
                    if value is None:
                        damage[key] = None
                    elif safe_token(value) is not None:
                        damage[key] = value
                    else:
                        damage[f"{key}_receipt"] = text_receipt(value)
                damage_after.append(damage)
            emit_turn_trace(log, {
                "event": "apply", "session": session_id, "branch": branch_id,
                "turn": turn, "turn_lo": turn_lo, "source": source,
                "proposed": [_trace_op(op) for op in ops],
                "applied": [_trace_op(op) for op in res.applied],
                "duplicates": [_trace_op(op) for op in res.duplicates],
                "rejected": [{
                    "op": _trace_op(q.get("op")),
                    "reason_receipt": text_receipt(q.get("reason")),
                }
                             for q in res.quarantined],
                "damage_after": damage_after,
            })
        except Exception:
            pass
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
    try:
        from .world_events import project_state_overlay

        world_overlay = project_state_overlay(state)
    except Exception:
        world_overlay = state.get("world_overlay", {})
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
        "epistemic_history": state.get("epistemic_history", []),
        "claims": state.get("claims", []),
        "propositions": state.get("propositions", {}),
        "world_events": state.get("world_events", []),
        "world_overlay": world_overlay,
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
        # Phase 1 War Room: the raw combat ledger + recorded clashes + frozen loot tables —
        # the Console never hides anything (pillar 17); empty in every pre-combat session.
        "combat": state.get("combat", {}),
        "clashes": state.get("clashes", []),
        "loot": state.get("loot", {}),
        # Phase 2 living world: ALL fronts (hidden included — rumor-gating applies to the
        # HUD/briefing ONLY; the Console/inspectors show clocks from turn one) + routes.
        "fronts": state.get("fronts", {}),
        "routes": state.get("routes", {}),
        "turn": state.get("meta", {}).get("turn", -1),
    }
