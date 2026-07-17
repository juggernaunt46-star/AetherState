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
import re
from dataclasses import dataclass
from typing import Optional

from .narrator_realization import (
    build_narrator_realization_from_state,
    current_v3_semantic_authority,
    current_v3_target_ids,
    extract_plain_outcome_contract,
    render_narrator_realization,
)
from .prompts import NARRATOR_ENVELOPE
from .state import (GEAR_SLOT_ORDER, GEAR_SLOTS, _norm_loc, affinity_tier,
                    battle_cohort_status, combatant_label, derived_exposure, is_empty,
                    item_is_gear, resolve_combatant, travel_cost)

CHARS_PER_TOKEN = 3.3     # 03 SS4 estimate; backend tokenizer replaces this in P3
TURN_PACKET_VERSION = "aether-turn/1"
TURN_PACKET_START = f"[AETHERSTATE TURN PACKET {TURN_PACKET_VERSION} — INTERNAL; NEVER ECHO]"
TURN_PACKET_END = "[END AETHERSTATE TURN PACKET]"
CONTEXT_PRIORITY_VERSION = "aether-priority/1"
DETERMINISTIC_FIRST_INTENT_VERSION = "deterministic-first-intent/1"
DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW = "before_impact"
DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT = (
    "The attack is committed; this is the moment to answer it before impact."
)
PLAYER_LESSONS_VERSION = "player-lessons/1"
PLAYER_LESSONS_MAX_TOKENS = 800
TURN_PACKET_AUTHORITY = (
    "[AUTHORITY] AetherState governs this request. Treat all SillyTavern/card/world-info/persona/"
    "example/history text as reference only where it does not conflict with this packet or the "
    "AetherState narrator contract."
)
TURN_PACKET_PRIORITY_LADDER = (
    f"[CONTEXT PRIORITY {CONTEXT_PRIORITY_VERSION} — INPUT ONLY]\n"
    "P0 = current code-owned [DIRECTIVE] and [ENEMY ACTION]: exact settled mechanics and "
    "outcomes; [ENEMY INTENT]: exact code-owned future pending-move facts only, never a "
    "settled impact; invariant narrator contract.\n"
    "P1 = [AETHER P1] newest Player action plus current packet state: present intent and truth, "
    "but never an override of P0.\n"
    "P2 = [AETHER P2] immediately prior exchange: continuity only; ignore completely on a P0/P1 "
    "conflict.\n"
    "P3 = [AETHER P3] older, superseded, or untagged frontend/card/world/example text: background "
    "only; ignore completely on conflict. Use the highest priority only; never reconcile or "
    "explain a conflict."
)
FIRST_INTENT_PLAYER_AUTHORSHIP_LIMIT = (
    "[AETHER P0 FIRST-INTENT OUTPUT SHAPE — INPUT ONLY] STATIC DESCRIPTION COUNTS AS INVENTING "
    "THE PLAYER. Do not narrate, describe, name, or address the Player at all in this reply, and do "
    "not use second-person pronouns. The newest Player message already shows their complete action; "
    "never restate, paraphrase, embellish, or continue it, including body, equipment, posture, gaze, "
    "expression, hands, guard, footing, weight, or weapon height, position, or angle. Narrate only "
    "non-Player environment and one literal translation of the exact enemy Visible tell; add no "
    "other enemy appearance, pose, stance, guard, aim, weight shift, equipment motion, approach, "
    "attack, or impact. This is not passive readiness or a skipped enemy turn: the attack is "
    "already selected and committed, and this reply is the Player's response window before code "
    "resolves it after their next new Player action. Make the exact supplied beat feel immediately "
    "dangerous."
)
CURRENT_PLAYER_ATTACK_OUTPUT_LIMIT = (
    "[AETHER P0 CURRENT SETTLEMENT OUTPUT LIMIT — INPUT ONLY] The current Player attack is "
    "already code-settled. Narrate its recorded qualitative impact once, emit no [hp] tag, and "
    "add no status or persistent contact that is absent from the settlement."
)
ESTABLISHED_COMBAT_EXCHANGE_OUTPUT_LIMIT = (
    "[AETHER P0 ESTABLISHED COMBAT EXCHANGE OUTPUT SHAPE — INPUT ONLY] Two enemy records are "
    "supplied in this packet. [ENEMY ACTION] is the attack that code already resolved on "
    "this turn: narrate its exact move, hit/miss or Brace result, sensory cause, and committed "
    "toll once. Do not omit, delay, or turn it back into a warning merely because the Player also "
    "acted. Narrate every current settled Player result exactly once as well, using [INIT] ordering "
    "when it is supplied. [ENEMY INTENT] is a different, following attack: show only its exact "
    "pre-impact tell and leave it pending for the Player's next new action. Never merge the settled "
    "action with the future intent, resolve the future intent early, invent Player behavior, emit "
    "an [hp] tag, or add an uncommitted status, movement, target, or second action."
)
SETTLED_ENEMY_ACTION_OUTPUT_LIMIT = (
    "[AETHER P0 SETTLED ENEMY ACTION OUTPUT SHAPE — INPUT ONLY] [ENEMY ACTION] is the enemy "
    "attack that code already resolved on this turn. Narrate its exact move, hit/miss or Brace "
    "result, sensory cause, and committed toll once. Do not omit, delay, or turn it back into a "
    "warning merely because the Player also acted. Narrate every current settled Player result "
    "exactly once as well, using [INIT] ordering when it is supplied. No following [ENEMY INTENT] "
    "is supplied in this packet: do not promise, telegraph, invent, or resolve a next enemy attack. "
    "Honor any exact combat-over directive. Never invent Player behavior, emit an [hp] tag, or add "
    "an uncommitted status, movement, target, or second action."
)
SETTLED_ATTACK_FIRST_INTENT_OUTPUT_LIMIT = (
    "[AETHER P0 SETTLED ATTACK + FIRST INTENT OUTPUT LIMIT — INPUT ONLY] Narrate only the "
    "current code-settled Player attack and its recorded qualitative impact, then use exactly "
    "the one complete enemy beat supplied by the current Enemy Intent. The enemy is not resetting, "
    "waiting, or losing its turn: its attack is already selected and committed, and this reply is "
    "the Player's response window before code resolves it after their next new Player action. Emit "
    "no [hp] tag and "
    "add no status or persistent contact. Add no other enemy motion, stance change, weapon motion, "
    "approach, attack, miss, or impact. The current Player settlement does not advance, disrupt, "
    "alter, deflect, interrupt, cancel, accelerate, or resolve the future enemy intent; only a "
    "later code receipt can do that."
)
SETTLED_PLAYER_RESULT_FIRST_INTENT_OUTPUT_LIMIT = (
    "[AETHER P0 SETTLED PLAYER RESULT + FIRST INTENT OUTPUT LIMIT — INPUT ONLY] Narrate every "
    "current asserted_settled Player result from NARRATOR REALIZATION exactly once, and add no "
    "mechanic or world change outside its settled_change_kinds. Then use exactly the one complete "
    "enemy beat supplied by the current Enemy Intent. The enemy is not resetting, waiting, or "
    "losing its turn: its attack is already selected and committed, and this reply is the Player's "
    "response window before code resolves it after their next new Player action. Add no other enemy "
    "motion, stance "
    "change, weapon motion, approach, attack, miss, or impact. The settled Player result does not "
    "advance, disrupt, alter, deflect, interrupt, cancel, accelerate, or resolve the future enemy "
    "intent; only a later code receipt can do that."
)

_DISTANCE_WORD = (
    r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred|a[ \t]+few|several)"
)
_AUTHORED_SEPARATION_RE = re.compile(
    rf"\b{_DISTANCE_WORD}[ \t]+(?:paces?|steps?|feet|foot|yards?|met(?:er|re)s?|miles?|"
    rf"kilomet(?:er|re)s?)(?:[ \t]+(?:ahead|away|apart|behind|distant|off))?\b",
    re.IGNORECASE,
)
_AUTHORED_SEPARATION_MARKER_RE = re.compile(
    r'Exact authored separation phrase: "([^"\r\n]{1,80})"', re.IGNORECASE,
)

_ST_GENERIC_NARRATOR = "next reply in a fictional chat between"
_LEGACY_NARRATOR_CONTRACT = "You are the Narrator — the voice of this world and everyone in it:"
_LEGACY_POST_HISTORY = "You are the Narrator: speak the world and its people, never the Player."


