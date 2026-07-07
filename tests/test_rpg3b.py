"""RPG-3b (doc 05 §9): the social plane — affinity ledgers + derived tiers, factions +
the deterministic cascade, the privileged soulmate/nemesis bonds (demote-then-set +
one_soulmate), world flags, the [FACTIONS]/[RELATIONS]/[WORLD] blocks, the rpg-gated
OOC set-paths, and the rpg-gated extraction wire. Truth lives in the ledger: the tier
label is DERIVED from the clamped ledger sum, never stored. Every class carries a
`none`-leak or deterministic-replay guard (the RPG invariants).
"""
from __future__ import annotations

import json
import random

from aetherstate import linter, tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.extraction import (EXTRACTION_OPS, delta_json_schema,
                                    delta_json_schema_anyof, scrub_op)
from aetherstate.prompts import system_prompt
from aetherstate.state import (AFFINITY_DELTA_CLAMP, DEVOTED_MIN, affinity_tier, apply_delta,
                               authority_violation, current_state, empty_state,
                               faction_cascade_ops, reduce_state, state_summary,
                               translate_path, validate_op)
from aetherstate.store import Store
from tests.mock_upstream import Reply


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    """Kael (player) + Mira (Iron Covenant member) + Seraphine + the Iron Covenant faction."""
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="rpg3b")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Mira"},
        {"op": "entity_add", "name": "Seraphine"},
        {"op": "entity_add", "name": "Iron Covenant", "kind": "faction"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"DEX": 14}, "skills": {"stealth": 3}}},
        {"op": "set_attribute", "entity": "Mira", "key": "faction", "value": "Iron Covenant"}],
        "genesis", cfg)
    return store, sid, bid


# ------------------------------ derived tiers ----------------------------------------
def test_affinity_tier_table_matches_doc_06():
    assert affinity_tier(100) == "Devoted" and affinity_tier(80) == "Devoted"
    assert affinity_tier(79) == "Ally" and affinity_tier(40) == "Ally"
    assert affinity_tier(39) == "Warm" and affinity_tier(10) == "Warm"
    assert affinity_tier(9) == "Neutral" and affinity_tier(-9) == "Neutral"
    assert affinity_tier(-10) == "Cold" and affinity_tier(-39) == "Cold"
    assert affinity_tier(-40) == "Hostile" and affinity_tier(-79) == "Hostile"
    assert affinity_tier(-80) == "Nemesis" and affinity_tier(-100) == "Nemesis"
    assert affinity_tier("garbage") == "Neutral"        # total, never raises


# ------------------------------ validation + authority -------------------------------
def test_social_op_validation_shapes():
    ok = {"op": "affinity_adj", "target": "Mira", "delta": 8}
    assert validate_op(dict(ok)) is not None
    assert validate_op({**ok, "delta": "lots"}) is None
    assert validate_op({**ok, "delta": True}) is None            # bool is not a delta
    assert validate_op({**ok, "kind": "guild"}) is None
    assert validate_op({**ok, "kind": "faction"}) is not None
    assert validate_op({"op": "set_soulmate", "target": None}) is not None   # null = clear
    assert validate_op({"op": "set_soulmate", "target": ""}) is None
    assert validate_op({"op": "set_soulmate", "target": "Mira",
                        "demote_label": 3}) is None
    assert validate_op({"op": "world_flag", "key": "", "value": "x"}) is None
    assert validate_op({"op": "world_flag", "key": "plague", "value": {"a": 1}}) is None
    assert validate_op({"op": "world_flag", "key": "plague", "value": None}) is not None
    for v in ("spreading", 3, True):
        assert validate_op({"op": "world_flag", "key": "plague", "value": v}) is not None


