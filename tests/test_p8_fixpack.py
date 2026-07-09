"""2026-07-09 Greywater fix pack: the live-playtest findings.

Covers: same-turn affinity dedup (tag+ladder double-ingest), number-word item counts +
'(worn)' author tags + worn-rig auto-equip, R8b DM-called checks (arm on the DM's own
((aether.check ...)) call; player explicit/NL always wins; none stays inert; replay pure),
NL detection over rank-0 defs skills, creator mechanic inference + registry-echo dedup +
governs fallback, narrator sentence-safe trims, genesis narrator-card cast guard, and the
real package version on /aether/status.
"""
from __future__ import annotations

import pathlib
import random
import re

from aetherstate import __version__, registry, tier0
from aetherstate.compose import _render_directive
from aetherstate.config import Config
from aetherstate.creator import _coerce_defs
from aetherstate.genesis import rules_ops
from aetherstate.narrator import _s_sent
from aetherstate.state import (_split_item_qty, _worn_flag, apply_delta, classify_item,
                               current_state)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p8")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Vex", "kind": "player"},
        {"op": "entity_add", "name": "Greer"},
        {"op": "player_seed", "entity": "Vex",
         "card": {"stats": {"CHA": 14}, "skills": {"persuasion": 3},
                  "defs": {"skills": {"dive_rig_operation": {
                      "name": "Dive-Rig Operation", "keyed_stat": "INT", "base_mod": 0,
                      "max_rank": 5, "governs": ["dive"], "desc": "rig work"}}},
                  "resources": {"hp": {"max": 20}}}}],
        "genesis", cfg)
    return store, sid, bid


class _Rig:
    def __init__(self, value=4):
        self.value = value

    def randint(self, a, b):
        return self.value


# ------------------------------ affinity double-ingest --------------------------------
def test_affinity_same_turn_identical_dup_ignored():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    op = {"op": "affinity_adj", "target": "greer", "delta": 4, "reason": "cracked his guard"}
    apply_delta(store, sid, bid, 2, [dict(op), dict(op)], "extraction", cfg)
    st = current_state(store, bid)
    aff = st["affinity"]["vex->greer"]
    assert aff["value"] == 4, "the tag+ladder double must count once"
    assert len(aff["ledger"]) == 1


def test_affinity_same_turn_different_reason_still_applies():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "affinity_adj", "target": "greer", "delta": 4, "reason": "showed the footage"},
        {"op": "affinity_adj", "target": "greer", "delta": 2, "reason": "swore on the Ledger"}],
        "extraction", cfg)
    st = current_state(store, bid)
    assert st["affinity"]["vex->greer"]["value"] == 6


# ------------------------------ item names: counts + worn tags ------------------------
def test_split_item_qty_number_words_and_worn_tags():
    assert _split_item_qty("two spent King's Coins on a cord") == \
        ("spent King's Coin on a cord", 2)
    assert _split_item_qty("pair of boots") == ("boot", 2)
    assert _split_item_qty("dozen iron nails") == ("iron nail", 12)
    assert _split_item_qty("Health Potion x3") == ("Health Potion", 3)
    assert _split_item_qty("(parchment)") == ("(parchment)", None)
    assert _split_item_qty("glass box") == ("glass box", None)          # no false plural
    assert _split_item_qty("stolen military dive-rig (worn)")[0] == "stolen military dive-rig"
    assert _worn_flag("cloak (worn)") is True
    assert _worn_flag("cloak (carried)") is False
    assert _worn_flag("cloak") is None


def test_worn_rig_auto_equips_and_worn_tag_wins():
    cfg = _rpg_cfg()
    cls = classify_item("stolen military dive-rig")
    assert cls["class"] == "gear" and cls["worn"] and cls["slot"] == "body"
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "item_gain", "char": "vex", "name": "stolen military dive-rig"}],
        "user", cfg)
    st = current_state(store, bid)
    locs = {r["name"]: r["loc"] for r in st["items"].values()}
    assert locs["stolen military dive-rig"] == "gear:body"

    store2, sid2, bid2 = _seeded(cfg)                 # '(worn)' forces a non-worn-classified
    apply_delta(store2, sid2, bid2, 1, [              # item onto the doll when its slot is free
        {"op": "item_gain", "char": "vex", "name": "odd lantern (worn)"}],
        "user", cfg)
    st2 = current_state(store2, bid2)
    row = next(r for r in st2["items"].values() if r["name"] == "odd lantern")
    assert row["loc"].startswith("gear:"), "(worn) is an explicit author signal"
    store3, sid3, bid3 = _seeded(cfg)                 # both in one turn: first takes body,
    apply_delta(store3, sid3, bid3, 1, [              # second falls back to carried (the
        {"op": "item_gain", "char": "vex", "name": "stolen military dive-rig"},   # occupied-
        {"op": "item_gain", "char": "vex", "name": "odd lantern (worn)"}],        # slot rule)
        "user", cfg)
    st3 = current_state(store3, bid3)
    locs3 = {r["name"]: r["loc"] for r in st3["items"].values()}
    assert locs3["stolen military dive-rig"] == "gear:body"
    assert locs3["odd lantern"].startswith("inv:")


