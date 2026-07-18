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

import hashlib
import random
import re
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

from . import registry
from .mechanic_settlement import (
    COMBAT_OPENING_CONTRACT,
    NON_SKILL_CHECK_ACTION_CLASSES,
    SKILL_CHECK_CONTRACT,
    WEAPON_ATTACK_CONTRACT,
    MechanicSettlementError,
    combat_opening_settlement_ref,
    skill_check_settlement_ref,
    weapon_attack_settlement_ref,
)
from .morphology import productive_compound_head
from .phrasebook import match as match_phrasebook
from .semantic import (
    DECLARED_MODIFIER_LIMIT,
    ActionFrame,
    CapabilityCandidate,
    SemanticTurn,
)
from .semantic_binding import (
    SemanticBindingError,
    action_classes_for_matches,
    build_meaning_binding,
    build_possessed_object_alignment,
    semantic_match_ref,
)
from .semantic_fabric import (
    CompiledMeaning,
    SemanticFabric,
    SemanticFabricError,
    load_default_semantic_fabric,
)
from .semantic_occurrence import (
    OccurrenceAnchor,
    build_occurrence_graph,
    occurrence_for_span,
    target_coordination_bridge_kind,
)
from .state import (
    BATTLE_COHORT_CAP,
    CHECK_ROLL_SHAPE_SCHEMA,
    COMBAT_SIDE_CAP,
    _COMBAT_PHASES,
    EFFECT_VALENCES,
    MASTERY_TICKS,
    canonical_location,
    combatant_label,
    slug,
    translate_path,
    validate_op,
)

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
    turn_guidance: str = ""              # fresh no-roll disposition for the newest RPG message;
    #                                     expires stale directives without faking an outcome
    semantic_turn: Optional[SemanticTurn] = None      # inspectable evidence at the intent boundary
    # Content-free final Player Lesson outcomes.  Pipeline persists them only after the rule batch
    # and semantic commits succeed; Tier-0 never writes lesson receipts itself.
    intent_applications: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CombatOpeningAssessment:
    """One read-only interpretation shared by prompt composition and Tier-0 settlement."""

    matched: bool
    target: Optional[tuple[str, str]]
    prompt_signal: bool
    clause_index: Optional[int] = None
    action_span: Optional[tuple[int, int]] = None
    target_span: Optional[tuple[int, int]] = None


def resource_change_op(char: str, resource: str, action: str, amount: int) -> dict:
    """Build the generic code-owned resource envelope for a future Tier-0 mechanic.

    This intentionally has no command, tag, or natural-language parser. Callers may append the
    returned op only to the trusted rule batch; ``apply_delta`` remains the authority boundary that
    verifies the exact declared pool and rejects an unavailable spend without journaling it.
    """
    op = {"op": "resource_change", "char": char, "resource": resource,
          "action": action, "amount": amount}
    if validate_op(op) is None:
        raise ValueError("invalid code-owned resource change")
    return op


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


def _parse_check(rest: str, res: Tier0Result, *, source_offset: Optional[int] = None,
                 command_span: Optional[tuple[int, int]] = None) -> None:
    """R8 declaration parse (no state/registry here): 'skill [+N|-N] [vs|dc N]'. Skill
    resolution + rolling happen in run() where state/cfg/registry/rng are in hand."""
    token_matches = list(re.finditer(r"\S+", rest))
    toks = [match.group(0) for match in token_matches]
    if not toks:
        res.notices.append("check needs a skill: ((aether.check <skill> [+N] [vs DC] "
                           "[scope ...] [use <ability>]))")
        return
    skill, mod, dc, scope, use, tgt, i = toks[0], 0, None, None, [], [], 1
    modifier_evidence: list[tuple[int, int, int]] = []
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
            value = int(toks[i])
            mod += value
            if source_offset is not None:
                modifier_evidence.append((
                    value,
                    source_offset + token_matches[i].start(),
                    source_offset + token_matches[i].end(),
                ))
        i += 1
    if not -DECLARED_MODIFIER_LIMIT <= mod <= DECLARED_MODIFIER_LIMIT:
        res.notices.append(
            "declared check modifier is outside the code-owned "
            f"{-DECLARED_MODIFIER_LIMIT:+d}..{DECLARED_MODIFIER_LIMIT:+d} bound"
        )
        return
    parsed = {"skill": skill, "mod": mod, "dc": dc, "scope": scope,
              "use": use, "target": (tgt[0] if tgt else None), "raw": rest}
    if source_offset is not None:
        skill_at = rest.lower().find(skill.lower())
        if skill_at >= 0:
            parsed["_skill_span"] = (
                source_offset + skill_at,
                source_offset + skill_at + len(skill),
            )
        if tgt:
            target_at = rest.lower().find(tgt[0].lower())
            if target_at >= 0:
                parsed["_target_span"] = (
                    source_offset + target_at,
                    source_offset + target_at + len(tgt[0]),
                )
        use_spans = []
        search_from = 0
        for reference in use:
            match = re.search(
                rf"(?i)\b(?:use|using|with)\s+['\"]?(?P<ability>{re.escape(reference)})\b",
                rest[search_from:],
            )
            if match is None:
                continue
            start = source_offset + search_from + match.start("ability")
            end = source_offset + search_from + match.end("ability")
            use_spans.append((reference, start, end))
            search_from += match.end()
        if use_spans:
            parsed["_use_spans"] = use_spans
        if modifier_evidence:
            parsed["_modifier_evidence"] = modifier_evidence
    if command_span is not None:
        parsed["_command_span"] = command_span
    res.checks.append(parsed)


