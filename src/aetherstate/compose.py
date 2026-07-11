"""Slice composition: state header + guard note -> budget governor -> placement splice.

Sources: 04 SS3.1 (header format), SS3.2 (user guard, Q12), SS4 (placement wrappers),
03 SS4 (budget governor: cap = min(max_tokens, max_fraction*ctx), header floor, priority
drop order), 06 B.1 (placement modes), 01 SS8 (priority classes).

P2 note (logged in findings): header/guard are rendered FRESH per request — they are
deterministic and sub-ms, so freeze/rolls reflect immediately. Expensive components
(memories/director/private blocks, P4) will come from the precomputed slice per 03 SS10.
The governor NEVER touches history in P2: shrink() needs detected ctx (P3 probe).
"""
from __future__ import annotations

import json
import random as _random
from dataclasses import dataclass
from typing import Optional

from .state import (GEAR_SLOT_ORDER, GEAR_SLOTS, _norm_loc, affinity_tier, derived_exposure,
                    is_empty, item_is_gear, travel_cost)

CHARS_PER_TOKEN = 3.3     # 03 SS4 estimate; backend tokenizer replaces this in P3


def _compact(cfg) -> bool:
    """Compression item 2 (2026-07-09): [injection].briefing_style == 'compact'. Verbose
    (default) renders byte-identically to 1.11 — the knob is opt-in by ratified decision."""
    return getattr(getattr(cfg, "injection", None), "briefing_style", "verbose") == "compact"


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


@dataclass
class Component:
    cls: str                # state_header | director_note | memories | relationship_belief | lore
    text: str
    priority: int
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = self.tokens or estimate_tokens(self.text)


def _name(state: dict, eid: str) -> str:
    return state.get("entities", {}).get(eid, {}).get("name") or eid


# ---- RPG specialization blocks (doc 05 §6; rendered only when specialization=rpg) -----
def _render_player(state: dict, cfg=None) -> str:
    """[PLAYER] — compact hot-field projection of the Player Card(s) (doc 06 §2.3). Skills
    render as EFFECTIVE check mods (registry-derived, snapshot-first — the narrator never does
    math); stored raw ranks are the fail-open fallback. Cached registry load: hot-path-legal."""
    players = state.get("player") or {}
    reg = None
    try:
        from . import registry as _registry
        reg = _registry.load(cfg)
    except Exception:                        # fail-open (invariant 1): render without the registry
        reg = None
    cards: list[str] = []
    for eid, p in players.items():
        if not isinstance(p, dict):
            continue
        head = f'{_name(state, eid)} · Lv{p.get("level", 1)}'
        if int(p.get("xp", 0) or 0) > 0:
            head += f' (XP {p["xp"]})'          # RPG-5: progression is visible truth
        hp = p.get("hp") or {}
        if hp.get("max"):
            head += f' · HP {hp.get("cur", hp["max"])}/{hp["max"]}'
        for rname, r in (p.get("resources") or {}).items():
            if isinstance(r, dict) and r.get("max"):
                head += f' · {str(rname).capitalize()} {r.get("cur", r["max"])}/{r["max"]}'
        if int(p.get("stat_points", 0) or 0) > 0:
            head += f' · {p["stat_points"]} stat pt unspent'
        block = [head]
        c2 = _compact(cfg)
        st_lbl, sk_lbl, ab_lbl = ("St: ", "Sk: ", "Ab: ") if c2 \
            else ("Stats: ", "Skills: ", "Abilities: ")
        stats = p.get("stats") or {}
        if stats:
            block.append(st_lbl + " ".join(f"{k}{v}" for k, v in stats.items()))
        skills = p.get("skills") or {}
        line = ""
        if reg is not None:
            try:                             # union: ranked skills + per-character frozen defs
                defs_sk = p.get("defs") if isinstance(p.get("defs"), dict) else {}
                defs_sk = defs_sk.get("skills") if isinstance(defs_sk.get("skills"), dict) else {}
                sk_ids = list(skills) + [k for k in defs_sk if k not in skills]
                line = " ".join(                     # + equipped-gear mods (doc 06 §2.2, RPG-2)
                    f"{reg.skill_label(sid, p)}"
                    f"{reg.effective_mod(p, sid) + _registry.gear_skill_mod(state, eid, sid):+d}"
                    for sid in sk_ids)
            except Exception:
                line = ""
        if not line and skills:              # fallback: stored ranks (pre-RPG-1 shape)
            line = " ".join(f"{str(k).capitalize()} {v}" for k, v in skills.items())
        if line:
            block.append(sk_lbl + line)
        abil = p.get("abilities") or []
        if abil:
            mab = {}
            if reg is not None:
                try:
                    mab = reg.merged_abilities(p)
                except Exception:
                    mab = {}
            tag = {"edge": "adv", "ward": "no-fumble", "extra_die": "xdie", "reroll": "reroll",
                   "surge": "surge", "basis": "basis", "mod_active": "burst"} if c2 else \
                  {"edge": "passive: advantage", "ward": "passive: no fumble",
                   "extra_die": "active: extra die on a miss", "reroll": "active: reroll a miss",
                   "surge": "active: big swing + higher ceiling", "basis": "grants a basis",
                   "mod_active": "active: +N burst on use"}
            bits = []
            for a in abil:
                d = mab.get(str(a)) or {}
                nm = str(d.get("name", a))
                try:
                    mech = _registry.ability_mechanic(d)
                    if mech == "mod" and _registry.ability_is_active(d):
                        mech = "mod_active"          # flat-burst active — invoked, not always-on
                except Exception:
                    mech = "mod"
                bits.append(f"{nm} [{tag[mech]}]" if mech in tag else nm)
            block.append(ab_lbl + ", ".join(bits))
        cards.append("\n".join(block))
    return "[PLAYER] " + "\n".join(cards) if cards else ""


def _render_gear(state: dict, cfg=None) -> str:
    """[GEAR] — the equipped paper-doll (slot=Name(mods)[cap]) PLUS carried GEAR-class items
    (weapons/armor/tools/bags not currently worn), so a sheathed sword reads as gear, not
    inventory (Bean 2026-07-07). Reads only baked per-instance data — pure state, µs."""
    players = state.get("player") or {}
    items = state.get("items") or {}
    out: list[str] = []
    for eid in players:
        slots = (state.get("gear") or {}).get(eid) or {}
        order = [s for s in GEAR_SLOT_ORDER if s in slots] + \
                [s for s in sorted(slots) if s not in GEAR_SLOTS]
        bits = []
        for slot in order:
            iid = slots.get(slot)
            it = items.get(iid) if iid else None
            if not it:
                continue
            txt = str(it.get("name", iid))
            mods = it.get("mods_snapshot") or {}
            mbits = []
            for k in sorted(mods):
                mv = mods[k]
                key = "dmg" if k == "damage" else str(k)
                mbits.append(f"{key}{mv:+d}" if isinstance(mv, int) else f"{key} {mv}")
            if mbits:
                txt += "(" + " ".join(mbits) + ")"
            if it.get("capacity"):
                txt += f"[{it['capacity']}]"
            if it.get("aura"):                  # 2026-07-10 (Bean): a gear piece's PROSE effect —
                txt += f" — {str(it['aura'])[:140]}"   # appearance/glamour/lore the DM must honor
            bits.append(f"{slot}={txt}")
        stowed = []                             # carried GEAR-class items (not in a body slot)
        for lst in ((state.get("inventory") or {}).get(eid) or {}).values():
            for iid in lst:
                it = items.get(iid)
                if not it or int(it.get("qty", 1)) < 1 or not item_is_gear(it):
                    continue
                q = int(it.get("qty", 1))
                stowed.append((f"{q}× " if q > 1 else "") + str(it.get("name", iid)))
        if cfg is not None and _compact(cfg) and len(stowed) > 8:   # item 2: long packs cap
            stowed = stowed[:8] + [f"+{len(stowed) - 8} more"]
        line = " · ".join(bits)
        if stowed:
            line += (" · " if line else "") + "stowed: " + ", ".join(stowed)
        if line:
            out.append(line)
    return "[GEAR] " + "\n".join(out) if out else ""


