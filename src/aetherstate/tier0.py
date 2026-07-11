"""Tier-0 deterministic rules pass — 03 SS6 R0-R7. Every turn, no LLM, sub-millisecond class.

Execution scope: frontends RESEND the whole history each request, so commands/safewords are
EXECUTED only from the newest content of a non-duplicate new_turn/new_session (each message
acts exactly once), while OOC spans are STRIPPED from every forwarded message every time
(the model never sees engine syntax; ST keeps them in its own chat log).

R0 safeword scan     user's own message in EVERY mode incl. raw (direct user action — Q13);
                     assistant prose only when safeword_scan=both AND mode != unrestricted (Q14/08 B5)
R1 OOC commands      ((aether.freeze|resume|set|scene|status)) -> user-source ops (authority-checked)
R2 scene counters    clock_tick minutes_per_turn on narrative turns
R3 time keywords     conservative list -> time_advance (which also ramps cravings, R4)
R4 craving ramp      lives in the reducer on time_advance (state.py — replay-safe)
R5 presence          arrival/departure verb + known alias -> presence op, LOW confidence
R6 repetition        3-gram Jaccard over last N=6 assistant turns -> scene.stagnation
R7 dice              ((roll d20+3)) -> real RNG, recorded + injected next turn via header
R8 skill-check       ((aether.check stealth [+N] [vs DC] [scope minor..mythic])) -> registered
                     skill -> ELIGIBILITY GATE (a basis-gated skill without its granting
                     ability is a NON-MOVE: visible notice, no roll) -> real-RNG multi-die
                     roll (scope over mastery scales the penalty + caps the tier ceiling)
                     -> PbtA tier -> `check` op + this-turn [DIRECTIVE]
                     (RPG only; inert otherwise; nothing freestyle — unknown skill rejected)
R9 effect tags       [status gained | <char> | <Name> | <valence>] etc. in the DM's LAST reply
                     -> effect_add/remove/update PROPOSALS (extraction-sourced; the ledger,
                     not the prose, is what's true — RPG only, acts once per settled reply)
R10 world tags       [scene | ...] / [item gained|lost | ...] / [quest | ...] /
                     [affinity | ...] / [hp | ...] -> scene/item/quest/affinity/hp PROPOSALS
                     (RPG-5: the recording floor for the whole ledger — same spine as R9)
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from . import registry
from .state import _COMBAT_PHASES, EFFECT_VALENCES, MASTERY_TICKS, slug, translate_path

OOC_RE = re.compile(r"\(\(\s*(aether\.[a-z_]+[^)]*?|roll\s+[^)]*?)\s*\)\)", re.IGNORECASE)
_DICE_RE = re.compile(r"^(\d*)d(\d+)\s*([+-]\s*\d+)?$", re.IGNORECASE)

# R3: conservative keyword list (03 R3) — matched on the user's new message only
_TIME_KEYWORDS: list[tuple[str, dict]] = [
    (r"\bthe next (morning|day)\b|\bnext morning\b", {"to_time_of_day": "morning"}),
    (r"\bthat evening\b", {"to_time_of_day": "evening"}),
    (r"\bthat night\b|\bnightfall\b", {"to_time_of_day": "night"}),
    (r"\bhours later\b|\ba few hours later\b", {"minutes": 180}),
    (r"\blater that day\b", {"minutes": 120}),
]
_ARRIVE = r"(enters|arrives|walks in|comes in|returns|steps in)"
_DEPART = r"(leaves|departs|exits|walks out|storms out|slips out|is gone)"


@dataclass
class Tier0Result:
    user_ops: list[dict] = field(default_factory=list)
    rule_ops: list[dict] = field(default_factory=list)
    doc: Optional[dict] = None          # set only when OOC spans were stripped (re-forward this)
    notices: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)   # R8: parsed check declarations (resolved in run)
    proposal_ops: list[dict] = field(default_factory=list)   # R9: model-authored effect proposals
    #                                     (caller applies with source="extraction" — clamped,
    #                                     quarantined visibly, never privileged)
    off_protocol: list[str] = field(default_factory=list)    # 2026-07-10: invented bracket-tag
    #                                     heads in the DM's last reply ("[TAGS]", "[AWAIT]") —
    #                                     compose turns them into a one-line corrective
    kill_note: str = ""                 # 2026-07-10: an out-of-combat kill outcome (stealth kill,
    #                                     grand working, or a routed NON-MOVE) for the [DIRECTIVE]


def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _strip_ooc(doc: dict) -> Optional[dict]:
    """Remove ((aether...)) / ((roll ...)) spans from every user message (03 R1)."""
    msgs = doc.get("messages")
    if not isinstance(msgs, list):
        return None
    changed = False
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and OOC_RE.search(c):
                m = {**m, "content": OOC_RE.sub("", c).strip()}
                changed = True
            elif isinstance(c, list):
                parts = []
                for p in c:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) \
                            and OOC_RE.search(p["text"]):
                        p = {**p, "text": OOC_RE.sub("", p["text"]).strip()}
                        changed = True
                    parts.append(p)
                m = {**m, "content": parts}
        out.append(m)
    if not changed:
        return None
    return {**doc, "messages": out}


def _roll(spec: str, rng: random.Random) -> Optional[int]:
    m = _DICE_RE.match(spec.strip())
    if not m:
        return None
    n = int(m.group(1) or 1)
    sides = int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if not (1 <= n <= 100 and 2 <= sides <= 1000):
        return None
    return sum(rng.randint(1, sides) for _ in range(n)) + mod


_CHECK_MOD_RE = re.compile(r"^[+-]\d+$")


def _parse_check(rest: str, res: Tier0Result) -> None:
    """R8 declaration parse (no state/registry here): 'skill [+N|-N] [vs|dc N]'. Skill
    resolution + rolling happen in run() where state/cfg/registry/rng are in hand."""
    toks = rest.split()
    if not toks:
        res.notices.append("check needs a skill: ((aether.check <skill> [+N] [vs DC] "
                           "[scope ...] [use <ability>]))")
        return
    skill, mod, dc, scope, use, tgt, i = toks[0], 0, None, None, [], [], 1
    while i < len(toks):
        tk = toks[i].lower()
        if tk in ("vs", "dc", "target") and i + 1 < len(toks):
            try:
                dc = int(toks[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        if tk == "scope" and i + 1 < len(toks):        # RPG-3: scope-gated power (doc 10)
            scope = toks[i + 1].lower()
            i += 2
            continue
        if tk in ("use", "using", "with") and i + 1 < len(toks):   # 2026-07-07: invoke an
            use.append(toks[i + 1].strip("\"'"))                    # ACTIVE ability for this roll
            i += 2
            continue
        if tk in ("at", "on") and i + 1 < len(toks):   # Phase 1: name a combat TARGET —
            j = i + 1                                   # words until the next keyword bind
            words = []                                  # the strike to a combatant row
            while j < len(toks) and toks[j].lower() not in ("vs", "dc", "target", "scope",
                                                            "use", "using", "with", "at",
                                                            "on") \
                    and not _CHECK_MOD_RE.match(toks[j]):
                words.append(toks[j].strip("\"'"))
                j += 1
            if words:
                tgt.append(" ".join(words))
                i = j
                continue
        if _CHECK_MOD_RE.match(toks[i]):
            mod += int(toks[i])
        i += 1
    res.checks.append({"skill": skill, "mod": mod, "dc": dc, "scope": scope,
                       "use": use, "target": (tgt[0] if tgt else None), "raw": rest})


def _norm_phrase(text) -> str:
    """Lowercase, keep word chars, collapse to single spaces — so 'Fire-Slash'/'fire slash'/
    'FIRE_SLASH' all normalize to 'fire slash' for loose name matching."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


# ---- pure-code semantic REFLEX floor (2026-07-10, Bean) — intent by MEANING, no network -------
# The keyword floor matched skill names / `governs` verbs LITERALLY, so a conjugation ("sneaked",
# "convincing") or an everyday synonym ("sweet-talk", "haggle") the curated list didn't spell out
# fired no check at all. These add two grounded, deterministic, replay-pure layers that need NO
# model and NO network (invariant 2 safe): (a) a light stemmer so morphological variants collapse
# to one form on BOTH sides of the match; (b) a curated intent lexicon tying natural phrasings to
# the `governs` seed they mean — expanded ONLY for skills the player OWNS (the eligibility gate
# holds). Gated by [specialization].intent_floor (default on); off = the exact-match floor.
_STEM_SUF = ("ing", "edly", "ed", "es", "s")


def _stem_token(w: str) -> str:
    """Aggressive, CONSISTENT stem (not a real lemma — both sides stem the same way so they meet
    in the middle): strip a common verb/plural suffix, undo CVC doubling, drop a final 'e'.
    'moving'/'move' -> 'mov'; 'sneaked'/'sneak' -> 'sneak'; 'castle' -> 'castl' (never 'cast')."""
    w = "".join(re.findall(r"[a-z0-9]+", w.lower()))
    if len(w) <= 3:
        return w
    for suf in _STEM_SUF:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    if len(w) >= 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _stem_seq(text: str) -> list:
    return [_stem_token(t) for t in _norm_phrase(text).split()]


def _seq_contains(hay: list, needle: list) -> bool:
    """needle is a contiguous sublist of hay (both already stemmed)."""
    n = len(needle)
    if not n or n > len(hay):
        return False
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _phrase_hit(cand: str, msg_norm: str, msg_stems: list, stem_aware: bool) -> bool:
    """Does the candidate phrase occur in the message? Exact padded-substring by default; also a
    stem-aware contiguous-token match when the reflex floor is on (so 'sneaked'/'convincing'/a
    lexicon synonym all land). Pure string work — no model, no network."""
    if f" {cand} " in msg_norm:                      # exact always counts (fast path)
        return True
    if not stem_aware:
        return False
    return _seq_contains(msg_stems, [_stem_token(t) for t in cand.split()])


# curated intent lexicon: a `governs` seed (already tied to a skill) -> the natural phrasings it
# means. Expanded ONLY for a skill the player OWNS, so nothing becomes rollable without a basis.
_INTENT_SYN: dict = {
    "sneak": ("tiptoe", "skulk", "prowl", "slink", "steal past", "creep past", "melt into",
              "blend into", "pad silently"),
    "hide": ("conceal", "duck behind", "take cover", "hunker down", "lie low"),
    "persuade": ("sweet talk", "talk into", "win over", "bring around", "reason with", "coax",
                 "cajole", "wheedle", "talk round"),
    "convince": ("win over", "talk into", "bring around"),
    "charm": ("flirt", "woo", "flatter", "butter up", "sweet talk"),
    "barter": ("haggle", "bargain", "dicker", "beat down the price", "talk down the price"),
    "plead": ("beg", "implore", "appeal to", "entreat"),
    "notice": ("scan", "look around", "case the room", "size up", "survey", "take stock"),
    "search": ("rummage", "rifle through", "comb through", "scour", "ransack", "root through"),
    "spot": ("catch sight of", "make out", "pick out"),
    "listen": ("eavesdrop", "overhear", "strain to hear"),
    "recall": ("call to mind", "dredge up"),
    "identify": ("make sense of", "figure out", "puzzle out"),
    "decipher": ("crack", "translate", "work out"),
    "climb": ("scale", "clamber", "scramble up", "shinny up"),
    "jump": ("leap", "vault", "bound across"),
    "swim": ("wade across", "paddle across", "strike out across"),
    "run": ("sprint", "dash", "race", "tear off"),
    "pick": ("jimmy", "crack the lock", "spring the lock", "work the lock"),
    "unlock": ("crack open", "spring open"),
    "cast": ("invoke", "intone", "chant the", "work a spell", "utter the words"),
    "channel": ("pull mana", "gather power", "focus my magic"),
    "conjure": ("summon", "call forth", "manifest"),
    "weave": ("spin a spell", "trace the sigil", "trace a sigil"),
}


