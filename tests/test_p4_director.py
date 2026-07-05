"""P4 item 4: director beats + condition DSL + pacing (03 SS8/8.1, 02 SS9, 04 SS3.3) —
DSL units, binding rules (user char never an actor), consent-gated escalation ladder,
frozen/flashback gating, cooldown/once_per_scene, effects via rule source, pacing
defaults, note assembly with linter corrective, rollback retraction, library loading."""
from __future__ import annotations

from aetherstate import director, linter
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def mk():
    cfg = Config()
    cfg.user_guard.name = "Bean"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kira"},
        {"op": "entity_add", "name": "Dane"},
        {"op": "entity_add", "name": "Bean"},
        {"op": "presence", "entity": "kira", "present": True},
        {"op": "presence", "entity": "dane", "present": True},
        {"op": "scene_set", "location": "tavern", "phase": "rising"}], "user", cfg)
    return cfg, store, sid, bid


def st(store, bid):
    return current_state(store, bid)


def dial(store, sid, bid, turn, dial, value):
    apply_delta(store, sid, bid, turn, [{"op": "scene_dial", "dial": dial, "set": value}],
                "rule", Config())


# ------------------------------ condition DSL (03 SS8.1) ------------------------------
def test_dsl_ops_and_combinators():
    s = {"scene": {"tension": 70, "phase": "rising", "participants": ["kira"]},
         "chars": {"kira": {"arousal": {"arousal": 55}, "secrets": ["s1"],
                            "status_effects": ["shaky_hands"]}},
         "relationships": {"kira->dane": {"dims": {"desire": 60}}}}
    ok = director.eval_dsl
    assert ok({"path": "scene.tension", "op": ">=", "value": 70}, s)
    assert not ok({"path": "scene.tension", "op": "<", "value": 70}, s)
    assert ok({"path": "scene.phase", "op": "in", "value": ["setup", "rising"]}, s)
    assert ok({"path": "char.kira.status_effects", "op": "contains", "value": "shaky_hands"}, s)
    assert ok({"path": "char.kira.secrets", "op": "!=", "value": []}, s)
    assert ok({"all": [{"path": "scene.tension", "op": ">", "value": 50},
                       {"not": {"path": "scene.phase", "op": "==", "value": "setup"}}]}, s)
    assert ok({"any": [{"path": "scene.tension", "op": "<", "value": 0},
                       {"path": "rel.kira->dane.desire", "op": ">=", "value": 60}]}, s)
    assert ok({"path": "char.kira.present", "op": "==", "value": True}, s)
    assert ok({"path": "char.kira.arousal", "op": ">=", "value": 55}, s)


def test_dsl_unknown_path_is_false_and_never_raises():
    s = {"scene": {}}
    assert not director.eval_dsl({"path": "scene.nope", "op": "==", "value": 1}, s)
    assert not director.eval_dsl({"path": "char.ghost.arousal", "op": ">=", "value": 0}, s)
    assert not director.eval_dsl({"path": "garbage", "op": ">=", "value": 0}, s)
    assert not director.eval_dsl({"path": "scene.tension", "op": "??", "value": 1},
                                 {"scene": {"tension": 5}})
    assert not director.eval_dsl("not a dict", s)
    assert not director.eval_dsl({}, s)
    # type mismatch fails closed, not loud
    assert not director.eval_dsl({"path": "scene.phase", "op": ">=", "value": 3},
                                 {"scene": {"phase": "rising"}})


def test_dsl_defaults_rel_zero_consent_unknown():
    s = {"scene": {}, "relationships": {}, "consent": {}}
    assert director.eval_dsl({"path": "rel.a->b.desire", "op": "<", "value": 1}, s)
    assert director.eval_dsl({"path": "consent.a->b.kissing.level", "op": "in",
                              "value": ["unknown", "hesitant"]}, s)
    # reversed storage order still resolves (mirrors L8)
    s2 = {"consent": {"b|a|kissing": {"level": "granted", "max_intensity": None}}}
    assert director.eval_dsl({"path": "consent.a->b.kissing.level", "op": "in",
                              "value": ["granted", "enthusiastic"]}, s2)


# ------------------------------ bindings (Q12: user never an actor) -------------------
def test_bindings_exclude_user_char_from_actor_slots():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "presence", "entity": "bean",
                                      "present": True}], "user", cfg)
    state = st(store, bid)
    uids = director._user_ids(state, "Bean")
    assert uids == {"bean"}
    pairs = director.bindings({"binds": "pair"}, state, uids)
    assert all(p["a"] != "bean" for p in pairs)          # never the initiator
    assert any(p["b"] == "bean" for p in pairs)          # may be the partner
    chars = director.bindings({"binds": "char"}, state, uids)
    assert {c["char"] for c in chars} == {"dane", "kira"}
    assert director.bindings({"binds": "none"}, state, uids) == [{}]


