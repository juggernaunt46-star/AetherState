"""Small, grounded, deterministic enemy kits.

Enemy mechanics are composed from orthogonal combat primitives and factual delivery bases.  This
keeps the system genre-flexible without a monster-by-monster encyclopedia.  Callers freeze the
returned payload in a journaled spawn op; this module is never consulted during replay.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Mapping
from typing import Any

from .morphology import productive_compound_head


KIT_SCHEMA = "enemy-kit/1"
INTENT_SCHEMA = "enemy-intent/1"
GENERATOR_VERSION = "enemy-grammar/1"
BRACE_REACTION = {
    "schema": "enemy-reaction/1",
    "kind": "brace",
    "trigger": "I brace.",
    "cost": "whole_action",
    "effect": "halve_committed_hp",
}

_TIER_MOVES = {"minion": 2, "standard": 3, "elite": 3, "boss": 4}
_TIER_DANGER = {"minion": "low", "standard": "moderate", "elite": "high",
                "boss": "extreme"}
_IDENTITY_KEYS = ("role", "class", "species", "type", "descriptor", "description",
                  "basis", "powers", "magic", "mutation", "augment", "cyberware")

_MAGIC = {"mage", "wizard", "witch", "sorcerer", "sorceress", "warlock", "pyromancer",
          "cryomancer", "necromancer", "arcanist", "spellcaster", "spellblade", "shaman",
          "occultist", "magician", "enchanter", "magical", "arcane"}
_SUPERNATURAL = {"demon", "devil", "fiend", "spirit", "specter", "spectre", "ghost",
                 "wraith", "vampire", "lich", "angel", "fae", "elemental", "aberration"}
_UNDEAD = {"zombie", "undead", "ghoul", "skeleton", "revenant", "mummy", "corpse"}
_TECH_CHASSIS = {"drone", "robot", "android", "cyborg", "mech", "turret", "synthetic",
                 "automaton", "nanite", "nanotech"}
_TECH_AUGMENT = {"cyberware", "augment", "augmented", "bionic", "cybernetic"}
_TECH_DELIVERY = {"arm", "arms", "leg", "legs", "fist", "fists", "claw", "claws",
                  "blade", "weapon", "gun", "cannon", "emitter", "projector", "servo",
                  "actuator", "actuators", "ram", "combat", "strength", "muscle", "tail",
                  "jaw", "jaws", "optic", "optics", "sensor", "sensors"}
_FIREARM = {"gun", "pistol", "revolver", "rifle", "carbine", "shotgun", "smg", "firearm",
            "musket", "blaster", "railgun", "autogun", "machinegun", "flamethrower",
            "launcher", "minigun", "coilgun", "nailgun"}
_ENERGY = {"energy", "plasma", "laser", "particle", "ion", "pulse", "phaser", "beam",
           "tesla", "ray", "blaster", "electric", "electrical", "electro", "shock"}
_PROJECTILE = {"bow", "longbow", "crossbow", "sling", "javelin", "harpoon", "throwing",
               "arrow", "arrows", "bolt", "bolts", "dart", "darts", "shuriken", "boomerang",
               "blowgun", "shortbow"}
_MARTIAL = {"sword", "longsword", "shortsword", "sabre", "saber", "rapier", "dagger",
             "knife", "axe", "hatchet", "mace", "hammer", "club", "maul", "spear", "pike",
             "halberd", "glaive", "staff", "shield", "flail", "scythe", "whip", "chain",
             "blade", "lance", "greatsword", "greataxe", "poleaxe", "crowbar", "chainsaw"}
_NATURAL = {"wolf", "beast", "animal", "hound", "cat", "bear", "boar", "serpent", "spider",
            "fang", "fangs", "claw", "claws", "teeth", "talon", "talons", "beak", "horn",
            "horns", "tentacle", "tentacles", "stinger", "mandible", "bite", "bird", "avian",
            "eagle", "hawk", "falcon", "raven", "snake", "cobra", "lion", "tiger", "rat",
            "crocodile", "shark", "scorpion", "wasp", "octopus", "dragon", "wyvern", "horse",
            "crab", "wing", "wings", "tusk", "tusks", "ooze", "slime", "amorphous",
            "phoenix", "werewolf"}
_HAZARD = {"radiation", "radioactive", "irradiated", "nuclear", "toxin", "toxic", "poison",
           "poisoned", "venom", "venomous", "acid", "acidic", "chemical", "biohazard",
           "corrosive", "pepper", "irritant"}
_HAZARD_DELIVERY = (_FIREARM | _PROJECTILE | _MARTIAL | {"sprayer", "emitter", "projector", "grenade",
                    "bomb", "cannon", "gland", "glands", "fang", "fangs", "stinger", "breath",
                    "spit", "touch", "skin", "blood", "body", "claw", "claws", "talon",
                    "talons", "arrow", "arrows", "bolt", "bolts", "dart", "darts", "bullet",
                    "bullets", "cloud", "cone", "torrent", "spray", "stream", "flask",
                    "bottle", "breathe", "exhale"})
_TECH_COMBAT_DELIVERY = {"arm", "arms", "leg", "legs", "fist", "fists", "claw", "claws",
                         "blade", "weapon", "gun", "cannon", "emitter", "projector", "servo",
                         "actuator", "actuators", "ram", "strength", "muscle", "tail",
                         "jaw", "jaws", "launcher", "missile", "eye", "whip"}
_RELATION_HEADS = {"hunter", "slayer", "killer", "bane", "trap", "operator", "mechanic",
                   "handler", "rider", "cultist", "medium", "engineer", "technician", "company",
                   "burglar", "actor", "wearer", "impersonator", "statue", "construct", "collector",
                   "researcher", "taxidermist", "cosplayer", "storyteller", "tamer", "summoner",
                   "fan", "guide", "photographer", "host", "documentarian", "documentary",
                   "historian", "curator", "reporter", "author", "expert", "scholar"}
_RELATION_PREPOSITIONS = {"of", "to", "for", "against", "about", "on", "into",
                          "concerning", "regarding"}
_REFERENCE_VERBS = {"summon", "summoning", "conjure", "conjuring", "command", "commanding",
                    "study", "studying", "cover", "covering", "specialize", "specializing",
                    "research", "researching", "document", "documenting", "report", "reporting",
                    "photograph", "photographing", "collect", "collecting", "hunt", "hunting",
                    "tame", "taming", "handle", "handling", "fight", "fighting", "battle",
                    "battling", "protect", "protecting", "aim", "aimed", "target", "targeting",
                    "direct", "directed"}
_NEG_BEFORE = {"no", "not", "without", "lack", "lacks", "lacking", "cannot", "unable",
               "immune", "immunity", "resistant", "resistance", "protected", "against",
               "scarred", "hunted", "hunts", "anti", "former", "retired", "mundane", "ex",
               "hunter", "slayer", "killer", "harmless", "illusory", "illusionary", "fake",
               "dummy", "nonfunctional", "inoperative", "prosthetic", "decorative", "disabled",
               "broken", "inert", "deactivated", "neutralized", "summon", "summons", "summoned",
               "summoning", "conjure", "conjures", "conjured", "command", "commands", "commanded"}
_NEG_AFTER = {"resistant", "resistance", "proof", "immune", "immunity", "hunter", "slayer",
              "killer", "bane", "trap", "operator", "mechanic", "handler", "rider", "cultist",
              "medium", "engineer", "company", "burglar", "hunting", "themed", "costume",
              "wearer", "pelt", "impersonator", "shaped", "statue", "collector", "researcher",
              "taxidermist", "cosplayer", "storyteller", "tamer", "summoner", "necklace",
              "trophy", "pendant", "ornament", "jewelry", "jewellery", "tattoo", "emblem",
              "badge", "display", "model"}
_IRREGULAR = {"wolves": "wolf", "zombies": "zombie", "knives": "knife",
              "staves": "staff", "axes": "axe",
              "teeth": "tooth", "mice": "mouse", "geese": "goose", "does": "do"}
_FALSE_MAGIC = {"none", "false", "no", "0", "off", "unknown", "none known", "no magic",
                "mundane"}
_ELEMENTS = {"fire", "flame", "ember", "cold", "frost", "ice", "storm", "lightning",
             "thunder", "arcane", "shadow", "radiant"}
_UTILITY_MAGIC = {"heal", "healing", "restoration", "restorative", "mending", "protective",
                  "protection", "illusion", "divination", "teleportation", "invisibility",
                  "counterspell", "summoning", "resistance", "ward", "utility", "detection",
                  "scrying", "reading", "flight", "barrier", "shield", "harmless", "immunity",
                  "warding", "illusionary", "illusory", "clairvoyance", "clairvoyant",
                  "foresight", "precognition", "memory", "communication", "communications",
                  "translation", "illumination", "navigation", "disguise", "concealment",
                  "clairaudience", "cartography", "empathy", "dream", "interpretation",
                  "ceremonial", "noncombat", "repair", "repairing"}
_OFFENSIVE_DELIVERY = {"attack", "bolt", "bolts", "blast", "breath", "touch", "ray", "missile",
                       "strike", "projectile", "weapon", "flame", "jet", "beam", "gaze", "scream",
                       "pulse", "wave", "lance", "spike", "blade", "shard", "cloud", "lash",
                       "spear", "orb", "arc", "cone", "torrent", "spray", "stream", "needle",
                       "strike", "punch", "burst", "slam", "kick", "slash", "thrust", "crush",
                       "fireball"}
_MANIFESTATIONS = {"breath", "bolt", "blast", "touch", "ray", "missile", "jet", "beam",
                   "gaze", "scream", "pulse", "wave", "lance", "spike", "blade", "shard",
                   "cloud", "lash", "spear", "orb", "arc", "cone", "torrent", "spray",
                   "stream", "needle", "strike", "punch", "burst", "slam", "kick", "slash",
                   "thrust", "crush", "fireball"}
_MANIFESTATION_ORDER = ("breath", "bolt", "blast", "touch", "ray", "missile", "jet", "beam",
                        "gaze", "scream", "pulse", "wave", "lance", "spike", "blade", "shard",
                        "cloud", "lash", "spear", "orb", "arc", "cone", "torrent", "spray",
                        "stream", "needle", "strike", "punch", "burst", "slam", "kick", "slash",
                        "thrust", "crush", "fireball")
_ATTACK_VERBS = {"breathe", "exhale", "shoot", "fire", "hurl", "launch", "unleash", "emit",
                 "project", "spray", "spit"}
_ATTACK_VERB_SHAPE = {"breathe": "breath", "exhale": "breath", "shoot": "bolt",
                      "fire": "bolt", "hurl": "projectile", "launch": "projectile",
                      "unleash": "blast", "emit": "beam", "project": "beam",
                      "spray": "spray", "spit": "spray"}
_MANIFESTATION_PAYLOAD = (_ELEMENTS | _HAZARD | {"force", "gravity", "sonic", "psychic",
                            "necrotic", "holy", "solar", "darkness", "telekinetic", "void",
                            "blood", "bone", "wind", "earth", "plant", "metal", "light",
                            "water", "sand"})
# Ash and cinder are not free-standing damage types. They imply fire only when the author joins
# one to an explicit attack morphology such as "ash-breath" or "cinder blast".
_ARMAMENT_MANIFESTATION_PAYLOAD = _MANIFESTATION_PAYLOAD | {"ash", "cinder"}
_ARMAMENT_SUPPORT = {"sight", "sighted", "designator", "scope", "detector", "badge", "antidote",
                     "vial", "rangefinder", "warning", "telemetry", "camera", "glass", "targeting",
                     "optic", "sensor", "recorder", "pointer", "monitor", "diagnostic",
                     "diagnostics", "decorative", "status", "medical", "indicator", "display",
                     "beacon", "transponder", "communication", "communications", "tracking"}
_NONFUNCTIONAL = {"disabled", "broken", "toy", "unloaded", "jammed", "inert", "empty",
                  "neutralized", "deactivated", "replica", "prop", "malfunctioning",
                  "inoperative", "nonfunctional", "dummy", "fake", "dry", "harmless",
                  "illusory", "illusionary", "prosthetic", "unusable", "unavailable",
                  "powerless", "dormant", "suppressed", "spent", "depleted", "exhausted",
                  "expended", "drained"}
_ACCESSORY_OBJECTS = {"necklace", "trophy", "pendant", "ornament", "jewelry", "jewellery",
                      "tattoo", "emblem", "badge", "holster", "case", "scabbard", "sheath",
                      "container", "display", "model"}
_COMPOUND_PAIRS = {
    ("short", "bow"): "shortbow", ("great", "axe"): "greataxe",
    ("pole", "axe"): "poleaxe", ("nail", "gun"): "nailgun",
    ("flame", "thrower"): "flamethrower", ("blow", "gun"): "blowgun",
    ("machine", "gun"): "machinegun", ("bolt", "action"): "boltaction",
}
_PRODUCTIVE_COMPOUND_HEADS = (
    "blade", "sword", "hammer", "knife", "axe", "rifle", "carbine", "pistol", "cannon",
)
_NONLIVING = {"construct", "golem", "robot", "drone", "android", "automaton", "synthetic",
              "skeleton", "undead", "mech", "turret"}
_DELIVERY_RESOURCES = {"ammunition", "ammo", "bullet", "round", "cartridge", "shell", "arrow",
                       "bolt", "dart", "stone", "fuel", "nail", "charge", "power", "cell",
                       "battery", "rocket", "missile", "needle", "fluid", "reservoir", "tank",
                       "pin"}
_RECEIPT_ONLY_RESTRAINT = {"net", "bola", "bolas", "snare", "cuff", "cuffs", "manacle",
                           "manacles"}
_RECEIPT_ONLY_SIEGE = {"ballista", "catapult", "trebuchet"}
_RECEIPT_ONLY_DEVICE = {"device", "emitter", "generator", "grenade", "projector", "weapon"}
_RECEIPT_ONLY_ZONE = {"field", "zone"}

_SIGNATURE_PRIORITY = {
    "magic": 100,
    "undead": 96,
    "supernatural": 95,
    "hazard": 90,
    "technology": 85,
    "natural": 80,
    "firearm": 70,
    "projectile": 65,
    "martial": 60,
    "physical": 50,
}
_ROLE_PRIMITIVES = (
    ({"artillery", "sniper", "gunner"},
     {"precision_strike", "focused_strike", "burst_strike", "sweeping_strike",
      "charged_strike", "launched_strike", "bracketed_strike"}),
    ({"bruiser", "breaker", "brawler"},
     {"heavy_strike", "driving_strike", "rushing_strike", "clamping_strike",
      "battering_strike", "ramming_strike"}),
    ({"skirmisher", "hunter", "scout", "assassin"},
     {"precision_strike", "tracking_strike", "lunge_strike", "followup_strike", "strike"}),
    ({"defender", "guard", "sentry", "tank"},
     {"braced_strike", "driving_strike", "tracking_strike", "strike"}),
    ({"controller", "occultist"},
     {"sweeping_strike", "pulse_strike", "focused_strike", "tracking_strike"}),
)


def _text(value: Any, limit: int = 160) -> str:
    """Bound unknown caller values without letting malformed metadata break the hot path."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    try:
        return str(value).strip()[:limit]
    except Exception:
        return ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_values(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, (set, frozenset)):
        source = sorted(value, key=lambda item: _text(item, 80))
    elif isinstance(value, (list, tuple)):
        source = value
    else:
        return []
    return [txt for item in itertools.islice(source, limit)
            if (txt := _text(item, 80))]


