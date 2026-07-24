"""Independent-review regressions for accepted Chat lifecycle authority."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

from aetherstate import chat_continuity, compose, memory
from aetherstate.canon import CanonMsg, chain
from aetherstate.config import Config
from aetherstate.experience import config_for_experience
from aetherstate.extraction import (
    Endpoint,
    StateDelta,
    delta_json_schema_anyof,
    parse_and_validate,
)
from aetherstate.jobs import Batch, JobRunner
from aetherstate.state import current_state
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from tests.test_chat_card import STARTING_CONTINUITY
from tests.test_chat_mode_vertical import ORDINARY, PERSONA, WORLD


def _response(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def _stamp(identity: dict, *, session: str, gen_type: str = "normal") -> Stamp:
    return Stamp(
        session=session,
        turn=1,
        gen_type=gen_type,
        speaker="Mara",
        card_role="character",
        user="Bean",
        mode="chat",
        core_fingerprint=identity["core_fingerprint"],
        character_actor_id=identity["character_actor_id"],
        persona_actor_id=identity["persona_actor_id"],
    )


async def _admit_chat(client, session: str, *, continuity=None, world=None) -> dict:
    payload = {"card": ORDINARY, "persona": PERSONA}
    if continuity is not None:
        payload["continuity"] = continuity
    if world is not None:
        from aetherstate import chat_card

        payload["aetherstate"] = chat_card.build_card(
            chat_card.ordinary_core(ORDINARY),
            world=world,
            continuity=continuity,
        )["data"]["extensions"]["aetherstate"]
    response = await client.post(f"/aether/session/{session}/chat-core", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def test_late_abandoned_chat_candidate_cannot_publish_before_or_after_replacement(
    client,
    proxy_app,
):
    identity = await _admit_chat(client, "review-publication-cas")
    pipeline = proxy_app.state.pipeline
    store = proxy_app.state.store
    request = json.dumps({
        "messages": [{
            "role": "user",
            "content": "I promise that the rain key is safe.",
        }],
    }).encode()

    _first_packet, first = pipeline.process(
        _stamp(identity, session="review-publication-cas"),
        request,
    )
    assert first is not None
    _replacement_packet, replacement = pipeline.process(
        _stamp(identity, session="review-publication-cas", gen_type="swipe"),
        request,
    )
    assert replacement is not None
    assert first.expected_swipe_count + 1 == replacement.expected_swipe_count

    abandoned = "ABANDONED_LATE_CANDIDATE_5c27"
    pipeline.on_response(first, _response(abandoned), "application/json")
    turn = store.db.execute(
        "SELECT t.*, x.assistant_text FROM turns t"
        " LEFT JOIN turn_texts x USING(branch_id, turn_index)"
        " WHERE t.branch_id=? AND t.turn_index=?",
        (first.branch_id, first.turn_index),
    ).fetchone()
    assert not turn["assistant_text"]
    assert not turn["assistant_hash"]
    assert not turn["accepted_response_occurrence_id"]
    assert store.experience_binding(first.session_id).locked is False
    assert store.claim_records(first.branch_id) == []

    accepted = "Mara promised that the ambulance is parked outside."
    pipeline.on_response(replacement, _response(accepted), "application/json")
    accepted_turn = store.db.execute(
        "SELECT t.*, x.assistant_text FROM turns t"
        " JOIN turn_texts x USING(branch_id, turn_index)"
        " WHERE t.branch_id=? AND t.turn_index=?",
        (replacement.branch_id, replacement.turn_index),
    ).fetchone()
    accepted_id = accepted_turn["accepted_response_occurrence_id"]
    assert accepted in accepted_turn["assistant_text"]
    assert accepted_id

    pipeline.on_response(first, _response(abandoned), "application/json")
    final_turn = store.db.execute(
        "SELECT t.*, x.assistant_text FROM turns t"
        " JOIN turn_texts x USING(branch_id, turn_index)"
        " WHERE t.branch_id=? AND t.turn_index=?",
        (replacement.branch_id, replacement.turn_index),
    ).fetchone()
    assert accepted in final_turn["assistant_text"]
    assert abandoned not in final_turn["assistant_text"]
    assert final_turn["accepted_response_occurrence_id"] == accepted_id
    assert abandoned not in json.dumps(store.claim_records(first.branch_id))

    user_journals = store.db.execute(
        "SELECT id FROM ops_journal WHERE branch_id=?"
        " AND lifecycle_source='user_text'",
        (first.branch_id,),
    ).fetchall()
    assert len(user_journals) == 1
    assert {
        json.loads(row["record_json"])["source"]
        for row in store.db.execute(
            "SELECT record_json, lifecycle_source FROM claim_records"
            " WHERE branch_id=?",
            (first.branch_id,),
        ).fetchall()
        if row["lifecycle_source"] == "user_text"
    } == {identity["persona_actor_id"]}


async def test_replacement_reuses_one_durable_user_text_recognition_receipt(
    client,
    proxy_app,
):
    identity = await _admit_chat(client, "review-user-idempotency")
    pipeline = proxy_app.state.pipeline
    store = proxy_app.state.store
    request = json.dumps({
        "messages": [{
            "role": "user",
            "content": "I promise that the rain key is safe.",
        }],
    }).encode()
    _packet, first = pipeline.process(
        _stamp(identity, session="review-user-idempotency"),
        request,
    )
    assert first is not None
    pipeline.on_response(
        first,
        _response("Mara promised that the ambulance is outside."),
        "application/json",
    )
    _swipe_packet, swipe = pipeline.process(
        _stamp(identity, session="review-user-idempotency", gen_type="swipe"),
        request,
    )
    assert swipe is not None
    pipeline.on_response(
        swipe,
        _response("Mara promised that the ambulance remains outside."),
        "application/json",
    )
    rows = store.db.execute(
        "SELECT id FROM ops_journal WHERE branch_id=?"
        " AND lifecycle_source='user_text'",
        (first.branch_id,),
    ).fetchall()
    receipts = store.db.execute(
        "SELECT * FROM chat_user_text_receipts WHERE branch_id=?",
        (first.branch_id,),
    ).fetchall()
    assert len(rows) == 1
    assert len(receipts) == 1
    assert receipts[0]["journal_op_id"] == rows[0]["id"]
    assert {
        json.loads(row["record_json"])["source"]
        for row in store.db.execute(
            "SELECT record_json, lifecycle_source FROM claim_records"
            " WHERE branch_id=?",
            (first.branch_id,),
        ).fetchall()
        if row["lifecycle_source"] == "user_text"
    } == {identity["persona_actor_id"]}


def _proposal(
    text: str,
    *,
    action: str,
    subject: str,
    participant: str,
    source: str = "assistant_text",
    start: int = 0,
    end: int | None = None,
    voluntariness: list | None = None,
    consent: list | None = None,
    disclosure: list | None = None,
) -> dict:
    return {
        "source_span": {
            "source": source,
            "start": start,
            "end": len(text) if end is None else end,
        },
        "subject_actor_id": subject,
        "action_code": action,
        "polarity": "positive",
        "modality": "actual",
        "participants": [{"kind": "actor", "actor_id": participant}],
        "voluntariness": voluntariness or [],
        "consent": consent or [],
        "disclosure": disclosure or [],
        "motive_claim_ref": None,
    }


def _seal(text: str, proposal: dict) -> dict[str, list[dict]]:
    return chat_continuity.seal_accepted_chat_proposals(
        user_text="",
        assistant_text=text,
        proposals=[proposal],
        session_id="session:review",
        branch_id="branch:review",
        turn_index=4,
        character_actor_id="character:review",
        persona_actor_id="persona:review",
        response_occurrence_id="response:" + "a" * 64,
    )


def test_social_gate_requires_exact_closed_action_and_speech_frames():
    wave = "I wave."
    forged = _seal(
        wave,
        _proposal(
            wave,
            action="sexual_contact",
            subject="character:review",
            participant="persona:review",
        ),
    )
    assert forged["assistant_response"] == []

    kiss = "I kiss you."
    romantic = _seal(
        kiss,
        _proposal(
            kiss,
            action="romantic_contact",
            subject="character:review",
            participant="persona:review",
        ),
    )
    assert [
        op["record"]["act"] for op in romantic["assistant_response"]
        if op["op"] == "social_occurrence_admit"
    ] == ["romantic_contact"]
    forged_counterpart = _proposal(
        kiss,
        action="romantic_contact",
        subject="character:review",
        participant="persona:review",
    )
    forged_counterpart["participants"] = [{
        "kind": "person",
        "person_id": "person:casey",
        "label": "Casey",
    }]
    assert _seal(kiss, forged_counterpart)["assistant_response"] == []

    named = "I kiss Casey."
    named_proposal = _proposal(
        named,
        action="romantic_contact",
        subject="character:review",
        participant="persona:review",
    )
    named_proposal["participants"] = [{
        "kind": "person",
        "person_id": "person:model-supplied",
        "label": "Casey",
    }]
    named_sealed = _seal(named, named_proposal)
    assert named_sealed["assistant_response"] == []

    promise = "I promise you that I will call after shift."
    promised = _seal(
        promise,
        _proposal(
            promise,
            action="promise_make",
            subject="character:review",
            participant="persona:review",
        ),
    )
    assert any(
        op["op"] == "social_occurrence_admit"
        and op["record"]["act"] == "promise_make"
        for op in promised["assistant_response"]
    )
    assert any(
        op["op"] == "continuity_thread_transition"
        and op["record"]["kind"] == "promise"
        and op["record"]["status"] == "open"
        for op in promised["assistant_response"]
    )

    arbitrary = "I discuss our plans."
    unsupported = _seal(
        arbitrary,
        _proposal(
            arbitrary,
            action="promise_make",
            subject="character:review",
            participant="persona:review",
        ),
    )
    assert unsupported["assistant_response"] == []


def test_non_unknown_social_subclaims_need_individually_matching_spans():
    text = "I kiss you. I chose this voluntarily. I consent to this."
    action_end = text.index(".") + 1
    voluntary_start = text.index("I chose")
    voluntary_end = text.index(".", voluntary_start) + 1
    consent_start = text.index("I consent")
    consent_end = len(text)
    proposal = _proposal(
        text,
        action="romantic_contact",
        subject="character:review",
        participant="persona:review",
        end=action_end,
        voluntariness=[{
            "participant": {"kind": "actor", "actor_id": "character:review"},
            "status": "voluntary",
            "source_span": {
                "source": "assistant_text",
                "start": voluntary_start,
                "end": voluntary_end,
            },
        }],
        consent=[{
            "participant": {"kind": "actor", "actor_id": "character:review"},
            "act": "romantic_contact",
            "status": "granted",
            "channel": "in_fiction",
            "source_span": {
                "source": "assistant_text",
                "start": consent_start,
                "end": consent_end,
            },
        }],
    )
    sealed = _seal(text, proposal)
    record = next(
        op["record"] for op in sealed["assistant_response"]
        if op["op"] == "social_occurrence_admit"
    )
    assert record["voluntariness"][0]["status"] == "voluntary"
    assert record["voluntariness"][0]["evidence"]["start"] == voluntary_start
    assert record["consent"][0]["status"] == "granted"
    assert record["consent"][0]["evidence"]["start"] == consent_start
    assert (
        record["voluntariness"][0]["evidence"]["fingerprint"]
        != record["consent"][0]["evidence"]["fingerprint"]
    )

    forged = copy.deepcopy(proposal)
    forged["consent"][0]["source_span"] = {
        "source": "assistant_text",
        "start": voluntary_start,
        "end": voluntary_end,
    }
    assert _seal(text, forged)["assistant_response"] == []

    forged_participant = copy.deepcopy(proposal)
    forged_participant["consent"][0]["participant"] = {
        "kind": "actor",
        "actor_id": "persona:review",
    }
    assert _seal(text, forged_participant)["assistant_response"] == []


async def test_real_chat_extraction_opens_but_self_report_cannot_resolve_promise(
    client,
    proxy_app,
):
    identity = await _admit_chat(client, "review-promise-ingress")
    store = proxy_app.state.store
    pipeline = proxy_app.state.pipeline
    pipeline.jobs = None
    request = json.dumps({
        "messages": [{"role": "user", "content": "Will you call me?"}],
    }).encode()

    class PromiseLadder:
        request_local_config = True

        @staticmethod
        def get_client():
            return None

        async def extract(
            self,
            _endpoint,
            _snapshot,
            _characters,
            lo,
            hi,
            exchange,
            **_kwargs,
        ):
            assistant_text = exchange.splitlines()[-1]
            evidence = assistant_text.split(": ", 1)[-1]
            action = (
                "promise_fulfill"
                if "kept my promise" in evidence
                else "promise_make"
            )
            return StateDelta.model_validate({
                "schema": "aetherstate/delta/2",
                "turn_range": [lo, hi],
                "ops": [],
                "social_occurrence_proposals": [_proposal(
                    assistant_text,
                    action=action,
                    subject=identity["character_actor_id"],
                    participant=identity["persona_actor_id"],
                    start=assistant_text.index(evidence),
                )],
            })

    jobs = JobRunner(store, pipeline.cfg, PromiseLadder())
    _packet, first = pipeline.process(
        _stamp(identity, session="review-promise-ingress"),
        request,
    )
    assert first is not None
    pipeline.on_response(
        first,
        _response("I promise you that I will call after shift."),
        "application/json",
    )
    await jobs._run_batch(
        Batch(
            first.session_id,
            first.branch_id,
            first.turn_index,
            first.turn_index,
            first.turn_index,
        ),
        Endpoint(base_url="http://example.test", model="chat-review"),
    )
    opened = current_state(store, first.branch_id)["continuity_threads"]
    occurrences = current_state(store, first.branch_id)["social_occurrences"]
    assert occurrences
    occurrence = next(iter(occurrences.values()))[-1]
    assert occurrence["lifecycle_source"] == "assistant_response"
    assert occurrence["response_occurrence_id"]
    assert len(opened) == 1
    thread_id, revisions = next(iter(opened.items()))
    assert revisions[-1]["status"] == "open"
    assert revisions[-1]["lifecycle_source"] == "assistant_response"

    second_stamp = replace(
        _stamp(identity, session="review-promise-ingress"),
        turn=2,
    )
    _packet2, second = pipeline.process(second_stamp, request)
    assert second is not None
    pipeline.on_response(
        second,
        _response("I kept my promise."),
        "application/json",
    )
    await jobs._run_batch(
        Batch(
            second.session_id,
            second.branch_id,
            second.turn_index,
            second.turn_index,
            second.turn_index,
        ),
        Endpoint(base_url="http://example.test", model="chat-review"),
    )
    transitioned = current_state(store, second.branch_id)[
        "continuity_threads"
    ][thread_id]
    assert [row["status"] for row in transitioned] == ["open"]


def test_chat_memory_wire_preserves_scope_but_code_owns_final_audience():
    schema = delta_json_schema_anyof(chat=True)["schema"]
    memory_branch = next(
        branch
        for branch in schema["properties"]["ops"]["items"]["anyOf"]
        if branch["properties"]["op"].get("enum") == ["memory_event"]
    )
    assert {"visibility", "scoped_actors"} <= set(memory_branch["properties"])

    raw = json.dumps({
        "schema": "aetherstate/delta/2",
        "turn_range": [1, 1],
        "ops": [{
            "op": "memory_event",
            "text": "Mara privately remembered the locked drawer.",
            "participants": ["persona:review"],
            "importance": 7,
            "tags": ["private"],
            "visibility": "public",
            "scoped_actors": ["persona:forged"],
        }],
        "social_occurrence_proposals": [],
    })
    parsed = parse_and_validate(raw)
    assert parsed is not None
    assert parsed.ops[0]["visibility"] == "public"
    assert parsed.ops[0]["scoped_actors"] == ["persona:forged"]
    scoped = chat_continuity.scope_chat_memory_ops(
        parsed.ops,
        character_actor_id="character:review",
        persona_actor_id="persona:review",
    )
    assert scoped[0]["visibility"] == "actor_scoped"
    assert scoped[0]["scoped_actors"] == ["character:review"]
    assert scoped[0]["participants"] == [
        "character:review",
        "persona:review",
    ]


def test_recall_is_branch_response_and_source_provenance_bound():
    store = Store(":memory:")
    source_session, source_branch = store.create_session(external_id="recall-source")
    canonical = CanonMsg("user", "", "0000000000000001")
    store.append_msgs(
        source_branch,
        0,
        [(canonical.role, canonical.content_hash, chain([canonical])[0])],
    )
    store.record_turn(source_branch, 0, "new_session", "normal")
    store.write_turn_hashes(source_branch, 0, user_hash=canonical.content_hash)
    store.write_turn_text(
        source_branch,
        0,
        user_text="The key?",
    )
    response_id = "response:" + "b" * 64
    assistant_text = "Mara: I remember the key."
    assert store.publish_chat_response_if_current(
        source_branch,
        0,
        expected_swipe_count=0,
        assistant_text=assistant_text,
        assistant_hash="sha256:" + hashlib.sha256(
            assistant_text.encode("utf-8"),
        ).hexdigest(),
        accepted_response_occurrence_id=response_id,
    )
    source_journal_id = store.journal(
        source_branch,
        0,
        0,
        [{"op": "memory_event", "text": "I remember the key."}],
        "extraction",
        lifecycle_source="deferred_extraction",
        response_occurrence_id=response_id,
    )
    source_journal_ref = f"{source_journal_id}:0"
    store.write_recall(
        source_session,
        1,
        ["- I remember the key. (just now)"],
        branch_id=source_branch,
        source_turn=0,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=response_id,
        source_message_fingerprint=store.turn_text_source_fingerprint(
            source_branch,
            0,
            "deferred_extraction",
        ),
        journal_op_refs=[source_journal_ref],
    )
    abandoned_reflection = "ABANDONED_REFLECTION_4bc1"
    store.memories_add(
        source_session,
        source_branch,
        tier="summary",
        text=abandoned_reflection,
        participants=[],
        location_id=None,
        tags=["reflection"],
        importance=5,
        created_turn=0,
        scene_index=0,
        visibility="actor_scoped",
        scoped_actors=["character:review"],
        lifecycle_source="deferred_extraction",
        response_occurrence_id=response_id,
        source_message_fingerprint=store.turn_text_source_fingerprint(
            source_branch,
            0,
            "deferred_extraction",
        ),
        source_journal_op_refs=[source_journal_ref],
    )
    assert store.read_recall(
        source_session,
        branch_id=source_branch,
        for_turn=1,
        experience_mode="chat",
    ) == ["- I remember the key. (just now)"]

    child_session, empty_child = store.create_session(external_id="recall-child")
    child_branch = store.fork_branch(
        source_branch,
        1,
        0,
        new_session_id=child_session,
        discard_empty_branch=empty_child,
    )
    sibling_session, empty_sibling = store.create_session(external_id="recall-sibling")
    sibling_branch = store.fork_branch(
        source_branch,
        1,
        0,
        new_session_id=sibling_session,
        discard_empty_branch=empty_sibling,
    )
    assert store.read_recall(
        child_session,
        branch_id=child_branch,
        for_turn=1,
        experience_mode="chat",
    )
    assert store.read_recall(
        sibling_session,
        branch_id=sibling_branch,
        for_turn=1,
        experience_mode="chat",
    )

    child_journal_id = store.journal(
        child_branch,
        0,
        0,
        [{"op": "memory_event", "text": "CHILD ONLY"}],
        "rule",
        lifecycle_source="user_text",
    )
    child_journal_ref = f"{child_journal_id}:0"
    store.write_recall(
        child_session,
        1,
        ["- CHILD ONLY"],
        branch_id=child_branch,
        source_turn=0,
        lifecycle_source="user_text",
        source_message_fingerprint=store.turn_text_source_fingerprint(
            child_branch,
            0,
            "user_text",
        ),
        journal_op_refs=[child_journal_ref],
    )
    assert "CHILD ONLY" in " ".join(store.read_recall(
        child_session,
        branch_id=child_branch,
        for_turn=1,
        experience_mode="chat",
    ))
    assert "CHILD ONLY" not in " ".join(store.read_recall(
        sibling_session,
        branch_id=sibling_branch,
        for_turn=1,
        experience_mode="chat",
    ))

    store.retract_chat_response_at(source_branch, 0)
    assert store.read_recall(
        source_session,
        branch_id=source_branch,
        for_turn=1,
        experience_mode="chat",
    ) == []
    assert abandoned_reflection not in {
        row["text"] for row in store.memories_candidates(source_branch)
    }

    replacement_id = "response:" + "c" * 64
    replacement_text = "Mara: I remember the replacement key."
    assert store.publish_chat_response_if_current(
        source_branch,
        0,
        expected_swipe_count=0,
        assistant_text=replacement_text,
        assistant_hash="replacement-hash",
        accepted_response_occurrence_id=replacement_id,
    )
    replacement_journal_id = store.journal(
        source_branch,
        0,
        0,
        [{"op": "memory_event", "text": "replacement key"}],
        "extraction",
        lifecycle_source="deferred_extraction",
        response_occurrence_id=replacement_id,
    )
    replacement_journal_ref = f"{replacement_journal_id}:0"
    store.write_recall(
        source_session,
        1,
        ["- I remember the replacement key. (just now)"],
        branch_id=source_branch,
        source_turn=0,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=replacement_id,
        source_message_fingerprint=store.turn_text_source_fingerprint(
            source_branch,
            0,
            "deferred_extraction",
        ),
        journal_op_refs=[replacement_journal_ref],
    )
    assert store.read_recall(
        source_session,
        branch_id=source_branch,
        for_turn=1,
        experience_mode="chat",
    ) == ["- I remember the replacement key. (just now)"]
    assert store.read_recall(
        child_session,
        branch_id=child_branch,
        for_turn=1,
        experience_mode="chat",
    )


async def test_starting_observables_world_and_lifecycle_provenance_are_structured(
    client,
    proxy_app,
):
    private_world = "CREATOR_PRIVATE_DIRECTION_82bf"
    public_world = "Rainline ferries stop after midnight."
    continuity = {
        "schema": chat_continuity.STARTING_CONTINUITY_SCHEMA,
        "agreement_revisions": [
            copy.deepcopy(STARTING_CONTINUITY["agreement_revisions"][0]),
        ],
        "player_visible_possessions_conditions": [{
            "record_id": "possession.rain-key",
            "kind": "possession",
            "summary": "Mara carries a silver rain key.",
        }],
        "open_threads": [{
            "schema": chat_continuity.THREAD_TRANSITION_SCHEMA,
            "thread_id": "thread.last-ferry",
            "revision": 1,
            "action": "create",
            "kind": "plan",
            "summary": "Catch the last ferry.",
            "participants": [
                {"role": "character"},
                {"role": "current_persona"},
            ],
            "status": "open",
        }],
    }
    identity = await _admit_chat(
        client,
        "review-starting-observables",
        continuity=continuity,
        world={
            **WORLD,
            "setting": public_world,
            "creator_private_direction": private_world,
        },
    )
    assert all(
        row["accepted"] for row in identity["continuity_results"]
    ), identity["continuity_results"]
    store = proxy_app.state.store
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='review-starting-observables'",
    ).fetchone()
    state = current_state(store, session["active_branch"])
    actor_attributes = state["attributes"][identity["character_actor_id"]]
    observable = next(
        value
        for key, value in actor_attributes.items()
        if key.startswith("chat_observable.")
    )
    assert observable == {
        "kind": "possession",
        "summary": "Mara carries a silver rain key.",
        "visibility": "actor_scoped",
        "scoped_actors": sorted([
            identity["character_actor_id"],
            identity["persona_actor_id"],
        ]),
    }
    assert not any(
        row["text"] == "Mara carries a silver rain key."
        for row in state["memories"]
    )

    packet = compose.render_chat_packet(
        state,
        store.experience_binding(session["session_id"]),
        config_for_experience(proxy_app.state.pipeline.cfg, "chat"),
    )
    assert "[OBSERVABLE CHARACTER STATE]" in packet
    assert "silver rain key" in packet
    assert public_world in packet
    assert private_world not in packet

    thread = state["continuity_threads"]["thread.last-ferry"][0]
    assert thread["lifecycle_source"] == "creator_starting_continuity"
    assert thread["response_occurrence_id"] == ""
    agreement = next(iter(state["relationship_agreements"].values()))[0]
    assert agreement["lifecycle_source"] == "creator_starting_continuity"
    assert agreement["response_occurrence_id"] == ""
    receipts = store.chat_continuity_seed_receipts(session["session_id"])
    assert receipts
    assert {row["lifecycle_source"] for row in receipts} == {
        "creator_starting_continuity"
    }
    assert {row["response_occurrence_id"] for row in receipts} == {""}

    index_names = {
        row["name"]
        for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'",
        ).fetchall()
    }
    assert {
        "idx_effect_receipts_lifecycle",
        "idx_mechanic_settlement_receipts_lifecycle",
        "idx_world_event_records_lifecycle",
        "idx_chat_continuity_seed_receipts_lifecycle",
    } <= index_names


def test_job_notify_resolves_session_config_when_caller_omits_it(monkeypatch):
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="request-local-job")
    store.experience_mode_set_unlocked(session_id, "chat")
    global_cfg = Config()
    global_cfg.extraction.mode = "off"
    request_cfg = config_for_experience(global_cfg, "chat")
    request_cfg.extraction.mode = "assist"
    runner = JobRunner(store, global_cfg, ladder=None)
    monkeypatch.setattr(runner, "_config_for_session", lambda _sid: request_cfg)
    armed: list[tuple] = []
    monkeypatch.setattr(
        runner,
        "_arm_debounce",
        lambda *args, **kwargs: armed.append((args, kwargs)),
    )

    runner.notify(session_id, branch_id, 0)

    assert armed
    assert runner._batch_context(session_id)[1] is request_cfg


def test_legacy_blank_memory_stays_rpg_visible_and_chat_excluded(cfg):
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="legacy-audience")
    store.memories_add(
        session_id,
        branch_id,
        tier="episodic",
        text="LEGACY_BLANK_AUDIENCE_0e91",
        participants=[],
        location_id=None,
        tags=[],
        importance=9,
        created_turn=0,
        scene_index=0,
        visibility="",
        scoped_actors=[],
    )
    assert memory.retrieve(
        store,
        cfg,
        branch_id,
        {},
        "legacy blank audience",
        1,
        experience_mode="rpg",
    )
    assert memory.retrieve(
        store,
        cfg,
        branch_id,
        {},
        "legacy blank audience",
        1,
        viewer_actor_id="character:review",
        player_actor_id="persona:review",
        experience_mode="chat",
    ) == []
