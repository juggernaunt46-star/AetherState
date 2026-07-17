from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

import aetherstate.player_lessons as player_lessons_module
from aetherstate.player_lessons import (
    MAX_LESSONS,
    PlayerLessons,
    PlayerLessonsConflictError,
    PlayerLessonsError,
    PlayerLessonsRetryableRemovalError,
    PlayerLessonsValidationError,
)
from aetherstate.playerlex import PlayerLex
from aetherstate.semantic_atlas import load_default_semantic_atlas
from aetherstate.store import Store


@pytest.fixture(scope="module")
def atlas():
    return load_default_semantic_atlas()


@pytest.fixture()
def services(atlas):
    store = Store(":memory:")
    playerlex = PlayerLex(store.db, atlas, store._lock)
    lessons = PlayerLessons(store.db, playerlex, store._lock)
    try:
        yield store, playerlex, lessons
    finally:
        store.db.close()


def _create(lessons: PlayerLessons, title: str = "Keep narration grounded", **overrides):
    values = {
        "effect_type": "narration_behavior",
        "title": title,
        "scope": "every_rpg_turn",
        "do_text": "Describe observable action clearly.",
        "avoid_text": "Do not invent a mechanical result.",
        "anchor_entry_id": None,
    }
    values.update(overrides)
    return lessons.create(**values)


def _identity(entry: dict) -> tuple[str, str, str]:
    return (
        entry["lex_id"],
        entry["concept"]["concept_id"],
        entry["concept"]["meaning_fingerprint"],
    )


def _intent_recognition(entry: dict, *, start: int = 2, end: int = 12) -> dict:
    revision = entry["provenance"]["approval_revision"]
    return {
        "lex_id": entry["lex_id"],
        "concept_id": entry["concept"]["concept_id"],
        "meaning_fingerprint": entry["concept"]["meaning_fingerprint"],
        "source_start": start,
        "source_end": end,
        "approval_source_id": (
            "playerlex."
            + entry["entry_id"].removeprefix("playerlex_")
            + f".r{revision}"
        ),
    }


def _create_intent(
    lessons: PlayerLessons,
    entry: dict,
    title: str = "Read Glass Read as inspection",
    **overrides,
):
    values = {
        "effect_type": "intent_interpretation",
        "title": title,
        "scope": "every_rpg_turn",
        "misunderstanding": "Treating the phrase as an unrelated action.",
        "correct_interpretation": "Use the approved inspection interpretation.",
        "anchor_entry_id": entry["entry_id"],
    }
    values.update(overrides)
    return lessons.create(**values)


def _selection(
    lessons: PlayerLessons,
    branch_id: str,
    turn_index: int,
    *,
    mode: str = "exploration",
    meanings=(),
    user_hash: str | None = None,
):
    return lessons.select(
        branch_id=branch_id,
        turn_index=turn_index,
        user_hash=user_hash or f"{turn_index:016x}",
        narration_mode=mode,
        recognized_meanings=meanings,
    )


def test_exact_separate_schema_is_verified_without_touching_playerlex_namespace():
    store = Store(":memory:")
    try:
        PlayerLessons(store.db, None, store._lock)
        objects = {
            (row["type"], row["name"], row["tbl_name"])
            for row in store.db.execute(
                """
                SELECT type, name, tbl_name FROM sqlite_master
                WHERE name GLOB 'player_lesson*' OR tbl_name GLOB 'player_lesson*'
                """
            )
        }
        assert {item[1] for item in objects} == {
            "player_lessons",
            "player_lesson_selection_receipts",
            "player_lesson_selection_items",
            "player_lesson_receipt_lesson_idx",
            "player_lesson_receipt_time_idx",
            "sqlite_autoindex_player_lessons_1",
            "sqlite_autoindex_player_lesson_selection_receipts_1",
            "sqlite_autoindex_player_lesson_selection_items_1",
            "sqlite_autoindex_player_lesson_selection_items_2",
            "player_lesson_intent_receipts",
            "player_lesson_intent_selection_items",
            "player_lesson_intent_applications",
            "player_lesson_intent_receipt_lesson_idx",
            "player_lesson_intent_receipt_time_idx",
            "player_lesson_intent_application_lesson_idx",
            "sqlite_autoindex_player_lesson_intent_receipts_1",
            "sqlite_autoindex_player_lesson_intent_selection_items_1",
            "sqlite_autoindex_player_lesson_intent_selection_items_2",
            "sqlite_autoindex_player_lesson_intent_applications_1",
            "sqlite_autoindex_player_lesson_intent_applications_2",
        }
        assert all(not name.startswith("playerlex_") for _kind, name, _table in objects)

        store.db.execute("DROP INDEX player_lesson_receipt_time_idx")
        store.db.commit()
        with pytest.raises(PlayerLessonsError, match="failed verification"):
            PlayerLessons(store.db, None, store._lock)
        assert (
            store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='player_lesson_receipt_time_idx'"
            ).fetchone()
            is None
        )
    finally:
        store.db.close()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"effect_type": "interpretation_correction"}, "narration_behavior"),
        ({"title": "x" * 121}, "at most 120"),
        ({"do_text": "x" * 1001}, "at most 1000"),
        ({"avoid_text": "x" * 1001}, "at most 1000"),
        ({"do_text": "", "avoid_text": ""}, "at least one"),
        ({"scope": "world_rule"}, "scope must be"),
    ],
)
def test_closed_type_and_bounded_definition_validation(services, overrides, message):
    _store, _playerlex, lessons = services
    with pytest.raises(PlayerLessonsValidationError, match=message):
        _create(lessons, **overrides)


