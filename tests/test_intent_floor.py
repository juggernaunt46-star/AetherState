"""Pure-code semantic REFLEX floor (2026-07-10, Bean) — the invariant-2-safe layer of the intent
cascade. Two grounded, deterministic, no-model/no-network layers gated by
[specialization].intent_floor (default on): (a) a stemmer + curated intent lexicon so natural
phrasings map to the OWNED skill they mean; (b) the entity-aware target picker — a strike resolves
to a PRESENT cast member, never a token run off the prose. off = the exact-match keyword floor
(v1.17.0); a `none` session is inert (byte-identical)."""
from __future__ import annotations

import json
import random

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.state import apply_delta, empty_state
from tests.mock_upstream import Reply


def _rpg_cfg(intent_floor=True):
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.intent_floor = intent_floor
    return cfg


def _player_state():
    st = empty_state()
    st["entities"]["wrenna"] = {"kind": "player", "name": "Wrenna", "present": True, "aliases": []}
    st["player"] = {"wrenna": {"eid": "wrenna", "stats": {"CHA": 14, "DEX": 13, "STR": 12},
        "skills": {"persuasion": 2, "stealth": 2, "swordplay": 1, "athletics": 1},
        "abilities": [], "defs": {}}}
    return st


def _combat_state():
    st = _player_state()
    st["entities"]["maren"] = {"kind": "npc", "name": "Maren", "present": True, "aliases": []}
    st["entities"]["pines"] = {"kind": "location", "name": "The Pines", "present": False}
    return st


def _echo_step_state():
    st = _player_state()
    player = st["player"]["wrenna"]
    player["defs"] = {
        "skills": {
            "echo_step": {
                "name": "Echo Step",
                "keyed_stat": "DEX",
                "governs": ["read", "listen", "trace", "touch", "feel"],
                "requires_ability": "echo_step",
            },
        },
        "abilities": {
            "echo_step": {
                "name": "Echo Step",
                "kind": "basis",
                "mechanic": "basis",
                "applies_to": "echo_step",
            },
        },
    }
    player["abilities"] = ["echo_step"]
    st["entities"]["tala"] = {
        "kind": "npc", "name": "Tala", "present": True, "aliases": []}
    return st


def _run(msg, cfg, st, klass="new_turn", seed=5, assistant=None):
    msgs = []
    if assistant:
        msgs.append({"role": "assistant", "content": assistant})
    msgs.append({"role": "user", "content": msg})
    return tier0.run({"messages": msgs}, klass, False, st, cfg, random.Random(seed))


def _checks(res):
    return [o for o in res.rule_ops if o.get("op") == "check"]


def _spawns(res):
    return [o for o in res.rule_ops if o.get("op") == "combatant_spawn"]


# ---- (a) stemmer + intent lexicon: a natural phrasing rolls the owned skill it MEANS ----------
def test_lexicon_synonym_fires_owned_skill():
    cfg = _rpg_cfg(True)                              # 'haggle' is not in persuasion's `governs`
    assert any(c["skill"] == "persuasion"
               for c in _checks(_run("I haggle over the price of the horse", cfg, _player_state())))


def test_conjugation_fires_via_stemmer():
    cfg = _rpg_cfg(True)                              # 'sneaked'/'scrambled' aren't literal governs
    assert any(c["skill"] == "stealth"
               for c in _checks(_run("I sneaked past the sentries", cfg, _player_state())))
    assert any(c["skill"] == "athletics"
               for c in _checks(_run("I scrambled up the wall", cfg, _player_state())))


def test_quoted_incidental_combat_language_is_not_an_action():
    """Wyrmgard: dialogue about striking must not roll or open the War Room."""
    cfg = _rpg_cfg(True)
    res = _run("I tell Maren, 'Do not strike the bell.'", cfg, _combat_state(),
               assistant="Maren listens.")
    assert not _checks(res)
    assert not _spawns(res)


def test_wyrmgard_haggle_ignores_skill_words_inside_dialogue():
    """The real social action survives while `blade` and `run` inside speech stay fictional."""
    cfg = _rpg_cfg(True)
    res = _run('I haggle with Maren. "A sworn blade\'s while, and coin for a milk-run."',
               cfg, _combat_state(), assistant="Maren names a steep price.")
    assert [c["skill"] for c in _checks(res)] == ["persuasion"]
    assert not _spawns(res)