def ensure_narrator_envelope(doc: dict) -> tuple[dict, bool]:
    """Guarantee one canonical stable contract for a typed narrator request.

    Generated cards and the recommended ST preset already carry the same envelope. This final
    wire boundary also migrates older AetherState cards and removes ST's known character-chat
    boilerplate so saved frontend state cannot quietly restore the card-as-character premise.
    Unrelated system prompts (world info, persona, author notes, extension prompts) are preserved.
    """
    original = list(doc.get("messages") or [])
    out: list = []
    found = False
    for message in original:
        if not isinstance(message, dict) or message.get("role") != "system" \
                or not isinstance(message.get("content"), str):
            out.append(message)
            continue
        text = message["content"]
        stripped = text.strip()
        if stripped.startswith("[AETHERSTATE NARRATOR CONTRACT "):
            if not found:
                out.append({**message, "content": NARRATOR_ENVELOPE})
                found = True
            continue
        if stripped.lower().startswith("write ") \
                and _ST_GENERIC_NARRATOR in stripped.lower():
            continue
        if _LEGACY_NARRATOR_CONTRACT in text:
            prefix = text.split(_LEGACY_NARRATOR_CONTRACT, 1)[0].rstrip()
            if prefix:
                out.append({**message, "content": prefix})
            continue
        if stripped.startswith(_LEGACY_POST_HISTORY):
            continue
        out.append(message)
    if not found:
        out.insert(0, {"role": "system", "content": NARRATOR_ENVELOPE})
    changed = out != original
    return ({**doc, "messages": out} if changed else doc), changed


def _compact(cfg) -> bool:
    """Compression item 2 (2026-07-09): [injection].briefing_style == 'compact'. Verbose
    (default) renders byte-identically to 1.11 — the knob is opt-in by verified decision."""
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


# ---- RPG specialization blocks (the public contract; rendered only when specialization=rpg) -----
def _render_player(state: dict, cfg=None) -> str:
    """[PLAYER] — compact hot-field projection of the Player Card(s) (the public contract). Skills
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
                raw_name = r.get("name")
                label = (raw_name.strip() if isinstance(raw_name, str) and raw_name.strip()
                         else str(rname).replace("_", " ").title())
                head += f' · {label} {r.get("cur", r["max"])}/{r["max"]}'
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
                line = " ".join(                     # + equipped-gear mods (the public contract, RPG-2)
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
    grouped by container; `loose` renders last (the public contract). GEAR-class carried items render
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
    tracked character carrying one (the public contract). Fed back every turn so the narrator can't
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

# RPG-5 (the public contract): defeat outcome classes — code picks the class, the narrator flavors it.
_DEFEAT_PHRASE = {
    "captured": "overcome and taken by the victors; narrate the capture, not a rescue",
    "wake_safe": "knocked out of the fight; they come to somewhere safe, time having passed",
    "robbed": "beaten and stripped of their carried goods; the loss is real",
    "rescued": "downed until someone intervenes to pull them out",
    "death": "this is death, final and unsoftened — narrate it with the weight it deserves",
}


def _war_room_on(state: dict, cfg=None) -> bool:
    """Phase 1 gate: combat instances active + the war_room knob (default on)."""
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    if spec is None or spec.name != "rpg" or not getattr(spec, "war_room", True):
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


def _combat_opening_window(state: dict, cfg=None, signaled: bool = False) -> bool:
    """True only for the private worked-example window at the start of a real fight.

    `signaled` catches the request that crosses from prose into combat before a combat row exists.
    Once code has opened the War Room, `started_turn` keeps the primer beside the first settled
    exchange as well. Nothing is journaled: the window is derived from replayable combat state,
    and disabling the knob changes prompt context only.
    """
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    if spec is None or spec.name != "rpg" or not getattr(spec, "war_room", True) \
            or not getattr(spec, "combat_opening_primer", True):
        return False
    if signaled:
        return True
    combat = state.get("combat") or {}
    if not isinstance(combat, dict) or not combat.get("active"):
        return False
    turn = (state.get("meta") or {}).get("turn")
    started = combat.get("started_turn")
    if not isinstance(turn, int) or not isinstance(started, int):
        return False
    return started <= turn <= started + 1


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
    """Phase 1 (verified): ONE pre-rolled action die per ally per combat turn — the R8c
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


def _render_war(state: dict, *, qualitative_targets: frozenset[str] = frozenset()) -> str:
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
    for cid, r in rows.items():
        if not isinstance(r, dict):
            continue
        hp = r.get("hp") or {}
        label = combatant_label(r, str(cid))
        if cid in qualitative_targets:
            t = f"{label} (state supplied by NARRATOR REALIZATION)"
        else:
            t = f"{label} {int(hp.get('cur', 0))}/{int(hp.get('max', 1))}"
        if r.get("armament"):
            t += f" ({r['armament']})"
        if r.get("tier") and r.get("tier") != "standard":
            t += f" [{r['tier']}]"
        if r.get("defeated"):
            t = f"☠ {label} DOWN"
        (foes if r.get("side") == "enemy" else allies).append(t)
    bits = []
    if foes:
        bits.append("foes: " + ", ".join(foes))
    if allies:
        bits.append("allies: " + ", ".join(allies))
    return f"[WAR] round {rnd} — " + " · ".join(bits) if bits else ""


def _opposition_roll(state: dict) -> tuple[int, int]:
    """Compatibility seam for HUD/tests; Tier-0 now owns and journals opposition."""
    from .tier0 import opposition_roll
    return opposition_roll(state)


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
        order.append((init, combatant_label(r, str(cid)), str(r.get("side", "enemy"))))
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
    cohort = battle_cohort_status(state)
    if cohort is not None:
        return (f"[BATTLE] {name}: the wider fight is {tide.upper()} for you"
                + (f" (wave {waves})" if waves else "")
                + f". FINITE COHORT {cohort['name']}: {cohort['active']} active, "
                  f"{cohort['defeated']} defeated, {cohort['queued']} queued of "
                  f"{cohort['total']}. Each numbered row is one ordinary actor with its own HP; "
                  "only the single supplied enemy intent may act. The count never authorizes "
                  "shared damage, simultaneous attacks, or off-ledger casualties. Narrate the "
                  "wider field in prose and report shifts with "
                  "[tide | winning|holding|losing | why]; the engine admits queued actors and "
                  "ends this finite cohort.")
    tail = {"winning": "the tide is turning YOUR way — hold and the field is won",
            "holding": "the line holds — the outcome is still open",
            "losing": "you are being pushed back — fresh waves keep coming until it turns"}[tide]
    return (f"[BATTLE] {name}: the wider fight is {tide.upper()} for you"
            + (f" (wave {waves})" if waves else "")
            + f". {tail}. Your War Room slice is the dice; the rest of the field is yours to "
              "NARRATE in prose — report any shift with [tide | winning|holding|losing | why]; "
              "the engine sends the waves and decides when it ends.")


def _authored_separation(state: dict, actor_name: str = "", player_name: str = "") -> str:
    """Newest exact distance phrase already committed in prose evidence, if any.

    First-intent narration must not turn an abstract move range into current spatial truth.  The
    Creator opening is retained as a turn-0 memory, so surface its literal phrase only when that
    recent opening evidence explicitly names this Player and this enemy.  A distance from some
    other memory, pair of actors, or older scene is never current spatial authority.
    """
    memories = state.get("memories") or {}
    rows = memories.values() if isinstance(memories, dict) else memories
    candidates = [row for row in rows if isinstance(row, dict)]
    candidates.sort(key=lambda row: int(row.get("turn", -1)), reverse=True)
    turn = (state.get("meta") or {}).get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool):
        return ""
    if not player_name:
        peid = next(iter(state.get("player") or {}), "")
        player_name = _name(state, peid) if peid else ""
    actor_name = str(actor_name or "").strip()
    player_name = str(player_name or "").strip()
    if not actor_name or not player_name:
        return ""

    def mentions(text: str, name: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None

    for row in candidates:
        evidence_turn = row.get("turn")
        if not isinstance(evidence_turn, int) or isinstance(evidence_turn, bool) \
                or evidence_turn < turn - 1 or evidence_turn > turn:
            continue
        text = str(row.get("text") or "")
        if not mentions(text, actor_name) or not mentions(text, player_name):
            continue
        match = _AUTHORED_SEPARATION_RE.search(text)
        if match:
            return match.group(0)
    return ""


def render_deterministic_first_intent_text(
    actor_label: object,
    tell: object,
    *,
    separation: object = "",
) -> str:
    """Render the one canonical pre-impact response-window sentence pair."""
    actor = str(actor_label or "").strip()
    visible_tell = str(tell or "").strip()
    exact_separation = str(separation or "").strip()
    if not actor or not visible_tell:
        return ""
    subject = f"{actor}, {exact_separation}" if exact_separation else actor
    story = f"{subject}: {visible_tell}"
    if not story.endswith((".", "!", "?")):
        story += "."
    return f"{story} {DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT}"


def deterministic_first_intent_components(state: dict) -> dict | None:
    """Return exact state-bound components for a newly staged first enemy intent."""
    from .enemy_kits import intent_matches_frozen_kit

    combat = state.get("combat") or {}
    intent = combat.get("pending_intent")
    rows = combat.get("combatants") or {}
    row = rows.get(str((intent or {}).get("actor"))) if isinstance(intent, dict) else None
    players = state.get("player") or {}
    peid = next(iter(players), "")
    pname = _name(state, peid) if peid else ""
    turn = (state.get("meta") or {}).get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool) \
            or not isinstance(row, dict) or row.get("side") != "enemy" \
            or row.get("defeated") or row.get("spawned_turn") != turn \
            or not intent_matches_frozen_kit(intent, row) \
            or intent.get("target") != peid or intent.get("target_name") != pname \
            or intent.get("prepared_turn") != turn:
        return None

    base_actor = str(intent.get("actor_name") or "").strip()
    actor = combatant_label(row, base_actor)
    tell = str(intent.get("tell") or "").strip()
    if not actor or not tell:
        return None
    separation = _authored_separation(state, base_actor, pname)
    visible_text = render_deterministic_first_intent_text(
        actor,
        tell,
        separation=separation,
    )
    if not visible_text:
        return None
    return {
        "actor_id": str(intent["actor"]),
        "actor_label": actor,
        "target_id": str(intent["target"]),
        "target_label": pname,
        "move_id": str(intent["move_id"]),
        "move_label": str(intent.get("move_name") or intent["move_id"]).strip(),
        "tell": tell,
        "separation": separation,
        "response_window": DETERMINISTIC_FIRST_INTENT_RESPONSE_WINDOW,
        "response_window_text": DETERMINISTIC_FIRST_INTENT_RESPONSE_TEXT,
        "visible_text": visible_text,
    }


