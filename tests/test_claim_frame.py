from __future__ import annotations

import copy

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.claim_frame import (
    CLAIM_FRAME_SCHEMA,
    LEGACY_CLAIM_FRAME_SCHEMA,
    build_claim_frames,
    build_claim_record,
    validate_claim_frame,
    validate_claim_frame_against_source,
    validate_claim_record,
)
from aetherstate.semantic_fabric import load_default_semantic_fabric


def _frames(text: str):
    fabric = load_default_semantic_fabric()
    return build_claim_frames(text, fabric.translate(text))


def _rehash(row: dict) -> dict:
    row["fingerprint"] = content_fingerprint({key: value for key, value in row.items() if key != "fingerprint"})
    return row


def test_claim_classes_preserve_spans_and_never_promote_truth() -> None:
    cases = {
        "Mara asserted that the gate is open.": "assertion",
        "Mara denied that the gate is open.": "denial",
        "Mara reported that the gate is open.": "report",
        "Mara believes that the gate is open.": "belief",
        "Mara promised that the gate will open.": "promise",
        "Mara asked whether the gate is open.": "question",
    }
    for text, claim_class in cases.items():
        rows = _frames(text)
        assert len(rows) == 1
        row = rows[0]
        assert row["schema"] == CLAIM_FRAME_SCHEMA
        assert row["claim_class"] == claim_class
        assert text[row["governor_span"]["start"] : row["governor_span"]["end"]] == row["governor"]
        assert text[row["proposition_span"]["start"] : row["proposition_span"]["end"]] == row["proposition"]
        assert row["speaker"] == "Mara"
        assert row["establishes_truth"] is False
        assert row["admits_world_event"] is False
        assert validate_claim_frame(row) == row


def test_claim_polarity_modality_and_conflicting_truth_remain_recognition_only() -> None:
    positive = _frames("Mara asserted that the gate is open.")[0]
    negative = _frames("Vosk denied that the gate is not open.")[0]
    assert positive["proposition_id"] != negative["proposition_id"]
    assert positive["proposition_polarity"] == "positive"
    assert negative["proposition_polarity"] == "negative"
    assert negative["speech_act_polarity"] == "negative"
    assert negative["semantic_features"]["proposition_polarity"]["value"] == "negative"
    assert negative["semantic_features"]["speech_act_polarity"]["value"] == "negative"
    assert positive["authority_ceiling"] == negative["authority_ceiling"] == "recognition_only"


def test_quotation_condition_time_quantification_and_roles_are_exact() -> None:
    text = 'Mara quoted "every gate opens tomorrow if the bell rings".'
    row = _frames(text)[0]
    assert row["claim_class"] == "quotation"
    assert row["quotation_span"] is not None
    assert row["condition_span"] is not None
    assert text[row["proposition_span"]["start"] : row["proposition_span"]["end"]] == (
        "every gate opens tomorrow if the bell rings"
    )
    assert row["semantic_features"]["time"]["value"] == "tomorrow"
    assert row["semantic_features"]["quantification"]["value"] == "every"
    assert {role["role"] for role in row["proposition_roles"]} >= {
        "content", "speaker", "subject_candidate", "predicate"
    }


def test_nested_attribution_has_two_governors_correct_speakers_and_parent() -> None:
    text = "Mara reported that Vosk denied that the gate is open."
    rows = _frames(text)
    assert [row["claim_class"] for row in rows] == ["report", "denial"]
    outer, inner = rows
    assert outer["speaker"] == "Mara"
    assert inner["speaker"] == "Vosk"
    assert outer["parent_claim_ref"] is None
    assert outer["nested_attribution_depth"] == 0
    assert inner["parent_claim_ref"] == outer["frame_id"]
    assert inner["nested_attribution_depth"] == 1
    assert outer["proposition_span"]["start"] <= inner["governor_span"]["start"]
    assert not any(row["claim_concept_id"].startswith("claim.frame.") for row in rows)


def test_addressee_is_separate_from_proposition() -> None:
    text = "Mara told Ryn that the gate might open tomorrow."
    row = _frames(text)[0]
    assert row["speaker"] == "Mara"
    assert row["addressee"] == "Ryn"
    assert text[row["addressee_span"]["start"] : row["addressee_span"]["end"]] == "Ryn"
    assert row["proposition"] == "the gate might open tomorrow"
    assert row["semantic_features"]["modality"]["value"] == "might"


