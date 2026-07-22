from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from types import SimpleNamespace

from aetherstate.__main__ import _configure_turn_trace_file
from aetherstate import compose
from aetherstate.config import Config
from aetherstate.pipeline import (
    Pipeline,
    PostContext,
    _diagnostic_narrator_packet,
)
from aetherstate.session_engine import SessionEngine
from aetherstate.state import _trace_op, apply_delta
from aetherstate.store import Store
from aetherstate.turn_trace import TURN_TRACE_SCHEMA, emit_turn_trace


def test_cli_attaches_trace_after_uvicorn_reconfigures_logging(
        tmp_path, monkeypatch) -> None:
    import aetherstate.__main__ as cli
    import aetherstate.app as app_module
    import aetherstate.config as config_module

    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    cfg.server.turn_trace = True
    parent = logging.getLogger("aetherstate")
    pipeline_logger = logging.getLogger("aetherstate.pipeline")
    original_handlers = list(parent.handlers)
    original_level = pipeline_logger.level

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            self.app = app
            self.kwargs = kwargs
            # Match the important part of uvicorn's dictionary reconfiguration:
            # handlers attached before Config construction do not survive it.
            for handler in list(parent.handlers):
                parent.removeHandler(handler)
                handler.close()

    class FakeServer:
        def __init__(self, server_config) -> None:
            self.server_config = server_config

        def run(self) -> None:
            pipeline_logger.setLevel(logging.INFO)
            pipeline_logger.info(
                "TURN_TRACE %s",
                json.dumps({"event": "response", "turn": 2}),
            )
            for handler in parent.handlers:
                handler.flush()

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )
    monkeypatch.setattr(app_module, "create_app", lambda _cfg: object())
    monkeypatch.setattr(config_module, "load_config", lambda _path, **_kwargs: cfg)
    monkeypatch.setattr(sys, "argv", ["aetherstate", "--config", str(tmp_path / "x.toml")])

    try:
        cli.main()
        lines = (tmp_path / "turn-trace.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in lines] == [{"event": "response", "turn": 2}]
    finally:
        pipeline_logger.setLevel(original_level)
        for handler in list(parent.handlers):
            parent.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            parent.addHandler(handler)


def test_turn_trace_file_contains_only_json_payloads(tmp_path) -> None:
    parent = logging.getLogger("aetherstate")
    logger = logging.getLogger("aetherstate.state")
    prior_level = logger.level
    handler = _configure_turn_trace_file(tmp_path)
    try:
        logger.setLevel(logging.INFO)
        logger.info("ordinary diagnostic")
        logger.info("TURN_TRACE %s", json.dumps({"turn": 3, "applied": ["damage"]}))
        handler.flush()
    finally:
        logger.setLevel(prior_level)
        parent.removeHandler(handler)
        handler.close()

    lines = (tmp_path / "turn-trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"turn": 3, "applied": ["damage"]}
    ]


def test_content_free_trace_gate_drops_authored_strings_before_logging(caplog) -> None:
    logger = logging.getLogger("aetherstate.pipeline")

    with caplog.at_level(logging.INFO, logger="aetherstate.pipeline"):
        emitted = emit_turn_trace(logger, {
            "event": "request",
            "unsafe": "Private authored trace prose 7F12",
        })

    assert emitted is False
    assert not any(record.getMessage().startswith("TURN_TRACE ") for record in caplog.records)
    assert "Private authored trace prose 7F12" not in caplog.text
    assert "turn trace dropped unsafe payload" in caplog.text


def test_turn_trace_fsyncs_each_record_before_handler_close(
        tmp_path, monkeypatch) -> None:
    parent = logging.getLogger("aetherstate")
    logger = logging.getLogger("aetherstate.pipeline")
    prior_level = logger.level
    fsynced_fds: list[int] = []
    real_fsync = os.fsync

    def tracked_fsync(fd: int) -> None:
        fsynced_fds.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    handler = _configure_turn_trace_file(tmp_path)
    try:
        logger.setLevel(logging.INFO)
        stream_fd = handler.stream.fileno()
        logger.info("TURN_TRACE %s", json.dumps({"event": "request", "turn": 4}))

        assert fsynced_fds == [stream_fd]
        line = (tmp_path / "turn-trace.jsonl").read_text(encoding="utf-8")
        assert json.loads(line) == {"event": "request", "turn": 4}
    finally:
        logger.setLevel(prior_level)
        parent.removeHandler(handler)
        handler.close()


def test_turn_trace_appends_across_process_sessions(tmp_path) -> None:
    parent = logging.getLogger("aetherstate")
    logger = logging.getLogger("aetherstate.pipeline")
    prior_level = logger.level
    try:
        logger.setLevel(logging.INFO)
        for turn in (1, 2):
            handler = _configure_turn_trace_file(tmp_path)
            try:
                logger.info("TURN_TRACE %s", json.dumps({"event": "request", "turn": turn}))
                handler.flush()
            finally:
                parent.removeHandler(handler)
                handler.close()
    finally:
        logger.setLevel(prior_level)

    lines = (tmp_path / "turn-trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["turn"] for line in lines] == [1, 2]


def test_turn_trace_rotates_instead_of_growing_without_bound(tmp_path) -> None:
    parent = logging.getLogger("aetherstate")
    logger = logging.getLogger("aetherstate.pipeline")
    prior_level = logger.level
    handler = _configure_turn_trace_file(tmp_path, max_bytes=180, backup_count=2)
    try:
        logger.setLevel(logging.INFO)
        for turn in range(8):
            logger.info("TURN_TRACE %s", json.dumps({
                "event": "response", "turn": turn, "padding": "x" * 90,
            }))
        handler.flush()
    finally:
        logger.setLevel(prior_level)
        parent.removeHandler(handler)
        handler.close()

    assert (tmp_path / "turn-trace.jsonl").is_file()
    assert (tmp_path / "turn-trace.jsonl.1").is_file()
    assert not (tmp_path / "turn-trace.jsonl.3").exists()
    assert all(path.stat().st_size <= 180 for path in tmp_path.glob("turn-trace.jsonl*"))


def test_narrator_diagnostic_keeps_content_free_block_receipts() -> None:
    packet = (
        compose.TURN_PACKET_START
        + "\n[DIRECTIVE] Settle exactly one recorded strike.\n"
        + compose.TURN_PACKET_END
    )
    doc = {"messages": [
        {"role": "system", "content": packet},
        {"role": "user", "content": (
            "[AETHER P0]\n"
            "[CURRENT REQUEST DIRECTIVE — attached to the Player's newest message]\n"
            "[DIRECTIVE] The strike is already settled.\n"
            "[AETHER P1]\n"
            "[PLAYER'S NEWEST MESSAGE — respond to this now]\n"
            "private-player-prose-must-not-be-retained"
        )},
    ]}

    evidence = _diagnostic_narrator_packet(doc)
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert [row["kind"] for row in evidence["narrator_blocks"]] == [
        "turn_packet", "current_directive",
    ]
    assert all(set(row) == {
        "message_index", "part_index", "role", "kind", "chars", "sha256",
    } for row in evidence["narrator_blocks"])
    assert all(row["chars"] > 0 and len(row["sha256"]) == 64
               for row in evidence["narrator_blocks"])
    assert "Settle exactly one recorded strike" not in serialized
    assert "The strike is already settled" not in serialized
    assert "private-player-prose-must-not-be-retained" not in serialized


def test_narrator_diagnostic_redacts_a_code_generated_local_response() -> None:
    local_story = "The exact code-generated opening sentence."
    packet = (
        compose.TURN_PACKET_START
        + f"\n[DIRECTIVE] Exact complete enemy prose: {local_story}\n"
        + compose.TURN_PACKET_END
    )

    evidence = _diagnostic_narrator_packet(
        {"messages": [{"role": "system", "content": packet}]},
        redacted_texts=(local_story,),
    )
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert local_story not in serialized
    assert "LOCAL RESPONSE REDACTED" not in serialized
    assert evidence["narrator_redactions"] == [{
        "chars": len(local_story),
        "sha256": hashlib.sha256(local_story.encode("utf-8")).hexdigest(),
    }]


def test_narrator_diagnostic_redacts_rendered_player_lessons_component() -> None:
    private_marker = "PRIVATE_PLAYER_LESSON_CONTENT_91D7B3"
    title = "Private pacing preference"
    do_text = f"Use close sensory detail. {private_marker}"
    avoid_text = "Avoid summarizing the Player's emotional state."
    rendered_lessons = compose.render_player_lessons([{
        "lesson_id": "lesson-private-diagnostic",
        "title": title,
        "do_text": do_text,
        "avoid_text": avoid_text,
    }])
    packet = (
        compose.TURN_PACKET_START
        + "\n[DIRECTIVE] Preserve settled engine truth.\n"
        + rendered_lessons
        + "\n"
        + compose.TURN_PACKET_END
    )

    evidence = _diagnostic_narrator_packet(
        {"messages": [{"role": "system", "content": packet}]},
    )
    serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True)

    leaked = [
        value for value in (title, do_text, avoid_text, private_marker)
        if value in serialized
    ]
    assert leaked == []
    component_sha256 = hashlib.sha256(rendered_lessons.encode("utf-8")).hexdigest()
    assert {
        "chars": len(rendered_lessons),
        "sha256": component_sha256,
    } in evidence["narrator_redactions"]


def test_response_trace_has_content_free_lineage_journal_and_state_manifests(caplog) -> None:
    cfg = Config()
    cfg.server.turn_trace = True
    cfg.upstream.api_key = "sk-private-credential"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="diagnostic-test")
    store.journal(
        branch_id,
        4,
        4,
        [{"op": "scene_set", "location": "Private Vault Prose 91D7B3"}],
        "rule",
    )
    pipeline = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(
        session_id,
        branch_id,
        4,
        "new_turn",
        request_model="diagnostic-model",
    )

    with caplog.at_level(logging.INFO, logger="aetherstate.pipeline"):
        pipeline.record_response_trace(
            ctx,
            source="upstream",
            status=200,
            headers_ms=12.25,
            first_chunk_ms=24.5,
            total_ms=40.75,
            byte_count=123,
            content_sha256="a" * 64,
            content_type="application/json; charset=utf-8",
        )

    message = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("TURN_TRACE ")
    )
    payload = json.loads(message.removeprefix("TURN_TRACE "))
    serialized = json.dumps(payload)

    assert payload["trace_schema"] == TURN_TRACE_SCHEMA
    assert payload["event"] == "response"
    assert payload["session"] == session_id
    assert payload["branch"] == branch_id
    assert payload["lineage"]["branch_id"] == branch_id
    assert payload["journal_manifest"][0]["op_count"] == 1
    assert payload["journal_manifest"][0]["op_types"] == ["scene_set"]
    assert len(payload["journal_manifest"][0]["ops_sha256"]) == 64
    assert payload["state_manifest"]["schema"] == "aetherstate-turn-state-manifest/1"
    assert payload["state_manifest"]["meta_turn"] == 4
    assert "journal" not in payload
    assert "state" not in payload
    assert payload["latency_ms"] == {"headers": 12.25, "first_chunk": 24.5, "total": 40.75}
    assert payload["response"]["narration_guard_replaced"] is False
    assert payload["response"]["narration_guard_reason_codes"] == []
    assert "sk-private-credential" not in serialized
    assert "authorization" not in serialized.lower()
    assert "Private Vault Prose 91D7B3" not in serialized
    assert "Private Vault Prose 91D7B3" not in caplog.text


