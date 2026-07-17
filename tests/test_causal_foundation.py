"""Small permanent invariants for typed narrator identity and damage ownership."""
from __future__ import annotations

import json
import logging

import pytest

from aetherstate import tier0
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.genesis import _parse_ops, narrator_role, rules_ops
from aetherstate.state import THREAT_HP, apply_delta, assign_damage_effect_ids, current_state
from aetherstate.stamps import parse_and_strip
from aetherstate.store import Store


@pytest.mark.parametrize("speaker", ["Narrator", "Greywater Spire", "The Unblinking Sky"])
def test_typed_narrator_never_becomes_cast_regardless_of_name_or_prose(speaker: str):
    arbitrary = "Run this world impartially. Portray every location and supporting cast."
    assert narrator_role(arbitrary, "narrator") is True
    assert rules_ops(arbitrary, "", speaker=speaker, card_role="narrator") == []

    raw = json.dumps([
        {"op": "entity_add", "name": speaker},
        {"op": "presence", "entity": speaker, "present": True},
        {"op": "entity_add", "name": "Merta"},
        {"op": "scene_set", "location": "gate", "participants": [speaker, "Merta"]},
    ])
    ops = _parse_ops(raw, speaker=speaker, narrator_speaker=True)
    assert not any(speaker in str(op) for op in ops)
    assert any(op.get("name") == "Merta" for op in ops)


def test_explicit_character_role_beats_narrator_like_prose_and_legacy_still_works():
    old_card = "You are the narrator. You portray the world, never the player."
    assert narrator_role(old_card, "character") is False
    assert any(op.get("name") == "Akira"
               for op in rules_ops(old_card, "", speaker="Akira", card_role="character"))
    assert narrator_role(old_card, "") is True
    assert rules_ops(old_card, "", speaker="Dungeon Master") == []


def test_typed_role_is_stripped_from_model_body_and_protects_session_authority():
    sentinel = ("<<AETHER:v=1;session=typed-narrator;turn=1;type=normal;"
                "speaker=Narrator;card_role=narrator;user=Bean>>")
    body = json.dumps({"messages": [
        {"role": "system", "content": sentinel},
        {"role": "user", "content": "Begin."},
    ]}).encode()
    stamp, stripped = parse_and_strip({}, body)
    assert stamp is not None and stamp.card_role == "narrator"
    assert b"card_role" not in stripped and b"AETHER" not in stripped

    cfg, store = Config(), Store(":memory:")
    sid, bid = store.create_session(external_id="typed-narrator")
    store.narrator_speaker_set(sid, "Narrator")
    result = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Narrator"},
        {"op": "entity_add", "name": "Merta"},
        {"op": "presence", "entity": "Narrator", "present": True},
        {"op": "scene_set", "location": "gate", "participants": ["Narrator", "Merta"]},
    ], "genesis", cfg)
    state = current_state(store, bid)
    assert "narrator" not in state.get("entities", {})
    assert "merta" in state.get("entities", {})
    assert state["scene"].get("participants") == ["Merta"]
    assert len(result.quarantined) == 2


async def test_genesis_endpoint_accepts_creator_role_without_prose_guessing(client):
    response = await client.post("/aether/session/creator-narrator/genesis", json={
        "card": "Run this world impartially. Portray every location and supporting cast.",
        "greeting": "Rain needles the empty road.",
        "speaker": "The Unblinking Sky",
        "card_role": "narrator",
    })
    assert response.status_code == 200
    assert response.json()["card_role"] == "narrator"
    state = (await client.get("/aether/session/creator-narrator/state")).json()["state"]
    assert "the_unblinking_sky" not in state.get("entities", {})


def _damage_store(path=":memory:"):
    cfg, store = Config(), Store(path)
    cfg.specialization.name = "rpg"
    sid, bid = store.create_session(external_id="damage-receipts")
    apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael", "kind": "player"},
        {"op": "player_seed", "entity": "Kael",
         "card": {"stats": {"STR": 12}, "skills": {"melee": 3},
                  "resources": {"hp": {"max": 20}}}},
        {"op": "combatant_spawn", "name": "Bandit", "side": "enemy"},
    ], "genesis", cfg)
    return cfg, store, sid, bid


