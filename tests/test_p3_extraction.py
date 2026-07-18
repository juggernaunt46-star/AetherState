"""P3a fixtures: parse/repair, probe protocol, ladder walk-down + demotion (03 SS5, 06 A.2)."""
from __future__ import annotations

import json

import httpx
import pytest

from aetherstate.config import Config
from aetherstate.extraction import (Endpoint, Ladder, delta_json_schema,
                                    filter_realization_owned_ops,
                                    filter_unearned_social_ops,
                                    filter_unrealized_memory_ops, parse_and_validate, repair_json)
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply

GOOD = '{"schema":"aetherstate/delta/1","turn_range":[1,1],"ops":[]}'


def test_routine_professional_cooperation_cannot_mint_social_standing():
    narration = (
        "The Warden's eyes settle on you -- not with warmth, not with hostility. "
        '"Verbatim, as you asked. I will not paraphrase." She reads the admitted record. '
        'Orla says, "I will give it to you the way I give evidence at tribunal. Nothing added."'
    )
    ops = [
        {"op": "affinity_adj", "target": "Warden Ivara", "delta": 3,
         "reason": "engaged professionally and read the Council record verbatim"},
        {"op": "affinity_adj", "target": "Orla", "delta": 2,
         "reason": "gave tribunal-grade firsthand testimony without embellishment"},
        {"op": "belief_acquire", "holder": "Vale", "statement": "the record is scoped",
         "stance": "believes", "evidence_source": "told", "teller": "Warden Ivara"},
    ]

    kept, rejected = filter_unearned_social_ops(ops, narration)

    assert kept == [ops[2]]
    assert rejected == ops[:2]


def test_factual_record_correction_cannot_mint_social_standing():
    narration = (
        "Voss accepts the factual correction and amends his draft. "
        "Ivara is not grateful or warm; she remains assessing."
    )
    ops = [
        {"op": "affinity_adj", "target": "Halren Voss", "delta": 2,
         "reason": "Accepted factual correction without dispute and amended his own draft "
                   "accurately"},
        {"op": "affinity_adj", "target": "Warden Ivara", "delta": 1,
         "reason": "Acknowledged her dating error and corrected the record"},
    ]

    kept, rejected = filter_unearned_social_ops(ops, narration)

    assert kept == []
    assert rejected == ops


def test_explicit_narrated_relational_shift_preserves_social_proposal():
    narration = (
        'Ivara lowers the record. "You handled this carefully. You have earned my respect, Vale."'
    )
    op = {"op": "affinity_adj", "target": "Warden Ivara", "delta": 3,
          "reason": "careful handling of the admitted record earned her respect"}

    kept, rejected = filter_unearned_social_ops([op], narration)

    assert kept == [op]
    assert rejected == []


def test_social_guard_is_narrow_to_routine_cooperation_reasons():
    op = {"op": "relationship_adj", "from_char": "Mara", "to_char": "Vess",
          "dimension": "trust", "delta": -25, "reason": "admitted year-long deception"}

    kept, rejected = filter_unearned_social_ops([op], "Mara reels from Vess's betrayal.")

    assert kept == [op]
    assert rejected == []


def test_incipent_handoff_cannot_become_completed_memory():
    narration = (
        "Ivara extends the sealed order toward Dennic. "
        "Dennic steps forward to receive it."
    )
    op = {
        "op": "memory_event",
        "text": "The sealed order was handed to Dennic Auer for direct delivery to Voss",
        "participants": ["Ivara", "Dennic Auer"],
        "importance": 5,
        "tags": ["order_dispatched"],
    }

    kept, rejected = filter_unrealized_memory_ops([op], narration)

    assert kept == []
    assert rejected == [op]


def test_realized_handoff_preserves_completed_memory():
    narration = (
        "Ivara extends the sealed order toward Dennic. "
        "Dennic steps forward to receive it. Ivara places the order in his hands. "
        "Dennic accepts the order and closes his dispatch case."
    )
    op = {
        "op": "memory_event",
        "text": "The sealed order was handed to Dennic Auer for direct delivery to Voss",
    }

    kept, rejected = filter_unrealized_memory_ops([op], narration)

    assert kept == [op]
    assert rejected == []


