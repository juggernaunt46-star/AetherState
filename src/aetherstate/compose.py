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
from dataclasses import dataclass
from typing import Optional

from .state import derived_exposure, is_empty

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
    rolls = [r for r in state.get("rolls", []) if r.get("turn") == turn]
    if rolls:
        lines.append("[ROLL] " + "; ".join(f'{r["spec"]} = {r["result"]}' for r in rolls))

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
    if evidence and cfg.user_guard.mode == "prevent_and_correct":   # L9 escalation (04 SS3.2)
        return (f'[CONTROL] VIOLATION last turn: you wrote for {name} ("{evidence}"). '
                f"{name} is played by the user ONLY. Never write {name}'s dialogue, "
                f"actions, or thoughts. Stop where {name} must act.")
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