def test_maximum_64_active_or_disabled_lessons_is_enforced():
    store = Store(":memory:")
    lessons = PlayerLessons(store.db, None, store._lock)
    try:
        for index in range(MAX_LESSONS):
            _create(lessons, title=f"Lesson {index}")
        assert len(lessons.list_lessons()) == MAX_LESSONS
        first = lessons.list_lessons()[0]
        lessons.set_enabled(
            first["lesson_id"],
            enabled=False,
            expected_revision=first["revision"],
            expected_fingerprint=first["fingerprint"],
        )
        with pytest.raises(PlayerLessonsValidationError, match="at most 64"):
            _create(lessons, title="One too many")
    finally:
        store.db.close()


def test_create_complete_correction_and_toggle_use_exact_optimistic_versions(services):
    _store, _playerlex, lessons = services
    created = _create(lessons)
    assert created["schema"] == "player-lesson/1"
    assert created["enabled"] is True
    assert created["revision"] == 1
    assert created["status"] == "current"
    assert created["anchor_status"] == "unanchored"
    assert created["provenance"]["approval"] == "explicit_local"
    assert created["provenance"]["approved_via"] == "local_control_api"

    corrected = lessons.correct(
        created["lesson_id"],
        effect_type="narration_behavior",
        title="Use tighter prose",
        scope="combat_exchange",
        do_text="Use short physical sentences.",
        avoid_text="Avoid summarizing the exchange.",
        anchor_entry_id=None,
        expected_revision=created["revision"],
        expected_fingerprint=created["fingerprint"],
    )
    assert corrected["revision"] == 2
    assert corrected["fingerprint"] != created["fingerprint"]
    assert corrected["title"] == "Use tighter prose"
    with pytest.raises(PlayerLessonsConflictError, match="changed since"):
        lessons.correct(
            created["lesson_id"],
            effect_type="narration_behavior",
            title="Stale tab",
            scope="exploration",
            do_text="No.",
            avoid_text="",
            expected_revision=created["revision"],
            expected_fingerprint=created["fingerprint"],
        )

    disabled = lessons.set_enabled(
        corrected["lesson_id"],
        enabled=False,
        expected_revision=corrected["revision"],
        expected_fingerprint=corrected["fingerprint"],
    )
    assert (disabled["enabled"], disabled["revision"], disabled["status"]) == (
        False,
        3,
        "disabled",
    )
    assert disabled["fingerprint"] != corrected["fingerprint"]
    with pytest.raises(PlayerLessonsConflictError, match="changed since"):
        lessons.set_enabled(
            corrected["lesson_id"],
            enabled=True,
            expected_revision=corrected["revision"],
            expected_fingerprint=corrected["fingerprint"],
        )
    enabled = lessons.set_enabled(
        disabled["lesson_id"],
        enabled=True,
        expected_revision=disabled["revision"],
        expected_fingerprint=disabled["fingerprint"],
    )
    assert enabled["enabled"] is True and enabled["revision"] == 4


