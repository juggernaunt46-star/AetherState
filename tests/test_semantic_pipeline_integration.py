"""End-to-end prevention-gate checks across Pipeline, SessionEngine, Store, and wire output."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import random
from types import SimpleNamespace

import pytest

import aetherstate.pipeline as pipeline_module
import aetherstate.narration_artifact_basis as basis_module
from aetherstate.canon import content_hash
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.narration_artifact_basis import extract_persisted_narration_basis
from aetherstate.response_wire import decode_chat_story
from aetherstate.session_engine import SessionEngine
from aetherstate.state import apply_delta, current_state
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from aetherstate.turn_lifecycle import raw_fingerprint


def _runtime(*, seed: int = 17, jobs=None) -> tuple[Pipeline, Store, SessionEngine]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    return Pipeline(store, engine, cfg, jobs=jobs, rng=random.Random(seed)), store, engine


def _body(text: str = "I wait.", *, stream: bool = False) -> bytes:
    return json.dumps({
        "model": "semantic-test-model",
        "stream": stream,
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")


def _stamp(session: str = "semantic-integration", *, kind: str = "normal") -> Stamp:
    return Stamp(
        session=session,
        turn=1,
        gen_type=kind,
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )


def _turn(store: Store) -> dict:
    row = store.db.execute(
        "SELECT * FROM turns ORDER BY turn_index DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return dict(row)


def _complete_semantic_delivery(pipe: Pipeline, store: Store, ctx):
    replay = ctx.semantic_replay
    assert replay is not None
    claimed = store.turn_lifecycle.claim_delivery(
        replay.lifecycle_key,
        replay.attempt_index,
        expected_logical_message_id=replay.logical_message_id,
        expected_artifact_digest=replay.selected_artifact_digest,
    )
    pipe.on_response(ctx, claimed.payload, claimed.content_type)
    return store.turn_lifecycle.complete_delivery(
        claimed.lifecycle_key,
        claimed.attempt_index,
        expected_logical_message_id=claimed.logical_message_id,
        expected_artifact_digest=claimed.selected_artifact_digest,
    )


@pytest.mark.parametrize(
    ("stream", "content_type"),
    [(False, "application/json"), (True, "text/event-stream")],
)
def test_pure_roleplay_terminalizes_as_exact_proof_carrying_wire(stream, content_type):
    pipe, store, _engine = _runtime()
    packet, ctx = pipe.process(_stamp(), _body(stream=stream))

    assert packet  # the enriched narrator request remains available for diagnostics only
    assert ctx is not None and ctx.semantic_gate and not ctx.semantic_error
    replay = ctx.semantic_replay
    assert replay is not None and replay.status == "fallback_ready"
    assert replay.content_type == content_type
    assert raw_fingerprint(replay.payload) == replay.payload_hash
    assert decode_chat_story(replay.payload, replay.content_type).strip()
    lifecycle = store.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()
    attempt = store.db.execute(
        "SELECT status FROM semantic_turn_attempts"
    ).fetchone()
    assert lifecycle["status"] == "committed"
    assert attempt["status"] == "fallback_ready"


def test_truth_gated_first_vertical_does_not_claim_player_lesson_before_final_selector():
    pipe, store, _engine = _runtime()
    lesson = pipe.player_lessons_service.create(
        effect_type="narration_behavior",
        title="AUDIT_PRIVATE_LESSON",
        scope="every_rpg_turn",
        do_text="AUDIT_KEEP_THIS_SENSORY",
        avoid_text="",
        anchor_entry_id=None,
    )
    intent_anchor = pipe.playerlex_service.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    pipe.player_lessons_service.create(
        effect_type="intent_interpretation",
        title="Keep Glass Read on inspection",
        scope="every_rpg_turn",
        misunderstanding="It can be read as a different action.",
        correct_interpretation="Use the approved inspection meaning.",
        anchor_entry_id=intent_anchor["entry_id"],
    )

    selector, ctx = pipe.process(
        _stamp(session="player-lessons-truth-gate"),
        _body("I use Glass Read and listen."),
    )
    wire = selector.decode("utf-8", errors="replace")

    assert ctx.semantic_gate is True
    assert ctx.semantic_replay.status == "fallback_ready"
    assert "[PLAYER LESSONS player-lessons/1" not in wire
    assert lesson["title"] not in wire
    assert pipe.player_lessons_service.latest_selections(ctx.session_id) == []
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_selection_receipts"
    ).fetchone()[0] == 0
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_selection_items"
    ).fetchone()[0] == 0
    assert pipe.player_lessons_service.latest_intent_applications(ctx.session_id) == []
    for table in (
        "player_lesson_intent_receipts",
        "player_lesson_intent_selection_items",
        "player_lesson_intent_applications",
    ):
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_fresh_session_commits_one_proved_t0_before_the_exact_visible_t1_window():
    pipe, store, _engine = _runtime()

    _packet, ctx = pipe.process(_stamp(), _body())

    assert ctx is not None and ctx.semantic_replay is not None
    proof = store.semantic_bootstrap_proof(ctx.session_id, ctx.branch_id)
    assert proof is not None
    assert proof.turn_index == 0
    assert proof.journal_rows
    assert all(
        row["turn_lo"] == row["turn_hi"] == 0 and row["source"] in {"bootstrap", "genesis"}
        for row in proof.journal_rows
    )
    visible_rows = store.diagnostic_turn(ctx.branch_id, ctx.turn_index)["journal"]
    assert visible_rows
    assert all(
        row["turn_lo"] == row["turn_hi"] == ctx.turn_index == 1
        for row in visible_rows
    )
    visible_kinds = {
        op["op"] for row in visible_rows for op in row["ops"]
    }
    assert not visible_kinds.intersection({"entity_add", "player_seed", "obsession", "craving"})
    lifecycle = store.db.execute(
        "SELECT key_json FROM semantic_turn_lifecycles WHERE branch_id=? AND turn_index=?",
        (ctx.branch_id, ctx.turn_index),
    ).fetchone()
    assert lifecycle is not None
    assert json.loads(lifecycle["key_json"])["pre_ledger_hash"] \
        == proof.post_bootstrap_state_fingerprint


def test_bootstrap_fault_rolls_back_session_rows_journal_proof_and_engine_index(monkeypatch):
    pipe, store, engine = _runtime()

    def fault(**_kwargs):
        raise RuntimeError("bootstrap proof fault")

    monkeypatch.setattr(pipeline_module, "build_semantic_bootstrap_proof", fault)
    forwarded, ctx = pipe.process(_stamp(session="bootstrap-fault"), _body())

    assert forwarded == _body()
    assert ctx is not None and ctx.semantic_status == 503
    for table in (
        "sessions",
        "branches",
        "turns",
        "branch_msgs",
        "ops_journal",
        "semantic_bootstrap_proofs",
        "semantic_turn_lifecycles",
    ):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert engine.index.branches == {}
    assert engine._dedup == {}


def test_restart_resumes_stranded_fresh_t1_without_advancing_or_accepting_divergence(
    monkeypatch, tmp_path
):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    path = tmp_path / "semantic-bootstrap-restart.db"
    store = Store(path)
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg, rng=random.Random(17))
    body = _body()
    stamp = _stamp(session="bootstrap-restart")

    def crash_after_bootstrap(*_args, **_kwargs):
        raise RuntimeError("crash before T1 settlement")

    monkeypatch.setattr(pipe, "_process_truth_gated", crash_after_bootstrap)
    _forwarded, failed = pipe.process(stamp, body)
    assert failed is not None and failed.semantic_status == 503
    assert store.db.execute("SELECT COUNT(*) FROM semantic_bootstrap_proofs").fetchone()[0] == 1
    assert store.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()[0] == "reserved"
    assert store.db.execute("SELECT head_turn FROM branches").fetchone()[0] == 1
    store.db.close()

    reopened = Store(path)
    restarted_engine = SessionEngine(reopened, cfg.session)
    restarted = Pipeline(reopened, restarted_engine, cfg, rng=random.Random(17))
    divergent_body = _body("I do something else.")
    divergent_bytes, divergent = restarted.process(stamp, divergent_body)
    assert divergent_bytes == divergent_body
    assert divergent is not None and divergent.semantic_status == 409
    assert reopened.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert reopened.db.execute("SELECT head_turn FROM branches").fetchone()[0] == 1
    assert reopened.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()[0] == "reserved"

    next_stamp = Stamp(**{**stamp.__dict__, "turn": 2})
    next_bytes, next_turn = restarted.process(next_stamp, divergent_body)
    assert next_bytes == divergent_body
    assert next_turn is not None and next_turn.semantic_status == 409
    assert reopened.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert reopened.db.execute("SELECT head_turn FROM branches").fetchone()[0] == 1
    assert reopened.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()[0] == "reserved"

    _packet, resumed = restarted.process(stamp, body)
    assert resumed is not None and resumed.semantic_replay is not None
    assert resumed.turn_index == 1
    assert reopened.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert reopened.db.execute("SELECT head_turn FROM branches").fetchone()[0] == 1
    assert reopened.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 1
    assert reopened.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()[0] == "committed"
    reopened.db.close()


def test_reserved_fresh_t1_refuses_explicit_prefix_fork_without_child_mutation(monkeypatch):
    pipe, store, engine = _runtime()
    parent_stamp = _stamp(session="reserved-fork-parent")
    parent_body = _body()

    def crash_before_settlement(*_args, **_kwargs):
        raise RuntimeError("crash before T1 settlement")

    monkeypatch.setattr(pipe, "_process_truth_gated", crash_before_settlement)
    _forwarded, failed = pipe.process(parent_stamp, parent_body)
    assert failed is not None and failed.semantic_status == 503
    lifecycle = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles"
    ).fetchone()
    assert lifecycle is not None and lifecycle["status"] == "reserved"
    assert lifecycle["active_attempt_index"] is None
    before_database = tuple(store.db.iterdump())
    before_index = {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.session_id,
        )
        for branch_id, view in engine.index.branches.items()
    }

    child_body = json.dumps({
        "model": "semantic-test-model",
        "stream": False,
        "messages": [
            {"role": "user", "content": "I wait."},
            {"role": "user", "content": "I brace."},
        ],
    }).encode("utf-8")
    child_stamp = Stamp(
        session="reserved-fork-child",
        parent="reserved-fork-parent",
        fork_pos=1,
        turn=2,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )
    forwarded, refused = pipe.process(child_stamp, child_body)

    assert forwarded == child_body
    assert refused is not None and refused.semantic_status == 409
    assert tuple(store.db.iterdump()) == before_database
    assert store.db.execute(
        "SELECT COUNT(*) FROM sessions WHERE external_id='reserved-fork-child'"
    ).fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM semantic_turn_attempts").fetchone()[0] == 1
    assert {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.session_id,
        )
        for branch_id, view in engine.index.branches.items()
    } == before_index


def test_terminal_full_prefix_relink_replays_exact_artifact_without_new_turn():
    pipe, store, engine = _runtime()
    engine.cfg.adopt_min_lcp = 1
    body = _body()
    _packet, first = pipe.process(_stamp(session="terminal-relink-old"), body)
    assert first is not None and first.semantic_replay is not None
    source = first.semantic_replay

    _packet, renamed = pipe.process(_stamp(session="terminal-relink-new"), body)

    assert renamed is not None and renamed.semantic_replay is not None
    assert renamed.session_id == first.session_id
    assert renamed.branch_id == first.branch_id
    assert renamed.turn_index == first.turn_index
    assert renamed.semantic_replay == source
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    session = store.db.execute(
        "SELECT external_id FROM sessions WHERE session_id=?", (first.session_id,)
    ).fetchone()
    assert session["external_id"] == "terminal-relink-new"


def test_terminal_full_prefix_relink_refuses_corrupt_source_turn_before_identity_mutation():
    pipe, store, engine = _runtime()
    engine.cfg.adopt_min_lcp = 1
    body = _body()
    _packet, first = pipe.process(_stamp(session="corrupt-relink-old"), body)
    assert first is not None and first.semantic_replay is not None
    with store.transaction():
        store.db.execute(
            "UPDATE turns SET user_hash=? WHERE branch_id=? AND turn_index=?",
            ("f" * 16, first.branch_id, first.turn_index),
        )
    before_database = tuple(store.db.iterdump())
    before_index = {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.session_id,
            view.last_seen,
        )
        for branch_id, view in engine.index.branches.items()
    }

    forwarded, refused = pipe.process(_stamp(session="corrupt-relink-new"), body)

    assert forwarded == body
    assert refused is not None and refused.semantic_status == 409
    assert tuple(store.db.iterdump()) == before_database
    assert {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.session_id,
            view.last_seen,
        )
        for branch_id, view in engine.index.branches.items()
    } == before_index
    session = store.db.execute(
        "SELECT external_id FROM sessions WHERE session_id=?", (first.session_id,)
    ).fetchone()
    assert session["external_id"] == "corrupt-relink-old"


def test_fresh_semantic_t0_visible_turn_is_refused_and_fully_rolled_back():
    pipe, store, engine = _runtime()
    stamp = _stamp(session="invalid-visible-t0")
    stamp = Stamp(**{**stamp.__dict__, "turn": 0})

    _forwarded, ctx = pipe.process(stamp, _body())

    assert ctx is not None and ctx.semantic_status == 409
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == 0
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_bootstrap_proofs"
    ).fetchone()[0] == 0
    assert engine.index.branches == {}


def test_settled_perception_check_terminalizes_with_exact_semantic_journal_coverage():
    pipe, store, _engine = _runtime()

    _packet, ctx = pipe.process(_stamp(), _body("I look around."))

    assert ctx is not None and ctx.semantic_gate and not ctx.semantic_error
    assert ctx.semantic_replay is not None
    ops = [
        op["op"]
        for row in store.diagnostic_turn(ctx.branch_id, ctx.turn_index)["journal"]
        for op in row["ops"]
    ]
    assert "semantic_meaning_commit" in ops
    assert "semantic_binding_commit" in ops
    assert "semantic_frame_commit" in ops
    assert "mechanic_settlement_commit" in ops
    assert "check" in ops


def test_duplicate_and_lost_reply_replay_one_exact_artifact_without_reducer_rerun(
    monkeypatch,
):
    pipe, store, engine = _runtime()
    body = _body()
    stamp = _stamp()
    calls = 0
    original = pipe._process_observed

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pipe, "_process_observed", counted)
    _packet, first = pipe.process(stamp, body)
    _packet, duplicate = pipe.process(stamp, body)
    assert first.semantic_replay is not None
    assert duplicate.semantic_replay is not None
    assert duplicate.semantic_replay.payload == first.semantic_replay.payload
    assert duplicate.semantic_replay.selected_artifact_digest \
        == first.semantic_replay.selected_artifact_digest
    assert calls == 1

    # Simulate the exact proved reply being lost before SillyTavern retained it.  Clearing only
    # the short network dedup cache forces the lifecycle-backed lost-reply classifier.
    engine._dedup.clear()
    _packet, lost = pipe.process(stamp, body)
    assert lost.semantic_replay is not None
    assert lost.semantic_replay.payload == first.semantic_replay.payload
    assert lost.semantic_replay.selected_artifact_digest \
        == first.semantic_replay.selected_artifact_digest
    assert calls == 1
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("observer", "expected_status"),
    [(lambda *_args, **_kwargs: None, 409),
     (lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")), 503)],
)
def test_visible_gate_resolution_failure_never_falls_through(observer, expected_status):
    pipe, store, engine = _runtime()
    engine.observe = observer

    forwarded, ctx = pipe.process(_stamp(), _body())

    assert forwarded == _body()
    assert ctx is not None and ctx.semantic_gate
    assert ctx.semantic_replay is None
    assert ctx.semantic_status == expected_status
    assert ctx.semantic_error
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 0


def test_continue_is_a_proof_safe_conflict_until_it_has_an_explicit_contract():
    pipe, store, _engine = _runtime()

    _forwarded, ctx = pipe.process(_stamp(kind="continue"), _body())

    assert ctx is not None and ctx.semantic_gate
    assert ctx.semantic_replay is None
    assert ctx.semantic_status == 409
    assert ctx.semantic_error == "semantic_turn_conflict"
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_lifecycles"
    ).fetchone()[0] == 0


def test_quiet_and_explicit_session_passthrough_remain_byte_exact():
    pipe, store, _engine = _runtime()
    body = _body()

    quiet_bytes, quiet_ctx = pipe.process(_stamp(kind="quiet"), body)
    assert quiet_bytes == body and quiet_ctx is None

    session_id, _branch_id = store.create_session(external_id="semantic-passthrough")
    store.session_mode_set(session_id, "passthrough")
    pass_bytes, pass_ctx = pipe.process(
        _stamp(session="semantic-passthrough"), body
    )
    assert pass_bytes == body and pass_ctx is None


def test_reserved_turn_cannot_rebind_after_preledger_divergence(monkeypatch):
    pipe, store, _engine = _runtime()
    body = _body()
    stamp = _stamp()
    original_fallback = pipeline_module.build_proof_complete_fallback

    def broken_proof(**_kwargs):
        raise RuntimeError("fault after reducer")

    monkeypatch.setattr(pipeline_module, "build_proof_complete_fallback", broken_proof)
    _packet, failed = pipe.process(stamp, body)
    assert failed is not None and failed.semantic_status == 503
    assert store.db.execute(
        "SELECT status FROM semantic_turn_lifecycles"
    ).fetchone()[0] == "reserved"

    external = apply_delta(
        store,
        failed.session_id,
        failed.branch_id,
        failed.turn_index,
        [{"op": "clock_tick", "minutes": 1}],
        "rule",
        pipe.cfg,
    )
    assert external.applied
    outside_state = external.state
    outside_journal_count = store.db.execute(
        "SELECT COUNT(*) FROM ops_journal"
    ).fetchone()[0]

    monkeypatch.setattr(pipeline_module, "build_proof_complete_fallback", original_fallback)
    _packet, refused = pipe.process(stamp, body)
    assert refused is not None and refused.semantic_status == 503
    assert refused.semantic_replay is None
    assert store.db.execute(
        "SELECT COUNT(*) FROM ops_journal"
    ).fetchone()[0] == outside_journal_count
    # The unrelated committed change survives; the reserved semantic reducer never reruns.
    assert current_state(store, failed.branch_id) == outside_state


def test_proof_failure_rolls_back_ledger_and_process_local_mutations_then_retries(
    monkeypatch,
):
    jobs = SimpleNamespace(models={}, user_names={})
    pipe, store, _engine = _runtime(jobs=jobs)
    body = _body()
    stamp = _stamp()
    before_rng = pipe.rng.getstate()
    before_dump = tuple(store.db.iterdump())
    original_observed = pipe._process_observed
    original_fallback = pipeline_module.build_proof_complete_fallback

    def leaky_observed(*args, **kwargs):
        pipe.rng.random()
        pipe._last_docs["leak"] = {"unsafe": True}
        pipe._request_packets["leak"] = (b"unsafe", None)
        pipe._notices["leak"] = ["unsafe"]
        jobs.models["leak"] = "unsafe"
        jobs.user_names["leak"] = "unsafe"
        return original_observed(*args, **kwargs)

    def broken_proof(**_kwargs):
        raise RuntimeError("proof construction fault")

    monkeypatch.setattr(pipe, "_process_observed", leaky_observed)
    monkeypatch.setattr(pipeline_module, "build_proof_complete_fallback", broken_proof)
    _forwarded, failed = pipe.process(stamp, body)

    assert failed is not None and failed.semantic_status == 503
    assert pipe.rng.getstate() == before_rng
    assert pipe._last_docs == OrderedDict()
    assert pipe._request_packets == OrderedDict()
    assert pipe._notices == OrderedDict()
    assert jobs.models == {}
    assert jobs.user_names == {}
    # The separately proved T0 bootstrap is durable, but visible T1 reducer/journal changes rolled
    # back with the failed artifact transaction.
    journal = store.db.execute(
        "SELECT turn_lo, turn_hi, source, ops FROM ops_journal ORDER BY id"
    ).fetchall()
    assert journal
    assert all(row["turn_lo"] == row["turn_hi"] == 0 for row in journal)
    assert all(row["source"] in {"bootstrap", "genesis"} for row in journal)
    assert store.semantic_bootstrap_proof(failed.session_id, failed.branch_id) is not None
    assert _turn(store)["user_hash"] == content_hash("I wait.")
    assert tuple(store.db.iterdump()) != before_dump

    monkeypatch.setattr(pipeline_module, "build_proof_complete_fallback", original_fallback)
    _packet, retried = pipe.process(stamp, body)
    assert retried is not None and retried.semantic_replay is not None
    assert retried.semantic_replay.status == "fallback_ready"
    committed_ops = [
        op["op"]
        for row in store.diagnostic_turn(retried.branch_id, retried.turn_index)["journal"]
        for op in row["ops"]
    ]
    assert committed_ops.count("clock_tick") == 1


def test_failed_swipe_keeps_prior_tip_and_successful_retry_changes_it_once(monkeypatch):
    pipe, store, engine = _runtime()
    body = _body()
    stamp = _stamp()
    _packet, first = pipe.process(stamp, body)
    replay = first.semantic_replay
    assert replay is not None
    _complete_semantic_delivery(pipe, store, first)
    before_turn = _turn(store)
    before_text = dict(store.db.execute("SELECT * FROM turn_texts").fetchone())
    before_msgs = [dict(row) for row in store.db.execute(
        "SELECT * FROM branch_msgs ORDER BY pos"
    )]
    original_derivation = pipeline_module.derive_swipe_fallback_from_persisted_basis

    def broken_proof(*_args, **_kwargs):
        raise RuntimeError("swipe proof construction fault")

    monkeypatch.setattr(
        pipeline_module,
        "derive_swipe_fallback_from_persisted_basis",
        broken_proof,
    )
    _packet, failed = pipe.process(_stamp(kind="swipe"), body)
    assert failed is not None and failed.semantic_status == 503
    assert _turn(store)["swipe_count"] == before_turn["swipe_count"] == 0
    assert _turn(store)["assistant_hash"] == before_turn["assistant_hash"]
    assert dict(store.db.execute("SELECT * FROM turn_texts").fetchone()) == before_text
    assert [dict(row) for row in store.db.execute(
        "SELECT * FROM branch_msgs ORDER BY pos"
    )] == before_msgs

    monkeypatch.setattr(
        pipeline_module,
        "derive_swipe_fallback_from_persisted_basis",
        original_derivation,
    )
    _packet, succeeded = pipe.process(_stamp(kind="swipe"), body)
    assert succeeded is not None and succeeded.semantic_replay is not None
    assert _turn(store)["swipe_count"] == 1
    assert _turn(store)["assistant_hash"] is None
    assert store.db.execute(
        "SELECT assistant_text FROM turn_texts"
    ).fetchone()[0] is None
    assert len(engine.index.branches[succeeded.branch_id].msgs) == len(before_msgs)


def test_restart_replays_unclaimed_terminal_swipe_without_a_second_context_mutation(
    tmp_path,
):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    path = tmp_path / "unclaimed-swipe-restart.db"
    body = _body()
    initial_stamp = _stamp(session="unclaimed-swipe-restart")
    swipe_stamp = _stamp(session="unclaimed-swipe-restart", kind="swipe")

    store = Store(path)
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(17),
    )
    _packet, initial_ctx = pipe.process(initial_stamp, body)
    initial = initial_ctx.semantic_replay
    assert initial is not None
    _complete_semantic_delivery(pipe, store, initial_ctx)

    _selector, first_swipe_ctx = pipe.process(swipe_stamp, body)
    first_swipe = first_swipe_ctx.semantic_replay
    assert first_swipe is not None and first_swipe.attempt_index == 1
    assert first_swipe.status == "fallback_ready"
    assert store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_delivery_claims"
        " WHERE lifecycle_key=? AND attempt_index=?",
        (first_swipe.lifecycle_key, first_swipe.attempt_index),
    ).fetchone()[0] == 0

    def durable_context_snapshot(active_store: Store) -> dict:
        return {
            "lifecycle": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM semantic_turn_lifecycles ORDER BY lifecycle_key"
            )],
            "attempts": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM semantic_turn_attempts ORDER BY lifecycle_key, attempt_index"
            )],
            "claims": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM semantic_turn_delivery_claims"
                " ORDER BY lifecycle_key, attempt_index"
            )],
            "turns": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM turns ORDER BY branch_id, turn_index"
            )],
            "messages": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM branch_msgs ORDER BY branch_id, pos"
            )],
            "texts": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM turn_texts ORDER BY branch_id, turn_index"
            )],
            "journal": [dict(row) for row in active_store.db.execute(
                "SELECT * FROM ops_journal ORDER BY id"
            )],
            "state": current_state(active_store, first_swipe_ctx.branch_id),
        }

    before_restart = durable_context_snapshot(store)
    assert before_restart["turns"][0]["swipe_count"] == 1
    assert before_restart["lifecycle"][0]["active_attempt_index"] == 1
    store.close()

    reopened = Store(path)
    restarted = Pipeline(
        reopened,
        SessionEngine(reopened, cfg.session),
        cfg,
        rng=random.Random(17),
    )
    _selector, recovered_ctx = restarted.process(swipe_stamp, body)
    recovered = recovered_ctx.semantic_replay

    assert recovered is not None
    assert recovered.attempt_index == first_swipe.attempt_index
    assert recovered.status == first_swipe.status
    assert recovered.payload == first_swipe.payload
    assert recovered.payload_hash == first_swipe.payload_hash
    assert recovered.logical_message_id == first_swipe.logical_message_id
    assert recovered.selected_artifact_digest == first_swipe.selected_artifact_digest
    assert durable_context_snapshot(reopened) == before_restart

    # A first-byte claim alone does not prove normal response completion.  Crash/reopen after the
    # claim must still recover the same swipe without another attempt/count/pointer transition.
    reopened.turn_lifecycle.claim_delivery(
        recovered.lifecycle_key,
        recovered.attempt_index,
        expected_logical_message_id=recovered.logical_message_id,
        expected_artifact_digest=recovered.selected_artifact_digest,
    )
    after_claim = durable_context_snapshot(reopened)
    assert reopened.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_delivery_completions"
        " WHERE lifecycle_key=? AND attempt_index=?",
        (recovered.lifecycle_key, recovered.attempt_index),
    ).fetchone()[0] == 0
    reopened.close()

    after_claim_store = Store(path)
    after_claim_pipe = Pipeline(
        after_claim_store,
        SessionEngine(after_claim_store, cfg.session),
        cfg,
        rng=random.Random(17),
    )
    _selector, claimed_recovery_ctx = after_claim_pipe.process(swipe_stamp, body)
    claimed_recovery = claimed_recovery_ctx.semantic_replay
    assert claimed_recovery is not None
    assert claimed_recovery.attempt_index == recovered.attempt_index
    assert claimed_recovery.payload == recovered.payload
    assert durable_context_snapshot(after_claim_store) == after_claim

    # Only normal generator completion after the exact claim unlocks a genuinely later swipe.
    after_claim_pipe.on_response(
        claimed_recovery_ctx,
        claimed_recovery.payload,
        claimed_recovery.content_type,
    )
    after_claim_store.turn_lifecycle.complete_delivery(
        claimed_recovery.lifecycle_key,
        claimed_recovery.attempt_index,
        expected_logical_message_id=claimed_recovery.logical_message_id,
        expected_artifact_digest=claimed_recovery.selected_artifact_digest,
    )
    _selector, later_ctx = after_claim_pipe.process(swipe_stamp, body)
    later = later_ctx.semantic_replay
    assert later is not None and later.attempt_index == claimed_recovery.attempt_index + 1
    assert later.payload == claimed_recovery.payload
    assert after_claim_store.db.execute(
        "SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
        (later_ctx.branch_id, later_ctx.turn_index),
    ).fetchone()[0] == 2
    lifecycle = after_claim_store.db.execute(
        "SELECT active_attempt_index FROM semantic_turn_lifecycles"
        " WHERE lifecycle_key=?",
        (later.lifecycle_key,),
    ).fetchone()
    assert lifecycle is not None and lifecycle["active_attempt_index"] == 2
    assert after_claim_store.db.execute(
        "SELECT COUNT(*) FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (later.lifecycle_key,),
    ).fetchone()[0] == 3
    after_claim_store.close()


def test_swipe_reuses_persisted_fallback_contract_plan_and_basis_after_builder_drift(
    monkeypatch,
):
    pipe, store, _engine = _runtime()
    body = _body()
    _packet, first = pipe.process(_stamp(), body)
    assert first is not None and first.semantic_replay is not None
    source_replay = first.semantic_replay
    _complete_semantic_delivery(pipe, store, first)
    source_fallback = store.turn_lifecycle.fallback_artifact(
        source_replay.lifecycle_key,
        attempt_index=source_replay.attempt_index,
    )
    source_basis = extract_persisted_narration_basis(source_fallback)

    def current_builder_must_not_run(*_args, **_kwargs):
        raise AssertionError("a swipe cannot rebuild authority from current code")

    monkeypatch.setattr(
        pipeline_module,
        "build_proof_complete_fallback",
        current_builder_must_not_run,
    )
    monkeypatch.setattr(
        basis_module,
        "build_narration_realization_plan",
        current_builder_must_not_run,
    )
    _packet, swipe = pipe.process(_stamp(kind="swipe"), body)
    assert swipe is not None and swipe.semantic_replay is not None
    swipe_replay = swipe.semantic_replay
    swipe_fallback = store.turn_lifecycle.fallback_artifact(
        swipe_replay.lifecycle_key,
        attempt_index=swipe_replay.attempt_index,
    )

    assert swipe_replay.attempt_index > source_replay.attempt_index
    assert swipe_fallback.fallback_bytes == source_fallback.fallback_bytes
    assert swipe_fallback.envelope["delivery_proof"] \
        == source_fallback.envelope["delivery_proof"]
    assert swipe_fallback.envelope["occurrences"] == source_fallback.envelope["occurrences"]
    assert swipe_fallback.envelope["effects"] == source_fallback.envelope["effects"]
    assert swipe_fallback.envelope["ledger"] == source_fallback.envelope["ledger"]
    assert extract_persisted_narration_basis(swipe_fallback) == source_basis

    reopen = store.turn_lifecycle.replay(
        swipe_replay.lifecycle_key,
        reason="reopen",
    )
    lost_reply = store.turn_lifecycle.replay(
        swipe_replay.lifecycle_key,
        reason="lost_reply",
    )
    assert reopen.payload == lost_reply.payload == swipe_replay.payload
    assert reopen.payload_hash == lost_reply.payload_hash == swipe_replay.payload_hash
    assert extract_persisted_narration_basis(reopen) == source_basis
    assert extract_persisted_narration_basis(lost_reply) == source_basis


def test_current_ledger_drift_rejects_swipe_and_leaves_prior_terminal_active(monkeypatch):
    pipe, store, _engine = _runtime()
    body = _body()
    _packet, first = pipe.process(_stamp(), body)
    assert first is not None and first.semantic_replay is not None
    prior = first.semantic_replay
    _complete_semantic_delivery(pipe, store, first)
    prior_turn = _turn(store)

    def drifted_state(actual_store, branch_id):
        state = deepcopy(current_state(actual_store, branch_id))
        state["_forged_current_drift"] = True
        return state

    monkeypatch.setattr(pipeline_module, "current_state", drifted_state)
    _packet, failed = pipe.process(_stamp(kind="swipe"), body)

    assert failed is not None and failed.semantic_status == 503
    active = store.turn_lifecycle.replay(prior.lifecycle_key, reason="reopen")
    assert active.attempt_index == prior.attempt_index
    assert active.payload == prior.payload
    assert _turn(store)["swipe_count"] == prior_turn["swipe_count"] == 0
    lifecycle = store.db.execute(
        "SELECT active_attempt_index FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (prior.lifecycle_key,),
    ).fetchone()
    attempts = store.db.execute(
        "SELECT attempt_index, status FROM semantic_turn_attempts"
        " WHERE lifecycle_key=? ORDER BY attempt_index",
        (prior.lifecycle_key,),
    ).fetchall()
    assert int(lifecycle["active_attempt_index"]) == prior.attempt_index
    assert [(row["attempt_index"], row["status"]) for row in attempts] == [
        (prior.attempt_index, prior.status),
        (prior.attempt_index + 1, "reserved"),
    ]


def test_early_fork_copies_exact_prefix_artifact_rebinds_basis_and_isolates_source():
    pipe, store, _engine = _runtime()
    _packet, first = pipe.process(_stamp(), _body())
    assert first is not None and first.semantic_replay is not None
    parent = first.semantic_replay
    parent_basis = extract_persisted_narration_basis(parent)
    parent_rows = [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (parent.lifecycle_key,),
    )]

    child_branch = store.fork_branch(
        first.branch_id,
        at_pos=1,
        fork_turn=first.turn_index,
    )
    child_lifecycle = store.db.execute(
        "SELECT * FROM semantic_turn_lifecycles WHERE branch_id=?",
        (child_branch,),
    ).fetchone()
    assert child_lifecycle is not None
    child = store.turn_lifecycle.replay(
        child_lifecycle["lifecycle_key"],
        reason="reopen",
    )
    child_basis = extract_persisted_narration_basis(child)
    parent_after = store.turn_lifecycle.replay(parent.lifecycle_key, reason="reopen")

    assert parent_after == parent
    assert [dict(row) for row in store.db.execute(
        "SELECT * FROM semantic_turn_attempts WHERE lifecycle_key=? ORDER BY attempt_index",
        (parent.lifecycle_key,),
    )] == parent_rows
    assert child.payload == parent.payload
    assert child.payload_hash == parent.payload_hash
    assert child.envelope["pre_mutation_key"]["session_id"] == first.session_id
    assert child.envelope["pre_mutation_key"]["branch_id"] == child_branch
    assert child.envelope["lifecycle_key"] != parent.lifecycle_key
    assert child.envelope["lineage"] == {
        "source_lifecycle_key": parent.lifecycle_key,
        "source_envelope_fingerprint": parent.envelope["envelope_fingerprint"],
    }
    assert child_basis["source_transition_projection"] \
        == parent_basis["source_transition_projection"]
    assert child_basis["source_truth_contract"] == parent_basis["source_truth_contract"]
    assert child_basis["source_lifecycle_binding"] == parent_basis["source_lifecycle_binding"]
    assert child_basis["current_lifecycle_binding"]["branch_ref"] == child_branch


def test_corrupt_stored_terminal_artifact_is_refused_before_replay_bytes():
    pipe, store, engine = _runtime()
    body = _body()
    stamp = _stamp()
    _packet, first = pipe.process(stamp, body)
    assert first.semantic_replay is not None
    with store.transaction():
        store.db.execute(
            "UPDATE semantic_turn_attempts SET fallback_bytes=?",
            (b'{"choices":[{"message":{"content":"The living guard dies."}}]}',),
        )
    engine._dedup.clear()

    _packet, refused = pipe.process(stamp, body)

    assert refused is not None and refused.semantic_gate
    assert refused.semantic_replay is None
    assert refused.semantic_status == 503
