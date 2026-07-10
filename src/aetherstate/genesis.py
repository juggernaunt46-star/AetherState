"""Genesis seeding (Q23): a character card + opening prompt
already encode initial state — seed it instead of starting blank.

Two stages, both idempotent via sessions.genesis ('' -> 'rules' -> 'done'/'skipped'):

Stage A — RULES, inline at the first request of a new session. Pure keyword scan over
card + opening prompt (sub-ms, hot-path safe): explicit obsessions/cravings only,
precision-first. Turn 1's header already carries what the card states outright.

Stage B — assist-LLM, post-stream after turn 1 (cold path). Reads card + opening prompt
and proposes the full matrix: entities, presence, obsessions, craving levels tuned to
the opening (sated <-> ravenous), gear via the 3-rung ladder (explicit -> personality ->
setting), relationships, facts/secrets, consent, positions, contacts, scene. Applied
with source="genesis" (authority: initialization may set organic values — 02 SS12b
extension, Q23.2). Later extraction refines; linter/director treat it as ordinary state.

Fail-open at every seam (invariants 1-3): no assist model -> Stage A only; everything
fails -> today's empty start. Exposure is NEVER seeded — it derives from worn gear.
"""
from __future__ import annotations

import json
import logging
import re

from .extraction import repair_json
from .state import apply_delta, current_state, validate_op

log = logging.getLogger("aetherstate.genesis")

CARD_CAP = 6000
PROMPT_CAP = 2000
MAX_OPS = 40

# Stage-A lexicons (precision-first: explicit statements only)
_OBSESSION_RE = re.compile(
    r"(?:obsess(?:ed|ion)\s+(?:with|over)|fixat(?:ed|ion)\s+(?:on|upon)|"
    r"consumed\s+by\s+(?:a\s+)?(?:desire|need|hunger)\s+for)"
    r"\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)", re.IGNORECASE)
_CRAVING_RE = re.compile(
    r"(?:addict(?:ed)?\s+to|craves?|dependen(?:t|ce)\s+on|can(?:not|'t)\s+(?:go|live)\s+"
    r"without|(?:un(?:quenchable|ending)|insatiable)\s+(?:thirst|hunger|appetite)\s+for)"
    r"\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)", re.IGNORECASE)
_TRIM = {"and", "or", "but", "it", "for", "that", "which", "the", "a", "an", "to", "of",
         "his", "her", "their", "its", "any", "some", "more"}
_SATED = re.compile(r"\b(sated|satiated|satisfied|well[- ]fed|content|freshly\s+fed|"
                    r"just\s+(?:ate|fed|drank|indulged))\b", re.IGNORECASE)
_RAVENOUS = re.compile(r"\b(ravenous|starving|desperate|withdrawal|shaking|craving|"
                       r"deprived|hasn'?t\s+(?:eaten|fed|had)|days?\s+without)\b",
                       re.IGNORECASE)
_NAME_RE = re.compile(r"^\s*(?:name|character)\s*[:=]\s*([A-Z][\w '\-]{1,40})",
                      re.IGNORECASE | re.MULTILINE)