def test_optional_anchor_supports_all_four_lexes_and_is_dynamically_stale(services):
    store, playerlex, lessons = services
    approved = [
        playerlex.approve(
            kind="alias",
            surface="Quiet Fold",
            lex_id="capability",
            concept_id="skill.stealth",
        ),
        playerlex.approve(
            kind="alias",
            surface="Crownward",
            lex_id="referent",
            concept_id="referent.body_part.head",
        ),
        playerlex.approve(
            kind="alias",
            surface="Insideward",
            lex_id="scene",
            concept_id="scene.location.containment",
        ),
        playerlex.approve(
            kind="alias",
            surface="Glass Read",
            lex_id="action",
            concept_id="action.inspect",
        ),
    ]
    stored = [
        _create(
            lessons,
            title=f"Anchor {entry['lex_id']}",
            scope="exploration",
            anchor_entry_id=entry["entry_id"],
        )
        for entry in approved
    ]
    assert {lesson["anchor"]["lex_id"] for lesson in stored} == {
        "capability",
        "referent",
        "scene",
        "action",
    }
    assert all(lesson["anchor_status"] == "current" for lesson in stored)

    _session_id, branch_id = store.create_session()
    selected = _selection(
        lessons,
        branch_id,
        0,
        meanings=[_identity(entry) for entry in approved],
    )
    assert selected["frozen_selected_count"] == 4
    assert {item["anchor"]["lex_id"] for item in selected["selected"]} == {
        "capability",
        "referent",
        "scene",
        "action",
    }

    removed_entry = approved[-1]
    removed_lesson = next(item for item in stored if item["anchor"]["entry_id"] == removed_entry["entry_id"])
    assert playerlex.remove(removed_entry["entry_id"])
    stale = next(item for item in lessons.list_lessons() if item["lesson_id"] == removed_lesson["lesson_id"])
    assert stale["status"] == "stale"
    assert stale["anchor_status"] == "stale"
    assert stale["stale_reason"] == "anchor_entry_removed"
    replayed = lessons.rehydrate(branch_id, 0)
    assert replayed is not None
    assert removed_lesson["lesson_id"] not in {item["lesson_id"] for item in replayed["selected"]}


def test_playerlex_unavailable_keeps_unanchored_lessons_live_and_anchors_visible(services):
    store, playerlex, lessons = services
    anchor = playerlex.approve(
        kind="alias",
        surface="Detail Sight",
        lex_id="action",
        concept_id="action.inspect",
    )
    anchored = _create(lessons, title="Anchored", anchor_entry_id=anchor["entry_id"])
    unanchored = _create(lessons, title="Unanchored")
    unavailable = PlayerLessons(store.db, None, store._lock)
    listed = {item["lesson_id"]: item for item in unavailable.list_lessons()}
    assert listed[anchored["lesson_id"]]["anchor_status"] == "unavailable"
    assert listed[anchored["lesson_id"]]["status"] == "stale"
    assert listed[unanchored["lesson_id"]]["selectable"] is True

    _session_id, branch_id = store.create_session()
    selected = _selection(unavailable, branch_id, 0)
    assert [item["lesson_id"] for item in selected["selected"]] == [unanchored["lesson_id"]]


def test_draft_uses_shared_fabric_and_playerlex_overlay_without_storing_sample(services, monkeypatch):
    store, playerlex, lessons = services
    anchor = playerlex.approve(
        kind="alias",
        surface="Night Fold",
        lex_id="action",
        concept_id="action.inspect",
    )
    request = {
        "effect_type": "narration_behavior",
        "title": "Keep inspection concrete",
        "scope": "exploration",
        "do_text": "Name visible details.",
        "avoid_text": "Avoid invented conclusions.",
        "anchor_entry_id": anchor["entry_id"],
        "narration_mode": "exploration",
    }
    shared = lessons.test_draft(sample_text="I inspect the panel carefully.", **request)
    overlay = lessons.test_draft(sample_text="I use Night Fold on the panel.", **request)
    unrelated = lessons.test_draft(sample_text="I wait by the panel.", **request)
    assert shared["matched"] is True and shared["reason"] == "scope_and_anchor_match"
    assert overlay["matched"] is True and overlay["reason"] == "scope_and_anchor_match"
    assert unrelated["matched"] is False and unrelated["reason"] == "anchor_not_recognized"
    assert shared["sample_stored"] is False
    assert "sample_text" not in shared
    assert store.db.execute("SELECT count(*) FROM player_lessons").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM player_lesson_selection_receipts").fetchone()[0] == 0

    wrong = (anchor["lex_id"], anchor["concept"]["concept_id"], "sha256:" + "f" * 64)
    monkeypatch.setattr(
        lessons,
        "_compiled_meaning_identities",
        lambda _compiled, _fabric: frozenset({wrong}),
    )
    mismatch = lessons.test_draft(sample_text="I inspect the panel carefully.", **request)
    assert mismatch["matched"] is False
    assert mismatch["reason"] == "anchor_not_recognized"


def test_draft_shared_compile_runs_without_playerlex_but_anchor_fails_unavailable(services):
    store, playerlex, lessons = services
    anchor = playerlex.approve(
        kind="alias",
        surface="Trace View",
        lex_id="action",
        concept_id="action.inspect",
    )
    _create(lessons, title="Stored anchor", anchor_entry_id=anchor["entry_id"])
    unavailable = PlayerLessons(store.db, None, store._lock)
    result = unavailable.test_draft(
        effect_type="narration_behavior",
        title="Stored anchor",
        scope="exploration",
        do_text="Show evidence.",
        avoid_text="",
        anchor_entry_id=anchor["entry_id"],
        sample_text="I inspect the evidence.",
        narration_mode="exploration",
    )
    assert result["matched"] is False
    assert result["reason"] == "anchor_unavailable"
    assert result["anchor_status"] == "unavailable"


