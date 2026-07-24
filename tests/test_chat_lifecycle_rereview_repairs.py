from __future__ import annotations

import hashlib
import json

import httpx

from aetherstate import chat_continuity, memory
from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.extraction import Endpoint
from aetherstate.jobs import Batch, JobRunner
from aetherstate.knowledge import record_visible_to
from aetherstate.state import (
    apply_delta,
    apply_partitioned_delta,
    current_state,
)
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from tests.mock_upstream import Reply
from tests.test_chat_mode_vertical import ORDINARY, PERSONA as PERSONA_CARD


CHARACTER = "character:review"
PERSONA = "persona:review"
RESPONSE = "response:" + "d" * 64


def _proposal(
    message: str,
    evidence: str,
    *,
    action: str = "romantic_contact",
    source: str = "assistant_text",
    subject: str = CHARACTER,
    participant: str = PERSONA,
) -> dict:
    start = message.index(evidence)
    return {
        "source_span": {
            "source": source,
            "start": start,
            "end": start + len(evidence),
        },
        "subject_actor_id": subject,
        "action_code": action,
        "polarity": "positive",
        "modality": "actual",
        "participants": [{
            "kind": "actor",
            "actor_id": participant,
        }],
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
        "motive_claim_ref": None,
    }


def _seal(message: str, proposal: dict) -> dict[str, list[dict]]:
    return chat_continuity.seal_accepted_chat_proposals(
        user_text="",
        assistant_text=message,
        proposals=[proposal],
        session_id="session:review",
        branch_id="branch:review",
        turn_index=7,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=RESPONSE,
        character_display_name="Mara",
    )


def _occurrences(sealed: dict[str, list[dict]]) -> list[dict]:
    return [
        op["record"]
        for op in sealed["assistant_response"]
        if op.get("op") == "social_occurrence_admit"
    ]


def test_social_frame_uses_complete_message_attribution_and_code_owned_segment():
    positive = "Mara: I kiss you."
    admitted = _occurrences(
        _seal(positive, _proposal(positive, "I kiss you."))
    )
    assert len(admitted) == 1
    assert admitted[0]["source_segment"] == "shared_observation"

    negatives = (
        'Mara: Mara imagined saying, "I kiss you."',
        'Mara: Casey: I kiss you.',
        'Mara: Casey accused Mara: "I kiss you."',
        'Mara: If you stayed, I kiss you.',
        'Mara: I said I kiss you.',
    )
    for message in negatives:
        assert _occurrences(
            _seal(message, _proposal(message, "I kiss you."))
        ) == [], message


def test_social_wire_contract_drops_model_segment_and_names_full_closed_vocabulary():
    from aetherstate.extraction import (
        SOCIAL_ACTION_CODES,
        delta_json_schema,
    )

    proposal = delta_json_schema(chat=True)["schema"]["properties"][
        "social_occurrence_proposals"
    ]["items"]
    assert "source_segment" not in proposal["properties"]
    assert "source_segment" not in proposal["required"]
    assert set(
        proposal["properties"]["action_code"]["enum"]
    ) == set(SOCIAL_ACTION_CODES)
    assert {
        "agreement_create",
        "agreement_amend",
        "agreement_withdraw",
        "agreement_release",
        "agreement_end",
        "promise_make",
        "promise_fulfill",
        "promise_violate",
        "promise_withdraw",
        "promise_release",
        "thread_resolve",
        "disclosure",
    } <= set(SOCIAL_ACTION_CODES)


def _model_content(payload: dict) -> bytes:
    return json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps(payload),
            },
        }],
    }).encode()


def _wire_proposal(
    text: str,
    evidence: str,
    identity: dict,
    *,
    action: str,
    source: str = "assistant_text",
    subject: str | None = None,
    participant: str | None = None,
    search_start: int = 0,
    motive_claim_ref: dict | None = None,
) -> dict:
    start = text.index(evidence, search_start)
    return {
        "source_span": {
            "source": source,
            "start": start,
            "end": start + len(evidence),
        },
        "subject_actor_id": subject or identity["character_actor_id"],
        "action_code": action,
        "polarity": "positive",
        "modality": "actual",
        "participants": [{
            "kind": "actor",
            "actor_id": participant or identity["persona_actor_id"],
        }],
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
        "motive_claim_ref": motive_claim_ref,
    }


async def _run_real_chat_extraction_turn(
    *,
    client,
    proxy_app,
    mock_upstream,
    session_name: str,
    turn: int,
    user_text: str,
    assistant_text: str,
    model_delta_factory,
    identity: dict | None = None,
    messages: list[dict] | None = None,
    before_batch=None,
):
    if identity is None:
        admitted_response = await client.post(
            f"/aether/session/{session_name}/chat-core",
            json={"card": ORDINARY, "persona": PERSONA_CARD},
        )
        assert admitted_response.status_code == 200
        identity = admitted_response.json()
    store = proxy_app.state.store
    pipeline = proxy_app.state.pipeline
    pipeline.jobs = None
    request = json.dumps({
        "messages": messages or [{"role": "user", "content": user_text}],
    }).encode()
    _packet, context = pipeline.process(
        Stamp(
            session=session_name,
            turn=turn,
            gen_type="normal",
            speaker="Mara",
            card_role="character",
            user="Bean",
            mode="chat",
            core_fingerprint=identity["core_fingerprint"],
            character_actor_id=identity["character_actor_id"],
            persona_actor_id=identity["persona_actor_id"],
        ),
        request,
    )
    assert context is not None
    pipeline.on_response(
        context,
        json.dumps({
            "choices": [{
                "message": {"content": assistant_text},
            }],
        }).encode(),
        "application/json",
    )
    stored = store.get_turn_texts(
        context.branch_id,
        context.turn_index,
        context.turn_index,
    )[0]
    model_delta = model_delta_factory(identity, stored, context, store)
    mock_upstream.enqueue(Reply(body=_model_content(model_delta)))
    pipeline.cfg.upstream.force_rung = 2
    pipeline.cfg.extraction.use_anyof = False
    store.caps_set(
        "http://mock-upstream/v1",
        "chat-authority-matrix",
        2,
        anyof=0,
    )
    if before_batch is not None:
        before_batch(store, context)
    runner = JobRunner(store, pipeline.cfg, proxy_app.state.jobs.ladder)
    await runner._run_batch(
        Batch(
            context.session_id,
            context.branch_id,
            context.turn_index,
            context.turn_index,
            context.turn_index,
        ),
        Endpoint(
            base_url="http://mock-upstream/v1",
            model="chat-authority-matrix",
        ),
    )
    return identity, context, stored


