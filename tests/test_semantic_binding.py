from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.semantic import ACTION_FRAME_SCHEMA, ActionFrame, validate_action_frame_snapshot
from aetherstate.semantic_binding import (
    SemanticBindingError,
    action_classes_for_matches,
    build_meaning_binding,
    build_possessed_object_alignment,
    semantic_match_ref,
    validate_action_match_contract,
    validate_meaning_binding,
    validate_world_alignment,
)
from aetherstate.semantic_fabric import load_default_semantic_fabric


IVEN = "toll_marshal_iven"
MARA = "mara_voss"


def _report_scope(match, source: str) -> dict:
    ref = semantic_match_ref(match)
    return {
        "scope_ref": "scope.report.1",
        "kind": "reported_content",
        "span_start": match.start,
        "span_end": len(source),
        "content_start": match.end,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "content",
        "evidence_refs": [ref],
    }


def _report_constraint(match) -> dict:
    return {
        "constraint_id": "constraint.report.1",
        "scope_ref": "scope.report.1",
        "target_event_ref": "event.f1",
        "dimension": "assertion_context",
        "value": "reported",
        "evidence_refs": [semantic_match_ref(match)],
    }


def test_action_adapter_is_closed_and_ignores_free_form_mechanic_selection():
    fabric = load_default_semantic_fabric()
    meaning = fabric.translate("I inspect the polehammer and strike Iven.")
    action_matches = meaning.for_lex("action")

    assert action_classes_for_matches(action_matches) == ("inspection", "weapon_attack")
    inspect = next(match for match in action_matches if match.concept_id == "action.inspect")
    assert validate_action_match_contract(inspect) == "inspection"

    corrupt = inspect.as_dict()
    corrupt["features"] = {"action_class": "weapon_attack", "damage_not_implied": True}
    with pytest.raises(SemanticBindingError, match="contract changed"):
        validate_action_match_contract(corrupt)


def test_reported_event_binding_preserves_recognition_and_derives_nonexecution():
    source = "I report that I use Rope-Dart Meridian Pierce to strike Iven."
    meaning = load_default_semantic_fabric().translate(source)
    report = next(
        match for match in meaning.for_lex("scene")
        if match.concept_id == "scene.discourse.report"
    )
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(attack.start, len(source)),
        scope_nodes=[_report_scope(report, source)],
        constraints=[_report_constraint(report)],
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(attack)],
        }],
        role_evidence=[{
            "role": "action",
            "evidence_refs": [semantic_match_ref(attack)],
        }],
    )

    assert binding["request_disposition"] == "attributed"
    assert binding["mechanic_disposition"] == "recognition_only"
    assert binding["reason_refs"] == ["constraint.report.1"]
    assert validate_meaning_binding(
        binding, meaning_receipt=meaning.receipt_dict(),
    ) == binding


def test_binding_rejects_uncommitted_evidence_and_derives_conflict_or_hold():
    source = "I strike Iven."
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(match for match in meaning.for_lex("action")
                  if match.concept_id == "action.weapon_attack")
    fake = semantic_match_ref(attack)
    fake["entry_fingerprint"] = "sha256:" + "0" * 64
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(0, len(source)),
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [fake],
        }],
    )
    with pytest.raises(SemanticBindingError, match="absent from its receipt"):
        validate_meaning_binding(binding, meaning_receipt=meaning.receipt_dict())

    scope = {
        "scope_ref": "scope.uncertain.1",
        "kind": "scope_candidate",
        "span_start": 0,
        "span_end": len(source),
        "content_start": 0,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "unresolved",
        "evidence_refs": [semantic_match_ref(attack)],
    }
    constraint = {
        "constraint_id": "constraint.scope.1",
        "scope_ref": "scope.uncertain.1",
        "target_event_ref": "event.f1",
        "dimension": "scope",
        "value": "unresolved",
        "evidence_refs": [semantic_match_ref(attack)],
    }
    held = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(0, len(source)),
        scope_nodes=[scope],
        constraints=[constraint],
    )
    conflict = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(0, len(source)),
        scope_nodes=[scope],
        constraints=[constraint],
        constraint_integrity="conflict",
    )
    assert held["mechanic_disposition"] == "hold_unresolved"
    assert conflict["mechanic_disposition"] == "invalid_scope_conflict"


