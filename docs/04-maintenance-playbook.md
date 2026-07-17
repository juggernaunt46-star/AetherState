# AetherState — Maintenance Playbook

Task-oriented recipes for the common repair / update / addition jobs, plus testing, invariants to
never break, and a glossary. Written so a skill can follow a recipe end-to-end.

---

## 0. Ground rules for any change

1. **Re-read the four invariants** (`00-MAINTAINER-MAP.md §2`). If a change risks one, it's wrong
   until proven otherwise.
2. **Hot path vs cold path.** New LLM/network/embedding work goes on the **cold path** (`jobs.py`,
   `pipeline.on_response`), never in `Pipeline.process`.
3. **State only changes via ops** through `state.apply_delta`. Never mutate the state dict or write
   the DB's state directly.
4. **DB migrations are additive-only.** Core Store columns use `_MIGRATIONS`; specialized exact
   local schemas such as PlayerLex and Player Lessons change only in their owning service and tests.
5. **Fail open.** Wrap new stages in try/except that falls back to the untouched request/response.
6. **Write a test** using the replay harness (`tests/`, mock upstream). Then run `pytest` + `ruff`.

Run tests from the repository root and prove the import origin. Multiple local checkouts may
share a version while containing different code, so a passing suite from another checkout is invalid
evidence.

```powershell
Set-Location <path-to-AetherState>
$env:PYTHONPATH = (Resolve-Path src).Path
python -c "import aetherstate; print(aetherstate.__file__)"
python -m pytest -q
python -m ruff check src tests tools nli-shim
```

---

## 1. Add or change a tracked state field / op kind

The single most common change. To add an op kind `foo`:

1. **`state.py`**
   - `_SPEC["foo"] = {required, fields}` — shape.
   - `_FAMILY["foo"] = "scene|facts|organic|consent|safety"` — authority family.
   - `_ORDER["foo"] = N` only if it must apply before others in a delta (freeze is −1).
   - `OP_FIELD_ENUMS["foo"] = {field: [enum...]}` if it has enum fields (this is the single source
     of truth — both wire schemas derive from it).
   - `validate_op` — add the enum/shape checks (must agree with `OP_FIELD_ENUMS`; a test welds them).
   - `_apply_op` — add the `elif kind == "foo":` mutation branch.
2. **`extraction.py`** (only if the *model* should emit it): add to `EXTRACTION_OPS`, `_OP_ALLOWED`,
   `_OP_FIELDS`.
3. **`prompts.py`**: add a line to `OP_CARD` and ideally a few-shot — schemas enforce shape, not
   vocabulary; an op absent from the prompt won't be produced.
4. **`compose.py`**: render it in `render_header` if it belongs in the briefing.
5. **`02-data-model.md`**: document the op + any new enum.
6. **Test**: `tests/test_p2_state.py` / `test_p2_authority.py` patterns — validate, authority per
   source, apply, replay.

To add a **field to an existing op**: update `_SPEC`/`_OP_ALLOWED`/`_OP_FIELDS`/`OP_CARD` + the
`_apply_op` branch. To add an **enum value**: add it to the vocabulary set in `state.py` — schemas
and validation derive automatically.

## 2. Change who can mutate what (authority)

Edit `state.authority_violation`. It branches by `source` then `family`/`kind`. Keep the safety
direction free (safewords/consent-down always land) and gate escalation. Add a test in
`test_p2_authority.py` for each source × the new rule.

## 3. Add / edit a director beat

- **No code needed** if it uses existing precondition paths + ops: add a beat object to a
  `beats/*.json` file (full schema in `02-data-model.md §6`). Tune `priority`/`cooldown_turns`; a
  beat may also carry `effects` (ops applied `source=rule` on fire) and `phase_hint`, and use the
  `pair` binding (`{a}`/`{b}`/`{initiator}`/`{partner}`).
- New library file → register it in `DirectorConfig.beat_libraries` (config + `config.example.toml`).
- New precondition **path** → extend `director.resolve_path`. New **op/combinator** → extend
  `director._leaf`/`eval_dsl`. New **binding** kind → extend `director.bindings`.
- Test with `test_p4_director.py`.

## 4. Add a consistency check (linter rule)

