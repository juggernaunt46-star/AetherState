"""RPG-2 (doc 05 §9): items — Inventory & Gear. Template+Instance model over the shipped spine:
`item_mint` (privileged) + move/equip/unequip/consume/transfer (proposable, transactional with
rollback), the baked template snapshot + generated instance id (replay purity), the
one-instance-one-place safety net (+ self-heal), the [GEAR]/[INVENTORY] blocks, gear mods
flowing into R8 resolution, and the rpg-gated extraction wire. Exit criteria (05 §9): item
fixtures green incl. rollback on partial failure and the duplication-bug guard; equip modifier
flows into resolution. Every test class includes a `none`-leak or deterministic-replay guard.
"""
from __future__ import annotations

import random

from aetherstate import linter, registry, tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.extraction import (EXTRACTION_OPS, delta_json_schema,
                                    delta_json_schema_anyof, scrub_op)
from aetherstate.prompts import system_prompt
from aetherstate.state import (apply_delta, authority_violation, current_state, empty_state,
                               reduce_state, validate_op)
from aetherstate.store import Store
from tests.mock_upstream import Reply


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None, hp_cur=None):
    """Store with Kael (player card: DEX14, stealth 3) — the standard RPG-2 fixture."""
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="rpg2")
    card = {"stats": {"DEX": 14}, "skills": {"stealth": 3},
            "resources": {"hp": {"max": 20}}}
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kael", "kind": "player"},
                                     {"op": "player_seed", "entity": "Kael", "card": card}],
                "genesis", cfg)
    if hp_cur is not None:
        st = current_state(store, bid)
        assert st["player"]["kael"]["hp"]["max"] == 20
        # damage via a fresh seed is not a thing — consume tests set cur through the op below
    return store, sid, bid


# ------------------------------ registry + validation ------------------------------
def test_item_templates_load_and_slots():
    reg = registry.load()
    assert {"iron_helm", "grey_mantle", "utility_belt", "short_sword",
            "healing_draught", "lockpicks"} <= set(reg.items)
    assert reg.items["grey_mantle"]["mods"] == {"stealth": 1}
    assert "head" in reg.slots and "mainhand" in reg.slots     # built-in defaults present


def test_item_op_validation_shapes():
    assert validate_op({"op": "item_mint", "template": "iron_helm", "owner": "Kael"}) is not None
    assert validate_op({"op": "item_mint", "template": "x", "owner": "K", "qty": 0}) is None
    assert validate_op({"op": "item_move", "instance": "i", "to": "attic:box"}) is None  # bad kind
    assert validate_op({"op": "item_move", "instance": "i", "to": "inv:loose"}) is not None
    assert validate_op({"op": "item_equip", "instance": "i", "slot": 3}) is None
    assert validate_op({"op": "item_consume", "instance": "i", "amount": 0}) is None
    assert validate_op({"op": "item_transfer", "instance": "i", "to_owner": "B"}) is not None


def test_item_authority_mint_privileged_moves_proposable():
    cfg, st = _rpg_cfg(), empty_state()
    mint = {"op": "item_mint", "template": "iron_helm", "owner": "kael"}
    move = {"op": "item_move", "instance": "iron_helm#1", "to": "world"}
    assert authority_violation(mint, "extraction", st, cfg) is not None   # never mints
    for src in ("user", "genesis", "rule"):
        assert authority_violation(mint, src, st, cfg) is None
    assert authority_violation(move, "extraction", st, cfg) is None      # may propose


# ------------------------------ mint: bake + reject ---------------------------------
def test_mint_bakes_snapshot_and_unique_iid():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1,
                    [{"op": "item_mint", "template": "grey_mantle", "owner": "Kael"},
                     {"op": "item_mint", "template": "grey_mantle", "owner": "Kael"}],
                    "user", cfg)
    assert len(r.applied) == 2
    iids = sorted(o["_iid"] for o in r.applied)
    assert iids == ["grey_mantle#1", "grey_mantle#2"]          # unique, monotonic per template
    assert r.applied[0]["_snapshot"]["mods"] == {"stealth": 1}
    st = current_state(store, bid)
    it = st["items"]["grey_mantle#1"]
    # worn gear whose slot is free AUTO-EQUIPS onto the paper-doll (Bean 2026-07-07);
    # the second mantle finds the cape slot taken and falls to carried.
    assert it["owner"] == "kael" and it["loc"] == "gear:cape"
    assert st["gear"]["kael"]["cape"] == "grey_mantle#1"
    assert st["items"]["grey_mantle#2"]["loc"] == "inv:loose"
    assert "grey_mantle#2" in st["inventory"]["kael"]["loose"]


