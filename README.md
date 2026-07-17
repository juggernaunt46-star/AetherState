# AetherState

AetherState is a local-first narrative state engine for AI roleplay. It runs as a transparent
OpenAI-compatible proxy between your roleplay frontend and the model endpoint you choose. While the
model writes the prose, AetherState keeps structured story state, supplies the relevant facts on later
turns, and can optionally run a code-authoritative RPG layer.

The included web Console works in any browser. The companion extension adds an AetherState panel,
Player HUD, Creator, and slash commands to SillyTavern. Other OpenAI-compatible clients can use the
proxy, but the companion interface is built for SillyTavern.

Current package version: **1.22.0**. Python **3.10 or newer** is required.

## What AetherState does

- Seeds a starting state from a character card and opening message.
- Tracks people, locations, clothing, possessions, relationships, consent, arousal, goals, secrets,
  scene time, and other continuity facts as play develops.
- Builds a compact state briefing for the model instead of relying on the model to remember every
  earlier detail.
- Keeps durable events and facts in a local SQLite ledger and recalls relevant material later.
- Provides a Console for inspecting state, changing settings, and making explicit manual edits.
- Supports optional helper-model extraction, memory work, and contradiction checking without making
  those background jobs part of the visible response path.
- Adds an optional RPG mode with code-owned checks, resources, items, gear, conditions, factions,
  quests, combat, progression, a World and Character Creator, and a Player HUD.
- Includes PlayerLex and Player Lessons, two separate Player-controlled semantic tools described
  below.

## The authority model

AetherState separates storytelling from authority:

1. **The Player supplies intent and remains free to attempt anything.**
2. **The model narrates.** It can propose story changes through the supported protocol, but prose is
   not automatically a mechanic or settled fact.
3. **Code checks eligibility and resolves supported mechanics.** Dice, costs, cooldowns, damage, and
   other implemented results are computed before the narrator describes them.
4. **The Ledger stores committed truth.** Later prompts, retries, and UI views consume that recorded
   state rather than asking the model to reconstruct it.
5. **Manual Player edits outrank extracted guesses.** Sensitive organic fields are protected unless
   manual override is explicitly enabled.

Recognition is not authority. Seeing that a phrase resembles a skill, target, scene, or action does
not prove that the Player owns it, that a check is legal, or that an effect happened.

## Semantic Atlas and PlayerLex

The Semantic Atlas is a sealed classification layer containing **311 exact meanings**:

- 265 CapabilityLex meanings
- 13 ReferentLex meanings
- 18 SceneLex meanings
- 15 ActionLex meanings

It gives AetherState stable names and fingerprints for concepts across those four Lexes. It does not
assign abilities, create targets, admit mechanics, settle results, or write world truth.

**PlayerLex** lets one local Player explicitly approve a name, alias, or bounded authoring pattern for
one exact Atlas meaning. For example, the Player can define `Ghost Step` as their local name for a
specific stealth concept. A current match can then enter the real RPG recognition receipt on a fresh
Player turn.

PlayerLex is deliberately recognition-only:

`recognized=true, authorized=false, executable=false`

Approvals retain local provenance and an exact meaning fingerprint. If the underlying meaning
changes, the approval becomes visibly stale and stops proposing until the Player explicitly corrects
or reapproves it. Ambiguous matches stay ambiguous. PlayerLex does not scan chat history or model
output, learn automatically, share entries between users, or turn a recognized phrase into a roll or
world fact.

## Player Lessons

Player Lessons are explicit, local preferences created and managed in the Console. They are not
learned from chats and they are separate from PlayerLex.

### Narration behavior

A narration lesson tells the narrator what to do or avoid for a selected scope such as exploration,
combat opening, or combat exchange. An applicable lesson's title and selected `do` and `avoid` text
may be included in a request to the configured model provider. The Console discloses this before the
Player saves and enables the lesson.

Narration lessons are prompt input only. They cannot override the Player's newest words, code-owned
mechanics, settled world truth, or the RPG rules contract. Provider receipt does not prove that the
model followed the preference or completed a successful response.

### Intent interpretation

An intent lesson records a recurring misunderstanding and the correct interpretation for the
Player's own reference. Its explanatory prose remains local: AetherState does not parse that prose
and does not send it to the narrator.

