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
>
> The `planning-precompletion of AetherState/` folder (sibling to this repo) is the **design
> intent** written before the code existed. It is excellent background and is cited throughout
> the source as `NN-name.md §X` / `QNN`. It is *not* always current with the shipped code —
> this docs set describes what actually shipped.

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

## 4. Module map (25 Python modules, ~6,900 LOC)

Grouped by plane and role. Full detail per module in `01-module-reference.md`.

### Entry / wiring
| Module | LOC | Role |
|---|---|---|
| `__main__.py` | 51 | CLI entry. AV hardening (drops `SSLKEYLOGFILE`, injects `truststore`), logging setup, uvicorn launch. |
| `app.py` | 67 | `create_app()` factory — wires Store, SessionEngine, JobRunner, Pipeline, routers. `get_client` injectable (tests mount a mock upstream). |
| `config.py` | 268 | Pydantic v2 config models + TOML/env loader. Never raises; `.bak` last-known-good fallback. |

### Hot path
| Module | LOC | Role |
|---|---|---|
| `proxy.py` | 138 | The transparent byte-relay. Catch-all `/{path}` route, sentinel strip, header handling, SSE tee, OpenAI-shaped error fallbacks. |
| `stamps.py` | 117 | Parse & strip the `x-aetherstate-session` header and `<<AETHER:...>>` sentinel. |
| `canon.py` | 103 | Canonicalize `messages[]` → stable transcript core + chained prefix hashes (for L3 identity). |
| `lcp.py` | 125 | Longest-common-prefix index (radix-ish) over branch message chains. |
| `session_engine.py` | 293 | L1 header / L2 sentinel / L3 heuristic session resolution + 4-way turn classification. |
| `pipeline.py` | 279 | Orchestrates hot-path enrichment and cold-path finish. The conductor. |
| `tier0.py` | 230 | Deterministic per-turn rules: OOC/`((aether.*))` command strip, dice, safewords, presence, clock tick. |
| `compose.py` | 237 | Render the state briefing, govern it under the token budget, splice into `messages[]`. |

### State core
| Module | LOC | Role |
|---|---|---|
| `state.py` | 797 | **The heart.** Canonical state dict, op spec + validation, mutation authority matrix, the pure reducer, OOC path translation. |
| `store.py` | 693 | SQLite spine. Schema (DDL), sessions/branches/turns, ops journal + checkpoints, `state_at` replay, memories, lint, director, caps, slices. |

### Cold path — cognition
| Module | LOC | Role |
|---|---|---|
| `jobs.py` | 387 | `JobRunner`: debounce, batching, per-session serialization, extraction scheduling, restart recovery, auto-disable on repeated failure. |
| `extraction.py` | 699 | Tier-1 extraction: capability probe, rung 1–4 ladder (grammar → strict JSON → JSON mode → freeform), parse/repair/salvage. |
| `prompts.py` | 148 | The extraction system prompt, OP CARD, few-shots, repair prompt. Verbatim from planning/04. |
| `genesis.py` | 344 | Two-stage seeding from character card + greeting: rules pass (inline) + LLM pass (cold). |
| `discovery.py` | 115 | Entity discovery — count evidence for unknown names before creating entities. |
| `linter.py` | 392 | Consistency checks L1–L9 (colocation, exposure, contact geometry, inventory, user-voice, timeline, belief leak, consent). |
| `director.py` | 414 | Deterministic beat engine: JSON beat libraries, precondition DSL, binding, cooldowns, note rendering. |
| `memory.py` | 241 | Tiered memory: index, BM25-ish + embedding retrieval, recall rendering, reflection/consolidation. |
| `assist.py` | 242 | Local-model sidecar helpers: chat call, embeddings, memory synthesis, NLI pass. |

### Control plane (HTTP `/aether/*`)
| Module | LOC | Role |
|---|---|---|
| `control.py` | 405 | The Console/extension API: genesis, mode, extraction/cadence, groups, connection setup+test, sessions CRUD, state view, `((aether.set))` PATCH, freeze. |
| `status.py` | 70 | `/aether/status` health + extraction view. |
| `static/console.html` | — | The built-in web dashboard (served at `/aether/console`). |

### Non-Python
| Path | Role |
|---|---|
| `st-extension/index.js` | The SillyTavern Companion extension: stamps identity, captures gen-type, panel + slash commands, writeback loop. |
| `st-extension/manifest.json` | ST extension manifest (registers `aetherstateInterceptor`). |
| `st-extension/style.css` | Panel styling. |
| `src/aetherstate/beats/*.json` | Authored director beat libraries (5 files). |
| `tests/*.py` | 25 test modules — the replay harness + a mock upstream. |

## 5. Where-to-change-what (fast index)

| I want to… | Go to | Also touch |
|---|---|---|
| Add / change a **tracked state field or op kind** | `state.py` (`_SPEC`, `_FAMILY`, `OP_FIELD_ENUMS`, `_apply_op`, `validate_op`) | `extraction.py` (`EXTRACTION_OPS`, `_OP_ALLOWED`, `_OP_FIELDS`), `prompts.py` (OP CARD), `compose.py` (render), `02-data-model.md` |
| Change **who can mutate what** (authority) | `state.py::authority_violation` | `02-data-model.md` authority matrix |
| Add / edit a **director beat** | `beats/*.json` (+ register in `config.DirectorConfig.beat_libraries`) | `director.py` if a new precondition path/op is needed |
| Add a **consistency check** | `linter.py` (add `_lN_*`, wire into `lint_turn`) | `config.LinterConfig.rules_off` docs |
| Add / change a **Tier-0 rule** (dice, OOC cmd, safeword, clock) | `tier0.py` | `state.py` if it emits a new op |
| Add an **extraction rung** or fix parsing | `extraction.py` (`Ladder`, `repair_json`, `scrub_op`, `enum_salvage`) | `06-backend-matrix.md` (planning) |
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
2. `proxy.py` then `pipeline.py` — the 