"""RPG-3 (doc 05 §9): Statuses & Conditions + the eligibility gate. The ledger — not the
prose — owns the truth: effect_add/remove/update (proposable; preset snapshot baked, open
vocabulary layered on top), the R9 tag protocol (the channel AI-Roguelite never had), dynamic
valence, the data-driven `requires` gate, effect mods flowing into R8, derived expiry, the
[EFFECTS] block, the privileged ability_grant acquisition route, basis-gated skills as
NON-MOVES, and scope-gated power (punishing odds + low ceiling, never a flat veto). Every
class carries a `none`-leak or deterministic-replay guard (the RPG invariants).
"""
from __future__ import annotations

import random

from aetherstate import registry, tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.extraction import (EXTRACTION_OPS, delta_json_schema,
                                    delta_json_schema_anyof, scrub_op)
from aetherstate.prompts import rules_contract, system_prompt
from aetherstate.state import (CHECK_TIERS, apply_delta, authority_violation, current_state,
                               empty_state, reduce_state, validate_op)
from aetherstate.store import Store
from tests.mock_upstream import Reply


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    """Kael (player: DEX14/INT12, stealth 3) + Mira (female NPC) — the RPG-3 fixture."""
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="rpg3")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Mira"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"DEX": 14, "INT": 12}, "skills": {"stealth": 3},
                  "resources": {"hp": {"max": 20}}}},
        {"op": "set_attribute", "entity": "Mira", "key": "sex", "value": "female"}],
        "genesis", cfg)
    return store, sid, bid


# ------------------------------ registry presets -------------------------------------
def test_effect_presets_load_and_resolve():
    reg = registry.load()
    assert {"bleeding", "poisoned", "stunned", "hasted", "cursed", "blessed", "drunk",
            "diseased", "pregnant"} <= set(reg.effects)
    assert reg.effects["bleeding"]["kind"] == "status"
    assert reg.effects["pregnant"]["requires"] == "female"          # data-driven, not hardcoded
    assert reg.resolve_effect("Bleeding") == "bleeding"             # display name
    assert reg.resolve_effect("bleeding") == "bleeding"             # id
    assert reg.resolve_effect("Dragonmarked") is None               # open vocabulary, NOT a reject


def test_effect_op_validation_shapes():
    ok = {"op": "effect_add", "char": "K", "effect": "Bleeding"}
    assert validate_op(dict(ok)) is not None
    assert validate_op({**ok, "kind": "status"}) is not None
    assert validate_op({**ok, "kind": "aura"}) is None              # unknown kind
    assert validate_op({**ok, "valence": "grim"}) is None           # unknown valence
    assert validate_op({**ok, "valence": 3}) is None                # mood-typed int quarantined
    assert validate_op({**ok, "duration": -1}) is None
    assert validate_op({**ok, "stacks": 0}) is None
    assert validate_op({"op": "effect_remove", "char": "K", "effect": ""}) is None
    assert validate_op({"op": "ability_grant", "char": "K", "ability": "x",
                        "def": "notadict"}) is None


def test_effect_authority_proposable_grant_privileged():
    cfg, st = _rpg_cfg(), empty_state()
    add = {"op": "effect_add", "char": "k", "effect": "Bleeding"}
    grant = {"op": "ability_grant", "char": "k", "ability": "arcane_gift"}
    assert authority_violation(add, "extraction", st, cfg) is None      # may propose
    assert authority_violation(grant, "extraction", st, cfg) is not None  # never bestows
    for src in ("user", "genesis", "rule"):
        assert authority_violation(grant, src, st, cfg) is None


