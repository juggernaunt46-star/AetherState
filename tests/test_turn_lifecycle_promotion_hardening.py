from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from aetherstate.canon import canonicalize, chain, content_hash
from aetherstate.response_wire import encode_chat_story
from aetherstate.store import Store
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    TurnArtifactError,
    TurnDivergenceError,
    TurnReservation,
    ReplayArtifact,
    TurnReservationConflict,
    build_delivery_proof,
    build_envelope,
    build_pre_mutation_key,
    fingerprint,
    logical_message_id,
)


POST_HASH = fingerprint({"hp": 2})


def _setup(store: Store) -> tuple[str, str, dict, TurnReservation]:
    session_id, branch_id = store.create_session()
    key = build_pre_mutation_key(
        session_id=session_id,
        branch_id=branch_id,
        turn_index=0,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=content_hash("I strike."),
        pre_ledger_hash=fingerprint({"hp": 3}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/promotion-hardening-1",
    )
    return session_id, branch_id, key, store.turn_lifecycle.reserve(key)


def _wire(story: str, *, artifact_ref: str) -> tuple[bytes, str]:
    wire = encode_chat_story(
        story,
        model="promotion-hardening-test",
        stream=False,
        artifact_ref=artifact_ref,
    )
    return wire.raw, wire.content_type


def _proof(
    key: dict,
    wire_bytes: bytes,
    content_type: str,
    story: str,
    *,
    artifact_kind: str,
) -> dict:
    graph = {"claims": []}
    return build_delivery_proof(
        wire_bytes=wire_bytes,
        content_type=content_type,
        renderer_bytes=b'{"adapter":"canonical-text-only/1"}',
        visible_bytes=story.encode("utf-8"),
        expected_graph=graph,
        observed_graph=graph,
        ledger_graph=graph,
        ledger_root_hash=POST_HASH,
        logical_message_identity=logical_message_id(key),
        artifact_kind=artifact_kind,
    )


def _fallback(
    key: dict,
    reservation: TurnReservation,
    *,
    story: str,
    source: ReplayArtifact | None = None,
) -> EnvelopeArtifact:
    wire, content_type = _wire(
        story,
        artifact_ref=f"fallback-{reservation.attempt_index}-{reservation.request_hash}",
    )
    return build_envelope(
        pre_mutation_key=key,
        attempt_index=reservation.attempt_index,
        attempt_kind="initial" if reservation.attempt_index == 0 else "swipe",
        request_hash=reservation.request_hash,
        occurrences=[{"occurrence_id": "occ-1", "settlement_ref": "settlement-1"}],
        effects=[{"effect_id": "effect-1", "occurrence_id": "occ-1"}],
        rng_fingerprint=fingerprint({"seed": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=key["pre_ledger_hash"],
        mechanics_post_ledger_hash=POST_HASH,
        fallback_bytes=wire,
        delivery_proof=_proof(key, wire, content_type, story, artifact_kind="fallback"),
        source_lifecycle_key=source.lifecycle_key if source is not None else None,
        source_envelope_fingerprint=(
            source.envelope["envelope_fingerprint"] if source is not None else None
        ),
    )


def _accepted(
    key: dict,
    reservation: TurnReservation,
    base: EnvelopeArtifact,
    *,
    story: str,
) -> EnvelopeArtifact:
    wire, content_type = _wire(
        story,
        artifact_ref=f"accepted-{reservation.attempt_index}-{reservation.request_hash}",
    )
    base_envelope = base.envelope
    lineage = base_envelope["lineage"]
    return build_envelope(
        pre_mutation_key=key,
        attempt_index=reservation.attempt_index,
        attempt_kind="initial" if reservation.attempt_index == 0 else "swipe",
        request_hash=reservation.request_hash,
        occurrences=[{"occurrence_id": "occ-1", "settlement_ref": "settlement-1"}],
        effects=[{"effect_id": "effect-1", "occurrence_id": "occ-1"}],
        rng_fingerprint=fingerprint({"seed": 7}),
        config_fingerprint=fingerprint({"difficulty": "normal"}),
        engine_version="test-engine/1",
        pre_ledger_hash=key["pre_ledger_hash"],
        mechanics_post_ledger_hash=POST_HASH,
        fallback_bytes=base.fallback_bytes,
        accepted_bytes=wire,
        delivery_proof=_proof(key, wire, content_type, story, artifact_kind="accepted"),
        decision="accept",
        source_lifecycle_key=lineage["source_lifecycle_key"],
        source_envelope_fingerprint=lineage["source_envelope_fingerprint"],
    )


def _settle_initial(store: Store, key: dict, reservation: TurnReservation) -> EnvelopeArtifact:
    base = _fallback(key, reservation, story="The guard remains standing.")
    store.turn_lifecycle.commit_mutation_with_fallback(base, lambda: POST_HASH)
    return base


def _settle_swipe(
    store: Store,
    key: dict,
    *,
    number: int,
    story: str,
) -> tuple[TurnReservation, EnvelopeArtifact]:
    source = store.turn_lifecycle.replay(key["lifecycle_key"])
    _complete_active_delivery(store, key["lifecycle_key"])
    reservation = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": number}),
        expected_post_ledger_hash=POST_HASH,
    )
    base = _fallback(key, reservation, story=story, source=source)
    store.turn_lifecycle.commit_mutation_with_fallback(base)
    return reservation, base


def _complete_active_delivery(store: Store, lifecycle_key: str):
    replay = store.turn_lifecycle.replay(lifecycle_key)
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
    return replay


def test_swipe_commit_rejects_arbitrary_source_fingerprint_before_pointer_change() -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    _settle_initial(store, key, initial)
    source = store.turn_lifecycle.replay(key["lifecycle_key"])
    _complete_active_delivery(store, key["lifecycle_key"])
    reservation = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "forged-source"}),
        expected_post_ledger_hash=POST_HASH,
    )
    forged_source = replace(
        source,
        envelope={
            **source.envelope,
            "envelope_fingerprint": fingerprint({"forged": "source-envelope"}),
        },
    )
    fallback = _fallback(
        key,
        reservation,
        story="The forged alternate must not become active.",
        source=forged_source,
    )
    callback_called = False

    def apply_swipe() -> str:
        nonlocal callback_called
        callback_called = True
        return POST_HASH

    with pytest.raises(TurnDivergenceError, match="active terminal source"):
        store.turn_lifecycle.commit_mutation_with_fallback(
            fallback,
            swipe_callback=apply_swipe,
        )

    assert callback_called is False
    assert store.turn_lifecycle.replay(key["lifecycle_key"]) == source
    row = store.db.execute(
        "SELECT status, fallback_bytes FROM semantic_turn_attempts"
        " WHERE lifecycle_key=? AND attempt_index=?",
        (key["lifecycle_key"], reservation.attempt_index),
    ).fetchone()
    assert (row["status"], row["fallback_bytes"]) == ("reserved", None)


