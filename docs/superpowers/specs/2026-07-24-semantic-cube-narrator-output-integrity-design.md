# Current-Main Semantic Cube Narrator-Output Integrity Design

**Date:** 2026-07-24

**Status:** Approved design

**Baseline at approval:** `origin/main` `82b58277d7a1fb167434be0290d3dfd2bb3588e2`, AetherState 1.24.0

## Objective

Improve, debug, and repair the narrator-output behavior that exists on current AetherState `main`.
The work is a system-integrity pass, not a queue of sentence-level narration patches.

A concrete failure is evidence that enters the investigation. It does not define the repair scope.
The Semantic Cube identifies the first boundary that lost, corrupted, duplicated, misdelivered, or
hid meaning. The repair targets the violated shared invariant and every current-main path that
depends on it.

## Scope

This design covers the current narrator-related path across:

- Recognition
- Binding and world alignment
- Admission
- Complete settlement
- Narrator transfer and model compliance
- Journal, HUD, and lifecycle visibility

It covers all three state-selected narration modes:

- Exploration
- Combat Opening
- Combat Exchange

It also covers the lifecycle paths that can duplicate or corrupt otherwise correct work:

- fresh response
- duplicate transport
- retry or lost-reply recovery
- swipe or regeneration
- Continue
- reopen
- branch and replay

Treat the shipped default Narration Compliance Auditor and the separate opt-in truth-gated path as
different behaviors. The default auditor is post-stream and advisory; it must not be credited with
preventing prose that already reached the Player.

## Explicit non-goals

- No LoRA or unimplemented narrator interpreter.
- No new Semantic Cube runtime component or new authority source.
- No broad new gameplay feature.
- No phrase-exception campaign.
- No blind long-turn playtest quota.
- No inspection of personal chats or stored personal content.
- No publication or release work without separate authorization.

If the audit proves that a desired behavior is absent from current `main`, record an exact `HOLD`.
Do not silently turn the integrity pass into feature development.

## Core repair rule

Use this chain:

`observed failure -> first failed Cube boundary -> violated invariant -> all affected current-main consumers -> shared repair -> exact and sibling proof`

“Repair the owner” means repair the causal component or contract, even when it is shared by many
paths. It does not mean adding a local conditional for the sentence that exposed the defect.

A phrase-level recognition change is appropriate only when evidence proves a genuine Coverage
failure and negative controls show that the new surface does not overmatch. A prompt change is
appropriate only when the final packet is correct and the failure is genuinely model compliance.

## Audit targets

### 1. Recognition

Determine whether current-main exact tags and supported deterministic narrator-output processing
preserve the intended actors, actions, targets, objects, polarity, actuality, time, and scope.

### 2. Binding and world alignment

Determine whether recognized meaning binds to the exact existing actor, faction, item, scene,
capability, turn, and occurrence. Ambiguity must remain explicit; shared names or words cannot invent
identity.

### 3. Admission and complete settlement

Determine whether supported proposals pass the correct authority checks, settle atomically once, and
remain replay-stable. Narrator prose and extraction must not create unsupported truth, mechanics,
ownership, damage, statuses, scene changes, or durable memories through a side path.

### 4. Narrator transfer and compliance

Separate two questions:

1. Did AetherState construct and deliver the correct narrator packet?
2. Did the narrator visibly follow that packet?

Wrong packets are delivery defects. Correct packets followed by contradictory prose are model-
compliance defects. Do not patch prompts to repair missing or incorrect code-owned truth.

### 5. Visibility and lifecycle

Determine whether state, journal, HUD, retry, swipe, Continue, reopen, branch, and replay preserve the
same occurrence and result. Correct state with an incorrect HUD is a visibility defect, not a
semantic or settlement defect.

## Rolling integrity sweep

The work proceeds one boundary at a time, repairing structural defects as soon as they are proven:

