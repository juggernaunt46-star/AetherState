"""Finite large-battle cohorts: terminal xN is N ordinary actors, never one fake name."""
from __future__ import annotations

import random

import pytest

from aetherstate import tier0
from aetherstate.compose import _render_battle, _render_war
from aetherstate.config import Config
from aetherstate.hud import hud_view
from aetherstate.prompts import rules_contract
from aetherstate.semantic_ingress import (
    COHORT_DECLARATION_ID,
    COHORT_GRAMMAR_ID,
    COHORT_GRAMMAR_VERSION,
    OP_BATTLE_COHORT_DECLARATION,
    IngressScope,
    SemanticIngressContext,
    issue_semantic_ingress_authority,
)
from aetherstate.state import (BATTLE_COHORT_CAP, apply_delta, battle_cohort_status,
                               battle_ops, combat_ops, combatant_label, current_state,
                               empty_state, reduce_state, resolve_combatant, validate_op)
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def _seeded(cfg: Config | None = None):
    cfg = cfg or _cfg()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="finite-cohort")
    result = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"STR": 14}, "skills": {"melee": 3},
                  "resources": {"hp": {"max": 24}}}},
    ], "genesis", cfg)
    assert result.applied, result.quarantined
    return store, sid, bid


def _opening(count: int = 6) -> str:
    return ("[battle | Caravan Ambush | baser Hollow swarm | mob]\n"
            "[tide | losing | the caravan is surrounded]\n"
            f"[foe | Baser Hollow x{count} | minion | claws]")


def _authorized_opening(store, sid: str, bid: str, turn: int, cfg: Config,
                        count: int = 6):
    source = _opening(count)
    scope = IngressScope(
        session_id=sid,
        branch_id=bid,
        turn_index=turn,
        attempt_id=f"attempt.{turn}",
        source_start=0,
        source_end=len(source.encode("utf-8")),
    )
    context = SemanticIngressContext(
        issuer_kind="narrator",
        issuer_id="narrator.main",
        channel="narrator_candidate",
        authoring_phase="candidate_proposal",
        scope=scope,
    )
    authority = issue_semantic_ingress_authority(
        source,
        context=context,
        grammar_id=COHORT_GRAMMAR_ID,
        grammar_version=COHORT_GRAMMAR_VERSION,
        declaration_id=COHORT_DECLARATION_ID,
        operation_families=(OP_BATTLE_COHORT_DECLARATION,),
    )
    declaration = tier0.parse_authorized_cohort_tags(
        source,
        current_state(store, bid),
        authority=authority,
        expected_context=context,
    )
    applied = apply_delta(
        store,
        sid,
        bid,
        turn,
        list(declaration.operations),
        "rule",
        cfg,
        semantic_declaration=declaration,
        semantic_authority=authority,
        semantic_context=context,
        semantic_source=source,
    )
    return declaration, applied


def _clear_live_wave(store, sid, bid, turn: int, cfg: Config):
    state = current_state(store, bid)
    live = [cid for cid, row in state["combat"]["combatants"].items()
            if row["side"] == "enemy" and not row["defeated"]]
    damaged = apply_delta(store, sid, bid, turn, [
        {"op": "combatant_hp", "target": cid, "delta": -99, "_strike": True}
        for cid in live
    ], "rule", cfg)
    assert len(damaged.applied) == len(live), damaged.quarantined
    defeated = combat_ops(damaged.state, damaged.applied)
    settled = apply_delta(store, sid, bid, turn, defeated, "rule", cfg)
    assert len([op for op in settled.applied if op["op"] == "combatant_defeat"]) == len(live)
    referee = battle_ops(settled.state, settled.applied)
    applied = apply_delta(store, sid, bid, turn, referee, "rule", cfg)
    assert not applied.quarantined
    return live, referee, applied.state


