"""Exact pre-display delivery for proof-carrying semantic artifacts."""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from aetherstate.config import Config
from aetherstate.proxy import make_relay_router
from aetherstate.turn_lifecycle import ReplayArtifact, TurnReservationConflict
from tests.mock_upstream import MockUpstream, Reply


def _replay(payload: bytes, content_type: str, *, attempt_index: int = 0) -> ReplayArtifact:
    return ReplayArtifact(
        lifecycle_key="a" * 64,
        attempt_index=attempt_index,
        status="fallback_ready",
        content_type=content_type,
        payload=payload,
        payload_hash=hashlib.sha256(payload).hexdigest(),
        envelope={},
        logical_message_id="b" * 64,
        selected_artifact_digest="c" * 64,
    )


class _Lifecycle:
    def __init__(self, replay: ReplayArtifact, events: list, error: Exception | None = None):
        self.replay = replay
        self.events = events
        self.error = error

    def claim_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
    ) -> ReplayArtifact:
        self.events.append((
            "claim",
            lifecycle_key,
            attempt_index,
            expected_logical_message_id,
            expected_artifact_digest,
        ))
        if self.error is not None:
            raise self.error
        return self.replay

    def complete_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
    ) -> ReplayArtifact:
        self.events.append((
            "complete",
            lifecycle_key,
            attempt_index,
            expected_logical_message_id,
            expected_artifact_digest,
        ))
        return self.replay


class _Pipeline:
    def __init__(
        self,
        replay: ReplayArtifact | None,
        events: list,
        *,
        gate: bool = True,
        semantic_error: str = "",
        claim_error: Exception | None = None,
    ) -> None:
        self.events = events
        lifecycle_replay = replay or _replay(b"unused", "application/json")
        self.store = SimpleNamespace(
            turn_lifecycle=_Lifecycle(lifecycle_replay, events, claim_error)
        )
        self.ctx = SimpleNamespace(
            semantic_gate=gate,
            semantic_replay=replay,
            semantic_error=semantic_error,
            local_response=None,
        )

    def process(self, _stamp, body: bytes):
        self.events.append(("process", body))
        return body, self.ctx

    def on_response(self, ctx, raw: bytes, content_type: str) -> None:
        self.events.append(("on_response", ctx.semantic_replay, raw, content_type))

    def on_upstream_error(self, _ctx, status: int, raw: bytes) -> None:
        self.events.append(("upstream_error", status, raw))

    def record_response_trace(self, _ctx, **fields) -> None:
        self.events.append(("trace", fields))


class _RecordFirstBody:
    """Record the first emitted payload without changing ASGI behavior."""

    def __init__(self, app, events: list) -> None:
        self.app = app
        self.events = events
        self.seen = False

    async def __call__(self, scope, receive, send) -> None:
        async def recording_send(message):
            if message["type"] == "http.response.body" and message.get("body") \
                    and not self.seen:
                self.seen = True
                self.events.append(("first_byte", bytes(message["body"])))
            await send(message)

        await self.app(scope, receive, recording_send)


class _AbortSemanticDelivery:
    """Inject cancellation after claim, either before or during the first payload send."""

    def __init__(self, app, events: list, stage: str) -> None:
        self.app = app
        self.events = events
        self.stage = stage

    async def __call__(self, scope, receive, send) -> None:
        async def aborting_send(message):
            if self.stage == "before_body" and message["type"] == "http.response.start":
                self.events.append(("cancel_before_body",))
                raise asyncio.CancelledError()
            if self.stage == "during_body" \
                    and message["type"] == "http.response.body" \
                    and message.get("body"):
                self.events.append(("cancel_during_body", bytes(message["body"])))
                raise asyncio.CancelledError()
            await send(message)

        await self.app(scope, receive, aborting_send)


def _app(cfg: Config, pipeline: _Pipeline, events: list, get_client=None):
    app = FastAPI()

    def forbidden_client():
        events.append(("upstream",))
        raise AssertionError("semantic delivery must not call upstream")

    app.include_router(make_relay_router(get_client or forbidden_client, cfg, pipeline=pipeline))
    return _RecordFirstBody(app, events)


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (
            b'{"id":"proof-json","choices":[{"message":{"content":"Guard alive."}}]}',
            "application/json",
        ),
        (
            b'data: {"id":"proof-sse","choices":[{"delta":{"content":"Guard alive."}}]}'
            b"\n\ndata: [DONE]\n\n",
            "text/event-stream",
        ),
    ],
)
async def test_semantic_artifact_is_claimed_then_relayed_as_exact_wire(payload, content_type):
    events: list = []
    replay = _replay(payload, content_type)
    pipeline = _Pipeline(replay, events)
    cfg = Config()
    cfg.upstream.base_url = "http://must-not-run/v1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(cfg, pipeline, events)),
        base_url="http://proxy",
    ) as client:
        response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == content_type
    names = [event[0] for event in events]
    assert names == [
        "process", "claim", "first_byte", "on_response", "complete", "trace",
    ]
    assert events[1] == (
        "claim",
        replay.lifecycle_key,
        replay.attempt_index,
        replay.logical_message_id,
        replay.selected_artifact_digest,
    )
    assert events[2] == ("first_byte", payload)
    assert events[3][1:] == (replay, payload, content_type)
    assert events[4] == (
        "complete",
        replay.lifecycle_key,
        replay.attempt_index,
        replay.logical_message_id,
        replay.selected_artifact_digest,
    )
    assert events[5][1]["source"] == "semantic"
    assert events[5][1]["content_sha256"] == hashlib.sha256(payload).hexdigest()
    assert "upstream" not in names


