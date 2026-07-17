# AetherState

**Your AI writes the scene. AetherState remembers the world.**

AetherState is a local-first continuity and RPG companion for AI roleplay. Put it between
SillyTavern and the model you already use, and it keeps track of the people, places, possessions,
relationships, secrets, quests, battles, and world changes that should still matter ten scenes
later.

The model stays free to tell the story. AetherState handles the parts that should not depend on a
model remembering perfectly: durable state, real dice, code-owned mechanics, replay safety, and a
Player-readable record of what is actually known or true.

Current version: **1.23.0**. Requires Python **3.10+**.

## What you can do

- Play ordinary freeform roleplay with persistent local continuity.
- Turn on RPG mode for real checks, resources, gear, conditions, quests, factions, relationships,
  combat, progression, a Player HUD, and the War Room.
- Build a world and Player character in the visual Creator, then generate a ready-to-play Narrator
  card for SillyTavern.
- Inspect and correct state in the browser Console instead of guessing what the model remembered.
- Track clothing, possessions, positions, relationships, consent, arousal, goals, secrets, scene
  time, notable NPCs, and other campaign facts.
- Give local names to recognized concepts with PlayerLex, and save explicit narration preferences
  with Player Lessons.
- Swipe, retry, Continue, reopen, replay, and fork without casually duplicating committed mechanics
  or semantic records.

## Install on Windows

