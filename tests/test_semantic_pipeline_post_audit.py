"""Adversarial post-audit regressions for the proof-carrying Semantic Pipeline."""
from __future__ import annotations

from dataclasses import replace
import json
import random

import httpx
import pytest
from fastapi import FastAPI

from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.proxy import make_relay_router
from aetherstate.response_wire import decode_chat_story
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from aetherstate.turn_lifecycle import TurnReservationConflict
from tests.mock_upstream import MockUpstream, Reply


def _runtime() -> tuple[Pipeline, Store, SessionEngine]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    return Pipeline(store, engine, cfg, rng=random.Random(90210)), store, engine


def _body(text: str = "I wait.", *, model: str = "semantic-post-audit") -> bytes:
    return json.dumps({
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")


def _stamp(session: str, *, kind: str = "normal") -> Stamp:
    return Stamp(
        session=session,
        turn=1,
        gen_type=kind,
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )


def _claim(store: Store, ctx):
    replay = ctx.semantic_replay
    assert replay is not None
    claimed = store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    ctx.semantic_replay = claimed
    return claimed


def test_lost_reply_replays_after_server_recorded_output_without_rerunning_settlement(
    monkeypatch,
):
    pipe, store, engine = _runtime()
    body = _body("I look around.")
    stamp = _stamp("lost-after-server-tee")
    calls = 0
    original = pipe._process_observed

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pipe, "_process_observed", counted)
    _packet, first = pipe.process(stamp, body)
    assert first is not None and first.semantic_replay is not None
    claimed = _claim(store, first)
    pipe.on_response(first, claimed.payload, claimed.content_type)
    persisted = store.db.execute(
        "SELECT assistant_hash FROM turns WHERE branch_id=? AND turn_index=?",
        (first.branch_id, first.turn_index),
    ).fetchone()
    assert persisted is not None and persisted["assistant_hash"]

    # The server emitted and recorded the reply, but the frontend never retained it: its next
    # canonical request still ends at the same exact Player message.
    engine._dedup.clear()
    _packet, recovered = pipe.process(stamp, body)

    assert recovered is not None and recovered.semantic_replay is not None
    assert recovered.semantic_replay.payload == claimed.payload
    assert recovered.semantic_replay.selected_artifact_digest == claimed.selected_artifact_digest
    assert recovered.branch_id == first.branch_id
    assert recovered.turn_index == first.turn_index
    assert calls == 1
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 1


def test_same_player_words_after_retained_assistant_are_a_new_occurrence(monkeypatch):
    pipe, store, engine = _runtime()
    first_body = _body("I look around.")
    stamp = _stamp("same-words-after-retained-assistant")
    calls = 0
    original = pipe._process_observed

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pipe, "_process_observed", counted)
    _packet, first = pipe.process(stamp, first_body)
    assert first is not None and first.semantic_replay is not None
    claimed = _claim(store, first)
    pipe.on_response(first, claimed.payload, claimed.content_type)
    story = decode_chat_story(claimed.payload, claimed.content_type)
    retained = json.dumps({
        "model": "semantic-post-audit",
        "stream": False,
        "messages": [
            {"role": "user", "content": "I look around."},
            {"role": "assistant", "content": story},
            {"role": "user", "content": "I look around."},
        ],
    }).encode("utf-8")

    engine._dedup.clear()
    _packet, second = pipe.process(stamp, retained)

    assert second is not None and second.semantic_replay is not None
    assert second.turn_index == first.turn_index + 1
    assert second.semantic_replay.lifecycle_key != claimed.lifecycle_key
    assert calls == 2
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 2


def _two_terminal_rows():
    pipe, store, _engine = _runtime()
    _packet, left = pipe.process(_stamp("artifact-left"), _body("I wait."))
    _packet, right = pipe.process(_stamp("artifact-right"), _body("I look around."))
    assert left is not None and left.semantic_replay is not None
    assert right is not None and right.semantic_replay is not None
    assert left.semantic_replay.payload != right.semantic_replay.payload
    rows = store.db.execute(
        "SELECT a.*, l.branch_id, l.turn_index"
        " FROM semantic_turn_attempts a"
        " JOIN semantic_turn_lifecycles l ON l.lifecycle_key=a.lifecycle_key"
        " ORDER BY l.branch_id"
    ).fetchall()
    assert len(rows) == 2
    source, target = rows
    if source["lifecycle_key"] == right.semantic_replay.lifecycle_key:
        source, target = target, source
    return pipe, store, source, target


def _cross_row_swap(store: Store, source, target) -> None:
    copied = (
        "attempt_kind", "request_hash", "ledger_anchor_hash", "status", "refusal_code",
        "fallback_envelope_fingerprint", "fallback_envelope_json", "fallback_bytes",
        "fallback_hash", "terminal_envelope_fingerprint", "terminal_envelope_json",
        "accepted_bytes", "accepted_hash", "logical_message_id", "selected_artifact_digest",
    )
    assignments = ", ".join(f"{field}=?" for field in copied)
    with store.transaction():
        store.db.execute(
            f"UPDATE semantic_turn_attempts SET {assignments}"
            " WHERE lifecycle_key=? AND attempt_index=?",
            (*[source[field] for field in copied], target["lifecycle_key"], target["attempt_index"]),
        )