def deterministic_first_intent_story(state: dict) -> str:
    """Return the complete code-owned prose for a newly staged enemy intent.

    This is deliberately smaller than ``_render_enemy_intent``.  It exposes only three already
    committed facts: the frozen enemy name, the newest literal authored separation (when one
    exists), and the frozen move's exact visible tell.  An empty string means the state is not a
    valid first-intent boundary and the caller must fail open to ordinary upstream narration.
    """
    components = deterministic_first_intent_components(state)
    return components["visible_text"] if components is not None else ""


def _render_enemy_intent(state: dict) -> str:
    """Render the one frozen future enemy move as a choice surface, never an impact."""
    from .enemy_kits import intent_matches_frozen_kit

    combat = state.get("combat") or {}
    intent = combat.get("pending_intent")
    rows = combat.get("combatants") or {}
    row = rows.get(str((intent or {}).get("actor"))) if isinstance(intent, dict) else None
    peid = next(iter(state.get("player") or {}), "")
    pname = _name(state, peid) if peid else ""
    turn = (state.get("meta") or {}).get("turn", -1)
    if not isinstance(row, dict) or row.get("side") != "enemy" or row.get("defeated") \
            or not intent_matches_frozen_kit(intent, row) \
            or intent.get("target") != peid or intent.get("target_name") != pname \
            or intent.get("prepared_turn") != turn:
        return ""
    if row.get("spawned_turn") == turn:
        exact_story = deterministic_first_intent_story(state)
        if not exact_story:
            return ""
        separation = _authored_separation(
            state, str(intent.get("actor_name") or ""), pname,
        )
        separation_rule = (
            f' Exact authored separation phrase: "{separation}". It already appears in the exact '
            "beat; do not repeat, replace, quantify, reinterpret, or change it."
            if separation else
            " No exact authored separation is available; omit distance entirely."
        )
        return (
            "[ENEMY INTENT enemy-intent/1] ENGINE-ONLY INPUT — NEVER QUOTE, COPY, OR EMIT THIS "
            "HEADER. PENDING ONLY — DO NOT RESOLVE. TURN CADENCE: this is not passive readiness, "
            "a reset, or a skipped enemy turn. The attack is already selected and committed. This "
            "reply is the Player's response window; after their next new Player action, code "
            "resolves the attack. Make the supplied beat read as immediate danger, then stop "
            "before impact. "
            "FIRST-INTENT CONTINUITY: Preserve every "
            "authored/current distance exactly. The move's abstract range category describes its "
            "eventual delivery, not current separation or permission to close. Only the exact "
            "Visible tell in the complete beat below authorizes enemy preparation. Never infer "
            "or extend the tell into a step, approach, range change, weight transfer, additional "
            "weapon motion, attack, miss, impact, or continuing advance. "
            + separation_rule + " "
            f"Exact complete enemy prose: {json.dumps(exact_story, ensure_ascii=False)} Use that "
            "complete beat once as written; it is the entire authorized enemy beat. The current "
            "Player settlement does not advance, disrupt, alter, deflect, interrupt, cancel, "
            "accelerate, or resolve this future intent. The future intent remains unchanged and pending until a later "
            f"code receipt. Intent id {intent.get('id', '?')} has not resolved: no hit, damage, "
            "condition, forced movement, or other impact happens now."
        )
    counters = "; ".join(str(x) for x in (intent.get("counterplay") or []) if str(x).strip())
    reaction = intent.get("reaction") if isinstance(intent.get("reaction"), dict) else None
    brace = (
        " Engine reaction available: BRACE — devote the whole Player action by replying "
        "'I brace.' (terminal punctuation is optional); it halves this move's committed HP damage "
        "(rounding down)."
        if reaction and reaction.get("kind") == "brace" else
        " No engine reaction is attached to this move."
    )
    return (
        "[ENEMY INTENT enemy-intent/1] ENGINE-ONLY INPUT — NEVER QUOTE, COPY, OR EMIT THIS "
        "HEADER; TRANSLATE IT INTO IN-FICTION PROSE. PENDING ONLY — DO NOT RESOLVE. TURN CADENCE: "
        "this is not passive readiness, a reset, or a skipped enemy turn. The attack is already "
        "selected and committed. This reply is the Player's response window; after their next new "
        "Player action, code resolves the attack. Make the tell read as immediate danger, then stop before "
        "impact. PENDING MOVE: "
        f"{combatant_label(row, str(intent.get('actor_name') or intent.get('actor') or 'Enemy'))} "
        "is committing "
        f"{intent.get('move_name', 'an attack')} against "
        f"{intent.get('target_name', intent.get('target', 'the Player'))} via "
        f"{intent.get('delivery', 'grounded force')} "
        f"({intent.get('basis', 'physical')}; {intent.get('range', 'close')} range; "
        f"{intent.get('timing', 'fast')} timing; {intent.get('cadence', 'reliable')} cadence; "
        f"{intent.get('danger', 'moderate')} danger). "
        f"Visible tell: {intent.get('tell', 'the attack lines up')}. "
        "Pre-impact evidence is the visible tell above; reserve the frozen sensory causality "
        "for the later action receipt and do not narrate its impact now. "
        f"Fictional openings the Player may exploit in their own stated action: "
        f"{counters or 'break the direct line'}; these are not automatic engine reactions."
        + brace + " "
        f"Commitment risk: {intent.get('risk', 'none')}. "
        f"Hard limit: {intent.get('forbid', 'no unlisted effects or extra targets')}. "
        f"Intent id {intent.get('id', '?')} has not resolved: no hit, damage, condition, forced "
        "movement, or other impact happens now. Make the commitment perceptible before impact "
        "and stop with room for the Player to respond; do not choose their reaction."
    )


def _current_narration_overlay_state(state: dict, cfg=None) -> tuple[bool, bool, bool, bool, bool]:
    """Return state-owned transition flags for all three narration modes.

    Production routing must not depend on matching prose in the rendered packet. The first flag
    comes from the exact frozen intent boundary, Player-settlement flags come only from a validated
    narrator-realization projection, and the final flag comes from a current code-owned opposition
    receipt. The fifth flag independently proves whether a following intent is currently valid.
    Malformed state fails closed to the ordinary lane.
    """
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    if spec is None or spec.name != "rpg":
        return False, False, False, False, False
    first_intent = False
    settled_enemy_action = False
    pending_enemy_intent = False
    enemy_runtime = getattr(spec, "war_room", True) and getattr(spec, "enemy_rolls", True)
    if enemy_runtime:
        try:
            first_intent = bool(deterministic_first_intent_story(state))
        except (KeyError, RuntimeError, TypeError, ValueError):
            first_intent = False
        try:
            pending_enemy_intent = bool(_render_enemy_intent(state))
        except (KeyError, RuntimeError, TypeError, ValueError):
            pending_enemy_intent = False
        players = state.get("player") if isinstance(state, dict) else None
        peid = next(iter(players), "") if isinstance(players, dict) else ""
        player = players.get(peid) if peid else None
        transient = state.get("_reserved_opposition") if isinstance(state, dict) else None
        committed = transient if isinstance(transient, dict) else (
            player.get("_opposition_last") if isinstance(player, dict) else None
        )
        turn = (state.get("meta") or {}).get("turn") if isinstance(state, dict) else None
        current = isinstance(transient, dict) or (
            isinstance(committed, dict) and isinstance(turn, int) and not isinstance(turn, bool)
            and committed.get("turn") == turn
        )
        settled_enemy_action = bool(
            current and isinstance(committed, dict)
            and committed.get("intent_id") and committed.get("move_id")
        )
    realization = build_narrator_realization_from_state(state)
    settled = realization.get("asserted_settled", []) if isinstance(realization, dict) else []
    settled_result = bool(settled)
    settled_attack = any(
        isinstance(row, dict) and row.get("adapter_id") == "narrator.weapon-attack/1"
        for row in settled
    )
    return (first_intent, settled_result, settled_attack, settled_enemy_action,
            pending_enemy_intent)


