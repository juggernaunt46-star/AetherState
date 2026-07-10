# AetherState

**A living state tracker for AI roleplay.** AetherState sits invisibly between your RP frontend
(SillyTavern, RisuAI, Agnai — anything OpenAI-compatible) and any OpenAI-compatible model API,
and keeps a persistent, structured "story bible" while you play: who is present, what everyone
wears and carries, moods, arousal, relationships, obsessions, cravings, goals, secrets, consent,
positions, the scene clock — extracted automatically from the chat and injected back into the
prompt, so the model stops forgetting.

- **Genesis seeding** — the moment you open a chat, the character card + greeting become a full
  starting state: clothes, personality-implied gear, moods, obsessions, relationships. Also tracks mature/NSFW-capable state (arousal, relationships, consent) alongside mundane states.
- **Live tracking** — a helper model reads new turns as you play and updates the state
  (every turn by default; set any cadence you like).
- **State briefing injection** — a compact, token-budgeted state header rides along with your
  prompts. The model sees the tracked truth, not its own guesses.
- **Director & linter** — beat suggestions and consistency checks (a corrective note when the
  prose contradicts tracked facts).
- **Memory** — events condense into summaries and durable facts, recalled when relevant.
- **Console** — a built-in web dashboard to watch and edit state live.
- **RPG / DM mode (optional)** — a full code-authoritative game layer: real dice, a Player Card,
  a World & Character Creator, items & gear, dice-shaping abilities, statuses & conditions,
  factions & affinity, persistent locations, quests, a complete progression system (XP, levels,
  mastery that grows by use, resource pools, defeat & optional permadeath), and a **tabbed player
  HUD** that always shows your sheet. Off by default; a non-RPG session is byte-identical to plain
  AetherState.
- **Fail-open by design** — if anything inside AetherState breaks, your chat continues untouched.
  It never blocks or edits the story stream.
- **Local-first, private** — everything lives in a local SQLite file. No telemetry; there is no
  switch to turn telemetry on, because it does not exist.

Works with hosted APIs (Venice.AI, OpenAI, OpenRouter, ...) and local engines (KoboldCpp,
llama.cpp, Ollama, LM Studio, vLLM, oobabooga). But actively developed, tested primarily against SillyTavern + Venice/OpenRouter.
Disclosure: I'm mostly a vibe-coder, and mainly a hobbyist that did this for myself, and thought it would be good enough to share.

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) — tick **"Add python.exe to PATH"**.
2. Unzip AetherState anywhere.
3. Double-click **`Start-AetherState.bat`**. The first run installs everything (a minute or two),
   then the **Console** opens in your browser. If the page doesn't load, wait a moment and refresh.
4. In the Console, open the **Connection** tab:
   - **Main model** (writes the story): endpoint + API key, e.g. `https://api.openai.com/v1`.
   - **Helper model** (tracks state): the same service, or a small local model. The *Connect test*
     verifies the key with a real call.
5. Point your frontend at the proxy (SillyTavern shown):
   - API: **Chat Completion** → **Custom (OpenAI-compatible)**
   - Base URL: `http://127.0.0.1:9130/v1`
   - API key: your real model API key. **This is the key that actually reaches the model** —
     AetherState forwards it upstream as-is, and it overrides anything set in the Console.
     (The Console/`config.toml` key is only a fallback for frontends that send no key.)
6. Install the SillyTavern extension: run **`Install-ST-Extension.bat`** (or copy the
   `st-extension` folder to `SillyTavern/data/default-user/extensions/AetherState` yourself),
   restart SillyTavern and hard-refresh the browser (**Ctrl+Shift+R**).

Open a chat. State seeds itself from the card the moment the chat opens; from then on it updates
as you play. The **AetherState panel** (Extensions menu) has the everyday controls; the Console
shows everything.

## Quick start (Linux / macOS)

```bash
./start-aetherstate.sh    # creates .venv, installs, starts on :9130, prints the Console link
```

