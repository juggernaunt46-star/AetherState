"""Semantic action-frame reducer authority and replay fixtures.

These tests deliberately exercise the state boundary directly.  Tier-0 production is covered by
the semantic vertical fixtures; this file proves that a baked frame is ordered, retained, and
enforced without reparsing source prose during apply or replay.
"""
from __future__ import annotations

import pytest

from aetherstate.config import Config
from aetherstate.semantic import ActionFrame
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _runtime(tag: str = "semantic-frame-state"):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id=tag)
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "entity_add", "name": "Kael", "kind": "player"}],
        "user",
        cfg,
    )
    assert not seeded.quarantined
    return cfg, store, sid, bid


def _snapshot(
    source: str = "I sneak past the sentry.",
    *,
    frame_id: str = "f1",
    polarity: str = "positive",
    modality: str = "actual",
    time_scope: str = "current",
    ambiguity: tuple[str, ...] = (),
) -> dict:
    frame = ActionFrame(
        frame_id=frame_id,
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="kael",
        capability_id="stealth",
        action_class="skill_check",
        polarity=polarity,
        modality=modality,
        time_scope=time_scope,
        ambiguity=list(ambiguity),
    )
    frame.add_evidence("capability", 0, len(source), "stealth")
    return frame.snapshot(source)


def _check(ref: str | None = None) -> dict:
    op = {
        "op": "check",
        "skill": "stealth",
        "result": 10,
        "tier": "success",
        "char": "kael",
        "_mod": 0,
        "_dice": "2d6",
    }
    if ref is not None:
        op["_semantic_frame_ref"] = ref
    return op


def _v2_snapshot(source: str = "I sneak past the sentry.") -> tuple[dict, dict]:
    meaning = load_default_semantic_fabric().translate(source).receipt_dict()
    frame = ActionFrame(
        frame_id="f1",
        clause_index=0,
        start=0,
        end=len(source),
        actor_id="kael",
        capability_id="stealth",
        action_class="skill_check",
        polarity="positive",
        modality="actual",
        time_scope="current",
        genre_ids=tuple(meaning["genre_ids"]),
        meaning_ref=meaning["fingerprint"],
        fabric_fingerprint=meaning["fabric_fingerprint"],
    )
    frame.add_evidence("capability", 2, 7, "stealth")
    return frame.snapshot(source), meaning


def test_commit_is_ordered_before_a_dependent_op_in_the_same_batch():
    cfg, store, sid, bid = _runtime("semantic-order")
    frame = _snapshot()

    # Submit the dependent mechanic first: deterministic family ordering must still commit the
    # frame before the check attempts to resolve its reference.
    result = apply_delta(
        store,
        sid,
        bid,
        4,
        [_check(frame["fingerprint"]), {"op": "semantic_frame_commit", "frame": frame}],
        "rule",
        cfg,
    )

    assert not result.quarantined
    assert [op["op"] for op in result.applied] == ["semantic_frame_commit", "check"]
    assert result.state["semantic_frames"] == [{"turn": 4, "frame": frame}]
    assert result.state["rolls"][-1]["skill"] == "stealth"


def test_v2_meaning_is_ordered_before_frame_and_dependent_mechanic():
    cfg, store, sid, bid = _runtime("semantic-v2-order")
    frame, meaning = _v2_snapshot()

    result = apply_delta(
        store,
        sid,
        bid,
        4,
        [
            _check(frame["fingerprint"]),
            {"op": "semantic_frame_commit", "frame": frame},
            {"op": "semantic_meaning_commit", "meaning": meaning},
        ],
        "rule",
        cfg,
    )

    assert not result.quarantined
    assert [op["op"] for op in result.applied] == [
        "semantic_meaning_commit",
        "semantic_frame_commit",
        "check",
    ]
    assert result.state["semantic_meanings"] == [{"turn": 4, "meaning": meaning}]
    assert result.state["semantic_frames"] == [{"turn": 4, "frame": frame}]


def test_v2_frame_without_same_turn_meaning_cannot_commit_or_execute():
    cfg, store, sid, bid = _runtime("semantic-v2-missing-meaning")
    frame, _meaning = _v2_snapshot()

    result = apply_delta(
        store,
        sid,
        bid,
        5,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            _check(frame["fingerprint"]),
        ],
        "rule",
        cfg,
    )

    assert not result.applied
    assert "semantic_frames" not in result.state
    assert result.state["rolls"] == []
    assert [row["reason"] for row in result.quarantined] == [
        "semantic action frame has no committed meaning receipt",
        "semantic action frame reference has no committed frame ledger",
    ]


def test_forged_v2_meaning_cannot_authorize_a_valid_frame():
    cfg, store, sid, bid = _runtime("semantic-v2-forged-meaning")
    frame, meaning = _v2_snapshot()
    forged = {**meaning, "fabric_fingerprint": "sha256:" + "0" * 64}

    result = apply_delta(
        store,
        sid,
        bid,
        6,
        [
            {"op": "semantic_meaning_commit", "meaning": forged},
            {"op": "semantic_frame_commit", "frame": frame},
            _check(frame["fingerprint"]),
        ],
        "rule",
        cfg,
    )

    assert not result.applied
    assert "semantic_meanings" not in result.state
    assert "semantic_frames" not in result.state
    assert result.state["rolls"] == []
    assert [row["reason"] for row in result.quarantined] == [
        "malformed op (02 SS11 spec)",
        "semantic action frame has no committed meaning receipt",
        "semantic action frame reference has no committed frame ledger",
    ]