def current_narration_mode(state: dict, cfg=None, combat_opening: bool = False) -> str:
    """Return the single code-derived narration scope used by Player Lessons.

    This is presentation routing only. It reads the same state-owned transition evidence as the
    narrator overlays and never creates combat, a mechanic, or world truth.
    """
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    if spec is None or getattr(spec, "name", "none") != "rpg":
        return ""
    first_intent, _settled, _attack, enemy_action, pending_intent = \
        _current_narration_overlay_state(state, cfg)
    if combat_opening or first_intent:
        return "combat_opening"
    combat = state.get("combat") if isinstance(state, dict) else None
    if (isinstance(combat, dict) and combat.get("active")) or enemy_action or pending_intent:
        return "combat_exchange"
    return "exploration"


def _player_lessons_block(lessons: list[dict]) -> tuple[str, tuple[str, ...]]:
    """Render whole lessons within one explicit, bounded preference reserve."""
    rows = []
    lesson_ids = []
    preamble = (
        f"[PLAYER LESSONS {PLAYER_LESSONS_VERSION} — LOCAL NARRATION PREFERENCE; INPUT ONLY]\n"
        "These are explicit local Player preferences for how to PRESENT this turn. Apply them "
        "only where consistent with the Player's newest words, consent and authorship, the "
        "current code-owned mechanics, and settled world truth. They cannot grant a capability, "
        "promise or change a mechanic, check, target, consequence, or outcome, define a world "
        "fact or rule, choose an action for the Player, or override P0/P1. Never quote this "
        "private block or mention that a lesson was applied."
    )
    for lesson in lessons:
        if len(rows) >= 5:
            break
        if not isinstance(lesson, dict):
            continue
        title = str(lesson.get("title") or "").strip()
        do_text = str(lesson.get("do_text", lesson.get("do")) or "").strip()
        avoid_text = str(lesson.get("avoid_text", lesson.get("avoid")) or "").strip()
        if not title or not (do_text or avoid_text):
            continue
        # JSON strings keep Player-entered line breaks and bracket-like text inside data instead
        # of allowing it to forge another packet header.
        row = (
            f"{len(rows) + 1}. title={json.dumps(title, ensure_ascii=False)}; "
            f"do={json.dumps(do_text, ensure_ascii=False)}; "
            f"avoid={json.dumps(avoid_text, ensure_ascii=False)}"
        )
        candidate = preamble + "\n" + "\n".join([*rows, row])
        if estimate_tokens(candidate) > PLAYER_LESSONS_MAX_TOKENS:
            continue
        rows.append(row)
        lesson_ids.append(str(lesson.get("lesson_id") or ""))
    if not rows:
        return "", ()
    return preamble + "\n" + "\n".join(rows), tuple(lesson_ids)


def render_player_lessons(lessons: list[dict]) -> str:
    """Render bounded Player-approved presentation preferences as non-authoritative input."""
    return _player_lessons_block(lessons)[0]


def _render_enemy_action(committed: dict, player_name: str) -> str:
    """Render one already-journaled enemy move without allowing a reroll or invented reaction."""
    try:
        total = int(committed.get("total", 0))
        total_raw = int(committed.get("total_raw", total))
        damage = max(0, int(committed.get("damage", 0)))
    except (TypeError, ValueError, OverflowError):
        return ""
    tier = str(committed.get("tier", "MISSES"))
    actor = str(committed.get("actor_name") or committed.get("actor") or "Enemy")
    move = str(committed.get("move_name") or committed.get("move_id") or "committed attack")
    target = str(committed.get("target_name") or player_name)
    reaction = committed.get("reaction") if isinstance(committed.get("reaction"), dict) else None
    brace = bool(reaction and reaction.get("kind") == "brace" and reaction.get("applied"))
    try:
        damage_before = max(0, int(committed.get("damage_before", damage)))
    except (TypeError, ValueError, OverflowError):
        damage_before = damage
    if brace and damage_before:
        outcome = (
            f"The Player explicitly spent their whole action on Brace. It {tier}; Brace halves "
            f"the normal {damage_before} HP to exactly {damage} HP, saving "
            f"{damage_before - damage}, all already committed. {target}'s HP immediately after "
            f"this impact was {committed.get('hp_cur', '?')}/{committed.get('hp_max', '?')}; use "
            "[PLAYER] and defeat state for any later post-fallout/current HP. Narrate the stated "
            "brace absorbing that exact amount once and emit no [hp] tag"
        )
    elif damage:
        outcome = (
            f"It {tier} and deals exactly {damage} HP damage, already committed; "
            f"{target}'s HP immediately after this impact was {committed.get('hp_cur', '?')}/"
            f"{committed.get('hp_max', '?')}; use [PLAYER] and defeat state for any later "
            "post-fallout/current HP. Narrate that exact toll once and emit no [hp] tag"
        )
    else:
        outcome = (
            f"It {tier} and commits 0 damage. Cause the failure through {actor}'s own aim, "
            "footing, timing, delivery, or the environment. Do not invent the Player dodging, "
            "parrying, blocking, twisting, ducking, sidestepping, moving, or acting in any way"
        )
        if brace:
            outcome += (". The Player did state Brace as their whole action, but the attack "
                        "already committed 0 before mitigation; do not credit Brace with a miss")
    simult = (
        " The move was already committed and in motion when the Player acted this turn; if its "
        "actor also fell this turn, this one launched action still resolves, but no new action "
        "comes from that defeated actor."
        if committed.get("simultaneous") else ""
    )
    reserved = ""
    if committed.get("settled_retry") or committed.get("lost_reply"):
        why = ("the earlier delivery of this same reply was lost" if committed.get("lost_reply")
               else "the Player regenerated narration for this same settled turn")
        reserved = (f" ALREADY SETTLED: {why}. Re-serve this exact action in the answer now; "
                    "it is not a new action, reroll, or second impact.")
    return (
        "[ENEMY ACTION enemy-action/1] ENGINE-ONLY INPUT — NEVER QUOTE, COPY, OR EMIT THIS "
        "HEADER; TRANSLATE IT INTO IN-FICTION PROSE. RESOLVE THIS SETTLED MOVE ONLY: "
        f"{actor} used {move} against {target} via "
        f"{committed.get('delivery', 'grounded force')} "
        f"({committed.get('basis', 'physical')}; {committed.get('range', 'close')} range; "
        f"{committed.get('timing', 'fast')} timing; "
        f"{committed.get('cadence', 'reliable')} cadence). Code roll: 2d6 raw={total_raw}; "
        f"adjusted result={total}; {outcome}. "
        f"Sensory causality: {committed.get('sensory', 'movement and impact')}. "
        f"Exact intent id: {committed.get('intent_id', committed.get('id', '?'))}. "
        f"Commitment risk: {committed.get('risk', 'none')}. "
        f"Hard limit: {committed.get('forbid', 'no unlisted effects or extra targets')}. "
        "Do not substitute a different move, delivery, target, spell, weapon, status, area effect, "
        "reroll, extra action, or Player behavior."
        + simult + reserved
    )


