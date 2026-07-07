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
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from . import registry
from .state import EFFECT_VALENCES, translate_path

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
        res.notices.append("check needs a skill: ((aether.check <skill> [+N] [vs DC]))")
        return
    skill, mod, dc, scope, i = toks[0], 0, None, None, 1
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
        if _CHECK_MOD_RE.match(toks[i]):
            mod += int(toks[i])
        i += 1
    res.checks.append({"skill": skill, "mod": mod, "dc": dc, "scope": scope, "raw": rest})


def _player_card(state: dict) -> tuple[Optional[str], dict]:
    """The one Player Card per branch (RPG); (eid, record) or (None, {})."""
    for eid, rec in (state.get("player") or {}).items():
        if isinstance(rec, dict):
            return eid, rec
    return None, {}


_SCOPE_RANK = {"minor": 0, "standard": 1, "major": 2, "epic": 3, "mythic": 4}


def _resolve_checks(res: Tier0Result, state: dict, cfg, rng: random.Random) -> None:
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
        rolled = registry.roll_dice(dice, rng)
        if rolled is None:
            res.notices.append(f"bad dice spec: {dice}")
            continue
        naturals, sides, flat = rolled
        eff = (reg.effective_mod(player, sid) if player else 0) + int(c["mod"]) + flat
        if player_eid:                # RPG-2: equipped-gear mods naming the skill flow into the
            eff += registry.gear_skill_mod(state, player_eid, sid)   # roll (doc 06 §2.2), baked
            eff += registry.effect_skill_mod(state, player_eid, sid, now)   # RPG-3: ledger effects
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
                cap = registry.CHECK_TIERS[max(2, 4 - over)]   # ...and a low ceiling
        tier, total = registry.resolve_tier(naturals, eff, sides, c["dc"], tiers)
        if cap is not None and registry.CHECK_TIERS.index(tier) \
                > registry.CHECK_TIERS.index(cap):
            tier = cap                              # baked FINAL — replay never re-derives it
        op = {"op": "check", "skill": sid, "result": total, "tier": tier,
              "_mod": eff, "_dice": dice, "_seed": naturals}
        if player_eid:
            op["char"] = player_eid          # a real entity (kind=player) -> resolves cleanly
        if c["dc"] is not None:
            op["dc"] = c["dc"]
        if scope is not None:
            op["scope"], op["_scope_over"] = scope, over
        res.rule_ops.append(op)


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
    if t.lower() in _USER_TOKENS:
        eid, _ = _player_card(state)
        return eid or t
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

    # R8 — explicit skill-checks resolve NOW (RPG only): registered skill -> dice -> PbtA tier
    # -> `check` rule op + this-turn [DIRECTIVE]. Inert unless specialization=rpg (invariant 3).
    if is_new and res.checks and rpg:
        _resolve_checks(res, state, cfg, rng)

    # R9 — effect tag protocol (RPG only, doc 05 §5.4): the DM's LAST settled reply is
    # scanned once per new turn; proposals apply with source="extraction" (pipeline). A
    # swiped reply never half-applies: tags parse only when its replacement settles.
    if is_new and last_assistant and rpg:
        res.proposal_ops.extend(_parse_effect_tags(last_assistant, state))

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

    # R5 — presence heuristic (LOW confidence, advisory until extraction confirms — 03 R5)
    if is_new:
        amap = _aliases(state)
        scan = f"{last_user}\n{last_assistant}".lower()
        for alias, eid in amap.items():
            if re.search(rf"\b{re.escape(alias)}\b[^.!?\n]{{0,40}}\b{_ARRIVE}\b", scan):
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
