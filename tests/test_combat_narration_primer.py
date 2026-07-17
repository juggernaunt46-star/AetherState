"""Private combat-opening demonstrations: bounded, hidden, and prompt-only."""
from __future__ import annotations

import json
import random

from aetherstate import compose, prompts, tier0
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline, _last_user_action_hash
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.combat_opening_primer = True
    cfg.injection.max_tokens = 2400
    cfg.injection.priorities["combat_primer"] = 74
    return cfg


def _state(*, turn: int = 8, active: bool = False, started: int | None = None) -> dict:
    state = {
        "meta": {"turn": turn},
        "scene": {"location_id": "iron-yard", "phase": "rising", "present": []},
        "clock": {}, "chars": {}, "attributes": {}, "poses": {}, "clothing": {},
        "effects": {}, "quests": {}, "rolls": [], "entities": {}, "player": {},
    }
    if active:
        state["combat"] = {"active": True, "started_turn": started,
                           "combatants": {}, "pending_intent": {}}
    return state


def _wire(cfg: Config, state: dict, *, signaled: bool) -> tuple[str, list[dict]]:
    doc = {"model": "m", "messages": [{"role": "user", "content": "I hold position."}]}
    out, kept = compose.compose(
        doc, state, cfg, Stamp(session="primer", user="Bean"), "new_turn",
        combat_opening=signaled)
    assert out is not None
    text = "\n".join(str(row.get("content", "")) for row in out["messages"])
    return text, kept


def test_primer_has_four_increasingly_complex_finished_demonstrations():
    text = prompts.COMBAT_NARRATION_PRIMER

    assert prompts.COMBAT_NARRATION_PRIMER_VERSION == "combat-narration-primer/4"
    assert text.count("SYNTHETIC PLAYER:") == 4
    assert text.count("CODE RESULTS:") == 4
    assert text.count("IDEAL NARRATOR:") == 4
    assert "EXAMPLE 1 — CODE-STAGED KNOWN FOE; TWO RESULTS AND PENDING INTENT" in text
    assert "EXAMPLE 2 — PLAYER, ALLY, THEN PENDING INTENT" in text
    assert "EXAMPLE 3 — SETTLED ENEMY MISS, TWO ALLIED RESULTS, MAGIC INTENT" in text
    assert "EXAMPLE 4 — BRACE, LAUNCHED ACTION, ALLY MISS" in text
    assert text.count("2d6") >= 8
    assert "NON-CANONICAL" in text
    assert "Never quote, summarize, mention" in text
    assert "never expose dice notation" in text
    assert "No approach, launch, impact, or range change exists" in text
    assert "the measured strike has not landed" in text
    assert "Rusk's sweep has not landed" in text
    assert "Nothing has landed" not in text
    assert "Orra's blade against Rusk" in text
    assert "[foe |" not in text and "[hp |" not in text
    assert tier0.parse_foe_tags(text, _state()) == []
    assert tier0.parse_reply_tags(text, _state()) == []


def test_window_is_signal_plus_first_two_code_owned_combat_turns_only():
    cfg = _cfg()

    assert compose._combat_opening_window(_state(), cfg, signaled=True)
    assert compose._combat_opening_window(_state(turn=5, active=True, started=5), cfg)
    assert compose._combat_opening_window(_state(turn=6, active=True, started=5), cfg)
    assert not compose._combat_opening_window(_state(turn=7, active=True, started=5), cfg)
    assert not compose._combat_opening_window(_state(), cfg)


def test_window_respects_mode_war_room_and_live_disable():
    cfg = _cfg()
    state = _state(turn=5, active=True, started=5)

    cfg.specialization.name = "none"
    assert not compose._combat_opening_window(state, cfg, signaled=True)
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = False
    assert not compose._combat_opening_window(state, cfg, signaled=True)
    cfg.specialization.war_room = True
    cfg.specialization.combat_opening_primer = False
    assert not compose._combat_opening_window(state, cfg, signaled=True)