def test_selection_is_max_five_newest_updated_then_id_and_receipts_store_no_content(services, monkeypatch):
    store, _playerlex, lessons = services
    marker = "PRIVATE_LESSON_CONTENT_6E9C1A"
    monkeypatch.setattr("aetherstate.player_lessons.time.time", lambda: 1000.0)
    created = [
        _create(
            lessons,
            title=f"Tie {index}",
            do_text=marker if index == 0 else f"Instruction {index}",
            avoid_text="",
        )
        for index in range(6)
    ]
    expected = sorted(item["lesson_id"] for item in created)[:5]
    _session_id, branch_id = store.create_session()
    selected = _selection(lessons, branch_id, 0)
    assert selected["frozen_selected_count"] == 5
    assert [item["lesson_id"] for item in selected["selected"]] == expected

    receipt_rows = [
        dict(row)
        for table in ("player_lesson_selection_receipts", "player_lesson_selection_items")
        for row in store.db.execute(f"SELECT * FROM {table}")
    ]
    encoded = json.dumps(receipt_rows, ensure_ascii=False)
    assert marker not in encoded
    assert "Instruction" not in encoded
    assert "Tie " not in encoded


def test_frozen_zero_and_duplicate_selection_never_rerank(services):
    store, _playerlex, lessons = services
    _create(lessons, title="Combat only", scope="combat_exchange")
    _session_id, branch_id = store.create_session()
    empty = _selection(lessons, branch_id, 0, mode="exploration", user_hash="a" * 16)
    assert empty["frozen_selected_count"] == 0
    assert empty["selected"] == []

    _create(lessons, title="Added later", scope="exploration")
    duplicate = _selection(
        lessons,
        branch_id,
        0,
        mode="exploration",
        user_hash="a" * 16,
    )
    assert duplicate["frozen_selected_count"] == 0
    assert duplicate["selected"] == []
    assert (
        store.db.execute(
            "SELECT count(*) FROM player_lesson_selection_receipts WHERE branch_id=?",
            (branch_id,),
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(PlayerLessonsConflictError, match="different input hash"):
        _selection(
            lessons,
            branch_id,
            0,
            mode="exploration",
            user_hash="b" * 16,
        )
    with pytest.raises(PlayerLessonsValidationError, match="action digest"):
        _selection(
            lessons,
            branch_id,
            1,
            user_hash="raw private Player prose",
        )


def test_select_is_savepoint_safe_inside_store_transaction_and_outer_rollback_wins(services):
    store, _playerlex, lessons = services
    _create(lessons)
    _session_id, branch_id = store.create_session()

    with pytest.raises(RuntimeError, match="rollback probe"):
        with store.transaction():
            selected = _selection(lessons, branch_id, 0)
            assert selected["available_count"] == 1
            assert store.db.in_transaction
            raise RuntimeError("rollback probe")
    assert lessons.rehydrate(branch_id, 0) is None

    with store.transaction():
        selected = _selection(lessons, branch_id, 0)
        assert selected["available_count"] == 1
    assert lessons.rehydrate(branch_id, 0)["available_count"] == 1


def test_exact_rehydrate_omits_changed_disabled_removed_and_stale_revisions(services):
    store, playerlex, lessons = services
    anchor_entry = playerlex.approve(
        kind="alias",
        surface="Trace Sight",
        lex_id="action",
        concept_id="action.inspect",
    )
    changed = _create(lessons, title="Changed")
    disabled = _create(lessons, title="Disabled")
    removed = _create(lessons, title="Removed")
    _create(lessons, title="Stale", anchor_entry_id=anchor_entry["entry_id"])
    _session_id, branch_id = store.create_session()
    original = _selection(lessons, branch_id, 0, meanings=[_identity(anchor_entry)])
    assert original["frozen_selected_count"] == 4

    lessons.correct(
        changed["lesson_id"],
        effect_type="narration_behavior",
        title="Changed revision",
        scope="every_rpg_turn",
        do_text="New text.",
        avoid_text="",
        expected_revision=changed["revision"],
        expected_fingerprint=changed["fingerprint"],
    )
    lessons.set_enabled(
        disabled["lesson_id"],
        enabled=False,
        expected_revision=disabled["revision"],
        expected_fingerprint=disabled["fingerprint"],
    )
    assert lessons.remove(removed["lesson_id"])
    assert playerlex.remove(anchor_entry["entry_id"])

    replayed = lessons.rehydrate(branch_id, 0)
    assert replayed is not None
    assert replayed["selected"] == []
    assert replayed["frozen_selected_count"] == 4
    assert replayed["omitted_count"] == 4
    assert (
        store.db.execute(
            "SELECT count(*) FROM player_lesson_selection_items WHERE lesson_id=?",
            (removed["lesson_id"],),
        ).fetchone()[0]
        == 0
    )


def test_delivery_and_latest_session_selection_are_content_free(services):
    store, _playerlex, lessons = services
    lesson = _create(lessons, title="Delivery marker")
    session_id, branch_id = store.create_session()
    selected = _selection(lessons, branch_id, 0)
    assert selected["selected"][0]["delivered"] is False
    delivered = lessons.mark_delivered(branch_id, 0, [lesson["lesson_id"]])
    assert delivered["selected"][0]["delivered"] is True
    latest = lessons.latest_selections(session_id)
    assert len(latest) == 1
    assert latest[0]["schema"] == "player-lesson-selection-item/1"
    assert latest[0]["lesson_id"] == lesson["lesson_id"]
    assert latest[0]["delivered"] is True
    assert "title" not in latest[0] and "do" not in latest[0] and "avoid" not in latest[0]


def test_ancestor_fallback_maps_message_position_to_turn_and_stops_after_fork_prefix(services):
    store, _playerlex, lessons = services
    lesson = _create(lessons, title="Fork-stable")
    session_id, parent = store.create_session()
    store.append_msgs(
        parent,
        0,
        [
            ("user", "u0", "c0"),
            ("assistant", "a0", "c1"),
            ("user", "u1", "c2"),
        ],
    )
    # Deliberately non-zero-based source turns prove forked_at is not a turn index.
    store.record_turn(parent, 5, "new_turn", "normal")
    store.record_turn(parent, 6, "new_turn", "normal")
    first = _selection(lessons, parent, 5, user_hash="5" * 16)
    _selection(lessons, parent, 6, user_hash="6" * 16)
    lessons.mark_delivered(parent, 5, [lesson["lesson_id"]])
    assert first["frozen_selected_count"] == 1

    child = "child_branch_for_player_lessons"
    with store.transaction():
        store.db.execute(
            """
            INSERT INTO branches(branch_id, session_id, parent_branch, forked_at, head_turn)
            VALUES(?,?,?,?,?)
            """,
            (child, session_id, parent, 1, 5),
        )
    inherited = lessons.rehydrate(child, 5)
    assert inherited is not None
    assert inherited["inherited_from_branch_id"] == parent
    assert inherited["input_hash"] == "5" * 16
    assert inherited["selected"][0]["delivered"] is False
    assert lessons.rehydrate(child, 6) is None


def test_exact_v1_migration_preserves_narration_definition_and_frozen_receipt():
    store = Store(":memory:")
    try:
        for statement in player_lessons_module._V1_SCHEMA_STATEMENTS:
            store.db.execute(statement)
        now = 1700000000.0
        values = {
            "lesson_id": "lesson_" + "a" * 32,
            "effect_type": "narration_behavior",
            "title": "Preserve this narration lesson",
            "scope": "exploration",
            "do_text": "Keep the old bytes.",
            "avoid_text": "Do not rewrite this row.",
            "anchor_entry_id": None,
            "anchor_lex_id": None,
            "anchor_concept_id": None,
            "anchor_meaning_fingerprint": None,
            "enabled": 1,
            "revision": 3,
            "fingerprint": "",
            "approved_via": "local_control_api",
            "approved_at": now,
            "created_at": now - 10,
            "updated_at": now,
        }
        values["fingerprint"] = PlayerLessons._definition_fingerprint(values)
        store.db.execute(
            f"""
            INSERT INTO player_lessons({", ".join(player_lessons_module._LESSON_COLUMNS)})
            VALUES({", ".join("?" for _ in player_lessons_module._LESSON_COLUMNS)})
            """,
            tuple(values[column] for column in player_lessons_module._LESSON_COLUMNS),
        )
        store.db.execute(
            """
            INSERT INTO player_lesson_selection_receipts(
                branch_id, turn_index, input_hash, narration_mode, frozen_selected_count,
                inherited_from_branch_id, selected_at
            ) VALUES('old-branch', 2, 'old-hash', 'exploration', 1, NULL, ?)
            """,
            (now,),
        )
        store.db.execute(
            """
            INSERT INTO player_lesson_selection_items(
                branch_id, turn_index, position, lesson_id, lesson_revision,
                lesson_fingerprint, reason, scope, anchor_entry_id, anchor_lex_id,
                anchor_concept_id, anchor_meaning_fingerprint, delivered, delivered_at
            ) VALUES('old-branch', 2, 0, ?, 3, ?, 'scope_match', 'exploration',
                     NULL, NULL, NULL, NULL, 0, NULL)
            """,
            (values["lesson_id"], values["fingerprint"]),
        )
        store.db.commit()

        lessons = PlayerLessons(store.db, None, store._lock)
        migrated = lessons.list_lessons()
        assert len(migrated) == 1
        assert migrated[0]["fingerprint"] == values["fingerprint"]
        assert migrated[0]["revision"] == 3
        replayed = lessons.rehydrate("old-branch", 2)
        assert replayed is not None
        assert replayed["input_hash"] == hashlib.blake2b(
            b"old-hash", digest_size=8
        ).hexdigest()
        assert replayed["selected"][0]["lesson_fingerprint"] == values["fingerprint"]
        assert {
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'player_lesson_intent*'"
            )
        } == {
            "player_lesson_intent_receipts",
            "player_lesson_intent_selection_items",
            "player_lesson_intent_applications",
        }
    finally:
        store.db.close()


def test_unknown_v1_shape_fails_closed_without_partial_migration():
    store = Store(":memory:")
    try:
        for statement in player_lessons_module._V1_SCHEMA_STATEMENTS:
            store.db.execute(statement)
        store.db.execute("ALTER TABLE player_lessons ADD COLUMN foreign_column TEXT")
        store.db.commit()
        with pytest.raises(PlayerLessonsError, match="failed verification"):
            PlayerLessons(store.db, None, store._lock)
        columns = [row[1] for row in store.db.execute("PRAGMA table_info(player_lessons)")]
        assert columns[-1] == "foreign_column"
        assert (
            store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='player_lesson_intent_receipts'"
            ).fetchone()
            is None
        )
    finally:
        store.db.close()


