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
import hashlib
import logging
import re

from .claim_ingress import claim_ops_from_text
from .extraction import repair_json
from .state import apply_delta, current_state, merge_baseline_skills, validate_op

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


def _is_narrator_card(card: str) -> bool:
    """A world/narrator card's speaker is a UI role, not a person in the fiction."""
    low = (card or "").lower()
    return "you are the narrator" in low or "never the player" in low or "the world —" in low


def narrator_role(card: str, card_role: str = "") -> bool:
    """Typed card metadata is authoritative; prose guessing exists only for old cards."""
    role = str(card_role or "").strip().lower()
    if role == "narrator":
        return True
    if role == "character":
        return False
    if role:
        log.warning("genesis: invalid card_role=%r; using legacy narrator detection", role[:32])
    return _is_narrator_card(card)


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
    '{"op":"belief_acquire","holder":str,"statement":str,"stance":"knows|believes|'
    'doubts|disputes|uncertain|rumor","evidence_source":"witnessed|told|overheard|inferred",'
    '"teller":str?}  // only this actor\'s knowledge or belief; never objective truth\n'
    '{"op":"fact_admit","statement":str,"basis_source":"card|opening","basis_text":str,'
    '"holder":str?}  // objective fact only when basis_text is an exact verbatim source span; '
    'holder is the safe belief fallback if that basis cannot be verified\n'
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
    "Seed ONLY what the card and opening support. Never invent a named character, place, or "
    "faction absent from those inputs. Dialogue examples, if present, demonstrate style and are "
    "not world facts. For scene_set, location means the exact physical place where the opening "
    "is happening NOW. A place named only as a destination in phrases such as 'traveling to X', "
    "'heading toward X', or 'en route to X' is not the current scene; omit scene_set unless the "
    "opening separately places the cast at X now. Persistent traits come from the card; "
    "initial LEVELS (craving, mood) also read the opening. Use the character's plain "
    "name for char/entity fields. The NARRATOR / DM NAME is a non-diegetic storyteller role: "
    "NEVER create it as an entity, put it in the scene, or give it mood, goals, or relations. "
    "ALWAYS include an entity_add for every in-world character you "
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


def rules_ops(card: str, prompt: str, speaker: str = "", card_role: str = "") -> list[dict]:
    """Stage A. Name source (2026-07-04, 3-tier): (1) the stamp's
    speaker = {{char}} — reliable on EVERY real card; (2) 'Name:' regex fallback;
    (3) optional 'Characters:'/'Cast:' line seeds the listed cast deterministically
    (opt-in precision booster, never required). No name at all -> no seed."""
    name = (speaker or "").strip() or _char_name(card)
    if not name:
        return []
    dm_card = narrator_role(card, card_role)  # a narrator/world card's speaker is the WORLD —
                                             # never stage its UI name as a present person
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
               speaker: str = "", card_role: str = "") -> int:
    """Inline Stage A at the first request of a new session. Sub-ms; fail-open."""
    try:
        if store.genesis_state(session_id) != "":
            return 0
        card, prompt = card_and_prompt(doc)
        if not card.strip():
            store.genesis_mark(session_id, "skipped")
            return 0
        is_narrator = narrator_role(card, card_role)
        if is_narrator:
            store.narrator_speaker_set(session_id, speaker)
        ops = rules_ops(card, prompt, speaker=speaker,
                        card_role="narrator" if is_narrator else "character")
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


# 2026-07-10 (Bean): a DM card's opening commonly NAMES the player character in second person
# ("You are Kael, a down-on-your-luck sellsword…"). Genesis used to ignore it and seed a generic
# "Player", so the ledger player ("Player") disagreed with the fiction ("Kael") AND stage B often
# staged a separate "Kael" NPC — the DM then couldn't tell who the player was. Derive the PC name
# from that phrasing (high-precision; proper-noun only) so the Player Card matches the fiction.
_PC_NAME_RE = re.compile(
    r"\byou(?:\s+are|\s*'?re|\s+play(?:\s+as)?)\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*)?)"
    r"|\byour\s+name\s+is\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*)?)"
    r"|\bplaying(?:\s+as)?\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*)?)",
    re.IGNORECASE)