def _render_inventory(state: dict) -> str:
    """[INVENTORY] — carried INVENTORY-class instances (consumables, materials, devices…)
    grouped by container; `loose` renders last (doc 06 §2.3). GEAR-class carried items render
    under [GEAR] instead (Bean 2026-07-07). Depleted (qty 0 / gone) instances never render."""
    players = state.get("player") or {}
    items = state.get("items") or {}
    out: list[str] = []
    for eid in players:
        conts = (state.get("inventory") or {}).get(eid) or {}
        bits = []
        for cid in sorted(conts, key=lambda c: (c == "loose", str(c))):
            entries = []
            for iid in conts[cid]:
                it = items.get(iid)
                if not it or int(it.get("qty", 1)) < 1 or item_is_gear(it):
                    continue
                q = int(it.get("qty", 1))
                entries.append((f"{q}× " if q > 1 else "") + str(it.get("name", iid)))
            if not entries:
                continue
            cname = "loose" if cid == "loose" else str((items.get(cid) or {}).get("name", cid))
            bits.append(f"{cname}: " + ", ".join(entries))
        if bits:
            out.append(" · ".join(bits))
    return "[INVENTORY] " + "\n".join(out) if out else ""


_VALENCE_GLYPH = {"negative": "-", "neutral": "~", "positive": "+"}


def _render_effects(state: dict) -> str:
    """[EFFECTS] — the ledger of active Statuses & Conditions, player first, then every
    tracked character carrying one (doc 05 §5.4). Fed back every turn so the narrator can't
    drift or forget; expired records (derived, never mutated) simply stop rendering. Pure
    state + a cached-registry-free path — µs."""
    effs = state.get("effects") or {}
    if not effs:
        return ""
    from . import registry as _registry
    turn = state.get("meta", {}).get("turn", -1)
    players = list((state.get("player") or {}).keys())
    bonds = {p[ptr] for p in (state.get("player") or {}).values() if isinstance(p, dict)
             for ptr in ("soulmate", "nemesis") if p.get(ptr)}
    ents = state.get("entities") or {}
    order = players + [e for e in effs if e not in players]
    out: list[str] = []
    for eid in order:
        if eid not in players and eid not in bonds \
                and (ents.get(eid) or {}).get("present") is False:
            continue        # item 5: an absent NPC's statuses stay ledger-only (bonds ride)
        bits = []
        for rec in (effs.get(eid) or {}).values():
            if not isinstance(rec, dict) or not _registry.effect_active(rec, turn):
                continue
            t = f'{rec.get("name", rec.get("id", "?"))}' \
                f'({_VALENCE_GLYPH.get(rec.get("valence"), "~")})'
            if int(rec.get("stacks", 1) or 1) > 1:
                t += f'×{rec["stacks"]}'
            if rec.get("duration") is not None:
                left = int(rec.get("gained_turn", 0)) + int(rec["duration"]) - turn
                t += f'[{max(0, left)}t]'
            bits.append(t)
        if bits:
            out.append(f'{_name(state, eid)}: ' + ", ".join(bits))
    return "[EFFECTS] " + " · ".join(out) if out else ""


_DIRECTIVE_PHRASE = {
    "crit_fail": "critical failure", "fail": "failure",
    "partial": "partial success (yes, but at a cost)", "success": "success",
    "crit_success": "critical success",
}

# RPG-5 (doc 10 §7): defeat outcome classes — code picks the class, the narrator flavors it.
_DEFEAT_PHRASE = {
    "captured": "overcome and taken by the victors; narrate the capture, not a rescue",
    "wake_safe": "knocked out of the fight; they come to somewhere safe, time having passed",
    "robbed": "beaten and stripped of their carried goods; the loss is real",
    "rescued": "downed until someone intervenes to pull them out",
    "death": "this is death, final and unsoftened — narrate it with the weight it deserves",
}


def _war_room_on(state: dict, cfg=None) -> bool:
    """Phase 1 gate: combat instances active + the war_room knob (default on)."""
    if cfg is not None and not getattr(getattr(cfg, "specialization", None),
                                       "war_room", True):
        return False
    return bool((state.get("combat") or {}).get("active"))


# A1 (2026-07-10): the contract-flip combat signal. Kept INDEPENDENT of the war_room knob
# (a fight can still be a fight with war_room off) — this only picks which contract SIZE rides.
_CONTRACT_COMBAT_PHASES = ("climax", "combat", "battle", "fight", "ambush")


def _combat_turn(state: dict) -> bool:
    """A1: a turn is 'in combat' when tracked combatants are on the field OR the scene phase is
    a fight phase — the window where the FULL war-room contract must stay in the prompt."""
    if bool((state.get("combat") or {}).get("active")):
        return True
    phase = str((state.get("scene") or {}).get("phase", "")).lower()
    return phase in _CONTRACT_COMBAT_PHASES


def _auto_compact_contract(state: dict, cfg=None) -> bool:
    """A1 (2026-07-10, Bean): flip the DM rules-contract to its ~40-tok compact form on calm,
    ESTABLISHED turns. Gated by [specialization].auto_compact_contract (default OFF, so an rpg
    session is byte-identical until the table enables it). The FULL contract still rides the first
    `contract_full_turns` turns (the model is still internalizing the rules) and EVERY combat turn.
    Pure state read on the hot path — no network, no config-baked replay concern (compose recomputes
    the briefing each turn; nothing here is journaled)."""
    spec = getattr(cfg, "specialization", None)
    if spec is None or not getattr(spec, "auto_compact_contract", False):
        return False
    if getattr(spec, "contract", "full") == "compact":
        return False                       # already compact by config — nothing to flip
    turn = (state.get("meta") or {}).get("turn", 0)
    if not isinstance(turn, int) or turn <= getattr(spec, "contract_full_turns", 3):
        return False                       # still in the warm-up window -> full contract
    return not _combat_turn(state)         # calm & established -> compact; combat -> full