async def test_real_ingress_context_target_disclosure_and_motive_authority(
    client,
    proxy_app,
    mock_upstream,
):
    assistant_text = (
        "I kiss you, provided that you agree. "
        "I tell Persona that the rain key is under the blue cup. "
        "I hug Persona. "
        "I asserted that I hugged Persona because I wanted reassurance."
    )

    def model_delta(identity, stored, context, store):
        accepted = str(stored["assistant_text"])
        claims = [
            row for row in store.claim_records(
                context.branch_id,
            )
            if "because I wanted reassurance" in str(
                (row.get("frame") or {}).get("proposition") or ""
            )
        ]
        assert len(claims) == 1
        motive_ref = {
            "claim_id": claims[0]["claim_id"],
            "fingerprint": claims[0]["fingerprint"],
        }
        return {
            "schema": "aetherstate/delta/2",
            "turn_range": [
                int(stored["turn_index"]),
                int(stored["turn_index"]),
            ],
            "ops": [],
            "social_occurrence_proposals": [
                    _wire_proposal(
                        accepted,
                        "I kiss you",
                        identity,
                        action="romantic_contact",
                    ),
                _wire_proposal(
                    accepted,
                    "I tell Persona that the rain key is under the blue cup.",
                    identity,
                    action="disclosure",
                ),
                _wire_proposal(
                    accepted,
                    "I hug Persona.",
                    identity,
                    action="romantic_contact",
                    motive_claim_ref=motive_ref,
                ),
            ],
        }

    identity, context, _stored = await _run_real_chat_extraction_turn(
        client=client,
        proxy_app=proxy_app,
        mock_upstream=mock_upstream,
        session_name="production-authority-context",
        turn=1,
        user_text="Tell me what happened.",
        assistant_text=assistant_text,
        model_delta_factory=model_delta,
    )
    state = current_state(proxy_app.state.store, context.branch_id)
    occurrences = [
        row
        for revisions in state["social_occurrences"].values()
        for row in revisions
    ]
    assert len(occurrences) == 2
    assert {row["summary"] for row in occurrences} == {
        "I tell Persona that the rain key is under the blue cup.",
        "I hug Persona.",
    }
    hug = next(row for row in occurrences if row["summary"] == "I hug Persona.")
    assert hug["outside_participants"] == [{
        "kind": "actor",
        "actor_id": identity["persona_actor_id"],
    }]
    assert hug["motive_claim_ref"] is not None
    assert any(
        belief["holder"] == identity["persona_actor_id"]
        and belief["stance"] == "was_told"
        and belief["source"] == "disclosed"
        for belief in state["beliefs"].values()
    )


async def test_real_ingress_bilateral_agreement_authority(
    client,
    proxy_app,
    mock_upstream,
):
    proposal_text = "I propose to you that we be exclusive."

    def proposal_delta(identity, stored, _context, _store):
        accepted = str(stored["assistant_text"])
        return {
            "schema": "aetherstate/delta/2",
            "turn_range": [
                int(stored["turn_index"]),
                int(stored["turn_index"]),
            ],
            "ops": [],
            "social_occurrence_proposals": [
                _wire_proposal(
                    accepted,
                    proposal_text,
                    identity,
                    action="agreement_create",
                ),
            ],
        }

    identity, first_context, _stored = await _run_real_chat_extraction_turn(
        client=client,
        proxy_app=proxy_app,
        mock_upstream=mock_upstream,
        session_name="production-authority-agreement",
        turn=1,
        user_text="What relationship do you want?",
        assistant_text=proposal_text,
        model_delta_factory=proposal_delta,
    )
    pending_state = current_state(proxy_app.state.store, first_context.branch_id)
    pending = next(iter(pending_state["relationship_agreements"].values()))[-1]
    assert [row["status"] for row in pending["assent"]] == ["proposed"]

    acceptance = "I accept your proposal that we be exclusive."

    def acceptance_delta(identity, stored, _context, _store):
        source = str(stored["user_text"])
        return {
            "schema": "aetherstate/delta/2",
            "turn_range": [
                int(stored["turn_index"]),
                int(stored["turn_index"]),
            ],
            "ops": [],
            "social_occurrence_proposals": [
                _wire_proposal(
                    source,
                    acceptance,
                    identity,
                    action="agreement_create",
                    source="user_text",
                    subject=identity["persona_actor_id"],
                    participant=identity["character_actor_id"],
                ),
            ],
        }

    _identity, second_context, second_stored = await _run_real_chat_extraction_turn(
        client=client,
        proxy_app=proxy_app,
        mock_upstream=mock_upstream,
        session_name="production-authority-agreement",
        turn=2,
        user_text=acceptance,
        assistant_text="I understand.",
        model_delta_factory=acceptance_delta,
        identity=identity,
        messages=[
            {
                "role": "user",
                "content": "What relationship do you want?",
            },
            {"role": "assistant", "content": proposal_text},
            {"role": "user", "content": acceptance},
        ],
    )
    assert second_context.branch_id == first_context.branch_id
    assert proxy_app.state.store.extraction_range_is(
        second_context.branch_id,
        int(second_stored["turn_index"]),
        int(second_stored["turn_index"]),
        "done",
    )
    state = current_state(proxy_app.state.store, second_context.branch_id)
    revisions = next(iter(state["relationship_agreements"].values()))
    assert len(revisions) == 2
    assert {
        row["party"]["actor_id"]
        for row in revisions[-1]["assent"]
        if row["status"] == "accepted"
    } == {
        identity["character_actor_id"],
        identity["persona_actor_id"],
    }


