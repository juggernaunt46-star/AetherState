"""Character Creator & World-first genesis authoring (doc 09).

Deterministic backbone + optional assist-LLM "fill the blanks". The player supplies the main
details they care about; every blank is filled — deterministically ALWAYS, and by an assist LLM
when one is configured (fail-open to the deterministic fill). The completed docs are then turned
into SHIPPED state ops (entities, memory/lore, goals, scene, `player_seed`) — no new storage
families, no new op vocabulary.

Same spine as the rest of AetherState: code is authority, the LLM only proposes. A proposal is
parsed, validated + clamped against the curated registry, then FROZEN into state at creation time.
Authoring is COLD-PATH / creation-time only (never inline on a roll or the token stream) and inert
for a `none` session (the caller gates it). Freestyle skills/abilities the LLM invents are frozen
into per-character `defs` snapshots (fixed numbers) via `player_seed` — so nothing is freestyle at
resolution (registry invariant 5).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import registry
from .assist import _chat, _json_or_none
from .state import TIMES, slug

log = logging.getLogger("aetherstate.creator")

GENRES = ["high_fantasy", "dark_fantasy", "sci_fi", "cyberpunk",
          "post_apoc", "historical", "modern", "custom"]

# Deterministic world templates — a playable default per genre when the player (and the LLM)
# leave a field blank. Kept short + evocative; the LLM enriches these when available.
_GENRE_TEMPLATES: dict[str, dict] = {
    "high_fantasy": {
        "setting": "A realm of feuding kingdoms, fading magic, and older grudges.",
        "date": "Year 1042 of the Third Age", "time": "morning", "tone": "heroic",
        "factions": ["The Crownlands", "The Free Companies", "The Order of the Ash"],
        "locations": ["Highmoor", "The Thornwood", "Karrick's Gate"],
        "aspects": ["Magic is real but rare and feared.",
                    "Steel and oath still decide most disputes."],
        "opening_scene": "A crossroads inn at first light, a road forking toward trouble.",
        "opening_quest": "Learn who put a price on your head — before they collect."},
    "dark_fantasy": {
        "setting": "A dying world where the light has grown thin and things gnaw at its edges.",
        "date": "The 9th Grey Winter", "time": "night", "tone": "grim",
        "factions": ["The Ashen Church", "The Vagrant Kings", "Those Below"],
        "locations": ["Gallowmere", "The Sunken Chapel", "Rookhollow"],
        "aspects": ["Faith is a weapon and a lie.", "The dead do not always stay buried."],
        "opening_scene": "A guttering candle, a locked door, and footsteps that shouldn't be there.",
        "opening_quest": "Survive the night and learn what the church is hiding."},
    "sci_fi": {
        "setting": "A fractured stellar frontier where old empires haggle over new worlds.",
        "date": "2412 CE, Standard Reckoning", "time": "midday", "tone": "adventurous",
        "factions": ["The Concord", "Freehaul Union", "The Silent Fleet"],
        "locations": ["Anchorage Station", "The Verge", "Dock 12"],
        "aspects": ["FTL is expensive and jealously controlled.",
                    "AI is legal, everywhere, and never quite trusted."],
        "opening_scene": "A docking clamp releases; a job you can't refuse waits three jumps out.",
        "opening_quest": "Deliver the cargo without learning what it is."},
    "cyberpunk": {
        "setting": "A neon megacity where the rain never stops and the data never sleeps.",
        "date": "2088, Night City reckoning", "time": "late_night", "tone": "noir",
        "factions": ["Arasaka-hana", "The Sprawl Collectives", "NetWatch"],
        "locations": ["The Combat Zone", "Kabuki Market", "Corpo Plaza"],
        "aspects": ["Everyone is for sale; the price is the only question.",
                    "Your body is mostly chrome and someone else's firmware."],
        "opening_scene": "A back-alley ripperdoc, a fresh implant, and a message you didn't send.",
        "opening_quest": "Find out who's wearing your identity across the net."},
    "post_apoc": {
        "setting": "The world after the end — rust, ruin, and the stubborn business of surviving.",
        "date": "Year 27 After", "time": "afternoon", "tone": "bleak",
        "factions": ["The Convoy", "Rustwater Holdfast", "The Bloom"],
        "locations": ["The Dry Sea", "Halberd Ruins", "Camp Ninety"],
        "aspects": ["Water is currency.", "The old tech still works, if it doesn't kill you."],
        "opening_scene": "A dead engine, a low canteen, and dust on the horizon that's moving.",
        "opening_quest": "Reach the next holdfast before the water runs out."},
    "historical": {
        "setting": "A grounded age of empire, intrigue, and hard roads.",
        "date": "The reign year of the old king", "time": "morning", "tone": "grounded",
        "factions": ["The Crown", "The Merchant Houses", "The Guild"],
        "locations": ["The Capital", "The Harbor District", "The Old Road"],
        "aspects": ["No magic — only steel, coin, and cunning.",
                    "Rank is everything and mercy is rare."],
        "opening_scene": "A crowded market square, a whispered name, and a purse you shouldn't have.",
        "opening_quest": "Clear your name before the magistrate hears the accusation."},
    "modern": {
        "setting": "The world as it is — ordinary on the surface, complicated underneath.",
        "date": "Present day", "time": "afternoon", "tone": "grounded",
        "factions": ["The City", "The Firm", "Old Friends"],
        "locations": ["Downtown", "The Waterfront", "Your Apartment"],
        "aspects": ["No magic, no monsters — just people and their secrets.",
                    "A phone is the deadliest weapon in the room."],
        "opening_scene": "A ringing phone at 2 a.m. and a favor you can't say no to.",
        "opening_quest": "Figure out why an old friend called, then vanished."},
}
_GENRE_TEMPLATES["custom"] = _GENRE_TEMPLATES["high_fantasy"]

# ------------------------------------------------------------------ genre packs (2026-07-06)
# The curated preset floor for NON-fantasy genres (live playtest: a sci_fi world was offered
# Spellcraft/Arcane Gift and "+1 archery" — the floor didn't exist outside fantasy). A pack
# `hide`s the fantasy-flavored registry entries and `adds` genre-true skills/abilities. Pack
# entries are NOT new registry rows: whatever the player picks is FROZEN into per-character
# `defs` at creation (snapshot overlay, doc 09 §1) — replay-pure, zero wire change, and the
# eligibility gate works because `skill_entry` reads defs first (requires_ability preserved).
# Shape mirrors registry/skills.toml + abilities: kind passive|active|basis ("basis" = grants
# the in-world basis for a gated skill — the Arcane Gift pattern, made a first-class kind).
GENRE_PACKS: dict[str, dict] = {
    "sci_fi": {
        "hide_skills": ["swordplay", "archery", "lockpicking", "lore", "spellcraft"],
        "hide_abilities": ["steady_hand", "arcane_gift", "power_strike"],
        "skills": {
            "gunnery": {"name": "Gunnery", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                        "desc": "Ranged weapons, turrets, and fire discipline."},
            "zero_g_ops": {"name": "Zero-G Operations", "keyed_stat": "CON", "base_mod": 0,
                           "max_rank": 5, "desc": "EVA, vacuum work, and microgravity motion."},
            "systems_intrusion": {"name": "Systems Intrusion", "keyed_stat": "INT", "base_mod": 0,
                                  "max_rank": 5, "requires_ability": "neural_lace",
                                  "desc": "Ghosting into computers. Requires a Neural Lace — "
                                          "you cannot declare access; you acquire it."},
            "tech_repair": {"name": "Tech Repair", "keyed_stat": "INT", "base_mod": 0,
                            "max_rank": 5, "desc": "Fixing machines with the parts you have."},
            "piloting": {"name": "Piloting", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                         "desc": "Ships, shuttles, drones — anything with thrust."},
            "medtech": {"name": "Medtech", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                        "desc": "Field medicine, nanite dosing, triage."},
            "scavenging": {"name": "Scavenging", "keyed_stat": "CUN", "base_mod": 0,
                           "max_rank": 5, "desc": "Reading wrecks: what's valuable, what's lethal."}},
        "abilities": {
            "neural_lace": {"name": "Neural Lace", "kind": "basis",
                            "effect": "Grants the in-world basis for Systems Intrusion checks."},
            "gene_tuned_reflexes": {"name": "Gene-Tuned Reflexes", "kind": "passive",
                                    "effect": "A tuned nervous system.",
                                    "passive_mod": {"skill": "piloting", "amount": 1}},
            "hardened_physiology": {"name": "Hardened Physiology", "kind": "passive",
                                    "effect": "Built for hard vacuum shifts.",
                                    "passive_mod": {"skill": "zero_g_ops", "amount": 1}},
            "combat_stims": {"name": "Combat Stims", "kind": "active", "resolution_mod": 2,
                             "effect": "Burn a stim dose to push one physical action."}}},
    "cyberpunk": {
        "hide_skills": ["swordplay", "archery", "lore", "spellcraft"],
        "hide_abilities": ["steady_hand", "arcane_gift", "power_strike"],
        "skills": {
            "firearms": {"name": "Firearms", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                         "desc": "Pistols to smart-rifles."},
            "netrunning": {"name": "Netrunning", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                           "requires_ability": "cranial_deck",
                           "desc": "Running the net. Requires a Cranial Deck — no deck, no dive."},
            "tech_craft": {"name": "Tech Craft", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                           "desc": "Hardware, implants, and improvised electronics."},
            "drive": {"name": "Drive", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                      "desc": "Cars, bikes, AVs — fast and wanted."},
            "streetwise": {"name": "Streetwise", "keyed_stat": "CUN", "base_mod": 0, "max_rank": 5,
                           "desc": "Who to pay, who to fear, where not to stand."},
            "corp_etiquette": {"name": "Corp Etiquette", "keyed_stat": "CHA", "base_mod": 0,
                               "max_rank": 5, "desc": "Passing in towers where a wrong word bills you."}},
        "abilities": {
            "cranial_deck": {"name": "Cranial Deck", "kind": "basis",
                             "effect": "Grants the in-world basis for Netrunning checks."},
            "wired_reflexes": {"name": "Wired Reflexes", "kind": "passive",
                               "effect": "Reaction time bought on credit.",
                               "passive_mod": {"skill": "firearms", "amount": 1}},
            "street_cred": {"name": "Street Cred", "kind": "passive",
                            "effect": "Your name opens back doors.",
                            "passive_mod": {"skill": "streetwise", "amount": 1}},
            "adrenal_booster": {"name": "Adrenal Booster", "kind": "active", "resolution_mod": 2,
                                "effect": "Implant surge for one desperate physical action."}}},
    "post_apoc": {
        "hide_skills": ["swordplay", "archery", "lore", "spellcraft"],
        "hide_abilities": ["arcane_gift"],
        "skills": {
            "firearms": {"name": "Firearms", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                         "desc": "Every bullet counted twice."},
            "wasteland_survival": {"name": "Wasteland Survival", "keyed_stat": "CON", "base_mod": 0,
                                   "max_rank": 5, "desc": "Water, shade, rads, and reading the dust."},
            "jury_rigging": {"name": "Jury-Rigging", "keyed_stat": "INT", "base_mod": 0,
                             "max_rank": 5, "desc": "Making broken things run one more day."},
            "old_tech_operation": {"name": "Old-Tech Operation", "keyed_stat": "INT", "base_mod": 0,
                                   "max_rank": 5, "requires_ability": "old_world_knowledge",
                                   "desc": "Waking pre-Fall machines. Requires Old-World Knowledge."},
            "barter": {"name": "Barter", "keyed_stat": "CHA", "base_mod": 0, "max_rank": 5,
                       "desc": "Trading when currency is water and trust."},
            "scavenging": {"name": "Scavenging", "keyed_stat": "CUN", "base_mod": 0, "max_rank": 5,
                           "desc": "Finding the unbroken thing in the broken world."}},
        "abilities": {
            "old_world_knowledge": {"name": "Old-World Knowledge", "kind": "basis",
                                    "effect": "Grants the in-world basis for Old-Tech Operation."},
            "rad_resistant": {"name": "Rad-Resistant", "kind": "passive",
                              "effect": "You keep going where counters scream.",
                              "passive_mod": {"skill": "wasteland_survival", "amount": 1}},
            "pack_rat": {"name": "Pack Rat", "kind": "passive",
                         "effect": "You never leave the good part behind.",
                         "passive_mod": {"skill": "scavenging", "amount": 1}}}},
    "modern": {
        "hide_skills": ["swordplay", "archery", "spellcraft"],
        "hide_abilities": ["steady_hand", "arcane_gift", "power_strike"],
        "skills": {
            "firearms": {"name": "Firearms", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                         "desc": "Range time shows."},
            "driving": {"name": "Driving", "keyed_stat": "DEX", "base_mod": 0, "max_rank": 5,
                        "desc": "Traffic, tails, and getaways."},
            "hacking": {"name": "Hacking", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                        "requires_ability": "hacker_background",
                        "desc": "Real intrusion. Requires a Hacker Background — not a montage."},
            "investigation": {"name": "Investigation", "keyed_stat": "CUN", "base_mod": 0,
                              "max_rank": 5, "desc": "Paper trails, people trails."},
            "first_aid": {"name": "First Aid", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                          "desc": "Keeping someone alive until sirens."}},
        "abilities": {
            "hacker_background": {"name": "Hacker Background", "kind": "basis",
                                  "effect": "Grants the in-world basis for Hacking checks."},
            "military_training": {"name": "Military Training", "kind": "passive",
                                  "effect": "Drilled until it's reflex.",
                                  "passive_mod": {"skill": "firearms", "amount": 1}},
            "medical_license": {"name": "Medical License", "kind": "passive",
                                "effect": "You've done this for real.",
                                "passive_mod": {"skill": "first_aid", "amount": 1}}}},
    "historical": {
        "hide_skills": ["spellcraft"],
        "hide_abilities": ["arcane_gift"],
        "skills": {
            "horsemanship": {"name": "Horsemanship", "keyed_stat": "DEX", "base_mod": 0,
                             "max_rank": 5, "desc": "Riding hard and arriving alive."},
            "etiquette": {"name": "Etiquette", "keyed_stat": "CHA", "base_mod": 0, "max_rank": 5,
                          "desc": "Court, guild, and table — rank is everything."},
            "navigation": {"name": "Navigation", "keyed_stat": "INT", "base_mod": 0, "max_rank": 5,
                           "desc": "Stars, charts, and roads that aren't on them."}},
        "abilities": {}},
}

# Genre-true Class/concept placeholder examples (the old fixed "Storm-Touched Skald" read
# absurd on a sci_fi sheet — live playtest note).
GENRE_CONCEPT_HINTS: dict[str, str] = {
    "high_fantasy": "e.g. Storm-Touched Skald", "dark_fantasy": "e.g. Plague-Doctor Turned Witness",
    "sci_fi": "e.g. Salvage Diver & Lace-Runner", "cyberpunk": "e.g. Burned-Out Corpo Fixer",
    "post_apoc": "e.g. Convoy Outrider", "historical": "e.g. Disgraced Guild Courier",
    "modern": "e.g. Night-Shift Paramedic Who Knows Too Much", "custom": "who are you?",
}


def _split_name_desc(line: str) -> tuple[str, str]:
    """'The Lattice Combine — salvage cartel that controls the docks' -> (name, desc).
    The Creator's one-box-per-row UI invites 'Name — description' lines; minting the WHOLE
    line as the entity name produced 80-char slug ids live (the vael_cora bug's Creator
    cousin). Split on the first em/en dash, ' - ', or ': ' — name head, description tail."""
    s = (line or "").strip()
    for sep in ("—", "–", " - ", ": "):
        if sep in s:
            head, _, tail = s.partition(sep)
            head, tail = head.strip(" -:—– "), tail.strip()
            if head:
                return head[:80], tail[:400]
    return s[:80], ""


def _resolve_skill_ref(token: str, candidates: dict) -> Optional[str]:
    """Resolve a model-written skill reference ('vac_ops') against known skill ids
    ('vacuum_operations'). Exact slug first; then unique prefix; then ordered token-prefix
    subsequence (vac_ops -> vacuum_operations, lace_intrusion -> neural_lace_intrusion).
    Ambiguous or unknown -> None (caller drops the mod — a dead reference must not look real)."""
    tok = slug(str(token or ""))
    if not tok:
        return None
    if tok in candidates:
        return tok
    starts = [c for c in candidates if c.startswith(tok)]
    if len(starts) == 1:
        return starts[0]

    def _tok_match(src: str, tgt: str) -> bool:
        # prefix ('vac'->'vacuum') or first-letter-anchored char subsequence
        # ('ops'->'operations') — abbreviations survive, unrelated words don't.
        if tgt.startswith(src):
            return True
        if not src or not tgt or src[0] != tgt[0]:
            return False
        i = 0
        for ch in tgt:
            if i < len(src) and ch == src[i]:
                i += 1
        return i == len(src)

    parts = [p for p in tok.split("_") if p]
    subseq = []
    for cid in candidates:
        cparts = cid.split("_")
        i = 0
        for cp in cparts:
            if i < len(parts) and _tok_match(parts[i], cp):
                i += 1
        if i == len(parts):
            subseq.append(cid)
    return subseq[0] if len(subseq) == 1 else None


_MAX_LIST = 20          # cap authored lists so a runaway model can't flood state
_TXT = 2000             # per-field prose clamp (roomy — the briefing budget governs downstream)


def _s(v, n=_TXT) -> str:
    return str(v if v is not None else "").strip()[:n]


def _lst(v) -> list:
    return list(v) if isinstance(v, list) else []


# ------------------------------------------------------------------ registry export (UI feed)
def registry_export(cfg=None) -> dict:
    """The curated registry as plain JSON for the creator window (stats/skills/abilities +
    the mod policy so the sheet can show live modifiers). Cached load — cold-path safe."""
    reg = registry.load(cfg)
    return {"version": reg.version, "mod_policy": reg.mod_policy,
            "dice": reg.dice, "tiers": reg.tiers,
            "stats": reg.stats, "skills": reg.skills, "abilities": reg.abilities,
            "items": reg.items, "slots": sorted(reg.slots),     # RPG-2: item templates + slots
            "effects": reg.effects,                             # RPG-3: Status/Condition presets
            "genres": GENRES, "times": list(TIMES),
            "genre_packs": GENRE_PACKS,                         # genre-true preset floor (UI)
            "concept_hints": GENRE_CONCEPT_HINTS}


# ------------------------------------------------------------------ deterministic fills
def deterministic_world(doc: dict) -> dict:
    """Fill a partial world doc from the genre template. Player fields always win."""
    doc = doc or {}
    genre = _s(doc.get("genre"), 40).lower() or "high_fantasy"
    if genre not in _GENRE_TEMPLATES:
        genre = "custom"
    tpl = _GENRE_TEMPLATES[genre]
    aspects = _lst(doc.get("aspects")) or list(tpl["aspects"])
    return {
        "name": _s(doc.get("name"), 80) or "Untitled World",
        "genre": genre,
        "setting": _s(doc.get("setting")) or tpl["setting"],
        "date": _s(doc.get("date"), 80) or tpl["date"],
        "time": (_s(doc.get("time"), 20).lower() if _s(doc.get("time"), 20).lower() in TIMES
                 else tpl["time"]),
        "tone": _s(doc.get("tone"), 60) or tpl["tone"],
        "factions": [_s(x, 80) for x in (_lst(doc.get("factions")) or tpl["factions"])][:_MAX_LIST],
        "locations": [_s(x, 80) for x in (_lst(doc.get("locations")) or tpl["locations"])][:_MAX_LIST],
        "npcs": _norm_npcs(doc.get("npcs")),
        "aspects": [_s(x) for x in aspects][:_MAX_LIST],
        "opening_scene": _s(doc.get("opening_scene")) or tpl["opening_scene"],
        "opening_quest": _s(doc.get("opening_quest")) or tpl["opening_quest"],
        "extras": _norm_extras(doc.get("extras")),
    }


def _norm_extras(extras) -> list:
    """Free-form custom detail CATEGORIES (Bean 2026-07-07): [{label, text}] the player invents
    for the world or character — a magic system, a history, a code of honor, a backstory beat.
    Kept as retrievable lore (the memory system IS AetherState's lorebook: injected on relevance,
    token-cheap), so the player can open up as many categories as they wish."""
    out = []
    for e in _lst(extras)[:_MAX_LIST]:
        if isinstance(e, dict):
            label = _s(e.get("label") or e.get("key") or e.get("name"), 60)
            text = _s(e.get("text") or e.get("value") or e.get("desc"))
            if label or text:
                out.append({"label": label or "Note", "text": text})
        elif isinstance(e, str) and e.strip():
            out.append({"label": "Note", "text": _s(e)})
    return out


def _norm_npcs(npcs) -> list:
    out = []
    for n in _lst(npcs)[:_MAX_LIST]:
        if isinstance(n, dict):
            name = _s(n.get("name"), 80)
            if name:
                out.append({"name": name, "role": _s(n.get("role"), 160),
                            "desc": _s(n.get("desc"), 600)})
        elif isinstance(n, str) and n.strip():
            out.append({"name": _s(n, 80), "role": "", "desc": ""})
    return out


def deterministic_player(doc: dict, cfg=None) -> dict:
    """Fill a partial Player Card from registry defaults + point-buy. Player fields win."""
    reg = registry.load(cfg)
    doc = doc or {}
    stats = {}
    given = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
    for sid, sdef in reg.stats.items():
        lo, hi = int(sdef.get("min", 1)), int(sdef.get("max", 20))
        val = given.get(sid, sdef.get("default", 10))
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = int(sdef.get("default", 10))
        stats[sid] = max(lo, min(hi, val))
    # Freeze freestyle defs FIRST: a frozen def skill is rankable and a frozen def ability is
    # knowable — filtering against registry ∪ defs (was registry-only) is what lets an authored
    # custom passive actually apply (its id must reach the player's known-abilities list).
    defs = _coerce_defs(doc.get("defs") or doc.get("custom"), reg)
    # RPG-5 floor (2026-07-07 live repro: GLM leaves every stat at baseline despite the
    # spend-6-points instruction — F6 persists): when ALL stats sit at default and the sheet
    # HAS ranked skills, spend deterministically along the concept's keyed stats.
    gsk0 = doc.get("skills") if isinstance(doc.get("skills"), dict) else {}
    if stats and gsk0 and all(
            int(v) == int((reg.stats.get(k) or {}).get("default", 10))
            for k, v in stats.items()):
        ranked = sorted(gsk0.items(), key=lambda kv: -_clampi(kv[1], 0, 99))
        spent, gives = [], [2, 2, 1, 1]
        merged_sk = {**reg.skills, **(defs.get("skills") or {})}
        for sid, _r in ranked:
            sdef = merged_sk.get(str(sid)) or merged_sk.get(slug(str(sid))) or {}
            keyed = str(sdef.get("keyed_stat", "")).upper()
            if keyed in stats and keyed not in spent:
                hi = int((reg.stats.get(keyed) or {}).get("max", 20))
                stats[keyed] = min(hi, stats[keyed] + gives[len(spent)])
                spent.append(keyed)
            if len(spent) >= len(gives):
                break
    def_skills = defs.get("skills", {})
    def_abils = defs.get("abilities", {})
    skills = {}
    gsk = doc.get("skills") if isinstance(doc.get("skills"), dict) else {}
    for sid, rank in gsk.items():
        key = str(sid)
        sdef = reg.skills.get(key) or def_skills.get(key)
        if sdef is None:                     # the model may rank an invented skill by display name
            key = slug(key)
            sdef = def_skills.get(key)
        if sdef is None:
            continue
        mx = int(sdef.get("max_rank", 5))
        try:
            skills[key] = max(0, min(mx, int(rank)))
        except (TypeError, ValueError):
            skills[key] = 0
    abilities = []
    for a in _lst(doc.get("abilities")):
        aid = str(a)
        if aid not in reg.abilities and aid not in def_abils:
            aid = slug(aid)                  # invented abilities may arrive by display name too
            if aid not in def_abils:
                continue
        if aid not in abilities:
            abilities.append(aid)
    for aid in def_abils:                    # RPG-5 floor (2026-07-07 live repro): a frozen
        if aid not in abilities:             # def ability on YOUR OWN sheet is definitionally
            abilities.append(aid)            # KNOWN — GLM listed the Quirk only under defs and
    abilities = abilities[:_MAX_LIST]        # its mechanics were dead on arrival
    hp_max = 20
    try:
        hp_max = max(1, min(10000, int((doc.get("resources") or {}).get("hp", {}).get("max", 20))))
    except (TypeError, ValueError, AttributeError):
        hp_max = 20
    # RPG-5 (doc 10 §6): pools. Stamina is universal; mana materializes only when the sheet
    # is magic-shaped (a basis ability, a gated skill, or a def that spends mana) — a
    # low-magic character never shows a Mana bar. All Console-editable afterwards.
    res_doc = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}

    def _pool(key: str, dflt: int) -> int:
        try:
            return max(0, min(10000, int((res_doc.get(key) or {}).get("max", dflt))))
        except (TypeError, ValueError, AttributeError):
            return dflt
    resources: dict = {"hp": {"max": hp_max}}
    stam = _pool("stamina", 12)
    if stam:
        resources["stamina"] = {"max": stam}
    magicish = any((a or {}).get("kind") == "basis" for a in def_abils.values()) \
        or any(isinstance(sk, dict) and "mana" in (sk.get("cost") or {})
               for sk in def_skills.values()) \
        or any((reg.skills.get(s) or {}).get("requires_ability") for s in skills)
    mana = _pool("mana", 10 if magicish else 0)
    if mana:
        resources["mana"] = {"max": mana}
    gear = [_s(g, 60) for g in _lst(doc.get("gear")) if _s(g)][:8]   # RPG-5 (G2): starting
    return {                                                         # gear finally SEEDS
        "name": _s(doc.get("name"), 80) or "Player",
        "sex": _s(doc.get("sex"), 40),
        "pronouns": _s(doc.get("pronouns"), 40),
        "species": _s(doc.get("species"), 120),
        "appearance": _s(doc.get("appearance") or doc.get("description"), 800),
        "concept": _s(doc.get("concept") or doc.get("class"), 200),
        "level": _clampi(doc.get("level", 1), 1, 999),
        "stats": stats, "skills": skills, "abilities": abilities,
        "defs": defs, "gear": gear, "extras": _norm_extras(doc.get("extras")),
        "resources": resources,
    }


def _clampi(v, lo, hi) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _coerce_defs(custom, reg) -> dict:
    """Freeze freestyle skills/abilities into a per-character `defs` snapshot (fixed numbers).
    A freestyle skill must key a real stat; numbers are clamped. This is the snapshot that the
    resolver reads snapshot-first (doc 09 §1) — so a bespoke mechanic resolves without ever being
    freestyle at roll time."""
    if not isinstance(custom, dict):
        return {}
    out: dict = {}
    for sk in _lst(custom.get("skills"))[:_MAX_LIST]:
        if not isinstance(sk, dict):
            continue
        sid = slug(_s(sk.get("id") or sk.get("name"), 40))
        keyed = _s(sk.get("keyed_stat"), 8).upper()
        if not sid or keyed not in reg.stats:
            continue
        entry = {
            "name": _s(sk.get("name") or sid, 60), "keyed_stat": keyed,
            "base_mod": _clampi(sk.get("base_mod", 0), -5, 10),
            "max_rank": _clampi(sk.get("max_rank", 5), 1, 10),
            "governs": ([_s(g, 40) for g in _lst(sk.get("governs"))][:12]
                        or [w for w in _s(sk.get("name") or sid, 60).lower()
                            .replace("-", " ").split() if len(w) >= 4][:4]),
            "desc": _s(sk.get("desc"), 400)}
        req = slug(_s(sk.get("requires_ability"), 40))
        if req:                                  # eligibility gate rides the def (validated below)
            entry["requires_ability"] = req
        grp = _s(sk.get("group") or sk.get("category"), 24)
        if grp:                                  # free-form category (Bean 07-07): "Spells",
            entry["group"] = grp                 # "Cyber-Ware", "Disciplines" — the HUD sections by it
        cost = sk.get("cost") if isinstance(sk.get("cost"), dict) else None
        if cost:                                 # RPG-5 (doc 10 §5.4): frozen resource cost —
            cc = {str(k).lower(): _clampi(v, 1, 10) for k, v in cost.items()   # clamped, never
                  if str(k).lower() in ("stamina", "mana", "hp")}              # model-typed at
            cc = {k: v for k, v in cc.items() if v > 0}                        # roll time
            if cc:
                entry["cost"] = cc
        out.setdefault("skills", {})[sid] = entry
    for ab in _lst(custom.get("abilities"))[:_MAX_LIST]:
        if not isinstance(ab, dict):
            continue
        aid = slug(_s(ab.get("id") or ab.get("name"), 40))
        if not aid:
            continue
        if aid in reg.abilities and not any(
                ab.get(k) for k in ("mechanic", "passive_mod", "cost",
                                    "cooldown_turns", "resolution_mod", "magnitude")):
            continue                             # a bare echo of a curated ability (author
        #                                          round-trips do this) — the registry def IS
        #                                          the truth; an inert copy must not shadow it
        kind = _s(ab.get("kind"), 10)
        if kind not in ("passive", "active", "basis"):   # "basis" (2026-07-06): grants the
            kind = "active"                              # in-world basis for a gated skill
        entry = {"name": _s(ab.get("name") or aid, 60), "kind": kind,
                 "effect": _s(ab.get("effect"), 400), "desc": _s(ab.get("desc"), 400)}
        # 2026-07-07 redesign: an ability's MECHANIC — how it bends the dice, frozen at authoring.
        # edge/ward = passive dice-shapers; extra_die/reroll/surge = active dice-shapers;
        # mod = legacy flat bonus; basis = a gate key. Everything clamped, never model-typed at roll.
        from . import registry as _registry
        mech = _s(ab.get("mechanic"), 16).lower()
        if mech not in _registry.ABILITY_MECHANICS:
            mech = "basis" if kind == "basis" else ""
        if not mech:                             # 2026-07-09: infer a missing mechanic from the
            txt = (_s(ab.get("effect"), 400) + " " + _s(ab.get("desc"), 400)).lower()
            if "extra die" in txt or "another die" in txt or "keep the best" in txt:
                mech = "extra_die" if kind == "active" else "edge"   # effect text (weak floor —
            elif "reroll" in txt or "re-roll" in txt:                # a typed row without a
                mech = "reroll"                                      # mechanic column still
            elif "fumble" in txt or "no critical" in txt:            # freezes as a real shaper)
                mech = "ward"
            elif "surge" in txt or "lifts the" in txt:
                mech = "surge"
        at = ab.get("applies_to")
        if isinstance(at, str) and at.strip():
            entry["applies_to"] = "all" if at.strip().lower() in ("all", "any") else slug(at)
        elif isinstance(at, (list, tuple)) and at:
            entry["applies_to"] = [slug(_s(x, 40)) for x in at if _s(x, 40)][:6]
        grp = _s(ab.get("group") or ab.get("category"), 24)
        if grp:                                  # free-form category (Bean 07-07): was locked to
            low = grp.lower()                    # talent|technique|spell — now any label the player
            entry["group"] = low if low in ("talent", "technique", "spell") else grp   # wants
        mag = ab.get("magnitude")
        if mech in ("extra_die", "reroll", "surge"):
            kind = entry["kind"] = "active"
            entry["mechanic"] = mech
            entry["magnitude"] = _clampi(mag if mag is not None else (2 if mech == "surge" else 1), 1, 4)
            entry["cooldown_turns"] = _clampi(ab.get("cooldown_turns", 1), 0, 10)
            cost = ab.get("cost") if isinstance(ab.get("cost"), dict) else {"stamina": 2}
            cc = {str(k).lower(): _clampi(v, 1, 10) for k, v in cost.items()
                  if str(k).lower() in ("stamina", "mana", "hp")}
            entry["cost"] = {k: v for k, v in cc.items() if v > 0} or {"stamina": 2}
        elif mech in ("edge", "ward"):
            kind = entry["kind"] = "passive"
            entry["mechanic"] = mech
            entry["magnitude"] = _clampi(mag if mag is not None else 1, 1, 3)
        elif mech == "basis":
            entry["mechanic"] = "basis"
        elif kind == "passive":
            pm = ab.get("passive_mod") if isinstance(ab.get("passive_mod"), dict) else {}
            skl = _s(pm.get("skill"), 40)
            if skl:
                entry["mechanic"] = "mod"
                entry["passive_mod"] = {"skill": slug(skl),
                                        "amount": _clampi(pm.get("amount", 1), -5, 5)}
        elif kind == "active":
            entry["resolution_mod"] = _clampi(ab.get("resolution_mod", 1), -5, 8)
        out.setdefault("abilities", {})[aid] = entry
    # Post-pass (2026-07-06, found live): resolve every cross-reference or drop it. GLM authored
    # a passive boosting 'vac_ops' while minting the skill as 'vacuum_operations' — the +1
    # could never apply and LOOKED real on the sheet. Dead references must not survive freezing.
    known = dict(reg.skills)
    known.update(out.get("skills", {}))
    for aid, entry in list(out.get("abilities", {}).items()):
        pm = entry.get("passive_mod")
        if pm:
            hit = _resolve_skill_ref(pm.get("skill"), known)
            if hit:
                pm["skill"] = hit
            else:
                del entry["passive_mod"]         # keep the ability as flavor, kill the dead mod
        at = entry.get("applies_to")             # 2026-07-07: resolve the target skill, or broaden
        if isinstance(at, str) and at != "all":  # to "all" so a shaper never silently applies to
            entry["applies_to"] = _resolve_skill_ref(at, known) or "all"   # a skill that isn't there
        elif isinstance(at, list):
            hits = [h for h in (_resolve_skill_ref(x, known) for x in at) if h]
            entry["applies_to"] = hits or "all"
    known_abils = set(reg.abilities) | set(out.get("abilities", {}))
    for sid, entry in list(out.get("skills", {}).items()):
        req = entry.get("requires_ability")
        if req and req not in known_abils:
            entry.pop("requires_ability", None)  # an unsatisfiable gate would brick the skill
    return out


# ------------------------------------------------------------------ doc -> shipped ops
def world_to_ops(world: dict) -> list[dict]:
    """Turn a finalized world doc into shipped ops (entities / memory-lore / scene). World is
    authored BEFORE the player, so no player-bound goal here — the opening quest is a lore note."""
    w = deterministic_world(world)
    ops: list[dict] = [{"op": "memory_event", "text": f"World — {w['name']} ({w['genre']}): {w['setting']}"}]
    ops.append({"op": "memory_event", "text": f"In-world date: {w['date']} ({w['time']})."})
    for line in w["aspects"]:
        if line:
            ops.append({"op": "memory_event", "text": f"World lore: {line}"})
    for f in w["factions"]:
        if f:
            name, desc = _split_name_desc(f)     # 'Name — description' lines: name is the id,
            ops.append({"op": "entity_add", "name": name, "kind": "faction"})
            if desc:                             # the description is an attribute (not the slug)
                ops.append({"op": "set_attribute", "entity": slug(name),
                            "key": "description", "value": desc})
    for loc in w["locations"]:
        if loc:
            name, desc = _split_name_desc(loc)
            ops.append({"op": "entity_add", "name": name, "kind": "location"})
            if desc:
                ops.append({"op": "set_attribute", "entity": slug(name),
                            "key": "description", "value": desc})
    for npc in w["npcs"]:
        ops.append({"op": "entity_add", "name": npc["name"], "kind": "npc"})
        eid = slug(npc["name"])
        if npc.get("role"):
            ops.append({"op": "set_attribute", "entity": eid, "key": "role", "value": npc["role"]})
        if npc.get("desc"):
            ops.append({"op": "set_attribute", "entity": eid, "key": "description", "value": npc["desc"]})
    if w["opening_scene"]:
        ops.append({"op": "memory_event", "text": f"Opening scene: {w['opening_scene']}"})
        if w["locations"]:
            scene_low = " " + " ".join(w["opening_scene"].lower()
                                       .replace("-", " ").split()) + " "
            pick, best = None, 0                 # the location the opening scene actually
            for loc in w["locations"]:           # NAMES wins; the first row is only the
                head = _split_name_desc(loc)[0]  # fallback (2026-07-09: the HUD opened on
                toks = " ".join(head.lower().replace("-", " ").split())   # 'the aerie' while
                if toks and f" {toks} " in scene_low and len(toks) > best:   # play began at
                    pick, best = head, len(toks)                             # the Atrium Stair)
                else:
                    t2 = " ".join(t for t in toks.split() if t not in ("the", "a", "an"))
                    if t2 and f" {t2} " in scene_low and len(t2) > best:
                        pick, best = head, len(t2)
            ops.append({"op": "scene_set",
                        "location": slug(pick or _split_name_desc(w["locations"][0])[0]),
                        "phase": "opening"})
    if w["time"] in TIMES:
        ops.append({"op": "time_advance", "to_time_of_day": w["time"]})
    if w["opening_quest"]:
        ops.append({"op": "memory_event", "text": f"Opening quest: {w['opening_quest']}"})
        qname = _split_name_desc(w["opening_quest"])[0][:80] or w["opening_quest"][:80]
        ops.append({"op": "quest_add", "name": qname,      # RPG-5 (G3): the opening quest is
                    "detail": w["opening_quest"][:300]})   # LEDGER truth, not just lore prose
    for ex in w.get("extras", []):                         # Bean 07-07: free-form custom lore
        if ex.get("text"):                                 # categories -> retrievable memory lore
            ops.append({"op": "memory_event",
                        "text": f"World lore — {ex['label']}: {ex['text']}"})
    return ops


def player_to_ops(player: dict, cfg=None) -> list[dict]:
    """Turn a finalized Player Card doc into [entity_add, player_seed, set_attribute...]. Mirrors
    the genesis `seed_player` op shape (privileged; applied with source='user')."""
    p = deterministic_player(player, cfg)
    name = p["name"]
    card = {"level": p["level"], "concept": p["concept"], "pronouns": p["pronouns"],
            "stats": p["stats"], "skills": p["skills"], "abilities": p["abilities"],
            "resources": p["resources"]}
    if p["defs"]:
        card["defs"] = p["defs"]
    ops: list[dict] = [{"op": "entity_add", "name": name, "kind": "player"},
                       {"op": "player_seed", "entity": name, "card": card}]
    eid = slug(name)
    if p["species"]:
        ops.append({"op": "set_attribute", "entity": eid, "key": "species", "value": p["species"]})
    if p["sex"]:
        ops.append({"op": "set_attribute", "entity": eid, "key": "sex", "value": p["sex"]})
    if p["concept"]:
        ops.append({"op": "set_attribute", "entity": eid, "key": "class", "value": p["concept"]})
    if p.get("appearance"):                     # player appearance/description — was missing
        ops.append({"op": "set_attribute", "entity": eid, "key": "appearance",
                    "value": p["appearance"]})   # entirely (only NPCs had one); HUD/Console/card read it
    for g in p.get("gear") or []:               # RPG-5 (G2): starting gear becomes INSTANCES —
        ops.append({"op": "item_gain", "char": name, "name": g})   # template names ground
    for ex in p.get("extras", []):              # Bean 07-07: free-form custom character detail
        if ex.get("text"):                      # categories -> retrievable lore about the PC
            ops.append({"op": "memory_event", "text": f"{name} — {ex['label']}: {ex['text']}"})
    return ops                                  # mechanics; the rest commit mechanics-free


# ------------------------------------------------------------------ state -> world doc
def world_from_state(state: dict) -> dict:
    """Best-effort inverse of world_to_ops: rebuild an editable world doc from committed
    state (2026-07-06 — 'no way to see the world details once set'). Reads only what
    world_to_ops wrote (entity kinds + the prefixed lore memories), so an organically
    grown session yields whatever fits and blanks elsewhere. Read-only helper."""
    state = state or {}
    ents = state.get("entities") or {}
    attrs = state.get("attributes") or {}
    doc: dict = {"name": "", "genre": "", "setting": "", "date": "", "time": "", "tone": "",
                 "factions": [], "locations": [], "npcs": [], "aspects": [],
                 "opening_scene": "", "opening_quest": ""}
    for eid, e in ents.items():
        kind, name = (e or {}).get("kind"), (e or {}).get("name") or eid
        if kind == "faction":
            doc["factions"].append(name)
        elif kind == "location":
            doc["locations"].append(name)
        elif kind == "npc":
            a = attrs.get(eid) or {}
            doc["npcs"].append({"name": name, "role": str(a.get("role") or ""),
                                "desc": str(a.get("description") or "")})
    for m in state.get("memories") or []:
        text = str((m or {}).get("text") or "")
        if text.startswith("World lore — "):
            head, _, body = text[len("World lore — "):].partition(": ")
            if head and body:                     # custom detail categories round-trip
                doc.setdefault("extras", []).append({"label": head.strip(), "text": body})
        elif text.startswith("World — "):
            head, _, setting = text.partition(": ")
            name_part = head[len("World — "):]
            if "(" in name_part:                  # only the "Name (genre)" head IS the world
                doc["setting"] = setting or doc["setting"]   # line — a custom-lore label
                doc["name"] = name_part[:name_part.rfind("(")].strip()   # ("The Drowning")
                doc["genre"] = name_part[name_part.rfind("(") + 1:].rstrip(")").strip()
            elif not doc.get("name"):             # legacy row without a genre: name only,
                doc["name"] = name_part.strip()   # first match wins, never clobbers
        elif text.startswith("In-world date: "):
            body = text[len("In-world date: "):].rstrip(".")
            if "(" in body:
                doc["date"] = body[:body.rfind("(")].strip()
                doc["time"] = body[body.rfind("(") + 1:].rstrip(")").strip()
            else:
                doc["date"] = body.strip()
        elif text.startswith("World lore: "):
            doc["aspects"].append(text[len("World lore: "):])
        elif text.startswith("Opening scene: "):
            doc["opening_scene"] = text[len("Opening scene: "):]
        elif text.startswith("Opening quest: "):
            doc["opening_quest"] = text[len("Opening quest: "):]
    return doc


# ------------------------------------------------------------------ assist-LLM authoring
_WORLD_SYSTEM = (
    "You are a world-building assistant for a tabletop RPG. The player gives you seed "
    "details — anywhere from one line to pages of lore. EVERYTHING they wrote is canon: "
    "build on it, weave every named person, place, faction, and idea they mention into the "
    "world, and fill in everything they left blank so the whole sheet is complete and "
    "internally consistent. Keep the player's own words verbatim where given; expand around "
    "them, never replace them. Output ONLY minified JSON, no prose, matching "
    "exactly this schema: {\"name\":str,\"genre\":str,\"setting\":str,\"date\":str,\"time\":str,"
    "\"tone\":str,\"factions\":[str],\"locations\":[str],\"npcs\":[{\"name\":str,\"role\":str,"
    "\"desc\":str}],\"aspects\":[str],\"opening_scene\":str,\"opening_quest\":str}. "
    "`time` must be one of: " + ", ".join(TIMES) + ". `setting` should be a substantial, "
    "vivid paragraph (or more) that captures what makes this world ITSELF. Give 4-6 factions "
    "with names that imply agendas, 5-8 locations, 4-8 npcs with sharp one-line hooks, 5-8 "
    "aspects (laws of the world: magic, tech, cosmology, taboos). Be evocative and SPECIFIC — "
    "proper nouns, concrete images, no generic fantasy filler. If the player gives `notes`, "
    "treat them as creative direction and follow them faithfully — any genre blend, tone, "
    "power level, or wild premise the player asks for is allowed. Keep physical geography and "
    "dates ARITHMETICALLY consistent: pick one spatial axis convention (what is up/down, "
    "higher/lower, inner/outer) and one calendar, and make every location description and "
    "every date agree with them — a reader must be able to do the math.")

_CHAR_SYSTEM_TMPL = (
    "You are a character-creation assistant for a tabletop RPG set in the world described. The "
    "player gives a few seed details; you fill in what they left blank into a complete character "
    "that FITS THE WORLD. Output ONLY minified JSON, no prose, matching this schema: "
    "{{\"name\":str,\"sex\":str,\"pronouns\":str,\"species\":str,\"appearance\":str,"
    "\"concept\":str,"
    "\"stats\":{{STAT:int}},\"skills\":{{skill_id:rank}},\"abilities\":[ability_id],"
    "\"gear\":[str],"
    "\"defs\":{{\"skills\":[{{\"id\":str,\"name\":str,\"keyed_stat\":STAT,\"base_mod\":int,"
    "\"max_rank\":int,\"governs\":[str],\"desc\":str,\"requires_ability\":str,\"group\":str,"
    "\"cost\":{{\"stamina\":int,\"mana\":int}}}}],"
    "\"abilities\":[{{\"id\":str,\"name\":str,"
    "\"kind\":\"active|passive|basis\","
    "\"mechanic\":\"edge|ward|extra_die|reroll|surge|mod|basis\","
    "\"applies_to\":str,\"magnitude\":int,\"group\":str,"
    "\"cost\":{{\"stamina\":int,\"mana\":int}},\"cooldown_turns\":int,"
    "\"passive_mod\":{{\"skill\":str,\"amount\":int}},"
    "\"resolution_mod\":int,\"effect\":str,\"desc\":str}}]}}}}. "
    "`concept` is the character's CLASS/archetype. STATS are: {stats}. Assign stats by point-buy "
    "in [{lo}..{hi}], defaulting to {default}, favouring the concept — SPEND about 6 points over "
    "baseline total (a fresh character should not be all-{default}s). `skills` MUST use only these "
    "ids: {skills}. `abilities` MUST use only these ids: {abilities}. If the concept needs a skill "
    "or ability NOT in those lists, INVENT it under `defs` (a frozen custom definition) — give it a "
    "real keyed_stat and numbers that fit the world's power level — then USE its id: rank an "
    "invented skill in `skills`, list an invented ability id in `abilities`. The 2-4 skills that "
    "DEFINE the concept must carry the highest ranks (2-3), invented ones included. In a `defs` "
    "ability, `kind` \"basis\" means it grants the in-world BASIS for a gated skill (its "
    "`requires_ability`). An ability's `mechanic` is HOW it bends the dice — NOT a flat number: "
    "`edge`=advantage (roll an extra die, keep the best) on `applies_to`; `ward`=no critical "
    "fumble on `applies_to`; `extra_die`=on a FAILED roll, roll another die and keep the best "
    "(active, the powerful one); `reroll`=reroll a failed roll (active); `surge`=a big bonus that "
    "ALSO lifts the outcome ceiling for one check (active); `basis`=grants a gated skill's basis. "
    "PREFER these dice-shapers over dull flat bonuses; use `mechanic`:`mod` + `passive_mod` only "
    "for a plain humble +1. `applies_to` is a skill id or \"all\"; `magnitude` is the extra dice or "
    "bonus (1-3); actives set `cost` (stamina/mana 1-5) and `cooldown_turns`; `group` is a "
    "free-form CATEGORY that sections the sheet — use talent (passive) / technique (active) / "
    "spell (magic) by default, OR invent a genre-true category and reuse it across related "
    "skills AND abilities (e.g. \"Spells\", \"Cyber-Ware\", \"Disciplines\", \"Hexes\") so the "
    "player's sheet groups them together. Skills are things you TRY (ranked, rolled); "
    "abilities are things you HAVE that RESHAPE the roll. Give the concept 1-2 signature abilities "
    "with real dice-shaping mechanics — be flavorful and specific, never generic. Every defs "
    "skill MUST include 3-6 `governs` verbs (the plain words a player would write to attempt "
    "it — e.g. dive, swim, descend for a diving skill); an active ability MUST name its "
    "`applies_to` skill id, its `cost`, and `cooldown_turns`. NEVER restate an ability that "
    "already exists in the preset list — pick it in `abilities` instead; defs are ONLY for "
    "new inventions. "
    "`appearance` is a vivid 1-3 sentence PHYSICAL description of the character (face, build, "
    "dress, notable marks) that fits the world — what someone would see on meeting them. "
    "`gear` is 2-5 STARTING ITEMS that fit the concept — plain names ('worn leather satchel', "
    "'combat knife'), no stats. A def skill may carry `cost` (stamina and/or mana, 1-5) when "
    "using it should visibly tire or drain the character; omit it otherwise. If the "
    "player gives `notes`, treat them as creative direction and follow them faithfully.")


def _pack_for(world: Optional[dict]) -> Optional[dict]:
    """The genre pack for a world doc's genre (None when the registry already fits)."""
    if not isinstance(world, dict):
        return None
    return GENRE_PACKS.get(_s(world.get("genre"), 40).lower())


