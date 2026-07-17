"""P4 item 1 (Q18 addendum): anyOf per-op wire schema — builder shape/stability, the
anyof capability probe + caching + flat fallback, enum salvage vs quarantine (E10 "pin"),
the store migration for pre-existing DBs, and the op-add cheapness weld (Q22)."""
from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from aetherstate import state
from aetherstate.config import Config
from aetherstate.extraction import (_OP_ALLOWED, _OP_ENUMS, _OP_FIELDS, EXTRACTION_OPS,
                                    Endpoint, Ladder, delta_json_schema,
                                    delta_json_schema_anyof, enum_salvage,
                                    parse_and_validate)
from aetherstate.state import OP_FIELD_ENUMS, apply_delta, validate_op
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply

GOOD = '{"schema":"aetherstate/delta/1","turn_range":[1,1],"ops":[]}'
ANYOF_OK = '{"ops":[{"op":"time_advance","minutes":30}]}'


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
    store = Store(":memory:")
    ladder = Ladder(store, cfg, lambda: client)

    async def _nosleep(_s):
        return None
    ladder.retry_sleep = _nosleep
    return mock, ladder, cfg, store


EP = Endpoint(base_url="http://mock-upstream/v1", model="glm-test")


# ------------------------------ builder shape + stability ------------------------------
def test_anyof_schema_branches_are_per_op_and_byte_stable():
    s = delta_json_schema_anyof()
    assert s["name"] == "aetherstate_delta_v2" and s["strict"] is True
    branches = s["schema"]["properties"]["ops"]["items"]["anyOf"]
    assert [b["properties"]["op"]["enum"] for b in branches] == [[k] for k in EXTRACTION_OPS]
    assert json.dumps(delta_json_schema_anyof()) == json.dumps(delta_json_schema_anyof())
    by_kind = {b["properties"]["op"]["enum"][0]: b for b in branches}
    move = by_kind["move_entity"]                       # a branch only HAS its own fields
    assert set(move["properties"]) == {"op", "entity", "to_location"}
    assert move["required"] == ["op", "entity", "to_location"]     # Venice strict: all required
    assert move["additionalProperties"] is False
    assert "char" not in move["properties"]             # mega-op filler now schema-impossible
    # branch enums are PER-OP (flat union split back apart): goal.action is 3 values + null
    goal_action = by_kind["goal"]["properties"]["action"]["enum"]
    assert goal_action == ["abandon", "add", "complete", None]
    assert by_kind["craving"]["properties"]["action"]["enum"] == ["adjust", "consume", None]
    # Fresh knowledge is actor-local; the legacy combined fact/belief op is not on the wire.
    assert "reveal_fact" not in by_kind
    assert set(by_kind["belief_acquire"]["properties"]) == {
        "op", "holder", "statement", "stance", "evidence_source", "teller",
    }
    assert "covers" not in by_kind["clothing"]["properties"]
    assert "calendar_note" not in by_kind["time_advance"]["properties"]


def test_flat_schema_unchanged_by_table_derivation():
    """The flat schema now DERIVES from state.OP_FIELD_ENUMS — its content must equal the
    historical literal exactly (hosted compilers cache by content, 06 A.4)."""
    assert _OP_ENUMS["action"] == sorted(set(state.CLOTHING_STATE)
                                         | {"start", "stop", "change"}
                                         | {"consume", "adjust"}
                                         | {"add", "complete", "abandon"})
    assert _OP_ENUMS["to_time_of_day"] == list(state.TIMES)       # chronological, NOT sorted
    assert _OP_ENUMS["signal"] == sorted(set(state.SIGNAL_TO_LEVEL) | {"safeword"})
    assert _OP_ENUMS["op"] == EXTRACTION_OPS
    assert delta_json_schema()["name"] == "aetherstate_delta"     # names never collide
    assert "anyOf" not in json.dumps(delta_json_schema())


