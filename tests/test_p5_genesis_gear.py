"""Q23 genesis seeding (two-stage) + Q24 gear generalization:
stage-A rules seeds, stage-B LLM full matrix, idempotency, authority, prompt-aware
craving levels, no-card no-op, gear categories, worn/carried exposure split, alias."""
from __future__ import annotations

import asyncio
import json

import httpx

from aetherstate import genesis
from aetherstate.config import Config
from aetherstate.extraction import Endpoint
from aetherstate.state import apply_delta, current_state, validate_op
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply

AKIRA = ("Name: Akira\n"
         "A merciless killer, obsessed with bloodshed. He is addicted to blood and "
         "cannot go without it for long. Wears a long black coat.")


def mk():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    return cfg, store, sid, bid


def doc(system=AKIRA, user="We meet at the docks.", greeting="Akira waits in the rain."):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if greeting:
        msgs.append({"role": "assistant", "content": greeting})
    if user:
        msgs.append({"role": "user", "content": user})
    return {"messages": msgs}


# ------------------------------ stage A: rules ------------------------------
def test_stage_a_seeds_explicit_obsession_and_craving():
    cfg, store, sid, bid = mk()
    n = genesis.seed_rules(store, cfg, sid, bid, doc())
    assert n >= 4                                # entity + presence + obsession + craving
    st = current_state(store, bid)
    akira = st["chars"]["akira"]
    assert any("bloodshed" in k or "blood" in k for k in akira["obsessions"])
    assert "blood" in akira["cravings"]
    assert akira["cravings"]["blood"]["level"] == 45          # neutral opening
    assert store.genesis_state(sid) == "rules"


def test_craving_level_reads_opening_intensity():
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid,
                       doc(user="He is ravenous, days without a kill."))
    lvl = current_state(store, bid)["chars"]["akira"]["cravings"]["blood"]["level"]
    assert lvl == 75
    cfg2, store2, sid2, bid2 = mk()
    genesis.seed_rules(store2, cfg2, sid2, bid2,
                       doc(user="Freshly fed and content, he relaxes."))
    lvl2 = current_state(store2, bid2)["chars"]["akira"]["cravings"]["blood"]["level"]
    assert lvl2 == 15


def test_stage_a_idempotent_and_no_card_skips():
    cfg, store, sid, bid = mk()
    assert genesis.seed_rules(store, cfg, sid, bid, doc()) > 0
    assert genesis.seed_rules(store, cfg, sid, bid, doc()) == 0      # marked, never again
    cfg2, store2, sid2, bid2 = mk()
    assert genesis.seed_rules(store2, cfg2, sid2, bid2, doc(system="")) == 0
    assert store2.genesis_state(sid2) == "skipped"
    # plain card with nothing explicit: entity+presence still seed (3-tier naming —
    # turn-1 header names the character even before Stage B)
    cfg3, store3, sid3, bid3 = mk()
    assert genesis.seed_rules(store3, cfg3, sid3, bid3,
                              doc(system="Name: Mira\nA quiet librarian.")) == 2
    assert store3.genesis_state(sid3) == "rules"


# ------------------------------ stage B: LLM ------------------------------
async def test_stage_b_full_matrix_and_idempotency():
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid, doc())
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ep = Endpoint(base_url="http://mock-upstream/v1", model="tiny")
    ops = [{"op": "entity_add", "name": "Mercer"},
           {"op": "relationship_adj", "from_char": "Akira", "to_char": "Mercer",
            "dimension": "tension", "delta": 25},
           {"op": "reveal_fact", "learner": "Akira", "statement": "Mercer saw the kill",
            "source": "told", "is_secret": True},
           {"op": "clothing", "char": "Akira", "item": "black coat", "action": "don",
            "category": "outerwear", "covers": ["chest", "back", "arms"]},
           {"op": "gear", "char": "Akira", "item": "knife", "action": "don",
            "category": "weapon"},
           {"op": "consent_set", "subject": "Akira", "partner": "Mercer",
            "category": "restraint", "level": "hard_limit"}]
    body = json.dumps({"choices": [{"message": {"content": json.dumps(ops)}}]})
    mock.enqueue(Reply(body=body.encode()))
    card, prompt = genesis.card_and_prompt(doc())
    n = await genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid, card, prompt)
    # +1: the parser auto-creates 'Akira' (referenced but never entity_add'd by the
    # model — the live-repro quarantine fix)
    assert n == len(ops) + 1
    st = current_state(store, bid)
    assert "mercer" in st["entities"]
    assert st["relationships"]["akira->mercer"]["dims"]["tension"] == 25
    assert st["consent"]["akira|mercer|restraint"]["level"] == "hard_limit"
    coat = st["clothing"]["akira"]["black coat"]
    assert coat["category"] == "outerwear" and coat["covers"]
    knife = st["clothing"]["akira"]["knife"]
    assert knife["category"] == "weapon" and knife["covers"] == []   # carried never covers
    assert store.genesis_state(sid) == "done"
    assert await genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid,
                                  card, prompt) == 0                 # idempotent