def test_compose_injects_one_primer_and_compact_contract_during_opening():
    cfg = _cfg()
    text, kept = _wire(cfg, _state(), signaled=True)

    assert text.count("[PRIVATE COMBAT NARRATION PRIMER ") == 1
    assert [row["cls"] for row in kept].count("combat_primer") == 1
    assert [row["cls"] for row in kept].count("rules_contract") == 1
    assert "A GAME with dice, not chat." in text
    assert "You are the Game Master of a mechanical RPG" not in text
    primer_row = next(row for row in kept if row["cls"] == "combat_primer")
    assert primer_row["tokens"] == compose.estimate_tokens(prompts.COMBAT_NARRATION_PRIMER)


def test_compose_removes_primer_after_opening_without_changing_normal_budget():
    cfg = _cfg()
    opening, _ = _wire(cfg, _state(turn=6, active=True, started=5), signaled=False)
    later, kept = _wire(cfg, _state(turn=7, active=True, started=5), signaled=False)

    assert "PRIVATE COMBAT NARRATION PRIMER" in opening
    assert "PRIVATE COMBAT NARRATION PRIMER" not in later
    assert all(row["cls"] != "combat_primer" for row in kept)
    assert compose._injection_cap(cfg) == 2400


def test_primer_header_echo_is_reserved_input_not_narrator_history():
    echoed = (
        "[PRIVATE COMBAT NARRATION PRIMER combat-narration-primer/4 — ENGINE INPUT ONLY]\n"
        "The live story sentence remains."
    )

    cleaned = compose._without_stale_engine_context([
        {"role": "assistant", "content": echoed},
    ])

    assert cleaned == [{"role": "assistant", "content": "The live story sentence remains."}]


def test_visible_intro_provenance_is_exact_and_rule_owned():
    cfg = _cfg()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="intro-provenance")
    apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {}, "resources": {"hp": {"max": 24}},
        }},
    ], "genesis", cfg)
    forged = apply_delta(store, sid, branch, 1, [{
        "op": "combatant_spawn", "name": "Forged Foe", "side": "enemy",
        "_intro_intent_visible": "known-opponent-opening/1",
    }], "user", cfg)

    assert not forged.applied
    assert forged.quarantined and "code-owned rule receipt" in forged.quarantined[0]["reason"]
    accepted = apply_delta(store, sid, branch, 1, [{
        "op": "combatant_spawn", "name": "Rule Foe", "side": "enemy",
        "_intro_intent_visible": "known-opponent-opening/1",
    }], "rule", cfg)
    assert accepted.applied


def test_quoted_punctuation_keeps_opening_actor_evidence_on_the_detector_clause():
    cfg = _cfg()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="quoted-opening-evidence")
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {}, "resources": {"hp": {"max": 24}},
        }},
        {"op": "entity_add", "name": "Marshal Varo", "kind": "npc"},
        {"op": "presence", "entity": "Marshal Varo", "present": True},
    ], "genesis", cfg)
    assert seeded.applied
    source = '"Wait!" I draw my longsword and challenge Marshal Varo.'

    result = tier0.run(
        {"messages": [{"role": "user", "content": source}]},
        "new_turn",
        False,
        current_state(store, branch),
        cfg,
        random.Random(17),
        turn=1,
    )
    frame = next(
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
        and op["frame"]["action_class"] == "combat_opening"
    )
    actor = next(row for row in frame["evidence"] if row["kind"] == "actor")
    target = next(row for row in frame["evidence"] if row["kind"] == "target")

    assert source[actor["start"]:actor["end"]] == "I"
    assert source[target["start"]:target["end"]] == "Marshal Varo"


