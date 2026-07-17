"""Live Player-turn retrieval and replay reuse for the separate Player Lessons seam."""
from __future__ import annotations

import json
import random
import threading
from types import SimpleNamespace

import pytest

from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.player_lessons import PlayerLessons
from aetherstate.playerlex import PlayerLex
from aetherstate.semantic_atlas import load_default_semantic_atlas
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from tests.mock_upstream import Reply


class RecordingLessons:
    def __init__(self) -> None:
        self.select_calls: list[dict] = []
        self.rehydrate_calls: list[tuple[str, int]] = []
        self.delivered_calls: list[tuple[str, int, tuple[str, ...]]] = []

    @staticmethod
    def _rows() -> dict:
        return {
            "schema": "player-lesson-selection/1",
            "lessons": [{
                "lesson_id": "lesson-test",
                "title": "Keep it sensory",
                "do": "Use concrete sound and texture.",
                "avoid": "Do not summarize the whole scene.",
            }],
        }

    def select(self, **kwargs):
        self.select_calls.append(kwargs)
        return self._rows()

    def rehydrate(self, branch_id: str, turn_index: int):
        self.rehydrate_calls.append((branch_id, turn_index))
        return self._rows()

    def mark_delivered(self, branch_id: str, turn_index: int, lesson_ids):
        self.delivered_calls.append((branch_id, turn_index, tuple(lesson_ids)))


class RecordingIntentLessons(RecordingLessons):
    def __init__(self, anchor: dict) -> None:
        super().__init__()
        self.anchor = anchor
        self.intent_select_calls: list[dict] = []
        self.intent_application_calls: list[tuple[str, int, list[dict]]] = []

    def select_intent(self, **kwargs):
        self.intent_select_calls.append(kwargs)
        identity = next(
            row
            for row in kwargs["recognized_meanings"]
            if row["lex_id"] == self.anchor["lex_id"]
            and row["concept_id"] == self.anchor["concept"]["concept_id"]
        )
        return {
            "schema": "player-lesson-intent-selection/1",
            "selected": [{
                "lesson_id": "lesson_" + "a" * 32,
                "lesson_revision": 1,
                "lesson_fingerprint": "sha256:" + "b" * 64,
                "reason": "scope_and_anchor_match",
                "scope": "every_rpg_turn",
                "intent_slot": "action",
                "anchor": {
                    "entry_id": self.anchor["entry_id"],
                    "lex_id": self.anchor["lex_id"],
                    "concept_id": self.anchor["concept"]["concept_id"],
                    "meaning_fingerprint": self.anchor["concept"]["meaning_fingerprint"],
                },
                "source_span": {
                    "start": identity["source_start"],
                    "end": identity["source_end"],
                },
                "approval_source_id": identity["approval_source_id"],
            }],
        }

    def record_intent_applications(self, branch_id, turn_index, applications):
        self.intent_application_calls.append((branch_id, turn_index, list(applications)))


class FlakyIntentLessons(RecordingIntentLessons):
    def __init__(self, anchor: dict) -> None:
        super().__init__(anchor)
        self.application_attempts = 0

    def record_intent_applications(self, branch_id, turn_index, applications):
        self.application_attempts += 1
        if self.application_attempts == 1:
            raise RuntimeError("transient receipt failure")
        super().record_intent_applications(branch_id, turn_index, applications)

    def repair_intent_applications(self, branch_id, turn_index, applications):
        self.record_intent_applications(branch_id, turn_index, applications)


def _cfg(*, rpg: bool = True) -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg" if rpg else "none"
    cfg.injection.max_tokens = 2400
    return cfg


