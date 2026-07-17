from __future__ import annotations

from dataclasses import replace
import json
import threading

import pytest

from aetherstate.canon import canonicalize, chain, content_hash
from aetherstate.response_wire import decode_chat_story, encode_chat_story
from aetherstate.store import Store
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    FencedMutationOutput,
    TurnArtifactError,
    TurnDivergenceError,
    TurnReservationConflict,
    build_delivery_proof,
    build_envelope,
    build_pre_mutation_key,
    canonical_claim_projection,
    fingerprint,
    logical_message_id,
    validate_envelope,
    validate_pre_mutation_key,
)


def _setup(store: Store, *, turn: int = 0, text: str = "I strike."):
    sid, bid = store.create_session()
    key = build_pre_mutation_key(
        session_id=sid,
        branch_id=bid,
        turn_index=turn,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash(text),
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    reservation = store.turn_lifecycle.reserve(key)
    return sid, bid, key, reservation


def _canonical_rows(*messages: tuple[str, str]) -> list[tuple[str, str, str]]:
    canonical = canonicalize([
        {"role": role, "content": text}
        for role, text in messages
    ])
    return [
        (message.role, message.content_hash, head)
        for message, head in zip(canonical, chain(canonical))
    ]


def _proof(
    key, raw: bytes, graph=None, *, ledger_root=None, hook=None, artifact_kind="fallback"
):
    graph = {"claims": []} if graph is None else graph
    ledger_root = ledger_root or fingerprint({"hp": 2})
    accepted = artifact_kind == "accepted"
    return build_delivery_proof(
        wire_bytes=raw,
        content_type="application/json",
        renderer_bytes=(
            b'{"adapter":"canonical-text-only/1"}' if accepted else b"rendered:" + raw
        ),
        visible_bytes=(
            decode_chat_story(raw, "application/json").encode("utf-8")
            if accepted else b"visible:" + raw
        ),
        expected_graph=graph,
        observed_graph=graph,
        ledger_graph=graph,
        ledger_root_hash=ledger_root,
        logical_message_identity=logical_message_id(key),
        artifact_kind=artifact_kind,
        phase_hook=hook,
    )


def _accepted_wire(story: str, artifact_ref: str) -> bytes:
    artifact = encode_chat_story(
        story,
        model="turn-lifecycle-test",
        stream=False,
        artifact_ref=artifact_ref,
    )
    assert artifact.content_type == "application/json"
    assert decode_chat_story(artifact.raw, artifact.content_type) == story
    return artifact.raw


def _artifact(
    key,
    request_hash: str,
    *,
    attempt: int = 0,
    kind: str = "initial",
    fallback: bytes = b'{"text":"safe fallback"}',
    accepted: bytes | None = None,
    decision: str = "fallback",
    pre=None,
    mechanics_post=None,
    terminal_post=None,
    declarations=(),
    consumed_intent_id=None,
    graph=None,
    proof_hook=None,
    source=None,
) -> EnvelopeArtifact:
    pre = pre or fingerprint({"hp": 3})
    mechanics_post = mechanics_post or fingerprint({"hp": 2})
    terminal_post = terminal_post or mechanics_post
    selected = fallback if decision == "fallback" else accepted
    assert selected is not None
    return build_envelope(
        pre_mutation_key=key,
        attempt_index=attempt,
        attempt_kind=kind,
        request_hash=request_hash,
        occurrences=[{"occurrence_id": "occ-1", "settlement_ref": "settlement-1"}],
        effects=[{"effect_id": "effect-1", "occurrence_id": "occ-1"}],
        rng_fingerprint=fingerprint({"seed": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=pre,
        mechanics_post_ledger_hash=mechanics_post,
        terminal_post_ledger_hash=terminal_post,
        fallback_bytes=fallback,
        accepted_bytes=accepted,
        delivery_proof=_proof(
            key,
            selected,
            graph,
            ledger_root=terminal_post,
            hook=proof_hook,
            artifact_kind="fallback" if decision == "fallback" else "accepted",
        ),
        decision=decision,
        candidate_declarations=declarations,
        consumed_intent_id=consumed_intent_id,
        source_lifecycle_key=(
            source["source_lifecycle_key"] if source is not None else None
        ),
        source_envelope_fingerprint=(
            source["source_envelope_fingerprint"] if source is not None else None
        ),
    )


def _source_lineage(replay) -> dict[str, str]:
    return {
        "source_lifecycle_key": replay.lifecycle_key,
        "source_envelope_fingerprint": replay.envelope["envelope_fingerprint"],
    }


def _complete_delivery(store: Store, replay):
    store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    return store.turn_lifecycle.complete_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )


def _journal_count(store: Store, source: str | None = None) -> int:
    if source is None:
        row = store.db.execute("SELECT COUNT(*) AS n FROM ops_journal").fetchone()
    else:
        row = store.db.execute(
            "SELECT COUNT(*) AS n FROM ops_journal WHERE source=?", (source,)
        ).fetchone()
    return int(row["n"])


def test_pre_mutation_key_is_deterministic_and_exact():
    store = Store(":memory:")
    sid, bid = store.create_session()
    kwargs = dict(
        session_id=sid,
        branch_id=bid,
        turn_index=3,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I brace."),
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="v3",
    )
    left = build_pre_mutation_key(**kwargs)
    right = build_pre_mutation_key(**kwargs)
    assert left == right == validate_pre_mutation_key(left)
    forged = dict(left, turn_index=4)
    with pytest.raises(TurnArtifactError, match="exact canonical"):
        validate_pre_mutation_key(forged)


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("turn_index", False),
        ("turn_index", True),
        ("accepted_prefix_pos", False),
        ("accepted_prefix_pos", True),
    ],
)
def test_lifecycle_key_indices_reject_boolean_integer_aliases(field, alias):
    store = Store(":memory:")
    session_id, branch_id = store.create_session()
    values = {
        "session_id": session_id,
        "branch_id": branch_id,
        "turn_index": 0,
        "accepted_prefix_pos": 0,
        "accepted_head_hash": EMPTY_PREFIX_HASH,
        "player_input_hash": content_hash("I brace."),
        "pre_ledger_hash": fingerprint({"hp": 3}),
        "pending_intent_fingerprint": fingerprint(None),
        "semantic_contract_version": "semantic-contract/typed-index-1",
    }
    values[field] = alias

    with pytest.raises(TurnArtifactError, match="non-negative integer"):
        build_pre_mutation_key(**values)

    assert store.turn_lifecycle.count() == 0
    store.close()