def test_opposition_die_renders_and_is_deterministic():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "greer", "present": True},
        {"op": "affinity_adj", "target": "greer", "delta": -15, "reason": "drew a knife"}],
        "extraction", cfg)
    st = current_state(store, bid)
    st["meta"]["turn"] = 4
    a = _render_directive(st, cfg)
    b = _render_directive(st, cfg)
    assert "[OPPOSITION]" in a and "2d6=" in a and "Vex" in a
    assert a == b, "same turn, same die — deterministic re-render"
    st["meta"]["turn"] = 5
    assert "[OPPOSITION]" in _render_directive(st, cfg)


def test_opposition_absent_without_present_hostile_or_under_none():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)                    # no one else present
    assert "[OPPOSITION]" not in _render_directive(st, cfg)
    apply_delta(store, sid, bid, 1, [                 # present but FRIENDLY: no enemy die
        {"op": "presence", "entity": "greer", "present": True}], "user", cfg)
    st_f = current_state(store, bid)
    assert "[OPPOSITION]" not in _render_directive(st_f, cfg)
    st_c = current_state(store, bid)                  # combat phase arms it without affinity
    st_c.setdefault("scene", {})["phase"] = "climax"
    assert "[OPPOSITION]" in _render_directive(st_c, cfg)
    cfg2 = _rpg_cfg()
    cfg2.specialization.enemy_rolls = False           # the knob turns it off
    st2 = current_state(store, bid)
    st2.setdefault("scene", {})["phase"] = "climax"
    assert "[OPPOSITION]" not in _render_directive(st2, cfg2)
    from aetherstate.compose import render_header
    none_cfg = Config()                               # none: no RPG blocks at all
    assert "[OPPOSITION]" not in render_header(st2, none_cfg)


# ------------------------------ R8b: DM-called checks ---------------------------------
def _doc(assistant, user):
    return {"messages": [{"role": "user", "content": "earlier"},
                         {"role": "assistant", "content": assistant},
                         {"role": "user", "content": user}]}