def test_unrealized_handoff_guard_does_not_touch_unrelated_memory():
    narration = "Dennic steps forward to receive the sealed order."
    op = {"op": "memory_event", "text": "Morning watch began at the Public Cistern."}

    kept, rejected = filter_unrealized_memory_ops([op], narration)

    assert kept == [op]
    assert rejected == []


def test_current_realization_fences_deferred_world_changes_but_keeps_actor_belief():
    realization = {
        "turn": 9,
        "forbidden_inference": [{
            "scope_ref": "turn:9",
            "code": "only_realized_changes_may_be_world_changes",
        }],
    }
    ops = [
        {"op": "item_gain", "char": "Vale", "item": "invented testimony scrap"},
        {"op": "hp_adj", "char": "Vale", "delta": -3},
        {"op": "memory_event", "text": "Vale found an invented testimony scrap."},
        {
            "op": "belief_acquire",
            "holder": "Vale",
            "statement": "the gate is shut",
            "stance": "believes",
            "evidence_source": "told",
            "teller": "Ivara",
        },
    ]

    kept, rejected = filter_realization_owned_ops(ops, realization, turn=9)

    assert kept == [ops[3]]
    assert rejected == ops[:3]


@pytest.mark.parametrize("realization,turn", [
    (None, 9),
    ({"turn": 8, "forbidden_inference": [{
        "scope_ref": "turn:8",
        "code": "only_realized_changes_may_be_world_changes",
    }]}, 9),
    ({"turn": 9, "forbidden_inference": []}, 9),
])
def test_missing_stale_or_incomplete_realization_does_not_fence_extraction(
        realization, turn):
    ops = [{"op": "memory_event", "text": "A witnessed event."}]

    kept, rejected = filter_realization_owned_ops(ops, realization, turn=turn)

    assert kept == ops
    assert rejected == []


def chat_reply(content: str, status: int = 200) -> Reply:
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return Reply(status=status, body=body.encode())


@pytest.fixture()
def harness():
    mock = MockUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock),
                               base_url="http://mock-upstream")
    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    cfg.extraction.use_anyof = False    # scripted-call-count tests target rungs/ladder/card;
    store = Store(":memory:")           # the anyof probe is exercised in test_p4_anyof.py
    return mock, Ladder(store, cfg, lambda: client), cfg, store


EP = Endpoint(base_url="http://mock-upstream/v1", model="glm-test")


# ------------------------------ parse & repair (03 SS5) ------------------------------
def test_parse_accepts_fences_prose_and_shot_c_empty_ops():
    assert parse_and_validate(GOOD).ops == []                       # empty ops = valid (Shot C)
    assert parse_and_validate(f"Sure! Here:\n```json\n{GOOD}\n```").ops == []
    assert parse_and_validate("no json here at all") is None
    assert parse_and_validate("") is None


def test_repair_fixes_rung4_classics():
    trunc = '{"schema":"aetherstate/delta/1","turn_range":[3,3],"ops":[{"op":"arousal","char":"Kira","delta":5'
    d = parse_and_validate(trunc)
    assert d and d.ops == [{"op": "arousal", "char": "Kira", "delta": 5}]
    single = "{'schema':'aetherstate/delta/1','turn_range':[1,1],'ops':[]}"
    assert parse_and_validate(single) is not None
    unquoted = '{schema:"aetherstate/delta/1",turn_range:[1,1],ops:[{op:"time_advance",minutes:60}]}'
    d = parse_and_validate(unquoted)
    assert d and d.ops[0]["op"] == "time_advance"
    trailing = '{"schema":"aetherstate/delta/1","turn_range":[1,1],"ops":[],}'
    assert parse_and_validate(trailing) is not None
    assert repair_json('{"a":[1,2') == '{"a":[1,2]}'


def test_rung2_null_padding_stripped():
    """Venice strict schemas pad every field with null (06 A.4) — ops come back clean."""
    padded = json.dumps({"schema": "aetherstate/delta/1", "turn_range": [2, 2], "ops": [
        {"op": "arousal", "char": "Kira", "delta": 10, "set": None, "entity": None,
         "item": None, "tags": None}]})
    d = parse_and_validate(padded)
    assert d.ops == [{"op": "arousal", "char": "Kira", "delta": 10}]