def test_real_action_after_smart_quoted_dialogue_still_fires():
    cfg = _rpg_cfg(True)
    res = _run("I warn Maren, ‘Do not strike the bell,’ then haggle over the price.",
               cfg, _combat_state(), assistant="Maren waits.")
    assert [c["skill"] for c in _checks(res)] == ["persuasion"]
    assert not _spawns(res)


def test_embedded_question_about_past_touch_does_not_invoke_echo_step():
    """Emberglass: asking who touched an object is not the Player touching it now."""
    cfg = _rpg_cfg(True)
    res = _run(
        "Wrenna haggles with Tala and asks who last touched the medicine chest.",
        cfg,
        _echo_step_state(),
        assistant="Tala guards the chest.",
    )

    assert [c["skill"] for c in _checks(res)] == ["persuasion"]


def test_direct_player_touch_still_invokes_echo_step_naturally():
    cfg = _rpg_cfg(True)
    res = _run(
        "Wrenna touches the emberglass and traces the freshest movement through it.",
        cfg,
        _echo_step_state(),
        assistant="The shard hums under her hand.",
    )

    assert [c["skill"] for c in _checks(res)] == ["echo_step"]


def test_negated_hypothetical_and_third_party_touch_do_not_invoke_player_skill():
    cfg = _rpg_cfg(True)
    st = _echo_step_state()

    assert not _checks(_run("Wrenna does not touch the emberglass.", cfg, st))
    assert not _checks(_run("If Wrenna touched the emberglass, it might answer.", cfg, st))
    assert not _checks(_run("Tala touched the emberglass before Wrenna arrived.", cfg, st))


def test_real_touch_after_embedded_question_survives_clause_filter():
    cfg = _rpg_cfg(True)
    res = _run(
        "Wrenna asks who touched the chest, then touches the emberglass herself.",
        cfg,
        _echo_step_state(),
    )

    assert [c["skill"] for c in _checks(res)] == ["echo_step"]


def test_no_roll_turn_guidance_distinguishes_conversation_from_unresolved_action():
    cfg = _rpg_cfg(True)
    st = _echo_step_state()
    conversational = _run(
        'I do not touch the root again. I ask Hallek, "Who touched this row?"', cfg, st)
    unresolved = _run("I examine the unfamiliar valve for hidden tampering.", cfg, st)
    resolved = _run("I touch the root and use Echo Step to trace it.", cfg, st)

    assert conversational.turn_guidance == "free_narration"
    assert unresolved.turn_guidance == "unresolved"
    assert resolved.turn_guidance == ""


def test_named_skill_anchors_clause_without_blocking_separate_action():
    cfg = _rpg_cfg(True)
    st = _player_state()
    player = st["player"]["wrenna"]
    player["defs"] = {"skills": {
        "tide_reading": {"name": "Tide Reading", "keyed_stat": "INT",
                         "governs": ["read tides", "forecast"]},
        "lore": {"name": "Lore", "keyed_stat": "INT", "governs": ["calculate"]},
    }}
    player["skills"].update({"tide_reading": 1, "lore": 1})

    one_action = _run(
        "I use Tide Reading to calculate whether the datum shifted naturally.", cfg, st)
    two_actions = _run(
        "I use Tide Reading to forecast the low, then calculate the old inscription.", cfg, st)

    assert [c["skill"] for c in _checks(one_action)] == ["tide_reading"]
    assert {c["skill"] for c in _checks(two_actions)} == {"tide_reading", "lore"}


def test_floor_off_reverts_to_exact_keyword_match():
    """intent_floor=False = v1.17.0: a synonym `governs` never listed does NOT fire; a literal
    governs verb still does."""
    off = _rpg_cfg(False)
    assert not any(c["skill"] == "persuasion"
                   for c in _checks(_run("I haggle over the price", off, _player_state())))
    assert any(c["skill"] == "persuasion"
               for c in _checks(_run("I persuade the guard", off, _player_state())))


def test_unowned_skill_synonym_never_fires():
    """Eligibility gate: a synonym of an UNOWNED skill is a non-move (nothing rollable w/o a basis)."""
    cfg = _rpg_cfg(True)
    st = _player_state()
    st["player"]["wrenna"]["skills"].pop("stealth")
    assert not any(c["skill"] == "stealth"
                   for c in _checks(_run("I tiptoe past the sentries", cfg, st)))