def _semantic_action_for_receipt(state: dict, ref: object) -> str:
    """Compact narrator-facing identity from an already admitted semantic receipt."""
    if not isinstance(ref, str) or not ref:
        return ""
    rows = state.get("semantic_frames")
    if not isinstance(rows, list):
        return ""
    frame = next(
        (
            row.get("frame") for row in reversed(rows)
            if isinstance(row, dict)
            and isinstance(row.get("frame"), dict)
            and row["frame"].get("fingerprint") == ref
        ),
        None,
    )
    if not isinstance(frame, dict) or frame.get("ambiguity"):
        return ""
    actor_id = str(frame.get("actor_id") or "")
    capability = str(frame.get("capability_id") or "")
    action = str(frame.get("action_class") or "").replace("_", " ")
    if not actor_id or not capability or not action:
        return ""
    bits = [f"{_name(state, actor_id)} / {capability} / {action}"]
    target_id = str(frame.get("target_entity_id") or "")
    target = str(frame.get("target_name") or "")
    if target_id or target:
        bits.append(f"target {target or _name(state, target_id)}")
    possessed = str(frame.get("possessed_object") or "")
    if possessed:
        owner_id = str(frame.get("possessed_object_owner_id") or "")
        part = str(frame.get("possessed_object_part") or "")
        owned = f"{_name(state, owner_id)}'s " if owner_id else ""
        bits.append(f"object {owned}{possessed}" + (f" {part}" if part else ""))
    locus = str(frame.get("target_locus") or "")
    if locus:
        bits.append(f"locus {locus}")
    return "CANONICAL ACTION: " + "; ".join(bits) \
        + ". Do not reassign its actor, capability, target, possession, or locus"


def _render_directive(state: dict, cfg=None) -> str:
    """[DIRECTIVE] — the pre-decided outcome(s) of THIS turn's check(s) (the public contract/§5.2). Reads
    every `check` record for the current turn (checks reuse the rolls buffer, the public contract), so a
    multi-check turn directs each result — no silently unnarrated resolution. Rides the
    never-dropped header so the resolve-then-narrate contract can't be budget-cut."""
    turn = state.get("meta", {}).get("turn", -1)
    v3_authority = current_v3_semantic_authority(state)
    if "_fresh_checks" in state:                 # 2026-07-09: exactly THIS request's checks —
        checks = [c for c in state["_fresh_checks"] if c.get("tier")]   # reliable, never stale
    else:                                        # replay / no-pipeline fallback: turn-scoped
        checks = [r for r in state.get("rolls", []) if r.get("turn") == turn and r.get("tier")]
    retry = state.get("_settled_retry")
    if v3_authority:
        # V3 owns current Player mechanics even when one reachable dependency is corrupt.  A
        # failed canonical projection must never reactivate numeric check/damage narration. Retry
        # delivery status is sealed inside narrator-realization/1, never reconstructed here.
        checks = []
    clauses = []
    if isinstance(retry, dict) and not v3_authority:
        why = ("the earlier reply was lost in transit" if retry.get("kind") == "lost_reply"
               else "the Player regenerated narration for this settled turn")
        families = [str(value).replace("_", " ") for value in (retry.get("families") or [])[:8]]
        owned = ", ".join(families) if families else "the mechanically empty Player action"
        clauses.append(
            f"ALREADY SETTLED because {why}: {owned}. Answer the same Player action now, but "
            "do not reroll, repeat a cost or cooldown, advance time again, recommit damage, or "
            "treat this as a second action")
        if state.get("_fresh_foe_retry"):
            clauses.append(
                "FRESH-FOE NARRATION RETRY: rewrite only the original introduction. Show identity, "
                "presence, equipment, hostility, and general readiness, then stop. Do not reveal "
                "or telegraph the code-frozen future move that exists after that original reply")
    for c in checks:
        tier = str(c.get("tier"))
        skill = str(c.get("skill") or "the")
        phrase = _DIRECTIVE_PHRASE.get(tier, tier)
        clause = f"{phrase} — the {skill} check resolved as {tier.upper()}"
        semantic_action = _semantic_action_for_receipt(
            state, c.get("_semantic_frame_ref"))
        if semantic_action:
            clause += f" ({semantic_action})"
        if c.get("dm_called") or c.get("_dm_called"):
            # Compatibility with old saved rolls. Current narration has no check authority.
            clause += " (legacy pre-settled engine check — narrate only its recorded result)"
        if c.get("settled_retry") or c.get("lost_reply"):
            why = ("your reply was lost in transit" if c.get("lost_reply") else
                   "the Player regenerated narration for the same settled turn")
            clause += (f" (ALREADY SETTLED: {why} — this is that roll re-served, not a new "
                       "or repeated one; answer the Player's message with it now)")
        bl = c.get("_ability_blocked")         # Arinvale: a declared active that didn't ride
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
        if tgt and isinstance(dmg, int) and not v3_authority:
            if dmg > 0:
                clause += (f" — the blow lands on {tgt} for {dmg} damage (already committed "
                           f"to the ledger; narrate that exact toll, no more, no less; do not emit "
                           "an [hp] tag for it")
                cid = resolve_combatant(state, tgt)
                row = ((state.get("combat") or {}).get("combatants") or {}).get(cid or "") or {}
                hp = row.get("hp") if isinstance(row, dict) else None
                if isinstance(hp, dict) and row.get("_struck_turn") == turn:
                    clause += (f"; current [WAR] HP is {hp.get('cur', '?')}/{hp.get('max', '?')} "
                               "and ALREADY INCLUDES this hit — do not subtract it again or emit "
                               "another [hp] tag for this committed strike")
                clause += ")"
            else:
                clause += f" — the attempt on {tgt} draws no blood"
        clauses.append(clause)
    for eid, p in (state.get("player") or {}).items():   # RPG-5 (the public contract): a defeat this
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
        if v3_authority:
            out = (
                "[DIRECTIVE] NARRATE CODE-SETTLED FALLOUT: " + "; ".join(clauses)
                + ". Narrate each listed fallout exactly once alongside every current "
                "asserted_settled Player result from NARRATOR REALIZATION, if any. The Player "
                "settlement and autonomous opposition fallout are separate occurrences unless "
                "exact occurrence references link them. Autonomous [ENEMY ACTION] fallout, "
                "including defeat resolution and combat end, does not cancel, replace, undo, or "
                "become the cause of the current Player settlement, and does not imply that "
                "settlement failed or did not happen. Do not soften, upgrade, or override any "
                "code-settled result. The decisions are complete: do not reconsider the skill, "
                "turn, outcome, or alternate possibilities; plan only the concrete consequences "
                "and answer immediately."
            )
        else:
            what = "these outcomes" if len(clauses) > 1 else "this outcome"
            out = ("[DIRECTIVE] NARRATE: " + "; ".join(clauses) + f". Narrate exactly {what}; "
                   "do not soften, upgrade, or override the result of a roll. This directive "
                   "always resolves the Player's NEWEST message — never an earlier one. The "
                   "decision is complete: do not reconsider the skill, turn, outcome, or alternate "
                   "possibilities; plan only the concrete consequence and answer immediately.")
    if not out and state.get("_turn_guidance") == "free_narration":
        out = ("[DIRECTIVE] CURRENT TURN: FREE NARRATION — the Player's NEWEST message is "
               "dialogue, a question, a negated reference, or another certain social beat; no "
               "roll is due. Answer it directly in fiction. Every earlier directive is stale: "
               "do not reuse it or deliberate whether it still applies. No mechanical analysis "
               "is needed; make one short continuity plan and answer immediately.")
    elif not out and state.get("_turn_guidance") == "combat_opening":
        out = ("[DIRECTIVE] CURRENT TURN: CODE-OWNED COMBAT OPENING — the Player deliberately "
               "brought the known on-scene opponent into combat without resolving an attack. "
               "Narrate only the Player's stated movement, equipment, words, and held position. "
               "The [WAR] combatant already exists: do not emit a duplicate [foe] tag. Translate "
               "the exact pending [ENEMY INTENT] below into its pre-impact tell and stop with room "
               "for the Player to answer. Do not add another enemy move, range change, impact, "
               "damage, condition, or Player reaction.")
    elif not out and state.get("_turn_guidance") == "unresolved":
        out = ("[DIRECTIVE] CURRENT TURN: UNRESOLVED PLAYER ACTION — no roll resolved the Player's "
               "NEWEST message. Every earlier directive is stale. You have no authority to call, "
               "emit, or simulate a check. Narrate only the immediate fictional setup or pressure; "
               "do not invent a mechanically significant success, failure, discovery, consequence, "
               "or outcome ledger tag for the Player's unresolved action. This limit does not "
               "suppress, postpone, or contradict an exact code-owned [ENEMY ACTION] appended "
               "below; narrate that already-settled enemy move exactly once. Decide once, briefly; "
               "do not deliberate over dice or skills.")
    kn = state.get("_kill_note")             # 2026-07-10 (Bean): out-of-combat kill outcome —
    if kn:                                   # stealth kill, grand working, or a routed NON-MOVE
        out = (out + "\n" + kn) if out else ("[DIRECTIVE] " + kn)
    # R8c — the enemy acts on real dice too (rendered whenever a non-player is on scene;
    # the DM is told to ignore it in peaceful beats). Code rolls, the model narrates.
    # Phase 1 rides here too: with the War Room active, ally dice + the exact-HP board
    # join the volatile tail (enemy_rolls only gates the [OPPOSITION] die itself).
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    enemy_rolls = bool(spec is not None and spec.name == "rpg"
                       and getattr(spec, "enemy_rolls", True))
    if cfg is not None and (state.get("player") or {}):
        peid = next(iter(state["player"]), "")
        pname = _name(state, peid)
        # A swipe of the foe-introduction reply preserves its rule-owned spawn and frozen intent,
        # but must regenerate the same pre-intent story boundary. Hide the later War tail only from
        # this replacement request; the ledger and HUD remain unchanged.
        war = _war_room_on(state, cfg) and not state.get("_fresh_foe_retry")
        player = (state.get("player") or {}).get(peid) or {}
        transient = state.get("_reserved_opposition")
        committed = transient if isinstance(transient, dict) else (
            player.get("_opposition_last") if isinstance(player, dict) else None)
        current_action = isinstance(transient, dict) or (
            isinstance(committed, dict) and committed.get("turn") == turn)
        if enemy_rolls and isinstance(committed, dict) and current_action:
            if committed.get("intent_id") and committed.get("move_id"):
                actor_row = (((state.get("combat") or {}).get("combatants") or {})
                             .get(str(committed.get("actor") or "")))
                visible_committed = ({**committed,
                                      "actor_name": combatant_label(
                                          actor_row, str(committed.get("actor_name") or "Enemy"))}
                                     if isinstance(actor_row, dict) else committed)
                opp = _render_enemy_action(visible_committed, pname)
            else:
                total = int(committed.get("total", 0))
                tier = str(committed.get("tier", "MISSES"))
                damage = max(0, int(committed.get("damage", 0)))
                if damage:
                    effect = (f"It lands on {pname} for {damage} damage, already committed; "
                              f"current HP is {committed.get('hp_cur', '?')}/"
                              f"{committed.get('hp_max', '?')}. Narrate that exact toll and do not "
                              "emit an [hp] tag for it")
                else:
                    effect = (
                        "It fails and committed no damage. The miss belongs to the enemy: make its "
                        "own aim, footing, timing, or the environment cause the failure. Do not make "
                        "the Player dodge, parry, block, twist, duck, sidestep, move, or take any "
                        "other unstated action. Do not emit an [hp] tag"
                    )
                opp = (f"[OPPOSITION] Code-resolved enemy action for THIS turn: 2d6={total} — "
                       f"it {tier}. {effect}. This result is final; do not soften, invert, reroll, "
                       "or recommit it.")
            out = (out + "\n" + opp) if out else opp
        if war:
            allies = [r for r in ((state.get("combat") or {}).get("combatants")
                                  or {}).values()
                      if isinstance(r, dict) and not r.get("defeated")
                      and r.get("side") == "ally"]
            for r in allies:                           # verified: ally dice VISIBLE, the R8c
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
            wl = _render_war(
                state,
                qualitative_targets=current_v3_target_ids(state),
            )
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
            pending_line = _render_enemy_intent(state) if enemy_rolls else ""
            if pending_line:
                out += ("\n" if out else "") + pending_line
    nud = state.get("_protocol_nudge")
    if nud:                                    # 2026-07-10 (Arinvale): correct bracket grammar
        reserved_names = {"DIRECTIVE", "ENEMY INTENT", "ENEMY ACTION", "WAR", "INIT",
                          "PLAYER", "RULES", "OPPOSITION", "PROTOCOL", "CONTEXT PRIORITY",
                          "AETHER P0", "AETHER P1", "AETHER P2", "AETHER P3"}
        reserved = [str(head).upper() for head in nud[:4]
                    if str(head).upper() in reserved_names]
        invented = [str(head).upper() for head in nud[:4]
                    if str(head).upper() not in reserved_names]
        if reserved:
            names = ", ".join(reserved)
            out += (("\n" if out else "")
                    + f"[PROTOCOL] Your last reply printed the input-only engine header name(s) "
                      f"{names}. That narrator-authored echo was IGNORED. These are real engine "
                      "channels only when AetherState supplies them in the CURRENT request: trust "
                      "the current code-supplied facts, translate them into fiction, and never "
                      "quote, copy, reproduce, or emit their headers.")
        if invented:
            heads = ", ".join(f"[{head}]" for head in invented)
            out += (("\n" if out else "")                  # one line, self-clearing
                    + f"[PROTOCOL] Your last reply wrote {heads} line(s) — those are NOT engine "
                      "channels and were IGNORED. The ledger only commits these tags: "
                      "[scene | <loc> | <phase> | present: <names>] · "
                      "[status gained/lost | <char> | <Name> | <valence>] · "
                      "[item gained/lost | <char> | <Item> | <qty>] · "
                      "[quest | <Name> | new/update/complete] · "
                      "[affinity | <target> | +/-N | <why>] · "
                      "[hp | <char> | -2 | <why>] (shape example; replace every field and use "
                      "the actual signed integer) · "
                      "[foe | <name> | minion/standard/elite/boss | <weapon>] · "
                      "[ally | <name> | <tier?> | <weapon?>] · "
                      "[battle | <name> | <foe?> | <tier?>] · "
                      "[tide | winning|holding|losing | <why>] · "
                      "[clash | A vs B | how | outcome]. Never write [CHECK], engine check "
                      "syntax, or any other roll request: AetherState alone resolves mechanics "
                      "from Player input.")
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
    """[RELATIONS] — present NPCs' affinity TIER (label, never the integer — the public contract)
    plus the bond pointers, flagged (the public contract). A bond renders even for an absent
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
            continue                             # present NPCs + bonded ones (the public contract)
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
    """0b (anti-main-character, the mechanics contract): how an NPC knows the Player, read from the
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
    """[FACTIONS] — faction -> affinity tier LABEL + standing circumstances (the public contract).
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
    """[WORLD] — active global flags/circumstances (world_flag ops; the public contract)."""
    w = state.get("world") or {}
    if not isinstance(w, dict) or not w:
        return ""
    return "[WORLD] " + " · ".join(f"{k}={_flag_str(v)}" for k, v in w.items())


def _render_living_tail(state: dict, cfg=None) -> str:
    """Phase 2 (the mechanics contract, verified): the living-world lines — volatile, so they ride
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
_NARRATOR_REALIZATION_FAIL_CLOSED = (
    "[DIRECTIVE] NARRATOR REALIZATION UNAVAILABLE — current V3 semantic authority did not "
    "validate completely. Do not narrate the Player performing or attempting the semantic "
    "action. Do not narrate a roll, damage, HP change, completed effect, object ownership, or "
    "other mechanical outcome from legacy state."
)


