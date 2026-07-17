"""User alias minting is an explicit, atomic journal occurrence boundary."""
from __future__ import annotations

from copy import deepcopy

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.semantic_transition_truth import project_journal_transitions
from aetherstate.state import apply_delta, current_state, reduce_state
from aetherstate.store import Store
from aetherstate.turn_lifecycle import journal_window_fingerprint


def _runtime():
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="alias-occurrences")
    return Config(), store, session_id, branch_id


def _apply(store, session_id, branch_id, ops, *, turn=1):
    return apply_delta(
        store,
        session_id,
        branch_id,
        turn,
        ops,
        "user",
        Config(),
    )


def test_unknown_goal_journals_entity_then_goal_with_exact_separate_projection():
    cfg, store, session_id, branch_id = _runtime()
    pre = current_state(store, branch_id)
    before_id = store.journal_high_water()

    result = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{"op": "goal", "char": "Ada", "action": "add", "text": "Leave the vault"}],
        "user",
        cfg,
    )

    assert not result.quarantined
    assert result.submitted_applied == 1
    assert result.applied == [
        {
            "op": "entity_add",
            "entity": "ada",
            "name": "Ada",
            "kind": "character",
            "present": True,
            "_turn": 1,
        },
        {
            "op": "goal",
            "char": "ada",
            "action": "add",
            "text": "Leave the vault",
            "_turn": 1,
        },
    ]
    assert all("_create" not in op for op in result.applied)
    post = current_state(store, branch_id)
    assert post["entities"]["ada"]["present"] is True
    assert post["chars"]["ada"]["goals"] == ["Leave the vault"]

    after_id = store.journal_high_water()
    rows = store.journal_window(branch_id, after_id=before_id, through_id=after_id)
    assert len(rows) == 1
    assert rows[0]["ops"] == result.applied

    replayed = deepcopy(pre)
    reduce_state(replayed, deepcopy(rows[0]["ops"]))
    assert replayed == post

    window_hash = journal_window_fingerprint(branch_id, rows)
    projection = project_journal_transitions(
        pre_state=pre,
        post_state=post,
        journal_rows=rows,
        branch_id=branch_id,
        turn_index=1,
        pre_ledger_hash=content_fingerprint(pre),
        post_ledger_hash=content_fingerprint(post),
        journal_window_fingerprint=window_hash,
    )
    assert projection["journal_window_fingerprint"] == window_hash
    entries = projection["entries"]
    assert [entry["op_kind"] for entry in entries] == ["entity_add", "goal"]
    assert [entry["operation"] for entry in entries] == rows[0]["ops"]
    assert entries[0]["cause_ref"] != entries[1]["cause_ref"]
    assert entries[0]["cause_ref"] == entries[0]["entry_ref"]
    assert entries[1]["cause_ref"] == entries[1]["entry_ref"]
    assert all(
        path == "/meta/turn" or path.startswith("/entities")
        for path in entries[0]["covered_paths"]
    )
    assert all(path.startswith("/chars") for path in entries[1]["covered_paths"])
    assert not any(path.startswith("/entities") for path in entries[1]["covered_paths"])


def test_same_unknown_reference_is_minted_once_across_multiple_owner_fields():
    _cfg, store, session_id, branch_id = _runtime()
    result = _apply(
        store,
        session_id,
        branch_id,
        [
            {
                "op": "contact",
                "from_char": "Ada",
                "to_char": "Ada",
                "type": "touching",
                "action": "start",
            }
        ],
    )

    assert not result.quarantined
    assert result.submitted_applied == 1
    assert [op["op"] for op in result.applied] == ["entity_add", "contact"]
    assert result.applied[1]["from_char"] == result.applied[1]["to_char"] == "ada"
    assert list(result.state["entities"]) == ["ada"]
    assert all("_create" not in op for op in result.applied)