def _parse_checks_only(text: str, res: Tier0Result) -> None:
    """Re-parse ONLY ((aether.check ...)) spans (used for swipe re-rolls) — never the other
    commands (freeze/scene/set), which must not re-fire when a reply is merely re-generated."""
    for m in OOC_RE.finditer(text):
        cmd = m.group(1).strip()
        if cmd.lower().startswith("aether.check"):
            _parse_check(cmd[len("aether.check"):].strip(), res)


def _detect_nl_checks(text: str, state: dict, cfg, res: Tier0Result) -> None:
    """Natural-language roll detection (RPG). When the player NAMES one of their own skills or
    abilities in prose ("I use fire-slash on the monsters"), roll the governing SKILL: an ability
    maps to the skill it `applies_to` and is INVOKED if active; a skill name rolls itself. Matching
    is case/hyphen/space-insensitive, whole-phrase, and restricted to what the player OWNS — an
    unknown or unowned name never fires (the eligibility gate holds: nothing rollable without an
    in-world basis). Explicit ((aether.check ...)) still wins; a duplicate skill just merges the
    invoked ability. Code detects + resolves; the narrator only narrates (vision pillar 3)."""
    reg = registry.load(cfg)
    _, player = _player_card(state)
    if not player:
        return
    msg = " " + _norm_phrase(text) + " "
    stem_aware = bool(getattr(getattr(cfg, "specialization", None), "intent_floor", True))
    msg_stems = _stem_seq(text) if stem_aware else []
    already = {str(c.get("skill", "")).lower() for c in res.checks}
    cands = []                                   # (normalized_name, kind, id, entry)
    owned = set(player.get("skills") or {})
    owned |= set(((player.get("defs") or {}).get("skills") or {}))   # a frozen custom skill is
    for sid in owned:                            # OWNED even at rank 0 (it is on the sheet)
        entry = reg.skill_entry(sid, player)
        names = {str(sid), str(entry.get("name", sid))}
        names |= {str(g) for g in (entry.get("governs") or [])}   # curated verbs -> sensitivity
        for nm in names:
            n = _norm_phrase(nm)
            if len(n) >= 3:
                cands.append((n, "skill", str(sid), entry))
                if stem_aware:                   # curated intent lexicon: this governs seed's
                    for syn in _INTENT_SYN.get(n, ()):     # natural synonyms, tied to THIS skill
                        s = _norm_phrase(syn)
                        if len(s) >= 3:
                            cands.append((s, "skill", str(sid), entry))
    for aid, a in (reg.known_abilities(player) or {}).items():
        for nm in {str(aid), str((a or {}).get("name", aid))}:
            n = _norm_phrase(nm)
            if len(n) >= 4:
                cands.append((n, "ability", str(aid), a or {}))
    cands.sort(key=lambda c: -len(c[0]))         # prefer the most specific phrase
    detected: dict = {}                          # skill_id -> {"use": [ability_id, ...]}
    for n, kind, rid, entry in cands:
        if not _phrase_hit(n, msg, msg_stems, stem_aware):
            continue
        if kind == "skill":
            sid = reg.resolve_skill(rid, player) or rid
            detected.setdefault(sid, {"use": []})
        else:                                     # ability -> its governing skill
            applies = entry.get("applies_to", "all")
            targets = applies if isinstance(applies, list) else [applies]
            gov = None
            for t in targets:
                if isinstance(t, str) and t not in ("all", "any", ""):
                    gov = reg.resolve_skill(t, player)
                    if gov:
                        break
            if gov is None:                       # an all-purpose ability names no skill by itself
                continue
            d = detected.setdefault(gov, {"use": []})
            if registry.ability_is_active(entry) and rid not in d["use"]:
                d["use"].append(rid)
    for sid, d in detected.items():
        if sid.lower() in already:                # explicit check already covers it -> merge use
            for c in res.checks:
                if str(c.get("skill", "")).lower() == sid.lower():
                    for u in d["use"]:
                        c.setdefault("use", [])
                        if u not in c["use"]:
                            c["use"].append(u)
            continue
        res.checks.append({"skill": sid, "mod": 0, "dc": None, "scope": None,
                           "use": d["use"], "raw": sid, "nl": True})


# 2026-07-10 (Eranmor dialect healer): a live GLM run showed the DM calling for rolls in
# INVENTED grammar — "[CHECK] Aeliriel melee attack | target: baser Hollow (1) |
# skill: Swordplay+2" + "[AWAIT]" — instead of ((aether.check swordplay)). The engine was
# deaf to it, the player answered the call anyway, and the round smeared. When the DM's
# reply carries no proper inline call, bracket-CHECK lines that NAME a skill are healed
# into the same R8b arming path (code still resolves; the dialect is translated, not obeyed).
_DM_BRACKET_CHECK_RE = re.compile(r"\[\s*check\b", re.IGNORECASE)
_BRACKET_SKILL_RE = re.compile(r"\bskill\s*:?\s*([A-Za-z][A-Za-z' _-]{0,40}?)\s*(?:[+-]\d+)?"
                               r"\s*(?:\||\]|$)", re.IGNORECASE)
_BRACKET_TARGET_RE = re.compile(r"\btarget\s*:?\s*([^|\]]+)", re.IGNORECASE)


def _healed_bracket_checks(reply: str) -> list[dict]:
    """A `[CHECK] ...` line in the DM's reply -> R8b-style check declarations. GLM writes the
    LABEL form `[CHECK] Aeliriel melee | target: X | skill: Swordplay+2` (the `]` closes right
    after CHECK; the fields ride bare on the same line) as well as the enclosed
    `[CHECK ... skill: X]` form — so scan the WHOLE line either way. The old bracket-body
    capture saw an EMPTY group on the label form (and a stray later `]`, e.g. `[AWAIT]`, even
    swallowed the match), healing nothing — that was the Eranmor 0-check bug."""
    out = []
    for line in (reply or "").splitlines():
        if not _DM_BRACKET_CHECK_RE.search(line):
            continue
        sk = _BRACKET_SKILL_RE.search(line)
        if not sk:
            continue
        sid = "_".join(_norm_phrase(sk.group(1)).split())
        if not sid:
            continue
        tgt = _BRACKET_TARGET_RE.search(line)
        target = None
        if tgt:
            target = re.sub(r"\s*\([^)]*\)\s*", " ", tgt.group(1)).strip() or None
        out.append({"skill": sid, "mod": 0, "dc": None, "scope": None, "use": [],
                    "target": target, "raw": line.strip()[:120], "dm_called": True,
                    "healed": True})
    return out[-2:]                                  # at most the two most recent calls


def _parse_dm_called_checks(reply: str, state: dict, cfg, res: Tier0Result) -> None:
    """R8b — the DM CALLED for a roll (dm-rules/4) and the player answered with plain prose:
    arm the DM's own ((aether.check ...)) from its LAST reply so the roll happens WITHOUT the
    player retyping syntax. Only fires when the player's message produced no explicit and no
    NL-detected check (theirs always wins); the parsed call rides the normal R8 resolve path,
    so code still decides and the [DIRECTIVE] marks it as DM-called. Multi-word skill phrases
    are slugged whole ("dive-rig operation" -> dive_rig_operation) and resolved against the
    player's own sheet — an unknown or unowned call stays a visible non-move, never a mint."""
    _, player = _player_card(state)
    if not player:
        return
    calls = []
    for m in OOC_RE.finditer(reply or ""):
        cmd = m.group(1).strip()
        if cmd.lower().startswith("aether.check"):
            calls.append(cmd[len("aether.check"):].strip())
    if not calls:                                    # dialect healer: proper syntax always wins
        for h in _healed_bracket_checks(reply or ""):
            sid = h["skill"]
            if any(str(c.get("skill", "")).lower() == sid for c in res.checks):
                continue
            res.checks.append(h)
        return
    for rest in calls[-2:]:                          # at most the two most recent calls
        toks = rest.split()
        phrase, mod, dc, scope, use, i = [], 0, None, None, [], 0
        while i < len(toks):
            tk = toks[i].lower()
            if tk in ("vs", "dc", "target") and i + 1 < len(toks):
                try:
                    dc = int(toks[i + 1])
                    i += 2
                    continue
                except ValueError:
                    pass
            if tk == "scope" and i + 1 < len(toks):
                scope = toks[i + 1].lower()
                i += 2
                continue
            if tk in ("use", "using", "with") and i + 1 < len(toks):
                use.append(toks[i + 1].strip("\"'"))
                i += 2
                continue
            if _CHECK_MOD_RE.match(toks[i]):
                mod += int(toks[i])
                i += 1
                continue
            phrase.append(toks[i])
            i += 1
        sid = "_".join(w for w in _norm_phrase(" ".join(phrase)).split())
        if not sid:
            continue
        if any(str(c.get("skill", "")).lower() == sid for c in res.checks):
            continue                                 # already rolled this skill this turn
        res.checks.append({"skill": sid, "mod": mod, "dc": dc, "scope": scope,
                           "use": use, "raw": rest, "dm_called": True})


def _player_card(state: dict) -> tuple[Optional[str], dict]:
    """The one Player Card per branch (RPG); (eid, record) or (None, {})."""
    for eid, rec in (state.get("player") or {}).items():
        if isinstance(rec, dict):
            return eid, rec
    return None, {}


_SCOPE_RANK = {"minor": 0, "standard": 1, "major": 2, "epic": 3, "mythic": 4}


def _tracked_pool(player: dict, rname: str) -> bool:
    """A resource the card actually TRACKS (a dict with a max) — the gate the cost logic uses."""
    pool = (player.get("hp") if rname == "hp"
            else (player.get("resources") or {}).get(rname)) if player else None
    return isinstance(pool, dict) and bool(pool.get("max"))


def _ability_affordable(player: dict, cost: dict) -> bool:
    """A TRACKED pool must cover the cost; an untracked pool waives it (weak-model floor)."""
    for rname, amt in (cost or {}).items():
        pool = (player.get("hp") if rname == "hp"
                else (player.get("resources") or {}).get(rname)) if player else None
        if isinstance(pool, dict) and pool.get("max") and int(pool.get("cur", 0)) < int(amt):
            return False
    return True


def _merge_cost(dst: dict, cost: dict) -> None:
    for k, v in (cost or {}).items():
        try:
            dst[k] = dst.get(k, 0) + int(v)
        except (TypeError, ValueError):
            continue


def _find_active_ability(reg, player: dict, ref: str):
    """Resolve `ref` (id | display name | slug) to a KNOWN ability (aid, def) or (None, None)."""
    r = str(ref or "").strip().lower()
    if not r:
        return None, None
    for aid, adef in reg.known_abilities(player).items():
        nm = str((adef or {}).get("name", aid))
        if r in (str(aid).lower(), nm.lower()) or slug(r) in (slug(nm), str(aid).lower()):
            return aid, adef
    return None, None


# ---- Phase 1 (plan doc 13): the player's strike — code-derived damage ------------------
_STRIKE_FACTOR = {"crit_success": 3, "success": 2, "partial": 1}   # x weapon magnitude
# 2026-07-10 (Thornhale live, ROOT CAUSE of the recurring "phantom combat" bug): the old
# `\b(stab|cast|bash|…)\w*\b` matched ORDINARY WORDS — "stab"->"STABLE", "cast"->"CASTLE"/"casting",
# "bash"->"bashful", "cut"->"cutlery" — so walking to a stable fired the combat FLOOR, a phantom
# foe was staged, and its [WAR]/[OPPOSITION]/[DIRECTIVE] wrecked a peaceful scene (the LLM then
# spiralled trying to reconcile it). We now match only REAL conjugations of each verb, built as an
# EXACT-WORD alternation, so no common word can masquerade as an attack. Same fix pattern applies
# to any code that keys combat/kills off a verb regex.
def _conjugate(v: str) -> set:
    f = {v, v + "s", v + "ing", v + "ed"}
    if v.endswith("e"):                                   # -e drop: cleave -> cleaved/cleaving
        f |= {v + "d", v[:-1] + "ing", v[:-1] + "ed"}
    else:
        f |= {v + "es"}
        if len(v) >= 3 and v[-1] in "bdgklmnprtvz" and v[-2] in "aeiou" and v[-3] not in "aeiou":
            f |= {v + v[-1] + "ing", v + v[-1] + "ed"}    # CVC doubling: stab -> stabbed/stabbing
    return f


