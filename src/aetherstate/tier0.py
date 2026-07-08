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
from .state import EFFECT_VALENCES, MASTERY_TICKS, slug, translate_path

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
        res.notices.append("check needs a skill: ((aether.check <skill> [+N] [vs DC] "
                           "[scope ...] [use <ability>]))")
        return
    skill, mod, dc, scope, use, i = toks[0], 0, None, None, [], 1
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
        if _CHECK_MOD_RE.match(toks[i]):
            mod += int(toks[i])
        i += 1
    res.checks.append({"skill": skill, "mod": mod, "dc": dc, "scope": scope,
                       "use": use, "raw": rest})


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
            mech = registry.ability_mechanic(adef)
            if mech == "edge":                      # passive advantage: +dice, keep best
                edge_extra += max(1, registry.ability_magnitude(adef, 1))
                shaped.append(str(adef.get("name", aid)))
            elif mech == "ward":                    # passive guard: raise the failure floor
                ward = max(ward, max(1, registry.ability_magnitude(adef, 1)))
                shaped.append(str(adef.get("name", aid)))
        surge_mod, surge_lift, onfail, pay_ability, cd_set = 0, 0, None, {}, {}
        for ref in c.get("use", []):                # active abilities the player invoked
            aid, adef = _find_active_ability(reg, player, ref) if player else (None, None)
            if adef is None:
                res.notices.append(f"you don't know an activated ability '{ref}'")
                continue
            mech, label = registry.ability_mechanic(adef), str(adef.get("name", aid))
            if mech not in registry.ACTIVE_MECHANICS:
                res.notices.append(f"{label} is passive — it already applies, no need to use it")
                continue
            if not registry.ability_applies(adef, sid):
                res.notices.append(f"{label} does not apply to {reg.skill_label(sid, player)}")
                continue
            if int(ability_cd.get(aid, 0)) > now:
                res.notices.append(f"{label} is recharging (ready on turn {int(ability_cd[aid])})")
                continue
            if not _ability_affordable(player, registry.skill_cost(adef)):
                res.notices.append(f"not enough resources to use {label} — recover first")
                continue
            if mech == "surge":                     # on use: big bonus + lift the scope ceiling
                surge_mod += max(1, registry.ability_magnitude(adef, 2))
                surge_lift += 1
                _merge_cost(pay_ability, registry.skill_cost(adef))
                if int(adef.get("cooldown_turns", 0)) > 0:
                    cd_set[aid] = now + int(adef["cooldown_turns"])
                shaped.append(label)
            elif onfail is None:                    # extra_die / reroll: applied ONLY on a miss
                onfail = (aid, adef)

        kept, pool = registry.roll_keep(n_keep, edge_extra, sides, rng)
        eff = (reg.effective_mod(player, sid) if player else 0) + int(c["mod"]) + flat + surge_mod
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
        if player_eid:
            op["char"] = player_eid          # a real entity (kind=player) -> resolves cleanly
        if c["dc"] is not None:
            op["dc"] = c["dc"]
        if scope is not None:
            op["scope"], op["_scope_over"] = scope, over
        if shaped or fired or edge_extra or ward or surge_mod:   # audit for the HUD/roll log
            op["_shape"] = {"abilities": sorted(set(shaped)), "fired": fired,
                            "improved": improved, "pool": list(pool), "kept": list(kept),
                            "edge": edge_extra, "ward": ward, "surge": surge_mod}
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
        res.rule_ops.append(op)
        if player_eid:                              # RPG-5 (doc 10 §4): use grows mastery —
            amt = MASTERY_TICKS.get(tier, 0)        # code-side, scene-capped in the reducer
            if amt:
                res.rule_ops.append({"op": "master_tick", "char": player_eid,
                                     "skill": sid, "amount": amt})
            if tier == "crit_fail":                 # RPG-5 (doc 10 §5/§8): a crit-fail leaves
                res.rule_ops.append({                # a mark; overreach bites back harder
                    "op": "effect_add", "char": player_eid,
                    "effect": "Backlash" if over else "Strained", "kind": "status"})


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
        if verb == "gained" and m.group(4):
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
    for m in _HP_TAG_RE.finditer(text):
        who = _tag_char(m.group(1), state)
        try:
            d = int(m.group(2))
        except ValueError:
            continue
        if not who or d == 0:
            continue
        op = {"op": "hp_adj", "char": who, "delta": d}
        if m.group(3):
            op["reason"] = m.group(3).strip()
        ops.append(op)
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
