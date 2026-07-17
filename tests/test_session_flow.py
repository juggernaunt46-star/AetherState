"""P1a integration: stamps through the live proxy relay (03 SS1/SS2.1)."""
from __future__ import annotations

import json

from tests.mock_upstream import Reply

SENT = "<<AETHER:v=1;session=chat-abc;turn=3;type={t};speaker=Dane;user=Bean>>"


def _payload(gen_type="normal"):
    return {"model": "m", "min_p": 0.07,
            "messages": [{"role": "system", "content": SENT.format(t=gen_type)},
                         {"role": "user", "content": "hello"}]}


async def test_sentinel_never_reaches_upstream(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    body = mock_upstream.requests[0].body
    assert b"<<AETHER:" not in body
    doc = json.loads(body)
    assert doc["min_p"] == 0.07                      # unknown fields intact through the strip
    # P2: stamp carries user=Bean -> the Q12 guard note is a legitimate engine system msg;
    # the sentinel-carrier system message itself stays dropped.
    assert [m["role"] for m in doc["messages"] if "[CONTROL]" not in str(m.get("content"))] \
        == ["user"]
    guards = [m for m in doc["messages"] if "[CONTROL]" in str(m.get("content"))]
    assert len(guards) == 1 and "Bean" in guards[0]["content"]


async def test_session_recorded_and_visible_in_status(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload())
    status = (await client.get("/aether/status")).json()
    assert status["sessions"] == 1


async def test_swipe_reuses_turn(client, mock_upstream):
    for _ in range(3):
        mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("normal"))
    await client.post("/v1/chat/completions", json=_payload("swipe"))
    await client.post("/v1/chat/completions", json=_payload("swipe"))
    status = (await client.get("/aether/status")).json()
    assert status["sessions"] == 1                   # same session, no phantom turns


async def test_quiet_not_recorded(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("quiet"))
    status = (await client.get("/aether/status")).json()
    assert status["sessions"] == 0                   # quiet gens never touch state (03 SS2.1)


async def test_unstamped_is_pure_passthrough(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    await client.post("/v1/chat/completions", content=raw,
                      headers={"content-type": "application/json"})
    assert mock_upstream.requests[0].body == raw     # byte-identical even with L3 observing
    assert (await client.get("/aether/status")).json()["sessions"] == 1  # P1b: L3 observes


async def test_malformed_json_with_marker_fails_open_scrubbed(client, mock_upstream):
    mock_upstream.enqueue(Reply())
    raw = b'{"messages": [{"role":"system","content":"<<AETHER:session=x>>"}'  # truncated JSON
    resp = await client.post("/v1/chat/completions", content=raw,
                             headers={"content-type": "application/json"})
    assert resp.status_code == 200                   # fail-open: still relayed
    assert b"<<AETHER:" not in mock_upstream.requests[0].body   # but scrubbed (09 I3)
