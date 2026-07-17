"""Trusted construction and linkage of recognition-only claim evidence.

``ingress`` names the transport that supplied the exact text, while ClaimFrame
``speaker`` names an attributed speaker inside that text.  A narrator reply that
quotes an NPC therefore remains narrator ingress with an NPC speaker.  Extraction
must link its actor-relative belief proposal to an existing exact claim instead of
reparsing the same exchange and creating a duplicate.  Code ingress is reserved for
an actual code-authored utterance, never a fact, event, or mechanics record.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping

from .claim_frame import build_claim_frames
from .knowledge import normalized_proposition, proposition_id
from .playerlex_recognition import merge_playerlex_proposal
from .semantic_fabric import load_default_semantic_fabric


# SillyTavern hides this exact one-line ledger-tag shape from the reader while retaining the raw
# model reply for state ingestion.  ClaimLex must consume the same story-visible projection, not
# treat protocol explanations such as an affinity reason as dialogue or attributed propositions.
_NARRATOR_CONTROL_TAG_RE = re.compile(
    r"\[\s*[A-Za-z][^\]\n|]*\|[^\]\n]*\]",
)


def _claim_visible_text(text: str, *, ingress: str) -> str:
    """Mask reader-hidden narrator tags without changing any source offset.

    Spaces preserve the raw response length and every visible character position, so ClaimFrame
    evidence spans still slice the exact visible text from the delivered response.  Newlines are
    outside the closed tag grammar and therefore remain byte-for-character unchanged as well.
    """
    if ingress != "narrator":
        return text
    return _NARRATOR_CONTROL_TAG_RE.sub(
        lambda match: " " * (match.end() - match.start()),
        text,
    )


def claim_ops_from_text(
    text: str,
    *,
    ingress: str,
    source_id: str,
    genre_ids: tuple[str, ...] = (),
    recognition_overlay: Callable[[str], Mapping] | None = None,
) -> list[dict]:
    """Return one meaning receipt plus its exact ClaimFrames, or no operations.

    The caller still commits through ``apply_delta(..., source='rule')``.  Raw
    model JSON is never accepted as a frame, and this helper grants no truth,
    mechanics, fact, belief, or World Event authority.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    claim_text = _claim_visible_text(text, ingress=ingress)
    if not claim_text.strip():
        return []
    fabric = load_default_semantic_fabric()
    meaning = fabric.translate(claim_text, genre_ids=genre_ids)
    if recognition_overlay is not None:
        meaning = merge_playerlex_proposal(
            meaning,
            recognition_overlay(claim_text),
            fabric=fabric,
            source_text=claim_text,
        )
    frames = build_claim_frames(
        claim_text,
        meaning,
        ingress=ingress,
        source_id=source_id,
    )
    if not frames:
        return []
    return [
        {"op": "semantic_meaning_commit", "meaning": meaning.receipt_dict()},
        *({"op": "claim_record", "frame": frame} for frame in frames),
    ]


def link_extracted_beliefs_to_claims(
    ops: Iterable[Mapping],
    claim_records: Iterable[Mapping],
    *,
    turn_lo: int,
    turn_hi: int,
) -> list[dict]:
    """Attach extraction beliefs to one exact same-batch Claim Record, or abstain.

    This never creates a Claim Record.  The Player and delivered-narrator paths have
    already recorded the source occurrence.  A link is admitted only for ``told`` or
    ``overheard`` evidence with an exact teller, proposition identity, polarity, and
    batch turn, and only when exactly one Claim Record matches.  Caller-supplied links
    are removed before this code-owned lookup.
    """

    try:
        lo, hi = int(turn_lo), int(turn_hi)
    except (TypeError, ValueError):
        lo, hi = 1, 0
    candidates = []
    for raw in claim_records:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("frame"), Mapping):
            continue
        try:
            turn = int(raw.get("turn"))
        except (TypeError, ValueError):
            continue
        frame = raw["frame"]
        claim_id = str(raw.get("claim_id") or "").strip()
        speaker = str(frame.get("speaker") or "").strip()
        identity = str(frame.get("proposition_identity") or "").strip()
        polarity = str(frame.get("proposition_polarity") or "").strip()
        if lo <= turn <= hi and claim_id and speaker and identity and polarity:
            candidates.append((claim_id, speaker.casefold(), identity, polarity))

    linked: list[dict] = []
    for raw in ops:
        row = dict(raw) if isinstance(raw, Mapping) else raw
        if not isinstance(row, dict):
            linked.append(row)
            continue
        if row.get("op") != "belief_acquire":
            linked.append(row)
            continue
        row.pop("claim_id", None)
        evidence = str(row.get("evidence_source") or row.get("source") or "").strip()
        teller = str(row.get("teller") or "").strip()
        statement = str(row.get("statement") or "").strip()
        if evidence not in {"told", "overheard"} or not teller or not statement:
            linked.append(row)
            continue
        try:
            identity = proposition_id(statement)
        except ValueError:
            linked.append(row)
            continue
        polarity = normalized_proposition(statement)[1]
        matches = {
            claim_id for claim_id, speaker, candidate_identity, candidate_polarity in candidates
            if speaker == teller.casefold()
            and candidate_identity == identity
            and candidate_polarity == polarity
        }
        if len(matches) == 1:
            row["claim_id"] = next(iter(matches))
        linked.append(row)
    return linked
