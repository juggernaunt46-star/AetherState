from __future__ import annotations

import pytest

from aetherstate.canon import content_hash
from aetherstate.store import Store
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    FencedMutationOutput,
    TurnArtifactError,
    TurnDivergenceError,
    build_delivery_proof,
    build_envelope,
    build_pre_mutation_key,
    fingerprint,
    logical_message_id,
)


def _setup(store: Store, pre_state: dict):
    session_id, branch_id = store.create_session()
    key = build_pre_mutation_key(
        session_id=session_id,
        branch_id=branch_id,
        turn_index=0,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I strike."),
        pre_ledger_hash=fingerprint(pre_state),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/fenced-window-test-1",
    )
    return branch_id, key, store.turn_lifecycle.reserve(key)


def _artifact(key: dict, reservation, post_hash: str, *, pre_hash: str) -> EnvelopeArtifact:
    raw = b'{"text":"safe fallback"}'
    graph = {"claims": []}
    proof = build_delivery_proof(
        wire_bytes=raw,
        content_type="application/json",
        renderer_bytes=b"rendered:" + raw,
        visible_bytes=b"visible:" + raw,
        expected_graph=graph,
        observed_graph=graph,
        ledger_graph=graph,
        ledger_root_hash=post_hash,
        logical_message_identity=logical_message_id(key),
        artifact_kind="fallback",
    )
    return build_envelope(
        pre_mutation_key=key,
        attempt_index=reservation.attempt_index,
        attempt_kind="initial",
        request_hash=reservation.request_hash,
        occurrences=[{"occurrence_id": "occ-1", "settlement_ref": "settlement-1"}],
        effects=[{"effect_id": "effect-1", "occurrence_id": "occ-1"}],
        rng_fingerprint=fingerprint({"seed": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=pre_hash,
        mechanics_post_ledger_hash=post_hash,
        fallback_bytes=raw,
        delivery_proof=proof,
    )


def test_factory_observes_exact_pre_state_and_only_new_ordered_branch_rows():
    store = Store(":memory:")
    pre_state = {"hp": 3, "conditions": []}
    branch_id, key, reservation = _setup(store, pre_state)
    store.journal(branch_id, 0, 0, [{"op": "old_same_turn"}], "old")
    other_session, other_branch = store.create_session()
    assert other_session
    store.journal(other_branch, 0, 0, [{"op": "other_branch"}], "other")
    observed_holder = {}

    def mutate():
        store.journal(branch_id, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
        store.journal(branch_id, 0, 1, [{"op": "condition_add", "id": "burning"}], "rule")
        return FencedMutationOutput(
            result={"applied": 2},
            post_state={"hp": 2, "conditions": ["burning"]},
        )

    def factory(observed):
        observed_holder["value"] = observed
        assert observed.pre_state == pre_state
        assert observed.pre_state is not pre_state
        assert observed.pre_hash == fingerprint(pre_state)
        assert [row["source"] for row in observed.journal_rows] == ["rule", "rule"]
        assert [row["ops"][0]["op"] for row in observed.journal_rows] == [
            "hp_adj",
            "condition_add",
        ]
        assert [row["id"] for row in observed.journal_rows] == sorted(
            row["id"] for row in observed.journal_rows
        )
        assert observed.journal_window_fingerprint == fingerprint(
            {
                "schema": "semantic-journal-window/1",
                "branch_id": branch_id,
                "rows": observed.journal_rows,
            }
        )
        return _artifact(
            key,
            reservation,
            observed.post_ledger_hash,
            pre_hash=observed.pre_hash,
        )

    replay = store.turn_lifecycle.commit_mutation_with_fallback_factory(
        reservation,
        mutate,
        factory,
        expected_pre_ledger_hash=fingerprint(pre_state),
        pre_state_callback=lambda: pre_state,
    )

    assert replay.status == "fallback_ready"
    assert observed_holder["value"].result == {"applied": 2}
    assert [row["source"] for row in store.diagnostic_turn(branch_id, 0)["journal"]] == [
        "old",
        "rule",
        "rule",
    ]


def test_factory_journal_write_after_observation_mismatch_rolls_back_window():
    store = Store(":memory:")
    pre_state = {"hp": 3}
    branch_id, key, reservation = _setup(store, pre_state)
    store.journal(branch_id, 0, 0, [{"op": "old_same_turn"}], "old")

    def mutate():
        store.journal(branch_id, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    def factory(observed):
        store.journal(branch_id, 0, 0, [{"op": "late_unobserved"}], "factory")
        return _artifact(
            key,
            reservation,
            observed.post_ledger_hash,
            pre_hash=observed.pre_hash,
        )

    with pytest.raises(TurnArtifactError, match="journal window changed"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation,
            mutate,
            factory,
            expected_pre_ledger_hash=fingerprint(pre_state),
            pre_state_callback=lambda: pre_state,
        )

    assert [row["source"] for row in store.diagnostic_turn(branch_id, 0)["journal"]] == ["old"]
    attempt = store.db.execute(
        "SELECT status, fallback_bytes FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (attempt["status"], attempt["fallback_bytes"]) == ("reserved", None)


def test_pre_state_mismatch_refuses_before_mutation_or_factory():
    store = Store(":memory:")
    pre_state = {"hp": 3}
    branch_id, _key, reservation = _setup(store, pre_state)
    calls = []

    def must_not_run():
        calls.append("called")
        raise AssertionError("callback ran after pre-state mismatch")

    with pytest.raises(TurnDivergenceError, match="pre-ledger or pending-intent state diverged"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation,
            must_not_run,
            must_not_run,
            expected_pre_ledger_hash=fingerprint(pre_state),
            pre_state_callback=lambda: {"hp": 99},
        )

    assert calls == []
    assert store.diagnostic_turn(branch_id, 0)["journal"] == []


def test_duplicate_retry_skips_pre_state_mutation_and_factory_callbacks():
    store = Store(":memory:")
    pre_state = {"hp": 3}
    branch_id, key, reservation = _setup(store, pre_state)
    calls = {"pre": 0, "mutation": 0, "factory": 0}

    def read_pre_state():
        calls["pre"] += 1
        return pre_state

    def mutate():
        calls["mutation"] += 1
        store.journal(branch_id, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    def factory(observed):
        calls["factory"] += 1
        return _artifact(
            key,
            reservation,
            observed.post_ledger_hash,
            pre_hash=observed.pre_hash,
        )

    first = store.turn_lifecycle.commit_mutation_with_fallback_factory(
        reservation,
        mutate,
        factory,
        expected_pre_ledger_hash=fingerprint(pre_state),
        pre_state_callback=read_pre_state,
    )

    def must_not_run():  # pragma: no cover - assertion is that replay returns first
        raise AssertionError("duplicate callback ran")

    duplicate = store.turn_lifecycle.commit_mutation_with_fallback_factory(
        reservation,
        must_not_run,
        must_not_run,
        expected_pre_ledger_hash=fingerprint(pre_state),
        pre_state_callback=must_not_run,
    )

    assert duplicate.payload == first.payload
    assert calls == {"pre": 1, "mutation": 1, "factory": 1}
    assert [row["source"] for row in store.diagnostic_turn(branch_id, 0)["journal"]] == ["rule"]