def test_intent_definition_has_dedicated_fields_required_exact_anchor_and_locked_type(services):
    _store, playerlex, lessons = services
    action = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    referent = playerlex.approve(
        kind="alias",
        surface="Crownward",
        lex_id="referent",
        concept_id="referent.body_part.head",
    )
    scene = playerlex.approve(
        kind="alias",
        surface="Insideward",
        lex_id="scene",
        concept_id="scene.location.containment",
    )
    action_lesson = _create_intent(lessons, action)
    target_lesson = _create_intent(lessons, referent, title="Interpret Crownward as the target")
    assert action_lesson["schema"] == "player-lesson-intent/1"
    assert action_lesson["intent_slot"] == "action"
    assert target_lesson["intent_slot"] == "target"
    assert action_lesson["misunderstanding"].startswith("Treating")
    assert action_lesson["correct_interpretation"].startswith("Use the approved")
    assert "do" not in action_lesson and "avoid" not in action_lesson

    corrected = lessons.correct(
        action_lesson["lesson_id"],
        effect_type="intent_interpretation",
        title="Keep Glass Read on inspection",
        scope="exploration",
        misunderstanding="Reading it as an attack.",
        correct_interpretation="Read it as inspection.",
        anchor_entry_id=action["entry_id"],
        expected_revision=action_lesson["revision"],
        expected_fingerprint=action_lesson["fingerprint"],
    )
    assert corrected["revision"] == 2
    with pytest.raises(PlayerLessonsValidationError, match="cannot change"):
        lessons.correct(
            corrected["lesson_id"],
            effect_type="narration_behavior",
            title="Cross type",
            scope="exploration",
            do_text="No.",
            avoid_text="",
            expected_revision=corrected["revision"],
            expected_fingerprint=corrected["fingerprint"],
        )
    with pytest.raises(PlayerLessonsValidationError, match="requires one current"):
        lessons.create(
            effect_type="intent_interpretation",
            title="Missing anchor",
            scope="exploration",
            misunderstanding="Wrong.",
            correct_interpretation="Right.",
        )
    with pytest.raises(PlayerLessonsValidationError, match="ActionLex or ReferentLex"):
        _create_intent(lessons, scene, title="Unsupported scene intent")