def _norm_phrase(text) -> str:
    """Lowercase, keep word chars, collapse to single spaces — so 'Fire-Slash'/'fire slash'/
    'FIRE_SLASH' all normalize to 'fire slash' for loose name matching."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


# Dialogue can mention an action without the Player performing it. Keep quoted speech available to
# the narrator, but remove it from the deterministic action detector so `"strike the bell"` does
# not roll Swordplay or open the War Room. Length-preserving whitespace keeps any later span math
# stable. Straight apostrophes inside words ("blade's") are not treated as quote delimiters.
_LEGACY_QUOTED_SPEECH_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|“[^”]*”|‘[^’]*’|(?<!\w)\'[^\'\r\n]+\'(?!\w)',
    re.DOTALL,
)
_QUOTED_SPEECH_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|'
    r"(?<!\w)'[^'\r\n]+'(?!\w)",
    re.DOTALL,
)

def _action_text(text: str) -> str:
    return _QUOTED_SPEECH_RE.sub(lambda m: " " * len(m.group(0)), str(text or ""))


def _brace_phrase(text: str) -> bool:
    """V1 whole-action reaction grammar: intentionally exact and discoverable in the HUD."""
    # Do not strip engine commands: `((aether.check ...)) I brace` spends another mechanical
    # action and therefore cannot also claim this whole-action reaction.
    action = _action_text(text).strip()
    return re.fullmatch(r"(?i)i\s+brace\s*[.!]?", action) is not None


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


_INTENT_TOKEN_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:[.!?;\n]\s*|,\s*(?:and\s+)?then\s+|\band\s+then\s+)", re.IGNORECASE)
_QUESTION_REPORT_RE = re.compile(
    r"\b(?:ask(?:s|ed|ing)?|wonder(?:s|ed|ing)?|inquir(?:e|es|ed|ing)|"
    r"question(?:s|ed|ing)?|want(?:s|ed|ing)?\s+to\s+know|find(?:s|ing)?\s+out)\b"
    r"[^.!?;\n]{0,120}\b(?:who|what|when|where|why|whether|which|how)\b"
    r"[^.!?;\n]*$",
    re.IGNORECASE,
)
_DIRECT_QUESTION_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|whether|which|how)\b", re.IGNORECASE)
_NEGATED_ACTION_RE = re.compile(
    r"(?:\b(?:do(?:es)?|did|will|would|can|could|should|may|might|must)\s+not\b|"
    r"\b(?:don't|doesn't|didn't|won't|wouldn't|can't|couldn't|shouldn't|mustn't)\b|"
    r"\bnever\b)[^.!?;\n]*$",
    re.IGNORECASE,
)
_WITHOUT_ACTION_PREFIX_RE = re.compile(
    r"\bwithout(?:\s+(?:ever|actually|actively|directly|deliberately|intentionally))?\s*$",
    re.IGNORECASE,
)
_HYPOTHETICAL_ACTION_RE = re.compile(
    r"^\s*(?:if|unless|suppose|supposing|assuming|imagine|imagining)\b|"
    r"\b(?:would|could|might)\b[^.!?;\n]*$",
    re.IGNORECASE,
)
_SPEECH_FRAME_RE = re.compile(
    r"^\s*(?:i|we)\s+(?:ask|say|tell|reply|answer|whisper|murmur|shout|call|"
    r"explain|admit|promise|warn|greet|thank|apologize|nod|shrug|smile|laugh|sigh)\b",
    re.IGNORECASE,
)
_ACTION_AFTER_SPEECH_RE = re.compile(
    r"\b(?:while|then|and\s+(?:i|we)|as\s+(?:i|we)|by\s+\w+ing)\b", re.IGNORECASE)


def _fresh_turn_guidance(text: str) -> str:
    """Classify the newest no-roll RPG message for a fresh negative directive.

    This abstaining semantic filter does not resolve uncertain actions. Dialogue, questions,
    negated references, hypotheticals, and harmless social gestures are safe free narration;
    any surviving performed action remains unresolved so the narrator cannot mistake silence for
    permission to invent a mechanical outcome.
    """
    action_text = _action_text(text)
    clauses = [c.strip(" \t\r\n,:") for c in _CLAUSE_BOUNDARY_RE.split(action_text)]
    for clause in clauses:
        if not clause:
            continue
        if _DIRECT_QUESTION_RE.search(clause) or _QUESTION_REPORT_RE.search(clause):
            continue
        if _NEGATED_ACTION_RE.search(clause) or _HYPOTHETICAL_ACTION_RE.search(clause):
            continue
        if _SPEECH_FRAME_RE.search(clause) and not _ACTION_AFTER_SPEECH_RE.search(clause):
            continue
        return "unresolved"
    return "free_narration"


def _candidate_spans(cand: str, text: str, stem_aware: bool) -> list[tuple[int, int]]:
    """Character spans where a normalized candidate occurs, preserving enough source context to
    decide whether the mention is an action or merely a reference to one. The old boolean matcher
    deliberately lost this distinction and therefore treated `asks who touched it` as `I touch it`."""
    toks = list(_INTENT_TOKEN_RE.finditer(text or ""))
    needle = cand.split()
    if not toks or not needle or len(needle) > len(toks):
        return []
    exact = [_norm_phrase(t.group(0)) for t in toks]
    stems = [_stem_token(t.group(0)) for t in toks]
    want_stems = [_stem_token(t) for t in needle]
    out = []
    for i in range(len(toks) - len(needle) + 1):
        if exact[i:i + len(needle)] == needle \
                or (stem_aware and stems[i:i + len(needle)] == want_stems):
            out.append((toks[i].start(), toks[i + len(needle) - 1].end()))
    return out


def _clause_prefix(text: str, start: int) -> str:
    """The source clause before a candidate mention. Strong separators start a new attempted
    action; ordinary `and` does not, because it may join a reporting verb to its embedded question."""
    prefix = (text or "")[:start]
    boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(prefix))
    return prefix[boundaries[-1].end():] if boundaries else prefix


def _clause_span(text: str, clause_index: int) -> tuple[int, int]:
    """Exact source bounds for one detector clause, excluding its separator."""
    boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(text or ""))
    starts = [0, *(match.end() for match in boundaries)]
    ends = [*(match.start() for match in boundaries), len(text or "")]
    index = max(0, min(int(clause_index), len(starts) - 1))
    start, end = starts[index], ends[index]
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _named_nonplayer_subject(prefix: str, state: dict, player_eid: str) -> bool:
    """Reject a capability word whose clause is explicitly performed by a known non-player.
    Pronouns remain ambiguous and are left for later semantic rungs; exact named actors are safe to
    decide locally."""
    low = _norm_phrase(prefix)
    if not low:
        return False
    for eid, entity in (state.get("entities") or {}).items():
        if eid == player_eid or not isinstance(entity, dict):
            continue
        names = [entity.get("name"), *(entity.get("aliases") or [])]
        for name in names:
            norm = _norm_phrase(name)
            if norm and (low == norm or low.startswith(norm + " ")):
                return True
            if norm and re.search(rf"\b(?:that|as|while|when)\s+{re.escape(norm)}(?:\s|$)", low):
                return True
    return False


def _performed_phrase_spans(cand: str, text: str, state: dict, player_eid: str,
                            msg_norm: str, msg_stems: list,
                            stem_aware: bool) -> list[tuple[int, int, int]]:
    """Evidence spans where a capability candidate is a performed Player action now."""
    if not _phrase_hit(cand, msg_norm, msg_stems, stem_aware):
        return []
    out = []
    for start, end in _candidate_spans(cand, text, stem_aware):
        before = _clause_prefix(text, start)
        if _QUESTION_REPORT_RE.search(before) or _DIRECT_QUESTION_RE.search(before):
            continue
        if _NEGATED_ACTION_RE.search(before) or _HYPOTHETICAL_ACTION_RE.search(before):
            continue
        if _named_nonplayer_subject(before, state, player_eid):
            continue
        clause = sum(1 for _ in _CLAUSE_BOUNDARY_RE.finditer(text[:start]))
        out.append((start, end, clause))
    return out


def _performed_phrase_clauses(cand: str, text: str, state: dict, player_eid: str,
                              msg_norm: str, msg_stems: list, stem_aware: bool) -> set[int]:
    """Clause indexes where a capability candidate is a performed Player action now."""
    return {clause for _start, _end, clause in _performed_phrase_spans(
        cand, text, state, player_eid, msg_norm, msg_stems, stem_aware)}


def _performed_phrase_hit(cand: str, text: str, state: dict, player_eid: str,
                          msg_norm: str, msg_stems: list, stem_aware: bool) -> bool:
    """Context-specific deterministic semantic filter for an owned capability candidate.

    Candidate generation stays broad. This gate accepts only a mention that can denote the Player's
    performed action now. Quoted speech was already blanked by `_action_text`; here we reject embedded
    questions, direct questions, negation, hypotheticals, and explicitly named third-party actors.
    Ambiguous mentions abstain from auto-rolling and remain visible as unresolved local evidence."""
    return bool(_performed_phrase_clauses(
        cand, text, state, player_eid, msg_norm, msg_stems, stem_aware))


def _semantic_phrase_spans(cand: str, text: str, state: dict, player_eid: str,
                           msg_norm: str, msg_stems: list,
                           stem_aware: bool) -> list[tuple[int, int, int]]:
    """Recognize grounded capability evidence before deciding whether it may execute.

    Negation, questions, and hypotheticals are meanings that must survive into the canonical
    frame.  They are therefore classified after recognition instead of being discarded here.
    A definitely named non-Player actor remains outside the Player-action lane.
    """
    if not _phrase_hit(cand, msg_norm, msg_stems, stem_aware):
        return []
    out = []
    for start, end in _candidate_spans(cand, text, stem_aware):
        before = _clause_prefix(text, start)
        if _named_nonplayer_subject(before, state, player_eid):
            continue
        clause = sum(1 for _ in _CLAUSE_BOUNDARY_RE.finditer(text[:start]))
        out.append((start, end, clause))
    return out


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

# Domain-level translation memory for an explicitly selected Player capability. The command is
# intent, not execution authority: one of these translated concepts must still occur in the same
# source-bounded action before a dangerous action class may settle. Markers are generic semantic
# roots rather than campaign or creature names, so custom Element-, Void-, or Hollow-derived
# capabilities inherit the same conservative mapping.
_CAPABILITY_DOMAIN_TRANSLATIONS: dict[str, tuple[str, ...]] = {
    "blade": (
        "cut", "parry", "slash", "stab", "strike", "thrust",
    ),
    "brawl": (
        "attack", "bash", "grapple", "hit", "kick", "lunge", "punch", "slam",
        "strike", "swing", "tackle",
    ),
    "element": (
        "acid", "air", "electric", "electricity", "fire", "flame", "frost", "ice",
        "lightning", "rock", "stone", "wind",
    ),
    "hollow": (
        "being", "essence", "existence", "hollow", "self", "soul", "spirit", "unmake",
    ),
    "melee": (
        "attack", "bash", "cut", "grapple", "hit", "kick", "lunge", "punch", "slash",
        "slam", "stab", "strike", "swing", "tackle", "thrust",
    ),
    "sword": (
        "cut", "parry", "slash", "stab", "strike", "thrust",
    ),
    "void": (
        "being", "erase", "essence", "existence", "self", "soul", "spirit", "unmake",
    ),
}

# Domain inheritance is root morphology, never an arbitrary substring.  This keeps useful custom
# names such as ``Voidcraft`` and ``Elementalism`` while preventing unrelated tokens such as
# ``Avoidance`` from inheriting Void mechanics merely because their letters happen to overlap.
_CAPABILITY_DOMAIN_SUFFIXES = frozenset({
    "", "al", "alist", "alism", "craft", "eaving", "er", "ing", "ism", "ist",
    "magic", "mancy", "mastery", "play", "work", "weaver", "weaving",
})


def _capability_domain_token(token: str, marker: str) -> bool:
    if not token.startswith(marker):
        return False
    return token[len(marker):] in _CAPABILITY_DOMAIN_SUFFIXES


def _phrasebook_slot_values(state: dict, player_eid: str) -> dict[str, dict[str, str]]:
    """Ledger-grounded slot vocabulary. The Phrasebook may generalize a construction, but its
    `{person}` and `{weapon}` slots must still bind to current world/card evidence."""
    people: dict[str, str] = {}
    token_owners: dict[str, set[str]] = {}
    for eid, entity in (state.get("entities") or {}).items():
        if eid == player_eid or not isinstance(entity, dict) or not entity.get("present"):
            continue
        if entity.get("kind") not in ("character", "npc"):
            continue
        display = str(entity.get("name") or eid)
        names = [display, *(entity.get("aliases") or [])]
        for name in names:
            norm = _norm_phrase(name)
            if not norm:
                continue
            people[norm] = display
            for token in norm.split():
                if len(token) >= 3:
                    token_owners.setdefault(token, set()).add(display)
    for token, owners in token_owners.items():
        if len(owners) == 1:
            people[token] = next(iter(owners))

    weapons = {word: word for word in _HELD_WORDS}
    for item in (state.get("items") or {}).values():
        if not isinstance(item, dict) or item.get("owner") != player_eid:
            continue
        name = _norm_phrase(item.get("name", ""))
        if not name:
            continue
        weapons[name] = str(item.get("name") or name)
        for token in name.split():
            if token in _HELD_WORDS:
                weapons[token] = str(item.get("name") or token)
    return {"person": people, "weapon": weapons}


def _parse_checks_only(text: str, res: Tier0Result) -> None:
    """Re-parse ONLY ((aether.check ...)) spans (used for swipe re-rolls) — never the other
    commands (freeze/scene/set), which must not re-fire when a reply is merely re-generated."""
    for m in OOC_RE.finditer(text):
        cmd = m.group(1).strip()
        if cmd.lower().startswith("aether.check"):
            _parse_check(cmd[len("aether.check"):].strip(), res)


_SEMANTIC_GENRE_PRESETS: dict[str, tuple[str, ...]] = {
    "high_fantasy": ("high_fantasy_medieval",),
    "dark_fantasy": ("dark_fantasy_gothic",),
    "sci_fi": ("space_opera_sci_fi",),
    "cyberpunk": ("cyberpunk_dystopian",),
    "post_apoc": (
        "post_apoc_nuclear_zombies",
        "post_apoc_pandemic",
        "post_apoc_climate_collapse",
    ),
    "historical": ("low_fantasy_historical",),
}


@dataclass(frozen=True)
class _SemanticGrammar:
    body_parts: frozenset[str]
    object_parts: frozenset[str]
    armament_heads: tuple[str, ...]
    genitive_marker_re: re.Pattern[str]
    part_whole_owner_re: re.Pattern[str]
    possession_re: re.Pattern[str]
    locus_re: re.Pattern[str]


def _semantic_genre_ids(state: dict, fabric: SemanticFabric) -> tuple[str, ...]:
    """Resolve optional genre hints without letting genre language author world identity."""
    raw_values: list[str] = []
    for container in (state, state.get("world") or {}, state.get("world_identity") or {}):
        if not isinstance(container, dict):
            continue
        for key in ("semantic_genre_ids", "genre_ids"):
            value = container.get(key)
            if isinstance(value, list):
                raw_values.extend(str(item) for item in value)
        if isinstance(container.get("genre"), str):
            raw_values.append(str(container["genre"]))
    try:
        from .creator import world_from_state

        world_genre = world_from_state(state).get("genre")
        if isinstance(world_genre, str) and world_genre:
            raw_values.append(world_genre)
    except (KeyError, TypeError, ValueError):
        pass

    aliases: dict[str, str] = {}
    glossary = fabric.capability_glossary
    for genre_id in fabric.genre_ids:
        aliases[_norm_phrase(genre_id)] = genre_id
        row = (glossary.genres.get(genre_id) if glossary is not None else {}) or {}
        for value in (row.get("label"), *(row.get("aliases") or [])):
            if value:
                aliases[_norm_phrase(value)] = genre_id
    selected: set[str] = set()
    for raw in raw_values:
        key = _norm_phrase(raw)
        exact = aliases.get(key)
        if exact:
            selected.add(exact)
            continue
        selected.update(_SEMANTIC_GENRE_PRESETS.get(str(raw).strip().lower(), ()))
    return tuple(genre_id for genre_id in fabric.genre_ids if genre_id in selected)


_PLAYER_LESSON_ID_RE = re.compile(r"lesson_[0-9a-f]{32}\Z")
_CONTENT_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAYERLEX_APPROVAL_SOURCE_RE = re.compile(r"playerlex\.[0-9a-f]{32}\.r[1-9][0-9]*\Z")


def _intent_preferences_from_overlay(
    turn: SemanticTurn,
    overlay: Callable[[SemanticTurn], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate a content-free lesson selection against this exact compiled meaning.

    The lesson service owns consent, stale checks, and receipt freezing.  Tier-0 independently
    proves that every supplied anchor is one exact current PlayerLex match before it can affect a
    contextual choice.  Malformed or prose-bearing rows simply fail closed.
    """
    meaning = turn.compiled_meaning
    if meaning is None:
        return []
    proposal = overlay(turn)
    rows = proposal.get("selected") if isinstance(proposal, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return []
    preferences: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows[:5]:
        if not isinstance(raw, Mapping):
            continue
        lesson_id = raw.get("lesson_id")
        revision = raw.get("lesson_revision")
        lesson_fingerprint = raw.get("lesson_fingerprint")
        slot = raw.get("intent_slot")
        anchor = raw.get("anchor")
        source_span = raw.get("source_span")
        approval_source_id = raw.get("approval_source_id")
        if (
            not isinstance(lesson_id, str)
            or _PLAYER_LESSON_ID_RE.fullmatch(lesson_id) is None
            or lesson_id in seen
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(lesson_fingerprint, str)
            or _CONTENT_FINGERPRINT_RE.fullmatch(lesson_fingerprint) is None
            or slot not in {"action", "target"}
            or not isinstance(anchor, Mapping)
            or not isinstance(source_span, Mapping)
            or not isinstance(approval_source_id, str)
            or _PLAYERLEX_APPROVAL_SOURCE_RE.fullmatch(approval_source_id) is None
        ):
            continue
        lex_id = anchor.get("lex_id")
        concept_id = anchor.get("concept_id")
        meaning_fingerprint = anchor.get("meaning_fingerprint")
        start, end = source_span.get("start"), source_span.get("end")
        if (
            (slot == "action" and lex_id != "action")
            or (slot == "target" and lex_id != "referent")
            or not isinstance(concept_id, str)
            or not concept_id
            or not isinstance(meaning_fingerprint, str)
            or _CONTENT_FINGERPRINT_RE.fullmatch(meaning_fingerprint) is None
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(turn.source_text)
        ):
            continue
        exact = [
            match
            for match in meaning.matches
            if match.surface_baseline == "playerlex"
            and match.lex_id == lex_id
            and match.concept_id == concept_id
            and match.entry_fingerprint == meaning_fingerprint
            and match.start == start
            and match.end == end
            and approval_source_id in match.source_ids
        ]
        if len(exact) != 1:
            continue
        preferred_value: str | None = None
        if slot == "action":
            try:
                action_classes = action_classes_for_matches(exact)
            except SemanticBindingError:
                continue
            if len(action_classes) != 1:
                continue
            preferred_value = action_classes[0]
        preferences.append(
            {
                "lesson_id": lesson_id,
                "lesson_revision": revision,
                "lesson_fingerprint": lesson_fingerprint,
                "intent_slot": slot,
                "source_start": start,
                "source_end": end,
                "approval_source_id": approval_source_id,
                "preferred_value": preferred_value,
                "status": "selected",
                "reason": "",
                "frame_id": "",
                "selected_value": None,
            }
        )
        seen.add(lesson_id)
    return preferences


def _new_semantic_turn(
    text: str,
    state: dict,
    res: Tier0Result,
    recognition_overlay: Callable[[str], Mapping[str, Any]] | None = None,
    interpretation_overlay: Callable[[SemanticTurn], Mapping[str, Any]] | None = None,
) -> SemanticTurn:
    """Compile shared recognition plus one optional local overlay for this source turn."""
    turn = SemanticTurn(text)
    try:
        fabric = load_default_semantic_fabric()
        turn.compile(
            fabric,
            genre_ids=_semantic_genre_ids(state, fabric),
            recognition_overlay=recognition_overlay,
        )
    except (OSError, SemanticFabricError, ValueError) as exc:
        if turn.compiled_meaning is not None and recognition_overlay is not None:
            res.notices.append(
                "PlayerLex recognition unavailable; shared semantic recognition retained "
                f"({type(exc).__name__})"
            )
        else:
            res.notices.append(
                "semantic fabric unavailable; prose-derived mechanics abstained "
                f"({type(exc).__name__})"
            )
    except Exception as exc:
        # PlayerLex is a local recognition overlay.  Its failure cannot erase the already
        # compiled shared meaning or break an otherwise valid Player turn.
        if turn.compiled_meaning is not None and recognition_overlay is not None:
            res.notices.append(
                "PlayerLex recognition unavailable; shared semantic recognition retained "
                f"({type(exc).__name__})"
            )
        else:
            raise
    if turn.compiled_meaning is not None and interpretation_overlay is not None:
        try:
            turn.interpretation_preferences = _intent_preferences_from_overlay(
                turn, interpretation_overlay,
            )
        except Exception as exc:
            # Player Lessons are optional local preferences.  A catalog or receipt failure cannot
            # erase recognition or interrupt the Player's turn.
            turn.interpretation_preferences = []
            res.notices.append(
                "Player Lesson intent retrieval unavailable; ordinary contextual binding retained "
                f"({type(exc).__name__})"
            )
    return turn


@lru_cache(maxsize=32)
def _semantic_grammar(genre_ids: tuple[str, ...]) -> _SemanticGrammar:
    """Compile interpreter grammar from sealed ReferentLex, never a private microglossary."""
    fabric = load_default_semantic_fabric()
    body_parts = frozenset(
        _norm_phrase(term) for term in fabric.terms_for(
            "referent", kind="body_part", genre_ids=genre_ids,
        )
    )
    object_parts = frozenset(
        _norm_phrase(term) for term in fabric.terms_for(
            "referent", kind="object_part", genre_ids=genre_ids,
        )
    )
    armament_heads = tuple(
        _norm_phrase(term) for term in fabric.terms_for(
            "referent", kind="object_head", genre_ids=genre_ids,
        )
    )
    possessive_terms = tuple(
        _norm_phrase(term) for term in fabric.terms_for(
            "referent", kind="pronoun", feature=("possessive", True), genre_ids=genre_ids,
        )
    )
    genitive = next(
        row for row in fabric.constructions_for("referent")
        if row.construction_id == "referent.possession.genitive"
    )
    part_whole = next(
        row for row in fabric.constructions_for("referent")
        if row.construction_id == "referent.part_whole.of_phrase"
    )
    genitive_pattern = "|".join(re.escape(marker) for marker in genitive.markers)
    part_whole_pattern = "|".join(
        re.escape(marker) for marker in part_whole.markers
    )
    armament_pattern = "|".join(
        sorted((re.escape(head) for head in armament_heads), key=len, reverse=True)
    )
    object_part_pattern = "|".join(
        sorted((re.escape(part) for part in object_parts), key=len, reverse=True)
    )
    body_part_pattern = "|".join(
        sorted((re.escape(part) for part in body_parts), key=len, reverse=True)
    )
    pronoun_pattern = "|".join(
        sorted((re.escape(term) for term in possessive_terms), key=len, reverse=True)
    )
    role_word = r"[a-z0-9][a-z0-9_'\u2019-]*"
    genitive_marker_re = re.compile(
        r"^\s*(?:" + genitive_pattern + r")\s+",
        re.IGNORECASE,
    )
    # ReferentLex owns the part/whole marker. This parser only exposes its grammatical roles; a
    # later exact world lookup still owns the person identity, and only a typed body part can make
    # that whole the HP patient of a harmful action.
    part_whole_owner_re = re.compile(
        r"^\s*(?:(?:at|on|onto|against|into|upon|through|toward|towards|to)\s+)?"
        r"(?:(?:the|a|an)\s+)?"
        r"(?P<nominal>" + role_word + r"(?:\s+" + role_word + r"){0,2})\s+"
        r"(?:" + part_whole_pattern + r")\s+(?:(?:the|a|an)\s+)?$",
        re.IGNORECASE,
    )
    possession_re = re.compile(
        r"^\s*(?:" + genitive_pattern + r")\s+(?:(?:the|a|an)\s+)?"
        r"(?P<object>(?:[a-z0-9][a-z0-9_-]*\s+){1,2}(?:" +
        armament_pattern + r")|[a-z0-9][a-z0-9_-]*)"
        r"(?:\s+(?P<part>" + object_part_pattern + r"))?\b",
        re.IGNORECASE,
    )
    locus_re = re.compile(
        r"\b(?P<pronoun>" + pronoun_pattern + r")\s+(?P<locus>" +
        body_part_pattern + r")\b",
        re.IGNORECASE,
    )
    return _SemanticGrammar(
        body_parts=body_parts,
        object_parts=object_parts,
        armament_heads=armament_heads,
        genitive_marker_re=genitive_marker_re,
        part_whole_owner_re=part_whole_owner_re,
        possession_re=possession_re,
        locus_re=locus_re,
    )


@dataclass(frozen=True)
class _EntityPatientRole:
    """One source-local role for a named entity beside an action.

    ``patient`` is deliberately separate from linguistic possession. A person in ``Ana's bell``
    is an exact possessor candidate, not the thing struck. The same syntax may name an HP patient
    only when the nominal is a typed patient locus, such as a ReferentLex body locus
    (``Ana's ribs``) or the bounded essential-self class (``Ana's existence``). This object is
    construction evidence only; it never proves item ownership or an ungrounded antecedent.
    """

    relation: str
    patient: bool
    locus: str = ""
    locus_span: tuple[int, int] | None = None


_ROLE_NOMINAL_RE = re.compile(
    r"(?:(?:the|a|an)\s+)?"
    r"(?P<nominal>[a-z0-9][a-z0-9_'\u2019-]*"
    r"(?:\s+[a-z0-9][a-z0-9_'\u2019-]*){0,2})"
    r"(?=\s*(?:$|[.,;!?]|\b(?:with|using|by|while|when|whereas|as|before|after|"
    r"from|into|onto|on|at|against|through|toward|towards|beside|near|and|or)\b))",
    re.IGNORECASE,
)
_ROLE_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_'\u2019-]*", re.IGNORECASE)
_ESSENTIAL_SELF_HEADS = frozenset({"being", "essence", "existence", "self"})
_ESSENTIAL_SELF_MODIFIERS = frozenset({"essential", "own", "very"})


def _typed_body_locus(
    text: str,
    start: int,
    end: int,
    grammar: _SemanticGrammar,
) -> tuple[str, tuple[int, int]] | None:
    """Return the ReferentLex body head at the end of one bounded nominal, if any."""
    words = list(_ROLE_WORD_RE.finditer(text, start, end))
    for index in range(len(words)):
        phrase = _norm_phrase(text[words[index].start():words[-1].end()])
        if phrase in grammar.body_parts:
            return phrase, (words[index].start(), words[-1].end())
    return None


def _typed_essential_self_locus(
    text: str,
    start: int,
    end: int,
) -> tuple[str, tuple[int, int]] | None:
    """Return one bounded essential-self head without promoting arbitrary possessions.

    This is a semantic class, not a creature or magic exception. A harmful construction may
    target someone's existence, being, essence, or self; a sword, reputation, project, or other
    possession remains only a possessed nominal. Optional modifiers are deliberately closed so
    an unrelated multiword object cannot inherit the head's patient authority.
    """
    words = list(_ROLE_WORD_RE.finditer(text, start, end))
    if not words:
        return None
    values = [_norm_phrase(word.group(0)) for word in words]
    head = values[-1]
    if head not in _ESSENTIAL_SELF_HEADS \
            or any(value not in _ESSENTIAL_SELF_MODIFIERS for value in values[:-1]):
        return None
    return head, (words[-1].start(), words[-1].end())


def _named_genitive_role(
    text: str,
    target_end: int,
    grammar: _SemanticGrammar,
) -> _EntityPatientRole | None:
    """Classify ``Name's nominal`` without treating ``Name`` as the nominal's patient."""
    marker = grammar.genitive_marker_re.match(text[target_end:])
    if marker is None:
        return None
    nominal_start = target_end + marker.end()
    nominal = _ROLE_NOMINAL_RE.match(text[nominal_start:])
    if nominal is None:
        # The genitive marker alone proves that the name is not a direct entity object. Unknown or
        # unusually shaped nominals therefore abstain instead of falling back to HP damage.
        return _EntityPatientRole("possessed_object_owner", False)
    start = nominal_start + nominal.start("nominal")
    end = nominal_start + nominal.end("nominal")
    locus = _typed_body_locus(text, start, end, grammar)
    if locus is not None:
        return _EntityPatientRole("body_locus_owner", True, locus[0], locus[1])
    essential_self = _typed_essential_self_locus(text, start, end)
    if essential_self is not None:
        return _EntityPatientRole(
            "essential_self_locus_owner",
            True,
            essential_self[0],
            essential_self[1],
        )
    return _EntityPatientRole("possessed_object_owner", False)


def _entity_patient_role(
    text: str,
    action_end: int,
    target_start: int,
    target_end: int,
    grammar: _SemanticGrammar,
) -> _EntityPatientRole | None:
    """Classify possession/part-whole roles before generic direct-object scoring."""
    genitive = _named_genitive_role(text, target_end, grammar)
    if genitive is not None:
        return genitive
    if target_start < action_end:
        return None
    bridge = text[action_end:target_start]
    part_whole = grammar.part_whole_owner_re.fullmatch(bridge)
    if part_whole is None:
        return None
    nominal_start = action_end + part_whole.start("nominal")
    nominal_end = action_end + part_whole.end("nominal")
    locus = _typed_body_locus(text, nominal_start, nominal_end, grammar)
    if locus is not None:
        return _EntityPatientRole("body_locus_owner", True, locus[0], locus[1])
    essential_self = _typed_essential_self_locus(text, nominal_start, nominal_end)
    if essential_self is not None:
        return _EntityPatientRole(
            "essential_self_locus_owner",
            True,
            essential_self[0],
            essential_self[1],
        )
    return _EntityPatientRole("possessed_object_owner", False)


def _meaning_matches(
    meaning: CompiledMeaning | None,
    lex_id: str,
    start: int,
    end: int,
):
    if meaning is None:
        return ()
    return tuple(
        match for match in meaning.for_lex(lex_id)
        if start <= match.start and match.end <= end
    )


_EVENT_SEPARATOR_RE = re.compile(
    r"(?:,\s*)?\b(?:but|yet|whereas)\b|"
    r"(?:,\s*)?\bwhile\s+(?=(?:i|we|he|she|they|it)\b|(?-i:[A-Z])[a-z0-9'-]*\s+)|"
    r"(?:,\s*)?\band\s+(?=(?:i|we)\b)",
    re.IGNORECASE,
)
_CONTENT_MARKER_CONCEPTS = {
    "scene.cognition.content_clause",
    "scene.discourse.reported_content",
}
_SCENE_EMBEDDING_GOVERNORS = {
    "scene.discourse.report": "testimony",
    "scene.cognition.memory": "remembered",
    "scene.cognition.belief": "believed",
    "scene.discourse.quotation": "quoted",
}


def _visible_meaning_matches(
    meaning: CompiledMeaning | None,
    lex_id: str,
    start: int,
    end: int,
    detection_text: str,
):
    """Project raw recognition through the quote-masked mechanical view."""
    return tuple(
        match for match in _meaning_matches(meaning, lex_id, start, end)
        if detection_text[match.start:match.end].strip()
    )


def _trim_event_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Trim only separator punctuation and whitespace without moving evidence coordinates."""
    while start < end and (text[start].isspace() or text[start] == ","):
        start += 1
    while end > start and (text[end - 1].isspace() or text[end - 1] == ","):
        end -= 1
    return start, end


def _frame_anchor(frame: ActionFrame) -> tuple[int, int]:
    """Choose the selected capability span that anchors this event projection."""
    selected = set(frame.selected)
    candidates = [candidate for candidate in frame.candidates
                  if not selected or candidate.capability_id in selected]
    if not candidates:
        candidates = list(frame.candidates)
    if not candidates:
        return frame.start, frame.end
    candidate = min(candidates, key=lambda row: (row.start, row.end, row.capability_id))
    return candidate.start, candidate.end


_TIER0_OCCURRENCE_AUTHORITY = {
    "issuer": "player",
    "channel": "player_input",
    "lifecycle_phase": "new_action",
    "grammar_version": "tier0-semantic/1",
    "operation_family": "semantic_interpretation",
}
_OCCURRENCE_DIRECT_TARGET_BRIDGE_RE = re.compile(
    r"\s*(?:(?:not\s+(?:only|merely|just|simply)|both|either|neither)\s+)?"
    r"(?:(?:at|on|onto|against|into|upon|through|toward|towards|to|of)\s+)?"
    r"(?:(?:not\s+(?:only|merely|just|simply)|both|either|neither)\s+)?"
    r"(?:(?:the|a|an)\s+)?\s*",
    re.IGNORECASE,
)
_OCCURRENCE_ENTITY_SUBJECT_PREDICATE_RE = re.compile(
    rf"^\s*(?:{_ARRIVE}|{_DEPART})\b",
    re.IGNORECASE,
)


def _occurrence_target_anchors(
    detection_text: str,
    references: dict[str, set[str]],
    semantic_spans: list[tuple[int, int]],
    grammar: _SemanticGrammar,
    additional_reference_spans: list[tuple[int, int, str, set[str]]] | None = None,
) -> list[OccurrenceAnchor]:
    """Bind only source-local grammatical patients to occurrence nodes.

    A present entity mention is world evidence, not automatically a target.  The mention must be
    the direct object of the nearest preceding capability/action anchor, or a coordinated target
    continuing one such object.  This deliberately leaves ``strike a bell beside Ana`` unresolved
    instead of silently turning Ana into the patient.
    """
    anchors: list[OccurrenceAnchor] = []
    semantic = sorted(set(semantic_spans), key=lambda row: (row[1], row[0]))
    accepted: list[tuple[int, int, frozenset[str]]] = []
    possessors: list[tuple[int, int, frozenset[str]]] = []
    reference_spans = _reference_spans(references, detection_text)
    if additional_reference_spans:
        # Combat rows do not necessarily exist in the world-entity reference index. Feed their
        # exact, already isolated source spans through this same patient/coordination grammar so
        # connectors such as ``as well as`` cannot cut the second written patient into another
        # occurrence and leave the first row executable by itself.
        merged = list(reference_spans)
        for row in additional_reference_spans:
            overlaps = [
                existing for existing in merged
                if row[0] < existing[1] and existing[0] < row[1]
            ]
            if not overlaps:
                merged.append(row)
                continue
            if any(row[0] == existing[0] and row[1] == existing[1]
                   for existing in overlaps):
                # The world identity already owns this exact source slot.
                continue
            if all(row[0] <= existing[0] and existing[1] <= row[1]
                   for existing in overlaps):
                # A combat label such as Hollowed #1 is more exact than the overlapping
                # world-name surface Hollowed. Preserve the full written slot so #1 and #2
                # still reaches shared cardinality grammar.
                merged = [existing for existing in merged if existing not in overlaps]
                merged.append(row)
        reference_spans = merged
        reference_spans.sort(key=lambda row: (row[0], -(row[1] - row[0]), row[2]))
    for reference_index, (start, end, _reference, entity_ids) in enumerate(reference_spans):
        preceding = [span for span in semantic if span[1] <= start]
        if not preceding:
            continue
        anchor_start, anchor_end = max(preceding, key=lambda row: (row[1], row[0]))
        bridge = detection_text[anchor_end:start]
        role = _entity_patient_role(
            detection_text, anchor_end, start, end, grammar,
        )
        # A syntactically exact possessor is not a coordinated or direct entity patient. Only a
        # ReferentLex body-locus head licenses its owner as the person receiving HP harm.
        if role is not None and not role.patient:
            ambiguous_list_possessor = False
            if accepted:
                _prior_start, prior_end, _prior_ids = accepted[-1]
                ambiguous_list_possessor = prior_end <= start \
                    and not any(
                        prior_end <= span[0] and span[1] <= start for span in semantic
                    ) \
                    and target_coordination_bridge_kind(
                        detection_text[prior_end:start]
                    ) is not None
            if ambiguous_list_possessor:
                accepted.append((start, end, frozenset(entity_ids)))
                anchors.extend(
                    OccurrenceAnchor(
                        "target", str(entity_id), start, end, "ambiguous_target_scope"
                    )
                    for entity_id in sorted(entity_ids)
                )
                continue
            if not _CLAUSE_BOUNDARY_RE.search(detection_text[anchor_end:end]) \
                    and not _EVENT_SEPARATOR_RE.search(detection_text[anchor_end:end]):
                possessors.append((start, end, frozenset(entity_ids)))
            continue
        direct = bool(role is not None and role.patient) \
            or _OCCURRENCE_DIRECT_TARGET_BRIDGE_RE.fullmatch(bridge) is not None
        coordinated = False
        ambiguous_coordination = False
        target_list_continuation = False
        if accepted:
            prior_start, prior_end, _prior_ids = accepted[-1]
            # The prior target must belong to the same nearest semantic anchor.  A new semantic
            # anchor between the mentions breaks the coordination and prevents role lending. A
            # repeated entity followed by a code-owned arrival/departure predicate is instead the
            # renewed subject of that world-event clause (``..., and Iven leaves``), not another
            # patient slot for the earlier attack.
            coordination_bridge = detection_text[prior_end:start]
            renewed_semantic_subject = any(
                end <= semantic_start
                and detection_text[end:semantic_start].strip() == ""
                for semantic_start, _semantic_end in semantic
            )
            explicit_renewed_subject = renewed_semantic_subject \
                or _OCCURRENCE_ENTITY_SUBJECT_PREDICATE_RE.match(
                    detection_text[end:]
                ) is not None
            bridge_kind = target_coordination_bridge_kind(coordination_bridge)
            same_event = prior_end <= start and not any(
                prior_end <= span[0] and span[1] <= start for span in semantic
            )
            future_list_arm = any(
                end <= future_start
                and target_coordination_bridge_kind(
                    detection_text[end:future_start]
                ) is not None
                for future_start, _future_end, _future_reference, _future_ids
                in reference_spans[reference_index + 1:]
            )
            closed_nominal_arm = re.match(
                r"^\s*(?:$|[.!?;])", detection_text[end:]
            ) is not None or future_list_arm
            if same_event and bridge_kind is not None and not explicit_renewed_subject:
                coordinated = bridge_kind in {"strong", "guarded"} and closed_nominal_arm
                ambiguous_coordination = not coordinated
            target_list_continuation = coordinated or ambiguous_coordination
        if not direct and not coordinated and not ambiguous_coordination:
            continue
        # A clause/event boundary between the semantic anchor and mention always wins, including
        # for a superficially valid preposition on the far side of that boundary.
        anchor_bridge = detection_text[anchor_end:start]
        correlative_target = (
            re.search(
                r"\bnot\s+(?:only|merely|just|simply)\b[^.!?;\n]*\bbut\b",
                anchor_bridge,
                re.IGNORECASE,
            ) is not None
            and not any(
                anchor_end <= span[0] and span[1] <= start for span in semantic
            )
        )
        if (_CLAUSE_BOUNDARY_RE.search(anchor_bridge) and not target_list_continuation) \
                or (_EVENT_SEPARATOR_RE.search(anchor_bridge)
                    and not (correlative_target or target_list_continuation)):
            continue
        accepted.append((start, end, frozenset(entity_ids)))
        source = role.relation if role is not None else (
            "ambiguous_target_scope" if ambiguous_coordination
            else "coordinated_patient" if coordinated
            else "direct_patient"
        )
        anchors.extend(
            OccurrenceAnchor("target", str(entity_id), start, end, source)
            for entity_id in sorted(entity_ids)
        )

    # One exact named possessor in this same source-bounded occurrence may be the antecedent of a
    # later third-person body locus (``Iven's polehammer ... his ribs``). Never derive this from a
    # lone foe or narrator context, and never let it override an explicit entity patient.
    for locus in grammar.locus_re.finditer(detection_text):
        if _norm_phrase(locus.group("pronoun")) \
                not in {"his", "her", "hers", "its", "their", "theirs"}:
            continue
        explicit_before = [
            row for row in accepted
            if row[1] <= locus.start()
            and not _CLAUSE_BOUNDARY_RE.search(detection_text[row[1]:locus.start()])
            and not _EVENT_SEPARATOR_RE.search(detection_text[row[1]:locus.start()])
        ]
        if explicit_before:
            continue
        local_possessors = [
            row for row in possessors
            if row[1] <= locus.start()
            and not _CLAUSE_BOUNDARY_RE.search(detection_text[row[1]:locus.start()])
            and not _EVENT_SEPARATOR_RE.search(detection_text[row[1]:locus.start()])
        ]
        identities = {
            identity
            for _start, _end, entity_ids in local_possessors
            for identity in entity_ids
        }
        if len(identities) != 1:
            continue
        anchors.append(OccurrenceAnchor(
            "target",
            next(iter(identities)),
            locus.start("locus"),
            locus.end("locus"),
            "pronominal_body_locus_owner",
        ))
    return anchors


def _partition_semantic_turn_by_occurrence(
    turn: SemanticTurn,
    text: str,
    state: dict,
    player_eid: str,
    *,
    detection_text: str,
) -> None:
    """Build one ordered graph, then create frames only from node-owned candidates.

    This is a construction boundary, not admission.  Exact world references are supplied as
    anchors, but a frame may consume them only when their evidence span lies inside its own graph
    node.  No missing actor, target, capability, or action is inherited from a neighbor.
    """
    candidates_by_key: dict[tuple, CapabilityCandidate] = {}
    for frame in turn.frames:
        for candidate in frame.candidates:
            key = (
                candidate.start, candidate.end, candidate.capability_id, candidate.kind,
                candidate.source, candidate.rank, candidate.fallback, candidate.use,
                candidate.action, candidate.target, candidate.instrument,
            )
            candidates_by_key[key] = candidate
    candidates = [candidates_by_key[key] for key in sorted(candidates_by_key)]
    if not candidates:
        return

    anchors: list[OccurrenceAnchor] = [
        OccurrenceAnchor(
            "capability", str(candidate.capability_id), candidate.start, candidate.end,
            f"candidate_{candidate.source}",
        )
        for candidate in candidates
    ]
    anchors.extend(
        OccurrenceAnchor("actor", player_eid, match.start(), match.end(), "first_person")
        for match in re.finditer(r"\b(?:I|we)\b", detection_text, re.IGNORECASE)
    )
    semantic_spans = [(candidate.start, candidate.end) for candidate in candidates]
    for candidate in candidates:
        if candidate.action:
            anchors.append(OccurrenceAnchor(
                "action", str(candidate.action), candidate.start, candidate.end,
                "construction_action",
            ))
    if turn.compiled_meaning is not None:
        for match in _visible_meaning_matches(
                turn.compiled_meaning, "action", 0, len(text), detection_text):
            action_classes = action_classes_for_matches((match,))
            if "weapon_attack" in action_classes and not _performed_attack_span(
                    detection_text, match.start, match.end):
                continue
            for action_class in sorted(action_classes):
                anchors.append(OccurrenceAnchor(
                    "action", str(action_class), match.start, match.end,
                    "semantic_fabric_action",
                ))
                semantic_spans.append((match.start, match.end))
    for start, end, _physical in _combat_transition_spans(detection_text):
        action_class = "weapon_attack" \
            if _has_attack_intent(detection_text[start:end]) else "combat_opening"
        anchors.append(OccurrenceAnchor(
            "action", action_class, start, end, "tier0_action",
        ))
        semantic_spans.append((start, end))

    anchors.extend(_occurrence_target_anchors(
        detection_text,
        _present_npc_references(state, player_eid),
        semantic_spans,
        _semantic_grammar(
            turn.compiled_meaning.genre_ids if turn.compiled_meaning is not None else ()
        ),
        _combat_occurrence_reference_spans(state, detection_text),
    ))

    graph = build_occurrence_graph(
        text,
        detection_text=detection_text,
        anchors=anchors,
        **_TIER0_OCCURRENCE_AUTHORITY,
    )
    if not graph["authority"]["allowed"]:
        turn.occurrence_graph = graph
        turn.frames = []
        return
    # An ordinary Player turn itself is actor authority even when the Player uses an imperative
    # without spelling out "I".  Bind that speaker separately inside each exact candidate span;
    # this is source-local default evidence, never actor lending from another occurrence.
    speaker_anchors: list[OccurrenceAnchor] = []
    for node in graph["occurrences"]:
        if node["capabilities"] and not node["actors"]:
            span = node["capabilities"][0]["span"]
            speaker_anchors.append(OccurrenceAnchor(
                "actor", player_eid, span[0], span[1], "turn_speaker",
            ))
    if speaker_anchors:
        graph = build_occurrence_graph(
            text, detection_text=detection_text, anchors=[*anchors, *speaker_anchors],
            **_TIER0_OCCURRENCE_AUTHORITY,
        )
    turn.occurrence_graph = graph

    candidates_by_occurrence: dict[str, list[CapabilityCandidate]] = {}
    for candidate in candidates:
        node = occurrence_for_span(graph, candidate.start, candidate.end)
        if node is None:
            continue
        candidates_by_occurrence.setdefault(node["occurrence_id"], []).append(candidate)
    turn.frames = []
    for node in graph["occurrences"]:
        owned = candidates_by_occurrence.get(node["occurrence_id"])
        if not owned:
            continue
        turn.frames.append(ActionFrame(
            frame_id="",
            clause_index=int(node["clause_index"]),
            start=int(node["source_span"][0]),
            end=int(node["source_span"][1]),
            candidates=sorted(
                owned,
                key=lambda row: (row.start, row.end, row.capability_id, row.source),
            ),
            occurrence=deepcopy(node),
        ))


def _embedding_context(
    meaning: CompiledMeaning | None,
    text: str,
    detection_text: str,
    segment_start: int,
    segment_end: int,
    anchor_start: int,
) -> dict | None:
    """Find one structurally licensed outer context for an embedded Player event.

    A bare ``that``, ``whether``, or ``if`` never grants this classification.  The content
    marker must be paired with a recognized reporting, cognition, or communication governor,
    and a new first-person event subject must occur between that marker and the capability.
    """
    if meaning is None:
        return None
    scene = tuple(
        match for match in meaning.for_lex("scene")
        if detection_text[match.start:match.end].strip()
    )
    action = tuple(
        match for match in meaning.for_lex("action")
        if detection_text[match.start:match.end].strip()
    )
    markers = [
        match for match in scene
        if match.concept_id in _CONTENT_MARKER_CONCEPTS
        and segment_start <= match.start
        and match.end <= anchor_start
    ]
    subjects = [
        match for match in action
        if match.concept_id == "action.frame.first_person_actual"
        and segment_start <= match.start < anchor_start
    ]
    candidates: list[tuple[int, int, object, object, str]] = []
    for marker in markers:
        inner_subjects = [subject for subject in subjects if subject.start >= marker.end]
        if not inner_subjects:
            continue
        subject = max(inner_subjects, key=lambda row: (row.start, row.end))
        governors: list[tuple[object, str]] = [
            (match, _SCENE_EMBEDDING_GOVERNORS[match.concept_id])
            for match in scene
            if match.concept_id in _SCENE_EMBEDDING_GOVERNORS
            and segment_start <= match.start
            and match.end <= anchor_start
            and match.start <= marker.end
        ]
        governors.extend(
            (match, "attributed")
            for match in action
            if match.concept_id == "action.communicate"
            and segment_start <= match.start
            and match.end <= anchor_start
            and match.start <= marker.end
        )
        if not governors:
            continue
        governor, assertion = max(
            governors,
            key=lambda row: (row[0].end, row[0].start,
                             1 if row[0].lex_id == "scene" else 0),
        )
        candidates.append((marker.end, subject.start, marker, governor, assertion))
    if not candidates:
        # A recognized governor plus a renewed first-person event, but no structural content
        # marker, is not safely direct. Preserve it as an unresolved scope instead of guessing.
        governors: list[tuple[object, str]] = [
            (match, _SCENE_EMBEDDING_GOVERNORS[match.concept_id])
            for match in scene
            if match.concept_id in _SCENE_EMBEDDING_GOVERNORS
            and segment_start <= match.start and match.end <= anchor_start
        ]
        governors.extend(
            (match, "attributed")
            for match in action
            if match.concept_id == "action.communicate"
            and segment_start <= match.start and match.end <= anchor_start
        )
        incomplete = [
            (governor, assertion, subject)
            for governor, assertion in governors
            for subject in subjects
            if subject.start >= governor.end
        ]
        if not incomplete:
            return None
        governor, _assertion, subject = max(
            incomplete, key=lambda row: (row[2].start, row[0].end),
        )
        return {
            "assertion_context": "unresolved",
            "content_start": subject.start,
            "scope_start": governor.start,
            "scope_end": segment_end,
            "marker": None,
            "governor": governor,
            "question": False,
            "incomplete": True,
        }
    _marker_end, subject_start, marker, governor, assertion = max(
        candidates, key=lambda row: (row[0], row[1], row[3].end),
    )
    return {
        "assertion_context": assertion,
        "content_start": subject_start,
        "scope_start": min(governor.start, marker.start),
        "scope_end": segment_end,
        "marker": marker,
        "governor": governor,
        "question": text[marker.start:marker.end].strip().lower() == "whether",
    }


def _bounded_event_span(
    frame: ActionFrame,
    text: str,
    meaning: CompiledMeaning | None,
    detection_text: str | None = None,
) -> tuple[int, int, dict | None]:
    """Bound one event before role grounding so adjacent clauses cannot lend arguments."""
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else text
    occurrence = frame.occurrence if isinstance(frame.occurrence, dict) else None
    if occurrence is not None:
        span = occurrence.get("source_span")
        if isinstance(span, list) and len(span) == 2 \
                and all(isinstance(item, int) and not isinstance(item, bool) for item in span) \
                and 0 <= span[0] < span[1] <= len(text):
            anchor_start, _anchor_end = _frame_anchor(frame)
            embedding = _embedding_context(
                meaning, text, detector, span[0], span[1], anchor_start,
            )
            start = max(span[0], int(embedding["content_start"])) \
                if embedding is not None else span[0]
            return start, span[1], embedding
    hard_start, hard_end = _clause_span(detector, frame.clause_index)
    anchor_start, anchor_end = _frame_anchor(frame)
    segments: list[tuple[int, int]] = []
    cursor = hard_start
    for separator in _EVENT_SEPARATOR_RE.finditer(detector, hard_start, hard_end):
        segment = _trim_event_span(detector, cursor, separator.start())
        if segment[1] > segment[0]:
            segments.append(segment)
        cursor = separator.end()
    segment = _trim_event_span(detector, cursor, hard_end)
    if segment[1] > segment[0]:
        segments.append(segment)
    start, end = next(
        (
            span for span in segments
            if span[0] <= anchor_start and anchor_end <= span[1]
        ),
        (hard_start, hard_end),
    )
    embedding = _embedding_context(
        meaning, text, detector, start, end, anchor_start,
    )
    if embedding is not None:
        start = max(start, int(embedding["content_start"]))
    return start, end, embedding


def _frame_context(
    frame: ActionFrame,
    text: str,
    meaning: CompiledMeaning | None = None,
) -> None:
    clause_text = text[frame.start:frame.end]
    starts = [candidate.start for candidate in frame.candidates]
    prefix = text[frame.start:min(starts) if starts else frame.end]
    clause_terminator = text[frame.end:frame.end + 1]
    frame.polarity = "negative" if _NEGATED_ACTION_RE.search(prefix) else "positive"
    if _DIRECT_QUESTION_RE.search(prefix) or _QUESTION_REPORT_RE.search(prefix) \
            or (("?" in clause_text or clause_terminator == "?")
                and not _HYPOTHETICAL_ACTION_RE.search(prefix)):
        frame.modality = "question"
    elif _HYPOTHETICAL_ACTION_RE.search(prefix):
        frame.modality = "hypothetical"
    else:
        frame.modality = "actual"
    scene_matches = _visible_meaning_matches(
        meaning, "scene", frame.start, frame.end, text,
    )
    scene_concepts = {match.concept_id for match in scene_matches}
    scene_features = [match.features for match in scene_matches]
    future = any(features.get("time_scope") == "future" for features in scene_features) \
        or bool(scene_concepts & {
        "scene.event.anticipated",
        "scene.modality.future_or_plan",
        "scene.time.future",
    })
    past = any(features.get("time_scope") == "past" for features in scene_features) \
        or "scene.time.past" in scene_concepts
    frame.time_scope = "past" if past else "future" if future else "current"
    if frame.modality == "actual" and future:
        frame.modality = "possible"


def _frame_intent_preferences(
    frame: ActionFrame,
    preferences: Iterable[Mapping[str, Any]],
    slot: str,
) -> list[dict[str, Any]]:
    return [
        preference
        for preference in preferences
        if isinstance(preference, dict)
        and preference.get("intent_slot") == slot
        and isinstance(preference.get("source_start"), int)
        and isinstance(preference.get("source_end"), int)
        and frame.start <= int(preference["source_start"])
        and int(preference["source_end"]) <= frame.end
    ]


def _set_intent_application(
    preference: dict[str, Any],
    *,
    frame: ActionFrame,
    applied: bool,
    reason: str,
    selected_value: str | None = None,
) -> None:
    """Set a provisional, repeatable decision; receipt persistence occurs after settlement."""
    preference["status"] = "applied" if applied else "not_applied"
    preference["reason"] = reason
    preference["frame_id"] = frame.frame_id
    preference["selected_value"] = selected_value if applied else None


def _frame_action_class(
    frame: ActionFrame,
    text: str,
    meaning: CompiledMeaning | None = None,
    intent_preferences: Iterable[Mapping[str, Any]] = (),
) -> str:
    clause_text = text[frame.start:frame.end]
    action_preferences = _frame_intent_preferences(frame, intent_preferences, "action")
    # Lethal declarations have a distinct settlement contract outside the War Room. Freeze that
    # distinction here so the kill gate never rediscovers intent after other mechanics consume it.
    if _GRAND.search(clause_text):
        for preference in action_preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="current_input_conflict",
            )
        return "grand_kill_attempt"
    if _KILL_VERBS.search(clause_text):
        for preference in action_preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="current_input_conflict",
            )
        return "kill_attempt"
    authored = {
        candidate.action for candidate in frame.candidates
        if candidate.action
        and frame.start <= candidate.start and candidate.end <= frame.end
    }
    recognized_matches = tuple(
        match for match in _visible_meaning_matches(
            meaning, "action", frame.start, frame.end, text,
        )
        if not _WITHOUT_ACTION_PREFIX_RE.search(_clause_prefix(text, match.start))
        and (
            "weapon_attack" not in action_classes_for_matches((match,))
            or _performed_attack_span(text, match.start, match.end)
        )
    )
    recognized = set(action_classes_for_matches(recognized_matches))
    supported = authored | recognized
    # Movement is a compatible manner/preparation inside an exact attack construction ("rush
    # and slash", "run my blade through"). This is an explicit topology combination, not a
    # blanket layer priority: unrelated classes still conflict below.
    if "weapon_attack" in supported and supported <= {"weapon_attack", "movement"}:
        for preference in action_preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="input_unambiguous",
            )
        return "weapon_attack"
    if len(supported) == 1:
        for preference in action_preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="input_unambiguous",
            )
        return next(iter(supported))
    if len(supported) > 1:
        if len(action_preferences) == 1:
            preference = action_preferences[0]
            preferred = preference.get("preferred_value")
            source_start = int(preference["source_start"])
            source_end = int(preference["source_end"])
            anchor_matches = tuple(
                match
                for match in recognized_matches
                if match.start == source_start and match.end == source_end
            )
            anchor_supported = set(action_classes_for_matches(anchor_matches)) | {
                candidate.action
                for candidate in frame.candidates
                if candidate.action
                and candidate.start == source_start
                and candidate.end == source_end
            }
            independent_supported = set(
                action_classes_for_matches(
                    tuple(
                        match
                        for match in recognized_matches
                        if match.start != source_start or match.end != source_end
                    )
                )
            ) | {
                candidate.action
                for candidate in frame.candidates
                if candidate.action
                and (candidate.start != source_start or candidate.end != source_end)
            }
            # A lesson may break only a genuine tie at its own approved phrase. Any separate
            # current verb/construction is newer, explicit evidence and therefore wins over the
            # recurring preference. Preserve ordinary binding rather than letting the lesson
            # reinterpret that input.
            if independent_supported or len(anchor_supported) < 2:
                _set_intent_application(
                    preference,
                    frame=frame,
                    applied=False,
                    reason="current_input_conflict",
                )
            elif isinstance(preferred, str) and preferred in anchor_supported:
                _set_intent_application(
                    preference,
                    frame=frame,
                    applied=True,
                    reason="action_ambiguity_resolved",
                    selected_value=preferred,
                )
                return preferred
            else:
                _set_intent_application(
                    preference,
                    frame=frame,
                    applied=False,
                    reason="candidate_absent",
                )
        elif len(action_preferences) > 1:
            for preference in action_preferences:
                _set_intent_application(
                    preference,
                    frame=frame,
                    applied=False,
                    reason="lesson_conflict",
                )
        frame.ambiguity = sorted(
            set(frame.ambiguity) | {f"action_class.{item}" for item in supported}
        )
        return "ambiguous_action"
    for preference in action_preferences:
        _set_intent_application(
            preference,
            frame=frame,
            applied=False,
            reason="candidate_absent",
        )
    if _has_attack_intent(clause_text):
        return "weapon_attack"
    return "skill_check"


def _apply_intent_target_preference(
    frame: ActionFrame,
    target_candidates: set[str],
    grounded_by_span: Mapping[tuple[int, int], set[str]],
    intent_preferences: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Narrow an existing target ambiguity only at one exact independently grounded span."""
    candidates = set(target_candidates)
    preferences = _frame_intent_preferences(frame, intent_preferences, "target")
    if not preferences:
        return candidates
    if "occurrence.multiple_targets" in frame.ambiguity:
        for preference in preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="explicit_multi_target",
            )
        return candidates
    if len(preferences) > 1:
        for preference in preferences:
            _set_intent_application(
                preference,
                frame=frame,
                applied=False,
                reason="lesson_conflict",
            )
        return candidates
    preference = preferences[0]
    if len(candidates) == 1:
        _set_intent_application(
            preference,
            frame=frame,
            applied=False,
            reason="input_unambiguous",
        )
        return candidates
    if not candidates:
        _set_intent_application(
            preference,
            frame=frame,
            applied=False,
            reason="candidate_absent",
        )
        return candidates
    span = (int(preference["source_start"]), int(preference["source_end"]))
    anchored = set(grounded_by_span.get(span, set())) & candidates
    if not anchored:
        _set_intent_application(
            preference,
            frame=frame,
            applied=False,
            reason="candidate_absent",
        )
        return candidates
    if len(anchored) != 1:
        _set_intent_application(
            preference,
            frame=frame,
            applied=False,
            reason="candidate_ambiguous",
        )
        return candidates
    selected = next(iter(anchored))
    _set_intent_application(
        preference,
        frame=frame,
        applied=True,
        reason="target_ambiguity_resolved",
        selected_value=selected,
    )
    return {selected}


def _semantic_armament_object(value: str, grammar: _SemanticGrammar) -> bool:
    word = _norm_phrase(value).replace(" ", "")
    if word in _HELD_WORDS:
        return True
    return productive_compound_head(word, grammar.armament_heads) is not None