async def test_real_ingress_mixed_social_failure_rolls_back_ordinary_memory(
    cfg,
    mock_upstream,
    tmp_path,
):
    proposal = "I propose to you that we be exclusive."
    assistant_text = f"{proposal} {proposal}"

    def conflicting_delta(identity, stored, _context, _store):
        accepted = str(stored["assistant_text"])
        first = accepted.index(proposal)
        second = accepted.index(proposal, first + len(proposal))
        return {
            "schema": "aetherstate/delta/2",
            "turn_range": [1, 1],
            "ops": [{
                "op": "memory_event",
                "text": "The rain key is under the blue cup.",
                "participants": [
                    identity["character_actor_id"],
                    identity["persona_actor_id"],
                ],
                "importance": 8,
                "tags": ["rain-key"],
            }],
            "social_occurrence_proposals": [
                _wire_proposal(
                    accepted,
                    proposal,
                    identity,
                    action="agreement_create",
                    search_start=first,
                ),
                _wire_proposal(
                    accepted,
                    proposal,
                    identity,
                    action="agreement_create",
                    search_start=second,
                ),
            ],
        }

    db_path = tmp_path / "production-authority-rollback.sqlite3"
    upstream_transport = httpx.ASGITransport(app=mock_upstream)
    upstream_client = httpx.AsyncClient(
        transport=upstream_transport,
        base_url="http://mock-upstream",
    )
    file_store = Store(db_path)
    file_app = create_app(
        cfg,
        client_factory=lambda: upstream_client,
        store=file_store,
    )
    before: dict[str, int] = {}

    def snapshot(store, _context):
        before["journal"] = store.journal_high_water()
        before["accepted_receipts"] = int(store.db.execute(
            "SELECT COUNT(*) FROM chat_accepted_message_receipts",
        ).fetchone()[0])
        before["effect_receipts"] = int(store.db.execute(
            "SELECT COUNT(*) FROM effect_receipts",
        ).fetchone()[0])

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=file_app),
            base_url="http://proxy",
        ) as file_client:
            await file_client.get("/aether/console")
            identity, context, _stored = await _run_real_chat_extraction_turn(
                client=file_client,
                proxy_app=file_app,
                mock_upstream=mock_upstream,
                session_name="production-authority-rollback",
                turn=1,
                user_text="Tell me what you decided.",
                assistant_text=assistant_text,
                model_delta_factory=conflicting_delta,
                before_batch=snapshot,
            )
        state = current_state(file_store, context.branch_id)
        assert state.get("relationship_agreements", {}) == {}
        assert state.get("social_occurrences", {}) == {}
        assert state.get("memories", []) == []
        assert file_store.memories_candidates(context.branch_id) == []
        assert file_store.extraction_range_is(
            context.branch_id,
            1,
            1,
            "failed",
        )
        assert identity["character_actor_id"] in state["entities"]
        assert file_store.journal_high_water() == before["journal"]
        assert int(file_store.db.execute(
            "SELECT COUNT(*) FROM chat_accepted_message_receipts",
        ).fetchone()[0]) == before["accepted_receipts"]
        assert int(file_store.db.execute(
            "SELECT COUNT(*) FROM effect_receipts",
        ).fetchone()[0]) == before["effect_receipts"]
    finally:
        await file_app.state.jobs.stop()
        await upstream_client.aclose()
        file_store.close()

    reopened = Store(db_path)
    try:
        replayed = current_state(reopened, context.branch_id)
        assert replayed.get("relationship_agreements", {}) == {}
        assert replayed.get("social_occurrences", {}) == {}
        assert replayed.get("memories", []) == []
        assert reopened.memories_candidates(context.branch_id) == []
        assert reopened.extraction_range_is(
            context.branch_id,
            1,
            1,
            "failed",
        )
        assert reopened.journal_high_water() == before["journal"]
        assert int(reopened.db.execute(
            "SELECT COUNT(*) FROM chat_accepted_message_receipts",
        ).fetchone()[0]) == before["accepted_receipts"]
        assert int(reopened.db.execute(
            "SELECT COUNT(*) FROM effect_receipts",
        ).fetchone()[0]) == before["effect_receipts"]
    finally:
        reopened.close()


