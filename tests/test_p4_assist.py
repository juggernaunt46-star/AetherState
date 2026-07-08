"""P4 assist wiring (Q8, 06 C, 03 SS7/SS9): group routing, embeddings retrieval upgrade,
LLM reflection synthesis, advisory NLI pass — every path fail-open to rules mode."""
from __future__ import annotations

import json

import httpx

from aetherstate import assist, memory
from aetherstate.config import AssistEndpointConfig, Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply


def chat_reply(content: str, status: int = 200) -> Reply:
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return Reply(status=status, body=body.encode())


def embed_reply(vectors: list) -> Reply:
    body = json.dumps({"data": [{"index": i, "embedding": v}
                                for i, v in enumerate(vectors)]})
    return Reply(body=body.encode())


def mk(groups=None):
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="local", base_url="http://mock-upstream/v1", model="tiny", tier="small")]
    for k, v in (groups or {}).items():
        setattr(cfg.assist.groups, k, v)
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    return cfg, mock, (lambda: client), store, sid, bid


def seed_memories(store, cfg, sid, bid, texts, turn=1):
    for i, t in enumerate(texts):
        apply_delta(store, sid, bid, turn + i, [{"op": "memory_event", "text": t,
                                                 "importance": 5}], "user", cfg)
        memory.index_applied(store, sid, bid,
                             [{"op": "memory_event", "text": t, "importance": 5,
                               "participants": [], "tags": []}],
                             current_state(store, bid))


# ------------------------------ group routing ------------------------------
def test_endpoint_for_group_routing():
    cfg, *_ = mk()
    assert assist.endpoint_for_group(cfg, "embeddings") is None          # default off
    assert assist.endpoint_for_group(cfg, "linter_nli") is None          # default rules
    cfg.assist.groups.embeddings = "assist"
    ep = assist.endpoint_for_group(cfg, "embeddings")
    assert ep is not None and ep.model == "tiny"
    cfg.assist.groups.embeddings = "main"
    assert assist.endpoint_for_group(cfg, "embeddings", "big").model == "big"
    cfg.assist.endpoints = []
    cfg.assist.groups.embeddings = "assist"
    assert assist.endpoint_for_group(cfg, "embeddings") is None          # warn once, None


def test_endpoint_for_group_per_group_override():
    cfg = Config()
    cfg.upstream.base_url = "http://up/v1"
    cfg.assist.endpoints = [
        AssistEndpointConfig(name="a", base_url="http://a/v1", model="ma", tier="small"),
        AssistEndpointConfig(name="b", base_url="http://b/v1", model="mb", tier="small")]
    cfg.assist.groups.linter_nli = "assist"
    cfg.assist.groups.memory_reflection = "assist"
    # unset -> endpoints[0] (byte-identical to 1.0)
    assert assist.endpoint_for_group(cfg, "linter_nli").base_url == "http://a/v1"
    # per-group override -> the named endpoint; the two groups now hit DIFFERENT endpoints at once
    cfg.assist.group_endpoints.linter_nli = "b"
    assert assist.endpoint_for_group(cfg, "linter_nli").base_url == "http://b/v1"
    assert assist.endpoint_for_group(cfg, "memory_reflection").base_url == "http://a/v1"
    # unknown name falls open to endpoints[0]
    cfg.assist.group_endpoints.linter_nli = "ghost"
    assert assist.endpoint_for_group(cfg, "linter_nli").base_url == "http://a/v1"


# ------------------------------ embeddings (03 SS7) ------------------------------
async def test_embed_missing_and_cosine_retrieval_beats_keyword():
    cfg, mock, get_client, store, sid, bid = mk({"embeddings": "assist"})
    # SAME turn -> equal recency (min-max degeneracy note, test_p4_memory): rel decides
    seed_memories(store, cfg, sid, bid, ["the iron gate needs the red key"], turn=1)
    seed_memories(store, cfg, sid, bid, ["the weather stayed mild"], turn=1)
    ep = assist.endpoint_for_group(cfg, "embeddings")
    # align vectors by TEXT (embeddings_missing orders by recency; don't assume)
    order = store.embeddings_missing(bid, 10)
    mock.enqueue(embed_reply([[1.0, 0.0] if "gate" in r["text"] else [0.0, 1.0]
                              for r in order]))
    assert await assist.embed_missing(store, cfg, get_client, ep, bid) == 2
    assert store.embeddings_missing(bid, 10) == []
    # query shares NO tokens with either memory -> keyword is blind, cosine is not
    mock.enqueue(embed_reply([[0.9, 0.1]]))
    qvec = await assist.embed_query(get_client, cfg, ep, "unlock the entrance")
    rows = memory.retrieve(store, cfg, bid, current_state(store, bid),
                           "unlock the entrance", 10, query_vec=qvec)
    assert rows and rows[0]["text"].startswith("the iron gate")