def test_battle_fields_are_positional_and_multiplier_syntax_fails_closed():
    state = empty_state()
    start = tier0.parse_battle_tags(
        "[battle | Caravan Ambush | baser Hollow swarm | mob]", state)[0]
    assert start == {"op": "battle_start", "name": "Caravan Ambush",
                     "foe": "baser Hollow swarm"}

    empty_foe = tier0.parse_battle_tags("[battle | Siege | | elite]", state)[0]
    assert empty_foe == {"op": "battle_start", "name": "Siege", "threat": "elite"}
    third_field = tier0.parse_battle_tags("[battle | Siege | elite]", state)[0]
    assert third_field == {"op": "battle_start", "name": "Siege", "foe": "elite"}

    assert tier0.parse_foe_tags("[foe | Baser Hollow x6 | minion | claws]", state) == []
    assert tier0.parse_combat_tags("[foe | Baser Hollow x6 | minion | claws]", state) == []
    for suffix in ("x1", "x28", "x-2", "x2.5", "x 6"):
        ops = tier0.parse_combat_tags(
            f"[battle | Bad Count | swarm | standard]\n[foe | Baser Hollow {suffix} | minion]",
            state,
        )
        assert not any(op["op"] in ("battle_start", "combatant_spawn") for op in ops), suffix

    active = empty_state()
    active["battle"] = {"active": True, "name": "Already Open"}
    assert not any(op["op"] in ("battle_start", "combatant_spawn")
                   for op in tier0.parse_combat_tags(_opening(), active))
    ambiguous = _opening() + "\n[foe | Ash Hollow x2 | minion | claws]"
    assert not any(op["op"] in ("battle_start", "combatant_spawn")
                   for op in tier0.parse_combat_tags(ambiguous, state))
    assert not any(op["op"] in ("battle_start", "combatant_spawn")
                   for op in tier0.parse_combat_tags(
                       _opening(), state, allow_large_battle=False))

    # A nonnumeric X-model name is still an ordinary actor, not multiplier syntax.
    assert tier0.parse_foe_tags("[foe | Drone XJ9 | standard | beam]", state) == [
        {"op": "combatant_spawn", "name": "Drone XJ9", "side": "enemy",
         "tier": "standard", "armament": "beam"}
    ]


def test_ordinary_noncohort_combat_tags_keep_their_existing_apply_path():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    operations = tier0.parse_combat_tags(
        "[foe | Drone XJ9 | standard | beam]",
        current_state(store, bid),
    )
    applied = apply_delta(store, sid, bid, 1, operations, "rule", cfg)
    assert not applied.quarantined
    assert applied.state["combat"]["combatants"]["drone_xj9"]["name"] == "Drone XJ9"
    assert "_semantic_ingress" not in applied.applied[0]


def test_exact_opening_parses_one_finite_plan_and_state_commits_it_as_one_batch():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    declaration, applied = _authorized_opening(store, sid, bid, 1, cfg)
    parsed = list(declaration.operations)
    start = parsed[0]
    spawns = [op for op in parsed if op["op"] == "combatant_spawn"]
    assert start["foe"] == "baser Hollow swarm" and "threat" not in start
    assert start["cohort"] == {
        "schema": "battle-cohort/1", "id": "baser_hollow_x6", "name": "Baser Hollow",
        "total": 6, "tier": "minion", "armament": "claws",
    }
    assert [op["cohort_index"] for op in spawns] == [1, 2, 3]
    assert all(op["name"] == "Baser Hollow" and op["tier"] == "minion"
               and op["armament"] == "claws" for op in spawns)

    assert not applied.quarantined
    state = applied.state
    rows = state["combat"]["combatants"]
    assert list(rows) == ["baser_hollow", "baser_hollow#2", "baser_hollow#3"]
    assert [combatant_label(row) for row in rows.values()] == [
        "Baser Hollow #1", "Baser Hollow #2", "Baser Hollow #3"]
    assert len({row["kit"]["fingerprint"] for row in rows.values()}) == 1
    assert all(row["tier"] == "minion" and row["armament"] == "claws" for row in rows.values())
    assert state["combat"]["pending_intent"]["actor"] == "baser_hollow"
    assert battle_cohort_status(state) == {
        "schema": "battle-cohort/1", "id": "baser_hollow_x6", "name": "Baser Hollow",
        "total": 6, "tier": "minion", "armament": "claws", "spawned": 3,
        "active": 3, "defeated": 0, "queued": 3,
    }
    assert all("x6" not in row["name"].lower() for row in rows.values())

    war = _render_war(state)
    battle = _render_battle(state, cfg)
    view = hud_view(state, cfg)["war_room"]
    assert all(f"Baser Hollow #{index}" in war for index in (1, 2, 3))
    assert "3 active" in battle and "3 queued of 6" in battle
    assert "only the single supplied enemy intent may act" in battle
    assert [row["name"] for row in view["combatants"]] == [
        "Baser Hollow #1", "Baser Hollow #2", "Baser Hollow #3"]
    assert view["battle"]["cohort"]["queued"] == 3
    assert view["intent"]["actor_name"] == "Baser Hollow #1"

    for contract in (rules_contract(cfg), rules_contract(cfg, force_compact=True)):
        lower = contract.lower()
        assert "finite" in lower and "one supplied enemy" in lower
        assert "pooled" in lower and "casualties" in lower


