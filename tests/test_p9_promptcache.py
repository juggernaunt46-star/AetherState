"""Phase 0a — KV-cache / prompt-caching enablement (plan doc 13, RATIFIED 2026-07-09).

Covers: the prompt_cache_key gate (enriched requests ONLY — untouched wires stay
byte-identical), the client-key override, the include_usage probe knob (default off),
usage parsing across provider dialects (SSE + JSON), /aether/status + CacheStats
surfacing, header byte-stability (the injection-audit contract), and the chat-open
prewarm (opt-in, cooldown-limited, correct 1-token non-streaming shape)."""
from __future__ import annotations

import asyncio
import json

from tests.mock_upstream import Reply

SENT = "<<AETHER:v=1;session=chat-cache;turn=3;type=normal;speaker=Dane;user=Bean>>"


def _payload(stream=False):
    return {"model": "m", "stream": stream,
            "messages": [{"role": "system", "content": SENT},
                         {"role": "user", "content": "hello"}]}


# ------------------------------ the key gate -----------------------------------------
async def test_enriched_request_carries_cache_key(client, mock_upstream):
    """A stamped, enriched request gains prompt_cache_key=<internal session id>."""
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    doc = json.loads(mock_upstream.requests[0].body)
    key = doc.get("prompt_cache_key", "")
    assert key.startswith("aether-") and len(key) > len("aether-")


async def test_cache_key_stable_across_turns(client, mock_upstream):
    """Same session -> same key every turn (that IS the routing hint)."""
    for _ in range(2):
        mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    await client.post("/v1/chat/completions", json=_payload())
    k0 = json.loads(mock_upstream.requests[0].body)["prompt_cache_key"]
    k1 = json.loads(mock_upstream.requests[1].body)["prompt_cache_key"]
    assert k0 == k1


async def test_cache_key_knob_off(client, mock_upstream, cfg):
    cfg.upstream.cache_key = False
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    assert "prompt_cache_key" not in json.loads(mock_upstream.requests[0].body)


async def test_untouched_request_never_gains_a_key(client, mock_upstream):
    """An unstamped request the engine leaves alone stays BYTE-identical (transparency)."""
    mock_upstream.enqueue(Reply())
    raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    await client.post("/v1/chat/completions", content=raw,
                      headers={"content-type": "application/json"})
    assert mock_upstream.requests[0].body == raw


async def test_client_cache_key_wins(client, mock_upstream):
    """The frontend's own prompt_cache_key is never overwritten."""
    mock_upstream.enqueue(Reply())
    p = _payload()
    p["prompt_cache_key"] = "mine"
    await client.post("/v1/chat/completions", json=p)
    assert json.loads(mock_upstream.requests[0].body)["prompt_cache_key"] == "mine"


# ------------------------------ the usage probe knob ----------------------------------
async def test_usage_probe_default_off(client, mock_upstream):
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"},
                                sse_chunks=[b"data: [DONE]\n\n"]))
    p = _payload(stream=True)
    async with client.stream("POST", "/v1/chat/completions", json=p) as resp:
        [c async for c in resp.aiter_raw()]
    assert "stream_options" not in json.loads(mock_upstream.requests[0].body)


async def test_usage_probe_knob_on(client, mock_upstream, cfg):
    cfg.upstream.include_usage = True
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"},
                                sse_chunks=[b"data: [DONE]\n\n"]))
    async with client.stream("POST", "/v1/chat/completions",
                             json=_payload(stream=True)) as resp:
        [c async for c in resp.aiter_raw()]
    fwd = json.loads(mock_upstream.requests[0].body)
    assert fwd["stream_options"] == {"include_usage": True}
    # non-streaming requests never gain stream_options
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload(stream=False))
    assert "stream_options" not in json.loads(mock_upstream.requests[1].body)


# ------------------------------ usage parsing + stats ---------------------------------
def test_parse_usage_dialects():
    from aetherstate import promptcache as pc
    u = pc.parse_usage(b'{"usage":{"prompt_tokens":10,"cached_tokens":4}}', "application/json")
    assert u["prompt_tokens"] == 10 and pc.cached_token_count(u) == 4
    u2 = pc.parse_usage(
        b'{"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":6}}}',
        "application/json")
    assert pc.cached_token_count(u2) == 6
    u3 = pc.parse_usage(b'{"usage":{"prompt_tokens":10,"cache_read_input_tokens":7}}',
                        "application/json")
    assert pc.cached_token_count(u3) == 7
    assert pc.parse_usage(b"total garbage", "application/json") is None
    assert pc.parse_usage(b'{"choices":[]}', "application/json") is None
    sse = (b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":100,'
           b'"prompt_tokens_details":{"cached_tokens":90}}}\n\n'
           b"data: [DONE]\n\n")
    u4 = pc.parse_usage(sse, "text/event-stream")
    assert u4["prompt_tokens"] == 100 and pc.cached_token_count(u4) == 90