@pytest.mark.parametrize("alias", [False, True], ids=["false-for-zero", "true-for-one"])
def test_boolean_attempt_alias_cannot_build_validate_select_claim_or_complete(alias):
    store = Store(":memory:")
    session_id, branch_id, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial_artifact = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial = store.turn_lifecycle.commit_mutation_with_fallback(
        initial_artifact, lambda: post
    )
    active_artifact = initial_artifact
    active = initial
    active_reservation = reservation
    source = None
    if alias is True:
        _complete_delivery(store, initial)
        active_reservation = store.turn_lifecycle.reserve_swipe(
            key["lifecycle_key"],
            request_hash=fingerprint({"typed-alias": "true-for-one"}),
            expected_post_ledger_hash=post,
        )
        source = _source_lineage(initial)
        active_artifact = _artifact(
            key,
            active_reservation.request_hash,
            attempt=active_reservation.attempt_index,
            kind="swipe",
            mechanics_post=post,
            source=source,
        )
        active = store.turn_lifecycle.commit_mutation_with_fallback(active_artifact)
    numeric_index = int(alias)
    assert active.attempt_index == active_reservation.attempt_index == numeric_index

    before_build = tuple(store.db.iterdump())
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        _artifact(
            key,
            active_reservation.request_hash,
            attempt=alias,
            kind="initial" if alias is False else "swipe",
            mechanics_post=post,
            source=source,
        )
    assert tuple(store.db.iterdump()) == before_build

    forged_envelope = {
        **active_artifact.envelope,
        "attempt": {**active_artifact.envelope["attempt"], "index": alias},
    }
    forged_envelope.pop("envelope_fingerprint")
    forged_envelope["envelope_fingerprint"] = fingerprint(forged_envelope)
    forged = EnvelopeArtifact(
        forged_envelope,
        active_artifact.fallback_bytes,
        active_artifact.accepted_bytes,
    )
    with pytest.raises(TurnArtifactError, match="envelope attempt index"):
        validate_envelope(forged)

    before_selection = tuple(store.db.iterdump())
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        store.turn_lifecycle.replay(key["lifecycle_key"], attempt_index=alias)
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        store.turn_lifecycle.fallback_artifact(
            key["lifecycle_key"], attempt_index=alias
        )
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        store.turn_lifecycle.claim_delivery(
            active.lifecycle_key,
            alias,
            expected_logical_message_id=active.logical_message_id,
            expected_artifact_digest=active.selected_artifact_digest,
        )
    assert tuple(store.db.iterdump()) == before_selection

    claimed = store.turn_lifecycle.claim_delivery(
        active.lifecycle_key,
        numeric_index,
        expected_logical_message_id=active.logical_message_id,
        expected_artifact_digest=active.selected_artifact_digest,
    )
    after_claim = tuple(store.db.iterdump())
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        store.turn_lifecycle.verify_claimed_delivery(
            active.lifecycle_key,
            alias,
            expected_logical_message_id=active.logical_message_id,
            expected_artifact_digest=active.selected_artifact_digest,
            expected_session_id=session_id,
            expected_branch_id=branch_id,
            expected_turn_index=0,
        )
    with pytest.raises(TurnArtifactError, match="expected_turn_index.*non-negative integer"):
        store.turn_lifecycle.verify_claimed_delivery(
            active.lifecycle_key,
            numeric_index,
            expected_logical_message_id=active.logical_message_id,
            expected_artifact_digest=active.selected_artifact_digest,
            expected_session_id=session_id,
            expected_branch_id=branch_id,
            expected_turn_index=False,
        )
    with pytest.raises(TurnArtifactError, match="attempt_index.*non-negative integer"):
        store.turn_lifecycle.complete_delivery(
            active.lifecycle_key,
            alias,
            expected_logical_message_id=active.logical_message_id,
            expected_artifact_digest=active.selected_artifact_digest,
        )
    assert tuple(store.db.iterdump()) == after_claim

    assert store.turn_lifecycle.verify_claimed_delivery(
        active.lifecycle_key,
        numeric_index,
        expected_logical_message_id=active.logical_message_id,
        expected_artifact_digest=active.selected_artifact_digest,
        expected_session_id=session_id,
        expected_branch_id=branch_id,
        expected_turn_index=0,
    ) == claimed
    assert store.turn_lifecycle.complete_delivery(
        active.lifecycle_key,
        numeric_index,
        expected_logical_message_id=active.logical_message_id,
        expected_artifact_digest=active.selected_artifact_digest,
    ) == claimed
    assert store.turn_lifecycle.replay(
        key["lifecycle_key"], attempt_index=numeric_index
    ) == claimed
    store.close()


def test_boolean_factory_reservation_alias_refuses_before_mutation_callback():
    store = Store(":memory:")
    _, _, _key, reservation = _setup(store)
    forged = replace(reservation, attempt_index=False)
    callback_called = False
    before = tuple(store.db.iterdump())

    def mutate():
        nonlocal callback_called
        callback_called = True
        return FencedMutationOutput(result="forged", post_state={"hp": 2})

    with pytest.raises(TurnReservationConflict, match="attempt index is invalid"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            forged,
            mutate,
            lambda _observed: (_ for _ in ()).throw(
                AssertionError("artifact factory must not run")
            ),
        )

    assert callback_called is False
    assert tuple(store.db.iterdump()) == before
    store.close()


def test_divergent_prefix_refuses_before_any_lifecycle_or_state_touch():
    store = Store(":memory:")
    sid, bid = store.create_session()
    msg = canonicalize([{"role": "assistant", "content": "Established truth."}])[0]
    head = chain([msg])[0]
    store.append_msgs(bid, 0, [(msg.role, msg.content_hash, head)])
    key = build_pre_mutation_key(
        session_id=sid,
        branch_id=bid,
        turn_index=1,
        accepted_prefix_pos=1,
        accepted_head_hash="0" * 16,
        player_input_hash=content_hash("I act."),
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="v1",
    )
    with pytest.raises(TurnDivergenceError, match="head diverged"):
        store.turn_lifecycle.reserve(key)
    assert store.turn_lifecycle.count() == 0
    assert _journal_count(store) == 0