def _ledger_possessed_item_alignment(
    state: dict,
    object_name: str,
    linguistic_possessor_id: str | None,
) -> tuple[str | None, str | None]:
    """Independently prove an exact item and owner; grammar is never ownership evidence."""
    if not linguistic_possessor_id:
        return None, None
    normalized = _norm_phrase(object_name)
    matches = [
        (str(instance_id), str(item.get("owner") or ""))
        for instance_id, item in (state.get("items") or {}).items()
        if isinstance(item, dict)
        and _norm_phrase(item.get("name", "")) == normalized
        and str(item.get("owner") or "") == linguistic_possessor_id
    ]
    if len(matches) != 1:
        return None, None
    return matches[0]


_RELATIVE_ENEMY_TARGET_RE = re.compile(
    r"\b(?:(?:(?:the|a|an)\s+)?(?:nearest|closest)\s+(?:enemy|foe|opponent)"
    r"|the\s+(?:enemy|foe|opponent))\b",
    re.IGNORECASE,
)
_EXPLICIT_TARGET_MARKER_RE = re.compile(
    r"\b(?:at|into|onto|upon|through|toward|towards|against|on)\s+",
    re.IGNORECASE,
)


def _bind_relative_enemy_target(frame: ActionFrame, text: str, state: dict) -> None:
    """Ground one relative hostile reference from the live combat ledger before freeze.

    Distance and ordering are not ledger facts, so ``nearest``/``closest`` can identify a target
    only when exactly one live enemy combatant exists.  Multiple live enemies remain an explicit
    ambiguity; a separately grounded exact target always wins before this fallback is considered.
    """
    if frame.action_class != "weapon_attack" or frame.target_entity_id \
            or frame.target_name or frame.ambiguity:
        return
    mentions = list(_RELATIVE_ENEMY_TARGET_RE.finditer(text, frame.start, frame.end))
    if len(mentions) != 1:
        return
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not isinstance(rows, dict):
        return
    foes = [
        (str(cid), row)
        for cid, row in rows.items()
        if isinstance(row, dict) and row.get("side") == "enemy" and not row.get("defeated")
    ]
    if not foes:
        return
    mention = mentions[0]
    foe_ids = sorted(cid for cid, _row in foes)
    if len(foes) == 1:
        target_id, target = foes[0]
        frame.target_entity_id = target_id
        frame.target_name = combatant_label(target, target_id)
        frame.add_evidence("target", mention.start(), mention.end(), target_id)
        return
    frame.ambiguity = sorted(set(frame.ambiguity) | set(foe_ids))
    frame.add_evidence("target", mention.start(), mention.end(), foe_ids)


def _ground_action_frame(frame: ActionFrame, text: str, state: dict,
                         player_eid: str, dm_text: str = "",
                         meaning: CompiledMeaning | None = None,
                         detection_text: str | None = None,
                         intent_preferences: Iterable[Mapping[str, Any]] = ()) -> None:
    """Bind actor, capability, action, entity roles, context, and exact evidence once."""
    genre_ids = meaning.genre_ids if meaning is not None else ()
    grammar = _semantic_grammar(genre_ids)
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else text
    frame.start, frame.end, embedding = _bounded_event_span(
        frame, text, meaning, detection_text=detector,
    )
    occurrence = frame.occurrence if isinstance(frame.occurrence, dict) else None
    frame.actor_id = player_eid
    frame.action_class = _frame_action_class(
        frame,
        detector,
        meaning,
        intent_preferences=intent_preferences,
    )
    if meaning is not None:
        frame.genre_ids = meaning.genre_ids
        frame.meaning_ref = str(meaning.receipt_dict()["fingerprint"])
        frame.fabric_fingerprint = meaning.fabric_fingerprint
        frame.ambiguity = sorted(set(frame.ambiguity) | {
            item for item in meaning.unresolved
            if item.startswith("semantic_fabric.match_budget_exceeded.")
        })
    _frame_context(frame, detector, meaning)
    if occurrence is not None:
        # Context comes from the same exact node as capability recognition.  Qualifier-only
        # reasons remain in the graph while polarity/modality carry their established journal
        # semantics; structural and nonliteral reasons also enter ambiguity so settlement closes.
        occurrence_polarity = str(occurrence.get("polarity") or "unknown")
        # The source-bounded occurrence is the canonical polarity owner.  Do not let the older
        # clause-prefix heuristic reclassify an affirmative additive construction such as ``not
        # only``, or simplify a structurally ambiguous double negative into executable authority.
        frame.polarity = {
            "negated": "negative",
            "affirmative": "positive",
        }.get(occurrence_polarity, "unknown")
        actuality = str(occurrence.get("actuality") or "unknown")
        if actuality == "hypothetical":
            frame.modality = "hypothetical"
        occurrence_reasons = {
            str(reason) for reason in occurrence.get("unresolved_reasons") or ()
        }
        # Players often write low-impact checks in immediate roleplay past (``I sneaked`` or
        # ``I pressed Halvic``). Preserve the graph's honest past-surface evidence, but let only
        # a targetless generic check or a typed social bargain use that narrow convention. A
        # directed capability such as ``I rained fire on Bandit`` keeps its grounded patient and
        # therefore remains noncurrent; every explicit time, report, modal, quote, hypothesis,
        # or additional unresolved reason also stays closed.
        roleplay_past_check = (
            occurrence_reasons == {
                "occurrence.actuality_unbound", "occurrence.past_surface",
            }
            and (
                frame.action_class == "social_bargain"
                or (
                    frame.action_class == "skill_check"
                    and not (occurrence.get("targets") or ())
                )
            )
        )
        structural_reasons = {
            str(reason) for reason in occurrence.get("unresolved_reasons") or ()
            if str(reason).startswith("occurrence.authority.")
            or str(reason) in {
                "occurrence.actor_unbound", "occurrence.quoted", "occurrence.metaphorical",
                "occurrence.polarity_unbound", "occurrence.actuality_unbound",
                "occurrence.multiple_targets",
            }
            # A richer source-bounded embedding already carries an independently recognized
            # testimony, memory, belief, question, or attribution constraint.  Preserve its
            # established recognition_only disposition instead of degrading it to an unresolved
            # hold; the occurrence reason remains in the graph as corroborating construction
            # evidence.  Gerund/infinitive representations with no such proof stay fail-closed.
            and not (
                str(reason) == "occurrence.actuality_unbound" and embedding is not None
            )
            and not (
                str(reason) == "occurrence.actuality_unbound" and roleplay_past_check
            )
        }
        frame.ambiguity = sorted(set(frame.ambiguity) | structural_reasons)
    if embedding is not None and embedding.get("question"):
        frame.modality = "question"

    occurrence_actors = occurrence.get("actors") if occurrence is not None else None
    if isinstance(occurrence_actors, list):
        for binding in occurrence_actors:
            span = binding.get("span") if isinstance(binding, dict) else None
            if binding.get("identity") == player_eid \
                    and isinstance(span, list) and len(span) == 2 \
                    and frame.start <= span[0] < span[1] <= frame.end:
                frame.add_evidence("actor", span[0], span[1], player_eid)
    else:
        actor = re.compile(r"\b(?:I|we)\b", re.IGNORECASE).search(
            detector, frame.start, frame.end,
        )
        if actor:
            frame.add_evidence("actor", actor.start(), actor.end(), player_eid)
    capability_evidence = ({frame.capability_id} if frame.capability_id else set(frame.ambiguity))
    for candidate in frame.candidates:
        if candidate.capability_id in capability_evidence \
                and frame.start <= candidate.start and candidate.end <= frame.end:
            frame.add_evidence(
                "capability", candidate.start, candidate.end, candidate.capability_id
            )

    target_candidates: set[str] = set()
    possessor_candidates: set[str] = set()
    body_loci: list[tuple[frozenset[str], _EntityPatientRole]] = []
    possession = None
    references = _present_npc_references(state, player_eid)
    # Group identical mention spans so aliases and derived short names cannot double-count.
    grouped: dict[tuple[int, int], set[str]] = {}
    for start, end, _reference, entity_ids in _reference_spans(references, detector):
        if start < frame.start or end > frame.end:
            continue
        grouped.setdefault((start, end), set()).update(entity_ids)

    # Combat rows are state-owned identities, not world NPC entities.  Classify the exact source
    # role first, then resolve only the isolated owner/patient span through the total reference API.
    # Every non-live status remains distinct; none can degrade into a guessed target.
    from .state import CombatReferenceStatus, resolve_combat_reference

    combat_reference_labels: dict[str, str] = {}
    combat_action_spans = [
        row for row in _combat_transition_spans(detector)
        if frame.start <= row[0] and row[1] <= frame.end
    ] if frame.action_class in {
        "weapon_attack", "grapple", "kill_attempt", "grand_kill_attempt",
    } else []
    combat_reference_options = []
    for target_start, target_end in _combat_reference_surface_spans(
            state, detector, frame.start, frame.end):
        role_options: list[tuple[int, _EntityPatientRole]] = []
        for action_start, action_end, _physical in combat_action_spans:
            role = _entity_patient_role(
                detector, action_end, target_start, target_end, grammar,
            )
            if role is None and _combat_opening_target_role(
                    detector, action_end, target_start, target_end, grammar):
                score = _combat_opening_target_score(
                    detector, action_start, action_end, target_start, target_end, grammar,
                )
                if score > 0:
                    role = _EntityPatientRole("direct_patient", True)
            if role is not None:
                role_options.append((action_end, role))
        # Nearest preceding action owns the grammatical relation.  Crucially, this occurs before
        # the resolver sees the entity text, so genitive essential-self and ordinary possession
        # cannot be flattened into the same target query.
        role = max(role_options, key=lambda row: row[0])[1] if role_options else None
        result = resolve_combat_reference(
            state, detector[target_start:target_end],
        )
        combat_reference_options.append((
            target_start, target_end, role, result,
        ))

    def _combat_reference_option_key(row):
        start, end, _role, result = row
        length = end - start
        known = result.status is not CombatReferenceStatus.UNKNOWN \
            and result.match_kind != "unique_token_subset"
        return (0 if known else 1, -length if known else length, start, end)

    selected_combat_references = []
    for option in sorted(combat_reference_options, key=_combat_reference_option_key):
        start, end, _role, _result = option
        if any(start < kept_end and kept_start < end
               for kept_start, kept_end, _kept_role, _kept_result
               in selected_combat_references):
            continue
        selected_combat_references.append(option)
    selected_combat_references.sort(key=lambda row: (row[0], row[1]))

    # The first prep-bound combat row has direct-patient grammar.  Later arms in the same written
    # target list are intentionally scored lower by the ordinary single-target heuristic, so
    # preserve the shared coordination grammar here before identity sets can collapse distinct
    # source spans (including the same row written twice) into one apparent patient.
    semantic_action_starts = {
        match.start
        for match in _visible_meaning_matches(
            meaning, "action", frame.start, frame.end, detector,
        )
    }
    semantic_action_starts.update(start for start, _end, _physical in combat_action_spans)
    qualified_combat_references = []
    last_patient_span: tuple[int, int] | None = None
    for start, end, role, result in selected_combat_references:
        if role is None and last_patient_span is not None:
            bridge = detector[last_patient_span[1]:start]
            renewed_subject = any(
                end <= action_start
                and detector[end:action_start].strip() == ""
                for action_start in semantic_action_starts
            ) or _OCCURRENCE_ENTITY_SUBJECT_PREDICATE_RE.match(detector[end:]) is not None
            if target_coordination_bridge_kind(bridge) is not None and not renewed_subject:
                role = _EntityPatientRole("coordinated_patient", True)
        if role is None:
            continue
        qualified_combat_references.append((start, end, role, result))
        if role.patient:
            last_patient_span = (start, end)
    selected_combat_references = qualified_combat_references

    patient_spans = sorted({
        (start, end)
        for start, end, role, _result in selected_combat_references
        if role.patient
    })
    coordinated_patients = any(
        left_end <= right_start
        and target_coordination_bridge_kind(detector[left_end:right_start]) is not None
        for index, (_left_start, left_end) in enumerate(patient_spans)
        for right_start, _right_end in patient_spans[index + 1:]
    )
    if coordinated_patients:
        frame.ambiguity = sorted(
            set(frame.ambiguity) | {"occurrence.multiple_targets"}
        )

    for start, end, _role, result in selected_combat_references:
        for existing_span in list(grouped):
            if start < existing_span[1] and existing_span[0] < end:
                del grouped[existing_span]
        if result.match_kind == "unique_token_subset":
            reason = "combat_reference.unknown"
        elif result.status is CombatReferenceStatus.RESOLVED:
            selected = result.selected
            combatant_id = selected.combatant_id if selected is not None else None
            if combatant_id:
                grouped[(start, end)] = {combatant_id}
                combat_reference_labels[combatant_id] = selected.label
                frame.add_evidence(
                    "target_reference", start, end, f"resolved:{selected.label}",
                )
                continue
            reason = "combat_reference.unknown"
        elif result.status is CombatReferenceStatus.AMBIGUOUS:
            reason = "combat_reference.ambiguous"
        elif result.status is CombatReferenceStatus.DEFEATED:
            reason = "combat_reference.defeated"
        elif result.status is CombatReferenceStatus.QUEUED:
            reason = "combat_reference.queued"
        elif result.status is CombatReferenceStatus.UNKNOWN:
            reason = "combat_reference.unknown"
        else:  # A future enum value must remain non-executable until this boundary is extended.
            reason = "combat_reference.unknown"
        frame.ambiguity = sorted(set(frame.ambiguity) | {reason})
        labels = "|".join(candidate.label for candidate in result.candidates)
        frame.add_evidence(
            "target_reference",
            start,
            end,
            f"{result.status.value}:{labels}" if labels else result.status.value,
        )

    # Graph-owned target roles precede all heuristic fallbacks.  Each binding is still rechecked
    # against this frame's exact node span, making cross-occurrence field lending impossible even
    # if a malformed caller tries to attach a neighboring node later.
    if occurrence is not None:
        for binding in occurrence.get("targets") or ():
            span = binding.get("span") if isinstance(binding, dict) else None
            identity = str(binding.get("identity") or "") \
                if isinstance(binding, dict) else ""
            if identity.startswith(_COMBAT_OCCURRENCE_REFERENCE_PREFIX):
                continue
            if identity and isinstance(span, list) and len(span) == 2 \
                    and frame.start <= span[0] < span[1] <= frame.end:
                target_candidates.add(identity)

    # A Phrasebook binding is already an exact semantic role. Resolve it against the present
    # reference index rather than asking combat to rediscover the target later.
    for candidate in frame.candidates:
        if candidate.start < frame.start or candidate.end > frame.end:
            continue
        target_ref = _norm_phrase(candidate.target)
        if target_ref and target_ref in references:
            target_candidates.update(references[target_ref])

    # For ordinary direct attacks, reuse the conservative patient-role grammar inside this one
    # interpreter. Incidental mentions after `while/when/as` score zero.
    if frame.action_class in (
            "weapon_attack", "grapple", "kill_attempt", "grand_kill_attempt"):
        action_spans = [
            row for row in _combat_transition_spans(detector)
            if frame.start <= row[0] and row[1] <= frame.end
        ]
        if frame.action_class in ("kill_attempt", "grand_kill_attempt"):
            action_spans = [
                (match.start(), match.end(), True)
                for match in (*list(_KILL_VERBS.finditer(detector)),
                              *list(_GRAND.finditer(detector)))
                if frame.start <= match.start() and match.end() <= frame.end
            ]
        for action_start, action_end, _physical in action_spans:
            for (target_start, target_end), entity_ids in grouped.items():
                patient_role = _entity_patient_role(
                    detector, action_end, target_start, target_end, grammar,
                )
                if patient_role is not None and patient_role.patient:
                    body_loci.append((frozenset(entity_ids), patient_role))
                if not _combat_opening_target_role(
                        detector, action_end, target_start, target_end, grammar):
                    continue
                score = _combat_opening_target_score(
                    detector, action_start, action_end, target_start, target_end, grammar)
                if score > 0:
                    target_candidates.update(entity_ids)

    for (start, end), entity_ids in sorted(grouped.items()):
        named_role = _named_genitive_role(detector, end, grammar)
        if named_role is not None and named_role.patient \
                and frame.action_class in {
                    "weapon_attack", "grapple", "kill_attempt", "grand_kill_attempt",
                }:
            # The nominal is a body locus, not a possessed instrument. Its owner was admitted as
            # a patient above and must not also be projected as an item possessor.
            continue
        owned = grammar.possession_re.match(detector[end:frame.end])
        if owned is None:
            continue
        object_name = owned.group("object").lower()
        relevant = frame.action_class == "inspection" \
            or object_name in grammar.body_parts \
            or _semantic_armament_object(object_name, grammar) \
            or named_role is not None
        if not relevant:
            continue
        possessor_candidates.update(entity_ids)
        # Inspection retains its established entity-context contract, but a harmful action never
        # promotes a possessed object's owner into the HP-patient role.
        if frame.action_class == "inspection":
            target_candidates.update(entity_ids)
        if possession is not None:
            continue
        object_start = end + owned.start("object")
        object_end = end + owned.end("object")
        part_start = end + owned.start("part") if owned.group("part") else None
        part_end = end + owned.end("part") if owned.group("part") else None
        possession = (
            start,
            end,
            object_start,
            object_end,
            object_name,
            part_start,
            part_end,
            (owned.group("part") or "").lower(),
            frozenset(entity_ids),
        )

    # A third-person possessive body locus may resolve to one exact named possessor already bound
    # inside this same occurrence (Root V: ``Iven's polehammer ... his ribs``). The pronoun does
    # not resolve from a lone foe, narrator prose, or insertion order; without exactly one local
    # antecedent it remains unbound. An independently explicit patient always wins instead.
    pronominal_loci = [
        locus
        for locus in grammar.locus_re.finditer(detector, frame.start, frame.end)
        if _norm_phrase(locus.group("pronoun"))
        in {"his", "her", "hers", "its", "their", "theirs"}
    ]
    if not target_candidates and len(possessor_candidates) == 1 and pronominal_loci:
        possessor = next(iter(possessor_candidates))
        locus = pronominal_loci[-1]
        target_candidates.add(possessor)
        body_loci.append((
            frozenset({possessor}),
            _EntityPatientRole(
                "pronominal_body_locus_owner",
                True,
                locus.group("locus").lower(),
                (locus.start("locus"), locus.end("locus")),
            ),
        ))

    if len(possessor_candidates) > 1:
        frame.ambiguity = sorted(set(frame.ambiguity) | possessor_candidates)
    grounded_target_spans = {
        span: set(entity_ids) for span, entity_ids in grouped.items()
    }
    if occurrence is not None:
        for binding in occurrence.get("targets") or ():
            span = binding.get("span") if isinstance(binding, dict) else None
            identity = str(binding.get("identity") or "") \
                if isinstance(binding, dict) else ""
            if identity.startswith(_COMBAT_OCCURRENCE_REFERENCE_PREFIX):
                continue
            if identity and isinstance(span, list) and len(span) == 2 \
                    and frame.start <= span[0] < span[1] <= frame.end:
                grounded_target_spans.setdefault((int(span[0]), int(span[1])), set()).add(
                    identity
                )
    target_candidates = _apply_intent_target_preference(
        frame,
        target_candidates,
        grounded_target_spans,
        intent_preferences,
    )
    capability_ambiguity = set(frame.ambiguity)
    if len(target_candidates) == 1:
        target = next(iter(target_candidates))
        entity = (state.get("entities") or {}).get(target) or {}
        frame.target_entity_id = target
        frame.target_name = str(
            combat_reference_labels.get(target) or entity.get("name") or target
        )
        attack_role = frame.action_class in {
            "weapon_attack", "grapple", "kill_attempt", "grand_kill_attempt",
        }
        mention = next(
            (
                (start, end)
                for (start, end), entity_ids in sorted(grouped.items())
                if target in entity_ids
                and (
                    not attack_role
                    or (role := _named_genitive_role(detector, end, grammar)) is None
                    or role.patient
                )
            ),
            None,
        )
        if mention is not None:
            frame.add_evidence("target", mention[0], mention[1], target)
    elif len(target_candidates) > 1:
        frame.ambiguity = sorted(capability_ambiguity | target_candidates)
        for (start, end), entity_ids in sorted(grouped.items()):
            overlap = sorted(target_candidates & entity_ids)
            if overlap:
                frame.add_evidence("target", start, end, overlap)
    else:
        frame.ambiguity = sorted(capability_ambiguity)

    # A relative phrase is source evidence, not a distance fact.  Bind it only when the combat
    # ledger contains one possible hostile patient, before the binding and frame snapshot freeze.
    _bind_relative_enemy_target(frame, detector, state)

    if frame.target_entity_id:
        matching_loci = sorted(
            {
                (role.locus_span or (frame.start, frame.start), role.locus)
                for entity_ids, role in body_loci
                if frame.target_entity_id in entity_ids and role.locus and role.locus_span
            },
            key=lambda row: (row[0][0], row[0][1], row[1]),
        )
        if matching_loci:
            locus_span, locus_name = matching_loci[-1]
            frame.target_locus = locus_name
            frame.target_locus_owner_id = frame.target_entity_id
            frame.add_evidence(
                "target_locus", locus_span[0], locus_span[1], locus_name,
            )

    if possession is not None:
        mention_start, mention_end, object_start, object_end, object_name, \
            part_start, part_end, part_name, possession_ids = possession
        if object_name in grammar.body_parts:
            frame.target_locus = object_name
            frame.target_locus_owner_id = frame.target_entity_id
            frame.add_evidence("target_locus", object_start, object_end, object_name)
        else:
            frame.possessed_object = object_name
            frame.linguistic_possessor_id = next(iter(possessor_candidates)) \
                if len(possessor_candidates) == 1 else None
            frame.possessed_object_part = part_name
            frame.add_evidence(
                "linguistic_possessor",
                mention_start,
                mention_end,
                frame.linguistic_possessor_id or sorted(possession_ids),
            )
            frame.add_evidence("possessed_object", object_start, object_end, object_name)
            if part_start is not None and part_end is not None:
                frame.add_evidence("possessed_object_part", part_start, part_end, part_name)
            # Grammar proves only a linguistic relation.  Exact item identity and ownership are
            # populated later from a separately committed world-alignment receipt.
            frame.possessed_object_instance_id = None
            frame.possessed_object_owner_id = None

    loci = list(grammar.locus_re.finditer(detector, frame.start, frame.end))
    if frame.action_class == "weapon_attack" and loci:
        locus = loci[-1]
        pronoun = _norm_phrase(locus.group("pronoun"))
        third_person = {"his", "her", "hers", "its", "their", "theirs"}
        locus_owner = frame.target_entity_id if pronoun in third_person else None
        if locus_owner:
            frame.target_locus = locus.group("locus").lower()
            frame.target_locus_owner_id = locus_owner
            frame.add_evidence(
                "target_locus", locus.start("locus"), locus.end("locus"), frame.target_locus
            )

    # A pronoun-targeted strike may name its antecedent earlier in the same action while the
    # prior narrator turn grounds that untracked creature ("rush the ash-wolf ... its neck").
    # Resolve that context here, before the frame freezes; combat never sees the sentence.
    if frame.action_class == "weapon_attack" and not frame.target_entity_id \
            and not frame.target_name and not frame.ambiguity and dm_text:
        clause_text = detector[frame.start:frame.end]
        motion = re.search(
            r"\b(?:rush|charge|advance\s+on|close\s+on|run\s+at)\s+(?:the\s+)?"
            r"(?P<target>[a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,2}?)"
            r"(?=\s*(?:,|\band\b|\bthen\b))",
            clause_text,
            re.IGNORECASE,
        )
        if motion:
            target_norm = _norm_phrase(motion.group("target"))
            dm_norm = " " + _norm_phrase(dm_text) + " "
            if target_norm and f" {target_norm} " in dm_norm:
                frame.target_name = target_norm.title()
                frame.add_evidence(
                    "target",
                    frame.start + motion.start("target"),
                    frame.start + motion.end("target"),
                    frame.target_name,
                )

    if occurrence is not None \
            and frame.action_class in {
                "weapon_attack", "grapple", "combat_opening",
                "kill_attempt", "grand_kill_attempt",
            } \
            and not frame.target_entity_id and not frame.target_name \
            and not frame.ambiguity:
        frame.ambiguity = sorted(set(frame.ambiguity) | {"occurrence.target_unbound"})


_INFERRED_FALLBACK_ACTION_TOPOLOGY = {
    # Brawl is the universal close-combat floor, not a generic substitute for any verb that
    # happens to resemble one of its governs. The already-grounded action topology therefore
    # decides whether an inferred Brawl candidate may proceed to a check.
    "brawl": frozenset({"weapon_attack"}),
}


def _enforce_inferred_fallback_action_topology(frame: ActionFrame) -> None:
    """Fail closed when an inferred fallback conflicts with the frozen action meaning.

    Candidate recognition remains inspectable, but an incidental stem such as ``attacking`` in
    ``study the shield without attacking`` cannot turn an inspection into a Brawl check. Named
    capabilities and exact Phrasebook constructions retain their stronger authored authority.
    """
    capability_id = str(frame.capability_id or "")
    allowed = _INFERRED_FALLBACK_ACTION_TOPOLOGY.get(capability_id.casefold())
    if allowed is None or frame.action_class in allowed:
        return
    candidates = [
        candidate for candidate in frame.candidates
        if candidate.capability_id == capability_id
    ]
    if not candidates:
        return
    winner = max(candidates, key=lambda candidate: candidate.priority)
    if winner.source != "inferred" or not winner.fallback:
        return
    frame.selected = []
    frame.capability_id = None
    frame.invoked_capability_ids = ()
    frame.ambiguity = sorted(set(frame.ambiguity) | {capability_id})


def _enforce_nonperforming_candidate_scope(frame: ActionFrame, text: str) -> None:
    """Retain ``without X`` as evidence without authorizing X as this turn's mechanic."""
    capability_id = str(frame.capability_id or "")
    if not capability_id:
        return
    candidates = [
        candidate for candidate in frame.candidates
        if candidate.capability_id == capability_id
    ]
    if not candidates or any(
        not _WITHOUT_ACTION_PREFIX_RE.search(_clause_prefix(text, candidate.start))
        for candidate in candidates
    ):
        return
    frame.selected = []
    frame.capability_id = None
    frame.invoked_capability_ids = ()
    frame.ambiguity = sorted(set(frame.ambiguity) | {capability_id})


def _enforce_explicit_capability_action_alignment(frame: ActionFrame) -> None:
    """Require source-local capability meaning before an explicit check can cause harm.

    Selecting any owned skill proves which capability the Player wants to use; it does not prove
    that capability supports an independently recognized weapon/grapple/kill action. A second
    candidate for the same capability, found in the natural companion through its name,
    ``governs`` vocabulary, or generic domain translation, is the bounded authorization link.
    """
    if frame.action_class not in NON_SKILL_CHECK_ACTION_CLASSES:
        return
    capability_id = str(frame.capability_id or "")
    if not capability_id:
        return
    explicit = [
        candidate for candidate in frame.candidates
        if candidate.capability_id == capability_id and candidate.source == "explicit"
    ]
    if not explicit:
        return
    aligned = any(
        candidate.capability_id == capability_id and candidate.source != "explicit"
        for candidate in frame.candidates
    )
    if aligned:
        return
    frame.ambiguity = sorted(
        set(frame.ambiguity) | {"capability.action_unbound"}
    )


def _canonical_match_refs(matches) -> list[dict]:
    """Canonicalize exact Semantic Fabric evidence without retaining source prose."""
    unique: dict[tuple, dict] = {}
    for match in matches:
        ref = semantic_match_ref(match)
        key = (
            ref["start"], ref["end"], ref["lex_id"], ref["concept_id"],
            ref["entry_fingerprint"],
        )
        unique[key] = ref
    return [unique[key] for key in sorted(unique)]


def _action_evidence_matches(
    frame: ActionFrame,
    meaning: CompiledMeaning,
    detection_text: str,
) -> tuple:
    """Return only code-adapted ActionLex rows that support the selected action topology."""
    supported = []
    for match in _visible_meaning_matches(
            meaning, "action", frame.start, frame.end, detection_text):
        classes = action_classes_for_matches((match,))
        if frame.action_class in classes:
            supported.append(match)
    return tuple(supported)


def _attach_meaning_binding(
    frame: ActionFrame,
    text: str,
    state: dict,
    meaning: CompiledMeaning,
    *,
    detection_text: str | None = None,
) -> tuple[dict, list[dict]]:
    """Freeze event-local constraints, then independently align any possessed object."""
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else text
    bounded_start, bounded_end, embedding = _bounded_event_span(
        frame, text, meaning, detection_text=detector,
    )
    frame.start, frame.end = bounded_start, bounded_end
    event_node_id = f"event.{frame.frame_id}"
    event_scope_ref = f"scope.event.{frame.frame_id}"
    embedding_scope_ref = f"scope.embedding.{frame.frame_id}"

    embedding_refs: list[dict] = []
    scope_nodes: list[dict] = []
    if embedding is not None:
        embedding_refs = _canonical_match_refs(
            match for match in (embedding["governor"], embedding.get("marker"))
            if match is not None
        )
        scope_nodes.append({
            "scope_ref": embedding_scope_ref,
            "kind": f"{embedding['assertion_context']}_content",
            "span_start": int(embedding["scope_start"]),
            "span_end": int(embedding["scope_end"]),
            "content_start": frame.start,
            "content_end": frame.end,
            "parent_scope_ref": None,
            "construction_role": "content",
            "evidence_refs": embedding_refs,
        })
    scope_nodes.append({
        "scope_ref": event_scope_ref,
        "kind": "event",
        "span_start": frame.start,
        "span_end": frame.end,
        "content_start": frame.start,
        "content_end": frame.end,
        "parent_scope_ref": embedding_scope_ref if embedding is not None else None,
        "construction_role": "ordinary_argument",
        "evidence_refs": [],
    })

    assertion_context = str(
        embedding["assertion_context"] if embedding is not None else "direct"
    )
    constraints = [{
        "constraint_id": f"constraint.assertion.{frame.frame_id}",
        "scope_ref": embedding_scope_ref if embedding is not None else event_scope_ref,
        "target_event_ref": event_node_id,
        "dimension": "assertion_context",
        "value": assertion_context,
        "evidence_refs": embedding_refs,
    }]

    scene_matches = _visible_meaning_matches(
        meaning, "scene", frame.start, frame.end, detector,
    )
    modality_refs = []
    if embedding is not None and embedding.get("question"):
        modality_refs = _canonical_match_refs((embedding["marker"],))
    elif frame.modality in ("hypothetical", "question"):
        modality_refs = _canonical_match_refs(
            match for match in meaning.for_lex("scene")
            if match.concept_id == "scene.cognition.content_clause"
            and frame.start <= match.start and match.end <= frame.end
            and detector[match.start:match.end].strip()
        )
    time_refs = _canonical_match_refs(
        match for match in scene_matches
        if (frame.time_scope == "future" and (
            match.features.get("time_scope") == "future"
            or match.concept_id in {
                "scene.event.anticipated",
                "scene.modality.future_or_plan",
                "scene.time.future",
            }
        )) or (frame.time_scope == "past" and (
            match.features.get("time_scope") == "past"
            or match.concept_id == "scene.time.past"
        ))
    )
    for dimension, value, evidence_refs in (
        ("polarity", frame.polarity, []),
        ("modality", frame.modality, modality_refs),
        ("time_scope", frame.time_scope, time_refs),
    ):
        constraints.append({
            "constraint_id": f"constraint.{dimension}.{frame.frame_id}",
            "scope_ref": event_scope_ref,
            "target_event_ref": event_node_id,
            "dimension": dimension,
            "value": value,
            "evidence_refs": evidence_refs,
        })
    if frame.ambiguity:
        constraints.append({
            "constraint_id": f"constraint.resolution.{frame.frame_id}",
            "scope_ref": event_scope_ref,
            "target_event_ref": event_node_id,
            "dimension": "resolution",
            "value": "unresolved",
            "evidence_refs": [],
        })

    actor_matches = tuple(
        match for match in _visible_meaning_matches(
            meaning, "action", frame.start, frame.end, detector,
        )
        if match.concept_id == "action.frame.first_person_actual"
    )
    action_matches = _action_evidence_matches(frame, meaning, detector)
    actor_refs = _canonical_match_refs(actor_matches)
    action_refs = _canonical_match_refs(action_matches)
    field_provenance = [
        {
            "field": "actor_id",
            "value": str(frame.actor_id or "unresolved"),
            "defaulted": not bool(actor_refs),
            "evidence_refs": actor_refs,
        },
        {
            "field": "capability_id",
            "value": str(frame.capability_id or "unresolved"),
            "defaulted": True,
            "evidence_refs": [],
        },
        {
            "field": "invoked_capability_ids",
            "value": ",".join(frame.invoked_capability_ids) or "none",
            "defaulted": True,
            "evidence_refs": [],
        },
        {
            "field": "action_class",
            "value": str(frame.action_class or "unresolved"),
            "defaulted": not bool(action_refs),
            "evidence_refs": action_refs,
        },
        {
            "field": "polarity",
            "value": frame.polarity,
            "defaulted": True,
            "evidence_refs": [],
        },
        {
            "field": "modality",
            "value": frame.modality,
            "defaulted": not bool(modality_refs),
            "evidence_refs": modality_refs,
        },
        {
            "field": "time_scope",
            "value": frame.time_scope,
            "defaulted": not bool(time_refs),
            "evidence_refs": time_refs,
        },
    ]
    role_evidence = []
    if actor_refs:
        role_evidence.append({"role": "actor", "evidence_refs": actor_refs})
    if action_refs:
        role_evidence.append({"role": "action", "evidence_refs": action_refs})

    binding = build_meaning_binding(
        meaning,
        binding_id=f"binding.{frame.frame_id}",
        event_node_id=event_node_id,
        event_span=(frame.start, frame.end),
        scope_nodes=scope_nodes,
        constraints=constraints,
        constraint_integrity=(
            "conflict" if any(
                item.startswith("action_class.") for item in frame.ambiguity
            ) else "valid"
        ),
        field_provenance=field_provenance,
        role_evidence=role_evidence,
        coordination_edges=(),
    )
    frame.meaning_binding_ref = str(binding["fingerprint"])
    frame.event_node_id = event_node_id
    frame.mechanic_disposition = str(binding["mechanic_disposition"])

    alignments: list[dict] = []
    frame.possessed_object_instance_id = None
    frame.possessed_object_owner_id = None
    if frame.possessed_object:
        alignment = build_possessed_object_alignment(
            state,
            recognition_ref=str(binding["fingerprint"]),
            object_name=frame.possessed_object,
            linguistic_possessor_id=frame.linguistic_possessor_id,
            time_scope=frame.time_scope,
        )
        alignments.append(alignment)
        if alignment["status"] == "positive" and len(alignment["resolved_ids"]) == 1:
            frame.possessed_object_instance_id = str(alignment["resolved_ids"][0])
            frame.possessed_object_owner_id = str(alignment["positive_authority_value"])
    frame.world_alignment_refs = tuple(
        sorted(str(alignment["fingerprint"]) for alignment in alignments)
    )
    return binding, alignments