def test_mint_unknown_template_rejected_visibly():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1,
                    [{"op": "item_mint", "template": "vorpal_sword", "owner": "Kael"}],
                    "user", cfg)
    assert not r.applied and len(r.quarantined) == 1
    assert "unknown item template" in r.quarantined[0]["reason"]   # nothing freestyle


# ------------------------------ equip / slot rules ----------------------------------
def test_equip_slot_rules_and_swap():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "iron_helm", "owner": "Kael"},
                 {"op": "item_mint", "template": "iron_helm", "owner": "Kael"}], "user", cfg)
    r = apply_delta(store, sid, bid, 2,
                    [{"op": "item_equip", "instance": "iron_helm#1", "slot": "head"}],
                    "user", cfg)
    assert len(r.applied) == 1
    st = current_state(store, bid)
    assert st["gear"]["kael"]["head"] == "iron_helm#1"
    assert st["items"]["iron_helm#1"]["loc"] == "gear:head"
    assert "iron_helm#1" not in st["inventory"]["kael"]["loose"]   # equipped => not carried
    # occupied slot without swap -> transactional reject
    r = apply_delta(store, sid, bid, 3,
                    [{"op": "item_equip", "instance": "iron_helm#2", "slot": "head"}],
                    "user", cfg)
    assert not r.applied and "occupied" in r.quarantined[0]["reason"]
    # swap displaces the incumbent back to inventory, atomically
    r = apply_delta(store, sid, bid, 4,
                    [{"op": "item_equip", "instance": "iron_helm#2", "slot": "head",
                      "swap": True}], "user", cfg)
    assert len(r.applied) == 1
    st = current_state(store, bid)
    assert st["gear"]["kael"]["head"] == "iron_helm#2"
    assert st["items"]["iron_helm#1"]["loc"] == "inv:loose"
    # bogus slot -> visible reject (profile slot map)
    r = apply_delta(store, sid, bid, 5,
                    [{"op": "item_equip", "instance": "iron_helm#1", "slot": "elbow"}],
                    "user", cfg)
    assert not r.applied and "unknown gear slot" in r.quarantined[0]["reason"]