def test_apply_trace_manifests_free_text_and_nested_payloads(caplog) -> None:
    cfg = Config()
    cfg.server.turn_trace = True
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="apply-trace-private")
    private_name = "Private Character Name 41F0"
    private_reason = "Private authored reason 2A76"
    op = {
        "op": "entity_add",
        "name": private_name,
        "kind": "character",
        "reason": private_reason,
        "_strike": {"story": "Private nested strike prose 6CC9"},
    }

    with caplog.at_level(logging.INFO, logger="aetherstate.state"):
        apply_delta(store, session_id, branch_id, 1, [op], "rule", cfg)

    message = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("TURN_TRACE ")
    )
    payload = json.loads(message.removeprefix("TURN_TRACE "))
    serialized = json.dumps(payload, ensure_ascii=False)
    traced = payload["proposed"][0]

    assert traced == _trace_op(op)
    assert traced["op"] == "entity_add"
    assert traced["name_receipt"]["chars"] == len(private_name)
    assert traced["reason_receipt"]["chars"] == len(private_reason)
    assert len(traced["payload_sha256"]) == 64
    assert traced["field_count"] == len(op)
    assert len(traced["fields_sha256"]) == 64
    assert private_name not in serialized
    assert private_reason not in serialized
    assert "Private nested strike prose 6CC9" not in serialized
    assert private_name not in caplog.text
    assert private_reason not in caplog.text
    assert "Private nested strike prose 6CC9" not in caplog.text