async def test_embed_failure_fails_open():
    cfg, mock, get_client, store, sid, bid = mk({"embeddings": "assist"})
    seed_memories(store, cfg, sid, bid, ["something happened"])
    ep = assist.endpoint_for_group(cfg, "embeddings")
    mock.enqueue(Reply(status=500, body=b'{"error":"boom"}'))
    assert await assist.embed_missing(store, cfg, get_client, ep, bid) == 0
    # keyword fallback still retrieves
    rows = memory.retrieve(store, cfg, bid, current_state(store, bid),
                           "something happened", 5)
    assert rows


def test_rollback_cleans_orphan_embeddings():
    cfg, mock, get_client, store, sid, bid = mk()
    seed_memories(store, cfg, sid, bid, ["event at turn one"], turn=5)
    rows = store.embeddings_missing(bid, 10)
    store.embeddings_put([(rows[0]["memory_id"], assist._pack([1.0, 2.0]), 2)])
    store.rollback_to(bid, 2)                            # memory (turn 5) rolled back
    assert store.embeddings_get([rows[0]["memory_id"]]) == {}


# ------------------------------ reflection synthesis (06 C) ------------------------------
async def test_synthesize_upgrades_digest_and_adds_semantic_facts():
    cfg, mock, get_client, store, sid, bid = mk({"memory_reflection": "assist"})
    cfg.memory.reflection_every_scenes = 0               # everything is past horizon
    seed_memories(store, cfg, sid, bid, ["Kira paid the innkeeper", "Dane broke the chair"])
    state = current_state(store, bid)
    assert memory.reflect(store, cfg, sid, bid, state) >= 1     # rules digest first
    ep = assist.endpoint_for_group(cfg, "memory_reflection")
    mock.enqueue(chat_reply(json.dumps({
        "summary": "Kira settled the bill while Dane wrecked the furniture.",
        "facts": ["Dane owes the innkeeper for a chair"]})))
    assert await assist.synthesize(store, cfg, get_client, ep, sid, bid) == 1
    rows = store.db.execute("SELECT tier, text, tags FROM memories").fetchall()
    summary = next(r for r in rows if r["tier"] == "summary")
    assert "settled the bill" in summary["text"] and "synthesized" in summary["tags"]
    assert any(r["tier"] == "semantic" and "owes the innkeeper" in r["text"] for r in rows)
    # second run: nothing left to upgrade (idempotent via tag)
    assert await assist.synthesize(store, cfg, get_client, ep, sid, bid) == 0


async def test_synthesize_malformed_reply_leaves_digest():
    cfg, mock, get_client, store, sid, bid = mk({"memory_reflection": "assist"})
    cfg.memory.reflection_every_scenes = 0
    seed_memories(store, cfg, sid, bid, ["Kira paid the innkeeper"])
    memory.reflect(store, cfg, sid, bid, current_state(store, bid))
    before = store.db.execute(
        "SELECT text FROM memories WHERE tier='summary'").fetchone()["text"]
    ep = assist.endpoint_for_group(cfg, "memory_reflection")
    mock.enqueue(chat_reply("I cannot do JSON today, sorry."))
    assert await assist.synthesize(store, cfg, get_client, ep, sid, bid) == 0
    after = store.db.execute(
        "SELECT text FROM memories WHERE tier='summary'").fetchone()["text"]
    assert after == before                               # honest rules product stands


# ---------------------- NLI pass (03 SS9, L10) — pure (premises, hypotheses) -> hits ----------
async def test_nli_pass_returns_contradiction_hits():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    ep = assist.endpoint_for_group(cfg, "linter_nli")
    mock.enqueue(chat_reply(json.dumps({"contradictions": [
        {"premise": 1, "quote": "the vault has no code", "score": 0.95}]})))
    hits = await assist.nli_pass(get_client, cfg, ep,
                                 ["Kira is present in the scene.", "The vault code is 4412."],
                                 ["She laughed", "the vault has no code at all"])
    assert hits == [{"premise": 1, "quote": "the vault has no code", "score": 0.95}]


async def test_nli_pass_ignores_new_detail_and_low_score():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    ep = assist.endpoint_for_group(cfg, "linter_nli")
    mock.enqueue(chat_reply(json.dumps({"contradictions": []})))    # new detail is NOT a contradiction
    assert await assist.nli_pass(get_client, cfg, ep, ["A fact."], ["A new claim."]) == []
    mock.enqueue(chat_reply(json.dumps({"contradictions": [
        {"premise": 0, "quote": "x", "score": 0.2}]})))             # below threshold -> dropped
    assert await assist.nli_pass(get_client, cfg, ep, ["A fact."], ["x"], threshold=0.6) == []


async def test_nli_pass_fails_open():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    ep = assist.endpoint_for_group(cfg, "linter_nli")
    assert await assist.nli_pass(get_client, cfg, ep, [], ["claim"]) == []       # no premises
    assert await assist.nli_pass(get_client, cfg, ep, ["fact"], []) == []        # no hypotheses
    mock.enqueue(chat_reply("{broken json"))
    assert await assist.nli_pass(get_client, cfg, ep, ["fact"], ["claim"]) == []   # garbage reply
