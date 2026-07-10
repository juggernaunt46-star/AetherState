"""Authored kind is the truth for active vs passive (Bean 2026-07-09): a custom ability frozen
as kind="active" with a flat-bonus mechanic ("mod" — the Combat-Stims pattern) must read as
ACTIVE everywhere (HUD flag, [PLAYER] tag, NL detection) and be INVOKABLE with `use`: +N on
that one check, cost paid, cooldown set. It never auto-applies. Passives keep auto-applying;
`use` on a true passive stays a friendly notice. All RPG-gated; resolution stays baked and
replay-pure (the burst rides `_shape.burst`, the modifier is baked into `_mod`)."""
from __future__ import annotations

import random

from aetherstate import hud, registry, tier0
from aetherstate.config import Config
from aetherstate.creator import _coerce_defs
from aetherstate.state import empty_state

CUSTOM = {"name": "Combat Focus", "kind": "active", "resolution_mod": 2,
          "cost": {"stamina": 2}, "cooldown_turns": 2, "applies_to": "athletics",
          "effect": "Burn focus to push one action."}
DEFS = {"abilities": {"combat_focus": dict(CUSTOM)}}


def _st(cfg, defs=None, abilities=None, cd=None):
    cfg.specialization.name = "rpg"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    p = {"eid": "kael", "stats": {"DEX": 14, "STR": 14, "CUN": 12},
         "skills": {"athletics": 2, "perception": 1},
         "abilities": abilities if abilities is not None else ["combat_focus"],
         "resources": {"stamina": {"max": 10, "cur": 10}}}
    if defs is not None:
        p["defs"] = defs
    if cd:
        p["ability_cd"] = dict(cd)
    st["player"] = {"kael": p}
    return st


def _check(res):
    ops = [o for o in res.rule_ops if o["op"] == "check"]
    return ops[0] if ops else None


def _run(cfg, content, seed, **kw):
    st = _st(cfg, **kw)
    doc = {"messages": [{"role": "user", "content": content}]}
    return tier0.run(doc, "new_turn", False, st, cfg, random.Random(seed))


# ------------------------------- the helper itself --------------------------------
def test_ability_is_active_honors_authored_kind():
    assert registry.ability_is_active({"kind": "active", "resolution_mod": 2})
    assert registry.ability_is_active({"kind": "active", "mechanic": "mod"})
    assert not registry.ability_is_active({"kind": "passive", "mechanic": "edge"})
    assert not registry.ability_is_active({"kind": "basis", "mechanic": "basis"})
    assert registry.ability_is_active({"mechanic": "surge"})     # mechanic floor, no kind
    assert not registry.ability_is_active({"mechanic": "edge"})
    reg = registry.load()
    assert registry.ability_is_active(reg.abilities["second_wind"])
    assert not registry.ability_is_active(reg.abilities["keen_senses"])
    assert not registry.ability_is_active(reg.abilities["silver_tongue"])


# ------------------------------- resolution ---------------------------------------
def test_mod_active_applies_on_use_pays_and_cools():
    cfg = Config()
    base = _check(_run(cfg, "((aether.check athletics vs 8))", 7, defs=DEFS))
    used = _check(_run(cfg, "((aether.check athletics vs 8 use combat_focus))", 7, defs=DEFS))
    assert used["_mod"] == base["_mod"] + 2            # the burst landed on THIS roll only
    assert used["_shape"]["burst"] == 2
    assert "Combat Focus" in used["_shape"]["abilities"]
    assert used["_cost"].get("stamina") == 2           # paid in full on use
    assert used["_ability_cd"]["combat_focus"] > 0     # and it went on cooldown
    assert base.get("_shape") is None                  # never auto-applies
    assert "_ability_cd" not in base


def test_mod_active_is_deterministic_and_baked():
    cfg = Config()
    a = _check(_run(cfg, "((aether.check athletics vs 8 use combat_focus))", 11, defs=DEFS))
    b = _check(_run(cfg, "((aether.check athletics vs 8 use combat_focus))", 11, defs=DEFS))
    assert a == b                                      # replay reads only baked fields


def test_mod_active_respects_cooldown():
    cfg = Config()
    res = _run(cfg, "((aether.check athletics vs 8 use combat_focus))", 7,
               defs=DEFS, cd={"combat_focus": 99})
    assert any("recharging" in n for n in res.notices)
    op = _check(res)
    assert op.get("_shape") is None                    # the burst did NOT land


def test_true_passive_use_still_notices():
    cfg = Config()
    res = _run(cfg, "((aether.check athletics vs 8 use silver_tongue))", 7,
               abilities=["silver_tongue"])
    assert any("is passive" in n for n in res.notices)


def test_basis_use_gets_its_own_notice():
    cfg = Config()
    res = _run(cfg, "((aether.check athletics vs 8 use arcane_gift))", 7,
               abilities=["arcane_gift"])
    assert any("in-world basis" in n for n in res.notices)


def test_nl_prose_arms_a_mod_active():
    cfg = Config()
    res = _run(cfg, "I use Combat Focus and vault the barricade.", 7, defs=DEFS)
    op = _check(res)
    assert op is not None and op["skill"] == "athletics"
    assert op["_shape"]["burst"] == 2                  # the named active was invoked, not ignored


# ------------------------------- HUD + creator freeze ------------------------------
def test_hud_marks_authored_active():
    cfg = Config()
    st = _st(cfg, defs=DEFS)
    reg = registry.load(cfg)
    rows = hud._ability_rows(reg, registry, st["player"]["kael"], 0)
    row = next(r for r in rows if r["id"] == "combat_focus")
    assert row["active"] is True and row["kind"] == "active"
    assert row["cost"] == "stamina 2" and row["cooldown"] == 2


def test_coerce_keeps_mod_active_real():
    reg = registry.load()
    out = _coerce_defs({"abilities": [
        {"name": "Overclock", "kind": "active", "resolution_mod": 3,
         "cost": {"mana": 2}, "cooldown_turns": 3, "effect": "Push the implant hard."}]}, reg)
    e = out["abilities"]["overclock"]
    assert e["mechanic"] == "mod" and e["resolution_mod"] == 3
    assert e["cost"] == {"mana": 2} and e["cooldown_turns"] == 3
    assert registry.ability_is_active(e)


def test_none_session_stays_inert():
    cfg = Config()                                     # specialization stays "none"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": {"eid": "kael", "abilities": ["combat_focus"],
                             "defs": dict(DEFS), "skills": {"athletics": 2}}}
    doc = {"messages": [{"role": "user", "content":
                         "((aether.check athletics vs 8 use combat_focus))"}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    assert not [o for o in res.rule_ops if o["op"] == "check"]