def test_swipe_commit_rejects_an_inactive_attempt_as_its_source() -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    _settle_initial(store, key, initial)
    inactive = store.turn_lifecycle.replay(key["lifecycle_key"])
    first, _first_base = _settle_swipe(
        store,
        key,
        number=1,
        story="The first active alternate remains safe.",
    )
    active = store.turn_lifecycle.replay(key["lifecycle_key"])
    assert active.attempt_index == first.attempt_index
    _complete_active_delivery(store, key["lifecycle_key"])
    second = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "inactive-source"}),
        expected_post_ledger_hash=POST_HASH,
    )
    stale = _fallback(
        key,
        second,
        story="The stale alternate must not become active.",
        source=inactive,
    )

    with pytest.raises(TurnDivergenceError, match="active terminal source"):
        store.turn_lifecycle.commit_mutation_with_fallback(stale)

    assert store.turn_lifecycle.replay(key["lifecycle_key"]) == active


def test_delayed_older_promotion_cannot_rewind_the_active_swipe() -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    _settle_initial(store, key, initial)
    first, first_base = _settle_swipe(
        store, key, number=1, story="The first alternate remains safe."
    )
    delayed = _accepted(key, first, first_base, story="The first alternate is accepted.")
    _complete_active_delivery(store, key["lifecycle_key"])
    second, second_base = _settle_swipe(
        store, key, number=2, story="The second alternate remains safe."
    )

    with pytest.raises(TurnReservationConflict, match="stale|newer|active"):
        store.turn_lifecycle.promote_candidate(delayed)

    lifecycle = store.db.execute(
        "SELECT active_attempt_index FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    first_row = store.db.execute(
        "SELECT status FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=?",
        (key["lifecycle_key"], first.attempt_index),
    ).fetchone()
    assert int(lifecycle["active_attempt_index"]) == second.attempt_index
    assert first_row["status"] == "fallback_ready"
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).payload == second_base.fallback_bytes


