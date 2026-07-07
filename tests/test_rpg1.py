"""RPG-1 fixtures (Q27 / doc 05 §9 phase RPG-1): the curated stat/skill/ability registry, the
Tier-0 R8 explicit skill-check -> PbtA resolution -> [DIRECTIVE], the `check` op, and the
`outcome_match` linter rule. Exit criteria (doc 05 §9):
  - resolve-then-narrate fixtures green;
  - seed + roll recorded in the turn record;
  - outcome_match rejects a contradicting narration;
  - deterministic replay of a rolled turn reproduces the tier.
Everything is RPG-gated so a `none` session stays byte-identical to 1.0 (invariant 3).
"""
from __future__ import annotations

import json
import random

from aetherstate import linter, registry, tier0
from aetherstate.compose import compose, render_header
from aetherstate.config import Config
from aetherstate.state import (apply_delta, authority_violation, current_state, empty_state,
                               reduce_state, validate_op)
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from tests.mock_upstream import Reply


# ------------------------------ curated registry -----------------------------------
def test_registry_loads_curated_defaults():
    reg = registry.load()
    assert reg.version == "rpg-registry/1" and reg.mod_policy == "dnd5e"
    assert {"STR", "DEX", "INT", "CHA", "CUN", "CON"} <= set(reg.stats)
    assert {"stealth", "swordplay", "persuasion", "perception"} <= set(reg.skills)
    assert reg.skills["stealth"]["keyed_stat"] == "DEX"


def test_registry_mod_policy_and_skill_resolution():
    reg = registry.load()
    assert reg.stat_mod(14) == 2 and reg.stat_mod(9) == -1 and reg.stat_mod(10) == 0
    assert reg.resolve_skill("stealth") == "stealth"          # by id
    assert reg.resolve_skill("Stealth") == "stealth"          # by display name
    assert reg.resolve_skill("sneak") == "stealth"            # by governs verb
    assert reg.resolve_skill("flibberflam") is None           # unknown -> rejected (no freestyle)


def test_registry_effective_mod_includes_stat_rank_and_passive():
    reg = registry.load()
    player = {"stats": {"DEX": 14, "CHA": 16, "CUN": 12}, "skills": {"stealth": 3, "persuasion": 2},
              "abilities": ["keen_senses", "silver_tongue"]}
    assert reg.effective_mod(player, "stealth") == 5          # DEX+2, base0, rank3
    assert reg.effective_mod(player, "persuasion") == 6       # CHA+3, rank2, silver_tongue +1 (mod)
    # 2026-07-07 redesign: keen_senses is now `edge` (advantage — it shapes the DICE, not the
    # modifier), so it adds NOTHING to effective_mod. Only legacy `mod` abilities (silver_tongue)
    # touch the number; the flat-buff era is over (Bean: "one shouldn't just buff a stat").
    assert reg.effective_mod(player, "perception") == 1       # CUN+1, rank0 (keen_senses = advantage)


def test_resolve_tier_is_pure_and_bounded():
    # every 2d6 outcome maps into the tier ladder; extremes crit
    tiers = {registry.resolve_tier([a, b], 0, 6)[0] for a in range(1, 7) for b in range(1, 7)}
    assert tiers <= set(registry.CHECK_TIERS)
    assert registry.resolve_tier([6, 6], 0, 6)[0] == "crit_success"
    assert registry.resolve_tier([1, 1], 0, 6)[0] == "crit_fail"
    assert registry.resolve_tier([5, 5], 0, 6)[0] == "success"   # total 10 >= 10
    assert registry.resolve_tier([4, 3], 0, 6)[0] == "partial"   # total 7 in [7,9]
    assert registry.resolve_tier([2, 2], 0, 6)[0] == "fail"      # total 4 < 7
    # explicit DC shifts the thresholds (hi=DC, mid=DC-3)
    assert registry.resolve_tier([3, 3], 2, 6, dc=12)[0] == "fail"      # total 8  < 9
    assert registry.resolve_tier([4, 4], 2, 6, dc=12)[0] == "partial"   # total 10 in [9,12)
    assert registry.resolve_tier([5, 5], 2, 6, dc=12)[0] == "success"   # total 12 >= 12


