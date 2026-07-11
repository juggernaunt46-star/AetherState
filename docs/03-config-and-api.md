# AetherState — Config Schema, HTTP API & Extension Contract

Every configuration key (`config.py`), every HTTP route, and the SillyTavern extension's contract
with the proxy.

---

## 1. Configuration

**File:** `aetherstate-data/config.toml` (created on first run; the Console writes it too).
**Precedence:** CLI flags > `AETHERSTATE_SECTION__KEY` env vars > `config.toml` > defaults.
**Safety:** invalid config never blocks startup — falls back to `config.toml.bak` (last-known-good),
then defaults. `Config.source` records which loaded.

### `[server]` — `ServerConfig`
| key | default | meaning |
|---|---|---|
| host | `127.0.0.1` | bind address |
| port | `9130` | bind port |
| cors_origins | `["http://localhost:8000","http://127.0.0.1:8000"]` | allowed CORS origins |
| data_dir | `./aetherstate-data` | DB + config + traces live here |
| log_polling | `false` | 1.9.0: also access-log the extension's hud/status/writeback polling GETs — off by default, they drowned real events at ~1 line/second |

### `[upstream]` — `UpstreamConfig` (the MAIN model that writes the story)
| key | default | meaning |
|---|---|---|
| base_url | `""` | full OpenAI base **including version segment** (e.g. `https://api.openai.com/v1`) |
| api_key | `""` | **fallback only** — the frontend's Authorization header is forwarded as-is and always wins; this key is used only when the frontend sends none (+ Console Connect test) |
| force_rung | `0` | 1–4 forces an extraction rung, skips probing; 0 = auto |
| probe_ttl_days | `7` | capability re-probe interval |
| idle_timeout_s | `0` | 0 = no proxy stream timeout |
| max_parse_mb | `20` | bodies larger than this bypass enrichment (pure passthrough) |
| cache_key | `true` | Phase 0a: add `prompt_cache_key=aether-<session id>` to ENRICHED requests so a conversation's turns route to the same warm provider prompt-cache. Untouched requests stay byte-identical; a client-sent key wins |
| include_usage | `false` | opt-in: set `stream_options.include_usage` on enriched streaming requests so the upstream reports `usage` (cache hits become measurable). Adds one spec-standard SSE chunk the frontend sees |
| prewarm | `false` | opt-in: on the extension's `chat_changed` hint, re-send the session's last enriched prompt once (`max_tokens=1`, non-streaming) so the first real message hits a warm prefix. One full-price prefill per warm; ≥240 s per-session cooldown; in-memory only (a fresh proxy has nothing to prewarm until one request flows). Cache-friendliness note: `[injection].placement="depth"` (default) keeps volatile state at the prompt tail; `"system_merge"` invalidates the provider prefix cache every turn (the proxy logs a notice) |

### `[stamp]` — `StampConfig`
| key | default | meaning |
|---|---|---|
| header_name | `x-aetherstate-session` | L1 identity header |
| sentinel_prefix | `<<AETHER:` | L2 sentinel prefix (must match extension) |

### `[session]` — `SessionConfig`
| key | default | meaning |
|---|---|---|
| min_anchor_msgs | 2 | LCP below this = new session |
| dedup_window_s | 30 | duplicate-request collapse window |
| adopt_min_lcp | 6 | chain evidence needed to adopt an unknown external id |
| align_k | 3 | consecutive content matches to confirm alignment |
| checkpoint_every_turns | 20 | `state_at` checkpoint cadence |

### `[injection]` — `InjectionConfig`
| key | default | meaning |
|---|---|---|
| max_tokens | 1200 | hard cap on the briefing |
| max_fraction | 0.15 | …or this fraction of detected context, whichever is smaller |
| header_floor_tokens | 150 | below floor → header-only; cap ≤ 0 → inject nothing |
| placement | `depth` | `depth \| system_merge \| suffix \| st_native` |
| depth | 3 | messages from the end (for `depth` placement) |
| briefing_style | `verbose` | compression item 2: `compact` = dense briefing notation (here:/St:/Sk:/Ab:/wear:/exp:/rep(...), capped stowed lists, 40-char quest notes) + a one-line `[KEY]` legend on the rpg contract. Opt-in; `verbose` renders byte-identically to 1.11. Item 5 rides regardless of style: explicitly-absent NPCs' physical/effect/drive detail stays ledger-only (bonds always ride), and only the 3 most-recently-touched active quests carry stakes/notes |
| tc_marker | `{{aetherstate}}` | text-completion marker |
| assumed_ctx_tokens | 0 | 0 = unknown → cap = max_tokens |
| priorities | state_header 100, director_note 80, memories 60, relationship_belief 40, lore 20 | drop order (low first) |

### `[consent]` — `ConsentConfig`
| key | default | meaning |
|---|---|---|
| mode | `strict` | `strict \| negotiated \| cnc \| unrestricted` (unrestricted = raw: consent inert for generation) |
| safeword_scan | `user_only` | `user_only \| both` |
| safewords | `[]` | exact-match freeze words; empty = only `((aether.freeze))` |
| guard_escalate_turns | 3 | L9 user-voice escalated-note duration |

### `[user_guard]` — `UserGuardConfig`
| key | default | meaning |
|---|---|---|
| enabled | true | keep the model out of the user's voice |
| mode | `prevent_and_correct` | `prevent \| prevent_and_correct` |
| name | `""` | persona name (empty → stamp/heuristic-resolved) |
| aliases | `[]` | additional persona names |

### `[manual_override]` — `ManualOverrideConfig`
| key | default | meaning |
|---|---|---|
| enabled | false | allow editing organic values (arousal/relationships/drives) |
| allow_ooc_set | true | `((aether.set))` obeys the authority matrix regardless |