@pytest.mark.parametrize("stage", ["before_body", "during_body"])
async def test_cancelled_semantic_emission_never_cold_writes_or_completes(stage):
    events: list = []
    payload = b'{"id":"cancelled","choices":[{"message":{"content":"Guard alive."}}]}'
    replay = _replay(payload, "application/json")
    pipeline = _Pipeline(replay, events)
    cfg = Config()
    app = _AbortSemanticDelivery(_app(cfg, pipeline, events), events, stage)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            await client.post("/v1/chat/completions", content=b'{"messages":[]}')
    except BaseException:
        # ASGI cancellation is intentionally injected below the HTTP client.  The contract under
        # test is the absence of cold/completion authority, not the transport's wrapper exception.
        pass
    await asyncio.sleep(0)

    names = [event[0] for event in events]
    if stage == "before_body":
        assert names == ["process", "claim", "cancel_before_body"]
    else:
        assert names == ["process", "claim", "first_byte", "cancel_during_body"]
    assert "on_response" not in names
    assert "complete" not in names
    assert "trace" not in names


@pytest.mark.parametrize(
    ("claim_error", "expected_status", "expected_type"),
    [
        (
            TurnReservationConflict("stale narration attempt contains private candidate text"),
            409,
            "semantic_delivery_conflict",
        ),
        (RuntimeError("database detail that must not leave the process"), 503,
         "semantic_delivery_unavailable"),
    ],
)
async def test_claim_failure_is_safe_and_never_calls_upstream(
    claim_error, expected_status, expected_type
):
    events: list = []
    secret = b"UNPROVED CANDIDATE: the living guard dies"
    pipeline = _Pipeline(
        _replay(secret, "application/json"),
        events,
        claim_error=claim_error,
    )
    cfg = Config()
    cfg.upstream.base_url = "http://must-not-run/v1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(cfg, pipeline, events)),
        base_url="http://proxy",
    ) as client:
        response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')

    assert response.status_code == expected_status
    assert response.json()["error"]["type"] == expected_type
    assert response.json()["error"]["code"] == expected_status
    assert secret not in response.content
    assert str(claim_error).encode() not in response.content
    assert [event[0] for event in events] == ["process", "claim", "first_byte"]


@pytest.mark.parametrize(
    ("replay", "semantic_error"),
    [(None, ""), (None, "proof transaction rolled back")],
)
async def test_gate_without_terminal_artifact_fails_closed(replay, semantic_error):
    events: list = []
    pipeline = _Pipeline(replay, events, semantic_error=semantic_error)
    cfg = Config()
    cfg.upstream.base_url = "http://must-not-run/v1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(cfg, pipeline, events)),
        base_url="http://proxy",
    ) as client:
        response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "semantic_delivery_unavailable"
    if semantic_error:
        assert semantic_error.encode() not in response.content
    assert [event[0] for event in events] == ["process", "first_byte"]


async def test_non_gated_upstream_response_remains_byte_exact():
    events: list = []
    payload = b"\x00raw upstream bytes\xff\r\n"
    mock = MockUpstream()
    mock.enqueue(Reply(headers={"content-type": "application/octet-stream"}, body=payload))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock),
        base_url="http://mock-upstream",
    )
    pipeline = _Pipeline(None, events, gate=False)
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    app = _app(cfg, pipeline, events, get_client=lambda: upstream_client)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            response = await client.post(
                "/v1/chat/completions?mode=exact",
                content=b'{"messages":[{"role":"user","content":"hello"}]}',
            )
    finally:
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.content == payload
    assert len(mock.requests) == 1
    assert mock.requests[0].body == b'{"messages":[{"role":"user","content":"hello"}]}'
    names = [event[0] for event in events]
    assert names == ["process", "first_byte", "on_response", "trace"]
    assert events[2][2] == payload


async def test_player_lesson_delivery_evidence_failure_never_breaks_upstream_relay(caplog):
    events: list = []
    payload = b'{"error":"provider rejected credentials"}'
    mock = MockUpstream()
    mock.enqueue(Reply(status=401, body=payload))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock),
        base_url="http://mock-upstream",
    )
    pipeline = _Pipeline(None, events, gate=False)

    def fail_delivery(ctx) -> None:
        events.append(("delivery_headers", ctx))
        raise RuntimeError("evidence store unavailable")

    pipeline.mark_player_lessons_delivered = fail_delivery
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    app = _app(cfg, pipeline, events, get_client=lambda: upstream_client)

    try:
        with caplog.at_level("WARNING", logger="aetherstate.proxy"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy",
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    content=b'{"messages":[{"role":"user","content":"hello"}]}',
                )
    finally:
        await upstream_client.aclose()

    assert response.status_code == 401
    assert response.content == payload
    assert any(event[0] == "delivery_headers" for event in events)
    assert "player lesson delivery evidence failed open: RuntimeError" in caplog.text