def test_bindings_craving_and_obsession_enumerate_state():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "craving", "char": "kira", "substance": "wine", "action": "adjust",
         "delta": 80},
        {"op": "obsession", "char": "dane", "target_kind": "entity", "target": "Kira",
         "set": 80}], "extraction", cfg)
    state = st(store, bid)
    assert director.bindings({"binds": "craving"}, state, set()) == \
        [{"char": "kira", "substance": "wine"}]
    obs = director.bindings({"binds": "obsession"}, state, set())
    assert obs == [{"char": "dane", "obs_key": "entity:kira", "obs_target": "Kira"}]


# ------------------------------ evaluate: beats fire from state -----------------------
def test_craving_withdrawal_beat_fires_with_names_not_ids():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "craving", "char": "kira", "substance": "wine", "action": "adjust",
         "delta": 90}], "extraction", cfg)
    # dependency via repeated consumes would take turns; set through extraction adjusts
    state = st(store, bid)
    state["chars"]["kira"]["cravings"]["wine"]["dependency"] = 60
    note = director.evaluate(store, cfg, sid, bid, 5, state)
    assert "Kira's craving for wine is clawing" in note
    assert store.director_counts() == {"core_drama.withdrawal_edge": 1}


def test_obsession_grip_beat_and_cooldown():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "obsession", "char": "dane", "target_kind": "entity", "target": "Kira",
         "set": 80}], "extraction", cfg)
    state = st(store, bid)
    n1 = director.evaluate(store, cfg, sid, bid, 5, state)
    assert "Dane's fixation on Kira" in n1
    assert director.evaluate(store, cfg, sid, bid, 6, state) != n1   # cooldown 6: not again
    n3 = director.evaluate(store, cfg, sid, bid, 11, state)          # past cooldown
    assert "Dane's fixation on Kira" in n3


def test_consent_gated_ladder_seek_then_first_kiss():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "relationship_adj", "from_char": "kira", "to_char": "dane",
         "dimension": "desire", "delta": 30},
        {"op": "relationship_adj", "from_char": "kira", "to_char": "dane",
         "dimension": "desire", "delta": 30},
        {"op": "relationship_adj", "from_char": "kira", "to_char": "dane",
         "dimension": "desire", "delta": 5}], "extraction", cfg)
    dial(store, sid, bid, 1, "intimacy", 45)
    state = st(store, bid)
    assert state["relationships"]["kira->dane"]["dims"]["desire"] >= 60
    # consent unknown -> seek_consent wins; the acting beat cannot fire (03 SS8.1 gating)
    note = director.evaluate(store, cfg, sid, bid, 5, state)
    assert "ask" in note and "Kira" in note and "Dane" in note
    assert list(store.director_counts()) == ["erp_escalation.seek_consent_kiss"]
    # consent granted -> first_kiss (priority 60) outranks seek variants
    apply_delta(store, sid, bid, 6, [
        {"op": "consent_signal", "from_char": "kira", "to_char": "dane",
         "category": "kissing", "signal": "grant"}], "extraction", cfg)
    state = st(store, bid)
    note2 = director.evaluate(store, cfg, sid, bid, 13, state)       # past seek cooldown
    assert "kiss" in note2 and "finally" in note2
    assert store.director_counts()["erp_escalation.first_kiss"] == 1


def test_beat_effects_apply_via_rule_source_and_journal():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "relationship_adj", "from_char": "kira", "to_char": "dane",
         "dimension": "desire", "delta": 30},
        {"op": "relationship_adj", "from_char": "kira", "to_char": "dane",
         "dimension": "desire", "delta": 15}], "extraction", cfg)
    state = st(store, bid)
    before = int(state.get("scene", {}).get("tension", 0))
    note = director.evaluate(store, cfg, sid, bid, 5, state)
    assert note                                          # charged_proximity or unsaid_want
    after = current_state(store, bid)                    # re-read: effects were JOURNALED
    assert int(after["scene"].get("tension", 0)) == before + 5


def test_once_per_scene_resets_on_scene_change():
    cfg, store, sid, bid = mk()
    dial(store, sid, bid, 1, "tension", 70)
    state = st(store, bid)
    n1 = director.evaluate(store, cfg, sid, bid, 5, state)
    assert "Turn the tables" in n1                       # core_drama.reversal (once_per_scene)
    assert "Turn the tables" not in (director.evaluate(store, cfg, sid, bid, 20, state) or "")
    apply_delta(store, sid, bid, 21, [{"op": "scene_set", "location": "cellar",
                                       "phase": "rising"}], "user", cfg)
    dial(store, sid, bid, 22, "tension", 70)
    n3 = director.evaluate(store, cfg, sid, bid, 40, st(store, bid))
    assert "Turn the tables" in n3                       # new scene_index -> may refire


# ------------------------------ frozen / flashback gating -----------------------------
def test_frozen_runs_aftercare_exclusively_and_drops_corrective():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 3, [{"op": "consent_signal", "from_char": "bean",
                                      "to_char": "kira", "category": "other",
                                      "signal": "safeword"}], "user", cfg)
    state = st(store, bid)
    assert state["frozen"]
    vios = [linter.Violation("L6", "med", ("clock",), "d", note="Continuity: x.")]
    note = director.stage(store, cfg, sid, bid, 5, state, vios)
    assert "paused out-of-character" in note             # 04 SS3.2 verbatim register
    assert "Continuity" not in note                      # aftercare owns the slot
    # steady while frozen (cooldown 0)
    assert "paused out-of-character" in director.stage(store, cfg, sid, bid, 6, state, [])