def test_equip_mod_flows_into_resolution():
    """RPG-2 exit criterion (05 §9): the equipped Grey Mantle's stealth+1 lands in the check."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    doc = {"messages": [{"role": "user", "content": "I slip by. ((aether.check stealth))"}]}
    before = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(7))
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "grey_mantle", "owner": "Kael"},
                 {"op": "item_equip", "instance": "grey_mantle#1", "slot": "cape"}],
                "user", cfg)
    after = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(7))
    m0 = [o for o in before.rule_ops if o["op"] == "check"][0]["_mod"]
    m1 = [o for o in after.rule_ops if o["op"] == "check"][0]["_mod"]
    assert m0 == 5 and m1 == 6                                 # DEX+2 rank3 (+ mantle 1)


# ------------------------------ consume ---------------------------------------------
def test_consume_heals_and_depletes_to_gone():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "healing_draught", "owner": "Kael", "qty": 2}],
                "user", cfg)
    # knock hp down so the heal is observable (user op through the same authority path)
    st = current_state(store, bid)
    st["player"]["kael"]["hp"]["cur"] = 5
    store.checkpoint(bid, 1, st)
    apply_delta(store, sid, bid, 2,
                [{"op": "item_consume", "instance": "Healing Draught"}], "user", cfg)
    st = current_state(store, bid)
    assert st["player"]["kael"]["hp"]["cur"] == 13             # +8 from the BAKED on_consume
    assert st["items"]["healing_draught#1"]["qty"] == 1
    apply_delta(store, sid, bid, 3,
                [{"op": "item_consume", "instance": "healing_draught#1"}], "user", cfg)
    st = current_state(store, bid)
    it = st["items"]["healing_draught#1"]
    assert it["qty"] == 0 and it["loc"] == "gone"
    assert "healing_draught#1" not in st["inventory"]["kael"]["loose"]
    r = apply_delta(store, sid, bid, 4,
                    [{"op": "item_consume", "instance": "healing_draught#1"}], "user", cfg)
    assert not r.applied and "depleted" in r.quarantined[0]["reason"]


# ------------------------------ transfer: the duplication-bug guard ------------------
def test_transfer_rollback_on_full_container_no_duplication():
    """Partial failure -> FULL rollback (the AI-Roguelite item-transfer bug, 05 §5.3):
    the item stays with its old owner, exactly once, indexes intact."""
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "entity_add", "name": "Brakk", "kind": "npc"},
                 {"op": "item_mint", "template": "utility_belt", "owner": "Kael"}], "user", cfg)
    belt = "utility_belt#1"
    fills = [{"op": "item_mint", "template": "healing_draught", "owner": "Kael",
              "to": f"inv:{belt}"} for _ in range(4)]
    r = apply_delta(store, sid, bid, 2, fills, "user", cfg)
    assert len(r.applied) == 4                                 # belt capacity 4: exactly full
    r = apply_delta(store, sid, bid, 3,
                    [{"op": "item_mint", "template": "lockpicks", "owner": "Brakk"}],
                    "user", cfg)
    picks = r.applied[0]["_iid"]
    r = apply_delta(store, sid, bid, 4,
                    [{"op": "item_transfer", "instance": picks, "to_owner": "Kael",
                      "to": f"inv:{belt}"}], "user", cfg)
    assert not r.applied and "full" in r.quarantined[0]["reason"]
    st = current_state(store, bid)
    it = st["items"][picks]
    assert it["owner"] == "brakk" and it["loc"] == "inv:loose"     # unchanged owner + loc
    assert st["inventory"]["brakk"]["loose"].count(picks) == 1     # exactly ONE copy anywhere
    assert picks not in st["inventory"]["kael"].get(belt, [])
    # a mint into the full belt also rejects (capacity holds at creation too)
    r = apply_delta(store, sid, bid, 5,
                    [{"op": "item_mint", "template": "healing_draught", "owner": "Kael",
                      "to": f"inv:{belt}"}], "user", cfg)
    assert not r.applied and "full" in r.quarantined[0]["reason"]
    assert "healing_draught#6" not in current_state(store, bid)["items"]


def test_move_unknown_instance_rejected_visibly():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    r = apply_delta(store, sid, bid, 1,
                    [{"op": "item_move", "instance": "ghost_dagger#9", "to": "world"}],
                    "user", cfg)
    assert not r.applied and "unknown item instance" in r.quarantined[0]["reason"]


# ------------------------------ render + none gate ----------------------------------
def _geared_state(cfg):
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "iron_helm", "owner": "Kael"},
                 {"op": "item_mint", "template": "utility_belt", "owner": "Kael"},
                 {"op": "item_mint", "template": "healing_draught", "owner": "Kael", "qty": 3,
                  "to": "inv:utility_belt#1"},
                 {"op": "item_mint", "template": "lockpicks", "owner": "Kael"}], "user", cfg)
    apply_delta(store, sid, bid, 2,
                [{"op": "item_equip", "instance": "iron_helm#1", "slot": "head"},
                 {"op": "item_equip", "instance": "utility_belt#1", "slot": "waist"}],
                "user", cfg)
    return current_state(store, bid)


def test_gear_inventory_render_and_none_gate():
    cfg = _rpg_cfg()
    st = _geared_state(cfg)
    h = render_header(st, cfg)
    # Lockpicks is a TOOL -> gear-class, so it renders as STOWED gear, not inventory (Bean
    # 2026-07-07: gear = weapons/tools/accessories/bags; inventory = consumables/materials).
    assert "[GEAR] head=Iron Helm(armor+1) · waist=Utility Belt[4] · stowed: Lockpicks" in h
    assert "[INVENTORY] Utility Belt: 3× Healing Draught" in h
    assert "· loose: Lockpicks" not in h                       # the tool moved to [GEAR]
    assert "Stealth+5" in h                                    # [PLAYER] unchanged by non-skill gear
    none_cfg = Config()
    h_none = render_header(st, none_cfg)
    assert "[GEAR]" not in h_none and "[INVENTORY]" not in h_none   # inert under none


def test_player_block_includes_equipped_skill_mod():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "grey_mantle", "owner": "Kael"},
                 {"op": "item_equip", "instance": "grey_mantle#1", "slot": "cape"}],
                "user", cfg)
    h = render_header(current_state(store, bid), cfg)
    assert "Stealth+6" in h                                    # DEX+2 rank3 mantle+1


# ------------------------------ replay purity ---------------------------------------
def test_deterministic_replay_reproduces_items_and_tier():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1,
                [{"op": "item_mint", "template": "grey_mantle", "owner": "Kael"},
                 {"op": "item_equip", "instance": "grey_mantle#1", "slot": "cape"}],
                "user", cfg)
    doc = {"messages": [{"role": "user", "content": "((aether.check stealth vs 9))"}]}
    t0 = tier0.run(doc, "new_turn", False, current_state(store, bid), cfg, random.Random(11))
    apply_delta(store, sid, bid, 4, t0.rule_ops, "rule", cfg)
    live = current_state(store, bid)
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay["items"] == live["items"]                    # journal -> identical item plane
    assert replay["gear"] == live["gear"] and replay["inventory"] == live["inventory"]
    assert replay["rolls"][-1] == live["rolls"][-1]            # gear-modified tier reproduced
    assert replay["rolls"][-1]["mod"] == 6                     # DEX+2 rank3 mantle+1, baked


# ------------------------------ one_instance_one_place -------------------------------
def test_one_instance_one_place_detects_and_heals():
    cfg = _rpg_cfg()
    st = _geared_state(cfg)
    # split-brain: same instance also listed loose while loc says gear:head
    st["inventory"]["kael"]["loose"].append("iron_helm#1")
    # dangling pointer: gear slot referencing an unknown instance
    st["gear"]["kael"]["feet"] = "phantom_boots#1"
    v = linter.run(st, "", cfg, turn=5)
    rules = [x.rule for x in v]
    assert rules.count("one_instance_one_place") == 2 and all(x.advisory for x in v)
    # store-wins self-heal: index rebuilt from loc (the authority); dangling pointer dropped
    assert "iron_helm#1" not in st["inventory"]["kael"]["loose"]
    assert st["gear"]["kael"]["head"] == "iron_helm#1"
    assert "feet" not in st["gear"]["kael"]
    assert not [x for x in linter.run(st, "", cfg, turn=5) if x.rule == "one_instance_one_place"]


def test_one_instance_one_place_inert_without_rpg_or_items():
    st = _geared_state(_rpg_cfg())
    assert not linter.run(st, "", Config(), turn=5)            # none cfg: rule never runs
    cfg = _rpg_cfg()
    clean = empty_state()
    clean["player"] = {"kael": {"eid": "kael"}}
    assert not [x for x in linter.run(clean, "", cfg, turn=5)
                if x.rule == "one_instance_one_place"]         # no items: gated off


# ------------------------------ extraction wire (rpg-gated) --------------------------
def test_extraction_wire_gated_by_specialization():
    base = delta_json_schema()
    props = base["schema"]["properties"]["ops"]["items"]["properties"]
    assert "instance" not in props                             # none: byte-identical wire
    assert props["op"]["enum"] == EXTRACTION_OPS
    rpg = delta_json_schema(True)
    rprops = rpg["schema"]["properties"]["ops"]["items"]["properties"]
    assert "instance" in rprops and "item_move" in rprops["op"]["enum"]
    assert rpg["name"] != base["name"]                         # distinct compiler cache entries
    kinds_base = {b["properties"]["op"]["enum"][0]
                  for b in delta_json_schema_anyof()["schema"]["properties"]["ops"]["items"]["anyOf"]}
    kinds_rpg = {b["properties"]["op"]["enum"][0]
                 for b in delta_json_schema_anyof(True)["schema"]["properties"]["ops"]["items"]["anyOf"]}
    assert "item_equip" not in kinds_base and "item_equip" in kinds_rpg
    assert "item_mint" not in kinds_rpg                        # privileged: never on the wire
    assert "RPG ITEM OPS" not in system_prompt(2) and "RPG ITEM OPS" in system_prompt(2, rpg=True)
    assert scrub_op({"op": "item_equip", "instance": "i", "slot": "head",
                     "valence": 3})["op"] == "item_equip"      # foreign fields scrubbed
    assert "valence" not in scrub_op({"op": "item_equip", "instance": "i", "slot": "head",
                                      "valence": 3})


# ------------------------------ live proxy e2e ---------------------------------------
SENT = "<<AETHER:v=1;session={s};turn={t};type=normal;speaker=Dungeon Master;user=Bean>>"


def _payload(session, turn):
    return {"model": "m", "messages": [
        {"role": "system", "content": SENT.format(s=session, t=turn) + " A cold keep at dusk."},
        {"role": "user", "content": "I check my pack."}]}


async def test_none_session_no_item_leak_e2e(client, mock_upstream, cfg):
    """A `none` session forwards byte-identically: no [GEAR]/[INVENTORY], no item vocabulary."""
    assert cfg.specialization.name == "none"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg2-none", 1))
    body = mock_upstream.requests[0].body
    assert b"[GEAR]" not in body and b"[INVENTORY]" not in body and b"item_" not in body


async def test_rpg_gear_renders_e2e(client, mock_upstream, cfg):
    """Flagship RPG-2 exit: mint+equip via the control API, then the next forwarded request
    carries [GEAR] with the item's baked mods."""
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg2", 1))
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    r = await client.patch(f"/aether/session/{sid}/state", json={"ops": [
        {"op": "item_mint", "template": "iron_helm", "owner": "Bean"}]})
    assert r.json()["applied"] == 1
    r = await client.patch(f"/aether/session/{sid}/state", json={"ops": [
        {"op": "item_equip", "instance": "iron_helm#1", "slot": "head"}]})
    assert r.json()["applied"] == 1
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg2", 2))
    fwd = next(r.body for r in reversed(mock_upstream.requests)     # the chat forward, not a
               if b"I check my pack." in r.body)                    # background probe request
    assert b"[GEAR]" in fwd and b"Iron Helm" in fwd
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert now["state"]["items"]["iron_helm#1"]["loc"] == "gear:head"