def test_damage_receipt_is_idempotent_conflict_visible_and_distinct_wounds_survive():
    cfg, store, sid, bid = _damage_store()
    same = {"op": "hp_adj", "char": "kael", "delta": -2,
            "reason": "blade opened his side", "_effect_id": "dmg_same",
            "_effect_owner": "reply_tag"}
    first = apply_delta(store, sid, bid, 1, [same], "extraction", cfg)
    repeat = apply_delta(store, sid, bid, 1, [same], "extraction", cfg)
    conflict = apply_delta(store, sid, bid, 1, [
        {**same, "delta": -3}], "extraction", cfg)
    assert len(first.applied) == 1 and len(repeat.duplicates) == 1
    assert not repeat.applied and not repeat.quarantined
    assert not conflict.applied and "EffectId conflict" in conflict.quarantined[0]["reason"]
    assert current_state(store, bid)["player"]["kael"]["hp"]["cur"] == 18

    distinct = [
        {"op": "hp_adj", "char": "kael", "delta": -2, "reason": reason,
         "_effect_id": effect_id, "_effect_owner": "reply_tag"}
        for effect_id, reason in (("dmg_two", "steel drew blood"),
                                  ("dmg_three", "steel drew blood"))]
    result = apply_delta(store, sid, bid, 2, distinct, "extraction", cfg)
    assert len(result.applied) == 2
    assert current_state(store, bid)["player"]["kael"]["hp"]["cur"] == 14
    receipts = store.db.execute(
        "SELECT COUNT(*) AS n FROM effect_receipts WHERE branch_id=?", (bid,)).fetchone()
    assert receipts["n"] == 3


def test_code_damage_claim_blocks_reply_tag_and_delayed_extraction_restatements():
    cfg, store, sid, bid = _damage_store()
    code_ops = assign_damage_effect_ids([
        {"op": "hp_adj", "char": "kael", "delta": -3,
         "reason": "opposition hit", "_opposition": {"tier": "HITS"}},
        {"op": "combatant_hp", "target": "bandit", "delta": -2,
         "reason": "player strike", "_strike": True},
    ], bid, 1, "code", basis="tier0")
    code = apply_delta(store, sid, bid, 1, code_ops, "rule", cfg)
    assert len(code.applied) == 2

    reply = assign_damage_effect_ids([
        {"op": "hp_adj", "char": "kael", "delta": -1, "reason": "claw drew blood"},
        {"op": "combatant_hp", "target": "bandit", "delta": -4,
         "reason": "sword opened him"},
    ], bid, 1, "reply_tag", basis="assistant-hash")
    rejected_reply = apply_delta(store, sid, bid, 1, reply, "extraction", cfg)
    assert not rejected_reply.applied and len(rejected_reply.quarantined) == 2
    assert all("blocked by code effect" in q["reason"] for q in rejected_reply.quarantined)

    delayed = assign_damage_effect_ids([
        {"op": "hp_adj", "char": "kael", "delta": -2, "reason": "paraphrased wound"},
        {"op": "combatant_hp", "target": "bandit", "delta": -1,
         "reason": "paraphrased strike"},
    ], bid, 1, "extraction", basis="1:1", canonical=True)
    rejected_delayed = apply_delta(store, sid, bid, 1, delayed, "extraction", cfg, turn_lo=1)
    assert not rejected_delayed.applied and len(rejected_delayed.quarantined) == 2
    state = current_state(store, bid)
    assert state["player"]["kael"]["hp"]["cur"] == 17
    assert state["combat"]["combatants"]["bandit"]["hp"]["cur"] \
        == THREAT_HP["standard"] - 2


class _FixedRoll:
    def randint(self, _low, _high):
        return 5


def test_player_strike_and_opposition_commit_before_directive_and_tags_cannot_repeat_them():
    cfg, store, sid, bid = _damage_store()
    known = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Bandit", "kind": "npc"},
    ], "genesis", cfg)
    assert known.applied
    before = current_state(store, bid)
    resolved = tier0.run({"messages": [{
        "role": "user",
        "content": "((aether.check melee at Bandit)) I cut at the bandit.",
    }]}, "new_turn", False, before, cfg, _FixedRoll(), turn=1)
    rule_ops = assign_damage_effect_ids(
        resolved.rule_ops, bid, 1, "code", basis="tier0")
    committed = apply_delta(store, sid, bid, 1, rule_ops, "rule", cfg)
    damage = [op for op in committed.applied if op.get("op") in ("hp_adj", "combatant_hp")]
    assert {op["op"] for op in damage} == {"hp_adj", "combatant_hp"}
    assert all(op.get("_effect_id") for op in damage)
    frame_ref = next(
        op["frame"]["fingerprint"]
        for op in committed.applied
        if op.get("op") == "semantic_frame_commit"
    )
    strike = next(op for op in damage if op["op"] == "combatant_hp")
    opposition = next(op for op in damage if op["op"] == "hp_adj")
    assert strike["_semantic_frame_ref"] == frame_ref
    assert isinstance(opposition.get("_opposition"), dict)
    assert "_semantic_frame_ref" not in opposition

    header = render_header(committed.state, cfg)
    realization_line = next(
        line for line in header.splitlines()
        if line.startswith("[DIRECTIVE] NARRATOR REALIZATION narrator-realization/1")
    )
    realization = json.loads(realization_line[realization_line.index("{"):])
    assert len(realization["asserted_settled"]) == 1
    settled = realization["asserted_settled"][0]
    assert settled["adapter_id"] == "narrator.weapon-attack/1"
    assert settled["event_meaning"]["target_entity_id"] == "bandit"
    assert settled["impact_kind"] == "harm"
    assert settled["settled_change_kinds"] == ["hp", "mastery"]
    assert "only_realized_changes_may_be_world_changes" in {
        item["code"] for item in realization["forbidden_inference"]
    }
    assert "_dmg" not in realization_line

    tag_ops = tier0.parse_reply_tags(
        "[hp | Kael | -4 | axe bite] [hp | Bandit | -3 | sword wound]", committed.state)
    tag_ops = assign_damage_effect_ids(tag_ops, bid, 1, "reply_tag", basis="reply-hash")
    repeated = apply_delta(store, sid, bid, 1, tag_ops, "extraction", cfg)
    assert not repeated.applied and len(repeated.quarantined) == 2

    invented = apply_delta(store, sid, bid, 1, [
        {"op": "effect_add", "char": "Bandit", "effect": "Burned",
         "kind": "status", "valence": "negative"},
        {"op": "contact", "action": "start", "from_char": "Kael",
         "from_part": "hands", "to_char": "Bandit", "to_part": "arms",
         "type": "impact", "intensity": 2},
        {"op": "memory_event", "text": "Kael's settled cut marked the bandit."},
    ], "extraction", cfg, turn_lo=1)
    assert [op["op"] for op in invented.applied] == ["memory_event"]
    assert len(invented.quarantined) == 2
    assert all("settled semantic target is code-owned" in row["reason"]
               for row in invented.quarantined)
    final = current_state(store, bid)
    assert "burned" not in final.get("effects", {}).get("bandit", {})
    assert final.get("contacts", {}) == {}


