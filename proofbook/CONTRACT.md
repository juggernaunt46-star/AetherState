# AetherState Engineering Learning Public Contract

## Purpose

**AetherState Engineering Learning**, also called **Proofbook**, is a developer-only ledger of
verified engineering lessons. It gives maintainers a short, evidence-linked briefing before
nontrivial work and preserves causal rules that can prevent a proven failure from recurring.

Proofbook is advisory. Current source and code-settled state outrank fresh tests and immutable
audits; those outrank current maintainer documentation; those outrank an active lesson. Proofbook
is not game state, Player memory, WorldLex authority, a settlement oracle, runtime configuration,
or model-training data. Nothing in this directory is imported by the AetherState package.

## Public edition

This directory is a new public history, not a copy or declassification of any private ledger.
Every published record is independently admitted from evidence that exists in this repository.
Public records start at revision 1, use public-contract provenance, and carry no prior record ID,
private review history, internal coordination path, or unavailable evidence.

A public lesson must validate in a clean checkout without a Git remote, private workspace, local
cache, or external evidence store. Owners, regressions, and evidence therefore use slash-separated
repository-relative paths and SHA-256 identities for current working-tree bytes. Git references are
not accepted.

## Destination before admission

Classify a proposed lesson before assembling its record:

- **Public**: the causal rule is useful to public contributors; every owner, regression, and
  evidence artifact is present in this repository; the prose is bounded and safe to publish; and a
  public maintainer has approved it.
- **Private**: any necessary evidence, context, or coordination history is private, local,
  personal, provider-derived, unavailable to a clean checkout, or inappropriate for publication.
  Keep the lesson wholly outside the public ledger.
- **Abstain**: the cause, falsifier, destination, or publication safety is ambiguous. Do not create
  a public record until the uncertainty is resolved.

Never split one proof across public and private storage merely to make its record appear portable.
A missed lesson is safer than durable advice whose proof cannot be checked.

## Record lifecycle

Records use schema `aetherstate/engineering-lesson/1` and are immutable JSONL entries.

- `candidate` is an explicit proposal and is never returned as normal advice.
- `verified` has one proven cause, an imperative repair rule, exact owners and regressions, public
  evidence, supported scope, explicit non-support, reviewed tags, and approved privacy review.
- `invalidated` is a current revision that withdraws a lesson.
- `superseded` and `stale` are derived views. A superseded record has a later same-key revision. A
  stale current record no longer matches its referenced bytes, symbols, nodes, or evidence.

Corrections append one same-key revision and point `supersedes` at the immediate predecessor.
Supersession rationale begins with `Corrects`, `Narrows`, `Expands`, or `Invalidates`. Records are
never rewritten, reordered, truncated, or silently repaired.

## Required fields

Each record contains:

- a namespaced lesson key, positive revision, lifecycle, domain, and diagnosis;
- bounded symptom, proven cause, imperative repair rule, and rationale;
- supported scope and explicit non-support;
- nonempty owner, regression, and evidence arrays with current SHA-256 identities;
- reviewed tag IDs, verification class and mode, public-contract provenance, and privacy review;
- an immediate predecessor ID or `null`; and
- a canonical record ID.

Semantic Cube records also name the first failed boundary and a compatible failure class.
Recognizing, binding, aligning, admitting, settling, and delivering meaning are separate proof
boundaries. A green earlier boundary does not prove a later one, and multi-target recognition is
not an area-of-effect mechanic.

## Canonical bytes

All strings are strict UTF-8 and Unicode NFKC. Floats are forbidden. The record ID is SHA-256 over
the exact record without `record_id`, serialized with recursively sorted keys, compact separators,
unescaped Unicode, and no trailing newline. The stored record is the same canonical object with
`record_id`, followed by exactly one LF.

The ledger is append-only. A malformed or non-LF-terminated tail blocks later writes; the tool does
not truncate or repair it automatically.

`GENESIS.json` seals the exact bytes and ordered record count of the initial public prefix. Every
validation and append first verifies that prefix. Later records may append after it, but deleting,
reordering, rewriting, or regenerating the seal for an initial record is corruption rather than a
revision.

## Public paths and privacy

References must be normal repository-relative files. Absolute, drive-relative, UNC, empty, dot,
dot-dot, traversal, symlink, junction, and reparse escapes are rejected. Private metadata
directories, private worktrees, machine-local material, personal checkout names, internal knowledge
stores, and private tooling layouts are not public evidence.

Records may contain bounded redacted engineering prose, closed fields, hashes, public source and
test paths, and public contract references. They reject credentials, authorization material,
endpoints, provider bodies, raw prompts or replies, model reasoning, personal campaign or chat
prose, session rows, absolute personal paths, arbitrary logs, and unavailable review artifacts.
Validation checks known unsafe shapes; publication approval remains a human or agent review
decision.

## Retrieval

Briefing is deterministic. It ranks exact regression or owner-symbol matches, component-safe owner
paths, reviewed tags and aliases, exact classification, then bounded token overlap. It returns no
more than five active verified lessons and explains each match. A valid no-match is a completed
briefing attempt; contributors must not force an unrelated lesson onto a task.

Every returned lesson is a hypothesis to check against current source and tests. Candidate,
invalidated, superseded, and stale records are excluded from normal advice.

## Scale and evolution

The public edition scans `LEDGER.jsonl` directly. It has no model calls, generated index, automatic
failure mining, or runtime write path. Do not pin record totals in durable documentation; run the
status command against current bytes.

A future generated index is acceptable only if its final briefings are byte-identical to direct
scan. A model may eventually propose candidates, clusters, or synthetic cases, but it may never
approve its own evidence or turn Proofbook into gameplay authority.
