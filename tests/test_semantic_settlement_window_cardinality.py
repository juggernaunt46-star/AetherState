"""Exact wrapper/member occurrence admission for fenced semantic truth construction."""
from __future__ import annotations

from copy import deepcopy
from random import Random

import pytest

from aetherstate import tier0
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.semantic_truth_runtime import (
    SemanticTruthRuntimeError,
    build_fenced_runtime_truth_contract,
)
from aetherstate.semantic_transition_truth import (
    SemanticTransitionTruthError,
    project_journal_transitions,
    validate_transition_projection,
)
from aetherstate.state import (
    apply_delta,
    assign_damage_effect_ids,
    current_state,
    empty_state,
    reduce_state,
)
from aetherstate.store import Store
from aetherstate.turn_lifecycle import journal_window_fingerprint
from tests.test_weapon_settlement_state import (
    _ops as weapon_ops,
    _runtime as weapon_runtime,
)


TURN = 1


def _case_from_store(
    store: Store,
    branch_id: str,
    pre_state: dict,
    post_state: dict,
) -> dict:
    rows = deepcopy(store.diagnostic_turn(branch_id, TURN)["journal"])
    return {
        "pre_state": deepcopy(pre_state),
        "post_state": deepcopy(post_state),
        "pre_ledger_hash": content_fingerprint(pre_state),
        "post_ledger_hash": content_fingerprint(post_state),
        "journal_rows": rows,
        "journal_window_fingerprint": journal_window_fingerprint(branch_id, rows),
        "branch_id": branch_id,
        "turn_index": TURN,
    }


def _with_rows(case: dict, rows: list[dict]) -> dict:
    changed = deepcopy(case)
    changed["journal_rows"] = deepcopy(rows)
    changed["journal_window_fingerprint"] = journal_window_fingerprint(
        changed["branch_id"], rows
    )
    changed["pre_ledger_hash"] = content_fingerprint(changed["pre_state"])
    changed["post_ledger_hash"] = content_fingerprint(changed["post_state"])
    return changed


def _project_case(case: dict) -> dict:
    return project_journal_transitions(
        pre_state=case["pre_state"],
        post_state=case["post_state"],
        journal_rows=case["journal_rows"],
        branch_id=case["branch_id"],
        turn_index=case["turn_index"],
        pre_ledger_hash=case["pre_ledger_hash"],
        post_ledger_hash=case["post_ledger_hash"],
        journal_window_fingerprint=case["journal_window_fingerprint"],
    )


def _skill_case() -> dict:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="settlement-cardinality-skill")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14},
                    "skills": {"stealth": 3},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    pre = deepcopy(current_state(store, branch_id))
    result = tier0.run(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "I slip unseen. ((aether.check stealth vs 9))",
                }
            ]
        },
        "new_turn",
        False,
        pre,
        cfg,
        Random(3),
        turn=TURN,
    )
    applied = apply_delta(
        store,
        session_id,
        branch_id,
        TURN,
        result.rule_ops,
        "rule",
        cfg,
    )
    assert not applied.quarantined
    return _case_from_store(store, branch_id, pre, applied.state)


def _two_skill_attempt(external_id: str):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=external_id)
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14, "WIS": 14},
                    "skills": {"stealth": 3, "perception": 3},
                    "abilities": [],
                    "resources": {"hp": {"max": 20}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    pre = deepcopy(current_state(store, branch_id))
    result = tier0.run(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "I slip unseen. ((aether.check stealth vs 9)) "
                        "I inspect the sigils. ((aether.check perception vs 9))"
                    ),
                }
            ]
        },
        "new_turn",
        False,
        pre,
        cfg,
        Random(3),
        turn=TURN,
    )
    return cfg, store, session_id, branch_id, pre, result.rule_ops


