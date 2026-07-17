# AetherState — Maintainer's Map (v1.0.0)

> **Purpose of this docs set.** A complete, code-accurate map of AetherState so that future
> work — repairs, updates, additions — can be scoped and executed (by a human or a skill)
> without re-reading the whole tree. Every claim here is grounded in the shipped source under
> `src/aetherstate/`. When code and this doc disagree, **the code wins** — fix the doc.
>
> Companion docs:
> - `01-module-reference.md` — every module: responsibility, public API, edit points.
> - `02-data-model.md` — the state dict, the op vocabulary, the authority matrix, the DB schema.
> - `03-config-and-api.md` — every config key, every HTTP route, the ST extension contract.
> - `04-maintenance-playbook.md` — task recipes ("I want to add an op", "fix extraction", …),
>   testing, invariants, glossary.
> The public [README](../README.md) gives the Player-facing overview of the Semantic Atlas,
> PlayerLex, Player Lessons, and their authority and privacy boundaries. This maintainer set
> describes the shipped implementation; when an older design citation disagrees, the current
> source and tests win.

---

## 1. What AetherState is (in one paragraph)

A **local, transparent, OpenAI-compatible reverse proxy** that sits between any RP frontend
(SillyTavern, RisuAI, Agnai, oobabooga, KoboldCpp Lite) and any OpenAI-compatible model backend
(Venice.AI, OpenAI, OpenRouter, Ollama, KoboldCpp, llama.cpp, vLLM, LM Studio). On every turn it
does three cheap things on the hot path — identify the session, splice a small precomputed
**state briefing** into the prompt, and stream the reply through byte-for-byte — and does all
the expensive cognition (state extraction, memory, linting, director beats) **after** the stream
finishes, asynchronously. The frontend and the model never know it is there. With every feature
off it is a correct OpenAI endpoint and nothing more.

## 2. The four invariants (never break these)

These are the spine of the whole design. Any change that risks one is wrong until proven safe.

1. **Fail open to passthrough.** If anything in AetherState errors, the *original, unmodified*
   request/response flows anyway. Enforced by `try/except` supervisors at every boundary in
   `proxy.py` and `pipeline.py`, and by per-op quarantine in `state.py`.
2. **Never block, corrupt, or delay the token stream.** The hot path does only microsecond work
   (parse header, hash, SQL point-reads, string splice). SSE bytes are relayed verbatim via an
   async tee (`proxy.py::stream_bytes`); the tee copies *after* yielding. No LLM/embedding calls
   on the hot path — they are cold-path jobs that start only after `[DONE]`.
3. **Never crash the frontend on malformed/missing state.** State reads are total functions
   (missing session → empty state → inject nothing). Bad extraction output → job fails, state
   unchanged, next turn uses the previous snapshot. The response bytes are never rewritten.
4. **Local-first & private.** Everything lives under one `data_dir` (default `./aetherstate-data/`):
   SQLite DB, config, traces. No telemetry — there is no key to enable it. Prose is redacted from
   logs by default.

## 3. The two planes

```
                         HOT PATH  (synchronous, sub-millisecond, per request)
 frontend ──POST /v1/chat/completions──▶ proxy.relay
                                          │  strip sentinel / read header        (stamps.py)
                                          │  resolve session + classify turn      (session_engine.py)
                                          │  genesis rules seed (new session)      (genesis.py)
                                          │  Tier-0 rules: OOC strip, dice,        (tier0.py)
                                          │    safeword, presence, clock
                                          │  apply user+rule deltas                (state.py)
                                          │  compose state briefing under budget   (compose.py)
                                          ▼
                                     forward to upstream, tee SSE bytes back verbatim
                                          │
                        [DONE] ───────────┘
                         COLD PATH  (async, after stream ends — never blocks anything)
                                          │  capture assistant text                (pipeline.on_response)
                                          │  entity discovery                       (discovery.py)
                                          │  recall precompute / reflection         (memory.py)
                                          │  consistency lint → director note       (linter.py, director.py)
                                          │  genesis stage-B LLM seed (turn 1)       (genesis.py)
                                          ▼
                                     JobRunner.notify → debounced batch             (jobs.py)
                                          │  Tier-1 extraction via capability ladder (extraction.py)
                                          │  validate → apply deltas → journal       (state.py, store.py)
                                          │  lint + director + memory + recall precompute
                                          ▼
                                     next turn's briefing is now a pure DB read
```