def _char_system(reg, pack: Optional[dict] = None) -> str:
    """Preset vocabulary for the authoring prompt. With a genre pack: fantasy-flavored
    registry entries are hidden and pack entries offered — the same curated floor the
    sheet shows (2026-07-06, the sci-fi neutrality fix)."""
    skills = dict(reg.skills)
    abilities = dict(reg.abilities)
    if pack:
        for sid in pack.get("hide_skills", []):
            skills.pop(sid, None)
        for aid in pack.get("hide_abilities", []):
            abilities.pop(aid, None)
        skills.update(pack.get("skills", {}))
        abilities.update(pack.get("abilities", {}))
    return _CHAR_SYSTEM_TMPL.format(
        stats=", ".join(reg.stats.keys()),
        skills=", ".join(skills.keys()) or "(none)",
        abilities=", ".join(abilities.keys()) or "(none)",
        lo=1, hi=20, default=10)


def _inject_pack_defs(doc: dict, pack: Optional[dict]) -> dict:
    """Freeze REFERENCED genre-pack entries into the doc's custom defs so ranks/picks on
    pack ids survive deterministic_player (pack entries are not registry rows — they become
    per-character `defs`, the snapshot overlay, exactly like authored freestyle)."""
    if not pack:
        return doc
    doc = dict(doc or {})
    cust = doc.get("custom") if isinstance(doc.get("custom"), dict) else \
        (doc.get("defs") if isinstance(doc.get("defs"), dict) else {})
    cust = {"skills": list(_lst(cust.get("skills"))), "abilities": list(_lst(cust.get("abilities")))}
    have_sk = {slug(_s((c or {}).get("id") or (c or {}).get("name"), 40))
               for c in cust["skills"] if isinstance(c, dict)}
    have_ab = {slug(_s((c or {}).get("id") or (c or {}).get("name"), 40))
               for c in cust["abilities"] if isinstance(c, dict)}
    ranked = {slug(str(k)) for k in (doc.get("skills") or {})} if isinstance(doc.get("skills"), dict) else set()
    picked = {slug(str(a)) for a in _lst(doc.get("abilities"))}
    needed_ab = set()
    for sid, sdef in pack.get("skills", {}).items():
        if sid in ranked and sid not in have_sk:
            cust["skills"].append({**sdef, "id": sid})
            if sdef.get("requires_ability"):
                needed_ab.add(sdef["requires_ability"])
    for aid, adef in pack.get("abilities", {}).items():
        if aid not in picked and aid not in needed_ab:
            continue
        if aid not in have_ab:
            cust["abilities"].append({**adef, "id": aid})
            continue
        for i, row in enumerate(cust["abilities"]):    # an id-matching custom row exists: if it
            rid = slug(_s((row or {}).get("id") or (row or {}).get("name"), 40))
            if rid == aid and isinstance(row, dict) and not any(
                    row.get(k) for k in ("mechanic", "passive_mod", "cost",
                                         "cooldown_turns", "resolution_mod", "magnitude")):
                cust["abilities"][i] = {**adef, "id": aid}   # carries no mechanics it is an echo
                break                                        # — the curated pack def wins
    doc["custom"] = cust
    return doc


