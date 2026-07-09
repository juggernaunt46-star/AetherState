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

from .state import (GEAR_SLOT_ORDER, GEAR_SLOTS, affinity_tier, derived_exposure, is_empty,
                    item_is_gear)

CHARS_PER_TOKEN = 3.3     # 03 SS4 estimate; backend tokenizer replaces this in P3


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
        stats = p.get("stats") or {}
        if stats:
            block.append("Stats: " + " ".join(f"{k}{v}" for k, v in stats.items()))
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
            block.append("Skills: " + line)
        abil = p.get("abilities") or []
        if abil:
            mab = {}
            if reg is not None:
                try:
                    mab = reg.merged_abilities(p)
                except Exception:
                    mab = {}
            tag = {"edge": "passive: advantage", "ward": "passive: no fumble",
                   "extra_die": "active: extra die on a miss", "reroll": "active: reroll a miss",
                   "surge": "active: big swing + higher ceiling", "basis": "grants a basis"}
            bits = []
            for a in abil:
                d = mab.get(str(a)) or {}
                nm = str(d.get("name", a))
                try:
                    mech = _registry.ability_mechanic(d)
                except Exception:
                    mech = "mod"
                bits.append(f"{nm} [{tag[mech]}]" if mech in tag else nm)
            block.append("Abilities: " + ", ".join(bits))
        cards.append("\n".join(block))
    return "[PLAYER] " + "\n".join(cards) if cards else ""


def _render_gear(state: dict) -> str:
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
            bits.append(f"{slot}={txt}")
        stowed = []                             # carried GEAR-class items (not in a body slot)
        for lst in ((state.get("inventory") or {}).get(eid) or {}).values():
            for iid in lst:
                it = items.get(iid)
                if not it or int(it.get("qty", 1)) < 1 or not item_is_gear(it):
                    continue
                q = int(it.get("qty", 1))
                stowed.append((f"{q}× " if q > 1 else "") + str(it.get("name", iid)))
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
    order = players + [e for e in effs if e not in players]
    out: list[str] = []
    for eid in order:
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
        sh = c.get("shape") or c.get("_shape")
        sh = sh if isinstance(sh, dict) else None
        if sh and sh.get("fired"):            # 2026-07-07: an active ability fired on the miss —
            if sh.get("improved"):            # narrate it HONESTLY: did it actually turn the roll?
                clause += (f" (the player spent {sh['fired']} and it turned the roll — "
                           f"narrate that reversal of fortune)")
            else:
                clause += (f" (the player spent {sh['fired']} but the roll still fell short — "
                           f"narrate the effort and the failure, no rescue)")
        clauses.append(clause)
    for eid, p in (state.get("player") or {}).items():   # RPG-5 (doc 10 §7): a defeat this
        d = p.get("defeated") if isinstance(p, dict) else None   # turn is a code-decided
        if isinstance(d, dict) and d.get("turn") == turn:        # outcome CLASS to narrate
            phrase = _DEFEAT_PHRASE.get(str(d.get("outcome")), "defeated")
            clauses.append(f"{_name(state, eid)} is DEFEATED — {phrase}")
    out = ""
    if clauses:
        what = "these outcomes" if len(clauses) > 1 else "this outcome"
        out = ("[DIRECTIVE] NARRATE: " + "; ".join(clauses) + f". Narrate exactly {what}; "
               "do not soften, upgrade, or override the result of a roll.")
    # R8c — the enemy acts on real dice too (rendered whenever a non-player is on scene;
    # the DM is told to ignore it in peaceful beats). Code rolls, the model narrates.
    if cfg is not None and getattr(getattr(cfg, "specialization", None),
                                   "enemy_rolls", True) and (state.get("player") or {}):
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
        if present and (hostile_present or combat_phase or combat_flag):
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
    return out


def _render_quest(state: dict) -> str:
    """[QUEST] — the quest LEDGER first (RPG-5, G3): active quests with stakes/notes, plus
    recently-settled ones so the narrator can close arcs. Falls back to the legacy
    per-character `goal` lines when no quest has ever been recorded."""
    qs = state.get("quests") or {}
    turn = state.get("meta", {}).get("turn", -1)
    bits: list[str] = []
    for qid, q in qs.items():
        if not isinstance(q, dict):
            continue
        st = q.get("status", "active")
        if st == "active":
            t = str(q.get("name", qid))
            if q.get("stakes"):
                t += f" ({q['stakes']})"
            if q.get("note"):
                t += f" — {str(q['note'])[:80]}"
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


def _render_relations(state: dict) -> str:
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
    return "[RELATIONS] " + " · ".join(bits) if bits else ""


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
        scene_bits.append("present: " + ", ".join(sorted(present)))
    if sc or present:
        lines.append("[SCENE] " + " · ".join(scene_bits))

    # [PHYSICAL] pose; worn items; derived exposure (02 SS5.2 — derived, never stored)
    phys_ids = sorted(set(state.get("poses", {})) | set(state.get("clothing", {})))
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
            bits.append("wearing " + ", ".join(sorted(on_body)))
        exposed = derived_exposure(state, eid)
        if exposed:
            bits.append("exposed: " + ", ".join(exposed))
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

    # [DRIVES] — only drives >= inject_threshold (02 SS4.1)
    thr = cfg.drives.inject_threshold
    dbits = []
    for eid, ch in state.get("chars", {}).items():
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
        if "PLAYER" in blocks:
            pblock = _render_player(state, cfg)
            if pblock:
                lines.append(pblock)
        if "EFFECTS" in blocks:
            eblock = _render_effects(state)
            if eblock:
                lines.append(eblock)
        if "GEAR" in blocks:
            gblock = _render_gear(state)
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
            rblock = _render_relations(state)
            if rblock:
                lines.append(rblock)
        if "QUEST" in blocks:
            qblock = _render_quest(state)
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
        if header_only and c.cls != "state_header":
            continue
        if c.cls == "state_header":                     # header (+guard) never dropped (04 SS3.2)
            kept.append(c)
            spent += c.tokens
            continue
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
        comps.append(Component("rules_contract", prompts.rules_contract(cfg),
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
