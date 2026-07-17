"""Pipeline wiring for sealed semantic selection, isolated from the fresh T0 bootstrap gap."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import random
from types import SimpleNamespace

import pytest

from aetherstate.canon import content_hash
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.narration_artifact_basis import (
    attach_persisted_narration_basis,
    build_persisted_narration_basis,
    rebind_persisted_narration_basis,
)
from aetherstate.narration_fallback_runtime import build_proof_complete_fallback
from aetherstate.narration_plan_runtime import (
    build_default_narration_plan_selection,
    build_narration_realization_plan,
)
from aetherstate.pipeline import Pipeline, PostContext
from aetherstate.response_wire import encode_chat_story
from aetherstate.semantic_truth_runtime import build_fenced_runtime_truth_contract
from aetherstate.semantic_narration_orchestrator import resolve_narration_selection
from aetherstate.session_engine import SessionEngine
from aetherstate.state import empty_state, reduce_state
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from aetherstate.turn_lifecycle import (
    EMPTY_PREFIX_HASH,
    EnvelopeArtifact,
    TurnArtifactError,
    build_pre_mutation_key,
    fingerprint,
    journal_window_fingerprint,
    raw_fingerprint,
    validate_envelope,
)


MODEL = "glm-5.2"
PLAYER_PROSE = "PLAYER PRIVATE: I praise the lantern while I wait."
TRANSCRIPT_PROSE = "PRIVATE PRIOR STORY: the watch remains awake."


@dataclass
class SelectionFixture:
    pipe: Pipeline
    store: Store
    body: bytes
    stamp: Stamp
    ctx: PostContext
    request_bytes: bytes
    key: dict
    fallback: EnvelopeArtifact
    plan: dict
    truth_contract: dict
    basis: dict
    pre_hash: str
    post_hash: str


def _preseeded_selection() -> SelectionFixture:
    """Install one already-proved fallback; this does not exercise fresh-turn truth projection."""
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg, rng=random.Random(22))
    session_id, branch_id = store.create_session(external_id="selection-path")
    turn = 0
    body = json.dumps({
        "model": MODEL,
        "stream": True,
        "temperature": 0.87,
        "messages": [
            {"role": "assistant", "content": TRANSCRIPT_PROSE},
            {"role": "user", "content": PLAYER_PROSE},
        ],
    }).encode("utf-8")
    player_hash = content_hash(PLAYER_PROSE)
    store.append_msgs(branch_id, 0, [("user", player_hash, "a" * 16)])
    store.record_turn(branch_id, turn, "new_turn", "normal")
    store.write_turn_hashes(branch_id, turn, user_hash=player_hash)

    pre = empty_state()
    pre["meta"]["turn"] = turn
    operation = {"op": "world_flag", "key": "selection_gate", "value": True, "_turn": turn}
    post = deepcopy(pre)
    reduce_state(post, [deepcopy(operation)])
    rows = [{
        "id": 101,
        "turn_lo": turn,
        "turn_hi": turn,
        "source": "rule",
        "ops": [operation],
    }]
    pre_hash = content_fingerprint(pre)
    post_hash = content_fingerprint(post)
    truth = build_fenced_runtime_truth_contract(
        pre_state=pre,
        post_state=post,
        pre_ledger_hash=pre_hash,
        post_ledger_hash=post_hash,
        journal_rows=rows,
        journal_window_fingerprint=journal_window_fingerprint(branch_id, rows),
        branch_id=branch_id,
        turn_index=turn,
    )
    plan = build_narration_realization_plan(truth.truth_contract)
    basis = build_persisted_narration_basis(
        truth.transition_projection,
        truth.truth_contract,
        plan,
    )
    key = build_pre_mutation_key(
        session_id=session_id,
        branch_id=branch_id,
        turn_index=turn,
        accepted_prefix_pos=0,
        accepted_head_hash=EMPTY_PREFIX_HASH,
        player_input_hash=player_hash,
        pre_ledger_hash=pre_hash,
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-prevention-runtime/1",
    )
    request_hash = fingerprint({
        "schema": "semantic-turn-request/1",
        "lifecycle_key": key["lifecycle_key"],
        "turn_class": "new_turn",
        "request_bytes": raw_fingerprint(body),
    })
    reservation = store.turn_lifecycle.reserve(key, request_hash=request_hash)
    fallback = build_proof_complete_fallback(
        contract=truth.truth_contract,
        pre_mutation_key=key,
        reservation=reservation,
        occurrences=[],
        effects=[],
        rng_fingerprint=fingerprint({"rng": 22}),
        config_fingerprint=fingerprint({"fixture": "selection-path"}),
        engine_version="test-engine/selection-path-1",
        pre_ledger_hash=pre_hash,
        mechanics_post_ledger_hash=post_hash,
        model=MODEL,
        stream=True,
    )
    fallback = attach_persisted_narration_basis(fallback, basis)
    store.turn_lifecycle.commit_mutation_with_fallback(fallback, lambda: post_hash)

    stamp = Stamp(
        session="selection-path",
        turn=turn,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )
    observed = SimpleNamespace(
        session_id=session_id,
        branch_id=branch_id,
        turn_index=turn,
        klass=SimpleNamespace(value="new_turn"),
        stamp=stamp,
        replay_reason="",
        duplicate=True,
    )
    engine.observe = lambda *_args, **_kwargs: observed
    request_bytes, ctx = pipe.process(stamp, body)
    assert ctx is not None
    return SelectionFixture(
        pipe=pipe,
        store=store,
        body=body,
        stamp=stamp,
        ctx=ctx,
        request_bytes=request_bytes,
        key=key,
        fallback=fallback,
        plan=plan,
        truth_contract=truth.truth_contract,
        basis=basis,
        pre_hash=pre_hash,
        post_hash=post_hash,
    )


def _selection_response(plan: dict, *, malformed: bool = False):
    story = "not a selection object" if malformed else json.dumps(
        build_default_narration_plan_selection(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encode_chat_story(
        story,
        model=MODEL,
        stream=False,
        artifact_ref="pipeline-selection-response",
    )


def _newer_swipe(fixture: SelectionFixture):
    source = fixture.store.turn_lifecycle.replay(fixture.key["lifecycle_key"])
    fixture.store.turn_lifecycle.claim_delivery(
        source.lifecycle_key,
        source.attempt_index,
        expected_logical_message_id=source.logical_message_id,
        expected_artifact_digest=source.selected_artifact_digest,
    )
    fixture.store.turn_lifecycle.complete_delivery(
        source.lifecycle_key,
        source.attempt_index,
        expected_logical_message_id=source.logical_message_id,
        expected_artifact_digest=source.selected_artifact_digest,
    )
    reservation = fixture.store.turn_lifecycle.reserve_swipe(
        fixture.key["lifecycle_key"],
        request_hash=fingerprint({"fixture": "newer-swipe"}),
        expected_post_ledger_hash=fixture.post_hash,
    )
    fallback = build_proof_complete_fallback(
        contract=fixture.truth_contract,
        pre_mutation_key=fixture.key,
        reservation=reservation,
        occurrences=[],
        effects=[],
        rng_fingerprint=fingerprint({"rng": 22}),
        config_fingerprint=fingerprint({"fixture": "selection-path"}),
        engine_version="test-engine/selection-path-1",
        pre_ledger_hash=fixture.pre_hash,
        mechanics_post_ledger_hash=fixture.post_hash,
        model=MODEL,
        stream=True,
        source_lifecycle_key=source.lifecycle_key,
        source_envelope_fingerprint=source.envelope["envelope_fingerprint"],
    )
    fallback = attach_persisted_narration_basis(fallback, fixture.basis)
    replay = fixture.store.turn_lifecycle.commit_mutation_with_fallback(fallback)
    return fallback, replay


def test_process_returns_only_the_sealed_nonstream_ids_request() -> None:
    fixture = _preseeded_selection()
    ctx = fixture.ctx

    assert ctx.semantic_gate and not ctx.semantic_error
    assert ctx.semantic_replay is not None
    assert ctx.semantic_replay.status == "fallback_ready"
    assert ctx.semantic_selection is not None
    prepared = ctx.semantic_selection.preparation.prepared
    assert prepared is not None
    assert fixture.request_bytes == prepared.transport_request.request_bytes
    assert raw_fingerprint(fixture.request_bytes) \
        == prepared.transport_request.request_fingerprint
    request = json.loads(fixture.request_bytes)
    assert request["model"] == MODEL
    assert request["stream"] is False
    assert request["temperature"] == 0
    assert len(request["messages"]) == 2
    assert PLAYER_PROSE.encode() not in fixture.request_bytes
    assert TRANSCRIPT_PROSE.encode() not in fixture.request_bytes
    assert b"0.87" not in fixture.request_bytes


def test_valid_selection_promotes_once_and_terminal_retry_sends_no_model_request() -> None:
    fixture = _preseeded_selection()
    response = _selection_response(fixture.plan)
    duplicate_ctx = replace(fixture.ctx)

    accepted = fixture.pipe.complete_semantic_selection(
        fixture.ctx,
        response.raw,
        response.content_type,
    )
    duplicate = fixture.pipe.complete_semantic_selection(
        duplicate_ctx,
        response.raw,
        response.content_type,
    )

    assert accepted == duplicate
    assert accepted.status == "accepted"
    assert accepted.payload != response.raw
    assert validate_envelope(
        EnvelopeArtifact(accepted.envelope, fixture.fallback.fallback_bytes, accepted.payload)
    )["gate"]["decision"] == "accept"
    assert fixture.ctx.semantic_selection is None
    assert fixture.ctx.semantic_replay == accepted
    assert fixture.store.turn_lifecycle.replay(
        accepted.lifecycle_key, reason="reopen"
    ) == accepted

    retry_bytes, retry_ctx = fixture.pipe.process(fixture.stamp, fixture.body)
    assert retry_bytes == fixture.body
    assert retry_ctx is not None and retry_ctx.semantic_selection is None
    assert retry_ctx.semantic_replay == accepted


@pytest.mark.parametrize("tamper", ["remove", "swap", "rebind"])
def test_promotion_rejects_changed_persisted_basis_and_retains_fallback(
    tamper: str,
) -> None:
    fixture = _preseeded_selection()
    pending = fixture.ctx.semantic_selection
    assert pending is not None
    response = _selection_response(fixture.plan)
    decision = resolve_narration_selection(
        pending.preparation,
        pending.fallback_artifact,
        pending.expectation,
        response_bytes=response.raw,
        response_content_type=response.content_type,
    )
    assert decision.action == "promote"
    terminal = decision.artifact
    envelope = deepcopy(terminal.envelope)
    diagnostics = envelope["diagnostics"]
    if tamper == "remove":
        diagnostics.pop("persisted_narration_basis")
    elif tamper == "swap":
        diagnostics["persisted_narration_basis"] = {
            "schema": "forged-persisted-basis/1",
            "fingerprint": fingerprint({"forged": "basis"}),
        }
    else:
        diagnostics["persisted_narration_basis"] = rebind_persisted_narration_basis(
            fixture.basis,
            branch_ref="branch.valid-but-unrelated",
        )
    envelope.pop("envelope_fingerprint")
    envelope["envelope_fingerprint"] = fingerprint(envelope)
    forged = EnvelopeArtifact(
        envelope,
        terminal.fallback_bytes,
        terminal.accepted_bytes,
    )

    with pytest.raises(TurnArtifactError, match="frozen semantic|diagnostics"):
        fixture.store.turn_lifecycle.promote_candidate(forged)

    current = fixture.store.turn_lifecycle.replay(fixture.key["lifecycle_key"])
    assert current.status == "fallback_ready"
    assert current.payload == fixture.fallback.fallback_bytes
    row = fixture.store.db.execute(
        "SELECT status, terminal_envelope_json, accepted_bytes"
        " FROM semantic_turn_attempts WHERE lifecycle_key=? AND attempt_index=0",
        (fixture.key["lifecycle_key"],),
    ).fetchone()
    assert (row["status"], row["terminal_envelope_json"], row["accepted_bytes"]) == (
        "fallback_ready",
        None,
        None,
    )


@pytest.mark.parametrize("failure", ["malformed", "error", "timeout"])
def test_malformed_error_or_timeout_finalizes_the_exact_fallback(failure: str) -> None:
    fixture = _preseeded_selection()
    original = fixture.ctx.semantic_replay
    assert original is not None
    response = _selection_response(fixture.plan, malformed=True)

    if failure == "malformed":
        terminal = fixture.pipe.complete_semantic_selection(
            fixture.ctx,
            response.raw,
            response.content_type,
        )
    elif failure == "error":
        terminal = fixture.pipe.complete_semantic_selection(
            fixture.ctx,
            response.raw,
            response.content_type,
            upstream_error=True,
        )
    else:
        terminal = fixture.pipe.complete_semantic_selection(
            fixture.ctx,
            timed_out=True,
        )

    assert terminal.status == "fallback_final"
    assert terminal.payload == original.payload
    assert terminal.payload_hash == original.payload_hash
    assert terminal.logical_message_id == original.logical_message_id
    assert terminal.selected_artifact_digest == original.selected_artifact_digest
    assert terminal.payload != response.raw
    assert fixture.ctx.semantic_selection is None

    retried = fixture.pipe.complete_semantic_selection(
        fixture.ctx,
        _selection_response(fixture.plan).raw,
        _selection_response(fixture.plan).content_type,
    )
    assert retried == terminal


def test_stale_attempt_response_replays_the_newer_swipe_without_accepting_old_bytes() -> None:
    fixture = _preseeded_selection()
    _new_fallback, newer = _newer_swipe(fixture)
    response = _selection_response(fixture.plan)

    result = fixture.pipe.complete_semantic_selection(
        fixture.ctx,
        response.raw,
        response.content_type,
    )

    assert result == newer
    assert result.attempt_index > fixture.fallback.envelope["attempt"]["index"]
    assert result.status == "fallback_ready"
    assert result.payload != response.raw
    assert fixture.ctx.semantic_selection is None
    assert fixture.store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts WHERE status='accepted'"
    ).fetchone()[0] == 0


def test_delivery_claimed_fallback_refuses_candidate_and_replays_exact_claimed_artifact() -> None:
    fixture = _preseeded_selection()
    original = fixture.ctx.semantic_replay
    assert original is not None
    claimed = fixture.store.turn_lifecycle.claim_delivery(
        original.lifecycle_key,
        original.attempt_index,
        expected_logical_message_id=original.logical_message_id,
        expected_artifact_digest=original.selected_artifact_digest,
    )
    response = _selection_response(fixture.plan)

    result = fixture.pipe.complete_semantic_selection(
        fixture.ctx,
        response.raw,
        response.content_type,
    )

    assert result == claimed
    assert result.status == "fallback_ready"
    assert result.payload == original.payload
    assert result.selected_artifact_digest == original.selected_artifact_digest
    assert result.payload != response.raw
    assert fixture.store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts WHERE status='accepted'"
    ).fetchone()[0] == 0


def test_cross_context_selection_fails_closed_before_lifecycle_promotion() -> None:
    fixture = _preseeded_selection()
    forged = replace(fixture.ctx, branch_id="branch.forged")
    response = _selection_response(fixture.plan)

    with pytest.raises(ValueError, match="context differs"):
        fixture.pipe.complete_semantic_selection(
            forged,
            response.raw,
            response.content_type,
        )

    current = fixture.store.turn_lifecycle.replay(
        fixture.key["lifecycle_key"], reason="reopen"
    )
    assert current.status == "fallback_ready"
    assert current.payload == fixture.fallback.fallback_bytes