def _world_user(seed: dict, world_ctx: str = "") -> str:
    """EVERY filled field rides along as context (2026-07-06: the old version dropped
    npcs/opening_scene/opening_quest, so filled boxes were invisible to the model and
    the fill ignored them)."""
    lines = ["Player's seed details (all of this is canon — build on it, fill in the rest):"]
    for k in ("name", "genre", "date", "time", "tone"):
        if _s(seed.get(k)):
            lines.append(f"- {k}: {_s(seed.get(k))}")
    if _s(seed.get("setting")):
        lines.append(f"- setting (verbatim, any length):\n{_s(seed.get('setting'), 6000)}")
    for k in ("factions", "locations", "aspects"):
        vals = [v for v in _lst(seed.get(k)) if _s(v)]
        if vals:
            lines.append(f"- {k}: {'; '.join(_s(v, 200) for v in vals)}")
    npcs = _norm_npcs(seed.get("npcs"))
    if npcs:
        lines.append("- npcs: " + "; ".join(
            f"{n['name']}" + (f" ({n['role']})" if n['role'] else "")
            + (f" — {n['desc']}" if n['desc'] else "") for n in npcs))
    for k, label in (("opening_scene", "opening scene"), ("opening_quest", "opening quest")):
        if _s(seed.get(k)):
            lines.append(f"- {label}: {_s(seed.get(k))}")
    for ex in _norm_extras(seed.get("extras")):       # free-form custom categories are canon
        if ex["text"]:
            lines.append(f"- {ex['label']} (canon, keep verbatim, build around it): {ex['text']}")
    if _s(seed.get("notes")):
        lines.append(f"- creative direction: {_s(seed.get('notes'), 4000)}")
    if len(lines) == 1:
        lines.append("- (the player left everything blank — invent a compelling world)")
    return "\n".join(lines)