def test_concurrent_duplicate_reservation_has_one_cas_owner(tmp_path):
    path = tmp_path / "lifecycle.sqlite3"
    first = Store(path)
    sid, bid = first.create_session()
    key = build_pre_mutation_key(
        session_id=sid,
        branch_id=bid,
        turn_index=0,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I strike."),
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="v1",
    )
    second = Store(path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def reserve(store):
        try:
            barrier.wait()
            results.append(store.turn_lifecycle.reserve(key))
        except BaseException as exc:  # pragma: no cover - assertion below exposes it
            errors.append(exc)

    threads = [threading.Thread(target=reserve, args=(first,)),
               threading.Thread(target=reserve, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(result.created for result in results) == [False, True]
    assert {result.lifecycle_key for result in results} == {key["lifecycle_key"]}
    assert first.turn_lifecycle.count() == 1


def test_concurrent_duplicate_settlement_runs_reducer_once(tmp_path):
    path = tmp_path / "settlement.sqlite3"
    first = Store(path)
    _, bid, key, reservation = _setup(first)
    second = Store(path)
    post = fingerprint({"hp": 2})
    artifact = _artifact(key, reservation.request_hash, mechanics_post=post)
    barrier = threading.Barrier(2)
    callback_lock = threading.Lock()
    calls = 0
    results = []
    errors = []

    def settle(store):
        nonlocal calls

        def callback():
            nonlocal calls
            with callback_lock:
                calls += 1
            store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
            return post

        try:
            barrier.wait()
            results.append(store.turn_lifecycle.commit_mutation_with_fallback(artifact, callback))
        except BaseException as exc:  # pragma: no cover - assertion below exposes it
            errors.append(exc)

    threads = [threading.Thread(target=settle, args=(first,)),
               threading.Thread(target=settle, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert calls == 1
    assert [result.payload for result in results] == [artifact.fallback_bytes] * 2
    assert _journal_count(first, "rule") == 1


def test_concurrent_factory_retry_invokes_mutation_and_factory_once(tmp_path):
    path = tmp_path / "factory-concurrency.sqlite3"
    first = Store(path)
    _, bid, key, reservation = _setup(first)
    second = Store(path)
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    counts = {"mutation": 0, "factory": 0}
    results = []
    errors = []

    def settle(store):
        def mutate():
            with counter_lock:
                counts["mutation"] += 1
            store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -1}], "factory-concurrent")
            return FencedMutationOutput(result="applied", post_state={"hp": 2})

        def factory(observed):
            with counter_lock:
                counts["factory"] += 1
            return _artifact(
                key,
                reservation.request_hash,
                mechanics_post=observed.post_ledger_hash,
            )

        try:
            barrier.wait()
            results.append(store.turn_lifecycle.commit_mutation_with_fallback_factory(
                reservation, mutate, factory
            ))
        except BaseException as exc:  # pragma: no cover - assertion below exposes it
            errors.append(exc)

    threads = [threading.Thread(target=settle, args=(first,)),
               threading.Thread(target=settle, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert counts == {"mutation": 1, "factory": 1}
    assert [result.payload for result in results] == [b'{"text":"safe fallback"}'] * 2
    assert _journal_count(first, "factory-concurrent") == 1


def test_mechanics_and_fallback_commit_atomically_and_duplicate_never_reruns():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    artifact = _artifact(key, reservation.request_hash, mechanics_post=post)
    calls = 0

    def commit():
        nonlocal calls
        calls += 1
        store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
        return post

    first = store.turn_lifecycle.commit_mutation_with_fallback(artifact, commit)
    duplicate = store.turn_lifecycle.commit_mutation_with_fallback(artifact, commit)
    assert first.payload == duplicate.payload == artifact.fallback_bytes
    assert calls == 1
    assert _journal_count(store, "rule") == 1
    for reason in ("retry", "lost_reply", "reopen"):
        replay = store.turn_lifecycle.replay(key["lifecycle_key"], reason=reason)
        assert replay.payload == artifact.fallback_bytes
        assert replay.payload_hash == first.payload_hash


def test_failure_after_mutation_callback_rolls_back_mechanics_and_artifact():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    artifact = _artifact(key, reservation.request_hash)

    def bad_commit():
        store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -1}], "rule")
        return fingerprint({"wrong": "root"})

    with pytest.raises(TurnArtifactError, match="different ledger root"):
        store.turn_lifecycle.commit_mutation_with_fallback(artifact, bad_commit)
    assert _journal_count(store) == 0
    row = store.db.execute(
        "SELECT status, fallback_bytes FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["fallback_bytes"]) == ("reserved", None)
    with pytest.raises(TurnReservationConflict, match="no proof-carrying"):
        store.turn_lifecycle.replay(key["lifecycle_key"])


@pytest.mark.parametrize(
    "fault_phase",
    [
        "fallback_construction",
        "observed_extraction",
        "observed_expected_comparison",
        "observed_ledger_comparison",
        "receipt_construction",
    ],
)
def test_each_fallback_proof_phase_fault_prevents_settlement(fault_phase):
    store = Store(":memory:")
    _, _, key, _ = _setup(store)

    def fault(phase):
        if phase == fault_phase:
            raise RuntimeError(phase)

    with pytest.raises(RuntimeError, match=fault_phase):
        _proof(key, b"fallback", hook=fault)
    assert _journal_count(store) == 0
    row = store.db.execute(
        "SELECT status, fallback_bytes FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["fallback_bytes"]) == ("reserved", None)


def test_factory_commits_actual_post_state_and_duplicate_skips_both_callbacks():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    calls = {"mutation": 0, "factory": 0}
    actual_state = {"hp": 1, "conditions": ["burning"]}

    def mutate():
        calls["mutation"] += 1
        store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -2}], "factory-rule")
        return FencedMutationOutput(result={"applied": 1}, post_state=actual_state)

    def make_fallback(observed):
        calls["factory"] += 1
        assert observed.result == {"applied": 1}
        assert observed.post_state == actual_state
        assert observed.post_state is not actual_state
        assert observed.post_ledger_hash == fingerprint(actual_state)
        return _artifact(
            key,
            reservation.request_hash,
            mechanics_post=observed.post_ledger_hash,
            graph={"hp": 1, "conditions": ["burning"]},
        )

    first = store.turn_lifecycle.commit_mutation_with_fallback_factory(
        reservation, mutate, make_fallback
    )
    assert first.payload == b'{"text":"safe fallback"}'
    assert first.envelope["ledger"]["mechanics_post_hash"] == fingerprint(actual_state)
    assert calls == {"mutation": 1, "factory": 1}
    assert _journal_count(store, "factory-rule") == 1

    def must_not_run():  # pragma: no cover - duplicate assertion verifies no call
        raise AssertionError("duplicate callback ran")

    duplicate = store.turn_lifecycle.commit_mutation_with_fallback_factory(
        reservation, must_not_run, must_not_run
    )
    assert duplicate.payload == first.payload
    assert calls == {"mutation": 1, "factory": 1}


