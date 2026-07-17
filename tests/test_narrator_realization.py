from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narrator_realization import (
    FORBIDDEN_INFERENCE_CODES,
    NARRATOR_REALIZATION_SCHEMA,
    NarratorRealizationError,
    build_narrator_realization,
    qualitative_hp_impact,
    render_narrator_realization,
    validate_narrator_realization,
)


FRAME = content_fingerprint({"frame": "f1"})
MEANING = content_fingerprint({"meaning": "m1"})
ALIGNMENT = content_fingerprint({"alignment": "polehammer"})


def _empty_object() -> dict:
    return {
        "object_kind_id": None,
        "linguistic_possessor_id": None,
        "resolved_instance_ids": [],
        "proven_owner_id": None,
        "part_id": None,
        "alignment_status": "none",
        "alignment_ref": None,
        "candidate_instance_ids": [],
    }


def _meaning(*, kind: str = "settled") -> dict:
    ambiguity = ["meridian_pierce", "weapon_attack"] if kind == "unresolved" else []
    embedded = kind == "attributed"
    return {
        "meaning_ref": MEANING,
        "actor_id": "player",
        "capability_id": None if ambiguity else "weapon_attack",
        "invoked_capability_ids": [],
        "action_class": "weapon_attack",
        "target_entity_id": "toll_marshal_iven",
        "object_relation": ({
            "object_kind_id": "polehammer",
            "linguistic_possessor_id": "toll_marshal_iven",
            "resolved_instance_ids": ["iven_polehammer"],
            "proven_owner_id": "toll_marshal_iven",
            "part_id": None,
            "alignment_status": "positive",
            "alignment_ref": ALIGNMENT,
            "candidate_instance_ids": [],
        } if kind == "settled" else _empty_object()),
        "target_locus": "flank" if kind == "settled" else None,
        "target_locus_owner_id": "toll_marshal_iven" if kind == "settled" else None,
        "assertion_status": "ambiguous" if ambiguity else ("embedded" if embedded else "asserted"),
        "embedding_kind": "reported_or_testified" if embedded else "none",
        "holder_role": "speaker" if embedded else "none",
        "holder_entity_id": "mara_voss" if embedded else None,
        "holder_candidates": [],
        "polarity": "positive",
        "modality": "actual",
        "time_scope": "current",
        "ambiguity_candidate_ids": ambiguity,
        "performance_mode": "context_only" if embedded else (
            "unresolved_do_not_select" if ambiguity else "may_perform"
        ),
    }


def _settled(event_ref: str = "settlement.f1") -> dict:
    return {
        "event_ref": event_ref,
        "adapter_id": "narrator.weapon-attack/1",
        "frame_ref": FRAME,
        "event_meaning": _meaning(kind="settled"),
        "outcome_quality": "crit_success",
        "impact_kind": "harm",
        "impact_magnitude": "modest",
        "target_state": "active",
        "settled_change_kinds": ["hp"],
    }


def _unresolved(event_ref: str = "event.f2") -> dict:
    return {
        "event_ref": event_ref,
        "frame_ref": FRAME,
        "event_meaning": _meaning(kind="unresolved"),
        "reason": "semantic_ambiguity",
        "allowed_stage": "attempt_only",
    }


def _attributed(event_ref: str = "event.f3") -> dict:
    return {
        "event_ref": event_ref,
        "frame_ref": FRAME,
        "event_meaning": _meaning(kind="attributed"),
    }


def test_build_is_canonical_content_free_and_fingerprinted():
    packet = build_narrator_realization(
        9,
        asserted_settled=[_settled("settlement.z"), _settled("settlement.a")],
        asserted_unresolved=[_unresolved()],
        attributed_noncurrent=[_attributed()],
        forbidden_inference=[
            {"scope_ref": "turn:9", "code": "no_unstated_player_action"},
            {"scope_ref": "event.f2", "code": "unresolved_candidates_must_not_be_selected"},
        ],
    )

    assert packet["schema"] == NARRATOR_REALIZATION_SCHEMA
    assert [row["event_ref"] for row in packet["asserted_settled"]] == [
        "settlement.a",
        "settlement.z",
    ]
    payload = {key: value for key, value in packet.items() if key != "fingerprint"}
    assert packet["fingerprint"] == content_fingerprint(payload)
    assert validate_narrator_realization(packet) == packet
    assert "damage" not in packet["asserted_settled"][0]
    assert "hp" not in packet["asserted_settled"][0]
    assert "priority" not in packet


def test_render_leads_with_plain_exact_outcome_boundaries_before_machine_packet():
    packet = build_narrator_realization(
        9,
        asserted_settled=[_settled()],
        asserted_unresolved=[_unresolved()],
    )

    rendered = render_narrator_realization(packet)

    assert "PLAIN OUTCOME CONTRACT:" in rendered
    assert (
        "exact target toll_marshal_iven; impact harm/modest; target state active"
        in rendered
    )
    assert "Affect no other target" in rendered
    assert "UNRESOLVED (semantic_ambiguity)" in rendered
    assert (
        "capability Unknown Capability; exact referent toll_marshal_iven; "
        "allowed stage attempt_only"
        in rendered
    )
    assert "It caused no hit, damage, injury" in rendered
    assert rendered.index("PLAIN OUTCOME CONTRACT:") < rendered.index('"schema":"narrator-realization/1"')