async def test_real_ladder_prompt_schema_parser_and_seal_receive_role_coordinates(
    client,
    proxy_app,
    mock_upstream,
):
    session_name = "rereview-real-parser"
    admitted_response = await client.post(
        f"/aether/session/{session_name}/chat-core",
        json={"card": ORDINARY, "persona": PERSONA_CARD},
    )
    assert admitted_response.status_code == 200
    identity = admitted_response.json()
    store = proxy_app.state.store
    pipeline = proxy_app.state.pipeline
    pipeline.jobs = None
    request = json.dumps({
        "messages": [{"role": "user", "content": "Will you call me?"}],
    }).encode()
    _packet, context = pipeline.process(
        Stamp(
            session=session_name,
            turn=1,
            gen_type="normal",
            speaker="Mara",
            card_role="character",
            user="Bean",
            mode="chat",
            core_fingerprint=identity["core_fingerprint"],
            character_actor_id=identity["character_actor_id"],
            persona_actor_id=identity["persona_actor_id"],
        ),
        request,
    )
    assert context is not None
    pipeline.on_response(
        context,
        json.dumps({
            "choices": [{
                "message": {
                    "content": "I promise you that I will call after shift.",
                },
            }],
        }).encode(),
        "application/json",
    )
    stored = store.get_turn_texts(
        context.branch_id,
        context.turn_index,
        context.turn_index,
    )[0]
    assistant_text = str(stored["assistant_text"])
    evidence = "I promise you that I will call after shift."
    start = assistant_text.index(evidence)
    model_delta = {
        "schema": "aetherstate/delta/2",
        "turn_range": [context.turn_index, context.turn_index],
        "ops": [],
        "social_occurrence_proposals": [{
            "source_span": {
                "source": "assistant_text",
                "start": start,
                "end": start + len(evidence),
            },
            "subject_actor_id": identity["character_actor_id"],
            "action_code": "promise_make",
            "polarity": "positive",
            "modality": "actual",
            "participants": [{
                "kind": "actor",
                "actor_id": identity["persona_actor_id"],
            }],
            "voluntariness": [],
            "consent": [],
            "disclosure": [],
            "motive_claim_ref": None,
        }],
    }
    legacy_extra_field = json.loads(json.dumps(
        model_delta["social_occurrence_proposals"][0]
    ))
    legacy_extra_field["evidence_refs"] = []
    model_delta["social_occurrence_proposals"].append(legacy_extra_field)
    mock_upstream.enqueue(Reply(body=_model_content(model_delta)))
    pipeline.cfg.upstream.force_rung = 2
    pipeline.cfg.extraction.use_anyof = False
    store.caps_set(
        "http://mock-upstream/v1",
        "chat-rereview",
        2,
        anyof=0,
    )
    runner = JobRunner(store, pipeline.cfg, proxy_app.state.jobs.ladder)
    await runner._run_batch(
        Batch(
            context.session_id,
            context.branch_id,
            context.turn_index,
            context.turn_index,
            context.turn_index,
        ),
        Endpoint(base_url="http://mock-upstream/v1", model="chat-rereview"),
    )

    request_body = json.loads(mock_upstream.requests[-1].body)
    system = request_body["messages"][0]["content"]
    user = request_body["messages"][1]["content"]
    assert "agreement_create" in system
    assert "promise_release" in system
    assert "disclosure" in system
    assert identity["character_actor_id"] in user
    assert identity["persona_actor_id"] in user
    assert '"assistant_text":{' in user
    assert assistant_text in user
    assert '"user_text":{' in user
    assert str(stored["user_text"]) in user
    schema = request_body["response_format"]["json_schema"]["schema"]
    proposal_schema = schema["properties"]["social_occurrence_proposals"]["items"]
    assert "source_segment" not in proposal_schema["properties"]
    state = current_state(store, context.branch_id)
    assert any(
        revision["kind"] == "promise" and revision["status"] == "open"
        for revisions in state["continuity_threads"].values()
        for revision in revisions
    )
    assert sum(
        len(revisions)
        for revisions in state["social_occurrences"].values()
    ) == 1


def _record_accepted_chat_turn(
    store: Store,
    branch_id: str,
    turn: int,
    *,
    user_text: str,
    assistant_text: str,
    response_id: str,
) -> None:
    store.record_turn(branch_id, turn, "normal", "normal")
    store.write_turn_text(
        branch_id,
        turn,
        user_text=user_text,
    )
    assert store.publish_chat_response_if_current(
        branch_id,
        turn,
        expected_swipe_count=0,
        assistant_text=assistant_text,
        assistant_hash="sha256:" + hashlib.sha256(
            assistant_text.encode("utf-8"),
        ).hexdigest(),
        accepted_response_occurrence_id=response_id,
    )


