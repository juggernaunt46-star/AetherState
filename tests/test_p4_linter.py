"""P4 item 3: consistency linter L1-L9 (03 SS9, 08 L-rows/B4, Q12/Q13) — rule-by-rule
units on the pure pass, mode/scene gating, cooldown dedup, corrective-note staging +
compose injection, L9 guard escalation, impersonate skip, rollback retraction."""
from __future__ import annotations

from aetherstate import linter
from aetherstate import director
from aetherstate.compose import compose, render_guard
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def mk(consent_mode="strict"):
    cfg = Config()
    cfg.consent.mode = consent_mode
    cfg.user_guard.name = "Bean"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kira"},
        {"op": "entity_add", "name": "Dane"},
        {"op": "entity_add", "name": "Vexana", "aliases": ["the sorceress"]},
        {"op": "presence", "entity": "kira", "present": True},
        {"op": "presence", "entity": "dane", "present": True},
        {"op": "scene_set", "location": "tavern"}], "user", cfg)
    return cfg, store, sid, bid


def st(store, bid):
    return current_state(store, bid)


def rules_of(vios):
    return {v.rule for v in vios}


# ------------------------------ L1 co-location ---------------------------------------
def test_l1_contact_with_absent_party():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "contact", "action": "start", "type": "touching",
                                      "from_char": "kira", "to_char": "vexana"}], "user", cfg)
    vios = linter.run(st(store, bid), "prose", cfg)
    l1 = [v for v in vios if v.rule == "L1"]
    assert l1 and "vexana" in l1[0].subjects
    assert "not present" in l1[0].note


def test_l1_present_but_elsewhere():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "move_entity", "entity": "dane",
                                      "to_location": "cellar"}], "user", cfg)
    vios = linter.run(st(store, bid), "", cfg)
    assert any(v.rule == "L1" and "dane" in v.subjects for v in vios)


def test_l1_clean_scene_silent():
    cfg, store, sid, bid = mk()
    assert linter.run(st(store, bid), "Kira smiles at Dane.", cfg) == []


# ------------------------------ L2 exposure ------------------------------------------
def test_l2_bare_zone_while_covered():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "blouse",
                                      "action": "don", "covers": ["chest"]}], "user", cfg)
    vios = linter.run(st(store, bid), "His eyes trace her bare chest.", cfg)
    l2 = [v for v in vios if v.rule == "L2"]
    assert l2 and l2[0].evidence == "bare chest"


def test_l2_through_item_not_worn():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "blouse",
                                      "action": "don", "covers": ["chest"]},
                                     {"op": "clothing", "char": "kira", "item": "blouse",
                                      "action": "open"}], "user", cfg)
    vios = linter.run(st(store, bid), "Warmth radiates through her blouse.", cfg)
    assert any(v.rule == "L2" and "blouse" in v.subjects for v in vios)


def test_l2_ambiguous_two_chars_stays_silent():
    cfg, store, sid, bid = mk()   # Kira covered, Dane tracked-and-bare -> ambiguous
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "blouse",
                                      "action": "don", "covers": ["chest"]},
                                     {"op": "clothing", "char": "dane", "item": "shirt",
                                      "action": "don", "covers": ["chest"]},
                                     {"op": "clothing", "char": "dane", "item": "shirt",
                                      "action": "remove"}], "user", cfg)
    vios = linter.run(st(store, bid), "a bare chest in the lamplight", cfg)
    assert not any(v.rule == "L2" for v in vios)


def test_l2_l4_skipped_in_flashback():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "blouse",
                                      "action": "don", "covers": ["chest"]},
                                     {"op": "scene_mode", "mode": "flashback"}], "user", cfg)
    vios = linter.run(st(store, bid), "her bare chest, back then", cfg)
    assert not any(v.rule in ("L2", "L4") for v in vios)          # 08 B4


# ------------------------------ L3 contact validity -----------------------------------
def test_l3_penetrating_absent_party():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "contact", "action": "start",
                                      "type": "penetrating", "from_char": "dane",
                                      "to_char": "vexana", "from_part": "genitals",
                                      "to_part": "genitals"}], "user", cfg)
    assert any(v.rule == "L3" for v in linter.run(st(store, bid), "", cfg))