def _tokens(text: Any) -> set[str]:
    raw = _words(text)
    out = set(raw)
    for token in raw:
        out.add(_singular_token(token))
    return out


def _singular_token(token: str) -> str:
    singular = _IRREGULAR.get(token)
    if singular:
        return singular
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and len(token) > 3 and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _words(text: Any) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", _text(text, 400).lower())
    # Collapse productive compounds into one grammar position.  Keeping an alias between the two
    # authored words lets "no short bow" and "machine gun disabled" bypass the nearby blocker.
    collapsed: list[str] = []
    idx = 0
    while idx < len(raw):
        compound = _COMPOUND_PAIRS.get((raw[idx], raw[idx + 1])) \
            if idx + 1 < len(raw) else None
        if compound:
            collapsed.append(compound)
            idx += 2
        else:
            collapsed.append(raw[idx])
            idx += 1

    # Productive weapon morphology covers genre terms such as vibroblade, chainsword, lasrifle,
    # plasmagun, needler, and slugthrower without a bestiary-sized list of exact item names.  The
    # derived class token remains adjacent, so normal negation/nonfunctional grammar still owns it.
    expanded: list[str] = []
    gun_prefixes = {"plasma", "laser", "las", "rail", "coil", "slug", "nail", "smart",
                    "auto", "machine", "pulse", "beam", "flame", "needle", "ion", "ray"}
    semantic_prefixes = {"plasma": "plasma", "laser": "laser", "las": "laser",
                         "pulse": "pulse", "beam": "beam", "ion": "ion", "ray": "ray",
                         "shock": "shock", "power": "power", "vibro": "vibro",
                         "mono": "mono", "chain": "chain"}
    weapon_suffixes = {"gun", "rifle", "carbine", "pistol", "cannon", "blade", "sword",
                       "hammer", "knife", "axe"}
    for word in collapsed:
        expanded.append(word)
        for prefix, semantic in semantic_prefixes.items():
            if word.startswith(prefix) and word[len(prefix):] in weapon_suffixes:
                expanded.append(semantic)
                break
        compound_head = productive_compound_head(word, _PRODUCTIVE_COMPOUND_HEADS)
        if compound_head:
            expanded.append(compound_head)
        if word.endswith("gun") and word not in _FIREARM and word[:-3] in gun_prefixes:
            expanded.append("gun")
        elif word == "needler" or word.endswith("slugthrower"):
            expanded.append("gun")
    return expanded


def _postpositive_disabled(words: list[str], idx: int) -> bool:
    """Recognize a capability whose following phrase explicitly makes it unusable."""
    tail = words[idx + 1:]
    if not tail:
        return False
    blockers = _NONFUNCTIONAL | {"harmless"}
    transparent = _FIREARM | _PROJECTILE | _MARTIAL | _ENERGY | {"cannon"}
    owned_tail = [word for word in tail if word not in transparent]
    if owned_tail and owned_tail[0] in blockers:
        return True
    if owned_tail and owned_tail[0] in {"is", "are", "was", "were", "has", "have", "had",
                                      "remains", "remain", "currently", "presently"} \
            and set(owned_tail[:5]) & blockers:
        return True
    if len(owned_tail) >= 2 and owned_tail[0] == "training" \
            and owned_tail[1] in {"prop", "dummy", "replica"}:
        return True
    if "cannot" in tail or "unable" in tail:
        return bool(set(tail) & {"use", "used", "activate", "activated", "fire", "fired",
                                "shoot", "harm", "damage", "cause", "work", "function"})
    if "not" in tail and set(tail) & {"do", "does"} \
            and set(tail) & {"work", "function", "fire", "harm"}:
        return True
    if {"no", "longer"} <= set(tail) and set(tail) & {"work", "works", "function", "fire"}:
        return True
    return False


def _accessory_reference(words: list[str], idx: int) -> bool:
    """True when the candidate labels an accessory or what that accessory depicts/holds."""
    accessory_positions = [pos for pos, word in enumerate(words) if word in _ACCESSORY_OBJECTS]
    if not accessory_positions:
        return False
    if any(pos > idx for pos in accessory_positions):
        return True
    relation = {"with", "of", "for", "made", "carrying", "containing", "depicting"}
    return any(pos < idx and set(words[pos + 1:idx]) & relation for pos in accessory_positions)


def _negative_resource_relation(words: list[str], start: int, pos: int) -> bool:
    """Bind absence/exhaustion to a delivery resource, not a depleted-uranium payload or target."""
    before = words[start:pos]
    after = words[pos + 1:pos + 5]
    target_verbs = {"target", "attack", "hit", "burn", "corrode", "damage", "pierce", "harm",
                    "affect"}
    if set(before) & target_verbs:
        return False
    modifiers = {"no", "without", "zero", "insufficient", "empty", "spent", "depleted",
                 "exhausted", "expended", "drained"}
    statuses = {"empty", "spent", "depleted", "exhausted", "expended", "drained"}
    if before and before[-1] in modifiers:
        return True
    if len(before) >= 2 and before[-2:] == ["out", "of"]:
        return True
    if set(before[-4:]) & {"lack", "lacks", "lacking"}:
        return True
    if after and after[0] in statuses:
        return True
    linkers = {"is", "are", "was", "were", "be", "been", "remains", "remain"}
    if len(after) >= 2 and after[0] in linkers and after[1] in statuses:
        return True
    containers = {"tank", "reservoir", "cell", "magazine", "cylinder", "chamber"}
    return len(after) >= 2 and after[0] in containers and after[1] in statuses


def _resource_is_missing(words: list[str], idx: int, token: str) -> bool:
    """True when the authored delivery explicitly lacks the consumable that makes it function."""
    context = set(words)
    resources: set[str] = set()
    if token in _FIREARM | {"bullet", "cannon"} or token == "gun":
        resources.update({"ammunition", "ammo", "bullet", "round", "cartridge", "shell", "pin"})
        if context & {"flamethrower"}:
            resources.add("fuel")
        if context & {"nailgun"}:
            resources.add("nail")
        if context & {"launcher"}:
            resources.update({"rocket", "missile"})
        if context & (_ENERGY | {"plasmagun", "lasrifle", "railgun", "coilgun", "blaster",
                                  "pulsegun", "beamgun", "iongun", "raygun"}):
            resources.update({"charge", "power", "cell", "battery"})
        if context & {"needler"}:
            resources.add("needle")
    if token in _PROJECTILE:
        resources.update({"ammunition", "ammo"})
        if context & {"bow", "longbow", "shortbow"}:
            resources.add("arrow")
        if context & {"crossbow"}:
            resources.add("bolt")
        if context & {"blowgun"}:
            resources.add("dart")
        if context & {"sling"}:
            resources.add("stone")
    if token in _ENERGY:
        resources.update({"charge", "power", "cell", "battery"})
    if token in _HAZARD:
        resources.update({"fluid", "reservoir", "tank", "fuel", "charge", "power"})
    if token == "chainsaw":
        resources.update({"fuel", "charge", "power", "cell"})
    if not resources:
        return False
    for pos in range(idx + 1, len(words)):
        if words[pos] not in resources:
            continue
        if _negative_resource_relation(words, idx + 1, pos):
            return True
    return False


def _field_texts(identity: Mapping[str, Any] | None, key: str) -> list[str]:
    if not isinstance(identity, Mapping):
        return []
    value = identity.get(key)
    if (txt := _text(value)):
        return [txt]
    return _bounded_values(value)


def grounded_actor_armament(identity: Mapping[str, Any] | None) -> str:
    """Return only authored actor equipment evidence suitable for a frozen enemy spawn.

    Exact equipment fields win.  Creator's current notable schema has only a bounded ``role``
    field, so a role may supply armament only when it literally names a recognized delivery
    implement (for example ``spear-and-shield fighter`` or ``crossbow sentry``). Descriptions,
    Player prose, hostility adjectives, and inferred tactics never enter this path.
    """
    if not isinstance(identity, Mapping):
        return ""
    exact: list[str] = []
    for key in ("armament", "weapon", "weapons", "equipment", "gear", "loadout"):
        exact.extend(_field_texts(identity, key))
    if exact:
        return _text("; ".join(exact), 160)
    delivery = _FIREARM | _ENERGY | _PROJECTILE | _MARTIAL | _HAZARD_DELIVERY
    for role in _field_texts(identity, "role"):
        implements: list[str] = []
        for word in _words(role):
            token = _singular_token(word)
            if token in delivery and token not in implements:
                implements.append(token)
        if implements:
            return _text(" and ".join(implements[:4]), 160)
    return ""


def _head_token(text: Any) -> str:
    words = _words(text)
    if not words:
        return ""
    return _singular_token(words[-1])


def _positive_token(text: Any, candidates: set[str]) -> bool:
    """True when a capability word is asserted, not resisted, hunted, or merely operated."""
    # Contrastive clauses carry independent assertions: "immune to fire; casts frost" owns
    # frost, while "fire resistance" still grants nothing.
    for clause in re.split(r"[;,.]|\b(?:but|however|yet)\b", _text(text, 400).lower()):
        words = _words(clause)
        normalized = [_singular_token(word) for word in words]
        for idx, token in enumerate(normalized):
            if token not in candidates \
                    and not ("mancer" in candidates and token.endswith("mancer")):
                continue
            before_words = normalized[max(0, idx - 4):idx]
            before = set(before_words)
            before_all = set(normalized[:idx])
            after = set(normalized[idx + 1:idx + 4])
            relational_defense = bool(before & {
                "immune", "immunity", "resistant", "resistance", "protected",
            }) and bool(before & {"to", "against"})
            hard_before = before & (_NEG_BEFORE - {
                "immune", "immunity", "resistant", "resistance", "protected", "against",
            })
            distant_inability = bool(before_all & {"cannot", "unable"})
            referenced = bool(before_all & (_REFERENCE_VERBS | {
                "against", "at", "from", "toward", "towards",
            }))
            if hard_before or relational_defense or after & _NEG_AFTER or distant_inability \
                    or referenced or _postpositive_disabled(normalized, idx):
                continue
            return True
    return False


def _armament_has(text: Any, candidates: set[str]) -> bool:
    """Recognize a delivered capability without applying subject-history negation to its head."""
    words = [_singular_token(word) for word in _words(text)]
    for idx, token in enumerate(words):
        if token not in candidates:
            continue
        # "Bolt-action" describes a firearm mechanism, never bow ammunition.  Likewise, the
        # spaced spelling "blow gun" is a blowgun rather than a firearm merely containing "gun".
        if token == "bolt" and idx + 1 < len(words) and words[idx + 1] in {
                "action", "boltaction"}:
            continue
        if token == "gun" and idx > 0 and words[idx - 1] in {"blow", "blowgun"}:
            continue
        if _resource_is_missing(words, idx, token):
            continue
        before = words[max(0, idx - 3):idx]
        before_all = set(words[:idx])
        after = set(words[idx + 1:idx + 4])
        accessory = _accessory_reference(words, idx) and not bool(
            set(words) & {"weaponized", "combat", "firing", "mounted"})
        denied_before = bool(before_all & {"no", "not", "without"}) or bool(
            before and before[-1] == "anti")
        if accessory or before and (denied_before or before_all & _NONFUNCTIONAL):
            continue
        coating_only = token in (_FIREARM | _PROJECTILE | _MARTIAL) \
            and _inactive_coating(text)
        if (after & (_NEG_AFTER | _ARMAMENT_SUPPORT) or _postpositive_disabled(words, idx)) \
                and not coating_only:
            continue
        return True
    return False


def _firearm_armament(text: Any) -> bool:
    """Recognize guns and gun-like cannon without turning loose sling ammunition into one."""
    raw = _text(text, 400)
    if _utility_delivery(raw):
        return False
    if _armament_has(raw, _FIREARM):
        return True
    if _armament_has(raw, {"bullet", "bullets"}) and not _armament_has(raw, _PROJECTILE):
        return True
    return _armament_has(raw, {"cannon"}) and not _positive_token(raw, _HAZARD)


def _combat_augment_field(text: Any) -> bool:
    """Require an actual offensive delivery; utility optics and telemetry are not weapons."""
    raw = _text(text, 400)
    if _utility_delivery(raw):
        return False
    tokens = _tokens(raw)
    concrete = _FIREARM | _PROJECTILE | _MARTIAL
    if _armament_has(raw, concrete):
        return True
    support = bool(tokens & _ARMAMENT_SUPPORT)
    energy_delivery = {"eye", "cannon", "emitter", "projector", "gun", "rifle", "weapon",
                       "phaser", "blaster", "ray"}
    if _armament_has(raw, _ENERGY) and tokens & energy_delivery and not support:
        return True
    body_delivery = {"fist", "fists", "claw", "claws", "blade", "whip", "ram", "tail",
                     "jaw", "jaws"}
    powered = _TECH_AUGMENT | {"combat", "weapon", "strength", "muscle", "servo", "actuator",
                               "actuators", "powered"}
    return bool(tokens & body_delivery and tokens & powered and not support)


