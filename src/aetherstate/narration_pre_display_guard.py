"""Deterministic narration-compliance diagnostics for authoritative RPG turns.

The normal narrator remains free to write rich prose.  This module checks only a narrow class of
claims that code has already settled: roll polarity and harm/defeat of exact War Room rows.  A
current V3 mechanic turn is streamed unchanged; this evaluator runs on the cold path afterward and
records bounded reason codes without delaying or replacing narration.  Turns without current
mechanic authority remain outside this module.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from . import linter
from .narration_fallback_runtime import EMPTY_FALLBACK_TEXT
from .narration_plan_runtime import (
    build_default_narration_plan_selection,
    build_narration_realization_plan,
    render_narration_plan_selection,
)
from .narrator_realization import (
    build_narrator_realization_from_state,
    validate_narrator_realization,
)
from .semantic_truth_runtime import build_runtime_truth_contract
from .state import CombatReferenceStatus, combatant_label, resolve_combat_reference
from .turn_lifecycle import fingerprint


NARRATION_PRE_DISPLAY_GUARD_SCHEMA = "narration-pre-display-guard/1"

_QUOTE_RE = re.compile(r'"(?:\\.|[^"\\])*"|“[^”]*”', re.DOTALL)
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?;\n]")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+|\n+")
_NEUTRAL_TARGET_PRONOUN_RE = re.compile(r"\b(?:it|them)\b", re.IGNORECASE)
_ARTICLE_LED_NOMINAL_RE = re.compile(
    r"\b(?:a|an|the|this|that|another|each|every)\s+[a-z][a-z0-9'\u2019-]*",
    re.IGNORECASE,
)
_ATTEMPT_PREFIX_RE = re.compile(
    r"(?:\b(?:tries?|tried|attempts?|attempted|aims?|aimed|threatens?|threatened|"
    r"fails?|failed)\s+(?:to\s+)?|\b(?:do|does|did)\s+not\s+|"
    r"\b(?:don't|doesn't|didn't|cannot|can't|never)\s+|"
    r"\b(?:would|could|might|may)(?:\s+have)?\s+|\b(?:almost|nearly)\s+)$",
    re.IGNORECASE,
)
_NONCONTACT_BRIDGE_RE = re.compile(
    r"\b(?:at|toward|towards|for|past|near|beside|around|above|below|before|behind|"
    r"short of|without touching|without reaching)\b",
    re.IGNORECASE,
)
_NONCONTACT_OBJECT_RE = re.compile(
    r"\b(?:air|ground|floor|wall|space|nothing|shadow)\b",
    re.IGNORECASE,
)
_EVENT_BOUNDARY_RE = re.compile(
    r",\s*(?:and|but|while|whereas)\b|\b(?:while|whereas)\b",
    re.IGNORECASE,
)
_HARM_VERB_RE = re.compile(
    r"\b(?:hits?|strikes?|struck|cuts?|cutting|slashes?|slashed|stabs?|stabbed|"
    r"slic(?:e|es|ed|ing)|cleav(?:e|es|ed|ing)|"
    r"pierces?|pierced|impales?|impaled|skewers?|skewered|punctures?|punctured|"
    r"wounds?|wounded|injures?|injured|damages?|damaged|burns?|burned|scorches?|"
    r"scorched|freezes?|froze|frozen|shatters?|shattered|crushes?|crushed|blasts?|"
    r"blasted|mauls?|mauled|bites?|bit|tears?|tore|gouges?|gouged|gores?|gored|"
    r"punches?|punched|drives?|drove|buries?|buried|pins?|pinned|restrains?|"
    r"restrained|immobili[sz]es?|immobili[sz]ed|encases?|encased|entombs?|entombed|"
    r"dismembers?|dismembered)\b",
    re.IGNORECASE,
)
_ATTRIBUTIVE_INJURY_RE = re.compile(
    r"(?:^|[,(]\s*|\b(?:the|a|an|this|that|each|every|another)\s+)"
    r"(?:(?:already|still|visibly|badly|gravely|severely|lightly|previously)\s+)*"
    r"(?:wounded|injured|damaged|burned|scorched|frozen)\s*$",
    re.IGNORECASE,
)
_STATIVE_INJURY_RE = re.compile(
    r"^\s*(?:(?:is|was|remains?|looks?|seems?)\s+|,\s*)"
    r"(?:(?:already|still|visibly|badly|gravely|severely|lightly|previously)\s+)*"
    r"(?:wounded|injured|damaged|bleeding|burned|scorched|frozen)\b",
    re.IGNORECASE,
)
_CAUSAL_COMPLEMENT_RE = re.compile(
    r"^\s*(?:,\s*)?(?:from|by|because\s+of|due\s+to)\s+"
    r"(?P<cause>[^.!?;\n]{1,96})",
    re.IGNORECASE,
)
_HISTORICAL_CAUSE_RE = re.compile(
    r"\b(?:prior|previous(?:ly)?|earlier|formerly|old|past|"
    r"yesterday(?:['\u2019]s)?|last\s+(?:turn|round|battle|attack)|long\s+ago|"
    r"before\s+(?:this|the\s+current)\s+(?:turn|round|battle|attack))\b",
    re.IGNORECASE,
)
_FRESH_CAUSE_MARKER_RE = re.compile(
    r"\b(?:new|current|fresh|this|that|just[- ]delivered|just[- ]landed)\b",
    re.IGNORECASE,
)
_IMPACT_CAUSE_RE = re.compile(
    r"\b(?:hit|blow|strike|attack|impact|projectile|spike|bolt|shot|slash|cut|"
    r"thrust|wound)\b",
    re.IGNORECASE,
)
_DEFINITE_IMPACT_CAUSE_RE = re.compile(
    r"^\s*(?:the|this|that)\s+(?:(?:new|current|fresh|ice|fire|stone|acid|"
    r"lightning)\s+){0,3}(?:hit|blow|strike|attack|impact|projectile|spike|bolt|"
    r"shot|slash|cut|thrust)\b",
    re.IGNORECASE,
)
_HISTORICAL_EVENT_SUFFIX_RE = re.compile(
    r"^\s*(?:,\s*)?(?:(?:during|in|after|from)\s+(?:the\s+)?)?"
    r"(?:prior|previous|earlier|past|last)\b|"
    r"^\s*(?:,\s*)?(?:yesterday|long\s+ago|before\s+this\s+turn)\b",
    re.IGNORECASE,
)
_PASSIVE_HARM_RE = re.compile(
    r"^\s*(?:,\s*)?(?:is|was|gets?|got|has been|had been)\s+"
    r"(?:hit|struck|cut|slashed|sliced|cleaved|stabbed|pierced|impaled|skewered|"
    r"punctured|wounded|"
    r"injured|damaged|burned|scorched|frozen|shattered|crushed|blasted|mauled|bitten|"
    r"torn|gouged|gored|punched|pinned|restrained|immobili[sz]ed|encased|entombed|"
    r"dismembered)\b",
    re.IGNORECASE,
)
_RESULT_HARM_RE = re.compile(
    r"^\s*(?:,\s*)?(?:bleeds?|is bleeding|crumples? from (?:the|that) (?:hit|blow)|"
    r"collapses? from (?:the|that) (?:hit|blow)|screams? in pain|clutches? (?:at )?"
    r"(?:a|the|its|their|his|her) wound)\b",
    re.IGNORECASE,
)
_STATIVE_RESULT_HARM_RE = re.compile(
    r"^\s*(?:,\s*)?(?:bleeds?|is bleeding|screams? in pain|clutches? (?:at )?"
    r"(?:a|the|its|their|his|her) wound)\b",
    re.IGNORECASE,
)
_DOWNSTREAM_SELF_HARM_RE = re.compile(
    r"^\s*(?:,\s*)?(?:"
    r"(?:staggers?|reels?|flinches?|jerks?|buckles?|convulses?)\s*,\s*"
    r"(?:wounded|injured|bleeding|burned|scorched|frozen)\b|"
    r"(?:staggers?|reels?|flinches?|jerks?|buckles?|convulses?)\b"
    r"[^.!?;\n]{0,100}\b(?:pierces?|pierced|impales?|impaled|skewers?|skewered|"
    r"punctures?|punctured|cuts?|slashed|stabs?|stabbed|wounds?|wounded|injures?|"
    r"injured|burns?|burned|scorches?|scorched|freezes?|froze|frozen|shatters?|"
    r"shattered|crushes?|crushed|tears?|tore|gouges?|gouged|gores?|gored)\s+"
    r"(?:its|their|his|her)\s+(?:body|flesh|skin|arm|leg|torso|chest|skull|bone|"
    r"bones|shell)\b)",
    re.IGNORECASE,
)
_POSSESSIVE_HARM_RE = re.compile(
    r"^\s*['\u2019]s\s+(?:body|flesh|skin|arm|leg|torso|chest|skull|bone|bones|shell)"
    r"(?![^.!?;\n]{0,45}\b(?:not|never)\b)[^.!?;\n]{0,45}\b(?:bleeds?|splits?|"
    r"breaks?|shatters?|tears?|burns?|freezes?|is bleeding|hit|struck|cut|slashed|"
    r"stabbed|pierced|impaled|skewered|punctured|wounded|injured|damaged|burned|"
    r"scorched|frozen|crushed|blasted|mauled|bitten|torn|gouged|gored)\b",
    re.IGNORECASE,
)
_DEATH_BEFORE_RE = re.compile(
    r"\b(?:kills?|killed|slays?|slew)\b",
    re.IGNORECASE,
)
_LEAVE_BEFORE_RE = re.compile(r"\bleaves?\s*$", re.IGNORECASE)
_LEAVE_DEATH_COMPLEMENT_RE = re.compile(
    r"^\s*(?:dead|lifeless|slain|a corpse|without breath|not breathing)\b",
    re.IGNORECASE,
)
_PASSIVE_DEATH_RE = re.compile(
    r"^\s*(?:,\s*)?(?:dies|died|is dead|was dead|falls? dead|fell dead|drops? dead|"
    r"is slain|was slain|is killed|was killed|lies? lifeless|becomes? a corpse|"
    r"breathes? (?:its|their|his|her) last|stops? breathing|"
    r"perish(?:es|ed)?|expire(?:s|d)?|succumb(?:s|ed)?|"
    r"(?:drops?|dropped)\s*,\s*(?:utterly\s+)?unmoving|"
    r"(?:falls?|fell)\s*,\s*(?:utterly\s+)?motionless)\b",
    re.IGNORECASE,
)
_PRONOUN_DEATH_RE = re.compile(
    r"^\s*(?:,\s*)?(?:and|then)\s+(?:kills?|slays?)\s+(?:it|them|him|her)\b",
    re.IGNORECASE,
)
_PASSIVE_HARM_THEN_DEATH_RE = re.compile(
    r"^\s*(?:,\s*)?(?:is|was|gets?|got|has been|had been)\s+"
    r"(?:hit|struck|cut|slashed|stabbed|pierced|impaled|skewered|punctured|wounded|"
    r"injured|damaged|burned|scorched|frozen|shattered|crushed|blasted|mauled|bitten|"
    r"torn|gouged|gored|punched|pinned|dismembered)\b[^.!?;\n]{0,70}"
    r"\b(?:and|then)\s+(?:(?:it|they|he|she)\s+)?(?:dies|died|falls? dead|fell dead|"
    r"drops? dead|dropped dead|lies? lifeless|stops? breathing)\b",
    re.IGNORECASE,
)

_NEGATED_FAILURE_ASSERTION_RE = re.compile(
    r"\b(?:(?:do|does|did|has|have|had|is|are|was|were|will|would|could|should)\s+not|"
    r"never|cannot|(?:do|does|did|has|have|had|is|are|was|were|could|would|should)"
    r"n['\u2019]t|can['\u2019]t|won['\u2019]t)\s+(?:(?:[a-z]+ly|ever)\s+){0,2}"
    r"(?:fail(?:s|ed)?|fall(?:s)?\s+short|be\s+unable)\b|"
    r"\b(?:not\s+(?:a\s+)?|no\s+)failure\b",
    re.IGNORECASE,
)
_NEGATED_SUCCESS_ASSERTION_RE = re.compile(
    r"\b(?:(?:do|does|did|has|have|had|is|are|was|were|will|would|could|should)\s+not|"
    r"never|cannot|(?:do|does|did|has|have|had|is|are|was|were|could|would|should)"
    r"n['\u2019]t|can['\u2019]t|won['\u2019]t)\s+(?:(?:[a-z]+ly|ever)\s+){0,2}"
    r"(?:succeed(?:s|ed)?|manage(?:s|d)?\s+to|pull(?:s|ed)?\s+it\s+off)\b|"
    r"\b(?:not\s+(?:a\s+)?|no\s+)success\b|"
    r"\bnot\s+successful(?:ly)?\b",
    re.IGNORECASE,
)

_REFERENCE_TOKEN_RE = re.compile(r"#[1-9][0-9]*|[a-z0-9]+(?:[-'][a-z0-9]+)*", re.IGNORECASE)


@dataclass(frozen=True)
class NarrationGuardBasis:
    """Frozen request-time authority needed to judge one completed upstream candidate."""

    schema: str
    turn_index: int
    state_fingerprint: str
    realization: dict[str, Any]
    fallback_story: str


@dataclass(frozen=True)
class NarrationGuardDecision:
    """One advisory story-level verdict with no delivery or replacement authority."""

    accepted: bool
    story: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    entity_ids: frozenset[str]


def _durable_state_projection(state: object) -> dict[str, Any]:
    """Drop prompt-only top-level helpers before binding request and response state."""
    if not isinstance(state, Mapping):
        return {}
    return {
        str(key): deepcopy(value)
        for key, value in state.items()
        if isinstance(key, str) and not key.startswith("_")
    }


def narration_guard_state_fingerprint(state: object) -> str:
    """Stable fingerprint shared by request-time preparation and exact-turn reconstruction."""
    return fingerprint(_durable_state_projection(state))


def _mechanically_authoritative(realization: Mapping[str, Any]) -> bool:
    return bool(realization.get("asserted_settled") or realization.get("asserted_unresolved"))


def _fallback_story(
    state: Mapping[str, Any],
    *,
    branch_id: str,
    turn_index: int,
    journal_rows: Sequence[Mapping[str, Any]],
) -> str:
    """Build the existing finite narration plan, failing closed to its empty safe sentence."""
    try:
        state_fp = narration_guard_state_fingerprint(state)
        contract = build_runtime_truth_contract(
            state,
            branch_id=branch_id,
            post_ledger_hash=state_fp,
            turn_index=turn_index,
            journal_rows=journal_rows,
        )
        plan = build_narration_realization_plan(contract)
        selection = build_default_narration_plan_selection(plan)
        story = render_narration_plan_selection(plan, selection).text
        return story if isinstance(story, str) and story.strip() else EMPTY_FALLBACK_TEXT
    except Exception:
        return EMPTY_FALLBACK_TEXT


def build_narration_guard_basis(
    state: object,
    *,
    branch_id: str,
    turn_index: int,
    journal_rows: Sequence[Mapping[str, Any]] = (),
) -> NarrationGuardBasis | None:
    """Prepare a guard only when this exact turn has current mechanical narrator authority."""
    if not isinstance(state, Mapping) or isinstance(turn_index, bool) \
            or not isinstance(turn_index, int) or turn_index < 0:
        return None
    realization = build_narrator_realization_from_state(state)
    if realization is None or not _mechanically_authoritative(realization):
        return None
    try:
        valid = validate_narrator_realization(realization)
    except (TypeError, ValueError):
        return None
    mechanic_turn = int(valid["turn"])
    if mechanic_turn != turn_index:
        retry = state.get("_settled_retry")
        if not isinstance(retry, Mapping) or retry.get("source_turn") != mechanic_turn:
            return None
    return NarrationGuardBasis(
        schema=NARRATION_PRE_DISPLAY_GUARD_SCHEMA,
        turn_index=mechanic_turn,
        state_fingerprint=narration_guard_state_fingerprint(state),
        realization=deepcopy(valid),
        fallback_story=_fallback_story(
            state,
            branch_id=str(branch_id),
            turn_index=mechanic_turn,
            journal_rows=journal_rows,
        ),
    )


def _unquoted(text: str) -> str:
    return _QUOTE_RE.sub(lambda match: " " * (match.end() - match.start()), text)


def _combatant_mentions(state: Mapping[str, Any], text: str) -> tuple[_Mention, ...]:
    rows = ((state.get("combat") or {}).get("combatants") or {})
    if not isinstance(rows, Mapping):
        return ()
    labels: dict[str, set[str]] = {}
    for raw_id, raw_row in rows.items():
        if not isinstance(raw_row, Mapping):
            continue
        entity_id = str(raw_row.get("id") or raw_id).strip()
        name = str(raw_row.get("name") or "").strip()
        visible = combatant_label(dict(raw_row), entity_id).strip()
        if not entity_id or not name or not visible:
            continue
        labels.setdefault(visible.casefold(), set()).add(entity_id)
        labels.setdefault(name.casefold(), set()).add(entity_id)

    candidates: list[tuple[int, int, frozenset[str]]] = []
    low = text.casefold()
    for label, entity_ids in labels.items():
        pattern = re.compile(r"(?<!\w)" + re.escape(label) + r"(?!\w)", re.IGNORECASE)
        candidates.extend(
            (match.start(), match.end(), frozenset(entity_ids))
            for match in pattern.finditer(low)
        )
    # Reuse the state-owned structured reference boundary for cohort ordinal surfaces.  This keeps
    # the guard aligned with mechanics for forms such as ``twenty first``, ``21st``, and ``21``
    # without maintaining a second ordinal grammar here.
    tokens = list(_REFERENCE_TOKEN_RE.finditer(text))
    reference_state = dict(state)
    for left in range(len(tokens)):
        for right in range(left + 1, min(len(tokens), left + 6) + 1):
            start, end = tokens[left].start(), tokens[right - 1].end()
            result = resolve_combat_reference(reference_state, text[start:end])
            if result.status not in {
                CombatReferenceStatus.RESOLVED,
                CombatReferenceStatus.AMBIGUOUS,
                CombatReferenceStatus.DEFEATED,
            }:
                continue
            entity_ids = frozenset(
                candidate.combatant_id
                for candidate in result.candidates
                if candidate.combatant_id is not None
            )
            if entity_ids:
                candidates.append((start, end, entity_ids))
    # A cohort base overlaps its exact numbered label.  Keep the longest mention at each span so
    # ``Hollowed #2`` never also becomes the ambiguous bare group ``Hollowed``.
    candidates.sort(key=lambda row: (row[0], -(row[1] - row[0]), row[1]))
    selected: list[_Mention] = []
    for start, end, entity_ids in candidates:
        if any(start < row.end and end > row.start for row in selected):
            continue
        selected.append(_Mention(start, end, entity_ids))
    return tuple(sorted(selected, key=lambda row: (row.start, row.end)))


def _clause(text: str, mention: _Mention) -> tuple[str, str]:
    left = 0
    for boundary in _CLAUSE_BOUNDARY_RE.finditer(text, 0, mention.start):
        left = boundary.end()
    next_boundary = _CLAUSE_BOUNDARY_RE.search(text, mention.end)
    right = next_boundary.start() if next_boundary else len(text)
    return text[left:mention.start], text[mention.end:right]


def _fresh_causal_complement(text: str) -> bool:
    """Return true only for an explicit, current impact cause after a harm state."""
    match = _CAUSAL_COMPLEMENT_RE.search(text)
    if match is None:
        return False
    cause = match.group("cause")
    if _HISTORICAL_CAUSE_RE.search(cause) or not _IMPACT_CAUSE_RE.search(cause):
        return False
    return bool(
        _FRESH_CAUSE_MARKER_RE.search(cause)
        or _DEFINITE_IMPACT_CAUSE_RE.search(cause)
    )


def _direct_harm_before(prefix: str) -> bool:
    tail = prefix[-140:]
    matches = list(_HARM_VERB_RE.finditer(tail))
    attributive = _ATTRIBUTIVE_INJURY_RE.search(tail)
    for verb in reversed(matches):
        # ``the wounded Hollowed`` is an existing-state adjective, not a hidden event verb.
        # If an earlier real verb exists (``slashes the wounded Hollowed``), continue back to it.
        if attributive is not None and verb.start() >= attributive.start():
            continue
        lead = tail[max(0, verb.start() - 28):verb.start()]
        bridge = tail[verb.end():]
        if _ATTEMPT_PREFIX_RE.search(lead.rstrip() + " "):
            continue
        if len(bridge) > 95 or _NONCONTACT_BRIDGE_RE.search(bridge) \
                or _NONCONTACT_OBJECT_RE.search(bridge) \
                or _EVENT_BOUNDARY_RE.search(bridge):
            continue
        return True
    return False


def _harm_claim_kind(prefix: str, suffix: str) -> str:
    """Classify a mention as a fresh change, a durable injury state, or no harm claim."""
    if _direct_harm_before(prefix):
        if _HISTORICAL_EVENT_SUFFIX_RE.search(suffix[:80]):
            return ""
        return "change"
    attributive = _ATTRIBUTIVE_INJURY_RE.search(prefix[-140:])
    stative = _STATIVE_INJURY_RE.search(suffix)
    if stative is not None:
        # A current impact complement owns a new event.  Bare or explicitly historical injury
        # remains a state assertion that prior missing HP may truthfully support.
        if _fresh_causal_complement(suffix[stative.end():stative.end() + 120]):
            return "change"
        return "state"
    result_state = _STATIVE_RESULT_HARM_RE.search(suffix)
    if result_state is not None:
        if _fresh_causal_complement(suffix[result_state.end():result_state.end() + 120]):
            return "change"
        return "state"
    if attributive is not None:
        return "state"
    if _PASSIVE_HARM_RE.search(suffix) or _RESULT_HARM_RE.search(suffix) \
            or _DOWNSTREAM_SELF_HARM_RE.search(suffix) \
            or _POSSESSIVE_HARM_RE.search(suffix):
        return "change"
    return ""


def _defeat_claim(prefix: str, suffix: str) -> bool:
    tail = prefix[-100:]
    matches = list(_DEATH_BEFORE_RE.finditer(tail))
    direct = False
    if matches:
        verb = matches[-1]
        lead = tail[max(0, verb.start() - 28):verb.start()]
        bridge = tail[verb.end():]
        direct = not _ATTEMPT_PREFIX_RE.search(lead.rstrip() + " ") \
            and not _NONCONTACT_BRIDGE_RE.search(bridge) \
            and not _EVENT_BOUNDARY_RE.search(bridge) and len(bridge) <= 70
    leave_change = bool(
        _LEAVE_BEFORE_RE.search(tail)
        and _LEAVE_DEATH_COMPLEMENT_RE.search(suffix)
    )
    return bool(
        direct
        or leave_change
        or _PASSIVE_DEATH_RE.search(suffix)
        or _PRONOUN_DEATH_RE.search(suffix)
        or _PASSIVE_HARM_THEN_DEATH_RE.search(suffix)
    )


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return non-empty sentence bodies without carrying an antecedent beyond one boundary."""
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in _SENTENCE_BOUNDARY_RE.finditer(text):
        if text[start:boundary.start()].strip():
            spans.append((start, boundary.start()))
        start = boundary.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return tuple(spans)


