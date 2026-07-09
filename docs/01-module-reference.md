# AetherState — Module Reference

Every module under `src/aetherstate/`. For each: **responsibility**, **public API** (the names
other modules / tests call), **key internals**, and **edit points** (what a repair/addition
touches, and what invariant guards it). Line numbers are approximate anchors, not contracts.

---

## `__init__.py` — package marker

Holds `__version__` (the single string `status.py` reports and the ST panel chip shows). Bump it in
lock-step with `pyproject.toml` and `st-extension/manifest.json` on release (see
`maintenance-playbook.md §10`).

---

## `__main__.py` — CLI entry

**Responsibility.** Boot the process. Runs *before any httpx import* so it can neutralize the
antivirus failure modes.

**Key internals.**
- `os.environ.pop("SSLKEYLOGFILE", None)` — Avast/AVG point this at a device Python can't write;
  leaving it set makes *every* `httpx.AsyncClient` construction raise `PermissionError`, which
  silently fails-open all extraction/genesis/assist to empty. **Do not remove.**
- `truststore.inject_into_ssl()` in a try/except — uses the OS trust store so corporate/AV MITM
  certs work. Optional; failure is swallowed.
- `logging.basicConfig(level=INFO, …)` — without this the `aetherstate.*` INFO logs are dropped
  (uvicorn only configures its own loggers). This is the visibility switch for genesis/extraction.
- Arg precedence: CLI `--host/--port` > env > file > defaults.

**Edit points.** Add startup-time environment fixes here. Anything that must happen before the
app is built.

---

## `app.py` — application factory

**Responsibility.** Assemble the dependency graph and mount routers in the right order.

**Public API.** `create_app(cfg, client_factory=None, store=None) -> FastAPI`.
- `client_factory` is injectable so `tests/mock_upstream.py` can serve canned SSE in-process.
- `store` is injectable so tests use an in-memory DB.

**Key internals.**
- Builds `Store → SessionEngine → JobRunner(Ladder) → Pipeline`. Stashes them on `app.state`.
- **Router order matters:** status router, then control router (`/aether/*`), then the relay
  router **last** because the relay is a catch-all `/{path:path}`. If you mount the relay first,
  it eats `/aether/*`.
- `lifespan`: on startup `jobs.resume_pending()` (re-queue extraction left `pending` by a
  restart); on shutdown `jobs.stop()` and close the shared httpx client.
- `default_factory` builds one shared `httpx.AsyncClient` with `read=None` when
  `idle_timeout_s == 0` (no proxy-imposed stream timeout).

**Edit points.** New routers mount here (before the relay). New app-scoped singletons attach to
`app.state`.

---

## `config.py` — configuration

**Responsibility.** Load and validate config; never raise. See `03-config-and-api.md` for the
full key list.

**Public API.** `load_config(path) -> Config`. The `Config` pydantic model and its section models
(`ServerConfig`, `UpstreamConfig`, `InjectionConfig`, `ExtractionConfig`, `AssistConfig`, …).

**Key internals.**
- Precedence: **CLI > `AETHERSTATE_SECTION__KEY` env > `config.toml` > defaults**.
- `load_config` tries `config.toml`, then `config.toml.bak` (last-known-good), then pure defaults.
  On every successful load of the real file it copies it to `.bak`. Invalid config never blocks
  startup (`.source` records `file | last_known_good | defaults`).
- `_env_overrides()` maps `AETHERSTATE_EXTRACTION__CADENCE_TURNS=2` → `{"extraction":{"cadence_turns":"2"}}`
  (pydantic coerces the string).
- `_sync_extraction_group` model-validator: `[assist.groups].extraction` is canonical when set,
  `[extraction].mode` is the documented shortcut. After validation `extraction.mode` always holds
  the effective value.

**Edit points.** Add a field to the relevant section model (defaults keep old configs valid).
Mirror it in `config.example.toml` and `03-config-and-api.md`. If it is an assist "group", add it
to `AssistGroupsConfig`.

---

## `proxy.py` — transparent byte-relay (hot path)

**Responsibility.** Be a correct OpenAI proxy first, an enrichment host second. This module
**never parses response bodies** and never blocks the stream.

**Public API.** `make_relay_router(get_client, cfg, engine=None, pipeline=None) -> APIRouter`
mounts one catch-all `@router.api_route("/{path:path}", methods=["GET","POST","OPTIONS"])`.
Also `upstream_url(base_url, path, query)`.

**Key internals / lifecycle.**
1. Read raw body bytes.
2. If a header or sentinel marker is present, call `stamps.parse_and_strip` inside a fail-open
   guard. If it throws but a `<<AETHER:` marker is still in the body, a last-resort regex scrub
   ensures the sentinel never reaches the model.