def test_op_add_stays_one_table_cheap():
    """Q22 standing directive: adding an op kind (inventory ops are the first customers)
    touches the tables only — builders, scrub, and salvage all derive. This weld breaks
    loudly if the tables drift apart."""
    from aetherstate.extraction import (EXTRACTION_OPS_RPG, RPG_COMBAT_OPS, RPG_EFFECT_OPS,
                                        RPG_GAP_OPS, RPG_ITEM_OPS, RPG_SOCIAL_OPS,
                                        _RPG_OP_FIELDS)
    rpg_ops = set(RPG_ITEM_OPS) | set(RPG_EFFECT_OPS) | set(RPG_SOCIAL_OPS) \
        | set(RPG_GAP_OPS) | set(RPG_COMBAT_OPS)       # RPG-2 + 3 + 3b + 5 + Phase 1
    assert set(EXTRACTION_OPS) | rpg_ops == set(_OP_ALLOWED)   # are the rpg tier
    assert set(EXTRACTION_OPS_RPG) == set(EXTRACTION_OPS) | rpg_ops
    assert set(EXTRACTION_OPS_RPG) <= set(state._SPEC)
    for kind, enums in OP_FIELD_ENUMS.items():
        if kind == "reveal_fact":
            continue  # reducer/replay compatibility only; never a fresh extraction producer
        assert kind in _OP_ALLOWED, kind
        assert set(enums) <= _OP_ALLOWED[kind], (kind, enums)
        for f, vocab in enums.items():
            assert vocab == sorted(vocab) or f == "to_time_of_day", (kind, f)
    for kind in EXTRACTION_OPS:                        # every wire field is typed
        assert _OP_ALLOWED[kind] & set(_OP_FIELDS), kind
    for kind in sorted(rpg_ops):                       # ...including the rpg-gated tier
        assert _OP_ALLOWED[kind] & (set(_OP_FIELDS) | set(_RPG_OP_FIELDS)), kind


# ------------------------------ validate_op agrees with the table ------------------------------
_MINIMAL: dict[str, dict] = {
    "clothing": {"op": "clothing", "char": "a", "item": "shirt", "action": "remove"},
    "position": {"op": "position", "participants": ["a"], "base": "standing"},
    "contact": {"op": "contact", "action": "start", "from_char": "a", "to_char": "b",
                "type": "touching"},
    "consent_signal": {"op": "consent_signal", "from_char": "a", "to_char": "b",
                       "category": "kissing", "signal": "grant"},
    "relationship_adj": {"op": "relationship_adj", "from_char": "a", "to_char": "b",
                         "dimension": "trust", "delta": 1},
    "reveal_fact": {"op": "reveal_fact", "learner": "a", "statement": "s", "source": "told"},
    "goal": {"op": "goal", "char": "a", "action": "add", "text": "t"},
    "time_advance": {"op": "time_advance", "minutes": 10, "to_time_of_day": "evening"},
    "obsession": {"op": "obsession", "char": "a", "target_kind": "entity", "target": "b",
                  "delta": 1},
    "craving": {"op": "craving", "char": "a", "substance": "wine", "action": "consume"},
    # RPG-5 quest ledger (rpg wire tier)
    "quest_add": {"op": "quest_add", "name": "q", "stakes": "serious"},
    "quest_update": {"op": "quest_update", "quest": "q", "status": "complete"},
}


def test_enum_table_matches_validate_op():
    """The weld: every out-of-vocabulary value on every enum field of every extraction op
    is REJECTED by validate_op (-> apply-side quarantine, never silent apply)."""
    for kind, enums in OP_FIELD_ENUMS.items():
        base = _MINIMAL[kind]
        assert validate_op(dict(base)) is not None, kind
        for f in enums:
            assert validate_op({**base, f: "___bogus___"}) is None, (kind, f)


def test_e10_pin_quarantines_never_applies():
    """Run-5 disk finding: Venice strict passed action="pin" (structure enforced, enum
    values NOT). The apply choke point must quarantine it visibly."""
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kira"}], "user", cfg)
    r = apply_delta(store, sid, bid, 1,
                    [{"op": "goal", "char": "Kira", "action": "pin", "text": "corkboard"}],
                    "extraction", cfg)
    assert len(r.quarantined) == 1 and "malformed" in r.quarantined[0]["reason"]
    assert not r.applied if hasattr(r, "applied") else True


# ------------------------------ parse-side enum salvage ------------------------------
def test_enum_salvage_drops_bad_optional_keeps_bad_required():
    # optional out-of-vocab -> dropped, rest of the op survives apply
    op = enum_salvage({"op": "time_advance", "minutes": 30, "to_time_of_day": "midnight"})
    assert op == {"op": "time_advance", "minutes": 30}
    assert validate_op(dict(op)) is not None
    # required out-of-vocab -> KEPT intact for the visible quarantine path
    op = enum_salvage({"op": "goal", "char": "a", "action": "pin", "text": "t"})
    assert op["action"] == "pin" and validate_op(dict(op)) is None
    # valid values never touched
    op = enum_salvage(dict(_MINIMAL["craving"]))
    assert op == _MINIMAL["craving"]