_PC_STOP = {"a", "an", "the", "you", "your", "now", "here", "there", "when", "as", "it", "this",
            "that", "if", "so", "and", "but", "not", "no", "about", "going", "ready", "free",
            "alone", "standing", "still", "just", "one", "my", "his", "her", "their", "our",
            "to", "in", "on", "at", "with", "who", "up", "down", "back", "out"}


def _player_name_from_text(*texts: str) -> str:
    """A player-character name declared in second person ("You are Kael" / "your name is Kael" /
    "playing as Kael"), or "". Conservative: the captured name must be a proper noun (capitalized
    in the source) and not a common word, so a phrase like "you are standing" never matches."""
    for text in texts:
        for m in _PC_NAME_RE.finditer(text or ""):
            cap = (next((g for g in m.groups() if g), "") or "").strip()
            if not cap:
                continue
            toks = cap.split()[:2]
            if toks[0].lower() in _PC_STOP or not toks[0][:1].isupper():
                continue
            if len(toks) == 2 and (toks[1].lower() in _PC_STOP or not toks[1][:1].isupper()):
                toks = toks[:1]                  # a trailing common word ("Corvin the") is not part
            return " ".join(toks)                # of the name (IGNORECASE let it slip the regex)
    return ""


def _player_seed_from_doc(doc: dict, cfg) -> tuple[str, dict]:
    """(player_name, seed_card). Tolerates a few carrier shapes for an explicit seed
    (doc 06 §S4: ST card extensions.aetherstate.player) without depending on any; falls back
    to a sensible default. The player is the USER's character, so the default name is the
    user persona (never the card/DM speaker); if neither is set, a PC name the opening declares
    in second person ("You are Kael") is used before the generic "Player" placeholder."""
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
        "skills": merge_baseline_skills(seed["skills"] if isinstance(seed.get("skills"), dict)
                                        else {}),
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
        if name == "Player":                    # no persona name — take one the opening declares
            card_txt, prompt_txt = card_and_prompt(doc)   # "You are Kael" -> player IS Kael
            derived = _player_name_from_text(prompt_txt, card_txt)
            if derived:
                name = derived
        card["_genesis_default"] = True          # still replaceable by a Creator-authored card
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
                   card: str, prompt: str, speaker: str = "", card_role: str = "") -> int:
    """Stage B: assist-LLM full-matrix pass, post-stream cold path. Fail-open.

    2026-07-06 live repro of 'genesis works half the time': at chat-open nothing has
    been proxied yet, so the endpoint arrived with model='' (upstream 400) — and the
    25 s shared timeout killed the calls that DID have a model. Both fixed here: the
    endpoint is resolved to a real model first (assist pick > upstream.model > GET
    /models), the call gets a long timeout + a budget that fits ~40 ops, and a HARD
    failure (no reply at all) re-marks 'rules' instead of 'done' so the next trigger
    (first message, chat re-open, /aether-genesis) retries instead of being locked out."""
    from .assist import _chat, resolve_endpoint      # shared client plumbing
    claim_epoch = None
    try:
        claim_epoch = store.genesis_claim_llm(session_id)
        if claim_epoch is None:
            return 0
        if not store.genesis_claim_is_current(session_id, claim_epoch, branch_id):
            store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        if not card.strip():
            if not store.genesis_mark_if_claim(
                session_id, claim_epoch, "done", branch_id,
            ):
                store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        ep = await resolve_endpoint(get_client, cfg, ep)
        if not ep.model:
            log.warning("genesis stage B: no model resolvable at %s — leaving session "
                        "re-seedable", ep.base_url or "(no endpoint)")
            store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        if not store.genesis_claim_is_current(session_id, claim_epoch, branch_id):
            store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        pname = ""
        try:                                     # the PLAYER's name (stage A already seeded it),
            _ps = current_state(store, branch_id)    # so stage B never twins the PC as an NPC
            for _eid, _rec in (_ps.get("player") or {}).items():
                _ent = (_ps.get("entities") or {}).get(_eid) or {}
                pname = str(_ent.get("name") or (_rec or {}).get("name") or _eid).strip()
                break
        except Exception:
            pname = ""
        user = (f"CHARACTER CARD:\n{card}\n\n"
                f"NARRATOR / DM NAME: {speaker or '(infer from card)'}\n\n"
                f"PLAYER CHARACTER (the user — NEVER create as an NPC or place in the scene): "
                f"{pname or '(unknown)'}\n\n"
                f"OPENING PROMPT:\n{prompt or '(none)'}\n\nJSON array of seed ops:")
        raw = await _chat(get_client, cfg, ep, _LLM_SYSTEM, user, max_tokens=3200,
                          timeout_s=120.0)
        if raw is None:                      # hard failure (timeout/auth/transport):
            log.warning("genesis stage B: no reply from %s (%s) — leaving session "
                        "re-seedable", ep.base_url, ep.model)
            store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        if not store.genesis_claim_is_current(session_id, claim_epoch, branch_id):
            store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
            return 0
        is_narrator = narrator_role(card, card_role)
        ops = _presence_with_basis(_parse_ops(
            raw,
            speaker=speaker,
            player_name=pname,
            narrator_speaker=is_narrator,
            card=card,
            prompt=prompt,
        ),
                                   card, prompt, speaker)
        ops = _scene_with_current_location_basis(ops, card, prompt)
        log.info("genesis stage B: raw=%d chars, %d valid op(s) parsed via %s",
                 len(raw or ""), len(ops), ep.model)
        if not ops and raw:
            log.info("genesis stage B raw head: %r", raw[:300])
        authored_claims = [
            claim_ops_from_text(
                authored_text,
                ingress="creator",
                source_id=authored_source,
            )
            for authored_text, authored_source in (
                (card, "creator_card"), (prompt, "creator_opening"),
            )
        ]

        def commit_stage_b() -> int | None:
            # The post-provider ownership check and every resulting state write share one fence.
            # A structured/forced genesis can win before this transaction, or wait until it ends;
            # an older worker can never publish after its claim has been superseded.
            with store.transaction():
                if not store.genesis_claim_is_current(session_id, claim_epoch, branch_id):
                    store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
                    return None
                if is_narrator:
                    store.narrator_speaker_set(session_id, speaker)
                applied = 0
                if ops:
                    head = store.db.execute(
                        "SELECT head_turn FROM branches WHERE branch_id=?", (branch_id,),
                    ).fetchone()
                    turn = max(0, (head["head_turn"] if head else 0))
                    res = apply_delta(
                        store, session_id, branch_id, turn, ops, "genesis", cfg,
                    )
                    applied = len(res.applied)
                    if res.quarantined:
                        log.info(
                            "genesis: %d applied, %d quarantined: %s",
                            applied,
                            len(res.quarantined),
                            "; ".join(
                                str(q.get("reason"))[:60] for q in res.quarantined[:5]
                            ),
                        )
                head = store.db.execute(
                    "SELECT head_turn FROM branches WHERE branch_id=?", (branch_id,),
                ).fetchone()
                claim_turn = max(0, (head["head_turn"] if head else 0))
                for claim_ops in authored_claims:
                    if not claim_ops:
                        continue
                    claim_result = apply_delta(
                        store, session_id, branch_id, claim_turn, claim_ops, "rule", cfg,
                    )
                    applied += sum(
                        1 for op in claim_result.applied if op.get("op") == "claim_record"
                    )
                if not store.genesis_mark_if_claim(
                    session_id, claim_epoch, "done", branch_id,
                ):
                    raise RuntimeError("Stage-B genesis claim changed during its commit fence")
                return applied

        n = commit_stage_b()
        if n is None:
            return 0
        log.info("genesis stage B seeded %d op(s) for %s", n, session_id[:8])
        return n
    except Exception as exc:
        log.warning("genesis LLM pass failed open: %s", type(exc).__name__)
        try:
            if claim_epoch is not None:
                # Never retry-loop a broken pass, but never overwrite a newer structured/forced
                # decision that already revoked this exact worker generation.
                if not store.genesis_mark_if_claim(
                    session_id, claim_epoch, "done", branch_id,
                ):
                    store.genesis_mark_if_claim(session_id, claim_epoch, "rules")
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