3. If `POST` to `.../chat/completions` and body ≤ `max_parse_mb`: call `pipeline.process(stamp, body)`
   inside a fail-open guard (falls back to original bytes). If no pipeline (harness), call
   `engine.observe` observe-only.
4. Build upstream headers: drop hop-by-hop + every `x-aetherstate*` header; force
   `accept-encoding: identity` (a teeing proxy must read what it relays); inject `Authorization`
   from `cfg.upstream.api_key` if the client didn't send one.
5. `upstream_url` maps the proxy's `/v1/...` surface onto `cfg.upstream.base_url` (which already
   includes the version segment — same rule as ST custom endpoints). Guards against the
   `/api/v1/v1/...` double-version bug.
6. Missing `base_url` → OpenAI-shaped 502 `not_configured`. Upstream unreachable → 502
   `upstream_unreachable`. Both are well-formed JSON so the frontend never crashes.
7. `stream_bytes()` async generator: `yield chunk` **then** append to a bounded (4 MB) tee buffer.
   On finish, if status < 400 and buffer within cap, call `pipeline.on_response` (cold path start).

**Edit points.** This file is deliberately thin — resist adding logic here. Header handling and
the error-shape fallbacks live here. The tee cap (`tee_cap`) bounds memory for huge responses.

---

## `pipeline.py` — the conductor (hot path + cold path)

**Responsibility.** Orchestrate per-request enrichment. The hot half (`process`) runs before the
request is forwarded; the cold half (`on_response`) runs after the stream ends. Every step is
fail-open (invariants 1–3); `proxy.py` also wraps both in its own guard.

**Public API.** `Pipeline(store, engine, cfg, jobs=None, rng=None)`.
- `process(stamp, body) -> (bytes_to_forward, PostContext | None)` — the **hot path**. Returns the
  (possibly enriched) request bytes plus the tee context the cold path needs.
- `on_response(ctx, raw, content_type) -> None` — the **cold path**, called by the proxy tee after
  `[DONE]`. Never raises.
- Dataclass `PostContext(session_id, branch_id, turn_index, klass, speaker, card, opening)`.

**Key internals — hot path (`process`).** `engine.observe` → if `None` (quiet/non-chat) or the
session is in `passthrough` mode, return the body untouched. Else: on `new_session` (non-duplicate)
run genesis **Stage A** rules inline; read `current_state`; run `tier0.run`; apply **user** ops
first (a mid-turn freeze then gates the **rule** batch — both via `apply_delta`); capture the new
user text; `_swipe_rollback_guard`. Then one hot-path `read_recall` + `read_note` (+ `lint_l9_evidence`
when the user-guard is in `prevent_and_correct`), `compose.compose`, `write_slice`, and return
`compose.to_bytes(doc)` only if something changed.

**Key internals — cold path (`on_response`).** `_response_text` reconstructs assistant text from SSE
deltas or a plain JSON body → write it as this turn's `assistant_text` → `_discover` (entity
evidence) → `_recall_pass` (only when extraction is `off/rules`; otherwise the jobs path owns recall)
→ `_lint_pass` (full L1–L9 when `off/rules`, which also stages the director note; L9-only otherwise
so the guard can still correct the next turn) → `_genesis_pass` (schedule Stage-B LLM seed after
turn 1) → `jobs.notify` (arm Tier-1). `_capture_user_text` only fires on `new_turn/new_session/
impersonate`; `_swipe_rollback_guard` retracts state if a swiped turn was already extracted early.

**Edit points.** A new **hot**-path stage goes in `process` (must stay sub-ms and fail-open — no
LLM/network/embedding work). A new **cold**-path pass goes in `on_response` (add it before
`jobs.notify`). Anything unbounded belongs in `jobs.py`, not here.

---

## `stamps.py` — identity stamp parsing

**Responsibility.** Extract session identity from L1 (header) and L2 (sentinel), and **strip the
sentinel** so it never reaches the model.

**Public API.** `parse_and_strip(headers, body, header_name=...) -> (Stamp|None, new_body)`.
Constants `MARKER` (`b"<<AETHER:"`) and `SENTINEL_ANY` (regex). Dataclass `Stamp(session, turn,
gen_type, speaker, user)`.

**Key internals.** Parses `<<AETHER:v=1;session=…;turn=…;type=…;speaker=…;user=…>>` from a system
message and removes it (handles string and multipart content). If the header session and sentinel
session disagree, it logs and **the per-request sentinel wins** (a stale saved header can't steal
turns into an old session).

**Edit points.** If you change the stamp wire format, change it here **and** in
`st-extension/index.js::sentinel()` — they must agree.

---

## `canon.py` — message canonicalization

**Responsibility.** Turn a raw `messages[]` into a stable transcript core + chained hashes, so L3
identity survives World-Info / injection churn.