def _has_competing_neutral_antecedent(
    text: str,
    sentence_start: int,
    sentence_end: int,
    combatant: _Mention,
) -> bool:
    """Return whether the prior sentence exposes another plausible neutral referent.

    This guard has no general coreference oracle.  A determiner-led nominal outside the one exact
    combatant therefore makes cross-sentence ``it``/``them`` attribution uncertain.  Refusing the
    guard claim is safer than replacing valid narration on a guessed antecedent.
    """
    outside_combatant = (
        text[sentence_start:combatant.start],
        text[combatant.end:sentence_end],
    )
    return any(_ARTICLE_LED_NOMINAL_RE.search(segment) for segment in outside_combatant)


def _immediate_pronoun_mentions(
    text: str,
    explicit_mentions: tuple[_Mention, ...],
) -> tuple[_Mention, ...]:
    """Resolve one neutral pronoun only from one exact combatant in the prior sentence."""
    resolved: list[_Mention] = []
    spans = _sentence_spans(text)
    for index in range(len(spans) - 1):
        start, end = spans[index]
        antecedents = [
            mention
            for mention in explicit_mentions
            if start <= mention.start and mention.end <= end
        ]
        if len(antecedents) != 1 or len(antecedents[0].entity_ids) != 1:
            continue
        if _has_competing_neutral_antecedent(text, start, end, antecedents[0]):
            continue
        next_start, next_end = spans[index + 1]
        if any(
            next_start <= mention.start and mention.end <= next_end
            for mention in explicit_mentions
        ):
            continue
        pronouns = list(_NEUTRAL_TARGET_PRONOUN_RE.finditer(text, next_start, next_end))
        if len(pronouns) != 1:
            continue
        pronoun = pronouns[0]
        mention = _Mention(pronoun.start(), pronoun.end(), antecedents[0].entity_ids)
        prefix, suffix = _clause(text, mention)
        if _harm_claim_kind(prefix, suffix) == "change" or _defeat_claim(prefix, suffix):
            resolved.append(mention)
    return tuple(resolved)


