"""RPG per-character snapshot overlay (Q27 assist-authored -> snapshot, doc 09 §1).

The scalability foundation for freestyle + mastery-evolved mechanics: a player may own FROZEN
per-character definitions under `player["defs"]["skills"|"abilities"|"stats"]`. They WIN over the
shipped registry for that id, and an id that exists ONLY in the player's defs still resolves — so a
bespoke ("freestyle") or evolved skill/ability has a home WITHOUT mutating the shared, process-wide
registry, and WITHOUT relaxing roll-time resolution (a token unknown to both is still rejected).

Invariants exercised here:
  - preset registry skills keep working unchanged (Bean: presets are fine);
  - a player-only freestyle skill resolves + rolls end-to-end;
  - a snapshot definition overrides the global one for that player only;
  - no `defs` => byte-identical to pre-overlay (same object, zero allocation);
  - replay-pure: a freestyle-skill check reproduces its tier from the journal, no RNG, no registry.
"""
from __future__ import annotations

import random

from aetherstate import registry, tier0
from aetherstate.config import Config
from aetherstate.state import (apply_delta, current_state, empty_state, reduce_state)
from aetherstate.store import Store

# A player carrying a freestyle skill + a snapshot that overrides a preset + a snapshot passive.
_DEFS = {
    "skills": {
        "voidwhisper": {"name": "Voidwhisper", "keyed_stat": "CHA", "base_mod": 1,
                        "max_rank": 5, "governs": ["commune with the void", "unmake"]},
        # override the PRESET `stealth`: re-key it off INT with a +2 base (this player only)
        "stealth": {"name": "Stealth", "keyed_stat": "INT", "base_mod": 2, "max_rank": 5},
    },
    "abilities": {
        "void_sense": {"name": "Void Sense", "kind": "passive",
                       "passive_mod": {"skill": "voidwhisper", "amount": 2}},
    },
}


def _player(**over):
    p = {"eid": "kael",
         "stats": {"DEX": 14, "INT": 16, "CHA": 14, "CUN": 12},
         "skills": {"stealth": 3, "voidwhisper": 2},
         "abilities": ["void_sense"],
         "defs": _DEFS}
    p.update(over)
    return p


# ------------------------------ resolution overlay ---------------------------------
def test_presets_still_resolve_unchanged():
    """Shipped registry skills are untouched — a player with no defs behaves exactly as before."""
    reg = registry.load()
    assert reg.resolve_skill("lockpicking") == "lockpicking"   # preset, no player
    assert reg.resolve_skill("sneak") == "stealth"             # preset governs-verb
    assert reg.resolve_skill("flibberflam") is None            # unknown still rejected


def test_freestyle_skill_resolves_only_with_its_owner():
    reg = registry.load()
    # unknown to the global registry...
    assert reg.resolve_skill("voidwhisper") is None
    assert reg.resolve_skill("voidwhisper", {}) is None
    # ...but resolves for the player who owns the frozen def (by id, name, and governs-verb)
    p = _player()
    assert reg.resolve_skill("voidwhisper", p) == "voidwhisper"
    assert reg.resolve_skill("Voidwhisper", p) == "voidwhisper"
    assert reg.resolve_skill("unmake", p) == "voidwhisper"
    # a token unknown to BOTH the registry and the player's defs is still rejected
    assert reg.resolve_skill("flibberflam", p) is None


def test_snapshot_def_overrides_global_for_that_player_only():
    reg = registry.load()
    plain = {"stats": {"DEX": 14, "INT": 16}, "skills": {"stealth": 3}, "abilities": []}
    # global stealth: DEX(+2) + base0 + rank3 = 5
    assert reg.effective_mod(plain, "stealth") == 5
    # this player's frozen stealth: INT(+3) + base2 + rank3 = 8 — override is per-character
    assert reg.effective_mod(_player(), "stealth") == 8
    # and the override does NOT leak into the shared registry
    assert reg.effective_mod(plain, "stealth") == 5


def test_freestyle_effective_mod_includes_stat_rank_and_snapshot_passive():
    reg = registry.load()
    # CHA(+2) + base1 + rank2 + snapshot passive void_sense(+2) = 7
    assert reg.effective_mod(_player(), "voidwhisper") == 7


def test_no_defs_is_byte_identical_zero_alloc():
    reg = registry.load()
    assert reg.merged_skills(None) is reg.skills           # same object, no merge allocated
    assert reg.merged_skills({}) is reg.skills
    assert reg.merged_skills({"defs": {}}) is reg.skills
    assert reg.merged_abilities({"defs": {"skills": {}}}) is reg.abilities


