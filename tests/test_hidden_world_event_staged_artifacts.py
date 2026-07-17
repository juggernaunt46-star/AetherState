"""Already-staged legacy prompt extras cannot disclose a hidden World Event cause."""

from __future__ import annotations

import json

from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta
from tests.test_hidden_world_event_cause_privacy import (
    CONSEQUENCE,
    SECRET_NAME,
    _add_front,
    _completion_ops,
    _runtime,
)


def test_pipeline_projects_staged_legacy_recall_and_direction_without_rewriting_them() -> None:
    cfg, store, session_id, branch_id, _world_id = _runtime("legacy-staged-extras")
    _add_front(cfg, store, session_id, branch_id)
    generated = _completion_ops(cfg, store, session_id, branch_id)
    committed = apply_delta(
        store, session_id, branch_id, 3, generated, "rule", cfg,
    )
    assert committed.applied and not committed.quarantined

    legacy = f"World event \N{EM DASH} {SECRET_NAME}: {CONSEQUENCE}"
    staged_recall = [f"- {legacy} (just now)"]
    staged_note = f"[Direction] Reinforce this fallout: {legacy}"
    store.write_recall(session_id, 4, staged_recall)
    store.write_note(session_id, 4, staged_note)

    pipeline = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    body = json.dumps({
        "model": "test-model",
        "messages": [{"role": "user", "content": "I inspect the sealed East Gate."}],
    }).encode()
    forwarded, context = pipeline.process(
        Stamp(
            session="legacy-staged-extras",
            turn=4,
            gen_type="normal",
            speaker="Dungeon Master",
            card_role="narrator",
            user="Kael",
        ),
        body,
    )
    assert context is not None
    wire = forwarded.decode()
    assert CONSEQUENCE in wire
    assert SECRET_NAME not in wire

    assert store.read_recall(session_id) == staged_recall
    assert store.read_note(session_id) == staged_note