# ---- (b) entity-aware target picker: a strike lands on a PRESENT cast member ------------------
def test_strike_stages_present_cast_member_as_foe():
    cfg = _rpg_cfg(True)
    res = _run("I strike at Maren with my blade", cfg, _combat_state(),
               assistant="Maren bars the door, sneering.")
    sp = _spawns(res)
    assert sp and sp[0]["name"] == "Maren" and sp[0].get("char") == "maren"


def _meteor_hammer_state(*names: str):
    st = _combat_state()
    st["entities"].pop("maren")
    for name in names:
        st["entities"][tier0.slug(name)] = {
            "kind": "npc", "name": name, "present": True, "aliases": []}
    player = st["player"]["wrenna"]
    player["skills"]["meteor_hammer_orbit_strike"] = 4
    player["defs"] = {"skills": {"meteor_hammer_orbit_strike": {
        "name": "Meteor-Hammer Orbit Strike",
        "keyed_stat": "DEX",
        "governs": ["meteor", "hammer", "orbit", "strike"],
        "cost": {},
    }}}
    return st


_KESSA_STRIKE = (
    "I use Meteor-Hammer Orbit Strike to loop the iron weight around Kessa's spear shaft, "
    "wrench her weapon arm off line, and drive the blunt second weight into her ribs."
)


def test_custom_skill_short_target_binds_the_full_present_authored_foe():
    res = _run(
        _KESSA_STRIKE,
        _rpg_cfg(True),
        _meteor_hammer_state("Span Warden Kessa"),
        assistant="Span Warden Kessa stands thirteen paces away with spear and shield.",
    )

    assert len(_spawns(res)) == 1
    spawn = _spawns(res)[0]
    assert {key: spawn.get(key) for key in (
        "op", "name", "side", "tier", "char", "_cid", "_intro_intent_visible"
    )} == {
        "op": "combatant_spawn",
        "name": "Span Warden Kessa",
        "side": "enemy",
        "tier": "standard",
        "char": "span_warden_kessa",
        "_cid": "span_warden_kessa",
        "_intro_intent_visible": "known-opponent-opening/1",
    }
    commit = next(op for op in res.rule_ops if op.get("op") == "semantic_frame_commit")
    assert spawn.get("_semantic_frame_ref") == commit["frame"]["fingerprint"]


def test_custom_skill_shared_short_target_abstains_instead_of_minting_an_alias():
    res = _run(
        _KESSA_STRIKE,
        _rpg_cfg(True),
        _meteor_hammer_state("North Warden Kessa", "South Warden Kessa"),
        assistant="North Warden Kessa and South Warden Kessa hold the crossing.",
    )

    assert not _spawns(res)


def test_generic_final_name_token_never_becomes_a_derived_reference():
    res = _run(
        "I strike one of the brass levers.",
        _rpg_cfg(True),
        _meteor_hammer_state("Masked One"),
        assistant="The Masked One waits nearby.",
    )

    assert not _spawns(res)


def test_incidental_possessive_reference_does_not_redirect_a_custom_strike():
    res = _run(
        "I use Meteor-Hammer Orbit Strike to hit the warning bell while checking Kessa's permit.",
        _rpg_cfg(True),
        _meteor_hammer_state("Span Warden Kessa"),
        assistant="Span Warden Kessa holds out her permit.",
    )

    assert not _spawns(res)


def test_possessed_weapon_noun_does_not_open_combat_during_inspection():
    state = _meteor_hammer_state("Span Warden Kessa")
    assistant = "Span Warden Kessa presents the spear for inspection."

    assert not _spawns(_run(
        "I inspect Kessa's spear for maker's marks.", _rpg_cfg(True), state,
        assistant=assistant))
    assert not _spawns(_run(
        "I use Perception to study Kessa's spear from where I stand.", _rpg_cfg(True), state,
        assistant=assistant))


def test_inferred_brawl_fallback_cannot_override_inspection_topology():
    state = _combat_state()
    state["player"]["wrenna"]["skills"]["brawl"] = 0
    variants = (
        (
            "I use my sight-lens to study Maren's stance and mirror shield, looking for any "
            "exposed opening without attacking."
        ),
        "I study Maren's stance rather than attacking.",
        "I study Maren's stance instead of attacking.",
        "I study Maren's stance before attacking.",
        "I study Maren's stance after attacking.",
        "I study Maren's stance while not attacking.",
    )

    for text in variants:
        res = _run(text, _rpg_cfg(True), state, assistant="Maren holds position.")
        assert not _checks(res), text
        assert not _spawns(res), text
        assert not any(
            op.get("op") in {"mastery_tick", "hp", "damage"} for op in res.rule_ops
        ), text
        frames = [
            frame for frame in res.semantic_turn.frames
            if any(candidate.capability_id == "brawl" for candidate in frame.candidates)
        ]
        assert len(frames) == 1, text
        assert frames[0].action_class == "inspection", text
        assert frames[0].capability_id is None, text
        assert "brawl" in frames[0].ambiguity, text