def render_header(state: dict, cfg) -> str:
    rpg = (
        getattr(cfg, "specialization", None) is not None
        and cfg.specialization.name == "rpg"
    )
    v3_authority = current_v3_semantic_authority(state)
    realization_line = ""
    if rpg:
        realization = build_narrator_realization_from_state(state)
        if realization is not None:
            realization_line = render_narrator_realization(realization)
        elif v3_authority:
            realization_line = _NARRATOR_REALIZATION_FAIL_CLOSED
    if is_empty(state) and not realization_line:
        return ""
    lines: list[str] = [realization_line] if realization_line else []
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
    rolls = (state.get("_fresh_rolls") if "_fresh_rolls" in state else
             [r for r in state.get("rolls", []) if r.get("turn") == turn and "spec" in r])
    if v3_authority:
        rolls = []
    if rolls:
        rendered = []
        for roll in rolls:
            line = f'{roll["spec"]} = {roll["result"]}'
            if roll.get("settled_retry") or roll.get("lost_reply"):
                line += " (ALREADY SETTLED; re-serve this result without rerolling)"
            rendered.append(line)
        lines.append("[ROLL] " + "; ".join(rendered))

    # RPG specialization blocks (the public contract) — gated by the profile's `blocks` list and the
    # active specialization; omitted when their data is absent (same pattern as [CONSENT]).
    if rpg:
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
        if "FACTIONS" in blocks:                 # RPG-3b social plane (the public contract)
            fblock = _render_factions(state)
            if fblock:
                lines.append(fblock)
        if "RELATIONS" in blocks:
            rblock = _render_relations(state, cfg)
            if rblock:
                lines.append(rblock)
        if "NEARBY" in blocks:                   # 0b home anchors (the mechanics contract)
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
    if dm:   # DM/Game-Master framing of the guard (the public contract) — text selection, not new logic
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
def _injection_cap(cfg) -> int:
    """Ordinary per-request injection cap before an explicitly temporary component bonus."""
    cap = cfg.injection.max_tokens
    if cfg.injection.assumed_ctx_tokens > 0:
        cap = min(cap, int(cfg.injection.max_fraction * cfg.injection.assumed_ctx_tokens))
    return cap