def _char_user(seed: dict, world: Optional[dict]) -> str:
    lines = []
    if world:
        w = deterministic_world(world)
        lines.append(f"WORLD: {w['name']} — genre {w['genre']}, tone {w['tone']}. {w['setting']}")
        if w["factions"]:
            lines.append("Factions: " + ", ".join(w["factions"]))
        if w["locations"]:
            lines.append("Locations: " + ", ".join(w["locations"]))
        if w["aspects"]:
            lines.append("World aspects: " + " | ".join(w["aspects"][:8]))
        if w["npcs"]:
            lines.append("NPCs: " + "; ".join(
                n["name"] + (f" ({n['role']})" if n.get("role") else "") for n in w["npcs"][:8]))
    lines.append("Player's character seed (all of this is canon — fill in everything else):")
    for k in ("name", "sex", "pronouns", "species", "concept", "class"):
        if _s(seed.get(k)):
            lines.append(f"- {k}: {_s(seed.get(k))}")
    if isinstance(seed.get("stats"), dict) and seed["stats"]:
        lines.append("- stats given: " + ", ".join(f"{k}={v}" for k, v in seed["stats"].items()))
    if isinstance(seed.get("skills"), dict) and seed["skills"]:
        lines.append("- skill ranks given: " + ", ".join(
            f"{k}={v}" for k, v in seed["skills"].items()))
    if _lst(seed.get("abilities")):
        lines.append("- abilities picked: " + ", ".join(str(a) for a in seed["abilities"][:20]))
    if _lst(seed.get("gear")):
        lines.append("- starting gear given: " + ", ".join(
            _s(g, 60) for g in seed["gear"][:8] if _s(g)))
    cust = seed.get("custom") if isinstance(seed.get("custom"), dict) else {}
    for kind in ("skills", "abilities"):
        names = [_s((c or {}).get("name"), 60) for c in _lst(cust.get(kind))
                 if isinstance(c, dict) and _s((c or {}).get("name"))]
        if names:
            lines.append(f"- custom {kind} the player already defined: " + ", ".join(names))
    for ex in _norm_extras(seed.get("extras")):       # free-form custom character categories
        if ex["text"]:
            lines.append(f"- {ex['label']} (canon, keep verbatim, build around it): {ex['text']}")
    if _s(seed.get("notes")):
        lines.append(f"- creative direction: {_s(seed.get('notes'), 4000)}")
    if len([x for x in lines if x.startswith("- ")]) == 0:
        lines.append("- (mostly blank — invent a character that fits the world)")
    return "\n".join(lines)