def test_intent_draft_uses_exact_playerlex_recognition_and_stores_no_sample(services):
    store, playerlex, lessons = services
    action = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    request = {
        "effect_type": "intent_interpretation",
        "title": "Interpret Glass Read",
        "scope": "exploration",
        "misunderstanding": "Reading it as an attack.",
        "correct_interpretation": "Read it as inspection.",
        "anchor_entry_id": action["entry_id"],
        "narration_mode": "exploration",
    }
    sample = "I Glass Read the rain-slick sigil."
    matched = lessons.test_draft(sample_text=sample, **request)
    missed = lessons.test_draft(sample_text="I wait beside the sigil.", **request)
    assert matched["schema"] == "player-lesson-intent-test/1"
    assert matched["matched"] is True
    assert matched["reason"] == "scope_and_exact_anchor_recognition"
    assert matched["intent_slot"] == "action"
    assert matched["stage"] == "post_recognition_pre_contextual_binding"
    assert matched["application_stage"] == "after recognition, before contextual binding"
    assert matched["test_kind"] == "retrieval_relevance_only"
    assert matched["application_evaluated"] is False
    assert matched["sample_stored"] is False and "sample_text" not in matched
    assert missed["matched"] is False and missed["reason"] == "anchor_not_recognized"
    dump = "\n".join(store.db.iterdump())
    assert sample not in dump
    assert store.db.execute("SELECT count(*) FROM player_lessons").fetchone()[0] == 0