def test_delta_schema_is_venice_strict():
    s = delta_json_schema()
    op_schema = s["schema"]["properties"]["ops"]["items"]
    assert s["strict"] is True
    assert op_schema["additionalProperties"] is False
    assert set(op_schema["required"]) == set(op_schema["properties"])   # ALL fields required
    assert "null" in op_schema["properties"]["delta"]["type"]           # nullable via type array


# ------------------------------ probe protocol (06 A.2) ------------------------------
async def test_probe_ladder_p2_p3_floor(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(chat_reply('{"ok": true}'))                        # P2 succeeds
    assert await ladder.rung_for(EP) == 2
    assert json.loads(mock.requests[0].body)["response_format"]["type"] == "json_schema"

    store.db.execute("DELETE FROM caps")
    store.db.commit()
    mock.script.clear()
    mock.requests.clear()
    mock.enqueue(Reply(status=400, body=b'{"error":"no schema support"}'))   # P2 fails
    mock.enqueue(chat_reply('{"ok": true}'))                        # P3 succeeds
    assert await ladder.rung_for(EP) == 3

    store.db.execute("DELETE FROM caps")
    store.db.commit()
    mock.script.clear()
    mock.enqueue(Reply(status=400, body=b"{}"))
    mock.enqueue(chat_reply("I'm sorry, I can only reply in prose."))
    assert await ladder.rung_for(EP) == 4                           # floor: always available


async def test_probe_cached_and_force_rung_wins(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(chat_reply('{"ok": true}'))
    assert await ladder.rung_for(EP) == 2
    n = len(mock.requests)
    assert await ladder.rung_for(EP) == 2                           # cache hit: no new calls
    assert len(mock.requests) == n
    cfg.upstream.force_rung = 4
    assert await ladder.rung_for(EP) == 4                           # override, probe skipped
    assert len(mock.requests) == n


# ------------------------------ the ladder (03 SS5) ------------------------------
async def test_ladder_walks_down_with_one_repair_per_rung(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)                        # seed rung 2, skip probe
    mock.enqueue(chat_reply("NOT JSON"))                            # rung 2 call
    mock.enqueue(chat_reply("STILL NOT JSON"))                      # rung 2 repair (the ONE pass)
    mock.enqueue(chat_reply(GOOD))                                  # rung 3 call succeeds
    delta = await ladder.extract(EP, "(nothing)", "Kira", 1, 1, "Kira: hi")
    assert delta is not None and delta.ops == []
    assert len(mock.requests) == 3
    bodies = [json.loads(r.body) for r in mock.requests]
    assert bodies[0]["response_format"]["type"] == "json_schema"    # rung 2
    assert "could not be parsed" in bodies[1]["messages"][1]["content"]   # repair prompt
    assert bodies[2]["response_format"]["type"] == "json_object"    # rung 3


async def test_three_strikes_demote_seed_rung(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)
    for _ in range(3):                                              # 3 failing batches at seed
        mock.enqueue(chat_reply("garbage"))                         # rung 2
        mock.enqueue(chat_reply("garbage"))                         # rung 2 repair
        mock.enqueue(chat_reply(GOOD))                              # rung 3 rescues the batch
        assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    assert store.caps_get(EP.base_url, EP.model)["rung"] == 3       # demoted (06 A.2)


async def test_total_failure_returns_none_nonfatal(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 4)                        # floor only: call + repair
    mock.enqueue(chat_reply("nope"))
    mock.enqueue(chat_reply("still nope"))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is None  # previous state stands


async def test_prompt_carries_data_fences_and_op_card(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 4)
    mock.enqueue(chat_reply(GOOD))
    await ladder.extract(EP, "[SCENE] tavern", "Kira; Bean [USER]", 3, 4,
                         "Bean: hello\nKira: hi there")
    body = json.loads(mock.requests[0].body)
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "OP CARD" in system and "state logger" in system         # rung 4: card in prompt
    assert "<data>\nBean: hello\nKira: hi there\n</data>" in user   # prose fenced (untrusted)
    assert "TURN_RANGE: [3,4]" in user
    assert '"ops":[]' in user                                       # Shot C ships in every call
    assert body["temperature"] == 0


# ------------------------------ rung 1 native (06 A.2 P1, A.3 — P3b) ------------------------------
LOCAL_EP = Endpoint(base_url="http://127.0.0.1:8080/v1", model="local-test")

MODELS_LLAMACPP = Reply(body=json.dumps(
    {"object": "list", "data": [{"id": "m", "owned_by": "llamacpp"}]}).encode())
MODELS_OLLAMA = Reply(body=json.dumps(
    {"object": "list", "data": [{"id": "m", "owned_by": "ollama"}]}).encode())


async def test_hosted_and_unknown_hosts_never_fingerprinted(harness):
    """P1 must never fire blind at non-local endpoints — not even the free GET /models."""
    mock, ladder, cfg, store = harness
    mock.enqueue(Reply(status=400, body=b"{}"))                     # P2 fails
    mock.enqueue(chat_reply("prose only"))                          # P3 fails
    assert await ladder.rung_for(EP) == 4
    assert all(r.method == "POST" and r.path.endswith("/chat/completions")
               for r in mock.requests)                              # no GET /models, no native field


async def test_rung1_probe_fingerprinted_llamacpp(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(MODELS_LLAMACPP)                                   # fingerprint GET
    mock.enqueue(chat_reply('{"ok": true}'))                        # P1 native probe
    assert await ladder.rung_for(LOCAL_EP) == 1
    assert mock.requests[0].method == "GET"
    probe = json.loads(mock.requests[1].body)
    assert "json_schema" in probe and "response_format" not in probe   # llama.cpp dialect
    row = store.caps_get(LOCAL_EP.base_url, LOCAL_EP.model)
    assert row["rung"] == 1 and row["native"] == "llamacpp"


async def test_ollama_local_gets_no_native_field(harness):
    """06 A.3: ollama /v1 has no native grammar field — P1 skipped, P2 probes next."""
    mock, ladder, cfg, store = harness
    mock.enqueue(MODELS_OLLAMA)
    mock.enqueue(chat_reply('{"ok": true}'))                        # P2 succeeds
    assert await ladder.rung_for(LOCAL_EP) == 2
    gen = json.loads(mock.requests[1].body)
    assert gen["response_format"]["type"] == "json_schema"
    assert "grammar" not in gen and "guided_json" not in gen and "json_schema" not in gen.keys() - {"response_format"}


async def test_rung1_extraction_body_gbnf_and_walkdown(harness):
    """koboldcpp dialect sends raw GBNF; a rung-1 failure walks down to 2 like any other."""
    mock, ladder, cfg, store = harness
    store.caps_set(LOCAL_EP.base_url, LOCAL_EP.model, 1, native="koboldcpp")
    mock.enqueue(chat_reply("busted"))                              # rung 1 call
    mock.enqueue(chat_reply("busted again"))                        # rung 1 repair (the ONE pass)
    mock.enqueue(chat_reply(GOOD))                                  # rung 2 rescues
    delta = await ladder.extract(LOCAL_EP, "s", "c", 1, 1, "x")
    assert delta is not None and ladder.last_rung == 2
    b0, b2 = json.loads(mock.requests[0].body), json.loads(mock.requests[2].body)
    assert "root ::= object" in b0["grammar"]                       # GBNF payload
    assert b2["response_format"]["type"] == "json_schema"           # rung 2 body
    assert store.caps_get(LOCAL_EP.base_url, LOCAL_EP.model)["failures"] == 1
    # demotion preserves the dialect (store native=None path)
    store.caps_set(LOCAL_EP.base_url, LOCAL_EP.model, 2)
    assert store.caps_get(LOCAL_EP.base_url, LOCAL_EP.model)["native"] == "koboldcpp"


async def test_force_rung1_skips_probe_defaults_llamacpp(harness):
    mock, ladder, cfg, store = harness
    cfg.upstream.force_rung = 1
    mock.enqueue(Reply(status=404, body=b"{}"))                     # GET /models: nothing useful
    mock.enqueue(chat_reply(GOOD))                                  # the extraction call itself
    delta = await ladder.extract(LOCAL_EP, "s", "c", 1, 1, "x")
    assert delta is not None and ladder.last_rung == 1
    gen = json.loads(mock.requests[1].body)
    assert isinstance(gen["json_schema"], dict)                     # default llama.cpp dialect
    assert store.caps_get(LOCAL_EP.base_url, LOCAL_EP.model) is None    # probe cache untouched


# ------------------------------ Venice gate fixes (live eval #1, 2026-07-03) ------------------------------
VENICE_EP = Endpoint(base_url="https://api.venice.ai/api/v1", model="zai-org-glm-4.7-flash")


async def test_venice_auto_thinking_model_gets_thinking_and_budget(harness):
    """auto + thinking-capable model (glm-4.7-flash): reasoning ON, max_tokens bumped,
    Venice system prompt ALWAYS excluded (pure waste for extraction)."""
    mock, ladder, cfg, store = harness
    store.caps_set(VENICE_EP.base_url, VENICE_EP.model, 3)          # seed, skip probe
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(VENICE_EP, "s", "c", 1, 1, "x")) is not None
    body = json.loads(mock.requests[0].body)
    assert body["venice_parameters"] == {"disable_thinking": False,
                                         "include_venice_system_prompt": False}
    assert body["max_tokens"] == cfg.extraction.thinking_max_tokens
    # non-Venice endpoints never carry the vendor block
    assert "venice_parameters" not in ladder._body(EP, 3, "s", "u")


async def test_thinking_off_disables_and_keeps_lean_budget(harness):
    mock, ladder, cfg, store = harness
    cfg.extraction.thinking = "off"                                 # low-budget preset (Q8)
    store.caps_set(VENICE_EP.base_url, VENICE_EP.model, 3)
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(VENICE_EP, "s", "c", 1, 1, "x")) is not None
    body = json.loads(mock.requests[0].body)
    assert body["venice_parameters"]["disable_thinking"] is True
    assert body["max_tokens"] == cfg.extraction.max_tokens


async def test_thinking_auto_detection_and_on_override(harness):
    from aetherstate.extraction import thinking_active, thinking_supported
    mock, ladder, cfg, store = harness
    assert thinking_supported("zai-org-glm-4.7-flash")
    assert thinking_supported("zai-org-glm-5-2")
    assert thinking_supported("deepseek-r1-distill")
    assert not thinking_supported("qwen2.5-7b-instruct")            # most locals: no
    assert not thinking_supported("llama-3.3-70b")
    plain = Endpoint(base_url="https://api.venice.ai/api/v1", model="llama-3.3-70b")
    assert not thinking_active(cfg, plain)                          # auto + non-thinking
    cfg.extraction.thinking = "on"                                  # high-budget override
    assert thinking_active(cfg, plain)
    body = ladder._body(plain, 3, "s", "u")
    assert body["max_tokens"] == cfg.extraction.thinking_max_tokens
    assert body["venice_parameters"]["disable_thinking"] is False


async def test_probes_always_disable_thinking(harness):
    """A capability probe (max_tokens 30) must never burn its budget on reasoning —
    thinking mode does not apply to probes, even when 'on'."""
    mock, ladder, cfg, store = harness
    cfg.extraction.thinking = "on"
    mock.enqueue(chat_reply('{"ok": true}'))                        # P2 probe succeeds
    assert await ladder.rung_for(VENICE_EP) == 2
    probe = json.loads(mock.requests[0].body)
    assert probe["venice_parameters"]["disable_thinking"] is True
    assert probe["max_tokens"] == 30


async def test_empty_content_falls_back_to_reasoning_content(harness):
    """Belt+suspenders: if thinking sneaks through, salvage the JSON from reasoning."""
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 4)
    body = json.dumps({"choices": [{"message": {
        "role": "assistant", "content": "", "reasoning_content": f"Let me think... {GOOD}"}}]})
    mock.enqueue(Reply(body=body.encode()))
    delta = await ladder.extract(EP, "s", "c", 1, 1, "x")
    assert delta is not None and delta.ops == []