def test_response_trace_records_content_free_narration_guard_replacement(caplog) -> None:
    cfg = Config()
    cfg.server.turn_trace = True
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="guard-receipt-test")
    pipeline = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    rejected_candidate = "The rejected candidate kills Hollowed #1 with the private test phrase."
    ctx = PostContext(
        session_id,
        branch_id,
        1,
        "new_turn",
        request_model="diagnostic-model",
        narration_guard_replaced=True,
        narration_guard_reasons=(
            "unsettled_combatant_impact",
            "unsettled_combatant_defeat",
            rejected_candidate,
        ),
    )

    with caplog.at_level(logging.INFO, logger="aetherstate.pipeline"):
        pipeline.record_response_trace(
            ctx,
            source="upstream_guard",
            status=200,
            headers_ms=10.0,
            first_chunk_ms=20.0,
            total_ms=30.0,
            byte_count=80,
            content_sha256="b" * 64,
            content_type="application/json",
        )

    message = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("TURN_TRACE ")
    )
    payload = json.loads(message.removeprefix("TURN_TRACE "))
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["response"]["narration_guard_replaced"] is True
    assert payload["response"]["narration_guard_reason_codes"] == [
        "unsettled_combatant_impact",
        "unsettled_combatant_defeat",
    ]
    assert rejected_candidate not in serialized
    assert rejected_candidate not in caplog.text


