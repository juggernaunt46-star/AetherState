from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.genesis import seed_player, seed_rules
from aetherstate.semantic_bootstrap_runtime import (
    ALLOWED_T0_BOOTSTRAP_FAMILIES,
    ALLOWED_T0_BOOTSTRAP_OPERATIONS,
    ALLOWED_T0_BOOTSTRAP_SOURCES,
    SEMANTIC_BOOTSTRAP_PROOF_SCHEMA,
    T0_BOOTSTRAP_OPERATION_FAMILIES,
    SemanticBootstrapProofError,
    build_semantic_bootstrap_proof,
    semantic_bootstrap_persistence_payload,
    validate_semantic_bootstrap_proof,
)
from aetherstate.state import current_state, empty_state, reduce_state
from aetherstate.store import Store


_PERSISTENCE_FIELDS = {
    "schema",
    "session_id",
    "branch_id",
    "turn_index",
    "journal_high_water_before",
    "journal_high_water_after",
    "pre_bootstrap_state",
    "pre_bootstrap_state_fingerprint",
    "post_bootstrap_state",
    "post_bootstrap_state_fingerprint",
    "journal_rows",
    "journal_window_fingerprint",
    "allowed_sources",
    "allowed_operations",
    "allowed_operation_families",
    "transition_projection",
    "transition_projection_fingerprint",
    "fingerprint",
}


def _actual_genesis(*, prior_journal_rows: int = 0) -> dict:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    store = Store(":memory:")
    for index in range(prior_journal_rows):
        _, prior_branch = store.create_session(external_id=f"prior-{index}")
        store.journal(
            prior_branch,
            0,
            0,
            [{"op": "clock_tick", "minutes": 1, "_turn": 0}],
            "rule",
        )
    session_id, branch_id = store.create_session(external_id="bootstrap-proof")
    document = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Name: Akira\n"
                    "Akira is obsessed with bloodshed and addicted to blood."
                ),
            },
            {"role": "assistant", "content": "Rain needles the dock."},
            {"role": "user", "content": "We meet beneath the crane."},
        ]
    }
    before = store.journal_high_water()
    pre_state = current_state(store, branch_id)
    assert pre_state == empty_state()
    assert seed_rules(
        store,
        cfg,
        session_id,
        branch_id,
        document,
        speaker="Akira",
        card_role="character",
    ) == 4
    assert seed_player(store, cfg, session_id, branch_id, document) == 2
    after = store.journal_high_water()
    rows = store.journal_window(branch_id, after_id=before, through_id=after)
    return {
        "store": store,
        "session_id": session_id,
        "branch_id": branch_id,
        "pre_state": pre_state,
        "post_state": current_state(store, branch_id),
        "before": before,
        "after": after,
        "rows": rows,
    }


def _build(fixture: dict, **changes):
    values = {
        "session_id": fixture["session_id"],
        "branch_id": fixture["branch_id"],
        "pre_bootstrap_state": fixture["pre_state"],
        "post_bootstrap_state": fixture["post_state"],
        "journal_high_water_before": fixture["before"],
        "journal_high_water_after": fixture["after"],
        "journal_rows": fixture["rows"],
    }
    values.update(changes)
    return build_semantic_bootstrap_proof(**values)


def _reseal(payload: dict) -> dict:
    body = deepcopy(payload)
    body.pop("fingerprint", None)
    return {**body, "fingerprint": content_fingerprint(body)}