Then steps 4–6 above (for step 6, copy `st-extension/` into your SillyTavern extensions folder).

## Everyday controls

| Where | What |
|---|---|
| `/aether-genesis` | (Re)seed state from the character card right now |
| `/aether-cadence 3` | Update state every 3 turns (1 = every turn, the default) |
| `/aether-freeze` / `/aether-resume` | Pause / resume all state escalation |
| `/aether-status` · `/aether-mode` · `/aether-set` | Status · per-chat on/off · set a value |
| `((aether.set scene.location Moonlit Tavern))` | In-chat state edit — stripped before the model sees it |
| `((aether.freeze))` · `((roll 2d6+1))` | Safeword-style freeze · real dice |
| Panel: *update state every N turns* / *context intake* | Cadence + how much recent chat each update reads |
| Console → session view | Watch state live; edit it with manual override on |

<img width="482" height="579" alt="image" src="https://github.com/user-attachments/assets/57a81503-76dc-4390-9c28-813c377bb226" />
<img width="1296" height="865" alt="image" src="https://github.com/user-attachments/assets/e18cc7be-10e4-482b-8695-e3bfc06e781f" />
<img width="1396" height="748" alt="image" src="https://github.com/user-attachments/assets/54f6ff1f-236c-4a87-9fab-6b27992f8ac5" />




## RPG / DM mode

Turn the narrator into a Dungeon Master with a rulebook it cannot fudge: set
`[specialization] name = "rpg"` in the Console (or `/aether-spec rpg`). The principle:
**the model writes the story; AetherState owns the truth — and nothing becomes real, or even
rollable, without an in-world basis.**

- **Real dice, resolved by code.** `((aether.check stealth))` rolls actual dice against your
  sheet (stats, ranks, gear, active effects); the model is handed the decided outcome to
  narrate — it never decides success itself. Ambition is welcome:
  `((aether.check <skill> scope minor..mythic))` scales the odds instead of saying no.
- **Skills vs abilities (know the difference).** A **skill** is the thing you roll — Swordplay,
  Fire Manipulation, Persuasion — and its number is the modifier added to the dice. An **ability**
  is never rolled on its own; it *attaches* to a skill roll and changes it: **passive** ones
  auto-apply (advantage, a guard against fumbles) or **unlock** a gated skill (a *basis* like the
  Arcane Gift), while **active** ones you *spend* on a check for a burst of power (a big bonus, an
  extra die on a miss, a reroll — at a stamina/mana + cooldown cost). You always roll a *skill*; an
  ability rides along.
- **Just write it (new in 1.8).** You don't have to type `((aether.check ...))`. Name a skill or
  ability you own in your prose — "I use Fire-Slash on the monsters" — and AetherState detects it
  (case/hyphen-loose), rolls the governing skill (Fire Manipulation), invokes the ability, and hands
  the narrator the outcome. The 🎲 Rolls tab lists your skills (tap to roll) and active abilities
  (one tap rolls their skill and invokes them); the HUD shows the last roll's SUCCESS / PARTIAL /
  FAIL. Swipes re-roll fresh.
- **The eligibility gate.** A skill can require an in-world basis (Spellcraft needs the Arcane
  Gift; Systems Intrusion needs a Neural Lace). Declaring power you don't have is a non-move —
  acquire it in play, then the same declaration rolls.
