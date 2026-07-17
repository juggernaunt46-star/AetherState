"""Proxy quarantine for the internal semantic-plan selector."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from aetherstate.config import Config
from aetherstate.proxy import make_relay_router
from aetherstate.semantic_selection_transport import MAX_SELECTION_RESPONSE_BYTES
from aetherstate.turn_lifecycle import ReplayArtifact
from tests.mock_upstream import MockUpstream, Reply


SELECTOR_REQUEST = (
    b'{"max_tokens":128,"messages":[{"role":"system","content":"IDs only"},'
    b'{"role":"user","content":"{\\"schema\\":\\"narration-plan-request/1\\"}"}],'
    b'"model":"glm-5.2","response_format":{"type":"json_object"},"stream":false,'
    b'"temperature":0}'
)
FALLBACK_PAYLOAD = (
    b'{"id":"fallback","choices":[{"message":{"content":"The guard remains alive."}}]}'
)
ACCEPTED_PAYLOAD = (
    b'{"id":"accepted","choices":[{"message":{"content":"The guard steadies his shield."}}]}'
)


def _replay(
    payload: bytes,
    *,
    status: str,
    attempt_index: int = 0,
    digest_char: str,
    content_type: str = "application/json",
) -> ReplayArtifact:
    return ReplayArtifact(
        lifecycle_key="a" * 64,
        attempt_index=attempt_index,
        status=status,
        content_type=content_type,
        payload=payload,
        payload_hash=hashlib.sha256(payload).hexdigest(),
        envelope={},
        logical_message_id="b" * 64,
        selected_artifact_digest=digest_char * 64,
    )


class _Lifecycle:
    def __init__(self, owner: "_SelectionPipeline") -> None:
        self.owner = owner

    def claim_delivery(
        self,
        lifecycle_key: str,
        attempt_index: int,
        *,
        expected_logical_message_id: str,
        expected_artifact_digest: str,
    ) -> ReplayArtifact:
        self.owner.events.append((
            "claim",
            lifecycle_key,
            attempt_index,
            expected_logical_message_id,
            expected_artifact_digest,
        ))
        replay = self.owner.ctx.semantic_replay
        assert replay is not None
        assert (
            lifecycle_key,
            attempt_index,
            expected_logical_message_id,
            expected_artifact_digest,
        ) == (
            replay.lifecycle_key,
            replay.attempt_index,
            replay.logical_message_id,
            replay.selected_artifact_digest,
        )
        return replay


class _SelectionPipeline:
    def __init__(
        self,
        events: list,
        *,
        terminal: ReplayArtifact,
    ) -> None:
        self.events = events
        self.terminal = terminal
        fallback = _replay(
            FALLBACK_PAYLOAD,
            status="fallback_ready",
            digest_char="c",
            content_type=terminal.content_type,
        )
        self.ctx = SimpleNamespace(
            semantic_gate=True,
            semantic_replay=fallback,
            semantic_selection=SimpleNamespace(sealed=True),
            semantic_error="",
            semantic_status=503,
            local_response=None,
        )
        self.store = SimpleNamespace(turn_lifecycle=_Lifecycle(self))

    def process(self, _stamp, body: bytes):
        self.events.append(("process", body))
        if self.ctx.semantic_selection is None:
            return body, self.ctx
        return SELECTOR_REQUEST, self.ctx

    def complete_semantic_selection(
        self,
        ctx,
        raw: bytes | None,
        content_type: str | None,
        *,
        timed_out: bool,
        upstream_error: bool,
    ) -> ReplayArtifact:
        self.events.append((
            "complete",
            raw,
            content_type,
            timed_out,
            upstream_error,
        ))
        assert ctx is self.ctx
        ctx.semantic_selection = None
        ctx.semantic_replay = self.terminal
        return self.terminal

    def on_response(self, ctx, raw: bytes, content_type: str) -> None:
        self.events.append(("on_response", ctx.semantic_replay, raw, content_type))

    def on_upstream_error(self, _ctx, status: int, raw: bytes) -> None:
        self.events.append(("upstream_error", status, raw))

    def record_response_trace(self, _ctx, **fields) -> None:
        self.events.append(("trace", fields))


class _RecordFirstBody:
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


class _QueryRecorder:
    def __init__(self, app) -> None:
        self.app = app
        self.query_strings: list[bytes] = []

    async def __call__(self, scope, receive, send) -> None:
        self.query_strings.append(bytes(scope.get("query_string", b"")))
        await self.app(scope, receive, send)


def _cfg() -> Config:
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    cfg.specialization.name = "rpg"
    cfg.specialization.semantic_truth_gate = True
    return cfg


def _app(cfg: Config, pipeline: _SelectionPipeline, get_client, events: list):
    app = FastAPI()
    app.include_router(make_relay_router(get_client, cfg, pipeline=pipeline))
    return _RecordFirstBody(app, events)


@pytest.mark.parametrize("selector_wire", ["json", "sse"])
async def test_selection_is_fully_buffered_before_claim_and_only_terminal_bytes_are_visible(
    selector_wire: str,
) -> None:
    events: list = []
    terminal_type = "text/event-stream" if selector_wire == "sse" else "application/json"
    terminal_payload = (
        b'data: {"id":"terminal","choices":[{"delta":{"content":"Safe."}}]}'
        b"\n\ndata: [DONE]\n\n"
        if selector_wire == "sse" else ACCEPTED_PAYLOAD
    )
    terminal = _replay(
        terminal_payload,
        status="accepted",
        digest_char="d",
        content_type=terminal_type,
    )
    pipeline = _SelectionPipeline(events, terminal=terminal)
    selector_secret = b'SELECTOR INTERNAL: {"schema":"narration-plan-selection/1"}'
    mock = MockUpstream()
    if selector_wire == "sse":
        selector_raw = (
            b'data: {"choices":[{"delta":{"content":"{\\"schema\\":"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"\\"narration-plan-selection/1\\"}"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        mock.enqueue(Reply(
            headers={"content-type": "text/event-stream"},
            sse_chunks=[selector_raw[:47], selector_raw[47:]],
        ))
    else:
        selector_raw = selector_secret
        mock.enqueue(Reply(
            headers={"content-type": "application/json; charset=utf-8"},
            body=selector_raw,
        ))
    query_recorder = _QueryRecorder(mock)
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=query_recorder),
        base_url="http://mock-upstream",
    )
    cfg = _cfg()
    original_secret = "PRIVATE TRANSCRIPT: I reveal the hidden route."
    original = (
        '{"model":"glm-5.2","stream":true,"messages":['
        f'{{"role":"user","content":"{original_secret}"}}],'
        '"venice_parameters":{"private_vendor_flag":"do-not-copy"}}'
    ).encode()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(cfg, pipeline, lambda: upstream_client, events)
            ),
            base_url="http://proxy",
        ) as client:
            response = await client.post(
                "/v1/chat/completions?vendor_query=do-not-copy",
                content=original,
                headers={
                    "authorization": "Bearer required-auth",
                    "x-api-key": "required-alt-auth",
                    "x-vendor-secret": "do-not-copy",
                    "anthropic-version": "do-not-copy",
                    "cookie": "private-cookie=do-not-copy",
                },
            )
    finally:
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.content == terminal_payload
    assert selector_raw not in response.content
    assert selector_secret not in response.content
    assert len(mock.requests) == 1
    sent = mock.requests[0]
    assert sent.body == SELECTOR_REQUEST
    assert original_secret.encode() not in sent.body
    assert b"private_vendor_flag" not in sent.body
    assert sent.headers["authorization"] == "Bearer required-auth"
    assert sent.headers["x-api-key"] == "required-alt-auth"
    assert "x-vendor-secret" not in sent.headers
    assert "anthropic-version" not in sent.headers
    assert "cookie" not in sent.headers
    assert sent.headers["content-type"] == "application/json"
    assert query_recorder.query_strings == [b""]

    names = [event[0] for event in events]
    assert names == ["process", "complete", "claim", "first_byte", "on_response", "trace"]
    assert events[1][1:] == (
        selector_raw,
        "text/event-stream" if selector_wire == "sse" else "application/json; charset=utf-8",
        False,
        False,
    )
    assert events[2][2] == terminal.attempt_index
    assert events[3] == ("first_byte", terminal_payload)
    assert events[4][2:] == (terminal_payload, terminal_type)
    assert events[5][1]["content_sha256"] == hashlib.sha256(terminal_payload).hexdigest()
    assert "upstream_error" not in names


@pytest.mark.parametrize("failure", ["timeout", "connect", "http", "oversize", "malformed"])
async def test_every_selector_failure_finalizes_and_delivers_only_the_exact_fallback(
    failure: str,
) -> None:
    events: list = []
    terminal = _replay(
        FALLBACK_PAYLOAD,
        status="fallback_final",
        digest_char="c",
    )
    pipeline = _SelectionPipeline(events, terminal=terminal)
    cfg = _cfg()
    mock = MockUpstream()
    upstream_client = None
    malformed = b"MALFORMED INTERNAL SELECTOR BYTES"
    if failure == "timeout":
        def get_client():
            raise httpx.ReadTimeout("selector timeout")
    elif failure == "connect":
        def get_client():
            raise httpx.ConnectError("selector connect failure")
    else:
        if failure == "http":
            mock.enqueue(Reply(status=503, body=b"PRIVATE SELECTOR HTTP ERROR"))
        elif failure == "oversize":
            mock.enqueue(Reply(body=b"X" * (MAX_SELECTION_RESPONSE_BYTES + 1)))
        else:
            mock.enqueue(Reply(body=malformed))
        upstream_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock),
            base_url="http://mock-upstream",
        )

        def get_client():
            return upstream_client

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(cfg, pipeline, get_client, events)),
            base_url="http://proxy",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"messages":[{"role":"user","content":"private"}]}',
            )
    finally:
        if upstream_client is not None:
            await upstream_client.aclose()

    assert response.status_code == 200
    assert response.content == FALLBACK_PAYLOAD
    complete = next(event for event in events if event[0] == "complete")
    if failure == "malformed":
        assert complete[1:] == (malformed, "application/json", False, False)
    else:
        assert complete[1] is None and complete[2] is None
        assert complete[4] is True
        assert complete[3] is (failure == "timeout")
    names = [event[0] for event in events]
    assert names.index("complete") < names.index("claim") < names.index("first_byte")
    assert "upstream_error" not in names
    assert b"PRIVATE SELECTOR" not in response.content
    assert malformed not in response.content


async def test_terminal_retry_replays_identical_bytes_without_running_selector_again() -> None:
    events: list = []
    terminal = _replay(
        ACCEPTED_PAYLOAD,
        status="accepted",
        digest_char="d",
    )
    pipeline = _SelectionPipeline(events, terminal=terminal)
    mock = MockUpstream()
    selector_raw = b'{"choices":[{"message":{"content":"selection"}}]}'
    mock.enqueue(Reply(body=selector_raw))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock),
        base_url="http://mock-upstream",
    )
    cfg = _cfg()
    app = _app(cfg, pipeline, lambda: upstream_client, events)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            first = await client.post("/v1/chat/completions", content=b'{"messages":[]}')
            second = await client.post("/v1/chat/completions", content=b'{"messages":[]}')
    finally:
        await upstream_client.aclose()

    assert first.content == second.content == ACCEPTED_PAYLOAD
    assert len(mock.requests) == 1
    names = [event[0] for event in events]
    assert names.count("complete") == 1
    assert names.count("claim") == 2
    assert names.count("on_response") == 2
    assert names.count("process") == 2