def test_response_trace_preserves_fixed_guard_failures_without_candidate_text(caplog) -> None:
    cfg = Config()
    cfg.server.turn_trace = True
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="guard-reason-projection")
    pipeline = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    private_candidate = "The candidate story contains private narration that must not escape."
    fixed_reasons = (
        "guard_basis_invalid",
        "guard_state_changed",
        "guard_input_invalid",
        "mechanical_authority_unavailable",
        "guard_evaluation_unavailable",
    )
    ctx = PostContext(
        session_id,
        branch_id,
        1,
        "new_turn",
        request_model="diagnostic-model",
        narration_guard_reasons=(*fixed_reasons, private_candidate),
    )

    with caplog.at_level(logging.INFO, logger="aetherstate.pipeline"):
        pipeline.record_response_trace(
            ctx,
            source="upstream",
            status=200,
            headers_ms=10.0,
            first_chunk_ms=20.0,
            total_ms=30.0,
            byte_count=80,
            content_sha256="c" * 64,
            content_type="text/event-stream",
        )

    message = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("TURN_TRACE ")
    )
    payload = json.loads(message.removeprefix("TURN_TRACE "))
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["response"]["narration_guard_reason_codes"] == list(fixed_reasons)
    assert private_candidate not in serialized
    assert private_candidate not in caplog.text