def _ally_die(state: dict, cid: str) -> tuple[int, int]:
    """Phase 1 (ratified): ONE pre-rolled action die per ally per combat turn — the R8c
    pattern exactly: derived DETERMINISTICALLY from (turn, scene, ally row), no journal
    row, the same turn always re-renders the same dice. Returns (2d6 total, damage die)."""
    import hashlib
    meta = state.get("meta", {})
    loc = str(state.get("scene", {}).get("location_id", ""))
    seed = int(hashlib.md5(f"ally:{meta.get('turn', -1)}:{loc}:{cid}".encode())
               .hexdigest()[:8], 16)
    rng = _random.Random(seed)
    return rng.randint(1, 6) + rng.randint(1, 6), rng.randint(1, 6)


def _die_tier(total: int) -> str:
    return ("CRITS" if total >= 12 else "HITS" if total >= 10
            else "GRAZES" if total >= 7 else "MISSES")


def _render_war(state: dict) -> str:
    """[WAR] — the board, exact HP (Bean: decided — pillar-17 rawness for the DM too):
    every live combatant with side, tier, HP numbers, and armament; the fallen marked.
    Rendered from committed rows only; rides the volatile directive tail (0a constraint)."""
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not rows:
        return ""
    turn = state.get("meta", {}).get("turn", -1)
    started = (state.get("combat") or {}).get("started_turn")
    rnd = max(1, turn - int(started or turn) + 1)
    foes, allies = [], []
    for r in rows.values():
        if not isinstance(r, dict):
            continue
        hp = r.get("hp") or {}
        t = f"{r.get('name', '?')} {int(hp.get('cur', 0))}/{int(hp.get('max', 1))}"
        if r.get("armament"):
            t += f" ({r['armament']})"
        if r.get("tier") and r.get("tier") != "standard":
            t += f" [{r['tier']}]"
        if r.get("defeated"):
            t = f"☠ {r.get('name', '?')} DOWN"
        (foes if r.get("side") == "enemy" else allies).append(t)
    bits = []
    if foes:
        bits.append("foes: " + ", ".join(foes))
    if allies:
        bits.append("allies: " + ", ".join(allies))
    return f"[WAR] round {rnd} — " + " · ".join(bits) if bits else ""


def _opposition_roll(state: dict) -> tuple[int, int]:
    """R8c (Bean 2026-07-09: 'enemies rarely even attempt to attack'): ONE pre-rolled
    enemy-action die per turn, derived DETERMINISTICALLY from (turn, scene, player) — the
    same turn always re-renders the same roll, replay needs no journal row, and the DM
    narrates a die code already cast instead of deciding hits itself. Returns
    (2d6 total, damage die)."""
    import hashlib
    meta = state.get("meta", {})
    loc = str(state.get("scene", {}).get("location_id", ""))
    peid = next(iter(state.get("player") or {}), "")
    seed = int(hashlib.md5(f"opp:{meta.get('turn', -1)}:{loc}:{peid}".encode()).hexdigest()[:8], 16)
    rng = _random.Random(seed)
    return rng.randint(1, 6) + rng.randint(1, 6), rng.randint(1, 6)


def _initiative_order(state: dict, cfg=None) -> list:
    """War-Room turn order (2026-07-10, Bean): every LIVE combatant + the Player, ranked by a
    curated initiative score DESC — the baked row['init'] for foes/allies, the Player's DEX
    modifier ×10 + a stable tiebreak. Pure reader (baked scores) → replay-independent. Returns
    [(score, name, side), ...] highest first; [] when no fight is on."""
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not rows:
        return []
    order: list = []
    pdict = state.get("player") or {}
    peid = next(iter(pdict), "")
    if peid:
        try:
            dex = int(((pdict.get(peid) or {}).get("stats") or {}).get("DEX", 10))
        except (TypeError, ValueError):
            dex = 10
        pmod = (dex - 10) // 2
        if cfg is not None:
            try:
                from . import registry as _registry
                pmod = int(_registry.load(cfg).stat_mod(dex))
            except Exception:
                pass
        tb = 0
        for ch in peid:
            tb = (tb * 31 + ord(ch)) & 0x7FFF
        order.append((pmod * 10 + (tb % 10), _name(state, peid), "player"))
    for cid, r in rows.items():
        if not isinstance(r, dict) or r.get("defeated"):
            continue
        try:
            init = int(r.get("init"))
        except (TypeError, ValueError):
            init = 10
        order.append((init, str(r.get("name", cid)), str(r.get("side", "enemy"))))
    order.sort(key=lambda x: (-x[0], x[1]))
    return order


def _render_battle(state: dict, cfg=None) -> str:
    """§F: the [BATTLE] line — the macro battle's TIDE (the code-owned momentum's sign) + the
    wave count, plus the standing directive: fight the micro slice on the dice, narrate the WIDER
    battle in prose, report shifts with [tide|...], and expect fresh waves while it isn't won.
    Rides the volatile tail (0a-safe). Gated on [specialization].large_battle."""
    if cfg is not None and not getattr(getattr(cfg, "specialization", None),
                                       "large_battle", True):
        return ""
    b = state.get("battle") or {}
    if not b.get("active"):
        return ""
    from .state import battle_tide
    tide = battle_tide(b.get("momentum", 0))
    name = str(b.get("name") or "the battle")
    waves = int(b.get("waves", 0))
    tail = {"winning": "the tide is turning YOUR way — hold and the field is won",
            "holding": "the line holds — the outcome is still open",
            "losing": "you are being pushed back — fresh waves keep coming until it turns"}[tide]
    return (f"[BATTLE] {name}: the wider fight is {tide.upper()} for you"
            + (f" (wave {waves})" if waves else "")
            + f". {tail}. Your War Room slice is the dice; the rest of the field is yours to "
              "NARRATE in prose — report any shift with [tide | winning|holding|losing | why]; "
              "the engine sends the waves and decides when it ends.")