# ------------------------------ ledger commits ---------------------------------------
def test_preset_add_bakes_snapshot_open_vocab_commits_bare():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1,
                    [{"op": "effect_add", "char": "Kael", "effect": "Bleeding"},
                     {"op": "effect_add", "char": "Kael", "effect": "Dragonmarked",
                      "kind": "condition", "valence": "positive", "note": "the Deep marked him"}],
                    "extraction", cfg)
    assert len(r.applied) == 2
    st = current_state(store, bid)
    b = st["effects"]["kael"]["bleeding"]
    assert b["kind"] == "status" and b["valence"] == "negative" and b["preset"]
    assert b["mods"] == {"all": -1} and b["duration"] == 6          # engine-owned mechanics
    d = st["effects"]["kael"]["dragonmarked"]
    assert d["name"] == "Dragonmarked" and not d["preset"] and d["mods"] == {}
    assert d["valence"] == "positive" and d["note"] == "the Deep marked him"


def test_effect_refresh_remove_and_valence_shift():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "effect_add", "char": "Kael",
                                      "effect": "Poisoned"}], "extraction", cfg)
    # re-applied -> refresh the clock + merge stacks (no duplicate ledger rows)
    apply_delta(store, sid, bid, 3, [{"op": "effect_add", "char": "Kael",
                                      "effect": "poisoned", "stacks": 2}], "extraction", cfg)
    st = current_state(store, bid)
    assert len(st["effects"]["kael"]) == 1
    rec = st["effects"]["kael"]["poisoned"]
    assert rec["gained_turn"] == 3 and rec["stacks"] == 2
    # dynamic valence: engine-tracked property, shiftable in play (doc 05 §5.4)
    apply_delta(store, sid, bid, 4, [{"op": "effect_update", "char": "Kael",
                                      "effect": "Poisoned", "valence": "neutral"}],
                "extraction", cfg)
    assert current_state(store, bid)["effects"]["kael"]["poisoned"]["valence"] == "neutral"
    # remove by display name; removing what the ledger doesn't show rejects visibly
    r = apply_delta(store, sid, bid, 5, [{"op": "effect_remove", "char": "Kael",
                                          "effect": "Poisoned"}], "extraction", cfg)
    assert len(r.applied) == 1 and not current_state(store, bid)["effects"]["kael"]
    r = apply_delta(store, sid, bid, 6, [{"op": "effect_remove", "char": "Kael",
                                          "effect": "Poisoned"}], "extraction", cfg)
    assert not r.applied and "not affecting" in r.quarantined[0]["reason"]
    r = apply_delta(store, sid, bid, 6, [{"op": "effect_update", "char": "Kael",
                                          "effect": "Poisoned", "valence": "negative"}],
                    "extraction", cfg)
    assert not r.applied and "add it before" in r.quarantined[0]["reason"]


def test_requires_female_gate_is_data_driven():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    # Kael: sex unknown -> visible reject, never a guess
    r = apply_delta(store, sid, bid, 1, [{"op": "effect_add", "char": "Kael",
                                          "effect": "Pregnant"}], "extraction", cfg)
    assert not r.applied and "female" in r.quarantined[0]["reason"]
    # Mira: sex=female attribute -> applies, neutral by default
    r = apply_delta(store, sid, bid, 1, [{"op": "effect_add", "char": "Mira",
                                          "effect": "Pregnant"}], "extraction", cfg)
    assert len(r.applied) == 1
    rec = current_state(store, bid)["effects"]["mira"]["pregnant"]
    assert rec["valence"] == "neutral"
    # pronouns satisfy the gate too (Player Card path)
    apply_delta(store, sid, bid, 2, [{"op": "player_seed", "entity": "Kael",
                                      "card": {"pronouns": "she/her"}}], "user", cfg)
    r = apply_delta(store, sid, bid, 3, [{"op": "effect_add", "char": "Kael",
                                          "effect": "Pregnant"}], "extraction", cfg)
    assert len(r.applied) == 1