def test_intent_selection_application_latest_and_removal_are_separate_and_content_free(services):
    store, playerlex, lessons = services
    action = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    marker = "PRIVATE_INTENT_LESSON_8D24AB"
    lesson = _create_intent(
        lessons,
        action,
        misunderstanding=marker,
        correct_interpretation="Prefer inspection.",
    )
    session_id, branch_id = store.create_session()
    recognition = _intent_recognition(action, start=2, end=12)
    selected = lessons.select_intent(
        branch_id=branch_id,
        turn_index=0,
        user_hash="7" * 16,
        narration_mode="exploration",
        recognized_meanings=(recognition,),
    )
    assert selected["schema"] == "player-lesson-intent-selection/1"
    assert selected["frozen_selected_count"] == 1
    assert selected["selected"][0]["intent_slot"] == "action"
    assert selected["selected"][0]["source_span"] == {"start": 2, "end": 12}
    assert selected["selected"][0]["approval_source_id"] == recognition["approval_source_id"]
    encoded = json.dumps(selected, ensure_ascii=False)
    assert marker not in encoded and "Prefer inspection" not in encoded
    assert "delivered" not in encoded

    application_request = {
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
    applied = lessons.record_intent_applications(branch_id, 0, [application_request])
    assert applied["selected"][0]["applied"] is True
    assert lessons.record_intent_applications(branch_id, 0, [application_request]) == applied
    with pytest.raises(PlayerLessonsConflictError, match="immutable"):
        lessons.record_intent_applications(
            branch_id,
            0,
            [{**application_request, "selected_value": "different"}],
        )

    latest = lessons.latest_intent_applications(session_id)
    assert len(latest) == 1
    assert latest[0]["schema"] == "player-lesson-intent-application/1"
    assert latest[0]["application_stage"] == "post_recognition_pre_contextual_binding"
    assert latest[0]["applied"] is True
    assert lessons.latest_selections(session_id) == []
    raw = json.dumps(
        [
            dict(row)
            for table in (
                "player_lesson_intent_receipts",
                "player_lesson_intent_selection_items",
                "player_lesson_intent_applications",
            )
            for row in store.db.execute(f"SELECT * FROM {table}")
        ],
        ensure_ascii=False,
    )
    assert marker not in raw and "Prefer inspection" not in raw and "delivered" not in raw

    assert lessons.remove(lesson["lesson_id"])
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_intent_receipts WHERE branch_id=?",
        (branch_id,),
    ).fetchone()[0] == 1
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_intent_selection_items WHERE lesson_id=?",
        (lesson["lesson_id"],),
    ).fetchone()[0] == 0
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_intent_applications WHERE lesson_id=?",
        (lesson["lesson_id"],),
    ).fetchone()[0] == 0
    replayed = lessons.rehydrate_intent(branch_id, 0)
    assert replayed is not None
    assert replayed["frozen_selected_count"] == 1
    assert replayed["selected"] == [] and replayed["omitted_count"] == 1


def test_intent_frozen_zero_duplicate_and_nonapplication_reason_never_rerank(services):
    store, playerlex, lessons = services
    action = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    lesson = _create_intent(lessons, action)
    _session_id, branch_id = store.create_session()
    frozen = lessons.select_intent(
        branch_id=branch_id,
        turn_index=0,
        user_hash="8" * 16,
        narration_mode="exploration",
        recognized_meanings=(),
    )
    assert frozen["frozen_selected_count"] == 0 and frozen["selected"] == []
    duplicate = lessons.select_intent(
        branch_id=branch_id,
        turn_index=0,
        user_hash="8" * 16,
        narration_mode="exploration",
        recognized_meanings=(_intent_recognition(action),),
    )
    assert duplicate["frozen_selected_count"] == 0 and duplicate["selected"] == []
    with pytest.raises(PlayerLessonsConflictError, match="different input hash"):
        lessons.select_intent(
            branch_id=branch_id,
            turn_index=0,
            user_hash="9" * 16,
            narration_mode="exploration",
            recognized_meanings=(),
        )

    second = lessons.select_intent(
        branch_id=branch_id,
        turn_index=1,
        user_hash="a" * 16,
        narration_mode="exploration",
        recognized_meanings=(_intent_recognition(action),),
    )
    not_applied = lessons.record_intent_applications(
        branch_id,
        1,
        [
            {
                "lesson_id": lesson["lesson_id"],
                "lesson_revision": lesson["revision"],
                "lesson_fingerprint": lesson["fingerprint"],
                "applied": False,
                "reason": "input_unambiguous",
            }
        ],
    )
    assert second["selected"][0]["applied"] is None
    assert not_applied["selected"][0]["applied"] is False
    assert not_applied["selected"][0]["application_reason"] == "input_unambiguous"