# Creative authoring knobs (2026-07-06 live repro): temperature 0.0 wrote template-grade
# prose, and the shared 25 s mechanics timeout expired before a large model finished 2-4k
# tokens of world JSON — every auto-fill silently fell back to templates. Authoring is
# creation-time cold path, so a long wait is fine.
# 2026-07-07 (Bean): the char sheet + world docs got CUT OFF mid-JSON — a full sheet (stats,
# skills, abilities, nested defs, gear, appearance) can run well past 4k tokens, so the reply
# truncated and _json_or_none salvaged only a partial character. Raised the ceiling with room
# to spare; the timeout scales with it so a big model can actually finish.
_AUTHOR_TEMP = 0.9
_AUTHOR_TIMEOUT_S = 240.0
_AUTHOR_MAX_TOKENS = 9000


def _row_head(row) -> str:
    """Normalized NAME head of a 'Name — description' row (or an npc/extra dict) for
    seed-vs-authored duplicate detection."""
    if isinstance(row, dict):
        row = row.get("name") or row.get("label") or ""
    head = _split_name_desc(str(row or ""))[0]
    return " ".join(w for w in str(head).lower().replace("-", " ").split() if w)[:60]


def _keep_seed_rows(seed_rows, model_rows, cap: int = 12) -> list:
    """The player's typed rows pass through VERBATIM (they are canon — the model's echo may
    be rewritten or truncated); the model contributes only rows whose name-head is new."""
    out = [r for r in seed_rows if (isinstance(r, dict) and (r.get("name") or r.get("label")
                                                             or r.get("text"))) or _s(r)]
    have = {_row_head(r) for r in out if _row_head(r)}
    for r in model_rows:
        h = _row_head(r)
        if h and h not in have:
            out.append(r)
            have.add(h)
        if len(out) >= cap:
            break
    return out