def test_actual_seed_rules_and_player_build_one_immutable_t0_proof() -> None:
    fixture = _actual_genesis()
    state_before_build = current_state(fixture["store"], fixture["branch_id"])
    high_water_before_build = fixture["store"].journal_high_water()

    proof = _build(fixture)
    payload = proof.to_persistence_payload()

    assert set(payload) == _PERSISTENCE_FIELDS
    assert payload["schema"] == SEMANTIC_BOOTSTRAP_PROOF_SCHEMA
    assert payload["session_id"] == fixture["session_id"]
    assert payload["branch_id"] == fixture["branch_id"]
    assert payload["turn_index"] == 0
    assert payload["journal_high_water_before"] == fixture["before"] == 0
    assert payload["journal_high_water_after"] == fixture["after"] == 2
    assert payload["pre_bootstrap_state"] == empty_state()
    assert payload["post_bootstrap_state"] == fixture["post_state"]
    assert payload["pre_bootstrap_state_fingerprint"] == content_fingerprint(empty_state())
    assert payload["post_bootstrap_state_fingerprint"] == content_fingerprint(
        fixture["post_state"]
    )
    assert payload["allowed_sources"] == sorted(ALLOWED_T0_BOOTSTRAP_SOURCES)
    assert payload["allowed_operations"] == sorted(ALLOWED_T0_BOOTSTRAP_OPERATIONS)
    assert set(payload["allowed_operation_families"].values()) == (
        ALLOWED_T0_BOOTSTRAP_FAMILIES
    )
    assert payload["allowed_operation_families"] == dict(
        sorted(T0_BOOTSTRAP_OPERATION_FAMILIES.items())
    )

    kinds = {
        op["op"] for row in payload["journal_rows"] for op in row["ops"]
    }
    assert kinds == {
        "craving",
        "entity_add",
        "obsession",
        "player_seed",
        "presence",
    }
    projection = payload["transition_projection"]
    assert payload["transition_projection_fingerprint"] == projection["fingerprint"]
    assert projection["required_facts"] == []
    assert projection["allowed_facts"] == []
    assert projection["metadata_receipts"] == projection["entries"]
    assert all(entry["visibility"] == "bootstrap" for entry in projection["entries"])
    assert len(projection["entries"]) == 6

    assert current_state(fixture["store"], fixture["branch_id"]) == state_before_build
    assert fixture["store"].journal_high_water() == high_water_before_build

    with pytest.raises(FrozenInstanceError):
        proof.branch_id = "0" * 32  # type: ignore[misc]
    detached_rows = proof.journal_rows
    detached_rows[0]["ops"].clear()
    assert proof.journal_rows == fixture["rows"]
    detached_payload = proof.to_persistence_payload()
    detached_payload["post_bootstrap_state"]["entities"].clear()
    assert proof.post_bootstrap_state == fixture["post_state"]


def test_validation_and_persistence_payload_are_detached_round_trips() -> None:
    proof = _build(_actual_genesis())
    serialized = json.loads(json.dumps(proof.to_persistence_payload()))

    validated = validate_semantic_bootstrap_proof(serialized)
    persisted = semantic_bootstrap_persistence_payload(validated)

    assert validated is not proof
    assert persisted == proof.to_persistence_payload()
    persisted["journal_rows"][0]["ops"].clear()
    assert semantic_bootstrap_persistence_payload(validated) == proof.to_persistence_payload()


def test_store_persists_and_revalidates_the_exact_current_bootstrap_proof() -> None:
    fixture = _actual_genesis()
    proof = _build(fixture)

    persisted = fixture["store"].persist_semantic_bootstrap_proof(proof)
    restored = fixture["store"].semantic_bootstrap_proof(
        fixture["session_id"], fixture["branch_id"]
    )

    assert persisted == proof.to_persistence_payload()
    assert restored is not None
    assert restored.to_persistence_payload() == proof.to_persistence_payload()
    assert fixture["store"].persist_semantic_bootstrap_proof(restored) == persisted


def test_store_retrieval_refuses_a_proof_whose_branch_row_is_missing() -> None:
    fixture = _actual_genesis()
    fixture["store"].persist_semantic_bootstrap_proof(_build(fixture))
    journal_rows = fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    )

    fixture["store"].db.execute(
        "DELETE FROM branches WHERE branch_id=?", (fixture["branch_id"],)
    )

    assert fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    ) == journal_rows
    with pytest.raises(ValueError, match="lost its session binding"):
        fixture["store"].semantic_bootstrap_proof(
            fixture["session_id"], fixture["branch_id"]
        )


def test_store_retrieval_refuses_a_proof_after_active_branch_drift() -> None:
    fixture = _actual_genesis()
    fixture["store"].persist_semantic_bootstrap_proof(_build(fixture))
    journal_rows = fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    )
    _other_session, other_branch = fixture["store"].create_session(
        external_id="bootstrap-active-branch-drift"
    )

    fixture["store"].db.execute(
        "UPDATE sessions SET active_branch=? WHERE session_id=?",
        (other_branch, fixture["session_id"]),
    )

    assert fixture["store"].db.execute(
        "SELECT session_id FROM branches WHERE branch_id=?", (fixture["branch_id"],)
    ).fetchone()["session_id"] == fixture["session_id"]
    assert fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    ) == journal_rows
    with pytest.raises(ValueError, match="lost its session binding"):
        fixture["store"].semantic_bootstrap_proof(
            fixture["session_id"], fixture["branch_id"]
        )


