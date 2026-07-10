"""World-specific Narrator card generation (RPG / DM mode).

The generic "Narrator" card (build_narrator_card.py at the repo root) is world-agnostic by
design — one card for every world. In play that made a real problem visible: when you open a
chat in SillyTavern you cannot SEE which world you are traversing. The card, its name, its
first message are all generic, so the world you carefully built in the Creator is invisible
until the model happens to mention it.

This module projects a COMMITTED world (creator.world_from_state) + the Player Card into a
V2 SillyTavern character card, so the world you built is the world you see the moment the
chat opens: its name in the header, its setting/factions/cast inside the card, its opening
scene as the very first message, a genre-tinted avatar in the character grid.

It is a READ-ONLY projection of the ledger — exactly like the briefing. It mints no truth,
reads no registry at replay, never touches the token stream, and stays byte-identical for a
`none` session (nothing here runs unless a route calls it). Pure stdlib (no Pillow), so it is
a weak-model / no-model floor: the card builds deterministically from committed state, no LLM
required. By Bean (AetherState, MIT)."""
from __future__ import annotations

import base64
import json
import math
import struct
import zlib
from typing import Optional

CARD_DATA_VERSION = "2.0"          # chara_card_v2 spec version
GEN_VERSION = "aether-world-1.1"   # this generator's version (stamped into extensions)
SEED_VERSION = "aether-seed-1"     # structured world+player seed carried INSIDE the card so a
#                                    fresh chat can auto-commit the ledger (the ST extension
#                                    reads it and POSTs /aether/session/{sid}/seed). This is the
#                                    fix for "you have to re-apply the world to every new chat":
#                                    the card is the carrier; no LLM needed (weak-model floor).
_W, _H = 512, 768                  # avatar dimensions (V2 card portrait)

# ---- the DM contract (the ledger-over-prose spine, shared with the generic Narrator card).
# Second person so it reads cleanly no matter what the card's {{char}} name resolves to
# (the world's name, here) — the generic card leaned on "{{char}} is not a person" phrasing
# that turned awkward once the card is NAMED after the world.
_CONTRACT = (
    "You are the Narrator — the voice of this world and everyone in it: every place, every "
    "NPC, every faction, every consequence. You are never the Player.\n\n"
    "THE LEDGER. AetherState rides alongside this chat and injects bracketed state blocks — "
    "[SCENE], [PLAYER], [QUEST], [FACTIONS], [RELATIONS], [EFFECTS], [GEAR], [INVENTORY], "
    "[DIRECTIVE], [RULES], [WORLD] and kin. These blocks are committed truth: the "
    "ledger. They outrank chat history, including your own earlier prose. Where prose and "
    "ledger disagree, the ledger wins.\n\n"
    "THE PLAYER. The human is the Player; their character is defined by the [PLAYER] block, "
    "not by a chat handle or persona. Use the name [PLAYER] gives; until then, say \"you\". "
    "Never invent the Player's character beyond what the ledger or the Player established, "
    "and never write the Player's dialogue, actions, decisions, or inner thoughts. End every "
    "reply the moment the Player must act.\n\n"
    "THE MECHANICS. The engine — never you — resolves dice, checks, damage, loot, and stats. "
    "When a [DIRECTIVE] reports an outcome, narrate exactly that outcome; a resolved check "
    "settles that attempt now — never re-roll, soften, upgrade, or reverse it. Use only the "
    "skills, abilities, items, and effects the ledger shows. A Player cannot assert new "
    "powers into existence: when they reach without basis, show them a road in the world to "
    "earn it instead of refusing. When the fiction changes what is true — an effect gained, "
    "an item taken, a place entered, a bond shifted — emit the matching ledger tag on its "
    "own line so the engine can commit it. The [RULES] block lists the exact tag formats "
    "(e.g. [status gained | Mira | Terrified | negative]); use only those — there is no "
    "'[TAGS]' tag, and an invented tag is silently ignored.\n\n"
    "THE VOICE. Vivid, concrete, sensory second person, present tense. NPCs speak in their "
    "own voices and pursue their own agendas. The world is honest and raw: dangers are real, "
    "choices bind, failure opens new roads instead of ending the story. Characters named in "
    "state blocks are KNOWN of, not necessarily on-scene — only [SCENE]'s present list is "
    "here now. Stay inside the fiction to the last word; end on the world's beat — a sound, a "
    "look, a silence that demands an answer — never an out-of-character prompt like \"What "
    "will you do?\"."
)

_PERSONALITY = ("Omniscient, impartial, vivid; a game master who loves the world more than any "
                "outcome; faithful to the ledger, generous with detail, never precious about "
                "the Player's plans.")

