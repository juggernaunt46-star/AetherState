from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from aetherstate.config import Config, record_runtime_config_overrides
from aetherstate.control import make_control_router
from aetherstate.pipeline import Pipeline, PostContext
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.store import Store


@pytest.mark.asyncio
async def test_session_experience_precedence_lock_and_relay_mode_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        from aetherstate.config import finalize_experience_profile_base
        from aetherstate.experience import (
            CHAT,
            RPG,
            ExperienceModeLocked,
            config_for_experience,
            infer_legacy_experience,
            normalize_experience_mode,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"per-session experience contract is missing: {exc}")

    cfg = Config()
    cfg.specialization.name = "rpg"
    finalize_experience_profile_base(cfg)
    store = Store(":memory:")

    blank = store.get_or_create_session("blank")
    inferred = infer_legacy_experience(blank, {}, None, fallback=CHAT)
    assert inferred == (CHAT, "default")
    binding = store.experience_inference_set_unlocked(
        blank["session_id"], inferred[0], inferred[1]
    )
    assert binding.mode == CHAT
    assert store.session_mode(blank["session_id"]) == "enriched"

    narrator = store.get_or_create_session("narrator")
    narrator_stamp = Stamp(
        session="narrator", speaker="Narrator", card_role="narrator"
    )
    inferred = infer_legacy_experience(narrator, {}, narrator_stamp, fallback=CHAT)
    assert inferred == (RPG, "card:narrator")
    store.experience_inference_set_unlocked(
        narrator["session_id"], inferred[0], inferred[1]
    )
    store.narrator_speaker_set(narrator["session_id"], "Narrator")
    stored_narrator = store.db.execute(
        "SELECT * FROM sessions WHERE session_id=?", (narrator["session_id"],)
    ).fetchone()
    assert infer_legacy_experience(
        stored_narrator, {}, None, fallback=CHAT
    ) == (RPG, "stored:narrator")
    assert infer_legacy_experience(
        stored_narrator,
        {},
        Stamp(session="narrator", card_role="character"),
        fallback=RPG,
    ) == (CHAT, "card:character")
    character = store.get_or_create_session("character")
    character_stamp = Stamp(
        session="character", speaker="Dane", card_role="character"
    )
    assert infer_legacy_experience(
        character, {"player": {"legacy": {}}}, character_stamp, fallback=RPG
    ) == (CHAT, "card:character")
    with pytest.raises(ValueError, match="chat\\|rpg"):
        infer_legacy_experience(
            character,
            {},
            Stamp(session="character", mode="banana"),
            fallback=RPG,
        )

    assert normalize_experience_mode("none") == CHAT

    parent = store.get_or_create_session("parent")
    store.experience_mode_set_unlocked(parent["session_id"], RPG)
    child_id, _ = store.create_session("child")
    assert store.inherit_session_settings(parent["session_id"], child_id)
    child = store.experience_binding(child_id)
    assert (child.mode, child.source, child.locked_turn) == (RPG, "explicit", None)

    selected = store.experience_mode_set_unlocked(narrator["session_id"], CHAT)
    narrator = store.db.execute(
        "SELECT * FROM sessions WHERE session_id=?", (narrator["session_id"],)
    ).fetchone()
    assert infer_legacy_experience(
        narrator, {}, narrator_stamp, fallback=RPG
    ) == (CHAT, "explicit")
    assert selected.mode == CHAT

    app = FastAPI()
    app.include_router(make_control_router(cfg, store))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-aetherstate"
    ) as client:
        changed = await client.post(
            f"/aether/session/{blank['session_id']}/experience-mode",
            json={"mode": "rpg"},
        )
        assert changed.status_code == 200
        assert changed.json()["mode"] == RPG

        published_pipe = Pipeline(
            store,
            SessionEngine(store, cfg.session),
            cfg,
            playerlex_service=None,
            player_lessons_service=None,
        )
        request_body = json.dumps(
            {"model": "test", "messages": [{"role": "user", "content": "Hello."}]}
        ).encode()
        _, published_ctx = published_pipe.process(
            Stamp(
                session="blank",
                speaker="Narrator",
                card_role="narrator",
            ),
            request_body,
        )
        assert published_ctx is not None
        accepted = json.dumps(
            {"choices": [{"message": {"content": "The answer arrives."}}]}
        ).encode()
        published_pipe.on_response(published_ctx, accepted, "application/json")
        published_binding = store.experience_binding(blank["session_id"])
        assert published_binding.locked_turn == published_ctx.turn_index
        locked = await client.post(
            f"/aether/session/{blank['session_id']}/experience-mode",
            json={"mode": "chat"},
        )
        assert locked.status_code == 409
        assert locked.json() == {
            "locked": True,
            "locked_turn": published_ctx.turn_index,
            "mode": RPG,
        }

        malformed_genesis = await client.post(
            "/aether/session/malformed-genesis/genesis",
            json={
                "mode": "banana",
                "speaker": "Narrator",
                "card_role": "narrator",
                "card": "A card that must not seed.",
                "greeting": "A greeting that must not seed.",
            },
        )
        assert malformed_genesis.status_code == 200
        assert malformed_genesis.json()["applied"] == 0
        assert malformed_genesis.json()["scheduled"] is False
        assert store.db.execute(
            "SELECT 1 FROM sessions WHERE external_id='malformed-genesis'"
        ).fetchone() is None

    for external_id, response_kind in (
        ("empty-response", "empty"),
        ("failed-response", "failed"),
        ("suppressed-response", "suppressed"),
    ):
        row = store.get_or_create_session(external_id)
        store.experience_mode_set_unlocked(row["session_id"], CHAT)
        pipe = Pipeline(
            store,
            SessionEngine(store, cfg.session),
            cfg,
            playerlex_service=None,
            player_lessons_service=None,
        )
        _, ctx = pipe.process(
            Stamp(session=external_id, speaker="Dane", card_role="character"),
            request_body,
        )
        assert ctx is not None
        if response_kind == "empty":
            pipe.on_response(
                ctx,
                json.dumps({"choices": [{"message": {"content": ""}}]}).encode(),
                "application/json",
            )
        elif response_kind == "failed":
            pipe.on_upstream_error(ctx, 503, b'{"error":{"message":"failed"}}')
        else:
            ctx.suppress_cold_path = True
            pipe.on_response(ctx, accepted, "application/json")
        assert store.experience_binding(row["session_id"]).locked_turn is None

    malformed_store = Store(":memory:")
    malformed = malformed_store.get_or_create_session("malformed-mode")
    malformed_pipe = Pipeline(
        malformed_store,
        SessionEngine(malformed_store, cfg.session),
        cfg,
        playerlex_service=None,
        player_lessons_service=None,
    )
    malformed_before = malformed_store.db.execute(
        "SELECT head_turn FROM branches WHERE branch_id=?", (malformed["active_branch"],)
    ).fetchone()[0]
    malformed_out, malformed_ctx = malformed_pipe.process(
        Stamp(session="malformed-mode", mode="banana"), request_body
    )
    malformed_after = malformed_store.db.execute(
        "SELECT head_turn FROM branches WHERE branch_id=?", (malformed["active_branch"],)
    ).fetchone()[0]
    assert malformed_out == request_body
    assert malformed_ctx is None
    assert malformed_after == malformed_before

    semantic_cfg = Config()
    semantic_cfg.specialization.name = "rpg"
    semantic_cfg.specialization.semantic_truth_gate = True
    record_runtime_config_overrides(
        semantic_cfg,
        "specialization",
        {"name": "rpg", "semantic_truth_gate": True},
    )
    finalize_experience_profile_base(semantic_cfg)
    semantic_store = Store(":memory:")
    semantic_chat = semantic_store.get_or_create_session("semantic-chat")
    semantic_store.experience_mode_set_unlocked(semantic_chat["session_id"], CHAT)
    semantic_pipe = Pipeline(
        semantic_store,
        SessionEngine(semantic_store, semantic_cfg.session),
        semantic_cfg,
        playerlex_service=None,
        player_lessons_service=None,
    )
    _, semantic_ctx = semantic_pipe.process(
        Stamp(session="semantic-chat", speaker="Dane", card_role="character"),
        request_body,
    )
    assert semantic_ctx is not None
    assert semantic_ctx.experience_mode == CHAT
    assert semantic_ctx.semantic_gate is False

    failed_bootstrap_store = Store(":memory:")
    failed_bootstrap_pipe = Pipeline(
        failed_bootstrap_store,
        SessionEngine(failed_bootstrap_store, semantic_cfg.session),
        semantic_cfg,
        playerlex_service=None,
        player_lessons_service=None,
    )

    def fail_after_semantic_bootstrap(*_args, **_kwargs):
        raise RuntimeError("forced semantic T1 settlement failure")

    monkeypatch.setattr(
        failed_bootstrap_pipe, "_process_observed", fail_after_semantic_bootstrap
    )
    failed_packet, failed_bootstrap_ctx = failed_bootstrap_pipe.process(
        Stamp(
            session="failed-semantic-bootstrap-reset",
            turn=1,
            speaker="Narrator",
            card_role="narrator",
            user="Bean",
        ),
        request_body,
    )
    assert failed_packet == request_body
    assert failed_bootstrap_ctx is not None
    assert failed_bootstrap_ctx.semantic_gate is True
    assert failed_bootstrap_ctx.semantic_status == 503
    assert failed_bootstrap_ctx.semantic_error == "semantic_turn_unavailable"
    failed_bootstrap_sid = failed_bootstrap_ctx.session_id
    failed_bootstrap_bid = failed_bootstrap_ctx.branch_id
    assert failed_bootstrap_store.db.execute(
        "SELECT 1 FROM semantic_bootstrap_proofs WHERE session_id=?",
        (failed_bootstrap_sid,),
    ).fetchone() is not None
    assert failed_bootstrap_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=?"
        " AND source IN ('bootstrap','genesis')",
        (failed_bootstrap_bid,),
    ).fetchone() is not None
    assert failed_bootstrap_store.db.execute(
        "SELECT 1 FROM checkpoints WHERE branch_id=?",
        (failed_bootstrap_bid,),
    ).fetchone() is not None
    failed_lifecycle = failed_bootstrap_store.db.execute(
        "SELECT lifecycle_key, key_json FROM semantic_turn_lifecycles"
        " WHERE session_id=?",
        (failed_bootstrap_sid,),
    ).fetchone()
    assert failed_lifecycle is not None
    assert failed_bootstrap_store.db.execute(
        "SELECT 1 FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (failed_lifecycle["lifecycle_key"],),
    ).fetchone() is not None
    failed_key = json.loads(failed_lifecycle["key_json"])

    reset_binding = failed_bootstrap_store.reset_unlocked_experience(
        failed_bootstrap_sid, CHAT
    )
    assert reset_binding.mode == CHAT
    for table in (
        "semantic_bootstrap_proofs",
        "ops_journal",
        "checkpoints",
        "semantic_turn_delivery_completions",
        "semantic_turn_delivery_claims",
        "semantic_turn_attempts",
        "semantic_turn_lifecycles",
    ):
        assert failed_bootstrap_store.db.execute(
            f"SELECT 1 FROM {table} WHERE "
            + (
                "session_id=?"
                if table in {"semantic_bootstrap_proofs", "semantic_turn_lifecycles"}
                else "branch_id=?"
                if table in {"ops_journal", "checkpoints"}
                else "lifecycle_key=?"
            ),
            (
                failed_bootstrap_sid
                if table in {"semantic_bootstrap_proofs", "semantic_turn_lifecycles"}
                else failed_bootstrap_bid
                if table in {"ops_journal", "checkpoints"}
                else failed_lifecycle["lifecycle_key"],
            ),
        ).fetchone() is None

    corrupt_delivery_store = Store(":memory:")
    corrupt_delivery_pipe = Pipeline(
        corrupt_delivery_store,
        SessionEngine(corrupt_delivery_store, semantic_cfg.session),
        semantic_cfg,
        playerlex_service=None,
        player_lessons_service=None,
    )
    monkeypatch.setattr(
        corrupt_delivery_pipe, "_process_observed", fail_after_semantic_bootstrap
    )
    _, corrupt_delivery_ctx = corrupt_delivery_pipe.process(
        Stamp(
            session="fabricated-bootstrap-delivery",
            turn=1,
            speaker="Narrator",
            card_role="narrator",
            user="Bean",
        ),
        request_body,
    )
    assert corrupt_delivery_ctx is not None
    assert corrupt_delivery_ctx.semantic_status == 503
    corrupt_delivery_lifecycle = corrupt_delivery_store.db.execute(
        "SELECT lifecycle_key, key_json FROM semantic_turn_lifecycles"
        " WHERE session_id=?",
        (corrupt_delivery_ctx.session_id,),
    ).fetchone()
    assert corrupt_delivery_lifecycle is not None
    corrupt_delivery_key = json.loads(corrupt_delivery_lifecycle["key_json"])
    corrupt_delivery_store.db.execute(
        "INSERT INTO semantic_turn_delivery_claims("
        "lifecycle_key, attempt_index, logical_message_id, artifact_digest, status,"
        " claimed_at) VALUES(?,0,?,?,'claimed',0.0)",
        (
            corrupt_delivery_lifecycle["lifecycle_key"],
            corrupt_delivery_key["lifecycle_key"],
            corrupt_delivery_key["pre_ledger_hash"],
        ),
    )
    corrupt_delivery_store.db.execute(
        "INSERT INTO semantic_turn_delivery_completions("
        "lifecycle_key, attempt_index, logical_message_id, artifact_digest, status,"
        " completed_at) VALUES(?,0,?,?,'completed',0.0)",
        (
            corrupt_delivery_lifecycle["lifecycle_key"],
            corrupt_delivery_key["lifecycle_key"],
            corrupt_delivery_key["pre_ledger_hash"],
        ),
    )
    corrupt_delivery_before = tuple(corrupt_delivery_store.db.iterdump())
    with pytest.raises(ExperienceModeLocked):
        corrupt_delivery_store.reset_unlocked_experience(
            corrupt_delivery_ctx.session_id, CHAT
        )
    assert tuple(corrupt_delivery_store.db.iterdump()) == corrupt_delivery_before
    assert corrupt_delivery_store.experience_binding(
        corrupt_delivery_ctx.session_id
    ).mode == RPG

    from aetherstate.turn_lifecycle import build_pre_mutation_key

    unmatched_lifecycle_store = Store(":memory:")
    unmatched_sid, unmatched_bid = unmatched_lifecycle_store.create_session(
        "unmatched-lifecycle-only"
    )
    unmatched_lifecycle_store.experience_mode_set_unlocked(unmatched_sid, RPG)
    unmatched_key = build_pre_mutation_key(
        session_id=unmatched_sid,
        branch_id=unmatched_bid,
        turn_index=failed_key["turn_index"],
        accepted_prefix_pos=failed_key["accepted_prefix_pos"],
        accepted_head_hash=failed_key["accepted_head_hash"],
        player_input_hash=failed_key["player_input_hash"],
        pre_ledger_hash=failed_key["pre_ledger_hash"],
        pending_intent_fingerprint=failed_key["pending_intent_fingerprint"],
        semantic_contract_version=failed_key["semantic_contract_version"],
    )
    unmatched_reservation = unmatched_lifecycle_store.turn_lifecycle.reserve(
        unmatched_key
    )
    with pytest.raises(ExperienceModeLocked):
        unmatched_lifecycle_store.reset_unlocked_experience(unmatched_sid, CHAT)
    assert unmatched_lifecycle_store.db.execute(
        "SELECT 1 FROM semantic_turn_lifecycles WHERE lifecycle_key=?",
        (unmatched_reservation.lifecycle_key,),
    ).fetchone() is not None
    assert unmatched_lifecycle_store.db.execute(
        "SELECT 1 FROM semantic_turn_attempts WHERE lifecycle_key=?",
        (unmatched_reservation.lifecycle_key,),
    ).fetchone() is not None

    parent_body = json.dumps(
        {"model": "test", "messages": [{"role": "user", "content": "Parent opening."}]}
    ).encode()
    semantic_parent = semantic_store.get_or_create_session("semantic-parent")
    semantic_store.experience_mode_set_unlocked(semantic_parent["session_id"], CHAT)
    _, parent_ctx = semantic_pipe.process(
        Stamp(session="semantic-parent", speaker="Dane", card_role="character"),
        parent_body,
    )
    assert parent_ctx is not None and parent_ctx.semantic_gate is False
    semantic_pipe.on_response(parent_ctx, accepted, "application/json")

    unstamped_body = json.dumps(
        {
            "model": "test",
            "messages": [
                {"role": "user", "content": "Parent opening."},
                {"role": "user", "content": "Unstamped continuation."},
            ],
        }
    ).encode()
    _, unstamped_ctx = semantic_pipe.process(None, unstamped_body)
    assert unstamped_ctx is not None
    assert unstamped_ctx.session_id == semantic_parent["session_id"]
    assert unstamped_ctx.experience_mode == CHAT
    assert unstamped_ctx.semantic_gate is False
    semantic_pipe.on_response(unstamped_ctx, accepted, "application/json")

    child_body = json.dumps(
        {
            "model": "test",
            "messages": [
                {"role": "user", "content": "Parent opening."},
                {"role": "user", "content": "Child divergence."},
            ],
        }
    ).encode()
    _, unseen_child_ctx = semantic_pipe.process(
        Stamp(
            session="semantic-child",
            parent="semantic-parent",
            fork_pos=1,
            speaker="Narrator",
            card_role="narrator",
        ),
        child_body,
    )
    assert unseen_child_ctx is not None
    assert unseen_child_ctx.experience_mode == CHAT
    assert unseen_child_ctx.semantic_gate is False
    child_row = semantic_store.db.execute(
        "SELECT session_id FROM sessions WHERE external_id='semantic-child'"
    ).fetchone()
    assert child_row is not None
    assert unseen_child_ctx.session_id == child_row["session_id"]

    class RecordingJobs:
        def __init__(self) -> None:
            self.models = {}
            self.user_names = {}
            self.notifications: list[object] = []
            self.endpoints: list[object] = []
            self._tasks = set()
            self.ladder = SimpleNamespace(get_client=lambda: None)

        def notify(self, _sid, _bid, _turn, *, request_cfg=None):
            self.notifications.append(request_cfg)

        def endpoint_for(self, _sid, request_cfg=None):
            self.endpoints.append(request_cfg)
            return SimpleNamespace(base_url="", model=""), "main", 1

    def response_local_case(
        global_mode: str,
        selected_mode: str,
        *,
        mutate_after_request: bool = False,
    ) -> tuple[list[object], list[object], PostContext]:
        local_cfg = Config()
        local_cfg.specialization.name = global_mode
        record_runtime_config_overrides(
            local_cfg, "specialization", {"name": global_mode}
        )
        finalize_experience_profile_base(local_cfg)
        local_store = Store(":memory:")
        row = local_store.get_or_create_session(f"{global_mode}-{selected_mode}")
        local_store.experience_mode_set_unlocked(row["session_id"], selected_mode)
        jobs = RecordingJobs()
        local_pipe = Pipeline(
            local_store,
            SessionEngine(local_store, local_cfg.session),
            local_cfg,
            jobs=jobs,
            playerlex_service=None,
            player_lessons_service=None,
        )
        seen: list[object] = []

        def capture(*args, **_kwargs):
            seen.append(args[-1])

        local_pipe._ingest_delivered_claims = capture
        local_pipe._ingest_reply_tags = capture
        local_pipe._discover = capture
        local_pipe._recall_pass = capture
        local_pipe._lint_pass = capture
        local_pipe._genesis_pass = capture
        local_pipe._evolve_pass = capture
        role = "narrator" if selected_mode == RPG else "character"
        _, ctx = local_pipe.process(
            Stamp(
                session=f"{global_mode}-{selected_mode}",
                speaker="Narrator" if role == "narrator" else "Dane",
                card_role=role,
            ),
            request_body,
        )
        assert ctx is not None and ctx.experience_mode == selected_mode
        if mutate_after_request:
            record_runtime_config_overrides(
                local_cfg,
                "specialization",
                {"name": RPG if global_mode == "none" else "none"},
            )
            local_cfg.specialization.name = RPG if global_mode == "none" else "none"
            finalize_experience_profile_base(local_cfg)
        local_pipe.on_response(ctx, accepted, "application/json")
        assert local_store.experience_binding(row["session_id"]).locked
        return seen, jobs.notifications, ctx

    chat_seen, chat_jobs, _ = response_local_case("rpg", CHAT)
    assert chat_seen and {item.specialization.name for item in chat_seen} == {"none"}
    assert [item.specialization.name for item in chat_jobs] == ["none"]
    rpg_seen, rpg_jobs, _ = response_local_case("none", RPG)
    assert rpg_seen and {item.specialization.name for item in rpg_seen} == {"rpg"}
    assert [item.specialization.name for item in rpg_jobs] == ["rpg"]
    snapshot_seen, snapshot_jobs, snapshot_ctx = response_local_case(
        "rpg", CHAT, mutate_after_request=True
    )
    assert snapshot_ctx.request_cfg is not None
    assert snapshot_ctx.request_cfg.specialization.name == "none"
    assert snapshot_seen and all(item is snapshot_ctx.request_cfg for item in snapshot_seen)
    assert snapshot_jobs == [snapshot_ctx.request_cfg]

    from aetherstate import pipeline as pipeline_module

    mechanics_rpg_cfg = Config()
    mechanics_rpg_cfg.specialization.name = "rpg"
    finalize_experience_profile_base(mechanics_rpg_cfg)
    mechanics_chat_request_cfg = config_for_experience(mechanics_rpg_cfg, CHAT)
    mechanics_rpg_store = Store(":memory:")
    mechanics_rpg_sid, mechanics_rpg_bid = mechanics_rpg_store.create_session(
        "mechanics-global-rpg"
    )
    mechanics_rpg_pipe = Pipeline(
        mechanics_rpg_store,
        SessionEngine(mechanics_rpg_store, mechanics_rpg_cfg.session),
        mechanics_rpg_cfg,
        jobs=None,
        playerlex_service=None,
        player_lessons_service=None,
    )
    mechanics_none_cfg = Config()
    finalize_experience_profile_base(mechanics_none_cfg)
    mechanics_rpg_request_cfg = config_for_experience(mechanics_none_cfg, RPG)
    mechanics_rpg_request_cfg.specialization.war_room = False
    mechanics_rpg_request_cfg.specialization.living_world = False
    mechanics_rpg_request_cfg.specialization.hardcore = True
    mechanics_none_store = Store(":memory:")
    mechanics_none_sid, mechanics_none_bid = mechanics_none_store.create_session(
        "mechanics-global-none"
    )
    mechanics_none_pipe = Pipeline(
        mechanics_none_store,
        SessionEngine(mechanics_none_store, mechanics_none_cfg.session),
        mechanics_none_cfg,
        jobs=None,
        playerlex_service=None,
        player_lessons_service=None,
    )

    def swipe_result(session_id: str, branch_id: str):
        return SimpleNamespace(
            session_id=session_id,
            branch_id=branch_id,
            turn_index=0,
            klass=SimpleNamespace(value="swipe"),
            stamp=Stamp(session="mechanics", user="Bean"),
        )

    retry_doc = {
        "messages": [{"role": "user", "content": "Repeat the settled action."}]
    }
    assert mechanics_rpg_pipe._reserve_lost_turn(
        swipe_result(mechanics_rpg_sid, mechanics_rpg_bid),
        retry_doc,
        {},
        mechanics_chat_request_cfg,
    ) is None
    assert mechanics_none_pipe._reserve_lost_turn(
        swipe_result(mechanics_none_sid, mechanics_none_bid),
        retry_doc,
        {},
        mechanics_rpg_request_cfg,
    )["kind"] == "swipe_replay"

    progression_calls: list[bool] = []
    progression_apply_configs: list[object] = []

    def record_progression(_state, _applied, *, hardcore=False):
        progression_calls.append(hardcore)
        return [{"op": "progression_probe"}]

    def record_progression_apply(
        _store, _sid, _bid, _turn, _ops, _source, request_cfg
    ):
        progression_apply_configs.append(request_cfg)
        return SimpleNamespace(state={"player": {"hero": {}}}, applied=[])

    monkeypatch.setattr(pipeline_module, "progression_ops", record_progression)
    monkeypatch.setattr(pipeline_module, "apply_delta", record_progression_apply)
    monkeypatch.setattr(mechanics_rpg_pipe, "_index_memories", lambda *_args: None)
    monkeypatch.setattr(mechanics_none_pipe, "_index_memories", lambda *_args: None)
    mechanics_rpg_pipe._progress(
        swipe_result(mechanics_rpg_sid, mechanics_rpg_bid),
        {"player": {"hero": {}}},
        [],
        mechanics_chat_request_cfg,
    )
    assert progression_calls == []
    mechanics_none_pipe._progress(
        swipe_result(mechanics_none_sid, mechanics_none_bid),
        {"player": {"hero": {}}},
        [],
        mechanics_rpg_request_cfg,
    )
    assert progression_calls == [True]
    assert progression_apply_configs == [mechanics_rpg_request_cfg]
    assert progression_apply_configs[0] is mechanics_rpg_request_cfg

    guard_configs: list[object] = []

    def record_guard(_basis, _state, _story, request_cfg, **_kwargs):
        guard_configs.append(request_cfg)
        return SimpleNamespace(accepted=True, reasons=())

    monkeypatch.setattr(pipeline_module, "guard_narration_story", record_guard)
    guard_raw = json.dumps(
        {"choices": [{"message": {"content": "Guard candidate."}}]}
    ).encode()
    chat_guard_ctx = PostContext(
        mechanics_rpg_sid,
        mechanics_rpg_bid,
        0,
        "new_turn",
        request_cfg=mechanics_chat_request_cfg,
        narration_guard={"schema": "test-guard"},
    )
    rpg_guard_ctx = PostContext(
        mechanics_none_sid,
        mechanics_none_bid,
        0,
        "new_turn",
        request_cfg=mechanics_rpg_request_cfg,
        narration_guard={"schema": "test-guard"},
    )
    mechanics_rpg_pipe.guard_response(
        chat_guard_ctx,
        guard_raw,
        "application/json",
        mechanics_chat_request_cfg,
    )
    mechanics_none_pipe.guard_response(
        rpg_guard_ctx,
        guard_raw,
        "application/json",
        mechanics_rpg_request_cfg,
    )
    assert guard_configs[0] is mechanics_chat_request_cfg
    assert guard_configs[1] is mechanics_rpg_request_cfg

    on_response_guard_configs: list[object] = []

    def record_on_response_guard(_ctx, raw, content_type, request_cfg):
        on_response_guard_configs.append(request_cfg)
        return raw, content_type

    monkeypatch.setattr(
        mechanics_rpg_pipe, "guard_response", record_on_response_guard
    )
    monkeypatch.setattr(
        mechanics_none_pipe, "guard_response", record_on_response_guard
    )
    empty_guard_raw = json.dumps(
        {"choices": [{"message": {"content": ""}}]}
    ).encode()
    mechanics_rpg_pipe.on_response(
        chat_guard_ctx, empty_guard_raw, "application/json"
    )
    mechanics_none_pipe.on_response(
        rpg_guard_ctx, empty_guard_raw, "application/json"
    )
    assert on_response_guard_configs[0] is mechanics_chat_request_cfg
    assert on_response_guard_configs[1] is mechanics_rpg_request_cfg

    fail_open_cfg = Config()
    finalize_experience_profile_base(fail_open_cfg)
    fail_open_store = Store(":memory:")
    fail_open_pipe = Pipeline(
        fail_open_store,
        SessionEngine(fail_open_store, fail_open_cfg.session),
        fail_open_cfg,
        jobs=None,
        rng=__import__("random").Random(31),
        playerlex_service=None,
        player_lessons_service=None,
    )
    fail_open_body = json.dumps(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "Preserve this request exactly."}],
        }
    ).encode()

    def ordinary_processing_failure(_stamp, _body, observed, **_kwargs):
        fail_open_pipe._prewarm_at["preserved"] = 99.0
        fail_open_pipe._prewarm_at["must_rollback"] = 1.0
        fail_open_store.journal(
            observed.branch_id,
            observed.turn_index,
            observed.turn_index,
            [{"op": "world_flag", "key": "must_rollback", "value": True}],
            "user",
        )
        raise RuntimeError("forced ordinary enrichment failure")

    monkeypatch.setattr(
        fail_open_pipe, "_process_observed", ordinary_processing_failure
    )
    fail_open_pipe._prewarm_at = {"preserved": 12.5}
    failed_packet, failed_ctx = fail_open_pipe.process(
        Stamp(session="ordinary-fail-open", mode=CHAT),
        fail_open_body,
    )
    assert failed_packet == fail_open_body
    assert failed_ctx is None
    assert fail_open_store.db.execute(
        "SELECT 1 FROM sessions WHERE external_id='ordinary-fail-open'"
    ).fetchone() is None
    assert fail_open_store.db.execute("SELECT 1 FROM ops_journal").fetchone() is None
    assert fail_open_pipe._prewarm_at == {"preserved": 12.5}

    from aetherstate import genesis as genesis_module

    async def no_stage_b(*_args, **_kwargs):
        return None

    monkeypatch.setattr(genesis_module, "seed_llm", no_stage_b)
    genesis_cfg = Config()
    finalize_experience_profile_base(genesis_cfg)
    genesis_store = Store(":memory:")
    genesis_jobs = RecordingJobs()
    genesis_app = FastAPI()
    genesis_app.include_router(
        make_control_router(genesis_cfg, genesis_store, jobs=genesis_jobs)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=genesis_app),
        base_url="http://local-aetherstate",
    ) as client:
        genesis_response = await client.post(
            "/aether/session/genesis-config/genesis",
            json={
                "mode": CHAT,
                "speaker": "Dane",
                "card_role": "character",
                "card": "Exact Stage-B card.",
                "greeting": "Hello.",
            },
        )
    assert genesis_response.status_code == 200
    assert genesis_response.json()["scheduled"] is True
    assert genesis_jobs.endpoints
    assert genesis_jobs.endpoints[0].specialization.name == "none"

    reset_store = Store(":memory:")
    reset_sid, reset_bid = reset_store.create_session("reset-safety")
    reset_store.experience_mode_set_unlocked(reset_sid, RPG)
    reset_store.journal(
        reset_bid, 0, 0, [{"op": "freeze"}], "user"
    )
    reset_store.checkpoint(reset_bid, 0, {"unrelated": "preserve"})
    with pytest.raises(ExperienceModeLocked):
        reset_store.reset_unlocked_experience(reset_sid, CHAT)
    checkpoint = reset_store.db.execute(
        "SELECT state FROM checkpoints WHERE branch_id=? AND turn_index=0", (reset_bid,)
    ).fetchone()
    assert checkpoint is not None
    assert json.loads(checkpoint["state"]) == {"unrelated": "preserve"}

    checkpoint_store = Store(":memory:")
    checkpoint_sid, checkpoint_bid = checkpoint_store.create_session("checkpoint-only")
    checkpoint_store.experience_mode_set_unlocked(checkpoint_sid, RPG)
    checkpoint_store.checkpoint(checkpoint_bid, 0, {"durable": "continuity"})
    with pytest.raises(ExperienceModeLocked):
        checkpoint_store.reset_unlocked_experience(checkpoint_sid, CHAT)

    branch_store = Store(":memory:")
    branch_sid, branch_bid = branch_store.create_session("other-branch")
    branch_store.experience_mode_set_unlocked(branch_sid, RPG)
    branch_store.fork_branch(branch_bid, 0, -1)
    branch_store.checkpoint(branch_bid, 0, {"other_branch": "durable"})
    with pytest.raises(ExperienceModeLocked):
        branch_store.reset_unlocked_experience(branch_sid, CHAT)
    assert branch_store.db.execute(
        "SELECT state FROM checkpoints WHERE branch_id=?", (branch_bid,)
    ).fetchone() is not None

    from aetherstate import creator, narrator
    from aetherstate.state import apply_delta, current_state

    authored_world_id = "world_" + "a" * 32
    authored_world_raw = {
        "world_id": authored_world_id,
        "name": "Exact Authored World",
        "genre": "fantasy",
    }
    authored_world, _ = creator.portable_seed_documents(
        authored_world_raw, None, cfg
    )
    authored_ops = creator.world_to_ops(authored_world)
    authored_world_source = next(
        op["document"] for op in authored_ops if op.get("op") == "creator_world_seed"
    )
    authored_seed = {"world": authored_world_raw}

    def authored_world_store(external_id: str, provisional_world_id: str):
        local_store = Store(":memory:")
        local_sid, local_bid = local_store.create_session(external_id)
        local_store.experience_mode_set_unlocked(local_sid, RPG)
        result = apply_delta(
            local_store,
            local_sid,
            local_bid,
            0,
            authored_ops,
            "user",
            cfg,
        )
        assert result.submitted_applied == len(authored_ops)
        local_store.persist_creator_seed_receipt(
            session_id=local_sid,
            seed_fingerprint=narrator.seed_fingerprint(authored_seed),
            branch_id=local_bid,
            seed=authored_seed,
            world_source=authored_world_source,
            player_source=None,
            world_requested=True,
            player_requested=False,
            world_id=authored_world_id,
            player_id="",
            admitted_turn=0,
            applied_ops=len(authored_ops),
            migrated=False,
        )
        local_store.journal(
            local_bid,
            0,
            0,
            [{"op": "world_identity_set", "world_id": provisional_world_id}],
            "genesis",
        )
        return local_store, local_sid, local_bid

    compatible_store, compatible_sid, compatible_bid = authored_world_store(
        "compatible-world", authored_world_id
    )
    compatible_store.reset_unlocked_experience(compatible_sid, CHAT)
    compatible_state = current_state(compatible_store, compatible_bid)
    assert compatible_state["world_identity"]["world_id"] == authored_world_id
    assert compatible_store.creator_seed_receipt_for_session(compatible_sid) is not None
    compatible_sources = compatible_store.db.execute(
        "SELECT source FROM ops_journal WHERE branch_id=?", (compatible_bid,)
    ).fetchall()
    assert [row["source"] for row in compatible_sources] == ["user"]

    provisional_store, provisional_sid, provisional_bid = authored_world_store(
        "provisional-player", authored_world_id
    )
    _, provisional_player = creator.portable_seed_documents(
        authored_world_raw,
        {"name": "Provisional Hero"},
        cfg,
    )
    provisional_player_ops = creator.player_to_ops(provisional_player, cfg)

    no_creator_store = Store(":memory:")
    no_creator_sid, no_creator_bid = no_creator_store.create_session(
        "provisional-player-no-creator"
    )
    no_creator_store.experience_mode_set_unlocked(no_creator_sid, RPG)
    no_creator_result = apply_delta(
        no_creator_store,
        no_creator_sid,
        no_creator_bid,
        0,
        provisional_player_ops,
        "genesis",
        cfg,
    )
    assert no_creator_result.submitted_applied == len(provisional_player_ops)
    assert no_creator_store.db.execute(
        "SELECT 1 FROM checkpoints WHERE branch_id=?", (no_creator_bid,)
    ).fetchone() is not None
    no_creator_binding = no_creator_store.reset_unlocked_experience(
        no_creator_sid, CHAT
    )
    assert no_creator_binding.mode == CHAT
    no_creator_state = current_state(no_creator_store, no_creator_bid)
    assert no_creator_state["player"] == {}
    assert no_creator_state["entities"] == {}
    assert no_creator_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=?", (no_creator_bid,)
    ).fetchone() is None
    assert no_creator_store.db.execute(
        "SELECT 1 FROM checkpoints WHERE branch_id=?", (no_creator_bid,)
    ).fetchone() is None
    assert no_creator_store.creator_seed_receipt_for_session(no_creator_sid) is None

    provisional_result = apply_delta(
        provisional_store,
        provisional_sid,
        provisional_bid,
        1,
        provisional_player_ops,
        "genesis",
        cfg,
    )
    assert provisional_result.submitted_applied == len(provisional_player_ops)
    assert current_state(provisional_store, provisional_bid)["player"]
    provisional_store.reset_unlocked_experience(provisional_sid, CHAT)
    provisional_state = current_state(provisional_store, provisional_bid)
    assert provisional_state["world_identity"]["world_id"] == authored_world_id
    assert provisional_state["player"] == {}
    assert [
        row["source"]
        for row in provisional_store.db.execute(
            "SELECT source FROM ops_journal WHERE branch_id=? ORDER BY id",
            (provisional_bid,),
        ).fetchall()
    ] == ["user"]

    nonprovisional_store, nonprovisional_sid, nonprovisional_bid = (
        authored_world_store("nonprovisional-player", authored_world_id)
    )
    nonprovisional_result = apply_delta(
        nonprovisional_store,
        nonprovisional_sid,
        nonprovisional_bid,
        1,
        provisional_player_ops,
        "user",
        cfg,
    )
    assert nonprovisional_result.submitted_applied == len(provisional_player_ops)
    with pytest.raises(ExperienceModeLocked):
        nonprovisional_store.reset_unlocked_experience(
            nonprovisional_sid, CHAT
        )
    assert current_state(nonprovisional_store, nonprovisional_bid)["player"]
    assert nonprovisional_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (nonprovisional_bid,),
    ).fetchone() is not None

    incompatible_store, incompatible_sid, incompatible_bid = authored_world_store(
        "incompatible-world", "world_" + "b" * 32
    )
    with pytest.raises(ExperienceModeLocked):
        incompatible_store.reset_unlocked_experience(incompatible_sid, CHAT)
    assert incompatible_store.creator_seed_receipt_for_session(incompatible_sid) is not None
    assert incompatible_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (incompatible_bid,),
    ).fetchone() is not None

    mutated_store, mutated_sid, mutated_bid = authored_world_store(
        "mutated-world-op", authored_world_id
    )
    mutated_row = mutated_store.db.execute(
        "SELECT id, ops FROM ops_journal WHERE branch_id=? AND source='user'",
        (mutated_bid,),
    ).fetchone()
    mutated_ops = json.loads(mutated_row["ops"])
    mutated_memory = next(op for op in mutated_ops if op.get("op") == "memory_event")
    mutated_memory["text"] += " Mutated after admission."
    mutated_store.db.execute(
        "UPDATE ops_journal SET ops=? WHERE id=?",
        (json.dumps(mutated_ops), mutated_row["id"]),
    )
    with pytest.raises(ExperienceModeLocked):
        mutated_store.reset_unlocked_experience(mutated_sid, CHAT)
    assert mutated_store.creator_seed_receipt_for_session(mutated_sid) is not None
    assert mutated_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (mutated_bid,),
    ).fetchone() is not None

    checkpoint_extra_store, checkpoint_extra_sid, checkpoint_extra_bid = (
        authored_world_store("checkpoint-extra-state", authored_world_id)
    )
    checkpoint_extra_row = checkpoint_extra_store.db.execute(
        "SELECT turn_index, state FROM checkpoints WHERE branch_id=?",
        (checkpoint_extra_bid,),
    ).fetchone()
    checkpoint_extra_state = json.loads(checkpoint_extra_row["state"])
    checkpoint_extra_state["player"] = {
        "unauthorized": {"hp": {"cur": 10, "max": 10}}
    }
    checkpoint_extra_store.db.execute(
        "UPDATE checkpoints SET state=? WHERE branch_id=? AND turn_index=?",
        (
            json.dumps(checkpoint_extra_state),
            checkpoint_extra_bid,
            checkpoint_extra_row["turn_index"],
        ),
    )
    with pytest.raises(ExperienceModeLocked):
        checkpoint_extra_store.reset_unlocked_experience(
            checkpoint_extra_sid, CHAT
        )
    assert checkpoint_extra_store.creator_seed_receipt_for_session(
        checkpoint_extra_sid
    ) is not None
    assert checkpoint_extra_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (checkpoint_extra_bid,),
    ).fetchone() is not None

    def corrupted_metadata_store(
        external_id: str, op_kind: str, metadata_key: str, bool_value: bool
    ):
        local_store, local_sid, local_bid = authored_world_store(
            external_id, authored_world_id
        )
        row = local_store.db.execute(
            "SELECT id, ops FROM ops_journal WHERE branch_id=? AND source='user'",
            (local_bid,),
        ).fetchone()
        ops = json.loads(row["ops"])
        target = next(op for op in ops if op.get("op") == op_kind)
        assert metadata_key in target
        target[metadata_key] = bool_value
        local_store.db.execute(
            "UPDATE ops_journal SET ops=? WHERE id=?",
            (json.dumps(ops), row["id"]),
        )
        return local_store, local_sid, local_bid

    false_turn_store, false_turn_sid, false_turn_bid = corrupted_metadata_store(
        "false-turn-metadata", "world_identity_set", "_turn", False
    )
    with pytest.raises(ExperienceModeLocked):
        false_turn_store.reset_unlocked_experience(false_turn_sid, CHAT)
    assert false_turn_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (false_turn_bid,),
    ).fetchone() is not None

    true_canon_store, true_canon_sid, true_canon_bid = corrupted_metadata_store(
        "true-canon-metadata", "scene_set", "_canon", True
    )
    with pytest.raises(ExperienceModeLocked):
        true_canon_store.reset_unlocked_experience(true_canon_sid, CHAT)
    assert true_canon_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (true_canon_bid,),
    ).fetchone() is not None

    true_turn_mark_store, true_turn_mark_sid, true_turn_mark_bid = (
        corrupted_metadata_store(
            "true-turn-mark-metadata",
            "time_advance",
            "_turn_mark",
            True,
        )
    )
    with pytest.raises(ExperienceModeLocked):
        true_turn_mark_store.reset_unlocked_experience(
            true_turn_mark_sid, CHAT
        )
    assert true_turn_mark_store.db.execute(
        "SELECT 1 FROM ops_journal WHERE branch_id=? AND source='genesis'",
        (true_turn_mark_bid,),
    ).fetchone() is not None

    from aetherstate.jobs import JobRunner

    recovery_cfg = Config()
    recovery_cfg.extraction.mode = "main"
    finalize_experience_profile_base(recovery_cfg)
    recovery_store = Store(":memory:")
    recovery_sid, recovery_bid = recovery_store.create_session("invalid-recovery")
    recovery_store.record_turn(recovery_bid, 0, "new_turn", "normal")
    recovery_store.db.execute(
        "UPDATE turns SET settled=1 WHERE branch_id=? AND turn_index=0",
        (recovery_bid,),
    )
    recovery_store.db.execute(
        "UPDATE sessions SET experience_mode='banana', experience_mode_source='explicit'"
        " WHERE session_id=?",
        (recovery_sid,),
    )
    recovery_jobs = JobRunner(
        recovery_store,
        recovery_cfg,
        SimpleNamespace(get_client=lambda: None),
    )
    monkeypatch.setattr(recovery_jobs, "_ensure_worker", lambda: None)
    assert recovery_jobs.resume_pending() == 0
    assert recovery_jobs.queue.empty()

    chat_cfg = config_for_experience(cfg, CHAT)
    rpg_cfg = config_for_experience(cfg, RPG)
    assert chat_cfg.specialization.name == "none"
    assert chat_cfg.injection.max_tokens == Config().injection.max_tokens
    assert chat_cfg.director.beat_libraries == Config().director.beat_libraries
    assert rpg_cfg.specialization.name == "rpg"
    assert rpg_cfg.injection.max_tokens == 2400
    assert "rpg_adventure" in rpg_cfg.director.beat_libraries
