"""Build the canonical concept catalog from preserved AetherState corpus truth.

Genre translation rows and web provenance are curated separately.  This builder ensures the shared
catalog never drops the existing 18 families, 15 primitives, RPG registry skills/abilities/effects,
or the grounded basis vocabulary when the wider glossary grows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


CATEGORIES = [
    (
        "offense",
        "Offense",
        "Immediate, committed, delayed, area, or persistent harmful action.",
        ["Actions or effects whose intended result is direct harm or destructive pressure."],
        ["Defensive prevention and neutral environmental description."],
        ["A sword strike", "A delayed explosive charge"],
        ["A shield block", "A dangerous ruin that nobody activates"],
    ),
    (
        "defense_reaction",
        "Defense and Reaction",
        "Prevention, avoidance, interception, countering, and recovery from attacks.",
        ["Responses that reduce, redirect, avoid, or answer a committed threat."],
        ["Passive toughness with no response and ordinary movement unrelated to a threat."],
        ["Brace against a blow", "Counterspell a cast"],
        ["Walk across town", "Possess heavy armor"],
    ),
    (
        "buff_support",
        "Buff and Support",
        "Positive states, assistance, restoration, empowerment, and team enablement.",
        ["Benefits or assistance applied to oneself, another actor, or a group."],
        ["The resource being spent and harmful conditions."],
        ["Restore an ally", "Grant temporary accuracy"],
        ["Spend mana", "Inflict blindness"],
    ),
    (
        "status_condition",
        "Status and Condition",
        "Persistent physical, mental, supernatural, social, or environmental state.",
        ["State that remains true beyond one instantaneous resolution."],
        ["The action that caused a state and a spendable capacity."],
        ["Bleeding", "Infected", "Panicked"],
        ["Swing a sword", "A pool of stamina"],
    ),
    (
        "movement_travel",
        "Movement and Travel",
        "Position changes from a step or stance through vehicles, worlds, time, and space.",
        ["Actor, vehicle, population, planar, or temporal relocation."],
        ["Forced restraint and terrain that merely affects movement."],
        ["Sprint to cover", "Travel between worlds"],
        ["Become grappled", "A muddy road"],
    ),
    (
        "control_position_terrain",
        "Control, Position, and Terrain",
        "Restraint, displacement, denial, concealment, hazards, and encounter shaping.",
        ["Change who can occupy, cross, see, or safely use a place."],
        ["Long-distance travel and world governance."],
        ["Raise a barrier", "Create a hazardous zone"],
        ["Sail to another continent", "Change a tax law"],
    ),
    (
        "summon_deploy_transform",
        "Summon, Deploy, and Transform",
        "Created helpers or objects, alternate forms, modes, and manifestations.",
        ["Bring forth, place, duplicate, manifest, or change the form of something."],
        ["Ordinary movement of an unchanged existing actor."],
        ["Summon an undead servant", "Transform into mist"],
        ["Walk through a doorway", "Move an existing crate"],
    ),
    (
        "resource_cost",
        "Resource and Cost",
        "Spendable, exhaustible, accumulating, timed, social, or environmental capacity.",
        ["A measurable capacity, debt, reserve, charge, or price that can change."],
        ["A persistent condition that is not a capacity and the action bought by a cost."],
        ["Mana", "Ammunition", "Political capital"],
        ["Poisoned", "Cast a spell"],
    ),
    (
        "equipment_technology_magic",
        "Equipment, Technology, and Magic",
        "Grounding bases, tools, weapons, devices, traditions, and power sources.",
        ["What grounds, enables, channels, or embodies a capability."],
        ["The consequence produced by the tool or tradition."],
        ["Longsword", "Cyberware", "Divine magic"],
        ["Bleeding damage", "A completed teleport"],
    ),
    (
        "social_investigation",
        "Social and Investigation",
        "Influence, deception, command, knowledge, perception, inquiry, and information.",
        ["Acquire, hide, test, communicate, or change social and informational state."],
        ["Pure physical harm and material fabrication."],
        ["Interrogate a witness", "Influence a faction"],
        ["Forge a sword", "Strike a target"],
    ),
    (
        "crafting_survival_logistics",
        "Crafting, Survival, and Logistics",
        "Making, repair, medicine, food, shelter, supplies, transport, and endurance.",
        ["Produce, maintain, supply, repair, treat, or sustain actors and infrastructure."],
        ["Abstract political authority and instantaneous combat harm."],
        ["Repair armor", "Establish a supply route"],
        ["Declare a kingdom", "Stab an enemy"],
    ),
    (
        "world_scale_authority",
        "World Scale and Authority",
        "Factions, institutions, territory, governance, war, economy, climate, and reality-scale change.",
        ["Meaning whose subject or consequence is institutional, territorial, societal, or world-scale."],
        ["Personal-scale use that does not propagate into wider world state."],
        ["Change faction control", "Begin an undead apocalypse"],
        ["Win one private argument", "Injure one enemy"],
    ),
]

CONCEPT_KINDS = [
    ("ability", "Ability", "An owned innate, trained, or granted capability.", "Second Wind", "Bleeding"),
    (
        "ability_mechanic",
        "Ability Mechanic",
        "A bounded modifier to eligibility, checks, costs, or outcomes.",
        "Reroll",
        "Swordplay",
    ),
    ("action", "Action", "An attempted or performed activity.", "Planar travel", "Mana"),
    ("basis", "Basis", "Grounding that makes a capability coherent in its world.", "Magic", "Fear"),
    (
        "condition",
        "Condition",
        "A recognized persistent state outside the registry preset set.",
        "Infected",
        "Attack",
    ),
    (
        "functional_family",
        "Functional Family",
        "A cross-genre family of intended function and delivery behavior.",
        "Guard barrier",
        "Longsword",
    ),
    ("identity", "Identity", "A distinct kind of actor, entity, or organized subject.", "Faction", "Damage"),
    (
        "relationship_state",
        "Relationship State",
        "A durable social bond, obligation, or relation between exact subjects.",
        "Debt",
        "Ammunition",
    ),
    ("resource", "Resource", "A measurable capacity, reserve, debt, charge, or cost.", "Stamina", "Poisoned"),
    (
        "semantic_primitive",
        "Semantic Primitive",
        "A small reusable meaning operation used to describe a capability.",
        "Strike",
        "Kingdom",
    ),
    ("skill", "Skill", "A learned proficiency or check domain.", "Stealth", "Invisible"),
    ("status", "Status", "A canonical registry-backed effect preset.", "Charmed", "Travel"),
    (
        "world_state",
        "World State",
        "A durable fact whose subject or effect is wider than one transient action.",
        "Territorial control",
        "Sword strike",
    ),
]

CLASSIFICATION_PLANES = [
    {
        "id": "concept_facets",
        "name": "Concept Facets",
        "stability": "sealed_corpus",
        "owns": ["concept_kind", "domain_shelves"],
        "does_not_own": ["current_support", "runtime_truth"],
    },
    {
        "id": "scale_profile",
        "name": "Scale Profile",
        "stability": "frozen_definition_revision",
        "owns": ["independent_behavior_axes"],
        "does_not_own": ["assignment", "mechanic_admission"],
    },
    {
        "id": "cube_coverage",
        "name": "Cube Coverage",
        "stability": "current_evidence",
        "owns": ["face_status", "proof_reference", "explicit_non_support"],
        "does_not_own": ["concept_meaning", "runtime_truth"],
    },
    {
        "id": "runtime_record",
        "name": "Runtime Record",
        "stability": "ledger_owned",
        "owns": ["settled_instance", "cause", "replay_identity"],
        "does_not_own": ["developer_advice", "mutable_latest_definition"],
    },
]

SCALE_AXES = [
    ("power", "Power", "The owned capability ceiling or potency before one result is settled."),
    ("severity", "Severity", "The consequence multiplier or band applied by an admitted mechanic."),
    ("target_count", "Target Count", "The number and identity shape of intended subjects."),
    ("area", "Area", "The spatial shape or zone affected by one use."),
    ("range", "Range", "The allowed distance or reach between source and subject."),
    ("duration", "Duration", "How long a settled state or effect remains active."),
    ("world_scope", "World Scope", "The highest actor, institution, territory, or world scale reached."),
    ("propagation", "Propagation", "How an effect spreads after its admitted cause."),
    ("reversibility", "Reversibility", "Whether and how committed consequences can end or be undone."),
]

AUTHORITY_STAGES = [
    ("recognized", "Recognized", "Language or authoring input maps to candidate canonical meaning."),
    ("defined", "Defined", "One immutable world-specific definition revision exists."),
    ("assigned", "Assigned", "One exact subject acquired that exact definition revision."),
    ("eligible", "Eligible", "Current trusted context permits evaluation by a compatible adapter."),
    ("admitted", "Admitted", "A versioned adapter and distinct receipt are accepted for this use."),
    ("settled", "Settled", "The owning reducer committed one complete result atomically."),
    ("replayed", "Replayed", "The exact baked result reconstructs without consulting mutable latest data."),
]

CUBE_FACES = [
    ("recognition", "Recognition", "Canonical candidates and source spans preserve the intended surface."),
    ("binding", "Binding", "One current event connects actor, action, subjects, scope, and occurrence."),
    ("world_alignment", "World Alignment", "Exact ledger-backed identities and relations ground the event."),
    (
        "admission",
        "Admission",
        "Ownership, eligibility, adapter, cost, and receipt authority permit execution.",
    ),
    ("complete_settlement", "Complete Settlement", "One atomic replay-stable receipt owns the whole result."),
    (
        "narrator_transfer",
        "Narrator Transfer",
        "The final source-free packet preserves settled and unresolved truth.",
    ),
    ("hud_visibility", "HUD Visibility", "The Witness Layer shows the relevant result without changing it."),
]

COVERAGE_STATUSES = ["working", "partial", "blocked", "not_applicable", "unproven"]

WORLD_EVENT_FIELDS = [
    "cause",
    "actor",
    "priority",
    "affected_domains",
    "scope",
    "propagation",
    "expiry",
    "reversibility",
    "supersession",
    "cause_visibility",
    "branch_replay_identity",
]

MEANING_FACET_VALUES = {
    "semantic_role": [
        "capability_identity",
        "operation",
        "state",
        "resource",
        "grounding_basis",
        "identity",
        "world_rule",
        "mechanic_modifier",
    ],
    "target_cardinality": ["not_applicable", "unspecified", "single", "multiple"],
    "spatial_extent": ["not_applicable", "unspecified", "entity", "area", "zone"],
    "world_scope": [
        "not_applicable",
        "unspecified",
        "personal",
        "local",
        "regional",
        "global",
        "cross_world",
    ],
}

SEMANTIC_ROLE_BY_KIND = {
    "skill": "capability_identity",
    "ability": "capability_identity",
    "action": "operation",
    "functional_family": "operation",
    "semantic_primitive": "operation",
    "condition": "state",
    "status": "state",
    "relationship_state": "state",
    "resource": "resource",
    "basis": "grounding_basis",
    "identity": "identity",
    "world_state": "world_rule",
    "ability_mechanic": "mechanic_modifier",
}

NON_TARGET_ROLES = {
    "resource",
    "grounding_basis",
    "identity",
    "world_rule",
    "mechanic_modifier",
}

MEANING_FACET_OVERRIDES = {
    "family.direct_pressure": {"target_cardinality": "single", "spatial_extent": "entity"},
    "family.sweep_burst": {"target_cardinality": "multiple"},
    "family.zone_denial": {"spatial_extent": "zone"},
    "primitive.zone": {"spatial_extent": "zone"},
}

FAMILY_CATEGORIES = {
    "direct_pressure": ["offense"],
    "committed_strike": ["offense"],
    "defense_breach": ["offense", "control_position_terrain"],
    "sweep_burst": ["offense", "control_position_terrain"],
    "delayed_impact": ["offense", "control_position_terrain"],
    "persistent_exposure": ["offense", "status_condition"],
    "restraint": ["control_position_terrain", "status_condition"],
    "displacement": ["control_position_terrain", "movement_travel"],
    "impairment": ["status_condition", "control_position_terrain"],
    "zone_denial": ["control_position_terrain"],
    "guard_barrier": ["defense_reaction", "control_position_terrain"],
    "reactive_defense": ["defense_reaction"],
    "reposition": ["movement_travel", "defense_reaction"],
    "conceal_ambush": ["control_position_terrain", "defense_reaction"],
    "sustain": ["buff_support", "defense_reaction"],
    "deploy": ["summon_deploy_transform", "control_position_terrain"],
    "disrupt": ["control_position_terrain", "defense_reaction"],
    "mark_escalate": ["buff_support", "status_condition"],
}

PRIMITIVE_CATEGORIES = {
    "afflict": ["status_condition", "offense"],
    "breach": ["offense", "control_position_terrain"],
    "deploy": ["summon_deploy_transform"],
    "displace": ["control_position_terrain", "movement_travel"],
    "empower": ["buff_support"],
    "guard": ["defense_reaction"],
    "information": ["social_investigation"],
    "objective": ["world_scale_authority"],
    "prepare": ["buff_support", "control_position_terrain"],
    "recover": ["buff_support", "crafting_survival_logistics"],
    "restrain": ["control_position_terrain", "status_condition"],
    "strike": ["offense"],
    "transform": ["summon_deploy_transform"],
    "traverse": ["movement_travel"],
    "zone": ["control_position_terrain"],
}

SKILL_CATEGORIES = {
    "stealth": ["control_position_terrain", "social_investigation"],
    "swordplay": ["offense", "defense_reaction"],
    "archery": ["offense"],
    "persuasion": ["social_investigation"],
    "perception": ["social_investigation"],
    "lockpicking": ["social_investigation", "equipment_technology_magic"],
    "lore": ["social_investigation"],
    "athletics": ["movement_travel", "crafting_survival_logistics"],
    "spellcraft": ["equipment_technology_magic", "social_investigation"],
    "brawl": ["offense"],
}

ABILITY_CATEGORIES = {
    "keen_senses": ["buff_support", "social_investigation"],
    "steady_hand": ["buff_support", "offense"],
    "silver_tongue": ["buff_support", "social_investigation"],
    "power_strike": ["offense"],
    "second_wind": ["buff_support", "defense_reaction"],
    "arcane_gift": ["equipment_technology_magic"],
}

EXTRA_SKILLS = {
    "navigation": (
        "Navigation",
        ["plot route", "wayfinding", "orienteering"],
        ["movement_travel", "social_investigation"],
    ),
    "survival": ("Survival", ["survive", "fieldcraft", "wilderness craft"], ["crafting_survival_logistics"]),
    "medicine": (
        "Medicine",
        ["first aid", "treat wounds", "diagnose"],
        ["buff_support", "crafting_survival_logistics"],
    ),
    "crafting": ("Crafting", ["fabricate", "make", "build"], ["crafting_survival_logistics"]),
    "engineering": (
        "Engineering",
        ["engineer", "repair machinery", "systems engineering"],
        ["crafting_survival_logistics", "equipment_technology_magic"],
    ),
    "hacking": (
        "Hacking",
        ["intrusion", "breach network", "crack system"],
        ["social_investigation", "equipment_technology_magic"],
    ),
    "investigation": ("Investigation", ["investigate", "deduction", "forensics"], ["social_investigation"]),
    "deception": ("Deception", ["lie", "bluff", "misdirect"], ["social_investigation"]),
    "intimidation": ("Intimidation", ["threaten", "coerce", "menace"], ["social_investigation"]),
    "command": (
        "Command",
        ["lead troops", "issue orders", "rally unit"],
        ["social_investigation", "world_scale_authority"],
    ),
    "tactics": (
        "Tactics",
        ["battle plan", "maneuver unit", "read battlefield"],
        ["social_investigation", "world_scale_authority"],
    ),
    "piloting": (
        "Piloting",
        ["pilot", "helm", "drive vehicle"],
        ["movement_travel", "equipment_technology_magic"],
    ),
    "seafaring": (
        "Seafaring",
        ["sailing", "seamanship", "handle ship"],
        ["movement_travel", "crafting_survival_logistics"],
    ),
    "horsemanship": ("Horsemanship", ["riding", "ride horse", "mounted handling"], ["movement_travel"]),
    "ritual": (
        "Ritual Practice",
        ["perform rite", "ceremony", "occult ritual"],
        ["equipment_technology_magic", "social_investigation"],
    ),
    "cultivation": (
        "Cultivation",
        ["cultivate qi", "meditate meridians", "refine essence"],
        ["buff_support", "equipment_technology_magic"],
    ),
    "academics": ("Academics", ["research", "scholarship", "study subject"], ["social_investigation"]),
    "diplomacy": (
        "Diplomacy",
        ["negotiate treaty", "mediate", "statecraft"],
        ["social_investigation", "world_scale_authority"],
    ),
    "performance": ("Performance", ["perform", "oratory", "entertain"], ["social_investigation"]),
    "scavenging": (
        "Scavenging",
        ["scavenge", "salvage search", "pick ruins"],
        ["crafting_survival_logistics", "social_investigation"],
    ),
}

ABILITY_MECHANICS = {
    "edge": ("Edge", ["advantage", "roll extra keep best"], ["buff_support"]),
    "ward": ("Ward Floor", ["failure floor", "fumble guard"], ["buff_support", "defense_reaction"]),
    "mod": ("Flat Modifier", ["flat bonus", "check modifier"], ["buff_support"]),
    "extra_die": ("Second-Chance Die", ["extra die on failure", "second chance die"], ["buff_support"]),
    "reroll": ("Reroll", ["roll again", "retry die"], ["buff_support"]),
    "surge": ("Surge", ["scope surge", "ceiling lift"], ["buff_support", "offense"]),
    "basis": ("Capability Basis", ["eligibility marker", "power basis"], ["equipment_technology_magic"]),
}

BASES = {
    "physical": ("Physical Basis", ["body force", "physical leverage"]),
    "martial": ("Martial Basis", ["weapon training", "combat technique"]),
    "projectile": ("Projectile Basis", ["bow", "thrown weapon"]),
    "firearm": ("Firearm Basis", ["gun", "firearm", "ballistic weapon"]),
    "natural": ("Natural Anatomy Basis", ["claws", "fangs", "natural weapon"]),
    "technology": ("Technology Basis", ["device", "machine", "advanced technology"]),
    "magic": ("Magic Basis", ["magic", "arcana", "sorcery"]),
    "undead": ("Undead Basis", ["undead", "corpse animation"]),
    "supernatural": ("Supernatural Basis", ["otherworldly power", "paranormal ability"]),
    "hazard": ("Hazard Basis", ["toxin", "radiation", "corrosive substance"]),
    "anatomy": ("Anatomy Basis", ["body structure", "organ", "limb"]),
    "weapon": ("Weapon Basis", ["armed with", "weapon"]),
    "training": ("Training Basis", ["trained", "discipline", "practice"]),
    "psionic": ("Psionic Basis", ["psychic power", "mind force", "psionics"]),
    "mutation": ("Mutation Basis", ["mutant trait", "mutation"]),
    "cyberware": ("Cyberware Basis", ["cybernetic implant", "cyberware"]),
    "device": ("Device Basis", ["gadget", "apparatus", "tool"]),
    "environment": ("Environmental Basis", ["terrain", "weather", "surroundings"]),
    "ally": ("Ally Basis", ["team assist", "companion aid"]),
    "divine": ("Divine Basis", ["miracle", "holy power", "divine favor"]),
    "infernal": ("Infernal Basis", ["infernal power", "unholy power", "hellish power"]),
    "occult": ("Occult Basis", ["occult power", "forbidden rite", "esoteric practice"]),
    "cultivation": ("Cultivation Basis", ["qi", "chi", "inner energy", "spiritual cultivation"]),
    "alchemy": ("Alchemy Basis", ["alchemy", "elixir", "transmutation craft"]),
    "steampunk": ("Steam and Clockwork Basis", ["steam power", "clockwork", "aetheric engine"]),
    "combustion": ("Combustion-Engine Basis", ["diesel engine", "internal combustion", "motorized"]),
    "biotech": ("Biotechnology Basis", ["biotech", "gene craft", "living technology"]),
    "virtual": ("Virtual-System Basis", ["game system", "simulation", "digital world"]),
    "world_system": (
        "World-System Interface Basis",
        ["status screen", "world interface", "system-granted interface"],
    ),
}

RESOURCES = {
    "hp": ("Health", ["health", "hit points", "wounds"]),
    "stamina": ("Stamina", ["stamina", "endurance", "exertion"]),
    "mana": ("Mana", ["mana", "spell energy", "magic points"]),
    "ammo": ("Ammunition", ["ammo", "ammunition", "rounds"]),
    "charges": ("Charges", ["charges", "uses", "dose count"]),
    "cooldown": ("Cooldown", ["cooldown", "recharge time", "recovery timer"]),
    "fuel": ("Fuel", ["fuel", "propellant", "power reserve"]),
    "heat": ("Heat", ["heat", "overheat", "thermal load"]),
    "oxygen": ("Oxygen", ["oxygen", "air supply", "breath reserve"]),
    "hunger": ("Hunger", ["hunger", "food need", "calories"]),
    "thirst": ("Thirst", ["thirst", "water need", "hydration"]),
    "morale": ("Morale", ["morale", "cohesion", "fighting spirit"]),
    "sanity": ("Mental Stability", ["sanity", "mental stability", "stability reserve"]),
    "infection_load": ("Infection Load", ["infection load", "pathogen burden", "viral load"]),
    "radiation_dose": ("Radiation Dose", ["radiation dose", "rads", "exposure dose"]),
    "currency": ("Currency", ["money", "credits", "coin"]),
    "reputation": ("Reputation", ["reputation", "standing", "renown"]),
    "supplies": ("Supplies", ["supplies", "provisions", "materiel"]),
    "time": ("Time", ["time", "deadline", "clock"]),
    "qi": ("Qi", ["qi", "chi", "inner energy"]),
    "divine_favor": ("Divine Favor", ["divine favor", "grace", "piety"]),
    "blood": ("Blood Reserve", ["blood pool", "vitae", "stored blood"]),
    "rage": ("Rage", ["rage", "fury", "berserk meter"]),
    "experience": ("Experience", ["experience", "xp", "progress points"]),
    "authority": ("Authority", ["authority", "influence", "mandate"]),
}

EXTRA_STATUSES = {
    "infection": ("Infected", ["infected", "infection"]),
    "radiation_sickness": ("Radiation Sickness", ["radiation sickness", "irradiated", "acute exposure"]),
    "frenzy": ("Frenzy", ["frenzy", "blood frenzy", "berserk"]),
    "possession": ("Possessed", ["possessed", "spirit ridden", "demonic possession"]),
    "corruption": ("Corrupted", ["corruption", "taint", "spiritual pollution"]),
    "mental_stress": ("Mental Stress", ["stress load", "mounting stress", "mental strain"]),
    "oxygen_deprivation": ("Oxygen Deprivation", ["suffocating", "hypoxia", "oxygen deprived"]),
    "dehydration": ("Dehydrated", ["dehydrated", "water deprived"]),
    "hypothermia": ("Hypothermia", ["hypothermia", "freezing exposure"]),
    "heatstroke": ("Heatstroke", ["heatstroke", "heat exhaustion"]),
    "suppressed": ("Suppressed", ["suppressed", "pinned down", "under fire"]),
    "prone": ("Prone", ["prone", "knocked down", "on the ground"]),
    "grappled": ("Grappled", ["grappled", "held", "clinched"]),
    "silenced": ("Silenced", ["silenced", "unable to cast", "voice sealed"]),
    "compromised": ("Compromised", ["hacked", "compromised", "system breached"]),
    "decompression": ("Decompression", ["decompression", "rapid pressure loss", "explosive decompression"]),
    "vampirism": ("Vampirism", ["vampirism", "turned vampire", "blood curse"]),
    "lycanthropy": ("Lycanthropy", ["lycanthropy", "werewolf curse", "moon curse"]),
    "haunted": ("Haunted", ["haunted", "spirit attachment", "ghost marked"]),
    "concealed": ("Concealed", ["concealed", "hidden", "obscured"]),
    "invisible": ("Invisible", ["invisible", "unseen", "optically cloaked"]),
    "marked": ("Marked", ["marked", "target locked", "designated"]),
    "wanted": ("Wanted", ["wanted", "bounty", "manhunt target"]),
    "qi_deviation": (
        "Persistent Meridian Deviation",
        ["persistent meridian disorder", "cultivation pathology", "deviated meridians"],
    ),
}

EXTRA_ACTIONS = {
    "planar_travel": (
        "Planar Travel",
        ["cross worlds", "world transfer", "dimensional gate"],
        ["movement_travel", "world_scale_authority"],
    ),
    "teleportation": ("Teleportation", ["teleport", "blink", "instant transit"], ["movement_travel"]),
    "flight": ("Flight", ["fly", "aerial travel", "levitation travel"], ["movement_travel"]),
    "vehicle_travel": ("Vehicle Travel", ["drive", "ride vehicle", "motor travel"], ["movement_travel"]),
    "space_travel": (
        "Space Travel",
        ["jump route", "hyperspace", "interstellar travel"],
        ["movement_travel", "world_scale_authority"],
    ),
    "time_travel": (
        "Time Travel",
        ["time travel", "temporal jump", "change timeline"],
        ["movement_travel", "world_scale_authority"],
    ),
    "shapeshift": (
        "Shapeshift",
        ["shapeshift", "change form", "transform body"],
        ["summon_deploy_transform"],
    ),
    "resurrection": (
        "Resurrection",
        ["resurrect", "return from death", "revive the dead"],
        ["summon_deploy_transform", "world_scale_authority"],
    ),
    "summoning": ("Summoning", ["summon", "conjure ally", "call entity"], ["summon_deploy_transform"]),
    "ritual_casting": (
        "Ritual Casting",
        ["cast ritual", "perform invocation", "ceremonial magic"],
        ["equipment_technology_magic", "summon_deploy_transform"],
    ),
    "divination": (
        "Divination",
        ["scry", "divine answer", "read omen"],
        ["social_investigation", "equipment_technology_magic"],
    ),
    "exorcism": (
        "Exorcism",
        ["exorcise", "banish spirit", "purge possession"],
        ["defense_reaction", "equipment_technology_magic"],
    ),
    "hacking": (
        "System Intrusion",
        ["hack system", "neural intrusion", "cyber intrusion"],
        ["social_investigation", "control_position_terrain"],
    ),
    "quarantine": (
        "Quarantine",
        ["quarantine", "isolate outbreak", "contain contagion"],
        ["crafting_survival_logistics", "world_scale_authority"],
    ),
    "decontamination": (
        "Decontamination",
        ["decontaminate", "purge contamination", "sterilize"],
        ["crafting_survival_logistics", "buff_support"],
    ),
    "fortification": (
        "Fortification",
        ["fortify", "build defenses", "entrench"],
        ["defense_reaction", "crafting_survival_logistics"],
    ),
    "siege": (
        "Siege",
        ["besiege", "siege operation", "break fortress"],
        ["world_scale_authority", "offense"],
    ),
    "mass_battle": (
        "Mass Battle",
        ["lead battle", "army engagement", "fleet battle"],
        ["world_scale_authority"],
    ),
    "governance": (
        "Governance",
        ["govern", "administer realm", "issue law"],
        ["world_scale_authority", "social_investigation"],
    ),
    "faction_influence": (
        "Faction Influence",
        ["influence faction", "political leverage", "court intrigue"],
        ["world_scale_authority", "social_investigation"],
    ),
    "territory_control": (
        "Territory Control",
        ["claim territory", "hold ground", "annex region"],
        ["world_scale_authority"],
    ),
    "climate_adaptation": (
        "Climate Adaptation",
        ["adapt settlement", "climate resilience", "managed retreat"],
        ["world_scale_authority", "crafting_survival_logistics"],
    ),
    "crafting": ("Crafting Action", ["craft item", "forge", "fabricate"], ["crafting_survival_logistics"]),
    "repair": (
        "Repair",
        ["repair", "mend", "restore machine"],
        ["crafting_survival_logistics", "buff_support"],
    ),
    "foraging": ("Foraging", ["forage", "gather food", "find water"], ["crafting_survival_logistics"]),
    "scavenging": ("Scavenging", ["scavenge", "salvage", "loot ruins"], ["crafting_survival_logistics"]),
    "tracking": (
        "Tracking",
        ["track quarry", "follow trail", "hunt signs"],
        ["social_investigation", "crafting_survival_logistics", "movement_travel"],
    ),
    "interrogation": (
        "Interrogation",
        ["interrogate", "question suspect", "extract information"],
        ["social_investigation"],
    ),
    "disguise": (
        "Disguise",
        ["disguise", "impersonate", "false identity"],
        ["social_investigation", "control_position_terrain"],
    ),
    "boarding": (
        "Boarding Action",
        ["board ship", "boarding party", "breach vessel"],
        ["offense", "movement_travel"],
    ),
    "cultivation_breakthrough": (
        "Cultivation Breakthrough",
        ["break through realm", "ascend cultivation", "core formation"],
        ["buff_support", "world_scale_authority"],
    ),
    "enchantment": (
        "Enchantment",
        ["enchant item", "imbue object", "bind magic"],
        ["equipment_technology_magic", "crafting_survival_logistics"],
    ),
    "transmutation": (
        "Transmutation",
        ["transmute", "alter matter", "change substance"],
        ["equipment_technology_magic", "summon_deploy_transform"],
    ),
    "research": (
        "Research",
        ["research project", "develop technology", "study phenomenon"],
        ["social_investigation", "world_scale_authority"],
    ),
    "training": (
        "Training",
        ["train skill", "practice technique", "study lesson"],
        ["buff_support", "social_investigation"],
    ),
    "isolation": (
        "Medical Isolation",
        ["isolate confirmed case", "isolate the infected", "infectious isolation"],
        ["control_position_terrain", "crafting_survival_logistics"],
    ),
    "reincarnation": (
        "Reincarnation",
        ["reborn in another body", "reincarnate", "death and rebirth transition"],
        ["summon_deploy_transform", "world_scale_authority"],
    ),
    "astral_projection": (
        "Astral Projection",
        ["project spirit", "astral travel", "out-of-body travel"],
        ["movement_travel", "summon_deploy_transform"],
    ),
    "duplication": (
        "Duplication",
        ["create duplicate", "split into copies", "manifest duplicate"],
        ["summon_deploy_transform"],
    ),
    "system_administration": (
        "System Administration",
        ["administer server", "moderate users", "host moderation", "server administration"],
        ["equipment_technology_magic", "social_investigation", "world_scale_authority"],
    ),
    "respawn": (
        "Respawn",
        ["respawn at checkpoint", "reappear after defeat", "return at spawn point"],
        ["summon_deploy_transform", "world_scale_authority"],
    ),
}

# Cross-genre distinctions that cannot be represented honestly by the older enemy/registry
# concepts.  These remain recognition-only until a reducer receipt is implemented.
ADDITIONAL_CONCEPTS = {
    "resource.boiler_pressure": (
        "Boiler Pressure",
        "resource",
        ["resource_cost"],
        ["steam pressure", "boiler pressure", "working pressure"],
        "Exhaustible pressure available to a steam-driven machine, distinct from thermal heat.",
    ),
    "resource.evidence": (
        "Evidence",
        "resource",
        ["resource_cost", "social_investigation"],
        ["case evidence", "clue chain", "proof"],
        "Collected and retained support for an investigation or accusation.",
    ),
    "resource.vehicle_integrity": (
        "Vehicle Integrity",
        "resource",
        ["resource_cost", "crafting_survival_logistics"],
        ["hull integrity", "vehicle condition", "structural integrity"],
        "Tracked structural capacity of a vehicle or vessel.",
    ),
    "condition.vehicle_disabled": (
        "Vehicle Disabled",
        "condition",
        ["status_condition", "movement_travel"],
        ["engine disabled", "mobility kill", "vehicle immobilized"],
        "A vehicle cannot perform normal movement until its disabling fault is resolved.",
    ),
    "condition.flooding": (
        "Flooding",
        "condition",
        ["status_condition", "control_position_terrain"],
        ["taking on water", "flooded compartment", "hull leak"],
        "Water is accumulating within a vessel or occupied space and requires damage control.",
    ),
    "resource.honor": (
        "Honor",
        "resource",
        ["resource_cost", "social_investigation", "world_scale_authority"],
        ["personal honor", "honorable standing", "social honor"],
        "Setting-defined personal or institutional honor, separate from generic reputation.",
    ),
    "condition.dishonored": (
        "Dishonored",
        "condition",
        ["status_condition", "social_investigation"],
        ["disgraced", "dishonored", "loss of face"],
        "A recognized social state of lost honor or standing, without implying a combat penalty.",
    ),
    "world.timeline_divergence": (
        "Timeline Divergence",
        "world_state",
        ["world_scale_authority", "social_investigation"],
        ["point of divergence", "alternate-history branch", "Jonbar point"],
        "A committed world fact identifying where and how a historical timeline departed from another account.",
    ),
    "resource.academic_progress": (
        "Academic Progress",
        "resource",
        ["resource_cost", "world_scale_authority"],
        ["course credit", "academic progress", "graduation progress"],
        "Tracked progress through a course, term, rank, or graduation requirement.",
    ),
    "condition.school_discipline": (
        "School Discipline",
        "condition",
        ["status_condition", "world_scale_authority"],
        ["detention", "academic probation", "school suspension"],
        "An institutional disciplinary state imposed by a school or academy.",
    ),
    "relationship.standing": (
        "Relationship Standing",
        "relationship_state",
        ["resource_cost", "social_investigation"],
        ["relationship standing", "friendship track", "rivalry standing", "personal trust"],
        "Tracked closeness, trust, rivalry, or estrangement between specific actors; not a fungible currency.",
    ),
    "resource.blood_hunger": (
        "Blood Hunger",
        "resource",
        ["resource_cost", "status_condition"],
        ["blood hunger", "vampire thirst", "feeding need", "hunger for blood"],
        "A vampiric need to feed, distinct from stored blood and ordinary food hunger.",
    ),
    "resource.law_heat": (
        "Law Heat",
        "resource",
        ["resource_cost", "social_investigation"],
        ["law heat", "police attention", "trace heat", "pursuit attention"],
        "Accumulating attention from police, corporate security, hunters, or another pursuing authority; not thermal heat.",
    ),
    "resource.debt_obligation": (
        "Debt and Obligation",
        "resource",
        ["resource_cost", "social_investigation"],
        ["debt obligation", "favor owed", "crew debt", "pact obligation", "sacred obligation"],
        "A specific outstanding debt, favor, oath, or obligation; separate from cash, reputation, authority, and favor.",
    ),
    "resource.patron_favor": (
        "Patron Favor",
        "resource",
        ["resource_cost", "social_investigation"],
        ["patron favor", "infernal patronage", "supernatural patron standing"],
        "Standing or credit with a specific patron, without assuming celestial or infernal polarity.",
    ),
    "resource.public_confidence": (
        "Public Confidence",
        "resource",
        ["resource_cost", "world_scale_authority"],
        ["public confidence", "popular confidence", "realm confidence"],
        "Population-scale confidence in a polity or institution, distinct from an actor being inspired.",
    ),
    "resource.legitimacy": (
        "Legitimacy",
        "resource",
        ["resource_cost", "social_investigation", "world_scale_authority"],
        ["dynastic legitimacy", "rightful claim", "recognized legitimacy"],
        "Recognized right of a person or institution to govern; not automatic obedience.",
    ),
    "resource.bandwidth": (
        "Bandwidth",
        "resource",
        ["resource_cost", "equipment_technology_magic"],
        ["network bandwidth", "signal bandwidth", "data throughput"],
        "Available communication or transfer capacity.",
    ),
    "resource.processing_memory": (
        "Processing Memory",
        "resource",
        ["resource_cost", "equipment_technology_magic"],
        ["processing memory", "compute memory", "working memory allocation"],
        "Available computational working memory, distinct from stored charges.",
    ),
    "resource.shield_capacity": (
        "Shield Capacity",
        "resource",
        ["resource_cost", "defense_reaction", "equipment_technology_magic"],
        ["shield capacity", "deflector reserve", "shield charge"],
        "Remaining capacity of a defensive field or screen.",
    ),
    "resource.ship_power": (
        "Ship Power",
        "resource",
        ["resource_cost", "equipment_technology_magic", "crafting_survival_logistics"],
        ["ship power", "reactor allocation", "power grid reserve"],
        "Power available for allocation among a vessel's systems.",
    ),
    "resource.threat": (
        "Threat",
        "resource",
        ["resource_cost", "control_position_terrain"],
        ["threat meter", "aggro", "hate value"],
        "Accumulated hostile attention toward an avatar or actor.",
    ),
    "condition.exposed": (
        "Disease Exposure",
        "condition",
        ["status_condition"],
        ["disease exposure", "pathogen exposure", "exposed contact"],
        "Contact with a pathogen without asserting infection or infectiousness.",
    ),
    "condition.incubating": (
        "Incubating Infection",
        "condition",
        ["status_condition"],
        ["incubating", "pre-symptomatic infection", "latent infection"],
        "An infection exists in an incubation phase; contagiousness remains a separate fact.",
    ),
    "condition.infectious": (
        "Infectious",
        "condition",
        ["status_condition", "control_position_terrain"],
        ["infectious", "contagious", "transmitting infection"],
        "A person or source can transmit an infection.",
    ),
    "condition.infection_protection": (
        "Infection Protection",
        "condition",
        ["buff_support", "status_condition"],
        ["vaccinated", "prophylaxis", "infection protection"],
        "Protection against infection from vaccination, prophylaxis, or a setting-defined equivalent; not recovery from illness.",
    ),
    "condition.vacuum_exposure": (
        "Vacuum Exposure",
        "condition",
        ["status_condition", "control_position_terrain"],
        ["vacuum exposure", "exposed to vacuum", "hard vacuum"],
        "Direct environmental exposure to vacuum, distinct from decompression and resulting hypoxia.",
    ),
    "condition.life_support_failure": (
        "Life-Support Failure",
        "condition",
        ["status_condition", "equipment_technology_magic", "crafting_survival_logistics"],
        ["life support failure", "life support offline", "environmental system failure"],
        "Failure of a system that maintains a habitable environment.",
    ),
    "condition.panic": (
        "Panic",
        "condition",
        ["status_condition"],
        ["panic episode", "panicking", "acute panic"],
        "An acute loss of composure, distinct from accumulated stress, fear status, and lasting trauma.",
    ),
    "condition.trauma": (
        "Trauma",
        "condition",
        ["status_condition"],
        ["traumatic shock", "lasting trauma", "horror fallout"],
        "A lasting consequence of overwhelming experience, distinct from immediate panic or a stress meter.",
    ),
    "condition.contaminated": (
        "Contaminated",
        "condition",
        ["status_condition", "control_position_terrain", "crafting_survival_logistics"],
        ["contaminated", "surface contamination", "hazard residue"],
        "A person, object, or place carries hazardous material without asserting biological infection.",
    ),
    "identity.account": (
        "Account Identity",
        "identity",
        ["social_investigation", "world_scale_authority"],
        ["user account", "account identity", "login identity"],
        "The host-level account that owns access and permissions, distinct from an in-world avatar.",
    ),
    "identity.avatar": (
        "Avatar Identity",
        "identity",
        ["summon_deploy_transform", "social_investigation"],
        ["player avatar", "avatar identity", "character body"],
        "An in-world represented actor controlled through an account.",
    ),
    "identity.system": (
        "System Identity",
        "identity",
        ["equipment_technology_magic", "world_scale_authority"],
        ["server identity", "host system", "world host"],
        "The hosting system or service, distinct from account and avatar actors.",
    ),
    "condition.avatar_desync": (
        "Avatar Desynchronization",
        "condition",
        ["status_condition", "movement_travel"],
        ["avatar desynced", "position desync", "avatar desynchronization"],
        "An avatar's represented state diverges from authoritative state.",
    ),
    "condition.session_disconnected": (
        "Session Disconnected",
        "condition",
        ["status_condition", "world_scale_authority"],
        ["disconnected", "session dropped", "logged out unexpectedly"],
        "A client session has lost its connection without asserting account compromise.",
    ),
    "condition.network_lag": (
        "Network Lag",
        "condition",
        ["status_condition", "equipment_technology_magic"],
        ["lagged out", "network lag", "high latency"],
        "Delayed communication between client and host, distinct from disconnection or server-state divergence.",
    ),
    "condition.server_fault": (
        "Server Fault",
        "condition",
        ["status_condition", "equipment_technology_magic", "world_scale_authority"],
        ["server fault", "server desync", "host outage"],
        "A host or server is malfunctioning, distinct from an avatar or account condition.",
    ),
    "world.aggro_rule": (
        "Aggro Rule",
        "world_state",
        ["control_position_terrain", "world_scale_authority"],
        ["aggro radius", "threat radius", "hostility acquisition rule"],
        "A world-system rule governing how hostile actors acquire targets; not literal zone denial.",
    ),
    "world.permadeath_rule": (
        "Permadeath Rule",
        "world_state",
        ["world_scale_authority"],
        ["permadeath", "no respawn rule", "single-life rule"],
        "A world rule that determines whether defeat permanently ends an avatar or character.",
    ),
    "world.weakness_rule": (
        "Weakness Rule",
        "world_state",
        ["world_scale_authority", "social_investigation"],
        ["configured weakness", "setting weakness", "vulnerability rule"],
        "A setting-defined vulnerability; genre or species labels alone do not grant it.",
    ),
    "world.effect_polarity_rule": (
        "Effect Polarity Rule",
        "world_state",
        ["world_scale_authority", "equipment_technology_magic"],
        ["holy unholy polarity", "effect polarity", "celestial infernal interaction"],
        "A world law defining how opposed supernatural polarities interact.",
    ),
    "world.doom_clock": (
        "Doom Clock",
        "world_state",
        ["resource_cost", "world_scale_authority"],
        ["doom clock", "threat clock", "escalation clock"],
        "Tracked escalation toward a world or encounter consequence; not an objective by itself.",
    ),
    "condition.ritual_impurity": (
        "Ritual Impurity",
        "condition",
        ["status_condition", "social_investigation"],
        ["ritual impurity", "sacred pollution", "ceremonial uncleanness"],
        "A tradition-defined ritual state, distinct from ordinary dirt, infection, and an automatically executable curse.",
    ),
}

FAMILY_BASES = {
    "anatomy",
    "weapon",
    "training",
    "magic",
    "psionic",
    "mutation",
    "cyberware",
    "device",
    "environment",
    "ally",
}
RUNTIME_BASES = {
    "physical",
    "martial",
    "projectile",
    "firearm",
    "natural",
    "technology",
    "undead",
    "supernatural",
    "hazard",
}
RESOURCE_LOCAL_SOURCES = {
    "hp": "existing.enemy_capability_families",
    "stamina": "existing.registry_snapshot",
    "mana": "existing.registry_snapshot",
    "ammo": "existing.enemy_capability_families",
    "charges": "existing.enemy_capability_families",
    "cooldown": "existing.enemy_capability_families",
    "heat": "existing.enemy_capability_families",
    "fuel": "existing.enemy_runtime_lexicon",
}


def _meaning_facets(concept_id: str, concept_type: str) -> dict[str, str]:
    role = SEMANTIC_ROLE_BY_KIND[concept_type]
    target = "not_applicable" if role in NON_TARGET_ROLES else "unspecified"
    spatial = "not_applicable" if role in NON_TARGET_ROLES else "unspecified"
    facets = {
        "semantic_role": role,
        "target_cardinality": target,
        "spatial_extent": spatial,
        "world_scope": "unspecified",
    }
    facets.update(MEANING_FACET_OVERRIDES.get(concept_id, {}))
    return facets


def _meaning_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "schema": "capability-concept-meaning/1",
        "concept_id": row["id"],
        "concept_type": row["concept_type"],
        "categories": sorted(set(row["categories"])),
        "definition": row["definition"],
        "meaning_facets": row["meaning_facets"],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _concept(
    concept_id: str,
    label: str,
    concept_type: str,
    categories: Iterable[str],
    aliases: Iterable[str],
    definition: str,
    source_ids: Iterable[str],
    receipt_ids: Iterable[str] = (),
    narration: str = "meaning",
) -> dict[str, Any]:
    row = {
        "id": concept_id,
        "label": label,
        "concept_type": concept_type,
        "categories": list(categories),
        "aliases": list(dict.fromkeys(str(item) for item in aliases if str(item).strip())),
        "definition": definition,
        "meaning_facets": _meaning_facets(concept_id, concept_type),
        "source_ids": list(source_ids),
        "support": {
            "recognition": "canonical",
            "authorization": "frozen_definition_required",
            "receipt_ids": list(receipt_ids),
            "narration": narration,
        },
    }
    row["meaning_fingerprint"] = _meaning_fingerprint(row)
    return row


def build(root: Path) -> None:
    enemy_root = root / "corpus" / "enemy-capabilities"
    out_root = root / "corpus" / "capability-glossary"
    out_root.mkdir(parents=True, exist_ok=True)
    families = json.loads((enemy_root / "capability-families.json").read_text(encoding="utf-8"))
    registry = json.loads((enemy_root / "registry-snapshot.json").read_text(encoding="utf-8"))["registries"]

    concepts: list[dict[str, Any]] = []
    for row in families["families"]:
        family_id = row["id"]
        genre_manifestations = row.get("genre_manifestations", {})
        manifestations = [genre_manifestations[key] for key in sorted(genre_manifestations)]
        concepts.append(
            _concept(
                f"family.{family_id}",
                family_id.replace("_", " ").title(),
                "functional_family",
                FAMILY_CATEGORIES[family_id],
                [row["function"], *manifestations],
                row["function"],
                ["existing.enemy_capability_families"],
                [],
                "boundary",
            )
        )

    for primitive_id, definition in families["semantic_primitives"].items():
        concepts.append(
            _concept(
                f"primitive.{primitive_id}",
                primitive_id.replace("_", " ").title(),
                "semantic_primitive",
                PRIMITIVE_CATEGORIES[primitive_id],
                [],
                definition,
                ["existing.enemy_capability_families"],
            )
        )

    for skill_id, row in registry["skills"].items():
        concepts.append(
            _concept(
                f"skill.{skill_id}",
                row["name"],
                "skill",
                SKILL_CATEGORIES[skill_id],
                row.get("governs", []),
                row.get("desc", ""),
                ["existing.registry_snapshot"],
                [],
            )
        )

    for skill_id, (label, aliases, categories) in EXTRA_SKILLS.items():
        concepts.append(
            _concept(
                f"skill.{skill_id}",
                label,
                "skill",
                categories,
                aliases,
                f"A recognition domain for {label.lower()}; a custom runtime definition still needs mechanics.",
                ["aetherstate.cross_genre_normalization"],
            )
        )

    for ability_id, row in registry["abilities"].items():
        concepts.append(
            _concept(
                f"ability.{ability_id}",
                row["name"],
                "ability",
                ABILITY_CATEGORIES[ability_id],
                [row.get("effect", "")],
                row.get("desc", ""),
                ["existing.registry_snapshot"],
                [],
            )
        )

    for mechanic_id, (label, aliases, categories) in ABILITY_MECHANICS.items():
        concepts.append(
            _concept(
                f"ability.{mechanic_id}",
                label,
                "ability_mechanic",
                categories,
                aliases,
                f"The frozen {label.lower()} dice or eligibility mechanic.",
                ["existing.registry_runtime"],
                [],
            )
        )

    for effect_id, row in registry["effects"].items():
        categories = (
            ["buff_support", "status_condition"] if row.get("valence") == "positive" else ["status_condition"]
        )
        if effect_id == "charmed":
            categories.append("control_position_terrain")
        concepts.append(
            _concept(
                f"status.{effect_id}",
                row["name"],
                str(row.get("kind", "condition")),
                categories,
                [],
                row.get("desc", ""),
                ["existing.registry_snapshot"],
                [],
            )
        )

    for status_id, (label, aliases) in EXTRA_STATUSES.items():
        categories = ["status_condition"]
        if status_id in {"concealed", "invisible"}:
            categories.append("control_position_terrain")
        concepts.append(
            _concept(
                f"condition.{status_id}",
                label,
                "condition",
                categories,
                aliases,
                f"Recognized {label.lower()} state; execution requires a matching receipt.",
                ["aetherstate.cross_genre_normalization"],
            )
        )

    # Existing Qi-deviation wording is intentionally normalized to the registry's Backlash preset
    # as well as the precise recognition-only condition.  Context decides which candidate binds.
    for concept in concepts:
        if concept["id"] == "status.backlash":
            concept["aliases"].extend(["qi deviation", "cultivation backlash"])

    for basis_id, (label, aliases) in BASES.items():
        source_ids = ["aetherstate.cross_genre_normalization"]
        if basis_id in FAMILY_BASES:
            source_ids.insert(0, "existing.enemy_capability_families")
        elif basis_id in RUNTIME_BASES:
            source_ids.insert(0, "existing.enemy_runtime_lexicon")
        concepts.append(
            _concept(
                f"basis.{basis_id}",
                label,
                "basis",
                ["equipment_technology_magic"],
                aliases,
                f"Grounding evidence based on {label.lower()}.",
                source_ids,
            )
        )

    for resource_id, (label, aliases) in RESOURCES.items():
        source_ids = ["aetherstate.cross_genre_normalization"]
        if resource_id in RESOURCE_LOCAL_SOURCES:
            source_ids.insert(0, RESOURCE_LOCAL_SOURCES[resource_id])
        concepts.append(
            _concept(
                f"resource.{resource_id}",
                label,
                "resource",
                ["resource_cost"],
                aliases,
                f"Tracked or proposed {label.lower()} capacity.",
                source_ids,
                [],
            )
        )

    for action_id, (label, aliases, categories) in EXTRA_ACTIONS.items():
        concepts.append(
            _concept(
                f"action.{action_id}",
                label,
                "action",
                categories,
                aliases,
                f"Canonical recognition frame for {label.lower()}.",
                ["aetherstate.cross_genre_normalization"],
            )
        )

    for concept_id, (label, concept_type, categories, aliases, definition) in ADDITIONAL_CONCEPTS.items():
        concepts.append(
            _concept(
                concept_id,
                label,
                concept_type,
                categories,
                aliases,
                definition,
                ["aetherstate.cross_genre_normalization"],
            )
        )

    by_id: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        if concept["id"] in by_id:
            raise ValueError(f"duplicate generated concept id: {concept['id']}")
        by_id[concept["id"]] = concept

    categories_doc = {
        "schema": "aetherstate-glossary-categories/2",
        "categories": [
            {
                "id": category_id,
                "label": label,
                "description": description,
                "includes": includes,
                "excludes": excludes,
                "examples": examples,
                "counterexamples": counterexamples,
            }
            for (
                category_id,
                label,
                description,
                includes,
                excludes,
                examples,
                counterexamples,
            ) in CATEGORIES
        ],
    }
    taxonomy_doc = {
        "schema": "aetherstate-semantic-atlas-taxonomy/1",
        "name": "Semantic Atlas",
        "authority": "classification_only",
        "classification_planes": CLASSIFICATION_PLANES,
        "concept_kinds": [
            {
                "id": kind_id,
                "label": label,
                "description": description,
                "example": example,
                "counterexample": counterexample,
            }
            for kind_id, label, description, example, counterexample in CONCEPT_KINDS
        ],
        "scale_axes": [
            {"id": axis_id, "label": label, "description": description}
            for axis_id, label, description in SCALE_AXES
        ],
        "authority_stages": [
            {"id": stage_id, "label": label, "description": description}
            for stage_id, label, description in AUTHORITY_STAGES
        ],
        "cube_faces": [
            {"id": face_id, "label": label, "description": description}
            for face_id, label, description in CUBE_FACES
        ],
        "coverage_statuses": COVERAGE_STATUSES,
        "meaning_facet_contract": {
            "schema": "capability-concept-meaning/1",
            "closed_values": MEANING_FACET_VALUES,
            "fingerprint_includes": [
                "concept_id",
                "concept_type",
                "categories",
                "definition",
                "meaning_facets",
            ],
            "fingerprint_excludes": ["label", "aliases", "source_ids", "support"],
        },
        "world_event_record": {
            "name": "World Event Record",
            "collection": "World Overlay Stack",
            "authority": "ledger_owned_after_admitted_settlement",
            "required_fields": WORLD_EVENT_FIELDS,
        },
    }
    concepts_doc = {
        "schema": "aetherstate-glossary-concepts/2",
        "concepts": [by_id[key] for key in sorted(by_id)],
    }
    (out_root / "categories.json").write_bytes(
        (json.dumps(categories_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    (out_root / "concepts.json").write_bytes(
        (json.dumps(concepts_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    (out_root / "taxonomy.json").write_bytes(
        (json.dumps(taxonomy_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    build(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
