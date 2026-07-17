"""P3a e2e: settle -> tee -> schedule -> extract -> apply, through the live proxy (03 SS1-3/SS5).

Deterministic harness recipe: force_rung=4 (no probe calls competing for the script),
drain() flushes debounce timers, MockUpstream scripts replies in strict request order.
"""
from __future__ import annotations

import json

from tests.mock_upstream import Reply

SENT = "<<AETHER:v=1;session=chat-p3;turn={t};type={ty};speaker=Kira;user=Bean>>"
GOOD_EMPTY = '{"schema":"aetherstate/delta/1","turn_range":[1,1],"ops":[]}'


def sse_reply(text: str) -> Reply:
    chunks = [f'data: {json.dumps({"choices":[{"delta":{"content":text}}]})}\n\n'.encode(),
              b"data: [DONE]\n\n"]
    return Reply(headers={"content-type": "text/event-stream"}, sse_chunks=chunks)


def extract_reply(delta: dict | str) -> Reply:
    content = delta if isinstance(delta, str) else json.dumps(delta)
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return Reply(body=body.encode())


def turn_payload(t: int, history: list[tuple[str, str]], ty: str = "normal") -> dict:
    msgs = [{"role": "system", "content": SENT.format(t=t, ty=ty)}]
    for role, text in history:
        msgs.append({"role": role, "content": text})
    return {"model": "glm-4", "messages": msgs}


async def play_turn(client, mock, t, history, reply_text):
    mock.enqueue(sse_reply(reply_text))
    resp = await client.post("/v1/chat/completions",
                             json=turn_payload(t, history), headers={"accept": "text/event-stream"})
    async for _ in resp.aiter_bytes():
        pass


async def test_settlement_extraction_apply_roundtrip(proxy_app, client, mock_upstream, cfg):
    cfg.upstream.force_rung = 4
    cfg.extraction.debounce_s = 30            # never fires in-test; drain() forces the flush
    sid_ops = [{"op": "entity_add", "name": "Kira"}, {"op": "entity_add", "name": "Dane"}]

    await play_turn(client, mock_upstream, 1, [("user", "hello ((roll d4+1)) there")],
                    "Kira smiles.")
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    await client.patch(f"/aether/session/{sid}/state", json={"ops": sid_ops})

    delta = {"schema": "aetherstate/delta/1", "turn_range": [1, 1], "ops": [
        {"op": "arousal", "char": "Kira", "delta": 15},
        {"op": "relationship_adj", "from_char": "Kira", "to_char": "Dane",
         "dimension": "desire", "delta": 10, "reason": "test"},
        {"op": "mood", "char": "Zed", "valence": 5},              # unknown entity -> quarantine
        {"op": "memory_event", "text": "they met at the tavern", "participants": ["Kira"],
         "importance": 5, "tags": ["meeting"]}]}
    await play_turn(client, mock_upstream, 2,
                    [("user", "hello there"), ("assistant", "Kira smiles."), ("user", "and then")],
                    "Kira laughs.")                                # turn 2 settles turn 1
    mock_upstream.enqueue(extract_reply(delta))                    # Tier-1 fires at drain (FIFO)
    await proxy_app.state.jobs.drain()

    ex_req = json.loads(mock_upstream.requests[-1].body)           # the extraction request
    user_msg = ex_req["messages"][1]["content"]
    assert "Bean: hello there" in user_msg                         # OOC-stripped user text
    assert "Kira: Kira smiles." in user_msg                        # tee-captured assistant text
    assert "Kira" in user_msg and "[USER]" in user_msg             # CHARACTERS line
    assert "((roll" not in user_msg                                # engine syntax never leaks

    now = (await client.get(f"/aether/session/{sid}/state")).json()
    st = now["state"]
    assert st["chars"]["kira"]["arousal"]["arousal"] == 15
    assert st["relationships"]["kira->dane"]["dims"]["desire"] == 10
    assert any(m["text"] == "they met at the tavern" for m in st["memories"])
    assert "zed" not in st["chars"]                                # quarantined, rest applied
    row = proxy_app.state.store.db.execute(
        "SELECT extraction FROM turns WHERE turn_index=1").fetchone()
    assert row["extraction"] == "done"