def _existing_harm_targets(rows: object) -> set[str]:
    """Return rows whose durable HP already supports a stative injury description."""
    harmed: set[str] = set()
    if not isinstance(rows, Mapping):
        return harmed
    for raw_id, row in rows.items():
        if not isinstance(row, Mapping):
            continue
        entity_id = str(row.get("id") or raw_id)
        hp = row.get("hp")
        if not isinstance(hp, Mapping):
            continue
        current = hp.get("cur")
        maximum = hp.get("max")
        if isinstance(current, (int, float)) and not isinstance(current, bool) \
                and isinstance(maximum, (int, float)) and not isinstance(maximum, bool) \
                and current < maximum:
            harmed.add(entity_id)
    return harmed


def _outcome_lint_story(state: Mapping[str, Any], story: str, turn_index: int) -> str:
    """Mask only outcome words whose grammatical negation reverses their surface polarity."""
    rolls = state.get("rolls")
    checks = [
        row for row in rolls
        if isinstance(row, Mapping) and row.get("turn") == turn_index and row.get("tier")
    ] if isinstance(rolls, Sequence) and not isinstance(rolls, (str, bytes)) else []
    if not checks:
        return story
    decided = str(checks[-1].get("tier") or "")
    pattern = (
        _NEGATED_FAILURE_ASSERTION_RE
        if decided in {"success", "crit_success"}
        else _NEGATED_SUCCESS_ASSERTION_RE
        if decided in {"fail", "crit_fail"}
        else None
    )
    if pattern is None:
        return story
    return pattern.sub(lambda match: " " * (match.end() - match.start()), story)


