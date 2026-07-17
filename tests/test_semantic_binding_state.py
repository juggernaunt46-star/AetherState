"""State admission and replay coverage for ActionFrame V3's detached receipt chain."""
from __future__ import annotations

from copy import deepcopy
import random

import pytest

from aetherstate import tier0
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.mechanic_settlement import SKILL_CHECK_CONTRACT
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_binding import (
    build_meaning_binding,
    build_possessed_object_alignment,
    semantic_match_ref,
)
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _runtime(tag: str, *, with_item: bool = False):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=tag)
    ops = [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Iven", "kind": "character"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"CUN": 10, "STR": 10},
            "skills": {"perception": 0, "brawl": 0},
            "abilities": [],
            "resources": {"hp": {"max": 20}},
        }},
    ]
    if with_item:
        ops.append({"op": "item_gain", "char": "Iven", "name": "polehammer"})
    seeded = apply_delta(store, sid, bid, 0, ops, "user", cfg)
    assert not seeded.quarantined
    return cfg, store, sid, bid


def _rehash(row: dict) -> dict:
    payload = {key: value for key, value in row.items() if key != "fingerprint"}
    return {**payload, "fingerprint": content_fingerprint(payload)}


def _candidate_receipts(
    source: str,
    *,
    occurrence_turn: int,
    action_concept: str,
    capability_id: str,
    target_entity_id: str | None = None,
    scope_nodes: list[dict] | None = None,
    constraints: list[dict] | None = None,
):
    occurrence = f"t{occurrence_turn}.f1"
    meaning = load_default_semantic_fabric().translate(source)
    action = next(
        match for match in meaning.for_lex("action")
        if match.concept_id == action_concept
    )
    binding = build_meaning_binding(
        meaning,
        binding_id=f"binding.{occurrence}",
        event_node_id=f"event.{occurrence}",
        event_span=(action.start, len(source)),
        scope_nodes=scope_nodes or (),
        constraints=constraints or (),
        field_provenance=[{
            "field": "action_class",
            "value": action_concept.removeprefix("action."),
            "defaulted": False,
            "evidence_refs": [semantic_match_ref(action)],
        }],
        role_evidence=[{
            "role": "action",
            "evidence_refs": [semantic_match_ref(action)],
        }],
    )
    frame = ActionFrame(
        frame_id=occurrence,
        clause_index=0,
        start=action.start,
        end=len(source),
        actor_id="kael",
        capability_id=capability_id,
        action_class=action_concept.removeprefix("action."),
        target_entity_id=target_entity_id,
        polarity="positive",
        modality="actual",
        time_scope="current",
        meaning_ref=meaning.receipt_dict()["fingerprint"],
        fabric_fingerprint=meaning.fabric_fingerprint,
        meaning_binding_ref=binding["fingerprint"],
        event_node_id=f"event.{occurrence}",
        mechanic_disposition=binding["mechanic_disposition"],
    )
    frame.add_evidence("action", action.start, action.end, frame.action_class)
    return meaning.receipt_dict(), binding, frame, action


def _check(skill: str, frame_ref: str) -> dict:
    return {
        "op": "check",
        "skill": skill,
        "result": 10,
        "tier": "success",
        "char": "kael",
        "_mod": 0,
        "_dice": "2d6",
        "_semantic_frame_ref": frame_ref,
    }


def _complete_skill_check_ops(
    cfg: Config,
    state: dict,
    frame: dict,
    binding: dict,
    *,
    turn: int,
) -> list[dict]:
    """Build one real check shape, then bind it to this exact focused V3 occurrence."""
    skill = str(frame["capability_id"])
    probe = tier0.run(
        {"messages": [{
            "role": "user",
            "content": f"I assess the situation. ((aether.check {skill} vs 9))",
        }]},
        "new_turn",
        False,
        state,
        cfg,
        random.Random(3),
        turn=turn,
    )
    probe_wrapper = next(
        op for op in probe.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
        and op.get("contract_id") == SKILL_CHECK_CONTRACT
    )
    members = deepcopy(probe_wrapper["members"])
    for member in members:
        member["_semantic_frame_ref"] = frame["fingerprint"]

    grouped = tier0.Tier0Result(rule_ops=members)
    tier0._group_skill_check_settlements(  # noqa: SLF001 - focused production grouping proof
        grouped,
        [frame],
        {frame["frame_id"]: binding},
    )
    assert not grouped.notices
    assert [op["op"] for op in grouped.rule_ops] == [
        "mechanic_settlement_commit", "check", "master_tick",
    ]
    return grouped.rule_ops