**Golden rule for edits:** anything that costs more than a SQLite point-read or a regex belongs
on the **cold path**. If you are tempted to add an LLM call, a network call, or an embedding to
`Pipeline.process` (hot path), stop — it goes in `jobs.py` / `on_response` instead.

## 4. Module map

Grouped by plane and role. Full detail per module in `01-module-reference.md`.

### Entry / wiring
| Module | Role |
|---|---|
| `__main__.py` | CLI entry. AV hardening (drops `SSLKEYLOGFILE`, injects `truststore`), logging setup, uvicorn launch. |
| `app.py` | `create_app()` factory — wires Store, SessionEngine, JobRunner, Pipeline, routers. `get_client` injectable (tests mount a mock upstream). |
| `config.py` | Pydantic v2 config models + TOML/env loader. Never raises; `.bak` last-known-good fallback. |

### Hot path
| Module | Role |
|---|---|
| `proxy.py` | The transparent byte-relay. Catch-all `/{path}` route, sentinel strip, header handling, SSE tee, OpenAI-shaped error fallbacks. |
| `stamps.py` | Parse & strip the `x-aetherstate-session` header and `<<AETHER:...>>` sentinel. |
| `canon.py` | Canonicalize `messages[]` → stable transcript core + chained prefix hashes (for L3 identity). |
| `lcp.py` | Longest-common-prefix index (radix-ish) over branch message chains. |
| `session_engine.py` | L1 header / L2 sentinel / L3 heuristic session resolution + 4-way turn classification. |
| `pipeline.py` | Orchestrates hot-path enrichment and cold-path finish. The conductor. |
| `tier0.py` | Deterministic per-turn rules and natural-action routing: OOC/`((aether.*))` strip, semantic evidence, dice, safewords, presence, clock tick. |
| `semantic.py` | Evidence-bearing `SemanticTurn` / action frames, overlap precedence, and honest ambiguity. |
| `phrasebook.py` | Local no-model Mechanics Phrasebook matcher; parameterized constructions live in `registry/mechanics_phrasebook.toml`. |
| `enemy_kits.py` | Deterministic cross-genre enemy kit grammar, frozen intent selection, fingerprints, grounding, and canonical matching. |
| `capability_glossary.py` | Cold-path CapabilityLex: 265 concepts across 31 genres, conservative Concept Facets/meaning fingerprints, and immutable `capability-definition/1` preview/freeze. |
| `semantic_fabric.py` | LexFabric recognition-only composition of CapabilityLex, ReferentLex, SceneLex, and ActionLex with exact spans, ambiguity, constructions, meaning fingerprints, and content-free receipts. |
| `semantic_atlas.py` | Verified searchable/paged catalog over all 311 exact Lex-qualified meanings without authority. |
| `playerlex.py` | Local explicit Player-approved names, aliases, and anchored authoring patterns bound to exact Lex-qualified Atlas fingerprints; full-casefold span matching, dual-mode optimistic correction, forged/corrupt-row quarantine, exact v2 schema/migration verification, retry-safe secure DB/WAL removal, and recognition-only proposals. |
| `playerlex_recognition.py` | Strict RPG live fan-in from current PlayerLex proposals to the compiled LexFabric receipt; rejects an invalid overlay row, otherwise fails open to unchanged shared recognition, re-derives valence from sealed meaning, and creates no candidate or mechanic. |
| `player_lessons.py` | Separate local Player Lessons service: explicit narration and intent lifecycles, optional narration anchors, required ActionLex/action or ReferentLex/target intent anchors, local record-only intent notes, separate frozen selection/delivery/application receipts, and retry-safe secure DB/WAL removal. |
| `worldlex.py` | Domain-neutral WorldLex refs, adapter contracts, translation products, and staged capability pools. |
| `enemy_capability_pool.py` | Enemy-kit translation, full HP mechanics adapter snapshots, receipt admission, staged pools, and exact kit reconstruction. |
| `compose.py` | Render the state briefing and the separate bounded Player Lessons narration component, govern both under authority and token budgets, and splice into `messages[]`; selected narration text may reach the configured provider, intent lessons never enter composition, and private lesson text is not kept for prompt prewarm. |