@pytest.mark.parametrize(
    "fault_phase",
    [
        "fallback_construction",
        "observed_extraction",
        "observed_expected_comparison",
        "observed_ledger_comparison",
        "receipt_construction",
    ],
)
def test_factory_proof_phase_failure_rolls_back_actual_mutation(fault_phase):
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    calls = {"mutation": 0, "factory": 0}

    def mutate():
        calls["mutation"] += 1
        store.journal(bid, 0, 0, [{"op": "hp_adj", "delta": -1}], "factory-phase")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    def fault(phase):
        if phase == fault_phase:
            raise RuntimeError(phase)

    def factory(observed):
        calls["factory"] += 1
        return _artifact(
            key,
            reservation.request_hash,
            mechanics_post=observed.post_ledger_hash,
            proof_hook=fault,
        )

    with pytest.raises(RuntimeError, match=fault_phase):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, mutate, factory
        )
    assert calls == {"mutation": 1, "factory": 1}
    assert _journal_count(store, "factory-phase") == 0
    row = store.db.execute(
        "SELECT status, fallback_bytes FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["fallback_bytes"]) == ("reserved", None)


def test_factory_mutation_or_render_failure_rolls_back_without_partial_artifact():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    factory_calls = 0

    def mutation_fails():
        store.journal(bid, 0, 0, [{"op": "hp_adj"}], "mutation-fails")
        raise RuntimeError("mutation failed")

    def factory(_observed):
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError("renderer failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, mutation_fails, factory
        )
    assert factory_calls == 0
    assert _journal_count(store, "mutation-fails") == 0

    def mutation_succeeds():
        store.journal(bid, 0, 0, [{"op": "hp_adj"}], "render-fails")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    with pytest.raises(RuntimeError, match="renderer failed"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, mutation_succeeds, factory
        )
    assert factory_calls == 1
    assert _journal_count(store, "render-fails") == 0
    assert store.db.execute(
        "SELECT status FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()["status"] == "reserved"


@pytest.mark.parametrize("tamper", ["post_hash", "wire"])
def test_factory_rejects_tampered_artifact_and_rolls_back(tamper):
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)

    def mutate():
        store.journal(bid, 0, 0, [{"op": "hp_adj"}], "factory-tamper")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    def factory(observed):
        post = observed.post_ledger_hash
        if tamper == "post_hash":
            post = fingerprint({"hp": 999})
        artifact = _artifact(
            key, reservation.request_hash, mechanics_post=post
        )
        if tamper == "wire":
            return EnvelopeArtifact(artifact.envelope, b"different exact bytes")
        return artifact

    pattern = "actual reducer post-ledger root" if tamper == "post_hash" else "fallback bytes"
    with pytest.raises(TurnArtifactError, match=pattern):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, mutate, factory
        )
    assert _journal_count(store, "factory-tamper") == 0
    assert store.db.execute(
        "SELECT status FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()["status"] == "reserved"


def test_factory_revalidates_reserved_transcript_before_invoking_callbacks():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    # A later message means this reservation no longer owns the branch head.
    store.append_msgs(bid, 1, [("assistant", content_hash("later"), "a" * 16)])
    calls = 0

    def must_not_run():
        nonlocal calls
        calls += 1
        raise AssertionError("callback ran after divergence")

    with pytest.raises(TurnDivergenceError, match="advanced past"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, must_not_run, must_not_run
        )
    assert calls == 0
    assert store.db.execute(
        "SELECT status FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()["status"] == "reserved"


def test_role_different_claim_graphs_compare_by_strict_causal_projection():
    store = Store(":memory:")
    _, _, key, _ = _setup(store)
    causal = {
        "occurrence_ref": "occ-1",
        "cause_ref": "cause-1",
        "actor_id": "player",
        "subject_ids": ["enemy-1"],
        "kind": "harm",
        "polarity": "positive",
        "actuality": "actual",
        "time_scope": "current",
        "multiplicity": 1,
        "detail": "hp",
        "amount": -2,
    }
    expected = {
        "role": "offline_expected",
        "issuer": "offline.human/1",
        "claims": [{**causal, "construction_ref": "construction-1", "claim_ref": "expected"}],
    }
    observed = {
        "role": "fallback",
        "issuer": "aetherstate.truth-gate/1",
        "channel": "fallback_bytes",
        "claims": [{
            **causal,
            "authority_ref": "construction-1",
            "construction_ref": "span-specific-construction",
            "claim_ref": "fallback",
            "span_start": 0,
            "span_end": 12,
        }],
    }
    ledger = {
        "issuer": "ledger.reducer/1",
        "claims": [{**causal, "construction_ref": "construction-1", "claim_ref": "ledger"}],
    }
    assert canonical_claim_projection(expected) == canonical_claim_projection(observed) \
        == canonical_claim_projection(ledger)
    proof = build_delivery_proof(
        wire_bytes=b"Enemy loses 2 HP.",
        content_type="text/plain",
        renderer_bytes=b"Enemy loses 2 HP.",
        visible_bytes=b"Enemy loses 2 HP.",
        expected_graph=expected,
        observed_graph=observed,
        ledger_graph=ledger,
        ledger_root_hash=fingerprint({"enemy-1": {"hp": 1}}),
        logical_message_identity=logical_message_id(key),
    )
    assert proof["comparisons"]["mode"] == "semantic_claim_multiset"
    assert proof["expected_graph"] != proof["observed_graph"]

    changed = {**ledger, "claims": [{
        **causal, "amount": -3, "construction_ref": "construction-1", "claim_ref": "ledger",
    }]}
    with pytest.raises(TurnArtifactError, match="expectation and ledger"):
        build_delivery_proof(
            wire_bytes=b"Enemy loses 2 HP.",
            content_type="text/plain",
            renderer_bytes=b"Enemy loses 2 HP.",
            visible_bytes=b"Enemy loses 2 HP.",
            expected_graph=expected,
            observed_graph=observed,
            ledger_graph=changed,
            ledger_root_hash=fingerprint({"enemy-1": {"hp": 1}}),
            logical_message_identity=logical_message_id(key),
        )


def test_crash_boundaries_reopen_fallback_then_exact_accepted_bytes():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    base = _artifact(key, reservation.request_hash, mechanics_post=post)

    # Crash before settlement: reservation alone has no visible/replayable artifact.
    with pytest.raises(TurnReservationConflict):
        store.turn_lifecycle.replay(key["lifecycle_key"])

    store.turn_lifecycle.commit_mutation_with_fallback(base, lambda: post)
    # Crash after reducer commit/before upstream, during stream, or after candidate generation all
    # reopen to the already durable truthful fallback. Candidate bytes are not stored yet.
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == base.fallback_bytes
    assert store.db.execute(
        "SELECT accepted_bytes FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()["accepted_bytes"] is None

    accepted = _accepted_wire("Creative but proven.", "crash-boundary-accepted")
    terminal = _artifact(
        key,
        reservation.request_hash,
        mechanics_post=post,
        accepted=accepted,
        decision="accept",
        graph={"claims": [{"kind": "hp", "value": 2}]},
    )
    store.turn_lifecycle.promote_candidate(terminal)
    # Crash after promotion/before visibility reopens exact accepted bytes without rerunning state.
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == accepted


def test_delivery_claim_is_durable_idempotent_and_freezes_selected_fallback():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    base = _artifact(key, reservation.request_hash, mechanics_post=post)
    replay = store.turn_lifecycle.commit_mutation_with_fallback(base, lambda: post)

    claimed = store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    duplicate = store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    assert claimed.payload == duplicate.payload == replay.payload
    assert claimed.payload_hash == replay.payload_hash
    row = store.db.execute(
        "SELECT * FROM semantic_turn_delivery_claims WHERE lifecycle_key=?",
        (replay.lifecycle_key,),
    ).fetchone()
    assert row["status"] == "claimed"
    assert row["logical_message_id"] == replay.logical_message_id
    assert row["artifact_digest"] == replay.selected_artifact_digest

    with pytest.raises(TurnReservationConflict, match="identity does not match"):
        store.turn_lifecycle.claim_delivery(
            replay.lifecycle_key,
            replay.attempt_index,
            expected_logical_message_id=replay.logical_message_id,
            expected_artifact_digest=fingerprint({"wrong": "artifact"}),
        )

    accepted = _accepted_wire("Late candidate.", "claimed-fallback-late-candidate")
    terminal = _artifact(
        key,
        reservation.request_hash,
        mechanics_post=post,
        accepted=accepted,
        decision="accept",
    )
    with pytest.raises(TurnReservationConflict, match="cannot be replaced"):
        store.turn_lifecycle.promote_candidate(terminal)
    assert store.turn_lifecycle.replay(replay.lifecycle_key).payload == replay.payload


def test_delivery_claim_refuses_stale_attempt_after_swipe_becomes_active():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial_replay = store.turn_lifecycle.commit_mutation_with_fallback(initial, lambda: post)
    store.turn_lifecycle.claim_delivery(
        initial_replay.lifecycle_key,
        initial_replay.attempt_index,
        expected_logical_message_id=initial_replay.logical_message_id,
        expected_artifact_digest=initial_replay.selected_artifact_digest,
    )
    store.turn_lifecycle.complete_delivery(
        initial_replay.lifecycle_key,
        initial_replay.attempt_index,
        expected_logical_message_id=initial_replay.logical_message_id,
        expected_artifact_digest=initial_replay.selected_artifact_digest,
    )

    swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "delivery"}),
        expected_post_ledger_hash=post,
    )
    swipe_fallback = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=b'{"text":"new swipe"}',
        source=_source_lineage(initial_replay),
    )
    swipe_replay = store.turn_lifecycle.commit_mutation_with_fallback(swipe_fallback)
    with pytest.raises(TurnReservationConflict, match="inactive or stale"):
        store.turn_lifecycle.claim_delivery(
            initial_replay.lifecycle_key,
            initial_replay.attempt_index,
            expected_logical_message_id=initial_replay.logical_message_id,
            expected_artifact_digest=initial_replay.selected_artifact_digest,
        )
    active = store.turn_lifecycle.claim_delivery(
        swipe_replay.lifecycle_key,
        swipe_replay.attempt_index,
        expected_logical_message_id=swipe_replay.logical_message_id,
        expected_artifact_digest=swipe_replay.selected_artifact_digest,
    )
    assert active.payload == b'{"text":"new swipe"}'
    assert store.db.execute(
        "SELECT COUNT(*) AS n FROM semantic_turn_delivery_claims WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()["n"] == 2


def test_provisional_declarations_commit_with_accepted_artifact_or_rollback_completely():
    store = Store(":memory:")
    _, bid, key, reservation = _setup(store)
    mechanics_post = fingerprint({"hp": 2, "door": "closed"})
    terminal_post = fingerprint({"hp": 2, "door": "open"})
    base = _artifact(key, reservation.request_hash, mechanics_post=mechanics_post)
    store.turn_lifecycle.commit_mutation_with_fallback(base, lambda: mechanics_post)
    accepted = _accepted_wire("The admitted door opens.", "provisional-door-open")
    terminal = _artifact(
        key,
        reservation.request_hash,
        mechanics_post=mechanics_post,
        terminal_post=terminal_post,
        accepted=accepted,
        decision="accept",
        declarations=[{"declaration_id": "decl-1", "kind": "door_open", "target": "door"}],
        graph={"claims": [{"kind": "door", "state": "open"}]},
    )

    def bad_provisional():
        store.journal(bid, 0, 0, [{"op": "door", "open": True}], "candidate")
        return fingerprint({"wrong": True})

    with pytest.raises(TurnArtifactError, match="wrong ledger root"):
        store.turn_lifecycle.promote_candidate(terminal, bad_provisional)
    assert _journal_count(store, "candidate") == 0
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == base.fallback_bytes

    def good_provisional():
        store.journal(bid, 0, 0, [{"op": "door", "open": True}], "candidate")
        return terminal_post

    promoted = store.turn_lifecycle.promote_candidate(terminal, good_provisional)
    assert promoted.payload == accepted
    assert _journal_count(store, "candidate") == 1

    rejected = _artifact(key, reservation.request_hash, mechanics_post=mechanics_post)
    with pytest.raises(TurnArtifactError, match="rejection cannot run"):
        store.turn_lifecycle.promote_candidate(rejected, good_provisional)
    assert _journal_count(store, "candidate") == 1


def test_single_use_pending_intent_collision_rolls_back_second_settlement():
    store = Store(":memory:")
    sid, bid, first_key, first_reservation = _setup(store, turn=0)
    post = fingerprint({"hp": 2})
    first = _artifact(
        first_key, first_reservation.request_hash,
        mechanics_post=post, consumed_intent_id="intent-7",
    )
    store.turn_lifecycle.commit_mutation_with_fallback(first, lambda: post)

    second_key = build_pre_mutation_key(
        session_id=sid,
        branch_id=bid,
        turn_index=1,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I evade."),
        pre_ledger_hash=fingerprint({"hp": 2}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    second_reservation = store.turn_lifecycle.reserve(second_key)
    second = _artifact(
        second_key, second_reservation.request_hash,
        mechanics_post=post, consumed_intent_id="intent-7",
    )

    def second_commit():
        store.journal(bid, 1, 1, [{"op": "enemy_intent_consume"}], "rule-second")
        return post

    with pytest.raises(TurnReservationConflict, match="single-use intent"):
        store.turn_lifecycle.commit_mutation_with_fallback(second, second_commit)
    assert _journal_count(store, "rule-second") == 0
    row = store.db.execute(
        "SELECT status FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (second_key["lifecycle_key"],),
    ).fetchone()
    assert row["status"] == "reserved"


def test_factory_persistence_failure_rolls_back_mutation_and_fallback():
    store = Store(":memory:")
    sid, bid, first_key, first_reservation = _setup(store, turn=0)
    post = fingerprint({"hp": 2})
    first = _artifact(
        first_key,
        first_reservation.request_hash,
        mechanics_post=post,
        consumed_intent_id="intent-already-consumed",
    )
    store.turn_lifecycle.commit_mutation_with_fallback(first, lambda: post)

    key = build_pre_mutation_key(
        session_id=sid,
        branch_id=bid,
        turn_index=1,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I brace."),
        pre_ledger_hash=fingerprint({"hp": 2}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    reservation = store.turn_lifecycle.reserve(key)

    def mutate():
        store.journal(bid, 1, 1, [{"op": "enemy_intent_consume"}], "factory-persist")
        return FencedMutationOutput(result="applied", post_state={"hp": 2})

    def factory(observed):
        return _artifact(
            key,
            reservation.request_hash,
            mechanics_post=observed.post_ledger_hash,
            consumed_intent_id="intent-already-consumed",
        )

    with pytest.raises(TurnReservationConflict, match="single-use intent"):
        store.turn_lifecycle.commit_mutation_with_fallback_factory(
            reservation, mutate, factory
        )
    assert _journal_count(store, "factory-persist") == 0
    row = store.db.execute(
        "SELECT a.status, a.fallback_bytes, l.status AS lifecycle_status"
        " FROM semantic_turn_attempts a JOIN semantic_turn_lifecycles l"
        " ON l.lifecycle_key=a.lifecycle_key WHERE a.lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["fallback_bytes"], row["lifecycle_status"]) \
        == ("reserved", None, "reserved")


def test_swipe_moves_active_pointer_only_after_proof_and_requires_unchanged_ledger():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial_replay = store.turn_lifecycle.commit_mutation_with_fallback(initial, lambda: post)
    _complete_delivery(store, initial_replay)

    swipe_request = fingerprint({"swipe": 1})
    swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"], request_hash=swipe_request, expected_post_ledger_hash=post
    )
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).attempt_index == 0
    swipe_fallback = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=b'{"text":"swipe fallback"}',
        source=_source_lineage(initial_replay),
    )
    store.turn_lifecycle.commit_mutation_with_fallback(swipe_fallback)
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).attempt_index == swipe.attempt_index

    accepted = _accepted_wire("Swipe accepted.", "swipe-accepted")
    swipe_terminal = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=swipe_fallback.fallback_bytes,
        accepted=accepted,
        decision="accept",
        source=swipe_fallback.envelope["lineage"],
    )
    store.turn_lifecycle.promote_candidate(swipe_terminal)
    promoted = store.turn_lifecycle.replay(key["lifecycle_key"])
    assert promoted.payload == accepted

    with pytest.raises(TurnDivergenceError, match="ledger root differs"):
        store.turn_lifecycle.reserve_swipe(
            key["lifecycle_key"],
            request_hash=fingerprint({"swipe": 2}),
            expected_post_ledger_hash=fingerprint({"hp": 999}),
        )
    store.turn_lifecycle.claim_delivery(
        promoted.lifecycle_key,
        promoted.attempt_index,
        expected_logical_message_id=promoted.logical_message_id,
        expected_artifact_digest=promoted.selected_artifact_digest,
    )
    store.turn_lifecycle.complete_delivery(
        promoted.lifecycle_key,
        promoted.attempt_index,
        expected_logical_message_id=promoted.logical_message_id,
        expected_artifact_digest=promoted.selected_artifact_digest,
    )
    refused = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": 3}),
        expected_post_ledger_hash=post,
        allow_regate=False,
    )
    assert refused.status == "refused"
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == accepted