def test_positive_observation_survives_without_attack_adjunct():
    state = _combat_state()
    player = state["player"]["wrenna"]
    player["stats"]["CUN"] = 13
    player["skills"].update({"perception": 1, "brawl": 0})

    res = _run(
        "I watch Maren's stance and mirror shield without attacking.",
        _rpg_cfg(True),
        state,
        assistant="Maren holds position.",
    )

    assert [check["skill"] for check in _checks(res)] == ["perception"]
    assert not _spawns(res)


def test_direction_or_place_never_becomes_a_foe():
    """Briarhold/Irongate: an attack whose object is a direction/place mints NO foe, even with a
    present NPC in the scene who is NOT the one attacked."""
    cfg = _rpg_cfg(True)
    res = _run("I strike toward the far corner with my blade", cfg, _combat_state(),
               assistant="The room is still and empty.")
    assert not _spawns(res)


def test_run_weapon_through_target_is_swordplay_not_athletics():
    cfg = _rpg_cfg(True)
    st = _combat_state()
    st["player"]["wrenna"]["skills"]["brawl"] = 0

    res = _run("I run my blade through Maren.", cfg, st,
               assistant="Maren steps into the narrow passage.")

    assert [c["skill"] for c in _checks(res)] == ["swordplay"]
    assert _spawns(res)[0]["name"] == "Maren"


def test_literal_run_remains_athletics_and_does_not_open_combat():
    cfg = _rpg_cfg(True)
    st = _combat_state()
    st["player"]["wrenna"]["skills"]["brawl"] = 0

    res = _run("I run toward the gate.", cfg, st,
               assistant="Maren watches from beside the road.")

    assert [c["skill"] for c in _checks(res)] == ["athletics"]
    assert not _spawns(res)


def test_specialized_skill_wins_same_span_over_brawl_fallback():
    cfg = _rpg_cfg(True)
    st = _combat_state()
    st["player"]["wrenna"]["skills"]["brawl"] = 0

    blade = _run("I strike at Maren with my blade.", cfg, st,
                 assistant="Maren raises her guard.")
    grapple = _run("I grapple Maren to the ground.", cfg, st,
                   assistant="Maren braces herself.")
    punch = _run("I punch Maren in the jaw.", cfg, st,
                 assistant="Maren squares up.")

    assert [c["skill"] for c in _checks(blade)] == ["swordplay"]
    assert [c["skill"] for c in _checks(grapple)] == ["athletics"]
    assert [c["skill"] for c in _checks(punch)] == ["brawl"]


def test_phrasebook_slash_falls_back_to_owned_brawl_and_opens_combat():
    """Ashfall live case: a baseline card with no Swordplay still gets one code-owned roll."""
    cfg = _rpg_cfg(True)
    st = _combat_state()
    st["player"]["wrenna"]["skills"].pop("swordplay")
    st["player"]["wrenna"]["skills"]["brawl"] = 0

    res = _run(
        "I draw my short sword, rush the ash-wolf, and slash at its neck before it can pounce.",
        cfg,
        st,
        assistant=("The ash-wolf lowers its head in the customs yard. Survive it before the "
                   "rest of its pack arrives."),
    )

    assert [c["skill"] for c in _checks(res)] == ["brawl"]
    spawn = _spawns(res)[0]
    assert spawn["name"] == "Ash Wolf"
    frame = next(
        op["frame"] for op in res.rule_ops
        if op.get("op") == "semantic_frame_commit"
        and op["frame"]["action_class"] == "weapon_attack"
    )
    assert frame["target_entity_id"] == spawn["_cid"] == "ash_wolf"
    assert frame["target_name"] == "Ash Wolf"
    assert not any("atomic mechanic group was incomplete" in n for n in res.notices)