async def test_429_retries_with_backoff_then_succeeds_no_strike(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    ladder.retry_sleep = fake_sleep
    mock.enqueue(Reply(status=429, headers={"content-type": "application/json",
                                            "retry-after": "1"}, body=b'{"error":"rate"}'))
    mock.enqueue(Reply(status=429, headers={"content-type": "application/json",
                                            "retry-after": "3"}, body=b'{"error":"rate"}'))
    mock.enqueue(chat_reply(GOOD))
    delta = await ladder.extract(EP, "s", "c", 1, 1, "x")
    assert delta is not None and ladder.last_rung == 2
    assert slept == [1.0, 3.0]                                      # Retry-After honored
    assert store.caps_get(EP.base_url, EP.model)["failures"] == 0   # transient != strike


async def test_persistent_429_aborts_ladder_without_strike_or_walkdown(harness):
    """A rate-limited endpoint must not be hammered at rungs 3/4 nor demoted (06 A.2:
    strikes are VALIDATION failures). The batch stays 'failed' and re-runs later."""
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)

    async def fake_sleep(s):
        pass

    ladder.retry_sleep = fake_sleep
    for _ in range(3):                                              # initial + 2 retries
        mock.enqueue(Reply(status=429, body=b'{"error":"rate limit"}'))
    delta = await ladder.extract(EP, "s", "c", 1, 1, "x")
    assert delta is None and ladder.last_rung is None
    assert len(mock.requests) == 3                                  # no rung-3/4 hammering
    assert store.caps_get(EP.base_url, EP.model)["failures"] == 0   # no demotion pressure
    assert store.caps_get(EP.base_url, EP.model)["rung"] == 2
    assert ladder.last_failure_kind == "transient_upstream"
    assert ladder.last_failure_attempts == [{
        "rung": 2,
        "kind": "transient_upstream",
        "status": 429,
    }]