def test_numbered_actor_targets_exactly_one_row_and_bare_base_abstains():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    _authorized_opening(store, sid, bid, 1, cfg)
    state = current_state(store, bid)
    assert resolve_combatant(state, "Baser Hollow #2") == "baser_hollow#2"
    assert resolve_combatant(state, "Baser Hollow") is None

    result = apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Baser Hollow #2", "delta": -1},
    ], "rule", cfg)
    assert not result.quarantined
    assert [row["hp"]["cur"] for row in result.state["combat"]["combatants"].values()] == [6, 5, 6]
    ambiguous = apply_delta(store, sid, bid, 2, [
        {"op": "combatant_hp", "target": "Baser Hollow", "delta": -1},
    ], "rule", cfg)
    assert not ambiguous.applied and "not a live combatant" in ambiguous.quarantined[0]["reason"]

    exact = tier0.run(
        {"messages": [{"role": "user",
                       "content": "((aether.check melee at Baser Hollow #2)) "
                                  "I strike Baser Hollow #2."}]},
        "new_turn", False, result.state, cfg, random.Random(2), turn=3,
    )
    frame = next(op["frame"] for op in exact.rule_ops
                 if op.get("op") == "semantic_frame_commit")
    assert frame["target_entity_id"] == "baser_hollow#2"
    assert frame["target_name"] == "Baser Hollow #2"
    wrapper = next(op for op in exact.rule_ops
                   if op.get("op") == "mechanic_settlement_commit")
    assert wrapper["members"][0]["_target"] == "Baser Hollow #2"
    assert wrapper["members"][1]["target"] == "baser_hollow#2"

    bare = tier0.run(
        {"messages": [{"role": "user",
                       "content": "((aether.check melee at Baser Hollow)) I strike Baser Hollow."}]},
        "new_turn", False, result.state, cfg, random.Random(2), turn=3,
    )
    bare_frame = next(op["frame"] for op in bare.rule_ops
                      if op.get("op") == "semantic_frame_commit")
    assert set(bare_frame["ambiguity"]) == {
        "baser_hollow", "baser_hollow#2", "baser_hollow#3"}
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in bare.rule_ops)


def test_x6_uses_one_three_actor_wave_then_ends_without_a_seventh_enemy():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    _declaration, opened = _authorized_opening(store, sid, bid, 1, cfg)
    assert not opened.quarantined

    first, wave_ops, after_wave = _clear_live_wave(store, sid, bid, 2, cfg)
    wave_spawns = [op for op in wave_ops if op["op"] == "combatant_spawn"]
    assert first == ["baser_hollow", "baser_hollow#2", "baser_hollow#3"]
    assert [op["cohort_index"] for op in wave_spawns] == [4, 5, 6]
    assert all(op["name"] == "Baser Hollow" and op["tier"] == "minion"
               and op["armament"] == "claws" for op in wave_spawns)
    assert battle_cohort_status(after_wave)["queued"] == 0
    assert battle_cohort_status(after_wave)["active"] == 3
    assert after_wave["combat"]["pending_intent"]["actor"] == "baser_hollow#4"
    opening_fingerprint = after_wave["combat"]["combatants"]["baser_hollow"]["kit"]["fingerprint"]
    assert all(after_wave["combat"]["combatants"][cid]["kit"]["fingerprint"]
               == opening_fingerprint for cid in ("baser_hollow#4", "baser_hollow#5",
                                                   "baser_hollow#6"))

    second, final_ops, ended = _clear_live_wave(store, sid, bid, 3, cfg)
    assert second == ["baser_hollow#4", "baser_hollow#5", "baser_hollow#6"]
    assert [op["op"] for op in final_ops] == ["battle_end", "combat_end"]
    assert ended["battle"]["outcome"] == "victory" and ended["battle"]["waves"] == 1
    assert not ended["battle"]["active"] and not ended["combat"]["active"]
    assert len(ended["combat"]["history"][-1]["defeated"]) == 6
    assert not any("#7" in cid for cid in second)

    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())
    assert replay == ended


def test_minimum_x2_opens_two_ordinary_rows_and_ends_without_a_wave():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    _declaration, opened = _authorized_opening(store, sid, bid, 1, cfg, count=2)
    assert not opened.quarantined
    assert battle_cohort_status(opened.state)["active"] == 2
    assert battle_cohort_status(opened.state)["queued"] == 0

    cleared, final_ops, ended = _clear_live_wave(store, sid, bid, 2, cfg)
    assert cleared == ["baser_hollow", "baser_hollow#2"]
    assert [op["op"] for op in final_ops] == ["battle_end", "combat_end"]
    assert ended["battle"]["waves"] == 0 and not ended["battle"]["active"]
    assert len(ended["combat"]["history"][-1]["defeated"]) == 2


