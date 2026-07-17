"""Recognition-only ClaimFrames and occurrence-bound Claim Records.

ClaimLex recognizes speech, attitude, and attribution structure.  Neither a
frame nor its durable occurrence establishes that its proposition is true,
occurred, was fulfilled, was deliberate deception, or can drive mechanics.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping

from .capability_glossary import content_fingerprint
from .knowledge import normalized_proposition, proposition_id
from .semantic_fabric import CompiledMeaning, claim_dialogue_attributions

CLAIM_FRAME_SCHEMA = "aetherstate-claim-frame/2"
LEGACY_CLAIM_FRAME_SCHEMA = "aetherstate-claim-frame/1"
CLAIM_RECORD_SCHEMA = "aetherstate-claim-record/1"

_INGRESS = frozenset({"player", "npc", "narrator", "creator", "extraction", "code"})
_VISIBILITY = frozenset({"public", "player", "actor_scoped", "hidden"})
_NAME = r"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2}"
_NAME_AT_END = re.compile(rf"(?P<name>{_NAME})\s*$")
_QUOTE = re.compile(r'["“](.*?)["”]', re.DOTALL)
_CONTENT_MARKER = re.compile(r"\b(?:that|whether|about)\b", re.IGNORECASE)
_CONDITION = re.compile(r"\b(?:if|unless|provided\s+that|as\s+long\s+as)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:not|never|no\s+longer|cannot|can't|didn't|doesn't|isn't|wasn't|won't|wouldn't)\b",
    re.IGNORECASE,
)
_FUTURE = re.compile(r"\b(?:will|shall|going\s+to)\b", re.IGNORECASE)
_PAST = re.compile(
    r"\b(?:was|were|had|did|said|reported|claimed|denied|remembered|observed|heard|saw)\b",
    re.IGNORECASE,
)
_MODAL = re.compile(r"\b(must|may|might|could|would|should|will|shall|can)\b", re.IGNORECASE)
_TIME = re.compile(
    r"\b(?:yesterday|today|tomorrow|tonight|now|then|earlier|later|"
    r"at\s+(?:dawn|noon|dusk|midnight)|on\s+day\s+\d+|before\s+[^,.!?]+|after\s+[^,.!?]+)\b",
    re.IGNORECASE,
)
_QUANTIFIER = re.compile(
    r"\b(?:all|every|each|any|some|many|most|few|several|none|nobody|nothing|no)\b",
    re.IGNORECASE,
)
_EVIDENCE = re.compile(
    r"\b(?:according\s+to|because|based\s+on|I\s+saw|I\s+heard|witnessed|overheard|inferred)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?=(?:[\"'\u2019\u201d])?(?:\s|$))")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_WORLD = re.compile(r"world_[0-9a-f]{32}\Z")

_SOURCE_ROLES = frozenset({
    "source", "speaker", "holder", "rememberer", "observer", "reasoner", "planner",
    "promisor", "questioner", "quoted_source", "accuser", "attributing_source",
})
_ADDRESSEE_ROLES = frozenset({"addressee", "accused_party", "accused_source"})


def _evidence_span(text: str, start: int, end: int) -> dict[str, Any]:
    if isinstance(start, bool) or isinstance(end, bool) or not 0 <= start < end <= len(text):
        raise ValueError("ClaimFrame span is outside its exact source")
    value = text[start:end]
    return {
        "start": int(start),
        "end": int(end),
        "text": value,
        "fingerprint": content_fingerprint(value),
    }


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
    left = max(text.rfind(".", 0, position), text.rfind("!", 0, position), text.rfind("?", 0, position))
    start = left + 1
    terminal = _SENTENCE_END.search(text, position)
    end = terminal.start() if terminal else len(text)
    return _trim_span(text, start, end)


def _speaker_before(text: str, governor_start: int, floor: int) -> tuple[str | None, tuple[int, int] | None]:
    segment = text[floor:governor_start]
    # Remove attribution connectors while preserving source offsets.
    match = _NAME_AT_END.search(segment)
    if match is None:
        return None, None
    start, end = floor + match.start("name"), floor + match.end("name")
    value = text[start:end]
    if value.endswith(("'s", "\u2019s")):
        value = value[:-2]
    elif value.endswith(("s'", "s\u2019")):
        value = value[:-1]
    return value, (start, end)


_NOMINAL_GOVERNOR_LEFT = re.compile(
    r"\b(?:a|an|the|this|that|these|those|my|your|his|her|its|our|their|"
    r"either|another|many|few|several|some|every|each|no)\s*$",
    re.IGNORECASE,
)
_POSSESSIVE_LEFT = re.compile(r"\b[\w'\u2019-]+(?:'s|\u2019s)\s*$")
_CLAIM_PRONOUN_SUBJECT = re.compile(
    r"\b(?:I|we|you|he|she|they)(?:\s+\w+ly){0,2}\s*$",
    re.IGNORECASE,
)
_CLAIM_NAMED_SUBJECT = re.compile(rf"\b{_NAME}(?:\s+\w+ly){{0,2}}\s*$")
_NOMINAL_GOVERNOR_SURFACES = frozenset({
    "state", "states", "report", "reports", "thought", "recall", "witness",
    "witnesses", "forecast", "forecasts", "promise", "promises", "quote", "quotes",
    "lie", "lies",
})


def _is_fresh_claim_governor(text: str, match: Any) -> bool:
    """Reject noun mentions that a broad ClaimLex surface cannot disambiguate alone.

    Several ClaimLex surfaces can be nouns as well as governors (``your promise``, ``either
    report``, ``many witnesses``).  Claim Records are occurrences, so those mentions fail closed
    unless they carry a same-sentence content clause or have a plausible grammatical subject.
    Recognition still grants no truth, occurrence, or fulfillment authority.
    """
    surface = text[match.start:match.end].casefold()
    if surface not in _NOMINAL_GOVERNOR_SURFACES:
        return True
    _sentence_start, sentence_end = _sentence_bounds(text, match.start)
    governed_tail = text[match.end:sentence_end]
    left = text[max(0, match.start - 96):match.start]
    has_content = bool(_CONTENT_MARKER.search(governed_tail) or _QUOTE.search(governed_tail))
    # Determiner-led uses remain noun phrases even when a relative clause follows:
    # ``a promise that costs him`` and ``the report that remains unproven``.
    if _NOMINAL_GOVERNOR_LEFT.search(left):
        return False
    # A possessive source plus bounded content is productive attribution (``Ryn's report that``),
    # while a bare possessive noun mention is not a fresh reporting occurrence.
    if _POSSESSIVE_LEFT.search(left):
        return has_content
    plausible_subject = bool(
        _CLAIM_PRONOUN_SUBJECT.search(left)
        or _CLAIM_NAMED_SUBJECT.search(left)
    )
    return plausible_subject


def _content_start(text: str, governor_end: int, sentence_end: int) -> tuple[int, tuple[int, int] | None]:
    quote = _QUOTE.search(text, governor_end, sentence_end)
    marker = _CONTENT_MARKER.search(text, governor_end, sentence_end)
    if quote is not None and (marker is None or quote.start() <= marker.start() + 2):
        return quote.start(1), (quote.start(), quote.end())
    start = marker.end() if marker is not None else governor_end
    while start < sentence_end and text[start] in " \t,:;-":
        start += 1
    return start, None


def _addressee_between(
    text: str,
    governor_end: int,
    proposition_start: int,
) -> tuple[str | None, tuple[int, int] | None]:
    if proposition_start <= governor_end:
        return None, None
    segment = text[governor_end:proposition_start]
    # Content markers are not people; take the last capitalized identity in the governed gap.
    matches = list(re.finditer(_NAME, segment))
    if not matches:
        return None, None
    match = matches[-1]
    start, end = governor_end + match.start(), governor_end + match.end()
    return text[start:end], (start, end)


_NON_IDENTITY_LEADS = frozenset({"a", "an", "the", "this", "that", "these", "those"})


def _leading_named_identity(
    text: str,
    start: int,
    end: int,
) -> tuple[str | None, tuple[int, int] | None]:
    """Return an exact leading proper-name candidate, never a pronoun or determiner phrase."""

    match = re.match(rf"(?P<name>{_NAME})(?=\b)", text[start:end])
    if match is None:
        return None, None
    value = match.group("name")
    if value.split(maxsplit=1)[0].casefold() in _NON_IDENTITY_LEADS:
        return None, None
    bounds = (start + match.start("name"), start + match.end("name"))
    return value, bounds


def _resolve_frame_role(
    frame: dict[str, Any],
    role_name: str,
    value: str,
    span: dict[str, Any] | None,
) -> None:
    """Resolve one already-shaped role in both indexed and ordered projections."""

    identity = value.casefold()
    role = frame["role_map"].get(role_name)
    if isinstance(role, dict):
        role.update(_role(role_name, value, span, identity=identity))
    for candidate in frame["proposition_roles"]:
        if isinstance(candidate, dict) and candidate.get("role") == role_name and candidate is not role:
            candidate.update(_role(role_name, value, span, identity=identity))


def _condition_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    marker = _CONDITION.search(text, start, end)
    if marker is None:
        return None
    terminal = re.search(r"[,;]", text[marker.end():end])
    c_end = marker.end() + terminal.start() if terminal else end
    return _trim_span(text, marker.start(), c_end)


def _typed_feature(
    value: str,
    *,
    status: str = "recognized",
    evidence: dict[str, Any] | None = None,
    source: str = "surface",
) -> dict[str, Any]:
    return {"value": value, "status": status, "evidence": evidence, "source": source}


def _claim_proposition_id(statement: str, polarity: str) -> str:
    core, _ = normalized_proposition(statement)
    return content_fingerprint({"proposition_core": core, "polarity": polarity})


def _evidence_ref(span: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if span is None:
        return None
    bounds = {"start": int(span["start"]), "end": int(span["end"])}
    payload = {"span": bounds, "text": str(span["text"])}
    return {**payload, "fingerprint": content_fingerprint(payload)}


def _proposition_structure_roles(
    source_text: str,
    proposition_start: int,
    proposition_end: int,
    proposition_identity: str,
) -> list[dict[str, Any]]:
    content = _evidence_span(source_text, proposition_start, proposition_end)
    tokens = list(re.finditer(r"[A-Za-z0-9][\w'’-]*", source_text[proposition_start:proposition_end]))
    subject_span = None
    predicate_span = None
    if tokens:
        first = tokens[0]
        subject_span = _evidence_span(
            source_text, proposition_start + first.start(), proposition_start + first.end()
        )
        if len(tokens) > 1:
            predicate_span = _evidence_span(
                source_text, proposition_start + tokens[1].start(), proposition_end
            )
    return [
        _role("content", content["text"], content, identity=proposition_identity),
        _role(
            "subject_candidate",
            subject_span["text"] if subject_span else None,
            subject_span,
            identity=subject_span["text"].casefold() if subject_span else None,
        ),
        _role(
            "predicate",
            predicate_span["text"] if predicate_span else None,
            predicate_span,
        ),
    ]


def _stance_for(claim_class: str) -> str:
    return {
        "denial": "denies", "belief": "believes", "memory": "remembers",
        "observation": "reports_observation", "inference": "infers",
        "prediction": "predicts", "plan": "intends", "promise": "commits",
        "hypothesis": "hypothesizes", "question": "questions", "quotation": "quotes",
        "accusation": "accuses", "deception_description": "attributes_deception",
        "hearsay": "relays_hearsay", "report": "reports", "assertion": "asserts",
    }.get(claim_class, "expresses")


def _role(
    role: str,
    value: str | None,
    span: dict[str, Any] | None,
    *,
    identity: str | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "status": "resolved" if value else "unresolved",
        "value": value,
        "identity": identity,
        "span": deepcopy(span),
    }


def build_claim_frames(
    source_text: str,
    meaning: CompiledMeaning,
    *,
    ingress: str = "player",
    source_id: str = "player",
    authority_ceiling: str = "recognition_only",
    playerlex_anchor_refs: tuple[str, ...] | list[str] = (),
) -> list[dict[str, Any]]:
    """Build one minimized ClaimFrame per actual ClaimLex governor."""
    if not isinstance(source_text, str) or not source_text:
        return []
    if meaning.source_fingerprint != content_fingerprint(source_text):
        raise ValueError("ClaimFrame meaning belongs to a different source")
    if ingress not in _INGRESS or authority_ceiling != "recognition_only":
        raise ValueError("ClaimFrame has a fixed recognition ceiling")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("ClaimFrame source identity is required")
    anchors = sorted({str(ref) for ref in playerlex_anchor_refs if isinstance(ref, str) and ref})

    matches = sorted(
        (
            match for match in meaning.matches
            if match.lex_id == "claim"
            and match.surface_baseline != "construction"
            and not match.concept_id.startswith("claim.frame.")
            and _is_fresh_claim_governor(source_text, match)
        ),
        key=lambda match: (match.start, match.end, match.concept_id),
    )
    dialogue_by_governor = {
        (row.governor_start, row.governor_end): row
        for row in claim_dialogue_attributions(source_text)
    }
    drafts: list[dict[str, Any]] = []
    for index, match in enumerate(matches, 1):
        frame_anchors = sorted({
            *anchors,
            *(
                str(ref) for ref in match.source_ids
                if match.surface_baseline == "playerlex" and str(ref).startswith("playerlex.")
            ),
        })
        dialogue = dialogue_by_governor.get((match.start, match.end)) \
            if match.surface_baseline == "dialogue_construction" else None
        sentence_start, sentence_end = _sentence_bounds(source_text, match.start)
        if dialogue is not None:
            proposition_start = dialogue.proposition_start
            proposition_end = dialogue.proposition_end
            quotation = (dialogue.quotation_start, dialogue.quotation_end)
        else:
            proposition_start, quotation = _content_start(source_text, match.end, sentence_end)
            proposition_end = quotation[1] - 1 if quotation else sentence_end
        proposition_start, proposition_end = _trim_span(
            source_text, proposition_start, proposition_end
        )
        if proposition_end <= proposition_start:
            proposition_start, proposition_end = match.start, match.end
        proposition = source_text[proposition_start:proposition_end]

        # A nested governor's source begins inside its parent's proposition.  Until parent links
        # are assigned, the nearest preceding content boundary is the safest exact floor.
        prior_starts = [
            row["proposition_span"]["start"] for row in drafts
            if row["proposition_span"]["start"] <= match.start < row["proposition_span"]["end"]
        ]
        speaker_floor = max(prior_starts, default=sentence_start)
        if dialogue is not None:
            speaker = dialogue.speaker
            speaker_bounds = (dialogue.speaker_start, dialogue.speaker_end)
            addressee, addressee_bounds = None, None
        else:
            speaker, speaker_bounds = _speaker_before(source_text, match.start, speaker_floor)
            addressee, addressee_bounds = _addressee_between(
                source_text, match.end, proposition_start
            )
        speaker_span = _evidence_span(source_text, *speaker_bounds) if speaker_bounds else None
        addressee_span = _evidence_span(source_text, *addressee_bounds) if addressee_bounds else None
        governor_span = _evidence_span(source_text, match.start, match.end)
        prop_span = _evidence_span(source_text, proposition_start, proposition_end)
        quote_span = _evidence_span(source_text, *quotation) if quotation else None
        claim_end = max(match.end, proposition_end, quotation[1] if quotation else 0)
        claim_start = speaker_bounds[0] if speaker_bounds else match.start
        if dialogue is not None:
            claim_start = min(claim_start, dialogue.quotation_start)
        claim_span = _evidence_span(source_text, claim_start, claim_end)

        condition_bounds = _condition_span(source_text, proposition_start, proposition_end)
        condition = _evidence_span(source_text, *condition_bounds) if condition_bounds else None
        modal_match = _MODAL.search(source_text, proposition_start, proposition_end)
        modal_span = _evidence_span(
            source_text, modal_match.start(), modal_match.end()
        ) if modal_match else None
        modality = str(match.features.get("modality") or (
            modal_match.group(1).casefold() if modal_match else "asserted"
        ))
        time_match = _TIME.search(source_text, proposition_start, proposition_end)
        time_span = _evidence_span(
            source_text, time_match.start(), time_match.end()
        ) if time_match else None
        quantifiers = [
            _evidence_span(source_text, found.start(), found.end())
            for found in _QUANTIFIER.finditer(source_text, proposition_start, proposition_end)
        ]
        evidence_match = _EVIDENCE.search(source_text, sentence_start, proposition_end)
        evidence_span = _evidence_span(
            source_text, evidence_match.start(), evidence_match.end()
        ) if evidence_match else None
        claim_class = str(match.features.get("claim_class") or match.kind)
        prop_core, prop_polarity = normalized_proposition(proposition)
        if not prop_core:
            prop_polarity = "unspecified"
        tense = "future" if _FUTURE.search(proposition) else (
            "past" if _PAST.search(proposition) else "present_or_unspecified"
        )
        source_value = speaker or source_id
        if speaker is not None and speaker.casefold() == "i" and ingress in {"player", "npc"}:
            # First-person text has an ingress-owned speaker identity.  Keep the exact ``I`` span
            # as evidence, but do not store a contextless pronoun as the durable actor identity.
            source_value = source_id
        grammatical_source_value = source_value
        grammatical_source_span = speaker_span
        accused_party, accused_party_bounds = _leading_named_identity(
            source_text, proposition_start, proposition_end
        ) if claim_class == "accusation" else (None, None)
        accused_party_span = _evidence_span(
            source_text, *accused_party_bounds
        ) if accused_party_bounds else None
        if claim_class == "deception_description":
            # ``Vosk lied ...`` is a claim *by this ingress source* about Vosk.  The grammatical
            # subject is the accused source, not a forged first-person statement from Vosk.
            source_value = source_id
            speaker_span = None
        base_proposition_id = proposition_id(proposition)
        claim_proposition_id = _claim_proposition_id(proposition, prop_polarity)
        roles: dict[str, dict[str, Any]] = {
            "speaker": _role("speaker", source_value, speaker_span,
                             identity=source_value.casefold()),
            "source": _role("source", source_value, speaker_span,
                            identity=source_value.casefold()),
            "proposition": _role("proposition", proposition, prop_span,
                                 identity=claim_proposition_id),
            "addressee": _role("addressee", addressee, addressee_span,
                               identity=addressee.casefold() if addressee else None),
        }
        unresolved: list[str] = []
        for required in match.required_roles:
            if required == "accused_party" and claim_class == "accusation":
                roles[required] = _role(
                    required, accused_party, accused_party_span,
                    identity=accused_party.casefold() if accused_party else None,
                )
                if accused_party is None:
                    unresolved.append(required)
            elif required == "attributing_source" and claim_class == "deception_description":
                roles[required] = _role(
                    required, source_value, speaker_span, identity=source_value.casefold()
                )
            elif required == "accused_source" and claim_class == "deception_description":
                accused_source = grammatical_source_value if speaker is not None else None
                accused_source_span = grammatical_source_span if speaker is not None else None
                roles[required] = _role(
                    required, accused_source, accused_source_span,
                    identity=accused_source.casefold() if accused_source else None,
                )
                if accused_source is None:
                    unresolved.append(required)
            elif required in _SOURCE_ROLES:
                roles[required] = _role(
                    required, source_value, speaker_span, identity=source_value.casefold()
                )
                if speaker is None:
                    unresolved.append(required)
            elif required in _ADDRESSEE_ROLES:
                roles[required] = _role(
                    required, addressee, addressee_span,
                    identity=addressee.casefold() if addressee else None,
                )
                if addressee is None:
                    unresolved.append(required)
            elif required == "proposition":
                roles[required] = roles["proposition"]
            elif required == "quotation":
                roles[required] = _role(
                    required, proposition if quote_span else None, quote_span,
                    identity=claim_proposition_id if quote_span else None,
                )
                if quote_span is None:
                    unresolved.append(required)
            else:
                roles[required] = _role(required, None, None)
                unresolved.append(required)
        if speaker is None and claim_class != "deception_description" \
                and "speaker" not in unresolved:
            unresolved.append("speaker")

        frame_id = f"claim-{index}"
        payload: dict[str, Any] = {
            "schema": CLAIM_FRAME_SCHEMA,
            "frame_id": frame_id,
            "source_fingerprint": meaning.source_fingerprint,
            "source_length": len(source_text),
            "fabric_fingerprint": meaning.fabric_fingerprint,
            "meaning_ref": match.entry_fingerprint,
            "claim_concept_id": match.concept_id,
            "claim_class": claim_class,
            "governor": governor_span["text"],
            "claim_span": claim_span,
            "governor_span": governor_span,
            "proposition_span": prop_span,
            "quotation_span": quote_span,
            "speaker": source_value,
            "speaker_span": speaker_span,
            "source": source_id,
            "addressee": addressee,
            "addressee_span": addressee_span,
            "parent_frame_id": None,
            "parent_claim_ref": None,
            "child_frame_ids": [],
            "nested_attribution_depth": 0,
            "proposition_id": claim_proposition_id,
            "proposition_identity": base_proposition_id,
            "proposition": proposition,
            "proposition_roles": [
                *list(roles.values()),
                *_proposition_structure_roles(
                    source_text, proposition_start, proposition_end, claim_proposition_id
                ),
            ],
            "role_map": roles,
            "proposition_polarity": prop_polarity,
            "speech_act_polarity": str(
                match.features.get("speech_act_polarity") or "positive"
            ),
            "modality": modality,
            "modality_detail": _typed_feature(
                modality, evidence=modal_span,
                source="claimlex" if match.features.get("modality") else "surface",
            ),
            "tense": tense,
            "time": _typed_feature(
                time_span["text"] if time_span else "unspecified",
                status="recognized" if time_span else "unresolved",
                evidence=time_span,
            ),
            "condition_span": condition,
            "condition": _typed_feature(
                condition["text"] if condition else "unconditional_or_unspecified",
                status="recognized" if condition else "unresolved",
                evidence=condition,
            ),
            "quantification": {
                "status": "recognized" if quantifiers else "unresolved",
                "items": quantifiers,
            },
            "evidential_source": _typed_feature(
                str(match.features.get("evidentiality") or (
                    evidence_span["text"] if evidence_span else "unspecified"
                )),
                status="recognized" if match.features.get("evidentiality") or evidence_span
                else "unresolved",
                evidence=evidence_span,
                source="claimlex" if match.features.get("evidentiality") else "surface",
            ),
            "speaker_stance": _typed_feature(
                _stance_for(claim_class), source="claim_class"
            ),
            "semantic_features": {
                "proposition_polarity": _typed_feature(prop_polarity, source="surface"),
                "speech_act_polarity": _typed_feature(
                    str(match.features.get("speech_act_polarity") or "positive"),
                    source="claimlex",
                ),
                "modality": _typed_feature(
                    modality, evidence=modal_span,
                    source="claimlex" if match.features.get("modality") else "surface",
                ),
                "tense": _typed_feature(tense, source="surface"),
                "time": _typed_feature(
                    time_span["text"] if time_span else "unspecified",
                    status="recognized" if time_span else "unresolved",
                    evidence=time_span,
                ),
                "condition": _typed_feature(
                    condition["text"] if condition else "unconditional_or_unspecified",
                    status="recognized" if condition else "unresolved",
                    evidence=condition,
                ),
                "quantification": _typed_feature(
                    quantifiers[0]["text"] if quantifiers else "unspecified",
                    status="recognized" if quantifiers else "unresolved",
                    evidence=quantifiers[0] if quantifiers else None,
                ),
                "evidential_source": _typed_feature(
                    str(match.features.get("evidentiality") or (
                        evidence_span["text"] if evidence_span else "unspecified"
                    )),
                    status="recognized" if match.features.get("evidentiality") or evidence_span
                    else "unresolved",
                    evidence=evidence_span,
                ),
                "stance": _typed_feature(_stance_for(claim_class), source="claim_class"),
            },
            "evidence_spans": {
                "claim": _evidence_ref(claim_span),
                "governor": _evidence_ref(governor_span),
                "proposition": _evidence_ref(prop_span),
                "quotation": _evidence_ref(quote_span),
                "speaker": _evidence_ref(speaker_span),
                "addressee": _evidence_ref(addressee_span),
                "condition": _evidence_ref(condition),
                "time": _evidence_ref(time_span),
            },
            "ambiguity": sorted(set(match.ambiguity)),
            "unresolved_roles": sorted(set(unresolved)),
            "playerlex_anchor_refs": frame_anchors,
            "ingress": ingress,
            "authority_ceiling": "recognition_only",
            "establishes_truth": False,
            "establishes_occurrence": False,
            "authorizes_mechanics": False,
            "admits_world_event": False,
        }
        drafts.append(payload)

    # Bound nested attribution after all proposition extents are known.  The nearest enclosing
    # proposition is the direct parent; content constructions never become phantom frames.
    for child in drafts:
        candidates = [
            parent for parent in drafts
            if parent is not child
            and parent["proposition_span"]["start"] <= child["governor_span"]["start"]
            < parent["proposition_span"]["end"]
        ]
        if candidates:
            parent = max(candidates, key=lambda row: row["proposition_span"]["start"])
            child["parent_frame_id"] = parent["frame_id"]
            child["parent_claim_ref"] = parent["frame_id"]
            parent["child_frame_ids"].append(child["frame_id"])
            if child["claim_class"] == "deception_description":
                # Nested deception is attributed by the enclosing claimant.  Preserve the local
                # grammatical subject independently as ``accused_source``.
                parent_speaker = str(parent["speaker"])
                parent_span = deepcopy(parent["speaker_span"])
                child["speaker"] = parent_speaker
                child["speaker_span"] = parent_span
                child["evidence_spans"]["speaker"] = _evidence_ref(parent_span)
                for role_name in ("speaker", "source", "attributing_source"):
                    _resolve_frame_role(child, role_name, parent_speaker, parent_span)
                child["unresolved_roles"] = sorted(
                    set(child["unresolved_roles"])
                    - {"speaker", "source", "attributing_source"}
                )
    by_id = {row["frame_id"]: row for row in drafts}
    for row in drafts:
        depth = 0
        parent_id = row["parent_frame_id"]
        seen: set[str] = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            depth += 1
            parent_id = by_id[parent_id]["parent_frame_id"]
        row["nested_attribution_depth"] = depth
        row["child_frame_ids"] = sorted(row["child_frame_ids"])
        row["fingerprint"] = content_fingerprint(row)
    return drafts


def _validate_span(row: Mapping[str, Any], key: str, *, optional: bool = False) -> None:
    span = row.get(key)
    if span is None and optional:
        return
    if not isinstance(span, Mapping):
        raise ValueError(f"ClaimFrame {key} is malformed")
    required = {"start", "end", "text", "fingerprint"}
    if set(span) != required:
        raise ValueError(f"ClaimFrame {key} is malformed")
    start, end = span.get("start"), span.get("end")
    if isinstance(start, bool) or isinstance(end, bool) \
            or not isinstance(start, int) or not isinstance(end, int) \
            or not 0 <= start < end <= int(row.get("source_length", -1)):
        raise ValueError(f"ClaimFrame {key} is invalid: outside its source")
    text = span.get("text")
    if not isinstance(text, str) or len(text) != end - start \
            or span.get("fingerprint") != content_fingerprint(text):
        raise ValueError(f"ClaimFrame {key} evidence is invalid")


def _span_within(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    return outer["start"] <= inner["start"] < inner["end"] <= outer["end"]


def _validate_contained_span(
    outer: Mapping[str, Any],
    inner: Mapping[str, Any],
    label: str,
) -> None:
    """Prove one minimized evidence span against text retained by its enclosing span."""
    if not _span_within(outer, inner):
        raise ValueError(f"ClaimFrame {label} scope is inconsistent")
    start = inner["start"] - outer["start"]
    end = inner["end"] - outer["start"]
    if outer["text"][start:end] != inner["text"]:
        raise ValueError(f"ClaimFrame {label} text conflicts with its claim evidence")


def _speaker_value_from_span(row: Mapping[str, Any]) -> str:
    """Rebuild the durable speaker value from its exact grammatical evidence."""
    source = row.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("ClaimFrame ingress source is malformed")
    span = row.get("speaker_span")
    if span is None:
        return source
    value = str(span["text"])
    if value.endswith(("'s", "\u2019s")):
        value = value[:-2]
    elif value.endswith(("s'", "s\u2019")):
        value = value[:-1]
    if value.casefold() == "i" and row.get("ingress") in {"player", "npc"}:
        return source
    return value


def _validate_role_projection(row: Mapping[str, Any]) -> None:
    """Keep ordered and indexed proposition roles bound to the frame's core evidence."""
    roles = row.get("proposition_roles")
    role_map = row.get("role_map")
    if not isinstance(roles, list) or not roles or not isinstance(role_map, Mapping):
        raise ValueError("ClaimFrame proposition roles are malformed")

    ordered: dict[str, Mapping[str, Any]] = {}
    for role in roles:
        if not isinstance(role, Mapping) or not isinstance(role.get("role"), str) \
                or role.get("status") not in {"resolved", "unresolved"}:
            raise ValueError("ClaimFrame proposition role is malformed")
        name = role["role"]
        if name in ordered:
            raise ValueError("ClaimFrame proposition roles contain a duplicate role")
        value = role.get("value")
        if value is not None and not isinstance(value, str):
            raise ValueError("ClaimFrame proposition role value is malformed")
        if role["status"] != ("resolved" if value else "unresolved"):
            raise ValueError("ClaimFrame proposition role status is inconsistent")
        span = role.get("span")
        if span is not None:
            probe = {**row, "_role_span": span}
            _validate_span(probe, "_role_span")
            claim_span = row["claim_span"]
            if _span_within(claim_span, span):
                _validate_contained_span(claim_span, span, f"{name} role")
            elif name not in _SOURCE_ROLES or span != row.get("speaker_span"):
                raise ValueError("ClaimFrame proposition role is outside its claim evidence")
        ordered[name] = role

    for name, role in role_map.items():
        if not isinstance(name, str) or not isinstance(role, Mapping) \
                or role.get("role") != name or ordered.get(name) != role:
            raise ValueError("ClaimFrame indexed proposition role is inconsistent")

    speaker = row["speaker"]
    speaker_span = row.get("speaker_span")
    proposition = row["proposition"]
    proposition_span = row["proposition_span"]
    addressee = row.get("addressee")
    addressee_span = row.get("addressee_span")
    expected = {
        "speaker": _role("speaker", speaker, speaker_span, identity=speaker.casefold()),
        "source": _role("source", speaker, speaker_span, identity=speaker.casefold()),
        "proposition": _role(
            "proposition", proposition, proposition_span, identity=row["proposition_id"]
        ),
        "addressee": _role(
            "addressee",
            addressee,
            addressee_span,
            identity=addressee.casefold() if addressee else None,
        ),
    }
    for name, required in expected.items():
        if role_map.get(name) != required:
            raise ValueError(f"ClaimFrame {name} role conflicts with its core evidence")