1. Install [Python 3.10 or newer](https://www.python.org/downloads/) and enable **Add Python to PATH**.
2. [Download AetherState](https://github.com/juggernaunt46-star/AetherState/archive/refs/heads/main.zip)
   and extract the ZIP.
3. Double-click **`Install-AetherState.bat`**.

The installer finds a normal SillyTavern installation, installs the companion extension, creates a
private Python environment and data folder, starts AetherState, and opens the Console. If your
SillyTavern folder is somewhere unusual, it asks you to select it.

Then:

1. Configure the story-writing model under **Connection** in the AetherState Console.
2. In SillyTavern, use the OpenAI-compatible Chat Completion endpoint
   `http://127.0.0.1:9130/v1`.
3. Keep the AetherState terminal open while you play.

## Install on Linux

Install Python 3.10+ with `venv`, then run:

```bash
git clone https://github.com/juggernaunt46-star/AetherState.git
cd AetherState
./install-aetherstate.sh
```

The backend-only launchers are `Start-AetherState.bat` and `./start-aetherstate.sh`.
`Install-ST-Extension.bat` is available as a Windows extension-only fallback.

## Creator: make the campaign you actually want

The Creator can generate and edit:

- world premise, tone, rules, factions, locations, NPCs, fronts, loot, and travel routes;
- Player identity, appearance, stats, skills, abilities, resources, and starting gear;
- custom skill and ability definitions with complete mechanics and visible effects;
- a Narrator card carrying the selected world and Player into a new SillyTavern chat.

Creator authoring uses the configured **MAIN** model. The Creative Direction box is sent explicitly
as controlling instruction, not buried as world lore. The default output allowance is **32,768
tokens**, configurable up to **131,072**, with a 600-second timeout. Incomplete or truncated JSON is
rejected and retried once instead of being silently accepted as half a world.

Offline deterministic templates remain available when no main model is configured.

## People in stories can be wrong

“Mara said the gate is open” is not the same thing as “the gate is open.”

AetherState can keep these separate:

- what was said, quoted, promised, denied, asked, or reported;
- who said it and who they addressed;
- who believes, knows, doubts, or treats it as rumor;
- what a privileged Creator or code-owned mechanic accepted as fact;
- which world change was admitted as objective event truth;
- whether an event is active, expired, reversed, or superseded;
- whether the Player is allowed to see the cause.

Claim recognition never proves that a statement is true. Narrator prose, extracted text, model
confidence, PlayerLex, and Player Lessons cannot admit a World Event. Hidden causes render as unknown
instead of leaking through ordinary Player inspection.

## A world that keeps changing

Immutable World Event Records support persistent, temporary, reversed, and superseded changes. For
implemented adapters, the active WorldOverlay can affect:

- world and location circumstances;
- existing and future actors, NPC knowledge, and behavior;
- enemy and capability eligibility;
- quests, factions, relationships, and reputation;
- retrieval, briefing, narration, Console, and HUD presentation.

Unsupported effects remain lore only. One admitted event has one privileged or code-settled cause
and one atomic Ledger commit; model output cannot grant itself this authority.

## Optional RPG mode

Use `/aether-spec rpg` or select RPG in the panel. Available systems include:

- real dice and natural-language or explicit skill checks;
- ranked skills, passive and active abilities, requirements, costs, cooldowns, and mastery;
- inventory, consumables, equipped gear, conditions, resources, quests, and factions;
- relationships, locations, time, rumors, world fronts, and reputation;
- enemies with grounded kits, HP, intents, opposition turns, defeat, loot, and XP;
- code-decided Player damage, progression, contextual defeat, and optional hardcore death;
- a Player HUD and War Room that show committed state without dumping protocol syntax into prose.

The Player can attempt anything, but only implemented mechanics receive code-owned settlement.
Current direct Player damage is single-target; complete area-of-effect and multi-target mechanics are
not yet available.

## Player controls

| Control | Purpose |
|---|---|
| `/aether-creator` | Open the World and Character Creator. |
| `/aether-hud` | Open the Player HUD. |
| `/aether-genesis` | Seed the current chat from its Narrator card. |
| `/aether-spec rpg` | Enable RPG mode for this chat. |
| `/aether-status` | Inspect current AetherState status. |
| `/aether-freeze` / `/aether-resume` | Pause or resume state escalation. |
| `((roll 2d6+1))` | Roll real dice. |
| `((aether.set scene.location Moonlit Tavern))` | Make an explicit state edit. |

The SillyTavern panel covers common actions. The Console provides configuration, session inspection,
Claims & Events, PlayerLex, Player Lessons, and manual correction tools.

## PlayerLex and Player Lessons

**PlayerLex** stores Player-approved local names, aliases, or bounded patterns for one exact Semantic
Atlas meaning. It is recognition-only: it cannot grant an ability, settle a roll, store a claim, or
create truth. Entries become stale and stop proposing when their sealed meaning changes.

**Player Lessons** are explicit local preferences, never automatic chat learning. Narration lessons
may send their selected `do`/`avoid` text to the configured narrator. Intent lessons keep their
explanatory prose local and may only use a current ActionLex or ReferentLex anchor to resolve a real
same-span ambiguity. Neither lesson type can override current Player words, mechanics, or truth.

## Privacy

- No telemetry or analytics.
- State, configuration, SQLite databases, and diagnostics stay in the local `aetherstate-data/`
  folder by default.
- Chat and Creator requests go only to the model providers you configure.
- Local-first does not mean offline: enabled model and helper features send the content they require
  to those configured providers.
- PlayerLex and Player Lessons do not learn across users or from chat history automatically.
- Secure removal clears AetherState-owned active database/WAL/cache evidence, but cannot erase
  external backups or text already retained by a model provider.

## Honest limits

- The RPG semantic truth gate remains **off by default**.
- Recognition never establishes truth, occurrence, fulfilled intent, deliberate lying, mechanics,
  capability, outcome, or event admission.
- Helper extraction can be wrong; the Console and manual controls exist so the Player can inspect
  and correct state.
- Narration preference delivery does not prove that a model followed it.
- There is no built-in cloud sync or cross-user learning.
- The full panel, HUD, Creator, and commands are SillyTavern features. Other OpenAI-compatible
  clients receive proxy behavior without the complete companion interface.

## Under the hood

The sealed Semantic Atlas contains **327 meanings**:

- 265 CapabilityLex
- 13 ReferentLex
- 18 SceneLex
- 15 ActionLex
- 16 ClaimLex

These Lexes classify meaning; they grant no authority. The local Ledger owns committed state,
branch/replay lineage, typed Claim Records, actor-relative epistemics, accepted facts, and immutable
World Event Records.

For development:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests tools
```

Start with [`docs/00-MAINTAINER-MAP.md`](docs/00-MAINTAINER-MAP.md). Runtime corpus assets under
`corpus/` are versioned product data and must stay in step with their manifests and builders.

## License

MIT. See [`LICENSE`](LICENSE).