# ------------------------------ R9: the tag protocol ---------------------------------
def test_tag_parser_gained_lost_shift_and_user_macro():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    # live_recalc (Bean 2026-07-07): tags parse from the FRESH reply on the cold path via
    # parse_reply_tags — the same regex spine, now reading the newest output on its own turn.
    reply = ("The spikes snap out — a gash opens deep across the thigh. "
             "[status gained | {{user}} | Bleeding | negative]\n"
             "[condition gained | Mira | Marked by the Deep]\n"
             "[status lost | Mira | Poisoned]\n"
             "[valence shift | Mira | Marked by the Deep | positive]\n"
             "[negative dice roll] [some other bracket | x]")
    ops = tier0.parse_reply_tags(reply, st)
    assert [o["op"] for o in ops] == ["effect_add", "effect_add", "effect_remove",
                                      "effect_update"]
    assert ops[0] == {"op": "effect_add", "char": "kael", "effect": "Bleeding",
                      "kind": "status", "valence": "negative"}     # {{user}} -> the Player Card
    assert ops[1]["kind"] == "condition" and ops[1]["char"] == "Mira"
    assert ops[3]["valence"] == "positive"
    # applied as PROPOSALS (extraction source): commit + quarantine both visible
    r = apply_delta(store, sid, bid, 1, ops, "extraction", cfg)
    assert len(r.applied) == 3                                     # Poisoned wasn't active
    assert "not affecting" in r.quarantined[0]["reason"]
    assert current_state(store, bid)["effects"]["kael"]["bleeding"]["preset"]


def test_tag_parser_inert_under_none_and_non_new_turns():
    store, sid, bid = _seeded(_rpg_cfg())
    st = current_state(store, bid)
    doc = {"messages": [
        {"role": "assistant", "content": "[status gained | Kael | Bleeding | negative]"},
        {"role": "user", "content": "hi"}]}
    assert not tier0.run(doc, "new_turn", False, st, Config(), random.Random(1)).proposal_ops
    assert not tier0.run(doc, "swipe", False, st, _rpg_cfg(), random.Random(1)).proposal_ops
    assert not tier0.run(doc, "new_turn", True, st, _rpg_cfg(), random.Random(1)).proposal_ops


# ------------------------------ mechanics: mods + expiry ------------------------------
def test_effect_mods_flow_into_resolution_and_expire():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    doc = {"messages": [{"role": "user", "content": "((aether.check stealth))"}]}
    m0 = [o for o in tier0.run(doc, "new_turn", False, current_state(store, bid), cfg,
                               random.Random(7)).rule_ops if o["op"] == "check"][0]["_mod"]
    apply_delta(store, sid, bid, 1, [{"op": "effect_add", "char": "Kael",
                                      "effect": "Hasted"}], "user", cfg)
    st = current_state(store, bid)
    m1 = [o for o in tier0.run(doc, "new_turn", False, st, cfg,
                               random.Random(7)).rule_ops if o["op"] == "check"][0]["_mod"]
    assert m0 == 5 and m1 == 6                       # DEX+2 rank3 (+ hasted all+1)
    # expiry is DERIVED (gained_turn + duration), never mutated: replay-pure
    assert registry.effect_skill_mod(st, "kael", "stealth", 3) == 1
    assert registry.effect_skill_mod(st, "kael", "stealth", 4) == 0   # 3 turns from turn 1
    assert registry.effect_active(st["effects"]["kael"]["hasted"], 3)
    assert not registry.effect_active(st["effects"]["kael"]["hasted"], 4)


