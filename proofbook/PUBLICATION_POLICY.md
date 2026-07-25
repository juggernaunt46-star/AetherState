# Proofbook Publication Policy

## The decision

Every proposed engineering lesson receives exactly one destination before admission: **public**,
**private**, or **abstain**. Destination is based on the proof the lesson needs, not on whether its
wording can be made generic.

### Public

Publish a lesson only when all of these are true:

1. The rule helps contributors work on the public AetherState repository.
2. Its cause and regression are proven by current public source, tests, or contracts.
3. Every owner, regression, and evidence path exists in a clean checkout.
4. Every reference uses current repository-relative bytes; no Git remote or private workspace is
   required.
5. The prose contains no credentials, provider material, raw model content, personal session
   content, internal coordination history, unavailable artifact, or absolute personal path.
6. Supported scope and non-support are explicit.
7. A public maintainer approves both the engineering conclusion and publication safety.

Public admission creates a new public record. It does not preserve a private revision number,
record ID, reviewer label, evidence receipt, or chronology.

### Private

Keep the whole lesson private when any necessary part of its proof depends on:

- a personal or private checkout;
- a private worktree, plan, handoff, review, candidate, audit receipt, or coordination record;
- a credential, provider response, raw prompt, raw reply, model reasoning, session row, or personal
  campaign/chat content;
- a machine-local model, corpus, database, cache, machine path, or environment detail that cannot be
  published safely; or
- an owner or regression that is not present in the public repository.

Do not publish a weakened paraphrase whose real falsifier remains private. A private lesson stays in
the private learning system and is not referenced by the public ledger.

### Abstain

Abstain when the cause is not isolated, the regression is not exact, the destination is uncertain,
the evidence cannot be reviewed, or publication safety is ambiguous. Abstention means no public
record is created. It is a correct outcome, not a validation failure.

## Admission workflow

1. Inspect current owning source and tests.
2. Run one bounded public briefing attempt.
3. Reproduce the failure and prove one causal rule.
4. Classify the lesson as public, private, or abstain.
5. For a public lesson, create one explicit strict JSON candidate using only public references.
6. Validate the candidate and run its regression in a clean checkout.
7. Obtain public-maintainer engineering and privacy approval.
8. Append through the Proofbook command and submit the record through normal code review.

Never mine test output, diffs, task text, logs, chat, model responses, or private ledgers directly
into the public ledger. Automation may validate structure and hashes; it does not decide provenance
or publication safety.

## Clean-checkout acceptance

A public record must remain valid when the public Proofbook directory, its command, and all
referenced repository files are copied into a clean directory with no Git metadata. Validation must
not reach outside that directory or depend on a specially named remote.

The ordered initial public history must also match `GENESIS.json`. New reviewed records append after
that sealed prefix; maintainers never rewrite the seal to hide deletion, reordering, or changed
initial bytes.

If a referenced file changes, the lesson becomes stale until a maintainer reviews the semantic
change. Never refresh a hash merely to make status green.

## Review boundaries

Proofbook advice never authorizes AetherState mechanics, facts, events, narration, state mutation,
or commits. It is not PlayerLex, Player Lessons, game memory, or training data. The owning source
and regression remain the authority.