def test_social_authority_matrix():
    cfg, st = _rpg_cfg(), empty_state()
    aff = {"op": "affinity_adj", "target": "m", "delta": 5}
    wf = {"op": "world_flag", "key": "plague", "value": "spreading"}
    bond = {"op": "set_soulmate", "target": "m"}
    assert authority_violation(aff, "extraction", st, cfg) is None      # may propose
    assert authority_violation(wf, "extraction", st, cfg) is None      # may propose
    assert authority_violation(bond, "extraction", st, cfg) is not None  # never seals bonds
    assert authority_violation({**bond, "op": "set_nemesis"}, "extraction", st, cfg) is not None
    for src in ("user", "genesis", "rule"):
        assert authority_violation(bond, src, st, cfg) is None
    assert authority_violation(aff, "user", st, cfg) is not None       # organic: override-gated
    cfg.manual_override.enabled = True
    assert authority_violation(aff, "user", st, cfg) is None
    frozen = {**empty_state(), "frozen": True}
    assert authority_violation(aff, "extraction", frozen, cfg) is not None  # frozen: paused
    assert authority_violation(aff, "rule", frozen, cfg) is None       # cascades keep running


# ------------------------------ the ledger --------------------------------------------
def test_affinity_ledger_commit_clamp_reason_and_tier():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [{"op": "affinity_adj", "target": "Mira",
                                          "delta": 40}], "extraction", cfg)
    assert len(r.applied) == 1
    assert r.applied[0]["_delta"] == AFFINITY_DELTA_CLAMP[1]     # baked per-turn clamp
    st = current_state(store, bid)
    entry = st["affinity"]["kael->mira"]
    assert entry["value"] == 15 and entry["kind"] == "npc"
    assert entry["ledger"][0]["delta"] == 15
    apply_delta(store, sid, bid, 2, [{"op": "affinity_adj", "target": "Mira", "delta": -8,
                                      "reason": "lied about the ledger"}], "extraction", cfg)
    entry = current_state(store, bid)["affinity"]["kael->mira"]
    assert entry["value"] == 7 and entry["ledger"][-1]["reason"] == "lied about the ledger"
    assert affinity_tier(entry["value"]) == "Neutral"
    summ = state_summary(current_state(store, bid))
    assert summ["affinity"]["kael->mira"]["tier"] == "Neutral"   # derived, inspector-facing
    # the player is not a target; unknown names quarantine (discovery counts evidence)
    r = apply_delta(store, sid, bid, 3, [{"op": "affinity_adj", "target": "Kael",
                                          "delta": 5}], "extraction", cfg)
    assert not r.applied and "not a thing" in r.quarantined[0]["reason"]
    r = apply_delta(store, sid, bid, 3, [{"op": "affinity_adj", "target": "Stranger",
                                          "delta": 5}], "extraction", cfg)
    assert not r.applied and "unknown entity" in r.quarantined[0]["reason"]


def test_affinity_needs_a_player_card():
    cfg = _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="nocard")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Mira"}], "user", cfg)
    r = apply_delta(store, sid, bid, 1, [{"op": "affinity_adj", "target": "Mira",
                                          "delta": 5}], "rule", cfg)
    assert not r.applied and "no Player Card" in r.quarantined[0]["reason"]


def test_affinity_toward_faction_derives_kind_and_mirrors_factions():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "affinity_adj", "target": "Iron Covenant",
                                      "delta": 12}], "extraction", cfg)
    st = current_state(store, bid)
    entry = st["affinity"]["kael->iron_covenant"]
    assert entry["kind"] == "faction"
    assert st["factions"]["iron_covenant"]["name"] == "Iron Covenant"


