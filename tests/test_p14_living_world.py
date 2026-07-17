"""Phase 2 — the living world (plan doc 13, ratified 2026-07-09; pillar 12 operational).

Covers: authored faction fronts (front_add seed-once, clamped bakes), code-only
advancement (world_ops: day pace, faction touches, quest/combat echoes; one tick per
front per batch), FILL -> world_flag + world-event memory + the fresh [FRONT] tail,
rumor-gated visibility (front_reveal via [rumor] tag + the name-mention floor; HUD
revealed-only, state_summary raw), travel time (route_set edges, the 1-segment default,
[TRAVEL] with a deterministic md5 cue), the idle clock floor, the DM's clamped [time]
ceiling — and, per the RPG invariants, `none`-leak guards and deterministic replay.
"""
from __future__ import annotations

from aetherstate import creator, tier0
from aetherstate.compose import _render_living_tail, render_header
from aetherstate.config import Config
from aetherstate.hud import hud_view
from aetherstate.state import (_enrich, apply_delta, authority_violation,
                               current_state, empty_state, state_summary, travel_cost,
                               validate_op, world_ops)
from aetherstate.store import Store


def _rpg_cfg():
    c = Config()
    c.specialization.name = "rpg"
    return c


def _seeded(cfg=None):
    cfg = cfg or _rpg_cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="p14")
    identity = apply_delta(
        store,
        sid,
        bid,
        0,
        [{"op": "world_identity_set", "world_id": creator.mint_world_id()}],
        "user",
        cfg,
    )
    assert identity.applied and not identity.quarantined
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "entity_add", "name": "Iron Pact", "kind": "faction"},
        {"op": "entity_add", "name": "The Docks", "kind": "location"},
        {"op": "entity_add", "name": "Old Gate", "kind": "location"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"DEX": 14}, "skills": {"stealth": 2},
                  "resources": {"hp": {"max": 20}, "stamina": {"max": 10}}}},
        {"op": "front_add", "name": "The Iron Pact Rearms", "faction": "Iron Pact",
         "segments": 4, "pace": 1, "consequence": "The Pact marches on the Docks."},
        {"op": "route_set", "a": "The Docks", "b": "Old Gate", "segments": 2}],
        "genesis", cfg)
    return store, sid, bid