def test_incomplete_initial_delivery_replays_before_the_first_swipe_reservation():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    replay = store.turn_lifecycle.commit_mutation_with_fallback(initial, lambda: post)
    before = tuple(store.db.iterdump())

    recovered = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "must-recover-initial"}),
        expected_post_ledger_hash=post,
    )

    assert not recovered.created
    assert recovered.attempt_index == replay.attempt_index == 0
    assert recovered.request_hash == reservation.request_hash
    assert recovered.status == replay.status
    assert tuple(store.db.iterdump()) == before

    store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    claimed_retry = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "claim-is-not-completion"}),
        expected_post_ledger_hash=post,
    )
    assert not claimed_retry.created
    assert claimed_retry.attempt_index == replay.attempt_index

    store.turn_lifecycle.complete_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    first_swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "now-authorized"}),
        expected_post_ledger_hash=post,
    )
    assert first_swipe.created
    assert first_swipe.attempt_index == 1
    assert first_swipe.status == "reserved"


def test_unclaimed_terminal_swipe_replays_before_a_new_reservation_until_claimed():
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial_replay = store.turn_lifecycle.commit_mutation_with_fallback(
        initial, lambda: post
    )
    _complete_delivery(store, initial_replay)
    swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"transport": "swipe-one", "count": 0}),
        expected_post_ledger_hash=post,
    )
    swipe_fallback = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=b'{"text":"durable swipe fallback"}',
        source=_source_lineage(initial_replay),
    )
    swipe_replay = store.turn_lifecycle.commit_mutation_with_fallback(swipe_fallback)

    ambiguous_retry = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"transport": "same bytes", "count": 1}),
        expected_post_ledger_hash=post,
    )

    assert not ambiguous_retry.created
    assert ambiguous_retry.attempt_index == swipe_replay.attempt_index
    assert ambiguous_retry.request_hash == swipe.request_hash
    assert ambiguous_retry.status == swipe_replay.status
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()[0] == 2
    assert store.turn_lifecycle.replay(key["lifecycle_key"]) == swipe_replay

    store.turn_lifecycle.claim_delivery(
        swipe_replay.lifecycle_key,
        swipe_replay.attempt_index,
        expected_logical_message_id=swipe_replay.logical_message_id,
        expected_artifact_digest=swipe_replay.selected_artifact_digest,
    )
    claimed_retry = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"transport": "after claim", "count": 1}),
        expected_post_ledger_hash=post,
    )
    assert not claimed_retry.created
    assert claimed_retry.attempt_index == swipe_replay.attempt_index
    store.turn_lifecycle.complete_delivery(
        swipe_replay.lifecycle_key,
        swipe_replay.attempt_index,
        expected_logical_message_id=swipe_replay.logical_message_id,
        expected_artifact_digest=swipe_replay.selected_artifact_digest,
    )
    later = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"transport": "later swipe", "count": 1}),
        expected_post_ledger_hash=post,
    )
    assert later.created
    assert later.attempt_index == swipe_replay.attempt_index + 1
    assert later.status == "reserved"


