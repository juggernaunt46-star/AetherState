"""Consistency linter L1-L11 (03 SS9, 08 L-rows, Q12/Q13; L11: 0b pillar-14 agency) — cold path only.

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

from .state import BODY_ZONES, CONSENT_RANK, _index_add, _index_remove

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
        key_eid, _, key_identity = key.partition("|")
        eid = str(b.get("holder") or key_eid)
        identity = str(b.get("proposition_id") or key_identity)
        aware.setdefault(identity, set()).add(eid)
        if b.get("teller"):
            aware[identity].add(str(b["teller"]))
    names = _char_names(state, set())
    speech = [(e, s, d) for e, s, d in _attributions(text, names) if e not in user_eids]
    if not speech:
        return
    for fid, f in facts.items():
        if not (f.get("is_secret") or f.get("visibility") == "hidden"):
            continue
        identity = str(f.get("proposition_id") or fid)
        tokens = {w for w in re.findall(r"[a-z]{5,}", str(f.get("statement", "")).lower())
                  if w not in _STOP}
        if not tokens:
            continue
        for eid, snippet, dialogue in speech:
            if eid in aware.get(identity, set()):
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


_DECIDE_VERBS = (r"(?:decides?|decided|agrees?|agreed|accepts?|accepted|refuses?|refused|"
                 r"chooses?|chose|resolves?\s+to|makes?\s+up\s+(?:his|her|their)\s+mind)")


def _open_intents(user_text: str) -> list[str]:
    """0b / pillar 14: the player's OPEN bracketed intents — '[I persuade Jerald.]' opens
    the one door through which the DM may voice the PC. [OOC ...] brackets are not a door."""
    return [m.group(1).strip() for m in re.finditer(r"\[([^\[\]]{2,300})\]", user_text or "")
            if not m.group(1).strip().lower().startswith("ooc")]


def _l11_player_voice(text, user_text, cfg, user_names, v):
    """L11 (0b, pillar 14): the DM never DECIDES for the Player. With no open bracketed
    intent this turn, decision-verbs attributed to the PC are a violation (L9 covers
    speech/action voice; this covers agency). With the door OPEN, one protection remains:
    a DIRECT QUOTE the player wrote inside the bracket is verbatim canon — PC speech that
    drops it is a rewrite. Quoted NPC dialogue is stripped before scanning (an NPC may
    SAY 'you agree, then?'). Conservative on purpose: precision over recall."""
    if not cfg.user_guard.enabled or not user_names:
        return
    intents = _open_intents(user_text)
    if not intents:
        unquoted = re.sub(r"[\"“][^\"”]*[\"”]", " ", text)
        for n in user_names:
            if len(n) < 2:
                continue
            m = (re.search(rf"\b{re.escape(n)}\s+{_DECIDE_VERBS}\b", unquoted)
                 or re.search(rf"(?i)\byou\s+{_DECIDE_VERBS}\b", unquoted))
            if m:
                v.append(Violation("L11", "high", ("user",),
                         f"assistant decided for the player character '{n}'",
                         evidence=m.group(0)[:80].strip(),
                         note=f"Note: never decide, agree, or choose on {n}'s behalf — "
                              f"present the moment and stop where {n} must decide."))
                return
        return
    quotes = [q for it in intents for q in re.findall(r"[\"“]([^\"”]{4,200})[\"”]", it)]
    if not quotes:
        return

    def _core(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()

    reply_core = _core(text)
    for q in quotes:
        cq = _core(q)
        if cq and cq not in reply_core:
            for n in user_names:
                if re.search(rf"\b{re.escape(n)}\b[^\n]{{0,60}}[\"“]", text):
                    v.append(Violation("L11", "high", ("user",),
                             "player's quoted line was rewritten",
                             evidence=q[:80],
                             note="Note: a line the player wrote in quotes is VERBATIM "
                                  "canon — deliver it word-for-word, never paraphrased."))
                    return


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


# ============================ RPG: outcome_match (05 §5.8, 08 §2) ====================
# Resolve-then-narrate anti-fudging: a `check` is resolved on the hot path (Tier-0 R8) into a
# PbtA tier + immutable [DIRECTIVE]; the narrator then writes the result. outcome_match verifies
# the narration's asserted result did not CONTRADICT the pre-decided tier. Store wins: the roll
# already happened — a mismatch steers the NEXT turn to honor it (escalating on repeat). Prose-
# facing, so it is conservative (opposite-polarity only; `partial` lenient) — precision over recall.
_POS_TIERS = {"success", "crit_success"}
_NEG_TIERS = {"fail", "crit_fail"}
_RESULT_LEXICON = {
    "crit_success": [r"critical success", r"flawless(?:ly)?", r"perfectly", r"couldn'?t have gone better",
                     r"far better than (?:hoped|expected)"],
    "success": [r"succeed(?:s|ed)?", r"\bsuccess(?:ful(?:ly)?)?", r"manage(?:s|d)? to",
                r"pull(?:s|ed)? it off", r"it works", r"you (?:do|manage) it"],
    "partial": [r"partial(?:ly)?", r"but at a cost", r"yes,? but", r"barely", r"not quite clean",
                r"at a price"],
    "fail": [r"fail(?:s|ed|ure)?", r"can'?t\b", r"cannot\b", r"doesn'?t work", r"falls? short",
             r"unable to", r"to no avail"],
    "crit_fail": [r"critical(?:ly)? fail", r"disastrous(?:ly)?", r"makes? it (?:far )?worse",
                  r"fumbl(?:e|es|ed)", r"catastroph"],
}


def _rpg_active(state, cfg) -> bool:
    """Inert in non-RPG sessions (08 §1). A Player Card exists only under RPG genesis (06 §1)."""
    if not (isinstance(state, dict) and state.get("player")):
        return False
    spec = getattr(cfg, "specialization", None)
    return spec is not None and getattr(spec, "name", "none") == "rpg"


def _check_subject_terms(state, latest, cfg) -> list:
    """Terms that bind a narrated result to THIS check: the skill label + its `governs` verbs
    (from the registry) + the acting character's name. len>=3 for precision."""
    terms: list = []
    skill = latest.get("skill")
    if skill:
        try:
            from . import registry
            players = state.get("player") or {}
            player = players.get(latest.get("char")) or next(iter(players.values()), None)
            terms += registry.load(cfg).subject_terms(skill, player)   # snapshot-first subject terms
        except Exception:
            terms.append(str(skill))
    char = latest.get("char")
    if char:
        terms.append(_name(state, char))
    return [t.lower() for t in terms if t and len(t) >= 3]


