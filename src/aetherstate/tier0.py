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
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from .state import translate_path

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


def _commands(text: str, rng: random.Random, res: Tier0Result) -> None:
    """R1 + R7: parse commands from the user's NEW message."""
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
            op = translate_path(path, value) if value else None
            if op is not None:
                res.user_ops.append(op)
            else:
                res.notices.append(f"unknown/unsupported path: {path}")   # visible, never silent (02 SS12b)
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

    # R1 + R7 — commands act once, from the new user message only
    if is_new and last_user:
        _commands(last_user, rng, res)

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