async def test_validation_exhaustion_records_bounded_diagnostics(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 4)
    mock.enqueue(chat_reply("not json"))
    mock.enqueue(chat_reply("still not json"))

    delta = await ladder.extract(EP, "s", "c", 9, 9, "x")

    assert delta is None
    assert ladder.last_failure_kind == "validation"
    assert ladder.last_failure_attempts == [{
        "rung": 4,
        "kind": "validation",
        "raw_chars": 14,
        "raw_sha256": ladder.last_failure_attempts[0]["raw_sha256"],
    }]
    assert len(ladder.last_failure_attempts[0]["raw_sha256"]) == 64


# ------------------------------ Q17: op vocabulary at schema rungs (live eval #1) ------------------------------
def test_strict_schema_enumerates_op_vocabulary():
    """Live eval #1 root cause: schemas enforce SHAPE, not vocabulary — the model could not
    emit ops it had never seen. The op field is now a closed enum of the 16 extraction ops."""
    from aetherstate import state
    from aetherstate.extraction import EXTRACTION_OPS
    props = delta_json_schema()["schema"]["properties"]["ops"]["items"]["properties"]
    assert props["op"]["enum"] == EXTRACTION_OPS
    assert len(EXTRACTION_OPS) == 16 and EXTRACTION_OPS == sorted(EXTRACTION_OPS)
    assert set(EXTRACTION_OPS) <= set(state._SPEC)              # single source of truth
    assert not {"freeze", "unfreeze", "scene_set", "entity_add",
                "consent_set", "roll"} & set(EXTRACTION_OPS)     # engine-internal: never offered
    assert None not in props["op"]["enum"]                       # op is required, non-null
    assert "safeword" in props["signal"]["enum"]                 # E06's missing variant
    assert None in props["signal"]["enum"]                       # nullable enums admit null
    assert "remove" in props["action"]["enum"] and "consume" in props["action"]["enum"]


