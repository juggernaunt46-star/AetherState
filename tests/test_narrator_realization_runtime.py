"""Runtime projection of canonical semantic journals into the final narrator packet."""
from __future__ import annotations

import json
from copy import deepcopy

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.compose import _render_directive, compose
from aetherstate.config import Config
from aetherstate.narrator_realization import build_narrator_realization_from_state
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import (
    build_meaning_binding,
    build_possessed_object_alignment,
    semantic_match_ref,
)
from aetherstate.semantic_fabric import load_default_semantic_fabric


IVEN = "toll_marshal_iven"
SOURCE = "I strike Iven with Iven's polehammer at Iven's flank."


def _semantic_state(*, owner: str = IVEN, settled: bool = True, locus: str = "flank") -> dict:
    source = f"I strike Iven with Iven's polehammer at Iven's {locus}."
    state = {
        "meta": {"turn": 7},
        "world_identity": {},
        "clock": {},
        "entities": {
            IVEN: {"name": "Iven", "present": True},
            "mara_voss": {"name": "Mara", "present": True},
        },
        "items": {
            "iven_polehammer": {"name": "polehammer", "owner": owner},
        },
    }
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
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
        state,
        recognition_ref=binding["fingerprint"],
        object_name="polehammer",
        linguistic_possessor_id=IVEN,
    )
    positive = alignment["status"] == "positive"
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=attack.start,
        end=len(source),
        actor_id="player",
        capability_id="meridian_pierce",
        invoked_capability_ids=("burst",),
        action_class="weapon_attack",
        target_entity_id=IVEN,
        target_name="Iven",
        possessed_object="polehammer",
        linguistic_possessor_id=IVEN,
        possessed_object_instance_id="iven_polehammer" if positive else None,
        possessed_object_owner_id=IVEN if positive else None,
        target_locus=locus,
        target_locus_owner_id=IVEN,
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
    locus_start = source.index(locus, source.index("polehammer") + len("polehammer"))
    frame.add_evidence("target_locus", locus_start, locus_start + len(locus), locus)
    snapshot = frame.snapshot(source)
    state.update({
        "semantic_meanings": [{"turn": 7, "meaning": meaning.receipt_dict()}],
        "semantic_frames": [{"turn": 7, "frame": snapshot}],
        "semantic_bindings": [{"turn": 7, "binding": binding}],
        "semantic_world_alignments": [{"turn": 7, "alignment": alignment}],
        "mechanic_settlements": [],
    })
    if settled:
        state["mechanic_settlements"] = [{"turn": 7, "receipt": _settlement(snapshot)}]
    return state


def _settlement(frame: dict) -> dict:
    identity = {
        "schema": "mechanic-settlement-identity/1",
        "contract_id": "weapon_attack/1",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "target_entity_id": IVEN,
    }
    receipt = {
        "schema": "mechanic-settlement/1",
        "settlement_ref": content_fingerprint(identity),
        "contract_id": "weapon_attack/1",
        "frame_ref": frame["fingerprint"],
        "meaning_ref": frame["meaning_ref"],
        "requirement_fingerprint": content_fingerprint({"requirements": "f1"}),
        "accepted_group_fingerprint": content_fingerprint({"group": "f1"}),
        "outcome": "hit",
        "outcome_quality": "success",
        "applied_changes": [
            {
                "kind": "target_admission",
                "subject_id": IVEN,
                "entity_id": IVEN,
                "post_state_ref": content_fingerprint({"combatant": IVEN}),
            },
            {
                "kind": "scene_transition",
                "subject_id": "scene",
                "post_state_ref": content_fingerprint({"combat": "opened"}),
            },
            {"kind": "hp", "subject_id": IVEN, "delta": -2, "post": 12},
            {
                "kind": "cost",
                "subject_id": "player",
                "resource_id": "spoolcharge",
                "delta": -6,
                "post": 254,
            },
            {
                "kind": "mastery",
                "subject_id": "player",
                "capability_id": "meridian_pierce",
                "delta": 1,
                "post": 4,
            },
            {
                "kind": "cooldown",
                "subject_id": "player",
                "ability_id": "meridian_pierce",
                "delta": 1,
                "post": 1,
            },
        ],
        "target_post_state": {"combatant_id": IVEN, "hp": {"cur": 12, "max": 14}},
    }
    receipt["receipt_fingerprint"] = content_fingerprint(receipt)
    return receipt