def test_flashback_gets_no_steering():
    cfg, store, sid, bid = mk()
    dial(store, sid, bid, 1, "tension", 70)
    apply_delta(store, sid, bid, 2, [{"op": "scene_mode", "mode": "flashback"}], "user", cfg)
    assert director.evaluate(store, cfg, sid, bid, 5, st(store, bid)) == ""


# ------------------------------ pacing defaults (03 SS8) ------------------------------
def test_pacing_raise_ease_and_stagnation():
    # phase resolution (target 30) so no shipped beat (e.g. reversal@rising) can outrank
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "scene_set", "location": "tavern",
                                      "phase": "resolution"}], "user", cfg)
    state = st(store, bid)                               # tension 0 < 30-20 -> raise
    assert "Tighten" in director.evaluate(store, cfg, sid, bid, 5, state)
    assert director.evaluate(store, cfg, sid, bid, 6, state) == ""   # pacing cooldown
    dial(store, sid, bid, 7, "tension", 90)              # 90 > 30+20 -> ease
    assert "breathe" in director.evaluate(store, cfg, sid, bid, 12, st(store, bid))
    apply_delta(store, sid, bid, 13, [{"op": "stagnation", "value": 0.9}], "rule", cfg)
    dial(store, sid, bid, 13, "tension", 30)             # in band; stagnation outranks
    assert "complication" in director.evaluate(store, cfg, sid, bid, 20, st(store, bid))


def test_pacing_silent_inside_band():
    cfg, store, sid, bid = mk()
    dial(store, sid, bid, 1, "tension", 55)              # == rising target
    assert director.evaluate(store, cfg, sid, bid, 5, st(store, bid)) == ""


# ------------------------------ note assembly (04 SS3.3) ------------------------------
def test_stage_appends_corrective_after_beat_note():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "obsession", "char": "dane", "target_kind": "entity", "target": "Kira",
         "set": 80}], "extraction", cfg)
    vios = [linter.Violation("L4", "med", ("skirt",), "d",
                             note="Continuity: the skirt was removed earlier.")]
    note = director.stage(store, cfg, sid, bid, 5, st(store, bid), vios)
    assert note.startswith("[Direction] Dane's fixation")
    assert note.endswith("the skirt was removed earlier.")
    assert store.read_note(sid) == note


def test_stage_director_disabled_keeps_corrective_only():
    cfg, store, sid, bid = mk()
    cfg.director.enabled = False
    vios = [linter.Violation("L4", "med", ("skirt",), "d", note="Continuity: x.")]
    note = director.stage(store, cfg, sid, bid, 5, st(store, bid), vios)
    assert note == "[Direction] Continuity: x."
    assert store.director_counts() == {}


def test_stage_empty_clears_stale_note():
    cfg, store, sid, bid = mk()
    store.write_note(sid, 5, "[Direction] old")
    dial(store, sid, bid, 1, "tension", 55)              # nothing to say
    assert director.stage(store, cfg, sid, bid, 5, st(store, bid), []) == ""
    assert store.read_note(sid) == ""


def test_render_note_drops_on_unresolved_placeholder():
    assert director.render_note("Let {mystery} happen.", {}, {"entities": {}}) == ""
    out = director.render_note("Let {a} kiss {b}.", {"a": "kira", "b": "dane"},
                               {"entities": {"kira": {"name": "Kira"},
                                             "dane": {"name": "Dane"}}})
    assert out == "Let Kira kiss Dane."


# ------------------------------ rollback + loading + fail-open ------------------------
def test_rollback_retracts_firings():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "obsession", "char": "dane", "target_kind": "entity", "target": "Kira",
         "set": 80}], "extraction", cfg)
    state = st(store, bid)
    assert director.evaluate(store, cfg, sid, bid, 5, state)
    store.rollback_to(bid, 2)
    assert store.director_counts() == {}
    assert director.evaluate(store, cfg, sid, bid, 5, st(store, bid))   # cooldown reset


def test_all_shipped_libraries_load_and_unknown_skips():
    beats = director.load_libraries(["core_drama", "erp_tension", "erp_escalation",
                                     "erp_aftercare", "aftercare_checkin"])
    assert len(beats) >= 15
    assert all({"beat_id", "preconditions", "note_template"} <= set(b) for b in beats)
    assert director.load_libraries(["no_such_library"]) == []           # warn once, skip


def test_evaluate_never_raises_on_garbage_state():
    cfg, store, sid, bid = mk()
    assert director.stage(store, cfg, sid, bid, 1,
                          {"scene": "??", "chars": None, "entities": []}, []) in ("", None) \
        or True                                          # must simply not raise