def _render_directive(state: dict, cfg=None) -> str:
    """[DIRECTIVE] — the pre-decided outcome(s) of THIS turn's check(s) (doc 05 §4/§5.2). Reads
    every `check` record for the current turn (checks reuse the rolls buffer, doc 07 §7.1), so a
    multi-check turn directs each result — no silently unnarrated resolution. Rides the
    never-dropped header so the resolve-then-narrate contract can't be budget-cut."""
    turn = state.get("meta", {}).get("turn", -1)
    if "_fresh_checks" in state:                 # 2026-07-09: exactly THIS request's checks —
        checks = [c for c in state["_fresh_checks"] if c.get("tier")]   # reliable, never stale
    else:                                        # replay / no-pipeline fallback: turn-scoped
        checks = [r for r in state.get("rolls", []) if r.get("turn") == turn and r.get("tier")]
    clauses = []
    for c in checks:
        tier = str(c.get("tier"))
        skill = str(c.get("skill") or "the")
        phrase = _DIRECTIVE_PHRASE.get(tier, tier)
        clause = f"{phrase} — the {skill} check resolved as {tier.upper()}"
        if c.get("dm_called") or c.get("_dm_called"):   # R8b: the DM asked for this roll and
            clause += " (the roll YOU called for last reply — narrate it as its answer)"
        if c.get("lost_reply"):                # Eranmor re-serve: same action, lost reply —
            clause += (" (ALREADY SETTLED: this same action was sent before but your reply "
                       "was lost in transit — this is that roll re-served, not a new or "
                       "repeated one; answer the Player's message with it now)")
        bl = c.get("_ability_blocked")         # Eranmor: a declared active that didn't ride
        if isinstance(bl, list) and bl:
            why = "; ".join(f"{b.get('name', '?')} did NOT ride this roll ({b.get('why', '')})"
                            for b in bl if isinstance(b, dict))
            clause += f" ({why} — narrate a plain attempt, not the technique)"
        sh = c.get("shape") or c.get("_shape")
        sh = sh if isinstance(sh, dict) else None
        if sh and sh.get("fired"):            # 2026-07-07: an active ability fired on the miss —
            if sh.get("improved"):            # narrate it HONESTLY: did it actually turn the roll?
                clause += (f" (the player spent {sh['fired']} and it turned the roll — "
                           f"narrate that reversal of fortune)")
            else:
                clause += (f" (the player spent {sh['fired']} but the roll still fell short — "
                           f"narrate the effort and the failure, no rescue)")
        tgt = c.get("target") or c.get("_target")     # Phase 1: a strike's damage is
        dmg = c.get("dmg") if c.get("dmg") is not None else c.get("_dmg")   # code-decided
        if tgt and isinstance(dmg, int):
            if dmg > 0:
                clause += (f" — the blow lands on {tgt} for {dmg} damage (already committed "
                           f"to the ledger; narrate that exact toll, no more, no less)")
            else:
                clause += f" — the attempt on {tgt} draws no blood"
        clauses.append(clause)
    for eid, p in (state.get("player") or {}).items():   # RPG-5 (doc 10 §7): a defeat this
        d = p.get("defeated") if isinstance(p, dict) else None   # turn is a code-decided
        if isinstance(d, dict) and d.get("turn") == turn:        # outcome CLASS to narrate
            phrase = _DEFEAT_PHRASE.get(str(d.get("outcome")), "defeated")
            clauses.append(f"{_name(state, eid)} is DEFEATED — {phrase}")
    for r in ((state.get("combat") or {}).get("combatants") or {}).values():
        if isinstance(r, dict) and r.get("defeated") and r.get("defeated_turn") == turn:
            c2 = (f"{r.get('name', '?')} FALLS this turn — the ledger has them at 0 HP; "
                  f"narrate the fall")                # Phase 1: code-detected defeat + the
            if r.get("dropped"):                      # frozen loot roll, handed pre-decided
                c2 += " (they drop: " + ", ".join(r["dropped"]) + " — now on the field)"
            clauses.append(c2)
    hist = (state.get("combat") or {}).get("history") or []
    if hist and hist[-1].get("turn") == turn:         # the fight settled THIS turn
        h = hist[-1]
        c2 = f"the combat is OVER ({h.get('outcome', 'resolved')})"
        if h.get("loot"):
            c2 += " — unclaimed spoils on the field: " + ", ".join(h["loot"][:6])
        clauses.append(c2 + "; narrate the dust settling and any wounds carried out of it")
    out = ""
    if clauses:
        what = "these outcomes" if len(clauses) > 1 else "this outcome"
        out = ("[DIRECTIVE] NARRATE: " + "; ".join(clauses) + f". Narrate exactly {what}; "
               "do not soften, upgrade, or override the result of a roll. This directive "
               "always resolves the Player's NEWEST message — never an earlier one.")
    kn = state.get("_kill_note")             # 2026-07-10 (Bean): out-of-combat kill outcome —
    if kn:                                   # stealth kill, grand working, or a routed NON-MOVE
        out = (out + "\n" + kn) if out else ("[DIRECTIVE] " + kn)
    # R8c — the enemy acts on real dice too (rendered whenever a non-player is on scene;
    # the DM is told to ignore it in peaceful beats). Code rolls, the model narrates.
    # Phase 1 rides here too: with the War Room active, ally dice + the exact-HP board
    # join the volatile tail (enemy_rolls only gates the [OPPOSITION] die itself).
    enemy_rolls = cfg is not None and getattr(getattr(cfg, "specialization", None),
                                              "enemy_rolls", True)
    if cfg is not None and (state.get("player") or {}):
        peid = next(iter(state["player"]), "")
        pname = _name(state, peid)
        present = [eid for eid, e in (state.get("entities") or {}).items()
                   if e.get("present") and eid != peid
                   and e.get("kind") not in ("location", "faction", "world")]
        aff = state.get("affinity") or {}
        hostile_present = any(
            isinstance(aff.get(f"{peid}->{eid}"), dict)
            and aff[f"{peid}->{eid}"].get("value", 0) <= -10 for eid in present)
        phase = str(state.get("scene", {}).get("phase", "")).lower()
        combat_phase = phase in ("climax", "combat", "battle", "fight", "ambush")
        flags = state.get("world") or {}
        combat_flag = any(str(k).lower() in ("combat", "battle", "fight", "under_attack")
                          and v and str(v).lower() not in ("no", "false", "0", "none")
                          for k, v in flags.items())
        war = _war_room_on(state, cfg)                 # Phase 1: instances on the field
        live_foes = [r for r in ((state.get("combat") or {}).get("combatants")
                                 or {}).values()
                     if isinstance(r, dict) and not r.get("defeated")
                     and r.get("side") == "enemy"] if war else []
        if enemy_rolls and ((present and (hostile_present or combat_phase or combat_flag))
                            or live_foes):
            total, dmg = _opposition_roll(state)
            graze = (dmg + 1) // 2
            tier = ("CRITS" if total >= 12 else "HITS" if total >= 10
                    else "GRAZES" if total >= 7 else "MISSES")
            eff = {"CRITS": f"narrate it landing hard and emit [hp | {pname} | -{dmg + 2} | <why>]",
                   "HITS": f"narrate it landing and emit [hp | {pname} | -{dmg} | <why>]",
                   "GRAZES": f"a glancing cost — emit [hp | {pname} | -{graze} | <why>] or a "
                             "fitting condition tag",
                   "MISSES": "it fails — narrate the miss honestly"}[tier]
            opp = (f"[OPPOSITION] Pre-rolled enemy action for THIS turn: if any hostile moves "
                   f"against {pname}, its attempt came up 2d6={total} — it {tier}. {eff}. "
                   "This die is final (do not soften or invert it); if no one hostile acts "
                   "this turn, ignore this line entirely.")
            out = (out + "\n" + opp) if out else opp
        if war:
            allies = [r for r in ((state.get("combat") or {}).get("combatants")
                                  or {}).values()
                      if isinstance(r, dict) and not r.get("defeated")
                      and r.get("side") == "ally"]
            for r in allies:                           # ratified: ally dice VISIBLE, the R8c
                total, dmg = _ally_die(state, str(r.get("id")))   # pattern (no journal row)
                tier = _die_tier(total)
                act = {"CRITS": f"they strike true — emit [hp | <their foe> | -{dmg + 2} | <why>]",
                       "HITS": f"their blow lands — emit [hp | <their foe> | -{dmg} | <why>]",
                       "GRAZES": f"a glancing effort — emit [hp | <their foe> | "
                                 f"-{(dmg + 1) // 2} | <why>] or a costly setback",
                       "MISSES": "they come up short — narrate the miss honestly"}[tier]
                out += (("\n" if out else "")
                        + f"[ALLY] {r.get('name', '?')}'s action this turn came up "
                          f"2d6={total} — it {tier}. If they engage, {act}. This die is "
                          "final; if they hold back this turn, ignore this line.")
            wl = _render_war(state)
            if wl:
                out += ("\n" if out else "") + wl
            bl = _render_battle(state, cfg)      # §F: the macro battle's tide + waves
            if bl:
                out += ("\n" if out else "") + bl
            io = _initiative_order(state, cfg)   # explicit turn order (2026-07-10, Bean)
            if len(io) > 1:
                seq = " → ".join(nm for _s, nm, _sd in io)
                out += ("\n" if out else "") + (
                    f"[INIT] Turn order this round (highest initiative first): {seq}. Resolve "
                    "the beat in this order — weave the pre-rolled dice above into that sequence; "
                    "whoever is higher acts first when it matters.")
    nud = state.get("_protocol_nudge")
    if nud:                                    # 2026-07-10 (Eranmor): the DM wrote invented
        heads = ", ".join(f"[{h}]" for h in nud[:4])   # bracket grammar last reply — correct
        out += (("\n" if out else "")                  # it NOW, in one line, self-clearing
                + f"[PROTOCOL] Your last reply wrote {heads} line(s) — those are NOT engine "
                  "channels and were IGNORED. The ledger only commits these tags: "
                  "[scene | <loc> | <phase> | present: <names>] · "
                  "[status gained/lost | <char> | <Name> | <valence>] · "
                  "[item gained/lost | <char> | <Item> | <qty>] · "
                  "[quest | <Name> | new/update/complete] · [affinity | <target> | +/-N | <why>] "
                  "· [hp | <char> | -N | <why>] · [foe | <name> | minion/standard/elite/boss | "
                  "<weapon>] · [ally | <name> | <tier?> | <weapon?>] · "
                  "[clash | A vs B | how | outcome]. To call for a roll write "
                  "((aether.check <skill>)) inline — never a [CHECK] line.")
    return out