Intent support requires one current ActionLex or ReferentLex PlayerLex anchor. Code may use that exact
anchor only to narrow a real same-span action or target ambiguity after recognition and before
contextual binding. It cannot change the actor, manufacture a missing action or target, collapse an
explicit multi-target statement, or override clear wording in the current turn.

### Consent and lifecycle

The Console can test a draft in memory without saving it or calling the narrator. Saving is explicit
consent. The Player can inspect provenance and currentness, revise with conflict protection, disable,
re-enable, or securely remove either lesson type.

Secure removal deletes the lesson and its matching AetherState-owned evidence from the active SQLite
database and WAL, then purges owned process-local cached copies. It cannot erase external backups,
filesystem snapshots, storage-device history, third-party logs, or text already received, retained,
or in flight at a model provider.

## RPG mode

Set the specialization to `rpg` in the Console, the SillyTavern panel, or with `/aether-spec rpg`.
RPG mode adds a rules contract and deterministic mechanics over the same state core. With the
specialization set to `none`, the RPG layer stays out of the session.

Implemented surfaces include:

- Real dice and skill checks, including explicit `((aether.check ...))` commands and supported
  natural-language intent.
- Skills, passive and active abilities, eligibility requirements, costs, cooldowns, and mastery.
- World-first character creation with genre-aware options and frozen custom definitions.
- Inventory, consumables, equipped gear, statuses, conditions, quests, factions, relationships,
  locations, time, rumors, and world fronts.
- Combatant instances with exact HP, code-decided Player damage, opposition and ally turns, defeat,
  loot, XP, and a visible War Room.
- Code-awarded progression and contextual defeat, with optional hardcore death rules.
- A narrator tag protocol for proposed scene, item, quest, affinity, time, and related updates.
  Supported reducers validate and commit accepted changes; unknown prose does not gain mechanical
  power merely because the model wrote it.

The Player HUD exposes character state, skills, abilities, rolls, gear, items, conditions, world
information, and combat state without showing internal ledger syntax in the story.

## Retry, swipe, and replay safety

AetherState stores branch, turn, and receipt identities so committed state can be replayed without
blindly applying the same semantic input twice.

- PlayerLex runs on an actual fresh RPG Player turn, not again for retries, swipes, Continue, or
  lost-reply delivery.
- Player Lessons freeze their selections for the turn. Narration selections can be rehydrated for
  eligible replay paths; intent correction is not rerun or reapplied there.
- Changed, disabled, removed, stale, or unavailable lessons are omitted rather than silently replaced.
- Exact duplicate delivery rechecks that cached narration lessons are still enabled and current.
- Journaled mechanics and state reducers own committed mutation; a model retry is not permission to
  duplicate an item, cost, effect, or settled result.
- If an internal enrichment step fails, AetherState is designed to preserve the original request or
  response path instead of corrupting the chat stream.

Different mechanics can intentionally have their own swipe rules. The guarantees above mean that
semantic learning and committed state are not casually rerun; they do not promise that every swipe
must produce the same model prose or random roll.

## Fastest SillyTavern setup on Windows