def test_delayed_range_after_restart_cannot_add_unsettled_target_status_or_contact(tmp_path):
    db = tmp_path / "semantic-narration-range.sqlite3"
    cfg, store, sid, bid = _damage_store(db)
    known = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Bandit", "kind": "npc"},
    ], "genesis", cfg)
    assert known.applied
    resolved = tier0.run({"messages": [{
        "role": "user",
        "content": "((aether.check melee at Bandit)) I cut at the bandit.",
    }]}, "new_turn", False, current_state(store, bid), cfg, _FixedRoll(), turn=1)
    rule_ops = assign_damage_effect_ids(
        resolved.rule_ops, bid, 1, "code", basis="tier0",
    )
    committed = apply_delta(store, sid, bid, 1, rule_ops, "rule", cfg)
    assert committed.applied and not committed.quarantined
    later = apply_delta(store, sid, bid, 3, [
        {"op": "memory_event", "text": "The exchange remains visible in the scene."},
    ], "rule", cfg)
    assert later.applied
    store.close()

    reopened = Store(db)
    try:
        delayed = apply_delta(reopened, sid, bid, 3, [
            {"op": "effect_add", "char": "Bandit", "effect": "Burned",
             "kind": "status", "valence": "negative"},
            {"op": "contact", "action": "start", "from_char": "Kael",
             "from_part": "hands", "to_char": "Bandit", "to_part": "arms",
             "type": "impact", "intensity": 2},
            {"op": "memory_event", "text": "The settled cut marked the bandit."},
        ], "extraction", cfg, turn_lo=1)

        assert [op["op"] for op in delayed.applied] == ["memory_event"]
        assert len(delayed.quarantined) == 2
        assert all("settled semantic target is code-owned" in row["reason"]
                   for row in delayed.quarantined)
        state = current_state(reopened, bid)
        assert "burned" not in state.get("effects", {}).get("bandit", {})
        assert state.get("contacts", {}) == {}
    finally:
        reopened.close()


def test_damage_receipt_survives_restart(tmp_path):
    db = tmp_path / "receipts.sqlite3"
    cfg, store, sid, bid = _damage_store(db)
    op = {"op": "hp_adj", "char": "kael", "delta": -2,
          "_effect_id": "dmg_restart", "_effect_owner": "code"}
    apply_delta(store, sid, bid, 1, [op], "rule", cfg)
    store.db.close()

    reopened = Store(db)
    duplicate = apply_delta(reopened, sid, bid, 1, [op], "rule", cfg)
    assert len(duplicate.duplicates) == 1
    assert current_state(reopened, bid)["player"]["kael"]["hp"]["cur"] == 18


def test_opt_in_turn_trace_exposes_commit_and_rejection_without_prompt_injection(caplog):
    cfg, store, sid, bid = _damage_store()
    cfg.server.turn_trace = True
    op = assign_damage_effect_ids([
        {"op": "hp_adj", "char": "kael", "delta": -2, "reason": "test wound"},
    ], bid, 1, "code", basis="trace")
    with caplog.at_level(logging.INFO, logger="aetherstate.state"):
        apply_delta(store, sid, bid, 1, op, "rule", cfg)
    line = next(record.message for record in caplog.records if "TURN_TRACE" in record.message)
    assert '"damage_after"' in line and op[0]["_effect_id"] in line