def _render_quest(state: dict, cfg=None) -> str:
    """[QUEST] — the quest LEDGER first (RPG-5, G3): active quests with stakes/notes, plus
    recently-settled ones so the narrator can close arcs. Falls back to the legacy
    per-character `goal` lines when no quest has ever been recorded. Item 5 (2026-07-09):
    only the 3 most-recently-touched active quests carry stakes/notes — older actives ride
    name-only (still true, just lean) until the story touches them again."""
    qs = state.get("quests") or {}
    turn = state.get("meta", {}).get("turn", -1)
    note_cap = 40 if (cfg is not None and _compact(cfg)) else 80
    actives = [(qid, q) for qid, q in qs.items()
               if isinstance(q, dict) and q.get("status", "active") == "active"]
    detail = {qid for qid, q in sorted(
        actives, key=lambda kv: int(kv[1].get("updated_turn",
                                              kv[1].get("created_turn", 0)) or 0),
        reverse=True)[:3]}
    bits: list[str] = []
    for qid, q in qs.items():
        if not isinstance(q, dict):
            continue
        st = q.get("status", "active")
        if st == "active":
            t = str(q.get("name", qid))
            if qid in detail:
                if q.get("stakes"):
                    t += f" ({q['stakes']})"
                if q.get("note"):
                    t += f" — {str(q['note'])[:note_cap]}"
            bits.append(t)
        elif st in ("complete", "failed") \
                and turn - int(q.get("updated_turn", -10**9)) <= 4:
            bits.append(f"{q.get('name', qid)} — {st.upper()}")
    if bits:
        return "[QUEST] " + " · ".join(bits)
    chars = state.get("chars", {})
    order = list((state.get("player") or {}).keys()) or list(chars.keys())
    quests: list[str] = []
    for eid in order:
        for g in chars.get(eid, {}).get("goals", []):
            if g not in quests:
                quests.append(g)
    return "[QUEST] " + " · ".join(quests) if quests else ""


_BOND_GLYPH = {"soulmate": "♥soulmate", "nemesis": "☠nemesis"}


def _flag_str(v) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v)


def _render_relations(state: dict, cfg=None) -> str:
    """[RELATIONS] — present NPCs' affinity TIER (label, never the integer — doc 05 §5.4)
    plus the bond pointers, flagged (doc 06 §2.4). A bond renders even for an absent
    character (a soulmate matters off-screen). Pure state, µs."""
    players = state.get("player") or {}
    peid = next(iter(players), None)
    if peid is None:
        return ""
    pl = players.get(peid) or {}
    bonds = {pl[ptr]: ptr for ptr in ("soulmate", "nemesis") if pl.get(ptr)}
    ents = state.get("entities", {})
    bits: list[str] = []
    seen: set = set()
    for key, rec in (state.get("affinity") or {}).items():
        a, _, b = key.partition("->")
        if a != peid or not isinstance(rec, dict) or rec.get("kind") == "faction":
            continue
        if not (ents.get(b, {}).get("present") or b in bonds):
            continue                             # present NPCs + bonded ones (doc 05 §6)
        t = f"{_name(state, b)}: {affinity_tier(rec.get('value', 0))}"
        labels = [x for x in (rec.get("labels") or []) if x]
        if b in bonds:
            t += f" {_BOND_GLYPH[bonds[b]]}"
        elif labels:
            t += f" ({labels[-1]})"              # demoted bonds keep their history visible
        bits.append(t)
        seen.add(b)
    for b, ptr in bonds.items():                 # a bond without a ledger row still renders
        if b not in seen:
            bits.append(f"{_name(state, b)}: {_BOND_GLYPH[ptr]}")
            seen.add(b)
    # 0b (anti-main-character): a PRESENT NPC with no relationship row is a STRANGER — said
    # structurally so the DM cannot default to treating the Player as a known main character.
    for eid in sorted(ents):
        e = ents.get(eid) or {}
        if not e.get("present") or eid == peid or eid in seen \
                or e.get("kind") not in ("character", "npc"):
            continue
        bits.append(f"{_name(state, eid)}: {_knows_player(state, peid, eid)}")
    if not bits:
        return ""
    out = "[RELATIONS] " + " · ".join(bits)
    if cfg is not None and _compact(cfg):
        out = out.replace("by reputation (", "rep(")   # item 2 (legend on the contract)
    return out


def _knows_player(state: dict, peid: str, eid: str) -> str:
    """0b (anti-main-character, plan doc 13): how an NPC knows the Player, read from the
    LEDGER — structural, not contract prose. Direct player->npc affinity row = its tier
    label; npc `faction` attribute + a player->faction row = by reputation (that faction's
    standing, and only that); nothing = a stranger. Pure state reads, µs."""
    aff = state.get("affinity") or {}
    rec = aff.get(f"{peid}->{eid}")
    if isinstance(rec, dict):
        return affinity_tier(rec.get("value", 0))
    fac = str(((state.get("attributes") or {}).get(eid) or {}).get("faction") or "")
    if fac:
        fid = fac
        frec = aff.get(f"{peid}->{fid}")
        if not isinstance(frec, dict):           # the attribute may carry a display name
            for key, r in aff.items():
                a, _, b = key.partition("->")
                if a == peid and isinstance(r, dict) and r.get("kind") == "faction" \
                        and _name(state, b).lower() == fac.lower():
                    fid, frec = b, r
                    break
        if isinstance(frec, dict):
            return f"by reputation ({_name(state, fid)}: {affinity_tier(frec.get('value', 0))})"
    return "stranger"