def _reported_state() -> dict:
    source = "I report that I strike Iven."
    meaning = load_default_semantic_fabric().translate(source)
    report = next(
        match for match in meaning.for_lex("scene")
        if match.concept_id == "scene.discourse.report"
    )
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
    scope = {
        "scope_ref": "scope.report.1",
        "kind": "content_scope",
        "span_start": report.start,
        "span_end": len(source),
        "content_start": attack.start,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "content",
        "evidence_refs": [semantic_match_ref(report)],
    }
    constraint = {
        "constraint_id": "constraint.report.1",
        "scope_ref": "scope.report.1",
        "target_event_ref": "event.report.1",
        "dimension": "assertion_context",
        "value": "reported",
        "evidence_refs": [semantic_match_ref(report)],
    }
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.report.1",
        event_node_id="event.report.1",
        event_span=(attack.start, len(source)),
        scope_nodes=[scope],
        constraints=[constraint],
        field_provenance=[
            {
                "field": "action_class",
                "value": "weapon_attack",
                "defaulted": False,
                "evidence_refs": [semantic_match_ref(attack)],
            },
            {
                "field": "speaker_id",
                "value": "player",
                "defaulted": False,
                "evidence_refs": [semantic_match_ref(report)],
            },
        ],
    )
    frame = ActionFrame(
        frame_id="f_report_1",
        clause_index=0,
        start=attack.start,
        end=len(source),
        actor_id="player",
        capability_id="meridian_pierce",
        action_class="weapon_attack",
        target_entity_id=IVEN,
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id="event.report.1",
        mechanic_disposition=binding["mechanic_disposition"],
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")
    return {
        "meta": {"turn": 7},
        "semantic_meanings": [{"turn": 7, "meaning": meaning.receipt_dict()}],
        "semantic_frames": [{"turn": 7, "frame": frame.snapshot(source)}],
        "semantic_bindings": [{"turn": 7, "binding": binding}],
        "semantic_world_alignments": [],
        "mechanic_settlements": [],
    }


def _qualified_state(*, polarity: str, ambiguous: bool, embedded: bool = False) -> dict:
    source = (
        "I report that I do not strike Iven."
        if embedded else "I do not strike Iven."
    )
    meaning = load_default_semantic_fabric().translate(source)
    attack = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == "action.weapon_attack"
    )
    scope_evidence = attack
    content_start = attack.start
    if embedded:
        scope_evidence = next(
            match for match in meaning.for_lex("scene")
            if match.concept_id == "scene.discourse.report"
        )
    scope = {
        "scope_ref": "scope.qualifier.1",
        "kind": "content_scope" if embedded else "event_scope",
        "span_start": scope_evidence.start,
        "span_end": len(source),
        "content_start": content_start,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "content" if embedded else "ordinary_argument",
        "evidence_refs": [semantic_match_ref(scope_evidence)],
    }
    constraints = [{
        "constraint_id": "constraint.negative.1",
        "scope_ref": scope["scope_ref"],
        "target_event_ref": "event.qualified.1",
        "dimension": "polarity",
        "value": "negative",
        "evidence_refs": [semantic_match_ref(attack)],
    }]
    if embedded:
        constraints.append({
            "constraint_id": "constraint.report.1",
            "scope_ref": scope["scope_ref"],
            "target_event_ref": "event.qualified.1",
            "dimension": "assertion_context",
            "value": "reported",
            "evidence_refs": [semantic_match_ref(scope_evidence)],
        })
    if ambiguous:
        constraints.append({
            "constraint_id": "constraint.capability.1",
            "scope_ref": scope["scope_ref"],
            "target_event_ref": "event.qualified.1",
            "dimension": "capability_resolution",
            "value": "unresolved",
            "evidence_refs": [semantic_match_ref(attack)],
        })
    binding = build_meaning_binding(
        meaning,
        binding_id="binding.qualified.1",
        event_node_id="event.qualified.1",
        event_span=(attack.start, len(source)),
        scope_nodes=[scope],
        constraints=constraints,
        field_provenance=[{
            "field": "action_class",
            "value": "weapon_attack",
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(attack)],
        }],
    )
    frame = ActionFrame(
        frame_id="f_qualified_1",
        clause_index=0,
        start=attack.start,
        end=len(source),
        actor_id="player",
        capability_id=None if ambiguous else "meridian_pierce",
        ambiguity=["meridian_pierce", "weapon_attack"] if ambiguous else [],
        action_class="weapon_attack",
        target_entity_id=IVEN,
        polarity=polarity,
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id="event.qualified.1",
        mechanic_disposition=binding["mechanic_disposition"],
    )
    frame.add_evidence("action", attack.start, attack.end, "weapon_attack")
    return {
        "meta": {"turn": 7},
        "semantic_meanings": [{"turn": 7, "meaning": meaning.receipt_dict()}],
        "semantic_frames": [{"turn": 7, "frame": frame.snapshot(source)}],
        "semantic_bindings": [{"turn": 7, "binding": binding}],
        "semantic_world_alignments": [],
        "mechanic_settlements": [],
    }