# ------------------------------- ops + reducer -------------------------------------
def test_front_add_is_seed_once_and_clamped():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    st = current_state(store, bid)
    f = st["fronts"]["the_iron_pact_rearms"]
    assert f["segments"] == 4 and f["pace"] == 1 and not f["revealed"] and not f["done"]
    assert st["routes"]["old_gate|the_docks"] == 2
    # re-seed with progress on the clock: nothing resets
    apply_delta(store, sid, bid, 1, [
        {"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "test"}], "rule", cfg)
    apply_delta(store, sid, bid, 2, [
        {"op": "front_add", "name": "The Iron Pact Rearms", "segments": 9,
         "consequence": "x"}], "user", cfg)
    f = current_state(store, bid)["fronts"]["the_iron_pact_rearms"]
    assert f["filled"] == 1 and f["segments"] == 4          # seed-once held
    # clamps: segments 3..12, pace 1..3
    op = _enrich({"op": "front_add", "name": "Z", "segments": 99, "pace": 9}, 0, cfg, {})
    assert op["_segments"] == 12 and op["_pace"] == 3


def test_front_fills_reveals_and_replays():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    for t in (1, 2, 3, 4):
        trigger = apply_delta(
            store,
            sid,
            bid,
            t,
            [{"op": "affinity_adj", "target": "Iron Pact", "delta": -1}],
            "rule",
            cfg,
        )
        generated = world_ops(
            trigger.state,
            trigger.applied,
            clock_turns=0,
            session_id=sid,
            branch_id=bid,
            turn_index=t,
        )
        result = apply_delta(store, sid, bid, t, generated, "rule", cfg)
        assert result.applied and not result.quarantined
    s1 = current_state(store, bid)
    f = s1["fronts"]["the_iron_pact_rearms"]
    assert f["done"] and f["revealed"] and f["filled"] == 4 and f["filled_turn"] == 4
    assert len(f["log"]) == 4
    # a tick on a done front no-ops; replay is byte-stable
    apply_delta(store, sid, bid, 5, [
        {"op": "front_tick", "front": "the_iron_pact_rearms", "reason": "over"}], "rule", cfg)
    assert current_state(store, bid)["fronts"]["the_iron_pact_rearms"]["filled"] == 4
    assert current_state(store, bid) == current_state(store, bid)


def test_front_reveal_resolves_display_name():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [
        {"op": "front_reveal", "front": "The Iron Pact Rearms"}], "extraction", cfg)
    assert current_state(store, bid)["fronts"]["the_iron_pact_rearms"]["revealed"]


def test_authority_fronts_are_engine_owned():
    cfg = _rpg_cfg()
    st = empty_state()
    for op in ({"op": "front_add", "name": "X", "segments": 4, "consequence": "c"},
               {"op": "front_tick", "front": "x"},
               {"op": "route_set", "a": "a", "b": "b", "segments": 1}):
        assert authority_violation(op, "extraction", st, cfg)
        assert authority_violation(op, "rule", st, cfg) is None
    assert authority_violation({"op": "front_reveal", "front": "x"},
                               "extraction", st, cfg) is None
    assert validate_op({"op": "front_add", "name": "", "segments": 4,
                        "consequence": ""}) is None


# ------------------------------- the referee ---------------------------------------
def _base(turn=3, fronts=None, tod="evening", day=1, last_adv=None):
    st = {"player": {"kael": {}}, "meta": {"turn": turn},
          "clock": {"day": day, "time_of_day": tod}, "entities": {}, "scene": {}}
    if last_adv is not None:
        st["clock"]["last_advance_turn"] = last_adv
    if fronts:
        st["fronts"] = fronts
    return st


def test_travel_costs_time_and_routes_override():
    st = _base(last_adv=3)
    st["routes"] = {"a|b": 2}
    mv = {"op": "scene_set", "location": "b", "_prev_loc": "a", "_canon": 1}
    ops = world_ops(st, [mv])
    assert ops == [{"op": "time_advance", "to_time_of_day": "late_night"}]  # evening +2
    assert travel_cost(st, "a", "b") == 2 and travel_cost(st, "a", "zzz") == 1
    # an explicit time move in the same batch wins over the travel charge
    ops = world_ops(st, [mv, {"op": "time_advance", "to_time_of_day": "night"}])
    assert not [o for o in ops if o["op"] == "time_advance"]


def test_idle_clock_floor_and_knob_off():
    st = _base(turn=9, last_adv=3)
    assert world_ops(st, [], clock_turns=6) == [
        {"op": "time_advance", "to_time_of_day": "night"}]
    assert world_ops(st, [], clock_turns=0) == []          # 0 disables the floor
    assert world_ops(_base(turn=5, last_adv=3), []) == []  # not yet


def _front(filled=0, segments=4, pace=1, revealed=False, done=False):
    return {"name": "The Iron Pact Rearms", "faction": "iron_pact", "segments": segments,
            "filled": filled, "pace": pace, "consequence": "The Pact marches.",
            "revealed": revealed, "done": done, "created_turn": 0, "log": []}


def test_fronts_tick_on_day_pace_and_faction_touch():
    # a day wrap paces the front
    st = _base(turn=4, tod="late_night", fronts={"f": _front()}, last_adv=4)
    ops = world_ops(st, [{"op": "time_advance", "to_time_of_day": "dawn", "_day_wrap": 1}])
    assert {"op": "front_tick", "front": "f",
            "reason": "day 2: the agenda advances on its own"} in ops
    # the player touching the faction ticks it ONCE (dedupe inside a batch)
    st = _base(turn=4, fronts={"f": _front()}, last_adv=4)
    ops = world_ops(st, [
        {"op": "affinity_adj", "target": "Iron Pact", "delta": -5},
        {"op": "world_flag", "key": "curfew", "value": "yes", "faction": "iron_pact"}])
    assert len([o for o in ops if o["op"] == "front_tick"]) == 1
    # a completed quest sharing a name token ticks it
    st = _base(turn=4, fronts={"f": _front()}, last_adv=4)
    ops = world_ops(st, [{"op": "quest_update", "quest": "break_the_iron_pact",
                          "status": "complete"}])
    assert any(o["op"] == "front_tick" for o in ops)


def test_fill_commits_world_event():
    st = _base(turn=6, fronts={"f": _front(filled=3)}, last_adv=6)
    ops = world_ops(st, [{"op": "affinity_adj", "target": "Iron Pact", "delta": -5}])
    kinds = [o["op"] for o in ops]
    assert kinds.count("front_tick") == 1
    assert {"op": "world_flag", "key": "f", "value": "come to a head",
            "faction": "iron_pact"} in ops
    assert any(o["op"] == "memory_event" and "The Pact marches." in o["text"] for o in ops)


# ------------------------------- tags (world-tags/5) --------------------------------
def _tag_state(**kw):
    st = empty_state()
    st["player"] = {"kael": {}}
    st.update(kw)
    return st


def test_time_tag_clamps_and_skips_noop():
    st = _tag_state()                                      # clock: evening
    assert tier0._parse_world_tags("[time | night]", st) == [
        {"op": "time_advance", "to_time_of_day": "night"}]
    assert tier0._parse_world_tags("[time | +5]", st) == [
        {"op": "time_advance", "to_time_of_day": "late_night"}]   # +N capped at 2
    assert tier0._parse_world_tags("[time | evening]", st) == []  # restating = no move
    assert tier0._parse_world_tags("[time | next day]", st) == [
        {"op": "time_advance", "to_time_of_day": "dawn"}]


def test_rumor_tag_and_name_mention_reveal():
    st = _tag_state(fronts={"f": _front()})
    ops = tier0._parse_world_tags("[rumor | The Iron Pact Rearms | they buy steel]", st)
    assert {"op": "front_reveal", "front": "The Iron Pact Rearms"} in ops
    assert any(o["op"] == "memory_event" and "they buy steel" in o["text"] for o in ops)
    ops = tier0._parse_world_tags(
        "Dock talk says the Iron Pact rearms in the night.", st)
    assert {"op": "front_reveal", "front": "f"} in ops     # the name-mention floor
    st["fronts"]["f"]["revealed"] = True
    assert tier0._parse_world_tags("the Iron Pact rearms again", st) == []


# ------------------------------- render + visibility --------------------------------
def test_tail_renders_travel_and_fronts_deterministically():
    cfg = _rpg_cfg()
    st = _tag_state(fronts={"f": _front(filled=2, revealed=True)})
    st["meta"] = {"turn": 5}
    st["scene"] = {"last_move": {"from": "the_docks", "to": "old_gate", "turn": 5}}
    st["entities"] = {"the_docks": {"name": "The Docks"}, "old_gate": {"name": "Old Gate"}}
    a, b = _render_living_tail(st, cfg), _render_living_tail(st, cfg)
    assert a == b                                          # md5 cue: same state, same bytes
    assert "[TRAVEL] The Docks → Old Gate" in a and "[FRONTS]" in a and "2/4" in a
    st["fronts"]["f"].update(done=True, filled=4, filled_turn=5)
    fresh = _render_living_tail(st, cfg)
    assert "HAS COME TO A HEAD" in fresh and "The Pact marches." in fresh


def test_rumor_gate_hides_fronts_from_hud_and_briefing_not_console():
    cfg = _rpg_cfg()
    st = _tag_state(fronts={"f": _front()})
    st["meta"] = {"turn": 3}
    assert _render_living_tail(st, cfg) == ""              # hidden: no briefing line
    assert hud_view(st, cfg)["fronts"] == []               # hidden: no HUD row
    assert "f" in state_summary(st)["fronts"]              # Console: raw from turn one
    st["fronts"]["f"]["revealed"] = True
    assert hud_view(st, cfg)["fronts"][0]["name"] == "The Iron Pact Rearms"
    assert hud_view(st, cfg)["fronts"][0]["consequence"] == ""   # no spoilers until fill


# ------------------------------- none-leak + replay ---------------------------------
def test_none_session_stays_byte_identical():
    cfg = Config()                                         # specialization: none
    st = empty_state()
    op = _enrich({"op": "scene_set", "location": "b"}, 1, cfg,
                 {"scene": {"location_id": "a"}})
    assert "_prev_loc" not in op and "_canon" not in op
    op = _enrich({"op": "time_advance", "to_time_of_day": "night"}, 1, cfg,
                 {"clock": {"time_of_day": "evening"}})
    assert "_turn_mark" not in op and "_day_wrap" not in op
    st["player"] = {}                                      # no player: referee inert
    assert world_ops(st, [{"op": "scene_set", "location": "x", "_prev_loc": "y"}]) == []
    assert _render_living_tail(st, cfg) == ""
    hdr = render_header(st, cfg)
    assert "[TRAVEL]" not in hdr and "[FRONTS]" not in hdr and "[FRONT]" not in hdr


def test_p14_journal_replays_deterministically():
    cfg = _rpg_cfg()
    store, sid, bid = _seeded(cfg)
    apply_delta(store, sid, bid, 1, [{"op": "scene_set", "location": "The Docks"}],
                "user", cfg)
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "Old Gate"}],
                "user", cfg)
    st = current_state(store, bid)
    assert st["scene"]["last_move"]["from"] == "the_docks"
    lw = world_ops(st, [{"op": "scene_set", "location": "old_gate",
                         "_prev_loc": "the_docks"}])
    apply_delta(store, sid, bid, 2, lw, "rule", cfg)
    s1 = current_state(store, bid)
    assert s1["clock"]["time_of_day"] == "late_night"      # evening + the 2-segment route
    assert s1["clock"]["last_advance_turn"] == 2
    assert current_state(store, bid) == s1                 # fresh replay of the journal