def _assert_map(text: str, subj_terms: list) -> dict:
    """tier -> evidence span, for result phrases asserted within 160 chars of a subject term
    (conservative binding: an incidental result word elsewhere in the prose is ignored)."""
    low = text.lower()
    positions: list = []
    for t in subj_terms:
        i = low.find(t)
        while i >= 0:
            positions.append(i)
            i = low.find(t, i + len(t))
    if not positions:
        return {}
    out: dict = {}
    for tier, pats in _RESULT_LEXICON.items():
        for pat in pats:
            for m in re.finditer(pat, low):
                if any(abs(m.start() - sp) <= 160 for sp in positions):
                    lo = max(0, m.start() - 24)
                    out.setdefault(tier, text[lo:m.end() + 24].strip())
                    break
            if tier in out:
                break
    return out


def _polarity_conflict(decided: str, asserted: str) -> bool:
    if decided in _POS_TIERS and asserted in _NEG_TIERS:
        return True
    if decided in _NEG_TIERS and asserted in _POS_TIERS:
        return True
    if decided == "partial" and asserted in ("crit_success", "crit_fail"):
        return True   # partial is lenient: only crits conflict (partial<->success/fail is a near-miss)
    return False


def _outcome_match(state, text, cfg, turn, v):
    rolls = state.get("rolls") or []
    checks = [r for r in rolls if r.get("turn") == turn and r.get("tier")]
    latest = checks[-1] if checks else None       # turn-scoped: same selection as [DIRECTIVE] —
    if not latest:                                # a later same-turn plain roll can't hide the check
        return                                    # no check settled this turn -> nothing to match
    decided = str(latest["tier"])
    subj = _check_subject_terms(state, latest, cfg)
    amap = _assert_map(text, subj)
    if not amap:
        return                                    # narration made no bound result claim -> ok
    conflict = next((a for a in amap if _polarity_conflict(decided, a)), None)
    if not conflict:
        return
    skill = latest.get("skill")
    v.append(Violation("outcome_match", "med", (skill, decided),
             f"narration asserts '{conflict}' but the {skill} check resolved '{decided}'",
             evidence=amap[conflict][:80],
             note=f"[DIRECTIVE] The {skill} check resolved as {decided.upper()} — narrate that "
                  f"outcome. Do not soften or override the result of a roll."))