# ------------------------------ the eligibility gate ----------------------------------
def test_gated_skill_is_a_non_move_until_granted():
    """Doc 10: 'you cannot assert capability into existence' — a warrior typing mana is a
    NON-MOVE (no roll, no op), and the SAME declaration rolls once the basis is earned."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    doc = {"messages": [{"role": "user", "content": "((aether.check spellcraft))"}]}
    res = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(3))
    assert not [o for o in res.rule_ops if o["op"] == "check"]
    assert any("no in-world basis" in n for n in res.notices)
    # the acquisition route: quest reward grants the ability (privileged), then it rolls
    apply_delta(store, sid, bid, 1, [{"op": "ability_grant", "char": "Kael",
                                      "ability": "arcane_gift"}], "user", cfg)
    res = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(3))
    assert [o for o in res.rule_ops if o["op"] == "check"]
    assert not any("no in-world basis" in n for n in res.notices)


def test_ability_grant_def_freezes_and_unknown_rejects():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1, [{"op": "ability_grant", "char": "Kael",
                                          "ability": "godhood"}], "user", cfg)
    assert not r.applied and "acquired in-world" in r.quarantined[0]["reason"]
    r = apply_delta(store, sid, bid, 1, [{"op": "ability_grant", "char": "Kael",
                                          "ability": "Void Step",
                                          "def": {"name": "Void Step", "kind": "passive",
                                                  "passive_mod": {"skill": "stealth",
                                                                  "amount": 1}}}], "user", cfg)
    assert len(r.applied) == 1
    pl = current_state(store, bid)["player"]["kael"]
    assert "void_step" in pl["abilities"] and "void_step" in pl["defs"]["abilities"]
    reg = registry.load(cfg)
    assert reg.has_ability(pl, "void_step")          # frozen def IS a basis
    assert reg.effective_mod(pl, "stealth") == 6     # DEX+2 rank3 + frozen passive 1


def test_scope_scales_odds_and_caps_ceiling():
    """Doc 10: freedom at the top, coherence by difficulty — a thin skill may attempt an
    epic feat (the door is open), but the roll is punishing and the ceiling low."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "ability_grant", "char": "Kael",
                                      "ability": "arcane_gift"}], "user", cfg)
    st = current_state(store, bid)
    epic = {"messages": [{"role": "user", "content": "((aether.check spellcraft scope epic))"}]}
    for seed in range(60):                           # rank 0 vs epic: over=3, -6, ceiling partial
        op = [o for o in tier0.run(epic, "new_turn", False, st, cfg,
                                   random.Random(seed)).rule_ops if o["op"] == "check"][0]
        assert op["_scope_over"] == 3 and op["scope"] == "epic"
        assert CHECK_TIERS.index(op["tier"]) <= CHECK_TIERS.index("partial")
    within = {"messages": [{"role": "user", "content": "((aether.check stealth scope major))"}]}
    op = [o for o in tier0.run(within, "new_turn", False, st, cfg,
                               random.Random(1)).rule_ops if o["op"] == "check"][0]
    assert op["_scope_over"] == 0 and op["_mod"] == 5   # rank 3 covers major: no penalty, no cap


# ------------------------------ render + none gate -----------------------------------
def test_effects_block_renders_and_none_gate():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "effect_add", "char": "Kael", "effect": "Bleeding"},
        {"op": "effect_add", "char": "Kael", "effect": "Blessed"},
        {"op": "effect_add", "char": "Mira", "effect": "Pregnant"}], "user", cfg)
    st = current_state(store, bid)
    h = render_header(st, cfg)
    assert "[EFFECTS] Kael: Bleeding(-)[6t], Blessed(+) · Mira: Pregnant(~)" in h
    h_none = render_header(st, Config())
    assert "[EFFECTS]" not in h_none                 # inert under none (invariant 3)


def test_rules_contract_carries_tag_protocol():
    rc = rules_contract(_rpg_cfg())
    assert rc.startswith("[RULES]") and "[TAGS]" in rc
    assert "status gained" in rc and "valence shift" in rc
    assert "Bleeding" in rc and "Pregnant" in rc     # the compact preset slice (anti-drift)


# ------------------------------ replay purity ----------------------------------------
def test_deterministic_replay_reproduces_effects():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "effect_add", "char": "Kael", "effect": "Bleeding"},
        {"op": "effect_add", "char": "Mira", "effect": "Dragonmarked", "kind": "condition"},
        {"op": "ability_grant", "char": "Kael", "ability": "arcane_gift"}], "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "effect_update", "char": "Kael",
                                      "effect": "Bleeding", "valence": "neutral"}],
                "user", cfg)
    live = current_state(store, bid)
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay["effects"] == live["effects"]      # journal -> identical ledger
    assert replay["player"]["kael"]["abilities"] == live["player"]["kael"]["abilities"]
    assert replay["effects"]["kael"]["bleeding"]["mods"] == {"all": -1}   # baked, not re-read


