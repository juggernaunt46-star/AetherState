"""Entity discovery (08 B2 / 08 E1): new-entity creation is a PRIVILEGED op.

Tier-0 counts evidence — a capitalized token adjacent to dialogue attribution or an action
verb — across turns (rules, zero cost, every turn). Quarantined unknown-name extraction ops
feed the same counter (jobs.py). At >=2 distinct turns of evidence:
  auto_entity_create=true (default) -> minimal Entity(kind=character) via source="rule";
  false -> the row is marked 'proposed' for the inspector queue (P6 UI reads it).
Extraction itself can NEVER create entities (03 SS5.1; authority matrix rejects entity_add).

Everything here is cold-path (post-stream) and fail-open (invariants 2/3).
"""
from __future__ import annotations

import logging

from .state import apply_delta

log = logging.getLogger("aetherstate.discovery")

EVIDENCE_TURNS = 2                # 08 B2: >=2 turns of evidence

# dialogue attribution + common action verbs (lowercased neighbor match)
_VERBS = {
    "said", "says", "saying", "asked", "asks", "replied", "replies", "answered", "answers",
    "whispered", "whispers", "shouted", "shouts", "muttered", "mutters", "murmured", "murmurs",
    "called", "calls", "laughed", "laughs", "giggled", "giggles", "sighed", "sighs",
    "smiled", "smiles", "grinned", "grins", "smirked", "smirks", "nodded", "nods",
    "frowned", "frowns", "blushed", "blushes", "moaned", "moans", "gasped", "gasps",
    "walked", "walks", "entered", "enters", "arrived", "arrives", "left", "leaves",
    "stood", "stands", "sat", "sits", "knelt", "kneels", "leaned", "leans", "stepped", "steps",
    "turned", "turns", "looked", "looks", "watched", "watches", "grabbed", "grabs",
    "reached", "reaches", "pulled", "pulls", "pushed", "pushes", "touched", "touches",
    "kissed", "kisses", "held", "holds", "wore", "wears", "took", "takes", "gave", "gives",
}

# capitalized tokens that are never names in this position
_STOP = {
    "the", "a", "an", "i", "he", "she", "they", "it", "we", "you", "his", "her", "their",
    "its", "our", "your", "my", "and", "but", "then", "when", "while", "after", "before",
    "as", "so", "now", "here", "there", "this", "that", "these", "those", "what", "who",
    "why", "how", "where", "yes", "no", "not", "narrator", "user", "ooc", "scene",
    "suddenly", "meanwhile", "later", "inside", "outside", "everyone", "someone", "nobody",
    "morning", "evening", "night", "today", "tomorrow", "yesterday", "well", "oh", "hey",
    "okay", "ok", "sir", "madam", "miss", "mister", "lord", "lady", "god", "gods",
}

_CAP = __import__("re").compile(r"^[A-Z][a-z]{2,}$")   # >=3 chars: "I"/"Ok" never match
_WORD = __import__("re").compile(r"[A-Za-z']+|[^\sA-Za-z']")  # punctuation breaks name runs


def scan(text: str) -> set[str]:
    """Candidate names: capitalized token(s) with an attribution/action verb IMMEDIATELY
    adjacent (prev or next token), or a speaker-prefix form ('Marla: ...'). Multi-word
    runs of capitalized tokens merge into one name. Pure function, no state."""
    if not text:
        return set()
    tokens = _WORD.findall(text)
    lowered = [t.lower() for t in tokens]
    found: set[str] = set()
    i = 0
    while i < len(tokens):
        if not _CAP.match(tokens[i]) or lowered[i] in _STOP:
            i += 1
            continue
        j = i                                   # extend over "Marla Vane"-style runs
        while (j + 1 < len(tokens) and _CAP.match(tokens[j + 1])
               and lowered[j + 1] not in _STOP):
            j += 1
        prev_ok = i > 0 and lowered[i - 1] in _VERBS
        nxt = j + 1
        next_ok = nxt < len(tokens) and (lowered[nxt] in _VERBS or tokens[nxt] == ":")
        if prev_ok or next_ok:
            found.add(" ".join(tokens[i:j + 1]))
        i = j + 1
    return found


def known_names(state: dict, extra: tuple[str, ...] = ()) -> set[str]:
    """Registered entity names + aliases + guard/persona names, lowercased."""
    out = {str(x).lower() for x in extra if x}
    for e in state.get("entities", {}).values():
        out.add(str(e.get("name", "")).lower())
        out.update(str(a).lower() for a in e.get("aliases", []))
    return out


def consider(store, cfg, session_id: str, branch_id: str, turn: int, name: str,
             count: int) -> str:
    """Threshold check + creation/proposal. Returns 'created' | 'proposed' | ''."""
    if count < EVIDENCE_TURNS:
        return ""
    if not cfg.extraction.auto_entity_create:
        store.discovery_mark(branch_id, name, "proposed")
        log.info("discovery: '%s' proposed (auto_entity_create=false)", name)
        return "proposed"
    r = apply_delta(store, session_id, branch_id, turn,
                    [{"op": "entity_add", "name": name}], "rule", cfg)
    if r.applied:
        store.discovery_mark(branch_id, name, "created")
        log.info("discovery: entity '%s' created after %d turns of evidence", name, count)
        return "created"
    return ""


def observe_text(store, cfg, session_id: str, branch_id: str, turn: int, text: str,
                 known: set[str]) -> list[str]:
    """Tier-0 text-evidence pass for one settled turn. Returns names created this call."""
    created = []
    for name in scan(text):
        if name.lower() in known:
            continue
        n = store.discovery_bump(branch_id, name, turn)
        if consider(store, cfg, session_id, branch_id, turn, name, n) == "created":
            created.append(name)
    return created