def test_l3_lying_front_penetration_impossible():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "position", "participants": ["dane"],
                                      "base": "lying_front"},
                                     {"op": "contact", "action": "start",
                                      "type": "penetrating", "from_char": "dane",
                                      "to_char": "kira", "from_part": "genitals",
                                      "to_part": "genitals"}], "user", cfg)
    assert any(v.rule == "L3" and "pose" in v.subjects
               for v in linter.run(st(store, bid), "", cfg))


# ------------------------------ L4 item state -----------------------------------------
def test_l4_manipulating_removed_item():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "skirt",
                                      "action": "don", "covers": ["hips"]},
                                     {"op": "clothing", "char": "kira", "item": "skirt",
                                      "action": "remove"}], "user", cfg)
    vios = linter.run(st(store, bid), "He unzips her skirt slowly.", cfg)
    l4 = [v for v in vios if v.rule == "L4"]
    assert l4 and "removed" in l4[0].note


# ------------------------------ L5 absent voice ----------------------------------------
def test_l5_dialogue_from_absent_char():
    cfg, store, sid, bid = mk()
    text = '"You called for me?" Vexana whispered from the doorway of memory.'
    vios = linter.run(st(store, bid), text, cfg)
    assert any(v.rule == "L5" and "vexana" in v.subjects for v in vios)


def test_l5_alias_attribution_counts():
    cfg, store, sid, bid = mk()
    vios = linter.run(st(store, bid), 'the sorceress says "Come closer."', cfg)
    assert any(v.rule == "L5" for v in vios)


def test_l5_present_char_dialogue_fine():
    cfg, store, sid, bid = mk()
    assert not linter.run(st(store, bid), '"Stay," Kira whispered.', cfg)


# ------------------------------ L6 timeline --------------------------------------------
def test_l6_time_skip_without_advance():
    cfg, store, sid, bid = mk()
    vios = linter.run(st(store, bid), "The next morning they met again.", cfg)
    l6 = [v for v in vios if v.rule == "L6"]
    assert l6 and not l6[0].advisory


def test_l6_silent_when_advance_applied():
    cfg, store, sid, bid = mk()
    vios = linter.run(st(store, bid), "The next morning they met again.", cfg,
                      applied_kinds=frozenset({"time_advance"}))
    assert not any(v.rule == "L6" for v in vios)


def test_l6_advisory_in_dream():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "scene_mode", "mode": "dream"}], "user", cfg)
    vios = linter.run(st(store, bid), "The next morning, in the dream,", cfg)
    l6 = [v for v in vios if v.rule == "L6"]
    assert l6 and l6[0].advisory                                   # 08 B4


# ------------------------------ L7 belief leak -----------------------------------------
def test_l7_unaware_char_references_secret():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "fact_admit",
                                      "statement": "the amulet is cursed",
                                      "cause": "creator:test:cursed-amulet",
                                      "authority": "creator", "visibility": "hidden"},
                                     {"op": "belief_acquire", "holder": "kira",
                                      "statement": "the amulet is cursed",
                                      "stance": "believes", "evidence_source": "told",
                                      "teller": "vexana", "visibility": "actor_scoped",
                                      "scoped_actors": ["kira"]}], "user", cfg)
    text = '"Careful — that amulet is cursed," Dane said grimly.'
    vios = linter.run(st(store, bid), text, cfg)
    assert any(v.rule == "L7" and "dane" in v.subjects for v in vios)


def test_l7_teller_and_learner_are_aware():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "presence", "entity": "vexana",
                                      "present": True},
                                     {"op": "fact_admit",
                                      "statement": "the amulet is cursed",
                                      "cause": "creator:test:cursed-amulet",
                                      "authority": "creator", "visibility": "hidden"},
                                     {"op": "belief_acquire", "holder": "kira",
                                      "statement": "the amulet is cursed",
                                      "stance": "believes", "evidence_source": "told",
                                      "teller": "vexana", "visibility": "actor_scoped",
                                      "scoped_actors": ["kira"]}], "user", cfg)
    ok = '"The amulet is cursed," Kira said. "Cursed," Vexana says.'
    assert not any(v.rule == "L7" for v in linter.run(st(store, bid), ok, cfg))