Add `_lN_*(state, text, ..., v)` in `linter.py`, append its `Violation`s, and call it from
`lint_turn`. Pick the next `LN` code and document it as a `linter.rules_off` option. The director
turns violations into next-turn corrective notes automatically. Test in `test_p4_linter.py`.

## 5. Add / fix extraction (backend support, parsing, rungs)

- **New backend dialect** (grammar mode): add to `extraction._NATIVE`; add a port hint to
  `_PORT_HINTS` if detectable.
- **Parsing robustness:** harden `repair_json` / `strip_fences_and_prose` / `scrub_op` /
  `enum_salvage`. Add the failing sample to `test_p3_extraction.py`.
- **New rung / probe logic:** `Ladder.rung_for` / `_probe`. Remember `force_rung` must always win.
- **Capability cache issues:** the `caps` table; `caps_fail` demotes, TTL re-probes. Clear a bad
  entry by deleting its row (see §9).
- The mock upstream (`tests/mock_upstream.py`) can emulate each rung — use it.

## 6. Add a config setting

Add a field with a default to the right `*Config` model in `config.py`. Defaults keep existing
`config.toml` valid. Mirror it in `config.example.toml` and `03-config-and-api.md`. If it's an
assist "group", add it to `AssistGroupsConfig`. Env override is automatic
(`AETHERSTATE_SECTION__KEY`). Test in `test_config.py`.

## 7. Add a control API route / Console feature

Add the route inside `control.make_control_router`. If it mutates config, persist via
`_persist_config`. If it mutates state, route through `state.apply_delta` / `translate_path`. Wire
the UI in `st-extension/index.js` (panel or slash command) and/or `static/console.html`. Keep
response shapes in sync with what the extension reads.

For PlayerLex, keep persistence in `playerlex.py` over the local caller-owned connection/lock rather
than adding session ops. Derive approval provenance and Lex-qualified Atlas fingerprints server-side,
keep tested text in memory only, and protect correction/reapproval plus hard removal in
`test_playerlex.py`. Live fan-in belongs in `playerlex_recognition.py` and must verify the exact source
span, re-derive valence from the sealed LexFabric, run once only for an actual new turn, and create no
independent capability candidate or authority.

For Player Lessons, keep persistence and lifecycle in `player_lessons.py`, never in PlayerLex rows,
session ops, or the state journal. Preserve the closed complete same-effect definition,
revision+fingerprint proof, in-memory-only draft test, and explicit save/revise/disable/remove
consent lifecycle. Narration may use any current exact PlayerLex anchor or none and is the only type
allowed into the capped prompt component; disclose before consent that selected title/`do`/`avoid`
may reach the configured provider, and never retain private lesson text for prompt prewarm. Intent
misunderstanding/correct-interpretation prose is a local record only: never parse or send it. The
exact current ActionLex/action or ReferentLex/target anchor and typed current frame alone may narrow a
safe ambiguity after recognition and before contextual binding; never change actor. Preserve
separate frozen narration-selection/delivery and intent-selection/application receipts. Reserved
pipeline replay may rehydrate narration but must not
rerank or reapply intent; service-level intent rehydration is content-free evidence inspection and
ancestor cloning only. Exact duplicates reuse cached context only while every selected lesson remains
current. Preserve delayed delivery only after upstream response headers and state plainly that this
proves neither narrator adherence nor response completion. Preserve semantic-truth-gate inertness,
content-free evidence, secure database/WAL plus owned-cache removal, and the honest limit that
provider/in-flight copies and external backups cannot be recalled. Route/API/Console changes must stay
synchronized and the focused gate is `tests/test_player_lessons*.py`.

## 8. Change session identity / turn classification

Touch `session_engine.py` (+ `canon.py` for what counts as the stable transcript, `lcp.py` for
matching, `stamps.py` for the stamp format). If you change the stamp format, change
`st-extension/index.js::sentinel()` in lock-step. Tune thresholds in `SessionConfig` first before
changing logic. Tests: `test_l3_sessions.py`, `test_session_flow.py`, `test_p3b_routing_discovery.py`.

## 9. Operational fixes (no code)

- **Force re-seed a chat:** `/aether-genesis` (or `POST /aether/session/{sid}/genesis?force=1`).
- **Turn a chat into a plain proxy:** `/aether-mode passthrough`.
- **Clear a bad capability probe:** `DELETE FROM caps WHERE base_url=? AND model=?;` then it
  re-probes.
