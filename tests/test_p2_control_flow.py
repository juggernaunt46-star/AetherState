"""P2 fixtures: control API + live proxy flow (07 P2 exit: header injected under cap; freeze e2e)."""
from __future__ import annotations

import json

from tests.mock_upstream import Reply

SENT = "<<AETHER:v=1;session=chat-p2;turn={t};type=normal;speaker=Dane;user=Bean>>"


def payload(turn, *extra_user):
    msgs = [{"role": "system", "content": SENT.format(t=turn)},
            {"role": "user", "content": "we begin"}]
    for i, u in enumerate(extra_user):
        msgs += [{"role": "assistant", "content": f"reply {i}"}, {"role": "user", "content": u}]
    return {"model": "m", "min_p": 0.07, "messages": msgs}


async def test_ooc_set_injects_header_same_turn(client, mock_upstream):
    """Live-smoke shape (07 P2): OOC set -> stripped from bytes, [SCENE] header under cap."""
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions",
                      json=payload(1, "((aether.set scene.location Moonlit Tavern)) We enter."))
    body = mock_upstream.requests[0].body
    doc = json.loads(body)
    assert b"((aether" not in body                            # R1 strip
    assert doc["min_p"] == 0.07                               # unknown fields intact
    header = next(m for m in doc["messages"] if "[SCENE]" in str(m.get("content")))
    assert header["role"] == "system" and "Moonlit Tavern" in header["content"]
    assert "[CONTROL]" in header["content"] and "Bean" in header["content"]
    assert any(m.get("content") == "We enter." for m in doc["messages"])   # history intact


async def test_safeword_freeze_unfreeze_e2e(client, mock_upstream, cfg):
    cfg.consent.safewords = ["red"]
    for _ in range(3):
        mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=payload(1, "hello there"))
    await client.post("/v1/chat/completions", json=payload(2, "hello there", "red. stop."))
    assert b"FROZEN" in mock_upstream.requests[1].body        # same-turn freeze surfaces
    sessions = (await client.get("/aether/sessions")).json()["sessions"]
    assert sessions[0]["frozen"] == 1
    sid = sessions[0]["session_id"]
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert now["frozen"] and now["state"]["frozen_reason"] == "safeword"
    await client.post("/v1/chat/completions",
                      json=payload(3, "hello there", "red. stop.", "((aether.resume)) ok"))
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert not now["frozen"]                                  # unfreeze: explicit user action only


async def test_control_api_freeze_and_patch_authority(client, mock_upstream, cfg):
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=payload(1))
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    r = (await client.post(f"/aether/session/{sid}/freeze")).json()
    assert r["frozen"] is True
    r = (await client.post(f"/aether/session/{sid}/unfreeze")).json()
    assert r["frozen"] is False
    # scene-fact edit: always allowed (02 SS12b)
    r = (await client.patch(f"/aether/session/{sid}/state",
                            json={"path": "scene.location", "value": "Harbor"})).json()
    assert r["applied"] == 1
    # organic edit: gated until manual_override flips (Q11), then applies live
    r = (await client.patch(f"/aether/session/{sid}/state",
                            json={"path": "char.Kira.arousal", "value": "70"})).json()
    assert r["applied"] == 0 and "manual_override" in r["rejected"][0]["reason"]
    cfg.manual_override.enabled = True
    r = (await client.patch(f"/aether/session/{sid}/state",
                            json={"path": "char.Kira.arousal", "value": "70"})).json()
    assert r["applied"] == 1
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert now["state"]["scene"]["location_id"] == "Harbor"
    assert now["state"]["chars"]["kira"]["arousal"]["arousal"] == 70
    # unknown path: rejected visibly, never silently (02 SS12b)
    resp = await client.patch(f"/aether/session/{sid}/state",
                              json={"path": "nope.nope", "value": "1"})
    assert resp.status_code == 422


async def test_unknown_session_404(client):
    assert (await client.get("/aether/session/ghost/state")).status_code == 404