_CAST_RE = re.compile(r"^\s*(?:characters|cast)\s*[:=]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SENTINEL = re.compile(r"<<AETHER:[^>]*>>\s?")

_LLM_SYSTEM = (
    "You initialize a roleplay state tracker from a character card and the scene's "
    "opening. Reply with ONLY a JSON array of operation objects. Allowed ops and their "
    "required fields:\n"
    '{"op":"entity_add","name":str}\n'
    '{"op":"presence","entity":str,"present":true}\n'
    '{"op":"scene_set","location":str,"participants":[str],"phase":"setup"}\n'
    '{"op":"obsession","char":str,"target_kind":"entity|act_category|substance|object|'
    'concept","target":str,"set":0-100,"flavor":"romantic|possessive|protective|hostile|'
    'curious|ambition|reverent|other","behavior_note":str}\n'
    '{"op":"craving","char":str,"substance":str,"action":"adjust","delta":0-100}  '
    "// delta = INITIAL level; read the opening: sated ~15, neutral ~45, ravenous ~75\n"
    '{"op":"clothing","char":str,"item":str,"action":"don","category":str,'
    '"covers":[zones]}  // worn categories: base_layer top bottom outerwear footwear '
    "headwear handwear facewear eyewear neckwear armor (give covers); carried: "
    "load_bearing backpack pouch tool weapon ammunition consumable medical electronics "
    "accessory (no covers). Rung 1: what card/opening states. Rung 2: what this "
    "personality would wear/carry. Rung 3: what the setting implies. Zones: head face "
    "mouth neck shoulders chest breasts nipples back arms wrists hands waist hips ass "
    "anus genitals thighs legs feet\n"
    '{"op":"relationship_adj","from_char":str,"to_char":str,"dimension":"trust|affection'
    '|respect|desire|tension|fear|familiarity","delta":-30..30}\n'
    '{"op":"reveal_fact","learner":str,"statement":str,"source":"told",'
    '"is_secret":bool}  // beliefs, facts, and secrets the card establishes\n'
    '{"op":"consent_set","subject":str,"partner":str,"category":"kissing|manual|'
    'oral_give|oral_receive|vaginal|anal|toys|restraint|impact|degradation|praise|'
    'exhibition|group|roleplay_scene|other","level":"unknown|hesitant|granted|'
    'enthusiastic|soft_limit|hard_limit"}  // only what card/opening establishes\n'
    '{"op":"position","participants":[str],"base":"standing|sitting|kneeling|straddling|'
    'lying_back|lying_front|on_all_fours|bent_over|held_carried"}\n'
    '{"op":"contact","action":"start","from_char":str,"to_char":str,"type":"touching|'
    'caressing|gripping|kissing"}  // only if the opening shows physical touch\n'
    '{"op":"mood","char":str,"valence":-100..100,"energy":-100..100}\n'
    '{"op":"goal","char":str,"action":"add","text":str}\n'
    "Seed ONLY what the card and opening support. Persistent traits come from the card; "
    "initial LEVELS (craving, mood) also read the opening. Use the character's plain "
    "name for char/entity fields. ALWAYS include an entity_add for every character you "
    "reference. NEVER invent explicit sexual state absent from the text. Max "
    f"{MAX_OPS} ops. NO commentary, NO markdown fences.\n"
    "Example output for a card about a vampire noble named Liss who is fixated on an "
    "heirloom ring, opens starving in her cold manor at night wearing a gown:\n"
    '[{"op":"entity_add","name":"Liss"},{"op":"presence","entity":"Liss","present":true},'
    '{"op":"scene_set","location":"manor hall","participants":["Liss"],"phase":"setup"},'
    '{"op":"obsession","char":"Liss","target_kind":"object","target":"heirloom ring",'
    '"set":55,"flavor":"possessive"},{"op":"craving","char":"Liss","substance":"blood",'
    '"action":"adjust","delta":75},{"op":"clothing","char":"Liss","item":"velvet gown",'
    '"action":"don","category":"top","covers":["chest","stomach","hips","thighs"]},'
    '{"op":"mood","char":"Liss","valence":-20,"energy":30}]')


def card_and_prompt(doc: dict) -> tuple[str, str]:
    """(card_text, opening_prompt) from the first request. Card = system content (+first
    assistant greeting); opening = first user message. Caps keep the LLM call bounded."""
    try:
        msgs = doc.get("messages") or []
        card, greeting, opening = [], "", ""
        for m in msgs:
            c = m.get("content")
            if not isinstance(c, str):
                continue
            role = m.get("role")
            if role == "system":
                c = _SENTINEL.sub("", c)     # never let a stamp pollute the card text
                if c.strip():
                    card.append(c)
            elif role == "assistant" and not greeting:
                greeting = c
            elif role == "user" and not opening:
                opening = c
        card_text = ("\n".join(card)).strip()[:CARD_CAP]
        if not card_text:               # no system card -> no genesis (a greeting alone
            return "", ""               # is not a character definition; precision-first)
        if greeting:
            card_text = f"{card_text}\n[Opening message]\n{greeting[:1500]}"
        return card_text, opening[:PROMPT_CAP]
    except Exception:
        return "", ""


def _initial_level(prompt: str, card: str) -> int:
    """Craving's starting level is scenario-dependent (sated <-> ravenous)."""
    text = f"{prompt}\n{card[-800:]}"
    if _RAVENOUS.search(text):
        return 75
    if _SATED.search(text):
        return 15
    return 45


def _char_name(card: str) -> str:
    m = _NAME_RE.search(card)
    return m.group(1).strip() if m else ""


def _clean_target(raw: str) -> str:
    """1-2 word target; cut at the first glue word (precision over recall)."""
    words = []
    for w in raw.strip().rstrip(".,;").split():
        if w.lower() in _TRIM:
            break
        words.append(w)
        if len(words) == 2:
            break
    return " ".join(words)


def rules_ops(card: str, prompt: str, speaker: str = "") -> list[dict]:
    """Stage A. Name source (2026-07-04, 3-tier): (1) the stamp's
    speaker = {{char}} — reliable on EVERY real card; (2) 'Name:' regex fallback;
    (3) optional 'Characters:'/'Cast:' line seeds the listed cast deterministically
    (opt-in precision booster, never required). No name at all -> no seed."""
    name = (speaker or "").strip() or _char_name(card)
    if not name:
        return []
    low = card.lower()                       # 2026-07-09: a NARRATOR/world card's "character"
    dm_card = ("you are the narrator" in low  # is the WORLD — never stage it as a present
               or "never the player" in low   # person (it corrupted [SCENE]'s cast: the tower
               or "the world —" in low)       # itself showed up as 'here')
    ops: list[dict] = [] if dm_card else [
        {"op": "entity_add", "name": name},
        {"op": "presence", "entity": name, "present": True}]
    m = _CAST_RE.search(card)
    if m:                                    # cast tracked; presence left to play
        for extra in re.split(r"[,;]", m.group(1))[:12]:
            extra = extra.strip().strip(".")
            if extra and extra.lower() != name.lower() and 1 < len(extra) <= 40:
                ops.append({"op": "entity_add", "name": extra})
    if dm_card:                              # the world has no obsessions/cravings of its own
        return ops
    seen: set = set()
    for m in _OBSESSION_RE.finditer(card):
        target = _clean_target(m.group(1))
        if not target or target.lower() in seen:
            continue
        seen.add(target.lower())
        ops.append({"op": "obsession", "char": name, "target_kind": "concept",
                    "target": target, "set": 65})
    for m in _CRAVING_RE.finditer(card):
        sub = _clean_target(m.group(1))
        if not sub or sub.lower() in seen:
            continue
        seen.add(sub.lower())
        ops.append({"op": "craving", "char": name, "substance": sub, "action": "adjust",
                    "delta": _initial_level(prompt, card)})
    return ops                              # entity+presence alone is a valid seed
                                            # (turn-1 header names the character)


def seed_rules(store, cfg, session_id: str, branch_id: str, doc: dict,
               speaker: str = "") -> int:
    """Inline Stage A at the first request of a new session. Sub-ms; fail-open."""
    try:
        if store.genesis_state(session_id) != "":
            return 0
        card, prompt = card_and_prompt(doc)
        if not card.strip():
            store.genesis_mark(session_id, "skipped")
            return 0
        ops = rules_ops(card, prompt, speaker=speaker)
        n = 0
        if ops:
            res = apply_delta(store, session_id, branch_id, 0, ops, "genesis", cfg)
            n = len(res.applied)
        store.genesis_mark(session_id, "rules")
        if n:
            log.info("genesis stage A seeded %d op(s) for %s", n, session_id[:8])
        return n
    except Exception as exc:                 # empty start is always acceptable
        log.warning("genesis rules pass failed open: %s", type(exc).__name__)
        return 0


# ---- RPG player genesis (track 2 of two-track genesis, doc 05 §3.3 / doc 06 §2) ---------
# World genesis (track 1) is the existing two-stage pass above, re-read as the DM/world seed
# when specialization=rpg (the card IS the Dungeon Master). Track 2 below establishes the
# Player Card. RPG-0 ships a deterministic, hot-path-safe skeleton (no LLM): an explicit seed
# if the request carries one, else a registry-less default so [PLAYER] renders from turn 0.
# The point-buy / curated-registry creator lands at RPG-1 (doc 09).
DEFAULT_PLAYER_STATS = {"STR": 10, "DEX": 10, "INT": 10, "CHA": 10, "CUN": 10, "CON": 10}
DEFAULT_PLAYER_HP_MAX = 20


def _player_seed_from_doc(doc: dict, cfg) -> tuple[str, dict]:
    """(player_name, seed_card). Tolerates a few carrier shapes for an explicit seed
    (doc 06 §S4: ST card extensions.aetherstate.player) without depending on any; falls back
    to a sensible default. The player is the USER's character, so the default name is the
    user persona (never the card/DM speaker)."""
    name = (cfg.user_guard.name or "").strip() or "Player"
    seed: dict = {}
    try:
        for key in ("aetherstate_player", "player"):
            if isinstance(doc.get(key), dict):
                seed = doc[key]
                break
        ext = doc.get("extensions")
        if not seed and isinstance(ext, dict) and isinstance(ext.get("aetherstate"), dict):
            if isinstance(ext["aetherstate"].get("player"), dict):
                seed = ext["aetherstate"]["player"]
    except Exception:
        seed = {}
    ident = seed.get("identity") if isinstance(seed.get("identity"), dict) else seed
    if isinstance(ident, dict) and str(ident.get("name", "")).strip():
        name = str(ident["name"]).strip()
    card = {
        "level": (ident or {}).get("level", 1),
        "concept": (ident or {}).get("concept"),
        "pronouns": (ident or {}).get("pronouns"),
        "stats": seed["stats"] if isinstance(seed.get("stats"), dict) else dict(DEFAULT_PLAYER_STATS),
        "skills": seed["skills"] if isinstance(seed.get("skills"), dict) else {},
        "abilities": seed["abilities"] if isinstance(seed.get("abilities"), list) else [],
        "resources": seed["resources"] if isinstance(seed.get("resources"), dict)
                     else {"hp": {"max": DEFAULT_PLAYER_HP_MAX},
                           "stamina": {"max": 12}},   # RPG-5 (doc 10 §6): the universal pool
    }
    if not seed:
        # No explicit seed anywhere: this is the registry-less PLACEHOLDER card (the floor so
        # [PLAYER] renders from turn 0). Mark it so a later authored player_seed (Creator save)
        # REPLACES it without leaving a ghost 'Player' entity behind (2026-07-06 live repro:
        # the placeholder rode alongside the real character as a second player).
        card["_genesis_default"] = True
    return name, card


def seed_player(store, cfg, session_id: str, branch_id: str, doc: dict) -> int:
    """RPG player genesis (doc 05 §3.3). Deterministic and sub-ms (no LLM): writes the Player
    Card via the privileged `player_seed` op (source='genesis'). Idempotent (skips if a Player
    Card already exists); inert unless specialization=rpg; fail-open (an empty Player Card is
    acceptable — [PLAYER] simply won't render)."""
    try:
        if getattr(cfg, "specialization", None) is None or cfg.specialization.name != "rpg":
            return 0
        if current_state(store, branch_id).get("player"):
            return 0                                       # already seeded
        name, card = _player_seed_from_doc(doc, cfg)
        ops = [{"op": "entity_add", "name": name, "kind": "player"},
               {"op": "player_seed", "entity": name, "card": card}]
        res = apply_delta(store, session_id, branch_id, 0, ops, "genesis", cfg)
        if res.applied:
            log.info("genesis player card seeded for %s (player=%r, %d op)",
                     session_id[:8], name, len(res.applied))
        return len(res.applied)
    except Exception as exc:
        log.warning("player genesis failed open: %s", type(exc).__name__)
        return 0


async def seed_llm(store, cfg, get_client, ep, session_id: str, branch_id: str,
                   card: str, prompt: str, speaker: str = "") -> int:
    """Stage B: assist-LLM full-matrix pass, post-stream cold path. Fail-open.

    2026-07-06 live repro of 'genesis works half the time': at chat-open nothing has
    been proxied yet, so the endpoint arrived with model='' (upstream 400) — and the
    25 s shared timeout killed the calls that DID have a model. Both fixed here: the
    endpoint is resolved to a real model first (assist pick > upstream.model > GET
    /models), the call gets a long timeout + a budget that fits ~40 ops, and a HARD
    failure (no reply at all) re-marks 'rules' instead of 'done' so the next trigger
    (first message, chat re-open, /aether-genesis) retries instead of being locked out."""
    from .assist import _chat, resolve_endpoint      # shared client plumbing
    try:
        if store.genesis_state(session_id) not in ("", "rules"):
            return 0
        if not card.strip():
            store.genesis_mark(session_id, "done")
            return 0
        ep = await resolve_endpoint(get_client, cfg, ep)
        if not ep.model:
            log.warning("genesis stage B: no model resolvable at %s — leaving session "
                        "re-seedable", ep.base_url or "(no endpoint)")
            store.genesis_mark(session_id, "rules")
            return 0
        user = (f"CHARACTER CARD:\n{card}\n\n"
                f"MAIN CHARACTER NAME: {speaker or '(infer from card)'}\n\n"
                f"OPENING PROMPT:\n{prompt or '(none)'}\n\nJSON array of seed ops:")
        raw = await _chat(get_client, cfg, ep, _LLM_SYSTEM, user, max_tokens=3200,
                          timeout_s=120.0)
        if raw is None:                      # hard failure (timeout/auth/transport):
            log.warning("genesis stage B: no reply from %s (%s) — leaving session "
                        "re-seedable", ep.base_url, ep.model)
            store.genesis_mark(session_id, "rules")
            return 0
        ops = _presence_with_basis(_parse_ops(raw, speaker=speaker), card, prompt, speaker)
        log.info("genesis stage B: raw=%d chars, %d valid op(s) parsed via %s",
                 len(raw or ""), len(ops), ep.model)
        if not ops and raw:
            log.info("genesis stage B raw head: %r", raw[:300])
        n = 0
        if ops:
            head = store.db.execute("SELECT head_turn FROM branches WHERE branch_id=?",
                                    (branch_id,)).fetchone()
            turn = max(0, (head["head_turn"] if head else 0))
            res = apply_delta(store, session_id, branch_id, turn, ops, "genesis", cfg)
            n = len(res.applied)
            if res.quarantined:
                log.info("genesis: %d applied, %d quarantined: %s", n,
                         len(res.quarantined),
                         "; ".join(str(q.get("reason"))[:60]
                                   for q in res.quarantined[:5]))
        store.genesis_mark(session_id, "done")
        log.info("genesis stage B seeded %d op(s) for %s", n, session_id[:8])
        return n
    except Exception as exc:
        log.warning("genesis LLM pass failed open: %s", type(exc).__name__)
        try:
            store.genesis_mark(session_id, "done")   # never retry-loop a broken pass
        except Exception:
            pass
        return 0


_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def _coerce(op: dict) -> dict:
    """Forgive the mistakes small models actually make (live repro 2026-07-04)."""
    if not isinstance(op, dict):
        return op
    op = dict(op)
    kind = op.get("op") or op.get("type") or op.get("kind")
    if kind and "op" not in op:
        op["op"] = kind
    if op.get("op") == "craving":
        op.setdefault("action", "adjust")
        if "delta" not in op:                # level/set/value -> initial-level delta
            for k in ("level", "set", "value", "intensity"):
                if k in op:
                    op["delta"] = op.pop(k)
                    break
    if op.get("op") == "obsession" and "set" not in op:
        for k in ("intensity", "level", "value"):
            if k in op:
                op["set"] = op.pop(k)
                break
    if op.get("op") == "obsession":
        op.setdefault("target_kind", "concept")
    if op.get("op") in ("clothing", "gear"):
        op.setdefault("action", "don")
    if op.get("op") == "relationship_adj" and "delta" not in op and "value" in op:
        op["delta"] = op.pop("value")
    return op


def _presence_with_basis(ops: list[dict], card: str, prompt: str,
                         speaker: str = "") -> list[dict]:
    """Presence needs an in-world basis (2026-07-06 live repro: notable NPCs staged 'present'
    at the opening for no reason). Keep a stage-B presence=true op only for names the card or
    opening prompt actually places in the scene text — everyone else stays known-but-offstage
    until the fiction brings them on. presence=false and all other ops pass through."""
    basis = f"{card}\n{prompt}".lower()
    spk = (speaker or "").strip().lower()
    keep = []
    for o in ops:
        if o.get("op") == "presence" and bool(o.get("present", True)):
            who = str(o.get("entity", "")).strip().lower()
            if who and who not in basis and who != spk:
                continue
        keep.append(o)
    return keep


def _parse_ops(raw, speaker: str = "") -> list[dict]:
    if not raw:
        return []
    text = _FENCE.sub("", raw)
    # Thinking/verbose models may preface the array with prose (live repro
    # 2026-07-04) — try the whole reply, then from the first '[', then from the
    # last '[{' (models that "think out loud" put the real array at the end).
    candidates = [text]
    i = text.find("[")
    if i > 0:
        candidates.append(text[i:])
    j = text.rfind("[{")
    if j > 0:
        candidates.append(text[j:])
    doc = None
    for cand in candidates:
        try:
            doc = json.loads(repair_json(cand))
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if doc is None:
        return []
    if isinstance(doc, dict):                # tolerate {"ops":[...]} / single-op replies
        doc = doc.get("ops", [doc] if "op" in doc else [])
    if not isinstance(doc, list):
        return []
    out = []
    for op in doc[:MAX_OPS]:
        v = validate_op(_coerce(op))
        if v is None:
            continue
        # Stage B never mints players: the Player Card has its own dedicated track
        # (seed_player / the Creator) and is the USER's character — an assist model reading
        # a card that talks about "the Player" would happily invent one (2026-07-06 live
        # repro: a generic 'Player' character seeded next to the real one).
        if v["op"] == "player_seed" or (v["op"] == "entity_add"
                                        and str(v.get("kind", "")) == "player"):
            continue
        out.append(v)
    # every referenced character must exist or its ops quarantine on alias resolution:
    # auto-prepend entity_add for names the model referenced but forgot to create.
    named = {str(o.get("name", "")).strip().lower() for o in out if o["op"] == "entity_add"}
    if speaker:
        named.add(speaker.strip().lower())   # Stage A already created the speaker
    refs: list[str] = []
    for o in out:
        for k in ("char", "entity", "from_char", "to_char", "subject", "partner",
                  "learner", "teller"):
            v = str(o.get(k, "") or "").strip()
            if v and v.lower() not in named and v.lower() not in [r.lower() for r in refs]:
                refs.append(v)
        for p in (o.get("participants") or []):
            p = str(p).strip()
            if p and p.lower() not in named and p.lower() not in [r.lower() for r in refs]:
                refs.append(p)
    return [{"op": "entity_add", "name": r} for r in refs] + out
