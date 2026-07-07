# Changelog

## 1.2.0 — 2026-07-07

The ledger keeps up with the story: every recording gap found in live play is closed, and the
full progression capstone lands — XP, levels, mastery that grows by use, resource pools,
consequences, and defeat. Everything below is inert unless `[specialization].name = "rpg"` —
a non-RPG session's requests stay byte-identical.

### The world-tag protocol (R10) — the recording floor
- The narrating model can now commit EVERY kind of tracked truth inline, and the engine owns
  it: `[scene | <place> | <phase?> | present: <names?>]` (scene moves + who's on stage),
  `[item gained/lost | <char> | <Item> | <qty?>]`, `[quest | <Name> | new|update|complete|
  failed|abandoned | <note?>]`, `[affinity | <target> | ±N | <why>]`, `[hp | <char> | ±N |
  <why>]` — all parsed deterministically from the settled reply, clamped, quarantined
  visibly, and fed back as committed state. Same propose-then-commit spine as the status
  tags; the prose is never the truth.

### Items, quests, and consequences become ledger truth
- **Organic item channel.** `item_gain`/`item_lose`: acquisitions the story grants become
  real inventory instances — a curated-template name grounds its mechanics, any other name
  commits MECHANICS-FREE (no power from prose), re-gains stack instead of duplicating, and
  losing what the ledger doesn't show is a visible reject. The Creator now seeds STARTING
  GEAR as instances (new sheet field) and records the opening quest in the quest ledger.
- **Quest ledger.** `quest_add`/`quest_update` + a `[QUEST]` block that renders active
  objectives (stakes, notes) and recent resolutions; near-duplicate objectives merge.
- **Bounded HP channel.** `hp_adj` with a baked per-op swing clamp — the narrator proposes
  severity, the engine owns the number.

### Progression (the doc-10 capstone) — code-awarded, never asserted
- **XP & levels.** Quest completions (by stakes), fulfilled goals, and won-over standings
  award curated XP; levels grant +HP, +pools, and banked stat points. All privileged ops —
  the model can't type a number that sticks.
- **Mastery by use.** Every resolved check ticks its skill (anti-grind scene cap); named
  brackets (Novice → Grandmaster) add a curated bonus to the effective mod, and crossing a
  bracket triggers a cold-path assist re-authoring of the skill's frozen definition (the
  evolution loop — with the curated bump as the no-assist floor).
- **Resources.** Skills may carry frozen stamina/mana costs, charged on attempt (failure
  pays half); pools regen on scene changes and rest. Untracked pools waive costs — weak
  setups keep playing.
- **Consequences & defeat.** Critical failures leave a mark (Strained; Backlash when you
  overreached your scope — and reaching FAR past mastery now fails outright). HP 0 routes to
  a contextual non-lethal outcome (captured / robbed / rescued / wake safe) narrated from a
  directive — or to death under the new `hardcore` flag.
- **Adventure beats & search.** A director beat pack (stale quests, wounded player, defeat
  aftermath, missing hook) plus `GET /aether/session/{sid}/search` over the memory ledger.

### DM contract v3 + live-play fixes
- **dm-rules/3:** a resolved check settles THIS attempt NOW — no stalling a rolled outcome
  into an open negotiation. The tag protocol teaches standing/harm recording insistently.
- Robustness from a 23-round live playtest: model JSON with unescaped inner quotes now heals;
  a first-name reference can never mint a twin of the player; AI-filled characters always
  spend their stat points and KNOW their own custom abilities; extraction survives proxy
  restarts without a model hint; duplicate lore/memory writes are guarded; the SillyTavern
  panel's Creator link always targets the current chat.

## 1.1.0 — 2026-07-06

RPG mode grows up: items, a character creator, statuses & conditions, a social plane, and
persistent locations. Everything below is inert unless `[specialization].name = "rpg"` — a
non-RPG session's requests stay byte-identical.

