# Changelog

## 1.21.0 — 2026-07-11

Large-scale battle — you fight your corner of it on the dice while the whole battle rages in prose,
and the fight keeps coming until the tide turns. Bean's macro-battle vision, built on the War Room.

- **Your slice is the dice; the war is the story.** In a big engagement the DM opens a battle
  (`[battle | <name> | <foe?> | <tier?>]`) and reports how the WIDER fight is going with
  `[tide | winning | holding | losing | why]`. You fight whoever's in front of you in the War Room
  3v3; the rest of the field — other units winning or losing, the line holding or breaking — is
  narrated in prose. Only the outcome for you is tracked.
- **It keeps coming until it turns.** While the battle isn't won, clearing the enemies in front of
  you doesn't end the fight — a fresh WAVE presses in. A battle going badly elsewhere means more
  waves; as you cut through them the tide shifts, and once it's turning your way the field is won.
  The engine owns the tide (a clamped momentum, one step per turn) and decides when reinforcements
  arrive — the model narrates, it never fudges the outcome.
- **See it live.** A `[BATTLE]` line rides the prompt (tide + wave count + the standing directive),
  and the War Room HUD shows a battle chip (⚑ name · tide · wave N). New knob
  `[specialization].large_battle` (default on, live-toggle); OOC `((aether.battle <name> | tide <t>
  | end))`.
- **Live-verified (Chrome + SillyTavern + GLM-5.2):** a losing "Siege of Dunmoor" — the player cut
  down the lead raider, a wave of two more surged into the gap, the tide ticked losing → holding,
  and the DM narrated the wider siege ("the rampart holds. Barely") entirely in prose.
- `none` (non-RPG) and knob-off sessions stay byte-identical. Suite 731 passed + 1 skip; ruff clean.
  New `tests/test_p18_large_battle.py` (11): the clamped tide, waves-when-losing, win-when-turned,
  the `[battle]`/`[tide]` tags + OOC, the `[BATTLE]` directive, none-leak, replay.

## 1.20.0 — 2026-07-10

The War Room becomes a real 3v3 — your companions actually fight beside you, and a crowd shows up as
a crowd. Closes Bean's "3v3 is missing" gap.

- **Your side of the fight finally forms.** Companions used to join only at a very high friendship
  score that campaigns almost never reached, so party battles never happened. Now a present ally
  enlists on a real BOND — your soulmate, a companion/lover/comrade role, or a genuinely close
  relationship — not a number that rarely moves. Deep standing still counts; a mere stranger still
  doesn't get conscripted.
- **Battlefield allies — enemy of my enemy.** In an ambush, whoever is fighting the same foes and
  isn't hostile to you stands with you, even with no personal bond: hired escorts, caravan guards,
  faction allies. Bean's example — a merchant caravan's sellswords all fight the bandits to survive —
  now works automatically. Grounded on a protective/martial role or an allied faction, so a random
  bystander isn't conscripted, and a guard who's turned on you is still a foe.
- **Your summons fight for you.** A creature the Player conjures, summons, or creates — a spectral
  wolf, an animated construct, a familiar — enlists on your side by construction (it exists because
  you called it). A hostile caster's construct is still an enemy.
- **The referee no longer needs the scene tagged.** Allies enlist whenever a fight is genuinely
  live (foes on the field), not only when the DM remembers to mark the scene as combat — a
  weak-model-floor fix so the party forms reliably.
- **The DM can now bring in allies, not just enemies.** New `[ally | name | tier? | weapon?]` tag,
  the mirror of `[foe]`: the narrator calls a present companion onto your side and the engine spawns
  them with real HP (a known character fights as themselves, wounds and all). The rules-contract now
  teaches the DM to populate BOTH sides.
- **A group arrives as a group.** When the story numbers the enemy — "three cutthroats", "a pack of
  ghouls" — the combat floor stages the whole band (capped at 3), not just the one you struck. A lone
  foe still stages exactly one.
- **Recruit hint in the HUD.** The War Room lane now shows the one-tap way to call a companion in:
  `((aether.ally <name>))`.
- `none` (non-RPG) sessions and knob-off RPG sessions stay byte-identical. Suite 720 passed + 1 skip;
  ruff clean. `test_p12_combat.py` +18 (party formation, the `[ally]` tag, the named-band floor,
  common-enemy escorts, player summons, contract teaching, none-leak, replay).
- **Live-verified on the bench (Chrome + SillyTavern + GLM-5.2):** a hired caravan guard (no bond,
  neutral affinity) auto-enlisted against ambushing bandits — the War Room HUD showed him as an ally
  with his own action die and both-side initiative, and the DM narrated him fighting the common foe.

## 1.19.0 — 2026-07-10

A leaner rules-contract on calm turns — the biggest per-turn token cut yet — plus a truthful briefing
inspector and a one-click toggle.