_POST_HISTORY = ("You are the Narrator: speak the world and its people, never the Player. The "
                 "bracketed state blocks are the ledger of what is true — honor them over chat "
                 "history; a [DIRECTIVE] outcome is final. Only [SCENE]'s present list is "
                 "on-scene; other known characters arrive when the fiction earns it. Emit the "
                 "exact ledger tags the [RULES] block teaches when the fiction changes state — "
                 "never invented ones. End in-fiction where the "
                 "Player must act — never an out-of-character prompt like \"What will you do?\".")

_MES_EXAMPLE = ("<START>\n"
                "{{user}}: I ease the door open and slip inside.\n"
                "{{char}}: The hinges hold, barely. Cold air rises off the stone, thick with "
                "tallow and wet earth. Below, a single lamp burns where no lamp should be. "
                "Someone is home.\n"
                "<START>\n"
                "{{user}}: \"It's me. Open up.\"\n"
                "{{char}}: The bolt slides. A face appears in the gap — pale, red-rimmed eyes, "
                "white knuckles on the door's edge. \"You shouldn't have come back,\" they "
                "whisper. \"They were here. They knew your name.\"\n"
                "[status gained | Mira | Terrified | negative]\n"
                "The door opens just wide enough for you to decide.")


# ------------------------------------------------------------------ small helpers
def _s(v, n: int = 400) -> str:
    """Coerce to a stripped string, capped at n chars (never raises)."""
    try:
        out = str(v or "").strip()
    except Exception:
        return ""
    return out[:n]


def _lst(v) -> list:
    return v if isinstance(v, list) else []


def _s_sent(v, n: int) -> str:
    """Clamp prose to <= n chars WITHOUT a mid-word cut: prefer the last sentence end,
    then the last word break, and mark a real cut with an ellipsis (2026-07-09 — the
    baked card greeting used to end 'find out wh')."""
    t = _s(v, n + 400)
    if len(t) <= n:
        return t
    cut = t[:n]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if dot >= int(n * 0.5):
        return cut[:dot + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= int(n * 0.6) else cut).rstrip(" ,;:—-") + "…"


def _name_only(line: str) -> str:
    """'Name — description' -> 'Name' (creator stores factions/locations this way)."""
    for sep in (" — ", " – ", " - ", ": "):
        if sep in line:
            return line.split(sep, 1)[0].strip()
    return line.strip()


def card_title(world: Optional[dict]) -> str:
    """The card's NAME — the single most visible 'which world am I in' cue (chat header +
    character grid). The world's own name when it has one; the neutral 'Narrator' otherwise."""
    name = _s((world or {}).get("name"), 48)
    return name or "Narrator"


# ------------------------------------------------------------------ text sections
def _world_section(world: dict) -> str:
    name = _s(world.get("name"), 60) or "an unnamed world"
    genre = _s(world.get("genre"), 40).replace("_", " ")
    head = f"THE WORLD — {name}" + (f" ({genre})" if genre else "")
    lines = [head]
    setting = _s_sent(world.get("setting"), 2400)
    if setting:
        lines.append(setting)
    date, tod = _s(world.get("date"), 80), _s(world.get("time"), 40).replace("_", " ")
    when = " · ".join(x for x in (date, tod) if x)
    if when:
        lines.append(f"When: {when}.")
    tone = _s(world.get("tone"), 120)
    if tone:
        lines.append(f"Tone: {tone}.")
    aspects = [_s_sent(a, 400) for a in _lst(world.get("aspects")) if _s(a)]
    if aspects:
        lines.append("Laws of this world: " + "; ".join(aspects[:8]) + ".")
    factions = [_name_only(_s(f, 80)) for f in _lst(world.get("factions")) if _s(f)]
    if factions:
        lines.append("Factions in play: " + ", ".join(factions[:8]) + ".")
    locs = [_name_only(_s(loc, 80)) for loc in _lst(world.get("locations")) if _s(loc)]
    if locs:
        lines.append("Places: " + ", ".join(locs[:10]) + ".")
    npcs = []
    for n in _lst(world.get("npcs")):
        if not isinstance(n, dict):
            continue
        nm = _s(n.get("name"), 48)
        if not nm:
            continue
        role, desc = _s(n.get("role"), 48), _s(n.get("desc"), 120)
        tail = " — ".join(x for x in (role, desc) if x)
        npcs.append(f"{nm}" + (f" ({tail})" if tail else ""))
    if npcs:
        lines.append("Known figures: " + "; ".join(npcs[:8]) + ".")
    quest = _s_sent(world.get("opening_quest"), 700)
    if quest:
        lines.append(f"The opening thread: {quest}")
    return "\n".join(lines)