def _render_nearby(state: dict, cfg=None) -> str:
    """[NEARBY] — 0b home anchors: notables whose authored `home` matches the scene's
    canonical location and who are NOT on scene. Tells the DM who is plausibly here —
    with the knows-player gate inline — without staging anyone; presence stays a fiction
    move ([scene]/present tags). Anchored-elsewhere notables spend ZERO tokens (the
    presence-basis gate). Caps at 4 (the fidelity budget goes to the player, pillar 12)."""
    loc = str((state.get("scene") or {}).get("location_id") or "")
    if not loc:
        return ""
    players = state.get("player") or {}
    peid = next(iter(players), None)
    ents = state.get("entities") or {}
    attrs = state.get("attributes") or {}
    loc_ent = ents.get(loc) or {}
    keys = {_norm_loc(loc), _norm_loc(loc.replace("_", " "))}
    for n in [loc_ent.get("name") or ""] + list(loc_ent.get("aliases") or []):
        keys.add(_norm_loc(n))
    keys.discard("")
    bits: list[str] = []
    for eid in sorted(ents):
        e = ents[eid] or {}
        if e.get("present") or e.get("kind") not in ("character", "npc") or eid in players:
            continue
        home = ((attrs.get(eid) or {}).get("home") or "")
        if not home or _norm_loc(str(home)) not in keys:
            continue
        role = (attrs.get(eid) or {}).get("role")
        t = _name(state, eid) + (f" ({role})" if role else "")
        if peid:
            t += f" — {_knows_player(state, peid, eid)}"
        bits.append(t)
        if len(bits) >= 4:
            break
    if not bits:
        return ""
    out = "[NEARBY] " + " · ".join(bits)
    if cfg is not None and _compact(cfg):
        out = out.replace("by reputation (", "rep(")   # item 2 (legend on the contract)
    return out


def _render_factions(state: dict) -> str:
    """[FACTIONS] — faction -> affinity tier LABEL + standing circumstances (doc 05 §6).
    A faction with neither an affinity ledger nor circumstances spends no tokens."""
    players = state.get("player") or {}
    peid = next(iter(players), None)
    aff = state.get("affinity") or {}
    ents = state.get("entities", {})
    facs = state.get("factions") or {}
    fids = [fid for fid, e in ents.items() if (e or {}).get("kind") == "faction"]
    fids += [fid for fid in facs if fid not in fids]
    bits: list[str] = []
    for fid in fids:
        rec = aff.get(f"{peid}->{fid}") if peid else None
        circ = (facs.get(fid) or {}).get("circumstances") or {}
        if not isinstance(rec, dict) and not circ:
            continue
        t = f"{_name(state, fid)}: {affinity_tier((rec or {}).get('value', 0))}"
        if circ:
            t += " (" + ", ".join(f"{k}={_flag_str(v)}" for k, v in circ.items()) + ")"
        bits.append(t)
    return "[FACTIONS] " + " · ".join(bits) if bits else ""


def _render_world(state: dict) -> str:
    """[WORLD] — active global flags/circumstances (world_flag ops; doc 05 §5.6)."""
    w = state.get("world") or {}
    if not isinstance(w, dict) or not w:
        return ""
    return "[WORLD] " + " · ".join(f"{k}={_flag_str(v)}" for k, v in w.items())


def _render_living_tail(state: dict, cfg=None) -> str:
    """Phase 2 (plan doc 13, ratified): the living-world lines — volatile, so they ride
    the injected TAIL after the directive (the 0a KV-cache constraint). Renders ONLY
    committed truth: a fresh travel move (with a deterministic en-route cue — md5 of the
    committed route + day, the R8c pattern: same state, same bytes, no journal row), a
    front that just FILLED (the DM is directed to narrate its consequence NOW), and the
    REVEALED clocks' standings. Rumor-gating applies here — hidden fronts stay hidden
    from the briefing; the Console shows everything (state_summary)."""
    if getattr(cfg, "specialization", None) is None or cfg.specialization.name != "rpg" \
            or not getattr(cfg.specialization, "living_world", True) \
            or not state.get("player"):
        return ""
    import hashlib
    out: list[str] = []
    turn = int((state.get("meta") or {}).get("turn", -1))
    day = int((state.get("clock") or {}).get("day", 1))
    mv = (state.get("scene") or {}).get("last_move") or {}
    if isinstance(mv, dict) and mv.get("to") and turn - int(mv.get("turn", -10**9)) <= 1:
        frm, to = str(mv.get("from", "")), str(mv.get("to", ""))
        cost = travel_cost(state, frm, to)
        seed = int(hashlib.md5(f"trav:{frm}:{to}:{day}".encode()).hexdigest()[:8], 16)
        cue = ("the road is quiet", "an omen on the road — foreshadow trouble ahead",
               "danger finds them en route — stage an encounter NOW (introduce the threat "
               "with a [foe] tag if it comes to violence)")[
                   0 if seed % 6 < 3 else (1 if seed % 6 < 5 else 2)]
        out.append(f"[TRAVEL] {_name(state, frm)} → {_name(state, to)} "
                   f"({cost} segment{'s' if cost > 1 else ''} of the day spent): {cue}.")
    fresh, standing = [], []
    for fid, f in sorted((state.get("fronts") or {}).items()):
        if not isinstance(f, dict) or not f.get("revealed"):
            continue
        nm = str(f.get("name", fid))
        if f.get("done") and turn - int(f.get("filled_turn", -10**9)) <= 1:
            cons = str(f.get("consequence") or "").strip() or "its consequence lands"
            fresh.append(f"[FRONT] {nm} HAS COME TO A HEAD: {cons} — the world moved; "
                         f"show it on-screen NOW, do not wait for the Player.")
        elif not f.get("done"):
            fac = str(f.get("faction") or "").replace("_", " ")
            standing.append(f"{nm}{' (' + fac + ')' if fac else ''} "
                            f"{int(f.get('filled', 0))}/{int(f.get('segments', 6))}")
    out.extend(fresh)
    if standing:
        out.append("[FRONTS] rumored agendas in motion — " + " · ".join(standing) +
                   " (these advance on their own; weave their momentum into the world)")
    return "\n".join(out)


