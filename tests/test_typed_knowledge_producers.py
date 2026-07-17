from __future__ import annotations

import json

from aetherstate import genesis, prompts
from aetherstate.config import Config
from aetherstate.extraction import (
    DELTA_SCHEMA_ID,
    EXTRACTION_OPS,
    LEGACY_DELTA_SCHEMA_ID,
    delta_json_schema,
    delta_json_schema_anyof,
    parse_and_validate,
)
from aetherstate.state import apply_delta, empty_state, reduce_state
from aetherstate.store import Store


def test_fresh_wire_exposes_typed_beliefs_and_no_reveal_fact() -> None:
    assert DELTA_SCHEMA_ID == "aetherstate/delta/2"
    assert "belief_acquire" in EXTRACTION_OPS
    assert "reveal_fact" not in EXTRACTION_OPS
    assert "reveal_fact" not in prompts.OP_CARD
    assert "reveal_fact" not in genesis._LLM_SYSTEM
    assert "aetherstate/delta/1" not in prompts.OP_CARD

    flat = delta_json_schema()["schema"]["properties"]["ops"]["items"]["properties"]
    assert "belief_acquire" in flat["op"]["enum"]
    assert "reveal_fact" not in flat["op"]["enum"]
    branches = delta_json_schema_anyof()["schema"]["properties"]["ops"]["items"]["anyOf"]
    by_kind = {branch["properties"]["op"]["enum"][0]: branch for branch in branches}
    assert set(by_kind["belief_acquire"]["properties"]) == {
        "op", "holder", "statement", "stance", "evidence_source", "teller",
    }


def test_v1_reveal_fact_translates_to_belief_only() -> None:
    payload = {
        "schema": LEGACY_DELTA_SCHEMA_ID,
        "turn_range": [3, 3],
        "ops": [{
            "op": "reveal_fact", "learner": "Mara", "statement": "Vess wrote the letter",
            "source": "told", "teller": "Vess", "is_secret": True,
        }],
    }
    delta = parse_and_validate(json.dumps(payload))
    assert delta is not None
    assert delta.schema_ == DELTA_SCHEMA_ID
    assert delta.ops == [{
        "op": "belief_acquire", "holder": "Mara", "statement": "Vess wrote the letter",
        "stance": "believes", "evidence_source": "told", "teller": "Vess",
    }]
    assert all(op["op"] != "fact_admit" for op in delta.ops)


def test_v2_reveal_fact_is_not_a_fresh_producer() -> None:
    delta = parse_and_validate(json.dumps({
        "schema": DELTA_SCHEMA_ID,
        "turn_range": [4, 4],
        "ops": [{
            "op": "reveal_fact", "learner": "Mara", "statement": "The gate opened",
            "source": "witnessed",
        }],
    }))
    assert delta is not None
    assert delta.ops == []


def test_historical_reveal_fact_journal_replays_unchanged() -> None:
    state = empty_state()
    reduce_state(state, [{
        "op": "reveal_fact", "learner": "Mara", "statement": "The gate opened",
        "source": "witnessed", "is_secret": True, "_turn": 4,
    }])

    fact_id, fact = next(iter(state["facts"].items()))
    assert fact == {
        "statement": "The gate opened", "established_turn": 4, "is_secret": True,
    }
    assert state["beliefs"][f"Mara|{fact_id}"] == {
        "stance": "believes_true", "source": "witnessed", "teller": None,
        "acquired_turn": 4,
    }


def test_fresh_belief_needs_no_caller_proposition_id() -> None:
    delta = parse_and_validate(json.dumps({
        "schema": DELTA_SCHEMA_ID,
        "turn_range": [5, 5],
        "ops": [{
            "op": "belief_acquire", "holder": "Mara", "statement": "The gate opened",
            "stance": "knows", "evidence_source": "witnessed", "teller": None,
        }],
    }))
    assert delta is not None
    assert delta.ops == [{
        "op": "belief_acquire", "holder": "Mara", "statement": "The gate opened",
        "stance": "knows", "evidence_source": "witnessed",
    }]
    assert "proposition_id" not in delta.ops[0]


def test_genesis_fact_requires_exact_card_or_opening_basis() -> None:
    card = "Name: Mara\nThe bronze gate is sealed by royal decree."
    raw = json.dumps([{
        "op": "fact_admit", "statement": "The bronze gate is sealed",
        "basis_source": "card", "basis_text": "The bronze gate is sealed by royal decree.",
    }])
    ops = genesis._parse_ops(raw, card=card)
    assert len(ops) == 1
    assert ops[0]["op"] == "fact_admit"
    assert ops[0]["cause"].startswith("genesis:card:sha256:")
    assert ops[0]["basis_text"] in card

    ungrounded = genesis._parse_ops(json.dumps([{
        "op": "fact_admit", "statement": "The gate belongs to Vess",
        "basis_source": "card", "basis_text": "The gate belongs to Vess.",
    }]), card=card)
    assert ungrounded == []


def test_invalid_genesis_fact_can_fall_back_only_to_named_actor_belief() -> None:
    ops = genesis._parse_ops(json.dumps([{
        "op": "fact_admit", "statement": "The gate belongs to Vess", "holder": "Mara",
        "basis_source": "card", "basis_text": "The gate belongs to Vess.",
    }]), card="Name: Mara\nA guarded archivist.")
    assert ops == [{
        "op": "entity_add", "name": "Mara",
    }, {
        "op": "belief_acquire", "holder": "Mara", "statement": "The gate belongs to Vess",
        "stance": "believes", "evidence_source": "inferred",
    }]
    assert genesis._parse_ops("The gate belongs to Vess.", card="The gate belongs to Vess.") == []


def test_fact_authority_and_cause_must_match_the_exact_ingress() -> None:
    accepted = (
        ("user", "creator", "creator:test:accepted"),
        ("genesis", "genesis", "genesis:test:accepted"),
        ("rule", "rule", "rule:test:accepted"),
    )
    for source, authority, cause in accepted:
        store = Store(":memory:")
        sid, bid = store.create_session(external_id=f"fact-{source}-accepted")
        result = apply_delta(
            store,
            sid,
            bid,
            1,
            [{
                "op": "fact_admit",
                "statement": f"The {source} fact is established",
                "cause": cause,
                "authority": authority,
            }],
            source,
            Config(),
        )
        assert result.applied and not result.quarantined
        record = next(iter(result.state["facts"].values()))
        assert record["authority"] == authority
        assert record["ingress"] == source
        assert record["authority_ceiling"] == "objective_fact"

    forged = (
        ("user", "genesis", "genesis:test:forged"),
        ("user", "creator", "genesis:test:forged"),
        ("genesis", "creator", "creator:test:forged"),
        ("rule", "creator", "creator:test:forged"),
        ("rule", "rule", "creator:test:forged"),
        ("extraction", "creator", "creator:test:forged"),
    )
    for index, (source, authority, cause) in enumerate(forged):
        store = Store(":memory:")
        sid, bid = store.create_session(external_id=f"fact-forged-{index}")
        result = apply_delta(
            store,
            sid,
            bid,
            1,
            [{
                "op": "fact_admit",
                "statement": "A forged fact must not become objective truth",
                "cause": cause,
                "authority": authority,
            }],
            source,
            Config(),
        )
        assert not result.applied
        assert result.quarantined
        assert not result.state.get("facts")