# ------------------------------ L8 consent (Q13 ladder) ---------------------------------
def _pen(store, cfg, sid, bid, intensity=1):
    apply_delta(store, sid, bid, 1, [{"op": "contact", "action": "start",
                                      "type": "penetrating", "from_char": "dane",
                                      "to_char": "kira", "from_part": "genitals",
                                      "to_part": "genitals",
                                      "intensity": intensity}], "user", cfg)


def test_l8_strict_fires_on_unknown():
    cfg, store, sid, bid = mk("strict")
    _pen(store, cfg, sid, bid)
    l8 = [v for v in linter.run(st(store, bid), "", cfg) if v.rule == "L8"]
    assert l8 and l8[0].severity == "high" and not l8[0].advisory


def test_l8_negotiated_silent_on_unknown_fires_below_granted():
    cfg, store, sid, bid = mk("negotiated")
    _pen(store, cfg, sid, bid)
    assert not any(v.rule == "L8" for v in linter.run(st(store, bid), "", cfg))
    apply_delta(store, sid, bid, 2, [{"op": "consent_set", "subject": "kira",
                                      "partner": "dane", "category": "vaginal",
                                      "level": "withdrawn"}], "user", cfg)
    assert any(v.rule == "L8" for v in linter.run(st(store, bid), "", cfg))


def test_l8_granted_is_clean_and_intensity_cap_enforced():
    cfg, store, sid, bid = mk("strict")
    apply_delta(store, sid, bid, 1, [{"op": "consent_signal", "from_char": "kira",
                                      "to_char": "dane", "category": "vaginal",
                                      "signal": "grant", "max_intensity": 2}],
                "extraction", cfg)          # organic path; user consent_set up-rank is Q11-gated
    _pen(store, cfg, sid, bid, intensity=1)
    assert not any(v.rule == "L8" for v in linter.run(st(store, bid), "", cfg))
    _pen(store, cfg, sid, bid, intensity=3)
    assert any(v.rule == "L8" for v in linter.run(st(store, bid), "", cfg))


def test_l8_cnc_advisory_unrestricted_off():
    cfg, store, sid, bid = mk("cnc")
    _pen(store, cfg, sid, bid)
    apply_delta(store, sid, bid, 2, [{"op": "consent_set", "subject": "kira",
                                      "partner": "dane", "category": "vaginal",
                                      "level": "withdrawn"}], "user", cfg)
    l8 = [v for v in linter.run(st(store, bid), "", cfg) if v.rule == "L8"]
    assert l8 and l8[0].advisory and l8[0].severity == "low"
    cfg.consent.mode = "unrestricted"
    assert not any(v.rule == "L8" for v in linter.run(st(store, bid), "", cfg))


# ------------------------------ L9 user guard (Q12) -------------------------------------
def test_l9_all_three_pattern_families():
    cfg, store, sid, bid = mk()
    state = st(store, bid)
    for text in ('Bean: "I surrender."',
                 '"Fine," Bean muttered.',
                 'Bean says quietly, "alright."',
                 '*Bean reaches for the door*'):
        vios = linter.run(state, text, cfg, user_name="Bean")
        assert any(v.rule == "L9" and v.severity == "high" for v in vios), text


def test_l9_skips_impersonate_and_mere_mention():
    cfg, store, sid, bid = mk()
    state = st(store, bid)
    assert not linter.run(state, 'Bean: "hi"', cfg, user_name="Bean",
                          klass="impersonate")
    assert not linter.run(state, "Kira glances at Bean and smiles.", cfg,
                          user_name="Bean")


