from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.semantic_truth_runtime import (
    SemanticTruthRuntimeError,
    build_fenced_runtime_truth_contract,
)
from aetherstate.state import empty_state, reduce_state
from aetherstate.turn_lifecycle import journal_window_fingerprint


TURN = 7
BRANCH = "branch.fenced-runtime"


def _base_state() -> dict:
    state = empty_state()
    state["meta"]["turn"] = TURN
    return state


def _journal(*ops: dict) -> list[dict]:
    return [
        {
            "id": 101,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": "rule",
            "ops": deepcopy(list(ops)),
        }
    ]


def _build(pre_state: dict, *ops: dict):
    post_state = deepcopy(pre_state)
    reduce_state(post_state, deepcopy(list(ops)))
    rows = _journal(*ops)
    return build_fenced_runtime_truth_contract(
        pre_state=pre_state,
        post_state=post_state,
        pre_ledger_hash=content_fingerprint(pre_state),
        post_ledger_hash=content_fingerprint(post_state),
        journal_rows=rows,
        journal_window_fingerprint=journal_window_fingerprint(BRANCH, rows),
        branch_id=BRANCH,
        turn_index=TURN,
    )


@pytest.mark.parametrize(
    ("prepare", "op", "expected_kind", "expected_detail", "expected_label"),
    [
        (
            lambda state: state["scene"].update(
                {"location_id": "village", "scene_index": 2}
            ),
            {
                "op": "scene_set",
                "location": "forest",
                "_loc_create": {"eid": "forest", "name": "Forest"},
                "_canon": True,
                "_loc_alias": "Dark Forest",
                "_prev_loc": "village",
                "_turn": TURN,
            },
            "movement",
            "forest",
            "world",
        ),
        (
            lambda _state: None,
            {
                "op": "time_advance",
                "to_time_of_day": "night",
                "calendar_note": "First Watch",
                "_turn_mark": True,
                "_turn": TURN,
            },
            "time",
            "night",
            "world",
        ),
        (
            lambda state: state["player"].update(
                {
                    "hero": {
                        "name": "Hero",
                        "resources": {"mana": {"cur": 4, "max": 10}},
                    }
                }
            ),
            {
                "op": "resource_change",
                "char": "hero",
                "resource": "mana",
                "action": "spend",
                "amount": 2,
                "_turn": TURN,
            },
            "resource",
            "mana",
            "Hero",
        ),
    ],
)
def test_fenced_runtime_merges_each_required_transition_fact_once(
    prepare, op, expected_kind, expected_detail, expected_label
):
    pre = _base_state()
    prepare(pre)

    result = _build(pre, op)
    contract = result.truth_contract
    projection = result.transition_projection

    projected = projection["required_facts"]
    projected_refs = [row["fact_ref"] for row in projected]
    merged = [
        row for row in contract["fallout_facts"] if row["fact_ref"] in projected_refs
    ]
    assert len(merged) == len(projected) == len(set(projected_refs))
    assert sorted(row["fact_ref"] for row in merged) == sorted(projected_refs)
    matching_facts = [
        row
        for row in merged
        for effect in row["effects"]
        if effect["kind"] == expected_kind and effect["detail"] == expected_detail
    ]
    assert len(matching_facts) == 1
    assert matching_facts[0]["subject_label"] == expected_label
    assert all(row["subject_label"] for row in merged)


@pytest.mark.parametrize("bad_binding", ["pre_hash", "post_hash", "window_hash"])
def test_fenced_runtime_refuses_any_hash_or_window_mismatch(bad_binding):
    pre = _base_state()
    post = deepcopy(pre)
    op = {"op": "world_flag", "key": "gate_open", "value": True, "_turn": TURN}
    reduce_state(post, [deepcopy(op)])
    rows = _journal(op)
    kwargs = {
        "pre_state": pre,
        "post_state": post,
        "pre_ledger_hash": content_fingerprint(pre),
        "post_ledger_hash": content_fingerprint(post),
        "journal_rows": rows,
        "journal_window_fingerprint": journal_window_fingerprint(BRANCH, rows),
        "branch_id": BRANCH,
        "turn_index": TURN,
    }
    kwargs[
        {
            "pre_hash": "pre_ledger_hash",
            "post_hash": "post_ledger_hash",
            "window_hash": "journal_window_fingerprint",
        }[bad_binding]
    ] = content_fingerprint({"wrong": bad_binding})

    with pytest.raises(SemanticTruthRuntimeError):
        build_fenced_runtime_truth_contract(**kwargs)


def test_explicit_combat_end_adapter_is_proved_but_not_projected_twice():
    pre = _base_state()
    pre["combat"] = {
        "active": True,
        "started_turn": 2,
        "combatants": {},
        "history": [],
        "pending_intent": None,
    }
    op = {"op": "combat_end", "outcome": "resolved", "_turn": TURN}

    result = _build(pre, op)
    projection = result.transition_projection
    contract = result.truth_contract

    assert projection["entries"][0]["visibility"] == "explicit_adapter"
    assert projection["required_facts"] == []
    matches = [
        row
        for row in contract["fallout_facts"]
        if row["effects"]
        == [{"kind": "world", "detail": "combat ended resolved", "amount": None}]
    ]
    assert len(matches) == 1
    claims = [
        row
        for row in contract["expected_claims"]
        if row["kind"] == "world" and row["detail"] == "combat ended resolved"
    ]
    assert len(claims) == 1


def test_required_transition_cannot_cover_an_adjacent_explicit_adapter():
    pre = _base_state()
    pre["combat"] = {
        "active": True,
        "started_turn": 2,
        "combatants": {},
        "history": [],
        "pending_intent": None,
    }
    result = _build(
        pre,
        {"op": "world_flag", "key": "gate_open", "value": True, "_turn": TURN},
        {"op": "combat_end", "outcome": "resolved", "_turn": TURN},
    )

    assert [row["visibility"] for row in result.transition_projection["entries"]] == [
        "required",
        "explicit_adapter",
    ]
    assert len(result.transition_projection["required_facts"]) == 1
    effects = [
        (effect["kind"], effect["detail"])
        for row in result.truth_contract["fallout_facts"]
        for effect in row["effects"]
    ]
    assert effects.count(("world", "flag gate open true")) == 1
    assert effects.count(("world", "combat ended resolved")) == 1


def test_fenced_runtime_result_is_an_immutable_detached_persistence_object():
    pre = _base_state()
    result = _build(
        pre,
        {"op": "world_flag", "key": "gate_open", "value": True, "_turn": TURN},
    )
    original_contract = result.truth_contract
    original_projection = result.transition_projection

    contract_copy = result.truth_contract
    contract_copy["fallout_facts"].clear()
    projection_copy = result.transition_projection
    projection_copy["required_facts"].clear()

    assert result.truth_contract == original_contract
    assert result.transition_projection == original_projection
    payload = result.to_persistence_payload()
    supplied = payload.pop("fingerprint")
    assert supplied == result.fingerprint == content_fingerprint(payload)
    assert result.truth_contract_fingerprint == original_contract["fingerprint"]
    assert (
        result.transition_projection_fingerprint
        == original_projection["fingerprint"]
    )
    with pytest.raises(FrozenInstanceError):
        result.fingerprint = "sha256:" + "0" * 64