1. Freeze and verify the exact current-main source under test.
2. Select one boundary invariant.
3. Probe its affected narration modes and lifecycle consumers with synthetic fixtures.
4. Record `PASS`, `HOLD`, `NOT_APPLICABLE`, or `INVALID`.
5. When a probe fails, locate the first failed boundary.
6. Search every current-main producer and consumer of the violated invariant.
7. Add the exact failing regression before changing behavior.
8. Repair the shared owner.
9. Run the exact reproduction, adversarial siblings, and affected lifecycle tests.
10. Recheck any previously passed boundary that depends on the repair.
11. Use focused live proof only when the model, stream, or visible interface is part of the claim.
12. Advance only after the boundary has an exact status.

Several defects sharing one boundary trigger a boundary-wide review. They do not become several
independent patches.

The initial matrix must cover every meaningful combination of the five audit targets, three
narration modes, and participating lifecycle paths. `NOT_APPLICABLE` requires a stated contract
reason; it cannot be used to shrink inconvenient coverage.

## Evidence for each investigated case

Use newly created synthetic data only. Retain enough evidence to reconcile:

- pre-turn state and Player input;
- selected narration mode;
- relevant recognition, binding, admission, and settlement receipts;
- final content-free narrator packet manifest;
- visible narrator response;
- resulting journal and authoritative state;
- visible HUD when applicable;
- turn, occurrence, attempt, branch, and replay identity where applicable.

Raw prompts and replies remain volatile unless a safely synthetic screenshot is needed. Durable
evidence should prefer typed facts, reason codes, identities, counts, and hashes.

Evidence from the wrong source, build, root, route, model mode, session, or contaminated browser
state is `INVALID`. It proves neither pass nor failure and must be replayed.

## Repair verification

Every repair requires:

- the original reproduction;
- one-variable positive controls;
- adversarial negative controls;
- every affected narration mode;
- every affected retry, swipe, Continue, reopen, branch, or replay path;
- focused tests for the owning component;
- the relevant broader regression cohort at the boundary milestone.

The full suite is not required after every small repair. Run it at the final integrity gate or when a
repair changes a foundation broad enough to justify it.

## Playtest role

Automated synthetic checks are the main workload. Disposable playtests have three jobs:

1. expose failures that only appear with the real narrator, stream, or interface;
2. replay a repaired model- or UI-facing defect end to end;
3. perform one final fresh mixed-mode discovery run after the automated matrix is stable.

Use the smallest focused scenario that distinguishes the competing causes. A foundational finding
stops the live run; preserve the reproduction, repair and verify the owner, then restart with fresh
synthetic state. Do not continue through contaminated evidence merely to accumulate turns.

## Status definitions

- **PASS:** The exact invariant is proven across its required automated and live surfaces.
- **HOLD:** A named missing capability, unresolved defect, or external requirement prevents proof;
  the first failed boundary and owner are known.
- **NOT_APPLICABLE:** The boundary genuinely does not participate in that mode or lifecycle.
- **INVALID:** A prerequisite was disproved, so dependent observations cannot be counted.

“Bad narration,” “parser issue,” and “probably prompt-related” are not terminal statuses.

## Deliverables

The execution plan should produce:

1. one living Cube integrity matrix for current-main narrator output;
2. bounded defect records naming the first failed boundary and shared invariant;
3. focused automated regressions for every repaired defect;
4. focused disposable playtest evidence where model or UI behavior matters;
5. one final simple `PASS/HOLD` report.

The matrix and private live evidence are coordination artifacts, not new runtime authority.

## Completion gate

The integrity pass is complete when:

- every selected audit path has an exact `PASS`, justified `HOLD`, or `NOT_APPLICABLE`;
- no discovered failure remains classified only by its visible symptom;
- every structural defect is repaired or assigned to a proven owner and boundary;
- all focused and affected broader regressions pass;
- every model- or UI-dependent repair passes focused disposable replay;
- one final fresh mixed-mode playtest reveals no new structural failure;
- all disposable services, tabs, and roots are cleanly retired.

The final report must separate automated proof, live proof, remaining HOLDs, and behavior that current
`main` simply does not implement.
