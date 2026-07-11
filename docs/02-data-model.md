# AetherState — Data Model, Op Vocabulary & DB Schema

The canonical state (`state.py`), the op vocabulary that mutates it, the mutation-authority
matrix, and the SQLite schema (`store.py`). This is the reference a skill consults before touching
state.

---

## 1. The canonical state dict

State is **one JSON dict** per branch — the currency of checkpoints and `state_at` replay. Pydantic
models in `extraction.py` define the *wire/extraction* schema; the reducer in `state.py` owns the
*storage* shape. `empty_state()`:

```python
{
  "schema": "aetherstate/1",
  "entities":   {},   # eid -> {kind, name, aliases[], location_id, present}
  "chars":      {},   # eid -> {affect, arousal, goals[], secrets[], status_effects[],
                      #         obsessions{}, cravings{}}
  "attributes": {},   # eid -> {key: value}   (free-form set_attribute store)
  "clothing":   {},   # eid -> {item: {state, covers[], slot, layer, category?, updated_turn}}
  "poses":      {},   # eid -> {base, anchor, detail}
  "contacts":   {},   # "from|from_part|to|to_part|type" -> {..., intensity, object, started_turn}
  "consent":    {},   # "subject|partner|category" -> {level, max_intensity, history[[turn,level]]}
  "relationships": {},# "A->B" -> {dims{dim:val}, history{dim:[[turn,val]]}, labels[]}
  "facts":      {},   # fid(blake2b) -> {statement, established_turn, is_secret}
  "beliefs":    {},   # "learner|fid" -> {stance, source, teller, acquired_turn}
  "memories":   [],   # rolling last-100 event dicts (retrieval index lives in the DB)
  "scene":      {},   # {location_id, participants[], phase, mode, tension, intimacy,
                      #  scene_index, stagnation}
  "clock":      {"day":1, "time_of_day":"evening", "minutes":0, "calendar_note":None},
  "frozen":     False,# + frozen_reason, frozen_turn when set
  "rolls":      [],   # rolling last-10 dice results {spec, result, turn}
  "meta":       {"turn": -1},
}
```

**Derived, never stored:** `derived_exposure(state, eid)` computes exposed body zones (tracked
zones minus zones covered by a worn item). `state_summary(state)` builds the inspector "Now" view.

### Controlled vocabularies (enums, from `state.py`)

| Vocabulary | Values |
|---|---|
| `BODY_ZONES` | head, face, mouth, neck, shoulders, chest, breasts, nipples, back, arms, wrists, hands, waist, hips, ass, anus, genitals, thighs, legs, feet |
| `CONTACT_TYPES` | touching, caressing, gripping, kissing, licking, sucking, penetrating, grinding, restraining, impact |
| `BASE_POSITIONS` | standing, sitting, kneeling, straddling, lying_back, lying_front, on_all_fours, bent_over, held_carried |
| `CLOTHING_STATE` (action→state) | don→worn, open→opened, displace→displaced, remove→removed, destroy→destroyed |
| `GEAR_WORN` | base_layer, top, bottom, outerwear, footwear, headwear, handwear, facewear, eyewear, neckwear, armor |
| `GEAR_CARRIED` | load_bearing, backpack, pouch, tool, weapon, ammunition, consumable, medical, electronics, accessory |
| `REL_DIMS` | trust, affection, respect, desire, tension, fear, familiarity (trust/affection/respect are −100..100; rest 0..100) |
| `ACT_CATEGORIES` | kissing, manual, oral_give, oral_receive, vaginal, anal, toys, restraint, impact, degradation, praise, exhibition, group, roleplay_scene, other |
| `CONSENT_RANK` (low→high) | hard_limit(0), withdrawn(1), unknown(2), soft_limit(3), hesitant(4), granted(5), enthusiastic(6) |
| `TIMES` (chronological) | dawn, morning, midday, afternoon, evening, night, late_night |
| `SCENE_MODES` | live, flashback, dream |

---

## 2. Op vocabulary

Ops are the *only* way state changes. Each op is `{"op": kind, ...fields}`. `validate_op` checks
shape+enums; `apply_delta` then resolves aliases, checks authority, and applies. The reducer branch
is in `state._apply_op`.

**Extraction-emitted ops** (the model may produce these — mirror in `extraction.EXTRACTION_OPS` +
`prompts.OP_CARD`):

| op | required fields | notes |
|---|---|---|
| `set_attribute` | entity, key, value | free-form attribute store |
| `move_entity` | entity, to_location | |
| `presence` | entity, present(bool) | |
| `clothing` (alias `gear`) | char, item, action | action ∈ don/open/displace/remove/destroy; optional category, covers[], moved_to |
| `position` | participants[], base | base ∈ BASE_POSITIONS; optional anchor, detail |
| `contact` | action, from_char, to_char, type | action ∈ start/stop/change; type ∈ CONTACT_TYPES; intensity 0–3 |
| `arousal` | char, (delta \| set) | 0–100 |
| `mood` | char, (valence \| energy \| dominance) | each −100..100 |
| `consent_signal` | from_char, to_char, category, signal | signal ∈ grant/enthusiastic/hesitant/refuse/withdraw/safeword |
| `relationship_adj` | from_char, to_char, dimension, delta | delta clamped −30..30 |
| `reveal_fact` | learner, statement, source | source ∈ witnessed/told/overheard/inferred; optional teller, is_secret. Compression item 3 (2026-07-09): a near-restatement (token Jaccard ≥ 0.75) SUPERSEDES the older fact — it gains `retired_turn` + `superseded_by` (kept + labeled, never deleted); retired facts leave the L10 premise set |
| `fact_retire` | fact **or** statement | engine-internal (user/rule/genesis only — extraction REJECTED: the model never erases truth). Retires by fid, or best statement-token match ≥ 0.5 |
| `memory_event` | text | + participants[], importance 1–10, tags[] |
| `goal` | char, action, text | action ∈ add/complete/abandon |
| `time_advance` | (minutes \| to_time_of_day) | wrapping to_time_of_day advances the day; triggers craving ramp |
| `obsession` | char, target_kind, target | target_kind ∈ entity/act_category/substance/object/concept; delta\|set 0–100 |
| `craving` | char, substance, action | action ∈ consume/adjust |

**Engine-internal ops** (only `rule`/`user`/`genesis` sources emit these — never extraction):

| op | required | purpose |
|---|---|---|
| `clock_tick` | minutes | Tier-0 scene-minute counter (no craving ramp) |
| `scene_set` | — | set location / participants / phase (location change bumps `scene_index`) |
| `scene_dial` | dial | tension \| intimacy (organic) |
| `scene_mode` | mode | live \| flashback \| dream |
| `entity_add` | name | privileged; discovery gates it for extraction |
| `consent_set` | subject, partner, category, level | direct set (inspector/OOC) |
| `freeze` / `unfreeze` | — | safety family; unfreeze is user-only |
| `roll` | spec, result | dice (result pre-rolled at Tier-0) |
| `stagnation` | value | repetition signal for the director |