def test_equal_custom_capability_overlap_abstains_with_semantic_evidence():
    cfg = _rpg_cfg(True)
    st = _player_state()
    player = st["player"]["wrenna"]
    player["skills"].update({"rune_lore": 1, "forensic_sight": 1})
    player["defs"] = {"skills": {
        "rune_lore": {"name": "Rune Lore", "keyed_stat": "INT",
                      "governs": ["examine"]},
        "forensic_sight": {"name": "Forensic Sight", "keyed_stat": "CUN",
                           "governs": ["examine"]},
    }}

    res = _run("I examine the unfamiliar sigil.", cfg, st)

    assert not _checks(res)
    assert res.turn_guidance == "unresolved"
    ambiguous = [f for f in res.semantic_turn.frames if f.ambiguity]
    assert len(ambiguous) == 1
    assert set(ambiguous[0].ambiguity) == {"rune_lore", "forensic_sight"}


# ---- invariants: none-inert + deterministic (replay-pure detection) ---------------------------
def test_none_session_is_inert():
    cfg = Config()                                   # specialization = none
    res = _run("I sweet-talk Maren and strike at her with my blade", cfg, _combat_state(),
               assistant="Maren sneers.")
    assert not any(o.get("op") in ("check", "combatant_spawn") for o in res.rule_ops)


def test_detection_is_deterministic():
    """Same state + text + seed -> byte-identical rule ops (the detection layer is a pure function;
    resolution values are baked, so replay from the journal is stable)."""
    cfg = _rpg_cfg(True)

    def norm(res):
        return json.dumps([{k: v for k, v in o.items() if k != "raw"} for o in res.rule_ops],
                          sort_keys=True, default=str)
    a = _run("I strike at Maren with my blade", cfg, _combat_state(), assistant="Maren bars the door.")
    b = _run("I strike at Maren with my blade", cfg, _combat_state(), assistant="Maren bars the door.")
    assert norm(a) == norm(b)


async def test_wyrmgard_haggle_proxy_path_injects_only_persuasion(
        client, proxy_app, mock_upstream, cfg):
    """Full proxy path: the current turn carries one correct directive and no phantom combat."""
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    external_id = "st-wyrmgard-quoted-haggle"
    store = proxy_app.state.store
    sid, bid = store.create_session(external_id=external_id)
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Wrenna", "kind": "player"},
        {"op": "entity_add", "name": "Maren", "kind": "npc"},
        {"op": "presence", "entity": "Maren", "present": True},
        {"op": "player_seed", "entity": "Wrenna", "card": {
            "stats": {"CHA": 14, "STR": 12},
            "skills": {"persuasion": 2, "swordplay": 1, "athletics": 1},
        }},
    ], "genesis", cfg)
    reply = {"choices": [{"message": {"role": "assistant",
                                       "content": "Maren considers the offer."}}]}
    mock_upstream.enqueue(Reply(body=json.dumps(reply).encode()))
    payload = {"model": "synthetic", "messages": [
        {"role": "system", "content":
         f"<<AETHER:v=1;session={external_id};turn=1;type=normal;"
         "speaker=Dungeon Master;user=Bean>>"},
        {"role": "assistant", "content": "Maren names a steep price."},
        {"role": "user", "content":
         'I haggle with Maren. "A sworn blade\'s while, and coin for a milk-run."'},
    ]}
    response = await client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200 and response.json() == reply

    forwarded_doc = json.loads(mock_upstream.requests[0].body)
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in forwarded_doc.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "system"
    )
    realization_line = next(
        line for line in system_text.splitlines()
        if line.startswith("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1")
    )
    realization = json.loads(realization_line[realization_line.index("{"):])
    assert realization["schema"] == "narrator-realization/1"
    assert [
        (
            row["adapter_id"],
            row["event_meaning"]["capability_id"],
            row["event_meaning"]["action_class"],
        )
        for row in realization["asserted_settled"]
    ] == [("narrator.skill-check/1", "persuasion", "skill_check")]
    event_capabilities = {
        row["event_meaning"]["capability_id"]
        for bucket in ("asserted_settled", "asserted_unresolved", "attributed_noncurrent")
        for row in realization[bucket]
    }
    assert event_capabilities.isdisjoint({"swordplay", "athletics"})
    state = (await client.get(f"/aether/session/{sid}/state")).json()["state"]
    assert [r["skill"] for r in state["rolls"]] == ["persuasion"]
    assert not state["combat"].get("active")