def test_fallback_artifact_returns_exact_proved_base_behind_accepted_terminal() -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    base = _settle_initial(store, key, initial)
    terminal = _accepted(
        key,
        initial,
        base,
        story="The accepted alternate remains truthful.",
    )
    accepted = store.turn_lifecycle.promote_candidate(terminal)

    persisted = store.turn_lifecycle.fallback_artifact(
        key["lifecycle_key"],
        attempt_index=accepted.attempt_index,
    )

    assert persisted == base
    assert persisted.fallback_bytes == base.fallback_bytes
    assert accepted.payload == terminal.accepted_bytes
    assert accepted.payload != persisted.fallback_bytes


def test_newer_refused_attempt_also_fences_delayed_promotion() -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    _settle_initial(store, key, initial)
    first, first_base = _settle_swipe(
        store, key, number=1, story="The current alternate remains safe."
    )
    delayed = _accepted(key, first, first_base, story="The delayed alternate is accepted.")
    _complete_active_delivery(store, key["lifecycle_key"])
    refused = store.turn_lifecycle.reserve_swipe(
        key["lifecycle_key"],
        request_hash=fingerprint({"swipe": "refused-newer"}),
        expected_post_ledger_hash=POST_HASH,
        allow_regate=False,
    )

    with pytest.raises(TurnReservationConflict, match="newer"):
        store.turn_lifecycle.promote_candidate(delayed)

    assert refused.attempt_index > first.attempt_index
    assert store.turn_lifecycle.replay(key["lifecycle_key"]).attempt_index == first.attempt_index