def test_chat_reflection_and_recall_use_current_artifact_lineage_and_exact_refs(
    tmp_path,
):
    from aetherstate.canon import CanonMsg, chain

    cfg = Config()
    cfg.memory.reflection_every_scenes = 1
    cfg.memory.top_k = 5
    db_path = tmp_path / "derived-lineage.sqlite3"
    store = Store(db_path)
    session_id, branch_id = store.create_session(external_id="derived-lineage")
    canonical_messages = [
        CanonMsg("user", "", "0000000000000001"),
        CanonMsg("user", "", "0000000000000002"),
    ]
    canonical_chain = chain(canonical_messages)
    store.append_msgs(
        branch_id,
        0,
        [
            (message.role, message.content_hash, canonical_chain[index])
            for index, message in enumerate(canonical_messages)
        ],
    )
    old_response = "response:" + "1" * 64
    _record_accepted_chat_turn(
        store,
        branch_id,
        0,
        user_text="Remember the lighthouse key.",
        assistant_text="Mara: I will remember the lighthouse key.",
        response_id=old_response,
    )
    store.write_turn_hashes(
        branch_id,
        0,
        user_hash=canonical_messages[0].content_hash,
    )
    old_result = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{
            "op": "memory_event",
            "text": "The lighthouse key is under the blue cup.",
            "participants": [CHARACTER, PERSONA],
            "importance": 8,
            "tags": ["lighthouse"],
            "visibility": "actor_scoped",
            "scoped_actors": [CHARACTER],
        }, {
            "op": "scene_set",
            "location": "lighthouse",
        }],
        "extraction",
        cfg,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=old_response,
    )
    memory.index_applied(
        store,
        session_id,
        branch_id,
        old_result.applied,
        old_result.state,
    )
    old_ref = next(
        op["_journal_op_ref"]
        for op in old_result.applied
        if op["op"] == "memory_event"
    )

    current_response = "response:" + "2" * 64
    current_text = "Mara: I still remember the lighthouse key."
    _record_accepted_chat_turn(
        store,
        branch_id,
        1,
        user_text="Do you still remember?",
        assistant_text=current_text,
        response_id=current_response,
    )
    store.write_turn_hashes(
        branch_id,
        1,
        user_hash=canonical_messages[1].content_hash,
    )
    scene_result = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{"op": "scene_set", "location": "harbor"}],
        "rule",
        cfg,
        lifecycle_source="user_text",
    )
    lineage = {
        "turn": 1,
        "lifecycle_source": "deferred_extraction",
        "response_occurrence_id": current_response,
        "source_message_fingerprint": store.turn_text_source_fingerprint(
            branch_id,
            1,
            "deferred_extraction",
        ),
    }
    assert memory.reflect(
        store,
        cfg,
        session_id,
        branch_id,
        scene_result.state,
        artifact_lineage=lineage,
    ) == 1
    summaries = [
        row for row in store.memories_candidates(branch_id)
        if row["tier"] == "summary"
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["created_turn"] == 1
    assert summary["lifecycle_source"] == "deferred_extraction"
    assert summary["response_occurrence_id"] == current_response
    assert summary["source_message_fingerprint"] == lineage[
        "source_message_fingerprint"
    ]
    assert json.loads(summary["source_journal_op_refs"]) == [old_ref]

    memory.precompute_recall(
        store,
        cfg,
        session_id,
        branch_id,
        scene_result.state,
        "Where is the lighthouse key?",
        1,
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
        experience_mode="chat",
        artifact_lineage=lineage,
    )
    recall_row = store.db.execute(
        "SELECT * FROM recall_records WHERE branch_id=? AND for_turn=2",
        (branch_id,),
    ).fetchone()
    assert recall_row is not None
    assert recall_row["source_turn"] == 1
    assert recall_row["response_occurrence_id"] == current_response
    assert json.loads(recall_row["journal_op_refs"]) == [old_ref]
    expected_lines = store.read_recall(
        session_id,
        branch_id=branch_id,
        for_turn=2,
        experience_mode="chat",
    )
    assert expected_lines and "lighthouse key" in expected_lines[0]

    store.db.close()
    reopened = Store(db_path)
    assert reopened.read_recall(
        session_id,
        branch_id=branch_id,
        for_turn=2,
        experience_mode="chat",
    ) == expected_lines

    prefix_session, prefix_empty = reopened.create_session(
        external_id="derived-lineage-prefix",
    )
    prefix_branch = reopened.fork_branch(
        branch_id,
        1,
        0,
        new_session_id=prefix_session,
        discard_empty_branch=prefix_empty,
    )
    assert reopened.read_recall(
        prefix_session,
        branch_id=prefix_branch,
        for_turn=2,
        experience_mode="chat",
    ) == []
    assert all(
        row["tier"] != "summary"
        for row in reopened.memories_candidates(prefix_branch)
    )

    child_session, child_empty = reopened.create_session(
        external_id="derived-lineage-child",
    )
    child_branch = reopened.fork_branch(
        branch_id,
        2,
        1,
        new_session_id=child_session,
        discard_empty_branch=child_empty,
    )
    sibling_session, sibling_empty = reopened.create_session(
        external_id="derived-lineage-sibling",
    )
    sibling_branch = reopened.fork_branch(
        branch_id,
        2,
        1,
        new_session_id=sibling_session,
        discard_empty_branch=sibling_empty,
    )
    for fork_session, fork_branch in (
        (child_session, child_branch),
        (sibling_session, sibling_branch),
    ):
        assert reopened.read_recall(
            fork_session,
            branch_id=fork_branch,
            for_turn=2,
            experience_mode="chat",
        ) == expected_lines
        copied_summary = next(
            row for row in reopened.memories_candidates(fork_branch)
            if row["tier"] == "summary"
        )
        copied_refs = json.loads(copied_summary["source_journal_op_refs"])
        assert copied_refs and copied_refs != [old_ref]
        assert reopened.journal_op_refs_current(fork_branch, copied_refs)

    reopened.retract_chat_response_at(branch_id, 1)
    assert reopened.read_recall(
        session_id,
        branch_id=branch_id,
        for_turn=2,
        experience_mode="chat",
    ) == []
    candidates = reopened.memories_candidates(branch_id)
    assert all(row["tier"] != "summary" for row in candidates)
    assert any(row["text"].startswith("The lighthouse key") for row in candidates)
    assert reopened.read_recall(
        child_session,
        branch_id=child_branch,
        for_turn=2,
        experience_mode="chat",
    ) == expected_lines
    assert reopened.read_recall(
        sibling_session,
        branch_id=sibling_branch,
        for_turn=2,
        experience_mode="chat",
    ) == expected_lines


def test_blank_legacy_memory_visibility_is_explicitly_chat_closed_and_rpg_open():
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="blank-visibility")
    result = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{
            "op": "memory_event",
            "text": "LEGACY_BLANK_PRIVATE_9c31",
            "participants": [CHARACTER],
            "importance": 5,
            "tags": ["legacy"],
        }],
        "user",
        cfg,
    )
    memory.index_applied(
        store,
        session_id,
        branch_id,
        result.applied,
        result.state,
    )
    assert result.state["memories"][-1]["visibility"] == ""
    projection = chat_continuity.project_continuity(
        result.state,
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
    )
    assert "LEGACY_BLANK_PRIVATE_9c31" not in json.dumps(projection)
    assert not record_visible_to(
        {"visibility": "", "scoped_actors": []},
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
        blank_visibility="deny",
    )
    assert record_visible_to(
        {"visibility": "", "scoped_actors": []},
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
        blank_visibility="public",
    )
    assert memory.retrieve(
        store,
        cfg,
        branch_id,
        result.state,
        "legacy blank",
        1,
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
        experience_mode="chat",
    ) == []
    assert memory.retrieve(
        store,
        cfg,
        branch_id,
        result.state,
        "legacy blank",
        1,
        experience_mode="rpg",
    )


def test_chat_world_projection_is_relevance_selected_by_visible_context():
    from tests.test_typed_knowledge_retrieval import (
        BRANCH as EVENT_BRANCH,
        SESSION as EVENT_SESSION,
        WORLD as EVENT_WORLD,
        _event,
    )

    relevant_event = _event(
        "event.lighthouse-storm",
        "The lighthouse storm shutters are locked.",
    )
    unrelated_event = _event(
        "event.desert-court",
        "The desert dragon court begins its coronation.",
    )
    state = {
        "meta": {"turn": 3},
        "chat_core": {
            "core": {
                "name": "Mara",
                "description": "A lighthouse keeper on the Rainline coast.",
                "personality": "Direct and observant.",
                "scenario": "Mara watches the harbor beacon through the storm.",
                "anchors": [],
                "boundaries": [],
            },
        },
        "scene": {
            "location": "lighthouse",
            "phase": "storm watch",
        },
        "continuity_threads": {
            "thread.keep-beacon-lit": [{
                "kind": "plan",
                "summary": "Keep the lighthouse beacon lit.",
                "participants": [
                    {"kind": "actor", "actor_id": CHARACTER},
                    {"kind": "actor", "actor_id": PERSONA},
                ],
                "status": "open",
            }],
        },
        "creator_world": {
            "document": {
                "name": "Rainline",
                "genre": "modern drama",
                "setting": "The lighthouse guards Rainline's storm harbor.",
                "premise": "A desert dragon court chooses its next monarch.",
                "lore": [
                    "The lighthouse beacon guides harbor ferries.",
                    "The dragon crown burns anyone who lies.",
                ],
            },
        },
        "world_identity": {"world_id": EVENT_WORLD},
        "knowledge_record_scope": {
            "session_id": EVENT_SESSION,
            "branch_id": EVENT_BRANCH,
            "source_branch_ids": [],
        },
        "clock": {"minutes": 0},
        "world_events": [relevant_event, unrelated_event],
    }

    projected = chat_continuity.project_continuity(
        state,
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
    )
    authored = projected["world"]["authored"]
    assert authored["name"] == "Rainline"
    assert authored["genre"] == "modern drama"
    assert "lighthouse" in authored["setting"].lower()
    assert authored["lore"] == [
        "The lighthouse beacon guides harbor ferries.",
    ]
    assert "premise" not in authored
    assert [
        event["id"] for event in projected["world"]["events"]
    ] == ["event.lighthouse-storm"]