async def author_world(get_client, cfg, ep, seed: dict) -> dict:
    """LLM-author the blanks of a world seed, then deterministic-fill + clamp.

    Returns source='llm' with the doc, or source='error' with a human-readable detail —
    the Creator shows the error and leaves the form alone instead of silently swapping
    in templates (the caller can still request the deterministic fill explicitly)."""
    try:
        raw = await _chat(get_client, cfg, ep, _WORLD_SYSTEM, _world_user(seed),
                          max_tokens=_AUTHOR_MAX_TOKENS, temperature=_AUTHOR_TEMP,
                          timeout_s=_AUTHOR_TIMEOUT_S)
        if raw is None:
            return {"source": "error",
                    "detail": f"no reply from {ep.base_url} ({ep.model}) — the call timed "
                              "out or the endpoint rejected it; try again or pick another "
                              "model in the menu"}
        parsed = _json_or_none(raw)
        if isinstance(parsed, dict):
            merged = {**seed, **{k: v for k, v in parsed.items() if v not in (None, "", [])}}
            # player-given scalars always win over the model
            for k in ("name", "genre", "setting", "date", "time", "tone",
                      "opening_scene", "opening_quest", "notes"):
                if _s(seed.get(k)):
                    merged[k] = seed[k]
            for k in ("factions", "locations", "aspects", "npcs", "extras"):
                seed_rows = _lst(seed.get(k))       # typed rows are canon: verbatim, never the
                if seed_rows:                       # model's (possibly mangled) echo of them
                    merged[k] = _keep_seed_rows(seed_rows, _lst(parsed.get(k)))
            return {"source": "llm", "doc": deterministic_world(merged)}
        log.warning("world authoring: unparseable reply (%d chars): head=%r tail=%r",
                    len(raw), raw[:200], raw[-240:])
        return {"source": "error",
                "detail": f"{ep.model} replied but not with usable JSON — try again"}
    except Exception as exc:                 # fail-open: report, never crash the route
        log.warning("world authoring failed open: %s", type(exc).__name__)
        return {"source": "error", "detail": f"authoring failed: {type(exc).__name__}"}