def test_parse_and_validate_salvages_padded_rung2_reply():
    padded = json.dumps({"schema": "aetherstate/delta/1", "turn_range": [2, 2], "ops": [
        {"op": "time_advance", "minutes": 45, "to_time_of_day": "midnight",
         "entity": None, "char": None}]})
    d = parse_and_validate(padded)
    assert d.ops == [{"op": "time_advance", "minutes": 45}]


# ------------------------------ probe + selection + fallback ------------------------------
async def test_anyof_probe_accepted_selects_anyof_schema(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(chat_reply('{"ok": true}'))            # P2 probe -> rung 2
    mock.enqueue(chat_reply(ANYOF_OK))                  # anyof probe -> accepted
    assert await ladder.rung_for(EP) == 2
    assert store.caps_get(EP.base_url, EP.model)["anyof"] == 1
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    rf = json.loads(mock.requests[-1].body)["response_format"]["json_schema"]
    assert rf["name"] == "aetherstate_delta_v2"
    assert "anyOf" in json.dumps(rf["schema"])


async def test_anyof_probe_rejected_falls_back_flat(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(chat_reply('{"ok": true}'))            # P2 -> rung 2
    mock.enqueue(Reply(status=400, body=b'{"error":"anyOf unsupported"}'))
    assert await ladder.rung_for(EP) == 2
    assert store.caps_get(EP.base_url, EP.model)["anyof"] == 0
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    rf = json.loads(mock.requests[-1].body)["response_format"]["json_schema"]
    assert rf["name"] == "aetherstate_delta"            # flat, forever cached as 0


async def test_anyof_transient_stays_unprobed_and_retries_within_ttl(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(chat_reply('{"ok": true}'))            # P2 -> rung 2
    for _ in range(3):                                  # anyof probe: persistent 429
        mock.enqueue(Reply(status=429, body=b'{"error":"limit"}'))
    assert await ladder.rung_for(EP) == 2
    assert store.caps_get(EP.base_url, EP.model)["anyof"] == -1   # verdict left open
    mock.enqueue(chat_reply(ANYOF_OK))                  # next rung_for retries JUST anyof
    assert await ladder.rung_for(EP) == 2               # (row still fresh within TTL)
    assert store.caps_get(EP.base_url, EP.model)["anyof"] == 1


async def test_use_anyof_off_never_probes_never_selects(harness):
    mock, ladder, cfg, store = harness
    cfg.extraction.use_anyof = False
    mock.enqueue(chat_reply('{"ok": true}'))            # P2 -> rung 2, and NOTHING else
    assert await ladder.rung_for(EP) == 2
    assert len(mock.requests) == 1                      # no anyof call
    store.caps_set(EP.base_url, EP.model, 2, anyof=1)   # even a cached yes is ignored
    mock.enqueue(chat_reply(GOOD))
    assert (await ladder.extract(EP, "s", "c", 1, 1, "x")) is not None
    rf = json.loads(mock.requests[-1].body)["response_format"]["json_schema"]
    assert rf["name"] == "aetherstate_delta"


async def test_rung3_endpoint_never_anyof_probed(harness):
    mock, ladder, cfg, store = harness
    mock.enqueue(Reply(status=400, body=b"{}"))         # P2 fails
    mock.enqueue(chat_reply('{"ok": true}'))            # P3 -> rung 3
    assert await ladder.rung_for(EP) == 3
    assert len(mock.requests) == 2                      # no anyof probe at rung 3
    assert store.caps_get(EP.base_url, EP.model)["anyof"] == -1


def test_demotion_preserves_anyof_verdict():
    store = Store(":memory:")
    store.caps_set("http://x/v1", "m", 2, native="", anyof=1)
    store.caps_set("http://x/v1", "m", 3)               # demotion path passes neither
    row = store.caps_get("http://x/v1", "m")
    assert row["rung"] == 3 and row["anyof"] == 1


def test_live_db_migration_adds_anyof_column(tmp_path):
    """A pre-P4 live DB predates the column — additive migration, default unprobed."""
    p = tmp_path / "aether.db"
    db = sqlite3.connect(p)
    db.execute("""CREATE TABLE caps(
      base_url TEXT, model TEXT, rung INTEGER, probed_at REAL, failures INTEGER DEFAULT 0,
      native TEXT DEFAULT '', PRIMARY KEY(base_url, model))""")
    db.execute("INSERT INTO caps(base_url, model, rung, probed_at) VALUES('b','m',2,1.0)")
    db.commit()
    db.close()
    store = Store(p)
    row = store.caps_get("b", "m")
    assert row["anyof"] == -1 and row["rung"] == 2 and row["native"] == ""