### RPG-3b — Affinity, Factions, Bonds & World Flags
- **The affinity ledger.** NPC/faction standing is journaled truth (reason-tagged deltas,
  per-turn clamped, value ±100) rendered as tiers (`Nemesis … Neutral … Devoted`), never raw
  sentiment. NPC shifts ripple deterministically to their faction (configurable factor).
- **Factions & world flags.** Factions are entities with standing circumstances; `world_flag`
  sets global or faction-scoped facts (`plague=spreading`, `at_war=yes`) that render in
  `[WORLD]`/`[FACTIONS]` and feed back every turn.
- **Bonds.** `set_soulmate`/`set_nemesis` — privileged, unique, earned (Devoted-eligibility);
  the `one_soulmate` linter rule guards structural integrity. OOC set-paths + Console editors
  for all of it.

### RPG-4 — Persistent locations, degradation, inspectors
- **Location canonicalization + registry.** Free-prose place names trim to canonical ids with
  learned aliases and visit counts — no more paragraph-long location ids; revisits resolve to
  the same row. Discovery observes ≥2-turn evidence and creates locations ONCE.
- **Compact rules contract** (`[specialization].contract = "compact"`) for tight token budgets.
- **Inspectors:** `GET /aether/session/{sid}/journal` + the Console "Recent activity" feed
  (applied ops + rolls, briefs carry their text).

### Creator quality — genre packs & the ability taxonomy
- **Genre packs.** The curated preset floor now follows the world's genre: sci-fi, cyberpunk,
  post-apocalyptic, modern, and historical sheets hide fantasy-flavored entries (no more
  Spellcraft on a starship) and offer genre-true skills/abilities whose picks freeze into the
  character's own defs — with working eligibility gates (Systems Intrusion requires a Neural
  Lace the way Spellcraft requires the Arcane Gift).
- **Abilities have identity:** `passive` (permanent edge), `active` (spendable surge), and
  `basis` (grants the in-world basis for a gated skill). Skills are things you TRY; abilities
  are things you HAVE.
- **Authoring reliability.** AI fill runs at creative temperature with a real timeout (no more
  silent template fallback), honors every filled field as canon, spends stat points, ranks the
  concept-defining skills, and its cross-references are validated at freeze (a passive boosting
  a misspelled skill id is resolved or dropped, never frozen dead). Clean entity naming from
  "Name — description" rows. Honest errors: an AI failure reports itself; templates are an
  explicit button.
- **Creator UX:** session switcher, named world/character presets, a Session review tab showing
  the committed world + Player Card, model picker, creator-first saves mint the session
  (no more 404), genre-true placeholders, rank steppers that show authored ranks.

### Genesis & session hygiene
- Chat-open genesis resolves its model reliably, retries on hard failure instead of locking
  out, and honors swiped greetings. One player per session — the Creator's character REPLACES
  the placeholder (no more duplicate 'Player' companions). Narrator endings stay in-fiction
  (dm-rules/2); known-but-absent NPCs stay offstage until the fiction earns them.
- Console Connection tab fixed (keyless probes, in-place saves, `[upstream].model` default for
  engine-initiated calls); config writes are atomic; the test suite can no longer clobber a
  live config.

### RPG-3 — Statuses & Conditions + the eligibility gate
- **The effects ledger.** Statuses (combat buffs/debuffs) and Conditions (anything in-world:
  Cursed, Blessed, Drunk, Diseased, Pregnant…) are engine-owned state on the player and every
  tracked character — the model proposes, the engine commits, and the committed `[EFFECTS]`
  block is fed back every turn so the story can't drift or forget.
- **Inline tag protocol.** The narrator can mark changes directly in prose —
  `[status gained | <char> | Bleeding | negative]`, `[status lost | …]`,
  `[condition gained/lost | …]`, `[valence shift | …]` — and the engine commits them to the
  ledger deterministically. The tag grammar + a preset slice ride the `[RULES]` contract every
  request, so long chats never lose the protocol.