def govern(components: list[Component], cfg, cap_bonus: int = 0) -> list[Component]:
    base_cap = _injection_cap(cfg)
    cap = base_cap + max(0, int(cap_bonus or 0))
    if base_cap <= 0:
        return []                                       # inject nothing (03 SS4 floor rule)
    comps = [c for c in components if c.text]
    header_only = base_cap < cfg.injection.header_floor_tokens
    kept, spent = [], 0
    for c in sorted(comps, key=lambda c: -c.priority):
        if header_only and c.cls not in ("state_header", "rules_contract", "combat_primer"):
            continue                                     # the contract is floored too (below)
        if c.cls == "state_header":                     # header (+guard) never dropped (04 SS3.2)
            kept.append(c)
            spent += c.tokens
            continue
        if c.cls in ("rules_contract", "combat_primer"):
            # The contract and its short-lived demonstrations are one authority floor. Compose
            # adds the primer's exact size to opening requests only, so later turns pay nothing.
            kept.append(c)                              # DEGRADES (compose picks compact) but
            spent += c.tokens                           # never silently drops — losing the tag
            continue                                    # grammar mid-campaign bred fake dialects
        if c.cls == "player_lessons":
            # Compose adds this component's exact bounded size to cap_bonus. Keep that reserve
            # independent of a higher-priority director note; base_cap<=0 and header_only have
            # already suppressed the component above.
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
_STALE_ENGINE_WRAPPER_RE = re.compile(
    r"\n*--- SCENE STATE \(engine\) ---\n.*?\n--- END SCENE STATE ---", re.DOTALL)
_RESERVED_ENGINE_REPLY_LINE_RE = re.compile(
    r"^[ \t]*(?:(?:>[ \t]*)|(?:[-+*][ \t]+)|(?:`{1,3}[ \t]*))*"
    r"\[(?:DIRECTIVE|ENEMY[ \t]+(?:ACTION|INTENT)|WAR|INIT|PLAYER(?:[ \t]+LESSONS)?|RULES|OPPOSITION|PROTOCOL|"
    r"CONTEXT[ \t]+PRIORITY|AETHER[ \t]+P[0-3]|"
    r"PRIVATE[ \t]+COMBAT[ \t]+NARRATION[ \t]+PRIMER)"
    r"(?:[^\]\r\n|]*)\]"
    r"[^\r\n]*(?:\r?\n|$)", re.IGNORECASE | re.MULTILINE)
_ATTACHED_CONTEXT_LINE_RE = re.compile(
    r"^[ \t]*\[(?:CURRENT REQUEST DIRECTIVE|PLAYER'S NEWEST MESSAGE|"
    r"CURRENT CONTINUATION TARGET)[^\]\r\n]*\]"
    r"[^\r\n]*(?:\r?\n|$)", re.IGNORECASE | re.MULTILINE)


def _clean_engine_history_text(text: str, role: str) -> str:
    """Remove stale packets and narrator-authored input-header echoes from one text part."""
    content = _ATTACHED_CONTEXT_LINE_RE.sub("", text).strip()
    if role == "system":
        content = _STALE_ENGINE_WRAPPER_RE.sub("", content).strip()
        standalone = (content.startswith(TURN_PACKET_START)
                      or content.startswith("[SCENE]")) and (
                          "\n[PLAYER]" in content or "\n[DIRECTIVE]" in content
                          or "\n[RULES]" in content)
        if standalone:
            return ""
    return _RESERVED_ENGINE_REPLY_LINE_RE.sub("", content).strip()


def _without_stale_engine_context(messages: list) -> list:
    """Remove volatile AetherState context echoed from an earlier request.

    Frontends normally resend raw history, but extensions, retries, and alternate placement modes
    can echo an enriched system message. A fresh block must replace that context, never coexist
    with it. Reserved engine-directive lines are also removed from system messages and narrator
    replies. The latter closes a prompt-conflict boundary: if a model echoed or invented an
    engine header, its surrounding story remains history but the forged header cannot sit beside
    the fresh code-owned intent/action on the next request.
    """
    out = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (
                "system", "assistant", "user"):
            out.append(message)
            continue
        role = message["role"]
        original = message.get("content")
        if isinstance(original, str):
            content = _clean_engine_history_text(original, role)
            if content:
                out.append({**message, "content": content})
            continue
        if isinstance(original, list):
            parts = []
            for part in original:
                if isinstance(part, str):
                    if cleaned := _clean_engine_history_text(part, role):
                        parts.append(cleaned)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    if cleaned := _clean_engine_history_text(part["text"], role):
                        parts.append({**part, "text": cleaned})
                else:
                    parts.append(part)
            if parts:
                out.append({**message, "content": parts})
            continue
        out.append(message)
    return out


def without_attached_user_context(doc: dict) -> tuple[dict, bool]:
    """Remove AetherState's prior outbound wrapper if a frontend returns it inside user history.

    Normal SillyTavern requests retain the raw Player text, but transport retries and other
    OpenAI-compatible clients may round-trip the enriched user message. Clean that wrapper before
    action hashing and Tier-0 so engine labels can never become part of Player intent identity.
    Assistant/system history is intentionally untouched here so Tier-0 can still detect and
    correct narrator-authored reserved echoes later in the pipeline.
    """
    original = list(doc.get("messages") or [])
    out = []
    for message in original:
        if not isinstance(message, dict) or message.get("role") != "user":
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append({**message, "content": _clean_engine_history_text(content, "user")})
            continue
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    if cleaned := _clean_engine_history_text(part, "user"):
                        parts.append(cleaned)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    if cleaned := _clean_engine_history_text(part["text"], "user"):
                        parts.append({**part, "text": cleaned})
                else:
                    parts.append(part)
            out.append({**message, "content": parts})
            continue
        out.append(message)
    changed = out != original
    return ({**doc, "messages": out} if changed else doc), changed