# ------------------------------ seeding (player_seed stores defs) ------------------
def test_player_seed_freezes_defs_into_the_record():
    st = empty_state()
    reduce_state(st, [{"op": "player_seed", "entity": "kael",
                       "card": {"stats": {"CHA": 14}, "skills": {"voidwhisper": 2},
                                "abilities": ["void_sense"], "defs": _DEFS}}])
    rec = st["player"]["kael"]
    assert rec["defs"]["skills"]["voidwhisper"]["keyed_stat"] == "CHA"
    assert rec["defs"]["abilities"]["void_sense"]["kind"] == "passive"
    # no defs on the card => no defs key at all (byte-identical default record)
    st2 = empty_state()
    reduce_state(st2, [{"op": "player_seed", "entity": "mara", "card": {"stats": {"DEX": 12}}}])
    assert "defs" not in st2["player"]["mara"]


# ------------------------------ R8 end-to-end -------------------------------------
def _rpg_state():
    st = empty_state()
    st["entities"]["kael"] = {"kind": "player", "name": "Kael", "present": True, "aliases": []}
    st["player"] = {"kael": _player()}
    return st


def test_r8_resolves_a_freestyle_snapshot_skill():
    cfg = Config()
    cfg.specialization.name = "rpg"
    st = _rpg_state()
    doc = {"messages": [{"role": "user", "content": "I unmake the ward. ((aether.check voidwhisper +1 vs 9))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(5))
    ops = [o for o in r.rule_ops if o["op"] == "check"]
    assert len(ops) == 1 and ops[0]["skill"] == "voidwhisper" and ops[0]["char"] == "kael"
    # baked effective mod = snapshot effective_mod(7) + declared(+1) + dice flat(0)
    assert ops[0]["_mod"] == 8
    assert ops[0]["result"] == sum(ops[0]["_seed"]) + ops[0]["_mod"]
    assert ops[0]["tier"] in registry.CHECK_TIERS


def test_r8_still_rejects_unknown_even_with_defs():
    cfg = Config()
    cfg.specialization.name = "rpg"
    st = _rpg_state()
    doc = {"messages": [{"role": "user", "content": "((aether.check flibberflam))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(5))
    assert not [o for o in r.rule_ops if o["op"] == "check"]
    assert any("unknown skill" in n for n in r.notices)


def test_freestyle_skill_inert_under_none():
    cfg = Config()                                    # specialization defaults to none
    st = _rpg_state()
    doc = {"messages": [{"role": "user", "content": "((aether.check voidwhisper))"}]}
    r = tier0.run(doc, "new_turn", False, st, cfg, random.Random(5))
    assert not [o for o in r.rule_ops if o["op"] == "check"]


def test_player_block_renders_effective_mods_snapshot_first():
    """[PLAYER] shows precomputed effective check mods (doc 06 §2.3 — the narrator never does
    math), resolved snapshot-first: the per-character stealth override and the freestyle skill
    both render with their true mods (INT16+base2+rank3=+8; CHA14+base1+rank2+passive2=+7)."""
    from aetherstate.compose import render_header
    cfg = Config()
    cfg.specialization.name = "rpg"
    h = render_header(_rpg_state(), cfg)
    assert "Stealth+8" in h and "Voidwhisper+7" in h
    assert "Abilities: Void Sense" in h                    # ability id labelled via merged defs


# ------------------------------ replay purity -------------------------------------
def test_deterministic_replay_reproduces_freestyle_tier():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="snap-replay")
    apply_delta(store, sid, bid, 0,
                [{"op": "entity_add", "name": "Kael", "kind": "player"},
                 {"op": "player_seed", "entity": "Kael",
                  "card": {"stats": {"CHA": 14}, "skills": {"voidwhisper": 2},
                           "abilities": ["void_sense"], "defs": _DEFS}}],
                "genesis", cfg)
    cfg.specialization.name = "rpg"
    doc = {"messages": [{"role": "user", "content": "((aether.check voidwhisper vs 9))"}]}
    commit_turn = 4
    t0 = tier0.run(
        doc, "new_turn", False, current_state(store, bid), cfg, random.Random(11),
        turn=commit_turn,
    )
    committed = apply_delta(store, sid, bid, commit_turn, t0.rule_ops, "rule", cfg)
    assert committed.applied and not committed.quarantined
    live = current_state(store, bid)["rolls"][-1]
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())["rolls"][-1]
    assert replay["skill"] == "voidwhisper"
    assert replay["tier"] == live["tier"] and replay["result"] == live["result"]
    assert replay["tier"] in registry.CHECK_TIERS