def _player_section(player: Optional[dict]) -> str:
    if not isinstance(player, dict):
        return ""
    name = _s(player.get("name"), 48)
    concept = _s(player.get("concept") or player.get("class"), 80)
    appearance = _s_sent(player.get("appearance") or player.get("description"), 900)
    if not name and not concept and not appearance:
        return ""
    who = name or "The Player"
    head = f"THE PLAYER — {who}" + (f", {concept}" if concept else "")
    line = head + ". Their sheet is the [PLAYER] block; honor it."
    if appearance:
        line += f"\nAppearance: {appearance}"
    return line


def _description(world: dict, player: Optional[dict]) -> str:
    blocks = [_world_section(world)]
    ps = _player_section(player)
    if ps:
        blocks.append(ps)
    blocks.append(_CONTRACT)
    return "\n\n".join(b for b in blocks if b)


def _first_mes(world: dict, player: Optional[dict]) -> str:
    """The player's first sight of their world. Rooted in the committed opening scene when
    there is one; a 'the world wakes' framing otherwise. Always ends in-fiction (dm-rules)."""
    name = _s(world.get("name"), 60)
    genre = _s(world.get("genre"), 40).replace("_", " ")
    opening = _s_sent(world.get("opening_scene"), 1800)
    quest = _s_sent(world.get("opening_quest"), 700)
    banner = f"*{name}*" if name else "*A world, newly spoken.*"
    if genre and name:
        banner = f"*{name} — {genre}*"
    parts = [banner, ""]
    if opening:
        parts.append(opening)
        if quest:
            parts.append(f"\nAt the edge of it all: {quest}")
        parts.append("\nThe world holds its breath, waiting on you.")
    else:
        setting = _s_sent(world.get("setting"), 900)
        if setting:
            parts.append(setting)
        parts.append("\nI am the Narrator, and I keep this world's truth without mercy or "
                     "favor. Tell me where you stand and what you do, and the first scene "
                     "opens around you.")
    return "\n".join(parts).strip()


def _scenario(world: dict) -> str:
    name = _s(world.get("name"), 60) or "a living world"
    setting = _s(world.get("setting"), 400)
    base = f"A living world — {name} — managed by the AetherState engine. "
    if setting:
        base += setting + " "
    return base + ("The current scene, cast, quests, and facts arrive in bracketed state "
                   "blocks each turn; play begins where the world's opening scene places you.")


def _trim(v, s_cap: int = 4000, l_cap: int = 48):
    """Cap string lengths and list sizes so a pathologically long doc can't bloat the embedded
    PNG — structure and values are otherwise preserved verbatim (fidelity kept, Bean 10 §10)."""
    if isinstance(v, str):
        return v[:s_cap]
    if isinstance(v, list):
        return [_trim(x, s_cap, l_cap) for x in v[:l_cap]]
    if isinstance(v, dict):
        return {k: _trim(x, s_cap, l_cap) for k, x in v.items()}
    return v


def _player_meaningful(p) -> bool:
    """True when the card should carry a Player Card seed — a character the user actually built
    (named, or with picked skills/abilities/custom mechanics). A world-only card carries NO
    player, so a fresh chat gets genesis's default Player Card floor instead of a baked blank."""
    if not isinstance(p, dict):
        return False
    if str(p.get("name") or "").strip():
        return True
    if p.get("skills") or p.get("abilities"):
        return True
    c = p.get("custom") or {}
    return bool(c.get("skills") or c.get("abilities") or p.get("defs"))


def seed_payload(world: Optional[dict], player: Optional[dict]) -> dict:
    """The world + Player Card docs the card carries so a fresh chat rebuilds the ledger with no
    LLM. Same doc shapes the Creator posts to /world and /player — the ST extension reads this
    seed on chat-open and replays it through /aether/session/{sid}/seed. Read-only projection;
    never a resolution channel (the world_to_ops/player_to_ops apply path validates it)."""
    seed: dict = {"world": _trim(world) if isinstance(world, dict) else {}}
    if _player_meaningful(player):
        seed["player"] = _trim(player)
    return seed