@pytest.mark.parametrize("corruption", ["missing_claim", "wrong_digest", "wrong_status"])
def test_corrupt_delivery_completion_cannot_unlock_a_later_swipe(corruption):
    store = Store(":memory:")
    _, _, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial_replay = store.turn_lifecycle.commit_mutation_with_fallback(
        initial, lambda: post
    )
    _complete_delivery(store, initial_replay)
    swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "completion-corruption"}),
        expected_post_ledger_hash=post,
    )
    swipe_artifact = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        source=_source_lineage(initial_replay),
    )
    replay = store.turn_lifecycle.commit_mutation_with_fallback(swipe_artifact)
    store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    store.turn_lifecycle.complete_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    with store.transaction():
        if corruption == "missing_claim":
            store.db.execute(
                "DELETE FROM semantic_turn_delivery_claims"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (replay.lifecycle_key, replay.attempt_index),
            )
        elif corruption == "wrong_digest":
            store.db.execute(
                "UPDATE semantic_turn_delivery_completions SET artifact_digest=?"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (
                    fingerprint({"wrong": "completion-artifact"}),
                    replay.lifecycle_key,
                    replay.attempt_index,
                ),
            )
        else:
            store.db.execute(
                "UPDATE semantic_turn_delivery_completions SET status='partial'"
                " WHERE lifecycle_key=? AND attempt_index=?",
                (replay.lifecycle_key, replay.attempt_index),
            )

    with pytest.raises(TurnReservationConflict, match="completion differs"):
        store.turn_lifecycle.reserve_swipe(
            key["lifecycle_key"],
            request_hash=fingerprint({"swipe": "must-not-unlock"}),
            expected_post_ledger_hash=post,
        )
    assert store.turn_lifecycle.replay(key["lifecycle_key"]) == replay
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()[0] == 2


def test_branch_lifecycle_cleanup_removes_delivery_completion_receipts():
    store = Store(":memory:")
    _, branch, key, reservation = _setup(store)
    post = fingerprint({"hp": 2})
    initial = _artifact(key, reservation.request_hash, mechanics_post=post)
    initial_replay = store.turn_lifecycle.commit_mutation_with_fallback(
        initial, lambda: post
    )
    _complete_delivery(store, initial_replay)
    swipe = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "cleanup"}),
        expected_post_ledger_hash=post,
    )
    swipe_artifact = _artifact(
        key,
        swipe.request_hash,
        attempt=swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        source=_source_lineage(initial_replay),
    )
    replay = store.turn_lifecycle.commit_mutation_with_fallback(swipe_artifact)
    store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    store.turn_lifecycle.complete_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    with store.transaction():
        store.turn_lifecycle.delete_branch(branch)

    for table in (
        "semantic_turn_lifecycles",
        "semantic_turn_attempts",
        "semantic_turn_delivery_claims",
        "semantic_turn_delivery_completions",
    ):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_fork_rebases_envelopes_and_keeps_parent_artifact_isolated():
    store = Store(":memory:")
    sid, bid, key, reservation = _setup(store)
    player_hash = key["player_input_hash"]
    # The exact current Player message is included in the fork prefix.
    store.append_msgs(bid, 0, _canonical_rows(("user", "I strike.")))
    store.record_turn(bid, 0, "normal", "normal")
    store.write_turn_hashes(bid, 0, user_hash=player_hash)
    post = fingerprint({"hp": 2})
    base = _artifact(key, reservation.request_hash, mechanics_post=post)
    store.turn_lifecycle.commit_mutation_with_fallback(base, lambda: post)

    child = store.fork_branch(bid, at_pos=1, fork_turn=0)
    child_row = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?", (child,)
    ).fetchone()
    assert child_row is not None
    assert child_row["lifecycle_key"] != key["lifecycle_key"]
    child_replay = store.turn_lifecycle.replay(child_row["lifecycle_key"])
    parent_replay = store.turn_lifecycle.replay(key["lifecycle_key"])
    assert child_replay.payload == parent_replay.payload == base.fallback_bytes
    assert child_replay.envelope["envelope_fingerprint"] \
        != parent_replay.envelope["envelope_fingerprint"]
    assert child_replay.envelope["lineage"]["source_lifecycle_key"] == key["lifecycle_key"]
    # A fork owns a distinct logical message identity.  Source delivery authority is not copied;
    # the child must prove completion of its own active inherited artifact before a child swipe.
    _complete_delivery(store, child_replay)

    child_swipe = store.turn_lifecycle.reserve_swipe(
        child_row["lifecycle_key"],
        request_hash=fingerprint({"child_swipe": 1}),
        expected_post_ledger_hash=post,
    )
    child_key = validate_pre_mutation_key(child_replay.envelope["pre_mutation_key"])
    child_fallback = _artifact(
        child_key,
        child_swipe.request_hash,
        attempt=child_swipe.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=b"child-only",
        source=_source_lineage(child_replay),
    )
    store.turn_lifecycle.commit_mutation_with_fallback(child_fallback)
    assert store.turn_lifecycle.replay(child_row["lifecycle_key"]).payload == b"child-only"
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == base.fallback_bytes


def test_direct_fork_refuses_reserved_inherited_semantic_turn_without_child_mutation():
    store = Store(":memory:")
    _sid, branch, key, _reservation = _setup(store)
    store.append_msgs(branch, 0, _canonical_rows(("user", "I strike.")))
    store.record_turn(branch, 0, "normal", "normal")
    store.write_turn_hashes(branch, 0, user_hash=key["player_input_hash"])
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="reserved or nonterminal"):
        store.fork_branch(branch, at_pos=1, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize(
    ("at_pos", "fork_turn"),
    [
        (1, -1),  # transcript carries the Player input while the turn/state cut omits it
        (0, 0),   # turn/state carries the semantic turn while the transcript cut omits it
    ],
)
def test_direct_fork_refuses_split_semantic_lifecycle_axes_without_mutation(
    at_pos, fork_turn
):
    store = Store(":memory:")
    _sid, branch, key, _reservation = _setup(store)
    store.append_msgs(branch, 0, _canonical_rows(("user", "I strike.")))
    store.record_turn(branch, 0, "normal", "normal")
    store.write_turn_hashes(branch, 0, user_hash=key["player_input_hash"])
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="splits transcript and turn"):
        store.fork_branch(branch, at_pos=at_pos, fork_turn=fork_turn)

    assert tuple(store.db.iterdump()) == before