def test_kill_source_pruning_retains_bootstrap_branch_as_dead_provenance() -> None:
    fixture = _actual_genesis()
    fixture["store"].persist_semantic_bootstrap_proof(_build(fixture))
    journal_rows = fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    )

    child_branch = fixture["store"].fork_branch(
        fixture["branch_id"],
        at_pos=0,
        fork_turn=-1,
        kill_source=True,
        prune_keep=0,
    )

    source = fixture["store"].db.execute(
        "SELECT session_id, status FROM branches WHERE branch_id=?",
        (fixture["branch_id"],),
    ).fetchone()
    session = fixture["store"].db.execute(
        "SELECT active_branch FROM sessions WHERE session_id=?",
        (fixture["session_id"],),
    ).fetchone()
    assert source is not None
    assert source["session_id"] == fixture["session_id"]
    assert source["status"] == "dead"
    assert session["active_branch"] == child_branch
    assert fixture["store"].journal_window(
        fixture["branch_id"],
        after_id=fixture["before"],
        through_id=fixture["after"],
    ) == journal_rows
    with pytest.raises(ValueError, match="lost its session binding"):
        fixture["store"].semantic_bootstrap_proof(
            fixture["session_id"], fixture["branch_id"]
        )


def test_store_refuses_cross_bound_drifted_or_corrupt_bootstrap_proofs() -> None:
    fixture = _actual_genesis()
    proof = _build(fixture)
    other_session, other_branch = fixture["store"].create_session(external_id="other")
    cross_bound = proof.to_persistence_payload()
    cross_bound["session_id"] = other_session
    cross_bound["branch_id"] = other_branch
    cross_bound = _reseal(cross_bound)

    with pytest.raises((SemanticBootstrapProofError, ValueError)):
        fixture["store"].persist_semantic_bootstrap_proof(cross_bound)

    fixture["store"].journal(
        fixture["branch_id"],
        0,
        0,
        [{"op": "clock_tick", "minutes": 1, "_turn": 0}],
        "rule",
    )
    with pytest.raises(ValueError, match="journal fence"):
        fixture["store"].persist_semantic_bootstrap_proof(proof)

    clean = _actual_genesis()
    clean["store"].persist_semantic_bootstrap_proof(_build(clean))
    clean["store"].db.execute(
        "UPDATE semantic_bootstrap_proofs SET proof_fingerprint=? WHERE session_id=?",
        ("sha256:" + "0" * 64, clean["session_id"]),
    )
    with pytest.raises(ValueError, match="columns are inconsistent"):
        clean["store"].semantic_bootstrap_proof(clean["session_id"])

    journal_corrupt = _actual_genesis()
    journal_corrupt["store"].persist_semantic_bootstrap_proof(_build(journal_corrupt))
    first_id = journal_corrupt["rows"][0]["id"]
    journal_corrupt["store"].db.execute(
        "UPDATE ops_journal SET source='rule' WHERE id=?", (first_id,)
    )
    with pytest.raises(ValueError, match="journal binding"):
        journal_corrupt["store"].semantic_bootstrap_proof(journal_corrupt["session_id"])