@pytest.mark.parametrize(
    ("text", "speaker", "addressee", "proposition"),
    (
        (
            "Essik told me he got it from his cousin, Marek.",
            "Essik",
            "me",
            "he got it from his cousin, Marek",
        ),
        (
            "Marek told Essik it wasn't just wet stone.",
            "Marek",
            "Essik",
            "it wasn't just wet stone",
        ),
        (
            "Marek told Warden Ivara the eastern grate was wet.",
            "Marek",
            "Warden Ivara",
            "the eastern grate was wet",
        ),
    ),
)
def test_bare_tell_recipient_is_not_swallowed_by_proposition(
    text: str,
    speaker: str,
    addressee: str,
    proposition: str,
) -> None:
    row = _frames(text)[0]
    assert row["speaker"] == speaker
    assert row["addressee"] == addressee
    assert row["addressee_span"]["text"] == addressee
    assert row["proposition"] == proposition
    assert row["proposition_span"]["text"] == proposition
    assert row["establishes_truth"] is False


def test_narrator_nested_report_sentences_keep_recipients_out_of_claim_content() -> None:
    text = (
        "Essik told me he got it from his cousin, Marek, who runs a water-cart. "
        "Marek told Essik it wasn't just wet stone."
    )
    fabric = load_default_semantic_fabric()
    rows = build_claim_frames(
        text,
        fabric.translate(text),
        ingress="narrator",
        source_id="narrator",
    )
    assert [(row["speaker"], row["addressee"], row["proposition"]) for row in rows] == [
        ("Essik", "me", "he got it from his cousin, Marek, who runs a water-cart"),
        ("Marek", "Essik", "it wasn't just wet stone"),
    ]
    assert all(row["nested_attribution_depth"] == 0 for row in rows)
    assert all(row["establishes_truth"] is False for row in rows)


def test_nominal_promise_mentions_are_not_fresh_promise_occurrences() -> None:
    for text in (
        "Your promise to this Guild, investigator.",
        "It is a promise, not a proven fact.",
        "Ryn has heard many promises and is waiting to learn what this one costs.",
        "It is a promise that costs him dearly.",
        "The report that the gate is open remains unproven.",
    ):
        assert _frames(text) == [], text


def test_a_report_noun_inside_a_truth_disclaimer_is_not_a_reporting_act() -> None:
    assert _frames("I do not claim either report is proven.") == []


def test_possessive_attribution_normalizes_speaker_without_forging_its_span() -> None:
    text = "Ryn's report that the Iron Pact may seal the gate tonight."
    row = _frames(text)[0]
    assert row["speaker"] == "Ryn"
    assert row["speaker_span"]["text"] == "Ryn's"
    assert text[row["speaker_span"]["start"] : row["speaker_span"]["end"]] == "Ryn's"


def test_direct_and_inverted_quoted_dialogue_build_exact_attributed_assertions() -> None:
    direct = 'Selene says, "The East Gate is shut."'
    inverted = '"The East Gate is shut," Selene says.'
    direct_meaning = load_default_semantic_fabric().translate(direct)
    inverted_meaning = load_default_semantic_fabric().translate(inverted)

    direct_row = build_claim_frames(direct, direct_meaning)[0]
    inverted_row = build_claim_frames(inverted, inverted_meaning)[0]

    for text, meaning, row in (
        (direct, direct_meaning, direct_row),
        (inverted, inverted_meaning, inverted_row),
    ):
        dialogue_matches = [
            match for match in meaning.matches
            if match.lex_id == "claim" and match.surface_baseline == "dialogue_construction"
        ]
        assert len(dialogue_matches) == 1
        assert row["claim_class"] == "assertion"
        assert row["speaker"] == "Selene"
        assert row["speaker_span"]["text"] == "Selene"
        assert row["governor"] == "says"
        assert row["quotation_span"] is not None
        assert row["ambiguity"] == []
        assert row["unresolved_roles"] == []
        for key in ("claim_span", "speaker_span", "governor_span", "proposition_span",
                    "quotation_span"):
            span = row[key]
            assert text[span["start"]:span["end"]] == span["text"]
        assert row["authority_ceiling"] == "recognition_only"
        assert row["establishes_truth"] is False
        assert row["establishes_occurrence"] is False
    assert direct_row["proposition_identity"] == inverted_row["proposition_identity"]


def test_narrator_unresolved_speaker_inside_dialogue_abstains() -> None:
    text = (
        'Talin meets your eyes. "Stormtide 7," he says. '
        '"The other figure was moving — gesturing, I think, though I could not see hands. '
        'I will not swear to it. That is everything I personally witnessed. If you ask, '
        'I will tell you I do not know."'
    )
    meaning = load_default_semantic_fabric().translate(text)

    assert build_claim_frames(
        text,
        meaning,
        ingress="narrator",
        source_id="Glasswake Tribunal",
    ) == []