_TRAVEL_DESTINATION_PREFIX_RE = re.compile(
    r"(?:\b(?:travel(?:s|ed|ing)?|travell(?:ed|ing)|head(?:s|ed|ing)?)\b"
    r"(?:\s+[\w'’-]+){0,4}\s+(?:to|toward|towards|for)"
    r"|\b(?:bound|en\s+route)\s+(?:to|toward|towards|for))\s+(?:the\s+)?$",
    re.IGNORECASE,
)


def _location_mentions(text: str, location: str) -> list[tuple[int, int]]:
    """Exact-ish location mentions, tolerant only of punctuation/spacing differences."""
    tokens = re.findall(r"[^\W_]+", str(location or ""), flags=re.UNICODE)
    if not tokens:
        return []
    pattern = re.compile(
        r"(?<!\w)" + r"[\W_]+".join(re.escape(token) for token in tokens) + r"(?!\w)",
        re.IGNORECASE,
    )
    return [(match.start(), match.end()) for match in pattern.finditer(text or "")]


def _scene_with_current_location_basis(ops: list[dict], card: str,
                                       prompt: str) -> list[dict]:
    """Reject a Stage-B scene location supported only as a travel destination.

    The full card may contain world lore about a destination, so current-scene validation uses the
    greeting after ``[Opening message]`` plus the first Player prompt when that marker exists.
    We never infer a replacement location here: dropping an ungrounded scene is safer than baking a
    second guess into turn zero, and the first settled scene can then establish itself normally.
    """
    marker = "[Opening message]"
    opening = card.split(marker, 1)[1] if marker in card else card
    basis = f"{opening}\n{prompt}"
    keep: list[dict] = []
    for op in ops:
        if op.get("op") != "scene_set":
            keep.append(op)
            continue
        mentions = _location_mentions(basis, str(op.get("location") or ""))
        destination_only = bool(mentions) and all(
            _TRAVEL_DESTINATION_PREFIX_RE.search(basis[max(0, start - 160):start])
            for start, _end in mentions
        )
        if not destination_only:
            keep.append(op)
    return keep


