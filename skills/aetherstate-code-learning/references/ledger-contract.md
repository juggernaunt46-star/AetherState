# Public Proofbook ledger contract

## Canonical public surfaces

- `proofbook/CONTRACT.md`: record schema, authority, lifecycle, privacy, and currentness.
- `proofbook/PUBLICATION_POLICY.md`: public evidence and destination policy.
- `proofbook/TAGS.json`: reviewed tag identifiers and aliases.
- `proofbook/GENESIS.json`: immutable seal over the ordered initial public prefix.
- `proofbook/LEDGER.jsonl`: append-only public lesson revisions.
- `tools/proofbook/engineering_learning_core.py`: strict validation and record identities.
- `tools/proofbook/engineering_learning.py`: validation, status, briefing, and explicit admission.

## Authority

Proofbook is developer-only. It is never gameplay authority, never Player data, and never
model-training data. Source and tests outrank lessons. A lesson cannot grant mechanics, truth,
state, identity, settlement, narration, retrieval authority, or runtime permission.

## Destination gate

Classify the lesson destination before assembling evidence:

- **PUBLIC:** all evidence is present in the public repository, publication-safe, and verifiable
  through repository-relative paths.
- **PRIVATE:** any evidence is private or unavailable in the public repository.
- **ABSTAIN:** when destination or evidence safety is ambiguous.

Never copy a private lesson into the public ledger. Never substitute a summary, redaction, private
reference, remote-only reference, or claim of prior review for missing public evidence.

## Record admission

Each record must follow the schema declared by the public contract and contain:

1. One stable lesson key and monotonic revision.
2. One exact domain diagnosis; Semantic Cube records also require a valid boundary and failure
   class.
3. A bounded symptom, proven cause, imperative repair rule, supported scale, and explicit
   non-support.
4. Current repository-relative owners and regressions with exact symbols or nodes and SHA-256
   identities.
5. Present public evidence with a closed evidence kind.
6. Reviewed tags, verification class, public provenance, privacy review, and valid supersession
   lineage when applicable.

One causal rule belongs in one record. A test pass, diff, task request, model proposal, or report is
not automatically a lesson.

## Lifecycle and currentness

- `candidate`: complete, current public references pending engineering or privacy approval; not
  normal advice.
- `verified`: current, public-safe advice eligible for briefing.
- `superseded`: an earlier same-key revision replaced by its immediate successor.
- `invalidated`: a current revision that withdraws the rule.
- `stale`: a verified record whose owner, regression, or evidence identity no longer matches.

Append corrections; never edit historical rows or regenerate the genesis seal to excuse a changed
initial record. Current source and tests remain authoritative when they disagree with a lesson.

## Public commands

Run from the repository root:

```text
python tools/proofbook/engineering_learning.py validate
python tools/proofbook/engineering_learning.py status
python tools/proofbook/engineering_learning.py brief --task "bounded task" --path "src/aetherstate/owner.py"
python tools/proofbook/engineering_learning.py add --input "candidate.json"
```

Treat a valid no-match as complete. Before admission, inspect the candidate directly; after
admission, validate the complete ledger and report fresh status.

## Privacy abstention

Permit only bounded engineering conclusions, closed classifications, repository-relative public
paths, hashes, synthetic tests, public contracts, and immutable public audits. Reject Player
content, secrets, provider material, raw model material, arbitrary logs, machine-specific paths,
and evidence that a clean public checkout cannot verify.

When proof is incomplete, route PRIVATE or ABSTAIN. A missed public lesson is safer than a durable
false claim.