def test_v3_orders_detached_receipts_before_frame_and_replays_idempotently():
    cfg, store, sid, bid = _runtime("semantic-v3-order")
    meaning, binding, frame, _action = _candidate_receipts(
        "I inspect the gate.",
        occurrence_turn=4,
        action_concept="action.inspect",
        capability_id="perception",
    )
    snapshot = frame.snapshot("I inspect the gate.")
    receipts = [
        {"op": "semantic_meaning_commit", "meaning": meaning},
        {"op": "semantic_binding_commit", "binding": binding},
        {"op": "semantic_frame_commit", "frame": snapshot},
    ]
    mechanics = _complete_skill_check_ops(
        cfg, current_state(store, bid), snapshot, binding, turn=4,
    )

    result = apply_delta(
        store,
        sid,
        bid,
        4,
        [*mechanics, *reversed(receipts)],
        "rule",
        cfg,
    )

    assert not result.quarantined
    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_frame_commit",
        "mechanic_settlement_commit",
        "check",
        "master_tick",
    ]
    assert result.state["semantic_bindings"] == [{"turn": 4, "binding": binding}]
    assert result.state["semantic_frames"] == [{"turn": 4, "frame": snapshot}]
    assert current_state(store, bid)["semantic_bindings"] == result.state["semantic_bindings"]

    recommit = apply_delta(store, sid, bid, 4, receipts, "rule", cfg)
    assert not recommit.quarantined
    assert recommit.state["semantic_meanings"] == [{"turn": 4, "meaning": meaning}]
    assert recommit.state["semantic_bindings"] == [{"turn": 4, "binding": binding}]
    assert recommit.state["semantic_frames"] == [{"turn": 4, "frame": snapshot}]