@pytest.mark.parametrize("operation", ["reopen", "claim", "fork"])
def test_fully_valid_artifact_cannot_be_laundered_across_attempt_rows(operation):
    _pipe, store, source, target = _two_terminal_rows()
    _cross_row_swap(store, source, target)
    branch_count = store.db.execute("SELECT COUNT(*) FROM branches").fetchone()[0]

    with pytest.raises(TurnReservationConflict, match="stored narration"):
        if operation == "reopen":
            store.turn_lifecycle.replay(target["lifecycle_key"], reason="reopen")
        elif operation == "claim":
            store.turn_lifecycle.claim_delivery(
                target["lifecycle_key"],
                int(target["attempt_index"]),
                expected_logical_message_id=source["logical_message_id"],
                expected_artifact_digest=source["selected_artifact_digest"],
            )
        else:
            store.fork_branch(
                target["branch_id"],
                at_pos=1,
                fork_turn=int(target["turn_index"]),
            )

    assert store.db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == branch_count
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_delivery_claims WHERE lifecycle_key=?",
        (target["lifecycle_key"],),
    ).fetchone()[0] == 0


def test_exact_fork_rebases_receipt_and_claim_without_mutating_source_lineage():
    pipe, store, _engine = _runtime()
    _packet, source_ctx = pipe.process(_stamp("fork-source"), _body())
    assert source_ctx is not None and source_ctx.semantic_replay is not None
    source = _claim(store, source_ctx)
    source_before = store.turn_lifecycle.replay(source.lifecycle_key, reason="reopen")

    child_branch = store.fork_branch(
        source_ctx.branch_id,
        at_pos=1,
        fork_turn=source_ctx.turn_index,
    )
    child_row = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?",
        (child_branch,),
    ).fetchone()
    assert child_row is not None
    child = store.turn_lifecycle.replay(child_row["lifecycle_key"], reason="reopen")
    assert child.payload == source.payload
    assert child.lifecycle_key != source.lifecycle_key
    assert child.logical_message_id != source.logical_message_id
    assert child.selected_artifact_digest != source.selected_artifact_digest
    assert child.envelope["lineage"]["source_lifecycle_key"] == source.lifecycle_key
    assert child.envelope["lineage"]["source_envelope_fingerprint"] \
        == source.envelope["envelope_fingerprint"]

    child_claim = store.turn_lifecycle.claim_delivery(
        child.lifecycle_key,
        child.attempt_index,
        expected_logical_message_id=child.logical_message_id,
        expected_artifact_digest=child.selected_artifact_digest,
    )
    assert child_claim.payload == source.payload
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_delivery_claims"
    ).fetchone()[0] == 2
    source_after = store.turn_lifecycle.replay(source.lifecycle_key, reason="reopen")
    assert source_after == source_before


def test_claimed_old_attempt_cannot_write_cold_state_after_swipe_moves_pointer():
    pipe, store, _engine = _runtime()
    body = _body()
    stamp = _stamp("stale-cold-after-swipe")
    _packet, initial = pipe.process(stamp, body)
    assert initial is not None and initial.semantic_replay is not None
    old = _claim(store, initial)
    pipe.on_response(initial, old.payload, old.content_type)
    store.turn_lifecycle.complete_delivery(
        old.lifecycle_key,
        old.attempt_index,
        expected_logical_message_id=old.logical_message_id,
        expected_artifact_digest=old.selected_artifact_digest,
    )

    _packet, swipe = pipe.process(_stamp(stamp.session, kind="swipe"), body)
    assert swipe is not None and swipe.semantic_replay is not None
    assert swipe.semantic_replay.attempt_index > old.attempt_index
    row = store.db.execute(
        "SELECT active_attempt_index FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (old.lifecycle_key,),
    ).fetchone()
    assert row is not None and row["active_attempt_index"] == swipe.semantic_replay.attempt_index

    # A delayed finally-block from the already-claimed old stream must not restore the retired
    # assistant hash/text after the terminal swipe atomically cleared them.
    pipe.on_response(initial, old.payload, old.content_type)

    turn = store.db.execute(
        "SELECT assistant_hash FROM turns WHERE branch_id=? AND turn_index=?",
        (initial.branch_id, initial.turn_index),
    ).fetchone()
    text = store.db.execute(
        "SELECT assistant_text FROM turn_texts WHERE branch_id=? AND turn_index=?",
        (initial.branch_id, initial.turn_index),
    ).fetchone()
    assert turn is not None and turn["assistant_hash"] is None
    assert text is None or text["assistant_text"] is None


