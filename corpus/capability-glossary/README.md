# AetherState Cross-Genre Capability Glossary

This is the first genre-complete domain of AetherState's machine-translation-style language and
world memory. It maps unrestricted and setting-specific wording into stable concept identifiers,
then supplies those identifiers to the separate world/actor definition, receipt, ledger, and
narration gates.

It is not a command grammar, an exhaustive move list, or permission to grant a power.

## Authority boundary

1. **Recognized:** the glossary found a plausible canonical meaning and retained its genre wording.
2. **Authorized:** a world or actor owns an immutable `capability-definition/1` revision.
3. **Executable:** that exact definition names an admitted receipt whose reducer can commit and
   replay the consequence.

A match stops at recognition. A frozen definition may preserve lore-only or narration-boundary
meaning. Missing receipts never become generic HP damage, a free status, or a silently invented
power.

## Contents

- `categories.json` defines the twelve broad Domain Shelves used in every genre, with explicit
  inclusions, exclusions, examples, and counterexamples.
- `taxonomy.json` defines the non-authoritative Semantic Atlas: classification planes, Concept Kinds,
  Scale Profile axes, Authority Chain stages, Cube faces, meaning facets, and future world-event fields.
- `concepts.json` is the canonical catalog. The deterministic builder imports the existing 18 enemy
  families, 15 semantic primitives, RPG registry skills/abilities/effects, and grounding bases before
  adding cross-genre concepts. Every concept includes conservative meaning facets and a meaning
  fingerprint that excludes labels, aliases, provenance, and current support.
- `genres/*.json` maps all 31 requested genre facets into those canonical concepts. Every row records
  the genre terms, its pre-glossary baseline (`existing_corpus`, `registry_corpus`, or `gap_filled`),
  and gap priority.
- `sources.json` holds local-corpus provenance and normalized web-research URLs. Source material is
  used for coverage checking and original normalization, not copied rules prose.
- `research/2026-07-13-genre-gap-audit.md` records the cross-genre findings and category decisions.
- `manifest.json` seals schemas, counts, exact artifact hashes, and the recognition/authority/receipt
  boundary.

The baseline marker describes where the canonical meaning existed before this glossary, not whether
every genre phrase already appeared verbatim. `existing_corpus` means the concept existed in the
preserved family, primitive, basis, or enemy corpus; `registry_corpus` means a canonical RPG
skill/ability/effect existed in the registry; and `gap_filled` means this work added the canonical
distinction.

## Cold-path services

`aetherstate.capability_glossary` validates and indexes the artifacts, returns deterministic lexical
and genre candidates, previews support classification, and freezes content-addressed
`capability-definition/1` revisions for `skill`, `ability`, `spell`, `augment`, `cyberware`, and
`enemy_move`.

The Semantic Atlas is classification only. `multiple` does not enable AoE, `zone` does not admit an
area receipt, `world_rule` does not create world truth, and `capability_identity` does not prove
assignment.

The loader verifies every manifest-declared artifact's exact byte count and SHA-256 fingerprint
before parsing it. Compiler v1 deliberately admits no active receipts because no immutable
definition storage/assignment/runtime adapter exists yet. A requested adapter remains visible in the
frozen record while active `receipt_id` stays null. A future adapter must require an explicit
`receipt_concept_ids` subset and prove its real reducer envelope so lore-only concepts cannot inherit
execution by sharing a definition with a settleable concept.

Corpus loading and definition compilation are cold-path Creator/import/authoring work. They do not
join the token-stream relay path. The existing runtime enemy generator remains authoritative and its
kit fingerprints are unchanged.

## Rebuild and validation

From the personal repository root:

```text
python tools/build_capability_glossary.py
python tools/finalize_capability_glossary.py
pytest -q tests/test_capability_glossary.py
```

The first command deterministically refreshes the shared concepts from preserved corpus truth. The
second preflights all 31 genres and their references, constructs every generated artifact in memory,
atomically replaces those artifacts, then publishes the manifest seal last.