async def author_player(get_client, cfg, ep, seed: dict, world: Optional[dict] = None) -> dict:
    """LLM-author the blanks of a character seed against the world, then clamp to registry.
    Same source='llm'|'error' contract as author_world."""
    reg = registry.load(cfg)
    pack = _pack_for(world)
    try:
        raw = await _chat(get_client, cfg, ep, _char_system(reg, pack), _char_user(seed, world),
                          max_tokens=_AUTHOR_MAX_TOKENS, temperature=_AUTHOR_TEMP,
                          timeout_s=_AUTHOR_TIMEOUT_S)
        if raw is None:
            return {"source": "error",
                    "detail": f"no reply from {ep.base_url} ({ep.model}) — the call timed "
                              "out or the endpoint rejected it; try again or pick another "
                              "model in the menu"}
        parsed = _json_or_none(raw)
        if isinstance(parsed, dict):
            merged = dict(parsed)
            for k in ("name", "sex", "pronouns", "species", "concept", "appearance", "notes"):
                if _s(seed.get(k)):
                    merged[k] = seed[k]
            if isinstance(seed.get("stats"), dict):
                merged.setdefault("stats", {}).update(seed["stats"])
            sc = seed.get("custom") if isinstance(seed.get("custom"), dict) else {}
            mc = merged.get("custom") if isinstance(merged.get("custom"), dict) else {}
            if sc.get("skills") or sc.get("abilities"):   # typed custom mechanics are canon —
                merged["custom"] = {                      # the model may only APPEND new ones
                    "skills": _keep_seed_rows(_lst(sc.get("skills")), _lst(mc.get("skills"))),
                    "abilities": _keep_seed_rows(_lst(sc.get("abilities")),
                                                 _lst(mc.get("abilities")))}
            if _lst(seed.get("gear")):
                merged["gear"] = _keep_seed_rows(_lst(seed.get("gear")),
                                                 _lst(merged.get("gear")), cap=10)
            if _lst(seed.get("extras")):
                merged["extras"] = _keep_seed_rows(_lst(seed.get("extras")),
                                                   _lst(merged.get("extras")))
            merged = _inject_pack_defs(merged, pack)   # ranks on pack ids must freeze into defs
            return {"source": "llm", "doc": deterministic_player(merged, cfg)}
        log.warning("player authoring: unparseable reply (%d chars): head=%r tail=%r",
                    len(raw), raw[:200], raw[-240:])
        return {"source": "error",
                "detail": f"{ep.model} replied but not with usable JSON — try again"}
    except Exception as exc:
        log.warning("player authoring failed open: %s", type(exc).__name__)
        return {"source": "error", "detail": f"authoring failed: {type(exc).__name__}"}