def test_scene_follows_player_move_entity_floor():
    """2026-07-09 Cinderveil live repro: a DM that never emits [scene] tags still yields
    extraction move_entity on the PLAYER — the camera (and the travel toll) must follow
    deterministically. Unknown destinations never mint; NPC moves never drag the scene."""
    st = _base(last_adv=3)
    st["player"] = {"rho": {}}
    st["entities"] = {
        "rho": {"kind": "player", "name": "Rho", "aliases": []},
        "ashen_maw": {"kind": "location", "name": "Ashen Maw", "aliases": []},
        "npc1": {"kind": "npc", "name": "Vex", "aliases": []}}
    st["scene"] = {"location_id": "sernas_relay"}   # the reducer's real key
    ops = world_ops(st, [{"op": "move_entity", "entity": "rho", "to_location": "Ashen Maw"}])
    assert {"op": "scene_set", "location": "ashen_maw"} in ops
    assert [o for o in ops if o["op"] == "time_advance"]   # the move pays the travel toll
    # an unknown destination never mints a location or moves the camera
    ops2 = world_ops(st, [{"op": "move_entity", "entity": "rho",
                           "to_location": "Nowhere Keep"}])
    assert not [o for o in ops2 if o["op"] in ("scene_set", "time_advance")]
    # a non-player mover doesn't drag the scene along
    ops3 = world_ops(st, [{"op": "move_entity", "entity": "npc1",
                           "to_location": "Ashen Maw"}])
    assert not [o for o in ops3 if o["op"] == "scene_set"]
    # already there: no move, no toll
    st["scene"]["location_id"] = "ashen_maw"
    ops4 = world_ops(st, [{"op": "move_entity", "entity": "rho", "to_location": "Ashen Maw"}])
    assert not [o for o in ops4 if o["op"] in ("scene_set", "time_advance")]


