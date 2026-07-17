"""Control API contract for explicit local Player Lessons."""
from __future__ import annotations

import pytest

from aetherstate.control import make_control_router
from aetherstate.player_lessons import PlayerLessons


def _draft(**overrides) -> dict:
    return {
        "effect_type": "narration_behavior",
        "title": "Keep it sensory",
        "scope": "every_rpg_turn",
        "do": "Use concrete sound and texture.",
        "avoid": "Do not summarize the whole scene.",
        "anchor_entry_id": None,
        **overrides,
    }


def _intent_draft(anchor_entry_id: str | None, **overrides) -> dict:
    return {
        "effect_type": "intent_interpretation",
        "title": "Read Glass Read as inspection",
        "scope": "exploration",
        "misunderstanding": "AetherState can read Glass Read as more than one action.",
        "correct_interpretation": "Use its approved inspection meaning in this situation.",
        "anchor_entry_id": anchor_entry_id,
        **overrides,
    }


@pytest.mark.asyncio
async def test_player_lessons_api_covers_consent_test_revision_toggle_selection_and_removal(
    client,
    proxy_app,
):
    empty = await client.get("/aether/player-lessons")
    assert empty.status_code == 200
    assert empty.json() == {"schema": "player-lessons-list/1", "lessons": []}

    future = await client.post(
        "/aether/player-lessons",
        json=_draft(effect_type="interpretation_correction"),
    )
    assert future.status_code == 422

    created = await client.post("/aether/player-lessons", json=_draft())
    assert created.status_code == 201
    lesson = created.json()["lesson"]
    assert lesson["enabled"] is True
    assert lesson["status"] == "current"
    assert lesson["anchor_status"] == "unanchored"
    assert lesson["provenance"]["approval"] == "explicit_local"
    assert lesson["provenance"]["approved_via"] == "local_control_api"

    sample = "I quietly open the rain-slick door."
    tested = await client.post(
        "/aether/player-lessons/test",
        json={**_draft(), "sample_text": sample, "narration_mode": "exploration"},
    )
    assert tested.status_code == 200
    assert tested.json()["result"] == {
        "schema": "player-lesson-test/1",
        "matched": True,
        "reason": "scope_match",
        "scope": "every_rpg_turn",
        "narration_mode": "exploration",
        "anchor": None,
        "anchor_status": "unanchored",
        "stale_reason": None,
        "sample_stored": False,
    }
    database_dump = "\n".join(proxy_app.state.store.db.iterdump())
    assert sample not in database_dump

    stale_revision = lesson
    corrected = await client.patch(
        f"/aether/player-lessons/{lesson['lesson_id']}",
        json={
            **_draft(title="Stay close to the moment"),
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert corrected.status_code == 200
    lesson = corrected.json()["lesson"]
    assert lesson["revision"] == 2

    raced = await client.patch(
        f"/aether/player-lessons/{lesson['lesson_id']}",
        json={
            **_draft(title="Stale overwrite"),
            "expected_revision": stale_revision["revision"],
            "expected_fingerprint": stale_revision["fingerprint"],
        },
    )
    assert raced.status_code == 409

    disabled = await client.post(
        f"/aether/player-lessons/{lesson['lesson_id']}/enabled",
        json={
            "enabled": False,
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert disabled.status_code == 200
    lesson = disabled.json()["lesson"]
    assert lesson["enabled"] is False and lesson["revision"] == 3

    enabled = await client.post(
        f"/aether/player-lessons/{lesson['lesson_id']}/enabled",
        json={
            "enabled": True,
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert enabled.status_code == 200
    lesson = enabled.json()["lesson"]
    assert lesson["enabled"] is True and lesson["revision"] == 4

    store = proxy_app.state.store
    session_id, branch_id = store.create_session(external_id="player-lessons-api-selection")
    service = proxy_app.state.pipeline.player_lessons_service
    selection = service.select(
        branch_id=branch_id,
        turn_index=0,
        user_hash="1" * 16,
        narration_mode="exploration",
        recognized_meanings=(),
    )
    assert [row["lesson_id"] for row in selection["selected"]] == [lesson["lesson_id"]]
    service.mark_delivered(branch_id, 0, [lesson["lesson_id"]])

    latest = await client.get(
        "/aether/player-lessons/selections",
        params={"session_id": session_id},
    )
    assert latest.status_code == 200
    assert latest.json()["selections"][0]["lesson_id"] == lesson["lesson_id"]
    assert latest.json()["selections"][0]["reason"] == "scope_match"
    assert latest.json()["selections"][0]["delivered"] is True

    removed = await client.delete(f"/aether/player-lessons/{lesson['lesson_id']}")
    assert removed.status_code == 200
    assert removed.json() == {"removed": True, "lesson_id": lesson["lesson_id"]}
    assert (await client.get("/aether/player-lessons")).json()["lessons"] == []
    assert (
        await client.get(
            "/aether/player-lessons/selections",
            params={"session_id": session_id},
        )
    ).json()["selections"] == []
    frozen = service.rehydrate(branch_id, 0)
    assert frozen["frozen_selected_count"] == 1
    assert frozen["available_count"] == 0
    assert frozen["omitted_count"] == 1

    # Removal is intentionally idempotent so a retry can finish a post-commit WAL scrub.
    repeated = await client.delete(f"/aether/player-lessons/{lesson['lesson_id']}")
    assert repeated.status_code == 200


@pytest.mark.asyncio
async def test_console_lazy_service_recovery_rebinds_live_pipeline(client, proxy_app):
    proxy_app.state.pipeline.player_lessons_service = None

    response = await client.get("/aether/player-lessons")

    assert response.status_code == 200
    assert proxy_app.state.pipeline.player_lessons_service is not None


@pytest.mark.asyncio
async def test_playerlex_partial_startup_recovery_rebinds_existing_player_lessons(
    client,
    proxy_app,
):
    pipeline = proxy_app.state.pipeline
    lessons = PlayerLessons(
        proxy_app.state.store.db,
        playerlex=None,
        lock=proxy_app.state.store.apply_guard(),
    )
    pipeline.playerlex_service = None
    pipeline.player_lessons_service = lessons

    approved = await client.post(
        "/aether/playerlex",
        json={
            "kind": "alias",
            "surface": "Recovered Glass Read",
            "lex_id": "action",
            "concept_id": "action.inspect",
        },
    )

    assert approved.status_code == 201, approved.text
    assert pipeline.playerlex_service is not None
    entry_id = approved.json()["entry"]["entry_id"]

    created = await client.post(
        "/aether/player-lessons",
        json=_intent_draft(entry_id),
    )

    assert created.status_code == 201, created.text
    assert created.json()["lesson"]["anchor"]["entry_id"] == entry_id


@pytest.mark.asyncio
async def test_player_lessons_api_requires_complete_bounded_payloads(client):
    missing = await client.post(
        "/aether/player-lessons",
        json={"effect_type": "narration_behavior", "title": "Incomplete"},
    )
    assert missing.status_code == 422
    assert "missing Player Lesson fields" in missing.json()["error"]

    unknown = await client.post(
        "/aether/player-lessons",
        json={**_draft(), "mechanic": "grant flight"},
    )
    assert unknown.status_code == 422
    assert "unknown Player Lesson fields" in unknown.json()["error"]

    invalid_mode = await client.post(
        "/aether/player-lessons/test",
        json={**_draft(), "sample_text": "I look around.", "narration_mode": "every_rpg_turn"},
    )
    assert invalid_mode.status_code == 422

    empty_sample = await client.post(
        "/aether/player-lessons/test",
        json={**_draft(), "sample_text": "   ", "narration_mode": "exploration"},
    )
    assert empty_sample.status_code == 422

    unknown_type = await client.post(
        "/aether/player-lessons",
        json=_draft(effect_type="world_rule"),
    )
    assert unknown_type.status_code == 422
    assert "effect_type must be" in unknown_type.json()["error"]

    missing_anchor = _intent_draft("placeholder")
    missing_anchor.pop("anchor_entry_id")
    rejected_missing_anchor = await client.post(
        "/aether/player-lessons",
        json=missing_anchor,
    )
    assert rejected_missing_anchor.status_code == 422
    assert "anchor_entry_id" in rejected_missing_anchor.json()["error"]

    anchorless_intent = await client.post(
        "/aether/player-lessons",
        json=_intent_draft(None),
    )
    assert anchorless_intent.status_code == 422

    mixed_intent = await client.post(
        "/aether/player-lessons",
        json={**_intent_draft("placeholder"), "do": "Execute this."},
    )
    assert mixed_intent.status_code == 422
    assert "unknown Player Lesson fields" in mixed_intent.json()["error"]

    mixed_narration = await client.post(
        "/aether/player-lessons",
        json={**_draft(), "misunderstanding": "Not a narration field."},
    )
    assert mixed_narration.status_code == 422
    assert "unknown Player Lesson fields" in mixed_narration.json()["error"]


@pytest.mark.asyncio
async def test_player_lessons_api_covers_intent_lifecycle_and_content_free_applications(
    client,
    proxy_app,
    cfg,
):
    playerlex = proxy_app.state.pipeline.playerlex_service
    service = proxy_app.state.pipeline.player_lessons_service
    anchor = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    draft = _intent_draft(anchor["entry_id"])

    created = await client.post("/aether/player-lessons", json=draft)
    assert created.status_code == 201
    lesson = created.json()["lesson"]
    assert lesson["effect_type"] == "intent_interpretation"
    assert lesson["intent_slot"] == "action"
    assert lesson["misunderstanding"] == draft["misunderstanding"]
    assert lesson["correct_interpretation"] == draft["correct_interpretation"]
    assert lesson["anchor_status"] == "current"
    assert lesson["provenance"]["approval"] == "explicit_local"
    assert "do" not in lesson and "avoid" not in lesson

    sample = "I Glass Read the rain-slick sigil."
    tested = await client.post(
        "/aether/player-lessons/test",
        json={**draft, "sample_text": sample, "narration_mode": "exploration"},
    )
    assert tested.status_code == 200
    result = tested.json()["result"]
    assert result["matched"] is True
    assert result["anchor_status"] == "current"
    assert result["application_stage"] == "after recognition, before contextual binding"
    assert result["test_kind"] == "retrieval_relevance_only"
    assert result["application_evaluated"] is False
    assert result["sample_stored"] is False
    assert sample not in "\n".join(proxy_app.state.store.db.iterdump())

    corrected = await client.patch(
        f"/aether/player-lessons/{lesson['lesson_id']}",
        json={
            **_intent_draft(
                anchor["entry_id"],
                title="Keep Glass Read on inspection",
                correct_interpretation="Prefer the approved inspection reading.",
            ),
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert corrected.status_code == 200
    lesson = corrected.json()["lesson"]
    assert lesson["revision"] == 2
    assert lesson["correct_interpretation"] == "Prefer the approved inspection reading."

    type_change = await client.patch(
        f"/aether/player-lessons/{lesson['lesson_id']}",
        json={
            **_draft(title="Cross-type replacement"),
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert type_change.status_code == 422

    disabled = await client.post(
        f"/aether/player-lessons/{lesson['lesson_id']}/enabled",
        json={
            "enabled": False,
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert disabled.status_code == 200
    lesson = disabled.json()["lesson"]
    assert lesson["enabled"] is False and lesson["revision"] == 3

    enabled = await client.post(
        f"/aether/player-lessons/{lesson['lesson_id']}/enabled",
        json={
            "enabled": True,
            "expected_revision": lesson["revision"],
            "expected_fingerprint": lesson["fingerprint"],
        },
    )
    assert enabled.status_code == 200
    lesson = enabled.json()["lesson"]
    assert lesson["enabled"] is True and lesson["revision"] == 4

    store = proxy_app.state.store
    session_id, branch_id = store.create_session(external_id="player-lessons-api-intent")
    source_start = sample.index("Glass Read")
    selected = service.select_intent(
        branch_id=branch_id,
        turn_index=0,
        user_hash="2" * 16,
        narration_mode="exploration",
        recognized_meanings=(
            {
                "lex_id": anchor["lex_id"],
                "concept_id": anchor["concept"]["concept_id"],
                "meaning_fingerprint": anchor["concept"]["meaning_fingerprint"],
                "source_start": source_start,
                "source_end": source_start + len("Glass Read"),
                "approval_source_id": (
                    "playerlex."
                    + anchor["entry_id"].removeprefix("playerlex_")
                    + f".r{anchor['provenance']['approval_revision']}"
                ),
            },
        ),
    )
    assert selected["schema"] == "player-lesson-intent-selection/1"
    assert [item["lesson_id"] for item in selected["selected"]] == [lesson["lesson_id"]]
    assert "misunderstanding" not in str(selected)
    assert "correct_interpretation" not in str(selected)

    application = service.record_intent_applications(
        branch_id,
        0,
        [
            {
                "lesson_id": lesson["lesson_id"],
                "lesson_revision": lesson["revision"],
                "lesson_fingerprint": lesson["fingerprint"],
                "applied": True,
                "reason": "exact_binding_applied",
                "frame_id": "t0.f1",
                "selected_value": "inspection",
                "meaning_binding_ref": "sha256:" + "a" * 64,
                "frame_fingerprint": "sha256:" + "b" * 64,
            }
        ],
    )
    assert application["selected"][0]["applied"] is True

    latest = await client.get(
        "/aether/player-lessons/applications",
        params={"session_id": session_id},
    )
    assert latest.status_code == 200
    payload = latest.json()
    assert payload["schema"] == "player-lesson-applications/1"
    assert len(payload["applications"]) == 1
    row = payload["applications"][0]
    assert row["schema"] == "player-lesson-intent-application/1"
    assert row["lesson_id"] == lesson["lesson_id"]
    assert row["applied"] is True
    assert row["reason"] == "exact_binding_applied"
    assert row["application_stage"] == "post_recognition_pre_contextual_binding"
    encoded = str(payload)
    assert sample not in encoded
    assert draft["misunderstanding"] not in encoded
    assert draft["correct_interpretation"] not in encoded
    assert "delivered" not in encoded
    assert (
        await client.get(
            "/aether/player-lessons/selections",
            params={"session_id": session_id},
        )
    ).json()["selections"] == []

    control_router = make_control_router(
        cfg,
        store,
        jobs=proxy_app.state.jobs,
        pipeline=proxy_app.state.pipeline,
    )
    application_routes = [
        route
        for route in control_router.routes
        if route.path == "/aether/player-lessons/applications"
    ]
    assert len(application_routes) == 1
    assert application_routes[0].methods == {"GET"}

    assert playerlex.remove(anchor["entry_id"])
    stale = next(
        item
        for item in (await client.get("/aether/player-lessons")).json()["lessons"]
        if item["lesson_id"] == lesson["lesson_id"]
    )
    assert stale["status"] == "stale"
    assert stale["anchor_status"] == "stale"

    removed = await client.delete(f"/aether/player-lessons/{lesson['lesson_id']}")
    assert removed.status_code == 200
    assert (
        await client.get(
            "/aether/player-lessons/applications",
            params={"session_id": session_id},
        )
    ).json()["applications"] == []