def _seal_one_social_turn(
    text: str,
    *,
    source: str,
    action: str,
    subject: str,
    counterpart: str,
    continuity_state: dict | None = None,
) -> dict[str, list[dict]]:
    proposal = _proposal(
        text,
        text,
        action=action,
        source=source,
        subject=subject,
        participant=counterpart,
    )
    return chat_continuity.seal_accepted_chat_proposals(
        user_text=text if source == "user_text" else "",
        assistant_text=text if source == "assistant_text" else "",
        proposals=[proposal],
        session_id="session:directional",
        branch_id="branch:directional",
        turn_index=3,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=RESPONSE,
        continuity_state=continuity_state,
    )


def _thread_ops(partitions: dict[str, list[dict]]) -> list[dict]:
    return [
        op["record"]
        for operations in partitions.values()
        for op in operations
        if op.get("op") == "continuity_thread_transition"
    ]


def test_reciprocal_promises_are_directional_and_only_the_authorized_actor_transitions():
    assert _seal_one_social_turn(
        "I kept my promise.",
        source="assistant_text",
        action="promise_fulfill",
        subject=CHARACTER,
        counterpart=PERSONA,
    )["assistant_response"] == []

    character_promise = _thread_ops(_seal_one_social_turn(
        "I promise you that I will call after shift.",
        source="assistant_text",
        action="promise_make",
        subject=CHARACTER,
        counterpart=PERSONA,
    ))[0]
    persona_promise = _thread_ops(_seal_one_social_turn(
        "I promise you that I will leave the porch light on.",
        source="user_text",
        action="promise_make",
        subject=PERSONA,
        counterpart=CHARACTER,
    ))[0]
    assert character_promise["thread_id"] != persona_promise["thread_id"]
    assert (
        character_promise["promisor_actor_id"],
        character_promise["promisee_actor_id"],
    ) == (CHARACTER, PERSONA)
    assert (
        persona_promise["promisor_actor_id"],
        persona_promise["promisee_actor_id"],
    ) == (PERSONA, CHARACTER)

    for record, suffix in (
        (character_promise, "3"),
        (persona_promise, "4"),
    ):
        record["fingerprint"] = "sha256:" + suffix * 64
    continuity_state = {
        "continuity_threads": {
            character_promise["thread_id"]: [character_promise],
            persona_promise["thread_id"]: [persona_promise],
        },
    }
    released = _thread_ops(_seal_one_social_turn(
        "I release you from your promise.",
        source="assistant_text",
        action="promise_release",
        subject=CHARACTER,
        counterpart=PERSONA,
        continuity_state=continuity_state,
    ))
    assert [row["thread_id"] for row in released] == [
        persona_promise["thread_id"],
    ]


