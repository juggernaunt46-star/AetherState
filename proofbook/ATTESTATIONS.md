# Proofbook Publication Attestations

`ATTESTATIONS.jsonl` is the append-only publication chain for the complete public Proofbook
ledger. `GENESIS.json` remains the immutable seal for the original 37-record foundation; the
attestation chain additionally seals every reviewed publication head, including later records.

## Publication gate

Run both validators from the repository root:

```powershell
python tools/proofbook/engineering_learning.py validate
python tools/proofbook/publication_attestations.py validate
```

The first command validates lesson structure, history, references, and currentness. The second
requires the latest publication attestation to cover the exact complete `LEDGER.jsonl` bytes.
Publication is incomplete unless both commands pass.

To seal a newly reviewed ledger suffix, first create one canonical review artifact beneath
`proofbook/reviews/`, then run:

```powershell
python tools/proofbook/publication_attestations.py attest --input "proofbook/reviews/review.json"
```

The review artifact must identify the exact ledger record count and SHA-256, bind approved
engineering and privacy reviewers, and name an inspectable public HTTPS artifact. The command does
not fetch that URL or decide whether an approval is truthful.

## Canonical chain

Each attestation uses schema `aetherstate/proofbook-publication-attestation/1` and contains:

- a contiguous sequence and the immediately preceding attestation ID;
- the exact covered ledger record count and SHA-256 over those LF-terminated bytes;
- the exact ordered record IDs added since the preceding attestation;
- a repository-relative review-artifact path and exact artifact SHA-256; and
- an attestation ID computed from canonical JSON without `attestation_id`.

Attestations use recursively sorted compact JSON, strict UTF-8, no floats, and exactly one trailing
LF. Against the retained chain, validation rejects ledger deletion, reordering, rewriting, an
unattested append, a broken chain link, approval drift, and missing or changed review artifacts.

## Boundary

This chain is a portable integrity receipt, not a digital signature or timestamp authority. It does
not prove reviewer identity, fetch external artifacts, make lessons correct, or survive coordinated
replacement of the repository, validator, ledger, review artifacts, and all known chain heads.
Git review and published commit history remain the external custody layer.