def test_outer_transaction_rolls_back_session_genesis_journal_and_proof_together() -> None:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    store = Store(":memory:")
    document = {
        "messages": [
            {"role": "system", "content": "Name: Akira"},
            {"role": "user", "content": "I wake beneath the crane."},
        ]
    }
    session_id = branch_id = ""

    with pytest.raises(RuntimeError, match="fault after proof"):
        with store.transaction():
            session_id, branch_id = store.create_session(external_id="rollback-bootstrap")
            before = store.journal_high_water()
            pre = current_state(store, branch_id)
            seed_rules(
                store,
                cfg,
                session_id,
                branch_id,
                document,
                speaker="Akira",
                card_role="character",
            )
            seed_player(store, cfg, session_id, branch_id, document)
            after = store.journal_high_water()
            proof = build_semantic_bootstrap_proof(
                session_id=session_id,
                branch_id=branch_id,
                pre_bootstrap_state=pre,
                post_bootstrap_state=current_state(store, branch_id),
                journal_high_water_before=before,
                journal_high_water_after=after,
                journal_rows=store.journal_window(
                    branch_id, after_id=before, through_id=after
                ),
            )
            store.persist_semantic_bootstrap_proof(proof)
            raise RuntimeError("fault after proof")

    assert store.db.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone() is None
    assert store.db.execute(
        "SELECT 1 FROM branches WHERE branch_id=?", (branch_id,)
    ).fetchone() is None
    assert store.db.execute(
        "SELECT 1 FROM semantic_bootstrap_proofs WHERE session_id=?", (session_id,)
    ).fetchone() is None
    assert store.journal_high_water() == 0


def test_high_water_window_may_start_after_unrelated_existing_sessions() -> None:
    fixture = _actual_genesis(prior_journal_rows=2)

    proof = _build(fixture)

    assert proof.journal_high_water_before == 2
    assert proof.journal_high_water_after == 4
    assert [row["id"] for row in proof.journal_rows] == [3, 4]


@pytest.mark.parametrize("source", ["extraction", "rule", "user"])
def test_extraction_rule_and_user_sources_reject(source: str) -> None:
    fixture = _actual_genesis()
    rows = deepcopy(fixture["rows"])
    rows[0]["source"] = source
    with pytest.raises(SemanticBootstrapProofError, match="source"):
        _build(fixture, journal_rows=rows)


def test_explicit_bootstrap_source_is_accepted_without_granting_new_operations() -> None:
    fixture = _actual_genesis()
    rows = deepcopy(fixture["rows"])
    for row in rows:
        row["source"] = "bootstrap"

    proof = _build(fixture, journal_rows=rows)

    assert {entry["source"] for entry in proof.transition_projection["entries"]} == {
        "bootstrap"
    }
    assert all(
        entry["visibility"] == "bootstrap"
        for entry in proof.transition_projection["entries"]
    )


def test_stage_b_and_non_bootstrap_operation_families_reject() -> None:
    fixture = _actual_genesis()
    rows = deepcopy(fixture["rows"])
    rows[0]["ops"][0] = {
        "op": "scene_set",
        "location": "dock",
        "participants": [],
        "phase": "setup",
        "_turn": 0,
    }
    with pytest.raises(SemanticBootstrapProofError, match="inventory|family"):
        _build(fixture, journal_rows=rows)


def test_mixed_t1_rows_and_operations_reject_before_projection() -> None:
    fixture = _actual_genesis()
    history_row = deepcopy(fixture["rows"])
    history_row[0]["turn_hi"] = 1
    with pytest.raises(SemanticBootstrapProofError, match="only T0"):
        _build(fixture, journal_rows=history_row)

    history_op = deepcopy(fixture["rows"])
    history_op[0]["ops"][0]["_turn"] = 1
    with pytest.raises(SemanticBootstrapProofError, match="explicitly T0"):
        _build(fixture, journal_rows=history_op)


def test_missing_duplicate_out_of_order_and_unbounded_journal_ids_reject() -> None:
    fixture = _actual_genesis()
    rows = deepcopy(fixture["rows"])

    with pytest.raises(SemanticBootstrapProofError, match="missing|duplicated|order"):
        _build(fixture, journal_rows=rows[1:])

    duplicated = deepcopy(rows)
    duplicated[1]["id"] = duplicated[0]["id"]
    with pytest.raises(SemanticBootstrapProofError, match="missing|duplicated|order"):
        _build(fixture, journal_rows=duplicated)

    with pytest.raises(SemanticBootstrapProofError, match="missing|duplicated|order"):
        _build(fixture, journal_rows=list(reversed(rows)))

    with pytest.raises(SemanticBootstrapProofError, match="missing|duplicated|order"):
        _build(
            fixture,
            journal_high_water_after=fixture["after"] + 1,
            journal_rows=rows,
        )


