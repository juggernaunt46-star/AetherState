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
  a World & Character Creator, items & gear, statuses & conditions, factions & affinity,
  persistent locations. Off by default; a non-RPG session is byte-identical to plain AetherState.
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
   - **Main model** (writes the story): endpoint + API key, e.g. `https://api.venice.ai/api/v1`.
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
| `[manual_override] enabled` | false | Allow editing organic values |
| `[user_guard] name` | "" | Your persona's name — keeps the model out of your voice |
| `[consent] safewords` | [] | Any of these in YOUR message freezes the scene instantly |

`base_url` always includes the version segment, exactly like SillyTavern custom endpoints:
`https://api.venice.ai/api/v1` · `https://api.openai.com/v1` · `http://127.0.0.1:11434/v1`
(Ollama) · `http://127.0.0.1:5001/v1` (KoboldCpp).

## The helper ("assist") model

State tracking is a background job — it never blocks your story. Small local models work well
(Llama 3.1 8B on KoboldCpp is plenty; set `tier = "small"`), or point it at your main API.
Thinking/reasoning models are handled automatically: reasoning is disabled for tracking calls,
or budgeted if you set `[extraction] thinking = "on"`.

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