def test_schema_is_byte_stable_across_calls():
    """06 A.4: ONE stable schema — hosted compilers cache by content; sets must not leak
    nondeterministic ordering into the schema string."""
    assert json.dumps(delta_json_schema()) == json.dumps(delta_json_schema())


def test_op_card_ships_at_every_rung_by_default():
    from aetherstate import prompts
    for rung in (1, 2, 3, 4):
        assert CARD_MARK in prompts.system_prompt(rung, assist_tier=False)
    # move_entity et al are now visible at rung 2 — the Class-1 failure set
    assert "move_entity" in prompts.system_prompt(2)
    assert "time_advance" in prompts.system_prompt(2)


RECAP_MARK = "retold, remembered, or recapped"   # Q19: E16 anti-recap rule lives in SYSTEM_CORE
NEWNAME_MARK = "NEW named person"                # Q20: text-introduced entities feed 08 B2 quarantine
LOC_MARK = "Never coin\n  location IDs"           # run-5: E01 location naming (SYSTEM_CORE)
DIR_MARK = 'NEVER "who asked"'                   # run-5: E04 directionality (OP CARD tail)


def test_run5_stack_rules_ship_correctly():
    from aetherstate import prompts
    for rung in (1, 2, 3, 4):
        for assist in (False, True):
            sysp = prompts.system_prompt(rung, assist_tier=assist)
            assert NEWNAME_MARK in sysp and LOC_MARK in sysp   # core rules: universal
            assert DIR_MARK in sysp                            # card ships by default
    # directionality lives in the CARD: trimmed with it at schema rungs, kept at parse floor
    assert DIR_MARK not in prompts.system_prompt(2, False, include_card=False)
    assert DIR_MARK in prompts.system_prompt(3, False, include_card=False)
    # core rules survive the card trim
    assert NEWNAME_MARK in prompts.system_prompt(2, False, include_card=False)
    assert LOC_MARK in prompts.system_prompt(2, False, include_card=False)