- **Presets + open vocabulary.** ~22 curated presets (`registry/effects.toml`, user-overridable)
  carry engine-side mechanics (check mods, durations, valence defaults) baked at commit; new
  effect names invented by a strong model still commit as open-vocabulary records. Valence
  (negative/neutral/positive) is dynamic — shiftable in play, never hardcoded. Data-driven
  requirements (e.g. Pregnant → female characters only) reject with a visible reason.
- **The eligibility gate.** A skill can require an in-world basis (`requires_ability`): declaring
  it without one is a NON-MOVE — no roll, a visible notice. Power is acquired in-world through
  the new privileged `ability_grant` op (quest rewards, rituals, user), never asserted into
  existence. New scope system: `((aether.check <skill> … scope minor..mythic))` scales odds
  (−2 per step past your rank) and caps the outcome ceiling — freedom at the top, coherence by
  difficulty, never a flat veto.
- Active effect mods flow into every skill check alongside gear; effects render in the Console
  as per-character chips with one-click removal; `GET /aether/registry` exposes the presets.

### RPG-2 — Inventory & Gear
- **Template+Instance items.** Curated templates (`registry/items.toml`) mint into unique
  instances with baked snapshots — editing a template never rewrites an owned item. Six ops:
  privileged `item_mint`; proposable `item_move`/`item_equip`/`item_unequip`/`item_consume`/
  `item_transfer`, all transactional with full rollback (no item duplication, ever).
- **`[GEAR]`/`[INVENTORY]` blocks**, 16 gear slots (extensible via `meta.toml extra_slots`),
  container capacities, equipped-item mods flowing into skill checks and the `[PLAYER]` block.
- **`one_instance_one_place` linter rule:** self-healing state-integrity net for the item plane.

### World Generator & Character Creator
- A standalone Creator window (`/aether/creator`, ST panel link + `/aether-creator` slash):
  world-first authoring with 8 genre templates, registry-driven point-buy, skill/ability pickers,
  and optional assist-LLM fill-the-blanks — freestyle mechanics are authored, clamped, and
  FROZEN into per-character defs (nothing freestyle at roll time). Persists via shipped ops only.

### Fixes
- `[PLAYER]` skills render as effective check mods (registry-derived) instead of raw ranks.
- `[DIRECTIVE]` now narrates every check resolved in a multi-check turn.
- `outcome_match` anti-fudging selection is turn-scoped (a later plain roll can't hide a check).
- Creator: freestyle-authored skills/abilities are rankable/known via the frozen defs overlay.

## 1.0.0 — 2026-07-04

First public release.

- Transparent, streaming-safe OpenAI-compatible proxy with a fail-open guarantee: AetherState
  can never block, edit, or crash the story stream.
- Session engine: multi-chat identity (per-request sentinel wins over stale headers), branch
  alignment, swipe/regenerate handling, duplicate-request protection.
- Two-stage genesis seeding from the character card + greeting (rules pass inline, full-matrix
  helper-LLM pass in the background), with `/aether-genesis` force re-seed.
- Tier-1 extraction ladder with capability probing (native grammar → strict JSON schema →
  JSON mode → freeform), per-op validation, quarantine, and entity discovery.
- User-set update cadence (`cadence_turns`, 1 = every turn) and transcript intake budget
  (`intake_chars`) — newest turns always ship whole, leftover budget carries earlier context.
- Idle settle + restart recovery: the newest turn extracts without waiting for your next
  message, and pending work resumes after a proxy restart.
- Memory tiers (episodic → summaries → durable facts) with recall injection; director beats;
  consistency linter; consent/safeword system; user-voice guard.
- Built-in web Console (sessions, live state view/edit, connection setup with real auth test).
- SillyTavern Companion extension: panel, slash commands, turn-0 seeding, cadence controls.
- Antivirus hardening baked in (TLS truststore injection, SSLKEYLOGFILE workaround).