def test_post_state_drift_and_unreproduced_mutation_reject() -> None:
    fixture = _actual_genesis()
    drifted = deepcopy(fixture["post_state"])
    drifted["world"]["unproven"] = True
    with pytest.raises(SemanticBootstrapProofError, match="post|replay|reproduce"):
        _build(fixture, post_bootstrap_state=drifted)

    missing_operation = deepcopy(fixture["rows"])
    missing_operation[0]["ops"] = missing_operation[0]["ops"][:-1]
    with pytest.raises(SemanticBootstrapProofError, match="post|replay|reproduce"):
        _build(fixture, journal_rows=missing_operation)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"session_id": "not-a-store-id"}, "session_id"),
        ({"branch_id": "A" * 32}, "branch_id"),
        ({"turn_index": 1}, "T0"),
        ({"turn_index": True}, "turn_index must be an integer"),
    ],
)
def test_unsupported_session_branch_and_turn_identities_reject(
    changes: dict, match: str
) -> None:
    with pytest.raises(SemanticBootstrapProofError, match=match):
        _build(_actual_genesis(), **changes)


def test_session_and_branch_must_be_distinct() -> None:
    fixture = _actual_genesis()
    with pytest.raises(SemanticBootstrapProofError, match="distinct"):
        _build(fixture, branch_id=fixture["session_id"])


def test_rpg_bootstrap_requires_exactly_one_player_seed() -> None:
    fixture = _actual_genesis()
    rows = deepcopy(fixture["rows"][:1])
    post_state = deepcopy(fixture["pre_state"])
    reduce_state(post_state, deepcopy(rows[0]["ops"]))
    with pytest.raises(SemanticBootstrapProofError, match="exactly one player_seed"):
        _build(
            fixture,
            post_bootstrap_state=post_state,
            journal_high_water_after=rows[0]["id"],
            journal_rows=rows,
        )


def test_resealed_detached_tampering_still_fails_internal_proof_relations() -> None:
    proof = _build(_actual_genesis())

    widened_inventory = proof.to_persistence_payload()
    widened_inventory["allowed_sources"].append("user")
    with pytest.raises(SemanticBootstrapProofError, match="inventory"):
        validate_semantic_bootstrap_proof(_reseal(widened_inventory))

    changed_turn = proof.to_persistence_payload()
    changed_turn["turn_index"] = 1
    with pytest.raises(SemanticBootstrapProofError, match="T0"):
        validate_semantic_bootstrap_proof(_reseal(changed_turn))

    drifted_state = proof.to_persistence_payload()
    drifted_state["post_bootstrap_state"]["world"]["unproven"] = True
    drifted_state["post_bootstrap_state_fingerprint"] = content_fingerprint(
        drifted_state["post_bootstrap_state"]
    )
    with pytest.raises(SemanticBootstrapProofError, match="post|replay|reproduce"):
        validate_semantic_bootstrap_proof(_reseal(drifted_state))


@pytest.mark.parametrize("turn_index", [False, True])
def test_resealed_detached_boolean_turn_index_rejects(turn_index: bool) -> None:
    payload = _build(_actual_genesis()).to_persistence_payload()
    payload["turn_index"] = turn_index

    with pytest.raises(
        SemanticBootstrapProofError,
        match="turn_index must be an integer",
    ):
        validate_semantic_bootstrap_proof(_reseal(payload))


@pytest.mark.parametrize("turn_index", [False, True])
def test_store_does_not_persist_resealed_boolean_turn_index(turn_index: bool) -> None:
    fixture = _actual_genesis()
    payload = _build(fixture).to_persistence_payload()
    payload["turn_index"] = turn_index

    with pytest.raises(
        SemanticBootstrapProofError,
        match="turn_index must be an integer",
    ):
        fixture["store"].persist_semantic_bootstrap_proof(_reseal(payload))

    assert fixture["store"].db.execute(
        "SELECT 1 FROM semantic_bootstrap_proofs WHERE session_id=?",
        (fixture["session_id"],),
    ).fetchone() is None
    assert fixture["store"].semantic_bootstrap_proof(
        fixture["session_id"], fixture["branch_id"]
    ) is None


def test_unresealed_top_level_change_fails_the_sealed_proof_fingerprint() -> None:
    payload = _build(_actual_genesis()).to_persistence_payload()
    payload["session_id"] = "0" * 32
    with pytest.raises(SemanticBootstrapProofError, match="proof fingerprint"):
        validate_semantic_bootstrap_proof(payload)