def test_narrator_determiner_before_nominal_tell_is_not_a_speaker() -> None:
    text = "The tell is clear: everything is committed to the next motion."
    meaning = load_default_semantic_fabric().translate(text)

    assert build_claim_frames(
        text,
        meaning,
        ingress="narrator",
        source_id="narrator",
    ) == []


@pytest.mark.parametrize(
    ("text", "claim_class", "tense", "evidential", "stance"),
    (
        ("Rumor has it that the gate is open.", "hearsay", "present_or_unspecified",
         "indirect", "relays_hearsay"),
        ("Mara remembered that the gate was open.", "memory", "past", "unspecified",
         "remembers"),
        ("Mara observed that the gate was open.", "observation", "past",
         "direct_claimed", "reports_observation"),
        ("Mara inferred that the gate must be open.", "inference",
         "present_or_unspecified", "derived", "infers"),
        ("Mara predicted that the gate will open tomorrow.", "prediction", "future",
         "unspecified", "predicts"),
        ("Mara planned to open the gate tomorrow.", "plan", "present_or_unspecified",
         "unspecified", "intends"),
        ("Mara hypothesized that the gate might open if the bell rang.", "hypothesis",
         "present_or_unspecified", "unspecified", "hypothesizes"),
        ("Mara alleged that Vosk opened the gate.", "accusation",
         "present_or_unspecified", "unspecified", "accuses"),
        ("Vosk lied about the gate being open.", "deception_description",
         "present_or_unspecified", "unspecified", "attributes_deception"),
    ),
)
def test_remaining_claim_classes_keep_typed_features_and_zero_authority(
    text: str,
    claim_class: str,
    tense: str,
    evidential: str,
    stance: str,
) -> None:
    rows = _frames(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_class"] == claim_class
    assert row["tense"] == tense
    assert row["evidential_source"]["value"] == evidential
    assert row["speaker_stance"]["value"] == stance
    assert row["authority_ceiling"] == "recognition_only"
    assert row["establishes_truth"] is False
    assert row["establishes_occurrence"] is False
    assert row["authorizes_mechanics"] is False
    assert row["admits_world_event"] is False


def test_accusation_and_deception_roles_resolve_only_from_exact_attribution() -> None:
    accusation = _frames("Mara alleged that Vosk opened the gate.")[0]
    assert accusation["role_map"]["accuser"]["value"] == "Mara"
    assert accusation["role_map"]["accused_party"]["value"] == "Vosk"
    assert "accused_party" not in accusation["unresolved_roles"]

    deception = _frames("Vosk lied about the gate being open.")[0]
    assert deception["speaker"] == "player"
    assert deception["role_map"]["attributing_source"]["value"] == "player"
    assert deception["role_map"]["accused_source"]["value"] == "Vosk"
    assert deception["role_map"]["accused_source"]["span"]["text"] == "Vosk"
    assert deception["unresolved_roles"] == []

    nested = _frames("Mara alleged that someone lied about the gate being open.")
    inner = next(row for row in nested if row["claim_class"] == "deception_description")
    assert inner["speaker"] == "Mara"
    assert inner["role_map"]["attributing_source"]["value"] == "Mara"
    assert inner["role_map"]["accused_source"]["status"] == "unresolved"
    assert inner["unresolved_roles"] == ["accused_source"]


def test_claim_inside_a_quotation_stops_before_the_following_player_action() -> None:
    text = (
        'I speak carefully: "I promise the Lantern Guild that I will protect this refuge." '
        "I ask Selene to record it, then secure the doors."
    )
    rows = _frames(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_class"] == "promise"
    assert row["speaker"] == "player"
    assert row["speaker_span"]["text"] == "I"
    assert row["addressee"] == "Lantern Guild"
    assert row["proposition"] == "I will protect this refuge"
    assert "record it" not in row["claim_span"]["text"]


def test_v2_validator_rejects_forged_ceiling_bounds_and_evidence_fingerprint() -> None:
    row = _frames("Mara asserted that the gate is open.")[0]

    ceiling = _rehash({**copy.deepcopy(row), "authority_ceiling": "world_truth"})
    with pytest.raises(ValueError, match="ceiling"):
        validate_claim_frame(ceiling)

    bounds = copy.deepcopy(row)
    bounds["proposition_span"]["end"] = bounds["source_length"] + 1
    bounds["evidence_spans"]["proposition"]["span"] = copy.deepcopy(bounds["proposition_span"])
    _rehash(bounds)
    with pytest.raises(ValueError, match="invalid"):
        validate_claim_frame(bounds)

    evidence = copy.deepcopy(row)
    evidence["evidence_spans"]["governor"]["fingerprint"] = "sha256:" + "0" * 64
    _rehash(evidence)
    with pytest.raises(ValueError, match="evidence fingerprint"):
        validate_claim_frame(evidence)


def test_v2_validator_rejects_self_rehashed_core_claim_fields() -> None:
    row = _frames("Mara asserted that the gate is open.")[0]
    for field, value, reason in (
        ("speaker", "Forged Speaker", "speaker"),
        ("proposition", "the wall is shut", "proposition"),
        ("governor", "denied", "governor"),
    ):
        forged = copy.deepcopy(row)
        forged[field] = value
        _rehash(forged)
        with pytest.raises(ValueError, match=reason):
            validate_claim_frame(forged)


def test_exact_source_rejects_a_fully_rehashed_speaker_and_span() -> None:
    text = "Mara asserted that the gate is open."
    row = _frames(text)[0]
    assert validate_claim_frame_against_source(row, text) == row

    forged = copy.deepcopy(row)
    speaker_span = {**forged["speaker_span"], "text": "Vosk"}
    speaker_span["fingerprint"] = content_fingerprint("Vosk")
    forged["speaker"] = "Vosk"
    forged["speaker_span"] = speaker_span
    forged["claim_span"]["text"] = forged["claim_span"]["text"].replace(
        "Mara", "Vosk", 1
    )
    forged["claim_span"]["fingerprint"] = content_fingerprint(
        forged["claim_span"]["text"]
    )
    for name in ("speaker", "source"):
        role = forged["role_map"][name]
        role.update(value="Vosk", identity="vosk", span=copy.deepcopy(speaker_span))
        next(item for item in forged["proposition_roles"] if item["role"] == name).update(
            copy.deepcopy(role)
        )
    for name, span in (("speaker", speaker_span), ("claim", forged["claim_span"])):
        bounds = {"start": span["start"], "end": span["end"]}
        payload = {"span": bounds, "text": span["text"]}
        forged["evidence_spans"][name] = {
            **payload,
            "fingerprint": content_fingerprint(payload),
        }
    _rehash(forged)

    assert validate_claim_frame(forged) == forged
    with pytest.raises(ValueError, match="exact source"):
        validate_claim_frame_against_source(forged, text)


def test_builder_rejects_arbitrary_ingress_or_authority_ceiling() -> None:
    text = "Mara asserted that the gate is open."
    meaning = load_default_semantic_fabric().translate(text)
    with pytest.raises(ValueError, match="fixed recognition ceiling"):
        build_claim_frames(text, meaning, ingress="model_output")
    with pytest.raises(ValueError, match="fixed recognition ceiling"):
        build_claim_frames(text, meaning, authority_ceiling="world_truth")


def test_legacy_v1_frame_remains_replay_valid() -> None:
    body = {
        "schema": LEGACY_CLAIM_FRAME_SCHEMA,
        "claim_span": {"start": 0, "end": 5},
        "governor_span": {"start": 0, "end": 2},
        "proposition_span": {"start": 2, "end": 5},
        "establishes_truth": False,
        "establishes_occurrence": False,
        "authorizes_mechanics": False,
        "admits_world_event": False,
    }
    legacy = {**body, "fingerprint": content_fingerprint(body)}
    assert validate_claim_frame(legacy) == legacy


def test_claim_record_retry_is_idempotent_but_later_repeat_is_distinct() -> None:
    frame = _frames("Mara asserted that the gate is open.")[0]
    kwargs = {
        "world_id": "world_0123456789abcdef0123456789abcdef",
        "session_id": "session-1",
        "branch_id": "branch-1",
        "turn": 4,
        "source": "player",
        "visibility": "player",
    }
    first = build_claim_record(frame, **kwargs)
    retry = build_claim_record(frame, **kwargs)
    later = build_claim_record(frame, **{**kwargs, "turn": 5})
    fork = build_claim_record(frame, **{**kwargs, "branch_id": "branch-2"})
    assert first == retry
    assert first["occurrence_id"] != later["occurrence_id"]
    assert first["occurrence_id"] != fork["occurrence_id"]
    assert validate_claim_record(first) == first


def test_claim_record_rejects_rehashed_forged_occurrence_or_authority() -> None:
    frame = _frames("Mara asserted that the gate is open.")[0]
    record = build_claim_record(
        frame,
        world_id="world_0123456789abcdef0123456789abcdef",
        session_id="session-1",
        branch_id="branch-1",
        turn=4,
    )
    forged = copy.deepcopy(record)
    forged["occurrence_id"] = "sha256:" + "f" * 64
    _rehash(forged)
    with pytest.raises(ValueError, match="forged"):
        validate_claim_record(forged)

    authority = copy.deepcopy(record)
    authority["authority_ceiling"] = "world_truth"
    _rehash(authority)
    with pytest.raises(ValueError, match="forged"):
        validate_claim_record(authority)