**Replay determinism.** `apply_delta` calls `_enrich` to bake config-dependent values into the
journaled op (`_turn`, and `_seed` for cravings, `_raw_mode` for raw-mode safewords). The reducer is
a pure function of (state, journaled ops); a later config change never rewrites history.

---

## 3. Mutation authority matrix (`authority_violation`)

Four **sources** with descending trust: `user > genesis > rule > extraction`. Ops belong to five
**families**: `scene`, `facts`, `organic`, `consent`, `safety`.

| Source | scene | facts | organic | consent | safety (freeze/unfreeze) |
|---|---|---|---|---|---|
| **user** | ✅ | ✅ | gated by `manual_override.enabled` | `consent_set` down (safer) always; up gated by override | ✅ (unfreeze allowed) |
| **genesis** | ✅ | ✅ | ✅ (initialization) | ✅ | freeze ✅, **unfreeze ✗** |
| **rule** | ✅ | ✅ | ✅ (suppressed while frozen) | via signals/boundary rules only, **not** `consent_set` | freeze ✅, **unfreeze ✗** |
| **extraction** | ✅ (but **not** `entity_add`) | ✅ | ✅ (suppressed while frozen) | via `consent_signal` only, **not** `consent_set` | **✗** may only propose |

Cross-cutting rules:
- **Frozen session** suppresses arousal / scene_dial / consent_signal (and craving/obsession for
  `rule`) from non-user sources — except a withdraw/refuse/safeword signal always lands.
- **Non-live scene** (flashback/dream) quarantines physical/consent/clock ops from non-user sources
  (`_NONLIVE_SUPPRESSED`) — a flashback can't undress the present.
- **Safewords:** in normal modes a safeword freezes the scene and sets scene-participant consent to
  `withdrawn`. In `consent.mode == "unrestricted"` (raw) a safeword is logged as data and does **not**
  freeze (Q13/Q14) — but a *user-commanded* freeze always works.
- **Entity creation** is privileged: extraction/rule can't `entity_add`; unknown names go to the
  discovery counter first, promoted after enough evidence.

Rejected ops are **quarantined** (dropped with a logged reason), never applied — invariant 3.

---

## 4. OOC path translation (`translate_path`)

`((aether.set <path> <value>))` (Tier-0) and the `/aether/session/{sid}/state` PATCH both route
through `translate_path(path, value) -> op|None`. Unknown paths return `None` (visibly rejected,
never silently applied). Bare-key aliases: `location→scene.location`, `time→clock.time_of_day`, etc.

| Path form | → op |
|---|---|
| `scene.location <v>` | `scene_set{location}` |
| `scene.phase <v>` | `scene_set{phase}` |
| `scene.mode live\|flashback\|dream` | `scene_mode` |
| `scene.tension\|intimacy <n>` | `scene_dial` |
| `clock.time_of_day <TIME>` | `time_advance{to_time_of_day}` |
| `clock.note <v>` | `time_advance{minutes:0, calendar_note}` |
| `char.<who>.arousal <n>` | `arousal{set}` |
| `char.<who>.affect.<valence\|energy\|dominance> <n>` | `mood` |
| `char.<who>.attr.<key> <v>` | `set_attribute` |
| `char.<who>.present <bool>` | `presence` |
| `char.<who>.location <v>` | `move_entity` |
| `char.<who>.craving.<sub>.level <n>` | `craving{adjust,delta}` |
| `char.<who>.obsession.<kind:target>.intensity <n>` | `obsession{set}` |
| `rel.<A>-><B>.<dim>` | `relationship_adj` |
| `consent.<A>-><B>.<cat> <level>` | `consent_set` |

Authority still applies (organic/consent edits need `manual_override`). `clock.day` is intentionally
**not** settable — the clock is monotonic; the day advances only via `time_advance` wraps.

---

## 5. SQLite schema (`store.py::_SCHEMA`)

WAL mode, single file (`aetherstate-data/aetherstate.db`). **Migrations are additive-only** — new
columns append to `_MIGRATIONS`; never drop/rename in place.

| Table | Key columns | Purpose |
|---|---|---|
| `sessions` | session_id PK, external_id UNIQUE, anchor_hash, frontend, active_branch, frozen, created_at, last_seen, **genesis**, **mode**, **label** | one row per chat |
| `branches` | branch_id PK, session_id, parent_branch, forked_at, status, head_turn | edit-forks/swipes create branches |
| `turns` | (branch_id, turn_index) PK, user_hash, assistant_hash, chain_hash, klass, gen_type, swipe_count, settled, extraction | per-turn ledger; `extraction` ∈ pending/done/failed |
| `ops_journal` | id PK, branch_id, turn_lo, turn_hi, ops(JSON), source, ts | the authorized-op journal (replay source of truth) |
| `checkpoints` | (branch_id, turn_index) PK, state(JSON) | periodic full-state snapshots (`checkpoint_every_turns`) |
| `branch_msgs` | (branch_id, pos) PK, role, content_hash, chain_hash | canonical transcript for L3 alignment |
| `slices` | session_id PK, for_turn, components(JSON), created | the precomputed briefing (hot path reads this) |
| `turn_texts` | (branch_id, turn_index) PK, user_text, assistant_text | captured prose for extraction/discovery/lint |
| `caps` | (base_url, model) PK, rung, probed_at, failures, **native**, **anyof** | capability cache per backend |
| `discovery` | (branch_id, name) PK, turns(JSON), status | entity-evidence counter |
| `memories` | memory_id PK, session_id, branch_id, tier, text, participants, location_id, tags, importance, created_turn, last_accessed_turn, parent_id, scene_index, embedding_ref | retrieval index |
| `recall` | session_id PK, for_turn, lines(JSON), created | precomputed recall for next turn |
| `lint` | id PK, branch_id, turn_index, rule, severity, subjects, detail, evidence, ts | violation log |
| `hints` | id PK, session_ext, event, message_index, ts | fire-and-forget UI hints |
| `notes` | session_id PK, for_turn, text, created | the next-turn director note |
| `embeddings` | memory_id PK, vec(BLOB), dim | packed memory embeddings |
| `director` | id PK, branch_id, turn_index, beat_id, scene_index, ts | fired-beat log (cooldowns) |

**`state_at(branch, turn, reducer, empty)`** = nearest checkpoint ≤ turn + ordered replay of
`ops_journal` through `state.reduce_state`. This one primitive powers current-state reads,
edit-forks, swipe rollback, the replay harness, and the inspector scrubber.

---

## 6. Director beat file format (`beats/*.json`)

A beat library is a JSON array of beat objects. `load_libraries` requires the four fields
`beat_id`, `name`, `preconditions`, `note_template`; the rest default (`binds="none"`,
`effects=[]`, `priority=50`, `cooldown_turns=6`, `once_per_scene=False`, `phase_hint=None`).