# ------------------------------ bonds -------------------------------------------------
def test_soulmate_demote_then_set_clear_and_nemesis():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "affinity_adj", "target": "Seraphine",
                                      "delta": 15}] * 6, "rule", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "set_soulmate", "target": "Seraphine"}],
                "user", cfg)
    st = current_state(store, bid)
    assert st["player"]["kael"]["soulmate"] == "seraphine"
    assert st["affinity"]["kael->seraphine"]["value"] == 90 >= DEVOTED_MIN
    # promotion demotes the incumbent — uniqueness by construction (doc 07 §7.8)
    apply_delta(store, sid, bid, 3, [{"op": "affinity_adj", "target": "Mira",
                                      "delta": 15}] * 6, "rule", cfg)
    apply_delta(store, sid, bid, 4, [{"op": "set_soulmate", "target": "Mira"}], "user", cfg)
    st = current_state(store, bid)
    assert st["player"]["kael"]["soulmate"] == "mira"
    assert "beloved" in st["affinity"]["kael->seraphine"]["labels"]
    apply_delta(store, sid, bid, 5, [{"op": "set_soulmate", "target": None}], "user", cfg)
    assert current_state(store, bid)["player"]["kael"]["soulmate"] is None
    apply_delta(store, sid, bid, 6, [{"op": "set_nemesis", "target": "Seraphine",
                                      "demote_label": "old enemy"}], "user", cfg)
    assert current_state(store, bid)["player"]["kael"]["nemesis"] == "seraphine"


# ------------------------------ world flags -------------------------------------------
def test_world_flag_global_faction_scoped_and_clear():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "world_flag", "key": "Plague", "value": "spreading"},
        {"op": "world_flag", "key": "gates_sealed", "value": True},
        {"op": "world_flag", "key": "at_war", "value": True, "faction": "Iron Covenant"}],
        "rule", cfg)
    st = current_state(store, bid)
    assert st["world"] == {"plague": "spreading", "gates_sealed": True}   # keys slugged
    assert st["factions"]["iron_covenant"]["circumstances"] == {"at_war": True}
    apply_delta(store, sid, bid, 2, [
        {"op": "world_flag", "key": "plague", "value": None},
        {"op": "world_flag", "key": "at_war", "value": None, "faction": "Iron Covenant"}],
        "rule", cfg)
    st = current_state(store, bid)
    assert st["world"] == {"gates_sealed": True}
    assert st["factions"]["iron_covenant"]["circumstances"] == {}


# ------------------------------ the faction cascade -----------------------------------
def test_faction_cascade_deterministic_and_bounded():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    up = [{"op": "affinity_adj", "target": "mira", "delta": 40, "_delta": 15}]
    assert faction_cascade_ops(st, up, 0.1) == [
        {"op": "affinity_adj", "target": "iron_covenant", "delta": 2, "kind": "faction",
         "reason": "standing with Mira"}]                       # 1.5 rounds away from zero
    down = [{"op": "affinity_adj", "target": "mira", "delta": -15, "_delta": -15}]
    assert faction_cascade_ops(st, down, 0.1)[0]["delta"] == -1  # negatives halved
    small = [{"op": "affinity_adj", "target": "mira", "delta": 4, "_delta": 4}]
    assert faction_cascade_ops(st, small, 0.1) == []             # below the rounding floor
    fac = [{"op": "affinity_adj", "target": "iron_covenant", "delta": 15, "_delta": 15}]
    assert faction_cascade_ops(st, fac, 0.1) == []               # cascades never chain
    assert faction_cascade_ops(st, up, 0) == []                  # knob off
    r = apply_delta(store, sid, bid, 1, faction_cascade_ops(st, up, 0.1), "rule", cfg)
    assert len(r.applied) == 1
    assert current_state(store, bid)["affinity"]["kael->iron_covenant"]["value"] == 2


# ------------------------------ render + none gate ------------------------------------
def _social_fixture(cfg):
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "affinity_adj", "target": "Mira", "delta": 12}], "rule", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "affinity_adj", "target": "Seraphine",
                                      "delta": 15}] * 6, "rule", cfg)
    apply_delta(store, sid, bid, 3, [{"op": "set_soulmate", "target": "Seraphine"}],
                "user", cfg)
    apply_delta(store, sid, bid, 4, [{"op": "affinity_adj", "target": "Iron Covenant",
                                      "delta": 15}] * 3, "rule", cfg)
    apply_delta(store, sid, bid, 5, [
        {"op": "world_flag", "key": "plague", "value": "spreading"},
        {"op": "world_flag", "key": "at_war", "value": True, "faction": "Iron Covenant"}],
        "rule", cfg)
    return store, sid, bid