# ============ RPG: one_instance_one_place (06 §5, 07 §7, 08 §3) — state-facing, exact ==========
# The Template+Instance safety net: `items[iid].loc` is the single source of truth; `gear`/
# `inventory` are derived indexes. The reducer preserves the invariant BY CONSTRUCTION
# (remove -> add with rollback), so a hit here means an out-of-band edit / migration produced a
# split-brain instance. Detect it, SELF-HEAL from the authority (`loc`), flag for the inspector.
# Advisory: a narrator can't fix a state bug by writing differently, so it never renders a
# corrective note and never competes with a prose violation for the single note slot.
def _heal_reindex(state, iid, loc, owner) -> None:
    """Store-wins self-heal: rebuild the index for `iid` from its `loc` (the authority)."""
    _index_remove(state, iid)
    _index_add(state, owner, iid, loc)


def _one_instance_one_place(state, v, *, heal=True):
    items = state.get("items") or {}
    gear = state.get("gear") or {}
    inv = state.get("inventory") or {}
    for iid, it in items.items():               # forward: loc reflected in exactly one index
        if not isinstance(it, dict):
            continue
        loc = str(it.get("loc") or "")
        owner = it.get("owner")
        in_gear = [(o, s) for o, slots in gear.items() for s, x in slots.items() if x == iid]
        in_inv = [(o, c) for o, conts in inv.items() for c, lst in conts.items() if iid in lst]
        if loc.startswith("gear:"):
            ok = in_gear == [(owner, loc.split(":", 1)[1])] and not in_inv
        elif loc.startswith("inv:"):
            ok = in_inv == [(owner, loc.split(":", 1)[1])] and not in_gear
        else:                                   # world / gone: indexed nowhere
            ok = not in_gear and not in_inv
        if not ok:
            v.append(Violation("one_instance_one_place", "med", (iid,),
                     f"instance {iid} loc={loc!r} but indexed at gear={in_gear} inv={in_inv}",
                     advisory=True))
            if heal:
                _heal_reindex(state, iid, loc, owner)
    for o, slots in gear.items():               # reverse: dangling index pointers
        for s, x in list(slots.items()):
            if x and x not in items:
                v.append(Violation("one_instance_one_place", "med", (x,),
                         f"gear[{o}][{s}] references unknown instance {x}", advisory=True))
                if heal:
                    del slots[s]
    for o, conts in inv.items():
        for c, lst in conts.items():
            for x in [x for x in lst if x not in items]:
                v.append(Violation("one_instance_one_place", "med", (x,),
                         f"inventory[{o}][{c}] references unknown instance {x}", advisory=True))
                if heal:
                    lst.remove(x)


# ============ RPG Phase 1: combatant_alive (plan doc 13) — death comes from the ledger ======
# The War Room's anti-fudging net: a combatant dies ONLY when code detects HP 0 (combatant_
# defeat). Narration killing a combatant whose row is alive is a contradiction — same family
# as outcome_match: conservative proximity binding, prose-facing, precision over recall.
_KILL_RE = re.compile(
    r"\b(?:dies|died|dead|slain|slays?|kill(?:s|ed)?|lifeless|corpse|breathes? "
    r"(?:his|her|their) last|no longer breathing|life (?:leaves|left)|"
    r"fell dead|drops? dead)\b", re.IGNORECASE)