def _normalize_genesis_knowledge(op: dict, card: str, prompt: str) -> dict | None:
    """Keep epistemics actor-local and admit facts only from an exact source span."""
    kind = op.get("op")
    if kind == "belief_acquire":
        holder = str(op.get("holder") or "").strip()
        statement = str(op.get("statement") or "").strip()
        stance = str(op.get("stance") or "believes").strip()
        evidence_source = str(op.get("evidence_source") or "inferred").strip()
        if not holder or not statement \
                or stance not in {"knows", "believes", "doubts", "disputes", "uncertain", "rumor"} \
                or evidence_source not in {"witnessed", "told", "overheard", "inferred"}:
            return None
        row = {
            "op": "belief_acquire", "holder": holder, "statement": statement,
            "stance": stance, "evidence_source": evidence_source,
        }
        teller = str(op.get("teller") or "").strip()
        if teller:
            row["teller"] = teller
        return row
    if kind != "fact_admit":
        return op
    statement = str(op.get("statement") or "").strip()
    basis_source = str(op.get("basis_source") or "").strip().lower()
    basis_text = str(op.get("basis_text") or "")
    source_text = card if basis_source == "card" else prompt if basis_source == "opening" else ""
    if statement and basis_text and basis_text in source_text:
        digest = hashlib.sha256(
            f"{basis_source}\0{basis_text}".encode("utf-8")
        ).hexdigest()
        return {
            "op": "fact_admit", "statement": statement,
            "cause": f"genesis:{basis_source}:sha256:{digest}",
            "authority": "genesis", "basis_source": basis_source,
            "basis_text": basis_text,
        }
    # A failed objective-fact proposal may retain only an explicitly named actor's belief.
    holder = str(op.get("holder") or "").strip()
    if holder and statement:
        return {
            "op": "belief_acquire", "holder": holder, "statement": statement,
            "stance": "believes", "evidence_source": "inferred",
        }
    return None