def _can_seek_world_target(
    frame: ActionFrame,
    text: str,
    meaning: CompiledMeaning | None,
    detection_text: str,
) -> bool:
    """Whether a direct event may ask world alignment for a target candidate.

    This is not mechanical admission. It only prevents a report, question, quotation, future
    plan, or ambiguous event from borrowing a narrator-grounded target before its binding exists.
    """
    _start, _end, embedding = _bounded_event_span(
        frame, text, meaning, detection_text=detection_text,
    )
    # ``occurrence.target_unbound`` is a provisional construction result: the Player occurrence
    # contains a direct harmful action but no already-ledgered target identity.  It is precisely
    # the case this next, source-bounded world-grounding step exists to resolve.  Every other
    # ambiguity remains a hard stop, including quoted/nonliteral scope and competing identities.
    unresolved = set(frame.ambiguity)
    return bool(
        frame.actor_id
        and frame.capability_id
        and frame.action_class in ("weapon_attack", "grapple")
        # A named possessor is explicit evidence that the mentioned person is not the direct
        # patient. Do not let the later narrator-grounding lane erase that grammatical role by
        # borrowing the same person as an HP target. Typed body/essential-self patients never
        # populate ``possessed_object`` and therefore remain eligible for exact grounding.
        and not (frame.linguistic_possessor_id and frame.possessed_object)
        and frame.polarity == "positive"
        and frame.modality in ("actual", "command")
        and frame.time_scope == "current"
        and unresolved <= {"occurrence.target_unbound"}
        and embedding is None
    )


def _semantic_candidate_anchor(
    frame: ActionFrame,
    capability_id: str,
) -> tuple[str, str, int, int] | None:
    """Return a source-stable handle for one capability occurrence inside a frame.

    ``SemanticTurn.resolve`` may legitimately renumber frames after another detector contributes
    a late candidate.  A provisional ``fN`` therefore cannot be the association key retained by
    a check.  Candidate source + exact span + capability remains stable across that re-resolution
    and also distinguishes two independent uses of the same capability in one message.
    """
    anchors = sorted(
        (
            str(candidate.capability_id),
            str(candidate.source),
            int(candidate.start),
            int(candidate.end),
        )
        for candidate in frame.candidates
        if candidate.capability_id == capability_id
    )
    return anchors[0] if anchors else None


def _scope_semantic_occurrence(res: Tier0Result, occurrence_turn: int) -> None:
    """Make otherwise identical semantic events distinct across committed turns.

    Local frame ordinals (``f1``, ``f2``) are deterministic within one interpretation, but they
    are not occurrence identities.  Prefixing them with the code-owned turn keeps an exact retry
    on the same turn idempotent while allowing the same sentence to be performed again later.
    Checks rebind from their immutable candidate span before receiving the turn-scoped frame ID.
    This survives late candidate discovery and repeated uses of the same capability in one turn.
    """
    frames = res.semantic_turn.frames if res.semantic_turn is not None else []
    if not frames:
        return
    turn_id = max(0, int(occurrence_turn))
    remap: dict[str, str] = {}
    for ordinal, frame in enumerate(frames, 1):
        local = str(frame.frame_id or f"f{ordinal}")
        scoped = f"t{turn_id}.{local}"
        remap[local] = scoped
        frame.frame_id = scoped
    if res.semantic_turn is not None:
        for preference in res.semantic_turn.interpretation_preferences:
            local = str(preference.get("frame_id") or "")
            if local in remap:
                preference["frame_id"] = remap[local]
    for check in res.checks:
        anchor = check.pop("_semantic_frame_anchor", None)
        rebound = [
            frame for frame in frames
            if isinstance(anchor, tuple)
            and len(anchor) == 4
            and any(
                (
                    str(candidate.capability_id),
                    str(candidate.source),
                    int(candidate.start),
                    int(candidate.end),
                ) == anchor
                for candidate in frame.candidates
            )
        ]
        if len(rebound) == 1:
            check["_semantic_frame_id"] = rebound[0].frame_id
            continue
        local = str(check.get("_semantic_frame_id") or "")
        if local in remap:
            check["_semantic_frame_id"] = remap[local]


def _bind_explicit_check_target(frame: ActionFrame, check: dict, text: str,
                                state: dict) -> None:
    """Bind a structured check target into the same frame used by its prose context."""
    token = str(check.get("target") or "").strip()
    if not token:
        return
    from .state import (
        CombatReferenceStatus,
        resolve_combat_reference,
        resolve_entity_ref,
    )

    target_id: Optional[str] = None
    target_name: Optional[str] = None
    combat_reference = resolve_combat_reference(state, token)
    if combat_reference.match_kind != "unique_token_subset" \
            and combat_reference.status is CombatReferenceStatus.RESOLVED \
            and combat_reference.selected is not None \
            and combat_reference.selected.combatant_id:
        target_id = combat_reference.selected.combatant_id
        target_name = combat_reference.selected.label
    elif combat_reference.status is CombatReferenceStatus.AMBIGUOUS:
        candidates = [
            candidate.combatant_id
            for candidate in combat_reference.candidates
            if candidate.state == "active" and candidate.combatant_id
        ] or ["combat_reference.ambiguous"]
        frame.ambiguity = sorted(
            (
                set(frame.ambiguity)
                - {"occurrence.target_unbound", "combat_reference.ambiguous"}
            )
            | set(candidates)
        )
        span = check.get("_target_span")
        if isinstance(span, tuple) and len(span) == 2:
            frame.add_evidence("target", int(span[0]), int(span[1]), candidates)
            labels = "|".join(
                candidate.label for candidate in combat_reference.candidates
            )
            frame.add_evidence(
                "target_reference",
                int(span[0]),
                int(span[1]),
                f"ambiguous:{labels}",
            )
        return
    elif combat_reference.status is CombatReferenceStatus.DEFEATED:
        frame.ambiguity = sorted(
            (set(frame.ambiguity) - {"occurrence.target_unbound"})
            | {"combat_reference.defeated"}
        )
        return
    elif combat_reference.status is CombatReferenceStatus.QUEUED:
        frame.ambiguity = sorted(
            (set(frame.ambiguity) - {"occurrence.target_unbound"})
            | {"combat_reference.queued"}
        )
        return
    elif combat_reference.status is CombatReferenceStatus.UNKNOWN:
        eid = resolve_entity_ref(state, token)
        if eid:
            row = (state.get("entities") or {}).get(eid) or {}
            target_id = str(eid)
            target_name = str(row.get("name") or token)
        elif ((state.get("combat") or {}).get("active")):
            frame.ambiguity = sorted(
                (set(frame.ambiguity) - {"occurrence.target_unbound"})
                | {"combat_reference.unknown"}
            )
            return
    if not target_id:
        return

    # The occurrence graph cannot resolve combat-row labels because those rows are not world
    # entities.  An exact structured-check target is a later, stricter binding: once it resolves
    # to one live row, retire only that graph-level placeholder instead of letting it veto the
    # exact ``Baser Hollow #2`` identity.
    frame.ambiguity = [
        reason for reason in frame.ambiguity
        if reason != "occurrence.target_unbound"
    ]
    existing = frame.target_entity_id
    if existing and existing != target_id:
        frame.ambiguity = sorted(set(frame.ambiguity) | {existing, target_id})
        frame.target_entity_id = None
        frame.target_name = None
    elif not frame.ambiguity:
        frame.target_entity_id = target_id
        frame.target_name = target_name
    span = check.get("_target_span")
    if isinstance(span, tuple) and len(span) == 2:
        frame.add_evidence("target", int(span[0]), int(span[1]), target_id)


def _add_grounded_target_evidence(frame: ActionFrame, text: str, name: str,
                                  target_id: str) -> None:
    """Attach the shortest exact source span that names a prose-grounded target."""
    normalized = _norm_phrase(name)
    spans = [
        (start, end)
        for start, end in (_candidate_spans(normalized, text, False) if normalized else [])
        if frame.start <= start and end <= frame.end
    ]
    if spans:
        start, end = spans[0]
        frame.add_evidence("target", start, end, target_id)


def _explicit_check_companion_candidates(
    text: str,
    detector: str,
    state: dict,
    player_eid: str,
    skill_id: str,
    skill: dict,
    rank: int,
    command_clause: int,
    *,
    stem_aware: bool,
) -> list[CapabilityCandidate]:
    """Keep the visible action beside a structured capability as construction evidence.

    ``aether.check`` selects the owned capability, so Tier-0 deliberately does not run the
    independent natural-check detector as well.  The natural companion still owns grammatical
    roles, however.  Without its same-capability anchor, ``I burn both Ana and Bo`` left the only
    semantic span inside the masked command and silently lost both grounded patients.  Reuse the
    ordinary owned-skill vocabulary here, but only for this exact capability, this exact clause,
    and outside all structured command spans.  The explicit candidate retains selection priority;
    these rows are source evidence and can never mint a second check.
    """
    msg = " " + _norm_phrase(detector) + " "
    msg_stems = _stem_seq(detector) if stem_aware else []
    command_spans = [(match.start(), match.end()) for match in OOC_RE.finditer(text)]
    presentation_spans = [
        (match.start(), match.end())
        for match in re.finditer(
            r"```.*?```|~~~.*?~~~|`[^`\r\n]+`|"
            r"(?m:^[ \t]*>[^\r\n]*(?:\r?\n[ \t]*>[^\r\n]*)*)",
            text,
            re.DOTALL,
        )
    ]
    phrases: dict[str, str] = {}
    for value in (skill_id, skill.get("name")):
        normalized = _norm_phrase(value)
        if len(normalized) >= 3:
            phrases[normalized] = "named"
    capability_tokens = {
        token
        for value in (skill_id, skill.get("name"), *(skill.get("governs") or ()))
        for token in _norm_phrase(value).split()
    }
    for marker, translations in _CAPABILITY_DOMAIN_TRANSLATIONS.items():
        if not any(_capability_domain_token(token, marker) for token in capability_tokens):
            continue
        for translation in translations:
            phrases.setdefault(translation, "inferred")
    for value in skill.get("governs") or ():
        normalized = _norm_phrase(value)
        if len(normalized) < 3:
            continue
        phrases.setdefault(normalized, "inferred")
        if stem_aware:
            for synonym in _INTENT_SYN.get(normalized, ()):
                synonym_norm = _norm_phrase(synonym)
                if len(synonym_norm) >= 3:
                    phrases.setdefault(synonym_norm, "inferred")

    by_span: dict[tuple[int, int, int], CapabilityCandidate] = {}
    for phrase, source in phrases.items():
        for start, end, clause in _semantic_phrase_spans(
            phrase,
            detector,
            state,
            player_eid,
            msg,
            msg_stems,
            stem_aware,
        ):
            if clause != command_clause or any(
                command_start < end and start < command_end
                for command_start, command_end in command_spans
            ) or any(
                presentation_start <= start and end <= presentation_end
                for presentation_start, presentation_end in presentation_spans
            ):
                continue
            candidate = CapabilityCandidate(
                capability_id=skill_id,
                kind="skill",
                source=source,
                phrase=text[start:end],
                start=start,
                end=end,
                clause_index=clause,
                rank=rank,
                fallback=skill_id.casefold() == "brawl",
            )
            key = (start, end, clause)
            prior = by_span.get(key)
            if prior is None or candidate.priority > prior.priority:
                by_span[key] = candidate
    return [by_span[key] for key in sorted(by_span)]


_COMPOSED_MANEUVER_BOUNDARY_RE = re.compile(
    r",\s*then\s+|;\s*then\s+|\band\s+then\s+",
    re.IGNORECASE,
)
_COMPOSED_BRACE_RE = re.compile(
    r"\b(?:brace|braces|braced|bracing)\b",
    re.IGNORECASE,
)
_COMPOSED_ABSTRACT_TOPIC_SETUP_RE = re.compile(
    r"^\s*(?:(?:i|we)\s+)?(?:rain|rains|rained|raining)\s+down\b[^.!?;\n]*$",
    re.IGNORECASE,
)


def _merged_overlap_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse duplicate/nested recognition of one harmful construction."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _composed_abstract_topic_setup(text: str) -> bool:
    """Recognize one explicitly non-impact topical preface before an exact sequence boundary."""
    if _COMPOSED_ABSTRACT_TOPIC_SETUP_RE.fullmatch(text or "") is None \
            or not _abstract_communication_tokens(text) \
            or _ABSTRACT_TOPIC_PREPOSITION_RE.search(text) is None:
        return False
    return not _has_attack_intent(text)


def _composed_maneuver_detection_view(
    text: str,
    detector: str,
    state: dict,
    player_eid: str,
    reg,
    player: dict,
    checks: list[dict],
    *,
    stem_aware: bool,
) -> str:
    """Join one exact non-impact setup to its adjacent harmful action before construction.

    A single structured check may describe one composed maneuver (``parry, then strike``) or a
    harmless topical preface followed by the real action. Punctuation is not authority to lend a
    capability across arbitrary events. Mask only the exact separator, preserving every source
    offset, after the same selected capability is proved inside exactly one adjacent harmful
    construction. Every later interpreter therefore sees one honest occurrence; no post-hoc frame
    or target evidence crosses a graph node.
    """
    if len(checks) != 1:
        return detector
    commands = list(OOC_RE.finditer(text))
    if len(commands) != 1 \
            or not commands[0].group(1).strip().lower().startswith("aether.check"):
        return detector
    check = checks[0]
    command_span = check.get("_command_span")
    if not (isinstance(command_span, tuple) and len(command_span) == 2) \
            or tuple(map(int, command_span)) != (commands[0].start(), commands[0].end()):
        return detector
    skill_id = reg.resolve_skill(str(check.get("skill") or ""), player)
    if skill_id is None:
        return detector
    try:
        rank = int((player.get("skills") or {}).get(skill_id, 0) or 0)
    except (TypeError, ValueError):
        rank = 0

    boundaries = [
        match for match in _COMPOSED_MANEUVER_BOUNDARY_RE.finditer(
            detector, commands[0].end(),
        )
    ]
    if not boundaries:
        return detector
    boundary = boundaries[0]
    setup = detector[commands[0].end():boundary.start()]
    if re.search(r"[.!?;\n]", setup):
        return detector
    defenses = [
        *list(_DEFENSIVE_PREDICATE_RE.finditer(setup)),
        *list(_COMPOSED_BRACE_RE.finditer(setup)),
    ]
    abstract_topic_setup = _composed_abstract_topic_setup(setup)
    if not abstract_topic_setup and len(defenses) != 1:
        return detector
    if not abstract_topic_setup:
        defense = defenses[0]
        if re.fullmatch(
                r"\s*(?:(?:i|we)\s+)?", setup[:defense.start()], re.IGNORECASE) is None:
            return detector
    if any(
            _has_attack_intent(setup[start:end])
            for start, end, _physical in _combat_transition_spans(setup)
        ):
        return detector

    event_tail = detector[boundary.end():]
    event_stop = re.search(r"[.!?;\n]", event_tail)
    if event_stop is not None:
        event_tail = event_tail[:event_stop.start()]
    event_end = boundary.end() + len(event_tail)
    if any(match.start() < event_end for match in boundaries[1:]):
        return detector
    # Lethal declarations own a separate settlement contract and cannot hitchhike on this bounded
    # weapon/capability composition rule.
    if _KILL_VERBS.search(event_tail) or _GRAND.search(event_tail):
        return detector
    harmful = _merged_overlap_spans([
        (start, end)
        for start, end, _physical in _combat_transition_spans(event_tail)
        if _has_attack_intent(event_tail[start:end])
    ])
    if len(harmful) != 1:
        return detector
    action_start, action_end = harmful[0]
    if re.fullmatch(
            r"\s*(?:(?:i|we)\s+)?", event_tail[:action_start], re.IGNORECASE) is None:
        return detector

    boundary_width = boundary.end() - boundary.start()
    # Normalize the sequencing surface to ordinary coordination without moving any evidence
    # offset. Plain ``and`` keeps the later verb a performed predicate for the defensive-nominal
    # filter, while unlike `and then`/punctuation it does not split the occurrence graph.
    joined_boundary = " and " + " " * (boundary_width - len(" and "))
    trial = (
        detector[:boundary.start()]
        + joined_boundary
        + detector[boundary.end():]
    )
    skill_span = check.get("_skill_span")
    if not (isinstance(skill_span, tuple) and len(skill_span) == 2):
        skill_span = command_span
    command_clause = sum(
        1 for _ in _CLAUSE_BOUNDARY_RE.finditer(trial[:int(skill_span[0])])
    )
    skill = reg.skill_entry(skill_id, player)
    action_start += boundary.end()
    action_end += boundary.end()
    aligned = any(
        action_start <= candidate.start and candidate.end <= action_end
        for candidate in _explicit_check_companion_candidates(
            text,
            trial,
            state,
            player_eid,
            skill_id,
            skill,
            rank,
            command_clause,
            stem_aware=stem_aware,
        )
    )
    return trial if aligned else detector


def _interpret_explicit_checks(text: str, state: dict, cfg, res: Tier0Result,
                               dm_text: str = "",
                               detection_text: str | None = None) -> None:
    """Project ``aether.check`` declarations through the canonical ActionFrame contract.

    The command chooses a capability; the surrounding Player action supplies action class and
    entity roles.  Cost, damage, combat opening, and replay then consume this frozen object just
    like a natural-language declaration instead of independently rereading the sentence.
    """
    semantic_turn = res.semantic_turn
    if semantic_turn is None or semantic_turn.source_text != text:
        semantic_turn = _new_semantic_turn(text, state, res)
        res.semantic_turn = semantic_turn
    if semantic_turn.compiled_meaning is None:
        for check in res.checks:
            check["_semantic_abstain"] = True
        return
    reg = registry.load(cfg)
    player_eid, player = _player_card(state)
    if not player_eid or not player:
        return
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else _action_text(text)
    stem_aware = bool(getattr(getattr(cfg, "specialization", None), "intent_floor", True))
    detector = _composed_maneuver_detection_view(
        text,
        detector,
        state,
        player_eid,
        reg,
        player,
        res.checks,
        stem_aware=stem_aware,
    )

    for check in res.checks:
        sid = reg.resolve_skill(str(check.get("skill") or ""), player)
        span = check.get("_skill_span")
        command_span = check.get("_command_span")
        if sid is None:
            continue
        if not (isinstance(span, tuple) and len(span) == 2):
            span = command_span
        if not (isinstance(span, tuple) and len(span) == 2):
            continue
        start, end = int(span[0]), int(span[1])
        if start < 0 or end <= start or end > len(text):
            continue
        try:
            rank = int((player.get("skills") or {}).get(sid, 0) or 0)
        except (TypeError, ValueError):
            rank = 0
        invoked: list[str] = []
        invoked_evidence: list[tuple[str, int, int]] = []
        spans_by_reference = {
            str(reference).casefold(): (int(evidence_start), int(evidence_end))
            for reference, evidence_start, evidence_end in check.get("_use_spans") or ()
        }
        for reference in check.get("use") or ():
            ability_id, ability = _find_active_ability(reg, player, str(reference))
            if ability_id is None or ability is None \
                    or not registry.ability_is_active(ability):
                continue
            canonical_id = str(ability_id)
            invoked.append(canonical_id)
            evidence_span = spans_by_reference.get(str(reference).casefold())
            if evidence_span is not None:
                invoked_evidence.append((canonical_id, *evidence_span))
        semantic_turn.add_candidate(CapabilityCandidate(
            capability_id=sid,
            kind="command",
            source="explicit",
            phrase=text[start:end],
            start=start,
            end=end,
            clause_index=sum(1 for _ in _CLAUSE_BOUNDARY_RE.finditer(text[:start])),
            rank=rank,
            use=tuple(sorted(set(invoked))),
        ))
        command_clause = sum(1 for _ in _CLAUSE_BOUNDARY_RE.finditer(text[:start]))
        skill = reg.skill_entry(sid, player)
        for companion in _explicit_check_companion_candidates(
            text,
            detector,
            state,
            player_eid,
            sid,
            skill,
            rank,
            command_clause,
            stem_aware=stem_aware,
        ):
            semantic_turn.add_candidate(companion)
        check["_canonical_skill"] = sid
        check["_semantic_candidate_span"] = (start, end)
        check["_semantic_invoked_capability_evidence"] = invoked_evidence

    _partition_semantic_turn_by_occurrence(
        semantic_turn,
        text,
        state,
        player_eid,
        detection_text=detector,
    )
    semantic_turn.resolve()
    for frame in semantic_turn.frames:
        _ground_action_frame(
            frame,
            text,
            state,
            player_eid,
            dm_text=dm_text,
            meaning=semantic_turn.compiled_meaning,
            detection_text=detector,
            intent_preferences=semantic_turn.interpretation_preferences,
        )
        _enforce_explicit_capability_action_alignment(frame)
        # The structured declaration selects a capability; it does not erase the actuality of
        # its natural-language companion. Only an event-local actual occurrence may become an
        # executable command. Modal, hypothetical, quoted, metaphorical, represented, or future
        # companions keep the classification already established by occurrence construction.
        occurrence = frame.occurrence if isinstance(frame.occurrence, dict) else {}
        if occurrence.get("actuality") == "actual":
            frame.modality = "command"
        for candidate in frame.candidates:
            if candidate.source == "explicit":
                frame.add_evidence(
                    "command", candidate.start, candidate.end, candidate.capability_id
                )

    for check in res.checks:
        sid = check.get("_canonical_skill")
        span = check.get("_semantic_candidate_span")
        if not sid or not (isinstance(span, tuple) and len(span) == 2):
            continue
        frame = next(
            (
                item for item in semantic_turn.frames
                if any(candidate.source == "explicit"
                       and candidate.capability_id == sid
                       and candidate.start == span[0]
                       and candidate.end == span[1]
                       for candidate in item.candidates)
            ),
            None,
        )
        if frame is None:
            continue
        _bind_explicit_check_target(frame, check, text, state)
        frame.declared_modifier = int(check.get("mod", 0) or 0)
        for modifier, evidence_start, evidence_end in check.get(
                "_modifier_evidence") or ():
            frame.add_evidence(
                "declared_modifier", evidence_start, evidence_end, int(modifier),
            )
        for ability_id, evidence_start, evidence_end in check.get(
                "_semantic_invoked_capability_evidence") or ():
            frame.add_evidence(
                "invoked_capability", evidence_start, evidence_end, ability_id,
            )
        check["_semantic_frame_id"] = frame.frame_id
        check["_semantic_frame_anchor"] = _semantic_candidate_anchor(frame, str(sid))
        check["_attack"] = frame.action_class in (
            "weapon_attack", "grapple", "combat_opening",
        )
        if not frame.mechanically_actionable:
            check["_semantic_abstain"] = True


def _primary_action_frame(turn: Optional[SemanticTurn]) -> Optional[ActionFrame]:
    if turn is None:
        return None
    candidates = [frame for frame in turn.frames if frame.capability_id or frame.ambiguity]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda frame: (
            0 if any(candidate.source == "named" for candidate in frame.candidates) else 1,
            frame.start,
            frame.frame_id,
        ),
    )


def _append_combat_transition_frame(
    turn: SemanticTurn,
    text: str,
    player_eid: str,
    assessment: CombatOpeningAssessment,
) -> ActionFrame:
    """Freeze an unrolled but deliberate combat transition beside any skill-check frame."""
    meaning = turn.compiled_meaning
    if meaning is None:
        raise ValueError("combat transition requires compiled semantic meaning")
    if assessment.target is None or assessment.clause_index is None \
            or assessment.action_span is None or assessment.target_span is None:
        raise ValueError("combat transition requires detector-owned source evidence")
    eid, name = assessment.target
    action_start, action_end = assessment.action_span
    target_start, target_end = assessment.target_span
    if not (0 <= action_start < action_end <= len(text)) \
            or not (0 <= target_start < target_end <= len(text)):
        raise ValueError("combat transition source evidence is invalid")
    frame = ActionFrame(
        frame_id=f"f{len(turn.frames) + 1}",
        clause_index=assessment.clause_index,
        start=min(action_start, target_start),
        end=max(action_end, target_end),
        selected=["combat_transition"],
        actor_id=player_eid,
        capability_id="combat_transition",
        action_class="combat_opening",
        target_entity_id=eid,
        target_name=name,
        polarity="positive",
        modality="actual",
        time_scope="current",
        genre_ids=meaning.genre_ids,
        meaning_ref=str(meaning.receipt_dict()["fingerprint"]),
        fabric_fingerprint=meaning.fabric_fingerprint,
    )
    detector = _action_text(text)
    detector = OOC_RE.sub(lambda match: " " * len(match.group(0)), detector)
    graph = turn.occurrence_graph
    if graph is None:
        graph = build_occurrence_graph(
            text,
            detection_text=detector,
            anchors=(
                OccurrenceAnchor(
                    "actor", player_eid, action_start, action_end, "turn_speaker",
                ),
                OccurrenceAnchor(
                    "action", "combat_opening", action_start, action_end, "tier0_action",
                ),
                OccurrenceAnchor(
                    "target", eid, target_start, target_end, "direct_patient",
                ),
            ),
            **_TIER0_OCCURRENCE_AUTHORITY,
        )
        turn.occurrence_graph = graph
    frame.occurrence = occurrence_for_span(
        graph,
        min(action_start, target_start),
        max(action_end, target_end),
    )
    clause_start, clause_end = _clause_span(detector, assessment.clause_index)
    actor = re.search(r"\b(?:I|we)\b", detector[clause_start:clause_end], re.IGNORECASE)
    if actor:
        frame.add_evidence(
            "actor", clause_start + actor.start(), clause_start + actor.end(), player_eid,
        )
    frame.add_evidence("target", target_start, target_end, eid)
    turn.frames.append(frame)
    return frame


def _append_kill_attempt_frame(turn: SemanticTurn, text: str, state: dict,
                               player_eid: str, dm_text: str = "",
                               detection_text: str | None = None) -> ActionFrame | None:
    """Freeze an otherwise unrolled lethal declaration before the bounded kill gate.

    A Player does not need to own a skill merely to attempt an unsupported declaration. The
    synthetic capability records that semantic fact without authorizing success: `_kill_intent`
    still requires stealth/concealment or a settled grand check, otherwise it remains a non-move.
    """
    if turn.compiled_meaning is None or (state.get("combat") or {}).get("active"):
        return None
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else _action_text(text)
    matches = [
        *list(_KILL_VERBS.finditer(detector or "")),
        *list(_GRAND.finditer(detector or "")),
    ]
    if not matches:
        return None
    match = min(matches, key=lambda item: (item.start(), item.end()))
    action = "grand_kill_attempt" if _GRAND.search(detector or "") else "kill_attempt"
    turn.add_candidate(CapabilityCandidate(
        capability_id="kill_attempt",
        kind="construction",
        source="construction",
        phrase=text[match.start():match.end()],
        start=match.start(),
        end=match.end(),
        clause_index=sum(1 for _ in _CLAUSE_BOUNDARY_RE.finditer(text[:match.start()])),
        action=action,
    ))
    _partition_semantic_turn_by_occurrence(
        turn,
        text,
        state,
        player_eid,
        detection_text=detector,
    )
    turn.resolve()
    frame = next(
        (
            item for item in turn.frames
            if any(candidate.capability_id == "kill_attempt" for candidate in item.candidates)
        ),
        None,
    )
    # Repartitioning includes every earlier capability on the turn.  Re-ground every rebuilt
    # frame, not only the newly appended kill attempt, so a later synthetic candidate cannot wipe
    # the utility/attack frame that was already tied to a check.
    for grounded in turn.frames:
        _ground_action_frame(
            grounded,
            text,
            state,
            player_eid,
            dm_text=dm_text,
            meaning=turn.compiled_meaning,
            detection_text=detector,
            intent_preferences=turn.interpretation_preferences,
        )
    return frame