def _combatant_alive(state, text, v):
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not rows or not text:
        return
    low = text.lower()
    spans = [m.start() for m in _KILL_RE.finditer(low)]
    if not spans:
        return
    for row in rows.values():
        if not isinstance(row, dict) or row.get("defeated"):
            continue
        hp = row.get("hp") or {}
        if int(hp.get("cur", 1)) <= 0:
            continue                            # about to be code-defeated: not a lie
        name = str(row.get("name", "")).lower()
        if len(name) < 3:
            continue
        for m in re.finditer(re.escape(name), low):
            if any(abs(m.start() - sp) <= 100 for sp in spans):
                lo = max(0, min(m.start(), min(spans, key=lambda s: abs(s - m.start()))) - 10)
                v.append(Violation("combatant_alive", "med", (row.get("id"),),
                         f"narration kills {row.get('name')} but the ledger has them at "
                         f"{hp.get('cur')}/{hp.get('max')} HP",
                         evidence=text[lo:lo + 90].strip(),
                         note=f"Continuity: {row.get('name')} still stands at "
                              f"{hp.get('cur')}/{hp.get('max')} HP — death comes from the "
                              f"ledger, never the prose. Bring their HP to 0 through the "
                              f"fight ([hp] tags / the engine's dice) before they can fall."))
                break
        else:
            continue
        break                                   # one hit is enough (single note slot)


# ============ RPG: one_soulmate / one_nemesis (06 §2.4, 07 §7.8, 08 §4) =====================
# The single-bond uniqueness + eligibility net. The reducer's demote-then-set keeps uniqueness
# BY CONSTRUCTION, so the structural arm firing means an out-of-band edit/migration; the
# eligibility and prose arms are the narrative guards — a bond must be EARNED (affinity >=
# Devoted) and promotions happen through set_soulmate, never through prose alone. No auto-heal:
# a wrong bond is a narrative fact to steer, so the corrective note is the instrument (08 §4).
_BOND_PHRASES = [r"\bmy soulmate\b", r"\byou are my one\b", r"\bmy one and only\b",
                 r"\bbonded (?:to (?:you|her|him|them) )?for life\b", r"\bsoul[- ]?bound to\b"]


def _player_eid(state) -> "str | None":
    for eid, rec in (state.get("player") or {}).items():
        if isinstance(rec, dict):
            return eid
    return None


def _bond_prose_target(state, text, exclude):
    """(eid, span) for a character a bond phrase binds to within 100 chars — the same
    conservative proximity family as outcome_match. (None, '') when nothing binds."""
    low = text.lower()
    spans = [m.start() for pat in _BOND_PHRASES for m in re.finditer(pat, low)]
    if not spans:
        return None, ""
    peid = _player_eid(state)
    for n, eid in _char_names(state, set()).items():
        if eid in (exclude, peid):
            continue
        for m in re.finditer(re.escape(n.lower()), low):
            for sp in spans:
                if abs(m.start() - sp) <= 100:
                    lo = max(0, min(m.start(), sp) - 10)
                    return eid, text[lo:max(m.end(), sp + 24)][:80]
    return None, ""


def _one_soulmate(state, text, cfg, v, *, ptr="soulmate"):
    from .state import DEVOTED_MIN
    peid = _player_eid(state)
    pl = (state.get("player") or {}).get(peid) or {}
    tgt = pl.get(ptr)
    if isinstance(tgt, (list, set, tuple)):     # (1) structural: a scalar pointer, always
        if len(tgt) > 1:
            v.append(Violation(f"one_{ptr}", "high", tuple(str(x) for x in tgt),
                     f"{ptr} must be a single pointer; found {sorted(str(x) for x in tgt)}",
                     advisory=True))            # data-model break: inspector-facing
        tgt = next(iter(tgt), None)
    if tgt:
        ent = state.get("entities", {}).get(tgt)
        if not ent or ent.get("kind", "character") not in ("character", "player"):
            v.append(Violation(f"one_{ptr}", "med", (tgt,),   # (3) referential integrity
                     f"{ptr} points at {tgt} which is not a known character"))
        elif ptr == "soulmate":                 # (2) eligibility — soulmate only (a nemesis
            val = (state.get("affinity", {})    # is not affinity-gated, doc 08 §4)
                   .get(f"{peid}->{tgt}", {}).get("value", 0)) if peid else 0
            if val < DEVOTED_MIN:
                v.append(Violation("one_soulmate", "med", (tgt,),
                         f"soulmate {_name(state, tgt)} has affinity {val} < "
                         f"Devoted {DEVOTED_MIN}",
                         note=f"Continuity: {_name(state, tgt)} is not yet devoted enough "
                              f"to be a soulmate — let the bond deepen before treating it "
                              f"as sealed."))
    if ptr == "soulmate":                       # (4) prose promoting a DIFFERENT partner
        other, span = _bond_prose_target(state, text, exclude=tgt)
        if other and other != tgt:
            cur = _name(state, tgt) if tgt else "no one"
            v.append(Violation("one_soulmate", "med", (tgt, other),
                     f"prose treats {_name(state, other)} as soulmate but the bond is {cur}",
                     evidence=span,
                     note=f"Continuity: the sealed soulmate bond is {cur}, not "
                          f"{_name(state, other)} — a new bond must be earned and set, "
                          f"never assumed in prose."))