def test_recap_rule_ships_every_rung_and_survives_card_trim():
    from aetherstate import prompts
    for rung in (1, 2, 3, 4):
        assert RECAP_MARK in prompts.system_prompt(rung, assist_tier=False)
        assert RECAP_MARK in prompts.system_prompt(rung, assist_tier=True)
    assert RECAP_MARK in prompts.system_prompt(2, False, include_card=False)


async def test_ladder_exposes_last_raw_for_evals(harness):
    """run_eval sidecar depends on last_raw = the text the model ACTUALLY emitted (pre-scrub)."""
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)
    mock.enqueue(chat_reply("NOT JSON"))                            # rung 2 call
    mock.enqueue(chat_reply("STILL NOT JSON"))                      # rung 2 repair
    mock.enqueue(chat_reply(GOOD))                                  # rung 3 succeeds
    delta = await ladder.extract(EP, "(nothing)", "Kira", 1, 1, "Kira: hi")
    assert delta is not None
    assert ladder.last_raw == GOOD                                  # raw of the SUCCESSFUL call
    mock.enqueue(Reply(status=429, body=b"{}"))                     # next case: transient abort
    mock.enqueue(Reply(status=429, body=b"{}"))
    mock.enqueue(Reply(status=429, body=b"{}"))
    ladder.retry_sleep = lambda _s: _noop()
    assert await ladder.extract(EP, "(nothing)", "Kira", 2, 2, "Kira: yo") is None
    assert ladder.last_raw is None                                  # never stale across cases


async def _noop():
    return None


CARD_MARK = "OP CARD (op -> required fields)"   # SYSTEM_CORE now NAMES the card (Q18 rule),
#                                                 so presence tests match the card header


def test_trim_op_card_only_bites_schema_rungs_never_assist():
    from aetherstate import prompts
    assert CARD_MARK not in prompts.system_prompt(2, False, include_card=False)
    assert CARD_MARK not in prompts.system_prompt(1, False, include_card=False)
    assert CARD_MARK in prompts.system_prompt(3, False, include_card=False)  # parse floor
    assert CARD_MARK in prompts.system_prompt(4, False, include_card=False)
    assert CARD_MARK in prompts.system_prompt(2, True, include_card=False)   # content floor (04 SS5)