def test_dm_called_check_arms_on_plain_prose():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("Greer hesitates. That's an ((aether.check persuasion)) if you push him.",
               "Vex leans in and pushes, voice low and even.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert len(checks) == 1 and checks[0]["skill"] == "persuasion"
    assert checks[0].get("_dm_called") is True


def test_dm_called_multiword_display_name_resolves():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("Forcing it is an ((aether.check dive-rig operation)) — the lattice resists.",
               "Vex plants her palm on the lattice and holds the connection open.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert len(checks) == 1 and checks[0]["skill"] == "dive_rig_operation"


def test_player_explicit_check_beats_dm_call():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("That's an ((aether.check persuasion)) if you push.",
               "((aether.check dive_rig_operation)) Vex ignores him and works the rig.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(5))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    assert len(checks) == 1 and checks[0]["skill"] == "dive_rig_operation"
    assert not checks[0].get("_dm_called")


def test_dm_called_check_none_session_inert():
    cfg = Config()                                     # spec none
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("That's an ((aether.check persuasion)) if you push.",
               "Vex leans in and pushes.")
    res = tier0.run(doc, "new_turn", False, state, cfg, random.Random(7))
    assert not [o for o in res.rule_ops if o.get("op") == "check"]


def test_dm_called_check_replay_deterministic():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    doc = _doc("That's an ((aether.check persuasion)).", "Vex pushes.")
    res = tier0.run(doc, "new_turn", False, state, cfg, _Rig(6))
    checks = [o for o in res.rule_ops if o.get("op") == "check"]
    apply_delta(store, sid, bid, 1, checks, "rule", cfg)
    st1 = current_state(store, bid)
    st2 = current_state(store, bid)                    # replay from journal again
    assert st1["rolls"][-1] == st2["rolls"][-1]
    assert st1["rolls"][-1]["dm_called"] is True


def test_directive_marks_dm_called_and_survives_defeat_row():
    state = {"meta": {"turn": 3},
             "_fresh_checks": [{"op": "check", "skill": "persuasion", "tier": "success",
                                "result": 9, "_dm_called": True}],
             "player": {"vex": {"defeated": {"turn": 3, "outcome": "captured"}}},
             "entities": {"vex": {"name": "Vex"}}, "rolls": []}
    out = _render_directive(state)
    assert "the roll YOU called for" in out
    assert "DEFEATED" in out                           # the turn-scope NameError is gone


# ------------------------------ NL detection over defs skills -------------------------
def test_nl_detects_rank0_defs_skill_by_governs_verb():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    state = current_state(store, bid)
    res = tier0.Tier0Result()
    tier0._detect_nl_checks("I dive for the flooded stairwell.", state, cfg, res)
    assert any(c["skill"] == "dive_rig_operation" for c in res.checks)


# ------------------------------ creator freeze quality --------------------------------
def test_coerce_infers_mechanic_from_effect_text():
    reg = registry.load(_rpg_cfg())
    out = _coerce_defs({"abilities": [
        {"name": "Keen Nose", "kind": "passive",
         "effect": "Roll an extra die on Perception checks and keep the best.",
         "applies_to": "perception"},
        {"name": "Dead Man Switch", "kind": "active",
         "effect": "On a failed roll, roll another die and keep the best."}]}, reg)
    ab = out["abilities"]
    assert ab["keen_nose"]["mechanic"] == "edge"
    assert ab["dead_man_switch"]["mechanic"] == "extra_die"
    assert ab["dead_man_switch"]["cost"]                 # actives get a real cost floor


def test_coerce_drops_bare_registry_echo_keeps_real_override():
    reg = registry.load(_rpg_cfg())
    out = _coerce_defs({"abilities": [
        {"name": "Silver Tongue", "kind": "passive",
         "effect": "you talk like you've rehearsed"},               # bare echo -> dropped
        {"name": "Keen Senses", "kind": "passive", "mechanic": "edge",
         "applies_to": "perception", "magnitude": 2}]}, reg)        # real override -> kept
    ab = out.get("abilities", {})
    assert "silver_tongue" not in ab
    assert ab["keen_senses"]["mechanic"] == "edge"


def test_coerce_governs_fallback_from_name():
    reg = registry.load(_rpg_cfg())
    out = _coerce_defs({"skills": [
        {"name": "Free Dive", "keyed_stat": "CON"}]}, reg)
    assert "dive" in out["skills"]["free_dive"]["governs"]


# ------------------------------ narrator: no mid-word cuts ----------------------------
def test_s_sent_never_cuts_mid_word():
    long = ("Recover the last transmission before the Bureau destroys it; but first, "
            "find out who the third name belongs to and why the terminal chose you.")
    cut = _s_sent(long, 80)
    assert not re.search(r"[a-z]$", cut) or cut.endswith("…") or cut.endswith(".")
    assert "find out wh" not in cut or "who" in cut


# ------------------------------ genesis: narrator card is not a person -----------------
def test_genesis_narrator_card_not_staged_as_character():
    card = ("THE WORLD — Greywater Spire (cyberpunk)\nA half-drowned arcology.\n"
            "You are the Narrator: speak the world and its people, never the Player.")
    ops = rules_ops(card, "hello", speaker="Greywater Spire")
    assert not any(o["op"] == "presence" for o in ops)
    assert not any(o["op"] == "entity_add" and o.get("name") == "Greywater Spire"
                   for o in ops)
    normal = rules_ops("Name: Tam\nA ferryman.", "hello", speaker="Tam")
    assert any(o["op"] == "presence" for o in normal)


# ------------------------------ the version tells the truth ---------------------------
def test_version_matches_pyproject():
    pp = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    m = re.search(r'^version\s*=\s*"([^"]+)"', pp.read_text(encoding="utf-8"), re.M)
    assert m and __version__ == m.group(1)
    assert __version__ != "1.0.0"