def test_pipeline_root_k_packet_is_private_hash_neutral_and_duplicate_stable(monkeypatch):
    cfg = _cfg()
    store = Store(":memory:")
    _sid, branch = store.create_session(external_id="primer-root-k")
    apply_delta(store, _sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {"athletics": 1, "perception": 1},
            "resources": {"hp": {"max": 24}},
        }},
        {"op": "entity_add", "name": "Marshal Varo", "kind": "npc",
         "aliases": ["Varo"]},
        {"op": "set_attribute", "entity": "Marshal Varo", "key": "role",
         "value": "spear-and-shield marshal"},
        {"op": "memory_event", "text":
         "Opening scene: Kael and Marshal Varo stand twelve paces apart."},
        {"op": "presence", "entity": "Marshal Varo", "present": True},
    ], "genesis", cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, rng=random.Random(17))
    original_assessment = tier0.combat_opening_assessment
    original_run = tier0.run
    assessments = []
    tier0_inputs = []

    def assess_once(*args, **kwargs):
        result = original_assessment(*args, **kwargs)
        assessments.append(result)
        return result

    def run_with_shared_assessment(*args, **kwargs):
        tier0_inputs.append(kwargs.get("opening_assessment"))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(tier0, "combat_opening_assessment", assess_once)
    monkeypatch.setattr(tier0, "run", run_with_shared_assessment)
    player_text = (
        "I cross the first iron line, draw my longsword, and challenge Marshal Varo. "
        "He is the hostile first opponent; I hold position and watch him commit."
    )
    raw_doc = {"model": "m", "messages": [{"role": "user", "content": player_text}]}
    body = json.dumps(raw_doc).encode()
    stamp = Stamp(session="primer-root-k", gen_type="normal", turn=1, user="Kael",
                  card_role="narrator")

    first, first_ctx = pipe.process(stamp, body)
    duplicate, duplicate_ctx = pipe.process(stamp, body)
    packet = json.loads(first)
    primer_rows = [row for row in packet["messages"]
                   if isinstance(row, dict)
                   and "PRIVATE COMBAT NARRATION PRIMER" in str(row.get("content", ""))]

    assert len(assessments) == 1
    assert tier0_inputs == [assessments[0]]

    assert len(primer_rows) == 1 and primer_rows[0]["role"] == "system"
    assert player_text not in primer_rows[0]["content"]
    packet_text = "\n".join(str(row.get("content", "")) for row in packet["messages"])
    assert first_ctx.local_response is None
    assert duplicate_ctx.local_response is None
    assert "narrator.skill-check/1" in packet_text
    assert "narrator.combat-opening/1" in packet_text
    assert "[WAR] round 1" in packet_text
    assert "[ENEMY INTENT enemy-intent/1]" in packet_text
    assert "[ENEMY ACTION enemy-action/1]" not in packet_text
    assert "FIRST-INTENT CONTINUITY" in packet_text
    assert "Preserve every authored/current distance exactly" in packet_text
    assert "abstract range category describes its eventual delivery" in packet_text
    assert "Only the exact Visible tell in the complete beat below authorizes enemy preparation" \
        in packet_text
    assert "Never infer or extend the tell into a step, approach, range change" in packet_text
    assert 'Exact authored separation phrase: "twelve paces apart"' in packet_text
    assert "Exact complete enemy prose:" in packet_text
    assert "The future intent remains unchanged and pending" in packet_text
    assert "not passive readiness, a reset, or a skipped enemy turn" in packet_text
    assert "Player's response window" in packet_text
    assert "The attack is committed; this is the moment to answer it before impact." in packet_text
    current = next(row["content"] for row in packet["messages"]
                   if row.get("role") == "user" and "[AETHER P1]" in str(row.get("content", "")))
    assert current.count("SETTLED PLAYER RESULT + FIRST INTENT OUTPUT LIMIT") == 1
    assert "FIRST-INTENT OUTPUT SHAPE" not in current
    assert current.index("SETTLED PLAYER RESULT + FIRST INTENT OUTPUT LIMIT") \
        < current.index("[AETHER P1]") \
        < current.index(player_text)
    assert "Narrate every current asserted_settled Player result" in current
    assert "one complete enemy beat" in current
    assert "does not advance, disrupt, alter, deflect, interrupt, cancel" in current
    assert 'exact existing distance phrase once: "twelve paces apart"' in current
    cleaned, changed = compose.without_attached_user_context({
        "messages": [{"role": "user", "content": current}],
    })
    assert changed is True
    assert cleaned["messages"] == [{"role": "user", "content": player_text}]
    state = current_state(store, branch)
    enemies = [row for row in (state.get("combat") or {}).get("combatants", {}).values()
               if row.get("side") == "enemy"]
    assert len(enemies) == 1 and enemies[0].get("eid") == "marshal_varo"
    assert "spear" in enemies[0].get("armament", "")
    assert "martial" in (enemies[0].get("kit") or {}).get("basis", [])
    assert (state.get("combat") or {}).get("pending_intent", {}).get("actor") == enemies[0]["id"]
    turn_ops = store.rule_ops_at(branch, first_ctx.turn_index)
    checks = [op for op in turn_ops if op.get("op") == "check"]
    assert checks and checks[0]["skill"] == "perception"
    assert "_target" not in checks[0] and "_dmg" not in checks[0]
    semantic_frames = [
        op["frame"] for op in turn_ops if op.get("op") == "semantic_frame_commit"
    ]
    assert [
        (
            frame["event_node_id"],
            frame["action_class"],
            frame["context_frame"]["span_start"],
            frame["context_frame"]["span_end"],
        )
        for frame in semantic_frames
    ] == [
        ("event.t1.f1", "skill_check", 110, 146),
        ("event.t1.f2", "combat_opening", 0, 74),
    ]
    assert all(frame["meaning_binding_ref"] for frame in semantic_frames)
    receipts = [
        row["receipt"]
        for row in state["mechanic_settlements"]
        if row["turn"] == first_ctx.turn_index
    ]
    assert {receipt["contract_id"] for receipt in receipts} == {
        "combat_opening/1", "skill_check/1",
    }
    opening_receipt = next(
        receipt for receipt in receipts if receipt["contract_id"] == "combat_opening/1"
    )
    admissions = [
        change for change in opening_receipt["applied_changes"]
        if change["kind"] == "target_admission"
    ]
    assert len(admissions) == 1
    assert opening_receipt["target_post_state"]["combatant_id"] == "marshal_varo"
    assert enemies[0]["hp"]["cur"] == enemies[0]["hp"]["max"]
    assert not any(op.get("op") == "combatant_hp" for op in turn_ops)
    assert not any(isinstance(op.get("_opposition"), dict) for op in turn_ops)
    assert first == duplicate
    assert duplicate_ctx.network_duplicate is True
    turn = store.db.execute(
        "SELECT user_hash FROM turns WHERE branch_id=? AND turn_index=?",
        (branch, first_ctx.turn_index),
    ).fetchone()
    assert turn["user_hash"] == _last_user_action_hash(raw_doc)


