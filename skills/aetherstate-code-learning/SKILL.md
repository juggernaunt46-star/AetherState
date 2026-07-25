---
name: aetherstate-code-learning
description: Use when reviewing, briefing, validating, or admitting AetherState public developer Proofbook lessons and when deciding whether engineering evidence is safe for public admission.
---

# AetherState Code Learning

## Core rule

Use the public Proofbook as developer-only advice. It is never gameplay authority, never Player
data, and never model-training data. Source and tests outrank lessons.

Read [references/ledger-contract.md](references/ledger-contract.md) before adding, revising,
invalidating, or publishing a lesson. Also read the repository's `proofbook/CONTRACT.md` and
`proofbook/PUBLICATION_POLICY.md` for the machine contract and publication boundary. Read
`proofbook/ATTESTATIONS.md` before sealing a publication.

## Classify before evidence

Classify the lesson destination before collecting, hashing, or admitting evidence:

| Destination | Exact decision |
|---|---|
| **PUBLIC** | Use only when all evidence is present in the public repository and every owner, regression, evidence file, and bounded statement is safe to publish. |
| **PRIVATE** | Use when any evidence is private or unavailable in the public repository. Keep the lesson in an authorized private process. |
| **ABSTAIN** | Use when destination or evidence safety is ambiguous. Gather safer evidence or leave the lesson unrecorded. |

Never copy a private lesson into the public ledger. Redaction does not turn missing public proof
into public evidence.

## Brief before changing code

Run one bounded public briefing from the repository root:

```text
python tools/proofbook/engineering_learning.py brief --task "bounded task" --path "src/aetherstate/owner.py"
```

A valid no-match completes the briefing attempt. Verify every returned rule against current source
and tests before using it.

## Review after a proven repair

Admit a lesson only when it has:

- one causal rule with a proven symptom, cause, repair, and falsifier;
- exact repository-relative owners and regression nodes with current SHA-256 identities;
- immutable public evidence and reviewed provenance;
- bounded supported scale and explicit non-support;
- approved public privacy review; and
- exact Semantic Cube coordinates only when the lesson truly belongs to that domain.

Do not convert task prose, model output, logs, test output, or a green surface into a lesson
automatically. Recognition, detection, or one passing consumer does not prove complete support.

Use an explicit strict JSON input for admission:

```text
python tools/proofbook/engineering_learning.py add --input "candidate.json"
python tools/proofbook/engineering_learning.py validate
python tools/proofbook/publication_attestations.py validate
python tools/proofbook/engineering_learning.py status
```

Do not rewrite or reorder prior ledger rows. Corrections append a valid revision under the public
contract. Before publication, bind approved engineering and privacy metadata to one public review
artifact, attest the exact complete ledger, and require both validators to pass. An unattested tail
is review work in progress, not a publication.

## Authority and privacy

Proofbook may advise maintainers about runtime boundaries, but it cannot create state, grant a
capability, admit a mechanic, settle an outcome, alter WorldLex, or supply narration. Never store
credentials, endpoints, prompts, replies, reasoning, session rows, campaign prose, or other Player
content.

If a required owner, regression, or evidence file is absent from the public repository, classify
the lesson as PRIVATE or ABSTAIN; do not weaken admission to make it publishable.