_ATTACK_BASES = ("attack", "strike", "hit", "shoot", "stab", "slash", "swing", "fire", "punch",
                 "kick", "blast", "charge", "shove", "cut", "cleave", "smash", "bash", "lunge",
                 "throw", "cast", "impale", "tackle", "grapple", "hack", "maim", "skewer",
                 "pierce", "gore", "batter", "thrust", "slam", "ram", "chop", "spear", "club")
_ATTACK_SET = set().union(*(_conjugate(v) for v in _ATTACK_BASES))
_ATTACK_VERBS = re.compile(r"\b(?:" + "|".join(sorted(_ATTACK_SET, key=len, reverse=True))
                           + r")\b", re.IGNORECASE)

# the foe is usually the object of a TARGETING preposition ("stab into <foe>", "cut at <foe>") —
# those objects are tried before a bare attack-verb object, which can lead with a direction or
# the Player's own weapon (2026-07-10 Redgate: "lunge FROM the pines and stab MY SHORTSWORD…").
_TARGET_PREP_RE = re.compile(r"\b(?:at|into|onto|upon|through|toward|towards)\s+", re.IGNORECASE)


def _war_room(state: dict, cfg) -> bool:
    """Combat instances live only under rpg + the war_room knob (default on)."""
    return bool(getattr(getattr(cfg, "specialization", None), "war_room", True)) \
        and bool((state.get("combat") or {}).get("active"))


def _weapon_magnitude(state: dict, eid: str) -> int:
    """The equipped weapon's `damage` mod (mainhand > offhand > any equipped piece), the
    curated damage scale for a player strike. Unarmed/unmodded floor: 1. Pure state, µs."""
    best = 0
    for it in (state.get("items") or {}).values():
        if not isinstance(it, dict) or it.get("owner") != eid \
                or not str(it.get("loc", "")).startswith("gear:"):
            continue
        dv = (it.get("mods_snapshot") or {}).get("damage")
        if isinstance(dv, int):
            slot = str(it.get("loc", ""))[5:]
            rank = 3 if slot == "mainhand" else 2 if slot == "offhand" else 1
            best = max(best, dv * 10 + rank)
    return max(1, best // 10) if best else 1


def _bind_targets(res: Tier0Result, state: dict, text: str, entity_aware: bool = True) -> None:
    """Bind each declared check to a LIVE enemy combatant so its damage can land:
    an explicit `at <name>` wins; else the player's prose naming a foe binds it (longest
    name first); else (entity-aware) the OBJECT of the attack resolved to a live foe; else a
    lone surviving enemy + an attack verb is unambiguous. A check that binds to nothing stays
    a plain skill check — nothing is guessed."""
    from .state import live_combatants, resolve_combatant
    foes = live_combatants(state, "enemy")
    if not foes or not res.checks:
        return
    low = " " + _norm_phrase(text or "") + " "
    named = sorted(foes, key=lambda r: -len(str(r.get("name", ""))))
    for c in res.checks:
        if c.get("target"):
            cid = resolve_combatant(state, c["target"])
            c["target"] = cid                    # unresolved -> None (stays a plain check)
            continue
        hit = next((r["id"] for r in named
                    if f" {_norm_phrase(r.get('name', ''))} " in low), None)
        if hit is None and entity_aware:         # entity-aware: the OBJECT of the attack (prep
            for span in _attack_object_spans(text):    # object first, then verb object) resolved
                cid = resolve_combatant(state, " ".join(_norm_phrase(span).split()[:4]))
                if cid:
                    hit = cid                    # to a LIVE foe — precise, before the lone fallback
                    break
        if hit is None and len(foes) == 1 and _ATTACK_VERBS.search(text or ""):
            hit = foes[0]["id"]                  # one foe + an attack verb: unambiguous
        c["target"] = hit


# 2026-07-10 (Eranmor): the DM emitted "[TAGS] scene_active | ..." / "[AWAIT]" lines —
# invented grammar the engine silently ignored, and nothing ever corrected it. Bracket
# lines whose head is neither a real channel nor an engine-block echo are collected so the
# NEXT prompt carries a one-line protocol corrective (compose renders it; self-clearing).
_KNOWN_TAG_HEADS = {"status", "condition", "valence", "scene", "item", "quest", "affinity",
                    "hp", "foe", "clash", "time", "rumor", "check"}   # check heals (R8b)
_ECHO_HEADS = {"directive", "player", "rules", "effects", "gear", "inventory", "factions",
               "relations", "nearby", "world", "opposition", "war", "ally", "key",
               "notice", "protocol", "start"}
_BRACKET_HEAD_RE = re.compile(r"^\s*\[\s*([A-Za-z][A-Za-z _-]{0,24}?)\s*(?:\||\])",
                              re.MULTILINE)


def _scan_off_protocol(text: str) -> list[str]:
    """Bracket-line heads in the DM's reply that match NO known grammar (nudge list)."""
    seen: list[str] = []
    for m in _BRACKET_HEAD_RE.finditer(text or ""):
        head = m.group(1).strip()
        first = head.split()[0].lower() if head.split() else ""
        if not first or first in _ECHO_HEADS:
            continue
        if first in _KNOWN_TAG_HEADS and first != "check":
            continue                          # a real channel (well-formed or not) — no nudge
        if head.upper() not in seen:
            seen.append(head.upper())
    return seen[:4]


# 2026-07-10 (Eranmor floor, pillar 6): the DM narrated a horde for three straight replies
# and never emitted [foe] — combat.active stayed false and the whole War Room was
# structurally unreachable. When the Player ATTACKS a target whose name the DM's OWN last
# reply narrated (the fiction is the in-world basis, exactly the parse_foe_tags argument),
# the engine stages that target itself. Conservative by design: attack verb required, every
# name token must appear in the DM's prose, body parts / stopwords never become foes.
_FLOOR_STOP = {"the", "and", "that", "this", "with", "from", "into", "onto", "them", "him",
               "her", "its", "his", "your", "their", "one", "two", "few", "all", "any",
               "closest", "nearest", "first", "last", "next", "other", "another",
               # directions / adverbs are never a foe (Thornhale: "slip OUT" staged foe 'Out')
               "out", "off", "up", "down", "back", "away", "aside", "here", "there", "around",
               "inside", "outside", "past", "forward", "onward", "toward", "towards", "over",
               "under", "behind", "ahead", "left", "right", "then", "again", "still", "just"}
_BODY_PARTS = {"head", "face", "neck", "throat", "chest", "arm", "arms", "leg", "legs",
               "hand", "hands", "eye", "eyes", "back", "side", "body", "torso", "shoulder",
               "shoulders", "knee", "knees", "foot", "feet", "skull", "heart", "gut",
               "belly", "waist", "hip", "hips", "wrist", "ankle", "jaw", "chin", "brow",
               "temple", "ribs", "spine", "flank", "wing", "tail", "maw", "mouth"}
# 2026-07-10 (Redgate live): "stab my SHORTSWORD into the nearest cutthroat" staged the weapon
# as a foe — the object of an attack verb often leads with the Player's own weapon/gear. Generic
# held-item words (plus the Player's actual owned gear tokens) are skipped BEFORE the target run.
_HELD_WORDS = {"sword", "shortsword", "longsword", "greatsword", "blade", "knife", "dagger",
               "axe", "handaxe", "mace", "spear", "lance", "staff", "wand", "gun", "pistol",
               "rifle", "bow", "crossbow", "hammer", "warhammer", "katana", "cleaver", "machete",
               "baton", "club", "sabre", "saber", "revolver", "blaster", "rapier", "scimitar",
               "glaive", "halberd", "polearm", "scythe", "sickle", "whip", "flail", "falchion",
               "fist", "fists", "weapon", "blades", "knives", "shield", "buckler"}


def _attack_object_spans(user_text: str) -> list:
    """The text after a targeting preposition (precise) then after any attack verb — the span
    most likely to NAME who is being hit. Used by the entity-aware target picker."""
    spans = []
    for mp in _TARGET_PREP_RE.finditer(user_text or ""):
        spans.append(user_text[mp.end():])
    for m in _ATTACK_VERBS.finditer(user_text or ""):
        spans.append(user_text[m.end():])
    return spans


def _present_cast(state: dict, peid) -> list:
    """Present, non-player character/npc entities: (eid, name, name-token set). The grounded
    cast a strike can land on — a target should be a REAL on-scene person, never a token run off
    the prose (Thornhale: 'Out'/'Pines'/'Shortsword' were a direction, a place, a weapon)."""
    out = []
    for eid, e in (state.get("entities") or {}).items():
        if not isinstance(e, dict) or eid == peid:
            continue
        if e.get("kind") not in ("character", "npc") or not e.get("present"):
            continue
        toks = {w for w in _norm_phrase(e.get("name", "")).split() if len(w) >= 3}
        if toks:
            out.append((eid, str(e.get("name", "")), toks))
    return out


def _entity_target(state: dict, user_text: str, peid) -> Optional[tuple]:
    """The PRESENT cast member the Player is attacking, or None. Matches the object-of-attack
    tokens (prep object first, then verb object) against present cast names; a whole-text name
    match is the last resort. Pure state read — resolves to a KNOWN entity, never mints one."""
    cast = _present_cast(state, peid)
    if not cast:
        return None
    for span in _attack_object_spans(user_text):
        head = {t for t in _norm_phrase(span).split()[:4] if len(t) >= 3}
        if not head:
            continue
        best = None
        for eid, name, ntoks in cast:
            score = len(ntoks & head)
            if score and (best is None or score > best[0]):
                best = (score, eid, name)
        if best:
            return best[1], best[2]
    low = " " + _norm_phrase(user_text or "") + " "     # last resort: a present name in the text
    for eid, name, ntoks in sorted(cast, key=lambda c: -len(c[1])):
        if ntoks and all(f" {t} " in low for t in ntoks):
            return eid, name
    return None


def _floor_stage_foe(res: Tier0Result, state: dict, user_text: str,
                     dm_text: str, entity_aware: bool = True) -> Optional[dict]:
    """A grounded `combatant_spawn` for the target the Player is attacking, or None. Entity-aware
    FIRST (2026-07-10): a strike on a PRESENT cast member stages THAT person (grounded by their
    existence — no DM-prose echo needed); else the conservative DM-prose token-run heuristic."""
    if not _ATTACK_VERBS.search(user_text or ""):
        return None
    from .state import live_combatants, resolve_entity_ref
    if live_combatants(state, "enemy"):
        return None                                  # foes exist — binding handles it
    peid, _p = _player_card(state)
    if entity_aware:                                 # the strike lands on a REAL on-scene person
        pick = _entity_target(state, user_text, peid)
        if pick:
            eid, name = pick
            base = slug(name)[:32] or "foe"          # deterministic cid (mirrors the reducer's
            rows = (state.get("combat") or {}).get("combatants") or {}   # collision suffix) so
            cid, n = base, 2                          # the opening strike can bind it THIS batch
            while cid in rows:
                cid, n = f"{base}#{n}", n + 1
            return {"op": "combatant_spawn", "name": name, "side": "enemy", "tier": "standard",
                    "_floor": True, "char": eid, "_cid": cid}
    if not (dm_text or "").strip():
        return None
    pname_toks = set()
    held = set(_HELD_WORDS)                           # the Player's own weapon/gear never a foe
    if peid:
        ent = (state.get("entities") or {}).get(peid) or {}
        pname_toks = {w for w in _norm_phrase(ent.get("name", "")).split() if len(w) >= 3}
        for it in (state.get("items") or {}).values():   # the Player's actual owned item words
            if isinstance(it, dict) and it.get("owner") == peid:
                held |= {w for w in _norm_phrase(str(it.get("name", ""))).split() if len(w) >= 3}
    for e in (state.get("entities") or {}).values():  # a LOCATION is never a foe (Redgate live:
        if isinstance(e, dict) and e.get("kind") == "location":   # "lunge from the pines")
            held |= {w for w in _norm_phrase(str(e.get("name", ""))).split() if len(w) >= 3}
    dm_low = " " + _norm_phrase(dm_text) + " "
    cands = [str(c["target"]) for c in res.checks if c.get("target")]
    for mp in _TARGET_PREP_RE.finditer(user_text or ""):   # targeting-preposition objects FIRST
        cands.append(user_text[mp.end():])
    for m in _ATTACK_VERBS.finditer(user_text or ""):      # then every attack-verb object
        cands.append(user_text[m.end():])
    for cand in cands:
        run: list[str] = []
        for t in _norm_phrase(cand).split():
            if t in held and not run:
                continue                             # skip the Player's own weapon/gear lead-in
            grounded = (f" {t} " in dm_low or f" {t}s " in dm_low   # plural/singular tolerant:
                        or (t.endswith("s") and f" {t[:-1]} " in dm_low))   # "cutthroat(s)"
            if len(t) >= 3 and t not in _FLOOR_STOP and t not in _BODY_PARTS and t not in held \
                    and t not in pname_toks and grounded:
                run.append(t)
            elif run:
                break                                # keep the FIRST grounded run only
        if not run:
            continue
        name = " ".join(run[:3]).title()
        eid = resolve_entity_ref(state, name)
        if eid and eid == peid:
            continue
        op: dict = {"op": "combatant_spawn", "name": name, "side": "enemy",
                    "tier": "standard", "_floor": True}
        if eid and (state.get("entities", {}).get(eid) or {}).get("kind") \
                in ("character", "npc"):
            op["char"] = eid                         # a KNOWN NPC fights as themselves
        base = slug(name)[:32] or "foe"              # deterministic cid (mirrors the reducer's
        rows = (state.get("combat") or {}).get("combatants") or {}   # collision suffix) so the
        cid, n = base, 2                             # opening strike can bind it THIS batch
        while cid in rows:
            cid, n = f"{base}#{n}", n + 1
        op["_cid"] = cid
        return op
    return None


_GROUP_COUNTS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "couple": 2,
                 "pair": 2, "trio": 3, "several": 3, "few": 3, "many": 4, "handful": 4,
                 "band": 3, "pack": 3, "gang": 3, "group": 3, "squad": 3, "horde": 4,
                 "swarm": 4, "cluster": 3, "knot": 3, "both": 2}