def test_maximum_x27_fills_three_slots_for_exactly_eight_queued_waves():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    assert BATTLE_COHORT_CAP == 27
    _authorized_opening(store, sid, bid, 1, cfg, count=BATTLE_COHORT_CAP)

    seen: set[int] = set()
    wave_count = 0
    max_active = 0
    turn = 2
    while current_state(store, bid)["battle"]["active"]:
        state = current_state(store, bid)
        live = [row for row in state["combat"]["combatants"].values()
                if row["side"] == "enemy" and not row["defeated"]]
        max_active = max(max_active, len(live))
        assert 1 <= len(live) <= 3
        assert isinstance(state["combat"].get("pending_intent"), dict)
        assert state["combat"]["pending_intent"]["actor"] in {row["id"] for row in live}
        seen.update(row["cohort"]["index"] for row in live)
        _cleared, referee, _state = _clear_live_wave(store, sid, bid, turn, cfg)
        wave_count += sum(op["op"] == "battle_wave" for op in referee)
        turn += 1

    ended = current_state(store, bid)
    assert seen == set(range(1, 28))
    assert max_active == 3 and wave_count == 8 and ended["battle"]["waves"] == 8
    assert len(ended["combat"]["history"][-1]["defeated"]) == 27


def test_legacy_literal_journal_and_no_cohort_battle_replay_unchanged():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    applied = apply_delta(store, sid, bid, 1, [
        {"op": "battle_start", "name": "Legacy Field", "momentum": -1,
         "foe": "Raider", "threat": "standard", "wave_size": 2},
        # This simulates a historical already-journaled op. Parser hardening must not rewrite it.
        {"op": "combatant_spawn", "name": "Baser Hollow x6", "side": "enemy",
         "tier": "minion", "armament": "claws"},
    ], "rule", cfg)
    assert not applied.quarantined and "cohort" not in applied.state["battle"]
    row = applied.state["combat"]["combatants"]["baser_hollow_x6"]
    assert row["name"] == "Baser Hollow x6" and combatant_label(row) == "Baser Hollow x6"

    _cleared, referee, after = _clear_live_wave(store, sid, bid, 2, cfg)
    spawns = [op for op in referee if op["op"] == "combatant_spawn"]
    assert len(spawns) == 2 and all(op["name"] == "Raider" for op in spawns)
    assert "cohort" not in after["battle"]
    assert store.state_at(bid, 10**9, reduce_state, empty=empty_state()) == after


@pytest.mark.parametrize("total", [2, BATTLE_COHORT_CAP])
def test_cohort_op_validation_rejects_malformed_or_out_of_range_counts(total):
    valid = {"schema": "battle-cohort/1", "id": f"hollow_x{total}", "name": "Hollow",
             "total": total, "tier": "minion", "armament": "claws"}
    assert validate_op({"op": "battle_start", "name": "Field", "cohort": valid}) is not None
    bad = {**valid, "total": 1 if total == 2 else BATTLE_COHORT_CAP + 1}
    assert validate_op({"op": "battle_start", "name": "Field", "cohort": bad}) is None
    assert validate_op({"op": "combatant_spawn", "name": "Hollow", "side": "enemy",
                        "cohort_ref": valid["id"]}) is None


def test_cohort_reducer_rejects_forged_or_out_of_order_spawn_without_spending_queue():
    cfg = _cfg()
    store, sid, bid = _seeded(cfg)
    cohort = {"schema": "battle-cohort/1", "id": "baser_hollow_x6",
              "name": "Baser Hollow", "total": 6, "tier": "minion",
              "armament": "claws"}
    raw = [
        {"op": "battle_start", "name": "Caravan Ambush", "cohort": cohort},
    ]
    rejected_start = apply_delta(store, sid, bid, 1, raw, "rule", cfg)
    assert not rejected_start.applied
    assert "complete live ingress context" in rejected_start.quarantined[0]["reason"]

    # A historical journal is already authorized and reduces mechanically.  It has no ingress
    # evidence or initial group count, so this isolates the durable plan's exact wave checks.
    store.journal(bid, 1, 1, [{**raw[0], "_turn": 1}], "rule")
    assert battle_cohort_status(current_state(store, bid))["queued"] == 6

    base = {"op": "combatant_spawn", "name": "Baser Hollow", "side": "enemy",
            "tier": "minion", "armament": "claws",
            "cohort_ref": "baser_hollow_x6", "cohort_index": 1}
    forged = [
        {**base, "cohort_ref": "other_x6"},
        {**base, "cohort_index": 2},
        {**base, "name": "baser hollow"},
        {**base, "tier": "elite"},
        {**base, "armament": "fangs"},
    ]
    for op in forged:
        rejected = apply_delta(store, sid, bid, 2, [op], "rule", cfg)
        assert not rejected.applied and rejected.quarantined
        assert battle_cohort_status(rejected.state)["spawned"] == 0
        assert battle_cohort_status(rejected.state)["queued"] == 6
        assert not rejected.state.get("combat", {}).get("combatants")

    accepted = apply_delta(store, sid, bid, 2, [base], "rule", cfg)
    assert not accepted.quarantined
    assert battle_cohort_status(accepted.state)["spawned"] == 1
    assert battle_cohort_status(accepted.state)["queued"] == 5