```json
{
  "beat_id": "erp_escalation.seek_consent_kiss",
  "name": "Seek consent: kiss",
  "binds": "pair",                         // none | char | pair | craving | obsession
  "preconditions": { "all": [              // DSL tree: all/any/not + {path, op, value}
    {"path": "rel.{a}->{b}.desire", "op": ">=", "value": 60},
    {"path": "scene.intimacy", "op": ">=", "value": 40},
    {"path": "consent.{a}->{b}.kissing.level", "op": "in", "value": ["unknown", "hesitant"]}
  ]},
  "note_template": "Have {initiator} ask before going further with a kiss. Let {partner} answer.",
  "priority": 58,
  "cooldown_turns": 8,
  "once_per_scene": false,                 // optional
  "effects": [                             // optional: ops applied (source=rule) when it fires
    {"op": "scene_dial", "dial": "tension", "delta": 5}
  ],
  "phase_hint": "rising"                   // optional: also emits scene_set{phase} on fire
}
```

- **`binds`** decides which `{tokens}` the template + preconditions can use, and enumerates
  candidates deterministically (sorted; the user's own character never fills an actor slot):
  `none` → scene-global (`[{}]`); `char` → `{char}` per present non-user character;
  **`pair`** → `{a}`/`{b}` over ordered present pairs, and `render_note` also exposes
  `{initiator}`(=a) and `{partner}`(=b) as display names; `craving` → `{char}`,`{substance}`
  per character craving; `obsession` → `{char}`,`{obs_key}`,`{obs_target}` per obsession.
- **`preconditions`** is a DSL tree combined with `all` / `any` / `not`; leaves are
  `{path, op, value}`. Operators: `==, !=, >, >=, <, <=, in, contains, exists`
  (`exists` tests path presence; `contains` tests membership the other way). Paths are resolved by
  `director.resolve_path` (`scene.*`, `session.frozen|turn`, `clock.*`, `rel.A->B.dim`,
  `consent.A->B.cat.level|max_intensity`, `char.X.arousal|affect.*|goals|secrets|status_effects|
  present|attr.*|craving.SUB.field|obsession.KEY.field`) — any path it understands is usable with no
  code change; an unresolved path makes the leaf **false** and logs an authoring warning.
- **`effects`** (optional) are ops the winning beat applies through `apply_delta` with
  `source="rule"` (so the authority matrix + frozen/non-live suppression still apply) — a beat can
  nudge state, not just narrate. `phase_hint` additionally emits `scene_set{phase}` on fire.
- **Selection:** filter by cooldown (`cooldown_turns`) and `once_per_scene` (via the `director`
  table), evaluate preconditions per binding, then pick the winner by
  `(priority, consent_headroom, beat_id)` — `consent_headroom` prefers beats furthest from consent
  limits. The rendered `note_template` becomes next turn's director note (a note with any unresolved
  `{token}` is dropped). If **no** beat matches, a pacing pseudo-beat may fire
  (`pacing.complication` on stagnation, `pacing.raise`/`pacing.ease` vs the phase tension curve).
  Linter violations are folded in as higher-priority **corrective** notes (unless
  `linter.corrective_notes=false`).
- **Frozen** sessions run `aftercare_checkin` **exclusively**; **flashback/dream** scenes get no
  steering at all (08 B4).

Shipped libraries: `core_drama`, `erp_tension`, `erp_escalation`, `erp_aftercare`,
`aftercare_checkin` (registered in `DirectorConfig.beat_libraries`).

## 7. RPG specialization — Player Card & mechanics (v0.2, phases RPG-0…RPG-2)

Gated by `[specialization].name == "rpg"` (see `03 §4`); a `none` session is byte-identical to
pre-RPG behaviour. RPG-0 adds one runtime state key and one privileged op; RPG-2 adds the item
plane (§7.6). Affinity/factions and their ops land with RPG-3 (doc 06 §1).

### 7.1 Runtime state key (additive to `empty_state()`)

`"player": {}` — `eid -> Player Card record` for the user's character. (In RPG mode the *card*
is the Dungeon Master; the user's persona is the Player.) Shape:

```
{eid, level, xp, hp:{cur,max}, resources:{name:{cur,max}}, stats:{STR:int,...},
 skills:{skill_id:rank}, abilities:[id], cooldowns:{id:until_turn},
 soulmate:eid|None, nemesis:eid|None, concept?, pronouns?,
 defs?:{skills|abilities|stats:{id: FROZEN per-character snapshot}}}
```

The player is also an ordinary `entities` row, marked `kind:"player"`. `state_summary` surfaces
`player`; `is_empty()` counts a lone Player Card so the header renders for a player-only state.

**Per-character `defs` (optional overlay).** A Player Card may carry FROZEN per-character definitions
under `defs:{skills|abilities|stats:{id:snapshot}}` — a mastery-evolved or freestyle-authored mechanic.
At resolution the registry reads these **snapshot-first** (falling back to the shipped registry); an id
present only in `defs` still resolves, while ids unknown to both are still rejected (preset mechanics
unchanged). Seeded via `player_seed`; the effective mod is baked into the `check` op, so replay stays
deterministic. Foundation for the Q27 assist-authored → snapshot loop and Q29 mastery evolution.

### 7.2 Op: `player_seed` (privileged — genesis/user only)