def test_state_builder_projects_only_qualitative_settled_truth():
    state = _semantic_state()

    packet = build_narrator_realization_from_state(state)

    assert packet is not None
    assert packet == build_narrator_realization_from_state(deepcopy(state))
    settled = packet["asserted_settled"]
    assert len(settled) == 1
    assert settled[0]["impact_kind"] == "harm"
    assert settled[0]["impact_magnitude"] == "modest"
    assert settled[0]["target_state"] == "active"
    meaning = settled[0]["event_meaning"]
    assert meaning["object_relation"]["alignment_ref"] \
        == state["semantic_world_alignments"][0]["alignment"]["fingerprint"]
    assert meaning["invoked_capability_ids"] == ["burst"]
    assert meaning["target_locus"] == "flank"
    assert meaning["target_locus_owner_id"] == IVEN
    assert settled[0]["settled_change_kinds"] == [
        "target_admission", "scene_transition", "hp", "cost", "mastery", "cooldown",
    ]
    encoded = json.dumps(packet, sort_keys=True)
    assert SOURCE not in encoded
    assert "applied_changes" not in encoded
    assert '"delta"' not in encoded and '"post"' not in encoded
    assert '"cur"' not in encoded and '"max"' not in encoded


def test_three_exact_target_admissions_project_one_canonical_change_family():
    state = _semantic_state()
    receipt = state["mechanic_settlements"][0]["receipt"]
    receipt["applied_changes"][1:1] = [
        {
            "kind": "target_admission",
            "subject_id": enemy_id,
            "entity_id": enemy_id,
            "post_state_ref": content_fingerprint({"combatant": enemy_id}),
        }
        for enemy_id in ("toll_marshal_iven#2", "toll_marshal_iven#3")
    ]
    receipt.pop("receipt_fingerprint")
    receipt["receipt_fingerprint"] = content_fingerprint(receipt)

    packet = build_narrator_realization_from_state(state)

    assert packet is not None
    assert packet["asserted_settled"][0]["settled_change_kinds"] == [
        "target_admission", "scene_transition", "hp", "cost", "mastery", "cooldown",
    ]
    assert [
        change["subject_id"]
        for change in receipt["applied_changes"]
        if change["kind"] == "target_admission"
    ] == [IVEN, "toll_marshal_iven#2", "toll_marshal_iven#3"]


def test_candidate_without_a_complete_receipt_is_explicitly_unresolved():
    packet = build_narrator_realization_from_state(_semantic_state(settled=False))

    assert packet is not None and packet["asserted_settled"] == []
    assert packet["asserted_unresolved"][0]["reason"] == "no_complete_settlement"
    assert packet["asserted_unresolved"][0]["allowed_stage"] == "attempt_only"
    assert {
        row["code"] for row in packet["forbidden_inference"]
    } >= {"no_receipt_no_outcome", "mechanic_numbers_are_not_story_language"}


def test_reported_event_is_context_not_a_world_change():
    packet = build_narrator_realization_from_state(_reported_state())

    assert packet is not None
    assert packet["asserted_settled"] == [] and packet["asserted_unresolved"] == []
    assert len(packet["attributed_noncurrent"]) == 1
    event = packet["attributed_noncurrent"][0]["event_meaning"]
    assert event["actor_id"] == "player"
    assert event["capability_id"] == "meridian_pierce"
    assert event["action_class"] == "weapon_attack"
    assert event["target_entity_id"] == IVEN
    assert event["embedding_kind"] == "reported_or_testified"
    assert event["holder_role"] == "speaker" and event["holder_entity_id"] == "player"
    assert event["polarity"] == "positive" and event["performance_mode"] == "context_only"
    assert "attributed_content_is_not_world_truth" in {
        row["code"] for row in packet["forbidden_inference"]
    }