- **Auto-compact rules-contract (opt-in).** The standing DM rulebook is ~2,000 tokens injected every
  turn. Once a session is a few turns in, the model has internalized it — so with the new
  `auto_compact_contract` on, the engine flips to the ~40%-smaller **compact** contract on calm,
  established turns, while keeping the **full** contract for the opening turns and every combat turn.
  A live A/B on GLM-5.2 measured the token cut with **no loss of coherence** (checks still called,
  ledger tags still emitted, narration still in-voice). Off by default; enable it per session.
- **Toggle it anywhere, live.** New "auto-compact contract" checkbox in the SillyTavern panel (next
  to "semantic intent floor"), and `POST /aether/specialization` accepts it too — flips with no
  restart. `contract_full_turns` (default 3) sets how many opening turns stay on the full contract.
- **The briefing inspector tells the truth.** `GET /aether/session/{id}/briefing` now reports the
  REAL contract the next turn will carry (`contract_variant` + `upcoming_turn`) instead of always
  showing the full one.
- `none` (non-RPG) sessions and default RPG sessions stay byte-identical. Suite 702 passed + 1 skip;
  ruff clean.

## 1.18.0 — 2026-07-10

The natural-language layer of the RPG engine gets smarter — and your character's identity is now
read straight from the story.

- **Roll what you MEAN, not just what you type.** A new pure-code "reflex floor" (no model, no
  network) reads actions by meaning: "I sweet-talk the guard" or "I haggle her down" now roll
  Persuasion, "I sneaked past" rolls Stealth — not only when you name the skill outright. It uses a
  light word-stemmer plus a curated intent lexicon, and only ever fires for skills your character
  actually has (nothing becomes rollable without an in-world basis). Toggle it live from the Console
  or the SillyTavern panel ("semantic intent floor"); off = the exact-keyword behavior of 1.17.
- **Strikes hit real people, not stray words.** When you attack, the engine resolves your target to
  an actual on-scene character instead of grabbing a noun out of the prose — ending the class of bug
  where a direction ("out"), a place ("the pines"), or your own weapon ("shortsword") could be
  staged as a phantom enemy.
- **"You are Kael" now means your character IS Kael.** When a DM card's opening names your
  character, the engine names your Player Card that (instead of a generic "Player") and never stages
  a duplicate NPC by the same name — so the Dungeon Master always knows who you are.
- `none` (non-RPG) sessions stay byte-identical. Suite 685 passed + 1 skip; ruff clean.

## 1.17.0 — 2026-07-10

The gear overhaul completed (prose effects + manual slotting), the out-of-combat kill system, the
narrator-card boilerplate fix — and, headlined, the ROOT CAUSE of the recurring "phantom combat /
lingering wrong directive" bug. Found in a long-form (social→magic→stealth) live playtest; full
write-up in `PLAYTEST-2026-07-10-Thornhale-RootCause-and-Findings.md`.

- **Phantom combat, fixed at the root.** The attack-verb (and companion) regexes matched with a
  trailing `\w*`, so ordinary words masqueraded as attacks — **"stab" matched "STABLE"**, "cast"
  matched "castle", "bash" matched "bashful". Walking to a *stable* fired the War-Room floor, staged
  a foe (once literally named "Out", from "slip **out**"), forced the scene to climax, and handed
  the model a `[WAR]`/`[OPPOSITION]`/`[DIRECTIVE]` that contradicted a peaceful scene — the LLM then
  spiralled. Verbs now match only real conjugations (base + s/es/ed/ing + doubled-consonant +
  -e-drop) as whole words; directions can never be a foe name. This is a distinct root from the
  earlier stale-directive fixes — the same symptom had several causes. (Not the KV cache.)
- **Gear can change the story, not just the dice.** Gear carries an authored **prose/glamour/lore
  effect** that rides `[GEAR]` and the HUD paper-doll — livetested: the narrator honored a cloak's
  "eyes slide off her in shadow." The Creator's gear box is now structured rows (name · slot · what
  it does); a pinned **slot** overrides the recognizer (the manual failsafe), and mid-play
  `((aether.equip <item> <slot>))` re-slots anything. The HUD shows what each piece does.
- **Out-of-combat kills.** Outside a fight you can't just declare a kill: a stealth/concealed
  approach makes it a real Stealth roll (success = a silent kill + XP), a grand working (epic/mythic
  scope or a reality-warp + a roll) kills by prose + XP, and anything else is a routed NON-MOVE
  (approach unseen, force a fight, or bring overwhelming power). Knob `[specialization].stealth_kills`.
- **Narrator cards drop the boilerplate.** Every card used to end on the same "the world holds its
  breath, waiting on you." — removed; the authored opening scene ends on its own beat.
- `none` sessions stay byte-identical. Suite 672 passed + 1 skip; ruff clean.