def test_intent_ancestor_replay_clones_selection_and_application_without_reranking(services):
    store, playerlex, lessons = services
    action = playerlex.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    lesson = _create_intent(lessons, action)
    session_id, parent = store.create_session()
    store.append_msgs(parent, 0, [("user", "u0", "c0")])
    store.record_turn(parent, 5, "new_turn", "normal")
    selected = lessons.select_intent(
        branch_id=parent,
        turn_index=5,
        user_hash="b" * 16,
        narration_mode="exploration",
        recognized_meanings=(_intent_recognition(action),),
    )
    lessons.record_intent_applications(
        parent,
        5,
        [
            {
                "lesson_id": lesson["lesson_id"],
                "lesson_revision": lesson["revision"],
                "lesson_fingerprint": lesson["fingerprint"],
                "applied": True,
                "reason": "action_ambiguity_resolved",
                "frame_id": "t5.f1",
                "selected_value": "inspection",
                "meaning_binding_ref": "sha256:" + "c" * 64,
                "frame_fingerprint": "sha256:" + "d" * 64,
            }
        ],
    )
    assert selected["frozen_selected_count"] == 1
    child = "child_branch_for_intent_lessons"
    with store.transaction():
        store.db.execute(
            """
            INSERT INTO branches(branch_id, session_id, parent_branch, forked_at, head_turn)
            VALUES(?,?,?,?,?)
            """,
            (child, session_id, parent, 1, 5),
        )
    inherited = lessons.rehydrate_intent(child, 5)
    assert inherited is not None
    assert inherited["inherited_from_branch_id"] == parent
    assert inherited["selected"][0]["applied"] is True
    assert inherited["selected"][0]["application_reason"] == "action_ambiguity_resolved"
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_intent_applications WHERE branch_id=?",
        (child,),
    ).fetchone()[0] == 1


class _CheckpointFailingConnection(sqlite3.Connection):
    checkpoint_calls: int
    fail_checkpoint_call: int

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checkpoint_calls = 0
        self.fail_checkpoint_call = 0

    def execute(self, sql, parameters=()):
        if " ".join(sql.split()).casefold() == "pragma wal_checkpoint(truncate)":
            self.checkpoint_calls += 1
            if self.checkpoint_calls == self.fail_checkpoint_call:
                raise sqlite3.OperationalError("simulated checkpoint contention")
        return super().execute(sql, parameters)


def test_secure_removal_scrubs_content_and_retry_finishes_post_commit_checkpoint(tmp_path: Path):
    path = tmp_path / "secure-player-lessons.sqlite3"
    wal_path = Path(str(path) + "-wal")
    marker = "PLAYER_LESSON_PRIVATE_DELETE_2C7D91E4"
    marker_bytes = marker.encode("utf-8")
    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        factory=_CheckpointFailingConnection,
    )
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        lessons = PlayerLessons(connection, None, threading.RLock())
        lesson = _create(
            lessons,
            title=marker,
            do_text=marker,
            avoid_text="",
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert marker_bytes in path.read_bytes()
        connection.checkpoint_calls = 0
        connection.fail_checkpoint_call = 2

        with pytest.raises(PlayerLessonsRetryableRemovalError, match="retry") as failed:
            lessons.remove(lesson["lesson_id"])
        assert failed.value.lesson_id == lesson["lesson_id"]
        assert connection.execute("SELECT count(*) FROM player_lessons").fetchone()[0] == 0

        connection.fail_checkpoint_call = 0
        assert lessons.remove(lesson["lesson_id"])
        assert lessons.remove(lesson["lesson_id"])
        assert marker_bytes not in path.read_bytes()
        assert not wal_path.exists() or marker_bytes not in wal_path.read_bytes()
    finally:
        connection.close()


def test_secure_removal_refuses_outer_transaction(services):
    store, _playerlex, lessons = services
    lesson = _create(lessons)
    store.db.execute("BEGIN")
    try:
        with pytest.raises(PlayerLessonsError, match="active transaction"):
            lessons.remove(lesson["lesson_id"])
    finally:
        store.db.rollback()
    assert any(item["lesson_id"] == lesson["lesson_id"] for item in lessons.list_lessons())
