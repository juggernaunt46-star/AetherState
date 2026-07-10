"""P1b exit gate: L3 classification accuracy >= 95% on a scripted corpus (07 P1, 11 SS4).

Seeded generator simulates ST request streams for interleaved chats: new turns, swipes,
edits, continues, plus occasional context-window slides (oldest-first trim, 08 S3).
Ground-truth labels come from the generator; the engine never sees them. Deterministic:
the whole corpus is replayed twice and must classify identically (11 determinism gate).
"""
from __future__ import annotations

import json
import random

from aetherstate.config import SessionConfig
from aetherstate.session_engine import SessionEngine, TurnClass
from aetherstate.store import Store

MIN_ACCURACY = 0.95
STEPS = 120
CHATS = 3
TRIM_KEEP = 12          # window slide: keep this many trailing msgs (even -> parity stable)


class SimChat:
    """One ST chat file. hist mirrors what the frontend would render."""

    def __init__(self, name: str):
        self.name = name
        self.hist: list[tuple[str, str]] = [("assistant", f"{name} greeting text")]
        self.i = 0
        self.started = False

    def _request(self, msgs) -> bytes:
        if len(msgs) > TRIM_KEEP:                       # context-window slide (08 S3)
            msgs = msgs[len(msgs) - TRIM_KEEP:]
        return json.dumps({"messages": [
            {"role": r, "content": t} for r, t in msgs]}).encode()

    def step(self, rng: random.Random) -> tuple[bytes, TurnClass]:
        ends_assistant = self.hist[-1][0] == "assistant"
        ops = ["new_turn"]
        if self.started and ends_assistant and len(self.hist) > 2:
            ops += ["swipe"] * 2 + ["continue"]
            if len(self.hist) >= 5:
                ops += ["edit"]
        op = rng.choice(ops) if self.started else "new_turn"

        if op == "new_turn":
            self.i += 1
            self.hist.append(("user", f"{self.name} user line {self.i}"))
            body = self._request(self.hist)
            expected = TurnClass.new_turn if self.started else TurnClass.new_session
            self.started = True
            self.hist.append(("assistant", f"{self.name} reply {self.i}"))
            return body, expected
        if op == "swipe":                                # regen: history minus the reply
            body = self._request(self.hist[:-1])
            self.hist[-1] = ("assistant", f"{self.name} reply {self.i} swiped {rng.random():.3f}")
            return body, TurnClass.swipe
        if op == "continue":                             # extend the partial reply
            body = self._request(self.hist)
            self.hist[-1] = ("assistant", self.hist[-1][1] + " extended")
            return body, TurnClass.continue_
        # edit: rewrite the last user msg, regenerate
        self.hist[-2] = ("user", f"{self.name} user line {self.i} EDITED {rng.random():.3f}")
        body = self._request(self.hist[:-1])
        self.hist[-1] = ("assistant", f"{self.name} reply {self.i} post-edit")
        return body, TurnClass.edit_fork


def run_corpus(seed: int) -> tuple[list[TurnClass], list[TurnClass]]:
    rng = random.Random(seed)
    chats = [SimChat(f"chat{c}") for c in range(CHATS)]
    engine = SessionEngine(Store(":memory:"), SessionConfig(dedup_window_s=0))
    got, want = [], []
    for _ in range(STEPS):
        body, expected = rng.choice(chats).step(rng)
        res = engine.observe(None, body)
        got.append(res.klass if res else None)
        want.append(expected)
    return got, want


def test_l3_accuracy_gate():
    got, want = run_corpus(seed=1108)
    assert all(k in want for k in (TurnClass.new_session, TurnClass.new_turn,
                                   TurnClass.swipe, TurnClass.continue_,
                                   TurnClass.edit_fork)), "corpus must cover all classes"
    correct = sum(1 for g, w in zip(got, want) if g is w)
    acc = correct / len(want)
    wrongs = [(i, w.value, g.value if g else None)
              for i, (g, w) in enumerate(zip(got, want)) if g is not w]
    assert acc >= MIN_ACCURACY, f"accuracy {acc:.3f} < {MIN_ACCURACY}; wrong: {wrongs[:10]}"


def test_l3_accuracy_deterministic():
    a = run_corpus(seed=7)
    b = run_corpus(seed=7)
    assert a == b                                        # 11: determinism repeat gate