@pytest.mark.parametrize(
    ("delta", "maximum", "defeated", "expected"),
    [
        (0, 100, False, "none"),
        (-15, 100, False, "modest"),
        (-16, 100, False, "solid"),
        (-35, 100, False, "solid"),
        (-36, 100, False, "severe"),
        (-65, 100, False, "severe"),
        (-66, 100, False, "devastating"),
        (-1, 100, True, "decisive"),
        (-2, 14, False, "modest"),
    ],
)
def test_qualitative_hp_impact_has_exact_boundaries(delta, maximum, defeated, expected):
    assert qualitative_hp_impact(delta, maximum, defeated=defeated) == expected


@pytest.mark.parametrize(
    ("delta", "maximum", "defeated"),
    [
        (True, 10, False),
        (1, 10, False),
        (-1, 0, False),
        (-11, 10, False),
        (0, 10, True),
        (-1, 10, 1),
    ],
)
def test_qualitative_hp_impact_rejects_non_settled_inputs(delta, maximum, defeated):
    with pytest.raises(NarratorRealizationError):
        qualitative_hp_impact(delta, maximum, defeated=defeated)


def test_validation_is_strict_and_detects_fingerprint_tampering():
    packet = build_narrator_realization(3, asserted_settled=[_settled()])

    extra = deepcopy(packet)
    extra["layer_priority"] = "semantic"
    with pytest.raises(NarratorRealizationError, match="unexpected fields"):
        validate_narrator_realization(extra)

    inner_extra = deepcopy(packet)
    inner_extra["asserted_settled"][0]["damage"] = 2
    with pytest.raises(NarratorRealizationError, match="unexpected fields"):
        validate_narrator_realization(inner_extra)

    changed = deepcopy(packet)
    changed["asserted_settled"][0]["event_meaning"]["target_locus"] = "ribs"
    with pytest.raises(NarratorRealizationError, match="fingerprint mismatch"):
        validate_narrator_realization(changed)


def test_entity_fields_admit_only_one_positive_cohort_ordinal_suffix():
    settled = _settled()
    settled["event_meaning"]["target_entity_id"] = "baser_hollow#21"
    packet = build_narrator_realization(3, asserted_settled=[settled])

    assert validate_narrator_realization(packet) == packet

    for invalid in ("baser_hollow#0", "baser_hollow#01", "baser_hollow#2#3"):
        forged = deepcopy(packet)
        forged["asserted_settled"][0]["event_meaning"]["target_entity_id"] = invalid
        with pytest.raises(NarratorRealizationError, match="entity reference"):
            validate_narrator_realization(forged)


def test_forbidden_inference_is_a_closed_machine_vocabulary():
    assert FORBIDDEN_INFERENCE_CODES == (
        "only_realized_changes_may_be_world_changes",
        "no_receipt_no_outcome",
        "object_ownership_unproven",
        "unresolved_candidates_must_not_be_selected",
        "attributed_content_is_not_world_truth",
        "pending_event_has_no_impact",
        "no_unstated_player_action",
        "mechanic_numbers_are_not_story_language",
    )
    with pytest.raises(NarratorRealizationError, match="code is invalid"):
        build_narrator_realization(
            3,
            forbidden_inference=[
                {"scope_ref": "turn:3", "code": "probably_do_not_invent_anything"}
            ],
        )


def test_one_event_cannot_appear_in_multiple_truth_buckets():
    with pytest.raises(NarratorRealizationError, match="unique across"):
        build_narrator_realization(
            3,
            asserted_settled=[_settled("event.same")],
            asserted_unresolved=[_unresolved("event.same")],
        )


def test_direct_current_context_cannot_be_misfiled_as_attributed():
    row = _attributed()
    row["event_meaning"].update(
        {
            "assertion_status": "asserted",
            "embedding_kind": "none",
            "holder_role": "none",
            "holder_entity_id": None,
            "performance_mode": "may_perform",
        }
    )
    with pytest.raises(NarratorRealizationError, match="belongs in an asserted bucket"):
        build_narrator_realization(3, attributed_noncurrent=[row])


def test_pending_intent_can_only_be_a_visible_tell_without_impact():
    pending = _unresolved("intent.enemy.1")
    pending.update({
        "frame_ref": None,
        "reason": "pending_intent",
        "allowed_stage": "visible_tell_only",
    })
    pending["event_meaning"].update({
        "meaning_ref": None,
        "actor_id": "toll_marshal_iven",
        "capability_id": "polehammer_sweep",
        "ambiguity_candidate_ids": [],
        "assertion_status": "asserted",
        "performance_mode": "may_perform",
    })
    packet = build_narrator_realization(
        4,
        asserted_unresolved=[pending],
        forbidden_inference=[
            {"scope_ref": "intent.enemy.1", "code": "pending_event_has_no_impact"}
        ],
    )
    assert packet["asserted_unresolved"][0]["allowed_stage"] == "visible_tell_only"

    pending["allowed_stage"] = "attempt_only"
    with pytest.raises(NarratorRealizationError, match="does not match"):
        build_narrator_realization(4, asserted_unresolved=[pending])


def test_impact_and_defeat_fields_cannot_disagree():
    row = _settled()
    row.update({"impact_magnitude": "decisive", "target_state": "active"})
    with pytest.raises(NarratorRealizationError, match="must agree"):
        build_narrator_realization(3, asserted_settled=[row])

    row.update({"impact_kind": "none", "impact_magnitude": "modest"})
    with pytest.raises(NarratorRealizationError, match="without harm"):
        build_narrator_realization(3, asserted_settled=[row])