# ------------------------------ orchestration -------------------------------------------
def test_lint_turn_persists_stages_note_and_cools_down():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "clothing", "char": "kira", "item": "skirt",
                                      "action": "don", "covers": ["hips"]},
                                     {"op": "clothing", "char": "kira", "item": "skirt",
                                      "action": "remove"}], "user", cfg)
    state = st(store, bid)
    text = "He unzips her skirt."
    fresh = linter.lint_turn(store, cfg, sid, bid, 5, state, text)
    assert [v.rule for v in fresh] == ["L4"]
    director.stage(store, cfg, sid, bid, 5, state, fresh)      # live flow: director owns note
    assert store.read_note(sid).startswith("[Direction] Continuity: the skirt was removed")
    assert store.lint_counts() == {"L4": 1}
    # same mismatch inside cooldown: no new row, note cleared (nag once, not every turn)
    fresh2 = linter.lint_turn(store, cfg, sid, bid, 6, state, text)
    assert fresh2 == []
    director.stage(store, cfg, sid, bid, 6, state, fresh2)
    assert store.read_note(sid) == ""
    assert store.lint_counts() == {"L4": 1}
    # past cooldown it may fire again
    assert [v.rule for v in linter.lint_turn(store, cfg, sid, bid, 9, state, text)] == ["L4"]


def test_lint_turn_top_severity_wins_and_l9_never_steers_director_slot():
    cfg, store, sid, bid = mk("strict")
    _pen(store, cfg, sid, bid)                        # L8 high
    state = st(store, bid)
    fresh = linter.lint_turn(store, cfg, sid, bid, 3, state,
                             "The next morning.\nBean: \"ow\"", user_name="Bean")
    director.stage(store, cfg, sid, bid, 3, state, fresh, user_name="Bean")
    note = store.read_note(sid)
    assert note.startswith("[Direction] STOP escalation")          # L8 beats L6; L9 excluded
    assert store.lint_l9_evidence(bid, 0) == 'Bean: "ow"'


def test_corrective_notes_off_still_detects():
    cfg, store, sid, bid = mk()
    cfg.linter.corrective_notes = False
    apply_delta(store, sid, bid, 1, [{"op": "contact", "action": "start", "type": "touching",
                                      "from_char": "kira", "to_char": "vexana"}], "user", cfg)
    fresh = linter.lint_turn(store, cfg, sid, bid, 2, st(store, bid), "prose")
    assert fresh and store.read_note(sid) == ""


def test_rules_off_and_disabled():
    cfg, store, sid, bid = mk()
    cfg.linter.rules_off = ["L6"]
    assert not linter.run(st(store, bid), "The next morning came.", cfg)
    cfg.linter.enabled = False
    assert linter.lint_turn(store, cfg, sid, bid, 1, st(store, bid), "x") == []


# ------------------------------ compose + guard escalation ------------------------------
def test_note_injects_as_director_note_component():
    cfg, store, sid, bid = mk()
    doc = {"messages": [{"role": "user", "content": "hi"}]}
    out, kept = compose(doc, st(store, bid), cfg, None, "new_turn",
                        note="[Direction] Note: test.")
    assert any(k["cls"] == "director_note" for k in kept)
    joined = "".join(m["content"] for m in out["messages"] if m["role"] == "system")
    assert "[Direction] Note: test." in joined


def test_guard_escalates_with_evidence_and_respects_mode():
    cfg = Config()
    cfg.user_guard.name = "Bean"
    base = render_guard(cfg, None, "new_turn")
    esc = render_guard(cfg, None, "new_turn", evidence='Bean: "ow"')
    assert "VIOLATION last turn" in esc and 'Bean: "ow"' in esc and esc != base
    cfg.user_guard.mode = "prevent"
    assert render_guard(cfg, None, "new_turn", evidence="x") == base


def test_rollback_retracts_lint_and_stale_note():
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "contact", "action": "start", "type": "touching",
                                      "from_char": "kira", "to_char": "vexana"}], "user", cfg)
    state = st(store, bid)
    fresh = linter.lint_turn(store, cfg, sid, bid, 4, state, "prose")
    director.stage(store, cfg, sid, bid, 4, state, fresh)
    assert store.lint_counts() and store.read_note(sid)
    store.rollback_to(bid, 2)
    assert store.lint_counts() == {} and store.read_note(sid) == ""


def test_malformed_state_never_raises():
    cfg = Config()
    assert linter.run({"entities": None, "scene": "??", "clock": []},   # garbage in
                      "some prose", cfg) is not None                    # invariant 3