# ============ L10: ledger-contradiction pass (03 SS9, assist-gated, cold path) ==============
# The one systematic prose-vs-LEDGER check. L1-L9 verify structured state and a few hand-written
# prose patterns; L10 asks a grounding model whether the narrator's prose FLATLY CONTRADICTS a
# committed ledger fact. It fires ONLY on contradiction — prose that merely adds detail the ledger
# doesn't cover is new fiction, not error (freedom of fiction, constraint on fact). Cold-path,
# fail-open, note-only: like every rule it never rewrites the current reply, only stages a
# next-turn corrective (director.best_corrective). OFF unless [assist.groups].linter_nli is
# 'assist'/'main' (default 'rules' = this pass never runs and the wire is byte-identical). It
# journals no state, so replay stays pure. Disable by name with [linter].rules_off = ["L10"].
_SENT_SPLIT = re.compile(r"[.!?]+[\s\"”\)]+|\n+")
_LEDGER_PREMISE_CAP = 24
_HYPOTHESIS_CAP = 40


def _split_sentences(text: str) -> list:
    """Prose -> hypothesis claims on sentence boundaries. Deterministic, NO LLM decomposition
    (the MiniCheck design takes claims directly). Drops fragments under 12 chars (greetings and
    interjections carry no assertable fact) and caps the count so the matrix stays bounded."""
    out: list = []
    for s in _SENT_SPLIT.split(text or ""):
        s = s.strip().strip("\"“”*").strip()
        if len(s) >= 12:
            out.append(s[:200])
        if len(out) >= _HYPOTHESIS_CAP:
            break
    return out


def _ledger_premises(state: dict) -> list:
    """The turn's committed ledger slice as (subject_key, short declarative sentence) pairs — the
    'truth' half of the NLI matrix. Reads the SAME typed state the compose renderers brief every
    turn, but emits declarative sentences a grounding model reads cleanly, and scopes it
    _prefilter-style (the present cast + the player scope the effects/items/quests in play) so the
    matrix stays bounded. Fact-dense, high-authority, DURABLE keys only: base facts and the RPG
    effects/hp/items/quests when present. Presence and clothing/poses/contacts stay out — they flip
    turn to turn and the deterministic L1–L5 rules already own them (live calibration 2026-07-08)."""
    if not isinstance(state, dict):
        return []
    present = _present(state)
    players = list((state.get("player") or {}).keys())
    prem: list = []

    def add(key: str, sentence: str) -> None:
        if sentence and len(prem) < _LEDGER_PREMISE_CAP:
            prem.append((key, sentence))

    live = [(fid, f) for fid, f in (state.get("facts") or {}).items()
            if not (isinstance(f, dict) and f.get("retired_turn") is not None)]
    for fid, f in live[-8:]:        # base facts: durable, top authority. Retired/superseded
        st = str((f or {}).get("statement", "")).strip()   # facts left the premise set — the
        if st:                                             # known stale-fact FP engine (item 3)
            add(f"fact:{fid}", st.rstrip(".") + ".")
    # presence is deliberately NOT a premise: a character legitimately enters/leaves as new fiction,
    # so it flips every sentence, and the deterministic L1/L5 rules already own absent-voice with
    # high precision. Live calibration 2026-07-08 proved presence premises were the FP engine.
    scope = set(present) | set(players)
    for eid in scope:                                              # effects: statuses & conditions (RPG)
        for rec in ((state.get("effects") or {}).get(eid) or {}).values():
            if not isinstance(rec, dict):
                continue
            nm = str(rec.get("name", rec.get("id", ""))).strip()
            if nm:
                add(f"effect:{eid}:{rec.get('id', nm)}",
                    f"{_name(state, eid)} currently has the status {nm}.")
    items = state.get("items") or {}
    for eid in players:                                            # vitals + owned items (RPG)
        p = (state.get("player") or {}).get(eid) or {}
        hp = p.get("hp") or {}
        if isinstance(hp, dict) and hp.get("max"):
            add(f"hp:{eid}", f"{_name(state, eid)} has {hp.get('cur', hp['max'])} of "
                             f"{hp['max']} hit points.")
        d = p.get("defeated")
        if isinstance(d, dict) and d.get("outcome"):
            add(f"defeat:{eid}", f"{_name(state, eid)} has been defeated.")
        for slot, iid in ((state.get("gear") or {}).get(eid) or {}).items():
            it = items.get(iid)
            if isinstance(it, dict) and it.get("name"):
                add(f"gear:{eid}:{slot}", f"{_name(state, eid)} has {it['name']} equipped.")
        for lst in ((state.get("inventory") or {}).get(eid) or {}).values():
            for iid in lst:
                it = items.get(iid)
                if isinstance(it, dict) and it.get("name") and int(it.get("qty", 1)) >= 1:
                    add(f"item:{eid}:{iid}", f"{_name(state, eid)} is carrying {it['name']}.")
    for qid, q in (state.get("quests") or {}).items():             # quests: active vs settled (RPG)
        if not isinstance(q, dict):
            continue
        nm = str(q.get("name", qid)).strip()
        stq = str(q.get("status", "active"))
        if stq == "active":
            add(f"quest:{qid}", f"The quest '{nm}' is currently active and unresolved.")
        elif stq in ("complete", "failed", "abandoned"):
            add(f"quest:{qid}", f"The quest '{nm}' is already {stq}.")
    return prem


