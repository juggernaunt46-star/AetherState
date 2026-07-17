# Cross-Genre Capability Glossary Gap Audit — 2026-07-13

## Scope

The audit compares AetherState's preserved enemy/capability corpus and RPG registry against 31
requested genre facets. Parallel research sampled multiple genre and game traditions, public-health
and climate references where those domains require precise distinctions, and the current local
corpus. The resulting vocabulary is original normalized terminology; no source stat block, spell
description, or rules passage is copied.

The exact genre rows, terms, priorities, false-friend warnings, and source URLs are machine-readable
under `genres/` and `sources.json`.

The sealed result covers all 31 genre facets through 12 shared categories and 265 canonical concepts.
Its provenance index contains 122 source records: five local/project records and 117 distinct web
references gathered across the genre clusters.

## Baseline found

The existing corpus was strong at immediate attacks and grounded delivery:

- 18 functional families and 15 semantic primitives;
- broad weapons, natural anatomy, hazards, technology, magic, undead, and supernatural bases;
- combat manifestations, payloads, negative evidence, roles, tells, cadence, and counterplay;
- ten registry skills, six abilities, and 26 effect presets;
- direct single-target HP settlement for `direct_pressure` and `committed_strike`, plus exact Brace.

The baseline was much thinner for noncombat capability meaning, accumulated state, resources,
environment, travel, investigation, social/economic structures, transformation, institutions, and
world-scale change. Existing words such as healing, warding, summoning, EMP, radiation, infection,
and teleportation often appeared only as negative or unsupported vocabulary, not as a complete
translation record.

In each genre row, `existing_corpus` means the canonical concept already existed in the preserved
family, primitive, basis, or enemy corpus; `registry_corpus` means it existed as a canonical registry
skill, ability, or effect; and `gap_filled` means this work introduced the canonical distinction. The
marker does not claim that every listed genre phrase previously existed verbatim.

## Twelve shared categories

Every genre is mapped through the same twelve categories so a setting does not become its own
incompatible rules island:

1. offense;
2. defense and reaction;
3. buff and support;
4. status and condition;
5. movement and travel;
6. control, position, and terrain;
7. summon, deploy, and transform;
8. resource and cost;
9. equipment, technology, and magic;
10. social and investigation;
11. crafting, survival, and logistics;
12. world scale and authority.

Genre manifestations point into shared canonical concepts. A radiant ward, riot shield, scrap
barricade, starship bulkhead, firewall, and flood barrier may share parts of guard/barrier meaning
without becoming mechanically identical definitions.

## Highest-value gaps

The largest cross-genre recognition gaps were:

- persistent statuses, duration, stacking, removal, and causal tracks;
- resources and costs beyond HP, mana, and stamina;
- utility magic, rituals, cultivation, divine/occult polarity, and setting-law weaknesses;
- vehicles, ships, zero gravity, exposure, long-distance travel, worlds, planes, and timelines;
- hacking/access, surveillance, clues, investigation, information authority, and false identities;
- crafting, repair, medicine, scavenging, shelter, logistics, scarcity, and environmental survival;
- transformations, summons, companions, avatars, respawn, infection, possession, and lineage;
- reputation, debts, factions, schools, guilds, kingdoms, territory, armies, climate, and other
  world-scale state.

These are now represented as recognition concepts and genre translations. Most remain receipt gaps.

## Distinctions that must not collapse

- Biological infection, zombie transformation, cyber malware, lycanthropy, possession, and occult
  corruption have different causes and state tracks.
- Immediate radiation harm, accumulated dose, radiation sickness, contamination, and a persistent
  radiation zone are separate meanings.
- Combat displacement and population displacement are separate.
- Hunger for food and vampiric blood hunger are separate resources.
- Fear, panic, stress, trauma, sanity, morale, humanity/control, rage, and corruption are not one
  universal meter.
- A Player, account, avatar, NPC, vehicle, starship, settlement, faction, army, fleet, and world are
  different actors or scales.
- Quarantine concerns potential exposure; isolation concerns known infection.
- Wuxia martial skill, xianxia cultivation, ordinary magic, divine miracles, occult rites, psionics,
  cyberware, and virtual-system permissions are different grounding bases.
- Genre identity does not imply every trope: vampire weaknesses, bite transmission, full-moon
  compulsion, moral alignment, respawn, FTL behavior, divine polarity, and isekai system rules are
  world-law facets.

## Receipt implications

Broad recognition does not claim broad execution. The most reusable future receipt families exposed
by the audit are status application/removal, resource adjustment/spend, movement, zones, barriers,
reactions, deploy/summon, transformation, scheduled effects, information reveal/access, equipment
state, travel, crafting/survival, objectives, faction relations, and world events.

Those are gap categories, not implemented receipt names. Each must still receive its own reducer,
authority rules, replay payload, retry behavior, HUD/briefing state, narrator boundary, migration,
and adversarial proof before it can execute.

## Outcome

The glossary fills the recognition and translation gap for all 31 genre facets while preserving the
old corpus as named baseline evidence. It does not migrate enemy runtime generation, grant new Player
power, or claim that recognition-only concepts are mechanics.