`{"op":"player_seed","entity":<name|eid>,"card":{level?,concept?,pronouns?,stats?,skills?,`
`abilities?,resources?,hp?,defs?}}` — seeds/updates the Player Card record, expanding a partial seed
tolerantly (a resource's `cur` defaults to its `max`; an `hp` resource routes to `hp`). Family
`player`. **Authority:** allowed for `user` and `genesis`; rejected for `rule` and `extraction`
— the same privileged discipline as entity creation (only discovery creates entities; only
genesis/user seed the Player Card). Never a member of `EXTRACTION_OPS`, so extraction can never
emit it and the anyOf/flat schema weld is unaffected.

### 7.3 Render (`compose.py`)

`render_header` emits `[PLAYER]` (name · level · HP/resources · stats · skills · abilities) and
`[QUEST]` (over the existing per-character `goal` ops) when specialization is `rpg` and the
block is listed in `[specialization].blocks`; each is omitted when its data is absent (same
conditional pattern as `[CONSENT]`). `render_guard` gains a Dungeon-Master framing gated by
`[specialization].dm_guard`. Render exemplar: doc 06 §2.3; full block catalog: doc 05 §6.


### 7.4 RPG-1 — curated mechanics & the `check` op (phase RPG-1)

**Curated registry (`src/aetherstate/registry/`).** Shipped TOML (`meta`/`stats`/`skills`/
`abilities`) + a cached loader (`registry.load(cfg)`). Each skill declares a `keyed_stat`,
`base_mod`, `max_rank`, and `governs` verbs; abilities carry bounded, code-understood effects (a
`resolution_mod`, or a passive `passive_mod` naming a skill). A user extends/overrides via
`<data_dir>/registry/*.toml` (per-table merge). The model may reference registry ids and propose
a check; code maps the proposal to a REGISTERED skill or REJECTS it (unknown id → no op + a
visible notice — "nothing freestyle"). Read once, cached → hot-path-legal.

**Op: `check` (privileged — rule/user; never extraction).**
`{"op":"check","skill":<id>,"result":<int total>,"tier":<CHECK_TIERS>,char?,dc?,_mod?,_dice?,_seed?}`.
`CHECK_TIERS = [crit_fail, fail, partial, success, crit_success]` lives in `state.py` — the single
source of truth (`validate_op` checks it; `registry.resolve_tier` only emits members). Family
`scene`; in `_NONLIVE_SUPPRESSED` (a flashback can't roll live dice). **Authority:** allowed for
`rule` (the Tier-0 R8 path) and `user` (manual OOC); rejected for `extraction` (the model never
rolls). NOT in `EXTRACTION_OPS`/`OP_FIELD_ENUMS` — a `check` has no wire schema, so the extraction
weld is untouched. The reducer **reuses the `rolls` buffer** (a check is a richer roll): appends
`{skill,result,tier,mod,dice,dc,char,turn}`, keeps the last 10. Replay-pure — `result`/`tier` are
literal fields on the journaled op, so `state_at` reproduces the tier with no RNG.

**R8 resolution (`tier0.py`, hot path, µs).** On an explicit `((aether.check <skill> [+N|-N]
[vs DC] [scope minor..mythic] [use <ability>]))` in the new user message (RPG only): map the token
to a registered skill, compute the effective modifier (`stat_mod(keyed_stat)+base_mod+rank+passive-
ability mods`) plus the declared situational mod, roll real multi-die per the dice knob, compute the
PbtA tier (10+/7-9; crits on all-max/all-min; a `vs DC` shifts the thresholds), and emit a `check`
rule op. Arithmetic only — no LLM (invariant 2). Inert unless `rpg`; the OOC span is stripped from
the forwarded message.

**Ability mechanics (2026-07-07 — abilities SHAPE THE DICE, they don't buff a number).** A skill
sets the modifier; an ability's curated, frozen `mechanic` reshapes the roll: **`edge`** (advantage
— roll an extra die, keep best), **`ward`** (a crit-fumble floors up to a plain miss), **`extra_die`**
(on a **miss**, roll another die keep best — the "second chance"), **`reroll`**, **`surge`** (a big
bonus that ALSO lifts the scope tier ceiling one step), plus legacy **`mod`** (flat +N) and
**`basis`** (the eligibility-gate key). Passive edge/ward auto-apply; active surge/extra_die/reroll
are invoked with `use <ability>`, gated on `has_ability` + affordability + a per-ability cooldown
(`player["ability_cd"]`), and fire (+ are paid) only when they help. The whole dice narrative bakes
into `check._shape` (`pool/kept/fired/improved/edge/ward/surge`) so replay reads no registry/RNG.

**Render (`compose.py`).** `[DIRECTIVE]` renders the pre-decided outcome of THIS turn's check
("NARRATE: <tier> — the <skill> check resolved as <TIER>. …") and rides the never-dropped
`state_header`, so the resolve-then-narrate contract can't be budget-cut. `[ROLL]` now skips
check records (no `spec`). A compact, droppable `[RULES]` DM rules-contract
(`prompts.DM_RULES_CONTRACT`, versioned) is injected under `rpg`.

**Linter: `outcome_match` (`linter.py`, cold path).** Fires when the narration asserts a result of
OPPOSITE polarity to the pre-decided `check` tier (a conservative lexicon bound to the skill/char
subject within a small window; `partial` is lenient — only crits conflict). `med`, escalating to
`high` on a persisting override (store-wins re-ask). Gated by a shared `_rpg_active(state, cfg)`
(Player Card + `specialization=rpg`) and live-scene only; `rules_off` code `outcome_match`.

### 7.6 Items — Template+Instance (phase RPG-2, doc 06 §3.5 / 07 §7)

**Runtime keys** (additive to `empty_state()`): `"items": {}` (instance_id →
`{template_id, name, qty, loc, owner, mods_snapshot, minted_turn, slot?/covers?/on_consume?/
stackable?/max_stack?/capacity?/is_container?/worn?/bound?}`), `"gear": {}` (eid → {slot: iid},
worn equipment) and `"inventory": {}` (eid → {container_id: [iid…]}, carried-not-worn;
`loose` is the unbounded pseudo-container). `items[iid].loc` (`gear:<slot>` | `inv:<cid>` |
`world` | `gone`) is the SINGLE source of truth; gear/inventory are derived indexes kept in
lock-step by the reducer (remove → add with rollback) — the `one_instance_one_place` linter rule
is a pure safety net that self-heals from `loc` on an out-of-band break.

**Templates** are immutable reference data (`registry/items.toml`, user-overridable via
`<data_dir>/registry/items.toml`). At mint, `_enrich` bakes the template's
mods/slot/covers/on_consume + a generated unique `_iid` (`template#N`) into the journaled op —
editing a template never rewrites a minted instance (replay purity).

**Ops.** `item_mint{template,owner,qty?,to?,bound?}` is PRIVILEGED (user/genesis/rule; extraction
is rejected like `entity_add`). `item_move{instance,to}` · `item_equip{instance,slot,swap?}` ·
`item_unequip{instance,to?}` · `item_consume{instance,amount?}` ·
`item_transfer{instance,to_owner,to?}` are PROPOSABLE (extraction may propose; on the wire only
under `rpg`) and TRANSACTIONAL — a capacity/slot/ownership failure quarantines the op with a
visible reason and rolls state back (the AI-Roguelite duplication-bug guard). All item ops are
scene-family and `_NONLIVE_SUPPRESSED` (a flashback can't touch live items). `instance` resolves
by exact id or UNIQUE case-insensitive display name; unknown/ambiguous → quarantined.

**Derived, never stored:** equipped-gear mods naming a skill flow into R8's effective check mod
and the `[PLAYER]` skill render at read time; `item_consume` applies only the bounded,
code-understood effects baked on the instance (`heal`, `restore`) — template prose is never
executed. Gear slots: the built-in 16-slot map, extensible per-table via `meta.toml`
`extra_slots` (membership baked onto `item_equip` at `_enrich`).

### 7.7 Statuses & Conditions + the eligibility gate (phase RPG-3)

**The principle:** the LLM writes the story, AetherState owns the truth — nothing becomes real (or
even rollable) without an in-world basis. Effects are ENGINE-OWNED ledger state, never flavor text;
the model *proposes* (tag protocol / extraction), code *commits*, and the committed ledger is fed
back every turn (`[EFFECTS]`) so the narrator cannot drift or forget.

**Runtime key** (additive to `empty_state()`): `"effects": {}` — eid →
`{effect_id: {id, name, kind: status|condition, valence: negative|neutral|positive, stacks,
gained_turn, mods, preset, duration?, note?}}`. Applies to the player AND every tracked character.
**Statuses** are combat-facing buffs/debuffs (Bleeding, Poisoned, Stunned, Hasted…); **Conditions**
are anything else that makes in-world sense (Cursed, Blessed, Drunk, Diseased, Pregnant…).
**Valence is dynamic** — a tracked property, never a hardcode: `effect_update` shifts it in play
(a blessing, or the character's own reframing). Expiry is DERIVED (`gained_turn + duration` vs the
current turn, `registry.effect_active`) and never mutated — replay needs no expiry ops.

**Presets = the floor; open vocabulary = the ceiling.** `registry/effects.toml` (user-overridable)
ships ~22 curated presets; at apply, `_enrich` bakes the matched preset's
name/kind/valence/mods/duration/requires into the journaled op (editing the file never rewrites
history). An UNKNOWN name still commits — as an open-vocabulary record with no engine-side
mechanics (a strong model can mint `Marked by the Deep`; a weak model still grounds `Bleeding`).
`mods` (per-skill or `all` check modifiers) come ONLY from the preset bake — never from the wire
(`scrub_op` drops them; the model never authors mechanics). `requires = "female"` is a data-driven
gate checked at apply against the sex/gender attribute or the card's pronouns (unknown → visible
reject, never a guess).

**Ops.** `effect_add{char,effect,kind?,valence?,note?,duration?,stacks?}` ·
`effect_remove{char,effect}` · `effect_update{char,effect,valence?,stacks?,duration?,note?}` are
PROPOSABLE (extraction + the R9 tag protocol; on the wire only under `rpg`); scene-family,
`_NONLIVE_SUPPRESSED`. Re-adding an active effect refreshes its clock and merges fields (no
duplicate rows); removing/updating what the ledger doesn't show raises `OpReject` with a visible
reason. `effect` resolves per-entity by id, slug, or unique display name.
`ability_grant{char,ability,def?}` is PRIVILEGED (user/genesis/rule; extraction rejected) — the
acquisition route: a registry ability grants directly (`_known` baked at `_enrich`), a freestyle
`def` freezes into `player["defs"]["abilities"]` first (Q27 snapshot loop).

**R9 — the effect tag protocol (`tier0.py`, hot path, µs).** The channel AI-Roguelite never had:
the DM marks the change inline — `[status gained | <char> | <Name> | <valence>]`,
`[status lost | …]`, `[condition gained/lost | …]`, `[valence shift | <char> | <Name> | <valence>]`
— and a deterministic regex pass over the LAST settled assistant reply turns tags into effect
proposals (source=extraction: clamped, quarantined visibly). `{{user}}` maps to the Player Card;
unknown bracket tags are ignored; acts once per new turn (swipe-safe by classification). RPG-only.
Tags stay in the prose (they teach the protocol by example); hide them cosmetically with an ST
regex script if desired. The narrator learns the grammar from the `[TAGS]` section appended to the
`[RULES]` contract (with a compact preset slice, re-sent every request — rollover-proof).

**The eligibility gate (R8, doc 10).** Before dice: *is there an in-world basis at all?* A skill
whose registry/def entry carries `requires_ability` is a NON-MOVE without that ability — visible
notice, no op, no roll ("you cannot declare power; you acquire it in-world"). Freedom is routed,
not blocked: earn the ability (quest → `ability_grant`), and the same declaration becomes a real
check. **Scope-gated power:** `((aether.check <skill> … scope minor|standard|major|epic|mythic))`
scales the attempt against mastery (= skill rank): each scope step past the rank costs −2 on the
roll AND lowers the tier ceiling one step (floor: partial) — punishing odds and a low ceiling for
a thin basis, plausibility for deep mastery, never a flat veto. Scope arithmetic and the FINAL
(capped) tier are baked into the journaled `check` op; ledger-effect mods
(`registry.effect_skill_mod`) join gear mods in the effective check mod.

### 7.8 The social plane — affinity, factions, bonds, world flags (phase RPG-3b)

**The principle, applied to relationships:** standing is LEDGER state, not prose sentiment. Every
shift is a journaled, reason-tagged delta; the briefing surfaces the DERIVED tier label, never the
integer — the number cannot drift because it is never restated by the model.

**Runtime keys** (additive to `empty_state()`):
`"affinity": {}` — `"<playerEid>-><targetEid>"` → `{value, kind: npc|faction, ledger: [{turn,
delta, reason}] (last 50), labels: []}`. `value` is the clamped ledger sum (−100..100); the tier is
DERIVED at render/inspection (`state.affinity_tier`): Nemesis ≤−80 · Hostile −79..−40 · Cold
−39..−10 · Neutral −9..9 · Warm 10..39 · Ally 40..79 · Devoted ≥80 (soulmate-eligible,
`DEVOTED_MIN`). `"factions": {}` — fid → `{name, circumstances: {key: value}}` (factions are also
`entities` rows with `kind: "faction"`). `"world": {}` — a bounded global flag map (`world_flag`).
The Player Card's `soulmate`/`nemesis` scalars (§7.1) are the bond pointers.

**Ops.** `affinity_adj{target,delta,reason?,kind?}` — PROPOSABLE (organic family: extraction may
propose, frozen-suppressed, user edits gated by `manual_override`). The per-turn delta is clamped
to ±15 (`AFFINITY_DELTA_CLAMP`, the AI-Roguelite anti-swing fix) and BAKED into the journaled op
(`_delta`) at `_enrich`; `kind` derives from the target entity's kind and never rides the wire.
`world_flag{key,value,faction?}` — PROPOSABLE (facts family): a standing world circumstance; `key`
is slugged, `value` is a scalar (str/int/bool), `value: null` clears, `faction` scopes it to
`factions[fid].circumstances`. `set_soulmate{target,demote_label?}` / `set_nemesis{…}` —
PRIVILEGED (user/genesis/rule; extraction may only nudge affinity, never seal a bond). The reducer
is an atomic DEMOTE-THEN-SET: promoting a new bond first stamps the incumbent's affinity entry
with a label (`beloved`/`rival` by default), then repoints; `target: null` clears. Uniqueness
holds by construction; the `one_soulmate` linter rule (+ off-by-default `one_nemesis`,
`[specialization] nemesis_enabled`) guards eligibility (affinity ≥ Devoted), referential
integrity, and off-book prose promotions.

**The faction cascade (deterministic, cold path).** After an extraction batch applies, NPC
affinity shifts ripple to the NPC's faction (membership = the entity's `faction` attribute):
`Δ_faction = Δ_npc × faction_cascade` (default 0.1), HALVED on negatives (anti-death-spiral),
rounded away from zero — emitted as journaled RULE-source `affinity_adj` ops
(`state.faction_cascade_ops`), never a hidden reducer side-effect. Cascades never chain.

**Render (rpg-gated blocks, doc 05 §6).** `[FACTIONS] Iron Covenant: Ally (at_war=yes)` — tier
label + circumstances. `[RELATIONS] Mira: Warm · Seraphine: Devoted ♥soulmate` — PRESENT NPCs +
all bonded characters (a demoted bond shows its label: `Seraphine: Devoted (beloved)`).
`[WORLD] plague=spreading` — global flags. All absent under `none` and when empty.

**OOC set-paths (rpg-gated — a `none` session's command surface is unchanged):**
`((aether.set world.<key> <value|none>))` (true/false/int coerced) ·
`((aether.set affinity.<name> <±N>))` (manual_override-gated, per-turn clamp applies) ·
`((aether.set player.soulmate <name|none>))` / `player.nemesis`. The extraction wire gains
`affinity_adj` + `world_flag` ONLY on the rpg tier (`RPG_SOCIAL_OPS`, `RPG SOCIAL OPS` card);
`state_summary` exposes `affinity` (with derived `tier`), `factions`, and `world` for the Console.

### 7.9 Persistence & the location registry (phase RPG-4)

**One player per session (2026-07-06).** `player_seed` is seed/REPLACE, enforced in the
reducer: seeding entity B while entity A holds the card drops A's record — a genesis-default
placeholder (card baked with `_genesis_default`, record marked `genesis_default`) vanishes
entirely (entity + attributes), an authored predecessor is demoted to `kind="npc"`. Genesis
stage B can never mint players (`player_seed` / `entity_add kind=player` scrubbed in
`_parse_ops`), and stage-B `presence=true` ops require the name in the card/prompt text
(`_presence_with_basis` — notable NPCs stay known-but-offstage until the fiction stages them).

**Location canonicalization (rpg only).** Under `specialization=rpg`, `_enrich` bakes a
canonical location into every journaled `scene_set`: `canonical_location(state, raw)` trims
free prose to its name head (≤6 words, split before `, ; : . (` and spaced dashes), then
resolves exact id → exact name/alias (article-stripped, case-insensitive) → unique
token-subset (raw ≤4 tokens AND exactly one candidate — the wrong-merge guard) → NEW. Baked
keys: `_canon=1`, `_loc_create{eid,name}` (new row), `_loc_alias` (learned variant). The
reducer (pure state+op) creates the registry row, appends the alias, and counts `visits` +
`last_visit_turn` on scene boundaries. Replay never re-resolves — the journaled op carries
the decision. A `none` session's ops carry none of these keys and behave exactly as before.

**Persistent procedural generation.** `discovery.scan_locations` (preposition + capitalized
run: "into the Gilded Lantern", "at Harborfall") feeds the same ≥2-turn evidence counter
(`loc::`-keyed) and creates `entity_add kind=location` via source=`rule` — but only after
`canonical_location` says the place is genuinely new, so a revisit under any variant is an
alias hit, never a duplicate row. Generated once, persisted forever, never regenerated.
Rpg-gated at the caller (`pipeline`), so `none` journals stay byte-identical.

**Degradation ladder (D7).** `[specialization].contract = "compact"` selects
`DM_RULES_CONTRACT_COMPACT` (~40 tokens, same non-negotiables) for weak/local models;
default `"full"`. The contract version is `dm-rules/2`: state-block NPCs are KNOWN not
on-scene, and replies end in-fiction — never "What will you do?"-style prompts.

**Auto-compact on calm turns (A1, 1.19.0).** Independent of the fixed `contract` size and of the
budget-degrade fallback, `[specialization].auto_compact_contract` (opt-in, default off) makes
`compose` flip to the compact contract on calm, ESTABLISHED turns — the model has internalized the
full rules by then, so re-injecting all ~1,981 tokens every turn is the biggest avoidable per-turn
token + reasoning cost. The FULL contract still rides the first `contract_full_turns` turns (warm-up,
default 3) and EVERY combat turn (`compose._combat_turn`: tracked combatants active OR a
climax/combat/battle/fight/ambush scene phase). The decision (`compose._auto_compact_contract`) is a
pure read of `meta.turn` / `scene.phase` / `combat.active` on the hot path — no network, nothing
journaled, replay-neutral — and a `none` (or knob-off) session is byte-identical.

**Inspector feed.** `GET /aether/session/{sid}/journal?limit=N` (03 §2) serves the applied-op
tail (turn · source · op · salient fields) + the last rolls; the Console Overview renders it
as "Recent activity (RPG)" — visible roll/state-change feedback without touching the stream.

### 7.10 RPG-5 — recording gaps closed + the progression capstone (2026-07-07)

**The R10 world-tag protocol (tier0).** The R9 spine extended to the whole ledger — parsed
deterministically from the DM's LAST settled reply, applied as extraction-source proposals
(clamped, quarantined visibly), rpg-only:
`[scene | <location> | <phase?> | present: <names?>]` → `scene_set` (canonicalized at
`_enrich`) + presence ops (a declared cast REPLACES the on-stage list; the player is never
un-staged) · `[item gained | <char> | <Item> | <qty?>]` / `[item lost | <char> | <Item>]` ·
`[quest | <Name> | new|update|complete|failed|abandoned | <note?>]` ·
`[affinity | <target> | ±N | <why>]` · `[hp | <char> | ±N | <why>]`. `_tag_char` maps
`{{user}}`/`user`/`player` AND the player's own name tokens to the player eid (a DM that says
"Kaji" must not hit a discovery twin; `discovery.known_names` also treats name tokens as known).

**New PROPOSABLE ops (tag protocol + the rpg extraction wire — `RPG_GAP_OPS`):**
- `item_gain{char,name,qty?}` — the organic acquisition channel. `_enrich` bakes a registry
  template snapshot when the name matches one (curated floor); otherwise the instance commits
  MECHANICS-FREE (no mods from prose — pillar 4). Same-name re-gain STACKS qty on the existing
  instance (the anti-duplication rule). `item_lose{char,name}` decrements/`gone`s a ledger
  instance — losing what the ledger doesn't show is a visible reject.
- `quest_add{name,detail?,giver?,stakes?:minor|serious|epic}` /
  `quest_update{quest,status?:active|complete|failed|abandoned,note?}` — the quest ledger
  (`state.quests`, facts family, bounded 40, settled quests age out first). Near-dupe guard:
  an ACTIVE quest whose ≥4-char name tokens contain/are contained by the new name's tokens is
  the SAME quest (add merges; update resolves by the same rule, unique hit only). `[QUEST]`
  renders active quests (+stakes, +note) and recently settled ones; legacy per-char `goal`s
  remain the fallback.
- `hp_adj{char,delta,reason?}` — the bounded consequence channel: per-op swing clamped at
  `_enrich` to ±max(5, hp.max//4) (baked `_delta`), floor 0 / cap max.

**Progression (doc 10) — ALL PRIVILEGED (rule/user/genesis; extraction rejected):**
- `award_exp{char,amount,reason?}` — code-awarded only: quest completion by stakes
  (25/75/150), goal completion (15), positive standing-tier crossings ≥Ally (30) — values in
  `XP_AWARDS`. `xp_level`: cumulative 50·L·(L−1) curve (L2=100, L3=300…).
- `level_up{char}` — grants baked at `_enrich` (`LEVEL_GRANTS`): +4 max HP, +2 every pool,
  +1 banked `stat_points` (rendered on `[PLAYER]`, spend UI later).
- `master_tick{char,skill,amount}` — use grows mastery: emitted by R8 per resolved check
  (crit 4 / success 3 / partial 1 / fail 1 / crit_fail 0), scene-capped at 6/skill
  (`mastery_scene`), hard ceiling 120. Brackets Novice/10 Adept/30 Expert/60 Master/100
  Grandmaster; the bracket BONUS (+0..+4) joins `registry.effective_mod` — the curated
  evolution floor. A crossing bakes `_bracket_up`, and the cold path schedules
  `creator.evolve_def_snapshot` (the Q27 loop): assist re-authors the def (base_mod at most
  +1, gate/cost preserved), clamps, freezes via `evolve_def{char,table,id,def}`.
- `defeat_resolve{char,outcome}` — HP 0 triggers it from `progression_ops` (deterministic
  outcome class: hostile present → captured, cool present → robbed, warm present → rescued,
  else wake_safe; `[specialization].hardcore=true` → death, final). Reducer: HP to max//4
  (death: 0), baked Battered/Dead condition, `robbed` drops carried unbound items to world.
  The defeat rides `[DIRECTIVE]` the turn it lands — code decides the class, the DM flavors it.
- `progression_ops(state, applied, hardcore)` runs post-apply on BOTH paths (pipeline hot
  µs-arithmetic + jobs post-batch), returning journaled rule ops — never reducer side-effects.

**Resources (doc 10 §6).** Registry/def skills may carry `cost = {stamina|mana|hp: N}`
(frozen at authoring; clamped 1..10). R8 charges on attempt (fail pays half) via baked
`_cost`; an insufficient TRACKED pool is a visible non-move ("recover first — not a roll");
an untracked pool waives the cost (the weak-floor rule). Regen is curated + replay-pure:
scene boundary +25% of max, `time_advance` with a time-of-day = stamina full / others +50%.
Creator seeds stamina 12 always; mana 10 only on magic-shaped sheets (basis ability, gated
skill, or a mana-costed def).

**Consequences of failure.** R8 crit_fail adds a curated status: `Strained` (−1 all, 3t) —
or `Backlash` (−2 all, 4t) when the check overreached scope; scope `over ≥ 3` now FORCE-fails
the attempt outright (ceiling — a natural crit_fail stays worse), per Bean's Alter-Reality rule.

**dm-rules/3.** A resolved check settles THIS attempt NOW — no stalling into negotiation, no
premise-nullifying, no deferral (two live playtests of directive-dodging). [TAGS] grew the
full R10 grammar with an affinity example and the "real harm = hp tag" rule.

**Director additions.** `rpg_adventure` beat pack (profile default): stale-quest push, wounded
player, defeat aftermath, no-active-quest hook. New DSL paths: `quest.active_count`,
`quest.<qid>.<field>`/`.stale_turns`, `player.hp_frac|level|xp|defeated_ago`, `world.<key>`;
new binds kind `quest`. `GET /aether/session/{sid}/search?q=` reuses the memory scorer over
the summary/memory ledger (the AI-search hook, read-only, fail-open).

A `none` session remains byte-identical: every surface above is rpg-gated (tags, wire tier,
progression passes, pools only exist on rpg cards), welded by the test suite (453 green).

### 7.11 Phase 1 — the full combat loop + 3v3 party / War Room (1.13.0, plan doc 13)

**Runtime keys (lazy — a pre-1.13 checkpoint replays untouched):** `state["combat"]`
`{active, started_turn, combatants: {cid: row}, history[≤10]}`; a combatant ROW is
snapshot-frozen at spawn: `{id, name, side: ally|enemy, kind: extra|tracked, eid?, tier,
hp{cur,max}, armament, mod, loot[], defeated, defeated_turn?, spawned_turn, dropped[]}`.
Plus `state["clashes"]` (≤20 recorded NPC-vs-NPC fights) and `state["loot"]`
(tier → frozen rows, written only by `loot_table`).

**Ops.** PRIVILEGED (rule/user/genesis; extraction rejected): `combatant_spawn{name, side,
tier?, char?, armament?}` — `_enrich` bakes `_cid`, `_hp` (THREAT_HP by tier — minion 6 /
standard 14 / elite 26 / boss 44 — or, for a tracked `char`, the entity's persisted
`attributes.hp`: wounds carry between fights), `_mod` (curated by tier) and `_loot` (the
frozen table: `state["loot"]` > `registry/loot.toml` > fallback); the reducer enforces the
3v3 cap (player holds an ally slot) and one live row per tracked entity.
`combatant_defeat{target}` — `_enrich` bakes `_xp` (THREAT_XP 15/30/60/120) and
`_loot_drop` (md5-seeded roll over the row's frozen table, iids pre-generated); drops mint
as `world` items. `combat_end{outcome?}` — extras evaporate; tracked survivors write
`attributes.hp` back (below half ⇒ `Wounded`, beaten ⇒ `Battered` — healing is a routed
in-world path); a history entry records defeated/survivors/loot. `loot_table{tier,
entries}` — Creator/assist-authored rows clamped + FROZEN (pillar 18). PROPOSABLE:
`combatant_hp{target, delta}` (resolves cid | unique name among LIVE rows; per-op clamp
±max(5, max//4) baked; a code-decided player strike carries `_strike` and lands exact) and
`clash_record{a, b, method?, outcome?}` (participants alias-resolve to REAL rows or
quarantine; commits a bounded clash entry + a fact — record, never resolve).

**Tier-0 / dice.** `((aether.check <skill> at <target>))` (or prose naming a live foe, or
the lone-foe (or a fiction-numbered band, "three cutthroats") + attack-verb floor) binds a check to an enemy row: damage = STRIKE_FACTOR
(crit 3 / success 2 / partial 1) × the equipped weapon's `damage` mod (floor 1, +1 on a
surge), emitted as a rule `combatant_hp` alongside the check and shown on the [DIRECTIVE]
("the blow lands on X for N — narrate that exact toll"). Every live ally gets ONE
pre-decided `[ALLY]` die per combat turn (`compose._ally_die`, md5(turn·scene·cid) — the
R8c pattern: deterministic, no journal row); enemy→player harm stays R8c `[OPPOSITION]` +
`[hp]` (the opposition die now also arms whenever live enemy rows exist, so extras with no
affinity ledger still fight). The `[WAR]` board (exact HP, ratified) rides the volatile
directive tail (0a-safe). The DM's channels: `[foe | <name> | <tier?> | <weapon?>]` (enemy side) and
`[ally | <name> | <tier?> | <weapon?>]` (2026-07-10: the symmetric party channel — a present
companion onto the Player's side; a whole enemy group is several `[foe]` tags)
(validated → re-sourced as rule by the pipeline, the R8b pattern; a known cast name spawns
TRACKED) and `[hp | <combatant> | -N | why]`; the user's: `((aether.foe/ally/combat end))`.

**The referee (`state.combat_ops`)** — pure, journaled, on BOTH apply paths (pipeline
`_progress` + `_ingest_reply_tags` + the jobs batch, before the progression pass so defeat
XP feeds level-ups): floor auto-spawn (combat phase/flag ⇒ present Cold-or-worse hostiles
enlist as foes, present COMPANIONS as allies (2026-07-10 — grounded on a BOND: soulmate, Ally-tier standing, a companion-class role/label, or a close relationship dim; OR the COMMON-ENEMY read `_shares_the_fight` — an escort/guard/martial role or an allied faction, not hostile to the Player, in an active fight; OR a player SUMMON/creation `_is_player_summon` — an ownership attribute pointing at the Player, or a summon-typed non-hostile entity), caps honored; the enlist block now runs on `gate OR active` so a live fight enlists even when the DM never tagged the phase), HP-0 defeats +
XP, and self-ending fights (last foe down = victory; player `defeat_resolve` = defeat;
scene phase moving on = resolved).

**Lint / prompts / surfaces.** `combatant_alive`: prose killing a live-HP row is a
contradiction (death comes from the ledger). Contract `dm-rules/7` (+ War Room teaching,
compact variant included, gated on the knob); tags `world-tags/4` (+[foe]/[clash]);
`RPG_CLASH_CARD` + `clash_record` on the rpg extraction wire (`RPG_COMBAT_OPS`).
`hud_view().war_room` (exact HP, per-row dice, drops, last settled fight) feeds the ST
HUD's combat lane; `state_summary` carries `combat`/`clashes`/`loot` raw.

A `none` session remains byte-identical: `empty_state()` gained no keys, every parser,
pass, block, and wire row is rpg-gated (+ the `war_room` knob), welded by
`test_p12_combat.py` (46 green, incl. the 3v3 party pass — bond / common-enemy / summon bases — none-leak + deterministic replay).

### §F — large-scale battle (1.21.0)

The player fights their MICRO slice in the War Room while the MACRO battle lives in PROSE; only the
outcome for the player is tracked. `state["battle"]` (lazy, so `none`/pre-1.21 checkpoints are
byte-identical): `{active, name, momentum, waves, threat, foe, wave_size, log}`. **Ops:**
`battle_start{name, momentum?, foe?, threat?, wave_size?}`, `battle_wave{}`, `battle_end{outcome?}`
are PRIVILEGED (rule/user/genesis); `tide_set{tide, why?}` is PROPOSABLE (`tide ∈ losing|holding|
winning`). The tide is DERIVED from a code-owned `momentum` (`state.battle_tide`: ≥1 winning / 0
holding / ≤−1 losing — never stored); `tide_set` steps momentum **one toward the reported tide per
turn** (baked `_delta` at `_enrich`, clamped ±3 — the engine owns the pace). **The referee
(`state.battle_ops`)** runs AFTER `combat_ops` on all three combat apply paths (so the turn's
defeats have landed): when a battle is active and the player's enemy rows have CLEARED but momentum
≤ 0 and `waves < BATTLE_WAVE_CAP` (8) → a fresh wave (`_battle_wave_ops`: a band of `wave_size`
foes + `battle_wave`, which nudges momentum +1); once momentum ≥ 1 → `battle_end` victory +
`combat_end`. `combat_ops` defers its victory `combat_end` to `battle_ops` while a battle is live.
**Channels:** the DM's `[battle | <name> | <foe?> | <tier?>]` opens it and `[tide | winning|holding|
losing | <why>]` reports the macro (both parsed by `tier0.parse_battle_tags`, re-sourced as rule
under the `large_battle` knob); OOC `((aether.battle <name> | tide <t> | end))`. The `[BATTLE]`
directive (`compose._render_battle`) rides the volatile tail; the HUD `war_room.battle` chip
(name/tide/waves) surfaces it. Gated by `[specialization].large_battle`; a `none`/knob-off session
carries no battle fingerprint. Welded by `test_p18_large_battle.py` (11, incl. none-leak + replay).

### 7.12 Phase 2 — the living world (1.14.0, plan doc 13 ratified)

Pillar 12 operational: the world moves whether or not the model cooperates. All state is
lazy (`empty_state()` unchanged); every surface is rpg-gated + the `living_world` knob.

**Ops.** `front_add {name, faction?, segments, pace?, consequence}` — an authored PbtA
agenda clock, PRIVILEGED, seed-once (re-seeding never resets progress); `_enrich` bakes
`_fid`/`_segments` (3–12)/`_pace` (1–3). `front_tick {front, reason}` — PRIVILEGED,
`_delta` baked 1; the reducer fills the clock, logs `[turn, reason]`, and on FILL sets
`done` + `revealed` + `filled_turn`. `front_reveal {front}` — PROPOSABLE (a rumor heard in
the fiction is witnessed truth); `_enrich` resolves id|display-name. `route_set {a, b,
segments}` — PRIVILEGED travel-time edge, undirected, slugged + clamped 1–4 at bake.
State keys: `fronts`, `routes` (lazy); `scene.last_move {from, to, turn}` (written only by
rpg-baked `scene_set` ops carrying `_prev_loc` — a `none` op has no key); `clock.
last_advance_turn` (written only by rpg-baked `time_advance` ops carrying `_turn_mark`;
`_day_wrap` marks a wrap for day-paced fronts).

**The referee (`state.world_ops`)** — pure, journaled, on BOTH apply paths after the
progression pass (`pipeline._progress` + `jobs` post-batch), like the combat referee:
(a) TRAVEL — the last committed location change this batch (baked `_prev_loc` ≠
`location`) emits a privileged `time_advance` of `travel_cost()` segments (route override,
default 1); an explicit time move in the same batch wins. (b) IDLE — `clock_turns`
(default 6) turns without real time passing advance one segment. (c) FRONTS — one tick
max per front per batch, reasons journaled: day pace (`day % pace == 0` on a wrap),
`affinity_adj` targeting the faction, `world_flag` touching it, a completed quest sharing
a ≥4-char name token, or a `combatant_defeat` of an enemy sharing one. A tick that FILLS
also commits `world_flag {key: fid, value: "come to a head"}` + a world-event memory.

**Channels.** DM tags (world-tags/5): `[time | <segment>]`/`[time | +N]` (+N clamped to
2; restating the current segment is a no-op; at most one time move per reply) and
`[rumor | <front/faction> | <whisper>]` → `front_reveal` (+ a Rumor memory); the
name-mention floor reveals a hidden front whose name appears in the reply. Volatile tail
(0a constraint): `[TRAVEL]` (fresh move + a deterministic md5 en-route cue — quiet/omen/
"stage an encounter NOW" routed through `[foe]`), `[FRONT]` (fresh fill: narrate the
consequence NOW), `[FRONTS]` (revealed, unfilled standings). Director: binds `front`
(fronts filled 1–4 turns ago) + the `rpg_adventure.front_fallout` beat.

**Visibility (ratified).** Rumor-gating applies to the HUD/briefing ONLY: `hud_view().
fronts` renders revealed clocks (consequence hidden until fill), the ST World tab shows
them as pip bars under "Agendas"; `state_summary` carries `fronts`/`routes` RAW from turn
one — the Console never hides. Knobs: `living_world` (off = 1.13 behavior), `clock_turns`.
Welded by `test_p14_living_world.py` (none-leak + deterministic replay included).