# ------------------------------ extraction wire (rpg-gated) --------------------------
def test_effect_wire_gated_and_mood_untouched():
    base = delta_json_schema()
    bp = base["schema"]["properties"]["ops"]["items"]["properties"]
    assert "effect" not in bp and bp["valence"] == {"type": ["integer", "null"]}   # none: 1.0 bytes
    assert bp["op"]["enum"] == EXTRACTION_OPS
    rp = delta_json_schema(True)["schema"]["properties"]["ops"]["items"]["properties"]
    assert "effect_add" in rp["op"]["enum"] and rp["kind"]["enum"] == ["condition", "status", None]
    kinds_base = {b["properties"]["op"]["enum"][0] for b in
                  delta_json_schema_anyof()["schema"]["properties"]["ops"]["items"]["anyOf"]}
    rpg_anyof = delta_json_schema_anyof(True)["schema"]["properties"]["ops"]["items"]["anyOf"]
    kinds_rpg = {b["properties"]["op"]["enum"][0] for b in rpg_anyof}
    assert "effect_add" not in kinds_base and "effect_add" in kinds_rpg
    assert "ability_grant" not in kinds_rpg          # privileged: never on the wire
    mood = next(b for b in rpg_anyof if b["properties"]["op"]["enum"] == ["mood"])
    assert mood["properties"]["valence"]["type"] == ["integer", "null"]   # mood stays numeric
    ea = next(b for b in rpg_anyof if b["properties"]["op"]["enum"] == ["effect_add"])
    assert ea["properties"]["valence"]["type"] == ["string", "null"]
    assert "negative" in ea["properties"]["valence"]["enum"]
    assert "RPG EFFECT OPS" not in system_prompt(2) and "RPG EFFECT OPS" in system_prompt(2, rpg=True)
    scrubbed = scrub_op({"op": "effect_add", "char": "a", "effect": "x", "mods": {"all": 9}})
    assert "mods" not in scrubbed                    # the model NEVER authors mechanics


# ------------------------------ live proxy e2e ---------------------------------------
SENT = "<<AETHER:v=1;session={s};turn={t};type=normal;speaker=Dungeon Master;user=Bean>>"


def _payload(session, turn, user="I press on."):
    return {"model": "m", "messages": [
        {"role": "system", "content": SENT.format(s=session, t=turn) + " A cold keep at dusk."},
        {"role": "user", "content": user}]}


async def test_none_session_no_effect_leak_e2e(client, mock_upstream, cfg):
    """A `none` session forwards byte-identically: no [EFFECTS], no [TAGS], no effect vocab."""
    assert cfg.specialization.name == "none"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3-none", 1))
    body = mock_upstream.requests[0].body
    assert b"[EFFECTS]" not in body and b"[TAGS]" not in body and b"effect_" not in body


async def test_rpg_effects_ledger_e2e(client, mock_upstream, cfg):
    """Flagship RPG-3 exit: an effect committed through the control API renders as the
    [EFFECTS] ledger in the very next forwarded request — truth fed back every turn."""
    cfg.specialization.name = "rpg"
    cfg.injection.max_tokens = 2200          # RPG mode's profile budget (contract ~1k + sheet)
    cfg.user_guard.name = "Bean"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3", 1))
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    r = await client.patch(f"/aether/session/{sid}/state", json={"ops": [
        {"op": "effect_add", "char": "Bean", "effect": "Cursed"}]})
    assert r.json()["applied"] == 1
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg3", 2))
    fwd = next(r.body for r in reversed(mock_upstream.requests)
               if b"I press on." in r.body)
    assert b"[EFFECTS]" in fwd and b"Cursed(-)" in fwd
    assert b"[TAGS]" in fwd                          # the protocol reminder rides the contract
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert now["state"]["effects"]["bean"]["cursed"]["kind"] == "condition"