**Public API.** `canonicalize(messages) -> list[CanonMsg]`, `chain(msgs) -> list[str]`,
`content_hash(text)`, `normalize(text)`, `split_collapsed(raw)`. Dataclass `CanonMsg(role,
content_hash, ...)`.

**Key internals.** Drops system messages and known injection shapes; normalizes whitespace/markdown;
`chain()` produces vLLM-style chained prefix hashes so a shared prefix yields identical hash
prefixes. `split_collapsed` handles frontends that merge turns.

**Edit points.** If a frontend's injection pattern pollutes identity, exclude it in `canonicalize`.

---

## `lcp.py` — longest-common-prefix index

**Responsibility.** Given incoming canonical messages, find the stored branch with the longest
matching prefix — the basis for "same conversation, next turn vs swipe vs edit vs new".

**Public API.** `PrefixIndex` with `add_branch/append/truncate/drop_branch/touch`,
`longest_prefix(hashes) -> Match|None`, `align(incoming, k=3) -> Match|None`. Dataclasses
`BranchView`, `Match`.

**Key internals.** Two indexes: `by_chain` (chain-hash → branch set) for prefix matching and
`by_content` (content-hash → occurrences) for alignment fallback. `align` requires `k` consecutive
content matches to accept an alignment (guards against coincidental single-message collisions).

**Edit points.** Matching thresholds interact with `SessionConfig.min_anchor_msgs / adopt_min_lcp /
align_k`. Tune there first.

---

## `session_engine.py` — session resolution + turn classification

**Responsibility.** Answer "which session/branch is this, and what kind of turn?" via the L1→L2→L3
ladder, then classify.

**Public API.** `SessionEngine(store, cfg)` with `observe(stamp, body) -> Resolution|None`,
`resolve_stamped(...)`, `resolve_heuristic(messages)`. Enum `TurnClass` = `{new_turn, swipe,
edit_fork, continue, new_session, quiet, impersonate}`. Dataclass `Resolution(session_id,
branch_id, turn_index, klass, stamp, duplicate, path)`.

**Key internals.**
- **L1/L2 (stamped):** trust the stamp's session id; classify from `gen_type` + the unseen tail.
  The stamp's `turn` is a hint only — the server head is authoritative. A new turn is `head+1`, and
  a client `turn` is honoured **only when it advances past the head** (the extension resets its
  counter to 0 on chat reload / CHAT_CHANGED, so a stamped turn can otherwise regress below the head
  and file a roll — and its `[DIRECTIVE]` — on an early turn where it silently vanishes).
- **L3 (heuristic):** canonicalize → chain hashes → `PrefixIndex.longest_prefix` → 4-way classify:
  superset ending on a new user msg = `new_turn`; same terminal user prefix = `swipe`; divergence
  at a non-terminal index = `edit_fork` (forks the branch at the divergence); no meaningful match =
  `new_session`.
- **Dedup:** a `dedup_window_s` cache collapses duplicate identical requests (retries) →
  `duplicate=True` so the pipeline skips re-applying deltas (08 S7).
- Forks via `store.fork_branch`; appends tails via `store.append_msgs` + `PrefixIndex.append`.

**Edit points.** New turn kinds or classification rules live here. Keep it a pure function of
(stamp, messages, stored branches) so the replay harness stays deterministic.

---

## `tier0.py` — deterministic per-turn rules (hot path)

**Responsibility.** The free, LLM-free per-turn pass: strip OOC/commands, roll dice, detect
safewords, advance the clock, detect presence. Runs on the hot path (sub-ms).

**Public API.** `run(doc, klass, duplicate, state, cfg, rng) -> Tier0Result`. `Tier0Result` carries
`doc` (possibly OOC-stripped), `user_ops`, `rule_ops`, `notices`.

**Key internals.**
- `_strip_ooc` removes `((...))` spans from the newest **user** message and returns a rewritten
  doc (so the model never sees `((aether.set ...))`).
- `_commands` parses `((aether.set path value))`, `((aether.freeze))`, `((roll 2d6+1))` etc. Dice
  are rolled here (deterministic via injected `rng`) and the *result* is journaled as a `roll` op.
- `_safeword_hit` scans per `consent.safeword_scan` (`user_only|both`) against `consent.safewords`.
- Emits a `clock_tick` op (`director.minutes_per_turn`) and presence/location ops from keyword scan.
- Separates **user** ops (authored, high authority) from **rule** ops (engine-inferred). The
  pipeline applies user ops first so a mid-turn freeze gates the rule batch.

**Edit points.** New in-chat commands, new dice syntax, new deterministic detectors. If a detector
needs to change state, emit a validated op (see `state.py` op vocabulary) rather than mutating.

---

## `compose.py` — state briefing + budget governance (hot path)

**Responsibility.** Render the compact state header (+ director note + recall), enforce the token
budget, and splice it into `messages[]`.