def test_swipe_of_code_staged_known_foe_preserves_the_same_visible_intent():
    cfg = _cfg()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="primer-swipe")
    apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "stats": {"STR": 12}, "skills": {"athletics": 1},
            "resources": {"hp": {"max": 24}},
        }},
        {"op": "entity_add", "name": "Marshal Varo", "kind": "npc"},
        {"op": "presence", "entity": "Marshal Varo", "present": True},
    ], "genesis", cfg)
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, rng=random.Random(23))
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": (
        "I draw my longsword and challenge Marshal Varo as the hostile opponent. "
        "I hold position and watch him commit."
    )}]}).encode()
    first, first_ctx = pipe.process(
        Stamp(session="primer-swipe", gen_type="normal", turn=1, user="Kael"), body)
    assert b"PRIVATE COMBAT NARRATION PRIMER" in first
    assert b"[ENEMY INTENT enemy-intent/1]" in first
    reply = json.dumps({"choices": [{"message": {"content": (
        "Marshal Varo holds the exact code-signaled threat at the ready."
    )}}]}).encode()
    pipe.on_response(first_ctx, reply, "application/json")

    swipe, _ = pipe.process(
        Stamp(session="primer-swipe", gen_type="swipe", turn=1, user="Kael"), body)
    text = swipe.decode()

    assert "PRIVATE COMBAT NARRATION PRIMER" in text
    assert "FRESH-FOE NARRATION RETRY" not in text
    assert "[ENEMY INTENT enemy-intent/1]" in text
    packet = json.loads(swipe)
    internal = "\n".join(str(row.get("content", "")) for row in packet["messages"]
                         if row.get("role") == "system")
    assert "\n[WAR] round " in internal
