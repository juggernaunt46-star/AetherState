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
4. **DB migrations are additive-only** (new tables, or new columns via `_MIGRATIONS`).
5. **Fail open.** Wrap new stages in try/except that falls back to the untouched request/response.
6. **Write a test** using the replay harness (`tests/`, mock upstream). Then run `pytest` + `ruff`.

Environment to run tests (sandbox):
```bash
cd AetherState-gitrelease
pip install -e ".[dev]" --break-system-packages
pytest -q            # test suite
ruff check src tests # lint (line-length 110)
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

Two identical trees ship: `AetherState-gitrelease` (public, the canonical repo — this docs set
lives here) and `AetherState-personal`. Keep them in sync when editing. CI is
`.github/workflows/ci.yml`. Version lives in three places — bump together: `src/aetherstate/__init__.py`
`__version__`, `pyproject.toml` `version`, `st-extension/manifest.json` `version`, and add a
`CHANGELOG.md` entry.

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

**Verification step for any change:** run the relevant test(s) + the full suite + `ruff`; for a
state/authority/extraction change, add a fixture that reproduces the scenario and assert the
journaled ops + resulting `state_summary`.

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
- [ ] `config.example.toml` + docs updated for new config keys.
- [ ] Version bumped in all three places if releasing.
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
- **Genesis** — seeding state from the character card (Stage A rules inline, Stage B LLM cold).
- **Beat** — an authored director guidance unit with state preconditions.
- **Recall / slice / note** — precomputed memory lines / precomputed briefing / next-turn director note.
- **Assist / group** — the local helper model / a feature's `off|rules|main|assist` mode.
- **Raw mode** — `consent.mode == "unrestricted"`: consent tracking is inert for generation, user
  controls still work.
- **Fail-open** — on any internal error, the original request/response flows unmodified.