def test_fully_terminal_nested_fork_reopens_each_rebased_prefix_artifact_exactly():
    store = Store(":memory:")
    _sid, branch, key, first_reservation = _setup(store)
    store.append_msgs(branch, 0, _canonical_rows(("user", "I strike.")))
    store.record_turn(branch, 0, "normal", "normal")
    store.write_turn_hashes(branch, 0, user_hash=key["player_input_hash"])
    first_post = fingerprint({"hp": 2})
    first = _artifact(key, first_reservation.request_hash, mechanics_post=first_post)
    parent_replay = store.turn_lifecycle.commit_mutation_with_fallback(
        first, lambda: first_post
    )
    child = store.fork_branch(branch, at_pos=1, fork_turn=0)
    child_first = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?", (child,)
    ).fetchone()
    assert child_first is not None
    child_first_replay = store.turn_lifecycle.replay(
        child_first["lifecycle_key"], reason="reopen"
    )
    assert child_first_replay.payload == parent_replay.payload

    second_player_hash = content_hash("I brace.")
    first_message = store.get_msgs(child)[0]
    second_row = _canonical_rows(
        ("user", "I strike."),
        ("user", "I brace."),
    )[1]
    store.append_msgs(child, 1, [second_row])
    store.record_turn(child, 1, "normal", "normal")
    store.write_turn_hashes(child, 1, user_hash=second_player_hash)
    second_key = build_pre_mutation_key(
        session_id=child_first["session_id"],
        branch_id=child,
        turn_index=1,
        accepted_prefix_pos=1,
        accepted_head_hash=first_message["chain_hash"],
        player_input_hash=second_player_hash,
        pre_ledger_hash=first_post,
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    second_reservation = store.turn_lifecycle.reserve(second_key)
    second_post = fingerprint({"hp": 1})
    second = _artifact(
        second_key,
        second_reservation.request_hash,
        pre=first_post,
        mechanics_post=second_post,
    )
    second_replay = store.turn_lifecycle.commit_mutation_with_fallback(
        second, lambda: second_post
    )

    nested = store.fork_branch(child, at_pos=2, fork_turn=1)
    nested_lifecycles = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=? ORDER BY turn_index",
        (nested,),
    ).fetchall()
    assert len(nested_lifecycles) == 2
    nested_replays = [
        store.turn_lifecycle.replay(row["lifecycle_key"], reason="reopen")
        for row in nested_lifecycles
    ]
    assert [replay.payload for replay in nested_replays] == [
        child_first_replay.payload,
        second_replay.payload,
    ]
    assert store.turn_lifecycle.replay(
        child_first["lifecycle_key"], reason="reopen"
    ) == child_first_replay


@pytest.mark.parametrize(
    "corruption",
    ["missing_active_pointer", "missing_active_row", "nonterminal_active", "corrupt_artifact"],
)
def test_direct_fork_refuses_every_incomplete_or_corrupt_active_artifact_before_mutation(
    corruption,
):
    store = Store(":memory:")
    _sid, branch, key, reservation = _setup(store)
    store.append_msgs(branch, 0, _canonical_rows(("user", "I strike.")))
    store.record_turn(branch, 0, "normal", "normal")
    store.write_turn_hashes(branch, 0, user_hash=key["player_input_hash"])
    post = fingerprint({"hp": 2})
    store.turn_lifecycle.commit_mutation_with_fallback(
        _artifact(key, reservation.request_hash, mechanics_post=post), lambda: post
    )
    with store.transaction():
        if corruption == "missing_active_pointer":
            store.db.execute(
                "UPDATE semantic_turn_lifecycles SET active_attempt_index=NULL"
                " WHERE lifecycle_key=?",
                (key["lifecycle_key"],),
            )
        elif corruption == "missing_active_row":
            store.db.execute(
                "UPDATE semantic_turn_lifecycles SET active_attempt_index=99"
                " WHERE lifecycle_key=?",
                (key["lifecycle_key"],),
            )
        elif corruption == "nonterminal_active":
            store.db.execute(
                "UPDATE semantic_turn_attempts SET status='reserved'"
                " WHERE lifecycle_key=? AND attempt_index=0",
                (key["lifecycle_key"],),
            )
        else:
            store.db.execute(
                "UPDATE semantic_turn_attempts SET fallback_bytes=?"
                " WHERE lifecycle_key=? AND attempt_index=0",
                (b'{"text":"corrupt"}', key["lifecycle_key"]),
            )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict):
        store.fork_branch(branch, at_pos=1, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


def _terminal_source_with_exact_fork_rows(store: Store):
    session_id, branch_id = store.create_session()
    rows = _canonical_rows(
        ("assistant", "The corridor is still."),
        ("user", "I listen."),
    )
    player_hash = rows[1][1]
    store.append_msgs(branch_id, 0, rows)
    store.record_turn(branch_id, 0, "normal", "normal")
    store.write_turn_hashes(branch_id, 0, user_hash=player_hash)
    key = build_pre_mutation_key(
        session_id=session_id,
        branch_id=branch_id,
        turn_index=0,
        accepted_prefix_pos=1,
        accepted_head_hash=rows[0][2],
        player_input_hash=player_hash,
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/test-1",
    )
    reservation = store.turn_lifecycle.reserve(key)
    post = fingerprint({"hp": 2})
    store.turn_lifecycle.commit_mutation_with_fallback(
        _artifact(key, reservation.request_hash, mechanics_post=post), lambda: post
    )
    return session_id, branch_id, key


@pytest.mark.parametrize("position", [0, 1], ids=["assistant", "player"])
def test_direct_fork_recomputes_every_selected_source_chain_before_mutation(position):
    store = Store(":memory:")
    _session_id, branch_id, _key = _terminal_source_with_exact_fork_rows(store)
    with store.transaction():
        store.db.execute(
            "UPDATE branch_msgs SET chain_hash=? WHERE branch_id=? AND pos=?",
            ("9" * 16, branch_id, position),
        )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="transcript chain is corrupt"):
        store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


def test_direct_fork_recomputes_chain_from_assistant_content_identity_before_mutation():
    store = Store(":memory:")
    _session_id, branch_id, _key = _terminal_source_with_exact_fork_rows(store)
    with store.transaction():
        store.db.execute(
            "UPDATE branch_msgs SET content_hash=? WHERE branch_id=? AND pos=0",
            (content_hash("A different assistant message."), branch_id),
        )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="transcript chain is corrupt"):
        store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize(
    ("at_pos", "fork_turn"),
    [
        (2, 1),  # one selected Player message maps exactly to source turn zero
        (3, 0),  # the claimed transcript prefix extends beyond the source branch
    ],
)
def test_direct_fork_refuses_noncanonical_source_cut_before_mutation(at_pos, fork_turn):
    store = Store(":memory:")
    _session_id, branch_id, _key = _terminal_source_with_exact_fork_rows(store)
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict):
        store.fork_branch(branch_id, at_pos=at_pos, fork_turn=fork_turn)

    assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "player_message",
        "preceding_head",
        "turn_user",
        "lifecycle_key_json",
        "lifecycle_branch",
        "lifecycle_session",
    ],
)
def test_direct_fork_rebinds_terminal_lifecycle_to_exact_source_rows(corruption):
    store = Store(":memory:")
    _session_id, branch_id, key = _terminal_source_with_exact_fork_rows(store)
    with store.transaction():
        if corruption == "player_message":
            store.db.execute(
                "UPDATE branch_msgs SET content_hash=? WHERE branch_id=? AND pos=1",
                ("c" * 16, branch_id),
            )
        elif corruption == "preceding_head":
            store.db.execute(
                "UPDATE branch_msgs SET chain_hash=? WHERE branch_id=? AND pos=0",
                ("d" * 16, branch_id),
            )
        elif corruption == "turn_user":
            store.db.execute(
                "UPDATE turns SET user_hash=? WHERE branch_id=? AND turn_index=0",
                ("e" * 16, branch_id),
            )
        elif corruption == "lifecycle_key_json":
            sealed = json.loads(store.db.execute(
                "SELECT key_json FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
                (key["lifecycle_key"],),
            ).fetchone()["key_json"])
            sealed["player_input_hash"] = "f" * 16
            store.db.execute(
                "UPDATE semantic_turn_lifecycles SET key_json=? WHERE lifecycle_key=?",
                (json.dumps(sealed, sort_keys=True, separators=(",", ":")),
                 key["lifecycle_key"]),
            )
        elif corruption == "lifecycle_branch":
            _other_session, other_branch = store.create_session()
            store.db.execute(
                "UPDATE semantic_turn_lifecycles SET branch_id=? WHERE lifecycle_key=?",
                (other_branch, key["lifecycle_key"]),
            )
        else:
            other_session, _other_branch = store.create_session()
            store.db.execute(
                "UPDATE semantic_turn_lifecycles SET session_id=? WHERE lifecycle_key=?",
                (other_session, key["lifecycle_key"]),
            )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict):
        store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize("position", [0, 1], ids=["assistant", "player"])