### State core
| Module | Role |
|---|---|
| `state.py` | **The heart.** Canonical state dict, op spec + validation, mutation authority matrix, the pure reducer, OOC path translation. |
| `store.py` | SQLite spine. Schema (DDL), sessions/branches/turns, ops journal + checkpoints, `state_at` replay, memories, lint, director, caps, slices. |
| `worldlex_store.py` | Stable world lineage plus append-only exact definition revisions, composed into Store transactions. |
| `worldlex_assignment.py` | Repository-backed exact acquisition materialization and repository-free assignment replay validation. |

### Cold path — cognition
| Module | Role |
|---|---|
| `jobs.py` | `JobRunner`: debounce, batching, per-session serialization, extraction scheduling, restart recovery, auto-disable on repeated failure. |
| `extraction.py` | Tier-1 extraction: capability probe, rung 1–4 ladder (grammar → strict JSON → JSON mode → freeform), parse/repair/salvage. |
| `prompts.py` | The extraction system prompt, OP CARD, few-shots, repair prompt. Verbatim from planning/04. |
| `genesis.py` | Two-stage seeding from character card + greeting: rules pass (inline) + LLM pass (cold). |
| `discovery.py` | Entity discovery — count evidence for unknown names before creating entities. |
| `linter.py` | Consistency checks L1–L11 (colocation, exposure, contact geometry, inventory, user-voice, timeline, belief leak, consent; L11 rpg-only: player agency + verbatim bracketed quotes). |
| `director.py` | Deterministic beat engine: JSON beat libraries, precondition DSL, binding, cooldowns, note rendering. |
| `memory.py` | Tiered memory: index, BM25-ish + embedding retrieval, recall rendering, reflection/consolidation. |
| `assist.py` | Local-model sidecar helpers: chat call, embeddings, memory synthesis, NLI pass. |

### Control plane (HTTP `/aether/*`)
| Module | Role |
|---|---|
| `control.py` | The Console/extension API: genesis, mode, extraction/cadence, groups, connection setup+test, sessions CRUD, state view, PlayerLex, Player Lessons, `((aether.set))` PATCH, freeze. |
| `status.py` | `/aether/status` health + extraction view. |
| `static/console.html` | The built-in web dashboard (served at `/aether/console`). |

### Non-Python
| Path | Role |
|---|---|
| `st-extension/index.js` | The SillyTavern Companion extension: stamps identity, captures gen-type, panel + slash commands, writeback loop. |
| `st-extension/manifest.json` | ST extension manifest (registers `aetherstateInterceptor`). |
| `st-extension/style.css` | Panel styling. |
| `src/aetherstate/beats/*.json` | Authored director beat libraries (5 files). |
| `tests/*.py` | The replay harness, focused regression suites, and a mock upstream. |

## 5. Where-to-change-what (fast index)