def _l10_ledger_contradiction(state, text, premises, hits, v, *, threshold: float = 0.6) -> None:
    """Turn model contradiction HITS into L10 Violations (pure; the model call already ran in
    ledger_contradiction_pass). Non-advisory and carries a note, so it competes for the single
    corrective-note slot like any continuity rule; subjects = the premise's stable key, so a
    persisting contradiction dedups on the cooldown and nags once, not every turn."""
    seen: set = set()
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        try:
            idx = int(h.get("premise"))
            score = float(h.get("score", 1.0))
        except (TypeError, ValueError):
            continue
        if score < threshold or not 0 <= idx < len(premises):
            continue
        key, sentence = premises[idx]
        if key in seen:
            continue
        seen.add(key)
        quote = str(h.get("quote", ""))[:120]
        v.append(Violation("L10", "med", (key,),
                 f"prose contradicts the ledger: {sentence[:110]}",
                 evidence=quote,
                 note=f"Continuity: the record establishes that {sentence[:110]} Keep the "
                      f"narration consistent with it — do not contradict what has already been "
                      f"committed to the ledger."))


async def ledger_contradiction_pass(store, cfg, get_client, ep, session_id: str, branch_id: str,
                                    turn: int, state: dict, text: str) -> list:
    """Cold-path L10 orchestrator (03 SS9): serialize the scoped ledger slice into premises, split
    the prose into hypotheses, ask the grounding model for CONTRADICTIONS only, and stage them as
    L10 corrective notes through the normal linter path (cooldown-dedup + persist for the
    inspector). Single-turn + cooldown (Bean 2026-07-08). Fail-open at every seam (invariant 1):
    any error yields no note and leaves the stream untouched. Journals no state -> replay stays
    pure. The caller supplies `ep` from endpoint_for_group('linter_nli') (None -> nothing runs)."""
    try:
        if not cfg.linter.enabled or ep is None:
            return []
        if "L10" in set(getattr(cfg.linter, "rules_off", []) or []):
            return []
        scene = state.get("scene")
        if isinstance(scene, dict) and scene.get("mode") in ("flashback", "dream"):
            return []                          # prose describes the past/dream; the ledger is now
        premises = _ledger_premises(state)
        hypotheses = _split_sentences(text)
        if not premises or not hypotheses:
            return []
        from . import assist                    # local import: no linter<->assist cycle at load
        threshold = float(getattr(cfg.linter, "nli_threshold", 0.6) or 0.6)
        hits = await assist.nli_pass(get_client, cfg, ep, [s for _, s in premises],
                                     hypotheses, threshold=threshold)
        vios: list = []
        _l10_ledger_contradiction(state, text, premises, hits, vios, threshold=threshold)
        if not vios:
            return []
        recent = store.lint_recent(branch_id, turn - COOLDOWN_TURNS)
        fresh = [x for x in vios if x.key not in recent]
        if fresh:
            store.lint_add(branch_id, turn, fresh)
        return fresh
    except Exception:                          # invariant 1/3: the pass never touches the stream
        return []