def test_nested_fork_revalidates_the_rebased_source_chain_before_mutation(position):
    store = Store(":memory:")
    _session_id, branch_id, _key = _terminal_source_with_exact_fork_rows(store)
    child = store.fork_branch(branch_id, at_pos=2, fork_turn=0)
    with store.transaction():
        store.db.execute(
            "UPDATE branch_msgs SET chain_hash=? WHERE branch_id=? AND pos=?",
            ("9" * 16, child, position),
        )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="transcript chain is corrupt"):
        store.fork_branch(child, at_pos=2, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize("position", [0, 1], ids=["assistant", "player"])
def test_reopened_store_revalidates_the_persisted_source_chain_before_mutation(
    tmp_path, position
):
    path = tmp_path / "fork-source.sqlite3"
    store = Store(str(path))
    _session_id, branch_id, _key = _terminal_source_with_exact_fork_rows(store)
    with store.transaction():
        store.db.execute(
            "UPDATE branch_msgs SET chain_hash=? WHERE branch_id=? AND pos=?",
            ("9" * 16, branch_id, position),
        )
    store.db.close()

    reopened = Store(str(path))
    before = tuple(reopened.db.iterdump())
    with pytest.raises(TurnReservationConflict, match="transcript chain is corrupt"):
        reopened.fork_branch(branch_id, at_pos=2, fork_turn=0)
    assert tuple(reopened.db.iterdump()) == before
    reopened.db.close()


def _commit_terminal_swipe_for_fork(store: Store, key):
    initial = store.turn_lifecycle.replay(key["lifecycle_key"])
    post = initial.envelope["ledger"]["terminal_post_hash"]
    _complete_delivery(store, initial)
    reservation = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"fork_swipe": "terminal"}),
        expected_post_ledger_hash=post,
    )
    swipe = _artifact(
        key,
        reservation.request_hash,
        attempt=reservation.attempt_index,
        kind="swipe",
        mechanics_post=post,
        fallback=b"terminal swipe",
        source=_source_lineage(initial),
    )
    return store.turn_lifecycle.commit_mutation_with_fallback(swipe)


def test_direct_fork_copies_every_terminal_attempt_through_the_active_swipe():
    store = Store(":memory:")
    _session_id, branch_id, key = _terminal_source_with_exact_fork_rows(store)
    active = _commit_terminal_swipe_for_fork(store, key)
    source_before = [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (key["lifecycle_key"],),
    ).fetchall()]

    child = store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    child_lifecycle = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?", (child,)
    ).fetchone()
    child_replay = store.turn_lifecycle.replay(
        child_lifecycle["lifecycle_key"], reason="reopen"
    )
    child_attempts = store.db.execute(
        "SELECT attempt_index FROM semantic_turn_attempts WHERE lifecycle_key=?"
        " ORDER BY attempt_index",
        (child_lifecycle["lifecycle_key"],),
    ).fetchall()
    assert int(child_lifecycle["active_attempt_index"]) == active.attempt_index == 1
    assert [int(row["attempt_index"]) for row in child_attempts] == [0, 1]
    assert child_replay.payload == active.payload == b"terminal swipe"
    assert [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (key["lifecycle_key"],),
    ).fetchall()] == source_before


def test_direct_fork_refuses_an_older_nonterminal_attempt_before_mutation():
    store = Store(":memory:")
    _session_id, branch_id, key = _terminal_source_with_exact_fork_rows(store)
    _commit_terminal_swipe_for_fork(store, key)
    with store.transaction():
        store.db.execute(
            "UPDATE semantic_turn_attempts SET status='reserved'"
            " WHERE lifecycle_key=? AND attempt_index=0",
            (key["lifecycle_key"],),
        )
    before = tuple(store.db.iterdump())

    with pytest.raises(TurnReservationConflict, match="older nonterminal attempt"):
        store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    assert tuple(store.db.iterdump()) == before


def test_direct_fork_excludes_a_newer_inflight_swipe_and_keeps_prior_terminal_active():
    store = Store(":memory:")
    _session_id, branch_id, key = _terminal_source_with_exact_fork_rows(store)
    source = store.turn_lifecycle.replay(key["lifecycle_key"])
    _complete_delivery(store, source)
    store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"fork_swipe": "inflight"}),
        expected_post_ledger_hash=source.envelope["ledger"]["terminal_post_hash"],
    )
    source_before = [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (key["lifecycle_key"],),
    ).fetchall()]

    child = store.fork_branch(branch_id, at_pos=2, fork_turn=0)

    child_lifecycle = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?", (child,)
    ).fetchone()
    child_attempts = store.db.execute(
        "SELECT attempt_index, status FROM semantic_turn_attempts WHERE lifecycle_key=?"
        " ORDER BY attempt_index",
        (child_lifecycle["lifecycle_key"],),
    ).fetchall()
    assert int(child_lifecycle["active_attempt_index"]) == 0
    assert [(int(row["attempt_index"]), row["status"]) for row in child_attempts] \
        == [(0, source.status)]
    assert store.turn_lifecycle.replay(child_lifecycle["lifecycle_key"]).payload \
        == source.payload
    assert [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (key["lifecycle_key"],),
    ).fetchall()] == source_before


def test_direct_fork_keeps_empty_and_nonsemantic_prefix_controls_valid():
    empty_store = Store(":memory:")
    _empty_session, empty_branch = empty_store.create_session()
    empty_child = empty_store.fork_branch(empty_branch, at_pos=0, fork_turn=-1)
    assert empty_store.get_msgs(empty_child) == []
    assert empty_store.db.execute(
        "SELECT head_turn FROM branches WHERE branch_id=?", (empty_child,)
    ).fetchone()["head_turn"] == -1

    legacy_store = Store(":memory:")
    _legacy_session, legacy_branch = legacy_store.create_session()
    legacy_rows = _canonical_rows(
        ("assistant", "A legacy opening."),
        ("user", "I continue the legacy session."),
    )
    legacy_store.append_msgs(legacy_branch, 0, legacy_rows)
    legacy_store.record_turn(legacy_branch, 0, "normal", "normal")
    legacy_store.write_turn_hashes(legacy_branch, 0, user_hash=legacy_rows[1][1])
    legacy_child = legacy_store.fork_branch(legacy_branch, at_pos=2, fork_turn=0)
    assert [(row["pos"], row["role"], row["content_hash"])
            for row in legacy_store.get_msgs(legacy_child)] == [
        (0, "assistant", legacy_rows[0][1]),
        (1, "user", legacy_rows[1][1]),
    ]

    text_store = Store(":memory:")
    _text_session, text_branch = text_store.create_session()
    text_rows = _canonical_rows(("text", "Bean: I continue."))
    text_store.append_msgs(text_branch, 0, text_rows)
    text_store.record_turn(text_branch, 0, "normal", "normal")
    text_store.write_turn_hashes(text_branch, 0, user_hash=text_rows[0][1])
    text_child = text_store.fork_branch(text_branch, at_pos=1, fork_turn=0)
    assert [(row["role"], row["content_hash"], row["chain_hash"])
            for row in text_store.get_msgs(text_child)] == [text_rows[0]]
