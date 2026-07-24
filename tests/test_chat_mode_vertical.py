"""Whole Chat Core admission vertical: card source -> route -> journal -> restart proof."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
from dataclasses import replace

import pytest

from aetherstate.state import current_state
from aetherstate.stamps import Stamp
from tests.test_chat_card import STARTING_CONTINUITY


ORDINARY = {
    "name": "Mara",
    "description": "A night-shift paramedic with a dry sense of humor.",
    "personality": "Direct, observant, private, and protective.",
    "scenario": "Mara and {{user}} meet after her shift.",
    "first_mes": "You are still awake, {{user}}?",
    "mes_example": "Mara: I noticed. I just did not want to corner you.",
}
PERSONA = "Bean-Persona.png"
WORLD = {
    "name": "Rainline",
    "genre": "modern_drama",
    "setting": "A rain-soaked coastal city where night shifts overlap.",
}


def _actor(prefix: str, domain: bytes, value: str) -> str:
    return prefix + hashlib.sha256(domain + value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("coded", [False, True], ids=["ordinary", "coded"])
async def test_ordinary_and_coded_chat_cards_bind_exact_core_persona_without_rpg(
    client, proxy_app, coded,
):
    chat_card = importlib.import_module("aetherstate.chat_card")
    ordinary_core = chat_card.ordinary_core(ORDINARY)
    payload = {
        "card": ORDINARY,
        "persona": PERSONA,
    }
    expected_core = ordinary_core
    expected_world = None
    if coded:
        expected_core = {
            **ordinary_core,
            "anchors": ["Does not pretend to know facts she has not learned."],
            "boundaries": ["Never speaks or acts for the Persona."],
        }
        expected_world = WORLD
        metadata = chat_card.build_card(expected_core, world=WORLD)[
            "data"
        ]["extensions"]["aetherstate"]
        payload["aetherstate"] = metadata

    response = await client.post("/aether/session/chat-core-case/chat-core", json=payload)
    assert response.status_code == 200, response.text
    admitted = response.json()
    assert admitted["mode"] == "chat"
    assert admitted["complete"] is True
    assert admitted["core_fingerprint"] == chat_card.core_fingerprint(expected_core)
    assert admitted["character_actor_id"] == _actor(
        "character:", b"chat-character\0", admitted["core_fingerprint"],
    )
    assert admitted["persona_actor_id"] == _actor(
        "persona:", b"chat-persona\0", PERSONA,
    )
    assert admitted["world_seeded"] is coded
    assert admitted["warning"] == ""

    store = proxy_app.state.store
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-core-case'",
    ).fetchone()
    assert session is not None
    assert session["core_fingerprint"] == admitted["core_fingerprint"]
    assert session["character_actor_id"] == admitted["character_actor_id"]
    assert session["persona_actor_id"] == admitted["persona_actor_id"]
    assert PERSONA not in json.dumps(dict(session))

    state = current_state(store, session["active_branch"])
    assert state["chat_core"]["core"] == expected_core
    assert state["chat_core"]["core_fingerprint"] == admitted["core_fingerprint"]
    assert state["chat_core"]["character_actor_id"] == admitted["character_actor_id"]
    assert state["chat_core"]["persona_actor_id"] == admitted["persona_actor_id"]
    assert state["player"] == {}
    assert state["entities"][admitted["character_actor_id"]]["name"] == "Mara"
    assert state["entities"][admitted["character_actor_id"]]["kind"] == "character"
    assert state["entities"][admitted["persona_actor_id"]]["kind"] == "persona"
    assert state["entities"][admitted["persona_actor_id"]]["name"] == "Persona"
    assert state.get("narrator") in (None, {}, "")
    assert "example_dialogue" in state["chat_core"]["core"]
    if coded:
        assert state["creator_world"]["document"]["name"] == "Rainline"
        assert state["world_identity"]["world_id"].startswith("world_")
    else:
        assert state["creator_world"] == {}
        assert state["world_identity"] == {}

    journal = store.db.execute(
        "SELECT id, ops FROM ops_journal WHERE branch_id=? ORDER BY id",
        (session["active_branch"],),
    ).fetchall()
    core_ops = [
        op
        for row in journal
        for op in json.loads(row["ops"])
        if op.get("op") == "chat_core_seed"
    ]
    assert len(core_ops) == 1
    receipt = store.chat_core_receipt_for_session(session["session_id"])
    assert receipt is not None
    assert receipt["journal_op_id"] == journal[0]["id"]
    assert receipt["core_fingerprint"] == admitted["core_fingerprint"]
    assert receipt["character_actor_id"] == admitted["character_actor_id"]
    assert receipt["persona_actor_id"] == admitted["persona_actor_id"]
    assert receipt["world_fingerprint"] == (
        chat_card.world_fingerprint(expected_world)
        if expected_world else ""
    )

    status = await client.get("/aether/session/chat-core-case/chat-core-status")
    assert status.status_code == 200
    assert status.json()["core_fingerprint"] == admitted["core_fingerprint"]
    assert status.json()["character_actor_id"] == admitted["character_actor_id"]
    assert status.json()["persona_actor_id"] == admitted["persona_actor_id"]
    assert "core" not in status.json()

    repeat = await client.post("/aether/session/chat-core-case/chat-core", json=payload)
    assert repeat.status_code == 200
    assert repeat.json()["already_present"] is True
    assert repeat.json()["applied"] == 0

    drifted = {
        "card": {**ORDINARY, "description": "A materially different Character Core."},
        "persona": PERSONA,
    }
    drift = await client.post("/aether/session/chat-core-case/chat-core", json=drifted)
    assert drift.status_code == 200
    assert drift.json()["replaced_provisional"] is True
    assert drift.json()["core_fingerprint"] != admitted["core_fingerprint"]
    replaced_state = current_state(store, session["active_branch"])
    assert replaced_state["chat_core"]["core"]["description"] == drifted["card"]["description"]
    assert len([
        op
        for row in store.db.execute(
            "SELECT ops FROM ops_journal WHERE branch_id=?", (session["active_branch"],),
        ).fetchall()
        for op in json.loads(row["ops"])
        if op.get("op") == "chat_core_seed"
    ]) == 1

    locked = store.experience_lock(session["session_id"], 0)
    assert locked.locked
    mismatch = await client.post(
        "/aether/session/chat-core-case/chat-core",
        json={"card": drifted["card"], "persona": "Different-Persona.png"},
    )
    assert mismatch.status_code == 409
    assert "new chat" in mismatch.json()["error"].lower()


async def test_malformed_coded_chat_core_uses_one_visible_ordinary_fallback(client):
    chat_card = importlib.import_module("aetherstate.chat_card")
    coded_core = {
        **chat_card.ordinary_core(ORDINARY),
        "name": "Forged coded name",
    }
    metadata = chat_card.build_card(coded_core, world=WORLD)[
        "data"
    ]["extensions"]["aetherstate"]
    metadata["core_fingerprint"] = "sha256:" + "0" * 64

    response = await client.post(
        "/aether/session/chat-core-fallback/chat-core",
        json={"card": ORDINARY, "persona": PERSONA, "aetherstate": metadata},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["core_fingerprint"] == chat_card.core_fingerprint(
        chat_card.ordinary_core(ORDINARY),
    )
    assert "coded" in body["warning"].lower()
    assert body["world_seeded"] is False


async def test_chat_card_build_and_inspect_routes_are_session_free(client):
    chat_card = importlib.import_module("aetherstate.chat_card")
    core = chat_card.ordinary_core(ORDINARY)
    built = await client.post("/aether/chat-card", json={"core": core, "world": WORLD})
    assert built.status_code == 200
    body = built.json()
    assert body["core_fingerprint"] == chat_card.core_fingerprint(core)
    assert body["png_b64"]

    inspected = await client.post(
        "/aether/chat-card/inspect",
        json={"filename": "mara.png", "data_b64": body["png_b64"]},
    )
    assert inspected.status_code == 200
    assert inspected.json()["name"] == "Mara"
    assert inspected.json()["extensions"]["aetherstate"]["role"] == "character"


async def test_chat_request_identity_stamp_fails_open_without_continuity_writes(
    client, proxy_app,
):
    admitted_response = await client.post(
        "/aether/session/chat-stamp-case/chat-core",
        json={"card": ORDINARY, "persona": PERSONA},
    )
    admitted = admitted_response.json()
    assert admitted_response.status_code == 200
    body = json.dumps({
        "model": "test",
        "messages": [{"role": "user", "content": "Keep this request unchanged."}],
    }).encode()
    store = proxy_app.state.store
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-stamp-case'",
    ).fetchone()
    before = tuple(
        store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("turns", "branch_msgs", "ops_journal")
    )
    valid = {
        "mode": "chat",
        "core_fingerprint": admitted["core_fingerprint"],
        "character_actor_id": admitted["character_actor_id"],
        "persona_actor_id": admitted["persona_actor_id"],
    }
    forged_values = (
        {"mode": None},
        {"core_fingerprint": "sha256:" + "0" * 64},
        {"character_actor_id": "character:" + "0" * 64},
        {"persona_actor_id": "persona:" + "0" * 64},
    )
    for drift in forged_values:
        forwarded, context = proxy_app.state.pipeline.process(
            Stamp(session="chat-stamp-case", card_role="character", **{
                **valid, **drift,
            }),
            body,
        )
        assert forwarded == body
        assert context is None
        assert tuple(
            store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("turns", "branch_msgs", "ops_journal")
        ) == before

    forwarded, context = proxy_app.state.pipeline.process(
        Stamp(session="chat-stamp-case", card_role="character", **valid),
        body,
    )
    assert context is not None
    assert forwarded != body
    assert store.experience_binding(session["session_id"]).core_fingerprint \
        == admitted["core_fingerprint"]


@pytest.mark.parametrize("locked", [False, True], ids=["explicit-unlocked", "locked"])
async def test_proven_explicit_chat_fork_inherits_exact_core_binding_and_receipt(
    client, proxy_app, locked,
):
    admitted_response = await client.post(
        "/aether/session/chat-fork-parent/chat-core",
        json={"card": ORDINARY, "persona": PERSONA},
    )
    assert admitted_response.status_code == 200
    admitted = admitted_response.json()
    store = proxy_app.state.store
    engine = proxy_app.state.engine
    engine.cfg.adopt_min_lcp = 2
    parent = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-fork-parent'",
    ).fetchone()
    if locked:
        store.experience_lock(parent["session_id"], 0)
    else:
        store.experience_mode_set_unlocked(parent["session_id"], "chat")

    parent_body = json.dumps({
        "messages": [
            {"role": "user", "content": "Shared question one."},
            {"role": "assistant", "content": "Shared answer one."},
        ],
    }).encode()
    parent_resolution = engine.observe(
        Stamp(session="chat-fork-parent"),
        parent_body,
    )
    assert parent_resolution is not None
    heuristic_body = json.dumps({
        "messages": [
            {"role": "user", "content": "Shared question one."},
            {"role": "assistant", "content": "Shared answer one."},
            {"role": "user", "content": "Heuristic lookalike question."},
        ],
    }).encode()
    heuristic = engine.observe(
        Stamp(session=f"chat-heuristic-{locked}"),
        heuristic_body,
    )
    assert heuristic is not None
    assert heuristic.session_id != parent["session_id"]
    heuristic_session = store.db.execute(
        "SELECT * FROM sessions WHERE session_id=?", (heuristic.session_id,),
    ).fetchone()
    assert not heuristic_session["core_fingerprint"]
    assert store.chat_core_receipt_for_session(heuristic.session_id) is None
    assert current_state(store, heuristic.branch_id)["chat_core"] == {}

    child_body = json.dumps({
        "messages": [
            {"role": "user", "content": "Shared question one."},
            {"role": "assistant", "content": "Shared answer one."},
            {"role": "user", "content": "Divergent child question."},
        ],
    }).encode()
    child_resolution = engine.observe(
        Stamp(
            session=f"chat-fork-child-{locked}",
            parent="chat-fork-parent",
            fork_pos=2,
        ),
        child_body,
    )
    assert child_resolution is not None
    assert child_resolution.session_id != parent["session_id"]
    child = store.db.execute(
        "SELECT * FROM sessions WHERE session_id=?",
        (child_resolution.session_id,),
    ).fetchone()
    assert child["core_fingerprint"] == admitted["core_fingerprint"]
    assert child["character_actor_id"] == admitted["character_actor_id"]
    assert child["persona_actor_id"] == admitted["persona_actor_id"]
    child_receipt = store.chat_core_receipt_for_session(child["session_id"])
    assert child_receipt is not None
    assert child_receipt["branch_id"] == child_resolution.branch_id
    assert child_receipt["core_fingerprint"] == admitted["core_fingerprint"]
    assert child_receipt["persona_actor_id"] == admitted["persona_actor_id"]
    child_state = current_state(store, child_resolution.branch_id)
    assert child_state["chat_core"]["core_fingerprint"] == admitted["core_fingerprint"]
    assert child_state["chat_core"]["persona_actor_id"] == admitted["persona_actor_id"]


async def test_persona_avatar_key_is_exact_and_invalid_values_are_rejected(client):
    exact_keys = ("Persona.png", " Persona.png", "Persona.png ")
    actor_ids = []
    for index, persona in enumerate(exact_keys):
        response = await client.post(
            f"/aether/session/exact-persona-{index}/chat-core",
            json={"card": ORDINARY, "persona": persona},
        )
        assert response.status_code == 200, response.text
        expected = _actor("persona:", b"chat-persona\0", persona)
        assert response.json()["persona_actor_id"] == expected
        actor_ids.append(expected)
    assert len(set(actor_ids)) == len(exact_keys)

    for index, invalid in enumerate(("", "x" * 513, "bad\u0000persona.png")):
        response = await client.post(
            f"/aether/session/invalid-persona-{index}/chat-core",
            json={"card": ORDINARY, "persona": invalid},
        )
        assert response.status_code == 422


async def test_chat_admission_never_calls_rpg_player_or_model_stage_b(
    client, monkeypatch,
):
    from aetherstate import genesis

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Chat admission crossed into RPG/Stage B Genesis")

    monkeypatch.setattr(genesis, "seed_player", forbidden)
    monkeypatch.setattr(genesis, "seed_llm", forbidden)
    response = await client.post(
        "/aether/session/chat-no-rpg-genesis/chat-core",
        json={"card": ORDINARY, "persona": PERSONA, "world": WORLD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is True


async def test_first_proxied_chat_turn_never_runs_player_or_model_genesis(
    client, proxy_app, monkeypatch,
):
    from aetherstate import genesis

    admission = await client.post(
        "/aether/session/chat-first-proxy/chat-core",
        json={"card": ORDINARY, "persona": PERSONA},
    )
    assert admission.status_code == 200
    identity = admission.json()
    player_calls = []
    model_calls = []

    def forbidden_player(*args, **kwargs):
        player_calls.append((args, kwargs))
        return 0

    async def forbidden_model(*args, **kwargs):
        model_calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(genesis, "seed_player", forbidden_player)
    monkeypatch.setattr(genesis, "seed_llm", forbidden_model)
    request = json.dumps({
        "model": "test",
        "messages": [
            {"role": "system", "content": "Raw Character card system prose."},
            {"role": "user", "content": "This is the first proxied Chat turn."},
        ],
    }).encode()
    stamp = Stamp(
        session="chat-first-proxy",
        mode="chat",
        card_role="character",
        core_fingerprint=identity["core_fingerprint"],
        character_actor_id=identity["character_actor_id"],
        persona_actor_id=identity["persona_actor_id"],
    )
    _forwarded, context = proxy_app.state.pipeline.process(stamp, request)
    assert context is not None
    assert context.klass == "new_session"
    accepted = json.dumps({
        "choices": [{"message": {"content": "The exact Chat reply."}}],
    }).encode()
    proxy_app.state.pipeline.on_response(context, accepted, "application/json")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert player_calls == []
    assert model_calls == []
    store = proxy_app.state.store
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-first-proxy'",
    ).fetchone()
    state = current_state(store, session["active_branch"])
    assert state["player"] == {}
    assert store.genesis_state(session["session_id"]) == "rules"


async def test_private_pov_swipe_and_fork_follow_only_accepted_lineage(
    client, proxy_app, monkeypatch,
):
    """One compact Chat history proves private POV, swipe CAS, and fork lineage."""
    from aetherstate import chat_continuity, compose, hud, knowledge, memory, state as state_module
    from aetherstate.experience import config_for_experience
    from aetherstate.extraction import Endpoint, StateDelta, delta_json_schema
    from aetherstate.jobs import Batch, JobRunner
    from aetherstate.state import apply_delta

    chat_extraction_schema = delta_json_schema(chat=True)["schema"]
    proposal_schema = chat_extraction_schema["properties"][
        "social_occurrence_proposals"
    ]["items"]
    assert {
        "source_span",
        "subject_actor_id",
        "action_code",
        "polarity",
        "modality",
        "participants",
        "voluntariness",
        "consent",
        "disclosure",
        "motive_claim_ref",
    } == set(proposal_schema["required"])

    starting = copy.deepcopy(STARTING_CONTINUITY)
    private_seed = "UNDISCOVERED_AFFAIR_SENTINEL_7f4c"
    private_knowledge = (
        "PRIVATE_MOTIVE_SENTINEL: Mara privately suspects the night dispatcher lied."
    )
    starting["memories"][0].update({
        "text": private_seed,
        "visibility": "character_private",
    })
    starting["memories"].append({
        "memory_id": "memory.shared-first-shift",
        "text": "Mara remembers the Persona waiting up after her first night shift.",
        "visibility": "shared",
    })
    starting["character_knowledge"][0]["statement"] = private_knowledge
    starting["player_visible_possessions_conditions"].append({
        "record_id": "possession.rain-key",
        "kind": "possession",
        "summary": "Mara openly carries a silver rain key.",
    })
    for family in ("agreement_revisions", "open_threads"):
        starting[family][0].update({
            "visibility": "hidden",
            "scoped_actors": [],
        })
    malformed = {
        "memory_id": "",
        "text": "This malformed optional row must not roll back its siblings.",
        "visibility": "shared",
    }
    starting["memories"].append(malformed)

    admitted_response = await client.post(
        "/aether/session/chat-private-lineage/chat-core",
        json={
            "card": ORDINARY,
            "persona": PERSONA,
            "continuity": starting,
        },
    )
    assert admitted_response.status_code == 200, admitted_response.text
    admitted = admitted_response.json()
    assert "continuity_results" in admitted
    results = admitted["continuity_results"]
    assert len(results) == sum(
        len(starting[name])
        for name in (
            "memories",
            "player_visible_possessions_conditions",
            "character_knowledge",
            "relationship_causes",
            "agreement_revisions",
            "open_threads",
        )
    )
    assert sum(row["accepted"] is True for row in results) == len(results) - 1
    assert sum(row["accepted"] is False for row in results) == 1
    accepted_receipts = {
        row["receipt_fingerprint"] for row in results if row["accepted"]
    }
    assert len(accepted_receipts) == len(results) - 1

    store = proxy_app.state.store
    pipeline = proxy_app.state.pipeline
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-private-lineage'",
    ).fetchone()
    branch = session["active_branch"]
    core_receipt = store.chat_core_receipt_for_session(session["session_id"])
    assert core_receipt is not None
    assert core_receipt["receipt_fingerprint"] not in accepted_receipts
    assert {
        row["record_fingerprint"]
        for row in store.chat_continuity_seed_receipts(session["session_id"])
    } == {
        row["record_fingerprint"] for row in results if row["accepted"]
    }

    reset_admission = await client.post(
        "/aether/session/chat-provisional-continuity-reset/chat-core",
        json={
            "card": ORDINARY,
            "persona": PERSONA,
            "continuity": {
                "schema": STARTING_CONTINUITY["schema"],
                "memories": [copy.deepcopy(STARTING_CONTINUITY["memories"][0])],
            },
        },
    )
    assert reset_admission.status_code == 200, reset_admission.text
    reset_session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-provisional-continuity-reset'",
    ).fetchone()
    reset_receipts = store.chat_continuity_seed_receipts(
        reset_session["session_id"]
    )
    assert len(reset_receipts) == 1
    reset_journal_ids = {row["journal_op_id"] for row in reset_receipts}
    store.reset_unlocked_experience(reset_session["session_id"], "rpg")
    assert store.chat_core_receipt_for_session(reset_session["session_id"]) is None
    assert store.chat_continuity_seed_receipts(reset_session["session_id"]) == []
    assert not store.db.execute(
        "SELECT 1 FROM ops_journal WHERE id IN (%s)"
        % ",".join("?" for _ in reset_journal_ids),
        tuple(reset_journal_ids),
    ).fetchone()
    apply_partitioned_delta = getattr(state_module, "apply_partitioned_delta")

    cfg = config_for_experience(pipeline.cfg, "chat")
    binding = store.experience_binding(session["session_id"])
    character_id = admitted["character_actor_id"]
    persona_id = admitted["persona_actor_id"]
    experience_status = await client.get(
        f"/aether/session/{session['session_id']}/experience-mode"
    )
    assert experience_status.status_code == 200
    assert experience_status.json()["core_fingerprint"] == admitted["core_fingerprint"]
    assert experience_status.json()["character_actor_id"] == character_id
    seeded_state = current_state(store, branch)
    character_packet = compose.render_chat_packet(seeded_state, binding, cfg)
    persona_projection = chat_continuity.project_continuity(
        seeded_state,
        viewer_actor_id=persona_id,
        player_actor_id=persona_id,
    )
    assert private_seed in character_packet
    assert private_knowledge in character_packet
    assert private_seed not in json.dumps(persona_projection)
    assert private_knowledge not in json.dumps(persona_projection)
    assert "silver rain key" in character_packet.lower()

    lessons = pipeline.player_lessons_service
    if lessons is None:
        lessons = proxy_app.state.player_lessons_service
        pipeline.player_lessons_service = lessons
    wide_marker = "CHAT_WIDE_LESSON_c981"
    core_marker = "EXACT_CORE_LESSON_a410"
    intent_marker = "RPG_ONLY_INTENT_LESSON_2d67"
    wide = lessons.create(
        effect_type="narration_behavior",
        title=wide_marker,
        scope="every_chat_turn",
        do_text=wide_marker,
        avoid_text="",
        anchor_entry_id=None,
        character_core_fingerprint="",
    )
    exact = lessons.create(
        effect_type="narration_behavior",
        title=core_marker,
        scope="every_chat_turn",
        do_text=core_marker,
        avoid_text="",
        anchor_entry_id=None,
        character_core_fingerprint=admitted["core_fingerprint"],
    )
    assert wide["character_core_fingerprint"] == ""
    assert exact["character_core_fingerprint"] == admitted["core_fingerprint"]

    stamp = Stamp(
        session="chat-private-lineage",
        turn=1,
        gen_type="normal",
        speaker="Mara",
        card_role="character",
        user="Bean",
        mode="chat",
        core_fingerprint=admitted["core_fingerprint"],
        character_actor_id=character_id,
        persona_actor_id=persona_id,
    )
    user_text = "I promise to keep the rain key safe, and I kiss you."
    request = json.dumps({
        "model": "chat-private-lineage",
        "messages": [{"role": "user", "content": user_text}],
    }).encode()
    first_packet, first_ctx = pipeline.process(stamp, request)
    assert first_ctx is not None
    first_wire = json.dumps(json.loads(first_packet), ensure_ascii=False)
    assert wide_marker in first_wire
    assert core_marker in first_wire
    assert intent_marker not in first_wire
    forbidden = (
        "[RULES]", "[PLAYER]", "[WAR]", "Player Card",
        "ROLL", "SKILLS", "RESOURCES",
    )
    assert not any(marker in first_wire for marker in forbidden)

    abandoned_secret = "ABANDONED_AFFAIR_SECRET_934e"
    abandoned_reply = (
        f"Mara promised that {abandoned_secret} will remain guarded. I kiss you."
    )
    pipeline.on_response(
        first_ctx,
        json.dumps({"choices": [{"message": {"content": abandoned_reply}}]}).encode(),
        "application/json",
    )
    old_turn = store.db.execute(
        "SELECT * FROM turns WHERE branch_id=? AND turn_index=?",
        (branch, first_ctx.turn_index),
    ).fetchone()
    old_response_id = old_turn["accepted_response_occurrence_id"]
    assert old_response_id
    accepted_abandoned_reply = str(
        store.get_turn_texts(
            branch,
            first_ctx.turn_index,
            first_ctx.turn_index,
        )[0]["assistant_text"]
    )
    old_claims = store.claim_records(branch)
    assert any(abandoned_secret in json.dumps(row) for row in old_claims)

    old_memory = {
        "op": "memory_event",
        "text": abandoned_secret,
        "participants": [character_id],
        "importance": 9,
        "visibility": "actor_scoped",
        "scoped_actors": [character_id],
    }
    old_result = apply_delta(
        store,
        session["session_id"],
        branch,
        first_ctx.turn_index,
        [old_memory],
        "extraction",
        cfg,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=old_response_id,
    )
    assert len(old_result.applied) == 1
    memory.index_applied(
        store,
        session["session_id"],
        branch,
        old_result.applied,
        old_result.state,
    )
    assert any(
        abandoned_secret in row["text"]
        for row in store.memories_candidates(branch)
    )
    assert any(
        abandoned_secret in row["text"]
        for row in memory.retrieve(
            store,
            cfg,
            branch,
            old_result.state,
            abandoned_secret,
            first_ctx.turn_index,
            viewer_actor_id=character_id,
            player_actor_id=persona_id,
            experience_mode="chat",
        )
    )
    assert all(
        abandoned_secret not in row["text"]
        for row in memory.retrieve(
            store,
            cfg,
            branch,
            old_result.state,
            abandoned_secret,
            first_ctx.turn_index,
            viewer_actor_id=persona_id,
            player_actor_id=persona_id,
            experience_mode="chat",
        )
    )

    abandoned_proposals = [{
        "source_span": {
            "source": "assistant_text",
            "start": accepted_abandoned_reply.index("I kiss you"),
            "end": accepted_abandoned_reply.index("I kiss you") + len("I kiss you"),
        },
        "subject_actor_id": character_id,
        "action_code": "romantic_contact",
        "polarity": "positive",
        "modality": "actual",
        "participants": [{"kind": "actor", "actor_id": persona_id}],
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
        "motive_claim_ref": None,
    }]
    sealed_old = chat_continuity.seal_accepted_chat_proposals(
        user_text=user_text,
        assistant_text=accepted_abandoned_reply,
        proposals=abandoned_proposals,
        session_id=session["session_id"],
        branch_id=branch,
        turn_index=first_ctx.turn_index,
        character_actor_id=character_id,
        persona_actor_id=persona_id,
        response_occurrence_id=old_response_id,
        character_display_name="Mara",
        claim_records=old_claims,
    )
    old_social = apply_partitioned_delta(
        store,
        session["session_id"],
        branch,
        first_ctx.turn_index,
        sealed_old,
        cfg,
        expected_response_occurrence_id=old_response_id,
    )
    assert old_social.applied
    assert old_social.state["social_occurrences"]
    assert {
        (row["source"], row["lifecycle_source"])
        for row in store.db.execute(
            "SELECT source, lifecycle_source FROM ops_journal"
            " WHERE branch_id=? AND response_occurrence_id=?",
            (branch, old_response_id),
        ).fetchall()
    } >= {
        ("rule", "assistant_response"),
        ("extraction", "deferred_extraction"),
    }
    assert abandoned_secret in compose.render_chat_packet(
        old_social.state, binding, cfg,
    )
    assert abandoned_secret not in json.dumps(chat_continuity.project_continuity(
        old_social.state,
        viewer_actor_id=persona_id,
        player_actor_id=persona_id,
    ))

    swipe_stamp = replace(stamp, gen_type="swipe")
    replacement_packet, replacement_ctx = pipeline.process(swipe_stamp, request)
    assert replacement_ctx is not None and replacement_ctx.klass == "swipe"
    replacement_reply = "I promise I will call after shift. I kiss you."
    pipeline.on_response(
        replacement_ctx,
        json.dumps({"choices": [{"message": {"content": replacement_reply}}]}).encode(),
        "application/json",
    )
    replacement_turn = store.db.execute(
        "SELECT * FROM turns WHERE branch_id=? AND turn_index=?",
        (branch, replacement_ctx.turn_index),
    ).fetchone()
    replacement_response_id = replacement_turn["accepted_response_occurrence_id"]
    assert replacement_response_id and replacement_response_id != old_response_id
    accepted_replacement_reply = str(
        store.get_turn_texts(
            branch,
            replacement_ctx.turn_index,
            replacement_ctx.turn_index,
        )[0]["assistant_text"]
    )
    replacement_claims = store.claim_records(branch)
    assert abandoned_secret not in json.dumps(current_state(store, branch))
    assert abandoned_secret not in json.dumps(replacement_claims)
    assert not any(
        abandoned_secret in row["text"]
        for row in store.memories_candidates(branch)
    )

    stale = apply_partitioned_delta(
        store,
        session["session_id"],
        branch,
        replacement_ctx.turn_index,
        {
            "deferred_extraction": [{
                "op": "memory_event",
                "text": "STALE_ABANDONED_EXTRACTION_117a",
                "visibility": "actor_scoped",
                "scoped_actors": [character_id],
            }],
        },
        cfg,
        expected_response_occurrence_id=old_response_id,
    )
    assert stale.applied == []
    assert "STALE_ABANDONED_EXTRACTION_117a" not in json.dumps(
        current_state(store, branch)
    )

    replacement_proposals = [{
        "source_span": {
            "source": "assistant_text",
            "start": accepted_replacement_reply.index("I kiss you"),
            "end": (
                accepted_replacement_reply.index("I kiss you")
                + len("I kiss you")
            ),
        },
        "subject_actor_id": character_id,
        "action_code": "romantic_contact",
        "polarity": "positive",
        "modality": "actual",
        "participants": [{"kind": "actor", "actor_id": persona_id}],
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
        "motive_claim_ref": None,
    }]
    sealed_replacement = chat_continuity.seal_accepted_chat_proposals(
        user_text=user_text,
        assistant_text=accepted_replacement_reply,
        proposals=replacement_proposals,
        session_id=session["session_id"],
        branch_id=branch,
        turn_index=replacement_ctx.turn_index,
        character_actor_id=character_id,
        persona_actor_id=persona_id,
        response_occurrence_id=replacement_response_id,
        character_display_name="Mara",
        claim_records=replacement_claims,
    )
    replacement_social = apply_partitioned_delta(
        store,
        session["session_id"],
        branch,
        replacement_ctx.turn_index,
        sealed_replacement,
        cfg,
        expected_response_occurrence_id=replacement_response_id,
    )
    assert replacement_social.applied

    class _AcceptedChatLadder:
        request_local_config = True

        @staticmethod
        def get_client():
            return None

        async def extract(
            self,
            _endpoint,
            snapshot,
            _characters,
            lo,
            hi,
            _exchange,
            *,
            context,
            request_cfg,
            experience_mode,
        ):
            assert experience_mode == "chat"
            assert request_cfg.specialization.name == "none"
            assert private_seed in snapshot
            assert context == ""
            return StateDelta.model_validate({
                "schema": "aetherstate/delta/2",
                "turn_range": [lo, hi],
                "ops": [{
                    "op": "memory_event",
                    "text": replacement_reply,
                    "participants": [character_id, persona_id],
                    "importance": 6,
                    "visibility": "actor_scoped",
                    "scoped_actors": [character_id],
                }],
                "social_occurrence_proposals": [],
            })

    chat_jobs = JobRunner(store, pipeline.cfg, _AcceptedChatLadder())
    await chat_jobs._run_batch(
        Batch(
            session["session_id"],
            branch,
            replacement_ctx.turn_index,
            replacement_ctx.turn_index,
            replacement_ctx.turn_index,
        ),
        Endpoint(base_url="http://example.test", model="chat-test"),
    )
    assert store.extraction_range_is(
        branch,
        replacement_ctx.turn_index,
        replacement_ctx.turn_index,
        "done",
    )
    indexed_replacement = next(
        row
        for row in store.memories_candidates(branch)
        if row["text"] == replacement_reply
    )
    assert indexed_replacement["lifecycle_source"] == "deferred_extraction"
    assert indexed_replacement["response_occurrence_id"] == replacement_response_id

    accepted_state = current_state(store, branch)
    admitted_agreement = accepted_state["relationship_agreements"][
        "agreement.starting"
    ][-1]
    admitted_thread = accepted_state["continuity_threads"]["thread.weekend-plan"][-1]
    for admitted_starting_record in (admitted_agreement, admitted_thread):
        assert admitted_starting_record["visibility"] == "actor_scoped"
        assert admitted_starting_record["scoped_actors"] == sorted({
            character_id,
            persona_id,
        })
    assert any(
        revision["status"] in {"open", "fulfilled"}
        for revisions in accepted_state["continuity_threads"].values()
        for revision in revisions
    )
    assert all(
        revision["revision"] == index + 1
        and (
            index == 0
            or revision["supersedes_fingerprint"]
            == revisions[index - 1]["fingerprint"]
        )
        for revisions in accepted_state["continuity_threads"].values()
        for index, revision in enumerate(revisions)
    )

    hud_response = await client.get(
        f"/aether/session/{session['session_id']}/hud"
    )
    assert hud_response.status_code == 200
    view = hud_response.json()
    assert view["experience_mode"] == "chat"
    assert view["continuity_available"] is True
    assert view["persona_actor_id"] == persona_id
    assert view["character_actor_id"] == character_id
    assert list(view["continuity"]) == [
        "now",
        "relationship",
        "open_threads",
        "shared_history",
        "character",
    ]
    assert view["continuity"]["now"]["setting"] == (
        "No authored World; continuity follows this conversation."
    )
    assert {
        row["summary"]
        for row in view["continuity"]["now"]["possessions"]
    } == {
        "Mara's left wrist is lightly sprained.",
        "Mara openly carries a silver rain key.",
    }
    relationship = view["continuity"]["relationship"]
    assert relationship["summary"] == "Trust grew."
    assert relationship["changes"][0] == {
        "change": "Trust grew",
        "reason": "The Persona waited up for Mara after a difficult shift.",
        "turn": 0,
        "evidence": "Accepted starting continuity",
    }
    admitted_projection = chat_continuity.project_continuity(
        accepted_state,
        viewer_actor_id=persona_id,
        player_actor_id=persona_id,
    )
    missing_starting_records = {
        sentinel
        for sentinel, present in (
            (
                "agreement.starting",
                "agreement.starting" in admitted_projection["agreements"],
            ),
            (
                "thread.weekend-plan",
                "thread.weekend-plan" in admitted_projection["open_threads"],
            ),
        )
        if not present
    }
    assert missing_starting_records == set()
    assert relationship["agreements"] == [{
        "summary": "Exclusive relationship agreement",
        "status": "create",
    }]
    assert view["continuity"]["open_threads"][0]["summary"] == (
        "They still need to choose where to go this weekend."
    )
    assert any(
        row["text"]
        == "Mara remembers the Persona waiting up after her first night shift."
        for row in view["continuity"]["shared_history"]["memories"]
    )
    visibility_state = copy.deepcopy(accepted_state)
    visibility_state["memories"].extend([
        {"text": "MISSING_VISIBILITY_MUST_STAY_HIDDEN"},
        {
            "text": "EMPTY_SCOPE_MUST_STAY_HIDDEN",
            "visibility": "actor_scoped",
            "scoped_actors": [],
        },
        {
            "text": "EXPLICIT_PERSONA_MEMORY_MUST_RENDER",
            "visibility": "actor_scoped",
            "scoped_actors": [persona_id],
        },
    ])
    agreement_template = copy.deepcopy(next(
        revisions[-1]
        for revisions in accepted_state["relationship_agreements"].values()
        if revisions
    ))
    empty_scope_agreement = {
        **agreement_template,
        "visibility": "actor_scoped",
        "scoped_actors": [],
    }
    persona_agreement = {
        **agreement_template,
        "visibility": "actor_scoped",
        "scoped_actors": [persona_id],
    }
    visibility_state["relationship_agreements"] = {
        "EMPTY_SCOPE_AGREEMENT_MUST_STAY_HIDDEN": [empty_scope_agreement],
        "PERSONA_AGREEMENT_MUST_RENDER": [persona_agreement],
    }

    occurrence_template = copy.deepcopy(next(
        revisions[-1]
        for revisions in accepted_state["social_occurrences"].values()
        if revisions
    ))
    empty_scope_occurrence = {
        **occurrence_template,
        "visibility": "actor_scoped",
        "scoped_actors": [],
    }
    persona_occurrence = {
        **occurrence_template,
        "visibility": "actor_scoped",
        "scoped_actors": [persona_id],
    }
    visibility_state["social_occurrences"] = {
        "EMPTY_SCOPE_OCCURRENCE_MUST_STAY_HIDDEN": [empty_scope_occurrence],
        "PERSONA_OCCURRENCE_MUST_RENDER": [persona_occurrence],
    }

    thread_template = copy.deepcopy(next(
        revisions[-1]
        for revisions in accepted_state["continuity_threads"].values()
        if revisions and revisions[-1]["status"] == "open"
    ))
    empty_scope_thread = {
        **thread_template,
        "visibility": "actor_scoped",
        "scoped_actors": [],
    }
    persona_thread = {
        **thread_template,
        "visibility": "actor_scoped",
        "scoped_actors": [persona_id],
    }
    visibility_state["continuity_threads"] = {
        "EMPTY_SCOPE_THREAD_MUST_STAY_HIDDEN": [empty_scope_thread],
        "PERSONA_THREAD_MUST_RENDER": [persona_thread],
    }

    family_projection = chat_continuity.project_continuity(
        visibility_state,
        viewer_actor_id=persona_id,
        player_actor_id=persona_id,
    )
    visibility_view = hud.chat_continuity_view(
        visibility_state,
        persona_actor_id=persona_id,
        character_actor_id=character_id,
        core_fingerprint=admitted["core_fingerprint"],
    )
    visible_memory_text = {
        row["text"]
        for row in visibility_view["continuity"]["shared_history"]["memories"]
    }
    assert "MISSING_VISIBILITY_MUST_STAY_HIDDEN" not in visible_memory_text
    assert "EMPTY_SCOPE_MUST_STAY_HIDDEN" not in visible_memory_text
    assert "EXPLICIT_PERSONA_MEMORY_MUST_RENDER" in visible_memory_text
    leaked_empty_scopes = {
        sentinel
        for family in ("agreements", "social_occurrences", "open_threads")
        for sentinel in family_projection[family]
        if sentinel.startswith("EMPTY_SCOPE_")
    }
    assert leaked_empty_scopes == set()
    assert "PERSONA_AGREEMENT_MUST_RENDER" in family_projection["agreements"]
    assert "PERSONA_OCCURRENCE_MUST_RENDER" in family_projection["social_occurrences"]
    assert "PERSONA_THREAD_MUST_RENDER" in family_projection["open_threads"]
    assert visibility_view["continuity"]["relationship"]["changes"][0] == {
        "change": "Trust grew",
        "reason": "The Persona waited up for Mara after a difficult shift.",
        "turn": 0,
        "evidence": "Accepted starting continuity",
    }
    assert view["continuity"]["character"] == {
        "name": "Mara",
        "description": "A night-shift paramedic with a dry sense of humor.",
        "personality": "Direct, observant, private, and protective.",
    }
    serialized_view = json.dumps(view, sort_keys=True)
    for forbidden_key in (
        '"players"',
        '"skills"',
        '"abilities"',
        '"rolls"',
        '"gear"',
        '"inventory"',
        '"war_room"',
        '"dims"',
        '"dimension"',
        '"score"',
        '"affinity"',
    ):
        assert forbidden_key not in serialized_view
    for private_sentinel in (
        abandoned_secret,
        private_seed,
        private_knowledge,
    ):
        assert private_sentinel not in serialized_view

    visible_after_disclosure = apply_delta(
        store,
        session["session_id"],
        branch,
        replacement_ctx.turn_index + 1,
        [{
                "op": "belief_acquire",
                "holder": persona_id,
                "statement": private_knowledge,
                "stance": "believes",
            "source": "told",
            "visibility": "actor_scoped",
            "scoped_actors": [persona_id],
        }],
        "user",
        cfg,
        lifecycle_source="user_text",
    )
    assert private_knowledge in knowledge.render_knowledge_context(
        visible_after_disclosure.state,
        audience="actor",
        actor_id=persona_id,
        player_actor_id=persona_id,
    )
    assert private_seed not in knowledge.render_knowledge_context(
        visible_after_disclosure.state,
        audience="actor",
        actor_id=persona_id,
        player_actor_id=persona_id,
    )

    def fork(name: str) -> tuple[str, str]:
        child_session, empty_branch = store.create_session(external_id=name)
        child_branch = store.fork_branch(
            branch,
            len(store.get_msgs(branch)),
            replacement_ctx.turn_index,
            new_session_id=child_session,
            discard_empty_branch=empty_branch,
        )
        assert store.inherit_session_settings(session["session_id"], child_session)
        return child_session, child_branch

    child_session, child_branch = fork("chat-private-child")
    sibling_session, sibling_branch = fork("chat-private-sibling")
    child_only = apply_delta(
        store,
        child_session,
        child_branch,
        replacement_ctx.turn_index + 1,
        [{
            "op": "memory_event",
            "text": "CHILD_ONLY_MEMORY_d294",
            "visibility": "actor_scoped",
            "scoped_actors": [character_id],
        }],
        "user",
        cfg,
        lifecycle_source="user_text",
    )
    assert child_only.applied
    assert "CHILD_ONLY_MEMORY_d294" in json.dumps(current_state(store, child_branch))
    assert "CHILD_ONLY_MEMORY_d294" not in json.dumps(current_state(store, branch))
    assert "CHILD_ONLY_MEMORY_d294" not in json.dumps(current_state(store, sibling_branch))
    assert store.chat_continuity_seed_receipts(child_session)
    assert store.chat_continuity_seed_receipts(sibling_session)

    failure_admission = await client.post(
        "/aether/session/chat-recognition-fail-open/chat-core",
        json={"card": ORDINARY, "persona": "Failure-Persona.png"},
    )
    assert failure_admission.status_code == 200
    failed_identity = failure_admission.json()

    def fail_recognition(*_args, **_kwargs):
        raise RuntimeError("injected accepted-recognition failure")

    monkeypatch.setattr(
        pipeline,
        "_recognize_accepted_chat_exchange",
        fail_recognition,
    )
    failure_stamp = Stamp(
        session="chat-recognition-fail-open",
        turn=1,
        gen_type="normal",
        speaker="Mara",
        card_role="character",
        user="Bean",
        mode="chat",
        core_fingerprint=failed_identity["core_fingerprint"],
        character_actor_id=failed_identity["character_actor_id"],
        persona_actor_id=failed_identity["persona_actor_id"],
    )
    failure_request = json.dumps({
        "messages": [{"role": "user", "content": "I promise this visible turn survives."}],
    }).encode()
    _failed_packet, failed_ctx = pipeline.process(failure_stamp, failure_request)
    assert failed_ctx is not None
    visible_reply = "This visible accepted reply survives recognition failure."
    pipeline.on_response(
        failed_ctx,
        json.dumps({"choices": [{"message": {"content": visible_reply}}]}).encode(),
        "application/json",
    )
    failed_session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='chat-recognition-fail-open'",
    ).fetchone()
    failed_turn = store.db.execute(
        "SELECT * FROM turn_texts WHERE branch_id=? AND turn_index=?",
        (failed_session["active_branch"], failed_ctx.turn_index),
    ).fetchone()
    assert visible_reply in failed_turn["assistant_text"]
    assert store.experience_binding(failed_session["session_id"]).locked
    assert store.claim_records(failed_session["active_branch"]) == []
    assert current_state(store, failed_session["active_branch"])[
        "social_occurrences"
    ] == {}


async def test_narrator_card_rpg_vertical_is_unchanged(client, proxy_app, cfg):
    """Narrator-card inference keeps the established Player/RPG HUD vertical intact."""
    from aetherstate import hud, narrator
    from aetherstate.experience import config_for_experience
    from aetherstate.stamps import Stamp
    from tests.test_narrator_card import _WORLD
    from tests.test_st_extension_hud import _MARKERS as RPG_HUD_MARKERS

    player = {"name": "Rook", "concept": "gravedigger", "stats": {"STR": 12}}
    card = narrator.build_card(_WORLD, player)["data"]
    seed = card["extensions"]["aetherstate"]["seed"]
    seed_response = await client.post(
        "/aether/session/narrator-rpg-vertical/seed", json={"seed": seed},
    )
    assert seed_response.status_code == 200, seed_response.text
    assert seed_response.json()["complete"] is True
    assert seed_response.json()["player_seeded"] is True

    genesis_response = await client.post(
        "/aether/session/narrator-rpg-vertical/genesis",
        json={
            "card": json.dumps(card),
            "greeting": card["first_mes"],
            "speaker": card["name"],
            "card_role": "narrator",
            "structured_seed": True,
            "seed_fingerprint": card["extensions"]["aetherstate"]["seed_fingerprint"],
        },
    )
    assert genesis_response.status_code == 200, genesis_response.text
    assert genesis_response.json()["structured_seed"] is True

    store = proxy_app.state.store
    session = store.db.execute(
        "SELECT * FROM sessions WHERE external_id='narrator-rpg-vertical'",
    ).fetchone()
    assert session is not None
    binding = store.experience_binding(session["session_id"])
    assert (binding.mode, binding.source) == ("rpg", "card:narrator")

    request = json.dumps({
        "model": "test",
        "messages": [{"role": "user", "content": "((aether.check stealth))"}],
    }).encode()
    _forwarded, context = proxy_app.state.pipeline.process(
        Stamp(
            session="narrator-rpg-vertical",
            speaker=card["name"],
            card_role="narrator",
        ),
        request,
    )
    assert context is not None
    assert store.experience_binding(session["session_id"]).mode == "rpg"

    state = current_state(store, session["active_branch"])
    assert state["player"]
    assert state["chat_core"] == {}
    view = hud.hud_view(state, config_for_experience(cfg, "rpg"))
    assert tuple(
        marker.removeprefix("function tab")
        for marker in RPG_HUD_MARKERS
        if marker.startswith("function tab")
    ) == ("Char", "Skills", "Abilities", "Rolls", "Gear", "Inventory", "Status", "World")
    assert set(view) == {
        "spec", "frozen", "intent_floor", "frozen_reason", "turn", "scene", "players",
        "cast", "quests", "rolls", "relationships", "relations", "factions", "world_flags",
        "memories", "knowledge", "consent", "rules", "war_room", "fronts", "clock",
        "player_safe_raw", "surface_visibility",
    }
    assert view["spec"] == "rpg"
    assert view["players"]
    assert view["rules"]
    assert view["rolls"] and view["rolls"][0]["kind"] == "check"
    assert "war_room" in view and "active" in view["war_room"]
    assert "chat_core" not in view
    assert "continuity" not in view