- **Reset a session's state:** delete it (`DELETE /aether/session/{sid}`) and re-open the chat.
- **Extraction stuck off:** it auto-disabled after `fail_autodisable_after` failures; it re-enables
  after `fail_reenable_after_turns`, or restart the proxy (`resume_pending` re-queues).
- **Nothing updates:** check the panel chip says online and the frontend base URL is
  `http://127.0.0.1:9130/v1`; check INFO logs (they only show if `__main__` ran `basicConfig`).
- **AV/SSL install failure:** `python -m pip install --use-feature=truststore -e .`.

## 10. Release / repo hygiene

Build every public release in a clean staged tree using an explicit allowlist; never copy local data,
credentials, traces, private evidence, or an entire development workspace wholesale. CI is
`.github/workflows/ci.yml`. `pyproject.toml` is the package version source of truth and
`src/aetherstate/__init__.py` reads it in a source checkout. Keep that version aligned with
`st-extension/manifest.json`, the README, and the public `CHANGELOG.md` entry.

---

## 11. Test map

The suite is a **replay harness** driven by fixtures + a canned-SSE mock upstream, so the whole
proxy runs deterministically in-process.

| Test file | Covers |
|---|---|
| `conftest.py`, `mock_upstream.py` | fixtures + the in-process mock backend (emulates each rung) |
| `test_config.py` | config load/precedence/fallback |
| `test_store.py` | schema, journal, `state_at`, checkpoints, migrations |
| `test_stamps.py` | header/sentinel parse + strip |
| `test_path_mapping.py` | `upstream_url` version-segment mapping |
| `test_transparency.py` | byte-for-byte passthrough (invariant 2) |
| `test_p2_state.py` | reducer + op apply |
| `test_p2_authority.py` | authority matrix per source |
| `test_p2_compose.py` | briefing render + budget governance |
| `test_p2_control_flow.py` | control routes |
| `test_p2_tier0.py` | OOC strip, dice, safewords, clock |
| `test_p3_extraction.py`, `test_p3_flow.py` | ladder, parse/repair, end-to-end extraction |
| `test_p3b_routing_discovery.py` | session routing + entity discovery |
| `test_p4_anyof.py` | anyOf schema derivation |
| `test_p4_assist.py` | assist sidecar |
| `test_p4_director.py` | beat selection/DSL |
| `test_p4_linter.py` | L1–L9 checks |
| `test_p4_memory.py` | retrieval/reflection |
| `test_p5_genesis_gear.py` | genesis seeding + gear |
| `test_p6_cadence.py` | cadence/debounce/batching |
| `test_l3_sessions.py`, `test_l3_accuracy.py`, `test_session_flow.py` | L3 identity accuracy |
| `test_capability_glossary.py` | sealed translation, frozen-definition compiler, provenance, tamper rejection, and deterministic rebuild |
| `test_semantic_atlas.py` | complete 327-meaning catalog, Lex-qualified collisions, deterministic search/paging, exact lookup, seal revalidation, and cursor binding |
| `test_playerlex.py` | explicit all-Lex local approval/provenance, names/aliases/patterns, exact spans, stale refusal, v1 migration, exact storage objects, correction/removal, reopen, API, and Console controls |
| `test_playerlex_live_recognition.py` | exact-source live fan-in, typed ambiguity/collisions, prose-free receipt, retry isolation, base-path retention, and no direct mechanics/world authority |
| `test_player_lessons*.py` | both closed lesson lifecycles, exact schemas, narration/intent field and anchor rules, informed Console consent, local record-only intent prose, exact-anchor-only safe action/target interpretation with actor refusal, separate fresh/frozen receipt paths, replay/fork/current-revision duplicate isolation, truth-gate inertness, bounded narrator transfer without prompt prewarm, header-only delivery evidence, immutable application evidence, authority denial, and secure removal/provider-limit wording |
| `test_world_identity.py`, `test_worldlex_store.py` | stable world transport, whole-world batch rejection, append-only lineage, and atomic rollback |
| `test_worldlex.py`, `test_worldlex_assignment.py`, `test_worldlex_runtime.py` | domain-neutral contracts, exact assignment, API/journal/fork/reopen, and noncombat scale seam |
| `test_enemy_capability_pool.py`, `test_worldlex_enemy_runtime.py` | all 270 pool round trips, forgery/unsupported-equipment guards, activated spawn/intent/action/replay path |