# ================================ entry points ======================================
def run(state: dict, text: str, cfg, *, applied_kinds: frozenset = frozenset(),
        klass: str = "new_turn", user_name: str = "", user_aliases: tuple = (),
        turn: int = -1, user_text: str = "") -> list[Violation]:
    """Pure rule pass. Never raises on malformed state (invariant 3) — callers still wrap."""
    v: list[Violation] = []
    text = text or ""
    if not isinstance(state, dict):
        state = {}
    off = set(getattr(cfg.linter, "rules_off", []))
    scene = state.get("scene")
    nonlive = isinstance(scene, dict) and scene.get("mode") in ("flashback", "dream")  # 08 B4
    user_names = tuple(n for n in (user_name, *user_aliases) if n)
    # 0b / pillar 14: an OPEN bracketed intent in the player's message ('[I persuade
    # Jerald.]') is the one door through which the DM may voice the PC — with it open,
    # L9 stands down this turn and L11 guards only the verbatim-quote rule. RPG-gated:
    # base sessions keep L9 byte-identical behavior.
    door = bool(_open_intents(user_text)) and _rpg_active(state, cfg)
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
               klass != "impersonate" and not door)]   # impersonate/door = user-voice OK
    if _rpg_active(state, cfg):                # RPG rules — inert in non-RPG sessions (08 §1)
        checks.append(("L11",                  # 0b / pillar 14: agency + verbatim quotes
                       lambda: _l11_player_voice(text, user_text, cfg, user_names, v),
                       klass != "impersonate"))
        checks.append(("outcome_match",
                       lambda: _outcome_match(state, text, cfg, turn, v), not nonlive))
        checks.append(("one_instance_one_place",   # state integrity: live AND non-live (08 §3)
                       lambda: _one_instance_one_place(state, v), bool(state.get("items"))))
        checks.append(("combatant_alive",          # Phase 1: death comes from the ledger
                       lambda: _combatant_alive(state, text, v),
                       bool((state.get("combat") or {}).get("active")) and not nonlive))
        nemesis_on = bool(getattr(getattr(cfg, "specialization", None),   # D6: off by default
                                  "nemesis_enabled", False))
        checks.append(("one_soulmate",             # RPG-3b bonds (08 §4)
                       lambda: _one_soulmate(state, text, cfg, v), True))
        checks.append(("one_nemesis",
                       lambda: _one_soulmate(state, text, cfg, v, ptr="nemesis"), nemesis_on))
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
              user_aliases: tuple = (), user_text: str = "") -> list[Violation]:
    """Orchestrator: run rules, cooldown-dedup vs the lint log, persist survivors,
    stage the corrective note for the next turn. Fail-open at every seam."""
    if not cfg.linter.enabled:
        return []
    vios = run(state, text, cfg, applied_kinds=applied_kinds, klass=klass,
               user_name=user_name, user_aliases=user_aliases, turn=turn,
               user_text=user_text)
    seen: set = set()
    fresh: list[Violation] = []
    recent = store.lint_recent(branch_id, turn - COOLDOWN_TURNS)
    for x in vios:
        if x.key in recent or x.key in seen:
            continue
        seen.add(x.key)
        fresh.append(x)
    if any(x.rule == "outcome_match" for x in fresh):   # 08 §2: a PERSISTING override -> high
        wide = store.lint_recent(branch_id, turn - COOLDOWN_TURNS * 4)
        for x in fresh:
            if x.rule == "outcome_match" and x.key in wide:
                x.severity = "high"    # re-ask escalates to the strongest note (cf. L9 precedent)
    if fresh:
        store.lint_add(branch_id, turn, fresh)
    return fresh                       # note staging moved to director.stage (04 SS3.3)
