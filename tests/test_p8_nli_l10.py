"""L10 ledger-contradiction pass (03 SS9, Bean 2026-07-08): the cold-path NLI check that turns a
prose-vs-ledger contradiction into a next-turn corrective note. It fires ONLY on contradiction
(constraint on fact); NEUTRAL / new detail never fires (freedom of fiction). Default linter_nli
'rules' stays byte-identical (none-leak), and the pass journals no state (replay-pure)."""
from __future__ import annotations

import json

import httpx

from aetherstate import assist, director, linter
from aetherstate.config import AssistEndpointConfig, Config
from aetherstate.state import apply_delta, current_state, reduce_state
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply


def chat_reply(content: str, status: int = 200) -> Reply:
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return Reply(status=status, body=body.encode())


def mk(groups=None):
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    cfg.director.enabled = False              # isolate the L10 corrective from beat notes
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="local", base_url="http://mock-upstream/v1", model="minicheck", tier="small")]
    for k, v in (groups or {}).items():
        setattr(cfg.assist.groups, k, v)
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    return cfg, mock, (lambda: client), store, sid, bid


def seed_fact(store, cfg, sid, bid, statement, turn=1):
    apply_delta(store, sid, bid, turn, [{"op": "fact_admit", "statement": statement,
                "cause": f"creator:test:nli:{turn}", "authority": "creator"}], "user", cfg)


# ------------------------------ premise / hypothesis construction ------------------------------
def test_split_sentences_drops_fragments():
    hyps = linter._split_sentences("Hi. The gate hangs wide open now! ok")
    assert "The gate hangs wide open now" in hyps
    assert "Hi" not in hyps and "ok" not in hyps        # < 12 chars carry no assertable fact


def test_premise_serializer_covers_facts():
    cfg, _mock, _gc, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "fact_admit",
                "statement": "The vault code is 4412",
                "cause": "creator:test:vault-code", "authority": "creator"}], "user", cfg)
    prem = linter._ledger_premises(current_state(store, bid))
    assert any("vault code is 4412" in s for _, s in prem)


# ------------------------------ contradiction -> L10 -> next-turn note --------------------------
async def test_contradiction_fires_l10_and_stages_note():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    apply_delta(store, sid, bid, 1, [{"op": "fact_admit",
                "statement": "The vault code is 4412",
                "cause": "creator:test:vault-code", "authority": "creator"}], "user", cfg)
    state = current_state(store, bid)
    prem = linter._ledger_premises(state)
    idx = next(i for i, (_, s) in enumerate(prem) if "4412" in s)
    mock.enqueue(chat_reply(json.dumps({"contradictions": [
        {"premise": idx, "quote": "the vault has no code", "score": 0.95}]})))
    ep = assist.endpoint_for_group(cfg, "linter_nli")
    vios = await linter.ledger_contradiction_pass(
        store, cfg, get_client, ep, sid, bid, 3, state,
        "She smirked. The vault has no code at all.")
    assert [x.rule for x in vios] == ["L10"]
    assert vios[0].note and not vios[0].advisory       # non-advisory + carries a corrective note
    note = director.stage(store, cfg, sid, bid, 3, state, vios)   # the existing corrective path
    assert "record establishes" in note and store.read_note(sid)


# ------------------------------ freedom pillar: neutral/new detail is silent --------------------
async def test_neutral_new_detail_fires_nothing():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    seed_fact(store, cfg, sid, bid, "Kira carries a silver dagger")
    state = current_state(store, bid)
    mock.enqueue(chat_reply(json.dumps({"contradictions": []})))   # new detail is NOT a contradiction
    vios = await linter.ledger_contradiction_pass(
        store, cfg, get_client, assist.endpoint_for_group(cfg, "linter_nli"),
        sid, bid, 3, state, "Kira also carries a bone whistle on a cord.")
    assert vios == []
    assert director.stage(store, cfg, sid, bid, 3, state, vios) == ""   # no note staged


# ------------------------------ none-leak: default 'rules' is byte-identical --------------------
async def test_default_rules_is_byte_identical_none_leak():
    cfg, mock, get_client, store, sid, bid = mk()      # linter_nli defaults to "rules"
    seed_fact(store, cfg, sid, bid, "The bridge is out")
    assert assist.endpoint_for_group(cfg, "linter_nli") is None    # default routing: no endpoint
    vios = await linter.ledger_contradiction_pass(     # jobs passes None here -> inert
        store, cfg, get_client, None, sid, bid, 3, current_state(store, bid),
        "The bridge stands whole and easily crossed.")
    assert vios == [] and store.read_note(sid) == ""
    assert mock.requests == []                         # nothing was ever sent upstream


async def test_off_switches_short_circuit_before_model():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    seed_fact(store, cfg, sid, bid, "The torch is lit")
    state = current_state(store, bid)
    ep = assist.endpoint_for_group(cfg, "linter_nli")
    cfg.linter.rules_off = ["L10"]                     # documented off-switch
    assert await linter.ledger_contradiction_pass(
        store, cfg, get_client, ep, sid, bid, 3, state, "The torch is dark and cold.") == []
    cfg.linter.rules_off, cfg.linter.enabled = [], False
    assert await linter.ledger_contradiction_pass(
        store, cfg, get_client, ep, sid, bid, 3, state, "The torch is dark and cold.") == []
    assert mock.requests == []                         # gated out BEFORE any model call


# ------------------------------ replay purity: L10 journals no state ----------------------------
async def test_pass_is_replay_pure():
    cfg, mock, get_client, store, sid, bid = mk({"linter_nli": "assist"})
    apply_delta(store, sid, bid, 1, [{"op": "fact_admit",
                "statement": "The gate is locked",
                "cause": "creator:test:locked-gate", "authority": "creator"}], "user", cfg)
    state = current_state(store, bid)
    before = store.state_at(bid, 99, reduce_state)
    prem = linter._ledger_premises(state)
    idx = next(i for i, (_, s) in enumerate(prem) if "gate is locked" in s)
    mock.enqueue(chat_reply(json.dumps({"contradictions": [
        {"premise": idx, "quote": "the gate hangs open", "score": 0.9}]})))
    await linter.ledger_contradiction_pass(
        store, cfg, get_client, assist.endpoint_for_group(cfg, "linter_nli"),
        sid, bid, 3, state, "He strolled through. The gate hangs open.")
    assert store.state_at(bid, 99, reduce_state) == before   # L10 journals no state ops
