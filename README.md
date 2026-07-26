# AetherState

**Your AI writes the scene. AetherState remembers the world.**

AetherState is a local-first companion for AI roleplay. It sits between SillyTavern and the model you already use, quietly keeping a structured record of the story so characters stop forgetting who is present, what happened, what people own, how relationships changed, and which parts of the world should still matter later.

Choose **Chat** for a first-class Living Character experience built around the exact Character card and SillyTavern Persona you selected; a World is optional. Choose **RPG** for real dice, character sheets, gear, conditions, quests, factions, combat, progression, and a Player HUD.

**The simple idea:** the AI tells the story. AetherState remembers the details and handles the game rules that should not change just because the model forgot them.

Current version: **1.24.0**. Requires Python **3.10 or newer**.

## What you get

- **Automatic story tracking:** people, locations, clothing, possessions, moods, relationships, arousal, consent, goals, secrets, injuries, scene time, quests, and more.
- **First-class Chat and Living Characters:** ordinary SillyTavern cards work without conversion. AetherState keeps the Character's stable Core, your exact selected Persona, and their shared continuity tied to the right chat.
- **Chat Continuity:** see **Now**, **Relationship**, **Open Threads**, **Shared History**, and **Character** in a compact Player-safe HUD, without RPG controls or Character-private information.
- **A living story bible:** relevant information is sent back to the model each turn so it can continue from established details.
- **Character-card starts:** Chat Character cards can carry an immutable Core, optional starting continuity, and an optional World; RPG Narrator cards can still carry the World, Player, opening scene, equipment, and relationships.
- **Memory that stays useful:** older events can become summaries and durable facts instead of filling every prompt forever.
- **A visual Creator for both experiences:** build or enhance a Chat Character JSON/PNG, or build a full RPG World, Player, cast, factions, locations, gear, skills, abilities, and Narrator card.
- **A browser Console:** inspect sessions, correct mistakes, configure models, edit state, and see what AetherState currently remembers.
- **A SillyTavern companion panel and experience-specific HUDs:** choose Chat or RPG for each new chat, open Chat Continuity in Chat, or use the Player HUD and combat War Room in RPG.
- **Optional RPG mode:** real checks, resources, abilities, enemies, loot, XP, mastery, quests, factions, relationships, travel, and living-world changes.
- **Safer retries and swipes:** committed damage, items, rolls, claims, and world changes are protected from casual duplication.
- **Fail-open behavior:** if AetherState has an internal problem, the original chat request or reply is allowed through instead of destroying the conversation.
- **Truthful System Health:** expected recoverable problems and unexpected invariant failures are tracked by stable condition, aggregate count, and recovery state without storing story or model content.
- **Local-first privacy:** AetherState has no telemetry or analytics.

AetherState works with OpenAI-compatible hosted services and local engines. The complete panel, Creator, and HUD experience is built for **SillyTavern**. Other compatible frontends can still use the proxy and state tracking without the full SillyTavern interface.

## Install on Windows

