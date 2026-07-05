"""Consistency linter L1-L9 (03 SS9, 08 L-rows, Q12/Q13) — cold path only.

Deterministic checks against the post-apply snapshot + the turn's assistant prose.
Each violation -> (rule, severity, subjects, evidence_span). Violations NEVER rewrite
the current response (invariant 2): the single highest-severity violation renders a
corrective note (04 SS3.4) that steers the NEXT turn via the director_note slot, and an
L9 hit escalates the NEXT turn's guard note (Q12). Dedup by (rule, subjects) with a
per-rule cooldown so a persisting state mismatch nags once, not every turn.

Precision over recall, by design: a false corrective note actively damages the RP, a
missed one costs nothing (the model usually self-corrects). Prose-facing rules use
conservative patterns; state-facing rules are exact. NLI contradiction pass
(linter_nli="assist") is a later assist-wiring item — rules stay the authority (03 SS9).

Mode gates: Q13 — L8 off entirely in `unrestricted`, advisory in `cnc`, explicit-levels-
only in `negotiated`. 08 B4 — non-live scenes (flashback/dream): L6/L7 advisory-only and
physical prose rules L2/L4 skipped (prose describes the past; the snapshot is the present).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .state import BODY_ZONES, CONSENT_RANK

COOLDOWN_TURNS = 3          # dedup window for (rule, subjects) — implementation constant
_SEV_RANK = {"high": 3, "med": 2, "low": 1}

_SPEECH_VERBS = (
    "said|says|say|whispered|whispers|murmured|murmurs|replied|replies|asked|asks|"
    "shouted|shouts|called|calls|muttered|mutters|breathed|breathes|moaned|moans|"
    "growled|growls|purred|purrs|gasped|gasps|hissed|hisses|snapped|snaps|added|adds|"
    "answered|answers|laughed|laughs|sighed|sighs|cried|cries|exclaimed|exclaims|"
    "continued|continues|offered|offers|promised|promises|warned|warns|teased|teases|"
    "drawled|drawls|husked|cooed|coos|groaned|groans")
_MANIP_VERBS = (r"(?:unzip|unbutton|unclasp|unhook|unlace|open|push(?:es|ed)?\s+aside|"
                r"pull(?:s|ed)?\s+(?:down|up|off|aside|open)|tug(?:s|ged)?|hik(?:e|es|ed)|"
                r"lift(?:s|ed)?|slip(?:s|ped)?|slid(?:e|es)?|smooth(?:s|ed)?|"
                r"straighten(?:s|ed)?|adjust(?:s|ed)?)")
_STOP = {"about", "after", "again", "their", "there", "these", "those", "where", "which",
         "while", "would", "could", "should", "being", "before", "between", "through",
         "under", "never", "always", "still", "something", "nothing"}
# L8 only fires for high-stakes contact (precision: linting every touch/kiss is nanny
# territory and gets the linter disabled — the Q13 worry). Ratify at gate (OQ Q13 addendum).
_CONTACT_TO_CATEGORY = {"restraining": "restraint", "impact": "impact"}
_PENETRATION_PART = {"anus": "anal", "genitals": "vaginal", "mouth": "oral_receive"}


@dataclass
class Violation:
    rule: str
    severity: str            # low | med | high (only L8/L9 are high — 03 SS9)
    subjects: tuple          # entity ids / item / category — the dedup key with rule
    detail: str              # human-readable, inspector-facing
    evidence: str = ""       # prose span (may be empty for state-only rules)
    advisory: bool = False   # detect + inspector only; never renders a corrective note
    note: str = ""           # rendered corrective (04 SS3.4); "" = no template

    @property
    def key(self) -> tuple:
        return (self.rule, "|".join(str(s) for s in self.subjects))


def _name(state: dict, eid: str) -> str:
    return state.get("entities", {}).get(eid, {}).get("name") or str(eid)


def _present(state: dict) -> set:
    return {eid for eid, e in state.get("entities", {}).items() if e.get("present")}


def _char_names(state: dict, exclude: set) -> dict[str, str]:
    """name/alias -> eid for registry characters (case-sensitive; len>=3 for precision)."""
    out: dict[str, str] = {}
    for eid, e in state.get("entities", {}).items():
        if e.get("kind", "character") != "character":
            continue
        for n in [e.get("name") or ""] + list(e.get("aliases") or []):
            if len(n) >= 3 and n not in exclude:
                out[n] = eid
    return out


def _attributions(text: str, names: dict[str, str]) -> list[tuple[str, str, str]]:
    """[(eid, evidence_snippet, quoted_dialogue)] — deterministic attribution patterns
    (same family as Q12 L9): '{Name}:' line prefix; Name+speech-verb adjacent to a quote."""
    out = []
    for n, eid in names.items():
        pat = re.escape(n)
        for m in re.finditer(rf"^[ \t]*{pat}[ \t]*:[ \t]*(.+)$", text, re.MULTILINE):
            out.append((eid, m.group(0)[:80].strip(), m.group(1)))
        for m in re.finditer(rf"[\"“]([^\"”“]+)[\"”]\s*[,—-]?\s*"
                             rf"{pat}\s+(?:{_SPEECH_VERBS})\b", text):
            out.append((eid, m.group(0)[:80].strip(), m.group(1)))
        for m in re.finditer(rf"\b{pat}\s+(?:{_SPEECH_VERBS})\b[^\"“\n]{{0,40}}"
                             rf"[\"“]([^\"”“]+)[\"”]", text):
            out.append((eid, m.group(0)[:80].strip(), m.group(1)))
    return out


# ================================ the rules =========================================
def _l1_colocation(state, v):
    present = _present(state)
    loc = state.get("scene", {}).get("location_id")
    for c in state.get("contacts", {}).values():
        if c.get("type") in ("penetrating", "restraining"):
            continue                                     # L3's jurisdiction
        for eid in (c.get("from_char"), c.get("to_char")):
            if eid and eid not in present:
                v.append(Violation("L1", "med", (eid,),
                         f"contact involves {_name(state, eid)} who is not present",
                         note=f"Note: {_name(state, eid)} is not present here"
                              f"{f' ({loc})' if loc else ''} — do not voice them until "
                              f"they arrive."))
    for eid, pose in state.get("poses", {}).items():
        a = pose.get("anchor")
        if a and a in state.get("entities", {}) and a not in present:
            v.append(Violation("L1", "med", (eid, a),
                     f"{_name(state, eid)} posed at {_name(state, a)} who is not present"))
    if loc:
        for eid in present:
            eloc = state["entities"][eid].get("location_id")
            if eloc and eloc != loc:
                v.append(Violation("L1", "med", (eid,),
                         f"{_name(state, eid)} present but located at {eloc} != {loc}",
                         note=f"Note: {_name(state, eid)} is at {eloc}, not here ({loc}) "
                              f"— do not voice them until they arrive."))


def _l2_exposure(state, text, v):
    present = _present(state)
    low = text.lower()
    for eid in present:
        items = state.get("clothing", {}).get(eid, {})
        covered = {z for it in items.values() if it.get("state") == "worn"
                   for z in it.get("covers", [])}
        for m in re.finditer(r"\b(?:bare|naked|exposed)\s+(\w+)", low):
            zone = m.group(1)
            if zone in BODY_ZONES and zone in covered:
                others = []
                for o in present:
                    if o == eid:
                        continue
                    oi = state.get("clothing", {}).get(o, {})
                    tracked = {z for it in oi.values() for z in it.get("covers", [])}
                    worn = {z for it in oi.values() if it.get("state") == "worn"
                            for z in it.get("covers", [])}
                    if zone in tracked - worn:           # known-bare on someone else
                        others.append(o)
                if others:
                    continue                             # ambiguous: someone's IS bare
                v.append(Violation("L2", "med", (eid, zone),
                         f"prose says bare {zone}; {_name(state, eid)}'s is covered",
                         evidence=m.group(0),
                         note=f"Continuity: {_name(state, eid)}'s {zone} is covered. "
                              f"Reflect this."))
        for iname, it in items.items():
            if it.get("state") == "worn":
                continue
            for m in re.finditer(rf"\b(?:through|beneath|under)\s+"
                                 rf"(?:her|his|their|its|\w+'s)\s+{re.escape(iname.lower())}\b",
                                 low):
                v.append(Violation("L2", "med", (eid, iname),
                         f"{_name(state, eid)}'s {iname} is {it.get('state')} but prose "
                         f"treats it as in place", evidence=m.group(0),
                         note=f"Continuity: {_name(state, eid)}'s {iname} is "
                              f"{it.get('state')}. Reflect this."))


def _l3_contact(state, v):
    present = _present(state)
    poses = state.get("poses", {})
    for c in state.get("contacts", {}).values():
        if c.get("type") not in ("penetrating", "restraining"):
            continue
        f, t = c.get("from_char"), c.get("to_char")
        absent = [e for e in (f, t) if e and e not in present]
        if absent:
            v.append(Violation("L3", "med", (f, t, c["type"]),
                     f"{c['type']} contact but {_name(state, absent[0])} is not present",
                     note=f"Continuity: {_name(state, absent[0])} is not present — that "
                          f"contact cannot continue. Resolve it before going on."))
            continue
        fa = (poses.get(f) or {}).get("anchor")
        ta = (poses.get(t) or {}).get("anchor")
        if fa and ta and fa != ta and fa != t and ta != f:
            v.append(Violation("L3", "med", (f, t, c["type"]),
                     f"{c['type']} contact while {_name(state, f)} and {_name(state, t)} "
                     f"are anchored to different partners",
                     note=f"Continuity: {_name(state, f)} and {_name(state, t)} are "
                          f"positioned apart — adjust before that contact continues."))
        if c["type"] == "penetrating" and (poses.get(f) or {}).get("base") == "lying_front":
            v.append(Violation("L3", "med", (f, t, "pose"),
                     f"{_name(state, f)} is lying face-down yet penetrating",
                     note=f"Continuity: {_name(state, f)}'s position makes that "
                          f"impossible — re-position first."))


def _l4_items(state, text, v):
    for eid, items in state.get("clothing", {}).items():
        for iname, it in items.items():
            if it.get("state") not in ("removed", "destroyed"):
                continue
            m = re.search(rf"{_MANIP_VERBS}[^.!?\n]{{0,30}}\b{re.escape(iname.lower())}\b",
                          text.lower())
            if m:
                v.append(Violation("L4", "med", (eid, iname),
                         f"prose manipulates {_name(state, eid)}'s {iname} which is "
                         f"{it['state']}", evidence=m.group(0)[:80],
                         note=f"Continuity: the {iname} was {it['state']} earlier — it "
                              f"cannot be handled as if worn."))


def _l5_absent_voice(state, text, user_eids, v):
    present = _present(state)
    absent_names = {n: e for n, e in _char_names(state, set()).items()
                    if e not in present and e not in user_eids}
    for eid, snippet, _ in _attributions(text, absent_names):
        v.append(Violation("L5", "med", (eid,),
                 f"dialogue attributed to {_name(state, eid)} who is not present",
                 evidence=snippet,
                 note=f"Note: {_name(state, eid)} is not present here — do not voice "
                      f"them until they arrive."))


def _l6_timeline(state, text, applied_kinds, advisory, v):
    clock = state.get("clock", {})
    low = text.lower()
    if not applied_kinds & {"time_advance", "clock_tick"}:
        m = re.search(r"\bthe next (?:morning|day|dawn|evening|night)\b|"
                      r"\b(?:hours|a day) later\b", low)
        if m:
            v.append(Violation("L6", "low", ("clock",),
                     "prose skips time but no time advance was recorded",
                     evidence=m.group(0), advisory=advisory,
                     note=f"Continuity: it is {clock.get('time_of_day', 'evening')}; do "
                          f"not skip time without cause."))
    if clock.get("day", 1) > 1 or clock.get("minutes", 0) > 0:
        m = re.search(r"\b(dawn|morning|midday|afternoon|evening|night)\s+"
                      r"(?:fell|broke|came|arrived|settled)\b", low)
        if m:
            now = clock.get("time_of_day", "evening")
            said = m.group(1)
            if said != now and not (said == "night" and now == "late_night"):
                v.append(Violation("L6", "low", ("clock", said),
                         f"prose says {said}; clock says {now}", evidence=m.group(0),
                         advisory=advisory,
                         note=f"Continuity: it is {now}; do not skip time without cause."))


def _l7_belief_leak(state, text, user_eids, advisory, v):
    facts = state.get("facts", {})
    beliefs = state.get("beliefs", {})
    if not facts or not beliefs:
        return
    aware: dict[str, set] = {}
    for key, b in beliefs.items():
        eid, _, fid = key.partition("|")
        aware.setdefault(fid, set()).add(eid)
        if b.get("teller"):
            aware[fid].add(b["teller"])
    names = _char_names(state, set())
    speech = [(e, s, d) for e, s, d in _attributions(text, names) if e not in user_eids]
    if not speech:
        return
    for fid, f in facts.items():
        if not f.get("is_secret"):
            continue
        tokens = {w for w in re.findall(r"[a-z]{5,}", str(f.get("statement", "")).lower())
                  if w not in _STOP}
        if not tokens:
            continue
        for eid, snippet, dialogue in speech:
            if eid in aware.get(fid, set()):
                continue
            if tokens & set(re.findall(r"[a-z]{5,}", dialogue.lower())):
                v.append(Violation("L7", "med", (eid, fid),
                         f"{_name(state, eid)} references secret fact "
                         f"'{str(f.get('statement'))[:60]}' they are unaware of",
                         evidence=snippet, advisory=advisory,
                         note=f"{_name(state, eid)} does not know "
                              f"{str(f.get('statement'))[:80]} — they must not "
                              f"reference it."))


def _l8_consent(state, cfg, v):
    mode = cfg.consent.mode
    if mode == "unrestricted" or state.get("frozen"):     # Q13: inert in raw; quiet in freeze
        return
    consent = state.get("consent", {})
    for c in state.get("contacts", {}).values():
        cat = _CONTACT_TO_CATEGORY.get(c.get("type")) or (
            _PENETRATION_PART.get(c.get("to_part") or "")
            if c.get("type") == "penetrating" else None)
        if not cat:
            continue
        f, t = c.get("from_char"), c.get("to_char")
        entry = consent.get(f"{t}|{f}|{cat}") or consent.get(f"{f}|{t}|{cat}")
        level = (entry or {}).get("level", "unknown")
        mi = (entry or {}).get("max_intensity")
        below = CONSENT_RANK.get(level, 2) < CONSENT_RANK["granted"]
        breach = ((below if mode == "strict" else (below and level != "unknown"))
                  or (mi is not None and c.get("intensity", 1) > mi))
        if breach:
            v.append(Violation("L8", "low" if mode == "cnc" else "high", (f, t, cat),
                     f"{cat} at consent level '{level}'"
                     + (f" intensity {c.get('intensity')}>{mi}" if mi is not None
                        and c.get("intensity", 1) > mi else ""),
                     advisory=(mode == "cnc"),
                     note=f"STOP escalation: {cat} exceeds what {_name(state, t)} has "
                          f"consented to. Pull back; if escalation is wanted, have it "
                          f"asked for and answered first."))


def _l9_user_guard(text, cfg, user_names, v):
    if not cfg.user_guard.enabled or not user_names:
        return
    for n in user_names:
        if len(n) < 2:
            continue
        pat = re.escape(n)
        m = (re.search(rf"^[ \t]*{pat}[ \t]*:[^\n]*", text, re.MULTILINE)
             or re.search(rf"[\"“][^\"”“]+[\"”]\s*[,—-]?\s*{pat}"
                          rf"\s+(?:{_SPEECH_VERBS})\b", text)
             or re.search(rf"\b{pat}\s+(?:{_SPEECH_VERBS})\b[^\"“\n]{{0,40}}"
                          rf"[\"“][^\"”“]+[\"”]", text)
             or re.search(rf"\*\s*{pat}\b[^*\n]{{0,200}}\*", text))
        if m:
            v.append(Violation("L9", "high", ("user",),
                     f"assistant wrote for the user character '{n}'",
                     evidence=m.group(0)[:80].strip()))
            return                                        # one hit is enough


# ================================ entry points ======================================
def run(state: dict, text: str, cfg, *, applied_kinds: frozenset = frozenset(),
        klass: str = "new_turn", user_name: str = "", user_aliases: tuple = ()) -> list[Violation]:
    """Pure rule pass. Never raises on malformed state (invariant 3) — callers still wrap."""
    v: list[Violation] = []
    text = text or ""
    if not isinstance(state, dict):
        state = {}
    off = set(getattr(cfg.linter, "rules_off", []))
    scene = state.get("scene")
    nonlive = isinstance(scene, dict) and scene.get("mode") in ("flashback", "dream")  # 08 B4
    user_names = tuple(n for n in (user_name, *user_aliases) if n)
    try:
        user_eids = {e for n, e in _char_names(state, set()).items() if n in user_names}
    except Exception:
        user_eids = set()

    checks = [("L1", lambda: _l1_colocation(state, v), True),
              ("L2", lambda: _l2_exposure(state, text, v), not nonlive),
              ("L3", lambda: _l3_contact(state, v), True),
              ("L4", lambda: _l4_items(state, text, v), not nonlive),
              ("L5", lambda: _l5_absent_voice(state, text, user_eids, v), True),
              ("L6", lambda: _l6_timeline(state, text, applied_kinds, nonlive, v), True),
              ("L7", lambda: _l7_belief_leak(state, text, user_eids, nonlive, v), True),
              ("L8", lambda: _l8_consent(state, cfg, v), True),
              ("L9", lambda: _l9_user_guard(text, cfg, user_names, v),
               klass != "impersonate")]       # impersonate = asked to write the user
    for rule, fn, gate in checks:
        if rule in off or not gate:
            continue
        try:
            fn()
        except Exception:                     # 03 SS11: skip failing rule, others run
            continue
    return v


def lint_turn(store, cfg, session_id: str, branch_id: str, turn: int, state: dict,
              text: str, *, applied_kinds: frozenset = frozenset(),
              klass: str = "new_turn", user_name: str = "",
              user_aliases: tuple = ()) -> list[Violation]:
    """Orchestrator: run rules, cooldown-dedup vs the lint log, persist survivors,
    stage the corrective note for the next turn. Fail-open at every seam."""
    if not cfg.linter.enabled:
        return []
    vios = run(state, text, cfg, applied_kinds=applied_kinds, klass=klass,
               user_name=user_name, user_aliases=user_aliases)
    seen: set = set()
    fresh: list[Violation] = []
    recent = store.lint_recent(branch_id, turn - COOLDOWN_TURNS)
    for x in vios:
        if x.key in recent or x.key in seen:
            continue
        seen.add(x.key)
        fresh.append(x)
    if fresh:
        store.lint_add(branch_id, turn, fresh)
    return fresh                       # note staging moved to director.stage (04 SS3.3)