def build_card(world: Optional[dict], player: Optional[dict] = None) -> dict:
    """A V2 chara card (dict) built from a committed world doc + optional Player Card.
    Fail-open: a None/empty world still yields a valid, if generic, Narrator card."""
    world = world if isinstance(world, dict) else {}
    title = card_title(world)
    genre = _s(world.get("genre"), 40)
    tags = ["aetherstate", "narrator", "rpg", "world"]
    if genre:
        tags.append(genre)
    if _s(world.get("name")):
        tags.append(_s(world.get("name"), 40))
    notes = ("Auto-generated by AetherState's World/Character Creator for the world "
             f"\"{_s(world.get('name'), 60) or 'unnamed'}\". Point SillyTavern's API at the "
             "AetherState proxy (default http://127.0.0.1:9130/v1), keep your Persona as the "
             "default \"User\" (your real character lives in the [PLAYER] block), open a chat "
             "with this card, and play. Regenerate this card from the Creator whenever the "
             "world changes. " + GEN_VERSION)
    return {
        "spec": "chara_card_v2",
        "spec_version": CARD_DATA_VERSION,
        "data": {
            "name": title,
            "description": _description(world, player),
            "personality": _PERSONALITY,
            "scenario": _scenario(world),
            "first_mes": _first_mes(world, player),
            "alternate_greetings": [],
            "mes_example": _MES_EXAMPLE,
            "system_prompt": "",
            "post_history_instructions": _POST_HISTORY,
            "creator_notes": notes,
            "tags": tags,
            "creator": "AetherState Creator",
            "character_version": GEN_VERSION,
            "extensions": {"aetherstate": {"role": "narrator", "generated": True,
                                           "world": _s(world.get("name"), 60),
                                           "genre": genre, "min_proxy": "1.6.0",
                                           "seed_version": SEED_VERSION,
                                           "seed": seed_payload(world, player)}},
        },
    }


# ------------------------------------------------------------------ procedural avatar
# Genre-tinted so different worlds are visibly different at a glance in the character grid —
# another "which world" cue. Palette = (top RGB, bottom RGB, glow RGB); perturbed by a hash of
# the world name so two worlds of the same genre still differ.
_GENRE_PALETTE = {
    "fantasy":    ((14, 10, 30), (40, 24, 58), (150, 120, 230)),
    "sci_fi":     ((6, 14, 22), (10, 34, 48), (90, 200, 220)),
    "cyberpunk":  ((14, 6, 22), (34, 10, 40), (240, 70, 180)),
    "post_apoc":  ((20, 14, 8), (44, 30, 16), (220, 150, 70)),
    "modern":     ((10, 12, 16), (26, 30, 38), (150, 170, 200)),
    "historical": ((16, 12, 8), (38, 28, 18), (200, 170, 110)),
    "horror":     ((8, 6, 8), (24, 10, 12), (200, 40, 50)),
    "noir":       ((8, 8, 10), (26, 26, 30), (170, 180, 200)),
}
_DEFAULT_PALETTE = ((10, 10, 28), (28, 24, 72), (120, 150, 220))


def _palette(world: Optional[dict]) -> tuple:
    world = world or {}
    base = _GENRE_PALETTE.get(_s(world.get("genre"), 40).lower(), _DEFAULT_PALETTE)
    seed = zlib.crc32(_s(world.get("name") or world.get("genre"), 60).encode("utf-8"))
    shift = ((seed & 0xFF) - 128) / 128.0 * 18.0          # ±18 hue nudge, deterministic
    (t, b, g) = base
    t = tuple(min(255, max(0, c + shift)) for c in t)
    g = tuple(min(255, max(0, c - shift)) for c in g)
    return (t, b, g, (seed >> 8))


def _pixel(x, y, pal):
    (tr, tg, tb), (br, bg, bb), (gr, gg, gb), noise = pal
    t = y / _H
    r = tr + (br - tr) * t
    g = tg + (bg - tg) * t
    b = tb + (bb - tb) * t
    dx, dy = (x / _W - 0.5), (y / _H - 0.42) * 1.5
    d = math.sqrt(dx * dx + dy * dy)
    glow = max(0.0, 1.0 - d * 2.2) ** 2.2
    r += gr / 255 * 220 * glow
    g += gg / 255 * 220 * glow
    b += gb / 255 * 220 * glow
    ring = abs(math.sin(d * 28.0)) ** 24 * max(0.0, 1.0 - d * 1.8)
    r += 35 * ring
    g += 45 * ring
    b += 70 * ring
    h = (x * 73856093 ^ y * 19349663 ^ noise) & 0xFFFF     # world-seeded starfield
    if h < 70:
        sp = (h % 60) + 110
        r += sp
        g += sp
        b += sp
    return min(255, int(r)), min(255, int(g)), min(255, int(b))


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def card_png(card: dict, world: Optional[dict] = None) -> bytes:
    """A V2 character PNG: genre-tinted avatar with the card JSON embedded in a tEXt chunk
    (the SillyTavern import format). Pure stdlib."""
    pal = _palette(world if world is not None
                   else (card.get("data", {}).get("extensions", {})
                         .get("aetherstate", {})))
    raw = bytearray()
    for y in range(_H):
        raw.append(0)                                      # filter: None
        for x in range(_W):
            raw += bytes(_pixel(x, y, pal))
    ihdr = struct.pack(">IIBBBBB", _W, _H, 8, 2, 0, 0, 0)
    payload = base64.b64encode(json.dumps(card, ensure_ascii=True).encode("utf-8"))
    text = b"chara\x00" + payload
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"tEXt", text)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))