def _authorized_impacts(realization: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    harm: set[str] = set()
    defeated: set[str] = set()
    for row in realization.get("asserted_settled") or []:
        if not isinstance(row, Mapping):
            continue
        event = row.get("event_meaning")
        target = event.get("target_entity_id") if isinstance(event, Mapping) else None
        if not isinstance(target, str) or not target:
            continue
        if row.get("impact_kind") == "harm":
            harm.add(target)
        if row.get("target_state") == "defeated":
            defeated.add(target)
    return harm, defeated


def narration_contradictions(
    realization: object,
    state: object,
    story: object,
    cfg,
    *,
    turn_index: int,
    klass: str = "new_turn",
    user_name: str = "",
    user_aliases: tuple = (),
    user_text: str = "",
) -> tuple[str, ...]:
    """Return stable reason codes for definite contradictions, never a rewritten candidate."""
    if not isinstance(state, Mapping) or not isinstance(story, str) or not story.strip():
        return ("guard_input_invalid",)
    try:
        valid = validate_narrator_realization(realization)
    except (TypeError, ValueError):
        return ("mechanical_authority_unavailable",)

    reasons: list[str] = []
    try:
        violations = linter.run(
            deepcopy(dict(state)),
            _outcome_lint_story(state, story, turn_index),
            cfg,
            klass=klass,
            user_name=user_name,
            user_aliases=user_aliases,
            turn=turn_index,
            user_text=user_text,
        )
        if any(row.rule == "outcome_match" for row in violations):
            reasons.append("roll_outcome_conflict")
    except Exception:
        return ("guard_evaluation_unavailable",)

    harm_targets, defeated_targets = _authorized_impacts(valid)
    rows = ((state.get("combat") or {}).get("combatants") or {})
    existing_harm_targets = _existing_harm_targets(rows)
    already_defeated = {
        str((row or {}).get("id") or entity_id)
        for entity_id, row in rows.items()
        if isinstance(row, Mapping) and (
            row.get("defeated") is True
            or int((row.get("hp") or {}).get("cur", 1) or 0) <= 0
        )
    } if isinstance(rows, Mapping) else set()
    clean_story = _unquoted(story)
    explicit_mentions = _combatant_mentions(state, clean_story)
    mentions = tuple(sorted(
        explicit_mentions + _immediate_pronoun_mentions(clean_story, explicit_mentions),
        key=lambda mention: (mention.start, mention.end),
    ))
    for mention in mentions:
        prefix, suffix = _clause(clean_story, mention)
        harm_claim_kind = _harm_claim_kind(prefix, suffix)
        authorized_harm = harm_targets | (
            existing_harm_targets if harm_claim_kind == "state" else set()
        )
        if harm_claim_kind and not mention.entity_ids <= authorized_harm:
            reasons.append("unsettled_combatant_impact")
        if _defeat_claim(prefix, suffix) \
                and not mention.entity_ids <= (defeated_targets | already_defeated):
            reasons.append("unsettled_combatant_defeat")
    return tuple(dict.fromkeys(reasons))


def guard_narration_story(
    basis: NarrationGuardBasis,
    state: object,
    story: object,
    cfg,
    *,
    klass: str = "new_turn",
    user_name: str = "",
    user_aliases: tuple = (),
    user_text: str = "",
) -> NarrationGuardDecision:
    """Judge one complete story against its frozen request-time authority."""
    if not isinstance(basis, NarrationGuardBasis) \
            or basis.schema != NARRATION_PRE_DISPLAY_GUARD_SCHEMA:
        return NarrationGuardDecision(False, EMPTY_FALLBACK_TEXT, ("guard_basis_invalid",))
    if narration_guard_state_fingerprint(state) != basis.state_fingerprint:
        return NarrationGuardDecision(
            False, basis.fallback_story, ("guard_state_changed",),
        )
    reasons = narration_contradictions(
        basis.realization,
        state,
        story,
        cfg,
        turn_index=basis.turn_index,
        klass=klass,
        user_name=user_name,
        user_aliases=user_aliases,
        user_text=user_text,
    )
    if reasons:
        return NarrationGuardDecision(False, basis.fallback_story, reasons)
    return NarrationGuardDecision(True, str(story), ())