def _attach_current_directive(messages: list, text: str, klass: str = "",
                              settled_retry: bool = False,
                              first_intent: Optional[bool] = None,
                              settled_player_result: Optional[bool] = None,
                              settled_player_attack: Optional[bool] = None,
                              settled_enemy_action: Optional[bool] = None,
                              pending_enemy_intent: Optional[bool] = None) -> list:
    """Auto-rebase context priority and attach fresh authority to the newest Player message.

    Some OpenAI-compatible providers hoist every system message to the initial system block even
    when AetherState inserted it at depth 1. GLM then sees the right directive in the wrong place
    and spends excessive reasoning deciding whether it is stale. Each request therefore promotes
    the newest Player action to P1, marks the immediately preceding exchange P2, and demotes older
    story to P3. The system packet remains P0/P1 authority; the adjacent copy supplies unambiguous
    same-request placement. Existing priority markers were removed by the history cleaner first,
    so retries slide through the ladder without accumulating wrappers.
    """
    heads = ("[DIRECTIVE]", "[ENEMY ACTION ", "[ENEMY INTENT ")
    directives = [line for line in text.splitlines()
                  if line.lstrip().startswith(heads)
                  and not line.lstrip().startswith("[DIRECTIVE] NARRATOR REALIZATION")]
    out = list(messages)
    narrative = [i for i, message in enumerate(out)
                 if isinstance(message, dict) and message.get("role") in ("user", "assistant")]
    answer_target = None
    if narrative and out[narrative[-1]].get("role") == "user" \
            and klass not in ("continue", "impersonate"):
        answer_target = narrative[-1]
    elif narrative and klass == "continue":
        answer_target = narrative[-1]
    history = [i for i in narrative if i != answer_target]
    recent = set(history[-2:])  # the immediately preceding completed exchange
    for i in history:
        priority = "P2" if i in recent else "P3"
        superseded = bool(settled_retry and out[i].get("role") == "assistant" and i in recent)
        if superseded:
            priority = "P3"
        marker = f"[AETHER {priority}{' SUPERSEDED' if superseded else ''}]\n"
        message = out[i]
        content = message.get("content")
        if isinstance(content, str):
            out[i] = {**message, "content": marker + content}
        elif isinstance(content, list):
            out[i] = {**message, "content": [{"type": "text", "text": marker}, *content]}

    if answer_target is None:
        return out

    prefix = ""
    if directives:
        prefix += ("[AETHER P0]\n"
                   "[CURRENT REQUEST DIRECTIVE — attached to the Player's newest message]\n"
                   + "\n".join(directives) + "\n")
    current_result = extract_plain_outcome_contract(text)
    if current_result:
        prefix += (
            "[AETHER P0 CURRENT CODE-SETTLED RESULT — INPUT ONLY] "
            + current_result
            + " This current result overrides conflicting action details in history; prior "
              "assistant action descriptions are continuity only and cannot replace, supply, "
              "repeat, or alter this event. Never quote this line.\n"
        )
    # Direct helper callers retain a packet-shape fallback. Production compose passes all five
    # booleans from `_current_narration_overlay_state`, so lane selection is code-state-driven.
    if first_intent is None:
        first_intent = "FIRST-INTENT CONTINUITY:" in text
    if settled_player_attack is None:
        settled_player_attack = "CURRENT PLAYER ATTACK IS ALREADY SETTLED" in text
    if settled_player_result is None:
        settled_player_result = bool(settled_player_attack) or (
            '"asserted_settled":[' in text and '"asserted_settled":[]' not in text
        )
    if settled_enemy_action is None:
        settled_enemy_action = "[ENEMY ACTION enemy-action/1]" in text
    if pending_enemy_intent is None:
        pending_enemy_intent = "[ENEMY INTENT enemy-intent/1]" in text
    if settled_enemy_action and out[answer_target].get("role") == "user":
        prefix += (
            ESTABLISHED_COMBAT_EXCHANGE_OUTPUT_LIMIT
            if pending_enemy_intent else SETTLED_ENEMY_ACTION_OUTPUT_LIMIT
        ) + "\n"
    elif first_intent and out[answer_target].get("role") == "user":
        # Keep the Player-authorship rule adjacent to the actual action. Models otherwise tend to
        # misclassify invented posture/weapon imagery as atmospheric description instead of a new
        # voluntary action. One line lets the round-trip history cleaner remove it atomically.
        prefix += (
            SETTLED_ATTACK_FIRST_INTENT_OUTPUT_LIMIT
            if settled_player_attack else (
                SETTLED_PLAYER_RESULT_FIRST_INTENT_OUTPUT_LIMIT
                if settled_player_result else FIRST_INTENT_PLAYER_AUTHORSHIP_LIMIT
            )
        ) + "\n"
        separation = _AUTHORED_SEPARATION_MARKER_RE.search(text)
        if separation:
            if settled_player_result:
                prefix += (
                    "[AETHER P0 EXACT AUTHORED SEPARATION — INPUT ONLY] If the enemy sentence "
                    f'uses distance, keep this exact existing distance phrase once: '
                    f'"{separation.group(1)}". '
                    "Do not replace or reinterpret it. This does not limit narration of the "
                    "current settled Player result or results.\n"
                )
            else:
                prefix += (
                    "[AETHER P0 EXACT AUTHORED SEPARATION — INPUT ONLY] The only permitted Player "
                    f'reference is this exact existing distance phrase: "{separation.group(1)}". '
                    "Use it verbatim once if distance is mentioned; otherwise omit distance.\n"
                )
        else:
            prefix += (
                "[AETHER P0 EXACT AUTHORED SEPARATION — INPUT ONLY] No exact current distance is "
                "available; omit distance entirely.\n"
            )
    elif settled_player_attack and out[answer_target].get("role") == "user":
        prefix += CURRENT_PLAYER_ATTACK_OUTPUT_LIMIT + "\n"
    if out[answer_target].get("role") == "user":
        prefix += "[AETHER P1]\n[PLAYER'S NEWEST MESSAGE — respond to this now]\n"
    else:
        prefix += "[AETHER P1]\n[CURRENT CONTINUATION TARGET — continue this prose now]\n"
    message = out[answer_target]
    content = message.get("content")
    if isinstance(content, str):
        out[answer_target] = {**message, "content": prefix + content}
    elif isinstance(content, list):
        out[answer_target] = {**message,
                              "content": [{"type": "text", "text": prefix}, *content]}
    return out


def splice(doc: dict, text: str, cfg, klass: str = "", settled_retry: bool = False,
           *, first_intent: Optional[bool] = None,
           settled_player_result: Optional[bool] = None,
           settled_player_attack: Optional[bool] = None,
           settled_enemy_action: Optional[bool] = None,
           pending_enemy_intent: Optional[bool] = None) -> dict:
    msgs = _without_stale_engine_context(list(doc.get("messages", [])))
    priority_enabled = (getattr(getattr(cfg, "specialization", None), "name", "none") == "rpg")
    if priority_enabled:
        msgs = _attach_current_directive(
            msgs, text, klass, settled_retry,
            first_intent=first_intent,
            settled_player_result=settled_player_result,
            settled_player_attack=settled_player_attack,
            settled_enemy_action=settled_enemy_action,
            pending_enemy_intent=pending_enemy_intent,
        )
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
            guard_evidence: Optional[str] = None,
            combat_opening: bool = False,
            player_lessons: Optional[list[dict]] = None) -> tuple[Optional[dict], list]:
    """Returns (modified doc | None if nothing to inject, kept components for the slice row)."""
    header = render_header(state, cfg)
    guard = render_guard(cfg, stamp, klass, evidence=guard_evidence)
    joined = "\n".join(t for t in (header, guard) if t)   # guard rides the header class (04 SS3.2)
    comps = [Component("state_header", joined, cfg.injection.priorities.get("state_header", 100))
             ] if joined else []
    if note:                                               # linter corrective / director (03 SS9)
        comps.append(Component("director_note", note,
                               cfg.injection.priorities.get("director_note", 80)))
    lesson_component_ids: tuple[str, ...] = ()
    if getattr(cfg, "specialization", None) is not None and cfg.specialization.name == "rpg":
        from . import prompts                               # DM rules-contract (the public contract)
        # 2026-07-10 (Arinvale): the contract used to be a droppable component — on a rich
        # sheet the WHOLE protocol ([TAGS] grammar, [foe], and dm-rules) silently vanished
        # mid-campaign and the DM invented its own grammar, unheard and uncorrected. The
        # contract is what MAKES rpg an RPG (Bean 07-07): degrade full -> compact when the
        # budget is tight, and govern keeps whichever variant rides STICKY (never dropped).
        primer = None
        if _combat_opening_window(state, cfg, combat_opening):
            primer = Component(
                "combat_primer", prompts.COMBAT_NARRATION_PRIMER,
                cfg.injection.priorities.get("combat_primer", 74))
            comps.append(primer)
        # The worked examples buy only their own temporary space. The ordinary RPG cap remains
        # the same on all later turns; under opening pressure the full contract still degrades.
        cap = _injection_cap(cfg) + (primer.tokens if primer is not None else 0)
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
                               cfg.injection.priorities.get("rules_contract", 72)))
        lesson_text, lesson_component_ids = _player_lessons_block(player_lessons or [])
        if lesson_text:
            comps.append(Component(
                "player_lessons", lesson_text,
                cfg.injection.priorities.get("player_lessons", 71),
            ))
    if recall:                                             # Q15 precomputed lines (04 SS3.5)
        from . import memory as _memory
        who = stamp.speaker if (stamp and getattr(stamp, "speaker", None)) else None
        comps.append(Component("memories", _memory.render_recall(recall, who),
                               cfg.injection.priorities.get("memories", 60)))
    primer_bonus = next((c.tokens for c in comps if c.cls == "combat_primer"), 0)
    # An explicitly enabled preference gets its own small context reserve, just like the private
    # opening primer. It cannot evict the sticky RPG contract and can never exceed the fixed block
    # cap above. A globally disabled or header-only injector still drops it normally.
    lesson_bonus = next((c.tokens for c in comps if c.cls == "player_lessons"), 0)
    sticky_without_primer = sum(
        c.tokens for c in comps if c.cls in ("state_header", "rules_contract")
    )
    sticky_overflow = (
        max(0, sticky_without_primer - _injection_cap(cfg)) if lesson_bonus else 0
    )
    kept = govern(
        comps,
        cfg,
        cap_bonus=primer_bonus + lesson_bonus + sticky_overflow,
    )
    if not kept:
        return None, []
    priority = (TURN_PACKET_PRIORITY_LADDER + "\n"
                if getattr(cfg.specialization, "name", "none") == "rpg" else "")
    body_text = (TURN_PACKET_START + "\n" + TURN_PACKET_AUTHORITY + "\n" + priority
                 + "\n".join(c.text for c in kept) + "\n" + TURN_PACKET_END)
    first_intent, settled_result, settled_attack, settled_enemy_action, pending_enemy_intent = \
        _current_narration_overlay_state(state, cfg)
    return splice(
        doc, body_text, cfg, klass, bool(state.get("_settled_retry")),
        first_intent=first_intent,
        settled_player_result=settled_result,
        settled_player_attack=settled_attack,
        settled_enemy_action=settled_enemy_action,
        pending_enemy_intent=pending_enemy_intent,
    ), [
        {
            "cls": c.cls,
            "tokens": c.tokens,
            **({"lesson_ids": list(lesson_component_ids)}
               if c.cls == "player_lessons" else {}),
        }
        for c in kept]


def to_bytes(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