def _body(text: str = "I open the old door.") -> bytes:
    return json.dumps({
        "model": "player-lessons-test",
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")


def _stamp(
    session: str,
    *,
    turn: int = 1,
    gen_type: str = "normal",
    card_role: str = "narrator",
) -> Stamp:
    return Stamp(
        session=session,
        turn=turn,
        gen_type=gen_type,
        speaker="Narrator",
        card_role=card_role,
        user="Bean",
    )


def _pipeline(session: str, *, rpg: bool = True):
    cfg = _cfg(rpg=rpg)
    store = Store(":memory:")
    lessons = RecordingLessons()
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(21),
        playerlex_service=None,
        player_lessons_service=lessons,
    )
    return pipe, store, lessons, _stamp(session)


def _wire_text(packet: bytes) -> str:
    payload = json.loads(packet)
    return "\n".join(str(row.get("content", "")) for row in payload["messages"])


def test_fresh_player_rpg_turn_selects_once_delivers_and_duplicate_reuses_exact_packet():
    pipe, _store, lessons, stamp = _pipeline("lesson-fresh")
    body = _body()

    first, first_ctx = pipe.process(stamp, body)

    assert lessons.delivered_calls == []
    assert pipe.mark_player_lessons_delivered(first_ctx) is True

    duplicate, duplicate_ctx = pipe.process(stamp, body)
    assert pipe.mark_player_lessons_delivered(duplicate_ctx) is False

    assert first == duplicate
    assert "[PLAYER LESSONS player-lessons/1" in _wire_text(first)
    assert len(lessons.select_calls) == 1
    call = lessons.select_calls[0]
    assert call["turn_index"] == first_ctx.turn_index
    assert call["narration_mode"] == "exploration"
    assert call["user_hash"]
    assert isinstance(call["recognized_meanings"], tuple)
    assert lessons.rehydrate_calls == [(first_ctx.branch_id, first_ctx.turn_index)]
    assert lessons.delivered_calls == [
        (first_ctx.branch_id, first_ctx.turn_index, ("lesson-test",))
    ]
    assert pipe.prewarm_doc(first_ctx.session_id) is None
    assert duplicate_ctx.network_duplicate is True
    assert duplicate_ctx.player_lesson_delivered is True


@pytest.mark.asyncio
async def test_delete_api_prevents_exact_duplicate_packet_from_replaying_player_lesson(
    client,
    proxy_app,
    mock_upstream,
):
    pipeline = proxy_app.state.pipeline
    pipeline.cfg.specialization.name = "rpg"
    pipeline.cfg.injection.max_tokens = 2400
    title = "Duplicate deletion sentinel"
    do_text = "Describe the wet hinges in close detail."
    avoid_text = "Never summarize the doorway in one line."
    created = await client.post(
        "/aether/player-lessons",
        json={
            "effect_type": "narration_behavior",
            "title": title,
            "scope": "every_rpg_turn",
            "do": do_text,
            "avoid": avoid_text,
            "anchor_entry_id": None,
        },
    )
    assert created.status_code == 201
    lesson_id = created.json()["lesson"]["lesson_id"]

    upstream_reply = Reply(body=json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "The swollen door opens with a damp scrape.",
            },
        }],
    }).encode("utf-8"))
    mock_upstream.enqueue(upstream_reply)
    mock_upstream.enqueue(upstream_reply)
    request = {
        "model": "player-lessons-delete-duplicate",
        "messages": [
            {
                "role": "system",
                "content": (
                    "<<AETHER:v=1;session=lesson-delete-duplicate;turn=1;type=normal;"
                    "speaker=Narrator;card_role=narrator;user=Bean>>"
                ),
            },
            {"role": "user", "content": "I open the rain-streaked door."},
        ],
    }
    raw_request = json.dumps(request, separators=(",", ":")).encode("utf-8")
    headers = {"content-type": "application/json"}

    first = await client.post("/v1/chat/completions", content=raw_request, headers=headers)
    assert first.status_code == 200
    first_wire = _wire_text(mock_upstream.requests[0].body)
    markers = {
        "[PLAYER LESSONS player-lessons/1",
        title,
        do_text,
        avoid_text,
    }
    assert {marker for marker in markers if marker not in first_wire} == set()

    removed = await client.delete(f"/aether/player-lessons/{lesson_id}")
    assert removed.status_code == 200

    duplicate = await client.post("/v1/chat/completions", content=raw_request, headers=headers)
    assert duplicate.status_code == 200
    duplicate_wire = _wire_text(mock_upstream.requests[1].body)
    leaked = {marker for marker in markers if marker in duplicate_wire}
    assert leaked == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("disable", "correct"))