async def test_stage_b_does_not_seed_a_travel_destination_as_the_current_scene():
    cfg, store, sid, bid = mk()
    opening = doc(
        system="Name: Seraphine\nA witch traveling with a guarded caravan.",
        greeting=("It is night. Seraphine has been traveling to Vael'Cora with the caravan, "
                  "but the opening finds her encamped outside when a Hollow Rift tears open "
                  "beside the camp."),
        user="I head to the part of the camp that needs help the most.",
    )
    genesis.seed_rules(store, cfg, sid, bid, opening)
    proposed = [{"op": "scene_set", "location": "Vael'Cora", "phase": "setup"}]
    assert genesis._parse_ops(json.dumps(proposed)) == proposed

    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ep = Endpoint(base_url="http://mock-upstream/v1", model="tiny")
    mock.enqueue(Reply(body=json.dumps({
        "choices": [{"message": {"content": json.dumps(proposed)}}],
    }).encode()))
    card, prompt = genesis.card_and_prompt(opening)

    assert await genesis.seed_llm(
        store, cfg, lambda: client, ep, sid, bid, card, prompt,
    ) == 0
    state = current_state(store, bid)
    assert not state["scene"].get("location_id")
    sent = json.loads(mock.requests[-1].body)
    system_prompt = next(message["content"] for message in sent["messages"]
                         if message["role"] == "system")
    assert "exact physical place where the opening is happening NOW" in system_prompt
    assert "traveling to X" in system_prompt and "en route to X" in system_prompt


def test_stage_b_keeps_a_destination_when_the_opening_separately_places_cast_there_now():
    ops = [{"op": "scene_set", "location": "Vael'Cora", "phase": "setup"}]
    card = ("World lore names Vael'Cora.\n[Opening message]\n"
            "After traveling to Vael'Cora, Seraphine reaches Vael'Cora and now stands "
            "inside its eastern gate.")

    assert genesis._scene_with_current_location_basis(ops, card, "I survey the gate.") == ops


def test_stage_b_destination_filter_covers_the_prompted_travel_phrases():
    ops = [{"op": "scene_set", "location": "Vael'Cora", "phase": "setup"}]
    for travel in ("traveling to", "heading toward", "en route to"):
        card = ("Name: Seraphine\n[Opening message]\n"
                f"Seraphine is {travel} Vael'Cora, but she is encamped outside when the "
                "opening begins.")
        assert genesis._scene_with_current_location_basis(ops, card, "I remain at camp.") == []


async def test_stage_b_chat_open_and_first_request_race_runs_once():
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid, doc())
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ep = Endpoint(base_url="http://mock-upstream/v1", model="tiny")
    body = json.dumps({"choices": [{"message": {"content":
                      '[{"op":"entity_add","name":"Mercer"}]'}}]}).encode()
    mock.enqueue(Reply(body=body))
    card, prompt = genesis.card_and_prompt(doc())

    results = await asyncio.gather(
        genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid, card, prompt),
        genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid, card, prompt),
    )

    assert sorted(results) == [0, 1]
    assert len(mock.requests) == 1
    assert store.genesis_state(sid) == "done"


async def test_stage_b_garbage_fails_open_and_marks_done():
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid, doc())
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ep = Endpoint(base_url="http://mock-upstream/v1", model="tiny")
    mock.enqueue(Reply(body=json.dumps(
        {"choices": [{"message": {"content": "no json here"}}]}).encode()))
    assert await genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid,
                                  "card text", "") == 0
    assert store.genesis_state(sid) == "done"    # never retry-loops


async def test_stage_b_hard_failure_leaves_session_reseedable():
    """2026-07-06: a transport/endpoint failure (no reply AT ALL) must not lock the
    session 'done' — the marker returns to 'rules' so the next trigger (first message,
    chat re-open, /aether-genesis) retries. Garbage-with-a-reply still marks done."""
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid, doc())
    mock = MockUpstream()                         # unscripted -> 500 on every request
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ep = Endpoint(base_url="http://mock-upstream/v1", model="tiny")
    assert await genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid,
                                  "card text", "") == 0
    assert store.genesis_state(sid) == "rules"    # re-seedable, not locked out