## 1.16.0 — 2026-07-10

Combat hardening, an explicit War-Room initiative order, and a much smarter gear-slot recognizer —
plus the Player-HP double-count fix. All verified on a live bench (Hollowmere / Redgate / Velvet
Court) with eyes on the UI.

- **Explicit initiative.** The War Room now shows a turn order — every live combatant plus the
  Player, ranked by a curated, baked initiative score (foes by threat tier, the Player by DEX),
  rendered on a new `[INIT]` directive line, in the HUD combat lane, and taught by the contract
  (dm-rules/9, replacing "initiative is loose"). Deterministic and replay-pure.
- **The opening blow of a floor-started fight lands.** When you attack a foe the DM only narrated
  (no `[foe]` tag), the engine stages that foe AND applies your opening strike the same turn (it
  used to whiff), and raises the scene to a combat phase. The foe's name is read correctly now,
  too — your own weapon ("stab my *shortsword* into…") and nearby scenery ("lunge *from the
  pines*…") are skipped in favor of the actual target (a targeting preposition — "into/at/through
  <foe>" — wins).
- **No more double damage.** A foe the Player struck this turn no longer takes a second hit when
  the DM re-narrates the same blow as an `[hp]` tag; and a Player wound reported twice with reworded
  reasons (the extraction ladder's paraphrase) counts once.
- **Gear slotting actually works.** The slot recognizer was widened enormously — footwear
  (heels, stilettos, pumps, flats, loafers, sneakers…), dresses/gowns/corsets, and the full
  accessory wardrobe now land in the right slot, with plural tolerance and long-only compound
  matching so ordinary words ("husband", "handicap") never masquerade as gear.
- Knobs unchanged; `none` sessions stay byte-identical. Suite 664 passed + 1 skip; ruff clean.

## 1.15.0 — 2026-07-10

The Eranmor fix pack: a live high-fantasy campaign exposed six ways the engine could resolve a
roll correctly and still feel broken around it. All six are fixed.

- **Lost turns re-serve instead of re-rolling.** If a generation dies upstream (an empty reply)
  after the engine already rolled, re-sending the same action now RE-SERVES that settled roll —
  no second dice, no double clock/cost/cooldown, and the directive tells the DM the earlier reply
  was lost in transit. A reply that actually exists still starts a genuinely new turn. Knob
  `[session].reserve_lost_turns` (default on).
- **The directive says which message it resolves.** `[DIRECTIVE]` now states it always resolves
  the Player's NEWEST message (never an earlier one), and when a declared ability could not ride
  the roll (e.g. still recharging) it says so — "narrate a plain attempt, not the technique." The
  RPG state block is placed directly above the newest message. This cuts the model's per-turn
  deliberation over whether a directive was stale.
- **The War Room starts itself.** A DM that narrates enemies but forgets the `[foe]` tag no longer
  leaves combat un-started: attacking a target the DM's own prose put on the scene now stages that
  foe and opens the fight (`[specialization].foe_floor`, default on). Body parts and ungrounded
  names never spawn. The DM's improvised `[CHECK] … | skill: X` roll-calls are also understood now
  (healed into a real armed check), and off-protocol bracket lines get a one-line nudge back to the
  real channels.
- **Turn counting can't drift.** The server turn head is authoritative — the next turn is always
  head+1, so a client counter that ticks on stopped or continued generations can no longer skip
  indices and desync cooldown / regen / mastery timing.
- **Roll notices are visible.** "Recharging", "not a move", "not enough stamina" and the like now
  surface to the HUD instead of only the proxy log; greyed roll buttons no longer fire.
- **The rules contract is never dropped.** Under a tight token budget the DM contract degrades to
  its compact form but always ships — losing the tag grammar mid-campaign was what let the model
  invent its own.

Tests: `tests/test_p9_eranmor_fixpack.py`. A `none` (non-RPG) session stays byte-identical; the
new resolution/staging paths are replay-deterministic.

## 1.14.1 — 2026-07-09

The Creator auto-fill finally completes the boxes (Bean's long-standing bug, live-reproduced
and root-caused this pass).

- **Rows no longer amputated mid-word.** `deterministic_world` hard-clamped every
  'Name — description' faction/location row at 80 chars, so each AI auto-fill returned
  stumps ('…behind hydrogel windo') that round-tripped into drafts, cards, and the ledger.
  Prose-facing fields now clamp WORD-SAFE with honest caps (rows 500, tone/date 160,
  front names 120; `_s_soft`), and `_split_name_desc`'s description tail is word-safe too.
- **Blanks on existing rows now fill.** The 'typed content is canon' merge kept seed rows
  verbatim at ROW level and discarded the model's completed version — an npc's empty
  `home`, a bare faction's description, or a bare custom skill's mechanics could never be
  auto-filled. `_keep_seed_rows` now match-merges per FIELD (`_row_fill`): every typed
  field wins verbatim, blank fields fill from the model's matching row, new rows append.
  The authoring prompt explicitly requires every npc to come back with a `home`.
- Tests: `tests/test_p15_creator_fill.py` (9). No wire/state/hot-path changes; a `none`
  session stays byte-identical.

Plus the Phase 2 live-verify fix pack (Cinderveil bench, all live-reproduced):

- **The camera follows the player (living-world floor).** A DM that never emits `[scene]`
  tags still yields extraction `move_entity` ops on the player — `world_ops` now emits the
  privileged `scene_set` + the travel toll from them (known locations only, never minting).
  Travel time no longer depends on the model's tag discipline.
- **The living-world referee reads the fresh reply.** `pipeline._ingest_reply_tags` ran
  combat_ops but never world_ops, so a reply-committed move bypassed travel/fronts entirely
  (the ladder later deduped it away). The live path now runs the referee too.
- **Location canonicalization: middle-dot + parent rung.** GLM-5.2 writes sub-locations as
  "Vael Thyrr · temple quarter" (middle dot — now a head separator) and "<known place> +
  specifier" ("Ashen Maw rim") — a unique ≥2-token name-head containment now resolves to
  the parent instead of minting a twin location every session.
- **hp_adj same-turn near-dupe dedup.** The DM's `[hp]` tag AND the extraction ladder both
  reported the same wound with slightly different words — a −2 graze landed as −4 (the item
  double-gain bug's hp cousin). Same-turn, same-delta, reason-similar (Jaccard ≥ 0.5)
  proposals now count once.
- Journal inspector briefs carry `to_location`/`front`. Creator chip shows the GLOBAL
  specialization when no session is picked (it claimed "RPG off" while the proxy ran rpg).
- Tests: +5 in `tests/test_p14_living_world.py` (camera floor, middle-dot, parent rung,
  hp dedup) + the updated `test_rpg4` wrong-merge pin. 638 passed + ruff clean.

## 1.14.0 — 2026-07-09

Phase 2: the living world (ratified plan doc 13) — pillar 12 made operational. The world
now moves whether or not the model cooperates, and custom ACTIVE abilities work again.

### Active abilities: authored kind is the truth (bug fix)
- Custom abilities frozen as `kind: "active"` with a flat-bonus mechanic (the Combat-Stims
  pattern: `resolution_mod` + cost + cooldown) were reading as inert PASSIVES everywhere —
  wrongly badged in the ST HUD, rejected at `use` ("it already applies"), and never applied
  at all. Active-ness now honors the authored `kind` (`registry.ability_is_active`), with
  the mechanic as the floor when kind is absent.
- Invoking a flat-burst active (`((aether.check <skill> use <ability>))` or naming it in
  prose) now applies its +N to THAT roll, pays its cost, and starts its cooldown — baked
  into the check op (`_shape.burst`), replay-pure. The Creator freeze keeps cost/cooldown
  on authored mod-actives; `combat_stims`/`adrenal_booster` genre-pack entries carry a real
  cost. `use` on a true passive/basis stays a friendly notice; actives never auto-apply.

### The world clock
- Travel between canon locations consumes day-segments: the committed `scene_set` move
  (baked `_prev_loc`, RPG-only) drives a privileged `time_advance` — 1 segment by default,
  `route_set` edges (Creator-authored `routes`) override up to 4.
- Idle floor: `clock_turns` (default 6) turns without real time passing advance one
  segment on their own. The DM ceiling: a `[time | <segment>]` / `[time | +1]` tag moves
  the clock (+N clamped to 2; restating the current segment moves nothing).
- A fresh move renders a volatile `[TRAVEL]` tail line with a deterministic en-route cue
  (md5 of route+day, the R8c pattern — no journal row): quiet road, omen, or "stage an
  encounter NOW" routing violence through the existing `[foe]` channel.

### Faction fronts (PbtA agenda clocks)
- `front_add` (privileged, Creator/genesis-frozen: name, faction, 3-12 segments, pace,
  consequence) creates a clock the ENGINE advances: `world_ops` (both apply paths, like
  the combat referee) ticks fronts deterministically — day pace, the Player's dealings
  with the faction, world flags touching it, quests resolving against it, its people
  falling in combat. One tick per front per batch; every tick journaled with its reason.
- A front that FILLS commits the consequence as world truth: a `world_flag`, a world-event
  memory, a fresh `[FRONT] ... HAS COME TO A HEAD` directive on the tail, and the
  `front_fallout` director beat (binds `front`) keeps the fallout landing for a few turns.
- Rumor-gating (ratified): fronts exist HIDDEN. They surface in the HUD/briefing only when
  the fiction references them — the DM's `[rumor | <agenda> | <whisper>]` tag (proposable
  `front_reveal`) or the name-mention floor. The Console/state_summary shows every clock
  from turn one (`fronts`/`routes` keys — raw, never hidden).
- HUD: World tab gains an "Agendas" section (revealed clocks as pip bars, fresh fills
  flagged); `hud_view` carries `fronts` + `clock`; `[FRONTS]` briefing line lists revealed,
  unfilled clocks. [TAGS] protocol is now world-tags/5 (adds [time]/[rumor]).
- Knobs: `[specialization] living_world = true`, `clock_turns = 6`. Off = 1.13 behavior.
  A `none` session stays byte-identical (every surface rpg-gated; baked keys only on
  rpg-enriched ops).

## 1.13.1 — 2026-07-09

The "minimized is not broken" fix. After 1.13.0 the player HUD appeared to lose everything
but the hp/stamina/mana bars. Nothing in the ledger or the renderers was actually broken —
the HUD had been left MINIMIZED (`hud.compact` saved as `true` in SillyTavern's settings by
the previous session), and the compact strip carried no hint that it *was* a strip. An
invisible UI state that reads as data loss is a real bug (pillars 17/19), so:

### ST extension (hud-clarity build 2026-07-09)
- The compact strip now labels itself: a full-width `▣ expand — full sheet` button sits
  under the vitals, and one tap restores the whole tabbed sheet (`window.aetherHudExpand`).
- The minimize button shows the state you're IN: `▁` when expanded, `▣` when minimized,
  with matching tooltips (`syncMinBtn`).
- Renderer failures are now VISIBLE: a throw inside any HUD renderer used to leave the
  previous innerHTML on screen forever (stale content that looks like lost data). Both
  `hudRefresh` and the tab switcher now catch, log, and render a `⚠ HUD render error` line
  instead — the ledger is untouched and the reader can see the view (not the truth) failed.
- Per-key hud settings merge: a saved `hud` object from an older build used to REPLACE the
  defaults wholesale, so every newly-added hud key (`hideTags`, future ones) came up
  `undefined` forever. Defaults now merge key-by-key under any saved values.

### Never again: the HUD render guard
- `tests/st_hud_smoke.mjs` runs the REAL `st-extension/index.js` in a stub DOM against a
  full-fat synthetic payload and asserts every surface renders: boot-minimized self-labels,
  expand re-renders (stale-body guard), all 8 tabs emit their content markers, the war-room
  lane renders, and a poisoned payload surfaces the visible error line.
- `tests/test_st_extension_hud.py` wires it into `pytest -q` (skips without node), adds a
  no-node static integrity check (every renderer entry point present + the IIFE still
  closes — catches silent file truncation), and asserts the INSTALLED SillyTavern copy is
  byte-identical to `st-extension/` so the UI can never run stale code.

No wire, state, or hot-path changes; a `none` session stays byte-identical.

## 1.13.0 — 2026-07-09

Phase 1 — the full combat loop + 3v3 party / War Room (plan doc 13, every ratified decision
in). Fights now run on engine-owned combatant instances with exact HP; the model narrates
dice it was handed, never outcomes it invented.

### New: combatant instances (extras vs tracked)
- `combatant_spawn` (privileged) freezes a combat instance AT SPAWN: HP by threat tier
  (minion 6 / standard 14 / elite 26 / boss 44), armament tag, and the loot row all bake
  into the journaled op (replay-pure). Unnamed EXTRAS evaporate when the fight ends;
  TRACKED combatants (known NPCs) reference their entity row — wounds persist in full:
  end-of-fight HP lands on the entity (`attributes.hp`), a survivor below half is visibly
  `Wounded`, a beaten one `Battered`, and the next fight starts from that toll.
- 3v3 cap, player included on the ally side; the reducer rejects a fourth visibly.
- The DM introduces foes with `[foe | <name> | <tier> | <weapon>]` (validated, re-sourced
  as rule — the R8b pattern); the player has `((aether.foe ...))` / `((aether.ally ...))`
  / `((aether.combat end))`.

### New: damage is code-decided
- A check bound to a live enemy (`at <name>`, prose naming the foe, or the lone-foe +
  attack-verb floor) deals outcome-tier × weapon-magnitude damage, applied to the ledger
  before the DM ever writes — the [DIRECTIVE] hands over the exact number.
- Enemy→player harm stays R8c + `[hp]`; DM chip damage and ally blows land through the
  same `[hp | <combatant> | -N]` tag, rerouted onto combatant rows and engine-clamped.
- Every ally rolls ONE pre-decided action die per combat turn (`[ALLY]`, deterministic,
  no journal row — the R8c pattern; visible in the HUD lane, ratified). Initiative is
  loose: the DM weaves the pre-rolled results into one beat.

### New: code-detected defeat, curated XP, frozen loot
- HP 0 is detected by the combat referee (`combat_ops`, both apply paths): privileged
  defeat + XP by threat tier (15/30/60/120) + a deterministic loot roll from the table
  frozen at spawn — Creator/assist-authored `loot_table` rows win, `registry/loot.toml`
  is the floor, drops land as world items. Fights end themselves: last foe down, player
  defeated, or the scene phase moves on.

### New: the record keeps up
- `[clash | A vs B | how | outcome]` + a `clash_record` extraction op: NPC-vs-NPC fights
  stay prose (no dice, Bean's call) but method + outcome commit as facts on REAL rows.
- `combatant_alive` linter rule: narration killing a combatant whose row still has HP is
  a contradiction — death comes from the ledger.
- The War Room HUD lane (combat-phase-only): combatant cards with exact HP, tier,
  armament, the pre-rolled dice, defeat marks and loot chips; a minimized-HUD foe strip;
  `state_summary` carries `combat`/`clashes`/`loot` raw (the Console hides nothing).
- DM contract → `dm-rules/7` (+War Room teaching), tag protocol → `world-tags/4`
  (+[foe]/[clash]); the Creator's world autofill now authors per-tier loot rows, frozen
  at save.
- Knob: `[specialization] war_room = true` (off = 1.12 combat behavior; a `none` session
  carries no fingerprint — checkpoints, wire, prompts all byte-identical).

## 1.12.0 — 2026-07-09

The compression pass (doc-13 items 2–5): the briefing gets leaner without losing a single
committed truth — less for the model to wade through, cheaper turns, fewer stale-fact
false alarms.

### New: compact briefing (opt-in)
- `[injection] briefing_style = "compact"` switches the state blocks to dense notation
  (`here:`, `St:/Sk:/Ab:`, `wear:/exp:`, `rep(Faction: tier)`, capped stowed lists, shorter
  quest notes) with a one-line `[KEY]` legend riding the DM contract. The default stays
  `verbose` and renders exactly as 1.11 did.

### New: facts retire instead of rotting
- Restating a fact now supersedes the old record — it's kept and labeled (`retired`,
  `superseded_by`), never deleted, and stops feeding the L10 contradiction check (the known
  stale-fact false-positive source). A `fact_retire` op (Console/OOC/engine only — the model
  can never erase truth) retires by id or text match. Near-duplicate lore memories no longer
  land twice.

### Scoping: the briefing follows the scene
- An explicitly-absent NPC's pose, clothing, statuses, and drives stay in the ledger but out
  of the prompt until they're back on stage (your soulmate/nemesis always rides). Only the 3
  most-recently-touched active quests carry their stakes/notes — older ones stay listed by
  name. Stale rolls were already gone from prompts; now it's pinned by test.

## 1.11.0 — 2026-07-09

Phase 0b — the notables gate and the player's voice. Fixes the two "main character syndrome"
bugs: notables no longer wander into scenes without an in-world basis, and every NPC now
carries a structural answer to "does this person actually know the Player?" Plus L11: the
engine now enforces that the DM never decides for you.

### New: home anchors + [NEARBY]
- Notable NPCs authored in the Creator carry a `home` location (a new field, AI autofill
  writes one per NPC). Anchors are frozen at creation. When the scene is at a notable's home
  and they're not on stage, a compact `[NEARBY]` line tells the DM who is plausibly here —
  anchored-elsewhere notables spend zero prompt tokens.
- The presence heuristic is stricter under RPG: only the NARRATOR placing someone in the
  scene stages them — a player wondering "I hope Marla arrives" no longer summons anyone.

### New: the knows-player gate (anti-main-character)
- Every on-scene NPC without a relationship row renders as `stranger`; with only a faction
  standing, as `by reputation (Faction: tier)`; a real relationship shows its tier as before.
  The DM contract (now dm-rules/6) enforces it: strangers don't recognize the Player, and
  recognition must be earned in play.

### New: L11 — your voice is yours
- Outside an open bracketed intent like `[I persuade Jerald.]`, the DM deciding for your
  character ("Bean agrees to the terms…") is flagged and corrected next turn. Inside one,
  the door is open — but a line you wrote in quotes must appear VERBATIM; a paraphrase is
  flagged. The open bracket also stands L9 down for that turn (it was always meant to be
  the one door). RPG-only; base sessions are unchanged. Disable with `rules_off = ["L11"]`.

## 1.10.0 — 2026-07-09

Prompt caching (KV-cache) enablement: long roleplay prompts are exactly the shape provider
prompt-caches reward — a huge stable history plus a small volatile tail — so AetherState now
routes, measures, and (optionally) pre-warms that cache. Cheaper input tokens and faster
time-to-first-token on providers that support prefix caching (e.g. Venice); on providers that
don't, nothing changes.

### New: cache-aware requests
- Enriched requests carry `prompt_cache_key=aether-<session id>` so every turn of a
  conversation lands on the same warm cache server. Requests AetherState doesn't touch stay
  byte-identical, and a key your frontend sets itself always wins. `[upstream] cache_key =
  false` turns it off.
- Injection-position audit: the default `depth` placement already keeps all volatile state
  (briefing, rolls, [DIRECTIVE]/[OPPOSITION]) at the prompt tail, and the sentinel never
  reaches upstream — the long history prefix stays byte-stable turn over turn. Using
  `placement="system_merge"` breaks that (volatile bytes in the FIRST message); the proxy now
  logs a notice when caching is on with that placement.

### New: cache hit-rate visibility
- `GET /aether/status` gains a `.cache` block (requests observed, responses with usage, hits,
  cached/prompt token totals, whole-run hit rate, prewarms) and the Console Status tab renders
  it as a "prompt cache" row — the proof the caching actually works, in plain sight.
- `[upstream] include_usage = true` (opt-in) asks a streaming upstream to report token usage
  (one spec-standard SSE chunk) so hit rates are measurable with streaming frontends.

### New: chat-open prewarm (opt-in)
- `[upstream] prewarm = true`: when you open a chat, the proxy quietly re-sends that session's
  last prompt with `max_tokens=1` so your first real message hits a warm cache. Costs one
  full-price prefill per warm; at most one per session per 4 minutes; fully fail-open.

## 1.9.0 — 2026-07-09

The Greywater playtest release: a full fresh-campaign live test produced a fix pack across the
Creator, the card, the ledger, and the HUD — plus three new mechanics: DM-called checks that
fire themselves, enemies that attack on real dice, and a "what the AI sees" inspector.

### New: the DM's own check-calls auto-fire (R8b)
- When the narrator says "that's an ((aether.check persuasion))" and you answer in plain prose,
  the engine now rolls it for you — no syntax to retype. Your own explicit or written-out checks
  always win; a `none` session is untouched. `[specialization] auto_dm_checks = false` disables.

### New: enemies attack on real dice (R8c)
- Combat turns inject a pre-rolled `[OPPOSITION]` die: if any hostile moves against the player,
  the DM narrates the tier the dice already decided (miss/graze/hit/crit) and tags the
  pre-decided damage — it can no longer wave enemy attacks through or decide them itself.
  Arms when a Cold-or-worse NPC is present, the scene phase is combat-like, or a combat world
  flag is set; the contract teaches the DM to raise the phase when violence starts.
  `[specialization] enemy_rolls = false` disables.

### New: briefing inspector
- `GET /aether/session/{sid}/briefing` returns exactly what the engine would inject into the
  next request (state header, DM contract, token counts after the budget governor).

### Creator: custom abilities survive with their real mechanics
- The custom-ability row now carries mechanic / applies-to / magnitude / cost / cooldown, so an
  AI-authored dice-shaper no longer degrades into an inert "all checks" trinket on save.
- A mechanics-bare echo of a preset ability (the author liked to restate Silver Tongue) is
  dropped — the curated definition wins; a REAL override (with mechanics) is kept.
- A missing mechanic is inferred from the effect text ("roll an extra die…" → edge/extra_die).
- Custom skills with no `governs` verbs get them derived from the name, so "I dive" still rolls
  a rank-0 Free Dive; NL detection now sees rank-0 custom skills at all.
- What you TYPED is canon: the AI autofill can no longer rewrite or truncate your own faction /
  location / NPC / custom rows — it only fills blanks and appends new ones.
- The authored character is draft-saved like the world; the session picker defaults to
  "card only (no session)" so an accidental Save can't land in your newest live game.

### Cards and seeding
- Card greetings/descriptions no longer truncate mid-word ("find out wh…"); prose clamps cut at
  sentence boundaries with room to spare.
- The opening scene seeds ITS OWN location (matched from the scene text) instead of blindly the
  first location row.
- A Narrator/world card is no longer staged as a present "character" in the cast.
- Starting gear that is worn by nature ("dive-rig") auto-equips; an explicit `(worn)` /
  `(carried)` tag in any item name is honored as the author's word.

### Ledger correctness
- Same-turn duplicate affinity reports (the DM tag + the extraction ladder both reporting the
  same fact) count once — +4 no longer becomes +8.
- Number-word counts split into quantities: "two spent King's Coins on a cord" is now a
  King's Coin x2, so spending one actually decrements.
- `[DIRECTIVE]` no longer crashes on a defeat that lands in the same request as fresh checks.

### HUD / extension
- The Items tab shows EVERYTHING carried (stowed gear included); every stowed piece has an
  equip button aimed at a sensible free slot; the paper-doll's twelve "— empty —" rows collapse
  to one line; the minimized HUD shows the last-roll chip; the Rolls feed carries the dice spec
  and marks DM-called rolls.

### Ops & hygiene
- `/aether/status` reports the REAL version (pyproject is the single source of truth) instead
  of a hardcoded 1.0.0 — the "is my proxy stale?" trap is closed.
- The proxy log no longer drowns in HUD/status polling lines (`[server] log_polling = true`
  restores the firehose).
- The scene-tag protocol pins `phase` to setup|rising|climax|lull and bans invented bookkeeping
  tags; contract bumped to dm-rules/5, tags to world-tags/3.

## 1.8.2 — 2026-07-09

Inventory fixes: items stop duplicating, counts live in the quantity (not the name), and using a
consumable removes it.

### Fixed: items no longer show up twice
- When the narrator's `[item gained]` tag and the background extraction both caught the same
  acquisition in one turn, it stacked to x2. A same-turn re-gain is now one event; a genuine
  re-acquisition on a LATER turn still stacks.

### Counts ride the quantity, not the item name
- A count baked into a name ("Verdan Sap Vial (30 doses)", "Health Potion x3") is split into the
  quantity, and the narrator is told to record counts in qty and NEVER in the name. Descriptive
  parentheses like "(parchment)" are left alone.

### Using a consumable removes it
- `[item lost | char | Item | qty?]` now honours a quantity and removes the item from the ledger
  when the last one is used up; the narrator is instructed to emit it whenever a dose/charge is
  spent — so consumables actually leave your inventory.

## 1.8.1 — 2026-07-09

Reliability fixes for the new rolling: the outcome always reaches the model, stale rolls stop
confusing it, detection is more sensitive, and manual + auto rolls can't double up.

### Fixed: the [DIRECTIVE] now delivers reliably and never goes stale
- The resolved outcome is briefed to the model as EXACTLY the check(s) rolled on this send (not
  matched by a drifting turn counter), so a roll that lands in AetherState always reaches the
  narrator — and a previous turn's roll can never linger in the prompt and confuse it.

### More sensitive detection, no accidental double-rolls
- Natural-language detection also recognises a skill's curated action verbs ("I sneak" → Stealth),
  not just its exact name. And if you already wrote an explicit `((aether.check ...))` yourself,
  auto-detection stands down for that message — you never get two different rolls at once.

## 1.8.0 — 2026-07-09

Roll by *writing* it — natural-language roll detection — plus a clearer Skills-vs-Abilities model in
the HUD and the last roll's outcome always in view. A non-RPG (chat) session is byte-identical.

### New: name a skill or ability and it rolls
- You no longer have to type `((aether.check ...))`. When your message names a skill or ability you
  own — "I use Fire-Slash on the monsters" — AetherState detects it (case/hyphen/space-insensitive),
  rolls the governing skill (an ability maps to the skill it applies to and is invoked if active),
  and hands the narrator the decided outcome. Unowned or unknown names never fire (the eligibility
  gate holds). Explicit `((aether.check ...))` and the Rolls-tab buttons still work.

### Clearer Skills vs Abilities, and visible outcomes
- The 🎲 Rolls tab separates **Skills** (what you roll) from **Abilities** (what bends a roll), with
  active abilities as one-tap "roll its skill + invoke it" buttons and passives shown as always-on
  tags. The HUD's vitals strip shows the **last roll's SUCCESS / PARTIAL / FAIL** at a glance.

### Swipes re-roll
- Regenerating (swiping) a reply now re-rolls the check with fresh dice.

## 1.7.0 — 2026-07-09

A one-tap Rolls tab in the SillyTavern HUD, a fix for skill checks that could silently stop
resolving after a page reload, and a hardening pass on the optional local NLI helper. A non-RPG
(chat) session is byte-identical to before.

### Fixed: a skill check could silently stop resolving after reloading the chat
- The companion extension keeps a per-turn counter that resets whenever SillyTavern reloads the page
  or you switch chats. The proxy had been trusting that counter as the authoritative turn number, so
  after a refresh a new message could be filed under an *earlier* turn than the one actually in play.
  The dice were still rolled — but the `[DIRECTIVE]` was written for the current turn and never saw
  the result, so the narrator was handed no outcome to narrate. The server's own turn head is now
  authoritative: a new message is always the next turn, and a client-supplied turn is honoured only
  when it moves the story forward, never backward. Covered by a regression test.

### New: Rolls tab — one-tap skill checks
- The HUD gains a **🎲 Rolls** tab listing your character's skills as buttons. Tapping one drops the
  matching `((aether.check <skill>))` into your message box without overwriting anything, so you can
  stack several, add your own narration, and send. A custom box takes any skill slug (or a full
  `((...))`), and active abilities show how to invoke them on a check. The engine still rolls the
  dice and writes the outcome — the button only writes the call.

### Hardened: the optional NLI helper can't stall turn processing
- When the local ledger-contradiction helper (`linter_nli`) is slow or offline, its cold-path pass
  is now time-bounded and fails open, so it can never hold up a turn's background processing.

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