- **World & Character Creator.** A standalone window (panel link or `/aether-creator`):
  world-first authoring, point-buy stats, curated skills/abilities with **genre packs**
  (fantasy, sci-fi, cyberpunk, post-apocalyptic, modern, historical — the sheet follows your
  world's genre), freestyle custom mechanics authored by AI and frozen into fixed numbers,
  named presets, and a review tab of everything committed.
- **Items & gear** (transactional, no duplication), **statuses & conditions** (inline tag
  protocol the narrator writes, the engine commits), **factions, affinity & bonds** (standing
  is journaled truth, not sentiment), **persistent locations** (canonical names, aliases,
  visit history).
- **The world-tag protocol (new in 1.2).** The narrator marks story-changes inline —
  `[scene | …]`, `[item gained | …]`, `[quest | …]`, `[affinity | …]`, `[hp | …]` — and the
  engine commits them to the ledger the same round: scene moves and who's on stage, organic
  item acquisitions (curated templates ground mechanics; unknown names commit mechanics-free —
  no power from prose), a real quest ledger, standing shifts, and clamped HP changes.
- **Progression (new in 1.2).** All code-awarded, never model-asserted: XP from completed
  quests, goals, and won-over standings; levels grant HP/pools/stat points; **mastery grows by
  use** through named brackets (Novice → Grandmaster) with an AI-authored evolution of the
  skill's frozen definition at each crossing (curated bump without an assist model); skills
  can cost stamina/mana, charged on attempt; critical failures leave real marks; HP 0 routes
  to contextual defeat (captured / robbed / rescued / wake safe) — or death with
  `[specialization] hardcore = true`.
- **Abilities that shape the dice (new in 1.3).** A skill sets the *modifier* you roll; an
  ability *bends the dice* — **advantage** (roll an extra die, keep the best), a **guard**
  against critical fumbles, a **second-chance extra die** when a roll misses, a **reroll**, or a
  **surge** that adds a big bonus and lifts the ceiling on ambitious attempts. Passive edges apply
  on their own; you invoke an active with `((aether.check <skill> use <ability>))` (it costs a
  resource and has a cooldown, and only spends when it actually helps). The Creator can author
  bespoke abilities with these mechanics, frozen into fixed rules before they can be rolled.
- **A player HUD (new in 1.3).** A movable, themeable in-page window — tabs for **Char · Skills ·
  Abilities · Gear · Status · World**, with your vitals always on top. Skills print the **dice
  rules** so you always know how a roll resolves; every ability spells out its mechanic, cost and
  cooldown in plain English; **Gear is a paper-doll** of equip slots (weapons / armor / trinkets,
  worn vs. carried, click to equip/unequip/use); Status always shows your conditions and diseases.
  The DM's raw ledger tags are hidden from the reader (the engine still reads them). Open it from
  the panel or with `/aether-hud`. The Console mirrors the same view.
- **New in 1.4 — gear, timing, categories, a sharper DM.** Worn starting gear (a helmet, coat,
  boots, a sword) now **auto-equips onto the paper-doll** at creation instead of sitting in a bag,
  and **gear vs. inventory is split by kind** — weapons/armor/tools/bags are gear (equipped *or*
  stowed), consumables/materials/devices are inventory, with a dedicated **🎒 Items** tab. State
  now takes in the **newest reply immediately** (its tags commit and extraction runs the instant a
  reply finishes, not a turn behind; swiping retracts and re-reads — toggle with
  `extraction.live_recalc`). Custom skills and abilities can carry **any category you name**
  ("Spells", "Cyber-Ware", "Disciplines") and the HUD groups by it. The DM now **calls for the
  exact check inline** when you attempt something uncertain. And the Creator's AI autofill no
  longer truncates, with room to add **free-form world & character detail categories** kept as
  retrievable lore.
- **New in 1.6 — the Narrator card carries your world.** Build a world + character in the Creator,
  click **Generate Narrator card**, and the card embeds the whole thing as a seed. Import it into
  SillyTavern, open a **new chat**, and your world, its opening scene, and your Player sheet are
  already committed to that chat's ledger — no going back to re-apply anything. Generating the card
  no longer needs a session (it's built straight from the form); the seed commits deterministically
  with **no AI call** and never overwrites a chat that already has a world or character, so
  re-opening an existing game is safe. The Creator's session picker now shows each session's
  **world & character names** and marks the newest, a **refresh** button updates it without wiping
  what you've typed, and your in-progress work is **auto-saved in the browser** so a reload never
  loses it.
- **New in 1.7 — a one-tap Rolls tab, and reliable rolls after a reload.** The HUD gains a **🎲
  Rolls** tab: tap a skill to drop its `((aether.check <skill>))` into your message (stackable,
  non-destructive), with a custom box for any skill and a reference for invoking active abilities —
  the engine still rolls and writes the outcome. Also fixes a bug where a check could silently stop
  resolving after reloading the page: the proxy's turn head is now authoritative, so a new message
  is always the next turn and a client turn is never allowed to regress.
- **New in 1.13 — real combat: the War Room.** Fights run on engine-owned combatant
  instances with **exact HP**: the DM summons foes with a `[foe | name | tier | weapon]`
  tag (or you do, with `((aether.foe ...))`), known NPCs fight as themselves and **keep
  their wounds after the battle**, and up to 3v3 with friends who join in on their own
  pre-rolled **[ALLY] dice**. Your strike damage is code-decided (outcome tier × your
  weapon) and applied to the ledger before the DM writes a word; enemy blows still land
  through the pre-rolled [OPPOSITION] die. Drop a foe to 0 HP and the engine — not the
  model — declares the defeat, pays XP by threat tier, and rolls loot from a table frozen
  at spawn (your world's Creator can author its own drop tables). A combat lane appears in
  the HUD with every fighter's exact HP, dice, and loot; NPC-vs-NPC fights stay pure prose
  but their outcomes are recorded (`[clash | A vs B | how | outcome]`); and a new lint
  catches any narration that kills a fighter the ledger says is still standing.
- **New in 1.12 — a leaner briefing that loses nothing.** Absent NPCs' physical detail,
  statuses, and drives stay in the ledger but out of your prompts until they're back on
  stage; only your freshest three quests carry full detail; restated facts retire their old
  versions (kept and labeled, never deleted — and no longer tripping the contradiction
  checker); and an opt-in `briefing_style = "compact"` squeezes the state blocks into dense
  notation with a one-line key, for smaller prompts on long campaigns.
- **New in 1.11 — the notables gate, and your voice is yours.** Notable NPCs get an authored
  **home** location: when you're at their home turf a `[NEARBY]` line tells the DM who's
  plausibly around, and notables anchored elsewhere stay out of your prompts entirely. Every
  on-scene NPC now declares how they know you — `stranger`, `by reputation (Faction)`, or a
  real relationship tier — so the world stops treating a newcomer like a famous main
  character. And a new lint (L11) enforces the player-voice rule: the DM deciding or speaking
  for your character gets flagged and corrected — unless you opened the door with an intent
  like `[I persuade Jerald.]`, and even then a line you wrote in quotes is delivered verbatim.
- **New in 1.10 — prompt caching, enabled and measured.** Long RP prompts are exactly what
  provider prompt-caches reward (a huge stable history + a small volatile tail), so enriched
  requests now carry a per-conversation `prompt_cache_key` that keeps every turn on the same warm
  cache server — cheaper input tokens and a faster first token on providers that support prefix
  caching (nothing changes on ones that don't). The Console's Status tab shows the measured
  hit-rate; `[upstream] include_usage = true` makes streaming responses report it, and
  `[upstream] prewarm = true` warms the cache when you open a chat so even your *first* message
  lands hot. All injected state already rides the prompt tail, so the cached prefix survives
  turn over turn.

## How state develops (priority)

1. **Character card + opening prompt** seed the start (turn 0).
2. **The chat itself** develops it — each update reads the newest turns (plus as much earlier
   context as your intake setting allows); newer information wins.
3. **Your manual edits** (slash commands, `((aether.set))`, Console) outrank everything.

"Organic" values (arousal, relationships, drives) are protected from manual editing unless you
turn **manual override** on (panel checkbox / Console) — the realism default.

## Configuration

Everything lives in `aetherstate-data/config.toml` (created on first run; the Console writes it
too). `config.example.toml` documents the common settings. Highlights:

| Setting | Default | Meaning |
|---|---|---|
| `[extraction] cadence_turns` | 1 | Update state every N turns |
| `[extraction] intake_chars` | 12000 | How much recent chat each update reads |
| `[assist.groups] extraction` | assist | Who tracks: `off` / `rules` (no LLM) / `main` / `assist` |
| `[assist.groups] linter_nli` | rules | Optional contradiction check (L10): `rules` (off) / `assist` / `main` |
| `[manual_override] enabled` | false | Allow editing organic values |
| `[user_guard] name` | "" | Your persona's name — keeps the model out of your voice |
| `[consent] safewords` | [] | Any of these in YOUR message freezes the scene instantly |

`base_url` always includes the version segment, exactly like SillyTavern custom endpoints:
`https://api.your-provider.com/v1` · `https://api.openai.com/v1` · `http://127.0.0.1:11434/v1`
(Ollama) · `http://127.0.0.1:5001/v1` (KoboldCpp).

## The helper ("assist") model

State tracking is a background job — it never blocks your story. Small local models work well
(Llama 3.1 8B on KoboldCpp is plenty; set `tier = "small"`), or point it at your main API.
Thinking/reasoning models are handled automatically: reasoning is disabled for tracking calls,
or budgeted if you set `[extraction] thinking = "on"`.

**Per-group helper endpoints.** Each background helper job — contradiction checking (`linter_nli`),
memory reflection, embeddings — can point at its **own** endpoint via `[assist.group_endpoints]`
(or the Console's **Connection → Assist routing** card, and the SillyTavern panel). So contradiction
checking can run on a small local model while memory reflection uses a cloud one, at the same time.
Leave it unset and every job uses the first assist endpoint — the classic behaviour.

## Contradiction checking (L10, optional)

L10 is an optional cold-path check that flags when the narrator's prose **flatly contradicts a
committed ledger fact**, and stages a next-turn corrective note (it never rewrites the current
reply). It fires **only** on contradiction — new detail the ledger doesn't cover is left alone
(*freedom of fiction, constraint on fact*). It's **off by default**
(`[assist.groups] linter_nli = "rules"`), so a default session is byte-identical.

To turn it on, point `linter_nli` at a small NLI model. A ready-to-run local one ships in
[`nli-shim/`](nli-shim/README.md): on Windows run `nli-shim\setup-nli.bat`, on Linux/macOS
`bash nli-shim/setup-nli.sh` — it installs a CPU build of torch + transformers and serves an
OpenAI-compatible endpoint on `127.0.0.1:8199`. Add it as an assist endpoint, set
`linter_nli = "assist"`, and (optionally) route it via `[assist.group_endpoints]`. Raise
`[linter] nli_threshold` (default `0.85`) if you see false hits.

## Troubleshooting

- **Antivirus (Avast/AVG and friends).** AetherState already works around the two common issues
  (TLS inspection and `SSLKEYLOGFILE`). If `pip install` itself fails with SSL errors:
  `python -m pip install --use-feature=truststore -e .`
- **Updated the extension but nothing changed?** Hard-refresh SillyTavern (**Ctrl+Shift+R**).
  The loaded build is printed in the browser console (F12).
- **A chat seeded empty once and won't retry?** `/aether-genesis` force-reseeds any chat.
- **Nothing updates during play?** Check the panel chip says the proxy is online, and that your
  frontend's base URL really is `http://127.0.0.1:9130/v1`.
- **401 / auth errors even though you set the key in the Console?** The key that reaches the
  model is the one in your **frontend's connection profile**, not the Console — AetherState
  forwards the frontend key as-is and it always wins. Put your real key in SillyTavern's
  connection profile (a dummy/placeholder there will 401). The Console/`config.toml` key is
  only used when the frontend sends none.

## Privacy

Local-first. Your chats, keys, and state stay in `./aetherstate-data/` on your machine. The proxy
talks only to the endpoints you configure. No telemetry, ever.

## License

MIT — see [LICENSE](LICENSE).
