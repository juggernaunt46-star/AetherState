"""Live Store admission and replay for atomic generic ``skill_check/1`` settlements."""
from __future__ import annotations

from copy import deepcopy
import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.mechanic_settlement import SKILL_CHECK_CONTRACT
from aetherstate.state import apply_delta, current_state, empty_state, reduce_state
from aetherstate.store import Store


def _runtime(tag: str, *, path=":memory:"):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(path)
    session_id, branch_id = store.create_session(external_id=tag)
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {"op": "player_seed", "entity": "Kael", "card": {
                "stats": {"DEX": 14},
                "skills": {"stealth": 3},
                "abilities": [],
                "resources": {"hp": {"max": 20}},
            }},
        ],
        "genesis",
        cfg,
    )
    assert not seeded.quarantined
    return cfg, store, session_id, branch_id


def _ops(cfg: Config, store: Store, branch_id: str, *, turn: int = 1) -> list[dict]:
    result = tier0.run(
        {"messages": [{
            "role": "user",
            "content": "I slip unseen past the watch. ((aether.check stealth vs 9))",
        }]},
        "new_turn",
        False,
        current_state(store, branch_id),
        cfg,
        random.Random(3),
        turn=turn,
    )
    wrapper = next(
        op for op in result.rule_ops if op.get("op") == "mechanic_settlement_commit"
    )
    assert wrapper["contract_id"] == SKILL_CHECK_CONTRACT
    check = next(member for member in wrapper["members"] if member["op"] == "check")
    assert check["_shape"]["schema"] == "check-roll-shape/1"
    return result.rule_ops


def _semantic_ops(ops: list[dict]) -> list[dict]:
    return [
        deepcopy(op) for op in ops
        if op.get("op") in {
            "semantic_meaning_commit", "semantic_binding_commit", "semantic_frame_commit",
        }
    ]


def _mechanic_ops(ops: list[dict]) -> list[dict]:
    return [
        deepcopy(op) for op in ops
        if op.get("op") == "mechanic_settlement_commit" or "_settlement_ref" in op
    ]


def test_live_targetless_check_persists_one_mechanic_receipt_and_no_damage_receipt():
    cfg, store, session_id, branch_id = _runtime("skill-check-live")
    result = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )

    assert not result.quarantined
    receipt = result.state["mechanic_settlements"][0]["receipt"]
    assert receipt["contract_id"] == SKILL_CHECK_CONTRACT
    assert receipt["outcome"] == "resolved"
    assert receipt["target_post_state"] is None
    assert [row["kind"] for row in receipt["applied_changes"]] == ["mastery"]
    assert len(result.state["rolls"]) == 1
    assert result.state["player"]["kael"]["mastery"]["stealth"] == 3
    assert store.db.execute(
        "SELECT COUNT(*) FROM mechanic_settlement_receipts"
    ).fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
    replay = store.state_at(branch_id, 10**9, reduce_state, empty=empty_state())
    assert replay == result.state == current_state(store, branch_id)


def test_missing_projection_and_raw_v3_fallback_are_atomic_failures():
    for mode in ("missing_projection", "raw_unwrapped"):
        cfg, store, session_id, branch_id = _runtime(f"skill-check-{mode}")
        ops = _ops(cfg, store, branch_id)
        semantics = apply_delta(
            store, session_id, branch_id, 1, _semantic_ops(ops), "rule", cfg,
        )
        assert not semantics.quarantined
        before = deepcopy(current_state(store, branch_id))
        mechanic = _mechanic_ops(ops)
        if mode == "missing_projection":
            attempted = mechanic[:-1]
        else:
            wrapper = next(op for op in mechanic if op["op"] == "mechanic_settlement_commit")
            attempted = deepcopy(wrapper["members"])

        result = apply_delta(
            store, session_id, branch_id, 1, attempted, "rule", cfg,
        )

        assert result.quarantined and not result.applied
        assert result.state == before
        assert result.state["rolls"] == []
        assert "mechanic_settlements" not in result.state
        assert store.db.execute(
            "SELECT COUNT(*) FROM mechanic_settlement_receipts"
        ).fetchone()[0] == 0


def test_exact_retry_is_duplicate_and_changed_same_ref_conflicts_without_mutation():
    cfg, store, session_id, branch_id = _runtime("skill-check-retry")
    ops = _ops(cfg, store, branch_id)
    first = apply_delta(store, session_id, branch_id, 1, ops, "rule", cfg)
    assert not first.quarantined
    before = deepcopy(current_state(store, branch_id))
    journal_count = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]

    exact = apply_delta(
        store, session_id, branch_id, 1, _mechanic_ops(ops), "rule", cfg,
    )
    assert exact.duplicates and not exact.applied and not exact.quarantined
    assert current_state(store, branch_id) == before
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == journal_count

    changed = _mechanic_ops(ops)
    wrapper = next(op for op in changed if op["op"] == "mechanic_settlement_commit")
    check_index = next(
        index for index, member in enumerate(wrapper["members"])
        if member["op"] == "check"
    )
    wrapper["members"][check_index]["result"] += 1
    next(
        op for op in changed if op.get("_settlement_member_index") == check_index
    )["result"] += 1
    conflict = apply_delta(
        store, session_id, branch_id, 1, changed, "rule", cfg,
    )

    assert conflict.quarantined and not conflict.applied
    assert current_state(store, branch_id) == before
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == journal_count


def test_reopen_replays_once_and_later_same_prose_is_a_distinct_occurrence(tmp_path):
    path = tmp_path / "skill-check-settlement.sqlite3"
    cfg, store, session_id, branch_id = _runtime("skill-check-reopen", path=path)
    first = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id, turn=1), "rule", cfg,
    )
    assert not first.quarantined
    after_first = deepcopy(first.state)
    store.db.close()

    reopened = Store(path)
    assert current_state(reopened, branch_id) == after_first
    second = apply_delta(
        reopened,
        session_id,
        branch_id,
        2,
        _ops(cfg, reopened, branch_id, turn=2),
        "rule",
        cfg,
    )

    assert not second.quarantined
    refs = [
        row["receipt"]["settlement_ref"] for row in second.state["mechanic_settlements"]
    ]
    assert len(refs) == 2 and len(set(refs)) == 2
    assert len(second.state["rolls"]) == 2
    assert reopened.db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