def test_canonical_location_splits_middle_dot_sublocation():
    """GLM-5.2 writes sub-locations as 'Name · sub quarter' — the head must split on the
    middle dot or the whole string mints a twin location (2026-07-09 Cinderveil live)."""
    from aetherstate.state import canonical_location
    st = {"entities": {"vael_thyrr_ruins": {"kind": "location",
                                            "name": "Vael Thyrr (Ruins at the Caldera Floor)",
                                            "aliases": []}}}
    loc, _name, is_new = canonical_location(st, "Vael Thyrr · temple quarter")
    assert loc == "vael_thyrr_ruins" and not is_new


def test_canonical_location_parent_rung_resolves_subarea():
    """'<known place> <sub-area specifier>' resolves to the known place (unique name-head
    containment, ≥2 head tokens) — 'Ashen Maw rim' and 'Vael Thyrr archive hall' were
    minting twin locations live (2026-07-09)."""
    from aetherstate.state import canonical_location
    st = {"entities": {
        "the_ashen_maw": {"kind": "location", "name": "The Ashen Maw", "aliases": []},
        "vael_thyrr_ruins": {"kind": "location",
                             "name": "Vael Thyrr (Ruins at the Caldera Floor)", "aliases": []},
        "midreach": {"kind": "location", "name": "Midreach", "aliases": []}}}
    assert canonical_location(st, "Ashen Maw rim")[0] == "the_ashen_maw"
    assert canonical_location(st, "vael_thyrr_archive_hall")[0] == "vael_thyrr_ruins"
    # a one-word head never swallows ("Midreach docks" has no ≥2-token parent) — stays new
    assert canonical_location(st, "Midreach docks")[2] is True
    # unrelated prose still mints nothing false
    assert canonical_location(st, "a collapsed stairwell")[2] is True