def _detect_nl_checks(text: str, state: dict, cfg, res: Tier0Result,
                      dm_text: str = "",
                      detection_text: str | None = None) -> None:
    """Natural-language roll detection (RPG). When the player NAMES one of their own skills or
    abilities in prose ("I use fire-slash on the monsters"), roll the governing SKILL: an ability
    maps to the skill it `applies_to` and is INVOKED if active; a skill name rolls itself. Matching
    is case/hyphen/space-insensitive, whole-phrase, and restricted to what the player OWNS — an
    unknown or unowned name never fires (the eligibility gate holds: nothing rollable without an
    in-world basis). Explicit ((aether.check ...)) still wins; a duplicate skill just merges the
    invoked ability. Code detects + resolves; the narrator only narrates (vision pillar 3)."""
    detector = detection_text if detection_text is not None and len(detection_text) == len(text) \
        else _action_text(text)
    semantic_turn = res.semantic_turn
    if semantic_turn is None or semantic_turn.source_text != text:
        semantic_turn = _new_semantic_turn(text, state, res)
        res.semantic_turn = semantic_turn
    if semantic_turn.compiled_meaning is None:
        return
    reg = registry.load(cfg)
    player_eid, player = _player_card(state)
    if not player:
        return
    msg = " " + _norm_phrase(detector) + " "
    stem_aware = bool(getattr(getattr(cfg, "specialization", None), "intent_floor", True))
    msg_stems = _stem_seq(detector) if stem_aware else []
    already = {str(c.get("skill", "")).lower() for c in res.checks}
    cands = []                                   # (normalized_name, kind, id, entry, source)
    owned = set(player.get("skills") or {})
    owned |= set(((player.get("defs") or {}).get("skills") or {}))   # a frozen custom skill is
    for sid in owned:                            # OWNED even at rank 0 (it is on the sheet)
        entry = reg.skill_entry(sid, player)
        for nm in {str(sid), str(entry.get("name", sid))}:
            n = _norm_phrase(nm)
            if len(n) >= 3:
                cands.append((n, "skill", str(sid), entry, "named"))
        for nm in {str(g) for g in (entry.get("governs") or [])}:
            n = _norm_phrase(nm)
            if len(n) >= 3:
                cands.append((n, "skill", str(sid), entry, "inferred"))
                if stem_aware:                   # curated intent lexicon: this governs seed's
                    for syn in _INTENT_SYN.get(n, ()):     # natural synonyms, tied to THIS skill
                        s = _norm_phrase(syn)
                        if len(s) >= 3:
                            cands.append((s, "skill", str(sid), entry, "inferred"))
    for aid, a in (reg.known_abilities(player) or {}).items():
        for nm in {str(aid), str((a or {}).get("name", aid))}:
            n = _norm_phrase(nm)
            if len(n) >= 4:
                cands.append((n, "ability", str(aid), a or {}, "named"))
    cands.sort(key=lambda c: -len(c[0]))         # prefer the most specific phrase
    # A named owned skill anchors its clause. Broad governs verbs/synonyms for OTHER skills in
    # that same clause are descriptive parts of the chosen method, not extra independent rolls.
    # Separate clauses remain eligible, preserving genuine multi-action turns.
    named_clause_skills: dict[int, set[str]] = {}
    for n, kind, rid, _entry, source in cands:
        if kind != "skill" or source != "named":
            continue
        sid = reg.resolve_skill(rid, player) or rid
        for _start, _end, clause in _semantic_phrase_spans(
                n, detector, state, player_eid, msg, msg_stems, stem_aware):
            named_clause_skills.setdefault(clause, set()).add(sid)
    run_weapon_spans = [
        (match.start(), match.end())
        for match in _RUN_WEAPON_THROUGH_RE.finditer(detector)
    ]
    capability_order: list[str] = []              # preserve the established detector output order
    for n, kind, rid, entry, source in cands:
        hit_spans = _semantic_phrase_spans(
            n, detector, state, player_eid, msg, msg_stems, stem_aware)
        if not hit_spans:
            continue
        if kind == "skill":
            sid = reg.resolve_skill(rid, player) or rid
            if source == "inferred":
                hit_spans = [span for span in hit_spans
                             if not named_clause_skills.get(span[2])
                             or sid in named_clause_skills[span[2]]]
                if n == "run":
                    hit_spans = [span for span in hit_spans if not any(
                        lo <= span[0] < hi for lo, hi in run_weapon_spans)]
                if not hit_spans:
                    continue
            use: tuple[str, ...] = ()
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
            sid = gov
            use = (rid,) if registry.ability_is_active(entry) else ()
        try:
            rank = int((player.get("skills") or {}).get(sid, 0) or 0)
        except (TypeError, ValueError):
            rank = 0
        if sid not in capability_order:
            capability_order.append(sid)
        for start, end, clause in hit_spans:
            semantic_turn.add_candidate(CapabilityCandidate(
                capability_id=sid,
                kind=kind,
                source=source,
                phrase=text[start:end],
                start=start,
                end=end,
                clause_index=clause,
                rank=rank,
                fallback=sid.lower() == "brawl",
                use=use,
            ))
    if stem_aware:
        owned_ids = {reg.resolve_skill(str(sid), player) or str(sid) for sid in owned}
        for construction in match_phrasebook(
                detector, _phrasebook_slot_values(state, player_eid)):
            before = _clause_prefix(detector, construction.start)
            if _named_nonplayer_subject(before, state, player_eid):
                continue
            sid = reg.resolve_skill(construction.skill, player) or construction.skill
            if sid not in owned_ids:
                # A construction can name the most specific skill while still falling back to the
                # universal close-combat floor on a card that does not own that specialization.
                # Example: `slash at the wolf` is Swordplay when owned, otherwise Brawl — still a
                # real player-owned check, never a capability minted by prose.
                if construction.action == "weapon_attack" and "brawl" in owned_ids:
                    sid = "brawl"
                else:
                    continue
            try:
                rank = int((player.get("skills") or {}).get(sid, 0) or 0)
            except (TypeError, ValueError):
                rank = 0
            if sid not in capability_order:
                capability_order.append(sid)
            clause = sum(
                1 for _ in _CLAUSE_BOUNDARY_RE.finditer(detector[:construction.start])
            )
            semantic_turn.add_candidate(CapabilityCandidate(
                capability_id=sid,
                kind="construction",
                source="construction",
                phrase=text[construction.start:construction.end],
                start=construction.start,
                end=construction.end,
                clause_index=clause,
                rank=rank,
                fallback=sid.lower() == "brawl",
                action=construction.action,
                target=construction.target,
                instrument=construction.instrument,
            ))
    detected: dict = {}                          # (skill_id, frame_id) -> one event projection
    _partition_semantic_turn_by_occurrence(
        semantic_turn,
        text,
        state,
        player_eid,
        detection_text=detector,
    )
    semantic_turn.resolve()
    for frame in semantic_turn.frames:
        _ground_action_frame(
            frame,
            text,
            state,
            player_eid,
            dm_text=dm_text,
            meaning=semantic_turn.compiled_meaning,
            detection_text=detector,
            intent_preferences=semantic_turn.interpretation_preferences,
        )
        _enforce_nonperforming_candidate_scope(frame, detector)
        _enforce_inferred_fallback_action_topology(frame)
        for candidate in frame.candidates:
            for ability_id in candidate.use:
                frame.add_evidence(
                    "invoked_capability",
                    candidate.start,
                    candidate.end,
                    ability_id,
                )
    for sid in capability_order:
        for frame in semantic_turn.frames:
            if frame.capability_id != sid:
                continue
            d = detected.setdefault(
                (sid, frame.frame_id),
                {"use": [], "target": frame.target_entity_id, "action": frame.action_class,
                 "frame_id": frame.frame_id,
                 "frame_anchor": _semantic_candidate_anchor(frame, sid)},
            )
            for cand in frame.candidates:
                if cand.capability_id != sid:
                    continue
                for ability_id in cand.use:
                    if ability_id not in d["use"]:
                        d["use"].append(ability_id)
    for (sid, _frame_id), d in detected.items():
        if sid.lower() in already:                # explicit check already covers it -> merge use
            for c in res.checks:
                if str(c.get("skill", "")).lower() == sid.lower():
                    for u in d["use"]:
                        c.setdefault("use", [])
                        if u not in c["use"]:
                            c["use"].append(u)
            continue
        res.checks.append({"skill": sid, "mod": 0, "dc": None, "scope": None,
                           "use": d["use"], "target": d["target"], "raw": sid, "nl": True,
                           "_attack": d["action"] in ("weapon_attack", "grapple"),
                           "_semantic_frame_id": d["frame_id"],
                           "_semantic_frame_anchor": d["frame_anchor"]})


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


def _player_frozen_def(player: dict, table: str, capability_id: str) -> bool:
    """Whether this exact capability is governed by the Player Card's frozen snapshot."""
    defs = (player.get("defs") or {}).get(table) if isinstance(player, dict) else None
    return isinstance(defs, dict) and capability_id in defs


def _legacy_registry_cost_waiver(player: dict, table: str, capability_id: str) -> bool:
    """Narrow pre-contract compatibility: old card + shared registry definition only."""
    return player.get("_resource_cost_policy") != "strict/1" \
        and not _player_frozen_def(player, table, capability_id)


def _merge_cost(dst: dict, cost: dict) -> None:
    for k, v in (cost or {}).items():
        try:
            dst[k] = dst.get(k, 0) + int(v)
        except (TypeError, ValueError):
            continue


def _reserve_cost(player: dict, reserved: dict, cost: dict, *,
                  legacy_registry_waiver: bool = False
                  ) -> tuple[dict, Optional[tuple[str, int, int]]]:
    """Atomically reserve tracked cost against this request's still-available balance.

    A frozen Player Card capability is never executable when one of its declared pools is absent:
    returning a shortage prevents a paid custom mechanic from silently becoming a free one. The
    only fail-open path is an explicit compatibility waiver supplied for a shared registry entry;
    callers must never supply it for a per-character frozen definition. The returned dict contains
    only tracked amounts added to ``reserved``. Callers keep that provisional slice so
    outcome-dependent charges can be reconciled before the next check.
    """
    tracked: dict[str, int] = {}
    for rname, raw_amount in (cost or {}).items():
        try:
            amount = max(0, int(raw_amount))
        except (TypeError, ValueError):
            continue
        if not amount:
            continue
        if not _tracked_pool(player, rname):
            if legacy_registry_waiver:
                continue
            return {}, (str(rname), 0, amount)
        pool = player.get("hp") if rname == "hp" \
            else (player.get("resources") or {}).get(rname)
        try:
            current = max(0, int(pool.get("cur", 0)))
            already = max(0, int(reserved.get(rname, 0)))
        except (AttributeError, TypeError, ValueError):
            current, already = 0, 0
        available = max(0, current - already)
        if available < amount:
            return {}, (str(rname), available, amount)
        tracked[str(rname)] = amount
    _merge_cost(reserved, tracked)
    return tracked, None


def _reconcile_cost(reserved: dict, provisional: dict, actual: dict) -> None:
    """Replace this check's provisional reservation with its exact baked charge."""
    for rname, held in provisional.items():
        try:
            keep = max(0, int((actual or {}).get(rname, 0)))
            reserved[rname] = max(
                0, int(reserved.get(rname, 0)) - max(0, int(held)) + keep)
        except (TypeError, ValueError):
            reserved[rname] = max(0, int(reserved.get(rname, 0)))


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
# A defended attack is the object of the Player's reaction, not a second attack performed by the
# Player. Keep this source-local and bounded so ``parry, then strike Iven`` retains the later
# predicate while ``parry his strike`` does not turn the noun into HP damage.
_DEFENSIVE_PREDICATE_RE = re.compile(
    r"\b(?:block|blocks|blocked|blocking|deflect|deflects|deflected|deflecting|"
    r"dodge|dodges|dodged|dodging|evade|evades|evaded|evading|"
    r"parry|parries|parried|parrying)\b",
    re.IGNORECASE,
)
_DEFENSE_EVENT_BOUNDARY_RE = re.compile(
    r"[,;:.!?]|\b(?:and|but|then|before|after|while|when)\b",
    re.IGNORECASE,
)


def _defended_attack_nominal(text: str, attack_start: int, attack_end: int) -> bool:
    prefix_start = max(0, attack_start - 96)
    prefix = (text or "")[prefix_start:attack_start]
    defenses = list(_DEFENSIVE_PREDICATE_RE.finditer(prefix))
    if not defenses:
        return False
    bridge = prefix[defenses[-1].end():]
    boundaries = list(_DEFENSE_EVENT_BOUNDARY_RE.finditer(bridge))
    if not boundaries:
        # With no renewed event boundary, the attack surface remains the defensive predicate's
        # nominal patient, regardless of intervening determiners, names, or modifiers.
        return True
    boundary = boundaries[-1]
    tail_words = re.findall(
        r"[a-z0-9]+(?:['’]s)?", bridge[boundary.end():], re.IGNORECASE,
    )
    if tail_words:
        # Punctuation or a temporal marker does not renew Player action by itself:
        # ``parry, his strike glances off`` still owns an attack noun. An explicit Player subject
        # does renew it (``parry before I strike``).
        return tail_words[0].casefold() not in {"i", "we"}
    boundary_word = _norm_phrase(boundary.group(0))
    if boundary_word in {"before", "after", "while", "when"}:
        # Subjectless temporal complements are non-performing unless the Player writes the
        # gerund action itself (``parry before striking Iven``).
        return not (text or "")[attack_start:attack_end].casefold().endswith("ing")
    return False


def _attack_false_friend_spans(text: str) -> tuple[tuple[int, int], ...] | None:
    try:
        return load_default_semantic_fabric().false_friend_spans(
            "action.weapon_attack", text,
        )
    except (OSError, SemanticFabricError, ValueError):
        # ActionLex is the authority for these exclusions. If it is unavailable, the conservative
        # legacy attack vocabulary must not guess its way around that missing boundary.
        return None


def _performed_attack_span(text: str, start: int, end: int) -> bool:
    if _attack_token_is_abstract_payload(text, start, end):
        return False
    false_friends = _attack_false_friend_spans(text)
    if false_friends is None:
        return False
    if any(false_start <= start and end <= false_end
           for false_start, false_end in false_friends):
        return False
    return not _defended_attack_nominal(text, start, end)


def _performed_attack_verb_matches(text: str) -> list[re.Match[str]]:
    source = text or ""
    return [
        match for match in _ATTACK_VERBS.finditer(source)
        if _performed_attack_span(source, match.start(), match.end())
    ]


# Narrow idiom exception: `run my blade through Maren` is a weapon attack, while literal
# `run toward the gate` remains movement. The weapon noun and targeting preposition are required.
_RUN_WEAPON_THROUGH_RE = re.compile(
    r"\b(?:run|runs|ran|running)\s+"
    r"(?:(?:my|the|a|an|his|her|their|our)\s+)?"
    r"(?:(?:[a-z][a-z'-]*)\s+){0,2}"
    r"(?:blade|sword|dagger|knife|spear|rapier|weapon)\s+"
    r"(?:through|into)\b",
    re.IGNORECASE,
)

# Harmful capability delivery is an attack construction even when the Player does not use a
# mundane attack verb.  Keep this compositional and narrow: `rain` needs both a dangerous payload
# and a directed patient, while `destabilize` needs an explicitly embodied/existential patient.
# This admits magic such as pointed ice or existence-destabilization without turning `rain down
# praise` or `destabilize negotiations` into combat.
_DIRECTED_HARMFUL_CAPABILITY_RE = re.compile(
    r"\b(?:"
    r"destabiliz(?:e|es|ed|ing)\s+(?:(?:the|a|an)\s+)?(?:very\s+)?"
    r"(?:existence|being|body|form|physical\s+structure)\s+of\s+|"
    r"(?:rain|rains|rained|raining)\s+down\b"
    r"(?=[^.!?;]{0,120}\b(?:pointed|sharp|jagged|razor(?:ed|-sharp)?|burning|"
    r"freezing|searing|scalding|caustic|venomous)\b)"
    r"[^.!?;]{0,160}?\b(?:at|on|onto|against)\s+"
    r")",
    re.IGNORECASE,
)
_PHYSICAL_HARM_CARRIERS = (
    "acid", "arrow", "arrows", "blade", "blades", "bolt", "bolts", "bone",
    "bramble", "brambles", "crystal", "crystals", "electricity", "fire", "flame",
    "flames", "force", "glass", "hail", "ice", "iron", "javelin", "javelins",
    "lava", "lightning", "magma", "metal", "missile", "missiles", "needle",
    "needles", "poison", "razor", "razors", "rock", "rocks", "shard", "shards",
    "sleet", "spear", "spears", "spike", "spikes", "steel", "stone", "stones",
    "thorn", "thorns", "venom", "wood",
)
_PHYSICAL_HARM_CARRIER_RE = re.compile(
    r"\b(?:" + "|".join(_PHYSICAL_HARM_CARRIERS) + r")\b",
    re.IGNORECASE,
)

# Spell delivery needs its own typed construction. Treating every ``send ... towards <foe>`` or
# ``cast ... on <foe>`` as an attack would turn messages, votes, glances, and blessings into HP
# damage. A closed destructive head plus a physical carrier proves the harmful event corridor
# while remaining independent of any creature name or Player-authored phrase.
_HARMFUL_SPELL_HEADS = (
    "beam", "blast", "bolt", "column", "cone", "eruption", "jet", "lance", "ray",
    "shard", "spike", "storm", "torrent", "tornado", "vortex", "wave",
)
_HARMFUL_SPELL_HEAD_SET = set(_HARMFUL_SPELL_HEADS)
_HARMFUL_SPELL_DELIVERY_SET = (
    _conjugate("cast") | _conjugate("conjure") | _conjugate("send") | {"sent"}
)
_SPELL_DELIVERY_RE = re.compile(
    r"\b(?:"
    + "|".join(sorted(_HARMFUL_SPELL_DELIVERY_SET, key=len, reverse=True))
    + r")\b\s+"
    r"(?:(?:a|an|the|my|our|this|that)\s+)?"
    r"(?:(?:[a-z][a-z0-9'-]*\s+){0,3})"
    r"(?P<head>[a-z][a-z0-9'-]*)\s+of\s+"
    r"(?P<carrier>" + "|".join(_PHYSICAL_HARM_CARRIERS) + r")"
    r"(?:\s+(?:and\s+)?[a-z][a-z0-9'-]*){0,2}\s+"
    r"(?:at|on|onto|against|toward|towards|through)\s+",
    re.IGNORECASE,
)


def _harmful_spell_delivery_matches(text: str) -> list[re.Match[str]]:
    """Return source-bounded physical spell deliveries with an explicit patient corridor."""
    return [
        match for match in _SPELL_DELIVERY_RE.finditer(text or "")
        if match.group("head").lower() in _HARMFUL_SPELL_HEAD_SET
    ]

# Communicative payloads remain abstract across ordinary inflection. Compact forms are admitted
# only when their other component is a known physical carrier or projectile head; arbitrary
# substring overlap is never semantic evidence.
_ABSTRACT_COMMUNICATION_TOKEN_RE = re.compile(
    r"(?:"
    r"critic(?:ism|isms|is(?:e|es|ed|ing)|iz(?:e|es|ed|ing))|"
    r"critique(?:s|d|ing)?|"
    r"insult(?:s|ed|ing)?|"
    r"question(?:s|ed|ing|naire|naires)?|"
    r"comment(?:s|ed|ing|ary|aries|at(?:e|es|ed|ing)|ation|ations)?|"
    r"debat(?:e|es|ed|ing)|"
    r"rhetor(?:ic|ical|ically)|verbal(?:ly)?"
    r")",
    re.IGNORECASE,
)
_ABSTRACT_TOPIC_PREPOSITION_RE = re.compile(
    r"\b(?:about|concerning|of|on|regarding)\b",
    re.IGNORECASE,
)
_ABSTRACT_TOPIC_BOUNDARY_RE = re.compile(
    r"[.!?;]|\b(?:but|then|while)\b",
    re.IGNORECASE,
)


def _abstract_communication_tokens(text: str) -> list[re.Match[str]]:
    def _is_form(value: str) -> bool:
        return _ABSTRACT_COMMUNICATION_TOKEN_RE.fullmatch(value) is not None

    def _is_bounded_compound(value: str) -> bool:
        anchors = set(_PHYSICAL_HARM_CARRIERS) | set(_HARMFUL_PROJECTILE_HEADS)
        for anchor in anchors:
            if value.startswith(anchor) and _is_form(value[len(anchor):]):
                return True
            if value.endswith(anchor) and _is_form(value[:-len(anchor)]):
                return True
        return False

    return [
        token for token in re.finditer(r"[a-z0-9]+", text or "", re.IGNORECASE)
        if _is_form(token.group(0)) or _is_bounded_compound(token.group(0))
    ]


def _physical_payload_carriers(text: str) -> list[re.Match[str]]:
    """Return physical carriers that are payloads, not topics of abstract communication."""
    source = text or ""
    abstract_tokens = _abstract_communication_tokens(source)
    owned = []
    first_renewal = _ABSTRACT_TOPIC_BOUNDARY_RE.search(source)
    for carrier in _PHYSICAL_HARM_CARRIER_RE.finditer(source):
        if first_renewal is not None and carrier.start() >= first_renewal.start():
            continue
        topical = False
        for abstract in reversed(abstract_tokens):
            if abstract.end() > carrier.start():
                continue
            bridge = source[abstract.end():carrier.start()]
            if _ABSTRACT_TOPIC_BOUNDARY_RE.search(bridge):
                break
            if _ABSTRACT_TOPIC_PREPOSITION_RE.search(bridge):
                topical = True
                break
        if not topical:
            owned.append(carrier)
    return owned


def _directed_harmful_capability_matches(text: str) -> list[re.Match[str]]:
    """Keep magical harm tied to an embodied/existential or physical payload cause.

    A dangerous adjective alone is not physical harm. ``sharp criticism`` and ``searing
    insults`` therefore remain prose/checks, while pointed ice, metal shards, fire, and acid
    retain deterministic attack construction.
    """
    matches = []
    for match in _DIRECTED_HARMFUL_CAPABILITY_RE.finditer(text or ""):
        surface = match.group(0)
        if re.match(r"\s*(?:rain|rains|rained|raining)\s+down\b", surface, re.IGNORECASE) \
                and not _physical_payload_carriers(surface):
            continue
        matches.append(match)
    return matches

# Projectile delivery is harmful only when the grammar supplies all three roles: a physical
# delivery verb, a bounded projectile head, and a directed patient corridor. The material is
# intentionally unconstrained, so an ice spike, stone shard, or metal bolt uses the same rule.
# Abstract payload modifiers are vetoes; ``launch a rhetorical dart`` stays non-impact language.
_PROJECTILE_DELIVERY_BASES = ("fire", "launch", "loft", "propel", "shoot", "throw")
_PROJECTILE_DELIVERY_SET = set().union(
    *(_conjugate(verb) for verb in _PROJECTILE_DELIVERY_BASES)
) | {"shot", "threw", "thrown"}
_HARMFUL_PROJECTILE_HEADS = (
    "arrow", "bolt", "dart", "javelin", "missile", "projectile", "shard", "spear", "spike",
)
_HARMFUL_PROJECTILE_HEAD_PATTERN = "|".join(_HARMFUL_PROJECTILE_HEADS)
_HARMFUL_PROJECTILE_DELIVERY_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_PROJECTILE_DELIVERY_SET, key=len, reverse=True)) + r")\b\s+"
    r"(?P<payload>"
    r"(?:(?:a|an|the|my|our|this|that)\s+)?"
    r"(?:"
    r"(?:(?:[a-z][a-z0-9'-]*)\s+){0,4}(?:" + _HARMFUL_PROJECTILE_HEAD_PATTERN + r")|"
    r"(?:[a-z][a-z0-9']*-){1,5}(?:" + _HARMFUL_PROJECTILE_HEAD_PATTERN + r")"
    r")"
    r")\s+(?:(?:directly|hard|straight)\s+)?"
    r"(?:at|into|onto|against|toward|towards|through)\s+",
    re.IGNORECASE,
)
_ABSTRACT_PROJECTILE_PAYLOAD_RE = re.compile(
    r"\b(?:"
    r"figurative|figuratively|metaphorical|metaphorically|rhetorical|symbolic|verbal|"
    r"activity|attention|cost|data|demand|employment|engagement|error|inflation|interest|"
    r"market|metric|popularity|price|rate|revenue|sales|signal|statistic|traffic|trend|"
    r"value|volatility|volume|workload"
    r")\b",
    re.IGNORECASE,
)


def _abstract_projectile_payload(payload: str) -> bool:
    return _ABSTRACT_PROJECTILE_PAYLOAD_RE.search(payload or "") is not None \
        or bool(_abstract_communication_tokens(payload or ""))


def _harmful_projectile_delivery_matches(text: str) -> list[re.Match[str]]:
    """Return only literal projectile-delivery constructions from one masked event view."""
    return [
        match for match in _HARMFUL_PROJECTILE_DELIVERY_RE.finditer(text or "")
        if not _abstract_projectile_payload(match.group("payload"))
    ]


def _attack_token_is_abstract_payload(text: str, start: int, end: int) -> bool:
    """Keep material nouns inside rejected abstract payloads from acting as attack verbs."""
    source = text or ""
    for abstract in reversed(_abstract_communication_tokens(source)):
        if abstract.end() > start:
            continue
        bridge = source[abstract.end():start]
        if _ABSTRACT_TOPIC_BOUNDARY_RE.search(bridge):
            break
        if _ABSTRACT_TOPIC_PREPOSITION_RE.search(bridge):
            return True
    # Once a whole projectile delivery is proved abstract, neither its delivery verb nor a
    # material-looking modifier inside it may independently reopen the attack path.
    for delivery in _HARMFUL_PROJECTILE_DELIVERY_RE.finditer(source):
        payload_end = delivery.end("payload")
        if delivery.start() <= start and end <= payload_end \
                and _abstract_projectile_payload(delivery.group("payload")):
            return True
    # A physical carrier is not independently an attack verb when its containing spell payload
    # has a harmless head (for example, ``send a blessing of fire towards ...``).
    for delivery in _SPELL_DELIVERY_RE.finditer(source):
        if delivery.start() <= start and end <= delivery.end() \
                and delivery.group("head").lower() not in _HARMFUL_SPELL_HEAD_SET:
            return True
    if _PHYSICAL_HARM_CARRIER_RE.fullmatch(source[start:end]) is None:
        return False
    # The same ambiguity exists in ``criticism of fire``. The topical-carrier classifier already
    # proved that no owned physical payload exists, so the topic word is not a performed attack.
    for delivery in _DIRECTED_HARMFUL_CAPABILITY_RE.finditer(source):
        if delivery.start() <= start and end <= delivery.end() \
                and not _physical_payload_carriers(delivery.group(0)):
            return True
    return False

# the foe is usually the object of a TARGETING preposition ("stab into <foe>", "cut at <foe>") —
# those objects are tried before a bare attack-verb object, which can lead with a direction or
# the Player's own weapon (2026-07-10 Redgate: "lunge FROM the pines and stab MY SHORTSWORD…").
_TARGET_PREP_RE = re.compile(r"\b(?:at|into|onto|upon|through|toward|towards)\s+", re.IGNORECASE)


def _has_attack_intent(text: str) -> bool:
    source = text or ""
    matches = [
        *_performed_attack_verb_matches(source),
        *list(_RUN_WEAPON_THROUGH_RE.finditer(source)),
        *_directed_harmful_capability_matches(source),
        *_harmful_spell_delivery_matches(source),
        *_harmful_projectile_delivery_matches(source),
    ]
    return any(
        not _WITHOUT_ACTION_PREFIX_RE.search(_clause_prefix(source, match.start()))
        for match in matches
    )


def _war_room(state: dict, cfg) -> bool:
    """Combat instances live only under rpg + the war_room knob (default on)."""
    spec = getattr(cfg, "specialization", None)
    combat = state.get("combat") if isinstance(state, dict) else None
    return bool(spec is not None and spec.name == "rpg"
                and getattr(spec, "war_room", True)
                and isinstance(combat, dict) and combat.get("active"))


_COMBAT_OPENING_AMBIGUOUS_RE = re.compile(
    r"\b(?:challeng(?:e|es|ed|ing)|engag(?:e|es|ed|ing)|confront(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_STRONG_RE = re.compile(
    r"\b(?:duel(?:s|ed|ing)?|duell(?:ed|ing)|"
    r"squar(?:e|es|ed|ing)\s+off|fac(?:e|es|ed|ing)\s+off|"
    r"enter(?:s|ed|ing)?\s+(?:combat|battle)|"
    r"(?:tak(?:e|es|ing)|took|taken)\s+up\s+arms\s+against)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_CONTEXT_RE = re.compile(
    r"\b(?:hostile|enemy|foe|opponent|combat|battle|fight(?:s|ing|er)?|armed|arms|"
    r"weapon|blade|(?:[a-z]+)?sword|dagger|knife|spear|axe|mace|bow|crossbow|pistol|"
    r"rifle|gun|staff|wand|shield|guard|pommel|armor|armour)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_PHYSICAL_SET = set().union(*(
    _conjugate(verb) for verb in (
        "attack", "strike", "stab", "slash", "punch", "kick", "shove", "cut", "cleave",
        "smash", "bash", "lunge", "impale", "tackle", "grapple", "hack", "maim",
        "skewer", "pierce", "gore", "batter", "thrust", "slam", "ram", "chop", "spear",
        "club",
    )
))
_COMBAT_OPENING_ABSTRACT_RE = re.compile(
    r"\b(?:conclusion|claim|argument|opinion|decision|ruling|account|testimony|"
    r"statement|logic|premise|version|idea|theory|position|report|story|interpretation|"
    r"evidence|doubt|conversation|debate)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_NEGATED_PREFIX_RE = re.compile(
    r"\b(?:refuse|decline|choose|decide|plan)\s+(?:not\s+)?to\s*$|"
    r"\b(?:am|are|is|was|were)\s+not\s+(?:going\s+to\s+)?$",
    re.IGNORECASE,
)
_COMBAT_OPENING_OBSERVER_PREFIX_RE = re.compile(
    r"\b(?:i|we)\s+(?:ask|tell|order|command|want|expect|watch|see|observe|hear|"
    r"notice|remember|recall|recount|describe|wait\s+for|let|think\s+back\s+to)\b"
    r"[^.!?;]*$",
    re.IGNORECASE,
)
_COMBAT_OPENING_REMOVAL_ROLE_RE = re.compile(
    r"^\s+from\s+(?:(?:the|a|an|his|her|their|our|my)\s+)?"
    r"(?:guest\s+list|team|roster|lineup|squad|committee|staff|payroll|job|post|"
    r"office|role|position|project|case|account)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_NONCOMBAT_OBJECT_RE = re.compile(
    r"\b(?:paperwork|documents?|files?|forms?|filings?|applications?|proposals?|"
    r"schedules?|budgets?)\b",
    re.IGNORECASE,
)
_COMBAT_OPENING_NONCOMBAT_POSSESSIVE_RE = re.compile(
    r"^\s*(?:'s|’s)\s+(?:(?:the|a|an)\s+)?(?:[a-z][a-z'-]*\s+){0,2}"
    r"(?:paperwork|documents?|files?|forms?|filings?|applications?|proposals?|"
    r"schedules?|budgets?)\b",
    re.IGNORECASE,
)
_PRESENT_SHORT_REFERENCE_STOP = {
    "again", "ahead", "all", "another", "any", "around", "aside", "away", "back", "behind",
    "closest", "down", "few", "first", "forward", "here", "inside", "just", "last", "left",
    "nearest", "next", "one", "onward", "other", "out", "outside", "over", "past", "right",
    "still", "then", "there", "toward", "towards", "two", "under", "up",
}


def _present_npc_references(state: dict, peid=None) -> dict[str, set[str]]:
    """Normalized reference -> present ledger NPC ids, including safe short names.

    A generated world commonly authors a titled display name (``Span Warden Kessa``) while
    ordinary play refers to that person by the final name (``Kessa``).  Treat that final token as
    a derived reference, but retain *all* owners in the index: callers may bind a unique reference
    and must explicitly abstain when two present people share it.  Full names and authored aliases
    remain exact references.
    """
    if not isinstance(state, dict):
        return {}
    entities = state.get("entities")
    if not isinstance(entities, dict):
        return {}
    players = state.get("player")
    player_ids = set(players) if isinstance(players, dict) else set()
    if peid:
        player_ids.add(peid)
    candidates: dict[str, set[str]] = {}
    for eid, entity in entities.items():
        if not isinstance(entity, dict) or eid in player_ids or not entity.get("present"):
            continue
        if entity.get("kind") not in ("character", "npc"):
            continue
        primary = _norm_phrase(entity.get("name"))
        references = [primary, *(_norm_phrase(alias) for alias in (entity.get("aliases") or []))]
        words = primary.split()
        if len(words) > 1 and len(words[-1]) >= 3 \
                and words[-1] not in _PRESENT_SHORT_REFERENCE_STOP:
            references.append(words[-1])
        for reference in references:
            if reference:
                candidates.setdefault(reference, set()).add(str(eid))
    return candidates


def _reference_spans(references: dict[str, set[str]], text: str
                     ) -> list[tuple[int, int, str, set[str]]]:
    """Raw-text spans for normalized reference forms, longest overlap first."""
    out: list[tuple[int, int, str, set[str]]] = []
    for reference, entity_ids in references.items():
        words = reference.split()
        pattern = r"(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, words)) \
            + r"(?![a-z0-9])"
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            out.append((match.start(), match.end(), reference, entity_ids))
    return sorted(out, key=lambda row: (row[0], -(row[1] - row[0]), row[2]))


@lru_cache(maxsize=1)
def _combat_reference_ordinal_prefix_re() -> re.Pattern[str]:
    """Use the state resolver's exact ordinal surfaces at the construction boundary."""
    from .state import _COMBAT_REFERENCE_ORDINALS

    surfaces = (
        re.escape(surface).replace(r"\ ", r"\s+")
        for surface in sorted(_COMBAT_REFERENCE_ORDINALS, key=len, reverse=True)
    )
    return re.compile(
        r"(?<![#a-z0-9])(?:the\s+)?(?:" + "|".join(surfaces) + r")(?=\s)",
        re.IGNORECASE,
    )
_COMBAT_REFERENCE_BASE_WORD_RE = re.compile(
    r"\s+(?P<word>[a-z0-9][a-z0-9_-]*)",
    re.IGNORECASE,
)
_COMBAT_OCCURRENCE_REFERENCE_PREFIX = "combat_ref."