def _validate_legacy_frame(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    fingerprint = row.pop("fingerprint", None)
    if fingerprint != content_fingerprint(row):
        raise ValueError("ClaimFrame fingerprint mismatch")
    # V1 replay predates explicit ingress/ceiling fields.  When present they must still be
    # recognition-only; absence is historical compatibility, never a fresh authority grant.
    if row.get("authority_ceiling", "recognition_only") != "recognition_only" \
            or row.get("ingress", "player") not in _INGRESS:
        raise ValueError("legacy ClaimFrame authority is invalid")
    if any(row.get(key) is not False for key in (
        "establishes_truth", "establishes_occurrence", "authorizes_mechanics",
        "admits_world_event",
    )):
        raise ValueError("ClaimFrame may only carry recognition authority")
    for key in ("claim_span", "governor_span", "proposition_span"):
        span = row.get(key)
        if not isinstance(span, Mapping) or set(span) != {"start", "end"}:
            raise ValueError(f"ClaimFrame {key} is malformed")
        if isinstance(span["start"], bool) or isinstance(span["end"], bool) \
                or not isinstance(span["start"], int) or not isinstance(span["end"], int) \
                or span["end"] <= span["start"]:
            raise ValueError(f"ClaimFrame {key} is invalid")
    return deepcopy(dict(value))


def validate_claim_frame(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("ClaimFrame must be an object")
    if value.get("schema") == LEGACY_CLAIM_FRAME_SCHEMA:
        return _validate_legacy_frame(value)
    if value.get("schema") != CLAIM_FRAME_SCHEMA:
        raise ValueError("unsupported ClaimFrame schema")
    row = deepcopy(dict(value))
    fingerprint = row.pop("fingerprint", None)
    if fingerprint != content_fingerprint(row):
        raise ValueError("ClaimFrame fingerprint mismatch")
    row["fingerprint"] = fingerprint
    if row.get("ingress") not in _INGRESS:
        raise ValueError("ClaimFrame ingress is forged")
    if row.get("authority_ceiling") != "recognition_only":
        raise ValueError("ClaimFrame authority ceiling is forged")
    if any(row.get(key) is not False for key in (
        "establishes_truth", "establishes_occurrence", "authorizes_mechanics",
        "admits_world_event",
    )):
        raise ValueError("ClaimFrame may only carry recognition authority")
    if not _SHA.fullmatch(str(row.get("source_fingerprint") or "")) \
            or not _SHA.fullmatch(str(row.get("fabric_fingerprint") or "")) \
            or not _SHA.fullmatch(str(row.get("meaning_ref") or "")):
        raise ValueError("ClaimFrame semantic lineage is malformed")
    if isinstance(row.get("source_length"), bool) or not isinstance(row.get("source_length"), int) \
            or row["source_length"] <= 0:
        raise ValueError("ClaimFrame source length is invalid")
    for key in ("claim_span", "governor_span", "proposition_span"):
        _validate_span(row, key)
    for key in ("quotation_span", "speaker_span", "addressee_span", "condition_span"):
        _validate_span(row, key, optional=True)
    claim = row["claim_span"]
    governor = row["governor_span"]
    prop = row["proposition_span"]
    expected_polarity = normalized_proposition(str(prop.get("text")))[1]
    if row.get("governor") != governor.get("text"):
        raise ValueError("ClaimFrame governor conflicts with its exact span")
    if row.get("proposition_polarity") != expected_polarity \
            or row.get("proposition") != prop.get("text") \
            or row.get("proposition_id") != _claim_proposition_id(
                str(prop.get("text")), expected_polarity
            ) \
            or row.get("proposition_identity") != proposition_id(prop.get("text")):
        raise ValueError("ClaimFrame proposition identity is invalid")
    _validate_contained_span(claim, governor, "governor")
    _validate_contained_span(claim, prop, "proposition")
    quotation = row.get("quotation_span")
    if quotation is not None:
        _validate_contained_span(claim, quotation, "quotation")
        _validate_contained_span(quotation, prop, "quoted proposition")
    for label in ("speaker", "addressee", "condition"):
        span = row.get(f"{label}_span")
        if span is not None and _span_within(claim, span):
            _validate_contained_span(claim, span, label)
    speaker = row.get("speaker")
    if not isinstance(speaker, str) or not speaker.strip() \
            or speaker != _speaker_value_from_span(row):
        raise ValueError("ClaimFrame speaker conflicts with its exact span")
    addressee = row.get("addressee")
    addressee_span = row.get("addressee_span")
    if (addressee_span is None and addressee is not None) \
            or (addressee_span is not None and addressee != addressee_span.get("text")):
        raise ValueError("ClaimFrame addressee conflicts with its exact span")
    condition_span = row.get("condition_span")
    condition = row.get("condition")
    if not isinstance(condition, Mapping) \
            or (condition_span is not None and condition.get("value") != condition_span.get("text")):
        raise ValueError("ClaimFrame condition conflicts with its exact span")
    _validate_role_projection(row)
    evidence = row.get("evidence_spans")
    evidence_names = {
        "claim", "governor", "proposition", "quotation",
        "speaker", "addressee", "condition", "time",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != evidence_names:
        raise ValueError("ClaimFrame evidence spans are malformed")
    for name, ref in evidence.items():
        if ref is None:
            if name != "time" and row.get(f"{name}_span") is not None:
                raise ValueError(f"ClaimFrame {name} evidence is missing")
            continue
        if not isinstance(ref, Mapping) or set(ref) != {"span", "text", "fingerprint"} \
                or not isinstance(ref.get("span"), Mapping) \
                or set(ref["span"]) != {"start", "end"}:
            raise ValueError(f"ClaimFrame {name} evidence is invalid")
        bounds = ref["span"]
        if isinstance(bounds.get("start"), bool) or isinstance(bounds.get("end"), bool) \
                or not isinstance(bounds.get("start"), int) or not isinstance(bounds.get("end"), int) \
                or not 0 <= bounds["start"] < bounds["end"] <= row["source_length"]:
            raise ValueError(f"ClaimFrame {name} evidence is invalid")
        payload = {"span": dict(bounds), "text": ref.get("text")}
        if ref.get("fingerprint") != content_fingerprint(payload):
            raise ValueError(f"ClaimFrame {name} evidence fingerprint mismatch")
        if name != "time":
            top = row.get(f"{name}_span")
            if top is None or bounds["start"] != top["start"] or bounds["end"] != top["end"] \
                    or ref.get("text") != top.get("text"):
                raise ValueError(f"ClaimFrame {name} evidence does not match its span")
    if not isinstance(row.get("ambiguity"), list) or not isinstance(row.get("unresolved_roles"), list):
        raise ValueError("ClaimFrame ambiguity metadata is malformed")
    if not isinstance(row.get("nested_attribution_depth"), int) \
            or row["nested_attribution_depth"] < 0:
        raise ValueError("ClaimFrame nested attribution depth is invalid")
    return deepcopy(dict(value))


def validate_claim_frame_against_source(value: object, source_text: str) -> dict[str, Any]:
    """Validate every retained v2 evidence span against the exact recognition source.

    ``source_text`` is the same story-visible projection used to compile the meaning receipt.
    Callers holding raw narrator output must first apply the ordinary hidden-control-tag mask;
    its length-preserving projection keeps every stored offset exact without retaining prose in
    the receipt itself.
    """
    checked = validate_claim_frame(value)
    if checked.get("schema") != CLAIM_FRAME_SCHEMA:
        raise ValueError("exact-source validation requires a v2 ClaimFrame")
    if not isinstance(source_text, str) or not source_text:
        raise ValueError("ClaimFrame exact source is required")
    if len(source_text) != checked["source_length"] \
            or content_fingerprint(source_text) != checked["source_fingerprint"]:
        raise ValueError("ClaimFrame exact source fingerprint is invalid")

    verified = 0

    def verify_spans(item: object) -> None:
        nonlocal verified
        if isinstance(item, Mapping):
            if set(item) == {"start", "end", "text", "fingerprint"}:
                start, end = item["start"], item["end"]
                if source_text[start:end] != item["text"]:
                    raise ValueError("ClaimFrame evidence conflicts with its exact source")
                verified += 1
                return
            for nested in item.values():
                verify_spans(nested)
        elif isinstance(item, list):
            for nested in item:
                verify_spans(nested)

    verify_spans(checked)
    if verified == 0:
        raise ValueError("ClaimFrame has no exact-source evidence")
    return deepcopy(checked)


def build_claim_record(
    frame: Mapping[str, Any],
    *,
    session_id: str,
    branch_id: str,
    world_id: str,
    turn: int,
    source: str = "player",
    occurrence_index: int = 1,
    visibility: str = "public",
    scoped_actors: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Seal one recognized frame to its exact branch/turn occurrence."""
    checked = validate_claim_frame(frame)
    if visibility not in _VISIBILITY:
        raise ValueError("Claim Record visibility is invalid")
    if not all(_ID.fullmatch(str(value or "")) for value in (session_id, branch_id, source)):
        raise ValueError("Claim Record ledger/source identity is invalid")
    if world_id != "world_unbound" and not _WORLD.fullmatch(str(world_id or "")):
        raise ValueError("Claim Record world identity is invalid")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0 \
            or isinstance(occurrence_index, bool) or not isinstance(occurrence_index, int) \
            or occurrence_index <= 0:
        raise ValueError("Claim Record occurrence is invalid")
    occurrence = {
        "session_id": session_id,
        "branch_id": branch_id,
        "world_id": world_id,
        "turn": turn,
        "source": source,
        "occurrence_index": occurrence_index,
        "frame_id": checked["frame_id"],
        "frame_fingerprint": checked["fingerprint"],
    }
    occurrence_id = content_fingerprint(occurrence)
    payload: dict[str, Any] = {
        "schema": CLAIM_RECORD_SCHEMA,
        "claim_id": "claim:" + occurrence_id.removeprefix("sha256:"),
        "occurrence_id": occurrence_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "world_id": world_id,
        "turn": turn,
        "source": source,
        "occurrence_index": occurrence_index,
        "visibility": visibility,
        "scoped_actors": sorted({str(value) for value in scoped_actors if str(value)}),
        "frame": deepcopy(checked),
        "frame_fingerprint": checked["fingerprint"],
        "proposition_id": checked["proposition_id"],
        "authority_ceiling": "recognition_only",
        "establishes_truth": False,
        "establishes_occurrence": False,
        "authorizes_mechanics": False,
        "admits_world_event": False,
    }
    payload["fingerprint"] = content_fingerprint(payload)
    return validate_claim_record(payload)


def validate_claim_record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != CLAIM_RECORD_SCHEMA:
        raise ValueError("unsupported Claim Record schema")
    row = deepcopy(dict(value))
    fingerprint = row.pop("fingerprint", None)
    if fingerprint != content_fingerprint(row):
        raise ValueError("Claim Record fingerprint mismatch")
    frame = validate_claim_frame(row.get("frame"))
    if row.get("frame_fingerprint") != frame.get("fingerprint") \
            or row.get("proposition_id") != frame.get("proposition_id"):
        raise ValueError("Claim Record frame lineage is invalid")
    if row.get("authority_ceiling") != "recognition_only" \
            or any(row.get(key) is not False for key in (
                "establishes_truth", "establishes_occurrence", "authorizes_mechanics",
                "admits_world_event",
            )):
        raise ValueError("Claim Record recognition authority is forged")
    if row.get("visibility") not in _VISIBILITY or not isinstance(row.get("scoped_actors"), list):
        raise ValueError("Claim Record visibility is malformed")
    if not all(_ID.fullmatch(str(row.get(key) or "")) for key in ("session_id", "branch_id", "source")):
        raise ValueError("Claim Record ledger/source identity is malformed")
    if row.get("world_id") != "world_unbound" \
            and not _WORLD.fullmatch(str(row.get("world_id") or "")):
        raise ValueError("Claim Record world identity is malformed")
    occurrence = {
        "session_id": row["session_id"],
        "branch_id": row["branch_id"],
        "world_id": row["world_id"],
        "turn": row["turn"],
        "source": row["source"],
        "occurrence_index": row["occurrence_index"],
        "frame_id": frame["frame_id"],
        "frame_fingerprint": frame["fingerprint"],
    }
    expected_occurrence = content_fingerprint(occurrence)
    expected_claim = "claim:" + expected_occurrence.removeprefix("sha256:")
    if row.get("occurrence_id") != expected_occurrence or row.get("claim_id") != expected_claim:
        raise ValueError("Claim Record occurrence identity is forged")
    if isinstance(row.get("turn"), bool) or not isinstance(row.get("turn"), int) or row["turn"] < 0 \
            or isinstance(row.get("occurrence_index"), bool) \
            or not isinstance(row.get("occurrence_index"), int) or row["occurrence_index"] <= 0:
        raise ValueError("Claim Record occurrence coordinates are invalid")
    return deepcopy(dict(value))