def test_social_blocks_render_and_none_gate():
    cfg = _rpg_cfg()
    store, sid, bid = _social_fixture(cfg)
    st = current_state(store, bid)
    h = render_header(st, cfg)
    assert "[FACTIONS] Iron Covenant: Ally (at_war=yes)" in h
    assert "[RELATIONS]" in h and "Mira: Warm" in h
    assert "Seraphine: Devoted ♥soulmate" in h       # bonds render even for absent chars
    assert "[WORLD] plague=spreading" in h
    h_none = render_header(st, Config())
    for marker in ("[FACTIONS]", "[RELATIONS]", "[WORLD]"):
        assert marker not in h_none                  # inert under none (invariant 3)


def test_relations_shows_demoted_label_and_skips_absent_unbonded():
    cfg = _rpg_cfg()
    store, sid, bid = _social_fixture(cfg)
    apply_delta(store, sid, bid, 6, [{"op": "affinity_adj", "target": "Mira",
                                      "delta": 15}] * 6, "rule", cfg)
    apply_delta(store, sid, bid, 7, [{"op": "set_soulmate", "target": "Mira"},
                                     {"op": "presence", "entity": "Seraphine",
                                      "present": True}], "user", cfg)
    st = current_state(store, bid)
    h = render_header(st, cfg)
    assert "Mira: Devoted ♥soulmate" in h
    assert "Seraphine: Devoted (beloved)" in h       # a PRESENT demoted bond keeps its history
    apply_delta(store, sid, bid, 8, [{"op": "presence", "entity": "Mira",
                                      "present": False}], "rule", cfg)
    apply_delta(store, sid, bid, 9, [{"op": "set_soulmate", "target": None}], "user", cfg)
    st = current_state(store, bid)
    h = render_header(st, cfg)
    assert "Mira:" not in h.split("[RELATIONS]")[-1].split("\n")[0]   # absent + unbonded


# ------------------------------ OOC set-paths (rpg-gated) ------------------------------
def test_translate_path_social_coercion_and_gate():
    assert translate_path("world.gates_sealed", "true", rpg=True) == {
        "op": "world_flag", "key": "gates_sealed", "value": True}
    assert translate_path("world.death_toll", "42", rpg=True)["value"] == 42
    assert translate_path("world.plague", "none", rpg=True)["value"] is None
    assert translate_path("affinity.Mira", "+8", rpg=True) == {
        "op": "affinity_adj", "target": "Mira", "delta": 8, "reason": "user edit"}
    assert translate_path("affinity.Mira", "lots", rpg=True) is None
    assert translate_path("player.nemesis", "Kreed", rpg=True) == {
        "op": "set_nemesis", "target": "Kreed"}
    assert translate_path("player.soulmate", "clear", rpg=True) == {
        "op": "set_soulmate", "target": None}
    for p, v in (("world.x", "y"), ("affinity.Mira", "+8"), ("player.soulmate", "Mira")):
        assert translate_path(p, v) is None          # none: command surface unchanged
    assert translate_path("scene.location", "Tavern", rpg=True) == {
        "op": "scene_set", "location": "Tavern"}     # base paths untouched under rpg


def test_ooc_social_commands_gated_end_to_end():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    doc = {"messages": [{"role": "user", "content":
        "((aether.set world.plague spreading)) ((aether.set affinity.Mira +8)) "
        "((aether.set player.soulmate Mira)) I move."}]}
    res = tier0.run(doc, "new_turn", False, st, cfg, random.Random(1))
    assert [o["op"] for o in res.user_ops] == ["world_flag", "affinity_adj", "set_soulmate"]
    res2 = tier0.run(doc, "new_turn", False, st, Config(), random.Random(1))
    assert not res2.user_ops
    assert sum("unknown/unsupported path" in n for n in res2.notices) == 3