async def test_failed_ladder_marks_failed_state_stands(proxy_app, client, mock_upstream, cfg):
    cfg.upstream.force_rung = 4
    await play_turn(client, mock_upstream, 1, [("user", "hi")], "reply one")
    await play_turn(client, mock_upstream, 2,
                    [("user", "hi"), ("assistant", "reply one"), ("user", "go on")], "reply two")
    mock_upstream.enqueue(extract_reply("utter garbage"))          # rung 4 call
    mock_upstream.enqueue(extract_reply("more garbage"))           # the ONE repair pass
    await proxy_app.state.jobs.drain()
    row = proxy_app.state.store.db.execute(
        "SELECT extraction FROM turns WHERE turn_index=1").fetchone()
    assert row["extraction"] == "failed"                           # non-fatal (invariant 3)
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    st = (await client.get(f"/aether/session/{sid}/state")).json()["state"]
    assert st["chars"] == {}                                       # previous state stands


async def test_autodisable_after_consecutive_failures(proxy_app, client, mock_upstream, cfg):
    """09 C2: failing batches trip the per-session breaker; no further Tier-1 calls."""
    cfg.upstream.force_rung = 4
    cfg.extraction.fail_autodisable_after = 1
    cfg.extraction.fail_reenable_after_turns = 100
    await play_turn(client, mock_upstream, 1, [("user", "hi")], "r1")
    await play_turn(client, mock_upstream, 2,
                    [("user", "hi"), ("assistant", "r1"), ("user", "u2")], "r2")
    mock_upstream.enqueue(extract_reply("bad"))
    mock_upstream.enqueue(extract_reply("bad"))
    await proxy_app.state.jobs.drain()
    n = len(mock_upstream.requests)
    await play_turn(client, mock_upstream, 3,
                    [("user", "hi"), ("assistant", "r1"), ("user", "u2"),
                     ("assistant", "r2"), ("user", "u3")], "r3")   # settles turn 2
    await proxy_app.state.jobs.drain()
    assert len(mock_upstream.requests) == n + 1                    # ONLY the chat call: no Tier-1


async def test_swipe_rollback_on_extracted_tip(proxy_app, client, mock_upstream, cfg):
    """08 E7: extracted-then-swiped prose retracts and the narration retry stays inert."""
    cfg.extraction.mode = "off"           # the rollback guard is pipeline-side, not Tier-1
    store = proxy_app.state.store
    await play_turn(client, mock_upstream, 1, [("user", "hi")], "r1")
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    await client.patch(f"/aether/session/{sid}/state",
                       json={"ops": [{"op": "entity_add", "name": "Kira"}]})
    branch = (await client.get(f"/aether/session/{sid}/state")).json()["branch_id"]

    await play_turn(client, mock_upstream, 2,
                    [("user", "hi"), ("assistant", "r1"), ("user", "u2")], "r2")
    # force the EARLY-FLUSH shape: pretend turn 2 itself was extracted (not just settled 1)
    store.mark_extraction(branch, 2, 2, "done")
    from aetherstate.state import apply_delta, reduce_state
    apply_delta(store, sid, branch, 2, [{"op": "arousal", "char": "Kira", "delta": 40}],
                "extraction", cfg)
    before = store.state_at(branch, 2**31, reduce_state)
    assert before["chars"]["kira"]["arousal"]["arousal"] == 40

    mock_upstream.enqueue(sse_reply("r2 swiped"))                  # the swipe generation
    resp = await client.post("/v1/chat/completions", json=turn_payload(
        2, [("user", "hi"), ("assistant", "r1"), ("user", "u2")], ty="swipe"))
    async for _ in resp.aiter_bytes():
        pass
    after = store.state_at(branch, 2**31, reduce_state)
    # correct replay: the retracted arousal op was what CREATED the chars entry
    assert after["chars"].get("kira", {}).get("arousal", {}).get("arousal", 0) == 0
    assert "kira" in after["entities"]                             # turn-1 registry survives
    row = store.db.execute("SELECT extraction FROM turns WHERE branch_id=? AND turn_index=2",
                           (branch,)).fetchone()
    assert row["extraction"] == "skipped"              # replacement prose is not re-extracted