async def test_disable_or_correction_evicts_old_private_duplicate_packet(
    mutation,
    client,
    proxy_app,
    mock_upstream,
):
    pipeline = proxy_app.state.pipeline
    pipeline.cfg.specialization.name = "rpg"
    marker = f"PRIVATE_OLD_{mutation.upper()}_LESSON"
    created_response = await client.post(
        "/aether/player-lessons",
        json={
            "effect_type": "narration_behavior",
            "title": marker,
            "scope": "every_rpg_turn",
            "do": f"{marker} do",
            "avoid": "",
            "anchor_entry_id": None,
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["lesson"]
    upstream_reply = Reply(body=json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "The door opens."}}],
    }).encode("utf-8"))
    mock_upstream.enqueue(upstream_reply)
    mock_upstream.enqueue(upstream_reply)
    request = {
        "model": f"player-lessons-{mutation}-duplicate",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"<<AETHER:v=1;session=lesson-{mutation}-duplicate;turn=1;type=normal;"
                    "speaker=Narrator;card_role=narrator;user=Bean>>"
                ),
            },
            {"role": "user", "content": "I open the door."},
        ],
    }
    raw_request = json.dumps(request, separators=(",", ":")).encode("utf-8")
    headers = {"content-type": "application/json"}
    first = await client.post("/v1/chat/completions", content=raw_request, headers=headers)
    assert first.status_code == 200
    assert marker in _wire_text(mock_upstream.requests[0].body)

    if mutation == "disable":
        changed = await client.post(
            f"/aether/player-lessons/{created['lesson_id']}/enabled",
            json={
                "enabled": False,
                "expected_revision": created["revision"],
                "expected_fingerprint": created["fingerprint"],
            },
        )
    else:
        changed = await client.patch(
            f"/aether/player-lessons/{created['lesson_id']}",
            json={
                "effect_type": "narration_behavior",
                "title": "Replacement lesson",
                "scope": "every_rpg_turn",
                "do": "Use the replacement only on a future new turn.",
                "avoid": "",
                "anchor_entry_id": None,
                "expected_revision": created["revision"],
                "expected_fingerprint": created["fingerprint"],
            },
        )
    assert changed.status_code == 200

    duplicate = await client.post("/v1/chat/completions", content=raw_request, headers=headers)
    assert duplicate.status_code == 200
    duplicate_wire = _wire_text(mock_upstream.requests[1].body)
    assert marker not in duplicate_wire
    assert "Replacement lesson" not in duplicate_wire


@pytest.mark.parametrize(
    ("kind", "gen_type", "stamp_turn", "expected_class"),
    (
        ("continue", "continue", 1, "continue"),
        ("swipe", "swipe", 1, "swipe"),
        ("lost_reply", "normal", 2, "new_turn"),
    ),
)
def test_replay_classes_rehydrate_frozen_selection_without_reranking(
    kind: str,
    gen_type: str,
    stamp_turn: int,
    expected_class: str,
):
    pipe, _store, lessons, initial_stamp = _pipeline(f"lesson-{kind}")
    body = _body()
    first, first_ctx = pipe.process(initial_stamp, body)
    assert pipe.mark_player_lessons_delivered(first_ctx) is True

    replay, replay_ctx = pipe.process(
        _stamp(initial_stamp.session, turn=stamp_turn, gen_type=gen_type),
        body,
    )
    assert pipe.mark_player_lessons_delivered(replay_ctx) is True

    assert len(lessons.select_calls) == 1
    assert lessons.rehydrate_calls == [(first_ctx.branch_id, first_ctx.turn_index)]
    assert replay_ctx.klass == expected_class
    assert "[PLAYER LESSONS player-lessons/1" in _wire_text(replay)
    assert len(lessons.delivered_calls) == 2


@pytest.mark.parametrize("rpg,gen_type", ((False, "normal"), (True, "impersonate")))
def test_non_rpg_and_impersonation_never_select_player_lessons(rpg: bool, gen_type: str):
    pipe, _store, lessons, stamp = _pipeline(f"lesson-inert-{rpg}-{gen_type}", rpg=rpg)
    stamp = _stamp(stamp.session, gen_type=gen_type)

    packet, _ctx = pipe.process(stamp, _body())

    assert lessons.select_calls == []
    assert lessons.rehydrate_calls == []
    assert lessons.delivered_calls == []
    assert "PLAYER LESSONS" not in _wire_text(packet)


def test_non_narrator_card_never_receives_private_narration_lesson_text():
    pipe, _store, lessons, stamp = _pipeline("lesson-character-card")

    packet, _ctx = pipe.process(
        _stamp(stamp.session, card_role="character"),
        _body("I open the old door."),
    )

    assert lessons.select_calls == []
    assert lessons.rehydrate_calls == []
    assert lessons.delivered_calls == []
    assert "PLAYER LESSONS" not in _wire_text(packet)


def test_unstamped_heuristic_request_never_receives_private_narration_lesson_text():
    pipe, _store, lessons, _stamp_value = _pipeline("lesson-unstamped")

    packet, _ctx = pipe.process(None, _body("I open the old door."))

    assert lessons.select_calls == []
    assert lessons.rehydrate_calls == []
    assert lessons.delivered_calls == []
    assert "PLAYER LESSONS" not in _wire_text(packet)


def test_cached_narrator_packet_is_never_reused_for_non_narrator_duplicate():
    pipe, _store, lessons, stamp = _pipeline("lesson-cross-role-duplicate")
    body = _body("I open the old door.")

    narrator_packet, _first_ctx = pipe.process(stamp, body)
    non_narrator_packet, duplicate_ctx = pipe.process(
        _stamp(stamp.session, card_role="character"),
        body,
    )

    assert "PLAYER LESSONS" in _wire_text(narrator_packet)
    assert "PLAYER LESSONS" not in _wire_text(non_narrator_packet)
    assert non_narrator_packet == body
    assert duplicate_ctx.network_duplicate is True
    assert lessons.select_calls and len(lessons.select_calls) == 1


