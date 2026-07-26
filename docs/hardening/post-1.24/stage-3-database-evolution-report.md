# Stage 3 Versioned Database Evolution

Stage 3 transfers the cumulative hardening terminal owner to
`stage-3-database-evolution`. It adds no Player-facing feature claim and does
not change the frozen public `1.24.0` baseline.

## Preserved contract

- Public baseline: version `1.24.0`, commit
  `82b58277d7a1fb167434be0290d3dfd2bb3588e2`.
- Stage 3 merge target:
  `55205f73c58da18a681212545c34e90ac63532a7`.
- Shared CI jobs: `quality`, `python-tests`, `javascript`, `package-build`,
  and `package-smoke`.
- The complete Stage 2 Semantic Cube matrix and its two accepted HOLD rows
  remain unchanged and are terminally validated.
- Every existing Behavior and Player Surface Preservation Manifest surface
  and proof remains present. One historical-upgrade proof is added only to
  the twelve database-affected surfaces.

## Terminal evidence

The Stage 3 builder emits
`aetherstate-hardening-stage-report/1` for exact stage
`stage-3-versioned-database-evolution`. The report contains the seventeen
ordered required rows and binds them to one exact candidate commit, tree,
proof-input fingerprint, unchanged umbrella and Semantic Cube contracts,
manifest hash, fixed test budget, and bounded evidence origin.

Only a dependency-closed exact-candidate report with every row `PASS`
authorizes Stage 4. A local Windows evidence wave remains
`HOLD cross_platform_ci_pending` while required Linux and cross-platform CI
rows are unavailable. `TEST_BUDGET_HOLD` and `INVALID` remain terminal stop
states.

## Proofbook decision

**ABSTAIN on new lesson creation.** The completed Stage 3 path did not
establish a genuinely new causal rule beyond the existing dependency-closed
terminal-gate doctrine.

Stage 3 does change source and regression anchors used by twenty-two existing
Public Proofbook lessons. Those lessons are maintained through append-only
`Corrects` revisions after semantic review, then sealed by the next public
publication attestation. This is not a blind hash refresh and does not change
the lessons' causal rules. No log, database, session, provider, credential, or
personal-data mining was used.