def _combat_reference_surface_spans(
    state: dict,
    text: str,
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    """Discover bounded entity-reference surfaces without deciding what they mean.

    Exact state-owned labels/names and syntactic ordinal phrases are only *surface candidates*.
    The caller first classifies patient/possessor/locus grammar, then passes the isolated entity
    span to ``resolve_combat_reference``.  This prevents ``Hollowed #1's existence`` or sword
    possession from reaching the state boundary as an undifferentiated sentence fragment.
    """
    from .state import combat_reference_candidates

    bounded_start = max(0, int(start))
    bounded_end = min(len(text), int(end))
    if bounded_end <= bounded_start:
        return []
    spans: set[tuple[int, int]] = set()
    candidates = combat_reference_candidates(state)
    surfaces = {
        surface.strip()
        for candidate in candidates
        for surface in (candidate.label, candidate.name)
        if surface and surface.strip()
    }
    for surface in surfaces:
        pattern = re.compile(
            r"(?<![a-z0-9])"
            + re.escape(surface).replace(r"\ ", r"[\s_-]+")
            + r"(?![a-z0-9])",
            re.IGNORECASE,
        )
        spans.update(
            (match.start(), match.end())
            for match in pattern.finditer(text, bounded_start, bounded_end)
        )

    # Ordinal morphology is validated exclusively by the structured resolver. Isolate either a
    # generic one-word head (``first hollow``) or a state-owned cohort phrase
    # (``first baser hollow``). Never greedily consume arbitrary following words: doing so can
    # swallow ``first hollow and Hollowed #2`` into one apparent entity span and hide the second
    # written patient.
    cohort_phrase_words = {
        tuple(re.findall(r"[a-z0-9]+", candidate.name.casefold()))
        for candidate in candidates
        if candidate.cohort_index is not None and candidate.name
    }
    cohort_phrase_words.discard(())
    for ordinal in _combat_reference_ordinal_prefix_re().finditer(
            text, bounded_start, bounded_end):
        first_word = _COMBAT_REFERENCE_BASE_WORD_RE.match(
            text, ordinal.end(), bounded_end,
        )
        if first_word is not None:
            spans.add((ordinal.start(), first_word.end()))
        for words in cohort_phrase_words:
            # Exact state-owned prefix words plus one bounded morphological head are sufficient;
            # the resolver remains the sole authority on whether that head is a valid form.
            prefix = r"\s+" + r"[\s_-]+".join(map(re.escape, words[:-1]))
            if words[:-1]:
                prefix += r"[\s_-]+"
            phrase = re.compile(
                prefix + r"[a-z0-9][a-z0-9_-]*(?![a-z0-9])",
                re.IGNORECASE,
            ).match(text, ordinal.end(), bounded_end)
            if phrase is not None:
                spans.add((ordinal.start(), phrase.end()))
    return sorted(spans, key=lambda row: (row[0], -(row[1] - row[0])))


def _combat_occurrence_reference_spans(
    state: dict,
    text: str,
) -> list[tuple[int, int, str, set[str]]]:
    """Project combat-row mentions into occurrence grammar without lending mechanics identity.

    The occurrence graph needs the exact written slots to preserve list/cardinality boundaries,
    but structured combat resolution remains later and role-aware. Stable synthetic identities
    therefore exist only inside construction and are explicitly ignored by mechanics binding.
    """
    from .state import CombatReferenceStatus, resolve_combat_reference

    options: list[tuple[int, int, str, object]] = []
    for start, end in _combat_reference_surface_spans(state, text, 0, len(text)):
        result = resolve_combat_reference(state, text[start:end])
        options.append((start, end, text[start:end], result))

    def _key(row: tuple[int, int, str, object]) -> tuple[int, int, int, int]:
        start, end, _surface, result = row
        status = getattr(result, "status", CombatReferenceStatus.UNKNOWN)
        player_exact = getattr(result, "match_kind", None) != "unique_token_subset"
        known = status is not CombatReferenceStatus.UNKNOWN and player_exact
        return (start, 0 if known else 1, -(end - start) if known else end - start, end)

    selected: list[tuple[int, int, str, object]] = []
    for option in sorted(options, key=_key):
        start, end, _surface, _result = option
        if any(start < kept_end and kept_start < end
               for kept_start, kept_end, _kept_surface, _kept_result in selected):
            continue
        selected.append(option)
    selected.sort(key=lambda row: (row[0], row[1]))
    return [
        (
            start,
            end,
            surface,
            {f"{_COMBAT_OCCURRENCE_REFERENCE_PREFIX}s{start}.e{end}"},
        )
        for start, end, surface, _result in selected
    ]


def _grounded_npc_spans(state: dict, text: str) -> list[tuple[int, int, str]]:
    """Mention spans for unambiguous, present ledger NPC names and references."""
    candidates = []
    for start, end, _reference, entity_ids in _reference_spans(
            _present_npc_references(state), text):
        if len(entity_ids) == 1:
            candidates.append((start, end, next(iter(entity_ids))))
    # A full display name and its derived final token overlap.  Preserve the longest form so
    # target grammar sees one mention, not two synthetic coordinated targets.
    out: list[tuple[int, int, str]] = []
    for candidate in candidates:
        start, end, _eid = candidate
        if any(start < kept_end and kept_start < end for kept_start, kept_end, _ in out):
            continue
        out.append(candidate)
    return out


def _combat_transition_spans(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, intrinsically-physical) spans from one already-filtered clause."""
    out = [(m.start(), m.end(), m.group(0).lower() in _COMBAT_OPENING_PHYSICAL_SET)
           for m in _performed_attack_verb_matches(text or "")]
    out.extend((m.start(), m.end(), True)
               for m in _RUN_WEAPON_THROUGH_RE.finditer(text or ""))
    out.extend((m.start(), m.end(), True)
               for m in _directed_harmful_capability_matches(text or ""))
    out.extend((m.start(), m.end(), True)
               for m in _harmful_spell_delivery_matches(text or ""))
    out.extend((m.start(), m.end(), True)
               for m in _harmful_projectile_delivery_matches(text or ""))
    out.extend((m.start(), m.end(), False)
               for m in _COMBAT_OPENING_STRONG_RE.finditer(text or ""))
    out.extend((m.start(), m.end(), False)
               for m in _COMBAT_OPENING_AMBIGUOUS_RE.finditer(text or ""))
    return sorted(out)


def _combat_opening_target_role(clause: str, action_end: int,
                                target_start: int, target_end: int,
                                grammar: _SemanticGrammar | None = None) -> bool:
    """Whether the grounded NPC mention is the attack's target, not adjacent context.

    Surface attack verbs also describe administrative removal (``cut Varo from the team``),
    actions against something the NPC owns (``tackle Varo's paperwork``), and relative-clause
    actors after a different object (``attack paperwork Varo left``). Those mentions ground Varo
    in the scene but do not make Varo the semantic patient of a combat transition.
    """
    if grammar is None:
        try:
            grammar = _semantic_grammar(())
        except (OSError, SemanticFabricError, StopIteration, ValueError):
            grammar = None
    if grammar is not None:
        patient_role = _entity_patient_role(
            clause, action_end, target_start, target_end, grammar,
        )
        if patient_role is not None:
            return patient_role.patient
    else:
        # A missing/corrupt ReferentLex may reduce availability, never turn possession into HP.
        if re.match(r"^\s*(?:'s|\u2019s)\b", clause[target_end:], re.IGNORECASE) \
                or re.search(
                    r"\bof\s+(?:(?:the|a|an)\s+)?$",
                    clause[action_end:target_start],
                    re.IGNORECASE,
                ):
            return False
    suffix = clause[target_end:]
    if _COMBAT_OPENING_REMOVAL_ROLE_RE.match(suffix):
        return False
    if _COMBAT_OPENING_NONCOMBAT_POSSESSIVE_RE.match(suffix):
        return False
    if target_start >= action_end:
        bridge = clause[action_end:target_start]
        targeted_by_prep = re.search(
            r"\b(?:at|into|onto|upon|through|toward|towards)\s*$",
            bridge,
            re.IGNORECASE,
        ) is not None
        if not targeted_by_prep and _COMBAT_OPENING_NONCOMBAT_OBJECT_RE.search(bridge):
            return False
    return True


def _combat_opening_target_score(clause: str, action_start: int, action_end: int,
                                 target_start: int, target_end: int,
                                 grammar: _SemanticGrammar | None = None) -> int:
    """Rank an NPC mention as the semantic patient of one combat transition.

    Prompt activation once tolerated any nearby grounded name. Ledger mutation cannot: a named
    bystander after ``while/when/as`` must never beat the direct object merely because entity rows
    happen to be ordered differently. Explicit targeting prepositions rank first, an immediate
    post-verb name second, and conservative fronted targets last. Everything else abstains.
    """
    if grammar is None:
        try:
            grammar = _semantic_grammar(())
        except (OSError, SemanticFabricError, StopIteration, ValueError):
            grammar = None
    if grammar is not None:
        patient_role = _entity_patient_role(
            clause, action_end, target_start, target_end, grammar,
        )
        if patient_role is not None:
            return 3 if patient_role.patient else 0
    elif re.match(r"^\s*(?:'s|\u2019s)\b", clause[target_end:], re.IGNORECASE) \
            or re.search(
                r"\bof\s+(?:(?:the|a|an)\s+)?$",
                clause[action_end:target_start],
                re.IGNORECASE,
            ):
        return 0
    if target_start >= action_end:
        bridge = clause[action_end:target_start]
        if re.search(
                r"\b(?:while|when|whereas|as|who|which|although|because|before|after)\b",
                bridge, re.IGNORECASE):
            return 0
        if re.search(r"\b(?:at|into|onto|upon|through|toward|towards)\s*$",
                     bridge, re.IGNORECASE):
            return 3
        if re.fullmatch(r"\s*(?:(?:the|a|an)\s+)?", bridge, re.IGNORECASE):
            return 2
        return 0
    between = clause[target_end:action_start]
    if re.search(r"[.!?;]|\b(?:while|when|whereas|as|who|which|although|because)\b",
                 between, re.IGNORECASE):
        return 0
    return 1 if len(_INTENT_TOKEN_RE.findall(between)) <= 3 else 0


def _combat_opening_match(
    doc: dict,
    state: dict,
    cfg,
    klass: str,
    duplicate: bool = False,
    *,
    require_primer: bool = True,
) -> tuple[
    bool,
    Optional[tuple[str, str]],
    Optional[int],
    Optional[tuple[int, int]],
    Optional[tuple[int, int]],
]:
    """Pure bounded combat-opening classification plus its grounded known-NPC target.

    The boolean also covers an explicit ``aether.foe`` command, whose target is intentionally
    ``None`` because the command parser already owns that spawn. Natural prose returns the exact
    present ledger entity it targets so the rule path can establish combat before narration.
    ``require_primer`` keeps the private prompt experiment independently disableable without
    disabling the mechanical known-opponent boundary.
    """
    spec = getattr(cfg, "specialization", None)
    kind = str(getattr(klass, "value", klass) or "")
    combat = state.get("combat") if isinstance(state, dict) else None
    if duplicate or kind not in ("new_turn", "new_session") \
            or spec is None or getattr(spec, "name", "none") != "rpg" \
            or not getattr(spec, "war_room", True) \
            or (require_primer and not getattr(spec, "combat_opening_primer", False)) \
            or (isinstance(combat, dict) and combat.get("active")):
        return False, None, None, None, None
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return False, None, None, None, None
    last_user = next(
        (_msg_text(message.get("content")) for message in reversed(messages)
         if isinstance(message, dict) and message.get("role") == "user"),
        "",
    )
    if not last_user:
        return False, None, None, None, None

    # Commands are already a deliberate Player-owned combat surface. Require a non-empty foe
    # payload so a malformed usage line does not spend the private opening context.
    for match in OOC_RE.finditer(last_user):
        command = match.group(1).strip()
        if re.match(r"(?i)^aether\.foe\s+\S", command):
            return True, None, None, None, None

    # Quoted dialogue and engine commands can remain in frontend history, but neither is a
    # performed natural-language transition. Keep the replacements length-preserving so mention
    # and action spans stay comparable.
    action_text = _action_text(last_user)
    action_text = OOC_RE.sub(lambda match: " " * len(match.group(0)), action_text)
    clauses: list[tuple[int, int, int, str]] = []
    boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(action_text))
    for clause_index in range(len(boundaries) + 1):
        clause_start, clause_end = _clause_span(action_text, clause_index)
        while clause_start < clause_end and action_text[clause_start] in " \t\r\n,:":
            clause_start += 1
        while clause_end > clause_start and action_text[clause_end - 1] in " \t\r\n,:":
            clause_end -= 1
        clauses.append((clause_index, clause_start, clause_end,
                        action_text[clause_start:clause_end]))
    active_clauses = []
    for row in clauses:
        clause = row[3]
        if clause and not _DIRECT_QUESTION_RE.search(clause) \
                and not _QUESTION_REPORT_RE.search(clause) \
                and not _NEGATED_ACTION_RE.search(clause) \
                and not _HYPOTHETICAL_ACTION_RE.search(clause):
            active_clauses.append(row)
    # A remembered, observed, requested, or merely spoken fight cannot provide context for a
    # different ambiguous verb later in the same request. Reuse the same non-performing/report
    # frames that reject an action below, plus the established speech-frame grammar.
    context_clauses = []
    for row in active_clauses:
        clause = row[3]
        if not _COMBAT_OPENING_OBSERVER_PREFIX_RE.search(clause) \
                and not (_SPEECH_FRAME_RE.search(clause)
                         and not _ACTION_AFTER_SPEECH_RE.search(clause)):
            context_clauses.append(row)
    combat_context = " ".join(row[3] for row in context_clauses)
    candidates: dict[
        str,
        tuple[str, int, int, tuple[int, int], tuple[int, int]],
    ] = {}
    coordinated: set[str] = set()
    for clause_index, clause_start, _clause_end, clause in active_clauses:
        targets = _grounded_npc_spans(state, clause)
        if not targets:
            continue
        ordered_targets = sorted(targets)
        for left, right in zip(ordered_targets, ordered_targets[1:]):
            if re.fullmatch(r"\s*(?:,\s*)?(?:and|or)\s*",
                            clause[left[1]:right[0]], re.IGNORECASE):
                coordinated.update((left[2], right[2]))
        for action_start, action_end, physical_action in _combat_transition_spans(clause):
            prefix = clause[:action_start]
            if not re.search(r"\b(?:i|we)\b", prefix, re.IGNORECASE):
                continue
            if _COMBAT_OPENING_NEGATED_PREFIX_RE.search(prefix) \
                    or _WITHOUT_ACTION_PREFIX_RE.search(prefix) \
                    or _COMBAT_OPENING_OBSERVER_PREFIX_RE.search(prefix):
                continue
            if _SPEECH_FRAME_RE.search(clause) \
                    and not _ACTION_AFTER_SPEECH_RE.search(prefix):
                continue
            for target_start, target_end, entity_id in targets:
                # ``spear``/``club`` can be nouns as well as attack verbs.  When the alleged
                # transition is exactly the noun owned by the preceding person (``Kessa's
                # spear``), it is not evidence that the Player attacked Kessa.  A separate real
                # attack earlier/later in the clause remains independently eligible.
                if target_end <= action_start and re.fullmatch(
                        r"\s*(?:'s|’s)\s*", clause[target_end:action_start], re.IGNORECASE):
                    continue
                if not _combat_opening_target_role(
                        clause, action_end, target_start, target_end):
                    continue
                lo = min(action_start, target_start)
                hi = min(len(clause), max(action_end, target_end) + 48)
                if _COMBAT_OPENING_ABSTRACT_RE.search(clause[lo:hi]):
                    continue
                bridge = clause[action_end:target_start] if target_start >= action_end else ""
                explicitly_targeted = re.fullmatch(
                    r"\s*(?:at|into|through|upon|toward|towards)\s*", bridge,
                    re.IGNORECASE,
                ) is not None
                if not physical_action and not explicitly_targeted \
                        and not _COMBAT_OPENING_CONTEXT_RE.search(combat_context):
                    continue
                score = _combat_opening_target_score(
                    clause, action_start, action_end, target_start, target_end)
                if score <= 0:
                    continue
                entity = ((state.get("entities") or {}).get(entity_id)
                          if isinstance(state, dict) else None)
                name = str(entity.get("name") or entity_id) if isinstance(entity, dict) \
                    else str(entity_id)
                previous = candidates.get(entity_id)
                if previous is None or score > previous[1]:
                    candidates[entity_id] = (
                        name,
                        score,
                        clause_index,
                        (clause_start + action_start, clause_start + action_end),
                        (clause_start + target_start, clause_start + target_end),
                    )
    if len(candidates) != 1:
        return False, None, None, None, None
    entity_id, (name, _score, clause_index, action_span, target_span) = next(
        iter(candidates.items())
    )
    if entity_id in coordinated:
        return False, None, None, None, None
    return True, (entity_id, name), clause_index, action_span, target_span


def combat_opening_assessment(doc: dict, state: dict, cfg, klass: str,
                              duplicate: bool = False) -> CombatOpeningAssessment:
    """Interpret the combat-opening boundary once for both prompt and rule consumers."""
    matched, target, clause_index, action_span, target_span = _combat_opening_match(
        doc, state, cfg, klass, duplicate=duplicate, require_primer=False)
    spec = getattr(cfg, "specialization", None)
    prompt_signal = bool(
        matched and spec is not None and getattr(spec, "combat_opening_primer", False)
    )
    return CombatOpeningAssessment(
        matched=matched,
        target=target,
        prompt_signal=prompt_signal,
        clause_index=clause_index,
        action_span=action_span,
        target_span=target_span,
    )


def combat_opening_signal(doc: dict, state: dict, cfg, klass: str,
                          duplicate: bool = False) -> bool:
    """Whether the private combat-opening examples belong on this narrator request.

    This remains a read-only prompt signal. The rule-owned companion
    :func:`combat_opening_target` exposes only an exact known target for Tier-0 staging.
    """
    return combat_opening_assessment(
        doc, state, cfg, klass, duplicate=duplicate).prompt_signal


def combat_opening_target(doc: dict, state: dict, cfg, klass: str,
                          duplicate: bool = False) -> Optional[tuple[str, str]]:
    """Exact present known NPC deliberately brought into combat by the newest Player action.

    This never mints a name and never handles ``aether.foe``; it only returns the unique ledger
    entity already proven by the same conservative semantic filter as the private primer signal.
    """
    return combat_opening_assessment(
        doc, state, cfg, klass, duplicate=duplicate).target


def opposition_roll(state: dict, turn: Optional[int] = None) -> tuple[int, int]:
    """The existing deterministic opposition die, with an explicit hot-path turn seam."""
    meta = state.get("meta") if isinstance(state, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    at = meta.get("turn", -1) if turn is None else turn
    scene = state.get("scene") if isinstance(state, dict) else None
    loc = str(scene.get("location_id", "")) if isinstance(scene, dict) else ""
    players = state.get("player") if isinstance(state, dict) else None
    peid = next(iter(players), "") if isinstance(players, dict) else ""
    seed = int(hashlib.md5(f"opp:{at}:{loc}:{peid}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    return rng.randint(1, 6) + rng.randint(1, 6), rng.randint(1, 6)


def _opposition_armed(state: dict, cfg, pending_enemy: bool = False) -> bool:
    """Mirror the old directive gate, but decide before narration so code can own damage."""
    spec = getattr(cfg, "specialization", None)
    if spec is None or spec.name != "rpg" or not getattr(spec, "enemy_rolls", True) \
            or not (state.get("player") or {}):
        return False
    players = state.get("player") if isinstance(state, dict) else None
    if not isinstance(players, dict):
        return False
    peid = next(iter(players), "")
    entities = state.get("entities")
    entities = entities if isinstance(entities, dict) else {}
    present = [eid for eid, entity in entities.items()
               if isinstance(entity, dict) and entity.get("present") and eid != peid
               and entity.get("kind") not in ("location", "faction", "world")]
    affinity = state.get("affinity")
    affinity = affinity if isinstance(affinity, dict) else {}
    hostile_present = False
    for eid in present:
        record = affinity.get(f"{peid}->{eid}")
        value = record.get("value", 0) if isinstance(record, dict) else 0
        if isinstance(value, bool):
            continue
        try:
            if float(value) <= -10:
                hostile_present = True
                break
        except (TypeError, ValueError, OverflowError):
            continue
    scene = state.get("scene")
    scene = scene if isinstance(scene, dict) else {}
    phase = str(scene.get("phase", "")).lower()
    combat_phase = phase in _COMBAT_PHASES
    flags = state.get("world")
    flags = flags if isinstance(flags, dict) else {}
    combat_flag = any(str(key).lower() in ("combat", "battle", "fight", "under_attack")
                      and value and str(value).lower() not in ("no", "false", "0", "none")
                      for key, value in flags.items())
    live_foe = _war_room(state, cfg) and any(
        isinstance(row, dict) and not row.get("defeated") and row.get("side") == "enemy"
        for row in ((state.get("combat") or {}).get("combatants") or {}).values())
    return bool((present and (hostile_present or combat_phase or combat_flag))
                or live_foe or pending_enemy)


def _opposition_op(state: dict, cfg, turn: int, pending_enemy: bool = False,
                   reaction: str = "") -> Optional[dict]:
    """Resolve this turn's enemy action into a code-owned Player HP operation."""
    spec = getattr(cfg, "specialization", None)
    # One gate owns both fresh and same-turn replay paths.  A hidden enemy roll must never
    # mutate HP merely because a structured intent happens to be present.
    if spec is None or spec.name != "rpg" or not getattr(spec, "enemy_rolls", True):
        return None
    try:
        turn_i = int(turn)
    except (TypeError, ValueError, OverflowError):
        return None
    players = state.get("player") if isinstance(state, dict) else None
    if not isinstance(players, dict):
        return None
    peid = next(iter(players), "")
    player = players.get(peid)
    if not isinstance(player, dict):
        return None
    prior = player.get("_opposition_last") if isinstance(player, dict) else None
    if isinstance(prior, dict) and prior.get("turn") == turn_i:
        # Defense in depth for any direct/replayed call: never give the enemy another action or
        # advance the telegraph. Preserve the intent-derived EffectId and exact baked result.
        meta = {k: v for k, v in prior.items()
                if k not in ("turn", "delta", "hp_cur", "hp_max", "effect_id")}
        try:
            delta = int(prior.get("delta", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        out = {"op": "hp_adj", "char": peid, "delta": delta,
               "reason": str(prior.get("reason") or "code-owned opposition replay"),
               "_opposition": meta}
        if prior.get("effect_id"):
            out["_effect_id"] = str(prior["effect_id"])
            out["_effect_owner"] = "code"
        return out

    structured = getattr(spec, "war_room", True)
    if structured:
        from .enemy_kits import intent_matches_frozen_kit

        cb = state.get("combat") if isinstance(state, dict) else None
        if not isinstance(cb, dict):
            return None
        intent = cb.get("pending_intent")
        rows = cb.get("combatants")
        if not isinstance(rows, dict):
            return None
        row = rows.get(str((intent or {}).get("actor"))) if isinstance(intent, dict) else None
        try:
            row_hp_data = row.get("hp") if isinstance(row, dict) else None
            row_hp = int(row_hp_data.get("cur", 0)) if isinstance(row_hp_data, dict) else 0
            prepared_turn = int(intent.get("prepared_turn", turn_i)) \
                if isinstance(intent, dict) else turn_i
            meta = state.get("meta") if isinstance(state, dict) else None
            committed_turn = int(meta.get("turn", -1)) if isinstance(meta, dict) else -1
            accuracy = int(intent.get("accuracy", 0)) if isinstance(intent, dict) else 0
            damage_mod = int(intent.get("damage", 0)) if isinstance(intent, dict) else 0
        except (TypeError, ValueError, OverflowError):
            return None
        # War Room enemies never spring an untelegraphed generic hit.  A missing, new, stale, or
        # malformed intent survives this beat as no action; the code referee prepares/reseats one.
        if not isinstance(row, dict) or row.get("side") != "enemy" or row.get("defeated") \
                or row_hp <= 0 or intent.get("channel") != "hp" \
                or not intent_matches_frozen_kit(intent, row) \
                or prepared_turn != committed_turn or turn_i <= committed_turn:
            return None
        entities = state.get("entities")
        entities = entities if isinstance(entities, dict) else {}
        player_entity = entities.get(peid)
        target_name = (str(player_entity.get("name") or peid)
                       if isinstance(player_entity, dict) else str(peid))
        if intent.get("target") != peid or intent.get("target_name") != target_name:
            return None
        if not peid or not isinstance(player.get("hp"), dict) or not player["hp"].get("max"):
            return None
        hp_max_raw = player["hp"].get("max")
        if isinstance(hp_max_raw, bool):
            return None
        try:
            hp_max = int(hp_max_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if hp_max <= 0:
            return None
        raw_total, die = opposition_roll(state, turn=turn_i)
        total = max(2, min(12, raw_total + max(-2, min(2, accuracy))))
        tier = "CRITS" if total >= 12 else "HITS" if total >= 10 \
            else "GRAZES" if total >= 7 else "MISSES"
        impact = max(1, die + max(-2, min(3, damage_mod)))
        raw_damage = {"CRITS": impact + 2, "HITS": impact,
                      "GRAZES": (impact + 1) // 2, "MISSES": 0}[tier]
        from .state import HP_ADJ_MIN_CAP
        damage_cap = max(HP_ADJ_MIN_CAP, hp_max // 4)
        # This is the same cap _enrich applies to hp_adj.  Brace halves the damage that would
        # actually commit, not an uncapped intermediate that the ledger later rewrites.
        damage_before = min(raw_damage, damage_cap)
        frozen_reaction = intent.get("reaction") if isinstance(intent, dict) else None
        brace_applied = bool(
            reaction == "brace" and isinstance(frozen_reaction, dict)
            and frozen_reaction.get("schema") == "enemy-reaction/1"
            and frozen_reaction.get("kind") == "brace"
            and frozen_reaction.get("cost") == "whole_action"
            and frozen_reaction.get("effect") == "halve_committed_hp")
        damage = damage_before // 2 if brace_applied else damage_before
        intent_id = str(intent.get("id"))
        effect_id = "dmg_opp_" + hashlib.blake2b(
            intent_id.encode(), digest_size=12).hexdigest()
        meta = {**intent, "intent_id": intent_id, "total_raw": raw_total, "total": total,
                "die": die, "tier": tier, "damage": damage,
                "damage_before": damage_before, "damage_after": damage,
                "damage_saved": damage_before - damage,
                "simultaneous": not brace_applied,
                "reason": f"{intent.get('actor_name', 'enemy')} used "
                          f"{intent.get('move_name', 'a committed move')}"}
        if brace_applied:
            meta["reaction"] = {**frozen_reaction, "applied": True}
        return {"op": "hp_adj", "char": peid, "delta": -damage,
                "reason": str(meta["reason"]), "_opposition": meta,
                "_effect_id": effect_id, "_effect_owner": "code"}

    if not _opposition_armed(state, cfg, pending_enemy=pending_enemy):
        return None
    if not isinstance(player.get("hp"), dict) or not player["hp"].get("max"):
        return None
    total, die = opposition_roll(state, turn=turn_i)
    tier = "CRITS" if total >= 12 else "HITS" if total >= 10 \
        else "GRAZES" if total >= 7 else "MISSES"
    damage = {"CRITS": die + 2, "HITS": die, "GRAZES": (die + 1) // 2,
              "MISSES": 0}[tier]
    scene = state.get("scene") if isinstance(state, dict) else None
    location_id = scene.get("location_id", "") if isinstance(scene, dict) else ""
    generic_id = hashlib.blake2b(
        f"generic:{turn_i}:{peid}:{location_id}".encode(),
        digest_size=12).hexdigest()
    return {"op": "hp_adj", "char": peid, "delta": -damage,
            "reason": f"code-owned opposition {tier.lower()}",
            "_effect_id": f"dmg_opp_{generic_id}", "_effect_owner": "code",
            "_opposition": {"total": total, "die": die, "tier": tier,
                             "damage": damage}}


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


def _bind_targets(res: Tier0Result, state: dict, text: str, entity_aware: bool = True,
                  offensive_intent: Optional[bool] = None) -> None:
    """Resolve explicit targets, and implicitly bind only genuinely offensive actions.

    An explicit ``at <name>`` may associate any check with a live combatant, but association alone
    never authorizes damage. Otherwise a named foe, attack object, or lone-foe fallback binds only
    when the Player actually performs an offensive action. Nothing is guessed for utility checks.
    """
    from .state import (
        CombatReferenceStatus,
        live_combatants,
        resolve_combat_reference,
    )

    def _player_reference_id(value: object) -> str | None:
        reference = resolve_combat_reference(state, value)
        if reference.match_kind != "unique_token_subset" \
                and reference.status is CombatReferenceStatus.RESOLVED \
                and reference.selected is not None:
            return reference.selected.combatant_id
        return None

    foes = live_combatants(state, "enemy")
    if not foes or not res.checks:
        return
    offensive = _has_attack_intent(text) if offensive_intent is None else offensive_intent
    low = " " + _norm_phrase(text or "") + " "
    unmatched_explicit_target = bool(_EXPLICIT_TARGET_MARKER_RE.search(text or "")) \
        and not _RELATIVE_ENEMY_TARGET_RE.search(text or "")
    named = sorted(foes, key=lambda r: -len(combatant_label(r, "")))
    for c in res.checks:
        if c.get("target"):
            cid = _player_reference_id(c["target"])
            c["target"] = cid                    # unresolved -> None (stays a plain check)
            continue
        if not offensive:
            c["target"] = None                   # observing a foe is not striking that foe
            continue
        named_hits = [
            (len(_norm_phrase(combatant_label(r, ""))), r["id"])
            for r in named
            if _norm_phrase(combatant_label(r, ""))
            and f" {_norm_phrase(combatant_label(r, ''))} " in low
        ]
        longest = max((length for length, _cid in named_hits), default=0)
        exact_named = {cid for length, cid in named_hits if length == longest}
        hit = next(iter(exact_named)) if len(exact_named) == 1 else None
        if hit is None and entity_aware:         # entity-aware: the OBJECT of the attack (prep
            for span in _attack_object_spans(text):    # object first, then verb object) resolved
                cid = _player_reference_id(" ".join(_norm_phrase(span).split()[:4]))
                if cid:
                    hit = cid                    # to a LIVE foe — precise, before the lone fallback
                    break
        if hit is None and len(foes) == 1 and not unmatched_explicit_target:
            hit = foes[0]["id"]                  # one foe + an attack verb: unambiguous
        c["target"] = hit


# 2026-07-10 (Eranmor): the DM emitted "[TAGS] scene_active | ..." / "[AWAIT]" lines —
# invented grammar the engine silently ignored, and nothing ever corrected it. Bracket
# lines whose head is neither a real channel nor an engine-block echo are collected so the
# NEXT prompt carries a one-line protocol corrective (compose renders it; self-clearing).
_KNOWN_TAG_HEADS = {"status", "condition", "valence", "scene", "item", "quest", "affinity",
                    "hp", "foe", "ally", "clash", "battle", "tide", "time", "rumor",
                    "check"}   # check heals (R8b)
_ECHO_HEADS = {"directive", "player", "rules", "effects", "gear", "inventory", "factions",
               "relations", "nearby", "world", "opposition", "war", "ally", "key",
               "notice", "protocol", "context", "start"}
_BRACKET_HEAD_RE = re.compile(r"^\s*\[\s*([A-Za-z][A-Za-z _-]{0,24}?)\s*(?:\||\])",
                              re.MULTILINE)
_RESERVED_OUTPUT_HEAD_RE = re.compile(
    r"^[ \t]*(?:(?:>[ \t]*)|(?:[-+*][ \t]+)|(?:`{1,3}[ \t]*))*"
    r"\[\s*(DIRECTIVE|ENEMY\s+(?:INTENT|ACTION)|WAR|INIT|PLAYER|RULES|OPPOSITION|PROTOCOL|"
    r"CONTEXT\s+PRIORITY|AETHER\s+P[0-3])"
    r"(?:\s+[^\]\r\n|]*)?\]",
    re.IGNORECASE | re.MULTILINE)


def _scan_off_protocol(text: str) -> list[str]:
    """Bracket-line heads in the DM's reply that match NO known grammar (nudge list)."""
    seen: list[str] = []
    # Versioned engine-input echoes contain schema slashes, which the deliberately narrow
    # generic tag-head scanner does not accept. Classify both versioned and unversioned forms
    # explicitly so the next request can correct the narrator without ever parsing the echo.
    for match in _RESERVED_OUTPUT_HEAD_RE.finditer(text or ""):
        head = " ".join(match.group(1).upper().split())
        if head not in seen:
            seen.append(head)
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
               # clause boundaries are grammar, not nouns ("slash its neck BEFORE it pounces")
               "before", "after", "until", "while", "when", "once", "unless", "although",
               "because", "if", "whether", "whereas",
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
    most likely to NAME who is being hit. A possessive body-part target ("slash at its neck")
    also exposes the pre-attack prefix so a named antecedent ("rush the wolf, then...") can bind.
    Used by the entity-aware target picker and conservative foe floor."""
    spans = []
    verb_tails = []
    antecedents = []
    for m in _performed_attack_verb_matches(user_text or ""):
        tail = user_text[m.end():]
        verb_tails.append(tail)
        words = _norm_phrase(tail).split()[:5]
        if len(words) >= 3 and words[0] in {"at", "across", "into", "through"} \
                and words[1] in {"its", "his", "her", "their"} \
                and words[2] in _BODY_PARTS:
            # The antecedent is more precise than the possessive body-part tail. Put it first,
            # before a later conjunction can be mistaken for the attacked noun.
            antecedents.append(user_text[:m.start()])
    spans.extend(antecedents)
    for mi in _RUN_WEAPON_THROUGH_RE.finditer(user_text or ""):
        spans.append(user_text[mi.end():])
    for mp in _TARGET_PREP_RE.finditer(user_text or ""):
        spans.append(user_text[mp.end():])
    spans.extend(verb_tails)
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


def _entity_target(state: dict, user_text: str, peid) -> tuple[Optional[tuple], bool]:
    """The PRESENT cast member attacked plus an ambiguity bit.

    Object-of-attack tokens remain the strongest evidence.  Equal best scores now abstain instead
    of silently choosing whichever entity happened to be inserted first.  A possessive short-name
    reference (``Kessa's spear``) is also valid when it has one present owner; a shared short name
    returns ``ambiguous=True`` so the prose fallback cannot mint a duplicate alias.
    """
    cast = _present_cast(state, peid)
    if not cast:
        return None, False
    for span in _attack_object_spans(user_text):
        head = {t for t in _norm_phrase(span).split()[:4]
                if len(t) >= 3 and t not in _PRESENT_SHORT_REFERENCE_STOP}
        if not head:
            continue
        scored = []
        for eid, name, ntoks in cast:
            score = len(ntoks & head)
            if score:
                scored.append((score, eid, name))
        if scored:
            top = max(row[0] for row in scored)
            winners = [(eid, name) for score, eid, name in scored if score == top]
            if len(winners) == 1:
                return winners[0], False
            return None, True

    references = _present_npc_references(state, peid)
    possessive_ids: set[str] = set()
    possessive_ambiguous = False
    attack_spans = _performed_attack_verb_matches(user_text or "")
    for start, end, _reference, entity_ids in _reference_spans(references, user_text):
        owned = re.match(r"^(?:'s|’s)\s+([a-z0-9_-]+)", (user_text or "")[end:], re.IGNORECASE)
        if not owned:
            continue
        possessed = _norm_phrase(owned.group(1))
        if possessed not in _HELD_WORDS and possessed not in _BODY_PARTS \
                and possessed not in {"armor", "armour"}:
            continue
        preceding = [match for match in attack_spans if match.end() <= start]
        if not preceding:
            continue
        bridge = (user_text or "")[preceding[-1].end():start]
        if re.search(
                r"[.!?;\n]|\b(?:while|when|whereas|as|who|which|although|because|before|after)\b",
                bridge, re.IGNORECASE):
            continue
        if not re.fullmatch(
                r"\s*(?:(?:the|a|an)\s+)?|.*\b(?:at|into|onto|upon|through|toward|towards|"
                r"around|against|across)\s*",
                bridge, re.IGNORECASE):
            continue
        possessive_ids.update(entity_ids)
        possessive_ambiguous = possessive_ambiguous or len(entity_ids) > 1
    if possessive_ambiguous or len(possessive_ids) > 1:
        return None, True
    if possessive_ids:
        eid = next(iter(possessive_ids))
        name = next(name for cast_eid, name, _toks in cast if cast_eid == eid)
        return (eid, name), False

    low = " " + _norm_phrase(user_text or "") + " "     # last resort: a present name in the text
    winners = [(eid, name) for eid, name, ntoks in cast
               if ntoks and all(f" {t} " in low for t in ntoks)]
    if len(winners) == 1:
        return winners[0], False
    return None, len(winners) > 1


def _present_target_hint(cast: list, hints: list[str]) -> tuple[Optional[tuple], bool]:
    """Resolve a check's short target against the present cast only when it is unambiguous.

    Exact custom-skill constructions can retain just ``Kessa`` even when the authored entity is
    ``Span Warden Kessa``. The hint is already the check detector's target, so matching all of its
    tokens inside one present name is safer than falling through to an ungrounded combat extra.
    A shared short name deliberately abstains instead of choosing or minting either person.
    """
    resolved: dict[str, tuple] = {}
    ambiguous = False
    for hint in hints:
        htoks = {t for t in _norm_phrase(hint).split() if len(t) >= 3}
        if not htoks:
            continue
        matches = [(eid, name) for eid, name, ntoks in cast if htoks <= ntoks]
        if len(matches) == 1:
            resolved[matches[0][0]] = matches[0]
        elif len(matches) > 1:
            ambiguous = True
    if len(resolved) == 1 and not ambiguous:
        return next(iter(resolved.values())), False
    return None, ambiguous or len(resolved) > 1


def _floor_stage_foe(res: Tier0Result, state: dict, user_text: str,
                     dm_text: str, entity_aware: bool = True,
                     semantic_frame_id: str = "") -> Optional[dict]:
    """A grounded `combatant_spawn` for the target the Player is attacking, or None. Entity-aware
    FIRST (2026-07-10): a strike on a PRESENT cast member stages THAT person (grounded by their
    existence — no DM-prose echo needed); else the conservative DM-prose token-run heuristic."""
    checks = [
        check for check in res.checks
        if not semantic_frame_id
        or str(check.get("_semantic_frame_id") or "") == semantic_frame_id
    ]
    if not _has_attack_intent(user_text) and not any(c.get("_attack") for c in checks):
        return None
    from .state import live_combatants, resolve_entity_ref
    if live_combatants(state, "enemy"):
        return None                                  # foes exist — binding handles it
    peid, _p = _player_card(state)
    if entity_aware:                                 # the strike lands on a REAL on-scene person
        cast = _present_cast(state, peid)
        hints = [str(check["target"]) for check in checks if check.get("target")]
        hinted, ambiguous_hint = _present_target_hint(cast, hints)
        if ambiguous_hint:
            return None
        pick = hinted
        if pick is None:
            pick, ambiguous_text = _entity_target(state, user_text, peid)
            if ambiguous_text:
                return None
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
    cands = [str(check["target"]) for check in checks if check.get("target")]
    cands.extend(_attack_object_spans(user_text))           # target objects before verb objects
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
        # If the conservative prose floor rediscovered a short form of a PRESENT authored NPC,
        # the entity-aware path above has already had the only authority to select that person.
        # Falling through here would recreate the Root U bug by minting a second generic alias.
        if entity_aware and _present_npc_references(state, peid).get(_norm_phrase(name)):
            continue
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
                    pending_foe: Optional[dict] = None,
                    offensive_intent: bool = False) -> None:
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
    reserved: dict[str, int] = {}                    # request-local; reducer applies after all checks
    for c in res.checks:
        # Registry identity decides whether this is a skill at all. Semantic abstention may
        # explain why a known skill could not align, but must not mask an unknown token as a
        # generic scope ambiguity.
        registered_sid = reg.resolve_skill(c["skill"], player)
        if registered_sid is None:
            res.notices.append(f"unknown skill '{c['skill']}': add it to the registry "
                               "(nothing freestyle; doc 05 section 5.2)")
            continue
        if c.get("_semantic_abstain"):
            # Construction safety and capability ownership are independent diagnostics.  An
            # unresolved target must still prevent the roll, but it must not hide that an
            # explicitly requested active ability is not owned.  Validate only identity here;
            # cost, cooldown, and execution remain below the actionable-frame gate.
            for ref in c.get("use", []):
                _aid, adef = _find_active_ability(reg, player, ref) \
                    if player else (None, None)
                if adef is None:
                    res.notices.append(
                        f"you don't know an activated ability '{ref}'"
                    )
            disposition = str(c.get("_semantic_disposition") or "")
            if disposition == "recognition_only":
                res.notices.append(
                    "described or attributed action retained as meaning; no roll executed"
                )
            elif disposition == "invalid_scope_conflict":
                res.notices.append(
                    "conflicting semantic scope: clarify the action before rolling"
                )
            else:
                res.notices.append(
                    "unresolved semantic scope or role: clarify before rolling"
                )
            continue
        sid = registered_sid   # snapshot-first: a freestyle/evolved skill resolves too
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
        pd = registry.parse_dice(dice)
        if pd is None:
            res.notices.append(f"bad dice spec: {dice}")
            continue
        n_keep, sides, flat = pd
        cost = registry.skill_cost(entry)       # RPG-5 (doc 10 §5.4): the resource gate —
        provisional: dict[str, int] = {}        # reserve the FULL skill cost before any active
        skill_held, short = _reserve_cost(
            player,
            reserved,
            cost,
            legacy_registry_waiver=_legacy_registry_cost_waiver(player, "skills", sid),
        )
        if short:                               # spent, not blocked: rest/recover re-opens it
            res.notices.append(f"not enough {short[0]} for {reg.skill_label(sid, player)} "
                               f"({short[1]}/{short[2]} needed) — recover first; not a roll")
            continue
        _merge_cost(provisional, skill_held)
        # ---- dice-shaping abilities (2026-07-07, Bean): a SKILL sets the modifier; an
        # ABILITY shapes the dice. Passive edge/ward auto-apply to matching checks; active
        # extra_die/reroll/surge fire only when the player invokes `use <ability>` (freedom is
        # routed — an unknown/unaffordable/cooling ability is a visible notice, the roll goes on).
        edge_extra, ward, shaped = 0, 0, []
        applied_passive_ids: list[str] = []
        executed_active_ids: list[str] = []
        for aid, adef in (reg.known_abilities(player).items() if player else ()):
            if not registry.ability_applies(adef, sid):
                continue
            if registry.ability_is_active(adef):    # an ACTIVE never auto-applies — it is
                continue                            # invoked (`use`), paid for, and cooled
            mech = registry.ability_mechanic(adef)
            if mech == "edge":                      # passive advantage: +dice, keep best
                edge_extra += max(1, registry.ability_magnitude(adef, 1))
                shaped.append(str(adef.get("name", aid)))
                applied_passive_ids.append(str(aid))
            elif mech == "ward":                    # passive guard: raise the failure floor
                ward = max(ward, max(1, registry.ability_magnitude(adef, 1)))
                shaped.append(str(adef.get("name", aid)))
                applied_passive_ids.append(str(aid))
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
            # Only one on-fail shaper can ride one check.  A later declaration is not admitted and
            # therefore must not consume the request-local reservation budget.
            if mech in registry.ON_FAIL_MECHANICS and onfail is not None:
                continue
            ability_cost = registry.skill_cost(adef)
            ability_held, ability_short = _reserve_cost(
                player,
                reserved,
                ability_cost,
                legacy_registry_waiver=_legacy_registry_cost_waiver(
                    player, "abilities", aid,
                ),
            )
            if ability_short:
                res.notices.append(f"not enough resources to use {label} — recover first")
                blocked.append({"name": label, "why": "not enough resources"})
                continue
            _merge_cost(provisional, ability_held)
            if mech == "surge":                     # on use: big bonus + lift the scope ceiling
                surge_mod += max(1, registry.ability_magnitude(adef, 2))
                surge_lift += 1
                _merge_cost(pay_ability, registry.skill_cost(adef))
                if int(adef.get("cooldown_turns", 0)) > 0:
                    cd_set[aid] = now + int(adef["cooldown_turns"])
                shaped.append(label)
                executed_active_ids.append(str(aid))
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
                executed_active_ids.append(str(aid))

        kept, pool = registry.roll_keep(n_keep, edge_extra, sides, rng)
        base_pool = list(pool)
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
        on_fail_receipt = None
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
                    selected = "reroll"
                else:
                    selected = "base"
                draw_pool = list(pool2)
            else:                                   # extra_die: literally ADD dice to the pool
                draw_pool = [rng.randint(1, sides) for _ in range(mag)]
                pool = pool + draw_pool
                kept = sorted(pool, reverse=True)[:n_keep]
                tier, total = _settle(kept)
                selected = "augmented"
            fired = str(oadef.get("name", oaid))
            improved = registry.CHECK_TIERS.index(tier) > registry.CHECK_TIERS.index(base_tier)
            on_fail_receipt = {
                "ability_id": str(oaid),
                "mechanic": omech,
                "draw_pool": draw_pool,
                "selected": selected,
            }
            _merge_cost(pay_ability, registry.skill_cost(oadef))
            if int(oadef.get("cooldown_turns", 0)) > 0:
                cd_set[oaid] = now + int(oadef["cooldown_turns"])
            shaped.append(fired)
            executed_active_ids.append(str(oaid))
        if over >= 3:                               # RPG-5 (doc 10 §8): reaching THAT far past
            forced = "crit_fail" if over >= 4 else "fail"   # mastery fails outright — surge
            if registry.CHECK_TIERS.index(tier) > registry.CHECK_TIERS.index(forced):
                tier = forced                       # can't beat the wall; deep mastery can
            res.notices.append(f"scope '{scope}' is far beyond {sid} mastery — "
                               f"the attempt fails outright (doc 10 §8)")   # "Alter Reality" rule

        op = {"op": "check", "skill": sid, "result": total, "tier": tier,
              "_mod": eff, "_declared_mod": int(c["mod"]),
              "_dice": dice, "_seed": kept}
        semantic_ref = c.get("_semantic_frame_ref")
        if semantic_ref:
            op["_semantic_frame_ref"] = semantic_ref
        if c.get("dm_called"):
            # Read compatibility for old saved/evaluation rows only. Current narration can
            # neither create nor arm this provenance.
            op["_dm_called"] = True
        if player_eid:
            op["char"] = player_eid          # a real entity (kind=player) -> resolves cleanly
        if c["dc"] is not None:
            op["dc"] = c["dc"]
        if scope is not None:
            op["scope"], op["_scope_over"] = scope, over
        # Every V3 settled check carries the same versioned roll-shape receipt, including the
        # ordinary empty-shaping case.  The live settlement authority validates this exact policy;
        # omitting the shape would make a plain Stealth/Haggle check impossible to admit.
        if semantic_ref or shaped or fired or edge_extra or ward or surge_mod or use_mod:
            op["_shape"] = {
                "schema": CHECK_ROLL_SHAPE_SCHEMA,
                "abilities": sorted(set(shaped)),
                "applied_passive_ids": sorted(set(applied_passive_ids)),
                "executed_active_ids": sorted(set(executed_active_ids)),
                "base_pool": base_pool,
                "on_fail": on_fail_receipt,
                "fired": fired,
                "improved": improved,
                "pool": list(pool),
                "kept": list(kept),
                "edge": edge_extra,
                "ward": ward,
                "surge": surge_mod,
                "burst": use_mod,
            }
        if blocked:
            op["_ability_blocked"] = blocked[:3]    # baked: directive + HUD tell the truth
        pay: dict = {}                              # SKILL cost (half on a miss) + ABILITY costs
        if cost and player_eid:                     # (full) — custom defs were admitted only when
                                                    # every declared pool existed; legacy registry
                                                    # costs alone may retain the untracked waiver
            for r, a in cost.items():
                if _tracked_pool(player, r):
                    pay[r] = a if tier != "fail" else max(1, (a + 1) // 2)
        for r, a in pay_ability.items():
            if _tracked_pool(player, r):
                pay[r] = pay.get(r, 0) + int(a)
        # The skill held its full price for admission. A failed check keeps the existing half-cost
        # rule, and an unused on-fail active keeps nothing; release those differences before the
        # next declared check in this same request is considered.
        _reconcile_cost(reserved, provisional, pay)
        if pay:
            op["_cost"] = pay
        if cd_set:
            op["_ability_cd"] = cd_set              # cooldowns set for fired/used actives
        strike = None                               # Phase 1: an offensive check bound to an enemy
        # Target association alone is not strike authority. A utility check may explicitly name a
        # foe, and a combat opening may stage one, but HP can change only for an offensive action.
        check_offensive = (
            bool(c.get("_attack"))
            if c.get("_semantic_frame_id")
            else offensive_intent
        )
        tgt = c.get("target") if check_offensive else None
        # fix C (2026-07-10): a foe the FLOOR is staging THIS batch is not in state yet, but its
        # combatant_spawn (prio 2) applies BEFORE combatant_hp (prio 6), so a strike against its
        # baked cid lands the same turn — the opening blow of a floor-started fight no longer whiffs.
        # An implicit binding requires offensive intent, and this final gate also prevents an
        # explicitly targeted utility check from becoming a strike.
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
                fname = combatant_label(row, str(cid))
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
                    strike = {"op": "combatant_hp", "target": cid, "delta": 0,
                              "reason": f"{reg.skill_label(sid, player)} {tier}",
                              "_strike": True}   # a settled miss owns the no-damage outcome too
        if strike is not None and semantic_ref:
            strike["_semantic_frame_ref"] = semantic_ref
        res.rule_ops.append(op)
        if strike is not None:
            res.rule_ops.append(strike)
        if player_eid:                              # RPG-5 (doc 10 §4): use grows mastery —
            amt = MASTERY_TICKS.get(tier, 0)        # code-side, scene-capped in the reducer
            if amt:
                mastery = {"op": "master_tick", "char": player_eid,
                           "skill": sid, "amount": amt}
                if semantic_ref:
                    mastery["_semantic_frame_ref"] = semantic_ref
                res.rule_ops.append(mastery)
            if tier == "crit_fail":                 # RPG-5 (doc 10 §5/§8): a crit-fail leaves
                consequence = {                     # a mark; overreach bites back harder
                    "op": "effect_add", "char": player_eid,
                    "effect": "Backlash" if over else "Strained", "kind": "status"}
                if semantic_ref:
                    consequence["_semantic_frame_ref"] = semantic_ref
                res.rule_ops.append(consequence)


def _group_weapon_attack_settlements(
    res: Tier0Result,
    semantic_snapshots: list[dict],
    semantic_bindings: dict[str, dict],
) -> None:
    """Replace each complete V3 weapon-attack operation set with one settlement envelope.

    Tier-0 still exposes each member as an adjacent top-level projection for existing reducers and
    UI readers.  The settlement reducer owns whether complete projections may apply.  An
    incomplete frame retains its semantic receipts but loses every frame-bound mechanic operation,
    so its roll, cost, cooldown, mastery, HP, or opening mutations cannot leak independently.
    """
    groups: list[tuple[int, int, dict, list[dict], set[int]]] = []
    claimed: set[int] = set()
    rejected: set[int] = set()

    def reject_frame_ops(indices: set[int]) -> None:
        if not indices:
            return
        rejected.update(indices)
        res.notices.append(
            "weapon attack remained unresolved: its atomic mechanic group was incomplete"
        )

    for frame_order, frame in enumerate(semantic_snapshots):
        if frame.get("schema") != "semantic-action-frame/3" \
                or frame.get("action_class") != "weapon_attack":
            continue
        frame_id = str(frame.get("frame_id") or "")
        frame_ref = str(frame.get("fingerprint") or "")
        binding = semantic_bindings.get(frame_id)
        same_frame = {
            index for index, op in enumerate(res.rule_ops)
            if frame_ref and op.get("_semantic_frame_ref") == frame_ref
        }
        if not frame_ref or binding is None:
            reject_frame_ops(same_frame)
            continue
        try:
            settlement_ref = weapon_attack_settlement_ref(frame, binding)
        except MechanicSettlementError:
            reject_frame_ops(same_frame)
            continue

        matching_checks = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "check"
            and op.get("_semantic_frame_ref") == frame_ref
            and op.get("skill") == frame.get("capability_id")
            and op.get("char") == frame.get("actor_id")
        ]
        matching_strikes = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "combatant_hp"
            and op.get("_semantic_frame_ref") == frame_ref
            and op.get("_strike") is True
        ]
        if len(matching_checks) != 1 or len(matching_strikes) != 1:
            reject_frame_ops(same_frame)
            continue
        check_index = matching_checks[0]
        strike_index = matching_strikes[0]
        check = res.rule_ops[check_index]
        strike = res.rule_ops[strike_index]
        target = str(strike.get("target") or "")
        if not target:
            reject_frame_ops(same_frame)
            continue

        spawn_indices = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "combatant_spawn"
            and op.get("_semantic_frame_ref") == frame_ref
        ]
        primary_spawns = [
            index for index in spawn_indices
            if target in {
                str(res.rule_ops[index].get("_cid") or ""),
                str(res.rule_ops[index].get("char") or ""),
            }
        ]
        if len(spawn_indices) > COMBAT_SIDE_CAP \
                or (spawn_indices and len(primary_spawns) != 1):
            reject_frame_ops(same_frame)
            continue
        scene_indices = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "scene_set"
            and op.get("_semantic_frame_ref") == frame_ref
            and op.get("_floor") is True
            and op.get("phase") == "climax"
        ]
        if len(scene_indices) > 1:
            reject_frame_ops(same_frame)
            continue
        mastery_indices = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "master_tick"
            and op.get("_semantic_frame_ref") == frame_ref
            and op.get("skill") == check.get("skill")
            and op.get("char") == check.get("char")
        ]
        if len(mastery_indices) > 1:
            reject_frame_ops(same_frame)
            continue
        consequence_indices = [
            index for index, op in enumerate(res.rule_ops)
            if op.get("op") == "effect_add"
            and op.get("_semantic_frame_ref") == frame_ref
            and op.get("char") == check.get("char")
            and op.get("effect") in {"Backlash", "Strained"}
            and op.get("kind") == "status"
        ]
        if check.get("tier") == "crit_fail":
            if len(consequence_indices) != 1:
                reject_frame_ops(same_frame)
                continue
        else:
            consequence_indices = []

        indices = {
            check_index,
            strike_index,
            *spawn_indices,
            *scene_indices,
            *mastery_indices,
            *consequence_indices,
        }
        if indices & claimed:
            reject_frame_ops(same_frame)
            continue
        ordered_indices = sorted(indices)
        members = [deepcopy(res.rule_ops[index]) for index in ordered_indices]
        wrapper = {
            "op": "mechanic_settlement_commit",
            "contract_id": WEAPON_ATTACK_CONTRACT,
            "settlement_ref": settlement_ref,
            "frame_ref": frame_ref,
            "members": members,
            "_semantic_frame_ref": frame_ref,
        }
        projections = []
        for member_index, member in enumerate(members):
            projection = deepcopy(member)
            projection["_settlement_ref"] = settlement_ref
            projection["_settlement_member_index"] = member_index
            projections.append(projection)
        claimed.update(indices)
        groups.append((ordered_indices[0], frame_order, wrapper, projections, indices))

    if not groups and not rejected:
        return
    groups.sort(key=lambda row: (row[0], row[1]))
    insertions = {first: (wrapper, projections) for first, _order, wrapper, projections, _ in groups}
    selected = set().union(*(indices for *_prefix, indices in groups))
    regrouped: list[dict] = []
    for index, op in enumerate(res.rule_ops):
        if index in rejected:
            continue
        insertion = insertions.get(index)
        if insertion is not None:
            wrapper, projections = insertion
            regrouped.append(wrapper)
            regrouped.extend(projections)
        if index not in selected:
            regrouped.append(op)
    res.rule_ops = regrouped


def _group_combat_opening_settlements(
    res: Tier0Result,
    semantic_snapshots: list[dict],
    semantic_bindings: dict[str, dict],
) -> None:
    """Replace each complete V3 combat transition with one atomic opening envelope."""
    groups: list[tuple[int, int, dict, list[dict], set[int]]] = []
    claimed: set[int] = set()
    rejected: set[int] = set()

    def reject_frame_ops(indices: set[int]) -> None:
        if not indices:
            return
        rejected.update(indices)
        res.notices.append(
            "combat opening remained unresolved: its atomic admission group was incomplete"
        )

    for frame_order, frame in enumerate(semantic_snapshots):
        if frame.get("schema") != "semantic-action-frame/3" \
                or frame.get("action_class") != "combat_opening":
            continue
        frame_id = str(frame.get("frame_id") or "")
        frame_ref = str(frame.get("fingerprint") or "")
        binding = semantic_bindings.get(frame_id)
        same_frame = {
            index for index, op in enumerate(res.rule_ops)
            if frame_ref and op.get("_semantic_frame_ref") == frame_ref
        }
        if not frame_ref or binding is None:
            reject_frame_ops(same_frame)
            continue
        try:
            settlement_ref = combat_opening_settlement_ref(frame, binding)
        except MechanicSettlementError:
            reject_frame_ops(same_frame)
            continue

        spawn_indices = [
            index for index in same_frame
            if res.rule_ops[index].get("op") == "combatant_spawn"
        ]
        scene_indices = [
            index for index in same_frame
            if res.rule_ops[index].get("op") == "scene_set"
            and res.rule_ops[index].get("_floor") is True
            and res.rule_ops[index].get("phase") == "climax"
        ]
        target = str(frame.get("target_entity_id") or "")
        primary_spawns = [
            index for index in spawn_indices
            if target in {
                str(res.rule_ops[index].get("_cid") or ""),
                str(res.rule_ops[index].get("char") or ""),
            }
        ]
        indices = {*spawn_indices, *scene_indices}
        complete = (
            bool(target)
            and 1 <= len(spawn_indices) <= COMBAT_SIDE_CAP
            and len(primary_spawns) == 1
            and len(scene_indices) <= 1
            and indices == same_frame
            and not indices.intersection(claimed)
        )
        if not complete:
            reject_frame_ops(same_frame)
            continue

        ordered_indices = sorted(indices)
        members = [deepcopy(res.rule_ops[index]) for index in ordered_indices]
        wrapper = {
            "op": "mechanic_settlement_commit",
            "contract_id": COMBAT_OPENING_CONTRACT,
            "settlement_ref": settlement_ref,
            "frame_ref": frame_ref,
            "members": members,
            "_semantic_frame_ref": frame_ref,
        }
        projections: list[dict] = []
        for member_index, member in enumerate(members):
            projection = deepcopy(member)
            projection["_settlement_ref"] = settlement_ref
            projection["_settlement_member_index"] = member_index
            projections.append(projection)
        claimed.update(indices)
        groups.append((ordered_indices[0], frame_order, wrapper, projections, indices))

    if not groups and not rejected:
        return
    groups.sort(key=lambda row: (row[0], row[1]))
    insertions = {
        first: (wrapper, projections)
        for first, _order, wrapper, projections, _indices in groups
    }
    selected = set().union(*(indices for *_prefix, indices in groups)) if groups else set()
    regrouped: list[dict] = []
    for index, op in enumerate(res.rule_ops):
        if index in rejected:
            continue
        insertion = insertions.get(index)
        if insertion is not None:
            wrapper, projections = insertion
            regrouped.append(wrapper)
            regrouped.extend(projections)
        if index not in selected:
            regrouped.append(op)
    res.rule_ops = regrouped


def _group_skill_check_settlements(
    res: Tier0Result,
    semantic_snapshots: list[dict],
    semantic_bindings: dict[str, dict],
) -> None:
    """Freeze each complete V3 non-impact check as one atomic settlement envelope.

    A skill frame may name a contextual referent, but this contract owns no target admission,
    scene transition, or HP mutation.  If Tier-0 cannot close the exact check and its mandatory
    side effects, it preserves the semantic receipts and removes every frame-bound mechanic op.
    """
    groups: list[tuple[int, int, dict, list[dict], set[int]]] = []
    claimed: set[int] = set()
    rejected: set[int] = set()

    for frame_order, frame in enumerate(semantic_snapshots):
        if frame.get("schema") != "semantic-action-frame/3" \
                or frame.get("action_class") in NON_SKILL_CHECK_ACTION_CLASSES:
            continue
        frame_id = str(frame.get("frame_id") or "")
        frame_ref = str(frame.get("fingerprint") or "")
        binding = semantic_bindings.get(frame_id)
        same_frame = {
            index for index, op in enumerate(res.rule_ops)
            if op.get("_semantic_frame_ref") == frame_ref
        }
        matching_check_candidates = [
            index for index in same_frame
            if res.rule_ops[index].get("op") == "check"
            and res.rule_ops[index].get("skill") == frame.get("capability_id")
            and res.rule_ops[index].get("char") == frame.get("actor_id")
        ]
        if not matching_check_candidates:
            continue
        # Impact-bearing frames need their own complete contract.  Preserve the older grapple and
        # lethal-intent lanes without pretending their check alone is a complete non-impact
        # occurrence.  Every other executable frame that produced an impact beside a check is an
        # invalid skill group and must fail closed rather than release raw mechanics.
        if any(
            res.rule_ops[index].get("op") in {"combatant_spawn", "scene_set", "combatant_hp"}
            for index in same_frame
        ):
            rejected.update(same_frame)
            if same_frame:
                res.notices.append(
                    "skill check remained unresolved: its atomic mechanic group was incomplete"
                )
            continue
        if not frame_ref or binding is None:
            rejected.update(same_frame)
            if same_frame:
                res.notices.append(
                    "skill check remained unresolved: its exact semantic binding was unavailable"
                )
            continue
        try:
            settlement_ref = skill_check_settlement_ref(frame, binding)
        except MechanicSettlementError:
            rejected.update(same_frame)
            if same_frame:
                res.notices.append(
                    "skill check remained unresolved: its semantic contract did not close"
                )
            continue

        matching_checks = matching_check_candidates
        complete = len(matching_checks) == 1
        indices: set[int] = set(matching_checks)
        if complete:
            check = res.rule_ops[matching_checks[0]]
            expected_mastery = int(MASTERY_TICKS.get(str(check.get("tier") or ""), 0))
            mastery_indices = [
                index for index in same_frame
                if res.rule_ops[index].get("op") == "master_tick"
                and res.rule_ops[index].get("skill") == check.get("skill")
                and res.rule_ops[index].get("char") == check.get("char")
                and res.rule_ops[index].get("amount") == expected_mastery
            ]
            consequence_indices = [
                index for index in same_frame
                if res.rule_ops[index].get("op") == "effect_add"
                and res.rule_ops[index].get("char") == check.get("char")
                and res.rule_ops[index].get("effect")
                == ("Backlash" if int(check.get("_scope_over", 0) or 0) else "Strained")
                and res.rule_ops[index].get("kind") == "status"
            ]
            complete = (
                len(mastery_indices) == (1 if expected_mastery > 0 else 0)
                and len(consequence_indices)
                == (1 if check.get("tier") == "crit_fail" else 0)
            )
            indices.update(mastery_indices)
            indices.update(consequence_indices)

        # Exact closure also proves there is no HP, scene, target-admission, or stray same-frame
        # side effect hidden beside the accepted check group.
        if not complete or indices != same_frame or indices & claimed:
            rejected.update(same_frame)
            if same_frame:
                res.notices.append(
                    "skill check remained unresolved: its atomic mechanic group was incomplete"
                )
            continue

        ordered_indices = sorted(indices)
        members = [deepcopy(res.rule_ops[index]) for index in ordered_indices]
        wrapper = {
            "op": "mechanic_settlement_commit",
            "contract_id": SKILL_CHECK_CONTRACT,
            "settlement_ref": settlement_ref,
            "frame_ref": frame_ref,
            "members": members,
            "_semantic_frame_ref": frame_ref,
        }
        projections: list[dict] = []
        for member_index, member in enumerate(members):
            projection = deepcopy(member)
            projection["_settlement_ref"] = settlement_ref
            projection["_settlement_member_index"] = member_index
            projections.append(projection)
        claimed.update(indices)
        groups.append((ordered_indices[0], frame_order, wrapper, projections, indices))

    if not groups and not rejected:
        return
    groups.sort(key=lambda row: (row[0], row[1]))
    insertions = {
        first: (wrapper, projections)
        for first, _order, wrapper, projections, _indices in groups
    }
    selected = set().union(*(indices for *_prefix, indices in groups)) if groups else set()
    regrouped: list[dict] = []
    for index, op in enumerate(res.rule_ops):
        insertion = insertions.get(index)
        if insertion is not None:
            wrapper, projections = insertion
            regrouped.append(wrapper)
            regrouped.extend(projections)
        if index not in selected and index not in rejected:
            regrouped.append(op)
    res.rule_ops = regrouped


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


def _kill_intent(res: Tier0Result, state: dict, cfg, user_text: str,
                 frame: ActionFrame | None = None, semantic_ref: str = "") -> None:
    """Gate and resolve one lethal declaration from its frozen semantic interpretation."""
    if (state.get("combat") or {}).get("active"):
        return                                       # inside a fight kills come from HP (War Room)
    if frame is not None:
        if frame.action_class not in ("kill_attempt", "grand_kill_attempt") \
                or not frame.mechanically_actionable:
            return
    elif not _KILL_VERBS.search(user_text or "") and not _GRAND.search(user_text or ""):
        return
    peid, _player = _player_card(state)
    if not peid:
        return
    ents = state.get("entities") or {}
    eff = state.get("effects") or {}
    present = [(eid, e) for eid, e in ents.items()
               if isinstance(e, dict) and e.get("present") and eid != peid
               and e.get("kind") in ("character", "npc")]
    tid = str(frame.target_entity_id or "") if frame is not None else ""
    tname = str(frame.target_name or "") if frame is not None else ""
    if frame is None:
        low = " " + _norm_phrase(user_text) + " "
        for eid, e in sorted(present, key=lambda p: -len(str(p[1].get("name", "")))):
            nm = _norm_phrase(str(e.get("name", "")))
            toks = [t for t in nm.split() if len(t) >= 3]
            if nm and (f" {nm} " in low or (toks and all(f" {t} " in low for t in toks))):
                tid, tname = eid, str(e.get("name", ""))
                break
    elif not any(eid == tid for eid, _entity in present):
        return
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
    grand_declared = frame.action_class == "grand_kill_attempt" if frame is not None \
        else bool(_GRAND.search(user_text or ""))
    grand = any(str(o.get("scope", "")).lower() in ("epic", "mythic") for o in check_ops) \
        or (grand_declared and bool(check_ops))

    def _kill_ops(reason: str, xp: int) -> None:
        bonus = 20 if tent.get("kind") == "npc" or tent.get("role") else 0
        statement = f"{pname} killed {tname} ({reason})"
        cause_occurrence_ref = "sha256:" + hashlib.sha256(
            f"tier0-kill\0{peid}\0{tid}\0{reason}".encode("utf-8")
        ).hexdigest()
        ops = [
            {"op": "effect_add", "char": tid, "effect": "Slain", "kind": "condition",
             "valence": "negative"},
            {"op": "presence", "entity": tid, "present": False},
            {"op": "award_exp", "char": peid, "amount": int(xp) + bonus, "reason": reason},
            {"op": "fact_admit", "statement": statement,
             "cause": f"tier0-kill:{semantic_ref or cause_occurrence_ref}:{reason}",
             "authority": "rule"},
        ]
        if semantic_ref:
            for op in ops:
                op["_semantic_frame_ref"] = semantic_ref
        res.rule_ops.extend(ops)

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
# is PRIVILEGED: the pipeline validates it and re-sources it as a rule-owned operation.
# [clash] records an NPC-vs-NPC fight: prose resolves it, the LEDGER remembers it (no dice).
_FOE_TAG_RE = re.compile(
    r"\[\s*foe\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?"
    r"(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
_FOE_COHORT_RE = re.compile(
    r"^(?P<name>.+?)\s+[x×](?P<count>\d+)\s*$", re.IGNORECASE)
_FOE_MULTIPLIER_LIKE_RE = re.compile(
    r"^.+?\s+[x×]\s*[+-]?\d+(?:\.\d+)?\s*$", re.IGNORECASE)
# the symmetric ally channel (2026-07-10, Bean: "3v3 is missing") — the DM brings a present
# companion onto the player's side, same grammar/authority path as [foe].
_ALLY_TAG_RE = re.compile(
    r"\[\s*ally\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]+?)\s*)?(?:\|\s*([^|\[\]]+?)\s*)?\]",
    re.IGNORECASE)
# §F large-scale battle: the DM OPENS a battle ([battle | name | foe? | tier?]) and REPORTS the
# macro tide ([tide | winning|holding|losing | why]). Both are re-sourced as rule operations;
# the engine owns momentum + waves. Gated by [specialization].large_battle in the pipeline.
_BATTLE_TAG_RE = re.compile(
    r"\[\s*battle\s*\|\s*([^|\[\]]+?)\s*(?:\|\s*([^|\[\]]*?)\s*)?(?:\|\s*([^|\[\]]*?)\s*)?\]",
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
_NARRATOR_SCENE_PHASES = frozenset({"opening", "setup", "rising", "climax", "lull"})
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
            else:
                phase = re.sub(r"^phase\s*:?[ \t]*", "", seg, flags=re.IGNORECASE).lower()
                if phase in _NARRATOR_SCENE_PHASES:
                    op["phase"] = phase
        current_location = str((state.get("scene") or {}).get("location_id") or "")
        canonical_location_id, _, _ = canonical_location(state, loc)
        if "phase" not in op and canonical_location_id != current_location:
            # A real location boundary starts a new dramatic scene. Invalid time-of-day
            # values are intentionally ignored above, but must not leave a historical bad
            # phase (for example ``night``) attached to the newly admitted location.
            op["phase"] = "opening"
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
    `[foe | <name> | <tier?> | <armament?> | faction:<known faction>?]` stages the ENEMY
    side; `[ally | <name> | <tier?> | <armament?>]` (2026-07-10, Bean: "3v3 is missing")
    brings a present COMPANION
    onto the player's side — the symmetric channel the DM never had. Spawning is PRIVILEGED,
    so these are NOT extraction proposals — the caller (pipeline) applies them source='rule'
    after this validation: the narrator supplied an in-world combatant record, while the ENGINE
    validates the basis and mints the instance with curated
    HP. A name that matches a known entity spawns TRACKED (wounds persist); order/caps enforced
    by the reducer (each side is parser-capped at 3; the player holds one ally slot)."""
    ops: list[dict] = []
    peid, _ = _player_card(state)
    from .state import THREAT_TIERS, resolve_combatant, resolve_entity_ref
    for side, rx in (("enemy", _FOE_TAG_RE), ("ally", _ALLY_TAG_RE)):
        n = 0
        for m in rx.finditer(text or ""):
            name = m.group(1).strip()
            if side == "enemy" and _FOE_MULTIPLIER_LIKE_RE.fullmatch(name):
                # A terminal xN is count syntax only inside parse_combat_tags' exact new-battle
                # contract.  Everywhere else it fails closed instead of minting one literal
                # combatant named e.g. "Baser Hollow x6".
                continue
            if not name or _tag_char(name, state) == peid:
                continue                         # never spawn the Player as their own combatant
            live = resolve_combatant(state, name)    # already on the field by this name?
            if live and (((state.get("combat") or {}).get("combatants") or {})
                         .get(live) or {}).get("side") == side:
                continue                         # DM re-tagged a LIVE combatant -> no twin (2026-07-11)
            op: dict = {"op": "combatant_spawn", "name": name, "side": side}
            explicit_faction = None
            invalid_faction = False
            segments = [m.group(2), m.group(3)]
            if side == "enemy":
                segments.append(m.group(4))
            for index, seg in enumerate(segments):
                if not seg:
                    continue
                seg = seg.strip()
                faction_match = re.fullmatch(r"faction\s*:\s*(.+)", seg, re.IGNORECASE)
                if faction_match and side == "enemy":
                    if index != 2:
                        invalid_faction = True
                        continue
                    faction_ref = faction_match.group(1).strip()
                    if faction_ref.endswith("?"):
                        # The narrator explicitly marked this optional qualifier as uncertain.
                        # It grants no faction authority, but the independent literal hostile
                        # actor remains admissible. Active WorldOverlay policy may still reject
                        # the resulting factionless spawn at the reducer boundary.
                        continue
                    faction_id = resolve_entity_ref(state, faction_ref)
                    faction_row = (state.get("entities") or {}).get(faction_id or "")
                    if not faction_ref:
                        invalid_faction = True
                    elif not isinstance(faction_row, dict) \
                            or faction_row.get("kind") != "faction":
                        # A hallucinated qualifier has no authority to invent a faction, but it
                        # also cannot erase the independently grounded hostile actor.  Keep the
                        # foe factionless; WorldOverlay admission remains the final boundary.
                        continue
                    elif explicit_faction is not None:
                        invalid_faction = True
                    else:
                        explicit_faction = faction_id
                elif side == "enemy" and index == 2:
                    invalid_faction = True
                elif seg.lower() in THREAT_TIERS:
                    op["tier"] = seg.lower()
                elif len(seg) <= 60:
                    op["armament"] = re.sub(r"^(?:uses|wields|carries|armed with)\s+", "", seg,
                                            flags=re.IGNORECASE)
            eid = resolve_entity_ref(state, name)
            if eid and (state.get("entities", {}).get(eid) or {}).get("kind") \
                    in ("character", "npc"):
                op["char"] = eid                 # a KNOWN cast member fights as themselves
                if side == "enemy":
                    stored_faction = ((state.get("attributes") or {}).get(eid) or {}).get("faction")
                    stored_faction_id = resolve_entity_ref(state, stored_faction) \
                        if stored_faction else None
                    if stored_faction_id and ((state.get("entities") or {}).get(
                            stored_faction_id) or {}).get("kind") == "faction":
                        if explicit_faction is not None \
                                and explicit_faction != stored_faction_id:
                            invalid_faction = True
                        explicit_faction = stored_faction_id
            if invalid_faction:
                continue
            if explicit_faction is not None:
                op["faction"] = explicit_faction
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
        # The documented grammar is positional.  Field three is always the wave-foe label;
        # field four is a threat tier only when it is one of the curated values.  An invalid
        # tier such as "mob" is ignored and can never overwrite the correctly parsed foe.
        foe = (m.group(2) or "").strip()
        if foe and len(foe) <= 60:
            op["foe"] = foe
        threat = (m.group(3) or "").strip().lower()
        if threat in THREAT_TIERS:
            op["threat"] = threat
        ops.append(op)
        break                                    # one battle at a time
    for m in _TIDE_TAG_RE.finditer(text or ""):
        ops.append({"op": "tide_set", "tide": m.group(1).lower(),
                    "why": (m.group(2) or "").strip()[:80]})
    return ops


def parse_combat_tags(text: str, state: dict, *, allow_large_battle: bool = True) -> list[dict]:
    """Parse the narrator's privileged combat channels against one pre-reply snapshot.

    Ordinary ``[foe]``/``[ally]`` tags retain their existing behavior.  A single terminal
    ``xN`` enemy tag becomes a finite cohort only when the same reply opens exactly one NEW
    large battle and ``2 <= N <= BATTLE_COHORT_CAP``.  The cohort still consists solely of
    ordinary combatant spawns: at most three enemies can be live, and the remainder is queued
    for ``battle_ops``.  Invalid, ambiguous, active-battle, or standalone xN syntax contributes
    no enemy op; it is never reinterpreted as a literal actor name.
    """
    ordinary = parse_foe_tags(text, state)
    if not allow_large_battle:
        return ordinary

    battle = parse_battle_tags(text, state)
    multiplier_tags: list[re.Match] = []
    counted: list[tuple[re.Match, re.Match]] = []
    for tag in _FOE_TAG_RE.finditer(text or ""):
        if not _FOE_MULTIPLIER_LIKE_RE.fullmatch(tag.group(1).strip()):
            continue
        multiplier_tags.append(tag)
        count_match = _FOE_COHORT_RE.fullmatch(tag.group(1).strip())
        if count_match:
            counted.append((tag, count_match))
    if not multiplier_tags:
        return [*ordinary, *battle]

    starts = [op for op in battle if op.get("op") == "battle_start"]
    tides = [op for op in battle if op.get("op") == "tide_set"]
    battle_tags = list(_BATTLE_TAG_RE.finditer(text or ""))
    if len(multiplier_tags) != 1 or len(counted) != 1 or len(starts) != 1 \
            or len(battle_tags) != 1 \
            or bool((state.get("battle") or {}).get("active")):
        return [*ordinary, *tides]

    tag, count_match = counted[0]
    if tag.group(4) or any(
        re.fullmatch(r"faction\s*:\s*.+", (segment or "").strip(), re.IGNORECASE)
        for segment in (tag.group(2), tag.group(3))
    ):
        return [*ordinary, *tides]  # faction is not versioned into queued cohort waves
    base_name = count_match.group("name").strip()
    try:
        total = int(count_match.group("count"))
    except (TypeError, ValueError):
        return [*ordinary, *tides]
    if not base_name or len(base_name) > 60 or not 2 <= total <= BATTLE_COHORT_CAP:
        return [*ordinary, *tides]

    from .state import THREAT_TIERS, live_combatants

    tier = "standard"
    armament = ""
    for segment in (tag.group(2), tag.group(3)):
        value = (segment or "").strip()
        if not value:
            continue
        if value.lower() in THREAT_TIERS:
            tier = value.lower()
        elif len(value) <= 60:
            armament = re.sub(r"^(?:uses|wields|carries|armed with)\s+", "", value,
                                flags=re.IGNORECASE)

    cohort_ref = f"{(slug(base_name)[:48] or 'foe')}_x{total}"
    start = dict(starts[0])
    start["cohort"] = {
        "schema": "battle-cohort/1",
        "id": cohort_ref,
        "name": base_name,
        "total": total,
        "tier": tier,
        "armament": armament,
    }

    ordinary_enemy_count = sum(
        op.get("op") == "combatant_spawn" and op.get("side") == "enemy"
        for op in ordinary
    )
    live_enemy_count = len(live_combatants(state, "enemy"))
    initial = min(total, max(0, 3 - live_enemy_count - ordinary_enemy_count))
    cohort_spawns = []
    for index in range(1, initial + 1):
        spawn = {"op": "combatant_spawn", "name": base_name, "side": "enemy",
                 "tier": tier, "cohort_ref": cohort_ref, "cohort_index": index}
        if armament:
            spawn["armament"] = armament
        cohort_spawns.append(spawn)
    return [start, *ordinary, *cohort_spawns, *tides]


def parse_authorized_cohort_tags(
    source: bytes | str,
    state: dict,
    *,
    authority: object,
    expected_context: object,
):
    """Parse the strict finite-cohort grammar through its live out-of-band authority gate.

    ``parse_combat_tags`` intentionally remains grammar-only.  Privileged callers use this
    wrapper and pass the resulting typed declaration, sealed receipt, exact context, and source
    onward to ``state.apply_delta`` for a second admission check before any mutation.
    """
    from .semantic_ingress import (
        SemanticIngressContext,
        SemanticIngressError,
        parse_authorized_cohort_declaration,
    )

    if type(expected_context) is not SemanticIngressContext:
        raise SemanticIngressError("authorized cohort parsing requires typed ingress context")
    return parse_authorized_cohort_declaration(
        source,
        state,
        authority=authority,
        expected_context=expected_context,
    )


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
            rest = cmd[len("aether.check"):].strip()
            leading = len(m.group(1)) - len(m.group(1).lstrip())
            command_start = m.start(1) + leading
            rest_at = cmd.find(rest, len("aether.check")) if rest else -1
            source_offset = command_start + rest_at if rest_at >= 0 else None
            _parse_check(
                rest,
                res,
                source_offset=source_offset,
                command_span=(m.start(), m.end()),
            )
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


def _final_intent_applications(
    turn: SemanticTurn | None,
    semantic_snapshots: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze provisional choices against the final committed frame projection."""
    if turn is None:
        return []
    snapshots = {
        str(snapshot.get("frame_id") or ""): snapshot
        for snapshot in semantic_snapshots
        if isinstance(snapshot, Mapping) and snapshot.get("frame_id")
    }
    applications: list[dict[str, Any]] = []
    for preference in turn.interpretation_preferences:
        status = preference.get("status")
        reason = str(preference.get("reason") or "candidate_absent")
        frame_id = str(preference.get("frame_id") or "")
        snapshot = snapshots.get(frame_id)
        applied = status == "applied"
        if applied and (
            snapshot is None
            or not isinstance(snapshot.get("meaning_binding_ref"), str)
            or _CONTENT_FINGERPRINT_RE.fullmatch(snapshot["meaning_binding_ref"]) is None
            or not isinstance(snapshot.get("fingerprint"), str)
            or _CONTENT_FINGERPRINT_RE.fullmatch(snapshot["fingerprint"]) is None
        ):
            applied = False
            reason = "binding_failed"
        applications.append(
            {
                "lesson_id": preference["lesson_id"],
                "lesson_revision": preference["lesson_revision"],
                "lesson_fingerprint": preference["lesson_fingerprint"],
                "applied": applied,
                "reason": reason,
                "frame_id": frame_id or None,
                "selected_value": preference.get("selected_value") if applied else None,
                "meaning_binding_ref": (
                    snapshot.get("meaning_binding_ref") if applied and snapshot is not None else None
                ),
                "frame_fingerprint": (
                    snapshot.get("fingerprint") if applied and snapshot is not None else None
                ),
            }
        )
    return applications


def run(
    doc: dict,
    klass: str,
    duplicate: bool,
    state: dict,
    cfg,
    rng: Optional[random.Random] = None,
    turn: Optional[int] = None,
    opening_assessment: Optional[CombatOpeningAssessment] = None,
    recognition_overlay: Callable[[str], Mapping[str, Any]] | None = None,
    interpretation_overlay: Callable[[SemanticTurn], Mapping[str, Any]] | None = None,
) -> Tier0Result:
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
    # skill). Same-turn swipes are narration retries: Pipeline re-serves their already settled
    # mechanics before this pass, so Tier-0 never creates a second roll. Inert unless RPG.
    pending_foe = None
    semantic_frame: Optional[ActionFrame] = None
    semantic_snapshots: list[dict] = []
    semantic_bindings: dict[str, dict] = {}
    semantic_alignments: dict[str, list[dict]] = {}
    semantic_refs: dict[str, str] = {}
    semantic_ref = ""
    semantic_abstained = False
    semantic_world_effects_allowed = True
    precomputed_floor_spawn: Optional[dict] = None
    if rpg and last_user and is_new:
        action_user = _action_text(last_user)
        explicit_check_declared = any(
            match.group(1).strip().lower().startswith("aether.check")
            for match in OOC_RE.finditer(last_user)
        )
        res.semantic_turn = _new_semantic_turn(
            last_user,
            state,
            res,
            recognition_overlay=recognition_overlay,
            interpretation_overlay=interpretation_overlay,
        )
        semantic_abstained = res.semantic_turn.compiled_meaning is None
        brace_declared = _brace_phrase(last_user)
        if res.checks:
            _interpret_explicit_checks(
                last_user, state, cfg, res, dm_text=last_assistant,
                detection_text=action_user,
            )
        elif not brace_declared and not explicit_check_declared:
            _detect_nl_checks(
                last_user, state, cfg, res, dm_text=last_assistant,
                detection_text=action_user,
            )
        player_eid, _player = _player_card(state)
        if not semantic_abstained and player_eid \
                and not (state.get("combat") or {}).get("active") \
                and not any(
                    frame.action_class in ("kill_attempt", "grand_kill_attempt")
                    for frame in (res.semantic_turn.frames if res.semantic_turn else [])
                ):
            _append_kill_attempt_frame(
                res.semantic_turn, last_user, state, player_eid,
                dm_text=last_assistant, detection_text=action_user,
            )
        legacy_opening_target = None
        if getattr(cfg.specialization, "foe_floor", True) \
                and getattr(cfg.specialization, "war_room", True):
            if opening_assessment is None:
                opening_assessment = combat_opening_assessment(
                    doc, state, cfg, klass, duplicate=duplicate)
            legacy_opening_target = opening_assessment.target
        existing_frames = res.semantic_turn.frames if res.semantic_turn else []
        if not semantic_abstained and legacy_opening_target is not None \
                and (existing_frames or not res.checks) and not any(
                frame.action_class in ("weapon_attack", "grapple", "combat_opening")
                for frame in existing_frames) and not any(
                frame.ambiguity for frame in existing_frames):
            player_eid, _player = _player_card(state)
            if player_eid:
                _append_combat_transition_frame(
                    res.semantic_turn, last_user, player_eid, opening_assessment
                )
        _scope_semantic_occurrence(
            res,
            int(turn if turn is not None else (state.get("meta") or {}).get("turn", -1) + 1),
        )
        semantic_frames = [
            frame for frame in (res.semantic_turn.frames if res.semantic_turn else [])
            if frame.capability_id or frame.ambiguity
        ]
        meaning = res.semantic_turn.compiled_meaning if res.semantic_turn else None
        entity_aware = bool(getattr(cfg.specialization, "intent_floor", True))
        target_probe = next(
            (
                frame for frame in semantic_frames
                if _can_seek_world_target(
                    frame, last_user, meaning, action_user,
                )
                and not frame.target_entity_id
            ),
            None,
        )
        if target_probe is not None \
                and getattr(cfg.specialization, "foe_floor", True) \
                and getattr(cfg.specialization, "war_room", True) \
                and not (state.get("combat") or {}).get("active"):
            bounded_action = (
                " " * target_probe.start
                + action_user[target_probe.start:target_probe.end]
                + " " * (len(action_user) - target_probe.end)
            )
            precomputed_floor_spawn = _floor_stage_foe(
                res,
                state,
                bounded_action,
                last_assistant,
                entity_aware=entity_aware,
                semantic_frame_id=target_probe.frame_id,
            )
            if precomputed_floor_spawn is not None:
                target_id = str(
                    precomputed_floor_spawn.get("char")
                    or precomputed_floor_spawn.get("_cid")
                    or ""
                )
                if target_id:
                    # The exact frame-span probe plus narrator grounding has now supplied the
                    # missing world target.  Retire only the graph placeholder; any independent
                    # ambiguity still prevents admission below.
                    target_probe.ambiguity = [
                        reason for reason in target_probe.ambiguity
                        if reason != "occurrence.target_unbound"
                    ]
                    target_probe.target_entity_id = target_id
                    target_probe.target_name = str(
                        precomputed_floor_spawn.get("name") or target_id
                    )
                    _add_grounded_target_evidence(
                        target_probe,
                        bounded_action,
                        target_probe.target_name,
                        target_id,
                    )
        if meaning is not None:
            for frame in semantic_frames:
                try:
                    binding, alignments = _attach_meaning_binding(
                        frame,
                        last_user,
                        state,
                        meaning,
                        detection_text=action_user,
                    )
                except (SemanticBindingError, ValueError) as exc:
                    frame.meaning_binding_ref = ""
                    frame.event_node_id = ""
                    frame.mechanic_disposition = ""
                    frame.world_alignment_refs = ()
                    res.notices.append(
                        "semantic binding unavailable; this action abstained "
                        f"({type(exc).__name__})"
                    )
                    continue
                semantic_bindings[frame.frame_id] = binding
                semantic_alignments[frame.frame_id] = alignments
        frames_by_id = {frame.frame_id: frame for frame in semantic_frames}
        if semantic_frames:
            semantic_world_effects_allowed = any(
                frame.mechanically_actionable for frame in semantic_frames
            )
        for check in res.checks:
            check_frame = frames_by_id.get(str(check.get("_semantic_frame_id") or ""))
            if check_frame is not None and check_frame.mechanically_actionable:
                check.pop("_semantic_abstain", None)
            else:
                check["_semantic_abstain"] = True
                check["_semantic_disposition"] = (
                    check_frame.mechanic_disposition if check_frame is not None
                    else "unavailable"
                )
        attack_frames = [
            frame for frame in semantic_frames
            if frame.mechanically_actionable
            and frame.action_class in ("weapon_attack", "grapple", "combat_opening")
        ]
        semantic_frame = attack_frames[0] if attack_frames else _primary_action_frame(
            res.semantic_turn
        )
        claim_frames = list(res.semantic_turn.claim_frames if res.semantic_turn else ())
        if (semantic_frames or claim_frames) and meaning is not None:
            res.rule_ops.append({
                "op": "semantic_meaning_commit",
                "meaning": meaning.receipt_dict(),
            })
        for claim_frame in claim_frames:
            res.rule_ops.append({"op": "claim_record", "frame": claim_frame})
        for frame in semantic_frames:
            binding = semantic_bindings.get(frame.frame_id)
            if binding is not None:
                res.rule_ops.append({"op": "semantic_binding_commit", "binding": binding})
        for frame in semantic_frames:
            for alignment in semantic_alignments.get(frame.frame_id, ()):
                res.rule_ops.append({
                    "op": "semantic_world_alignment_commit",
                    "alignment": alignment,
                })
        for frame in semantic_frames:
            snapshot = frame.snapshot(last_user)
            semantic_snapshots.append(snapshot)
            semantic_refs[frame.frame_id] = str(snapshot["fingerprint"])
            res.rule_ops.append({"op": "semantic_frame_commit", "frame": snapshot})
        res.intent_applications = _final_intent_applications(
            res.semantic_turn,
            semantic_snapshots,
        )
        if semantic_frame is not None and semantic_frame.mechanically_actionable:
            semantic_ref = semantic_refs.get(semantic_frame.frame_id, "")
        for check in res.checks:
            ref = semantic_refs.get(str(check.get("_semantic_frame_id") or ""))
            if ref:
                check["_semantic_frame_ref"] = ref
        # A deliberate combat transition against an exact present NPC is code-owned before the
        # narrator speaks.  This closes the old first-foe causality gap: combatant_spawn freezes
        # the grounded kit and its first pending intent in the same hot-path rule batch, so the
        # first narrator packet can translate a real tell instead of inventing one.  Explicit
        # aether.foe commands remain owned by _commands above and therefore return no target here.
        opening_target = None
        if getattr(cfg.specialization, "foe_floor", True) \
                and getattr(cfg.specialization, "war_room", True) \
                and not (state.get("combat") or {}).get("active") \
                and precomputed_floor_spawn is None:
            if semantic_frame is not None:
                if semantic_frame.mechanically_actionable \
                        and semantic_frame.action_class \
                        in ("weapon_attack", "grapple", "combat_opening") \
                        and (semantic_frame.target_entity_id or semantic_frame.target_name):
                    opening_target = (
                        semantic_frame.target_entity_id,
                        str(semantic_frame.target_name or semantic_frame.target_entity_id),
                    )
            elif not semantic_frames and not res.checks:
                opening_target = legacy_opening_target
        if precomputed_floor_spawn is not None:
            spawn = dict(precomputed_floor_spawn)
            if semantic_ref:
                spawn["_semantic_frame_ref"] = semantic_ref
            res.rule_ops.append(spawn)
            band = _floor_group_extras(state, last_assistant, spawn)
            if semantic_ref:
                for extra in band:
                    extra["_semantic_frame_ref"] = semantic_ref
            res.rule_ops.extend(band)
            pending_foe = {"cid": spawn["_cid"], "name": spawn["name"]}
            res.notices.append(
                f"combat floor: staged '{spawn['name']}' as a foe"
                + (f" (+{len(band)} more of the band)" if band else "")
                + " — the canonical frame retained the narrator-grounded target"
            )
            if str((state.get("scene") or {}).get("phase", "")).lower() not in _COMBAT_PHASES:
                scene_open = {"op": "scene_set", "phase": "climax", "_floor": True}
                if semantic_ref:
                    scene_open["_semantic_frame_ref"] = semantic_ref
                res.rule_ops.append(scene_open)
        elif opening_target is not None:
            eid, name = opening_target
            base = slug(name)[:32] or "foe"
            rows = (state.get("combat") or {}).get("combatants") or {}
            cid, n = base, 2
            while cid in rows:
                cid, n = f"{base}#{n}", n + 1
            spawn = {"op": "combatant_spawn", "name": name, "side": "enemy",
                     "tier": "standard", "_cid": cid}
            if eid:
                spawn["char"] = eid
                spawn["_intro_intent_visible"] = "known-opponent-opening/1"
            else:
                spawn["_floor"] = True
            if semantic_ref:
                spawn["_semantic_frame_ref"] = semantic_ref
            res.rule_ops.append(spawn)
            pending_foe = {"cid": cid, "name": name}
            res.notices.append(
                f"combat opening: staged known on-scene opponent '{name}' before narration; "
                "its first code-selected intent is visible in this reply")
            if str((state.get("scene") or {}).get("phase", "")).lower() \
                    not in _COMBAT_PHASES:
                scene_open = {"op": "scene_set", "phase": "climax", "_floor": True}
                if semantic_ref:
                    scene_open["_semantic_frame_ref"] = semantic_ref
                res.rule_ops.append(scene_open)
        if res.checks:
            offensive_intent = (
                semantic_frame.action_class in ("weapon_attack", "grapple")
                if semantic_frame is not None
                else _has_attack_intent(action_user)
                or any(bool(c.get("_attack")) for c in res.checks)
            )
            if pending_foe is None \
                    and getattr(cfg.specialization, "foe_floor", True) \
                    and getattr(cfg.specialization, "war_room", True) \
                    and not (state.get("combat") or {}).get("active") \
                    and semantic_frame is None:
                sp = _floor_stage_foe(res, state, action_user, last_assistant,
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
                        scene_open = {"op": "scene_set", "phase": "climax", "_floor": True}
                        if semantic_ref:
                            scene_open["_semantic_frame_ref"] = semantic_ref
                        res.rule_ops.append(scene_open)
            if pending_foe is not None and offensive_intent:
                for c in res.checks:
                    target = str(c.get("target") or "")
                    if semantic_frame is not None \
                            and c.get("_semantic_frame_id") == semantic_frame.frame_id:
                        c["target"] = pending_foe["cid"]
                    elif not target or slug(target)[:32] == pending_foe["cid"] \
                            or _norm_phrase(target) == _norm_phrase(pending_foe["name"]):
                        c["target"] = pending_foe["cid"]
            if _war_room(state, cfg):        # Phase 1: bind strikes to live enemy rows
                if semantic_frame is not None and semantic_frame.target_entity_id:
                    rows = (state.get("combat") or {}).get("combatants") or {}
                    target_cid = next(
                        (cid for cid, row in rows.items() if isinstance(row, dict)
                         and (cid == semantic_frame.target_entity_id
                              or row.get("eid") == semantic_frame.target_entity_id)
                         and not row.get("defeated")),
                        None,
                    )
                    for check in res.checks:
                        if check.get("_semantic_frame_id") == semantic_frame.frame_id:
                            check["target"] = target_cid
                else:
                    _bind_targets(res, state, action_user, entity_aware=entity_aware,
                                  offensive_intent=offensive_intent)
            _resolve_checks(res, state, cfg, rng, pending_foe=pending_foe,
                            offensive_intent=offensive_intent)
        _group_weapon_attack_settlements(
            res,
            semantic_snapshots,
            semantic_bindings,
        )
        _group_combat_opening_settlements(
            res,
            semantic_snapshots,
            semantic_bindings,
        )
        if res.checks:
            _group_skill_check_settlements(
                res,
                semantic_snapshots,
                semantic_bindings,
            )
        if getattr(cfg.specialization, "stealth_kills", True):
            kill_frame = next(
                (
                    frame for frame in semantic_frames
                    if frame.action_class in ("kill_attempt", "grand_kill_attempt")
                ),
                None,
            )
            kill_ref = semantic_refs.get(kill_frame.frame_id, "") if kill_frame else ""
            _kill_intent(
                res, state, cfg, action_user, frame=kill_frame, semantic_ref=kill_ref
            )
        if pending_foe is not None and not any(
                o.get("op") == "check" for o in res.rule_ops) and not res.kill_note:
            res.turn_guidance = "combat_opening"
        elif not any(o.get("op") == "check" for o in res.rule_ops) and not res.kill_note:
            # Always give the newest RPG message a fresh mechanical disposition. This prevents
            # a cached/earlier [DIRECTIVE] from becoming ambiguous. An unresolved action remains
            # visible as a perception gap; the narrator never gains authority to create a roll.
            res.turn_guidance = _fresh_turn_guidance(last_user)
        pending_enemy = bool(pending_foe) or any(
            op.get("op") == "combatant_spawn" and op.get("side", "enemy") == "enemy"
            for op in res.user_ops + res.rule_ops)
        opposition = _opposition_op(
            state, cfg,
            turn=int(turn if turn is not None else (state.get("meta") or {}).get("turn", -1) + 1),
            pending_enemy=pending_enemy,
            reaction="brace" if brace_declared else "",
        )
        if opposition is not None:
            # This resolves the prior pending enemy intent, not the current Player occurrence.
            # Until enemy actions own a separately frozen semantic frame, they remain unframed.
            res.rule_ops.append(opposition)
        if brace_declared:
            applied = isinstance((opposition or {}).get("_opposition"), dict) \
                and bool(opposition["_opposition"].get("reaction", {}).get("applied"))
            if applied:
                res.turn_guidance = ""
                res.notices.append(
                    "Brace committed: the whole action halves this direct-contact enemy damage")
            else:
                res.turn_guidance = ""
                res.notices.append(
                    "Brace was declared, but the committed enemy move is not braceable")

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
    if is_new and klass != "impersonate" and not semantic_abstained:
        res.rule_ops.append({"op": "clock_tick", "minutes": cfg.director.minutes_per_turn})

    # R3 — conservative time keywords (which trigger R4 craving ramp inside the reducer)
    if is_new and last_user and not semantic_abstained:
        world_user = _action_text(last_user) if semantic_world_effects_allowed else ""
        low = OOC_RE.sub("", world_user).lower()
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
        world_user = _action_text(last_user) if semantic_world_effects_allowed else ""
        scan = f"{world_user}\n{last_assistant}".lower()
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