**Public API.** `compose(doc, state, cfg, stamp, klass, recall=None, note="", guard_evidence=None)
-> (new_doc|None, kept_components)`. Also `render_header(state, cfg)`, `render_guard(...)`,
`govern(components, cfg)`, `splice(doc, text, cfg)`, `estimate_tokens(text)`, `to_bytes(doc)`.
Dataclass `Component(name, text, priority, tokens)`.

**Key internals.**
- `render_header` builds the human-readable state slice: scene + clock, present characters with
  pose/clothing, contacts, consent/boundary flags (or a freeze banner), obsessions/cravings above
  `drives.inject_threshold`, recent dice rolls. Respects `consent.mode == "unrestricted"` (raw):
  consent lines are inert, but a **user-commanded** freeze still surfaces (user controls always work).
- `estimate_tokens` ≈ chars/3.3 (ST's own fallback ratio).
- `govern` enforces `min(max_tokens, max_fraction × assumed_ctx)` with a `header_floor_tokens`
  floor; drops components from lowest priority up (priorities from `InjectionConfig.priorities`).
- `splice` places the briefing per `injection.placement` (`depth | system_merge | suffix | st_native`)
  at `injection.depth` messages from the end.

**Edit points.** To add a line to the briefing, extend `render_header`. To add a new injected
component (e.g. a new note type), add a `Component` in `compose()` and a priority key in
`InjectionConfig.priorities`. Placement modes live in `splice`.

---

## `state.py` — state core (the heart)

**Responsibility.** Define the canonical state dict, validate ops, enforce mutation authority, and
apply ops via a **pure reducer**. Full field/op reference in `02-data-model.md`.

**Public API.**
- `empty_state()`, `is_empty(state)`, `state_summary(state)` (inspector payload),
  `derived_exposure(state, eid)`.
- `validate_op(op) -> op|None` — per-op shape/enum validation.
- `resolve_aliases(op, state, source) -> (op|None, reason)` — names → entity ids; unknown +
  non-user source → quarantine (feeds discovery); unknown + user → auto-create.
- `authority_violation(op, source, state, cfg) -> reason|None` — the authority matrix.
- `reduce_state(state, ops) -> state` — pure mechanical replay of pre-authorized journaled ops.
- `apply_delta(store, session_id, branch_id, turn, ops, source, cfg, turn_lo=None) -> ApplyResult`
  — the full apply pipeline: validate → order → alias-resolve → authority-check → enrich → apply →
  journal (only what applied) → checkpoint on cadence → mirror `frozen` to the session row.
- `translate_path(path, value) -> op|None` — `((aether.set scene.location Tavern))` → typed op.
- `current_state(store, branch_id)` — reduce from journal to now.

**Key internals & design rules.**
- **The reducer is pure.** Config-dependent values (craving seeds, withdrawal thresholds) are
  **baked into the journaled op** at apply time via `_enrich` (`_seed`), so a later config change
  never rewrites history. This is what makes replay deterministic.
- **Authority runs before journaling.** The journal holds only authorized ops; replay applies them
  mechanically. Sources: `user > genesis > rule > extraction`, gated by families
  (`scene | facts | organic | consent | safety`). See `02-data-model.md` for the full matrix.
- **Op families & apply order.** `_ORDER` sorts ops within a delta so `freeze` applies first
  (a mid-delta safeword gates the rest), then entity/presence/scene, then physical layers.
- **Single source of truth for enums:** `OP_FIELD_ENUMS` — `extraction.py` derives *both* wire
  schemas from it; `validate_op` must agree (welded by a test).
- Non-live scenes (flashback/dream) quarantine physical/consent/clock mutations from non-user
  sources (`_NONLIVE_SUPPRESSED`). Frozen sessions suppress arousal/escalation/consent
  (`_FROZEN_SUPPRESSED`).

**Edit points.** THE most common addition site. Adding an op kind touches, in this file: `_SPEC`
(required fields), `_FAMILY` (authority family), `_ORDER` (if it must apply early), `OP_FIELD_ENUMS`
(if it has enum fields), `validate_op` (the enum checks), and a branch in `_apply_op` (the mutation).
Then `extraction.py` + `prompts.py` if extraction should emit it. Recipe in `04-maintenance-playbook.md`.

---

## `store.py` — SQLite spine

**Responsibility.** Persist everything; provide `state_at` (checkpoint + journal replay) and all
the point-read/upsert helpers the hot and cold paths use. Full schema in `02-data-model.md`.

**Public API (grouped).**
- Sessions/branches: `get_or_create_session`, `create_session`, `touch_session`,
  `relink_external`, `live_branches`, `fork_branch`, `session_delete`, `session_label_set`,
  `session_mode`/`session_mode_set`, `genesis_state`/`genesis_mark`, `set_frozen`.
- Messages/turns: `append_msgs`, `truncate_msgs`, `get_msgs`, `record_turn`, `bump_swipe`,
  `settle_head`, `pending_extractions`, `mark_extraction`, `rollback_to`.
- Journal/state: `journal`, `checkpoint`, `state_at(branch, turn, reducer, empty)`.
- Turn texts: `write_turn_text`, `get_turn_texts`.
- Slices/recall/notes: `write_slice`/`read_slice`, `write_recall`/`read_recall`,
  `write_note`/`read_note`.
- Memories: `memories_add`, `memories_candidates`, `memories_bump_access`, `memories_set_parent`,
  `memories_stale_episodic`, `memories_members`, `memories_update_text`,
  `summaries_unsynthesized`.
- Embeddings: `embeddings_missing/put/get`.
- Lint: `lint_add`, `lint_recent`, `lint_l9_evidence`, `lint_counts`.
- Director: `director_add`, `director_recent`, `director_counts`.
- Discovery (entity evidence): `discovery_bump`, `discovery_mark`, `discovery_rows`.
- Caps (capability cache): `caps_get/set/all/fail/ok`.
- Hints: written by control `/hint`.

**Key internals.**
- `_SCHEMA` is the full DDL (see `02-data-model.md`). WAL mode; one process; a coarse `threading.Lock`
  around tiny critical sections.
- `_MIGRATIONS` is an **additive-column** migration list (table, col, decl). On open it `ALTER
  TABLE ADD COLUMN` any missing column. **Schema changes must be additive** — never drop/rename in
  place, or old DBs break invariant 4.
- `state_at` = nearest checkpoint ≤ turn + ordered replay of journal ops through the caller-supplied
  reducer (`state.reduce_state`). This same primitive drives edit-forks, swipe rollback, replay
  harness, and the inspector scrubber.

**Edit points.** New persisted data = new table in `_SCHEMA` + helper methods here. New column on an
existing table = append to `_MIGRATIONS` too. Keep helpers thin; put semantics in the caller.

---

## `jobs.py` — cold-path job runner

**Responsibility.** Schedule and run Tier-1 extraction without ever burying a weak machine or
blocking a turn.

**Public API.** `JobRunner(store, cfg, ladder)` with `notify(session_id, branch_id, head_turn)`,
`resume_pending()`, `drain(timeout)`, `stop()`, `endpoint_for(session_id) -> (Endpoint, group, conc)`.
State: `.models` (session → model id), `.user_names` (session → persona name), `.ladder`, `._tasks`.
Dataclass `Batch`.

**Key internals.**
- `notify` settles the head turn, then either flushes immediately (cadence reached) or arms a
  `debounce_s` idle timer. **Lag-1**: turn T-1 extracts when turn T arrives (swipes settle first).
- `_flush` collects up to `batch_max_turns` settled, unextracted turns into one `Batch` and enqueues
  it on a priority `asyncio.Queue`. One serialized worker (`_work`) drains it; `_run_guarded` bounds
  concurrency with a semaphore (assist `max_concurrent`).
- `_run_batch` calls `ladder.extract`, applies deltas (`state.apply_delta`, source `extraction`),
  runs the full lint pass + director staging + memory index + recall precompute for the next turn.
- **Auto-disable (09 C2):** `fail_autodisable_after` consecutive failed batches → Tier-1 off for
  that session until `fail_reenable_after_turns` later. Tracked in `_fails` / `_disabled_until`.
- `_discover_from_quarantine` mines unknown-entity quarantine reasons to feed discovery.
- `resume_pending` re-queues turns left `extraction='pending'` by a crash/restart.

**Edit points.** Cadence/debounce/batch logic; priority ordering of cold-path work; new cold-path
job types (add to `_run_batch`). Everything here is cold-path and fail-open.

---

## `extraction.py` — the Tier-1 capability ladder

**Responsibility.** Get structured state-delta JSON out of *any* backend, from grammar-constrained
locals to freeform hosted APIs, and parse it robustly.

**Public API.** `Ladder(store, cfg, get_client)` with `rung_for(ep) -> int` and
`extract(ep, state_snapshot, characters, t0, t1, exchange) -> ExtractResult`. Dataclass `Endpoint`.
Free functions: `parse_and_validate(text) -> StateDelta|None`, `repair_json`,
`strip_fences_and_prose`, `scrub_op`, `enum_salvage`, `delta_json_schema()`,
`delta_json_schema_anyof()`, `is_local_host`, `is_venice_host`, `thinking_supported/active`.
Pydantic `StateDelta`. Exception `TransientUpstreamError`.

**Key internals.**
- **Rungs:** 1 = native grammar (GBNF/guided_json — llama.cpp/koboldcpp/vLLM), 2 = strict
  `response_format: json_schema`, 3 = `json_object` mode + schema-in-prompt, 4 = freeform prompt +
  robust parse. Venice/GLM lands on rung 3–4 — the bottom rungs are the primary path, not a fallback.
- **Capability probe:** probe once with a trivial schema request, classify by response/error, cache
  in the `caps` table (per `base_url+model`), re-probe on TTL/failure. `force_rung` skips probing.
  `_fingerprint` uses `/models` + port hints (`_PORT_HINTS`: 11434 ollama, 1234 lmstudio, 5001 kobold).
- **anyOf schema (Q18):** per-op `anyOf` schema at rung 2 where strict mode accepts it
  (probed → `caps.anyof`); flat-schema fallback otherwise. `use_anyof` config toggles it.
- **Parse pipeline:** `strip_fences_and_prose` → `repair_json` (brace balance, trailing-comma/quote
  repair) → pydantic `StateDelta` → per-op `scrub_op` (drop unknown fields) + `enum_salvage`
  (fix near-miss enums). One **repair pass** per rung (re-prompt with the error + malformed output);
  still failing → mark job failed, keep previous state, demote rung confidence.
- **Thinking/reasoning models:** disabled for extraction by default; budgeted if `extraction.thinking
  = "on"`. `_vendor_params` adds vendor-specific knobs.
- **Enums derive from `state.OP_FIELD_ENUMS`.** `EXTRACTION_OPS`, `_OP_ALLOWED`, `_OP_FIELDS` define
  which ops extraction may emit and their allowed fields.

**Edit points.** Add a rung or a backend dialect (`_NATIVE`, `_PORT_HINTS`); harden parsing
(`repair_json`, `scrub_op`, `enum_salvage`); change which ops extraction may emit (`EXTRACTION_OPS`,
`_OP_ALLOWED`). If you add an op in `state.py`, add it here too or extraction can't produce it.

---

## `prompts.py` — extraction prompts

**Responsibility.** Hold the *stable* extraction prompt so a backend can cache it (Venice compiles
once). One prompt+schema, RP prose always fenced in `<data>` tags (untrusted), an empty-ops shot in
every call (anti-hallucination anchor).

**Public API.** `system_prompt(rung, assist_tier=False, include_card=True)`,
`few_shots(assist_tier=False)`, `user_message(state_snapshot, characters, t0, t1, exchange)`,
`repair_prompt(parser_error, malformed)`. Constants `SYSTEM_CORE`, `OP_CARD`.

**Key internals.** `OP_CARD` is a compact `op → required fields` reference the model reads. Q17
lesson (baked into the docstring): schemas enforce *shape*, not op *vocabulary* — dropping the OP
CARD made ops absent from the shots unlearnable, so the card ships at every rung by default
(`extraction.trim_op_card` restores the trim for budget users at schema rungs).

**Edit points.** When you add an op kind, add its line to `OP_CARD` and ideally a few-shot, or the
model won't emit it even though the schema allows it.

---

## `genesis.py` — two-stage seeding

**Responsibility.** Turn a character card + greeting into a starting state the moment a chat opens.

**Public API.** `card_and_prompt(doc) -> (card, prompt)`, `rules_ops(card, prompt, speaker) -> ops`,
`seed_rules(store, cfg, session_id, branch_id, doc, speaker)`,
`async seed_llm(store, cfg, get_client, ep, session_id, branch_id, card, opening, speaker)`.

**Key internals.**
- **Stage A (rules, inline/hot):** cheap regex/keyword derivation of clothing, entities, initial
  scene from the card — sub-ms, runs during the first request.
- **Stage B (LLM, cold):** after turn 1's stream ends, an assist/main LLM does a full-matrix seed
  (`_parse_ops`/`_coerce` normalize its output). Scheduled from `pipeline._genesis_pass`.
- Idempotent via the `sessions.genesis` marker (`'' | rules | done | skipped`); `/aether-genesis`
  forces a re-seed even if marked done.

**Edit points.** Improve card parsing in `rules_ops`/`_coerce`; change what Stage B extracts via its
prompt. Keep Stage A LLM-free (it's on the hot path for the first turn).

---

## `discovery.py` — entity discovery

**Responsibility.** Don't create an entity the first time a name appears; **count evidence** across
turns, then promote. Prevents spurious entities from one-off mentions.

**Public API.** `scan(text) -> set[str]` (capitalized-name candidates), `known_names(state, extra)`,
`consider(store, cfg, session_id, branch_id, turn, name, ...)`, `observe_text(store, cfg, ..., text,
known)`.

**Key internals.** Uses the `discovery` table (`branch_id, name, turns, status`). A name seen on
enough distinct turns flips `counting → promoted` and an `entity_add` is applied. `auto_entity_create`
config gates it.

**Edit points.** Promotion threshold, name-candidate regex (`scan`), stop-words.

---

## `linter.py` — consistency checks

**Responsibility.** Compare the new assistant prose against tracked state; record violations that
become next-turn corrective director notes (**never** rewrite the current response).

**Public API.** `lint_turn(store, cfg, session_id, branch_id, turn, state, text, klass, user_name,
user_aliases) -> list[Violation]`. Dataclass `Violation(rule, severity, subjects, detail, note,
evidence)`.

**Key internals (the rules).**
- `_l1_colocation` — penetrating/restraining contact or pose-anchor to an absent character.
- `_l2_exposure` — prose describes exposure/covering inconsistent with tracked clothing.
- `_l3_contact` — contact geometry vs pose (e.g. penetrating while `lying_front`).
- `_l4_items` — using an item marked removed/destroyed.
- `_l5_absent_voice` — a character speaks/acts who isn't present.
- `_l6_timeline` — time-of-day/day contradictions (silenceable via `rules_off=["L6"]`).
- `_l7_belief_leak` — a character references a secret they can't know (theory-of-mind).
- `_l8_consent` — action beyond tracked consent level (inert in raw mode / freeze).
- **L9** (user-voice guard) — prose that speaks in the user's persona voice; escalates over
  `consent.guard_escalate_turns`. Runs on the hot-adjacent cold pass so the guard can correct the
  very next turn.

**Edit points.** Add `_lN_*(state, ..., v)` and call it in `lint_turn`; document a `rules_off` code.
Each rule appends `Violation`s; the director turns them into notes.

---

## `director.py` — deterministic beat engine

**Responsibility.** Choose at most one active "beat" (pacing/drama guidance) from authored JSON
libraries whose preconditions match tracked state, and render its note for the next turn.

**Public API.** `stage(store, cfg, session_id, branch_id, turn, state, violations, user_name,
user_aliases)` (the cold-path entry), `load_libraries(names)`, `eval_dsl(cond, state, trace)`,
`resolve_path(state, path)`, `bindings(beat, state, user_ids)`, `render_note(template, binding,
state)`, `consent_headroom(...)`.

**Key internals.**
- **Beat file shape** (`beats/*.json`, full spec in `02-data-model.md §6`): required `beat_id`,
  `name`, `preconditions`, `note_template`; optional `binds` (`none|char|pair|craving|obsession`),
  `effects` (ops applied `source=rule` on fire), `phase_hint`, `priority`, `cooldown_turns`,
  `once_per_scene`.
- **Precondition DSL (`eval_dsl`/`_leaf`):** combinators `all`/`any`/`not`; leaf ops
  `==, !=, >, >=, <, <=, in, contains, exists`. `resolve_path` reads dotted paths into state
  (`char.{char}.craving.{substance}.level`, `scene.tension`, `rel.A->B.trust`,
  `consent.A->B.cat.level`, `session.frozen`, …); `{char}`/`{a}`/`{b}`/`{substance}`/`{obs_key}`
  tokens bind per candidate. An unresolved path → leaf false + an authoring warning.
- **`binds`** enumerates candidates deterministically and the user's character never fills an actor
  slot: `pair` exposes `{a}`/`{b}` (+ `{initiator}`/`{partner}` in the note).
- Selection: filter by cooldown/`once_per_scene` (via the `director` table), then winner by
  `(priority, consent_headroom, beat_id)`; render + `store.write_note` for next turn. **No match →**
  a pacing pseudo-beat (`pacing.complication/raise/ease`). Frozen → `aftercare_checkin` only;
  flashback/dream → no steering. Linter violations fold in as higher-priority **corrective** notes.

**Edit points.** Add/tune beats in `beats/*.json` (no code change if it uses existing paths + ops;
a beat may carry `effects`/`phase_hint`). New precondition paths → extend `resolve_path`. New op or
combinator → extend `_leaf`/`eval_dsl`. New binding kinds → extend `bindings`. Register new
libraries in `DirectorConfig.beat_libraries`.

---

## `memory.py` — tiered memory + recall

**Responsibility.** Store meaningful events, retrieve the most relevant few for the next turn, and
periodically consolidate.

**Public API.** `index_applied(store, session_id, branch_id, applied_ops, state)`,
`retrieve(store, cfg, branch_id, state, query_text, query_vec=None) -> rows`,
`recall_lines(rows, now_turn)`, `render_recall(lines, who)`, `when_phrase(delta_turns)`,
`reflect(store, cfg, session_id, branch_id, state) -> int`,
`precompute_recall(store, cfg, session_id, branch_id, state, query, turn)`.

**Key internals.**
- `memory_event` ops become `memories` rows (tier `episodic`), tagged, with importance.
- **Scoring** = recency × importance × relevance (generative-agents style), weights + decay from
  `MemoryConfig`. Relevance is BM25-ish keyword overlap (`_bm25ish`) plus cosine over embeddings
  when present (`_cos`); `_prefilter` narrows candidates to scene participants/location/tags first.
- `reflect` consolidates episodic memories per scene into summaries every
  `reflection_every_scenes`.
- `precompute_recall` writes the next turn's recall lines to the `recall` table (hot path just reads).

**Edit points.** Scoring weights (`_relevance`, `retrieve`), consolidation cadence (`reflect`), recall
phrasing (`recall_lines`, `render_recall`).

---

## `assist.py` — local-model sidecar

**Responsibility.** Helpers for calling the assist (local) endpoint: chat, embeddings, memory
synthesis, NLI contradiction pass.

**Public API.** `endpoint_for_group(cfg, group, model_hint) -> Endpoint`,
`async _chat(...)`, `async embed_texts/embed_missing/embed_query(...)`, `unpack(blob)`,
`async synthesize(...)` (summary/facts), `async nli_pass(...)` (contradiction detection).

**Key internals.** Reads `assist.endpoints` + `assist.groups`. Handles reasoning models
(`reasoning_content` fallback when `content` empty). Embeddings are packed to BLOBs for the
`embeddings` table. All cold-path, all fail-open to `rules` mode.

**Edit points.** New assist-powered features hook here and are gated by an `assist.groups` entry.

---

## `control.py` — control/Console API (`/aether/*`)

**Responsibility.** Everything the Console and ST extension call. Full route list in
`03-config-and-api.md`.

**Public API.** `make_control_router(cfg, store, jobs=None) -> APIRouter`. Helpers `_persist_config`
(writes `config.toml`), `_session`, `_head`.

**Key route groups.** `/console` (serves the HTML), `/override` (manual-override toggle),
`/session/{sid}/genesis|mode|writeback|state|freeze|unfreeze|label` , `/extraction` (cadence/intake
get+set), `/groups` (assist group live-toggle), `/connection` + `/connection/models` (endpoint setup
with a real auth test), `/sessions` (list), `/hint` (fire-and-forget UI hints), delete session.

**Key internals.** `/connection/models` does a live `GET /models` and a real `chat/completions`
probe to verify the key. Config-mutating routes persist via `_persist_config` so changes survive
restart. `/session/{sid}/state` PATCH routes `((aether.set))`-style path/value through
`state.translate_path` under the authority matrix.

**Edit points.** New Console/extension capabilities are routes here. If they mutate config, persist
it; if they mutate state, go through `state.apply_delta`/`translate_path` (never write state directly).

---

## `status.py` — health endpoint

**Responsibility.** `/aether/status` — version, mode, extraction view, session/lint/director counts.

**Public API.** `make_status_router(cfg, store=None, jobs=None) -> APIRouter`. Helper
`_extraction_view(cfg, store, jobs)`.

**Edit points.** Add health/metrics fields here. The ST panel chip and `/aether-status` slash
command read this shape — keep them in sync.

---

## `st-extension/` — SillyTavern Companion

**Responsibility.** Stamp identity, capture generation type, provide the panel + slash commands,
seed at chat-open, run the writeback loop. Entirely fail-open — if the proxy is down or the file
errors, ST works untouched.

**Key internals (`index.js`).**
- **Identity:** per-chat `chatMetadata.aetherstate_sid`; writes `x-aetherstate-session` into
  `custom_include_headers` (L1, Custom source only) and injects the `<<AETHER:...>>` sentinel on
  `CHAT_COMPLETION_PROMPT_READY` (L2, all Chat Completion sources), always skipping dry runs.
- **Gen type:** `aetherstateInterceptor` (registered via `manifest.generate_interceptor`) +
  `GENERATION_STARTED` capture `swipe|regenerate|impersonate|normal|continue`; increments the turn
  counter on real generations only.
- **Genesis at chat-open:** `CHAT_CHANGED` → `doGenesis()` POSTs the card/greeting to
  `/aether/session/{sid}/genesis` (the greeting renders with no request, so the extension hands the
  card over itself). Idempotent server-side.
- **Panel:** status chip, freeze/resume, manual-override toggle, enabled/enrichment toggles, proxy
  URL, persona name, cadence + intake inputs, assist-group selectors. All wired to `/aether/*`.
- **Slash commands:** `/aether-status`, `/aether-freeze`, `/aether-resume`, `/aether-set`,
  `/aether-mode`, `/aether-genesis`, `/aether-cadence` (registered via `SlashCommandParser.addCommandObject`).
- **Writeback loop:** polls `/aether/session/{sid}/writeback` and applies a `chatMetadata` patch
  (WI/Author's-Note routes are reserved for a later route-split to avoid double injection).

**Edit points.** Anything user-facing in ST. Keep the sentinel format in lock-step with `stamps.py`.
Keep panel/slash-command reads in sync with `status.py` / `control.py` response shapes.