def test_concurrent_promotion_and_newer_reservation_never_rewind_after_newer_commit(
    tmp_path,
) -> None:
    path = tmp_path / "promotion-cas.sqlite3"
    promoter = Store(path)
    _, _, key, initial = _setup(promoter)
    _settle_initial(promoter, key, initial)
    first, first_base = _settle_swipe(
        promoter, key, number=1, story="The first concurrent alternate remains safe."
    )
    terminal = _accepted(key, first, first_base, story="The concurrent candidate is accepted.")
    _complete_active_delivery(promoter, key["lifecycle_key"])
    reserver = Store(path)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def promote() -> None:
        barrier.wait()
        try:
            outcomes["promotion"] = promoter.turn_lifecycle.promote_candidate(terminal)
        except TurnReservationConflict as exc:
            outcomes["promotion"] = exc

    def reserve() -> None:
        barrier.wait()
        outcomes["reservation"] = reserver.turn_lifecycle.reserve_swipe(
            key["lifecycle_key"],
            request_hash=fingerprint({"swipe": "concurrent-newer"}),
            expected_post_ledger_hash=POST_HASH,
        )

    threads = [threading.Thread(target=promote), threading.Thread(target=reserve)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    newer = outcomes["reservation"]
    assert isinstance(newer, TurnReservation)
    newer_base = _fallback(
        key,
        newer,
        story="The newer concurrent alternate remains safe.",
        source=reserver.turn_lifecycle.replay(key["lifecycle_key"]),
    )
    reserver.turn_lifecycle.commit_mutation_with_fallback(newer_base)
    try:
        promoter.turn_lifecycle.promote_candidate(terminal)
    except TurnReservationConflict:
        pass
    lifecycle = reserver.db.execute(
        "SELECT active_attempt_index FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (key["lifecycle_key"],),
    ).fetchone()
    assert int(lifecycle["active_attempt_index"]) == newer.attempt_index
    assert reserver.turn_lifecycle.replay(key["lifecycle_key"]).payload == newer_base.fallback_bytes


@pytest.mark.parametrize(
    "column,value",
    [
        ("base_envelope_fingerprint", fingerprint({"wrong": "base"})),
        ("terminal_envelope_fingerprint", fingerprint({"wrong": "terminal"})),
        ("post_ledger_hash", fingerprint({"wrong": "ledger"})),
    ],
)
def test_promotion_cas_rejects_changed_lifecycle_anchor_and_rolls_back_attempt(
    column: str, value: str
) -> None:
    store = Store(":memory:")
    _, _, key, initial = _setup(store)
    base = _settle_initial(store, key, initial)
    terminal = _accepted(key, initial, base, story="The accepted story remains truthful.")
    store.db.execute(
        f"UPDATE semantic_turn_lifecycles SET {column}=? WHERE lifecycle_key=?",
        (value, key["lifecycle_key"]),
    )

    with pytest.raises(TurnReservationConflict, match="lifecycle|CAS|anchor"):
        store.turn_lifecycle.promote_candidate(terminal)

    row = store.db.execute(
        "SELECT status, terminal_envelope_json FROM semantic_turn_attempts"
        " WHERE lifecycle_key=? AND attempt_index=0",
        (key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["terminal_envelope_json"]) == ("fallback_ready", None)


def test_accepted_proof_rejects_wire_story_that_differs_from_visible_truth() -> None:
    store = Store(":memory:")
    _, _, key, _ = _setup(store)
    wire, content_type = _wire("The guard dies.", artifact_ref="wire-visible-inversion")
    graph = {"claims": []}

    with pytest.raises(TurnArtifactError, match="wire|visible|surface"):
        build_delivery_proof(
            wire_bytes=wire,
            content_type=content_type,
            renderer_bytes=b'{"adapter":"canonical-text-only/1"}',
            visible_bytes=b"The guard remains alive.",
            expected_graph=graph,
            observed_graph=graph,
            ledger_graph=graph,
            ledger_root_hash=POST_HASH,
            logical_message_identity=logical_message_id(key),
            artifact_kind="accepted",
        )


def test_accepted_proof_requires_exact_wire_content_type_identity() -> None:
    store = Store(":memory:")
    _, _, key, _ = _setup(store)
    wire, _ = _wire("The guard remains alive.", artifact_ref="wrong-content-type")
    graph = {"claims": []}

    with pytest.raises(TurnArtifactError, match="content type|wire|surface"):
        build_delivery_proof(
            wire_bytes=wire,
            content_type="text/event-stream",
            renderer_bytes=b'{"adapter":"canonical-text-only/1"}',
            visible_bytes=b"The guard remains alive.",
            expected_graph=graph,
            observed_graph=graph,
            ledger_graph=graph,
            ledger_root_hash=POST_HASH,
            logical_message_identity=logical_message_id(key),
            artifact_kind="accepted",
        )


def test_fork_binds_child_lifecycle_to_active_swipe_terminal_not_attempt_zero() -> None:
    store = Store(":memory:")
    _, branch_id, key, initial = _setup(store)
    initial_base = _settle_initial(store, key, initial)
    player_hash = key["player_input_hash"]
    player = canonicalize([{"role": "user", "content": "I strike."}])[0]
    store.append_msgs(branch_id, 0, [("user", player_hash, chain([player])[0])])
    store.record_turn(branch_id, 0, "normal", "normal")
    store.write_turn_hashes(branch_id, 0, user_hash=player_hash)

    swipe, swipe_base = _settle_swipe(
        store, key, number=1, story="The active alternate remains safe."
    )
    terminal = _accepted(key, swipe, swipe_base, story="The active alternate is accepted.")
    parent_terminal = store.turn_lifecycle.promote_candidate(terminal)
    child_branch = store.fork_branch(branch_id, at_pos=1, fork_turn=0)

    child_lifecycle = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?", (child_branch,)
    ).fetchone()
    child_attempts = store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (child_lifecycle["lifecycle_key"],),
    ).fetchall()
    child_active = child_attempts[swipe.attempt_index]

    assert child_lifecycle["active_attempt_index"] == swipe.attempt_index
    assert child_lifecycle["base_envelope_fingerprint"] \
        == child_active["fallback_envelope_fingerprint"]
    assert child_lifecycle["base_envelope_fingerprint"] \
        != child_attempts[0]["fallback_envelope_fingerprint"]
    assert child_lifecycle["terminal_envelope_fingerprint"] \
        == child_active["terminal_envelope_fingerprint"]
    child_replay = store.turn_lifecycle.replay(child_lifecycle["lifecycle_key"])
    assert child_replay.attempt_index == swipe.attempt_index
    assert child_replay.payload == parent_terminal.payload
    assert child_replay.envelope["lineage"]["source_envelope_fingerprint"] \
        == parent_terminal.envelope["envelope_fingerprint"]
    assert initial_base.fallback_bytes != parent_terminal.payload