# ------------------------------ header (04 SS3.1) -----------------------------------
def render_header(state: dict, cfg) -> str:
    if is_empty(state):
        return ""
    lines: list[str] = []
    sc = state.get("scene", {})
    clock = state.get("clock", {})

    present = [_name(state, eid) for eid, e in state.get("entities", {}).items()
               if e.get("present")]
    scene_bits = []
    if sc.get("location_id"):
        scene_bits.append(str(sc["location_id"]))
    scene_bits.append(f'{clock.get("time_of_day", "evening")}, day {clock.get("day", 1)}')
    if sc.get("phase"):
        scene_bits.append(f'phase {sc["phase"]}')
    if sc.get("mode") in ("flashback", "dream"):
        scene_bits.append(f'{sc["mode"].upper()} SCENE')
    if present:
        scene_bits.append(("here: " if _compact(cfg) else "present: ")
                          + ", ".join(sorted(present)))
    if sc or present:
        lines.append("[SCENE] " + " · ".join(scene_bits))

    # [PHYSICAL] pose; worn items; derived exposure (02 SS5.2 — derived, never stored).
    # Item 5 scoping (2026-07-09): an EXPLICITLY-absent entity spends no tokens here — its
    # ledger state persists untouched; only the render is scene-scoped. Missing/unknown
    # presence keeps rendering (chats that never emit presence ops lose nothing).
    ents_all = state.get("entities", {})
    phys_ids = sorted(eid for eid in (set(state.get("poses", {}))
                                      | set(state.get("clothing", {})))
                      if (ents_all.get(eid) or {}).get("present") is not False)
    wear_lbl, exp_lbl = ("wear: ", "exp: ") if _compact(cfg) else ("wearing ", "exposed: ")
    phys = []
    for eid in phys_ids:
        bits = []
        pose = state.get("poses", {}).get(eid)
        if pose:
            bits.append(pose["base"] + (f'@{pose["anchor"]}' if pose.get("anchor") else ""))
        items = state.get("clothing", {}).get(eid, {})
        on_body = [f"{n}({it['state']})" for n, it in items.items()
                   if it.get("state") in ("worn", "opened", "displaced")]
        if on_body:
            bits.append(wear_lbl + ", ".join(sorted(on_body)))
        exposed = derived_exposure(state, eid)
        if exposed:
            bits.append(exp_lbl + ", ".join(exposed))
        if bits:
            phys.append(f'{_name(state, eid)}: ' + "; ".join(bits))
    if phys:
        lines.append("[PHYSICAL] " + "\n".join(phys))

    contacts = state.get("contacts", {})
    if contacts:
        cbits = [f'{_name(state, c["from_char"])}→{_name(state, c["to_char"])}: '
                 f'{c["type"]}({c.get("intensity", 1)}) '
                 f'{c.get("from_part") or "?"}→{c.get("to_part") or "?"}'
                 for c in contacts.values()]
        lines.append("[CONTACT] " + "; ".join(cbits))

    # [CONSENT] — omitted entirely in unrestricted mode (Q13: inert for generation)
    if cfg.consent.mode != "unrestricted":
        consent = state.get("consent", {})
        cmap: dict[str, list[str]] = {}
        for key, entry in consent.items():
            a, b, cat = key.split("|", 2)
            pair = f"{_name(state, a)}↔{_name(state, b)}"
            lvl = entry.get("level", "unknown")
            mx = entry.get("max_intensity")
            cmap.setdefault(pair, []).append(f"{cat} {lvl}" + (f"≤{mx}" if mx is not None else ""))
        parts = [f"{pair}: " + "; ".join(sorted(items)) for pair, items in sorted(cmap.items())]
        if state.get("frozen"):
            reason = state.get("frozen_reason", "user")
            parts.append("FROZEN — " + ("safeword active" if reason == "safeword"
                                        else "paused by user"))
        if parts:
            lines.append("[CONSENT] " + " · ".join(parts))
    elif state.get("frozen"):   # user-commanded freeze surfaces even in raw (Q13: user controls always work)
        lines.append("[CONTROL] scene paused by user — do not escalate; follow the user's lead")

    # [DRIVES] — only drives >= inject_threshold (02 SS4.1); item 5: absent NPCs' drives
    # stay ledger-only (rendered again the moment they return)
    thr = cfg.drives.inject_threshold
    dbits = []
    for eid, ch in state.get("chars", {}).items():
        if (ents_all.get(eid) or {}).get("present") is False:
            continue
        for o in ch.get("obsessions", {}).values():
            if o["intensity"] >= thr:
                dbits.append(f'{_name(state, eid)}: obsession {o["target"]} '
                             f'{o["intensity"]}/100 ({o["flavor"]})')
        for sub, c in ch.get("cravings", {}).items():
            if c["level"] >= thr:
                seed = c.get("_seed", {})
                wd = (c["level"] >= seed.get("withdrawal_level", 70)
                      and c["dependency"] >= seed.get("withdrawal_dependency", 50))
                dbits.append(f'{_name(state, eid)}: craving {sub} {c["level"]}/100'
                             + (" — withdrawal" if wd else ""))
    if dbits:
        lines.append("[DRIVES] " + "; ".join(sorted(dbits)))

    if clock.get("calendar_note"):
        lines.append(f'[CLOCK] {clock["calendar_note"]}')

    # R7: rolls from the current turn, injected once (state caps the list; turn-scoped render)
    turn = state.get("meta", {}).get("turn", -1)
    rolls = [r for r in state.get("rolls", []) if r.get("turn") == turn and "spec" in r]
    if rolls:
        lines.append("[ROLL] " + "; ".join(f'{r["spec"]} = {r["result"]}' for r in rolls))

    # RPG specialization blocks (doc 05 §6) — gated by the profile's `blocks` list and the
    # active specialization; omitted when their data is absent (same pattern as [CONSENT]).
    if getattr(cfg, "specialization", None) is not None and cfg.specialization.name == "rpg":
        blocks = cfg.specialization.blocks
        if "DIRECTIVE" in blocks:
            dblock = _render_directive(state, cfg)
            if dblock:
                lines.append(dblock)
            lw = _render_living_tail(state, cfg)   # Phase 2: clock/front/travel lines ride
            if lw:                                 # the volatile tail (0a constraint)
                lines.append(lw)
        if "PLAYER" in blocks:
            pblock = _render_player(state, cfg)
            if pblock:
                lines.append(pblock)
        if "EFFECTS" in blocks:
            eblock = _render_effects(state)
            if eblock:
                lines.append(eblock)
        if "GEAR" in blocks:
            gblock = _render_gear(state, cfg)
            if gblock:
                lines.append(gblock)
        if "INVENTORY" in blocks:
            iblock = _render_inventory(state)
            if iblock:
                lines.append(iblock)
        if "FACTIONS" in blocks:                 # RPG-3b social plane (doc 05 §6)
            fblock = _render_factions(state)
            if fblock:
                lines.append(fblock)
        if "RELATIONS" in blocks:
            rblock = _render_relations(state, cfg)
            if rblock:
                lines.append(rblock)
        if "NEARBY" in blocks:                   # 0b home anchors (plan doc 13)
            nblock = _render_nearby(state, cfg)
            if nblock:
                lines.append(nblock)
        if "QUEST" in blocks:
            qblock = _render_quest(state, cfg)
            if qblock:
                lines.append(qblock)
        if "WORLD" in blocks:
            wblock = _render_world(state)
            if wblock:
                lines.append(wblock)

    return "\n".join(lines)


