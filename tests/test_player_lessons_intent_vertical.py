"""End-to-end Player Lesson intent correction through the real RPG turn pipeline."""
from __future__ import annotations

import json
import random

from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.state import apply_delta
from aetherstate.stamps import Stamp
from aetherstate.store import Store


def test_actual_service_resolves_one_existing_action_ambiguity_once() -> None:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.foe_floor = False
    cfg.specialization.war_room = False
    cfg.specialization.semantic_truth_gate = False
    store = Store(":memory:")
    external_id = "player-lesson-intent-vertical"
    session_id, branch_id = store.create_session(external_id=external_id)
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [
            {"op": "entity_add", "name": "Sava Orr", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Sava Orr",
                "card": {
                    "stats": {"CUN": 14},
                    "skills": {"glass_read": 2},
                    "abilities": [],
                    "resources": {},
                    "defs": {
                        "skills": {
                            "glass_read": {
                                "name": "Glass Read",
                                "keyed_stat": "CUN",
                                "governs": ["inspect"],
                            }
                        }
                    },
                },
            },
            {"op": "entity_add", "name": "Etched Panel", "kind": "object"},
            {"op": "presence", "entity": "Etched Panel", "present": True},
        ],
        "genesis",
        cfg,
    )
    assert len(seeded.applied) == 4, seeded.quarantined

    pipeline = Pipeline(
        store,
        SessionEngine(store, cfg.session),
        cfg,
        rng=random.Random(41),
    )
    inspect = pipeline.playerlex_service.approve(
        kind="alias",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.inspect",
    )
    pipeline.playerlex_service.approve(
        kind="name",
        surface="Glass Read",
        lex_id="action",
        concept_id="action.move",
    )
    private_marker = "PRIVATE_INTENT_EXPLANATION_7A19F2"
    lesson = pipeline.player_lessons_service.create(
        effect_type="intent_interpretation",
        title="Read Glass Read as inspection",
        scope="every_rpg_turn",
        misunderstanding=f"{private_marker}: it can be mistaken for movement.",
        correct_interpretation="Use the approved inspection meaning.",
        anchor_entry_id=inspect["entry_id"],
    )
    body = json.dumps(
        {
            "model": "player-lessons-intent-vertical",
            "messages": [
                {"role": "user", "content": "I use Glass Read on the etched panel."}
            ],
        }
    ).encode("utf-8")
    stamp = Stamp(
        session=external_id,
        turn=1,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Sava Orr",
    )

    packet, context = pipeline.process(stamp, body)
    duplicate_packet, duplicate_context = pipeline.process(stamp, body)

    assert packet == duplicate_packet
    assert duplicate_context.network_duplicate is True
    receipt_counts = {
        table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "player_lesson_intent_receipts",
            "player_lesson_intent_selection_items",
            "player_lesson_intent_applications",
        )
    }
    assert receipt_counts == {
        "player_lesson_intent_receipts": 1,
        "player_lesson_intent_selection_items": 1,
        "player_lesson_intent_applications": 1,
    }, store.diagnostic_turn(context.branch_id, context.turn_index)
    applications = pipeline.player_lessons_service.latest_intent_applications(session_id)
    assert len(applications) == 1
    application = applications[0]
    assert application["lesson_id"] == lesson["lesson_id"]
    assert application["application_stage"] == "post_recognition_pre_contextual_binding"
    assert application["intent_slot"] == "action"
    assert application["applied"] is True
    assert application["reason"] == "action_ambiguity_resolved"
    assert application["selected_value"] == "inspection"
    assert application["meaning_binding_ref"].startswith("sha256:")
    assert application["frame_fingerprint"].startswith("sha256:")
    assert context.player_lesson_intent_ids == (lesson["lesson_id"],)
    assert duplicate_context.player_lesson_intent_ids == context.player_lesson_intent_ids
    assert store.db.execute(
        "SELECT count(*) FROM player_lesson_intent_applications WHERE lesson_id=?",
        (lesson["lesson_id"],),
    ).fetchone()[0] == 1

    wire = packet.decode("utf-8")
    diagnostic = json.dumps(
        store.diagnostic_turn(context.branch_id, context.turn_index),
        ensure_ascii=False,
    )
    assert private_marker not in wire
    assert private_marker not in diagnostic