async def test_rung2_extraction_carries_card_unless_trimmed(harness):
    mock, ladder, cfg, store = harness
    store.caps_set(EP.base_url, EP.model, 2)
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    system = json.loads(mock.requests[0].body)["messages"][0]["content"]
    assert CARD_MARK in system                                   # quality default

    cfg.extraction.trim_op_card = True                           # budget knob (Q17)
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    system = json.loads(mock.requests[1].body)["messages"][0]["content"]
    assert CARD_MARK not in system


# ------------------------------ Q18: mega-op field bleed (live E01 capture) ------------------------------
# Verbatim from a live diagnostic run (GLM-5.2, rung 2, thinking off): ONE op wearing every
# op type's fields — strict all-required + enums nudges filling over nulling.
MEGA_OP = ('{"schema":"aetherstate/delta/1","turn_range":[4,4],"ops":[{"op":"move_entity",'
           '"entity":"Kira","key":"to_location","value":"tavern_interior","to_location":'
           '"tavern_interior","present":true,"char":"Kira","item":"cloak","action":"displace",'
           '"moved_to":"shaken_off","participants":["Kira"],"base":"standing","anchor":'
           '"tavern_door","detail":"shaking water from cloak","from_char":"Kira","from_part":'
           '"hands","to_char":"Kira","to_part":"cloak","type":"touching","intensity":0,'
           '"object":"cloak","delta":0,"set":0,"valence":0,"energy":0,"dominance":0,'
           '"category":"other","signal":"grant","max_intensity":0,"dimension":"trust",'
           '"reason":"getting out of rain together","learner":"Kira","statement":'
           '"Bean wants to get out of the rain","source":"witnessed","teller":"Bean","text":'
           '"Kira and Bean take shelter from rain in the tavern","importance":2,"tags":'
           '["setting","weather"],"goal":"Bean","minutes":0,"to_time_of_day":"evening",'
           '"target_kind":"entity","target":"tavern","flavor":"shelter","behavior_note":'
           '"seeking dry place","substance":"rain"}]}')


def test_mega_op_scrubbed_to_its_own_fields():
    """The bloated op reduces to a clean move_entity; the spurious consent/mood/relationship/
    craving values can never reach the journal, state, or matchers."""
    d = parse_and_validate(MEGA_OP)
    assert d is not None and len(d.ops) == 1
    assert d.ops[0] == {"op": "move_entity", "entity": "Kira",
                        "to_location": "tavern_interior"}


def test_scrub_keeps_apply_side_optionals_and_own_fields():
    from aetherstate.extraction import scrub_op
    op = {"op": "clothing", "char": "Kira", "item": "jacket", "action": "remove",
          "covers": ["chest"], "moved_to": "chair", "signal": "grant", "delta": 5}
    assert scrub_op(op) == {"op": "clothing", "char": "Kira", "item": "jacket",
                            "action": "remove", "covers": ["chest"], "moved_to": "chair"}
    op = {"op": "time_advance", "to_time_of_day": "morning", "calendar_note": "festival",
          "valence": 3}
    assert scrub_op(op) == {"op": "time_advance", "to_time_of_day": "morning",
                            "calendar_note": "festival"}


def test_scrub_passes_unknown_kinds_untouched():
    """Unknown ops keep their shape so downstream quarantine reports them honestly."""
    from aetherstate.extraction import scrub_op
    weird = {"op": "teleport", "entity": "Kira", "signal": "grant"}
    assert scrub_op(weird) == weird


def test_multiple_clean_ops_survive_scrub_unchanged():
    body = json.dumps({"schema": "aetherstate/delta/1", "turn_range": [12, 12], "ops": [
        {"op": "contact", "action": "start", "from_char": "Kira", "to_char": "Bean",
         "type": "kissing", "intensity": 1},
        {"op": "arousal", "char": "Kira", "delta": 10}]})
    d = parse_and_validate(body)
    assert len(d.ops) == 2
    assert d.ops[0]["type"] == "kissing" and d.ops[1] == {"op": "arousal", "char": "Kira",
                                                          "delta": 10}


def test_system_core_carries_one_op_per_change_rule():
    from aetherstate.prompts import OP_CARD, SYSTEM_CORE
    assert "one op per change" in SYSTEM_CORE.lower()
    assert "null" in SYSTEM_CORE
    assert "one op per change" in OP_CARD.lower()