def test_nonpositive_world_alignment_never_becomes_narrator_ownership():
    state = _semantic_state(owner="mara_voss")
    packet = build_narrator_realization_from_state(state)

    assert packet is not None
    relation = packet["asserted_settled"][0]["event_meaning"]["object_relation"]
    assert relation["alignment_status"] == "false"
    assert relation["proven_owner_id"] is None
    assert relation["resolved_instance_ids"] == ["iven_polehammer"]
    assert "object_ownership_unproven" in {
        row["code"] for row in packet["forbidden_inference"]
    }


def test_absent_or_malformed_journals_preserve_existing_directive_output():
    state = {"meta": {"turn": 7}, "_turn_guidance": "free_narration"}
    expected = _render_directive(state)
    assert build_narrator_realization_from_state(state) is None

    malformed = {
        **state,
        "semantic_frames": "not-a-journal",
        "semantic_bindings": [],
        "semantic_world_alignments": [],
        "mechanic_settlements": [],
    }
    assert build_narrator_realization_from_state(malformed) is None
    assert _render_directive(malformed) == expected

    corrupt_receipt = _semantic_state()
    corrupt_receipt["mechanic_settlements"][0]["receipt"]["applied_changes"][0]["delta"] = -9
    assert build_narrator_realization_from_state(corrupt_receipt) is None


def test_final_packet_and_adjacent_request_receive_the_same_realization_directive():
    state = _semantic_state()
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.blocks = ["DIRECTIVE"]
    doc = {"messages": [{"role": "user", "content": SOURCE}]}

    out, kept = compose(doc, state, cfg, None, "new_turn")

    assert out is not None and kept[0]["cls"] == "state_header"
    rendered = "\n".join(
        str(message.get("content") or "") for message in out["messages"]
        if isinstance(message, dict)
    )
    assert rendered.count("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1") == 1
    realization_lines = [
        line for line in rendered.splitlines()
        if line.startswith("[DIRECTIVE] NARRATOR REALIZATION")
    ]
    assert SOURCE not in realization_lines[0]
    assert "applied_changes" not in realization_lines[0]
    assert '"delta"' not in realization_lines[0] and '"post"' not in realization_lines[0]
    assert '"cur"' not in realization_lines[0] and '"max"' not in realization_lines[0]


def test_referentlex_backed_ribs_locus_survives_with_its_owner():
    packet = build_narrator_realization_from_state(_semantic_state(locus="ribs"))

    assert packet is not None
    event = packet["asserted_settled"][0]["event_meaning"]
    assert event["target_locus"] == "ribs"
    assert event["target_locus_owner_id"] == IVEN


def test_priority_preserves_ambiguity_negation_and_embedding_concurrently():
    positive = build_narrator_realization_from_state(
        _qualified_state(polarity="positive", ambiguous=True),
    )
    negative = build_narrator_realization_from_state(
        _qualified_state(polarity="negative", ambiguous=True),
    )
    direct_negative = build_narrator_realization_from_state(
        _qualified_state(polarity="negative", ambiguous=False),
    )
    embedded = build_narrator_realization_from_state(
        _qualified_state(polarity="negative", ambiguous=False, embedded=True),
    )

    assert positive is not None and negative is not None
    assert direct_negative is not None and embedded is not None
    positive_event = positive["asserted_unresolved"][0]["event_meaning"]
    negative_row = negative["asserted_unresolved"][0]
    negative_event = negative_row["event_meaning"]
    direct_negative_row = direct_negative["asserted_unresolved"][0]
    direct_negative_event = direct_negative_row["event_meaning"]
    embedded_event = embedded["attributed_noncurrent"][0]["event_meaning"]
    assert positive_event["performance_mode"] == "unresolved_do_not_select"
    assert positive_event["ambiguity_candidate_ids"] == ["meridian_pierce", "weapon_attack"]
    assert negative_event["polarity"] == "negative"
    assert negative_event["ambiguity_candidate_ids"] == ["meridian_pierce", "weapon_attack"]
    assert negative_event["performance_mode"] == "must_not_perform"
    assert negative_row["allowed_stage"] == "no_performance"
    assert direct_negative_event["performance_mode"] == "must_not_perform"
    assert direct_negative_row["allowed_stage"] == "no_performance"
    assert "attributed_content_is_not_world_truth" not in {
        row["code"] for row in direct_negative["forbidden_inference"]
    }
    assert embedded_event["polarity"] == "negative"
    assert embedded_event["embedding_kind"] == "reported_or_testified"
    assert embedded_event["performance_mode"] == "context_only"