def _capability_clauses(text: Any) -> list[str]:
    return [clause.strip() for clause in re.split(
        r"[;,.]|\b(?:but|however|yet|and)\b", _text(text, 400).lower()) if clause.strip()]


def _explicit_harm_clause(text: Any) -> bool:
    """Require harmful grammar, not a defensive clause that merely mentions damage or attacks."""
    raw_words = re.findall(r"[a-z0-9]+", _text(text, 400).lower())
    words = [_singular_token(word) for word in raw_words]
    negative = {"no", "not", "never", "cannot", "unable", "incapable", "harmless",
                "noncombat", "ceremonial"}
    if set(words) & negative:
        return False
    strong = {"offensive", "damaging", "harmful", "weaponized", "lethal"}
    if set(words) & strong:
        return True
    harms = {"attack", "strike", "wound", "injure", "burn", "damage", "harm", "hurt", "kill",
             "pierce", "corrode", "affect", "hit"}
    reference = {"prevent", "avoid", "block", "absorb", "heal", "warn", "escape", "resist",
                 "protect", "shield", "reduce", "mend", "restore", "deflect", "negate",
                 "report", "diagnose", "detect", "record", "measure", "describe", "treat",
                 "monitor", "show", "announce", "communicate", "predict", "forecast",
                 "anticipate"}
    prepositions = {"for", "of", "during", "before", "after", "about", "against", "from",
                    "with", "without", "among", "around"}
    for idx, word in enumerate(words):
        if word not in harms:
            continue
        before = words[max(0, idx - 5):idx]
        if set(before) & reference or (before and before[-1] in prepositions):
            continue
        authored = raw_words[idx]
        affirmative_verb = authored.endswith("s") and len(authored) > 3
        modal_verb = bool(before and before[-1] in {"can", "may", "will", "does", "to", "deal"})
        capability_subject = bool(set(before) & (_MANIFESTATIONS | _ATTACK_VERBS))
        if affirmative_verb or modal_verb or capability_subject:
            return True
    return False


def _utility_delivery(text: Any) -> bool:
    tokens = _tokens(text)
    return bool(tokens & _UTILITY_MAGIC and not _explicit_harm_clause(text))


def _defensive_reference_clause(text: Any) -> bool:
    """True when attack words name what a defensive/diagnostic capability acts against."""
    words = [_singular_token(word) for word in _words(text)]
    defensive = {"protect", "deflect", "block", "resist", "absorb", "prevent", "guard", "ward",
                 "negate", "counter", "report", "diagnose", "detect", "record", "predict",
                 "forecast", "anticipate", "warn", "monitor", "reduce", "heal", "treat"}
    threats = _MANIFESTATIONS | _ELEMENTS | {"attack", "strike", "wound", "damage", "harm",
                                              "weapon"}
    return bool(set(words) & defensive and set(words) & threats)


def _strip_harmless_referents(text: Any) -> str:
    """Remove only the capability clause a following harmlessness referent points back to."""
    raw = _text(text, 400).lower()
    pattern = re.compile(
        r"(^|[;,.])\s*[^;,.]*?\b(?:but|however|yet)\s+"
        r"(?:it\s+|they\s+|the\s+(?:magic|spell|power)\s+)?"
        r"(?:cannot|can\s+not|does\s+not|never|is\s+unable\s+to)\s+"
        r"(?:cause\s+)?(?:harm|damage|wound|injure|hurt|kill)\b(?=\s*(?:[;,.]|$))")
    previous = None
    while raw != previous:
        previous = raw
        raw = pattern.sub(lambda match: match.group(1), raw)
    return raw.strip(" ;,.")


def _defensive_coordination(text: Any) -> bool:
    """Keep coordinated threat objects under their defensive governor."""
    raw = _text(text, 400).lower()
    defensive = r"protect|deflect|block|resist|absorb|prevent|guard|ward|negate|counter"
    shape = "|".join(sorted(_MANIFESTATIONS, key=len, reverse=True))
    match = re.search(rf"\b(?:{defensive})\w*\b[^;,.]*\b(?:{shape})s?\b\s+and\s+(.+)$", raw)
    if not match or not re.search(rf"\b(?:{shape})s?\b", match.group(1)):
        return False
    return not re.search(r"\b(?:cast|casts|casting|wield|wields|unleash|unleashes|shoot|shoots|"
                         r"damaging|offensive|harmful|attacks?)\b", match.group(1))


def _contradictory_magic_field(text: Any) -> bool:
    clauses = _capability_clauses(text)
    signature = _ELEMENTS | _MAGIC | _UTILITY_MAGIC | _MANIFESTATIONS | _MANIFESTATION_PAYLOAD
    positive: set[str] = set()
    negative: list[set[str]] = []
    for clause in clauses:
        tokens = _tokens(clause)
        denied = bool(tokens & {"no", "not", "without"}) and "magic" in tokens
        owned = tokens & signature - {"magic", "magical", "spell", "casting", "cast"}
        if denied:
            if not owned:
                return True
            negative.append(owned)
        else:
            positive.update(owned)
    return any(denied & positive for denied in negative)


def _positive_magic_field(text: Any) -> bool:
    raw = _strip_harmless_referents(text)
    if raw in _FALSE_MAGIC:
        return False
    # Contradiction is capability-scoped: "fire magic and no fire magic" fails closed, while
    # "fire bolt and no healing magic" keeps the independently asserted attack.
    if not raw or _contradictory_magic_field(raw) or _defensive_coordination(raw):
        return False
    capabilities = _MAGIC | _ELEMENTS | {"mancer", "magic", "spell", "casting", "cast",
                                         "casts", "sorcery"}
    for clause in _capability_clauses(raw):
        if _capability_unavailable(clause) or _defensive_reference_clause(clause):
            continue
        tokens = _tokens(clause)
        explicit_offense = _explicit_harm_clause(clause)
        if tokens & _UTILITY_MAGIC and not explicit_offense:
            continue
        cannot_harm = bool(tokens & {"cannot", "unable", "harmless"}) and bool(
            tokens & {"harm", "harmful", "damage", "damaging", "cause"})
        if cannot_harm:
            continue
        # "X magic only" is often an explicit boundary, not permission to manufacture an attack.
        limited = tokens & {"only", "solely", "exclusively", "noncombat", "restricted", "purely",
                            "ceremonial"}
        if limited and not (explicit_offense or tokens & _ELEMENTS):
            continue
        # An authored attack morphology is sufficient evidence in the explicitly named magic field.
        if _manifestation_kind(clause):
            return True
        if _positive_token(clause, capabilities):
            return True
    return False


def _positive_power_magic_field(text: Any) -> bool:
    """Treat an elemental bodily/superpower manifestation as itself, not as a spell grant."""
    raw = _strip_harmless_referents(text)
    if raw in _FALSE_MAGIC:
        return False
    if not raw or _capability_unavailable(raw) or _defensive_coordination(raw):
        return False
    if _tokens(raw) & {"cast", "casts", "casting", "wield", "wields"}:
        return _positive_magic_field(raw)
    markers = _MAGIC | {"mancer", "magic", "spell", "spells", "cast", "casts", "casting",
                        "sorcery", "hex", "fireball"}
    return _positive_token(raw, markers)


def _capability_unavailable(text: Any) -> bool:
    """Recognize an authored attack shape whose required source is absent or exhausted."""
    raw = _text(text, 400).lower()
    if re.search(r"\b(?:cause|causes|deal|deals)\s+no\s+(?:harm|damage)\b", raw):
        return True
    words = [_singular_token(word) for word in _words(raw)]
    anchors = [idx for idx, word in enumerate(words) if word in _MANIFESTATIONS]
    if not anchors:
        anchors = [idx for idx, word in enumerate(words) if word in _ATTACK_VERBS]
    if not anchors:
        return False
    tail = words[min(anchors) + 1:]
    unavailable = {"spent", "depleted", "exhausted", "drained", "inert", "disabled",
                   "nonfunctional", "inoperative", "unusable", "unavailable", "powerless",
                   "dormant", "suppressed", "expended", "empty"}
    linkers = {"is", "are", "was", "were", "be", "been", "has", "have", "remains", "remain",
               "currently", "presently"}
    for pos, word in enumerate(tail):
        if word not in unavailable:
            continue
        prefix = tail[:pos]
        if all(item in linkers for item in prefix):
            return True
    for pos, word in enumerate(tail):
        if word not in {"cannot", "unable", "incapable"}:
            continue
        if pos <= 2 and all(item in linkers | {"that", "which", "it"} for item in tail[:pos]):
            return True
    if {"no", "longer"} <= set(tail) and set(tail) & {"work", "works", "function", "fire"}:
        return True
    resources = {"power", "energy", "charge", "voice", "force", "fluid", "fuel", "ammunition",
                 "ammo", "air", "breath", "reservoir"}
    for pos, word in enumerate(tail):
        if word not in resources:
            continue
        if _negative_resource_relation(tail, 0, pos):
            return True
    return False


def _manifestation_kind(text: Any) -> str:
    """Return one grounded attack morphology, including verb-authored manifestations."""
    raw = _strip_harmless_referents(text)
    if not raw or _defensive_coordination(raw):
        return ""
    for clause in _capability_clauses(raw):
        if _capability_unavailable(clause) or _defensive_reference_clause(clause):
            continue
        clause_tokens = _tokens(clause)
        # Shape nouns are not inherently attacks: a healing wave, communication beam, or
        # protective pulse remains utility unless the author explicitly says it harms.
        if clause_tokens & _UTILITY_MAGIC and not _explicit_harm_clause(clause):
            continue
        for kind in _MANIFESTATION_ORDER:
            if _positive_token(clause, {kind}):
                return kind
        words = [_singular_token(word) for word in _words(clause)]
        for idx, verb in enumerate(words):
            if verb not in _ATTACK_VERBS or not _positive_token(clause, {verb}):
                continue
            payload = set(words[idx + 1:])
            # Bare "fire" is also an element adjective ("fire elemental").  A firing verb still
            # grounds a move when it names a payload ("fires lightning") or an explicit shape
            # ("fires bolts"), but the word alone cannot manufacture a bolt.
            inherently_offensive = verb in {"breathe", "exhale", "shoot", "hurl",
                                             "launch", "unleash", "spray", "spit"}
            if inherently_offensive or payload & _MANIFESTATION_PAYLOAD:
                return _ATTACK_VERB_SHAPE[verb]
    return ""


def _positive_hazard_field(text: Any) -> bool:
    """Recognize one asserted hazard+delivery clause without leaking nearby immunities."""
    raw = _strip_harmless_referents(text)
    if not raw or _defensive_coordination(raw):
        return False
    clauses = re.split(r"[;,.]|\b(?:but|however|yet|and)\b", raw)
    return any(not _capability_unavailable(clause)
               and _positive_token(clause, _HAZARD)
               and _positive_token(clause, _HAZARD_DELIVERY)
               for clause in clauses)


def _inactive_coating(text: Any) -> bool:
    raw = _text(text, 400).lower()
    blockers = ("neutralized|inert|disabled|harmless|spent|depleted|empty|nonfunctional|"
                "inoperative")
    return bool(re.search(rf"\b(?:{blockers})\b.{{0,16}}\bcoat(?:ing|ed)?\b", raw)
                or re.search(rf"\bcoat(?:ing|ed)?\b.{{0,16}}\b(?:is\s+)?(?:{blockers})\b", raw))


def _subject_tokens(name: str, identity: Mapping[str, Any] | None) -> tuple[set[str], list[str]]:
    """Separate what the actor *is* from quarry, employer, tool, and descriptive history."""
    tokens: set[str] = set()
    profile: list[str] = []
    capability_tokens = _MAGIC | _SUPERNATURAL | _UNDEAD | _TECH_CHASSIS | _NATURAL

    def accept_subject_phrase(text: str, *, authoritative: bool = False) -> None:
        head = _head_token(text)
        if not head or (head in _RELATION_HEADS and not authoritative):
            return
        profile.append(text)
        words = [_singular_token(word) for word in _words(text)]
        for token in set(words) & capability_tokens:
            positions = [idx for idx, word in enumerate(words) if word == token]
            # A capability after "of/to/for/against/about" is the topic, quarry, or referent:
            # "fan of vampires" and "guide to dragons" do not describe the actor's body.
            owned = any(not set(words[:idx]) & _RELATION_PREPOSITIONS for idx in positions)
            if owned and _positive_token(text, {token}):
                tokens.add(token)

    for text in [name, *sum((_field_texts(identity, key)
                             for key in ("role", "class", "descriptor")), [])]:
        accept_subject_phrase(text)
    for key in ("species", "type", "basis"):
        for text in _field_texts(identity, key):
            # These fields explicitly describe the actor rather than a topical name or occupation.
            accept_subject_phrase(text, authoritative=True)
    return tokens, profile