async def test_stage_b_resolves_model_from_assist_config():
    """2026-07-06 chat-open repro: nothing proxied yet -> ep.model=''. seed_llm now
    resolves a real model (the configured assist pick) instead of posting model=''."""
    from aetherstate.config import AssistEndpointConfig
    cfg, store, sid, bid = mk()
    genesis.seed_rules(store, cfg, sid, bid, doc())
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="mech", base_url="http://mock-upstream/v1", model="glm-mech")]
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    ops = [{"op": "entity_add", "name": "Mercer"}]
    mock.enqueue(Reply(body=json.dumps(
        {"choices": [{"message": {"content": json.dumps(ops)}}]}).encode()))
    ep = Endpoint(base_url="http://mock-upstream/v1", model="")   # chat-open: no model yet
    n = await genesis.seed_llm(store, cfg, lambda: client, ep, sid, bid,
                               "card text", "")
    assert n >= 1
    sent = json.loads(mock.requests[-1].body)
    assert sent["model"] == "glm-mech"
    assert store.genesis_state(sid) == "done"


# ------------------------------ authority (Q23.2) ------------------------------
def test_genesis_source_may_set_organics_user_still_gated():
    cfg, store, sid, bid = mk()
    r = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Akira"},
        {"op": "obsession", "char": "Akira", "target_kind": "concept",
         "target": "blood", "set": 70}], "genesis", cfg)
    assert len(r.applied) == 2 and not r.quarantined
    r2 = apply_delta(store, sid, bid, 1, [
        {"op": "obsession", "char": "akira", "target_kind": "concept",
         "target": "blood", "set": 99}], "user", cfg)
    assert r2.quarantined                        # Q11: user organic edit still gated
    r3 = apply_delta(store, sid, bid, 1, [{"op": "unfreeze"}], "genesis", cfg)
    assert r3.quarantined                        # unfreeze stays user-only


# ------------------------------ Q24 gear ------------------------------
def test_gear_alias_and_category_validation():
    v = validate_op({"op": "gear", "char": "a", "item": "rope", "action": "don",
                     "category": "tool"})
    assert v is not None and v["op"] == "clothing"
    assert validate_op({"op": "clothing", "char": "a", "item": "x", "action": "don",
                        "category": "starship"}) is None      # unknown category rejected


def test_carried_gear_never_feeds_exposure():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kira"},
        {"op": "clothing", "char": "Kira", "item": "shirt", "action": "don",
         "category": "top", "covers": ["chest"]},
        {"op": "clothing", "char": "Kira", "item": "medkit", "action": "don",
         "category": "medical", "covers": ["chest"]}], "genesis", cfg)
    st = current_state(store, bid)
    assert st["clothing"]["kira"]["shirt"]["covers"] == ["chest"]
    assert st["clothing"]["kira"]["medkit"]["covers"] == []   # forced empty (02 SS5.2)


# ------------------------------ console + override endpoints (Q11 addendum 2) ---------
async def test_console_served_and_override_toggle(client, cfg):
    r = await client.get("/aether/console")
    assert r.status_code == 200 and "AetherState Console" in r.text
    assert (await client.get("/aether/override")).json() == {"enabled": False}
    r2 = await client.post("/aether/override", json={"enabled": True})
    assert r2.json() == {"enabled": True}
    assert cfg.manual_override.enabled is True
    assert (await client.post("/aether/override", content=b"junk")).status_code == 400


# ------------------------------ P5 endpoints (05 SS5-SS7) ------------------------------
async def test_hint_mode_writeback_groups_endpoints(client, cfg):
    assert (await client.post("/aether/hint", json={"event": "swipe", "session": "x",
                                                    "messageIndex": 3})).json() == {"ok": True}
    # create a session by relaying one exchange
    await client.post("/v1/chat/completions", json={
        "model": "m", "messages": [{"role": "user", "content": "hi"}]})
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    r = await client.post(f"/aether/session/{sid}/mode", json={"mode": "passthrough"})
    assert r.json()["mode"] == "passthrough"
    assert (await client.post(f"/aether/session/{sid}/mode",
                              json={"mode": "??"})).status_code == 422
    wb = (await client.get(f"/aether/session/{sid}/writeback?cursor=0")).json()
    assert wb["world_info"] == [] and wb["authors_note"] is None
    meta = wb["chat_metadata_patch"]["aetherstate"]
    assert meta["session"] == sid and meta["mode"] == "passthrough"
    g = await client.post("/aether/groups", json={"embeddings": "assist", "bogus": "x",
                                                  "linter_nli": "nope"})
    assert g.json()["applied"] == {"embeddings": "assist"}   # +endpoints/persisted fields now
    assert cfg.assist.groups.embeddings == "assist"