def _parse_ops(raw, speaker: str = "", player_name: str = "",
               narrator_speaker: bool = False, card: str = "",
               prompt: str = "") -> list[dict]:
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
        v = _normalize_genesis_knowledge(_coerce(op), card, prompt)
        if v is None:
            continue
        # Model JSON may propose scene detail and exact card-grounded knowledge, but it is
        # never the privileged genesis authority that admits immutable world events.  Any
        # genesis event must be constructed by deterministic code outside this parser.
        if v.get("op") == "world_event_admit":
            continue
        if v.get("op") not in {"belief_acquire", "fact_admit"}:
            v = validate_op(v)
            if v is None:
                continue
        # Stage B never mints players: the Player Card has its own dedicated track
        # (seed_player / the Creator) and is the USER's character — an assist model reading
        # a card that talks about "the Player" would happily invent one (2026-07-06 live
        # repro: a generic 'Player' character seeded next to the real one).
        if v["op"] == "player_seed" or (v["op"] == "entity_add"
                                        and str(v.get("kind", "")) == "player"):
            continue
        # never TWIN the player as an NPC (2026-07-10): the opening's "You are Kael" IS the player,
        # not a cast member — drop an entity_add for the player's own name, and don't let the
        # world-seed stage the player into the scene (the player track owns the PC's presence).
        pn = player_name.strip().lower()
        if pn and str(v.get("name", "")).strip().lower() == pn and v["op"] == "entity_add":
            continue
        if pn and v["op"] == "presence" and str(v.get("entity", "")).strip().lower() == pn:
            continue
        # A narrator card's stamp speaker (commonly "Dungeon Master") is a frontend role, not an
        # in-world NPC. Stage B used to re-invent it after Stage A had correctly omitted it.
        narrator = speaker.strip().lower() if narrator_speaker else ""
        if narrator:
            ref_keys = ("name", "char", "entity", "from_char", "to_char", "subject",
                        "partner", "learner", "holder", "teller")
            if any(str(v.get(key, "")).strip().lower() == narrator for key in ref_keys):
                continue
            if isinstance(v.get("participants"), list):
                v["participants"] = [p for p in v["participants"]
                                     if str(p).strip().lower() != narrator]
        out.append(v)
    # every referenced character must exist or its ops quarantine on alias resolution:
    # auto-prepend entity_add for names the model referenced but forgot to create.
    named = {str(o.get("name", "")).strip().lower() for o in out if o["op"] == "entity_add"}
    if speaker and not narrator_speaker:
        named.add(speaker.strip().lower())   # Stage A already created the speaker
    if player_name:
        named.add(player_name.strip().lower())   # refs to the player resolve to the player entity
                                                 # (never auto-mint a same-name NPC twin)
    refs: list[str] = []
    for o in out:
        for k in ("char", "entity", "from_char", "to_char", "subject", "partner",
                  "learner", "holder", "teller"):
            v = str(o.get(k, "") or "").strip()
            if v and v.lower() not in named and v.lower() not in [r.lower() for r in refs]:
                refs.append(v)
        for p in (o.get("participants") or []):
            p = str(p).strip()
            if p and p.lower() not in named and p.lower() not in [r.lower() for r in refs]:
                refs.append(p)
    return [{"op": "entity_add", "name": r} for r in refs] + out
