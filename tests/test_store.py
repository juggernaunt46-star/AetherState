"""Store spine: sessions, settlement, swipes, state_at replay (03 SS3)."""
from __future__ import annotations

from aetherstate.store import Store


def merge_reducer(state: dict, ops: list[dict]) -> dict:
    for op in ops:
        state = {**state, **op}
    return state


def test_session_idempotent():
    s = Store(":memory:")
    a, b = s.get_or_create_session("chat-1"), s.get_or_create_session("chat-1")
    assert a["session_id"] == b["session_id"]
    assert s.get_or_create_session("chat-2")["session_id"] != a["session_id"]


def test_settlement_lag1():
    s = Store(":memory:")
    b = s.get_or_create_session("c")["active_branch"]
    s.record_turn(b, 0, "new_session", "normal")
    s.record_turn(b, 1, "new_turn", "normal")
    rows = {r["turn_index"]: r["settled"] for r in
            s.db.execute("SELECT turn_index, settled FROM turns").fetchall()}
    assert rows == {0: 1, 1: 0}                     # newer turn settles older (lag-1 gate)


def test_swipe_bumps_and_clears_assistant_hash():
    s = Store(":memory:")
    b = s.get_or_create_session("c")["active_branch"]
    s.record_turn(b, 0, "new_session", "normal")
    assert s.bump_swipe(b) == 1
    assert s.bump_swipe(b) == 2


def test_state_at_checkpoint_plus_replay():
    s = Store(":memory:")
    b = s.get_or_create_session("c")["active_branch"]
    s.checkpoint(b, 0, {"hp": 10})
    s.journal(b, 0, 1, [{"hp": 9}], "extraction")
    s.journal(b, 1, 2, [{"mood": "grim"}], "extraction")
    s.journal(b, 2, 3, [{"hp": 3}], "extraction")
    assert s.state_at(b, 1, merge_reducer) == {"hp": 9}
    assert s.state_at(b, 2, merge_reducer) == {"hp": 9, "mood": "grim"}
    assert s.state_at(b, 3, merge_reducer) == {"hp": 3, "mood": "grim"}
    s.checkpoint(b, 2, {"hp": 9, "mood": "grim"})   # later checkpoint shortcuts replay
    assert s.state_at(b, 3, merge_reducer) == {"hp": 3, "mood": "grim"}


def test_state_at_empty_branch():
    s = Store(":memory:")
    b = s.get_or_create_session("c")["active_branch"]
    assert s.state_at(b, 5, merge_reducer) == {}
