"""Phase-0 exit criteria (07 P0): byte-transparency + fault relay (09 I/U rows) + determinism."""
from __future__ import annotations

import json

from tests.mock_upstream import Reply

SSE = [
    b': keep-alive ping\n\n',
    b'data: {"id":"c1","choices":[{"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"id":"c1","choices":[{"delta":{"content":"Hel"}}]}\n\n',
    b'data: {"id":"c1","choices":[{"delta":{"content":"lo"}}]}\n\n',
    b'data: [DONE]\n\n',
]


async def test_sse_relay_byte_identical(client, mock_upstream):
    """Invariant 2: the token stream is forwarded byte-for-byte, [DONE] intact."""
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"}, sse_chunks=SSE))
    async with client.stream("POST", "/v1/chat/completions",
                             json={"model": "m", "stream": True, "messages": []}) as resp:
        got = b"".join([chunk async for chunk in resp.aiter_raw()])
    assert resp.status_code == 200
    assert got == b"".join(SSE)                      # golden bytes, comments and all
    assert resp.headers["content-type"] == "text/event-stream"


async def test_unknown_fields_reach_upstream_verbatim(client, mock_upstream):
    """Invariant 1: unknown body fields (min_p, DRY, ...) pass through untouched — raw bytes equal."""
    mock_upstream.enqueue(Reply(body=b'{"ok":true}'))
    body = {"model": "m", "messages": [], "min_p": 0.05, "dry_multiplier": 0.8,
            "custom_thing": {"nested": [1, 2, 3]}}
    raw = json.dumps(body).encode()
    resp = await client.post("/v1/chat/completions", content=raw,
                             headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert mock_upstream.requests[0].body == raw     # byte-for-byte: proxy never parsed it


async def test_upstream_errors_relay_verbatim(client, mock_upstream):
    """09 U2/U3: 4xx/5xx status AND body forwarded exactly."""
    for status, body in [(429, b'{"error":{"message":"rate limited","type":"rate_limit"}}'),
                         (500, b'{"error":{"message":"boom"}}')]:
        mock_upstream.enqueue(Reply(status=status, body=body))
        resp = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert resp.status_code == status
        assert resp.content == body


async def test_malformed_sse_still_relays(client, mock_upstream):
    """09 U7: garbage bytes relay verbatim — parseability never gates the stream."""
    garbage = [b'data: {"broken json\n\n', b'\xff\xfenot sse at all', b'data: [DONE]\n\n']
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"}, sse_chunks=garbage))
    async with client.stream("POST", "/v1/chat/completions", json={"stream": True}) as resp:
        got = b"".join([chunk async for chunk in resp.aiter_raw()])
    assert got == b"".join(garbage)


async def test_midstream_cut_relays_prefix(client, mock_upstream):
    """09 U4: upstream dies mid-stream -> client gets exactly what arrived, no synthetic bytes."""
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"},
                                sse_chunks=SSE[:3], fault="midstream_cut"))
    async with client.stream("POST", "/v1/chat/completions", json={"stream": True}) as resp:
        got = b"".join([chunk async for chunk in resp.aiter_raw()])
    assert got == b"".join(SSE[:3])
    assert b"[DONE]" not in got


async def test_models_endpoint_relays(client, mock_upstream):
    mock_upstream.enqueue(Reply(body=b'{"object":"list","data":[{"id":"glm-4.6"}]}'))
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert b"glm-4.6" in resp.content
    assert mock_upstream.requests[0].path == "/v1/models"   # /v1 from BASE, not from caller


async def test_aetherstate_headers_never_forwarded(client, mock_upstream):
    """05/06: x-aetherstate-* is consumed by the proxy, invisible upstream."""
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json={"messages": []},
                      headers={"x-aetherstate-session": "chat-abc", "x-custom-user": "keep-me"})
    seen = mock_upstream.requests[0].headers
    assert "x-aetherstate-session" not in seen
    assert seen.get("x-custom-user") == "keep-me"    # OTHER custom headers still pass (transparency)


async def test_api_key_injected_when_client_sends_none(client, mock_upstream, cfg):
    cfg.upstream.api_key = "sk-test"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json={})
    assert mock_upstream.requests[0].headers.get("authorization") == "Bearer sk-test"


async def test_client_auth_wins_over_config_key(client, mock_upstream, cfg):
    cfg.upstream.api_key = "sk-config"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json={}, headers={"authorization": "Bearer sk-client"})
    assert mock_upstream.requests[0].headers.get("authorization") == "Bearer sk-client"


async def test_status_endpoint_is_separate_surface(client):
    """10 SS5 / 09 F3: control plane responds even with an unscripted (dead) upstream."""
    resp = await client.get("/aether/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "enriched"           # P2: Tier-0 + composition active
    assert data["telemetry"] == "none, ever"


async def test_determinism_repeat(client, mock_upstream):
    """11 SS3: same scripted exchange twice -> byte-identical results."""
    results = []
    for _ in range(2):
        mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"}, sse_chunks=SSE))
        async with client.stream("POST", "/v1/chat/completions", json={"stream": True}) as resp:
            results.append(b"".join([chunk async for chunk in resp.aiter_raw()]))
    assert results[0] == results[1]


async def test_upstream_asked_for_identity_encoding(client, mock_upstream):
    """A teeing proxy must receive readable bytes: we force accept-encoding: identity upstream."""
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json={})
    assert mock_upstream.requests[0].headers.get("accept-encoding") == "identity"


async def test_compressed_response_relays_with_its_header(client, mock_upstream):
    """If upstream compresses anyway, bytes AND content-encoding header travel together (verbatim)."""
    import gzip
    from tests.mock_upstream import Reply
    payload = gzip.compress(b'{"object":"list","data":[{"id":"glm-4.6"}]}')
    mock_upstream.enqueue(Reply(headers={"content-type": "application/json",
                                         "content-encoding": "gzip"}, body=payload))
    async with client.stream("GET", "/v1/models") as resp:
        raw = b"".join([c async for c in resp.aiter_raw()])
    assert resp.headers.get("content-encoding") == "gzip"
    assert raw == payload                     # compressed bytes untouched, header intact