1. Install [Python 3.10+](https://www.python.org/downloads/) and enable **Add python.exe to PATH**.
2. [Download the AetherState ZIP](https://github.com/juggernaunt46-star/AetherState/archive/refs/heads/main.zip)
   and extract it.
3. Double-click **`Install-AetherState.bat`**.

The installer finds a normal SillyTavern installation automatically. If yours is elsewhere, it asks
for that folder once. It installs and verifies the companion, creates AetherState's private Python
environment and data folder, starts the proxy, and opens the Console. It does not touch chats or
overwrite local AetherState configuration.

## Fastest SillyTavern setup on Linux

Install Python 3.10+ with its `venv` support, then run:

```bash
git clone https://github.com/juggernaunt46-star/AetherState.git
cd AetherState
./install-aetherstate.sh
```

The Linux installer performs the same companion detection, private environment setup, and launch.
If you downloaded the source archive instead, extract it and run `./install-aetherstate.sh` inside
the folder. The script also works on macOS with Python 3.10+.

After either installer opens the Console:

1. In **Connection**, configure the main model that writes the story. Optionally configure a helper
   model for state extraction and other background work.
2. In SillyTavern, point the OpenAI-compatible Chat Completion connection to
   `http://127.0.0.1:9130/v1`.
3. Keep the AetherState terminal window open while you play. Restart SillyTavern and hard-refresh
   the browser once after the first companion install.

Put the real upstream API key in the SillyTavern connection profile. A frontend-supplied key is
forwarded to the configured upstream provider and takes precedence over the Console fallback.

For a backend-only start, use `Start-AetherState.bat` on Windows or `./start-aetherstate.sh` on
Linux. `Install-ST-Extension.bat` remains available as a Windows companion-only fallback. To update
a Git checkout, run `git pull` and use the combined installer again.

## Everyday controls

| Control | Purpose |
|---|---|
| `/aether-genesis` | Reseed the current chat from its character card. |
| `/aether-cadence 3` | Update extracted state every three turns. |
| `/aether-freeze` / `/aether-resume` | Pause or resume state escalation. |
| `/aether-status` / `/aether-mode` / `/aether-set` | Inspect, toggle, or edit chat state. |
| `/aether-spec rpg` | Enable RPG specialization for the chat. |
| `/aether-creator` | Open the World and Character Creator. |
| `/aether-hud` | Open the Player HUD. |
| `((aether.set scene.location Moonlit Tavern))` | Apply an explicit in-chat state edit. |
| `((roll 2d6+1))` | Roll real dice. |

The SillyTavern panel provides the common controls without requiring commands. The Console provides
full configuration, session inspection, PlayerLex, and Player Lessons management.

## Configuration

The launchers copy [`config.example.toml`](config.example.toml) to the private
`aetherstate-data/config.toml` file on first run. The Console writes the same local configuration.

Common settings include extraction cadence and context intake, main and helper endpoints, helper
routing, manual override, safewords, prompt caching, and RPG specialization. OpenAI-compatible base
URLs normally include their version suffix, such as `/v1`.

The optional contradiction checker is off by default. It can use a configured helper endpoint to
stage a later corrective note when narration directly contradicts committed state; it does not
rewrite the current model response.

## Privacy and network behavior

- AetherState has no telemetry or analytics.
- Runtime state, SQLite databases, configuration, diagnostics, and traces live under the local
  `aetherstate-data/` folder by default.
- The proxy sends chat requests to the main model endpoint you configure. Enabled helper features
  send only their required work to the helper endpoints you configure.
- A selected narration lesson may be sent to the configured model provider. Intent lesson prose is
  local-only and never enters the narrator prompt.
- The bounded turn trace can contain private world state, mechanics, branch lineage, narrator
  instructions, timing, and hash receipts. It excludes authorization headers, API keys, the full
  chat request, and raw model reply prose. Keep the entire data folder private.
- API keys and live configuration do not belong in Git. Use the generated local configuration or
  environment injection, and never commit `aetherstate-data/`, `.env` files, logs, traces, or
  databases.
- There is no cloud sync or cross-user learning built into PlayerLex or Player Lessons.

## Current limits

- The 311-meaning Atlas, PlayerLex, and shared semantic recognition improve classification; they do
  not grant capabilities, settle mechanics, or create world truth.
- The RPG semantic truth gate remains off by default. Enabling it is separate from ordinary RPG use.
- Current active Player damage is single-target. Recognizing plural or area language is not a
  complete multi-target or area-of-effect mechanic.
- Player Lessons can record provider receipt of narration preferences, not narrator compliance or
  response quality.
- Helper-model extraction can be wrong. The Console and manual controls exist so the Player can
  inspect and correct state.
- Secure removal controls the active AetherState database, WAL, and owned caches. It cannot recall
  copies outside that boundary.
- The implemented test and bounded live-proof surfaces do not amount to a blanket guarantee for
  every model, frontend, campaign, or 25-turn play sequence.
- The SillyTavern companion is the primary integrated frontend. Other OpenAI-compatible clients get
  the proxy behavior but may not provide the same panel, HUD, Creator, or slash commands.

## Development

Install development dependencies and run the public test suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests tools nli-shim
```

Start with [`docs/00-MAINTAINER-MAP.md`](docs/00-MAINTAINER-MAP.md) for the code map and invariants.
Runtime corpus assets under `corpus/` are part of the packaged semantic systems and must remain in
step with their manifests and source.

## License

MIT. See [`LICENSE`](LICENSE).