def test_passthrough_mode_skips_pipeline():
    from aetherstate.pipeline import Pipeline
    from aetherstate.session_engine import SessionEngine
    cfg = Config()
    store = Store(":memory:")
    engine = SessionEngine(store, cfg.session)
    pipe = Pipeline(store, engine, cfg)
    body = json.dumps({"model": "m", "messages": [
        {"role": "system", "content": AKIRA},
        {"role": "user", "content": "hello"}]}).encode()
    out, ctx = pipe.process(None, body)
    sid = store.db.execute("SELECT session_id FROM sessions").fetchone()["session_id"]
    store.session_mode_set(sid, "passthrough")
    out2, ctx2 = pipe.process(None, json.dumps({"model": "m", "messages": [
        {"role": "system", "content": AKIRA},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "next"}]}).encode())
    assert ctx2 is None                          # byte-exact relay, no tee
    assert json.loads(out2)["messages"][0]["content"] == AKIRA   # untouched


# ------------------------------ P6 live-fix regressions (handoff 2026-07-04) ----------
def test_stage_a_speaker_beats_regex_and_cast_line():
    cfg, store, sid, bid = mk()
    prose = ("Akira is a merciless killer in the neon rain.\n"
             "Characters: Akira, Asuna, Old Mercer\n"
             "He is addicted to blood and obsessed with bloodshed.")
    n = genesis.seed_rules(store, cfg, sid, bid, doc(system=prose), speaker="Akira")
    assert n >= 6                    # akira+presence, asuna, mercer, obsession, craving
    st = current_state(store, bid)
    assert {"akira", "asuna", "old_mercer"} <= set(st["entities"])
    assert st["entities"]["akira"].get("present") is True
    assert not st["entities"]["asuna"].get("present")     # cast tracked, not present


def test_stage_a_prose_card_no_name_line_seeds_via_speaker():
    cfg, store, sid, bid = mk()
    prose = "A merciless killer stalks the docks, obsessed with bloodshed."
    n = genesis.seed_rules(store, cfg, sid, bid, doc(system=prose), speaker="Akira")
    assert n >= 3                                # entity + presence + obsession
    assert "akira" in current_state(store, bid)["entities"]


def test_card_and_prompt_strips_sentinel_system_message():
    d = {"messages": [
        {"role": "system", "content": "<<AETHER:v=1;session=x;speaker=Akira>>"},
        {"role": "system", "content": AKIRA},
        {"role": "assistant", "content": "greeting"},
        {"role": "user", "content": "hi"}]}
    card, _ = genesis.card_and_prompt(d)
    assert "AETHER" not in card and card.startswith("Name: Akira")


def test_parse_ops_coerces_small_model_mistakes():
    raw = """```json
    [{"op":"craving","char":"Akira","substance":"blood","level":80},
     {"op":"obsession","char":"Akira","target":"bloodshed","intensity":70},
     {"op":"gear","char":"Akira","item":"coat","category":"outerwear"}]
    ```"""
    ops = genesis._parse_ops(raw, speaker="Akira")
    kinds = [o["op"] for o in ops]
    assert kinds == ["craving", "obsession", "clothing"]  # coerced + fence-tolerant
    assert ops[0]["delta"] == 80 and ops[0]["action"] == "adjust"
    assert ops[1]["set"] == 70 and ops[1]["target_kind"] == "concept"
    assert ops[2]["action"] == "don"


async def test_turn0_genesis_endpoint_seeds_before_first_message(client):
    r = await client.post("/aether/session/chat-abc/genesis", json={
        "card": AKIRA, "greeting": "Akira waits in the rain.",
        "speaker": "Akira", "user": "Bean", "opening": ""})
    d = r.json()
    assert d["applied"] >= 4 and d["stage"] == "rules"
    st = (await client.get("/aether/session/chat-abc/state")).json()
    assert st["state"]                            # seeded at turn 0, no generation yet
    # idempotent: second open applies nothing
    r2 = await client.post("/aether/session/chat-abc/genesis", json={
        "card": AKIRA, "speaker": "Akira"})
    assert r2.json()["applied"] == 0


async def test_structured_narrator_seed_skips_llm_genesis(client, proxy_app):
    r = await client.post("/aether/session/structured-narrator/genesis", json={
        "card": "THE WORLD — Cinder Gate", "greeting": "An ash-wolf advances.",
        "speaker": "Cinder Gate", "card_role": "narrator", "structured_seed": True,
    })

    body = r.json()
    assert body["structured_seed"] is True
    assert body["scheduled"] is False
    sessions = (await client.get("/aether/sessions")).json()["sessions"]
    row = next(item for item in sessions if item["external_id"] == "structured-narrator")
    assert proxy_app.state.store.genesis_state(row["session_id"]) == "done"


async def test_writeback_unknown_session_is_quiet_200(client):
    r = await client.get("/aether/session/never-seen/writeback?cursor=5")
    assert r.status_code == 200
    assert r.json() == {"cursor": 5, "world_info": [], "authors_note": None,
                        "chat_metadata_patch": {}}