def test_hp_adj_same_turn_near_dupe_counts_once():
    """The DM's [hp] tag AND the extraction ladder both report the same wound with slightly
    different reason text — it must land ONCE (2026-07-09 Cinderveil live: -2 became -4).
    A same-turn DIFFERENT wound (another foe) still applies."""
    from aetherstate.state import _apply_op
    st = {"player": {"rho": {"hp": {"cur": 20, "max": 20}}}, "meta": {"turn": 5},
          "entities": {}, "attributes": {}}
    _apply_op(st, {"op": "hp_adj", "char": "rho", "delta": -2,
                   "reason": "hook-pole strike, ribs below compression plate", "_turn": 5})
    assert st["player"]["rho"]["hp"]["cur"] == 18
    _apply_op(st, {"op": "hp_adj", "char": "rho", "delta": -2,
                   "reason": "hook-pole strike to ribs below compression plate", "_turn": 5})
    assert st["player"]["rho"]["hp"]["cur"] == 18          # the near-dupe counted once
    _apply_op(st, {"op": "hp_adj", "char": "rho", "delta": -2,
                   "reason": "second warden's counter-swing across the back", "_turn": 5})
    assert st["player"]["rho"]["hp"]["cur"] == 16          # a different wound still lands
    _apply_op(st, {"op": "hp_adj", "char": "rho", "delta": -2,
                   "reason": "hook-pole strike, ribs below compression plate", "_turn": 6})
    assert st["player"]["rho"]["hp"]["cur"] == 14          # a later turn re-applies fine


def test_hp_adj_paraphrased_reason_counts_once():
    """The cold-path ladder can PARAPHRASE the [hp] tag's reason enough to drop Jaccard below
    the near-dupe gate (2026-07-10 Hollowmere live: "wight's pike-butt grazes his ribs below
    the plate" vs "pike-butt graze below breastplate" -> J=0.30, so -1 landed twice -> -2).
    Containment catches the compressed re-report; a distinct same-magnitude wound still stacks."""
    from aetherstate.state import _apply_op
    st = {"player": {"ald": {"hp": {"cur": 24, "max": 24}}}, "meta": {"turn": 1},
          "entities": {}, "attributes": {}}
    _apply_op(st, {"op": "hp_adj", "char": "ald", "delta": -1,
                   "reason": "wight's pike-butt grazes his ribs below the plate", "_turn": 1})
    assert st["player"]["ald"]["hp"]["cur"] == 23
    _apply_op(st, {"op": "hp_adj", "char": "ald", "delta": -1,
                   "reason": "pike-butt graze below breastplate", "_turn": 1})
    assert st["player"]["ald"]["hp"]["cur"] == 23          # paraphrased re-report counts once
    _apply_op(st, {"op": "hp_adj", "char": "ald", "delta": -1,
                   "reason": "second wight's claw rakes his sword-arm", "_turn": 1})
    assert st["player"]["ald"]["hp"]["cur"] == 22          # a distinct wound still applies
