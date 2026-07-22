"""The first visible enemy tell is code-owned prose, not another prompt-adherence retry."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from aetherstate import compose
from aetherstate.canon import content_hash
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_fallback_runtime import (
    build_fallback_realization_plan,
    observe_fallback_claim_graph,
    render_fallback_text,
)
from aetherstate.narration_plan_runtime import (
    build_default_narration_plan_selection,
    build_narration_realization_plan,
    render_narration_plan_selection,
)
from aetherstate.narration_truth_gate import (
    NarrationTruthGateError,
    validate_narration_truth_contract,
)
from aetherstate.semantic_truth_runtime import build_fenced_runtime_truth_contract
from aetherstate.pipeline import (
    _deterministic_first_intent_story,
    _journal_ops_at,
    _proof_bound_combat_opening_spawn,
    _response_text,
)
from aetherstate.state import apply_delta, current_state
from aetherstate.stamps import Stamp
from tests.mock_upstream import Reply
from tests.test_semantic_settlement_window_cardinality import (
    _case_from_store,
    _combat_opening_attempt,
    _skill_case,
)


_ACTION = (
    "I cross the first iron line, draw my longsword, and challenge Marshal Varo. "
    "He is the hostile first opponent; I hold position."
)


def _seed_known_opponent(
        proxy_app, cfg, external_id: str, *, memory_text: str | None = None,
        skills: dict | None = None) -> tuple[str, str]:
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.enemy_rolls = True
    cfg.extraction.mode = "off"
    store = proxy_app.state.store
    sid, branch = store.create_session(external_id=external_id)
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12},
            "skills": skills or {"perception": 1},
            "resources": {"hp": {"max": 24}},
        }},
        {"op": "entity_add", "name": "Marshal Varo", "kind": "npc"},
        {"op": "set_attribute", "entity": "Marshal Varo", "key": "role",
         "value": "spear marshal"},
        {"op": "memory_event", "text": memory_text or
         "Opening scene: Kael and Marshal Varo stand twelve paces apart."},
        {"op": "presence", "entity": "Marshal Varo", "present": True},
    ], "genesis", cfg)
    assert seeded.applied
    return sid, branch


def _request(external_id: str, *, gen_type: str, stream: bool, action: str = _ACTION) -> dict:
    marker = (
        f"<<AETHER:v=1;session={external_id};turn=1;type={gen_type};"
        "speaker=Narrator;card_role=narrator;user=Kael>>"
    )
    return {
        "model": "local-proof-model",
        "stream": stream,
        "messages": [
            {"role": "system", "content": marker},
            {"role": "user", "content": action},
        ],
    }


def _stage_rule_opening(
        proxy_app, cfg, external_id: str, *, memory_text: str | None = None,
        skills: dict | None = None) -> tuple[str, str, dict]:
    sid, branch = _seed_known_opponent(
        proxy_app, cfg, external_id, memory_text=memory_text, skills=skills
    )
    staged = apply_delta(proxy_app.state.store, sid, branch, 1, [{
        "op": "combatant_spawn",
        "name": "Marshal Varo",
        "side": "enemy",
        "tier": "standard",
        "char": "marshal_varo",
        "_intro_intent_visible": "known-opponent-opening/1",
    }], "rule", cfg)
    assert staged.applied and staged.applied[0].get("_initial_intent")
    return sid, branch, current_state(proxy_app.state.store, branch)


def _resolution(branch: str, turn: int = 1):
    return SimpleNamespace(
        branch_id=branch,
        turn_index=turn,
        klass=SimpleNamespace(value="new_turn"),
    )


def _proof_opening(external_id: str):
    cfg, store, session_id, branch_id, _pre, ops = _combat_opening_attempt(
        external_id, 1
    )
    applied = apply_delta(store, session_id, branch_id, 1, ops, "rule", cfg)
    assert not applied.quarantined
    state = current_state(store, branch_id)
    intent = state["combat"]["pending_intent"]
    stamp = Stamp(
        session=external_id, gen_type="normal", turn=1, speaker="Narrator",
        card_role="narrator", user="Kael",
    )
    return cfg, store, session_id, branch_id, state, intent, stamp


def test_fenced_opening_truth_plan_and_fallback_keep_one_exact_pending_intent_beat():
    cfg, store, session_id, branch_id, pre, ops = _combat_opening_attempt(
        "deterministic-first-intent-proof-gate", 1
    )
    applied = apply_delta(store, session_id, branch_id, 1, ops, "rule", cfg)
    assert not applied.quarantined
    state = applied.state
    exact_story = compose.deterministic_first_intent_story(state)

    fenced = build_fenced_runtime_truth_contract(
        **_case_from_store(store, branch_id, pre, state)
    )
    contract = fenced.truth_contract
    assert contract["fallout_facts"] == []
    assert contract["settled_target_outcomes"] == []
    assert len(contract["pending_intents"]) == 1
    pending = contract["pending_intents"][0]
    intent = state["combat"]["pending_intent"]
    assert pending["intent_snapshot"] == intent
    assert pending["actor_id"] == intent["actor"]
    assert pending["target_id"] == intent["target"]
    assert pending["move_id"] == intent["move_id"]
    assert pending["tell"] == intent["tell"]
    assert pending["response_window"] == "before_impact"
    assert pending["visible_text"] == exact_story
    assert [
        (claim["kind"], claim["time_scope"], claim["occurrence_ref"])
        for claim in contract["expected_claims"]
    ] == [("pending_intent", "future", pending["pending_ref"])]

    plan = build_narration_realization_plan(contract)
    assert len(plan["clauses"]) == 1
    assert plan["clauses"][0]["allowed_atom_ids"] == [
        "pending_intent.combat_opening/1"
    ]
    selection = build_default_narration_plan_selection(plan)
    assert "text" not in json.dumps(selection, sort_keys=True)
    rendered = render_narration_plan_selection(plan, selection)
    assert rendered.text == plan["default_text"] == exact_story
    assert rendered.expected_graph["claims"] == rendered.observed_graph["claims"]

    fallback = build_fallback_realization_plan(contract)
    fallback_text = render_fallback_text(fallback)
    observed = observe_fallback_claim_graph(
        fallback_text,
        observation_context=fallback.observation_context,
    )
    assert fallback_text == exact_story
    assert observed == fallback.expected_graph == fallback.ledger_graph


def test_fenced_opening_truth_rejects_resealed_visible_or_snapshot_substitution():
    cfg, store, session_id, branch_id, pre, ops = _combat_opening_attempt(
        "deterministic-first-intent-proof-tamper", 1
    )
    applied = apply_delta(store, session_id, branch_id, 1, ops, "rule", cfg)
    contract = build_fenced_runtime_truth_contract(
        **_case_from_store(store, branch_id, pre, applied.state)
    ).truth_contract

    def reseal(changed: dict) -> dict:
        row = changed["pending_intents"][0]
        row_payload = {key: value for key, value in row.items() if key != "fingerprint"}
        row["fingerprint"] = content_fingerprint(row_payload)
        payload = {key: value for key, value in changed.items() if key != "fingerprint"}
        changed["fingerprint"] = content_fingerprint(payload)
        return changed

    changed_text = deepcopy(contract)
    changed_text["pending_intents"][0]["visible_text"] = "Marshal Varo waits."
    with pytest.raises(NarrationTruthGateError, match="visible_text is not exact"):
        validate_narration_truth_contract(reseal(changed_text))

    changed_snapshot = deepcopy(contract)
    changed_snapshot["pending_intents"][0]["intent_snapshot"]["tell"] = "the guard waits"
    changed_snapshot["pending_intents"][0]["intent_fingerprint"] = content_fingerprint(
        changed_snapshot["pending_intents"][0]["intent_snapshot"]
    )
    with pytest.raises(NarrationTruthGateError, match="exact pending intent snapshot"):
        validate_narration_truth_contract(reseal(changed_snapshot))


async def test_local_first_intent_is_exact_for_nonstream_and_same_turn_streamed_swipe(
        client, proxy_app, mock_upstream, cfg, caplog):
    external_id = "deterministic-first-intent-wire"
    _sid, branch = _seed_known_opponent(proxy_app, cfg, external_id)
    cfg.server.turn_trace = True
    caplog.set_level(logging.INFO, logger="aetherstate.pipeline")

    normal = await client.post(
        "/v1/chat/completions",
        json=_request(external_id, gen_type="normal", stream=False),
    )
    assert normal.status_code == 200
    assert normal.headers["content-type"].startswith("application/json")
    normal_story = normal.json()["choices"][0]["message"]["content"]

    state = current_state(proxy_app.state.store, branch)
    intent = state["combat"]["pending_intent"]
    expected = f"Marshal Varo, twelve paces apart: {intent['tell']}"
    if not expected.endswith((".", "!", "?")):
        expected += "."
    expected += " The attack is committed; this is the moment to answer it before impact."
    assert normal_story == expected == compose.deterministic_first_intent_story(state)
    assert "Kael" not in normal_story
    assert mock_upstream.requests == []
    assert proxy_app.state.pipeline.cache.requests == 0
    assert proxy_app.state.pipeline.prewarm_doc(_sid) is None
    assert [
        (source, op.get("op"), op.get("contract_id"))
        for source, op in _journal_ops_at(proxy_app.state.store, branch, 1)
    ] == [
        ("rule", "semantic_meaning_commit", None),
        ("rule", "semantic_binding_commit", None),
        ("rule", "semantic_frame_commit", None),
        ("rule", "mechanic_settlement_commit", "combat_opening/1"),
        ("rule", "combatant_spawn", None),
        ("rule", "scene_set", None),
        ("rule", "clock_tick", None),
    ]

    async with client.stream(
        "POST", "/v1/chat/completions",
        json=_request(external_id, gen_type="swipe", stream=True),
    ) as swipe:
        swipe_raw = b"".join([chunk async for chunk in swipe.aiter_raw()])
    assert swipe.status_code == 200
    assert swipe.headers["content-type"].startswith("text/event-stream")
    assert swipe_raw.endswith(b"data: [DONE]\n\n")
    assert _response_text(swipe_raw, "text/event-stream") == normal_story
    assert mock_upstream.requests == []

    texts = proxy_app.state.store.get_turn_texts(branch, 1, 1)
    assert texts and texts[0]["assistant_text"] == f"Narrator: {normal_story}"
    turn = proxy_app.state.store.db.execute(
        "SELECT assistant_hash, extraction FROM turns WHERE branch_id=? AND turn_index=1",
        (branch,),
    ).fetchone()
    assert turn["assistant_hash"] == content_hash(normal_story)
    assert turn["extraction"] == "skipped"

    packet_payloads = []
    for record in caplog.records:
        if record.name == "aetherstate.pipeline" and record.message.startswith("TURN_TRACE "):
            payload = json.loads(record.message.removeprefix("TURN_TRACE "))
            if payload.get("event") == "packet":
                packet_payloads.append(payload)
    assert len(packet_payloads) == 2
    for payload in packet_payloads:
        assert payload["response_provenance"] == {
            "source": "local",
            "kind": compose.DETERMINISTIC_FIRST_INTENT_VERSION,
            "chars": len(normal_story),
            "sha256": hashlib.sha256(normal_story.encode("utf-8")).hexdigest(),
        }
        assert normal_story not in json.dumps(payload, ensure_ascii=False)


async def test_local_first_intent_does_not_mark_player_lesson_delivered(
        client, proxy_app, mock_upstream, cfg):
    external_id = "player-lessons-local-first-intent"
    session_id, _branch_id = _seed_known_opponent(proxy_app, cfg, external_id)
    service = proxy_app.state.pipeline.player_lessons_service
    lesson = service.create(
        effect_type="narration_behavior",
        title="AUDIT_LOCAL_REPLY_LESSON",
        scope="every_rpg_turn",
        do_text="AUDIT_USE_SENSORY_DETAIL",
        avoid_text="",
        anchor_entry_id=None,
    )

    response = await client.post(
        "/v1/chat/completions",
        json=_request(external_id, gen_type="normal", stream=False),
    )
    latest = service.latest_selections(session_id)

    assert response.status_code == 200
    assert mock_upstream.requests == []
    assert len(latest) == 1
    assert latest[0]["lesson_id"] == lesson["lesson_id"]
    assert latest[0]["delivered"] is False


async def test_network_duplicate_reuses_local_story_without_upstream_or_second_cold_path(
        client, proxy_app, mock_upstream, cfg):
    external_id = "deterministic-first-intent-duplicate"
    _sid, branch = _seed_known_opponent(proxy_app, cfg, external_id)
    request = _request(external_id, gen_type="normal", stream=False)

    first = await client.post("/v1/chat/completions", json=request)
    second = await client.post("/v1/chat/completions", json=request)
    first_story = first.json()["choices"][0]["message"]["content"]

    assert first.status_code == second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == first_story
    assert mock_upstream.requests == []
    assert len(proxy_app.state.pipeline._completed_responses) == 1
    texts = proxy_app.state.store.get_turn_texts(branch, 1, 1)
    assert texts and texts[0]["assistant_text"] == f"Narrator: {first_story}"


@pytest.mark.parametrize(
    "standalone",
    [
        {
            "op": "check", "skill": "perception", "char": "kael",
            "result": 8, "tier": "partial",
        },
        {
            "op": "master_tick", "char": "kael", "skill": "perception",
            "amount": 3,
        },
    ],
    ids=["check", "mastery"],
)
def test_local_opening_rejects_a_separate_same_turn_player_result(standalone: dict):
    cfg, store, session_id, branch, _state, _intent, stamp = _proof_opening(
        f"deterministic-first-intent-standalone-{standalone['op']}"
    )
    result = apply_delta(
        store, session_id, branch, 1, [deepcopy(standalone)], "rule", cfg
    )
    assert result.applied and not result.quarantined
    state = current_state(store, branch)

    assert not _deterministic_first_intent_story(
        store, _resolution(branch), state, cfg, stamp
    )


def test_opening_wrapper_cannot_borrow_another_contracts_valid_receipt_pair():
    _cfg, store, _sid, branch, state, intent, _stamp = _proof_opening(
        "deterministic-first-intent-other-contract"
    )
    journal = _journal_ops_at(store, branch, 1)
    wrapper = next(
        op for _source, op in journal if op.get("op") == "mechanic_settlement_commit"
    )
    skill_wrapper = next(
        op
        for row in _skill_case()["journal_rows"]
        for op in row["ops"]
        if op.get("op") == "mechanic_settlement_commit"
    )
    old_ref = wrapper["settlement_ref"]
    new_ref = skill_wrapper["settlement_ref"]
    wrapper["receipt"] = deepcopy(skill_wrapper["receipt"])
    wrapper["_store_row"] = deepcopy(skill_wrapper["_store_row"])
    wrapper["settlement_ref"] = new_ref
    wrapper["frame_ref"] = skill_wrapper["frame_ref"]
    wrapper["_semantic_frame_ref"] = skill_wrapper["frame_ref"]
    projections = []
    for _source, op in journal:
        if op.get("_settlement_ref") == old_ref:
            op["_settlement_ref"] = new_ref
            projections.append(op)
    state["mechanic_settlements"][0]["receipt"] = deepcopy(skill_wrapper["receipt"])
    private = state["_mechanic_settlement_projection_members"][0]
    private["settlement_ref"] = new_ref
    private["members"] = deepcopy(projections)

    assert wrapper["contract_id"] == "combat_opening/1"
    assert wrapper["receipt"]["contract_id"] == "skill_check/1"
    assert not _proof_bound_combat_opening_spawn(
        journal, state, 1, intent["actor"], intent
    )


def test_opening_requires_exact_turn_fingerprints_and_live_primary_actor():
    def fresh(tag: str):
        _cfg, store, _sid, branch, state, intent, _stamp = _proof_opening(tag)
        return _journal_ops_at(store, branch, 1), state, intent

    journal, state, intent = fresh("deterministic-first-intent-wrapper-turn")
    wrapper = next(
        op for _source, op in journal if op.get("op") == "mechanic_settlement_commit"
    )
    wrapper["_turn"] = 2
    assert not _proof_bound_combat_opening_spawn(
        journal, state, 1, intent["actor"], intent
    )

    journal, state, intent = fresh("deterministic-first-intent-blank-request")
    wrapper = next(
        op for _source, op in journal if op.get("op") == "mechanic_settlement_commit"
    )
    wrapper["_request_fingerprint"] = ""
    state["_mechanic_settlement_projection_members"][0]["request_fingerprint"] = ""
    assert not _proof_bound_combat_opening_spawn(
        journal, state, 1, intent["actor"], intent
    )

    for key in ("mechanic_settlements", "_mechanic_settlement_projection_members"):
        journal, state, intent = fresh(f"deterministic-first-intent-bool-{key}")
        state[key][0]["turn"] = True
        assert not _proof_bound_combat_opening_spawn(
            journal, state, 1, intent["actor"], intent
        )

    journal, state, intent = fresh("deterministic-first-intent-live-actor")
    state["combat"]["combatants"].pop(intent["actor"])
    assert not _proof_bound_combat_opening_spawn(
        journal, state, 1, intent["actor"], intent
    )


@pytest.mark.parametrize("turn_alias", [True, 1.0], ids=["bool", "float"])
def test_local_opening_rejects_turn_type_aliases(turn_alias):
    cfg, store, _sid, branch, state, _intent, stamp = _proof_opening(
        f"deterministic-first-intent-turn-alias-{type(turn_alias).__name__}"
    )
    assert not _deterministic_first_intent_story(
        store, _resolution(branch, turn=turn_alias), state, cfg, stamp
    )


def test_eligibility_rejects_stale_missing_changed_impact_and_disabled_config(
        proxy_app, cfg):
    external_id = "deterministic-first-intent-gates"
    _sid, branch, state = _stage_rule_opening(proxy_app, cfg, external_id)
    store = proxy_app.state.store
    stamp = Stamp(
        session=external_id, gen_type="normal", turn=1, speaker="Narrator",
        card_role="narrator", user="Kael",
    )
    assert _deterministic_first_intent_story(
        store, _resolution(branch), state, cfg, stamp
    )

    assert not _deterministic_first_intent_story(
        store, _resolution(branch, turn=2), state, cfg, stamp
    )
    cfg.specialization.war_room = False
    assert not _deterministic_first_intent_story(
        store, _resolution(branch), state, cfg, stamp
    )
    cfg.specialization.war_room = True
    cfg.specialization.enemy_rolls = False
    assert not _deterministic_first_intent_story(
        store, _resolution(branch), state, cfg, stamp
    )
    cfg.specialization.enemy_rolls = True

    row = store.db.execute(
        "SELECT id, ops FROM ops_journal WHERE branch_id=? AND turn_lo=1 AND turn_hi=1 "
        "AND source='rule'",
        (branch,),
    ).fetchone()
    journal_ops = json.loads(row["ops"])
    journal_ops[0]["_initial_intent"]["id"] = "intent_changed_after_spawn"
    with store.db:
        store.db.execute(
            "UPDATE ops_journal SET ops=? WHERE id=?", (json.dumps(journal_ops), row["id"])
        )
    assert not _deterministic_first_intent_story(
        store, _resolution(branch), state, cfg, stamp
    )

    store.checkpoint(branch, 1, state)
    with store.db:
        store.db.execute(
            "DELETE FROM ops_journal WHERE branch_id=? AND turn_lo=1 AND turn_hi=1",
            (branch,),
        )
    assert not _deterministic_first_intent_story(
        store, _resolution(branch), current_state(store, branch), cfg, stamp
    )

    impact_id = "deterministic-first-intent-impact"
    _sid2, branch2, state2 = _stage_rule_opening(proxy_app, cfg, impact_id)
    impact = apply_delta(store, _sid2, branch2, 1, [{
        "op": "combatant_hp", "target": "marshal_varo", "delta": 0,
    }], "rule", cfg)
    assert impact.applied
    impact_stamp = Stamp(
        session=impact_id, gen_type="normal", turn=1, speaker="Narrator",
        card_role="narrator", user="Kael",
    )
    assert not _deterministic_first_intent_story(
        store, _resolution(branch2), state2, cfg, impact_stamp
    )


def test_unrelated_distance_memory_is_omitted_from_local_story(proxy_app, cfg):
    external_id = "deterministic-first-intent-unrelated-distance"
    _sid, branch, state = _stage_rule_opening(
        proxy_app, cfg, external_id,
        memory_text="Opening scene: Ren and Orin stand three paces apart.",
    )
    stamp = Stamp(
        session=external_id, gen_type="normal", turn=1, speaker="Narrator",
        card_role="narrator", user="Kael",
    )
    story = _deterministic_first_intent_story(
        proxy_app.state.store, _resolution(branch), state, cfg, stamp
    )

    assert story == (
        f"Marshal Varo: {state['combat']['pending_intent']['tell']}. "
        "The attack is committed; this is the moment to answer it before impact."
    )
    assert "three paces" not in story


async def test_local_first_intent_failure_falls_open_to_upstream(
        client, proxy_app, mock_upstream, cfg, monkeypatch):
    external_id = "deterministic-first-intent-fail-open"
    _seed_known_opponent(proxy_app, cfg, external_id)
    upstream_story = "The upstream narrator remains available."
    mock_upstream.enqueue(Reply(body=json.dumps({
        "choices": [{"message": {"role": "assistant", "content": upstream_story}}],
    }).encode()))

    def _broken_local_story(_state):
        raise RuntimeError("synthetic local renderer failure")

    monkeypatch.setattr(compose, "deterministic_first_intent_story", _broken_local_story)
    response = await client.post(
        "/v1/chat/completions",
        json=_request(external_id, gen_type="normal", stream=False),
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == upstream_story
    assert len(mock_upstream.requests) == 1


async def test_none_specialization_never_uses_local_response(
        client, proxy_app, mock_upstream, cfg):
    external_id = "deterministic-first-intent-none"
    cfg.extraction.mode = "off"
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.enemy_rolls = True
    store = proxy_app.state.store
    sid, branch = store.create_session(external_id=external_id)
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {}, "resources": {"hp": {"max": 24}},
        }},
    ], "genesis", cfg)
    assert seeded.applied
    staged = apply_delta(store, sid, branch, 1, [{
        "op": "combatant_spawn", "name": "Gate Guard", "side": "enemy",
        "tier": "standard", "armament": "spear",
    }], "rule", cfg)
    assert staged.applied
    assert compose.deterministic_first_intent_story(current_state(store, branch))

    cfg.specialization.name = "none"
    upstream_story = "Ordinary upstream response."
    mock_upstream.enqueue(Reply(body=json.dumps({
        "choices": [{"message": {"role": "assistant", "content": upstream_story}}],
    }).encode()))
    response = await client.post(
        "/v1/chat/completions",
        json=_request(external_id, gen_type="normal", stream=False),
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == upstream_story
    assert len(mock_upstream.requests) == 1