def test_binding_is_rechecked_against_the_exact_committed_meaning_receipt():
    cfg, store, sid, bid = _runtime("semantic-v3-binding-evidence")
    meaning, binding, _frame, _action = _candidate_receipts(
        "I inspect the gate.",
        occurrence_turn=3,
        action_concept="action.inspect",
        capability_id="perception",
    )
    forged = deepcopy(binding)
    forged["field_provenance"][0]["evidence_refs"][0]["entry_fingerprint"] = (
        "sha256:" + "0" * 64
    )
    forged = _rehash(forged)

    result = apply_delta(
        store,
        sid,
        bid,
        3,
        [
            {"op": "semantic_binding_commit", "binding": forged},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == ["semantic_meaning_commit"]
    assert "semantic_bindings" not in result.state
    assert [row["reason"] for row in result.quarantined] == [
        "semantic meaning binding does not match its meaning receipt"
    ]


def test_recognition_only_v3_frame_commits_but_cannot_authorize_a_mechanic():
    cfg, store, sid, bid = _runtime("semantic-v3-recognition-only")
    source = "I report that I strike Iven."
    meaning_probe = load_default_semantic_fabric().translate(source)
    report = next(
        match for match in meaning_probe.for_lex("scene")
        if match.concept_id == "scene.discourse.report"
    )
    report_ref = semantic_match_ref(report)
    scope = {
        "scope_ref": "scope.report.1",
        "kind": "reported_content",
        "span_start": report.start,
        "span_end": len(source),
        "content_start": report.end,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "content",
        "evidence_refs": [report_ref],
    }
    constraint = {
        "constraint_id": "constraint.report.1",
        "scope_ref": "scope.report.1",
        "target_event_ref": "event.t5.f1",
        "dimension": "assertion_context",
        "value": "reported",
        "evidence_refs": [report_ref],
    }
    meaning, binding, frame, _action = _candidate_receipts(
        source,
        occurrence_turn=5,
        action_concept="action.weapon_attack",
        capability_id="brawl",
        target_entity_id="iven",
        scope_nodes=[scope],
        constraints=[constraint],
    )
    snapshot = frame.snapshot(source)

    result = apply_delta(
        store,
        sid,
        bid,
        5,
        [
            _check("brawl", snapshot["fingerprint"]),
            {"op": "semantic_frame_commit", "frame": snapshot},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_frame_commit",
    ]
    assert result.state["rolls"] == []
    assert [row["reason"] for row in result.quarantined] == [
        "semantic action frame binding abstains from mechanic execution"
    ]


@pytest.mark.parametrize("raw_kind", ["check_and_mastery", "effect"])
def test_same_frame_scene_cannot_exempt_unsettled_skill_mechanics(raw_kind: str):
    cfg, store, sid, bid = _runtime(f"semantic-v3-scene-bypass-{raw_kind}")
    meaning, binding, frame, _action = _candidate_receipts(
        "I inspect the gate.",
        occurrence_turn=4,
        action_concept="action.inspect",
        capability_id="perception",
    )
    snapshot = frame.snapshot("I inspect the gate.")
    grouped = _complete_skill_check_ops(
        cfg, current_state(store, bid), snapshot, binding, turn=4,
    )
    wrapper = next(op for op in grouped if op["op"] == "mechanic_settlement_commit")
    if raw_kind == "check_and_mastery":
        raw = deepcopy(wrapper["members"])
    else:
        raw = [{
            "op": "effect_add",
            "char": "iven",
            "effect": "Burning",
            "_semantic_frame_ref": snapshot["fingerprint"],
        }]
    scene = {
        "op": "scene_set",
        "phase": "climax",
        "_semantic_frame_ref": snapshot["fingerprint"],
    }

    result = apply_delta(
        store,
        sid,
        bid,
        4,
        [
            *raw,
            scene,
            {"op": "semantic_frame_commit", "frame": snapshot},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_frame_commit",
    ]
    attempted = {op["op"] for op in [*raw, scene]}
    quarantined = [
        row for row in result.quarantined if row["op"].get("op") in attempted
    ]
    assert len(quarantined) == len(raw) + 1
    assert {row["reason"] for row in quarantined} == {
        "current V3 mechanics require one complete mechanic settlement"
    }
    assert result.state["rolls"] == []
    assert result.state["player"]["kael"].get("mastery") is None
    assert result.state["effects"] == {}
    assert result.state["scene"] == {}


def test_unsupported_impact_frame_cannot_raw_apply_while_its_contract_is_unresolved():
    cfg, store, sid, bid = _runtime("semantic-v3-unresolved-kill-impact")
    meaning, binding, frame, _action = _candidate_receipts(
        "I kill Iven.",
        occurrence_turn=8,
        action_concept="action.kill_attempt",
        capability_id="brawl",
        target_entity_id="iven",
    )
    snapshot = frame.snapshot("I kill Iven.")
    raw = [
        _check("brawl", snapshot["fingerprint"]),
        {
            "op": "scene_set",
            "phase": "climax",
            "_semantic_frame_ref": snapshot["fingerprint"],
        },
    ]

    result = apply_delta(
        store,
        sid,
        bid,
        8,
        [
            *raw,
            {"op": "semantic_frame_commit", "frame": snapshot},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_frame_commit",
    ]
    assert [row["reason"] for row in result.quarantined] == [
        "current V3 mechanics require one complete mechanic settlement",
        "current V3 mechanics require one complete mechanic settlement",
    ]
    assert result.state["rolls"] == []
    assert result.state["scene"] == {}


def test_wrong_turn_noncandidate_chain_cannot_supply_specific_disposition():
    cfg, store, sid, bid = _runtime("semantic-v3-wrong-turn-disposition")
    source = "I report that I strike Iven."
    meaning_probe = load_default_semantic_fabric().translate(source)
    report = next(
        match for match in meaning_probe.for_lex("scene")
        if match.concept_id == "scene.discourse.report"
    )
    report_ref = semantic_match_ref(report)
    scope = {
        "scope_ref": "scope.report.1",
        "kind": "reported_content",
        "span_start": report.start,
        "span_end": len(source),
        "content_start": report.end,
        "content_end": len(source),
        "parent_scope_ref": None,
        "construction_role": "content",
        "evidence_refs": [report_ref],
    }
    constraint = {
        "constraint_id": "constraint.report.1",
        "scope_ref": "scope.report.1",
        "target_event_ref": "event.t99.f1",
        "dimension": "assertion_context",
        "value": "reported",
        "evidence_refs": [report_ref],
    }
    meaning, binding, frame, _action = _candidate_receipts(
        source,
        occurrence_turn=99,
        action_concept="action.weapon_attack",
        capability_id="brawl",
        target_entity_id="iven",
        scope_nodes=[scope],
        constraints=[constraint],
    )
    snapshot = frame.snapshot(source)
    raw = _check("brawl", snapshot["fingerprint"])

    result = apply_delta(
        store,
        sid,
        bid,
        5,
        [
            raw,
            {"op": "semantic_frame_commit", "frame": snapshot},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    raw_failure = next(row for row in result.quarantined if row["op"] == raw)
    assert raw_failure["reason"] == (
        "current V3 mechanics require one complete mechanic settlement"
    )
    assert result.state["rolls"] == []
    assert "semantic_bindings" not in result.state
    assert "semantic_frames" not in result.state


def _possessive_receipts(
    state: dict,
    *,
    occurrence_turn: int,
    false_alignment: bool = False,
):
    source = "I inspect Iven's polehammer."
    meaning, binding, frame, _action = _candidate_receipts(
        source,
        occurrence_turn=occurrence_turn,
        action_concept="action.inspect",
        capability_id="perception",
        target_entity_id="iven",
    )
    alignment_state = deepcopy(state)
    if false_alignment:
        alignment_state["items"]["polehammer"]["owner"] = "mara"
    alignment = build_possessed_object_alignment(
        alignment_state,
        recognition_ref=binding["fingerprint"],
        object_name="polehammer",
        linguistic_possessor_id="iven",
    )
    frame.possessed_object = "polehammer"
    frame.linguistic_possessor_id = "iven"
    frame.possessed_object_instance_id = "polehammer"
    frame.possessed_object_owner_id = "iven"
    frame.world_alignment_refs = (alignment["fingerprint"],)
    return source, meaning, binding, alignment, frame.snapshot(source)


def test_only_positive_exact_alignment_may_populate_v3_item_identity_and_owner():
    cfg, store, sid, bid = _runtime("semantic-v3-positive-alignment", with_item=True)
    source, meaning, binding, alignment, frame = _possessive_receipts(
        current_state(store, bid), occurrence_turn=6
    )
    assert source and alignment["status"] == "positive"
    mechanics = _complete_skill_check_ops(
        cfg, current_state(store, bid), frame, binding, turn=6,
    )

    result = apply_delta(
        store,
        sid,
        bid,
        6,
        [
            *mechanics,
            {"op": "semantic_frame_commit", "frame": frame},
            {"op": "semantic_world_alignment_commit", "alignment": alignment},
            {"op": "semantic_binding_commit", "binding": binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert not result.quarantined
    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
        "mechanic_settlement_commit",
        "check",
        "master_tick",
    ]
    assert result.state["semantic_world_alignments"] == [
        {"turn": 6, "alignment": alignment}
    ]
    recommit = apply_delta(
        store,
        sid,
        bid,
        6,
        [{"op": "semantic_world_alignment_commit", "alignment": alignment}],
        "rule",
        cfg,
    )
    assert not recommit.quarantined
    assert recommit.state["semantic_world_alignments"] == [
        {"turn": 6, "alignment": alignment}
    ]

    cfg2, store2, sid2, bid2 = _runtime("semantic-v3-false-alignment", with_item=True)
    _source, meaning2, binding2, alignment2, frame2 = _possessive_receipts(
        current_state(store2, bid2), occurrence_turn=6, false_alignment=True
    )
    assert alignment2["status"] == "false"
    mechanics2 = _complete_skill_check_ops(
        cfg2, current_state(store2, bid2), frame2, binding2, turn=6,
    )
    rejected = apply_delta(
        store2,
        sid2,
        bid2,
        6,
        [
            *mechanics2,
            {"op": "semantic_frame_commit", "frame": frame2},
            {"op": "semantic_world_alignment_commit", "alignment": alignment2},
            {"op": "semantic_binding_commit", "binding": binding2},
            {"op": "semantic_meaning_commit", "meaning": meaning2},
        ],
        "rule",
        cfg2,
    )

    assert [op["op"] for op in rejected.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
    ]
    assert "semantic_frames" not in rejected.state
    assert [row["reason"] for row in rejected.quarantined] == [
        "semantic action frame item ownership lacks one positive exact world alignment",
        "semantic action frame reference has no committed frame ledger",
        "semantic action frame reference has no committed frame ledger",
        "semantic action frame reference has no committed frame ledger",
    ]


def test_v3_frame_event_node_must_match_its_exact_binding():
    cfg, store, sid, bid = _runtime("semantic-v3-event-binding")
    source = "I inspect the gate."
    meaning, binding, frame, _action = _candidate_receipts(
        source,
        occurrence_turn=7,
        action_concept="action.inspect",
        capability_id="perception",
    )
    alternate_binding = deepcopy(binding)
    alternate_binding["binding_id"] = "binding.t7.f2"
    alternate_binding["event_node_id"] = "event.t7.f2"
    alternate_binding = _rehash(alternate_binding)
    snapshot = frame.snapshot(source)
    snapshot["meaning_binding_ref"] = alternate_binding["fingerprint"]
    snapshot = _rehash(snapshot)

    result = apply_delta(
        store,
        sid,
        bid,
        7,
        [
            {"op": "semantic_frame_commit", "frame": snapshot},
            {"op": "semantic_binding_commit", "binding": alternate_binding},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_binding_commit",
    ]
    assert [row["reason"] for row in result.quarantined] == [
        "semantic V3 frame and binding occurrence identities disagree"
    ]