def _floor_group_extras(state: dict, dm_text: str, primary: dict) -> list[dict]:
    """2026-07-10 (Bean, "3v3 is missing"): when the DM's prose names a GROUP ("three
    cutthroats", "a pack of ghouls") the floor stages the whole band, not just the one the
    Player struck — capped within the 3v3 enemy side. Only for an EXTRA foe (a named
    individual is not a crowd); grounded in the fiction's own count word next to the foe's
    noun; deterministic pure token math so the journaled spawns replay clean."""
    from .state import COMBAT_SIDE_CAP
    if not dm_text or primary.get("char"):            # a specific person isn't a group
        return []
    name = str(primary.get("name") or "").strip()
    toks = _norm_phrase(name).split()
    if not toks:
        return []
    head = toks[-1]
    heads = {head, head + "s", head[:-1] if head.endswith("s") and len(head) > 3 else head}
    words = _norm_phrase(dm_text).split()
    count = 0
    for i, w in enumerate(words):                     # a count word within 3 tokens BEFORE the
        if w in heads:                                # foe's noun ("three ... cutthroats")
            for back in words[max(0, i - 3):i]:
                if back in _GROUP_COUNTS:
                    count = max(count, _GROUP_COUNTS[back])
    if count < 2:
        return []
    rows = (state.get("combat") or {}).get("combatants") or {}
    taken = set(rows) | {primary.get("_cid")}
    base = slug(name)[:32] or "foe"
    out: list[dict] = []
    for _ in range(min(count, COMBAT_SIDE_CAP) - 1):  # primary is already one of the band
        cid, n = base, 2
        while cid in taken:
            cid, n = f"{base}#{n}", n + 1
        taken.add(cid)
        out.append({"op": "combatant_spawn", "name": name, "side": "enemy",
                    "tier": primary.get("tier") or "standard", "_floor": True, "_cid": cid})
    return out


