"""P2 fixtures: Tier-0 R0-R7 (03 SS6, Q13/Q14 scopes, 08 B5)."""
from __future__ import annotations

import random

from aetherstate.config import Config
from aetherstate.state import empty_state
from aetherstate import tier0


def doc(*msgs):
    return {"model": "m", "messages": [{"role": r, "content": c} for r, c in msgs]}


def run(d, klass="new_turn", dup=False, state=None, cfg=None, seed=7):
    cfg = cfg or Config()
    return tier0.run(d, klass, dup, state or empty_state(), cfg, random.Random(seed))


def test_r0_own_message_safeword_freezes_in_every_mode():
    for mode in ("strict", "negotiated", "cnc", "unrestricted"):
        cfg = Config()
        cfg.consent.mode = mode
        cfg.consent.safewords = ["red"]
        res = run(doc(("user", "Red. I need to stop.")), cfg=cfg)
        assert {"op": "freeze", "reason": "safeword"} in res.user_ops, mode


def test_r0_fiction_side_scan_gated_by_scope_and_raw():
    cfg = Config()
    cfg.consent.safewords = ["red"]
    d = doc(("user", "u1"), ("assistant", 'She gasps "red! red!"'), ("user", "go on"))
    assert not run(d, cfg=cfg).rule_ops or \
        all(o["op"] != "freeze" for o in run(d, cfg=cfg).rule_ops)   # user_only default (Q14)
    cfg.consent.safeword_scan = "both"
    assert any(o["op"] == "freeze" for o in run(d, cfg=cfg).rule_ops)  # 08 X1 via both
    cfg.consent.mode = "unrestricted"
    assert all(o["op"] != "freeze" for o in run(d, cfg=cfg).rule_ops)  # raw: no fiction-side trigger


def test_r1_commands_parse_execute_and_strip():
    res = run(doc(("user", "((aether.set scene.location Moonlit Tavern)) We enter.")))
    assert {"op": "scene_set", "location": "Moonlit Tavern"} in res.user_ops
    assert res.doc is not None
    assert "((" not in res.doc["messages"][0]["content"]
    assert "We enter." in res.doc["messages"][0]["content"]
    res = run(doc(("user", "((aether.freeze)) hold on")))
    assert {"op": "freeze", "reason": "user"} in res.user_ops
    res = run(doc(("user", "((aether.resume)) ok")))
    assert {"op": "unfreeze"} in res.user_ops
    res = run(doc(("user", "((aether.set nonsense.path 5)) hi")))
    assert not res.user_ops and any("unsupported path" in n for n in res.notices)


def test_r1_commands_not_reexecuted_from_history_but_always_stripped():
    """Frontends resend history: old OOC must strip from bytes yet act only once."""
    d = doc(("user", "((aether.freeze)) wait"), ("assistant", "paused"), ("user", "((aether.resume)) go"))
    res = run(d)
    assert {"op": "unfreeze"} in res.user_ops
    assert {"op": "freeze", "reason": "user"} not in res.user_ops   # history cmd not re-run
    assert "((" not in str(res.doc["messages"])                      # but stripped everywhere


def test_r2_clock_ticks_only_on_narrative_turns():
    assert any(o["op"] == "clock_tick" for o in run(doc(("user", "hi"))).rule_ops)
    for klass in ("swipe", "continue"):
        assert not run(doc(("user", "hi")), klass=klass).rule_ops
    assert not run(doc(("user", "hi")), dup=True).user_ops           # 08 S7: dedup never re-applies


def test_r3_time_keywords_conservative():
    res = run(doc(("user", "The next morning, she found the note.")))
    assert {"op": "time_advance", "to_time_of_day": "morning"} in res.rule_ops
    assert all(o["op"] != "time_advance" for o in run(doc(("user", "mornings are nice"))).rule_ops)


def test_r5_presence_heuristic_low_confidence():
    st = empty_state()
    st["entities"]["kira"] = {"name": "Kira", "aliases": ["Kir"], "present": False}
    res = run(doc(("user", "Just then Kira walks in from the rain.")), state=st)
    ops = [o for o in res.rule_ops if o["op"] == "presence"]
    assert ops and ops[0]["entity"] == "kira" and ops[0]["present"] is True
    assert ops[0]["_conf"] == "low"
    res = run(doc(("user", "Kir storms out, furious.")), state=st)
    ops = [o for o in res.rule_ops if o["op"] == "presence"]
    assert ops and ops[0]["present"] is False


def test_r6_stagnation_metric():
    same = "She smiles softly and pours the wine again for the guests tonight"
    d = doc(("user", "u1"), ("assistant", same), ("user", "u2"), ("assistant", same),
            ("user", "u3"), ("assistant", same), ("user", "u4"))
    ops = [o for o in run(d).rule_ops if o["op"] == "stagnation"]
    assert ops and ops[0]["value"] > 0.9
    varied = doc(("user", "u1"), ("assistant", "The rain hammered the tin roof all night long"),
                 ("user", "u2"), ("assistant", "Dawn broke green and cold over the harbor"),
                 ("user", "u3"), ("assistant", same), ("user", "u4"))
    ops = [o for o in run(varied).rule_ops if o["op"] == "stagnation"]
    assert ops and ops[0]["value"] < 0.3


def test_r7_dice_deterministic_and_bounded():
    r1 = run(doc(("user", "((roll 2d6+1)) attack!")), seed=42)
    r2 = run(doc(("user", "((roll 2d6+1)) attack!")), seed=42)
    op1 = next(o for o in r1.user_ops if o["op"] == "roll")
    op2 = next(o for o in r2.user_ops if o["op"] == "roll")
    assert op1 == op2 and 3 <= op1["result"] <= 13               # real RNG, seeded = replayable
    assert not any(o["op"] == "roll" for o in run(doc(("user", "((roll d0)) hm"))).user_ops)


def test_r1_set_value_quotes_stripped_and_bare_aliases():
    """P2 gate feedback (2026-07-03): quoted values lose their quotes; bare keys alias."""
    res = run(doc(("user", "((aether.set scene.location \"the dragon's lair\")) onward")))
    assert {"op": "scene_set", "location": "the dragon's lair"} in res.user_ops
    res = run(doc(("user", "((aether.set location 'Moonlit Tavern')) hi")))
    assert {"op": "scene_set", "location": "Moonlit Tavern"} in res.user_ops
    res = run(doc(("user", "((aether.set time night)) hi")))
    assert {"op": "time_advance", "to_time_of_day": "night"} in res.user_ops
    res = run(doc(("user", "((aether.set tension 60)) hi")))
    assert {"op": "scene_dial", "dial": "tension", "set": 60} in res.user_ops
    res = run(doc(("user", "((aether.set nonsense 1)) hi")))     # unknown bare key: still rejected
    assert not res.user_ops and any("unsupported path" in n for n in res.notices)
    # interior quotes/apostrophes untouched
    res = run(doc(("user", "((aether.set location the dragon's lair)) hi")))
    assert {"op": "scene_set", "location": "the dragon's lair"} in res.user_ops