**Verification step for any change:** first prove the active import/tool origin and run the relevant
test(s). Run the full suite and Ruff for broad/shared-core changes, integration checkpoints, or
release preparation. For a state/authority/extraction change, add a fixture that reproduces the
scenario and assert the journaled ops + resulting `state_summary`.

### Additional semantic evaluations

Some development checkouts carry larger experimental semantic evaluators and live-observation
harnesses. They are not part of the public release gate. Public changes must still pass the shipped
deterministic tests for the semantic modules and corpus artifacts they affect.

---

## 12. Invariants & gotchas checklist (paste into any review)

- [ ] Hot path added no LLM/network/embedding/unbounded work.
- [ ] New stage is wrapped in a fail-open guard that returns the untouched request/response.
- [ ] State changed only via `apply_delta` (validated, authority-checked, journaled).
- [ ] DB change is additive (new table or `_MIGRATIONS` column).
- [ ] Sentinel/stamp format unchanged, or changed in BOTH `stamps.py` and `index.js`.
- [ ] Router order preserved (relay catch-all stays mounted last).
- [ ] Op reducer stays pure; config-dependent values baked via `_enrich`, not read at replay.
- [ ] New op kind is reflected in `_SPEC`/`_FAMILY`/`OP_FIELD_ENUMS`/`validate_op`/`_apply_op`
      (+ extraction + OP CARD if model-emitted).
- [ ] Player Lessons remain two separate effects: narration is bounded prompt input; intent is a
      typed ActionLex/action or ReferentLex/target correction after recognition and before contextual
      binding, with actor unsupported. Fresh selection freezes separate content-free receipts,
      reserved replay rehydrates narration but never reapplies intent, exact duplicates reuse cached
      context only while lesson revisions remain current, and Player Lessons stay inert under the
      semantic truth gate. Selected narration text has informed provider disclosure and no prompt
      prewarm; intent prose remains local record only and the exact anchor alone can narrow a safe
      ambiguity; delivery headers claim no adherence/completion; secure removal states its
      provider/in-flight/backup limits; neither effect can grant mechanics, truth, or Player
      authorship.
- [ ] `config.example.toml` + docs updated for new config keys.
- [ ] Version bumped in all three places if releasing.
- [ ] Working directory, `aetherstate.__file__`, and tool paths resolve inside the active checkout.
- [ ] Tests + `ruff` pass; a new test reproduces the change.

---

## 13. Glossary

- **Hot path / cold path** — synchronous per-request enrichment vs post-stream async cognition.
- **Stamp / sentinel** — the L1 header / L2 `<<AETHER:...>>` message carrying session identity.
- **L1/L2/L3** — session-resolution layers: header / sentinel / heuristic (LCP).
- **Turn class** — new_turn / swipe / edit_fork / continue / new_session / quiet / impersonate.
- **Op** — an atomic state change `{op, ...}`; the only way state mutates.
- **Family / source / authority** — op grouping / who proposed it / whether it may apply.
- **Quarantine** — a rejected op dropped with a logged reason (never applied).
- **Journal / checkpoint / `state_at`** — the append-only op log / periodic snapshots / replay to now.
- **Ladder / rung** — the extraction fallback chain (native grammar → strict JSON → JSON mode → freeform).
- **Capability probe / `caps`** — one-time backend feature detection, cached per base_url+model.
- **Recognized / authorized / executable** — understood glossary meaning / an exact frozen revision
  assigned to an actor or world / a versioned adapter and receipt can settle it. None implies the next.
- **Capability definition** — an immutable, fingerprinted authored revision. Preview/freeze alone is
  not persistence, assignment, or runtime admission.
- **Genesis** — seeding state from the character card (Stage A rules inline, Stage B LLM cold).
- **Beat** — an authored director guidance unit with state preconditions.
- **Recall / slice / note** — precomputed memory lines / precomputed briefing / next-turn director note.
- **Assist / group** — the local helper model / a feature's `off|rules|main|assist` mode.
- **Raw mode** — `consent.mode == "unrestricted"`: consent tracking is inert for generation, user
  controls still work.
- **Fail-open** — on any internal error, the original request/response flows unmodified.