| I want to… | Go to | Also touch |
|---|---|---|
| Add / change a **tracked state field or op kind** | `state.py` (`_SPEC`, `_FAMILY`, `OP_FIELD_ENUMS`, `_apply_op`, `validate_op`) | `extraction.py` (`EXTRACTION_OPS`, `_OP_ALLOWED`, `_OP_FIELDS`), `prompts.py` (OP CARD), `compose.py` (render), `02-data-model.md` |
| Change **who can mutate what** (authority) | `state.py::authority_violation` | `02-data-model.md` authority matrix |
| Add / edit a **director beat** | `beats/*.json` (+ register in `config.DirectorConfig.beat_libraries`) | `director.py` if a new precondition path/op is needed |
| Add a **consistency check** | `linter.py` (add `_lN_*`, wire into `lint_turn`) | `config.LinterConfig.rules_off` docs |
| Add / change a **Tier-0 rule** (dice, OOC cmd, safeword, clock) | `tier0.py` | `state.py` if it emits a new op |
| Change **enemy move generation / intent matching** | `enemy_kits.py` | `state.py`, `tier0.py`, `compose.py`, `hud.py`, `tests/test_enemy_kits.py` |
| Change **CapabilityLex / definition compiler** | `capability_glossary.py`, `corpus/capability-glossary/` | `tests/test_capability_glossary.py`; do not migrate `enemy_kits.py` without fingerprint parity |
| Change **LexFabric recognition / Semantic Atlas** | `semantic_fabric.py`, `semantic_atlas.py`, `corpus/semantic-fabric/` | `tests/test_semantic_fabric*.py`, `tests/test_semantic_atlas.py`; preserve exact Lex-qualified fingerprints and zero authority |
| Change **PlayerLex approvals / live recognition** | `playerlex.py`, `playerlex_recognition.py` | `semantic.py`, `tier0.py`, `pipeline.py`, `control.py`, `static/console.html`, `tests/test_playerlex*.py`; preserve normal/corrupt version proof, exact source spans, retry isolation, current-meaning revalidation, exact schema/object checks, secure DB/WAL deletion, and zero direct candidate/mechanic authority |
| Change **Player Lessons** | `player_lessons.py` | `pipeline.py`, `tier0.py`, `compose.py`, `control.py`, `static/console.html`, `tests/test_player_lessons*.py`; preserve narration-versus-intent separation, informed provider disclosure for selected narration text, local record-only intent notes, exact-anchor-only intent narrowing, actor non-support, separate delivery/application receipts, narration replay versus fresh-turn-only intent application, current-revision duplicate reuse, no lesson prompt prewarm, header-only delivery evidence that claims no adherence/completion, truth-gate inertness, secure DB/WAL/cache deletion, and honest provider/in-flight/backup limits |
| Change **WorldLex storage, assignment, pools, or an adapter** | `worldlex*.py`, the domain adapter (`enemy_capability_pool.py` today) | `state.py`, domain tests; preserve recognized/authorized/executable separation |
| Add an **extraction rung** or fix parsing | `extraction.py` (`Ladder`, `repair_json`, `scrub_op`, `enum_salvage`) | matching extraction and configuration tests |
| Add / change a **config setting** | `config.py` (add field to the right model) | `config.example.toml`, `03-config-and-api.md` |
| Add a **control API route** | `control.py` (`make_control_router`) | `st-extension/index.js` if the UI needs it, `03-config-and-api.md` |
| Change **injection placement / budget** | `compose.py::govern`, `compose.py::splice` | `config.InjectionConfig` |
| Change **session identity / turn classification** | `session_engine.py`, `canon.py`, `lcp.py` | `stamps.py` (stamp format) + `st-extension/index.js` (must agree) |
| Change the **DB schema** | `store.py` (`_SCHEMA`, `_MIGRATIONS` — additive only!) | `04-maintenance-playbook.md` migration recipe |
| Change **memory retrieval / scoring** | `memory.py` (`_relevance`, `retrieve`, `reflect`) | `config.MemoryConfig` |
| Change **assist groups** (embeddings / reflection / NLI) or per-group routing | `assist.py` (cold-path sidecar) + `config.AssistGroupsConfig` | `control.py` (`POST /aether/groups`), `config.example.toml [assist.groups]` |
| Change **entity discovery** (evidence → privileged create) | `discovery.py` | `tier0.py` evidence + `jobs.py` feed; extraction can never create entities |
| Change the **ST extension** behavior | `st-extension/index.js` | `control.py` routes it calls |

## 6. Runtime shape

- **Process:** single Python process (FastAPI + uvicorn), single SQLite file (WAL mode).
- **Default endpoint:** `http://127.0.0.1:9130`. Frontends point their OpenAI base URL at
  `http://127.0.0.1:9130/v1`.
- **Data dir:** `./aetherstate-data/` → `aetherstate.db`, `config.toml` (+ `.bak`), traces.
- **Two upstreams:** `[upstream]` = the **main** model (writes the story), `[[assist.endpoints]]`
  = the **helper/assist** model (tracks state in the background). They may be the same service.
- **Dependencies:** fastapi, httpx, pydantic v2, uvicorn, truststore, tomli (py3.10). Python ≥3.10.
- **Launch:** `Start-AetherState.bat` (Windows) / `start-aetherstate.sh` (Linux/macOS), or
  `python -m aetherstate --config ./aetherstate-data/config.toml`.

## 7. Reading order for a newcomer / a fresh skill session

1. This file.
2. `proxy.py` then `pipeline.py` — the request path and its orchestration boundary.
