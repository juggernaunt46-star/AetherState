"""P6 (2026-07-04): update cadence, transcript intake, idle settle, restart resume."""
from __future__ import annotations

from aetherstate.config import Config
from aetherstate.extraction import Ladder
from aetherstate.jobs import JobRunner
from aetherstate.prompts import user_message
from aetherstate.store import Store


def _store_with_turns(n: int, settled: int) -> tuple[Store, str, str]:
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="cad-test")
    for i in range(n):
        store.record_turn(bid, i, "new_turn", "normal")
        store.write_turn_text(bid, i, user_text=f"User: u{i}",
                              assistant_text=f"Char: a{i} " + "x" * 50)
    # record_turn settles everything below the head; optionally settle the head too
    if settled >= n:
        store.settle_head(bid)
    return store, sid, bid


def _runner(store: Store, cfg: Config) -> JobRunner:
    return JobRunner(store, cfg, Ladder(store, cfg, lambda: None))


def test_cadence_gates_flush():
    cfg = Config()
    cfg.extraction.mode = "assist"
    cfg.extraction.cadence_turns = 3
    store, sid, bid = _store_with_turns(3, settled=2)   # 2 settled, head unsettled
    jobs = _runner(store, cfg)
    flushed = []
    jobs._flush = lambda s, b, h: flushed.append((s, b, h))
    armed = []
    jobs._arm_debounce = lambda s, b, h: armed.append(b)
    jobs.notify(sid, bid, 2)
    assert not flushed and armed          # below cadence -> debounce catch-up only
    cfg.extraction.cadence_turns = 1
    jobs.notify(sid, bid, 2)
    assert flushed                        # cadence 1 -> immediate


def test_settle_head_requires_assistant_text():
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="settle-test")
    store.record_turn(bid, 0, "new_session", "normal")
    store.write_turn_text(bid, 0, user_text="User: hi")     # still generating
    assert store.settle_head(bid) is False
    store.write_turn_text(bid, 0, assistant_text="Char: hello there")
    assert store.settle_head(bid) is True
    assert store.pending_extractions(bid) == [0]


def test_exchange_parts_context_budget():
    cfg = Config()
    cfg.extraction.intake_chars = 100000
    store, sid, bid = _store_with_turns(6, settled=6)
    jobs = _runner(store, cfg)
    from aetherstate.jobs import Batch
    b = Batch(sid, bid, 4, 5, 5)
    context, exchange = jobs._exchange_parts(b)
    assert "a4" in exchange and "a5" in exchange
    assert "a3" in context and "a0" in context      # earlier turns rode along
    assert "a4" not in context                      # batch never duplicated into context
    cfg.extraction.intake_chars = 0                 # intake off -> batch only
    context, exchange = jobs._exchange_parts(b)
    assert context == "" and "a5" in exchange       # exchange itself NEVER truncated


def test_user_message_context_section():
    m = user_message("(state)", "Mira", 4, 5, "the new exchange", context="older prose")
    assert "older prose" in m and "reference only" in m
    assert m.index("older prose") < m.index("the new exchange")
    m2 = user_message("(state)", "Mira", 4, 5, "the new exchange")
    assert "reference only" not in m2


def test_resume_pending_requeues_after_restart():
    cfg = Config()
    cfg.extraction.mode = "assist"
    store, sid, bid = _store_with_turns(3, settled=3)   # all settled+pending, no runner state
    jobs = _runner(store, cfg)                          # fresh runner = post-restart
    flushed = []
    jobs._flush = lambda s, b, h: flushed.append(b)
    assert jobs.resume_pending() == 1 and flushed == [bid]
    cfg.extraction.mode = "rules"
    assert jobs.resume_pending() == 0                   # respects off/rules