# ------------------------------------------------------------------ RPG-5: Q27 evolution
_EVOLVE_SYSTEM = (
    "You are the mechanics assistant for a tabletop RPG engine. A character's skill just "
    "crossed a MASTERY BRACKET through real play; author its EVOLVED FORM. Keep the same id "
    "and keyed_stat; you may refine the name (an evolved title), rewrite desc/effect to show "
    "the growth, widen `governs`, and raise base_mod by AT MOST +1 over the current value. "
    "Never change what the skill fundamentally is; never touch requires_ability. Output ONLY "
    "minified JSON: {\"name\":str,\"keyed_stat\":str,\"base_mod\":int,\"max_rank\":int,"
    "\"governs\":[str],\"desc\":str}")


async def evolve_def_snapshot(store, cfg, get_client, ep, session_id: str, branch_id: str,
                              char_eid: str, table: str, sid: str, bracket: str,
                              turn: Optional[int] = None) -> None:
    """RPG-5 (doc 10 §4 / Q27): cold-path mastery re-authoring. The assist LLM proposes the
    evolved form; this validates, clamps, and FREEZES it as a new per-character def version
    via a privileged evolve_def op (the journal keeps every prior version for replay).
    Fail-open at every step — the curated bracket bonus already applied is the floor."""
    from . import assist
    from .state import apply_delta, current_state
    try:
        state = current_state(store, branch_id)
        pl = (state.get("player") or {}).get(char_eid)
        if not isinstance(pl, dict) or table != "skills":
            return
        reg = registry.load(cfg)
        cur = dict(((pl.get("defs") or {}).get("skills") or {}).get(sid)
                   or reg.skills.get(sid) or {})
        if not cur:
            return
        mastery = int((pl.get("mastery") or {}).get(sid, 0))
        ep = await assist.resolve_endpoint(get_client, cfg, ep)
        user = (f"CURRENT DEFINITION of '{sid}':\n{json.dumps(cur, ensure_ascii=False)}\n"
                f"MASTERY: {mastery} — just reached the {bracket} bracket.\n"
                f"Character concept: {_s(pl.get('concept'), 200) or 'unknown'}.\nJSON:")
        raw = await assist._chat(get_client, cfg, ep, _EVOLVE_SYSTEM, user,
                                 max_tokens=800, temperature=0.8, timeout_s=90.0)
        parsed = assist._json_or_none(raw) if raw else None
        if not isinstance(parsed, dict):
            return
        parsed.setdefault("keyed_stat", cur.get("keyed_stat"))
        parsed["base_mod"] = min(_clampi(parsed.get("base_mod", cur.get("base_mod", 0)),
                                         -5, 10),
                                 int(cur.get("base_mod", 0)) + 1)   # at most +1 per bracket
        coerced = _coerce_defs({"skills": [{**parsed, "id": sid}]}, reg
                               ).get("skills", {}).get(slug(sid))
        if not coerced:
            return
        for k in ("requires_ability", "cost"):   # engine-owned rows survive evolution intact
            if cur.get(k) is not None:
                coerced[k] = cur[k]
            else:
                coerced.pop(k, None)
        t = turn if turn is not None else state.get("meta", {}).get("turn", -1)
        r = apply_delta(store, session_id, branch_id, max(0, t),
                        [{"op": "evolve_def", "char": char_eid, "table": "skills",
                          "id": slug(sid), "def": coerced,
                          "note": f"mastery evolution: {bracket}"}], "rule", cfg)
        if r.applied:
            log.info("evolved %s/%s to %s bracket form", char_eid, sid, bracket)
    except Exception as exc:
        log.warning("evolution authoring failed open: %s", type(exc).__name__)