# ------------------------------ one_soulmate linter ------------------------------------
def _lint_state(value=40, soulmate="seraphine"):
    st = empty_state()
    st["entities"] = {
        "kael": {"kind": "player", "name": "Kael", "aliases": [], "present": True},
        "seraphine": {"kind": "character", "name": "Seraphine", "aliases": [],
                      "present": True},
        "mira": {"kind": "character", "name": "Mira", "aliases": [], "present": True}}
    st["player"] = {"kael": {"eid": "kael", "soulmate": soulmate, "nemesis": None}}
    if value is not None:
        st["affinity"] = {"kael->seraphine": {"value": value, "kind": "npc",
                                              "ledger": [], "labels": []}}
    return st


def test_one_soulmate_eligibility_referential_and_prose():
    cfg = _rpg_cfg()
    vios = linter.run(_lint_state(value=40), "", cfg, turn=3)
    soul = [x for x in vios if x.rule == "one_soulmate"]
    assert soul and soul[0].severity == "med" and "devoted" in soul[0].note.lower()
    assert not [x for x in linter.run(_lint_state(value=90), "", cfg, turn=3)
                if x.rule == "one_soulmate"]                      # earned bond: clean
    vios = linter.run(_lint_state(value=None, soulmate="ghost"), "", cfg, turn=3)
    assert any(x.rule == "one_soulmate" and "not a known character" in x.detail
               for x in vios)                                     # dangling pointer
    text = 'Mira presses close. "You are my one and only," she breathes against his jaw.'
    vios = linter.run(_lint_state(value=90), text, cfg, turn=3)
    off = [x for x in vios if x.rule == "one_soulmate"]
    assert off and "mira" in off[0].subjects and off[0].evidence
    st = _lint_state(value=90)
    st["player"]["kael"]["soulmate"] = ["seraphine", "mira"]      # structural break -> high
    vios = linter.run(st, "", cfg, turn=3)
    assert any(x.rule == "one_soulmate" and x.severity == "high" for x in vios)


def test_one_nemesis_off_by_default_and_none_inert():
    cfg = _rpg_cfg()
    st = _lint_state(value=90)
    st["player"]["kael"]["nemesis"] = "ghost"
    assert not [x for x in linter.run(st, "", cfg, turn=3) if x.rule == "one_nemesis"]
    cfg.specialization.nemesis_enabled = True                     # D6: opt-in
    assert [x for x in linter.run(st, "", cfg, turn=3) if x.rule == "one_nemesis"]
    assert not [x for x in linter.run(_lint_state(value=10), "", Config(), turn=3)
                if x.rule.startswith("one_")]                     # inert in non-RPG sessions


# ------------------------------ replay purity ------------------------------------------
def test_deterministic_replay_reproduces_the_social_plane():
    cfg = _rpg_cfg()
    store, sid, bid = _social_fixture(cfg)
    live = current_state(store, bid)
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay["affinity"] == live["affinity"]
    assert replay["factions"] == live["factions"]
    assert replay["world"] == live["world"]
    assert replay["player"]["kael"]["soulmate"] == live["player"]["kael"]["soulmate"]
    rows = store.db.execute("SELECT ops FROM ops_journal WHERE branch_id=?",
                            (bid,)).fetchall()
    journaled = [op for r in rows for op in json.loads(r["ops"])
                 if op["op"] == "affinity_adj"]
    assert journaled and all("_delta" in op for op in journaled)   # the clamp is BAKED