def _resolve_checks(res: Tier0Result, state: dict, cfg, rng: random.Random,
                    pending_foe: Optional[dict] = None) -> None:
    """R8 resolution: map each declared skill to a REGISTERED skill, pass the ELIGIBILITY
    GATE, roll real dice, compute the PbtA tier, and emit a `check` rule op (with the
    effective mod / dice / naturals / scope arithmetic baked for audit + replay).
    Deterministic arithmetic — hot-path-legal (invariant 2). Unknown skills are REJECTED
    with a visible notice (nothing freestyle — doc 05 §5.2).

    The gate (RPG-3, doc 10): a skill whose definition carries `requires_ability` has NO
    in-world basis until the character owns that ability — declaring it is a NON-MOVE
    (notice, no op, no roll), never a failed roll. Freedom is routed, not blocked: earn
    the ability in-world (ability_grant) and the same declaration becomes a real check.
    Scope (doc 10): `scope minor|standard|major|epic|mythic` scales the attempt against
    MASTERY (= skill rank): each scope step past the rank costs -2 AND lowers the tier
    ceiling one step (floor: partial). A thin skill may attempt something enormous — the
    roll is punishing and the ceiling low; deep mastery makes it plausible."""
    reg = registry.load(cfg)
    player_eid, player = _player_card(state)
    dice = registry.dice_spec(reg, cfg)
    tiers = registry.tiers_model(reg, cfg)
    now = state.get("meta", {}).get("turn", -1) + 1   # ops apply at the NEXT turn index
    ability_cd = (player or {}).get("ability_cd") or {}   # per-ability cooldown ledger (RPG)
    for c in res.checks:
        sid = reg.resolve_skill(c["skill"], player)   # snapshot-first: a freestyle/evolved skill resolves too
        if sid is None:
            res.notices.append(f"unknown skill '{c['skill']}': add it to the registry "
                               f"(nothing freestyle — doc 05 §5.2)")
            continue
        entry = reg.skill_entry(sid, player)
        need = str(entry.get("requires_ability") or "").strip()
        if need and not reg.has_ability(player, need):   # the eligibility gate: a NON-MOVE
            label = reg.skill_label(sid, player)
            aname = str((reg.merged_abilities(player).get(need) or {}).get("name", need))
            res.notices.append(f"no in-world basis: {label} requires {aname} — not a roll. "
                               f"You cannot declare power; acquire it in-world first (doc 10)")
            continue
        cost = registry.skill_cost(entry)       # RPG-5 (doc 10 §5.4): the resource gate —
        short = None                            # a pool the card TRACKS must cover the cost;
        for rname, amt in cost.items():         # an untracked pool waives it (fail-open floor)
            pool = (player.get("hp") if rname == "hp"
                    else (player.get("resources") or {}).get(rname)) if player else None
            if isinstance(pool, dict) and pool.get("max") and int(pool.get("cur", 0)) < amt:
                short = (rname, int(pool.get("cur", 0)), amt)
                break
        if short:                               # spent, not blocked: rest/recover re-opens it
            res.notices.append(f"not enough {short[0]} for {reg.skill_label(sid, player)} "
                               f"({short[1]}/{short[2]} needed) — recover first; not a roll")
            continue
        # ---- dice-shaping abilities (2026-07-07, Bean): a SKILL sets the modifier; an
        # ABILITY shapes the dice. Passive edge/ward auto-apply to matching checks; active
        # extra_die/reroll/surge fire only when the player invokes `use <ability>` (freedom is
        # routed — an unknown/unaffordable/cooling ability is a visible notice, the roll goes on).
        pd = registry.parse_dice(dice)
        if pd is None:
            res.notices.append(f"bad dice spec: {dice}")
            continue
        n_keep, sides, flat = pd
        edge_extra, ward, shaped = 0, 0, []
        for aid, adef in (reg.known_abilities(player).items() if player else ()):
            if not registry.ability_applies(adef, sid):
                continue
            if registry.ability_is_active(adef):    # an ACTIVE never auto-applies — it is
                continue                            # invoked (`use`), paid for, and cooled
            mech = registry.ability_mechanic(adef)
            if mech == "edge":                      # passive advantage: +dice, keep best
                edge_extra += max(1, registry.ability_magnitude(adef, 1))
                shaped.append(str(adef.get("name", aid)))
            elif mech == "ward":                    # passive guard: raise the failure floor
                ward = max(ward, max(1, registry.ability_magnitude(adef, 1)))
                shaped.append(str(adef.get("name", aid)))
        surge_mod, surge_lift, use_mod, onfail, pay_ability, cd_set = 0, 0, 0, None, {}, {}
        blocked: list[dict] = []                    # 2026-07-10 (Eranmor): a DECLARED active
        #                                             that didn't ride is baked onto the op so
        #                                             the [DIRECTIVE] tells the narrator "plain
        #                                             attempt, not the technique" (and the HUD
        #                                             shows why) — silence here cost a live run
        for ref in c.get("use", []):                # active abilities the player invoked
            aid, adef = _find_active_ability(reg, player, ref) if player else (None, None)
            if adef is None:
                res.notices.append(f"you don't know an activated ability '{ref}'")
                blocked.append({"name": str(ref)[:60], "why": "unknown ability"})
                continue
            mech, label = registry.ability_mechanic(adef), str(adef.get("name", aid))
            if mech == "basis":                     # a gate key has no dice effect to spend
                res.notices.append(f"{label} grants your in-world basis — it already applies")
                continue
            if not registry.ability_is_active(adef):    # authored kind is the truth (2026-07-09)
                res.notices.append(f"{label} is passive — it already applies, no need to use it")
                continue
            if not registry.ability_applies(adef, sid):
                res.notices.append(f"{label} does not apply to {reg.skill_label(sid, player)}")
                blocked.append({"name": label, "why": "does not apply to this skill"})
                continue
            if int(ability_cd.get(aid, 0)) > now:
                res.notices.append(f"{label} is recharging (ready on turn {int(ability_cd[aid])})")
                blocked.append({"name": label,
                                "why": f"still recharging (ready turn {int(ability_cd[aid])})"})
                continue
            if not _ability_affordable(player, registry.skill_cost(adef)):
                res.notices.append(f"not enough resources to use {label} — recover first")
                blocked.append({"name": label, "why": "not enough resources"})
                continue
            if mech == "surge":                     # on use: big bonus + lift the scope ceiling
                surge_mod += max(1, registry.ability_magnitude(adef, 2))
                surge_lift += 1
                _merge_cost(pay_ability, registry.skill_cost(adef))
                if int(adef.get("cooldown_turns", 0)) > 0:
                    cd_set[aid] = now + int(adef["cooldown_turns"])
                shaped.append(label)
            elif mech in registry.ON_FAIL_MECHANICS:    # extra_die / reroll: ONLY on a miss
                if onfail is None:
                    onfail = (aid, adef)
            else:                                   # restored 2026-07-09: the flat-burst active
                if mech == "edge":                  # (Combat-Stims pattern) + active-authored
                    edge_extra += max(1, registry.ability_magnitude(adef, 1))   # dice-shapers —
                elif mech == "ward":                # they spend on THIS check instead of
                    ward = max(ward, max(1, registry.ability_magnitude(adef, 1)))   # always-on
                else:                               # "mod": a +N burst on this one roll
                    use_mod += max(1, registry.ability_magnitude(adef, 1))
                _merge_cost(pay_ability, registry.skill_cost(adef))
                if int(adef.get("cooldown_turns", 0)) > 0:
                    cd_set[aid] = now + int(adef["cooldown_turns"])
                shaped.append(label)

        kept, pool = registry.roll_keep(n_keep, edge_extra, sides, rng)
        eff = (reg.effective_mod(player, sid) if player else 0) + int(c["mod"]) \
            + flat + surge_mod + use_mod
        if player_eid:                # RPG-2/3: equipped-gear + active-effect mods flow in, baked
            eff += registry.gear_skill_mod(state, player_eid, sid)
            eff += registry.effect_skill_mod(state, player_eid, sid, now)
        cap = None
        over = 0
        scope = c.get("scope")
        if scope is not None:                       # RPG-3: scope-gated power (doc 10)
            srank = _SCOPE_RANK.get(scope)
            if srank is None:
                res.notices.append(f"unknown scope '{scope}' (minor|standard|major|epic|"
                                   f"mythic) — treated as standard")
                srank = 1
            band = min(4, int((player or {}).get("skills", {}).get(sid, 0)))
            over = max(0, srank - band)
            if over:
                eff -= 2 * over                     # punishing odds...
                ceil = min(len(registry.CHECK_TIERS) - 1, max(2, 4 - over) + surge_lift)
                cap = registry.CHECK_TIERS[ceil]    # ...and a low ceiling (surge lifts one step)

        def _settle(nats):                          # tier from naturals, then ward floor + cap
            t, tot = registry.resolve_tier(nats, eff, sides, c["dc"], tiers)
            if ward >= 1 and t == "crit_fail":
                t = "fail"
            if ward >= 2 and t == "fail":
                t = "partial"
            if cap is not None and registry.CHECK_TIERS.index(t) \
                    > registry.CHECK_TIERS.index(cap):
                t = cap
            return t, tot

        tier, total = _settle(kept)
        fired, improved = None, False               # the on-fail power fires only when it helps
        if onfail is not None and over < 3 and tier in ("fail", "crit_fail"):
            base_tier = tier                        # remember what the miss WAS, to tell if it lifted
            oaid, oadef = onfail
            omech = registry.ability_mechanic(oadef)
            mag = max(1, registry.ability_magnitude(oadef, 1))
            if omech == "reroll":                   # roll a fresh pool, keep the BETTER outcome
                kept2, pool2 = registry.roll_keep(n_keep, edge_extra, sides, rng)
                t2, tot2 = _settle(kept2)
                if registry.CHECK_TIERS.index(t2) >= registry.CHECK_TIERS.index(tier):
                    kept, pool, tier, total = kept2, pool2, t2, tot2
            else:                                   # extra_die: literally ADD dice to the pool
                pool = pool + [rng.randint(1, sides) for _ in range(mag)]
                kept = sorted(pool, reverse=True)[:n_keep]
                tier, total = _settle(kept)
            fired = str(oadef.get("name", oaid))
            improved = registry.CHECK_TIERS.index(tier) > registry.CHECK_TIERS.index(base_tier)
            _merge_cost(pay_ability, registry.skill_cost(oadef))
            if int(oadef.get("cooldown_turns", 0)) > 0:
                cd_set[oaid] = now + int(oadef["cooldown_turns"])
            shaped.append(fired)
        if over >= 3:                               # RPG-5 (doc 10 §8): reaching THAT far past
            forced = "crit_fail" if over >= 4 else "fail"   # mastery fails outright — surge
            if registry.CHECK_TIERS.index(tier) > registry.CHECK_TIERS.index(forced):
                tier = forced                       # can't beat the wall; deep mastery can
            res.notices.append(f"scope '{scope}' is far beyond {sid} mastery — "
                               f"the attempt fails outright (doc 10 §8)")   # "Alter Reality" rule

        op = {"op": "check", "skill": sid, "result": total, "tier": tier,
              "_mod": eff, "_dice": dice, "_seed": kept}
        if c.get("dm_called"):
            op["_dm_called"] = True          # the DM asked for this roll (R8b) - shown on the
        #                                      [DIRECTIVE] so the narrator can own its own call
        if player_eid:
            op["char"] = player_eid          # a real entity (kind=player) -> resolves cleanly
        if c["dc"] is not None:
            op["dc"] = c["dc"]
        if scope is not None:
            op["scope"], op["_scope_over"] = scope, over
        if shaped or fired or edge_extra or ward or surge_mod or use_mod:   # audit (HUD/log)
            op["_shape"] = {"abilities": sorted(set(shaped)), "fired": fired,
                            "improved": improved, "pool": list(pool), "kept": list(kept),
                            "edge": edge_extra, "ward": ward, "surge": surge_mod,
                            "burst": use_mod}
        if blocked:
            op["_ability_blocked"] = blocked[:3]    # baked: directive + HUD tell the truth
        pay: dict = {}                              # SKILL cost (half on a miss) + ABILITY costs
        if cost and player_eid:                     # (full) — tracked pools only; untracked waives
            for r, a in cost.items():
                if _tracked_pool(player, r):
                    pay[r] = a if tier != "fail" else max(1, (a + 1) // 2)
        for r, a in pay_ability.items():
            if _tracked_pool(player, r):
                pay[r] = pay.get(r, 0) + int(a)
        if pay:
            op["_cost"] = pay
        if cd_set:
            op["_ability_cd"] = cd_set              # cooldowns set for fired/used actives
        strike = None                               # Phase 1: a check BOUND to a live enemy
        tgt = c.get("target")
        # fix C (2026-07-10): a foe the FLOOR is staging THIS batch is not in state yet, but its
        # combatant_spawn (prio 2) applies BEFORE combatant_hp (prio 6), so a strike against its
        # baked cid lands the same turn — the opening blow of a floor-started fight no longer whiffs.
        # (Binding itself requires an attack verb — see _bind_targets — so a non-attack utility
        # check like a Stealth 'slip' or Hexcraft 'scry' is never bound and never strikes.)
        floor_hit = bool(pending_foe and tgt and tgt == pending_foe.get("cid"))
        if tgt and (_war_room(state, cfg) or floor_hit):   # code-derived damage — outcome
            from .state import resolve_combatant       # tier x weapon magnitude (ratified)
            if floor_hit:
                cid, fname, is_enemy = pending_foe["cid"], pending_foe["name"], True
            else:
                cid = resolve_combatant(state, tgt)
                row = ((state.get("combat") or {}).get("combatants") or {}).get(cid)
                is_enemy = isinstance(row, dict) and not row.get("defeated") \
                    and row.get("side") == "enemy"
                fname = str((row or {}).get("name", cid))
            if is_enemy:
                factor = _STRIKE_FACTOR.get(tier, 0)
                if factor:
                    dmg = _weapon_magnitude(state, player_eid) * factor \
                        + (1 if surge_mod else 0)
                    op["_target"], op["_dmg"] = fname, dmg
                    strike = {"op": "combatant_hp", "target": cid, "delta": -dmg,
                              "reason": f"{reg.skill_label(sid, player)} {tier}",
                              "_strike": True}   # exact — code decided it, no proposal clamp
                else:
                    op["_target"], op["_dmg"] = fname, 0
        res.rule_ops.append(op)
        if strike is not None:
            res.rule_ops.append(strike)
        if player_eid:                              # RPG-5 (doc 10 §4): use grows mastery —
            amt = MASTERY_TICKS.get(tier, 0)        # code-side, scene-capped in the reducer
            if amt:
                res.rule_ops.append({"op": "master_tick", "char": player_eid,
                                     "skill": sid, "amount": amt})
            if tier == "crit_fail":                 # RPG-5 (doc 10 §5/§8): a crit-fail leaves
                res.rule_ops.append({                # a mark; overreach bites back harder
                    "op": "effect_add", "char": player_eid,
                    "effect": "Backlash" if over else "Strained", "kind": "status"})


# ---- Out-of-combat kills (2026-07-10, Bean) ------------------------------------------
# Outside an active fight you cannot simply DECLARE a kill. Three roads: a STEALTH/concealed
# approach makes it a real Stealth roll (success = a silent kill + XP); a GRAND working
# (epic/mythic scope, ritual / reality-warp) kills by prose + XP; anything else is a NON-MOVE —
# routed, not blocked (approach unseen, force a fight, or bring world-ending power). Combat
# resolves kills through HP, so this only fires when combat.active is False. Freedom of fiction,
# constraint on fact (pillars 4-5).
_KILL_VERBS = re.compile(
    r"\b(kill|kills|slay|slays|murder|murders|assassinate|assassinates|execute|executes|"
    r"behead|beheads|decapitate|finish(?:es)?|dispatch(?:es)?|silence|silences|strangle|"
    r"strangles|throttle|throttles|slit|slits|gut|guts)\b", re.IGNORECASE)
_KILL_STEALTH = ("stealth", "sneak", "shadow", "assassin", "infiltrat", "subterfuge", "guile",
                 "prowl", "stalk", "ambush", "backstab")
_CONCEAL = {"invisible", "invisibility", "hidden", "cloaked", "unseen", "concealed", "shadowmeld",
            "veiled", "obscured", "camouflaged"}
_GRAND = re.compile(
    r"\b(ritual|reality[- ]?warp\w*|unmake\w*|unwrite\w*|erase\w*|obliterate\w*|annihilate\w*|"
    r"disintegrate\w*|apocalyp\w*|cataclysm\w*|godlike|unbeing|banish\w*)\b", re.IGNORECASE)
STEALTH_KILL_XP = 40                    # curated (doc 10 XP scale); a named/tracked target +20
GRAND_KILL_XP = 60


def _kill_intent(res: Tier0Result, state: dict, cfg, user_text: str) -> None:
    """Gate + resolve a DECLARED kill against a present target outside active combat."""
    if (state.get("combat") or {}).get("active"):
        return                                       # inside a fight kills come from HP (War Room)
    if not _KILL_VERBS.search(user_text or "") and not _GRAND.search(user_text or ""):
        return                                       # a kill verb OR a grand-working verb (erase…)
    peid, _player = _player_card(state)
    if not peid:
        return
    ents = state.get("entities") or {}
    eff = state.get("effects") or {}
    present = [(eid, e) for eid, e in ents.items()
               if isinstance(e, dict) and e.get("present") and eid != peid
               and e.get("kind") in ("character", "npc")]
    low = " " + _norm_phrase(user_text) + " "
    tid = tname = None
    for eid, e in sorted(present, key=lambda p: -len(str(p[1].get("name", "")))):
        nm = _norm_phrase(str(e.get("name", "")))
        toks = [t for t in nm.split() if len(t) >= 3]
        if nm and (f" {nm} " in low or (toks and all(f" {t} " in low for t in toks))):
            tid, tname = eid, str(e.get("name", ""))
            break
    if not tid:
        return                                       # no present target named -> not a declared kill
    if any(str(x.get("name", "")).lower() in ("slain", "dead") for x in (eff.get(tid) or [])):
        return                                       # already dead
    tent = ents.get(tid) or {}
    pname = str((ents.get(peid) or {}).get("name", "The player"))
    check_ops = [o for o in res.rule_ops if o.get("op") == "check"]
    stealth_chk = next((o for o in check_ops
                        if any(k in str(o.get("skill", "")).lower() for k in _KILL_STEALTH)), None)
    concealed = any(str(x.get("name", "")).lower() in _CONCEAL for x in (eff.get(peid) or []))
    # a GRAND working is grounded in a real roll: an epic/mythic-scope check, or a reality-warp
    # invocation that ALSO rolled a check (bare "I erase you from reality" with no roll = no basis)
    grand = any(str(o.get("scope", "")).lower() in ("epic", "mythic") for o in check_ops) \
        or (bool(_GRAND.search(user_text or "")) and bool(check_ops))

    def _kill_ops(reason: str, xp: int) -> None:
        bonus = 20 if tent.get("kind") == "npc" or tent.get("role") else 0
        res.rule_ops.extend([
            {"op": "effect_add", "char": tid, "effect": "Slain", "kind": "condition",
             "valence": "negative"},
            {"op": "presence", "entity": tid, "present": False},
            {"op": "award_exp", "char": peid, "amount": int(xp) + bonus, "reason": reason},
            {"op": "reveal_fact", "statement": f"{pname} killed {tname} ({reason})"}])

    if stealth_chk is not None or concealed:         # STEALTH KILL — a real roll carries it
        tier = str((stealth_chk or {}).get("tier", "success" if concealed else "fail"))
        if tier in ("success", "crit_success"):
            _kill_ops("stealth kill", STEALTH_KILL_XP)
            res.kill_note = (f"STEALTH KILL — {tname} dies here, silently and unseen (the roll "
                             f"landed). Narrate the kill: they never cried out, the body is down, "
                             f"{tname} is gone from the scene. Do NOT start a fight.")
        elif tier == "partial":
            res.kill_note = (f"The stealth strike on {tname} only HALF-lands — a wound, not a "
                             f"clean kill, and {tname} is ALERTED. Narrate the botch; a fight may "
                             f"erupt (raise the scene to climax if it does).")
        else:
            res.kill_note = (f"The stealth kill on {tname} FAILS — {tname} senses you, unharmed "
                             f"and alerted. Narrate the miss; a fight is now likely.")
        return
    if grand:                                        # GRAND WORKING — prose kill + XP
        ok = (not check_ops) or any(str(o.get("tier")) in ("success", "crit_success", "partial")
                                    for o in check_ops)
        if ok:
            _kill_ops("a grand working", GRAND_KILL_XP)
            res.kill_note = (f"GRAND WORKING — the reality-bending power the Player invoked "
                             f"consumes {tname}; they are dead and gone from the scene. Narrate it "
                             f"as the momentous, world-touching event it is.")
        else:
            res.kill_note = (f"The grand working aimed at {tname} does not take hold — narrate the "
                             f"power guttering out, {tname} unharmed.")
        return
    res.notices.append(                              # NO BASIS — a routed NON-MOVE
        f"non-move: you can't just declare {tname} dead outside a fight — approach UNSEEN and roll "
        f"Stealth for a silent kill, force a real confrontation, or bring overwhelming power")
    res.kill_note = (
        f"NON-MOVE: the Player declared killing {tname} but has no in-world basis for it here — no "
        f"stealth approach, no fight, no overwhelming power. Do NOT narrate {tname}'s death. Show "
        f"why it can't simply happen and offer the road: slip in unseen (a Stealth roll), force a "
        f"fight, or wield something world-ending.")


# ---- R9: the effect tag protocol (RPG-3, doc 05 §5.4) --------------------------------
# The channel AI-Roguelite never had: the narrating model marks a Status/Condition change
# inline and the ENGINE commits it to the ledger. Tags are proposals (extraction-source
# authority: clamped, quarantined visibly); the prose itself is never the truth.
_EFFECT_TAG_RE = re.compile(
    r"\[\s*(status|condition)\s+(gained|lost)\s*\|\s*([^|\[\]]+?)\s*\|\s*([^|\[\]]+?)"
    r"\s*(?:\|\s*([a-z]+)\s*)?\]", re.IGNORECASE)
_VALENCE_TAG_RE = re.compile(
    r"\[\s*valence\s+shift\s*\|\s*([^|\[\]]+?)\s*\|\s*([^|\[\]]+?)\s*\|\s*"
    r"(negative|neutral|positive)\s*\]", re.IGNORECASE)
_USER_TOKENS = {"{{user}}", "{{char}}", "user", "player"}   # {{user}} -> the Player Card


def _tag_char(token: str, state: dict) -> str:
    t = str(token or "").strip()
    low = t.lower()
    if low in _USER_TOKENS:
        eid, _ = _player_card(state)
        return eid or t
    eid, _ = _player_card(state)               # 2026-07-07 live repro: the DM tags the player
    if eid:                                     # by FIRST NAME ('Kaji'), which alias-resolved to
        ent = state.get("entities", {}).get(eid) or {}   # a discovery-minted twin entity — the
        name = str(ent.get("name", eid))                 # player's own name tokens are the player
        toks = {name.lower()} | {w for w in name.lower().split() if len(w) >= 3}
        if low == eid or low in toks:
            return eid
    return t


def _parse_effect_tags(text: str, state: dict) -> list[dict]:
    """Deterministic regex pass over the DM's settled reply -> effect proposals. Unknown
    valence words are dropped (the preset/default supplies one); unknown bracket tags are
    ignored — this parser mints nothing and decides nothing (invariant 2)."""
    ops: list[dict] = []
    for m in _EFFECT_TAG_RE.finditer(text):
        kind, verb, who, name, val = (m.group(1).lower(), m.group(2).lower(),
                                      _tag_char(m.group(3), state), m.group(4).strip(),
                                      (m.group(5) or "").lower())
        if not who or not name:
            continue
        if verb == "gained":
            op = {"op": "effect_add", "char": who, "effect": name, "kind": kind}
            if val in EFFECT_VALENCES:
                op["valence"] = val
            ops.append(op)
        else:
            ops.append({"op": "effect_remove", "char": who, "effect": name})
    for m in _VALENCE_TAG_RE.finditer(text):
        who = _tag_char(m.group(1), state)
        if who:
            ops.append({"op": "effect_update", "char": who, "effect": m.group(2).strip(),
                        "valence": m.group(3).lower()})
    return ops


# ---- R10: the world tag protocol (RPG-5, playtest 2026-07-06 G1-G5) -------------------
# The R9 spine extended to the rest of the ledger: the narrating model marks scene moves,
# item acquisitions/losses, quest beats, standing shifts, and harm inline; the ENGINE
# commits them (extraction-source authority: clamped, quarantined visibly). This is the
# recording floor the sci-fi playtest proved missing — 27 turns where the [SCENE] block
# lied, items lived only in prose, and quest tags parsed to nothing.
_SCENE_TAG_RE = re.compile(
    r"\[\s*scene\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
_ITEM_TAG_RE = re.compile(
    r"\[\s*item\s+(gained|lost)\s*\|\s*([^|\[\]]+?)\s*\|\s*([^|\[\]]+?)"
    r"\s*(?:\|\s*(\d+)\s*)?\]", re.IGNORECASE)
_QUEST_TAG_RE = re.compile(
    r"\[\s*quest\s*\|\s*([^|\[\]]+?)\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
_AFFINITY_TAG_RE = re.compile(
    r"\[\s*affinity\s*\|\s*([^|\[\]]+?)\s*\|\s*([+-]?\d+)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
_HP_TAG_RE = re.compile(
    r"\[\s*hp\s*\|\s*([^|\[\]]+?)\s*\|\s*([+-]?\d+)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
# Phase 1 (plan doc 13): the DM's combat channels. [foe] introduces an unnamed EXTRA (or
# stages a known NPC) as a combatant — parsed separately (parse_foe_tags) because spawning
# is PRIVILEGED: the pipeline validates and re-sources it as rule, the R8b arming pattern.
# [clash] records an NPC-vs-NPC fight: prose resolves it, the LEDGER remembers it (no dice).
_FOE_TAG_RE = re.compile(
    r"\[\s*foe\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
# the symmetric ally channel (2026-07-10, Bean: "3v3 is missing") — the DM brings a present
# companion onto the player's side, same grammar/authority path as [foe].
_ALLY_TAG_RE = re.compile(
    r"\[\s*ally\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
# §F large-scale battle: the DM OPENS a battle ([battle | name | foe? | tier?]) and REPORTS the
# macro tide ([tide | winning|holding|losing | why]). Both re-sourced as rule (the R8b pattern);
# the engine owns momentum + waves. Gated by [specialization].large_battle in the pipeline.
_BATTLE_TAG_RE = re.compile(
    r"\[\s*battle\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
_TIDE_TAG_RE = re.compile(
    r"\[\s*tide\s*\|\s*(winning|holding|losing)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]", re.IGNORECASE)
_CLASH_TAG_RE = re.compile(
    r"\[\s*clash\s*\|\s*([^|\[\]]+?)\s+vs\.?\s+([^|\[\]]+?)\s*"
    r"(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]", re.IGNORECASE)
# Phase 2 (plan doc 13): the living-world channels. [time] is the DM's clock ceiling —
# a named segment or +N, CLAMPED to at most two segments at parse (the engine owns pace);
# [rumor] surfaces a hidden faction front (reveal only — advancement stays code-side).
_TIME_TAG_RE = re.compile(
    r"\[\s*time\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]", re.IGNORECASE)
_RUMOR_TAG_RE = re.compile(
    r"\[\s*rumor\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?\]", re.IGNORECASE)
_QUEST_NEW = {"new", "add", "added", "start", "started", "begin", "begins", "accepted",
              "offered"}
_QUEST_STATUS_WORDS = {"complete": "complete", "completed": "complete", "done": "complete",
                       "fulfilled": "complete", "success": "complete",
                       "failed": "failed", "fail": "failed",
                       "abandoned": "abandoned", "dropped": "abandoned",
                       "active": "active", "update": None, "updated": None,
                       "progress": None, "in progress": None}


def _parse_world_tags(text: str, state: dict) -> list[dict]:
    """Deterministic regex pass over the DM's settled reply -> ledger proposals for scene,
    items, quests, affinity, and HP. Mints no mechanics, decides no outcomes: item names
    ground on a registry template or commit MECHANICS-FREE; affinity/HP deltas are clamped
    at _enrich; quest facts are text. Unknown bracket tags are ignored (invariant 2)."""
    ops: list[dict] = []
    for m in _SCENE_TAG_RE.finditer(text):
        loc = m.group(1).strip()
        if not loc:
            continue
        op: dict = {"op": "scene_set", "location": loc}
        present: list[str] = []
        for seg in (m.group(2), m.group(3)):
            if not seg:
                continue
            seg = seg.strip()
            if seg.lower().startswith("present:"):
                present = [p.strip() for p in seg[8:].split(",") if p.strip()]
            elif len(seg) <= 40:
                op["phase"] = seg
        ops.append(op)
        if present:
            names = {_tag_char(p, state) for p in present}
            ops.extend({"op": "presence", "entity": n, "present": True} for n in sorted(names))
            here = {eid for eid, e in (state.get("entities") or {}).items()
                    if isinstance(e, dict) and e.get("present")
                    and e.get("kind") in ("character", "npc", "player")}
            lowset = {str(n).lower() for n in names}
            for eid in sorted(here):             # a declared cast REPLACES the present list
                nm = str((state["entities"][eid] or {}).get("name", eid))
                if eid not in names and nm.lower() not in lowset \
                        and (state.get("player") or {}).get(eid) is None:
                    ops.append({"op": "presence", "entity": eid, "present": False})
    for m in _ITEM_TAG_RE.finditer(text):
        verb, who, name = m.group(1).lower(), _tag_char(m.group(2), state), m.group(3).strip()
        if not who or not name:
            continue
        op = {"op": "item_gain" if verb == "gained" else "item_lose",
              "char": who, "name": name}
        if m.group(4):                          # qty on BOTH gained and lost (consume N)
            op["qty"] = int(m.group(4))
        ops.append(op)
    qs = state.get("quests") or {}
    for m in _QUEST_TAG_RE.finditer(text):
        qname, word, note = m.group(1).strip(), m.group(2).strip().lower(), \
            (m.group(3) or "").strip()
        if not qname:
            continue
        known = slug(qname)[:64] in qs or any(
            str((r or {}).get("name", "")).lower() == qname.lower() for r in qs.values())
        if word in _QUEST_NEW or not known:
            op = {"op": "quest_add", "name": qname}
            if note:
                op["detail"] = note
            if word in _QUEST_STATUS_WORDS and _QUEST_STATUS_WORDS[word]:
                ops.append(op)                    # e.g. unseen quest tagged complete: record
                ops.append({"op": "quest_update", "quest": qname,   # it, then settle it
                            "status": _QUEST_STATUS_WORDS[word]})
                continue
            ops.append(op)
            continue
        st = _QUEST_STATUS_WORDS.get(word)
        op = {"op": "quest_update", "quest": qname}
        if st:
            op["status"] = st
        if note:
            op["note"] = note
        if st is None and not note:
            op["note"] = m.group(2).strip()       # free-text beat -> the quest's note line
        ops.append(op)
    for m in _AFFINITY_TAG_RE.finditer(text):
        tgt = m.group(1).strip()
        if not tgt or tgt.lower() in _USER_TOKENS:
            continue                              # standing is measured FROM the player
        try:
            d = int(m.group(2))
        except ValueError:
            continue
        op = {"op": "affinity_adj", "target": tgt, "delta": d}
        if m.group(3):
            op["reason"] = m.group(3).strip()
        ops.append(op)
    peid, _ = _player_card(state)
    for m in _HP_TAG_RE.finditer(text):
        who = _tag_char(m.group(1), state)
        try:
            d = int(m.group(2))
        except ValueError:
            continue
        if not who or d == 0:
            continue
        op = {"op": "hp_adj", "char": who, "delta": d}
        if who != peid:                          # Phase 1: [hp] on a NON-player who is a live
            from .state import resolve_combatant   # combatant reroutes to the clamped
            cid = resolve_combatant(state, m.group(1))   # combatant channel (DM chip damage /
            if cid:                                      # ally blows land on real HP rows)
                op = {"op": "combatant_hp", "target": cid, "delta": d}
        if m.group(3):
            op["reason"] = m.group(3).strip()
        ops.append(op)
    for m in _CLASH_TAG_RE.finditer(text):       # Phase 1: NPC-vs-NPC — record, never resolve
        a, b = _tag_char(m.group(1), state), _tag_char(m.group(2), state)
        if not a or not b or a == b or peid in (a, b):
            continue                             # the player's own fights use dice, not clashes
        op = {"op": "clash_record", "a": a, "b": b}
        if m.group(3):
            op["method"] = m.group(3).strip()
        if m.group(4):
            op["outcome"] = m.group(4).strip()
        ops.append(op)
    from .state import TIMES
    clock = state.get("clock") or {}
    cur = str(clock.get("time_of_day", "evening"))
    ci = TIMES.index(cur) if cur in TIMES else 4
    for m in _TIME_TAG_RE.finditer(text):        # Phase 2: the DM's clock ceiling — clamped
        ref = m.group(1).strip().lower().replace(" ", "_")
        steps = None
        if ref.startswith("+"):
            try:
                steps = int(ref[1:])
            except ValueError:
                continue
        elif ref in ("next_day", "next_morning", "tomorrow"):
            steps = (len(TIMES) - ci) if ci else len(TIMES)   # wrap to dawn (reducer day++)
        elif ref in TIMES:
            steps = (TIMES.index(ref) - ci) % len(TIMES)
            if steps == 0:
                continue                         # restating the current segment moves nothing
        if steps is None or steps <= 0:
            continue
        if ref.startswith("+"):
            steps = min(2, steps)                # +N is capped at two segments (engine owns
        steps = min(len(TIMES), steps)           # pace); a NAMED segment is explicit intent
        ops.append({"op": "time_advance", "to_time_of_day": TIMES[(ci + steps) % len(TIMES)]})
        break                                    # at most ONE time move per reply
    fronts = state.get("fronts") or {}
    if fronts:
        low = " " + " ".join(str(text).lower().replace("-", " ").split()) + " "
        for m in _RUMOR_TAG_RE.finditer(text):   # [rumor | <front/faction> | whisper?]
            ref = m.group(1).strip()
            ops.append({"op": "front_reveal", "front": ref})
            note = (m.group(2) or "").strip()
            if note:
                ops.append({"op": "memory_event", "text": f"Rumor — {ref}: {note}"[:300]})
        for fid, f in sorted(fronts.items()):    # name-mention floor: speaking of an agenda
            if not isinstance(f, dict) or f.get("revealed"):   # by name IS the rumor
                continue
            nm = " ".join(str(f.get("name", "")).lower().replace("-", " ").split())
            if len(nm) >= 6 and f" {nm} " in low:
                ops.append({"op": "front_reveal", "front": fid})
    return ops


def parse_foe_tags(text: str, state: dict) -> list[dict]:
    """Phase 1: the DM's combat-spawn tags in a settled reply -> combatant_spawn ops.
    `[foe | <name> | <tier?> | <armament?>]` stages the ENEMY side; `[ally | <name> |
    <tier?> | <armament?>]` (2026-07-10, Bean: "3v3 is missing") brings a present COMPANION
    onto the player's side — the symmetric channel the DM never had. Spawning is PRIVILEGED,
    so these are NOT extraction proposals — the caller (pipeline) applies them source='rule'
    after this validation, exactly like R8b arms the DM's own check call: the DM narrated the
    combatant into the fiction (the in-world basis); the ENGINE mints the instance with curated
    HP. A name that matches a known entity spawns TRACKED (wounds persist); order/caps enforced
    by the reducer (each side is parser-capped at 3; the player holds one ally slot)."""
    ops: list[dict] = []
    peid, _ = _player_card(state)
    from .state import THREAT_TIERS, resolve_entity_ref
    for side, rx in (("enemy", _FOE_TAG_RE), ("ally", _ALLY_TAG_RE)):
        n = 0
        for m in rx.finditer(text or ""):
            name = m.group(1).strip()
            if not name or _tag_char(name, state) == peid:
                continue                         # never spawn the Player as their own combatant
            op: dict = {"op": "combatant_spawn", "name": name, "side": side}
            for seg in (m.group(2), m.group(3)):
                if not seg:
                    continue
                seg = seg.strip()
                if seg.lower() in THREAT_TIERS:
                    op["tier"] = seg.lower()
                elif len(seg) <= 60:
                    op["armament"] = re.sub(r"^(?:uses|wields|carries|armed with)\s+", "", seg,
                                            flags=re.IGNORECASE)
            eid = resolve_entity_ref(state, name)
            if eid and (state.get("entities", {}).get(eid) or {}).get("kind") \
                    in ("character", "npc"):
                op["char"] = eid                 # a KNOWN cast member fights as themselves
            ops.append(op)
            n += 1
            if n >= 3:                           # the 3v3 cap starts at the parser (per side)
                break
    return ops


def parse_battle_tags(text: str, state: dict) -> list[dict]:
    """§F: the DM's large-scale-battle tags in a settled reply -> privileged ops (re-sourced as
    rule by the pipeline, gated on [specialization].large_battle). `[battle | <name> | <foe?> |
    <tier?>]` OPENS the battle (name + the label/tier of the waves it sends); `[tide |
    winning|holding|losing | <why>]` REPORTS how the wider fight goes (clamped +/-1 step per turn
    at apply — the engine owns the pace). The engine owns momentum and sends the waves; the DM
    only narrates the macro and reports the tide."""
    from .state import THREAT_TIERS
    ops: list[dict] = []
    for m in _BATTLE_TAG_RE.finditer(text or ""):
        name = m.group(1).strip()
        if not name:
            continue
        op: dict = {"op": "battle_start", "name": name}
        for seg in (m.group(2), m.group(3)):
            if not seg:
                continue
            seg = seg.strip()
            if seg.lower() in THREAT_TIERS:
                op["threat"] = seg.lower()
            elif len(seg) <= 60:
                op["foe"] = seg
        ops.append(op)
        break                                    # one battle at a time
    for m in _TIDE_TAG_RE.finditer(text or ""):
        ops.append({"op": "tide_set", "tide": m.group(1).lower(),
                    "why": (m.group(2) or "").strip()[:80]})
    return ops


def parse_reply_tags(text: str, state: dict) -> list[dict]:
    """R9 + R10 combined: parse a settled assistant reply into effect + world ledger proposals.
    Under live_recalc the COLD path calls this on the FRESH reply the instant its stream ends
    (the newest output commits on its own turn — Bean 2026-07-07); under legacy lag-1 the hot
    path calls the two parsers on the echoed-back previous reply. Mints nothing, decides
    nothing — proposals apply with source='extraction' (clamped, quarantined visibly)."""
    ops: list[dict] = []
    if text:
        ops.extend(_parse_effect_tags(text, state))
        ops.extend(_parse_world_tags(text, state))
    return ops


def _commands(text: str, rng: random.Random, res: Tier0Result, rpg: bool = False) -> None:
    """R1 + R7: parse commands from the user's NEW message. `rpg` unlocks the RPG-3b
    set-paths (world./affinity./player.soulmate) — a `none` session's surface is unchanged."""
    for m in OOC_RE.finditer(text):
        cmd = m.group(1).strip()
        low = cmd.lower()
        if low.startswith("roll "):
            spec = cmd[5:].strip()
            result = _roll(spec, rng)
            if result is not None:
                res.user_ops.append({"op": "roll", "spec": spec, "result": result})
            else:
                res.notices.append(f"bad roll spec: {spec}")
        elif low.startswith("aether.freeze"):
            res.user_ops.append({"op": "freeze", "reason": "user"})
        elif low.startswith(("aether.resume", "aether.unfreeze")):
            res.user_ops.append({"op": "unfreeze"})
        elif low.startswith("aether.scene "):
            mode = cmd.split(None, 1)[1].strip().lower()
            if mode in ("live", "flashback", "dream"):
                res.user_ops.append({"op": "scene_mode", "mode": mode})
            else:
                res.notices.append(f"unknown scene mode: {mode}")
        elif low.startswith("aether.set "):
            rest = cmd[len("aether.set "):].strip()
            path, _, value = rest.partition(" ")
            op = translate_path(path, value, rpg=rpg) if value else None
            if op is not None:
                res.user_ops.append(op)
            else:
                res.notices.append(f"unknown/unsupported path: {path}")   # visible, never silent (02 SS12b)
        elif low.startswith("aether.check"):
            _parse_check(cmd[len("aether.check"):].strip(), res)
        elif rpg and low.startswith(("aether.foe ", "aether.ally ")):   # Phase 1: the user's
            side = "enemy" if low.startswith("aether.foe") else "ally"  # own spawn surface
            toks = cmd.split(None, 1)[1].split() if " " in cmd else []
            from .state import THREAT_TIERS
            tier, arm, name_w = None, [], []
            for tk in toks:
                if tk.lower() in THREAT_TIERS and tier is None:
                    tier = tk.lower()
                elif tier is not None:
                    arm.append(tk)
                else:
                    name_w.append(tk)
            if name_w:
                op = {"op": "combatant_spawn", "name": " ".join(name_w), "side": side}
                if tier:
                    op["tier"] = tier
                if arm:
                    op["armament"] = " ".join(arm)
                res.user_ops.append(op)
            else:
                res.notices.append("usage: ((aether.foe <name> [minion|standard|elite|boss]"
                                   " [armament]))")
        elif rpg and low.startswith("aether.combat"):   # ((aether.combat end)) settles it
            rest2 = cmd.split(None, 1)[1].strip().lower() if " " in cmd else ""
            if rest2 in ("end", "over", "stop"):
                res.user_ops.append({"op": "combat_end", "outcome": "called"})
            else:
                res.notices.append("usage: ((aether.combat end))")
        elif rpg and low.startswith("aether.battle"):   # §F: ((aether.battle <name> | tide <t> | end))
            from .state import BATTLE_TIDES
            rest2 = cmd.split(None, 1)[1].strip() if " " in cmd else ""
            low2 = rest2.lower()
            if low2 in ("end", "over", "stop"):
                res.user_ops.append({"op": "battle_end", "outcome": "called"})
            elif low2.startswith("tide"):
                t = rest2.split(None, 1)[1].strip().lower() if " " in rest2 else ""
                if t in BATTLE_TIDES:
                    res.user_ops.append({"op": "tide_set", "tide": t})
                else:
                    res.notices.append("usage: ((aether.battle tide winning|holding|losing))")
            else:
                name = rest2.split(None, 1)[1].strip() if low2.startswith("start") \
                    and " " in rest2 else rest2
                if name:
                    res.user_ops.append({"op": "battle_start", "name": name})
                else:
                    res.notices.append("usage: ((aether.battle <name> | tide <t> | end))")
        elif rpg and low.startswith("aether.equip "):   # manual re-slot failsafe (2026-07-10,
            rest2 = cmd.split(None, 1)[1].strip()        # Bean): ((aether.equip <item> <slot>))
            m2 = re.match(r"^(.*?)[\s,]+(?:to\s+|in\s+|->\s*)?([a-z0-9_]+)\s*$", rest2, re.I)
            from .state import GEAR_SLOTS
            slot2 = m2.group(2).lower() if m2 else ""
            if m2 and slot2 in GEAR_SLOTS and m2.group(1).strip():
                res.user_ops.append({"op": "item_equip", "instance": m2.group(1).strip(),
                                     "slot": slot2})       # _resolve_instance matches by NAME
            else:
                res.notices.append("usage: ((aether.equip <item name> <slot>)) — slots: head "
                                   "face neck shoulders body cape arms hands mainhand offhand "
                                   "waist legs feet back accessory1 accessory2")
        elif low.startswith("aether.status"):
            res.notices.append("status")
        else:
            res.notices.append(f"unknown command: {cmd.split(None, 1)[0]}")


def _safeword_hit(text: str, safewords: list[str]) -> Optional[str]:
    low = text.lower()
    for w in safewords:
        if w and re.search(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", low):
            return w
    return None


def _ngrams(text: str, n: int) -> set:
    toks = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)} if len(toks) >= n else set()


def _aliases(state: dict) -> dict[str, str]:
    out = {}
    for eid, e in state.get("entities", {}).items():
        if e.get("name"):
            out[e["name"].lower()] = eid
        for a in e.get("aliases", []):
            out[str(a).lower()] = eid
    return out


def run(doc: dict, klass: str, duplicate: bool, state: dict, cfg,
        rng: Optional[random.Random] = None) -> Tier0Result:
    """The pass. klass = Resolution.klass value; caller applies user_ops then rule_ops."""
    res = Tier0Result()
    rng = rng or random.Random()
    msgs = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(msgs, list):
        return res

    texts = [(m.get("role"), _msg_text(m.get("content"))) for m in msgs if isinstance(m, dict)]
    last_user = next((t for r, t in reversed(texts) if r == "user"), "")
    last_assistant = next((t for r, t in reversed(texts) if r == "assistant"), "")
    is_new = (not duplicate) and klass in ("new_turn", "new_session", "impersonate")
    raw_mode = cfg.consent.mode == "unrestricted"

    rpg = getattr(cfg, "specialization", None) is not None \
        and cfg.specialization.name == "rpg"

    # R1 + R7 — commands act once, from the new user message only
    if is_new and last_user:
        _commands(last_user, rng, res, rpg=rpg)
        for op in res.user_ops:                  # Phase 1: a user-spawned combatant naming a
            if op.get("op") == "combatant_spawn" and "char" not in op:   # KNOWN cast member
                from .state import resolve_entity_ref                    # fights as themselves
                eid = resolve_entity_ref(state, op.get("name"))
                if eid and (state.get("entities", {}).get(eid) or {}).get("kind") \
                        in ("character", "npc"):
                    op["char"] = eid

    # R8 — skill-checks resolve NOW (RPG only): registered skill -> dice -> PbtA tier -> `check`
    # rule op + this-turn [DIRECTIVE]. Two triggers: explicit ((aether.check ...)) parsed above,
    # PLUS natural-language detection (naming a skill/ability you OWN in prose rolls its governing
    # skill). Swipes RE-ROLL fresh (Bean 2026-07-09): re-parse the same message on a swipe so a
    # regenerate rolls new dice. Inert unless specialization=rpg (invariant 3).
    if rpg and last_user and (is_new or klass == "swipe"):
        if klass == "swipe":
            _parse_checks_only(last_user, res)
        if not res.checks:            # explicit ((aether.check ...)) present -> do NOT also auto-detect
            _detect_nl_checks(last_user, state, cfg, res)
        if not res.checks and last_assistant \
                and getattr(cfg.specialization, "auto_dm_checks", True):
            _parse_dm_called_checks(last_assistant, state, cfg, res)   # R8b: the DM's call arms
        if res.checks:
            pending_foe = None
            entity_aware = bool(getattr(cfg.specialization, "intent_floor", True))
            if getattr(cfg.specialization, "foe_floor", True) \
                    and getattr(cfg.specialization, "war_room", True) \
                    and not (state.get("combat") or {}).get("active"):
                sp = _floor_stage_foe(res, state, last_user, last_assistant,
                                      entity_aware=entity_aware)
                if sp is not None:               # Eranmor floor: the attacked, DM-narrated
                    res.rule_ops.append(sp)      # target opens the War Room itself —
                    band = _floor_group_extras(state, last_assistant, sp)   # the whole named
                    res.rule_ops.extend(band)    # band, not just the one struck (Bean 2026-07-10)
                    res.notices.append(          # no [foe] tag required (pillar 6)
                        f"combat floor: staged '{sp['name']}' as a foe"
                        + (f" (+{len(band)} more of the band)" if band else "")
                        + " — the Player attacked it and the DM's prose grounds it")
                    # fix C: bind the attacking check(s) to the just-staged foe so the opening
                    # strike LANDS this turn (spawn applies before the strike in the batch), and
                    # raise the scene to a combat phase so the fight reads as begun (not 'setup').
                    pending_foe = {"cid": sp["_cid"], "name": sp["name"]}
                    for c in res.checks:            # rebind the attacking check(s) onto the
                        t = str(c.get("target") or "")     # freshly-staged foe: empty targets,
                        if not t or slug(t)[:32] == sp["_cid"] \
                                or _norm_phrase(t) == _norm_phrase(sp["name"]):   # or its name
                            c["target"] = sp["_cid"]
                    if str((state.get("scene") or {}).get("phase", "")).lower() \
                            not in _COMBAT_PHASES:
                        res.rule_ops.append({"op": "scene_set", "phase": "climax",
                                             "_floor": True})
            if _war_room(state, cfg):        # Phase 1: bind strikes to live enemy rows
                _bind_targets(res, state, last_user, entity_aware=entity_aware)
            _resolve_checks(res, state, cfg, rng, pending_foe=pending_foe)
        if getattr(cfg.specialization, "stealth_kills", True):   # out-of-combat kill gating +
            _kill_intent(res, state, cfg, last_user)             # stealth/grand-event kills

    # 2026-07-10 (Eranmor): invented bracket grammar in the DM's last reply -> a one-line
    # corrective on the next prompt (compose renders it; silent both-ways failure no more)
    if is_new and last_assistant and rpg:
        res.off_protocol = _scan_off_protocol(last_assistant)

    # R9 — effect tag protocol (RPG only, doc 05 §5.4) + R10 — world tag protocol (RPG-5:
    # scene / items / quests / affinity / HP). LEGACY lag-1 path only: the DM's LAST reply,
    # echoed back in this request, is scanned here (one turn behind the narration). Under
    # live_recalc (the default, Bean 2026-07-07) the COLD path parses the FRESH reply the
    # instant its stream ends instead, so the newest output commits on its OWN turn — the
    # hot path stays silent to avoid double-applying (pipeline.on_response owns it).
    legacy_tags = not getattr(getattr(cfg, "extraction", None), "live_recalc", True)
    if is_new and last_assistant and rpg and legacy_tags:
        res.proposal_ops.extend(parse_reply_tags(last_assistant, state))

    # R0 — safeword scan (Q13/Q14 scopes)
    if is_new:
        plain_user = OOC_RE.sub("", last_user)
        hit = _safeword_hit(plain_user, cfg.consent.safewords)
        if hit:   # own-message safeword = direct user action -> freezes in EVERY mode incl. raw
            res.user_ops.append({"op": "freeze", "reason": "safeword"})
        elif cfg.consent.safeword_scan == "both" and not raw_mode and last_assistant:
            hit = _safeword_hit(last_assistant, cfg.consent.safewords)
            if hit:   # fiction-side trigger (08 B5/X1, scan=both) — never in raw
                res.rule_ops.append({"op": "freeze", "reason": "safeword"})

    # R2 — scene counters (narrative turns only; swipe/continue don't advance time)
    if is_new and klass != "impersonate":
        res.rule_ops.append({"op": "clock_tick", "minutes": cfg.director.minutes_per_turn})

    # R3 — conservative time keywords (which trigger R4 craving ramp inside the reducer)
    if is_new and last_user:
        low = OOC_RE.sub("", last_user).lower()
        for pat, effect in _TIME_KEYWORDS:
            if re.search(pat, low):
                res.rule_ops.append({"op": "time_advance", **effect})
                break

    # R5 — presence heuristic (LOW confidence, advisory until extraction confirms — 03 R5).
    # 0b (2026-07-09, Bean's notables bug), RPG-GATED: under rpg, ARRIVALS read the
    # ASSISTANT text only — the DM placing someone in the scene is an in-world basis; a
    # player merely wondering ("I hope Marla arrives") is speculation and stages no one
    # (pillar 14: the player speaks only for their PC there). Base sessions keep the old
    # both-sides scan byte-identical — co-narrated arrivals are normal chat RP. Departures
    # always scan both (either side can end a presence cheaply; wrongly-absent self-heals).
    if is_new:
        amap = _aliases(state)
        scan = f"{last_user}\n{last_assistant}".lower()
        arrive_scan = (last_assistant or "").lower() if rpg else scan
        for alias, eid in amap.items():
            if re.search(rf"\b{re.escape(alias)}\b[^.!?\n]{{0,40}}\b{_ARRIVE}\b", arrive_scan):
                res.rule_ops.append({"op": "presence", "entity": eid, "present": True,
                                     "_conf": "low"})
            elif re.search(rf"\b{re.escape(alias)}\b[^.!?\n]{{0,40}}\b{_DEPART}\b", scan):
                res.rule_ops.append({"op": "presence", "entity": eid, "present": False,
                                     "_conf": "low"})

    # R6 — repetition metric over last N=6 assistant turns
    if is_new:
        a_texts = [t for r, t in texts if r == "assistant"][-6:]
        if len(a_texts) >= 3:
            n = cfg.director.stagnation_ngram
            last = _ngrams(a_texts[-1], n)
            if last:
                sims = []
                for prev in a_texts[:-1]:
                    g = _ngrams(prev, n)
                    if g:
                        sims.append(len(last & g) / len(last | g))
                if sims:
                    res.rule_ops.append({"op": "stagnation",
                                         "value": round(sum(sims) / len(sims), 3)})

    # R1 strip — every forwarded user message, every request (hygiene is unconditional)
    res.doc = _strip_ooc(doc)
    return res
