# Proofbook

Proofbook is the short name for **AetherState Engineering Learning**: a public, developer-only
ledger of verified lessons from real repairs. It helps contributors recover a relevant causal rule
before changing code and keeps that rule tied to current public source and regressions.

It does not run inside AetherState, store Player data, authorize gameplay, or train a model.

## Use it

From the repository root:

```powershell
python tools/proofbook/engineering_learning.py validate
python tools/proofbook/engineering_learning.py status
python tools/proofbook/engineering_learning.py brief --task "bounded engineering task" --path "src/aetherstate/example.py"
```

`brief` is read-only. A no-match is a successful briefing attempt: continue from current source and
tests instead of forcing an unrelated lesson onto the task.

To submit an explicitly reviewed candidate:

```powershell
python tools/proofbook/engineering_learning.py add --input "path/to/public-candidate.json"
```

The command accepts strict JSON only. It does not convert logs, test output, chats, or free-form
notes into lessons.

## Files

- `CONTRACT.md` defines authority, record bytes, lifecycle, privacy, retrieval, and scale.
- `PUBLICATION_POLICY.md` defines the required public/private/abstain decision.
- `TAGS.json` is the reviewed tag and alias registry.
- `GENESIS.json` seals the ordered initial public-history prefix.
- `LEDGER.jsonl` is the append-only public lesson history.
- `tools/proofbook/` contains the standalone validator and briefing command.
- `skills/aetherstate-code-learning/` contains the optional public Codex workflow.

## Contribute a lesson

1. Read the contract and publication policy.
2. Prove one failure, one cause, and one falsifying regression.
3. Decide the destination before writing a record.
4. Use only public repository-relative owners, regressions, and evidence.
5. State what the rule supports and what it does not support.
6. Validate the candidate and its regression in a clean checkout.
7. Submit it for public-maintainer engineering and privacy review.

Future lessons append after the sealed genesis prefix. Do not regenerate `GENESIS.json` to excuse
deletion, reordering, or rewriting of an initial public record.

Public lessons contain complete public proof. Lessons requiring private or unavailable context stay
outside this repository. When either the proof or destination is uncertain, abstain.

Do not update hashes just to clear staleness. A changed owner or regression is a prompt to review
whether the rule still means the same thing.