# ------------------------------ extraction wire (rpg-gated) ----------------------------
def test_social_wire_gated_and_bonds_never_ride():
    base = delta_json_schema()
    bp = base["schema"]["properties"]["ops"]["items"]["properties"]
    assert "faction" not in bp and bp["op"]["enum"] == EXTRACTION_OPS    # none: 1.0 bytes
    rp = delta_json_schema(True)["schema"]["properties"]["ops"]["items"]["properties"]
    assert "affinity_adj" in rp["op"]["enum"] and "world_flag" in rp["op"]["enum"]
    assert "set_soulmate" not in rp["op"]["enum"] and "faction" in rp
    rpg_anyof = delta_json_schema_anyof(True)["schema"]["properties"]["ops"]["items"]["anyOf"]
    kinds_rpg = {b["properties"]["op"]["enum"][0] for b in rpg_anyof}
    kinds_base = {b["properties"]["op"]["enum"][0] for b in
                  delta_json_schema_anyof()["schema"]["properties"]["ops"]["items"]["anyOf"]}
    assert "affinity_adj" in kinds_rpg and "affinity_adj" not in kinds_base
    assert "set_soulmate" not in kinds_rpg and "set_nemesis" not in kinds_rpg
    aff = next(b for b in rpg_anyof if b["properties"]["op"]["enum"] == ["affinity_adj"])
    assert set(aff["properties"]) == {"op", "delta", "reason", "target"}
    wf = next(b for b in rpg_anyof if b["properties"]["op"]["enum"] == ["world_flag"])
    assert set(wf["properties"]) == {"op", "faction", "key", "value"}
    assert "kind" not in scrub_op({"op": "affinity_adj", "target": "m", "delta": 5,
                                   "kind": "npc"})   # kind is DERIVED, never from the wire
    assert "RPG SOCIAL OPS" not in system_prompt(2)
    assert "RPG SOCIAL OPS" in system_prompt(2, rpg=True)


# ------------------------------ live proxy e2e -----------------------------------------
SENT = "<<AETHER:v=1;session={s};turn={t};type=normal;speaker=Dungeon Master;user=Bean>>"


def _payload(session, turn, user="I press on."):
    return {"model": "m", "messages": [
        {"role": "system", "content": SENT.format(s=session, t=turn) + " A cold keep at dusk."},
        {"role": "user", "content": user}]}


async def test_none_session_no_social_leak_e2e(client, mock_upstream, cfg):
    """A `none` session forwards byte-identically: no social blocks, no social op vocab."""
    assert cfg.specialization.name == "none"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3b-none", 1))
    body = mock_upstream.requests[0].body
    for marker in (b"[RELATIONS]", b"[FACTIONS]", b"[WORLD]", b"affinity_adj",
                   b"world_flag", b"RPG SOCIAL"):
        assert marker not in body


async def test_rpg_social_plane_e2e(client, mock_upstream, cfg):
    """Flagship RPG-3b exit: affinity + a world flag committed through the control API
    render as [RELATIONS]/[WORLD] in the very next forwarded request, tier label not raw
    number — the ledger fed back every turn."""
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    cfg.manual_override.enabled = True
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3b", 1))
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    r = await client.patch(f"/aether/session/{sid}/state", json={"ops": [
        {"op": "entity_add", "name": "Mira"},
        {"op": "presence", "entity": "Mira", "present": True},
        {"op": "affinity_adj", "target": "Mira", "delta": 12, "reason": "saved her life"},
        {"op": "world_flag", "key": "plague", "value": "spreading"}]})
    assert r.json()["applied"] == 4
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3b", 2))
    fwd = next(rq.body for rq in reversed(mock_upstream.requests)
               if b"I press on." in rq.body)
    assert b"[RELATIONS]" in fwd and "Mira: Warm".encode() in fwd
    assert b"[WORLD]" in fwd and b"plague=spreading" in fwd
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    aff = now["state"]["affinity"]
    entry = aff[next(iter(aff))]
    assert entry["tier"] == "Warm" and entry["value"] == 12