def _bounded_identity(identity: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    values: list[str] = []
    if isinstance(identity, Mapping):
        for key in _IDENTITY_KEYS:
            value = identity.get(key)
            if (txt := _text(value)):
                values.append(txt)
            else:
                values.extend(_bounded_values(value))
    return " ".join(values), values[:16]


def _armament_manifestation_field(label: str, tokens: set[str]) -> bool:
    """True only for a listed extraordinary delivery, never its sensor, ornament, or cover."""
    passive_or_referential = _ARMAMENT_SUPPORT | _ACCESSORY_OBJECTS | {
        "camouflage", "camouflaged", "camouflaging", "concealment", "concealing",
        "screen", "screening", "decoy",
    }
    return bool(_manifestation_kind(label)
                and tokens & _ARMAMENT_MANIFESTATION_PAYLOAD
                and not tokens & passive_or_referential
                and not _positive_hazard_field(label)
                and not _utility_delivery(label))


def _receipt_only_delivery(text: Any) -> bool:
    """True when equipment describes an unsupported consequence, not an HP implement.

    These are semantic families whose fiction is valid but whose effect requires restraint,
    disruption, impairment, persistent-zone, or siege receipts.  The actor may still fall back to
    independently grounded body force; the named device itself cannot become generic HP delivery.
    """
    tokens = _tokens(text)
    if tokens & _RECEIPT_ONLY_RESTRAINT or tokens & _RECEIPT_ONLY_SIEGE:
        return True
    disruption = bool(tokens & {"emp", "electromagnetic"} and tokens & _RECEIPT_ONLY_DEVICE)
    impairment = "flashbang" in tokens or {"tear", "gas"} <= tokens
    persistent_zone = bool(tokens & _HAZARD and tokens & _RECEIPT_ONLY_ZONE
                           and tokens & _RECEIPT_ONLY_DEVICE)
    return disruption or impairment or persistent_zone


def _basis(name: str, armament: str,
           identity: Mapping[str, Any] | None) -> tuple[list[str], dict, str]:
    _identity_text, evidence = _bounded_identity(identity)
    subject, profile = _subject_tokens(name, identity)
    atoks = _tokens(armament)
    armament_parts = _armament_parts(armament)
    mechanical_parts = [(label, tokens) for label, tokens in armament_parts
                        if not _receipt_only_delivery(label)]
    found: list[str] = []
    anatomy = {"fang", "fangs", "claw", "claws", "teeth", "talon", "talons", "beak",
               "horn", "horns", "tentacle", "tentacles", "stinger", "mandible", "bite"}
    body_delivery = any(_armament_has(label, anatomy) and not tokens & (
        _FIREARM | _PROJECTILE | _MARTIAL | {"missile"})
                        for label, tokens in mechanical_parts)
    energy_delivery = any(
        _armament_has(label, _ENERGY)
        and bool(tokens & (_FIREARM | _MARTIAL | {"cannon", "emitter", "projector", "lance",
                                                 "blade", "rifle", "weapon", "eye", "phaser",
                                                 "blaster", "ray", "baton"}))
        and not _utility_delivery(label)
        for label, tokens in mechanical_parts)
    hazard_delivery = any(
        _armament_has(label, _HAZARD) and bool(tokens & _HAZARD_DELIVERY)
        for label, tokens in mechanical_parts)
    coating_delivery = bool(re.search(
        r"\b(?:poison(?:ed)?|venom(?:ous)?|acid(?:ic)?|corrosive|toxic)\b.{0,24}"
        r"\b(?:coat(?:ed|ing)?|edge|blade|arrow|bolt|dart|bullet)s?\b",
        armament.lower())) and bool(atoks & (_MARTIAL | _PROJECTILE | _FIREARM)) \
        and not _inactive_coating(armament)
    hazard_delivery = hazard_delivery or coating_delivery

    magic_texts = _field_texts(identity, "magic")
    power_texts = _field_texts(identity, "powers")
    mutation_texts = _field_texts(identity, "mutation")
    augment_texts = [*_field_texts(identity, "augment"),
                     *_field_texts(identity, "cyberware")]
    basis_texts = _field_texts(identity, "basis")
    natural_texts = [*power_texts, *mutation_texts]
    armament_manifestations = [
        label for label, tokens in mechanical_parts
        if _armament_manifestation_field(label, tokens)
    ]

    def is_magic_subject(token: str) -> bool:
        return token in _MAGIC or (token.endswith("mancer") and len(token) > 6) \
            or (token.endswith("mage") and token != "image")

    explicit_magic = any(_positive_magic_field(text) for text in magic_texts)
    powered_magic = any(_positive_power_magic_field(text) for text in power_texts)
    powered_natural = any(_positive_token(text, _NATURAL) for text in natural_texts)
    powered_manifestation = bool(armament_manifestations) or any(
        _manifestation_kind(text) and not _positive_power_magic_field(text)
        and not _positive_hazard_field(text)
        for text in natural_texts)
    explicit_hazard_texts = [*basis_texts, *power_texts, *mutation_texts, *augment_texts,
                             *_field_texts(identity, "species"), *_field_texts(identity, "type")]
    powered_hazard = any(_positive_hazard_field(text) for text in explicit_hazard_texts)
    combat_augment = any(_combat_augment_field(text) for text in augment_texts)

    # Concrete delivery wins the core slot; extraordinary identity expands it but never replaces it.
    if any(_firearm_armament(label) for label, _tokens_ in mechanical_parts):
        found.append("firearm")
    if any(_armament_has(label, _PROJECTILE) for label, _tokens_ in mechanical_parts):
        found.append("projectile")
    if any(_armament_has(label, _MARTIAL) for label, _tokens_ in mechanical_parts):
        found.append("martial")
    if hazard_delivery:
        found.append("hazard")
    if body_delivery or subject & _NATURAL or powered_natural:
        found.append("natural")
    # A chassis noun can ground technology, but umbrella words such as "cyberware" cannot
    # manufacture combat servos, sensors, and weapons.  Augments need a concrete delivery fact.
    chassis = subject & _TECH_CHASSIS
    if chassis or combat_augment or energy_delivery:
        found.append("technology")
    subject_magic = any(is_magic_subject(token) for token in subject)
    if (subject_magic and (not magic_texts or explicit_magic)) or explicit_magic or powered_magic:
        found.append("magic")
    if subject & _UNDEAD:
        found.append("undead")
    if subject & _SUPERNATURAL:
        found.append("supernatural")
    if powered_manifestation and "supernatural" not in found:
        found.append("supernatural")
    if powered_hazard and "hazard" not in found:
        found.append("hazard")
    if not found:
        found.append("physical")
    if explicit_magic:
        profile.extend(magic_texts)
    if powered_magic:
        profile.extend(text for text in power_texts if _positive_power_magic_field(text))
    if powered_natural:
        profile.extend(text for text in natural_texts if _positive_token(text, _NATURAL))
    if powered_manifestation:
        profile.extend(armament_manifestations)
        profile.extend(text for text in natural_texts
                       if _manifestation_kind(text)
                       and not _positive_power_magic_field(text)
                       and not _positive_hazard_field(text))
    if powered_hazard:
        profile.extend(text for text in explicit_hazard_texts
                       if _positive_hazard_field(text))
    if combat_augment:
        profile.extend(text for text in augment_texts if _combat_augment_field(text))
    grounding = {"name": _text(name, 80), "armament": _text(armament, 120),
                 "identity": evidence}
    return found, grounding, "; ".join(dict.fromkeys(profile))[:800]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:48] or "move"


def _move(basis: str, primitive: str, name: str, delivery: str, range_: str, timing: str,
          accuracy: int, damage: int, tell: str, counterplay: list[str], sensory: str,
          *, cadence: str = "reliable", target_rule: str = "player",
          risk: str = "none", forbid: str = "unlisted effects or extra targets",
          brace: bool = False) -> dict:
    counters = [_text(x, 100) for x in (counterplay if isinstance(counterplay, list) else [])[:3]]
    move = {
        "id": _slug(f"{basis} {primitive} {name}"),
        "name": _text(name, 80),
        "primitive": _text(primitive, 48) or "strike",
        "basis": _text(basis, 40) or "physical",
        "channel": "hp",
        "delivery": _text(delivery, 160) or "physical force",
        "range": _text(range_, 40) or "close",
        "timing": _text(timing, 80) or "fast",
        "cadence": _text(cadence, 80) or "reliable",
        "target_rule": _text(target_rule, 40) or "player",
        "accuracy": max(-2, min(2, _safe_int(accuracy))),
        "damage": max(-2, min(3, _safe_int(damage))),
        "tell": _text(tell, 240) or "the attack lines up",
        "counterplay": counters or ["guard the direct line"],
        "sensory": _text(sensory, 200) or "movement and impact",
        "risk": _text(risk, 160) or "none",
        "forbid": _text(forbid, 240) or "unlisted effects or extra targets",
    }
    if brace:
        move["reaction"] = dict(BRACE_REACTION)
    return move


def _clean_implement(segment: str, default: str) -> str:
    """Return a compact visible implement without changing what the source says it is."""
    raw_words = re.findall(r"[A-Za-z0-9'-]+", segment)
    words = [w for w in raw_words if w.lower() not in {"a", "an", "the", "and", "pair"}]
    if words and words[0].lower() == "of":
        words = words[1:]
    # Qualifiers are mechanics evidence ("broken old service rifle", "necklace with claws").
    # Retain them in the bounded label instead of silently trimming away the grammar.
    return " ".join(words)[:60] or default


def _armament_parts(armament: str) -> list[tuple[str, set[str]]]:
    raw = _text(armament, 160)
    raw_tokens = _tokens(raw)
    accessory_relation = bool(raw_tokens & _ACCESSORY_OBJECTS) and bool(
        raw_tokens & {"with", "of", "for", "made", "carrying", "containing", "depicting"})
    raw_parts = [raw] if accessory_relation else [part.strip() for part in re.split(
        r"\b(?:and|with|plus)\b|[,;/+&]", raw, flags=re.IGNORECASE) if part.strip()]
    parts: list[str] = []
    deliveries = _FIREARM | _PROJECTILE | _MARTIAL | _NATURAL | _HAZARD | _ENERGY
    for part in raw_parts:
        tokens = _tokens(part)
        words = [_singular_token(word) for word in _words(part)]
        resource_fragment = any(word in _DELIVERY_RESOURCES
                                and _negative_resource_relation(words, 0, pos)
                                for pos, word in enumerate(words))
        # A trailing condition fragment still modifies the preceding implement:
        # "rifle, unloaded" is not a separate object named "unloaded".
        condition_fragment = tokens & _NONFUNCTIONAL and not tokens & deliveries \
            and not tokens & _DELIVERY_RESOURCES
        if parts and (resource_fragment or condition_fragment):
            parts[-1] = f"{parts[-1]} {part}"
        else:
            parts.append(part)
    return [(_clean_implement(part, "weapon"), _tokens(part)) for part in parts]


def _source_for(armament: str, basis: str, default: str) -> str:
    """Bind each generated move family to the armament segment that licensed it."""
    candidates: list[tuple[int, int, str]] = []
    for idx, (label, tokens) in enumerate(_armament_parts(armament)):
        if _receipt_only_delivery(label):
            continue
        score = 0
        if basis == "firearm" and _firearm_armament(label):
            score = 10
        elif basis == "projectile" and _armament_has(label, _PROJECTILE):
            score = 10
        elif basis == "martial" and _armament_has(label, _MARTIAL):
            score = 8 if tokens <= {"shield"} else 10
        elif basis == "technology" and _armament_has(label, _ENERGY) and tokens & (
                _FIREARM | _MARTIAL | {"cannon", "emitter", "projector", "weapon", "eye",
                                       "phaser", "blaster", "ray", "baton"}):
            score = 12
        elif basis == "technology" and _combat_augment_field(label) and tokens & (
                _TECH_AUGMENT | {"wrist", "forearm", "finger", "shoulder", "mounted", "implant",
                                 "monofilament"}):
            score = 10
        elif basis == "hazard" and _armament_has(label, _HAZARD) \
                and tokens & _HAZARD_DELIVERY:
            score = 10
        elif basis == "natural" and _armament_has(label, _NATURAL) \
                and not tokens & (_FIREARM | _PROJECTILE | _MARTIAL | {"missile"}):
            score = 10
        if score:
            candidates.append((score, -idx, label))
    if candidates:
        return max(candidates)[2]
    # A coating is grammatically attached to its preceding implement even though the general
    # armament parser separates "with" clauses.  Preserve the caller's exact combined phrase.
    if basis == "hazard" and re.search(
            r"\b(?:poison(?:ed)?|venom(?:ous)?|acid(?:ic)?|corrosive|toxic)\b.{0,24}"
            r"\b(?:coat(?:ed|ing)?|edge|blade|arrow|bolt|dart|bullet)s?\b",
            _text(armament, 160).lower()) and not _inactive_coating(armament):
        return _text(armament, 160) or default
    return default