async def test_status_reports_cache_hits(client, mock_upstream):
    usage_chunk = (b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":1000,'
                   b'"prompt_tokens_details":{"cached_tokens":900}}}\n\n')
    sse = [b'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n\n',
           usage_chunk, b"data: [DONE]\n\n"]
    mock_upstream.enqueue(Reply(headers={"content-type": "text/event-stream"},
                                sse_chunks=sse))
    async with client.stream("POST", "/v1/chat/completions",
                             json=_payload(stream=True)) as resp:
        [c async for c in resp.aiter_raw()]
    await asyncio.sleep(0)                     # the tee's cold path runs post-stream
    c = (await client.get("/aether/status")).json()["cache"]
    assert c["enabled"] is True and c["requests"] >= 1
    assert c["with_usage"] == 1 and c["hits"] == 1
    assert c["prompt_tokens"] == 1000 and c["cached_tokens"] == 900
    assert c["hit_rate_tokens"] == 0.9


# ------------------------------ byte-stability (audit item 2) -------------------------
def test_header_renders_byte_stable_regardless_of_dict_order():
    """Same committed state -> identical briefing bytes, even when entity dicts were
    built in a different insertion order (sorted present-list; deterministic blocks)."""
    from aetherstate import compose
    from aetherstate.config import Config
    cfg = Config()
    ents_a = {"zara": {"name": "Zara", "kind": "character", "present": True},
              "arlo": {"name": "Arlo", "kind": "character", "present": True}}
    ents_b = dict(reversed(list(ents_a.items())))
    base = {"scene": {"location_id": "the_docks"}, "clock": {"day": 2},
            "chars": {}, "meta": {"turn": 4}}
    a = compose.render_header({**base, "entities": ents_a}, cfg)
    b = compose.render_header({**base, "entities": ents_b}, cfg)
    assert a == b and "present: Arlo, Zara" in a


# ------------------------------ prewarm (stretch item 5) ------------------------------
async def test_prewarm_default_off(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    await client.post("/aether/hint", json={"event": "chat_changed",
                                            "session": "chat-cache"})
    await asyncio.sleep(0.05)
    assert len(mock_upstream.requests) == 1    # no prewarm request went upstream


async def test_prewarm_fires_on_chat_changed_with_cooldown(
    client, mock_upstream, cfg, monkeypatch
):
    cfg.upstream.prewarm = True
    mock_upstream.enqueue(Reply())             # the real (enriched) turn
    await client.post("/v1/chat/completions", json=_payload())
    # Simulate a freshly booted host whose monotonic clock is below the cooldown.
    # No prior timestamp means this is still the first eligible prewarm.
    monkeypatch.setattr("aetherstate.promptcache.PREWARM_COOLDOWN_S", 10**18)
    mock_upstream.enqueue(Reply())             # the prewarm's upstream reply
    r = await client.post("/aether/hint", json={"event": "chat_changed",
                                                "session": "chat-cache"})
    assert r.status_code == 200
    for _ in range(100):                       # the prewarm task is async — wait for it
        if len(mock_upstream.requests) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(mock_upstream.requests) == 2
    real = json.loads(mock_upstream.requests[0].body)
    warm = json.loads(mock_upstream.requests[1].body)
    assert warm["max_tokens"] == 1 and warm["stream"] is False
    assert "stream_options" not in warm
    assert warm["messages"] == real["messages"]            # byte-prefix shared
    assert warm["prompt_cache_key"] == real["prompt_cache_key"]
    # cooldown: a second chat_changed inside the window sends NOTHING upstream
    await client.post("/aether/hint", json={"event": "chat_changed",
                                            "session": "chat-cache"})
    await asyncio.sleep(0.1)
    assert len(mock_upstream.requests) == 2
    c = (await client.get("/aether/status")).json()["cache"]
    assert c["prewarms_sent"] == 1 and c["prewarm_fails"] == 0


async def test_prewarm_unknown_session_is_a_noop(client, mock_upstream, cfg):
    cfg.upstream.prewarm = True
    r = await client.post("/aether/hint", json={"event": "chat_changed",
                                                "session": "never-seen"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    await asyncio.sleep(0.05)
    assert len(mock_upstream.requests) == 0