def test_profile_dice_knob_overrides_registry_default():
    reg = registry.load()
    cfg = Config()
    assert registry.dice_spec(reg, cfg) == "2d6"             # none: registry default
    cfg.specialization.name = "rpg"
    cfg.specialization.dice = "3d6"
    assert registry.dice_spec(reg, cfg) == "3d6"             # profile knob wins (D1)


# ------------------------------ the `check` op -------------------------------------
_CHK = {"op": "check", "skill": "stealth", "result": 11, "tier": "success",
        "char": "Kael", "_mod": 5, "_dice": "2d6"}


def test_check_validation():
    assert validate_op(dict(_CHK)) is not None
    assert validate_op({"op": "check", "skill": "x", "result": 1, "tier": "nope"}) is None
    assert validate_op({"op": "check", "skill": "x", "result": 1}) is None       # tier required


def test_check_authority_privileged_no_extraction():
    cfg, st = Config(), empty_state()
    assert authority_violation(_CHK, "rule", st, cfg) is None        # the Tier-0 R8 path
    assert authority_violation(_CHK, "user", st, cfg) is None        # manual OOC
    assert authority_violation(_CHK, "extraction", st, cfg) is not None   # never rolled by the model


def test_check_reducer_reuses_rolls_buffer():
    st = empty_state()
    reduce_state(st, [{**_CHK, "char": "kael", "_turn": 4}])
    rec = st["rolls"][-1]
    assert rec["skill"] == "stealth" and rec["tier"] == "success" and rec["turn"] == 4
    assert rec["mod"] == 5 and rec["dice"] == "2d6"


# ------------------------------ R8 resolve-then-narrate ----------------------------
def _rpg_setup(cfg):
    cfg.specialization.name = "rpg"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": {"eid": "kael", "stats": {"DEX": 14}, "skills": {"stealth": 3},
                             "abilities": []}}
    return st