def _manifestation_spec(profile_text: str,
                        asserted_element: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """Extract one authored attack shape without turning its surrounding prose into mechanics."""
    manifestation = _manifestation_kind(profile_text)
    if not manifestation:
        return None
    clauses = _capability_clauses(_text(profile_text, 800))
    source_clause = next((clause.strip() for clause in clauses
                          if _manifestation_kind(clause) == manifestation), manifestation)
    words = [_singular_token(word) for word in _words(source_clause)]
    kind_index = next((idx for idx, word in enumerate(words)
                       if word == manifestation), -1)
    verb_index = next((idx for idx, word in enumerate(words)
                       if word in _ATTACK_VERBS
                       and _ATTACK_VERB_SHAPE.get(word) == manifestation), -1)
    ignored = {
        "a", "an", "the", "of", "with", "can", "may", "will", "magic", "magical",
        "spell", "spells", "cast", "casting", "casts", "wield", "wields", "use", "uses",
        "emit", "emits", "unleash", "unleashes", "project", "projects", "focused",
        "committed", "attack", "attacks", "offensive", "combat", "weapon", "weaponized",
        "against", "at", "from", "toward", "towards", "into", "through", "for",
        "target", "targeting", "aim", "aimed", "direct", "directed",
        *(_ATTACK_VERBS - _MANIFESTATION_PAYLOAD),
    }
    modifier = ""
    if kind_index >= 0:
        before = [word for word in words[max(0, kind_index - 4):kind_index]
                  if word not in ignored and word != manifestation]
        after = [word for word in words[kind_index + 1:kind_index + 5]
                 if word not in ignored and word != manifestation]
        modifier = before[-1] if before else (after[0] if after else "")
    elif verb_index >= 0:
        payload = [word for word in words[verb_index + 1:verb_index + 6]
                   if word not in ignored and word != manifestation]
        modifier = next((word for word in payload if word in _MANIFESTATION_PAYLOAD),
                        payload[0] if payload else "")

    if asserted_element:
        modifier = asserted_element[0]
    modifier = {
        "flame": "fire", "ember": "fire", "ice": "cold", "frost": "cold",
        "ash": "fire", "cinder": "fire", "storm": "lightning",
        "thunder": "lightning", "radiant": "radiance",
    }.get(modifier, modifier)
    if manifestation == "fireball" and modifier in {"", "fire", "flame"}:
        delivery = "fireball"
    else:
        delivery = f"{modifier} {manifestation}".strip()
    delivery = delivery or manifestation
    delivery_tokens = _tokens(delivery)
    if delivery_tokens & {"fire", "flame", "ember", "fireball"}:
        sensory = "heat, flame motion, and one focused impact"
    elif delivery_tokens & {"cold", "frost", "ice"}:
        sensory = "frost, muffled air, and one cracking impact"
    elif delivery_tokens & {"storm", "lightning", "thunder"}:
        sensory = "ozone, hard light, and one electrical impact"
    elif delivery_tokens & {"acid", "acidic", "corrosive", "poison", "toxin", "toxic",
                            "venom", "venomous"}:
        sensory = "visible hazardous material, a warning hiss, and one impact"
    elif delivery_tokens & {"gravity", "force", "telekinetic", "kinetic"}:
        sensory = "air pressure, visible distortion, and one focused impact"
    else:
        sensory = asserted_element[1] if asserted_element else \
            "a perceptible buildup, focused release, and one impact"
    close_shapes = {"touch", "punch", "strike", "slam", "kick", "slash", "thrust", "crush"}
    far_shapes = {"bolt", "blast", "gaze", "beam", "ray", "missile", "lance", "shard",
                  "spear", "orb", "arc", "needle", "fireball", "projectile"}
    range_ = "close" if manifestation in close_shapes else \
        "far" if manifestation in far_shapes else "near"
    counters = (["break the firing line", "use solid cover"] if range_ == "far" else
                ["leave the contact line", "guard its point of contact"] if range_ == "close" else
                ["leave the forming line", "interrupt the gathering force"])
    return {"shape": manifestation, "delivery": delivery, "label": delivery.title(),
            "range": range_, "sensory": sensory, "counterplay": counters}


def _manifested_moves(basis: str, profile_text: str,
                      asserted_element: tuple[str, str] | None = None, *, caster: bool = False
                      ) -> list[dict] | None:
    spec = _manifestation_spec(profile_text, asserted_element)
    if not spec:
        return None
    delivery = spec["delivery"]
    label = spec["label"]
    shape = spec["shape"]
    range_ = spec["range"]
    counters = (["break the spell line", "interrupt the casting"] if caster else
                spec["counterplay"])
    direct_timing = "spoken or gestured release" if caster else "fast"
    cadence = "casting" if caster else "reliable"
    return [
        _move(basis, "manifested_strike", label, delivery, range_, direct_timing, 0, 0,
              f"the {delivery} gathers onto one perceptible attack line", counters,
              spec["sensory"], cadence=cadence,
              forbid="an area effect, persistent element, status, or extra target",
              brace=range_ == "close"),
        _move(basis, "focused_strike", f"Focused {label}", delivery, range_, "measured",
              1, -1, f"the {shape} narrows onto one deliberate impact line", counters,
              spec["sensory"], cadence="casting" if caster else "recovery",
              forbid="an area effect, persistent element, status, or extra target",
              brace=range_ == "close"),
        _move(basis, "charged_strike", f"Committed {label}", delivery, range_,
              "charged wind-up", -1, 2,
              f"the {delivery} perceptibly builds before release", counters,
              spec["sensory"], cadence="setup", risk="obvious recovery after release",
              forbid="an area effect, persistent element, status, or extra target",
              brace=range_ == "close"),
    ]


def _natural_shape(text: str, armament: str) -> str:
    source: set[str] = set()
    for label, tokens in _armament_parts(armament):
        if _armament_has(label, _NATURAL):
            source.update(tokens & _NATURAL)
    # Explicit anatomy is stronger than a broad species silhouette: a serpent with named fangs
    # bites, while an unqualified serpent uses its serpentine body without invented claws.
    if source & {"fang", "fangs", "teeth", "bite"}:
        return "jawed"
    if source & {"stinger"}:
        return "stinger"
    if source & {"horn", "horns", "tusk", "tusks"}:
        return "horned"
    if source & {"claw", "claws", "talon", "talons"}:
        return "clawed"
    tokens = _tokens(f"{text} {armament}")
    if tokens & {"ooze", "slime", "amorphous"}:
        return "amorphous"
    if tokens & {"serpent", "snake", "cobra", "eel", "octopus", "tentacle", "tentacles"}:
        return "serpentine"
    if tokens & {"spider", "arachnid", "mandible", "mandibles"}:
        return "arachnid"
    if tokens & {"boar", "bull", "ram", "horn", "horns", "tusk", "tusks"}:
        return "horned"
    if tokens & {"stinger", "scorpion", "wasp"}:
        return "stinger"
    if tokens & {"fang", "fangs", "teeth", "bite", "wolf", "werewolf", "hound", "bear",
                  "lion", "tiger", "rat", "crocodile", "shark", "dragon", "wyvern"}:
        return "jawed"
    if tokens & {"claw", "claws", "talon", "talons", "crab"}:
        return "clawed"
    if tokens & {"wing", "wings", "winged", "beak", "bird", "avian", "eagle", "hawk",
                 "falcon", "raven", "phoenix"}:
        return "winged"
    return "body"


def _moves_for(basis: str, name: str, armament: str, profile_text: str) -> list[dict]:
    weapon = _source_for(armament, basis, "weapon")
    text = f"{name} {profile_text}".lower()
    if basis == "natural":
        shape = _natural_shape(text, armament)
        nonliving = bool(_tokens(text) & _NONLIVING)
        direct_by_shape = {
            "amorphous": ("Surging Slam", "amorphous body mass"),
            "serpentine": ("Body Lash", "coiling body and momentum"),
            "arachnid": ("Skittering Strike", "legs or body weight"),
            "horned": ("Driving Gore", "horns, tusks, or armored brow"),
            "stinger": ("Stinger Strike", "a grounded stinger"),
            "jawed": ("Bite", "jaws and teeth"),
            "clawed": ("Raking Strike", "grounded claws or talons"),
            "winged": ("Winged Strike", "wings, beak, talons, or body weight"),
            "body": ("Body Strike", "body weight or grounded anatomy"),
        }
        direct, fallback_source = direct_by_shape[shape]
        source = _source_for(armament, basis, fallback_source)
        approach = {
            "amorphous": ("Flowing Surge", "the amorphous mass bunches into one forward line",
                           "wet drag, surface spread, and accelerating mass"),
            "serpentine": ("Coiling Lunge", "the body coils into one forward line",
                            "scales or hide drag, then weight surges"),
            "arachnid": ("Skittering Rush", "the limbs gather beneath a direct approach",
                          "rapid footfalls and a sudden weight shift"),
            "horned": ("Committed Charge", "the head lowers into a straight charge line",
                       "scraping footing and accelerating weight"),
            "winged": ("Diving Pass", "the flight path narrows onto one target",
                       "wingbeats, displaced air, and descending weight"),
        }.get(shape, ("Body-Led Rush", "weight settles behind one direct approach",
                      "breath, footing, and accelerating weight"))
        close_name, close_tell = {
            "amorphous": ("Folding Impact", "the amorphous body folds around one impact line"),
            "horned": ("Hooking Gore", "the horns, tusks, or armored brow turn into a close arc"),
            "serpentine": ("Coiling Strike", "the body coils around one close attack line"),
            "arachnid": ("Close Skitter", "the legs gather for a short close-range strike"),
            "stinger": ("Stinger Feint", "the grounded stinger draws into one close attack line"),
            "winged": ("Close Wingbeat", "the wings and body tighten into a close pass"),
        }.get(shape, ("Clamping Strike", "the striking anatomy reaches for solid contact"))
        direct_sensory = ("surface motion, shifting mass, and impact" if shape == "amorphous" else
                          "joint motion, chassis strain, and impact" if nonliving else
                          "breath, muscle, and impact")
        approach_sensory = ("joint loading, hard footfalls, and accelerating mass"
                            if nonliving else approach[2])
        return [
            _move(basis, "strike", direct, source, "close", "fast", 0, 0,
                  "the striking anatomy lines up on the target",
                  ["guard the direct line", "create distance"], direct_sensory,
                  brace=True),
            _move(basis, "rushing_strike", approach[0], "body and forward momentum", "near",
                  "committed", -1, 2, approach[1],
                  ["break the approach line", "interrupt before contact"], approach_sensory,
                  risk="open recovery after the committed approach", brace=True),
            _move(basis, "clamping_strike", close_name, source, "close", "committed", 1, -1,
                  close_tell,
                  ["keep outside its reach", "brace against the impact"],
                  "scraping contact and dragged weight",
                  forbid="a restraint, infection, persistent status, or extra damage not in the receipt",
                  brace=True),
        ]
    if basis == "martial":
        weapon_tokens = _tokens(weapon)
        blunt = bool(weapon_tokens & {"mace", "hammer", "club", "maul", "staff", "shield",
                                      "flail", "crowbar", "baton"})
        flexible = bool(weapon_tokens & {"chain", "whip"})
        energized = bool(weapon_tokens & (_ENERGY | {"power", "vibro"}))
        electrical = bool(weapon_tokens & {"electric", "electrical", "electro", "shock",
                                           "tesla", "ion"})
        reach_weapon = bool(weapon_tokens & {"spear", "pike", "halberd", "glaive", "staff",
                                             "whip", "chain", "flail", "lance"})
        direct = "Measured Impact" if blunt else "Measured Lash" if flexible else "Measured Cut"
        contact = ("electrical contact" if electrical else "energized contact" if energized else
                   "blunt impact" if blunt else
                   "flexible impact" if flexible else "edge or point")
        direct_sensory = ("footwork, electrical crackle, ozone, and impact" if electrical else
                          f"footwork, {contact}, and recoil")
        return [
            _move(basis, "strike", direct, weapon, "close", "fast", 0, 0,
                  "the weapon settles onto a direct attack line",
                  ["guard the weapon line", "step outside its reach"], direct_sensory,
                  brace=True),
            _move(basis, "driving_strike", "Driving Advance", weapon, "close", "committed", 1, -1,
                  "the lead foot presses forward behind the weapon",
                  ["yield ground before contact", "angle off the advance"],
                  "footing, electrical crackle, and forward weight" if electrical else
                  "footing, charged contact motion, and forward weight" if energized else
                  "boots, steel, and forward weight",
                  forbid="forced movement or knockdown not in the receipt", brace=True),
            _move(basis, "heavy_strike", "Heavy Commitment", weapon,
                  "reach" if reach_weapon else "close", "slow wind-up", -1, 2,
                  "the weapon draws back and the torso commits to one powerful line",
                  ["interrupt the wind-up", "move beyond the committed arc"],
                  "strained grip, rising electrical crackle, and heavy impact" if electrical else
                  f"strained grip and heavy {contact}", cadence="setup",
                  risk="slow recovery after the swing", brace=True),
        ]
    if basis == "firearm":
        ftoks = _tokens(weapon)
        if "flamethrower" in ftoks:
            return [
                _move(basis, "jet_strike", "Flame Jet", weapon, "near", "direct release", 0, 0,
                      "the nozzle and pilot flame settle onto one direct line",
                      ["break line of effect", "use heat-resistant cover"],
                      "fuel hiss, ignition, and a short rush of flame",
                      forbid="burning status, persistent fire, terrain change, or extra targets"),
                _move(basis, "sweeping_strike", "Sweeping Flame", weapon, "near",
                      "committed sweep", -1, 1, "the nozzle begins tracing one visible arc",
                      ["leave the forming arc", "interrupt the fuel line"],
                      "fuel hiss and flame crossing one attack line", cadence="fuel",
                      risk="fuel use and an exposed sweeping motion",
                      forbid="a lasting fire zone, burning status, or extra targets"),
                _move(basis, "focused_strike", "Focused Burn", weapon, "near", "steady aim",
                      1, -1, "the nozzle steadies on one protected point",
                      ["break line of effect", "disrupt the steady aim"],
                      "focused heat, fuel hiss, and direct flame",
                      forbid="equipment destruction, a lasting burn, or a status"),
            ]
        if ftoks & {"launcher"}:
            return [
                _move(basis, "launched_strike", "Direct Launch", weapon, "far", "fast", 0, 0,
                      "the launcher aligns one projectile path on the target",
                      ["take hard cover", "break the firing solution"],
                      "launch report, projectile flight, and focused impact",
                      forbid="an area blast, extra targets, persistent fire, or a status"),
                _move(basis, "bracketed_strike", "Bracketed Launch", weapon, "far",
                      "measured aim", 1, -1, "the firing angle is corrected onto one narrow path",
                      ["change position before launch", "disrupt the aim"],
                      "sight adjustment, launch, and one focused impact", cadence="ammo",
                      risk="limited ammunition and visible firing position",
                      forbid="an area blast, suppression, or extra targets"),
                _move(basis, "precision_strike", "Aimed Warhead", weapon, "far", "slow aim",
                      -1, 2, "the launcher holds a precise firing solution",
                      ["break line of sight", "interrupt before launch"],
                      "still aim, launch report, and concentrated impact", cadence="setup",
                      risk="slow recovery and scarce ammunition",
                      forbid="an area blast, terrain destruction, or extra targets"),
            ]
        if "stun" in ftoks and "gun" in ftoks:
            return [
                _move(basis, "discharge_strike", "Stun-Gun Discharge", weapon, "near", "fast",
                      0, 0, "the electrodes align on one direct firing line",
                      ["break the firing line", "use insulating cover"],
                      "electrical snap and one direct impact",
                      forbid="stun, paralysis, a status, or persistent current"),
                _move(basis, "tracking_strike", "Tracked Discharge", weapon, "near", "tracking",
                      1, -1, "the electrodes track one target's line",
                      ["break the firing line", "use solid cover"],
                      "electrical crackle and one direct impact", cadence="charge",
                      forbid="lock-on, stun, paralysis, or a status"),
                _move(basis, "charged_strike", "Committed Discharge", weapon, "near",
                      "charged wind-up", -1, 2, "the electrical delivery visibly charges",
                      ["interrupt the charge", "leave the fixed line"],
                      "rising electrical tone and one hard discharge", cadence="setup",
                      risk="slow recovery", forbid="stun, paralysis, or persistent current"),
            ]
        energy = bool(ftoks & _ENERGY)
        sensory = ("charge snap, hard light, and heated air" if energy else
                   "weapon report, recoil, and projectile impact")
        automatic = bool(ftoks & {"smg", "autogun", "machinegun", "assault", "automatic",
                                  "submachine", "burst", "gatling"}) \
            or {"machine", "gun"} <= ftoks or {"select", "fire"} <= ftoks \
            or {"full", "auto"} <= ftoks or {"belt", "fed"} <= ftoks \
            or {"machine", "pistol"} <= ftoks
        single_shot = "musket" in ftoks
        second_name = ("Suppressing Burst" if automatic else
                       "Braced Shot" if single_shot else "Follow-up Shot")
        second_timing = "sustained" if automatic else "measured"
        second_tell = ("the weapon tracks a lane rather than a single point" if automatic else
                       "the weapon settles for a second deliberate firing line")
        second_sensory = ("repeated discharges and impacts along one lane" if automatic else
                          sensory)
        return [
            _move(basis, "strike", "Controlled Shot", weapon, "far", "fast", 0, 0,
                  "the muzzle and sights settle on the target",
                  ["take hard cover", "break line of sight"], sensory),
            _move(basis, "burst_strike" if automatic else "followup_strike", second_name,
                  weapon, "far", second_timing, 1, -1, second_tell,
                  ["leave the covered lane", "use solid cover"] if automatic else
                  ["break the renewed firing line", "use hard cover"], second_sensory,
                  cadence="ammo" if automatic else "reload" if single_shot else "reliable",
                  risk="ammo use and visible firing position",
                  forbid="forced movement, pinning, or extra targets not in the receipt"),
            _move(basis, "precision_strike", "Aimed Shot", weapon, "far", "slow aim", 1, 1,
                  "breathing stills while the sights hold one narrow line",
                  ["break line of sight", "disrupt the aim"], sensory,
                  cadence="setup", risk="tunnel vision during the aim"),
        ]
    if basis == "projectile":
        ptoks = _tokens(weapon)
        crossbow = "crossbow" in ptoks
        javelin = "javelin" in ptoks
        sling = "sling" in ptoks
        blowgun = "blowgun" in ptoks
        dart = "dart" in ptoks or "darts" in ptoks
        thrown_implement = "throwing" in ptoks
        shuriken = "shuriken" in ptoks
        boomerang = "boomerang" in ptoks
        harpoon = "harpoon" in ptoks
        if crossbow:
            names = ("Quick Bolt", "Braced Bolt", "Aimed Bolt")
            direct_sensory = "trigger click, bolt flight, and one impact"
            aimed_tell = "the stock locks into a stable firing position"
            aimed_sensory = "stock tension, trigger release, and bolt flight"
            range_ = "far"
        elif blowgun:
            names = ("Quick Blowgun Dart", "Measured Blowgun Dart", "Aimed Blowgun Dart")
            direct_sensory = "breath pressure, dart flight, and one impact"
            aimed_tell = "the blowgun steadies onto one deliberate breath-driven line"
            aimed_sensory = "held breath, pressure release, and dart flight"
            range_ = "near"
        elif javelin or dart or thrown_implement or shuriken or boomerang or harpoon:
            thrown_noun = next((label for token, label in (
                ("axe", "Throwing Axe"), ("hatchet", "Throwing Hatchet"),
                ("knife", "Throwing Knife"), ("dagger", "Throwing Dagger"),
                ("spear", "Throwing Spear"),
            ) if token in ptoks), "Thrown Weapon")
            noun = ("Javelin" if javelin else "Dart" if dart else
                    "Shuriken" if shuriken else "Boomerang" if boomerang else
                    "Harpoon" if harpoon else thrown_noun)
            names = (f"Quick {noun} Cast", f"Measured {noun} Cast", f"Committed {noun} Cast")
            direct_sensory = "grip release, projectile flight, and one impact"
            aimed_tell = "the throwing stance fixes on one deliberate release line"
            aimed_sensory = "footing, throwing motion, and projectile flight"
            range_ = "near"
        elif sling:
            names = ("Quick Sling", "Measured Sling", "Aimed Sling")
            direct_sensory = "sling rotation, release, and one impact"
            aimed_tell = "the sling's rotation steadies onto one release line"
            aimed_sensory = "measured rotation, release, and projectile flight"
            range_ = "far"
        else:
            names = ("Quick Shot", "Follow-up Shot", "Drawn Shot")
            direct_sensory = "string motion, projectile flight, and one impact"
            aimed_tell = "the draw reaches full commitment"
            aimed_sensory = "string tension, stillness, and release"
            range_ = "far"
        return [
            _move(basis, "strike", names[0], weapon, range_, "fast", 0, 0,
                  "the projectile is brought onto a direct line",
                  ["take cover", "close inside the firing line"], direct_sensory),
            _move(basis, "braced_strike", names[1], weapon, range_, "measured", 1, -1,
                  "the weapon settles onto one deliberate release line",
                  ["break the firing line", "use solid cover"],
                  direct_sensory, cadence="reload" if crossbow else "ammo",
                  forbid="pinning or extra targets not in the receipt"),
            _move(basis, "precision_strike", names[2], weapon, range_, "slow aim", -1, 2,
                  aimed_tell,
                  ["interrupt before release", "break the firing angle"], aimed_sensory,
                  cadence="setup", risk="open recovery after release"),
        ]
    if basis == "technology":
        ttoks = _tokens(f"{text} {armament}")
        delivery = weapon if weapon != "weapon" else _source_for(
            profile_text, basis, "combat-capable actuators")
        source_tokens = _tokens(delivery)
        energy = bool(source_tokens & _ENERGY)
        energy_contact = energy and bool(source_tokens & (_MARTIAL | {"baton"}))
        electrical = bool(source_tokens & {"electric", "electrical", "electro", "shock",
                                           "tesla", "ion"})
        flexible_contact = energy_contact and bool(source_tokens & {"chain", "whip"})
        if ttoks & {"turret"}:
            delivery = weapon if armament else "the turret's mounted weapon"
            return [
                _move(basis, "mounted_strike", "Tracked Shot", delivery, "far", "fast", 0, 0,
                      "the mount traverses and fixes one firing line",
                      ["take hard cover", "move outside the traverse arc"],
                      "motor pitch, weapon discharge, and impact"),
                _move(basis, "sweeping_strike", "Traverse Burst", delivery, "far", "sustained",
                      1, -1, "the mount begins sweeping one lane",
                      ["leave the firing lane", "use solid cover"],
                      "traverse motors and repeated impacts", cadence="ammo",
                      forbid="forced movement, a status, or extra targets not in the receipt"),
                _move(basis, "charged_strike", "Charged Discharge", delivery, "far",
                      "charged wind-up", -1, 2, "the fixed mount draws power into one line",
                      ["interrupt its power", "leave the fixed attack line"],
                      "rising motor tone, heat, and discharge", cadence="setup",
                      risk="heat and slow traverse after discharge"),
            ]
        if ttoks & {"nanite", "nanotech"}:
            return [
                _move(basis, "swarm_strike", "Abrasive Swarm", "a contained micro-machine swarm",
                      "near", "sustained", 0, 0, "the cloud contracts toward one exposed line",
                      ["seal exposed surfaces", "leave the cloud's reach"],
                      "metallic haze, fine impacts, and surface scoring",
                      forbid="infection, disassembly, a persistent cloud, or extra targets"),
                _move(basis, "focused_strike", "Concentrated Scour", "a narrow micro-machine stream",
                      "near", "committed", 1, -1, "the haze narrows onto a single point",
                      ["break the stream", "interpose a sealed barrier"],
                      "a tightening whine and concentrated surface impacts",
                      forbid="equipment destruction, a status, or persistent damage"),
                _move(basis, "surging_strike", "Swarm Surge", "the mass of the micro-machine cloud",
                      "near", "charged", -1, 2, "the scattered cloud gathers into a dense front",
                      ["disperse it before contact", "move beyond the gathering front"],
                      "rising static, metallic haze, and a sudden impact", cadence="setup",
                      risk="the swarm disperses after contact"),
            ]
        if ttoks & {"drone"} and not armament:
            return [
                _move(basis, "ramming_strike", "Ramming Pass", "a reinforced mobile chassis",
                      "near", "fast", 0, 0, "the drone aligns its whole flight path on one target",
                      ["break the approach line", "use a solid obstacle"],
                      "motor pitch, displaced air, and hard impact"),
                _move(basis, "diving_strike", "Committed Dive", "the drone's chassis and momentum",
                      "near", "committed", 1, -1, "altitude and angle lock into a steep approach",
                      ["leave the dive line", "interrupt its approach"],
                      "rising motor whine and accelerating air", risk="wide recovery arc"),
                _move(basis, "charged_strike", "Full-Thrust Impact", "overdriven propulsion",
                      "near", "charged wind-up", -1, 2, "the drive rises beyond its normal pitch",
                      ["disrupt the charge", "put hard cover in its path"],
                      "heat, motor strain, and a violent impact", cadence="setup",
                      risk="heat and slow recovery"),
            ]
        ranged_delivery = bool(source_tokens & (
            _FIREARM | _PROJECTILE | {"cannon", "emitter", "projector", "launcher", "missile",
                                      "eye", "phaser", "beam", "ray"}))
        ranged = ranged_delivery and not energy_contact
        thrown_ranged = ranged and bool(source_tokens & {"throwing", "javelin", "dart", "darts"})
        range_ = "near" if thrown_ranged else "far" if ranged else "close"
        first_name = "Integrated Throw" if thrown_ranged else (
            "Capacitor Shot" if ranged and energy else "Mounted Shot") if ranged else (
            "Energized Lash" if flexible_contact else
            "Energized Cut" if energy_contact else "Servo Strike")
        follow_name = "Tracked Throw" if thrown_ranged else (
            "Emitter Follow-up" if energy else "Tracked Follow-up") if ranged else (
            "Powered Follow-Through" if energy_contact else "Driven Follow-Through")
        follow_tell = ("the integrated throwing delivery corrects onto one release line"
                       if thrown_ranged else "the emitter corrects onto the same attack line" if ranged else
                       "the powered delivery recovers into one follow-through line")
        follow_counters = (["break line of effect", "use solid cover"] if ranged else
                           ["move outside its reach", "guard the contact line"])
        return [
            _move(basis, "strike", first_name, delivery,
                  range_, "fast", 0, 0,
                  "electrodes crackle along the contact line" if electrical and energy_contact else
                  "an emitter brightens" if ranged and energy else
                  "the ranged delivery aligns on the target" if ranged else
                  "the powered delivery aligns on the target",
                  ["break line of effect", "use cover"] if ranged else
                  ["guard the contact line", "move outside its reach"],
                  "electrical crackle, ozone, and impact" if electrical else
                  "charge whine and hard light" if energy else
                  "mechanical alignment, discharge, and impact" if ranged else
                  "motor pitch, metal movement, and impact",
                  brace=not ranged),
            _move(basis, "followup_strike", follow_name, delivery,
                  range_, "tracking", 1, -1,
                  follow_tell, follow_counters,
                  "electrical crackle followed by a concrete impact" if electrical else
                  "emitter movement followed by a concrete impact" if ranged else
                  "powered movement followed by a concrete impact",
                  forbid="a hack, lock-on status, or extra target not in the receipt",
                  brace=not ranged),
            _move(basis, "charged_strike", "Committed Throw" if thrown_ranged else
                  ("Overcharged Shot" if energy else
                  "Committed Shot") if ranged else
                  "Overcharged Contact", delivery,
                  range_,
                  "charged wind-up", -1, 2, "power rises above the normal operating pitch",
                  ["interrupt the charge", "leave the fixed attack line"],
                  "rising electrical tone, ozone, and impact" if electrical else
                  "heat, rising tone, and discharge",
                  cadence="setup", risk="heat and slow recovery", brace=not ranged),
        ]
    if basis == "magic":
        if any(_positive_token(text, {token})
               for token in {"pyromancer", "fire", "flame", "ember"}):
            force, sensory = "focused fire", "heat, ember light, and rushing flame"
            asserted_element = ("fire", sensory)
        elif any(_positive_token(text, {token})
                 for token in {"cryomancer", "ice", "frost", "cold"}):
            force, sensory = "focused cold", "frost, muffled air, and cracking ice"
            asserted_element = ("cold", sensory)
        elif any(_positive_token(text, {token})
                 for token in {"storm", "lightning", "thunder"}):
            force, sensory = "focused lightning", "ozone, hard light, and electrical snap"
            asserted_element = ("lightning", sensory)
        elif _positive_token(text, {"shadow"}):
            force, sensory = "focused shadow", "light loss, cold pressure, and dark motion"
            asserted_element = ("shadow", sensory)
        elif _positive_token(text, {"radiant"}):
            force, sensory = "focused radiance", "hard light, heat, and luminous impact"
            asserted_element = ("radiance", sensory)
        else:
            force, sensory = "shaped arcane force", "gathering light, pressure, and discharge"
            asserted_element = None
        # A named attack shape owns its own payload.  The profile-wide element is only the
        # fallback for generic spell kits; otherwise a disabled neighbouring clause such as
        # "fire blast is powerless" can repaint an independently active gravity wave.
        if manifested := _manifested_moves(basis, profile_text, None, caster=True):
            return manifested
        return [
            _move(basis, "focused_strike", "Focused Spell", force, "far", "spoken or gestured release", 0, 0,
                  "the caster gathers one narrow line of power",
                  ["break the spell line", "interrupt the casting"], sensory,
                  cadence="casting", forbid="another element, spell, status, or target not in the receipt"),
            _move(basis, "sweeping_strike", "Sweeping Spell", force, "near", "broad wind-up", -1, 1,
                  "the casting gesture widens across a visible area",
                  ["leave the forming area", "interrupt before release"], sensory,
                  cadence="setup", risk="longer, obvious casting window",
                  forbid="persistent terrain, status, or extra targets not in the receipt"),
            _move(basis, "piercing_strike", "Piercing Working", force, "near", "precise casting", 1, -1,
                  "the caster fixes a narrow attack on one exposed opening",
                  ["break concentration", "deny line of effect"], sensory,
                  cadence="casting",
                  forbid="dispel, silence, equipment damage, a status, or extra damage"),
        ]
    if basis == "supernatural":
        stoks = _tokens(text)
        incorporeal = bool(stoks & {"spirit", "specter", "spectre", "ghost", "wraith"})
        element_groups = (
            ({"fire", "flame", "ember"}, "fire", "heat, flame motion, and impact"),
            ({"cold", "frost", "ice"}, "cold", "frost, cracking ice, and impact"),
            ({"storm", "lightning", "thunder"}, "lightning",
             "ozone, electrical snap, and impact"),
            ({"shadow"}, "shadow", "light loss, cold pressure, and dark impact"),
            ({"radiant"}, "radiance", "hard light, heat, and luminous impact"),
        )
        asserted_element = next(((label, sense) for tokens, label, sense in element_groups
                                 if any(_positive_token(text, {token}) for token in tokens)), None)
        if manifested := _manifested_moves(basis, profile_text, None):
            return manifested
        elemental = "elemental" in stoks or asserted_element is not None
        delivery = ("manifested incorporeal force" if incorporeal else
                    f"manifested {asserted_element[0]}" if elemental and asserted_element else
                    "manifested elemental force" if elemental else
                    "grounded supernatural force or anatomy")
        movement = ("its manifestation gathers toward one target" if incorporeal else
                    "its visible force aligns on one target")
        sensory = ("air pressure, translucent motion, and impact" if incorporeal else
                   asserted_element[1] if elemental and asserted_element else
                   "shifting matter, pressure, and impact" if elemental else
                   "pressure, visible manifestation, and unnatural motion")
        return [
            _move(basis, "lash_strike", "Otherworldly Lash", delivery,
                  "near", "fast", 0, 0, movement,
                  ["break its direct path", "use a grounded barrier"], sensory,
                  forbid="an element, teleport, curse, or status not in the receipt"),
            _move(basis, "pulse_strike", "Manifested Pulse", "a short supernatural pulse",
                  "near", "charged", -1, 1,
                  "the surrounding air draws inward around the entity",
                  ["leave the forming radius", "interrupt the gathering force"],
                  "air pressure, visible distortion, and a sudden pulse", cadence="setup",
                  forbid="fear, curse, possession, a status, or extra targets not in the receipt"),
            _move(basis, "lunge_strike", "Manifested Surge", delivery, "near",
                  "committed", 1, -1, "its manifested force compresses into one approach line",
                  ["break the lunge line", "intercept before extension"], sensory,
                  forbid="teleportation or phasing not in the receipt"),
        ]
    if basis == "undead":
        ravenous = bool(_tokens(text) & {"zombie", "ghoul", "mummy", "corpse"})
        direct = "Ravenous Bite" if ravenous else "Bone Strike"
        delivery = "teeth and body weight" if ravenous else "rigid limbs and dead weight"
        tell = ("the jaw works while the head fixes on exposed flesh" if ravenous else
                "the dead frame squares one rigid limb onto an attack line")
        return [
            _move(basis, "strike", direct, delivery, "close", "fast", 0, 0, tell,
                  ["keep beyond grabbing distance", "guard the head and shoulders"],
                  "ragged motion, hard contact, and dead weight",
                  forbid="infection, transformation, or status not in the receipt", brace=True),
            _move(basis, "grasping_strike", "Grasping Lunge", "hands and forward body weight", "close",
                  "committed", 1, -1, "both hands spread to seize rather than strike",
                  ["stay outside the grasp", "brace against the lunge"], "scraping hands and dragging weight",
                  forbid="persistent restraint, infection, or extra damage not in the receipt", brace=True),
            _move(basis, "battering_strike", "Relentless Rush", "repeated body impacts", "near", "sustained",
                  -1, 1, "the corpse leans into a direct battering path",
                  ["redirect the rush", "use a barrier or elevation"], "foot drag, impacts, and splintering force",
                  risk="poor recovery and exposed flanks", forbid="barrier destruction not in the receipt",
                  brace=True),
        ]
    if basis == "hazard":
        source = _source_for(armament, basis, "") or _source_for(
            profile_text, basis, "grounded hazardous anatomy")
        source_tokens = _tokens(source)
        projectile_delivery = bool(source_tokens & (_PROJECTILE | {"arrow", "arrows", "bolt",
                                                    "bolts", "dart", "darts", "bullet", "bullets"}))
        remote_delivery = {"breath", "spit", "sprayer", "projector", "emitter", "cannon", "gun",
                           "rifle", "launcher", "arrow", "arrows", "bolt", "bolts", "dart",
                           "darts", "bullet", "bullets", "grenade", "bomb", "cloud", "cone",
                           "torrent", "spray", "stream"} | _PROJECTILE
        contact = bool(source_tokens & (_MARTIAL | {"fang", "fangs", "stinger", "touch",
                                                   "claw", "claws", "talon", "talons", "skin",
                                                   "blood", "gland", "glands", "body"})) \
            and not bool(source_tokens & remote_delivery)
        if contact:
            return [
                _move(basis, "contact_strike", "Tainted Strike", source, "close", "fast", 0, 0,
                      "the contaminated edge or anatomy lines up for direct contact",
                      ["deny contact", "guard the delivery line"],
                      "a warning sheen, sharp odor, and physical impact",
                      forbid="persistent poison, radiation dose, corrosion, or status",
                      brace=True),
                _move(basis, "driving_strike", "Hazardous Drive", source, "close", "committed", 1, -1,
                      "the hazardous contact source presses into one attack line",
                      ["leave its reach", "interpose sealed protection"],
                      "surface reaction and committed physical force",
                      forbid="armor destruction, a lasting dose, or a status", brace=True),
                _move(basis, "heavy_strike", "Saturating Impact", source, "close", "slow wind-up", -1, 2,
                      "the contaminated delivery draws back for one heavy contact",
                      ["interrupt the wind-up", "move beyond contact range"],
                      "hazard warning signs followed by heavy impact", cadence="setup",
                      risk="slow recovery", forbid="a lasting zone, dose, or condition",
                      brace=True),
            ]
        payload = bool(source_tokens & {"grenade", "bomb", "molotov", "flask", "bottle"})
        if payload:
            range_ = "far" if "launcher" in source_tokens else "near"
            return [
                _move(basis, "payload_strike", "Hazard Payload", source, range_, "fast", 0, 0,
                      "the payload settles onto one visible release line",
                      ["break the release line", "use sealed hard cover"],
                      "release motion, payload flight, and one focused impact",
                      forbid="an area blast, lasting hazard, status, terrain damage, or extra target"),
                _move(basis, "arcing_strike", "Arced Hazard Payload", source, range_, "measured",
                      1, -1, "the payload's release angle rises into one deliberate arc",
                      ["leave the forming arc", "interrupt before release"],
                      "measured release, payload flight, and one focused impact",
                      forbid="an area blast, lasting hazard, status, terrain damage, or extra target"),
                _move(basis, "committed_strike", "Committed Hazard Payload", source, range_,
                      "slow wind-up", -1, 2, "the whole delivery commits to one fixed payload path",
                      ["interrupt the commitment", "break the fixed path"],
                      "strained release, payload flight, and concentrated impact", cadence="setup",
                      risk="open recovery after release",
                      forbid="an area blast, lasting hazard, status, terrain damage, or extra target"),
            ]
        blowgun_delivery = "blowgun" in source_tokens
        thrown_projectile = not blowgun_delivery and bool(source_tokens & {
            "javelin", "dart", "darts", "throwing", "knife", "harpoon", "shuriken",
            "boomerang",
        })
        far = bool(source_tokens & {"projector", "emitter", "rifle", "gun", "cannon", "bow",
                                    "crossbow", "sling",
                                    "launcher", "arrow", "arrows", "bolt", "bolts", "dart",
                                    "darts", "bullet", "bullets"}) \
            and not thrown_projectile and not blowgun_delivery
        range_ = "far" if far else "near"
        spit_delivery = "spit" in source_tokens
        breath_delivery = bool(source_tokens & {"breath", "breathe", "exhale"})
        if projectile_delivery:
            if source_tokens & {"radiation", "radioactive", "irradiated", "nuclear"}:
                label = "Irradiated"
            elif source_tokens & {"acid", "acidic", "corrosive"}:
                label = "Corrosive"
            else:
                label = "Toxic"
            noun = "Throw" if thrown_projectile else "Shot"
            direct_name, sweep_name, focus_name = (
                f"{label} {noun}", f"{label} Follow-up", f"Focused {label} {noun}")
            direct_sensory = ("breath pressure, dart flight, visible hazardous material, and one impact"
                              if blowgun_delivery else
                              "projectile flight, visible hazardous material, and one impact")
            sweep_sensory = "a renewed projectile release and one hazardous impact"
            focus_sensory = "a held firing line, projectile flight, and concentrated impact"
            warning = ("the blowgun steadies onto one visible breath-driven line"
                       if blowgun_delivery else
                       "the hazardous projectile settles onto one visible firing line")
        elif spit_delivery:
            if source_tokens & {"radiation", "radioactive", "irradiated", "nuclear"}:
                label = "Radiation"
                direct_sensory = "instrument crackle, projected hazardous fluid, and one impact"
            elif source_tokens & {"acid", "acidic", "corrosive"}:
                label = "Corrosive"
                direct_sensory = "warning hiss, projected corrosive fluid, and one impact"
            else:
                label = "Toxic"
                direct_sensory = "sharp odor, projected hazardous fluid, and one impact"
            direct_name, sweep_name, focus_name = (
                f"{label} Spit", f"Sweeping {label} Spit", f"Focused {label} Spit")
            sweep_sensory = "projected hazardous fluid traces one visible attack arc"
            focus_sensory = "the projected fluid narrows into one concentrated impact line"
            warning = "the hazardous spit gathers behind one visible target line"
        elif breath_delivery:
            if source_tokens & {"radiation", "radioactive", "irradiated", "nuclear"}:
                label = "Radiation"
                direct_sensory = "instrument crackle, exhaled force, and one hard impact"
            elif source_tokens & {"acid", "acidic", "corrosive"}:
                label = "Corrosive"
                direct_sensory = "warning hiss, exhaled spray, and one corrosive impact"
            else:
                label = "Toxic"
                direct_sensory = "warning hiss, sharp odor, and one exhaled impact"
            direct_name, sweep_name, focus_name = (
                f"{label} Breath", f"Sweeping {label} Breath", f"Focused {label} Breath")
            sweep_sensory = "breath and visible hazardous material trace one attack arc"
            focus_sensory = "the breath narrows into one concentrated impact line"
            warning = "the hazardous breath gathers onto one visible target line"
        elif source_tokens & {"radiation", "radioactive", "irradiated", "nuclear"}:
            direct_name, sweep_name, focus_name = (
                "Radiation Pulse", "Radiation Sweep", "Concentrated Radiation")
            direct_sensory = "instrument crackle, ionized air, and one hard pulse"
            sweep_sensory = "instrument chatter, ionized air, and a traversing pulse"
            focus_sensory = "rising instrument crackle and a concentrated pulse"
            warning = "instrument readings spike as the source aligns on one target"
        elif source_tokens & {"acid", "acidic", "corrosive"}:
            direct_name, sweep_name, focus_name = (
                "Corrosive Jet", "Corrosive Sweep", "Concentrated Spray")
            direct_sensory = "warning hiss, sharp odor, and a direct corrosive impact"
            sweep_sensory = "hiss, visible spray, and surface reaction along one arc"
            focus_sensory = "a narrowing hiss and concentrated surface reaction"
            warning = "the corrosive delivery aligns on one target"
        elif source_tokens & {"toxin", "toxic", "poison", "venom", "venomous", "chemical",
                              "biohazard", "pepper", "irritant"}:
            direct_name, sweep_name, focus_name = (
                "Toxic Jet", "Toxic Sweep", "Concentrated Jet")
            direct_sensory = "warning hiss, sharp odor, and one physical impact"
            sweep_sensory = "hiss, visible droplets, and impacts along one arc"
            focus_sensory = "a narrowing hiss and one concentrated impact"
            warning = "the hazardous delivery aligns on one target"
        else:
            direct_name, sweep_name, focus_name = (
                "Contaminant Jet", "Hazardous Sweep", "Concentrated Release")
            direct_sensory = "warning hiss, instrument crackle, and one physical impact"
            sweep_sensory = "warning signs and impacts along one visible arc"
            focus_sensory = "a narrowing warning tone and one concentrated impact"
            warning = "the hazardous delivery aligns on one target"
        return [
            _move(basis, "jet_strike", direct_name, source, range_, "direct release", 0, 0,
                  warning,
                  ["break line of effect", "use sealed cover"], direct_sensory,
                  forbid="persistent contamination, poison, radiation dose, status, or extra target not in the receipt"),
            _move(basis, "sweeping_strike", sweep_name, source, range_, "sweeping release", -1, 1,
                  "the delivery source begins tracing a wider arc",
                  ["leave the forming arc", "interrupt or block the source"],
                  sweep_sensory, cadence="setup",
                  forbid="a lasting zone, dose, mutation, or extra target not in the receipt"),
            _move(basis, "focused_strike", focus_name, source, range_, "committed", 1, -1,
                  "the hazardous delivery narrows onto one protected point",
                  ["break the focused line", "use sealed cover"], focus_sensory,
                  forbid="armor destruction, a persistent condition, or extra target not in the receipt"),
        ]
    physical_parts = _armament_parts(armament)
    usable_parts = [label for label, _tokens_ in physical_parts
                    if not _receipt_only_delivery(label)]
    if usable_parts:
        delivery = _text(armament, 160) if len(usable_parts) == len(physical_parts) \
            else usable_parts[0]
        tokens = _tokens(delivery)
        thrown = bool(tokens & {"grenade", "bomb", "molotov", "throwing", "thrown"})
        taser = "taser" in tokens
        if thrown:
            return [
                _move("physical", "thrown_strike", "Direct Throw", delivery, "near", "fast",
                      0, 0, "the implement is brought onto one visible throwing line",
                      ["break the throwing line", "use solid cover"],
                      "throwing motion, flight, and one focused impact",
                      forbid="an area blast, persistent fire, terrain damage, or extra targets"),
                _move("physical", "arcing_strike", "Arced Throw", delivery, "near", "measured",
                      1, -1, "the throwing angle rises into a deliberate arc",
                      ["move outside the arc", "interrupt before release"],
                      "measured motion, flight, and one focused impact",
                      forbid="an area blast, persistent fire, terrain damage, or extra targets"),
                _move("physical", "committed_strike", "Committed Throw", delivery, "near",
                      "slow wind-up", -1, 2, "the whole body commits behind one release",
                      ["interrupt the wind-up", "break the release line"],
                      "strained motion, flight, and concentrated impact", cadence="setup",
                      risk="open recovery after release",
                      forbid="an area blast, persistent fire, terrain damage, or extra targets"),
            ]
        if taser:
            return [
                _move("physical", "strike", "Taser Discharge", delivery, "near", "fast", 0, 0,
                      "the taser aligns on one direct line",
                      ["break line of effect", "use insulating cover"],
                      "electrical snap and one direct impact",
                      forbid="stun, paralysis, a status, or persistent current"),
                _move("physical", "followup_strike", "Tracked Discharge", delivery, "near",
                      "tracking", 1, -1, "the taser follows one target's line",
                      ["break line of effect", "use solid cover"],
                      "electrical crackle and one direct impact",
                      forbid="lock-on, stun, paralysis, or a status"),
                _move("physical", "charged_strike", "Committed Discharge", delivery, "near",
                      "charged wind-up", -1, 2, "the electrical delivery visibly charges",
                      ["interrupt the charge", "leave the fixed line"],
                      "rising electrical tone and one hard discharge", cadence="setup",
                      risk="slow recovery", forbid="stun, paralysis, or persistent current"),
            ]
        return [
            _move("physical", "strike", "Improvised Strike", delivery, "close", "fast", 0, 0,
                  "the carried implement settles onto a direct attack line",
                  ["guard the implement line", "create distance"],
                  "footwork, implement movement, and impact", brace=True),
            _move("physical", "driving_strike", "Driving Implement", delivery, "close",
                  "committed", 1, -1, "the lead foot presses behind the carried implement",
                  ["angle off the advance", "move outside its reach"],
                  "footing, implement contact, and forward weight", brace=True,
                  forbid="forced movement or knockdown not in the receipt"),
            _move("physical", "heavy_strike", "Heavy Implement Swing", delivery, "close",
                  "slow wind-up", -1, 2, "the implement draws back for one heavy effort",
                  ["interrupt the wind-up", "move beyond the committed arc"],
                  "strained grip and one heavy impact", cadence="setup",
                  risk="slow recovery", brace=True),
        ]
    return [
        _move("physical", "strike", "Direct Blow", "body or improvised force", "close", "fast",
              0, 0, "weight shifts onto a direct attack line",
              ["guard the line", "create distance"], "footwork, breath, and impact", brace=True),
        _move("physical", "rushing_strike", "Driving Rush", "body weight", "close", "committed",
              -1, 1, "the attacker lowers their center and drives forward",
              ["angle away", "brace or intercept"], "heavy steps and forward impact",
              forbid="forced movement or knockdown not in the receipt", brace=True),
        _move("physical", "heavy_strike", "Committed Swing", "body or improvised force", "close",
              "slow wind-up", -1, 2, "the whole body draws back for one heavy effort",
              ["interrupt the wind-up", "move outside the arc"], "strained movement and a heavy impact",
              cadence="setup", risk="slow recovery", brace=True),
    ]


def _role_axis(name: str, identity: Mapping[str, Any] | None) -> tuple[str, set[str]]:
    """Collapse free-form combat roles into a small preference axis, never a move grant."""
    role_text = " ".join([name, *sum((_field_texts(identity, key)
                                      for key in ("role", "class", "descriptor")), [])])
    role_tokens = _tokens(role_text)
    for labels, primitives in _ROLE_PRIMITIVES:
        if labels & role_tokens:
            return sorted(labels & role_tokens)[0], primitives
    return "general", set()


def build_enemy_kit(name: str, tier: str = "standard", armament: str = "",
                    identity: Mapping[str, Any] | None = None) -> dict:
    """Compose and return a bounded frozen kit from grounded identity/equipment facts."""
    name = _text(name, 80) or "Foe"
    armament = _text(armament, 160)
    tier_key = _text(tier, 20).lower()
    tier = tier_key if tier_key in _TIER_MOVES else "standard"
    bases, grounding, profile_text = _basis(name, armament, identity)
    by_basis: dict[str, list[dict]] = {}
    for basis in bases:
        by_basis[basis] = _moves_for(basis, name, armament, profile_text)
    candidates = [move for basis in bases for move in by_basis[basis]]

    cap = _TIER_MOVES[tier]
    signature = max(bases, key=lambda basis: _SIGNATURE_PRIORITY.get(basis, 0))
    role, role_primitives = _role_axis(name, identity)
    selected: list[dict] = []
    used_primitives: set[str] = set()
    used_bases: set[str] = set()
    # The extraordinary/identity-defining basis leads.  Each other grounded basis then gets one
    # seat while capacity permits; remaining seats reinforce the signature and stated combat role.
    seat_order = [signature, *sorted((basis for basis in bases if basis != signature),
                                     key=lambda basis: _SIGNATURE_PRIORITY.get(basis, 0),
                                     reverse=True)]
    for basis in seat_order[:cap]:
        pool = by_basis.get(basis) or []
        if not pool:
            continue
        move = max(enumerate(pool), key=lambda item: (
            4 if item[1]["primitive"] in role_primitives else 0,
            2 if item[1]["primitive"] not in used_primitives else 0,
            1 if item[1].get("cadence") == "reliable" else 0,
            -item[0]))[1]
        selected.append({**move, "danger": _TIER_DANGER[tier]})
        used_primitives.add(move["primitive"])
        used_bases.add(move["basis"])

    remaining = list(enumerate(candidates))
    while remaining and len(selected) < cap:
        def score(item: tuple[int, dict]) -> tuple[int, int]:
            idx, move = item
            value = (8 if move["basis"] == signature else 0) \
                + (5 if move["primitive"] not in used_primitives else 0) \
                + (4 if move["primitive"] in role_primitives else 0) \
                + (3 if move["basis"] not in used_bases else 0)
            return value, -idx

        pos, move = max(remaining, key=score)
        remaining = [(i, m) for i, m in remaining if i != pos and m["id"] != move["id"]]
        if move["id"] in {m["id"] for m in selected}:
            continue
        selected.append({**move, "danger": _TIER_DANGER[tier]})
        used_primitives.add(move["primitive"])
        used_bases.add(move["basis"])

    frozen = {"schema": KIT_SCHEMA, "generator": GENERATOR_VERSION, "tier": tier,
              "signature_basis": signature, "role_axis": role, "basis": bases,
              "grounding": grounding, "moves": selected}
    material = json.dumps(frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fingerprint = hashlib.blake2b(material.encode(), digest_size=10).hexdigest()
    return {**frozen, "fingerprint": fingerprint}


def select_enemy_intent(row: Mapping[str, Any], turn: int, target: str, target_name: str = "Player",
                        previous_move_id: str = "") -> dict | None:
    """Choose one frozen move deterministically and return its complete narration contract."""
    kit = row.get("kit") if isinstance(row, Mapping) else None
    moves = (kit or {}).get("moves") if isinstance(kit, Mapping) else None
    if not isinstance(moves, list) or not moves:
        return None
    legal = [m for m in moves if isinstance(m, Mapping) and m.get("id")]
    if not legal:
        return None
    nonrepeat = [m for m in legal if m.get("id") != previous_move_id]
    pool = nonrepeat or legal
    actor = _text(row.get("id") or row.get("cid"), 80) or "enemy"
    turn_i = _safe_int(turn)
    previous_move_id = _text(previous_move_id, 80)
    seed_material = f"{actor}|{turn_i}|{previous_move_id}|{(kit or {}).get('fingerprint', '')}"
    index = int(hashlib.blake2b(seed_material.encode(), digest_size=8).hexdigest(), 16) % len(pool)
    move = dict(pool[index])
    iid_material = f"{actor}|{move['id']}|{turn_i}|{previous_move_id}"
    intent_id = "intent_" + hashlib.blake2b(iid_material.encode(), digest_size=10).hexdigest()
    intent = {
        "schema": INTENT_SCHEMA,
        "id": intent_id,
        "previous_move_id": previous_move_id,
        "actor": actor,
        "actor_name": _text(row.get("name") or actor, 80),
        "move_id": _text(move["id"], 80),
        "move_name": _text(move.get("name") or move["id"], 80),
        "primitive": _text(move.get("primitive"), 48) or "strike",
        "basis": _text(move.get("basis"), 40) or "physical",
        "channel": _text(move.get("channel"), 20) or "hp",
        "delivery": _text(move.get("delivery"), 160) or "physical force",
        "range": _text(move.get("range"), 40) or "close",
        "timing": _text(move.get("timing"), 80) or "fast",
        "cadence": _text(move.get("cadence"), 80) or "reliable",
        "target_rule": _text(move.get("target_rule"), 40) or "player",
        "danger": _text(move.get("danger"), 40) or "moderate",
        "accuracy": max(-2, min(2, _safe_int(move.get("accuracy", 0)))),
        "damage": max(-2, min(3, _safe_int(move.get("damage", 0)))),
        "tell": _text(move.get("tell"), 240) or "the attack lines up",
        "counterplay": [_text(x, 120) for x in
                        ((move.get("counterplay") or []) if isinstance(
                            move.get("counterplay"), list) else [])[:3]],
        "sensory": _text(move.get("sensory"), 200) or "movement and impact",
        "risk": _text(move.get("risk"), 160) or "none",
        "forbid": _text(move.get("forbid"), 240) or "unlisted effects or extra targets",
        "target": _text(target, 80) or "player",
        "target_name": _text(target_name or target, 80) or "Player",
        "prepared_turn": turn_i,
    }
    if isinstance(move.get("reaction"), Mapping):
        intent["reaction"] = dict(move["reaction"])
    return intent


def intent_matches_frozen_kit(intent: Mapping[str, Any] | None,
                              row: Mapping[str, Any] | None) -> bool:
    """True only when an intent names this live row and one of its frozen move ids."""
    if not isinstance(intent, Mapping) or not isinstance(row, Mapping):
        return False
    allowed_fields = {
        "schema", "id", "previous_move_id", "actor", "actor_name", "move_id", "move_name",
        "primitive", "basis", "channel", "delivery", "range", "timing", "cadence",
        "target_rule", "danger", "accuracy", "damage", "tell", "counterplay", "sensory",
        "risk", "forbid", "target", "target_name", "prepared_turn", "reaction",
    }
    if set(intent) - allowed_fields:
        return False
    kit = row.get("kit")
    if intent.get("schema") != INTENT_SCHEMA or not isinstance(intent.get("id"), str) \
            or not intent["id"].startswith("intent_") \
            or str(intent.get("actor")) != str(row.get("id")) \
            or intent.get("actor_name") != _text(row.get("name") or row.get("id"), 80) \
            or not isinstance(kit, Mapping) or kit.get("schema") != KIT_SCHEMA \
            or not isinstance(kit.get("generator"), str) \
            or not re.fullmatch(r"enemy-grammar/\d+", kit["generator"]):
        return False
    try:
        frozen = {key: value for key, value in kit.items() if key != "fingerprint"}
        material = json.dumps(frozen, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False)
        expected_fingerprint = hashlib.blake2b(material.encode(), digest_size=10).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return False
    moves = kit.get("moves")
    if kit.get("fingerprint") != expected_fingerprint or not isinstance(moves, list) \
            or not 2 <= len(moves) <= 4 or not isinstance(kit.get("basis"), list):
        return False
    move = next((m for m in moves if isinstance(m, Mapping)
                 and m.get("id") == intent.get("move_id")), None)
    if not isinstance(move, Mapping):
        return False
    if move.get("channel") != "hp" or move.get("target_rule") != "player" \
            or move.get("basis") not in kit["basis"] \
            or isinstance(move.get("accuracy"), bool) \
            or not isinstance(move.get("accuracy"), int) \
            or not -2 <= move["accuracy"] <= 2 \
            or isinstance(move.get("damage"), bool) \
            or not isinstance(move.get("damage"), int) \
            or not -2 <= move["damage"] <= 3 \
            or not isinstance(move.get("counterplay"), list):
        return False
    if isinstance(intent.get("prepared_turn"), bool) \
            or not isinstance(intent.get("prepared_turn"), int) \
            or intent["prepared_turn"] < 0 \
            or not isinstance(intent.get("previous_move_id"), str) \
            or not _text(intent.get("target"), 80) \
            or not _text(intent.get("target_name"), 80):
        return False
    previous_move_id = intent.get("previous_move_id")
    if previous_move_id and not any(isinstance(candidate, Mapping)
                                    and candidate.get("id") == previous_move_id
                                    for candidate in moves):
        return False
    canonical_material = (f"{intent['actor']}|{intent['move_id']}|{intent['prepared_turn']}|"
                          f"{intent['previous_move_id']}")
    canonical_id = "intent_" + hashlib.blake2b(
        canonical_material.encode(), digest_size=10).hexdigest()
    if intent.get("id") != canonical_id:
        return False
    fields = {"name": "move_name", "primitive": "primitive", "basis": "basis",
              "channel": "channel", "target_rule": "target_rule",
              "delivery": "delivery", "range": "range", "timing": "timing",
              "cadence": "cadence", "danger": "danger", "accuracy": "accuracy",
              "damage": "damage", "tell": "tell", "counterplay": "counterplay",
              "sensory": "sensory", "risk": "risk", "forbid": "forbid",
              "reaction": "reaction"}
    return all(intent.get(intent_key) == move.get(move_key)
               for move_key, intent_key in fields.items())