### `[drives]` — `DrivesConfig`
| key | default | meaning |
|---|---|---|
| craving_default_ramp | 5 | craving rise per time-advance |
| craving_default_satisfaction | 40 | drop on consume |
| withdrawal_level | 70 | craving level for withdrawal |
| withdrawal_dependency | 50 | dependency for withdrawal |
| dependency_per_consume | 2 | dependency rise per consume |
| inject_threshold | 40 | drives below this stay out of the briefing |

### `[director]` — `DirectorConfig`
| key | default | meaning |
|---|---|---|
| enabled | true | beat engine on/off |
| beat_libraries | core_drama, erp_tension, erp_escalation, erp_aftercare, aftercare_checkin | loaded `beats/*.json` |
| stagnation_ngram | 3 | repetition n-gram size |
| stagnation_threshold | 0.82 | similarity that flags stagnation |
| minutes_per_turn | 3 | Tier-0 clock advance |

### `[linter]` — `LinterConfig`
| key | default | meaning |
|---|---|---|
| enabled | true | run the L1–L11 consistency pass (and L10, the NLI check) at all. **L11** (0b, rpg-only): the DM deciding for the Player outside an open bracketed intent — `[I persuade Jerald.]` opens the one door (and stands L9 down that turn); a bracketed direct quote must survive verbatim. Disable with `rules_off = ["L11"]` |
| rules_off | `[]` | silence specific rules by name, e.g. `["L6"]` for timeline, `["L10"]` for the NLI ledger-contradiction check |
| corrective_notes | true | false = detect + log to the inspector only, **never** steer the next turn (the director's corrective slot stays empty) |
| nli_threshold | 0.85 | L10: minimum contradiction confidence (0.0–1.0) before a corrective note is staged. Local NLI models over-fire on RP prose — keep ~0.85–0.9; a chat-judge (`main`) can go lower. Consulted only when `[assist.groups].linter_nli` = `assist`/`main` |

### `[extraction]` — `ExtractionConfig`
| key | default | meaning |
|---|---|---|
| mode | `main` | `off \| rules \| main \| assist` (shortcut for `[assist.groups].extraction`) |
| lag_turns | 1 | settle-before-extract (swipe protection) |
| debounce_s | 20.0 | idle flush |
| live_recalc | true | 2026-07-07 (Bean): ingest the NEWEST reply on its own cold path — its R9/R10 world-tags commit and Tier-1 extraction flushes the instant the stream ends (was lag-1: state reflected the reply BEFORE the last). Swipes retract only extraction-source ops (`store.retract_extraction_at`) so a resolved check survives. `false` = legacy lag-1. |
| cadence_turns | 1 | extract every N settled turns |
| intake_chars | 12000 | transcript budget per call (newest ships whole) |
| batch_max_turns | 3 | turns per extraction call |
| fail_autodisable_after | 5 | consecutive failures → Tier-1 off for the session |
| fail_reenable_after_turns | 10 | re-enable after this many turns |
| language_hint | `""` | extraction language hint |
| auto_entity_create | true | entity discovery on |
| max_tokens | 600 | extraction reply budget |
| thinking | `auto` | `auto \| on \| off` for reasoning models |
| thinking_max_tokens | 3000 | budget when thinking active |
| trim_op_card | false | drop the OP CARD at schema rungs (budget users) |
| use_anyof | true | per-op anyOf schema at rung 2 where accepted |

### `[memory]` — `MemoryConfig`
| key | default | meaning |
|---|---|---|
| top_k | 4 | memories recalled per turn |
| w_recency / w_importance / w_relevance | 1.0 | score weights |
| recency_decay | 0.995 | recency falloff |
| prefilter_limit | 200 | candidates before scoring |
| reflection_every_scenes | 3 | consolidation cadence |

### `[[assist.endpoints]]` — `AssistEndpointConfig` (the HELPER model)
| key | default | meaning |
|---|---|---|
| name | `local` | label |
| base_url / api_key / model | `""` | the assist endpoint |
| tier | `small` | `nano \| small \| medium` (prompt-size preset) |
| max_concurrent | 1 | serialize weak machines |

### `[assist.groups]` — `AssistGroupsConfig` (per-feature `off\|rules\|main\|assist`, live-toggleable)
| group | default | |
|---|---|---|
| extraction | `""` | unset → `[extraction].mode` is authoritative |
| director_selection | `rules` | |
| linter_nli | `rules` | **L10 prose-vs-ledger contradiction check.** `rules`/`off` = L1–L9 only, no model, byte-identical to 1.0 (default). `assist` = a **local** MiniCheck / 3-way NLI model behind an OpenAI-compatible shim (recommended). `main` = a big judge — point it at a **different** endpoint than the narrator (self-judging inflates scores). Both fire ONLY on contradiction of a committed fact, never on new detail you author |
| memory_reflection | `rules` | |
| embeddings | `off` | |
| lore_gen | `off` | |

### `[assist.group_endpoints]` — `AssistGroupEndpointsConfig` (optional per-group endpoint override, 2026-07-08)
Each key is a group name; its value is the **name of an `[[assist.endpoints]]`** that group should use when
its mode is `assist`. Empty / omitted → the first endpoint (`endpoints[0]`) — today's behaviour, so an
all-empty table is **byte-identical to 1.0**; an unknown name fails open to `endpoints[0]`. Lets e.g.
`linter_nli` hit a LOCAL NLI box while `memory_reflection` hits a cloud chat model — different assist
endpoints at once (also honoured by extraction's own routing). Editable live in the Console
(Connection → **Assist routing**) and the SillyTavern panel; persisted on change.

Routes: `POST /aether/groups` now also accepts `"group_endpoints": {<group>: <endpoint name>}` alongside the
`{<group>: <mode>}` pairs (persisted; a blank/unknown name clears the override). `POST /aether/assist/endpoints`
`{ "endpoints": [ {name, base_url, model?, tier?, api_key?, max_concurrent?} ] }` replaces the endpoint list
(a blank `api_key` keeps a stored key; never echoed back). `GET /aether/status` `.extraction` exposes
`groups`, `group_endpoints`, and `assist_endpoints` (name/model/tier/base_url) for the UIs.

### `[privacy]` — `PrivacyConfig`
| key | default | meaning |
|---|---|---|
| trace_level | `meta` | `off \| meta \| full` (full contains prose) |
| trace_ring | 200 | trace ring-buffer size |
| log_prose | false | redact prose from logs |
| backups_keep | 3 | DB backups retained |

> **There is no telemetry key.** It does not exist to be enabled.

---

## 2. HTTP API

### OpenAI surface (relay — `proxy.py`)
`ANY /{path}` — the transparent proxy. Frontends call `/v1/chat/completions`, `/v1/completions`,
`/v1/models`, etc. `/v1` is stripped and grafted onto `upstream.base_url`. `x-aetherstate-*` request
headers are consumed, never forwarded. Errors are OpenAI-shaped JSON (502 `not_configured` /
`upstream_unreachable`).

### Control surface (`/aether/*` — `control.py` + `status.py`)
| Method + path | Purpose |
|---|---|
| `GET /aether/status` | version, mode, extraction view, counts (the panel chip + `/aether-status` read this); `.cache` (Phase 0a) carries the prompt-cache knobs + whole-run hit rates (`requests`, `with_usage`, `hits`, `prompt_tokens`, `cached_tokens`, `hit_rate_tokens`, `prewarms_sent`, per-session last numbers) — the Console Status tab renders it as the "prompt cache" row |
| `GET /aether/console` | serves the built-in dashboard HTML |
| `GET /aether/override` · `POST /aether/override` | read / set `manual_override.enabled` |
| `POST /aether/session/{sid}/genesis?force=0&ifearly=0` | seed from card/greeting (body: card, greeting, speaker, user, opening); `ifearly=1` acts only while the session has no real exchange (greeting swipes) |
| `POST /aether/hint` | fire-and-forget UI hint (event, session, messageIndex); a `chat_changed` hint also triggers the opt-in prompt-cache prewarm (`[upstream].prewarm`) |
| `POST /aether/session/{sid}/mode` | set `enriched \| passthrough` for the chat |
| `GET /aether/session/{sid}/writeback?cursor=N` | poll native-writeback patches (chatMetadata) |
| `GET /aether/extraction` · `POST /aether/extraction` | read / set cadence_turns, intake_chars (persisted) |
| `POST /aether/groups` | live-toggle an assist group |
| `GET /aether/connection` | current endpoint config |
| `POST /aether/connection/models` | list models + real auth test for a base_url/key |
| `POST /aether/connection` | save an endpoint (target: upstream \| assist) |
| `GET /aether/sessions` | list sessions, newest first; each row also carries `world_name` + `player_name` from committed state so the Creator's session picker is legible (2026-07-08 — cryptic st-ids gave no clue which world a session was) |
| `POST /aether/session/{sid}/label` | rename a session |
| `DELETE /aether/session/{sid}` | delete a session |
| `GET /aether/session/{sid}/state` | inspector "Now" view (`state_summary`) |
| `PATCH /aether/session/{sid}/state` | `((aether.set))`-style edit (body: path, value) under authority |
| `POST /aether/session/{sid}/freeze` · `POST .../unfreeze` | freeze / resume |
| `GET /aether/creator` | serves the World & Character creator window (`static/creator.html`) |
| `GET /aether/registry` | curated stats/skills/abilities + genres/times for the creator sheet, plus `genre_packs` (genre-true preset floor: per-genre skills/abilities that hide fantasy-flavored entries and freeze into per-character defs at save) and `concept_hints` |
| `GET /aether/creator/models` | model ids detected at the configured endpoints (feeds the Creator's model menu; fail-open to `[]`) |
| `GET /aether/session/{sid}/creator` | prefill: current Player Card + `player_name`, committed `world` doc (best-effort reverse of the seed ops), `world_seeded`, spec, persona |
| `POST /aether/session/{sid}/author` | assist-LLM fill-the-blanks (body: mode `world\|player`, doc, world, optional `model`, optional `offline:1` for an explicit template fill); works session-less from config; auto-detects a model via `GET /models` when none is known; an LLM failure returns `source:"error"` + `detail` (the window keeps the form untouched — templates only on request) |
| `GET /aether/presets` · `POST /aether/presets` | list / upsert named creator presets (kind `world\|player`, name, doc) |
| `GET /aether/presets/{id}` · `DELETE /aether/presets/{id}` | fetch / delete one preset |
| `GET /aether/session/{sid}/briefing` | 1.9.0 transparency inspector: EXACTLY what the engine would inject into the next request — the composed state header, the DM rules-contract under rpg, per-component token counts after the budget governor, and the injection budget. Read-only ("the console shouldn't hide anything raw") |
| `GET /aether/session/{sid}/journal?limit=N` | RPG-4 inspector feed: applied-op tail (turn · source · op · brief fields) + last rolls — the Console's "Recent activity (RPG)" card |
| `GET /aether/session/{sid}/search?q=&limit=N` | RPG-5: search the session's memory/summary ledger with the composite recall scorer (lexical + importance + recency; embeddings when staged). Read-only, fail-open to `[]` |
| `POST /aether/session/{sid}/world` | persist a world doc as shipped ops (entities/lore/scene); creator-first: an unknown `sid` mints the session by external id (2026-07-06 — the row the relay adopts on the chat's first stamped message); response carries `session_id` |
| `POST /aether/session/{sid}/player` | persist a Player Card (`entity_add`+`player_seed`+attrs); same creator-first session-minting as the world route |
| `GET /aether/session/{sid}/narrator-card.png` | download a **world-specific Narrator card** (PNG with the V2 chara card embedded — ST import format), built by `narrator.build_card` from the committed world (`creator.world_from_state`) + Player Card. Read-only ledger projection; a `none` session is unaffected (control route, off the relay) |
| `GET /aether/session/{sid}/narrator-card.json` | the same card as JSON (inspect / manual import) |
| `POST /aether/session/{sid}/narrator-card` | build the card from the committed world and, when `[specialization].narrator_card_dir` is a real directory, install the PNG there (so it shows in SillyTavern's character list); returns `{name, world, bytes, installed, filename, tags, png_b64, download}`. Best-effort, fail-open |
| `POST /aether/narrator-card` | **session-free** card build (2026-07-08): body `{world, player}` (the Creator form) — builds straight from posted docs, no committed session needed, installs to `narrator_card_dir` when set, returns `{name, world, bytes, installed, filename, tags, seeded_world, seeded_player, png_b64}`. The card embeds a structured `seed` in `extensions.aetherstate.seed` (the whole world + Player Card) |
| `POST /aether/session/{sid}/seed` | **idempotent auto-seed** (2026-07-08): body `{seed:{world, player}}` — commits the world only when none is seeded and the Player Card only when none exists (re-opening an established chat never clobbers progress). The ST extension replays a Narrator card's embedded seed here on chat-open, so a fresh chat with a Creator-built card already has the world + character in its ledger — no re-applying. Mints the session on an unknown `sid`; deterministic (no LLM — weak-model floor); privileged `source='user'` via `world_to_ops`/`player_to_ops` |

| `GET /aether/session/{sid}/hud` | the **resolved player-facing HUD payload** (`hud.hud_view`): scene, player card(s) with EFFECTIVE skill mods + resolved abilities + appearance, statuses/conditions, drives, gear (worn) + inventory (carried), quests, dice rolls/checks, relations/factions. Registry math done server-side; the ONE source both the SillyTavern HUD and the Console "Player" tab render. Read-only, fail-open |

The **player HUD** (2026-07-07, `hud.py` + the ST extension window + the Console "Player" tab) closes the "AetherState hides everything from the human" gap. AetherState computes a rich player state and injects it into the MODEL as bracketed blocks — the HUD surfaces that same truth to the PLAYER. `hud.hud_view(state, cfg)` is the single resolved projection; `GET .../hud` serves it; the SillyTavern extension renders it in a movable, themeable (`neutral`/`fantasy`/`scifi`/`modern`) window (launcher tab, `/aether-hud`, or the panel link), and the Console renders the identical payload in its **Player** tab (now the default) — so nothing player-facing lives only in ST or only in `raw`. The active specialization (`rpg`/`none`) is shown in the HUD header, the panel chip, and the status chip, and the panel has a narrative-mode selector. **Player appearance/description** is a new field (Creator form → `set_attribute appearance` on the player entity → HUD/Console/Narrator card); it did not exist before (only NPCs had descriptions). The HUD and the Console "Player" tab are also EDITABLE (HUD via an ✎ edit-mode toggle): HP ±  (`hp_adj`), spend a banked stat point (the new privileged `stat_spend {char, stat}` op — +1 to a stat clamped to the registry max, −1 point), equip/unequip (`item_equip`/`item_unequip`), use (`item_consume`), remove a status (`effect_remove`), and sate/± drives (`craving`/`obsession`, override-gated like all organic edits). All buttons build ops and go through the privileged `PATCH /state` path. The HUD also has a **▁ compact/minimize** mode. `/hud` carries the ids these controls need (item `iid`, effect `key`, obsession `target_kind`, item `slot`/`consumable`). The payload is COMPREHENSIVE — not just the player: effect rows carry `kind`/`kind_label` (**Status / Condition / Disease**) + `note` + `mods`; a **`cast`** array surfaces every tracked non-player entity (presence, location, mood, their statuses/conditions/diseases, drives, goals, worn/exposed, relationship tier + dims to the player); plus player `mood` + skill `mastery`, `relationships` (dims), `memories` (recent events), `consent`, and world flags/factions/affinity. Both the HUD and the Console "Player" tab render all of it (statuses shown even when empty), so nothing tracked stays hidden.

The **world Narrator card** (2026-07-07, `narrator.py`) closes the "hard to see which world I'm in" gap: the card is NAMED after the world, its first message opens on the committed opening scene, its description carries the setting/laws/factions/places/cast + the Player, and its avatar is genre-tinted. The Creator's "🎭 Generate Narrator card" button (Session review tab / Character tab) POSTs the install route then downloads the PNG. It projects `world_from_state`, so a session that has accumulated more than one world's lore will surface all of it — generate from a single-world (or fresh) session. This is additive to the world-agnostic `build_narrator_card.py` at the repo root (still the floor card for "no world yet").

Config-mutating routes persist to `config.toml` (`_persist_config`). State-mutating routes go through
`state.apply_delta` / `translate_path` — never write state directly.

---

## 3. SillyTavern extension contract (`st-extension/`)

**Install:** `Install-ST-Extension.bat`, or copy `st-extension/` to
`SillyTavern/data/default-user/extensions/AetherState`, restart ST, hard-refresh (Ctrl+Shift+R).

**manifest.json** registers `generate_interceptor: "aetherstateInterceptor"`, `loading_order: 100`,
`minimum_client_version: "1.12.0"`.

**Identity the proxy relies on:**
- **L1 header** `x-aetherstate-session: <sid>` in `custom_include_headers` (Custom source only).
- **L2 sentinel** `<<AETHER:v=1;session=<sid>;turn=<n>;type=<gentype>;speaker=<name>;user=<persona>>>`
  injected as a system message on `CHAT_COMPLETION_PROMPT_READY` (all Chat Completion sources),
  never on dry runs. **The proxy strips this before forwarding upstream** (`stamps.py`).
- `sid` = per-chat `chatMetadata.aetherstate_sid`. Sentinel wins over a stale header on mismatch.

**Events consumed:** `CHAT_CHANGED` (reset turn counter, stamp header, genesis at open),
`GENERATION_STARTED` / interceptor (capture gen type, bump turn counter),
`MESSAGE_SWIPED/EDITED/DELETED` (hints), `MESSAGE_RECEIVED` (chip refresh).

**Routes the extension calls:** `/aether/status`, `/aether/session/{sid}/genesis|mode|freeze|
unfreeze|writeback|state`, `/aether/extraction`, `/aether/groups`, `/aether/override`, `/aether/hint`.

**Slash commands:** `/aether-status`, `/aether-freeze`, `/aether-resume`, `/aether-set <path> <value>`,
`/aether-mode enriched|passthrough`, `/aether-genesis`, `/aether-cadence <1-50>`.

**Fail-open guarantee:** every fetch has a short timeout and swallows errors; if the proxy is down or
`index.js` throws, SillyTavern works untouched.

> **Contract invariant:** the sentinel format here and `stamps.py` must stay identical, and the
> panel/slash reads must match `status.py`/`control.py` response shapes. Changing one side alone
> breaks identity or the UI silently.

## 4. RPG specialization (v0.2, phases RPG-0…RPG-2)

`[specialization].name = "none"` (default) is inert — byte-identical to pre-RPG. `"rpg"` turns
the character card into a Dungeon Master and tracks the user's persona as a Player Card
(`02 §7`).

### 4.1 `[specialization]` — `SpecializationConfig`

| key | default | meaning |
|---|---|---|
| `name` | `"none"` | `none` \| `rpg` |
| `blocks` | `[PLAYER,EFFECTS,GEAR,INVENTORY,FACTIONS,RELATIONS,NEARBY,QUEST,WORLD,DIRECTIVE]` | header blocks the profile may render (consulted only under `rpg`). `NEARBY` (0b): notables whose Creator-authored `home` anchor matches the scene's location but who are NOT on scene — with the knows-player gate inline (`stranger` / `by reputation (Faction: tier)` / affinity tier); anchored-elsewhere notables spend zero tokens. `[RELATIONS]` adds the same gate line for PRESENT NPCs with no relationship row (anti-main-character) |
| `dm_guard` | `true` | Dungeon-Master framing of the Q12 guard |
| `dice` | `"2d6"` | resolution dice knob (consumed from RPG-1) |
| `tiers` | `"pbta3"` | resolution tier model (consumed from RPG-1) |
| `nemesis_enabled` | `false` | RPG-3b: gates the `one_nemesis` linter rule (D6; the op itself always exists) |
| `faction_cascade` | `0.1` | RPG-3b: NPC→faction affinity ripple factor (negatives halved; `0` disables) |
| `contract` | `"full"` | RPG-4 (D7): DM rules-contract size — `"compact"` (~40 tokens, same non-negotiables) for weak/local model budgets |
| `auto_compact_contract` | `false` | A1 (1.19.0, Bean): on calm, ESTABLISHED turns auto-flip the contract to its `"compact"` form (the model has internalized the full rules by then — the biggest per-turn token + reasoning cut). The FULL contract still rides the first `contract_full_turns` turns and EVERY combat turn (tracked combatants on the field OR a climax/combat/battle/fight/ambush scene phase). Opt-in: off = the size is fixed by `contract`, so an rpg session is byte-identical until enabled. Ignored when `contract == "compact"`. Pure state read on the hot path — no network, replay-neutral |
| `contract_full_turns` | `3` | A1 (1.19.0): keep the FULL contract for this many opening turns before `auto_compact_contract` may kick in (`0` = compact-eligible from turn 1) |
| `hardcore` | `false` | RPG-5 (doc 10 §7): `defeat_resolve` routes to DEATH (permadeath) instead of the contextual non-lethal outcomes (captured / wake safe / robbed / rescued) |
| `auto_dm_checks` | `true` | R8b (1.9.0): a `((aether.check …))` the DM calls in its OWN reply ARMS — a plain-prose player answer rolls it automatically next request (the player's explicit/NL checks always win; the [DIRECTIVE] marks it DM-called) |
| `enemy_rolls` | `true` | R8c (1.9.0, Bean): hostile beats pre-roll ONE enemy-action die and inject it as `[OPPOSITION]` alongside the [DIRECTIVE] — foes attack on real dice the DM must narrate, never wave through. Arms when a Cold-or-worse NPC is present, scene phase is climax/combat/battle/fight/ambush, or a combat-ish world flag is truthy. Deterministic per (turn·scene·player) — replay re-renders the same die |
| `war_room` | `true` | Phase 1 (1.13.0, plan doc 13 ratified): the full combat loop — combatant INSTANCES (extras via the DM's `[foe \| name \| tier \| weapon]` tag / `((aether.foe …))`; known NPCs fight tracked and KEEP their wounds via `attributes.hp` + a `Wounded`/`Battered` condition), 3v3 cap with auto-enlisted companions (1.20.0: grounded on a BOND - soulmate / companion role/label / close relationship dim, not just the rarely-reached Ally-tier affinity) plus the DM's `[ally \| name \| tier? \| weapon?]` tag and the player's `((aether.ally name))`, each fighting on a per-ally pre-rolled `[ALLY]` die, code-derived player strike damage (outcome tier × weapon `damage` mod, applied to the ledger, handed to the DM on the [DIRECTIVE]), the clamped `[hp \| <combatant> \| -N]` chip-damage channel, code-detected defeat → XP by threat tier + a loot roll frozen at spawn (`state["loot"]` via `loot_table` ops > `registry/loot.toml`), self-ending fights, the exact-HP `[WAR]` board, the `[clash \| A vs B \| how \| outcome]` NPC-fight record, the `combatant_alive` linter rule, and the War Room HUD lane. Off = pre-1.13 combat (R8c + `[hp]` only) |

| `large_battle` | `true` | §F (1.21.0): large-scale battle — the Player fights their MICRO 3v3 slice on the dice while the MACRO battle (army-on-army, the rest of the field) lives in PROSE. The DM opens one with `[battle \| <name> \| <foe?> \| <tier?>]` and reports it with `[tide \| winning\|holding\|losing \| why]` (clamped one step/turn); a code-owned momentum is the tide, and the `state.battle_ops` referee sends fresh WAVES into the War Room while it isn't won (`battle_wave`, capped) — settling to victory once the tide turns. The `[BATTLE]` tail directive + a HUD battle chip surface it; OOC `((aether.battle <name> \| tide <t> \| end))`. Off = no `[battle]`/`[tide]`/waves (needs `war_room`) |
| `living_world` | `true` | Phase 2 (1.14.0, plan doc 13 ratified): the living world — travel between canon locations consumes day-segments (`route_set` edges override the 1-segment default), an idle clock floor (`clock_turns`), the DM's `[time \| <segment>/+N]` ceiling (clamped), authored faction FRONTS (`front_add`, 3-12 segments) advanced deterministically by `state.world_ops` on both apply paths, FILL → `world_flag` + world-event memory + the `[FRONT]` tail directive + the `front_fallout` beat, and rumor-gated visibility (`front_reveal` via `[rumor]` tag / name-mention; HUD/briefing hide unrumored clocks, `state_summary.fronts`/`routes` always raw). Off = 1.13 behavior |
| `clock_turns` | `6` | Phase 2: idle auto-tick — advance one time segment after this many turns with no real time passing (0 disables the idle floor; travel and `[time]` tags still move the clock) |

**Overlay resolver (`config._apply_specialization`).** When `name == "rpg"` the built-in
`RPG_PROFILE` is deep-merged UNDER the user's config, so precedence is **user-override >
profile > base-default**. RPG-0's profile contributes `[injection].priorities` (adds the
`directive`/`player_card`/`quest`/… header-class ranks). A `none` load never touches the
profile. Applied in `load_config` on both the file and defaults paths; `name` may also be set
via `AETHERSTATE_SPECIALIZATION__NAME`.

### 4.2 Control route (`control.py`)

- `GET /aether/specialization` → `{name, blocks, dm_guard, dice, tiers}`.
- `POST /aether/specialization {"name":"none"|"rpg"}` → sets it live (compose reads `cfg` per
  request, so rendering + the DM guard change immediately) and persists `[specialization].name`
  to `config.toml` so the profile overlay re-applies on next load. `422` on an unknown name.
- `/aether/status` also reports the active `specialization`.

### 4.3 Extension slash (`st-extension/index.js`)

- `/aether-spec [none|rpg]` → POSTs the route; with no argument, reports the current mode.


### 4.4 RPG-1 — mechanics, resolution & the rules contract (phase RPG-1)

**Curated registry.** `src/aetherstate/registry/*.toml` (`meta`/`stats`/`skills`/`abilities`),
loaded once and cached (`registry.load(cfg)`); a user extends/overrides via
`<data_dir>/registry/*.toml` (per-table merge, like beat libraries). `meta.dice`/`meta.tiers` are
DEFAULTS; the `[specialization].dice`/`.tiers` knobs win under `rpg` (D1).

**OOC command (`tier0.py` R8).** `((aether.check <skill> [+N|-N] [vs DC | dc DC] [scope
minor..mythic] [use <ability>]))` — resolves an explicit skill-check on the hot path and injects the
`[DIRECTIVE]` the SAME turn. `use <ability>` invokes an ACTIVE dice-shaping ability (surge /
extra_die / reroll); passive shapers (edge / ward) apply on their own. Stripped from the forwarded
message like every OOC span. Unknown skill → a visible notice, no op (nothing freestyle).

**Player HUD (`GET /aether/session/{sid}/hud`; `hud.py`).** The one resolved player-facing payload,
rendered by the ST extension HUD (now **tabbed**: Char · Skills · Abilities · Gear · Status · World,
with the dice rules on Skills, abilities grouped Spells/Techniques/Talents with each mechanic spelled
out, and Gear as a paper-doll of equip slots) and the Console "Player" tab. Read-only, off the relay
— a `none` session's wire is untouched. The extension hides the DM's raw `[tag | …]` lines from the
reader (display-only; `settings.hud.hideTags`), never altering the message the proxy parses.

**Header blocks (`compose.py`).** `[DIRECTIVE]` (per-turn resolved outcome; rides the never-dropped
header) and a droppable `[RULES]` DM rules-contract (`rules_contract` component; `RPG_PROFILE`
priority 46). Both gated by `specialization=rpg`; `[DIRECTIVE]` also needs `DIRECTIVE` in
`[specialization].blocks`.

**Linter.** `outcome_match` (new `rules_off` code) enforces narration ↔ pre-decided tier; `med`
→ `high` on repeat; inert in non-RPG/flashback scenes. No new `[linter]` keys — it rides `enabled`
and `rules_off`.


### 4.5 World Generator & Character Creator (doc 09; `creator.py` + `static/creator.html`)

A standalone window served same-origin by the proxy — opened from the ST panel (`open Creator`) or
`/aether-creator`. **World-first ordering** (doc 09 §5): author the World, then the Character against
it. Two authoring paths, always available together:

- **Deterministic backbone** — genre templates + point-buy + the curated registry fill every blank
  and clamp every value. Consistent + calculable; runs with no model.
- **Assist-LLM fill-the-blanks** (`POST .../author`, cold-path, creation-time) — the player supplies
  the details they care about; a capable assist model authors the rest (world nations/NPCs/lore, or a
  character's stats/skills/class), which is then parsed, validated + clamped, and FROZEN. Freestyle
  skills/abilities land as per-character `defs` snapshots (fixed numbers) via `player_seed`, so nothing
  is freestyle at resolution (registry invariant). Fail-open: no assist endpoint ⇒ deterministic fill.
  **Model resolution (2026-07-06):** the route takes an optional `model` (the Creator's header menu,
  populated from `GET /aether/creator/models`); with no pick it uses the session's last-seen model,
  and on a fresh session it detects one at the endpoint via `GET /models` — the Creator no longer
  needs a chat message to have flowed before AI fill works, and no session row is required (it
  builds the endpoint from `[assist]`/`[upstream]` config). Both tabs carry a free-form
  **creative direction / notes** field passed to the authoring prompts as high-priority direction.
  Authoring clamps are roomy rails, not straitjackets: 2000-char prose fields, 20-item lists,
  custom passive mods ±5 / resolution mods −5..+8 (still clamped, still frozen at creation).
  **Quality pass (2026-07-06):** authoring calls run at temperature 0.9 with a 150 s timeout and a
  4000-token budget (the shared 25 s mechanics timeout used to expire mid-generation and silently
  fall back to templates); EVERY filled field — setting of any length, NPCs, opening scene/quest,
  the world's aspects/locations for character fill — rides along as canon context; an LLM failure
  is reported (`source:"error"`) instead of silently substituting templates. The window also gained
  a **session switcher** (apply a world/char to any session), named **presets** (save/load/delete
  world + character docs across sessions), and a **📋 Session review tab** that renders the
  committed Player Card + world (via the prefill route's `world`/`player_name`) with
  load-into-form buttons.

**Persistence maps onto SHIPPED primitives only** — no new op vocabulary, no new storage families.
World → `memory_event` (setting/lore/date/quest) + `entity_add`/`set_attribute` (factions/locations/
NPCs) + `scene_set` + `time_advance`. Character → `entity_add` + `player_seed` (+ `set_attribute` for
species/sex/class). All applied with `source='user'` (privileged). Creator writes land one turn past
the latest checkpoint (`_next_turn`) so they survive the genesis-checkpoint shadow and stay visible to
`current_state`. Everything is inert under `specialization=none` (the ops render nothing without RPG
mode); the window offers a one-click switch to RPG.

### 4.6 RPG-2 — Inventory & Gear (items)

Under `rpg`, two new header blocks render for the Player Card (gated by the profile `blocks`
list): `[GEAR]` (worn slots → `slot=Name(mods)[capacity]`, e.g. `head=Iron Helm(armor+1) ·
waist=Utility Belt[4]`) and `[INVENTORY]` (carried instances grouped by container, `loose`
last, `3× Healing Draught`). Both read only baked per-instance snapshots — pure state, µs.

**Extraction wire (rpg-gated).** The five proposable item ops ride the extraction schema + a new
`RPG ITEM OPS` card ONLY when `specialization=rpg` — a `none` session's wire schema, OP CARD, and
forwarded bytes stay byte-identical to 1.0 (each variant is its own stable schema string, so
hosted compilers cache both once). `item_mint` never appears on the wire.

**Item ops via the control API.** `PATCH /aether/session/{sid}/state` accepts the `item_*` ops
(`source='user'`, privileged minting): mint from a `registry/items.toml` template, equip into the
16 built-in slots (extend via `meta.toml extra_slots`), transfer with full rollback on a full
container. Rejections come back in `rejected[]` with the transactional reason.

**Registry export.** `GET /aether/registry` now also returns `items` (the template table) and
`slots` (the effective slot vocabulary) for the creator/inspector surfaces.

### 4.7 RPG-3 — Statuses & Conditions + the eligibility gate

Under `rpg`, the `[EFFECTS]` header block (profile `blocks` list; default on) renders the ledger
of active effects for the player and every tracked character carrying one:
`[EFFECTS] Kael: Bleeding(-)[6t], Blessed(+) · Mira: Pregnant(~)` — valence glyph `(-)/(~)/(+)`,
stacks `×N`, remaining turns `[Nt]`. The `[RULES]` contract gains a `[TAGS]` section teaching the
inline tag protocol (`[status gained | <char> | <Name> | <valence>]` …) plus a compact preset
slice — re-sent with the contract every request, so context rollovers can't lose the protocol.

**Presets.** `registry/effects.toml` (user-overridable via `<data_dir>/registry/effects.toml`):
per-entry `name/kind/valence/mods/duration/requires/desc`. `GET /aether/registry` now also
returns `effects`.

**OOC / R8.** `((aether.check <skill> [+N|-N] [vs DC] [scope minor|standard|major|epic|mythic]))`
— `scope` engages scope-gated power (doc 10): −2 per scope step past the skill's rank + a tier
ceiling (floor: partial). A skill with `requires_ability` (e.g. the shipped `spellcraft` →
`arcane_gift`) is a NON-MOVE without the ability: visible notice, no roll.

**Extraction wire (rpg-gated).** `effect_add`/`effect_remove`/`effect_update` ride the extraction
schema + a new `RPG EFFECT OPS` card ONLY under `specialization=rpg`; a `none` session's wire
stays byte-identical to 1.0. `effect_add.mods` is not a wire field (scrubbed — the model never
authors mechanics); `ability_grant` is privileged and never on the wire. The anyOf schema keeps
`mood.valence` integer-typed while effect branches take the string vocabulary.

**Control API.** Effect ops + `ability_grant` flow through `PATCH /aether/session/{sid}/state`
(`source='user'`); rejections come back in `rejected[]` with the transactional reason (e.g. the
data-driven `requires = "female"` gate). `state_summary` (the state window / Console) exposes
`effects`; the Console renders per-character effect chips (valence-colored, duration/note tooltip,
one-click remove).

### 4.8 RPG-3b — the social plane (affinity, factions, bonds, world flags)

Under `rpg`, three new header blocks render when their data exists (profile `blocks` list;
default on): `[FACTIONS] Iron Covenant: Ally (at_war=yes)` (affinity TIER label + standing
circumstances), `[RELATIONS] Mira: Warm · Seraphine: Devoted ♥soulmate` (present NPCs + all
bonded characters; demoted bonds show their label), `[WORLD] plague=spreading` (global flags).
Tier labels derive from the clamped ledger sum (`02 §7.8`) — the integer never rides the header.

**OOC set-paths (rpg-gated; unknown under `none` → the usual visible reject).**
`((aether.set world.<key> <value>))` — `true`/`false`/integers coerce, `none|null|clear|-`
clears; `((aether.set affinity.<name> <±N>))` — organic, so manual_override-gated; the ±15
per-turn clamp still applies; `((aether.set player.soulmate <name|none>))` and
`((aether.set player.nemesis <name|none>))` — the privileged bond pointers.

**Extraction wire (rpg-gated).** `affinity_adj{target,delta,reason}` +
`world_flag{key,value,faction?}` ride the extraction schemas + a new `RPG SOCIAL OPS` card ONLY
under `specialization=rpg`; a `none` session's wire stays byte-identical to 1.0. Affinity `kind`
is derived engine-side and scrubbed off the wire; `set_soulmate`/`set_nemesis` are privileged and
never on it. The deterministic faction cascade (`[specialization].faction_cascade`) runs on the
cold path after each extraction batch and lands as journaled rule-source `affinity_adj` ops.

**Control API / Console.** All four ops flow through `PATCH /aether/session/{sid}/state`
(`source='user'`; affinity edits need manual override ON). `state_summary` exposes `affinity`
(each record with its derived `tier` + trimmed ledger tail), `factions`, and `world`; the Console
Overview gains a **Relations (RPG)** card (tier pill, bond flags, demote labels, ±5 nudge
buttons, latest ledger reason) and a **Factions & world** card (circumstance chips + world flags
with one-click clear); the Edit tab gains an **RPG** group (Affinity ±, World flag,
Soulmate, Nemesis — empty selections send `null` to clear).

**Linter.** `one_soulmate` (new `rules_off` code): structural uniqueness (`high`, advisory),
Devoted-eligibility (`med`, corrective note), referential integrity (`med`), and a conservative
off-book-promotion prose arm. `one_nemesis` is the same machinery, registered only when
`[specialization].nemesis_enabled` (D6).

---

## L10 — NLI ledger-contradiction check (2026-07-08)

The one systematic **prose-vs-ledger** guard — wiring the previously advisory-only `linter_nli`
dial to real teeth. L1–L9 verify structured state and a few hand-written prose patterns; **L10**
asks a grounding model whether the narrator's prose FLATLY CONTRADICTS a committed ledger fact and
turns each hit into a next-turn corrective note through the existing `director.best_corrective`
slot (it never rewrites the current reply). Runs in **both** chat and RPG sessions.

**The one semantic that matters:** it fires ONLY on **contradiction** (prose asserts a committed
fact is false). Prose that merely adds detail the ledger doesn't cover is NEW FICTION, not error —
left untouched (*freedom of fiction, constraint on fact*). The contradiction-only prompt plus the
`nli_threshold` score are the two filters.

**Premises** = the turn's scoped ledger slice serialized to short declarative sentences: base
`facts` and the RPG `effects`/`hp`/`items`/`quests` when present (presence and clothing/poses/
contacts stay out — the deterministic L1–L5 rules already own them). **Hypotheses** = the reply
split on sentence boundaries (no LLM decomposition). Bounded (≤24 premises × ≤40 hypotheses) so the
cold path stays cheap.

**Model/endpoint.** `assist` = a **local** MiniCheck / 3-way NLI model behind an OpenAI-compatible
shim (runs on a 4070 or CPU; never touches the narrator backend). `main` = a big model as judge —
**point it at a different endpoint than the narrator**; self-judging inflates scores. Both speak the
same `assist._chat` path: `assist.nli_pass(get_client, cfg, ep, premises, hypotheses) -> hits`.

**Invariants.** Cold-path only (post-`[DONE]`), fully **fail-open** (any error → no note, stream
untouched), **note-only** (freedom is routed, never blocked), and **journals no state** (replay-pure).
Default `linter_nli="rules"` runs no model and is **byte-identical** to 1.0. Off-switch:
`[linter].rules_off = ["L10"]`; knob `[linter].nli_threshold` (default 0.85). Single-turn detection +
the existing 3-turn lint cooldown (a persisting contradiction nags once, not every turn). Tests:
`tests/test_p8_nli_l10.py` (+ `nli_pass` units in `tests/test_p4_assist.py`).

### L10 precision notes

A raw 3-way NLI classifier over the premise×hypothesis matrix over-fires on loosely-related RP
prose, so this check is tuned for precision over recall (a false corrective note damages the RP; a
missed one costs nothing). Three guards:

- **Only durable keys are premises.** `_ledger_premises` emits base facts + the RPG
  effects/hp/items/quests. Presence and clothing/poses/contacts stay out — they change turn to turn
  and the deterministic L1–L5 rules already own them.
- **Threshold.** `[linter].nli_threshold` defaults to `0.85`. Real contradictions score ~0.99, so
  0.85–0.9 keeps them while dropping noise. Tune per model; a `main` chat-judge can sit lower.
- **Shared-subject gate.** A good NLI endpoint scores only a (fact, claim) pair that shares a
  subject word — a contradiction is ABOUT the same thing. The bundled shim (`nli-shim/`) does this.

**Known limit (surface NLI).** An accumulating fact ledger can hold stale/transient facts; a surface
NLI model may flag later prose as "contradicting" a fact that was true earlier but no longer applies.
The floor/ceiling answer: the local NLI is a fast, private FLOOR; for higher precision point
`linter_nli="main"` at a big judge on a **separate** endpoint (it reads context and currency far
better than surface NLI). A ready-to-run local shim ships in `nli-shim/` — see its README.