1. Install [Python 3.10 or newer](https://www.python.org/downloads/) and enable **Add Python to PATH**.
2. [Download AetherState](https://github.com/juggernaunt46-star/AetherState/archive/refs/heads/main.zip) and extract it.
3. Double-click <code>Install-AetherState.bat</code>.

The installer:

- creates AetherState's private Python environment and data folder;
- finds a normal SillyTavern installation and installs the companion extension;
- asks you to select SillyTavern if it is somewhere unusual;
- starts AetherState and opens the Console.

Then:

1. Open **Connection** in the AetherState Console.
2. Add the endpoint, model, and API key for the model that writes your story.
3. In SillyTavern, choose an OpenAI-compatible Chat Completion connection.
4. Set the base URL to <code>http://127.0.0.1:9130/v1</code>.
5. Do not duplicate the provider key in that SillyTavern profile; use a non-secret placeholder if
   the connection requires a value.
6. Before the first reply, confirm **Session experience** is **Chat** for a Character card or **RPG**
   for a Narrator card.
7. Keep the AetherState terminal open while you play.

The backend-only launcher is <code>Start-AetherState.bat</code>. The extension-only fallback is <code>Install-ST-Extension.bat</code>.

### Start the complete Windows play stack

The optional play-stack controller starts and owns the local NLI helper, AetherState, and
SillyTavern together, verifies all three services, installs the current companion extension, and
prints the matching stop command:

~~~powershell
.\.venv\Scripts\python.exe -m aetherstate.playstack start
~~~

Run <code>nli-shim\setup-nli.bat</code> once first to create the NLI environment, then stop its
standalone server before starting the combined stack. The controller discovers the checkout-local
<code>nli-shim</code> and common SillyTavern locations. For an unusual install, pass
<code>--sillytavern-root "D:\path\to\SillyTavern"</code>; explicit
<code>--project-root</code> and <code>--nli-root</code> overrides are also available.

Parent provider, cloud, and Git credentials are not forwarded to NLI or SillyTavern. Only
AetherState-prefixed configuration and network transport settings can reach the AetherState proxy,
where provider transport occurs. A newly seeded isolated profile reads its model name from
Console-managed configuration or <code>--model</code>; it does not assume a provider-specific model.

## Install on Linux

Install Python 3.10+ with <code>venv</code>, then run:

~~~bash
git clone https://github.com/juggernaunt46-star/AetherState.git
cd AetherState
./install-aetherstate.sh
~~~

The backend-only fallback is <code>./start-aetherstate.sh</code>.

## Your first game

Choose the experience you want before the first reply. After the conversation starts, that chat stays
in the selected experience; start a new chat to switch.

### Chat with a Character

1. Select the exact SillyTavern Persona you want to use.
2. Open a new chat with an ordinary JSON/PNG Character card or an AetherState Chat Character card.
3. In the AetherState panel, confirm **Session experience: Chat**.
4. Talk normally. Open the HUD whenever you want to see **Chat Continuity**.

A World is optional. Chat does not create a Player sheet or enable dice, combat, progression,
inventory grids, or the War Room.

### Build a Chat Character

1. Open the AetherState panel and select **Creator**, or use <code>/aether-creator</code>.
2. Set **Card draft** to **Chat Character**.
3. Start from blank fields or import an ordinary JSON/PNG card to enhance it.
4. Define the Character Core and, if wanted, add starting continuity and a World.
5. Auto-fill blank Core fields with the configured main model, or edit everything yourself.
6. Generate the Chat Character card, import it into SillyTavern, and open a new Chat.

### Build an RPG campaign

1. Open the AetherState panel and select **Creator**, or use <code>/aether-creator</code>.
2. Set **Card draft** to **RPG Narrator**.
3. Describe the World and Player character you want.
4. Generate or edit the details.
5. Generate the Narrator card.
6. Import that card into SillyTavern, open a new chat, and confirm **Session experience: RPG**.

The RPG chat begins with the selected World, opening situation, and Player sheet already attached.

## What AetherState remembers

AetherState can track ordinary continuity such as:

- who is present and where everyone is;
- clothing, equipped gear, carried possessions, and lost or consumed items;
- injuries, diseases, temporary conditions, moods, cravings, and obsessions;
- relationships, attraction, arousal, consent, promises, rivalries, and loyalties;
- locations, aliases, travel, time of day, and previous visits;
- goals, secrets, rumors, quests, factions, fronts, and notable NPCs;
- what happened recently and which older events still matter.

A background helper can read new turns and suggest updates. Those updates become a compact briefing for the story-writing model. You can choose how frequently this happens and how much recent conversation the helper reads.

### Which information wins?

1. **The character card and opening scene** establish the starting point.
2. **The conversation** develops that state as new things happen.
3. **Your explicit edits** through the panel, commands, or Console take priority.

Sensitive or naturally changing values such as arousal, relationships, and drives are protected from casual manual editing unless you enable manual override.

## Chat and Living Characters

Chat is its own experience, not RPG with the controls hidden. The selected SillyTavern Character card is the Character, the exact selected Persona is you, and a World is optional.

AetherState keeps two parts separate:

- **Character Core:** who the Character is—their authored identity, personality, behavioral anchors, boundaries, scenario, and first message.
- **Lived continuity:** what has happened since—the current situation, shared memories, relationship changes, open promises, possessions, conditions, and unfinished conversations.

The Chat Continuity HUD shows only the Player-safe view: **Now**, **Relationship**, **Open Threads**, **Shared History**, and **Character**. Character-private beliefs, motives, affairs, memories, and undisclosed events stay private.

Accepted replies become continuity. If you replace a reply with a swipe, its assistant-derived continuity is retired. Forks inherit only their accepted shared history. Changing the selected Persona or materially changing the Character Core starts separate continuity in a new chat instead of rewriting established history.

## Creator: build the Character or campaign you want

The **Card draft** selector chooses **Chat Character** or **RPG Narrator**. It controls what Creator builds; it does not silently change an attached live session.

For a Chat Character, Creator can:

- start blank or import an ordinary JSON/PNG card for enhancement;
- define an immutable Character Core with behavioral anchors and boundaries;
- add optional shared memories, Character knowledge, possessions, non-RPG conditions, open threads, and a starting relationship;
- add an optional World or leave it blank for worldless Chat;
- use the configured main model to fill blank Core fields;
- generate a portable Chat Character card.

Private Creator direction can guide the AI fill but never enters the portable card. A worldless Chat does not invent a placeholder World, Player Card, or RPG setup.

For RPG Narrator drafts, the World and Character Creator supports:

- premise, genre, tone, themes, writing style, and campaign rules;
- locations, factions, NPCs, relationships, fronts, rumors, loot, and travel routes;
- Player identity, appearance, species, class, biography, stats, resources, and starting situation;
- skills, active and passive abilities, costs, cooldowns, requirements, mastery, gear, and inventory;
- custom world and character detail categories;
- named presets and a final review before creating the Narrator card.

When you reopen an existing game, the Session tab can load its committed World and Character back
into the forms. These prefills preserve the original authored setup separately from changing live
values such as current HP, resources, and runtime NPCs. Saving an unchanged prefill is a safe no-op;
it does not duplicate starting gear or rewrite the committed World.

The **Enemy Workshop** turns authored enemy facts into an exact, inspectable preview of grounded moves, danger, tells, and counterplay, then can add that enemy to the World draft as an NPC. The preview is deliberately honest about the current mechanics boundary: it covers single-target Player HP effects and does not spawn, assign, or settle a runtime enemy.

The **Creative Direction** box tells the AI how to write and what kind of result you want. It is sent as a direct instruction to the configured **main story model**, not hidden inside the world's lore. Directions such as `exactly 2 factions`, `exactly 3 locations`, `exactly 2 NPCs`, or `no fronts` override the richer defaults. Character directions can also request a named custom resource and an ability that spends it; the resource and cost are validated together before anything enters the form.

Creator generation allows **32,768 output tokens by default**, can be configured up to **131,072**, and waits up to 600 seconds. Empty, incomplete, or cut-off JSON is rejected and retried once instead of quietly creating half a world.

Slow AI fills show persistent progress and elapsed time while every field stays editable. When the
result returns, generated values fill only paths that were untouched during the wait; newer Player
edits, custom skills, abilities, and resource choices win. World and Character AI/default fills share
one request gate, so an accidental second click cannot start another paid generation.

Narrator cards are admitted seed-first when a new SillyTavern chat opens. AetherState verifies that
the exact fingerprint carried by the card has one durable, atomic admission receipt before genesis
continues. If the browser loses a slow response, the extension checks that read-only receipt instead
of retrying the write or falsely reporting failure. A rejected, partial, or identity-mismatched seed
rolls back and stops visibly instead of opening an apparently valid but unseeded game.

If no main model is configured, deterministic offline templates remain available.

## People can be mistaken, dishonest, or uncertain

Stories are more believable when AetherState does not treat every sentence as objective truth.

For example:

- **Mara said the gate is open.**
- **Jon believes Mara.**
- **The guard doubts her.**
- **The gate is actually locked.**

AetherState can keep those as four different pieces of information. The **Claims & Events** view can show:

- what was said, asked, promised, denied, quoted, or reported;
- who said it and who they were speaking to;
- who believes it, knows it, doubts it, or has only heard a rumor;
- which information has actually been accepted as fact;
- which world-changing event is currently active;
- why the event happened, when the cause is visible to the Player;
- whether an older event expired, was reversed, or was replaced by something newer.

Recognizing a sentence does **not** automatically make it true. Ordinary AI narration cannot secretly declare itself objective world truth, and hidden causes stay hidden from the Player.

Quoted text and code examples remain non-authoritative. Before a recognized action can change
mechanics, AetherState aligns it with the exact current world and turn, rejects stale world
evidence, and leaves ambiguous names unresolved instead of choosing the first apparent match.
Only a complete admitted result can change the ledger, and reopen or replay reconstructs that same
committed result.

## A world that can keep changing

AetherState can preserve lasting and temporary world changes instead of relying on the model to remember them.

Examples include:

- a bridge being destroyed and later rebuilt;
- a city entering a temporary curfew;
- a faction changing its attitude toward the Player;
- an NPC learning something that changes their behavior;
- a quest becoming available or impossible;
- an enemy type becoming eligible to appear;
- a storm affecting a location for several in-world hours;
- one event replacing or reversing an older event.

Supported changes can affect locations, characters, NPC knowledge and behavior, factions, relationships, reputation, quests, enemy possibilities, model briefings, the Console, and the HUD. Effects that do not yet have game support remain story information rather than silently inventing new mechanics.

Event duration uses story and turn time, not the computer's real-world clock.

## Optional RPG / Dungeon-Master mode

Choose **RPG** under **Session experience** before the first reply, or use <code>/aether-experience rpg</code>. The older <code>/aether-spec rpg</code> setting is only an advanced global fallback.

You can still write actions naturally. AetherState handles the parts that need dependable rules, then gives the result to the AI to narrate.

### Dice and checks

- Real dice are rolled by AetherState.
- Stats, skill rank, gear, conditions, difficulty, and abilities can affect the roll.
- Results can be success, partial success, failure, or a critical result.
- Swiping creates a fresh roll where appropriate without duplicating earlier state.
- Ambitious actions can become harder instead of being forbidden just because they are powerful.

You can use an explicit command such as <code>((aether.check stealth))</code>, but you usually do not need to. Writing “I quietly pick the lock” or “I try to persuade the guard” can select the relevant owned skill and a real target in the scene.

### Skills and abilities

A **skill** is what you roll: Swordplay, Persuasion, Fire Manipulation, Medicine, and so on.

An **ability** changes or unlocks a skill:

- passive abilities can grant advantage, protect against a critical failure, provide a second chance, or unlock a special kind of action;
- active abilities can provide a bonus, reroll, surge, special effect, or other benefit at a resource and cooldown cost;
- abilities and skills can require a real in-world basis, such as magical training, equipment, biology, or cyberware.

If your character does not have the required basis, AetherState explains why the mechanic is unavailable. The story can still explore the attempt, but the missing ability does not become real merely because it was declared.

### Character sheet, resources, gear, and inventory

RPG mode supports:

- stats, HP, stamina, mana, and custom resources;
- ranked skills and mastery gained through use;
- active and passive abilities;
- weapons, armor, tools, bags, consumables, materials, and devices;
- equipment slots and a paper-doll view of worn gear;
- item quantities, use, loss, transfer, and effects;
- temporary statuses, longer-term conditions, diseases, and injuries;
- costs, cooldowns, requirements, and visible reasons when something cannot be used.

Worn starting equipment can automatically appear in the correct slot. Gear and inventory are kept separate so a sword, coat, medicine, and crafting material do not all behave like the same kind of item.

### Quests, factions, relationships, and travel

- Quests and goals can progress, complete, fail, and award XP.
- NPC and faction attitudes can improve or worsen with a recorded reason.
- Important bonds such as companion, soulmate, rival, or nemesis can remain consistent.
- NPCs can know the Player personally, by reputation, or not at all.
- Locations keep names, aliases, visit history, and notable residents.
- Travel advances in-world time.
- Faction fronts can develop in the background.
- Rumors can reveal that something is happening without exposing a hidden cause as fact.

### Combat and the War Room

Combat uses characters with exact HP instead of asking the model to remember injuries approximately.

The War Room can show:

- what just happened;
- your action and its result;
- enemy intentions and danger;
- current HP and conditions;
- allies, enemies, and queued reinforcements;
- available responses and relevant abilities.

Player attacks, enemy opposition, costs, damage, defeat, XP, and loot are settled before the AI describes them. Known NPCs can enter combat as themselves and keep their wounds afterward.

Your side can include present companions, battlefield allies with a shared enemy, and summons or conjurations. Large battles let you fight the part of the battlefield around you while the wider conflict continues in the story. Enemy waves can continue until the battle's momentum changes.

Current direct Player damage is single-target. Complete area-of-effect and unrestricted multi-target mechanics are not yet available.

### Progression and defeat

- Completed goals, quests, victories, and other supported achievements can award XP.
- Levels can increase HP, resources, and available stat points.
- Skills can grow from Novice toward Grandmaster through actual use.
- Abilities can cost resources and enter cooldown.
- Critical failures can leave lasting consequences.
- Reaching 0 HP can lead to a contextual defeat such as capture, robbery, rescue, or waking somewhere safe.
- Optional hardcore mode allows death instead.

## The Player HUD, panel, and Console

### SillyTavern panel

Use it for everyday controls:

- enable or disable AetherState for a chat;
- choose Chat or RPG for a new chat;
- open the Creator, current experience HUD, Console, or RPG War Room;
- choose update cadence and context intake;
- freeze or resume state changes;
- see whether the proxy is connected.

### Chat Continuity

In Chat, the movable HUD becomes **Chat Continuity**. Its compact view shows the current situation and relationship summary; Expand reveals **Now**, **Relationship**, **Open Threads**, **Shared History**, and **Character**. RPG sheets, rules, rolls, and War Room controls stay out of this view.

### RPG Player HUD

In RPG, the movable Player HUD includes character information, skills, abilities, rolls, gear, inventory, status, World information, and Claims & Events. It explains costs, cooldowns, dice rules, current resources, and why an option is unavailable.

### Browser Console

The Console provides the detailed view:

- configure main and helper models;
- inspect and correct session state;
- view the journal of changes;
- inspect Claims & Events;
- manage PlayerLex names and Player Lessons;
- review worlds, factions, relationships, locations, quests, and NPCs;
- review the compact **System Health** card for active failures, degraded conditions, counts, and recovery;
- use manual override when an automatic update needs correction.

Maintainers can export the same content-free view from
<code>GET /aether/health/diagnostics</code>. The ordinary status route reports
truthful <code>failed</code>, <code>degraded</code>, or <code>none</code>
precedence while remaining available even if durable health storage fails.

Raw tracking instructions and internal tags are hidden from ordinary story text.

## Everyday controls

| Control | What it does |
|---|---|
| <code>/aether-creator</code> | Open the Chat Character / RPG Creator |
| <code>/aether-hud</code> | Open Chat Continuity or the RPG Player HUD for this chat |
| <code>/aether-genesis</code> | Seed or reseed the chat from its card |
| <code>/aether-experience chat\|rpg</code> | Choose this new chat's experience before it locks |
| <code>/aether-cadence 3</code> | Update state every three turns |
| <code>/aether-status</code> | Show the current AetherState status |
| <code>/aether-mode</code> | Enable or disable AetherState for the chat |
| <code>/aether-freeze</code> / <code>/aether-resume</code> | Pause or resume state changes |
| <code>((roll 2d6+1))</code> | Roll real dice |
| <code>((aether.set scene.location Moonlit Tavern))</code> | Make an explicit state edit |

In-chat AetherState commands are removed before the story model sees them.

## Your character's voice stays yours

AetherState checks for narration that decides your character's actions or speaks dialogue for them. If the model crosses that boundary, a correction can be prepared for the following turn.

This check runs after the visible response has streamed. It never silently blocks or replaces prose
that the Player has already seen; it is an advisory for what should happen next.

You can intentionally open the door by writing a clear action or quoted line yourself. Text you wrote in quotation marks should remain your text rather than being rewritten as different dialogue.

## PlayerLex and Player Lessons

These are optional, local tools controlled by the Player.

**PlayerLex** lets you approve a personal name, alias, or writing pattern for something AetherState already recognizes. It helps the system understand your wording. It does not grant abilities, change game rules, or make a statement true.

**Player Lessons** let you save explicit preferences:

- narration lessons tell the narrator what to do or avoid;
- intent lessons can help choose between two meanings only when your current sentence is genuinely ambiguous.

AetherState does not automatically mine your chats to create these entries. You create, inspect, revise, disable, re-enable, or remove them yourself. Your current words always win.

## Main and helper models

The **main model** writes the story and handles Creator generation.

Optional **helper models** can handle background work such as state extraction, memory reflection, embeddings, or contradiction checking. They can use the same provider as the main model or separate local/cloud endpoints.

Background state tracking does not wait in the middle of the visible story stream. If a helper fails, the chat continues.

Supported OpenAI-compatible choices include hosted providers and local engines such as KoboldCpp, llama.cpp, Ollama, LM Studio, vLLM, and oobabooga.

## Prompt size, caching, and performance

AetherState tries to send the model what matters now rather than every detail it has ever stored.

- absent NPC details stay local until they become relevant again;
- old facts can be replaced by newer versions without erasing their history;
- recent quests receive more prompt space than stale ones;
- compact briefing and compact RPG-rule options are available;
- supported providers can reuse a conversation-specific prompt cache;
- optional prewarming can prepare that cache when a chat opens.

The Console can display token use and cache information when the provider returns it.

## Useful settings

Settings live in <code>aetherstate-data/config.toml</code> and can also be changed through the Console. See <code>config.example.toml</code> for the complete reference.

| Setting | Default | Meaning |
|---|---:|---|
| <code>[extraction] cadence_turns</code> | 1 | Update state every N turns |
| <code>[extraction] intake_chars</code> | 12000 | How much recent chat the helper reads |
| <code>[assist.groups] extraction</code> | assist | Choose rules, main model, helper model, or off |
| <code>[assist.groups] linter_nli</code> | rules | Optional contradiction checking; off by default |
| <code>[manual_override] enabled</code> | false | Allow direct editing of naturally changing values |
| <code>[user_guard] name</code> | empty | Your persona name, used to protect your voice |
| <code>[consent] safewords</code> | empty | Player messages that immediately freeze state escalation |
| <code>[specialization] name</code> | none | Advanced global fallback only; normally choose Chat or RPG per chat in the SillyTavern panel |

Your provider base URL normally includes its version suffix, such as <code>/v1</code>.

## Optional contradiction checking

AetherState can use a small helper model to notice when new narration directly contradicts an established fact. It prepares a correction for the next turn; it does not rewrite text you already saw.

This is off by default. A local NLI helper is available under <code>nli-shim/</code> for Players who want it.

## Privacy

- No telemetry or analytics.
- State, configuration, SQLite databases, and local diagnostics stay under <code>aetherstate-data/</code> by default.
- AetherState sends chat or Creator content only to the model endpoints you configure.
- Local-first does not mean fully offline when a cloud model or helper feature is enabled.
- PlayerLex and Player Lessons do not automatically learn across users or mine chat history.
- System Health retains only declared subsystem and condition codes, classification, severity,
  aggregate counts, times, recovery state, and a bounded safe correlation identifier. It never
  stores prompts, replies, exception text, credentials, endpoints, or arbitrary logs.
- Secure removal clears active AetherState-owned database, cache, and WAL evidence. It cannot erase external backups or text a model provider already retained.
- API keys saved in the Console go to the operating system credential vault. Ordinary AetherState
  configuration stores only opaque references. Environment injection remains available when a
  secure vault is unavailable.

## Troubleshooting

- **Nothing updates:** confirm the SillyTavern connection points to <code>http://127.0.0.1:9130/v1</code> and the AetherState panel says the proxy is online.
- **The extension looks unchanged after an update:** restart SillyTavern and hard-refresh the browser with **Ctrl+Shift+R**.
- **A new chat seeded incorrectly:** use <code>/aether-genesis</code> to reseed it.
- **Authentication fails:** save the provider key in the AetherState Console. A saved AetherState
  credential takes priority over a SillyTavern placeholder.
- **The Console takes a moment to appear:** leave the launcher open, wait for startup, and refresh once.
- **Antivirus interferes with Python installation:** try <code>python -m pip install --use-feature=truststore -e .</code>
- **A local model cannot follow the full RPG instructions:** enable the compact RPG contract or use a stronger story model.

## Honest current limits

- AetherState is actively developed and some systems are still expanding.
- Automatic state extraction can be wrong. The Console and manual controls exist so you can inspect and correct it.
- The RPG truth gate remains off by default.
- Recognizing a claim does not prove it is true, prove that someone lied, or create a world-changing event.
- A saved narration preference can be delivered to the model, but AetherState cannot guarantee that every model follows it perfectly.
- A normal Continue does not advance mechanics or create a new state-changing turn.
- Run only one Continue or regeneration against a given assistant reply at a time; simultaneous
  generations for the same reply do not yet have a stable external identity for exact correlation.
- Unsupported world-event effects remain story information rather than inventing mechanics.
- Direct Player damage is currently single-target; complete area-of-effect and unrestricted multi-target mechanics remain future work.
- There is no built-in cloud sync or cross-user learning.
- Full Creator, panel, Chat Continuity, RPG Player HUD, and War Room support currently requires SillyTavern.

## For maintainers and curious Players

AetherState stores committed changes in a local append-only journal so retries, swipes, reopening, replay, and branches can keep stable identities instead of casually repeating effects.

The sealed Semantic Atlas currently contains **327 meanings**:

- 265 capability meanings;
- 13 character and object reference meanings;
- 18 scene meanings;
- 15 action meanings;
- 16 speech and claim meanings.

Those meanings help recognize language. They do not grant powers or establish truth.

### Maintainer proof and learning boundaries

The public repository carries four named hardening proof sets:

- **Stage 1 Post-1.24 Safety Baseline** preserves the known public behavior surface, package and
  installer shape, schema history, cross-platform checks, and bounded terminal evidence.
- **Stage 2 Semantic Cube Narrator-Output Integrity** adds the narrator-output integrity matrix,
  defect ledger, focused regressions, and cumulative terminal gate.
- **Stage 3 Versioned Database Evolution** establishes the shared ordered migration registry,
  exact historical schema admission, retryable cleanup, and protected cumulative gate.
- **Stage 4 System Health** adds content-free durable health aggregation, truthful status and
  diagnostics, a compact Console view, and a deterministic audit of every tracked broad exception
  boundary.

The private development workspace may also use **AetherState Engineering Learning**, whose stable
short name is **Proofbook**. It is a separate developer-only lesson ledger: it is not part of the
runtime or public package, does not read Player sessions, and cannot grant mechanics, establish
truth, settle outcomes, or train the narrator.

This repository also includes a separate **Public Proofbook** in
[`proofbook/`](proofbook/README.md). It is a curated, clean-clone-safe engineering reference built
only from public repository files. Its history and identities are independent from the private
ledger; private evidence is never copied into it. Public contributors can use it directly:

~~~bash
python tools/proofbook/engineering_learning.py validate
python tools/proofbook/publication_attestations.py validate
python tools/proofbook/engineering_learning.py status
python tools/proofbook/engineering_learning.py brief --task "describe the change" --path "relative/path.py"
~~~

The optional public [`aetherstate-code-learning`](skills/aetherstate-code-learning/SKILL.md) skill
teaches Codex the same public/private/abstain boundary. Proofbook remains developer-only: it is not
imported by AetherState, included in the wheel, used as Player data, or used as model-training data.

For development:

~~~bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests tools
~~~

Start with [the maintainer map](docs/00-MAINTAINER-MAP.md). Runtime corpus files under <code>corpus/</code> are versioned product data and must stay aligned with their manifests and builders.

## License

MIT. See [LICENSE](LICENSE).
