# Changelog

## 1.6.1 — 2026-07-08

A focused fix for the cast/scene tracker, plus a cleaner startup for the optional local NLI helper.
A non-RPG (chat) session is byte-identical to before.

### Places and skills no longer get tracked as people
- When the story named a place, a skill, or a stray word in a scene's "who's here" list, AetherState
  could mint it as a present *character* — cluttering the Cast panel with things that aren't people
  (often flagged "here") and muddying the model's briefing. Scene presence and movement now only
  ever refer to a cast member that already exists: an unknown name resolves to a real character or is
  quietly ignored, never invented. This also clears a duplicate-character glitch where one NPC could
  show up twice (e.g. "Marla" and "marla"), and a real location named as "present" is no longer
  staged as a person.

### Local NLI shim: quieter, honest startup
- The optional local contradiction-checker (`nli-shim`) no longer prints an alarming
  "UNEXPECTED roberta.pooler" load report on every start. Those pooler weights are simply unused by
  the sequence-classification head — expected, not an error — so the shim states that plainly and
  drops the noisy report and the unauthenticated-Hub advisory.

## 1.6.0 — 2026-07-08

Building a world in the Creator and actually playing it is now one smooth path: the Narrator card
you generate carries your world **and** your character, so a brand-new chat is already set up. A
non-RPG (chat) session is byte-identical to before.

### The Narrator card carries your world now
- Build a world + character in the Creator, click **Generate Narrator card**, and the card embeds
  the whole thing as a seed. Import it into SillyTavern, open a **new chat**, and your world, its
  opening scene, and your Player sheet are already there — no going back to re-apply anything.
  (Before, you had to save to a session, keep that session blank, make the card, then re-apply the
  same world + character to every new chat by hand.)
- Generating the card no longer needs a session at all — it's built straight from the form.
- Deterministic and safe: the seed commits with no AI call, and it never overwrites a chat that
  already has a world or character, so re-opening an existing game leaves your progress untouched.

### Creator quality-of-life
- The session picker now shows each session's **world and character names** and marks the newest,
  and a **refresh button** updates the list without wiping what you've typed.
- Your in-progress work is **auto-saved in the browser**, so a refresh or reopening the Creator
  never loses it — and the form now opens on a clean slate for a new world instead of silently
  loading an existing session's contents.

## 1.5.0 — 2026-07-08

Two opt-in additions for keeping the story consistent, plus more flexible helper routing. A default
(chat) session is byte-identical to before — everything here is off unless you turn it on.

### Contradiction checking (L10)
- A new **optional** background check flags when the narration **flatly contradicts a committed
  fact** and nudges the next turn to stay consistent. It never rewrites the current reply, and fires
  only on a real contradiction — new detail the ledger doesn't cover is left alone.
- Off by default. To enable, point `[assist.groups] linter_nli` at a small NLI model. A ready-to-run
  local one now ships in **`nli-shim/`** — run `setup-nli.bat` (Windows) or `bash setup-nli.sh`
  (Linux/macOS) and it serves an OpenAI-compatible endpoint you can route the check to.
- Tunable via `[linter] nli_threshold` (default `0.85`); silence it with `[linter] rules_off = ["L10"]`.

### Per-group helper endpoints
- Each background helper job (contradiction checking, memory reflection, embeddings) can now use its
  **own** endpoint via `[assist.group_endpoints]` — so, e.g., contradiction checking runs on a local
  model while memory reflection uses a cloud one, at the same time.
- Set it from the Console (**Connection → Assist routing**, which also gained a multi-endpoint
  editor) or the SillyTavern panel. Leave it unset and every job uses the first assist endpoint, as
  before.

## 1.4.0 — 2026-07-07

Six fixes to RPG mode that make the sheet, the gear, and the timing behave the way you'd expect.
As always, a non-RPG (chat) session is byte-identical to before.

### Gear you're wearing actually shows as worn
- Starting gear that's obviously worn — a helmet, a coat, boots, a sword — now **auto-equips onto
  the paper-doll** the moment your character is created, instead of sitting in a bag.