def test_item_no_double_no_embedded_count_and_consume_removes():
    """RPG-2 fixes (2026-07-09): a same-turn double item_gain (tag + extraction) does NOT 2x;
    a count baked in the name rides qty instead; item_lose honors qty and removes when used up."""
    from aetherstate.state import empty_state, reduce_state, _split_item_qty
    assert _split_item_qty("Verdan Sap Vial (30 doses)") == ("Verdan Sap Vial", 30)
    assert _split_item_qty("Health Potion x3") == ("Health Potion", 3)
    assert _split_item_qty("Vael Morath Map (parchment)") == ("Vael Morath Map (parchment)", None)  # descriptor, not a count
    st = empty_state()
    # same turn, emitted twice -> ONE row, qty 30 (from the name), never 60
    reduce_state(st, [{"op": "item_gain", "char": "p", "name": "Verdan Sap Vial (30 doses)", "_turn": 5}])
    reduce_state(st, [{"op": "item_gain", "char": "p", "name": "Verdan Sap Vial (30 doses)", "_turn": 5}])
    rows = [it for it in st["items"].values() if it["name"] == "Verdan Sap Vial"]
    assert len(rows) == 1 and rows[0]["qty"] == 30
    # a LATER turn genuinely stacks (+5 -> 35)
    reduce_state(st, [{"op": "item_gain", "char": "p", "name": "Verdan Sap Vial", "qty": 5, "_turn": 6}])
    v = [it for it in st["items"].values() if it["name"] == "Verdan Sap Vial"][0]
    assert v["qty"] == 35
    # consume 5 -> 30, still present
    reduce_state(st, [{"op": "item_lose", "char": "p", "name": "Verdan Sap Vial", "qty": 5, "_turn": 7}])
    v = [it for it in st["items"].values() if it["name"] == "Verdan Sap Vial"][0]
    assert v["qty"] == 30 and v["loc"] != "gone"
    # use up the rest -> removed from the ledger
    reduce_state(st, [{"op": "item_lose", "char": "p", "name": "Verdan Sap Vial", "qty": 999, "_turn": 8}])
    v = [it for it in st["items"].values() if it["name"] == "Verdan Sap Vial"][0]
    assert v["loc"] == "gone"