def test_list_and_operation_specific_alias_carriers_share_the_general_expansion_path():
    _cfg, store, session_id, branch_id = _runtime()
    position = _apply(
        store,
        session_id,
        branch_id,
        [{"op": "position", "participants": ["Ada", "Bo", "Ada"], "base": "standing"}],
    )
    assert not position.quarantined
    assert [op["op"] for op in position.applied] == [
        "entity_add",
        "entity_add",
        "position",
    ]
    assert [op.get("entity") for op in position.applied[:2]] == ["ada", "bo"]
    assert position.applied[2]["participants"] == ["ada", "bo", "ada"]

    world_flag = _apply(
        store,
        session_id,
        branch_id,
        [{"op": "world_flag", "key": "alert", "value": True, "faction": "Ash Order"}],
        turn=2,
    )
    assert not world_flag.quarantined
    assert [op["op"] for op in world_flag.applied] == ["entity_add", "world_flag"]
    assert world_flag.applied[0]["entity"] == "ash_order"
    assert world_flag.applied[1]["faction"] == "ash_order"
    assert all("_create" not in op for op in position.applied + world_flag.applied)


def test_sequential_owners_do_not_duplicate_an_entity_created_by_the_first_owner():
    _cfg, store, session_id, branch_id = _runtime()
    result = _apply(
        store,
        session_id,
        branch_id,
        [
            {"op": "goal", "char": "Ada", "action": "add", "text": "Leave the vault"},
            {"op": "goal", "char": "Ada", "action": "add", "text": "Find Bo"},
        ],
    )

    assert not result.quarantined
    assert result.submitted_applied == 2
    assert [op["op"] for op in result.applied] == ["entity_add", "goal", "goal"]
    assert sum(op["op"] == "entity_add" for op in result.applied) == 1
    assert list(result.state["entities"]) == ["ada"]
    assert result.state["chars"]["ada"]["goals"] == ["Leave the vault", "Find Bo"]


def test_owning_reducer_rejection_rolls_back_staged_entity_and_journal():
    _cfg, store, session_id, branch_id = _runtime()
    pre = current_state(store, branch_id)
    before_id = store.journal_high_water()

    result = _apply(
        store,
        session_id,
        branch_id,
        [{"op": "effect_remove", "char": "Ada", "effect": "poisoned"}],
    )

    assert result.applied == []
    assert result.submitted_applied == 0
    assert len(result.quarantined) == 1
    assert "nothing to remove" in result.quarantined[0]["reason"]
    assert result.state == pre
    assert current_state(store, branch_id) == pre
    assert store.journal_high_water() == before_id
    assert store.journal_window(branch_id, after_id=before_id, through_id=before_id) == []


def test_invalid_owner_and_caller_supplied_create_carrier_cannot_mint_entities():
    _cfg, store, session_id, branch_id = _runtime()
    invalid = _apply(
        store,
        session_id,
        branch_id,
        [{"op": "goal", "char": "Ada", "action": "explode", "text": "Leave the vault"}],
    )
    assert invalid.applied == []
    assert current_state(store, branch_id)["entities"] == {}

    injected = _apply(
        store,
        session_id,
        branch_id,
        [
            {
                "op": "goal",
                "char": "Ada",
                "action": "add",
                "text": "Leave the vault",
                "_create": [{"eid": "mallory", "name": "Mallory"}],
            }
        ],
        turn=2,
    )
    assert not injected.quarantined
    assert [op["op"] for op in injected.applied] == ["entity_add", "goal"]
    assert list(injected.state["entities"]) == ["ada"]
    assert all("_create" not in op for op in injected.applied)


def test_presence_and_move_remain_refer_only_for_unknown_user_aliases():
    _cfg, store, session_id, branch_id = _runtime()
    before_id = store.journal_high_water()
    result = _apply(
        store,
        session_id,
        branch_id,
        [
            {"op": "presence", "entity": "Ada", "present": True},
            {"op": "move_entity", "entity": "Ada", "to_location": "vault"},
        ],
    )

    assert result.applied == []
    assert result.state["entities"] == {}
    assert store.journal_high_water() == before_id