def _worlds() -> dict[str, dict]:
    return {
        "W0": {"entities": {IVEN: {"name": "Iven"}}, "meta": {"turn": 4}},
        "W1": {
            "entities": {IVEN: {"name": "Iven"}},
            "items": {
                "polehammer_b": {"name": "polehammer", "owner": IVEN},
                "polehammer_a": {"name": "polehammer", "owner": IVEN},
            },
            "meta": {"turn": 4},
        },
        "W2": {
            "entities": {IVEN: {"name": "Iven"}, MARA: {"name": "Mara"}},
            "items": {"mara_polehammer": {"name": "polehammer", "owner": MARA}},
            "meta": {"turn": 4},
        },
        "W3": {
            "entities": {IVEN: {"name": "Iven"}},
            "items": {"iven_polehammer": {"name": "polehammer", "owner": IVEN}},
            "meta": {"turn": 4},
        },
    }


def test_four_valued_world_alignment_is_explicit_and_order_invariant():
    recognition_ref = "sha256:" + "1" * 64
    observed = {
        label: build_possessed_object_alignment(
            deepcopy(world),
            recognition_ref=recognition_ref,
            object_name="polehammer",
            linguistic_possessor_id=IVEN,
        )
        for label, world in _worlds().items()
    }

    assert observed["W0"]["status"] == "uncheckable"
    assert observed["W1"]["status"] == "unresolved"
    assert observed["W1"]["candidate_ids"] == ["polehammer_a", "polehammer_b"]
    assert observed["W2"]["status"] == "false"
    assert observed["W2"]["resolved_ids"] == ["mara_polehammer"]
    assert observed["W2"]["positive_authority_value"] is None
    assert observed["W3"]["status"] == "positive"
    assert observed["W3"]["resolved_ids"] == ["iven_polehammer"]
    assert observed["W3"]["positive_authority_value"] == IVEN
    assert all(validate_world_alignment(row) == row for row in observed.values())

    reversed_w1 = deepcopy(_worlds()["W1"])
    reversed_w1["items"] = dict(reversed(tuple(reversed_w1["items"].items())))
    reordered = build_possessed_object_alignment(
        reversed_w1,
        recognition_ref=recognition_ref,
        object_name="polehammer",
        linguistic_possessor_id=IVEN,
    )
    assert reordered == observed["W1"]


@pytest.mark.parametrize("time_scope", ["past", "future"])
def test_noncurrent_alignment_never_projects_current_item_authority(time_scope: str):
    alignment = build_possessed_object_alignment(
        _worlds()["W3"],
        recognition_ref="sha256:" + "1" * 64,
        object_name="polehammer",
        linguistic_possessor_id=IVEN,
        time_scope=time_scope,
    )

    assert alignment["status"] == "uncheckable"
    assert alignment["candidate_ids"] == []
    assert alignment["resolved_ids"] == []
    assert alignment["positive_authority_value"] is None


def test_action_frame_v3_cites_one_event_binding_and_canonical_alignments():
    source = "I strike Iven with Iven's polehammer."
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(match for match in meaning.for_lex("action")
                  if match.concept_id == "action.weapon_attack")
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.f1",
        event_node_id="event.f1",
        event_span=(attack.start, len(source)),
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(attack)],
        }],
    )
    alignment = build_possessed_object_alignment(
        _worlds()["W3"],
        recognition_ref=binding["fingerprint"],
        object_name="polehammer",
        linguistic_possessor_id=IVEN,
    )
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=attack.start,
        end=len(source),
        actor_id="player",
        capability_id="meridian_pierce",
        action_class="weapon_attack",
        target_entity_id=IVEN,
        possessed_object="polehammer",
        linguistic_possessor_id=IVEN,
        possessed_object_instance_id="iven_polehammer",
        possessed_object_owner_id=IVEN,
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id="event.f1",
        world_alignment_refs=(alignment["fingerprint"],),
        mechanic_disposition=binding["mechanic_disposition"],
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")

    snapshot = frame.snapshot(source)

    assert snapshot["schema"] == ACTION_FRAME_SCHEMA
    assert snapshot["meaning_binding_ref"] == binding["fingerprint"]
    assert snapshot["event_node_id"] == "event.f1"
    assert snapshot["world_alignment_refs"] == [alignment["fingerprint"]]
    assert frame.mechanically_actionable
    assert validate_action_frame_snapshot(snapshot) == snapshot

    frame.mechanic_disposition = "recognition_only"
    assert not frame.mechanically_actionable