def test_v2_frame_rejects_a_receipt_from_another_turn():
    cfg, store, sid, bid = _runtime("semantic-v2-meaning-turn")
    frame, meaning = _v2_snapshot()
    committed = apply_delta(
        store,
        sid,
        bid,
        7,
        [{"op": "semantic_meaning_commit", "meaning": meaning}],
        "rule",
        cfg,
    )
    assert not committed.quarantined

    result = apply_delta(
        store,
        sid,
        bid,
        8,
        [{"op": "semantic_frame_commit", "frame": frame}],
        "rule",
        cfg,
    )

    assert not result.applied
    assert [row["reason"] for row in result.quarantined] == [
        "semantic action frame meaning is not committed for this turn"
    ]


def test_v2_meaning_and_frame_recommit_are_state_idempotent():
    cfg, store, sid, bid = _runtime("semantic-v2-idempotence")
    frame, meaning = _v2_snapshot()
    ops = [
        {"op": "semantic_meaning_commit", "meaning": meaning},
        {"op": "semantic_frame_commit", "frame": frame},
    ]

    first = apply_delta(store, sid, bid, 10, ops, "rule", cfg)
    second = apply_delta(store, sid, bid, 10, ops, "rule", cfg)

    assert not first.quarantined
    assert not second.quarantined
    assert second.state["semantic_meanings"] == [{"turn": 10, "meaning": meaning}]
    assert second.state["semantic_frames"] == [{"turn": 10, "frame": frame}]


def test_forged_snapshot_is_rejected_and_its_reference_cannot_execute():
    cfg, store, sid, bid = _runtime("semantic-forgery")
    frame = _snapshot()
    forged = {**frame, "action_class": "weapon_attack"}  # stale fingerprint proves tampering

    result = apply_delta(
        store,
        sid,
        bid,
        2,
        [
            {"op": "semantic_frame_commit", "frame": forged},
            _check(frame["fingerprint"]),
        ],
        "rule",
        cfg,
    )

    assert not result.applied
    assert "semantic_frames" not in result.state
    assert result.state["rolls"] == []
    assert {row["reason"] for row in result.quarantined} == {
        "malformed op (02 SS11 spec)",
        "semantic action frame reference has no committed frame ledger",
    }


@pytest.mark.parametrize(
    ("frame_kwargs", "tag"),
    [
        ({"polarity": "negative"}, "negative"),
        ({"ambiguity": ("perception", "stealth")}, "ambiguous"),
    ],
)
def test_negative_and_ambiguous_frames_commit_but_abstain_from_execution(frame_kwargs, tag):
    cfg, store, sid, bid = _runtime(f"semantic-{tag}")
    frame = _snapshot(**frame_kwargs)

    result = apply_delta(
        store,
        sid,
        bid,
        3,
        [
            {"op": "semantic_frame_commit", "frame": frame},
            _check(frame["fingerprint"]),
        ],
        "rule",
        cfg,
    )

    assert [op["op"] for op in result.applied] == ["semantic_frame_commit"]
    assert result.state["semantic_frames"] == [{"turn": 3, "frame": frame}]
    assert result.state["rolls"] == []
    assert [row["reason"] for row in result.quarantined] == [
        "semantic action frame abstains from mechanic execution"
    ]


def test_reference_to_a_frame_from_another_turn_abstains():
    cfg, store, sid, bid = _runtime("semantic-turn-scope")
    frame = _snapshot()
    committed = apply_delta(
        store,
        sid,
        bid,
        8,
        [{"op": "semantic_frame_commit", "frame": frame}],
        "rule",
        cfg,
    )
    assert not committed.quarantined

    result = apply_delta(
        store,
        sid,
        bid,
        9,
        [_check(frame["fingerprint"])],
        "rule",
        cfg,
    )

    assert not result.applied
    assert result.state["rolls"] == []
    assert [row["reason"] for row in result.quarantined] == [
        "semantic action frame reference is not committed for this turn"
    ]


def test_semantic_frame_ledger_is_lazy_and_retains_only_the_latest_sixteen():
    cfg, store, sid, bid = _runtime("semantic-retention")
    assert "semantic_frames" not in current_state(store, bid)

    snapshots = []
    for turn in range(1, 18):
        frame = _snapshot(source=f"I sneak past sentry number {turn}.")
        snapshots.append(frame)
        result = apply_delta(
            store,
            sid,
            bid,
            turn,
            [{"op": "semantic_frame_commit", "frame": frame}],
            "rule",
            cfg,
        )
        assert not result.quarantined

    rows = current_state(store, bid)["semantic_frames"]
    assert len(rows) == 16
    assert [row["turn"] for row in rows] == list(range(2, 18))
    assert [row["frame"]["fingerprint"] for row in rows] == [
        frame["fingerprint"] for frame in snapshots[1:]
    ]


def test_legacy_journaled_op_without_a_frame_reference_still_replays():
    cfg, store, sid, bid = _runtime("semantic-legacy")

    result = apply_delta(store, sid, bid, 7, [_check()], "rule", cfg)
    replayed = current_state(store, bid)

    assert not result.quarantined
    assert [op["op"] for op in result.applied] == ["check"]
    assert "_semantic_frame_ref" not in result.applied[0]
    assert "semantic_frames" not in replayed
    assert replayed["rolls"] == result.state["rolls"]
    assert replayed["rolls"][-1]["skill"] == "stealth"
    assert replayed["rolls"][-1]["turn"] == 7