def test_r8_resolves_registered_skill_to_check_op():
    cfg = Config()
    st = _rpg_setup(cfg)
    doc = {"messages": [{"role": "user", "content": "I sneak by. ((aether.check stealth +1 vs 10))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    ops = [o for o in r.rule_ops if o["op"] == "check"]
    assert len(ops) == 1
    op = ops[0]
    assert op["skill"] == "stealth" and op["char"] == "kael" and op["dc"] == 10
    assert op["_dice"] == "2d6" and op["_mod"] == 6           # DEX+2 + rank3 + declared +1
    assert op["result"] == sum(op["_seed"]) + op["_mod"]     # seed(roll) recorded in the op
    assert op["tier"] in registry.CHECK_TIERS
    assert "aether.check" not in r.doc["messages"][-1]["content"]   # OOC stripped from forward


def test_r8_unknown_skill_is_rejected_nothing_freestyle():
    cfg = Config()
    st = _rpg_setup(cfg)
    doc = {"messages": [{"role": "user", "content": "((aether.check flibberflam))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    assert not [o for o in r.rule_ops if o["op"] == "check"]
    assert any("unknown skill" in n for n in r.notices)


def test_r8_inert_under_none():
    cfg = Config()                                            # specialization defaults to none
    st = _rpg_setup(Config())                                 # rpg-shaped state, but cfg is none
    doc = {"messages": [{"role": "user", "content": "((aether.check stealth))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(7))
    assert not [o for o in r.rule_ops if o["op"] == "check"]  # no resolution under none


def test_directive_renders_this_turn_and_roll_coexists():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="dir")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kael", "kind": "player"},
                {"op": "player_seed", "entity": "Kael",
                 "card": {"stats": {"DEX": 14}, "skills": {"stealth": 3},
                          "resources": {"hp": {"max": 20}}}}], "genesis", cfg)
    cfg.specialization.name = "rpg"
    doc = {"messages": [{"role": "user",
                         "content": "I sneak. ((roll d20+2)) ((aether.check stealth vs 9))"}]}
    st = current_state(store, bid)
    t0 = tier0.run(doc, "new_turn", False, st, cfg, random.Random(3))
    apply_delta(store, sid, bid, 4, t0.user_ops, "user", cfg)
    apply_delta(store, sid, bid, 4, t0.rule_ops, "rule", cfg)
    h = render_header(current_state(store, bid), cfg)
    assert "[DIRECTIVE]" in h and "the stealth check resolved as" in h
    assert "[ROLL] d20+2 =" in h                              # plain roll still renders (no crash)


def test_directive_renders_every_check_this_turn():
    """Two checks declared in one message BOTH land in [DIRECTIVE] — a multi-check turn is
    fully directed (no silently unnarrated resolution)."""
    cfg = Config()
    st = _rpg_setup(cfg)
    st["player"]["kael"]["skills"]["persuasion"] = 2
    doc = {"messages": [{"role": "user", "content":
                         "((aether.check stealth vs 9)) ((aether.check persuasion vs 9))"}]}
    t0 = tier0.run(doc, "new_turn", False, st, cfg, random.Random(3))
    ops = [o for o in t0.rule_ops if o["op"] == "check"]
    assert len(ops) == 2
    reduce_state(st, [{**o, "_turn": 6} for o in ops])
    h = render_header(st, cfg)
    assert "the stealth check resolved as" in h and "the persuasion check resolved as" in h
    assert h.count("[DIRECTIVE]") == 1 and "these outcomes" in h


def test_none_session_has_no_directive_or_rules_leak():
    cfg = Config()
    st = _rpg_setup(Config())
    st["rolls"] = [{"skill": "stealth", "result": 11, "tier": "success", "char": "kael", "turn": 0}]
    st["meta"] = {"turn": 0}
    assert "[DIRECTIVE]" not in render_header(st, cfg)        # none: no leak
    out, kept = compose({"messages": [{"role": "user", "content": "hi"}]}, st, cfg,
                        Stamp(session="s", user="Kael"), "new_turn")
    assert "[RULES]" not in json.dumps(out) and "[DIRECTIVE]" not in json.dumps(out)


def test_rules_contract_present_under_rpg_droppable():
    cfg = Config()
    cfg.specialization.name = "rpg"
    st = _rpg_setup(cfg)
    out, kept = compose({"messages": [{"role": "user", "content": "hi"}]}, st, cfg,
                        Stamp(session="s", user="Kael"), "new_turn")
    assert "rules_contract" in [c["cls"] for c in kept]
    assert "[RULES]" in json.dumps(out) and "Game Master" in json.dumps(out)


# ------------------------------ deterministic replay -------------------------------
def test_deterministic_replay_reproduces_tier():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="replay")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kael", "kind": "player"}],
                "user", cfg)
    cfg.specialization.name = "rpg"
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": {"eid": "kael", "stats": {"DEX": 14}, "skills": {"stealth": 3}}}
    doc = {"messages": [{"role": "user", "content": "((aether.check stealth))"}]}
    t0 = tier0.run(doc, "new_turn", False, st, cfg, random.Random(11))
    apply_delta(store, sid, bid, 4, t0.rule_ops, "rule", cfg)
    live = current_state(store, bid)["rolls"][-1]
    # rebuild the whole branch from the journal (fresh reducer) -> same tier + result, no RNG
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())["rolls"][-1]
    assert replay["tier"] == live["tier"] and replay["result"] == live["result"]
    assert replay["tier"] in registry.CHECK_TIERS


# ------------------------------ outcome_match linter -------------------------------
def _check_state(tier, turn=5, mode="live"):
    return {"schema": "aetherstate/1",
            "entities": {"kael": {"name": "Kael", "present": True, "kind": "player"}},
            "player": {"kael": {"eid": "kael"}}, "scene": {"mode": mode},
            "rolls": [{"skill": "stealth", "result": 5, "tier": tier, "char": "kael", "turn": turn}],
            "meta": {"turn": turn}}


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def test_outcome_match_rejects_contradiction():
    cfg = _rpg_cfg()
    v = linter.run(_check_state("fail"), "Kael slips past and succeeds without a sound.", cfg, turn=5)
    assert [x.rule for x in v] == ["outcome_match"]
    assert v[0].severity == "med" and "resolved 'fail'" in v[0].detail


def test_outcome_match_partial_is_lenient():
    cfg = _rpg_cfg()
    v = linter.run(_check_state("partial"),
                   "A partial success — Kael sneaks by, but at a cost: a board creaks.", cfg, turn=5)
    assert not v                                              # partial<->success is a near-miss, not flagged


def test_outcome_match_selection_is_turn_scoped_not_buffer_tail():
    """A later same-turn plain roll (no tier) must not hide the check — selection matches
    [DIRECTIVE]: the latest record for THIS turn that carries a tier."""
    cfg = _rpg_cfg()
    st = _check_state("fail")
    st["rolls"].append({"spec": "d20", "result": 14, "turn": 5})   # plain R7 roll after the check
    v = linter.run(st, "Kael slips past and succeeds without a sound.", cfg, turn=5)
    assert [x.rule for x in v] == ["outcome_match"]


def test_outcome_match_neutral_prose_clean():
    cfg = _rpg_cfg()
    v = linter.run(_check_state("success"), "Kael moves through the shadows toward the door.",
                   cfg, turn=5)
    assert not v                                              # no explicit result claim -> ok


def test_outcome_match_gated_off_in_flashback_and_stale_and_none():
    cfg = _rpg_cfg()
    hot = "Kael succeeds brilliantly at sneaking past."
    assert not linter.run(_check_state("fail", mode="flashback"), hot, cfg, turn=5)  # nonlive gate
    assert not linter.run(_check_state("fail", turn=3), hot, cfg, turn=5)            # stale check
    assert not linter.run(_check_state("fail"), hot, Config(), turn=5)              # non-rpg inert


def test_outcome_match_escalates_on_repeat():
    cfg = _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="esc")
    prose = "Kael slips past and succeeds without a sound."
    f1 = linter.lint_turn(store, cfg, sid, bid, 5, _check_state("fail", 5), prose)
    assert [(x.rule, x.severity) for x in f1] == [("outcome_match", "med")]
    assert not linter.lint_turn(store, cfg, sid, bid, 6, _check_state("fail", 6), prose)  # cooldown
    f3 = linter.lint_turn(store, cfg, sid, bid, 10, _check_state("fail", 10), prose)
    assert [(x.rule, x.severity) for x in f3] == [("outcome_match", "high")]  # persisting -> high


# ------------------------------ live proxy e2e -------------------------------------
SENT = "<<AETHER:v=1;session={s};turn={t};type=normal;speaker=Dungeon Master;user=Bean>>"


def _payload(session, turn, tail=""):
    return {"model": "m", "messages": [
        {"role": "system", "content": SENT.format(s=session, t=turn) + " A cold keep at dusk."},
        {"role": "user", "content": "I try the lock. " + tail}]}


async def test_rpg_check_e2e_injects_directive(client, mock_upstream, cfg):
    """Flagship RPG-1 exit: a declared check resolves on the hot path and the SAME forwarded
    request already carries the immutable [DIRECTIVE] for the narrator to honour."""
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions",
                      json=_payload("chat-rpg1", 1, "((aether.check lockpicking vs 8))"))
    fwd = mock_upstream.requests[0].body
    assert b"[DIRECTIVE]" in fwd and b"lockpicking check resolved as" in fwd
    assert b"aether.check" not in fwd                         # engine syntax never forwarded
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    rolls = now["state"]["rolls"]
    assert rolls and rolls[-1]["skill"] == "lockpicking" and rolls[-1]["tier"] in registry.CHECK_TIERS


async def test_none_session_no_check_leak_e2e(client, mock_upstream, cfg):
    assert cfg.specialization.name == "none"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions",
                      json=_payload("chat-none1", 1, "((aether.check lockpicking))"))
    fwd = mock_upstream.requests[0].body
    assert b"[DIRECTIVE]" not in fwd and b"[RULES]" not in fwd