def _combat_opening_attempt(external_id: str, enemy_count: int):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id=external_id)
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"STR": 12},
                    "skills": {"perception": 1},
                    "abilities": [],
                    "resources": {"hp": {"max": 24}},
                },
            },
            {"op": "entity_add", "name": "Marshal Varo", "kind": "npc"},
            {"op": "presence", "entity": "Marshal Varo", "present": True},
            {"op": "entity_add", "name": "Guard Two", "kind": "npc"},
            {"op": "presence", "entity": "Guard Two", "present": True},
            {"op": "entity_add", "name": "Guard Three", "kind": "npc"},
            {"op": "presence", "entity": "Guard Three", "present": True},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    pre = deepcopy(current_state(store, branch_id))
    result = tier0.run(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "I cross the first iron line, draw my longsword, and challenge Marshal "
                        "Varo. He is the hostile first opponent; I hold position and watch him "
                        "commit."
                    ),
                }
            ]
        },
        "new_turn",
        False,
        pre,
        cfg,
        Random(17),
        turn=TURN,
    )
    evidence = [
        deepcopy(op)
        for op in result.rule_ops
        if op.get("op") in {
            "semantic_meaning_commit",
            "semantic_binding_commit",
            "semantic_frame_commit",
        }
    ]
    wrapper = deepcopy(next(
        op for op in result.rule_ops if op.get("contract_id") == "combat_opening/1"
    ))
    frame_ref = wrapper["frame_ref"]
    additions = [
        {
            "op": "combatant_spawn",
            "name": name,
            "side": "enemy",
            "tier": "standard",
            "_cid": entity_id,
            "_semantic_frame_ref": frame_ref,
        }
        for name, entity_id in (
            ("Guard Two", "guard_two"),
            ("Guard Three", "guard_three"),
        )[:enemy_count - 1]
    ]
    wrapper["members"][1:1] = additions
    projections: list[dict] = []
    for index, member in enumerate(wrapper["members"]):
        projection = deepcopy(member)
        projection["_settlement_ref"] = wrapper["settlement_ref"]
        projection["_settlement_member_index"] = index
        projections.append(projection)
    return cfg, store, session_id, branch_id, pre, [*evidence, wrapper, *projections]


def _weapon_case(*, lethal: bool = False) -> dict:
    cfg, store, session_id, branch_id = weapon_runtime(
        f"settlement-cardinality-weapon-{lethal}"
    )
    if lethal:
        prepared = apply_delta(
            store,
            session_id,
            branch_id,
            0,
            [
                {"op": "combatant_hp", "target": "iven", "delta": -5, "reason": "fixture"},
                {"op": "combatant_hp", "target": "iven", "delta": -5, "reason": "fixture"},
                {"op": "combatant_hp", "target": "iven", "delta": -2, "reason": "fixture"},
            ],
            "genesis",
            cfg,
        )
        assert not prepared.quarantined
        assert prepared.state["combat"]["combatants"]["iven"]["hp"]["cur"] == 2
    pre = deepcopy(current_state(store, branch_id))
    applied = apply_delta(
        store,
        session_id,
        branch_id,
        TURN,
        weapon_ops(TURN, tier="success"),
        "rule",
        cfg,
    )
    assert not applied.quarantined
    return _case_from_store(store, branch_id, pre, applied.state)