def test_accepted_agreement_and_disclosure_emit_typed_coupled_transitions():
    from tests.test_chat_relationship_contract import (
        CHARACTER as EXACT_CHARACTER,
        PERSONA as EXACT_PERSONA,
        _admit_chat_identity,
    )

    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="typed-social-transitions")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    proposal_response = "response:" + "5" * 64
    proposal_text = "I propose to you that we be exclusive."
    _record_accepted_chat_turn(
        store,
        branch_id,
        1,
        user_text="What relationship terms do you want?",
        assistant_text=proposal_text,
        response_id=proposal_response,
    )
    proposal_parts = chat_continuity.seal_accepted_chat_proposals(
        user_text="What relationship terms do you want?",
        assistant_text=proposal_text,
        proposals=[_proposal(
            proposal_text,
            proposal_text,
            action="agreement_create",
            subject=EXACT_CHARACTER,
            participant=EXACT_PERSONA,
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=1,
        character_actor_id=EXACT_CHARACTER,
        persona_actor_id=EXACT_PERSONA,
        response_occurrence_id=proposal_response,
    )
    proposal_ops = [
        op for op in proposal_parts["assistant_response"]
        if op["op"] == "relationship_agreement_revision"
    ]
    assert len(proposal_ops) == 1
    assert [row["status"] for row in proposal_ops[0]["record"]["assent"]] == [
        "proposed"
    ]
    proposal_result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        1,
        proposal_parts,
        cfg,
        expected_response_occurrence_id=proposal_response,
    )
    assert not proposal_result.quarantined

    acceptance_response = "response:" + "6" * 64
    acceptance_text = "I accept your proposal that we be exclusive."
    _record_accepted_chat_turn(
        store,
        branch_id,
        2,
        user_text=acceptance_text,
        assistant_text="I understand.",
        response_id=acceptance_response,
    )
    agreement_parts = chat_continuity.seal_accepted_chat_proposals(
        user_text=acceptance_text,
        assistant_text="I understand.",
        proposals=[_proposal(
            acceptance_text,
            acceptance_text,
            action="agreement_create",
            source="user_text",
            subject=EXACT_PERSONA,
            participant=EXACT_CHARACTER,
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=2,
        character_actor_id=EXACT_CHARACTER,
        persona_actor_id=EXACT_PERSONA,
        response_occurrence_id=acceptance_response,
        continuity_state=proposal_result.state,
    )
    agreement_ops = [
        op for op in agreement_parts["user_text"]
        if op["op"] == "relationship_agreement_revision"
    ]
    assert len(agreement_ops) == 1
    assert {
        row["party"]["actor_id"]
        for row in agreement_ops[0]["record"]["assent"]
        if row["status"] == "accepted"
    } == {EXACT_CHARACTER, EXACT_PERSONA}
    agreement_result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        2,
        agreement_parts,
        cfg,
        expected_response_occurrence_id=acceptance_response,
    )
    assert not agreement_result.quarantined
    assert agreement_result.state["relationship_agreements"]

    disclosure_response = "response:" + "7" * 64
    disclosure_text = "I tell you the rain key is under the blue cup."
    _record_accepted_chat_turn(
        store,
        branch_id,
        3,
        user_text="Where is the rain key?",
        assistant_text=disclosure_text,
        response_id=disclosure_response,
    )
    disclosure_parts = chat_continuity.seal_accepted_chat_proposals(
        user_text="Where is the rain key?",
        assistant_text=disclosure_text,
        proposals=[_proposal(
            disclosure_text,
            disclosure_text,
            action="disclosure",
            subject=EXACT_CHARACTER,
            participant=EXACT_PERSONA,
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=3,
        character_actor_id=EXACT_CHARACTER,
        persona_actor_id=EXACT_PERSONA,
        response_occurrence_id=disclosure_response,
        continuity_state=agreement_result.state,
    )
    belief_ops = [
        op for op in disclosure_parts["assistant_response"]
        if op["op"] == "belief_acquire"
    ]
    assert len(belief_ops) == 1
    assert belief_ops[0]["holder"] == EXACT_PERSONA
    assert belief_ops[0]["statement"] == "the rain key is under the blue cup"
    disclosure_result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        3,
        disclosure_parts,
        cfg,
        expected_response_occurrence_id=disclosure_response,
    )
    assert not disclosure_result.quarantined
    assert any(
        row["holder"] == EXACT_PERSONA
        and row["statement"] == "the rain key is under the blue cup"
        for row in disclosure_result.state["beliefs"].values()
    )


def test_coupled_social_group_rolls_back_if_its_thread_predecessor_is_stale():
    from tests.test_chat_relationship_contract import (
        CHARACTER as EXACT_CHARACTER,
        PERSONA as EXACT_PERSONA,
        _admit_chat_identity,
    )

    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="atomic-social-group")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    response_id = "response:" + "7" * 64
    text = "I promise you that I will call after shift."
    _record_accepted_chat_turn(
        store,
        branch_id,
        1,
        user_text="Will you call?",
        assistant_text=text,
        response_id=response_id,
    )
    partitions = chat_continuity.seal_accepted_chat_proposals(
        user_text="Will you call?",
        assistant_text=text,
        proposals=[_proposal(
            text,
            text,
            action="promise_make",
            subject=EXACT_CHARACTER,
            participant=EXACT_PERSONA,
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=1,
        character_actor_id=EXACT_CHARACTER,
        persona_actor_id=EXACT_PERSONA,
        response_occurrence_id=response_id,
    )
    thread_op = next(
        op for op in partitions["assistant_response"]
        if op["op"] == "continuity_thread_transition"
    )
    thread_op["record"]["revision"] = 2
    thread_op["record"]["action"] = "update"
    thread_op["record"]["supersedes_fingerprint"] = "sha256:" + "8" * 64
    result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        1,
        partitions,
        cfg,
        expected_response_occurrence_id=response_id,
    )
    assert result.atomic_group_failures
    assert result.applied == []
    state = current_state(store, branch_id)
    assert state["social_occurrences"] == {}
    assert state["continuity_threads"] == {}
    assert store.extraction_pending_range(branch_id, 1, 1)


def _durable_claim(
    *,
    session_id: str,
    branch_id: str,
    response_id: str,
    scoped_actors: list[str],
    text: str = (
        "I kiss you. "
        "I asserted that I kissed you because I wanted reassurance."
    ),
) -> dict:
    from aetherstate.claim_frame import build_claim_frames, build_claim_record
    from aetherstate.semantic_fabric import load_default_semantic_fabric

    fabric = load_default_semantic_fabric()
    frame = build_claim_frames(
        text,
        fabric.translate(text),
        ingress="npc",
        source_id=CHARACTER,
    )[0]
    return build_claim_record(
        frame,
        session_id=session_id,
        branch_id=branch_id,
        world_id="world_unbound",
        turn=4,
        source=CHARACTER,
        visibility="actor_scoped",
        scoped_actors=scoped_actors,
        lifecycle_source="assistant_response",
        response_occurrence_id=response_id,
    )


def test_motive_reference_requires_one_exact_durable_visible_claim():
    import pytest

    with pytest.raises(ValueError, match="response occurrence"):
        _durable_claim(
            session_id="session:motive",
            branch_id="branch:motive",
            response_id="",
            scoped_actors=[CHARACTER],
        )

    claim = _durable_claim(
        session_id="session:motive",
        branch_id="branch:motive",
        response_id=RESPONSE,
        scoped_actors=[CHARACTER],
    )
    text = (
        "I kiss you. "
        "I asserted that I kissed you because I wanted reassurance."
    )
    proposal = _proposal(
        text,
        "I kiss you.",
        subject=CHARACTER,
        participant=PERSONA,
    )
    proposal["motive_claim_ref"] = {
        "claim_id": claim["claim_id"],
        "fingerprint": claim["fingerprint"],
    }
    accepted = chat_continuity.seal_accepted_chat_proposals(
        user_text="",
        assistant_text=text,
        proposals=[proposal],
        session_id="session:motive",
        branch_id="branch:motive",
        turn_index=4,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=RESPONSE,
        claim_records=[claim],
    )
    occurrence = _occurrences(accepted)[0]
    assert occurrence["motive_claim_ref"] == proposal["motive_claim_ref"]

    forged = json.loads(json.dumps(proposal))
    forged["motive_claim_ref"]["fingerprint"] = "sha256:" + "f" * 64
    forged_result = chat_continuity.seal_accepted_chat_proposals(
        user_text="",
        assistant_text=text,
        proposals=[forged],
        session_id="session:motive",
        branch_id="branch:motive",
        turn_index=4,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=RESPONSE,
        claim_records=[claim],
    )
    assert _occurrences(forged_result)[0]["motive_claim_ref"] is None

    invisible = _durable_claim(
        session_id="session:motive",
        branch_id="branch:motive",
        response_id=RESPONSE,
        scoped_actors=[PERSONA],
    )
    invisible_proposal = json.loads(json.dumps(proposal))
    invisible_proposal["motive_claim_ref"] = {
        "claim_id": invisible["claim_id"],
        "fingerprint": invisible["fingerprint"],
    }
    invisible_result = chat_continuity.seal_accepted_chat_proposals(
        user_text="",
        assistant_text=text,
        proposals=[invisible_proposal],
        session_id="session:motive",
        branch_id="branch:motive",
        turn_index=4,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=RESPONSE,
        claim_records=[invisible],
    )
    assert _occurrences(invisible_result)[0]["motive_claim_ref"] is None


def test_claim_supersession_and_binding_keep_immutable_lifecycle_provenance(
    tmp_path,
):
    from aetherstate.claim_ingress import claim_ops_from_text
    from tests.test_chat_relationship_contract import (
        ACTOR_CHARACTER,
        CHARACTER as EXACT_CHARACTER,
        OUTSIDE_ACTOR,
        _admit_chat_identity,
        _evidence,
        _occurrence,
    )

    cfg = Config()
    db_path = tmp_path / "immutable-social-provenance.sqlite3"
    store = Store(db_path)
    session_id, branch_id = store.create_session(external_id="immutable-provenance")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    first_response = "response:" + "9" * 64
    _record_accepted_chat_turn(
        store,
        branch_id,
        1,
        user_text="What is in the drawer?",
        assistant_text="Mara asserted that the locked drawer contains the rain key.",
        response_id=first_response,
    )
    claim_result = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        claim_ops_from_text(
            "Mara asserted that the locked drawer contains the rain key.",
            ingress="narrator",
            source_id=EXACT_CHARACTER,
            preserve_source_identity=True,
        ),
        "rule",
        cfg,
        lifecycle_source="assistant_response",
        response_occurrence_id=first_response,
    )
    assert not claim_result.quarantined
    stored_claim = store.claim_records(branch_id)[0]
    assert stored_claim["lifecycle_source"] == "assistant_response"
    assert stored_claim["response_occurrence_id"] == first_response

    original = _occurrence(
        "occurrence.provenance",
        outside=[{
            "kind": "anonymous",
            "occurrence_id": "occurrence.provenance",
            "anonymous_id": "anon:outside",
            "label": "an unidentified visitor",
        }],
        turn=1,
    )
    admitted = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{"op": "social_occurrence_admit", "record": original}],
        "user",
        cfg,
        lifecycle_source="assistant_response",
        response_occurrence_id=first_response,
    )
    assert not admitted.quarantined
    admitted_record = admitted.applied[0]["_record"]
    assert admitted_record["lifecycle_source"] == "assistant_response"
    assert admitted_record["response_occurrence_id"] == first_response

    second_response = "response:" + "a" * 64
    _record_accepted_chat_turn(
        store,
        branch_id,
        2,
        user_text="That account was wrong.",
        assistant_text="Mara: I retract that account.",
        response_id=second_response,
    )
    supersession = {
        "schema": chat_continuity.OCCURRENCE_SUPERSESSION_SCHEMA,
        "occurrence_id": original["occurrence_id"],
        "revision": 2,
        "action": "retract",
        "supersedes_fingerprint": admitted_record["fingerprint"],
        "cause": _evidence("correction", "accepted exact correction"),
    }
    superseded = apply_delta(
        store,
        session_id,
        branch_id,
        2,
        [{"op": "social_occurrence_supersede", "record": supersession}],
        "user",
        cfg,
        lifecycle_source="assistant_response",
        response_occurrence_id=second_response,
    )
    assert not superseded.quarantined
    latest = superseded.applied[0]["_record"]
    assert latest["lifecycle_source"] == "assistant_response"
    assert latest["response_occurrence_id"] == second_response

    actor_add = apply_delta(
        store,
        session_id,
        branch_id,
        2,
        [{
            "op": "entity_add",
            "entity": OUTSIDE_ACTOR,
            "name": "Casey",
            "kind": "character",
            "present": False,
        }],
        "user",
        cfg,
    )
    assert not actor_add.quarantined
    binding = {
        "schema": chat_continuity.REFERENT_BINDING_SCHEMA,
        "occurrence_id": original["occurrence_id"],
        "anonymous_id": "anon:outside",
        "actor_id": OUTSIDE_ACTOR,
        "cause_ref": {
            "kind": "manual_identity_confirmation",
            "fingerprint": _evidence("identity_confirmation")["fingerprint"],
        },
    }
    bound = apply_delta(
        store,
        session_id,
        branch_id,
        2,
        [{"op": "social_referent_bind", "record": binding}],
        "user",
        cfg,
        lifecycle_source="assistant_response",
        response_occurrence_id=second_response,
    )
    assert not bound.quarantined
    bound_record = bound.applied[0]["_record"]
    assert bound_record["lifecycle_source"] == "assistant_response"
    assert bound_record["response_occurrence_id"] == second_response
    assert admitted_record["agreement_actor"] == ACTOR_CHARACTER

    store.close()
    reopened = Store(db_path)
    replayed = current_state(reopened, branch_id)
    replayed_latest = replayed["social_occurrences"][
        original["occurrence_id"]
    ][-1]
    assert replayed_latest["response_occurrence_id"] == second_response
    replayed_binding = replayed["social_referent_bindings"][0]
    assert replayed_binding["lifecycle_source"] == "assistant_response"
    assert replayed_binding["response_occurrence_id"] == second_response