def test_claimed_artifact_cannot_write_cold_state_through_a_different_branch_context():
    pipe, store, _engine = _runtime()
    _packet, source = pipe.process(_stamp("cold-source"), _body("I wait."))
    _packet, target = pipe.process(_stamp("cold-target"), _body("I look around."))
    assert source is not None and target is not None
    claimed = _claim(store, source)
    forged_context = replace(
        source,
        session_id=target.session_id,
        branch_id=target.branch_id,
        turn_index=target.turn_index,
    )
    forged_context.semantic_replay = claimed

    pipe.on_response(forged_context, claimed.payload, claimed.content_type)

    target_turn = store.db.execute(
        "SELECT assistant_hash FROM turns WHERE branch_id=? AND turn_index=?",
        (target.branch_id, target.turn_index),
    ).fetchone()
    target_text = store.db.execute(
        "SELECT assistant_text FROM turn_texts WHERE branch_id=? AND turn_index=?",
        (target.branch_id, target.turn_index),
    ).fetchone()
    assert target_turn is not None and target_turn["assistant_hash"] is None
    assert target_text is None or target_text["assistant_text"] is None


def test_cold_path_requires_the_exact_receipt_bound_content_type():
    pipe, store, _engine = _runtime()
    _packet, ctx = pipe.process(_stamp("cold-content-type"), _body())
    assert ctx is not None and ctx.semantic_replay is not None
    claimed = _claim(store, ctx)

    pipe.on_response(ctx, claimed.payload, claimed.content_type + "; charset=utf-8")

    turn = store.db.execute(
        "SELECT assistant_hash FROM turns WHERE branch_id=? AND turn_index=?",
        (ctx.branch_id, ctx.turn_index),
    ).fetchone()
    text = store.db.execute(
        "SELECT assistant_text FROM turn_texts WHERE branch_id=? AND turn_index=?",
        (ctx.branch_id, ctx.turn_index),
    ).fetchone()
    assert turn is not None and turn["assistant_hash"] is None
    assert text is None or text["assistant_text"] is None


async def test_oversized_gated_rpg_request_fails_closed_without_upstream_visibility():
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    cfg.upstream.max_parse_mb = 1
    cfg.upstream.base_url = "http://unsafe-upstream/v1"
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg, rng=random.Random(4))
    upstream = MockUpstream()
    unsafe = b'{"choices":[{"message":{"content":"The living guard dies."}}]}'
    upstream.enqueue(Reply(headers={"content-type": "application/json"}, body=unsafe))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://unsafe-upstream",
    )
    app = FastAPI()
    app.include_router(
        make_relay_router(lambda: upstream_client, cfg, engine=engine, pipeline=pipe)
    )
    body = _body("x" * (1024 * 1024 + 256))

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            response = await client.post("/v1/chat/completions", content=body)
    finally:
        await upstream_client.aclose()

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "semantic_delivery_unavailable"
    assert unsafe not in response.content
    assert upstream.requests == []
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 0


async def test_unhandled_gated_pipeline_failure_never_falls_through_to_upstream(monkeypatch):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    cfg.upstream.base_url = "http://unsafe-upstream/v1"
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg, rng=random.Random(6))

    def catastrophic_failure(_stamp, _body):
        raise RuntimeError("failure outside the pipeline's internal supervisor")

    monkeypatch.setattr(pipe, "process", catastrophic_failure)
    upstream = MockUpstream()
    unsafe = b'{"choices":[{"message":{"content":"The living guard dies."}}]}'
    upstream.enqueue(Reply(headers={"content-type": "application/json"}, body=unsafe))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://unsafe-upstream",
    )
    app = FastAPI()
    app.include_router(
        make_relay_router(lambda: upstream_client, cfg, engine=engine, pipeline=pipe)
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            response = await client.post("/v1/chat/completions", content=_body())
    finally:
        await upstream_client.aclose()

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "semantic_delivery_unavailable"
    assert unsafe not in response.content
    assert upstream.requests == []


@pytest.mark.parametrize("exemption", ["quiet", "passthrough"])
async def test_oversized_quiet_and_explicit_passthrough_keep_their_exemption(exemption):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    cfg.upstream.max_parse_mb = 1
    cfg.upstream.base_url = "http://allowed-upstream/v1"
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg, rng=random.Random(5))
    headers = {}
    if exemption == "quiet":
        body = json.dumps({
            "model": "quiet-large",
            "messages": [
                {
                    "role": "system",
                    "content": "<<AETHER:session=large-quiet;turn=1;type=quiet>>",
                },
                {"role": "user", "content": "x" * (1024 * 1024 + 256)},
            ],
        }).encode("utf-8")
    else:
        session_id, _branch = store.create_session(external_id="large-passthrough")
        store.session_mode_set(session_id, "passthrough")
        body = _body("x" * (1024 * 1024 + 256))
        headers[cfg.stamp.header_name] = "large-passthrough"
    upstream = MockUpstream()
    allowed = b'{"choices":[{"message":{"content":"background or explicit passthrough"}}]}'
    upstream.enqueue(Reply(headers={"content-type": "application/json"}, body=allowed))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://allowed-upstream",
    )
    app = FastAPI()
    app.include_router(
        make_relay_router(lambda: upstream_client, cfg, engine=engine, pipeline=pipe)
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            response = await client.post(
                "/v1/chat/completions", content=body, headers=headers
            )
    finally:
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.content == allowed
    assert len(upstream.requests) == 1