def _opposition_case() -> dict:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="settlement-cardinality-opposition")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "entity_add", "name": "Orlen", "kind": "npc"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"DEX": 14},
                    "skills": {"stealth": 3},
                    "abilities": [],
                    "resources": {"hp": {"max": 3}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    pre = deepcopy(current_state(store, branch_id))
    opposition = {
        "intent_id": "intent-orlen-measured-cut",
        "move_id": "measured-cut",
        "move_name": "Measured Cut",
        "actor": "orlen",
        "actor_name": "Orlen",
        "target": "kael",
        "target_name": "Kael",
        "total_raw": 8,
        "total": 8,
        "tier": "HITS",
        "damage": 1,
        "delivery": "a hooked glaive",
        "basis": "physical",
        "range": "close",
        "timing": "fast",
        "cadence": "reliable",
        "sensory": "steel bites",
    }
    ops = assign_damage_effect_ids(
        [{"op": "hp_adj", "char": "kael", "delta": -1, "_opposition": opposition}],
        branch_id,
        TURN,
        "code",
        basis="opposition:cardinality-positive",
    )
    applied = apply_delta(
        store, session_id, branch_id, TURN, ops, "rule", cfg
    )
    assert not applied.quarantined
    return _case_from_store(store, branch_id, pre, applied.state)


def _manual_case(pre_state: dict, *ops: dict) -> dict:
    post_state = deepcopy(pre_state)
    reduce_state(post_state, deepcopy(list(ops)))
    branch_id = "branch.settlement-cardinality-manual"
    rows = [
        {
            "id": 1,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": "rule",
            "ops": deepcopy(list(ops)),
        }
    ]
    return {
        "pre_state": deepcopy(pre_state),
        "post_state": post_state,
        "pre_ledger_hash": content_fingerprint(pre_state),
        "post_ledger_hash": content_fingerprint(post_state),
        "journal_rows": rows,
        "journal_window_fingerprint": journal_window_fingerprint(branch_id, rows),
        "branch_id": branch_id,
        "turn_index": TURN,
    }


def _wrapper_and_children(rows: list[dict]):
    wrapper = next(
        op
        for row in rows
        for op in row["ops"]
        if op.get("op") == "mechanic_settlement_commit"
    )
    settlement_ref = wrapper["settlement_ref"]
    children = [
        op
        for row in rows
        for op in row["ops"]
        if op.get("_settlement_ref") == settlement_ref
    ]
    children.sort(key=lambda op: op["_settlement_member_index"])
    return wrapper, children


def _remove_operation(rows: list[dict], target: dict) -> None:
    for row in rows:
        for index, op in enumerate(row["ops"]):
            if op is target:
                del row["ops"][index]
                return
    raise AssertionError("target operation was not present")


def test_valid_skill_and_weapon_windows_preserve_exact_projection_members():
    skill = build_fenced_runtime_truth_contract(**_skill_case())
    weapon = build_fenced_runtime_truth_contract(**_weapon_case())

    assert any(claim["kind"] == "resource" for claim in skill.truth_contract["expected_claims"])
    assert any(claim["kind"] == "harm" for claim in weapon.truth_contract["expected_claims"])
    for result in (skill, weapon):
        member_entries = [
            entry
            for entry in result.transition_projection["entries"]
            if entry["operation"].get("_settlement_ref")
        ]
        assert member_entries
        assert all(entry["visibility"] == "explicit_adapter" for entry in member_entries)
        assert all(entry["changed"] is False for entry in member_entries)


def test_two_nonimpact_v3_checks_keep_two_exact_settlements_and_receipts():
    cfg, store, session_id, branch_id, pre, ops = _two_skill_attempt(
        "settlement-cardinality-two-skill"
    )
    wrappers = [
        op for op in ops if op.get("op") == "mechanic_settlement_commit"
    ]

    assert len(wrappers) == 2
    assert all(wrapper["contract_id"] == "skill_check/1" for wrapper in wrappers)
    assert [
        [member["op"] for member in wrapper["members"]]
        for wrapper in wrappers
    ] == [["check", "master_tick"], ["check", "master_tick"]]

    applied = apply_delta(
        store, session_id, branch_id, TURN, ops, "rule", cfg
    )
    assert not applied.quarantined
    receipts = [
        row["receipt"]
        for row in applied.state["mechanic_settlements"]
        if row["turn"] == TURN
    ]
    assert len(receipts) == 2
    assert {receipt["frame_ref"] for receipt in receipts} == {
        wrapper["frame_ref"] for wrapper in wrappers
    }
    assert {op["skill"] for op in applied.applied if op.get("op") == "check"} \
        == {"stealth", "perception"}

    fenced = build_fenced_runtime_truth_contract(
        **_case_from_store(store, branch_id, pre, applied.state)
    )
    assert {
        entry["operation"].get("_settlement_ref")
        for entry in fenced.transition_projection["entries"]
        if entry["operation"].get("_settlement_ref")
    } == {wrapper["settlement_ref"] for wrapper in wrappers}


def test_stripped_second_v3_check_cannot_partially_apply_beside_exact_first_wrapper():
    cfg, store, session_id, branch_id, _pre, ops = _two_skill_attempt(
        "settlement-cardinality-stripped-second-skill"
    )
    wrappers = [
        op for op in ops if op.get("op") == "mechanic_settlement_commit"
    ]
    stripped_ref = wrappers[1]["settlement_ref"]
    attempted: list[dict] = []
    for op in ops:
        if op is wrappers[1]:
            continue
        changed = deepcopy(op)
        if changed.get("_settlement_ref") == stripped_ref:
            changed.pop("_settlement_ref")
            changed.pop("_settlement_member_index")
        attempted.append(changed)

    applied = apply_delta(
        store, session_id, branch_id, TURN, attempted, "rule", cfg
    )

    assert any(
        row["op"].get("_semantic_frame_ref") == wrappers[1]["frame_ref"]
        and "unindexed standalone" in row["reason"]
        for row in applied.quarantined
    )
    receipts = [
        row["receipt"]
        for row in applied.state["mechanic_settlements"]
        if row["turn"] == TURN
    ]
    assert len(receipts) == 1 and receipts[0]["frame_ref"] == wrappers[0]["frame_ref"]
    assert {op["skill"] for op in applied.applied if op.get("op") == "check"} \
        == {"stealth"}


@pytest.mark.parametrize("enemy_count", [1, 2, 3])
def test_combat_opening_atomically_admits_one_to_three_grounded_enemies(enemy_count: int):
    from aetherstate.hud import hud_view

    cfg, store, session_id, branch_id, pre, ops = _combat_opening_attempt(
        f"settlement-cardinality-opening-{enemy_count}", enemy_count
    )
    applied = apply_delta(
        store, session_id, branch_id, TURN, ops, "rule", cfg
    )

    assert not applied.quarantined
    assert applied.state["combat"]["active"] is True
    assert applied.state["scene"]["phase"] == "climax"
    enemies = [
        row for row in applied.state["combat"]["combatants"].values()
        if row["side"] == "enemy"
    ]
    assert len(enemies) == enemy_count
    assert all(row.get("kit") for row in enemies)
    receipt = next(
        row["receipt"]
        for row in applied.state["mechanic_settlements"]
        if row["turn"] == TURN
    )
    assert receipt["contract_id"] == "combat_opening/1"
    assert receipt["target_post_state"]["combatant_id"] == "marshal_varo"
    admissions = [
        change for change in receipt["applied_changes"]
        if change["kind"] == "target_admission"
    ]
    assert len(admissions) == enemy_count
    assert not any(op.get("op") == "combatant_hp" for op in applied.applied)
    war = hud_view(applied.state, cfg)["war_room"]
    assert war["active"] is True
    assert len([row for row in war["combatants"] if row["side"] == "enemy"]) == enemy_count

    fenced = build_fenced_runtime_truth_contract(
        **_case_from_store(store, branch_id, pre, applied.state)
    )
    indexed = [
        entry
        for entry in fenced.transition_projection["entries"]
        if entry["operation"].get("_settlement_ref") == receipt["settlement_ref"]
    ]
    assert len(indexed) == enemy_count + 1
    assert all(entry["visibility"] == "explicit_adapter" for entry in indexed)


def test_lethal_weapon_settlement_keeps_its_exact_fallout_claims():
    result = build_fenced_runtime_truth_contract(**_weapon_case(lethal=True))
    claims = result.truth_contract["expected_claims"]

    assert any(claim["kind"] == "harm" and claim["amount"] == -2 for claim in claims)
    assert any(claim["kind"] == "defeat" for claim in claims)


def test_opposition_no_effect_and_explicit_adapter_paths_remain_admitted():
    opposition = build_fenced_runtime_truth_contract(**_opposition_case())
    assert any(
        claim["kind"] == "opposition_action"
        for claim in opposition.truth_contract["expected_claims"]
    )

    no_effect_pre = empty_state()
    no_effect_pre["meta"]["turn"] = TURN
    no_effect_op = {
        "op": "world_flag",
        "key": "gate_open",
        "value": True,
        "_turn": TURN,
    }
    reduce_state(no_effect_pre, [deepcopy(no_effect_op)])
    no_effect = build_fenced_runtime_truth_contract(
        **_manual_case(no_effect_pre, no_effect_op)
    )
    assert no_effect.transition_projection["entries"][0]["visibility"] == "no_effect"

    adapter_pre = empty_state()
    adapter_pre["meta"]["turn"] = TURN
    adapter_pre["combat"] = {
        "active": True,
        "started_turn": 0,
        "combatants": {},
        "history": [],
        "pending_intent": None,
    }
    adapter = build_fenced_runtime_truth_contract(
        **_manual_case(
            adapter_pre,
            {"op": "combat_end", "outcome": "resolved", "_turn": TURN},
        )
    )
    assert adapter.transition_projection["entries"][0]["visibility"] == "explicit_adapter"


def test_duplicate_wrapper_in_a_later_current_row_is_rejected():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    wrapper, _children = _wrapper_and_children(rows)
    rows.append(
        {
            "id": rows[-1]["id"] + 1,
            "turn_lo": TURN,
            "turn_hi": TURN,
            "source": "rule",
            "ops": [deepcopy(wrapper)],
        }
    )

    with pytest.raises(SemanticTruthRuntimeError, match="exactly one wrapper"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_duplicate_indexed_member_occurrence_is_rejected():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    rows[0]["ops"].append(deepcopy(children[0]))

    with pytest.raises(SemanticTruthRuntimeError, match="duplicated"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


@pytest.mark.parametrize("member_position", [0, -1], ids=["gapped", "missing-tail"])
def test_missing_or_gapped_indexed_member_is_rejected(member_position: int):
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    _remove_operation(rows, children[member_position])

    with pytest.raises(SemanticTruthRuntimeError, match="missing, gapped, or extra"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_extra_indexed_member_is_rejected():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    wrapper, children = _wrapper_and_children(rows)
    extra = deepcopy(children[-1])
    extra["_settlement_member_index"] = len(wrapper["members"])
    rows[0]["ops"].append(extra)

    with pytest.raises(SemanticTruthRuntimeError, match="missing, gapped, or extra"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_child_only_current_receipt_cannot_be_lent_from_post_state():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    wrapper, _children = _wrapper_and_children(rows)
    _remove_operation(rows, wrapper)

    with pytest.raises(SemanticTruthRuntimeError, match="current wrappers"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_child_only_occurrence_cannot_borrow_a_historical_receipt():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    wrapper, _children = _wrapper_and_children(rows)
    _remove_operation(rows, wrapper)
    receipt = deepcopy(case["post_state"]["mechanic_settlements"][0]["receipt"])
    case["pre_state"]["mechanic_settlements"] = [{"turn": 0, "receipt": receipt}]
    case["post_state"]["mechanic_settlements"] = [{"turn": 0, "receipt": receipt}]

    with pytest.raises(SemanticTruthRuntimeError, match="historical receipt"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_member_with_another_settlement_ref_cannot_borrow_the_wrapper():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    children[0]["_settlement_ref"] = content_fingerprint({"settlement": "other"})

    with pytest.raises(SemanticTruthRuntimeError, match="mechanic settlement"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def test_indexed_member_payload_must_equal_the_wrapper_member_exactly():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    children[0]["result"] += 1

    with pytest.raises(SemanticTruthRuntimeError, match="payload is not exact"):
        build_fenced_runtime_truth_contract(**_with_rows(case, rows))


def _stripped_member(member: dict) -> dict:
    stripped = deepcopy(member)
    stripped.pop("_settlement_ref", None)
    stripped.pop("_settlement_member_index", None)
    stripped.pop("_semantic_frame_ref", None)
    return stripped


def test_apply_rejects_any_unindexed_member_family_beside_a_current_wrapper():
    cfg, store, session_id, branch_id = weapon_runtime(
        "settlement-cardinality-stripped-apply"
    )
    ops = weapon_ops(TURN, tier="success")
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    mastery = next(member for member in wrapper["members"] if member["op"] == "master_tick")
    rogue = _stripped_member(mastery)
    rogue["skill"] = "perception"
    rogue["_ignored_metadata"] = "bypass"
    independent = {
        "op": "check",
        "skill": "perception",
        "char": "kael",
        "result": 7,
        "tier": "partial",
    }

    result = apply_delta(
        store,
        session_id,
        branch_id,
        TURN,
        [*ops, rogue, independent],
        "rule",
        cfg,
    )

    assert any(
        row["op"] == rogue and "unindexed standalone" in row["reason"]
        for row in result.quarantined
    )
    assert any(
        row["op"] == independent and "unindexed standalone" in row["reason"]
        for row in result.quarantined
    )
    assert sum(op.get("op") == "master_tick" for op in result.applied) == 1
    assert not any(op.get("skill") == "perception" for op in result.applied)

    # Compatibility is scoped to a window with no current settlement wrapper. An ordinary legacy
    # check still applies there; it cannot be smuggled beside a proof-carrying mechanic group.
    clean_cfg, clean_store, clean_session, clean_branch = weapon_runtime(
        "settlement-cardinality-standalone-legacy-check"
    )
    standalone = apply_delta(
        clean_store,
        clean_session,
        clean_branch,
        TURN,
        [independent],
        "rule",
        clean_cfg,
    )
    assert any(
        all(op.get(key) == value for key, value in independent.items())
        for op in standalone.applied
    )


@pytest.mark.parametrize("mode", ["duplicate_wrapper", "missing_wrapper"])
def test_invalid_settlement_evidence_cannot_release_a_stripped_identity_switch(mode: str):
    cfg, store, session_id, branch_id = weapon_runtime(
        f"settlement-cardinality-invalid-evidence-{mode}"
    )
    pre = deepcopy(current_state(store, branch_id))
    ops = weapon_ops(TURN, tier="success")
    wrapper = next(op for op in ops if op.get("op") == "mechanic_settlement_commit")
    mastery = next(member for member in wrapper["members"] if member["op"] == "master_tick")
    rogue = _stripped_member(mastery)
    rogue["skill"] = "perception"
    rogue["_ignored_metadata"] = "bypass"
    if mode == "duplicate_wrapper":
        attempted = [*ops, deepcopy(wrapper), rogue]
    else:
        attempted = [
            *[op for op in ops if op.get("op") != "mechanic_settlement_commit"],
            rogue,
        ]

    result = apply_delta(
        store,
        session_id,
        branch_id,
        TURN,
        attempted,
        "rule",
        cfg,
    )

    assert any(
        row["op"] == rogue and "unindexed standalone" in row["reason"]
        for row in result.quarantined
    )
    assert "perception" not in result.state["player"]["kael"].get("mastery", {})
    assert not any(
        op.get("op") == "master_tick" and op.get("skill") == "perception"
        for op in result.applied
    )
    fenced = build_fenced_runtime_truth_contract(
        **_case_from_store(store, branch_id, pre, result.state)
    )
    assert not any(
        entry["operation"].get("skill") == "perception"
        for entry in fenced.transition_projection["entries"]
    )


def test_fenced_truth_rejects_a_stripped_member_even_when_post_state_replays_it():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    mastery = next(child for child in children if child["op"] == "master_tick")
    rogue = _stripped_member(mastery)
    rogue["skill"] = "perception"
    rogue["_ignored_metadata"] = "bypass"
    rows[0]["ops"].append(rogue)

    # Historical replay deliberately remains compatible; the current fenced window must still
    # refuse to reinterpret that unframed row as a second code-owned settlement occurrence.
    changed = deepcopy(case)
    reduce_state(changed["post_state"], [deepcopy(rogue)])

    with pytest.raises(SemanticTruthRuntimeError, match="unindexed standalone"):
        build_fenced_runtime_truth_contract(**_with_rows(changed, rows))


@pytest.mark.parametrize("member_kind", ["check", "scene_set"])
def test_fenced_truth_rejects_modern_member_when_entire_wrapper_is_missing(
    member_kind: str,
):
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    wrapper, children = _wrapper_and_children(rows)
    if member_kind == "check":
        modern = deepcopy(next(child for child in children if child["op"] == "check"))
        modern.pop("_settlement_ref")
        modern.pop("_settlement_member_index")
    else:
        modern = {
            "op": "scene_set",
            "phase": "climax",
            "_semantic_frame_ref": wrapper["frame_ref"],
            "_turn": TURN,
        }

    kept = [
        deepcopy(op)
        for op in rows[0]["ops"]
        if op.get("op") != "mechanic_settlement_commit"
        and "_settlement_ref" not in op
    ]
    clock_index = next(
        (index for index, op in enumerate(kept) if op.get("op") == "clock_tick"),
        len(kept),
    )
    kept.insert(clock_index, modern)
    rows[0]["ops"] = kept

    changed = deepcopy(case)
    changed["post_state"] = deepcopy(case["pre_state"])
    reduce_state(changed["post_state"], deepcopy(kept))

    with pytest.raises(
        SemanticTruthRuntimeError,
        match="current V3 mechanic member requires one complete indexed mechanic settlement",
    ):
        build_fenced_runtime_truth_contract(**_with_rows(changed, rows))


@pytest.mark.parametrize("reference_case", ["nonexistent", "wrong_turn", "historical"])
def test_fenced_truth_rejects_incomplete_modern_member_reference_cases(
    reference_case: str,
):
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    modern = deepcopy(next(child for child in children if child["op"] == "check"))
    modern.pop("_settlement_ref")
    modern.pop("_settlement_member_index")
    if reference_case == "nonexistent":
        modern["_semantic_frame_ref"] = "sha256:" + "a" * 64
    elif reference_case == "wrong_turn":
        modern["_turn"] = TURN + 1
    else:
        frame = next(
            op["frame"] for op in rows[0]["ops"]
            if op.get("op") == "semantic_frame_commit"
        )
        case["pre_state"]["semantic_frames"] = [{"turn": 0, "frame": deepcopy(frame)}]

    rows[0]["ops"] = [modern]
    changed = deepcopy(case)
    changed["post_state"] = deepcopy(changed["pre_state"])

    with pytest.raises(
        SemanticTruthRuntimeError,
        match="current V3 mechanic member requires one complete indexed mechanic settlement",
    ):
        build_fenced_runtime_truth_contract(**_with_rows(changed, rows))


def test_fenced_truth_keeps_truly_unframed_legacy_member_compatibility():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    legacy = _stripped_member(
        next(child for child in children if child["op"] == "check")
    )
    rows[0]["ops"] = [legacy]
    changed = deepcopy(case)
    changed["post_state"] = deepcopy(case["pre_state"])
    reduce_state(changed["post_state"], [deepcopy(legacy)])

    result = build_fenced_runtime_truth_contract(**_with_rows(changed, rows))

    assert [entry["op_kind"] for entry in result.transition_projection["entries"]] == [
        "check"
    ]
    assert result.transition_projection["entries"][0]["visibility"] == "required"


def test_direct_transition_projection_rejects_modern_member_without_wrapper():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    modern = deepcopy(next(child for child in children if child["op"] == "check"))
    modern.pop("_settlement_ref")
    modern.pop("_settlement_member_index")
    kept = [
        deepcopy(op)
        for op in rows[0]["ops"]
        if op.get("op") != "mechanic_settlement_commit"
        and "_settlement_ref" not in op
    ]
    clock_index = next(
        (index for index, op in enumerate(kept) if op.get("op") == "clock_tick"),
        len(kept),
    )
    kept.insert(clock_index, modern)
    rows[0]["ops"] = kept
    changed = deepcopy(case)
    changed["post_state"] = deepcopy(case["pre_state"])
    reduce_state(changed["post_state"], deepcopy(kept))

    with pytest.raises(
        SemanticTransitionTruthError,
        match="current V3 mechanic member requires one complete indexed mechanic settlement",
    ):
        _project_case(_with_rows(changed, rows))


def test_direct_transition_projection_rejects_incomplete_member_index_set():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    rows[0]["ops"].remove(children[-1])

    with pytest.raises(
        SemanticTransitionTruthError,
        match="mechanic settlement members are missing, gapped, or extra",
    ):
        _project_case(_with_rows(case, rows))


def test_detached_transition_validation_rechecks_modern_member_relation():
    case = _skill_case()
    rows = deepcopy(case["journal_rows"])
    _wrapper, children = _wrapper_and_children(rows)
    legacy = _stripped_member(
        next(child for child in children if child["op"] == "check")
    )
    rows[0]["ops"] = [legacy]
    changed = deepcopy(case)
    changed["post_state"] = deepcopy(case["pre_state"])
    reduce_state(changed["post_state"], [deepcopy(legacy)])
    projection = _project_case(_with_rows(changed, rows))

    projection["journal_rows"][0]["ops"][0]["_semantic_frame_ref"] = (
        "sha256:" + "a" * 64
    )
    projection["journal_window_fingerprint"] = journal_window_fingerprint(
        projection["branch_id"], projection["journal_rows"]
    )
    projection["fingerprint"] = content_fingerprint(
        {key: value for key, value in projection.items() if key != "fingerprint"}
    )

    with pytest.raises(
        SemanticTransitionTruthError,
        match="current V3 mechanic member requires one complete indexed mechanic settlement",
    ):
        validate_transition_projection(projection)