- **Gear and inventory are split by kind, not by whether it's in a slot.** Gear = weapons, armor,
  tools, accessories, bags (equipped *or* stowed). Inventory = consumables, materials, devices,
  odds and ends. The HUD gains a dedicated **🎒 Items** tab beside **⚔ Gear**, and a sheathed
  sword reads as gear, not inventory.

### The newest reply is taken in immediately
- State now updates from the **latest** thing the story just said — its item/scene/quest/status
  tags commit and the background state pass runs the instant a reply finishes, rather than a turn
  behind. Re-rolling (swiping) a reply retracts the change and re-reads it from the new one. A
  setting (`extraction.live_recalc`) restores the old one-turn-behind behaviour if you want it.

### Make your own skill & ability categories
- Custom skills and abilities can carry **any category you name** — "Spells", "Cyber-Ware",
  "Disciplines" — and the HUD groups them under that heading. Pick a suggestion or type your own.

### RPG mode feels like an RPG
- The Game Master now **drives the dice**: when you try something uncertain it calls for the exact
  check inline and stops for your roll, instead of narrating past it. RPG mode also gets more room
  in the prompt so its rules and your full sheet are always present.

### A roomier, open-ended Creator
- The AI world/character autofill **no longer gets cut off** partway through a big sheet.
- Add as many **free-form world & character detail categories** as you like — a magic system, a
  history, a backstory, a code of honor — kept as retrievable lore that surfaces when relevant.


## 1.3.0 — 2026-07-07

Skills and abilities become two genuinely different things, and you finally get to *see* your
whole character. Everything below is RPG-mode only — a non-RPG session is byte-identical.

### Abilities now shape the dice (they don't just buff a number)
- A **skill** sets the modifier you roll. An **ability** bends the dice: **advantage** (roll an
  extra die, keep the best), a **guard** against critical fumbles, a **second-chance extra die**
  when a roll misses, a **reroll**, or a **surge** that adds a big bonus *and* lifts the ceiling on
  an ambitious attempt. There's still a humble flat "+1" tier and the "basis" marker that unlocks
  gated skills.
- Passive edges apply on their own; you invoke an active in a check with
  `((aether.check <skill> use <ability>))`. Actives cost a resource and have a cooldown, and the
  on-miss ones only spend when they actually fire — so they feel like insurance, not a tax.
- The Creator can author bespoke abilities with these mechanics; they're clamped and frozen into
  fixed rules before they can ever be rolled (nothing freestyle at the table).

### A player HUD — see everything you have, always
- A movable, themeable in-page window with tabs: **Char · Skills · Abilities · Gear · Status ·
  World**, and your vitals (HP / stamina / mana) always visible on top.
- **Skills** print the dice rules right there (how a roll resolves, what the tiers mean).
  **Abilities** are grouped into Spells / Techniques / Talents, each spelling out its mechanic,
  cost, cooldown and what it applies to. **Gear is a paper-doll** — labeled equip slots for
  weapons, armor and trinkets, worn items shown with their bonuses, empty slots clearly marked,
  and one-click equip / unequip / use. **Status** always shows your statuses, conditions and
  diseases. Open it from the panel or `/aether-hud`; the Console mirrors the same view.
- **Edit from the HUD.** An edit toggle lets you spend banked stat points, equip / unequip / use
  gear, adjust HP, and clear statuses right from the window (the Console has the same controls);
  a compact/minimized mode collapses it to a vitals strip.

### A Narrator card named after your world
- The Creator can generate a **Dungeon-Master card built from your committed world** — named after
  the world, opening on its first scene, seeded with its setting, factions and places, with a
  genre-tinted avatar — so you can see which world you're traversing from your character list.
  Optionally auto-installs into your SillyTavern characters folder.

### Cleaner play
- The DM's raw ledger tags (`[hp | …]`, `[scene | …]`, …) are now hidden from the reader — the
  engine still parses them, you just see clean prose. Toggle in the extension if you want them.
- A gated skill you actually have the basis for no longer warns "needs a basis," and an on-miss
  ability that fires but can't save the roll is narrated honestly (no phantom rescue).
- The extension backs off its polling when the proxy is offline, so a stopped proxy no longer
  floods the browser console with failed-request errors.

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