# ------------------------------ guard note (04 SS3.2, Q12) --------------------------
def render_guard(cfg, stamp, klass: str, evidence: Optional[str] = None) -> str:
    if not cfg.user_guard.enabled:
        return ""
    if klass == "impersonate":   # model SHOULD write the user's voice this turn (05/Q12)
        return ""
    name = cfg.user_guard.name or (stamp.user if stamp and stamp.user else "")
    if not name:
        return ""               # heuristic name resolution lands with the extension (P5)
    dm = (getattr(cfg, "specialization", None) is not None
          and cfg.specialization.name == "rpg" and cfg.specialization.dm_guard)
    if evidence and cfg.user_guard.mode == "prevent_and_correct":   # L9 escalation (04 SS3.2)
        who = "the Player " if dm else ""
        return (f'[CONTROL] VIOLATION last turn: you wrote for {who}{name} ("{evidence}"). '
                f"{name} is played by the user ONLY. Never write {name}'s dialogue, "
                f"actions, or thoughts. Stop where {name} must act.")
    if dm:   # DM/Game-Master framing of the guard (doc 05 §3.2) — text selection, not new logic
        return (f"[CONTROL] You are the Game Master. Narrate the world, its NPCs, factions, "
                f"and the outcomes the engine resolves — never the Player. {name} is the "
                f"Player, played by the user ONLY. Never write {name}'s dialogue, actions, "
                f"decisions, inner thoughts, or dice; resolve only what the engine hands you "
                f"and end your reply in-fiction where {name} must act — no 'What will "
                f"you do?' prompts.")
    return (f"[CONTROL] {name} is played by the user ONLY. Never write {name}'s dialogue, "
            f"actions, decisions, or inner thoughts. End your reply where {name} must "
            f"respond. Portray {name} only as perceived by others.")


# ------------------------------ governor (03 SS4) -----------------------------------
def govern(components: list[Component], cfg) -> list[Component]:
    cap = cfg.injection.max_tokens
    if cfg.injection.assumed_ctx_tokens > 0:
        cap = min(cap, int(cfg.injection.max_fraction * cfg.injection.assumed_ctx_tokens))
    if cap <= 0:
        return []                                       # inject nothing (03 SS4 floor rule)
    comps = [c for c in components if c.text]
    header_only = cap < cfg.injection.header_floor_tokens
    kept, spent = [], 0
    for c in sorted(comps, key=lambda c: -c.priority):
        if header_only and c.cls not in ("state_header", "rules_contract"):
            continue                                     # the contract is floored too (below)
        if c.cls == "state_header":                     # header (+guard) never dropped (04 SS3.2)
            kept.append(c)
            spent += c.tokens
            continue
        if c.cls == "rules_contract":                   # 2026-07-10 (Eranmor): the rpg contract
            kept.append(c)                              # DEGRADES (compose picks compact) but
            spent += c.tokens                           # never silently drops — losing the tag
            continue                                    # grammar mid-campaign bred fake dialects
        if spent + c.tokens <= cap:
            kept.append(c)
            spent += c.tokens
        else:
            break                                       # drop this class and everything below
        # (memories mid-list truncation lands with memories, P4)
    return kept


# ------------------------------ splice (06 B.1 / 04 SS4) ----------------------------
def splice(doc: dict, text: str, cfg) -> dict:
    msgs = list(doc.get("messages", []))
    mode = cfg.injection.placement
    block = {"role": "system", "content": text}
    if mode == "system_merge":
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "system" \
                    and isinstance(m.get("content"), str):
                merged = (m["content"] + "\n\n--- SCENE STATE (engine) ---\n"
                          + text + "\n--- END SCENE STATE ---")
                msgs[i] = {**m, "content": merged}
                break
        else:
            msgs.insert(0, block)
    elif mode == "suffix":
        msgs.append(block)
    else:  # depth (default; st_native routing is P5 — falls back to depth proxy-side)
        pos = max(0, len(msgs) - max(0, cfg.injection.depth))
        msgs.insert(pos, block)
    return {**doc, "messages": msgs}


# ------------------------------ entry ----------------------------------------------
def compose(doc: dict, state: dict, cfg, stamp, klass: str,
            recall: Optional[list] = None, note: str = "",
            guard_evidence: Optional[str] = None) -> tuple[Optional[dict], list]:
    """Returns (modified doc | None if nothing to inject, kept components for the slice row)."""
    header = render_header(state, cfg)
    guard = render_guard(cfg, stamp, klass, evidence=guard_evidence)
    joined = "\n".join(t for t in (header, guard) if t)   # guard rides the header class (04 SS3.2)
    comps = [Component("state_header", joined, cfg.injection.priorities.get("state_header", 100))
             ] if joined else []
    if note:                                               # linter corrective / director (03 SS9)
        comps.append(Component("director_note", note,
                               cfg.injection.priorities.get("director_note", 80)))
    if getattr(cfg, "specialization", None) is not None and cfg.specialization.name == "rpg":
        from . import prompts                               # DM rules-contract (doc 05 §5.2)
        # 2026-07-10 (Eranmor): the contract used to be a droppable component — on a rich
        # sheet the WHOLE protocol ([TAGS] grammar, [foe], dm-rules, R8b) silently vanished
        # mid-campaign and the DM invented its own grammar, unheard and uncorrected. The
        # contract is what MAKES rpg an RPG (Bean 07-07): degrade full -> compact when the
        # budget is tight, and govern keeps whichever variant rides STICKY (never dropped).
        cap = cfg.injection.max_tokens
        if cfg.injection.assumed_ctx_tokens > 0:
            cap = min(cap, int(cfg.injection.max_fraction * cfg.injection.assumed_ctx_tokens))
        spent = sum(c.tokens for c in comps)
        # A1 (2026-07-10, Bean): on calm, established turns flip to the compact contract (opt-in;
        # the full contract still rides the warm-up turns and every combat turn). This is a
        # DELIBERATE flip — distinct from the budget-degrade below, which only fires when the full
        # contract will not fit. off (default) => force_compact=False => byte-identical to before.
        auto_compact = _auto_compact_contract(state, cfg)
        text = prompts.rules_contract(cfg, force_compact=auto_compact)
        if not auto_compact and getattr(cfg.specialization, "contract", "full") != "compact" \
                and spent + estimate_tokens(text) > cap:
            text = prompts.rules_contract(cfg, force_compact=True)   # D7: degrade, never drop
        comps.append(Component("rules_contract", text,
                               cfg.injection.priorities.get("rules_contract", 30)))
    if recall:                                             # Q15 precomputed lines (04 SS3.5)
        from . import memory as _memory
        who = stamp.speaker if (stamp and getattr(stamp, "speaker", None)) else None
        comps.append(Component("memories", _memory.render_recall(recall, who),
                               cfg.injection.priorities.get("memories", 60)))
    kept = govern(comps, cfg)
    if not kept:
        return None, []
    body_text = "\n".join(c.text for c in kept)
    return splice(doc, body_text, cfg), [{"cls": c.cls, "tokens": c.tokens} for c in kept]


def to_bytes(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