def test_recognition_projection_contains_exact_identity_without_source_phrase():
    match = SimpleNamespace(
        lex_id="action",
        concept_id="action.inspect",
        entry_fingerprint="sha256:" + "a" * 64,
        surface_baseline="playerlex",
        features={},
        matched_phrase="private source prose",
    )
    result = SimpleNamespace(
        semantic_turn=SimpleNamespace(
            compiled_meaning=SimpleNamespace(matches=(match, match)),
        )
    )

    assert Pipeline._recognized_meanings(result) == (
        ("action", "action.inspect", "sha256:" + "a" * 64),
    )
    assert "private source prose" not in repr(Pipeline._recognized_meanings(result))


def test_recovered_playerlex_publication_is_atomic_with_lesson_rebind():
    cfg = _cfg()
    store = Store(":memory:")
    lessons = PlayerLessons(store.db, playerlex=None, lock=store.apply_guard())
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(22),
        playerlex_service=None,
        player_lessons_service=lessons,
    )
    recovered = PlayerLex(store.db, load_default_semantic_atlas(), store.apply_guard())
    bind_entered = threading.Event()
    release_bind = threading.Event()
    guard_entered = threading.Event()
    failures: list[BaseException] = []
    original_bind = lessons.bind_playerlex

    def blocking_bind(service) -> None:
        bind_entered.set()
        if not release_bind.wait(2):
            raise TimeoutError("test did not release PlayerLex rebind")
        original_bind(service)

    lessons.bind_playerlex = blocking_bind

    def publish() -> None:
        try:
            pipe.bind_playerlex_service(recovered)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def probe_live_turn_guard() -> None:
        with lessons.lifecycle_guard():
            guard_entered.set()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert bind_entered.wait(2)
    assert pipe.playerlex_service is None

    probe = threading.Thread(target=probe_live_turn_guard)
    probe.start()
    assert not guard_entered.wait(0.05)

    release_bind.set()
    publisher.join(2)
    probe.join(2)

    assert not publisher.is_alive()
    assert not probe.is_alive()
    assert failures == []
    assert guard_entered.is_set()
    assert pipe.playerlex_service is recovered


def test_fresh_intent_selection_and_application_record_once_before_duplicate_replay():
    cfg = _cfg()
    store = Store(":memory:")
    playerlex = PlayerLex(store.db, load_default_semantic_atlas(), store._lock)
    anchor = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    lessons = RecordingIntentLessons(anchor)
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(22),
        playerlex_service=playerlex,
        player_lessons_service=lessons,
    )
    stamp = _stamp("lesson-intent-fresh")
    body = _body("I use Glass Read on the panel.")

    first, first_ctx = pipe.process(stamp, body)
    duplicate, duplicate_ctx = pipe.process(stamp, body)

    assert first == duplicate
    assert len(lessons.intent_select_calls) == 1
    recognized = lessons.intent_select_calls[0]["recognized_meanings"]
    assert len(recognized) == 1
    assert recognized[0]["lex_id"] == "action"
    assert recognized[0]["concept_id"] == "action.inspect"
    assert "matched_phrase" not in recognized[0]
    assert len(lessons.intent_application_calls) == 1
    application = lessons.intent_application_calls[0][2][0]
    assert application["applied"] is False
    assert application["reason"] == "candidate_absent"
    assert first_ctx.player_lesson_intent_ids == ("lesson_" + "a" * 32,)
    assert duplicate_ctx.player_lesson_intent_ids == first_ctx.player_lesson_intent_ids

    purged = pipe.forget_player_lesson("lesson_" + "a" * 32)
    assert purged["request_packets"] == 1


def test_transient_intent_application_receipt_failure_repairs_before_process_returns():
    cfg = _cfg()
    store = Store(":memory:")
    playerlex = PlayerLex(store.db, load_default_semantic_atlas(), store._lock)
    anchor = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    lessons = FlakyIntentLessons(anchor)
    pipe = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(23),
        playerlex_service=playerlex,
        player_lessons_service=lessons,
    )

    _packet, context = pipe.process(
        _stamp("lesson-intent-repair"),
        _body("I use Glass Read on the panel."),
    )

    assert lessons.application_attempts == 2
    assert len(lessons.intent_application_calls) == 1
    assert context.player_lesson_intent_applications_pending == ()
